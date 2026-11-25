"""DailyIndicators.bear_consecutivos (etapa 1c)."""

from datetime import date, timedelta

from argbot.strategy.daily import DailyIndicators


def _filas(cierres, inicio=date(2026, 1, 5)):
    out, d = [], inicio
    for c in cierres:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"fecha": d, "cierre": c, "maximo": c * 1.01, "minimo": c * 0.99})
        d += timedelta(days=1)
    return out


def test_cuenta_barras_bear_hacia_atras():
    # 60 barras subiendo, después 5 cayendo fuerte → EMA10 < EMA30 en las últimas
    cierres = [100 + i for i in range(60)] + [150, 140, 130, 120, 110]
    filas = _filas(cierres)
    di = DailyIndicators({"X": filas})
    n = di.bear_consecutivos("X", filas[-1]["fecha"] + timedelta(days=1))
    assert 1 <= n <= 5


def test_bull_da_cero():
    cierres = [100 + i for i in range(60)]
    filas = _filas(cierres)
    di = DailyIndicators({"X": filas})
    assert di.bear_consecutivos("X", filas[-1]["fecha"] + timedelta(days=1)) == 0


def test_sin_datos_none():
    assert DailyIndicators({}).bear_consecutivos("X", date(2026, 3, 1)) is None
