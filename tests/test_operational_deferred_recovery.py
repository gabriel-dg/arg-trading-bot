"""Fictional broker/clock: bounded retry timing, expiry and restart safety."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from test_operational_candidate import rig, start
from test_operational_quote_refresh import one_position
from argbot.strategy.candidate import Candidate, config_hash, validate_config
from argbot.execution import ExecutionController

@pytest.fixture(autouse=True)
def policy(rig):
    bot,c,b,m,clock,cfg,_=rig
    # Strict management age: these scenarios exercise deferral/recovery of sources
    # older than the admitted limit. The 300 s default is covered separately.
    cfg.update(max_daily_loss_pct='0.05',managed_recovery={'delay_seconds':30,'max_attempts_per_cycle':2},
               quote_timeout_seconds=5,opportunity_scan_seconds=20,
               max_quote_age_seconds=60,managed_quote_age_seconds=60)
    bot.config=deepcopy(cfg);bot.monotonic=lambda:clock().timestamp()
    bot.runtime['config_hash']=config_hash(cfg);bot._save()

def old_source(rig,monkeypatch,symbol,age=90):
    bot,c,b,m,clock,cfg,_=rig;original=m.quote
    def get(s):
        raw=original(s)
        if s==symbol:raw['fechaHora']=(clock()-timedelta(seconds=age)).isoformat()
        return raw
    monkeypatch.setattr(m,'quote',get)
    return original

def test_old_source_is_deferred_then_two_passes_can_restore_valuation(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,folder=rig
    original=old_source(rig,monkeypatch,s);m.calls.clear();before=len(b.sent)
    bot.cycle();parent=bot.runtime['last_cycle']
    assert m.calls.count(s)==1 and len(b.sent)==before
    assert bot.runtime['managed_quote_refreshes'][s]['phase']=='deferred_old_source'
    assert not bot.runtime['valuation_available']
    clock.now+=timedelta(seconds=29);bot.cycle();assert m.calls.count(s)==1
    clock.now+=timedelta(seconds=1);bot.cycle();assert m.calls.count(s)==2
    assert bot.runtime['managed_recovery_attempt']['number']==1
    monkeypatch.setattr(m,'quote',original)
    clock.now+=timedelta(seconds=29);bot.cycle();assert m.calls.count(s)==2
    clock.now+=timedelta(seconds=1);bot.cycle()
    assert m.calls.count(s)==3 and bot.runtime['valuation_available']
    assert bot.runtime['entries_blocked'] is None
    assert bot.runtime['last_cycle']==parent and cfg['max_quote_age_seconds']==60
    calls=len(m.calls);clock.now+=timedelta(seconds=60);bot.cycle();assert len(m.calls)==calls
    rows=[json.loads(x) for x in (folder/'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [r['recovery_attempt'] for r in rows if r.get('parent_cycle_at')==parent and r['phase']=='completed']==[1,2]

@pytest.mark.parametrize('age,expected',[(-2,2),(61,1),(90,1)])
def test_only_confirmed_old_quote_defers_immediate_retry(rig,monkeypatch,age,expected):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    old_source(rig,monkeypatch,s,age);m.calls.clear();bot.cycle()
    assert m.calls.count(s)==expected
    assert bot.runtime['entries_blocked']=='managed_quote_missing'

def test_null_recovery_policy_keeps_original_immediate_refresh(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    bot.config['managed_recovery']=None
    old_source(rig,monkeypatch,s);m.calls.clear();bot.cycle()
    assert m.calls.count(s)==2
    assert bot.runtime['entries_blocked']=='managed_quote_missing'

def test_second_delay_is_from_completion_not_first_start(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    original=old_source(rig,monkeypatch,s);bot.cycle()
    stale=m.quote
    def slow(symbol):
        clock.now+=timedelta(seconds=20)
        return stale(symbol)
    monkeypatch.setattr(m,'quote',slow)
    clock.now+=timedelta(seconds=30);bot.cycle();calls=len(m.calls)
    clock.now+=timedelta(seconds=29);bot.cycle();assert len(m.calls)==calls
    clock.now+=timedelta(seconds=1);bot.cycle();assert len(m.calls)==calls+1

def test_second_attempt_survives_restart_and_no_third_attempt(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,folder=rig
    old_source(rig,monkeypatch,s);bot.cycle();clock.now+=timedelta(seconds=30);bot.cycle()
    calls=len(m.calls);c.close()
    with ExecutionController(folder,cfg['account_id'],b) as resumed:
        nextbot=Candidate(resumed,m,cfg,clock=clock);start(nextbot,clock)
        clock.now+=timedelta(seconds=30);nextbot.cycle();assert len(m.calls)==calls+1
        clock.now+=timedelta(seconds=30);nextbot.cycle();assert len(m.calls)==calls+1
        assert not nextbot.runtime['valuation_available']

def test_each_claim_durable_before_reconciliation_or_quote(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,folder=rig
    old_source(rig,monkeypatch,s);bot.cycle();account=b.account;seen=[]
    def read():
        state=json.loads((folder/'execution-state.json').read_text(encoding='utf-8'))['state']
        claim=state['strategy']['managed_recovery_attempt']
        assert claim['completed_at'] is None
        seen.append(claim['number']);return account()
    monkeypatch.setattr(b,'account',read)
    for _ in range(2):clock.now+=timedelta(seconds=30);bot.cycle()
    assert seen==[1,2]

def test_budget_exhaustion_blocks_second_without_consuming_it(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    old_source(rig,monkeypatch,s);bot.cycle();clock.now+=timedelta(seconds=30);bot.cycle()
    calls=len(m.calls);state=c.state
    state['http_budget']=dict(day=clock().date().isoformat(),total=4000,reserve=400,used=3599)
    c._commit(state);clock.now+=timedelta(seconds=30);bot.cycle()
    assert len(m.calls)==calls and bot.runtime['managed_recovery_attempt']['number']==1

def test_old_runtime_claim_blocks_any_replay_of_prior_round(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    old_source(rig,monkeypatch,s);bot.cycle()
    bot.runtime['recovery_claim_cycle']=bot.runtime['last_cycle'];bot._save()
    calls=len(m.calls);clock.now+=timedelta(seconds=60);bot.cycle();assert len(m.calls)==calls
    clock.advance();bot.cycle();clock.now+=timedelta(seconds=30);bot.cycle()
    assert bot.runtime['managed_recovery_attempt']['number']==1

def test_second_recovery_never_adds_stop_confirmation(rig,monkeypatch):
    bot,c,b,m,clock,cfg,_=rig;bot.cycle();b.fill('1');b.fill('2')
    for n in ['3','4','5']:b.cancel(n)
    clock.advance();bot.cycle();a,missing=c.state['positions']
    clock.advance(145);m.prices[a]=75;c.pause(True);old_source(rig,monkeypatch,missing)
    bot.cycle();assert c.state['positions'][a]['swing']['stop_toques']==1
    for _ in range(2):
        clock.now+=timedelta(seconds=30);bot.cycle()
        assert c.state['positions'][a]['swing']['stop_toques']==1
    assert not any(o['side']=='SELL' for o in b.sent)

@pytest.mark.parametrize('value',[0,3,True,2.0])
def test_attempt_limit_cannot_be_relaxed(rig,value):
    cfg=deepcopy(rig[5]);cfg['managed_recovery']['max_attempts_per_cycle']=value
    with pytest.raises(ValueError):validate_config(cfg)

def test_second_reconciliation_failure_prevents_quote_reads_and_orders(rig,monkeypatch):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    old_source(rig,monkeypatch,s);bot.cycle();clock.now+=timedelta(seconds=30);bot.cycle()
    calls=len(m.calls);before=len(b.sent);b.fail=True
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert len(m.calls)==calls and len(b.sent)==before
    assert bot.runtime['managed_recovery_attempt']['number']==2
    assert not c.state['last_reconciliation']['ok']

def test_full_session_two_attempts_keep_read_bound_and_no_duplicate_stops(rig,monkeypatch):
    bot,c,b,m,clock,cfg,folder=rig;bot.cycle()
    for n in list(b.orders):b.fill(n)
    clock.advance();bot.cycle();assert len(c.state['positions'])==6
    c.pause(True)
    clock.now=(clock()+timedelta(days=1)).replace(hour=10,minute=30,second=0,microsecond=0)
    start(bot,clock);m.calls.clear();accounts=[];account=b.account
    def counted_account():accounts.append(1);return account()
    monkeypatch.setattr(b,'account',counted_account)
    get=m.quote;held=set(c.state['positions']);before=len(b.sent)
    def gaps(s):
        raw=get(s)
        if s in held and bot._cycle_audit.get('recovery_attempt')!=2:
            raw['fechaHora']=(clock()-timedelta(seconds=89)).isoformat()
        return raw
    monkeypatch.setattr(m,'quote',gaps)
    for _ in range(78):
        bot.cycle();assert not bot.runtime['valuation_available']
        clock.now+=timedelta(seconds=30);bot.cycle();assert not bot.runtime['valuation_available']
        clock.now+=timedelta(seconds=30);bot.cycle();assert bot.runtime['valuation_available']
        calls=len(m.calls);clock.now+=timedelta(seconds=30);bot.cycle();assert len(m.calls)==calls
        clock.now+=timedelta(seconds=210)
    assert len(b.sent)==before and not bot.runtime['risk_paused']
    assert len(m.calls)==78*42 and len(accounts)==78*3
    # Three-read account snapshots plus quotes; excludes auth/history/orders.
    assert len(m.calls)+3*len(accounts)==3978
    rows=[json.loads(x) for x in (folder/'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()]
    session=[r for r in rows if r['session']==clock().date().isoformat() and r['phase']=='completed']
    assert len(session)==234
    for row in session:
        fresh=[o for o in row['observations'] if o['kind']=='stop_check' and o['fresh']]
        assert len(fresh)==(6 if row.get('recovery_attempt')==2 else 0)
