"""Offline queue/feedback coverage; no Tk interpreter or transport is opened."""
from __future__ import annotations

import queue
import threading
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kakao_gui_feedback import (
    HubRunFeedbackMixin,
    UI_QUEUE_ACTIVE_POLL_MS,
    UI_QUEUE_CONTINUE_MS,
    UI_QUEUE_EVENT_LIMIT,
    UI_QUEUE_IDLE_POLL_MS,
)
from kakao_send_run import SendRunCompleted, SendRunId, SendRunProgress, SendRunResult
from kakao_sender import MessageRow


class RecordingVar:
    def __init__(self) -> None:
        self.value = None
        self.values: list[object] = []

    def set(self, value: object) -> None:
        self.value = value
        self.values.append(value)


class RecordingLabel:
    def __init__(self) -> None:
        self.values: list[str] = []

    def configure(self, *, text: str) -> None:
        self.values.append(text)


class FeedbackHarness(HubRunFeedbackMixin):
    """Apply real feedback/summary logic with only widget writes replaced."""

    def __init__(self, row_count: int = 1) -> None:
        self.ui_queue: queue.Queue = queue.Queue()
        self.counts: Counter = Counter()
        self.scheduled_delays: list[int] = []
        self.active_run = SimpleNamespace(
            run_id=SendRunId("synthetic"), cancel_event=threading.Event(),
        )
        self.rows = [MessageRow(i + 2, f"fixture-{i}", "message") for i in range(row_count)]
        self.run_total = row_count
        self.results: list[dict] = []
        self.summary_var = RecordingVar()
        self.progress_var = RecordingVar()
        self.status_var = RecordingVar()
        self.attachment_state_label = RecordingLabel()
        self.progress_state_label = RecordingLabel()
        self.rows_rendered: list[tuple[int, str]] = []
        self.results_rendered: list[dict] = []
        self.logs: list[tuple[str, str]] = []
        self.terminals: list[SendRunCompleted] = []

    def after(self, delay: int, _callback) -> None:
        self.scheduled_delays.append(delay)

    def handle_ui_event(self, event: str, payload) -> None:
        if event == "run_event":
            self.counts["envelopes"] += 1
        super().handle_ui_event(event, payload)

    def update_summary(self) -> None:
        self.counts["summary"] += 1
        super().update_summary()

    def refresh_retry_failed_action(self) -> None:
        pass

    def update_row_status(self, index: int, status: str) -> None:
        self.rows_rendered.append((index, status))

    def refresh_result_delivery_route(self, payload: dict) -> None:
        self.results_rendered.append(dict(payload))

    def log(self, category: str, message: str) -> None:
        self.logs.append((category, message))

    def finish_send_run(self, completed: SendRunCompleted) -> None:
        self.terminals.append(completed)
        self.active_run = None
        self.results = completed.legacy_results()
        self._progress_results = {}
        self._running_row_indexes = set()
        self.update_summary()
        self.status_var.set("terminal")

    def emit(self, kind: str, payload) -> None:
        self.post("run_event", SendRunProgress(self.active_run.run_id, kind, payload))


def enqueue_row_burst(app: FeedbackHarness) -> None:
    for i in range(len(app.rows)):
        app.emit("row", (i, "진행 중"))
        app.emit("result", {"row_index": i, "status": "sent"})
        app.emit("progress", (i + 1) * 100 / len(app.rows))
        app.emit("status", f"processed {i + 1}")


class ResponsiveFeedbackTests(unittest.TestCase):
    def test_burst_yields_after_bounded_slice_and_retains_every_result(self) -> None:
        app = FeedbackHarness(1000)
        enqueue_row_burst(app)
        # A constant clock isolates the deterministic event-count bound.
        with patch("kakao_gui_feedback.time.perf_counter", return_value=0):
            app.drain_queue()
            self.assertEqual(app.counts["envelopes"], UI_QUEUE_EVENT_LIMIT)
            self.assertEqual(app.ui_queue.qsize(), 4000 - UI_QUEUE_EVENT_LIMIT)
            self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_CONTINUE_MS)
            self.assertEqual(app.counts["summary"], 1)
            self.assertEqual(len(app.progress_var.values), 1)
            self.assertEqual(len(app.status_var.values), 1)
            while not app.ui_queue.empty():
                app.drain_queue()

        self.assertEqual(app.counts["envelopes"], 4000)
        self.assertEqual(len(app._progress_results), 1000)
        self.assertEqual(app.rows_rendered, [(i, "진행 중") for i in range(1000)])
        self.assertEqual(app.results_rendered, [{"row_index": i, "status": "sent"} for i in range(1000)])
        self.assertEqual(app._running_row_indexes, set())
        self.assertEqual(app.progress_var.value, 100)
        self.assertEqual(app.status_var.value, "processed 1000")
        self.assertEqual(app.counts["summary"], 63)
        self.assertEqual(len(app.progress_var.values), 63)
        self.assertEqual(len(app.status_var.values), 63)
        self.assertIn("성공: 1000", app.summary_var.value)

    def test_time_budget_yields_even_before_event_limit(self) -> None:
        app = FeedbackHarness()
        for i in range(10):
            app.emit("status", str(i))
        with patch("kakao_gui_feedback.time.perf_counter", side_effect=[0, 0.003, 0.006, 0.009]):
            app.drain_queue()
        self.assertEqual(app.counts["envelopes"], 3)
        self.assertEqual(app.ui_queue.qsize(), 7)
        self.assertEqual(app.status_var.values, ["2"])
        self.assertEqual(app.scheduled_delays, [UI_QUEUE_CONTINUE_MS])

    def test_empty_queue_polls_active_work_faster_and_idle_at_normal_rate(self) -> None:
        app = FeedbackHarness()
        app.drain_queue()
        self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_ACTIVE_POLL_MS)
        app.active_run = None
        for attr in (
            "_auto_match_loading", "_diagnostic_busy", "_document_loading",
            "_capacity_loading",
        ):
            setattr(app, attr, True)
            app.drain_queue()
            self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_ACTIVE_POLL_MS)
            setattr(app, attr, False)
        app._recovery_offer_token = 1
        app.drain_queue()
        self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_ACTIVE_POLL_MS)
        app._recovery_offer_token = None
        app._run_history_view = {"loading": True}
        app.drain_queue()
        self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_ACTIVE_POLL_MS)
        for closed_view in ({"loading": False}, {}, None, object()):
            app._run_history_view = closed_view
            app.drain_queue()
            self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_IDLE_POLL_MS)
        app.drain_queue()
        self.assertEqual(app.scheduled_delays[-1], UI_QUEUE_IDLE_POLL_MS)

    def test_confirm_flushes_observed_summary_and_progress_before_dialog(self) -> None:
        app = FeedbackHarness()
        response = queue.Queue()
        app.emit("row", (0, "진행 중"))
        app.emit("result", {"row_index": 0, "status": "sent"})
        app.emit("status", "ready for confirmation")
        app.emit("progress", 42)
        app.emit("confirm", (app.rows[0], 1, 1, None, response))

        def answer(*_args):
            self.assertIn("성공: 1", app.summary_var.value)
            self.assertEqual(app.status_var.value, "ready for confirmation")
            self.assertEqual(app.progress_var.value, 42)
            return False

        with patch("kakao_gui_feedback.messagebox.askyesnocancel", side_effect=answer):
            app.drain_queue()
        self.assertEqual(response.get_nowait(), "skip")
        self.assertTrue(response.empty())

    def test_confirmation_after_backlog_is_fifo_and_cancellation_wins(self) -> None:
        app = FeedbackHarness()
        response = queue.Queue()
        for i in range(65):
            app.emit("status", str(i))
        app.emit("confirm", (app.rows[0], 1, 1, None, response))
        with patch("kakao_gui_feedback.time.perf_counter", return_value=0), \
             patch("kakao_gui_feedback.messagebox.askyesnocancel") as ask:
            app.drain_queue()
            self.assertTrue(response.empty())
            app.active_run.cancel_event.set()
            app.drain_queue()
        self.assertEqual(app.status_var.value, "64")
        self.assertEqual(response.get_nowait(), "stop")
        ask.assert_not_called()

    def test_error_and_logs_are_preserved_and_flush_prior_feedback(self) -> None:
        app = FeedbackHarness()
        app.emit("status", "before error")
        app.emit("log", ("first", "one"))
        app.emit("error", "important failure")
        app.emit("log", ("last", "two"))
        with patch("kakao_gui_feedback.messagebox.showerror") as show:
            show.side_effect = lambda *_args: self.assertEqual(app.status_var.value, "before error")
            app.drain_queue()
        show.assert_called_once_with("실행 오류", "important failure")
        self.assertEqual(app.logs, [("first", "one"), ("오류", "important failure"), ("last", "two")])

    def test_stale_run_events_never_replace_active_pending_visuals(self) -> None:
        app = FeedbackHarness()
        stale = SendRunId("stale")
        app.emit("status", "current first")
        app.post("run_event", SendRunProgress(stale, "status", "stale progress"))
        app.post("run_event", SendRunCompleted(stale, ()))
        app.emit("status", "current latest")
        app.drain_queue()
        self.assertEqual(app.status_var.values, ["current latest"])
        self.assertEqual(app.terminals, [])

    def test_terminal_flushes_prior_values_and_late_progress_cannot_overwrite_it(self) -> None:
        app = FeedbackHarness()
        run_id = app.active_run.run_id
        app.emit("status", "last observed progress")
        completed = SendRunCompleted(run_id, (SendRunResult({"row_index": 0, "status": "sent"}),))
        app.post("run_event", completed)
        app.post("run_event", SendRunProgress(run_id, "status", "late progress"))
        app.post("run_event", completed)
        app.drain_queue()
        self.assertEqual(app.status_var.values, ["last observed progress", "terminal"])
        self.assertEqual(app.terminals, [completed])
        self.assertEqual(app.results, [{"row_index": 0, "status": "sent"}])

    def test_rendering_failure_keeps_pump_scheduled_and_important_event_pending(self) -> None:
        app = FeedbackHarness()
        app.emit("status", "before failure")
        app.emit("row", (0, "진행 중"))
        app.emit("result", {"row_index": 0, "status": "sent"})
        app.update_row_status = Mock(side_effect=ValueError("render failure"))
        with self.assertRaisesRegex(ValueError, "render failure"):
            app.drain_queue()
        self.assertEqual(app.status_var.value, "before failure")
        self.assertEqual(app.ui_queue.qsize(), 1)
        self.assertEqual(app.scheduled_delays, [UI_QUEUE_IDLE_POLL_MS])
        self.assertFalse(app._feedback_batch_active)
        app.update_row_status = Mock()
        app.drain_queue()
        self.assertEqual(app._progress_results[0]["status"], "sent")

    def test_direct_dispatch_remains_immediate_and_optional_flusher_runs(self) -> None:
        app = FeedbackHarness()
        app.handle_ui_event("status", "direct")
        app.handle_ui_event("progress", 9)
        app.handle_ui_event("result", {"row_index": 0, "status": "sent"})
        self.assertEqual(app.status_var.value, "direct")
        self.assertEqual(app.progress_var.value, 9)
        self.assertEqual(app.counts["summary"], 1)
        app.flush_pending_visual_feedback = Mock()
        app.emit("status", "queued")
        app.drain_queue()
        app.flush_pending_visual_feedback.assert_called_once_with()

    def test_panel_result_events_flush_and_dispatch_without_dropping_payload(self) -> None:
        app = FeedbackHarness()
        app.handle_recovery_offer_result = Mock()
        app.handle_run_history_result = Mock()
        app.handle_document_load_result = Mock()
        app.handle_capacity_view_result = Mock()
        recovery = (1, ["recovery"])
        history = (2, ["history"])
        document = (3, ["document"])
        capacity = (4, ["capacity"])
        app.emit("status", "before panel")
        app.post("recovery_offer_result", recovery)
        app.post("run_history_result", history)
        app.post("document_load_result", document)
        app.post("capacity_view_result", capacity)
        app.drain_queue()
        self.assertEqual(app.status_var.value, "before panel")
        app.handle_recovery_offer_result.assert_called_once_with(recovery)
        app.handle_run_history_result.assert_called_once_with(history)
        app.handle_document_load_result.assert_called_once_with(document)
        app.handle_capacity_view_result.assert_called_once_with(capacity)


if __name__ == "__main__":
    unittest.main()
