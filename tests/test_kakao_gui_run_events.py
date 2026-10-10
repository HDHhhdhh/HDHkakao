from __future__ import annotations

import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kakao_send_run import (
    SendRunCompleted,
    SendRunId,
    SendRunProgress,
    SendRunResult,
    SendRunSupervisor,
)
from kakao_sender import MessageRow
from kakao_sender_gui import CLOSE_WAIT_TIMEOUT_SECONDS, KakaoSenderApp


class FakeVar:
    def __init__(self, value: object = None) -> None:
        self.value = value
        self.set_calls: list[object] = []

    def set(self, value: object) -> None:
        self.value = value
        self.set_calls.append(value)


class AliveWorker:
    def is_alive(self) -> bool:
        return True


class FakeCellEditor:
    def __init__(self, value: str) -> None:
        self.value = value
        self.destroyed = False

    def get(self) -> str:
        return self.value

    def destroy(self) -> None:
        self.destroyed = True


def make_supervisor(run_id: str) -> SendRunSupervisor:
    return SendRunSupervisor(lambda _event: None, run_id=SendRunId(run_id))


def make_close_queue_app(run_id: str) -> tuple[KakaoSenderApp, SendRunSupervisor]:
    app = KakaoSenderApp.__new__(KakaoSenderApp)
    app.ui_queue = queue.Queue()
    supervisor = SendRunSupervisor(
        lambda event: app.post("run_event", event), run_id=SendRunId(run_id)
    )
    app.active_run = supervisor
    app.worker = AliveWorker()
    app.results = []
    app.run_row_indexes = [0]
    app._close_requested = False
    app._close_deadline = None
    app._close_poll_after_id = None
    app._screen_use_tokens = {}
    app._screen_use_stop_requested = False
    app._screen_use_poll_after_id = None
    app.status_var = FakeVar("running")
    app.restore_send_controls = Mock()
    app.update_summary = Mock()
    app._render_screen_use_guard = Mock()
    app.log = Mock()
    app.destroy = Mock()
    app.after = Mock(return_value="scheduled-callback")
    app.after_idle = Mock()
    return app, supervisor


class SendRunGuiEventTests(unittest.TestCase):
    def test_queue_handler_error_is_visible_and_does_not_stop_future_polling(self):
        app, _ = make_close_queue_app("queue-error")
        app.ui_queue.put(("status", "first"))
        app.handle_ui_event = Mock(side_effect=ValueError("synthetic rendering failure"))

        with self.assertRaisesRegex(ValueError, "synthetic rendering failure"):
            app.drain_queue()

        app.after.assert_called_once_with(100, app.drain_queue)
        app.handle_ui_event = Mock()
        app.ui_queue.put(("status", "later completion still drains"))
        app.drain_queue()
        app.handle_ui_event.assert_called_once_with("status", "later completion still drains")

    def test_confirm_dialog_failure_releases_waiting_worker_with_stop(self):
        app, _ = make_close_queue_app("confirm-error")
        response = queue.Queue()
        app.ui_queue.put(("confirm", (MessageRow(2, "fixture", "message"), 1, 1, None, response)))
        with patch("kakao_sender_gui.messagebox.askyesnocancel", side_effect=RuntimeError("dialog unavailable")):
            with self.assertRaisesRegex(RuntimeError, "dialog unavailable"):
                app.drain_queue()
        self.assertEqual(response.get_nowait(), "stop")
        self.assertTrue(response.empty())
        app.after.assert_called_once_with(100, app.drain_queue)

    def test_terminal_restores_controls_before_warning_dialog_failure(self):
        app, supervisor = make_close_queue_app("warning-error")
        completed = SendRunCompleted(supervisor.run_id, (SendRunResult({
            "row_index": 0, "row": 2, "chat_name": "fixture", "status": "unknown", "detail": "unconfirmed",
        }),))
        with patch("kakao_gui_feedback.write_log", return_value=Path("synthetic.csv")), \
             patch("kakao_sender_gui.messagebox.showwarning", side_effect=RuntimeError("dialog unavailable")):
            with self.assertRaisesRegex(RuntimeError, "dialog unavailable"):
                app.finish_send_run(completed)
        self.assertIsNone(app.active_run)
        app.restore_send_controls.assert_called_once()
        app.update_summary.assert_called_once()
        self.assertEqual(app.results[0]["status"], "unknown")

    def test_100_results_update_100_route_cells_without_recomputing_all_rows(self):
        from kakao_delivery import delivery_routes

        app, supervisor = make_close_queue_app("route-burst")
        app.rows = [MessageRow(i + 2, f"fixture-{i}", "message") for i in range(100)]
        app.route_summary_var = FakeVar()
        app.common_schedule_datetime = Mock(return_value=None)
        app.direct_enabled_var = Mock(get=Mock(return_value=True))
        app.direct_mappings = {row.chat_name: str(i + 1000) for i, row in enumerate(app.rows)}
        app.tree = Mock()
        app.tree.exists.return_value = True
        with patch("kakao_sender_gui.delivery_routes", wraps=delivery_routes) as resolve_route:
            app.refresh_delivery_routes()
            self.assertEqual(resolve_route.call_count, 1)
            resolve_route.reset_mock()
            app.tree.set.reset_mock()
            for i in range(100):
                app.handle_send_run_event(SendRunProgress(supervisor.run_id, "result", {
                    "row_index": i, "status": "direct_sent", "transport": "direct",
                }))
            resolve_route.assert_not_called()
        self.assertEqual(app.tree.set.call_count, 100)
        self.assertEqual(app.route_summary_var.value, "직접 100건")

    def test_excluding_a_chat_refreshes_all_occurrences_of_that_chat(self):
        app, supervisor = make_close_queue_app("excluded-repeat")
        app.rows = [MessageRow(2, "fixture", "first"), MessageRow(3, "fixture", "second")]
        app.route_summary_var = FakeVar()
        app.common_schedule_datetime = Mock(return_value=None)
        app.direct_enabled_var = Mock(get=Mock(return_value=True))
        app.direct_mappings = {"fixture": "1000"}
        app.refresh_delivery_routes()
        app.handle_send_run_event(SendRunProgress(supervisor.run_id, "result", {
            "row_index": 0, "chat_name": "fixture", "status": "error", "transport": "excluded",
        }))
        self.assertEqual(app._delivery_routes, ["매칭 제외", "매칭 제외"])

    def test_invalid_schedule_clears_cached_routes_and_keeps_warning_on_result(self):
        app, supervisor = make_close_queue_app("invalid-schedule")
        app.rows = [MessageRow(2, "fixture", "message")]
        app.route_summary_var = FakeVar()
        app.common_schedule_datetime = Mock(return_value=None)
        app.direct_enabled_var = Mock(get=Mock(return_value=True))
        app.direct_mappings = {"fixture": "1000"}
        app.refresh_delivery_routes()
        app.common_schedule_datetime.side_effect = ValueError("invalid schedule")
        app.refresh_delivery_routes()
        self.assertNotIn("_delivery_routes", app.__dict__)
        app.handle_send_run_event(SendRunProgress(supervisor.run_id, "result", {
            "row_index": 0, "status": "direct_sent", "transport": "direct",
        }))
        self.assertEqual(app.route_summary_var.value, "예약일시 확인 필요")

    def test_stale_progress_and_terminal_are_rejected_before_ui_dispatch(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = make_supervisor("current-run")
        original_results = [{"status": "sent", "marker": "current"}]
        app.results = original_results
        app.handle_ui_event = Mock(side_effect=AssertionError("stale progress reached Tk"))
        app.finish_send_run = Mock(side_effect=AssertionError("stale terminal reached Tk"))

        stale_progress = SendRunProgress(SendRunId("stale-run"), "status", "must-not-apply")
        stale_completed = SendRunCompleted(
            run_id=SendRunId("stale-run"),
            results=(SendRunResult({"status": "error", "marker": "stale"}),),
        )

        self.assertFalse(app.handle_send_run_event(stale_progress))
        self.assertFalse(app.handle_send_run_event(stale_completed))
        app.handle_ui_event.assert_not_called()
        app.finish_send_run.assert_not_called()
        self.assertIs(app.results, original_results)

    def test_current_terminal_commits_once_and_old_terminal_cannot_overwrite_it(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = make_supervisor("current-run")
        app.worker = object()
        app.results = [{"status": "error", "marker": "old"}]
        app.run_row_indexes = [3]
        app._close_requested = False
        app.status_var = FakeVar("running")
        app.restore_send_controls = Mock()
        app.update_summary = Mock()
        log_calls: list[tuple[str, str]] = []
        app.log = lambda category, message: log_calls.append((category, message))
        completed = SendRunCompleted(
            run_id=SendRunId("current-run"),
            results=(SendRunResult({"status": "sent", "marker": "committed"}),),
        )

        with patch("kakao_gui_feedback.write_log", return_value=Path("result-log.csv")) as write_log:
            self.assertTrue(app.handle_send_run_event(completed))

        self.assertEqual(app.results, [{"status": "sent", "marker": "committed"}])
        self.assertIsNone(app.active_run)
        self.assertIsNone(app.worker)
        self.assertIsNone(app.run_row_indexes)
        app.restore_send_controls.assert_called_once_with()
        app.update_summary.assert_called_once_with()
        write_log.assert_called_once()
        self.assertEqual(write_log.call_args.args[1], app.results)
        self.assertEqual(app.status_var.value, "완료")

        committed_results = app.results
        self.assertFalse(app.handle_send_run_event(completed))
        self.assertIs(app.results, committed_results)

        app.active_run = make_supervisor("next-run")
        stale_completed = SendRunCompleted(
            run_id=SendRunId("current-run"),
            results=(SendRunResult({"status": "error", "marker": "late"}),),
        )

        self.assertFalse(app.handle_send_run_event(stale_completed))
        self.assertIs(app.results, committed_results)
        self.assertEqual(app.results[0]["marker"], "committed")
        app.restore_send_controls.assert_called_once_with()
        app.update_summary.assert_called_once_with()

    def test_request_close_cancels_active_run_and_starts_bounded_polling(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        supervisor = make_supervisor("active-run")
        app.active_run = supervisor
        app._close_requested = False
        app._close_deadline = None
        app._close_poll_after_id = None
        app.status_var = FakeVar("running")
        app.log = Mock()
        app.destroy = Mock(side_effect=AssertionError("active run must not close immediately"))
        scheduled: list[tuple[int, object]] = []

        def after(delay_ms: int, callback: object) -> str:
            scheduled.append((delay_ms, callback))
            return "close-poll-token"

        app.after = after

        with patch("kakao_sender_gui.time.monotonic", return_value=100.0):
            app.request_close()

        self.assertTrue(supervisor.cancel_event.is_set())
        self.assertTrue(app._close_requested)
        self.assertEqual(app._close_deadline, 100.0 + CLOSE_WAIT_TIMEOUT_SECONDS)
        self.assertEqual(app._close_poll_after_id, "close-poll-token")
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 100)
        self.assertEqual(scheduled[0][1], app._poll_close)
        self.assertIn("종료 요청됨", str(app.status_var.value))
        app.log.assert_called_once()

    def test_log_write_failure_does_not_undo_terminal_commit_or_control_restore(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = make_supervisor("log-failure-run")
        app.worker = object()
        app.results = []
        app.run_row_indexes = [0]
        app._close_requested = False
        app.status_var = FakeVar("running")
        app.restore_send_controls = Mock()
        app.update_summary = Mock()
        app.log = Mock()
        completed = SendRunCompleted(
            run_id=SendRunId("log-failure-run"),
            results=(SendRunResult({"status": "sent", "marker": "kept"}),),
        )

        with patch("kakao_gui_feedback.write_log", side_effect=OSError("disk full")):
            self.assertTrue(app.handle_send_run_event(completed))

        self.assertEqual(app.results, [{"status": "sent", "marker": "kept"}])
        self.assertIsNone(app.active_run)
        self.assertIsNone(app.worker)
        self.assertIsNone(app.run_row_indexes)
        app.restore_send_controls.assert_called_once_with()
        app.update_summary.assert_called_once_with()
        self.assertTrue(
            any(
                call.args[0] == "오류" and "결과 로그 저장 실패" in call.args[1]
                for call in app.log.call_args_list
            )
        )

    def test_send_terminal_does_not_close_tk_before_screen_cleanup(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = make_supervisor("close-screen-race")
        app.worker = object()
        app.results = []
        app.run_row_indexes = [0]
        app._close_requested = True
        app._screen_use_tokens = {"screen": (1, threading.Event())}
        app.status_var = FakeVar("closing")
        app.restore_send_controls = Mock()
        app.update_summary = Mock()
        app.log = Mock()
        app._schedule_close_poll = Mock()
        app.after_idle = Mock()
        completed = SendRunCompleted(
            run_id=SendRunId("close-screen-race"),
            results=(),
        )

        app.finish_send_run(completed)

        app._schedule_close_poll.assert_called_once_with()
        app.after_idle.assert_not_called()

    def test_request_close_drains_screen_release_and_journal_before_completed(self) -> None:
        app, supervisor = make_close_queue_app("close-queued-screen")
        token = f"send:{supervisor.run_id}"
        app._screen_use_tokens[token] = (-1, supervisor.cancel_event)
        app._recovery_sources_by_number = {42: ("parent-run", 7)}

        def worker(context):
            context.emit("journal_started", ((0, 42),))
            context.emit("screen_use_state", (token, -1, False, context.cancel_event, None))

        supervisor.run(worker)
        app.request_close()

        self.assertIs(app.active_run, supervisor)
        self.assertIn(token, app._screen_use_tokens)
        self.assertNotIn("_active_journal_rows", app.__dict__)
        app.after_idle.assert_not_called()
        app.destroy.assert_not_called()

        app.drain_queue()

        self.assertTrue(app.ui_queue.empty())
        self.assertIsNone(app.active_run)
        self.assertFalse(app._screen_use_tokens)
        self.assertEqual(app._recovery_sources_by_number[42], ("close-queued-screen", 0))
        self.assertEqual(app._last_run_sources_by_number[42], ("close-queued-screen", 0))
        app.after_idle.assert_called_once_with(app.destroy)

    def test_close_poll_does_not_overtake_queued_journal_and_completed(self) -> None:
        app, supervisor = make_close_queue_app("poll-queued-journal")
        app._close_requested = True
        app._close_deadline = 0.0
        app._close_poll_after_id = "previous-poll"
        app._recovery_sources_by_number = {42: ("parent-run", 7)}
        supervisor.run(lambda context: context.emit("journal_started", ((0, 42),)))

        with patch("kakao_sender_gui.messagebox.askyesnocancel") as ask:
            app._poll_close()

        self.assertIs(app.active_run, supervisor)
        self.assertEqual(app._recovery_sources_by_number[42], ("parent-run", 7))
        self.assertNotIn("_active_journal_rows", app.__dict__)
        app.after.assert_called_once_with(100, app._poll_close)
        app.after_idle.assert_not_called()
        app.destroy.assert_not_called()
        ask.assert_not_called()

        app.drain_queue()

        self.assertTrue(app.ui_queue.empty())
        self.assertIsNone(app.active_run)
        self.assertEqual(app._recovery_sources_by_number[42], ("poll-queued-journal", 0))
        app.after_idle.assert_called_once_with(app.destroy)

    def test_reload_is_blocked_until_queued_terminal_is_committed(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        supervisor = make_supervisor("terminal-queued")
        supervisor.run(lambda _context: None)
        app.active_run = supervisor
        app.file_path = Path("new-file.csv")
        original_rows = [object()]
        app.rows = original_rows

        with (
            patch("kakao_sender_gui.load_messages") as load_messages,
            patch("kakao_sender_gui.messagebox.showinfo") as showinfo,
        ):
            app.reload_file()

        load_messages.assert_not_called()
        showinfo.assert_called_once()
        self.assertIs(app.rows, original_rows)
        self.assertIs(app.active_run, supervisor)

    def test_open_cell_editor_cannot_commit_after_run_becomes_active(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = make_supervisor("editing-locked")
        original_row = MessageRow(2, "기존 대화명", "기존 메시지")
        app.rows = [original_row]
        editor = FakeCellEditor("바뀌면 안 됨")
        app.cell_editor = editor
        app.cell_editor_info = {"row_index": 0, "column_id": "#2"}

        with patch("kakao_sender_gui.messagebox.showinfo") as showinfo:
            result = app.commit_cell_edit()

        self.assertEqual(result, "break")
        self.assertTrue(editor.destroyed)
        self.assertIsNone(app.cell_editor)
        self.assertIsNone(app.cell_editor_info)
        self.assertIs(app.rows[0], original_row)
        showinfo.assert_called_once()

    def test_big_edit_dialog_cannot_open_while_run_is_active(self) -> None:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = make_supervisor("dialog-locked")

        with (
            patch("kakao_sender_gui.messagebox.showinfo") as showinfo,
            patch("kakao_sender_gui.tk.Toplevel") as toplevel,
        ):
            app.edit_selected_row()

        showinfo.assert_called_once()
        toplevel.assert_not_called()

    def test_close_timeout_exposes_wait_force_and_cancel_choices(self) -> None:
        for choice in (True, False, None):
            with self.subTest(choice=choice):
                app = KakaoSenderApp.__new__(KakaoSenderApp)
                app.active_run = make_supervisor(f"active-{choice}")
                app.worker = AliveWorker()
                app._close_requested = True
                app._close_deadline = 1.0
                app._close_poll_after_id = "previous-token"
                app.status_var = FakeVar("closing")
                app.log = Mock()
                app.destroy = Mock()
                app.after = Mock(return_value="next-token")

                with (
                    patch("kakao_sender_gui.time.monotonic", return_value=10.0),
                    patch("kakao_sender_gui.messagebox.askyesnocancel", return_value=choice) as ask,
                ):
                    app._poll_close()

                ask.assert_called_once()
                prompt = ask.call_args.args[1]
                self.assertIn("예: 5초 더 기다림", prompt)
                self.assertIn("아니오: 즉시 종료", prompt)
                self.assertIn("취소: 창 닫기 취소", prompt)

                if choice is True:
                    self.assertTrue(app._close_requested)
                    self.assertEqual(app._close_deadline, 10.0 + CLOSE_WAIT_TIMEOUT_SECONDS)
                    app.destroy.assert_not_called()
                    app.after.assert_called_once_with(100, app._poll_close)
                elif choice is False:
                    app.destroy.assert_called_once_with()
                    app.after.assert_not_called()
                else:
                    self.assertFalse(app._close_requested)
                    self.assertIsNone(app._close_deadline)
                    self.assertEqual(
                        app.status_var.value,
                        "창 닫기 취소 · 발송 중지 요청은 유지됩니다.",
                    )
                    app.destroy.assert_not_called()
                    app.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
