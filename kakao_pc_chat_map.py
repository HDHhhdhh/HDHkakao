"""Read-only PC KakaoTalk window geometry to stable chat-ID resolver.

KakaoTalk keeps window placement metadata in ``chatWindowUi.db`` and names
its local chat databases ``chatLogs_<chatId>.edb``.  This module compares
only those metadata identifiers with the chat IDs reported by the tablet
session.  It never opens a chat log database, reads a message, moves a
window, or writes to KakaoTalk data.

Typical integration::

    discovery = discover_pc_chat_map(tablet_chat_ids)
    if discovery.provider is not None:
        result = discovery.provider.resolve_window_rect(left, top, width, height)
        if result.resolved:
            stable_chat_id = result.chat_id

``repr(...)`` and ``to_safe_dict()`` deliberately omit paths and chat IDs so
the result/status objects can be placed in application diagnostics safely.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Iterator


DEFAULT_POSITION_TOLERANCE_PX = 8
DEFAULT_SIZE_TOLERANCE_PX = 16
MAX_POSITION_TOLERANCE_PX = 16
MAX_SIZE_TOLERANCE_PX = 32
DEFAULT_SQLITE_TIMEOUT_SECONDS = 0.25
MAX_WINDOW_CHAT_ID_CANDIDATES = 512

_CHAT_LOG_NAME = re.compile(r"^chatLogs_(.+)\.edb$", re.IGNORECASE)
_POSITIVE_NUMERIC_CHAT_ID = re.compile(r"^[1-9]\d*$")
_WINDOW_COLUMNS = (
    "chatId",
    "windowsizeWidth",
    "windowsizeHeight",
    "windowsposLeft",
    "windowsposTop",
)


class DiscoveryCode(str, Enum):
    """Safe account-discovery outcomes."""

    READY = "ready"
    NO_TABLET_IDS = "no_tablet_ids"
    USERS_ROOT_UNAVAILABLE = "users_root_unavailable"
    NO_ACCOUNT_DATABASE = "no_account_database"
    NO_READABLE_DATABASE = "no_readable_database"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS_ACCOUNT = "ambiguous_account"


class ResolutionCode(str, Enum):
    """Safe geometry-resolution outcomes."""

    RESOLVED = "resolved"
    INVALID_RECT = "invalid_rect"
    DATABASE_UNAVAILABLE = "database_unavailable"
    DATABASE_BUSY = "database_busy"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    NO_MATCH = "no_match"
    GEOMETRY_COLLISION = "geometry_collision"
    TABLET_MEMBERSHIP_MISS = "tablet_membership_miss"


class MatchKind(str, Enum):
    NONE = "none"
    EXACT = "exact"
    TOLERANCE = "tolerance"


@dataclass(frozen=True, slots=True)
class AccountSelectionPolicy:
    """Confidence gates for selecting one active KakaoTalk account.

    Tablet coverage must pass.  PC coverage is intentionally diagnostic only
    because historical PC chat logs can greatly outnumber tablet chat rows.
    """

    minimum_overlap: int = 1
    minimum_tablet_coverage: float = 0.80

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_overlap, bool)
            or not isinstance(self.minimum_overlap, int)
            or self.minimum_overlap < 1
        ):
            raise ValueError("minimum_overlap must be a positive integer")
        coverage = self.minimum_tablet_coverage
        if (
            isinstance(coverage, bool)
            or not isinstance(coverage, (int, float))
            or not 0.0 < float(coverage) <= 1.0
        ):
            raise ValueError("minimum_tablet_coverage must be greater than 0 and at most 1")


@dataclass(frozen=True, slots=True)
class GeometryTolerance:
    """Explicit, bounded tolerance for Win32 invisible borders / DPI rounding."""

    position_px: int = DEFAULT_POSITION_TOLERANCE_PX
    size_px: int = DEFAULT_SIZE_TOLERANCE_PX

    def __post_init__(self) -> None:
        _validate_bounded_integer(
            self.position_px,
            name="position_px",
            maximum=MAX_POSITION_TOLERANCE_PX,
        )
        _validate_bounded_integer(
            self.size_px,
            name="size_px",
            maximum=MAX_SIZE_TOLERANCE_PX,
        )


@dataclass(frozen=True, slots=True)
class WindowRect:
    left: int
    top: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name, value in (
            ("left", self.left),
            ("top", self.top),
            ("width", self.width),
            ("height", self.height),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("window width and height must be positive")


@dataclass(frozen=True, slots=True)
class SafeDiscoveryStatus:
    """Log-safe discovery details: counts and confidence only."""

    code: DiscoveryCode
    candidate_accounts: int = 0
    readable_accounts: int = 0
    tablet_chat_count: int = 0
    best_overlap_count: int = 0
    best_tablet_coverage: float = 0.0
    best_pc_coverage: float = 0.0
    selected: bool = False

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "candidate_accounts": self.candidate_accounts,
            "readable_accounts": self.readable_accounts,
            "tablet_chat_count": self.tablet_chat_count,
            "best_overlap_count": self.best_overlap_count,
            "best_tablet_coverage": self.best_tablet_coverage,
            "best_pc_coverage": self.best_pc_coverage,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class SafeResolutionStatus:
    """Log-safe resolution details.  It intentionally has no path or ID."""

    code: ResolutionCode
    match_kind: MatchKind = MatchKind.NONE
    candidate_count: int = 0
    position_tolerance_px: int = DEFAULT_POSITION_TOLERANCE_PX
    size_tolerance_px: int = DEFAULT_SIZE_TOLERANCE_PX

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "match_kind": self.match_kind.value,
            "candidate_count": self.candidate_count,
            "position_tolerance_px": self.position_tolerance_px,
            "size_tolerance_px": self.size_tolerance_px,
        }


@dataclass(frozen=True, slots=True)
class ChatIdResolution:
    """A resolution whose normal representation never prints the chat ID."""

    status: SafeResolutionStatus
    chat_id: str | None = field(default=None, repr=False)

    @property
    def resolved(self) -> bool:
        return self.status.code is ResolutionCode.RESOLVED and self.chat_id is not None

    def to_safe_dict(self) -> dict[str, object]:
        safe = self.status.to_safe_dict()
        safe["resolved"] = self.resolved
        return safe


@dataclass(frozen=True, slots=True)
class PcChatMapDiscovery:
    status: SafeDiscoveryStatus
    provider: "PcKakaoChatMapProvider | None" = field(default=None, repr=False)

    def to_safe_dict(self) -> dict[str, object]:
        return self.status.to_safe_dict()


@dataclass(frozen=True, slots=True, repr=False)
class PcKakaoChatMapProvider:
    """Resolve current PC KakaoTalk geometry using one selected account DB."""

    _database_path: Path = field(repr=False)
    _tablet_chat_ids: frozenset[str] = field(repr=False)
    tolerance: GeometryTolerance = field(default_factory=GeometryTolerance)
    sqlite_timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.sqlite_timeout_seconds < 0:
            raise ValueError("sqlite_timeout_seconds cannot be negative")

    def __repr__(self) -> str:
        return "<PcKakaoChatMapProvider ready>"

    def resolve_window_rect(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> ChatIdResolution:
        """Return one stable tablet chat ID only when geometry is unambiguous."""

        try:
            requested = WindowRect(left=left, top=top, width=width, height=height)
        except (TypeError, ValueError):
            return self._result(ResolutionCode.INVALID_RECT)

        try:
            rows = _read_window_rows(
                self._database_path,
                timeout_seconds=self.sqlite_timeout_seconds,
            )
        except _DatabaseReadFailure as exc:
            return self._result(exc.code)

        exact_ids = {
            chat_id
            for chat_id, stored in rows
            if stored == requested
        }
        if exact_ids:
            return self._resolve_candidates(exact_ids, MatchKind.EXACT)

        tolerated_ids = {
            chat_id
            for chat_id, stored in rows
            if _within_tolerance(requested, stored, self.tolerance)
        }
        if tolerated_ids:
            return self._resolve_candidates(tolerated_ids, MatchKind.TOLERANCE)

        return self._result(ResolutionCode.NO_MATCH)

    def resolve_window_rect_candidate(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> ChatIdResolution:
        """Return one exact PC-metadata candidate without tablet membership.

        This method deliberately never applies geometry tolerance and does not
        treat the provider's tablet chat-ID snapshot as membership evidence.
        A returned chat ID is only a candidate: it must not be used for a
        mapping or send until a server ``getChat`` lookup and member snapshot
        independently re-verify the room and peer identity.
        """

        try:
            requested = WindowRect(left=left, top=top, width=width, height=height)
        except (TypeError, ValueError):
            return self._result(ResolutionCode.INVALID_RECT)

        try:
            rows = _read_window_rows(
                self._database_path,
                timeout_seconds=self.sqlite_timeout_seconds,
            )
        except _DatabaseReadFailure as exc:
            return self._result(exc.code)

        exact_ids = {
            chat_id
            for chat_id, stored in rows
            if stored == requested
        }
        if not exact_ids:
            return self._result(ResolutionCode.NO_MATCH)
        if len(exact_ids) != 1:
            return self._result(
                ResolutionCode.GEOMETRY_COLLISION,
                match_kind=MatchKind.EXACT,
                candidate_count=len(exact_ids),
            )
        return self._result(
            ResolutionCode.RESOLVED,
            match_kind=MatchKind.EXACT,
            candidate_count=1,
            chat_id=next(iter(exact_ids)),
        )

    def list_window_chat_id_candidates(self) -> tuple[str, ...]:
        """List bounded numeric IDs recorded in the selected PC metadata DB.

        Every returned ID is only a candidate.  Before it may be mapped or
        used for sending, callers must independently verify it with a server
        ``getChat`` lookup and a stable member snapshot.  This method reads
        only ``chatWindowUi`` through the existing read-only database helper;
        it never applies geometry tolerance or tablet membership.

        Database read failures are intentionally converted to an empty result
        so local paths, account details, and chat IDs are not exposed.
        """

        try:
            raw_ids = _read_database_chat_ids(
                self._database_path,
                timeout_seconds=self.sqlite_timeout_seconds,
            )
        except _DatabaseReadFailure:
            return ()

        numeric_ids = {
            chat_id
            for chat_id in raw_ids
            if _POSITIVE_NUMERIC_CHAT_ID.fullmatch(chat_id)
        }
        # Length-first ordering is numeric ordering without converting an
        # untrusted ID to an integer (which could be arbitrarily large).
        ordered_ids = sorted(numeric_ids, key=lambda value: (len(value), value))
        return tuple(ordered_ids[:MAX_WINDOW_CHAT_ID_CANDIDATES])

    def _resolve_candidates(
        self,
        candidate_ids: set[str],
        match_kind: MatchKind,
    ) -> ChatIdResolution:
        if len(candidate_ids) != 1:
            return self._result(
                ResolutionCode.GEOMETRY_COLLISION,
                match_kind=match_kind,
                candidate_count=len(candidate_ids),
            )
        chat_id = next(iter(candidate_ids))
        if chat_id not in self._tablet_chat_ids:
            return self._result(
                ResolutionCode.TABLET_MEMBERSHIP_MISS,
                match_kind=match_kind,
                candidate_count=1,
            )
        return self._result(
            ResolutionCode.RESOLVED,
            match_kind=match_kind,
            candidate_count=1,
            chat_id=chat_id,
        )

    def _result(
        self,
        code: ResolutionCode,
        *,
        match_kind: MatchKind = MatchKind.NONE,
        candidate_count: int = 0,
        chat_id: str | None = None,
    ) -> ChatIdResolution:
        return ChatIdResolution(
            status=SafeResolutionStatus(
                code=code,
                match_kind=match_kind,
                candidate_count=candidate_count,
                position_tolerance_px=self.tolerance.position_px,
                size_tolerance_px=self.tolerance.size_px,
            ),
            chat_id=chat_id,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _AccountCandidate:
    database_path: Path
    pc_chat_ids: frozenset[str]
    overlap_count: int
    tablet_coverage: Fraction
    pc_coverage: Fraction

    @property
    def rank(self) -> tuple[int, Fraction]:
        # PC chat logs are historical and can outnumber the current tablet
        # list by several times.  PC coverage is therefore diagnostic only:
        # using it to break a tie could select a smaller stale account.
        return (self.overlap_count, self.tablet_coverage)


class _DatabaseReadFailure(RuntimeError):
    def __init__(self, code: ResolutionCode) -> None:
        self.code = code
        super().__init__(code.value)


def default_users_root(local_app_data: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the expected KakaoTalk users directory without creating it."""

    raw_root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if raw_root is None or not os.fspath(raw_root).strip():
        return None
    return Path(raw_root) / "Kakao" / "KakaoTalk" / "users"


def discover_pc_chat_map(
    tablet_chat_ids: Iterable[object],
    *,
    local_app_data: str | os.PathLike[str] | None = None,
    users_root: str | os.PathLike[str] | None = None,
    policy: AccountSelectionPolicy | None = None,
    tolerance: GeometryTolerance | None = None,
    sqlite_timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
) -> PcChatMapDiscovery:
    """Select one active PC account using metadata-only chat-ID overlap.

    ``users_root`` exists for deterministic integration tests and portable
    callers.  Normal application code should let the function derive the
    location from ``LOCALAPPDATA``.
    """

    policy = policy or AccountSelectionPolicy()
    tolerance = tolerance or GeometryTolerance()
    try:
        tablet_ids = _normalize_chat_ids(tablet_chat_ids)
    except (TypeError, ValueError):
        tablet_ids = frozenset()
    if not tablet_ids:
        return PcChatMapDiscovery(
            SafeDiscoveryStatus(code=DiscoveryCode.NO_TABLET_IDS)
        )

    root = Path(users_root) if users_root is not None else default_users_root(local_app_data)
    if root is None or not root.is_dir():
        return PcChatMapDiscovery(
            SafeDiscoveryStatus(
                code=DiscoveryCode.USERS_ROOT_UNAVAILABLE,
                tablet_chat_count=len(tablet_ids),
            )
        )

    database_paths = tuple(_discover_account_databases(root))
    if not database_paths:
        return PcChatMapDiscovery(
            SafeDiscoveryStatus(
                code=DiscoveryCode.NO_ACCOUNT_DATABASE,
                tablet_chat_count=len(tablet_ids),
            )
        )

    candidates: list[_AccountCandidate] = []
    for database_path in database_paths:
        try:
            db_chat_ids = _read_database_chat_ids(
                database_path,
                timeout_seconds=sqlite_timeout_seconds,
            )
        except _DatabaseReadFailure:
            continue
        log_chat_ids = _discover_chat_log_ids(database_path.parent)
        # Log filenames represent the historical account set while
        # chatWindowUi may contain a newly opened room whose log filename has
        # not been observed yet.  Their union is still metadata-only.
        pc_chat_ids = log_chat_ids.union(db_chat_ids)
        overlap_count = len(tablet_ids.intersection(pc_chat_ids))
        candidates.append(
            _AccountCandidate(
                database_path=database_path,
                pc_chat_ids=pc_chat_ids,
                overlap_count=overlap_count,
                tablet_coverage=Fraction(overlap_count, len(tablet_ids)),
                pc_coverage=Fraction(overlap_count, len(pc_chat_ids)) if pc_chat_ids else Fraction(0, 1),
            )
        )

    if not candidates:
        return PcChatMapDiscovery(
            SafeDiscoveryStatus(
                code=DiscoveryCode.NO_READABLE_DATABASE,
                candidate_accounts=len(database_paths),
                tablet_chat_count=len(tablet_ids),
            )
        )

    best_rank = max(candidate.rank for candidate in candidates)
    strongest = [candidate for candidate in candidates if candidate.rank == best_rank]
    best = strongest[0]
    safe_common = {
        "candidate_accounts": len(database_paths),
        "readable_accounts": len(candidates),
        "tablet_chat_count": len(tablet_ids),
        "best_overlap_count": best.overlap_count,
        "best_tablet_coverage": _safe_fraction(best.tablet_coverage),
        "best_pc_coverage": _safe_fraction(best.pc_coverage),
    }

    if len(strongest) != 1:
        return PcChatMapDiscovery(
            SafeDiscoveryStatus(
                code=DiscoveryCode.AMBIGUOUS_ACCOUNT,
                **safe_common,
            )
        )

    if (
        best.overlap_count < policy.minimum_overlap
        or float(best.tablet_coverage) < policy.minimum_tablet_coverage
    ):
        return PcChatMapDiscovery(
            SafeDiscoveryStatus(
                code=DiscoveryCode.LOW_CONFIDENCE,
                **safe_common,
            )
        )

    status = SafeDiscoveryStatus(
        code=DiscoveryCode.READY,
        selected=True,
        **safe_common,
    )
    provider = PcKakaoChatMapProvider(
        _database_path=best.database_path,
        _tablet_chat_ids=tablet_ids,
        tolerance=tolerance,
        sqlite_timeout_seconds=sqlite_timeout_seconds,
    )
    return PcChatMapDiscovery(status=status, provider=provider)


def _validate_bounded_integer(value: object, *, name: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")


def _normalize_chat_id(value: object) -> str:
    if value is None:
        raise ValueError("chat ID is required")
    text = str(value).strip()
    if not text or any(ord(character) < 0x20 for character in text):
        raise ValueError("chat ID is invalid")
    return text


def _normalize_chat_ids(values: Iterable[object]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("tablet_chat_ids must be an iterable of IDs")
    return frozenset(_normalize_chat_id(value) for value in values)


def _is_backup_name(name: str) -> bool:
    return "backup" in name.casefold()


def _discover_account_databases(users_root: Path) -> Iterator[Path]:
    try:
        children = sorted(users_root.iterdir(), key=lambda value: value.name.casefold())
    except OSError:
        return
    for user_dir in children:
        try:
            is_directory = user_dir.is_dir()
        except OSError:
            continue
        if not is_directory or _is_backup_name(user_dir.name):
            continue
        database_path = user_dir / "chatWindowUi.db"
        try:
            if database_path.is_file():
                yield database_path
        except OSError:
            continue


def _discover_chat_log_ids(user_dir: Path) -> frozenset[str]:
    found: set[str] = set()
    try:
        walker = os.walk(user_dir, followlinks=False)
        for current_root, directory_names, file_names in walker:
            directory_names[:] = [name for name in directory_names if not _is_backup_name(name)]
            if _is_backup_name(Path(current_root).name):
                directory_names[:] = []
                continue
            for file_name in file_names:
                match = _CHAT_LOG_NAME.fullmatch(file_name)
                if match is None:
                    continue
                try:
                    found.add(_normalize_chat_id(match.group(1)))
                except ValueError:
                    continue
    except OSError:
        return frozenset()
    return frozenset(found)


def _readonly_sqlite_uri(database_path: Path) -> str:
    # Path.as_uri() performs the necessary escaping for spaces, '#' and
    # non-ASCII user profile names.  mode=ro makes accidental writes fail at
    # SQLite's VFS boundary, rather than relying on application discipline.
    return database_path.resolve().as_uri() + "?mode=ro"


def _open_readonly_database(database_path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
    connection = sqlite3.connect(
        _readonly_sqlite_uri(database_path),
        uri=True,
        timeout=timeout_seconds,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _read_database_chat_ids(database_path: Path, *, timeout_seconds: float) -> frozenset[str]:
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_readonly_database(database_path, timeout_seconds=timeout_seconds)
        rows = connection.execute("SELECT chatId FROM chatWindowUi").fetchall()
    except sqlite3.OperationalError as exc:
        raise _translate_sqlite_error(exc) from None
    except (OSError, sqlite3.DatabaseError):
        raise _DatabaseReadFailure(ResolutionCode.DATABASE_UNAVAILABLE) from None
    finally:
        if connection is not None:
            connection.close()

    chat_ids: set[str] = set()
    for (raw_chat_id,) in rows:
        try:
            chat_ids.add(_normalize_chat_id(raw_chat_id))
        except ValueError:
            continue
    return frozenset(chat_ids)


def _read_window_rows(
    database_path: Path,
    *,
    timeout_seconds: float,
) -> tuple[tuple[str, WindowRect], ...]:
    columns = ", ".join(_WINDOW_COLUMNS)
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_readonly_database(database_path, timeout_seconds=timeout_seconds)
        raw_rows = connection.execute(f"SELECT {columns} FROM chatWindowUi").fetchall()
    except sqlite3.OperationalError as exc:
        raise _translate_sqlite_error(exc) from None
    except (OSError, sqlite3.DatabaseError):
        raise _DatabaseReadFailure(ResolutionCode.DATABASE_UNAVAILABLE) from None
    finally:
        if connection is not None:
            connection.close()

    rows: list[tuple[str, WindowRect]] = []
    for raw_chat_id, raw_width, raw_height, raw_left, raw_top in raw_rows:
        try:
            chat_id = _normalize_chat_id(raw_chat_id)
            rect = WindowRect(
                left=_coerce_sqlite_integer(raw_left),
                top=_coerce_sqlite_integer(raw_top),
                width=_coerce_sqlite_integer(raw_width),
                height=_coerce_sqlite_integer(raw_height),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        rows.append((chat_id, rect))
    return tuple(rows)


def _coerce_sqlite_integer(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid geometry")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("invalid geometry")
        return int(value)
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ValueError("invalid geometry")
    return int(text)


def _translate_sqlite_error(error: sqlite3.OperationalError) -> _DatabaseReadFailure:
    detail = str(error).casefold()
    if "locked" in detail or "busy" in detail:
        return _DatabaseReadFailure(ResolutionCode.DATABASE_BUSY)
    if "no such table" in detail or "no such column" in detail:
        return _DatabaseReadFailure(ResolutionCode.SCHEMA_UNAVAILABLE)
    return _DatabaseReadFailure(ResolutionCode.DATABASE_UNAVAILABLE)


def _within_tolerance(
    requested: WindowRect,
    stored: WindowRect,
    tolerance: GeometryTolerance,
) -> bool:
    return (
        abs(requested.left - stored.left) <= tolerance.position_px
        and abs(requested.top - stored.top) <= tolerance.position_px
        and abs(requested.width - stored.width) <= tolerance.size_px
        and abs(requested.height - stored.height) <= tolerance.size_px
    )


def _safe_fraction(value: Fraction) -> float:
    return round(float(value), 4)


__all__ = [
    "AccountSelectionPolicy",
    "ChatIdResolution",
    "DiscoveryCode",
    "GeometryTolerance",
    "MatchKind",
    "PcChatMapDiscovery",
    "PcKakaoChatMapProvider",
    "ResolutionCode",
    "SafeDiscoveryStatus",
    "SafeResolutionStatus",
    "WindowRect",
    "default_users_root",
    "discover_pc_chat_map",
]
