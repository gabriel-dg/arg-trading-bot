"""Selección de candidatos sin acceso a ejecución ni capital."""
from dataclasses import dataclass
from argbot.lab.config import Experiment
from argbot.lab.models import Quote

@dataclass(frozen=True)
class Candidate:
    symbol: str
    score: float
    size_fraction: float
    reason: str
    event_id: str | None = None

def candidate(q: Quote, cfg: Experiment, event=None):
    if not q.reliable:
        return None, "unreliable_data"
    if q.spread > cfg.max_spread_pct:
        return None, "spread"
    if event is not None and cfg.event_entries and event["direction"] == "positive":
        # Evento abre una vía alternativa; nunca elimina liquidez, riesgo ni límite de precio.
        if q.last > q.vwap * 1.02:
            return None, "event_extended"
        return Candidate(q.symbol, 0.0, 0.5, "event", event["id"]), None
    drawdown = max(0.0, q.peak - q.last)
    if cfg.entry_mode == "baseline":
        if q.last / q.peak - 1 > -cfg.drawdown_pct:
            return None, "drawdown"
        if q.range_fraction > 0.5:
            return None, "day_range"
        if q.last > q.vwap:
            return None, "vwap"
        return Candidate(q.symbol, q.last / q.peak - 1, 1.0, "baseline"), None
    normalized = drawdown / q.atr
    if normalized < cfg.min_drawdown_atr:
        return None, "drawdown_atr"
    if q.range_fraction > 0.75 or q.last > q.vwap * 1.01:
        return None, "extended"
    # Liquidez y distancia bajo VWAP intervienen en el ranking; umbrales de hipótesis,
    # no estimaciones de probabilidad ni parámetros calibrados contra la muestra.
    score = -normalized + 100 * q.spread + abs(q.last / q.vwap - 1)
    size = 1.0 if normalized >= 2.0 and q.last <= q.vwap else 0.5
    return Candidate(q.symbol, score, size, "ranked"), None
