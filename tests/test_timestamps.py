"""Broker timestamps parse the same on Python 3.10 and later (synthetic values)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from argbot.brokers.iol.readers import normalize_order, order_instant
from argbot.execution import instant, parse_timestamp

ART = ZoneInfo("America/Argentina/Buenos_Aires")


@pytest.mark.parametrize("fraction,micro", [
    ("1", 100000), ("12", 120000), ("123", 123000), ("1234", 123400), ("12345", 123450),
    ("123456", 123456), ("1234567", 123456)])
def test_any_number_of_fractional_digits(fraction, micro):
    parsed = parse_timestamp(f"2026-01-05T11:15:02.{fraction}-03:00")
    assert parsed.microsecond == micro and parsed.utcoffset() == timedelta(hours=-3)


def test_trailing_z_and_plain_values():
    assert parse_timestamp("2026-01-05T14:15:02.5Z") == datetime(2026, 1, 5, 14, 15, 2, 500000, tzinfo=timezone.utc)
    assert parse_timestamp("2026-01-05T11:15:02") == datetime(2026, 1, 5, 11, 15, 2)
    assert instant("2026-01-05T11:15:02.1234567-03:00").microsecond == 123456
    with pytest.raises(ValueError):
        parse_timestamp(None)
    with pytest.raises(ValueError):
        instant("2026-01-05T11:15:02.12345")  # naive is still refused by instant()


def test_order_detail_with_odd_fraction_digits_is_read():
    detail = dict(numero=7, mercado="bCBA", simbolo="QA0", moneda="peso_Argentino", tipo="compra",
                  plazo="a24horas", modalidad="precio_Limite", fechaAlta="2026-01-05T11:15:00.1234567",
                  estadoActual="terminada", cantidad=2, precio=100,
                  operaciones=[dict(fecha="2026-01-05T11:16:00.12", cantidad=2, precio=99)],
                  arancelesARS=1.98, arancelesUSD=0)
    view = normalize_order(detail, "account", naive_timezone="America/Argentina/Buenos_Aires")
    assert view.status == "filled" and view.filled_quantity == 2 and Decimal(view.gross) == 198
    assert order_instant("2026-01-05T11:15:00.1234567", "America/Argentina/Buenos_Aires") == \
        datetime(2026, 1, 5, 11, 15, 0, 123456, tzinfo=ART)
