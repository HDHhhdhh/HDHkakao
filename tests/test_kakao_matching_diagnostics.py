import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kakao_matching_diagnostics import record_matching_diagnostic


class MatchingDiagnosticTests(unittest.TestCase):
    def test_success_preserves_last_problem_and_excludes_message_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(record_matching_diagnostic("file_load", ("대상",), {}, {}, [], directory=root))
            problem = (root / "matching-last-problem.json").read_bytes()
            room = {"chat_id": "101", "title": "대상", "last_message": "private-message",
                    "oauth_token": "private-token"}
            self.assertTrue(record_matching_diagnostic("file_load", ("대상",), {"대상": "101"},
                                                       {"101": "대상"}, [room], directory=root))
            latest = (root / "matching-latest.json").read_text(encoding="utf-8")
            self.assertNotIn("private", latest)
            self.assertEqual(json.loads(latest)["missing"], [])
            self.assertEqual((root / "matching-last-problem.json").read_bytes(), problem)

    def test_disk_failure_is_reported_without_breaking_matching(self):
        with patch("kakao_matching_diagnostics.atomic_write_json", side_effect=OSError):
            self.assertFalse(record_matching_diagnostic("file_load", (), {}, {}, []))
