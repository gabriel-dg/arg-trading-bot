"""Rule: a window round evaluates the whole universe and re-reads late candidates.

Without it, stocks without a position could go unevaluated in their window (cut by the
scan time limit, expired before the decision, or delivered late by the broker).
Fake market and broker, offline.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from test_operational_candidate import Broker, Clock, Market, config, rig, start  # noqa: F401
from argbot.strategy.candidate import ART, Candidate, validate_config
from argbot.execution import ExecutionController

RULE = json.loads(Path('tests/fixtures/window_scan.json').read_text(encoding='utf-8'))['window_scan']


def make(tmp_path, rule=True, lag=None, start_at=None, moving=False):
    """Each quote read takes one second of the bot's monotonic clock (and of the wall clock
    if `moving`); `lag(symbol, n)` ages a read."""
    cfg = config(str(tmp_path))
    cfg['opportunity_scan_seconds'] = 20  # as in the live configuration
    if rule:
        cfg['window_scan'] = deepcopy(RULE)
    clock = Clock(); market = Market(clock); broker = Broker(cfg)
    if start_at:
        clock.now = start_at
    ticks, reads = [0.0], Counter()
    original = market.quote
    def quote(symbol):
        ticks[0] += 1
        if moving:
            clock.now += timedelta(seconds=1)
        reads[(symbol, clock.now)] += 1
        raw = original(symbol)
        if lag and lag(symbol, reads[(symbol, clock.now)]):
            raw['fechaHora'] = (clock.now - timedelta(seconds=91)).isoformat()
        return raw
    market.quote = quote
    folder = tmp_path / 'candidate'
    ExecutionController.prepare(folder, cfg['account_id'], cash=cfg['assigned_capital'], fee_reserve=cfg['fee_reserve'])
    controller = ExecutionController(folder, cfg['account_id'], broker)
    bot = Candidate(controller, market, cfg, clock=clock, monotonic=lambda: ticks[0])
    start(bot, clock)
    return bot, broker, market, clock, folder


def kinds(folder):
    rows = [json.loads(l) for l in (folder / 'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()]
    return Counter(o['kind'] for r in rows for o in r.get('observations', []))


def test_repo_rule_and_its_validation():
    assert RULE == {'full_universe': True, 'max_candidate_refreshes': 8}
    cfg = config(); cfg['window_scan'] = deepcopy(RULE)
    validate_config(cfg)
    for bad in (dict(RULE, full_universe=1), dict(RULE, max_candidate_refreshes=-1),
                dict(RULE, max_candidate_refreshes=31), dict(RULE, extra=True), [], dict(full_universe=True)):
        cfg['window_scan'] = bad
        with pytest.raises(ValueError, match='window scan'):
            validate_config(cfg)


@pytest.mark.parametrize('rule', [True, False])
def test_a_window_round_reads_the_whole_universe_only_with_the_rule(tmp_path, rule):
    bot, broker, market, clock, folder = make(tmp_path, rule)
    universe = set(bot.config['universe'])
    bot.cycle()  # 11:20: the window is due
    read = set(market.calls)
    if rule:
        assert read == universe and kinds(folder)['window_full_scan'] == 1
    else:
        assert len(read) == 20 and kinds(folder)['opportunity_scan_yielded'] == 1


def test_rounds_that_decide_no_window_keep_the_20_second_cut(tmp_path):
    bot, broker, market, clock, folder = make(tmp_path)
    bot.cycle()  # window round: full scan
    for number in list(broker.orders):
        broker.fill(number)
    calls = len(market.calls)
    clock.advance(); bot.cycle()  # 11:25: 11:20 already used, no window due
    held = set(bot.execution.state['positions'])
    scanned = [s for s in market.calls[calls:] if s not in held]
    assert len(scanned) <= 20 and kinds(folder)['opportunity_scan_yielded'] >= 1


@pytest.mark.parametrize('rule', [True, False])
def test_candidates_read_late_are_read_once_more_before_deciding(tmp_path, rule):
    """Every first read of the round arrives 90 s late from IOL; a second read is fresh."""
    bot, broker, market, clock, folder = make(tmp_path, rule, lag=lambda symbol, n: n == 1)
    bot.cycle()
    bought = [o['symbol'] for o in broker.sent if o['side'] == 'BUY']
    if rule:
        refreshed = kinds(folder)['entry_candidates_refreshed']
        assert refreshed == 1 and bought  # re-read, now fresh, and decided
        reads = Counter(market.calls)
        assert sum(1 for n in reads.values() if n == 2) == RULE['max_candidate_refreshes']
        assert all(reads[s] == 2 for s in bought)
    else:
        assert not bought and kinds(folder)['entry_candidates_refreshed'] == 0


def test_a_refresh_is_never_a_second_chance_for_a_held_symbol(tmp_path):
    bot, broker, market, clock, folder = make(tmp_path, lag=lambda symbol, n: n == 1)
    bot.cycle()
    for number in list(broker.orders):
        broker.fill(number)
    clock.advance(); bot.cycle()  # reconciles the fills
    held = set(bot.execution.state['positions'])
    assert held
    bot.runtime['windows'] = []  # 11:20 still due, as after a deferred round
    calls = len(market.calls)
    clock.advance(); bot.cycle()
    rows = [json.loads(l) for l in (folder / 'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()]
    refreshed = [o for r in rows for o in r.get('observations', []) if o['kind'] == 'entry_candidates_refreshed'][-1]
    assert not set(refreshed['symbols']) & held and len(refreshed['symbols']) <= RULE['max_candidate_refreshes']
    # Holdings: a stop read and at most one management refresh; candidates: one re-read each.
    assert len(market.calls) - calls <= len(bot.config['universe']) + len(held) + RULE['max_candidate_refreshes']


@pytest.mark.parametrize('rule', [True, False])
def test_a_window_that_opens_mid_round_waits_for_the_next_full_round(tmp_path, rule):
    """A round starting at 11:19:50 reaches its entry decision after 11:20 (one second per read)."""
    bot, broker, market, clock, folder = make(tmp_path, rule, start_at=datetime(2026, 9, 30, 11, 19, 50, tzinfo=ART),
                                              moving=True)
    bot.cycle()
    assert clock.now.time() > datetime(2026, 9, 30, 11, 20).time()  # the decision came after 11:20
    if not rule:
        assert bot.runtime['windows'] == ['11:20']  # previous behavior: decided on the partial scan
        return
    assert bot.runtime['windows'] == [] and kinds(folder)['window_full_scan'] == 0
    clock.now = datetime(2026, 9, 30, 11, 24, 51, tzinfo=ART)
    bot.cycle()  # next round: the window was due at its start
    assert bot.runtime['windows'] == ['11:20'] and kinds(folder)['window_full_scan'] == 1


