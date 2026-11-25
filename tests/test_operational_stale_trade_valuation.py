"""Rule: a holding with an old last trade is valued with its live book.

Fake market and broker, offline. QA9 traded ~0.7 times a minute: 27 reads that day had a
last trade older than 300 s, all with a valid book, and each one blocked every new entry.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from test_operational_candidate import Broker, Clock, Market, config, rig, start  # noqa: F401
from argbot.strategy.candidate import ART, Candidate, validate_config
from argbot.execution import ExecutionController

RULE = json.loads(Path('tests/fixtures/stale_trade_valuation.json').read_text(encoding='utf-8'))['stale_trade_valuation']


def make(tmp_path, rule=True, **extra):
    cfg = config(str(tmp_path))
    cfg.update(max_exposure_pct='1', **extra)
    if rule:
        cfg['stale_trade_valuation'] = deepcopy(RULE if rule is True else rule)
    clock = Clock(); market = Market(clock); broker = Broker(cfg)
    folder = tmp_path / 'candidate'
    ExecutionController.prepare(folder, cfg['account_id'], cash=cfg['assigned_capital'], fee_reserve=cfg['fee_reserve'])
    controller = ExecutionController(folder, cfg['account_id'], broker)
    bot = Candidate(controller, market, cfg, clock=clock)
    start(bot, clock)
    return bot, broker, market, clock


def hold(bot, broker, clock, keep=None):
    """First window buys; fill `keep` orders (default all) and let the rest time out."""
    bot.cycle()
    numbers = sorted(broker.orders, key=int)
    for number in numbers[:keep]:
        broker.fill(number)
    clock.advance(); bot.cycle()
    return sorted(bot.execution.state['positions'])


def old_trade(market, clock, symbols, *, minutes=12, bid=None, ask=None, days=0):
    """Same quote as the fake market, but its last trade is old; the book can move."""
    original = market.quote
    def quote(symbol):
        raw = original(symbol)
        if symbol in symbols:
            raw['fechaHora'] = (clock() - timedelta(days=days, minutes=minutes)).isoformat()
            if bid is not None:
                raw['puntas'] = [dict(precioCompra=bid, precioVenta=ask, cantidadCompra=500, cantidadVenta=500)]
        return raw
    market.quote = quote


def test_repo_rule_covers_the_whole_session_and_validates():
    assert RULE == {'max_trade_age_seconds': 18000, 'before_first_trade': 'previous_close'}
    cfg = config(); cfg['stale_trade_valuation'] = deepcopy(RULE)
    validate_config(cfg)


@pytest.mark.parametrize('rule', [dict(max_trade_age_seconds=300), dict(max_trade_age_seconds=8*3600+1),
                                  dict(max_trade_age_seconds='18000'), dict(max_trade_age_seconds=True),
                                  dict(max_trade_age_seconds=18000, extra=1), []
                                  , dict(max_trade_age_seconds=18000, before_first_trade='open')
                                  , dict(before_first_trade='previous_close')])
def test_invalid_rule_fails_closed(rule):
    cfg = config(); cfg['stale_trade_valuation'] = rule
    with pytest.raises(ValueError, match='stale trade valuation'):
        validate_config(cfg)


def test_without_the_rule_an_old_trade_still_blocks_entries(tmp_path):
    bot, broker, market, clock = make(tmp_path, rule=False)
    held = hold(bot, broker, clock)
    old_trade(market, clock, {held[0]})
    clock.advance(); bot.cycle()
    assert bot.runtime['entries_blocked'] == 'managed_quote_missing'
    assert bot.runtime['valuation_available'] is False and bot.runtime['managed_book_valued'] == []


def test_old_trade_with_live_book_is_valued_at_that_bid_and_never_traded(tmp_path):
    bot, broker, market, clock = make(tmp_path)
    held = hold(bot, broker, clock)
    symbol = held[0]
    before = deepcopy(bot.runtime['quotes'][symbol])
    old_trade(market, clock, {symbol}, bid=88.5, ask=88.9)
    sent = len(broker.sent)
    clock.advance(); bot.cycle()
    assert bot.runtime['valuation_available'] is True and bot.runtime['entries_blocked'] is None
    assert bot.runtime['managed_book_valued'] == [symbol]
    # Stops, VWAP and runtime quotes never see the book mark.
    assert bot.runtime['quotes'][symbol] == before
    assert not any(o['side'] == 'SELL' for o in broker.sent[sent:])
    state = bot.execution.state
    reserve = Decimal(bot.config['fee_reserve'])
    expected = Decimal(state['cash']) + sum(Decimal(o['hold']) for o in state['orders'].values())
    for name, position in state['positions'].items():
        bid = Decimal('88.5') if name == symbol else Decimal(bot.runtime['quotes'][name]['bid'])
        expected += position['quantity'] * bid * (1 - reserve)
    assert Decimal(bot.runtime['equity']) == expected


@pytest.mark.parametrize('case', ['yesterday', 'wide_book', 'crossed_book', 'too_old'])
def test_no_valid_same_day_book_still_blocks(tmp_path, case):
    # 'yesterday' blocks only under the earlier shape of the rule, without before_first_trade.
    bot, broker, market, clock = make(tmp_path, rule=dict(max_trade_age_seconds=18000))
    held = hold(bot, broker, clock)
    if case == 'yesterday':
        old_trade(market, clock, {held[0]}, days=1)
    elif case == 'wide_book':
        old_trade(market, clock, {held[0]}, bid=80, ask=90)
    elif case == 'crossed_book':
        old_trade(market, clock, {held[0]}, bid=90, ask=89)
    else:
        old_trade(market, clock, {held[0]}, minutes=6*60+1)
    clock.advance(); bot.cycle()
    assert bot.runtime['entries_blocked'] == 'managed_quote_missing' and bot.runtime['managed_book_valued'] == []


def test_before_its_first_trade_a_holding_is_worth_its_previous_close(tmp_path):
    """A holding with no trade yet in the morning must not block every entry."""
    bot, broker, market, clock = make(tmp_path)
    held = hold(bot, broker, clock)
    symbol = held[0]
    old_trade(market, clock, {symbol}, days=1)  # IOL still shows yesterday's last trade
    sent = len(broker.sent)
    clock.advance(); bot.cycle()
    close = bot.rows[symbol][-1]['cierre']
    assert bot._book_marks[symbol]['basis'] == 'previous_close' and bot._book_marks[symbol]['bid'] == str(close)
    assert bot.runtime['entries_blocked'] is None and bot.runtime['managed_book_valued'] == [symbol]
    assert not any(o['side'] == 'SELL' for o in broker.sent[sent:])  # the stop still waits for a trade
    state = bot.execution.state
    reserve = Decimal(bot.config['fee_reserve'])
    expected = Decimal(state['cash']) + sum(Decimal(o['hold']) for o in state['orders'].values())
    for name, position in state['positions'].items():
        price = Decimal(str(close)) if name == symbol else Decimal(bot.runtime['quotes'][name]['bid'])
        expected += position['quantity'] * price * (1 - reserve)
    assert Decimal(bot.runtime['equity']) == expected


def test_a_later_failed_read_of_the_symbol_drops_its_mark(tmp_path):
    bot, broker, market, clock = make(tmp_path)
    held = hold(bot, broker, clock)
    symbol = held[0]
    old_trade(market, clock, {symbol}, bid=88.5, ask=88.9)
    clock.advance()
    assert bot._fetch(symbol) is None and symbol in bot._book_marks
    market.bad.add(symbol)
    assert bot._fetch(symbol) is None and symbol not in bot._book_marks
    quotes = {s: q for s, q in bot.runtime['quotes'].items() if s != symbol}
    assert bot._risk(quotes) is False and bot.runtime['entries_blocked'] == 'managed_quote_missing'


def carried_holding(tmp_path, rule):
    """One holding bought yesterday (the other orders cancelled), next day's 11:20 window."""
    bot, broker, market, clock = make(tmp_path, rule=rule)
    bot.cycle()
    numbers = sorted(broker.orders, key=int)
    broker.fill(numbers[0])
    for number in numbers[1:]:
        broker.cancel(number)
    clock.advance(); bot.cycle()
    held = sorted(bot.execution.state['positions'])
    clock.now = clock.now.replace(hour=11, minute=20) + timedelta(days=1)
    start(bot, clock)
    old_trade(market, clock, set(held), bid=88.5, ask=88.9)
    return bot, broker, held


@pytest.mark.parametrize('rule', [True, False])
def test_a_window_buys_while_the_holding_is_valued_from_its_book(tmp_path, rule):
    bot, broker, held = carried_holding(tmp_path, rule)
    sent = len(broker.sent)
    bot.cycle()
    bought = [o['symbol'] for o in broker.sent[sent:] if o['side'] == 'BUY']
    if rule:
        assert len(bought) == 5 and held[0] not in bought
        assert bot.runtime['managed_book_valued'] == held and bot._risk_marks[held[0]]['ask'] == '88.9'
        # Recorded, not an incident: an incident would show as a critical popup.
        assert bot.runtime['status'] == 'running' and not bot.runtime['incidents']
    else:
        assert not bought and bot.runtime['entries_blocked'] == 'managed_quote_missing'
        assert bot.runtime['incidents'][-1]['code'] == 'managed_quote_unavailable'


def test_the_daily_brake_trips_on_a_real_drop_of_the_book(tmp_path):
    bot, broker, market, clock = make(tmp_path)
    held = hold(bot, broker, clock)
    assert len(held) == 6
    # About 60% invested: a 6% lower bid on every holding is more than the 3% brake.
    old_trade(market, clock, set(held), bid=84.5, ask=84.8)
    clock.advance(); bot.cycle()
    assert sorted(bot.runtime['managed_book_valued']) == held and bot.runtime['risk_paused'] is True


def test_recovery_passes_still_recheck_a_book_valued_holding(tmp_path):
    # The live configuration has bounded recovery: 30 s, two passes per cycle.
    bot, broker, market, clock = make(tmp_path, managed_recovery=dict(delay_seconds=30, max_attempts_per_cycle=2))
    held = hold(bot, broker, clock)
    old_trade(market, clock, {held[0]}, bid=88.5, ask=88.9)
    clock.advance(); bot.cycle()
    assert bot.runtime['entries_blocked'] is None and bot.runtime['managed_book_valued'] == [held[0]]
    calls = len(market.calls)
    clock.now += timedelta(seconds=bot.config['managed_recovery']['delay_seconds'] + 5)
    bot.cycle()
    assert held[0] in market.calls[calls:]
    assert bot.runtime['managed_recovery_attempt']['number'] == 1


def test_a_recovery_pass_for_a_book_valued_holding_never_uses_an_entry_window(tmp_path):
    bot, broker, market, clock = make(tmp_path, managed_recovery=dict(delay_seconds=30, max_attempts_per_cycle=2))
    held = hold(bot, broker, clock)
    old_trade(market, clock, {held[0]}, bid=88.5, ask=88.9)
    clock.now = clock.now.replace(hour=13, minute=44, second=40)
    bot.cycle()  # normal round just before the 13:45 window
    assert bot.runtime['managed_book_valued'] == [held[0]] and '13:45' not in bot.runtime['windows']
    clock.now = clock.now.replace(minute=45, second=15)
    bot.cycle()  # recovery pass: candidate reads are 35 s old, still fresh for an entry
    assert bot.runtime['managed_recovery_attempt']['number'] == 1
    assert '13:45' not in bot.runtime['windows']  # left for the full scan of the next round
    clock.now = clock.now.replace(minute=49, second=40)
    bot.cycle()
    assert '13:45' in bot.runtime['windows']


def observations(tmp_path, kind):
    path = tmp_path / 'candidate' / 'operational-cycles.private.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    return [o for r in rows for o in r.get('observations', []) if o['kind'] == kind]


@pytest.mark.parametrize('bid,recorded', [(60, True), (88.5, False)])
def test_a_bid_under_the_stop_is_only_recorded_never_sold(tmp_path, bid, recorded):
    """Option B: the stop keeps waiting for a trade; the bid is only measured."""
    bot, broker, market, clock = make(tmp_path)
    held = hold(bot, broker, clock)
    symbol = held[0]
    old_trade(market, clock, {symbol}, bid=bid, ask=bid + 0.3)
    sent = len(broker.sent)
    clock.advance(); bot.cycle()
    assert bot.runtime['managed_book_valued'] == [symbol]
    assert not any(o['side'] == 'SELL' for o in broker.sent[sent:])
    marks = observations(tmp_path, 'stop_below_on_book')
    if recorded:
        assert [m['symbol'] for m in marks] == [symbol]
        assert Decimal(marks[0]['bid']) < Decimal(marks[0]['stop']) and marks[0]['last'] == '90.0'
    else:
        assert not marks


