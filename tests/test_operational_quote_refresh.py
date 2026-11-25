from copy import deepcopy
from datetime import timedelta
import pytest
from test_operational_candidate import rig,raw_quote
from argbot.strategy.candidate import config_hash


@pytest.fixture(autouse=True)
def strict_management_age(rig):
    # These refresh scenarios use 84-91 s sources against the fixture limit.
    # The 300 s management default is covered in test_operational_quote_age_by_use.py.
    bot,c,b,m,clock,cfg,_=rig
    cfg['managed_quote_age_seconds']=90
    bot.config=deepcopy(cfg);bot.runtime['config_hash']=config_hash(cfg);bot._save()

def one_position(rig):
    bot,c,b,m,clock,cfg,_=rig
    bot.cycle();b.fill('1')
    for n in list(b.orders):
        if n!='1':b.cancel(n)
    clock.advance();bot.cycle()
    assert len(c.state['positions'])==1
    clock.advance(145)
    return next(iter(c.state['positions']))

@pytest.mark.parametrize('provider_fresh',[True,False])
def test_scan_expiry_refreshes_once_and_never_accepts_old_source(rig,monkeypatch,provider_fresh):
    symbol=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    calls=[];original=m.quote;before=len(b.sent)
    def slow(s):
        calls.append(s)
        if s!=symbol:clock.now+=timedelta(seconds=.5)
        result=original(s)
        if s==symbol:
            refresh=calls.count(s)>1
            age=0 if refresh and provider_fresh else (91 if refresh else 84)
            result['fechaHora']=(clock()-timedelta(seconds=age)).isoformat()
        return result
    monkeypatch.setattr(m,'quote',slow)
    bot.cycle()
    assert calls.count(symbol)==2 and len(calls)==31
    audit=bot.runtime['managed_quote_refreshes'][symbol]
    assert audit['prior_age_seconds']>90 and audit['accepted']==provider_fresh
    assert cfg['max_quote_age_seconds']==90
    if provider_fresh:
        assert bot.runtime['entries_blocked'] is None and len(b.sent)>before
    else:
        assert bot.runtime['entries_blocked']=='managed_quote_missing' and len(b.sent)==before

def test_same_fresh_refresh_does_not_recount_vwap_or_reset_source_time(rig,monkeypatch):
    bot,c,b,m,clock,cfg,_=rig;s=cfg['universe'][0]
    raw=raw_quote(s,clock);raw['fechaHora']=(clock()-timedelta(seconds=85)).isoformat()
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw))
    first=bot._fetch(s);before=deepcopy(bot.runtime['vwap'])
    clock.now+=timedelta(seconds=1)
    second=bot._fetch(s,refresh=True)
    assert second['source_at']==first['source_at'] and bot.runtime['vwap']==before
    clock.now+=timedelta(seconds=5)
    assert bot._fetch(s,refresh=True) is None
    assert bot.runtime['exclusions'][s]=='stale_or_future_quote'

def test_same_timestamp_conflicting_quote_remains_rejected(rig,monkeypatch):
    bot,c,b,m,clock,cfg,_=rig;s=cfg['universe'][0];raw=raw_quote(s,clock)
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw));assert bot._fetch(s)
    raw['montoOperado']+=1
    assert bot._fetch(s,refresh=True) is None
    assert bot.runtime['exclusions'][s]=='duplicate_or_regressing_quote'

def test_refresh_never_counts_a_second_stop_confirmation(rig,monkeypatch):
    symbol=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    m.prices[symbol]=75;original=m.quote;calls=[]
    def slow(s):
        calls.append(s)
        if s!=symbol:clock.now+=timedelta(seconds=.5)
        raw=original(s)
        if s==symbol and calls.count(s)==1:raw['fechaHora']=(clock()-timedelta(seconds=84)).isoformat()
        return raw
    monkeypatch.setattr(m,'quote',slow);bot.cycle()
    assert calls.count(symbol)==2
    assert c.state['positions'][symbol]['swing']['stop_toques']==1
    assert not any(o['side']=='SELL' for o in b.sent)

def test_initial_missing_quote_can_recover_in_single_bounded_refresh(rig,monkeypatch):
    symbol=one_position(rig);bot,c,b,m,clock,cfg,_=rig;original=m.quote;calls=[]
    def missing_once(s):
        calls.append(s)
        if s==symbol and calls.count(s)==1:raise TimeoutError()
        return original(s)
    monkeypatch.setattr(m,'quote',missing_once);bot.cycle()
    assert calls.count(symbol)==2 and bot.runtime['entries_blocked'] is None

def test_slow_order_batch_cannot_reuse_expired_portfolio_mark_or_refresh_forever(rig,monkeypatch):
    symbol=one_position(rig);bot,c,b,m,clock,cfg,_=rig;original=m.quote;calls=[]
    def slow_scan(s):
        calls.append(s)
        if s!=symbol:clock.now+=timedelta(seconds=.5)
        raw=original(s)
        if s==symbol and calls.count(s)==1:raw['fechaHora']=(clock()-timedelta(seconds=84)).isoformat()
        return raw
    submit=b.submit;before=len(b.sent)
    def slow_submit(order):
        result=submit(order)
        clock.now+=timedelta(seconds=100)
        return result
    monkeypatch.setattr(m,'quote',slow_scan);monkeypatch.setattr(b,'submit',slow_submit)
    bot.cycle()
    assert calls.count(symbol)==2 and len(b.sent)==before+1
    assert bot.runtime['entries_blocked']=='managed_quote_missing'
