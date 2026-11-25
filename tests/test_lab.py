"""Pruebas de invariantes de cartera, ejecución, disponibilidad y replay."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import pytest
from argbot.lab.config import Experiment
from argbot.lab.models import Quote
from argbot.lab.engine import Engine
from argbot.lab.execution import PaperBroker
from argbot.lab.events import EventBook, validate_event
from argbot.lab.risk import quantity
from argbot.lab.strategy import candidate
from argbot.lab.data import load_quotes, write_quotes, digest
from argbot.lab.replay import run

T = datetime(2026, 9, 7, 11, 15, tzinfo=timezone(timedelta(hours=-3)))

def q(ts=T, symbol="DEMO", price=90.0, **kw):
    fields = dict(ts=ts, symbol=symbol, last=price, bid=price-0.05, ask=price+0.05,
                  low=88.0, high=94.0, vwap=92.0, atr=2.0, peak=100.0, support=88.0,
                  bullish=True, group="energy")
    fields.update(kw)
    return Quote(**fields)

def event(**kw):
    e = dict(id="one", symbol="DEMO", published_at=(T-timedelta(hours=1)).isoformat(),
             observed_at=(T-timedelta(minutes=30)).isoformat(), analyzed_at=T.isoformat(),
             expires_at=(T+timedelta(days=1)).isoformat(), source_url="https://example.org/release",
             fact="Fictitious earnings increased.", interpretation="Potential catalyst",
             direction="positive", invalidation="Guidance withdrawn", evidence="earnings increased",
             source_hash="abc")
    e.update(kw)
    return e

def open_position(cfg=None):
    engine = Engine(cfg or Experiment())
    engine.step(T, {"DEMO": q()})
    engine.step(T+timedelta(minutes=5), {"DEMO": q(T+timedelta(minutes=5), price=89.8)})
    assert "DEMO" in engine.positions
    return engine

def test_order_never_fills_on_submission_snapshot():
    b = PaperBroker()
    b.submit(symbol="DEMO", side="BUY", quantity=3, limit=100, submitted=T, reason="test")
    assert b.advance({"DEMO": q()}) == []
    assert len(b.advance({"DEMO": q(T+timedelta(minutes=5))})) == 1

def test_passive_order_does_not_fill_just_because_day_low_touched():
    b = PaperBroker()
    b.submit(symbol="DEMO", side="BUY", quantity=3, limit=89, submitted=T, reason="test")
    assert not b.advance({"DEMO": q(T+timedelta(minutes=5), low=80)})
    assert b.orders[0].status == "pending"

def test_partial_fills_share_available_depth():
    b = PaperBroker()
    for _ in range(2):
        b.submit(symbol="DEMO", side="BUY", quantity=4, limit=91, submitted=T, reason="test")
    result = b.advance({"DEMO": q(T+timedelta(minutes=5), ask_size=5)})
    assert sum(f["quantity"] for _, f in result) == 5
    assert [o.filled for o in b.orders] == [4, 1]

def test_limit_never_executes_at_worse_price():
    b = PaperBroker()
    b.submit(symbol="DEMO", side="SELL", quantity=1, limit=90, submitted=T, reason="test")
    assert not b.advance({"DEMO": q(T+timedelta(minutes=5), price=89)})

def test_order_expires_next_session():
    b = PaperBroker()
    order = b.submit(symbol="DEMO", side="BUY", quantity=1, limit=100, submitted=T, reason="test")
    assert not b.advance({"DEMO": q(T+timedelta(days=1))})
    assert order.status == "cancelled"

def test_fill_uses_execution_price_and_frees_reservation():
    e = open_position()
    p = e.positions["DEMO"]
    assert p.entry == pytest.approx(89.85)
    assert e.cash == pytest.approx(e.initial - p.cost)
    assert e.free_cash() == pytest.approx(e.cash)

def test_pending_buys_reserve_cash_and_slots():
    e = Engine(Experiment(max_positions=2, max_position_pct=0.8))
    e.step(T, {s: q(symbol=s) for s in ("A", "B", "C")})
    orders = e.broker.pending()
    assert len(orders) <= 2
    assert sum(o.quantity * o.limit * (1+o.fee_rate) for o in orders) <= e.cash

def test_catastrophe_exits_first_touch_during_grace():
    e = open_position(Experiment(emergency_first_touch=True))
    now = T+timedelta(minutes=10)
    e.step(now, {"DEMO": q(now, price=80, low=80)})
    assert e.broker.pending()[0].reason == "catastrophic_stop"
    assert e.positions["DEMO"].quantity > 0  # envío no equivale a fill

def test_thesis_exit_applies_during_grace():
    e = open_position(Experiment(thesis_exit=True))
    now = T+timedelta(minutes=10)
    e.step(now, {"DEMO": q(now, price=87, low=87)})
    assert e.broker.pending()[0].reason == "price_invalidation"

def test_failed_sell_is_cancelled_before_replacement():
    e = open_position(Experiment(emergency_first_touch=True, sell_timeout_min=5))
    for minutes, price in ((10, 80), (15, 79)):
        now = T+timedelta(minutes=minutes)
        e.step(now, {"DEMO": q(now, price=price, low=price)})
    assert len(e.broker.pending("DEMO")) == 1
    sells = [o for o in e.broker.orders if o.side == "SELL"]
    assert [o.status for o in sells] == ["cancelled", "pending"]

def test_closed_trade_reconciles_cash_after_actual_fills():
    e = open_position(Experiment(emergency_first_touch=True))
    for minutes, price in ((10, 80), (15, 80.2)):
        now = T+timedelta(minutes=minutes)
        e.step(now, {"DEMO": q(now, price=price, low=price)})
    assert not e.positions
    assert e.realized == pytest.approx(e.cash-e.initial)
    assert e.summary()["unrealized_pnl"] == pytest.approx(0, abs=1e-8)
    assert len(e.trades) == 1

def test_missing_quote_blocks_new_risk():
    e = open_position(Experiment(entry_interval_min=15))
    now = T+timedelta(minutes=15)
    e.step(now, {"OTHER": q(now, symbol="OTHER")})
    assert e.audit[-1]["reason"] == "missing_position_quote"

def test_risk_budget_counts_existing_and_group_risk():
    cfg = Experiment(risk_per_trade=0.01, portfolio_risk=0.05, group_risk=0.02)
    values = dict(cfg=cfg, entry=100, stop=90, cash=100000, equity=100000,
                  allocated=0, fraction=1, regime=1, open_risk=0, group_risk=0)
    n = quantity(**values)
    assert n * (100*(1+cfg.fee)-90*(1-cfg.fee)) <= 1000
    assert quantity(**{**values, "group_risk": 2000}) == 0

def test_event_cannot_enter_before_analysis_or_after_expiry():
    book = EventBook([event(analyzed_at=(T+timedelta(hours=1)).isoformat())])
    assert book.latest("DEMO", T) is None
    assert book.latest("DEMO", T+timedelta(hours=2)) is not None
    assert book.latest("DEMO", T+timedelta(days=1)) is None

def test_news_is_separate_entry_path_without_drawdown():
    quote = q(price=100, peak=100, vwap=100, low=99, high=101)
    assert candidate(quote, Experiment())[0] is None
    c, _ = candidate(quote, Experiment(event_entries=True), event())
    assert c.reason == "event" and c.size_fraction == 0.5

def test_absence_of_news_does_not_veto_mechanical_entry():
    c, _ = candidate(q(), Experiment(event_entries=True))
    assert c is not None

def test_negative_event_cannot_close_before_its_analysis():
    e = open_position(Experiment(event_exits=True))
    e.events = EventBook([event(direction="negative", analyzed_at=(T+timedelta(minutes=15)).isoformat())])
    e.step(T+timedelta(minutes=10), {"DEMO": q(T+timedelta(minutes=10))})
    assert not e.broker.pending()
    e.step(T+timedelta(minutes=15), {"DEMO": q(T+timedelta(minutes=15))})
    assert e.broker.pending()[0].reason == "event_invalidation"


def test_source_date_cannot_be_in_the_future():
    with pytest.raises(ValueError):
        validate_event(event(observed_at=(T+timedelta(hours=1)).isoformat()))

def test_replay_is_reproducible_and_refuses_overwrite(tmp_path):
    path = tmp_path / "quotes.csv"
    write_quotes(path, [q(), q(T+timedelta(minutes=5), price=89.8)])
    first = run(path, Experiment(), tmp_path/"first")
    second = run(path, Experiment(), tmp_path/"second")
    assert first == second
    assert json.loads((tmp_path/"first"/"manifest.json").read_text())["csv_sha256"] == digest(path)
    with pytest.raises(ValueError, match="vacío"):
        run(path, Experiment(), tmp_path/"first")

def test_strict_data_rejects_duplicates_and_naive_time(tmp_path):
    with pytest.raises(ValueError):
        q(ts=T.replace(tzinfo=None))
    path = tmp_path/"quotes.csv"
    write_quotes(path, [q(), q()])
    with pytest.raises(ValueError, match="duplicado"):
        load_quotes(path)

@pytest.mark.parametrize("kwargs", [{"risk_per_trade": float("nan")}, {"max_positions": 0},
                                    {"max_position_pct": 2}, {"entry_interval_min": 1.5},
                                    {"entry_mode": "guess"}])
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        Experiment(**kwargs)

def test_overnight_loss_included_in_daily_guard():
    e = open_position(Experiment(include_unrealized_loss=True, daily_loss_pct=0.005))
    now = T + timedelta(days=1)
    e.step(now, {"DEMO": q(now, price=80, low=80)})
    assert e.day_blocked

def test_risk_reservation_uses_invalidation_when_enabled():
    cfg = Experiment(thesis_exit=True, risk_per_trade=0.01)
    e = Engine(cfg)
    e.step(T, {"DEMO": q()})
    order = e.broker.pending()[0]
    expected = order.remaining * (order.limit * (1+cfg.fee) - order.invalidation * (1-cfg.fee))
    assert e.pending_risk() == pytest.approx(expected)

def test_importer_recovers_after_counter_reset(tmp_path):
    import csv
    from datetime import date
    from argbot.lab.data import import_market
    source = tmp_path/"source"
    daily = source/"daily"
    daily.mkdir(parents=True)
    with (daily/"DEMO.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fecha","apertura","maximo","minimo","cierre","volumen"])
        for i in range(45):
            d = date(2026,7,1) + timedelta(days=i)
            w.writerow([d.isoformat(),100,102,98,100,1000])
    with (source/"DEMO_ticks.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp","ultimo_precio","monto_operado","minimo","maximo"])
        for i, amount in enumerate([10000000,10000,12000]):
            w.writerow([(T+timedelta(minutes=i*5)).isoformat(),100,amount,98,102])
    out = tmp_path/"imported.csv"
    meta = import_market(source,out,start=T.date(),end=T.date(),spread_bps=20,symbols=["DEMO"])
    frames = load_quotes(out)
    assert len(frames) == 3 and meta["rejected_rows"] == 0
    assert meta["quality_events"]["counter_reset_reanchored"] == 1
    assert not frames[1][1]["DEMO"].reliable
    assert frames[2][1]["DEMO"].vwap == 100
