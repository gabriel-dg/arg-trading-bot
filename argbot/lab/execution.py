"""Broker de papel: solo ejecuta contra una observación POSTERIOR a la orden.

Las puntas y sus tamaños son observados o supuestos explícitos del dataset.
No se infiere que tocar un mínimo diario garantice llenar una compra.
"""
from dataclasses import asdict
from argbot.lab.models import Order, Quote

class PaperBroker:
    def __init__(self):
        self.orders: list[Order] = []
        self.fills: list[dict] = []

    def submit(self, **kwargs):
        order = Order(id=len(self.orders) + 1, **kwargs)
        self.orders.append(order)
        return order

    def pending(self, symbol=None):
        return [o for o in self.orders if o.status == "pending"
                and (symbol is None or o.symbol == symbol)]

    def cancel(self, order):
        if order.status == "pending":
            order.status = "cancelled"

    def advance(self, quotes: dict[str, Quote]):
        fills = []
        capacity = {(s, side): q.ask_size if side == "BUY" else q.bid_size
                    for s, q in quotes.items() for side in ("BUY", "SELL")}
        for order in self.pending():
            q = quotes.get(order.symbol)
            if q is None or not q.reliable or q.ts <= order.submitted:
                continue
            if q.ts.date() != order.submitted.date():
                self.cancel(order)  # órdenes por el día
                continue
            price = q.ask if order.side == "BUY" else q.bid
            marketable = price <= order.limit if order.side == "BUY" else price >= order.limit
            available = capacity[(order.symbol, order.side)]
            if not marketable or available <= 0:
                continue
            quantity = min(order.remaining, available)
            capacity[(order.symbol, order.side)] -= quantity
            order.filled += quantity
            if not order.remaining:
                order.status = "filled"
            fill = {"order_id": order.id, "symbol": order.symbol, "side": order.side,
                    "quantity": quantity, "price": price, "ts": q.ts.isoformat(),
                    "fee": quantity * price * order.fee_rate, "reason": order.reason}
            self.fills.append(fill)
            fills.append((order, fill))
        return fills

    def records(self):
        return [asdict(o) for o in self.orders]
