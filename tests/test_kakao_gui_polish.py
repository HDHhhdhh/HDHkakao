import queue
import unittest
from pathlib import Path
from unittest.mock import patch

from kakao_sender import MessageRow
from kakao_sender_gui import (
    KakaoSenderApp,
    attachment_status_text,
    failed_result_indexes,
    format_search_match_status,
    format_search_no_result_status,
    rows_with_common_attachments,
    wire_big_edit_dialog_keyboard,
)


class FakeStatusVar:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class FakeStringVar:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeStateButton:
    def __init__(self) -> None:
        self.states: list[list[str]] = []
        self.grid_removed = False

    def state(self, states: list[str]) -> None:
        self.states.append(states)

    def grid_remove(self) -> None:
        self.grid_removed = True


class FakeStopEvent:
    def __init__(self, stopped: bool) -> None:
        self.stopped = stopped

    def is_set(self) -> bool:
        return self.stopped


class FakeListBox:
    def __init__(self) -> None:
        self.items: list[str] = []

    def delete(self, start: int, end: str) -> None:
        self.items = []

    def insert(self, index: str, value: str) -> None:
        self.items.append(value)


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, text: str) -> None:
        self.text = text


class FakeSearchFrame:
    def __init__(self, mapped: bool = True) -> None:
        self.mapped = mapped
        self.grid_called = False

    def winfo_ismapped(self) -> bool:
        return self.mapped

    def grid(self) -> None:
        self.grid_called = True
        self.mapped = True


class FakeSearchEntry:
    def __init__(self) -> None:
        self.focused = False
        self.selection: tuple[int, str] | None = None

    def focus_set(self) -> None:
        self.focused = True

    def selection_range(self, first: int, last: str) -> None:
        self.selection = (first, last)


class FakeTree:
    def __init__(self) -> None:
        self.selected: list[str] = []
        self.focused = ""
        self.seen = ""

    def exists(self, item_id: str) -> bool:
        return True

    def selection(self) -> list[str]:
        return self.selected

    def selection_set(self, item_id: str) -> None:
        self.selected = [item_id]

    def focus(self, item_id: str) -> None:
        self.focused = item_id

    def see(self, item_id: str) -> None:
        self.seen = item_id

    def index(self, item_id: str) -> int:
        return int(item_id)


class FakeDialog:
    def __init__(self) -> None:
        self.bindings = {}

    def bind(self, sequence: str, handler) -> None:
        self.bindings[sequence] = handler


class FakeEditableField:
    def __init__(self) -> None:
        self.focused = False
        self.selection: tuple[int, str] | None = None

    def focus_set(self) -> None:
        self.focused = True

    def selection_range(self, first: int, last: str) -> None:
        self.selection = (first, last)


class GuiPolishTests(unittest.TestCase):
    def test_failure_summary_status_remains_after_done_when_errors_exist(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.ui_queue = queue.Queue()
        app.status_var = FakeStatusVar()
        app.results = [
            {
                "row": 2,
                "chat_name": "A",
                "status": "error",
                "detail": "blocked",
                "retry_transport": "macro",
            },
            {"row": 3, "chat_name": "B", "status": "sent", "detail": ""},
        ]
        app.stop_event = FakeStopEvent(False)
        app.start_button = FakeStateButton()
        app.test_one_check = FakeStateButton()
        app.stop_button = FakeStateButton()
        app.run_row_indexes = [0, 1]
        app._close_requested = False
        app.update_summary = lambda: None
        app.after = lambda _delay, _callback: None
        logged: list[tuple[str, str]] = []
        app.log = lambda category, message: logged.append((category, message))
        from kakao_send_run import SendRunCompleted, SendRunResult, SendRunSupervisor
        app.active_run = SendRunSupervisor(lambda _event: None)
        app.ui_queue.put(("run_event", SendRunCompleted(
            app.active_run.run_id, tuple(SendRunResult(item) for item in app.results),
        )))

        # When
        with patch("kakao_gui_feedback.write_log", return_value=Path("synthetic.csv")):
            KakaoSenderApp.drain_queue(app)

        # Then
        self.assertIn("실패 1건", app.status_var.value)
        self.assertIn("세부 로그", app.status_var.value)
        self.assertIn(("오류명단", "전송 실패 1건"), logged)
        self.assertIn(
            (
                "오류명단",
                "상단 [직접 실패 2차 전송] 버튼으로 직접 전송에 명시적으로 실패한 행만 PC 화면 방식으로 다시 보낼 수 있습니다.",
            ),
            logged,
        )
        self.assertIsNone(app.run_row_indexes)

    def test_attachment_status_text_names_common_and_mixed_modes(self):
        # Given
        common_paths = (Path("C:/tmp/common.pdf"),)
        common_rows = [
            MessageRow(2, "A", "one", common_paths),
            MessageRow(3, "B", "two", common_paths),
        ]
        mixed_rows = [
            MessageRow(2, "A", "one", (Path("C:/tmp/a.pdf"),)),
            MessageRow(3, "B", "two", (Path("C:/tmp/b.pdf"),)),
        ]

        # When / Then
        self.assertEqual(attachment_status_text(common_rows), ("1개", "파일 1개 · 모든 대화명 공통"))
        self.assertEqual(attachment_status_text(mixed_rows), ("행별 2개", "행별 첨부 섞임 · 파일 2개"))

    def test_attachment_panel_help_names_common_and_mixed_modes(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.attachment_list = FakeListBox()
        app.attachment_help = FakeLabel()
        common_paths = (Path("C:/tmp/common.pdf"),)
        app.rows = [
            MessageRow(2, "A", "one", common_paths),
            MessageRow(3, "B", "two", common_paths),
        ]

        # When
        KakaoSenderApp.refresh_attachment_panel(app)

        # Then
        self.assertIn("모든 대화명 공통", app.attachment_help.text)

        # Given
        app.rows = [
            MessageRow(2, "A", "one", (Path("C:/tmp/a.pdf"),)),
            MessageRow(3, "B", "two", (Path("C:/tmp/b.pdf"),)),
        ]

        # When
        KakaoSenderApp.refresh_attachment_panel(app)

        # Then
        self.assertIn("행별 첨부 섞임", app.attachment_help.text)

    def test_search_feedback_remains_in_status_var_for_no_result_and_match_position(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.search_frame = FakeSearchFrame()
        app.search_entry = FakeSearchEntry()
        app.status_var = FakeStatusVar()
        app.tree = FakeTree()
        app.rows = [
            MessageRow(2, "Alpha", "first"),
            MessageRow(3, "Beta", "second"),
        ]
        logs: list[tuple[str, str]] = []
        app.log = lambda category, message: logs.append((category, message))
        app.search_match_indexes = []
        app.search_match_position = -1
        app.search_var = FakeStringVar("missing")

        # When
        KakaoSenderApp.find_preview_match(app)

        # Then
        self.assertEqual(app.status_var.value, format_search_no_result_status("missing"))

        # Given
        app.search_var.set("Alpha")

        # When
        KakaoSenderApp.find_preview_match(app)

        # Then
        self.assertEqual(app.status_var.value, format_search_match_status(1, 1, "Alpha"))

    def test_big_edit_dialog_focuses_first_editable_field_and_saves_with_control_return(self):
        # Given
        dialog = FakeDialog()
        first_field = FakeEditableField()
        saves: list[str] = []

        # When
        wire_big_edit_dialog_keyboard(dialog, first_field, lambda: saves.append("saved"))
        result = dialog.bindings["<Control-Return>"](None)

        # Then
        self.assertTrue(first_field.focused)
        self.assertEqual(first_field.selection, (0, "end"))
        self.assertEqual(saves, ["saved"])
        self.assertEqual(result, "break")

    def test_send_order_and_retry_mapping_stay_index_based(self):
        # Given
        rows = [
            MessageRow(2, "A", "one", (Path("C:/tmp/old-a.pdf"),)),
            MessageRow(3, "B", "two", (Path("C:/tmp/old-b.pdf"),)),
        ]
        attachments = (Path("C:/tmp/common.pdf"),)
        results = [{"row_index": 1, "row": 3, "chat_name": "B", "status": "error"}]

        # When
        updated = rows_with_common_attachments(rows, attachments)
        retry_indexes = failed_result_indexes(results, updated)

        # Then
        self.assertEqual([row.chat_name for row in updated], ["A", "B"])
        self.assertEqual([row.attachments for row in updated], [attachments, attachments])
        self.assertEqual(retry_indexes, [1])


if __name__ == "__main__":
    unittest.main()
