from __future__ import annotations

from argparse import Namespace
from datetime import date, datetime, tzinfo
import unittest
from unittest.mock import patch

from kakao_sender import (
    FocusSafetyError,
    MessageRow,
    OpenChatContext,
    ScheduleNotSubmittedError,
    click_schedule_submit,
    select_schedule_date,
    select_schedule_time,
    send_scheduled_one,
    set_schedule_datetime,
)


Action = tuple[str, int, int] | tuple[str, int] | tuple[str, str] | tuple[str, tuple[str, ...]]


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        value = cls(2026, 6, 17, 10, 0)
        if tz is not None:
            return value.replace(tzinfo=tz)
        return value


class FakePyAutoGui:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))

    def hotkey(self, *keys: str) -> None:
        self.actions.append(("hotkey", tuple(keys)))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))

    def scroll(self, clicks: int) -> None:
        self.actions.append(("scroll", clicks))


class ScheduleWorkflowTests(unittest.TestCase):
    def setUp(self):
        guard = patch("kakao_sender.verify_window_target", return_value=77)
        guard.start()
        self.addCleanup(guard.stop)
        foreground = patch("kakao_sender.get_foreground_window_handle", return_value=456)
        foreground.start()
        self.addCleanup(foreground.stop)
        activate = patch("kakao_sender.activate_window_handle", return_value=True)
        activate.start()
        self.addCleanup(activate.stop)

    @classmethod
    def setUpClass(cls) -> None:
        print("ScheduleWorkflowTests: date popup, dynamic time popup, submit safety, and failed-reservation cleanup cases")

    def test_same_day_scheduling_skips_date_picker(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        scheduled_at = datetime(2026, 6, 17, 15, 30)

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.select_schedule_date") as select_date,
            patch("kakao_sender.select_schedule_time") as select_time,
        ):
            set_schedule_datetime(pyautogui, {}, args, 456, schedule_rect, scheduled_at)

        select_date.assert_not_called()
        select_time.assert_called_once_with(pyautogui, {}, args, schedule_rect, scheduled_at)

    def test_future_day_scheduling_invokes_date_picker(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        scheduled_at = datetime(2026, 6, 18, 9, 5)

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.select_schedule_date") as select_date,
            patch("kakao_sender.select_schedule_time") as select_time,
        ):
            set_schedule_datetime(pyautogui, {}, args, 456, schedule_rect, scheduled_at)

        select_date.assert_called_once_with(pyautogui, {}, args, 456, schedule_rect, date(2026, 6, 18))
        select_time.assert_called_once_with(pyautogui, {}, args, schedule_rect, scheduled_at)

    def test_select_schedule_date_uses_date_picker_window(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        picker_rect = (1000, 2000, 1300, 2600)
        config = {
            "schedule_ui": {
                "date_field": {"x_offset": 10, "y_offset": 20},
                "date_picker": {
                    "next_x_offset": 30,
                    "prev_x_offset": 5,
                    "month_nav_y_offset": 40,
                    "grid_x_offset": 10,
                    "grid_y_offset": 100,
                    "cell_width": 20,
                    "cell_height": 30,
                },
            },
        }

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", return_value=(222, "EVA_Window_Dblclk", "", picker_rect)) as wait_picker,
        ):
            select_schedule_date(pyautogui, config, args, 456, schedule_rect, date(2026, 7, 15))

        wait_picker.assert_called_once_with(
            class_name="EVA_Window_Dblclk",
            exclude_handles={111, 456},
            timeout=2.0,
        )
        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 110, 220),
                ("click", 1030, 2040),
                ("click", 1070, 2160),
            ],
        )

    def test_select_schedule_time_uses_popup_and_keyboard_navigation(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        config = {
            "schedule_ui": {
                "ampm_field": {"x_offset": 10, "y_offset": 20},
                "hour_field": {"x_offset": 50, "y_offset": 20},
                "minute_field": {"x_offset": 70, "y_offset": 20},
            },
        }

        ampm_popup = (333, "EVA_Window_Dblclk", "", (1000, 2000, 1045, 2066))
        hour_popup = (444, "EVA_Window_Dblclk", "", (1100, 2000, 1134, 2126))
        minute_popup = (555, "EVA_Window_Dblclk", "", (1200, 2000, 1234, 2126))
        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", side_effect=[ampm_popup, hour_popup, minute_popup]),
        ):
            select_schedule_time(pyautogui, config, args, schedule_rect, datetime(2026, 6, 17, 15, 30))

        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 110, 220),
                ("click", 1022, 2050),
                ("click", 150, 220),
                ("press", "down"),
                ("press", "down"),
                ("press", "down"),
                ("press", "down"),
                ("press", "down"),
                ("press", "enter"),
                ("click", 170, 220),
                ("press", "down"),
                ("press", "down"),
                ("press", "down"),
                ("press", "down"),
                ("press", "down"),
                ("press", "enter"),
            ],
        )

    def test_click_schedule_submit_activation_failure_raises_focus_safety_error(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)

        with (
            patch("kakao_sender.activate_window_handle", return_value=False),
            patch("kakao_sender.click_window_offset") as click_offset,
        ):
            with self.assertRaisesRegex(FocusSafetyError, "예약 메시지 창을 앞으로 가져오지 못했습니다"):
                click_schedule_submit(pyautogui, {}, args, 456, (100, 200, 500, 800))

        click_offset.assert_not_called()

    def test_send_scheduled_one_missing_schedule_rect_raises_before_paste_or_cleanup(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0, no_close_chat=False)
        row = MessageRow(2, "테스트방", "예약 메시지")
        chat = OpenChatContext(main_handle=111, chat_handle=222, chat_title="테스트방")

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.open_chat", return_value=chat),
            patch("kakao_sender.open_schedule_dialog", return_value=456),
            patch("kakao_sender.get_window_rect", return_value=None),
            patch("kakao_sender.paste_schedule_message") as paste_message,
            patch("kakao_sender.set_schedule_datetime") as set_datetime,
            patch("kakao_sender.click_schedule_submit") as submit,
            patch("kakao_sender.close_chat_after_send") as close_chat,
            patch("kakao_sender.focus_main_and_click_search") as focus_search,
            patch("kakao_sender.clear_search_box") as clear_search,
        ):
            with self.assertRaisesRegex(ScheduleNotSubmittedError, "예약 메시지 창 위치를 확인하지 못했습니다"):
                send_scheduled_one(pyautogui, {}, row, args, datetime(2026, 6, 18, 9, 5))

        paste_message.assert_not_called()
        set_datetime.assert_not_called()
        submit.assert_not_called()
        close_chat.assert_not_called()
        focus_search.assert_not_called()
        clear_search.assert_not_called()

    def test_send_scheduled_one_window_not_closing_raises_before_chat_close_or_search_clear(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0, no_close_chat=False, confirm_schedule_ready=lambda row, target: True)
        row = MessageRow(2, "테스트방", "예약 메시지")
        chat = OpenChatContext(main_handle=111, chat_handle=222, chat_title="테스트방")
        schedule_rect = (100, 200, 500, 800)

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.open_chat", return_value=chat),
            patch("kakao_sender.open_schedule_dialog", return_value=456),
            patch("kakao_sender.get_window_rect", return_value=schedule_rect),
            patch("kakao_sender.paste_schedule_message") as paste_message,
            patch("kakao_sender.set_schedule_datetime") as set_datetime,
            patch("kakao_sender.click_schedule_submit") as submit,
            patch("kakao_sender.wait_until_window_gone", return_value=False),
            patch("kakao_sender.close_chat_after_send") as close_chat,
            patch("kakao_sender.focus_main_and_click_search") as focus_search,
            patch("kakao_sender.clear_search_box") as clear_search,
        ):
            with self.assertRaisesRegex(FocusSafetyError, "예약 메시지 창이 닫히지 않아"):
                send_scheduled_one(pyautogui, {}, row, args, datetime(2026, 6, 18, 9, 5))

        paste_message.assert_called_once_with(pyautogui, {}, args, 456, schedule_rect, row.text)
        set_datetime.assert_called_once_with(pyautogui, {}, args, 456, schedule_rect, datetime(2026, 6, 18, 9, 5))
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args, (pyautogui, {}, args, 456, schedule_rect))
        self.assertTrue(callable(submit.call_args.kwargs["before_dispatch"]))
        close_chat.assert_not_called()
        focus_search.assert_not_called()
        clear_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
