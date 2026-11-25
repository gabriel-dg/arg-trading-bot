from __future__ import annotations

import logging
import re
import sys
from datetime import date
from pathlib import Path

# Redact credentials and tokens if they ever appear in a log record.
_REDACTION_PATTERNS = [
    re.compile(r"(authorization\s*[:=]\s*)(?:bearer\s+)?[\w\-\.]+", re.IGNORECASE),
    re.compile(r"(\bbearer\s+)[\w\-\.]+", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*)\S+", re.IGNORECASE),
    re.compile(r"((?:access_token|refresh_token)\s*[:=]\s*)[\w\-\.]+", re.IGNORECASE),
]


def _redact(text: str) -> str:
    for pat in _REDACTION_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    return text


class RedactSensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Format first so a token in args (e.g. "Bearer %s", token) is redacted too.
        try:
            message = record.getMessage()
        except Exception:
            message = record.msg if isinstance(record.msg, str) else str(record.msg)
            record.msg = _redact(message) if isinstance(message, str) else message
            if record.args:
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a for a in record.args
                )
        else:
            record.msg = _redact(message)
            record.args = ()
        if record.exc_text:
            record.exc_text = _redact(record.exc_text)
        return True


def _has_redact_filter(owner: logging.Filterer) -> bool:
    return any(isinstance(item, RedactSensitiveFilter) for item in owner.filters)


def _add_redact_filter(owner: logging.Filterer) -> None:
    if not _has_redact_filter(owner):
        owner.addFilter(RedactSensitiveFilter())


def _redact_handlers(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        _add_redact_filter(handler)


def install_redaction(name: str = "argbot") -> logging.Logger:
    """Attach the redaction filter to `name` and its current handlers (idempotent).

    A parent logger's own filters are not applied to records from child loggers,
    so handlers on `name` must carry the filter as well.
    """
    logger = logging.getLogger(name)
    _add_redact_filter(logger)
    _redact_handlers(logger)
    return logger


def configure_live_logging(stream=None) -> logging.Logger:
    """Redact live-mode logs, including records from child loggers such as argbot.execution.

    Attaches RedactSensitiveFilter to handlers of `argbot` and of the root logger.
    Logger-level filters on `argbot` are not enough: child records skip them when
    they propagate. If neither logger has a handler, a StreamHandler with the
    filter is added to `argbot` so there is a redacting handler in the path.
    """
    logger = install_redaction("argbot")
    root = logging.getLogger()
    _redact_handlers(root)
    if not logger.handlers and not root.handlers:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.set_name("argbot_live_redact")
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        ))
        _add_redact_filter(handler)
        logger.addHandler(handler)
    return logger


def setup_logging(
    level: int = logging.INFO,
    log_dir: Path | str = "logs",
) -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)

    logger = install_redaction("argbot")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_file = log_dir / f"argbot_{date.today().isoformat()}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Handler filters, not only the logger filter: child loggers skip the latter.
    _redact_handlers(logger)
    return logger
