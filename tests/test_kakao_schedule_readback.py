from datetime import datetime
import unittest

from kakao_schedule_readback import (
    ScheduleReadbackError, find_dropdown_option_center, has_schedule_success_toast,
    read_calendar_month, read_schedule_form, verify_schedule_form,
)
from kakao_windows_ocr import OcrLine, OcrRect, OcrResult, OcrWord


def form(*, recipient="A 고양외1 학생가", day="2026. 9. 28 (월)", period="오후", minute="05"):
    def line(text, x, y, width):
        rect = OcrRect(x, y, width, 20)
        return OcrLine(text, (OcrWord(text, rect),), rect)
    # Keep the time split across OCR lines, as observed in the saved demo.
    return OcrResult(740, 1200, "ko", (
        line("예약한 본문 2030. 1. 1 (화) 오전 10:00", 60, 200, 600),
        line(recipient, 144, 800, 210),
        line(day, 163, 890, 161),
        line(period, 512, 890, 46), line("1", 573, 890, 8),
        line(": " + minute, 597, 890, 35),
    ))


class ScheduleReadbackTests(unittest.TestCase):
    def test_reads_split_time_using_field_geometry_not_ocr_line_order(self):
        result = form()
        shuffled = OcrResult(result.width, result.height, result.language, tuple(reversed(result.lines)))
        actual = read_schedule_form(shuffled)
        self.assertEqual(actual.chat_name, "A 고양외1 학생가")
        self.assertEqual(actual.scheduled_at, datetime(2026, 9, 28, 13, 5))

    def test_no_fuzzy_matching_of_recipient_date_or_period(self):
        for image in (form(recipient="A 고양외1 학생가B"), form(day="2026. 9. 29 (화)"), form(period="오전")):
            with self.subTest(image=image), self.assertRaises(ScheduleReadbackError):
                verify_schedule_form(image, chat_name="A 고양외1 학생가", scheduled_at=datetime(2026, 9, 28, 13, 5), expected_body="본문", actual_body="본문")

    def test_hidden_minutes_are_not_inferred(self):
        with self.assertRaises(ScheduleReadbackError):
            read_schedule_form(form(minute=""))

    def test_observed_colon_glyph_noise_does_not_change_numeric_time(self):
        original=form(minute='00')
        for separator in (':','•.','·'):
            lines=tuple(OcrLine(line.text.replace(':',separator),tuple(OcrWord(word.text.replace(':',separator),word.rect) for word in line.words),line.rect) for line in original.lines)
            image=OcrResult(original.width,original.height,original.language,lines)
            self.assertEqual(read_schedule_form(image).scheduled_at,datetime(2026,9,28,13,0))
        for minute in ('0','OO','100',''):
            with self.subTest(minute=minute),self.assertRaises(ScheduleReadbackError):read_schedule_form(form(minute=minute))

    def test_body_must_match_in_full_except_windows_line_endings(self):
        kwargs = dict(chat_name="A 고양외1 학생가", scheduled_at=datetime(2026, 9, 28, 13, 5), expected_body="첫째\n둘째")
        verify_schedule_form(form(), actual_body="첫째\r\n둘째", **kwargs)
        for body in ("첫째", "첫째\n둘째\n", "첫째\n틀림"):
            with self.subTest(body=body), self.assertRaises(ScheduleReadbackError):
                verify_schedule_form(form(), actual_body=body, **kwargs)

    def test_full_desktop_cannot_be_mistaken_for_reservation_form(self):
        original = form()
        with self.assertRaises(ScheduleReadbackError):
            read_schedule_form(OcrResult(2780, 1652, original.language, original.lines))


def words_image(*items):
    """Each tuple is (text, x, y, width, height), intentionally separate OCR lines."""
    lines = []
    for text, x, y, width, height in items:
        rect = OcrRect(x, y, width, height)
        lines.append(OcrLine(text, (OcrWord(text, rect),), rect))
    return OcrResult(600, 500, "ko", tuple(lines))


class CalendarDropdownToastTests(unittest.TestCase):
    def test_calendar_split_tokens_and_original_coordinates(self):
        image = words_image(("9월", 220, 20, 40, 20), ("2026년", 110, 18, 80, 24), ("28", 110, 100, 20, 20))
        self.assertEqual(read_calendar_month(image), (2026, 9))

    def test_calendar_is_exact_valid_and_unique(self):
        for heading in ("2026. 9.", "2026년 13월", "0000년 9월", "2026년 9월 예약", "2026년 O9월", "２０２６년 9월", "2026년 9월 28일", "20 26년 9월", "2026년 1 0월"):
            with self.subTest(heading=heading), self.assertRaises(ScheduleReadbackError):
                read_calendar_month(words_image((heading, 50, 20, 250, 20)))
        with self.assertRaises(ScheduleReadbackError):
            read_calendar_month(words_image(("2026년 9월", 20, 20, 150, 20), ("2026년 9월", 20, 100, 150, 20)))

    def test_dropdown_numeric_equivalence_and_exact_period(self):
        image = words_image(("오전", 50, 20, 40, 20), ("오후", 50, 60, 40, 20), ("05", 100, 120, 20, 20))
        self.assertEqual(find_dropdown_option_center(image, "오후"), (70, 70))
        self.assertEqual(find_dropdown_option_center(image, 5), (110, 130))
        self.assertEqual(find_dropdown_option_center(image, "005"), (110, 130))

    def test_dropdown_never_fuzzes_digits_period_or_units(self):
        for text in ("15", "5분", "5시", "5.0", "O5", "０５", "5:", "1 2", "오후입니다", "오 후"):
            target = "오후" if "오" in text else (12 if text == "1 2" else 5)
            with self.subTest(text=text), self.assertRaises(ScheduleReadbackError):
                find_dropdown_option_center(words_image((text, 20, 20, 120, 20)), target)
        for target in (True, -1, 5.0, "5분", " 5", "오 후"):
            with self.subTest(target=target), self.assertRaises(ScheduleReadbackError):
                find_dropdown_option_center(words_image(("05", 20, 20, 30, 20)), target)

    def test_duplicate_dropdown_candidates_are_rejected_even_with_equal_numbers(self):
        image = words_image(("5", 20, 20, 10, 20), ("05", 20, 60, 20, 20))
        with self.assertRaises(ScheduleReadbackError):
            find_dropdown_option_center(image, 5)
        self.assertEqual(find_dropdown_option_center(image, 5, region=(0, 0, 100, 45)), (25, 30))

    def test_toast_tokens_group_by_geometry_without_fuzzy_text(self):
        image = words_image(("예약했습니다.", 160, 302, 150, 20), ("메시지를", 50, 300, 90, 24))
        self.assertTrue(has_schedule_success_toast(image))
        for text in ("메시지를 예약했습니다", "메시지를 예약했습니디.", "메시지를 예약하지 못했습니다.", "메시지가 예약되었습니다.", "메시지를 예약했습니다. 취소", "예약"):
            with self.subTest(text=text):
                self.assertFalse(has_schedule_success_toast(words_image((text, 30, 300, 450, 20))))

    def test_toast_duplicates_and_separate_rows_fail_closed(self):
        self.assertFalse(has_schedule_success_toast(words_image(
            ("메시지를 예약했습니다.", 30, 100, 300, 20), ("메시지를 예약했습니다.", 30, 300, 300, 20))))
        self.assertFalse(has_schedule_success_toast(words_image(("메시지를", 30, 100, 90, 20), ("예약했습니다.", 30, 150, 150, 20))))

    def test_clipped_or_overlapping_text_cannot_form_a_match(self):
        image = words_image(("05", 80, 20, 40, 20))
        with self.assertRaises(ScheduleReadbackError):
            find_dropdown_option_center(image, 5, region=(0, 0, 100, 100))
        overlap = words_image(("메시지를", 30, 100, 150, 20), ("예약했습니다.", 60, 100, 250, 20))
        self.assertFalse(has_schedule_success_toast(overlap))


if __name__ == "__main__":
    unittest.main()
