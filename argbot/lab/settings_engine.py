"""Extensión de operatoria: reutiliza el motor congelado sin alterar sus fuentes."""
from dataclasses import dataclass
from datetime import time, timedelta
import math
from zoneinfo import ZoneInfo

from argbot.lab.config import Experiment
from argbot.lab.engine import Engine
from argbot.lab.settings import OperatingLimits


@dataclass(frozen=True)
class _EffectiveConfig:
    base: Experiment
    limits: OperatingLimits

    def __getattr__(self, key):
        return getattr(self.base, key)

    @property
    def max_positions(self):
        # Sólo comparador interno; JSON persiste null, nunca Infinity ni un cupo ficticio.
        return math.inf if self.limits.max_positions is None else self.limits.max_positions

    @property
    def max_position_pct(self):
        return 1.0 if self.limits.max_position_pct is None else self.limits.max_position_pct / 100

    @property
    def daily_loss_pct(self):
        return math.inf if self.limits.daily_loss_pct is None else self.limits.daily_loss_pct / 100

    @property
    def include_unrealized_loss(self):
        return self.limits.include_unrealized_loss


class SettingsEngine(Engine):
    def __init__(self, config, capital=200000.0, events=None, reference=None, *, limits, schedule=None):
        self.limits = limits
        self.schedule = schedule
        super().__init__(_EffectiveConfig(config, limits), capital, events, reference)

    def step(self, now, quotes):
        # Configuración nueva declara ART aun cuando el input esté en UTC.
        if self.schedule is not None:
            now = now.astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
        return super().step(now, quotes)

    def entries(self, now, quotes):
        if not quotes and self.reference is None:
            self.record(now, "ENTRY_BLOCK", reason="no_snapshot_quotes")
            return
        for symbol, quote in quotes.items():
            if not quote.reliable:
                self.record(now, "REJECT", symbol=symbol, reason="unreliable_data")
        return super().entries(now, quotes)

    def record(self, now, event, **data):
        if data.get("reason") == "risk_or_capital":
            data["capital_context"] = {
                "equity": self.equity(), "cash": self.cash,
                "unreserved_cash": self.unreserved_cash(), "entry_cash": self.free_cash(),
                "max_position_pct": self.limits.max_position_pct,
                "max_invested_pct": self.limits.max_invested_pct,
                "risk_budget_enabled": bool(self.cfg.risk_per_trade)}
        return super().record(now, event, **data)

    def unreserved_cash(self):
        return super().free_cash()

    def free_cash(self):
        """Fondos habilitados para dimensionar entradas, después de ambos topes."""
        free = self.unreserved_cash()
        if self.limits.max_invested_pct is None:
            return free
        equity = self.equity()
        held = equity - self.cash
        reserved = self.cash - free
        room = equity * self.limits.max_invested_pct / 100 - held - reserved
        return min(free, max(0.0, room))

    def due(self, now):
        # Engine.step consulta due en TODOS los cortes abiertos, después de fills,
        # salidas y pérdidas, antes de nuevas entradas y de guardar la curva.
        # Sólo esta extensión incluye igualdad; el motor histórico conserva '<'.
        daily = self.limits.daily_loss_pct
        loss = self.equity() - self.day_equity if self.limits.include_unrealized_loss else self.day_realized
        if daily is not None and loss <= -daily / 100 * self.day_equity:
            self.day_blocked = True
            for order in self.broker.pending():
                if order.side == "BUY":
                    self.broker.cancel(order)
                    self.record(now, "CANCEL", order_id=order.id, reason="daily_loss")
        if self.schedule is None:
            return super().due(now)
        schedule = self.schedule
        local = now.time()
        if local >= time.fromisoformat(schedule.entry_cutoff):
            return False
        if schedule.mode == "interval":
            if local < time.fromisoformat(schedule.interval_start):
                return False
            if self.last_entry_check is None or now - self.last_entry_check >= timedelta(minutes=schedule.interval_minutes):
                self.last_entry_check = now
                return True
            return False
        due = [t for t in schedule.entry_times if local >= time.fromisoformat(t) and t not in self.windows_done]
        self.windows_done.update(due)
        return bool(due)
