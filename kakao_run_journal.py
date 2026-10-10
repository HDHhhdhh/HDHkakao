"""Durable, Tk-independent send history with conservative crash recovery.

Call mark_attempt *before* crossing a transport's irreversible boundary. A
missing result for a committed attempt always recovers as unknown, never as a
retry. Recovery only describes work: this module cannot send messages.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from kakao_sender import MessageRow


class RunJournalError(RuntimeError):
    """Safe diagnostic which never includes a database or message payload."""


def default_journal_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "KakaoSender" / "run-journal.sqlite3"


class _PayloadProtection:
    """DPAPI CurrentUser on Windows; private filesystem storage on POSIX."""

    entropy = b"KakaoTalkHub.RunJournal.v1"

    def protect(self, plaintext: bytes) -> bytes:
        if os.name != "nt":
            return b"private-file-v1\0" + plaintext
        try:
            import win32crypt
            result = win32crypt.CryptProtectData(plaintext, None, self.entropy, None, None, 0)
            return b"dpapi-v1\0" + bytes(result[-1] if isinstance(result, tuple) else result)
        except Exception:
            raise RunJournalError("실행 기록을 암호화하지 못했습니다. 발송을 시작하지 않았습니다.") from None

    def unprotect(self, payload: bytes) -> bytes:
        try:
            if payload.startswith(b"dpapi-v1\0") and os.name == "nt":
                import win32crypt
                result = win32crypt.CryptUnprotectData(payload[9:], self.entropy, None, None, 0)
                return bytes(result[-1] if isinstance(result, tuple) else result)
            if payload.startswith(b"private-file-v1\0") and os.name != "nt":
                return payload[16:]
        except Exception:
            pass
        raise RunJournalError("실행 기록을 복호화하지 못했습니다. 기록을 만든 Windows 계정인지 확인하세요.")


_METADATA_KEYS = frozenset({
    "send_mode", "direct_enabled", "common_schedule", "routes", "source_kind",
    "parent_run_id", "recovery_of", "continue_on_error", "confirm_each",
    "recovery_row_indexes", "recovery_sources",
})
_SECRET_KEY_PARTS = ("password", "credential", "token", "secret", "cookie", "authorization")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_identity(pid: int) -> tuple[str, str | None]:
    """Return alive/dead/unknown plus an OS creation marker, never only a PID.

    Failure to query another account's process is unknown, not proof that its
    send stopped. Windows creation FILETIME and Linux boot ID/start ticks make
    PID reuse distinguishable from the process that opened the run.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.GetProcessTimes.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ("dead", None) if ctypes.get_last_error() in {87, 1168} else ("unknown", None)
            try:
                exit_code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return "unknown", None
                if exit_code.value != 259:  # STILL_ACTIVE
                    return "dead", None
                creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return "unknown", None
                ticks = creation.dwLowDateTime | (creation.dwHighDateTime << 32)
                return "alive", f"windows-filetime:{ticks}"
            finally:
                kernel.CloseHandle(handle)
        except Exception:
            return "unknown", None
    try:
        # /proc stat field 2 can contain spaces or parentheses; split after its
        # final closing parenthesis, then select field 22 (starttime).
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = content[content.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return "dead", None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return "alive", f"linux-start:{boot_id}:{fields[19]}"
    except FileNotFoundError:
        return ("dead", None) if Path("/proc").is_dir() else ("unknown", None)
    except Exception:
        return "unknown", None


def _owner_is_active(status: str, pid: int | None, marker: str | None) -> bool:
    if status != "running":
        return False
    if pid is None or not marker:
        # Version 1 never recorded ownership. Do not guess that a legacy run
        # is abandoned; it remains inspectable and can be explicitly finished.
        return True
    state, current_marker = _process_identity(pid)
    if state == "dead":
        return False
    if state == "unknown" or current_marker is None:
        return True
    return current_marker == marker


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or any(part in key.lower() for part in _SECRET_KEY_PARTS):
                raise RunJournalError("실행 기록에는 인증 설정을 저장할 수 없습니다.")
            _reject_secret_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secret_keys(nested)


def _serialize_row(index: int, row: MessageRow) -> dict[str, Any]:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise RunJournalError("실행 기록의 행 식별자가 올바르지 않습니다.")
    return {
        "index": index, "row_number": row.row_number, "chat_name": row.chat_name,
        "text": row.text, "attachments": [str(path) for path in row.attachments],
        "image_bundle": row.image_bundle,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at is not None else None,
    }


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    created_at: str
    updated_at: str
    status: str
    acknowledged: bool
    row_count: int
    result_count: int
    fingerprint: str
    owner_pid: int | None = None
    owner_start: str | None = None
    is_active: bool = False


@dataclass(frozen=True)
class RecoveryRun:
    summary: RunSummary
    selected: tuple[tuple[int, MessageRow], ...]
    source_path: str | None
    metadata: dict[str, Any]
    results: tuple[dict[str, Any], ...]
    pending_row_indexes: tuple[int, ...]
    failed_row_indexes: tuple[int, ...]
    retryable_row_indexes: tuple[int, ...]

    @property
    def run_id(self) -> str:
        return self.summary.run_id

    @property
    def is_active(self) -> bool:
        return self.summary.is_active

    @property
    def resumable_row_indexes(self) -> tuple[int, ...]:
        if self.is_active:
            return ()
        eligible = set(self.pending_row_indexes) | set(self.retryable_row_indexes)
        eligible.update(record["row_index"] for record in self.results if record["status"] == "stopped")
        return tuple(index for index, _row in self.selected if index in eligible)


@dataclass(frozen=True)
class JournalRun:
    journal: "RunJournal"
    run_id: str

    def mark_attempt(self, row_index: int, route: str) -> None:
        self.journal.mark_attempt(self.run_id, row_index, route)

    def add_result(self, record: Mapping[str, Any]) -> None:
        self.journal.add_result(self.run_id, record)

    def finish(self, status: str = "completed") -> None:
        self.journal.finish(self.run_id, status)


class RunJournal:
    """A transaction per operation; safe to share across worker/UI threads.

    payload_protection is an explicit test seam, never an automatic plaintext
    fallback. Production callers should leave it omitted.
    """

    def __init__(self, path: str | Path | None = None, *, payload_protection: Any = None) -> None:
        self.path = Path(path) if path is not None else default_journal_path()
        self._protection = payload_protection or _PayloadProtection()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(self.path.parent, 0o700)
            if self.path.exists() and self.path.stat().st_size < 100:
                raise RunJournalError("실행 기록 파일이 비어 있거나 잘렸습니다. 원본을 보존하고 복구를 확인하세요.")
            with self._connection() as db:
                check = db.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RunJournalError("실행 기록이 손상되었습니다. 원본 기록을 보존하고 복구를 확인하세요.")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise RunJournalError("이 버전에서 읽을 수 없는 실행 기록입니다.")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                expected_tables = {"runs", "attempts", "results"} | ({"recovery_claims"} if version == 3 else set())
                if version in (1, 2, 3) and not expected_tables.issubset(tables):
                    raise RunJournalError("실행 기록 테이블이 손상되었습니다. 원본을 보존하고 복구를 확인하세요.")
                if version == 0 and tables:
                    raise RunJournalError("지원하지 않는 실행 기록 저장소입니다.")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL, status TEXT NOT NULL,
                        acknowledged INTEGER NOT NULL DEFAULT 0,
                        row_count INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                        document BLOB NOT NULL, owner_pid INTEGER, owner_start TEXT
                    );
                    CREATE TABLE IF NOT EXISTS attempts (
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        row_index INTEGER NOT NULL, route TEXT NOT NULL,
                        attempted_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, row_index)
                    );
                    CREATE TABLE IF NOT EXISTS results (
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        row_index INTEGER NOT NULL, recorded_at TEXT NOT NULL,
                        payload BLOB NOT NULL,
                        PRIMARY KEY (run_id, row_index)
                    );
                    CREATE TABLE IF NOT EXISTS recovery_claims (
                        parent_run_id TEXT NOT NULL REFERENCES runs(run_id),
                        parent_row_index INTEGER NOT NULL,
                        child_run_id TEXT NOT NULL REFERENCES runs(run_id),
                        child_row_index INTEGER NOT NULL,
                        claimed_at TEXT NOT NULL,
                        PRIMARY KEY (parent_run_id, parent_row_index),
                        UNIQUE (child_run_id, child_row_index)
                    );
                """)
                if version == 1:
                    db.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER")
                    db.execute("ALTER TABLE runs ADD COLUMN owner_start TEXT")
                db.execute("PRAGMA user_version=3")
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except RunJournalError:
            raise
        except Exception:
            raise RunJournalError("실행 기록 저장소를 열지 못했습니다. 파일 손상 또는 접근 권한을 확인하세요.") from None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        except RunJournalError:
            raise
        except Exception:
            raise RunJournalError("실행 기록을 안전하게 읽거나 저장하지 못했습니다. 발송을 중단하고 저장소를 확인하세요.") from None
        finally:
            if db is not None:
                db.close()

    def _encode(self, value: Any) -> bytes:
        try:
            return self._protection.protect(_json(value))
        except RunJournalError:
            raise
        except Exception:
            raise RunJournalError("실행 기록 내용을 저장하지 못했습니다. 발송을 중단하세요.") from None

    def _decode(self, blob: bytes) -> Any:
        try:
            return json.loads(self._protection.unprotect(bytes(blob)).decode("utf-8"))
        except RunJournalError:
            raise
        except Exception:
            raise RunJournalError("실행 기록 내용이 손상되어 안전하게 복구할 수 없습니다.") from None

    def begin_run(
        self, run_id: str, selected: Sequence[tuple[int, MessageRow]],
        source_path: str | Path | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> JournalRun:
        """Commit a run and any permanent recovery claims before dispatch.

        metadata['recovery_sources'] maps a subset of string child indexes to
        {'run_id': parent_id, 'row_index': parent_index}. Unmapped child rows
        are new work. When present, this mapping takes precedence over legacy
        recovery_of/recovery_row_indexes (whose exact-coverage API remains).
        """
        run_id = str(run_id)
        if not run_id or len(run_id) > 160:
            raise RunJournalError("실행 기록 ID가 올바르지 않습니다.")
        metadata = dict(metadata or {})
        _reject_secret_keys(metadata)
        if set(metadata) - _METADATA_KEYS:
            raise RunJournalError("실행 기록에는 발송 경로 설정만 저장할 수 있습니다.")
        rows = [_serialize_row(index, row) for index, row in selected]
        if len({row["index"] for row in rows}) != len(rows):
            raise RunJournalError("실행 기록에 중복 행 식별자가 있습니다.")
        document = {"rows": rows, "source_path": str(source_path) if source_path is not None else None, "metadata": metadata}
        fingerprint = hashlib.sha256(_json(rows)).hexdigest()
        protected = self._encode(document)
        stamp = _now()
        owner_pid = os.getpid()
        state, owner_start = _process_identity(owner_pid)
        if state != "alive" or owner_start is None:
            raise RunJournalError("실행 기록의 소유 프로세스를 확인하지 못했습니다. 발송을 시작하지 않았습니다.")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            claims = self._validate_recovery_claim(db, run_id, rows, metadata)
            db.execute("INSERT INTO runs VALUES (?, ?, ?, 'running', 0, ?, ?, ?, ?, ?)",
                       (run_id, stamp, stamp, len(rows), fingerprint, protected, owner_pid, owner_start))
            for parent_run_id, parent_index, child_index in claims:
                db.execute("INSERT INTO recovery_claims VALUES (?, ?, ?, ?, ?)",
                           (parent_run_id, parent_index, run_id, child_index, stamp))
        return JournalRun(self, run_id)

    def _validate_recovery_claim(
        self, db: sqlite3.Connection, child_run_id: str, rows: list[dict[str, Any]], metadata: dict[str, Any],
    ) -> tuple[tuple[str, int, int], ...]:
        """Validate and reserve recovery ownership inside the begin transaction.

        Once committed, only the child is eligible for subsequent recovery,
        even when it exits before dispatch. The parent can never replay a row.
        """
        child_indexes = {str(row["index"]) for row in rows}
        if "recovery_sources" in metadata:
            sources = metadata["recovery_sources"]
            if not isinstance(sources, dict) or not set(sources).issubset(child_indexes):
                raise RunJournalError("복구 명단과 원본 행 연결 정보가 다릅니다.")
        else:
            parent_id = metadata.get("recovery_of")
            mapping = metadata.get("recovery_row_indexes")
            if not parent_id:
                if mapping:
                    raise RunJournalError("복구할 원본 실행 기록이 지정되지 않았습니다.")
                return ()
            if not isinstance(mapping, dict) or set(mapping) != child_indexes:
                raise RunJournalError("복구 명단과 원본 행 연결 정보가 다릅니다.")
            sources = {child: {"run_id": parent_id, "row_index": original} for child, original in mapping.items()}
        normalized = []
        seen: set[tuple[str, int]] = set()
        for child_index, source in sources.items():
            if not isinstance(source, dict) or set(source) != {"run_id", "row_index"}:
                raise RunJournalError("복구 기록의 원본 행 연결 정보가 올바르지 않습니다.")
            parent_id, parent_index = source["run_id"], source["row_index"]
            if not isinstance(parent_id, str) or not parent_id or parent_id == child_run_id:
                raise RunJournalError("복구 기록의 원본 실행 식별자가 올바르지 않습니다.")
            if isinstance(parent_index, bool) or not isinstance(parent_index, int) or parent_index < 0:
                raise RunJournalError("복구할 원본 행 식별자가 올바르지 않습니다.")
            pair = (parent_id, parent_index)
            if pair in seen:
                raise RunJournalError("같은 원본 행을 한 실행에서 여러 번 복구할 수 없습니다.")
            seen.add(pair)
            normalized.append((parent_id, parent_index, int(child_index)))
        parent_rows: dict[str, set[int]] = {}
        for parent_id, parent_index, child_index in normalized:
            if parent_id not in parent_rows:
                parent = db.execute("SELECT * FROM runs WHERE run_id=?", (parent_id,)).fetchone()
                if parent is None:
                    raise RunJournalError("복구할 원본 실행 기록을 찾지 못했습니다.")
                if _owner_is_active(parent["status"], parent["owner_pid"], parent["owner_start"]):
                    raise RunJournalError("원본 작업이 아직 실행 중입니다. 작업 종료 후 복구하세요.")
                document = self._decode(parent["document"])
                try:
                    source_rows = document["rows"]
                    if hashlib.sha256(_json(source_rows)).hexdigest() != parent["fingerprint"]:
                        raise ValueError("invalid fingerprint")
                    parent_rows[parent_id] = {item["index"] for item in source_rows}
                except Exception:
                    raise RunJournalError("원본 실행 명단이 손상되어 복구를 시작할 수 없습니다.") from None
            if parent_index not in parent_rows[parent_id]:
                raise RunJournalError("복구할 행이 원본 실행 명단에 없습니다.")
            if db.execute("SELECT 1 FROM recovery_claims WHERE parent_run_id=? AND parent_row_index=?", (parent_id, parent_index)).fetchone():
                raise RunJournalError("이 행은 다른 복구 실행으로 이미 넘겼습니다. 해당 실행 기록에서 이어서 진행하세요.")
            record = db.execute("SELECT payload FROM results WHERE run_id=? AND row_index=?", (parent_id, parent_index)).fetchone()
            if record is not None:
                result = self._decode(record["payload"])
                eligible = isinstance(result, dict) and result.get("row_index") == parent_index and (
                    result.get("status") == "stopped" or
                    (result.get("status") == "error" and result.get("retry_transport") == "macro")
                )
            else:
                eligible = not db.execute("SELECT 1 FROM attempts WHERE run_id=? AND row_index=?", (parent_id, parent_index)).fetchone()
            if not eligible:
                raise RunJournalError("성공했거나 발송 여부가 불확실한 행은 복구 발송 대상으로 사용할 수 없습니다.")
        return tuple(normalized)

    def _require_row(self, db: sqlite3.Connection, run_id: str, row_index: int) -> None:
        run = db.execute("SELECT status, document FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["status"] != "running":
            raise RunJournalError("종료되었거나 없는 실행 기록에는 발송을 추가할 수 없습니다.")
        doc = self._decode(run["document"])
        if row_index not in {row["index"] for row in doc["rows"]}:
            raise RunJournalError("발송 행이 저장된 실행 명단과 다릅니다.")

    def mark_attempt(self, run_id: str, row_index: int, route: str) -> None:
        if route not in {"direct", "macro", "macro_retry", "scheduled"}:
            raise RunJournalError("실행 기록의 발송 경로가 올바르지 않습니다.")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_row(db, run_id, row_index)
            if db.execute("SELECT 1 FROM results WHERE run_id=? AND row_index=?", (run_id, row_index)).fetchone():
                raise RunJournalError("이미 처리한 행은 같은 실행에서 다시 발송할 수 없습니다.")
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?)", (run_id, row_index, route, _now()))
            db.execute("UPDATE runs SET updated_at=?, acknowledged=0 WHERE run_id=?", (_now(), run_id))

    def add_result(self, run_id: str, record: Mapping[str, Any]) -> None:
        record = dict(record)
        _reject_secret_keys(record)
        row_index = record.get("row_index")
        if isinstance(row_index, bool) or not isinstance(row_index, int) or not isinstance(record.get("status"), str):
            raise RunJournalError("실행 결과의 행 또는 상태가 올바르지 않습니다.")
        protected = self._encode(record)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_row(db, run_id, row_index)
            existing = db.execute("SELECT payload FROM results WHERE run_id=? AND row_index=?", (run_id, row_index)).fetchone()
            if existing is not None:
                if self._decode(existing["payload"]) == record:
                    return
                raise RunJournalError("이미 저장한 실행 결과를 다른 상태로 덮어쓸 수 없습니다.")
            db.execute("INSERT INTO results VALUES (?, ?, ?, ?)", (run_id, row_index, _now(), protected))
            db.execute("UPDATE runs SET updated_at=?, acknowledged=0 WHERE run_id=?", (_now(), run_id))

    def finish(self, run_id: str, status: str = "completed") -> None:
        if status not in {"completed", "cancelled", "failed"}:
            raise RunJournalError("실행 기록의 종료 상태가 올바르지 않습니다.")
        with self._connection() as db:
            result = db.execute("UPDATE runs SET status=?, updated_at=?, acknowledged=0 WHERE run_id=?", (status, _now(), run_id))
            if result.rowcount != 1:
                raise RunJournalError("종료할 실행 기록을 찾지 못했습니다.")

    def acknowledge(self, run_id: str, acknowledged: bool = True) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status, owner_pid, owner_start FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise RunJournalError("확인할 실행 기록을 찾지 못했습니다.")
            if acknowledged and _owner_is_active(run["status"], run["owner_pid"], run["owner_start"]):
                raise RunJournalError("아직 실행 중인 기록은 확인 완료로 숨길 수 없습니다. 작업 종료 후 결과를 확인하세요.")
            result = db.execute("UPDATE runs SET acknowledged=? WHERE run_id=?", (int(acknowledged), run_id))
            if result.rowcount != 1:
                raise RunJournalError("확인할 실행 기록을 찾지 못했습니다.")

    @staticmethod
    def _summary(row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            row["run_id"], row["created_at"], row["updated_at"], row["status"],
            bool(row["acknowledged"]), row["row_count"], row["result_count"], row["fingerprint"],
            row["owner_pid"], row["owner_start"],
            _owner_is_active(row["status"], row["owner_pid"], row["owner_start"]),
        )

    def list_runs(self) -> list[RunSummary]:
        with self._connection() as db:
            return [self._summary(row) for row in db.execute("""
                SELECT runs.*, (SELECT COUNT(*) FROM results WHERE results.run_id=runs.run_id) AS result_count
                FROM runs ORDER BY created_at DESC, run_id
            """)]

    def list_recoverable_runs(self) -> list[RunSummary]:
        recoverable = []
        for summary in self.list_runs():
            if summary.acknowledged or summary.is_active:
                continue
            recovery = self.load_recovery(summary.run_id)
            if recovery.pending_row_indexes or any(
                item["status"] in {"error", "unknown", "stopped"} for item in recovery.results
            ):
                recoverable.append(summary)
        return recoverable

    def load_recovery(self, run_id: str) -> RecoveryRun:
        with self._connection() as db:
            run = db.execute("""SELECT runs.*,
                (SELECT COUNT(*) FROM results WHERE results.run_id=runs.run_id) AS result_count
                FROM runs WHERE run_id=?""", (run_id,)).fetchone()
            if run is None:
                raise RunJournalError("복구할 실행 기록을 찾지 못했습니다.")
            doc = self._decode(run["document"])
            try:
                serialized = doc["rows"]
                if hashlib.sha256(_json(serialized)).hexdigest() != run["fingerprint"]:
                    raise ValueError("document fingerprint mismatch")
                selected = tuple((item["index"], MessageRow(
                    item["row_number"], item["chat_name"], item["text"],
                    tuple(Path(path) for path in item["attachments"]), item["image_bundle"],
                    datetime.fromisoformat(item["scheduled_at"]) if item["scheduled_at"] else None,
                )) for item in serialized)
                records = {item["row_index"]: self._decode(item["payload"]) for item in
                           db.execute("SELECT row_index, payload FROM results WHERE run_id=?", (run_id,))}
                attempts = {item["row_index"]: item["route"] for item in
                            db.execute("SELECT row_index, route FROM attempts WHERE run_id=?", (run_id,))}
                claims = {item["parent_row_index"]: item for item in db.execute(
                    "SELECT parent_row_index, child_run_id, child_row_index FROM recovery_claims WHERE parent_run_id=?", (run_id,))}
                pending = []
                for index, row in selected:
                    if index in claims:
                        claim = claims[index]
                        records[index] = {
                            "row_index": index, "row": row.row_number, "chat_name": row.chat_name,
                            "status": "transferred", "retry_transport": "none", "failure_stage": "recovery_transfer",
                            "detail": "다른 복구 실행으로 넘긴 행입니다. 해당 실행 기록에서 확인하세요.",
                            "child_run_id": claim["child_run_id"], "child_row_index": claim["child_row_index"],
                        }
                    if index in records:
                        if records[index].get("row_index") != index or not isinstance(records[index].get("status"), str):
                            raise ValueError("invalid result payload")
                        continue
                    if index in attempts:
                        records[index] = {
                            "row_index": index, "row": row.row_number, "chat_name": row.chat_name,
                            "status": "unknown", "detail": "발송 시도 기록 후 최종 결과가 없습니다. 대화방에서 먼저 확인하세요.",
                            "transport": attempts[index], "failure_stage": "interrupted_attempt",
                            "retry_transport": "none", "attachments": row.attachment_count,
                            "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else "",
                        }
                    else:
                        pending.append(index)
                ordered = tuple(records[index] for index, _row in selected if index in records)
                failed = tuple(item["row_index"] for item in ordered if item["status"] == "error")
                retryable = tuple(item["row_index"] for item in ordered if item["status"] == "error" and (
                    item.get("retry_transport") in {"direct", "macro", "macro_retry"} or item.get("retry_safe") is True
                ))
                return RecoveryRun(self._summary(run), selected, doc["source_path"], dict(doc["metadata"]),
                                   ordered, tuple(pending), failed, retryable)
            except RunJournalError:
                raise
            except Exception:
                raise RunJournalError("실행 기록 내용이 손상되어 안전하게 복구할 수 없습니다.") from None
