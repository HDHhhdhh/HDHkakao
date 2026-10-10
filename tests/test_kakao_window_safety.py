from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kakao_sender import (
    FocusSafetyError,
    MessageRow,
    close_chat_after_send,
    focus_main_and_click_search,
    focus_kakao_window_via_uia,
    open_chat,
    search_box_config_from_position,
)


class KakaoWindowSafetyTests(unittest.TestCase):
    def setUp(self):
        # Synthetic windows in these established workflow tests; real guard
        # decision cases live in test_kakao_sender_dispatch_safety.py.
        guard = patch("kakao_sender.verify_window_target", return_value=77)
        guard.start()
        self.addCleanup(guard.stop)

    def test_focus_search_uses_window_relative_offset_when_available(self):
        pyautogui = FakePyAutoGui()
        config = {
            "app_titles": ["KakaoTalk"],
            "search_box": {"x": 10, "y": 20, "window_offset": {"x": 40, "y": 50}},
            "require_main_window_focus": True,
            "delays": {},
        }
        args = SimpleNamespace(delay_scale=0)

        with (
            patch("kakao_sender.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender.get_foreground_window_handle", return_value=111),
            patch("kakao_sender.get_window_title", return_value="KakaoTalk"),
            patch("kakao_sender.get_window_rect", return_value=(100, 200, 600, 800)),
        ):
            focus_main_and_click_search(pyautogui, config, args)

        self.assertEqual(pyautogui.actions, [("click", 140, 250)])

    def test_focus_search_restores_main_window_via_uia_when_pygetwindow_misses(self):
        pyautogui = FakePyAutoGui()
        config = {
            "app_titles": ["KakaoTalk"],
            "search_box": {"x": 10, "y": 20},
            "restore_main_window_via_uia": True,
            "require_main_window_focus": True,
            "delays": {},
        }
        args = SimpleNamespace(delay_scale=0)

        with (
            patch("kakao_sender.focus_kakao_window", return_value=None),
            patch("kakao_sender.focus_kakao_window_via_uia", return_value="KakaoTalk") as restore,
            patch("kakao_sender.get_foreground_window_handle", return_value=111),
            patch("kakao_sender.get_window_title", return_value="KakaoTalk"),
            patch("kakao_sender.get_window_rect", return_value=(0, 0, 500, 800)),
        ):
            focus_main_and_click_search(pyautogui, config, args)

        restore.assert_called_once_with(["KakaoTalk"])
        self.assertEqual(pyautogui.actions, [("click", 10, 20)])

    def test_focus_search_shortcut_avoids_stale_window_coordinates(self):
        pyautogui = FakePyAutoGui()
        config = {
            "app_titles": ["KakaoTalk"],
            "search_box": {"x": 9999, "y": 9999, "window_offset": {"x": 9999, "y": 9999}},
            "open_search_shortcut": ["ctrl", "f"],
            "require_main_window_focus": True,
            "delays": {},
        }
        args = SimpleNamespace(delay_scale=0)

        with (
            patch("kakao_sender.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender.get_foreground_window_handle", return_value=111),
            patch("kakao_sender.get_window_title", return_value="KakaoTalk"),
            patch("kakao_sender.get_window_rect", return_value=(100, 200, 500, 800)),
        ):
            focus_main_and_click_search(pyautogui, config, args)

        self.assertEqual(pyautogui.actions, [("hotkey", ("ctrl", "f"))])

    def test_uia_focus_restores_taskbar_only_kakaotalk_without_saved_icon_coordinates(self):
        state = {"restored": False}

        class Rect:
            left = 100
            top = 100
            right = 700
            bottom = 900

        class MainInfo:
            rectangle = Rect()
            control_type = "Window"
            visible = True
            enabled = True
            handle = 222

        class MainWindow:
            element_info = MainInfo()

            @staticmethod
            def window_text():
                return "KakaoTalk"

            @staticmethod
            def click_input(coords=None):
                state["main_click"] = coords

        class TaskbarInfo:
            control_type = "Button"
            automation_id = r"Appid: token\Kakao\KakaoTalk\KakaoTalk.exe"

        class TaskbarButton:
            element_info = TaskbarInfo()

            @staticmethod
            def click_input():
                state["restored"] = True

        class Taskbar:
            @staticmethod
            def descendants():
                return [TaskbarButton()]

        class Desktop:
            @staticmethod
            def windows():
                return [MainWindow()] if state["restored"] else []

            @staticmethod
            def window(**_kwargs):
                return Taskbar()

        with (
            patch("kakao_sender.get_foreground_window_handle", return_value=222),
            patch("kakao_sender.get_window_title", return_value="KakaoTalk"),
            patch("kakao_sender.time.sleep"),
        ):
            title = focus_kakao_window_via_uia(
                ["KakaoTalk"],
                desktop_factory=lambda **_kwargs: Desktop(),
                wait_seconds=0.1,
            )

        self.assertEqual(title, "KakaoTalk")
        self.assertTrue(state["restored"])
        self.assertEqual(state["main_click"], (200, 15))

    def test_uia_focus_restores_notification_tray_only_kakaotalk_by_semantic_name(self):
        state = {"overflow_open": False, "restored": False}

        class Rect:
            left = 100
            top = 100
            right = 700
            bottom = 900

        class MainInfo:
            rectangle = Rect()
            control_type = "Window"
            visible = True
            enabled = True
            handle = 333

        class MainWindow:
            element_info = MainInfo()

            @staticmethod
            def window_text():
                return "카카오톡"

            @staticmethod
            def click_input(coords=None):
                state["main_click"] = coords

        class HiddenInfo:
            control_type = "Button"
            automation_id = "SystemTrayIcon"

        class HiddenButton:
            element_info = HiddenInfo()

            @staticmethod
            def window_text():
                return "숨겨진 아이콘 표시"

            @staticmethod
            def click_input():
                state["overflow_open"] = True

        class TrayIconInfo:
            control_type = "Button"
            automation_id = "NotifyItemIcon"

        class TrayIcon:
            element_info = TrayIconInfo()

            @staticmethod
            def window_text():
                return "KakaoTalk"

            @staticmethod
            def double_click_input():
                state["restored"] = True

        class OverflowInfo:
            class_name = "TopLevelWindowForOverflowXamlIsland"

        class Overflow:
            element_info = OverflowInfo()

            @staticmethod
            def descendants():
                return [TrayIcon()]

        class Taskbar:
            @staticmethod
            def descendants():
                return [HiddenButton()]

        class Desktop:
            @staticmethod
            def windows():
                if state["restored"]:
                    return [MainWindow()]
                if state["overflow_open"]:
                    return [Overflow()]
                return []

            @staticmethod
            def window(**_kwargs):
                return Taskbar()

        with (
            patch("kakao_sender.get_foreground_window_handle", return_value=333),
            patch("kakao_sender.get_window_title", return_value="카카오톡"),
            patch("kakao_sender.time.sleep"),
        ):
            title = focus_kakao_window_via_uia(
                ["KakaoTalk", "카카오톡"],
                desktop_factory=lambda **_kwargs: Desktop(),
                wait_seconds=0.1,
            )

        self.assertEqual(title, "카카오톡")
        self.assertTrue(state["overflow_open"])
        self.assertTrue(state["restored"])
        self.assertEqual(state["main_click"], (200, 15))

    def test_open_chat_closes_comment_view_before_raising(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": ["KakaoTalk"],
            "clear_search_strategy": "select_text",
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "require_new_chat_window": True,
            "verify_chat_title": True,
            "non_chat_view_close_key": "esc",
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0)

        with (
            patch("kakao_sender.set_clipboard"),
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 222]),
            patch("kakao_sender.get_window_title", return_value="댓글 모아보기"),
        ):
            with self.assertRaisesRegex(FocusSafetyError, "댓글 모아보기"):
                open_chat(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        self.assertEqual(pyautogui.actions[-1], ("press", "esc"))
        self.assertEqual(pyautogui.actions.count(("hotkey", ("ctrl", "v"))), 1)

    def test_open_chat_reuses_window_relative_offset_without_refocusing(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20, "window_offset": {"x": 40, "y": 50}},
            "app_titles": ["KakaoTalk"],
            "clear_search_strategy": "select_text",
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "require_new_chat_window": True,
            "verify_chat_title": True,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0)

        with (
            patch("kakao_sender.set_clipboard"),
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 111, 222]),
            patch("kakao_sender.get_window_title", side_effect=["KakaoTalk", "테스트방"]),
            patch("kakao_sender.get_window_rect", return_value=(100, 200, 600, 800)),
        ):
            open_chat(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        self.assertEqual(pyautogui.actions[0], ("click", 140, 250))

    def test_search_box_config_stores_window_relative_offset(self):
        with (
            patch("kakao_sender.get_root_window_handle_at_point", return_value=111),
            patch("kakao_sender.get_window_rect", return_value=(100, 200, 600, 800)),
            patch("kakao_sender.get_window_title", return_value="KakaoTalk"),
        ):
            search_box = search_box_config_from_position(140, 250, ["KakaoTalk"])

        self.assertEqual(search_box, {"x": 140, "y": 250, "window_offset": {"x": 40, "y": 50}})

    def test_close_chat_after_send_rejects_main_window_title_as_close_target(self):
        # Given
        pyautogui = FakePyAutoGui()
        config = {"app_titles": ["KakaoTalk"], "delays": {}}
        args = SimpleNamespace(delay_scale=0)

        # When / Then
        with (
            patch("kakao_sender.close_window_handle") as close_window_handle,
            self.assertRaisesRegex(FocusSafetyError, "메인 창이 닫힘 대상"),
        ):
            close_chat_after_send(pyautogui, config, args, main_handle=111, chat_handle=222, chat_title="KakaoTalk")

        close_window_handle.assert_not_called()
        self.assertEqual(pyautogui.actions, [])

    def test_close_chat_after_send_without_chat_handle_does_not_use_global_alt_f4_by_default(self):
        # Given
        pyautogui = FakePyAutoGui()
        config = {"app_titles": ["KakaoTalk"], "close_chat_key": ["alt", "f4"], "delays": {}}
        args = SimpleNamespace(delay_scale=0)

        # When / Then
        with self.assertRaisesRegex(FocusSafetyError, "닫을 대화창 핸들"):
            close_chat_after_send(pyautogui, config, args, main_handle=111, chat_handle=None, chat_title="테스트방")

        self.assertNotIn(("hotkey", ("alt", "f4")), pyautogui.actions)
        self.assertEqual(pyautogui.actions, [])

    def test_close_chat_after_send_waits_for_exact_chat_handle_to_disappear(self):
        # Given
        pyautogui = FakePyAutoGui()
        config = {"app_titles": ["KakaoTalk"], "delays": {}}
        args = SimpleNamespace(delay_scale=0)

        # When
        with (
            patch("kakao_sender.close_window_handle", return_value=True) as close_window_handle,
            patch("kakao_sender.wait_until_window_gone", return_value=True) as wait_until_window_gone,
            patch("kakao_sender.close_windows_with_title", return_value=0) as close_windows_with_title,
            patch("kakao_sender.window_handle_visible", return_value=False),
            patch("kakao_sender.sleep_delay"),
        ):
            close_chat_after_send(pyautogui, config, args, main_handle=111, chat_handle=222, chat_title="테스트방")

        # Then
        close_window_handle.assert_called_once_with(222)
        wait_until_window_gone.assert_called_once_with(222, timeout=1.5)
        close_windows_with_title.assert_not_called()
        self.assertEqual(pyautogui.actions, [])

    def test_close_chat_after_send_uses_embedded_chat_close_key_for_main_handle_chat(self):
        # Given
        pyautogui = FakePyAutoGui()
        config = {"app_titles": ["KakaoTalk"], "embedded_chat_close_key": "esc", "delays": {}}
        args = SimpleNamespace(delay_scale=0)

        # When
        with (
            patch("kakao_sender.close_windows_with_title", return_value=0),
            patch("kakao_sender.sleep_delay"),
        ):
            close_chat_after_send(pyautogui, config, args, main_handle=111, chat_handle=111, chat_title="테스트방")

        # Then
        self.assertEqual(pyautogui.actions, [("press", "esc")])

    def test_close_chat_after_send_raises_when_exact_chat_handle_remains_visible_after_close(self):
        # Given
        pyautogui = FakePyAutoGui()
        config = {"app_titles": ["KakaoTalk"], "delays": {}}
        args = SimpleNamespace(delay_scale=0)

        # When / Then
        with (
            patch("kakao_sender.close_window_handle", return_value=True),
            patch("kakao_sender.wait_until_window_gone", return_value=True),
            patch("kakao_sender.close_windows_with_title", return_value=0),
            patch("kakao_sender.window_handle_visible", return_value=True),
            patch("kakao_sender.sleep_delay"),
            self.assertRaisesRegex(FocusSafetyError, "대화창을 닫지 못해"),
        ):
            close_chat_after_send(pyautogui, config, args, main_handle=111, chat_handle=222, chat_title="테스트방")

        self.assertEqual(pyautogui.actions, [])

    def test_search_box_config_keeps_absolute_position_without_usable_main_window(self):
        # Given / When
        with (
            patch("kakao_sender.get_root_window_handle_at_point", return_value=111),
            patch("kakao_sender.get_window_rect", return_value=(100, 200, 600, 800)),
            patch("kakao_sender.get_window_title", return_value="테스트방"),
        ):
            search_box = search_box_config_from_position(140, 250, ["KakaoTalk"])

        # Then
        self.assertEqual(search_box, {"x": 140, "y": 250})


class FakePyAutoGui:
    def __init__(self):
        self.actions = []

    def click(self, x, y):
        self.actions.append(("click", x, y))

    def hotkey(self, *keys):
        self.actions.append(("hotkey", tuple(keys)))

    def press(self, key):
        self.actions.append(("press", key))


if __name__ == "__main__":
    unittest.main()
