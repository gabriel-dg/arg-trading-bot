"""Referencias fechadas del laboratorio. Nunca usa la versión más reciente por defecto."""
from datetime import date, time, timedelta
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo
from argbot.lab.models import timestamp

ZONE = "America/Argentina/Buenos_Aires"


class ReferenceBook:
    def __init__(self, document):
        if document.get("schema_version") != 1:
            raise ValueError("Versión de referencias desconocida")
        self.document = document
        self.versions = document["versions"]
        seen = set()
        for version in self.versions:
            available = timestamp(version["available_at"])
            if available in seen or not version["source"]:
                raise ValueError("Referencia duplicada o sin fuente")
            seen.add(available)
            date.fromisoformat(version["updated_on"])
            if version["timezone"] != ZONE:
                raise ValueError("Zona de mercado no admitida")
            if date.fromisoformat(version["valid_from"]) > date.fromisoformat(version["valid_to"]):
                raise ValueError("Vigencia invertida")
            if time.fromisoformat(version["open"]) >= time.fromisoformat(version["close"]):
                raise ValueError("Horario invertido")
            for day in version.get("holidays", []):
                date.fromisoformat(day)
            for hours in version.get("sessions", {}).values():
                if hours and time.fromisoformat(hours[0]) >= time.fromisoformat(hours[1]):
                    raise ValueError("Sesión excepcional invertida")
            for symbol, asset in version["universe"].items():
                if not symbol or asset["kind"] not in {"local", "cedear"} or not asset["group"]:
                    raise ValueError("Clasificación de universo inválida")
                date.fromisoformat(asset["active_from"])
                if asset.get("active_to"):
                    date.fromisoformat(asset["active_to"])

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def version(self, day, asof):
        if asof.tzinfo is None:
            raise ValueError("Reloj sin zona")
        known = [v for v in self.versions if timestamp(v["available_at"]) <= asof]
        if not known:
            raise ValueError("No hay referencia disponible a esa fecha")
        version = max(known, key=lambda v: timestamp(v["available_at"]))
        if not date.fromisoformat(version["valid_from"]) <= day <= date.fromisoformat(version["valid_to"]):
            raise ValueError("Calendario fuera de vigencia; requiere actualización")
        return version

    def hours(self, day, asof):
        v = self.version(day, asof)
        override = v.get("sessions", {}).get(day.isoformat(), "default")
        if override != "default":
            return tuple(time.fromisoformat(x) for x in override) if override else None
        if day.weekday() >= 5 or day.isoformat() in v.get("holidays", []):
            return None
        return time.fromisoformat(v["open"]), time.fromisoformat(v["close"])

    def is_open(self, now):
        local = now.astimezone(ZoneInfo(ZONE))
        hours = self.hours(local.date(), now)
        return bool(hours and hours[0] <= local.time() <= hours[1])

    def universe(self, day, asof):
        v = self.version(day, asof)
        return {s: a for s, a in v["universe"].items()
                if date.fromisoformat(a["active_from"]) <= day
                and (not a.get("active_to") or day < date.fromisoformat(a["active_to"]))}

    def slots(self, start, end, interval_seconds):
        if (start.tzinfo is None or end.tzinfo is None or start > end
                or not math.isfinite(interval_seconds) or interval_seconds <= 0):
            raise ValueError("Ventana de cobertura inválida")
        # La grilla comienza en start: debe coincidir con el sondeo acordado.
        now = start
        while now <= end:
            if self.is_open(now):
                yield now, self.universe(now.astimezone(ZoneInfo(ZONE)).date(), now)
            now += timedelta(seconds=interval_seconds)

    def change(self, previous, current, *, symbol, currency, start, end, asof, basis="raw"):
        actions = self.version(end, asof).get("corporate_actions", [])
        return economic_change(previous, current, actions, symbol=symbol, currency=currency,
                               start=start, end=end, asof=asof, basis=basis)


def economic_change(previous, current, actions, *, symbol, currency, start, end, asof, basis="raw"):
    """Retorno diagnóstico por acción original; NO ajusta datos ni cartera.

    Dividendos expresados por acción vigente, en la moneda del precio. No incluye
    impuestos/costos. Rechaza series ya ajustadas para evitar doble ajuste.
    """
    if basis != "raw" or not all(math.isfinite(x) and x > 0 for x in (previous, current)):
        raise ValueError("Se requieren precios crudos positivos y finitos")
    if asof.tzinfo is None or start >= end or asof.astimezone(ZoneInfo(ZONE)).date() < end:
        raise ValueError("Intervalo de ajuste inválido o futuro")
    shares, cash, seen, used = 1.0, 0.0, set(), []
    effective_days = set()
    for action in sorted(actions, key=lambda a: (a["effective_on"], a["id"])):
        if action["symbol"] != symbol:
            continue
        if action["id"] in seen:
            raise ValueError("Ajuste duplicado")
        seen.add(action["id"])
        if not start < date.fromisoformat(action["effective_on"]) <= end:
            continue
        if timestamp(action["available_at"]) > asof:
            continue
        if action["effective_on"] in effective_days:
            raise ValueError("Varios ajustes en la misma fecha requieren una convención explícita")
        effective_days.add(action["effective_on"])
        if not action["source"]:
            raise ValueError("Ajuste sin fuente")
        value = action["value"]
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Ajuste inválido")
        if action["kind"] == "split":
            shares *= value
        elif action["kind"] == "dividend":
            if action["currency"] != currency:
                raise ValueError("Dividendo y precio en monedas diferentes")
            cash += shares * value
        else:
            raise ValueError("Ajuste no soportado")
        used.append(action["id"])
    return {"return": (shares * current + cash) / previous - 1,
            "shares": shares, "cash": cash, "actions": used,
            "limitation": "Solo ajustes conocidos y suministrados; no certifica cobertura corporativa."}
