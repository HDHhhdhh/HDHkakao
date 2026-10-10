from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kakao_windows_ocr import WindowsOcrError, _parse_result, recognize_image


class WindowsOcrTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "version": 1, "width": 434, "height": 684, "language": "ko",
            "lines": [{"text": "예약\x00 메시지\r\n", "rect": {"x": 8, "y": 20, "width": 100, "height": 15},
                       "words": [{"text": "예약", "rect": {"x": 8, "y": 20, "width": 30, "height": 15}}]}],
        }

    def test_utf8_bom_korean_and_original_coordinates(self):
        result = _parse_result(b"\xef\xbb\xbf" + json.dumps(self.payload, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(result.text, "예약 메시지")
        self.assertEqual(result.lines[0].words[0].text, "예약")
        self.assertEqual(result.lines[0].words[0].rect.center, (23, 27.5))
        self.assertEqual((result.width, result.height), (434, 684))

    def test_empty_recognition_is_not_an_error(self):
        self.payload["lines"] = []
        self.assertEqual(_parse_result(json.dumps(self.payload)).text, "")

    def test_malformed_and_out_of_bounds_results_are_rejected(self):
        for change in (
            {"version": 2}, {"version": True}, {"width": True}, {"height": 0}, {"lines": None}, {"language": ""}
        ):
            with self.subTest(change=change), self.assertRaises(WindowsOcrError):
                _parse_result(json.dumps(self.payload | change))
        for rect in (
            {"x": 434, "y": 2, "width": 10, "height": 3},
            {"x": -1, "y": 2, "width": 10, "height": 3},
            {"x": float("nan"), "y": 2, "width": 10, "height": 3},
        ):
            value = copy.deepcopy(self.payload)
            value["lines"][0]["words"][0]["rect"] = rect
            with self.subTest(rect=rect), self.assertRaises(WindowsOcrError):
                _parse_result(json.dumps(value))
        with self.assertRaisesRegex(WindowsOcrError, "UTF-8 JSON"):
            _parse_result(b"noise\n{}")

    def test_command_passes_path_as_argument_and_hides_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "한글 ' $() 이미지.png"
            path.write_bytes(b"test")
            completed = subprocess.CompletedProcess([], 0, json.dumps(self.payload).encode(), b"")
            with patch("kakao_windows_ocr.subprocess.run", return_value=completed) as run:
                recognize_image(path, scale=3)
            command = run.call_args.args[0]
            self.assertIn(str(path.resolve()), command)
            self.assertEqual(command[command.index("-Scale") + 1], "3.0")
            self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_timeout_and_helper_error_are_clear(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            with patch("kakao_windows_ocr.subprocess.run", side_effect=subprocess.TimeoutExpired("powershell", 1)):
                with self.assertRaisesRegex(WindowsOcrError, "timed out"):
                    recognize_image(image.name, timeout=1)
            completed = subprocess.CompletedProcess([], 1, b"", "언어 없음".encode())
            with patch("kakao_windows_ocr.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(WindowsOcrError, "언어 없음"):
                    recognize_image(image.name)

    def test_invalid_arguments_do_not_start_a_process(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            for kwargs in ({"language": "ko; whoami"}, {"scale": 0}, {"scale": float("inf")}, {"timeout": -1}):
                with self.subTest(kwargs=kwargs), patch("kakao_windows_ocr.subprocess.run") as run:
                    with self.assertRaises(ValueError):
                        recognize_image(image.name, **kwargs)
                    run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
