"""The broker boundary every adapter implements.

A broker adapter (an ALyC such as IOL, or the built-in paper broker) provides two
objects:

- a `Broker` for orders and the account, returning normalized `OrderView` and
  `AccountView` values with CONFIRMED fills and fees, never inferred ones;
- a `MarketData` source for quotes and daily history.

Quotes use one canonical payload shape (the field names follow BYMA/IOL usage):
`simbolo`, `fechaHora` (ISO 8601 with offset), `moneda` ("peso_Argentino"),
`plazo` ("T1"), `lote`, `laminaMinima`, `ultimoPrecio`, `minimo`, `maximo`,
`montoOperado`, optional `cantidadOperaciones`, and `puntas`, a list of
`{precioCompra, cantidadCompra, precioVenta, cantidadVenta}`. An adapter for
another broker maps its own feed to this shape; `argbot.market.quote` validates it.

Daily history is returned already validated by `argbot.market.daily_rows`:
`(rows_by_symbol, excluded_by_symbol)`.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

from argbot.execution import AccountView, Broker, BrokerRejected, OrderView, RequestNotSent

__all__ = ["AccountView", "Broker", "BrokerRejected", "MarketData", "OrderView", "RequestNotSent"]


class MarketData(Protocol):
    def quote(self, symbol: str) -> dict: ...

    def history(self, symbols: list[str], session_date: date,
                previous_session: date) -> tuple[dict[str, list[dict]], dict[str, str]]: ...

    def set_priority(self, symbols) -> None: ...
