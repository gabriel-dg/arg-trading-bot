"""A round whose reconciliation failed on the network is tried again at 30 s.

Otherwise a dropped connection that fails the reconciliation (IOLAPIError) makes every
stop wait for the next round. A login that got no answer or a 5xx is retried too;
a refused login still waits. Fake broker and
market; no network.
"""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test_operational_candidate import rig, start
from test_operational_quote_refresh import one_position
from argbot.brokers.iol.exceptions import (IOLAPIError, IOLAuthError, IOLAuthNetworkError, IOLAuthRejectedError,
                            IOLAuthServerError)
from argbot.strategy.candidate import Candidate, config_hash
from argbot.execution import ExecutionBlocked, ExecutionController, instant


@pytest.fixture(autouse=True)
def live_policy(rig):
    # The live configuration: two bounded recovery passes per round, 30 s apart.
    bot,c,b,m,clock,cfg,_=rig
    additions=dict(max_daily_loss_pct='0.05',managed_recovery={'delay_seconds':30,'max_attempts_per_cycle':2},
                   quote_timeout_seconds=5,opportunity_scan_seconds=20,max_quote_age_seconds=60,managed_quote_age_seconds=60)
    cfg.update(deepcopy(additions));bot.config.update(additions)
    bot.monotonic=lambda:clock().timestamp()
    bot.runtime['config_hash']=config_hash(cfg);bot._save()


class Outage:
    """Account reads raise `error` while `down`; every read is counted."""
    def __init__(self, broker, error):
        self.read, self.error, self.down, self.calls = broker.account, error, error is not None, 0
        broker.account = self

    def __call__(self):
        self.calls += 1
        if self.down:
            raise self.error
        return self.read()


def network():
    return IOLAPIError(0, 'Error de red en GET /api/v2/estadocuenta')


@pytest.mark.parametrize('error', [network(), TimeoutError(), IOLAuthNetworkError('Error de red en /token'),
                                   IOLAuthServerError('Token endpoint devolvió 503')],
                         ids=['IOLAPIError', 'TimeoutError', 'login_network', 'login_5xx'])
def test_network_failure_is_retried_at_30_s_with_every_stop_and_no_window(rig, error):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    outage=Outage(b,error);sent=len(b.sent)
    bot.cycle();parent=bot.runtime['last_cycle']
    assert c.state['strategy']['status']=='blocked' and c.state['last_reconciliation']['ok'] is False
    assert bot.runtime['reconciliation_retry_cycle']==parent
    assert bot.runtime.get('stop_checked_cycle',{}).get(s)!=parent
    outage.down=False;calls=outage.calls;quotes=len(m.calls)
    clock.now+=timedelta(seconds=29);bot.cycle()
    assert outage.calls==calls and len(m.calls)==quotes
    clock.now+=timedelta(seconds=1);bot.cycle()
    assert outage.calls==calls+1 and c.state['last_reconciliation']['ok'] is True
    assert bot.runtime['last_cycle']==parent and bot.runtime['recovery_claim_cycle']==parent
    assert bot.runtime['stop_checked_cycle'][s]==parent
    assert bot.runtime['status']=='running' and bot.runtime['reconciliation_retry_cycle'] is None
    # Stops only: no buy and the 13:45 window stays for the next normal round.
    assert len(b.sent)==sent and '13:45' not in bot.runtime['windows']
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==calls+1
    clock.now=instant(parent)+timedelta(seconds=300);bot.cycle()
    assert bot.runtime['last_cycle']!=parent and '13:45' in bot.runtime['windows']


@pytest.mark.parametrize('case', ['refused_login', 'persistent_401', 'unclassified_login', 'retry_after',
                                  'discrepancy'])
def test_failures_other_than_the_network_wait_for_the_next_round(rig, case):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    if case=='refused_login':
        # Repeating a refused login could lock the account .
        outage=Outage(b,IOLAuthRejectedError('Token endpoint devolvió 400'))
    elif case=='persistent_401':
        outage=Outage(b,IOLAuthRejectedError('401 persistente después de refresh+retry'))
    elif case=='unclassified_login':
        # A 200 without a token, or missing credentials: not a passing failure.
        outage=Outage(b,IOLAuthError('Respuesta de /token sin campo'))
    elif case=='retry_after':
        outage=Outage(b,ExecutionBlocked('Broker Retry-After active'))
    else:
        outage=Outage(b,None);b.positions[s]+=1
    bot.cycle();parent=bot.runtime['last_cycle'];calls=outage.calls
    assert c.state['last_reconciliation']['ok'] is False
    assert bot.runtime.get('reconciliation_retry_cycle')!=parent
    for _ in range(3):
        clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==calls and bot.runtime['last_cycle']==parent


def test_a_retry_that_fails_again_is_bounded_and_not_replayed_after_a_restart(rig):
    s=one_position(rig);bot,c,b,m,clock,cfg,folder=rig
    outage=Outage(b,network())
    bot.cycle();parent=bot.runtime['last_cycle']
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==2 and bot.runtime['managed_recovery_attempt']['number']==1
    assert bot.runtime['reconciliation_retry_cycle']==parent and bot.runtime['status']=='blocked'
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==3 and bot.runtime['managed_recovery_attempt']['number']==2
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==3
    c.close()
    with ExecutionController(folder,cfg['account_id'],b) as resumed:
        nextbot=Candidate(resumed,m,cfg,clock=clock);start(nextbot,clock)
        clock.now+=timedelta(seconds=30);nextbot.cycle()
        assert outage.calls==3 and nextbot.runtime['last_cycle']==parent
        outage.down=False
        clock.now=instant(parent)+timedelta(seconds=300);nextbot.cycle()
        # The normal round reconciles again before each buy of its window.
        assert outage.calls>3 and resumed.state['last_reconciliation']['ok'] is True
        assert nextbot.runtime['stop_checked_cycle'][s]==nextbot.runtime['last_cycle']!=parent


def test_a_failed_retry_never_shows_running_in_between(rig, monkeypatch):
    # A monitor raises a notice on each change of status: blocked -> running ->
    # blocked would be a second notice for the same outage.
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    outage=Outage(b,network());bot.cycle()
    seen=[];original=c.reconcile
    def watched():
        seen.append(c.state['strategy']['status'])
        return original()
    monkeypatch.setattr(c,'reconcile',watched)
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert seen==['blocked'] and c.state['strategy']['status']=='blocked'


@pytest.mark.parametrize('grants,retried', [(3, True), (4, False)])
def test_the_retry_keeps_three_logins_for_the_normal_rounds(rig, grants, retried):
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    outage=Outage(b,network());bot.cycle();calls=outage.calls
    state=c.state
    state['http_budget']={'day':clock().date().isoformat(),'total':4000,'reserve':400,'used':100,
                          'market_data':0,'password_grants':grants}
    c._commit(state)
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==calls+retried




class Logins(Outage):
    """Like the real budget: each failed account read here is a login, spent before it is sent."""
    def __call__(self):
        if self.down:
            state=self.controller.state
            state['http_budget']['password_grants']+=1
            self.controller._commit(state)
        return super().__call__()


def test_replayed_a_dropped_login_is_retried_once_and_keeps_three_logins(rig):
    # Two logins spent earlier; that round's login dropped (3). The retry at
    # 30 s may spend the 4th; a second retry would leave fewer than three, so the bot
    # waits for the next round, as intended.
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    state=c.state
    state['http_budget']={'day':clock().date().isoformat(),'total':4000,'reserve':400,'used':100,
                          'market_data':0,'password_grants':2}
    c._commit(state)
    outage=Logins(b,IOLAuthNetworkError('Error de red en /token'));outage.controller=c
    bot.cycle();parent=bot.runtime['last_cycle']
    assert c.state['http_budget']['password_grants']==3
    assert c.state['last_reconciliation']['reason']=='IOLAuthNetworkError'
    assert bot.runtime['reconciliation_retry_cycle']==parent
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==2 and c.state['http_budget']['password_grants']==4
    assert bot.runtime['status']=='blocked'
    clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==2 and c.state['http_budget']['password_grants']==4
    outage.down=False
    clock.now=instant(parent)+timedelta(seconds=300);bot.cycle()
    assert c.state['last_reconciliation']['ok'] is True and bot.runtime['status']=='running'
    assert bot.runtime['stop_checked_cycle'][s]==bot.runtime['last_cycle']


def test_a_5xx_login_followed_by_retry_after_does_not_loop_or_show_running(rig, monkeypatch):
    # A 5xx carrying Retry-After makes the budget refuse the next request: the retry
    # stops there and the bot waits for the next round, still blocked.
    s=one_position(rig);bot,c,b,m,clock,cfg,_=rig
    outage=Outage(b,IOLAuthServerError('Token endpoint devolvió 503'))
    bot.cycle();parent=bot.runtime['last_cycle']
    assert bot.runtime['reconciliation_retry_cycle']==parent
    outage.error=ExecutionBlocked('Broker Retry-After active')
    seen=[];original=c.reconcile
    def watched():
        seen.append(c.state['strategy']['status'])
        return original()
    monkeypatch.setattr(c,'reconcile',watched)
    for _ in range(3):
        clock.now+=timedelta(seconds=30);bot.cycle()
    assert outage.calls==2 and seen==['blocked']
    assert bot.runtime['reconciliation_retry_cycle'] is None and bot.runtime['status']=='blocked'


def test_a_budget_refusal_of_a_login_is_never_taken_for_a_network_failure(tmp_path):
    # The retry must not fire on "Session login limit reached": the refusal leaves the
    # budget transport as ExecutionBlocked, outside httpx's errors.
    import httpx
    from argbot.brokers.iol.auth import IOLAuth
    from argbot.brokers.iol.budget import PASSWORD_GRANT_LIMIT, BudgetTransport, SessionBudget
    from datetime import date
    ExecutionController.prepare(tmp_path/'state','fixture',cash='100000',fee_reserve='.01')
    with ExecutionController(tmp_path/'state','fixture',None) as c:
        budget=SessionBudget(c,date(2026,11,16),4000,400)
        state=c.state;state['http_budget']['password_grants']=PASSWORD_GRANT_LIMIT;c._commit(state)
        transport=BudgetTransport(httpx.MockTransport(lambda r:pytest.fail('sent')),budget)
        auth=IOLAuth(username='user',password='pass',env='production',token_path=tmp_path/'token.json',
                     http_client=httpx.Client(transport=transport))
        with pytest.raises(ExecutionBlocked) as caught:
            auth.headers()
        assert type(caught.value) is ExecutionBlocked and str(caught.value)=='Session login limit reached'
