"""Rule: a cash difference never stops the bot. Fake market and broker, offline."""
from datetime import datetime, timezone
from decimal import Decimal
import json

from test_operational_candidate import Broker, Clock, Market, config, start
from test_operational_sector_limits import audit
from argbot.strategy.candidate import Candidate
from argbot.execution import ExecutionController


def make_rig(tmp_path, settlement):
    """A book of 100,000 that already counts a sale of 89,100 net which IOL has not paid."""
    cfg = config(str(tmp_path)); clock = Clock(); market = Market(clock); broker = Broker(cfg)
    folder = tmp_path / 'candidate'
    ExecutionController.prepare(folder, cfg['account_id'], cash=cfg['assigned_capital'], fee_reserve=cfg['fee_reserve'])
    path = folder / 'execution-state.json'
    state = json.loads(path.read_text(encoding='utf-8'))['state']
    state['orders']['sold'] = dict(local_id='sold', symbol='ZZZ', side='SELL', quantity=10, limit='9000',
        created_at=datetime.now(timezone.utc).isoformat(), reason='mechanical_stop', state='filled',
        broker_id='900', filled=10, gross='90000', fees='900', hold='0', broker_settlement=settlement)
    ExecutionController._write(path, state)
    broker.cash -= Decimal('89100')
    controller = ExecutionController(folder, cfg['account_id'], broker)
    bot = Candidate(controller, market, cfg, clock=clock)
    start(bot, clock)
    return bot, controller, broker, folder


def test_a_sale_at_48_hours_not_yet_paid_buys_with_iols_amount(tmp_path):
    bot, c, b, folder = make_rig(tmp_path, 'a48horas')
    try:
        bot.cycle()
        buys = [o for o in b.sent if o['side'] == 'BUY']
        assert sum(Decimal(o['hold']) for o in buys) <= Decimal('10900')
        # The second buy is sized from the broker's cash left, not from the book's.
        assert [(o['symbol'], o['quantity']) for o in buys] == [('QA0', 99), ('QA1', 21)]
        assert not bot.runtime.get('incidents')
        cash = c.state['last_reconciliation']['cash']
        assert cash['buys_allowed'] is True and Decimal(cash['unsettled_sales']) == Decimal('89100')
        assert c.state['last_reconciliation']['ok'] is True
    finally:
        c.close()


def test_the_same_shortfall_unexplained_stops_buys_only(tmp_path):
    bot, c, b, folder = make_rig(tmp_path, 'a24horas')
    try:
        bot.cycle()
        assert not b.sent and c.state['last_reconciliation']['ok'] is True
        assert c.state['last_reconciliation']['cash']['buys_allowed'] is False
        skipped = [o for o in audit(folder) if o['kind'] == 'entry_not_submitted']
        assert skipped and {o['reason'] for o in skipped} == {'available_cash_shortfall'}
        assert not bot.runtime.get('incidents')
    finally:
        c.close()
