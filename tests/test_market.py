"""argbot/exchange.py: BCBA price ticks and the holiday calendar."""

from datetime import date

from argbot.exchange import FERIADOS_BCBA, round_precio


def test_round_precio_por_rango():
    assert round_precio(23456.0) == 23455.0    # tick $5
    assert round_precio(1500.3) == 1500.0      # tick $1
    assert round_precio(150.30) == 150.50      # tick $0.50
    assert round_precio(47.97) == 47.95        # tick $0.05
    assert round_precio(5.123) == 5.12         # tick $0.01


def test_round_precio_limites_de_rango():
    assert round_precio(100.24) == 100.0       # justo arriba de 100 usa tick 0.50
    assert round_precio(99.97) == 99.95        # justo abajo usa tick 0.05


