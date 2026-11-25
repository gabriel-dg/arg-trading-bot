"""Tesis registradas: fuentes, vigencia y disponibilidad sin información del futuro."""
import json
from pathlib import Path
from urllib.parse import urlparse
from argbot.lab.models import timestamp

REQUIRED = {"id", "symbol", "published_at", "observed_at", "analyzed_at", "expires_at",
            "source_url", "fact", "interpretation", "direction", "invalidation",
            "evidence", "source_hash"}

def validate_event(raw: dict) -> dict:
    missing = REQUIRED - raw.keys()
    if missing:
        raise ValueError(f"Evento incompleto: {sorted(missing)}")
    event = dict(raw)
    for key in ("published_at", "observed_at", "analyzed_at", "expires_at"):
        timestamp(event[key])
    published, observed, analyzed, expires = (
        timestamp(event[k]) for k in ("published_at", "observed_at", "analyzed_at", "expires_at"))
    if not published <= observed <= analyzed < expires:
        raise ValueError("Evento con fechas inconsistentes")
    if event["direction"] not in {"positive", "negative", "neutral"}:
        raise ValueError("Dirección inválida")
    parsed = urlparse(event["source_url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Fuente HTTPS inválida")
    for key in ("id", "symbol", "fact", "interpretation", "invalidation", "evidence", "source_hash"):
        if not isinstance(event[key], str) or not event[key].strip():
            raise ValueError(f"Campo de evento vacío: {key}")
    return event

class EventBook:
    def __init__(self, events=()):
        self.events = []
        seen = set()
        for raw in events:
            event = validate_event(raw)
            if event["id"] in seen:
                raise ValueError("ID de evento duplicado")
            seen.add(event["id"])
            self.events.append(event)

    @classmethod
    def load(cls, path: Path):
        return cls(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip())

    def latest(self, symbol, now):
        available = [e for e in self.events if e["symbol"] == symbol
                     and timestamp(e["analyzed_at"]) <= now < timestamp(e["expires_at"])]
        return max(available, key=lambda e: timestamp(e["analyzed_at"]), default=None)
