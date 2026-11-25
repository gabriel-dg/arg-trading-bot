"""Presupuesto de pérdida prevista, incluidos costos y órdenes de compra pendientes."""
import math
from argbot.lab.config import Experiment

def unit_risk(entry: float, stop: float, fee: float) -> float:
    return max(0.0, entry * (1 + fee) - max(0.0, stop) * (1 - fee))

def quantity(*, cfg: Experiment, entry: float, stop: float, cash: float,
             equity: float, allocated: float, fraction: float, regime: float,
             open_risk: float, group_risk: float) -> int:
    if entry <= 0 or stop <= 0 or stop >= entry or equity <= 0:
        return 0
    max_cash = min(cash, max(0.0, equity * cfg.max_position_pct * regime - allocated))
    max_cash *= fraction
    limit = max_cash / (entry * (1 + cfg.fee))
    if cfg.risk_per_trade:
        budget = min(equity * cfg.risk_per_trade * fraction * regime,
                     max(0.0, equity * cfg.portfolio_risk - open_risk),
                     max(0.0, equity * cfg.group_risk - group_risk))
        limit = min(limit, budget / unit_risk(entry, stop, cfg.fee))
    return max(0, math.floor(limit))
