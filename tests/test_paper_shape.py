"""Frozen shape of a paper run: what the bot records and reports.

A change to any set below is a visible change to what the bot writes, so it must be
deliberate (update the set in the same commit). No fixed digest: values may change,
the shape may not change by accident.
"""
import json
from pathlib import Path

import pytest

from argbot.paper_session import run_paper_session

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/examples/strategy.example.json"
ENVELOPE_KEYS = {"sha256", "state"}
STATE_KEYS = {"account_key", "cash", "events", "external_positions", "fee_reserve", "last_reconciliation", "orders",
              "paused", "positions", "provenance", "realized", "revision", "schema", "strategy"}
RUNTIME_KEYS = {
    "broker_rejected_today", "clock_evidence", "closed_orders", "closed_today", "config_hash", "cooldowns",
    "day", "entries_blocked", "equity", "exclusions", "exits_only", "heartbeat_at", "history_diagnostics",
    "history_exclusions", "incidents", "last_cycle", "last_cycle_diagnostics", "managed_book_valued",
    "managed_data_gap_since", "managed_quote_refreshes", "opening_equity", "opening_equity_at",
    "opening_equity_basis", "opening_equity_marks", "opening_ledger", "opportunity_scan_cursor", "quotes",
    "risk_paused", "shortlist", "status", "stop_checked_cycle", "stopped_at", "telemetry_ok", "valuation_at",
    "valuation_available", "vwap", "windows",
}
# Every audit row of a phase has exactly these top-level keys.
ROW_KEYS = {
    "started": {"cycle_id", "phase", "schema", "session", "started_at"},
    "completed": {"completed_at", "cycle_id", "entries_blocked", "http_budget", "managed_quote_refreshes",
                  "observations", "phase", "risk_paused", "schema", "session", "started_at", "status",
                  "valuation_available"},
}
AUDIT_KINDS = {"market_mark", "order_submitted", "quote_received", "risk_check", "send_check", "stop_check",
               "valuation"}
REPORT_KEYS = {"book_matches_paper_broker", "buys_filled", "ending_equity_marked", "incidents", "kind",
               "open_positions", "orders", "real_network_requests", "real_orders", "realized", "rounds", "seed",
               "sells_filled", "sessions", "starting_capital", "symbols"}
# With "advisor": {"capture_enabled": true}: one more runtime key, two more kinds, and this batch.
CAPTURE_KINDS = {"advisor_batch", "advisor_reference"}
BATCH_KEYS = {"candidates", "captured_at", "coalesced_windows", "comparison", "excluded", "mechanical_candidates",
              "schema", "window"}


@pytest.mark.parametrize("capture", [False, True])
def test_paper_run_records_the_frozen_shape(tmp_path, capture):
    config = EXAMPLE
    if capture:
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cfg["advisor"] = {"capture_enabled": True}
        config = tmp_path / "capture.json"
        config.write_text(json.dumps(cfg), encoding="utf-8")
    out = tmp_path / "run"
    run_paper_session(config, out, days=1)
    envelope = json.loads((out / "execution-state.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (out / "operational-cycles.private.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    runtime = envelope["state"]["strategy"]
    assert set(envelope) == ENVELOPE_KEYS and set(envelope["state"]) == STATE_KEYS
    assert {r["phase"] for r in rows} == set(ROW_KEYS)
    assert all(set(r) == ROW_KEYS[r["phase"]] for r in rows)
    assert set(report) == REPORT_KEYS
    kinds = {o["kind"] for r in rows for o in r.get("observations", [])}
    if capture:
        assert set(runtime) == RUNTIME_KEYS | {"advisor_batch"}
        assert kinds == AUDIT_KINDS | CAPTURE_KINDS  # no advisor_capture_failed / advisor_reference_failed
        assert set(runtime["advisor_batch"]) == BATCH_KEYS
    else:
        assert set(runtime) == RUNTIME_KEYS
        assert kinds == AUDIT_KINDS
