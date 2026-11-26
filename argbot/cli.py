"""Command line: paper trading by default, offline lab tools, and a gated live mode."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

# Resolved from the checkout, like DEFAULT_SETTINGS in argbot/lab/settings.py, so `argbot paper`
# works from any folder after `pip install -e .`.
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs/examples/strategy.example.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="argbot", description="Educational trading bot for the Argentine market")
    sub = parser.add_subparsers(dest="command", required=True)

    paper = sub.add_parser("paper", help="Simulate sessions against the paper broker (no network, no account)")
    paper.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    paper.add_argument("--out", type=Path, required=True, help="New, empty output folder")
    paper.add_argument("--days", type=int, default=3)
    paper.add_argument("--seed", type=int, default=7)
    paper.add_argument("--session", type=date.fromisoformat, default=None, help="First session (YYYY-MM-DD)")

    check = sub.add_parser("check-config", help="Validate a strategy configuration (offline)")
    check.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    lab = sub.add_parser("lab", help="Offline research tools over CSV market data")
    lab_sub = lab.add_subparsers(dest="lab_command", required=True)
    compare = lab_sub.add_parser("compare", help="Compare experiment profiles over one dataset")
    compare.add_argument("--data", type=Path, required=True)
    compare.add_argument("--configs", type=Path, nargs="+", required=True)
    compare.add_argument("--out", type=Path, required=True)
    compare.add_argument("--capital", type=float, default=200000)
    compare.add_argument("--events", type=Path)
    compare.add_argument("--reference", type=Path)
    compare.add_argument("--inactivity-sessions", type=int, default=3)
    validate = lab_sub.add_parser("validate-settings", help="Validate operating limits")
    validate.add_argument("--settings", type=Path)
    simulate = lab_sub.add_parser("simulate-settings", help="Replay a dataset with operating limits")
    simulate.add_argument("--settings", type=Path)
    simulate.add_argument("--data", type=Path, required=True)
    simulate.add_argument("--out", type=Path, required=True)
    simulate.add_argument("--capital", type=float, default=200000)
    simulate.add_argument("--reference", type=Path)
    imp = lab_sub.add_parser("import-market", help="Convert your own tick CSVs into a lab dataset")
    imp.add_argument("--source", type=Path, required=True)
    imp.add_argument("--out", type=Path, required=True)
    imp.add_argument("--start", type=date.fromisoformat, required=True)
    imp.add_argument("--end", type=date.fromisoformat, required=True)
    imp.add_argument("--spread-bps", type=float, required=True, help="Total spread assumption in basis points")
    imp.add_argument("--symbols", nargs="+", required=True)
    imp.add_argument("--groups", type=Path, help="JSON symbol -> sector")

    live = sub.add_parser("live", help="LIVE trading with IOL. Requires LIVE_TRADING=true and a typed confirmation")
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--state-dir", type=Path, required=True)

    watch = sub.add_parser("watch-positions", help="Read-only terminal dashboard for local state (no broker, no LLM)")
    watch.add_argument("--state-dir", type=Path, required=True, help="Folder containing execution-state.json")
    watch.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    watch.add_argument("--once", action="store_true", help="Print once and exit")
    watch.add_argument("--interval", type=int, default=10, help="Refresh interval in seconds (minimum 5)")

    mon = sub.add_parser("monitor", help="Optional technical monitor: off by default, observe-only, uses the Claude Code CLI")
    mon.add_argument("--state-dir", type=Path, required=True, help="Folder containing execution-state.json")
    mon.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="The strategy config the bot runs with")
    mon.add_argument("--policy", type=Path, default=None,
                     help='Monitor policy with "enabled": true (default: the disabled example)')
    mon.add_argument("--out", type=Path, default=None, help="Report folder (default: runs/monitor/<day>, gitignored)")
    mon.add_argument("--once", action="store_true", help="One check now, then exit")
    mon.add_argument("--as-of-heartbeat", action="store_true",
                     help="With --once: observe the state at its last heartbeat (finished paper runs)")
    mon.add_argument("--dry-run", action="store_true",
                     help="With --once: write the summary that would be sent and call no model")

    demo = sub.add_parser("advisor-demo", help="Advisor v1 contract against a fictional answer (no model, no network)")
    demo.add_argument("--output", type=Path, required=True, help="New folder for request, fixture and result")

    adv = sub.add_parser("advisor", help="Optional shadow advisor: off by default, never sends orders, "
                                         "sends symbols and public market data to the Claude Code CLI")
    adv.add_argument("--state-dir", type=Path, required=True, help="Folder containing execution-state.json")
    adv.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="The strategy config the bot runs with")
    adv.add_argument("--policy", type=Path, default=None,
                     help='Advisor policy with "enabled": true (default: the disabled example)')
    adv.add_argument("--authorization", type=Path, required=True, help="Your consent file for today's session")
    adv.add_argument("--out", type=Path, default=None, help="Report folder (default: runs/advisor/<day>, gitignored)")

    args = parser.parse_args(argv)
    if args.command == "paper":
        from argbot.paper_session import DEFAULT_SESSION, run_paper_session
        report = run_paper_session(args.config, args.out, session=args.session or DEFAULT_SESSION,
                                   days=args.days, seed=args.seed)
        print(json.dumps(report, indent=2))
    elif args.command == "check-config":
        from argbot.paper_session import load_config
        from argbot.strategy.candidate import validate_config
        cfg = load_config(args.config)
        cfg.setdefault("account_id", None)
        if cfg.get("account_id") is None or cfg.get("assigned_capital") is None:
            print("Note: account_id and assigned_capital are empty; filling placeholders to check the rest.")
            cfg["account_id"] = cfg["account_id"] or "PLACEHOLDER"
            cfg["assigned_capital"] = cfg["assigned_capital"] or "1"
        validate_config(cfg)
        print(json.dumps({"valid": True, "symbols": len(cfg["universe"])}, indent=2))
    elif args.command == "lab":
        _lab(args)
    elif args.command == "live":
        from argbot.live import run_live_iol
        run_live_iol(args.config, args.state_dir)
    elif args.command == "watch-positions":
        from argbot.watch_positions import watch
        watch(args.state_dir, args.config, once=args.once, interval=args.interval)
    elif args.command == "monitor":
        from argbot.agents import monitor
        if (args.dry_run or args.as_of_heartbeat) and not args.once:
            parser.error("--dry-run and --as-of-heartbeat need --once")
        policy_path = args.policy or monitor.DEFAULT_POLICY
        try:
            if args.once:
                text, folder = monitor.once(args.state_dir, args.config, policy_path, out=args.out,
                                            as_of_heartbeat=args.as_of_heartbeat, dry_run=args.dry_run)
                print(text)
                print(f"\nReport folder: {folder}")
            else:
                monitor.watch(args.state_dir, args.config, policy_path, out=args.out)
        except monitor.MonitorDisabled as exc:
            print(exc, file=sys.stderr)
            return 2
    elif args.command == "advisor-demo":
        from datetime import datetime, timezone
        from argbot.agents.advisor import demo
        demo(args.output, now=datetime.now(timezone.utc))
        print("Advisor contract checked against a fictional answer. No inference, no data sent, no orders.")
    elif args.command == "advisor":
        from argbot.agents import advisor_runner
        try:
            advisor_runner.watch(args.state_dir, args.config, args.policy or advisor_runner.DEFAULT_POLICY,
                                 args.authorization, out=args.out)
        except advisor_runner.AdvisorDisabled as exc:
            print(exc, file=sys.stderr)
            return 2
    return 0


def _lab(args) -> None:
    if args.lab_command in {"validate-settings", "simulate-settings"}:
        from argbot.lab.settings import DEFAULT_SETTINGS, load_settings
        limits = load_settings(args.settings or DEFAULT_SETTINGS)
        if args.lab_command == "validate-settings":
            result = {"valid": True, "settings": limits.snapshot(), "network_requests": 0}
        else:
            from argbot.lab.config import Experiment
            from argbot.lab.replay import run
            result = run(args.data, Experiment(name="user_settings"), args.out,
                         capital=args.capital, reference_path=args.reference, limits=limits)
        print(json.dumps(result, indent=2, default=str, allow_nan=False))
    elif args.lab_command == "compare":
        from argbot.lab.config import load_config
        from argbot.lab.replay import compare
        result = compare(args.data, [load_config(p) for p in args.configs], args.out, args.capital, args.events,
                         reference_path=args.reference, inactivity_sessions=args.inactivity_sessions)
        print(json.dumps([{"profile": r["profile"], "return_pct": round(r["return_pct"], 2),
                           "closed_trades": r["closed_trades"]} for r in result], indent=2))
    else:
        from argbot.lab.data import import_market
        groups = json.loads(args.groups.read_text(encoding="utf-8")) if args.groups else None
        result = import_market(args.source, args.out, start=args.start, end=args.end,
                               spread_bps=args.spread_bps, symbols=args.symbols, groups=groups)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
