"""Hub matching responsibilities; app state remains on the Tk owner thread."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any
import tkinter as tk
from tkinter import messagebox, ttk
from kakao_send_service import row_uses_direct_send
from kakao_mapping_policy import ManualChatCandidate, normalize_chat_match_name, confirmed_peer_name, confirmed_chat_label, is_confirmed_mapping_label, is_one_to_one_direct_chat, positive_numeric_chat_id, merge_chat_snapshots, unique_blank_memo_chat_id, reconcile_chat_mappings, manual_chat_candidates, apply_manual_chat_mapping, unverified_alias_mapping_names, resolve_chat_mapping, preserve_guarded_mapping_labels
from kakao_chat_catalog import MAX_PC_CHAT_CATALOG_IDS, discover_pc_chat_catalog_ids
from kakao_pc_lease import pc_kakao_ui_lease
from kakao_diagnostics import connection_guidance
from kakao_direct import DirectBridgeError, DirectKakaoConfig, KakaoDirectBridge, MAX_CHAT_LOOKUP_IDS, normalize_guarded_mapping_ids, normalize_mapping_labels, normalize_mappings, redact_error
from kakao_sender import MessageRow, deep_merge, close_chat_after_send, focus_kakao_window_via_uia, find_kakao_windows, get_window_rect, chat_title_matches, open_chat, require_pyautogui, return_to_recent_chat_top, normalize_runtime_config, schedule_datetime_for_row
from recover_chat_mappings import merge_sanitized_chat_list
try:
    from kakao_pc_chat_map import discover_pc_chat_map
except ImportError:
    discover_pc_chat_map = None
from kakao_gui_constants import GENERIC_KAKAO_WINDOW_TITLES, PC_CATALOG_REFRESH_REASONS, SCREEN_USE_ACK_TIMEOUT_SECONDS

@dataclass(frozen=True, slots=True)
class AutoMatchFailure:
    """Credential-safe diagnostic passed from the query worker to Tk."""

    code: str
    cause: str
    next_action: str


@dataclass(frozen=True, slots=True)
class AutoMatchMetrics:
    """Monotonic timings and counts only; never recipient or auth data."""

    mode: str
    target_count: int
    lookup_ids: int
    catalog_count: int
    connect_seconds: float
    list_seconds: float
    lookup_seconds: float
    close_seconds: float
    started_at: float
    worker_seconds: float


def saved_target_lookup_ids(
    chat_names: Iterable[str],
    mappings: Mapping[Any, Any],
    labels: Mapping[Any, Any],
) -> tuple[str, ...]:
    """Select existing owners only. New names still need a fresh full catalog."""

    try:
        safe_mappings = normalize_mappings(mappings)
        safe_labels = normalize_mapping_labels(labels)
    except (TypeError, ValueError):
        return ()
    ids: list[str] = []
    for name in chat_names:
        chat_id = positive_numeric_chat_id(resolve_chat_mapping(safe_mappings, name))
        if not chat_id or not is_confirmed_mapping_label(chat_id, safe_labels.get(chat_id)):
            return ()
        ids.append(chat_id)
    return tuple(dict.fromkeys(ids))


def saved_target_lookup_is_complete(
    chat_names: tuple[str, ...],
    mappings: Mapping[Any, Any],
    labels: Mapping[Any, Any],
    chats: list[dict[str, Any]],
) -> bool:
    """Require fresh, unique, unchanged 1:1 evidence for every saved target.

    A partial catalog cannot discover a new name or prove its uniqueness.
    Never merge old snapshot rows into this evidence or create a new owner.
    """

    ids = saved_target_lookup_ids(chat_names, mappings, labels)
    if not ids or len(chats) != len(ids):
        return False
    expected_ids = set(ids)
    rows_by_id: dict[str, Mapping[str, Any]] = {}
    for chat in chats:
        if not isinstance(chat, Mapping):
            return False
        chat_id = positive_numeric_chat_id(chat.get("chat_id"))
        if (
            chat_id not in expected_ids
            or chat_id in rows_by_id
            or not is_one_to_one_direct_chat(chat)
            or not confirmed_peer_name(chat)
            or normalize_chat_match_name(confirmed_chat_label(dict(chat)))
            != normalize_chat_match_name(labels.get(chat_id))
        ):
            return False
        rows_by_id[chat_id] = chat
    reconciled = reconcile_chat_mappings(mappings, labels, chat_names, chats)
    return bool(
        not reconciled.ambiguous
        and not reconciled.unmatched
        and not reconciled.invalidated
        and len(reconciled.matched) == len(chat_names)
        and all(
            resolve_chat_mapping(reconciled.mappings, name)
            == resolve_chat_mapping(dict(mappings), name)
            for name in chat_names
        )
    )


@dataclass(frozen=True, slots=True)
class PcChatFallbackResult:
    """Safe result of resolving only the remaining names through PC geometry."""

    mappings: dict[str, str]
    unresolved: tuple[str, ...]
    mapping_labels: dict[str, str] = field(default_factory=dict)
    retry_safe: bool = True


def unique_chat_names(rows: list[MessageRow] | tuple[MessageRow, ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name = row.chat_name.strip()
        normalized = normalize_chat_match_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        names.append(name)
    return tuple(names)


def direct_chat_names_for_rows(
    rows: list[MessageRow] | tuple[MessageRow, ...],
    *,
    direct_enabled: bool,
    fallback_schedule: datetime | None = None,
) -> tuple[str, ...]:
    """Return rows eligible for immediate direct text or attachment delivery.

    File loading and other read-only reconciliation may query the tablet chat
    list, but must not try to resolve scheduled rows through the
    PC window.  Keep this filter beside ``row_uses_direct_send`` so the GUI
    uses the same routing contract as the send worker.
    """

    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not row_uses_direct_send(row, schedule_datetime_for_row(row, fallback_schedule), direct_enabled):
            continue
        name = row.chat_name.strip()
        normalized = normalize_chat_match_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        names.append(name)
    return tuple(names)


def is_exact_pc_geometry_resolution(resolution: Any) -> bool:
    """Accept only one exact DB rectangle match, never a tolerance match."""

    if not bool(getattr(resolution, "resolved", False)):
        return False
    status = getattr(resolution, "status", None)
    match_kind = getattr(status, "match_kind", None)
    match_kind_value = getattr(match_kind, "value", match_kind)
    try:
        candidate_count = int(getattr(status, "candidate_count"))
    except (TypeError, ValueError, OverflowError):
        return False
    return str(match_kind_value or "").strip().casefold() == "exact" and candidate_count == 1


def discover_open_pc_chat_ids(
    tablet_chats: Iterable[Mapping[str, Any]],
    *,
    provider_discovery: Callable[[Any], Any] | None = None,
    window_finder: Callable[[], Iterable[Any]] | None = None,
) -> tuple[str, ...]:
    """Return exact local-window chat-ID candidates absent from the list.

    The function only enumerates already-visible KakaoTalk windows and reads
    the local ``chatWindowUi`` metadata provider.  It never focuses, opens,
    closes, clicks, or types into a window.  Candidates are exact-rectangle
    hints only; callers must still resolve each ID through current server
    CHATINFO plus a stable member snapshot before mapping or sending.
    """

    tablet_ids: list[str] = []
    seen_tablet_ids: set[str] = set()
    for chat in tablet_chats:
        if not isinstance(chat, Mapping):
            continue
        chat_id = positive_numeric_chat_id(chat.get("chat_id"))
        if not chat_id or chat_id in seen_tablet_ids:
            continue
        seen_tablet_ids.add(chat_id)
        tablet_ids.append(chat_id)
    if not tablet_ids:
        return ()

    discover = provider_discovery if provider_discovery is not None else discover_pc_chat_map
    if discover is None:
        return ()
    try:
        discovery = discover(tuple(tablet_ids))
        provider = getattr(discovery, "provider", None)
        resolver = getattr(provider, "resolve_window_rect_candidate", None)
    except Exception:
        return ()
    if not callable(resolver):
        return ()

    finder = window_finder if window_finder is not None else find_kakao_windows
    try:
        windows = tuple(finder())
    except Exception:
        return ()

    candidates: list[str] = []
    seen_candidates: set[str] = set()
    for window in windows:
        try:
            _handle, _window_class, raw_title, raw_rect = window
            title = str(raw_title or "").strip()
            if title in GENERIC_KAKAO_WINDOW_TITLES:
                continue
            if isinstance(raw_rect, (str, bytes)) or len(raw_rect) != 4:
                continue
            left, top, right, bottom = raw_rect
            if any(isinstance(value, bool) for value in (left, top, right, bottom)):
                continue
            left, top, right, bottom = (int(value) for value in (left, top, right, bottom))
            width = right - left
            height = bottom - top
            if width <= 0 or height <= 0:
                continue
            resolution = resolver(left, top, width, height)
        except Exception:
            continue
        if not is_exact_pc_geometry_resolution(resolution):
            continue
        chat_id = positive_numeric_chat_id(getattr(resolution, "chat_id", None))
        if (
            not chat_id
            or chat_id in seen_tablet_ids
            or chat_id in seen_candidates
        ):
            continue
        seen_candidates.add(chat_id)
        candidates.append(chat_id)
        if len(candidates) >= MAX_CHAT_LOOKUP_IDS:
            break
    return tuple(candidates)


def resolve_pc_chat_ids_via_window(
    chat_names: tuple[str, ...] | list[str],
    tablet_chats: list[dict[str, Any]],
    config: Mapping[str, Any],
    *,
    pyautogui_module: Any | None = None,
    provider_discovery: Callable[[Any], Any] | None = None,
    reserved_chat_ids: Iterable[Any] = (),
    cancel_event: threading.Event | None = None,
    prepare_friend_tab: bool = False,
    friend_tab_focus: Callable[[list[str]], str | None] | None = None,
    screen_use_callback: Callable[[bool, threading.Event, threading.Event | None], None] | None = None,
) -> PcChatFallbackResult:
    """Resolve unresolved names by reading existing PC chat-window geometry.

    It discovers the read-only PC provider once, opens each remaining chat
    with an empty ``MessageRow.text``, reads the current window rectangle, and
    closes the exact window again.  It never writes geometry, sends a message,
    accepts a tolerance match, or accepts a collision.  An ordinary room is
    eligible only when the tablet independently confirms a two-person
    ``DirectChat`` and one peer identity.  The singleton blank ``MemoChat`` is
    retained as the self-chat exception.  Any unavailable dependency or
    cleanup failure leaves the target unresolved; a cleanup failure also stops
    the sequence before another PC room can be opened.

    ``screen_use_callback`` is notified only after the PC UI lock, provider,
    and pyautogui gates have all succeeded and immediately before the first
    focus/open operation.  It receives ``(active, cancel_event,
    acknowledgement)``.  The acknowledgement is supplied only for the
    active transition; the resolver waits for it before touching the PC UI so
    the Tk side can paint its warning first.  The inactive transition is
    emitted in a guaranteed ``finally`` after the current-room cleanup and
    safe-list reset have completed.  The callback is never invoked for the
    tablet/server-only list path because that path never calls this resolver.
    """

    names: list[str] = []
    seen_names: set[str] = set()
    for raw_name in chat_names:
        name = str(raw_name).strip()
        normalized = normalize_chat_match_name(name)
        if normalized and normalized not in seen_names:
            seen_names.add(normalized)
            names.append(name)
    if not names:
        return PcChatFallbackResult({}, ())

    search_box = config.get("search_box") if isinstance(config, Mapping) else None
    try:
        x = int(float(search_box.get("x", 0))) if isinstance(search_box, Mapping) else 0
        y = int(float(search_box.get("y", 0))) if isinstance(search_box, Mapping) else 0
    except (TypeError, ValueError, OverflowError):
        x = y = 0
    if x <= 0 or y <= 0:
        return PcChatFallbackResult({}, tuple(names))

    tablet_by_id = {
        str(chat.get("chat_id") or "").strip(): chat
        for chat in tablet_chats
        if str(chat.get("chat_id") or "").strip()
    }
    if not tablet_by_id:
        return PcChatFallbackResult({}, tuple(names))

    memo_chat_id = unique_blank_memo_chat_id(tablet_chats)
    eligible_tablet_ids = {
        chat_id
        for chat_id, chat in tablet_by_id.items()
        if is_one_to_one_direct_chat(chat) and confirmed_peer_name(chat)
    }
    if memo_chat_id:
        eligible_tablet_ids.add(memo_chat_id)
    if not eligible_tablet_ids:
        return PcChatFallbackResult({}, tuple(names))

    discover = provider_discovery if provider_discovery is not None else discover_pc_chat_map
    if discover is None:
        return PcChatFallbackResult({}, tuple(names))
    try:
        discovery = discover(tuple(tablet_by_id))
    except Exception:
        return PcChatFallbackResult({}, tuple(names))
    provider = getattr(discovery, "provider", None)
    if provider is None:
        return PcChatFallbackResult({}, tuple(names))

    # Serialize all PC KakaoTalk UI operations with send_worker.  Discovery
    # above is read-only and intentionally remains outside the lock; waiting
    # for the lock is only needed once this fallback is ready to touch a
    # window.  A cancellation that arrives while waiting is observed before
    # pyautogui/open_chat is called.
    operation_cancel_event = cancel_event if cancel_event is not None else threading.Event()
    screen_callback = screen_use_callback

    def notify_screen_state(active: bool) -> bool:
        """Notify the optional observer without ever touching Tk directly."""

        if screen_callback is None:
            return True
        acknowledgement = threading.Event() if active else None
        screen_callback(active, operation_cancel_event, acknowledgement)
        if active and acknowledgement is not None:
            if not acknowledgement.wait(timeout=SCREEN_USE_ACK_TIMEOUT_SECONDS):
                # Never start PC input when the GUI could not paint and
                # acknowledge the operator warning.  Publish the matching
                # inactive event so a delayed queue drain cannot leave a
                # stale warning token behind.
                operation_cancel_event.set()
                screen_callback(False, operation_cancel_event, None)
                return False
        return True

    with pc_kakao_ui_lease(cancel_event=operation_cancel_event):
        if operation_cancel_event.is_set():
            return PcChatFallbackResult({}, tuple(names))
        if pyautogui_module is None:
            try:
                pyautogui_module = require_pyautogui()
            except Exception:
                return PcChatFallbackResult({}, tuple(names))

        # Keep open_chat's focus/handle safety checks, then verify the returned
        # title ourselves so even a mismatch can be closed in the finally
        # block before any geometry result is accepted.
        fallback_config = dict(config)
        fallback_config["verify_chat_title"] = False
        # The operator may move or resize KakaoTalk after calibration.  The
        # saved search-box offset then points outside the current main window.
        # This read-only resolver uses KakaoTalk's focused-window Ctrl+F entry
        # instead; normal send paths keep their configured coordinate flow.
        fallback_config["open_search_shortcut"] = ["ctrl", "f"]
        args = SimpleNamespace(
            assume_focused=True,
            delay_scale=max(0.0, float(fallback_config.get("delay_scale", 1.0) or 1.0)),
            paste_only=False,
            no_close_chat=False,
        )
        resolved: dict[str, str] = {}
        resolved_labels: dict[str, str] = {}
        used_ids: dict[str, str] = {
            str(chat_id).strip(): "existing"
            for chat_id in reserved_chat_ids
            if str(chat_id).strip()
        }
        unresolved: list[str] = []
        retry_safe = True
        focus_for_friend_tab = friend_tab_focus or focus_kakao_window_via_uia

        # The lock/provider/pyautogui gates above are complete.  From this
        # point until the final safe-list reset the resolver may focus/open a
        # KakaoTalk room, so publish the active state before the first such
        # operation.  The matching inactive notification is sent below only
        # after the final room cleanup has run.
        if operation_cancel_event.is_set():
            return PcChatFallbackResult({}, tuple(names))
        if not notify_screen_state(True):
            return PcChatFallbackResult({}, tuple(names))

        try:
            for index, name in enumerate(names, start=1):
                # Cancellation is checked only between targets.  If a cancellation
                # arrives while the current target is open, its finally block
                # still closes that exact window before returning.
                if operation_cancel_event.is_set():
                    unresolved.extend(names[index - 1 :])
                    break
                context: Any = None
                resolved_id: str | None = None
                cleanup_ok = True
                try:
                    if prepare_friend_tab:
                        app_titles = list(fallback_config.get("app_titles") or [])
                        if not focus_for_friend_tab(app_titles):
                            unresolved.append(name)
                            continue
                        pyautogui_module.hotkey("ctrl", "1")
                        time.sleep(0.5)
                    # Shift+Esc may arrive while the friends tab is being
                    # prepared.  Do not open a new room after that request.
                    if operation_cancel_event.is_set():
                        unresolved.extend(names[index - 1 :])
                        break
                    # Empty text is the guardrail that makes this an open/inspect
                    # operation rather than a send operation.
                    context = open_chat(
                        pyautogui_module,
                        fallback_config,
                        MessageRow(index, name, ""),
                        args,
                    )
                    # Keep the explicit title guard even though ``open_chat`` is
                    # run with its internal verification disabled.  This lets the
                    # finally block close a wrongly opened child window safely
                    # before the fallback considers any geometry result.
                    if chat_title_matches(name, str(getattr(context, "chat_title", "") or "")):
                        rect = get_window_rect(getattr(context, "chat_handle", None))
                        if rect is not None:
                            left, top, right, bottom = (int(value) for value in rect)
                            width = right - left
                            height = bottom - top
                            resolution = provider.resolve_window_rect(left, top, width, height)
                            candidate = str(getattr(resolution, "chat_id", "") or "").strip()
                            pc_label = str(getattr(context, "chat_title", "") or "").strip()
                            tablet_chat = tablet_by_id.get(candidate)
                            is_verified_direct = bool(
                                tablet_chat is not None
                                and is_one_to_one_direct_chat(tablet_chat)
                                and confirmed_peer_name(tablet_chat)
                            )
                            is_verified_memo = bool(
                                tablet_chat is not None
                                and candidate == memo_chat_id
                                and not confirmed_chat_label(tablet_chat)
                            )
                            if (
                                is_exact_pc_geometry_resolution(resolution)
                                and tablet_chat is not None
                                and (is_verified_direct or is_verified_memo)
                                and pc_label
                                and candidate not in used_ids
                            ):
                                resolved_id = candidate
                except Exception:
                    resolved_id = None
                finally:
                    if context is not None:
                        try:
                            close_chat_after_send(
                                pyautogui_module,
                                fallback_config,
                                args,
                                getattr(context, "main_handle", None),
                                getattr(context, "chat_handle", None),
                                str(getattr(context, "chat_title", "") or ""),
                            )
                        except Exception:
                            cleanup_ok = False
                            retry_safe = False
                    if cleanup_ok:
                        try:
                            # A closed chat can leave KakaoTalk's search result
                            # selection active.  Starting the next lookup from that
                            # state makes Enter alternate between the intended room
                            # and the bottom "댓글 모아보기" item.  Restore an
                            # idempotent main-list/search state after every target,
                            # including failures that never returned a context.
                            return_to_recent_chat_top(pyautogui_module, fallback_config, args)
                        except Exception:
                            cleanup_ok = False
                            retry_safe = False

                # A cancellation that arrived while the window was open must not
                # return a usable mapping.  The exact window has already gone
                # through the cleanup block above, so it is now safe to stop.
                if operation_cancel_event.is_set():
                    unresolved.append(name)
                    unresolved.extend(names[index:])
                    break
                if resolved_id is not None and cleanup_ok:
                    resolved[name] = resolved_id
                    used_ids[resolved_id] = normalize_chat_match_name(name)
                    # Verification evidence for the UI-thread apply boundary.
                    # DirectChat persistence uses the tablet's current label after
                    # re-checking the chat metadata there.
                    resolved_labels[resolved_id] = str(getattr(context, "chat_title", "") or "").strip()
                else:
                    unresolved.append(name)
                if not cleanup_ok:
                    # Never open another chat if the previous exact window could
                    # not be closed safely.
                    unresolved.extend(names[index:])
                    break

            result = PcChatFallbackResult(
                resolved,
                tuple(dict.fromkeys(unresolved)),
                resolved_labels,
                retry_safe=retry_safe,
            )
            return result
        finally:
            # Every per-target finally has already closed the exact room and
            # restored the safe list before this transition is published.
            notify_screen_state(False)


def safe_auto_match_failure(exc: BaseException) -> AutoMatchFailure:
    """Describe a chat-list failure without copying exception text/tokens."""

    code = redact_error(exc, fallback=type(exc).__name__)
    key = f"{type(exc).__name__} {code}".casefold()
    if "get_chats_failed" in key:
        cause = "카카오 전체 채팅 목록 페이지 조회 실패"
        next_action = "저장된 매칭은 Hub가 채팅 ID별로 다시 확인합니다. 남은 미일치 대상만 [수동 매칭]에서 확인하세요."
    elif any(
        marker in key
        for marker in (
            "credential",
            "vault",
            "auth",
            "refresh_rejected",
            "invalid_access_token",
            "login_failed",
            "refresh_unavailable",
        )
    ):
        cause = "저장된 카카오 인증정보가 만료되었거나 해제됨"
        next_action = " ".join(connection_guidance("credentials_missing" if "credentials_missing" in key else "refresh_rejected"))
    elif "timeout" in key:
        cause = "카카오 조회 응답 시간 초과"
        next_action = "네트워크와 태블릿 연결 상태를 확인한 뒤 다시 시도하세요."
    elif "process_start" in key or "node" in key or "sdk" in key:
        cause = "직접 전송 브리지 실행 환경을 시작하지 못함"
        next_action = "Node/브리지 설치 경로와 실행 권한을 확인한 뒤 다시 시도하세요."
    elif "disconnect" in key or "bridge_closed" in key:
        cause = "조회 중 직접 전송 브리지 연결이 종료됨"
        next_action = "카카오톡 로그인과 태블릿 연결을 확인하고 앱을 다시 연 뒤 재시도하세요."
    else:
        cause = "카카오 채팅 목록 조회가 완료되지 않음"
        next_action = "카카오톡 로그인·태블릿 연결·네트워크 상태를 확인한 뒤 다시 시도하세요."
    return AutoMatchFailure(code=code, cause=cause, next_action=next_action)


def direct_send_target_preview(
    selected: list[tuple[int, MessageRow]] | tuple[tuple[int, MessageRow], ...],
    fallback_schedule: datetime | None,
    direct_enabled: bool,
    mappings: dict[str, str],
    mapping_labels: dict[str, str],
    excluded_names: Iterable[Any] = (),
) -> tuple[int, tuple[str, ...]]:
    """Return direct row count and operator-visible, deduplicated targets."""

    excluded = {
        normalize_chat_match_name(name)
        for name in excluded_names
        if normalize_chat_match_name(name)
    }
    direct_rows = [
        row
        for _, row in selected
        if row_uses_direct_send(row, schedule_datetime_for_row(row, fallback_schedule), direct_enabled)
        and normalize_chat_match_name(row.chat_name) not in excluded
    ]
    lines: list[str] = []
    seen_names: set[str] = set()
    for row in direct_rows:
        normalized = normalize_chat_match_name(row.chat_name)
        if normalized in seen_names:
            continue
        seen_names.add(normalized)
        chat_id = resolve_chat_mapping(mappings, row.chat_name)
        saved_label = mapping_labels.get(chat_id) if chat_id else None
        label = saved_label if chat_id and is_confirmed_mapping_label(chat_id, saved_label) else "이름 미확인"
        lines.append(f"- {row.chat_name.strip()} → {label} · chat ID {chat_id or '-'}")
    return len(direct_rows), tuple(lines)


class HubChatMatchingMixin:
    def cancel_auto_chat_matches(self) -> None:
        """Invalidate an in-flight list read before the loaded rows change."""

        cancel_event = getattr(self, "_auto_match_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        self._auto_match_generation = int(getattr(self, "_auto_match_generation", 0)) + 1
        self._auto_match_loading = False
        callback = getattr(self, "_auto_match_pending_callback", None)
        self._auto_match_pending_callback = None
        if callback is not None:
            callback(False)


    def direct_names_for_selected(self, selected: list[tuple[int, MessageRow]] | tuple[tuple[int, MessageRow], ...]) -> tuple[str, ...]:
        """Return immediate-text recipients that need direct transport."""

        try:
            fallback_schedule = self.common_schedule_datetime()
        except ValueError:
            # ``preflight`` will provide the detailed format warning.  Do not
            # start a background chat-list query while the schedule is invalid.
            return ()
        return direct_chat_names_for_rows(
            [row for _, row in selected],
            direct_enabled=bool(self.direct_enabled_var.get()),
            fallback_schedule=fallback_schedule,
        )


    def pc_chat_fallback_config_snapshot(self) -> dict[str, Any] | None:
        """Snapshot read-only PC-chat lookup settings without saving geometry."""

        try:
            x = int(float(self.coord_x_var.get()))
            y = int(float(self.coord_y_var.get()))
        except (TypeError, ValueError, OverflowError):
            return None
        if x <= 0 or y <= 0:
            return None
        return normalize_runtime_config(
            deep_merge(
                self.config_data,
                {"search_box": {"x": x, "y": y}},
            )
        )


    def request_auto_chat_matches(
        self,
        chat_names: tuple[str, ...] | list[str],
        *,
        reason: str = "file_load",
        on_complete: Callable[[bool], None] | None = None,
        pc_fallback_config: Mapping[str, Any] | None = None,
    ) -> bool:
        """Refresh chat IDs in a worker and reconcile them on the Tk thread.

        File loading checks only saved IDs when every target already has a
        confirmed owner. New targets or incomplete room evidence require a
        fresh full list. Other requests always use read-only ``list_chats``
        plus bounded server lookups for omitted IDs. Manual
        refreshes may also enumerate already-visible KakaoTalk windows and
        read exact local rectangle metadata, but never manipulate those
        windows.  It never touches Tk variables or widgets.  Results are
        posted through ``ui_queue`` and are ignored if a newer request has
        already started.  ``on_complete`` is invoked on the Tk thread.
        """

        if self.__dict__.get("_diagnostic_busy"):
            self.log("연결", "직접 연결 점검이 끝난 뒤 자동 매칭을 다시 실행해 주세요.")
            if on_complete is not None:
                on_complete(False)
            return False
        normalized_names: list[str] = []
        seen: set[str] = set()
        for raw_name in chat_names:
            name = str(raw_name).strip()
            normalized = normalize_chat_match_name(name)
            if normalized and normalized not in seen:
                seen.add(normalized)
                normalized_names.append(name)

        previous_callback = getattr(self, "_auto_match_pending_callback", None)
        if getattr(self, "_auto_match_loading", False):
            # A newer file load or send request supersedes the older one.  Do
            # not leave its worker manipulating the PC UI after the result is
            # ignored by generation matching, even when the older request did
            # not have a completion callback (for example, file-load refresh).
            self._auto_match_pending_callback = None
            previous_cancel_event = getattr(self, "_auto_match_cancel_event", None)
            if previous_cancel_event is not None:
                previous_cancel_event.set()
            self._auto_match_generation = int(getattr(self, "_auto_match_generation", 0)) + 1
            if previous_callback is not None:
                previous_callback(False)
        if not normalized_names:
            if on_complete is not None:
                on_complete(True)
            return False

        self._auto_match_generation = int(getattr(self, "_auto_match_generation", 0)) + 1
        generation = self._auto_match_generation
        self._auto_match_loading = True
        self._auto_match_pending_callback = on_complete
        cancel_event = threading.Event()
        self._auto_match_cancel_event = cancel_event
        config_snapshot = dict(self.direct_config.to_dict())
        names_snapshot = tuple(normalized_names)
        mappings_snapshot = dict(self.__dict__.get("direct_mappings", {}))
        labels_snapshot = dict(self.__dict__.get("direct_mapping_labels", {}))
        saved_ids = (
            saved_target_lookup_ids(names_snapshot, mappings_snapshot, labels_snapshot)
            if reason == "file_load"
            else ()
        )
        started_at = time.perf_counter()
        if reason == "file_load":
            status = self.__dict__.get("status_var")
            if status is not None:
                saved_count = sum(
                    bool(is_confirmed_mapping_label(
                        resolve_chat_mapping(mappings_snapshot, name),
                        labels_snapshot.get(resolve_chat_mapping(mappings_snapshot, name)),
                    ))
                    for name in names_snapshot
                )
                status.set(f"저장 매칭 {saved_count}/{len(names_snapshot)}개 · 서버 확인 중")
        latest_chat_ids_snapshot = tuple(
            chat_id
            for chat_id in (
                positive_numeric_chat_id(chat.get("chat_id"))
                for chat in self.__dict__.get("_latest_chat_snapshot", ())
                if isinstance(chat, Mapping)
            )
            if chat_id
        )
        # Automatic reconciliation is read-only tablet reconciliation.  Never
        # let a stale caller-provided PC config turn file loading, mode
        # changes, the before-send check, or the top-level Auto Match action
        # into screen automation.  Only the explicit target-level PC lookup
        # path supplies ``reason="manual_retry"`` together with a config.
        pc_config_snapshot = (
            dict(pc_fallback_config)
            if pc_fallback_config is not None and reason == "manual_retry"
            else None
        )

        def worker() -> None:
            bridge: KakaoDirectBridge | None = None
            chats: list[dict[str, Any]] | None = None
            error: AutoMatchFailure | None = None
            pc_mappings: dict[str, str] = {}
            pc_mapping_labels: dict[str, str] = {}
            cancelled = False
            supplemental_warning: str | None = None
            supplemental_failure: AutoMatchFailure | None = None
            mode = "saved_ids" if saved_ids else "full_list"
            durations = {phase: 0.0 for phase in ("connect", "list", "lookup", "close")}
            queried_ids: set[str] = set()

            def check_cancelled() -> None:
                if cancel_event.is_set():
                    raise InterruptedError("auto match cancelled")

            def measure(phase: str, action: Callable[[], Any]) -> Any:
                before = time.perf_counter()
                try:
                    return action()
                finally:
                    durations[phase] += time.perf_counter() - before

            try:
                # list_chats is allowed while direct send is OFF; this path is
                # read-only and still requires the tablet credential vault.
                check_cancelled()
                bridge = measure("connect", lambda: self.create_direct_bridge(config_snapshot, direct_enabled=False))
                ready = getattr(bridge, "ready", None)
                if callable(ready):
                    measure("connect", ready)
                check_cancelled()
                if saved_ids:
                    target_chats: list[dict[str, Any]] = []
                    for offset in range(0, len(saved_ids), MAX_CHAT_LOOKUP_IDS):
                        check_cancelled()
                        batch = saved_ids[offset : offset + MAX_CHAT_LOOKUP_IDS]
                        queried_ids.update(batch)
                        # Transport/auth failures are errors, not permission
                        # to trust a cache or repeat a costly full request.
                        target_chats.extend(measure("lookup", lambda: bridge.get_chats_by_ids(batch)))
                    check_cancelled()
                    if saved_target_lookup_is_complete(names_snapshot, mappings_snapshot, labels_snapshot, target_chats):
                        chats = target_chats
                    else:
                        mode = "saved_ids_fallback"
                if chats is None:
                    check_cancelled()
                    try:
                        chats = list(measure("list", bridge.list_chats))
                    except DirectBridgeError as exc:
                        if exc.code != "get_chats_failed":
                            raise
                        # An aggregate page failure need not invalidate the
                        # authenticated per-room CHATINFO recovery path.
                        chats = []
                        supplemental_warning = "full_list_failed"
                        supplemental_failure = safe_auto_match_failure(exc)
                check_cancelled()
                base_chat_ids = {
                    chat_id
                    for chat_id in (
                        positive_numeric_chat_id(chat.get("chat_id"))
                        for chat in chats
                        if isinstance(chat, Mapping)
                    )
                    if chat_id
                }
                supplemental_ids: list[str] = []
                seen_supplemental_ids = set(base_chat_ids)
                required_supplemental_ids: set[str] = set()

                def add_supplemental_id(raw_chat_id: Any, *, required: bool = True) -> None:
                    chat_id = positive_numeric_chat_id(raw_chat_id)
                    if (
                        not chat_id
                        or chat_id in seen_supplemental_ids
                    ):
                        return
                    if len(supplemental_ids) >= MAX_PC_CHAT_CATALOG_IDS:
                        if required:
                            required_supplemental_ids.add(chat_id)
                        return
                    seen_supplemental_ids.add(chat_id)
                    supplemental_ids.append(chat_id)
                    if required:
                        required_supplemental_ids.add(chat_id)

                # A persisted mapping for a currently requested destination
                # remains re-verifiable even when the aggregate list omits it.
                for name in names_snapshot:
                    add_supplemental_id(resolve_chat_mapping(mappings_snapshot, name))

                if reason in {"manual_picker", "manual_validate"}:
                    # Keep a candidate from the first manual snapshot available
                    # for the mandatory second server check, even if its PC
                    # window is closed after the operator confirms it.
                    for chat_id in latest_chat_ids_snapshot:
                        add_supplemental_id(chat_id)
                    # Exact local rectangles only nominate IDs.  Every result
                    # is still fetched and member-verified by the server bridge.
                    for chat_id in discover_open_pc_chat_ids(chats):
                        add_supplemental_id(chat_id)
                    # Manual selection displays ownership across the saved
                    # catalog, so recover other saved IDs while room budget
                    # remains.  Non-manual refreshes stay target-scoped.
                    for chat_id in mappings_snapshot.values():
                        add_supplemental_id(chat_id)

                if reason in PC_CATALOG_REFRESH_REASONS:
                    catalog_candidates = discover_pc_chat_catalog_ids(
                        chats,
                        saved_mapping_ids=(
                            *mappings_snapshot.values(),
                            *latest_chat_ids_snapshot,
                        ),
                    )
                    for chat_id in catalog_candidates:
                        add_supplemental_id(chat_id, required=False)

                if supplemental_ids:
                    supplemental: list[dict[str, Any]] = []
                    lookup_failed = False
                    for offset in range(0, len(supplemental_ids), MAX_CHAT_LOOKUP_IDS):
                        check_cancelled()
                        batch = supplemental_ids[offset : offset + MAX_CHAT_LOOKUP_IDS]
                        queried_ids.update(batch)
                        try:
                            supplemental.extend(measure("lookup", lambda: bridge.get_chats_by_ids(batch)))
                        except Exception as exc:
                            # The aggregate list is still useful when one
                            # recovery batch is temporarily unavailable.
                            lookup_failed = True
                            if supplemental_failure is None:
                                supplemental_failure = safe_auto_match_failure(exc)
                    if lookup_failed:
                        supplemental_warning = "lookup_failed"
                    requested_supplemental_ids = set(supplemental_ids)
                    supplemental = [
                        chat
                        for chat in supplemental
                        if isinstance(chat, Mapping)
                        and positive_numeric_chat_id(chat.get("chat_id"))
                        in requested_supplemental_ids
                        and is_one_to_one_direct_chat(chat)
                        and bool(confirmed_peer_name(chat))
                    ]
                    verified_supplemental_ids = {
                        positive_numeric_chat_id(chat.get("chat_id"))
                        for chat in supplemental
                    }
                    if required_supplemental_ids.difference(verified_supplemental_ids):
                        supplemental_warning = supplemental_warning or "lookup_incomplete"
                    chats = merge_chat_snapshots(chats, supplemental)
                if pc_config_snapshot is not None and chats:
                    # Determine unresolved names from the same immutable
                    # snapshots used for this request; Tk state is never read
                    # from the worker.
                    reconciled = reconcile_chat_mappings(
                        mappings_snapshot,
                        labels_snapshot,
                        names_snapshot,
                        chats,
                    )
                    # Ambiguous names are deliberately never sent through the
                    # PC geometry fallback: opening the first same-title
                    # result could silently choose the wrong recipient.  Only
                    # names with no automatic candidate at all are eligible.
                    unresolved_names = tuple(reconciled.unmatched)
                    if unresolved_names:
                        screen_callback = self._make_screen_use_callback(
                            generation,
                            f"auto-match:{generation}:chat-list",
                        )
                        fallback = resolve_pc_chat_ids_via_window(
                            unresolved_names,
                            chats,
                            pc_config_snapshot,
                            reserved_chat_ids=reconciled.mappings.values(),
                            cancel_event=cancel_event,
                            screen_use_callback=screen_callback,
                        )
                        pc_mappings = dict(fallback.mappings)
                        pc_mapping_labels = dict(fallback.mapping_labels)
                        friend_tab_names = tuple(getattr(fallback, "unresolved", ()) or ())
                        if fallback.retry_safe and friend_tab_names and not cancel_event.is_set():
                            # PC aliases for ordinary 1:1 contacts can be absent
                            # from the chat-list search while remaining available
                            # in the friends list. Retry only still-unresolved
                            # names, re-selecting the friends tab before every
                            # target. The same exact geometry/peer gates apply.
                            friend_screen_callback = self._make_screen_use_callback(
                                generation,
                                f"auto-match:{generation}:friends",
                            )
                            friend_fallback = resolve_pc_chat_ids_via_window(
                                friend_tab_names,
                                chats,
                                pc_config_snapshot,
                                reserved_chat_ids=(
                                    *reconciled.mappings.values(),
                                    *pc_mappings.values(),
                                ),
                                cancel_event=cancel_event,
                                prepare_friend_tab=True,
                                screen_use_callback=friend_screen_callback,
                            )
                            pc_mappings.update(friend_fallback.mappings)
                            pc_mapping_labels.update(friend_fallback.mapping_labels)
            except Exception as exc:
                # Never put raw exception text in the queue.  Third-party
                # adapters may include tokens, while stable error codes and a
                # local action guide are enough for operator diagnosis.
                error = safe_auto_match_failure(exc)
            finally:
                if bridge is not None:
                    try:
                        measure("close", bridge.close)
                    except Exception:
                        pass
                cancelled = cancel_event.is_set()
            metrics = AutoMatchMetrics(
                mode=mode,
                target_count=len(names_snapshot),
                lookup_ids=len(queried_ids),
                catalog_count=len(chats or ()),
                connect_seconds=durations["connect"],
                list_seconds=durations["list"],
                lookup_seconds=durations["lookup"],
                close_seconds=durations["close"],
                started_at=started_at,
                worker_seconds=time.perf_counter() - started_at,
            )
            self.post(
                "auto_match_result",
                (
                    generation,
                    reason,
                    names_snapshot,
                    chats,
                    error,
                    on_complete,
                    pc_mappings,
                    pc_mapping_labels,
                    cancelled,
                    supplemental_warning,
                    supplemental_failure,
                    metrics,
                ),
            )

        threading.Thread(target=worker, name="kakao-auto-chat-match", daemon=True).start()
        return True


    def apply_auto_chat_match_result(
        self,
        chat_names: tuple[str, ...],
        chats: list[dict[str, Any]],
        *,
        reason: str = "file_load",
        pc_mappings: Mapping[Any, Any] | None = None,
        pc_mapping_labels: Mapping[Any, Any] | None = None,
    ) -> bool:
        """Apply and atomically persist a successful chat-list reconciliation."""

        previous_mappings = dict(self.direct_mappings)
        previous_labels = dict(self.direct_mapping_labels)
        previous_guarded_ids = tuple(self.__dict__.get("guarded_mapping_ids", ()))
        try:
            reconciled = reconcile_chat_mappings(
                previous_mappings,
                previous_labels,
                chat_names,
                chats,
            )
            self.direct_mappings = dict(reconciled.mappings)
            self.direct_mapping_labels = dict(reconciled.mapping_labels)
            # A guarded manual mapping keeps the exact label the operator
            # confirmed.  Do not silently replace that evidence with a new
            # title from the same stable ID; preflight must compare the fresh
            # room against the original label and block on any identity/title
            # change until the operator confirms it again.
            self.direct_mapping_labels = preserve_guarded_mapping_labels(
                self.direct_mappings, self.direct_mapping_labels,
                previous_labels, previous_guarded_ids,
            )

            # Re-check every PC result against the same current tablet list at
            # the UI/persistence boundary.  Ordinary rooms must be verified
            # two-person DirectChats with one stable peer identity.  A single
            # blank MemoChat remains the self-chat exception.
            pc_applied_names: list[str] = []
            pc_chats_by_id = {
                str(chat.get("chat_id") or "").strip(): chat
                for chat in chats
                if str(chat.get("chat_id") or "").strip()
            }
            pc_chat_id_counts: dict[str, int] = {}
            for chat in chats:
                chat_id = str(chat.get("chat_id") or "").strip()
                if chat_id:
                    pc_chat_id_counts[chat_id] = pc_chat_id_counts.get(chat_id, 0) + 1
            memo_chat_id = unique_blank_memo_chat_id(chats)
            used_chat_ids = set(self.direct_mappings.values())
            pc_labels = dict(pc_mapping_labels or {})
            pc_mapping_items = list((pc_mappings or {}).items())
            pc_payload_id_counts: dict[str, int] = {}
            for _raw_name, raw_chat_id in pc_mapping_items:
                chat_id = str(raw_chat_id or "").strip()
                if chat_id:
                    pc_payload_id_counts[chat_id] = pc_payload_id_counts.get(chat_id, 0) + 1
            unmatched_pc_names = {
                normalize_chat_match_name(name)
                for name in reconciled.unmatched
                if normalize_chat_match_name(name)
            }
            for raw_name, raw_chat_id in pc_mapping_items:
                name = str(raw_name).strip()
                normalized_name = normalize_chat_match_name(name)
                chat_id = str(raw_chat_id or "").strip()
                chat = pc_chats_by_id.get(chat_id)
                verified_pc_title = str(pc_labels.get(chat_id) or "").strip()
                tablet_label = confirmed_chat_label(chat) if chat is not None else ""
                verified_direct = bool(
                    chat is not None
                    and is_one_to_one_direct_chat(chat)
                    and confirmed_peer_name(chat)
                    and tablet_label
                )
                verified_memo = bool(
                    chat is not None
                    and memo_chat_id
                    and chat_id == memo_chat_id
                    and not tablet_label
                )
                accepted_label = tablet_label if verified_direct else verified_pc_title
                canonical_catalog_owner: str | None = None
                if verified_direct and tablet_label and chat_id in used_chat_ids:
                    canonical_label = normalize_chat_match_name(tablet_label)
                    canonical_owners = [
                        existing_name
                        for existing_name, existing_chat_id in self.direct_mappings.items()
                        if str(existing_chat_id).strip() == chat_id
                        and normalize_chat_match_name(existing_name) == canonical_label
                    ]
                    # A catalog label may be replaced only when it is the
                    # sole owner of this stable ID.  A differing/manual
                    # owner remains authoritative and blocks the fallback.
                    if len(canonical_owners) == 1 and normalized_name != canonical_label:
                        canonical_catalog_owner = canonical_owners[0]
                if (
                    not normalized_name
                    or normalized_name not in unmatched_pc_names
                    or not chat_id
                    or chat is None
                    or pc_chat_id_counts.get(chat_id) != 1
                    or pc_payload_id_counts.get(chat_id) != 1
                    or not (verified_direct or verified_memo)
                    or not verified_pc_title
                    or not accepted_label
                    or not chat_title_matches(name, verified_pc_title)
                    or (chat_id in used_chat_ids and canonical_catalog_owner is None)
                ):
                    continue
                if canonical_catalog_owner is not None:
                    self.direct_mappings.pop(canonical_catalog_owner, None)
                    used_chat_ids.discard(chat_id)
                for key in list(self.direct_mappings):
                    if normalize_chat_match_name(key) == normalized_name:
                        old_id = self.direct_mappings.pop(key)
                        used_chat_ids.discard(old_id)
                self.direct_mappings[normalized_name] = chat_id
                self.direct_mapping_labels[chat_id] = accepted_label
                used_chat_ids.add(chat_id)
                unmatched_pc_names.discard(normalized_name)
                pc_applied_names.append(name)

            # Keep the complete sanitized tablet snapshot in the persistent
            # catalog as well as reconciling the current request.  The
            # targeted reconciliation and accepted PC aliases are already in
            # ``self.direct_mappings`` at this point, so the recovery merge
            # treats those names/IDs as owners and can only add unique,
            # unowned catalog entries.  It never substitutes a saved/manual
            # alias with the tablet's display label.
            if isinstance(self.direct_config, DirectKakaoConfig):
                catalog_base = replace(
                    self.direct_config,
                    mappings=self.direct_mappings,
                    mapping_labels=self.direct_mapping_labels,
                    guarded_mapping_ids=self.__dict__.get("guarded_mapping_ids", ()),
                )
            else:  # Lightweight test doubles do not carry runtime settings.
                catalog_base = DirectKakaoConfig(
                    mappings=self.direct_mappings,
                    mapping_labels=self.direct_mapping_labels,
                    guarded_mapping_ids=self.__dict__.get("guarded_mapping_ids", ()),
                )
            catalog_plan = merge_sanitized_chat_list(catalog_base, chats)
            self.direct_mappings = dict(catalog_plan.merged_config.mappings)
            self.direct_mapping_labels = dict(catalog_plan.merged_config.mapping_labels)
            self.guarded_mapping_ids = tuple(catalog_plan.merged_config.guarded_mapping_ids)
            self.direct_mappings = normalize_mappings(self.direct_mappings)
            mapped_ids = set(self.direct_mappings.values())
            self.direct_mapping_labels = normalize_mapping_labels(
                {
                    chat_id: label
                    for chat_id, label in self.direct_mapping_labels.items()
                    if chat_id in mapped_ids and is_confirmed_mapping_label(chat_id, label)
                }
            )
            if self.direct_mappings != previous_mappings or self.direct_mapping_labels != previous_labels:
                self.save_direct_settings()
        except (OSError, ValueError) as exc:
            self.direct_mappings = previous_mappings
            self.direct_mapping_labels = previous_labels
            self.guarded_mapping_ids = previous_guarded_ids
            self.direct_config.mappings = normalize_mappings(previous_mappings)
            self.direct_config.mapping_labels = normalize_mapping_labels(previous_labels)
            self.direct_config.guarded_mapping_ids = normalize_guarded_mapping_ids(previous_guarded_ids)
            self.log("자동 매칭", f"{reason} 결과를 저장하지 못했습니다: {type(exc).__name__}")
            self.status_var.set("자동 매칭 저장 실패 · 다시 시도해 주세요.")
            return False

        pc_resolved_names = {
            normalize_chat_match_name(name)
            for name in pc_applied_names
            if normalize_chat_match_name(name)
        }
        remaining_ambiguous = tuple(
            name for name in reconciled.ambiguous if normalize_chat_match_name(name) not in pc_resolved_names
        )
        remaining_unmatched = tuple(
            name for name in reconciled.unmatched if normalize_chat_match_name(name) not in pc_resolved_names
        )
        conflict_names = tuple(
            dict.fromkeys((*remaining_ambiguous, *reconciled.invalidated))
        )
        conflict_count = len(conflict_names)
        unmatched_count = len(remaining_unmatched)
        saved_total = len(self.direct_mappings)
        manual_hint = " · [수동 매칭]에서 확인" if conflict_count or unmatched_count else ""
        self.status_var.set(
            f"자동 매칭 확정 {len(reconciled.matched) + len(pc_applied_names)}개 · 전체 저장 {saved_total}개 · "
            f"중복/불일치 {conflict_count}개 · 미일치 {unmatched_count}개{manual_hint}"
        )
        self.log(
            "자동 매칭",
            f"{reason}: 확정 {len(reconciled.matched) + len(pc_applied_names)}개, "
            f"전체 저장 {saved_total}개, "
            f"중복/불일치 {conflict_count}개, "
            f"미일치 {unmatched_count}개",
        )
        if remaining_ambiguous:
            self.log("자동 매칭", "중복 후보: " + ", ".join(remaining_ambiguous[:12]))
        if remaining_unmatched:
            self.log("자동 매칭", "미일치: " + ", ".join(remaining_unmatched[:12]))
        if reconciled.invalidated:
            self.log("자동 매칭", "저장 검증 실패: " + ", ".join(reconciled.invalidated[:12]))
        if conflict_count or unmatched_count:
            self.log(
                "자동 매칭",
                "남은 대상은 상단 [수동 매칭]에서 검증된 1:1 채팅을 직접 지정하세요.",
            )
        return True


    def handle_auto_chat_match_result(self, payload: Any) -> None:
        """Handle one generation-matched auto-match result on the Tk thread."""

        supplemental_warning: str | None = None
        supplemental_failure: AutoMatchFailure | None = None
        metrics: AutoMatchMetrics | None = None
        if len(payload) == 12 and isinstance(payload[11], AutoMatchMetrics):
            metrics = payload[11]
            payload = payload[:11]
        if len(payload) == 6:
            generation, reason, chat_names, chats, error, callback = payload
            pc_mappings: Mapping[Any, Any] = {}
            pc_mapping_labels: Mapping[Any, Any] = {}
            cancelled = False
        elif len(payload) == 7:
            generation, reason, chat_names, chats, error, callback, pc_mappings = payload
            pc_mapping_labels = {}
            cancelled = False
        elif len(payload) == 8:
            generation, reason, chat_names, chats, error, callback, pc_mappings, pc_mapping_labels = payload
            cancelled = False
        elif len(payload) == 9:
            generation, reason, chat_names, chats, error, callback, pc_mappings, pc_mapping_labels, cancelled = payload
        elif len(payload) == 10:
            (
                generation,
                reason,
                chat_names,
                chats,
                error,
                callback,
                pc_mappings,
                pc_mapping_labels,
                cancelled,
                supplemental_warning,
            ) = payload
        elif len(payload) == 11:
            (
                generation,
                reason,
                chat_names,
                chats,
                error,
                callback,
                pc_mappings,
                pc_mapping_labels,
                cancelled,
                supplemental_warning,
                supplemental_failure,
            ) = payload
        else:
            self.log("자동 매칭", "형식이 잘못된 채팅방 조회 결과를 무시했습니다.")
            return
        if generation != getattr(self, "_auto_match_generation", 0):
            return

        def log_metrics(apply_seconds: float = 0.0) -> None:
            if metrics is None:
                return
            mode_label = {
                "saved_ids": "저장 대상만 확인",
                "full_list": "전체 조회",
                "saved_ids_fallback": "저장 대상 확인 후 전체 조회",
            }.get(metrics.mode, "조회")
            elapsed = time.perf_counter() - metrics.started_at
            queue_seconds = max(0.0, elapsed - metrics.worker_seconds - apply_seconds)
            self.log(
                "매칭 시간",
                f"{reason}: {mode_label} · 대상 {metrics.target_count}개 / ID 조회 {metrics.lookup_ids}개 / "
                f"결과 {metrics.catalog_count}개 · 연결 {metrics.connect_seconds:.2f}초 · "
                f"전체 목록 {metrics.list_seconds:.2f}초 · ID 조회 {metrics.lookup_seconds:.2f}초 · "
                f"정리 {metrics.close_seconds:.2f}초 · 반영 {apply_seconds:.2f}초 · "
                f"화면 대기 {queue_seconds:.2f}초 · 합계 {elapsed:.2f}초",
            )

        self._auto_match_loading = False
        if callback is getattr(self, "_auto_match_pending_callback", None):
            self._auto_match_pending_callback = None
        if cancelled:
            log_metrics()
            self.status_var.set("자동 매칭 중단됨 · 현재 채팅방 정리를 마쳤습니다.")
            self.log("자동 매칭", f"{reason} 화면 사용을 중단하고 현재 채팅방 정리를 마쳤습니다.")
            if callback is not None:
                callback(False)
            return
        if error is not None or chats is None:
            log_metrics()
            failure = (
                error
                if isinstance(error, AutoMatchFailure)
                else AutoMatchFailure(
                    code="missing_result" if error is None else "operation_failed",
                    cause="카카오 채팅 목록 조회 결과를 받지 못함",
                    next_action="카카오톡 로그인·태블릿 연결·네트워크 상태를 확인한 뒤 다시 시도하세요.",
                )
            )
            self.status_var.set(f"자동 조회 실패 · {failure.cause} · {failure.next_action}")
            self.log(
                "자동 매칭",
                f"{reason} 채팅방 조회 실패 · {failure.cause} · code={failure.code} · 다음 행동: {failure.next_action}",
            )
            if callback is not None:
                callback(False)
            return
        apply_started = time.perf_counter()
        success = self.apply_auto_chat_match_result(
            tuple(chat_names),
            list(chats),
            reason=reason,
            pc_mappings=pc_mappings,
            pc_mapping_labels=pc_mapping_labels,
        )
        log_metrics(time.perf_counter() - apply_started)
        if success:
            # Manual selection must use the same sanitized, generation-matched
            # snapshot that was just reconciled on the Tk thread.  Copy each
            # row so a bridge-owned container cannot mutate the dialog input.
            self._latest_chat_snapshot = tuple(
                dict(chat) for chat in chats if isinstance(chat, Mapping)
            )
            if supplemental_warning == "lookup_failed":
                if isinstance(supplemental_failure, AutoMatchFailure):
                    self.status_var.set(
                        f"기본 목록 확인 완료 · 추가 조회 실패: {supplemental_failure.cause} · "
                        f"{supplemental_failure.next_action}"
                    )
                    self.log(
                        "자동 매칭",
                        f"{reason}: 누락 채팅 추가 조회 실패 · {supplemental_failure.cause} · "
                        f"code={supplemental_failure.code} · 다음 행동: {supplemental_failure.next_action}",
                    )
                else:
                    self.status_var.set(
                        "기본 목록 확인 완료 · 누락 채팅 추가 조회 실패 · 목록 새로고침을 다시 시도하세요."
                    )
                    self.log("자동 매칭", f"{reason}: 누락 채팅 추가 조회 실패")
            elif supplemental_warning == "lookup_incomplete":
                self.status_var.set(
                    "기본 목록 확인 완료 · 일부 추가 채팅은 최신 1:1 상태로 확인되지 않았습니다."
                )
                self.log("자동 매칭", f"{reason}: 일부 추가 채팅 1:1 검증 미완료")
            elif supplemental_warning == "full_list_failed":
                self.status_var.set(
                    "전체 목록 페이지 오류 · 저장된 매칭은 채팅 ID별로 다시 확인했습니다."
                )
                self.log(
                    "자동 매칭",
                    f"{reason}: 전체 목록 페이지 실패 · 저장된 chat ID 개별 검증 경로 사용",
                )
        if callback is not None:
            callback(success)


    def open_chat_mapping(self) -> None:
        """Retry server-only automatic tablet chat-ID reconciliation.

        PC-window lookup is intentionally reserved for the explicit
        ``선택 대상 PC에서 찾기`` action inside the manual chooser.  This
        top-level action must never require coordinates or use mouse/keyboard
        automation.
        """

        if getattr(self, "active_run", None) is not None or (
            getattr(self, "worker", None) is not None and self.worker.is_alive()
        ):
            messagebox.showinfo("자동 매칭", "발송 작업이 끝난 뒤 매칭을 다시 실행해 주세요.")
            return
        chat_names = unique_chat_names(self.rows)
        if not chat_names:
            messagebox.showinfo("자동 매칭", "먼저 CSV/XLSX 파일을 열거나 미리보기에 대화명을 추가해 주세요.")
            return
        if getattr(self, "_auto_match_loading", False):
            messagebox.showinfo("자동 매칭", "이미 카카오 채팅방을 확인하고 있습니다.")
            return
        self.status_var.set("태블릿의 최신 1:1 채팅 목록으로 자동 매칭하는 중…")
        self.request_auto_chat_matches(
            chat_names,
            reason="auto_match",
        )


    def open_manual_chat_mapping(
        self,
        *,
        target_names: Iterable[Any] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        """Refresh the tablet list, then open the explicit manual chooser.

        Like :meth:`open_chat_mapping`, refreshing this chooser never supplies
        a PC fallback configuration, so it does not focus, open, close, click,
        or type into KakaoTalk.  Only the chooser's explicit target-level PC
        lookup may use the saved coordinates.  A mapping can be persisted only
        after current server CHATINFO and a stable member snapshot identify one
        verified two-person chat.
        """

        if getattr(self, "active_run", None) is not None or (
            getattr(self, "worker", None) is not None and self.worker.is_alive()
        ):
            messagebox.showinfo("수동 매칭", "발송 작업이 끝난 뒤 수동 매칭을 실행해 주세요.")
            return
        chat_names = (
            unique_chat_names(self.rows)
            if target_names is None
            else tuple(
                dict.fromkeys(
                    str(name).strip()
                    for name in target_names
                    if normalize_chat_match_name(name)
                )
            )
        )
        if not chat_names:
            messagebox.showinfo("수동 매칭", "먼저 CSV/XLSX 파일을 열거나 미리보기에 대화명을 추가해 주세요.")
            if on_close is not None:
                on_close()
            return
        if getattr(self, "_auto_match_loading", False):
            messagebox.showinfo("수동 매칭", "이미 카카오 채팅방을 확인하고 있습니다.")
            return

        def show_after_refresh(success: bool) -> None:
            if not success:
                if on_close is not None:
                    on_close()
                return
            if target_names is None and on_close is None:
                # Preserve the simple explicit-menu call shape used by older
                # integrations while the send preflight can pass a bounded
                # target set and continuation.
                self.show_manual_chat_mapping_dialog(self._latest_chat_snapshot)
                return
            self.show_manual_chat_mapping_dialog(
                self._latest_chat_snapshot,
                target_names=chat_names,
                on_close=on_close,
            )

        self.status_var.set("수동 매칭용 최신 1:1 채팅 목록을 확인하는 중…")
        self.request_auto_chat_matches(
            chat_names,
            reason="manual_picker",
            on_complete=show_after_refresh,
        )


    def request_manual_pc_target_lookup(
        self,
        target_name: Any,
        pc_config: Mapping[str, Any],
        *,
        on_complete: Callable[[bool], None],
    ) -> bool:
        """Start the explicit, one-target PC lookup used by the manual dialog."""

        target = str(target_name or "").strip()
        if not target or not isinstance(pc_config, Mapping):
            return False
        self.status_var.set(f"PC에서 선택 대상 확인 중 · {target}")
        return self.request_auto_chat_matches(
            (target,),
            reason="manual_retry",
            on_complete=on_complete,
            pc_fallback_config=dict(pc_config),
        )


    def manual_mapping_unresolved_names(
        self,
        latest_chats: Iterable[Mapping[str, Any]] = (),
        *,
        target_names: Iterable[Any] | None = None,
    ) -> tuple[str, ...]:
        """Return bounded direct targets that cannot yet be sent safely."""

        names = (
            unique_chat_names(self.rows)
            if target_names is None
            else tuple(
                dict.fromkeys(
                    str(name).strip()
                    for name in target_names
                    if normalize_chat_match_name(name)
                )
            )
        )
        missing = {
            normalize_chat_match_name(name)
            for name in names
            if not resolve_chat_mapping(self.direct_mappings, name)
        }
        unconfirmed = {
            normalize_chat_match_name(name)
            for name in names
            if (
                (chat_id := resolve_chat_mapping(self.direct_mappings, name))
                and not is_confirmed_mapping_label(
                    chat_id,
                    self.direct_mapping_labels.get(chat_id),
                )
            )
        }
        unsafe = {
            normalize_chat_match_name(name)
            for name in unverified_alias_mapping_names(
                names,
                self.direct_mappings,
                self.direct_mapping_labels,
                latest_chats,
                guarded_mapping_ids=self.__dict__.get("guarded_mapping_ids", ()),
            )
        } if latest_chats else set()
        return tuple(
            name
            for name in names
            if normalize_chat_match_name(name) in missing | unconfirmed | unsafe
        )


    def manual_mapping_reserved_chat_ids(self, target_name: str) -> tuple[str, ...]:
        """Reserve IDs already used by another current CSV destination."""

        normalized_target = normalize_chat_match_name(target_name)
        reserved: list[str] = []
        for name in unique_chat_names(self.rows):
            if normalize_chat_match_name(name) == normalized_target:
                continue
            chat_id = resolve_chat_mapping(self.direct_mappings, name)
            if chat_id:
                reserved.append(chat_id)
        return tuple(dict.fromkeys(reserved))


    def commit_manual_chat_mapping(
        self,
        target_name: str,
        candidate: ManualChatCandidate,
        latest_chats: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """Validate and atomically persist one operator-confirmed choice."""

        reserved_ids = self.manual_mapping_reserved_chat_ids(target_name)
        result = apply_manual_chat_mapping(
            target_name,
            candidate.chat_id,
            latest_chats,
            self.direct_mappings,
            self.direct_mapping_labels,
            selected_candidate=candidate,
            reserved_chat_ids=reserved_ids,
        )
        if not result.success:
            return False, "선택한 채팅방이 최신 목록의 안전 조건을 통과하지 못했습니다. 목록을 새로고침해 주세요."

        previous_mappings = dict(self.direct_mappings)
        previous_labels = dict(self.direct_mapping_labels)
        previous_guarded_ids = tuple(self.__dict__.get("guarded_mapping_ids", ()))
        previous_config_mappings = dict(getattr(self.direct_config, "mappings", {}))
        previous_config_labels = dict(getattr(self.direct_config, "mapping_labels", {}))
        previous_config_guarded_ids = tuple(getattr(self.direct_config, "guarded_mapping_ids", ()))
        previous_config_enabled = bool(getattr(self.direct_config, "enabled", False))
        try:
            self.direct_mappings = dict(result.mappings)
            self.direct_mapping_labels = dict(result.mapping_labels)
            self.guarded_mapping_ids = normalize_guarded_mapping_ids(
                (*previous_guarded_ids, candidate.chat_id)
            )
            self.save_direct_settings()
        except (OSError, TypeError, ValueError) as exc:
            self.direct_mappings = previous_mappings
            self.direct_mapping_labels = previous_labels
            self.guarded_mapping_ids = previous_guarded_ids
            self.direct_config.mappings = previous_config_mappings
            self.direct_config.mapping_labels = previous_config_labels
            self.direct_config.guarded_mapping_ids = previous_config_guarded_ids
            self.direct_config.enabled = previous_config_enabled
            return False, f"설정 파일 저장에 실패했습니다 ({type(exc).__name__})."
        return True, ""


    def show_manual_chat_mapping_dialog(
        self,
        latest_chats: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        *,
        target_names: Iterable[Any] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        """Show a modal, explicit target-to-room chooser for unresolved rows."""

        snapshot = tuple(dict(chat) for chat in latest_chats if isinstance(chat, Mapping))
        target_scope = (
            unique_chat_names(self.rows)
            if target_names is None
            else tuple(
                dict.fromkeys(
                    str(name).strip()
                    for name in target_names
                    if normalize_chat_match_name(name)
                )
            )
        )
        unresolved = self.manual_mapping_unresolved_names(snapshot, target_names=target_scope)
        if not unresolved:
            messagebox.showinfo("수동 매칭", "현재 파일의 모든 대화명이 매칭되고 최신 1:1 상태로 확인되었습니다.")
            if on_close is not None:
                on_close()
            return

        dialog = tk.Toplevel(self)
        dialog.title("수동 매칭 · 검증된 1:1 채팅만 표시")
        dialog.transient(self)
        dialog.minsize(760, 500)
        dialog.geometry("820x560")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(4, weight=1)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="CSV 대상과 실제 카카오 1:1 채팅을 직접 확인해 연결합니다.",
            font=("Malgun Gothic", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 4))
        ttk.Label(
            dialog,
            text="목록 새로고침은 카카오톡을 조작하지 않습니다. ‘선택 대상 PC에서 찾기’만 검색창을 사용하며 메시지는 보내지 않습니다.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 12))

        controls = ttk.Frame(dialog)
        controls.grid(row=2, column=0, sticky="ew", padx=16)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text="미연결/재확인 대상").grid(row=0, column=0, sticky="w", padx=(0, 6))
        target_var = tk.StringVar(value=unresolved[0])
        target_combo = ttk.Combobox(
            controls,
            textvariable=target_var,
            values=unresolved,
            state="readonly",
            width=28,
        )
        target_combo.grid(row=0, column=1, sticky="ew", padx=(0, 14))
        ttk.Label(controls, text="후보 검색").grid(row=0, column=2, sticky="w", padx=(0, 6))
        filter_var = tk.StringVar(value="")
        filter_entry = ttk.Entry(controls, textvariable=filter_var, width=24)
        filter_entry.grid(row=0, column=3, sticky="ew")

        candidate_status_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=candidate_status_var, style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", padx=16, pady=(10, 5)
        )

        table_wrap = ttk.Frame(dialog)
        table_wrap.grid(row=4, column=0, sticky="nsew", padx=16)
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)
        columns = ("label", "peer", "owner", "chat_id")
        candidate_tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="browse")
        candidate_tree.heading("label", text="카카오 표시명")
        candidate_tree.heading("peer", text="확인된 상대")
        candidate_tree.heading("owner", text="현재 저장명")
        candidate_tree.heading("chat_id", text="chat ID 식별")
        candidate_tree.column("label", width=230, anchor="w")
        candidate_tree.column("peer", width=160, anchor="w")
        candidate_tree.column("owner", width=220, anchor="w")
        candidate_tree.column("chat_id", width=105, anchor="center", stretch=False)
        candidate_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_wrap, orient="vertical", command=candidate_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        candidate_tree.configure(yscrollcommand=scrollbar.set)

        ttk.Label(
            dialog,
            text="서버 표시명이 다른 PC 로컬 별칭은 대상을 고른 뒤 ‘선택 대상 PC에서 찾기’를 사용하세요.",
            style="Muted.TLabel",
        ).grid(row=5, column=0, sticky="w", padx=16, pady=(8, 4))

        candidates_by_item: dict[str, ManualChatCandidate] = {}
        continuation_fired = False
        candidate_generation = 0
        candidate_after_id: str | None = None
        candidate_loading = False
        validation_active = False
        # Reuse only display candidates derived from the exact same source
        # and ownership. The final server validation below never uses this.
        candidate_cache: tuple[Any, ...] | None = None

        def chat_id_hint(chat_id: str) -> str:
            return chat_id if len(chat_id) <= 6 else f"…{chat_id[-6:]}"

        def current_unresolved() -> tuple[str, ...]:
            return self.manual_mapping_unresolved_names(snapshot, target_names=target_scope)

        def cancel_candidate_callback() -> None:
            nonlocal candidate_after_id, candidate_generation
            candidate_generation += 1
            if candidate_after_id is not None:
                dialog.after_cancel(candidate_after_id)
                candidate_after_id = None

        def render_candidates(
            generation: int,
            candidates: tuple[ManualChatCandidate, ...],
            old_items: tuple[str, ...],
            deleted: int = 0,
            inserted: int = 0,
        ) -> None:
            nonlocal candidate_after_id, candidate_loading
            if generation != candidate_generation or not dialog.winfo_exists() or validation_active:
                return
            candidate_after_id = None
            deadline = time.monotonic() + 0.008
            operations = 0
            while deleted < len(old_items) and operations < 150:
                candidate_tree.delete(old_items[deleted])
                deleted += 1
                operations += 1
                if time.monotonic() >= deadline:
                    break
            while deleted == len(old_items) and inserted < len(candidates) and operations < 150:
                candidate = candidates[inserted]
                item = candidate_tree.insert(
                    "", "end",
                    values=(candidate.label, candidate.peer_name, candidate.owner_name or "미사용", chat_id_hint(candidate.chat_id)),
                )
                candidates_by_item[item] = candidate
                inserted += 1
                operations += 1
                if time.monotonic() >= deadline:
                    break
            if deleted < len(old_items) or inserted < len(candidates):
                candidate_status_var.set(f"후보 목록 표시 중 · {inserted}/{len(candidates)}개")
                candidate_after_id = dialog.after(1, lambda: render_candidates(generation, candidates, old_items, deleted, inserted))
                return
            candidate_loading = False
            candidate_tree.state(["!disabled"])
            connect_button.state(["!disabled"])
            if candidates:
                candidate_status_var.set(f"선택 가능한 검증된 1:1 채팅 {len(candidates)}개 · 자동 선택하지 않습니다.")
            else:
                candidate_status_var.set(
                    "선택 가능한 1:1 후보가 없습니다. 대상을 고른 뒤 ‘선택 대상 PC에서 찾기’를 사용하세요."
                )

        def prepare_candidates(generation: int) -> None:
            nonlocal candidate_after_id, candidate_loading, candidate_cache
            if generation != candidate_generation or not dialog.winfo_exists() or validation_active:
                return
            candidate_after_id = None
            target = target_var.get().strip()
            if not target:
                candidate_tree.delete(*candidate_tree.get_children())
                candidate_loading = False
                candidate_tree.state(["!disabled"])
                candidate_status_var.set("미연결 CSV 대상을 선택하세요.")
                return
            try:
                rows_snapshot = tuple(self.rows)
                if (candidate_cache is not None and candidate_cache[0] == target
                        and candidate_cache[1] is snapshot
                        and candidate_cache[2] == self.direct_mappings
                        and candidate_cache[3] == rows_snapshot):
                    searchable = candidate_cache[4]
                else:
                    candidates = manual_chat_candidates(
                        snapshot,
                        self.direct_mappings,
                        target_name=target,
                        reserved_chat_ids=self.manual_mapping_reserved_chat_ids(target),
                    )
                    searchable = tuple((candidate, normalize_chat_match_name(
                        " ".join((candidate.label, candidate.peer_name, candidate.owner_name, candidate.chat_id))
                    )) for candidate in candidates)
                    candidate_cache = (target, snapshot, dict(self.direct_mappings), rows_snapshot, searchable)
            except (TypeError, ValueError):
                candidate_tree.delete(*candidate_tree.get_children())
                candidate_loading = False
                candidate_tree.state(["!disabled"])
                candidate_status_var.set("저장된 매칭에 충돌이 있어 후보를 표시할 수 없습니다.")
                return
            needle = normalize_chat_match_name(filter_var.get())
            candidates = tuple(candidate for candidate, haystack in searchable if not needle or needle in haystack)
            render_candidates(generation, candidates, tuple(candidate_tree.get_children()))

        def schedule_candidates(delay: int) -> None:
            nonlocal candidate_after_id, candidate_loading
            if not dialog.winfo_exists() or validation_active:
                return
            cancel_candidate_callback()
            generation = candidate_generation
            candidate_loading = True
            selected_items = candidate_tree.selection()
            if selected_items:
                candidate_tree.selection_remove(*selected_items)
            candidates_by_item.clear()
            candidate_tree.state(["disabled"])
            connect_button.state(["disabled"])
            candidate_status_var.set("후보 검색 결과를 준비하는 중…")
            candidate_after_id = dialog.after(delay, lambda: prepare_candidates(generation))

        def rebuild_candidates(*_args: Any) -> None:
            schedule_candidates(0)

        def search_candidates(*_args: Any) -> None:
            schedule_candidates(120)

        def close_dialog(*, continue_flow: bool = True) -> None:
            nonlocal continuation_fired
            cancel_candidate_callback()
            if dialog.winfo_exists():
                dialog.destroy()
            if continue_flow and on_close is not None and not continuation_fired:
                continuation_fired = True
                on_close()

        def refresh_snapshot() -> None:
            close_dialog(continue_flow=False)
            self.after_idle(
                lambda: self.open_manual_chat_mapping(
                    target_names=target_scope,
                    on_close=on_close,
                )
            )

        def find_selected_target_on_pc() -> None:
            target = target_var.get().strip()
            if not target:
                messagebox.showwarning(
                    "PC에서 대상 찾기",
                    "먼저 미연결/재확인 대상을 선택해 주세요.",
                    parent=dialog,
                )
                return
            pc_config = self.pc_chat_fallback_config_snapshot()
            if pc_config is None:
                messagebox.showinfo(
                    "PC에서 대상 찾기",
                    "먼저 [화면 좌표 설정]에서 카카오톡 검색창 위치를 저장해 주세요.",
                    parent=dialog,
                )
                return
            confirmed = messagebox.askyesno(
                "PC에서 대상 찾기",
                f"선택 대상: {target}\n\n"
                "카카오톡 검색창에 이 대화명을 입력해 빈 메시지 상태로 방만 열고, "
                "정확한 chat ID를 확인한 뒤 다시 닫습니다. 메시지는 발송하지 않습니다.\n\n"
                "계속할까요?",
                parent=dialog,
            )
            if not confirmed:
                return

            previous_snapshot = tuple(snapshot)
            close_dialog(continue_flow=False)

            def show_after_pc_lookup(success: bool) -> None:
                latest = (
                    tuple(
                        dict(chat)
                        for chat in self.__dict__.get("_latest_chat_snapshot", ())
                        if isinstance(chat, Mapping)
                    )
                    if success
                    else previous_snapshot
                )
                if not success:
                    messagebox.showerror(
                        "PC에서 대상 찾기 실패",
                        "대상 채팅방을 안전하게 확인하지 못했습니다. 검색창 좌표와 카카오톡 로그인을 확인해 주세요.",
                    )
                self.show_manual_chat_mapping_dialog(
                    latest,
                    target_names=target_scope,
                    on_close=on_close,
                )

            requested = self.request_manual_pc_target_lookup(
                target,
                pc_config,
                on_complete=show_after_pc_lookup,
            )
            if not requested:
                self.after_idle(
                    lambda: self.show_manual_chat_mapping_dialog(
                        previous_snapshot,
                        target_names=target_scope,
                        on_close=on_close,
                    )
                )

        def set_validation_state(active: bool) -> None:
            nonlocal validation_active
            validation_active = active
            if active:
                cancel_candidate_callback()
            target_combo.configure(state="disabled" if active else "readonly")
            filter_entry.state(["disabled"] if active else ["!disabled"])
            candidate_tree.state(["disabled"] if active or candidate_loading else ["!disabled"])
            refresh_button.state(["disabled"] if active else ["!disabled"])
            pc_find_button.state(["disabled"] if active else ["!disabled"])
            connect_button.state(["disabled"] if active or candidate_loading else ["!disabled"])

        def finish_successful_connection(target: str, candidate: ManualChatCandidate) -> None:
            self.status_var.set(f"수동 매칭 저장 완료 · {target} → {candidate.label}")
            self.log("수동 매칭", f"{target} → {candidate.label} 연결을 저장했습니다.")
            remaining = current_unresolved()
            if not remaining:
                messagebox.showinfo(
                    "수동 매칭",
                    "현재 파일의 미연결/재확인 대상을 모두 처리했습니다.",
                    parent=dialog,
                )
                close_dialog()
                return
            target_combo.configure(values=remaining)
            target_var.set(remaining[0])
            filter_var.set("")

        def connect_selected() -> None:
            nonlocal snapshot
            if candidate_loading or validation_active:
                return
            target = target_var.get().strip()
            selected_items = candidate_tree.selection()
            if not target or len(selected_items) != 1:
                messagebox.showwarning("수동 매칭", "CSV 대상과 연결할 1:1 채팅을 하나 선택해 주세요.", parent=dialog)
                return
            candidate = candidates_by_item.get(selected_items[0])
            if candidate is None:
                messagebox.showwarning("수동 매칭", "후보 목록을 새로고침한 뒤 다시 선택해 주세요.", parent=dialog)
                return
            live_targets = {
                normalize_chat_match_name(name): name for name in unique_chat_names(self.rows)
            }
            if normalize_chat_match_name(target) not in live_targets:
                messagebox.showwarning("수동 매칭", "CSV 대상이 미리보기에서 변경되었습니다. 목록을 새로고침해 주세요.", parent=dialog)
                return

            handoff_text = ""
            if candidate.owner_name and normalize_chat_match_name(candidate.owner_name) != normalize_chat_match_name(target):
                handoff_text = f"\n기존 기본 저장명: {candidate.owner_name} (이 이름을 CSV 대상으로 승계)"
            selection_generation = candidate_generation
            confirmed = messagebox.askyesno(
                "수동 매칭 최종 확인",
                f"CSV 대상: {target}\n"
                f"카카오 표시명: {candidate.label}\n"
                f"확인된 상대: {candidate.peer_name}\n"
                f"chat ID: {chat_id_hint(candidate.chat_id)}"
                f"{handoff_text}\n\n"
                "이 1:1 채팅을 직접 전송 목적지로 저장할까요?",
                parent=dialog,
            )
            if not confirmed:
                return
            if (selection_generation != candidate_generation or candidate_loading
                    or validation_active or target_var.get().strip() != target):
                return

            # Do not commit the snapshot that may have sat behind an open
            # dialog.  Fetch and reconcile a second server-only snapshot, then
            # require the originally confirmed candidate to remain byte-for-
            # byte equivalent at the apply boundary.
            set_validation_state(True)
            candidate_status_var.set("저장 전 최신 1:1 채팅 상태를 다시 확인하는 중…")
            self.status_var.set(f"수동 매칭 저장 전 재확인 · {target}")

            def apply_after_refresh(refreshed: bool) -> None:
                nonlocal snapshot
                if not dialog.winfo_exists():
                    return
                set_validation_state(False)
                if not refreshed:
                    messagebox.showerror(
                        "수동 매칭 실패",
                        "최신 채팅 목록을 다시 확인하지 못해 저장하지 않았습니다.",
                        parent=dialog,
                    )
                    rebuild_candidates()
                    return
                snapshot = tuple(
                    dict(chat)
                    for chat in getattr(self, "_latest_chat_snapshot", ())
                    if isinstance(chat, Mapping)
                )
                try:
                    fresh_candidate = next(
                        (
                            item
                            for item in manual_chat_candidates(
                                snapshot,
                                self.direct_mappings,
                                target_name=target,
                                reserved_chat_ids=self.manual_mapping_reserved_chat_ids(target),
                            )
                            if item.chat_id == candidate.chat_id
                        ),
                        None,
                    )
                except (TypeError, ValueError):
                    fresh_candidate = None
                if fresh_candidate is None or (
                    fresh_candidate.chat_id,
                    fresh_candidate.label,
                    fresh_candidate.peer_name,
                ) != (candidate.chat_id, candidate.label, candidate.peer_name):
                    messagebox.showerror(
                        "수동 매칭 변경 감지",
                        "최종 확인 뒤 채팅방 표시명 또는 상대 identity가 바뀌어 저장하지 않았습니다.",
                        parent=dialog,
                    )
                    rebuild_candidates()
                    return
                # Always commit the candidate rebuilt from the second snapshot.
                # This also lets the operator repair an existing stale guarded
                # mapping by explicitly confirming the same room's new label,
                # or by selecting a different verified 1:1 room.  The pure
                # apply boundary below still enforces current ownership and
                # reserved-ID rules atomically.
                success, error = self.commit_manual_chat_mapping(target, fresh_candidate, snapshot)
                if not success:
                    messagebox.showerror("수동 매칭 실패", error, parent=dialog)
                    rebuild_candidates()
                    return
                finish_successful_connection(target, fresh_candidate)

            requested = self.request_auto_chat_matches(
                target_scope,
                reason="manual_validate",
                on_complete=apply_after_refresh,
            )
            if not requested:
                set_validation_state(False)
                messagebox.showerror(
                    "수동 매칭 실패",
                    "재확인할 CSV 대상이 없어 저장하지 않았습니다.",
                    parent=dialog,
                )
                rebuild_candidates()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=6, column=0, sticky="ew", padx=16, pady=(10, 16))
        refresh_button = ttk.Button(buttons, text="목록 새로고침", command=refresh_snapshot)
        refresh_button.pack(side="left")
        pc_find_button = ttk.Button(
            buttons,
            text="선택 대상 PC에서 찾기",
            command=find_selected_target_on_pc,
        )
        pc_find_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="닫기", command=close_dialog).pack(side="right")
        connect_button = ttk.Button(buttons, text="선택 연결", command=connect_selected, style="Accent.TButton")
        connect_button.pack(side="right", padx=(0, 8))

        target_combo.bind("<<ComboboxSelected>>", rebuild_candidates)
        filter_var.trace_add("write", search_candidates)
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.bind("<Destroy>", lambda event: cancel_candidate_callback() if event.widget is dialog else None, add="+")
        rebuild_candidates()
        target_combo.focus_set()
