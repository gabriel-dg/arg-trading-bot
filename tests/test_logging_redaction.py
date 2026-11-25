"""Logs must not carry tokens, passwords or raw HTTP bodies."""
import io
import logging

import httpx
import pytest

from argbot.brokers.iol.exceptions import IOLAPIError, IOLAuthError
from argbot.logging_config import (
    RedactSensitiveFilter,
    _redact,
    configure_live_logging,
    install_redaction,
)
from test_auth import TokenServer, _make_auth
from test_client import make_client, _json


def test_redact_covers_bearer_password_and_tokens():
    assert "abc.def.ghi" not in _redact("Authorization: Bearer abc.def.ghi")
    assert "[REDACTED]" in _redact("Authorization: Bearer abc.def.ghi")
    assert "secretpass" not in _redact("password=secretpass")
    assert "tokvalue" not in _redact("access_token=tokvalue")
    assert "tokvalue" not in _redact("refresh_token=tokvalue")


def test_filter_is_attached_to_the_argbot_logger():
    logger = install_redaction("argbot")
    assert any(isinstance(item, RedactSensitiveFilter) for item in logger.filters)
    install_redaction("argbot")
    assert sum(isinstance(item, RedactSensitiveFilter) for item in logger.filters) == 1


def test_client_error_logs_status_but_not_the_body(caplog):
    body = {"cuenta": "secret-account-999", "access_token": "leak-me-now"}
    client, _ = make_client(lambda req: _json(500, body))
    with caplog.at_level(logging.INFO, logger="argbot.brokers.iol.client"):
        with pytest.raises(IOLAPIError):
            client.get_estado_cuenta()
    text = caplog.text
    assert "500" in text
    assert "secret-account-999" not in text
    assert "leak-me-now" not in text


def test_auth_logs_and_exceptions_omit_secrets(caplog, tmp_path):
    secret = "visible-only-if-logged"
    with caplog.at_level(logging.DEBUG, logger="argbot.brokers.iol.auth"):
        with pytest.raises(IOLAuthError) as caught:
            _make_auth(tmp_path, lambda req: httpx.Response(401, text=secret)).headers()
        assert secret not in str(caught.value)
    assert secret not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="argbot.brokers.iol.auth"):
        auth = _make_auth(tmp_path, TokenServer())
        auth.headers()
        auth.close()
    assert "acc-1" not in caplog.text
    assert "ref-1" not in caplog.text


def test_filter_redacts_records_on_the_argbot_tree():
    install_redaction("argbot")
    filt = RedactSensitiveFilter()
    dirty = logging.LogRecord(
        "argbot.brokers.iol.auth",
        logging.INFO,
        __file__,
        0,
        "Authorization: Bearer super-secret-token",
        None,
        None,
    )
    assert filt.filter(dirty)
    assert "super-secret-token" not in dirty.getMessage()
    assert "[REDACTED]" in dirty.getMessage()


def _restore_logging(logger, handlers, filters, propagate):
    logger.handlers[:] = handlers
    logger.filters[:] = filters
    logger.propagate = propagate


def test_live_logging_redacts_child_logger_output_on_existing_handlers():
    """Records from argbot.execution must be redacted on root handlers after live setup.

    This must inspect formatted handler output, not call the filter directly:
    a logger-level filter on `argbot` is skipped for child records.
    """
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setLevel(logging.DEBUG)
    capture.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root = logging.getLogger()
    argbot = logging.getLogger("argbot")
    saved = (
        (root, list(root.handlers), list(root.filters), root.propagate),
        (argbot, list(argbot.handlers), list(argbot.filters), argbot.propagate),
    )
    saved_handler_filters = [
        (handler, list(handler.filters))
        for handler in (*root.handlers, *argbot.handlers)
    ]
    root.addHandler(capture)
    try:
        configure_live_logging()
        token = "super-secret-live-token"
        logging.getLogger("argbot.execution").warning("Authorization: Bearer %s", token)
        text = stream.getvalue()
        assert "argbot.execution" in text
        assert token not in text
        assert "[REDACTED]" in text
    finally:
        for logger, handlers, filters, propagate in saved:
            _restore_logging(logger, handlers, filters, propagate)
        for handler, filters in saved_handler_filters:
            handler.filters[:] = filters


def test_live_logging_adds_a_redacting_handler_when_none_exist():
    stream = io.StringIO()
    root = logging.getLogger()
    argbot = logging.getLogger("argbot")
    saved = (
        (root, list(root.handlers), list(root.filters), root.propagate),
        (argbot, list(argbot.handlers), list(argbot.filters), argbot.propagate),
    )
    saved_handler_filters = [
        (handler, list(handler.filters))
        for handler in (*root.handlers, *argbot.handlers)
    ]
    root.handlers.clear()
    argbot.handlers.clear()
    try:
        configure_live_logging(stream=stream)
        token = "super-secret-live-token"
        logging.getLogger("argbot.execution").warning("Authorization: Bearer %s", token)
        text = stream.getvalue()
        assert token not in text
        assert "[REDACTED]" in text
    finally:
        for logger, handlers, filters, propagate in saved:
            _restore_logging(logger, handlers, filters, propagate)
        for handler, filters in saved_handler_filters:
            handler.filters[:] = filters
