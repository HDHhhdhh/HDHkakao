"""Cross-process lease for remote tablet authentication, separate from vault writes.

Both hosts lock byte zero of ``.<vault name>.auth.lock``. The file must remain
in place: deleting it could let two processes lock different file objects.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import threading
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AuthLeaseError(RuntimeError):
    def __init__(self, code: str = "auth_lease_busy") -> None:
        self.code = code
        super().__init__(code)


_gates: dict[str, threading.Lock] = {}
_guard = threading.Lock()


class AuthLease:
    def __init__(self, vault_path, *, timeout: float = 10.0, cancel=None):
        destination = Path(vault_path).expanduser().resolve()
        self.path = destination.with_name(f".{destination.name}.auth.lock")
        self.timeout = max(0.0, float(timeout))
        self.cancel = cancel
        with _guard:
            self._gate = _gates.setdefault(os.path.normcase(str(self.path)), threading.Lock())
        self._handle = None
        self._held = False
        self._release_guard = threading.Lock()

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        acquired = False
        try:
            while True:
                if self.cancel is not None and self.cancel.is_set():
                    raise AuthLeaseError("auth_lease_cancelled")
                if self._gate.acquire(blocking=False):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    raise AuthLeaseError()
                time.sleep(0.025)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+b")
            while True:
                if self.cancel is not None and self.cancel.is_set():
                    raise AuthLeaseError("auth_lease_cancelled")
                try:
                    self._handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._held = True
                    return self
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise AuthLeaseError() from None
                    time.sleep(0.025)
        except Exception:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            if acquired:
                self._gate.release()
            raise

    def release(self):
        with self._release_guard:
            if not self._held:
                return
            self._held = False
            try:
                self._handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
                self._gate.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()
