"""Headless Hub integration regressions: every external action is a fake port."""
from __future__ import annotations

import threading
import unittest
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kakao_delivery import delivery_route, progress_summary, route_summary
from kakao_send_run import SendRunCompleted, SendRunId, SendRunProgress, SendRunSupervisor
from kakao_sender import MacroSendCancelled, MessageRow, ScheduleRegistrationResult
from kakao_sender_gui import KakaoSenderApp, SendRunRequest


class ValuePort:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class WidgetPort:
    def configure(self, **_kwargs):
        pass

    def state(self, _value):
        pass


def screen_request(route: str) -> SendRunRequest:
    scheduled = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    row = MessageRow(
        2, "fixture-room", "fixture-message",
        attachments=(Path("fixture-only.pdf"),) if route in {"attachment", "direct_attachment"} else (),
        scheduled_at=scheduled if route == "scheduled" else None,
    )
    return SendRunRequest(
        selected=((0, row),),
        config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
        countdown=0, delay_scale=0, common_schedule=None,
        continue_on_error=False, confirm_each=False,
        direct_enabled=route in {"direct_attachment", "scheduled", "direct"},
        chat_mappings=(("fixture-room", "1001"),),
        direct_config=None,
        send_mode="macro_retry" if route == "macro_retry" else "normal",
    )


class SendWorkerScreenIntegrationTests(unittest.TestCase):
    def make_harness(self, *, deliver_screen_events=False):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = [{"status": "previous-run"}]
        app.log = Mock()
        app.status_var = ValuePort()
        app._screen_use_tokens = {}
        app._screen_use_stop_requested = False
        app._screen_use_poll_after_id = None
        app._screen_use_key_down = False
        app._screen_use_key_reader = None
        app.screen_use_state_var = ValuePort()
        app.screen_use_state_label = WidgetPort()
        app.screen_use_stop_button = WidgetPort()
        app._start_screen_use_hotkey_poll = Mock()
        timeline = []
        events = []
        lease_state = {"depth": 0, "cancel_event": None}

        def receive(event):
            events.append(event)
            if isinstance(event, SendRunProgress) and event.kind == "screen_use_state":
                payload = event.payload
                timeline.append(("screen", payload[2], lease_state["depth"]))
                if deliver_screen_events:
                    app.handle_screen_use_state(payload)
            elif isinstance(event, SendRunCompleted):
                timeline.append(("terminal", lease_state["depth"]))

        supervisor = SendRunSupervisor(receive, run_id=SendRunId("fixture-hub-run"))
        app.active_run = supervisor
        app.post = lambda kind, payload: receive(payload) if kind == "run_event" else None

        @contextmanager
        def lease(*, cancel_event, **_kwargs):
            self.assertIs(cancel_event, supervisor.cancel_event)
            lease_state["cancel_event"] = cancel_event
            lease_state["depth"] += 1
            timeline.append(("lease-enter", lease_state["depth"]))
            try:
                yield
            finally:
                timeline.append(("lease-exit", lease_state["depth"]))
                lease_state["depth"] -= 1

        def action(name, result=None):
            def call(*args, **_kwargs):
                self.assertEqual(lease_state["depth"], 1, f"{name} escaped the PC lease")
                self.assertIn(("screen", True, 1), timeline)
                self.assertNotIn(("screen", False, 1), timeline)
                timeline.append((name,))
                if name in {"send", "schedule"}:
                    self.assertIs(args[3].cancel_event, supervisor.cancel_event)
                return result
            return call

        app.create_direct_bridge = Mock(side_effect=AssertionError("screen fixture must never initialize credentials or a bridge"))
        return SimpleNamespace(
            app=app, supervisor=supervisor, timeline=timeline, events=events,
            lease=lease, state=lease_state, action=action,
        )

    def assert_screen_lifecycle(self, harness):
        screen = [event for event in harness.events if isinstance(event, SendRunProgress) and event.kind == "screen_use_state"]
        self.assertEqual([event.payload[2] for event in screen], [True, False])
        self.assertEqual(screen[0].payload[0], screen[1].payload[0])
        self.assertIs(screen[0].payload[3], harness.supervisor.cancel_event)
        self.assertIs(screen[1].payload[3], harness.supervisor.cancel_event)
        self.assertEqual(harness.timeline[0], ("lease-enter", 1))
        self.assertEqual(harness.timeline[-3:], [("screen", False, 1), ("lease-exit", 1), ("terminal", 0)])
        self.assertEqual(harness.state["depth"], 0)
        self.assertEqual(harness.app.results, [{"status": "previous-run"}])

    def test_macro_attachment_schedule_and_retry_are_guarded_through_cleanup(self):
        for route in ("macro", "attachment", "scheduled", "macro_retry"):
            with self.subTest(route=route):
                h = self.make_harness()
                native = Mock()
                native.prepare_batch.side_effect = h.action("native-prepare")
                native.register.side_effect = h.action("native-register", ScheduleRegistrationResult(False, "unverified", True))
                native.verify_pending.side_effect = h.action("native-audit")
                native.result.return_value = ScheduleRegistrationResult(False, "unverified", True)
                native.close.side_effect = h.action("native-close")
                with (
                    patch("kakao_sender_gui.pc_kakao_ui_lease", h.lease),
                    patch("kakao_sender_gui.NativeScheduleSession", return_value=native),
                    patch("kakao_sender_gui.require_pyautogui", side_effect=h.action("prepare", SimpleNamespace(PAUSE=0))),
                    patch("kakao_sender_gui.focus_kakao_window", side_effect=h.action("focus", "KakaoTalk")),
                    patch("kakao_sender_gui.send_one", side_effect=h.action("send")) as send,
                    patch("kakao_sender_gui.send_scheduled_one", side_effect=h.action("schedule")) as schedule,
                    patch("kakao_sender_gui.return_to_recent_chat_top", side_effect=h.action("cleanup")),
                ):
                    completed = h.app.send_worker(screen_request(route), h.supervisor)
                self.assertIsNone(completed.failure)
                self.assertEqual(completed.legacy_results()[0]["status"], "schedule_acknowledged" if route == "scheduled" else "sent")
                native.verify_pending.assert_not_called()
                schedule.assert_not_called()
                self.assertEqual(native.register.call_count, int(route == "scheduled"))
                self.assertEqual(send.call_count, int(route != "scheduled"))
                self.assert_screen_lifecycle(h)

    def test_all_screen_routes_release_guard_when_port_initialization_raises(self):
        for route in ("macro", "attachment", "scheduled", "macro_retry"):
            with self.subTest(route=route):
                h = self.make_harness()
                def fail_initialization(*args, **kwargs):
                    h.action("prepare")()
                    raise RuntimeError("fixture initialization failure")
                with (
                    patch("kakao_sender_gui.pc_kakao_ui_lease", h.lease),
                    patch("kakao_sender_gui.NativeScheduleSession", side_effect=fail_initialization),
                    patch("kakao_sender_gui.require_pyautogui", side_effect=fail_initialization),
                    patch("kakao_sender_gui.focus_kakao_window", side_effect=AssertionError("must not focus after initialization failure")),
                    patch("kakao_sender_gui.send_one", side_effect=AssertionError("must not send after initialization failure")),
                    patch("kakao_sender_gui.send_scheduled_one", side_effect=AssertionError("must not schedule after initialization failure")),
                    patch("kakao_sender_gui.return_to_recent_chat_top", side_effect=AssertionError("must not touch the PC after initialization failure")),
                ):
                    completed = h.app.send_worker(screen_request(route), h.supervisor)
                if route == "macro_retry":
                    self.assertEqual(completed.legacy_results()[0]["retry_transport"], "macro")
                else:
                    self.assertEqual(completed.failure.exception_type, "RuntimeError")
                self.assert_screen_lifecycle(h)

    def test_shift_escape_cancels_the_same_event_given_to_the_sender(self):
        h = self.make_harness(deliver_screen_events=True)
        def request_stop(_ui, _config, _row, options):
            self.assertIs(options.cancel_event, h.supervisor.cancel_event)
            self.assertTrue(h.app._screen_use_is_active())
            self.assertEqual(h.app._on_screen_use_hotkey(), "break")
            self.assertTrue(options.cancel_event.is_set())
            raise MacroSendCancelled("fixture stop before dispatch")
        with (
            patch("kakao_sender_gui.pc_kakao_ui_lease", h.lease),
            patch("kakao_sender_gui.require_pyautogui", side_effect=h.action("prepare", SimpleNamespace(PAUSE=0))),
            patch("kakao_sender_gui.focus_kakao_window", side_effect=h.action("focus", "KakaoTalk")),
            patch("kakao_sender_gui.send_one", side_effect=request_stop),
            patch("kakao_sender_gui.send_scheduled_one", side_effect=AssertionError("not a scheduled fixture")),
            patch("kakao_sender_gui.return_to_recent_chat_top", side_effect=AssertionError("cancelled run must not type cleanup keys")),
            patch("kakao_gui_screen.windows_async_key_state_reader", side_effect=AssertionError("test must not read real global keyboard state")),
        ):
            completed = h.app.send_worker(screen_request("macro"), h.supervisor)
        self.assertTrue(completed.cancelled)
        self.assertEqual(completed.legacy_results()[0]["status"], "stopped")
        self.assertFalse(h.app._screen_use_is_active())
        self.assertIn("화면 사용 안 함", h.app.screen_use_state_var.value)
        self.assert_screen_lifecycle(h)

    def test_direct_only_run_never_requests_pc_lease_or_screen_ports(self):
        h = self.make_harness()
        bridge = SimpleNamespace(send_message=Mock(return_value={
            "success": True, "status_code": 0, "chat_id": "1001",
            "log_id": "9001", "sent_at": 1800000000,
        }), close=Mock())
        h.app.create_direct_bridge = Mock(return_value=bridge)
        with ExitStack() as stack:
            for target in ("pc_kakao_ui_lease", "require_pyautogui", "focus_kakao_window", "send_one", "send_scheduled_one", "return_to_recent_chat_top"):
                stack.enter_context(patch(f"kakao_sender_gui.{target}", side_effect=AssertionError(f"direct-only used screen port {target}")))
            completed = h.app.send_worker(screen_request("direct"), h.supervisor)
        self.assertIsNone(completed.failure)
        self.assertEqual(completed.legacy_results()[0]["status"], "direct_sent")
        bridge.send_message.assert_called_once_with(text="fixture-message", chat_id="1001")
        bridge.close.assert_called_once_with()
        self.assertEqual([event for event in h.events if isinstance(event, SendRunProgress) and event.kind == "screen_use_state"], [])
        self.assertEqual(h.timeline, [("terminal", 0)])

    def test_direct_attachment_run_never_requests_pc_lease_or_screen_ports(self):
        h = self.make_harness()
        receipt = {
            "success": True, "status_code": 0, "chat_id": "1001", "sent_at": 1800000000,
            "attachment_count": 1, "log_id": "9002",
            "results": [
                {"success": True, "status_code": 0, "chat_id": "1001", "sent_at": 1800000000, "kind": "text", "log_id": "9001"},
                {"success": True, "status_code": 0, "chat_id": "1001", "sent_at": 1800000000, "kind": "file", "log_id": "9002"},
            ],
        }
        bridge = SimpleNamespace(
            send_message=Mock(side_effect=AssertionError("the attachment transport owns the body and files")),
            send_attachments=Mock(return_value=receipt), close=Mock(),
            prepare_attachments=Mock(), release_attachments=Mock(),
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        attachment = Path(temporary.name) / "fixture-only.pdf"
        attachment.write_bytes(b"%PDF- synthetic")
        request = screen_request("direct_attachment")
        request = replace(request, selected=tuple((index, replace(row, attachments=(attachment,))) for index, row in request.selected))
        h.app.create_direct_bridge = Mock(return_value=bridge)
        with ExitStack() as stack:
            for target in ("pc_kakao_ui_lease", "require_pyautogui", "focus_kakao_window", "send_one", "send_scheduled_one", "return_to_recent_chat_top"):
                stack.enter_context(patch(f"kakao_sender_gui.{target}", side_effect=AssertionError(f"direct attachment used screen port {target}")))
            completed = h.app.send_worker(request, h.supervisor)
        self.assertIsNone(completed.failure)
        result = completed.legacy_results()[0]
        self.assertEqual((result["status"], result["transport"]), ("direct_sent", "direct"))
        self.assertEqual(result["direct_receipt_count"], 2)
        self.assertEqual(result["direct_log_ids"], "9001,9002")
        bridge.send_attachments.assert_called_once()
        sent = bridge.send_attachments.call_args.kwargs
        self.assertEqual((sent["text"], sent["chat_id"]), ("fixture-message", "1001"))
        self.assertEqual(sent["attachments"][0].name, "fixture-only.pdf")
        bridge.prepare_attachments.assert_called_once_with(attachments=sent["attachments"], attachment_run_id=sent["attachment_run_id"])
        bridge.release_attachments.assert_called_once_with(attachment_run_id=sent["attachment_run_id"])
        bridge.send_message.assert_not_called()
        bridge.close.assert_called_once_with()
        self.assertEqual([event for event in h.events if isinstance(event, SendRunProgress) and event.kind == "screen_use_state"], [])
        self.assertEqual(h.timeline, [("terminal", 0)])


class HubProgressIntegrationTests(unittest.TestCase):
    def make_app(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = SendRunSupervisor(lambda _event: None, run_id=SendRunId("current-hub-run"))
        app.results = [{"row_index": 0, "status": "old-result"}]
        app._progress_results = {}
        app._running_row_indexes = set()
        app.rows = [MessageRow(i + 2, f"fixture-{i}", "message") for i in range(9)]
        app.run_total = 9
        app.summary_var = ValuePort()
        app.refresh_retry_failed_action = Mock()
        app.attachment_state_label = WidgetPort()
        app.update_row_status = Mock()
        return app

    def test_result_events_update_live_counts_before_terminal_commits_results(self):
        app = self.make_app()
        original_results = app.results
        run_id = app.active_run.run_id
        self.assertTrue(app.handle_send_run_event(SendRunProgress(run_id, "row", (0, "진행 중"))))
        self.assertIn("처리 중: 1", app.summary_var.value)
        self.assertIn("성공: 0", app.summary_var.value)
        result = {"row_index": 0, "status": "direct_sent", "transport": "direct"}
        self.assertTrue(app.handle_send_run_event(SendRunProgress(run_id, "result", result)))
        self.assertIs(app.results, original_results)
        self.assertEqual(app.summary_var.value, "대기: 8 | 처리 중: 0 | 성공: 1 | 실패: 0 | 확인: 0 | 중단: 0 | 제외: 0 | 재시도: 0")
        # Delivery of the same queued result twice must not inflate live totals.
        app.handle_send_run_event(SendRunProgress(run_id, "result", result))
        self.assertEqual(len(app._progress_results), 1)
        self.assertIn("성공: 1", app.summary_var.value)

    def test_stale_result_and_row_events_cannot_change_current_counts(self):
        app = self.make_app()
        app.update_summary()
        previous = app.summary_var.value
        stale = SendRunId("old-hub-run")
        self.assertFalse(app.handle_send_run_event(SendRunProgress(stale, "result", {"row_index": 1, "status": "sent"})))
        self.assertFalse(app.handle_send_run_event(SendRunProgress(stale, "row", (2, "진행 중"))))
        self.assertEqual(app.summary_var.value, previous)
        self.assertEqual(app._progress_results, {})
        self.assertEqual(app._running_row_indexes, set())
        app.update_row_status.assert_not_called()

    def test_live_summary_separates_stopped_rows_from_pending_in_all_eight_counters(self):
        app = self.make_app()
        run_id = app.active_run.run_id
        records = [
            {"row_index": 0, "status": "direct_sent", "transport": "direct"},
            {"row_index": 1, "status": "scheduled", "transport": "macro"},
            {"row_index": 2, "status": "error", "transport": "direct", "retry_transport": "macro"},
            {"row_index": 3, "status": "unknown", "transport": "macro", "retry_transport": "none"},
            {"row_index": 4, "status": "error", "transport": "excluded", "retry_transport": "none"},
            {"row_index": 5, "status": "skipped"},
            {"row_index": 7, "status": "stopped", "failure_stage": "direct_batch_not_attempted", "retry_transport": "none"},
        ]
        for record in records:
            app.handle_send_run_event(SendRunProgress(run_id, "result", record))
        app.handle_send_run_event(SendRunProgress(run_id, "row", (6, "진행 중")))
        self.assertEqual(app.summary_var.value, "대기: 1 | 처리 중: 1 | 성공: 2 | 실패: 1 | 확인: 1 | 중단: 1 | 제외: 2 | 재시도: 1")
        self.assertEqual(len(app.summary_var.value.split(" | ")), 8)
        # A late in-progress index already completed must not count twice.
        self.assertEqual(progress_summary(9, records, [0, 6, 6, 7]), app.summary_var.value)


class DeliveryRouteMatrixTests(unittest.TestCase):
    def test_preflight_route_matrix_and_summary(self):
        future = datetime(2030, 1, 1, 9, 0)
        text = MessageRow(2, "mapped", "message")
        attachment = MessageRow(3, "attachment", "message", (Path("fixture.pdf"),))
        scheduled = MessageRow(4, "scheduled", "message", scheduled_at=future)
        excluded = MessageRow(5, "excluded", "message")
        missing = MessageRow(6, "missing", "message")
        mappings = {"mapped": "1001", "attachment": "1002"}
        routes = [delivery_route(row, None, True, mappings, [" EXCLUDED "])
                  for row in (text, attachment, scheduled, excluded, missing)]
        self.assertEqual(routes, ["직접", "직접", "예약 등록", "매칭 제외", "매칭 필요"])
        self.assertEqual(route_summary(routes), "직접 2건 · 예약 등록 1건 · 매칭 제외 1건 · 매칭 필요 1건")
        self.assertEqual(delivery_route(attachment, None, False, mappings), "화면")
        self.assertEqual(delivery_route(text, None, False, mappings), "화면")
        self.assertEqual(delivery_route(missing, None, False, mappings), "화면")
        self.assertEqual(delivery_route(text, future, True, mappings), "예약 등록")
        self.assertEqual(route_summary([]), "발송 대상 없음")


if __name__ == "__main__":
    unittest.main()
