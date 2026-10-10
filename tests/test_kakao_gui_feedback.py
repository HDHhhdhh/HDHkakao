import queue
import unittest
from pathlib import Path
from unittest.mock import patch

from kakao_sender import MessageRow
from kakao_send_run import SendRunCompleted, SendRunResult, SendRunSupervisor
from kakao_sender_gui import (
    KakaoSenderApp,
    attachment_status_text,
    failed_result_indexes,
    macro_retry_result_indexes,
    format_attachment_empty_label,
    format_attachment_panel_help,
    format_failed_retry_instruction,
    format_failed_summary_count,
    format_failed_summary_status,
    format_retry_failed_button_label,
    format_search_empty_status,
    format_search_match_status,
    format_search_no_result_status,
    rows_with_common_attachments,
    shared_attachment_paths,
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


class FakeSearchFrame:
    def __init__(self, mapped: bool = True) -> None:
        self.mapped = mapped

    def winfo_ismapped(self) -> bool:
        return self.mapped

    def grid(self) -> None:
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

    def focus_set(self) -> None:
        self.focused = "tree"

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


class FakeStateButton:
    def __init__(self) -> None:
        self.states: list[list[str]] = []
        self.grid_removed = False
        self.text = ""

    def state(self, states: list[str]) -> None:
        self.states.append(states)

    def grid_remove(self) -> None:
        self.grid_removed = True

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self.text = str(kwargs["text"])


class FakeStopEvent:
    def __init__(self, stopped: bool) -> None:
        self.stopped = stopped

    def is_set(self) -> bool:
        return self.stopped


class GuiFeedbackTests(unittest.TestCase):
    def test_attachment_feedback_helpers_cover_empty_common_and_mixed_modes(self):
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
        self.assertEqual(format_attachment_empty_label(), "첨부 파일 없음")
        self.assertEqual(attachment_status_text([]), ("0개", "파일 0개 · 모든 대화명 공통"))
        self.assertEqual(format_attachment_panel_help(()), "모든 대화명 공통 · 여러 경로는 ; 또는 줄바꿈으로 구분합니다.")
        self.assertEqual(attachment_status_text(common_rows), ("1개", "파일 1개 · 모든 대화명 공통"))
        self.assertEqual(format_attachment_panel_help(shared_attachment_paths(common_rows)), "모든 대화명 공통: 파일 1개 · 새 첨부를 적용하면 기존 첨부를 교체합니다.")
        self.assertEqual(attachment_status_text(mixed_rows), ("행별 2개", "행별 첨부 섞임 · 파일 2개"))
        self.assertEqual(format_attachment_panel_help(shared_attachment_paths(mixed_rows)), "행별 첨부 섞임 · 새 첨부를 적용하면 모든 행의 첨부를 같은 파일로 교체합니다.")

    def test_attachment_panel_uses_feedback_helpers_without_tk(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.attachment_list = FakeListBox()
        app.attachment_help = FakeLabel()
        app.rows = []

        # When
        KakaoSenderApp.refresh_attachment_panel(app)

        # Then
        self.assertEqual(app.attachment_list.items, [format_attachment_empty_label()])
        self.assertIn("모든 대화명 공통", app.attachment_help.text)

    def test_recipient_selection_preserves_unapplied_common_attachment_path(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.attachment_list = FakeListBox()
        app.attachment_help = FakeLabel()
        attached = Path("C:/자료/기존 첨부.pdf")
        app.rows = [MessageRow(2, "대상 A", "본문 A", (attached,)),
                    MessageRow(3, "대상 B", "본문 B", (attached,))]
        draft = "C:/자료/새 첨부 1.pdf;C:/자료/새 사진.png"
        app.attachment_path_var = FakeStringVar(draft)
        app.tree = FakeTree()

        app.tree.selection_set("1")
        app.on_preview_selection_changed()
        app.tree.selection_set("0")
        app.on_preview_selection_changed()

        self.assertEqual(app.attachment_path_var.get(), draft)
        self.assertEqual(app.attachment_list.items, ["파일 · 기존 첨부.pdf"])
        self.assertEqual([row.attachments for row in app.rows], [(attached,), (attached,)])

        # A committed preview mutation or undo still synchronizes the entry.
        restored = Path("C:/자료/되돌린 첨부.pdf")
        app.rows = rows_with_common_attachments(app.rows, (restored,))
        app.refresh_attachment_panel()
        self.assertEqual(app.attachment_path_var.get(), str(restored))

        # Given
        app.rows = [
            MessageRow(2, "A", "one", (Path("C:/tmp/a.pdf"),)),
            MessageRow(3, "B", "two", (Path("C:/tmp/b.pdf"),)),
        ]

        # When
        KakaoSenderApp.refresh_attachment_panel(app)

        # Then
        self.assertEqual(app.attachment_help.text, format_attachment_panel_help(None))

    def test_failed_feedback_helpers_include_count_status_and_retry_instruction(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        logged: list[tuple[str, str]] = []
        failed_rows = [
            {
                "row": 2,
                "chat_name": "A",
                "status": "error",
                "detail": "blocked",
                "retry_transport": "macro",
            }
        ]
        app.log = lambda category, message: logged.append((category, message))

        # When
        KakaoSenderApp.log_failed_summary(app, failed_rows)

        # Then
        self.assertEqual(format_failed_summary_count(1), "전송 실패 1건")
        self.assertEqual(format_failed_summary_status("완료", 1), "완료: 실패 1건 · 세부 로그를 확인하세요")
        self.assertEqual(
            format_failed_retry_instruction(),
            "상단 [직접 실패 2차 전송] 버튼으로 직접 전송에 명시적으로 실패한 행만 PC 화면 방식으로 다시 보낼 수 있습니다.",
        )
        self.assertIn(("오류명단", format_failed_summary_count(1)), logged)
        self.assertIn(("오류명단", format_failed_retry_instruction()), logged)

    def test_retry_failed_rows_sends_only_explicit_direct_failures_via_macro(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [
            MessageRow(2, "즉시 실패", "one"),
            MessageRow(3, "성공", "two"),
            MessageRow(4, "예약 실패", "three"),
            MessageRow(5, "매칭 실패", "four"),
        ]
        app.results = [
            {
                "row_index": 0,
                "row": 2,
                "chat_name": "즉시 실패",
                "status": "error",
                "scheduled_at": "",
                "retry_transport": "macro",
            },
            {"row_index": 1, "row": 3, "chat_name": "성공", "status": "sent", "scheduled_at": ""},
            {"row_index": 2, "row": 4, "chat_name": "예약 실패", "status": "error", "scheduled_at": "2026-08-01 13:15"},
            {
                "row_index": 3,
                "row": 5,
                "chat_name": "매칭 실패",
                "status": "error",
                "failure_stage": "mapping",
                "retry_transport": "none",
            },
        ]
        started: list[tuple[list[int], str]] = []
        logged: list[tuple[str, str]] = []
        app.start_sending = lambda indexes, *, send_mode="normal": started.append((indexes, send_mode))
        app.log = lambda category, message: logged.append((category, message))

        # When
        with patch("kakao_sender_gui.messagebox.showinfo") as showinfo:
            KakaoSenderApp.retry_failed_rows(app)

        # Then
        self.assertEqual(started, [([0], "macro_retry")])
        self.assertIn(("2차 전송", "직접 전송 실패 1건을 PC 화면 방식으로 다시 발송합니다."), logged)
        showinfo.assert_not_called()

    def test_retry_failed_rows_without_errors_does_not_start(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "성공", "one")]
        app.results = [{"row_index": 0, "row": 2, "chat_name": "성공", "status": "sent"}]
        started: list[list[int]] = []
        app.start_sending = lambda indexes: started.append(indexes)

        # When
        with patch("kakao_sender_gui.messagebox.showinfo") as showinfo:
            KakaoSenderApp.retry_failed_rows(app)

        # Then
        self.assertEqual(started, [])
        showinfo.assert_called_once_with(
            "2차 전송 대상 없음",
            "PC 화면으로 다시 보낼 명시적인 직접 전송 실패 행이 없습니다.",
        )

    def test_retry_button_shows_failure_count_and_enabled_state(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "A", "one")]
        app.results = [
            {
                "row_index": 0,
                "row": 2,
                "chat_name": "A",
                "status": "error",
                "retry_transport": "macro",
            }
        ]
        app.worker = None
        app.retry_failed_button = FakeStateButton()

        # When
        KakaoSenderApp.refresh_retry_failed_action(app)

        # Then
        self.assertEqual(app.retry_failed_button.text, format_retry_failed_button_label(1))
        self.assertEqual(app.retry_failed_button.states[-1], ["!disabled"])

        # When
        app.worker = type("RunningWorker", (), {"is_alive": lambda self: True})()
        KakaoSenderApp.refresh_retry_failed_action(app)

        # Then
        self.assertEqual(app.retry_failed_button.states[-1], ["disabled"])

        # When
        app.worker = None
        app.results = []
        KakaoSenderApp.refresh_retry_failed_action(app)

        # Then
        self.assertEqual(app.retry_failed_button.text, format_retry_failed_button_label(0))
        self.assertEqual(app.retry_failed_button.states[-1], ["disabled"])

    def test_done_status_keeps_failure_summary_in_status_var(self):
        # Given
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.ui_queue = queue.Queue()
        app.status_var = FakeStatusVar()
        app.results = [
            {"row": 2, "chat_name": "A", "status": "error", "detail": "blocked"},
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
        app.log = lambda _category, _message: None
        app.active_run = SendRunSupervisor(lambda _event: None)
        app.ui_queue.put(("run_event", SendRunCompleted(
            app.active_run.run_id, tuple(SendRunResult(item) for item in app.results),
        )))

        # When
        with patch("kakao_gui_feedback.write_log", return_value=Path("synthetic.csv")):
            KakaoSenderApp.drain_queue(app)

        # Then
        self.assertEqual(app.status_var.value, format_failed_summary_status("완료", 1))
        self.assertIsNone(app.run_row_indexes)

    def test_search_feedback_helpers_remain_status_var_compatible(self):
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
        app.log = lambda _category, _message: None
        app.search_match_indexes = []
        app.search_match_position = -1
        app.search_var = FakeStringVar("")

        # When
        KakaoSenderApp.find_preview_match(app)

        # Then
        self.assertEqual(app.status_var.value, format_search_empty_status())

        # Given
        app.search_var.set("missing")

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

    def test_common_attachment_and_failed_retry_mapping_stay_index_based(self):
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

    def test_macro_retry_indexes_keep_duplicate_recipients_distinct_and_exclude_unknown(self):
        rows = [
            MessageRow(2, "같은 방", "one"),
            MessageRow(3, "같은 방", "two"),
            MessageRow(4, "다른 방", "three"),
        ]
        results = [
            {"row_index": 0, "status": "error", "retry_transport": "macro"},
            {"row_index": 1, "status": "error", "retry_transport": "macro"},
            {"row_index": 2, "status": "unknown", "retry_transport": "macro"},
            {"row_index": 2, "status": "error", "retry_transport": "none"},
        ]

        self.assertEqual(macro_retry_result_indexes(results, rows), [0, 1])


if __name__ == "__main__":
    unittest.main()
