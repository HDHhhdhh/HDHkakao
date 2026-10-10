"""Guarded Esc cleanup including its synthetic stop-key latch; no live input."""
from unittest.mock import Mock
import pytest
import kakao_native_schedule as native
from kakao_native_schedule import NativeScheduleDriver, NativeWindow
from kakao_sender import FocusSafetyError


def driver():
    d = NativeScheduleDriver.__new__(NativeScheduleDriver)
    d.gui = Mock(); d.input = Mock(); d.user = Mock(); d.log = Mock()
    d.stopped = False; d.cancel_event = None
    d.guard = Mock(); d.activate = Mock(side_effect=lambda window:window)
    d.user.GetAsyncKeyState.return_value = 0
    d.gui.IsWindow.side_effect = [True, False]
    d.gui.IsWindowVisible.return_value = True
    return d, NativeWindow(2, 'fixture room', 'EVA', 9, (0, 0, 740, 1200))


def test_only_completed_chat_gets_one_esc_and_injected_latch_does_not_stop_run():
    d, chat = driver()
    d.user.GetAsyncKeyState.side_effect = [0, 1, 0, 1]
    d.close_registered_chat(chat, expected_chat_name=chat.title)
    d.activate.assert_called_once_with(chat)
    d.input.press.assert_called_once_with('esc')
    assert not d.stopped
    with pytest.raises(InterruptedError):
        d.check()  # A later physical Esc must still interrupt.


@pytest.mark.parametrize('title', ['wrong room', '카카오톡', '예약 메시지', '브리핑 보드'])
def test_main_form_or_unrelated_chat_never_receives_esc(title):
    d, chat = driver()
    window = NativeWindow(chat.handle, title, chat.class_name, chat.pid, chat.rect)
    with pytest.raises(FocusSafetyError):
        d.close_registered_chat(window, expected_chat_name=title if title != 'wrong room' else chat.title)
    d.input.press.assert_not_called()


def test_lost_focus_or_changed_window_blocks_cleanup_input():
    d, chat = driver()
    d.guard.side_effect = FocusSafetyError('wrong foreground')
    with pytest.raises(FocusSafetyError):
        d.close_registered_chat(chat, expected_chat_name=chat.title)
    d.input.press.assert_not_called()


def test_already_closed_chat_needs_no_input():
    d, chat = driver(); d.gui.IsWindow.side_effect = None
    d.gui.IsWindow.return_value = False
    d.close_registered_chat(chat, expected_chat_name=chat.title)
    d.input.press.assert_not_called(); d.activate.assert_not_called()


def test_physical_esc_held_during_cleanup_still_stops():
    d, chat = driver()
    d.user.GetAsyncKeyState.side_effect = [0, 0x8001]
    with pytest.raises(InterruptedError):
        d.close_registered_chat(chat, expected_chat_name=chat.title)
    assert d.stopped
    d.input.press.assert_called_once_with('esc')


def test_unclosed_chat_times_out_without_repeated_esc(monkeypatch):
    d, chat = driver(); d.gui.IsWindow.side_effect = None
    d.gui.IsWindow.return_value = True
    ticks = {'now': 0.}
    monkeypatch.setattr(native.time, 'monotonic', lambda: ticks['now'])
    monkeypatch.setattr(native.time, 'sleep', lambda duration:ticks.update(now=ticks['now']+duration))
    with pytest.raises(FocusSafetyError, match='닫힌 것'):
        d.close_registered_chat(chat, expected_chat_name=chat.title)
    assert ticks['now'] <= 2.05
    d.input.press.assert_called_once_with('esc')
