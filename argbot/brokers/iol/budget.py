"""One durable HTTP counter for broker reads/writes AND authentication.

Use two BudgetTransport wrappers sharing one counter when auth and IOLClient
have separate httpx clients. Every transport attempt is charged BEFORE send.
"""
import httpx
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs

from argbot.execution import ExecutionBlocked, RequestNotSent

# Password logins per session, failed attempts included: each is charged before it is sent.
PASSWORD_GRANT_LIMIT = 6


class SessionBudget:
    def __init__(self, controller, day, total, reserve, *, now=None):
        if type(total) is not int or type(reserve) is not int or not 0 < reserve < total:
            raise ValueError('Invalid HTTP budget')
        self.controller = controller
        self.day, self.total, self.reserve = day.isoformat(), total, reserve
        self.priority = set()
        self.now = now or (lambda:datetime.now(timezone.utc))
        old = controller.state.get('http_budget')
        if old and old['day'] > self.day:
            raise ValueError('Cannot reset budget to an earlier session')
        if old and old['day'] == self.day:
            if (old['total'],old['reserve']) != (total,reserve):
                raise ValueError('Cannot change a consumed budget')
        else:
            state=controller.state
            state['http_budget']={'day':self.day,'total':total,'reserve':reserve,'used':0,'market_data':0}
            controller._commit(state)

    def charge(self,request):
        path=request.url.path
        market_data='/Cotizacion' in path
        symbol=path.split('/Titulos/',1)[1].split('/',1)[0] if '/Titulos/' in path else None
        management=not market_data or (symbol in self.priority and 'seriehistorica' not in path)
        state=self.controller.state
        counter=state['http_budget']
        if counter.get('retry_after') and self.now() < datetime.fromisoformat(counter['retry_after']):
            raise ExecutionBlocked('Broker Retry-After active')
        if counter['day'] != self.day:
            raise ExecutionBlocked('HTTP session changed')
        ceiling=self.total if management else self.total-self.reserve
        if counter['used'] >= ceiling:
            raise ExecutionBlocked('HTTP management reserve reached' if not management else 'HTTP budget exhausted')
        if path == '/token':
            grant=parse_qs(request.content.decode('utf-8')).get('grant_type',['password'])[0]
            if grant != 'refresh_token':
                if counter.get('password_grants',0) >= PASSWORD_GRANT_LIMIT:
                    raise ExecutionBlocked('Session login limit reached')
                counter['password_grants']=counter.get('password_grants',0)+1
        counter['used']+=1
        counter['market_data']+=int(market_data)
        self.controller._commit(state)

    def response(self, response):
        if response.status_code != 429 and 'Retry-After' not in response.headers:
            return
        raw=response.headers.get('Retry-After','60')
        try:
            until=self.now()+timedelta(seconds=max(0,int(raw)))
        except ValueError:
            try:
                until=parsedate_to_datetime(raw)
                if until.tzinfo is None:raise ValueError('Missing timezone')
            except (ValueError,TypeError,OverflowError):
                until=self.now()+timedelta(seconds=60)
        state=self.controller.state
        state['http_budget']['retry_after']=max(self.now(),until).isoformat()
        self.controller._commit(state)


class BudgetTransport(httpx.BaseTransport):
    def __init__(self, transport, budget, *, before_send=None, quote_timeout_seconds=None):
        self.inner,self.budget=transport,budget
        self.before_send=before_send
        if quote_timeout_seconds is not None and (type(quote_timeout_seconds) is not int or quote_timeout_seconds != 5):
            raise ValueError('Unsupported quote timeout policy')
        self.quote_timeout_seconds=quote_timeout_seconds
    def handle_request(self,request):
        try:
            if self.before_send is not None:
                self.before_send(request)
            self.budget.charge(request)
        except Exception as exc:
            # Reads and logins keep their errors. An order mutation refused here never
            # reached the network, so the controller may release it instead of
            # recording an unknown outcome.
            if request.method in {'GET','HEAD','OPTIONS'} or request.url.path=='/token':
                raise
            raise RequestNotSent(str(exc) if type(exc) is ExecutionBlocked else type(exc).__name__) from exc
        if (self.quote_timeout_seconds is not None and request.method=='GET'
                and '/Cotizacion' in request.url.path and 'seriehistorica' not in request.url.path):
            # HTTPX inactivity bounds apply only to quote reads. Auth, account,
            # historical reads, POST and DELETE keep their existing behavior.
            request.extensions['timeout']={name:float(self.quote_timeout_seconds)
                                           for name in ('connect','read','write','pool')}
        response=self.inner.handle_request(request)
        self.budget.response(response)
        return response
    def close(self):
        self.inner.close()
