from datetime import date,timedelta
from copy import deepcopy
import pytest
from argbot.market import daily_rows

DAY=date(2026,10,1);PREVIOUS=date(2026,9,30)
def history(n=80):
    return [dict(fechaHora=(PREVIOUS-timedelta(days=i)).isoformat()+'T17:00:00',
        apertura=100,maximo=110,minimo=90,ultimoPrecio=105,volumenNominal=1000) for i in range(n)]

def test_bad_intraday_snapshot_does_not_replace_valid_final_daily_summary():
    raw=history();intraday=dict(raw[0],fechaHora=PREVIOUS.isoformat()+'T10:31:00',ultimoPrecio=111)
    before=deepcopy(raw)
    assert daily_rows(raw+[intraday],DAY,PREVIOUS)==daily_rows(raw,DAY,PREVIOUS)
    assert raw==before

def test_bad_old_day_truncates_prefix_without_filling_ohlc_or_internal_hole():
    raw=history();raw[50]['ultimoPrecio']=120;audit={}
    rows=daily_rows(raw,DAY,PREVIOUS,audit=audit)
    assert len(rows)==50 and rows[0]['fecha']==PREVIOUS-timedelta(days=49)
    assert audit['invalid_final_days']==[(PREVIOUS-timedelta(days=50)).isoformat()]
    assert raw[50]['maximo']==110 and raw[50]['ultimoPrecio']==120

@pytest.mark.parametrize('index',[0,1,34])
def test_bad_recent_final_bar_blocks_even_when_earlier_tick_is_valid(index):
    raw=history();valid=dict(raw[index],fechaHora=raw[index]['fechaHora'].replace('17:00','16:00'))
    raw[index]['ultimoPrecio']=120
    with pytest.raises(ValueError,match='insufficient'):daily_rows(raw+[valid],DAY,PREVIOUS)

def test_minimum_35_valid_recent_bars_unchanged():
    raw=history();raw[35]['minimo']=0
    assert len(daily_rows(raw,DAY,PREVIOUS))==35
    raw[34]['minimo']=0
    with pytest.raises(ValueError):daily_rows(raw,DAY,PREVIOUS)

def test_final_conflict_rejected_and_future_bad_rows_do_not_leak():
    raw=history();conflict=dict(raw[0],ultimoPrecio=106)
    with pytest.raises(ValueError,match='conflicting'):daily_rows(raw+[conflict],DAY,PREVIOUS)
    future=dict(raw[0],fechaHora=DAY.isoformat()+'T10:30:00',ultimoPrecio=-1)
    assert daily_rows(raw+[future],DAY,PREVIOUS)==daily_rows(raw,DAY,PREVIOUS)
