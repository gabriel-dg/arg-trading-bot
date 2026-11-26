"""Short, shareable local reads for observers. No HTTP or inference retries.

On Windows shared_reader opens with FILE_SHARE_READ | WRITE | DELETE, asking for no
more than a read, and read_bytes closes the handle before parsing or waiting. While the
handle is open, the bot's os.replace of its state can still be denied; the bot retries
that replacement for about a second, and a read lasts milliseconds.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import time


def access_failure(exc):
    return isinstance(exc, PermissionError) or (isinstance(exc, OSError) and (
        exc.errno == errno.EACCES or getattr(exc, "winerror", None) in (5, 32, 33)))


@contextmanager
def shared_reader(path):
    if os.name != "nt":
        with Path(path).open("rb") as stream:
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ, share read|write|delete, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    handle = kernel.CreateFileW(str(Path(path).resolve()), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def read_bytes(path, *, opener=None, sleep=time.sleep):
    opener = opener or shared_reader
    for attempt in range(6):
        try:
            with opener(path) as stream:
                raw = stream.read()
            return raw  # Close the handle BEFORE parsing/hashing or waiting.
        except OSError as exc:
            if not access_failure(exc) or attempt == 5:
                raise
            sleep(.05)


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8"))
