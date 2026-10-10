"""Offline registration-review integration tests; no Tk or KakaoTalk surfaces."""
from __future__ import annotations

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
import io
from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import kakao_sender as sender
from kakao_run_journal import RunJournal, RunJournalError
from kakao_send_run import SendRunContext, SendRunId, SendRunSupervisor
from kakao_send_service import SendPorts, SendRunRequest, SendService
from kakao_sender_gui import KakaoSenderApp


def scheduled_rows():
    target = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
    return (
        sender.MessageRow(2, "review-fixture-one", "first fixture body", scheduled_at=target),
        sender.MessageRow(3, "review-fixture-two", "second fixture body", scheduled_at=target),
    )


def recovery_app():
    class Var:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = KakaoSenderApp.__new__(KakaoSenderApp)
    app.rows = []
    app.results = []
    app.preview_edit_locked = lambda: False
    for name in ("cancel_auto_chat_matches", "cancel_cell_edit", "populate_table", "update_row_status", "refresh_preview_count", "update_summary", "log", "post"):
        setattr(app, name, Mock())
    app.common_schedule_var = Var(True)
    app.schedule_at_var = Var("2035-01-01 12:00")
    app.progress_var = Var(100)
    app.file_var = Var("fixture.csv")
    app.status_var = Var("")
    app.start_sending = Mock(side_effect=AssertionError("restoring a fixture must never dispatch"))
    return app


class ScheduleServiceReviewTests(unittest.TestCase):
    def execute(self, registration, *, review=None, inspect_options=None, journal=None, row_count=2):
        rows = scheduled_rows()[:row_count]
        events = []
        supervisor = SendRunSupervisor(events.append, run_id=SendRunId("review-service-fixture"))
        contexts = []

        def register(_ui, _config, row, options, target):
            if inspect_options is not None:
                inspect_options(row, options, target, contexts[0])
            if isinstance(registration, Exception):
                raise registration
            return registration

        ports = SendPorts(
            bridge_factory=Mock(side_effect=AssertionError("no bridge or credentials in this fixture")),
            bridge_close=Mock(),
            confirm_row=Mock(return_value="send"),
            require_pyautogui=Mock(return_value=SimpleNamespace(PAUSE=0)),
            focus_kakao_window=Mock(return_value="KakaoTalk fixture"),
            send_one=Mock(side_effect=AssertionError("scheduled fixture must never send an immediate message")),
            send_scheduled_one=Mock(side_effect=register),
            return_to_recent_chat_top=Mock(),
            confirm_schedule_ready=review,
        )
        request = SendRunRequest(
            selected=tuple(enumerate(rows)), config={}, countdown=0, delay_scale=0,
            common_schedule=None, continue_on_error=True, confirm_each=False,
            direct_enabled=True,
        )

        def run(context):
            contexts.append(context)
            if journal is not None:
                journal_run = journal.begin_run(str(context.run_id), request.selected)
                context.observe_attempts(journal_run.mark_attempt)
                context.observe_results(journal_run.add_result)
                context.register_finalizer(lambda: journal_run.finish("cancelled" if context.cancel_requested else "completed"))
            SendService(ports).execute(request, context)

        completed = supervisor.run(run)
        ports.bridge_factory.assert_not_called()
        ports.send_one.assert_not_called()
        return completed, ports, contexts[0], rows

    def test_only_literal_verified_true_records_scheduled_and_continues(self):
        review = Mock(return_value=True)

        def inspect_options(row, options, target, context):
            self.assertIs(options.cancel_event, context.cancel_event)
            self.assertTrue(options.confirm_schedule_ready(row, target))
            review.assert_called_with(row, target, context)

        completed, ports, context, rows = self.execute(
            sender.ScheduleRegistrationResult(verified=True, detail="fixture verified"),
            review=review, inspect_options=inspect_options,
        )

        self.assertIsNone(completed.failure)
        self.assertEqual(ports.send_scheduled_one.call_count, 2)
        self.assertEqual(review.call_count, 2)
        for result in completed.legacy_results():
            self.assertEqual(result["status"], "scheduled")
            self.assertEqual(result["transport"], "scheduled")
            self.assertEqual(result["failure_stage"], "")
            self.assertEqual(result["retry_transport"], "none")
        self.assertIs(review.call_args.args[2], context)
        self.assertEqual(review.call_args.args[:2], (rows[1], rows[1].scheduled_at))

    def test_unverified_or_truthy_results_stay_unknown_and_stop_following_rows(self):
        verified_mock = MagicMock(spec=sender.ScheduleRegistrationResult)
        verified_mock.verified = True
        verified_mock.detail = "mock evidence is not registration verification"
        cases = (
            ("none", None),
            ("false", False),
            ("mock", MagicMock()),
            ("typed mock", MagicMock(spec=sender.ScheduleRegistrationResult)),
            ("typed verified mock", verified_mock),
            ("unverified", sender.ScheduleRegistrationResult()),
            ("truthy integer", sender.ScheduleRegistrationResult(verified=1)),
            ("truthy string", sender.ScheduleRegistrationResult(verified="true")),
        )
        for label, registration in cases:
            with self.subTest(registration=label):
                completed, ports, _context, _rows = self.execute(registration)
                self.assertIsNone(completed.failure)
                ports.send_scheduled_one.assert_called_once()
                ports.return_to_recent_chat_top.assert_not_called()
                results = completed.legacy_results()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["row_index"], 0)
                self.assertEqual(results[0]["status"], "unknown")
                self.assertEqual(results[0]["transport"], "scheduled")
                self.assertEqual(results[0]["failure_stage"], "schedule_verification")
                self.assertEqual(results[0]["retry_transport"], "none")
                self.assertIsInstance(results[0]["detail"], str)

    def test_missing_review_port_does_not_manufacture_approval(self):
        def inspect_options(_row, options, _target, _context):
            self.assertIsNone(options.confirm_schedule_ready)

        completed, ports, _context, _rows = self.execute(None, inspect_options=inspect_options)

        ports.send_scheduled_one.assert_called_once()
        self.assertEqual(completed.legacy_results()[0]["status"], "unknown")

    def test_schedule_exception_stops_following_rows_despite_continue_on_error(self):
        completed, ports, _context, _rows = self.execute(RuntimeError("fixture schedule failed"))

        ports.send_scheduled_one.assert_called_once()
        self.assertEqual(len(completed.legacy_results()), 1)
        self.assertEqual(completed.legacy_results()[0]["status"], "error")
        self.assertEqual(completed.legacy_results()[0]["transport"], "scheduled")
        self.assertEqual(completed.legacy_results()[0]["retry_transport"], "none")

    def test_explicit_not_submitted_error_preserves_a_cancelled_resumable_result(self):
        completed, ports, context, _rows = self.execute(
            sender.ScheduleNotSubmittedError("schedule_submit", ValueError("fixture: minimum five-minute lead time"))
        )

        ports.send_scheduled_one.assert_called_once()
        self.assertEqual(len(completed.legacy_results()), 1)
        self.assertEqual(completed.legacy_results()[0]["status"], "stopped")
        self.assertEqual(completed.legacy_results()[0]["transport"], "scheduled")
        self.assertEqual(completed.legacy_results()[0]["retry_transport"], "none")
        self.assertTrue(context.cancel_event.is_set())

    def test_pre_submit_stop_can_restore_edit_time_and_claim_a_child_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule-review.sqlite3"
            journal = RunJournal(path)
            completed, ports, _context, rows = self.execute(
                sender.ScheduleNotSubmittedError("schedule_submit", ValueError("fixture: minimum five-minute lead time")),
                journal=journal, row_count=1,
            )
            ports.send_scheduled_one.assert_called_once()
            parent_id = str(completed.run_id)
            recovery = RunJournal(path).load_recovery(parent_id)
            self.assertFalse(recovery.is_active)
            self.assertEqual(recovery.results[0]["status"], "stopped")
            self.assertEqual(recovery.resumable_row_indexes, (0,))

            app = recovery_app()
            self.assertTrue(app.restore_run_preview(recovery))
            self.assertEqual(app.rows, [rows[0]])
            self.assertFalse(app.common_schedule_var.get())
            corrected_time = rows[0].scheduled_at + timedelta(hours=1)
            app.rows[0] = replace(app.rows[0], scheduled_at=corrected_time)
            source_id, source_index = app._recovery_sources_by_number[app.rows[0].row_number]
            metadata = {"recovery_sources": {"0": {"run_id": source_id, "row_index": source_index}}}
            child = journal.begin_run("rescheduled-child", ((0, app.rows[0]),), metadata=metadata)
            child.finish("cancelled")

            child_recovery = journal.load_recovery("rescheduled-child")
            self.assertEqual(child_recovery.selected[0][1].scheduled_at, corrected_time)
            self.assertEqual(child_recovery.resumable_row_indexes, (0,))
            self.assertEqual(journal.load_recovery(parent_id).resumable_row_indexes, ())
            self.assertEqual(journal.load_recovery(parent_id).results[0]["status"], "transferred")
            with self.assertRaises(RunJournalError):
                journal.begin_run("duplicate-parent-claim", ((0, app.rows[0]),), metadata=metadata)
            app.start_sending.assert_not_called()

    def test_unknown_schedule_cannot_restore_or_claim_a_retry_child(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RunJournal(Path(directory) / "unknown-review.sqlite3")
            completed, ports, _context, rows = self.execute(
                sender.ScheduleRegistrationResult(), journal=journal, row_count=1,
            )
            ports.send_scheduled_one.assert_called_once()
            parent_id = str(completed.run_id)
            recovery = journal.load_recovery(parent_id)
            self.assertEqual(recovery.results[0]["status"], "unknown")
            self.assertEqual(recovery.resumable_row_indexes, ())
            app = recovery_app()
            with patch("kakao_sender_gui.messagebox.showinfo") as info:
                self.assertFalse(app.restore_run_preview(recovery))
            info.assert_called_once()
            self.assertEqual(app.rows, [])
            corrected_row = replace(rows[0], scheduled_at=rows[0].scheduled_at + timedelta(hours=1))
            with self.assertRaises(RunJournalError):
                journal.begin_run("unknown-retry-child", ((0, corrected_row),), metadata={
                    "recovery_sources": {"0": {"run_id": parent_id, "row_index": 0}},
                })
            app.start_sending.assert_not_called()


class ScheduleCliReviewTests(unittest.TestCase):
    def test_cli_review_requires_confirmation_word_and_rejects_interrupted_input(self):
        row = scheduled_rows()[0]
        for answer, accepted in (("확인", True), ("", False), ("yes", False), ("SEND", False), ("확인 완료", False), ("true", False)):
            with self.subTest(answer=answer), patch("builtins.input", return_value=answer), redirect_stdout(io.StringIO()):
                self.assertIs(sender.confirm_schedule_ready_cli(row, row.scheduled_at), accepted)
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption.__name__), patch("builtins.input", side_effect=interruption), redirect_stdout(io.StringIO()):
                self.assertFalse(sender.confirm_schedule_ready_cli(row, row.scheduled_at))

    def run_command(self, registration):
        rows = scheduled_rows()
        args = sender.build_parser().parse_args(["send", "--file", "fixture.csv", "--yes", "--countdown", "0", "--continue-on-error"])

        def register(_ui, _config, row, options, target):
            self.assertIs(options, args)
            self.assertIs(options.confirm_schedule_ready, sender.confirm_schedule_ready_cli)
            self.assertTrue(options.confirm_schedule_ready(row, target))
            if isinstance(registration, Exception):
                raise registration
            return registration

        with (
            patch.object(sender, "pc_kakao_ui_lease", return_value=nullcontext()),
            patch.object(sender, "load_config", return_value={}),
            patch.object(sender, "load_messages", return_value=rows),
            patch.object(sender, "require_pyautogui", return_value=SimpleNamespace(PAUSE=0)),
            patch.object(sender, "countdown"),
            patch.object(sender, "send_scheduled_one", side_effect=register) as schedule,
            patch.object(sender, "send_one", side_effect=AssertionError("no immediate sends")),
            patch.object(sender, "return_to_recent_chat_top") as cleanup,
            patch.object(sender, "write_log", return_value=Path("fixture-log.csv")) as write_log,
            patch("builtins.input", return_value="확인") as read_input,
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            exit_code = sender.command_send(args)
        return exit_code, write_log.call_args.args[1], schedule, cleanup, read_input

    def test_yes_skips_start_prompt_but_keeps_each_schedule_review(self):
        exit_code, results, schedule, _cleanup, read_input = self.run_command(sender.ScheduleRegistrationResult(verified=True))

        self.assertEqual(exit_code, 0)
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(read_input.call_count, 2)
        self.assertTrue(all("모든 값이 일치" in call.args[0] for call in read_input.call_args_list))
        self.assertEqual([result["status"] for result in results], ["scheduled", "scheduled"])

    def test_cli_unknown_stops_even_with_continue_on_error(self):
        for label, registration in (("none", None), ("mock", MagicMock()), ("typed mock", MagicMock(spec=sender.ScheduleRegistrationResult)), ("unverified", sender.ScheduleRegistrationResult()), ("truthy integer", sender.ScheduleRegistrationResult(verified=1))):
            with self.subTest(registration=label):
                exit_code, results, schedule, cleanup, read_input = self.run_command(registration)
                self.assertEqual(exit_code, 2)
                schedule.assert_called_once()
                cleanup.assert_not_called()
                read_input.assert_called_once()
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["status"], "unknown")
                self.assertEqual(results[0]["failure_stage"], "schedule_verification")
                self.assertEqual(results[0]["retry_transport"], "none")
                self.assertEqual(results[0]["transport"], "scheduled")
                self.assertIsInstance(results[0]["detail"], str)

    def test_cli_schedule_exception_stops_even_with_continue_on_error(self):
        exit_code, results, schedule, _cleanup, _read_input = self.run_command(RuntimeError("fixture schedule failed"))

        self.assertEqual(exit_code, 2)
        schedule.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(results[0]["transport"], "scheduled")
        self.assertEqual(results[0]["retry_transport"], "none")

    def test_cli_explicit_not_submitted_error_remains_stopped(self):
        exit_code, results, schedule, _cleanup, _read_input = self.run_command(
            sender.ScheduleNotSubmittedError("schedule_submit", ValueError("fixture: minimum five-minute lead time"))
        )

        self.assertEqual(exit_code, 2)
        schedule.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "stopped")
        self.assertEqual(results[0]["transport"], "scheduled")
        self.assertEqual(results[0]["retry_transport"], "none")


class ScheduleGuiQueueReviewTests(unittest.TestCase):
    def make_app(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.active_run = None
        app.post = Mock()
        app.show_schedule_review = Mock(return_value=True)
        return app

    def test_run_context_emits_review_and_accepts_only_literal_true(self):
        row = scheduled_rows()[0]
        for answer, expected in ((True, True), (False, False), (None, False), (1, False), ("확인", False), (MagicMock(), False)):
            with self.subTest(answer=type(answer).__name__, expected=expected):
                app = self.make_app()
                app.stop_event.set()  # The run owns its separate cancellation event.
                events = []

                def receive(event):
                    events.append(event)
                    event.payload[2].put(answer)

                context = SendRunContext(SendRunId("review-queue-fixture"), threading.Event(), receive)
                self.assertIs(app.confirm_schedule_ready(row, row.scheduled_at, context), expected)
                self.assertEqual(events[0].kind, "confirm_schedule_ready")
                self.assertEqual(events[0].payload[:2], (row, row.scheduled_at))
                app.post.assert_not_called()
                app.show_schedule_review.assert_not_called()

    def test_cancellation_before_review_emits_no_prompt(self):
        app = self.make_app()
        app.stop_event.set()
        row = scheduled_rows()[0]

        self.assertFalse(app.confirm_schedule_ready(row, row.scheduled_at))

        app.post.assert_not_called()

    def test_cancellation_during_queue_wait_beats_late_approval(self):
        for queued_answer in (True, queue.Empty):
            with self.subTest(answer=queued_answer):
                app = self.make_app()
                cancel_event = threading.Event()
                context = SendRunContext(SendRunId("cancel-review-fixture"), cancel_event, Mock())
                response = Mock()

                def receive_response(**_kwargs):
                    cancel_event.set()
                    if queued_answer is queue.Empty:
                        raise queue.Empty
                    return queued_answer

                response.get.side_effect = receive_response
                row = scheduled_rows()[0]
                with patch("kakao_sender_gui.queue.Queue", return_value=response):
                    self.assertFalse(app.confirm_schedule_ready(row, row.scheduled_at, context))
                response.get.assert_called_once_with(timeout=0.1)

    def test_handler_skips_review_when_the_active_run_is_cancelled(self):
        app = self.make_app()
        cancel_event = threading.Event()
        cancel_event.set()
        app.active_run = SimpleNamespace(cancel_event=cancel_event)
        response = queue.Queue(maxsize=1)
        row = scheduled_rows()[0]

        app.handle_ui_event("confirm_schedule_ready", (row, row.scheduled_at, response))

        self.assertFalse(response.get_nowait())
        app.show_schedule_review.assert_not_called()

    def test_handler_cancellation_wins_over_review_dialog_acceptance(self):
        app = self.make_app()
        cancel_event = threading.Event()
        app.active_run = SimpleNamespace(cancel_event=cancel_event)
        response = queue.Queue(maxsize=1)
        row = scheduled_rows()[0]

        def accept_after_cancel(*_args):
            cancel_event.set()
            return True

        app.show_schedule_review.side_effect = accept_after_cancel
        app.handle_ui_event("confirm_schedule_ready", (row, row.scheduled_at, response))

        app.show_schedule_review.assert_called_once_with(row, row.scheduled_at, cancel_event)
        self.assertFalse(response.get_nowait())

    def test_handler_always_unblocks_worker_when_review_dialog_raises(self):
        app = self.make_app()
        app.show_schedule_review.side_effect = RuntimeError("fake dialog failed")
        response = queue.Queue(maxsize=1)
        row = scheduled_rows()[0]

        app.handle_ui_event("confirm_schedule_ready", (row, row.scheduled_at, response))

        self.assertFalse(response.get_nowait())
        app.post.assert_called_once()
        self.assertEqual(app.post.call_args.args[0], "log")
        self.assertEqual(app.post.call_args.args[1][0], "오류")


if __name__ == "__main__":
    unittest.main()
