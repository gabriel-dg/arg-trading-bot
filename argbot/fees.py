"""Observed fee rates from the local order ledger. No broker or network IO."""
from __future__ import annotations

from decimal import Decimal

from argbot.execution import TERMINAL


def money(value) -> Decimal:
    return Decimal(str(value))


def fee_estimates(state: dict, reserve) -> dict[str, dict[str, str]]:
    """Confirmed historic charges are indicative, not a promised future tariff."""
    result = {}
    for side in ("BUY", "SELL"):
        orders = [
            o for o in state["orders"].values()
            if o["side"] == side and o["state"] in TERMINAL and o["filled"] and money(o["gross"]) > 0
        ]
        gross = sum((money(o["gross"]) for o in orders), Decimal(0))
        rate = sum((money(o["fees"]) for o in orders), Decimal(0)) / gross if gross else money(reserve)
        if not 0 <= rate < 1:
            raise ValueError("Invalid observed fee rate")
        result[side] = {
            "rate": str(rate),
            "basis": "confirmed_historical_charges" if gross else "configured_reserve_estimate",
        }
    return result
