"""Optional advisor capture in the candidate: off by default, local only.

Fake market and broker, no credentials or network, and no model call anywhere.
"""
import json
from pathlib import Path
import re

import pytest

from test_operational_candidate import Broker, Clock, Market, config, rig, start  # noqa: F401
from test_operational_quote_refresh import one_position
from argbot.execution import ExecutionController
from argbot.paper_session import run_paper_session
from argbot.strategy.candidate import Candidate, validate_config

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/examples/strategy.example.json"


@pytest.fixture
def capture_rig(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    root = tmp_path / "capture"  # apart from `rig`, which some tests also use
    cfg = config(str(root))
    cfg["advisor"] = {"capture_enabled": True}
    clock = Clock()
    market = Market(clock)
    broker = Broker(cfg)
    folder = root / "candidate"
    ExecutionController.prepare(folder, cfg["account_id"], cash=cfg["assigned_capital"], fee_reserve=cfg["fee_reserve"])
    controller = ExecutionController(folder, cfg["account_id"], broker)
    bot = Candidate(controller, market, cfg, clock=clock)
    start(bot, clock)
    yield bot, controller, broker, market, clock, cfg, folder
    controller.close()


def advisor_rows(folder):
    path = Path(folder) / "operational-cycles.private.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [o["kind"] for r in rows for o in r.get("observations", []) if o["kind"].startswith("advisor")]


def count_saves(monkeypatch):
    saves = []
    real = Candidate._save

    def counted(self, *a, **kw):
        saves.append(1)
        return real(self, *a, **kw)
    monkeypatch.setattr(Candidate, "_save", counted)
    return saves


def test_off_by_default_adds_no_key_no_audit_row_and_runs_no_hook(rig, monkeypatch):
    bot, c, broker, market, clock, cfg, folder = rig
    assert "advisor" not in cfg
    for name in ("_capture_advisor", "_observe_blocked_entries"):
        monkeypatch.setattr(Candidate, name, lambda *a, **kw: pytest.fail("hook ran while off"))
    monkeypatch.setattr("argbot.agents.advisor_feed.capture_batch", lambda *a, **kw: pytest.fail("capture while off"))
    bot.cycle()
    assert broker.sent
    assert "advisor_batch" not in bot.runtime and "advisor_batch" not in c.state["strategy"]
    assert advisor_rows(folder) == []


def test_capture_adds_no_extra_checkpoint(rig, capture_rig, monkeypatch):
    saves = count_saves(monkeypatch)
    rig[0].cycle()
    off = len(saves)
    capture_rig[0].cycle()
    assert len(saves) - off == off and len(rig[2].sent) == len(capture_rig[2].sent)


def test_candidate_publishes_batch_before_first_order_without_calling_model(capture_rig, monkeypatch):
    bot, c, broker, market, clock, cfg, folder = capture_rig
    before = broker.submit

    def submit(order):
        saved = json.loads(c.path.read_text())["state"]["strategy"]
        assert saved["windows"] == ["11:20"] and saved["advisor_batch"]["window"] == "11:20"
        assert saved["advisor_batch"]["candidates"]
        return before(order)
    monkeypatch.setattr(broker, "submit", submit)
    bot.cycle()
    assert broker.sent and len(market.calls) == 30
    batch = c.state["strategy"]["advisor_batch"]
    assert batch["mechanical_candidates"]
    for row in batch["candidates"]:
        assert set(row) == {"symbol", "source_at", "drawdown_fraction", "day_range_fraction",
                            "price_to_vwap", "spread_fraction"}
    kinds = advisor_rows(folder)
    assert "advisor_batch" in kinds and "advisor_reference" in kinds


def test_advisor_capture_failure_cannot_change_orders_or_block_entries(rig, capture_rig, monkeypatch):
    def failed(*a, **kw):
        raise RuntimeError("fixture")
    monkeypatch.setattr("argbot.agents.advisor_feed.capture_batch", failed)
    rig[0].cycle()
    bot, c, broker, market, clock, cfg, folder = capture_rig
    bot.cycle()
    assert len(broker.sent) == len(rig[2].sent) and bot.runtime["entries_blocked"] is None
    assert bot.runtime["advisor_batch"] is None
    assert not bot.runtime["incidents"]
    assert "advisor_capture_failed" in advisor_rows(folder)


def test_blocked_portfolio_still_captures_fresh_shadow_candidates_without_orders(capture_rig):
    s = one_position(capture_rig)
    bot, c, b, m, clock, cfg, _ = capture_rig
    before = len(b.sent)
    m.bad.add(s)
    bot.runtime["advisor_batch"] = None
    bot.cycle()
    assert bot.runtime["entries_blocked"] == "managed_quote_missing"
    assert len(b.sent) == before and "13:45" not in bot.runtime["windows"]
    batch = bot.runtime["advisor_batch"]
    assert batch and batch["candidates"]
    assert s not in {row["symbol"] for row in batch["candidates"]}
    assert batch["window"] == "13:45"


def test_blocked_shadow_empty_data_does_not_consume_advisor_attempt(capture_rig):
    one_position(capture_rig)
    bot, c, b, m, clock, cfg, _ = capture_rig
    bot.runtime["advisor_batch"] = None
    m.bad.update(cfg["universe"])
    bot.cycle()
    assert bot.runtime["advisor_batch"] is None and "13:45" not in bot.runtime["windows"]


@pytest.mark.parametrize("block,valid", [
    (None, True), ({"capture_enabled": False}, True), ({"capture_enabled": True}, True), ({}, True),
    ({"capture_enabled": "true"}, False), ({"capture_enabled": 1}, False), ({"enabled": True}, False), (True, False),
])
def test_only_a_boolean_capture_flag_is_accepted(block, valid):
    cfg = config("synthetic-account")
    if block is not None:
        cfg["advisor"] = block
    if valid:
        validate_config(cfg)
    else:
        with pytest.raises(ValueError, match="advisor"):
            validate_config(cfg)


def normalized(folder):
    """Paper outputs with the per-run values removed: uuids, wall-clock UTC times and the state digest."""
    result = {}
    for path in sorted(Path(folder).iterdir()):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"\b[0-9a-f]{32}\b", "<uuid>", text)
        text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00", "<wall>", text)
        text = re.sub(r'"sha256": "[0-9a-f]{64}"', '"sha256": "<digest>"', text)
        result[path.name] = text
    return result


def test_paper_run_with_capture_off_is_byte_identical_to_a_run_where_the_hook_cannot_run(tmp_path, monkeypatch):
    run_paper_session(EXAMPLE, tmp_path / "plain", days=1)
    for name in ("_capture_advisor", "_observe_blocked_entries"):
        monkeypatch.setattr(Candidate, name, lambda *a, **kw: pytest.fail("hook ran while off"))
    monkeypatch.setattr("argbot.agents.advisor_feed.capture_batch", lambda *a, **kw: pytest.fail("capture while off"))
    run_paper_session(EXAMPLE, tmp_path / "guarded", days=1)
    plain, guarded = normalized(tmp_path / "plain"), normalized(tmp_path / "guarded")
    assert plain == guarded
    assert "advisor" not in plain["execution-state.json"] and "advisor" not in plain["operational-cycles.private.jsonl"]


def test_paper_run_with_capture_on_keeps_the_batch_local(tmp_path):
    cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    cfg["advisor"] = {"capture_enabled": True}
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    run_paper_session(path, tmp_path / "run", days=1)
    state = json.loads((tmp_path / "run" / "execution-state.json").read_text(encoding="utf-8"))["state"]
    assert "advisor_batch" in state["strategy"]
    assert "advisor_batch" in advisor_rows(tmp_path / "run")
