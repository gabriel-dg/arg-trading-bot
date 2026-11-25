"""Synthetic orders only: bounded observation gaps and fresh equal-source reads."""
from copy import deepcopy
from datetime import timedelta
import pytest
from test_operational_candidate import rig, raw_quote, start
from argbot.strategy.candidate import Candidate, MechanicalRules, config_hash, validate_config
from argbot.execution import QUOTE_EXPIRED, ExecutionBlocked, ExecutionController, RequestNotSent
from argbot.market import quote


@pytest.fixture
def managed(rig, monkeypatch):
    bot, controller, broker, market, clock, cfg, folder = rig
    monkeypatch.setattr(MechanicalRules, '_trailing_stop_nivel', lambda *a: 85)
    bot.cycle(); broker.fill('1')
    for number in list(broker.orders):
        if number != '1': broker.cancel(number)
    clock.advance(); bot.cycle()
    controller.pause(True)
    cfg['stop_confirmation_max_gap_seconds'] = 660
    bot.config = deepcopy(cfg)
    bot.runtime['config_hash'] = config_hash(cfg); bot._save()
    symbol = broker.orders['1'].symbol
    return rig, symbol


def observe(managed, *, seconds=0, price=80, missing=False, age=0):
    rig, symbol = managed
    bot, controller, broker, market, clock, cfg, folder = rig
    clock.now += timedelta(seconds=seconds)
    bot.runtime['last_cycle'] = clock().isoformat()
    quotes = {}
    if not missing:
        raw = raw_quote(symbol, clock, price)
        raw['fechaHora'] = (clock()-timedelta(seconds=age)).isoformat()
        quotes[symbol] = quote(raw, symbol, clock(), max_age=60, max_spread=cfg['max_spread_pct'])
    bot._stops(quotes, {symbol})
    return controller.state['positions'][symbol]['swing']


def sales(managed):
    return [o for o in managed[0][2].sent if o['side'] == 'SELL']


def test_a_stop_sale_refused_before_the_network_sells_on_the_next_breach(managed, monkeypatch):
    """An expired quote no longer leaves the exit `unknown` and the bot blocked."""
    rig, symbol = managed
    bot, controller, broker, market, clock, cfg, folder = rig
    real = broker.submit
    def refuse(order):
        raise RequestNotSent(QUOTE_EXPIRED)
    monkeypatch.setattr(broker, 'submit', refuse)
    observe(managed)
    observe(managed, seconds=300)  # second touch: still short of stop_confirm_polls
    observe(managed, seconds=300)  # third touch: the sale is refused before the network
    assert not sales(managed) and controller.state['positions'][symbol]['quantity']
    assert [o['state'] for o in controller.state['orders'].values() if o['side'] == 'SELL'] == ['rejected']
    assert symbol not in bot.runtime['exclusions']  # a holding is never shown as quote-less
    monkeypatch.setattr(broker, 'submit', real)
    observe(managed, seconds=300)
    assert len(sales(managed)) == 1




def test_one_missing_poll_preserves_touch_then_next_fresh_breach_sells(managed):
    assert observe(managed)['stop_toques'] == 1
    assert observe(managed, seconds=300, missing=True)['stop_toques'] == 1
    assert not sales(managed)
    assert observe(managed, seconds=300)['stop_toques'] == 2
    assert not sales(managed)
    assert observe(managed, seconds=300)['stop_toques'] == 3
    assert len(sales(managed)) == 1


@pytest.mark.parametrize('elapsed,expected', [(659,2),(660,2),(661,1),(900,1)])
def test_expiry_is_bounded_from_last_valid_confirmation(managed, elapsed, expected):
    observe(managed)
    assert observe(managed, seconds=elapsed)['stop_toques'] == expected
    assert not sales(managed)


def test_repeated_missing_observations_do_not_extend_window(managed):
    first = observe(managed)['stop_confirmation_at']
    for i in range(6):
        meta = observe(managed, seconds=100, missing=True)
        assert meta['stop_toques'] == 1 and meta['stop_confirmation_at'] == first
    assert observe(managed, seconds=61, missing=True)['stop_toques'] == 0
    assert not sales(managed)


@pytest.mark.parametrize('price',[85,90])
def test_valid_nonbreach_resets_confirmations(managed, price):
    observe(managed)
    assert observe(managed, seconds=300, price=price)['stop_toques'] == 0
    assert observe(managed, seconds=300)['stop_toques'] == 1
    assert not sales(managed)


def test_same_source_is_not_a_second_touch_or_new_expiry(managed):
    first = observe(managed)['stop_confirmation_at']
    meta = observe(managed, seconds=20, age=20)
    assert meta['stop_toques'] == 1 and meta['stop_confirmation_at'] == first
    assert not sales(managed)


def test_clock_backwards_does_not_reuse_touch(managed):
    observe(managed)
    assert observe(managed, seconds=-1)['stop_toques'] == 1
    assert not sales(managed)


def test_prior_session_touch_does_not_carry_over(managed):
    observe(managed)
    rig,symbol=managed
    rig[0].session += timedelta(days=1)
    rig[4].now += timedelta(days=1)
    assert observe(managed)['stop_toques'] == 1
    assert not sales(managed)


@pytest.mark.parametrize('fault',['missing_time','bad_time','missing_source','future_source','quantity','entry'])
def test_unbound_inherited_confirmation_is_not_reused(managed, fault):
    observe(managed)
    rig,symbol=managed;bot=rig[0]
    meta=deepcopy(rig[1].state['positions'][symbol]['swing'])
    if fault=='missing_time': meta.pop('stop_confirmation_at')
    elif fault=='bad_time': meta['stop_confirmation_at']='invalid'
    elif fault=='missing_source': meta.pop('stop_confirmation_source_at')
    elif fault=='future_source': meta['stop_confirmation_source_at']=(rig[4]()+timedelta(days=1)).isoformat()
    elif fault=='quantity': meta['stop_confirmation_quantity']+=1
    else: meta['stop_confirmation_entry']='different-position'
    bot._save({symbol:meta})
    assert observe(managed,seconds=300)['stop_toques']==1
    assert not sales(managed)


def test_restart_keeps_original_confirmation_deadline(managed):
    rig,symbol=managed;bot,c,b,m,clock,cfg,folder=rig
    initial=observe(managed)['stop_confirmation_at']
    observe(managed,seconds=300,missing=True); c.close()
    with ExecutionController(folder,cfg['account_id'],b) as resumed:
        replacement=Candidate(resumed,m,cfg,clock=clock);start(replacement,clock)
        assert resumed.state['positions'][symbol]['swing']['stop_confirmation_at']==initial
        new=(replacement,resumed,b,m,clock,cfg,folder),symbol
        assert observe(new,seconds=300)['stop_toques']==2
        assert not sales(new)
        assert observe(new,seconds=300)['stop_toques']==3
        assert len(sales(new))==1


def test_missing_quote_still_blocks_entries_and_never_sells(managed):
    observe(managed)
    bot,c,b,m,clock,cfg,folder=managed[0]
    m.bad.add(managed[1]);clock.advance();bot.cycle()
    assert bot.runtime['entries_blocked']=='managed_quote_missing'
    assert c.state['positions'][managed[1]]['swing']['stop_toques']==1
    assert not sales(managed)


def test_same_cycle_recovery_never_adds_confirmation(managed):
    observe(managed)
    rig,symbol=managed;bot,c,b,m,clock,cfg,folder=rig
    bot.config['managed_recovery']=dict(delay_seconds=30,max_attempts_per_cycle=2)
    for elapsed in [30,30]:
        clock.now+=timedelta(seconds=elapsed)
        cot=quote(raw_quote(symbol,clock,80),symbol,clock(),max_age=60,max_spread=cfg['max_spread_pct'])
        bot._stops({symbol:cot},{symbol})
        assert c.state['positions'][symbol]['swing']['stop_toques']==1
    assert not sales(managed)


@pytest.mark.parametrize('delay,sold',[(61,True),(300,True),(301,False)])
def test_stop_exit_send_uses_management_age(managed,monkeypatch,delay,sold):
    # A stop SELL exits held risk: it tolerates the management age (300 s), not
    # the strict entry age (60 s). Beyond it the quote has expired at send.
    observe(managed)
    rig,symbol=managed;bot,c,b,m,clock,cfg,folder=rig
    original=bot._send
    def delayed(*args,**kwargs):
        clock.now+=timedelta(seconds=delay)
        return original(*args,**kwargs)
    monkeypatch.setattr(bot,'_send',delayed)
    observe(managed,seconds=300)
    observe(managed,seconds=300)
    assert bool(sales(managed))==sold
    if not sold:
        assert bot.runtime['exclusions'][symbol]=='quote_expired_before_send'


@pytest.mark.parametrize('age,counted',[(59,True),(180,True),(299,True),
    (300,False),  # same source as the first touch: never a second confirmation
    (301,False)]) # older than the management age
def test_stop_confirmation_accepts_management_age(managed,age,counted):
    observe(managed)
    rig,symbol=managed;bot,c,b,m,clock,cfg,folder=rig
    clock.now+=timedelta(seconds=300)
    bot.runtime['last_cycle']=clock().isoformat()
    raw=raw_quote(symbol,clock,80);raw['fechaHora']=(clock()-timedelta(seconds=age)).isoformat()
    cot=quote(raw,symbol,clock(),max_age=600,max_spread=cfg['max_spread_pct'])
    bot._stops({symbol:cot},{symbol})
    assert c.state['positions'][symbol]['swing']['stop_toques']==(2 if counted else 1)
    assert not sales(managed)


def test_without_confirmation_memory_stops_keep_strict_entry_age(managed):
    # Repeated-source protection lives in the confirmation memory; without it
    # the stop must not accept older sources.
    rig,symbol=managed;bot,c,b,m,clock,cfg,folder=rig
    bot.config.pop('stop_confirmation_max_gap_seconds')
    raw=raw_quote(symbol,clock,80);raw['fechaHora']=(clock()-timedelta(seconds=120)).isoformat()
    cot=quote(raw,symbol,clock(),max_age=600,max_spread=cfg['max_spread_pct'])
    bot.runtime['last_cycle']=clock().isoformat()
    bot._stops({symbol:cot},{symbol})
    assert c.state['positions'][symbol]['swing'].get('stop_toques',0)==0


def test_equal_source_fresh_normal_read_has_no_vwap_double_count(rig, monkeypatch):
    bot,c,b,m,clock,cfg,folder=rig;symbol=cfg['universe'][0]
    raw=raw_quote(symbol,clock)
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw))
    first=bot._fetch(symbol);vwap=deepcopy(bot.runtime['vwap'])
    clock.now+=timedelta(seconds=2)
    second=bot._fetch(symbol)
    assert first is not None and second is not None
    assert first['source_at']==second['source_at'] and first['received_at']!=second['received_at']
    assert bot.runtime['vwap']==vwap
    # Admission ends at the management age (300 s), not the entry age.
    clock.now+=timedelta(seconds=297)
    assert bot._fetch(symbol) is not None and bot.runtime['vwap']==vwap
    clock.now+=timedelta(seconds=2)
    assert bot._fetch(symbol) is None
    assert bot.runtime['exclusions'][symbol]=='stale_or_future_quote'


@pytest.mark.parametrize('fault',['regression','trade_conflict','crossed','identity'])
def test_equal_source_exception_does_not_relax_other_validation(rig, monkeypatch, fault):
    bot,c,b,m,clock,cfg,folder=rig;symbol=cfg['universe'][0];raw=raw_quote(symbol,clock)
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw))
    assert bot._fetch(symbol)
    if fault=='regression':raw['fechaHora']=(clock()-timedelta(seconds=1)).isoformat()
    elif fault=='trade_conflict':raw['ultimoPrecio']=91
    elif fault=='crossed':raw['puntas'][0]['precioCompra']=raw['puntas'][0]['precioVenta']+1
    else:raw['simbolo']='WRONG'
    assert bot._fetch(symbol) is None


@pytest.mark.parametrize('gap',[True,False,0,-1,299,691,900,690.0,'690'])
def test_invalid_confirmation_window_rejected(rig, gap):
    cfg=deepcopy(rig[5]);cfg['stop_confirmation_max_gap_seconds']=gap
    with pytest.raises(ValueError,match='confirmation gap'):validate_config(cfg)
