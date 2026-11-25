"""Sector concentration bound. Fake market and broker, offline."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_operational_candidate import Broker, Clock, Market, config, start
from argbot.strategy.candidate import Candidate, sector_full, validate_config
from argbot.execution import ExecutionController

LIMITS = json.loads(Path('tests/fixtures/sector_limits.json').read_text(encoding='utf-8'))['sector_limits']


def limited(account, max_positions=2, sectors=None):
    cfg = config(account)
    cfg['sector_limits'] = dict(max_positions=max_positions, sectors=sectors or deepcopy(LIMITS['sectors']))
    return cfg


def make_rig(tmp_path, cfg):
    clock = Clock(); market = Market(clock); broker = Broker(cfg)
    folder = tmp_path / 'candidate'
    ExecutionController.prepare(folder, cfg['account_id'], cash=cfg['assigned_capital'], fee_reserve=cfg['fee_reserve'])
    controller = ExecutionController(folder, cfg['account_id'], broker)
    bot = Candidate(controller, market, cfg, clock=clock)
    start(bot, clock)
    return bot, controller, broker, market, clock, folder


def audit(folder):
    return [o for line in (folder / 'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()
            for o in json.loads(line).get('observations', [])]


def test_repo_sector_map_covers_the_universe_and_validates():
    cfg = limited('synthetic-account')
    assert set(LIMITS['sectors']) == set(cfg['universe'])
    validate_config(cfg)


@pytest.mark.parametrize('fault', ['missing_symbol', 'extra_symbol', 'zero', 'not_int', 'extra_key', 'blank'])
def test_invalid_sector_limits_fail_closed(fault):
    cfg = limited('synthetic-account')
    limits = cfg['sector_limits']
    if fault == 'missing_symbol': limits['sectors'].pop('QA0')
    elif fault == 'extra_symbol': limits['sectors']['ZZZ'] = 'otro'
    elif fault == 'zero': limits['max_positions'] = 0
    elif fault == 'not_int': limits['max_positions'] = 2.0
    elif fault == 'extra_key': limits['note'] = 'x'
    else: limits['sectors']['QA0'] = ' '
    with pytest.raises(ValueError, match='sector limits'):
        validate_config(cfg)


def test_absent_limits_keep_previous_behavior():
    cfg = config('synthetic-account')
    assert 'sector_limits' not in cfg
    assert not sector_full(cfg, 'QA0', set(cfg['universe']))


def test_one_window_never_exceeds_the_sector_bound(tmp_path):
    cfg = limited(str(tmp_path), sectors={s: 'uno' for s in config('x')['universe']})
    bot, c, b, m, clock, folder = make_rig(tmp_path, cfg)
    bot.cycle()
    assert len(b.sent) == 2
    skipped = [o for o in audit(folder) if o['kind'] == 'entry_not_submitted' and o['reason'] == 'sector_cap']
    assert skipped


def test_full_sector_lets_other_sectors_take_the_free_slots(tmp_path):
    universe = config('x')['universe']
    sectors = {s: ('uno' if i % 2 == 0 else 'dos') for i, s in enumerate(universe)}
    cfg = limited(str(tmp_path), sectors=sectors)
    bot, c, b, m, clock, folder = make_rig(tmp_path, cfg)
    bot.cycle()
    picked = [o['symbol'] for o in b.sent if o['side'] == 'BUY']
    assert len(picked) == 4  # two per sector, below the five slots
    assert sorted(sectors[s] for s in picked) == ['dos', 'dos', 'uno', 'uno']
    clock.advance(); bot.cycle()
    assert len(b.sent) == 4
