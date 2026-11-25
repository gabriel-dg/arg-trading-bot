"""Read-only SNTP offset measurement (RFC 4330). Never sets the system clock."""
from __future__ import annotations

from datetime import datetime, timezone
import socket
import struct
import time

NTP_EPOCH_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01


class ClockMeasurementUnavailable(RuntimeError):
    def __init__(self, reason: str):
        self.diagnostic = {"reason": reason}
        super().__init__(reason)


def _sample(server: str, timeout: float) -> float:
    packet = b"\x1b" + 47 * b"\0"  # LI=0, VN=3, Mode=3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t0 = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
        t3 = time.time()
    if len(data) < 48:
        raise ClockMeasurementUnavailable("short_response")
    def ts(offset):
        seconds, fraction = struct.unpack("!II", data[offset:offset + 8])
        return seconds - NTP_EPOCH_DELTA + fraction / 2**32
    t1, t2 = ts(32), ts(40)  # server receive, server transmit
    return ((t1 - t0) + (t2 - t3)) / 2  # server time minus local time


def measure_clock(server: str = "pool.ntp.org", *, samples: int = 3, timeout: float = 5.0,
                  pause: float = 1.0) -> dict:
    """Three offsets in seconds (exchange time = local time + offset)."""
    offsets = []
    try:
        for i in range(samples):
            if i:
                time.sleep(pause)
            offsets.append(round(_sample(server, timeout), 4))
    except (OSError, socket.timeout):
        raise ClockMeasurementUnavailable("network_error") from None
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "source": f"{server}/SNTP",
            "offsets_seconds": offsets}
