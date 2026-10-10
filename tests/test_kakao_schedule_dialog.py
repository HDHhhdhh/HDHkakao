from __future__ import annotations

from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from kakao_sender import OpenChatContext, find_schedule_plus_button_point, open_schedule_dialog


WindowRect = tuple[int, int, int, int]
WindowResult = tuple[int, str, str, WindowRect]
WindowQueryValue = str | set[int] | float


class FakePyAutoGui:
    def __init__(self) -> None:
        self.actions: list[tuple[str, int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))


class ScheduleDialogTests(unittest.TestCase):
    def setUp(self):
        guard = patch("kakao_sender.verify_window_target", return_value=77)
        guard.start()
        self.addCleanup(guard.stop)
        finder = patch("kakao_sender.find_schedule_plus_button_point", return_value=None)
        finder.start()
        self.addCleanup(finder.stop)

    def test_named_plus_button_is_found_within_each_chat_window(self) -> None:
        control = SimpleNamespace(element_info=SimpleNamespace(
            name="첨부", visible=True, enabled=True,
            rectangle=SimpleNamespace(left=528, top=648, right=560, bottom=680),
        ))
        desktop = SimpleNamespace(window=lambda **kwargs: SimpleNamespace(
            descendants=lambda **kwargs: [control],
        ))
        with patch.dict(sys.modules, {"pywinauto": SimpleNamespace(Desktop=lambda **kwargs: desktop)}):
            self.assertEqual(find_schedule_plus_button_point(222, (500, 100, 900, 700)), (544, 664))
            self.assertIsNone(find_schedule_plus_button_point(222, (0, 0, 400, 600)))

    def test_open_schedule_dialog_prefers_detected_plus_button(self) -> None:
        pyautogui = FakePyAutoGui()
        args = SimpleNamespace(delay_scale=0)
        chat = OpenChatContext(main_handle=111, chat_handle=222, chat_title="테스트방")
        with (
            patch("kakao_sender.find_schedule_plus_button_point", return_value=(544, 664)),
            patch("kakao_sender.activate_window_handle", return_value=True),
            patch("kakao_sender.get_window_rect", return_value=(500, 100, 900, 700)),
            patch("kakao_sender.find_kakao_windows", return_value=[]),
            patch("kakao_sender.wait_for_kakao_window", side_effect=[
                (333, "EVA_Menu", "", (1000, 2000, 1200, 2100)),
                (444, "EVA_Window", "예약 메시지", (200, 200, 500, 800)),
            ]),
        ):
            handle = open_schedule_dialog(pyautogui, {}, args, chat)
        self.assertEqual(handle, 444)
        self.assertEqual(pyautogui.actions, [("click", 544, 664), ("click", 1075, 2045)])

    def test_open_schedule_dialog_prefers_chat_specific_plus_button_offsets(self) -> None:
        pyautogui = FakePyAutoGui()
        args = SimpleNamespace(delay_scale=0)
        chat = OpenChatContext(main_handle=111, chat_handle=222, chat_title="테스트방")
        chat_rect = (0, 0, 400, 800)
        menu_rect = (1000, 2000, 1200, 2100)
        config = {
            "schedule_ui": {
                "plus_button": {"x_offset": 24, "y_offset": -23},
                "plus_button_by_chat": {
                    "테스트방": [{"x_offset": 64, "y_offset": -58}],
                },
                "reservation_menu": {"x_offset": 75, "y_offset": 45},
            }
        }

        def wait_for_window(**kwargs: WindowQueryValue) -> WindowResult | None:
            if kwargs.get("class_name") == "EVA_Menu":
                if pyautogui.actions[-1] == ("click", 64, 742):
                    return (333, "EVA_Menu", "", menu_rect)
                return None
            if kwargs.get("title") == "예약 메시지":
                return (444, "EVA_Window", "예약 메시지", (200, 200, 500, 800))
            return None

        with (
            patch("kakao_sender.activate_window_handle", return_value=True),
            patch("kakao_sender.get_window_rect", return_value=chat_rect),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", side_effect=wait_for_window),
        ):
            schedule_handle = open_schedule_dialog(pyautogui, config, args, chat)

        self.assertEqual(schedule_handle, 444)
        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 64, 742),
                ("click", 1075, 2045),
            ],
        )

    def test_open_schedule_dialog_tries_fallback_plus_button_offsets(self) -> None:
        pyautogui = FakePyAutoGui()
        args = SimpleNamespace(delay_scale=0)
        chat = OpenChatContext(main_handle=111, chat_handle=222, chat_title="테스트방")
        chat_rect = (0, 0, 400, 800)
        menu_rect = (1000, 2000, 1200, 2100)
        config = {
            "schedule_ui": {
                "plus_button": {"x_offset": 24, "y_offset": -23},
                "plus_button_candidates": [{"x_offset": 52, "y_offset": -48}],
                "reservation_menu": {"x_offset": 75, "y_offset": 45},
            }
        }

        with (
            patch("kakao_sender.activate_window_handle", return_value=True) as activate_window,
            patch("kakao_sender.get_window_rect", return_value=chat_rect),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch(
                "kakao_sender.wait_for_kakao_window",
                side_effect=[
                    None,
                    (333, "EVA_Menu", "", menu_rect),
                    (444, "EVA_Window", "예약 메시지", (200, 200, 500, 800)),
                ],
            ) as wait_window,
        ):
            schedule_handle = open_schedule_dialog(pyautogui, config, args, chat)

        self.assertEqual(schedule_handle, 444)
        activate_window.assert_called_once_with(222)
        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 24, 777),
                ("click", 52, 752),
                ("click", 1075, 2045),
            ],
        )
        self.assertEqual(wait_window.call_count, 3)
        self.assertEqual(
            wait_window.call_args_list[0].kwargs,
            {"class_name": "EVA_Menu", "exclude_handles": {111}, "timeout": 2.0},
        )
        self.assertEqual(
            wait_window.call_args_list[1].kwargs,
            {"class_name": "EVA_Menu", "exclude_handles": {111}, "timeout": 2.0},
        )
        self.assertEqual(
            wait_window.call_args_list[2].kwargs,
            {"title": "예약 메시지", "exclude_handles": {111}, "timeout": 3.0},
        )

    def test_open_schedule_dialog_uses_screenshot_plus_button_fallback_offset(self) -> None:
        pyautogui = FakePyAutoGui()
        args = SimpleNamespace(delay_scale=0)
        chat = OpenChatContext(main_handle=111, chat_handle=222, chat_title="학생갸T")
        chat_rect = (0, 0, 400, 800)
        menu_rect = (1000, 2000, 1200, 2100)
        config = {
            "schedule_ui": {
                "plus_button": {"x_offset": 24, "y_offset": -23},
                "reservation_menu": {"x_offset": 75, "y_offset": 45},
            }
        }

        def wait_for_window(**kwargs: WindowQueryValue) -> WindowResult | None:
            if kwargs.get("class_name") == "EVA_Menu":
                if pyautogui.actions[-1] == ("click", 36, 758):
                    return (333, "EVA_Menu", "", menu_rect)
                return None
            if kwargs.get("title") == "예약 메시지":
                return (444, "EVA_Window", "예약 메시지", (200, 200, 500, 800))
            return None

        with (
            patch("kakao_sender.activate_window_handle", return_value=True),
            patch("kakao_sender.get_window_rect", return_value=chat_rect),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", side_effect=wait_for_window),
        ):
            schedule_handle = open_schedule_dialog(pyautogui, config, args, chat)

        self.assertEqual(schedule_handle, 444)
        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 24, 777),
                ("click", 36, 758),
                ("click", 1075, 2045),
            ],
        )


if __name__ == "__main__":
    unittest.main()
