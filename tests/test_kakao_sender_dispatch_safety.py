from __future__ import annotations

import csv
import tempfile
import threading
import unittest
from argparse import Namespace
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import kakao_sender as sender


class SenderDispatchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.state = {"foreground": 222, "pid": 77, "visible": True, "title": "fixture-room"}
        self.actions = []
        self.ui = SimpleNamespace(
            hotkey=lambda *keys: self.actions.append(("hotkey", keys)),
            press=lambda key: self.actions.append(("press", key)),
            click=lambda *point: self.actions.append(("click", point)),
        )
        self.chat = sender.OpenChatContext(111, 222, "fixture-room", 77)
        self.row = sender.MessageRow(2, "fixture-room", "fixture message")
        self.args = Namespace(delay_scale=0, paste_only=False, no_close_chat=True, cancel_event=threading.Event())
        self.config = {"send_enter_presses": 1, "send_enter_interval": 0, "delays": {}}
        self.stack.enter_context(patch.object(sender, "pc_kakao_ui_lease", side_effect=lambda **kwargs: nullcontext()))
        self.stack.enter_context(patch.object(sender, "open_chat", return_value=self.chat))
        self.stack.enter_context(patch.object(sender, "set_clipboard"))
        self.stack.enter_context(patch.object(sender, "set_file_clipboard"))
        self.stack.enter_context(patch.object(sender, "get_foreground_window_handle", side_effect=lambda: self.state["foreground"]))
        self.stack.enter_context(patch.object(sender, "get_window_process_id", side_effect=lambda handle: self.state["pid"]))
        self.stack.enter_context(patch.object(sender, "is_kakao_process_id", side_effect=lambda pid: pid == 77))
        self.stack.enter_context(patch.object(sender, "window_handle_visible", side_effect=lambda handle: self.state["visible"]))
        self.stack.enter_context(patch.object(sender, "get_window_title", side_effect=lambda handle: self.state["title"]))
        self.close = self.stack.enter_context(patch.object(sender, "close_window_handle", return_value=True))

    def test_focus_changed_before_paste_sends_no_keys(self):
        self.state["foreground"] = 999
        with self.assertRaises(sender.FocusSafetyError) as caught:
            sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertNotIsInstance(caught.exception, sender.MacroSendUnknownError)
        self.assertEqual(self.actions, [])

    def test_pid_reuse_is_rejected_even_when_handle_and_title_match(self):
        self.state["pid"] = 99
        with self.assertRaises(sender.FocusSafetyError):
            sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertEqual(self.actions, [])

    def test_verified_window_title_suffix_is_accepted_during_cleanup(self):
        self.state["title"] = "fixture-room - KakaoTalk"
        self.assertEqual(sender.verify_window_target(222, process_id=77, expected_title="fixture-room - KakaoTalk"), 77)

    def test_focus_changed_after_paste_prevents_first_dispatch(self):
        with patch.object(sender, "sleep_delay", side_effect=lambda *a, **kw: self.state.update(foreground=999)):
            with self.assertRaises(sender.FocusSafetyError) as caught:
                sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertNotIsInstance(caught.exception, sender.MacroSendUnknownError)
        self.assertEqual(self.actions, [("hotkey", ("ctrl", "v"))])

    def test_cleanup_failure_after_enter_is_unknown(self):
        self.args.no_close_chat = False
        with patch.object(sender, "close_chat_after_send", side_effect=sender.FocusSafetyError("cleanup blocked")):
            with self.assertRaises(sender.MacroSendUnknownError) as caught:
                sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertEqual(caught.exception.stage, "cleanup")
        self.assertTrue(caught.exception.safety_error)
        self.assertEqual(self.actions.count(("press", "enter")), 1)

    def test_exception_during_enter_is_unknown_not_retryable(self):
        self.ui.press = Mock(side_effect=RuntimeError("input completion unavailable"))
        with self.assertRaises(sender.MacroSendUnknownError) as caught:
            sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertEqual(caught.exception.stage, "text_dispatch")
        self.assertFalse(caught.exception.safety_error)

    def test_cancellation_after_paste_closes_exact_chat_and_never_sends(self):
        with patch.object(sender, "sleep_delay", side_effect=lambda *a, **kw: self.args.cancel_event.set()):
            with self.assertRaises(sender.MacroSendCancelled):
                sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertNotIn(("press", "enter"), self.actions)
        self.close.assert_called_once_with(222)

    def test_cancellation_after_first_enter_is_unknown_and_blocks_second_enter(self):
        self.config["send_enter_presses"] = 2
        def press(key):
            self.actions.append(("press", key))
            self.args.cancel_event.set()
        self.ui.press = press
        with self.assertRaises(sender.MacroSendUnknownError) as caught:
            sender.send_one(self.ui, self.config, self.row, self.args)
        self.assertIsInstance(caught.exception.cause, sender.MacroSendCancelled)
        self.assertEqual(self.actions.count(("press", "enter")), 1)
        self.close.assert_called_once_with(222)

    def test_missing_attachment_is_rejected_before_text_is_dispatched(self):
        row = sender.MessageRow(2, "fixture-room", "text", (Path("definitely-missing-fixture-file.xyz"),))
        with self.assertRaises(FileNotFoundError):
            sender.send_one(self.ui, self.config, row, self.args)
        self.assertEqual(self.actions, [])

    def test_attachment_focus_change_after_text_is_unknown_without_attachment_paste(self):
        row = sender.MessageRow(2, "fixture-room", "text", (Path("fixture.pdf"),))
        def clipboard(_paths):
            self.state["foreground"] = 999
        with patch.object(sender, "missing_attachment_paths", return_value=[]), patch.object(Path, "exists", return_value=True), patch.object(sender, "set_file_clipboard", side_effect=clipboard):
            with self.assertRaises(sender.MacroSendUnknownError) as caught:
                sender.send_one(self.ui, self.config, row, self.args)
        self.assertEqual(caught.exception.stage, "attachment_paste")
        self.assertEqual(self.actions.count(("hotkey", ("ctrl", "v"))), 1)

    def test_attachment_popup_must_be_owned_by_the_target_chat(self):
        self.state["foreground"] = 333
        with patch.object(sender, "get_window_owner", return_value=444):
            with self.assertRaises(sender.FocusSafetyError):
                sender.verify_attachment_target(self.chat)
        with patch.object(sender, "get_window_owner", return_value=222):
            sender.verify_attachment_target(self.chat)

    def test_schedule_submit_focus_change_never_clicks(self):
        with patch.object(sender, "activate_window_handle", return_value=True), patch.object(sender, "click_window_offset") as click:
            with self.assertRaises(sender.FocusSafetyError):
                sender.click_schedule_submit(self.ui, {}, self.args, 456, (0, 0, 100, 100))
        click.assert_not_called()

    def test_schedule_submit_uncertain_window_close_is_unknown(self):
        self.state.update(foreground=456, title="예약 메시지")
        self.args.confirm_schedule_ready = lambda row, target: True
        with (
            patch.object(sender, "activate_window_handle", return_value=True),
            patch.object(sender, "open_schedule_dialog", return_value=456),
            patch.object(sender, "get_window_rect", return_value=(0, 0, 500, 800)),
            patch.object(sender, "paste_schedule_message"),
            patch.object(sender, "set_schedule_datetime"),
            patch.object(sender, "click_schedule_submit"),
            patch.object(sender, "wait_until_window_gone", return_value=False),
        ):
            target = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
            target = target.replace(minute=0)
            with self.assertRaises(sender.MacroSendUnknownError) as caught:
                sender.send_scheduled_one(self.ui, {}, self.row, self.args, target)
        self.assertEqual(caught.exception.stage, "schedule_submit")


    def prepare_schedule_form(self):
        self.state.update(foreground=456, title="예약 메시지")
        self.args.confirm_schedule_ready = Mock(return_value=True)
        clock = self.stack.enter_context(patch.object(sender, "datetime"))
        clock.now.return_value = datetime(2030, 1, 1, 10, 0)
        self.stack.enter_context(patch.object(sender, "activate_window_handle", return_value=True))
        self.stack.enter_context(patch.object(sender, "open_schedule_dialog", return_value=456))
        self.stack.enter_context(patch.object(sender, "get_window_rect", return_value=(0, 0, 500, 800)))
        self.stack.enter_context(patch.object(sender, "paste_schedule_message"))
        self.stack.enter_context(patch.object(sender, "set_schedule_datetime"))
        submit = self.stack.enter_context(patch.object(sender, "click_schedule_submit"))
        gone = self.stack.enter_context(patch.object(sender, "wait_until_window_gone", return_value=True))
        return clock, submit, gone

    def test_schedule_submit_requires_exact_true_and_returns_unverified_result(self):
        _clock, submit, gone = self.prepare_schedule_form()
        target = datetime(2030, 1, 1, 10, 10)

        result = sender.send_scheduled_one(self.ui, {}, self.row, self.args, target)

        self.args.confirm_schedule_ready.assert_called_once_with(self.row, target)
        submit.assert_called_once()
        gone.assert_called_once()
        self.assertIsInstance(result, sender.ScheduleRegistrationResult)
        self.assertFalse(result.verified)
        self.assertIn("실제 예약 등록 여부는 확인하지 못", result.detail)

    def test_schedule_validation_failure_is_not_submitted_with_original_cause(self):
        _clock, submit, gone = self.prepare_schedule_form()
        previous_state = sender._MACRO_DISPATCH.get()
        with self.assertRaises(sender.ScheduleNotSubmittedError) as caught:
            sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 0))

        self.assertEqual(caught.exception.stage, "prepare")
        self.assertIsInstance(caught.exception.cause, ValueError)
        self.assertIs(caught.exception.__cause__, caught.exception.cause)
        self.assertIs(sender._MACRO_DISPATCH.get(), previous_state)
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_lease_acquisition_failure_is_not_submitted(self):
        _clock, submit, gone = self.prepare_schedule_form()
        failure = RuntimeError("fixture lease acquisition failure")
        previous_state = sender._MACRO_DISPATCH.get()
        with patch.object(sender, "pc_kakao_ui_lease", side_effect=failure):
            with self.assertRaises(sender.ScheduleNotSubmittedError) as caught:
                sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertEqual(caught.exception.stage, "schedule_lease")
        self.assertIs(caught.exception.cause, failure)
        self.assertIs(sender._MACRO_DISPATCH.get(), previous_state)
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_lease_release_failure_after_submit_is_unknown(self):
        _clock, submit, gone = self.prepare_schedule_form()
        failure = RuntimeError("fixture lease release failure")
        previous_state = sender._MACRO_DISPATCH.get()
        @contextmanager
        def failing_release(**_kwargs):
            try:
                yield
            finally:
                raise failure

        with patch.object(sender, "pc_kakao_ui_lease", side_effect=failing_release):
            with self.assertRaises(sender.MacroSendUnknownError) as caught:
                sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertEqual(caught.exception.stage, "cleanup")
        self.assertIs(caught.exception.cause, failure)
        self.assertIs(sender._MACRO_DISPATCH.get(), previous_state)
        submit.assert_called_once()
        gone.assert_called_once()

    def test_schedule_keyboard_interrupt_keeps_cancel_cleanup_inside_lease(self):
        _clock, submit, gone = self.prepare_schedule_form()
        interruption = KeyboardInterrupt("fixture interruption")
        self.args.confirm_schedule_ready.side_effect = interruption
        lease_active = [False]
        @contextmanager
        def tracked_lease(**_kwargs):
            lease_active[0] = True
            try:
                yield
            finally:
                lease_active[0] = False

        with (
            patch.object(sender, "pc_kakao_ui_lease", side_effect=tracked_lease),
            patch.object(sender, "_close_cancelled_chat", side_effect=lambda _chat: self.assertTrue(lease_active[0])) as close,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertIs(caught.exception, interruption)
        self.assertFalse(lease_active[0])
        close.assert_called_once_with(self.chat)
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_cancellation_after_submit_is_unknown(self):
        _clock, submit, gone = self.prepare_schedule_form()
        submit.side_effect = lambda *args, **kwargs: self.args.cancel_event.set()
        with patch.object(sender, "_close_cancelled_chat") as close:
            with self.assertRaises(sender.MacroSendUnknownError) as caught:
                sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertIsInstance(caught.exception.cause, sender.MacroSendCancelled)
        self.assertEqual(caught.exception.stage, "cleanup")
        close.assert_called_once_with(self.chat)
        submit.assert_called_once()
        gone.assert_called_once()

    def test_schedule_missing_review_callback_never_submits(self):
        _clock, submit, gone = self.prepare_schedule_form()
        del self.args.confirm_schedule_ready

        with self.assertRaisesRegex(sender.ScheduleNotSubmittedError, "예약 메시지 입력값.*사용자 확인 절차가 없어") as caught:
            sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertNotIsInstance(caught.exception, sender.MacroSendUnknownError)
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_unconfirmed_or_truthy_non_boolean_response_never_submits(self):
        _clock, submit, gone = self.prepare_schedule_form()
        for response in (False, None, 1, "yes"):
            with self.subTest(response=response):
                self.args.confirm_schedule_ready.return_value = response
                with self.assertRaises(sender.MacroSendCancelled):
                    sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_cancellation_during_review_never_submits(self):
        _clock, submit, gone = self.prepare_schedule_form()
        def confirm(_row, _target):
            self.args.cancel_event.set()
            return True
        self.args.confirm_schedule_ready = confirm

        with self.assertRaises(sender.MacroSendCancelled):
            sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_lead_time_consumed_during_review_never_submits(self):
        clock, submit, gone = self.prepare_schedule_form()
        def confirm(_row, _target):
            clock.now.return_value = datetime(2030, 1, 1, 10, 5, 0, 1)
            return True
        self.args.confirm_schedule_ready = confirm

        with self.assertRaisesRegex(sender.ScheduleNotSubmittedError, "최소 5분") as caught:
            sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertEqual(caught.exception.stage, "schedule_submit")
        self.assertIsInstance(caught.exception.cause, ValueError)
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_pid_changed_during_review_never_submits(self):
        _clock, submit, gone = self.prepare_schedule_form()
        def confirm(_row, _target):
            self.state["pid"] = 88
            return True
        self.args.confirm_schedule_ready = confirm

        with self.assertRaises(sender.ScheduleNotSubmittedError) as caught:
            sender.send_scheduled_one(self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10))

        self.assertNotIsInstance(caught.exception, sender.MacroSendUnknownError)
        submit.assert_not_called()
        gone.assert_not_called()

    def test_schedule_dialog_moved_during_review_uses_current_rect(self):
        actual_submit = sender.click_schedule_submit
        self.prepare_schedule_form()
        rect = [(0, 0, 500, 800)]
        moved_rect = (300, 150, 800, 950)
        def confirm(_row, _target):
            rect[0] = moved_rect
            return True
        self.args.confirm_schedule_ready = confirm

        with (
            patch.object(sender, "get_window_rect", side_effect=lambda _handle: rect[0]),
            patch.object(sender, "click_schedule_submit", side_effect=actual_submit),
            patch.object(sender, "click_window_offset") as click,
            patch.object(sender, "sleep_delay"),
        ):
            result = sender.send_scheduled_one(
                self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10),
            )

        click.assert_called_once_with(self.ui, moved_rect, 183.0, 562.0)
        self.assertFalse(result.verified)

    def test_schedule_dialog_rect_missing_after_review_never_dispatches(self):
        actual_submit = sender.click_schedule_submit
        _clock, _submit, gone = self.prepare_schedule_form()
        rect = [(0, 0, 500, 800)]
        def confirm(_row, _target):
            rect[0] = None
            return True
        self.args.confirm_schedule_ready = confirm

        with (
            patch.object(sender, "get_window_rect", side_effect=lambda _handle: rect[0]),
            patch.object(sender, "click_schedule_submit", side_effect=actual_submit),
            patch.object(sender, "click_window_offset") as click,
            patch.object(sender, "_mark_macro_dispatch") as mark_dispatch,
        ):
            with self.assertRaisesRegex(sender.ScheduleNotSubmittedError, "예약 메시지 창 위치") as caught:
                sender.send_scheduled_one(
                    self.ui, {}, self.row, self.args, datetime(2030, 1, 1, 10, 10),
                )

        self.assertNotIsInstance(caught.exception, sender.MacroSendUnknownError)
        mark_dispatch.assert_not_called()
        click.assert_not_called()
        gone.assert_not_called()

    def test_schedule_lead_time_consumed_during_submit_activation_never_dispatches(self):
        self.state.update(foreground=456, title="예약 메시지")
        now = [datetime(2030, 1, 1, 10, 0)]
        target = datetime(2030, 1, 1, 10, 10)
        def activate(_handle):
            now[0] = datetime(2030, 1, 1, 10, 5, 0, 1)
            return True
        with (
            patch.object(sender, "activate_window_handle", side_effect=activate),
            patch.object(sender, "get_window_rect", return_value=(0, 0, 500, 800)),
            patch.object(sender, "click_window_offset") as click,
            patch.object(sender, "_mark_macro_dispatch") as mark_dispatch,
        ):
            with self.assertRaisesRegex(ValueError, "최소 5분"):
                sender.click_schedule_submit(
                    self.ui, {}, self.args, 456, (0, 0, 500, 800),
                    before_dispatch=lambda: sender.validate_schedule_row(self.row, target, now=now[0]),
                )
        mark_dispatch.assert_not_called()
        click.assert_not_called()


class ExactWindowCloseTests(unittest.TestCase):
    def test_normal_cleanup_never_enumerates_same_title_windows(self):
        with (
            patch.object(sender, "verify_window_target", return_value=77),
            patch.object(sender, "close_window_handle", return_value=True) as close,
            patch.object(sender, "wait_until_window_gone", return_value=True),
            patch.object(sender, "window_handle_visible", return_value=False),
            patch.object(sender, "close_windows_with_title") as by_title,
        ):
            sender.close_chat_after_send(SimpleNamespace(), {}, Namespace(delay_scale=0), 111, 222, "fixture-room", process_id=77)
        close.assert_called_once_with(222)
        by_title.assert_not_called()

    def test_title_compatibility_helper_excludes_other_processes(self):
        fake_windows = SimpleNamespace(
            EnumWindows=lambda callback, extra: [callback(handle, extra) for handle in (222, 999)],
            IsWindowVisible=lambda handle: True,
            GetWindowText=lambda handle: "fixture-room",
        )
        with (
            patch.dict("sys.modules", {"win32gui": fake_windows}),
            patch.object(sender, "is_kakao_process_id", side_effect=lambda pid: pid == 77),
            patch.object(sender, "get_window_process_id", side_effect=lambda handle: 77 if handle == 222 else 99),
            patch.object(sender, "close_window_handle", return_value=True) as close,
        ):
            self.assertEqual(sender.close_windows_with_title("fixture-room"), 0)
            self.assertEqual(sender.close_windows_with_title("fixture-room", process_id=77), 1)
        close.assert_called_once_with(222)


class ExtendedLogTests(unittest.TestCase):
    def test_back_to_back_runs_keep_both_files_and_route_metadata(self):
        first = {"row": 2, "status": "unknown", "transport": "macro", "failure_stage": "cleanup", "retry_transport": "none", "chat_id": "123", "log_id": "456"}
        with tempfile.TemporaryDirectory() as temp:
            path1 = sender.write_log(Path(temp), [first], run_id="run-one")
            path2 = sender.write_log(Path(temp), [{"row": 3, "status": "sent", "run_id": "run-two"}])
            self.assertNotEqual(path1, path2)
            self.assertEqual(len(list(Path(temp).glob("*.csv"))), 2)
            with path1.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            with path2.open(encoding="utf-8-sig", newline="") as handle:
                other = next(csv.DictReader(handle))
        self.assertEqual(row["run_id"], "run-one")
        self.assertEqual(row["failure_stage"], "cleanup")
        self.assertEqual(row["chat_id"], "123")
        self.assertEqual(row["log_id"], "456")
        self.assertEqual(other["run_id"], "run-two")


if __name__ == "__main__":
    unittest.main()
