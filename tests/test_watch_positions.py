"""Terminal positions viewer: local files only, never writes, never asks the broker."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from argbot import watch_positions as w
from argbot.cli import main
from argbot.execution import ExecutionController
from argbot.paper_session import run_paper_session

NOW = datetime(2026, 10, 14, 13, 50, tzinfo=w.ART)
STRATEGY_CONFIG = "configs/examples/strategy.example.json"


@pytest.fixture
def synthetic_state(tmp_path):
    """A synthetic state folder: two placeholder holdings, one with lock on, one sale charged."""
    state_dir = tmp_path / "state"
    cfg_path = tmp_path / "config.json"
    ExecutionController.prepare(
        state_dir,
        "FICTIONAL",
        cash="100000",
        fee_reserve="0.01",
        positions={
            "SYNA": dict(
                quantity=4,
                cost="4000",
                swing=dict(precio_entrada=990.0, max_precio_desde_entrada=1010.0, stop_nivel=910.5),
            ),
            "SYNB": dict(
                quantity=2,
                cost="4040",
                swing=dict(precio_entrada=2000.0, max_precio_desde_entrada=2120.0, stop_nivel=2050.0),
            ),
        },
    )
    path = state_dir / "execution-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))["state"]
    state["orders"] = {
        "s1": dict(
            local_id="s1",
            symbol="SYNC",
            side="SELL",
            quantity=1,
            limit="10000",
            created_at="2026-10-13T16:00:00-03:00",
            reason="mechanical_stop",
            state="filled",
            broker_id="9",
            quote_expires_at=None,
            filled=1,
            gross="10000",
            fees="60.00",
            hold="0",
        )
    }
    state["strategy"] = dict(
        day="2026-10-14",
        status="running",
        heartbeat_at=(NOW - timedelta(seconds=20)).isoformat(),
        equity="111500",
        opening_equity="112000",
        valuation_at=(NOW - timedelta(seconds=90)).isoformat(),
        entries_blocked=None,
        risk_paused=False,
        quotes={
            "SYNA": dict(ultimoPrecio=980.0, bid="975", source_at=(NOW - timedelta(minutes=3)).isoformat()),
            "SYNB": dict(ultimoPrecio=2080.0, bid="2075", source_at=(NOW - timedelta(seconds=40)).isoformat()),
        },
    )
    ExecutionController._write(path, state)
    cfg_path.write_text(
        json.dumps(
            dict(
                fee_reserve="0.01",
                max_daily_loss_pct="0.02",
                strategy=dict(lock_ganador_umbral=0.05),
            )
        ),
        encoding="utf-8",
    )
    return state_dir, cfg_path, path


def test_positions_are_valued_at_the_bid_net_of_both_commissions(synthetic_state):
    state_dir, cfg_path, _ = synthetic_state
    view = w.collect(state_dir, cfg_path, now=NOW)
    rows = {r["symbol"]: r for r in view["rows"]}
    assert view["sell_rate"] == Decimal("0.006")  # the commission actually charged
    expected = 4 * Decimal("975") * (1 - Decimal("0.006"))
    assert rows["SYNA"]["value"] == expected and rows["SYNA"]["result"] == expected - 4000
    assert rows["SYNB"]["lock"] is True and rows["SYNA"]["lock"] is False  # 2120 >= 2000 x 1.05
    # Price move only, no commissions: (980 - 990) x 4 and 980 / 990 - 1.
    assert rows["SYNA"]["gross"] == Decimal("-40") and rows["SYNA"]["gross_pct"] == Decimal("980") / Decimal("990") - 1
    assert view["cash"] == Decimal("100000") and view["invested"] == rows["SYNA"]["value"] + rows["SYNB"]["value"]


def table(screen: str, symbol: str) -> str:
    return next(line for line in screen.splitlines() if line.startswith("│ " + symbol))


def test_screen_shows_the_day_result_the_stops_and_no_private_ids(synthetic_state):
    state_dir, cfg_path, _ = synthetic_state
    screen = w.render(w.collect(state_dir, cfg_path, now=NOW), color=False)
    assert "ARGBOT · POSICIONES ABIERTAS" in screen
    assert "Disponible $100.000" in screen and "● operando · última señal hace 20 s" in screen
    assert "Resultado del día -$500 (-0,45%)" in screen and "apertura $112.000" in screen
    assert "freno -2% (faltan 1,55%)" in screen and "FICTIONAL" not in screen
    syna, synb = table(screen, "SYNA"), table(screen, "SYNB")
    assert "lock 2.050,0" in synb and "910,5" in syna and "lock" not in syna
    # Price and age keep their columns whatever the age text ("3 min" or "40 s").
    assert syna.index("980") + 3 == synb.index("2.080") + 5 and syna.index("3 min") + 5 == synb.index("40 s") + 4
    assert "-$40   -1,01%" in syna and syna.index("-$40") < syna.index("910,5")
    total = table(screen, "TOTAL")
    assert "-$40" not in total and total.count("%") == 2  # gross and net totals
    assert "Resultado: precio actual contra precio de compra, sin comisiones." in screen
    assert "no consulta al broker" in screen
    assert "\x1b[" not in screen  # no color codes outside a terminal


def test_every_line_of_the_boxes_has_the_same_width(synthetic_state):
    state_dir, cfg_path, _ = synthetic_state
    for color in (False, True):
        lines = w.render(w.collect(state_dir, cfg_path, now=NOW), color=color).splitlines()
        framed = [line for line in lines if line[:1] in "╭│├╰┌└"]
        assert len({w.visible(line) for line in framed}) == 1


def test_columns_stay_aligned_with_colors(synthetic_state):
    state_dir, cfg_path, _ = synthetic_state
    plain = w.render(w.collect(state_dir, cfg_path, now=NOW), color=False).splitlines()
    colored = w.render(w.collect(state_dir, cfg_path, now=NOW), color=True).splitlines()
    assert [w.ANSI.sub("", line) for line in colored] == plain


def test_a_console_without_box_characters_gets_plain_borders(synthetic_state):
    state_dir, cfg_path, _ = synthetic_state
    screen = w.render(w.collect(state_dir, cfg_path, now=NOW), color=False, box=w.ASCII_BOX)
    assert screen.isascii() is False  # accents stay; only the borders change
    assert "+--------+" in screen and "| SYNA " in screen and "─" not in screen
    assert w.supports(w.UNICODE_BOX, type("S", (), {"encoding": "cp437"})()) is False
    assert w.supports(w.UNICODE_BOX, type("S", (), {"encoding": "utf-8"})()) is True


def test_a_silent_bot_a_missing_price_and_a_paused_day_are_visible(synthetic_state):
    state_dir, cfg_path, path = synthetic_state
    state = json.loads(path.read_text(encoding="utf-8"))["state"]
    state["strategy"].update(heartbeat_at=(NOW - timedelta(minutes=6)).isoformat(), risk_paused=True)
    del state["strategy"]["quotes"]["SYNA"]
    ExecutionController._write(path, state)
    screen = w.render(w.collect(state_dir, cfg_path, now=NOW), color=False)
    assert "SIN SEÑALES hace 6 min" in screen and "Freno diario activo" in screen and "sin precio" in screen


def test_the_viewer_never_writes_and_retries_a_locked_read(synthetic_state, tmp_path, monkeypatch):
    state_dir, cfg_path, path = synthetic_state
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    w.render(w.collect(state_dir, cfg_path, now=NOW), color=False)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    calls, pauses = [], []
    real = w.files.read_bytes

    def flaky(target, *args, **kwargs):
        calls.append(Path(target).name)
        if calls.count("execution-state.json") == 1:
            raise PermissionError("replacement in progress")
        return real(target, *args, **kwargs)

    monkeypatch.setattr(w.files, "read_bytes", flaky)
    assert w.read_json(path, sleep=pauses.append)["state"]["cash"] == "100000" and pauses == [0.2]


def test_old_prices_keep_every_border_in_place(synthetic_state):
    state_dir, cfg_path, _ = synthetic_state
    later = NOW + timedelta(hours=17, minutes=13)
    lines = w.render(w.collect(state_dir, cfg_path, now=later), color=True).splitlines()
    framed = [line for line in lines if line[:1] in "╭│├╰┌└"]
    assert len({w.visible(line) for line in framed}) == 1
    assert "17 h 16" in table(w.ANSI.sub("", "\n".join(lines)), "SYNA")
    # The next morning, before the start: waiting, not an alarm.
    assert "esperando el arranque de hoy · datos de la rueda del 14/10" in w.ANSI.sub("", "\n".join(lines))
    assert "SIN SEÑALES" not in w.ANSI.sub("", "\n".join(lines))
    assert w.pad("1033 min", 7) == "1033 m…" and w.pad("5 s", 7, ">") == "    5 s"


def test_empty_strategy_state_renders_cleanly(tmp_path):
    state_dir = tmp_path / "fresh"
    ExecutionController.prepare(state_dir, "FICTIONAL", cash="50000", fee_reserve="0.01")
    screen = w.render(w.collect(state_dir, STRATEGY_CONFIG, now=NOW), color=False)
    assert "sin sesión iniciada" in screen and "Sin posiciones abiertas." in screen


@pytest.mark.parametrize("seconds,text", [(45, "45 s"), (720, "12 min"), (61980, "17 h 13"), (200000, "2 d")])
def test_ages_are_short_at_any_size(seconds, text):
    assert w.age((NOW - timedelta(seconds=seconds)).isoformat(), NOW) == text


@pytest.mark.parametrize("value,text", [(Decimal("1234.4"), "$1.234"), (Decimal("-989.6"), "-$990"), (Decimal("0"), "$0")])
def test_pesos_use_the_argentine_format(value, text):
    assert w.ars(value) == text
    assert w.price(4567.5) == "4.567,5" and w.price(1234.0) == "1.234"
    # Stops keep one decimal on every row so the column lines up.
    assert [w.fixed(v) for v in (1234.0, 2345.6789, 3456.74, 7890.1)] == ["1.234,0", "2.345,7", "3.456,7", "7.890,1"]


def test_cli_watch_positions_reads_paper_session_without_writing(tmp_path, capsys):
    out = tmp_path / "paper-run"
    run_paper_session(STRATEGY_CONFIG, out, session=date(2026, 10, 14), days=2, seed=7)
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert main(["watch-positions", "--state-dir", str(out), "--once"]) == 0
    captured = capsys.readouterr()
    assert "ARGBOT · POSICIONES ABIERTAS" in captured.out
    assert "Disponible" in captured.out
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before
