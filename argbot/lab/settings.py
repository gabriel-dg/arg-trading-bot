"""Settings de operatoria explícitos: porcentajes humanos (20 = 20%), sin red."""
from dataclasses import asdict, dataclass, field, replace
from datetime import time
import json
import math
from pathlib import Path

DEFAULT_SETTINGS = Path(__file__).resolve().parents[2] / "configs/bot_settings.json"


@dataclass(frozen=True)
class OperatingLimits:
    max_position_pct: float | None = 15
    max_positions: int | None = 4
    max_invested_pct: float | None = 80
    daily_loss_pct: float | None = 2
    include_unrealized_loss: bool = False

    def __post_init__(self):
        for key in ("max_position_pct", "max_invested_pct", "daily_loss_pct"):
            value = getattr(self, key)
            if value is not None and (type(value) not in (int, float)
                    or not math.isfinite(value) or not 0 < value <= 100):
                raise ValueError(f"{key}: porcentaje mayor que 0 y hasta 100, o null")
        if self.max_positions is not None and (type(self.max_positions) is not int
                                               or self.max_positions < 1):
            raise ValueError("max_positions: cantidad entera positiva o null")
        if type(self.include_unrealized_loss) is not bool:
            raise ValueError("include_unrealized_loss: booleano obligatorio")

    def snapshot(self):
        return {"schema_version": 1, "limits": asdict(self)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Setting duplicado: {key}")
        result[key] = value
    return result


def load_settings(path=DEFAULT_SETTINGS):
    raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if isinstance(raw, dict) and type(raw.get("schema_version")) is int and raw["schema_version"] in (2, 3):
        keys = {"schema_version", "limits", "schedule", "exits", "execution"}
        if raw["schema_version"] == 3:
            keys.add("events")
        if set(raw) != keys:
            raise ValueError("Settings: se requieren las secciones exactas de la versión")
        parts = {}
        classes = [("limits", OperatingLimits), ("schedule", Schedule), ("exits", Exits), ("execution", Execution)]
        if raw["schema_version"] == 3:
            classes.append(("events", EventOptions))
        for key, cls in classes:
            values = raw[key]
            if not isinstance(values, dict) or set(values) != set(asdict(cls())):
                raise ValueError(f"Settings {key}: faltan opciones o hay opciones desconocidas")
            if key == "schedule":
                values = dict(values)
                if not isinstance(values["entry_times"], list):
                    raise ValueError("entry_times debe ser una lista")
                values["entry_times"] = tuple(values["entry_times"])
            parts[key] = cls(**values)
        return BotSettings(**parts)
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "limits"}:
        raise ValueError("Settings: se requieren solo schema_version y limits")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("Versión de settings no soportada")
    limits = raw["limits"]
    if not isinstance(limits, dict) or set(limits) != set(asdict(OperatingLimits())):
        raise ValueError("Settings limits: faltan opciones o hay opciones desconocidas")
    return OperatingLimits(**limits)


def _number(name, value, low, high=math.inf, *, integer=False):
    valid_type = type(value) is int if integer else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: valor inválido ({low} a {high})")


def _time(value):
    if not isinstance(value, str) or len(value) != 5:
        raise ValueError("Horario inválido: usar HH:MM en ART")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Horario inválido: usar HH:MM en ART") from exc
    if parsed.strftime("%H:%M") != value:
        raise ValueError("Horario inválido: usar HH:MM en ART")
    return parsed


@dataclass(frozen=True)
class Schedule:
    mode: str = "windows"
    entry_times: tuple[str, ...] = ("11:45", "14:00", "16:00")
    interval_minutes: int | None = None
    interval_start: str = "10:45"
    entry_cutoff: str = "16:20"

    def __post_init__(self):
        cutoff, start = _time(self.entry_cutoff), _time(self.interval_start)
        # No prometer horarios posteriores al límite del motor congelado reutilizado.
        if not time(10, 30) < cutoff <= time(16, 40) or not time(10, 30) <= start < cutoff:
            raise ValueError("Inicio desde 10:30 y anterior al cutoff; cutoff máximo 16:40 ART")
        if type(self.entry_times) is not tuple:
            raise ValueError("entry_times debe ser una secuencia inmutable")
        times = tuple(_time(t) for t in self.entry_times)
        if tuple(sorted(set(times))) != times or any(not time(10, 30) <= t < cutoff for t in times):
            raise ValueError("entry_times: horarios únicos ordenados entre 10:30 y cutoff exclusivo")
        if self.mode == "windows":
            if not times or self.interval_minutes is not None:
                raise ValueError("windows requiere horarios e interval_minutes null")
        elif self.mode == "interval":
            if times:
                raise ValueError("interval requiere entry_times vacío")
            _number("interval_minutes", self.interval_minutes, 1, 1440, integer=True)
        else:
            raise ValueError("schedule.mode debe ser windows o interval")


@dataclass(frozen=True)
class Exits:
    stop_atr: float = 2.5
    trail_atr: float = 3.0
    grace_sessions: int = 2
    giveback_pct: float = 10
    lock_threshold_pct: float = 5
    lock_fraction_pct: float = 40
    stop_confirmations: int = 3
    emergency_first_touch: bool = False

    def __post_init__(self):
        for key in ("stop_atr", "trail_atr"):
            _number(key, getattr(self, key), 0)
            if getattr(self, key) == 0:
                raise ValueError(f"{key} debe ser positivo")
        for key in ("giveback_pct", "lock_threshold_pct", "lock_fraction_pct"):
            _number(key, getattr(self, key), 0, 100)
        _number("grace_sessions", self.grace_sessions, 0, integer=True)
        _number("stop_confirmations", self.stop_confirmations, 1, integer=True)
        if type(self.emergency_first_touch) is not bool:
            raise ValueError("emergency_first_touch debe ser booleano")


@dataclass(frozen=True)
class Execution:
    passive_buy: bool = True
    adaptive_buy: bool = False
    buy_timeout_min: int = 45
    sell_timeout_min: int = 15
    cooldown_min: int = 60

    def __post_init__(self):
        for key in ("passive_buy", "adaptive_buy"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} debe ser booleano")
        for key in ("buy_timeout_min", "sell_timeout_min", "cooldown_min"):
            _number(key, getattr(self, key), 0, integer=True)


@dataclass(frozen=True)
class EventOptions:
    event_entries: bool = False
    event_exits: bool = False

    def __post_init__(self):
        if type(self.event_entries) is not bool or type(self.event_exits) is not bool:
            raise ValueError("events: opciones estrictamente booleanas")


@dataclass(frozen=True)
class BotSettings:
    limits: OperatingLimits = field(default_factory=OperatingLimits)
    schedule: Schedule = field(default_factory=Schedule)
    exits: Exits = field(default_factory=Exits)
    execution: Execution = field(default_factory=Execution)
    events: EventOptions | None = None  # None conserva snapshots v2 byte-semánticamente.

    @property
    def events_enabled(self):
        return self.events is not None and (self.events.event_entries or self.events.event_exits)

    def snapshot(self):
        result = {"schema_version": 2, **asdict(self)}
        if self.events is None:
            result.pop("events")
        else:
            result["schema_version"] = 3
        result["schedule"]["entry_times"] = list(self.schedule.entry_times)
        return result

    def apply(self, config):
        exits = asdict(self.exits)
        exits["giveback_pct"] /= 100
        exits["lock_threshold"] = exits.pop("lock_threshold_pct") / 100
        exits["lock_fraction"] = exits.pop("lock_fraction_pct") / 100
        return replace(config, **exits, **asdict(self.execution), **(asdict(self.events) if self.events else {}),
                       entry_interval_min=self.schedule.interval_minutes or 0)
