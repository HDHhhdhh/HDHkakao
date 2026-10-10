"""Durable, content-addressed native reservation receipts; this module has no UI.

Call ``mark_submitting`` and wait for it to return *before* pressing the native
reservation button. A crash, missing toast, or unverified receipt must never
turn an attempted reservation into a retry. All mutations commit atomically.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


STATES = frozenset({"prepared", "submitting", "acknowledged", "verified",
                    "rejected_before_submit", "unknown", "cancelled_by_user"})
RETRYABLE_STATES = frozenset({"prepared", "rejected_before_submit", "cancelled_by_user"})
_TRANSITIONS = {
    "prepared": {"submitting", "rejected_before_submit"},
    "rejected_before_submit": {"submitting", "rejected_before_submit"},
    "submitting": {"acknowledged", "verified", "unknown"},
    "acknowledged": {"verified", "unknown"},
    "unknown": {"acknowledged", "verified", "unknown"},
    "verified": set(),
    # Only an explicit user cancellation reconciliation creates this state;
    # neither a missing receipt nor an expired due time can enter it.
    "cancelled_by_user": {"submitting", "rejected_before_submit"},
}


class ScheduleJournalError(RuntimeError):
    """Fail-closed journal error; messages intentionally omit message contents."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_row(row: Any) -> dict[str, str]:
    """Accept a MessageRow or mapping, preserving exact target and body text.

    Datetimes use local, timezone-naive ISO representation, like KakaoSender's
    CSV parser. No whitespace, Unicode, or line-ending normalization is done.
    """
    try:
        get = row.__getitem__ if isinstance(row, Mapping) else lambda name: getattr(row, name)
        chat_name, text, scheduled_at = (get(name) for name in ("chat_name", "text", "scheduled_at"))
        if not isinstance(chat_name, str) or not chat_name or not isinstance(text, str) or not text:
            raise ValueError
        moment = scheduled_at if isinstance(scheduled_at, datetime) else datetime.fromisoformat(scheduled_at)
        if moment.tzinfo is not None:
            raise ValueError
        return {"chat_name": chat_name, "text": text, "scheduled_at": moment.isoformat()}
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ScheduleJournalError("예약 기록의 대상·본문·현지 예약 시각이 올바르지 않습니다.") from None


def reservation_key(row: Any) -> str:
    return hashlib.sha256(_json(canonical_row(row)).encode("utf-8")).hexdigest()


def _unique_rows(rows: Iterable[Any]) -> list[tuple[str, dict[str, str]]]:
    result, seen = [], set()
    for row in rows:
        payload = canonical_row(row)
        key = reservation_key(payload)
        if key in seen:
            raise ScheduleJournalError("같은 대상·본문·시각의 예약이 입력에 중복되어 있습니다.")
        seen.add(key)
        result.append((key, payload))
    return result


@dataclass(frozen=True)
class ScheduleRecord:
    key: str
    chat_name: str
    text: str
    scheduled_at: str
    state: str
    events: tuple[dict[str, Any], ...]

    @property
    def safe_to_submit(self) -> bool:
        return self.state in RETRYABLE_STATES

    @property
    def row(self) -> dict[str, str]:
        return {"chat_name": self.chat_name, "text": self.text, "scheduled_at": self.scheduled_at}


class ScheduleJournal:
    """One SQLite transaction per operation, with full-content integrity checks.

    Separate instances/processes serialize the submit boundary with SQLite's
    write lock. ``safe_to_submit`` is informational: ``mark_submitting`` checks
    eligibility again inside the same transaction that commits the attempt.
    Evidence may contain screenshot paths and actual observed field values.
    Stored evidence is never interpreted as an instruction or fabricated here.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            if existed and self.path.stat().st_size < 100:
                raise ScheduleJournalError("예약 기록이 비어 있거나 잘렸습니다. 자동 재등록을 중단합니다.")
            with self._connection(require_existing=existed) as db:
                db.execute("BEGIN IMMEDIATE")
                tables = {item[0] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not existed and not tables:
                    db.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("CREATE TABLE reservations (key TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, events TEXT NOT NULL)")
                    db.execute("INSERT INTO metadata VALUES ('version', '1')")
                    self._seal(db)
                self._validate(db)
                db.commit()
        except ScheduleJournalError:
            raise
        except (OSError, sqlite3.Error):
            raise ScheduleJournalError("예약 기록을 열지 못했습니다. 자동 재등록을 중단합니다.") from None

    @contextmanager
    def _connection(self, *, require_existing: bool = True) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            if require_existing and (not self.path.is_file() or self.path.stat().st_size < 100):
                raise ScheduleJournalError("예약 기록이 없거나 손상됐습니다. 자동 재등록을 중단합니다.")
            db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except ScheduleJournalError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise ScheduleJournalError("예약 기록 검증 또는 저장에 실패했습니다. 자동 재등록을 중단합니다.") from None
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    @staticmethod
    def _snapshot(db: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
        return list(db.execute("SELECT key, payload, state, events FROM reservations ORDER BY key"))

    @classmethod
    def _seal(cls, db: sqlite3.Connection) -> None:
        digest = hashlib.sha256(_json(cls._snapshot(db)).encode("utf-8")).hexdigest()
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot_sha256', ?)", (digest,))

    @classmethod
    def _validate(cls, db: sqlite3.Connection) -> None:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ScheduleJournalError("예약 기록 무결성 검사에 실패했습니다.")
        metadata = dict(db.execute("SELECT name, value FROM metadata"))
        snapshot = cls._snapshot(db)
        digest = hashlib.sha256(_json(snapshot).encode("utf-8")).hexdigest()
        if metadata.get("version") != "1" or metadata.get("snapshot_sha256") != digest:
            raise ScheduleJournalError("예약 기록 버전 또는 내용 검증에 실패했습니다.")
        for key, payload_json, state, events_json in snapshot:
            payload, events = json.loads(payload_json), json.loads(events_json)
            if (not isinstance(payload, dict) or canonical_row(payload) != payload
                    or reservation_key(payload) != key or state not in STATES
                    or not isinstance(events, list) or not events
                    or not all(isinstance(event, dict) and event.get("state") in STATES
                               and isinstance(event.get("evidence"), dict)
                               and isinstance(event.get("at"), str) for event in events)
                    or events[-1]["state"] != state):
                raise ScheduleJournalError("예약 행의 식별자 또는 상태 기록이 손상됐습니다.")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._validate(db)
            yield db
            self._seal(db)
            db.commit()

    @staticmethod
    def _insert(db: sqlite3.Connection, key: str, payload: dict[str, str]) -> None:
        event = {"at": _now(), "state": "prepared", "evidence": {}}
        db.execute("INSERT OR IGNORE INTO reservations VALUES (?, ?, 'prepared', ?)",
                   (key, _json(payload), _json([event])))

    @staticmethod
    def _record(raw: tuple[str, str, str, str]) -> ScheduleRecord:
        key, payload, state, events = raw
        return ScheduleRecord(key=key, **json.loads(payload), state=state, events=tuple(json.loads(events)))

    @classmethod
    def _read_record(cls, db: sqlite3.Connection, key: str) -> ScheduleRecord:
        raw = db.execute("SELECT key, payload, state, events FROM reservations WHERE key = ?", (key,)).fetchone()
        if raw is None:
            raise ScheduleJournalError("준비되지 않은 예약입니다. 원본 행을 먼저 기록해야 합니다.")
        return cls._record(raw)

    def prepare(self, rows: Iterable[Any]) -> tuple[ScheduleRecord, ...]:
        source = _unique_rows(rows)
        with self._transaction() as db:
            for key, payload in source:
                self._insert(db, key, payload)
            prepared = tuple(self._read_record(db, key) for key, _ in source)
        return prepared

    def records(self) -> tuple[ScheduleRecord, ...]:
        with self._connection() as db:
            db.execute("BEGIN")
            self._validate(db)
            return tuple(self._record(raw) for raw in db.execute(
                "SELECT key, payload, state, events FROM reservations ORDER BY rowid"))

    def get(self, row_or_key: Any) -> ScheduleRecord:
        key = row_or_key if isinstance(row_or_key, str) else reservation_key(row_or_key)
        with self._connection() as db:
            db.execute("BEGIN")
            self._validate(db)
            return self._read_record(db, key)

    @contextmanager
    def slot_accounting(self, *, write=False):
        """Share a validated snapshot and lock with the local slot counter.

        The counter may update its own table, never reservation receipts.
        Count claims and submitted rows together to admit concurrent attempts.
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._validate(db)
            records = tuple(self._record(raw) for raw in db.execute(
                "SELECT key, payload, state, events FROM reservations ORDER BY rowid"))
            yield db, records
            if write:
                db.commit()

    def safe_to_submit(self, row_or_key: Any) -> bool:
        return self.get(row_or_key).safe_to_submit

    def assert_safe_to_submit(self, row_or_key: Any) -> ScheduleRecord:
        record = self.get(row_or_key)
        if not record.safe_to_submit:
            raise ScheduleJournalError("이미 제출했거나 결과가 불명확한 예약입니다. 재등록하지 않습니다.")
        return record

    def pending(self, rows: Iterable[Any] | None = None) -> tuple[ScheduleRecord, ...]:
        records = {record.key: record for record in self.records()}
        keys = list(records) if rows is None else [key for key, _ in _unique_rows(rows)]
        if any(key not in records for key in keys):
            raise ScheduleJournalError("입력에 준비되지 않은 예약이 있습니다.")
        return tuple(records[key] for key in keys if records[key].safe_to_submit)

    def _mark(self, row_or_key: Any, state: str, evidence: Mapping[str, Any] | None) -> ScheduleRecord:
        key = row_or_key if isinstance(row_or_key, str) else reservation_key(row_or_key)
        try:
            evidence_copy = json.loads(_json(dict(evidence or {})))
        except (ValueError, TypeError):
            raise ScheduleJournalError("예약 증거는 JSON으로 저장 가능한 값이어야 합니다.") from None
        with self._transaction() as db:
            raw = db.execute("SELECT state, events FROM reservations WHERE key = ?", (key,)).fetchone()
            if raw is None or state not in _TRANSITIONS.get(raw[0], set()):
                raise ScheduleJournalError("허용되지 않은 예약 상태 변경입니다. 재등록하지 않습니다.")
            events = json.loads(raw[1])
            events.append({"at": _now(), "state": state, "evidence": evidence_copy})
            db.execute("UPDATE reservations SET state = ?, events = ? WHERE key = ?",
                       (state, _json(events), key))
            updated = self._read_record(db, key)
        return updated

    def mark_submitting(self, row_or_key: Any, *, evidence: Mapping[str, Any] | None = None) -> ScheduleRecord:
        return self._mark(row_or_key, "submitting", evidence)

    def mark_acknowledged(self, row_or_key: Any, *, evidence: Mapping[str, Any] | None = None) -> ScheduleRecord:
        return self._mark(row_or_key, "acknowledged", evidence)

    def mark_verified(self, row_or_key: Any, *, evidence: Mapping[str, Any] | None = None) -> ScheduleRecord:
        return self._mark(row_or_key, "verified", evidence)

    def mark_unknown(self, row_or_key: Any, *, evidence: Mapping[str, Any] | None = None) -> ScheduleRecord:
        return self._mark(row_or_key, "unknown", evidence)

    def mark_rejected_before_submit(self, row_or_key: Any, *, evidence: Mapping[str, Any] | None = None) -> ScheduleRecord:
        return self._mark(row_or_key, "rejected_before_submit", evidence)

    def import_prior_run(self, rows_path: str | Path, results_path: str | Path) -> tuple[ScheduleRecord, ...]:
        """Import immutable source rows plus their native CUA receipts atomically.

        Historical results omit body text. Their source index selects a body
        only within the accompanying immutable source file; result target,
        time, and explicit exact-body evidence must match. The final identity
        and all joins against a current CSV always use the full content hash.
        """
        rows_path, results_path = Path(rows_path), Path(results_path)
        try:
            rows_bytes, results_bytes = rows_path.read_bytes(), results_path.read_bytes()
            rows, results = json.loads(rows_bytes), json.loads(results_bytes)
            if not isinstance(rows, list) or not isinstance(results, list):
                raise ValueError
            source = _unique_rows(rows)
            indexed = {}
            for raw, (key, payload) in zip(rows, source):
                index = raw["index"]
                if isinstance(index, bool) or not isinstance(index, int) or index < 1 or index in indexed:
                    raise ValueError
                indexed[index] = key, payload
            imported, seen = [], set()
            for result in results:
                index = result["index"]
                if isinstance(index, bool) or not isinstance(index, int) or index in seen:
                    raise ValueError
                seen.add(index)
                key, payload = indexed[index]
                if result.get("chat_name") != payload["chat_name"]:
                    raise ValueError
                comparison = {**payload, "scheduled_at": result["scheduled_at"]}
                if canonical_row(comparison) != payload or ("text" in result and result["text"] != payload["text"]):
                    raise ValueError
                state = {"registered_verified": "verified", "registered_acknowledged": "acknowledged"}[result["status"]]
                body_flag = "body_exact_after_registration" if state == "verified" else "body_exact_before_registration"
                if result.get(body_flag) is not True:
                    raise ValueError
                screenshots = []
                for name, seq in result.items():
                    if name.endswith("_seq") and isinstance(seq, int) and not isinstance(seq, bool):
                        screenshots.extend(str(path.resolve()) for path in sorted(
                            (results_path.parent / "screenshots").glob(f"{seq:05d}-*.png")))
                evidence = {"source_rows": str(rows_path.resolve()),
                            "source_results": str(results_path.resolve()),
                            "source_rows_sha256": hashlib.sha256(rows_bytes).hexdigest(),
                            "source_results_sha256": hashlib.sha256(results_bytes).hexdigest(),
                            "source_index": index, "receipt": result,
                            "screenshots": screenshots}
                imported.append((key, state, evidence))
        except ScheduleJournalError:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ScheduleJournalError("이전 예약 기록이 원본 내용과 일치하지 않습니다. 가져오기를 중단합니다.") from None
        with self._transaction() as db:
            for key, payload in source:
                self._insert(db, key, payload)
            for key, state, evidence in imported:
                previous, events_json = db.execute("SELECT state, events FROM reservations WHERE key = ?", (key,)).fetchone()
                # Repeated imports must neither downgrade verified receipts nor
                # discard an existing unknown/attempt record's history.
                if previous == "verified" or (previous == "acknowledged" and state == "acknowledged"):
                    continue
                events = json.loads(events_json)
                events.append({"at": _now(), "state": state, "evidence": evidence})
                db.execute("UPDATE reservations SET state = ?, events = ? WHERE key = ?", (state, _json(events), key))
            records = tuple(self._read_record(db, key) for key, _, _ in imported)
        return records
