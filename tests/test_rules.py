"""Pure rule engine of the example strategy: entries, sizing, stops."""
from datetime import date, datetime, time
from types import SimpleNamespace

import pytest

from argbot.strategy.rules import Decision, RuleEngine, StrategyConfig


class Daily:
    def __init__(self, bars=None, drawdowns=None):
        self.bars, self.drawdowns = bars or {}, drawdowns or {}

    def __bool__(self):
        return True

    def asof(self, symbol, day):
        return self.bars.get(symbol)

    def caida_desde_max(self, symbol, day, n):
        return self.drawdowns.get(symbol)


class Engine(RuleEngine):
    def __init__(self, config, daily=None, vwap=None, positions=None, phase1=False):
        self.config, self._daily, self._daily_sospechoso = config, daily, set()
        self._vwaps, self._phase1 = vwap or {}, phase1
        self._st = {"positions": positions or {}, "capital_dia_total": 100000.0}

    def _vwap(self, symbol):
        return self._vwaps.get(symbol)

    def _en_fase1_stop(self, pos, now):
        return self._phase1


NOW = datetime(2026, 10, 14, 11, 30)
BULL = SimpleNamespace(ema_f=2, ema_s=1, slope=1, atr=2.0)
BEAR = SimpleNamespace(ema_f=1, ema_s=2, slope=-1, atr=2.0)


def cfg(**kw):
    base = dict(universe=["AAA", "BBB", "CCC"], caida_umbral=-0.08, max_concurrent=2,
                max_capital_pct=0.2, hora_no_entrada=time(16), entry_windows=[time(11), time(14)])
    base.update(kw)
    return StrategyConfig(**base)


def quote(price, low=95.0, high=105.0):
    return {"ultimoPrecio": price, "minimo": low, "maximo": high}


def test_only_drawdowns_beyond_the_threshold_qualify_ranked_deepest_first():
    daily = Daily({s: BULL for s in "AAA BBB CCC".split()}, {"AAA": -0.09, "BBB": -0.20, "CCC": -0.02})
    e = Engine(cfg(), daily, vwap={s: 100.0 for s in ["AAA", "BBB", "CCC"]})
    quotes = {s: quote(97) for s in ["AAA", "BBB", "CCC"]}
    assert e._simbolos_a_evaluar(quotes, NOW) == ["BBB", "AAA"]


def test_entry_needs_lower_day_range_and_price_at_or_under_vwap():
    daily = Daily({s: BULL for s in ["AAA", "BBB"]}, {"AAA": -0.1, "BBB": -0.1})
    e = Engine(cfg(universe=["AAA", "BBB"], vwap_filtro_pct=0.0), daily, vwap={"AAA": 100.0, "BBB": 100.0})
    decisions = e._decisiones_mecanicas({"AAA": quote(97), "BBB": quote(104)}, NOW)
    assert [d.simbolo for d in decisions] == ["AAA"]
    assert isinstance(decisions[0], Decision) and decisions[0].accion == "BUY"
    assert decisions[0].monto_ars == 20000  # 20% of the day's capital, regime breadth is healthy


def test_weak_breadth_shrinks_new_positions_but_never_vetoes_them():
    daily = Daily({"AAA": BEAR, "BBB": BEAR}, {"AAA": -0.1, "BBB": -0.1})
    e = Engine(cfg(universe=["AAA", "BBB"], regimen_umbral=0.4, regimen_factor=0.5), daily,
               vwap={"AAA": 100.0, "BBB": 100.0})
    decisions = e._decisiones_mecanicas({"AAA": quote(97), "BBB": quote(97)}, NOW)
    assert [d.monto_ars for d in decisions] == [10000, 10000]


def test_free_slots_bound_the_number_of_buys():
    daily = Daily({s: BULL for s in ["AAA", "BBB", "CCC"]}, {s: -0.1 for s in ["AAA", "BBB", "CCC"]})
    positions = {"AAA": {"en_posicion": True}}
    e = Engine(cfg(), daily, vwap={s: 100.0 for s in ["AAA", "BBB", "CCC"]}, positions=positions)
    decisions = e._decisiones_mecanicas({s: quote(97) for s in ["AAA", "BBB", "CCC"]}, NOW)
    assert len(decisions) == 1 and decisions[0].simbolo != "AAA"


def test_no_entries_after_the_cutoff():
    daily = Daily({"AAA": BULL}, {"AAA": -0.1})
    e = Engine(cfg(universe=["AAA"]), daily, vwap={"AAA": 100.0})
    late = datetime(2026, 10, 14, 16, 5)
    assert e._decisiones_mecanicas({"AAA": quote(97)}, late) == []


@pytest.mark.parametrize("price,expected", [(90, 0.0), (100, 50.0), (110, 100.0)])
def test_day_range_position(price, expected):
    assert RuleEngine._rango_dia_pct({"minimo": 90, "maximo": 110}, price) == expected


def test_stop_is_the_highest_of_protective_chandelier_and_lock_and_never_goes_down():
    daily = Daily({"AAA": SimpleNamespace(ema_f=2, ema_s=1, slope=1, atr=2.0)})
    c = cfg(stop_atr_mult=2.0, trail_atr_mult=3.0, trail_giveback_max=0.5,
            lock_ganador_umbral=0.05, lock_ganador_frac=0.5)
    e = Engine(c, daily)
    pos = {"precio_entrada": 100.0}
    assert e._trailing_stop_nivel("AAA", pos, 100.0, NOW) == 96.0          # 100 - 2 * ATR
    assert e._trailing_stop_nivel("AAA", pos, 110.0, NOW) == 105.0         # lock: 100 + 0.5 * 10
    assert e._trailing_stop_nivel("AAA", pos, 101.0, NOW) == 105.0         # ratchet never lowers it


def test_phase_one_runs_only_the_protective_stop():
    daily = Daily({"AAA": SimpleNamespace(ema_f=2, ema_s=1, slope=1, atr=2.0)})
    e = Engine(cfg(stop_atr_mult=2.0, lock_ganador_umbral=0.05), daily, phase1=True)
    assert e._trailing_stop_nivel("AAA", {"precio_entrada": 100.0}, 120.0, NOW) == 96.0


def test_without_daily_atr_the_stop_falls_back_to_a_percentage():
    e = Engine(cfg(trail_stop_pct=0.05, lock_ganador_umbral=0), Daily())
    assert e._trailing_stop_nivel("AAA", {"precio_entrada": 100.0}, 100.0, NOW) == pytest.approx(95.0)


def test_unknown_candidate_filter_is_an_error():
    e = Engine(cfg(filtro_candidatos="nope"), Daily({"AAA": BULL}))
    with pytest.raises(ValueError):
        e._score_estructural("AAA", NOW)


def test_session_dates_used_here_are_trading_days():
    from argbot.sessions import is_trading_day
    assert is_trading_day(date(2026, 10, 14))
