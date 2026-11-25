"""Indicadores de barras diarias y cache.

Sin dependencias de la estrategia (evita imports circulares). Provee:
  - build_daily_cache(client, ...) → baja OHLCV diario por símbolo a data/daily/<SIM>.csv
  - load_daily(...) → lee el cache a memoria
  - DailyIndicators → EMA diaria, ATR de Wilder, slope, breakout K-día, consultable por fecha
"""

from __future__ import annotations

import csv
import logging
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from argbot.strategy.corporate import aplicar_splits, cargar_splits
from argbot.brokers.iol.exceptions import IOLNoDataError

logger = logging.getLogger("argbot.strategy.daily")

CAMPOS = ["fecha", "apertura", "maximo", "minimo", "cierre", "volumen"]
DATA_DAILY = Path("data/daily")


# ── construcción del cache desde la API ─────────────────────────────────────────

def _fecha(b: dict) -> date:
    # fechaHora ISO con fracción de segundo de longitud variable; Python 3.10 no la
    # parsea con fromisoformat — basta la porción de fecha.
    return date.fromisoformat(b["fechaHora"][:10])


def _de_ajustada(bars: list) -> list[dict]:
    filas = []
    for b in bars:
        cierre = b.get("ultimoPrecio")
        ape    = b.get("apertura") or cierre
        # la subasta de cierre puede imprimir fuera del rango continuo → clamp consistente
        maximo = max(b.get("maximo") or cierre, ape, cierre)
        minimo = min(b.get("minimo") or cierre, ape, cierre)
        filas.append({
            "fecha":    _fecha(b).isoformat(),
            "apertura": ape, "maximo": maximo, "minimo": minimo,
            "cierre":   cierre, "volumen": b.get("volumenNominal") or 0,
        })
    return filas


def _de_sinajustar(ticks: list) -> list[dict]:
    """Agrega ticks intradía a barras diarias OHLCV (fallback si ajustada da 0 barras)."""
    from collections import OrderedDict
    por_dia: "OrderedDict[str, dict]" = OrderedDict()
    # Cada tick trae los campos del DÍA (apertura/maximo/minimo/volumenNominal
    # acumulados hasta ese momento). Para los días viejos IOL devuelve UN solo tick
    # con el resumen diario completo (1 tick/día salvo los últimos). Ignorarlos y usar solo `ultimoPrecio` colapsaba
    # la barra a O=H=L=C → ATR diario ≈ 0 → stop pegado a la entrada.
    for t in sorted(ticks, key=lambda x: x["fechaHora"]):
        p = t.get("ultimoPrecio")
        if not p:
            continue
        d = _fecha(t).isoformat()
        b = por_dia.get(d)
        hi = max(t.get("maximo") or p, p)
        lo = min(t.get("minimo") or p, p)
        vol = t.get("volumenNominal") or 0
        if b is None:
            por_dia[d] = {"fecha": d, "apertura": t.get("apertura") or p,
                          "maximo": hi, "minimo": lo, "cierre": p, "volumen": vol}
        else:
            b["maximo"] = max(b["maximo"], hi)
            b["minimo"] = min(b["minimo"], lo)
            b["cierre"] = p
            # volumenNominal es acumulado del día en el tick resumen y tamaño del tick
            # en los densos: el máximo es la mejor estimación del total diario
            b["volumen"] = max(b["volumen"], vol)
    return list(por_dia.values())


def build_daily_cache(client, universe: list[str], dias: int = 130,
                      out_dir: Path | None = None, mercado: str = "bCBA",
                      splits: dict | None = None) -> dict[str, int]:
    """Baja ~`dias` de barras diarias por símbolo y escribe data/daily/<SIM>.csv.

    Devuelve {simbolo: n_barras}. Errores por símbolo se loguean y no abortan el lote.

    IOL no ajusta la serie histórica por splits, así que se reescala acá con los
    eventos declarados en data/splits.json (`splits=None` los carga; `{}` los
    desactiva). Se aplica sobre el crudo recién bajado y antes de escribir el CSV
    → idempotente: cada arranque parte de la serie original, nunca de una ya
    ajustada. Ver argbot/strategy/corporate.py.
    """
    if splits is None:
        splits = cargar_splits()
    # resuelto en runtime (no en la firma) para que los tests puedan redirigirlo
    # y no pisar el cache real del bot
    out_dir = Path(out_dir) if out_dir is not None else DATA_DAILY
    out_dir.mkdir(parents=True, exist_ok=True)
    hasta = date.today()
    desde = hasta - timedelta(days=dias)
    resultado: dict[str, int] = {}
    for simbolo in universe:
        try:
            try:
                bars  = client.get_historico(mercado, simbolo, desde, hasta, ajustada="ajustada")
                filas = _de_ajustada(bars)
            except IOLNoDataError:
                ticks = client.get_historico(mercado, simbolo, desde, hasta, ajustada="sinAjustar")
                filas = _de_sinajustar(ticks)
        except Exception as e:  # noqa: BLE001
            logger.warning("Cache diario: fallo bajando %s: %s", simbolo, e)
            continue
        filas.sort(key=lambda f: f["fecha"])
        filas = aplicar_splits(simbolo, filas, splits)
        with open(out_dir / f"{simbolo}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS)
            w.writeheader()
            w.writerows(filas)
        resultado[simbolo] = len(filas)
    return resultado


# ── lectura del cache ───────────────────────────────────────────────────────────

def load_daily(data_daily: Path, universe: list[str]) -> dict[str, list[dict]]:
    """{simbolo: [{fecha(date), apertura, maximo, minimo, cierre, volumen}, ...]} ascendente."""
    out: dict[str, list[dict]] = {}
    for simbolo in universe:
        path = data_daily / f"{simbolo}.csv"
        if not path.exists():
            continue
        filas = []
        for r in csv.DictReader(open(path, encoding="utf-8")):
            filas.append({
                "fecha":    date.fromisoformat(r["fecha"]),
                "apertura": float(r["apertura"]), "maximo": float(r["maximo"]),
                "minimo":   float(r["minimo"]),   "cierre": float(r["cierre"]),
                "volumen":  float(r["volumen"] or 0),
            })
        filas.sort(key=lambda f: f["fecha"])
        out[simbolo] = filas
    return out


# ── indicadores ─────────────────────────────────────────────────────────────────

def _ema(vals: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr_wilder(hi: list[float], lo: list[float], cl: list[float], n: int) -> list[float]:
    tr = [hi[0] - lo[0]]
    for i in range(1, len(cl)):
        tr.append(max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
    out = [tr[0]]
    for i in range(1, len(tr)):
        out.append((out[-1] * (n - 1) + tr[i]) / n)
    return out


@dataclass
class Asof:
    ema_f: float
    ema_s: float
    slope: float
    atr:   float
    hh_K:  float   # máximo de los últimos K máximos diarios confirmados


class DailyIndicators:
    """Indicadores diarios precomputados por símbolo, consultables por fecha (barra < fecha)."""

    def __init__(self, daily: dict[str, list[dict]], n_fast: int = 10, n_slow: int = 30,
                 S: int = 3, K: int = 5, atr_n: int = 14):
        self.S, self.K = S, K
        self._fechas: dict[str, list[date]]  = {}
        self._ema_f:  dict[str, list[float]] = {}
        self._ema_s:  dict[str, list[float]] = {}
        self._atr:    dict[str, list[float]] = {}
        self._hi:     dict[str, list[float]] = {}
        self._cl:     dict[str, list[float]] = {}
        for simbolo, filas in daily.items():
            if len(filas) < max(n_slow, K, atr_n) + S + 1:
                continue
            cl = [f["cierre"] for f in filas]
            hi = [f["maximo"] for f in filas]
            lo = [f["minimo"] for f in filas]
            self._fechas[simbolo] = [f["fecha"] for f in filas]
            self._ema_f[simbolo]  = _ema(cl, n_fast)
            self._ema_s[simbolo]  = _ema(cl, n_slow)
            self._atr[simbolo]    = _atr_wilder(hi, lo, cl, atr_n)
            self._hi[simbolo]     = hi
            self._cl[simbolo]     = cl

    def cierres(self, simbolo: str, dia: date, n: int = 6) -> list[float] | None:
        """Últimos `n` cierres confirmados (fecha < dia), ascendentes. None sin datos."""
        fechas = self._fechas.get(simbolo)
        if not fechas:
            return None
        i = bisect_left(fechas, dia) - 1
        if i < 0:
            return None
        return self._cl[simbolo][max(0, i - n + 1): i + 1]

    def caida_desde_max(self, simbolo: str, dia: date, n: int = 10) -> float | None:
        """Retorno del último cierre confirmado contra el máximo de las últimas `n` ruedas.

        Negativo = el papel está por debajo de su pico reciente. Es la señal de
        reversión que usa la estrategia de ejemplo (una hipótesis a validar con
        tus propios datos, no un resultado garantizado). None si no hay `n`
        barras confirmadas.
        """
        fechas = self._fechas.get(simbolo)
        if not fechas:
            return None
        i = bisect_left(fechas, dia) - 1
        if i < n - 1:
            return None
        pico = max(self._hi[simbolo][i - n + 1 : i + 1])
        if pico <= 0:
            return None
        return self._cl[simbolo][i] / pico - 1

    def bear_consecutivos(self, simbolo: str, dia: date, max_n: int = 10) -> int | None:
        """Cuántas barras confirmadas seguidas (hacia atrás desde la última < dia) están en
        ↓BEAR diario (EMA rápida < EMA lenta — la misma condición del tag DIA). None sin datos."""
        fechas = self._fechas.get(simbolo)
        if not fechas:
            return None
        i = bisect_left(fechas, dia) - 1
        if i < self.S:
            return None
        n = 0
        while i - n >= 0 and n < max_n and self._ema_f[simbolo][i - n] < self._ema_s[simbolo][i - n]:
            n += 1
        return n

    def asof(self, simbolo: str, dia: date) -> Asof | None:
        """Indicadores con la última barra confirmada (fecha < dia)."""
        fechas = self._fechas.get(simbolo)
        if not fechas:
            return None
        i = bisect_left(fechas, dia) - 1
        if i < self.S or i < self.K:
            return None
        return Asof(
            ema_f = self._ema_f[simbolo][i],
            ema_s = self._ema_s[simbolo][i],
            slope = self._ema_s[simbolo][i] - self._ema_s[simbolo][i - self.S],
            atr   = self._atr[simbolo][i],
            hh_K  = max(self._hi[simbolo][i - self.K + 1 : i + 1]),
        )
