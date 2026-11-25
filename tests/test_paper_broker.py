"""Paper broker, synthetic exchange, paper sessions and the live gate. Offline."""
from datetime import date, datetime
from decimal import Decimal
import json
import socket

import pytest

from argbot.brokers.base import Broker, MarketData
from argbot.brokers.paper import ART, PaperBroker, PaperMarket, SyntheticExchange
from argbot.cli import main
from argbot.live import CONFIRMATION, LiveTradingDisabled, require_live_opt_in
from argbot.paper_session import run_paper_session

CONFIG = "configs/examples/strategy.example.json"
DAY = date(2026, 10, 14)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network"))


def test_paper_objects_satisfy_the_protocols():
    ex = SyntheticExchange(["SYNA"], session=DAY)
    broker, market = PaperBroker(ex), PaperMarket(ex)
    for proto, obj in ((Broker, broker), (MarketData, market)):
        members = {n for n in vars(proto) if not n.startswith("_")}
        assert members and all(callable(getattr(obj, n)) for n in members)


def test_same_seed_same_prices():
    a = SyntheticExchange(["SYNA", "SYNB"], session=DAY, seed=3)
    b = SyntheticExchange(["SYNA", "SYNB"], session=DAY, seed=3)
    for _ in range(10):
        a.advance(); b.advance()
    assert a.quote_payload("SYNA") == b.quote_payload("SYNA")


def test_limit_buy_fills_at_its_limit_with_fees_and_updates_the_account():
    ex = SyntheticExchange(["SYNA"], session=DAY, cash="100000", fee_rate="0.01")
    broker = PaperBroker(ex)
    ask = Decimal(str(ex.quote_payload("SYNA")["puntas"][0]["precioVenta"]))
    limit = ask * 2  # marketable
    oid = broker.submit({"symbol": "SYNA", "side": "BUY", "quantity": 3, "limit": str(limit),
                         "created_at": datetime(2026, 10, 14, 10, 30, tzinfo=ART).isoformat()})
    assert broker.order(oid).status == "open"
    ex.advance()
    view = broker.order(oid)
    assert view.status == "filled" and view.filled_quantity == 3
    assert Decimal(view.gross) == 3 * limit and Decimal(view.fees) == (3 * limit * Decimal("0.01")).quantize(Decimal("0.01"))
    account = broker.account()
    assert account.positions == {"SYNA": 3} and not account.open_order_ids


def test_history_is_valid_for_the_session():
    ex = SyntheticExchange(["SYNA"], session=DAY)
    rows, excluded = PaperMarket(ex).history(["SYNA", "NOPE"], DAY, date(2026, 10, 13))
    assert len(rows["SYNA"]) >= 35 and excluded == {"NOPE": "unknown_symbol"}


def test_paper_session_runs_end_to_end_and_book_matches_the_broker(tmp_path):
    report = run_paper_session(CONFIG, tmp_path / "run", days=2)
    assert report["sessions"] == ["2026-10-14", "2026-10-15"]
    assert report["orders"] > 0 and report["book_matches_paper_broker"] is True
    assert report["real_orders"] == 0 and report["real_network_requests"] == 0
    assert json.loads((tmp_path / "run" / "report.json").read_text())["orders"] == report["orders"]


def test_paper_session_refuses_a_used_folder(tmp_path):
    (tmp_path / "x").mkdir(); (tmp_path / "x" / "file").write_text("")
    with pytest.raises(FileExistsError):
        run_paper_session(CONFIG, tmp_path / "x")


def test_live_is_disabled_without_the_environment_variable():
    with pytest.raises(LiveTradingDisabled):
        require_live_opt_in(environ={}, ask=lambda _p: CONFIRMATION, interactive=True)
    with pytest.raises(LiveTradingDisabled):
        require_live_opt_in(environ={"LIVE_TRADING": "1"}, ask=lambda _p: CONFIRMATION, interactive=True)


def test_live_needs_the_exact_typed_confirmation_in_a_terminal():
    env = {"LIVE_TRADING": "true"}
    with pytest.raises(LiveTradingDisabled):
        require_live_opt_in(environ=env, ask=lambda _p: "yes", interactive=True)
    with pytest.raises(LiveTradingDisabled):
        require_live_opt_in(environ=env, ask=lambda _p: CONFIRMATION, interactive=False)
    require_live_opt_in(environ=env, ask=lambda _p: CONFIRMATION, interactive=True)


def test_cli_live_without_opt_in_sends_nothing(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    with pytest.raises(SystemExit):
        main(["live", "--config", CONFIG, "--state-dir", "unused"])


def test_cli_check_config():
    assert main(["check-config", "--config", CONFIG]) == 0
