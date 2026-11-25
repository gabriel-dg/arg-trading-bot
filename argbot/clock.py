"""Exchange time for judging broker quote ages during the session.

The PC clock can drift a few seconds from NTP, so quote ages can turn slightly
negative during the day. A fresh broker quote then looks "from the future"
and is rejected, blocking entries and leaving stops without a fresh check. The bot
measures the offset again during the session, read-only like the startup check (it
never sets or resyncs Windows), and applies it only where a time is compared with
IOL's fechaHora. Times that other processes compare with their own clock (heartbeat,
incidents, orders, clock.json) stay on the PC clock.
"""
from datetime import timedelta
import math
from statistics import median

# The startup bound: a larger or inconsistent measurement is not applied, and the bot
# keeps rejecting quotes from the future as before.
MAX_OFFSET_SECONDS = 10
MAX_SAMPLE_SPREAD_SECONDS = 1
REFRESH_SECONDS = 20 * 60


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def estimate(offsets):
    """Median of three agreeing samples within the bound; None means not applied."""
    if not isinstance(offsets, list) or len(offsets) != 3 or not all(_number(x) for x in offsets):
        return None
    if any(abs(x) > MAX_OFFSET_SECONDS for x in offsets) or max(offsets)-min(offsets) > MAX_SAMPLE_SPREAD_SECONDS:
        return None
    return float(median(offsets))


class ExchangeClock:
    """PC clock plus the last accepted NTP offset. Without a measurer it is the PC clock."""

    def __init__(self, raw, monotonic, *, measure=None, refresh_seconds=REFRESH_SECONDS):
        self.raw, self.monotonic, self.measure = raw, monotonic, measure
        self.refresh_seconds = refresh_seconds
        self.offset_seconds = 0.0
        self.next_check = None

    @property
    def enabled(self):
        return self.measure is not None

    def now(self):
        return self.raw()+timedelta(seconds=self.offset_seconds)

    def start(self, evidence, prior=None):
        """Session admission; returns the runtime snapshot, or None when disabled.

        A first launch applies its startup samples, taken seconds before. A continuation
        keeps the session's last applied offset and measures again in its first round."""
        if not self.enabled:
            return None
        continuation = bool(evidence.get('continuation_checked_at'))
        snapshot = None
        if continuation and isinstance(prior, dict) and _number(prior.get('offset_seconds')) \
                and abs(prior['offset_seconds']) <= MAX_OFFSET_SECONDS:
            snapshot = dict(offset_seconds=float(prior['offset_seconds']), basis='continuation',
                            checked_at=prior.get('checked_at'))
        elif (value := estimate(evidence.get('offsets_seconds'))) is not None:
            snapshot = dict(offset_seconds=value, basis='startup', checked_at=evidence.get('checked_at'))
        if snapshot is None:
            snapshot = dict(offset_seconds=0.0, basis='not_applied', checked_at=None)
        self.offset_seconds = snapshot['offset_seconds']
        fresh = snapshot['basis'] == 'startup' and not continuation
        self.next_check = self.monotonic()+self.refresh_seconds if fresh else None
        return snapshot

    def due(self):
        return self.enabled and (self.next_check is None or self.monotonic() >= self.next_check)

    def refresh(self):
        """One read-only measurement that never raises: (audit record, snapshot or None).

        A failure, a timeout or rejected samples keep the last applied offset; the next
        attempt comes one interval later. Only fixed fields are kept, never w32tm output."""
        self.next_check = self.monotonic()+self.refresh_seconds
        try:
            measured = self.measure()
            offsets, checked_at = measured.get('offsets_seconds'), measured.get('checked_at')
        except Exception as exc:
            diagnostic = getattr(exc, 'diagnostic', None)
            reason = diagnostic.get('reason') if isinstance(diagnostic, dict) else None
            return dict(accepted=False, reason=reason if isinstance(reason, str) else type(exc).__name__,
                        applied_offset_seconds=self.offset_seconds), None
        samples = [float(x) for x in offsets] if isinstance(offsets, list) and all(_number(x) for x in offsets) else None
        checked_at = checked_at if isinstance(checked_at, str) else None
        value = estimate(offsets)
        if value is None:
            return dict(accepted=False, reason='samples_rejected', offsets_seconds=samples, checked_at=checked_at,
                        applied_offset_seconds=self.offset_seconds), None
        self.offset_seconds = value
        return (dict(accepted=True, offsets_seconds=samples, checked_at=checked_at, applied_offset_seconds=value),
                dict(offset_seconds=value, basis='refresh', checked_at=checked_at))
