from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kakao_sender_gui import (
    KakaoSenderApp,
    schedule_picker_datetime,
    schedule_picker_hour_values,
    schedule_picker_initial_datetime,
    schedule_picker_minute_values,
    schedule_picker_period_values,
    schedule_picker_time_slots,
)


class SchedulePickerTests(unittest.TestCase):
    def test_initial_datetime_rounds_up_after_five_minutes_of_lead_time(self) -> None:
        now = datetime(2026, 7, 11, 20, 2, 30)

        result = schedule_picker_initial_datetime("", now)

        self.assertEqual(result, datetime(2026, 7, 11, 20, 10))

    def test_initial_datetime_preserves_a_valid_future_reservation(self) -> None:
        now = datetime(2026, 7, 11, 20, 2)

        result = schedule_picker_initial_datetime("2026-07-12 09:30", now)

        self.assertEqual(result, datetime(2026, 7, 12, 9, 30))

    def test_existing_time_with_exactly_five_minutes_remaining_is_preserved(self) -> None:
        now = datetime(2026, 7, 11, 10, 0)

        self.assertEqual(schedule_picker_initial_datetime("2026-07-11 10:05", now), datetime(2026, 7, 11, 10, 5))

    def test_existing_time_with_four_minutes_fifty_nine_seconds_is_replaced(self) -> None:
        now = datetime(2026, 7, 11, 10, 0, 1)

        self.assertEqual(schedule_picker_initial_datetime("2026-07-11 10:05", now), datetime(2026, 7, 11, 10, 10))

    def test_initial_value_and_first_slot_agree_across_seconds_and_midnight(self) -> None:
        cases = (
            (datetime(2026, 7, 11, 10, 0), datetime(2026, 7, 11, 10, 5)),
            (datetime(2026, 7, 11, 10, 0, 0, 1), datetime(2026, 7, 11, 10, 10)),
            (datetime(2026, 7, 11, 10, 4, 59), datetime(2026, 7, 11, 10, 10)),
            (datetime(2026, 7, 11, 10, 5), datetime(2026, 7, 11, 10, 10)),
            (datetime(2026, 7, 11, 23, 55), datetime(2026, 7, 12, 0, 0)),
            (datetime(2026, 7, 11, 23, 55, 1), datetime(2026, 7, 12, 0, 5)),
            (datetime(2026, 7, 11, 23, 59, 59), datetime(2026, 7, 12, 0, 5)),
            (datetime(2026, 7, 12, 0, 0), datetime(2026, 7, 12, 0, 5)),
            (datetime(2026, 7, 12, 0, 0, 1), datetime(2026, 7, 12, 0, 10)),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                initial = schedule_picker_initial_datetime("", now)
                slots = schedule_picker_time_slots(initial.date(), now)
                self.assertEqual(initial, expected)
                self.assertEqual(slots[0], expected)
                self.assertTrue(all(slot >= now + timedelta(minutes=5) for slot in slots))
                self.assertLess(initial - timedelta(minutes=5), now + timedelta(minutes=5))

    def test_picker_datetime_converts_twelve_hour_values(self) -> None:
        selected_date = date(2026, 7, 12)

        midnight = schedule_picker_datetime(selected_date, False, 12, 5)
        noon = schedule_picker_datetime(selected_date, True, 12, 5)

        self.assertEqual(midnight, datetime(2026, 7, 12, 0, 5))
        self.assertEqual(noon, datetime(2026, 7, 12, 12, 5))

    def test_today_slots_keep_five_minutes_of_lead_time_and_filter_cascading_values(self) -> None:
        now = datetime(2026, 7, 11, 10, 2, 30)

        slots = schedule_picker_time_slots(now.date(), now)

        self.assertEqual(slots[0], datetime(2026, 7, 11, 10, 10))
        self.assertTrue(all(slot >= now + timedelta(minutes=5) for slot in slots))
        self.assertEqual(schedule_picker_period_values(slots), ("오전", "오후"))
        self.assertEqual(schedule_picker_hour_values(slots, "오전"), ("10", "11"))
        self.assertEqual(
            schedule_picker_minute_values(slots, "오전", "10"),
            ("10", "15", "20", "25", "30", "35", "40", "45", "50", "55"),
        )

    def test_slot_exactly_five_minutes_away_is_available(self) -> None:
        now = datetime(2026, 7, 11, 10, 5)

        slots = schedule_picker_time_slots(now.date(), now)

        self.assertEqual(slots[0], datetime(2026, 7, 11, 10, 10))

    def test_today_is_unavailable_when_last_slot_has_less_than_five_minutes_left(self) -> None:
        now = datetime(2026, 7, 11, 23, 50, 1)

        slots = schedule_picker_time_slots(now.date(), now)

        self.assertEqual(slots, ())

    def test_future_date_exposes_the_full_day_in_chronological_order(self) -> None:
        now = datetime(2026, 7, 11, 23, 50)
        selected_date = date(2026, 7, 12)

        slots = schedule_picker_time_slots(selected_date, now)

        self.assertEqual(len(slots), 288)
        self.assertEqual(slots[0], datetime(2026, 7, 12, 0, 0))
        self.assertEqual(slots[-1], datetime(2026, 7, 12, 23, 55))
        self.assertEqual(schedule_picker_period_values(slots), ("오전", "오후"))
        self.assertEqual(schedule_picker_hour_values(slots, "오전"), ("12", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"))
        self.assertEqual(schedule_picker_minute_values(slots, "오후", "12"), tuple(f"{minute:02d}" for minute in range(0, 60, 5)))

    def test_tomorrow_midnight_is_excluded_when_it_has_less_than_five_minutes_left(self) -> None:
        now = datetime(2026, 7, 11, 23, 59)

        slots = schedule_picker_time_slots(date(2026, 7, 12), now)

        self.assertEqual(len(slots), 287)
        self.assertEqual(slots[0], datetime(2026, 7, 12, 0, 5))
        self.assertEqual(schedule_picker_minute_values(slots, "오전", "12")[0], "05")

    def test_noon_and_midnight_boundaries_keep_only_future_slots(self) -> None:
        before_noon = datetime(2026, 7, 11, 11, 58)
        midnight = datetime(2026, 7, 11, 0, 0)

        noon_slots = schedule_picker_time_slots(before_noon.date(), before_noon)
        midnight_slots = schedule_picker_time_slots(midnight.date(), midnight)

        self.assertEqual(noon_slots[0], datetime(2026, 7, 11, 12, 5))
        self.assertEqual(schedule_picker_period_values(noon_slots), ("오후",))
        self.assertEqual(schedule_picker_hour_values(noon_slots, "오후")[0], "12")
        self.assertEqual(midnight_slots[0], datetime(2026, 7, 11, 0, 5))


class SchedulePickerDialogTests(unittest.TestCase):
    """Exercise picker callbacks with fake widgets, without creating a Tk window."""

    def run_picker(self, initial: str, current: list[datetime], interact):
        class Var:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return current[0]

        target = Var(initial)
        applied = Mock()
        dialog = Mock()
        buttons = {}

        def button(*args, **kwargs):
            buttons[kwargs["text"]] = kwargs["command"]
            return Mock()

        with (
            patch("kakao_sender_gui.datetime", Clock),
            patch("kakao_sender_gui.tk.Toplevel", return_value=dialog),
            patch("kakao_sender_gui.tk.StringVar", Var),
            patch("kakao_sender_gui.ttk.Label"),
            patch("kakao_sender_gui.ttk.Frame"),
            patch("kakao_sender_gui.ttk.Combobox"),
            patch("kakao_sender_gui.ttk.Button", side_effect=button),
            patch("kakao_sender_gui.messagebox.showwarning") as warning,
        ):
            dialog.wait_window.side_effect = lambda: interact(buttons, target, applied, warning, dialog)
            KakaoSenderApp.open_schedule_picker(SimpleNamespace(), target, applied)
        return target, applied, warning, dialog

    def test_quick_selection_and_apply_share_the_earliest_safe_slot_across_midnight(self):
        current = [datetime(2026, 7, 11, 23, 55, 1)]

        def interact(buttons, *_args):
            buttons["가장 빠른 시각"]()
            buttons["적용"]()

        target, applied, warning, dialog = self.run_picker("2026-07-15 12:00", current, interact)

        self.assertEqual(target.get(), "2026-07-12 00:05")
        applied.assert_called_once_with()
        warning.assert_not_called()
        dialog.destroy.assert_called_once_with()

    def test_apply_rejects_elapsed_lead_time_then_refreshes_to_a_safe_slot(self):
        current = [datetime(2026, 7, 11, 10, 0)]

        def interact(buttons, target, applied, warning, dialog):
            current[0] = datetime(2026, 7, 11, 10, 0, 1)
            buttons["적용"]()
            self.assertEqual(target.get(), "2026-07-11 10:05")
            applied.assert_not_called()
            dialog.destroy.assert_not_called()
            warning.assert_called_once()
            self.assertIn("최소 5분", warning.call_args.args[1])
            buttons["적용"]()

        target, applied, warning, dialog = self.run_picker("2026-07-11 10:05", current, interact)

        self.assertEqual(target.get(), "2026-07-11 10:10")
        applied.assert_called_once_with()
        warning.assert_called_once()
        dialog.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
