"""Durable execution controls for the strategy; no implicit broker client.

The broker boundary is deliberately explicit. A broker adapter must provide
confirmed cumulative fills and account snapshots, never infer fills from an
accepted submission.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from argbot.lock import AccountLease, account_key

LOG = logging.getLogger('argbot.execution')


class ExecutionBlocked(RuntimeError):
    pass


class BrokerRejected(Exception):
    """Adapter has explicit evidence the broker rejected the submission.

    A timeout, malformed success, 5xx, empty search or authentication ambiguity
    MUST NOT be represented by this exception.

    `detail` is the broker's own reason, already masked by the adapter, so a refusal
    can be explained later. It is kept apart from the
    exception text, which stays the fixed code, so no logger prints it by accident.
    """

    def __init__(self, code="explicit_business_rejection", detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code)


# The one refusal that is expected in normal operation: the quote aged out while the
# order was reconciled. Every other refusal before the network can persist.
QUOTE_EXPIRED = "Quote expired during reconciliation or authentication"


class RequestNotSent(ExecutionBlocked):
    """A mutation stopped BEFORE the network transport: nothing reached the broker.

    Only BudgetTransport raises it, around the pre-send guard and the budget charge
    (for example a BUY whose quote expired during the reconciliation, which must not
    be recorded as `unknown`). Any failure once the request has
    been handed to the transport is NOT this. `code` keeps the fixed refusal text,
    never request or response data.
    """

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def money(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not money")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid money") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Money must be finite and nonnegative")
    return result


def quantity(value, *, positive=False) -> int:
    if type(value) is not int or value < int(positive):
        raise ValueError("Invalid integer quantity")
    return value


def integer(value, positive=False) -> int:
    """An integral amount from a broker payload (int, str or Decimal-like)."""
    amount = money(value)
    if amount != amount.to_integral_value() or amount < int(positive):
        raise ValueError('Expected integral quantity')
    return int(amount)


_FRACTION = re.compile(r'(\d\d:\d\d:\d\d)\.(\d+)')


def parse_timestamp(value: str) -> datetime:
    """`datetime.fromisoformat` that behaves the same on Python 3.10 and later.

    Brokers send a variable number of fractional-second digits (IOL: 1 to 7) and
    sometimes a trailing `Z`. Python 3.10 only accepts 3 or 6 digits and no `Z`, so
    the fraction is padded or cut to microseconds before parsing.
    """
    if type(value) is not str:
        raise ValueError("Timestamp must be a string")
    text = _FRACTION.sub(lambda m: m.group(1) + "." + (m.group(2) + "000000")[:6], value, count=1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def instant(value: str) -> datetime:
    result = parse_timestamp(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


@dataclass(frozen=True)
class OrderView:
    broker_id: str
    symbol: str
    side: str
    requested_quantity: int
    limit: str
    submitted_at: str
    status: str  # open, filled, cancelled, rejected
    filled_quantity: int
    gross: str  # confirmed cumulative executed amount, not limit * quantity
    fees: str   # confirmed cumulative charges; adapter must not invent zero
    account_id: str  # verified owner of this order, not a caller-supplied label
    time_basis: str = 'source_offset'
    source_submitted_at: str | None = None
    settlement: str = 'a24horas'  # as IOL reports it, not as it was requested


# A sale sent at 24 h can be recorded by the broker at 48 h. The settlement
# changes when the cash arrives, not the quantity, the price or the charges.
SETTLEMENTS = frozenset({'inmediata', 'a24horas', 'a48horas', 'a72horas'})
LONG_SETTLEMENTS = frozenset({'a48horas', 'a72horas'})
UNSETTLED_DAYS = 10
SHORTFALL_MARGIN = Decimal('0.05')


def unsettled_sales(state, now):
    """Sales at 48/72 h of the last UNSETTLED_DAYS days that IOL may not have paid yet:
    their net cash and their quantities by symbol."""
    since = now - timedelta(days=UNSETTLED_DAYS)
    cash, quantities = Decimal(0), {}
    for o in state['orders'].values():
        if (o['side'] == 'SELL' and o['filled'] and o.get('broker_settlement') in LONG_SETTLEMENTS
                and instant(o['created_at']) >= since):
            cash += money(o['gross']) - money(o['fees'])
            quantities[o['symbol']] = quantities.get(o['symbol'], 0) + o['filled']
    return cash, quantities


def holdings_agree(expected, actual, state, now):
    """Holdings are strict. The one allowance: IOL may still list shares the book sold at
    48/72 h until they settle, up to those shares."""
    _, pending = unsettled_sales(state, now)
    return all(expected.get(s, 0) <= actual.get(s, 0) <= expected.get(s, 0) + pending.get(s, 0)
               for s in set(expected) | set(actual))


def cash_check(state, broker_cash, now):
    """A cash difference never stops the bot or its stops.

    More cash at IOL than in the book is recorded and the book keeps the lower amount.
    Less cash at IOL limits buys to IOL's amount. A shortfall beyond what the book
    explains (sales at 48/72 h of the last days, not yet paid) plus 5% of the book's
    value is too large to trust the book: no buys while it lasts, stops as always.
    The startup reconciliation and every in-session one use this same rule.
    """
    book, broker = money(state['cash']), money(broker_cash)
    unsettled, _ = unsettled_sales(state, now)
    value = (book + sum((money(o['hold']) for o in state['orders'].values()), Decimal(0))
             + sum((money(p['cost']) for p in state['positions'].values()), Decimal(0)))
    shortfall = max(Decimal(0), book - broker)
    allowance = unsettled + SHORTFALL_MARGIN * value
    return dict(book=str(book), broker=str(broker), broker_minus_book=str(broker - book),
                shortfall=str(shortfall), unsettled_sales=str(unsettled), allowance=str(allowance),
                buys_allowed=shortfall <= allowance)


@dataclass(frozen=True)
class AccountView:
    available_cash: str
    positions: dict[str, int]  # all broker holdings, including declared external holdings
    open_order_ids: frozenset[str]
    account_id: str  # broker-verified identity, also used for the shared lease
    cash_basis: str = 'adapter_confirmed'


class Broker(Protocol):
    def submit(self, intent: dict) -> str: ...
    def order(self, broker_id: str) -> OrderView: ...
    def cancel(self, broker_id: str) -> None: ...
    def account(self) -> AccountView: ...


TERMINAL = frozenset({"filled", "cancelled", "rejected"})


class ExecutionController:
    """One owner, one durable state, an explicit reconciliation before each send.

    `cash` is the assigned pilot's free cash, not the whole account balance.
    Buy reservations are deducted before any submission. Existing holdings must
    be explicitly declared managed or external; reconciliation never adopts them.
    """

    def __init__(self, directory: Path, account: str, broker: Broker):
        self.lease = AccountLease(account).acquire()
        self.path = Path(directory) / "execution-state.json"
        self.broker = broker
        self.ready = False
        self.failure = "reconciliation_required"
        self.exits_only, self.quarantined = False, {}
        self.cash = None  # the last reconciliation's cash_check, None until one passes
        self._poisoned = False
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            self._state = envelope["state"]
            if envelope["sha256"] != self._digest(self._state):
                raise ValueError("Execution state checksum mismatch")
            self._validate(self._state)
            if self._state["account_key"] != self.lease.key:
                raise ValueError("Execution state belongs to another account")
        except BaseException:
            self.lease.close()
            raise

    @classmethod
    def prepare(cls, directory: Path, account: str, *, cash, positions=None,
                external_positions=None, fee_reserve="0", provenance=None):
        """Explicit offline seed in a NEW directory. Never reads the broker.

        A missing/corrupt state is not automatically reseeded at startup. For a
        real migration, provenance and quantities must come from a reviewed state.
        """
        with AccountLease(account) as lease:
            state = {"schema": 1, "account_key": lease.key, "cash": str(money(cash)),
                     "positions": deepcopy(positions or {}),
                     "external_positions": deepcopy(external_positions or {}),
                     "fee_reserve": str(money(fee_reserve)), "realized": "0",
                     "orders": {}, "paused": False, "revision": 0, "events": [],
                     "last_reconciliation": None,
                     "provenance": deepcopy(provenance)}
            cls._validate(state)
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=False)
            cls._write(directory / "execution-state.json", state)

    @staticmethod
    def _digest(state):
        raw = json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _validate(state):
        if state.get("schema") != 1 or type(state.get("paused")) is not bool:
            raise ValueError("Invalid execution state schema")
        money(state["cash"])
        money(state["fee_reserve"])
        if not Decimal(state["realized"]).is_finite():
            raise ValueError("Invalid realized result")
        quantity(state["revision"])
        if not isinstance(state["events"], list):
            raise ValueError("Invalid execution audit")
        for symbol, pos in state["positions"].items():
            if not symbol or not isinstance(pos, dict):
                raise ValueError("Invalid position")
            quantity(pos["quantity"], positive=True)
            money(pos["cost"])
        for symbol, qty in state["external_positions"].items():
            if not symbol:
                raise ValueError("Invalid external position")
            quantity(qty, positive=True)
        ids = []
        active_symbols = []
        for local_id, order in state["orders"].items():
            if order["local_id"] != local_id or order["side"] not in ("BUY", "SELL"):
                raise ValueError("Invalid durable order")
            if order["state"] not in TERMINAL | {"submitting", "unknown", "open", "cancel_pending"}:
                raise ValueError("Invalid order state")
            quantity(order["quantity"], positive=True)
            if quantity(order["filled"]) > order["quantity"] or money(order["limit"]) <= 0:
                raise ValueError("Invalid order amounts")
            for field in ("gross", "fees", "hold"):
                money(order[field])
            instant(order["created_at"])
            if order.get('quote_expires_at') is not None:
                instant(order['quote_expires_at'])
            if order["broker_id"] is not None:
                if not isinstance(order["broker_id"], str) or not order["broker_id"]:
                    raise ValueError("Invalid broker identifier")
                ids.append(order["broker_id"])
            if order["state"] not in TERMINAL:
                active_symbols.append(order["symbol"])
            elif money(order["hold"]) != 0:
                raise ValueError("Terminal order still reserves cash")
        if len(ids) != len(set(ids)) or len(active_symbols) != len(set(active_symbols)):
            raise ValueError("Duplicate broker identifier or active symbol")

    @classmethod
    def _write(cls, path, state):
        cls._validate(state)
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"state": state, "sha256": cls._digest(state)}, stream,
                      ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers may temporarily deny delete-sharing. Retry only the
        # identical local atomic replacement, never a broker request or commit.
        for attempt in range(11):
            try:
                os.replace(temp, path)
                break
            except OSError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 10:
                    raise
                time.sleep(0.1)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _check(self):
        self.lease.check()
        if self._poisoned:
            raise ExecutionBlocked("Persistence failed; reopen and reconcile before continuing")

    def _commit(self, state):
        self._check()
        state["revision"] = self._state["revision"] + 1
        changes = []
        for key, order in state["orders"].items():
            prior = self._state["orders"].get(key, {})
            if order != prior:
                changes.append({"local_id": key, "from": prior.get("state"),
                                "to": order["state"], "broker_id": order["broker_id"],
                                "filled": order["filled"], "gross": order["gross"],
                                "fees": order["fees"], "hold": order["hold"]})
        state["events"].append({"revision": state["revision"],
                                "at": datetime.now(timezone.utc).isoformat(),
                                "orders": changes, "paused": state["paused"],
                                "cash": state["cash"],
                                "reconciliation": state["last_reconciliation"]})
        try:
            self._write(self.path, state)
        except BaseException as exc:
            self._poisoned = True
            self.ready = False
            # Codes only: exception text may contain private data. Preserve the
            # original failure even if finally/checkpoint raises ExecutionBlocked.
            try:
                failure = {'at': datetime.now(timezone.utc).isoformat(),
                           'code': type(exc).__name__, 'errno': getattr(exc, 'errno', None),
                           'winerror': getattr(exc, 'winerror', None),
                           'revision': state['revision']}
                with self.path.with_name('persistence-failures.jsonl').open('a', encoding='utf-8') as log:
                    log.write(json.dumps(failure)+'\n'); log.flush(); os.fsync(log.fileno())
            except Exception:
                pass
            raise
        self._state = state

    @property
    def state(self):
        return deepcopy(self._state)

    def close(self):
        self.lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def pause(self, paused=True):
        if type(paused) is not bool:
            raise ValueError("Pause must be boolean")
        state = self.state
        state["paused"] = paused
        self._commit(state)

    def strategy_checkpoint(self, runtime, positions):
        """Persist strategy metadata with the SAME atomic ledger, never a second file.

        This interface cannot edit quantities, costs, cash, orders or reservations.
        """
        state = self.state
        if set(positions) - set(state['positions']):
            raise ValueError('Strategy metadata references an absent position')
        state['strategy'] = deepcopy(runtime)
        for symbol, metadata in positions.items():
            state['positions'][symbol]['swing'] = deepcopy(metadata)
        self._commit(state)

    def reconcile(self) -> bool:
        """A failed read is never recorded as a successful daily sync.

        A single unreadable order must not leave every holding's stop unchecked. An order with a broker id whose read we refuse, or whose cancel is still
        pending, is set aside: the result is still a failure with the same code, but if
        the account agrees on everything else `exits_only` names those orders and
        `submit` accepts a sale of another symbol, never a buy. Unbound orders, an
        unreadable account and any other discrepancy still block everything.
        """
        self._check()
        self.ready, self.failure = False, "reconciliation_required"
        self.exits_only, self.quarantined = False, {}
        self.cash = None
        held, code, cash = {}, None, None
        try:
            for local_id, order in list(self._state["orders"].items()):
                if order["state"] not in TERMINAL and order["broker_id"]:
                    try:
                        self.observe(local_id, self.broker.order(order["broker_id"]))
                    except (ValueError, KeyError, TypeError) as exc:
                        if self._poisoned:
                            raise
                        LOG.error('Order set aside: %s: %s: %s', order["symbol"], type(exc).__name__, str(exc)[:200])
                        held[order["symbol"]] = local_id
                        code = code or type(exc).__name__
            active = [o for o in self._state["orders"].values() if o["state"] not in TERMINAL]
            if any(o["state"] in ("submitting", "unknown") for o in active):
                raise ExecutionBlocked("unresolved_order")
            for o in active:
                if o["state"] == "cancel_pending":
                    held.setdefault(o["symbol"], o["local_id"])
                    code = code or "unresolved_order"
            view = self.broker.account()
            if not isinstance(view, AccountView):
                raise ValueError("Invalid account snapshot")
            if account_key(view.account_id) != self.lease.key:
                raise ExecutionBlocked("broker_account_mismatch")
            expected = dict(self._state["external_positions"])
            for symbol, pos in self._state["positions"].items():
                expected[symbol] = expected.get(symbol, 0) + pos["quantity"]
            actual = {s: quantity(q) for s, q in view.positions.items() if q != 0}
            # A set-aside order may still fill: its symbol is the only one left out.
            if not holdings_agree({s: q for s, q in expected.items() if s not in held},
                                  {s: q for s, q in actual.items() if s not in held},
                                  self._state, datetime.now(timezone.utc)):
                raise ExecutionBlocked("position_discrepancy")
            if not isinstance(view.open_order_ids, frozenset):
                raise ValueError("Invalid open orders snapshot")
            aside = frozenset(self._state["orders"][i]["broker_id"] for i in held.values())
            if view.open_order_ids - aside != frozenset(o["broker_id"] for o in active) - aside:
                raise ExecutionBlocked("open_order_discrepancy")
            # A set-aside order can move broker cash we have not booked (charges on an
            # open order). Sales only add cash; buys wait for a full reconciliation.
            # Less cash at the broker does not refuse the reconciliation, which would
            # leave every stop unchecked. It limits buys instead (cash_check).
            if not held:
                cash = cash_check(self._state, view.available_cash, datetime.now(timezone.utc))
        except Exception as exc:
            if self._poisoned:
                raise
            self.failure = str(exc) if type(exc) is ExecutionBlocked else type(exc).__name__
            # "ValueError" alone would hide which check refused. Our validators' texts carry
            # no account data; broker errors can quote response bodies and stay unnamed.
            if isinstance(exc, (ValueError, KeyError, TypeError)):
                LOG.error('Reconciliation refused: %s: %s', type(exc).__name__, str(exc)[:200])
            self._record_reconciliation(False, self.failure)
            return False
        if held:
            self.failure = code
            self._record_reconciliation(False, code)
            self.exits_only, self.quarantined = True, held
            return False
        # Only a shortfall is written: it is the case that changes what the bot may buy.
        self._record_reconciliation(True, None, cash if Decimal(cash["broker_minus_book"]) < 0 else None)
        self.cash = cash
        self.ready, self.failure = True, None
        return True

    def buying_cash(self) -> Decimal:
        """What a buy may reserve now: the book's cash less IOL's shortfall at the last
        reconciliation (a reservation lowers both alike), nothing while the shortfall
        is too large, nothing before a reconciliation passed."""
        if self.cash is None or not self.cash["buys_allowed"]:
            return Decimal(0)
        return max(Decimal(0), money(self._state["cash"]) - money(self.cash["shortfall"]))

    def _record_reconciliation(self, ok, reason, cash=None):
        state = self.state
        state["last_reconciliation"] = {
            "at": datetime.now(timezone.utc).isoformat(), "ok": ok, "reason": reason}
        if cash is not None:
            state["last_reconciliation"]["cash"] = deepcopy(cash)
        self._commit(state)

    def submit(self, symbol: str, side: str, qty: int, limit, *, now: datetime,
               reason="", quote_expires_at=None) -> str:
        self._check()
        quantity(qty, positive=True)
        price = money(limit)
        if side not in ("BUY", "SELL") or not symbol or price == 0:
            raise ValueError("Invalid order")
        instant(now.isoformat())
        if side == "BUY" and self._state["paused"]:
            raise ExecutionBlocked("New entries are paused")
        if not self.reconcile() and not (side == "SELL" and self.exits_only and symbol not in self.quarantined):
            raise ExecutionBlocked(self.failure)
        if any(o["symbol"] == symbol and o["state"] not in TERMINAL
               for o in self._state["orders"].values()):
            raise ExecutionBlocked("Symbol already has an active order")
        state = self.state
        hold = Decimal(0)
        if side == "BUY":
            if symbol in state["positions"]:
                raise ExecutionBlocked("Increasing existing positions is not enabled")
            hold = qty * price * (1 + money(state["fee_reserve"]))
            if not self.cash["buys_allowed"]:
                raise ExecutionBlocked("available_cash_shortfall")
            if hold > self.buying_cash():
                raise ExecutionBlocked("Insufficient assigned free cash")
            state["cash"] = str(money(state["cash"]) - hold)
        elif qty > state["positions"].get(symbol, {}).get("quantity", 0):
            raise ExecutionBlocked("Sell exceeds managed holdings")
        local_id = uuid4().hex
        order = {"local_id": local_id, "symbol": symbol, "side": side,
                 "quantity": qty, "limit": str(price), "created_at": now.isoformat(),
                 "reason": reason, "state": "submitting", "broker_id": None,
                 "quote_expires_at":quote_expires_at,
                 "filled": 0, "gross": "0", "fees": "0", "hold": str(hold)}
        state["orders"][local_id] = order
        self._commit(state)  # durable intent and reservation BEFORE any side effect
        self.ready = False
        try:
            broker_id = self.broker.submit(deepcopy(order))
            if not isinstance(broker_id, str) or not broker_id.strip():
                raise ValueError("Submission has no broker identifier")
            if any(o["broker_id"] == broker_id for o in self._state["orders"].values()):
                raise ValueError("Broker identifier already assigned")
        except RequestNotSent as exc:
            # Provably never sent: release the reservation and keep the refusal code.
            state = self.state
            state["cash"] = str(money(state["cash"]) + hold)
            state["orders"][local_id].update(state="rejected", hold="0", not_sent=exc.code)
            self._commit(state)
        except BrokerRejected as exc:
            state = self.state
            state["cash"] = str(money(state["cash"]) + hold)
            state["orders"][local_id].update(state="rejected", hold="0")
            if exc.detail:
                # The broker's masked reason, so a refusal can be explained.
                state["orders"][local_id]["broker_message"] = exc.detail
            self._commit(state)
        except Exception as exc:
            # Outcome unknown; the exception type only (its text may hold private data).
            state = self.state
            state["orders"][local_id].update(state="unknown", submit_error=type(exc).__name__)
            self._commit(state)
        else:
            state = self.state
            state["orders"][local_id].update(state="open", broker_id=broker_id)
            self._commit(state)
        return local_id

    def bind_unknown(self, local_id: str, broker_id: str):
        """Explicit identification, never 'latest matching order' or empty search.

        Broker details must match the recorded intent. An absent order remains
        reserved/blocked and needs reviewed broker evidence, not an automatic retry.
        """
        self._check()
        order = self._state["orders"][local_id]
        if order["state"] not in ("submitting", "unknown") or order["broker_id"]:
            raise ExecutionBlocked("Order is not unbound")
        if any(o["broker_id"] == broker_id for o in self._state["orders"].values()):
            raise ExecutionBlocked("Broker identifier already assigned")
        self.observe(local_id, self.broker.order(broker_id), binding=broker_id)

    def observe(self, local_id: str, view: OrderView, *, binding=None):
        self._check()
        self.ready = False
        old = self._state["orders"][local_id]
        if not isinstance(view, OrderView) or view.status not in TERMINAL | {"open"}:
            raise ValueError("Invalid order observation")
        if account_key(view.account_id) != self.lease.key:
            raise ExecutionBlocked("broker_order_account_mismatch")
        expected_id = old["broker_id"] or binding
        if not expected_id or view.broker_id != expected_id:
            raise ValueError("Wrong broker identifier")
        if (view.symbol != old["symbol"] or view.side != old["side"]
                or quantity(view.requested_quantity, positive=True) != old["quantity"]
                or money(view.limit) != money(old["limit"])
                or instant(view.submitted_at) < instant(old["created_at"])):
            raise ValueError("Observation does not match durable intent")
        filled = quantity(view.filled_quantity)
        gross, fees = money(view.gross), money(view.fees)
        if filled > old["quantity"] or filled < old["filled"]:
            raise ValueError("Invalid cumulative fill quantity")
        if (gross < money(old["gross"]) or fees < money(old["fees"])
                or (filled == 0 and (gross != 0 or fees != 0))
                or (filled > 0 and gross <= 0)):
            raise ValueError("Invalid cumulative fill amounts")
        if view.status == "filled" and filled != old["quantity"]:
            raise ValueError("Filled status without confirmed full quantity")
        if view.status == "rejected" and filled:
            raise ValueError("Rejected order has fills")
        if old["state"] in TERMINAL:
            if (view.status, filled, gross, fees) != (old["state"], old["filled"], money(old["gross"]), money(old["fees"])):
                raise ValueError("Terminal order changed; review required")
            return
        delta_qty = filled - old["filled"]
        delta_gross, delta_fees = gross - money(old["gross"]), fees - money(old["fees"])
        # A broker can show a partial fill without charges and post them when the order
        # ends. Not a correction.
        closing_fees = delta_qty == 0 and delta_gross == 0 and view.status in TERMINAL and filled > 0
        if delta_qty == 0 and (delta_gross != 0 or delta_fees != 0) and not closing_fees:
            raise ValueError("Fill correction requires explicit review")
        if delta_qty and delta_gross <= 0:
            raise ValueError("Fill without executed amount")
        if delta_qty:
            limit_amount = delta_qty * money(old["limit"])
            if ((old["side"] == "BUY" and delta_gross > limit_amount)
                    or (old["side"] == "SELL" and delta_gross < limit_amount)):
                raise ValueError("Execution price violates the recorded limit")
        state = self.state
        order = state["orders"][local_id]
        symbol = order["symbol"]
        new_hold = Decimal(0)
        if order["side"] == "BUY":
            if view.status not in TERMINAL:
                new_hold = (order["quantity"] - filled) * money(order["limit"]) * (1 + money(state["fee_reserve"]))
            cash = money(state["cash"]) + money(order["hold"]) - new_hold - delta_gross - delta_fees
            if cash < 0:
                raise ExecutionBlocked("Confirmed cost exceeds assigned funds; review required")
            state["cash"] = str(cash)
            if delta_qty:
                pos = state["positions"].setdefault(symbol, {"quantity": 0, "cost": "0", "opened_at": view.submitted_at})
                pos["quantity"] += delta_qty
                pos["cost"] = str(money(pos["cost"]) + delta_gross + delta_fees)
            elif delta_fees:
                if symbol not in state["positions"]:
                    raise ValueError("Closing charges for an absent position")
                pos = state["positions"][symbol]
                pos["cost"] = str(money(pos["cost"]) + delta_fees)
        elif delta_fees and not delta_qty:
            cash = money(state["cash"]) - delta_fees
            if cash < 0:
                raise ExecutionBlocked("Fees exceed assigned funds")
            state["cash"] = str(cash)
            state["realized"] = str(Decimal(state["realized"]) - delta_fees)
            order['realized'] = str(Decimal(order.get('realized', '0')) - delta_fees)
        elif delta_qty:
            pos = state["positions"][symbol]
            if delta_qty > pos["quantity"]:
                raise ExecutionBlocked("Confirmed sale exceeds managed position")
            cost = money(pos["cost"]) * delta_qty / pos["quantity"]
            net = delta_gross - delta_fees
            cash = money(state["cash"]) + net
            if cash < 0:
                raise ExecutionBlocked("Fees exceed assigned funds")
            state["cash"] = str(cash)
            state["realized"] = str(Decimal(state["realized"]) + net - cost)
            order['realized'] = str(Decimal(order.get('realized', '0')) + net - cost)
            pos["cost"] = str(money(pos["cost"]) - cost)
            pos["quantity"] -= delta_qty
            if pos["quantity"] == 0:
                del state["positions"][symbol]
        status = "cancel_pending" if old["state"] == "cancel_pending" and view.status == "open" else view.status
        order.update(broker_id=expected_id, state=status, filled=filled,
                     gross=str(gross), fees=str(fees), hold=str(new_hold),
                     broker_time_basis=view.time_basis, broker_source_submitted_at=view.source_submitted_at,
                     broker_settlement=view.settlement)
        self._commit(state)

    def cancel(self, local_id: str):
        self._check()
        order = self._state["orders"][local_id]
        if order["state"] in TERMINAL:
            return
        if not order["broker_id"]:
            raise ExecutionBlocked("Cannot cancel an unidentified order")
        self.observe(local_id, self.broker.order(order["broker_id"]))
        order = self._state["orders"][local_id]
        if order["state"] in TERMINAL or order["state"] == "cancel_pending":
            return
        state = self.state
        state["orders"][local_id]["state"] = "cancel_pending"
        self._commit(state)
        try:
            self.broker.cancel(order["broker_id"])
        except Exception:
            return  # reservation persists; no repeated DELETE without resolution
        self.observe(local_id, self.broker.order(order["broker_id"]))
