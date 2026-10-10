"""A private, bounded saved-image OCR worker for one reservation batch."""
from __future__ import annotations

from collections import OrderedDict, deque
import hashlib
import json
import queue
import subprocess
import threading
import time
import uuid

from kakao_windows_ocr import WindowsOcrError, _parse_result, _validate_request


class WindowsOcrSession:
    """Serialize requests and verify their IDs; never retry native actions."""
    MAX_REPLY_BYTES = 4 * 1024 * 1024
    CACHE_LIMIT = 128

    def __init__(self, *, cancel_event=None):
        self.cancel_event = cancel_event
        self._process = None
        self._replies = queue.Queue()
        self._errors = deque(maxlen=8)
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._threads = []
        self._closed = False
        self._broken = False
        self.launches = 0
        self.requests = 0
        self.cache_hits = 0

    def _read_stdout(self, stream):
        try:
            while True:
                line = stream.readline(self.MAX_REPLY_BYTES + 1)
                if not line:
                    self._replies.put(None)
                    return
                if len(line) > self.MAX_REPLY_BYTES or not line.endswith(b"\n"):
                    self._replies.put(WindowsOcrError("OCR worker returned an oversized or incomplete reply."))
                    return
                self._replies.put(line)
        except (OSError, ValueError):
            self._replies.put(None)

    def _read_stderr(self, stream):
        try:
            while True:
                data = stream.read(512)
                if not data:
                    return
                self._errors.append(data.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            return

    def _check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            self._stop()
            raise InterruptedError("Saved-image OCR was cancelled.")
        if self._closed or self._broken:
            raise WindowsOcrError("OCR worker is closed or unavailable.")
        if self._process is not None and self._process.poll() is not None:
            self._stop()
            raise WindowsOcrError("OCR worker exited before returning a result.")

    def _check_deadline(self, deadline):
        self._check()
        if time.monotonic() >= deadline:
            self._stop()
            raise WindowsOcrError("OCR worker timed out. No result was accepted.")

    def _receive(self, deadline):
        while True:
            self._check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop()
                raise WindowsOcrError("OCR worker timed out. No result was accepted.")
            try:
                reply = self._replies.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if reply is None or isinstance(reply, WindowsOcrError):
                self._stop()
                if isinstance(reply, WindowsOcrError):
                    raise reply
                detail = ''.join(self._errors)[-1200:]
                raise WindowsOcrError("OCR worker exited before returning a result." + (" " + detail if detail else ""))
            try:
                value = json.loads(reply.decode("utf-8-sig"))
                if not isinstance(value, dict) or type(value.get("version")) is not int or value['version'] != 1:
                    raise ValueError
                return value
            except (UnicodeError, ValueError):
                self._stop()
                raise WindowsOcrError("OCR worker returned an invalid response.") from None

    def _start(self, powershell, script, deadline):
        command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Server"]
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            self.launches += 1
            for name, target, stream in (("stdout", self._read_stdout, self._process.stdout),
                                         ("stderr", self._read_stderr, self._process.stderr)):
                reader = threading.Thread(target=target, args=(stream,), name="kakao-ocr-" + name, daemon=True)
                reader.start()
                self._threads.append(reader)
            ready = self._receive(deadline)
            if set(ready) != {"version", "ready"} or ready.get("ready") is not True:
                raise WindowsOcrError("OCR worker did not complete initialization.")
        except OSError as error:
            self._stop()
            raise WindowsOcrError(f"Could not start Windows OCR: {error}") from error
        except BaseException:
            self._stop()
            raise

    def recognize_image(self, path, language="ko", *, timeout=45, scale=2.0):
        image, powershell, script = _validate_request(path, language, timeout, scale)
        deadline = time.monotonic() + timeout
        while True:
            self._check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WindowsOcrError("OCR worker timed out waiting for another image.")
            if self._lock.acquire(timeout=min(0.1, remaining)):
                break
        try:
            self._check()
            digest = hashlib.sha256(image.read_bytes()).digest()
            key = (digest, language.lower(), float(scale))
            if key in self._cache:
                if hashlib.sha256(image.read_bytes()).digest() != digest:
                    raise WindowsOcrError("OCR input changed during verification.")
                self._check_deadline(deadline)
                self.cache_hits += 1
                self._cache.move_to_end(key)
                return self._cache[key]
            if self._process is None:
                self._start(powershell, script, deadline)
            self._check_deadline(deadline)
            request_id = uuid.uuid4().hex
            request = {"version": 1, "id": request_id, "path": str(image), "language": language, "scale": float(scale)}
            try:
                self._process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
                self._process.stdin.flush()
                self.requests += 1
            except (OSError, ValueError):
                self._stop()
                raise WindowsOcrError("OCR worker could not receive an image request.") from None
            response = self._receive(deadline)
            if response.get("id") != request_id or type(response.get("ok")) is not bool:
                self._stop()
                raise WindowsOcrError("OCR worker response does not belong to this image.")
            if not response['ok']:
                raise WindowsOcrError("Windows OCR failed: " + str(response.get("error", "no error detail"))[:1200])
            try:
                result = _parse_result(json.dumps(response.get("result"), ensure_ascii=False))
            except WindowsOcrError:
                self._stop()
                raise
            if hashlib.sha256(image.read_bytes()).digest() != digest:
                raise WindowsOcrError("OCR input changed while it was being read.")
            self._check_deadline(deadline)
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self.CACHE_LIMIT:
                self._cache.popitem(last=False)
            return result
        finally:
            self._lock.release()

    def _stop(self):
        self._broken = True
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            # The session remains broken. A later close may finish cleanup.
            pass
        finally:
            if process.poll() is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass

    def close(self):
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._stop()
        for thread in self._threads:
            thread.join(timeout=0.2)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
