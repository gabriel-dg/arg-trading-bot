"""Live trading with a real broker account. Off by default and gated twice.

1. The environment variable LIVE_TRADING must be exactly "true".
2. The operator must type the confirmation phrase at an interactive prompt.

Nothing here is financial advice. Live trading can lose money; you are solely
responsible for any use. Read README.md ("Going live") before considering it.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import date, datetime, timezone
import json
import logging
import os
import sys
import time as timing
from pathlib import Path

LIVE_ENV = "LIVE_TRADING"
CONFIRMATION = "I UNDERSTAND THIS TRADES REAL MONEY"


class LiveTradingDisabled(SystemExit):
    def __init__(self, message: str):
        super().__init__(message)


def require_live_opt_in(*, environ=None, ask=input, interactive=None) -> None:
    """Both gates or nothing. There is no flag that skips the typed confirmation."""
    environ = os.environ if environ is None else environ
    if environ.get(LIVE_ENV) != "true":
        raise LiveTradingDisabled(f"Live trading is disabled. Set {LIVE_ENV}=true to enable it "
                                  "(paper mode needs nothing).")
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if not interactive:
        raise LiveTradingDisabled("Live trading needs an interactive terminal for the confirmation.")
    answer = ask(f'Type "{CONFIRMATION}" to continue: ')
    if answer.strip() != CONFIRMATION:
        raise LiveTradingDisabled("Confirmation did not match. Nothing was sent.")


def run_live_iol(config_path: Path, state_dir: Path, *, environ=None, ask=input, interactive=None,
                 today: date | None = None) -> None:
    """One admitted session against IOL. Credentials come from IOL_USERNAME / IOL_PASSWORD."""
    require_live_opt_in(environ=environ, ask=ask, interactive=interactive)
    import httpx

    from argbot.brokers.iol.adapter import IOLExecutionBroker, NoReplayIOLClient
    from argbot.brokers.iol.budget import BudgetTransport, SessionBudget
    from argbot.brokers.iol.market import IOLMarket
    from argbot.brokers.iol.readers import IOLReaders
    from argbot.clock import ExchangeClock
    from argbot.execution import ExecutionBlocked, ExecutionController
    from argbot.ntp import measure_clock
    from argbot.paper_session import load_config
    from argbot.sessions import is_trading_day, trading_day_before
    from argbot.strategy.candidate import ART, Candidate, session_end, validate_config

    from argbot.brokers.iol.auth import IOLAuth
    from argbot.logging_config import configure_live_logging

    configure_live_logging()
    # HTTP libraries and the IOL adapters must not print bodies or tokens.
    for name in ("argbot.brokers.iol.auth", "argbot.brokers.iol.client", "httpx", "httpcore"):
        logging.getLogger(name).disabled = True
    cfg = load_config(config_path)
    validate_config(cfg)
    day = today or datetime.now(timezone.utc).astimezone(ART).date()
    if not is_trading_day(day):
        raise ExecutionBlocked("Not a trading day in the reviewed calendar")
    previous = trading_day_before(day)
    state_dir = Path(state_dir)
    if not (state_dir / "execution-state.json").exists():
        ExecutionController.prepare(state_dir, cfg["account_id"], cash=cfg["assigned_capital"],
                                    external_positions=cfg.get("external_positions") or {},
                                    fee_reserve=cfg["fee_reserve"])
    raw_clock = measure_clock()
    offsets = raw_clock["offsets_seconds"]
    if len(offsets) != 3 or any(abs(x) > 10 for x in offsets):
        raise ExecutionBlocked("Initial clock rejected")
    evidence = dict(raw_clock, session=day.isoformat(), samples=3, offset_seconds=max(offsets, key=abs))
    (state_dir / "clock.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    pc_clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    exchange = ExchangeClock(pc_clock, timing.monotonic, measure=measure_clock)
    with ExecutionController(state_dir, cfg["account_id"], None) as controller, ExitStack() as stack:
        budget = SessionBudget(controller, day, cfg["http_session_budget"], cfg["http_management_reserve"])
        end = session_end(cfg)
        transport = BudgetTransport(httpx.HTTPTransport(retries=0), budget,
                                    before_send=lambda r: guard_request(r, controller, day, quote_now=exchange.now,
                                                                        end=end),
                                    quote_timeout_seconds=cfg.get("quote_timeout_seconds"))
        http = stack.enter_context(httpx.Client(transport=transport, timeout=25, follow_redirects=False))
        auth = IOLAuth(env="production", http_client=http)
        client = NoReplayIOLClient(auth=auth, http_client=http)
        readers = IOLReaders(naive_order_timezone=cfg["broker_order_timezone"])
        controller.broker = IOLExecutionBroker(client, read_order=readers.order, read_account=readers.account)
        account = controller.broker.account()
        if account.account_id != cfg["account_id"]:
            raise ExecutionBlocked("account_id in the configuration does not match the broker account")
        bot = Candidate(controller, IOLMarket(client, budget), cfg, clock=pc_clock, exchange=exchange)
        bot.start_session(day, previous, clock_evidence=evidence)
        if not controller.reconcile() and not controller.exits_only:
            raise ExecutionBlocked("Startup reconciliation failed")
        bot.run_session(stop_requested=lambda: (state_dir / ".stop").exists())


def guard_request(request, controller, day, *, now=None, quote_now=None, end=None):
    """Last check before any order mutation leaves the process.

    Mutations only inside the admitted session, and a POST only while its quote is
    still within the age limit, judged on the same exchange clock as the quote.
    """
    from datetime import time

    from argbot.execution import QUOTE_EXPIRED, ExecutionBlocked, instant
    from argbot.strategy.candidate import ART

    if request.method in {"GET", "HEAD", "OPTIONS"} or request.url.path == "/token":
        return
    current = (now or datetime.now(timezone.utc)).astimezone(ART)
    if current.date() != day or not time(10, 30) <= current.time() < (end or time(17)):
        raise ExecutionBlocked("Mutation outside admitted session")
    if request.method == "POST":
        orders = [o for o in controller.state["orders"].values() if o["state"] == "submitting"]
        quote_time = quote_now() if quote_now else current
        if (len(orders) != 1 or not orders[0].get("quote_expires_at")
                or quote_time > instant(orders[0]["quote_expires_at"])):
            raise ExecutionBlocked(QUOTE_EXPIRED)
