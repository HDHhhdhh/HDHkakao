"""Hub feedback responsibilities; app state remains on the Tk owner thread."""
from __future__ import annotations

import queue
import time
from pathlib import Path
from typing import Any
from tkinter import messagebox
from kakao_app_paths import LOG_DIR
from kakao_document import attachment_status_text
from kakao_delivery import progress_summary
from kakao_send_run import SendRunCompleted, SendRunEvent, SendRunProgress
from kakao_sender import MessageRow, format_schedule_datetime, write_log
from kakao_gui_constants import ATTACHMENT_EMPTY_LABEL, FAILED_RETRY_INSTRUCTION, SEARCH_EMPTY_STATUS, SUCCESS_RESULT_STATUSES

UI_QUEUE_EVENT_LIMIT = 64
UI_QUEUE_TIME_BUDGET_SECONDS = 0.008
UI_QUEUE_CONTINUE_MS = 1
UI_QUEUE_ACTIVE_POLL_MS = 20
UI_QUEUE_IDLE_POLL_MS = 100

def format_attachment_empty_label() -> str:
    return ATTACHMENT_EMPTY_LABEL


def format_attachment_panel_help(shared: tuple[Path, ...] | None) -> str:
    if shared is None:
        return "행별 첨부 섞임 · 새 첨부를 적용하면 모든 행의 첨부를 같은 파일로 교체합니다."
    if not shared:
        return "모든 대화명 공통 · 여러 경로는 ; 또는 줄바꿈으로 구분합니다."
    return f"모든 대화명 공통: 파일 {len(shared)}개 · 새 첨부를 적용하면 기존 첨부를 교체합니다."


def format_search_empty_status() -> str:
    return SEARCH_EMPTY_STATUS


def format_search_no_result_status(query: str) -> str:
    return f"검색 결과 없음: {query}"


def format_search_match_status(position: int, total: int, query: str) -> str:
    return f"검색 {position}/{total}: {query}"


def format_failed_summary_count(failed_count: int) -> str:
    return f"전송 실패 {failed_count}건"


def format_failed_retry_instruction() -> str:
    return FAILED_RETRY_INSTRUCTION


def format_failed_summary_status(prefix: str, failed_count: int) -> str:
    return f"{prefix}: 실패 {failed_count}건 · 세부 로그를 확인하세요"


def format_retry_failed_button_label(failed_count: int) -> str:
    return f"직접 실패 2차 전송 ({failed_count})"


def format_done_status(stopped: bool, failed_count: int) -> str:
    prefix = "중지됨" if stopped else "완료"
    if failed_count:
        return format_failed_summary_status(prefix, failed_count)
    return prefix


def failed_result_indexes(results: list[dict[str, Any]], rows: list[MessageRow]) -> list[int]:
    indexes: list[int] = []
    seen: set[int] = set()
    for item in results:
        if item.get("status") != "error":
            continue
        row_index = item.get("row_index")
        if isinstance(row_index, int) and 0 <= row_index < len(rows) and row_index not in seen:
            indexes.append(row_index)
            seen.add(row_index)
            continue
        row_number = item.get("row")
        chat_name = str(item.get("chat_name") or "")
        for index, row in enumerate(rows):
            if index in seen:
                continue
            if row.row_number == row_number and row.chat_name == chat_name:
                indexes.append(index)
                seen.add(index)
                break
    return indexes


def macro_retry_result_indexes(results: list[dict[str, Any]], rows: list[MessageRow]) -> list[int]:
    """Return only explicit direct-send failures eligible for PC macro retry.

    The direct worker writes ``retry_transport=\"macro\"`` only after an
    explicit failure (including direct-bridge initialization failure).  An
    ``unknown`` outcome is deliberately excluded because the original send
    may already have reached KakaoTalk.  Row indexes, rather than recipient
    names, keep duplicate recipients as distinct retry items.
    """

    indexes: list[int] = []
    seen: set[int] = set()
    for item in results:
        if item.get("status") != "error" or item.get("retry_transport") != "macro":
            continue
        row_index = item.get("row_index")
        if isinstance(row_index, int) and 0 <= row_index < len(rows) and row_index not in seen:
            indexes.append(row_index)
            seen.add(row_index)
    return indexes


def row_status_tag(status: str) -> str:
    if status in {"전송", "붙여넣음", "예약", "예약 접수", "직접 전송", "예약 대기"}:
        return "success"
    if status == "진행 중":
        return "running"
    if status in {"오류", "매칭 실패"}:
        return "error"
    if status in {"확인 필요", "예약 확인 필요", "예약 중단", "예약 접수 · 검증 대기"}:
        return "warning"
    if status == "건너뜀":
        return "skipped"
    return "ready"


class HubRunFeedbackMixin:
    def post(self, event: str, payload: Any) -> None:
        self.ui_queue.put((event, payload))


    def drain_queue(self) -> None:
        """Yield to Tk between bounded FIFO slices of worker feedback."""
        started = time.perf_counter()
        succeeded = False
        self._feedback_batch_active = True
        try:
            for _ in range(UI_QUEUE_EVENT_LIMIT):
                try:
                    event, payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                self.handle_ui_event(event, payload)
                if time.perf_counter() - started >= UI_QUEUE_TIME_BUDGET_SECONDS:
                    break
            self._flush_feedback_updates()
            succeeded = True
        finally:
            self._feedback_batch_active = False
            try:
                if not succeeded:
                    self._flush_feedback_updates()
            finally:
                # Tk still reports rendering errors. Later cancellation and
                # completion events must retain a scheduled queue pump.
                delay = UI_QUEUE_IDLE_POLL_MS
                if succeeded:
                    if not self.ui_queue.empty():
                        delay = UI_QUEUE_CONTINUE_MS
                    elif self._feedback_work_active():
                        delay = UI_QUEUE_ACTIVE_POLL_MS
                self.after(delay, self.drain_queue)


    def _feedback_work_active(self) -> bool:
        state = self.__dict__
        history = state.get("_run_history_view")
        return (
            state.get("active_run") is not None
            or any(state.get(name) for name in (
                "_auto_match_loading", "_diagnostic_busy", "_document_loading", "_capacity_loading",
            ))
            or bool(state.get("_exports_pending"))
            or bool(state.get("_recovery_offer_token"))
            or (isinstance(history, dict) and bool(history.get("loading")))
        )


    def _request_feedback_summary(self) -> None:
        if self.__dict__.get("_feedback_batch_active"):
            self._feedback_summary_pending = True
        else:
            self.update_summary()


    def _set_feedback_value(self, event: str, payload: Any) -> None:
        if self.__dict__.get("_feedback_batch_active"):
            self.__dict__.setdefault("_feedback_values_pending", {})[event] = payload
        elif event == "status":
            self.status_var.set(payload)
        else:
            self.progress_var.set(payload)
            if hasattr(self, "progress_state_label"):
                self.progress_state_label.configure(text=f"{int(payload)}%")


    def _flush_feedback_updates(self) -> None:
        """Render observed values once per slice, before any modal/terminal UI."""
        if self.__dict__.pop("_feedback_summary_pending", False):
            self.update_summary()
        values = self.__dict__.pop("_feedback_values_pending", {})
        for event, payload in values.items():
            if event == "status":
                self.status_var.set(payload)
            else:
                self.progress_var.set(payload)
                if hasattr(self, "progress_state_label"):
                    self.progress_state_label.configure(text=f"{int(payload)}%")
        # The concrete GUI may also batch expensive log/scroll rendering.
        # Inspect the class without Tk's __getattr__ fallback in no-Tk tests.
        flusher = self.__dict__.get("flush_pending_visual_feedback")
        if callable(flusher):
            flusher()
        else:
            flusher = getattr(type(self), "flush_pending_visual_feedback", None)
            if callable(flusher):
                flusher(self)


    def handle_ui_event(self, event: str, payload: Any) -> None:
        """Apply one queue event on the Tk thread.

        New send events arrive through the run-ID envelope. Legacy event names
        remain accepted for non-send jobs and focused compatibility tests.
        """

        if event not in {"run_event", "row", "result", "progress", "status", "log"}:
            self._flush_feedback_updates()
        if event == "run_event":
            self.handle_send_run_event(payload)
        elif event == "diagnostic_result":
            self.handle_diagnostic_result(payload)
        elif event == "attachment_dimensions":
            self.handle_attachment_dimensions(payload)
        elif event == "recovery_offer_result":
            self.handle_recovery_offer_result(payload)
        elif event == "run_history_result":
            self.handle_run_history_result(payload)
        elif event == "document_load_result":
            self.handle_document_load_result(payload)
        elif event == "capacity_view_result":
            self.handle_capacity_view_result(payload)
        elif event == "export_result":
            self.handle_export_result(payload)
        elif event == "journal_started":
            self._active_journal_rows = tuple(payload)
        elif event == "journal_error":
            self._journal_error = str(payload)
            self.log("기록 오류", self._journal_error)
            self.status_var.set("실행 기록 저장 실패 · 재발송 전 기록 확인 필요")
        elif event == "log":
            category, message = payload
            self.log(category, message)
        elif event == "status":
            self._set_feedback_value(event, payload)
        elif event == 'schedule_capacity':
            self.render_schedule_capacity(payload)
        elif event == "tablet_reauth":
            state, detail = payload
            if state == "passcode":
                self.status_var.set("태블릿 인증 확인 대기")
                messagebox.showinfo(
                    "카카오 태블릿 인증",
                    f"카카오톡에서 태블릿 인증 요청을 확인해 주세요.\n\n인증번호: {detail}",
                )
            elif state == "force_starting":
                self.status_var.set("기존 태블릿 슬롯을 다시 등록하는 중")
                self.log("연결 복구", "저장된 태블릿 슬롯을 다시 등록하고 있습니다.")
            elif state == "complete":
                self.status_var.set("태블릿 인증 자동 복구 완료")
                self.log("연결 복구", "태블릿 인증정보를 새로 발급해 암호화 저장했습니다.")
            elif state == "starting":
                self.status_var.set("태블릿 인증 자동 복구 중")
                self.log("연결 복구", "명시적 인증 해제를 감지해 태블릿 설정을 다시 불러옵니다.")
        elif event in {"screen_use_state", "screen_use"}:
            self.handle_screen_use_state(payload)
        elif event == "auto_match_result":
            self.handle_auto_chat_match_result(payload)
            self.refresh_delivery_routes()
        elif event == "result":
            records = self.__dict__.setdefault("_progress_results", {})
            records[int(payload["row_index"])] = dict(payload)
            self.__dict__.setdefault("_running_row_indexes", set()).discard(int(payload["row_index"]))
            self._request_feedback_summary()
            self.refresh_result_delivery_route(payload)
        elif event == "row":
            running = self.__dict__.setdefault("_running_row_indexes", set())
            if payload[1] == "진행 중":
                running.add(int(payload[0]))
            else:
                running.discard(int(payload[0]))
            self.update_row_status(*payload)
            self._request_feedback_summary()
        elif event == "progress":
            self._set_feedback_value(event, payload)
        elif event == "error":
            messagebox.showerror("실행 오류", payload)
            self.log("오류", payload)
        elif event == "confirm":
            row, index, total, scheduled_at, response = payload
            active_run = getattr(self, "active_run", None)
            cancel_event = active_run.cancel_event if active_run is not None else self.stop_event
            if cancel_event.is_set():
                response.put("stop")
                return
            action = f"예약 등록: {format_schedule_datetime(scheduled_at)}" if scheduled_at is not None else "즉시 발송"
            decision = "stop"
            try:
                answer = messagebox.askyesnocancel(
                    "한 명씩 확인",
                    f"[{index}/{total}] '{row.chat_name}'에게 처리할까요?\n{action}\n\n예: 진행\n아니오: 건너뜀\n취소: 중지",
                )
                if not cancel_event.is_set():
                    decision = "send" if answer is True else ("skip" if answer is False else "stop")
            finally:
                response.put(decision)
        elif event == "confirm_schedule_ready":
            row, scheduled_at, response = payload
            active_run = getattr(self, "active_run", None)
            cancel_event = active_run.cancel_event if active_run is not None else self.stop_event
            answer = False
            try:
                if not cancel_event.is_set():
                    answer = self.show_schedule_review(row, scheduled_at, cancel_event)
            except Exception as exc:
                # Release the waiting worker and keep the Tk queue pump alive
                # so its cancellation/completion events can restore controls.
                self.post("log", ("오류", f"예약 내용 대조창을 표시하지 못해 등록을 중단합니다 ({type(exc).__name__})."))
            finally:
                response.put(answer is True and not cancel_event.is_set())


    def handle_send_run_event(self, event: SendRunEvent) -> bool:
        """Reject stale runs before they can mutate any Tk-owned state."""

        active_run = getattr(self, "active_run", None)
        if active_run is None or event.run_id != active_run.run_id:
            return False
        if isinstance(event, SendRunProgress):
            self.handle_ui_event(event.kind, event.payload)
            return True
        self._flush_feedback_updates()
        self.finish_send_run(event)
        return True


    def finish_send_run(self, completed: SendRunCompleted) -> None:
        """Commit one supervisor terminal snapshot on the Tk thread."""

        active_run = getattr(self, "active_run", None)
        if active_run is None or completed.run_id != active_run.run_id:
            return

        # Claim the terminal event before any logging/dialog side effect. A
        # duplicate delivery can therefore never commit or publish twice.
        self.active_run = None
        self.worker = None
        self.results = completed.legacy_results()
        self._progress_results = {}
        self._running_row_indexes = set()
        self.advance_run_origins(str(completed.run_id), self.results)
        self.restore_send_controls()
        self.update_summary()
        if self.results:
            try:
                log_path = write_log(LOG_DIR, self.results, run_id=str(completed.run_id))
                self.log("로그", f"결과 로그 저장: {log_path}")
            except Exception as exc:
                self.log("오류", f"결과 로그 저장 실패: {exc}")

        failed_rows = [item for item in self.results if item["status"] == "error"]
        unknown_rows = [item for item in self.results if item["status"] == "unknown"]
        if failed_rows:
            self.log_failed_summary(failed_rows)
        if unknown_rows:
            self.log_uncertain_summary(unknown_rows)
            messagebox.showwarning(
                "전송 결과 확인 필요",
                f"{len(unknown_rows)}건의 처리 결과를 확정할 수 없습니다.\n\n"
                "예약 건은 카카오톡 예약 목록의 대상·본문·일시를, 즉시 전송 건은 대화방의 수신 내용을 확인하세요.\n\n"
                "중복 처리를 막기 위해 자동 재전송 대상에서는 제외했습니다. 미시도 행은 [실행 기록]에서 이어서 진행할 수 있습니다.",
            )

        if completed.failure is not None:
            # Do not surface raw exception text here: bridge/SDK errors can
            # contain credentials or request fragments. Expected failures have
            # already emitted a deliberately worded UI event.
            self.log(
                "오류",
                f"발송 실행이 안전하게 중단되었습니다 (오류 유형: {completed.failure.exception_type}).",
            )
        for failure in completed.finalizer_failures:
            operation = failure.operation or "cleanup"
            self.log("정리", f"실행 정리 경고: {operation} ({failure.exception_type})")

        success = sum(1 for item in self.results if item["status"] in SUCCESS_RESULT_STATUSES)
        failed = len(failed_rows)
        unknown = len(unknown_rows)
        batch_unattempted = sum(
            item.get("status") == "stopped" and item.get("failure_stage") == "direct_batch_not_attempted"
            for item in self.results
        )
        prewrite_unattempted = sum(
            item.get("status") == "stopped" and item.get("failure_stage") == "direct_prewrite"
            for item in self.results
        )
        retryable = len(
            macro_retry_result_indexes(
                self.results,
                list(self.__dict__.get("rows", [])),
            )
        )
        if self.__dict__.get("_journal_error"):
            self.status_var.set("실행 기록 저장 실패 · 앱 재시작 후 [실행 기록] 확인")
        elif prewrite_unattempted:
            self.status_var.set(f"연결 복구 중단 · 성공 {success}건 · 미시도 {prewrite_unattempted + batch_unattempted}건 · [실행 기록]에서 이어서 진행")
        elif batch_unattempted:
            self.status_var.set(f"직접 전송 중단 · 실패 {failed}건 · 확인 필요 {unknown}건 · 후속 {batch_unattempted}건 미시도")
        elif unknown:
            suffix = f" · 직접 실패 2차 전송 {retryable}건 대기" if retryable else ""
            self.status_var.set(f"완료 · 확인 필요 {unknown}건 · 자동 재전송 제외{suffix}")
        elif retryable:
            self.status_var.set(f"완료 · 직접 실패 {retryable}건 · [직접 실패 2차 전송]을 누르세요")
        elif completed.failure is not None:
            self.status_var.set("오류로 안전 중단 · 로그를 확인하세요")
        elif completed.finalizer_failures:
            self.status_var.set("완료 · 연결 정리 경고")
        else:
            self.status_var.set(format_done_status(completed.cancelled, failed))
        unattempted_count = prewrite_unattempted + batch_unattempted
        unattempted_note = f", 미시도 {unattempted_count}건" if unattempted_count else ""
        self.log("완료", f"작업 종료: 성공 {success}건, 실패 {failed}건, 확인 필요 {unknown}건{unattempted_note}")
        self.run_row_indexes = None

        if getattr(self, "_close_requested", False):
            # A send completion can race with an independently active PC
            # auto-match cleanup.  Do not let its terminal event destroy Tk
            # (and therefore stop draining screen-state events) until the
            # exact KakaoTalk room has been closed and the search state reset.
            if self._screen_use_is_active():
                self._schedule_close_poll()
            else:
                self.after_idle(self.destroy)


    def advance_run_origins(self, run_id: str, results: list[dict[str, Any]]) -> None:
        """Move recovered rows to the durable child after every attempted run."""
        started_rows = self.__dict__.pop("_active_journal_rows", ())
        if not started_rows:
            return
        sources = self.__dict__.setdefault("_recovery_sources_by_number", {})
        latest = self.__dict__.setdefault("_last_run_sources_by_number", {})
        retry_records = self.__dict__.setdefault("_recovery_retry_records_by_number", {})
        records = {item["row_index"]: item for item in results}
        for index, number in started_rows:
            latest[number] = (run_id, index)
            if number in sources:
                sources[number] = (run_id, index)
                record = records.get(index, {})
                if record.get("status") == "error" and record.get("retry_transport") == "macro":
                    retry_records[number] = dict(record)
                else:
                    retry_records.pop(number, None)
        self._restored_retry_only = bool(retry_records)


    def restore_send_controls(self) -> None:
        self.start_button.state(["!disabled"])
        self.test_one_check.state(["!disabled"])
        self.set_attachment_edit_state(False)
        if "direct_toggle" in self.__dict__:
            self.direct_toggle.state(["!disabled"])
        if "mapping_button" in self.__dict__:
            self.mapping_button.state(["!disabled"])
        if "manual_mapping_button" in self.__dict__:
            self.manual_mapping_button.state(["!disabled"])
        self.stop_button.state(["disabled"])


    def update_summary(self) -> None:
        active = self.__dict__.get("active_run") is not None
        records = list(self.__dict__.get("_progress_results", {}).values()) if active else self.results
        total = self.run_total if (active or records) and self.run_total else len(self.rows)
        self.summary_var.set(progress_summary(total, records, self.__dict__.get("_running_row_indexes", ())))
        self.refresh_retry_failed_action()
        attachment_count, _ = attachment_status_text(self.rows)
        if hasattr(self, "attachment_state_label"):
            self.attachment_state_label.configure(text=attachment_count)


    def tick_elapsed(self) -> None:
        if self.started_at and self.worker and self.worker.is_alive():
            elapsed = int(time.time() - self.started_at)
            hours, rem = divmod(elapsed, 3600)
            minutes, seconds = divmod(rem, 60)
            self.elapsed_var.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        self.after(1000, self.tick_elapsed)
