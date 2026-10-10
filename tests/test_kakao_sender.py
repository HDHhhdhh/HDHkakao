from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
import struct
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from kakao_sender import (
    FocusSafetyError,
    MessageRow,
    format_schedule_datetime,
    load_csv_messages_from_bytes,
    load_csv_messages_from_text,
    load_messages,
    load_xlsx_messages_from_bytes,
    parse_schedule_datetime,
    return_to_recent_chat_top,
    schedule_datetime_for_row,
    send_one,
    set_file_clipboard,
    chat_title_matches,
    validate_schedule_row,
    validate_send_mode,
)
from kakao_sender_gui import (
    attachment_status_text,
    failed_result_indexes,
    format_preflight_warning,
    is_fatal_send_error,
    row_status_tag,
    rows_to_csv_text,
    rows_with_common_attachments,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class LoadMessagesTests(unittest.TestCase):
    def setUp(self):
        # Existing workflow tests use synthetic HWNDs. Guard behavior itself is
        # covered without this seam in test_kakao_sender_dispatch_safety.py.
        for target, value in (("verify_window_target", 77), ("verify_attachment_target", None), ("get_foreground_window_handle", 111), ("get_window_title", "테스트방"), ("focus_kakao_window_via_uia", None), ("focus_kakao_window", None)):
            patcher = patch(f"kakao_sender.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_loads_korean_csv_headers(self):
        rows = load_messages(FIXTURES / "korean_messages.csv")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].chat_name, "테스트방")
        self.assertEqual(rows[0].text, "안녕하세요")

    def test_skips_disabled_rows(self):
        rows = load_messages(FIXTURES / "disabled_messages.csv")

        self.assertEqual([row.chat_name for row in rows], ["room one"])

    def test_loads_utf16_tab_delimited_csv(self):
        data = "카카오톡 대화명\t텍스트\n테스트방\t안녕하세요\n".encode("utf-16")

        rows = load_csv_messages_from_bytes(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].chat_name, "테스트방")
        self.assertEqual(rows[0].text, "안녕하세요")

    def test_finds_header_after_blank_xlsx_rows(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([None, None, None, None])
        sheet.append([None, None, None, None])
        sheet.append([None, None, None, None])
        sheet.append([None, None, "대화명", "텍스트"])
        sheet.append([None, None, "[도승지] 학생갸", "마지막"])
        buffer = BytesIO()
        workbook.save(buffer)

        rows = load_xlsx_messages_from_bytes(buffer.getvalue())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].row_number, 5)
        self.assertEqual(rows[0].chat_name, "[도승지] 학생갸")
        self.assertEqual(rows[0].text, "마지막")

    def test_loads_attachment_columns_and_relative_paths(self):
        base_dir = Path("C:/base")
        text = "카카오톡 대화명,텍스트,첨부\n테스트방,안녕하세요,docs/a.pdf;images/b.png\n"

        rows = load_csv_messages_from_text(text, base_dir=base_dir)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attachments, (base_dir / "docs" / "a.pdf", base_dir / "images" / "b.png"))
        self.assertEqual(rows[0].image_count, 1)

    def test_loads_schedule_column(self):
        text = "카카오톡 대화명,텍스트,예약일시\n테스트방,예약입니다,2026-05-27 09:30\n"

        rows = load_csv_messages_from_text(text)

        self.assertEqual(rows[0].scheduled_at, datetime(2026, 5, 27, 9, 30))

    def test_parse_schedule_datetime_accepts_korean_ampm(self):
        parsed = parse_schedule_datetime("2026년 5월 27일 오후 3시 10분")

        self.assertEqual(parsed, datetime(2026, 5, 27, 15, 10))

    def test_parse_schedule_datetime_accepts_dotted_korean_date(self):
        parsed = parse_schedule_datetime("2026.05.27. 09:30")

        self.assertEqual(parsed, datetime(2026, 5, 27, 9, 30))

    def test_allows_attachment_only_rows(self):
        base_dir = Path("C:/base")
        text = "대화명,첨부\n테스트방,only.png\n"

        rows = load_csv_messages_from_text(text, base_dir=base_dir)

        self.assertEqual(rows[0].text, "")
        self.assertEqual(rows[0].attachments, (base_dir / "only.png",))

    def test_set_file_clipboard_uses_hdrop_binary_payload(self):
        calls = []
        fake_clipboard = SimpleNamespace(
            OpenClipboard=lambda: calls.append(("open",)),
            EmptyClipboard=lambda: calls.append(("empty",)),
            SetClipboardData=lambda fmt, data: calls.append(("set", fmt, data)),
            CloseClipboard=lambda: calls.append(("close",)),
        )
        fake_con = SimpleNamespace(CF_HDROP=15)

        with patch.dict("sys.modules", {"win32clipboard": fake_clipboard, "win32con": fake_con}):
            set_file_clipboard((Path("C:/tmp/a.png"), Path("C:/tmp/b.png")))

        set_call = next(call for call in calls if call[0] == "set")
        self.assertEqual(set_call[1], 15)
        self.assertIsInstance(set_call[2], bytes)
        self.assertEqual(set_call[2][:20], struct.pack("<IiiII", 20, 0, 0, 0, 1))
        payload = set_call[2][20:].decode("utf-16le")
        self.assertIn("a.png", payload)
        self.assertIn("b.png", payload)
        self.assertTrue(payload.endswith("\0\0"))

    def test_gui_common_attachment_helper_replaces_every_row(self):
        attachments = (Path("C:/tmp/common.pdf"), Path("C:/tmp/image.png"))
        rows = [
            MessageRow(2, "A", "one", (Path("C:/tmp/old-a.pdf"),)),
            MessageRow(3, "B", "two", (Path("C:/tmp/old-b.pdf"),)),
        ]

        updated = rows_with_common_attachments(rows, attachments)

        self.assertEqual([row.attachments for row in updated], [attachments, attachments])
        self.assertEqual(attachment_status_text(updated), ("2개", "파일 2개 · 모든 대화명 공통"))

    def test_send_one_closes_verified_chat_and_clears_search_without_escape(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": ["KakaoTalk"],
            "clear_search_strategy": "select_text",
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "send_enter_presses": 2,
            "send_enter_interval": 0,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with (
            patch("kakao_sender.set_clipboard"),
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 222, None]),
            patch("kakao_sender.get_window_title", return_value="테스트방"),
            patch("kakao_sender.close_window_handle", return_value=True),
            patch("kakao_sender.wait_until_window_gone", return_value=True),
            patch("kakao_sender.close_windows_with_title", return_value=0),
        ):
            send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 10, 20),
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
                ("press", "enter"),
                ("click", 10, 20),
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
            ],
        )

    def test_send_one_sends_attachments_after_verified_chat(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": ["KakaoTalk"],
            "clear_search_strategy": "select_text",
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "send_enter_presses": 1,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)
        attachments = (Path("C:/tmp/a.png"), Path("C:/tmp/b.png"))

        with (
            patch("kakao_sender.set_clipboard"),
            patch("kakao_sender.set_file_clipboard") as file_clipboard,
            patch("pathlib.Path.exists", return_value=True),
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 222, None]),
            patch("kakao_sender.get_window_title", return_value="테스트방"),
            patch("kakao_sender.close_window_handle", return_value=True),
            patch("kakao_sender.wait_until_window_gone", return_value=True),
            patch("kakao_sender.close_windows_with_title", return_value=0),
        ):
            send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요", attachments), args)

        file_clipboard.assert_called_once_with(attachments)
        self.assertEqual(pyautogui.actions.count(("hotkey", ("ctrl", "v"))), 3)
        self.assertIn(("press", "enter"), pyautogui.actions)

    def test_send_one_does_not_touch_attachments_before_chat_verification(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": [],
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_new_chat_window": True,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with (
            patch("kakao_sender.missing_attachment_paths", return_value=[]),
            patch("kakao_sender.set_clipboard") as text_clipboard,
            patch("kakao_sender.set_file_clipboard") as file_clipboard,
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 111]),
            patch("kakao_sender.get_window_title", return_value=""),
        ):
            with self.assertRaises(FocusSafetyError):
                send_one(pyautogui, config, MessageRow(2, "테스트방", "", (Path("C:/tmp/a.png"),)), args)

        text_clipboard.assert_called_once_with("테스트방")
        file_clipboard.assert_not_called()

    def test_send_one_stops_when_window_handles_are_unavailable(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": [],
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with patch("kakao_sender.get_foreground_window_handle", return_value=None):
            with self.assertRaises(FocusSafetyError):
                send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        self.assertNotIn(("hotkey", ("alt", "f4")), pyautogui.actions)

    def test_send_one_paste_only_leaves_message_open(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": [],
            "close_chat_key": "esc",
            "clear_search_key": "esc",
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_new_chat_window": False,
            "require_window_handles": False,
            "verify_chat_title": False,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=True, no_close_chat=False)

        with patch("kakao_sender.set_clipboard"):
            send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 10, 20),
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
                ("hotkey", ("ctrl", "v")),
            ],
        )

    def test_send_one_uses_embedded_close_when_chat_handle_is_main_handle(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": [],
            "embedded_chat_close_key": "esc",
            "clear_search_strategy": "select_text",
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "require_new_chat_window": False,
            "verify_chat_title": False,
            "send_enter_presses": 1,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with (
            patch("kakao_sender.set_clipboard"),
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 111, None]),
            patch("kakao_sender.get_window_title", return_value=""),
        ):
            send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 10, 20),
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
                ("press", "esc"),
                ("click", 10, 20),
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
            ],
        )

    def test_send_one_stops_before_message_paste_when_chat_window_does_not_change(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": [],
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_new_chat_window": True,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with (
            patch("kakao_sender.set_clipboard") as clipboard,
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 111]),
            patch("kakao_sender.get_window_title", return_value=""),
        ):
            with self.assertRaises(FocusSafetyError):
                send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        clipboard.assert_called_once_with("테스트방")
        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 10, 20),
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),
            ],
        )

    def test_send_one_stops_when_chat_target_is_main_window(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": ["KakaoTalk"],
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with (
            patch("kakao_sender.set_clipboard") as clipboard,
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 222]),
            patch("kakao_sender.get_window_title", return_value="KakaoTalk"),
            patch("kakao_sender.close_window_handle") as close_window,
        ):
            with self.assertRaises(FocusSafetyError):
                send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        clipboard.assert_called_once_with("테스트방")
        close_window.assert_not_called()

    def test_send_one_stops_when_chat_title_does_not_match_target(self):
        pyautogui = FakePyAutoGui()
        config = {
            "search_box": {"x": 10, "y": 20},
            "app_titles": ["KakaoTalk"],
            "focus_before_each_search": False,
            "require_main_window_focus": False,
            "require_window_handles": True,
            "verify_chat_title": True,
            "delays": {},
        }
        args = SimpleNamespace(assume_focused=True, delay_scale=0, paste_only=False, no_close_chat=False)

        with (
            patch("kakao_sender.set_clipboard") as clipboard,
            patch("kakao_sender.get_foreground_window_handle", side_effect=[111, 222]),
            patch("kakao_sender.get_window_title", return_value="다른 대화방"),
        ):
            with self.assertRaises(FocusSafetyError):
                send_one(pyautogui, config, MessageRow(2, "테스트방", "안녕하세요"), args)

        clipboard.assert_called_once_with("테스트방")
        self.assertEqual(pyautogui.actions.count(("hotkey", ("ctrl", "v"))), 1)

    def test_chat_title_match_requires_exact_name_except_known_app_suffix(self):
        self.assertTrue(chat_title_matches("테스트방", "테스트방 - KakaoTalk"))
        self.assertTrue(chat_title_matches("테스트방", "테스트방 - 카카오톡"))
        self.assertFalse(chat_title_matches("[도승지] 학생갸", "학생갸"))
        self.assertFalse(chat_title_matches("테스트방", "테스트방2"))
        self.assertFalse(chat_title_matches("테스트방", "다른 대화방"))

    def test_return_to_recent_chat_top_clears_search_exits_and_presses_home(self):
        pyautogui = FakePyAutoGui()
        config = {
            "clear_search_strategy": "select_text",
            "exit_search_key": "esc",
            "recent_list_top_key": "home",
            "delays": {},
        }
        args = SimpleNamespace(delay_scale=0)

        with patch("kakao_sender.focus_main_and_click_search") as focus:
            return_to_recent_chat_top(pyautogui, config, args)

        focus.assert_called_once_with(pyautogui, config, args)
        self.assertEqual(
            pyautogui.actions,
            [
                ("press", "home"),
                ("hotkey", ("shift", "end")),
                ("press", "backspace"),
                ("press", "esc"),
                ("press", "home"),
            ],
        )

    def test_validate_send_mode_blocks_multirow_risky_modes(self):
        with self.assertRaises(ValueError):
            validate_send_mode(2, paste_only=True, no_close_chat=False)
        with self.assertRaises(ValueError):
            validate_send_mode(2, paste_only=False, no_close_chat=True)

        validate_send_mode(1, paste_only=True, no_close_chat=True)

    def test_validate_schedule_row_blocks_past_and_attachments(self):
        future = datetime.now() + timedelta(days=1)
        future = future.replace(minute=future.minute - future.minute % 5, second=0, microsecond=0)
        validate_schedule_row(MessageRow(2, "테스트방", "예약"), future)

        with self.assertRaises(ValueError):
            validate_schedule_row(MessageRow(2, "테스트방", "예약", (Path("C:/tmp/a.png"),)), future)
        with self.assertRaises(ValueError):
            validate_schedule_row(MessageRow(2, "테스트방", "예약"), datetime.now() - timedelta(minutes=1))

    def test_schedule_datetime_for_row_prefers_row_value(self):
        row_schedule = datetime(2026, 5, 27, 9, 30)
        fallback = datetime(2026, 5, 28, 10, 0)

        self.assertEqual(schedule_datetime_for_row(MessageRow(2, "테스트방", "예약", scheduled_at=row_schedule), fallback), row_schedule)
        self.assertEqual(schedule_datetime_for_row(MessageRow(2, "테스트방", "예약"), fallback), fallback)

    def test_rows_to_csv_text_uses_korean_headers(self):
        schedule = datetime(2026, 5, 27, 9, 30)
        text = rows_to_csv_text([MessageRow(7, "테스트방", "줄1\n줄2", scheduled_at=schedule)])

        self.assertIn("카카오톡 대화명,텍스트,첨부,예약일시", text)
        self.assertIn("테스트방", text)
        self.assertIn(format_schedule_datetime(schedule), text)

    def test_failed_result_indexes_prefers_stored_row_index(self):
        rows = [
            MessageRow(2, "A", "one"),
            MessageRow(3, "B", "two"),
            MessageRow(4, "A", "three"),
        ]
        results = [
            {"row_index": 2, "row": 4, "chat_name": "A", "status": "error"},
            {"row": 3, "chat_name": "B", "status": "error"},
            {"row": 2, "chat_name": "A", "status": "sent"},
        ]

        self.assertEqual(failed_result_indexes(results, rows), [2, 1])

    def test_fatal_send_error_classification(self):
        self.assertTrue(is_fatal_send_error(FocusSafetyError("대화창을 닫지 못해 중단했습니다.")))
        self.assertFalse(is_fatal_send_error(FocusSafetyError("대화창이 새 창으로 열렸는지 확인하지 못해 중단했습니다.")))
        self.assertFalse(is_fatal_send_error(RuntimeError("random")))

    def test_preflight_warning_adds_action_for_search_coordinate(self):
        message = format_preflight_warning("카카오톡 대화명 검색창 좌표를 먼저 저장해 주세요.")

        self.assertIn("다음 행동", message)
        self.assertIn("좌표 찍기", message)

    def test_row_status_tag_maps_user_visible_states(self):
        self.assertEqual(row_status_tag("진행 중"), "running")
        self.assertEqual(row_status_tag("오류"), "error")
        self.assertEqual(row_status_tag("전송"), "success")
        self.assertEqual(row_status_tag("대기"), "ready")


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
