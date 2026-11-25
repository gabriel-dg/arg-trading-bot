"""Configuración explícita y versionable de cada experimento."""
from dataclasses import asdict, dataclass, fields
import json
import math
from pathlib import Path

@dataclass(frozen=True)
class Experiment:
    name: str = "reference"
    entry_mode: str = "baseline"
    entry_interval_min: int = 0
    max_positions: int = 5
    max_position_pct: float = 0.20
    drawdown_pct: float = 0.08
    min_drawdown_atr: float = 1.5
    max_spread_pct: float = 0.006
    commission: float = 0.003
    market_fee: float = 0.0005
    vat: float = 0.21
    stop_atr: float = 3.0
    trail_atr: float = 4.0
    grace_sessions: int = 6
    giveback_pct: float = 0.15
    lock_threshold: float = 0.06
    lock_fraction: float = 0.5
    stop_confirmations: int = 2
    emergency_first_touch: bool = False
    thesis_exit: bool = False
    invalidation_buffer_atr: float = 0.25
    risk_per_trade: float = 0.0
    portfolio_risk: float = 0.05
    group_risk: float = 0.025
    daily_loss_pct: float = 0.03
    include_unrealized_loss: bool = False
    passive_buy: bool = True
    adaptive_buy: bool = False
    buy_timeout_min: int = 30
    sell_timeout_min: int = 10
    cooldown_min: int = 1440
    regime_threshold: float = 0.40
    regime_factor: float = 0.50
    event_entries: bool = False
    event_exits: bool = False

    def __post_init__(self):
        if self.entry_mode not in {"baseline", "ranked"}:
            raise ValueError("entry_mode debe ser baseline o ranked")
        for key, value in asdict(self).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{key} debe ser finito y no negativo")
        for key in ("max_position_pct", "drawdown_pct", "max_spread_pct", "commission",
                    "market_fee", "giveback_pct", "lock_fraction", "risk_per_trade",
                    "portfolio_risk", "group_risk", "daily_loss_pct", "regime_threshold",
                    "regime_factor"):
            if getattr(self, key) > 1:
                raise ValueError(f"{key} debe estar entre 0 y 1")
        if not self.name or self.max_positions < 1 or self.stop_confirmations < 1:
            raise ValueError("Nombre, cupos y confirmaciones inválidos")
        if self.stop_atr <= 0 or self.trail_atr <= 0:
            raise ValueError("Los múltiplos ATR deben ser positivos")
        for key in ("entry_interval_min", "max_positions", "grace_sessions",
                    "stop_confirmations", "buy_timeout_min", "sell_timeout_min", "cooldown_min"):
            if type(getattr(self, key)) is not int:
                raise ValueError(f"{key} debe ser entero")

    @property
    def fee(self):
        return (self.commission + self.market_fee) * (1 + self.vat)

def load_config(path: Path) -> Experiment:
    raw = json.loads(path.read_text(encoding="utf-8"))
    unknown = set(raw) - {f.name for f in fields(Experiment)}
    if unknown:
        raise ValueError(f"Opciones desconocidas: {sorted(unknown)}")
    return Experiment(**raw)
