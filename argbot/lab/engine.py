"""Un motor de cartera para todos los perfiles, con un único broker de papel."""
from dataclasses import asdict
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import math
from argbot.lab.config import Experiment
from argbot.lab.events import EventBook
from argbot.lab.execution import PaperBroker
from argbot.lab.models import Position, Quote
from argbot.lab.risk import quantity, unit_risk
from argbot.lab.strategy import candidate
from argbot.exchange import ruedas_entre, FERIADOS_BCBA

class Engine:
    def __init__(self, config: Experiment, capital=200000.0, events=None, reference=None):
        if not math.isfinite(capital) or capital <= 0:
            raise ValueError("Capital inicial inválido")
        self.cfg, self.initial, self.cash = config, capital, capital
        self.reference = reference
        self.broker = PaperBroker()
        self.events = events or EventBook()
        self.positions: dict[str, Position] = {}
        self.last_quotes: dict[str, Quote] = {}
        self.audit: list[dict] = []
        self.curve: list[dict] = []
        self.trades: list[dict] = []
        self.cooldowns: dict[str, datetime] = {}
        self.completed_event_ids = set()
        self.day = None
        self.day_equity = capital
        self.day_realized = 0.0
        self.realized = 0.0
        self.day_blocked = False
        self.windows_done = set()
        self.last_entry_check = None
        self.last_ts = None
        self.roundtrip_pnl = {}

    def sessions_between(self, start, end, now):
        if self.reference is None:
            return ruedas_entre(start, end)
        count, day = 0, start
        while day < end:
            day += timedelta(days=1)
            count += self.reference.hours(day, now) is not None
        return count

    def record(self, now, event, **data):
        self.audit.append({"ts": now.isoformat(), "event": event, **data})

    def equity(self):
        return self.cash + sum(p.quantity * self.last_quotes[s].bid * (1 - self.cfg.fee)
                               for s, p in self.positions.items())

    def free_cash(self):
        reserved = sum(o.remaining * o.limit * (1 + o.fee_rate)
                       for o in self.broker.pending() if o.side == "BUY")
        return max(0.0, self.cash - reserved)

    def pending_risk(self, group=None):
        held = sum(p.quantity * unit_risk(self.last_quotes[s].bid, max(p.stop, p.invalidation) if self.cfg.thesis_exit else p.stop, self.cfg.fee)
                   for s, p in self.positions.items() if group is None or p.group == group)
        pending = sum(o.remaining * unit_risk(o.limit, max(o.stop, o.invalidation) if self.cfg.thesis_exit else o.stop, self.cfg.fee)
                      for o in self.broker.pending()
                      if o.side == "BUY" and (group is None or o.group == group))
        return held + pending

    def apply_fill(self, order, fill):
        s, n, price, fee = fill["symbol"], fill["quantity"], fill["price"], fill["fee"]
        now = datetime.fromisoformat(fill["ts"])
        if order.side == "BUY":
            cost = n * price + fee
            if cost > self.cash + 0.0001:
                raise RuntimeError("Broker produjo un fill sin fondos")
            self.cash -= cost
            p = self.positions.get(s)
            if p:
                p.entry = (p.entry * p.quantity + n * price) / (p.quantity + n)
                p.quantity += n
                p.cost += cost
            else:
                self.positions[s] = Position(s, n, price, cost, now, price, order.stop,
                                             order.stop, order.invalidation, order.group,
                                             thesis_id=order.thesis_id)
                self.roundtrip_pnl[s] = 0.0
                if order.thesis_id:
                    self.completed_event_ids.add(order.thesis_id)
        else:
            p = self.positions[s]
            if n > p.quantity:
                raise RuntimeError("Venta mayor a la posición")
            cost = p.cost * n / p.quantity
            proceeds = n * price - fee
            pnl = proceeds - cost
            self.cash += proceeds
            self.realized += pnl
            self.day_realized += pnl
            self.roundtrip_pnl[s] += pnl
            p.quantity -= n
            p.cost -= cost
            if not p.quantity:
                total = self.roundtrip_pnl.pop(s)
                self.trades.append({"symbol": s, "opened": p.opened.isoformat(),
                                    "closed": now.isoformat(), "pnl": total,
                                    "reason": order.reason,
                                    "sessions": self.sessions_between(p.opened.date(), now.date(), now)})
                if total < 0:
                    self.cooldowns[s] = now + timedelta(minutes=self.cfg.cooldown_min)
                del self.positions[s]
        self.record(now, "FILL", **{k: v for k, v in fill.items() if k != "ts"})

    def submit_sell(self, s, q, now, reason):
        # Cancelar el remanente BUY evita comprar de nuevo después de decidir salir.
        for order in self.broker.pending(s):
            if order.side == "BUY":
                self.broker.cancel(order)
                self.record(now, "CANCEL", order_id=order.id, reason="exit_pending_buy")
        if any(o.side == "SELL" for o in self.broker.pending(s)):
            return
        p = self.positions[s]
        fee = self.cfg.market_fee * (1 + self.cfg.vat) if p.opened.date() == now.date() else self.cfg.fee
        self.broker.submit(symbol=s, side="SELL", quantity=p.quantity, limit=q.bid,
                           submitted=now, reason=reason, fee_rate=fee)
        self.record(now, "SELL_INTENT", symbol=s, reason=reason, limit=q.bid)

    def due(self, now):
        if self.cfg.entry_interval_min:
            if now.time() < time(10, 45):
                return False
            if self.last_entry_check is None or now - self.last_entry_check >= timedelta(minutes=self.cfg.entry_interval_min):
                self.last_entry_check = now
                return True
            return False
        due = [w for w in (time(11, 15), time(13, 30), time(16, 30))
               if now.time() >= w and w not in self.windows_done]
        self.windows_done.update(due)
        return bool(due)

    def step(self, now, quotes):
        if self.reference is not None:
            now = now.astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
        if self.last_ts is not None and now <= self.last_ts:
            raise ValueError("El replay debe avanzar estrictamente en el tiempo")
        if any(q.ts != now or s != q.symbol for s, q in quotes.items()):
            raise ValueError("Snapshot inconsistente")
        self.last_ts = now
        previous_equity = self.equity()
        self.last_quotes.update({s: q for s, q in quotes.items() if q.reliable})
        market_open = self.reference.is_open(now) if self.reference else (
            now.weekday() < 5 and now.date() not in FERIADOS_BCBA and time(10, 30) <= now.time() <= time(17))
        if not market_open:
            self.record(now, "MARKET_SKIP", reason="outside_session")
            return
        if now.date() != self.day:
            for order in self.broker.pending():
                self.broker.cancel(order)
                self.record(now, "CANCEL", order_id=order.id, reason="day_expiry")
            self.day = now.date()
            self.day_equity = previous_equity
            self.day_realized = 0.0
            self.day_blocked = False
            self.windows_done = set()
            self.last_entry_check = None
        for order, fill in self.broker.advance(quotes):
            self.apply_fill(order, fill)

        for order in self.broker.pending():
            timeout = self.cfg.buy_timeout_min if order.side == "BUY" else self.cfg.sell_timeout_min
            if now - order.submitted >= timedelta(minutes=timeout):
                self.broker.cancel(order)
                self.record(now, "CANCEL", order_id=order.id, reason="timeout")
                q = quotes.get(order.symbol)
                if order.side == "SELL" and order.symbol in self.positions and q and q.reliable:
                    self.submit_sell(order.symbol, q, now, order.reason)

        for s, p in list(self.positions.items()):
            q = quotes.get(s)
            if q is None or not q.reliable:
                continue
            if any(o.side == "SELL" for o in self.broker.pending(s)):
                continue
            p.peak = max(p.peak, q.last)
            phase1 = self.sessions_between(p.opened.date(), now.date(), now) < self.cfg.grace_sessions
            if not phase1:
                p.stop = max(p.stop, p.peak - self.cfg.trail_atr * q.atr,
                             p.peak * (1 - self.cfg.giveback_pct))
                if p.peak >= p.entry * (1 + self.cfg.lock_threshold):
                    p.stop = max(p.stop, p.entry + self.cfg.lock_fraction * (p.peak - p.entry))
            event = self.events.latest(s, now)
            reason = None
            if self.cfg.emergency_first_touch and q.last < p.catastrophe:
                reason = "catastrophic_stop"
            elif self.cfg.thesis_exit and q.last < p.invalidation:
                reason = "price_invalidation"
            elif (self.cfg.event_exits and event and event["direction"] == "negative"
                  and datetime.fromisoformat(event["analyzed_at"]) >= p.opened):
                reason = "event_invalidation"
            elif q.last < p.stop:
                p.confirmations += 1
                if p.confirmations >= self.cfg.stop_confirmations:
                    reason = "trailing_stop"
            else:
                p.confirmations = 0
            if reason:
                self.submit_sell(s, q, now, reason)

        equity = self.equity()
        loss = equity - self.day_equity if self.cfg.include_unrealized_loss else self.day_realized
        if loss < -self.cfg.daily_loss_pct * self.day_equity:
            self.day_blocked = True
            # Un límite de nuevas entradas también cancela compras que aún trabajan.
            for order in self.broker.pending():
                if order.side == "BUY":
                    self.broker.cancel(order)
                    self.record(now, "CANCEL", order_id=order.id, reason="daily_loss")
        if self.due(now) and now.time() < time(16, 40):
            self.entries(now, quotes)
        else:
            self.record(now, "ENTRY_SKIP", reason="entry_schedule")
        exposure = sum(p.quantity * self.last_quotes[s].bid for s, p in self.positions.items())
        self.curve.append({"ts": now.isoformat(), "equity": self.equity(), "cash": self.cash,
                           "exposure": exposure, "positions": len(self.positions),
                           "pending": len(self.broker.pending()),
                           "stale_marks": sum(s not in quotes or not quotes[s].reliable for s in self.positions)})

    def entries(self, now, quotes):
        assets = self.reference.universe(now.date(), now) if self.reference else None
        if assets is not None:
            for symbol in sorted(set(assets) - set(quotes)):
                self.record(now, "REJECT", symbol=symbol, reason="missing_quote")
        if self.day_blocked:
            self.record(now, "ENTRY_BLOCK", reason="daily_loss")
            return
        # No dimensionar una cartera cuyo riesgo no se puede observar en este snapshot.
        if any(s not in quotes or not quotes[s].reliable for s in self.positions):
            self.record(now, "ENTRY_BLOCK", reason="missing_position_quote")
            return
        local = [q for q in quotes.values() if q.reliable and
                 (assets.get(q.symbol, {}).get("kind") == "local" if assets is not None else q.group != "cedear")]
        breadth = sum(q.bullish for q in local) / len(local) if local else None
        regime = self.cfg.regime_factor if breadth is not None and breadth < self.cfg.regime_threshold else 1.0
        candidates = []
        for s, q in quotes.items():
            if assets is not None and s not in assets:
                self.record(now, "REJECT", symbol=s, reason="outside_universe")
                continue
            if assets is not None and q.group != assets[s]["group"]:
                self.record(now, "REJECT", symbol=s, reason="classification_mismatch")
                continue
            if s in self.positions or self.broker.pending(s):
                self.record(now, "ENTRY_SKIP", symbol=s, reason="held_or_pending")
                continue
            if self.cooldowns.get(s, now) > now:
                self.record(now, "REJECT", symbol=s, reason="cooldown")
                continue
            event = self.events.latest(s, now)
            if event and event["id"] in self.completed_event_ids:
                event = None
            c, reject = candidate(q, self.cfg, event)
            if c:
                candidates.append(c)
            else:
                self.record(now, "REJECT", symbol=s, reason=reject)
        for c in sorted(candidates, key=lambda c: (c.score, c.symbol)):
            occupied = set(self.positions) | {o.symbol for o in self.broker.pending() if o.side == "BUY"}
            if len(occupied) >= self.cfg.max_positions:
                self.record(now, "REJECT", symbol=c.symbol, reason="slots")
                continue
            q = quotes[c.symbol]
            aggressive = self.cfg.adaptive_buy and q.spread <= 0.002
            entry = q.ask if aggressive or not self.cfg.passive_buy else q.bid
            stop = entry - self.cfg.stop_atr * q.atr
            invalidation = min(entry - 0.5 * q.atr, q.support - self.cfg.invalidation_buffer_atr * q.atr)
            risk_stop = max(stop, invalidation) if self.cfg.thesis_exit else stop
            n = quantity(cfg=self.cfg, entry=entry, stop=risk_stop, cash=self.free_cash(),
                         equity=self.equity(), allocated=0.0, fraction=c.size_fraction, regime=regime,
                         open_risk=self.pending_risk(), group_risk=self.pending_risk(q.group))
            if n == 0:
                self.record(now, "REJECT", symbol=c.symbol, reason="risk_or_capital")
                continue
            order = self.broker.submit(symbol=c.symbol, side="BUY", quantity=n, limit=entry,
                                       submitted=now, reason=c.reason, stop=stop,
                                       invalidation=invalidation, group=q.group, thesis_id=c.event_id,
                                       fee_rate=self.cfg.fee)
            self.record(now, "BUY_INTENT", symbol=c.symbol, order_id=order.id, quantity=n,
                        limit=entry, catastrophe=stop, invalidation=invalidation, reason=c.reason)

    def summary(self):
        eq = self.equity()
        peak, max_drawdown = self.initial, 0.0
        for row in self.curve:
            peak = max(peak, row["equity"])
            max_drawdown = max(max_drawdown, 1 - row["equity"] / peak)
        wins = [t["pnl"] for t in self.trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in self.trades if t["pnl"] < 0]
        buys = [o for o in self.broker.orders if o.side == "BUY"]
        return {
            "profile": self.cfg.name, "capital_initial": self.initial, "equity_final": eq,
            "return_pct": (eq / self.initial - 1) * 100, "realized_pnl": self.realized,
            "unrealized_pnl": eq - self.initial - self.realized,
            "max_drawdown_pct": max_drawdown * 100, "closed_trades": len(self.trades),
            "win_rate_pct": len(wins) / len(self.trades) * 100 if self.trades else None,
            "average_win": sum(wins) / len(wins) if wins else None,
            "average_loss": sum(losses) / len(losses) if losses else None,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
            "fees_paid": sum(f["fee"] for f in self.broker.fills),
            "buy_orders": len(buys),
            "buy_order_fill_rate": sum(o.filled > 0 for o in buys) / len(buys) if buys else None,
            "average_exposure_pct": sum(r["exposure"] / r["equity"] for r in self.curve if r["equity"] > 0)
                                    / max(1, len(self.curve)) * 100,
            "open_positions": {s: asdict(p) for s, p in self.positions.items()},
            "pending_orders": len(self.broker.pending()),
            "stale_mark_polls": sum(r["stale_marks"] > 0 for r in self.curve),
            "note": "Replay de investigación; ejecución por puntas posteriores, sin garantía de fills reales.",
        }
