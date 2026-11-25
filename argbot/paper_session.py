"""Run one simulated session of the example strategy against the paper broker."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

from argbot.brokers.paper import PAPER_ACCOUNT, PaperBroker, PaperMarket, SyntheticExchange
from argbot.execution import ExecutionController
from argbot.sessions import is_trading_day, trading_day_before
from argbot.strategy.candidate import Candidate, session_end

DEFAULT_SESSION = date(2026, 10, 14)


def load_config(path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def run_paper_session(config_path, output, *, session: date = DEFAULT_SESSION, days: int = 1,
                      seed: int = 7, step_minutes: int = 5) -> dict:
    """Simulate `days` consecutive sessions from `session`. Writes report.json in `output`."""
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"{output} is not empty; choose a new output folder")
    if not is_trading_day(session):
        raise ValueError("The paper session must be a trading day in the reviewed calendar")
    if not 1 <= days <= 20:
        raise ValueError("days must be between 1 and 20")
    cfg = load_config(config_path)
    cfg.update(status="synthetic", account_id=PAPER_ACCOUNT)
    if cfg.get("assigned_capital") is None:
        cfg["assigned_capital"] = "1000000"
    exchange = SyntheticExchange(cfg["universe"], session=session, seed=seed, cash=cfg["assigned_capital"])
    ExecutionController.prepare(output, cfg["account_id"], cash=cfg["assigned_capital"],
                                fee_reserve=cfg["fee_reserve"])
    rounds, sessions = 0, []
    with ExecutionController(output, cfg["account_id"], PaperBroker(exchange)) as control:
        market = PaperMarket(exchange)
        end = session_end(cfg)
        for index in range(days):
            day = exchange.session
            bot = Candidate(control, market, cfg, clock=exchange.clock, quote_sleep=lambda _s: None)
            bot.start_session(day, trading_day_before(day), clock_evidence={
                "session": day.isoformat(), "checked_at": exchange.now.isoformat(), "samples": 3,
                "offset_seconds": "0", "source": "paper_simulation_not_an_ntp_measurement"})
            while exchange.now.time() < end:
                bot.cycle()
                rounds += 1
                exchange.advance(step_minutes)
            sessions.append(day.isoformat())
            if index + 1 < days:
                exchange.roll(next_trading_day(day))
        state = control.state
        orders = list(state["orders"].values())
        holds = sum((Decimal(o["hold"]) for o in orders), Decimal(0))
        marks = {s: Decimal(str(exchange.symbols[s].price)) for s in state["positions"]}
        equity = Decimal(state["cash"]) + holds + sum(
            (p["quantity"] * marks[s] for s, p in state["positions"].items()), Decimal(0))
        report = {
            "kind": "paper_session",
            "sessions": sessions,
            "seed": seed,
            "rounds": rounds,
            "symbols": len(cfg["universe"]),
            "orders": len(orders),
            "buys_filled": sum(o["side"] == "BUY" and o["filled"] > 0 for o in orders),
            "sells_filled": sum(o["side"] == "SELL" and o["filled"] > 0 for o in orders),
            "open_positions": sorted(state["positions"]),
            "realized": str(Decimal(state["realized"]).quantize(Decimal("0.01"))),
            "starting_capital": str(cfg["assigned_capital"]),
            "ending_equity_marked": str(equity.quantize(Decimal("0.01"))),
            "book_matches_paper_broker": {s: p["quantity"] for s, p in state["positions"].items()}
                == {s: q for s, q in exchange.positions.items() if q},
            "incidents": len(state["strategy"].get("incidents") or []),
            "real_network_requests": 0,
            "real_orders": 0,
        }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def next_trading_day(day: date) -> date:
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day
