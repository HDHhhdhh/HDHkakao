"""Exports use fixed snapshots and never touch Tk from a file writer."""
from datetime import datetime
from pathlib import Path
import queue
import tempfile
import threading
import time
from unittest.mock import Mock, patch

import pytest

from kakao_document import rows_to_csv_text
from kakao_gui_exports import ExportResult, HubExportsMixin
from kakao_gui_feedback import HubRunFeedbackMixin
from kakao_sender import MessageRow


class LogText:
    def __init__(self, owner):
        self.owner = owner
        self.text = "[00:00:00] [시험] 원래 로그\n\n"
        self.reads = []

    def get(self, first, last):
        assert threading.get_ident() == self.owner, "Tk log read from worker"
        self.reads.append((first, last))
        return self.text


class ExportHost(HubExportsMixin, HubRunFeedbackMixin):
    def __init__(self):
        self.owner = threading.get_ident()
        self._close_requested = False
        self.file_path = Path("source.xlsx")
        self.rows = [MessageRow(2, "대상", "원래 본문")]
        self.log_text = LogText(self.owner)
        self.logs = []
        self.events = queue.Queue()

    def log(self, category, message):
        assert threading.get_ident() == self.owner, "Tk log written from worker"
        self.logs.append((category, message))

    def post(self, event, payload):
        assert threading.get_ident() != self.owner, "File worker did not post its result"
        self.events.put((event, payload))

    def take_result(self):
        event, result = self.events.get(timeout=3)
        assert event == "export_result"
        return result


def choose_path(app, path):
    def choose(**_kwargs):
        assert threading.get_ident() == app.owner, "Save chooser opened from worker"
        return str(path)
    return choose


def test_slow_csv_write_returns_early_and_uses_immutable_owner_snapshot():
    app = ExportHost()
    app.rows = [MessageRow(7, '대상,"이름"', "원래 본문\n두 번째 줄", (Path("C:/자료/a.pdf"),), False, datetime(2027, 1, 2, 3, 4))]
    original = tuple(app.rows)
    expected = "\ufeff" + rows_to_csv_text(original)
    entered, release = threading.Event(), threading.Event()
    writes, converters = [], []

    def convert(rows):
        assert threading.get_ident() != app.owner, "CSV converted on Tk"
        assert isinstance(rows, tuple)
        converters.append(rows)
        return rows_to_csv_text(rows)

    def write(path, text, *, encoding):
        assert threading.get_ident() != app.owner, "Disk write ran on Tk"
        entered.set()
        assert release.wait(3)
        writes.append((path, text, encoding))

    try:
        with patch("kakao_gui_exports.filedialog.asksaveasfilename", side_effect=choose_path(app, "preview.csv")) as chooser, \
             patch("kakao_gui_exports.rows_to_csv_text", side_effect=convert), \
             patch.object(Path, "write_text", write):
            started = time.perf_counter()
            assert app.request_preview_csv_export()
            assert time.perf_counter() - started < 0.5
            assert entered.wait(3)
            assert app.events.empty()
            assert not any("저장 완료" in message for _category, message in app.logs)
            app.rows[0] = MessageRow(7, "다른 대상", "바뀐 본문")
            app.rows.append(MessageRow(8, "추가", "추가 본문"))
            app.log("입력", "저장 중에도 GUI 콜백 실행")
            release.set()
            result = app.take_result()
        assert converters == [original]
        assert writes == [(Path("preview.csv"), expected, "utf-8")]
        chooser.assert_called_once_with(title="편집본 CSV 저장", defaultextension=".csv", initialfile="source_edited.csv", filetypes=(("CSV UTF-8", "*.csv"), ("모든 파일", "*.*")))
        assert app._exports_pending
        app.handle_export_result(result)
        assert app._exports_pending == {}
        assert app.logs[-1] == ("저장", "편집본 CSV 저장 완료: preview.csv")
    finally:
        release.set()


def test_log_snapshot_flushes_received_text_on_owner_and_writes_in_worker():
    app = ExportHost()
    writes, directory_calls = [], []
    def flush():
        assert threading.get_ident() == app.owner
        app.log_text.text += "받은 로그\n"
    app.flush_pending_visual_feedback = flush

    def mkdir(path, *, parents, exist_ok):
        assert threading.get_ident() != app.owner, "Log directory created on Tk"
        directory_calls.append((path, parents, exist_ok))

    def write(path, text, *, encoding):
        assert threading.get_ident() != app.owner
        writes.append((path, text, encoding))

    with patch("kakao_gui_exports.LOG_DIR", Path("log-directory")), \
         patch("kakao_gui_exports.filedialog.asksaveasfilename", side_effect=choose_path(app, "visible.txt")) as chooser, \
         patch.object(Path, "mkdir", mkdir), patch.object(Path, "write_text", write):
        assert app.request_visible_log_export()
        result = app.take_result()
    assert app.log_text.reads == [("1.0", "end")]
    assert writes == [(Path("visible.txt"), "[00:00:00] [시험] 원래 로그\n\n받은 로그\n", "utf-8")]
    assert directory_calls == [(Path("log-directory"), True, True)]
    chooser.assert_called_once_with(title="로그 저장", defaultextension=".txt", initialdir="log-directory", filetypes=(("텍스트 로그", "*.txt"), ("모든 파일", "*.*")))
    app.log_text.text = "저장 후 변경"
    app.handle_export_result(result)
    assert app.logs[-1] == ("로그", "화면 로그 저장: visible.txt")


def test_all_writes_are_fifo_and_same_path_exports_never_overlap():
    app = ExportHost()
    entered, release = threading.Event(), threading.Event()
    writes = []
    active = maximum_active = 0

    def write(path, text, *, encoding):
        nonlocal active, maximum_active
        assert threading.get_ident() != app.owner
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            if not writes:
                entered.set()
                assert release.wait(3)
            writes.append((path, text, encoding))
        finally:
            active -= 1

    try:
        with patch("kakao_gui_exports.filedialog.asksaveasfilename", return_value="same.csv"), patch.object(Path, "write_text", write):
            assert app.request_preview_csv_export()
            assert entered.wait(3)
            app.rows = [MessageRow(2, "대상", "두 번째 저장")]
            assert app.request_preview_csv_export()
            assert len(app._exports_pending) == 2
            assert writes == []
            release.set()
            first, second = app.take_result(), app.take_result()
        assert maximum_active == 1
        assert [result.token for result in (first, second)] == [1, 2]
        assert [path for path, _text, _encoding in writes] == [Path("same.csv"), Path("same.csv")]
        assert "원래 본문" in writes[0][1]
        assert "두 번째 저장" in writes[1][1]
        # No Tk completion callback was needed to start the second disk write.
        app.handle_export_result(first)
        app.handle_export_result(second)
        assert app._exports_pending == {}
    finally:
        release.set()


def test_close_does_not_cancel_accepted_writes_or_render_late_completion():
    app = ExportHost()
    entered, release = threading.Event(), threading.Event()
    writes = []

    def write(path, text, *, encoding):
        if not writes:
            entered.set()
            assert release.wait(3)
        writes.append((path, text))

    try:
        with patch("kakao_gui_exports.filedialog.asksaveasfilename", side_effect=["first.csv", "second.csv"]), patch.object(Path, "write_text", write):
            app.request_preview_csv_export()
            assert entered.wait(3)
            app.request_preview_csv_export()
            app._close_requested = True
            before = list(app.logs)
            release.set()
            first, second = app.take_result(), app.take_result()
        with patch("kakao_gui_exports.messagebox.showerror") as error:
            app.handle_export_result(first)
            app.handle_export_result(second)
            app.handle_export_result(ExportResult(999, "csv", Path("stale.csv"), "stale error"))
        assert len(writes) == 2
        assert app._exports_pending == {}
        assert app.logs == before
        error.assert_not_called()
    finally:
        release.set()


@pytest.mark.parametrize("kind", ["csv", "log"])
def test_failed_write_reports_exact_error_and_never_reports_success(kind):
    app = ExportHost()
    request = app.request_preview_csv_export if kind == "csv" else app.request_visible_log_export
    with patch("kakao_gui_exports.filedialog.asksaveasfilename", return_value="failed.txt"), \
         patch.object(Path, "mkdir"), patch.object(Path, "write_text", side_effect=OSError("fixture disk full")):
        assert request()
        result = app.take_result()
    with patch("kakao_gui_exports.messagebox.showerror") as error:
        app.handle_export_result(result)
    title = "CSV 저장 실패" if kind == "csv" else "로그 저장 실패"
    error.assert_called_once_with(title, "failed.txt\n\nOSError: fixture disk full")
    assert app.logs[-1] == ("오류", f"{title}: failed.txt · OSError: fixture disk full")
    assert not any("저장 완료" in message or message.startswith("화면 로그 저장:") for _category, message in app.logs)
    assert app._exports_pending == {}


def test_duplicate_unknown_or_mismatched_completion_cannot_change_ui():
    app = ExportHost()
    with patch("kakao_gui_exports.filedialog.asksaveasfilename", return_value="current.csv"), patch.object(Path, "write_text"):
        app.request_preview_csv_export()
        result = app.take_result()
    before = list(app.logs)
    with patch("kakao_gui_exports.messagebox.showerror") as error:
        app.handle_export_result(ExportResult(999, "csv", Path("current.csv"), "stale"))
        app.handle_export_result(ExportResult(result.token, "csv", Path("different.csv"), "wrong path"))
        app.handle_export_result(ExportResult(result.token, "log", result.path, "wrong kind"))
        assert app.logs == before
        assert result.token in app._exports_pending
        app.handle_export_result(result)
        once = list(app.logs)
        app.handle_export_result(result)
    assert app.logs == once
    error.assert_not_called()


def test_feedback_dispatch_and_active_poll_follow_pending_export_commit():
    app = ExportHost()
    assert app._feedback_work_active() is False
    with patch("kakao_gui_exports.filedialog.asksaveasfilename", return_value="queued.csv"), patch.object(Path, "write_text"):
        assert app.request_preview_csv_export()
        result = app.take_result()
    assert app._feedback_work_active() is True
    app.handle_ui_event("export_result", result)
    assert app._feedback_work_active() is False
    assert app.logs[-1] == ("저장", "편집본 CSV 저장 완료: queued.csv")


def test_cancel_empty_or_closed_requests_start_no_worker_or_disk_write():
    app = ExportHost()
    with patch("kakao_gui_exports.filedialog.asksaveasfilename", return_value="") as chooser, \
         patch("kakao_gui_exports.threading.Thread") as thread, \
         patch("kakao_gui_exports.messagebox.showinfo") as info:
        assert app.request_preview_csv_export() is False
        assert app.request_visible_log_export() is False
        assert chooser.call_count == 2
        app.rows = []
        assert app.request_preview_csv_export() is False
        info.assert_called_once_with("저장할 내용 없음", "먼저 CSV 또는 XLSX 파일을 선택하거나 행을 추가해 주세요.")
        app._close_requested = True
        assert app.request_preview_csv_export() is False
        assert app.request_visible_log_export() is False
        assert chooser.call_count == 2
        thread.assert_not_called()


def test_close_during_save_chooser_starts_no_writer():
    app = ExportHost()
    def choose(**_kwargs):
        app._close_requested = True
        return "ignored.csv"
    with patch("kakao_gui_exports.filedialog.asksaveasfilename", side_effect=choose), patch("kakao_gui_exports.threading.Thread") as thread:
        assert app.request_preview_csv_export() is False
        thread.assert_not_called()


def test_thread_start_failure_is_a_save_failure_and_releases_pending_request():
    app = ExportHost()
    with patch("kakao_gui_exports.filedialog.asksaveasfilename", return_value="unstarted.csv"), \
         patch("kakao_gui_exports.threading.Thread") as thread, \
         patch("kakao_gui_exports.messagebox.showerror") as error:
        thread.return_value.start.side_effect = RuntimeError("fixture thread unavailable")
        assert app.request_preview_csv_export() is False
    assert app._exports_pending == {}
    assert app._export_writer._running is False
    assert not app._export_writer._requests
    error.assert_called_once_with("CSV 저장 실패", "unstarted.csv\n\nRuntimeError: fixture thread unavailable")


def test_csv_and_log_bytes_equal_existing_sync_writers():
    app = ExportHost()
    app.rows = [MessageRow(2, "한글,대상", '줄1\n줄2 "인용"', (Path("C:/자료/파일.pdf"),), scheduled_at=datetime(2027, 1, 2, 3, 4))]
    fixture_parent = Path.cwd() / ".test-tmp-exports"
    fixture_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=fixture_parent) as directory:
        root = Path(directory)
        csv_actual, csv_expected = root / "actual.csv", root / "expected.csv"
        csv_expected.write_text("\ufeff" + rows_to_csv_text(app.rows), encoding="utf-8")
        log_actual, log_expected = root / "actual.txt", root / "expected.txt"
        log_expected.write_text(app.log_text.text, encoding="utf-8")
        with patch("kakao_gui_exports.LOG_DIR", root / "logs"), \
             patch("kakao_gui_exports.filedialog.asksaveasfilename", side_effect=[str(csv_actual), str(log_actual)]):
            assert app.request_preview_csv_export()
            assert app.request_visible_log_export()
            app.handle_export_result(app.take_result())
            app.handle_export_result(app.take_result())
        assert csv_actual.read_bytes() == csv_expected.read_bytes()
        assert csv_actual.read_bytes().startswith(b"\xef\xbb\xbf")
        assert log_actual.read_bytes() == log_expected.read_bytes()
