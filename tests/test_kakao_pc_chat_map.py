from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kakao_pc_chat_map
from kakao_pc_chat_map import (
    AccountSelectionPolicy,
    DiscoveryCode,
    GeometryTolerance,
    MatchKind,
    ResolutionCode,
    discover_pc_chat_map,
)


WINDOW_SCHEMA = """
CREATE TABLE chatWindowUi (
    chatId TEXT NOT NULL,
    windowsizeWidth INTEGER,
    windowsizeHeight INTEGER,
    windowsposLeft INTEGER,
    windowsposTop INTEGER
)
"""


class PcChatMapFixture:
    def __init__(self, root: Path) -> None:
        self.local_app_data = root / "Local App Data"
        self.users_root = self.local_app_data / "Kakao" / "KakaoTalk" / "users"
        self.users_root.mkdir(parents=True)

    def account(
        self,
        name: str,
        *,
        rows: tuple[tuple[object, object, object, object, object], ...] = (),
        log_ids: tuple[object, ...] = (),
    ) -> Path:
        account_dir = self.users_root / name
        account_dir.mkdir(parents=True)
        database_path = account_dir / "chatWindowUi.db"
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(WINDOW_SCHEMA)
            connection.executemany(
                """
                INSERT INTO chatWindowUi (
                    chatId, windowsizeWidth, windowsizeHeight,
                    windowsposLeft, windowsposTop
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()
        finally:
            connection.close()
        chat_data = account_dir / "chat_data"
        chat_data.mkdir()
        for chat_id in log_ids:
            (chat_data / f"chatLogs_{chat_id}.edb").touch()
        return database_path


class PcChatMapDiscoveryTests(unittest.TestCase):
    def test_discovers_default_users_root_and_excludes_all_backup_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            active_db = fixture.account(
                "active-user",
                rows=(("101", 600, 700, 10, 20),),
                log_ids=("101", "102"),
            )
            nested_backup = active_db.parent / "chat_data" / "backup-2025"
            nested_backup.mkdir()
            (nested_backup / "chatLogs_999.edb").touch()
            fixture.account(
                "backup-old-account",
                rows=(("101", 600, 700, 10, 20),),
                log_ids=("101", "102"),
            )

            discovery = discover_pc_chat_map(
                {"101", "102"},
                local_app_data=fixture.local_app_data,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.READY)
            self.assertEqual(discovery.status.candidate_accounts, 1)
            self.assertEqual(discovery.status.best_pc_coverage, 1.0)
            self.assertIsNotNone(discovery.provider)

    def test_selects_strongest_account_by_intersection_then_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            fixture.account(
                "weaker",
                rows=(("1", 500, 600, 100, 100),),
                log_ids=("1", "2", "3", "other"),
            )
            fixture.account(
                "active",
                rows=(("4", 720, 640, 300, 200),),
                log_ids=("1", "2", "3", "4"),
            )

            discovery = discover_pc_chat_map(
                {"1", "2", "3", "4"},
                users_root=fixture.users_root,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.READY)
            self.assertEqual(discovery.status.candidate_accounts, 2)
            self.assertEqual(discovery.status.best_overlap_count, 4)
            resolution = discovery.provider.resolve_window_rect(300, 200, 720, 640)  # type: ignore[union-attr]
            self.assertTrue(resolution.resolved)
            self.assertEqual(resolution.chat_id, "4")

    def test_same_tablet_overlap_is_ambiguous_regardless_of_pc_history_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            fixture.account(
                "noisy",
                rows=(("1", 400, 500, 20, 30),),
                log_ids=("1", "2", "3", "unrelated"),
            )
            fixture.account(
                "exact-set",
                rows=(("3", 800, 600, 50, 60),),
                log_ids=("1", "2", "3"),
            )

            discovery = discover_pc_chat_map(
                {"1", "2", "3"},
                users_root=fixture.users_root,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.AMBIGUOUS_ACCOUNT)
            self.assertIsNone(discovery.provider)

    def test_full_tablet_overlap_with_large_pc_history_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            tablet_ids = {str(value) for value in range(1, 71)}
            pc_log_ids = tuple(str(value) for value in range(1, 375))
            fixture.account(
                "realistic-active",
                rows=(("70", 800, 600, 50, 60),),
                log_ids=pc_log_ids,
            )

            discovery = discover_pc_chat_map(
                tablet_ids,
                users_root=fixture.users_root,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.READY)
            self.assertEqual(discovery.status.best_overlap_count, 70)
            self.assertEqual(discovery.status.best_tablet_coverage, 1.0)
            self.assertAlmostEqual(discovery.status.best_pc_coverage, 70 / 374, places=4)
            resolution = discovery.provider.resolve_window_rect(50, 60, 800, 600)  # type: ignore[union-attr]
            self.assertEqual(resolution.chat_id, "70")

    def test_equal_account_scores_are_refused_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            fixture.account("account-a", log_ids=("1", "2", "3"))
            fixture.account("account-b", log_ids=("1", "2", "3"))

            discovery = discover_pc_chat_map(
                {"1", "2", "3"},
                users_root=fixture.users_root,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.AMBIGUOUS_ACCOUNT)
            self.assertIsNone(discovery.provider)
            self.assertFalse(discovery.status.selected)

    def test_low_overlap_ratio_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            fixture.account("stale", log_ids=("1", "old-a", "old-b"))

            discovery = discover_pc_chat_map(
                {"1", "2", "3", "4", "5"},
                users_root=fixture.users_root,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.LOW_CONFIDENCE)
            self.assertEqual(discovery.status.best_overlap_count, 1)
            self.assertIsNone(discovery.provider)

    def test_unreadable_or_wrong_schema_database_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            broken_dir = fixture.users_root / "broken"
            broken_dir.mkdir()
            connection = sqlite3.connect(broken_dir / "chatWindowUi.db")
            try:
                connection.execute("CREATE TABLE somethingElse (value TEXT)")
                connection.commit()
            finally:
                connection.close()

            discovery = discover_pc_chat_map(
                {"1"},
                users_root=fixture.users_root,
            )

            self.assertEqual(discovery.status.code, DiscoveryCode.NO_READABLE_DATABASE)
            self.assertIsNone(discovery.provider)


class PcChatMapGeometryTests(unittest.TestCase):
    def _provider(
        self,
        fixture: PcChatMapFixture,
        *,
        rows: tuple[tuple[object, object, object, object, object], ...],
        tablet_ids: set[str],
        log_ids: tuple[object, ...] | None = None,
        tolerance: GeometryTolerance | None = None,
    ):
        fixture.account(
            "active",
            rows=rows,
            log_ids=log_ids if log_ids is not None else tuple(tablet_ids),
        )
        discovery = discover_pc_chat_map(
            tablet_ids,
            users_root=fixture.users_root,
            tolerance=tolerance,
        )
        self.assertEqual(discovery.status.code, DiscoveryCode.READY)
        self.assertIsNotNone(discovery.provider)
        return discovery.provider

    def test_exact_match_has_priority_over_other_tolerance_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(
                    ("1", 800, 600, 100, 200),
                    ("2", 808, 608, 104, 204),
                ),
                tablet_ids={"1", "2"},
            )

            resolution = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertEqual(resolution.status.code, ResolutionCode.RESOLVED)
            self.assertEqual(resolution.status.match_kind, MatchKind.EXACT)
            self.assertEqual(resolution.chat_id, "1")

    def test_unique_bounded_tolerance_match_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(("77", 816, 584, 108, 192),),
                tablet_ids={"77"},
            )

            resolution = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertTrue(resolution.resolved)
            self.assertEqual(resolution.status.match_kind, MatchKind.TOLERANCE)
            self.assertEqual(resolution.chat_id, "77")

    def test_geometry_outside_explicit_tolerance_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(("77", 817, 600, 100, 200),),
                tablet_ids={"77"},
            )

            resolution = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertEqual(resolution.status.code, ResolutionCode.NO_MATCH)
            self.assertIsNone(resolution.chat_id)

    def test_exact_geometry_collision_is_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(
                    ("1", 800, 600, 100, 200),
                    ("2", 800, 600, 100, 200),
                ),
                tablet_ids={"1", "2"},
            )

            resolution = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertEqual(resolution.status.code, ResolutionCode.GEOMETRY_COLLISION)
            self.assertEqual(resolution.status.match_kind, MatchKind.EXACT)
            self.assertEqual(resolution.status.candidate_count, 2)
            self.assertIsNone(resolution.chat_id)

    def test_tolerance_geometry_collision_is_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(
                    ("1", 808, 608, 104, 204),
                    ("2", 792, 592, 96, 196),
                ),
                tablet_ids={"1", "2"},
            )

            resolution = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertEqual(resolution.status.code, ResolutionCode.GEOMETRY_COLLISION)
            self.assertEqual(resolution.status.match_kind, MatchKind.TOLERANCE)
            self.assertIsNone(resolution.chat_id)

    def test_pc_geometry_id_missing_from_tablet_set_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(("not-on-tablet", 800, 600, 100, 200),),
                tablet_ids={"known-tablet-id"},
                log_ids=("known-tablet-id",),
            )

            resolution = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertEqual(resolution.status.code, ResolutionCode.TABLET_MEMBERSHIP_MISS)
            self.assertIsNone(resolution.chat_id)

    def test_exact_candidate_can_nominate_an_id_missing_from_the_tablet_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(("202", 800, 600, 100, 200),),
                tablet_ids={"101"},
                log_ids=("101",),
            )

            candidate = provider.resolve_window_rect_candidate(100, 200, 800, 600)
            gated = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertTrue(candidate.resolved)
            self.assertEqual(candidate.chat_id, "202")
            self.assertEqual(candidate.status.match_kind, MatchKind.EXACT)
            self.assertEqual(candidate.status.candidate_count, 1)
            self.assertEqual(gated.status.code, ResolutionCode.TABLET_MEMBERSHIP_MISS)

    def test_candidate_lookup_never_uses_geometry_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(("202", 816, 584, 108, 192),),
                tablet_ids={"101"},
                log_ids=("101",),
            )

            candidate = provider.resolve_window_rect_candidate(100, 200, 800, 600)
            tolerant = provider.resolve_window_rect(100, 200, 800, 600)

            self.assertFalse(candidate.resolved)
            self.assertEqual(candidate.status.code, ResolutionCode.NO_MATCH)
            self.assertEqual(candidate.status.match_kind, MatchKind.NONE)
            self.assertEqual(tolerant.status.match_kind, MatchKind.TOLERANCE)

    def test_window_candidate_catalog_filters_numeric_ids_deduplicates_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(
                    ("12", None, None, None, None),
                    ("2", None, None, None, None),
                    ("12", None, None, None, None),
                    ("1", None, None, None, None),
                    ("42", None, None, None, None),
                    ("0", None, None, None, None),
                    ("001", None, None, None, None),
                    ("-7", None, None, None, None),
                    ("not-a-chat", None, None, None, None),
                ),
                tablet_ids={"1"},
                log_ids=("1",),
            )

            self.assertEqual(
                provider.list_window_chat_id_candidates(),
                ("1", "2", "12", "42"),
            )

    def test_window_candidate_catalog_is_capped_and_failure_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=tuple(
                    (str(value), None, None, None, None)
                    for value in range(1, 601)
                ),
                tablet_ids={"1"},
                log_ids=("1",),
            )

            candidates = provider.list_window_chat_id_candidates()
            self.assertEqual(len(candidates), 512)
            self.assertEqual(candidates[0], "1")
            self.assertEqual(candidates[-1], "512")

            with patch.object(
                kakao_pc_chat_map,
                "_read_database_chat_ids",
                side_effect=kakao_pc_chat_map._DatabaseReadFailure(
                    ResolutionCode.DATABASE_BUSY
                ),
            ):
                self.assertEqual(provider.list_window_chat_id_candidates(), ())

    def test_window_candidate_catalog_stays_read_only_under_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            database_path = fixture.account(
                "active",
                rows=(("123", None, None, None, None),),
                log_ids=("123",),
            )
            discovery = discover_pc_chat_map(
                {"123"},
                users_root=fixture.users_root,
            )
            provider = discovery.provider
            self.assertIsNotNone(provider)
            original_bytes = database_path.read_bytes()
            real_connect = sqlite3.connect
            calls: list[tuple[object, dict[str, object]]] = []

            writer = real_connect(database_path)
            writer.execute("BEGIN IMMEDIATE")

            def connect_spy(database, *args, **kwargs):
                calls.append((database, dict(kwargs)))
                return real_connect(database, *args, **kwargs)

            try:
                with patch.object(kakao_pc_chat_map.sqlite3, "connect", side_effect=connect_spy):
                    self.assertEqual(provider.list_window_chat_id_candidates(), ("123",))  # type: ignore[union-attr]
            finally:
                writer.rollback()
                writer.close()

            self.assertEqual(database_path.read_bytes(), original_bytes)
            self.assertEqual(len(calls), 1)
            database, kwargs = calls[0]
            self.assertIsInstance(database, str)
            self.assertTrue(str(database).startswith("file:"))
            self.assertTrue(str(database).endswith("?mode=ro"))
            self.assertIs(kwargs.get("uri"), True)

    def test_invalid_rect_and_unreasonably_large_tolerance_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            provider = self._provider(
                fixture,
                rows=(("1", 800, 600, 100, 200),),
                tablet_ids={"1"},
            )
            invalid = provider.resolve_window_rect(100, 200, 0, 600)
            self.assertEqual(invalid.status.code, ResolutionCode.INVALID_RECT)

        with self.assertRaises(ValueError):
            GeometryTolerance(position_px=17)
        with self.assertRaises(ValueError):
            GeometryTolerance(size_px=33)
        with self.assertRaises(ValueError):
            AccountSelectionPolicy(minimum_overlap=1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AccountSelectionPolicy(minimum_tablet_coverage="0.8")  # type: ignore[arg-type]


class PcChatMapReadOnlySafetyTests(unittest.TestCase):
    def test_all_sqlite_connections_are_read_only_and_reserved_lock_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            database_path = fixture.account(
                "active",
                rows=(("123456789", 800, 600, 100, 200),),
                log_ids=("123456789",),
            )
            original_bytes = database_path.read_bytes()
            real_connect = sqlite3.connect
            calls: list[tuple[object, dict[str, object]]] = []

            writer = real_connect(database_path)
            writer.execute("BEGIN IMMEDIATE")

            def connect_spy(database, *args, **kwargs):
                calls.append((database, dict(kwargs)))
                return real_connect(database, *args, **kwargs)

            try:
                with patch.object(kakao_pc_chat_map.sqlite3, "connect", side_effect=connect_spy):
                    discovery = discover_pc_chat_map(
                        {"123456789"},
                        users_root=fixture.users_root,
                    )
                    self.assertEqual(discovery.status.code, DiscoveryCode.READY)
                    resolution = discovery.provider.resolve_window_rect(100, 200, 800, 600)  # type: ignore[union-attr]
            finally:
                writer.rollback()
                writer.close()

            self.assertTrue(resolution.resolved)
            self.assertGreaterEqual(len(calls), 2)
            for database, kwargs in calls:
                self.assertIsInstance(database, str)
                self.assertTrue(str(database).startswith("file:"))
                self.assertTrue(str(database).endswith("?mode=ro"))
                self.assertIs(kwargs.get("uri"), True)
            self.assertEqual(database_path.read_bytes(), original_bytes)

    def test_exclusive_lock_returns_safe_busy_status_without_leaking_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            database_path = fixture.account(
                "active",
                rows=(("SECRET-ID-42", 800, 600, 100, 200),),
                log_ids=("SECRET-ID-42",),
            )
            discovery = discover_pc_chat_map(
                {"SECRET-ID-42"},
                users_root=fixture.users_root,
            )
            self.assertEqual(discovery.status.code, DiscoveryCode.READY)

            writer = sqlite3.connect(database_path)
            writer.execute("BEGIN EXCLUSIVE")
            try:
                resolution = discovery.provider.resolve_window_rect(  # type: ignore[union-attr]
                    100, 200, 800, 600
                )
            finally:
                writer.rollback()
                writer.close()

            self.assertEqual(resolution.status.code, ResolutionCode.DATABASE_BUSY)
            self.assertIsNone(resolution.chat_id)
            self.assertNotIn("SECRET-ID-42", repr(resolution))
            self.assertNotIn(str(database_path), repr(resolution))
            self.assertNotIn("SECRET-ID-42", str(resolution.to_safe_dict()))

    def test_ready_and_resolved_representations_hide_path_and_chat_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = PcChatMapFixture(Path(temp_dir))
            database_path = fixture.account(
                "private-account-name",
                rows=(("PRIVATE-CHAT-ID", 800, 600, 100, 200),),
                log_ids=("PRIVATE-CHAT-ID",),
            )
            discovery = discover_pc_chat_map(
                {"PRIVATE-CHAT-ID"},
                users_root=fixture.users_root,
            )
            resolution = discovery.provider.resolve_window_rect(100, 200, 800, 600)  # type: ignore[union-attr]

            combined_safe_output = " ".join(
                (
                    repr(discovery),
                    repr(discovery.provider),
                    repr(resolution),
                    str(discovery.to_safe_dict()),
                    str(resolution.to_safe_dict()),
                )
            )
            self.assertNotIn("PRIVATE-CHAT-ID", combined_safe_output)
            self.assertNotIn("private-account-name", combined_safe_output)
            self.assertNotIn(str(database_path), combined_safe_output)
            self.assertEqual(resolution.chat_id, "PRIVATE-CHAT-ID")


if __name__ == "__main__":
    unittest.main()
