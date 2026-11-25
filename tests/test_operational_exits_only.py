"""An order the ledger refuses must not leave the other holdings' stops unchecked."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

from test_operational_candidate import rig
from argbot.strategy.candidate import config_hash


def rounds(folder):
    rows = [json.loads(l) for l in (folder/'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()]
    return [r for r in rows if r.get('phase') == 'completed']


def stop_checks(row):
    return {o['symbol'] for o in row['observations'] if o['kind'] == 'stop_check' and o['fresh']}


def refused_order(rig):
    """Five full buys and one 2-share partial that the broker then reports as complete."""
    bot,c,b,m,clock,cfg,folder=rig
    bot.cycle()
    for n in ['1','2','3','4','5']:
        b.fill(n)
    b.fill('6', 2)
    clock.advance();bot.cycle()
    assert c.state['last_reconciliation']['ok'] and len(c.state['positions']) == 6
    b.orders['6'] = replace(b.orders['6'], status='filled')
    return b.orders['6'].symbol


def test_an_order_we_refuse_leaves_the_other_stops_working_and_sells_on_a_stop(rig):
    bot,c,b,m,clock,cfg,folder=rig
    held = refused_order(rig)
    others = set(c.state['positions']) - {held}
    clock.advance();bot.cycle()
    last = rounds(folder)[-1]
    assert stop_checks(last) == others and not c.state['last_reconciliation']['ok']
    assert bot.runtime['status'] == 'blocked' and bot.runtime['incidents'][-1]['code'] == 'ValueError'
    assert bot.runtime['entries_blocked'] == 'unresolved_order'
    assert any(o['kind'] == 'exits_only' and o['symbols'] == [held] for o in last['observations'])
    assert bot.runtime['exits_only']['symbols'] == [held]
    first = b.orders['1'].symbol
    m.prices[first] = max(1, c.state['positions'][first]['swing']['stop_nivel'] - 1)
    clock.advance();bot.cycle()
    assert len(b.sent) == 6
    clock.advance();bot.cycle()
    assert len(b.sent) == 6
    clock.advance();bot.cycle()
    assert len(b.sent) == 7 and b.sent[-1]['side'] == 'SELL' and b.sent[-1]['symbol'] == first
    assert b.sent[-1]['quantity'] == b.orders['1'].requested_quantity
    b.fill('7');clock.advance();bot.cycle()
    assert first not in c.state['positions'] and held in c.state['positions']
    assert '6' not in b.cancels and not any(o['side'] == 'BUY' for o in b.sent[6:])
    b.orders['6'] = replace(b.orders['6'], status='cancelled')  # the broker's record reads cleanly
    clock.advance();bot.cycle()
    assert c.state['last_reconciliation']['ok'] and bot.runtime['status'] == 'running'
    assert stop_checks(rounds(folder)[-1]) == set(c.state['positions'])
    assert bot.runtime['exits_only'] is None  # a clean round clears the restart evidence


def test_the_set_aside_symbol_is_never_sold_or_cancelled(rig):
    bot,c,b,m,clock,cfg,folder=rig
    held = refused_order(rig)
    m.prices[held] = max(1, c.state['positions'][held]['swing']['stop_nivel'] - 1)
    for _ in range(3):
        clock.advance();bot.cycle()
        assert held not in stop_checks(rounds(folder)[-1])
    assert len(b.sent) == 6 and not b.cancels


def test_a_recovery_pass_also_checks_the_other_stops(rig):
    bot,c,b,m,clock,cfg,folder=rig
    additions = dict(managed_recovery={'delay_seconds':30,'max_attempts_per_cycle':1})
    cfg.update(deepcopy(additions));bot.config.update(additions)
    bot.runtime['config_hash'] = config_hash(cfg);bot._save()
    held = refused_order(rig)
    missing = b.orders['1'].symbol
    m.bad.add(missing)
    clock.advance();bot.cycle()
    assert missing not in stop_checks(rounds(folder)[-1])
    assert bot.runtime['entries_blocked'] == 'managed_quote_missing'
    m.bad.remove(missing);clock.now += timedelta(seconds=30);bot.cycle()
    last = rounds(folder)[-1]
    assert last['cycle_kind'] == 'managed_recovery' and missing in stop_checks(last)
    assert held not in stop_checks(last) and len(b.sent) == 6
