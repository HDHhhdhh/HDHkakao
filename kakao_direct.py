"""Direct KakaoTalk transport primitives.

The normal sender in this project drives the KakaoTalk desktop UI.  This
module is a deliberately separate, opt-in transport for the long-lived
``agent-messenger`` tablet session.  It communicates with
``bridge/kakao_direct_bridge.mjs`` over newline-delimited JSON (NDJSON) and
never writes credentials to the application log.

Direct sending is disabled by default.  ``list_chats`` remains available when
the switch is off so the operator can inspect chat IDs and build a mapping,
but ``send_message`` refuses to send until the switch is explicitly enabled.

Scheduled rows are intentionally outside this transport.  The GUI routes
them through KakaoTalk's existing native reservation workflow so the
reservation survives closing the application.
"""

from __future__ import annotations

import json
import math
import errno
import hashlib
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid

from kakao_auth_lease import AuthLease, AuthLeaseError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, MutableMapping, Protocol

if os.name == "nt":  # pragma: no branch - selected once for the host OS.
    import msvcrt
else:  # pragma: no cover - Windows is primary; exercised on POSIX CI.
    import fcntl

try:  # pywin32 is the only non-stdlib dependency used by this module.
    import win32crypt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on non-Windows CI only.
    win32crypt = None  # type: ignore[assignment]


DEFAULT_SDK_ROOT = Path(__file__).resolve().parent / "bridge" / "node_modules" / "agent-messenger"
SDK_MISSING_MESSAGE = (
    "agent-messenger SDK를 찾지 못했습니다. bridge/node_modules/agent-messenger에 "
    "고정 의존성을 준비하거나 KAKAO_AGENT_MESSENGER_ROOT를 설치 경로로 설정하세요."
)
DEFAULT_BRIDGE_SCRIPT = Path(__file__).resolve().parent / "bridge" / "kakao_direct_bridge.mjs"
EXPECTED_SDK_VERSION = "2.37.1"
DPAPI_ENTROPY = b"Spoiler.LocoCredentials.v1"
DEFAULT_REQUEST_TIMEOUT = 15.0
PEER_IDENTITY_REQUEST_TIMEOUT = 60.0
MAX_CHAT_LOOKUP_IDS = 128
MAX_DIRECT_ATTACHMENTS = 10
MAX_DIRECT_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_DIRECT_ATTACHMENT_TOTAL_BYTES = MAX_DIRECT_ATTACHMENT_BYTES
DIRECT_ATTACHMENT_EXTENSIONS = frozenset(
    "jpg jpeg png gif webp pdf doc docx xls xlsx ppt pptx hwp hwpx txt csv json zip 7z tar gz mp4 mov webm mkv m4v mp3 m4a wav ogg".split()
)
_DIRECT_PHOTO_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})
_MUTATION_COMMANDS = frozenset({"send_message", "send_attachments"})
_TABLET_REAUTH_CAUSES = frozenset(
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
DEFAULT_READY_TIMEOUT = 10.0
DEFAULT_CONFIG_LOCK_TIMEOUT = 10.0
DEFAULT_CONFIG_LOCK_POLL_INTERVAL = 0.05

_SENSITIVE_KEYS = frozenset(
    {
        "oauth_token",
        "oauthToken",
        "access_token",
        "accessToken",
        "refresh_token",
        "refreshToken",
        "password",
        "token",
        "authorization",
        "cookie",
    }
)
_ENV_VAR_PATTERN = re.compile(r"%([^%]+)%")
_SAFE_CODE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_POSITIVE_CHAT_ID_PATTERN = re.compile(r"^[1-9]\d*$")


def default_credential_vault_path() -> Path:
    """Return the per-user DPAPI vault path without creating it."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Spoiler" / "loco-credentials.dat"
    # This fallback keeps import and tests useful on non-Windows hosts.  The
    # real Windows default is always %LOCALAPPDATA%\Spoiler\....
    return Path.home() / "AppData" / "Local" / "Spoiler" / "loco-credentials.dat"


# Kept as constants for callers that need to display the default location;
# constructors still call the function so tests and user environment changes
# after import are respected.
DEFAULT_CREDENTIAL_VAULT = default_credential_vault_path()
DEFAULT_CREDENTIAL_VAULT_PATH = DEFAULT_CREDENTIAL_VAULT


def _expand_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    raw = _ENV_VAR_PATTERN.sub(lambda match: os.environ.get(match.group(1), match.group(0)), raw)
    return Path(os.path.expanduser(os.path.expandvars(raw)))


def discover_node_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Find a Node executable, preferring an explicit path or env override."""

    if explicit:
        return _expand_path(explicit)
    env_path = os.environ.get("KAKAO_NODE_PATH")
    if env_path:
        return _expand_path(env_path)
    found = shutil.which("node")
    return Path(found) if found else Path("node")


def discover_sdk_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the agent-messenger package root used by the Node bridge.

    ``KAKAO_AGENT_MESSENGER_ROOT`` may point to the package directory or
    directly to an index.js file. Without an override, only the project-local
    bridge/node_modules installation is used. Missing SDKs fail at startup,
    so the GUI can still load settings and display local diagnostics.
    """

    if explicit:
        return _expand_path(explicit)
    env_root = os.environ.get("KAKAO_AGENT_MESSENGER_ROOT")
    if env_root:
        return _expand_path(env_root)
    return Path(__file__).resolve().parent / "bridge" / "node_modules" / "agent-messenger"


def normalize_chat_name(value: Any) -> str:
    """Normalize a PC KakaoTalk display name for deterministic lookup."""

    if value is None:
        raise ValueError("chat name is required")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = " ".join(text.split())
    if not text:
        raise ValueError("chat name is required")
    return text.casefold()


def normalize_chat_id(value: Any) -> str:
    """Normalize a protocol chat ID while rejecting control characters."""

    if value is None:
        raise ValueError("chat_id is required")
    text = str(value).strip()
    if not text or any(ord(char) < 0x20 for char in text):
        raise ValueError("chat_id is required")
    return text


def normalize_mappings(mappings: Mapping[Any, Any] | None) -> dict[str, str]:
    """Return a normalized PC-name -> chat-id mapping.

    Keys are NFKC/case-folded and repeated whitespace is collapsed.  A
    normalization collision with two different IDs is rejected rather than
    silently choosing one target.
    """

    if mappings is None:
        return {}
    if not isinstance(mappings, Mapping):
        raise ValueError("mappings must be an object")
    normalized: dict[str, str] = {}
    names_by_chat_id: dict[str, str] = {}
    for raw_name, raw_chat_id in mappings.items():
        name = normalize_chat_name(raw_name)
        chat_id = normalize_chat_id(raw_chat_id)
        previous = normalized.get(name)
        if previous is not None and previous != chat_id:
            raise ValueError(f"multiple chat IDs map to the same normalized name: {name}")
        previous_name = names_by_chat_id.get(chat_id)
        if previous_name is not None and previous_name != name:
            raise ValueError(f"chat ID is already mapped to another PC chat name: {chat_id}")
        normalized[name] = chat_id
        names_by_chat_id[chat_id] = name
    return normalized


def normalize_mapping_labels(labels: Mapping[Any, Any] | None) -> dict[str, str]:
    """Normalize operator-confirmed chat labels keyed by stable chat ID."""

    if labels is None:
        return {}
    if not isinstance(labels, Mapping):
        raise ValueError("mapping_labels must be an object")
    normalized: dict[str, str] = {}
    for raw_chat_id, raw_label in labels.items():
        chat_id = normalize_chat_id(raw_chat_id)
        label = unicodedata.normalize("NFKC", str(raw_label or "")).strip()
        label = " ".join(label.split())
        if not label or len(label) > 200 or any(ord(char) < 0x20 for char in label):
            raise ValueError(f"invalid chat label for chat ID: {chat_id}")
        previous = normalized.get(chat_id)
        if previous is not None and previous != label:
            raise ValueError(f"multiple labels map to the same chat ID: {chat_id}")
        normalized[chat_id] = label
    return normalized


def normalize_guarded_mapping_ids(values: Any | None) -> tuple[str, ...]:
    """Normalize the stable IDs that require a before-send identity check.

    The guard records provenance (for example, an operator-confirmed manual
    mapping), not a second copy of the peer's personal identifier.  IDs are
    normalized with :func:`normalize_chat_id`, deduplicated, and sorted so
    equivalent configurations serialize deterministically.  Strings and
    mappings are rejected instead of being iterated as accidental collections.
    """

    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)) or isinstance(values, Mapping):
        raise ValueError("guarded_mapping_ids must be an array")
    try:
        iterator = iter(values)
    except TypeError:
        raise ValueError("guarded_mapping_ids must be an array") from None

    normalized: set[str] = set()
    for value in iterator:
        normalized.add(normalize_chat_id(value))
    return tuple(sorted(normalized))


class UnmappedChatError(KeyError):
    """Raised when a PC display name has no explicitly configured chat ID."""

    def __init__(self, chat_name: Any) -> None:
        self.chat_name = str(chat_name)
        try:
            self.normalized_name = normalize_chat_name(chat_name)
        except ValueError:
            self.normalized_name = ""
        super().__init__(f"No chat_id mapping for PC chat name: {self.chat_name}")


def resolve_chat_id(chat_name: Any, mappings: Mapping[Any, Any] | "DirectKakaoConfig") -> str:
    """Resolve a normalized PC display name or raise a clear mapping error."""

    source = mappings.mappings if isinstance(mappings, DirectKakaoConfig) else mappings
    normalized = normalize_mappings(source)
    key = normalize_chat_name(chat_name)
    try:
        return normalized[key]
    except KeyError as exc:
        raise UnmappedChatError(chat_name) from exc


# Alias kept intentionally explicit for callers that describe this operation
# as a mapping lookup rather than a chat-ID resolution.
resolve_mapping = resolve_chat_id


class DirectConfigLockTimeout(TimeoutError):
    """Raised when another writer keeps the direct-config lock too long."""

    def __init__(self) -> None:
        # Keep the public error independent of the config path and its data.
        super().__init__("direct Kakao config is busy")


@dataclass
class _DirectConfigLockState:
    thread_lock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    handle: BinaryIO | None = None


_DIRECT_CONFIG_LOCK_STATES: dict[str, _DirectConfigLockState] = {}
_DIRECT_CONFIG_LOCK_STATES_GUARD = threading.Lock()


def _direct_config_lock_target(
    path: str | os.PathLike[str],
) -> tuple[Path, Path, _DirectConfigLockState]:
    destination = _expand_path(path)
    try:
        destination = destination.resolve(strict=False)
    except OSError:
        destination = Path(os.path.abspath(destination))
    lock_path = destination.with_name(f".{destination.name}.lock")
    key = os.path.normcase(str(destination))
    with _DIRECT_CONFIG_LOCK_STATES_GUARD:
        state = _DIRECT_CONFIG_LOCK_STATES.setdefault(key, _DirectConfigLockState())
    return destination, lock_path, state


def _try_lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - Windows is primary; exercised on POSIX CI.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - Windows is primary; exercised on POSIX CI.
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_lock_contention(error: OSError) -> bool:
    return error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


class DirectConfigFileLock:
    """Bounded, re-entrant writer lock adjacent to one direct config file.

    The in-process ``RLock`` serializes threads and makes nested calls safe;
    the one-byte OS lock serializes independent processes and is released by
    the operating system if a process exits.  The lock file is intentionally
    retained because unlinking a live lock file can split writers across two
    different filesystem objects.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
        poll_interval: float = DEFAULT_CONFIG_LOCK_POLL_INTERVAL,
    ) -> None:
        self.destination, self.lock_path, self._state = _direct_config_lock_target(path)
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.001, float(poll_interval))
        self._acquired = False

    def acquire(self) -> "DirectConfigFileLock":
        if self._acquired:
            raise RuntimeError("direct config lock instance is already acquired")
        deadline = time.monotonic() + self.timeout
        remaining = max(0.0, deadline - time.monotonic())
        if remaining == 0.0:
            thread_acquired = self._state.thread_lock.acquire(blocking=False)
        else:
            thread_acquired = self._state.thread_lock.acquire(timeout=remaining)
        if not thread_acquired:
            raise DirectConfigLockTimeout() from None

        handle: BinaryIO | None = None
        try:
            if self._state.depth == 0:
                self.destination.parent.mkdir(parents=True, exist_ok=True)
                handle = self.lock_path.open("a+b")
                # Windows permits locking beyond EOF. Keeping this file
                # empty avoids writing into the byte another language locked.
                while True:
                    try:
                        _try_lock_file(handle)
                        break
                    except OSError as exc:
                        if not _is_lock_contention(exc):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0.0:
                            raise DirectConfigLockTimeout() from None
                        time.sleep(min(self.poll_interval, remaining))
                self._state.handle = handle
                handle = None

            self._state.depth += 1
            self._acquired = True
            return self
        except BaseException:
            if handle is not None:
                handle.close()
            self._state.thread_lock.release()
            raise

    def release(self) -> None:
        if not self._acquired:
            raise RuntimeError("direct config lock instance is not acquired")
        try:
            self._state.depth -= 1
            if self._state.depth == 0:
                handle = self._state.handle
                self._state.handle = None
                if handle is not None:
                    try:
                        _unlock_file(handle)
                    finally:
                        handle.close()
        finally:
            self._acquired = False
            self._state.thread_lock.release()

    def __enter__(self) -> "DirectConfigFileLock":
        return self.acquire()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def direct_config_lock(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
    poll_interval: float = DEFAULT_CONFIG_LOCK_POLL_INTERVAL,
) -> DirectConfigFileLock:
    """Return the shared bounded writer lock for ``path``."""

    return DirectConfigFileLock(path, timeout=timeout, poll_interval=poll_interval)


def atomic_write_json(path: str | os.PathLike[str], payload: Any) -> Path:
    """Write JSON through a same-directory temporary file and ``os.replace``."""

    destination = _expand_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return destination


def save_mappings(path: str | os.PathLike[str], mappings: Mapping[Any, Any]) -> Path:
    """Atomically save normalized mappings in a small, forward-compatible JSON object."""

    return atomic_write_json(_expand_path(path), {"version": 1, "mappings": normalize_mappings(mappings)})


def load_mappings(path: str | os.PathLike[str]) -> dict[str, str]:
    """Load a mapping file written by :func:`save_mappings`.

    A plain object is also accepted for compatibility with hand-written
    configuration files.
    """

    source = _expand_path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("mapping file could not be read") from None
    if not isinstance(payload, Mapping):
        raise ValueError("mapping file must contain an object")
    nested = payload.get("mappings")
    return normalize_mappings(nested if isinstance(nested, Mapping) else payload)


class MappingStore:
    """Atomic JSON-backed mapping store used by the operator GUI."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = _expand_path(path)

    def load(self) -> dict[str, str]:
        return load_mappings(self.path)

    def save(self, mappings: Mapping[Any, Any]) -> Path:
        return save_mappings(self.path, mappings)

    def resolve(self, chat_name: Any) -> str:
        return resolve_chat_id(chat_name, self.load())


@dataclass
class DirectKakaoConfig:
    """Runtime settings for direct KakaoTalk operation.

    ``enabled`` deliberately defaults to ``False``.  ``mappings`` stores
    normalized PC display names and stable Kakao chat IDs, not window geometry.
    """

    enabled: bool = False
    mappings: dict[str, str] = field(default_factory=dict)
    mapping_labels: dict[str, str] = field(default_factory=dict)
    node_path: Path | str | None = None
    sdk_root: Path | str | None = None
    credential_vault: Path | str | None = None
    bridge_script: Path | str | None = None
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    ready_timeout: float = DEFAULT_READY_TIMEOUT
    guarded_mapping_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.enabled = _coerce_bool(self.enabled, False)
        self.mappings = normalize_mappings(self.mappings)
        self.mapping_labels = normalize_mapping_labels(self.mapping_labels)
        self.guarded_mapping_ids = normalize_guarded_mapping_ids(self.guarded_mapping_ids)
        mapped_ids = set(self.mappings.values())
        self.mapping_labels = {
            chat_id: label for chat_id, label in self.mapping_labels.items() if chat_id in mapped_ids
        }
        self.guarded_mapping_ids = tuple(
            chat_id for chat_id in self.guarded_mapping_ids if chat_id in mapped_ids
        )
        self.node_path = discover_node_path(self.node_path)
        self.sdk_root = discover_sdk_root(self.sdk_root)
        self.credential_vault = (
            default_credential_vault_path()
            if self.credential_vault is None
            else _expand_path(self.credential_vault)
        )
        self.bridge_script = DEFAULT_BRIDGE_SCRIPT if self.bridge_script is None else _expand_path(self.bridge_script)
        self.request_timeout = max(0.1, float(self.request_timeout))
        self.ready_timeout = max(0.1, float(self.ready_timeout))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "DirectKakaoConfig":
        """Build settings from either a direct object or a nested runtime config."""

        source: Mapping[str, Any] = payload or {}
        for nested_key in ("direct_kakao", "direct", "kakao_direct"):
            nested = source.get(nested_key)
            if isinstance(nested, Mapping):
                source = nested
                break
        mappings = source.get("mappings", source.get("chat_mappings", {}))
        mapping_labels = source.get("mapping_labels", source.get("chat_labels", {}))
        guarded_mapping_ids = source.get("guarded_mapping_ids", ())
        return cls(
            enabled=source.get("enabled", source.get("direct_enabled", False)),
            mappings=mappings if isinstance(mappings, Mapping) else {},
            mapping_labels=mapping_labels if isinstance(mapping_labels, Mapping) else {},
            node_path=source.get("node_path", source.get("node")),
            sdk_root=source.get("sdk_root", source.get("agent_messenger_root")),
            credential_vault=source.get("credential_vault", source.get("credentials_path")),
            bridge_script=source.get("bridge_script"),
            request_timeout=source.get("request_timeout", DEFAULT_REQUEST_TIMEOUT),
            ready_timeout=source.get("ready_timeout", DEFAULT_READY_TIMEOUT),
            guarded_mapping_ids=guarded_mapping_ids,
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "DirectKakaoConfig":
        """Load a model instance from a JSON config path."""

        return cls.from_dict(load_direct_config(path))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation suitable for atomic save."""

        return {
            "enabled": self.enabled,
            "mappings": dict(self.mappings),
            "mapping_labels": dict(self.mapping_labels),
            "guarded_mapping_ids": list(self.guarded_mapping_ids),
            "node_path": str(self.node_path),
            "sdk_root": str(self.sdk_root),
            "credential_vault": str(self.credential_vault),
            "bridge_script": str(self.bridge_script),
            "request_timeout": self.request_timeout,
            "ready_timeout": self.ready_timeout,
        }

    def resolve_chat_id(self, chat_name: Any) -> str:
        return resolve_chat_id(chat_name, self.mappings)

    def save(
        self,
        path: str | os.PathLike[str],
        *,
        lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
    ) -> Path:
        return save_direct_config(path, self, lock_timeout=lock_timeout)


def load_direct_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load direct settings as a JSON-friendly dictionary.

    Returning a dictionary keeps this helper convenient for the Tkinter GUI
    (``settings["enabled"]``/``settings["mappings"]``).  Use
    ``DirectKakaoConfig.load`` when a typed model is preferable.  A missing
    file means safe defaults.
    """

    source = _expand_path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return DirectKakaoConfig().to_dict()
    except (OSError, json.JSONDecodeError):
        raise ValueError("direct Kakao config could not be read") from None
    if not isinstance(payload, Mapping):
        raise ValueError("direct Kakao config must contain an object")
    return DirectKakaoConfig.from_dict(payload).to_dict()


def save_direct_config(
    path: str | os.PathLike[str],
    config: DirectKakaoConfig | Mapping[str, Any],
    *,
    lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
) -> Path:
    """Serialize and atomically save direct settings under the writer lock."""

    settings = config if isinstance(config, DirectKakaoConfig) else DirectKakaoConfig.from_dict(config)
    with direct_config_lock(path, timeout=lock_timeout):
        return atomic_write_json(path, settings.to_dict())


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"", "0", "false", "off", "no", "disabled", "아니오", "아니요"}:
        return False
    if text in {"1", "true", "on", "yes", "enabled", "예", "네"}:
        return True
    return default


def _safe_code(value: Any, fallback: str = "operation_failed") -> str:
    text = _SAFE_CODE_PATTERN.sub("_", str(value or fallback))[:64]
    return text or fallback


def redact_error(error: BaseException | Any, fallback: str = "operation failed") -> str:
    """Return a safe diagnostic string that cannot contain token-bearing text."""

    code = getattr(error, "code", None)
    if code:
        if isinstance(error, DirectSendNotStartedError):
            detail = f"{_safe_code(code)} · 원인={error.cause_code}"
            if error.cause_status_code is not None:
                detail += f" · 서버상태={error.cause_status_code}"
            return detail
        return _safe_code(code)
    return fallback


class CredentialVaultError(RuntimeError):
    """Safe wrapper for DPAPI vault failures."""


class CredentialVaultConflictError(CredentialVaultError):
    """A newer credential bundle won the shared vault write race."""

    code = "credential_vault_conflict"


@dataclass(frozen=True)
class CredentialSnapshot:
    credentials: dict[str, Any] | None
    revision: str | None


_UNCONDITIONAL_CREDENTIAL_SAVE = object()


class CredentialVault:
    """Current-user Windows DPAPI vault for Kakao credentials.

    The vault stores UTF-8 JSON protected with entropy
    ``Spoiler.LocoCredentials.v1`` and the default CurrentUser scope (flags=0).
    It intentionally has no plaintext fallback: silently writing a token in
    clear text would be worse than declining to start direct mode.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        crypto_module: Any | None = None,
        entropy: bytes = DPAPI_ENTROPY,
        lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
    ) -> None:
        self.path = default_credential_vault_path() if path is None else _expand_path(path)
        self.crypto_module = crypto_module
        if not isinstance(entropy, bytes) or not entropy:
            raise CredentialVaultError("Credential vault entropy is invalid")
        self.entropy = entropy
        self.lock_timeout = lock_timeout

    def _crypto(self) -> Any:
        crypto = self.crypto_module if self.crypto_module is not None else win32crypt
        if crypto is None:
            raise CredentialVaultError("Windows DPAPI is unavailable")
        return crypto

    def load(self) -> dict[str, Any] | None:
        """Load and decrypt credentials, returning ``None`` when absent."""

        return self.load_snapshot().credentials

    def load_snapshot(self) -> CredentialSnapshot:
        """Decrypt and identify exactly the same encrypted file contents."""

        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError:
            return CredentialSnapshot(None, None)
        except OSError:
            raise CredentialVaultError("Credential vault could not be read") from None
        try:
            # CryptUnprotectData has one fewer positional argument than
            # CryptProtectData in pywin32: (data, entropy, reserved,
            # prompt, flags).  Flags=0 means CurrentUser scope.
            result = self._crypto().CryptUnprotectData(encrypted, self.entropy, None, None, 0)
            plaintext = result[-1] if isinstance(result, tuple) else result
            if not isinstance(plaintext, (bytes, bytearray)):
                raise ValueError("invalid DPAPI payload")
            payload = json.loads(bytes(plaintext).decode("utf-8"))
        except CredentialVaultError:
            raise
        except Exception:
            raise CredentialVaultError("Credential vault could not be decrypted") from None
        if not isinstance(payload, dict):
            raise CredentialVaultError("Credential vault payload is invalid")
        return CredentialSnapshot(dict(payload), hashlib.sha256(encrypted).hexdigest())

    def save(self, credentials: Mapping[str, Any]) -> Path:
        """Encrypt and atomically save credentials under the current user."""

        self._save(credentials, _UNCONDITIONAL_CREDENTIAL_SAVE)
        return self.path

    def save_if_unchanged(
        self, credentials: Mapping[str, Any], expected_revision: str | None
    ) -> CredentialSnapshot:
        """Persist rotated tokens only if their original bundle is current."""

        return self._save(credentials, expected_revision)

    def _save(self, credentials: Mapping[str, Any], expected_revision: Any) -> CredentialSnapshot:

        if not isinstance(credentials, Mapping):
            raise CredentialVaultError("Credential payload is invalid")
        try:
            plaintext = json.dumps(dict(credentials), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise CredentialVaultError("Credential payload is invalid") from None
        try:
            encrypted = self._crypto().CryptProtectData(plaintext, None, self.entropy, None, None, 0)
            if isinstance(encrypted, tuple):
                encrypted = encrypted[-1]
            if not isinstance(encrypted, (bytes, bytearray)):
                raise ValueError("invalid DPAPI result")
            with DirectConfigFileLock(self.path, timeout=self.lock_timeout):
                if expected_revision is not _UNCONDITIONAL_CREDENTIAL_SAVE:
                    try:
                        current_revision = hashlib.sha256(self.path.read_bytes()).hexdigest()
                    except FileNotFoundError:
                        current_revision = None
                    if current_revision != expected_revision:
                        raise CredentialVaultConflictError("Credential vault changed during authentication")
                self._replace_encrypted(bytes(encrypted))
            return CredentialSnapshot(dict(credentials), hashlib.sha256(encrypted).hexdigest())
        except CredentialVaultError:
            raise
        except DirectConfigLockTimeout:
            raise CredentialVaultError("Credential vault writer lock timed out") from None
        except Exception:
            raise CredentialVaultError("Credential vault could not be protected") from None

    def _replace_encrypted(self, encrypted: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            temporary_name = None
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # Descriptive method aliases make the vault convenient to use from code
    # that treats it as a credential repository rather than a file object.
    load_credentials = load
    save_credentials = save


# Descriptive aliases make migration from earlier prototypes less brittle.
DPAPIVault = CredentialVault
WindowsCredentialVault = CredentialVault


def load_credentials(
    path: str | os.PathLike[str] | None = None,
    *,
    crypto_module: Any | None = None,
) -> dict[str, Any] | None:
    return CredentialVault(path, crypto_module=crypto_module).load()


def save_credentials(
    path: str | os.PathLike[str],
    credentials: Mapping[str, Any],
    *,
    crypto_module: Any | None = None,
) -> Path:
    return CredentialVault(path, crypto_module=crypto_module).save(credentials)


def normalize_credentials(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize snake/camel credential fields for the Node bridge."""

    if not isinstance(credentials, Mapping):
        raise CredentialVaultError("Credential payload is invalid")

    def first(*names: str) -> Any:
        for name in names:
            value = credentials.get(name)
            if value not in (None, ""):
                return value
        return None

    oauth_token = first("oauth_token", "oauthToken", "access_token", "accessToken")
    user_id = first("user_id", "userId")
    if oauth_token is None or user_id is None:
        raise CredentialVaultError("Credential payload is missing required fields")
    # The direct companion is deliberately tablet-only.  Reusing a PC
    # device identity could evict the user's desktop session, so normalize
    # legacy/accidental values to the safe tablet identity.
    device_type = str(first("device_type", "deviceType") or "tablet").casefold()
    if device_type != "tablet":
        device_type = "tablet"
    canonical: dict[str, Any] = {
        "oauth_token": str(oauth_token),
        "user_id": str(user_id),
        "device_uuid": str(first("device_uuid", "deviceUuid") or f"kakao-direct-{user_id}"),
        "device_type": device_type,
    }
    refresh_token = first("refresh_token", "refreshToken")
    if refresh_token not in (None, ""):
        canonical["refresh_token"] = str(refresh_token)
    account_id = first("account_id", "accountId")
    if account_id not in (None, ""):
        canonical["account_id"] = str(account_id)
    for key in ("created_at", "updated_at", "auth_method"):
        if credentials.get(key) not in (None, ""):
            canonical[key] = str(credentials[key])
    return canonical


class DirectBridgeError(RuntimeError):
    """Base class for safe protocol/process errors."""

    def __init__(self, message: str = "Direct Kakao bridge operation failed", *, code: str = "operation_failed") -> None:
        self.code = _safe_code(code)
        super().__init__(message)


class DirectBridgeTimeoutError(DirectBridgeError):
    """The bridge did not answer within the request deadline."""

    def __init__(self, message: str = "Direct Kakao bridge request timed out") -> None:
        super().__init__(message, code="timeout")


class DirectBridgeDisconnectedError(DirectBridgeError):
    """The bridge process ended before a response arrived."""

    def __init__(self, message: str = "Direct Kakao bridge disconnected") -> None:
        super().__init__(message, code="process_disconnected")


class DirectSendError(DirectBridgeError):
    """An explicit KakaoTalk/provider rejection for a send request."""

    def __init__(
        self,
        message: str = "KakaoTalk rejected the direct message",
        *,
        code: str = "server_rejected",
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, code=code)


class DirectSendDisabledError(DirectSendError):
    """Direct sends are intentionally blocked while the opt-in switch is off."""

    def __init__(self) -> None:
        super().__init__("Direct Kakao sending is disabled", code="direct_disabled")


class DirectSendNotStartedError(DirectSendError):
    """The bridge explicitly confirms no message/upload request was issued."""

    def __init__(self, *, retryable: bool = False, cause_code: str = "unclassified_session_error",
                 cause_status_code: int | None = None) -> None:
        self.retryable_prewrite = retryable is True
        self.cause_code = _safe_code(cause_code, "unclassified_session_error")
        self.cause_status_code = cause_status_code if type(cause_status_code) is int else None
        super().__init__("직접 전송 전 연결을 확보하지 못했습니다.", code="session_acquire_failed")


class DirectSendUnknownError(DirectSendError):
    """A send may have reached KakaoTalk but its final outcome is unknown."""

    def __init__(
        self,
        message: str = "Direct Kakao send outcome is unknown; do not retry blindly",
        *,
        code: str = "send_outcome_unknown",
        status_code: int | None = None,
    ) -> None:
        self.ambiguous = True
        super().__init__(message, code=code, status_code=status_code)


def validate_direct_attachments(attachments: Any) -> tuple[Path, ...]:
    """Validate the entire local batch before beginning any direct send.

    Limits are Hub limits, not a claim about provider capacity. The Node
    transport additionally snapshots and validates image/PDF contents before
    sending the optional text or requesting any upload ticket.
    """
    if not isinstance(attachments, (list, tuple)) or not 1 <= len(attachments) <= MAX_DIRECT_ATTACHMENTS:
        raise DirectSendError("파일은 1~10개까지 직접 전송할 수 있습니다.", code="invalid_attachment_count")
    paths: list[Path] = []
    total = 0
    for value in attachments:
        try:
            path = Path(value).resolve(strict=True)
            extension = path.suffix.lstrip(".").lower()
            if (extension not in DIRECT_ATTACHMENT_EXTENSIONS or len(path.name) > 255
                    or any(ord(char) < 32 or ord(char) == 127 for char in path.name)):
                raise DirectSendError("직접 전송을 지원하지 않는 파일 형식입니다.", code="unsupported_attachment_type")
            stat = path.stat()
            if not path.is_file() or not 0 < stat.st_size <= MAX_DIRECT_ATTACHMENT_BYTES:
                raise DirectSendError("파일은 비어 있지 않아야 하며 100 MiB 이하여야 합니다.", code="attachment_size_invalid")
            total += stat.st_size
            if total > MAX_DIRECT_ATTACHMENT_TOTAL_BYTES:
                raise DirectSendError("직접 전송 파일 합계는 100 MiB 이하여야 합니다.", code="attachment_size_invalid")
        except DirectSendError:
            raise
        except (OSError, TypeError, ValueError):
            raise DirectSendError("직접 전송할 파일을 읽을 수 없습니다.", code="attachment_read_failed") from None
        paths.append(path)
    return tuple(paths)


class _ProcessLike(Protocol):
    stdin: Any
    stdout: Any

    def poll(self) -> int | None: ...

    def terminate(self) -> Any: ...

    def kill(self) -> Any: ...

    def wait(self, timeout: float | None = None) -> Any: ...


def _redact_protocol_error(error: Mapping[str, Any] | None) -> tuple[str, int | None, bool]:
    data = error if isinstance(error, Mapping) else {}
    code = _safe_code(data.get("code"), "operation_failed")
    raw_status = data.get("status_code", data.get("server_status"))
    status: int | None = raw_status if isinstance(raw_status, int) else None
    kind = str(data.get("kind", ""))
    ambiguous = bool(data.get("ambiguous", False)) or kind in {
        "unknown",
        "transient_or_unknown",
        "transport_or_unknown",
    }
    if status == -1:
        ambiguous = True
    return code, status, ambiguous


class KakaoDirectBridge:
    """Long-lived NDJSON client for the optional Node KakaoTalk bridge."""

    def __init__(
        self,
        config: DirectKakaoConfig | Mapping[str, Any] | None = None,
        *,
        direct_enabled: bool | None = None,
        credential_vault: CredentialVault | None = None,
        credential_reloader: Callable[[str], Mapping[str, Any] | None] | None = None,
        popen_factory: Callable[..., _ProcessLike] | None = None,
    ) -> None:
        self.config = (
            DirectKakaoConfig.from_dict(config.to_dict())
            if isinstance(config, DirectKakaoConfig)
            else DirectKakaoConfig.from_dict(config)
        )
        if direct_enabled is not None:
            self.config.enabled = bool(direct_enabled)
        self.credential_vault = credential_vault or CredentialVault(self.config.credential_vault)
        self._credential_reloader = credential_reloader
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: _ProcessLike | None = None
        self._process_generation = 0
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: MutableMapping[str, queue.Queue[dict[str, Any]]] = {}
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._sequence = 0
        self._ready_event = threading.Event()
        self._disconnected_event = threading.Event()
        self._ready_info: dict[str, Any] = {}
        self._credentials: dict[str, Any] | None = None
        self._auth_lease = None
        self._auth_lease_id = None
        self._auth_lease_pending = False
        self._auth_lease_cancel = threading.Event()
        self._auth_lease_timer = None
        self._credential_revision: str | None = None
        self._authenticated = False
        self._closed = False
        self._ready_lock = threading.Lock()
        self._sdk_config_dir: Path | None = None

    def _reload_tablet_credentials(self, cause_code: str) -> dict[str, Any] | None:
        """Run the optional account re-registration hook for explicit auth loss.

        The callback receives only a stable error code.  It may decrypt the
        separately protected account secret, perform Spoiler's tablet auth,
        and return fresh LOCO credentials.  Generic disconnects and network
        errors never reach this hook.
        """

        code = _safe_code(cause_code)
        if self._credential_reloader is None or code not in _TABLET_REAUTH_CAUSES:
            return None
        try:
            original = self.credential_vault.load_snapshot()
            reloaded = self._credential_reloader(code)
            if reloaded is None:
                return None
            canonical = normalize_credentials(reloaded)
            current = self.credential_vault.load_snapshot()
            if current.credentials is not None and normalize_credentials(current.credentials) == canonical:
                # The coordinator already committed its CAS before returning.
                persisted = current
            else:
                persisted = self.credential_vault.save_if_unchanged(canonical, original.revision)
            self._credentials = canonical
            self._credential_revision = persisted.revision
            return canonical
        except DirectBridgeError:
            raise
        except CredentialVaultConflictError:
            raise DirectBridgeError(
                "Credential vault changed during tablet authentication", code="credential_vault_conflict"
            ) from None
        except Exception:
            raise DirectBridgeError(
                "Automatic tablet re-registration failed", code="tablet_reauth_failed"
            ) from None

    def _request_read_with_auth_recovery(
        self,
        command: str,
        *,
        timeout: float | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        """Retry one read after explicit token rejection and re-registration."""

        if command in _MUTATION_COMMANDS:
            raise ValueError("read recovery cannot wrap a mutation")
        try:
            return self._request(command, timeout=timeout, **arguments)
        except DirectBridgeError as exc:
            reloaded = self._reload_tablet_credentials(exc.code)
            if reloaded is None:
                raise
            self.login(reloaded)
            return self._request(command, timeout=timeout, **arguments)

    def _create_isolated_sdk_config_dir(self) -> Path:
        """Create a throwaway sync-state directory for a complete chat list."""

        self._cleanup_sdk_config_dir()
        path = Path(tempfile.mkdtemp(prefix="kakao-sender-agent-sync-")).resolve()
        self._sdk_config_dir = path
        return path

    def _cleanup_sdk_config_dir(self) -> None:
        path = self._sdk_config_dir
        self._sdk_config_dir = None
        if path is None:
            return
        try:
            temp_root = Path(tempfile.gettempdir()).resolve()
            resolved = path.resolve()
            if resolved.parent != temp_root or not resolved.name.startswith("kakao-sender-agent-sync-"):
                return
            shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass

    @property
    def process(self) -> _ProcessLike | None:
        return self._process

    @property
    def is_running(self) -> bool:
        process = self._process
        if process is None:
            return False
        try:
            return process.poll() is None
        except Exception:
            return True

    def _next_id(self, prefix: str = "req") -> str:
        with self._state_lock:
            self._sequence += 1
            return f"{prefix}-{self._sequence:08d}"

    def start(self) -> None:
        """Start the hidden bridge process once; no network operation is made here."""

        with self._state_lock:
            if self.is_running:
                return
            if self._closed:
                raise DirectBridgeError("Direct Kakao bridge is closed", code="bridge_closed")
            # Authentication and readiness belong to one child process. A
            # delayed reader from an exited process must not authenticate or
            # disconnect its replacement, or deliver a response to its waiter.
            self._release_auth_lease()
            self._auth_lease_cancel = threading.Event()
            self._auth_lease_pending = False
            self._process_generation += 1
            generation = self._process_generation
            self._authenticated = False
            self._ready_info = {}
            self._ready_event.clear()
            self._disconnected_event.clear()
            self._disconnect_pending_requests()
            command = [str(self.config.node_path), str(self.config.bridge_script)]
            environment = os.environ.copy()
            environment["KAKAO_AGENT_MESSENGER_ROOT"] = str(self.config.sdk_root)
            environment["KAKAO_DIRECT_ENABLED"] = "1" if self.config.enabled else "0"
            # agent-messenger stores incremental Kakao sync tokens.  Reusing
            # them for the read-only matching dialog can shrink later results
            # to only recently changed rooms.  An OFF bridge is list-only, so
            # use an isolated, throwaway sync state to request the complete
            # canonical room list on every refresh.
            if not self.config.enabled:
                environment["AGENT_MESSENGER_CONFIG_DIR"] = str(self._create_isolated_sdk_config_dir())
            kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "bufsize": 1,
                "text": True,
                "encoding": "utf-8",
                "env": environment,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                try:
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
                    kwargs["startupinfo"] = startupinfo
                except AttributeError:  # pragma: no cover - defensive for test doubles.
                    pass
            try:
                self._process = self._popen_factory(command, **kwargs)
            except Exception:
                self._process = None
                self._cleanup_sdk_config_dir()
                raise DirectBridgeError("Could not start the direct Kakao bridge", code="process_start_failed") from None
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                args=(self._process, generation),
                name="kakao-direct-bridge-reader",
                daemon=True,
            )
            self._reader_thread.start()

    def _write_frame(self, frame: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise DirectBridgeDisconnectedError()
        line = json.dumps(dict(frame), ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(line)
                process.stdin.flush()
        except Exception:
            raise DirectBridgeDisconnectedError() from None

    def _disconnect_pending_requests(self) -> None:
        """Wake pending callers without ever replaying a possible mutation.

        The caller holds ``_state_lock``. A queued reply wins over disconnect;
        duplicate/late child frames cannot block the only reader thread.
        """

        disconnect_payload = {
            "ok": False,
            "error": {"code": "process_disconnected", "kind": "unknown", "ambiguous": True},
        }
        for waiter in self._pending.values():
            try:
                waiter.put_nowait(disconnect_payload)
            except queue.Full:
                pass

    def _reader_loop(self, process: _ProcessLike, generation: int) -> None:
        stdout = process.stdout if process is not None else None
        try:
            while stdout is not None:
                line = stdout.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    # stdout is a strict protocol channel.  Ignore malformed
                    # diagnostics rather than exposing arbitrary child output.
                    continue
                if not isinstance(payload, dict):
                    continue
                with self._state_lock:
                    if generation != self._process_generation or process is not self._process:
                        return
                    event_name = payload.get("event") or payload.get("type")
                    if event_name:
                        self._handle_event(payload, str(event_name))
                        continue
                    request_id = payload.get("id")
                    if request_id is None:
                        continue
                    waiter = self._pending.get(str(request_id))
                    if waiter is not None:
                        try:
                            waiter.put_nowait(payload)
                        except queue.Full:
                            pass
        finally:
            with self._state_lock:
                if generation == self._process_generation and process is self._process:
                    self._auth_lease_cancel.set()
                    # EOF alone is not proof that a remote SDK call ended.
                    try:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=1.0)
                    except Exception:
                        pass
                    if process.poll() is not None:
                        self._release_auth_lease()
                    self._authenticated = False
                    self._disconnected_event.set()
                    self._ready_event.set()
                    self._disconnect_pending_requests()

    def _release_auth_lease(self) -> None:
        timer, self._auth_lease_timer = self._auth_lease_timer, None
        if timer is not None:
            timer.cancel()
        lease, self._auth_lease = self._auth_lease, None
        self._auth_lease_id = None
        if lease is not None:
            lease.release()

    def _acquire_auth_lease(self, payload, process, generation, cancel) -> None:
        lease = None
        request_id = payload.get("request_id")
        code = "auth_lease_invalid"
        try:
            expected = normalize_credentials(payload.get("credentials"))
            lease = AuthLease(self.credential_vault.path, timeout=10.0, cancel=cancel).acquire()
            snapshot = self.credential_vault.load_snapshot()
            current = normalize_credentials(snapshot.credentials)
            if any(current[key] != expected[key] for key in ("user_id", "device_uuid")):
                raise AuthLeaseError("auth_identity_changed")
            with self._state_lock:
                if cancel.is_set() or process is not self._process or generation != self._process_generation:
                    return
                self._auth_lease = lease
                lease = None
                self._auth_lease_id = uuid.uuid4().hex
                self._credentials = current
                self._credential_revision = snapshot.revision
                # A stuck SDK call cannot retain the cross-app lease forever.
                # Kill that child before releasing its lease, never while a
                # potentially live remote refresh continues in the background.
                def expire():
                    with self._state_lock:
                        if process is self._process and self._auth_lease is not None:
                            try:
                                process.kill()
                                process.wait(timeout=1.0)
                            except Exception:
                                pass
                            if process.poll() is not None:
                                self._release_auth_lease()
                self._auth_lease_timer = threading.Timer(65.0, expire)
                self._auth_lease_timer.daemon = True
                self._auth_lease_timer.start()
                self._write_frame({"id": self._next_id("lease"), "command": "auth_lease_granted",
                    "request_id": request_id, "ok": True, "auth_lease_id": self._auth_lease_id,
                    "credentials": current})
                return
        except AuthLeaseError as error:
            code = error.code
        except Exception:
            code = "auth_lease_invalid"
        finally:
            if lease is not None:
                lease.release()
            with self._state_lock:
                if process is self._process and generation == self._process_generation:
                    self._auth_lease_pending = False
        with self._state_lock:
            if process is self._process and generation == self._process_generation and not cancel.is_set():
                try:
                    self._write_frame({"id": self._next_id("lease"), "command": "auth_lease_granted",
                        "request_id": request_id, "ok": False, "error_code": code})
                except DirectBridgeError:
                    pass

    def _handle_event(self, payload: dict[str, Any], event_name: str) -> None:
        if event_name == "auth_lease_request":
            if self._auth_lease is not None or self._auth_lease_pending or not isinstance(payload.get("request_id"), str):
                return
            self._auth_lease_pending = True
            threading.Thread(target=self._acquire_auth_lease,
                args=(payload, self._process, self._process_generation, self._auth_lease_cancel),
                daemon=True, name="kakao-auth-lease").start()
            return
        if event_name == "auth_lease_release":
            valid = self._auth_lease is not None and payload.get("auth_lease_id") == self._auth_lease_id
            if valid:
                self._release_auth_lease()
            self._write_frame({"id": self._next_id("lease"), "command": "auth_lease_released",
                "request_id": payload.get("request_id"), "ok": valid})
            return
        if event_name == "startup_error" and payload.get("code") == "sdk_missing":
            self._ready_info = {"startup_error": "sdk_missing"}
            self._ready_event.set()
            return
        if event_name == "ready":
            self._ready_info = {
                key: value
                for key, value in payload.items()
                if key in {"event", "protocol", "direct_enabled", "authenticated"}
            }
            self._ready_event.set()
            self._events.put(dict(self._ready_info))
            return
        if event_name == "credentials":
            credentials = payload.get("credentials")
            saved = False
            failure_code = "credential_save_failed"
            if isinstance(credentials, Mapping):
                try:
                    canonical = normalize_credentials(credentials)
                    current = normalize_credentials(self._credentials) if self._credentials else None
                    lease_id = payload.get("auth_lease_id")
                    if lease_id is not None:
                        if self._auth_lease is None or lease_id != self._auth_lease_id:
                            raise CredentialVaultError("Authentication lease is invalid")
                    elif canonical != current:
                        # An old child cannot rotate credentials without the lease protocol.
                        raise CredentialVaultError("Authentication lease is required")
                    persisted = self.credential_vault.save_if_unchanged(credentials, self._credential_revision)
                    self._credentials = dict(credentials)
                    self._credential_revision = persisted.revision
                    saved = True
                except CredentialVaultConflictError:
                    failure_code = "credential_vault_conflict"
                except CredentialVaultError:
                    saved = False
            event_id = str(payload.get("event_id") or payload.get("id") or self._next_id("credential"))
            try:
                # The ACK is deliberately sent only after DPAPI persistence.
                self._write_frame(
                    {
                        "id": self._next_id("ack"),
                        "command": "credentials_saved",
                        "type": "credentials_saved",
                        "ack_for": event_id,
                        "ok": saved,
                        "error_code": None if saved else failure_code,
                    }
                )
            except DirectBridgeError:
                pass
            safe_event = {key: value for key, value in payload.items() if key != "credentials"}
            safe_event["credentials_saved"] = saved
            if not saved:
                safe_event["credential_save_error"] = failure_code
            self._events.put(safe_event)
            return
        if event_name == "session":
            state = _safe_code(payload.get("state"), "session_event")
            if state in {"disconnected", "invalid_access_token", "login_rejected", "not_authenticated"}:
                # The child process remains usable. Mark only the SDK client
                # unauthenticated so the next read/send first logs in again;
                # an explicit token rejection on that login then invokes the
                # account re-registration callback.
                self._authenticated = False
        # Session/disconnect events never contain credentials.  Keep only a
        # bounded copy for GUI diagnostics.
        safe_event = {key: value for key, value in payload.items() if key not in _SENSITIVE_KEYS}
        self._events.put(safe_event)

    def drain_events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return queued protocol events without exposing credential payloads."""

        events: list[dict[str, Any]] = []
        while limit is None or len(events) < limit:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def _request(
        self,
        command: str,
        *,
        timeout: float | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        """Send one serialized request and return its result object."""

        self.start()
        request_id = self._next_id()
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        deadline = self.config.request_timeout if timeout is None else max(0.1, float(timeout))
        with self._request_lock:
            try:
                frame = {"id": request_id, "command": command, **arguments}
                with self._state_lock:
                    self._pending[request_id] = waiter
                    self._write_frame(frame)
                try:
                    response = waiter.get(timeout=deadline)
                except queue.Empty:
                    if command in _MUTATION_COMMANDS:
                        raise DirectSendUnknownError() from None
                    raise DirectBridgeTimeoutError() from None
            except DirectBridgeDisconnectedError as exc:
                if command in _MUTATION_COMMANDS:
                    raise DirectSendUnknownError(code=exc.code) from None
                if self._ready_info.get("startup_error") == "sdk_missing":
                    raise DirectBridgeError(SDK_MISSING_MESSAGE, code="sdk_missing") from None
                raise
            except DirectBridgeError:
                raise
            finally:
                with self._state_lock:
                    self._pending.pop(request_id, None)

        if response.get("ok", True) is False or "error" in response:
            error = response.get("error")
            code, status_code, ambiguous = _redact_protocol_error(error if isinstance(error, Mapping) else None)
            if command in _MUTATION_COMMANDS:
                if ambiguous:
                    raise DirectSendUnknownError(code=code, status_code=status_code) from None
                if (isinstance(error, Mapping) and code == "session_acquire_failed"
                        and error.get("send_phase") == "prewrite" and error.get("kind") == "explicit"):
                    raise DirectSendNotStartedError(
                        retryable=error.get("retryable_prewrite") is True,
                        cause_code=error.get("cause_code", "unclassified_session_error"),
                        cause_status_code=error.get("cause_status_code"),
                    ) from None
                raise DirectSendError(code=code, status_code=status_code) from None
            raise DirectBridgeError(code=code, message="Direct Kakao bridge request failed") from None
        result = response.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        if isinstance(result, list):
            # The production bridge wraps lists, but accepting a raw list here
            # keeps the protocol client tolerant of small test/adapter bridges.
            return {"value": result}
        return {key: value for key, value in response.items() if key not in {"id", "ok"}}

    def ready(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the bridge and opportunistically login from the DPAPI vault."""

        self.start()
        wait_timeout = self.config.ready_timeout if timeout is None else max(0.1, float(timeout))
        with self._ready_lock:
            if not self._ready_event.wait(wait_timeout):
                # The production Node bridge emits an unsolicited ready
                # event.  A small adapter/fake may instead implement ready as
                # a correlated request, so retain that protocol-compatible
                # fallback without weakening the timeout contract.
                try:
                    response = self._request("ready", timeout=min(2.0, wait_timeout))
                except DirectBridgeError:
                    raise DirectBridgeTimeoutError("Direct Kakao bridge did not become ready") from None
                self._ready_info = {
                    "event": "ready",
                    **{key: value for key, value in response.items() if key in {"protocol", "direct_enabled", "authenticated"}},
                }
                self._ready_event.set()
            if self._ready_info.get("startup_error") == "sdk_missing":
                raise DirectBridgeError(SDK_MISSING_MESSAGE, code="sdk_missing")
            if self._disconnected_event.is_set():
                raise DirectBridgeDisconnectedError()
            if self._credentials is None:
                try:
                    snapshot = self.credential_vault.load_snapshot()
                    self._credentials = snapshot.credentials
                    self._credential_revision = snapshot.revision
                except CredentialVaultError:
                    raise
            if self._credentials and not self._authenticated:
                try:
                    self.login(self._credentials)
                except DirectBridgeError as exc:
                    reloaded = self._reload_tablet_credentials(exc.code)
                    if reloaded is None:
                        raise
                    self.login(reloaded)
            elif self._credentials is None:
                reloaded = self._reload_tablet_credentials("credentials_missing")
                if reloaded is not None:
                    self.login(reloaded)
        return dict(self._ready_info)

    def login(self, credentials: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Login the tablet session; credential values never enter exceptions."""

        if credentials is None:
            credentials = self._credentials
        if credentials is None:
            snapshot = self.credential_vault.load_snapshot()
            credentials = snapshot.credentials
            self._credential_revision = snapshot.revision
        if credentials is None:
            raise DirectBridgeError("No saved Kakao credentials are available", code="credentials_missing")
        canonical = normalize_credentials(credentials)
        if self._credentials is None or normalize_credentials(self._credentials) != canonical:
            snapshot = self.credential_vault.load_snapshot()
            if snapshot.credentials is not None and normalize_credentials(snapshot.credentials) != canonical:
                raise DirectBridgeError(
                    "Credential vault changed before tablet login", code="credential_vault_conflict"
                )
            self._credential_revision = snapshot.revision
        self._credentials = canonical
        self.start()
        with self._state_lock:
            generation = self._process_generation
        result = self._request("login", credentials=canonical)
        with self._state_lock:
            if generation == self._process_generation and not self._disconnected_event.is_set():
                self._authenticated = bool(result.get("authenticated", True))
        return {key: value for key, value in result.items() if key not in _SENSITIVE_KEYS}

    def set_enabled(self, enabled: bool) -> None:
        """Update the opt-in switch and mirror it in an already-running bridge."""

        self.config.enabled = bool(enabled)
        if self.is_running and self._ready_event.is_set():
            self._request("set_enabled", enabled=self.config.enabled)

    def set_direct_enabled(self, enabled: bool) -> None:
        """Compatibility spelling used by the GUI toggle."""

        self.set_enabled(enabled)

    @property
    def direct_enabled(self) -> bool:
        return bool(self.config.enabled)

    def list_chats(
        self,
        *,
        search: str | None = None,
        all_chats: bool = True,
        resolve_titles: bool = True,
        resolve_peer_identities: bool = True,
    ) -> list[dict[str, Any]]:
        """List sanitized chat records; permitted even while direct send is off.

        Peer identity resolution is read-only but performs a consistency-
        checked member snapshot for each two-person DirectChat, so this one
        operation receives a larger bounded timeout than ordinary bridge
        commands.  Callers may disable it for a fast title-only list.
        """

        self.ready()
        result = self._request_read_with_auth_recovery(
            "list_chats",
            timeout=(
                max(self.config.request_timeout, PEER_IDENTITY_REQUEST_TIMEOUT)
                if resolve_peer_identities and all_chats and not search
                else self.config.request_timeout
            ),
            search=str(search) if search else None,
            all=bool(all_chats),
            resolve_titles=bool(resolve_titles),
            resolve_peer_identities=bool(resolve_peer_identities),
        )
        records = result.get("chats", result.get("value", result)) if isinstance(result, Mapping) else result
        if not isinstance(records, list):
            return []
        return [_sanitize_chat_record(item) for item in records if isinstance(item, Mapping)]

    def get_chats_by_ids(self, chat_ids: Any, *, resolve_friend_aliases: bool = False) -> list[dict[str, Any]]:
        """Fetch and sanitize a bounded set of stable chat IDs.

        This is a read-only recovery path for rooms omitted by ``list_chats``.
        The Node bridge independently obtains current CHATINFO and a stable
        member snapshot before peer identity fields can reach the caller.
        Invalid or oversized batches are rejected before the bridge starts.
        """

        if isinstance(chat_ids, (str, bytes, bytearray, Mapping)):
            raise ValueError("chat_ids must be an array")
        try:
            iterator = iter(chat_ids)
        except TypeError as exc:
            raise ValueError("chat_ids must be an array") from exc

        normalized: list[str] = []
        seen: set[str] = set()
        for raw_chat_id in iterator:
            chat_id = str(raw_chat_id).strip()
            if (
                not _POSITIVE_CHAT_ID_PATTERN.fullmatch(chat_id)
                or len(chat_id) > 32
            ):
                raise ValueError("chat_ids must contain positive numeric IDs")
            if chat_id in seen:
                continue
            seen.add(chat_id)
            normalized.append(chat_id)
            if len(normalized) > MAX_CHAT_LOOKUP_IDS:
                raise ValueError("too many chat IDs")

        if not normalized:
            return []

        self.ready()
        result = self._request_read_with_auth_recovery(
            "get_chats_by_ids",
            timeout=max(self.config.request_timeout, PEER_IDENTITY_REQUEST_TIMEOUT),
            chat_ids=normalized,
            **({"resolve_friend_aliases": True} if resolve_friend_aliases else {}),
        )
        records = result.get("chats", result.get("value", result)) if isinstance(result, Mapping) else result
        if not isinstance(records, list):
            return []
        return [_sanitize_chat_record(item) for item in records if isinstance(item, Mapping)]

    def resolve_chat_aliases(self, chat_ids: Any) -> list[dict[str, Any]]:
        """Reverify rooms and read exact saved friend names for unresolved targets."""
        return self.get_chats_by_ids(chat_ids, resolve_friend_aliases=True)

    def _resolve_send_chat_id(self, chat_name: str | None, chat_id: str | None) -> str:
        if chat_id is not None:
            resolved = normalize_chat_id(chat_id)
        else:
            candidate = str(chat_name or "").strip()
            if candidate in self.config.mappings.values() or (candidate.isdigit() and candidate not in self.config.mappings):
                resolved = normalize_chat_id(candidate)
            else:
                resolved = self.config.resolve_chat_id(candidate)
        # BSON Long.fromString wraps out-of-range values. Reject those IDs
        # before login, file upload, or WRITE so they cannot target another room.
        if not re.fullmatch(r"[1-9][0-9]{0,18}", resolved) or int(resolved) > 9223372036854775807:
            raise DirectSendError(code="invalid_chat_id")
        return resolved

    def _request_send_with_auth_recovery(self, command: str, **arguments: Any) -> dict[str, Any]:
        if command not in _MUTATION_COMMANDS:
            raise ValueError("send recovery requires a mutation command")
        try:
            return self._request(command, **arguments)
        except DirectSendNotStartedError as exc:
            reloaded = self._reload_tablet_credentials(exc.cause_code)
            if reloaded is None:
                raise
            # Only this structured error proves that no WRITE/POST occurred.
            # Re-authenticate and retry once; unknown outcomes never enter here.
            self.login(reloaded)
            return self._request(command, **arguments)

    def send_message(
        self,
        chat_name: str | None = None,
        text: str = "",
        *,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one text through the stable chat-ID mapping.

        ``chat_id`` is accepted for mapping UI actions that have already
        resolved a record; normal callers should provide the PC display name.
        """

        if not self.config.enabled:
            raise DirectSendDisabledError()
        resolved_chat_id = self._resolve_send_chat_id(chat_name, chat_id)
        if not isinstance(text, str) or not text:
            raise DirectSendError("Direct Kakao message text is empty", code="message_empty")
        self.ready()
        result = self._request_send_with_auth_recovery("send_message", chat_id=resolved_chat_id, text=text)
        if isinstance(result, Mapping) and result.get("success") is False:
            raw_status = result.get("status_code")
            status_code = raw_status if type(raw_status) is int else None
            if status_code in (None, -1, 0):
                raise DirectSendUnknownError(status_code=status_code) from None
            raise DirectSendError(status_code=status_code) from None
        return validate_direct_send_receipt(result, resolved_chat_id)

    @staticmethod
    def _validate_attachment_run_id(attachment_run_id: str) -> str:
        if not isinstance(attachment_run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attachment_run_id):
            raise DirectSendError(code="invalid_attachment_run_id")
        return attachment_run_id

    def prepare_attachments(
        self,
        *,
        attachments: tuple[Path, ...] | list[Path],
        attachment_run_id: str,
    ) -> dict[str, Any]:
        """Validate frozen run files and retain bounded, reusable media buffers.

        This command performs local file work only; it never logs in or sends.
        A reconnect can prepare the same immutable snapshot paths again.
        """
        if not self.config.enabled:
            raise DirectSendDisabledError()
        run_id = self._validate_attachment_run_id(attachment_run_id)
        paths = validate_direct_attachments(attachments)
        result = self._request(
            "prepare_attachments", timeout=max(self.config.request_timeout, 30.0),
            attachments=[str(path) for path in paths], attachment_run_id=run_id,
        )
        if (type(result.get("attachment_count")) is not int or result["attachment_count"] != len(paths)
                or type(result.get("total_bytes")) is not int
                or not 0 < result["total_bytes"] <= MAX_DIRECT_ATTACHMENT_TOTAL_BYTES):
            raise DirectBridgeError(code="invalid_attachment_preparation")
        return result

    def release_attachments(self, attachment_run_id: str) -> None:
        """Release one run's prepared buffers without starting a closed child."""
        run_id = self._validate_attachment_run_id(attachment_run_id)
        if self.is_running:
            self._request("release_attachments", timeout=min(self.config.request_timeout, 2.0),
                          attachment_run_id=run_id)

    def send_attachments(
        self,
        chat_name: str | None = None,
        text: str = "",
        *,
        chat_id: str | None = None,
        attachments: tuple[Path, ...] | list[Path],
        attachment_run_id: str | None = None,
        image_send_as: str = "photo",
    ) -> dict[str, Any]:
        """Send optional text then each attachment, once, through LOCO.

        An incomplete batch is always unknown, including a later explicit
        rejection after an earlier receipt. Callers must not replay that row.
        """
        if not self.config.enabled:
            raise DirectSendDisabledError()
        if not isinstance(text, str):
            raise DirectSendError(code="invalid_media_request")
        if image_send_as not in ("photo", "file"):
            raise DirectSendError(code="invalid_image_send_as")
        if attachment_run_id is not None:
            self._validate_attachment_run_id(attachment_run_id)
        paths = validate_direct_attachments(attachments)
        resolved_chat_id = self._resolve_send_chat_id(chat_name, chat_id)
        self.ready()
        request_timeout = max(self.config.request_timeout, 90.0 + 120.0 * len(paths))
        request_arguments = {
            "chat_id": resolved_chat_id,
            "text": text,
            "attachments": [str(path) for path in paths],
        }
        if attachment_run_id is not None:
            request_arguments["attachment_run_id"] = attachment_run_id
        if image_send_as != "photo":
            request_arguments["imageSendAs"] = image_send_as
        result = self._request_send_with_auth_recovery("send_attachments", timeout=request_timeout, **request_arguments)
        expected_kinds = (["text"] if text.strip() else []) + [
            "photo" if image_send_as == "photo" and path.suffix.lstrip(".").lower() in _DIRECT_PHOTO_EXTENSIONS else "file" for path in paths
        ]
        receipts = result.get("results")
        if (not isinstance(receipts, list) or len(receipts) != len(expected_kinds)
                or type(result.get("attachment_count")) is not int or result["attachment_count"] != len(paths)):
            raise DirectSendUnknownError(code="invalid_media_receipt")
        sanitized = []
        for receipt, kind in zip(receipts, expected_kinds):
            if not isinstance(receipt, Mapping) or receipt.get("kind") != kind:
                raise DirectSendUnknownError(code="invalid_media_receipt")
            sanitized.append({**_strict_media_receipt(receipt, resolved_chat_id), "kind": kind})
        if len({receipt["log_id"] for receipt in sanitized}) != len(sanitized):
            raise DirectSendUnknownError(code="duplicate_media_receipt")
        final = _strict_media_receipt(result, resolved_chat_id)
        if final != {key: value for key, value in sanitized[-1].items() if key != "kind"}:
            raise DirectSendUnknownError(code="invalid_media_receipt")
        return {**final, "attachment_count": len(paths), "results": sanitized}

    def shutdown(self) -> None:
        """Ask the child to close its client, then close the hidden process."""

        with self._state_lock:
            process = self._process
            if process is None:
                self._closed = True
                return
        try:
            if self._ready_event.is_set() and self.is_running:
                self._request("shutdown", timeout=1.0)
        except DirectBridgeError:
            pass
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except Exception:
                    pass
            thread = self._reader_thread
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
            with self._state_lock:
                self._auth_lease_cancel.set()
                if process.poll() is not None:
                    self._release_auth_lease()
                self._closed = True
                if process.poll() is not None:
                    self._process = None
                self._authenticated = False
            self._cleanup_sdk_config_dir()

    close = shutdown

    def __enter__(self) -> "KakaoDirectBridge":
        return self

    def __exit__(self, *_: Any) -> None:
        self.shutdown()


def _sanitize_chat_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the same safe chat shape expected from the Node bridge."""

    chat_id = str(record.get("chat_id", ""))
    chat_type = record.get("type")
    if not isinstance(chat_type, (str, int, float)) or isinstance(chat_type, bool):
        chat_type = "unknown"
    sanitized = {
        "chat_id": chat_id,
        "type": chat_type,
        "display_name": record.get("display_name") if isinstance(record.get("display_name"), (str, type(None))) else None,
        "title": record.get("title") if isinstance(record.get("title"), (str, type(None))) else None,
        "active_members": record.get("active_members") if isinstance(record.get("active_members"), int) else 0,
        "peer_user_id": None,
        "peer_names": [],
        "peer_local_names": [],
    }
    is_direct = str(chat_type).strip().casefold() == "directchat" and sanitized["active_members"] == 2
    peer_user_id = str(record.get("peer_user_id") or "").strip()
    raw_peer_names = record.get("peer_names")
    peer_names: list[str] = []
    if isinstance(raw_peer_names, list):
        for raw_name in raw_peer_names:
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if name and len(name) <= 100 and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
                if name not in peer_names:
                    peer_names.append(name)
    if is_direct and peer_user_id.isdigit() and not peer_user_id.startswith("0") and len(peer_names) == 1:
        sanitized["peer_user_id"] = peer_user_id
        sanitized["peer_names"] = peer_names
        raw_local_names = record.get("peer_local_names")
        peer_local_names: list[str] = []
        if isinstance(raw_local_names, list):
            for raw_name in raw_local_names:
                if not isinstance(raw_name, str):
                    continue
                name = raw_name.strip()
                if name and len(name) <= 100 and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
                    if name not in peer_local_names:
                        peer_local_names.append(name)
        if len(peer_local_names) == 1:
            sanitized["peer_local_names"] = peer_local_names
    return sanitized


def validate_direct_send_receipt(
    result: Any, chat_id: str, *, error_code: str = "invalid_send_receipt",
) -> dict[str, Any]:
    """Require a server message receipt; never invent missing success fields.

    A receipt confirms server acceptance, not recipient-device delivery or read.
    Missing fields after a mutation are unknown and must never be replayed.
    """
    if not isinstance(result, Mapping):
        raise DirectSendUnknownError(code=error_code)
    log_id = result.get("log_id")
    sent_at = result.get("sent_at")
    if (result.get("success") is not True or type(result.get("status_code")) is not int
            or result["status_code"] != 0 or result.get("chat_id") != chat_id
            or not isinstance(log_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", log_id)
            or int(log_id) > 9223372036854775807 or type(sent_at) not in (int, float)
            or not 0 < sent_at <= 9007199254740991 or not float(sent_at).is_integer()):
        raise DirectSendUnknownError(code=error_code)
    return {"success": True, "status_code": 0, "chat_id": chat_id, "log_id": log_id, "sent_at": sent_at}


def _strict_media_receipt(result: Mapping[str, Any], chat_id: str) -> dict[str, Any]:
    return validate_direct_send_receipt(result, chat_id, error_code="invalid_media_receipt")


__all__ = [
    "EXPECTED_SDK_VERSION",
    "DEFAULT_BRIDGE_SCRIPT",
    "DEFAULT_CREDENTIAL_VAULT",
    "DEFAULT_CREDENTIAL_VAULT_PATH",
    "DEFAULT_CONFIG_LOCK_POLL_INTERVAL",
    "DEFAULT_CONFIG_LOCK_TIMEOUT",
    "DEFAULT_REQUEST_TIMEOUT",
    "MAX_CHAT_LOOKUP_IDS",
    "MAX_DIRECT_ATTACHMENTS",
    "MAX_DIRECT_ATTACHMENT_BYTES",
    "MAX_DIRECT_ATTACHMENT_TOTAL_BYTES",
    "DIRECT_ATTACHMENT_EXTENSIONS",
    "PEER_IDENTITY_REQUEST_TIMEOUT",
    "DEFAULT_READY_TIMEOUT",
    "DEFAULT_SDK_ROOT",
    "DPAPI_ENTROPY",
    "CredentialVault",
    "CredentialVaultError",
    "CredentialVaultConflictError",
    "CredentialSnapshot",
    "DirectBridgeDisconnectedError",
    "DirectBridgeError",
    "DirectBridgeTimeoutError",
    "DirectConfigFileLock",
    "DirectConfigLockTimeout",
    "DirectKakaoConfig",
    "DirectSendDisabledError",
    "DirectSendError",
    "DirectSendNotStartedError",
    "DirectSendUnknownError",
    "DPAPIVault",
    "KakaoDirectBridge",
    "MappingStore",
    "UnmappedChatError",
    "WindowsCredentialVault",
    "atomic_write_json",
    "default_credential_vault_path",
    "discover_node_path",
    "discover_sdk_root",
    "direct_config_lock",
    "load_credentials",
    "load_direct_config",
    "load_mappings",
    "normalize_chat_id",
    "normalize_chat_name",
    "normalize_credentials",
    "normalize_guarded_mapping_ids",
    "normalize_mapping_labels",
    "normalize_mappings",
    "redact_error",
    "resolve_chat_id",
    "resolve_mapping",
    "save_credentials",
    "save_direct_config",
    "save_mappings",
    "validate_direct_attachments",
    "validate_direct_send_receipt",
]
