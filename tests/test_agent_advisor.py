"""Optional shadow advisor: contracts, egress whitelist, scheduling and fail-visible behavior.

Every value here is synthetic. No test calls a real model: a fake runner stands in for
the Claude Code CLI, and the symbols SYNA/SYNB are fictional.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from argbot.agents import advisor_runner as advisor
from argbot.agents import claude_cli
from argbot.agents.advisor import (EGRESS_SCOPE_NAMED as EGRESS_SCOPE, MARKET_CONTEXT_MAX_CHARS, PROMPT_NAMED,
                                   SCORES, SOURCE_DATE_MAX_CHARS, SOURCE_URL_MAX_CHARS, SOURCES_MAX_ITEMS,
                                   SUMMARY_MAX_CHARS, demo, named_prompt, prepare_named_request, prepare_request,
                                   response_limits_text, validate_named_response, validate_response)
from argbot.execution import ExecutionController

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POLICY = ROOT / "configs/examples/advisor.example.json"
EXAMPLE_STRATEGY = ROOT / "configs/examples/strategy.example.json"
ACCOUNT = "FICTIONAL_ACCOUNT"
NOW = datetime(2026, 10, 1, 11, 50, tzinfo=advisor.ART)
SCHEDULE = dict(entry_windows=["11:45", "14:00", "16:45"], entry_cutoff="17:00", session_end="18:00")


def consent():
    return dict(scope=EGRESS_SCOPE, real_data_approved=True, operator_reference="fictional operator")


def reviewed():
    """The example policy with the example strategy schedule attached (as watch does)."""
    return advisor.schedule_matches(SCHEDULE, advisor.policy(EXAMPLE_POLICY, require_enabled=False))


def batch(now, window="11:45"):
    return dict(window=window, captured_at=now.isoformat(), mechanical_candidates=["SYNA"],
                candidates=[dict(symbol="SYNA", source_at=now.isoformat(), drawdown_fraction=-.09,
                                 day_range_fraction=.2, price_to_vwap=.99, spread_fraction=.003),
                            dict(symbol="SYNB", source_at=now.isoformat(), drawdown_fraction=-.10,
                                 day_range_fraction=.4, price_to_vwap=.98, spread_fraction=.004)])


QUOTES = {"SYNA": dict(ultimoPrecio=1234.5, cierreAnterior=1250.0, variacion=-1.2, apertura=1255.0, maximo=1260.0,
                       minimo=1230.0, montoOperado=1.5e8, cantidadOperaciones=420, bid="1234", ask="1235",
                       descripcionTitulo="Fictional Holding"),
          "SYNB": dict(ultimoPrecio=321.0, cantidadOperaciones=19)}
PRIVATE_KEYS = ("cash", "quantity", "account", "pnl", "descripcionTitulo", "bid", "ask", "hold", "equity", "position")


# ---------------------------------------------------------------- v1 contract

def v1_fixture():
    now = datetime(2026, 10, 2, 14, 15, tzinfo=timezone.utc)
    rows = [dict(source_at=(now - timedelta(seconds=10)).isoformat(), drawdown_fraction=-.09,
                 day_range_fraction=.25, price_to_vwap=.99, spread_fraction=.004)]
    return now, rows


def test_offline_demo_has_no_execution_or_provider_dependency(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("Network prohibited"))
    now, _ = v1_fixture()
    result = demo(tmp_path / "demo", now=now)
    assert result["advisory_only"] and result["orders_authorized"] is False
    assert len(list((tmp_path / "demo").iterdir())) == 3
    with pytest.raises(FileExistsError):
        demo(tmp_path / "demo", now=now)


@pytest.mark.parametrize("change", ["identity", "symbol", "price", "quantity", "old", "future", "nan", "real"])
def test_v1_rejects_private_extras_bad_time_and_unapproved_real_data(change):
    now, rows = v1_fixture()
    if change in ("identity", "symbol", "price", "quantity"):
        rows[0][change] = "PRIVATE"
    elif change == "old":
        rows[0]["source_at"] = (now - timedelta(seconds=61)).isoformat()
    elif change == "future":
        rows[0]["source_at"] = (now + timedelta(seconds=1)).isoformat()
    elif change == "nan":
        rows[0]["price_to_vwap"] = float("nan")
    with pytest.raises(ValueError):
        prepare_request(rows, now=now, synthetic=change != "real")


@pytest.mark.parametrize("change", ["late", "binding", "order", "unknown", "extra", "duplicate"])
def test_v1_response_cannot_authorize_orders_or_reuse_stale_input(change):
    now, rows = v1_fixture()
    request = prepare_request(rows, now=now)
    result = dict(request_id=request["request_id"], opinions=[dict(candidate_id="C01", opinion="watch", reason_code="setup")])
    if change == "late":
        now += timedelta(seconds=21)
    elif change == "binding":
        result["request_id"] = "other"
    elif change == "order":
        result["opinions"][0]["opinion"] = "BUY"
    elif change == "unknown":
        result["opinions"][0]["candidate_id"] = "C99"
    elif change == "extra":
        result["execute"] = True
    elif change == "duplicate":
        result["opinions"] *= 2
    with pytest.raises(ValueError):
        validate_response(request, result, now=now)


def test_quote_remaining_life_bounds_advice_deadline():
    now, rows = v1_fixture()
    rows[0]["source_at"] = (now - timedelta(seconds=58)).isoformat()
    request = prepare_request(rows, now=now)
    assert datetime.fromisoformat(request["expires_at"]) == now + timedelta(seconds=2)


def test_cli_demo_writes_the_fixture_without_any_process(tmp_path, monkeypatch, capsys):
    from argbot.cli import main
    monkeypatch.setattr(claude_cli.subprocess, "run", lambda *a, **kw: pytest.fail("No process expected"))
    assert main(["advisor-demo", "--output", str(tmp_path / "demo")]) == 0
    assert "No inference" in capsys.readouterr().out
    assert json.loads((tmp_path / "demo/result.json").read_text(encoding="utf-8"))["orders_authorized"] is False


# ---------------------------------------------------------------- policy, schedule, consent

def test_off_by_default_and_the_reviewed_policy_cannot_be_loosened(tmp_path):
    with pytest.raises(advisor.AdvisorDisabled):
        advisor.policy(EXAMPLE_POLICY)
    base = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    assert base["enabled"] is False and base["orders_enabled"] is False and base["mode"] == "shadow_only"
    assert "entry_windows" not in base and "entry_cutoff" not in base
    assert base["context_url"] == "" and base["symbol_url_template"] == ""
    for change in [dict(orders_enabled=True), dict(mode="live"), dict(model="other"), dict(max_calls_per_session=4),
                   dict(tools=["WebSearch", "WebFetch", "Bash"]), dict(allowed_domains=["*"]),
                   dict(aggregator_domains=["not-allowed.example"]), dict(enabled="yes"),
                   dict(real_data_requires_session_authorization=False), dict(egress_scope="anything")]:
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({**base, "enabled": True, **change}), encoding="utf-8")
        with pytest.raises(ValueError, match="Unreviewed"):
            advisor.policy(path)


@pytest.mark.parametrize("change", [dict(entry_windows=["11:45", "14:00", "15:00", "16:45"]),
                                    dict(entry_windows=["14:00", "11:45"]), dict(entry_cutoff="16:00"),
                                    dict(entry_windows=[]), dict(entry_windows="11:45"), dict(session_end="18:30")])
def test_schedule_comes_from_the_strategy_and_must_fit_the_policy(change):
    p = advisor.policy(EXAMPLE_POLICY, require_enabled=False)
    assert advisor.schedule_matches(SCHEDULE, p)["entry_windows"] == SCHEDULE["entry_windows"]
    with pytest.raises(ValueError, match="schedule"):
        advisor.schedule_matches(dict(SCHEDULE, **change), p)


def test_authorization_binds_date_model_policy_and_data_scope(tmp_path):
    p = advisor.policy(EXAMPLE_POLICY, require_enabled=False)
    a = dict(consent(), session=NOW.date().isoformat(), policy_sha256=advisor.digest(EXAMPLE_POLICY),
             model=p["model"], max_calls_per_session=3)
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(a))
    assert advisor.authorization(path, NOW.date(), p, EXAMPLE_POLICY) == a
    for field, value in [("session", "2026-09-30"), ("scope", "anonymous_four_features_timestamps_v1"),
                         ("model", "other"), ("max_calls_per_session", 4), ("policy_sha256", "changed"),
                         ("real_data_approved", False), ("operator_reference", "")]:
        path.write_text(json.dumps(dict(a, **{field: value})))
        with pytest.raises(ValueError):
            advisor.authorization(path, NOW.date(), p, EXAMPLE_POLICY)


def test_the_example_authorization_cannot_be_used_as_is(tmp_path):
    p = advisor.policy(EXAMPLE_POLICY, require_enabled=False)
    with pytest.raises(ValueError):
        advisor.authorization(ROOT / "configs/examples/advisor-authorization.example.json", NOW.date(), p, EXAMPLE_POLICY)


# Prompt numbers outside the limits sentence: score scale, step list, 30-day horizon, 0-1 range.
# The limits sentence is stripped here so its digits cannot mask a strategy threshold
# (for example 0.40 -> "40"). That sentence is checked separately against the constants.
PROMPT_DIGITS = {"0", "1", "2", "3", "4", "5", "30"}


def prompt_without_limits(text):
    return text.replace(response_limits_text(), "")


def test_the_prompts_carry_no_strategy_threshold():
    text = PROMPT_NAMED + advisor.CONTEXT + advisor.SYSTEM
    assert "%" not in text and set(re.findall(r"\d+", prompt_without_limits(text))) <= PROMPT_DIGITS
    assert "score entero de 1 a 5" in PROMPT_NAMED and "ordenalos entre sí" in PROMPT_NAMED
    assert "nunca la fecha en que lo leíste" in PROMPT_NAMED
    date = advisor.RESPONSE_SCHEMA["properties"]["verdicts"]["items"]["properties"]["sources"]["items"]["properties"]["date"]
    assert "no la de lectura" in date["description"]


PAGES = dict(context_url="https://www.cnv.gov.ar/contexto",
             symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}")


def policy_with(tmp_path, **change):
    base = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**base, "enabled": True, **change}), encoding="utf-8")
    return path


def test_example_policy_defaults_to_the_generic_reading_procedure():
    raw = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    assert raw["context_url"] == "" and raw["symbol_url_template"] == ""
    p = advisor.policy(EXAMPLE_POLICY, require_enabled=False)
    text = advisor.named_prompt(p)
    assert text == PROMPT_NAMED
    assert "una página de un sitio permitido con el contexto de hoy" in text
    assert "reemplazá {symbol} por el ticker" not in text


def test_configured_reading_pages_enter_the_prompt(tmp_path):
    p = advisor.policy(policy_with(tmp_path, **PAGES))
    text = advisor.named_prompt(p)
    assert PAGES["context_url"] in text and PAGES["symbol_url_template"] in text
    assert "reemplazá {symbol} por el ticker" in text
    assert "una página de un sitio permitido con el contexto de hoy" not in text
    assert "buscá con WebSearch sus noticias" not in text
    assert "%" not in text and set(re.findall(r"\d+", prompt_without_limits(text))) <= PROMPT_DIGITS


@pytest.mark.parametrize("change", [
    dict(context_url="https://www.cnv.gov.ar/contexto"),  # only one
    dict(symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="http://www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="http://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://evil.example/x", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="https://evil.example/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar.evil.example/x", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://user:pass@www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="ftp://www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="//www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar/contexto#x", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/SYNA"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="https://{symbol}.cnv.gov.ar/perfil"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}/{other}"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar/contexto", symbol_url_template="https://www.cnv.gov.ar:8443/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar:foo/contexto", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url="https://www.cnv.gov.ar/{symbol}", symbol_url_template="https://www.cnv.gov.ar/perfil/{symbol}"),
    dict(context_url=None, symbol_url_template=None),
])
def test_reading_pages_must_be_https_on_an_allowed_host(tmp_path, change):
    with pytest.raises(ValueError, match="Unreviewed"):
        advisor.policy(policy_with(tmp_path, **change))


def test_no_order_path_exists_in_the_advisor_modules():
    for name in ("advisor.py", "advisor_runner.py", "advisor_feed.py"):
        source = (ROOT / "argbot/agents" / name).read_text(encoding="utf-8")
        assert "argbot.brokers" not in source and "submit" not in source and "operational-cycles" not in source


# ---------------------------------------------------------------- named contract

def named(now):
    return prepare_named_request(advisor.named_rows(batch(now), QUOTES), now=now, authorization=consent())


def test_named_request_carries_public_data_only():
    request = named(NOW)
    syna = next(c for c in request["candidates"] if c["symbol"] == "SYNA")
    assert syna["mechanical_signal"] == "candidate" and syna["last_price"] == 1234.5 and syna["trades"] == 420
    assert "descripcionTitulo" not in syna and "bid" not in syna
    synb = next(c for c in request["candidates"] if c["symbol"] == "SYNB")
    assert synb["mechanical_signal"] == "not_selected" and "previous_close" not in synb
    assert set(request) == {"schema", "mode", "request_id", "requested_at", "candidates"}


def test_unapproved_scope_cannot_build_a_named_request():
    for approval in (None, dict(consent(), scope="anonymous_four_features_timestamps_v1"),
                     dict(consent(), real_data_approved=False), dict(consent(), operator_reference="")):
        with pytest.raises(ValueError):
            prepare_named_request(advisor.named_rows(batch(NOW), QUOTES), now=NOW, authorization=approval)


def score_response(**score):
    return dict(request_id="r1", market_context="", verdicts=[
        dict(symbol=s, verdict="ok", confidence="media", reason_code="sin_novedades", summary="", sources=[],
             score=score.get(s, 3)) for s in ("SYNA", "SYNB")])


def test_every_verdict_carries_a_whole_score_from_1_to_5():
    request = dict(request_id="r1", requested_at=(NOW - timedelta(seconds=30)).isoformat(),
                   candidates=[dict(symbol="SYNA"), dict(symbol="SYNB")])
    result = validate_named_response(request, score_response(SYNA=5, SYNB=1), now=NOW, allowed_domains=["example.com"])
    assert {r["symbol"]: r["score"] for r in result["verdicts"]} == {"SYNA": 5, "SYNB": 1}
    for bad in (0, 6, 2.5, "3", True):
        with pytest.raises(ValueError, match="verdict"):
            validate_named_response(request, score_response(SYNA=bad), now=NOW, allowed_domains=["example.com"])
    item = advisor.RESPONSE_SCHEMA["properties"]["verdicts"]["items"]
    assert "score" in item["required"] and item["properties"]["score"]["minimum"] == min(SCORES)


def _limit_request():
    return dict(request_id="r1", requested_at=(NOW - timedelta(seconds=30)).isoformat(),
                candidates=[dict(symbol="SYNA"), dict(symbol="SYNB")])


def _limit_response(*, market_context="", syna=None, synb=None):
    def row(symbol, **fields):
        base = dict(symbol=symbol, verdict="ok", confidence="media", reason_code="sin_novedades",
                    summary="", sources=[], score=3)
        base.update(fields)
        return base
    return dict(request_id="r1", market_context=market_context,
                verdicts=[row("SYNA", **(syna or {})), row("SYNB", **(synb or {}))])


def test_response_field_limits_are_told_and_enforced():
    request = _limit_request()
    at_limit = _limit_response(
        market_context="m" * MARKET_CONTEXT_MAX_CHARS,
        syna=dict(summary="s" * SUMMARY_MAX_CHARS,
                  sources=[dict(url="u" * SOURCE_URL_MAX_CHARS, date="d" * SOURCE_DATE_MAX_CHARS)
                           for _ in range(SOURCES_MAX_ITEMS)]))
    result = validate_named_response(request, at_limit, now=NOW, allowed_domains=["example.com"])
    assert len(result["market_context"]) == MARKET_CONTEXT_MAX_CHARS
    syna = next(r for r in result["verdicts"] if r["symbol"] == "SYNA")
    assert len(syna["summary"]) == SUMMARY_MAX_CHARS and len(syna["sources"]) == SOURCES_MAX_ITEMS

    too_long_context = _limit_response(market_context="m" * (MARKET_CONTEXT_MAX_CHARS + 1))
    with pytest.raises(ValueError, match="market context"):
        validate_named_response(request, too_long_context, now=NOW, allowed_domains=["example.com"])

    # One over-limit field rejects the whole response, including the other symbol's valid verdict.
    cases = [
        dict(syna=dict(summary="s" * (SUMMARY_MAX_CHARS + 1))),
        dict(syna=dict(sources=[dict(url="u", date="2026-01-01") for _ in range(SOURCES_MAX_ITEMS + 1)])),
        dict(syna=dict(sources=[dict(url="u" * (SOURCE_URL_MAX_CHARS + 1), date="2026-01-01")])),
        dict(syna=dict(sources=[dict(url="u", date="d" * (SOURCE_DATE_MAX_CHARS + 1))])),
    ]
    for change in cases:
        with pytest.raises(ValueError, match="verdict"):
            validate_named_response(request, _limit_response(**change), now=NOW, allowed_domains=["example.com"])

    limits = response_limits_text()
    assert limits in PROMPT_NAMED and limits in named_prompt(PAGES)
    props = advisor.RESPONSE_SCHEMA["properties"]
    item = props["verdicts"]["items"]["properties"]
    assert props["market_context"]["maxLength"] == MARKET_CONTEXT_MAX_CHARS
    assert item["summary"]["maxLength"] == SUMMARY_MAX_CHARS
    assert item["sources"]["maxItems"] == SOURCES_MAX_ITEMS
    assert item["sources"]["items"]["properties"]["url"]["maxLength"] == SOURCE_URL_MAX_CHARS
    assert item["sources"]["items"]["properties"]["date"]["maxLength"] == SOURCE_DATE_MAX_CHARS


# ---------------------------------------------------------------- inference with a fake CLI

def fake_runner(*, denied=None, failure=False, seen=None, subtype=None, bad_id=False, verdict="ok",
                sources=None, model="claude-sonnet-5-5"):
    def run(args, **kw):
        folder = Path(kw["stdout"].name).parent
        request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        allowed = args[args.index("--allowedTools") + 1:args.index("--strict-mcp-config")]
        assert args[args.index("--tools") + 1] == "WebSearch,WebFetch" and allowed[0] == "WebSearch"
        assert all(a.startswith("WebFetch(domain:") for a in allowed[1:]) and "WebFetch(domain:www.cnv.gov.ar)" in allowed
        assert "--strict-mcp-config" in args and "--disable-slash-commands" in args
        assert args[args.index("--permission-mode") + 1] == "dontAsk"
        assert args[args.index("--model") + 1] == "claude-sonnet-5-5" and kw["timeout"] == 300
        assert args[args.index("--effort") + 1] == "medium"
        schema = json.loads((folder / "schema.json").read_text(encoding="utf-8"))
        assert json.loads(args[args.index("--json-schema") + 1]) == schema
        assert schema["properties"]["request_id"]["enum"] == [request["request_id"]]
        assert schema["properties"]["verdicts"]["minItems"] == len(request["candidates"])
        assert "SYNA" in kw["input"] and "fictional operator" not in kw["input"]
        sent = json.loads(kw["input"][kw["input"].index('{"schema"'):])
        assert sent == request
        assert not any(key.startswith(PRIVATE_KEYS) for c in sent["candidates"] for key in c)
        assert not any(k.upper().startswith(("IOL_", "ANTHROPIC_", "OPENAI_", "AWS_", "ARGBOT_")) for k in kw["env"])
        assert not Path(kw["cwd"]).resolve().is_relative_to(Path.cwd().resolve())
        if seen is not None:
            seen.append(dict(request=request, prompt=kw["input"]))
        result = dict(request_id=request["request_id"], market_context="Mercado estable (ficticio).", verdicts=[
            dict(symbol=c["symbol"], verdict=verdict, confidence="media",
                 reason_code="noticia_negativa" if verdict == "veto" else "sin_novedades", score=3,
                 summary="Resumen ficticio.", sources=list(sources or [])) for c in request["candidates"]])
        if bad_id:
            result["request_id"] = result["request_id"][:-1]
        kw["stdout"].write(json.dumps(dict(type="result", subtype=subtype or "success", is_error=bool(subtype),
                                           structured_output=result, modelUsage={model: {}},
                                           permission_denials=list(denied or []),
                                           usage={"input_tokens": 100, "output_tokens": 10}, total_cost_usd=0.01)))
        kw["stdout"].flush()
        return SimpleNamespace(returncode=2 if failure else 0)
    return run


def infer(tmp_path, now, **options):
    return advisor.infer(named(now), tmp_path / "run", reviewed(), approved=consent(),
                         runner=fake_runner(**options), executable="fictional-claude", clock=lambda: now)


@pytest.mark.parametrize("field", ["quantity", "account_id", "cash", "pnl", "instruction", "cost"])
def test_account_fields_cannot_reach_the_provider(tmp_path, field):
    request = named(NOW)
    request["candidates"][0][field] = 1
    with pytest.raises(ValueError):
        advisor.infer(request, tmp_path / "run", reviewed(), approved=consent(),
                      runner=lambda *a, **kw: pytest.fail("Unauthorized outgoing data"), clock=lambda: NOW)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("subtype", ["error_max_turns", "error_during_execution"])
def test_unsuccessful_cli_result_is_never_advice(tmp_path, subtype):
    with pytest.raises(RuntimeError, match="failure"):
        infer(tmp_path, NOW, subtype=subtype)
    assert not (tmp_path / "run/validated.json").exists()


def test_model_must_echo_exact_id_even_when_schema_constrains_it(tmp_path):
    with pytest.raises(ValueError, match="binding"):
        infer(tmp_path, NOW, bad_id=True)
    assert not (tmp_path / "run/validated.json").exists()


def test_blocked_reads_outside_allowed_sites_are_recorded_not_fatal(tmp_path):
    result = infer(tmp_path, NOW, denied=[dict(tool_name="WebFetch", tool_input=dict(url="https://example.com/x"))])
    assert result["usage"]["denied_reads"] == ["https://example.com/x"]


@pytest.mark.parametrize("denied,model", [([dict(tool_name="Bash", tool_input={})], "claude-sonnet-5-5"),
                                          ([dict(tool_name="mcp__broker__place_order", tool_input={})], "claude-sonnet-5-5"),
                                          ([], "claude-haiku-5-5")])
def test_any_other_tool_attempt_or_model_is_rejected(tmp_path, denied, model):
    with pytest.raises(RuntimeError):
        infer(tmp_path, NOW, denied=denied, model=model)


@pytest.mark.parametrize("url,kept", [("https://www.cnv.gov.ar/hecho/1", True), ("https://foro.example.com/x", False),
                                      ("https://evil.example/cnv.gov.ar", False), ("notaurl", False)])
def test_a_veto_needs_a_source_on_an_allowed_site(tmp_path, url, kept):
    result = infer(tmp_path, NOW, verdict="veto", sources=[dict(url=url, date="2026-09-30")])
    assert all(v["verdict"] == ("veto" if kept else "sin_datos") for v in result["verdicts"])
    assert result["vetoes"] == (2 if kept else 0)
    if not kept:
        assert all(v["downgraded_from"] == "veto" for v in result["verdicts"])


AGGREGATED = dict(url="https://www.rava.com/perfil/SYNA", date="2026-09-30")
NOTE = dict(url="https://www.cronista.com/finanzas/nota", date="2026-09-30")


@pytest.mark.parametrize("sources,kept,aggregator", [([AGGREGATED], True, True), ([AGGREGATED, NOTE], True, False),
                                                     ([NOTE], True, False), ([], False, False)])
def test_a_veto_resting_only_on_an_aggregator_page_is_tagged_not_dropped(tmp_path, sources, kept, aggregator):
    result = infer(tmp_path, NOW, verdict="veto", sources=sources)
    assert all(v["verdict"] == ("veto" if kept else "sin_datos") for v in result["verdicts"])
    assert all(v["aggregator_only"] is aggregator for v in result["verdicts"])
    assert result["aggregator_only_vetoes"] == (2 if aggregator else 0)


# ---------------------------------------------------------------- windows

def test_default_inference_keeps_the_generic_procedure(tmp_path):
    seen = []
    infer(tmp_path, NOW, seen=seen)
    prompt = seen[0]["prompt"]
    assert "una página de un sitio permitido con el contexto de hoy" in prompt
    assert "reemplazá {symbol} por el ticker" not in prompt
    assert PAGES["context_url"] not in prompt


def test_configured_pages_reach_the_model_prompt(tmp_path):
    seen = []
    p = advisor.schedule_matches(SCHEDULE, advisor.policy(policy_with(tmp_path, **PAGES)))
    advisor.infer(named(NOW), tmp_path / "run", p, approved=consent(),
                  runner=fake_runner(seen=seen), executable="fictional-claude", clock=lambda: NOW)
    prompt = seen[0]["prompt"]
    assert PAGES["context_url"] in prompt and PAGES["symbol_url_template"] in prompt
    assert "reemplazá {symbol} por el ticker" in prompt
    assert "una página de un sitio permitido con el contexto de hoy" not in prompt


def test_end_to_end_shadow_records_named_verdicts_once_without_secrets(tmp_path, monkeypatch):
    seen = []
    for name in ("IOL_PASSWORD", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "SECRET")

    def analyzer(request, folder, p, **kwargs):
        return advisor.infer(request, folder, p, **kwargs, runner=fake_runner(seen=seen),
                             executable="fictional-claude", clock=lambda: NOW + timedelta(seconds=40))
    report = advisor.process_batch(tmp_path, batch(NOW), reviewed(), consent(), now=NOW, analyzer=analyzer, quotes=QUOTES)
    assert report["status"] == "recorded" and not report["orders_authorized"]
    by_symbol = {v["symbol"]: v for v in report["verdicts"]}
    assert by_symbol["SYNA"]["mechanical_signal"] == "candidate" and by_symbol["SYNB"]["mechanical_signal"] == "not_selected"
    advisor.process_batch(tmp_path, batch(NOW), reviewed(), consent(), now=NOW, analyzer=analyzer, quotes=QUOTES)
    assert len(seen) == 1
    text = (tmp_path / "latest-report.txt").read_text(encoding="utf-8")
    assert "SYNA: OK" in text and "SOLO OBSERVACION" in text


@pytest.mark.parametrize("case", ["tools", "exit", "timeout"])
def test_model_failure_is_visible_and_consumes_window_without_retry(tmp_path, case):
    calls = []

    def inference(request, folder, p, **kw):
        calls.append(1)
        if case == "timeout":
            raise TimeoutError("fixture")
        return advisor.infer(request, folder, p, **kw, runner=fake_runner(
            denied=[dict(tool_name="Bash")] if case == "tools" else None, failure=case == "exit"),
            executable="fictional-claude", clock=lambda: NOW)
    report = advisor.process_batch(tmp_path, batch(NOW), reviewed(), consent(), now=NOW, analyzer=inference)
    assert report["status"] == "failed" and not report["orders_authorized"]
    advisor.process_batch(tmp_path, batch(NOW), reviewed(), consent(), now=NOW, analyzer=inference)
    assert len(calls) == 1 and "Falló la revisión" in (tmp_path / "latest-report.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("case", ["empty", "queue", "old_window", "future", "unknown_window"])
def test_missing_or_old_batch_never_causes_inference(tmp_path, case):
    now, data = NOW, batch(NOW)
    if case == "empty":
        data["candidates"] = []
    if case == "queue":
        now += timedelta(seconds=121)
    if case == "old_window":
        now = now.replace(hour=14, minute=5)
    if case == "future":
        now -= timedelta(seconds=1)
    if case == "unknown_window":
        data["window"] = "12:00"

    def call(*a, **kw):
        pytest.fail("No inference expected")
    if case in ("old_window", "future", "unknown_window"):
        with pytest.raises(ValueError):
            advisor.process_batch(tmp_path, data, reviewed(), consent(), now=now, analyzer=call)
    else:
        report = advisor.process_batch(tmp_path, data, reviewed(), consent(), now=now, analyzer=call)
        assert report["status"].startswith("skipped_")


def test_one_call_per_window_at_most_even_with_repeated_polling(tmp_path):
    calls = []
    p = reviewed()

    def failed(*a, **kw):
        calls.append(1)
        raise TimeoutError()
    for window in p["entry_windows"]:
        now = datetime(2026, 10, 1, *map(int, window.split(":")), tzinfo=advisor.ART)
        for _ in range(3):
            advisor.process_batch(tmp_path, batch(now, window), p, consent(), now=now, analyzer=failed)
    assert len(calls) == 3 and len(list(tmp_path.glob("*.claim.json"))) == 3


# ---------------------------------------------------------------- watch over a state file

@pytest.fixture
def recording(tmp_path):
    folder = tmp_path / "state"
    ExecutionController.prepare(folder, ACCOUNT, cash="9876543", positions={"SYNC": dict(quantity=7654, cost="876543")},
                                fee_reserve=".01")
    path = folder / "execution-state.json"
    state = advisor.read(path)["state"]
    state["strategy"] = dict(day=NOW.date().isoformat(), status="running", heartbeat_at=NOW.isoformat(),
                             last_cycle=NOW.isoformat(), equity="654321", advisor_batch=batch(NOW), quotes=QUOTES)
    state["last_reconciliation"] = dict(ok=True, at=NOW.isoformat(), reason=None)
    ExecutionController._write(path, state)
    # The private cycle audit sits beside the state; the advisor must never read it.
    (folder / "operational-cycles.private.jsonl").write_text('{"PRIVATE_AUDIT_SENTINEL": 1}\n', encoding="utf-8")
    cfg = dict(json.loads(EXAMPLE_STRATEGY.read_text(encoding="utf-8")), account_id=ACCOUNT, **SCHEDULE)
    config = tmp_path / "strategy.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    policy = tmp_path / "advisor.json"
    policy.write_text(json.dumps(dict(json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8")), enabled=True)),
                      encoding="utf-8")
    approval = tmp_path / "authorization.json"
    approval.write_text(json.dumps(dict(consent(), session=NOW.date().isoformat(), policy_sha256=advisor.digest(policy),
                                        model="claude-sonnet-5-5", max_calls_per_session=3)), encoding="utf-8")
    return path, config, policy, approval


def test_snapshot_reads_only_the_advisor_keys(recording):
    path, config, policy, approval = recording
    cfg = dict(json.loads(config.read_text(encoding="utf-8")))
    view = advisor.snapshot(path, cfg, NOW.date())
    assert set(view) == set(advisor.RUNTIME_KEYS) | {"_last_reconciliation"}
    raw = json.dumps(view)
    for private in ("9876543", "876543", "7654", "654321", "SYNC", ACCOUNT):
        assert private not in raw


def test_watch_keeps_the_ledger_unchanged_calls_the_model_once_and_never_reads_the_audit(tmp_path, recording, monkeypatch):
    path, config, policy, approval = recording
    before = path.read_bytes()
    opened = []
    real_open = Path.open

    def spy(self, *a, **kw):
        opened.append(self.name)
        return real_open(self, *a, **kw)
    monkeypatch.setattr(Path, "open", spy)
    moments = [NOW, NOW, NOW.replace(hour=18, minute=6)]
    calls = []

    def model(request, folder, p, **kw):
        calls.append(request)
        assert {c["symbol"] for c in request["candidates"]} == {"SYNA", "SYNB"}
        response = dict(request_id=request["request_id"], market_context="", verdicts=[
            dict(symbol=c["symbol"], verdict="ok", confidence="baja", reason_code="sin_novedades", score=3, summary="",
                 sources=[]) for c in request["candidates"]])
        return validate_named_response(request, response, now=NOW, allowed_domains=p["allowed_domains"])
    out = tmp_path / "out"
    advisor.watch(path.parent, config, policy, approval, out=out, clock=lambda: moments[0],
                  sleep=lambda _: moments.pop(0), analyzer=model)
    assert len(calls) == 1 and path.read_bytes() == before
    assert (out / "1145.report.private.json").exists()
    assert "operational-cycles.private.jsonl" not in opened
    assert not any("PRIVATE_AUDIT_SENTINEL" in f.read_text(encoding="utf-8") for f in out.rglob("*") if f.is_file())
    # The same binding can resume, but a window is never consumed twice.
    moments[:] = [NOW, NOW.replace(hour=18, minute=6)]
    advisor.watch(path.parent, config, policy, approval, out=out, clock=lambda: moments[0],
                  sleep=lambda _: moments.pop(0), analyzer=model)
    assert len(calls) == 1 and path.read_bytes() == before


@pytest.mark.parametrize("case", ["stale", "stopped", "reconciliation", "other_day"])
def test_watch_refuses_without_a_current_cycle(tmp_path, recording, case):
    path, config, policy, approval = recording
    state = advisor.read(path)["state"]
    if case == "stale":
        state["strategy"]["heartbeat_at"] = (NOW - timedelta(seconds=181)).isoformat()
        state["strategy"]["last_cycle"] = state["strategy"]["heartbeat_at"]
    elif case == "stopped":
        state["strategy"]["status"] = "stopped"
    elif case == "reconciliation":
        state["last_reconciliation"]["ok"] = False
    else:
        state["strategy"]["day"] = "2026-09-30"
    ExecutionController._write(path, state)
    with pytest.raises(ValueError):
        advisor.watch(path.parent, config, policy, approval, out=tmp_path / "out", clock=lambda: NOW,
                      sleep=lambda _: pytest.fail("No observation expected"),
                      analyzer=lambda *a, **kw: pytest.fail("No inference expected"))


def test_cli_needs_an_authorization_and_refuses_without_opt_in(tmp_path, recording, capsys):
    from argbot.cli import main
    path, config, policy, approval = recording
    with pytest.raises(SystemExit):
        main(["advisor", "--state-dir", str(path.parent), "--config", str(config)])
    assert "--authorization" in capsys.readouterr().err
    assert main(["advisor", "--state-dir", str(path.parent), "--config", str(config),
                 "--authorization", str(approval)]) == 2
    assert "enabled" in capsys.readouterr().err


def test_a_paper_capture_feeds_the_named_request(tmp_path):
    """The batch the candidate records in a paper run is exactly what the runner can send."""
    from argbot.paper_session import run_paper_session
    cfg = json.loads(EXAMPLE_STRATEGY.read_text(encoding="utf-8"))
    cfg["advisor"] = {"capture_enabled": True}
    config = tmp_path / "capture.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    run_paper_session(config, tmp_path / "run", days=1)
    runtime = json.loads((tmp_path / "run/execution-state.json").read_text(encoding="utf-8"))["state"]["strategy"]
    captured = runtime["advisor_batch"]
    assert captured and captured["candidates"]
    rows = advisor.named_rows(captured, runtime["quotes"])
    request = prepare_named_request(rows, now=datetime.fromisoformat(captured["captured_at"]), authorization=consent())
    advisor.validate_egress(request, consent())
    from argbot.agents.advisor import NAMED_FIELDS
    assert all(set(c) <= NAMED_FIELDS for c in request["candidates"])
