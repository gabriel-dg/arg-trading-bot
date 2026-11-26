"""Shadow-advisor data contract. No provider or broker in this module.

Only anonymous normalized candidates enter this contract. This module holds the
request side used by the local capture (argbot.agents.advisor_feed); nothing here
sends data anywhere.
"""
from __future__ import annotations

from datetime import timedelta
import math
import uuid

from argbot.execution import instant

FEATURES = {"drawdown_fraction", "day_range_fraction", "price_to_vwap", "spread_fraction"}
EGRESS_SCOPE = "anonymous_four_features_timestamps_v1"


def prepare_request(candidates, *, now, synthetic=True, authorization=None):
    if type(synthetic) is not bool:
        raise ValueError("Invalid synthetic marker")
    if not synthetic and (not authorization or authorization.get("scope") != EGRESS_SCOPE
                          or authorization.get("real_data_approved") is not True
                          or not authorization.get("operator_reference")):
        raise ValueError("Real-data advisor not authorized or connected")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 30:
        raise ValueError("Invalid candidate set")
    admitted = []
    for row in candidates:
        if set(row) != FEATURES | {"source_at"}:
            raise ValueError("Unexpected advisor input fields")
        source = instant(row["source_at"])
        if not 0 <= (now - source).total_seconds() <= 60:
            raise ValueError("Advisor source is stale or future")
        values = {k: row[k] for k in sorted(FEATURES)}
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values.values()):
            raise ValueError("Invalid normalized features")
        if not (-1 <= values["drawdown_fraction"] <= 0 and 0 <= values["day_range_fraction"] <= 1
                and 0 < values["price_to_vwap"] <= 10 and 0 <= values["spread_fraction"] <= 1):
            raise ValueError("Features outside contract")
        admitted.append(dict(candidate_id=f"C{len(admitted) + 1:02d}", source_at=source.isoformat(), **values))
    expires = min(now + timedelta(seconds=20), min(instant(r["source_at"]) + timedelta(seconds=60) for r in admitted))
    return dict(schema=1, mode="shadow_offline" if synthetic else "shadow_observation", synthetic=synthetic,
                request_id=uuid.uuid4().hex, requested_at=now.isoformat(), expires_at=expires.isoformat(),
                candidates=admitted)
