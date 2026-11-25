"""Corporate actions (splits): ajuste retroactivo de la serie diaria.

IOL **no** ajusta la serie histórica cuando un papel hace un split. El cache
diario queda con un escalón (por ejemplo un 10:1 lleva el cierre de 1000 a 100) y los
indicadores que salen de ahí son basura que *parece válida*: ATRd 96%, EMAs en
la escala vieja, stop inicial por debajo de cero.

Dos piezas complementarias:

1. `aplicar_splits()` reescala la serie con los splits declarados en
   `data/splits.json`, y corre **dentro** de `build_daily_cache` (antes de
   escribir el CSV). Como el cache se reconstruye entero desde la API en cada
   arranque, el ajuste es idempotente por construcción: siempre se aplica sobre
   el crudo, nunca sobre algo ya ajustado.

2. `detectar_split()` deriva el factor del feed en vivo, sin heurística: IOL
   reporta `variacion` contra el cierre previo **ya ajustado**, así que
   `ultimoPrecio / (1 + variacion/100)` es el cierre previo según el feed.
   Compararlo contra el último cierre del cache da el factor exacto — y de paso
   distingue un split de un derrumbe real, porque en una caída genuina la
   variación reportada *es* la caída y el cociente da ~1.

Decisión de diseño: se detecta automáticamente pero se ajusta **solo** con
factores limpios (2:1, 10:1, o sus recíprocos). Ante un salto que no encaja,
esto no inventa un factor: avisa fuerte y deja que el caller excluya el
símbolo. Reescalar mal es peor que no operar.

El camino live (`argbot/brokers/iol/market.py`) no usa este módulo: lee la historia sin
ajustar y bloquea el símbolo cuando detecta un salto de escala.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger("argbot.strategy.corporate")

SPLITS_PATH = Path("data/splits.json")

# Por debajo de esto no se mira: ningún split es menor a 2:1 (=50%), y así los
# dividendos en acciones (típicamente <15%) no generan falsos positivos.
UMBRAL_DETECCION = 0.15
# Cuán cerca de un entero tiene que estar el cociente para llamarlo split. El
# margen absorbe el movimiento de precio del propio día del split.
TOL_FACTOR = 0.03
# Salto grande que NO encaja con ningún factor limpio → símbolo no confiable.
UMBRAL_EXCLUSION = 0.35


@dataclass(frozen=True)
class Split:
    """Un evento de split declarado. `factor` >1 divide (10:1), <1 multiplica (reverse)."""
    simbolo: str
    fecha:   date          # primera rueda que ya cotiza POST-split
    factor:  float
    origen:  str = "manual"
    nota:    str = ""


@dataclass(frozen=True)
class Discrepancia:
    """Incoherencia entre el último cierre del cache y el cierre previo del feed."""
    simbolo:      str
    ratio:        float
    cierre_cache: float
    cierre_feed:  float
    factor:       float | None   # None = no encaja con ningún split limpio

    @property
    def excluir(self) -> bool:
        """Salto grande sin explicación → no emitir indicadores, sacar de la shortlist."""
        return self.factor is None and abs(self.ratio - 1) >= UMBRAL_EXCLUSION


# ── persistencia ────────────────────────────────────────────────────────────────

def cargar_splits(path: Path = SPLITS_PATH) -> dict[str, list[Split]]:
    """{simbolo: [Split, ...]}. Archivo ausente → {} (no es un error)."""
    if not path.exists():
        return {}
    try:
        crudo = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # Fallar ruidoso: un splits.json roto haría operar con la serie sin ajustar,
        # que es exactamente el bug que este módulo existe para evitar.
        logger.error("splits.json ilegible (%s) — la serie diaria queda SIN ajustar", e)
        return {}
    out: dict[str, list[Split]] = {}
    for simbolo, eventos in crudo.items():
        ss = []
        for ev in eventos:
            try:
                ss.append(Split(
                    simbolo=simbolo,
                    fecha=date.fromisoformat(ev["fecha"]),
                    factor=float(ev["factor"]),
                    origen=ev.get("origen", "manual"),
                    nota=ev.get("nota", ""),
                ))
            except (KeyError, ValueError) as e:
                logger.error("Entrada de split inválida en %s: %r (%s) — ignorada",
                             simbolo, ev, e)
        if ss:
            out[simbolo] = sorted(ss, key=lambda s: s.fecha)
    return out


def guardar_split(split: Split, path: Path = SPLITS_PATH) -> bool:
    """Agrega el split al archivo. Devuelve False si ya estaba (mismo símbolo+fecha)."""
    actuales = cargar_splits(path)
    if any(s.fecha == split.fecha for s in actuales.get(split.simbolo, [])):
        return False
    crudo: dict = {}
    if path.exists():
        try:
            crudo = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            crudo = {}
    crudo.setdefault(split.simbolo, []).append({
        "fecha":  split.fecha.isoformat(),
        "factor": split.factor,
        "origen": split.origen,
        "nota":   split.nota,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(crudo, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


# ── aplicación ──────────────────────────────────────────────────────────────────

def _a_date(v) -> date:
    """`build_daily_cache` maneja fechas ISO; `load_daily` las devuelve como date."""
    return v if isinstance(v, date) else date.fromisoformat(v)


def aplicar_splits(simbolo: str, filas: list[dict],
                   splits: dict[str, list[Split]]) -> list[dict]:
    """Reescala las barras ANTERIORES a cada split para dejarlas en la escala actual.

    Precios /= factor y volumen *= factor (un split 10:1 decuplica los nominales).
    Devuelve una lista nueva; no muta la entrada.
    """
    eventos = splits.get(simbolo)
    if not eventos or not filas:
        return filas
    out = [dict(f) for f in filas]
    for ev in eventos:
        if ev.factor <= 0:
            logger.error("Split %s %s con factor %s inválido — ignorado",
                         simbolo, ev.fecha, ev.factor)
            continue
        n = 0
        for f in out:
            if _a_date(f["fecha"]) < ev.fecha:
                for k in ("apertura", "maximo", "minimo", "cierre"):
                    if f.get(k):
                        f[k] = f[k] / ev.factor
                if f.get("volumen"):
                    f["volumen"] = f["volumen"] * ev.factor
                n += 1
        if n:
            logger.info("Split aplicado: %s %s factor %g → %d barras reescaladas (%s)",
                        simbolo, ev.fecha, ev.factor, n, ev.origen or "manual")
    return out


# ── detección desde el feed en vivo ─────────────────────────────────────────────

def _factor_limpio(ratio: float, tol: float = TOL_FACTOR) -> float | None:
    """Cociente → factor de split entero (o recíproco). None si no encaja.

    Los splits son razones enteras. Un cociente que no cae cerca de una es otra
    cosa (dato malo, dividendo extraordinario, caída real) y no se ajusta sola.
    """
    if ratio <= 0:
        return None
    if ratio >= 1.5:                      # split directo: 2:1, 10:1, ...
        n = round(ratio)
        if n >= 2 and abs(ratio / n - 1) <= tol:
            return float(n)
    elif ratio <= 1 / 1.5:                # reverse split: 1:2, 1:10, ...
        inv = 1 / ratio
        n = round(inv)
        if n >= 2 and abs(inv / n - 1) <= tol:
            return 1.0 / n
    return None


def detectar_split(simbolo: str, cot: dict, cierre_cache: float,
                   umbral: float = UMBRAL_DETECCION) -> Discrepancia | None:
    """Compara el cierre previo implícito del feed contra el último cierre del cache.

    `cot` es la cotización viva (`ultimoPrecio` + `variacion` en %). Devuelve None
    si el feed no alcanza para decidir o si la serie es coherente.
    """
    precio = cot.get("ultimoPrecio") or 0.0
    var    = cot.get("variacion")
    if precio <= 0 or cierre_cache <= 0 or var is None:
        return None
    den = 1 + var / 100.0
    if den <= 0:                      # variación ≤ −100%: imposible, dato basura
        return None
    cierre_feed = precio / den
    ratio       = cierre_cache / cierre_feed
    if abs(ratio - 1) < umbral:
        return None
    return Discrepancia(
        simbolo=simbolo, ratio=ratio,
        cierre_cache=cierre_cache, cierre_feed=cierre_feed,
        factor=_factor_limpio(ratio),
    )
