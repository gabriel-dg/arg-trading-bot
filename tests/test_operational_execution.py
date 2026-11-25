"""Failure-boundary tests, with an independent in-memory broker; no live IO."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import argbot.execution as execution
from argbot.execution import (
    AccountView, BrokerRejected, ExecutionBlocked, ExecutionController, OrderView, RequestNotSent,
)
from argbot.lock import AccountLease, AlreadyRunning


NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


class FakeBroker:
    def __init__(self, cash="1000", positions=None, reserve="0.01"):
        self.cash = Decimal(cash)
        self.positions = dict(positions or {})
        self.reserve = Decimal(reserve)
        self.orders = {}
        self.sent = []
        self.cancels = []
        self.failure = None
        self.cancel_failure = False
        self.read_failure = False
        self.before_send = None
        self.account_id = "fixture"

    def submit(self, intent):
        if self.before_send:
            self.before_send(intent)
        self.sent.append(deepcopy(intent))
        if self.failure == "rejected":
            raise BrokerRejected()
        broker_id = str(len(self.sent))
        self.orders[broker_id] = OrderView(
            broker_id, intent["symbol"], intent["side"], intent["quantity"],
            intent["limit"], intent["created_at"], "open", 0, "0", "0", self.account_id)
        if self.failure == "timeout":
            raise TimeoutError("response lost after broker accepted")
        if self.failure == "crash":
            raise KeyboardInterrupt("process died after broker accepted")
        if self.failure == "missing_id":
            return None
        return broker_id

    def order(self, broker_id):
        if self.read_failure:
            raise ConnectionError("read unavailable")
        return self.orders[broker_id]

    def account(self):
        if self.read_failure:
            raise ConnectionError("snapshot unavailable")
        hold = sum((v.requested_quantity - v.filled_quantity) * Decimal(v.limit) * (1 + self.reserve)
                   for v in self.orders.values() if v.status == "open" and v.side == "BUY")
        return AccountView(str(self.cash - hold), dict(self.positions),
                           frozenset(v.broker_id for v in self.orders.values() if v.status == "open"),
                           self.account_id)

    def fill(self, broker_id, qty, gross, fees, status="open"):
        old = self.orders[broker_id]
        delta_qty = qty - old.filled_quantity
        delta_gross = Decimal(gross) - Decimal(old.gross)
        delta_fees = Decimal(fees) - Decimal(old.fees)
        sign = 1 if old.side == "BUY" else -1
        self.positions[old.symbol] = self.positions.get(old.symbol, 0) + sign * delta_qty
        if not self.positions[old.symbol]:
            del self.positions[old.symbol]
        self.cash += (-delta_gross if old.side == "BUY" else delta_gross) - delta_fees
        self.orders[broker_id] = replace(old, status=status, filled_quantity=qty, gross=gross, fees=fees)

    def cancel(self, broker_id):
        self.cancels.append(broker_id)
        if self.cancel_failure:
            raise TimeoutError()
        self.orders[broker_id] = replace(self.orders[broker_id], status="cancelled")


@pytest.fixture
def setup_controller(tmp_path, monkeypatch):
    # Any accidental real socket use in this module fails immediately.
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    owned = []

    def setup(*, positions=None, external=None, cash="1000"):
        directory = tmp_path / ("candidate" + str(len(owned)))
        account = "fixture-account-" + directory.name + str(tmp_path)
        ExecutionController.prepare(directory, account, cash=cash, positions=positions,
                                    external_positions=external, fee_reserve="0.01")
        broker_positions = dict(external or {})
        for s, p in (positions or {}).items():
            broker_positions[s] = broker_positions.get(s, 0) + p["quantity"]
        broker = FakeBroker(cash, broker_positions)
        broker.account_id = account
        controller = ExecutionController(directory, account, broker)
        owned.append(controller)
        return controller, broker, directory, account

    yield setup
    for controller in owned:
        controller.close()


def test_intent_and_reservation_are_durable_before_post(setup_controller):
    c, b, directory, _ = setup_controller()
    def before(intent):
        state = json.loads((directory / "execution-state.json").read_text())["state"]
        assert state["orders"][intent["local_id"]]["state"] == "submitting"
        assert Decimal(state["cash"]) == Decimal("798")
    b.before_send = before
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    assert c.state["orders"][local]["state"] == "open"
    assert c.state["positions"] == {}  # acceptance is not a fill


@pytest.mark.parametrize("failure", ["timeout", "missing_id", "crash"])
def test_uncertain_order_survives_restart_and_never_resubmits(setup_controller, failure):
    c, b, folder, account = setup_controller()
    b.failure = failure
    if failure == "crash":
        with pytest.raises(KeyboardInterrupt):
            c.submit("AAA", "BUY", 2, "100", now=NOW)
        local = next(iter(c.state["orders"]))
    else:
        local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    assert not c.state["positions"]
    c.close()
    with ExecutionController(folder, account, b) as resumed:
        assert not resumed.ready
        assert not resumed.reconcile()
        assert Decimal(resumed.state["cash"]) == Decimal("798")
        with pytest.raises(ExecutionBlocked):
            resumed.submit("BBB", "BUY", 1, "10", now=NOW)
        assert len(b.sent) == 1
        resumed.bind_unknown(local, "1")
        assert resumed.reconcile()
        assert not resumed.state["positions"]
        b.fill("1", 2, "198", "1.98", "filled")
        assert resumed.reconcile()
        assert resumed.state["positions"]["AAA"]["quantity"] == 2
        assert Decimal(resumed.state["positions"]["AAA"]["cost"]) == Decimal("199.98")


def test_empty_or_ambiguous_lookup_cannot_release_unknown_cash(setup_controller):
    c, b, _, _ = setup_controller()
    b.failure = "timeout"
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    b.orders.clear()
    with pytest.raises(KeyError):
        c.bind_unknown(local, "1")
    assert not c.reconcile()
    assert Decimal(c.state["cash"]) == Decimal("798")
    assert len(b.sent) == 1


def test_binding_rejects_old_or_different_order(setup_controller):
    c, b, _, _ = setup_controller()
    b.failure = "timeout"
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    original = b.orders["1"]
    for bad in [replace(original, submitted_at=(NOW-timedelta(minutes=1)).isoformat()),
                replace(original, symbol="BBB"), replace(original, requested_quantity=3),
                replace(original, limit="101"), replace(original, side="SELL")]:
        b.orders["1"] = bad
        with pytest.raises(ValueError):
            c.bind_unknown(local, "1")
    assert c.state["orders"][local]["broker_id"] is None


def test_buy_partial_cancel_sell_partial_and_restart_account_exactly(setup_controller):
    c, b, folder, account = setup_controller()
    local = c.submit("AAA", "BUY", 4, "100", now=NOW)
    b.fill("1", 2, "198", "1.98")
    assert c.reconcile()
    assert Decimal(c.state["cash"]) == Decimal("598.02")
    assert c.state["positions"]["AAA"]["quantity"] == 2
    before = c.state
    assert c.reconcile()
    assert c.state["cash"] == before["cash"]
    c.cancel(local)
    assert c.state["orders"][local]["state"] == "cancelled"
    assert Decimal(c.state["cash"]) == Decimal("800.02")
    c.close()
    with ExecutionController(folder, account, b) as resumed:
        sell = resumed.submit("AAA", "SELL", 2, "110", now=NOW+timedelta(days=1))
        b.fill("2", 1, "111", "1.11")
        assert resumed.reconcile()
        assert resumed.state["positions"]["AAA"]["quantity"] == 1
        assert Decimal(resumed.state["positions"]["AAA"]["cost"]) == Decimal("99.99")
        b.fill("2", 2, "222", "2.22", "filled")
        assert resumed.reconcile()
        assert resumed.state["positions"] == {}
        assert Decimal(resumed.state["cash"]) == Decimal("1019.80")
        assert Decimal(resumed.state["realized"]) == Decimal("19.80")
        assert resumed.state["orders"][sell]["filled"] == 2
        assert len(b.sent) == 2


def test_cancel_timeout_does_not_release_or_resend(setup_controller):
    c, b, _, _ = setup_controller()
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    b.cancel_failure = True
    c.cancel(local)
    c.cancel(local)
    assert b.cancels == ["1"]
    assert not c.reconcile()
    assert Decimal(c.state["cash"]) == Decimal("798")
    b.fill("1", 1, "99", ".99", "cancelled")
    assert c.reconcile()
    assert Decimal(c.state["cash"]) == Decimal("900.01")


def test_charges_posted_when_a_partly_filled_buy_ends_are_booked_not_refused(setup_controller):
    # A partly filled buy shows no charges; the broker posts them only when the
    # cancelled rest closes the order. Synthetic figures.
    c, b, _, _ = setup_controller(cash="30000")
    local = c.submit("QB6", "BUY", 9, "2200", now=NOW)
    b.fill("1", 4, "8800", "0")
    assert c.reconcile()
    b.cancel = b.cancels.append  # accepted; the broker still reads the order open
    c.cancel(local)
    assert c.state["orders"][local]["state"] == "cancel_pending"
    assert not c.reconcile() and c.failure == "unresolved_order"
    b.fill("1", 4, "8800", "57.20", "cancelled")
    assert c.reconcile()
    order = c.state["orders"][local]
    assert order["state"] == "cancelled" and Decimal(order["hold"]) == 0 and Decimal(order["fees"]) == Decimal("57.20")
    assert Decimal(c.state["positions"]["QB6"]["cost"]) == Decimal("8857.20")
    assert Decimal(c.state["cash"]) == Decimal("21142.80")  # 30000 - 8800 - 57.20
    assert c.reconcile()


def test_charges_posted_when_a_partly_filled_sale_ends_reduce_cash_and_result(setup_controller):
    position = {"quantity": 4, "cost": "400", "opened_at": "2026-09-25T15:00:00+00:00"}
    c, b, _, _ = setup_controller(positions={"AAA": position})
    local = c.submit("AAA", "SELL", 4, "110", now=NOW)
    b.fill("1", 2, "220", "0")
    assert c.reconcile()
    b.fill("1", 2, "220", "1.46", "cancelled")
    assert c.reconcile()
    assert Decimal(c.state["cash"]) == Decimal("1218.54")
    assert Decimal(c.state["realized"]) == Decimal("18.54")
    assert Decimal(c.state["orders"][local]["realized"]) == Decimal("18.54")
    assert c.state["positions"]["AAA"]["quantity"] == 2 and Decimal(c.state["positions"]["AAA"]["cost"]) == 200


def test_a_charge_or_amount_change_without_a_close_still_needs_review(setup_controller, caplog):
    c, b, _, _ = setup_controller()
    c.submit("AAA", "BUY", 4, "100", now=NOW)
    b.fill("1", 2, "198", "0")
    assert c.reconcile()
    b.fill("1", 2, "198", "1.98")  # charges move on an order still open
    assert not c.reconcile() and c.failure == "ValueError"
    assert "Fill correction requires explicit review" in caplog.text
    with pytest.raises(ExecutionBlocked):  # the broker cash it moved is not booked: no buy
        c.submit("BBB", "BUY", 1, "10", now=NOW)
    assert len(b.sent) == 1
    b.fill("1", 2, "199", "1.98", "cancelled")  # gross moves at the close without a new fill
    assert not c.reconcile()
    assert Decimal(c.state["cash"]) == Decimal("600")


def set_aside(setup_controller, unbound=False):
    # AAA is held and reads fine; a BBB buy, 2 of 4 filled, then reads "complete".
    position = {"quantity": 2, "cost": "180", "opened_at": "2026-09-25T15:00:00+00:00"}
    c, b, _, _ = setup_controller(positions={"AAA": position})
    c.submit("BBB", "BUY", 4, "100", now=NOW)
    b.fill("1", 2, "198", "1.98")
    assert c.reconcile()
    if unbound:
        b.failure = "timeout"
        c.submit("CCC", "BUY", 1, "10", now=NOW)
        b.failure = None
    b.orders["1"] = replace(b.orders["1"], status="filled")
    return c, b


def test_an_order_we_refuse_is_set_aside_and_only_other_holdings_can_be_sold(setup_controller, caplog):
    c, b = set_aside(setup_controller)
    local = next(iter(c.state["orders"]))
    assert not c.reconcile() and c.failure == "ValueError" and not c.ready
    assert c.exits_only and c.quarantined == {"BBB": local}
    assert c.state["last_reconciliation"]["ok"] is False
    assert "Order set aside: BBB: ValueError" in caplog.text
    for symbol, side in (("CCC", "BUY"), ("BBB", "SELL")):
        with pytest.raises(ExecutionBlocked):
            c.submit(symbol, side, 1, "110", now=NOW)
    sell = c.submit("AAA", "SELL", 2, "110", now=NOW)
    assert [o["symbol"] for o in b.sent] == ["BBB", "AAA"] and not b.cancels
    b.fill("2", 2, "220", "2.20", "filled")
    assert not c.reconcile() and c.exits_only
    assert "AAA" not in c.state["positions"] and c.state["orders"][sell]["state"] == "filled"
    assert c.state["orders"][local]["state"] == "open" and Decimal(c.state["orders"][local]["hold"]) == 202
    b.orders["1"] = replace(b.orders["1"], status="cancelled")  # the broker's record reads cleanly
    assert c.reconcile() and c.ready and not c.exits_only and not c.quarantined
    assert c.state["positions"]["BBB"]["quantity"] == 2 and Decimal(c.state["cash"]) == Decimal("1017.82")


def test_a_cancel_still_pending_sets_only_its_symbol_aside(setup_controller):
    position = {"quantity": 2, "cost": "180", "opened_at": "2026-09-25T15:00:00+00:00"}
    c, b, _, _ = setup_controller(positions={"AAA": position})
    local = c.submit("BBB", "BUY", 2, "100", now=NOW)
    b.cancel = b.cancels.append  # accepted; the broker still reads the order open
    c.cancel(local)
    assert not c.reconcile() and c.failure == "unresolved_order" and c.quarantined == {"BBB": local}
    c.submit("AAA", "SELL", 2, "110", now=NOW)
    assert b.sent[-1]["symbol"] == "AAA" and b.cancels == ["1"]


@pytest.mark.parametrize("case", ["account_unreadable", "other_holding_differs", "foreign_open_order", "unbound_order"])
def test_anything_beyond_the_set_aside_order_still_blocks_every_order(setup_controller, case):
    c, b = set_aside(setup_controller, unbound=case == "unbound_order")
    if case == "account_unreadable":
        def unreadable():
            raise ConnectionError("snapshot unavailable")
        b.account = unreadable
    elif case == "other_holding_differs":
        b.positions["AAA"] = 1
    elif case == "foreign_open_order":
        b.orders["manual"] = OrderView("manual", "Z", "SELL", 1, "1", NOW.isoformat(), "open", 0, "0", "0", b.account_id)
    sent = len(b.sent)
    assert not c.reconcile() and not c.exits_only and not c.quarantined
    with pytest.raises(ExecutionBlocked):
        c.submit("AAA", "SELL", 2, "110", now=NOW)
    assert len(b.sent) == sent


def test_failed_reconciliation_never_marks_ready_and_preserves_metadata(setup_controller):
    position = {"quantity": 2, "cost": "180", "stop": "80", "peak": "115",
                "opened_at": "2026-09-25T15:00:00+00:00", "confirmations": 1}
    c, b, folder, account = setup_controller(positions={"AAA": position})
    assert c.reconcile()
    c.close()
    b.read_failure = True
    with ExecutionController(folder, account, b) as resumed:
        assert not resumed.ready
        assert not resumed.reconcile()
        with pytest.raises(ExecutionBlocked):
            resumed.submit("BBB", "BUY", 1, "10", now=NOW)
        assert resumed.state["positions"]["AAA"] == position
        assert not b.sent
        b.read_failure = False
        b.positions["AAA"] = 1
        assert not resumed.reconcile()
        assert resumed.failure == "position_discrepancy"
        assert resumed.state["positions"]["AAA"] == position
        b.positions["AAA"] = 2
        assert resumed.reconcile()


def test_external_holdings_never_adopted_or_sold(setup_controller):
    c, b, _, _ = setup_controller(external={"MANUAL": 3})
    assert c.reconcile()
    with pytest.raises(ExecutionBlocked):
        c.submit("MANUAL", "SELL", 1, "10", now=NOW)
    b.positions["OTHER"] = 1
    assert not c.reconcile()
    assert not c.state["positions"]
    assert not b.sent


def test_unknown_broker_orders_block(setup_controller):
    c, b, _, _ = setup_controller()
    b.orders["manual"] = OrderView("manual", "Z", "SELL", 1, "1", NOW.isoformat(), "open", 0, "0", "0", b.account_id)
    assert not c.reconcile()
    assert c.failure == "open_order_discrepancy"
    assert c.cash is None and c.buying_cash() == 0


def test_less_cash_at_iol_limits_buys_and_never_blocks(setup_controller):
    """Rule: a cash difference never stops the bot; buys use the lower amount."""
    c, b, _, _ = setup_controller()
    assert c.reconcile() and "cash" not in c.state["last_reconciliation"]
    assert c.buying_cash() == Decimal("1000")
    b.cash = Decimal("950")
    assert c.reconcile() and c.ready and c.failure is None
    record = c.state["last_reconciliation"]
    assert record["ok"] is True and record["reason"] is None
    assert Decimal(record["cash"]["broker_minus_book"]) == Decimal("-50") and record["cash"]["buys_allowed"]
    assert c.buying_cash() == Decimal("950")
    with pytest.raises(ExecutionBlocked, match="Insufficient assigned free cash"):
        c.submit("AAA", "BUY", 1, "945", now=NOW)  # 954.45 with the 1% reserve
    local = c.submit("AAA", "BUY", 1, "940", now=NOW)
    assert c.state["orders"][local]["state"] == "open"
    # The reservation lowers the book and IOL alike: the shortfall stays 50.
    assert c.reconcile() and c.buying_cash() == Decimal("1000") - Decimal("949.40") - 50


def test_a_shortfall_the_book_cannot_explain_stops_buys_only(setup_controller):
    c, b, _, _ = setup_controller(positions={"AAA": {"quantity": 2, "cost": "200"}})
    b.positions["AAA"] = 2
    # 5% of the book (1000 cash + 200 cost) is 60: a 60 shortfall still buys.
    b.cash = Decimal("940")
    assert c.reconcile() and c.state["last_reconciliation"]["cash"]["buys_allowed"]
    b.cash = Decimal("939.99")
    assert c.reconcile() and c.ready
    record = c.state["last_reconciliation"]
    assert record["ok"] is True and record["cash"]["buys_allowed"] is False
    assert c.buying_cash() == 0
    with pytest.raises(ExecutionBlocked, match="available_cash_shortfall"):
        c.submit("BBB", "BUY", 1, "1", now=NOW)
    local = c.submit("AAA", "SELL", 2, "100", now=NOW)
    assert c.state["orders"][local]["state"] == "open"


def test_recent_sales_at_48_hours_explain_the_shortfall(setup_controller):
    """A stop sale settled at 48 h; its cash was not in IOL's 24 h balance."""
    c, b, _, _ = setup_controller(positions={"QC3": {"quantity": 2, "cost": "10000.00"}}, cash="20000.00")
    b.positions["QC3"] = 2
    b.cash = Decimal("20000.00")
    sent = datetime.now(timezone.utc)
    local = c.submit("QC3", "SELL", 2, "5300", now=sent)
    b.fill("1", 2, "10600.00", "70.00", status="filled")
    b.orders["1"] = replace(b.orders["1"], settlement="a48horas")
    b.cash -= Decimal("10600.00") - Decimal("70.00")  # IOL pays it later
    assert c.reconcile()
    order = c.state["orders"][local]
    assert order["state"] == "filled" and order["broker_settlement"] == "a48horas"
    assert Decimal(c.state["cash"]) == Decimal("30530.00")
    assert Decimal(c.state["realized"]) == Decimal("530.00")
    cash = c.state["last_reconciliation"]["cash"]
    assert Decimal(cash["shortfall"]) == Decimal("10530.00") and Decimal(cash["unsettled_sales"]) == Decimal("10530.00")
    assert cash["buys_allowed"] and c.buying_cash() == Decimal("20000.00")
    # IOL may keep listing the 2 sold shares until it pays them: accepted, never more.
    b.positions["QC3"] = 2
    assert c.reconcile() and "QC3" not in c.state["positions"]
    b.positions["QC3"] = 3
    assert not c.reconcile() and c.failure == "position_discrepancy"
    b.positions["QC3"] = 2
    state = c.state
    assert execution.holdings_agree({}, {"QC3": 2}, state, sent)
    assert not execution.holdings_agree({"QC3": 1}, {}, state, sent)  # fewer at IOL: refused
    # The same sale recorded at 24 h explains nothing: the shortfall exceeds 5% of the book.
    state["orders"][local]["broker_settlement"] = "a24horas"
    assert not execution.cash_check(state, b.cash, sent)["buys_allowed"]
    assert not execution.holdings_agree({}, {"QC3": 2}, state, sent)
    # Nor does a sale older than the window.
    state["orders"][local]["broker_settlement"] = "a48horas"
    later = sent + timedelta(days=execution.UNSETTLED_DAYS, seconds=1)
    assert not execution.cash_check(state, b.cash, later)["buys_allowed"]
    assert not execution.holdings_agree({}, {"QC3": 2}, state, later)


def test_rejected_order_releases_reserve_but_unknown_does_not(setup_controller):
    c, b, _, _ = setup_controller()
    b.failure = "rejected"
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    assert c.state["orders"][local]["state"] == "rejected"
    assert Decimal(c.state["cash"]) == Decimal("1000")
    assert c.reconcile()


def test_a_send_refused_before_the_network_releases_the_reserve(setup_controller):
    """The quote expired during the reconciliation and the guard refused the POST."""
    c, b, _, _ = setup_controller()
    def refuse(intent):
        raise RequestNotSent("Quote expired during reconciliation or authentication")
    b.before_send = refuse
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    order = c.state["orders"][local]
    assert order["state"] == "rejected" and order["hold"] == "0" and not b.sent
    assert order["not_sent"] == "Quote expired during reconciliation or authentication"
    assert Decimal(c.state["cash"]) == Decimal("1000") and c.reconcile()
    # An outcome that is not known keeps the reserve and records only the exception type.
    b.before_send = None
    b.failure = "timeout"
    local = c.submit("BBB", "BUY", 1, "100", now=NOW)
    order = c.state["orders"][local]
    assert order["state"] == "unknown" and order["submit_error"] == "TimeoutError" and "not_sent" not in order
    assert Decimal(c.state["cash"]) == Decimal("1000") - Decimal("101")


def test_only_the_not_sent_marker_releases_a_blocked_send(setup_controller):
    c, b, _, _ = setup_controller()
    def blocked(intent):
        raise ExecutionBlocked("Quote expired during reconciliation or authentication")
    b.before_send = blocked
    local = c.submit("AAA", "BUY", 1, "100", now=NOW)
    assert c.state["orders"][local]["state"] == "unknown"
    assert c.state["orders"][local]["submit_error"] == "ExecutionBlocked"


def test_pause_blocks_buys_but_retains_known_position_exit(setup_controller):
    c, b, _, _ = setup_controller(positions={"AAA": {"quantity": 2, "cost": "180"}})
    c.pause()
    with pytest.raises(ExecutionBlocked):
        c.submit("BBB", "BUY", 1, "10", now=NOW)
    c.submit("AAA", "SELL", 2, "100", now=NOW)
    assert len(b.sent) == 1


def test_bad_fill_cannot_create_position_or_consume_reservation(setup_controller):
    c, b, _, _ = setup_controller()
    local = c.submit("AAA", "BUY", 2, "100", now=NOW)
    before = c.state
    for bad in [replace(b.orders["1"], status="filled"),
                replace(b.orders["1"], filled_quantity=3, gross="300"),
                replace(b.orders["1"], filled_quantity=1, gross="NaN"),
                replace(b.orders["1"], filled_quantity=1, gross="0")]:
        with pytest.raises(ValueError):
            c.observe(local, bad)
        assert c.state == before


def test_storage_failure_prevents_post_and_poisoned_instance_cannot_continue(setup_controller, monkeypatch):
    c, b, _, _ = setup_controller()
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(c, "_write", fail)
    with pytest.raises(OSError):
        c.submit("AAA", "BUY", 1, "100", now=NOW)
    assert not b.sent
    with pytest.raises(ExecutionBlocked):
        c.reconcile()


def test_missing_corrupt_state_or_wrong_account_never_resets(tmp_path):
    directory = tmp_path / "state"
    with pytest.raises(FileNotFoundError):
        ExecutionController(directory, "fixture", FakeBroker())
    ExecutionController.prepare(directory, "fixture", cash="1000")
    with pytest.raises(ValueError, match="another account"):
        ExecutionController(directory, "other", FakeBroker())
    path = directory / "execution-state.json"
    envelope = json.loads(path.read_text())
    envelope["state"]["cash"] = "9999"
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="checksum"):
        ExecutionController(directory, "fixture", FakeBroker())
    with pytest.raises(FileExistsError):
        ExecutionController.prepare(directory, "fixture", cash="1000")


def test_crash_after_receipt_before_save_keeps_unbound_intent(setup_controller, monkeypatch):
    c, b, folder, account = setup_controller()
    def fail(*args):
        raise OSError("failed to persist broker receipt")
    b.before_send = lambda intent: monkeypatch.setattr(c, "_write", fail)
    with pytest.raises(OSError):
        c.submit("AAA", "BUY", 2, "100", now=NOW)
    assert len(b.sent) == 1
    c.close()
    with ExecutionController(folder, account, b) as resumed:
        local = next(iter(resumed.state["orders"]))
        assert resumed.state["orders"][local]["state"] == "submitting"
        assert not resumed.reconcile()
        resumed.bind_unknown(local, "1")
        assert resumed.reconcile()
        assert len(b.sent) == 1


def test_uncertain_sale_keeps_position_and_does_not_sell_twice(setup_controller):
    c, b, folder, account = setup_controller(positions={"AAA": {"quantity": 2, "cost": "180"}})
    b.failure = "timeout"
    local = c.submit("AAA", "SELL", 2, "100", now=NOW)
    assert c.state["positions"]["AAA"]["quantity"] == 2
    c.close()
    with ExecutionController(folder, account, b) as resumed:
        with pytest.raises(ExecutionBlocked):
            resumed.submit("AAA", "SELL", 2, "100", now=NOW)
        resumed.bind_unknown(local, "1")
        b.fill("1", 2, "200", "2", "filled")
        assert resumed.reconcile()
        assert resumed.state["positions"] == {}
        assert Decimal(resumed.state["cash"]) == Decimal("1198")
        assert Decimal(resumed.state["realized"]) == Decimal("18")
        assert len(b.sent) == 1


def test_cash_and_duplicate_order_guards(setup_controller):
    c, b, _, _ = setup_controller()
    with pytest.raises(ExecutionBlocked):
        c.submit("AAA", "BUY", 10, "100", now=NOW)
    assert not b.sent
    c.submit("AAA", "BUY", 2, "100", now=NOW)
    with pytest.raises(ExecutionBlocked):
        c.submit("AAA", "BUY", 1, "100", now=NOW)
    with pytest.raises(ExecutionBlocked):
        c.submit("BBB", "SELL", 1, "100", now=NOW)
    assert len(b.sent) == 1


def test_diagnostic_is_durable_without_marking_failed_sync_successful(setup_controller):
    c, b, folder, _ = setup_controller()
    b.read_failure = True
    assert not c.reconcile()
    state = json.loads((folder / "execution-state.json").read_text())["state"]
    assert state["last_reconciliation"]["ok"] is False
    assert state["last_reconciliation"]["reason"] == "ConnectionError"
    assert state["events"][-1]["reconciliation"]["ok"] is False


def test_broker_identity_must_match_locked_account(setup_controller):
    c, b, _, _ = setup_controller()
    b.account_id = "different-account-with-same-balances"
    with pytest.raises(ExecutionBlocked, match="broker_account_mismatch"):
        c.submit("AAA", "BUY", 1, "100", now=NOW)
    assert not b.sent


def test_account_lease_excludes_second_directory_and_releases_after_crash(tmp_path):
    account = "lease-crash-" + str(tmp_path)
    script = ("from argbot.lock import AccountLease; import sys; "
              "lease=AccountLease(sys.argv[1]).acquire(); print('locked',flush=True); sys.stdin.read()")
    process = subprocess.Popen([sys.executable, "-B", "-u", "-c", script, account],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=Path(__file__).resolve().parents[1])
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning):
            AccountLease(account).acquire()
        process.kill()
        process.wait(timeout=10)
        with AccountLease(account):
            with pytest.raises(AlreadyRunning):
                AccountLease(account).acquire()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("unbound", [False, True])
def test_wrong_account_order_cannot_change_ledger(setup_controller, unbound):
    c, b, _, _ = setup_controller()
    if unbound:
        b.failure = "timeout"
    local = c.submit("AAA", "BUY", 1, "100", now=NOW)
    before = c.state
    alien = replace(b.orders["1"], account_id="other-account", status="filled",
                    filled_quantity=1, gross="100", fees="1")
    b.orders["1"] = alien
    with pytest.raises(ExecutionBlocked, match="broker_order_account_mismatch"):
        if unbound:
            c.bind_unknown(local, "1")
        else:
            c.observe(local, alien)
    assert c.state == before
    assert len(b.sent) == 1
