"""Executable synthetic scenario through the real client + IOL normalizers.

httpx.MockTransport is mandatory here. No sockets, auth token or environment.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import httpx

from argbot.brokers.iol.budget import SessionBudget, BudgetTransport
from argbot.strategy.candidate import Candidate, ART
from argbot.execution import ExecutionController
from argbot.brokers.iol.adapter import NoReplayIOLClient, IOLExecutionBroker
from argbot.brokers.iol.market import IOLMarket
from argbot.brokers.iol.readers import IOLReaders


class SyntheticAPI:
    def __init__(self):
        self.now=datetime(2026,9,30,11,15,tzinfo=ART)
        self.cash=Decimal('100000');self.positions={};self.orders={};self.requests=0
        self.prices={};self.writes=[]

    def clock(self):return self.now

    def handle(self,r):
        self.requests+=1
        p=r.url.path
        if p.endswith('/estadocuenta'):
            held=sum((o['cantidad']-sum(f['cantidad'] for f in o['operaciones']))*Decimal(str(o['precio']))*Decimal('1.01') for o in self.orders.values() if o['tipo']=='compra' and o['estadoActual'] not in ('terminada','cancelada'))
            result={'cuentas':[dict(numero='DEMO',tipo='inversion_Argentina_Pesos',moneda='peso_Argentino',estado='operable',disponible=str(self.cash-held),
                                   saldos=[dict(liquidacion='inmediato',disponibleOperar=str(self.cash-held)),
                                           dict(liquidacion='hrs24',disponibleOperar=str(self.cash-held))])]}
        elif '/portafolio/' in p:
            result={'pais':'argentina','activos':[{'titulo':{'simbolo':s},'cantidad':q} for s,q in self.positions.items() if q]}
        elif p.endswith('/operaciones'):
            result=[{'numero':n,'estado':o['estadoActual']} for n,o in self.orders.items() if o['estadoActual'] not in ('terminada','cancelada')]
        elif '/operaciones/' in p:
            o=self.orders[int(p.rsplit('/',1)[1])]
            if r.method=='DELETE':
                self.writes.append(('DELETE',o['numero']));o['estadoActual']='cancelada'
                result={'ok':True}
            else:result=o
        elif '/operar/' in p:
            body=json.loads(r.content);n=len(self.orders)+1
            self.writes.append((r.method,n))
            self.orders[n]=dict(numero=n,mercado=body['mercado'],simbolo=body['simbolo'],
                moneda='peso_Argentino',tipo='compra' if p.endswith('/Comprar') else 'venta',
                plazo='a24horas',modalidad='precio_Limite',fechaAlta=self.now.isoformat(),
                estadoActual='en_Proceso',cantidad=body['cantidad'],precio=body['precio'],
                operaciones=[],arancelesARS='0',arancelesUSD='0')
            result={'numeroOperacion':n}
        elif 'seriehistorica' in p:
            previous=self.now.date()-timedelta(days=1)
            result=[dict(fechaHora=(previous-timedelta(days=40-i)).isoformat(),apertura=99,
                         maximo=100,minimo=98,ultimoPrecio=99,volumenNominal=1000) for i in range(41)]
            result[-1].update(apertura=92,maximo=92,minimo=90,ultimoPrecio=91)
        elif '/Cotizacion' in p:
            s=p.split('/Titulos/')[1].split('/')[0];price=self.prices.get(s,90)
            result=dict(simbolo=s,fechaHora=self.now.isoformat(),moneda='peso_Argentino',plazo='T1',
                lote=1,laminaMinima=1,ultimoPrecio=price,minimo=min(price,89),maximo=max(price,100),
                montoOperado=100000+(self.now.hour*60+self.now.minute)*100,
                puntas=[dict(precioCompra=price-.1,precioVenta=price+.1,cantidadCompra=10000,cantidadVenta=10000)])
        else:raise AssertionError('Unexpected synthetic endpoint: '+p)
        return httpx.Response(200,json=result)

    def fill(self,n,quantity=None):
        o=self.orders[n];done=sum(f['cantidad'] for f in o['operaciones'])
        new=(quantity or o['cantidad'])-done
        if new<=0:return
        price=Decimal(str(o['precio']));gross=new*price;fees=gross*Decimal('.01')
        o['operaciones'].append(dict(fecha=self.now.isoformat(),cantidad=new,precio=str(price)))
        o['arancelesARS']=str(Decimal(o['arancelesARS'])+fees)
        o['estadoActual']='terminada' if done+new==o['cantidad'] else 'parcialmente_Terminada'
        sign=1 if o['tipo']=='compra' else -1
        self.positions[o['simbolo']]=self.positions.get(o['simbolo'],0)+sign*new
        self.cash+=-sign*gross-fees


class SyntheticAuth:
    base_url='https://offline.invalid'
    def headers(self):return {'Accept':'application/json'}
    def invalidate_access_token(self):pass
    def close(self):pass


def run_demo(output, config_path):
    output=Path(output)
    if output.exists():raise FileExistsError('Use a new demo directory')
    cfg=json.loads(Path(config_path).read_text(encoding='utf-8'))
    cfg.update(status='synthetic',account_id='IOL:inversion_Argentina_Pesos:DEMO',
               assigned_capital='100000',fee_reserve='.01',max_exposure_pct='.6',max_daily_loss_pct='.03')
    api=SyntheticAPI();readers=IOLReaders(naive_order_timezone=cfg.get('broker_order_timezone'))
    ExecutionController.prepare(output,cfg['account_id'],cash=cfg['assigned_capital'],fee_reserve=cfg['fee_reserve'])
    with ExecutionController(output,cfg['account_id'],None) as control:
        budget=SessionBudget(control,api.now.date(),cfg['http_session_budget'],cfg['http_management_reserve'])
        with httpx.Client(transport=BudgetTransport(httpx.MockTransport(api.handle),budget)) as http:
            client=NoReplayIOLClient(SyntheticAuth(),http)
            control.broker=IOLExecutionBroker(client,read_order=readers.order,read_account=readers.account)
            market=IOLMarket(client,budget)
            bot=Candidate(control,market,cfg,clock=api.clock)
            def start(candidate):
                candidate.start_session(api.now.date(),api.now.date()-timedelta(days=1),clock_evidence={
                    'session':api.now.date().isoformat(),'checked_at':api.now.isoformat(),
                    'samples':3,'offset_seconds':'0.1','source':'synthetic_not_an_ntp_measurement'})
            start(bot);bot.cycle()
            assert len(api.orders)==5 and not control.state['positions']
            api.now+=timedelta(minutes=5);api.fill(1,2);bot.cycle()
            first=api.orders[1]['simbolo']
            assert control.state['positions'][first]['quantity']==2
            api.now+=timedelta(minutes=5)
            for n in list(api.orders):api.fill(n)
            bot.cycle()
            assert len(control.state['positions'])==5
            # Persist a manual entry pause; stops continue to operate.
            control.pause(True)
            stop=control.state['positions'][first]['swing']['stop_nivel']
            api.prices[first]=stop-1
            api.now+=timedelta(minutes=5);bot.cycle()
            assert len(api.orders)==5
            # New strategy instance over the durable state; process-level restart
            # and lease recovery are covered separately by failure-boundary tests.
            bot=Candidate(control,market,cfg,clock=api.clock);start(bot)
            api.now+=timedelta(minutes=5);bot.cycle()
            assert len(api.orders)==6 and api.orders[6]['tipo']=='venta'
            api.now+=timedelta(minutes=5);api.fill(6);bot.cycle()
            state=control.state
            assert first not in state['positions'] and state['paused']
            assert Decimal(state['cash'])==api.cash
            assert {s:p['quantity'] for s,p in state['positions'].items()}=={s:q for s,q in api.positions.items() if q}
            assert state['http_budget']['used']==api.requests
            result={'kind':'synthetic_integrated_candidate','symbols':len(cfg['universe']),
                    'orders':len(api.orders),'closed_positions':1,'remaining_positions':len(state['positions']),
                    'http_attempts_simulated':api.requests,'cash_matches_broker':True,
                    'quantities_match_broker':True,'real_network_requests':0,
                    'real_orders':0,'strategy_recreated_without_replaying_window':True,
                    'status':state['strategy']['status'],'live_ready':False}
    (output/'report.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result
