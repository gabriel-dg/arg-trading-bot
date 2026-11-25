"""Settings: validación, límites efectivos, reservas y snapshot reproducible offline."""
from datetime import datetime, timedelta, timezone
import json
import socket

import pytest

from argbot.lab.cli import main
from argbot.lab.config import Experiment
from argbot.lab.settings_engine import SettingsEngine as Engine
from argbot.lab.models import Quote
from argbot.lab.provenance import object_hash
from argbot.lab.replay import run
from argbot.lab.settings import DEFAULT_SETTINGS, BotSettings, OperatingLimits, load_settings

T = datetime(2026, 9, 7, 11, 15, tzinfo=timezone(timedelta(hours=-3)))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("settings tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def quote(symbol="A", ts=T, **changes):
    fields = dict(ts=ts, symbol=symbol, last=90., bid=89.95, ask=90.05,
                  low=88., high=94., vwap=92., atr=2., peak=100., support=88.,
                  bullish=True, group="energy")
    fields.update(changes)
    return Quote(**fields)


def submit(limits, symbols="ABCDEF", **config):
    engine = Engine(Experiment(**config), limits=limits)
    engine.step(T, {s: quote(s) for s in symbols})
    return engine


def reserved(engine):
    return sum(o.remaining * o.limit * (1 + o.fee_rate)
               for o in engine.broker.pending() if o.side == "BUY")


def test_default_file_and_limits_agree():
    from argbot.lab.settings import EventOptions
    assert load_settings(DEFAULT_SETTINGS) == BotSettings(events=EventOptions())


@pytest.mark.parametrize("key", ["max_position_pct", "max_invested_pct", "daily_loss_pct"])
@pytest.mark.parametrize("value", [0, -1, 100.1, True, "20", float("nan"), float("inf")])
def test_invalid_percent(key, value):
    with pytest.raises(ValueError):
        OperatingLimits(**{key: value})


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "5"])
def test_invalid_slots(value):
    with pytest.raises(ValueError):
        OperatingLimits(max_positions=value)


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_invalid_loss_basis(value):
    with pytest.raises(ValueError):
        OperatingLimits(include_unrealized_loss=value)


@pytest.mark.parametrize("change", [
    lambda x: x.update(unknown=1),
    lambda x: x.update(schema_version=True),
    lambda x: x.update(schema_version=2),
    lambda x: x["limits"].update(typo=20),
    lambda x: x["limits"].pop("max_positions"),
    lambda x: x.update(limits=[]),
])
def test_reject_unknown_missing_and_version(tmp_path, change):
    raw = OperatingLimits().snapshot()
    change(raw)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(path)


def test_reject_duplicate_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":1,"schema_version":1,"limits":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicado"):
        load_settings(path)


def test_default_limits_count_pending_buys_and_fees():
    engine = submit(OperatingLimits())
    assert len(engine.broker.pending()) == OperatingLimits().max_positions
    assert reserved(engine) <= engine.initial
    for order in engine.broker.pending():
        assert order.quantity * order.limit * (1 + order.fee_rate) <= engine.initial * .20


@pytest.mark.parametrize("pct", [100, None])
def test_full_position_never_borrows_or_ignores_fees(pct):
    engine = submit(OperatingLimits(max_position_pct=pct, max_positions=None, max_invested_pct=None))
    assert reserved(engine) <= engine.initial
    assert reserved(engine) > engine.initial * .99
    assert len(engine.broker.pending()) == 1


def test_unlimited_slots_still_obey_per_position():
    engine = submit(OperatingLimits(max_positions=None, max_position_pct=10))
    assert len(engine.broker.pending()) == 6
    assert reserved(engine) <= engine.initial * .60


def test_total_cap_applies_across_pending_orders():
    engine = submit(OperatingLimits(max_position_pct=60, max_positions=None, max_invested_pct=80))
    assert len(engine.broker.pending()) == 2
    assert reserved(engine) <= engine.initial * .80
    assert engine.unreserved_cash() >= engine.initial * .20


def test_partial_fill_counts_held_and_remaining_reservations():
    limits = OperatingLimits(max_position_pct=60, max_invested_pct=80)
    engine = submit(limits, symbols="A", entry_interval_min=1)
    later = T + timedelta(minutes=1)
    engine.step(later, {"A": quote("A", later, ask=89.94, bid=89.90, ask_size=10),
                        "B": quote("B", later), "C": quote("C", later)})
    assert engine.positions["A"].quantity == 10
    assert engine.broker.pending("A")[0].remaining > 0
    held = engine.equity() - engine.cash
    assert held + reserved(engine) <= engine.equity() * .80 + 1e-8
    assert engine.unreserved_cash() >= engine.equity() * .20 - 1e-8


def test_pending_sell_does_not_release_slot():
    engine = submit(OperatingLimits(max_positions=1), symbols="A", entry_interval_min=1)
    later = T + timedelta(minutes=1)
    engine.step(later, {"A": quote("A", later, ask=89.94, bid=89.90)})
    engine.submit_sell("A", quote("A", later), later, "test")
    engine.entries(later, {"A": quote("A", later), "B": quote("B", later)})
    assert not engine.broker.pending("B")
    assert any(row.get("reason") == "slots" for row in engine.audit)


@pytest.mark.parametrize("limit,loss,blocked", [(3, -6000, True), (3, -5999, False), (None, -6000, False)])
def test_daily_boundary_and_disabled(limit, loss, blocked):
    engine = submit(OperatingLimits(daily_loss_pct=limit), symbols="A")
    engine.day_realized = loss
    later = T + timedelta(seconds=1)
    engine.step(later, {"A": quote("A", later)})
    assert engine.day_blocked is blocked
    assert bool(engine.broker.pending()) is not blocked


def test_unrealized_loss_basis_and_no_forced_liquidation():
    engine = submit(OperatingLimits(include_unrealized_loss=True), symbols="A")
    engine.cash -= 6000  # simulate an equity decline, not a realized loss
    later = T + timedelta(seconds=1)
    engine.step(later, {"A": quote("A", later)})
    assert engine.day_realized == 0
    assert engine.day_blocked
    assert not engine.broker.pending()
    assert not any(o.side == "SELL" for o in engine.broker.orders)


def test_risk_budget_and_regime_still_restrict_full_allocation():
    engine = submit(OperatingLimits(max_position_pct=100), symbols="A", risk_per_trade=.001)
    assert reserved(engine) < engine.initial * .10
    engine = Engine(Experiment(), limits=OperatingLimits(max_position_pct=100))
    engine.step(T, {"A": quote(bullish=False)})
    assert reserved(engine) <= engine.initial * .50


def test_file_change_does_not_mutate_loaded_settings(tmp_path):
    path = tmp_path / "settings.json"
    limits = OperatingLimits(max_positions=1)
    path.write_text(json.dumps(limits.snapshot()), encoding="utf-8")
    loaded = load_settings(path)
    path.write_text(json.dumps(OperatingLimits(max_positions=5).snapshot()), encoding="utf-8")
    assert len(submit(loaded).broker.pending()) == 1


def test_replay_persists_effective_snapshot_and_preserves_output(tmp_path):
    limits = OperatingLimits(max_position_pct=10, max_positions=None)
    out = tmp_path / "replay"
    run("examples/synthetic.csv", Experiment(), out, limits=limits)
    snapshot = json.loads((out / "settings.json").read_text())
    manifest = json.loads((out / "manifest.json").read_text())
    assert snapshot == limits.snapshot() == manifest["operating_settings"]
    assert manifest["operating_settings_sha256"] == object_hash(snapshot)
    with pytest.raises(ValueError, match="vacío"):
        run("examples/synthetic.csv", Experiment(), out, limits=limits)


def test_cli_validation_and_replay(tmp_path, capsys):
    main(["validate-settings"])
    assert json.loads(capsys.readouterr().out)["network_requests"] == 0
    out = tmp_path / "cli"
    main(["simulate-settings", "--data", "examples/synthetic.csv", "--out", str(out)])
    assert (out / "settings.json").exists()
    assert json.loads(capsys.readouterr().out)["profile"] == "user_settings"


def test_legacy_replay_does_not_read_settings(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("historical replay must not read user settings")
    monkeypatch.setattr("argbot.lab.settings.load_settings", forbidden)
    out = tmp_path / "legacy"
    run("examples/synthetic.csv", Experiment(), out)
    assert not (out / "settings.json").exists()
    assert "operating_settings" not in json.loads((out / "manifest.json").read_text())
