import tempfile
import unittest
from pathlib import Path

from kakao_sender import normalize_runtime_config
from kakao_sender_gui import KakaoSenderApp, load_relaxed_config_data


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


class FakeStatusVar:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class GuiLocalConfigWarningTests(unittest.TestCase):
    def test_malformed_gui_config_falls_back_to_defaults_and_returns_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text("{ not-json", encoding="utf-8")

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertEqual(config, normalize_runtime_config({}))
        self.assertIsNotNone(warning)
        self.assertIn("config/kakao_sender.local.json", warning or "")

    def test_malformed_nested_gui_config_falls_back_to_defaults_and_returns_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text('{"search_box": []}', encoding="utf-8")

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertEqual(config, normalize_runtime_config({}))
        self.assertIsNotNone(warning)
        self.assertIn("config/kakao_sender.local.json", warning or "")
        self.assertIsInstance(config["search_box"], dict)

    def test_top_level_non_object_gui_config_falls_back_to_defaults_and_returns_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text("[]", encoding="utf-8")

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertEqual(config, normalize_runtime_config({}))
        self.assertIsNotNone(warning)
        self.assertIn("config/kakao_sender.local.json", warning or "")

    def test_malformed_search_box_coordinate_falls_back_to_defaults_and_returns_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text('{"search_box": {"x": [], "y": 0}}', encoding="utf-8")

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertEqual(config, normalize_runtime_config({}))
        self.assertIsNotNone(warning)
        self.assertIn("config/kakao_sender.local.json", warning or "")
        self.assertIsInstance(config["search_box"]["x"], int)
        self.assertIsInstance(config["search_box"]["y"], int)

    def test_malformed_search_box_window_offset_coordinate_falls_back_to_defaults_and_returns_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text(
                '{"search_box": {"x": 12, "y": 34, "window_offset": {"x": [], "y": 0}}}',
                encoding="utf-8",
            )

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertEqual(config, normalize_runtime_config({}))
        self.assertIsNotNone(warning)
        self.assertIn("config/kakao_sender.local.json", warning or "")
        self.assertNotIn("window_offset", config["search_box"])

    def test_valid_gui_config_returns_normalized_config_without_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text(
                '{"search_box": {"x": 12, "y": 34}, "close_chat_key": "esc"}',
                encoding="utf-8",
            )

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertIsNone(warning)
        self.assertEqual(config["search_box"]["x"], 12)
        self.assertEqual(config["search_box"]["y"], 34)
        self.assertEqual(config["close_chat_key"], ["alt", "f4"])

    def test_valid_gui_config_with_window_offset_numeric_strings_returns_without_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"
            config_path.write_text(
                '{"search_box": {"x": 12, "y": 34, "window_offset": {"x": "56", "y": "78.0"}}}',
                encoding="utf-8",
            )

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertIsNone(warning)
        self.assertEqual(config["search_box"]["x"], 12)
        self.assertEqual(config["search_box"]["y"], 34)
        self.assertEqual(config["search_box"]["window_offset"]["x"], "56")
        self.assertEqual(config["search_box"]["window_offset"]["y"], "78.0")

    def test_missing_gui_config_uses_defaults_without_warning(self):
        # Given
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as temp_dir:
            config_path = Path(temp_dir) / "kakao_sender.local.json"

            # When
            config, warning = load_relaxed_config_data(config_path)

        # Then
        self.assertEqual(config, normalize_runtime_config({}))
        self.assertIsNone(warning)

    def test_config_warning_is_statused_and_logged_after_widgets_exist(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.config_warning = "config/kakao_sender.local.json 형식 오류"
        app.status_var = FakeStatusVar()
        logged: list[tuple[str, str]] = []

        def log(category: str, message: str) -> None:
            logged.append((category, message))

        app.log = log

        # When
        KakaoSenderApp.surface_config_warning(app)

        # Then
        self.assertEqual(app.status_var.value, "설정 파일 경고")
        self.assertEqual(logged, [("설정 경고", "config/kakao_sender.local.json 형식 오류")])


if __name__ == "__main__":
    unittest.main()
