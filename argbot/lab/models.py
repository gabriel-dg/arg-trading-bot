"""Datos observables y estado de cartera; los timestamps incluyen zona horaria."""
from dataclasses import dataclass
from datetime import datetime
import math

def timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Timestamp sin zona horaria")
    return dt

@dataclass(frozen=True)
class Quote:
    ts: datetime
    symbol: str
    last: float
    bid: float
    ask: float
    low: float
    high: float
    vwap: float
    atr: float
    peak: float
    support: float
    bullish: bool
    group: str = "local"
    bid_size: int = 1000000
    ask_size: int = 1000000
    reliable: bool = True

    def __post_init__(self):
        if self.ts.tzinfo is None or not self.symbol or not self.group:
            raise ValueError("Quote necesita zona horaria, símbolo y grupo")
        for key in ("last", "bid", "ask", "low", "high", "vwap", "atr", "peak", "support"):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{self.symbol}: {key} inválido")
        if self.bid > self.ask or self.low > self.high:
            raise ValueError("Puntas o rango invertidos")
        if any(type(n) is not int or n < 0 for n in (self.bid_size, self.ask_size)):
            raise ValueError("Tamaño de punta inválido")

    @property
    def spread(self):
        return (self.ask - self.bid) / self.last

    @property
    def range_fraction(self):
        return (self.last - self.low) / (self.high - self.low) if self.high > self.low else 0.5

@dataclass
class Position:
    symbol: str
    quantity: int
    entry: float
    cost: float
    opened: datetime
    peak: float
    stop: float
    catastrophe: float
    invalidation: float
    group: str
    confirmations: int = 0
    thesis_id: str | None = None

@dataclass
class Order:
    id: int
    symbol: str
    side: str
    quantity: int
    limit: float
    submitted: datetime
    reason: str
    stop: float = 0.0
    invalidation: float = 0.0
    group: str = "local"
    thesis_id: str | None = None
    filled: int = 0
    status: str = "pending"
    fee_rate: float = 0.0

    @property
    def remaining(self):
        return self.quantity - self.filled
