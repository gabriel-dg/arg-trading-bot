"""Liquidity bound for NEW entries. Fake market and broker, offline."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from test_operational_candidate import Broker, Clock, Market, config, rig, start  # noqa: F401
from argbot.strategy.candidate import ART, Candidate, liquid_enough, validate_config
from argbot.execution import ExecutionController

RULE = json.loads(Path('tests/fixtures/entry_liquidity.json').read_text(encoding='utf-8'))['entry_liquidity']


def with_rule(account, **changes):
    cfg = config(account)
    cfg['entry_liquidity'] = dict(RULE, **changes)
    return cfg


def test_fixture_rule_is_trades_per_minute_since_the_open_and_validates():
    assert RULE == {'min_trades_per_minute': 1.5, 'since': '10:30'}
    validate_config(with_rule('synthetic-account'))


@pytest.mark.parametrize('rule', [dict(min_trades_per_minute=0), dict(min_trades_per_minute=-1),
    dict(min_trades_per_minute=True), dict(min_trades_per_minute='2'), dict(since='11:30'),
    dict(since='bad'), dict(extra=1)])
def test_invalid_liquidity_rule_fails_closed(rule):
    cfg = with_rule('synthetic-account')
    cfg['entry_liquidity'].update(rule)
    with pytest.raises(ValueError, match='entry liquidity'):
        validate_config(cfg)


@pytest.mark.parametrize('trades,minute,expected', [(170, 120, False), (180, 120, True), (300, 120, True),
                                                     (None, 120, False), ('x', 120, False), (50, 0, False)])
def test_trades_per_minute_since_the_open(trades, minute, expected):
    cfg = with_rule('synthetic-account')
    now = datetime(2026, 10, 8, 10, 30, tzinfo=ART).replace(hour=10 + (30 + minute) // 60, minute=(30 + minute) % 60)
    cot = {} if trades is None else {'cantidadOperaciones': trades}
    assert liquid_enough(cfg, cot, now) is expected


def test_absent_rule_keeps_previous_behavior():
    cfg = config('synthetic-account')
    assert 'entry_liquidity' not in cfg
    assert liquid_enough(cfg, {}, datetime(2026, 10, 8, 11, 15, tzinfo=ART))


def make_rig(tmp_path, cfg, trades):
    clock = Clock(); market = Market(clock); broker = Broker(cfg)
    original = market.quote
    def quote(symbol):
        raw = original(symbol)
        raw['cantidadOperaciones'] = trades(symbol)
        return raw
    market.quote = quote
    folder = tmp_path / 'candidate'
    ExecutionController.prepare(folder, cfg['account_id'], cash=cfg['assigned_capital'], fee_reserve=cfg['fee_reserve'])
    controller = ExecutionController(folder, cfg['account_id'], broker)
    bot = Candidate(controller, market, cfg, clock=clock)
    start(bot, clock)
    return bot, broker, clock, folder


def test_only_liquid_candidates_are_bought_and_the_review_says_why(tmp_path):
    universe = config('x')['universe']
    thin = set(universe[::2])  # every other symbol trades too little
    cfg = with_rule(str(tmp_path))
    # Clock starts at 11:20: 50 minutes since 10:30, so the bound is 75 trades.
    bot, broker, clock, folder = make_rig(tmp_path, cfg, lambda s: 40 if s in thin else 400)
    bot.cycle()
    bought = {o['symbol'] for o in broker.sent if o['side'] == 'BUY'}
    assert bought and not bought & thin

def test_held_positions_are_never_filtered(tmp_path):
    cfg = with_rule(str(tmp_path))
    bot, broker, clock, folder = make_rig(tmp_path, cfg, lambda s: 400)
    bot.cycle()
    for number in list(broker.orders):
        broker.fill(number)
    clock.advance(); bot.cycle()
    held = set(bot.execution.state['positions'])
    assert held
    trades = {'value': 0}
    original = bot.market.quote
    bot.market.quote = lambda s: dict(original(s), cantidadOperaciones=trades['value'])
    clock.advance(); bot.cycle()
    assert bot.runtime['valuation_available'] is True  # illiquid holdings are still managed
    assert set(bot.execution.state['positions']) == held


