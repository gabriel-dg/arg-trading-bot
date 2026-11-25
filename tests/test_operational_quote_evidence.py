"""Offline semantics, privacy, retention and execution-independence regressions."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from test_operational_candidate import rig, raw_quote, Clock, config, Market, Broker, start
from argbot.strategy.candidate import Candidate
from argbot.execution import ExecutionController
from argbot.market import quote, quote_timing, inspect_quote
from argbot.quote_evidence import (
    QuoteEvidenceJournal, rejection_record, market_excerpt, decode_record, canonical,
    MAX_LEVELS, MAX_RECORD_BYTES)


def inspection(payload, clock):
    return inspect_quote(payload, 'QA0', clock(), max_age=60, max_spread='.02')


def record(clock=None):
    clock = clock or Clock()
    raw = raw_quote('QA0', clock)
    raw['fechaHora'] = (clock()-timedelta(seconds=120)).isoformat()
    return rejection_record(raw, 'QA0', clock(), validated_at=clock(),
        reason='stale_or_future_quote', refresh=False, cycle_at=clock().isoformat(),
        session=clock().date().isoformat(), previous=None, max_age=60, max_spread='.02')


def strict_age(bot):
    # These capture scenarios use 120 s sources as "rejected for age" against the
    # strict limit. The 300 s management default is covered in
    # test_operational_quote_age_by_use.py.
    bot.config['managed_quote_age_seconds'] = 60






def test_times_are_distinct_and_book_time_cannot_be_supplied_by_unknown_payload_field():
    clock = Clock(); raw = raw_quote('QA0', clock)
    raw['fechaHora'] = (clock()-timedelta(seconds=120)).isoformat()
    raw['book_at'] = raw['bookTimestamp'] = clock().isoformat()
    result = inspection(raw, clock)
    assert result['timing']['source_age_at_receipt_seconds'] == 120
    assert result['timing']['receive_age_at_validation_seconds'] == 0
    assert result['timing']['source_event'] == 'unspecified'
    assert result['timing']['book_at'] is None and result['timing']['trade_at'] is None
    assert result['book_freshness'] == 'unknown'
    assert result['checks']['book_structure']['status'] == 'valid'
    assert result['checks']['source_time']['reason'] == 'stale_or_future_quote'
    assert not result['execution_authorized']
    with pytest.raises(ValueError, match='stale_or_future_quote'):
        quote(raw, 'QA0', clock(), max_age=60, max_spread='.02')


def test_stale_source_does_not_hide_crossed_book_or_bad_identity():
    clock = Clock(); raw = raw_quote('OTHER', clock)
    raw['fechaHora'] = (clock()-timedelta(seconds=120)).isoformat()
    raw['puntas'][0]['precioVenta'] = 1
    result = inspection(raw, clock)
    assert result['checks']['source_time']['reason'] == 'stale_or_future_quote'
    assert result['checks']['identity']['reason'] == 'wrong_symbol'
    assert result['checks']['book_structure']['reason'] == 'crossed_or_wide_book'


@pytest.mark.parametrize('bad', ['PRIVATE_EXCEPTION_TEXT', None, {}, 123])
def test_malformed_source_still_diagnoses_structure_without_echoing_input(bad):
    clock = Clock(); raw = raw_quote('QA0', clock); raw['fechaHora'] = bad
    result = inspection(raw, clock)
    assert result['timing'] is None and result['checks']['source_time']['status'] == 'invalid'
    assert result['checks']['book_structure']['status'] == 'valid'
    assert 'PRIVATE_EXCEPTION_TEXT' not in json.dumps(result)


def test_reception_never_rejuvenates_frozen_source():
    clock = Clock(); raw = raw_quote('QA0', clock); source = raw['fechaHora']
    clock.now += timedelta(seconds=61)
    first = inspection(raw, clock)
    clock.now += timedelta(seconds=30)
    second = inspection(raw, clock)
    assert first['timing']['source_age_at_receipt_seconds'] == 61
    assert second['timing']['source_age_at_receipt_seconds'] == 91
    assert second['timing']['source_at'] == source
    assert second['checks']['source_time']['status'] == 'invalid'


def test_validated_time_preserves_causal_wait_and_received_time():
    clock = Clock(); receipt = clock(); raw = raw_quote('QA0', clock)
    raw['fechaHora'] = (receipt+timedelta(seconds=.2)).isoformat()
    timing = quote_timing(raw, receipt, receipt+timedelta(seconds=.2))
    timing.validate_source(60)
    result = timing.evidence()
    assert result['received_at'] == receipt.isoformat()
    assert result['source_age_at_receipt_seconds'] == -.2
    assert result['source_age_at_validation_seconds'] == 0
    assert result['receive_age_at_validation_seconds'] == .2


@pytest.mark.parametrize('payload', [None, [], 'PRIVATE_TOKEN', 42])
def test_non_object_payload_has_no_private_echo(payload):
    result = inspection(payload, Clock())
    assert all(c['status'] == 'invalid' for c in result['checks'].values())
    assert 'PRIVATE_TOKEN' not in json.dumps(result)


def test_large_book_diagnostic_is_bounded_and_not_reported_valid():
    raw = raw_quote('QA0', Clock()); raw['puntas'] *= 1001
    result = inspection(raw, Clock())
    assert result['checks']['book_structure'] == dict(status='not_checked', reason='diagnostic_level_limit')


def test_excerpt_is_closed_whitelist_type_safe_bounded_and_non_mutating():
    raw = raw_quote('QA0', Clock()); raw['puntas'] *= 100
    raw.update(access_token='TOKEN_SECRET', password='PASSWORD_SECRET',
               account={'balance': 'BALANCE_SECRET'}, ultimoPrecio='PRICE_SECRET',
               precioPromedio=float('nan'), volumenNominal=10**1000,
               moneda='CURRENCY_SECRET', fechaHora='TIME_SECRET')
    raw['puntas'][0] = dict(raw['puntas'][0], password='BOOK_SECRET')
    before = deepcopy(raw); excerpt = market_excerpt(raw); encoded = canonical(excerpt)
    for secret in (b'TOKEN_SECRET', b'PASSWORD_SECRET', b'BALANCE_SECRET', b'PRICE_SECRET',
                   b'CURRENCY_SECRET', b'TIME_SECRET', b'BOOK_SECRET'):
        assert secret not in encoded
    assert len(excerpt['puntas']) == MAX_LEVELS and excerpt['book_truncated']
    assert excerpt['book_levels_total'] == 100 and len(encoded) < MAX_RECORD_BYTES
    assert raw['access_token'] == before['access_token'] and len(raw['puntas']) == 100


def test_journal_roundtrip_checksum_restart_and_hard_byte_cap(tmp_path):
    data = record(); journal = QuoteEvidenceJournal(tmp_path)
    assert journal.append(data) == 'recorded'
    path = next(journal.directory.glob('*.jsonl')); original = path.read_bytes()
    assert decode_record(original) == data
    restarted = QuoteEvidenceJournal(tmp_path, max_bytes=2*len(original), max_record_bytes=len(original))
    assert restarted.append(data) == 'recorded'
    assert restarted.append(data) == 'session_limit'
    assert path.read_bytes() == original*2
    assert QuoteEvidenceJournal(tmp_path, max_bytes=2*len(original), max_record_bytes=len(original)).append(data) == 'session_limit'


def test_partial_tail_is_preserved_and_not_appended_after_restart(tmp_path):
    journal = QuoteEvidenceJournal(tmp_path); journal.append(record())
    path = next(journal.directory.glob('*.jsonl'))
    with path.open('ab') as stream: stream.write(b'{"incomplete":')
    before = path.read_bytes()
    assert QuoteEvidenceJournal(tmp_path).append(record()) == 'incomplete_tail'
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match='Incomplete'):
        decode_record(b'{"incomplete":')


def test_corrupt_record_checksum_is_not_hidden(tmp_path):
    journal = QuoteEvidenceJournal(tmp_path); journal.append(record())
    line = next(journal.directory.glob('*.jsonl')).read_bytes()
    changed = json.loads(line); changed['record']['reason'] = 'changed'
    with pytest.raises(ValueError, match='checksum'):
        decode_record(canonical(changed)+b'\n')


@pytest.mark.parametrize('session', ['../escape', '2026-10-05/../escape', '20261005', None])
def test_invalid_session_cannot_escape_journal(tmp_path, session):
    data = record(); data['session'] = session
    before = set(tmp_path.rglob('*'))
    with pytest.raises((ValueError, TypeError)):
        QuoteEvidenceJournal(tmp_path).append(data)
    assert set(tmp_path.rglob('*')) == before


def test_oversized_record_is_not_written(tmp_path):
    data = record(); data['reason'] = 'x'*MAX_RECORD_BYTES
    before = set(tmp_path.rglob('*'))
    assert QuoteEvidenceJournal(tmp_path).append(data) == 'record_limit'
    assert set(tmp_path.rglob('*')) == before


def test_rejection_uses_one_existing_response_preserves_cache_and_private_context(rig, monkeypatch):
    bot,c,b,m,clock,cfg,folder = rig
    first = bot._fetch('QA0'); before = deepcopy(bot.runtime['quotes'])
    clock.now += timedelta(seconds=80)
    raw = raw_quote('QA0', clock); raw['fechaHora'] = first['fechaHora']
    raw['puntas'][0]['cantidadCompra'] += 100
    calls = []
    monkeypatch.setattr(m, 'quote', lambda symbol: (calls.append(symbol), deepcopy(raw))[1])
    bot._cycle_audit = dict(started_at=clock().isoformat(), observations=[])
    assert bot._fetch('QA0', refresh=True) is None
    assert calls == ['QA0'] and not b.sent and bot.runtime['quotes'] == before
    path = next((folder/'quote-rejections').glob('*.jsonl'))
    saved = decode_record(path.read_bytes())
    assert saved['current']['fechaHora'] == first['fechaHora']
    assert saved['previous_cached']['fechaHora'] == first['fechaHora']
    assert saved['current']['puntas'] != saved['previous_cached']['puntas']
    assert saved['previous_cache_is_not_freshness_evidence']
    assert saved['diagnosis']['book_freshness'] == 'unknown'
    assert bot._cycle_audit['observations'][-1]['rejection_evidence'] == 'recorded'
    assert 'quote-rejections' not in json.dumps(c.state)


def test_accepted_response_does_not_create_rejection_file(rig):
    bot,_,_,_,_,_,folder = rig
    assert bot._fetch('QA0')
    assert not (folder/'quote-rejections').exists()


def test_capture_latency_is_not_reported_as_http_latency(rig, monkeypatch):
    bot,_,_,m,clock,_,_ = rig; strict_age(bot)
    initial = clock(); raw = raw_quote('QA0', clock)
    raw['fechaHora'] = (initial-timedelta(seconds=120)).isoformat()
    def fetch(_):
        clock.now += timedelta(seconds=.4)
        return raw
    def delayed_disk(_):
        clock.now += timedelta(seconds=3)
        return 'recorded'
    monkeypatch.setattr(m, 'quote', fetch)
    monkeypatch.setattr(bot._quote_evidence, 'append', delayed_disk)
    bot._cycle_audit = dict(observations=[])
    assert bot._fetch('QA0') is None
    observed = bot._cycle_audit['observations'][-1]
    assert observed['request_to_receipt_seconds'] == .4
    assert observed['elapsed_seconds'] == 3.4
    assert observed['received_at'] == (initial+timedelta(seconds=.4)).isoformat()


def test_sync_failure_is_not_reported_recorded_and_existing_records_survive(tmp_path, monkeypatch):
    import argbot.quote_evidence as module
    journal = QuoteEvidenceJournal(tmp_path); journal.append(record())
    path = next(journal.directory.glob('*.jsonl')); before = path.read_bytes()
    def fail(_): raise OSError('fictional disk failure')
    monkeypatch.setattr(module.os, 'fsync', fail)
    with pytest.raises(OSError): journal.append(record())
    assert path.read_bytes().startswith(before)


def test_untrusted_payload_cannot_supply_local_received_time():
    raw = raw_quote('QA0', Clock())
    raw.update(received_at=Clock()().isoformat(), source_at=Clock()().isoformat(),
               validated_at=Clock()().isoformat())
    current = market_excerpt(raw)
    assert not {'received_at', 'source_at', 'validated_at'} & set(current)
    assert market_excerpt(raw, cached=True)['received_at'] == raw['received_at']


def test_transport_failure_does_not_invent_received_payload(rig):
    bot,_,_,m,_,_,folder = rig; m.bad.add('QA0')
    assert bot._fetch('QA0') is None
    assert not (folder/'quote-rejections').exists() and m.calls == ['QA0']


@pytest.mark.parametrize('failure', [PermissionError('SECRET'), OSError('SECRET'), RuntimeError('SECRET')])
def test_capture_failure_does_not_change_other_orders_scan_or_audit_policy(rig, monkeypatch, failure):
    bot,c,b,m,clock,cfg,folder = rig; original = m.quote; bad = cfg['universe'][0]
    def fetch(symbol):
        raw = original(symbol)
        if symbol == bad: raw['fechaHora'] = (clock()-timedelta(seconds=120)).isoformat()
        return raw
    def fail(_): raise failure
    monkeypatch.setattr(m, 'quote', fetch); monkeypatch.setattr(bot._quote_evidence, 'append', fail)
    bot.cycle()
    assert len(m.calls) == 30 and len(b.sent) == 6
    assert bad not in {o['symbol'] for o in b.sent}
    assert bot.runtime['entries_blocked'] is None and bot.runtime['telemetry_ok']
    text = (folder/'operational-cycles.private.jsonl').read_text(encoding='utf-8')
    assert 'unavailable' in text and 'SECRET' not in text


@pytest.mark.parametrize('status', ['session_limit', 'record_limit', 'incomplete_tail'])
def test_capture_limits_are_visible_without_admitting_old_quote(rig, monkeypatch, status):
    bot,_,b,m,clock,_,_ = rig; strict_age(bot); raw = raw_quote('QA0', clock)
    raw['fechaHora'] = (clock()-timedelta(seconds=120)).isoformat()
    monkeypatch.setattr(m, 'quote', lambda _: raw)
    monkeypatch.setattr(bot._quote_evidence, 'append', lambda _: status)
    bot._cycle_audit = dict(observations=[])
    assert bot._fetch('QA0') is None and not b.sent
    assert bot._cycle_audit['observations'][-1]['rejection_evidence'] == status
    assert bot.runtime['exclusions']['QA0'] == 'stale_or_future_quote'






