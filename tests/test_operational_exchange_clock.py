"""Exchange clock: quote ages on IOL's time, the PC clock plus a measured offset.

A PC clock a few seconds behind NTP makes fresh broker quotes look "from the future";
without the offset they would be rejected, blocking entries and stop checks.
Fake clock, market, broker and NTP samples only: no w32tm, network or model calls.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
from types import SimpleNamespace

import httpx
import pytest

from test_operational_candidate import Broker, Clock, Market, config, raw_quote
from argbot.strategy.candidate import ART, Candidate, MechanicalRules, config_hash
from argbot.clock import REFRESH_SECONDS, ExchangeClock, estimate
from argbot.execution import ExecutionBlocked, ExecutionController, instant
from argbot.market import quote

NTP_AT = '2026-09-30T14:20:00+00:00'


class Mono:
    t = 1000.0
    def __call__(self): return self.t


class Measure:
    def __init__(self, *results): self.results, self.calls = list(results), 0
    def __call__(self):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def ntp(*offsets):
    return dict(checked_at=NTP_AT, source='synthetic', offsets_seconds=list(offsets))


def admit(bot, clock, offsets):
    bot.start_session(clock().date(), clock().date()-timedelta(days=1), clock_evidence={
        'session': clock().date().isoformat(), 'checked_at': clock().isoformat(), 'samples': 3,
        'offset_seconds': max(offsets, key=abs), 'offsets_seconds': list(offsets), 'source': 'synthetic'})




def cycles(r):
    lines = (r.folder/'operational-cycles.private.jsonl').read_text(encoding='utf-8').splitlines()
    return [row for row in map(json.loads, lines) if 'observations' in row]


def observed(r, kind):
    return [o for row in cycles(r) for o in row['observations'] if o['kind'] == kind]


def rejected_future(r):
    return [o for o in observed(r, 'quote_received') if o['reason'] == 'stale_or_future_quote']


@pytest.mark.parametrize('offsets,expected', [
    ([3.93, 3.9, 3.95], 3.93), ([0.02, 0.01, 0.03], 0.02), ([-2.5, -2.4, -2.6], -2.5), ([10, 9.5, 10], 10.0),
    ([10.01, 10, 10], None), ([3.9, 3.9, 5.0], None), ([3.9, 3.9], None), ([3.9, 3.9, 3.9, 3.9], None),
    ([3.9, True, 3.9], None), ([3.9, float('nan'), 3.9], None), ([3.9, '3.9', 3.9], None), (None, None)])
def test_estimate_is_the_median_of_three_agreeing_samples_within_ten_seconds(offsets, expected):
    assert estimate(offsets) == expected














def test_continuation_keeps_the_session_offset_and_measures_in_its_first_round():
    mono = Mono(); measure = Measure(ntp(.3, .3, .4))
    clock = ExchangeClock(lambda: datetime(2026, 11, 4, 14, tzinfo=ART), mono, measure=measure)
    evidence = dict(checked_at='2026-11-04T13:20:00+00:00', offsets_seconds=[3.93, 3.9, 3.95],
                    continuation_checked_at='2026-11-04T17:00:00+00:00')
    prior = dict(offset_seconds=.01, basis='refresh', checked_at='2026-11-04T16:50:00+00:00')
    assert clock.start(evidence, prior) == dict(prior, basis='continuation')
    assert clock.offset_seconds == .01 and clock.due() and measure.calls == 0
    assert clock.start(evidence, dict(offset_seconds=11.0)) == dict(
        offset_seconds=3.93, basis='startup', checked_at='2026-11-04T13:20:00+00:00')
    assert clock.due()  # a continuation measures in its first round even from the morning samples
    record, snapshot = clock.refresh()
    assert record['accepted'] and snapshot == dict(offset_seconds=.3, basis='refresh', checked_at=NTP_AT)
    assert not clock.due()
    mono.t += REFRESH_SECONDS
    assert clock.due()


def test_disabled_clock_is_the_pc_clock_and_never_measures():
    now = datetime(2026, 11, 4, 14, tzinfo=ART)
    clock = ExchangeClock(lambda: now, Mono())
    assert clock.start(dict(offsets_seconds=[3.9, 3.9, 3.9]), None) is None
    assert clock.now() == now and not clock.due() and not clock.enabled


def test_unusable_startup_samples_apply_no_offset_and_measure_in_the_first_round():
    clock = ExchangeClock(lambda: datetime(2026, 11, 4, 14, tzinfo=ART), Mono(), measure=Measure())
    assert clock.start(dict(checked_at=NTP_AT, offsets_seconds=[0.1, 0.1, 2.5])) == dict(
        offset_seconds=0.0, basis='not_applied', checked_at=None)
    assert clock.due()


