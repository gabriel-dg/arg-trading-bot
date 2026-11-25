"""BCBA trading days for the operational path: the 2026 holidays plus later-announced closures.

`argbot/exchange.py` keeps the yearly holiday list, which the lab also reads; a closure the
market announces during the year is added here, where only the operational bot reads it.
"""
from datetime import date, timedelta

from argbot.exchange import FERIADOS_BCBA

# Extraordinary closures announced after the yearly holiday calendar was published.
EXTRAORDINARY_CLOSURES = frozenset({date(2026, 11, 9), date(2026, 11, 10)})
CLOSED = FERIADOS_BCBA | EXTRAORDINARY_CLOSURES
# Years whose holiday list was reviewed. Add next year's holidays before trading in it.
REVIEWED_YEARS = frozenset(d.year for d in FERIADOS_BCBA)


def is_trading_day(day):
    return day.weekday() < 5 and day not in CLOSED


def trading_day_before(day):
    day -= timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def trading_day_after(day):
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def trading_days_between(start, end):
    """Trading days in (start, end], as market.ruedas_entre counts them."""
    count = 0
    while start < end:
        start += timedelta(days=1)
        count += is_trading_day(start)
    return count
