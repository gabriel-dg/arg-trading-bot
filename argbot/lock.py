"""Account-scoped OS lease, shared across candidate folders; no broker IO."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading


class AlreadyRunning(RuntimeError):
    pass


_held: set[str] = set()
_guard = threading.Lock()


def account_key(account: str) -> str:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("An explicit stable account identifier is required")
    return hashlib.sha256(account.strip().encode("utf-8")).hexdigest()


class AccountLease:
    """Must stay held for the entire read/modify/send lifecycle.

    Only processes of this package honor this lease. Never run another tool that
    trades the same account at the same time.
    """

    def __init__(self, account: str):
        self.key = account_key(account)
        self._handle = None
        self._pid = os.getpid()

    def acquire(self):
        with _guard:
            if self.key in _held:
                raise AlreadyRunning("An executor already owns this account")
            _held.add(self.key)
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                kernel.CreateMutexW.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
                kernel.ReleaseMutex.restype = wintypes.BOOL
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.CloseHandle.restype = wintypes.BOOL
                handle = kernel.CreateMutexW(None, False, "Global\\ArgTradingBot_" + self.key)
                if not handle:
                    raise OSError(ctypes.get_last_error(), "Cannot create account mutex")
                result = kernel.WaitForSingleObject(handle, 0)
                if result not in (0, 0x80):  # acquired or previous owner died
                    kernel.CloseHandle(handle)
                    if result == 0x102:
                        raise AlreadyRunning("Another process owns this account")
                    raise OSError(ctypes.get_last_error(), "Cannot acquire account mutex")
                self._kernel, self._handle = kernel, handle
            else:
                import fcntl
                folder = Path(tempfile.gettempdir()) / "arg-trading-bot-leases"
                folder.mkdir(exist_ok=True, mode=0o700)
                stream = (folder / (self.key + ".lock")).open("a+b")
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.close()
                    raise AlreadyRunning("Another process owns this account") from None
                self._handle = stream
            return self
        except BaseException:
            with _guard:
                _held.discard(self.key)
            raise

    def check(self):
        if self._handle is None or os.getpid() != self._pid:
            raise RuntimeError("Account lease is not owned by this process")

    def close(self):
        if self._handle is None:
            return
        self.check()
        if os.name == "nt":
            self._kernel.ReleaseMutex(self._handle)
            self._kernel.CloseHandle(self._handle)
        else:
            self._handle.close()
        self._handle = None
        with _guard:
            _held.discard(self.key)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.close()
