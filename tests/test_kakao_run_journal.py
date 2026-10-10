import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from kakao_run_journal import RunJournal, RunJournalError, default_journal_path
from kakao_sender import MessageRow


ROOT = Path(__file__).resolve().parents[1]


class RunJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal" / "run-journal.sqlite3"
        self.journal = RunJournal(self.path)

    def rows(self):
        return (
            (2, MessageRow(12, "복구 대상", "고유 본문-복구-923", (Path("C:/files/document.pdf"),), False)),
            (7, MessageRow(17, "예약 대상", "예약 본문", scheduled_at=datetime(2030, 1, 2, 9, 30))),
        )

    def test_round_trip_preserves_sparse_rows_document_and_metadata(self):
        run = self.journal.begin_run("round-trip", self.rows(), "C:/input/students.csv", {
            "send_mode": "normal", "routes": ["macro", "scheduled"], "common_schedule": None,
        })
        run.finish("failed")
        recovered = RunJournal(self.path).load_recovery("round-trip")
        self.assertEqual(recovered.selected, self.rows())
        self.assertEqual(recovered.source_path, "C:/input/students.csv")
        self.assertEqual(recovered.metadata["routes"], ["macro", "scheduled"])
        self.assertEqual(recovered.pending_row_indexes, (2, 7))
        self.assertEqual(recovered.resumable_row_indexes, (2, 7))
        self.assertEqual(recovered.results, ())
        self.assertEqual(recovered.run_id, "round-trip")
        self.assertEqual(recovered.summary.row_count, 2)

    def test_real_process_exit_after_attempt_recovers_unknown_not_retry(self):
        script = r'''
import os, sys
from pathlib import Path
from kakao_run_journal import RunJournal
from kakao_sender import MessageRow
journal = RunJournal(Path(sys.argv[1]))
run = journal.begin_run("crashed", [(2, MessageRow(3, "first", "private first")), (7, MessageRow(8, "second", "private second")), (10, MessageRow(11, "pending", "not attempted")), (11, MessageRow(12, "success", "delivered"))])
run.mark_attempt(7, "direct")
run.add_result({"row_index":7,"row":8,"chat_name":"second","status":"error","failure_stage":"direct","retry_transport":"macro"})
run.mark_attempt(11, "direct")
run.add_result({"row_index":11,"row":12,"chat_name":"success","status":"direct_sent"})
run.mark_attempt(2, "direct")
os._exit(23)
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.path)], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr)
        recovered = RunJournal(self.path).load_recovery("crashed")
        records = {record["row_index"]: record for record in recovered.results}
        self.assertEqual(records[2]["status"], "unknown")
        self.assertEqual(records[2]["retry_transport"], "none")
        self.assertEqual(records[7]["retry_transport"], "macro")
        self.assertEqual(records[11]["status"], "direct_sent")
        self.assertEqual(recovered.pending_row_indexes, (10,))
        self.assertEqual(recovered.failed_row_indexes, (7,))
        self.assertEqual(recovered.retryable_row_indexes, (7,))
        self.assertEqual(recovered.resumable_row_indexes, (7, 10))

    def test_success_unknown_mapping_failure_and_skip_never_resume(self):
        rows = [(index, MessageRow(index + 2, "person", "body")) for index in range(5)]
        run = self.journal.begin_run("filtered", rows)
        records = [
            {"row_index": 0, "status": "sent", "retry_safe": True},
            {"row_index": 1, "status": "unknown", "retry_transport": "macro", "retry_safe": True},
            {"row_index": 2, "status": "error", "failure_stage": "mapping", "retry_transport": "none"},
            {"row_index": 3, "status": "skipped"},
            {"row_index": 4, "status": "error", "retry_transport": "macro"},
        ]
        for record in records:
            run.add_result(record)
        run.finish()
        recovered = self.journal.load_recovery("filtered")
        self.assertEqual(recovered.failed_row_indexes, (2, 4))
        self.assertEqual(recovered.resumable_row_indexes, (4,))

    def test_completion_and_acknowledgement_do_not_delete_history(self):
        run = self.journal.begin_run("done", self.rows())
        run.add_result({"row_index": 2, "status": "sent"})
        run.add_result({"row_index": 7, "status": "scheduled"})
        run.finish()
        self.assertEqual(self.journal.list_recoverable_runs(), [])
        self.journal.begin_run("pending", self.rows()).finish("failed")
        self.assertEqual([item.run_id for item in self.journal.list_recoverable_runs()], ["pending"])
        self.journal.acknowledge("pending")
        self.assertEqual(self.journal.list_recoverable_runs(), [])
        self.assertEqual({item.run_id for item in self.journal.list_runs()}, {"pending", "done"})
        self.assertEqual(self.journal.load_recovery("pending").selected, self.rows())
        self.journal.acknowledge("pending", False)
        self.assertEqual([item.run_id for item in self.journal.list_recoverable_runs()], ["pending"])

    def test_duplicate_attempt_and_result_overwrite_are_rejected(self):
        run = self.journal.begin_run("once", self.rows())
        run.mark_attempt(2, "macro")
        with self.assertRaises(RunJournalError):
            run.mark_attempt(2, "macro")
        record = {"row_index": 2, "status": "sent"}
        run.add_result(record)
        run.add_result(record)
        with self.assertRaises(RunJournalError):
            run.add_result({"row_index": 2, "status": "error"})
        with self.assertRaises(RunJournalError):
            run.mark_attempt(2, "macro")
        self.assertEqual(self.journal.load_recovery("once").results, (record,))

    def test_failed_marker_persistence_prevents_transport_call(self):
        run = self.journal.begin_run("closed", self.rows())
        run.finish("failed")
        sent = []
        with self.assertRaises(RunJournalError):
            run.mark_attempt(2, "direct")
            sent.append("must not dispatch")
        self.assertEqual(sent, [])
        self.assertEqual(self.journal.load_recovery("closed").pending_row_indexes, (2, 7))

    def test_missing_row_and_duplicate_source_index_are_rejected(self):
        with self.assertRaises(RunJournalError):
            self.journal.begin_run("duplicate", (self.rows()[0], self.rows()[0]))
        run = self.journal.begin_run("valid", self.rows())
        with self.assertRaises(RunJournalError):
            run.mark_attempt(99, "direct")
        with self.assertRaises(RunJournalError):
            run.add_result({"row_index": 99, "status": "sent"})

    def test_corrupt_and_truncated_databases_fail_without_payload_in_error(self):
        for number, content in enumerate((b"", b"not sqlite secret=MY-TOKEN", b"SQLite format 3\0\0")):
            path = Path(self.directory.name) / f"broken-{number}.sqlite3"
            path.write_bytes(content)
            with self.assertRaises(RunJournalError) as caught:
                RunJournal(path)
            self.assertNotIn("MY-TOKEN", str(caught.exception))
            self.assertEqual(path.read_bytes(), content)

    def test_missing_table_is_not_recreated_as_empty_history(self):
        self.journal.begin_run("existing", self.rows())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE results")
        with self.assertRaises(RunJournalError):
            RunJournal(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='results'").fetchone())

    def test_corrupt_document_is_not_silently_skipped_during_startup(self):
        self.journal.begin_run("corrupt", self.rows()).finish("failed")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE runs SET document=? WHERE run_id='corrupt'", (b"broken SECRET",))
        with self.assertRaises(RunJournalError) as caught:
            self.journal.list_recoverable_runs()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_corrupt_result_fails_closed(self):
        run = self.journal.begin_run("corrupt-result", self.rows())
        run.add_result({"row_index": 2, "status": "sent"})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE results SET payload=?", (b"broken",))
        with self.assertRaises(RunJournalError):
            self.journal.load_recovery("corrupt-result")

    def test_credentials_are_rejected_and_never_echoed(self):
        for metadata in ({"access_token": "never-save-me"}, {"routes": [{"authorization": "never-save-me"}]}, {"direct_config": {"enabled": True}}):
            with self.assertRaises(RunJournalError) as caught:
                self.journal.begin_run("secret", self.rows(), metadata=metadata)
            self.assertNotIn("never-save-me", str(caught.exception))
        self.assertEqual(self.journal.list_runs(), [])

    @unittest.skipUnless(os.name == "nt", "Windows payloads require DPAPI")
    def test_windows_storage_has_no_plaintext_document_or_result(self):
        run = self.journal.begin_run("protected", self.rows(), "C:/private-input-794/student.csv")
        run.add_result({"row_index": 2, "status": "sent", "detail": "private-result-329"})
        for path in self.path.parent.iterdir():
            if path.is_file():
                content = path.read_bytes()
                for secret in ("고유 본문-복구-923", "복구 대상", "private-input-794", "private-result-329"):
                    self.assertNotIn(secret.encode("utf-8"), content)
        self.assertEqual(self.journal.load_recovery("protected").selected, self.rows())

    @unittest.skipIf(os.name == "nt", "POSIX private-filesystem fallback")
    def test_posix_permissions_are_private(self):
        self.journal.begin_run("private", self.rows())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_default_path_matches_existing_app_storage(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(Path(self.directory.name))}):
            self.assertEqual(default_journal_path(), Path(self.directory.name) / "KakaoSender" / "run-journal.sqlite3")

    def test_same_process_live_run_is_inspectable_but_not_recoverable(self):
        run = self.journal.begin_run("live-local", self.rows())
        run.mark_attempt(2, "direct")
        recovery = RunJournal(self.path).load_recovery("live-local")
        self.assertTrue(recovery.is_active)
        self.assertEqual(recovery.summary.owner_pid, os.getpid())
        self.assertTrue(recovery.summary.owner_start)
        self.assertEqual(recovery.pending_row_indexes, (7,))
        self.assertEqual(recovery.resumable_row_indexes, ())
        self.assertEqual(self.journal.list_recoverable_runs(), [])
        run.finish("cancelled")
        recovery = self.journal.load_recovery("live-local")
        self.assertFalse(recovery.is_active)
        self.assertEqual(recovery.resumable_row_indexes, (7,))

    def test_another_living_process_cannot_be_recovered_until_it_exits(self):
        script = r'''
import os, sys
from kakao_run_journal import RunJournal
from kakao_sender import MessageRow
run = RunJournal(sys.argv[1]).begin_run("live-child", [(0, MessageRow(2,"sending","private")),(1,MessageRow(3,"pending","private"))])
run.mark_attempt(0,"direct")
print("READY", os.getpid(), flush=True)
sys.stdin.readline()
os._exit(29)
'''
        with subprocess.Popen([sys.executable, "-B", "-c", script, str(self.path)], cwd=ROOT,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as process:
            try:
                ready, owner_pid = process.stdout.readline().strip().split()
                self.assertEqual(ready, "READY")
                recovery = self.journal.load_recovery("live-child")
                self.assertTrue(recovery.is_active)
                # A Windows venv python.exe can be a launcher whose child runs
                # this code. Compare the PID emitted by the actual interpreter.
                self.assertEqual(recovery.summary.owner_pid, int(owner_pid))
                self.assertEqual(recovery.resumable_row_indexes, ())
                self.assertEqual(self.journal.list_recoverable_runs(), [])
                process.communicate("exit\n", timeout=10)
                self.assertEqual(process.returncode, 29)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
        recovered = RunJournal(self.path).load_recovery("live-child")
        self.assertFalse(recovered.is_active)
        self.assertEqual(recovered.results[0]["status"], "unknown")
        self.assertEqual(recovered.resumable_row_indexes, (1,))
        self.assertEqual([item.run_id for item in self.journal.list_recoverable_runs()], ["live-child"])

    def test_pid_reuse_does_not_lock_abandoned_run_to_unrelated_process(self):
        self.journal.begin_run("reused-pid", self.rows())
        recorded = self.journal.load_recovery("reused-pid").summary.owner_start
        with patch("kakao_run_journal._process_identity", return_value=("alive", recorded + "-new-process")):
            recovered = self.journal.load_recovery("reused-pid")
            self.assertFalse(recovered.is_active)
            self.assertEqual(recovered.resumable_row_indexes, (2, 7))

    def test_inaccessible_owner_is_conservatively_active(self):
        self.journal.begin_run("protected-owner", self.rows())
        with patch("kakao_run_journal._process_identity", return_value=("unknown", None)):
            recovered = self.journal.load_recovery("protected-owner")
            self.assertTrue(recovered.is_active)
            self.assertEqual(recovered.resumable_row_indexes, ())
            self.assertEqual(self.journal.list_recoverable_runs(), [])

    def test_version_one_running_record_without_owner_is_not_assumed_dead(self):
        self.journal.begin_run("legacy", self.rows())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE runs DROP COLUMN owner_pid")
            db.execute("ALTER TABLE runs DROP COLUMN owner_start")
            db.execute("PRAGMA user_version=1")
        migrated = RunJournal(self.path)
        recovered = migrated.load_recovery("legacy")
        self.assertTrue(recovered.is_active)
        self.assertEqual(recovered.resumable_row_indexes, ())
        self.assertEqual(migrated.list_recoverable_runs(), [])

    def test_recovery_claim_permanently_transfers_parent_row_to_child(self):
        self.journal.begin_run("parent", self.rows()).finish("cancelled")
        child = self.journal.begin_run("child", ((0, self.rows()[0][1]),), metadata={
            "recovery_of": "parent", "recovery_row_indexes": {"0": 2},
        })
        parent = self.journal.load_recovery("parent")
        self.assertEqual(parent.pending_row_indexes, (7,))
        self.assertEqual(parent.resumable_row_indexes, (7,))
        self.assertEqual(parent.results[0]["status"], "transferred")
        self.assertEqual(parent.results[0]["child_run_id"], "child")
        self.assertEqual(parent.results[0]["child_row_index"], 0)
        child.finish("cancelled")
        self.assertEqual(self.journal.load_recovery("child").resumable_row_indexes, (0,))
        with self.assertRaises(RunJournalError):
            self.journal.begin_run("duplicate-child", ((0, self.rows()[0][1]),), metadata={
                "recovery_of": "parent", "recovery_row_indexes": {"0": 2},
            })
        self.assertNotIn("duplicate-child", {item.run_id for item in self.journal.list_runs()})

    def test_recovery_claim_rejects_live_success_unknown_and_nonretryable_rows(self):
        run = self.journal.begin_run("parent", self.rows())
        metadata = {"recovery_of": "parent", "recovery_row_indexes": {"0": 2}}
        with self.assertRaises(RunJournalError):
            self.journal.begin_run("live-child", ((0, self.rows()[0][1]),), metadata=metadata)
        run.add_result({"row_index": 2, "status": "sent"})
        run.add_result({"row_index": 7, "status": "unknown", "retry_transport": "macro"})
        run.finish()
        for index in (2, 7):
            with self.assertRaises(RunJournalError):
                self.journal.begin_run(f"bad-child-{index}", ((0, self.rows()[0][1]),), metadata={
                    "recovery_of": "parent", "recovery_row_indexes": {"0": index},
                })

    def test_only_macro_retryable_errors_or_explicit_stopped_results_can_transfer(self):
        run = self.journal.begin_run("parent", self.rows())
        run.add_result({"row_index": 2, "status": "error", "retry_transport": "macro"})
        run.add_result({"row_index": 7, "status": "stopped"})
        run.finish("failed")
        self.journal.begin_run("child", ((0, self.rows()[0][1]), (1, self.rows()[1][1])), metadata={
            "recovery_of": "parent", "recovery_row_indexes": {"0": 2, "1": 7},
        })
        self.assertEqual(self.journal.load_recovery("parent").resumable_row_indexes, ())
        self.assertNotIn("parent", {item.run_id for item in self.journal.list_recoverable_runs()})

    def test_concurrent_processes_cannot_claim_same_parent_row(self):
        self.journal.begin_run("parent", self.rows()).finish("failed")
        script = r'''
import json, sys
from kakao_run_journal import RunJournal, RunJournalError
from kakao_sender import MessageRow
journal = RunJournal(sys.argv[1])
print("READY", flush=True)
sys.stdin.readline()
try:
    journal.begin_run(sys.argv[2], [(0,MessageRow(12,"recovered","body"))], metadata={"recovery_sources":{"0":{"run_id":"parent","row_index":2}}})
    print(json.dumps({"claimed":True,"run_id":sys.argv[2]}), flush=True)
except RunJournalError:
    print(json.dumps({"claimed":False,"run_id":sys.argv[2]}), flush=True)
'''
        processes = [subprocess.Popen([sys.executable, "-B", "-c", script, str(self.path), name], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for name in ("claim-a", "claim-b")]
        observations = []
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), "READY")
            for process in processes:
                process.stdin.write("go\n")
                process.stdin.flush()
            for process in processes:
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                observations.append(json.loads(output.strip()))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        winners = [item["run_id"] for item in observations if item["claimed"]]
        self.assertEqual(len(winners), 1)
        parent = self.journal.load_recovery("parent")
        self.assertEqual(parent.results[0]["child_run_id"], winners[0])
        self.assertEqual(self.journal.load_recovery(winners[0]).resumable_row_indexes, (0,))

    def test_child_crash_before_attempt_can_only_resume_from_child(self):
        self.journal.begin_run("parent", self.rows()).finish("cancelled")
        script = r'''
import os, sys
from kakao_run_journal import RunJournal
from kakao_sender import MessageRow
RunJournal(sys.argv[1]).begin_run("crashed-child", [(0,MessageRow(12,"person","body"))], metadata={"recovery_of":"parent","recovery_row_indexes":{"0":2}})
os._exit(31)
'''
        exited = subprocess.run([sys.executable, "-B", "-c", script, str(self.path)], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(exited.returncode, 31, exited.stderr)
        self.assertEqual(self.journal.load_recovery("crashed-child").resumable_row_indexes, (0,))
        with self.assertRaises(RunJournalError):
            self.journal.begin_run("replay-parent", ((0, self.rows()[0][1]),), metadata={
                "recovery_of": "parent", "recovery_row_indexes": {"0": 2},
            })
        grandchild = self.journal.begin_run("grandchild", ((5, self.rows()[0][1]),), metadata={
            "recovery_of": "crashed-child", "recovery_row_indexes": {"5": 0},
        })
        grandchild.finish("cancelled")
        self.assertEqual(self.journal.load_recovery("crashed-child").resumable_row_indexes, ())
        self.assertEqual(self.journal.load_recovery("grandchild").resumable_row_indexes, (5,))

    def test_multiple_parent_sources_and_new_rows_can_share_one_child_run(self):
        self.journal.begin_run("first-parent", self.rows()).finish("cancelled")
        self.journal.begin_run("second-parent", self.rows()).finish("failed")
        selected = ((0, self.rows()[0][1]), (4, self.rows()[0][1]), (9, MessageRow(300, "new", "new body")))
        run = self.journal.begin_run("mixed-child", selected, metadata={"recovery_sources": {
            "0": {"run_id": "first-parent", "row_index": 2},
            "4": {"run_id": "second-parent", "row_index": 2},
        }})
        run.finish("cancelled")
        self.assertEqual(self.journal.load_recovery("first-parent").pending_row_indexes, (7,))
        self.assertEqual(self.journal.load_recovery("second-parent").pending_row_indexes, (7,))
        self.assertEqual(self.journal.load_recovery("mixed-child").resumable_row_indexes, (0, 4, 9))
        self.assertEqual(self.journal.load_recovery("mixed-child").metadata["recovery_sources"]["4"]["run_id"], "second-parent")

    def test_partial_recovered_failure_retries_from_child_alongside_unclaimed_original_row(self):
        self.journal.begin_run("original", self.rows()).finish("cancelled")
        partial = self.journal.begin_run("partial", ((0, self.rows()[0][1]),), metadata={
            "recovery_sources": {"0": {"run_id": "original", "row_index": 2}},
        })
        partial.mark_attempt(0, "direct")
        partial.add_result({"row_index": 0, "status": "error", "retry_transport": "macro"})
        partial.finish()
        next_run = self.journal.begin_run("retry-and-remaining", ((0, self.rows()[0][1]), (1, self.rows()[1][1])), metadata={
            "recovery_sources": {
                "0": {"run_id": "partial", "row_index": 0},
                "1": {"run_id": "original", "row_index": 7},
            },
        })
        next_run.finish("cancelled")
        self.assertEqual(self.journal.load_recovery("original").resumable_row_indexes, ())
        self.assertEqual(self.journal.load_recovery("partial").resumable_row_indexes, ())
        self.assertEqual(self.journal.load_recovery("retry-and-remaining").resumable_row_indexes, (0, 1))

    def test_new_sources_take_precedence_over_legacy_provenance(self):
        self.journal.begin_run("actual-parent", self.rows()).finish("failed")
        child = self.journal.begin_run("preferred-new", ((0, self.rows()[0][1]),), metadata={
            "recovery_of": "old-provenance-not-used", "recovery_row_indexes": {"99": 999},
            "recovery_sources": {"0": {"run_id": "actual-parent", "row_index": 2}},
        })
        child.finish("cancelled")
        self.assertEqual(self.journal.load_recovery("actual-parent").pending_row_indexes, (7,))
        new_only = self.journal.begin_run("brand-new", self.rows(), metadata={
            "recovery_of": "old-provenance-not-used", "recovery_sources": {},
        })
        new_only.finish("cancelled")
        self.assertEqual(self.journal.load_recovery("brand-new").pending_row_indexes, (2, 7))

    def test_new_source_mapping_rejects_duplicate_pairs_and_nonselected_child(self):
        self.journal.begin_run("parent", self.rows()).finish("failed")
        selected = ((0, self.rows()[0][1]), (1, self.rows()[1][1]))
        invalid_sources = (
            {"0": {"run_id": "parent", "row_index": 2}, "1": {"run_id": "parent", "row_index": 2}},
            {"42": {"run_id": "parent", "row_index": 2}},
            {"00": {"run_id": "parent", "row_index": 2}},
            {"0": {"run_id": "parent", "row_index": True}},
            {"0": {"run_id": "parent", "row_index": 2, "extra": "unexpected"}},
        )
        for number, sources in enumerate(invalid_sources):
            with self.subTest(number=number), self.assertRaises(RunJournalError):
                self.journal.begin_run(f"invalid-sources-{number}", selected, metadata={"recovery_sources": sources})
        self.assertEqual(self.journal.load_recovery("parent").pending_row_indexes, (2, 7))
        self.assertEqual({item.run_id for item in self.journal.list_runs()}, {"parent"})

    def test_mixed_parent_validation_failure_does_not_partially_claim_first_parent(self):
        self.journal.begin_run("safe-parent", self.rows()).finish("failed")
        self.journal.begin_run("live-parent", self.rows())
        with self.assertRaises(RunJournalError):
            self.journal.begin_run("invalid-mixed-child", ((0, self.rows()[0][1]), (1, self.rows()[1][1])), metadata={
                "recovery_sources": {
                    "0": {"run_id": "safe-parent", "row_index": 2},
                    "1": {"run_id": "live-parent", "row_index": 7},
                },
            })
        self.assertEqual(self.journal.load_recovery("safe-parent").pending_row_indexes, (2, 7))
        self.assertNotIn("invalid-mixed-child", {item.run_id for item in self.journal.list_runs()})

    def test_live_run_cannot_be_acknowledged_before_its_result_is_known(self):
        run = self.journal.begin_run("not-finished", self.rows())
        with self.assertRaises(RunJournalError):
            self.journal.acknowledge("not-finished")
        self.assertFalse(self.journal.load_recovery("not-finished").summary.acknowledged)
        run.mark_attempt(2, "direct")
        run.finish("failed")
        self.assertEqual([item.run_id for item in self.journal.list_recoverable_runs()], ["not-finished"])
        self.journal.acknowledge("not-finished")
        self.assertEqual(self.journal.list_recoverable_runs(), [])

    def test_recording_new_work_clears_legacy_acknowledgement(self):
        run = self.journal.begin_run("legacy-ack", self.rows())
        for operation in (lambda: run.mark_attempt(2, "direct"),
                          lambda: run.add_result({"row_index": 2, "status": "unknown"}),
                          lambda: run.finish("failed")):
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE runs SET acknowledged=1 WHERE run_id='legacy-ack'")
            operation()
            self.assertFalse(self.journal.load_recovery("legacy-ack").summary.acknowledged)


if __name__ == "__main__":
    unittest.main()
