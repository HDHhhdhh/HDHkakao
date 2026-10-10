import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kakao_schedule_journal import ScheduleJournal, ScheduleJournalError, reservation_key


def row(**changes):
    return {"index": 1, "chat_name": "A 학생", "text": "첫째\n둘째", "scheduled_at": "2026-09-28T23:00:00", **changes}


class ScheduleJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "journal.sqlite3"
        self.journal = ScheduleJournal(self.path)

    def test_canonical_key_ignores_index_but_preserves_exact_body(self):
        original = reservation_key(row())
        self.assertEqual(original, reservation_key(row(index=999, scheduled_at=datetime(2026, 9, 28, 23))))
        for change in ({"text": "첫째\r\n둘째"}, {"text": "첫째\n둘째 "}, {"chat_name": "A 학생 "}, {"scheduled_at": "2026-09-29T23:00:00"}):
            self.assertNotEqual(original, reservation_key(row(**change)))

    def test_duplicate_csv_rows_reject_whole_preparation(self):
        with self.assertRaises(ScheduleJournalError):
            self.journal.prepare([row(), row(index=2)])
        self.assertEqual((), self.journal.records())

    def test_batch_preparation_checks_integrity_once_and_returns_committed_records(self):
        source = [row(index=index, chat_name=f"fixture {index}") for index in range(1, 51)]
        with patch.object(ScheduleJournal, "_validate", wraps=ScheduleJournal._validate) as validate:
            prepared = self.journal.prepare(source)
        self.assertEqual(1, validate.call_count)
        self.assertEqual([reservation_key(item) for item in source], [record.key for record in prepared])
        self.assertEqual(prepared, ScheduleJournal(self.path).records())

    def test_batch_preparation_returns_no_receipts_if_commit_fails(self):
        with patch.object(ScheduleJournal, "_seal", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(ScheduleJournalError):
                self.journal.prepare([row()])
        self.assertEqual((), ScheduleJournal(self.path).records())

    def test_submitting_is_committed_before_external_action_and_restart_blocks_retry(self):
        self.journal.prepare([row()])
        self.journal.mark_submitting(row(), evidence={"screenshots": ["pre.png"], "observed": {"time": "23:00"}})
        reloaded = ScheduleJournal(self.path)
        self.assertEqual("submitting", reloaded.get(row()).state)
        self.assertFalse(reloaded.safe_to_submit(row()))
        self.assertEqual((), reloaded.pending())
        with self.assertRaises(ScheduleJournalError):
            reloaded.mark_submitting(row())
        self.assertEqual(["pre.png"], reloaded.get(row()).events[-1]["evidence"]["screenshots"])

    def test_uncertain_ack_never_becomes_retryable(self):
        self.journal.prepare([row()])
        self.journal.mark_submitting(row())
        self.journal.mark_unknown(row(), evidence={"reason": "toast not observed"})
        for operation in (self.journal.mark_submitting, self.journal.mark_rejected_before_submit):
            with self.assertRaises(ScheduleJournalError):
                operation(row())
        self.assertFalse(ScheduleJournal(self.path).safe_to_submit(row()))
        self.journal.mark_verified(row(), evidence={"list_screenshot": "list.png"})
        self.assertEqual("verified", self.journal.get(row()).state)

    def test_acknowledged_and_verified_are_not_resubmitted(self):
        self.journal.prepare([row()])
        self.journal.mark_submitting(row())
        self.journal.mark_acknowledged(row(), evidence={"toast": "메시지를 예약했습니다."})
        self.assertFalse(self.journal.safe_to_submit(row()))
        self.journal.mark_verified(row(), evidence={"detail_screenshot": "detail.png"})
        self.journal.prepare([row()])
        self.assertEqual("verified", self.journal.get(row()).state)
        with self.assertRaises(ScheduleJournalError):
            self.journal.assert_safe_to_submit(row())

    def test_rejection_before_submit_can_retry_but_not_after_submit(self):
        self.journal.prepare([row()])
        self.journal.mark_rejected_before_submit(row(), evidence={"reason": "wrong date"})
        self.assertTrue(self.journal.safe_to_submit(row()))
        self.journal.mark_submitting(row())
        with self.assertRaises(ScheduleJournalError):
            self.journal.mark_rejected_before_submit(row())

    def test_two_instances_cannot_both_cross_submit_boundary(self):
        self.journal.prepare([row()])
        second = ScheduleJournal(self.path)
        self.assertTrue(second.safe_to_submit(row()))
        self.journal.mark_submitting(row())
        with self.assertRaises(ScheduleJournalError):
            second.mark_submitting(row())

    def test_failed_atomic_seal_rolls_back_state(self):
        self.journal.prepare([row()])
        with patch.object(ScheduleJournal, "_seal", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(ScheduleJournalError):
                self.journal.mark_submitting(row())
        self.assertEqual("prepared", ScheduleJournal(self.path).get(row()).state)

    def test_tampered_valid_sqlite_fails_closed(self):
        self.journal.prepare([row()])
        self.journal.mark_submitting(row())
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("UPDATE reservations SET state='prepared'")
        with self.assertRaises(ScheduleJournalError):
            self.journal.pending()
        with self.assertRaises(ScheduleJournalError):
            ScheduleJournal(self.path)

    def test_truncation_and_disappearance_fail_closed(self):
        self.path.write_bytes(b"broken")
        with self.assertRaises(ScheduleJournalError):
            ScheduleJournal(self.path)
        self.path.unlink()
        with self.assertRaises(ScheduleJournalError):
            self.journal.prepare([row()])

    def prior_files(self, rows, results):
        rows_path, results_path = self.folder / "rows.json", self.folder / "results.json"
        rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        results_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        return rows_path, results_path

    def receipt(self, source, *, verified=False):
        return {"index": source["index"], "chat_name": source["chat_name"], "scheduled_at": source["scheduled_at"],
                "status": "registered_verified" if verified else "registered_acknowledged",
                "body_exact_after_registration" if verified else "body_exact_before_registration": True, "ack_seq": 25}

    def test_import_uses_exact_content_not_current_index_and_preserves_evidence(self):
        previous = row()
        edited = row(text="수정된 내용")
        reindexed = row(index=7)
        self.journal.prepare([edited, reindexed])
        screenshots = self.folder / "screenshots"
        screenshots.mkdir()
        (screenshots / "00025-0.png").write_bytes(b"test evidence")
        source_paths = self.prior_files([previous], [self.receipt(previous)])
        self.journal.import_prior_run(*source_paths)
        self.assertEqual("acknowledged", self.journal.get(reindexed).state)
        self.assertEqual("prepared", self.journal.get(edited).state)
        self.assertEqual([reservation_key(edited)], [entry.key for entry in self.journal.pending([edited, reindexed])])
        evidence = self.journal.get(reindexed).events[-1]["evidence"]
        self.assertEqual(str((screenshots / "00025-0.png").resolve()), evidence["screenshots"][0])
        self.journal.import_prior_run(*source_paths)
        self.assertEqual(2, len(self.journal.get(reindexed).events))

    def test_import_mismatched_result_is_atomic_and_never_uses_index_alone(self):
        first, second = row(), row(index=2, chat_name="B 학생")
        bad = {**self.receipt(second), "chat_name": "다른 학생"}
        paths = self.prior_files([first, second], [self.receipt(first), bad])
        with self.assertRaises(ScheduleJournalError):
            self.journal.import_prior_run(*paths)
        self.assertEqual((), self.journal.records())

    def test_import_requires_explicit_exact_body_evidence(self):
        receipt = self.receipt(row())
        receipt.pop("body_exact_before_registration")
        paths = self.prior_files([row()], [receipt])
        with self.assertRaises(ScheduleJournalError):
            self.journal.import_prior_run(*paths)

    def test_verified_import_is_not_downgraded_by_older_ack(self):
        self.journal.import_prior_run(*self.prior_files([row()], [self.receipt(row(), verified=True)]))
        self.journal.import_prior_run(*self.prior_files([row()], [self.receipt(row())]))
        self.assertEqual("verified", self.journal.get(row()).state)


if __name__ == "__main__":
    unittest.main()
