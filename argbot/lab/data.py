"""Carga estricta de snapshots y conversión explícita de ticks del proyecto original."""
import csv
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from argbot.lab.models import Quote, timestamp
from argbot.strategy.daily import DailyIndicators, load_daily
from argbot.strategy.vwap import update_vwap, value as vwap_value

ART = timezone(timedelta(hours=-3))
COLUMNS = list(Quote.__dataclass_fields__)

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def load_quotes(path: Path):
    frames = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(COLUMNS) <= set(reader.fieldnames or []):
            raise ValueError(f"Dataset necesita columnas {COLUMNS}")
        for line, row in enumerate(reader, 2):
            try:
                row["ts"] = timestamp(row["ts"]).astimezone(ART)
                for name in ("last", "bid", "ask", "low", "high", "vwap", "atr", "peak", "support"):
                    row[name] = float(row[name])
                for name in ("bid_size", "ask_size"):
                    row[name] = int(row[name])
                for name in ("bullish", "reliable"):
                    if row[name].lower() not in {"true", "false"}:
                        raise ValueError("Booleano debe ser true/false")
                    row[name] = row[name].lower() == "true"
                q = Quote(**row)
                snapshot = frames.setdefault(q.ts, {})
                if q.symbol in snapshot:
                    raise ValueError("Símbolo duplicado en snapshot")
                snapshot[q.symbol] = q
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
    if not frames:
        raise ValueError("Dataset vacío")
    return sorted(frames.items())

def write_quotes(path, quotes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for q in quotes:
            row = asdict(q)
            row["ts"] = q.ts.isoformat()
            writer.writerow(row)

def import_market(source: Path, output: Path, *, start: date, end: date,
                  spread_bps: float, symbols: list[str], groups=None):
    """Lee SOLO ticks y barras diarios; no copia logs, credenciales ni estado."""
    if start > end or not 0 < spread_bps <= 200 or not symbols:
        raise ValueError("Fechas, universo o supuesto de spread inválidos")
    if output.exists():
        raise ValueError("El destino ya existe; elegí un archivo nuevo")
    groups = groups or {}
    daily = load_daily(source / "daily", symbols)
    indicators = DailyIndicators(daily)
    snapshots = []
    rejected, hashes = 0, {}
    reasons, accepted_by_symbol = Counter(), Counter()
    for s in symbols:
        if not s.replace("_", "").isalnum():
            raise ValueError("Símbolo inválido")
        file = source / f"{s}_ticks.csv"
        if not file.exists():
            raise ValueError(f"Faltan ticks de {s}")
        hashes[str(file.name)] = digest(file)
        daily_file = source / "daily" / f"{s}.csv"
        if not daily_file.exists():
            raise ValueError(f"Faltan barras de {s}")
        hashes[f"daily/{s}.csv"] = digest(daily_file)
        vwap_state, day = {"cum_shares": 0.0, "last_monto": None}, None
        with file.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                ts = datetime.fromisoformat(row["timestamp"])
                ts = ts.replace(tzinfo=ART) if ts.tzinfo is None else ts.astimezone(ART)
                if not start <= ts.date() <= end:
                    continue
                a = indicators.asof(s, ts.date())
                bars = [b for b in daily.get(s, []) if b["fecha"] < ts.date()]
                if a is None or len(bars) < 10:
                    rejected += 1
                    reasons["insufficient_daily_history"] += 1
                    continue
                try:
                    p = float(row["ultimo_precio"])
                    amount = float(row.get("monto_operado") or 0)
                    if day != ts.date():
                        vwap_state, day = {"cum_shares": 0.0, "last_monto": None}, ts.date()
                    if p <= 0 or amount <= 0:
                        raise ValueError("Sin precio/volumen para VWAP")
                    counter_reset = update_vwap(vwap_state, p, amount)
                    if counter_reset:
                        reasons["counter_reset_reanchored"] += 1
                    prior_close = bars[-1]["cierre"]
                    # Feed/cache a escalas distintas: excluir, nunca inventar un ATR.
                    reliable = abs(p / prior_close - 1) < 0.35 and not counter_reset
                    half = spread_bps / 20000
                    snapshots.append(Quote(
                        ts=ts, symbol=s, last=p, bid=p*(1-half), ask=p*(1+half),
                        low=float(row["minimo"]), high=float(row["maximo"]),
                        vwap=vwap_value(vwap_state), atr=a.atr,
                        peak=max(b["maximo"] for b in bars[-10:]),
                        support=min(b["minimo"] for b in bars[-5:]),
                        bullish=a.ema_f > a.ema_s and a.slope > 0,
                        group=groups.get(s, "local"), reliable=reliable))
                    accepted_by_symbol[s] += 1
                except (ValueError, ZeroDivisionError) as exc:
                    rejected += 1
                    reasons[str(exc)] += 1
    if not snapshots:
        raise ValueError("No hay snapshots con datos suficientes")
    write_quotes(output, sorted(snapshots, key=lambda q: (q.ts, q.symbol)))
    metadata = {
        "kind": "historical_with_execution_assumptions", "symbols": symbols,
        "start": start.isoformat(), "end": end.isoformat(), "spread_bps": spread_bps,
        "rejected_rows": rejected, "quality_events": dict(reasons), "accepted_by_symbol": dict(accepted_by_symbol), "source_hashes": hashes, "csv_sha256": digest(output),
        "limitations": ["Puntas sintéticas: el CSV original no guarda bid/ask.",
                        "Tamaño de puntas supuesto abundante; sin cola de ejecución.",
                        "VWAP aproximado con deltas de monto; primera observación neutral.",
                        "Universo fijo elegido hoy: posible sesgo de supervivencia.",
                        "Cache diario actual: no conserva todas las revisiones históricas.",
                        "Datos para investigación; no demuestran fills ni retornos reales."]}
    output.with_suffix(".manifest.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return metadata
