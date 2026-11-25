"""Integrated candidate scenarios; fake market and broker, no credentials/network."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from argbot.strategy.candidate import Candidate, ART, validate_config
from argbot.execution import ExecutionController, AccountView, OrderView
from argbot.market import daily_rows
from argbot.market import quote
from argbot.brokers.iol.readers import normalize_account, normalize_order, IOLReaders


class Clock:
    now = datetime(2026,9,30,11,20,tzinfo=ART)
    def __call__(self): return self.now
    def advance(self, minutes=5): self.now += timedelta(minutes=minutes)


def config(account='synthetic-account'):
    c=json.loads(Path('tests/fixtures/candidate.json').read_text())
    c.update(status='synthetic',account_id=account,assigned_capital='100000',fee_reserve='0.01',
             max_exposure_pct='0.6',max_daily_loss_pct='0.03')
    return c


def raw_quote(symbol,clock,price=90):
    return dict(simbolo=symbol,fechaHora=clock().isoformat(),moneda='peso_Argentino',plazo='T1',
                lote=1,laminaMinima=1,ultimoPrecio=price,minimo=min(89,price),maximo=max(100,price),
                montoOperado=100000+(clock().hour*60+clock().minute)*100,
                puntas=[dict(precioCompra=price-0.1,precioVenta=price+0.1,cantidadCompra=10000,cantidadVenta=10000)])


class Market:
    def __init__(self,clock):
        self.clock=clock; self.calls=[]; self.prices={}; self.bad=set()
    def quote(self,s):
        self.calls.append(s)
        if s in self.bad: raise TimeoutError()
        return raw_quote(s,self.clock,self.prices.get(s,90))
    def history(self,symbols,day,previous):
        rows=[dict(fecha=previous-timedelta(days=40-i),apertura=99.,maximo=100.,minimo=98.,cierre=99.,volumen=1000.) for i in range(41)]
        rows[-1].update(apertura=92.,maximo=92.,minimo=90.,cierre=91.)
        return {s:deepcopy(rows) for s in symbols},{}


class Broker:
    def __init__(self,cfg):
        self.identity=cfg['account_id'];self.cash=Decimal(cfg['assigned_capital']); self.positions={}
        self.orders={};self.sent=[];self.cancels=[];self.fail=False
    def account(self):
        if self.fail: raise TimeoutError()
        held=sum((v.requested_quantity-v.filled_quantity)*Decimal(v.limit)*Decimal('1.01') for v in self.orders.values() if v.side=='BUY' and v.status=='open')
        return AccountView(str(self.cash-held),dict(self.positions),frozenset(k for k,v in self.orders.items() if v.status=='open'),self.identity)
    def submit(self,o):
        self.sent.append(deepcopy(o));number=str(len(self.sent))
        self.orders[number]=OrderView(number,o['symbol'],o['side'],o['quantity'],o['limit'],o['created_at'],'open',0,'0','0',self.identity)
        return number
    def order(self,n): return self.orders[n]
    def cancel(self,n):
        self.cancels.append(n);self.orders[n]=replace(self.orders[n],status='cancelled')
    def fill(self,n,qty=None):
        o=self.orders[n];qty=qty or o.requested_quantity
        gross=Decimal(o.limit)*qty;fees=gross*Decimal('.01')
        dq=qty-o.filled_quantity;dg=gross-Decimal(o.gross);df=fees-Decimal(o.fees)
        self.cash+=(-dg if o.side=='BUY' else dg)-df
        self.positions[o.symbol]=self.positions.get(o.symbol,0)+(dq if o.side=='BUY' else -dq)
        if not self.positions[o.symbol]: del self.positions[o.symbol]
        self.orders[n]=replace(o,filled_quantity=qty,gross=str(gross),fees=str(fees),status='filled' if qty==o.requested_quantity else 'open')


@pytest.fixture
def rig(tmp_path,monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('network'))
    cfg=config(str(tmp_path));clock=Clock();market=Market(clock);broker=Broker(cfg)
    folder=tmp_path/'candidate'
    ExecutionController.prepare(folder,cfg['account_id'],cash=cfg['assigned_capital'],fee_reserve=cfg['fee_reserve'])
    controller=ExecutionController(folder,cfg['account_id'],broker)
    bot=Candidate(controller,market,cfg,clock=clock)
    start(bot,clock)
    yield bot,controller,broker,market,clock,cfg,folder
    controller.close()


def start(bot,clock):
    bot.start_session(clock().date(),clock().date()-timedelta(days=1),
                      clock_evidence={'session':clock().date().isoformat(),'checked_at':clock().isoformat(),
                                      'samples':3,'offset_seconds':'0.1','source':'synthetic'})


def test_full_universe_entries_partial_stop_sale_restart(rig):
    bot,c,b,m,clock,cfg,folder=rig
    bot.cycle()
    assert len(m.calls)==30 and len(b.sent)==6
    assert c.state['positions']=={}
    first=b.orders['1'];b.fill('1',2)
    clock.advance();bot.cycle()
    assert c.state['positions'][first.symbol]['quantity']==2
    assert m.calls[30]==first.symbol  # managed holdings queried before candidate scan
    # Finish buys, then ratchet stop at an actual higher mark.
    for n in list(b.orders): b.fill(n)
    clock.advance();bot.cycle()
    assert len(c.state['positions'])==6
    m.prices[first.symbol]=94
    clock.advance();bot.cycle()
    stop=c.state['positions'][first.symbol]['swing']['stop_nivel']
    # New position's phase-one ATR stop can be far away; exercise two confirmations.
    m.prices[first.symbol]=max(1,stop-1)
    clock.advance();bot.cycle();assert len(b.sent)==6
    c.close()
    with ExecutionController(folder,cfg['account_id'],b) as resumed:
        nextbot=Candidate(resumed,m,cfg,clock=clock);start(nextbot,clock)
        assert nextbot.runtime['windows']==['11:20']
        clock.advance();nextbot.cycle()
        clock.advance();nextbot.cycle()
        assert len(b.sent)==7 and b.sent[-1]['side']=='SELL'
        assert b.sent[-1]['quantity']==first.requested_quantity
        b.fill('7');clock.advance();nextbot.cycle()
        assert first.symbol not in resumed.state['positions']
        assert resumed.state['strategy']['closed_today']==1
        assert first.symbol in resumed.state['strategy']['cooldowns']


def test_bad_symbol_does_not_block_other_29_and_metadata_is_strict(rig):
    bot,c,b,m,clock,cfg,_=rig
    m.bad.add(cfg['universe'][0]);bot.cycle()
    assert len(m.calls)==30 and b.sent
    assert cfg['universe'][0] not in {o['symbol'] for o in b.sent}
    assert bot.runtime['exclusions'][cfg['universe'][0]]=='TimeoutError'


def test_failed_reconciliation_blocks_entire_batch_and_preserves_window(rig):
    bot,c,b,m,clock,_,_=rig
    b.fail=True;bot.cycle()
    assert not b.sent and not m.calls and not bot.runtime['windows']
    assert c.state['strategy']['status']=='blocked'
    b.fail=False;clock.advance();bot.cycle();assert b.sent


def test_pending_buy_timeout_not_resent_in_same_window(rig):
    bot,c,b,m,clock,cfg,_=rig
    bot.cycle();clock.advance(cfg['strategy']['buy_timeout_min']);bot.cycle()
    assert len(b.cancels)==6 and len(b.sent)==6
    assert Decimal(c.state['cash'])==Decimal('100000')


def test_daily_loss_blocks_entries_but_keeps_positions_managed(rig):
    bot,c,b,m,clock,_,_=rig
    bot.cycle()
    for n in list(b.orders):b.fill(n)
    clock.advance();bot.cycle()
    for s in b.positions:m.prices[s]=70
    clock.advance();bot.cycle()
    assert bot.runtime['risk_paused']
    assert not any(o['side']=='BUY' for o in b.sent[6:])
    assert any(e['code']=='daily_loss_limit' for e in bot.runtime['incidents'])


def test_new_day_baseline_uses_previous_close_not_entry_cost_and_survives_restart(rig):
    bot,c,b,m,clock,cfg,folder=rig
    bot.cycle()
    for n in list(b.orders):b.fill(n)
    clock.advance();bot.cycle()
    clock.now=datetime(2026,10,1,10,30,tzinfo=ART)
    start(bot,clock)
    baseline=Decimal(c.state['cash'])+sum(p['quantity']*Decimal('91')*Decimal('.99') for p in c.state['positions'].values())
    assert Decimal(bot.runtime['opening_equity'])==baseline
    assert baseline!=Decimal(c.state['cash'])+sum(Decimal(p['cost']) for p in c.state['positions'].values())
    bot.runtime['risk_paused']=True;bot._save();c.close()
    with ExecutionController(folder,cfg['account_id'],b) as resumed:
        second=Candidate(resumed,m,cfg,clock=clock);start(second,clock)
        assert Decimal(second.runtime['opening_equity'])==baseline and second.runtime['risk_paused']


def test_unverified_daily_baseline_blocks_buys_but_does_not_guess_zero(rig):
    bot,c,b,m,clock,_,_=rig
    bot.runtime['opening_equity']=None;bot.cycle()
    assert not b.sent and bot.runtime['entries_blocked']=='daily_baseline_unavailable'


def test_missing_position_quote_blocks_new_buys_and_breaks_stop_confirmation(rig):
    bot,c,b,m,clock,_,_=rig
    bot.cycle();b.fill('1');clock.advance();bot.cycle()
    s=b.orders['1'].symbol;m.bad.add(s)
    clock.advance(145);bot.cycle()
    assert len(b.sent)==6 and bot.runtime['entries_blocked']=='managed_quote_missing'
    assert c.state['positions'][s]['swing']['stop_toques']==0


def test_day_change_preserves_ratchet_and_cannot_reuse_clock(rig):
    bot,c,b,m,clock,_,_=rig
    bot.cycle();b.fill('1');clock.advance();bot.cycle()
    old=c.state['positions'][b.orders['1'].symbol]['swing']
    clock.now=datetime(2026,10,1,10,30,tzinfo=ART)
    with pytest.raises(Exception,match='session'):bot.cycle()
    start(bot,clock);bot.cycle()
    new=c.state['positions'][b.orders['1'].symbol]['swing']
    assert new['ts_entrada']==old['ts_entrada'] and new['stop_nivel']>=old['stop_nivel']


def test_draft_cannot_run_without_money_decisions():
    cfg=json.loads(Path('tests/fixtures/candidate.json').read_text())
    with pytest.raises(ValueError,match='configuration pending'):validate_config(cfg)


def test_history_rejects_stale_and_future_rows_never_change_indicators():
    raw=[dict(fechaHora=(date(2026,9,29)-timedelta(days=i)).isoformat(),apertura=100,maximo=110,minimo=80,ultimoPrecio=90,volumenNominal=1000) for i in range(40)]
    rows=daily_rows(raw,date(2026,9,30),date(2026,9,29))
    assert daily_rows(raw+[dict(raw[0],fechaHora='2026-09-30',ultimoPrecio=100000)],date(2026,9,30),date(2026,9,29))==rows
    with pytest.raises(ValueError):daily_rows(raw[1:],date(2026,9,30),date(2026,9,29))


def account_payload():
    return {'cuentas':[dict(numero='123',tipo='inversion_Argentina_Pesos',moneda='peso_Argentino',estado='operable',disponible=1000,
                           saldos=[dict(liquidacion='hrs24',disponibleOperar=1000)])]}


def detail():
    return dict(numero=7,mercado='bCBA',simbolo='QA0',moneda='peso_Argentino',tipo='compra',
                plazo='a24horas',modalidad='precio_Limite',fechaAlta='2026-09-30T11:15:00-03:00',
                estadoActual='parcialmente_Terminada',cantidad=4,precio=100,
                operaciones=[dict(fecha='2026-09-30T11:16:00-03:00',cantidad=2,precio=99)],
                arancelesARS=1.98,arancelesUSD=0)


def test_reader_uses_executions_not_limit_and_verified_account():
    a=normalize_account(account_payload(),{'pais':'argentina','activos':[]},[])
    v=normalize_order(detail(),a.account_id)
    assert v.filled_quantity==2 and Decimal(v.gross)==198 and Decimal(v.fees)==Decimal('1.98')
    assert a.account_id=='IOL:inversion_Argentina_Pesos:123'


def test_terminada_with_part_executed_is_a_partial_cancel():
    # The broker can mark a partly filled order "terminada" after a cancel request, like a full fill.
    v=normalize_order(dict(detail(),estadoActual='terminada'),'account')
    assert v.status=='cancelled' and v.filled_quantity==2 and Decimal(v.fees)==Decimal('1.98')


@pytest.mark.parametrize('change',[{'operaciones':None},{'estadoActual':'terminada','operaciones':[],'arancelesARS':0},
    {'fechaAlta':'2026-09-30T11:15:00'},
    {'arancelesARS':None},{'arancelesUSD':1},{'cantidadOperada':3},{'moneda':'dolar_Estadounidense'},
    {'estadoActual':'en_Modificacion'},{'cantidad':1}])
def test_reader_rejects_ambiguous_execution(change):
    with pytest.raises((ValueError,TypeError)):normalize_order(dict(detail(),**change),'account')


@pytest.mark.parametrize('change',[{'moneda':'dolar_Estadounidense'},{'lote':100}, {'laminaMinima':10},
    {'puntas':[]},{'ultimoPrecio':float('nan')},{'fechaHora':'2026-09-30T11:16:00-03:00'},
    {'fechaHora':'2026-09-30T11:13:59-03:00'},{'simbolo':'WRONG'}])
def test_quote_units_time_and_book_are_admission_guards(change):
    clock=Clock()
    with pytest.raises(ValueError):quote(dict(raw_quote('QA0',clock),**change),'QA0',clock(),max_age=60,max_spread='.02')


def test_management_marks_cannot_go_stale_while_scanning_other_symbols(rig,monkeypatch):
    bot,c,b,m,clock,_,_=rig
    bot.cycle();b.fill('1');clock.advance();bot.cycle()
    original=m.quote
    def slow(symbol):
        clock.now+=timedelta(seconds=4)
        return original(symbol)
    monkeypatch.setattr(m,'quote',slow)
    clock.advance(145);bot.cycle()
    assert bot.runtime['entries_blocked'] is None
    assert len(bot.runtime['managed_quote_refreshes'])==1
    assert all(datetime.fromisoformat(o['quote_expires_at'])>=datetime.fromisoformat(o['created_at'])
               for o in c.state['orders'].values())


def test_single_session_loop_halt_and_close_preserve_funds(rig):
    bot,c,b,m,clock,_,folder=rig
    clock.now=datetime(2026,9,30,16,59,tzinfo=ART)
    (folder/'.halt').write_text('pause')
    bot.run_session(sleep=lambda seconds:clock.advance(1))
    assert c.state['paused'] and not b.sent and c.state['cash']=='100000'
    assert bot.runtime['status']=='stopped'
