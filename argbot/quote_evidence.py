"""Bounded private rejection evidence. No broker/model dependencies or admission.

One writer: the candidate already holds its AccountLease. Per session and state
directory, retain at most 8 MiB; never rotate/delete earlier evidence. A partial
last line closes collection visibly. Reopening the process does not reset limits.
"""
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re

from argbot.execution import instant
from argbot.market import inspect_quote

MAX_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_LEVELS = 20
NUMBERS = ('lote', 'laminaMinima', 'ultimoPrecio', 'minimo', 'maximo',
           'montoOperado', 'volumenNominal', 'cantidadOperaciones', 'precioPromedio')
BOOK_FIELDS = ('precioCompra', 'cantidadCompra', 'precioVenta', 'cantidadVenta')
NUMERIC_TEXT = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')


def _number(value):
    if type(value) in (int, float) and not isinstance(value, bool):
        if (type(value) is int and value.bit_length() < 212) or (type(value) is float and math.isfinite(value)):
            return value
    if type(value) is str and len(value) <= 64 and NUMERIC_TEXT.fullmatch(value):
        return value
    # Do not serialize arbitrary API strings, nested objects or exception bodies.
    return {'not_retained': 'invalid_or_oversized_number'}


def _timestamp(value):
    if type(value) is str and len(value) <= 64:
        try:
            instant(value)
            return value  # preserve the original source spelling and offset
        except (ValueError, TypeError):
            pass
    return {'not_retained': 'invalid_timestamp'}


def market_excerpt(payload, *, cached=False):
    """Closed whitelist, retaining types where safe; explicitly mark truncation."""
    if type(payload) is not dict:
        return {'not_retained': 'payload_not_object'}
    result = {k: _number(payload[k]) for k in NUMBERS if k in payload}
    timestamp_keys = ('fechaHora', 'source_at', 'received_at', 'validated_at') if cached else ('fechaHora',)
    for key in timestamp_keys:
        if key in payload:
            result[key] = _timestamp(payload[key])
    if 'simbolo' in payload:
        symbol = payload['simbolo']
        result['simbolo'] = symbol if (type(symbol) is str and re.fullmatch(r'[A-Z0-9]{1,32}', symbol)) else {'not_retained': 'invalid_symbol'}
    enums = {'moneda': ('peso_Argentino', 'dolar_Estadounidense'),
             'plazo': ('t0', 't1', 't2', 't3', 'T0', 'T1', 'T2', 'T3')}
    for key, allowed in enums.items():
        if key in payload:
            value = payload[key]
            result[key] = value if type(value) is str and value in allowed else {'not_retained': 'unknown_enum'}
    if 'puntas' in payload:
        book = payload['puntas']
        if type(book) is list:
            result['puntas'] = [{k: _number(row[k]) for k in BOOK_FIELDS if k in row}
                                if type(row) is dict else {'not_retained': 'level_not_object'}
                                for row in book[:MAX_LEVELS]]
            result['book_levels_total'] = len(book)
            result['book_truncated'] = len(book) > MAX_LEVELS
        else:
            result['puntas'] = {'not_retained': 'book_not_array'}
    return result


def rejection_record(payload, symbol, received_at, *, validated_at, reason,
                     refresh, cycle_at, session, previous, max_age, max_spread):
    """Values stay local. The previous cache is context, never a replacement quote."""
    return dict(schema=1, kind='quote_rejection', session=session,
                symbol=symbol, received_at=received_at.isoformat(),
                validated_at=validated_at.isoformat(), cycle_at=cycle_at,
                reason=reason, refresh=refresh, current=market_excerpt(payload),
                previous_cached=market_excerpt(previous, cached=True) if previous is not None else None,
                previous_cache_is_not_freshness_evidence=True,
                diagnosis=inspect_quote(payload, symbol, received_at, max_age=max_age,
                                        max_spread=max_spread, validated_at=validated_at))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


class QuoteEvidenceJournal:
    def __init__(self, directory, *, max_bytes=MAX_BYTES, max_record_bytes=MAX_RECORD_BYTES):
        if (type(max_bytes) is not int or type(max_record_bytes) is not int or
                not 0 < max_record_bytes <= max_bytes <= MAX_BYTES or max_record_bytes > MAX_RECORD_BYTES):
            raise ValueError('Invalid private evidence bounds')
        self.directory = Path(directory) / 'quote-rejections'
        self.max_bytes, self.max_record_bytes = max_bytes, max_record_bytes

    def append(self, record):
        # A session date cannot escape the fixed private directory.
        session = record['session']
        if type(session) is not str or date.fromisoformat(session).isoformat() != session:
            raise ValueError('Invalid evidence session')
        data = canonical(record)
        envelope = dict(record=record, sha256=hashlib.sha256(data).hexdigest())
        encoded = canonical(envelope) + b'\n'
        if len(encoded) > self.max_record_bytes:
            return 'record_limit'
        self.directory.mkdir(exist_ok=True)
        path = self.directory / (session + '.private.jsonl')
        with path.open('a+b') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b'\n':
                    return 'incomplete_tail'
            if size + len(encoded) > self.max_bytes:
                return 'session_limit'
            stream.seek(0, os.SEEK_END)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return 'recorded'


def decode_record(line):
    """Offline integrity check; no acceptance/admission of the stored quote."""
    if len(line) > MAX_RECORD_BYTES or not line.endswith(b'\n'):
        raise ValueError('Incomplete or oversized private evidence')
    envelope = json.loads(line)
    if (set(envelope) != {'record', 'sha256'} or
            hashlib.sha256(canonical(envelope['record'])).hexdigest() != envelope['sha256']):
        raise ValueError('Private evidence checksum mismatch')
    record = envelope['record']
    if record.get('schema') != 1 or record.get('kind') != 'quote_rejection':
        raise ValueError('Unknown private evidence schema')
    return record
