"""Optional monitor: technical-summary privacy, scheduling and fail-visible behavior.

Every value here is synthetic. No test calls a real model: a fake runner stands in
for the Claude Code CLI.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from argbot.agents import claude_cli
from argbot.agents import monitor as m
from argbot.execution import ExecutionController

ACCOUNT = "FICTIONAL_ACCOUNT"
EXAMPLE_POLICY = Path(__file__).resolve().parents[1] / "configs/examples/monitor.example.json"


@pytest.fixture
def enabled(tmp_path):
    p = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    p["enabled"] = True
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps(p), encoding="utf-8")
    return path


@pytest.fixture
def recording(tmp_path):
    now = datetime(2026, 10, 1, 11, tzinfo=m.ART)
    folder = tmp_path / "state"
    cfg = dict(account_id=ACCOUNT, poll_seconds=300, max_quote_age_seconds=60)
    ExecutionController.prepare(folder, ACCOUNT, cash="9876543", positions={"SYNA": dict(quantity=7654, cost="876543")},
                                fee_reserve=".01")
    path = folder / "execution-state.json"
    state = m.read(path)["state"]
    state["strategy"] = dict(
        day=now.date().isoformat(), status="running", heartbeat_at=now.isoformat(),
        last_cycle=now.isoformat(), equity="654321", opening_equity="765432", risk_paused=False,
        quotes={"SYNA": dict(source_at=(now - timedelta(seconds=150)).isoformat(), bid="12345")},
        incidents=[dict(at=now.isoformat(), code="PASSWORDVALUE", symbol="SYNA")],
        exclusions={"SYNB": "API error includes secret 12345"})
    state["last_reconciliation"] = dict(ok=True, at=now.isoformat(), reason=None)
    state["http_budget"] = dict(used=100, total=5000, reserve=500, password_grants=1, market_data=80)
    ExecutionController._write(path, state)
    return path, cfg, now


def rewrite(path, **strategy):
    state = m.read(path)["state"]
    state["strategy"].update(strategy)
    ExecutionController._write(path, state)
    return state


def test_summary_excludes_private_fields_and_free_text_without_changing_state(recording):
    path, cfg, now = recording
    before = path.read_bytes()
    s = m.snapshot(path, cfg, now.date(), now)
    raw = json.dumps(s)
    for secret in (ACCOUNT, "SYNA", "SYNB", "9876543", "876543", "7654", "654321", "765432", "12345", "PASSWORDVALUE"):
        assert secret not in raw
    assert s["managed_quote_age_seconds"] == [150]
    assert not s["deterministic_signals"]
    assert s["new_incidents"][0]["code"] == "redacted_diagnostic"
    assert path.read_bytes() == before


def test_cycle_data_gap_is_reported_without_exporting_private_cycle_trace(recording):
    path, cfg, now = recording
    rewrite(path, valuation_available=False, managed_data_gap_since=(now - timedelta(seconds=400)).isoformat(),
            telemetry_ok=False, last_cycle_diagnostics={"equity": "PRIVATE_EQUITY", "symbols": ["SYNA"]})
    data = m.snapshot(path, cfg, now.date(), now)
    assert data["managed_data_gap_age_seconds"] == 400
    assert {"severity": "warning", "code": "managed_risk_unavailable"} in data["deterministic_signals"]
    assert {"severity": "critical", "code": "cycle_telemetry_unavailable"} in data["deterministic_signals"]
    assert "PRIVATE_EQUITY" not in json.dumps(data) and "SYNA" not in json.dumps(data)


@pytest.mark.parametrize("kind", ["checksum", "account", "day", "counter"])
def test_untrusted_state_rejected_before_inference(recording, kind):
    path, cfg, now = recording
    envelope = m.read(path)
    if kind == "checksum":
        envelope["state"]["cash"] = "1"
    elif kind == "account":
        cfg["account_id"] = "DIFFERENT"
    elif kind == "day":
        now += timedelta(days=1)
    else:
        envelope["state"]["http_budget"]["used"] = "PRIVATE_COUNTER"
        envelope["sha256"] = ExecutionController._digest(envelope["state"])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        m.snapshot(path, cfg, now.date(), now)


def test_critical_heartbeat_and_incident_cursor(recording):
    path, cfg, now = recording
    data = m.snapshot(path, cfg, now.date(), now + timedelta(minutes=7), since=now.isoformat())
    assert not data["new_incidents"]
    assert {"severity": "critical", "code": "heartbeat_stale_or_missing"} in data["deterministic_signals"]
    # Held sources may be up to the management age (300 s) old when admitted.
    assert {"severity": "warning", "code": "managed_data_delayed"} not in data["deterministic_signals"]
    data = m.snapshot(path, cfg, now.date(), now + timedelta(minutes=11), since=now.isoformat())
    assert {"severity": "warning", "code": "managed_data_delayed"} in data["deterministic_signals"]


def test_a_holding_valued_at_its_live_bid_is_not_reported_as_delayed_data(recording):
    path, cfg, now = recording
    state = m.read(path)["state"]
    state["strategy"]["quotes"]["SYNA"]["source_at"] = (now - timedelta(minutes=15)).isoformat()
    ExecutionController._write(path, state)
    assert {"severity": "warning", "code": "managed_data_delayed"} in m.snapshot(path, cfg, now.date(), now)["deterministic_signals"]
    rewrite(path, managed_book_valued=["SYNA"])
    data = m.snapshot(path, cfg, now.date(), now)
    assert not data["deterministic_signals"]
    assert data["managed_quote_age_seconds"] == [] and data["book_valued_trade_age_seconds"] == [900]
    assert "SYNA" not in json.dumps(data)
    assert "book_valued_trade_age_seconds" in m.PROMPT


@pytest.mark.parametrize("status,blocked,stale", [
    ("blocked", "managed_quote_missing", False),
    ("blocked", None, False),
    ("blocked", "managed_quote_missing", True),
    ("stopped", None, False),
])
def test_blocked_cycle_is_not_misreported_as_process_exit(recording, status, blocked, stale):
    path, cfg, now = recording
    extra = dict(heartbeat_at=(now - timedelta(seconds=181)).isoformat()) if stale else {}
    rewrite(path, status=status, entries_blocked=blocked, **extra)
    signals = m.snapshot(path, cfg, now.date(), now)["deterministic_signals"]
    codes = {s["code"] for s in signals}
    assert ("execution_not_running" in codes) == (status == "stopped")
    assert ("heartbeat_stale_or_missing" in codes) == stale
    if blocked:
        assert {"severity": "warning", "code": "new_entries_blocked"} in signals
    if status == "blocked" and not blocked:
        assert {"severity": "warning", "code": "cycle_blocked"} in signals
    if status == "blocked" and not stale:
        assert not any(s["severity"] == "critical" for s in signals)


def claude_answer(answer, *, model="claude-sonnet-5-5", denials=(), subtype="success"):
    return dict(type="result", subtype=subtype, is_error=subtype != "success", structured_output=answer,
                modelUsage={model: {"inputTokens": 100, "outputTokens": 10}}, permission_denials=list(denials),
                usage={"input_tokens": 100, "output_tokens": 10}, total_cost_usd=0.01)


def fake_runner(*, tool=False, returncode=0, model="claude-sonnet-5-5", seen=None):
    def run(args, **kwargs):
        assert args[1] == "-p" and args[args.index("--model") + 1] == "claude-sonnet-5-5"
        assert args[args.index("--effort") + 1] == "low"
        assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args
        assert args[args.index("--permission-mode") + 1] == "dontAsk" and "--no-session-persistence" in args
        assert args[args.index("--setting-sources") + 1] == "" and "--disable-slash-commands" in args
        assert kwargs["timeout"] == 120
        assert not any(k.upper().startswith(("IOL_", "ANTHROPIC_", "OPENAI_", "ARGBOT_")) for k in kwargs["env"])
        assert not Path(kwargs["cwd"]).resolve().is_relative_to(Path.cwd().resolve())
        if seen is not None:
            seen.update(env=kwargs["env"], prompt=kwargs["input"])
        answer = dict(severity="ok", summary="Resumen ficticio", findings=[])
        kwargs["stdout"].write(json.dumps(claude_answer(answer, model=model,
                                                        denials=[{"tool_name": "Bash"}] if tool else ())))
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=returncode)
    return run


def test_model_cannot_downgrade_deterministic_critical_and_secrets_not_in_env(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("IOL_PASSWORD", "fictional-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fictional-key-would-bill-api-credits")
    monkeypatch.setenv("HOME", str(tmp_path / "fictional-home"))
    seen = {}
    data = {"deterministic_signals": [dict(severity="critical", code="unresolved_order")]}
    result = m.analyze(data, tmp_path / "review", m.policy(enabled), runner=fake_runner(seen=seen),
                       executable="synthetic-claude")
    assert result["severity"] == "critical" and result["usage"]["input_tokens"] == 100
    assert result["deterministic_signals"] == data["deterministic_signals"]
    assert "fictional-secret" not in json.dumps(seen["env"]) and "fictional-key" not in json.dumps(seen["env"])
    assert seen["env"].get("HOME") == str(tmp_path / "fictional-home")
    assert seen["prompt"].startswith(m.PROMPT)


@pytest.mark.parametrize("mode", ["tool", "failed", "other_model"])
def test_failed_model_or_unexpected_tool_never_reports_success(tmp_path, mode, enabled):
    folder = tmp_path / "review"
    with pytest.raises(RuntimeError):
        m.analyze({"deterministic_signals": []}, folder, m.policy(enabled),
                  runner=fake_runner(tool=mode == "tool", returncode=2 if mode == "failed" else 0,
                                     model="claude-haiku-5-5" if mode == "other_model" else "claude-sonnet-5-5"),
                  executable="synthetic-claude")
    assert not (folder / "report.json").exists()


def test_off_by_default_and_reviewed_policy_cannot_be_loosened(tmp_path, recording):
    path, cfg, now = recording
    with pytest.raises(m.MonitorDisabled):
        m.policy(m.DEFAULT_POLICY)
    with pytest.raises(m.MonitorDisabled):
        m.once(path.parent, tmp_path / "unused.json")
    for key, value in (("interval_seconds", 60), ("model", "another-model"), ("mode", "act"), ("enabled", "yes")):
        p = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
        p.update(enabled=True)
        p[key] = value
        loose = tmp_path / f"{key}.json"
        loose.write_text(json.dumps(p), encoding="utf-8")
        with pytest.raises(ValueError, match="Unreviewed"):
            m.policy(loose)


def test_command_has_no_tools_and_env_whitelist_keeps_home(monkeypatch):
    monkeypatch.setenv("HOME", "/fictional-home")
    monkeypatch.setenv("ARGBOT_IOL_PASSWORD", "fictional-secret")
    env = claude_cli.claude_env()
    assert env.get("HOME") == "/fictional-home" and "ARGBOT_IOL_PASSWORD" not in env
    args = claude_cli.command("claude", m.SCHEMA, dict(model="claude-sonnet-5-5", reasoning_effort="low"), m.SYSTEM)
    assert args[args.index("--tools") + 1] == "" and "--allowedTools" not in args


def clock_at(monkeypatch, moments):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moments[0]
    monkeypatch.setattr(m, "datetime", Clock)
    monkeypatch.setattr(m.timing, "sleep", lambda _: moments.pop(0))


def write_config(tmp_path, cfg):
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_watch_delivers_known_critical_before_waiting_for_model(tmp_path, recording, monkeypatch, enabled):
    path, cfg, now = recording
    monkeypatch.setattr(m, "snapshot", lambda *a, **kw: dict(deterministic_signals=[dict(severity="critical", code="unresolved_order")]))
    clock_at(monkeypatch, [now, now.replace(hour=17, minute=6)])
    delivered = []
    monkeypatch.setattr(m, "notify", lambda folder, report: delivered.append(report))

    def analyze(data, *a, **kw):
        assert delivered and delivered[0]["severity"] == "critical"
        raise TimeoutError("fixture")
    monkeypatch.setattr(m, "analyze", analyze)
    m.watch(path.parent, write_config(tmp_path, cfg), enabled, out=tmp_path / "out")
    assert len(delivered) == 2 and delivered[-1]["severity"] == "critical"
    assert delivered[-1]["findings"][0]["code"] == "monitor_failure"


def test_watch_claims_once_no_backfill_and_stops_after_close(tmp_path, recording, monkeypatch, enabled):
    path, cfg, now = recording
    clock_at(monkeypatch, [now, now, now - timedelta(minutes=15), now + timedelta(minutes=31), now.replace(hour=17, minute=6)])
    calls = []

    def analyze(data, folder, p):
        calls.append(folder.name)
        return dict(severity="ok", summary="Bien", findings=[], deterministic_signals=[])
    monkeypatch.setattr(m, "analyze", analyze)
    out = tmp_path / "out"
    m.watch(path.parent, write_config(tmp_path, cfg), enabled, out=out)
    assert calls == ["02", "04"]
    assert len(list(out.glob("*.claim.json"))) == 2
    assert m.read(out / "binding.json")["state"] == str(path.resolve())


def test_watch_refuses_to_rebind_an_output_folder(tmp_path, recording, monkeypatch, enabled):
    path, cfg, now = recording
    out = tmp_path / "out"
    out.mkdir()
    (out / "binding.json").write_text(json.dumps(dict(state="another")), encoding="utf-8")
    clock_at(monkeypatch, [now])
    with pytest.raises(ValueError, match="bound"):
        m.watch(path.parent, write_config(tmp_path, cfg), enabled, out=out)


def test_local_failure_report_is_visible(tmp_path):
    m.notify(tmp_path, dict(severity="critical", summary="Revisión incompleta", findings=[]))
    assert "CRITICAL" in (tmp_path / "latest-report.txt").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "alerts.jsonl").read_text(encoding="utf-8"))["severity"] == "critical"
    m.notify(tmp_path, dict(severity="ok", summary="Bien", findings=[], deterministic_signals=[]))
    assert len((tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def close_snapshot(recording, *, early=False, failed=False, verified=True, supervisor=False):
    path, cfg, start = recording
    now = start.replace(hour=17, minute=1)
    stopped = now.replace(hour=16, minute=59) if early else now.replace(minute=0, second=1)
    state = m.read(path)["state"]
    state["strategy"].update(status="stopped" if verified or early else "running", stopped_at=stopped.isoformat(),
                             heartbeat_at=stopped.isoformat(), incidents=[], entries_blocked=None)
    state["last_reconciliation"]["at"] = now.replace(hour=16, minute=57).isoformat()
    ExecutionController._write(path, state)
    explicit = dict(exit="scheduled", at=now.replace(minute=0, second=2).isoformat(), worker_failure=failed) if supervisor else None
    return m.snapshot(path, cfg, now.date(), now, supervisor=explicit)


def test_verified_close_skips_model_and_reports_normal_stop(recording, tmp_path, enabled):
    data = close_snapshot(recording)
    assert data["session_phase"] == "after_close" and data["session_closed_normally"]
    assert data["supervisor_exit"] == "scheduled" and not data["deterministic_signals"]

    def forbidden(*a, **kw):
        pytest.fail("Normal close must not call an LLM")
    report = m.analyze(data, tmp_path / "closed-report", m.policy(enabled), runner=forbidden, executable="unused")
    assert report["severity"] == "ok" and report["usage"] is None
    m.notify(tmp_path, report)
    assert "CIERRE NORMAL" in (tmp_path / "latest-report.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("case", ["early", "failed", "unverified"])
def test_close_does_not_hide_unexpected_exit_or_missing_evidence(recording, case):
    data = close_snapshot(recording, early=case == "early", failed=case == "failed",
                          verified=case != "unverified", supervisor=case == "failed")
    assert not data["session_closed_normally"] and m.closing_report(data) is None
    signals = data["deterministic_signals"]
    expected = {"early": "premature_session_stop", "failed": "execution_failed", "unverified": "session_close_unverified"}[case]
    assert any(s["code"] == expected for s in signals)
    if case != "unverified":
        assert any(s["severity"] == "critical" for s in signals)


def test_normal_stop_keeps_real_critical_signals_and_historical_incidents(recording):
    data = close_snapshot(recording)
    data["deterministic_signals"] = [dict(severity="critical", code="unresolved_order")]
    data["new_incidents"] = [dict(code="managed_quote_unavailable", at=data["observed_at"])]
    report = m.closing_report(data)
    assert report["severity"] == "critical" and report["deterministic_signals"] == data["deterministic_signals"]
    assert any(f["code"] == "incidents_before_close" for f in report["findings"])


def test_local_lifecycle_needs_a_recorded_stop_at_or_after_the_end():
    end = datetime(2026, 10, 1, 17, tzinfo=m.ART)
    assert m.local_lifecycle(dict(status="stopped", stopped_at=end.isoformat()), end)["exit"] == "scheduled"
    assert m.local_lifecycle(dict(status="stopped", stopped_at=(end - timedelta(seconds=1)).isoformat()), end)["exit"] is None
    assert m.local_lifecycle(dict(status="running", stopped_at=end.isoformat()), end)["exit"] is None


def test_first_report_counts_only_this_sessions_incidents(recording):
    path, cfg, now = recording
    state = m.read(path)["state"]
    old = [dict(at=(now - timedelta(days=d)).isoformat(), code="managed_quote_unavailable", symbol=None) for d in (1, 2)]
    state["strategy"]["incidents"] = old * 40 + state["strategy"]["incidents"]
    ExecutionController._write(path, state)
    data = m.snapshot(path, cfg, now.date(), now, since=m.first_since(now.date()))
    assert data["new_incident_count"] == 1


@pytest.mark.parametrize("clock,waits", [((16, 59, 59), False), ((17, 0, 0), True), ((17, 0, 59), True), ((17, 1, 0), False)])
def test_final_check_waits_for_the_bot_to_record_its_close(clock, waits):
    assert m.settling_after_close(datetime(2026, 10, 7, *clock, tzinfo=m.ART)) is waits


@pytest.mark.parametrize("gap,lasting", [(400, False), (600, True)])
def test_missing_data_notice_only_for_a_lasting_gap(recording, gap, lasting):
    path, cfg, now = recording
    rewrite(path, valuation_available=False, managed_data_gap_since=(now - timedelta(seconds=gap)).isoformat())
    data = m.snapshot(path, cfg, now.date(), now)
    assert {"severity": "warning", "code": "managed_risk_unavailable"} in data["deterministic_signals"]
    assert ({"severity": "warning", "code": "managed_risk_persistent"} in data["deterministic_signals"]) is lasting


def at(recording, hour, minute, **strategy):
    """The recorded state as seen at hour:minute, with a fresh heartbeat and cycle."""
    path, cfg, now = recording
    state = m.read(path)["state"]
    when = now.replace(hour=hour, minute=minute)
    state["strategy"].update(heartbeat_at=when.isoformat(), last_cycle=when.isoformat(), **strategy)
    state["last_reconciliation"]["at"] = when.isoformat()
    ExecutionController._write(path, state)
    return when


def test_the_known_opening_block_is_not_a_warning_until_10_50(recording):
    path, cfg, now = recording
    opening = dict(status="blocked", entries_blocked="managed_quote_missing", valuation_available=False, quotes={})
    when = at(recording, 10, 30, managed_data_gap_since=now.replace(hour=10, minute=29, second=50).isoformat(), **opening)
    data = m.snapshot(path, cfg, now.date(), when)
    assert data["opening_settling"] is True and data["deterministic_signals"] == []
    assert "opening_settling" in m.PROMPT and "exclusions" in m.PROMPT
    when = at(recording, 10, 50, managed_data_gap_since=now.replace(hour=10, minute=49, second=50).isoformat(), **opening)
    data = m.snapshot(path, cfg, now.date(), when)
    assert data["opening_settling"] is False
    assert {"managed_data_delayed", "new_entries_blocked", "managed_risk_unavailable"} <= {s["code"] for s in data["deterministic_signals"]}


@pytest.mark.parametrize("case", ["persistent_gap", "other_block", "reconciliation", "heartbeat"])
def test_the_opening_never_hides_a_lasting_gap_another_block_or_a_critical_signal(recording, case):
    path, cfg, now = recording
    strategy = dict(status="blocked", entries_blocked="managed_quote_missing", valuation_available=False, quotes={},
                    managed_data_gap_since=now.replace(hour=10, minute=40).isoformat())
    if case == "persistent_gap":
        strategy["managed_data_gap_since"] = now.replace(hour=10, minute=34).isoformat()
    elif case == "other_block":
        strategy["entries_blocked"] = "unresolved_order"
    when = at(recording, 10, 45, **strategy)
    state = m.read(path)["state"]
    if case == "reconciliation":
        state["last_reconciliation"]["ok"] = False
    elif case == "heartbeat":
        state["strategy"]["heartbeat_at"] = (when - timedelta(seconds=181)).isoformat()
    ExecutionController._write(path, state)
    codes = {s["code"] for s in m.snapshot(path, cfg, now.date(), when)["deterministic_signals"]}
    expected = {"persistent_gap": "managed_risk_persistent", "other_block": "new_entries_blocked",
                "reconciliation": "reconciliation_not_confirmed", "heartbeat": "heartbeat_stale_or_missing"}[case]
    assert expected in codes
    if case == "other_block":
        assert {"managed_data_delayed", "managed_risk_unavailable"} <= codes


def test_opening_incidents_are_summarized_apart_from_new_ones(recording):
    path, cfg, now = recording
    incidents = [dict(at=now.replace(hour=10, minute=45).isoformat(), code="managed_quote_unavailable", symbol="SYNA"),
                 dict(at=now.replace(hour=11, minute=5).isoformat(), code="managed_quote_unavailable", symbol="SYNA")]
    when = at(recording, 11, 6, incidents=incidents)
    data = m.snapshot(path, cfg, now.date(), when, since=now.replace(hour=10, minute=40).isoformat())
    assert data["opening_incidents"] == {"managed_quote_unavailable": 1}
    assert [i["at"][11:16] for i in data["new_incidents"]] == ["11:05"] and data["new_incident_count"] == 1
    assert "SYNA" not in json.dumps(data)


def test_the_kind_of_a_failed_login_reaches_the_monitor():
    for name in ("IOLAuthNetworkError", "IOLAuthServerError", "IOLAuthRejectedError", "IOLAuthError"):
        assert m.code(name) == name
    assert m.code('Token endpoint returned 400: {"error":"invalid_grant"}') == "redacted_diagnostic"


def test_once_runs_with_a_fake_model_and_writes_the_report(tmp_path, recording, enabled):
    path, cfg, now = recording
    text, folder = m.once(path.parent, write_config(tmp_path, cfg), enabled, out=tmp_path / "out",
                          as_of_heartbeat=True, runner=fake_runner(), executable="synthetic-claude")
    assert "ARGBOT MONITOR" in text and (folder / "report.json").is_file() and (folder / "result.json").is_file()
    assert "SYNA" not in (folder / "snapshot.json").read_text(encoding="utf-8")


def test_cli_dry_run_over_a_paper_run_never_calls_a_model(tmp_path, monkeypatch, capsys):
    from argbot.cli import main
    from argbot.paper_session import run_paper_session

    def forbidden(*a, **kw):
        pytest.fail("--dry-run must not start any process")
    monkeypatch.setattr(m.subprocess, "run", forbidden)
    monkeypatch.setattr(claude_cli.subprocess, "run", forbidden)
    run = tmp_path / "paper"
    run_paper_session(Path(__file__).resolve().parents[1] / "configs/examples/strategy.example.json", run, days=1)
    assert main(["monitor", "--state-dir", str(run), "--once", "--dry-run", "--as-of-heartbeat",
                 "--out", str(tmp_path / "mon")]) == 0
    assert "ARGBOT MONITOR" in capsys.readouterr().out
    snapshots = list((tmp_path / "mon").glob("once-*-dry-run/snapshot.json"))
    assert len(snapshots) == 1
    state = json.loads((run / "execution-state.json").read_text(encoding="utf-8"))["state"]
    raw = snapshots[0].read_text(encoding="utf-8")
    for symbol in list(state["positions"]) + list((state.get("strategy") or {}).get("quotes") or {}):
        assert f'"{symbol}"' not in raw


def test_cli_refuses_without_opt_in(tmp_path, recording, capsys):
    from argbot.cli import main
    path, cfg, now = recording
    assert main(["monitor", "--state-dir", str(path.parent), "--config", str(write_config(tmp_path, cfg)), "--once"]) == 2
    assert "enabled" in capsys.readouterr().err


def test_watch_exits_cleanly_on_a_day_without_session(tmp_path, recording, monkeypatch, enabled):
    path, cfg, now = recording
    saturday = now.replace(day=3)
    clock_at(monkeypatch, [saturday])
    monkeypatch.setattr(m, "snapshot", lambda *a, **kw: pytest.fail("No check on a day without session"))
    out = tmp_path / "out"
    m.watch(path.parent, write_config(tmp_path, cfg), enabled, out=out)
    assert not out.exists()


def test_watch_waits_for_the_first_cycle_without_alerts(tmp_path, recording, monkeypatch, enabled):
    path, cfg, now = recording
    rewrite(path, day=(now.date() - timedelta(days=1)).isoformat())
    moments = [now, now + timedelta(minutes=1), now + timedelta(minutes=2), now.replace(hour=17, minute=6)]

    def advance(_):
        moments.pop(0)
        if len(moments) == 2:  # the bot records today's session
            rewrite(path, day=now.date().isoformat(), heartbeat_at=moments[0].isoformat(),
                    last_cycle=moments[0].isoformat())
    clock_at(monkeypatch, moments)
    monkeypatch.setattr(m.timing, "sleep", advance)
    seen = []

    def analyze(data, folder, p):
        seen.append(data["deterministic_signals"])
        return dict(severity="ok", summary="Bien", findings=[], deterministic_signals=[])
    monkeypatch.setattr(m, "analyze", analyze)
    out = tmp_path / "out"
    m.watch(path.parent, write_config(tmp_path, cfg), enabled, out=out)
    assert len(seen) == 1 and not any(s["severity"] == "critical" for s in seen[0])
    assert len(list(out.glob("*.claim.json"))) == 1 and not list(out.glob("*.failure.json"))
    assert not (out / "alerts.jsonl").exists()


def test_once_takes_the_read_only_lease_and_never_reuses_a_folder(tmp_path, recording):
    from argbot.lock import AccountLease, AlreadyRunning
    path, cfg, now = recording
    config = write_config(tmp_path, cfg)
    folders = {m.once(path.parent, config, out=tmp_path / "out", as_of_heartbeat=True, dry_run=True)[1] for _ in range(3)}
    assert len(folders) == 3
    with AccountLease("read-only-monitor:" + ACCOUNT):
        with pytest.raises(AlreadyRunning):
            m.once(path.parent, config, out=tmp_path / "out", as_of_heartbeat=True, dry_run=True)


@pytest.mark.parametrize("value", ["300", 0, 300.0, None, True])
def test_poll_seconds_must_be_a_positive_integer(tmp_path, recording, value):
    path, cfg, now = recording
    cfg["poll_seconds"] = value
    with pytest.raises(ValueError, match="poll_seconds"):
        m.bind(path.parent, write_config(tmp_path, cfg))


def test_default_reports_go_under_the_repository_runs_folder():
    out = m.default_out(datetime(2026, 10, 1).date())
    assert out == m.ROOT / "runs" / "monitor" / "2026-10-01" and (m.ROOT / "pyproject.toml").is_file()


def test_watch_reads_the_state_only_for_checks_once_the_session_is_seen(tmp_path, recording, monkeypatch, enabled):
    path, cfg, now = recording
    moments = [now + timedelta(seconds=5 * i) for i in range(6)] + [now.replace(hour=17, minute=6)]
    clock_at(monkeypatch, moments)
    reads = []
    real = m.read

    def counted(target):
        if Path(target).name == "execution-state.json":
            reads.append(1)
        return real(target)
    monkeypatch.setattr(m, "read", counted)
    monkeypatch.setattr(m, "analyze", lambda data, folder, p: dict(severity="ok", summary="Bien", findings=[],
                                                                  deterministic_signals=[]))
    m.watch(path.parent, write_config(tmp_path, cfg), enabled, out=tmp_path / "out")
    assert len(reads) == 2  # the first-session check and the one snapshot of slot 02
