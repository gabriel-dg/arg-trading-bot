"""Shared local reads: retry only access denials, close before parsing, never hide damage."""
from contextlib import contextmanager
import errno
import io
import json

import pytest

from argbot import files


def test_shared_read_closes_before_parse_and_retries_only_access():
    calls, closed, sleeps = [], [], []

    @contextmanager
    def reader(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError(errno.EACCES, "fixture")
        try:
            yield io.BytesIO(b'{"fresh":true}')
        finally:
            closed.append(True)
    assert files.read_bytes("fixture", opener=reader, sleep=sleeps.append) == b'{"fresh":true}'
    assert len(calls) == 3 and closed == [True] and sleeps == [.05, .05]


@pytest.mark.parametrize("error", [FileNotFoundError(), OSError(errno.EIO, "fixture")])
def test_shared_read_does_not_hide_missing_or_corrupt_storage(error):
    @contextmanager
    def reader(_):
        raise error
        yield
    with pytest.raises(type(error)):
        files.read_bytes("fixture", opener=reader, sleep=lambda _: pytest.fail("No retry"))


def test_shared_read_access_retry_budget_is_finite():
    calls = []

    @contextmanager
    def reader(_):
        calls.append(1)
        raise PermissionError("fixture")
        yield
    with pytest.raises(PermissionError):
        files.read_bytes("fixture", opener=reader, sleep=lambda _: None)
    assert len(calls) == 6


def test_read_json_round_trip(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sequence": 1}), encoding="utf-8")
    assert files.read_json(path) == {"sequence": 1}

