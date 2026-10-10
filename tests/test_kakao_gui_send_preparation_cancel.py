from __future__ import annotations

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp


class SendPreparationCancellationTests(unittest.TestCase):
    def make_app(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "내 테스트", "prepared text")]
        app.active_run = None
        app.worker = None
        app.stop_event = threading.Event()
        app.direct_enabled_var = SimpleNamespace(get=lambda: True)
        app.status_var = SimpleNamespace(set=Mock())
        app.log = Mock()
        app.filtered_rows = lambda _indexes: list(enumerate(app.rows))
        app.direct_names_for_selected = lambda _selected: ("내 테스트",)
        app.manual_mapping_unresolved_names = Mock(return_value=())
        app.show_manual_chat_mapping_dialog = Mock()
        app.start_sending_after_preflight = Mock()
        app.set_attachment_edit_state = Mock()
        app._latest_chat_snapshot = ()
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app._auto_match_cancel_event = None
        for name in ("start_button", "stop_button", "direct_toggle", "mapping_button", "manual_mapping_button"):
            setattr(app, name, Mock())
        app.callbacks = []
        def request(_names, *, reason, on_complete):
            self.assertEqual(reason, "before_send")
            app._auto_match_generation += 1
            app._auto_match_loading = True
            app._auto_match_cancel_event = threading.Event()
            app._auto_match_pending_callback = on_complete
            app.callbacks.append(on_complete)
            return True
        app.request_auto_chat_matches = Mock(side_effect=request)
        return app

    def test_stop_cancels_read_and_blocks_late_success_before_any_send(self):
        app = self.make_app()
        app.start_sending()
        event = app._auto_match_cancel_event
        generation = app._auto_match_generation
        app.stop_button.state.assert_called_with(["!disabled"])
        app.stop_sending()
        self.assertTrue(event.is_set())
        self.assertGreater(app._auto_match_generation, generation)
        self.assertIsNone(app._auto_match_pending_callback)
        self.assertFalse(app._auto_match_waiting_to_send)
        self.assertFalse(app._auto_match_loading)
        app.stop_button.state.assert_called_with(["disabled"])
        app.start_button.state.assert_called_with(["!disabled"])
        app.callbacks[0](True)
        app.start_sending_after_preflight.assert_not_called()
        app.show_manual_chat_mapping_dialog.assert_not_called()

    def test_cancelled_attempt_callbacks_cannot_start_or_unlock_a_new_attempt(self):
        app = self.make_app()
        app.start_sending()
        old_callback = app.callbacks[0]
        old_token = app._active_send_preparation_token
        app.stop_sending()
        app.start_sending()
        self.assertIsNot(app._active_send_preparation_token, old_token)
        app.start_button.state.reset_mock()
        old_callback(False)
        old_callback(True)
        self.assertTrue(app._auto_match_waiting_to_send)
        app.start_button.state.assert_not_called()
        app.start_sending_after_preflight.assert_not_called()
        app.callbacks[1](True)
        app.start_sending_after_preflight.assert_called_once_with(list(enumerate(app.rows)))
        app.callbacks[1](True)
        self.assertEqual(app.start_sending_after_preflight.call_count, 1)

    def test_late_manual_dialog_close_cannot_start_a_cancelled_or_new_attempt(self):
        app = self.make_app()
        app.manual_mapping_unresolved_names.return_value = ("내 테스트",)
        app.start_sending()
        app.callbacks[0](True)
        old_close = app.show_manual_chat_mapping_dialog.call_args.kwargs["on_close"]
        self.assertTrue(app._auto_match_waiting_to_send)
        app.stop_sending()
        old_close()
        app.start_sending_after_preflight.assert_not_called()
        app.start_sending()
        new_token = app._active_send_preparation_token
        old_close()
        self.assertIs(app._active_send_preparation_token, new_token)
        self.assertTrue(app._auto_match_waiting_to_send)
        app.start_sending_after_preflight.assert_not_called()

    def test_current_manual_callback_still_proceeds_once(self):
        app = self.make_app()
        app.manual_mapping_unresolved_names.return_value = ("내 테스트",)
        app.start_sending()
        app.callbacks[0](True)
        close = app.show_manual_chat_mapping_dialog.call_args.kwargs["on_close"]
        close()
        app.start_sending_after_preflight.assert_called_once_with(
            list(enumerate(app.rows)), blocked_direct_names=("내 테스트",),
        )
        close()
        self.assertEqual(app.start_sending_after_preflight.call_count, 1)

    def test_duplicate_start_is_blocked_while_preparation_is_pending(self):
        app = self.make_app()
        app.start_sending()
        token = app._active_send_preparation_token
        with patch("kakao_sender_gui.messagebox.showinfo"):
            app.start_sending()
        self.assertIs(app._active_send_preparation_token, token)
        app.request_auto_chat_matches.assert_called_once()

    def test_active_run_cancellation_keeps_existing_supervisor_contract(self):
        app = self.make_app()
        app.active_run = Mock()
        app.stop_sending()
        app.active_run.request_cancel.assert_called_once()
        app.request_auto_chat_matches.assert_not_called()
        app.start_sending_after_preflight.assert_not_called()

    def test_failed_current_read_releases_controls_without_sending(self):
        app = self.make_app()
        app.start_sending()
        app.callbacks[0](False)
        self.assertFalse(app._auto_match_waiting_to_send)
        self.assertIsNone(app._active_send_preparation_token)
        app.stop_button.state.assert_called_with(["disabled"])
        app.start_sending_after_preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
