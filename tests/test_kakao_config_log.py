import csv
import tempfile
import unittest
from pathlib import Path

from kakao_sender import MessageRow, normalize_runtime_config, write_log
from kakao_sender_gui import failed_result_indexes


LOG_FIELDNAMES = ["row_index", "row", "chat_name", "status", "detail", "attachments", "scheduled_at", "run_id", "transport", "failure_stage", "retry_transport", "chat_id", "log_id"]


class ConfigNormalizationTests(unittest.TestCase):
    def test_config_normalization_converts_legacy_esc_keys_when_loaded(self):
        # Given
        legacy_config = {
            "close_chat_key": "esc",
            "clear_search_key": "esc",
            "unknown_future_key": {"kept": True},
        }

        # When
        normalized = normalize_runtime_config(legacy_config)

        # Then
        self.assertEqual(normalized["close_chat_key"], ["alt", "f4"])
        self.assertEqual(normalized["clear_search_key"], "")
        self.assertEqual(normalized["unknown_future_key"], {"kept": True})


class LogSchemaTests(unittest.TestCase):
    def test_log_schema_writes_exact_header_order_and_ignores_extra_result_keys(self):
        # Given
        results = [
            {
                "row_index": 2,
                "row": 7,
                "chat_name": "테스트방",
                "status": "error",
                "detail": "실패",
                "attachments": 1,
                "scheduled_at": "2026-05-27 09:30",
                "unexpected": "ignored",
            },
            {
                "row": 8,
                "chat_name": "누락방",
                "status": "sent",
                "detail": "",
                "attachments": 0,
                "scheduled_at": "",
                "unexpected": "ignored",
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            # When
            log_path = write_log(Path(temp_dir), results)

            # Then
            with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(reader.fieldnames, LOG_FIELDNAMES)
        self.assertEqual(rows[0]["row_index"], "2")
        self.assertEqual(rows[0]["scheduled_at"], "2026-05-27 09:30")
        self.assertEqual(rows[1]["row_index"], "")
        self.assertNotIn("unexpected", rows[0])

    def test_log_schema_utf_8_sig_reader_removes_bom_from_header(self):
        # Given
        results = [
            {
                "row_index": 0,
                "row": 2,
                "chat_name": "테스트방",
                "status": "sent",
                "detail": "",
                "attachments": 0,
                "scheduled_at": "",
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            # When
            log_path = write_log(Path(temp_dir), results)

            # Then
            with log_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)

        self.assertEqual(reader.fieldnames[0], "row_index")
        self.assertNotEqual(reader.fieldnames[0], "\ufeffrow_index")
        self.assertEqual(rows[0]["row_index"], "0")

    def test_log_schema_failed_result_indexes_prefers_row_index_for_retry_matching(self):
        # Given
        rows = [
            MessageRow(2, "A", "one"),
            MessageRow(3, "B", "two"),
            MessageRow(4, "C", "three"),
        ]
        results = [
            {"row_index": 2, "row": 2, "chat_name": "A", "status": "error"},
            {"row": 3, "chat_name": "B", "status": "error"},
        ]

        # When
        indexes = failed_result_indexes(results, rows)

        # Then
        self.assertEqual(indexes, [2, 1])


if __name__ == "__main__":
    unittest.main()
