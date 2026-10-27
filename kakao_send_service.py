"""Tk-independent send orchestration with explicit I/O ports."""
from __future__ import annotations

from kakao_schedule_journal import reservation_key
import time
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from kakao_direct import DirectSendError, DirectSendNotStartedError, DirectSendUnknownError, redact_error, validate_direct_send_receipt
from kakao_attachment_run import AttachmentRunSnapshot
from kakao_mapping_policy import normalize_chat_match_name, resolve_chat_mapping
from kakao_send_run import SendRunContext, SendRunCancelled
from kakao_run_journal import RunJournalError
from kakao_pc_lease import PcUiLeaseCancelled
from kakao_sender import MessageRow, ScheduleRegistrationResult, ScheduleNotSubmittedError, FocusSafetyError, MacroSendUnknownError, MacroSendCancelled, format_schedule_datetime, schedule_datetime_for_row, validate_schedule_row, validate_send_mode, missing_attachment_paths


DEFAULT_DIRECT_SEND_BATCH_SIZE = 30
MIN_DIRECT_SEND_BATCH_PAUSE_SECONDS = 2.0
MAX_DIRECT_PREWRITE_RECOVERIES = 2
DIRECT_PREWRITE_RECOVERY_PAUSE_SECONDS = 2.0


def direct_send_batch_policy(config: dict[str, Any]) -> tuple[int, float]:
    """Count bridge calls per recipient row, not messages inside attachments."""
    batch_size = config.get("direct_send_batch_size", DEFAULT_DIRECT_SEND_BATCH_SIZE)
    if type(batch_size) is not int or not 1 <= batch_size <= DEFAULT_DIRECT_SEND_BATCH_SIZE:
        batch_size = DEFAULT_DIRECT_SEND_BATCH_SIZE
    value = config.get("direct_send_batch_pause_seconds", MIN_DIRECT_SEND_BATCH_PAUSE_SECONDS)
    if isinstance(value, bool):
        return batch_size, MIN_DIRECT_SEND_BATCH_PAUSE_SECONDS
    try:
        pause = float(value)
    except (TypeError, ValueError, OverflowError):
        return batch_size, MIN_DIRECT_SEND_BATCH_PAUSE_SECONDS
    return batch_size, max(MIN_DIRECT_SEND_BATCH_PAUSE_SECONDS, pause) if math.isfinite(pause) else MIN_DIRECT_SEND_BATCH_PAUSE_SECONDS


def wait_for_direct_cancel(cancel_event: Any, seconds: float) -> bool:
    return bool(cancel_event.wait(seconds))


@dataclass(frozen=True, slots=True)
class SendRunRequest:
    selected: tuple[tuple[int, MessageRow], ...]
    config: dict[str, Any]
    countdown: int
    delay_scale: float
    common_schedule: datetime | None
    continue_on_error: bool
    confirm_each: bool
    direct_enabled: bool = False
    chat_mappings: tuple[tuple[str, str], ...] = ()
    direct_config: dict[str, Any] | None = None
    blocked_direct_names: tuple[str, ...] = ()
    send_mode: str = "normal"
    source_path: str | None = None
    recovery_of: str | None = None
    recovery_rows: tuple[tuple[int, int], ...] = ()
    recovery_sources: tuple[tuple[int, str, int], ...] = ()


def preserve_macro_retry_queue(
    context: SendRunContext,
    pending: Iterable[tuple[int, MessageRow]],
    *,
    detail: str,
) -> None:
    """Record unattempted second-pass rows so a cancellation cannot lose them."""

    for row_index, row in pending:
        context.add_result(
            {
                "row_index": row_index,
                "row": row.row_number,
                "chat_name": row.chat_name,
                "status": "error",
                "detail": detail,
                "attachments": row.attachment_count,
                "scheduled_at": "",
                "transport": "macro_retry",
                "failure_stage": "macro_retry_not_attempted",
                "retry_transport": "macro",
            }
        )
        context.emit("row", (row_index, "오류"))


def is_fatal_send_error(exc: Exception) -> bool:
    if isinstance(exc, MacroSendUnknownError):
        return True
    if not isinstance(exc, FocusSafetyError):
        return False
    message = str(exc)
    fatal_markers = (
        "pywin32",
        "메인 창 핸들",
        "카카오톡 메인 창을 찾지 못",
        "메인 창 대신",
        "닫을 대화창",
        "대화창을 닫지 못해",
        "메인 창이 닫힘 대상",
        "예약 메시지",
        "카카오톡 + 메뉴",
    )
    return any(marker in message for marker in fatal_markers)


def preflight_next_action(message: str) -> str:
    if "chat ID" in message or "매칭" in message:
        return "[자동 매칭]을 다시 실행하고, 남으면 [수동 매칭]에서 검증된 1:1 방을 직접 지정해 주세요."
    if "메인 창" in message:
        return "카카오톡 메인 창을 열고 가려진 대화창을 닫은 뒤 다시 실행하세요."
    if "좌표" in message or "검색창" in message:
        return "카카오톡 메인 창을 열고 [화면 좌표 설정](좌표 찍기)을 다시 실행하세요."
    if "파일" in message and ("선택" in message or "찾을 수 없습니다" in message):
        return "파일 경로를 확인한 뒤 CSV/XLSX를 다시 읽거나 첨부를 다시 선택하세요."
    if "예약" in message:
        return "예약일시는 YYYY-MM-DD HH:MM 형식으로 맞추고 첨부가 있는 예약 행은 즉시 발송으로 분리하세요."
    if "행" in message or "대화명" in message:
        return "미리보기에서 비어 있는 대화명, 메시지, 첨부를 먼저 채우세요."
    return "미리보기와 설정을 확인한 뒤 다시 [실행 시작]을 누르세요."


def format_preflight_warning(message: str) -> str:
    return f"{message}\n\n다음 행동\n- {preflight_next_action(message)}"


def row_uses_direct_send(row: MessageRow, scheduled_at: datetime | None, direct_enabled: bool) -> bool:
    return bool(direct_enabled and scheduled_at is None and (row.attachments or row.text.strip()))


def direct_attachment_result_summary(result: Any, row: MessageRow, chat_id: str) -> dict[str, str | int]:
    """Keep only bounded receipt metadata; never persist paths or media URLs."""
    def validate_receipt(receipt: Any) -> str:
        return validate_direct_send_receipt(
            receipt, chat_id, error_code="attachment_receipt_incomplete",
        )["log_id"]

    final_log_id = validate_receipt(result)
    if result.get("partial"):
        raise DirectSendUnknownError(code="attachment_receipt_incomplete")
    receipts = result.get("results")
    expected_text = int(bool(row.text.strip()))
    if (
        not isinstance(receipts, (tuple, list))
        or len(receipts) != row.attachment_count + expected_text
        or type(result.get("attachment_count")) is not int
        or result.get("attachment_count") != row.attachment_count
    ):
        raise DirectSendUnknownError(code="attachment_receipt_incomplete")
    counts = {"text": 0, "photo": 0, "file": 0}
    log_ids: list[str] = []
    seen_ids: set[int] = set()
    for receipt in receipts:
        log_id = validate_receipt(receipt)
        kind = receipt.get("kind")
        if not isinstance(kind, str) or kind not in counts:
            raise DirectSendUnknownError(code="attachment_receipt_incomplete")
        counts[kind] += 1
        if int(log_id) in seen_ids:
            raise DirectSendUnknownError(code="attachment_receipt_incomplete")
        seen_ids.add(int(log_id))
        log_ids.append(log_id)
    if (counts["text"] != expected_text or counts["photo"] + counts["file"] != row.attachment_count
            or not log_ids or final_log_id != log_ids[-1]
            or result["sent_at"] != receipts[-1]["sent_at"]):
        raise DirectSendUnknownError(code="attachment_receipt_incomplete")
    return {
        "direct_receipt_count": len(receipts),
        "direct_text_count": counts["text"],
        "direct_photo_count": counts["photo"],
        "direct_file_count": counts["file"],
        "direct_log_ids": ",".join(log_ids),
    }


def selected_rows_require_macro(
    selected: list[tuple[int, MessageRow]] | tuple[tuple[int, MessageRow], ...],
    fallback_schedule: datetime | None,
    direct_enabled: bool,
) -> bool:
    return any(
        not row_uses_direct_send(row, schedule_datetime_for_row(row, fallback_schedule), direct_enabled)
        for _, row in selected
    )


def is_unknown_direct_send_error(exc: Exception) -> bool:
    return isinstance(exc, DirectSendUnknownError)



@dataclass(frozen=True)
class SendPorts:
    bridge_factory: Callable
    bridge_close: Callable
    confirm_row: Callable
    require_pyautogui: Callable
    focus_kakao_window: Callable
    send_one: Callable
    send_scheduled_one: Callable
    return_to_recent_chat_top: Callable
    confirm_schedule_ready: Callable | None = None
    monotonic: Callable[[], float] = time.monotonic
    wait_for_cancel: Callable[[Any, float], bool] = wait_for_direct_cancel
    native_schedule_factory: Callable | None = None


class SendService:
    def __init__(self, ports: SendPorts):
        self.ports = ports

    def execute(self, request: SendRunRequest, context: SendRunContext) -> None:
        selected = request.selected
        total = len(selected)
        fatal_error = False
        has_results = False
        fallback_schedule = request.common_schedule
        mappings = dict(request.chat_mappings)
        blocked_names = {
            normalize_chat_match_name(name)
            for name in request.blocked_direct_names
            if normalize_chat_match_name(name)
        }

        def scheduled_for(row: MessageRow) -> datetime | None:
            if request.send_mode == "macro_retry":
                return None
            return schedule_datetime_for_row(row, fallback_schedule)

        direct_required = any(
            row_uses_direct_send(row, scheduled_for(row), request.direct_enabled)
            and normalize_chat_match_name(row.chat_name) not in blocked_names
            for _, row in selected
        )
        macro_required = selected_rows_require_macro(selected, fallback_schedule, request.direct_enabled)
        native_rows = [replace(row, scheduled_at=scheduled_for(row)) for _, row in selected
                       if scheduled_for(row) is not None] if callable(self.ports.native_schedule_factory) else []
        if native_rows:
            macro_required = any(scheduled_for(row) is None and not row_uses_direct_send(row, None, request.direct_enabled)
                                 for _, row in selected)
        native_schedule = None
        unresolved_schedule_keys = frozenset()
        accepted_schedule_keys = frozenset()
        pyautogui: Any = None
        direct_bridge: Any = None
        direct_bridge_error: str | None = None
        macro_setup_error: str | None = None
        direct_dispatch_count = 0
        direct_batch_size, direct_batch_pause = direct_send_batch_policy(request.config)
        attachment_snapshot = None
        attachment_run_id = uuid.uuid4().hex

        def release_direct_attachments() -> None:
            if attachment_snapshot is not None and direct_bridge is not None:
                direct_bridge.release_attachments(attachment_run_id=attachment_run_id)

        def close_active_direct_bridge() -> None:
            nonlocal direct_bridge
            if direct_bridge is not None:
                closing_bridge, direct_bridge = direct_bridge, None
                self.ports.bridge_close(closing_bridge)

        def send_direct_with_recovery(row: MessageRow, chat_id: str) -> Any:
            nonlocal direct_bridge
            for recovery in range(MAX_DIRECT_PREWRITE_RECOVERIES + 1):
                context.raise_if_cancelled()
                try:
                    if row.attachments:
                        return direct_bridge.send_attachments(
                            text=row.text, chat_id=chat_id,
                            attachments=attachment_snapshot.paths_for(row.attachments),
                            attachment_run_id=attachment_run_id,
                            **({"image_send_as": request.config["image_send_as"]}
                               if request.config.get("image_send_as", "photo") != "photo" else {}),
                        )
                    return direct_bridge.send_message(text=row.text, chat_id=chat_id)
                except DirectSendNotStartedError as exc:
                    # An error code alone is insufficient. This exception is
                    # created only from the bridge's explicit pre-WRITE proof.
                    # Never replay an ambiguous WRITE or partially sent files.
                    if not exc.retryable_prewrite or recovery == MAX_DIRECT_PREWRITE_RECOVERIES:
                        raise
                    attempt = recovery + 1
                    context.emit("status", f"직접 연결 복구 중 ({attempt}/{MAX_DIRECT_PREWRITE_RECOVERIES}) · 현재 행 미발송")
                    context.emit("log", ("연결 복구", f"발송 전 연결을 다시 준비합니다 ({attempt}/{MAX_DIRECT_PREWRITE_RECOVERIES}) · {redact_error(exc)}"))
                    try:
                        close_active_direct_bridge()
                    except Exception:
                        raise DirectSendNotStartedError(cause_code="bridge_close_failed") from None
                    deadline = self.ports.monotonic() + DIRECT_PREWRITE_RECOVERY_PAUSE_SECONDS
                    while True:
                        context.raise_if_cancelled()
                        remaining = deadline - self.ports.monotonic()
                        if remaining <= 0:
                            break
                        if self.ports.wait_for_cancel(context.cancel_event, remaining):
                            context.raise_if_cancelled()
                    context.raise_if_cancelled()
                    try:
                        direct_bridge = self.ports.bridge_factory(request.direct_config)
                    except Exception:
                        raise DirectSendNotStartedError(cause_code="bridge_start_failed") from None
                    context.raise_if_cancelled()
                    if row.attachments:
                        try:
                            direct_bridge.prepare_attachments(
                                attachments=attachment_snapshot.paths_for(row.attachments),
                                attachment_run_id=attachment_run_id,
                            )
                        except SendRunCancelled:
                            raise
                        except Exception:
                            # Preparation performs local file work only. A
                            # replacement child cannot have sent this row yet.
                            raise DirectSendNotStartedError(cause_code="attachment_prepare_failed") from None
            raise AssertionError("unreachable direct recovery state")

        def wait_for_direct_slot() -> None:
            if not direct_dispatch_count or direct_dispatch_count % direct_batch_size:
                context.raise_if_cancelled()
                return
            # Pause only before the next batch: never delay the first call or
            # add a trailing wait after the final recipient. An attachment
            # call counts as one row even when it sends several messages.
            deadline = self.ports.monotonic() + direct_batch_pause
            context.emit("status", f"직접 전송 {direct_dispatch_count}행 처리 · 다음 묶음 전 {direct_batch_pause:g}초 대기")
            while True:
                context.raise_if_cancelled()
                remaining = deadline - self.ports.monotonic()
                if remaining <= 0:
                    return
                if self.ports.wait_for_cancel(context.cancel_event, remaining):
                    context.raise_if_cancelled()

        def record_unattempted_after_direct_stop(pending: Iterable[tuple[int, MessageRow]]) -> None:
            for pending_index, pending_row in pending:
                pending_schedule = scheduled_for(pending_row)
                pending_direct = row_uses_direct_send(pending_row, pending_schedule, request.direct_enabled)
                context.add_result({
                    "row_index": pending_index,
                    "row": pending_row.row_number,
                    "chat_name": pending_row.chat_name,
                    "status": "stopped",
                    "detail": "앞선 직접 전송의 응답을 확인할 수 없거나 거절되어 배치를 중단했습니다. 이 행은 발송을 시도하지 않았습니다.",
                    "attachments": pending_row.attachment_count,
                    "scheduled_at": format_schedule_datetime(pending_schedule),
                    "transport": "scheduled" if pending_schedule is not None else ("direct" if pending_direct else "macro"),
                    "failure_stage": "direct_batch_not_attempted",
                    "retry_transport": "none",
                })
                context.emit("row", (pending_index, "미시도"))

        def record_direct_setup_failures() -> bool:
            if direct_bridge_error is None:
                return False
            recorded = False
            for row_index, row in selected:
                scheduled_at = scheduled_for(row)
                if not row_uses_direct_send(row, scheduled_at, request.direct_enabled):
                    continue
                if normalize_chat_match_name(row.chat_name) in blocked_names:
                    continue
                context.add_result(
                    {
                        "row_index": row_index,
                        "row": row.row_number,
                        "chat_name": row.chat_name,
                        "status": "error",
                        "detail": direct_bridge_error,
                        "attachments": row.attachment_count,
                        "scheduled_at": format_schedule_datetime(scheduled_at),
                        "transport": "direct",
                        "failure_stage": "direct",
                        "retry_transport": "macro",
                    }
                )
                context.emit("row", (row_index, "오류"))
                recorded = True
            return recorded

        if direct_required:
            try:
                direct_bridge = self.ports.bridge_factory(request.direct_config)
                context.register_finalizer(close_active_direct_bridge)
            except Exception as exc:
                safe_detail = redact_error(exc, fallback=type(exc).__name__)
                direct_bridge_error = f"직접 전송 연결 초기화 실패 ({safe_detail})"
                context.emit(
                    "log",
                    ("직접 전송", f"연결 초기화 실패 · 각 대상은 2차 화면 전송 대기로 표시합니다 ({safe_detail})."),
                )
        if macro_required:
            try:
                pyautogui = self.ports.require_pyautogui()
                pyautogui.PAUSE = 0.05
            except Exception as exc:
                if request.send_mode == "macro_retry":
                    macro_setup_error = f"2차 화면 전송 초기화 실패 ({type(exc).__name__})"
                    context.emit("log", ("2차 전송", macro_setup_error))
                else:
                    record_direct_setup_failures()
                    context.emit(
                        "error",
                        f"예약·화면 제어를 시작할 수 없습니다 ({type(exc).__name__}).",
                    )
                    raise

        for remaining in range(request.countdown, 0, -1):
            if context.cancel_requested:
                context.emit("log", ("중지", "카운트다운 중 작업이 취소되었습니다."))
                if request.send_mode == "macro_retry":
                    preserve_macro_retry_queue(
                        context,
                        selected,
                        detail="2차 화면 전송 카운트다운 중 사용자가 중지해 아직 시도하지 않았습니다.",
                    )
                context.raise_if_cancelled()
            context.emit("status", f"{remaining}초 후 시작")
            time.sleep(1)

        config = request.config
        attachment_bundles = [row.attachments for _, row in selected
                              if row.attachments
                              and row_uses_direct_send(row, scheduled_for(row), request.direct_enabled)
                              and normalize_chat_match_name(row.chat_name) not in blocked_names]
        if attachment_bundles and direct_bridge_error is None:
            context.raise_if_cancelled()
            attachment_snapshot = AttachmentRunSnapshot()
            context.register_finalizer(attachment_snapshot.close)
            context.register_finalizer(release_direct_attachments)
            context.emit("status", "직접 전송 첨부 파일 준비 중")
            try:
                attachment_snapshot.prepare(attachment_bundles, cancel_check=context.raise_if_cancelled)
                for bundle in attachment_snapshot.bundles:
                    context.raise_if_cancelled()
                    direct_bridge.prepare_attachments(attachments=bundle, attachment_run_id=attachment_run_id)
            except SendRunCancelled:
                raise
            except Exception as exc:
                context.emit("error", f"첨부 파일 준비에서 중단했습니다. 발송은 시작하지 않았습니다.\n\n{redact_error(exc)}")
                raise
        if macro_required and macro_setup_error is None:
            try:
                title = self.ports.focus_kakao_window(pyautogui, list(config.get("app_titles") or []))
                if title:
                    context.emit("status", "카카오톡 활성화 완료")
                elif config.get("require_main_window_focus", True):
                    warning = "카카오톡 메인 창을 자동으로 찾지 못했습니다. 메인 창을 열어둔 뒤 다시 실행해 주세요."
                    if request.send_mode == "macro_retry":
                        macro_setup_error = warning
                        context.emit("log", ("2차 전송", warning))
                    else:
                        context.emit("error", format_preflight_warning(warning))
                        raise FocusSafetyError(warning)
                else:
                    context.emit("log", ("주의", "카카오톡 창을 자동으로 찾지 못했습니다. 현재 포커스/화면 기준으로 진행합니다."))
            except Exception as exc:
                if request.send_mode == "macro_retry":
                    macro_setup_error = f"2차 화면 전송용 카카오톡 활성화 실패 ({type(exc).__name__})"
                    context.emit("log", ("2차 전송", macro_setup_error))
                else:
                    record_direct_setup_failures()
                    raise

        options = SimpleNamespace(
            assume_focused=True,
            delay_scale=request.delay_scale,
            paste_only=False,
            no_close_chat=False,
            cancel_event=context.cancel_event,
            confirm_schedule_ready=(
                lambda row, scheduled_at: self.ports.confirm_schedule_ready(row, scheduled_at, context)
            ) if callable(self.ports.confirm_schedule_ready) else None,
        )

        if native_rows:
            native_schedule = self.ports.native_schedule_factory(options, context)
            context.register_finalizer(native_schedule.close)
            context.emit('status', '카카오톡 예약 슬롯·이전 등록 기록 확인 중')
            try:
                native_schedule.prepare_batch(native_rows)
                unresolved = getattr(native_schedule, 'unresolved_keys', frozenset())
                if type(unresolved) in (set, frozenset) and all(isinstance(key, str) for key in unresolved):
                    unresolved_schedule_keys = frozenset(unresolved)
                accepted = getattr(native_schedule, 'accepted_keys', frozenset())
                if type(accepted) in (set, frozenset) and all(isinstance(key, str) for key in accepted):
                    accepted_schedule_keys = frozenset(accepted)
            except Exception as error:
                context.emit('error', f'예약 사전 점검에서 중단했습니다.\n\n{error}')
                raise

        def record_stopped_result(
            row_index: int,
            row: MessageRow,
            scheduled_at_text: str,
        ) -> None:
            context.add_result(
                {
                    "row_index": row_index,
                    "row": row.row_number,
                    "chat_name": row.chat_name,
                    "status": "stopped",
                    "detail": "Stopped by user",
                    "attachments": row.attachment_count,
                    "scheduled_at": scheduled_at_text,
                }
            )
            context.emit("row", (row_index, "중지"))

        for done_count, (row_index, row) in enumerate(selected, start=1):
            scheduled_at = scheduled_for(row)
            scheduled_at_text = format_schedule_datetime(scheduled_at)
            use_direct = row_uses_direct_send(row, scheduled_at, request.direct_enabled)
            reuse_schedule_receipt = (scheduled_at is not None
                and reservation_key(replace(row, scheduled_at=scheduled_at)) in accepted_schedule_keys)
            if context.cancel_requested:
                if request.send_mode == "macro_retry":
                    preserve_macro_retry_queue(
                        context,
                        selected[done_count - 1 :],
                        detail="2차 화면 전송 전에 사용자가 중지해 아직 시도하지 않았습니다.",
                    )
                else:
                    record_stopped_result(row_index, row, scheduled_at_text)
                has_results = True
                break

            if use_direct and normalize_chat_match_name(row.chat_name) in blocked_names:
                context.add_result(
                    {
                        "row_index": row_index,
                        "row": row.row_number,
                        "chat_name": row.chat_name,
                        "status": "error",
                        "detail": "자동·수동 매칭 후에도 안전한 chat ID를 확인하지 못해 발송에서 제외했습니다.",
                        "attachments": row.attachment_count,
                        "scheduled_at": scheduled_at_text,
                        "transport": "excluded",
                        "failure_stage": "mapping",
                        "retry_transport": "none",
                    }
                )
                has_results = True
                context.emit("row", (row_index, "매칭 실패"))
                context.emit("log", ("매칭 실패", f"행 {row.row_number} | {row.chat_name} | 발송 제외"))
                percent = int(done_count / total * 100)
                context.emit("progress", percent)
                context.emit("status", f"매칭 실패 제외 {percent}%")
                continue

            if scheduled_at is not None and reservation_key(replace(row, scheduled_at=scheduled_at)) in unresolved_schedule_keys:
                detail = f'이전 제출 결과 확인 필요 · {row.chat_name} · {scheduled_at_text}. 이 행은 재등록하지 않고 보류하며 다른 미등록 행을 계속 처리합니다.'
                context.add_result({'row_index':row_index, 'row':row.row_number, 'chat_name':row.chat_name,
                    'status':'unknown', 'detail':detail, 'attachments':row.attachment_count,
                    'scheduled_at':scheduled_at_text, 'transport':'scheduled',
                    'failure_stage':'schedule_journal', 'retry_transport':'none'})
                has_results = True
                context.emit('row', (row_index, '예약 확인 필요'))
                context.emit('log', ('이전 예약 보류', f'행 {row.row_number} | {row.chat_name} | {scheduled_at_text} | 재등록 없이 다음 행 진행'))
                context.emit('progress', int(done_count / total * 100))
                continue

            decision = self.ports.confirm_row(
                row,
                done_count,
                total,
                scheduled_at,
                request.confirm_each,
                context,
            )
            if decision == "stop" or context.cancel_requested:
                context.cancel_event.set()
                if request.send_mode == "macro_retry":
                    preserve_macro_retry_queue(
                        context,
                        selected[done_count - 1 :],
                        detail="2차 화면 전송 확인 중 사용자가 중지해 아직 시도하지 않았습니다.",
                    )
                else:
                    record_stopped_result(row_index, row, scheduled_at_text)
                has_results = True
                break
            if decision == "skip":
                context.add_result(
                    {
                        "row_index": row_index,
                        "row": row.row_number,
                        "chat_name": row.chat_name,
                        "status": "skipped",
                        "detail": "Skipped by user",
                        "attachments": row.attachment_count,
                        "scheduled_at": scheduled_at_text,
                    }
                )
                has_results = True
                context.emit("row", (row_index, "건너뜀"))
                context.emit("progress", done_count / total * 100)
                continue

            percent = int((done_count - 1) / total * 100)
            if use_direct:
                action_label = '직접 전송 중'
            elif request.send_mode == 'macro_retry':
                action_label = '2차 화면 전송 중'
            elif reuse_schedule_receipt:
                action_label = '기존 예약 확인 중'
            elif scheduled_at is not None:
                action_label = '카카오 예약 등록 중'
            else:
                action_label = '화면 발송 중'
            context.emit("status", f"{action_label} {percent}%")
            context.emit("row", (row_index, "진행 중"))
            context.emit("log", ("진행", f"{action_label}, 진행률 {percent}%"))
            # This is the final cooperative boundary before an irreversible
            # direct send, reservation registration, or PC macro starts.
            if context.cancel_requested:
                if request.send_mode == "macro_retry":
                    preserve_macro_retry_queue(
                        context,
                        selected[done_count - 1 :],
                        detail="2차 화면 전송 직전 사용자가 중지해 아직 시도하지 않았습니다.",
                    )
                else:
                    record_stopped_result(row_index, row, scheduled_at_text)
                has_results = True
                break
            direct_dispatch_started = False
            try:
                if use_direct:
                    chat_id = resolve_chat_mapping(mappings, row.chat_name)
                    if not chat_id:
                        raise RuntimeError(f"'{row.chat_name}'의 chat ID 매칭이 없습니다.")
                    if direct_bridge_error is not None or direct_bridge is None:
                        raise RuntimeError(direct_bridge_error or "직접 전송 연결이 준비되지 않았습니다.")
                    wait_for_direct_slot()
                    context.mark_attempt(row_index, "direct")
                    attachment_summary = {}
                    direct_dispatch_count += 1
                    direct_dispatch_started = True
                    result = send_direct_with_recovery(row, chat_id)
                    if row.attachments:
                        attachment_summary = direct_attachment_result_summary(result, row, chat_id)
                    else:
                        result = validate_direct_send_receipt(result, chat_id)
                    status = "direct_sent"
                    display_status = "직접 전송"
                    log_id = (
                        attachment_summary["direct_log_ids"].split(",")[-1] or None
                        if attachment_summary else (result.get("log_id") if isinstance(result, dict) else None)
                    )
                    detail = f"chat_id={chat_id}" + (f" · log_id={log_id}" if log_id else "")
                    if attachment_summary:
                        detail += f" · 첨부 {row.attachment_count}개 · 메시지 {attachment_summary['direct_receipt_count']}건 확인"
                elif scheduled_at is not None:
                    if reuse_schedule_receipt:
                        registration = native_schedule.result(replace(row, scheduled_at=scheduled_at))
                    else:
                        context.mark_attempt(row_index, "scheduled")
                        registration = (native_schedule.register(replace(row, scheduled_at=scheduled_at)) if native_schedule is not None
                                        else self.ports.send_scheduled_one(pyautogui, config, row, options, scheduled_at))
                    verified = type(registration) is ScheduleRegistrationResult and registration.verified is True
                    acknowledged = native_schedule is not None and type(registration) is ScheduleRegistrationResult and registration.acknowledged is True
                    status = "scheduled" if verified else ("schedule_acknowledged" if acknowledged else "unknown")
                    display_status = "예약" if verified else ("예약 접수" if acknowledged else "예약 확인 필요")
                    detail = registration.detail if type(registration) is ScheduleRegistrationResult and isinstance(registration.detail, str) else "예약 등록 여부를 확인하지 못했습니다. 카카오톡 예약 목록에서 대상·본문·일시를 확인하세요."
                else:
                    if request.send_mode == "macro_retry" and macro_setup_error is not None:
                        raise RuntimeError(macro_setup_error)
                    context.mark_attempt(row_index, "macro_retry" if request.send_mode == "macro_retry" else "macro")
                    self.ports.send_one(pyautogui, config, row, options)
                    status = "pasted" if options.paste_only else "sent"
                    display_status = "붙여넣음" if options.paste_only else "전송"
                    detail = ""
                result_item = {
                    "row_index": row_index,
                    "row": row.row_number,
                    "chat_name": row.chat_name,
                    "status": status,
                    "detail": detail,
                    "attachments": row.attachment_count,
                    "scheduled_at": scheduled_at_text,
                    "transport": "scheduled" if scheduled_at is not None else ("direct" if use_direct else ("macro_retry" if request.send_mode == "macro_retry" else "macro")),
                    "chat_id": chat_id if use_direct else "",
                    "log_id": log_id if use_direct else "",
                }
                if scheduled_at is not None:
                    result_item['schedule_already_registered'] = reuse_schedule_receipt and status != 'unknown'
                    result_item["failure_stage"] = "schedule_verification" if status == "unknown" else ""
                    result_item["retry_transport"] = "none"
                    if result_item['schedule_already_registered']:
                        context.emit('log', ('기존 예약 유지', f'행 {row.row_number} | {row.chat_name} | {scheduled_at_text} · 재등록 없이 기존 접수 기록 사용'))
                elif use_direct and row.attachments:
                    result_item.update(attachment_summary)
                    result_item["retry_transport"] = "none"
                context.add_result(result_item)
                has_results = True
                context.emit("row", (row_index, display_status))
                if (scheduled_at is not None and type(registration) is ScheduleRegistrationResult
                        and registration.cleanup_error):
                    fatal_error = True
                    context.emit("log", ("예약 후 창 정리 중단", registration.cleanup_error))
                    context.emit("error", "예약 접수는 완료했지만 대화창 정리에서 중단했습니다.\n\n"
                                 +registration.cleanup_error+"\n\n이미 접수된 이 행은 다시 등록하지 않습니다. "
                                 "대상 대화창을 닫은 뒤 나머지 미시도 행을 이어서 진행하세요.")
                    break
                if scheduled_at is not None and status == "unknown":
                    fatal_error = True
                    context.emit("log", ("예약 확인 필요", "예약 목록 확인 전에는 다시 등록하지 마세요. 나머지 미시도 행은 [실행 기록]에서 이어서 진행할 수 있습니다."))
                    break
            except RunJournalError:
                # A missing durable result after dispatch must remain unknown.
                # Never turn a storage failure into a retryable transport error.
                raise
            except (MacroSendCancelled, SendRunCancelled, PcUiLeaseCancelled):
                context.cancel_event.set()
                if request.send_mode == "macro_retry":
                    preserve_macro_retry_queue(context, selected[done_count - 1:], detail="발송 전 중단되어 아직 시도하지 않았습니다.")
                else:
                    record_stopped_result(row_index, row, scheduled_at_text)
                has_results = True
                break
            except Exception as exc:
                direct_not_started = use_direct and isinstance(exc, DirectSendNotStartedError)
                unknown_outcome = bool(
                    isinstance(exc, MacroSendUnknownError)
                    or (use_direct and is_unknown_direct_send_error(exc))
                    or (direct_dispatch_started and not isinstance(exc, DirectSendError))
                )
                detail = (
                    redact_error(exc, fallback=type(exc).__name__)
                    if use_direct
                    else str(exc)
                )
                # Only the transport's explicit pre-submit evidence can make
                # a reservation resumable; generic/cleanup failures cannot.
                schedule_not_submitted = scheduled_at is not None and isinstance(exc, ScheduleNotSubmittedError)
                result_status = "unknown" if unknown_outcome else ("stopped" if schedule_not_submitted or direct_not_started else "error")
                display_status = "확인 필요" if unknown_outcome else ("미시도" if direct_not_started else ("예약 중단" if schedule_not_submitted else "오류"))
                if schedule_not_submitted and native_schedule is None:
                    context.cancel_event.set()
                retryable = bool(
                    not unknown_outcome
                    and (use_direct or request.send_mode == "macro_retry")
                )
                context.add_result(
                    {
                        "row_index": row_index,
                        "row": row.row_number,
                        "chat_name": row.chat_name,
                        "status": result_status,
                        "detail": detail,
                        "attachments": row.attachment_count,
                        "scheduled_at": scheduled_at_text,
                        "transport": "scheduled" if scheduled_at is not None else ("direct" if use_direct else ("macro_retry" if request.send_mode == "macro_retry" else "macro")),
                        "failure_stage": "direct_prewrite" if direct_not_started else ("direct" if use_direct else getattr(exc, "stage", "schedule_prepare" if schedule_not_submitted else ("macro_retry" if request.send_mode == "macro_retry" else "macro"))),
                        "retry_transport": "direct" if direct_not_started else ("macro" if retryable else "none"),
                    }
                )
                has_results = True
                context.emit("row", (row_index, display_status))
                category = "확인 필요" if unknown_outcome else "오류"
                context.emit("log", (category, f"행 {row.row_number} | {row.chat_name} | {detail}"))
                if direct_dispatch_started:
                    fatal_error = True
                    record_unattempted_after_direct_stop(selected[done_count:])
                    stop_detail = ("연결을 복구하지 못해 현재 행과 후속 행은 발송하지 않았습니다. [실행 기록]에서 미시도 행을 이어서 진행할 수 있습니다."
                                   if direct_not_started else
                                   "후속 행은 발송하지 않았습니다. 현재 행의 결과를 확인한 뒤 [실행 기록]에서 미시도 행을 확인하세요.")
                    context.emit("log", ("직접 전송 중단", stop_detail))
                    context.emit("status", "직접 전송 중단 · 후속 행 미시도")
                    break
                if is_fatal_send_error(exc) or scheduled_at is not None:
                    fatal_error = True
                    next_action = (
                        "- 이 행은 발송 여부를 확인할 때까지 다시 보내지 마세요. 카카오톡 대화방에서 먼저 확인하세요.\n"
                        "- 나머지 미시도 행은 [실행 기록]에서 불러올 수 있습니다."
                        if unknown_outcome else
                        "- 열린 카카오톡 창을 확인하고 [화면 좌표 설정]을 다시 실행하세요.\n"
                        "- 미시도 행은 [실행 기록], 명시적인 직접 실패 행은 [직접 실패 2차 전송]에서 이어서 진행하세요."
                    )
                    if scheduled_at is not None:
                        next_action = (
                            "- 카카오톡 예약 목록에서 대상·본문·일시를 확인하기 전에는 다시 등록하지 마세요.\n"
                            if unknown_outcome else
                            "- 열린 카카오톡 예약창과 오류 내용을 확인하세요. 등록 시각은 현재보다 최소 5분 이후여야 합니다.\n"
                        ) + "- 나머지 미시도 행은 [실행 기록]에서 이어서 진행할 수 있습니다."
                    error_message = (
                        "안전 중단\n\n"
                        f"{detail}\n\n"
                        "다음 행동\n" + next_action
                    )
                    context.emit("error", error_message)
                    if request.send_mode == "macro_retry":
                        preserve_macro_retry_queue(
                            context,
                            selected[done_count:],
                            detail="앞선 행의 화면 안전 오류로 중단되어 아직 시도하지 않았습니다.",
                        )
                    break
                if not request.continue_on_error and not use_direct and request.send_mode != "macro_retry":
                    break
            percent = int(done_count / total * 100)
            context.emit("progress", percent)
            context.emit("status", f"{action_label} {percent}%")

        if (
            macro_required
            and pyautogui is not None
            and macro_setup_error is None
            and has_results
            and not context.cancel_requested
            and not fatal_error
        ):
            try:
                self.ports.return_to_recent_chat_top(pyautogui, config, options)
                context.emit("status", "최근 대화 목록 정리 완료")
            except Exception as exc:
                context.emit("log", ("정리", f"최근 대화 목록 맨 위 이동 실패: {exc}"))
