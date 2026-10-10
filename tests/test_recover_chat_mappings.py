from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from kakao_direct import (
    DirectConfigLockTimeout,
    DirectKakaoConfig,
    direct_config_lock,
)
from recover_chat_mappings import (
    apply_recovery,
    create_adjacent_backup,
    merge_sanitized_chat_list,
)


class MergeSanitizedChatListTests(unittest.TestCase):
    def test_preserves_manual_alias_and_verified_label_while_adding_unique_room(self) -> None:
        existing = DirectKakaoConfig(
            enabled=True,
            mappings={"PC manual alias": "existing-id"},
            mapping_labels={"existing-id": "Verified Existing Label"},
        )

        plan = merge_sanitized_chat_list(
            existing,
            [
                {"chat_id": "existing-id", "title": "Current Server Label"},
                {"chat_id": "new-id", "title": "New Unique Room"},
            ],
        )

        self.assertEqual(
            plan.merged_config.mappings,
            {"pc manual alias": "existing-id", "new unique room": "new-id"},
        )
        self.assertEqual(
            plan.merged_config.mapping_labels,
            {
                "existing-id": "Verified Existing Label",
                "new-id": "New Unique Room",
            },
        )
        self.assertEqual(plan.counts.retained_mappings, 1)
        self.assertEqual(plan.counts.retained_labels, 1)
        self.assertEqual(plan.counts.added_mappings, 1)
        self.assertEqual(plan.counts.existing_chat_id_conflicts, 1)
        self.assertEqual(existing.mappings, {"pc manual alias": "existing-id"})

    def test_normalized_conflicts_and_duplicate_ownership_fail_closed(self) -> None:
        existing = DirectKakaoConfig(
            mappings={"Existing Room": "owned-id", "Manual Alias": "alias-id"},
            mapping_labels={"owned-id": "Existing Room", "alias-id": "Saved Alias Label"},
        )
        secret_values = (
            "Existing Room",
            "replacement-id",
            "Duplicate Name",
            "shared-id",
            "Manual Alias",
        )

        plan = merge_sanitized_chat_list(
            existing,
            [
                {"chat_id": "replacement-id", "title": " ＥＸＩＳＴＩＮＧ   ROOM "},
                {"chat_id": "duplicate-a", "title": "Duplicate Name"},
                {"chat_id": "duplicate-b", "title": " duplicate   name "},
                {"chat_id": "shared-id", "title": "First Owner"},
                {"chat_id": "shared-id", "title": "Second Owner"},
                {"chat_id": "alias-id", "title": "Different Current Label"},
                {"chat_id": "safe-id", "title": "Safe Unique Room"},
            ],
        )

        self.assertEqual(plan.merged_config.mappings["existing room"], "owned-id")
        self.assertEqual(plan.merged_config.mappings["manual alias"], "alias-id")
        self.assertEqual(plan.merged_config.mappings["safe unique room"], "safe-id")
        self.assertNotIn("duplicate name", plan.merged_config.mappings)
        self.assertNotIn("first owner", plan.merged_config.mappings)
        self.assertNotIn("second owner", plan.merged_config.mappings)
        self.assertEqual(plan.counts.duplicate_name_records, 2)
        self.assertEqual(plan.counts.duplicate_chat_id_records, 2)
        self.assertEqual(plan.counts.existing_name_conflicts, 1)
        self.assertEqual(plan.counts.existing_chat_id_conflicts, 1)
        for value in secret_values:
            self.assertNotIn(value, repr(plan))

    def test_rejects_invalid_synthetic_and_unverified_peer_labels(self) -> None:
        plan = merge_sanitized_chat_list(
            DirectKakaoConfig(),
            [
                {"chat_id": "one", "title": "채팅 one"},
                {"chat_id": "two", "title": "\u0001unsafe"},
                {
                    "chat_id": "three",
                    "type": "DirectChat",
                    "active_members": 3,
                    "peer_user_id": "123",
                    "peer_names": ["Not Verified"],
                },
                {
                    "chat_id": "four",
                    "type": "DirectChat",
                    "active_members": 2,
                    "peer_user_id": "123",
                    "peer_names": ["Verified Peer"],
                },
            ],
        )

        self.assertEqual(plan.merged_config.mappings, {"verified peer": "four"})
        self.assertEqual(plan.counts.invalid_records, 3)
        self.assertEqual(plan.counts.added_mappings, 1)


class ApplyRecoveryTests(unittest.TestCase):
    def test_backup_name_collision_does_not_overwrite_or_delete_existing_backup(self) -> None:
        timestamp = datetime(2026, 8, 31, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            config_path.write_bytes(b"current config")
            expected_backup = config_path.with_name(
                "direct.json.20260831T000000000000Z.bak"
            )
            expected_backup.write_bytes(b"pre-existing backup")

            with self.assertRaises(FileExistsError):
                create_adjacent_backup(config_path, timestamp=timestamp)

            self.assertEqual(expected_backup.read_bytes(), b"pre-existing backup")

    def test_apply_creates_exact_adjacent_backup_before_atomic_model_save(self) -> None:
        timestamp = datetime(2026, 8, 31, 1, 2, 3, 456789, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            DirectKakaoConfig(
                enabled=True,
                mappings={"Existing": "old-id"},
                mapping_labels={"old-id": "Existing"},
                request_timeout=22.0,
            ).save(config_path)
            original_bytes = config_path.read_bytes()
            expected_backup = config_path.with_name(
                "direct.json.20260831T010203456789Z.bak"
            )
            real_save = DirectKakaoConfig.save
            save_observations: list[bool] = []

            def checked_save(config: DirectKakaoConfig, path: str | Path) -> Path:
                save_observations.append(
                    expected_backup.is_file()
                    and expected_backup.read_bytes() == original_bytes
                )
                return real_save(config, path)

            with patch.object(DirectKakaoConfig, "save", new=checked_save):
                result = apply_recovery(
                    config_path,
                    [{"chat_id": "new-id", "title": "New Room"}],
                    timestamp=timestamp,
                )

            self.assertEqual(save_observations, [True])
            self.assertEqual(result.backup_path, expected_backup)
            self.assertEqual(expected_backup.read_bytes(), original_bytes)
            reloaded = DirectKakaoConfig.load(config_path)
            self.assertEqual(
                reloaded.mappings,
                {"existing": "old-id", "new room": "new-id"},
            )
            self.assertTrue(reloaded.enabled)
            self.assertEqual(reloaded.request_timeout, 22.0)
            self.assertEqual(result.counts.added_mappings, 1)

    def test_save_failure_keeps_backup_and_original_config(self) -> None:
        timestamp = datetime(2026, 8, 31, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            DirectKakaoConfig(mappings={"Existing": "old-id"}).save(config_path)
            original_bytes = config_path.read_bytes()
            expected_backup = config_path.with_name(
                "direct.json.20260831T000000000000Z.bak"
            )

            with patch.object(
                DirectKakaoConfig,
                "save",
                side_effect=OSError("simulated atomic save failure"),
            ):
                with self.assertRaises(OSError):
                    apply_recovery(config_path, [], timestamp=timestamp)

            self.assertEqual(config_path.read_bytes(), original_bytes)
            self.assertEqual(expected_backup.read_bytes(), original_bytes)

    def test_writer_before_recovery_is_loaded_and_both_updates_survive(self) -> None:
        timestamp = datetime(2026, 8, 31, 4, 5, 6, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            DirectKakaoConfig(mappings={"Base": "base-id"}).save(config_path)
            recovery_lock_requested = threading.Event()
            recovery_done = threading.Event()
            recovery_errors: list[BaseException] = []
            real_lock = direct_config_lock

            def observed_recovery_lock(*args, **kwargs):
                recovery_lock_requested.set()
                return real_lock(*args, **kwargs)

            def recover() -> None:
                try:
                    apply_recovery(
                        config_path,
                        [{"chat_id": "recovery-id", "title": "Recovery"}],
                        timestamp=timestamp,
                    )
                except BaseException as exc:  # surfaced by the main test thread
                    recovery_errors.append(exc)
                finally:
                    recovery_done.set()

            # Model an ordinary writer that entered first.  Recovery announces
            # its lock request, then must wait.  The nested save also proves
            # the lock is re-entrant for the owning thread.
            with direct_config_lock(config_path):
                with patch(
                    "recover_chat_mappings.direct_config_lock",
                    side_effect=observed_recovery_lock,
                ):
                    worker = threading.Thread(target=recover, daemon=True)
                    worker.start()
                    self.assertTrue(recovery_lock_requested.wait(timeout=2.0))
                    self.assertFalse(recovery_done.is_set())
                    DirectKakaoConfig(
                        mappings={"Base": "base-id", "Writer": "writer-id"}
                    ).save(config_path)

            worker.join(timeout=2.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(recovery_errors, [])
            self.assertEqual(
                DirectKakaoConfig.load(config_path).mappings,
                {
                    "base": "base-id",
                    "writer": "writer-id",
                    "recovery": "recovery-id",
                },
            )

    def test_recovery_lock_timeout_preserves_active_config_and_creates_no_backup(self) -> None:
        timestamp = datetime(2026, 8, 31, 7, 8, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            DirectKakaoConfig(mappings={"Existing": "old-id"}).save(config_path)
            original_bytes = config_path.read_bytes()
            errors: list[BaseException] = []

            def recover() -> None:
                try:
                    apply_recovery(
                        config_path,
                        [{"chat_id": "new-id", "title": "New"}],
                        timestamp=timestamp,
                        lock_timeout=0.0,
                    )
                except BaseException as exc:  # surfaced by the main test thread
                    errors.append(exc)

            with direct_config_lock(config_path):
                worker = threading.Thread(target=recover, daemon=True)
                worker.start()
                worker.join(timeout=2.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], DirectConfigLockTimeout)
            self.assertEqual(str(errors[0]), "direct Kakao config is busy")
            self.assertEqual(config_path.read_bytes(), original_bytes)
            expected_backup = config_path.with_name(
                "direct.json.20260831T070809000000Z.bak"
            )
            self.assertFalse(expected_backup.exists())


if __name__ == "__main__":
    unittest.main()
