"""Paper trading: a deterministic synthetic exchange, a broker and a market on top of it.

Nothing here touches the network or a real account. Prices are generated from a
seed, so the same seed always gives the same session. The fill model is simple on
purpose: a limit BUY fills when the ask reaches its limit, a limit SELL when the bid
reaches its limit, always at the limit price, with a flat fee rate. Real markets
have partial fills, queues and slippage; treat paper results as a smoke test of the
mechanics, not as evidence that a strategy makes money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
import random
from zoneinfo import ZoneInfo

from argbot.execution import AccountView, BrokerRejected, OrderView
from argbot.market import daily_rows
from argbot.sessions import trading_day_before

ART = ZoneInfo("America/Argentina/Buenos_Aires")
CENT = Decimal("0.01")
PAPER_ACCOUNT = "PAPER:synthetic"


def _cents(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class _Symbol:
    previous_close: float
    price: float
    low: float
    high: float
    amount: float = 0.0
    trades: int = 0
    drift: float = 0.0
    history: list[dict] = field(default_factory=list)


@dataclass
class _Order:
    number: int
    symbol: str
    side: str
    quantity: int
    limit: Decimal
    submitted_at: datetime
    status: str = "open"
    filled: int = 0
    gross: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)


class SyntheticExchange:
    """A seeded random-walk market for one session, with an order book per symbol.

    About half of the symbols get a recent drawdown in their daily history and a
    weak morning, so the example strategy has setups to act on.
    """

    def __init__(self, symbols, *, session: date, seed: int = 7, cash="1000000",
                 fee_rate="0.006", spread=0.002, start=time(10, 30), history_days=60):
        self.session = session
        self.now = datetime.combine(session, start, ART)
        self.rng = random.Random(seed)
        self.fee_rate = Decimal(str(fee_rate))
        self.spread = spread
        self.cash = Decimal(str(cash))
        self.positions: dict[str, int] = {}
        self.orders: dict[int, _Order] = {}
        self.rejected_symbols: set[str] = set()
        self.symbols: dict[str, _Symbol] = {}
        for index, symbol in enumerate(symbols):
            self.symbols[symbol] = self._make_symbol(index, history_days)

    # ---- generation ---------------------------------------------------------
    def _make_symbol(self, index, history_days):
        rng = self.rng
        price = rng.choice([250.0, 800.0, 1500.0, 3200.0, 9000.0]) * rng.uniform(0.8, 1.2)
        setup = index % 2 == 0
        days, day = [], self.session
        for _ in range(history_days):
            day = trading_day_before(day)
            days.append(day)
        days.reverse()
        history = []
        for i, day in enumerate(days):
            late = i >= history_days - 8
            drift = -0.015 if (setup and late) else 0.001
            close = price * (1 + drift + rng.gauss(0, 0.012))
            high = max(price, close) * (1 + abs(rng.gauss(0, 0.006)))
            low = min(price, close) * (1 - abs(rng.gauss(0, 0.006)))
            history.append(dict(fechaHora=f"{day.isoformat()}T17:00:00", apertura=round(price, 2),
                                maximo=round(high, 2), minimo=round(low, 2), ultimoPrecio=round(close, 2),
                                volumenNominal=rng.randint(20_000, 400_000)))
            price = close
        close = history[-1]["ultimoPrecio"]
        # Setups open weak and drift down into the first entry window, then recover.
        drift = -0.0012 if setup else 0.0002
        return _Symbol(previous_close=close, price=close, low=close, high=close, drift=drift, history=history)

    def _step_prices(self):
        minutes = (self.now - datetime.combine(self.session, time(10, 30), ART)).total_seconds() / 60
        for s in self.symbols.values():
            drift = s.drift if minutes < 75 else abs(s.drift) * 0.6
            s.price = max(0.01, s.price * (1 + drift + self.rng.gauss(0, 0.0015)))
            s.low, s.high = min(s.low, s.price), max(s.high, s.price)
            volume = self.rng.randint(50, 600)
            s.amount += volume * s.price
            s.trades += self.rng.randint(2, 12)

    def _book(self, s: _Symbol):
        half = s.price * self.spread / 2
        return float(_cents(s.price - half)), float(_cents(s.price + half))

    # ---- time ---------------------------------------------------------------
    def clock(self) -> datetime:
        return self.now

    def advance(self, minutes: int = 5):
        self.now += timedelta(minutes=minutes)
        self._step_prices()
        self._match()

    def roll(self, next_session: date):
        """Close the session: append today's daily bar, expire day orders, open the next one."""
        bar_open = None
        for s in self.symbols.values():
            bar_open = s.previous_close
            s.history.append(dict(fechaHora=f"{self.session.isoformat()}T17:00:00", apertura=round(bar_open, 2),
                                  maximo=round(max(s.high, bar_open, s.price), 2),
                                  minimo=round(min(s.low, bar_open, s.price), 2),
                                  ultimoPrecio=round(s.price, 2), volumenNominal=max(int(s.amount / s.price), 1)))
            s.previous_close = s.low = s.high = s.price
            s.amount, s.trades = 0.0, 0
            s.drift = self.rng.choice([-0.0012, 0.0002, 0.0004])
        for order in self.orders.values():
            if order.status == "open":
                order.status = "cancelled"  # day orders expire at the close
        self.session = next_session
        self.now = datetime.combine(next_session, time(10, 30), ART)

    # ---- market data --------------------------------------------------------
    def quote_payload(self, symbol: str) -> dict:
        s = self.symbols[symbol]
        bid, ask = self._book(s)
        price = round(s.price, 2)
        return dict(simbolo=symbol, fechaHora=self.now.isoformat(), moneda="peso_Argentino", plazo="T1",
                    lote=1, laminaMinima=1, ultimoPrecio=price,
                    minimo=round(min(s.low, price), 2), maximo=round(max(s.high, price), 2),
                    montoOperado=round(max(s.amount, 1.0), 2), cantidadOperaciones=max(s.trades, 1),
                    puntas=[dict(precioCompra=bid, cantidadCompra=5000, precioVenta=ask, cantidadVenta=5000)])

    # ---- orders -------------------------------------------------------------
    def submit(self, symbol, side, quantity, limit, created_at) -> int:
        if symbol not in self.symbols or symbol in self.rejected_symbols:
            raise BrokerRejected("explicit_business_rejection", detail="paper: symbol not tradable")
        number = len(self.orders) + 1
        self.orders[number] = _Order(number, symbol, side, int(quantity), Decimal(str(limit)),
                                     max(created_at, self.now))
        return number

    def cancel(self, number: int):
        order = self.orders[number]
        if order.status == "open":
            order.status = "cancelled"

    def _match(self):
        for order in self.orders.values():
            if order.status != "open":
                continue
            bid, ask = self._book(self.symbols[order.symbol])
            limit = float(order.limit)
            if (order.side == "BUY" and ask <= limit) or (order.side == "SELL" and bid >= limit):
                qty = order.quantity - order.filled
                if order.side == "SELL" and self.positions.get(order.symbol, 0) < qty:
                    continue
                gross = qty * order.limit
                fees = _cents(gross * self.fee_rate)
                order.filled += qty
                order.gross += gross
                order.fees += fees
                order.status = "filled"
                sign = 1 if order.side == "BUY" else -1
                self.positions[order.symbol] = self.positions.get(order.symbol, 0) + sign * qty
                self.cash += -sign * gross - fees

    def held_cash(self) -> Decimal:
        return sum(((o.quantity - o.filled) * o.limit * (1 + self.fee_rate)
                    for o in self.orders.values() if o.side == "BUY" and o.status == "open"), Decimal(0))


class PaperBroker:
    """`Broker` over a SyntheticExchange. Fills and fees come from the exchange ledger."""

    def __init__(self, exchange: SyntheticExchange, account_id: str = PAPER_ACCOUNT):
        self.exchange, self.account_id = exchange, account_id

    def submit(self, intent: dict) -> str:
        created = datetime.fromisoformat(intent["created_at"])
        return str(self.exchange.submit(intent["symbol"], intent["side"], intent["quantity"],
                                        intent["limit"], created))

    def order(self, broker_id: str) -> OrderView:
        o = self.exchange.orders[int(broker_id)]
        return OrderView(broker_id=str(o.number), symbol=o.symbol, side=o.side,
                         requested_quantity=o.quantity, limit=str(o.limit),
                         submitted_at=o.submitted_at.isoformat(), status=o.status,
                         filled_quantity=o.filled, gross=str(o.gross), fees=str(o.fees),
                         account_id=self.account_id)

    def cancel(self, broker_id: str) -> None:
        self.exchange.cancel(int(broker_id))

    def account(self) -> AccountView:
        ex = self.exchange
        return AccountView(available_cash=str(ex.cash - ex.held_cash()),
                           positions={s: q for s, q in ex.positions.items() if q},
                           open_order_ids=frozenset(str(n) for n, o in ex.orders.items() if o.status == "open"),
                           account_id=self.account_id, cash_basis="paper")


class PaperMarket:
    """`MarketData` over a SyntheticExchange: canonical quotes and validated daily history."""

    def __init__(self, exchange: SyntheticExchange):
        self.exchange = exchange
        self.history_diagnostics = {}

    def set_priority(self, symbols) -> None:
        pass

    def quote(self, symbol: str) -> dict:
        return self.exchange.quote_payload(symbol)

    def history(self, symbols, session_date, previous_session):
        result, excluded = {}, {}
        for symbol in symbols:
            data = self.exchange.symbols.get(symbol)
            if data is None:
                excluded[symbol] = "unknown_symbol"
                continue
            self.history_diagnostics[symbol] = {}
            try:
                result[symbol] = daily_rows(data.history, session_date, previous_session,
                                            audit=self.history_diagnostics[symbol])
            except ValueError as exc:
                excluded[symbol] = str(exc)
        return result, excluded
