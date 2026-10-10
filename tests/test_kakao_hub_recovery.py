import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kakao_run_journal import RunJournal, RunJournalError
from kakao_direct import DirectSendError, DirectSendUnknownError
from kakao_send_run import SendRunProgress
from kakao_send_service import SendPorts
from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp, SendRunRequest


class Var:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def fake_app():
    app = KakaoSenderApp.__new__(KakaoSenderApp)
    app.rows = [MessageRow(2, "existing", "keep unless restored")]
    app.results = []
    app.preview_edit_locked = lambda: False
    app.cancel_auto_chat_matches = Mock()
    app.cancel_cell_edit = Mock()
    app.populate_table = Mock()
    app.update_row_status = Mock()
    app.refresh_preview_count = Mock()
    app.update_summary = Mock()
    app.log = Mock()
    app.post = Mock()
    app.common_schedule_var = Var(True)
    app.schedule_at_var = Var("2035-01-01 12:00")
    app.progress_var = Var(100)
    app.file_var = Var("old.csv")
    app.status_var = Var("")
    app.start_sending = Mock(side_effect=AssertionError("restore must never send"))
    return app


def recovery_fixture():
    return SimpleNamespace(
        run_id="source-run", is_active=False, source_path="C:/input/source.csv", metadata={},
        selected=tuple((index, MessageRow(index + 2, f"person-{index}", f"body-{index}"))
                       for index in (4, 9, 20, 31, 42)),
        pending_row_indexes=(42,),
        results=(
            {"row_index": 4, "status": "sent"},
            {"row_index": 9, "status": "unknown", "retry_transport": "macro"},
            {"row_index": 20, "row": 22, "chat_name": "person-20", "status": "error", "retry_transport": "macro", "failure_stage": "direct"},
            {"row_index": 31, "status": "error", "retry_transport": "none", "failure_stage": "mapping"},
        ),
    )


class HubRecoveryTests(unittest.TestCase):
    def test_reservation_only_csv_uses_main_window_search_shortcut(self):
        app = fake_app()
        self.configure_runtime(app, Mock())
        app.common_schedule_var.set(False)
        scheduled = MessageRow(2, "A 고양외1 학생가", "예약 본문", scheduled_at=datetime(2035, 1, 1, 23, 0))

        request = app.build_send_run_request([(0, scheduled)])

        self.assertEqual(request.config["open_search_shortcut"], ["ctrl", "f"])
        self.assertEqual(request.common_schedule, None)
        self.assertEqual(request.config["search_box"]["x"], 200)

    def test_mixed_run_preserves_calibrated_search_method(self):
        app = fake_app()
        self.configure_runtime(app, Mock())
        app.common_schedule_var.set(False)
        scheduled = MessageRow(2, "first", "예약 본문", scheduled_at=datetime(2035, 1, 1, 23, 0))
        immediate = MessageRow(3, "second", "즉시 본문")

        request = app.build_send_run_request([(0, scheduled), (1, immediate)])

        self.assertFalse(request.config.get("open_search_shortcut"))

    def test_late_server_diagnostic_cannot_clear_new_generation_busy_state(self):
        app = fake_app()
        app._diagnostic_token = "current-dialog"
        app._diagnostic_busy = True
        app.handle_diagnostic_result(("previous-dialog", "late server result", True))
        self.assertTrue(app._diagnostic_busy)

    def test_journal_failure_latches_send_block_and_preserves_record_guidance(self):
        app = fake_app()
        app.handle_ui_event("journal_error", "전송 결과를 기록하지 못했습니다. 실행 기록을 확인하세요.")
        self.assertIn("실행 기록", app._journal_error)
        self.assertIn("재발송 전", app.status_var.get())
        app.active_run = None
        with patch("kakao_sender_gui.messagebox.showerror") as error:
            app.start_sending_after_preflight([(0, app.rows[0])])
        error.assert_called_once()

    def test_journal_is_terminal_before_gui_receives_completion(self):
        from kakao_send_run import SendRunCompleted
        with tempfile.TemporaryDirectory() as directory:
            app = fake_app()
            app.run_journal = RunJournal(Path(directory) / "journal.sqlite3")
            app._send_worker_impl = Mock()
            inspected = []
            def receive(event, payload):
                if event == "run_event" and isinstance(payload, SendRunCompleted):
                    recovery = app.run_journal.load_recovery(str(payload.run_id))
                    inspected.append((recovery.summary.status, recovery.is_active))
            app.post = receive
            request = SendRunRequest(selected=((0, app.rows[0]),), config={}, countdown=0,
                delay_scale=1.0, common_schedule=None, continue_on_error=True,
                confirm_each=False, direct_enabled=True)
            app.send_worker(request)
            self.assertEqual(inspected, [("completed", False)])

    def configure_runtime(self, app, journal):
        app.run_journal = journal
        app.coord_x_var = Var(200)
        app.coord_y_var = Var(300)
        app.config_data = {"search_box": {"x": 200, "y": 300}}
        app.direct_enabled_var = Var(True)
        app.countdown_var = Var(0)
        app.delay_scale_var = Var(1.0)
        app.continue_error_var = Var(True)
        app.confirm_each_var = Var(False)
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True})
        app.direct_mappings = {row.chat_name: str(number + 100) for number, row in enumerate(app.rows)}

    def run_journaled(self, app, request, bridge):
        events = []
        app.post = lambda event, payload: events.append((event, payload))
        app.create_direct_bridge = Mock(return_value=bridge)
        virtual_time = 0.0

        def wait_for_cancel(event, seconds):
            nonlocal virtual_time
            virtual_time += seconds
            return event.is_set()

        with (
            patch("kakao_sender_gui.SendPorts", side_effect=lambda **ports: SendPorts(
                **ports, monotonic=lambda: virtual_time, wait_for_cancel=wait_for_cancel,
            )),
            patch("kakao_sender_gui.pc_kakao_ui_lease", return_value=nullcontext()),
            patch("kakao_sender_gui.require_pyautogui", return_value=SimpleNamespace(PAUSE=0)),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender_gui.send_one") as macro,
            patch("kakao_sender_gui.send_scheduled_one", side_effect=AssertionError("unexpected schedule")),
            patch("kakao_sender_gui.return_to_recent_chat_top"),
        ):
            completed = app.send_worker(request)
        for event, payload in events:
            if event == "run_event" and isinstance(payload, SendRunProgress) and payload.kind == "journal_started":
                app.handle_ui_event(payload.kind, payload.payload)
        app.results = completed.legacy_results()
        app.advance_run_origins(str(completed.run_id), app.results)
        return completed, macro

    def test_pending_restore_excludes_sent_unknown_and_retry_rows(self):
        app = fake_app()
        self.assertTrue(app.restore_run_preview(recovery_fixture()))
        self.assertEqual([row.chat_name for row in app.rows], ["person-42"])
        self.assertEqual(app.results, [])
        self.assertEqual(app.file_path, Path("C:/input/source.csv"))
        self.assertEqual(app._recovery_of, "source-run")
        self.assertFalse(app._restored_retry_only)
        app.start_sending.assert_not_called()

    def test_retry_restore_only_imports_explicit_macro_error_and_remaps_index(self):
        app = fake_app()
        self.assertTrue(app.restore_run_preview(recovery_fixture(), retry_only=True))
        self.assertEqual([row.chat_name for row in app.rows], ["person-20"])
        self.assertEqual(len(app.results), 1)
        self.assertEqual(app.results[0]["row_index"], 0)
        self.assertEqual(app.results[0]["row"], 22)
        self.assertEqual(app.results[0]["retry_transport"], "macro")
        self.assertEqual(app.results[0]["failure_stage"], "direct")
        self.assertTrue(app._restored_retry_only)
        app.update_row_status.assert_called_once_with(0, "오류")
        app.start_sending.assert_not_called()

    def test_live_run_refuses_both_restore_modes_without_mutating_preview(self):
        for retry in (False, True):
            with self.subTest(retry=retry):
                app = fake_app()
                original = app.rows
                recovery = recovery_fixture()
                recovery.is_active = True
                with patch("kakao_sender_gui.messagebox.showinfo") as info:
                    self.assertFalse(app.restore_run_preview(recovery, retry_only=retry))
                info.assert_called_once()
                self.assertIs(app.rows, original)
                app.cancel_auto_chat_matches.assert_not_called()
                app.start_sending.assert_not_called()

    def test_effective_common_schedule_is_journaled_per_row_and_restored_without_global_fallback(self):
        app = fake_app()
        journal_run = Mock()
        app.run_journal = Mock()
        app.run_journal.begin_run.return_value = journal_run
        app._send_worker_impl = Mock()
        common = datetime(2035, 1, 2, 9, 30)
        own = datetime(2035, 1, 3, 10, 0)
        request = SendRunRequest(
            selected=((4, MessageRow(6, "common", "body")), (9, MessageRow(11, "own", "body", scheduled_at=own))),
            config={}, countdown=0, delay_scale=1.0, common_schedule=common,
            continue_on_error=True, confirm_each=False, direct_enabled=True,
        )
        with patch("kakao_sender_gui.pc_kakao_ui_lease", return_value=nullcontext()):
            completed = app.send_worker(request)
        self.assertIsNone(completed.failure)
        stored = app.run_journal.begin_run.call_args.args[1]
        self.assertEqual([row.scheduled_at for _, row in stored], [common, own])
        recovery = SimpleNamespace(
            run_id="scheduled-source", is_active=False, selected=stored, results=(),
            pending_row_indexes=(4, 9), source_path=None, metadata={"common_schedule": common.isoformat()},
        )
        self.assertTrue(app.restore_run_preview(recovery))
        self.assertEqual([row.scheduled_at for row in app.rows], [common, own])
        self.assertFalse(app.common_schedule_var.get())
        self.assertEqual(app.schedule_at_var.get(), "")
        app.start_sending.assert_not_called()

    def test_closed_diagnostic_dialog_does_not_receive_late_worker_text(self):
        app = fake_app()
        app._diagnostic_token = "old-dialog"
        app._diagnostic_busy = True
        app._diagnostic_dialog = SimpleNamespace(winfo_exists=lambda: False)
        app._diagnostic_text = Mock(side_effect=AssertionError("closed widget"))
        app.handle_diagnostic_result(("old-dialog", "late message", True))
        self.assertFalse(app._diagnostic_busy)
        self.assertEqual(app._diagnostic_text.mock_calls, [])

    def test_stale_diagnostic_generation_does_not_update_reopened_dialog(self):
        app = fake_app()
        app._diagnostic_token = "new-dialog"
        app._diagnostic_dialog = SimpleNamespace(winfo_exists=lambda: True)
        app._diagnostic_text = Mock()
        app.handle_diagnostic_result(("old-dialog", "late local result", False))
        self.assertEqual(app._diagnostic_text.mock_calls, [])

    def test_result_journal_failure_after_delivery_stops_without_creating_retry_record(self):
        app = fake_app()
        journal_run = Mock()
        # A transient write failure must not be converted into a transport failure
        # which saves on a second call and makes a delivered message retryable.
        journal_run.add_result.side_effect = [RunJournalError("simulated storage failure"), None]
        app.run_journal = Mock()
        app.run_journal.begin_run.return_value = journal_run
        bridge = Mock()
        bridge.send_message.return_value = {
            "success": True, "status_code": 0, "chat_id": "123",
            "log_id": "9001", "sent_at": 1800000000,
        }
        app.create_direct_bridge = Mock(return_value=bridge)
        request = SendRunRequest(
            selected=((0, MessageRow(2, "person", "body")),), config={}, countdown=0,
            delay_scale=1.0, common_schedule=None, continue_on_error=True,
            confirm_each=False, direct_enabled=True, chat_mappings=(("person", "123"),),
        )
        completed = app.send_worker(request)
        self.assertIsNotNone(completed.failure)
        self.assertEqual(completed.failure.exception_type, "RunJournalError")
        bridge.send_message.assert_called_once()
        self.assertEqual(journal_run.add_result.call_count, 1)
        self.assertEqual(journal_run.add_result.call_args.args[0]["status"], "direct_sent")
        self.assertEqual(journal_run.add_result.call_args.args[0]["log_id"], "9001")
        self.assertFalse(any(record.get("retry_transport") == "macro" for record in completed.legacy_results()))

    def test_failed_attempt_marker_stops_before_any_dispatch(self):
        app = fake_app()
        journal_run = Mock()
        journal_run.mark_attempt.side_effect = RunJournalError("simulated marker failure")
        app.run_journal = Mock()
        app.run_journal.begin_run.return_value = journal_run
        bridge = Mock()
        app.create_direct_bridge = Mock(return_value=bridge)
        request = SendRunRequest(
            selected=((0, MessageRow(2, "one", "body")), (1, MessageRow(3, "two", "body"))),
            config={}, countdown=0, delay_scale=1.0, common_schedule=None,
            continue_on_error=True, confirm_each=False, direct_enabled=True,
            chat_mappings=(("one", "123"), ("two", "456")),
        )
        completed = app.send_worker(request)
        self.assertIsNotNone(completed.failure)
        bridge.send_message.assert_not_called()
        journal_run.mark_attempt.assert_called_once_with(0, "direct")
        journal_run.add_result.assert_not_called()

    def test_real_journal_restore_edit_delete_subset_failure_retry_and_new_row(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RunJournal(Path(directory) / "journal.sqlite3")
            original = (
                (5, MessageRow(12, "first", "first original")),
                (8, MessageRow(31, "deleted", "deleted original")),
                (9, MessageRow(77, "remaining", "remaining original")),
            )
            journal.begin_run("parent", original).finish("cancelled")
            app = fake_app()
            self.assertTrue(app.restore_run_preview(journal.load_recovery("parent")))
            self.configure_runtime(app, journal)
            # Preserve the first row's identity through an edit and a middle-row deletion.
            app.rows[0] = replace(app.rows[0], text="edited first")
            del app.rows[1]
            app.reset_preview_results()
            first_request = app.build_send_run_request([(0, app.rows[0])])
            self.assertEqual(first_request.recovery_sources, ((0, "parent", 5),))
            rejecting = Mock()
            rejecting.send_message.side_effect = DirectSendError("explicit rejection")
            first, _ = self.run_journaled(app, first_request, rejecting)
            self.assertIsNone(first.failure)
            self.assertEqual(first.legacy_results()[0]["retry_transport"], "macro")
            self.assertEqual(journal.load_recovery("parent").pending_row_indexes, (8, 9))
            first_id = str(first.run_id)
            self.assertEqual(app._recovery_sources_by_number[12], (first_id, 0))

            # Retrying must claim the failed child, not re-claim its transferred parent.
            app.reset_preview_results()
            retry_request = app.build_send_run_request([(0, app.rows[0])], send_mode="macro_retry", direct_enabled_override=False)
            self.assertEqual(retry_request.recovery_sources, ((0, first_id, 0),))
            retry, macro = self.run_journaled(app, retry_request, Mock())
            self.assertIsNone(retry.failure)
            macro.assert_called_once()
            self.assertEqual(retry.legacy_results()[0]["status"], "sent")
            self.assertEqual(journal.load_recovery(first_id).results[0]["status"], "transferred")
            self.assertFalse(app._restored_retry_only)

            # A new row has no source claim; a remaining restored row keeps its original claim.
            new_number = app.next_preview_row_number()
            self.assertGreater(new_number, 77)
            app.rows.append(MessageRow(new_number, "fresh", "new body"))
            app.direct_mappings["fresh"] = "999"
            app.reset_preview_results()
            remainder = app.build_send_run_request([(1, app.rows[1]), (2, app.rows[2])])
            self.assertEqual(remainder.recovery_sources, ((1, "parent", 9),))
            accepting = Mock()
            accepting.send_message.side_effect = lambda *, text, chat_id: {
                "success": True, "status_code": 0, "chat_id": chat_id,
                "log_id": str(9000 + accepting.send_message.call_count), "sent_at": 1800000000,
            }
            final, _ = self.run_journaled(app, remainder, accepting)
            self.assertIsNone(final.failure)
            self.assertEqual(accepting.send_message.call_count, 2)
            self.assertEqual([item["status"] for item in final.legacy_results()], ["direct_sent", "direct_sent"])
            self.assertEqual([item["log_id"] for item in final.legacy_results()], ["9001", "9002"])
            self.assertEqual(journal.load_recovery("parent").pending_row_indexes, (8,))

            # Replaying a previously successful restored row fails before any transport call.
            replay = app.build_send_run_request([(0, app.rows[0])])
            forbidden = Mock()
            blocked, _ = self.run_journaled(app, replay, forbidden)
            self.assertIsNotNone(blocked.failure)
            self.assertEqual(blocked.failure.exception_type, "RunJournalError")
            forbidden.send_message.assert_not_called()

    def test_real_journal_batch_stop_recovers_only_unattempted_following_row(self):
        for failure, expected_status, retry_indexes in (
            (DirectSendError(code="server_rejected", status_code=-777), "error", (0,)),
            (DirectSendUnknownError(), "unknown", ()),
        ):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                app = fake_app()
                app.rows = [MessageRow(2, "first", "body one"), MessageRow(3, "later", "body two")]
                journal = RunJournal(Path(directory) / "journal.sqlite3")
                self.configure_runtime(app, journal)
                app.common_schedule_var.set(False)
                request = app.build_send_run_request(list(enumerate(app.rows)))
                bridge = Mock()
                bridge.send_message.side_effect = failure

                completed, macro = self.run_journaled(app, request, bridge)

                self.assertIsNone(completed.failure)
                self.assertEqual([item["status"] for item in completed.legacy_results()], [expected_status, "stopped"])
                bridge.send_message.assert_called_once_with(text="body one", chat_id="100")
                macro.assert_not_called()
                recovery = journal.load_recovery(str(completed.run_id))
                # Persisted stopped results are resumable, distinct from missing-result pending rows.
                self.assertEqual(recovery.pending_row_indexes, ())
                self.assertEqual(recovery.results[1]["failure_stage"], "direct_batch_not_attempted")
                self.assertEqual(recovery.retryable_row_indexes, retry_indexes)
                self.assertEqual(recovery.resumable_row_indexes, retry_indexes + (1,))
                restored = fake_app()
                self.assertTrue(restored.restore_run_preview(recovery))
                self.assertEqual(restored.rows, [app.rows[1]])
                restored.start_sending.assert_not_called()

    def test_new_row_number_does_not_reuse_deleted_recovery_or_recent_run_identity(self):
        app = fake_app()
        app._recovery_sources_by_number = {70: ("old-parent", 2)}
        app._last_run_sources_by_number = {90: ("recent-child", 3)}
        self.assertEqual(app.next_preview_row_number(), 91)


if __name__ == "__main__":
    unittest.main()
