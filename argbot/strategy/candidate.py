"""The example strategy wired to a broker: sessions, quotes, risk, entries and stops.

Explicit dependencies, no implicit live IO. The pure rules live in
`argbot.strategy.rules`; orders go through `argbot.execution.ExecutionController`,
which talks to any `Broker` (paper by default).
"""
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import logging
import time as timing
import uuid
from zoneinfo import ZoneInfo

from argbot.strategy.rules import RuleEngine, StrategyConfig
from argbot.strategy.daily import DailyIndicators
from argbot.brokers.iol.budget import PASSWORD_GRANT_LIMIT
from argbot.execution import QUOTE_EXPIRED, ExecutionBlocked, TERMINAL, instant, money
from argbot.market import quote
from argbot.strategy.vwap import update_vwap, value as vwap_value
from argbot.sessions import REVIEWED_YEARS, is_trading_day, trading_day_before, trading_days_between
from argbot.clock import ExchangeClock
from argbot.telemetry import append_cycle
from argbot.quote_evidence import QuoteEvidenceJournal, rejection_record

ART = ZoneInfo('America/Argentina/Buenos_Aires')
LOG = logging.getLogger('argbot.candidate')
REQUIRED_PILOT = ('account_id','assigned_capital','fee_reserve','max_exposure_pct','max_daily_loss_pct')
# Maximum source age for managing held positions when the configuration does not
# set `managed_quote_age_seconds`: one poll interval.
DEFAULT_MANAGED_QUOTE_AGE_SECONDS = 300
# A reconciliation that failed on the network would leave every stop waiting for the
# next round. It is tried again by the bounded 30 s recovery pass. Discrepancies, set-aside
# orders, budget or Retry-After refusals wait for the next round.
# A login that got no answer or a 5xx is retried too, for the same reason. A refused login
# (4xx, or a persistent 401) still waits: repeating it could lock the account.
RETRYABLE_RECONCILIATION = frozenset({'IOLAPIError', 'TimeoutError', 'ConnectionError',
                                      'IOLAuthNetworkError', 'IOLAuthServerError'})
# A login refused by the network still spends one of the session's logins. The retry
# keeps three for the normal rounds and a restart.
RETRY_LOGIN_HEADROOM = 3
# A broker can keep refusing one symbol (for example because of the investor profile),
# and the window's only slot must not go to it each time. A symbol whose BUY the broker refused is left out for
# the rest of the day, and the same window tries the next candidate. At most two
# refusals per window, so one round never chains sends.
BROKER_REJECTED_TODAY = 'broker_rejected_today'
MAX_BROKER_REJECTIONS_PER_WINDOW = 2
# BYMA extended the trading hours for 24 h equities until 18:00, so an order left
# open at 17:00 can still fill. The session end is a reviewed setting; a configuration without it is the old 17:00 schedule. From
# BUY_CANCEL_BEFORE_END before the end every unfilled BUY is cancelled, so the next rounds
# confirm it before the stop. Stop sales keep working until the end.
SESSION_ENDS = ('17:00', '18:00')
BUY_CANCEL_BEFORE_END = timedelta(minutes=15)
LATE_START_BEFORE_CUTOFF = timedelta(minutes=5)


def _shift(clock, delta):
    return (datetime.combine(datetime(2000, 1, 3).date(), clock) + delta).time()


def session_end(config):
    """End of the bot's session (ART) for this configuration: 17:00 without the setting."""
    return time.fromisoformat(config.get('session_end') or '17:00')


def buy_cancel_time(config):
    return _shift(session_end(config), -BUY_CANCEL_BEFORE_END)


def late_start_limit(config):
    """Last minute (exclusive) to prepare, reconcile, admit, relaunch or continue a session
    today: 5 minutes before the entry cutoff (16:35 with the 17:00 schedule)."""
    return _shift(time.fromisoformat(config['entry_cutoff']), -LATE_START_BEFORE_CUTOFF)


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()


def sector_full(config, symbol, occupied):
    """Configurable concentration bound: positions plus active orders per sector.

    Without `sector_limits` there is no sector bound, exactly as before."""
    limits = config.get('sector_limits')
    if not limits:
        return False
    sectors = limits['sectors']
    return sum(1 for s in occupied if sectors.get(s) == sectors.get(symbol)) >= limits['max_positions']


def sizing_capital(config, runtime):
    """Base for each buy and for the exposure cap.

    Size from the equity, not from the capital assigned at the start, which does
    not grow with the account or with inflation. With `sizing_base` it is the day's opening
    equity (the daily brake's base); until that exists, and without the rule, it stays the
    assigned capital. Cash still bounds every buy: no leverage."""
    if config.get('sizing_base') and runtime.get('opening_equity') is not None:
        return money(runtime['opening_equity'])
    return money(config['assigned_capital'])


def liquid_enough(config, cot, now):
    """Configurable bound for NEW entries: trades per minute since the open.

    An illiquid holding can leave its quote older than the management age, blocking
    valuation and entries. Missing or
    invalid trade counts are not liquid. Without `entry_liquidity` nothing changes."""
    rule = config.get('entry_liquidity')
    if not rule:
        return True
    start = datetime.combine(now.date(), time.fromisoformat(rule['since']), tzinfo=now.tzinfo)
    minutes = (now - start).total_seconds() / 60
    trades = cot.get('cantidadOperaciones')
    if minutes <= 0 or isinstance(trades, bool) or not isinstance(trades, (int, float)) or trades < 0:
        return False
    return trades / minutes >= rule['min_trades_per_minute']


def validate_config(config):
    missing = [key for key in REQUIRED_PILOT if config.get(key) is None]
    if missing:
        raise ValueError('Pilot configuration pending: ' + ', '.join(missing))
    if config['schema'] != 1 or config['status'] not in ('candidate_not_authorized_for_live', 'synthetic'):
        raise ValueError('Unsupported candidate configuration')
    if not isinstance(config['account_id'], str) or not config['account_id'].strip():
        raise ValueError('Account identity required')
    if money(config['assigned_capital']) <= 0 or not 0 < money(config['fee_reserve']) < 1:
        raise ValueError('Explicit capital and fee reservation required')
    for key in ('max_exposure_pct','max_daily_loss_pct','max_spread_pct','max_scale_change_pct'):
        if not 0 < money(config[key]) <= 1:
            raise ValueError('Invalid bound: '+key)
    universe = config['universe']
    if (not isinstance(universe, list) or not universe or len(set(universe)) != len(universe)
            or any(not isinstance(s,str) or not s.isalnum() or s != s.upper() for s in universe)
            or not set(config['cedears']) <= set(universe)):
        raise ValueError('Invalid universe')
    liquidity = config.get('entry_liquidity')
    if liquidity is not None:
        try:
            since = time.fromisoformat(liquidity['since'])
            minimum = liquidity['min_trades_per_minute']
            valid = (set(liquidity) == {'min_trades_per_minute','since'}
                     and not isinstance(minimum, bool) and isinstance(minimum, (int, float))
                     and 0 < minimum <= 1000 and time(10) <= since
                     and since < min(time.fromisoformat(w) for w in config['entry_windows']))
        except (TypeError, ValueError, KeyError, AttributeError):
            valid = False
        if not valid:
            raise ValueError('Unsupported entry liquidity rule')
    scan = config.get('window_scan')
    if scan is not None and (not isinstance(scan, dict) or set(scan) != {'full_universe', 'max_candidate_refreshes'}
            or type(scan['full_universe']) is not bool or type(scan['max_candidate_refreshes']) is not int
            or not 0 <= scan['max_candidate_refreshes'] <= len(config['universe'])):
        raise ValueError('Unsupported window scan')
    send = config.get('send_refresh')
    if send is not None and (not isinstance(send, dict) or set(send) != {'min_remaining_seconds'}
            or type(send['min_remaining_seconds']) is not int
            or not 0 < send['min_remaining_seconds'] < config['max_quote_age_seconds']):
        raise ValueError('Unsupported send refresh')
    sizing = config.get('sizing_base')
    if sizing is not None and sizing != {'basis': 'opening_equity'}:
        raise ValueError('Unsupported sizing base')
    stale = config.get('stale_trade_valuation')
    if stale is not None and (not isinstance(stale, dict)
            or not {'max_trade_age_seconds'} <= set(stale) <= {'max_trade_age_seconds', 'before_first_trade'}
            or stale.get('before_first_trade', 'previous_close') != 'previous_close'
            or type(stale['max_trade_age_seconds']) is not int
            or not config['poll_seconds'] < stale['max_trade_age_seconds'] <= 8*3600):
        raise ValueError('Unsupported stale trade valuation')
    limits = config.get('sector_limits')
    if limits is not None and (not isinstance(limits, dict) or set(limits) != {'max_positions','sectors'}
            or type(limits['max_positions']) is not int or limits['max_positions'] <= 0
            or not isinstance(limits['sectors'], dict) or set(limits['sectors']) != set(universe)
            or any(not isinstance(v, str) or not v.strip() for v in limits['sectors'].values())):
        raise ValueError('Unsupported sector limits')
    for key in ('poll_seconds','max_quote_age_seconds','http_session_budget','http_management_reserve'):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError('Invalid positive integer: '+key)
    if config['http_management_reserve'] >= config['http_session_budget']:
        raise ValueError('Management reserve exceeds HTTP budget')
    managed_age = config.get('managed_quote_age_seconds')
    if managed_age is not None and (type(managed_age) is not int
            or not config['max_quote_age_seconds'] <= managed_age <= config['poll_seconds']):
        raise ValueError('Unsupported managed quote age')
    confirmation_gap = config.get('stop_confirmation_max_gap_seconds')
    if confirmation_gap is not None and (type(confirmation_gap) is not int
            or not config['poll_seconds'] <= confirmation_gap <=
                2*config['poll_seconds']+config['max_quote_age_seconds']):
        raise ValueError('Unsupported stop confirmation gap')
    recovery = config.get('managed_recovery')
    if recovery is not None and (not isinstance(recovery, dict)
            or set(recovery) != {'delay_seconds','max_attempts_per_cycle'}
            or recovery.get('delay_seconds') != 30 or recovery.get('max_attempts_per_cycle') not in (1,2)
            or any(type(v) is not int for v in recovery.values())):
        raise ValueError('Unsupported bounded recovery policy')
    for key, value in (('quote_timeout_seconds',5),('opportunity_scan_seconds',20)):
        if key in config and (type(config[key]) is not int or config[key]!=value):
            raise ValueError('Unsupported bounded IO policy: '+key)
    end = config.get('session_end')
    if end is not None and end not in SESSION_ENDS:
        raise ValueError('Unsupported session end')
    windows = [time.fromisoformat(s) for s in config['entry_windows']]
    # Sends end before the closing BUY cancellation starts (16:40 < 16:45, 17:40 < 17:45).
    if not windows or windows != sorted(set(windows)) or not all(
            time(10,30) <= w < time.fromisoformat(config['entry_cutoff']) < buy_cancel_time(config) for w in windows):
        raise ValueError('Invalid entry windows')
    rules = config['strategy']
    allowed = {'max_capital_pct','max_concurrent','sizing_overshoot_max','filtro_candidatos',
               'caida_lookback','caida_umbral','mec_rango_dia_max','mec_vwap_max',
               'regimen_umbral','regimen_factor','trail_stop_pct','trail_atr_mult','stop_atr_mult',
               'trail_giveback_max','lock_ganador_umbral','lock_ganador_frac','stop_confirm_polls',
               'stop_fase1_ruedas','compra_pasiva','buy_timeout_min','sell_timeout_min',
               'max_trades_dia','cooldown_loss_min','vwap_filtro_pct'}
    if set(rules) != allowed or rules['filtro_candidatos'] != 'caida' or rules['sizing_overshoot_max'] != 1:
        raise ValueError('Unsupported strategy configuration')
    for key in ('max_concurrent','caida_lookback','stop_confirm_polls','buy_timeout_min','sell_timeout_min','max_trades_dia','cooldown_loss_min'):
        if type(rules[key]) is not int or rules[key] <= 0:
            raise ValueError('Invalid strategy integer: '+key)
    if type(rules['stop_fase1_ruedas']) is not int or rules['stop_fase1_ruedas'] < 0:
        raise ValueError('Invalid stop phase')
    if type(rules['compra_pasiva']) is not bool or not -1 < rules['caida_umbral'] < 0:
        raise ValueError('Invalid entry rule')
    for key in allowed - {'filtro_candidatos','caida_umbral','compra_pasiva'}:
        money(rules[key])
    for key in ('max_capital_pct','mec_rango_dia_max','regimen_umbral','regimen_factor','trail_stop_pct','trail_giveback_max','lock_ganador_frac'):
        if not 0 < money(rules[key]) <= 1:
            raise ValueError('Invalid strategy fraction: '+key)


class MechanicalRules(RuleEngine):
    """The pure rules bound to this candidate's configuration and daily history."""

    def _en_fase1_stop(self, pos, now):
        # Sessions are counted on the operational calendar, which also skips
        # later-announced closures.
        n = self.config.stop_fase1_ruedas
        if n <= 0 or not pos.get('ts_entrada'):
            return False
        return trading_days_between(datetime.fromisoformat(pos['ts_entrada']).date(), now.date()) < n

    def __init__(self, config, daily):
        # The regime breadth is measured on local stocks only, never on CEDEARs.
        self.config = StrategyConfig(universe=config['universe'], **config['strategy'],
                                     regimen_universe=[s for s in config['universe'] if s not in config['cedears']],
                                     entry_windows=[time.fromisoformat(s) for s in config['entry_windows']],
                                     hora_no_entrada=time.fromisoformat(config['entry_cutoff']))
        self._daily = daily
        self._daily_sospechoso = set()
        self._st = {}

    def _score_estructural(self, symbol, now, modo=None):
        # A symbol without daily history is never a candidate.
        if not self._daily.asof(symbol, now.date()):
            return None
        return super()._score_estructural(symbol, now, modo)

    def _vwap(self, symbol):
        return vwap_value(self._st['vwap'].get(symbol, {}))


class Candidate:
    def __init__(self, controller, market, config, *, clock, monotonic=None, quote_sleep=None, exchange=None):
        validate_config(config)
        from argbot.lock import account_key
        if controller.lease.key != account_key(config['account_id']):
            raise ValueError('Candidate/account mismatch')
        if money(controller.state['fee_reserve']) != money(config['fee_reserve']):
            raise ValueError('Fee reservation differs from durable state')
        self.execution, self.market, self.config, self.clock = controller, market, deepcopy(config), clock
        self.monotonic = monotonic or timing.monotonic
        self.quote_sleep = quote_sleep or timing.sleep
        # Quote ages are judged on the exchange's time (PC clock plus the measured NTP
        # offset); everything else, read by other processes, stays on the PC clock.
        self.exchange = exchange or ExchangeClock(clock, self.monotonic)
        self.runtime = controller.state.get('strategy') or {
            'config_hash':config_hash(config), 'day':None, 'windows':[], 'vwap':{},
            'quotes':{}, 'cooldowns':{}, 'closed_orders':[], 'closed_today':0,
            'risk_paused':False, 'incidents':[], 'exclusions':{}, 'last_cycle':None,
            'equity':str(money(controller.state['cash']) + sum(money(p['cost']) for p in controller.state['positions'].values()) + sum(money(o['hold']) for o in controller.state['orders'].values()))}
        provenance = controller.state.get('provenance') or {}
        if provenance.get('config_hash') and provenance['config_hash'] != config_hash(config):
            raise ValueError('Migration configuration changed; prepare a reviewed migration')
        if self.runtime['config_hash'] != config_hash(config):
            raise ValueError('Configuration changed; explicit migration required')
        self.session = None
        self.rules = None
        self.rows = {}
        self._cycle_audit = None
        # Valuation-only marks of this pass; never quotes, stops, VWAP or entries.
        self._book_marks = {}
        self._risk_marks = {}
        self._round_at = None  # start of the current round or recovery pass
        self._quote_evidence = QuoteEvidenceJournal(self.execution.path.parent)
        self._save()

    def _audit(self, kind, **values):
        if self._cycle_audit is not None:
            self._cycle_audit['observations'].append(dict(kind=kind, at=self.clock().isoformat(), **values))

    def _write_cycle_audit(self, record):
        try:
            append_cycle(self.execution.path.parent, record)
            self.runtime['telemetry_ok'] = True
        except (OSError, ValueError, TypeError):
            # Keep stops/reconciliation available even if the auxiliary audit
            # disk is unavailable. New entries require a working audit trail.
            self.runtime['telemetry_ok'] = False
            self._incident('cycle_telemetry_unavailable')

    def _quote_age(self, cot):
        return (self.exchange.now()-instant(cot['source_at'])).total_seconds() if cot else None

    def _fresh(self, cot):
        """Entry freshness: new risk (candidates, BUY) needs a recent source."""
        age = self._quote_age(cot)
        return age is not None and 0 <= age <= self.config['max_quote_age_seconds']

    @property
    def _managed_age(self):
        # IOL's fechaHora lags 60-110 s even on liquid symbols while carrying new
        # trades and a changed book. Managing held positions (stops, valuation,
        # stop exits) tolerates up to one poll interval; entries keep the strict
        # limit.
        return max(self.config['max_quote_age_seconds'],
                   self.config.get('managed_quote_age_seconds', DEFAULT_MANAGED_QUOTE_AGE_SECONDS))

    def _managed_fresh(self, cot):
        """Management freshness: stops, valuation and stop exits of held positions."""
        age = self._quote_age(cot)
        return age is not None and 0 <= age <= self._managed_age

    def _save(self, metadata=None):
        # Every durable checkpoint is progress, so a long window round is never mistaken
        # for a stalled process by an external monitor reading `heartbeat_at` (none is included).
        self.runtime['heartbeat_at'] = self.clock().isoformat()
        self.execution.strategy_checkpoint(self.runtime, metadata or {})

    def _incident(self, code, symbol=None):
        event = {'at':self.clock().isoformat(), 'code':code, 'symbol':symbol}
        self.runtime['incidents'] = (self.runtime['incidents'] + [event])[-100:]
        self.runtime['status'] = 'blocked'
        LOG.error('Candidate incident %s %s', code, symbol or '')
        self._save()

    def start_session(self, day, previous_session, *, clock_evidence):
        """Explicit session admission; caller supplies the one initial clock check.

        No NTP request here. Production launcher must obtain and retain this evidence,
        plus the reviewed calendar, before calling this. The admission checks use the PC
        clock; the exchange clock starts from this evidence (or, on a continuation, from
        the session's last applied offset) and measures again only at round starts.
        """
        checked = instant(clock_evidence['checked_at']).astimezone(ART)
        admission_checked=checked
        if clock_evidence.get('continuation_checked_at'):
            prior=self.runtime.get('clock_evidence') or {}
            expected={k:v for k,v in clock_evidence.items() if k!='continuation_checked_at'}
            prior_initial={k:v for k,v in prior.items() if k!='continuation_checked_at'}
            if (prior_initial!=expected or self.runtime.get('day')!=day.isoformat()
                    or (self.execution.state.get('http_budget') or {}).get('day')!=day.isoformat()):
                raise ValueError('Clock continuation does not match durable session')
            admission_checked=instant(clock_evidence['continuation_checked_at']).astimezone(ART)
            if prior.get('continuation_checked_at') and admission_checked < instant(prior['continuation_checked_at']):
                raise ValueError('Clock continuation moved backward')
        expected_previous = trading_day_before(day)
        if day.year not in REVIEWED_YEARS or not is_trading_day(day) or previous_session != expected_previous:
            raise ValueError('Session calendar mismatch; review required')
        if (checked.date() != day or clock_evidence['session'] != day.isoformat()
                or abs(Decimal(str(clock_evidence['offset_seconds']))) > 10
                or checked > self.clock().astimezone(ART)
                or admission_checked>self.clock().astimezone(ART)
                or (self.clock()-admission_checked).total_seconds() > 60
                or clock_evidence.get('samples') != 3 or previous_session >= day):
            raise ValueError('Invalid initial session clock evidence')
        if self.runtime['day'] and day.isoformat() < self.runtime['day']:
            raise ValueError('Cannot move the operational state backward')
        new_day = self.runtime['day'] != day.isoformat()
        if new_day:
            self.runtime.update(day=day.isoformat(), windows=[], vwap={}, quotes={},
                                risk_paused=False, closed_today=0, broker_rejected_today={},
                                managed_data_gap_since=None, managed_book_valued=[],
                                last_cycle_diagnostics=None,
                                opening_equity=None, opening_equity_basis='previous_close_pending')
            self.runtime['opening_ledger'] = dict(cash=self.execution.state['cash'],
                holds=str(sum((money(o['hold']) for o in self.execution.state['orders'].values()),Decimal(0))),
                positions={s:dict(quantity=p['quantity'],cost=p['cost']) for s,p in self.execution.state['positions'].items()},
                realized=self.execution.state['realized'])
        self.runtime['clock_evidence'] = deepcopy(clock_evidence)
        # Separate key: clock_evidence must stay the morning's for continuations.
        exchange = self.exchange.start(clock_evidence, self.runtime.get('exchange_clock'))
        if exchange is not None:
            self.runtime['exchange_clock'] = exchange
        symbols = list(dict.fromkeys([*self.execution.state['positions'], *self.config['universe']]))
        rows, excluded = self.market.history(symbols, day, previous_session)
        self.rows = rows
        self.rules = MechanicalRules(self.config, DailyIndicators(rows))
        self.runtime['history_exclusions'] = excluded
        self.runtime['history_diagnostics'] = deepcopy(getattr(self.market,'history_diagnostics',{}))
        if new_day:
            # Compare this day's marks with the previous close, NOT historical
            # purchase costs. Overnight gaps count; same-day restarts cannot
            # reset the baseline or a latched loss pause.
            state = self.execution.state
            if all(s in rows for s in state['positions']):
                baseline = money(state['cash']) + sum(money(o['hold']) for o in state['orders'].values())
                baseline += sum(p['quantity'] * money(rows[s][-1]['cierre']) *
                                (1-money(self.config['fee_reserve']))
                                for s,p in state['positions'].items())
                self.runtime.update(opening_equity=str(baseline),
                                    opening_equity_basis='previous_close_net_exit_reserve',
                                    opening_equity_marks={s:str(rows[s][-1]['cierre']) for s in state['positions']},
                                    opening_equity_at=previous_session.isoformat())
        self.session = day
        # A successful admission starts a new process session. A stop timestamp
        # from the prior process must not make observers report a stopped worker.
        # Do this only after admission/history succeed; keep the financial book,
        # daily baseline and latched pauses untouched on same-day continuation.
        self.runtime['stopped_at'] = None
        self._save()

    def _metadata(self):
        result = {}
        for symbol, pos in self.execution.state['positions'].items():
            meta = deepcopy(pos.get('swing') or {})
            buys = [o for o in self.execution.state['orders'].values()
                    if o['symbol'] == symbol and o['side'] == 'BUY' and o['filled'] > 0]
            if not meta.get('precio_entrada'):
                if not buys:
                    raise ExecutionBlocked('Position lacks reviewed entry metadata: '+symbol)
                last = buys[-1]
                meta.update(precio_entrada=float(money(last['gross']) / last['filled']),
                            ts_entrada=last['created_at'], source_order=last['local_id'])
            elif meta.get('source_order'):
                entry = self.execution.state['orders'][meta['source_order']]
                meta['precio_entrada'] = float(money(entry['gross']) / entry['filled'])
            meta.update(cantidad=pos['quantity'], en_posicion=True,
                        capital_comprometido=float(money(pos['cost'])))
            result[symbol] = meta
        return result

    def _fetch(self, symbol, *, refresh=False):
        # Every time here is on the exchange clock, the one fechaHora is compared with;
        # its offset changes only at a round start, never during this read.
        started = self.exchange.now()
        raw = None
        received_at = None
        validated_at = None
        previous = self.runtime['quotes'].get(symbol)
        observed = dict(symbol=symbol, refresh=refresh, started_at=started.isoformat(),
                        source_at=None, age_at_receipt_seconds=None, accepted=False, reason=None)
        if self.exchange.enabled:
            observed['clock_offset_seconds'] = self.exchange.offset_seconds
        self._book_marks.pop(symbol, None)  # only the latest read of a symbol counts
        try:
            raw = self.market.quote(symbol)
            now = self.exchange.now()
            received_at = now
            validated_at = now
            observed.update(received_at=now.isoformat(),
                            request_to_receipt_seconds=(now-started).total_seconds())
            # Record timestamps even when the source is rejected for age. Do
            # not copy payloads or exception bodies into the audit.
            source = instant(raw['fechaHora'])
            observed.update(source_at=source.isoformat(), age_at_receipt_seconds=(now-source).total_seconds())
            validated_at = now
            ahead = (source-now).total_seconds()
            if 0 < ahead <= 1.0:
                # One bounded local wait, no extra GET/NTP, no timestamp rewrite.
                # If the clock still lags or jumps, validation rejects the data.
                waiting = self.monotonic()
                self.quote_sleep(ahead)
                validated_at = self.exchange.now()
                observed['causal_wait_seconds'] = self.monotonic()-waiting
                if abs((validated_at-now).total_seconds()-observed['causal_wait_seconds'])>.25:
                    raise ValueError('clock_changed_during_quote_wait')
            observed['validated_at'] = validated_at.isoformat()
            # Admit up to the management limit. Each use re-checks its own age:
            # entries and BUY orders still require `max_quote_age_seconds`.
            cot = quote(raw, symbol, now, max_age=self._managed_age,
                        max_spread=self.config['max_spread_pct'], validated_at=validated_at)
            if symbol in self.rows and abs(cot['ultimoPrecio']/self.rows[symbol][-1]['cierre']-1) > float(self.config['max_scale_change_pct']):
                raise ValueError('history_quote_scale_discontinuity')
            prior = self.runtime['quotes'].get(symbol)
            if prior and instant(cot['source_at']) <= instant(prior['source_at']):
                # IOL can update the book without changing fechaHora. Only the
                # book may differ at equal source time: trade/counter/identity
                # conflicts still reject. Source freshness remains unchanged.
                book_fields = {'puntas','bid','ask','bid_size','ask_size','received_at','validated_at'}
                if (instant(cot['source_at'])==instant(prior['source_at'])
                        and {k:v for k,v in cot.items() if k not in book_fields}==
                            {k:v for k,v in prior.items() if k not in book_fields}):
                    self.runtime['quotes'][symbol] = cot
                    self.runtime['exclusions'].pop(symbol, None)
                    observed['accepted'] = True
                    observed['book_only_update'] = cot['puntas'] != prior['puntas']
                    self._record_market_mark(symbol, cot)
                    return cot
                raise ValueError('duplicate_or_regressing_quote')
            vwap = self.runtime['vwap'].setdefault(symbol, {'cum_shares':0.0, 'last_monto':None})
            reset = update_vwap(vwap, cot['ultimoPrecio'], cot['montoOperado'])
            self.runtime['quotes'][symbol] = cot
            if reset:
                raise ValueError('amount_counter_reset')
            self.runtime['exclusions'].pop(symbol, None)
            observed['accepted'] = True
            self._record_market_mark(symbol, cot)
            return cot
        except Exception as exc:
            allowed = {'stale_or_future_quote','wrong_symbol','unsupported_currency_settlement_units',
                       'invalid_price_range_or_amount','empty_book','crossed_or_wide_book',
                       'history_quote_scale_discontinuity','duplicate_or_regressing_quote','amount_counter_reset',
                       'invalid_quote_validation_time','clock_changed_during_quote_wait'}
            reason = str(exc) if type(exc) is ValueError and str(exc) in allowed else type(exc).__name__
            self.runtime['exclusions'][symbol] = reason
            observed['reason'] = reason
            if reason == 'stale_or_future_quote':
                observed['book_mark'] = self._book_mark(raw, symbol, received_at, validated_at)
            return None
        finally:
            if received_at is not None and not observed['accepted']:
                # Use the response already fetched. Never retry IO or use this
                # private diagnostic to alter quotes, VWAP, risk or orders.
                try:
                    record = rejection_record(raw, symbol, received_at,
                        validated_at=validated_at, reason=observed['reason'],
                        refresh=refresh, cycle_at=(self._cycle_audit or {}).get('started_at'),
                        session=self.runtime['day'], previous=previous,
                        max_age=self._managed_age, max_spread=self.config['max_spread_pct'])
                    observed['rejection_evidence'] = self._quote_evidence.append(record)
                except Exception:
                    # Failure is visible in the existing audit; no exception body
                    # (which could contain private input) goes to logs or models.
                    observed['rejection_evidence'] = 'unavailable'
            # Only the just-completed fetch controls deferral; never a cached
            # quote or an old exclusion from a previous round.
            self._last_fetch_observation = observed
            self._audit('quote_received', **observed,
                        elapsed_seconds=(self.exchange.now()-started).total_seconds())

    def _book_mark(self, raw, symbol, received_at, validated_at):
        """Value a holding whose last trade is old with the live book.

        The broker updates the book without a new trade, so an illiquid holding can
        still have a valid bid. Same validation as a quote except the trade age, which must
        be from this session. Feeds valuation and exposure only: never stops, VWAP,
        runtime quotes or entries. Without `stale_trade_valuation` nothing changes."""
        rule = self.config.get('stale_trade_valuation')
        if not rule or raw is None or received_at is None or symbol not in self.execution.state['positions']:
            return False
        if rule.get('before_first_trade') == 'previous_close':
            # Before its first trade of the day a holding is worth its previous close, the
            # last real price and its own mark in the daily brake baseline. Otherwise a
            # late-trading holding would block every entry each morning.
            try:
                traded = instant(raw['fechaHora']).astimezone(ART).date()
            except (KeyError, TypeError, ValueError):
                return False
            if traded < self.session and symbol in self.rows:
                close = self.rows[symbol][-1]['cierre']
                self._book_marks[symbol] = dict(bid=str(close), ask=str(close), last=close, basis='previous_close',
                    source_at=instant(raw['fechaHora']).isoformat(), received_at=received_at.isoformat())
                return True
        try:
            cot = quote(raw, symbol, received_at, max_age=rule['max_trade_age_seconds'],
                        max_spread=self.config['max_spread_pct'], validated_at=validated_at)
            if instant(cot['source_at']).astimezone(ART).date() != self.session:
                return False
            if symbol in self.rows and abs(cot['ultimoPrecio']/self.rows[symbol][-1]['cierre']-1) > float(self.config['max_scale_change_pct']):
                return False
        except Exception:
            return False
        self._book_marks[symbol] = dict(bid=cot['bid'], ask=cot['ask'], last=cot['ultimoPrecio'],
                                        source_at=cot['source_at'], received_at=cot['received_at'])
        return True

    def _audit_stop_on_book(self, symbols):
        """Record only: a book-valued holding whose live bid is under its stop.

        The stop still waits for a new trade and nothing is sold here. The level is the
        one `_stops` would compute from the last trade, on a copy of the metadata. This
        measures, over many sessions, whether a bid-based stop would ever have acted earlier."""
        metadata = self._metadata()
        now = self.clock().astimezone(ART)
        for symbol in symbols:
            mark, meta = self._book_marks.get(symbol), metadata.get(symbol)
            if not mark or not meta:
                continue
            try:
                meta = deepcopy(meta)
                peak = max(meta.get('max_precio_desde_entrada') or meta['precio_entrada'], mark['last'])
                stop = self.rules._trailing_stop_nivel(symbol, meta, peak, now)
            except Exception as exc:
                self._audit('stop_on_book_unavailable', symbol=symbol, code=type(exc).__name__)
                continue
            if money(mark['bid']) < money(stop):
                self._audit('stop_below_on_book', symbol=symbol, bid=mark['bid'], stop=str(stop),
                            last=str(mark['last']), trade_at=mark['source_at'], received_at=mark['received_at'])

    def _position_mark(self, symbol, quotes):
        """Fresh quote of a holding, else this pass's book mark, else None."""
        cot = quotes.get(symbol)
        if self._managed_fresh(cot):
            return cot
        return self._book_marks.get(symbol)

    def _record_market_mark(self, symbol, cot):
        # Private evidence in the existing audit.
        self._audit('market_mark', symbol=symbol, source_at=cot['source_at'],
            received_at=cot['received_at'], validated_at=cot['validated_at'],
            bid=cot['bid'], ask=cot['ask'], last=str(cot['ultimoPrecio']))

    def _refresh_managed_quotes(self, quotes, refreshed):
        """At most ONE extra GET per held symbol in a cycle, within its budget.

        Stops have already been evaluated once. This pass refreshes portfolio
        valuation before entry decisions; it never repeats stop confirmations.
        """
        positions=self.execution.state['positions']
        if hasattr(self.market, 'set_priority'):
            self.market.set_priority(list(dict.fromkeys([*positions,*self._active()])))
        for symbol in positions:
            cot=quotes.get(symbol)
            age=self._quote_age(cot)
            if cot and 0 <= age < max(0,self.config['max_quote_age_seconds']-5):
                continue
            if symbol in refreshed:
                continue
            refreshed.add(symbol)
            result=self._fetch(symbol,refresh=True)
            self.runtime.setdefault('managed_quote_refreshes',{})[symbol]={
                'at':self.clock().isoformat(), 'prior_age_seconds':age,
                'accepted':result is not None,
                'reason':None if result is not None else self.runtime['exclusions'].get(symbol)}
            if result is not None:
                quotes[symbol]=result
            elif not self._managed_fresh(cot):
                # A failed refresh never discards a quote still valid for management.
                quotes.pop(symbol,None)

    def _send(self, symbol, side, qty, price, cot, reason):
        now = self.clock()
        local_now = now.astimezone(ART)
        if local_now.date() != self.session or not time(10,30) <= local_now.time() < session_end(self.config):
            raise ExecutionBlocked('Order outside admitted session')
        # BUY adds risk: strict entry age. SELL exits a held position: management age.
        limit = self.config['max_quote_age_seconds'] if side == 'BUY' else self._managed_age
        fresh = self._fresh(cot) if side == 'BUY' else self._managed_fresh(cot)
        self._audit('send_check', symbol=symbol, side=side, age_seconds=self._quote_age(cot), fresh=fresh,
                    max_age_seconds=limit)
        if not fresh:
            self.runtime['exclusions'][symbol] = 'quote_expired_before_send'
            return
        local = self.execution.submit(symbol, side, qty, price, now=now, reason=reason,
                 quote_expires_at=(instant(cot['source_at'])+timedelta(seconds=limit)).isoformat())
        order = self.execution.state['orders'][local]
        if order.get('not_sent'):
            # Refused before the network: released, nothing to reconcile.
            self._audit('order_not_sent', symbol=symbol, side=side, local_id=local, code=order['not_sent'])
            if order['not_sent'] != QUOTE_EXPIRED:
                # The send guard, the budget or another executor can keep refusing, and a
                # stop exit must not keep failing quietly: the cycle records an incident.
                raise ExecutionBlocked('order_not_sent')
            if side == 'BUY':
                self.runtime['exclusions'][symbol] = 'order_not_sent'
            return
        self._audit('order_submitted', symbol=symbol, side=side, local_id=local)
        if order['state'] in ('unknown', 'submitting'):
            raise ExecutionBlocked('unresolved_order')
        if order['state'] == 'rejected':
            # The broker refused it explicitly (for example because of the investor
            # profile). A refusal before the network
            # returned above with `not_sent` and keeps its per-round behaviour.
            self._audit('order_rejected', symbol=symbol, side=side, local_id=local,
                        message=order.get('broker_message'))
            if side == 'BUY':
                # Not again today: the window's slot goes to the next candidate.
                self.runtime.setdefault('broker_rejected_today', {})[symbol] = now.isoformat()
                self.runtime['exclusions'][symbol] = BROKER_REJECTED_TODAY
            return 'rejected'

    def _active(self):
        return {o['symbol']:o for o in self.execution.state['orders'].values() if o['state'] not in TERMINAL}

    def _timeouts(self, skip=()):
        now = self.clock()
        for order in list(self._active().values()):
            if order['symbol'] in skip:
                continue
            age = (now-instant(order['created_at'])).total_seconds()/60
            timeout = self.config['strategy']['buy_timeout_min' if order['side']=='BUY' else 'sell_timeout_min']
            halt_buy = order['side']=='BUY' and (self.execution.state['paused'] or self.runtime['risk_paused'])
            closing_buy = order['side']=='BUY' and now.astimezone(ART).time() >= buy_cancel_time(self.config)
            if closing_buy and order['state']=='open' and age < timeout:
                self._audit('buy_cancelled_before_close', symbol=order['symbol'], local_id=order['local_id'])
            if age >= timeout or halt_buy or closing_buy or instant(order['created_at']).astimezone(ART).date() < self.session:
                self.execution.cancel(order['local_id'])
        if any(o['state'] in ('cancel_pending','unknown','submitting') and o['symbol'] not in skip
               for o in self._active().values()):
            raise ExecutionBlocked('unresolved_order')

    def _cancel_due_orders(self):
        """A cancellation the broker does not confirm at once must not skip the other
        holdings' stops.

        Without this, `_timeouts` would refuse the round before any stop check and only
        the next round, through `exits_only`, would check them. A second reconciliation sets the pending order aside in
        this same round: every other holding gets its stop (sales only, no scan, no entries)
        and the round stays blocked and visible. An order IOL may not have received
        (`submitting`, `unknown`) still blocks the whole round.

        Returns None when the round may go on; otherwise the exits-only quotes, and the
        caller records the round as blocked.
        """
        try:
            self._timeouts()
            return None
        except ExecutionBlocked as exc:
            if str(exc) != 'unresolved_order' or any(
                    o['state'] in ('submitting', 'unknown') for o in self._active().values()):
                raise
        if self.execution.reconcile():
            self._timeouts()  # the broker confirmed it in the meantime: the round goes on
            return None
        if not self.execution.exits_only:
            raise ExecutionBlocked(self.execution.failure)
        return self._manage_exits_only()

    def _manage_exits_only(self):
        """An order with problems must not stop the other holdings' stops.

        The ledger set aside the orders it could not confirm (`exits_only`); the rest of
        the account agreed. Stops and the daily brake run for every other holding, with
        their sales the only orders `submit` accepts. No scan and no entries; the caller
        still records the failure, so the round stays blocked and visible.
        """
        held = set(self.execution.quarantined)
        self._audit('exits_only', symbols=sorted(held))
        priority = list(self.execution.state['positions'])
        if hasattr(self.market, 'set_priority'):
            self.market.set_priority(priority)
        quotes = {}
        for symbol in priority:
            cot = self._fetch(symbol)
            if cot is None:
                cot = self._fetch(symbol, refresh=True)
            if cot is not None:
                quotes[symbol] = cot
            if symbol not in held:  # its quantity can still change; it is only valued
                self._stops(quotes, symbols={symbol})
        self._risk(quotes)
        self._timeouts(skip=held)
        self.runtime['entries_blocked'] = self.runtime.get('entries_blocked') or 'unresolved_order'
        # Durable evidence that this round completed its exits-only management: the
        # first-round check of a restart accepts it in place of a clean reconciliation.
        self.runtime['exits_only'] = dict(at=self.clock().isoformat(), symbols=sorted(held))
        return quotes

    def _stops(self, quotes, symbols=None):
        metadata = self._metadata()
        if symbols is not None:
            metadata = {s:m for s,m in metadata.items() if s in symbols}
        for symbol, meta in metadata.items():
            checked = self.runtime.setdefault('stop_checked_cycle', {})
            if (self.config.get('managed_recovery') and self.runtime.get('last_cycle')
                    and checked.get(symbol) == self.runtime['last_cycle']):
                continue
            cot = quotes.get(symbol)
            gap = self.config.get('stop_confirmation_max_gap_seconds')
            # Compared with the quote's source time below: same exchange clock.
            checked_at = self.exchange.now()
            if gap is not None and meta.get('stop_toques', 0):
                # An unknown observation does not prove a price recovery. Keep
                # a confirmed touch only while its original bounded clock,
                # session and position identity remain valid. Never invent a
                # timestamp for confirmations inherited from an older runtime.
                try:
                    prior_at = instant(meta['stop_confirmation_at'])
                    prior_source = instant(meta['stop_confirmation_source_at'])
                    valid = (0 <= (checked_at-prior_at).total_seconds() <= gap
                        and prior_at.astimezone(ART).date() == self.session
                        and prior_source <= prior_at
                        and meta.get('stop_confirmation_quantity') == meta['cantidad']
                        and meta.get('stop_confirmation_entry') == meta.get('ts_entrada'))
                except (KeyError, ValueError, TypeError, OverflowError):
                    valid = False
                if not valid:
                    meta['stop_toques'] = 0
                    self._audit('stop_confirmation_expired', symbol=symbol)
            # The management age needs the confirmation memory: it rejects a repeated
            # source as a second touch. Without it, keep the strict entry age.
            fresh = self._managed_fresh(cot) if gap is not None else self._fresh(cot)
            self._audit('stop_check', symbol=symbol, age_seconds=self._quote_age(cot), fresh=fresh)
            if not fresh:
                if gap is None:
                    meta['stop_toques'] = 0
                else:
                    self._audit('stop_confirmation_waiting', symbol=symbol,
                        retained_touches=meta.get('stop_toques', 0), max_gap_seconds=gap)
                continue
            checked[symbol] = self.runtime.get('last_cycle')
            price = cot['ultimoPrecio']
            peak = max(meta.get('max_precio_desde_entrada') or meta['precio_entrada'], price)
            meta['max_precio_desde_entrada'] = peak
            stop = self.rules._trailing_stop_nivel(symbol, meta, peak, self.clock().astimezone(ART))
            if gap is not None and price < stop and meta.get('stop_toques', 0):
                if instant(cot['source_at']) <= instant(meta['stop_confirmation_source_at']):
                    self._audit('stop_confirmation_duplicate', symbol=symbol)
                    continue
            meta['stop_toques'] = meta.get('stop_toques',0)+1 if price < stop else 0
            if gap is not None:
                if price < stop:
                    meta.update(stop_confirmation_at=checked_at.isoformat(),
                        stop_confirmation_source_at=cot['source_at'],
                        stop_confirmation_quantity=meta['cantidad'],
                        stop_confirmation_entry=meta.get('ts_entrada'))
                else:
                    for key in ('stop_confirmation_at','stop_confirmation_source_at',
                                'stop_confirmation_quantity','stop_confirmation_entry'):
                        meta.pop(key, None)
            # Ratchet and confirmation persist BEFORE the order side effect.
            self._save({s:m for s,m in metadata.items() if s in self.execution.state['positions']})
            if meta['stop_toques'] >= self.config['strategy']['stop_confirm_polls']:
                active = self._active().get(symbol)
                if active and active['side'] == 'BUY':
                    self.execution.cancel(active['local_id'])
                    # A late partial fill changes qty: next cycle reconciles first.
                    continue
                if not active:
                    self._send(symbol,'SELL',self.execution.state['positions'][symbol]['quantity'],
                               cot['bid'],cot,'mechanical_stop')
        self._save({s:m for s,m in metadata.items() if s in self.execution.state['positions']})

    def _risk(self, quotes):
        state = self.execution.state
        marks = {s:self._position_mark(s, quotes) for s in state['positions']}
        missing = [s for s,m in marks.items() if m is None]
        # Old last trade, live book: valued, but recovery passes still re-check its stop.
        book_valued = [s for s in state['positions'] if marks[s] is not None and not self._managed_fresh(quotes.get(s))]
        self._audit('risk_check', ages_seconds={s:self._quote_age(quotes.get(s)) for s in state['positions']}, unavailable=missing,
                    book_valued=book_valued, max_age_seconds=self._managed_age)
        self.runtime['valuation_available'] = not missing
        self.runtime['managed_book_valued'] = book_valued
        self._risk_marks = marks
        if missing:
            self.runtime['managed_data_gap_since'] = self.runtime.get('managed_data_gap_since') or self.clock().isoformat()
            self.runtime['entries_blocked'] = 'managed_quote_missing'
            return False
        if self.runtime.get('managed_data_gap_since'):
            self._audit('managed_data_recovered', first_failed_check_at=self.runtime['managed_data_gap_since'])
        self.runtime['managed_data_gap_since'] = None
        equity = money(state['cash']) + sum(money(o['hold']) for o in state['orders'].values())
        equity += sum(p['quantity']*money(marks[s]['bid'])*(1-money(self.config['fee_reserve']))
                      for s,p in state['positions'].items())
        self.runtime['equity'] = str(equity)
        self.runtime['valuation_at'] = self.clock().isoformat()
        self._audit('valuation', equity_net_exit_reserve=str(equity),
                    equity_bid=str(equity + sum(p['quantity']*money(marks[s]['bid'])*money(self.config['fee_reserve']) for s,p in state['positions'].items())))
        if self.runtime.get('opening_equity') is None:
            self.runtime['entries_blocked'] = 'daily_baseline_unavailable'
            return False
        baseline = money(self.runtime['opening_equity'])
        if not self.runtime['risk_paused'] and equity <= baseline*(1-money(self.config['max_daily_loss_pct'])):
            self.runtime['risk_paused'] = True
            self._incident('daily_loss_limit')
        for oid, order in state['orders'].items():
            if order['side']=='SELL' and order['state'] in TERMINAL and order['filled'] and oid not in self.runtime['closed_orders']:
                self.runtime['closed_orders'].append(oid)
                if order['symbol'] not in state['positions']:
                    self.runtime['closed_today'] += 1
                    if Decimal(order.get('realized','0')) < 0:
                        self.runtime['cooldowns'][order['symbol']] = self.clock().isoformat()
        self.runtime['entries_blocked'] = None
        return not self.runtime['risk_paused']

    def _rule_positions(self):
        positions = {s:{'en_posicion':False} for s in self.config['universe']}
        positions.update(self._metadata())
        for symbol, order in self._active().items():
            positions.setdefault(symbol, {'en_posicion':False})['buy_pendiente'] = order if order['side']=='BUY' else None
        return positions

    def _window_round(self, now):
        """A round that may decide an entry window, under the window_scan rule."""
        if not self.config.get('window_scan') or self.execution.state['paused'] or self.runtime['risk_paused']:
            return False
        if now.time() >= time.fromisoformat(self.config['entry_cutoff']):
            return False
        return any(time.fromisoformat(w) <= now.time() and w not in self.runtime['windows']
                   for w in self.config['entry_windows'])

    def _refresh_entry_candidates(self, quotes, refreshed):
        """Read again, once, the candidates whose quote is too old to enter.

        A quote read early in a long round can be older than the entry age by decision
        time, or arrive already late from the broker. The pre-selection uses the quotes in hand only to choose whom
        to read again; the decision itself still requires an entry-fresh quote."""
        limit = (self.config.get('window_scan') or {}).get('max_candidate_refreshes', 0)
        if not limit:
            return
        now = self.clock().astimezone(ART)
        held = set(self.execution.state['positions']) | set(self._active())
        pool = {s:q for s,q in quotes.items() if s in self.config['universe'] and s in self.rows and s not in held}
        self.rules._st = dict(positions=self._rule_positions(), vwap=self.runtime['vwap'],
                              capital_dia_total=float(sizing_capital(self.config, self.runtime)), stop_diario_triggered=False)
        stale = [s for s in self.rules._simbolos_a_evaluar(pool, now)
                 if s in pool and not self._fresh(quotes.get(s)) and s not in refreshed][:limit]
        accepted = []
        for symbol in stale:
            refreshed.add(symbol)
            result = self._fetch(symbol, refresh=True)
            if result is not None:
                quotes[symbol] = result
                accepted.append(symbol)
        if stale:
            self._audit('entry_candidates_refreshed', symbols=stale, accepted=accepted)

    def _entry_inputs(self, quotes, now):
        """Pure signal assessment, independent of permission to submit orders."""
        positions = self._rule_positions()
        active = self._active()
        rejected = self.runtime.get('broker_rejected_today') or {}
        eligible = {}
        for s,cot in quotes.items():
            if s not in self.config['universe'] or s not in self.rows or not self._fresh(cot):
                continue
            if s in rejected and s not in self.execution.state['positions']:
                self.runtime['exclusions'][s] = BROKER_REJECTED_TODAY
                continue
            close = self.rows[s][-1]['cierre']
            if abs(cot['ultimoPrecio']/close-1) > float(self.config['max_scale_change_pct']):
                self.runtime['exclusions'][s] = 'history_quote_scale_discontinuity'
                continue
            eligible[s] = cot
        self.rules._st = dict(positions=positions, vwap=self.runtime['vwap'],
                             capital_dia_total=float(sizing_capital(self.config, self.runtime)),
                             stop_diario_triggered=False)
        # A full sector is filtered BEFORE the rule, so the free slots go to the
        # next candidates instead of being truncated by the slot count.
        occupied = set(self.execution.state['positions']) | set(active)
        decisions = self.rules._decisiones_mecanicas(
            {s:q for s,q in eligible.items() if not sector_full(self.config, s, occupied)
             and liquid_enough(self.config, q, now)}, now)
        self.runtime['shortlist'] = self.rules._simbolos_a_evaluar(eligible,now)
        return eligible, decisions

    def _entries(self, quotes, refreshed=None):
        refreshed=refreshed if refreshed is not None else set()
        if self.runtime.get('telemetry_ok') is False:
            self.runtime['entries_blocked'] = 'cycle_telemetry_unavailable'
            return
        now = self.clock().astimezone(ART)
        if self.execution.state['paused'] or self.runtime['risk_paused'] or now.time() >= time.fromisoformat(self.config['entry_cutoff']):
            return
        # With window_scan, only windows already due when the round STARTED: a window that
        # opens mid-round would be decided on that round's partial scan; it waits for the
        # next round, which reads everything (at most one poll later).
        cut = self._round_at if self.config.get('window_scan') and self._round_at else now
        due = [w for w in self.config['entry_windows'] if time.fromisoformat(w) <= cut.time() and w not in self.runtime['windows']]
        if not due:
            return
        consumed_before = list(self.runtime['windows'])
        self.runtime['windows'] = sorted(set(self.runtime['windows']) | set(due))
        if self.runtime['closed_today'] >= self.config['strategy']['max_trades_dia']:
            self._save()
            return
        eligible, decisions = self._entry_inputs(quotes, now)
        # Durable checkpoint before any send.
        self._save()
        pending, rejections = list(decisions), 0
        while pending:
            decision = pending.pop(0)
            self._refresh_managed_quotes(quotes,refreshed)
            if not self._risk(quotes):
                # Interrupted window: keep it due for the next round instead of
                # losing its remaining decisions. Occupied slots and active orders
                # are recounted then, so nothing already sent is repeated.
                self.runtime['windows'] = consumed_before
                self._audit('entry_window_deferred', windows=due, reason=self.runtime.get('entries_blocked'))
                self._save()
                return
            s = decision.simbolo
            if s in self.runtime['cooldowns'] and (now-instant(self.runtime['cooldowns'][s])).total_seconds() < 60*self.config['strategy']['cooldown_loss_min']:
                self._audit('entry_not_submitted',symbol=s,reason='loss_cooldown')
                continue
            state = self.execution.state
            occupied = set(state['positions']) | set(self._active())
            if len(occupied) >= self.config['strategy']['max_concurrent']:
                self._audit('entry_not_submitted',symbol=s,reason='capacity_full')
                break
            if sector_full(self.config, s, occupied):
                # Two picks of one sector in the same window: the second waits.
                self._audit('entry_not_submitted',symbol=s,reason='sector_cap')
                continue
            if not (self.execution.cash or {}).get('buys_allowed', True):
                # The broker shows less cash than the book can explain. No buys while
                # it lasts; stops and sales go on. The reconciliation keeps the figures.
                self._audit('entry_not_submitted',symbol=s,reason='available_cash_shortfall')
                break
            # The marks the risk check just validated (fresh quote or this pass's book).
            exposure = sum(max(money(p['cost']),p['quantity']*money(self._risk_marks[name]['ask']))
                           for name,p in state['positions'].items())
            exposure += sum(money(o['hold']) for o in state['orders'].values())
            cap = sizing_capital(self.config, self.runtime)
            # Buys use the lower of the book's cash and the broker's (buying_cash).
            allocation = min(money(decision.monto_ars), cap*money(self.config['strategy']['max_capital_pct']),
                             self.execution.buying_cash(), max(Decimal(0),cap*money(self.config['max_exposure_pct'])-exposure))
            cot = eligible[s]
            if self.config.get('send_refresh'):
                cot = self._recheck_before_send(s, cot)
                if cot is None:
                    continue
            price = money(cot['bid' if self.config['strategy']['compra_pasiva'] else 'ask'])
            qty = int((allocation/(price*(1+money(self.config['fee_reserve'])))).to_integral_value(rounding=ROUND_FLOOR))
            if not qty:
                self._audit('entry_not_submitted',symbol=s,reason='allocation_below_one_unit')
            elif self._send(s,'BUY',qty,price,cot,decision.razon) == 'rejected':
                rejections += 1
                if rejections < MAX_BROKER_REJECTIONS_PER_WINDOW:
                    # The rule again, on this round's quotes, without the refused symbol:
                    # the freed slot goes to the next candidate in the same window.
                    eligible, pending = self._entry_inputs(quotes, now)
                    self._audit('entry_retry_after_rejection', symbol=s,
                                next=[d.simbolo for d in pending])
                else:
                    self._audit('entry_window_rejections_limit', symbol=s, rejections=rejections)
                    break

    def _recheck_before_send(self, symbol, cot):
        """Read again, once, a candidate whose quote would expire while it is sent.

        A live send can take several seconds, most of it the reconciliation before the
        request, and a quote close to its maximum age can expire on the way. The rule is decided again on the new quote, so the decision
        and the order use the same quote, as a trader looks again before clicking."""
        prior = self._quote_age(cot)
        if self.config['max_quote_age_seconds'] - prior >= self.config['send_refresh']['min_remaining_seconds']:
            return cot
        fresh = self._fetch(symbol, refresh=True)
        now = self.clock().astimezone(ART)
        selected = bool(fresh is not None and self._fresh(fresh) and liquid_enough(self.config, fresh, now)
                        and self.rules._decisiones_mecanicas({symbol: fresh}, now))
        self._audit('entry_rechecked', symbol=symbol, prior_age_seconds=prior,
                    age_seconds=self._quote_age(fresh), accepted=fresh is not None, selected=selected)
        if not selected:
            self._audit('entry_not_submitted', symbol=symbol, reason='recheck_before_send')
            return None
        return fresh

    def _recover_managed_data(self, now):
        """Bounded management passes after a gap, no opportunity scan.

        Durable claim precedes IO. Quotes keep their original source-age limits.
        Stops get at most one valid evaluation per normal 300 s cycle. A prior
        successful stop check is never counted again by this recovery pass.
        It also runs after a round whose reconciliation failed on the network
        (`reconciliation_retry_cycle`): stops, risk and valuation only, never a window.
        """
        policy = self.config.get('managed_recovery')
        parent = self.runtime.get('last_cycle')
        completed = (self.runtime.get('last_cycle_diagnostics') or {}).get('completed_at')
        # A holding valued from its book still has no fresh trade for its stop.
        quote_gap = (self.runtime.get('entries_blocked') == 'managed_quote_missing'
                     or bool(self.runtime.get('managed_book_valued')))
        retry = self.runtime.get('reconciliation_retry_cycle') == parent
        if not policy or not parent or not completed or not (quote_gap or retry):
            return
        grants = (self.execution.state.get('http_budget') or {}).get('password_grants', 0)
        if not quote_gap and grants > PASSWORD_GRANT_LIMIT - RETRY_LOGIN_HEADROOM:
            return
        claim = self.runtime.get('managed_recovery_attempt') or {}
        count = 0
        if claim.get('parent_cycle_at') == parent:
            count = claim.get('number')
            if type(count) is not int or not 1 <= count <= policy['max_attempts_per_cycle']:
                raise ExecutionBlocked('invalid_recovery_claim')
            completed = claim.get('completed_at') or claim['claimed_at']
        elif self.runtime.get('recovery_claim_cycle') == parent:
            # An older durable claim consumes the entire old-round allowance.
            # An upgrade/restart must not replay an already-managed old round.
            return
        if (count >= policy['max_attempts_per_cycle']
                or (now-instant(completed)).total_seconds() < policy['delay_seconds']):
            return
        budget = self.execution.state.get('http_budget')
        # Leave the ordinary management reserve alone when scheduling optional
        # recovery. The shared durable transport budget remains authoritative.
        expected_reads = 5 + 2*len(self._active()) + len(self.execution.state['positions'])
        if budget and budget['total']-budget['reserve']-budget['used'] < expected_reads:
            return
        # Only a pass for blocked entries may run a deferred window. A pass for a
        # book-valued holding re-checks stops and valuation; windows stay with the
        # normal cycle and its full candidate scan.
        entry_recovery = self.runtime.get('entries_blocked') == 'managed_quote_missing'
        self.runtime['recovery_claim_cycle'] = parent
        self.runtime['managed_recovery_attempt'] = dict(parent_cycle_at=parent,
            number=count+1, claimed_at=now.isoformat(), completed_at=None)
        self._book_marks = {}
        self._round_at = now
        # The status turns 'running' only once the reconciliation passes: a retry that
        # fails again must not flash 'running' and raise a second blocked notice.
        self.runtime['exits_only'] = None
        self._save()
        self._cycle_audit = dict(schema=1, cycle_id=uuid.uuid4().hex, session=self.session.isoformat(),
            started_at=now.isoformat(), cycle_kind='managed_recovery', parent_cycle_at=parent,
            recovery_attempt=count+1, observations=[])
        self._write_cycle_audit({k:v for k,v in dict(self._cycle_audit, phase='started').items() if k!='observations'})
        quotes = {}
        try:
            if not self.execution.reconcile():
                if self.execution.failure not in RETRYABLE_RECONCILIATION:
                    self.runtime['reconciliation_retry_cycle'] = None
                if self.execution.exits_only:
                    quotes = self._manage_exits_only()
                raise ExecutionBlocked(self.execution.failure)
            self.runtime['reconciliation_retry_cycle'] = None
            self.runtime['status'] = 'running'
            set_aside = self._cancel_due_orders()
            if set_aside is not None:
                quotes = set_aside
                raise ExecutionBlocked(self.execution.failure)
            priority = list(dict.fromkeys([*self.execution.state['positions'], *self._active()]))
            if hasattr(self.market, 'set_priority'):
                self.market.set_priority(priority)
            quotes = {s:q for s,q in self.runtime['quotes'].items() if s not in priority and self._fresh(q)}
            for symbol in priority:
                cot = self._fetch(symbol, refresh=True)
                if cot is not None:
                    quotes[symbol] = cot
                self._stops(quotes, symbols={symbol})
            risk_ready=self._risk(quotes)
            if not entry_recovery:
                pass
            elif risk_ready:
                # All managed symbols have used their one recovery read. The
                # entry path cannot add another read of them in this pass.
                if any(s in self.rows and s in self.config['universe'] and s not in priority
                       and self._fresh(q) for s,q in quotes.items()):
                    self._entries(quotes, set(priority))
            if self.runtime['risk_paused']:
                self._timeouts()
            if self.runtime.get('entries_blocked'):
                self.runtime['status'] = 'blocked'
        except Exception as exc:
            self._incident(str(exc) if type(exc) is ExecutionBlocked else type(exc).__name__)
        self.runtime['heartbeat_at'] = self.clock().isoformat()
        self.runtime['last_recovery_at'] = self.clock().isoformat()
        self.runtime['managed_recovery_attempt']['completed_at'] = self.clock().isoformat()
        self._write_cycle_audit(dict(self._cycle_audit, phase='completed',
            completed_at=self.runtime['managed_recovery_attempt']['completed_at'], status=self.runtime['status'],
            entries_blocked=self.runtime.get('entries_blocked'), risk_paused=self.runtime['risk_paused'],
            valuation_available=self.runtime.get('valuation_available'),
            http_budget=deepcopy(self.execution.state.get('http_budget'))))
        self._cycle_audit = None
        self._save()

    def cycle(self):
        now = self.clock().astimezone(ART)
        if self.session != now.date() or self.rules is None:
            raise ExecutionBlocked('Explicit session setup required')
        if not time(10,30) <= now.time() < session_end(self.config):
            return
        if self.runtime['last_cycle'] and (now-instant(self.runtime['last_cycle'])).total_seconds() < self.config['poll_seconds']:
            self._recover_managed_data(now)
            return
        self.runtime.update(last_cycle=now.isoformat(), status='running',managed_quote_refreshes={},
                            exits_only=None)
        self._book_marks = {}
        self._round_at = now
        self._cycle_audit = dict(schema=1, cycle_id=uuid.uuid4().hex, session=self.session.isoformat(),
                                 started_at=now.isoformat(), observations=[])
        self._write_cycle_audit({k:v for k,v in dict(self._cycle_audit, phase='started').items() if k!='observations'})
        if self.exchange.due():
            # One read-only NTP check (about 3 s) at the start of a round, never inside
            # one, so every quote of a round is judged with the same offset.
            record, snapshot = self.exchange.refresh()
            self._audit('clock_check', **record)
            if snapshot is not None:
                self.runtime['exchange_clock'] = snapshot
        quotes = {}
        try:
            if not self.execution.reconcile():
                if self.execution.exits_only:
                    quotes = self._manage_exits_only()
                elif self.execution.failure in RETRYABLE_RECONCILIATION:
                    self.runtime['reconciliation_retry_cycle'] = now.isoformat()
                raise ExecutionBlocked(self.execution.failure)
            set_aside = self._cancel_due_orders()
            if set_aside is not None:
                quotes = set_aside
                raise ExecutionBlocked(self.execution.failure)
            priority = list(dict.fromkeys([*self.execution.state['positions'], *self._active()]))
            if hasattr(self.market, 'set_priority'):
                self.market.set_priority(priority)
            quotes = {}
            refreshed = set()
            for symbol in priority:
                cot = self._fetch(symbol)
                # Retry ordinary failures once before the next holding. Under
                # the two-pass policy, defer a confirmed old source instead.
                if cot is None:
                    refreshed.add(symbol)
                    observed = getattr(self, '_last_fetch_observation', {})
                    defer_old = ((self.config.get('managed_recovery') or {}).get('max_attempts_per_cycle') == 2
                        and observed.get('symbol') == symbol
                        and observed.get('reason') == 'stale_or_future_quote'
                        and observed.get('age_at_receipt_seconds') is not None
                        and observed['age_at_receipt_seconds'] > self._managed_age)
                    if not defer_old:
                        cot = self._fetch(symbol, refresh=True)
                    self.runtime['managed_quote_refreshes'][symbol] = dict(
                        at=self.clock().isoformat(), prior_age_seconds=None,
                        accepted=cot is not None, reason=self.runtime['exclusions'].get(symbol),
                        phase='deferred_old_source' if defer_old else 'before_stop')
                if cot is not None:
                    quotes[symbol] = cot
                # A slow read for another holding must not age this holding's
                # stop check. Each position gets ONE confirmation per cycle.
                self._stops(quotes, symbols={symbol})
            # Evaluate daily risk promptly, before the broad opportunity scan.
            self._risk(quotes)
            if self.runtime['risk_paused']:
                self._timeouts()
            universe=self.config['universe']
            scan_start=self.monotonic()
            cursor=self.runtime.get('opportunity_scan_cursor',0) % len(universe)
            # A round that decides a window reads the whole universe; the time-boxed scan
            # could otherwise leave part of it unevaluated in its window.
            window_round=self._window_round(now)
            full=window_round and self.config['window_scan']['full_universe']
            for index in range(len(universe)):
                slot=(cursor+index) % len(universe)
                symbol=universe[slot]
                if symbol in priority:
                    continue
                limit=None if full else self.config.get('opportunity_scan_seconds')
                if limit is not None and self.monotonic()-scan_start>=limit:
                    self.runtime['opportunity_scan_cursor']=slot
                    self._audit('opportunity_scan_yielded',elapsed_seconds=self.monotonic()-scan_start)
                    break
                if (q:=self._fetch(symbol)):
                    quotes[symbol] = q
            else:
                self.runtime['opportunity_scan_cursor']=0
                if full:
                    self._audit('window_full_scan',elapsed_seconds=self.monotonic()-scan_start)
            self._refresh_managed_quotes(quotes,refreshed)
            if window_round:
                self._refresh_entry_candidates(quotes,refreshed)
            risk_ready=self._risk(quotes)
            if risk_ready:
                self._entries(quotes,refreshed)
            if self.runtime['risk_paused']:
                self._timeouts()  # cancel identified pending buys once
            stale = [s for s in self.execution.state['positions'] if not self._managed_fresh(quotes.get(s))]
            # A holding valued from its live book is recorded, not an incident: an
            # incident marks the run blocked, which a monitor would show as critical.
            if any(s not in self._book_marks for s in stale):
                self._incident('managed_quote_unavailable')
            elif stale:
                self._audit('managed_trade_stale', symbols=stale)
            self._audit_stop_on_book([s for s in stale if s in self._book_marks])
        except Exception as exc:
            self._incident(str(exc) if type(exc) is ExecutionBlocked else type(exc).__name__)
        self.runtime['heartbeat_at'] = self.clock().isoformat()
        self.runtime['last_cycle_diagnostics'] = dict(
            cycle_id=self._cycle_audit['cycle_id'], completed_at=self.clock().isoformat(),
            duration_seconds=(self.clock()-now).total_seconds(),
            quote_requests=sum(o['kind']=='quote_received' for o in self._cycle_audit['observations']),
            rejected_quotes=sum(o['kind']=='quote_received' and not o['accepted'] for o in self._cycle_audit['observations']))
        self._write_cycle_audit(dict(self._cycle_audit, phase='completed',
            completed_at=self.clock().isoformat(), status=self.runtime['status'],
            entries_blocked=self.runtime.get('entries_blocked'), risk_paused=self.runtime['risk_paused'],
            valuation_available=self.runtime.get('valuation_available'),
            http_budget=deepcopy(self.execution.state.get('http_budget')),
            managed_quote_refreshes=deepcopy(self.runtime['managed_quote_refreshes'])))
        if self.runtime.get('telemetry_ok') is False:
            self.runtime['entries_blocked'] = 'cycle_telemetry_unavailable'
        self._cycle_audit = None
        self._save()

    def run_session(self, *, sleep=None, stop_requested=None):
        """Single admitted session, manual launch; no next-day auto-start.

        A .halt file in the state directory latches the entry pause. Removing it
        does not silently resume entries. Caller owns process logging/notification.
        """
        import time as timing
        sleep=sleep or timing.sleep
        stop_requested=stop_requested or (lambda:False)
        if self.session != self.clock().astimezone(ART).date():
            raise ExecutionBlocked('Explicit session setup required')
        heartbeat=None
        try:
            while not stop_requested():
                now=self.clock().astimezone(ART)
                if now.date()!=self.session or now.time()>=session_end(self.config):break
                if (self.execution.path.parent/'.halt').exists() and not self.execution.state['paused']:
                    self.execution.pause(True)
                self.cycle()
                if heartbeat is None or (now-heartbeat).total_seconds()>=30:
                    self.runtime['heartbeat_at']=self.clock().isoformat();self._save();heartbeat=now
                sleep(5)
        finally:
            self.runtime['stopped_at']=self.clock().isoformat()
            self.runtime['status']='stopped'
            self._save()  # no liquidation, capital reset or forgotten orders
