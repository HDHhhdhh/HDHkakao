import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import Mock, patch

from kakao_sender import MessageRow
from kakao_send_run import SendRunCompleted, SendRunProgress, SendRunSupervisor
from kakao_sender_gui import KakaoSenderApp, SendRunRequest


class RecordingBridge:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **payload: Any) -> dict[str, Any]:
        self.sent.append(payload)
        return {
            "success": True, "status_code": 0, "chat_id": payload["chat_id"],
            "log_id": str(9000 + len(self.sent)), "sent_at": 1800000000,
        }

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakePyAutoGui:
    PAUSE = 0.0


class SendWorkerLifecycleFailureTests(unittest.TestCase):
    @staticmethod
    def make_request(
        *,
        include_scheduled_row: bool = False,
        countdown: int = 0,
    ) -> SendRunRequest:
        selected: tuple[tuple[int, MessageRow], ...] = (
            (0, MessageRow(2, "즉시방", "직접 전송")),
        )
        if include_scheduled_row:
            selected += (
                (
                    1,
                    MessageRow(
                        3,
                        "예약방",
                        "예약 등록",
                        scheduled_at=datetime.now() + timedelta(days=1),
                    ),
                ),
            )
        return SendRunRequest(
            selected=selected,
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=countdown,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=True,
            chat_mappings=(("즉시방", "1001"),),
            direct_config={"enabled": True, "mappings": {"즉시방": "1001"}},
        )

    @staticmethod
    def make_app(events: list[tuple[str, Any]]) -> tuple[KakaoSenderApp, list[dict[str, str]]]:
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        initial_results = [{"status": "previous-run"}]
        app.results = initial_results
        app.post = lambda event, payload: events.append((event, payload))
        return app, initial_results

    def assert_single_terminal(
        self,
        events: list[tuple[str, Any]],
        returned: SendRunCompleted,
    ) -> None:
        terminals = [
            payload
            for event, payload in events
            if event == "run_event" and isinstance(payload, SendRunCompleted)
        ]
        self.assertEqual(len(terminals), 1)
        self.assertIs(terminals[0], returned)

    def assert_app_results_untouched(
        self,
        app: KakaoSenderApp,
        initial_results: list[dict[str, str]],
    ) -> None:
        self.assertIs(app.results, initial_results)
        self.assertEqual(app.results, [{"status": "previous-run"}])

    def test_direct_bridge_creation_failure_is_clean_and_terminal(self) -> None:
        events: list[tuple[str, Any]] = []
        app, initial_results = self.make_app(events)

        def fail_bridge_creation(_config: dict[str, Any] | None = None) -> Any:
            raise RuntimeError("bridge creation failed")

        app.create_direct_bridge = fail_bridge_creation

        completed = app.send_worker(self.make_request())

        self.assertIsNone(completed.failure)
        results = completed.legacy_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(results[0]["failure_stage"], "direct")
        self.assertEqual(results[0]["retry_transport"], "macro")
        self.assertEqual(completed.finalizer_failures, ())
        self.assert_single_terminal(events, completed)
        self.assert_app_results_untouched(app, initial_results)

    def test_native_session_acquisition_failure_closes_bridge_once_and_is_terminal(self) -> None:
        events: list[tuple[str, Any]] = []
        app, initial_results = self.make_app(events)
        bridge = RecordingBridge()
        app.create_direct_bridge = lambda _config=None: bridge

        with patch(
            "kakao_sender_gui.NativeScheduleSession",
            side_effect=RuntimeError("native session unavailable"),
        ):
            completed = app.send_worker(self.make_request(include_scheduled_row=True))

        self.assertIsNotNone(completed.failure)
        self.assertEqual(completed.failure.exception_type, "RuntimeError")
        self.assertEqual(completed.legacy_results(), [])
        self.assertEqual(bridge.close_calls, 1)
        self.assert_single_terminal(events, completed)
        self.assert_app_results_untouched(app, initial_results)

    def test_initial_focus_failure_after_bridge_acquisition_is_clean_and_terminal(self) -> None:
        """A preflight focus failure must not strand a mixed-run resource or UI."""

        events: list[tuple[str, Any]] = []
        app, initial_results = self.make_app(events)
        bridge = RecordingBridge()
        app.create_direct_bridge = lambda _config=None: bridge

        session = Mock()
        session.prepare_batch.side_effect = RuntimeError("focus failed")

        with (
            patch("kakao_sender_gui.NativeScheduleSession", return_value=session),
        ):
            completed = app.send_worker(self.make_request(include_scheduled_row=True))

        self.assertIsNotNone(completed.failure)
        self.assertEqual(completed.failure.exception_type, "RuntimeError")
        self.assertEqual(completed.legacy_results(), [])
        self.assertEqual(bridge.close_calls, 1)
        session.close.assert_called_once()
        self.assert_single_terminal(events, completed)
        self.assert_app_results_untouched(app, initial_results)

    def test_finalizer_failure_is_captured_without_losing_result_snapshot(self) -> None:
        events: list[tuple[str, Any]] = []
        app, initial_results = self.make_app(events)
        bridge = RecordingBridge(close_error=RuntimeError("close failed"))
        app.create_direct_bridge = lambda _config=None: bridge

        completed = app.send_worker(self.make_request())

        self.assertIsNone(completed.failure)
        self.assertFalse(completed.cancelled)
        self.assertEqual(bridge.close_calls, 1)
        self.assertEqual(len(completed.finalizer_failures), 1)
        self.assertEqual(completed.finalizer_failures[0].phase, "finalizer")
        self.assertEqual(completed.finalizer_failures[0].exception_type, "RuntimeError")
        snapshot = completed.legacy_results()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["status"], "direct_sent")
        self.assertEqual(snapshot[0]["chat_name"], "즉시방")
        snapshot[0]["status"] = "mutated test copy"
        self.assertEqual(completed.legacy_results()[0]["status"], "direct_sent")
        self.assert_single_terminal(events, completed)
        self.assert_app_results_untouched(app, initial_results)

    def test_countdown_cancellation_closes_bridge_once_and_is_terminal(self) -> None:
        events: list[tuple[str, Any]] = []
        app, initial_results = self.make_app(events)
        bridge = RecordingBridge()
        app.create_direct_bridge = lambda _config=None: bridge
        supervisor = SendRunSupervisor(lambda event: app.post("run_event", event))

        with patch(
            "kakao_sender_gui.time.sleep",
            side_effect=lambda _seconds: supervisor.request_cancel(),
        ) as sleep:
            completed = app.send_worker(
                self.make_request(countdown=2),
                supervisor,
            )

        self.assertTrue(completed.cancelled)
        self.assertIsNone(completed.failure)
        self.assertEqual(completed.legacy_results(), [])
        self.assertEqual(bridge.close_calls, 1)
        self.assertEqual(sleep.call_count, 1)
        self.assert_single_terminal(events, completed)
        self.assert_app_results_untouched(app, initial_results)

    def test_cancel_wins_over_queued_confirmation_send_decision(self) -> None:
        events: list[tuple[str, Any]] = []
        app, initial_results = self.make_app(events)
        bridge = RecordingBridge()
        app.create_direct_bridge = lambda _config=None: bridge
        supervisor = SendRunSupervisor(lambda event: app.post("run_event", event))
        request = replace(self.make_request(), confirm_each=True)
        returned: list[SendRunCompleted] = []

        worker = threading.Thread(
            target=lambda: returned.append(app.send_worker(request, supervisor)),
        )
        worker.start()

        response = None
        for _ in range(100):
            for event_name, payload in events:
                if (
                    event_name == "run_event"
                    and isinstance(payload, SendRunProgress)
                    and payload.kind == "confirm"
                ):
                    response = payload.payload[-1]
                    break
            if response is not None:
                break
            threading.Event().wait(0.01)
        self.assertIsNotNone(response)

        supervisor.request_cancel()
        response.put("send")
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(bridge.sent, [])
        self.assertEqual(bridge.close_calls, 1)
        self.assertEqual(len(returned), 1)
        completed = returned[0]
        self.assertTrue(completed.cancelled)
        self.assertEqual(completed.legacy_results()[0]["status"], "stopped")
        self.assert_single_terminal(events, completed)
        self.assert_app_results_untouched(app, initial_results)


if __name__ == "__main__":
    unittest.main()
