"""Fault scenarios for the isolated candidate; no real broker/model/network."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from test_operational_candidate import rig, raw_quote, start
from test_operational_quote_refresh import one_position
from argbot.strategy.candidate import Candidate, validate_config, config_hash
from argbot.execution import ExecutionController


@pytest.fixture(autouse=True)
def proposed_policy(rig):
    bot,c,b,m,clock,cfg,_=rig
    # Strict management age: gap scenarios use sources older than the admitted
    # limit. The 300 s default is covered in test_operational_quote_age_by_use.py.
    additions=dict(max_daily_loss_pct='0.05',managed_recovery={'delay_seconds':30,'max_attempts_per_cycle':1},
                   quote_timeout_seconds=5,opportunity_scan_seconds=20,max_quote_age_seconds=60,managed_quote_age_seconds=60)
    cfg.update(deepcopy(additions));bot.config.update(additions)
    bot.monotonic=lambda:clock().timestamp()
    bot.runtime['config_hash']=config_hash(cfg);bot._save()


def test_book_can_change_without_trade_timestamp_or_vwap_change(rig, monkeypatch):
    bot,c,b,m,clock,cfg,_=rig; s=cfg['universe'][0]
    raw=raw_quote(s,clock)
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw))
    first=bot._fetch(s);vwap=deepcopy(bot.runtime['vwap'])
    clock.now+=timedelta(seconds=10)
    raw['puntas'][0]['precioCompra']-=.1
    raw['puntas'][0]['cantidadCompra']-=1
    second=bot._fetch(s,refresh=True)
    assert second and second['source_at']==first['source_at']
    assert second['bid']!=first['bid'] and bot.runtime['vwap']==vwap
    assert bot.runtime['quotes'][s]==second
    clock.now+=timedelta(seconds=51)
    assert bot._fetch(s,refresh=True) is None


@pytest.mark.parametrize('field,value', [('ultimoPrecio',91),('montoOperado',999999),
    ('volumenNominal',123),('moneda','dolar_Estadounidense'),('plazo','T0'),
    ('lote',2),('simbolo','WRONG'),('minimo',88)])
def test_same_timestamp_nonbook_conflicts_are_still_rejected(rig,monkeypatch,field,value):
    bot,c,b,m,clock,cfg,_=rig;s=cfg['universe'][0];raw=raw_quote(s,clock)
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw));assert bot._fetch(s)
    raw[field]=value
    assert bot._fetch(s,refresh=True) is None


@pytest.mark.parametrize('case',['crossed','empty','regressing','future'])
def test_book_update_cannot_bypass_validation(rig,monkeypatch,case):
    bot,c,b,m,clock,cfg,_=rig;s=cfg['universe'][0];raw=raw_quote(s,clock)
    monkeypatch.setattr(m,'quote',lambda _:deepcopy(raw));assert bot._fetch(s)
    if case=='crossed':raw['puntas'][0]['precioCompra']=100
    if case=='empty':raw['puntas']=[]
    if case=='regressing':raw['fechaHora']=(clock()-timedelta(seconds=1)).isoformat()
    if case=='future':raw['fechaHora']=(clock()+timedelta(seconds=1)).isoformat()
    assert bot._fetch(s,refresh=True) is None




def test_round_without_usable_quotes_does_not_consume_the_window(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    m.bad.update(cfg['universe']);bot.cycle()
    assert '13:45' not in bot.runtime['windows']


def test_managed_recovery_is_delayed_once_and_can_reopen_entries(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    before=len(b.sent);m.bad.add(s);bot.cycle();main=bot.runtime['last_cycle']
    calls=len(m.calls);m.bad.remove(s);clock.now+=timedelta(seconds=29);bot.cycle()
    assert len(m.calls)==calls
    clock.now+=timedelta(seconds=1);bot.cycle()
    assert len(m.calls)==calls+1 and bot.runtime['entries_blocked'] is None
    assert bot.runtime['last_cycle']==main and bot.runtime['recovery_claim_cycle']==main
    assert len(b.sent)>before and '13:45' in bot.runtime['windows']
    calls=len(m.calls);clock.now+=timedelta(seconds=30);bot.cycle();assert len(m.calls)==calls


def test_failed_recovery_claim_survives_restart_without_duplicate_reads(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,folder=rig
    m.bad.add(s);bot.cycle();clock.now+=timedelta(seconds=30);bot.cycle()
    calls=len(m.calls);before=len(b.sent);c.close()
    with ExecutionController(folder,cfg['account_id'],b) as resumed:
        nextbot=Candidate(resumed,m,cfg,clock=clock);start(nextbot,clock)
        clock.now+=timedelta(seconds=30);nextbot.cycle()
        assert len(m.calls)==calls and len(b.sent)==before
        assert nextbot.runtime['entries_blocked']=='managed_quote_missing'


def test_recovery_does_not_count_second_stop_check_in_same_primary_cycle(rig):
    bot,c,b,m,clock,cfg,_=rig;bot.cycle()
    b.fill('1');b.fill('2')
    for n in ['3','4','5']:b.cancel(n)
    clock.advance();bot.cycle();symbols=list(c.state['positions']);a,missing=symbols
    clock.advance(145);m.prices[a]=75;m.bad.add(missing);c.pause(True);bot.cycle()
    assert c.state['positions'][a]['swing']['stop_toques']==1
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert c.state['positions'][a]['swing']['stop_toques']==1
    assert not any(o['side']=='SELL' for o in b.sent)


def test_recovery_reconciliation_failure_prevents_quotes_and_orders(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    m.bad.add(s);bot.cycle();calls=len(m.calls);before=len(b.sent)
    b.fail=True;clock.now+=timedelta(seconds=30);bot.cycle()
    assert len(m.calls)==calls and len(b.sent)==before
    assert not c.state['last_reconciliation']['ok']


def test_recovery_preserves_management_reserve(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    m.bad.add(s);bot.cycle();calls=len(m.calls)
    state=c.state;state['http_budget']={'day':clock().date().isoformat(),'total':4000,'reserve':400,'used':3599};c._commit(state)
    clock.now+=timedelta(seconds=30);bot.cycle();assert len(m.calls)==calls
    assert bot.runtime.get('recovery_claim_cycle')!=bot.runtime['last_cycle']


def test_recovery_keeps_window_available_when_cached_candidates_expired(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    m.bad.add(s);bot.cycle();before=len(b.sent)
    m.bad.remove(s);clock.now+=timedelta(seconds=70);bot.cycle()
    assert bot.runtime['entries_blocked'] is None and len(b.sent)==before
    assert '13:45' not in bot.runtime['windows']


def test_recovery_audits_both_phases_without_changing_main_round(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,folder=rig
    m.bad.add(s);bot.cycle();main=bot.runtime['last_cycle'];clock.now+=timedelta(seconds=30);bot.cycle()
    rows=[json.loads(line) for line in (folder/'operational-cycles.private.jsonl').read_text().splitlines()]
    recovery=[r for r in rows if r.get('cycle_kind')=='managed_recovery']
    assert [r['phase'] for r in recovery]==['started','completed']
    assert all(r['parent_cycle_at']==main for r in recovery)


@pytest.mark.parametrize('value',[{'delay_seconds':0,'max_attempts_per_cycle':1},
    {'delay_seconds':30,'max_attempts_per_cycle':3}, {'delay_seconds':30.,'max_attempts_per_cycle':1}, False])
def test_unbounded_recovery_configuration_is_rejected(rig,value):
    cfg=deepcopy(rig[5]);cfg['managed_recovery']=value
    with pytest.raises(ValueError):validate_config(cfg)


def test_timeout_scan_yields_and_resumes_at_next_symbol(rig,monkeypatch):
    bot,c,b,m,clock,cfg,_=rig;c.pause(True);seen=[]
    def timeout(s):
        seen.append(s);clock.now+=timedelta(seconds=5)
        raise TimeoutError()
    monkeypatch.setattr(m,'quote',timeout)
    bot.cycle();assert len(seen)==4
    assert bot.runtime['opportunity_scan_cursor']==4
    assert not b.sent
    clock.advance();bot.cycle();assert len(seen)==8
    assert seen[4]==cfg['universe'][4]


def test_scan_yields_after_single_slow_call_without_launching_rest(rig,monkeypatch):
    bot,c,b,m,clock,cfg,_=rig;c.pause(True);seen=[]
    def slow(s):
        seen.append(s);clock.now+=timedelta(seconds=25)
        raise TimeoutError()
    monkeypatch.setattr(m,'quote',slow);bot.cycle()
    assert len(seen)==1 and bot.runtime['opportunity_scan_cursor']==1


def test_scan_deadline_uses_monotonic_despite_wall_clock_jump(rig,monkeypatch):
    bot,c,b,m,clock,cfg,_=rig;c.pause(True);elapsed=[0];seen=[]
    bot.monotonic=lambda:elapsed[0]
    def timeout(s):
        seen.append(s);elapsed[0]+=5;clock.now-=timedelta(seconds=10)
        raise TimeoutError()
    monkeypatch.setattr(m,'quote',timeout);bot.cycle()
    assert len(seen)==4 and not b.sent


@pytest.mark.parametrize('method,path,expected',[
    ('GET','/api/v2/bCBA/Titulos/TEST/Cotizacion',5),
    ('GET','/api/v2/bCBA/Titulos/TEST/Cotizacion/seriehistorica/a/b/c',25),
    ('GET','/api/v2/estadocuenta',25),('POST','/token',25),
    ('POST','/api/v2/operar/Comprar',25),('DELETE','/api/v2/operaciones/123',25)])
def test_fast_timeout_only_changes_quote_reads(rig,method,path,expected):
    import httpx
    from argbot.brokers.iol.budget import BudgetTransport
    seen=[];charges=[]
    class Budget:
        def charge(self,r):charges.append(r.method)
        def response(self,r):pass
    def capture(r):
        seen.append(r.extensions['timeout']);return httpx.Response(200,json={})
    with httpx.Client(transport=BudgetTransport(httpx.MockTransport(capture),Budget(),
            quote_timeout_seconds=5),timeout=25) as client:
        client.request(method,'https://synthetic.invalid'+path)
    assert seen==[{k:float(expected) for k in ('connect','read','write','pool')}]
    assert charges==[method]


def test_full_session_repeated_quote_gaps_recover_without_orders_or_stop_duplicates(rig,monkeypatch):
    bot,c,b,m,clock,cfg,folder=rig
    bot.cycle()
    for n in list(b.orders):b.fill(n)
    clock.advance();bot.cycle();assert len(c.state['positions'])==6
    c.pause(True)
    clock.now=(clock()+timedelta(days=1)).replace(hour=10,minute=30,second=0,microsecond=0)
    start(bot,clock);m.calls.clear();accounts=[];account=b.account
    def counted_account():accounts.append(1);return account()
    monkeypatch.setattr(b,'account',counted_account)
    quote=m.quote;held=set(c.state['positions']);before=len(b.sent)
    def gaps(s):
        raw=quote(s)
        if s in held and bot._cycle_audit.get('cycle_kind')!='managed_recovery':
            raw['fechaHora']=(clock()-timedelta(seconds=89)).isoformat()
        return raw
    monkeypatch.setattr(m,'quote',gaps)
    for _ in range(78):
        bot.cycle();assert bot.runtime['entries_blocked']=='managed_quote_missing'
        clock.now+=timedelta(seconds=30);bot.cycle()
        assert bot.runtime['entries_blocked'] is None
        clock.now+=timedelta(seconds=270)
    assert len(b.sent)==before and not bot.runtime['risk_paused']
    assert len(m.calls)==78*42 and len(accounts)==78*2
    rows=[json.loads(line) for line in (folder/'operational-cycles.private.jsonl').read_text().splitlines()]
    session=[r for r in rows if r['session']==clock().date().isoformat() and r['phase']=='completed']
    assert len(session)==156
    assert sum(r.get('cycle_kind')=='managed_recovery' for r in session)==78
    # Quote + known three-request account snapshots only, not a claim about
    # real authentication, latency, submissions, provider quality or fills.
    assert len(m.calls)+3*len(accounts)==3744
    for row in session:
        fresh=[o for o in row['observations'] if o['kind']=='stop_check' and o['fresh']]
        assert len(fresh)==(6 if row.get('cycle_kind')=='managed_recovery' else 0)
