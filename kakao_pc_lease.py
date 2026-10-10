"""A cancellable, process-wide lease for the current user's Kakao PC UI.

Windows uses a session-local named kernel mutex: abandoned owners are recovered
by Windows, and recursion follows the owning thread. The POSIX fallback combines
an RLock with flock for local test/development environments.
"""
from __future__ import annotations

import getpass
import hashlib
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class PcUiLeaseCancelled(RuntimeError):
    pass


class PcUiLeaseTimeout(TimeoutError):
    pass


DEFAULT_LEASE_TIMEOUT = 30.0


class PcUiLease:
    def __init__(self, name: str | None = None) -> None:
        user_key = hashlib.sha256(getpass.getuser().casefold().encode("utf-8")).hexdigest()[:20]
        self.name = name or f"Local\\KakaoTalkHub.PcUi.{user_key}"
        self._init_lock = threading.Lock()
        self._local_lock = threading.RLock()
        self._owner = threading.local()
        self._handle: Any = None
        self._kernel: Any = None
        self._file: Any = None

    def _initialize(self) -> None:
        with self._init_lock:
            if self._handle is not None or self._file is not None:
                return
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
                kernel.CreateMutexW.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
                kernel.ReleaseMutex.restype = wintypes.BOOL
                handle = kernel.CreateMutexW(None, False, self.name)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._kernel, self._handle = kernel, handle
            else:
                key = hashlib.sha256(self.name.encode("utf-8")).hexdigest()
                path = Path(tempfile.gettempdir()) / f"kakao-pc-ui-{key}.lock"
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                self._file = os.fdopen(fd, "a+b")

    def acquire(self, blocking: bool = True, timeout: float = DEFAULT_LEASE_TIMEOUT, *, cancel_event: Any = None) -> bool:
        self._initialize()
        # Deliberately bounded even when legacy callers pass timeout=-1.
        timeout = DEFAULT_LEASE_TIMEOUT if timeout is None or timeout < 0 else float(timeout)
        deadline = time.monotonic() + (timeout if blocking else 0)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            remaining = max(0.0, deadline - time.monotonic())
            if os.name == "nt":
                wait_ms = int(min(remaining, 0.05) * 1000) if blocking else 0
                status = self._kernel.WaitForSingleObject(self._handle, wait_ms)
                if status in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                    self._owner.depth = getattr(self._owner, "depth", 0) + 1
                    if cancel_event is not None and cancel_event.is_set():
                        self.release()
                        return False
                    return True
                if status != 0x102:  # WAIT_TIMEOUT
                    import ctypes
                    raise ctypes.WinError(ctypes.get_last_error())
            elif self._local_lock.acquire(blocking=False):
                import fcntl

                depth = getattr(self._owner, "depth", 0)
                try:
                    if depth == 0:
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._owner.depth = depth + 1
                    if cancel_event is not None and cancel_event.is_set():
                        self.release()
                        return False
                    return True
                except BlockingIOError:
                    self._local_lock.release()
            if not blocking or time.monotonic() >= deadline:
                return False
            if os.name != "nt":
                pause = min(0.05, max(0.0, deadline - time.monotonic()))
                if cancel_event is not None:
                    cancel_event.wait(pause)
                else:
                    time.sleep(pause)

    def release(self) -> None:
        depth = getattr(self._owner, "depth", 0)
        if depth <= 0:
            raise RuntimeError("PC UI lease must be released by its owning thread")
        if os.name == "nt":
            if not self._kernel.ReleaseMutex(self._handle):
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            if depth == 1:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._local_lock.release()
        self._owner.depth = depth - 1

    @contextmanager
    def lease(self, cancel_event: Any = None, timeout: float = DEFAULT_LEASE_TIMEOUT) -> Iterator["PcUiLease"]:
        if not self.acquire(timeout=timeout, cancel_event=cancel_event):
            if cancel_event is not None and cancel_event.is_set():
                raise PcUiLeaseCancelled("PC 카카오톡 제어 대기가 취소되었습니다.")
            raise PcUiLeaseTimeout("다른 작업이 PC 카카오톡을 사용 중입니다. 잠시 후 다시 시도해 주세요.")
        try:
            yield self
        finally:
            self.release()

    def __enter__(self) -> "PcUiLease":
        if not self.acquire():
            raise PcUiLeaseTimeout("다른 작업이 PC 카카오톡을 사용 중입니다.")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


PC_KAKAO_UI_LOCK = PcUiLease()


def pc_kakao_ui_lease(cancel_event: Any = None, timeout: float = DEFAULT_LEASE_TIMEOUT):
    return PC_KAKAO_UI_LOCK.lease(cancel_event=cancel_event, timeout=timeout)
