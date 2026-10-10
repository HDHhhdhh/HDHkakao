"""Automatic Spoiler tablet re-registration for explicit Kakao auth loss.

The Kakao account password lives in a separate CurrentUser DPAPI vault.  It
is never added to Hub JSON, command-line arguments, environment variables, or
logs.  Spoiler's installed ``spoiler-auth.mjs`` receives it once over stdin
and returns fresh LOCO credentials over its bounded NDJSON protocol.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from kakao_auth_lease import AuthLease, AuthLeaseError

from kakao_direct import (
    CredentialVault,
    CredentialVaultError,
    DirectBridgeError,
    normalize_credentials,
)
from kakao_runtime import RuntimeValidationError, VerifiedSpoilerRuntime


ACCOUNT_DPAPI_ENTROPY = b"KakaoSender.TabletAccount.v1"
DEFAULT_REAUTH_COOLDOWN_SECONDS = 15 * 60.0
DEFAULT_AUTH_TIMEOUT_SECONDS = 210.0
MAX_AUTH_OUTPUT_BYTES = 1024 * 1024
MAX_AUTH_FRAME_BYTES = 64 * 1024
AUTO_REAUTH_CAUSES = frozenset(
    {
        "invalid_access_token",
        "token_expired",
        "oauth_expired",
        "login_rejected",
        "refresh_rejected",
        "refresh_unavailable",
        "refresh_http_error",
        "not_authenticated",
        "missing_token",
        "missing_user_id",
        "credentials_missing",
        "credentials_invalid",
    }
)
_SAFE_CODE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_code(value: Any, fallback: str = "tablet_reauth_failed") -> str:
    candidate = str(value or fallback)
    sanitized = _SAFE_CODE_PATTERN.sub("_", candidate)[:64]
    return sanitized or fallback


def default_account_vault_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Spoiler" / "tablet-account.dat"
    return Path.home() / "AppData" / "Local" / "Spoiler" / "tablet-account.dat"


def default_spoiler_runtime_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Spoiler" / "runtime"
    return Path.home() / "AppData" / "Local" / "Spoiler" / "runtime"


class TabletAccountVault:
    """Validated account secret stored with a distinct DPAPI entropy."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        crypto_module: Any | None = None,
    ) -> None:
        self._vault = CredentialVault(
            default_account_vault_path() if path is None else path,
            crypto_module=crypto_module,
            entropy=ACCOUNT_DPAPI_ENTROPY,
        )

    @property
    def path(self) -> Path:
        return self._vault.path

    @staticmethod
    def _normalize(payload: Mapping[str, Any]) -> dict[str, str]:
        email = str(payload.get("email") or "").strip()
        password = str(payload.get("password") or "")
        if not email or "@" not in email or len(email) > 320:
            raise CredentialVaultError("Tablet account email is invalid")
        if not password or len(password) > 4096:
            raise CredentialVaultError("Tablet account password is invalid")
        return {"email": email, "password": password}

    def load(self) -> dict[str, str] | None:
        payload = self._vault.load()
        return None if payload is None else self._normalize(payload)

    def save(self, email: str, password: str) -> Path:
        return self._vault.save(self._normalize({"email": email, "password": password}))

    def exists(self) -> bool:
        return self.path.is_file()


def discover_spoiler_auth_runtime(
    runtime_root: str | os.PathLike[str] | None = None,
) -> tuple[Path, Path]:
    """Return the verified runtime explicitly published by Spoiler."""
    root = runtime_root if runtime_root is not None else default_spoiler_runtime_root()
    try:
        with VerifiedSpoilerRuntime(root) as runtime:
            return runtime.node, runtime.auth
    except RuntimeValidationError:
        raise DirectBridgeError("Spoiler auth runtime must be prepared by restarting Spoiler", code="spoiler_auth_missing") from None


class SpoilerTabletAuthenticator:
    """Bounded adapter for Spoiler's stdin/stdout tablet auth protocol."""

    def __init__(
        self,
        *,
        runtime_root: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        event_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.runtime_root = runtime_root
        self.timeout = max(10.0, float(timeout))
        self._popen_factory = popen_factory
        self._event_callback = event_callback

    def _emit(self, event: str, detail: str = "") -> None:
        if self._event_callback is None:
            return
        try:
            self._event_callback(event, detail)
        except Exception:
            pass

    def authenticate(
        self,
        account: Mapping[str, str],
        *,
        device_uuid: str | None = None,
    ) -> dict[str, Any]:
        root = self.runtime_root if self.runtime_root is not None else default_spoiler_runtime_root()
        try:
            runtime = VerifiedSpoilerRuntime(root)
        except RuntimeValidationError:
            raise DirectBridgeError("Spoiler auth runtime must be prepared by restarting Spoiler", code="spoiler_auth_missing") from None
        with runtime:
            return self._authenticate_with_runtime(account, runtime.node, runtime.auth, device_uuid=device_uuid)

    def _authenticate_with_runtime(
        self,
        account: Mapping[str, str],
        node: Path,
        auth: Path,
        *,
        device_uuid: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "email": account["email"],
            "password": account["password"],
            "deviceType": "tablet",
            # Spoiler first tries the current slot and only replaces it when
            # the server explicitly reports that the tablet slot is occupied.
            "force": True,
        }
        if device_uuid:
            request["deviceUuid"] = str(device_uuid)
        payload = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        environment = {key: value for key, value in os.environ.items() if key.upper() != "NODE_OPTIONS"}
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "env": environment,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self._emit("starting", "")
        process = None
        deadline = time.monotonic() + self.timeout
        try:
            process = self._popen_factory([str(node), str(auth)], **kwargs)
        except Exception:
            if process is not None:
                self._stop_process(process)
            raise DirectBridgeError("Tablet re-registration could not start", code="tablet_reauth_start_failed") from None
        finally:
            request["password"] = ""

        frames: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=8)
        stopped = threading.Event()
        def publish(kind: str, value: bytes = b"") -> None:
            while not stopped.is_set():
                try:
                    frames.put((kind, value), timeout=0.1)
                    return
                except queue.Full:
                    continue
        def read_frames() -> None:
            total = 0
            try:
                while not stopped.is_set():
                    # Bound each read before decoding/JSON allocation. A
                    # flooding or unterminated line never grows an unbounded
                    # capture_output buffer or blocks the consumer indefinitely.
                    line = process.stdout.readline(MAX_AUTH_FRAME_BYTES + 1)
                    if not line:
                        publish("eof")
                        return
                    total += len(line)
                    if total > MAX_AUTH_OUTPUT_BYTES or len(line) > MAX_AUTH_FRAME_BYTES:
                        publish("invalid")
                        return
                    publish("line", line)
            except Exception:
                publish("invalid")
        def write_input(data: bytes) -> None:
            try:
                process.stdin.write(data)
                process.stdin.flush()
                process.stdin.close()
            except Exception:
                publish("start_failed")
        reader = threading.Thread(target=read_frames, name="spoiler-tablet-auth-reader", daemon=True)
        writer = threading.Thread(target=write_input, args=(payload,), name="spoiler-tablet-auth-input", daemon=True)
        reader.start()
        writer.start()
        # The writer retains the secret only until stdin consumption completes.
        # This releases local references without claiming byte zeroization.
        payload = b""
        credentials: dict[str, Any] | None = None
        error_code: str | None = None
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired([str(node), str(auth)], self.timeout)
                kind, line = frames.get(timeout=remaining)
                if kind == "invalid":
                    raise DirectBridgeError("Spoiler auth output was invalid", code="spoiler_auth_protocol_invalid")
                if kind == "start_failed":
                    raise DirectBridgeError("Tablet re-registration could not start", code="tablet_reauth_start_failed")
                if kind == "eof":
                    break
                try:
                    frame = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(frame, Mapping):
                    continue
                frame_type = frame.get("type")
                if frame_type == "passcode":
                    code = str(frame.get("code") or "")
                    if re.fullmatch(r"[0-9]{4,8}", code):
                        self._emit("passcode", code)
                elif frame_type == "status" and frame.get("status") == "force_starting":
                    self._emit("force_starting", "")
                elif frame_type == "credentials" and isinstance(frame.get("credentials"), Mapping):
                    try:
                        credentials = normalize_credentials(frame["credentials"])
                    except CredentialVaultError:
                        raise DirectBridgeError("Spoiler auth output was invalid", code="spoiler_auth_protocol_invalid") from None
                elif frame_type == "error":
                    error_code = _safe_code(frame.get("code"), "tablet_reauth_failed")
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if credentials is not None and returncode == 0 and error_code is None:
                self._emit("complete", "")
                return credentials
            raise DirectBridgeError(
                "Tablet re-registration failed",
                code=error_code or ("tablet_reauth_failed" if returncode else "spoiler_auth_protocol_invalid"),
            )
        except (queue.Empty, subprocess.TimeoutExpired):
            raise DirectBridgeError("Tablet re-registration timed out", code="tablet_reauth_timeout") from None
        finally:
            stopped.set()
            self._stop_process(process)
            reader.join(timeout=1.0)
            writer.join(timeout=1.0)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class TabletReauthCoordinator:
    """Single-flight, rate-limited account re-registration callback."""

    def __init__(
        self,
        *,
        account_vault: TabletAccountVault | None = None,
        credential_vault: CredentialVault | None = None,
        authenticator: SpoilerTabletAuthenticator | None = None,
        cooldown: float = DEFAULT_REAUTH_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.account_vault = account_vault or TabletAccountVault()
        self.credential_vault = credential_vault or CredentialVault()
        self.authenticator = authenticator or SpoilerTabletAuthenticator()
        self.cooldown = max(0.0, float(cooldown))
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._last_attempt: float | None = None
        self._last_success = False

    def configured(self) -> bool:
        return self.account_vault.exists()

    def __call__(self, cause_code: str) -> dict[str, Any] | None:
        cause = _safe_code(cause_code)
        if cause not in AUTO_REAUTH_CAUSES:
            return None
        with self._lock:
            now = self._monotonic()
            if self._last_attempt is not None and now - self._last_attempt < self.cooldown:
                if self._last_success:
                    current = self.credential_vault.load()
                    return normalize_credentials(current) if current else None
                raise DirectBridgeError("Tablet re-registration is cooling down", code="tablet_reauth_cooldown")
            self._last_attempt = now
            self._last_success = False
            account = self.account_vault.load()
            if account is None:
                raise DirectBridgeError("Tablet account is not configured", code="tablet_account_missing")
            baseline = self.credential_vault.load_snapshot()
            try:
                with AuthLease(self.credential_vault.path, timeout=10.0):
                    original = self.credential_vault.load_snapshot()
                    if original.revision != baseline.revision and original.credentials is not None:
                        current = normalize_credentials(original.credentials)
                        previous = normalize_credentials(baseline.credentials) if baseline.credentials else None
                        if previous is None or (current["user_id"], current["device_uuid"]) != (previous["user_id"], previous["device_uuid"]):
                            raise DirectBridgeError("Authentication identity changed", code="auth_identity_changed")
                        if (current["oauth_token"], current.get("refresh_token")) != (previous["oauth_token"], previous.get("refresh_token")):
                            self._last_success = True
                            return current
                    existing = original.credentials
                    device_uuid = str((existing or {}).get("device_uuid") or "").strip() or None
                    credentials = self.authenticator.authenticate(account, device_uuid=device_uuid)
                    canonical = normalize_credentials(credentials)
                    self.credential_vault.save_if_unchanged(canonical, original.revision)
                    self._last_success = True
                    return canonical
            except AuthLeaseError as error:
                raise DirectBridgeError("Another app is authenticating", code=error.code) from None


def _setup_account(vault: TabletAccountVault) -> int:
    email = input("Kakao account email: ").strip()
    password = getpass.getpass("Kakao account password: ")
    vault.save(email, password)
    password = ""
    print(f"Encrypted tablet account saved: {vault.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the encrypted Spoiler tablet re-auth account.")
    parser.add_argument("command", choices=("setup", "status"))
    parser.add_argument("--vault", type=Path, default=None)
    args = parser.parse_args(argv)
    vault = TabletAccountVault(args.vault)
    if args.command == "setup":
        return _setup_account(vault)
    print("configured" if vault.exists() else "not configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
