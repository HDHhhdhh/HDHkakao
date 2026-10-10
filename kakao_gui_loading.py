"""Read files and local reservation views without blocking the Tk owner."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from tkinter import messagebox
from typing import Any, Callable


@dataclass(frozen=True)
class _DocumentRead:
    token: int
    path: Path
    read: Callable[[Path], Any]
    post: Callable[[str, Any], None]


def _read_document(request: _DocumentRead) -> None:
    """The worker receives values and callbacks captured on the Tk thread."""
    document = None
    error = None
    try:
        document = request.read(request.path)
    except Exception as exc:
        error = str(exc)
    request.post("document_load_result", (request.token, document, error))


class _CapacityReader:
    """Only the serialized workers access the store in this reader."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self.factory = factory
        self.store = None

    def view(self):
        if self.store is None:
            self.store = self.factory()
        return self.store.view()


def _read_capacity(
    token: int, reader: _CapacityReader, manual: bool,
    post: Callable[[str, Any], None],
) -> None:
    view = None
    error = None
    try:
        view = reader.view()
    except Exception as exc:
        error = str(exc)
    post("capacity_view_result", (token, view, error, manual))


class HubLoadingMixin:
    """All request and result methods run on Tk; workers only read and post.

    The host supplies ``_read_document``, ``apply_loaded_document``,
    ``set_document_loading``, and ``_new_schedule_capacity_store``. It routes
    the two result events through its existing thread-safe UI queue.
    """

    def _document_read_blocked(self) -> bool:
        state = self.__dict__
        worker = state.get("worker")
        return bool(
            state.get("_close_requested")
            or state.get("active_run") is not None
            or (worker is not None and worker.is_alive())
            or state.get("_auto_match_waiting_to_send")
            or state.get("_active_send_preparation_token") is not None
        )

    def _set_document_read_state(self, loading: bool) -> None:
        self._document_loading = loading
        self.set_document_loading(loading)

    def request_document_load(self, candidate_path: Path | None = None) -> bool:
        """Queue the latest file choice and return before parsing starts."""
        if self.__dict__.get("_close_requested"):
            return False
        if self._document_read_blocked():
            messagebox.showinfo(
                "실행 중", "발송 준비와 결과 처리가 끝난 뒤 파일을 변경해 주세요.",
            )
            return False
        path = candidate_path if candidate_path is not None else self.__dict__.get("file_path")
        if path is None:
            messagebox.showinfo("파일 선택", "먼저 CSV 또는 XLSX 파일을 선택해 주세요.")
            return False
        token = int(self.__dict__.get("_document_load_generation", 0)) + 1
        self._document_load_generation = token
        request = _DocumentRead(token, Path(path), self._read_document, self.post)
        self._set_document_read_state(True)
        self.status_var.set(f"파일 읽는 중 · {request.path.name}")
        if self.__dict__.get("_document_load_active_token") is not None:
            self._document_load_pending_request = request
        else:
            self._start_document_read(request)
        return True

    def _start_document_read(self, request: _DocumentRead) -> None:
        self._document_load_active_token = request.token
        self._document_load_pending_request = None
        try:
            threading.Thread(
                target=_read_document, args=(request,),
                name="kakao-document-read", daemon=True,
            ).start()
        except RuntimeError as exc:
            self.handle_document_load_result((request.token, None, str(exc)))

    def cancel_document_load(self) -> None:
        """Discard queued choices and invalidate the active read's result."""
        self._document_load_generation = int(self.__dict__.get("_document_load_generation", 0)) + 1
        self._document_load_pending_request = None
        if not self.__dict__.get("_close_requested") and self.__dict__.get("_document_loading"):
            self._set_document_read_state(False)
        else:
            self._document_loading = False

    def handle_document_load_result(self, payload) -> None:
        token, document, error = payload
        if token != self.__dict__.get("_document_load_active_token"):
            return
        self._document_load_active_token = None
        pending = self.__dict__.pop("_document_load_pending_request", None)
        if self.__dict__.get("_close_requested"):
            self._document_loading = False
            return
        if self._document_read_blocked():
            if self.__dict__.get("_document_loading"):
                self._set_document_read_state(False)
            return
        if pending is not None:
            self._start_document_read(pending)
            return
        if token != self.__dict__.get("_document_load_generation"):
            return
        self._set_document_read_state(False)
        if error is not None:
            messagebox.showerror("파일 읽기 실패", str(error))
            self.log("오류", f"파일을 읽지 못했습니다: {error}")
            self.status_var.set("파일 읽기 실패 · 기존 미리보기 유지")
            return
        self.apply_loaded_document(document)

    def request_schedule_capacity_refresh(self, manual: bool = False) -> bool:
        """Keep one local view read active; coalesce manual requests once."""
        if self.__dict__.get("_close_requested"):
            return False
        if self.__dict__.get("_capacity_loading"):
            if manual:
                self._capacity_refresh_pending = True
            return False
        reader = self.__dict__.get("_capacity_reader")
        if reader is None:
            reader = _CapacityReader(self._new_schedule_capacity_store)
            self._capacity_reader = reader
        token = int(self.__dict__.get("_capacity_view_generation", 0)) + 1
        self._capacity_view_generation = token
        self._capacity_loading = True
        self._capacity_read_revision = self.__dict__.get("_capacity_render_revision", 0)
        if manual:
            self.schedule_counter_detail_var.set("예약 기록 계산 중 · 현재 표시를 유지합니다.")
        try:
            threading.Thread(
                target=_read_capacity, args=(token, reader, bool(manual), self.post),
                name="kakao-capacity-view", daemon=True,
            ).start()
        except RuntimeError as exc:
            self.handle_capacity_view_result((token, None, str(exc), bool(manual)))
            return False
        return True

    def handle_capacity_view_result(self, payload) -> None:
        token, view, error, manual = payload
        if token != self.__dict__.get("_capacity_view_generation") or not self.__dict__.get("_capacity_loading"):
            return
        self._capacity_loading = False
        pending = self.__dict__.pop("_capacity_refresh_pending", False)
        if self.__dict__.get("_close_requested"):
            return
        current = self.__dict__.get("_capacity_read_revision", 0) == self.__dict__.get("_capacity_render_revision", 0)
        if not current:
            # A newer service event already painted the ledger while this
            # read was in flight. Release this reader without rolling it back.
            pass
        elif error is None:
            self.render_schedule_capacity(view)
            if manual:
                self.status_var.set("예약 기록과 현재 시각으로 슬롯을 계산했습니다.")
        else:
            self.schedule_counter_var.set("예약 기록 확인 필요 · 자동 등록 차단")
            self.schedule_counter_detail_var.set(str(error))
            if manual:
                self.status_var.set(f"예약 기록 확인 실패: {error}")
        if pending:
            self.request_schedule_capacity_refresh(manual=True)
