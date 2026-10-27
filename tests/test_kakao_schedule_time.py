from __future__ import annotations

from argparse import Namespace
from datetime import datetime, tzinfo
import unittest
from typing import override
from unittest.mock import patch

from kakao_sender import FocusSafetyError, MessageRow, default_schedule_time, earliest_schedule_datetime, parse_schedule_datetime, select_schedule_time, validate_schedule_row


WindowRect = tuple[int, int, int, int]
WindowResult = tuple[int, str, str, WindowRect]


class FrozenDatetime(datetime):
    @override
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        value = cls(2026, 7, 11, 20, 2)
        if tz is not None:
            return value.replace(tzinfo=tz)
        return value


class FakePyAutoGui:
    def __init__(self) -> None:
        self.actions: list[tuple[str, int, int] | tuple[str, str]] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))


class DynamicScheduleTimeTests(unittest.TestCase):
    def setUp(self):
        guard = patch("kakao_sender.verify_window_target", return_value=77)
        guard.start()
        self.addCleanup(guard.stop)
        foreground = patch("kakao_sender.get_foreground_window_handle", return_value=456)
        foreground.start()
        self.addCleanup(foreground.stop)

    def test_selects_hour_and_minute_by_keyboard_from_five_minute_default(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        config = {
            "schedule_ui": {
                "hour_field": {"x_offset": 50, "y_offset": 20},
                "minute_field": {"x_offset": 70, "y_offset": 20},
            }
        }
        popup = (333, "EVA_Window_Dblclk", "", (1000, 2000, 1034, 2126))

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", side_effect=[popup, popup]) as wait_popup,
        ):
            select_schedule_time(pyautogui, config, args, schedule_rect, datetime(2026, 7, 12, 22, 15))

        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 150, 220),
                ("press", "down"),
                ("press", "down"),
                ("press", "enter"),
                ("click", 170, 220),
                ("press", "down"),
                ("press", "enter"),
            ],
        )
        self.assertEqual(wait_popup.call_count, 2)
        self.assertEqual(
            wait_popup.call_args_list[0].kwargs,
            {"exclude_handles": {111}, "timeout": 2.0},
        )

    def test_selects_am_from_the_actual_two_row_popup(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        config = {"schedule_ui": {"ampm_field": {"x_offset": 10, "y_offset": 20}, "hour_field": {"x_offset": 50, "y_offset": 20}}}
        ampm_popup = (333, "EVA_Window_Dblclk", "", (1000, 2000, 1045, 2066))
        hour_popup = (444, "EVA_Window_Dblclk", "", (1100, 2000, 1134, 2126))

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", side_effect=[ampm_popup, hour_popup, hour_popup]),
        ):
            select_schedule_time(pyautogui, config, args, schedule_rect, datetime(2026, 7, 12, 10, 5))

        self.assertEqual(
            pyautogui.actions,
            [
                ("click", 110, 220),
                ("click", 1022, 2016),
                ("click", 150, 220),
                ("press", "down"),
                ("press", "down"),
                ("press", "enter"),
                ("click", 388, 650),
                ("press", "up"),
                ("press", "enter"),
            ],
        )

    def test_rejects_missing_am_option_before_changing_the_hour(self) -> None:
        pyautogui = FakePyAutoGui()
        args = Namespace(delay_scale=0)
        schedule_rect = (100, 200, 500, 800)
        config = {"schedule_ui": {"ampm_field": {"x_offset": 10, "y_offset": 20}, "hour_field": {"x_offset": 50, "y_offset": 20}}}
        pm_only_popup = (333, "EVA_Window_Dblclk", "", (1000, 2000, 1045, 2036))

        with (
            patch("kakao_sender.datetime", FrozenDatetime),
            patch("kakao_sender.find_kakao_windows", return_value=[(111, "EVA_Window", "기존", (0, 0, 1, 1))]),
            patch("kakao_sender.wait_for_kakao_window", return_value=pm_only_popup),
        ):
            with self.assertRaisesRegex(FocusSafetyError, "오전 선택 항목"):
                select_schedule_time(pyautogui, config, args, schedule_rect, datetime(2026, 7, 12, 10, 5))

        self.assertEqual(pyautogui.actions, [("click", 110, 220)])

    def test_rejects_minutes_that_kakaotalk_cannot_select(self) -> None:
        row = MessageRow(2, "테스트방", "예약 메시지")

        with self.assertRaisesRegex(ValueError, "5분 단위"):
            validate_schedule_row(row, datetime(2026, 7, 12, 22, 3), now=datetime(2026, 7, 11, 20, 2))


class ScheduleLeadTimeTests(unittest.TestCase):
    def test_ampm_hours_must_be_between_one_and_twelve(self):
        for marker in ("오전", "오후"):
            for hour in (0, 13, 23):
                with self.subTest(marker=marker, hour=hour), self.assertRaises(ValueError):
                    parse_schedule_datetime(f"2099년 10월 28일 {marker} {hour}시 00분")

    def test_valid_ampm_and_twenty_four_hour_times_remain_supported(self):
        for marker, hours in (("오전", {1: 1, 11: 11, 12: 0}), ("오후", {1: 13, 11: 23, 12: 12})):
            for hour, expected in hours.items():
                with self.subTest(marker=marker, hour=hour):
                    self.assertEqual(
                        parse_schedule_datetime(f"2099년 10월 28일 {marker} {hour}시 00분"),
                        datetime(2099, 10, 28, expected),
                    )
        for hour in (0, 13, 23):
            with self.subTest(hour=hour):
                self.assertEqual(parse_schedule_datetime(f"2099-10-28 {hour:02}:00"), datetime(2099, 10, 28, hour))

    def test_form_default_uses_the_same_five_minute_lead_time(self):
        self.assertEqual(default_schedule_time(datetime(2026, 9, 28, 12, 58)), datetime(2026, 9, 28, 13, 5))
        self.assertEqual(default_schedule_time(datetime(2026, 9, 28, 12, 58, 1)), datetime(2026, 9, 28, 13, 5))

    def test_earliest_slot_rounds_up_after_five_full_minutes(self):
        cases = (
            (datetime(2030, 1, 1, 10, 0), datetime(2030, 1, 1, 10, 5)),
            (datetime(2030, 1, 1, 10, 0, 0, 1), datetime(2030, 1, 1, 10, 10)),
            (datetime(2030, 1, 1, 10, 4, 59, 999999), datetime(2030, 1, 1, 10, 10)),
            (datetime(2030, 1, 1, 10, 5), datetime(2030, 1, 1, 10, 10)),
            (datetime(2030, 1, 1, 10, 55, 0, 1), datetime(2030, 1, 1, 11, 5)),
            (datetime(2030, 12, 31, 23, 55, 0, 1), datetime(2031, 1, 1, 0, 5)),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(earliest_schedule_datetime(now), expected)

    def test_exact_five_minutes_is_allowed_but_one_microsecond_less_is_not(self):
        row = MessageRow(2, "fixture-room", "fixture")
        target = datetime(2030, 1, 1, 10, 5)
        validate_schedule_row(row, target, now=datetime(2030, 1, 1, 10, 0))
        for now in (datetime(2030, 1, 1, 10, 0, 0, 1), datetime(2030, 1, 1, 10, 0, 1)):
            with self.subTest(now=now), self.assertRaisesRegex(ValueError, "최소 5분"):
                validate_schedule_row(row, target, now=now)

    def test_seconds_and_microseconds_must_be_zero(self):
        row = MessageRow(2, "fixture-room", "fixture")
        for target in (datetime(2030, 1, 1, 10, 10, 1), datetime(2030, 1, 1, 10, 10, 0, 1)):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "초와 마이크로초는 0"):
                validate_schedule_row(row, target, now=datetime(2030, 1, 1, 10, 0))

    def test_parser_preserves_precision_so_validation_cannot_silently_truncate_it(self):
        target = datetime(2030, 1, 1, 10, 10, 1, 1000)
        self.assertEqual(parse_schedule_datetime(target), target)
        self.assertEqual(parse_schedule_datetime("2030-01-01 10:10:01"), target.replace(microsecond=0))
        from openpyxl.utils.datetime import to_excel
        parsed_excel = parse_schedule_datetime(to_excel(target))
        self.assertEqual(parsed_excel, target)
        with self.assertRaisesRegex(ValueError, "초와 마이크로초는 0"):
            validate_schedule_row(MessageRow(2, "fixture-room", "fixture"), parsed_excel, now=datetime(2030, 1, 1, 10, 0))


if __name__ == "__main__":
    unittest.main()
