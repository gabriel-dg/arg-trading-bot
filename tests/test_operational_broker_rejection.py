"""A BUY the broker refused explicitly.

The refusal's masked text is kept on the order; the symbol is left out for the rest of
the day and the same window tries the next candidate, at most two refusals per window.
A refusal before the network (`not_sent`) keeps its per-round behaviour. Fake market and
broker only: no credentials, network or model calls.
"""
import json
from datetime import timedelta
from decimal import Decimal

from test_operational_candidate import rig, raw_quote, start  # noqa: F401  (rig es fixture)
from argbot.strategy.candidate import BROKER_REJECTED_TODAY, MAX_BROKER_REJECTIONS_PER_WINDOW
from argbot.execution import BrokerRejected, RequestNotSent
from argbot.brokers.iol.adapter import broker_message
from argbot.market import quote

REASON = 'La orden a ingresar está relacionada a un instrumento no acorde con tu perfil'


def refusing(rig, monkeypatch, how_many):
    """The broker refuses the first `how_many` BUYs it receives, then accepts."""
    b = rig[2]
    original, refused = b.submit, []
    def submit(order):
        if order['side'] == 'BUY' and len(refused) < how_many:
            refused.append(order['symbol'])
            raise BrokerRejected(detail=REASON)
        return original(order)
    monkeypatch.setattr(b, 'submit', submit)
    return refused


def observations(folder):
    lines = (folder / 'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()
    return [o for line in lines for o in json.loads(line).get('observations', [])]


def test_refused_buy_frees_the_slot_for_the_next_candidate_in_the_same_window(rig, monkeypatch):
    bot, c, b, m, clock, cfg, folder = rig
    refused = refusing(rig, monkeypatch, 1)
    bot.cycle()  # 11:20: the window fills every free slot
    symbol = refused[0]
    buys = [o['symbol'] for o in b.sent if o['side'] == 'BUY']
    # Without the second pass the window would end one slot short.
    assert len(buys) == cfg['strategy']['max_concurrent'] and symbol not in buys
    order = next(o for o in c.state['orders'].values() if o['symbol'] == symbol)
    assert order['state'] == 'rejected' and order['broker_message'] == REASON
    assert symbol in bot.runtime['broker_rejected_today']
    assert bot.runtime['exclusions'][symbol] == BROKER_REJECTED_TODAY
    seen = observations(folder)
    rejected = [o for o in seen if o['kind'] == 'order_rejected']
    assert [(o['symbol'], o['side'], o['message']) for o in rejected] == [(symbol, 'BUY', REASON)]
    retry = [o for o in seen if o['kind'] == 'entry_retry_after_rejection']
    assert len(retry) == 1 and symbol not in retry[0]['next']
    assert c.reconcile()


def test_at_most_two_refusals_per_window(rig, monkeypatch):
    bot, c, b, m, clock, cfg, folder = rig
    refused = refusing(rig, monkeypatch, 99)
    bot.cycle()
    assert len(refused) == MAX_BROKER_REJECTIONS_PER_WINDOW == 2
    assert not b.sent  # the fake records only accepted submissions
    assert set(bot.runtime['broker_rejected_today']) == set(refused)
    kinds = [o['kind'] for o in observations(folder)]
    assert kinds.count('order_rejected') == 2 and 'entry_window_rejections_limit' in kinds
    assert all(o['state'] == 'rejected' for o in c.state['orders'].values())
    assert Decimal(c.state['cash']) == Decimal(cfg['assigned_capital'])  # every hold released




def test_a_new_day_clears_the_refusals(rig, monkeypatch):
    bot, c, b, m, clock, cfg, folder = rig
    refusing(rig, monkeypatch, 1)
    bot.cycle()
    assert bot.runtime['broker_rejected_today']
    clock.now += timedelta(days=1)
    start(bot, clock)
    assert bot.runtime['broker_rejected_today'] == {}


def test_refusal_before_the_network_is_not_a_day_exclusion(rig, monkeypatch):
    """An expired quote is refused before the POST; it may go next round."""
    bot, c, b, m, clock, cfg, _ = rig
    s = cfg['universe'][0]
    cot = quote(raw_quote(s, clock), s, clock(), max_age=60, max_spread=cfg['max_spread_pct'])
    monkeypatch.setattr(b, 'submit', lambda order: (_ for _ in ()).throw(
        RequestNotSent('Quote expired during reconciliation or authentication')))
    bot._cycle_audit = {'observations': []}
    assert bot._send(s, 'BUY', 1, cot['ask'], cot, 'synthetic') is None
    assert s not in (bot.runtime.get('broker_rejected_today') or {})
    assert bot.runtime['exclusions'][s] == 'order_not_sent'


def test_refused_sell_is_not_excluded_and_is_retried_as_before(rig, monkeypatch):
    bot, c, b, m, clock, cfg, _ = rig
    s = cfg['universe'][0]
    cot = quote(raw_quote(s, clock), s, clock(), max_age=60, max_spread=cfg['max_spread_pct'])
    monkeypatch.setattr(b, 'submit', lambda order: (_ for _ in ()).throw(BrokerRejected(detail=REASON)))
    bot._cycle_audit = {'observations': []}
    state = c.state
    state['positions'][s] = {'quantity': 1, 'cost': '90', 'opened_at': clock().isoformat()}
    c._commit(state)
    b.positions[s] = 1  # the broker holds it too, so the reconciliation agrees
    assert bot._send(s, 'SELL', 1, cot['bid'], cot, 'synthetic') == 'rejected'
    assert s not in (bot.runtime.get('broker_rejected_today') or {})


def test_broker_message_masks_numbers_and_mail_and_is_capped():
    rows = [{'description': '  Cuenta 12345678  sin   perfil '}, {'description': 'avisar a x@y.com'},
            {'description': ''}, {'other': 'ignored'}, 'not a row']
    assert broker_message(rows) == 'Cuenta [numero] sin perfil; avisar a [correo]'
    assert broker_message([{'description': 'monto 12345 ok'}]) == 'monto 12345 ok'
    assert broker_message([]) is None and broker_message([{'description': '  '}]) is None
    assert len(broker_message([{'description': 'x' * 500}])) == 200


