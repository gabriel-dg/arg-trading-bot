"""Fallback sinAjustar del cache diario.

IOL devuelve, para símbolos sin serie `ajustada` (CEDEARs, QB7, QB8, QB9), un
tick por día con el resumen diario completo en apertura/maximo/minimo/volumenNominal.
El agregador los ignoraba y colapsaba la barra a O=H=L=C → ATR ≈ 0.
"""

from argbot.strategy.daily import _de_sinajustar


def _tick(fh, p, ape=None, hi=None, lo=None, vol=0):
    return {"fechaHora": fh, "ultimoPrecio": p, "apertura": ape, "maximo": hi, "minimo": lo,
            "volumenNominal": vol}


def test_tick_unico_conserva_el_rango_del_dia():
    b = _de_sinajustar([_tick("2026-01-05T16:58:10", 1000.0, ape=990.0, hi=1012.0, lo=985.0, vol=12000)])
    assert b == [{"fecha": "2026-01-05", "apertura": 990.0, "maximo": 1012.0, "minimo": 985.0,
                  "cierre": 1000.0, "volumen": 12000}]


def test_dia_denso_agrega_y_toma_el_volumen_maximo():
    ticks = [
        _tick("2026-01-06T11:00:00", 1100.0, ape=1100.0, hi=1100.0, lo=1100.0, vol=10),
        _tick("2026-01-06T13:00:00", 1120.0, ape=1100.0, hi=1125.0, lo=1090.0, vol=300),
        _tick("2026-01-06T16:59:50", 1105.0, ape=1100.0, hi=1125.0, lo=1090.0, vol=45000),
    ]
    b = _de_sinajustar(ticks)
    assert len(b) == 1
    assert b[0]["apertura"] == 1100.0 and b[0]["maximo"] == 1125.0 and b[0]["minimo"] == 1090.0
    assert b[0]["cierre"] == 1105.0 and b[0]["volumen"] == 45000


def test_sin_campos_del_dia_cae_al_precio():
    b = _de_sinajustar([_tick("2026-01-05T16:10:00", 100.0), _tick("2026-01-05T16:20:00", 101.0)])
    assert b[0]["apertura"] == 100.0 and b[0]["maximo"] == 101.0 and b[0]["minimo"] == 100.0


def test_ordena_por_fecha_y_separa_dias():
    b = _de_sinajustar([_tick("2026-01-06T17:00:00", 2.0, hi=2.5, lo=1.5),
                        _tick("2026-01-05T17:00:00", 1.0, hi=1.2, lo=0.9)])
    assert [x["fecha"] for x in b] == ["2026-01-05", "2026-01-06"]
