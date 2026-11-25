"""Broker-neutral validation of quotes and daily history (canonical payload shape)."""
from datetime import date, datetime
from dataclasses import dataclass
from argbot.execution import instant, integer, money


@dataclass(frozen=True)
class QuoteTiming:
    """Observed times, without assigning an undocumented event to fechaHora."""
    source_at: datetime
    received_at: datetime
    validated_at: datetime

    def validate_source(self, max_age):
        if (self.validated_at < self.received_at or
                (self.validated_at != self.received_at and
                 (self.source_at-self.received_at).total_seconds() > 1)):
            raise ValueError('invalid_quote_validation_time')
        age = (self.validated_at-self.source_at).total_seconds()
        if not 0 <= age <= max_age:
            raise ValueError('stale_or_future_quote')

    def evidence(self):
        return dict(source_at=self.source_at.isoformat(),
                    received_at=self.received_at.isoformat(),
                    validated_at=self.validated_at.isoformat(),
                    source_age_at_receipt_seconds=(self.received_at-self.source_at).total_seconds(),
                    source_age_at_validation_seconds=(self.validated_at-self.source_at).total_seconds(),
                    receive_age_at_validation_seconds=(self.validated_at-self.received_at).total_seconds(),
                    source_event='unspecified', trade_at=None, book_at=None,
                    book_age_seconds=None, book_freshness='unknown')


def quote_timing(payload, received_at, validated_at=None):
    return QuoteTiming(instant(payload['fechaHora']), received_at, validated_at or received_at)


def _identity(payload, symbol):
    if payload.get('simbolo', symbol) != symbol:
        raise ValueError('wrong_symbol')
    if (payload['moneda'] != 'peso_Argentino' or payload['plazo'] not in ('t1', 'T1')
            or integer(payload['lote'], True) != 1 or integer(payload['laminaMinima'], True) != 1):
        raise ValueError('unsupported_currency_settlement_units')


def _trade_values(payload):
    price, low, high, amount = (money(payload[k]) for k in ('ultimoPrecio','minimo','maximo','montoOperado'))
    if not 0 < low <= price <= high or amount <= 0:
        raise ValueError('invalid_price_range_or_amount')
    return price, low, high, amount


def _book_values(payload, max_spread):
    book = payload['puntas']
    bids = [(money(p['precioCompra']), integer(p['cantidadCompra'])) for p in book]
    asks = [(money(p['precioVenta']), integer(p['cantidadVenta'])) for p in book]
    bids = [p for p in bids if p[0] > 0 and p[1] > 0]
    asks = [p for p in asks if p[0] > 0 and p[1] > 0]
    if not bids or not asks:
        raise ValueError('empty_book')
    bid, bid_size = max(bids)
    ask, ask_size = min(asks)
    if ask < bid or (ask - bid) / bid > money(max_spread):
        raise ValueError('crossed_or_wide_book')
    return bid, bid_size, ask, ask_size


def quote(payload, symbol, received_at, *, max_age, max_spread, validated_at=None):
    timing = quote_timing(payload, received_at, validated_at)
    # A payload may be held briefly, but never used ahead of its source time.
    # Retain the actual receipt and source times; neither is refreshed/invented.
    timing.validate_source(max_age)
    _identity(payload, symbol)
    price, low, high, amount = _trade_values(payload)
    bid, bid_size, ask, ask_size = _book_values(payload, max_spread)
    return {**payload, 'ultimoPrecio':float(price), 'minimo':float(low), 'maximo':float(high),
            'montoOperado':float(amount), 'bid':str(bid), 'ask':str(ask),
            'bid_size':bid_size, 'ask_size':ask_size, 'source_at':timing.source_at.isoformat(),
            'received_at':timing.received_at.isoformat(), 'validated_at':timing.validated_at.isoformat()}


def inspect_quote(payload, symbol, received_at, *, max_age, max_spread, validated_at=None):
    """Independent diagnostics only: passing book structure never admits a quote.

    In particular an old source timestamp must not conceal a crossed/empty book.
    There is no book-time attestation or alternative admission policy in v1.
    """
    if not isinstance(payload, dict):
        return dict(timing=None, checks={name:dict(status='invalid', reason='payload_not_object')
                    for name in ('source_time', 'identity', 'trade_values', 'book_structure')},
                    book_freshness='unknown', execution_authorized=False, diagnostic_only=True)
    known = {'invalid_quote_validation_time', 'stale_or_future_quote', 'wrong_symbol',
             'unsupported_currency_settlement_units', 'invalid_price_range_or_amount',
             'empty_book', 'crossed_or_wide_book'}
    def check(operation):
        try:
            operation()
            return dict(status='valid', reason=None)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            reason = str(exc) if type(exc) is ValueError and str(exc) in known else type(exc).__name__
            return dict(status='invalid', reason=reason)
    times = None
    def temporal():
        nonlocal times
        timing = quote_timing(payload, received_at, validated_at)
        times = timing.evidence()
        timing.validate_source(max_age)
    checks = dict(source_time=check(temporal),
                  identity=check(lambda: _identity(payload, symbol)),
                  trade_values=check(lambda: _trade_values(payload)),
                  book_structure=(dict(status='not_checked', reason='diagnostic_level_limit')
                      if isinstance(payload.get('puntas'), list) and len(payload['puntas']) > 1000
                      else check(lambda: _book_values(payload, max_spread))))
    return dict(timing=times, checks=checks, book_freshness='unknown',
                execution_authorized=False, diagnostic_only=True)


def daily_rows(raw, session_date, previous_session, *, audit=None):
    """Use provider session labels; no timezone invented for historical dates.

    Select the last daily summary BEFORE validating OHLC: intraday snapshots are
    not complete daily bars. Never invent missing prices or expand a bad range.
    An invalid completed bar breaks the usable history: only the valid suffix
    after it can warm up indicators (at least 35 bars, ending on previous_session).
    This does not skip bad internal days or reuse an earlier tick as that close.
    """
    by_day = {}
    for row in sorted(raw, key=lambda r:r['fechaHora']):
        day = date.fromisoformat(row['fechaHora'][:10])
        if day >= session_date:
            continue
        # Equal final timestamps with contradictory OHLCV cannot pick a winner.
        old=by_day.get(day)
        if old and old['fechaHora']==row['fechaHora'] and any(old.get(k)!=row.get(k) for k in
                ('apertura','maximo','minimo','ultimoPrecio','volumenNominal')):
            raise ValueError('conflicting_daily_summary')
        by_day[day]=row
    result=[];invalid=[]
    for day,row in sorted(by_day.items()):
        try:
            op, hi, lo, cl = [money(row[k]) for k in ('apertura','maximo','minimo','ultimoPrecio')]
            if not 0 < lo <= min(op,cl) <= max(op,cl) <= hi:
                raise ValueError('invalid_daily_ohlc')
            volume=money(row['volumenNominal'])
        except (ValueError,KeyError,TypeError):
            result=[];invalid.append(day.isoformat());continue
        result.append(dict(fecha=day,apertura=float(op),maximo=float(hi),minimo=float(lo),
                           cierre=float(cl),volumen=float(volume)))
    if audit is not None:
        audit.update(input_days=len(by_day),invalid_final_days=invalid,usable_suffix_days=len(result),
                     policy='last_daily_snapshot_strict_valid_suffix_min35')
    if len(result) < 35 or result[-1]['fecha'] != previous_session:
        raise ValueError('insufficient_or_stale_history')
    return result

