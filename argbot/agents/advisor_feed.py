"""Pure local shadow capture: no IO, provider, or feedback into trading.

Called by the candidate only when the strategy config sets
"advisor": {"capture_enabled": true}. The batch stays in the local state file.
"""
from __future__ import annotations

from argbot.agents.advisor import prepare_request


def capture_batch(rules, quotes, shortlist, decisions, *, now, windows, occupied):
    rows = []
    excluded = []
    for symbol in shortlist:
        if symbol in occupied:
            continue
        cot = quotes.get(symbol) or {}
        try:
            price = float(cot["ultimoPrecio"])
            vwap = rules._vwap(symbol)
            drawdown = rules._daily.caida_desde_max(symbol, now.date(), rules.config.caida_lookback)
            span = rules._rango_dia_pct(cot, price)
            row = dict(source_at=cot["source_at"], drawdown_fraction=drawdown,
                       day_range_fraction=span / 100, price_to_vwap=price / vwap,
                       spread_fraction=(float(cot["ask"]) - float(cot["bid"])) / float(cot["bid"]))
            # Reuse strict feature/time validation, with no remote call.
            prepare_request([row], now=now)
            rows.append(dict(symbol=symbol, **row))
        except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError):
            excluded.append(symbol)
    return dict(schema=1, captured_at=now.isoformat(), window=windows[-1],
                coalesced_windows=windows[:-1], candidates=rows, excluded=excluded,
                mechanical_candidates=[d.simbolo for d in decisions],
                comparison="mechanical signal only; not evidence of submission or fill")


def mark(symbol, cot):
    """Reference prices for the local cycle audit only; never part of the batch."""
    return dict(symbol=symbol, source_at=cot["source_at"], received_at=cot["received_at"],
                validated_at=cot.get("validated_at", cot["received_at"]), bid=cot["bid"], ask=cot["ask"],
                last=str(cot["ultimoPrecio"]))
