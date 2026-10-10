"""Slow local reads must leave Tk free and commit only the current result."""
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kakao_gui_loading import HubLoadingMixin


class TkValue:
    def __init__(self):
        self.owner = threading.get_ident()
        self.values = []

    def set(self, value):
        assert threading.get_ident() == self.owner, "Tk value written from worker"
        self.values.append(value)


class LoadingHost(HubLoadingMixin):
    def __init__(self, read=None, factory=None):
        self.owner = threading.get_ident()
        self._close_requested = False
        self.file_path = Path("old.csv")
        self.rows = ["old row"]
        self.events = queue.Queue()
        self.status_var = TkValue()
        self.schedule_counter_var = TkValue()
        self.schedule_counter_detail_var = TkValue()
        self.loading_states = []
        self.documents = []
        self.views = []
        self.logs = []
        self.read = read
        self.factory = factory

    def _read_document(self, path):
        assert threading.get_ident() != self.owner, "File parsed on Tk"
        return self.read(path)

    def _new_schedule_capacity_store(self):
        assert threading.get_ident() != self.owner, "Store opened on Tk"
        return self.factory()

    def set_document_loading(self, loading):
        assert threading.get_ident() == self.owner, "Widget changed from worker"
        self.loading_states.append(loading)

    def apply_loaded_document(self, document):
        assert threading.get_ident() == self.owner, "Document applied from worker"
        self.documents.append(document)
        self.file_path = document.source_path
        self.rows = list(document.rows)

    def render_schedule_capacity(self, view):
        assert threading.get_ident() == self.owner, "Counter rendered from worker"
        self.views.append(view)

    def log(self, category, message):
        assert threading.get_ident() == self.owner, "Log rendered from worker"
        self.logs.append((category, message))

    def post(self, event, payload):
        self.events.put((event, payload))

    def take_result(self, expected):
        event, payload = self.events.get(timeout=3)
        assert event == expected
        return payload


def document(path, text="new row"):
    return SimpleNamespace(source_path=Path(path), rows=(text,))


def blocking_read(entered, release):
    def read(path):
        entered.set()
        assert release.wait(3), "Test did not release the file reader"
        return document(path)
    return read


def test_slow_file_callback_returns_before_io_and_commits_on_tk():
    entered = threading.Event()
    release = threading.Event()
    app = LoadingHost(blocking_read(entered, release))
    try:
        started = time.perf_counter()
        assert app.request_document_load(Path("new.xlsx"))
        assert time.perf_counter() - started < 0.5
        assert entered.wait(3)
        assert app.rows == ["old row"]
        assert app.loading_states == [True]
        assert app.events.empty()
        # A user callback can still write a Tk value while parsing is blocked.
        app.schedule_counter_var.set("responsive")
        release.set()
        payload = app.take_result("document_load_result")
        assert app.documents == []
        app.handle_document_load_result(payload)
        assert app.rows == ["new row"]
        assert app.file_path == Path("new.xlsx")
        assert app.loading_states == [True, False]
    finally:
        release.set()


def test_latest_file_request_replaces_pending_choice_without_overlapping_reads():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def read(path):
        calls.append(path.name)
        if path.name == "first.csv":
            entered.set()
            assert release.wait(3)
        return document(path, path.name)

    app = LoadingHost(read)
    try:
        app.request_document_load(Path("first.csv"))
        assert entered.wait(3)
        app.request_document_load(Path("discarded.csv"))
        app.request_document_load(Path("latest.csv"))
        assert calls == ["first.csv"]
        release.set()
        first = app.take_result("document_load_result")
        app.handle_document_load_result(first)
        assert app.documents == []
        current = app.take_result("document_load_result")
        app.handle_document_load_result(current)
        assert calls == ["first.csv", "latest.csv"]
        assert app.rows == ["latest.csv"]
        app.handle_document_load_result(first)
        assert app.rows == ["latest.csv"]
    finally:
        release.set()


@pytest.mark.parametrize("close", [False, True])
def test_cancelled_or_closed_file_result_keeps_existing_document(close):
    entered = threading.Event()
    release = threading.Event()
    app = LoadingHost(blocking_read(entered, release))
    try:
        app.request_document_load(Path("ignored.csv"))
        assert entered.wait(3)
        app._close_requested = close
        app.cancel_document_load()
        before = (list(app.loading_states), list(app.status_var.values))
        release.set()
        app.handle_document_load_result(app.take_result("document_load_result"))
        assert app.rows == ["old row"]
        assert app.file_path == Path("old.csv")
        assert app.documents == []
        assert (app.loading_states, app.status_var.values) == before
        assert app._document_loading is False
    finally:
        release.set()


def test_failed_file_read_preserves_rows_and_unlocks_only_on_tk():
    def fail(_path):
        raise ValueError("fixture malformed workbook")

    app = LoadingHost(fail)
    app.request_document_load(Path("bad.xlsx"))
    payload = app.take_result("document_load_result")
    with patch("kakao_gui_loading.messagebox.showerror") as error:
        app.handle_document_load_result(payload)
    error.assert_called_once_with("파일 읽기 실패", "fixture malformed workbook")
    assert app.rows == ["old row"]
    assert app.file_path == Path("old.csv")
    assert app.loading_states == [True, False]
    assert "기존 미리보기 유지" in app.status_var.values[-1]


@pytest.mark.parametrize("state", ["active_run", "_auto_match_waiting_to_send", "_active_send_preparation_token", "worker"])
def test_file_request_during_send_or_preparation_starts_no_reader(state):
    reads = []
    app = LoadingHost(lambda path: reads.append(path))
    setattr(app, state, SimpleNamespace(is_alive=lambda: True) if state == "worker" else object())
    with patch("kakao_gui_loading.messagebox.showinfo"):
        assert app.request_document_load(Path("blocked.csv")) is False
    assert reads == []
    assert app.loading_states == []
    assert app.rows == ["old row"]


def test_send_started_before_document_result_prevents_commit():
    app = LoadingHost(lambda path: document(path))
    app.request_document_load(Path("blocked.csv"))
    payload = app.take_result("document_load_result")
    app.active_run = object()
    app.handle_document_load_result(payload)
    assert app.documents == []
    assert app.rows == ["old row"]
    assert app.loading_states == [True, False]


class BlockingCapacity:
    def __init__(self, owner, entered, release):
        self.owner = owner
        self.entered = entered
        self.release = release
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    def view(self):
        assert threading.get_ident() != self.owner, "Counter read on Tk"
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.calls == 1:
                self.entered.set()
                assert self.release.wait(3)
            return f"view {self.calls}"
        finally:
            self.active -= 1


def test_capacity_tick_returns_before_io_and_coalesces_busy_manual_refresh():
    entered = threading.Event()
    release = threading.Event()
    store = BlockingCapacity(threading.get_ident(), entered, release)
    factories = []
    app = LoadingHost(factory=lambda: (factories.append(threading.get_ident()), store)[1])
    app._schedule_capacity_store = SimpleNamespace(view=lambda: pytest.fail("Sync store used by worker"))
    app.active_run = object()  # The footer still refreshes while a send is active.
    try:
        started = time.perf_counter()
        assert app.request_schedule_capacity_refresh()
        assert time.perf_counter() - started < 0.5
        assert entered.wait(3)
        for _ in range(20):
            assert app.request_schedule_capacity_refresh() is False
            assert app.request_schedule_capacity_refresh(manual=True) is False
        assert store.calls == 1
        assert app.views == []
        release.set()
        first = app.take_result("capacity_view_result")
        app.handle_capacity_view_result(first)
        second = app.take_result("capacity_view_result")
        assert second[3] is True
        app.handle_capacity_view_result(second)
        assert store.calls == 2
        assert store.maximum_active == 1
        assert len(factories) == 1
        assert factories[0] != app.owner
        assert app.views == ["view 1", "view 2"]
        assert app._capacity_loading is False
        assert app.status_var.values == ["예약 기록과 현재 시각으로 슬롯을 계산했습니다."]
        app.handle_capacity_view_result(first)
        assert app.views == ["view 1", "view 2"]
    finally:
        release.set()


def test_capacity_closed_result_and_followup_do_not_touch_widgets():
    entered = threading.Event()
    release = threading.Event()
    store = BlockingCapacity(threading.get_ident(), entered, release)
    app = LoadingHost(factory=lambda: store)
    try:
        app.request_schedule_capacity_refresh()
        assert entered.wait(3)
        app.request_schedule_capacity_refresh(manual=True)
        app._close_requested = True
        release.set()
        app.handle_capacity_view_result(app.take_result("capacity_view_result"))
        assert app.views == []
        assert app.schedule_counter_var.values == []
        assert app.status_var.values == []
        assert store.calls == 1
        assert app.request_schedule_capacity_refresh(manual=True) is False
    finally:
        release.set()


def test_capacity_error_keeps_explicit_blocked_status_on_tk():
    def fail_view():
        raise RuntimeError("fixture damaged reservation record")

    app = LoadingHost(factory=lambda: SimpleNamespace(view=fail_view))
    app.request_schedule_capacity_refresh(manual=True)
    app.handle_capacity_view_result(app.take_result("capacity_view_result"))
    assert app.views == []
    assert app.schedule_counter_var.values == ["예약 기록 확인 필요 · 자동 등록 차단"]
    assert app.schedule_counter_detail_var.values == ["예약 기록 계산 중 · 현재 표시를 유지합니다.", "fixture damaged reservation record"]
    assert app.status_var.values == ["예약 기록 확인 실패: fixture damaged reservation record"]
    assert app._capacity_loading is False


def test_document_thread_start_failure_unlocks_and_keeps_old_rows():
    app = LoadingHost(lambda path: document(path))
    with patch('kakao_gui_loading.threading.Thread.start', side_effect=RuntimeError('thread unavailable')), \
         patch('kakao_gui_loading.messagebox.showerror') as error:
        app.request_document_load(Path('new.csv'))
    assert app.rows == ['old row']
    assert app._document_loading is False
    assert app._document_load_active_token is None
    error.assert_called_once()


def test_capacity_thread_start_failure_releases_reader():
    app = LoadingHost(factory=lambda: SimpleNamespace(view=lambda: 'fixture'))
    with patch('kakao_gui_loading.threading.Thread.start', side_effect=RuntimeError('thread unavailable')):
        assert app.request_schedule_capacity_refresh(manual=True) is False
    assert app._capacity_loading is False
    assert app.views == []
    assert app.schedule_counter_var.values == ['예약 기록 확인 필요 · 자동 등록 차단']


def test_capacity_older_read_cannot_overwrite_newer_service_view():
    entered, release = threading.Event(), threading.Event()

    def read_view():
        entered.set()
        assert release.wait(3)
        return 'older view'

    app = LoadingHost(factory=lambda: SimpleNamespace(view=read_view))
    try:
        app.request_schedule_capacity_refresh()
        assert entered.wait(3)
        app._capacity_render_revision = 1
        app.render_schedule_capacity('newer service view')
        release.set()
        app.handle_capacity_view_result(app.take_result('capacity_view_result'))
        assert app.views == ['newer service view']
        assert app._capacity_loading is False
    finally:
        release.set()
