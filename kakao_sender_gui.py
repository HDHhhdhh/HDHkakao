from __future__ import annotations

import csv
import ctypes
import io
import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from kakao_gui_theme import configure_hub_style
from kakao_gui_text import KoreanLabel

SMALL_IMAGE_WARNING_THRESHOLD_PX: Final = 700

from kakao_app_paths import SCHEDULE_JOURNAL_PATH
from kakao_schedule_capacity import ScheduleCapacity
from kakao_schedule_journal import ScheduleJournal
from kakao_schedule_session import NativeScheduleSession
from kakao_ui_dpi import initialize_dpi_awareness

from kakao_send_service import (
    SendPorts,
    SendRunRequest,
    SendService,
    format_preflight_warning,
    is_fatal_send_error,
    is_unknown_direct_send_error,
    preflight_next_action,
    preserve_macro_retry_queue,
    row_uses_direct_send,
    selected_rows_require_macro,
)

from kakao_mapping_policy import (
    POSITIVE_CHAT_ID_PATTERN,
    ChatMappingHandoff,
    ChatAutoMatchResult,
    ChatMappingReconcileResult,
    ManualChatCandidate,
    ManualChatMappingResult,
    normalize_chat_match_name,
    chat_choice_title,
    confirmed_peer_name,
    confirmed_peer_local_names,
    confirmed_chat_label,
    is_confirmed_mapping_label,
    structured_direct_chat_aliases,
    direct_chat_person_identities,
    canonical_handoff_owner_identity,
    is_one_to_one_direct_chat,
    positive_numeric_chat_id,
    merge_chat_snapshots,
    unique_blank_memo_chat_id,
    auto_match_chat_ids,
    reconcile_chat_mappings,
    normalize_manual_chat_targets,
    manual_chat_candidates,
    apply_manual_chat_mapping,
    unverified_alias_mapping_names,
    resolve_chat_mapping,
    preserve_guarded_mapping_labels,
)

from kakao_chat_catalog import (
    MAX_PC_CHAT_CATALOG_IDS,
    discover_pc_chat_catalog_ids,
)

from kakao_app_paths import (
    app_data_dir,
    local_data_dir,
    CONFIG_PATH,
    DIRECT_CONFIG_PATH,
    LOG_DIR,
)

from kakao_document import (
    PREVIEW_HEADERS,
    attachment_status_text,
    attachment_summary,
    format_attachment_paths,
    row_with_attachments,
    rows_to_csv_text,
    rows_with_common_attachments,
    shared_attachment_paths,
)

from kakao_document import load_document, plan_clipboard_paste
from kakao_file_drop import (
    attachment_paths_from_drop,
    attachment_paths_from_text,
    enable_file_drop,
    format_attachment_path_input,
    validated_attachment_paths,
)
from kakao_delivery import delivery_route, delivery_routes, route_summary, progress_summary
from kakao_gui_loading import HubLoadingMixin
from kakao_gui_exports import HubExportsMixin
from kakao_pc_lease import PC_KAKAO_UI_LOCK, pc_kakao_ui_lease, PcUiLeaseCancelled
from kakao_run_journal import RunJournal, RunJournalError
from kakao_diagnostics import diagnose_local, connection_guidance

try:
    from kakao_pc_chat_map import discover_pc_chat_map
except ImportError:  # pragma: no cover - optional until the provider is installed
    discover_pc_chat_map = None  # type: ignore[assignment]

from kakao_direct import (
    CredentialVault,
    DirectBridgeError,
    DirectKakaoConfig,
    DirectSendError,
    DirectSendUnknownError,
    KakaoDirectBridge,
    MAX_CHAT_LOOKUP_IDS,
    normalize_chat_name,
    normalize_chat_id,
    normalize_guarded_mapping_ids,
    normalize_mapping_labels,
    normalize_mappings,
    redact_error,
    validate_direct_attachments,
)
from kakao_tablet_reauth import SpoilerTabletAuthenticator, TabletReauthCoordinator

from kakao_send_run import (
    SendRunCancelled,
    SendRunCompleted,
    SendRunContext,
    SendRunEvent,
    SendRunProgress,
    SendRunSupervisor,
)

from kakao_sender import (
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_PATH,
    FocusSafetyError,
    MessageRow,
    deep_merge,
    earliest_schedule_datetime,
    close_chat_after_send,
    focus_kakao_window,
    focus_kakao_window_via_uia,
    find_kakao_windows,
    get_window_rect,
    chat_title_matches,
    format_schedule_datetime,
    is_image_path,
    load_messages,
    missing_attachment_paths,
    open_chat,
    parse_schedule_datetime,
    parse_attachment_paths,
    require_pyautogui,
    return_to_recent_chat_top,
    save_config,
    search_box_config_from_position,
    send_one,
    send_scheduled_one,
    normalize_runtime_config,
    schedule_datetime_for_row,
    validate_schedule_row,
    validate_send_mode,
    write_log,
)

from recover_chat_mappings import merge_sanitized_chat_list


from kakao_gui_constants import (
    APP_NAME,
    APP_VERSION,
    ATTACHMENT_EMPTY_LABEL,
    CLOSE_WAIT_TIMEOUT_SECONDS,
    FAILED_RETRY_INSTRUCTION,
    GENERIC_KAKAO_WINDOW_TITLES,
    KOREAN_WEEKDAYS,
    MIN_WINDOW_SIZE,
    PC_CATALOG_REFRESH_REASONS,
    SCHEDULE_PICKER_DAYS,
    SCHEDULE_PICKER_MINUTES,
    SCREEN_USE_ACK_TIMEOUT_SECONDS,
    SCREEN_USE_POLL_MS,
    SEARCH_EMPTY_STATUS,
    SUCCESS_RESULT_STATUSES,
    UNBOUNDED_WINDOW_SIZE,
    VK_ESCAPE,
    VK_SHIFT
)
from kakao_gui_matching import (
    HubChatMatchingMixin,
    AutoMatchFailure,
    PcChatFallbackResult,
    unique_chat_names,
    direct_chat_names_for_rows,
    is_exact_pc_geometry_resolution,
    discover_open_pc_chat_ids,
    resolve_pc_chat_ids_via_window,
    safe_auto_match_failure,
    direct_send_target_preview
)
from kakao_gui_feedback import (
    HubRunFeedbackMixin,
    format_attachment_empty_label,
    format_attachment_panel_help,
    format_search_empty_status,
    format_search_no_result_status,
    format_search_match_status,
    format_failed_summary_count,
    format_failed_retry_instruction,
    format_failed_summary_status,
    format_retry_failed_button_label,
    format_done_status,
    failed_result_indexes,
    macro_retry_result_indexes,
    row_status_tag
)
from kakao_gui_panels import (
    HubRunPanelsMixin
)
from kakao_gui_screen import (
    windows_async_key_state_reader,
    shift_escape_is_down
)










def schedule_picker_initial_datetime(value: str, now: datetime) -> datetime:
    earliest = earliest_schedule_datetime(now)
    try:
        parsed = parse_schedule_datetime(value)
    except ValueError:
        parsed = None
    if parsed is not None and parsed >= earliest and parsed.minute % 5 == 0 and parsed.second == 0 and parsed.microsecond == 0:
        return parsed
    return earliest


def schedule_picker_datetime(selected_date: date, is_pm: bool, hour12: int, minute: int) -> datetime:
    hour = hour12 % 12 + (12 if is_pm else 0)
    return datetime(selected_date.year, selected_date.month, selected_date.day, hour, minute)


def schedule_picker_time_slots(selected_date: date, now: datetime) -> tuple[datetime, ...]:
    earliest = earliest_schedule_datetime(now)
    if selected_date < earliest.date():
        return ()
    slots = tuple(
        datetime(selected_date.year, selected_date.month, selected_date.day, hour, minute)
        for hour in range(24)
        for minute in SCHEDULE_PICKER_MINUTES
    )
    return tuple(slot for slot in slots if slot >= earliest)


def schedule_picker_period_values(slots: tuple[datetime, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys("오후" if slot.hour >= 12 else "오전" for slot in slots))


def schedule_picker_hour_values(slots: tuple[datetime, ...], period: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(slot.hour % 12 or 12)
            for slot in slots
            if (slot.hour >= 12) == (period == "오후")
        )
    )


def schedule_picker_minute_values(
    slots: tuple[datetime, ...],
    period: str,
    hour_value: str,
) -> tuple[str, ...]:
    try:
        hour12 = int(hour_value)
    except (TypeError, ValueError):
        return ()
    return tuple(
        f"{slot.minute:02d}"
        for slot in slots
        if (slot.hour >= 12) == (period == "오후") and (slot.hour % 12 or 12) == hour12
    )


def schedule_picker_date_label(value: date) -> str:
    return f"{value:%Y-%m-%d} ({KOREAN_WEEKDAYS[value.weekday()]})"






















def wire_big_edit_dialog_keyboard(dialog: tk.Toplevel, first_field: ttk.Entry, save_edit: Callable[[], None]) -> None:
    def save_from_keyboard(_event: tk.Event) -> str:
        save_edit()
        return "break"

    dialog.bind("<Control-Return>", save_from_keyboard)
    first_field.focus_set()
    first_field.selection_range(0, "end")


def default_downloads_dir() -> Path:
    candidates = [Path.home() / "Downloads", Path.home() / "downloads", Path.home() / "다운로드"]
    return next((path for path in candidates if path.exists()), Path.home())


def default_desktop_dir() -> Path:
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "바탕 화면",
        Path.home() / "OneDrive" / "Desktop",
        Path.home() / "OneDrive" / "바탕 화면",
    ]
    return next((path for path in candidates if path.exists()), Path.home())






















def format_gui_config_warning(path: Path, reason: str) -> str:
    return (
        f"{DEFAULT_CONFIG_PATH.as_posix()} 형식이 잘못되어 기본 설정으로 시작합니다. "
        f"파일을 고치거나 삭제한 뒤 다시 실행해 주세요. 실제 파일: {path}. 원인: {reason}"
    )


def _is_relaxed_numeric_coordinate(value: Any) -> bool:
    try:
        int(float(str(value)))
    except (OverflowError, ValueError):
        return False
    return True


def load_relaxed_config_data(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return normalize_runtime_config({}), None
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return normalize_runtime_config({}), format_gui_config_warning(path, str(exc))
    if not isinstance(loaded, dict):
        return normalize_runtime_config({}), format_gui_config_warning(path, f"JSON object expected, got {type(loaded).__name__}")
    config = normalize_runtime_config(loaded)
    search_box = config.get("search_box")
    if not isinstance(search_box, dict):
        return normalize_runtime_config({}), format_gui_config_warning(
            path,
            f"search_box object expected, got {type(search_box).__name__}",
        )
    for coordinate_name in ("x", "y"):
        coordinate_value = search_box.get(coordinate_name, 0)
        if not _is_relaxed_numeric_coordinate(coordinate_value):
            return normalize_runtime_config({}), format_gui_config_warning(
                path,
                f"search_box.{coordinate_name} numeric expected, got {type(coordinate_value).__name__}",
            )
    window_offset = search_box.get("window_offset")
    if window_offset is not None:
        if not isinstance(window_offset, dict):
            return normalize_runtime_config({}), format_gui_config_warning(
                path,
                f"search_box.window_offset object expected, got {type(window_offset).__name__}",
            )
        for coordinate_name in ("x", "y"):
            coordinate_value = window_offset.get(coordinate_name, 0)
            if not _is_relaxed_numeric_coordinate(coordinate_value):
                return normalize_runtime_config({}), format_gui_config_warning(
                    path,
                    f"search_box.window_offset.{coordinate_name} numeric expected, got {type(coordinate_value).__name__}",
                )
    return config, None


load_gui_config = load_relaxed_config_data


class KakaoSenderApp(HubChatMatchingMixin, HubRunFeedbackMixin, HubRunPanelsMixin, HubLoadingMixin, HubExportsMixin, tk.Tk):
    def __init__(self) -> None:
        initialize_dpi_awareness()
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.ui_display_scale = max(1.0, float(self.tk.call('tk', 'scaling')) / (4 / 3))
        self.min_window_size = tuple(round(value * self.ui_display_scale) for value in MIN_WINDOW_SIZE)
        self.minsize(*self.min_window_size)

        self.file_path: Path | None = None
        self.rows: list[MessageRow] = []
        self.results: list[dict[str, Any]] = []
        self._progress_results: dict[int, dict[str, Any]] = {}
        self._running_row_indexes: set[int] = set()
        self._recovery_of: str | None = None
        self._recovery_sources_by_number: dict[int, tuple[str, int]] = {}
        self._recovery_retry_records_by_number: dict[int, dict[str, Any]] = {}
        self._last_run_sources_by_number: dict[int, tuple[str, int]] = {}
        self._journal_error: str | None = None
        try:
            self.run_journal = RunJournal()
        except RunJournalError as exc:
            self.run_journal = None
            self._journal_error = str(exc)
        self.active_run: SendRunSupervisor | None = None
        # Compatibility view for code that only needs the current cancellation
        # signal. A fresh event is installed from ``active_run`` for each run.
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._close_requested = False
        self._close_deadline: float | None = None
        self._close_poll_after_id: str | None = None
        # Chat-list reconciliation is always performed off the Tk thread;
        # only the generation-matched queue event mutates mappings or widgets.
        self._auto_match_generation = 0
        self._auto_match_loading = False
        self._auto_match_pending_callback: Callable[[bool], None] | None = None
        self._auto_match_cancel_event = threading.Event()
        self._auto_match_waiting_to_send = False
        # Latest sanitized tablet snapshot used by the modal manual chooser.
        # It is kept in memory only and replaced after every successful
        # refresh; messages and credentials are never included in list_chats.
        self._latest_chat_snapshot: tuple[dict[str, Any], ...] = ()
        # PC fallback sessions are tracked by independent tokens rather than
        # by generation alone.  A stale worker must be allowed to clear only
        # its own token; otherwise a newer screen-use session could disappear
        # from the guard row while it is still controlling KakaoTalk.
        self._screen_use_tokens: dict[str, tuple[int, threading.Event]] = {}
        self._screen_use_stop_requested = False
        self._screen_use_key_reader: Callable[[int], int] | None = None
        self._screen_use_key_down = False
        self._screen_use_poll_after_id: str | None = None
        self.started_at: float | None = None
        self.run_row_indexes: list[int] | None = None
        self.run_total = 0
        self.undo_stack: list[list[MessageRow]] = []
        self.cell_editor: ttk.Entry | None = None
        self.cell_editor_info: dict[str, Any] | None = None
        self.layout_after_id: str | None = None
        self.config_warning: str | None = None
        self.direct_config_warning: str | None = None
        self.direct_warning_confirmed = False

        self.config_data = self.load_relaxed_config()
        self.direct_config = self.load_direct_config()
        self.direct_mappings: dict[str, str] = dict(self.direct_config.mappings)
        self.direct_mapping_labels: dict[str, str] = dict(self.direct_config.mapping_labels)
        self.guarded_mapping_ids: tuple[str, ...] = tuple(self.direct_config.guarded_mapping_ids)

        self.status_var = tk.StringVar(value="준비")
        self.file_var = tk.StringVar(value="선택된 파일 없음")
        self.summary_var = tk.StringVar(value="대기: 0 | 성공: 0 | 실패: 0 | 확인: 0")
        self.route_summary_var = tk.StringVar(value="발송 대상 없음")
        self.attachment_var = tk.StringVar(value=attachment_status_text([])[1])
        self.attachment_path_var = tk.StringVar(value="")
        self.coord_x_var = tk.StringVar(value=str(self.config_data["search_box"].get("x", 0)))
        self.coord_y_var = tk.StringVar(value=str(self.config_data["search_box"].get("y", 0)))
        self.paste_only_var = tk.BooleanVar(value=False)
        self.confirm_each_var = tk.BooleanVar(value=False)
        self.test_one_var = tk.BooleanVar(value=False)
        self.no_close_var = tk.BooleanVar(value=False)
        self.assume_focused_var = tk.BooleanVar(value=False)
        self.continue_error_var = tk.BooleanVar(value=True)
        self.common_schedule_var = tk.BooleanVar(value=False)
        self.schedule_at_var = tk.StringVar(value="")
        self.direct_enabled_var = tk.BooleanVar(value=self.direct_config.enabled)
        self.direct_mode_status_var = tk.StringVar(value="OFF · PC 카톡 화면 제어")
        self.screen_use_state_var = tk.StringVar(value="화면 사용 안 함 · 서버 자동매칭은 화면을 사용하지 않습니다")
        self.schedule_mode_hint_var = tk.StringVar(value="예약 자동 등록 · 대상/시간/본문 자동 대조")
        self.schedule_counter_var = tk.StringVar(value='예약 기록 계산 중 / 100')
        self.schedule_counter_detail_var = tk.StringVar(value='등록 기록·현재 시각 기준 · 발송 완료 확인 아님')
        self.previous_limit_before_test_one = 0
        self.start_at_var = tk.IntVar(value=1)
        self.limit_var = tk.IntVar(value=0)
        self.countdown_var = tk.IntVar(value=5)
        self.delay_scale_var = tk.DoubleVar(value=1.0)
        self.progress_var = tk.DoubleVar(value=0)
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.search_var = tk.StringVar(value="")
        self.search_match_indexes: list[int] = []
        self.search_match_position = -1

        self.configure_style()
        self.build_layout()
        self.status_var.trace_add("write", self.refresh_primary_action_visual)
        self.refresh_primary_action_visual()
        self.file_drop_enabled, drop_hint = enable_file_drop(
            self, (self.attachment_panel, self.tree), self.on_attachment_drop,
        )
        self.attachment_drop_hint.configure(text=drop_hint)
        self.on_direct_mode_changed(persist=False)
        self.apply_display_half_geometry()
        self.bind("<Configure>", self.schedule_layout_rebalance)
        self.rebalance_layout()
        self.refresh_coordinate_labels()
        self.log("대기", "파일 열기 → 자동 매칭/화면 좌표 설정 → 발송 시작 순서로 진행하세요.")
        self.surface_config_warning()
        self.protocol("WM_DELETE_WINDOW", self.request_close)
        # This binding catches Shift+Escape while focus is anywhere inside
        # this application.  A separate GetAsyncKeyState poll below covers
        # the interval in which KakaoTalk itself owns the Windows focus.
        self.bind_all("<Shift-Escape>", self._on_screen_use_hotkey, add="+")
        self.after(100, self.drain_queue)
        self.after(1000, self.tick_elapsed)
        self.after(500, self.offer_run_recovery)
        self.after(300, self.tick_schedule_capacity)

    def apply_display_half_geometry(self) -> None:
        scale = self.ui_display_scale
        width = max(self.min_window_size[0], min(round(1440 * scale), int(self.winfo_screenwidth() * 0.78)))
        height = max(self.min_window_size[1], min(round(960 * scale), int(self.winfo_screenheight() * 0.82)))
        width = min(width, self.winfo_screenwidth() - 80)
        height = min(height, self.winfo_screenheight() - 80)
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        self.geometry(f"{width}x{height}")
        self.release_size_constraints()

    def release_size_constraints(self) -> None:
        self.minsize(*self.__dict__.get('min_window_size', MIN_WINDOW_SIZE))
        self.maxsize(*UNBOUNDED_WINDOW_SIZE)

    def restore_window_snapshot(self, geometry: str, state: str) -> None:
        self.release_size_constraints()
        if state == "zoomed":
            self.state("zoomed")
            self.after(50, self.release_size_constraints)
            return
        if state not in {"normal", "zoomed"}:
            try:
                self.state("normal")
            except tk.TclError:
                pass
        if geometry:
            self.geometry(geometry)
        self.update_idletasks()
        if geometry:
            self.geometry(geometry)
        self.after(50, self.release_size_constraints)

    def configure_style(self) -> None:
        self.colors, self.ui_line_height = configure_hub_style(self)
        self.preview_full_title_width = tkfont.Font(
            self, font=("Malgun Gothic", 11, "bold")
        ).measure("수신자 미리보기") + 6

    def refresh_primary_action_visual(self, *_trace_args: str) -> None:
        """Render existing run state; leave enabled state and callbacks alone."""
        button = self.__dict__.get("start_button")
        if not isinstance(button, ttk.Button):
            return
        preparing = bool(self.__dict__.get("_auto_match_waiting_to_send"))
        running = self.__dict__.get("active_run") is not None
        loading = bool(self.__dict__.get("_document_loading"))
        appearance = (
            "Busy.Accent.TButton" if preparing or running or loading else "Accent.TButton",
            "준비 중" if preparing else "처리 중" if running else "파일 읽는 중" if loading else "발송 시작",
        )
        if self.__dict__.get("_primary_action_appearance") != appearance:
            button.configure(style=appearance[0], text=appearance[1])
            self._primary_action_appearance = appearance

    def build_layout(self) -> None:
        self.option_add("*tearOff", False)
        self.build_menu_bar()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        main = ttk.Frame(self, padding=(12, 10))
        self.main_frame = main
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1, minsize=240)

        command = ttk.Frame(main, style="Card.TFrame", padding=(14, 12))
        self.command_frame = command
        command.grid(row=0, column=0, sticky="ew")
        command.columnconfigure(0, weight=1)
        ttk.Label(command, text=APP_NAME, style="CommandTitle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        actions = ttk.Frame(command, style="Surface.TFrame")
        self.primary_actions_frame = actions
        actions.grid(row=0, column=1, sticky="e")
        self.open_file_button = ttk.Button(actions, text="CSV/XLSX 열기", command=self.select_file, style="Secondary.TButton")
        self.open_file_button.grid(row=0, column=0, padx=(0, 6))
        self.start_button = ttk.Button(actions, text="발송 시작", width=8, command=self.start_sending, style="Accent.TButton")
        self.start_button.grid(row=0, column=1, padx=(0, 6))
        self.stop_button = ttk.Button(actions, text="중지", width=6, command=self.stop_sending, style="Danger.TButton")
        self.stop_button.grid(row=0, column=2, padx=(0, 12))
        self.stop_button.state(["disabled"])
        self.history_button = ttk.Button(actions, text="실행 기록", command=self.open_run_history, style="Compact.TButton")
        self.history_button.grid(row=0, column=3, padx=(0, 6))
        self.diagnostics_button = ttk.Button(actions, text="연결 점검", command=self.open_connection_diagnostics, style="Compact.TButton")
        self.diagnostics_button.grid(row=0, column=4)
        self.file_path_label = KoreanLabel(command, textvariable=self.file_var, style="CommandMuted.TLabel", anchor="w")
        self.file_path_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        readiness = ttk.Frame(main, style="Card.TFrame", padding=(12, 8))
        self.readiness_frame = readiness
        readiness.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        for column in range(4):
            readiness.columnconfigure(column, weight=1, uniform="readiness")
        self.loaded_count_label = self.status_card(readiness, 0, "발송 목록", "0건")
        self.coord_state_label = self.status_card(readiness, 1, "발송 방식", "화면 · 좌표 미설정")
        self.attachment_state_label = self.status_card(readiness, 2, "공통 첨부", "0개")
        self.schedule_state_label = self.status_card(readiness, 3, "예약", "없음")

        workspace = ttk.Frame(main)
        self.workspace_frame = workspace
        workspace.grid(row=2, column=0, sticky="nsew")
        workspace.columnconfigure(0, weight=1)
        workspace.columnconfigure(1, weight=0, minsize=340)
        workspace.rowconfigure(0, weight=1)
        preview_frame = ttk.Frame(workspace, style="Card.TFrame", padding=(12, 10))
        self.preview_frame = preview_frame
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(2, weight=1)
        header = ttk.Frame(preview_frame, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.preview_title_label = ttk.Label(header, text="수신자 미리보기", style="Section.TLabel")
        self.preview_title_label.grid(row=0, column=0, sticky="w")
        self.preview_add_button = ttk.Button(header, text="행 추가", command=self.add_preview_row, style="Compact.TButton")
        self.preview_add_button.grid(row=0, column=1, padx=(6, 0))
        ttk.Button(header, text="행 편집", command=self.edit_selected_row, style="Compact.TButton").grid(row=0, column=2, padx=(6, 0))
        ttk.Button(header, text="검색", command=self.open_search_bar, style="Compact.TButton").grid(row=0, column=3, padx=(6, 0))
        self.route_summary_label = KoreanLabel(header, textvariable=self.route_summary_var, style="SurfaceMuted.TLabel")
        self.route_summary_label.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 7))
        self.search_frame = ttk.Frame(preview_frame, style="Surface.TFrame")
        self.search_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.search_frame.columnconfigure(1, weight=1)
        ttk.Label(self.search_frame, text="검색", style="SurfaceMuted.TLabel").grid(row=0, column=0, padx=(0, 6))
        self.search_entry = ttk.Entry(self.search_frame, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(self.search_frame, text="이전", command=lambda: self.find_preview_match(-1), style="Compact.TButton").grid(row=0, column=2, padx=(6, 0))
        ttk.Button(self.search_frame, text="다음", command=lambda: self.find_preview_match(1), style="Compact.TButton").grid(row=0, column=3, padx=(6, 0))
        ttk.Button(self.search_frame, text="닫기", command=self.close_search_bar, style="Compact.TButton").grid(row=0, column=4, padx=(6, 0))
        self.search_frame.grid_remove()
        table_wrap = ttk.Frame(preview_frame, style="Surface.TFrame")
        self.table_frame = table_wrap
        table_wrap.grid(row=2, column=0, sticky="nsew")
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)
        columns = ("row", "chat", "text", "attachments", "schedule", "status", "route")
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("row", text="No.")
        self.tree.heading("chat", text="대화명")
        self.tree.heading("text", text="메시지 내용")
        self.tree.heading("attachments", text="파일")
        self.tree.heading("schedule", text="예약")
        self.tree.heading("status", text="상태")
        self.tree.heading("route", text="발송 방식")
        self.tree.column("row", width=54, anchor="center", stretch=False)
        self.tree.column("chat", width=170, stretch=False)
        self.tree.column("text", width=320, minwidth=220)
        self.tree.column("attachments", width=110, anchor="center", stretch=False)
        self.tree.column("schedule", width=132, anchor="center", stretch=False)
        self.tree.column("status", width=80, anchor="center", stretch=False)
        self.tree.column("route", width=84, anchor="center", stretch=False)
        self.tree.tag_configure("ready", background=self.colors["surface"])
        self.tree.tag_configure("running", background=self.colors["busy"])
        self.tree.tag_configure("success", background=self.colors["success_wash"])
        self.tree.tag_configure("error", background=self.colors["danger_wash"])
        self.tree.tag_configure("warning", background=self.colors["warning_wash"])
        self.tree.tag_configure("skipped", background=self.colors["soft"])
        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.table_xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=self.table_xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self.begin_cell_edit)
        self.tree.bind("<Return>", self.begin_cell_edit)
        self.tree.bind("<Delete>", self.delete_selected_rows)
        self.tree.bind("<BackSpace>", self.delete_selected_rows)
        self.tree.bind("<Control-a>", self.select_all_rows)
        self.tree.bind("<Control-c>", self.copy_selected_rows)
        self.tree.bind("<Control-v>", self.paste_rows_from_clipboard)
        self.tree.bind("<Control-z>", self.undo_preview_change)
        self.tree.bind("<Control-f>", self.open_search_bar)
        self.tree.bind("<Control-F>", self.open_search_bar)
        self.tree.bind("<Escape>", self.cancel_cell_edit)
        self.tree.bind("<Button-3>", self.show_preview_menu)
        self.tree.bind("<<TreeviewSelect>>", self.on_preview_selection_changed)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.table_xscroll.grid(row=1, column=0, sticky="ew")

        sidebar = ttk.Frame(workspace, style="Sidebar.TFrame", width=340)
        self.sidebar_frame = sidebar
        sidebar.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)
        self.sidebar_tab_var = tk.StringVar(value="attachments")
        self.sidebar_is_compact = False
        self.sidebar_tabs = ttk.Frame(sidebar, style="Sidebar.TFrame")
        self.sidebar_tabs.columnconfigure((0, 1), weight=1, uniform="tabs")
        self.attachment_tab_button = ttk.Radiobutton(self.sidebar_tabs, text="공통 첨부", value="attachments", variable=self.sidebar_tab_var, command=self.show_sidebar_tab, style="Toolbutton")
        self.attachment_tab_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.settings_tab_button = ttk.Radiobutton(self.sidebar_tabs, text="발송 설정", value="settings", variable=self.sidebar_tab_var, command=self.show_sidebar_tab, style="Toolbutton")
        self.settings_tab_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        settings_panel = ttk.Frame(sidebar, style="Card.TFrame", height=1)
        self.settings_panel = settings_panel
        settings_panel.grid(row=1, column=0, sticky="new", pady=(0, 8))
        settings_panel.grid_propagate(False)
        settings_panel.columnconfigure(0, weight=1)
        settings_panel.rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(settings_panel, background=self.colors["surface"],
                                         highlightthickness=0, borderwidth=0, width=1, height=1,
                                         yscrollincrement=max(14, self.ui_line_height))
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        self.settings_scrollbar = ttk.Scrollbar(settings_panel, orient="vertical", command=self.settings_canvas.yview)
        self.settings_scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_canvas.configure(yscrollcommand=self.settings_scrollbar.set)
        settings = ttk.Frame(self.settings_canvas, style="Surface.TFrame", padding=(10, 9))
        self.settings_content = settings
        self.settings_window_id = self.settings_canvas.create_window((0, 0), window=settings, anchor="nw")
        self.settings_canvas.bind("<Configure>", self.resize_settings_viewport)
        settings.bind("<Configure>", self.refresh_settings_scroll_region)
        settings.columnconfigure(0, weight=1)
        ttk.Label(settings, text="발송 설정", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        mode_bar = ttk.Frame(settings, style="Surface.TFrame")
        mode_bar.grid(row=1, column=0, sticky="ew")
        mode_bar.columnconfigure(0, weight=1)
        self.direct_toggle = ttk.Checkbutton(mode_bar, text="직접 전송", variable=self.direct_enabled_var, command=self.on_direct_mode_changed, style="Direct.TCheckbutton")
        self.direct_toggle.grid(row=0, column=0, sticky="w")
        self.test_one_check = ttk.Checkbutton(mode_bar, text="테스트 1건", variable=self.test_one_var, command=self.apply_test_one_limit)
        self.test_one_check.grid(row=0, column=1, sticky="e")
        self.direct_status_label = KoreanLabel(settings, textvariable=self.direct_mode_status_var, style="SurfaceMuted.TLabel", wraplength=310)
        self.direct_status_label.grid(row=2, column=0, sticky="ew", pady=(5, 6))
        mapping_bar = ttk.Frame(settings, style="Surface.TFrame")
        mapping_bar.grid(row=3, column=0, sticky="ew")
        mapping_bar.columnconfigure((0, 1), weight=1, uniform="mapping")
        self.mapping_button = ttk.Button(mapping_bar, text="자동 매칭", command=self.open_chat_mapping, style="Secondary.TButton")
        self.mapping_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.manual_mapping_button = ttk.Button(mapping_bar, text="수동 매칭", command=self.open_manual_chat_mapping, style="Secondary.TButton")
        self.manual_mapping_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        schedule_bar = ttk.Frame(settings, style="Surface.TFrame")
        schedule_bar.grid(row=4, column=0, sticky="ew", pady=(7, 0))
        schedule_bar.columnconfigure(0, weight=1)
        self.common_schedule_check = ttk.Checkbutton(schedule_bar, text="공통 예약", variable=self.common_schedule_var, command=self.refresh_preview_count)
        self.common_schedule_check.grid(row=0, column=0, columnspan=2, sticky="w")
        self.schedule_at_entry = ttk.Entry(schedule_bar, textvariable=self.schedule_at_var, width=17)
        self.schedule_at_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.schedule_at_entry.bind("<KeyRelease>", lambda _: self.refresh_preview_count())
        self.schedule_picker_button = ttk.Button(schedule_bar, text="일시 선택", command=self.choose_common_schedule, style="Compact.TButton")
        self.schedule_picker_button.grid(row=1, column=1, padx=(6, 0), pady=(4, 0))
        self.schedule_hint_label = KoreanLabel(settings, textvariable=self.schedule_mode_hint_var, style="SurfaceMuted.TLabel", wraplength=310)
        self.schedule_hint_label.grid(row=5, column=0, sticky="ew", pady=(4, 6))
        capacity_box = ttk.Frame(settings, style='Surface.TFrame')
        capacity_box.grid(row=6, column=0, sticky='ew', pady=(2, 7))
        capacity_box.columnconfigure(0, weight=1)
        self.schedule_counter_label = KoreanLabel(capacity_box, textvariable=self.schedule_counter_var, style='SurfaceMuted.TLabel', wraplength=300)
        self.schedule_counter_label.grid(row=0, column=0, sticky='ew')
        self.schedule_counter_detail_label = KoreanLabel(capacity_box, textvariable=self.schedule_counter_detail_var, style='SurfaceMuted.TLabel', wraplength=300)
        self.schedule_counter_detail_label.grid(row=1, column=0, sticky='ew', pady=(2, 4))
        self.schedule_counter_button = ttk.Button(capacity_box, text='예약 기록 새로 계산', command=lambda: self.request_schedule_capacity_refresh(manual=True), style='Compact.TButton')
        self.schedule_counter_button.grid(row=2, column=0, sticky='ew')
        self.coordinate_button = ttk.Button(settings, text="화면 좌표 설정", command=self.capture_coordinate, style="Compact.TButton")
        self.coordinate_button.grid(row=7, column=0, sticky="ew")
        self.retry_failed_button = ttk.Button(settings, text=format_retry_failed_button_label(0), command=self.retry_failed_rows, style="Secondary.TButton")
        self.retry_failed_button.grid(row=8, column=0, sticky="ew", pady=(5, 0))
        self.retry_failed_button.state(["disabled"])
        pending_scroll_widgets = [self.settings_canvas]
        while pending_scroll_widgets:
            widget = pending_scroll_widgets.pop()
            widget.bind("<MouseWheel>", self.on_settings_mousewheel, add="+")
            widget.bind("<FocusIn>", lambda event: self.scroll_settings_to_widget(event.widget), add="+")
            pending_scroll_widgets.extend(widget.winfo_children())

        attachment_panel = ttk.Frame(sidebar, style="SoftCard.TFrame", padding=(12, 10))
        self.attachment_panel = attachment_panel
        attachment_panel.grid(row=2, column=0, sticky="nsew")
        attachment_panel.columnconfigure(0, weight=1)
        attachment_panel.rowconfigure(5, weight=1)
        self.attachment_title_label = ttk.Label(attachment_panel, text="공통 첨부", style="PanelTitle.TLabel")
        self.attachment_title_label.grid(row=0, column=0, sticky="w")
        self.attachment_help = KoreanLabel(attachment_panel, text=format_attachment_panel_help(()), style="SoftMuted.TLabel", wraplength=310)
        self.attachment_help.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        file_actions = ttk.Frame(attachment_panel, style="Soft.TFrame")
        file_actions.grid(row=2, column=0, sticky="ew")
        file_actions.columnconfigure((0, 1), weight=1, uniform="file_actions")
        self.attachment_pick_button = ttk.Button(file_actions, text="첨부 파일 선택", command=self.attach_files_to_all_rows, style="Secondary.TButton")
        self.attachment_pick_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.attachment_clear_button = ttk.Button(file_actions, text="첨부 초기화", command=self.clear_all_attachments, style="Secondary.TButton")
        self.attachment_clear_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.attachment_path_label = ttk.Label(attachment_panel, text="공통 첨부 경로", style="SoftMuted.TLabel")
        self.attachment_path_label.grid(row=3, column=0, sticky="w", pady=(7, 3))
        path_row = ttk.Frame(attachment_panel, style="Soft.TFrame")
        path_row.grid(row=4, column=0, sticky="ew")
        path_row.columnconfigure(0, weight=1)
        self.attachment_path_entry = ttk.Entry(path_row, textvariable=self.attachment_path_var, width=15)
        self.attachment_path_entry.grid(row=0, column=0, sticky="ew")
        self.attachment_path_entry.bind("<Return>", self.apply_attachment_path_input)
        self.attachment_path_button = ttk.Button(path_row, text="경로 적용", command=self.apply_attachment_path_input, style="Compact.TButton")
        self.attachment_path_button.grid(row=0, column=1, padx=(6, 0))
        file_list_frame = ttk.Frame(attachment_panel, style="Surface.TFrame")
        self.attachment_list_frame = file_list_frame
        file_list_frame.grid(row=5, column=0, sticky="nsew", pady=(7, 0))
        file_list_frame.columnconfigure(0, weight=1)
        file_list_frame.rowconfigure(0, weight=1, minsize=max(70, self.ui_line_height * 3))
        self.attachment_list = tk.Listbox(file_list_frame, height=4, relief="flat", exportselection=False, font=("Malgun Gothic", 10), borderwidth=0, background=self.colors["surface"], foreground=self.colors["text"], highlightthickness=1, highlightbackground=self.colors["line"], highlightcolor=self.colors["focus"], selectbackground=self.colors["select"], selectforeground=self.colors["select_text"])
        self.attachment_list.grid(row=0, column=0, sticky="nsew")
        self.attachment_empty_label = KoreanLabel(file_list_frame, text="파일을 여기에 놓으세요\n이미지·PDF 공통 첨부", style="DropEmpty.TLabel", wraplength=290, wrap_mode="balance")
        self.attachment_empty_label.grid(row=0, column=0, sticky="nsew")
        self.attachment_drop_hint = KoreanLabel(attachment_panel, text="", style="SoftMuted.TLabel", wraplength=310)
        self.attachment_drop_hint.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.refresh_attachment_panel()

        log_frame = ttk.Frame(main, style="Card.TFrame", padding=(12, 8))
        self.log_frame = log_frame
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(2, weight=1)
        screen_guard = ttk.Frame(log_frame, style="Soft.TFrame", padding=(8, 4))
        self.screen_guard_frame = screen_guard
        screen_guard.grid(row=0, column=0, sticky="ew")
        screen_guard.columnconfigure(0, weight=1)
        self.screen_use_state_label = KoreanLabel(screen_guard, textvariable=self.screen_use_state_var, style="ScreenIdle.TLabel", anchor="w", wraplength=700)
        self.screen_use_state_label.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.screen_use_stop_button = ttk.Button(screen_guard, text="화면 사용 중단", command=self.request_screen_use_stop, style="Danger.TButton")
        self.screen_use_stop_button.grid(row=0, column=1, sticky="e")
        self.screen_use_stop_button.state(["disabled"])
        log_header = ttk.Frame(log_frame, style="Surface.TFrame")
        self.log_header_frame = log_header
        log_header.grid(row=1, column=0, sticky="ew", pady=(4, 3))
        log_header.columnconfigure(0, weight=1)
        self.summary_label = KoreanLabel(log_header, textvariable=self.summary_var, style="SurfaceMuted.TLabel")
        self.summary_label.grid(row=0, column=0, sticky="w")
        self.progress_state_label = ttk.Label(log_header, text="대기", style="SurfaceMuted.TLabel")
        self.progress_state_label.grid(row=0, column=1, padx=(8, 0))
        ttk.Progressbar(log_header, variable=self.progress_var, maximum=100, length=110).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(log_header, textvariable=self.elapsed_var, style="SurfaceMuted.TLabel").grid(row=0, column=3, padx=(8, 0))
        self.status_label = KoreanLabel(log_header, textvariable=self.status_var, style="SurfaceMuted.TLabel")
        self.status_label.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        self.log_text = ScrolledText(log_frame, height=2, font=("Malgun Gothic", 9), wrap="word", relief="flat", borderwidth=0, padx=8, pady=5, background=self.colors["soft"], foreground=self.colors["text"], insertbackground=self.colors["text"], selectbackground=self.colors["select"], selectforeground=self.colors["select_text"], highlightthickness=1, highlightbackground=self.colors["line"], highlightcolor=self.colors["focus"])
        self.log_text.grid(row=2, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        self.bind_all("<Control-f>", self.open_search_bar)
        self.bind_all("<Control-F>", self.open_search_bar)
        self.search_entry.bind("<Return>", lambda _: self.find_preview_match(1))
        self.search_entry.bind("<Shift-Return>", lambda _: self.find_preview_match(-1))
        self.search_entry.bind("<Escape>", self.close_search_bar)
        for label in (self.file_path_label, self.status_label, self.summary_label,
                      self.screen_use_state_label, self.attachment_help,
                      self.coord_state_label, self.schedule_state_label):
            # Text can wrap after a run/status change without resizing the
            # root. Recompute the available table/log space in that case too.
            label.bind("<Configure>", lambda _event: self.schedule_layout_rebalance())

    def status_card(self, parent: ttk.Frame, column: int, title: str, value: str) -> ttk.Label:
        frame = ttk.Frame(parent, style="Surface.TFrame", padding=(4, 2))
        frame.grid(row=0, column=column, sticky="ew", padx=(0, 8 if column < 3 else 0))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=title, style="SurfaceMuted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 7))
        label = KoreanLabel(frame, text=value, style="Metric.TLabel")
        label.grid(row=0, column=1, sticky="ew")
        return label

    def schedule_layout_rebalance(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is not self:
            return
        if self.layout_after_id is not None:
            self.after_cancel(self.layout_after_id)
        self.layout_after_id = self.after(40, self.rebalance_layout)

    def rebalance_layout(self) -> None:
        self.layout_after_id = None
        if self.__dict__.get("_layout_rebalancing"):
            return
        self._layout_rebalancing = True
        try:
            self._rebalance_layout_now()
        finally:
            self._layout_rebalancing = False

    def _configure_layout_widget(self, widget, **options) -> None:
        cache = self.__dict__.setdefault("_layout_widget_options", {})
        previous = cache.setdefault(widget, {})
        changed = {key: value for key, value in options.items() if previous.get(key) != value}
        if changed:
            widget.configure(**changed)
            previous.update(changed)

    def _rebalance_layout_now(self) -> None:
        if not hasattr(self, "main_frame") or not hasattr(self, "log_frame"):
            return
        height = max(self.winfo_height(), MIN_WINDOW_SIZE[1])
        width = max(self.winfo_width(), MIN_WINDOW_SIZE[0])
        line_height = self.__dict__.get("ui_line_height", 20)
        sidebar_cap = round(420 * self.__dict__.get('ui_display_scale', 1.0))
        sidebar_width = min(sidebar_cap, max(320, round(width * 0.27), line_height * 15))
        self._configure_layout_widget(self.main_frame, padding=(10, 8) if width < 1180 else (12, 10))
        self._configure_layout_widget(self.command_frame, padding=(10, 8))
        self._configure_layout_widget(self.file_path_label, wraplength=max(300, width - 56))
        self._configure_layout_widget(self.screen_use_state_label, wraplength=max(320, width - self.screen_use_stop_button.winfo_reqwidth() - 78))
        self._configure_layout_widget(self.status_label, wraplength=max(400, width - 70))
        self._configure_layout_widget(self.summary_label, wraplength=max(300, width - 360))
        self._configure_layout_widget(self.route_summary_label, wraplength=max(260, width - sidebar_width - 90))
        for label_name in ("attachment_help", "attachment_drop_hint"):
            self._configure_layout_widget(getattr(self, label_name), wraplength=sidebar_width - 34)
        settings_wrap = sidebar_width - self.settings_scrollbar.winfo_reqwidth() - 28
        self._configure_layout_widget(self.direct_status_label, wraplength=settings_wrap)
        self._configure_layout_widget(self.schedule_hint_label, wraplength=settings_wrap)
        for name in ('schedule_counter_label', 'schedule_counter_detail_label'):
            label = self.__dict__.get(name)
            if label is not None:
                self._configure_layout_widget(label, wraplength=settings_wrap)
        self._configure_layout_widget(self.attachment_empty_label, wraplength=sidebar_width - 58)
        metric_width = max(85, (width - 90) // 4 - 85)
        for label in (self.loaded_count_label, self.coord_state_label, self.attachment_state_label, self.schedule_state_label):
            self._configure_layout_widget(label, wraplength=metric_width)
        self.workspace_frame.columnconfigure(1, minsize=sidebar_width, weight=0)
        self._configure_layout_widget(self.sidebar_frame, width=sidebar_width)
        self.update_idletasks()
        title_width = max(0, self.preview_add_button.winfo_x() - 6)
        self._configure_layout_widget(self.preview_title_label, text="수신자 미리보기" if title_width >= self.preview_full_title_width else "수신자")
        log_height = max(132, self.screen_guard_frame.winfo_reqheight() + self.log_header_frame.winfo_reqheight() + max(45, line_height * 2) + 28)
        self.main_frame.rowconfigure(3, minsize=log_height, weight=0)
        self.main_frame.rowconfigure(2, weight=1, minsize=240)
        self._configure_layout_widget(self.log_frame, height=log_height)
        self.log_frame.grid_propagate(False)
        self.update_idletasks()
        available_height = self.workspace_frame.winfo_height()
        settings_height = self.settings_content.winfo_reqheight()
        stacked_height = settings_height + self.attachment_panel.winfo_reqheight() + 8
        compact = width < 1180 or available_height < stacked_height
        self.sidebar_is_compact = compact
        self.sidebar_frame.rowconfigure(1, weight=1 if compact else 0)
        self.sidebar_frame.rowconfigure(2, weight=0 if compact else 1)
        if compact:
            self._configure_layout_widget(self.settings_panel, height=1)
            self.sidebar_tabs.grid(row=0, column=0, sticky="ew", pady=(0, 7))
            selected = self.sidebar_tab_var.get()
            for name, panel in (("settings", self.settings_panel), ("attachments", self.attachment_panel)):
                if selected == name:
                    panel.grid(row=1, column=0, sticky="nsew", pady=0)
                else:
                    panel.grid_remove()
        else:
            self._configure_layout_widget(self.settings_panel, height=settings_height)
            self.sidebar_tabs.grid_remove()
            self.settings_panel.grid(row=1, column=0, sticky="new", pady=(0, 8))
            self.attachment_panel.grid(row=2, column=0, sticky="nsew", pady=0)

    def show_sidebar_tab(self, name: str | None = None) -> None:
        if name in {"settings", "attachments"}:
            self.sidebar_tab_var.set(name)
        self.rebalance_layout()

    def resize_settings_viewport(self, event: tk.Event) -> None:
        self.settings_canvas.itemconfigure(self.settings_window_id, width=max(1, event.width))
        self.refresh_settings_scroll_region()

    def refresh_settings_scroll_region(self, event: tk.Event | None = None) -> None:
        region = self.settings_canvas.bbox("all")
        if region is not None:
            self.settings_canvas.configure(scrollregion=region)
            if region[3] <= self.settings_canvas.winfo_height():
                self.settings_canvas.yview_moveto(0)

    def on_settings_mousewheel(self, event: tk.Event) -> str | None:
        region = self.settings_canvas.bbox("all")
        if region is None or region[3] <= self.settings_canvas.winfo_height():
            return None
        delta = getattr(event, "delta", 0)
        if not delta:
            return None
        steps = int(-delta / 120) or (-1 if delta > 0 else 1)
        self.settings_canvas.yview_scroll(steps, "units")
        return "break"

    def scroll_settings_to_widget(self, widget: tk.Widget) -> None:
        """Keep keyboard focus inside the visible settings viewport."""
        if widget is self.settings_canvas or not widget.winfo_ismapped():
            return
        canvas = self.settings_canvas
        region = canvas.bbox("all")
        if region is None:
            return
        top = widget.winfo_rooty() - self.settings_content.winfo_rooty()
        bottom = top + widget.winfo_height()
        view_top = canvas.canvasy(0)
        view_bottom = view_top + canvas.winfo_height()
        extent = max(1, region[3] - region[1])
        margin = max(4, int(canvas.cget("yscrollincrement")))
        if top < view_top:
            canvas.yview_moveto(max(0, top - margin) / extent)
        elif bottom > view_bottom:
            canvas.yview_moveto(max(0, bottom + margin - canvas.winfo_height()) / extent)

    def build_menu_bar(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="파일 찾기", command=self.select_file)
        file_menu.add_command(label="다시 읽기", command=self.request_document_load)
        file_menu.add_separator()
        file_menu.add_command(label="편집본 CSV 저장", command=self.request_preview_csv_export)
        file_menu.add_command(label="예시 CSV 열기", command=self.load_example)
        file_menu.add_separator()
        file_menu.add_command(label="로그 폴더 열기", command=self.open_log_folder)
        file_menu.add_command(label="실행 기록 / 중단 작업 복원", command=self.open_run_history)
        menubar.add_cascade(label="파일", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        for label, command in (
            ("행 추가", self.add_preview_row),
            ("행 복제", self.duplicate_selected_rows),
            ("행 삭제", self.delete_selected_rows),
            ("파일 첨부", self.attach_files_to_all_rows),
            ("파일 초기화", self.clear_all_attachments),
            ("검색", self.open_search_bar),
            ("복사", self.copy_selected_rows),
            ("붙여넣기", self.paste_rows_from_clipboard),
            ("되돌리기", self.undo_preview_change),
            ("큰 창 수정", self.edit_selected_row),
        ):
            edit_menu.add_command(label=label, command=command)
        menubar.add_cascade(label="편집", menu=edit_menu)

        run_menu = tk.Menu(menubar, tearoff=False)
        run_menu.add_checkbutton(label="직접 전송", variable=self.direct_enabled_var, command=self.on_direct_mode_changed)
        run_menu.add_command(label="자동 매칭", command=self.open_chat_mapping)
        run_menu.add_command(label="수동 매칭", command=self.open_manual_chat_mapping)
        run_menu.add_separator()
        run_menu.add_command(label="화면 좌표 설정", command=self.capture_coordinate)
        run_menu.add_command(label="실행 시작", command=self.start_sending)
        run_menu.add_command(label="직접 실패 2차 전송", command=self.retry_failed_rows)
        run_menu.add_command(label="즉시 중지", command=self.stop_sending)
        menubar.add_cascade(label="작업", menu=run_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="도움말", command=self.show_help)
        help_menu.add_command(label="연결 준비 점검 / 인증 복구", command=self.open_connection_diagnostics)
        menubar.add_cascade(label="도움말", menu=help_menu)
        self.configure(menu=menubar)
        self.build_preview_menu()

    def build_preview_menu(self) -> None:
        self.preview_menu = tk.Menu(self, tearoff=False)
        for label, command in (
            ("행 추가", self.add_preview_row),
            ("행 복제", self.duplicate_selected_rows),
            ("행 삭제", self.delete_selected_rows),
            ("파일 첨부", self.attach_files_to_all_rows),
            ("파일 초기화", self.clear_all_attachments),
            ("검색", self.open_search_bar),
            ("복사", self.copy_selected_rows),
            ("붙여넣기", self.paste_rows_from_clipboard),
            ("되돌리기", self.undo_preview_change),
            ("큰 창 수정", self.edit_selected_row),
        ):
            self.preview_menu.add_command(label=label, command=command)

    def show_preview_menu(self, event: tk.Event) -> str:
        item_id = self.tree.identify_row(event.y)
        if item_id and item_id not in self.tree.selection():
            self.tree.selection_set(item_id)
            self.tree.focus(item_id)
        self.preview_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def sidebar_step(self, parent: ttk.Frame, number: str, text: str) -> None:
        row = ttk.Frame(parent, style="Sidebar.TFrame")
        row.pack(fill="x", pady=7)
        badge = tk.Label(row, text=number, width=2, height=1, bg=self.colors["accent"], fg="#ffffff", font=("Malgun Gothic", 10, "bold"))
        badge.pack(side="left", padx=(0, 10))
        tk.Label(row, text=text, bg="#eef5f4", fg=self.colors["text"], font=("Malgun Gothic", 10, "bold")).pack(side="left")

    def option_spinbox(self, parent: ttk.Frame, row: int, label: str, variable: tk.Variable, low: float, high: float, increment: float = 1) -> None:
        ttk.Label(parent, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(parent, from_=low, to=high, increment=increment, textvariable=variable, width=10).grid(row=row, column=1, sticky="w", pady=(10, 0))

    def load_relaxed_config(self) -> dict[str, Any]:
        config, warning = load_relaxed_config_data(CONFIG_PATH)
        self.config_warning = warning
        return config

    def load_direct_config(self) -> DirectKakaoConfig:
        try:
            return DirectKakaoConfig.load(DIRECT_CONFIG_PATH)
        except (OSError, ValueError) as exc:
            self.direct_config_warning = f"직접 전송 설정을 읽지 못해 안전하게 OFF로 시작합니다. {exc}"
            return DirectKakaoConfig(enabled=False)

    def surface_config_warning(self) -> None:
        warnings = [
            warning
            for warning in (
                self.__dict__.get("config_warning"),
                self.__dict__.get("direct_config_warning"),
            )
            if warning
        ]
        if warnings:
            self.status_var.set("설정 파일 경고")
            for warning in warnings:
                self.log("설정 경고", warning)

    def save_coordinates_from_fields(self, quiet: bool = False) -> None:
        try:
            x = int(float(self.coord_x_var.get()))
            y = int(float(self.coord_y_var.get()))
        except ValueError:
            messagebox.showerror("좌표 오류", "X/Y 좌표는 숫자로 입력해 주세요.")
            return
        self.config_data["search_box"] = search_box_config_from_position(x, y, list(self.config_data.get("app_titles") or []))
        save_config(CONFIG_PATH, self.config_data)
        self.refresh_coordinate_labels()
        if not quiet:
            self.log("설정", f"검색창 좌표를 저장했습니다. X={x}, Y={y}")

    def refresh_coordinate_labels(self) -> None:
        x = int(float(self.coord_x_var.get() or 0))
        y = int(float(self.coord_y_var.get() or 0))
        state = "설정됨" if x > 0 and y > 0 else "미설정"
        if self.direct_enabled_var.get():
            suffix = state if state == "미설정" else f"{x}, {y}"
            self.coord_state_label.configure(text=f"직접 ON · 보조 {suffix}")
        else:
            self.coord_state_label.configure(text=f"화면 · {state}" if state == "미설정" else f"화면 · {x}, {y}")

    def save_direct_settings(self) -> None:
        self.direct_config.enabled = bool(self.direct_enabled_var.get())
        self.direct_config.mappings = normalize_mappings(self.direct_mappings)
        mapped_ids = set(self.direct_config.mappings.values())
        self.direct_config.mapping_labels = normalize_mapping_labels(
            {
                chat_id: label
                for chat_id, label in self.direct_mapping_labels.items()
                if chat_id in mapped_ids
            }
        )
        self.direct_config.guarded_mapping_ids = normalize_guarded_mapping_ids(
            chat_id
            for chat_id in self.__dict__.get(
                "guarded_mapping_ids",
                getattr(self.direct_config, "guarded_mapping_ids", ()),
            )
            if chat_id in mapped_ids
        )
        self.direct_config.save(DIRECT_CONFIG_PATH)
        self.direct_mappings = dict(self.direct_config.mappings)
        self.direct_mapping_labels = dict(self.direct_config.mapping_labels)
        self.guarded_mapping_ids = tuple(self.direct_config.guarded_mapping_ids)

    def on_direct_mode_changed(self, persist: bool = True) -> None:
        direct_enabled = bool(self.direct_enabled_var.get())
        if direct_enabled and persist and not self.direct_warning_confirmed:
            accepted = messagebox.askyesno(
                "직접 전송 실험 기능",
                "직접 전송은 카카오가 공식 제공하는 제3자 API가 아닙니다.\n"
                "계정·클라이언트 버전에 따라 차단되거나 동작이 바뀔 수 있으며 탐지되지 않는다고 보장할 수 없습니다.\n\n"
                "즉시 텍스트와 첨부 파일을 chat ID로 직접 전송하고, 예약 메시지는 기존 PC 카카오톡 화면으로 처리합니다. 계속할까요?",
            )
            if not accepted:
                self.direct_enabled_var.set(False)
                direct_enabled = False
            else:
                self.direct_warning_confirmed = True
        if persist:
            try:
                self.save_direct_settings()
            except (OSError, ValueError) as exc:
                self.direct_enabled_var.set(False)
                self.direct_config.enabled = False
                direct_enabled = False
                messagebox.showerror("직접 전송 설정", f"설정을 저장하지 못해 직접 전송을 OFF로 유지합니다.\n\n{exc}")
        if direct_enabled:
            self.direct_mode_status_var.set("ON · 즉시 텍스트·첨부 직접 / 예약은 화면 자동 등록")
            self.schedule_mode_hint_var.set("예약 자동 등록 · 대상/시간/본문 자동 대조")
        else:
            self.direct_mode_status_var.set("OFF · PC 카톡 화면 제어")
            self.schedule_mode_hint_var.set("예약 자동 등록 · 대상/시간/본문 자동 대조")
        if hasattr(self, "coordinate_button"):
            # Keep the control discoverable in both modes.  Direct mode still
            # needs this coordinate for reservations and the
            # operator-triggered second-pass screen send.
            self.coordinate_button.configure(text="화면 좌표 설정")
            self.coordinate_button.state(["!disabled"])
        if "coord_state_label" in self.__dict__:
            self.refresh_coordinate_labels()
        self.refresh_delivery_routes()
        if direct_enabled and persist and getattr(self, "rows", None):
            # A file may already be loaded when the operator turns direct
            # mode on. Reconcile immediate text and attachment rows. Enabling a mode
            # must not unexpectedly open or focus a PC KakaoTalk window;
            # explicit manual mapping retains the opt-in PC fallback path.
            names = self.direct_names_for_selected(tuple(enumerate(self.rows)))
            if names:
                self.request_auto_chat_matches(names, reason="mode_enabled")

    def _screen_use_is_active(self) -> bool:
        # Some shutdown tests (and defensive close paths) construct the app
        # without running Tk.__init__.  Reading through tkinter's __getattr__
        # in that state recurses while looking for ``tk``; inspect our own
        # instance state directly instead.
        return bool(self.__dict__.get("_screen_use_tokens", {}))

    def _render_screen_use_guard(self) -> None:
        """Render the stable screen-use guard row on the Tk thread."""

        active = self._screen_use_is_active()
        stop_requested = bool(getattr(self, "_screen_use_stop_requested", False))
        if not active:
            text = "화면 사용 안 함 · 서버 자동매칭은 화면을 사용하지 않습니다"
            style = "ScreenIdle.TLabel"
        elif stop_requested:
            text = "화면 사용 중단 요청됨 · 현재 채팅방 정리 후 중단합니다 (Shift+Esc)"
            style = "ScreenStop.TLabel"
        else:
            text = "화면 사용 중 · Shift+Esc 또는 [화면 사용 중단]을 누르면 현재 채팅방 정리 후 중단합니다"
            style = "ScreenActive.TLabel"
        state_var = getattr(self, "screen_use_state_var", None)
        if state_var is not None:
            state_var.set(text)
        state_label = getattr(self, "screen_use_state_label", None)
        if state_label is not None:
            try:
                state_label.configure(style=style)
            except (AttributeError, tk.TclError):
                pass
        stop_button = getattr(self, "screen_use_stop_button", None)
        if stop_button is not None:
            try:
                stop_button.state(["!disabled"] if active and not stop_requested else ["disabled"])
            except (AttributeError, tk.TclError):
                pass

    def _start_screen_use_hotkey_poll(self) -> None:
        """Poll Shift+Esc only while at least one PC screen session is live."""

        if not self._screen_use_is_active():
            return
        if getattr(self, "_screen_use_key_reader", None) is None:
            self._screen_use_key_reader = windows_async_key_state_reader()
        if getattr(self, "_screen_use_poll_after_id", None) is None:
            try:
                self._screen_use_poll_after_id = self.after(
                    SCREEN_USE_POLL_MS,
                    self.poll_screen_use_hotkey,
                )
            except (AttributeError, tk.TclError):
                self._screen_use_poll_after_id = None

    def poll_screen_use_hotkey(self) -> None:
        """Handle one debounced global Shift+Esc state sample."""

        self._screen_use_poll_after_id = None
        if not self._screen_use_is_active():
            self._screen_use_key_down = False
            return
        is_down = shift_escape_is_down(getattr(self, "_screen_use_key_reader", None))
        was_down = bool(getattr(self, "_screen_use_key_down", False))
        self._screen_use_key_down = is_down
        if is_down and not was_down:
            self.request_screen_use_stop(source="Shift+Esc")
        self._start_screen_use_hotkey_poll()

    def _on_screen_use_hotkey(self, _event: tk.Event | None = None) -> str | None:
        """Handle the local Tk binding without affecting inactive operations."""

        if self._screen_use_is_active():
            self.request_screen_use_stop(source="Shift+Esc")
            return "break"
        return None

    def request_screen_use_stop(self, *, source: str = "화면 사용 중단") -> bool:
        """Request stop for every active PC fallback session.

        This intentionally does not call ``cancel_auto_chat_matches`` and does
        not touch ``active_run``.  The resolver keeps the auto-match generation
        alive until its current room is closed and its safe-list reset have
        completed; its normal result event then reports ``callback(False)``.
        """

        sessions = dict(getattr(self, "_screen_use_tokens", {}))
        if not sessions:
            return False
        for _token, (_generation, cancel_event) in sessions.items():
            if cancel_event is not None:
                cancel_event.set()
        if bool(getattr(self, "_screen_use_stop_requested", False)):
            return True
        self._screen_use_stop_requested = True
        self._render_screen_use_guard()
        status_var = getattr(self, "status_var", None)
        if status_var is not None:
            status_var.set("화면 사용 중단 요청됨 · 현재 채팅방 정리 중")
        log = getattr(self, "log", None)
        if callable(log):
            log("화면 사용", f"{source}: 현재 채팅방 정리 후 중단합니다.")
        return True

    def _make_screen_use_callback(
        self,
        generation: int,
        token: str,
    ) -> Callable[[bool, threading.Event, threading.Event | None], None]:
        """Create a worker-safe callback that only posts a Tk queue event."""

        def callback(
            active: bool,
            cancel_event: threading.Event,
            acknowledgement: threading.Event | None = None,
        ) -> None:
            self.post(
                "screen_use_state",
                (token, generation, bool(active), cancel_event, acknowledgement),
            )

        return callback

    def handle_screen_use_state(self, payload: Any) -> None:
        """Track one tokenized screen-use transition on the Tk thread."""

        token: str
        generation: int
        active: bool
        cancel_event: threading.Event | None
        acknowledgement: threading.Event | None
        try:
            if isinstance(payload, Mapping):
                token = str(payload.get("token") or "")
                generation = int(payload.get("generation") or 0)
                active = bool(payload.get("active"))
                cancel_event = payload.get("cancel_event")
                acknowledgement = payload.get("ack")
            else:
                token, generation, active, cancel_event, acknowledgement = payload
                token = str(token or "")
                generation = int(generation)
                active = bool(active)
        except (TypeError, ValueError):
            return

        if not token:
            if acknowledgement is not None:
                acknowledgement.set()
            return
        try:
            if not isinstance(getattr(self, "_screen_use_tokens", None), dict):
                self._screen_use_tokens = {}
            if active:
                if cancel_event is None:
                    cancel_event = threading.Event()
                if bool(getattr(self, "_screen_use_stop_requested", False)):
                    # A second/stale session can become active while an older
                    # room is still cleaning up.  The operator's stop request
                    # covers it too; never let that later session open a room.
                    cancel_event.set()
                self._screen_use_tokens[token] = (generation, cancel_event)
                self._render_screen_use_guard()
                # Force the warning row through Tk's idle layout before
                # acknowledging the worker.  The resolver will not call
                # focus/open until this acknowledgement arrives.
                if "tk" in self.__dict__:
                    try:
                        self.update_idletasks()
                    except (AttributeError, tk.TclError):
                        pass
                self._start_screen_use_hotkey_poll()
            else:
                self._screen_use_tokens.pop(token, None)
                if not self._screen_use_tokens:
                    self._screen_use_stop_requested = False
                    self._screen_use_key_down = False
                    self._screen_use_key_reader = None
                    poll_id = getattr(self, "_screen_use_poll_after_id", None)
                    if poll_id is not None:
                        try:
                            self.after_cancel(poll_id)
                        except (AttributeError, tk.TclError):
                            pass
                        self._screen_use_poll_after_id = None
                self._render_screen_use_guard()
        finally:
            if acknowledgement is not None:
                acknowledgement.set()














    def select_file(self) -> None:
        if self.document_change_locked():
            return
        path = filedialog.askopenfilename(
            title="발송 파일 선택",
            initialdir=str(default_downloads_dir()),
            filetypes=(("CSV 또는 XLSX", "*.csv *.xlsx"), ("CSV", "*.csv"), ("Excel", "*.xlsx"), ("모든 파일", "*.*")),
        )
        if not path:
            return
        self.request_document_load(Path(path))

    def load_example(self) -> None:
        if self.document_change_locked():
            return
        example = Path(__file__).resolve().parent / "examples" / "messages.csv"
        if not example.exists():
            messagebox.showerror("예시 파일 없음", f"예시 파일을 찾을 수 없습니다:\n{example}")
            return
        self.request_document_load(example)

    def reload_file(self, candidate_path: Path | None = None) -> None:
        """Synchronous compatibility entry; UI commands use request_document_load."""
        if self.document_change_locked():
            return
        self.cancel_document_load()
        path = candidate_path or self.file_path
        if not path:
            messagebox.showinfo("파일 선택", "먼저 CSV 또는 XLSX 파일을 선택해 주세요.")
            return
        try:
            document = self._read_document(path)
        except Exception as exc:
            messagebox.showerror("파일 읽기 실패", str(exc))
            self.log("오류", f"파일을 읽지 못했습니다: {exc}")
            return
        self.apply_loaded_document(document)

    def _read_document(self, path: Path):
        return load_document(path, loader=load_messages)

    def _new_schedule_capacity_store(self):
        return ScheduleCapacity(SCHEDULE_JOURNAL_PATH)

    def set_document_loading(self, loading: bool) -> None:
        """Lock mutations while retaining file selection and cancellation."""
        if loading:
            self.cancel_cell_edit()
        locked = bool(loading or self.__dict__.get("active_run") is not None
                      or self.__dict__.get("_auto_match_waiting_to_send"))
        self.set_attachment_edit_state(locked)
        for name in ("start_button", "direct_toggle", "mapping_button", "manual_mapping_button"):
            widget = self.__dict__.get(name)
            if widget is not None:
                widget.state(["disabled"] if locked else ["!disabled"])
        stop = self.__dict__.get("stop_button")
        if stop is not None:
            stop.state(["!disabled"] if locked else ["disabled"])
        self.refresh_primary_action_visual()

    def apply_loaded_document(self, document) -> None:
        """Commit one completed read on the Tk thread without reading again."""
        self.cancel_auto_chat_matches()
        self.file_path = document.source_path
        self.file_var.set(str(self.file_path))
        self.rows = list(document.rows)
        self._restored_retry_only = False
        self._recovery_of = None
        self._recovery_source_indexes = {}
        self._recovery_sources_by_number = {}
        self._recovery_retry_records_by_number = {}
        self._last_run_sources_by_number = {}
        self.results = []
        self._progress_results = {}
        self._running_row_indexes = set()
        self.run_total = 0
        self.run_row_indexes = None
        self.undo_stack = []
        self.populate_table()
        self.refresh_preview_count()
        self.update_summary()
        scheduled_count = sum(1 for row in self.rows if row.scheduled_at is not None)
        schedule_note = f", 예약 {scheduled_count}건" if scheduled_count else ""
        self.log("확인", f"{self.file_path.name} 파일에서 {len(self.rows)}건{schedule_note}을 불러왔습니다.")
        if bool(self.direct_enabled_var.get()):
            names = self.direct_names_for_selected(list(enumerate(self.rows)))
            if names:
                # CSV/XLSX load is allowed to query the tablet/server chat
                # list only.  No PC config is passed, so no resolver,
                # pyautogui, focus, click, hotkey, typing, or chat open/close
                # can occur on this path.
                self.request_auto_chat_matches(names, reason="file_load")

    def document_change_locked(self) -> bool:
        """Keep one send result snapshot bound to the rows that created it."""

        worker = self.__dict__.get("worker")
        if (self.__dict__.get("active_run") is None
                and not (worker is not None and worker.is_alive())
                and not self.__dict__.get("_auto_match_waiting_to_send")):
            return False
        messagebox.showinfo(
            "실행 결과 정리 중",
            "현재 발송 실행의 결과 처리가 끝난 뒤 파일을 변경해 주세요.",
        )
        return True

    def preview_edit_locked(self) -> bool:
        if self.__dict__.get("_document_loading"):
            messagebox.showinfo("파일 읽는 중", "파일 읽기가 끝난 뒤 미리보기를 수정해 주세요. [중지]로 읽기를 취소할 수 있습니다.")
            return True
        worker = self.__dict__.get("worker")
        if self.__dict__.get("active_run") is not None or (worker and worker.is_alive()):
            messagebox.showinfo("실행 중", "발송 중에는 미리보기를 수정할 수 없습니다.")
            return True
        if getattr(self, "_auto_match_waiting_to_send", False):
            messagebox.showinfo("채팅방 확인 중", "발송 직전 채팅방 확인이 끝난 뒤 미리보기를 수정해 주세요.")
            return True
        return False

    def push_undo_snapshot(self) -> None:
        self.undo_stack.append(list(self.rows))
        if len(self.undo_stack) > 30:
            self.undo_stack.pop(0)

    def reset_preview_results(self) -> None:
        # Row numbers survive edits, deletion and undo. Preserve each restored
        # row's durable origin instead of silently detaching it on an edit.
        retry_records = self.__dict__.get("_recovery_retry_records_by_number", {})
        self.results = [dict(retry_records[row.row_number], row_index=index,
                             chat_name=row.chat_name, attachments=row.attachment_count)
                        for index, row in enumerate(self.rows) if row.row_number in retry_records]
        self._progress_results = {}
        self._running_row_indexes = set()
        self._restored_retry_only = bool(self.results)
        self.run_total = len(self.rows) if self.results else 0
        self.run_row_indexes = None
        self.progress_var.set(0)
        self.update_summary()

    def refresh_preview_count(self) -> None:
        source = self.file_path.name if self.file_path else "편집 중"
        self.loaded_count_label.configure(text=f"{len(self.rows)}건")
        attachment_count, attachment_text = attachment_status_text(self.rows)
        self.attachment_var.set(attachment_text)
        if hasattr(self, "attachment_state_label"):
            self.attachment_state_label.configure(text=attachment_count)
        self.refresh_schedule_state_label()
        self.refresh_delivery_routes()
        if self.file_path:
            prefix = "기록 복원 · " if self.__dict__.get("_recovery_of") else ""
            self.file_var.set(prefix + str(self.file_path))

    def refresh_delivery_routes(self) -> None:
        if "route_summary_var" not in self.__dict__:
            return
        try:
            common = self.common_schedule_datetime()
        except ValueError:
            self.__dict__.pop("_delivery_routes", None)
            self.route_summary_var.set("예약일시 확인 필요")
            return
        active = self.__dict__.get("active_run") is not None
        records = list(self.__dict__.get("_progress_results", {}).values()) if active else self.results
        excluded = [r.get("chat_name", "") for r in records if r.get("transport") == "excluded"]
        routes = delivery_routes(self.rows, common, bool(self.direct_enabled_var.get()), self.direct_mappings, excluded)
        if active and self.__dict__.get("_active_send_mode") == "macro_retry":
            for index in self.__dict__.get("run_row_indexes", ()) or ():
                if 0 <= index < len(routes):
                    routes[index] = "화면"
        for record in records:
            index = record.get("row_index")
            if not isinstance(index, int) or not 0 <= index < len(routes):
                continue
            transport = record.get("transport")
            if transport in {"direct", "macro_retry", "excluded"}:
                routes[index] = {"direct": "직접", "macro_retry": "화면", "excluded": "매칭 제외"}[transport]
        if self.__dict__.get("_feedback_batch_active"):
            self._route_summary_dirty = True
        else:
            self.route_summary_var.set(route_summary(routes))
        self._delivery_routes = routes
        if "tree" in self.__dict__:
            rendered = self.__dict__.setdefault("_rendered_route_cells", {})
            for index, route in enumerate(routes):
                if rendered.get(index) != route and self.tree.exists(str(index)):
                    self.tree.set(str(index), "route", route)
                    rendered[index] = route

    def refresh_result_delivery_route(self, record: Mapping[str, Any]) -> None:
        """A send result changes one route; document or mapping edits refresh all."""
        if "route_summary_var" not in self.__dict__:
            return
        routes = self.__dict__.get("_delivery_routes", ())
        index = record.get("row_index")
        if (len(routes) != len(self.rows) or type(index) is not int
                or not 0 <= index < len(routes) or record.get("transport") == "excluded"):
            # Exclusion applies to every occurrence of the same chat name.
            self.refresh_delivery_routes()
            return
        route = {"direct": "직접", "macro_retry": "화면"}.get(record.get("transport"))
        if route is not None:
            routes[index] = route
            tree = self.__dict__.get("tree")
            if tree is not None and tree.exists(str(index)):
                tree.set(str(index), "route", route)
                self.__dict__.setdefault("_rendered_route_cells", {})[index] = route
        if self.__dict__.get("_feedback_batch_active"):
            self._route_summary_dirty = True
        else:
            self.route_summary_var.set(route_summary(routes))

    def common_schedule_datetime(self) -> datetime | None:
        if not self.common_schedule_var.get():
            return None
        value = self.schedule_at_var.get().strip()
        if not value:
            return None
        return parse_schedule_datetime(value)

    def choose_common_schedule(self) -> None:
        def apply_common_schedule() -> None:
            self.common_schedule_var.set(True)
            self.refresh_preview_count()

        self.open_schedule_picker(self.schedule_at_var, apply_common_schedule)

    def open_schedule_picker(
        self,
        target_var: tk.StringVar,
        on_apply: Callable[[], None] | None = None,
        parent: tk.Toplevel | None = None,
    ) -> None:
        owner = parent or self
        picker_now = datetime.now()
        initial = schedule_picker_initial_datetime(target_var.get(), picker_now)
        first_available_date = schedule_picker_initial_datetime("", picker_now).date()
        date_values = [first_available_date + timedelta(days=offset) for offset in range(SCHEDULE_PICKER_DAYS)]
        if initial.date() not in date_values:
            date_values.append(initial.date())
            date_values.sort()
        date_labels = {schedule_picker_date_label(value): value for value in date_values}

        dialog = tk.Toplevel(owner)
        dialog.title("예약 일시 선택")
        dialog.transient(owner)
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.columnconfigure(1, weight=1)

        date_var = tk.StringVar()
        period_var = tk.StringVar()
        hour_var = tk.StringVar()
        minute_var = tk.StringVar()

        ttk.Label(dialog, text="날짜").grid(row=0, column=0, sticky="w", padx=14, pady=(14, 8))
        date_combo = ttk.Combobox(dialog, textvariable=date_var, values=tuple(date_labels), state="readonly", width=18)
        date_combo.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(14, 8))
        ttk.Label(dialog, text="시간").grid(row=1, column=0, sticky="w", padx=14, pady=8)
        time_frame = ttk.Frame(dialog)
        time_frame.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=8)
        period_combo = ttk.Combobox(time_frame, textvariable=period_var, state="readonly", width=5)
        period_combo.grid(row=0, column=0)
        hour_combo = ttk.Combobox(time_frame, textvariable=hour_var, state="readonly", width=4)
        hour_combo.grid(row=0, column=1, padx=(6, 0))
        ttk.Label(time_frame, text="시").grid(row=0, column=2, padx=(3, 0))
        minute_combo = ttk.Combobox(time_frame, textvariable=minute_var, state="readonly", width=4)
        minute_combo.grid(row=0, column=3, padx=(6, 0))
        ttk.Label(time_frame, text="분").grid(row=0, column=4, padx=(3, 0))
        ttk.Label(
            dialog,
            text="현재부터 최소 5분 여유가 있는 시각만 표시합니다. 예약은 5분 단위입니다.",
            style="SoftMuted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 8))

        def set_combo_value(combo: ttk.Combobox, variable: tk.StringVar, values: tuple[str, ...], preferred: str = "") -> None:
            combo.configure(values=values)
            current = preferred if preferred in values else variable.get()
            variable.set(current if current in values else (values[0] if values else ""))

        def refresh_time_values(preferred: datetime | None = None) -> None:
            selected_date = date_labels.get(date_var.get())
            now = datetime.now()
            slots = schedule_picker_time_slots(selected_date, now) if selected_date is not None else ()
            if not slots:
                selected_date = next(
                    (value for value in date_values if schedule_picker_time_slots(value, now)),
                    None,
                )
                if selected_date is not None:
                    date_var.set(schedule_picker_date_label(selected_date))
                    slots = schedule_picker_time_slots(selected_date, now)

            preferred_period = ""
            preferred_hour = ""
            preferred_minute = ""
            if preferred is not None and selected_date == preferred.date():
                preferred_period = "오후" if preferred.hour >= 12 else "오전"
                preferred_hour = str(preferred.hour % 12 or 12)
                preferred_minute = f"{preferred.minute:02d}"

            periods = schedule_picker_period_values(slots)
            set_combo_value(period_combo, period_var, periods, preferred_period)
            hours = schedule_picker_hour_values(slots, period_var.get())
            set_combo_value(hour_combo, hour_var, hours, preferred_hour)
            minutes = schedule_picker_minute_values(slots, period_var.get(), hour_var.get())
            set_combo_value(minute_combo, minute_var, minutes, preferred_minute)

        def set_picker_values(value: datetime) -> None:
            date_var.set(schedule_picker_date_label(value.date()))
            refresh_time_values(value)

        def close_picker() -> None:
            dialog.destroy()
            if parent is not None and parent.winfo_exists():
                parent.grab_set()

        def apply_picker() -> None:
            selected_date = date_labels.get(date_var.get())
            if selected_date is None or not period_var.get() or not hour_var.get() or not minute_var.get():
                messagebox.showwarning("예약 일시", "현재부터 최소 5분 뒤의 예약 일시를 선택해 주세요.", parent=dialog)
                return
            selected_at = schedule_picker_datetime(selected_date, period_var.get() == "오후", int(hour_var.get()), int(minute_var.get()))
            if selected_at < earliest_schedule_datetime(datetime.now()):
                messagebox.showwarning("예약 일시", "예약까지 최소 5분 여유가 필요합니다. 시간을 다시 선택해 주세요.", parent=dialog)
                refresh_time_values()
                return
            target_var.set(format_schedule_datetime(selected_at))
            if on_apply is not None:
                on_apply()
            close_picker()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", padx=14, pady=(0, 14))
        ttk.Button(buttons, text="가장 빠른 시각", command=lambda: set_picker_values(earliest_schedule_datetime(datetime.now()))).pack(side="left")
        ttk.Button(buttons, text="취소", command=close_picker).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="적용", command=apply_picker, style="Accent.TButton").pack(side="right")
        date_combo.bind("<<ComboboxSelected>>", lambda _event: refresh_time_values())
        period_combo.bind("<<ComboboxSelected>>", lambda _event: refresh_time_values())
        hour_combo.bind("<<ComboboxSelected>>", lambda _event: refresh_time_values())
        set_picker_values(initial)
        dialog.wait_window()

    def effective_schedule_datetime(self, row: MessageRow, fallback: datetime | None = None) -> datetime | None:
        return schedule_datetime_for_row(row, fallback)

    def refresh_schedule_state_label(self) -> None:
        if not hasattr(self, "schedule_state_label"):
            return
        row_schedule_count = sum(1 for row in self.rows if row.scheduled_at is not None)
        if self.common_schedule_var.get():
            try:
                common_schedule = self.common_schedule_datetime()
            except ValueError:
                self.schedule_state_label.configure(text="형식 확인")
                return
            if common_schedule is None:
                self.schedule_state_label.configure(text="입력 필요")
                return
            self.schedule_state_label.configure(text=f"공통 {format_schedule_datetime(common_schedule)}")
            return
        self.schedule_state_label.configure(text=f"{row_schedule_count}건" if row_schedule_count else "없음")

    def render_schedule_capacity(self, view) -> None:
        self._capacity_render_revision = int(self.__dict__.get("_capacity_render_revision", 0)) + 1
        if 'schedule_counter_var' not in self.__dict__:
            return
        self.schedule_counter_var.set(view.label())
        parts = ['등록 기록·현재 시각 기준 · 발송 완료 확인 아님']
        if view.next_release is not None:
            parts.append(f'다음 해제 예상 {view.next_release:%m/%d %H:%M}')
        if view.expired:
            parts.append(f'예약 시각 경과 {view.expired}건 · 슬롯 제외')
        self.schedule_counter_detail_var.set(' · '.join(parts))

    def tick_schedule_capacity(self) -> None:
        if not self.__dict__.get("_close_requested"):
            self.request_schedule_capacity_refresh()
            self.after(1000, self.tick_schedule_capacity)

    def refresh_schedule_capacity(self) -> None:
        try:
            if '_schedule_capacity_store' not in self.__dict__:
                self._schedule_capacity_store = ScheduleCapacity(SCHEDULE_JOURNAL_PATH)
            self.render_schedule_capacity(self._schedule_capacity_store.view())
            self.status_var.set('예약 기록과 현재 시각으로 슬롯을 계산했습니다.')
        except Exception as error:
            self.schedule_counter_var.set('예약 기록 확인 필요 · 자동 등록 차단')
            self.schedule_counter_detail_var.set(str(error))
            self.status_var.set(f'예약 기록 확인 실패: {error}')

    def save_preview_csv(self) -> None:
        if not self.rows:
            messagebox.showinfo("저장할 내용 없음", "먼저 CSV 또는 XLSX 파일을 선택하거나 행을 추가해 주세요.")
            return
        initial_name = "kakao_messages_edited.csv"
        if self.file_path:
            initial_name = f"{self.file_path.stem}_edited.csv"
        path = filedialog.asksaveasfilename(
            title="편집본 CSV 저장",
            defaultextension=".csv",
            initialfile=initial_name,
            filetypes=(("CSV UTF-8", "*.csv"), ("모든 파일", "*.*")),
        )
        if not path:
            return
        Path(path).write_text("\ufeff" + rows_to_csv_text(self.rows), encoding="utf-8")
        self.log("저장", f"편집본 CSV 저장 완료: {path}")

    def retry_failed_rows(self) -> None:
        indexes = macro_retry_result_indexes(self.results, self.rows)
        if not indexes:
            messagebox.showinfo(
                "2차 전송 대상 없음",
                "PC 화면으로 다시 보낼 명시적인 직접 전송 실패 행이 없습니다.",
            )
            return
        self.log("2차 전송", f"직접 전송 실패 {len(indexes)}건을 PC 화면 방식으로 다시 발송합니다.")
        self.start_sending(indexes, send_mode="macro_retry")

    def refresh_retry_failed_action(self) -> None:
        if not hasattr(self, "retry_failed_button"):
            return
        failed_count = len(macro_retry_result_indexes(self.results, self.rows))
        self.retry_failed_button.configure(text=format_retry_failed_button_label(failed_count))
        worker = getattr(self, "worker", None)
        worker_running = bool(worker and worker.is_alive())
        self.retry_failed_button.state(["disabled"] if worker_running or not failed_count else ["!disabled"])

    def pick_attachments(self, images_only: bool = False) -> tuple[Path, ...]:
        filetypes = (
            ("이미지", "*.jpg *.jpeg *.png *.gif *.bmp *.webp *.tif *.tiff *.heic *.heif"),
            ("모든 파일", "*.*"),
        ) if images_only else (
            ("파일 및 이미지", "*.*"),
            ("이미지", "*.jpg *.jpeg *.png *.gif *.bmp *.webp *.tif *.tiff *.heic *.heif"),
            ("문서", "*.pdf *.doc *.docx *.xls *.xlsx *.ppt *.pptx *.txt"),
        )
        paths = filedialog.askopenfilenames(
            title="첨부 파일 선택",
            initialdir=str(default_desktop_dir()),
            filetypes=filetypes,
        )
        return tuple(Path(path) for path in paths)

    def pick_attachments_into_var(self, variable: tk.StringVar) -> None:
        paths = self.pick_attachments(images_only=False)
        if paths:
            current = parse_attachment_paths(variable.get(), base_dir=self.file_path.parent if self.file_path else None)
            variable.set(format_attachment_paths(current + paths))

    def attach_files_to_all_rows(self) -> str | None:
        if self.preview_edit_locked():
            return "break"
        if not self.rows:
            messagebox.showinfo("파일 첨부", "먼저 CSV/XLSX 파일을 선택하거나 행을 추가해 주세요.")
            return "break"
        paths = self.pick_attachments(images_only=False)
        if not paths:
            return "break"
        self.apply_common_attachment_paths(paths)
        return "break"

    def apply_common_attachment_paths(self, paths: Iterable[str | Path]) -> bool:
        if self.preview_edit_locked():
            return False
        if not self.rows:
            messagebox.showinfo("파일 첨부", "먼저 CSV/XLSX 파일을 선택하거나 행을 추가해 주세요.")
            return False
        try:
            validated = validated_attachment_paths(paths)
        except ValueError as exc:
            messagebox.showwarning("첨부 경로 확인", str(exc))
            return False
        if all(row.attachments == validated for row in self.rows):
            self.log("편집", "이미 모든 행에 같은 파일이 첨부돼 있습니다.")
            return True
        self.push_undo_snapshot()
        self.rows = rows_with_common_attachments(self.rows, validated)
        self.finish_preview_mutation(f"{len(self.rows)}개 행의 공통 첨부를 파일 {len(validated)}개로 교체했습니다. 발송 대기 상태입니다.", 0)
        return True

    def apply_attachment_path_input(self, event: tk.Event | None = None) -> str:
        if self.preview_edit_locked():
            return "break"
        try:
            paths = attachment_paths_from_text(
                self.attachment_path_var.get(), base_dir=self.file_path.parent if self.file_path else None,
            )
        except ValueError as exc:
            messagebox.showwarning("첨부 경로 확인", str(exc))
            return "break"
        self.apply_common_attachment_paths(paths)
        return "break"

    def on_attachment_drop(self, event: Any) -> str:
        # A drop changes the preview only. It cannot call any send operation.
        if self.preview_edit_locked():
            return "refuse_drop"
        try:
            paths = attachment_paths_from_drop(getattr(event, "data", ""), self.tk.splitlist)
        except ValueError as exc:
            messagebox.showwarning("파일 드롭 확인", str(exc))
            return "refuse_drop"
        return "copy" if self.apply_common_attachment_paths(paths) else "refuse_drop"

    def set_attachment_edit_state(self, locked: bool) -> None:
        for name in ("attachment_path_entry", "attachment_path_button",
                     "attachment_pick_button", "attachment_clear_button"):
            widget = self.__dict__.get(name)
            if widget is not None:
                widget.state(["disabled"] if locked else ["!disabled"])

    def clear_all_attachments(self) -> str | None:
        if self.preview_edit_locked():
            return "break"
        if not self.rows:
            return "break"
        if all(not row.attachments for row in self.rows):
            self.log("편집", "초기화할 파일 첨부가 없습니다.")
            return "break"
        self.push_undo_snapshot()
        self.rows = rows_with_common_attachments(self.rows, ())
        self.finish_preview_mutation("모든 대화명의 파일 첨부를 초기화했습니다.", 0)
        return "break"

    def add_attachments_to_selected(self) -> str | None:
        return self.attach_files_to_all_rows()

    def add_images_to_selected(self) -> str | None:
        return self.attach_files_to_all_rows()

    def clear_selected_attachments(self) -> str | None:
        return self.clear_all_attachments()

    def on_preview_selection_changed(self, event: tk.Event | None = None) -> None:
        # Attachments are common to all rows. Selecting a recipient must not
        # replace an attachment path draft that has not been applied yet.
        if "_preview_render_rows" not in self.__dict__:
            self.refresh_attachment_panel(sync_path_input=False)

    def refresh_attachment_panel(self, *, sync_path_input: bool = True) -> None:
        if not hasattr(self, "attachment_list"):
            return
        self.attachment_list.delete(0, "end")
        shared = shared_attachment_paths(self.rows)
        empty_label = self.__dict__.get("attachment_empty_label")
        if empty_label is not None:
            if shared:
                empty_label.grid_remove()
            else:
                empty_label.configure(text="행마다 첨부가 다릅니다\n새 파일을 놓으면 전체에 공통 적용" if shared is None else "파일을 여기에 놓으세요\n이미지·PDF 공통 첨부")
                empty_label.grid()
        path_var = self.__dict__.get("attachment_path_var")
        if path_var is not None and sync_path_input:
            path_var.set(format_attachment_path_input(shared or ()))
        if shared is None:
            self.attachment_help.configure(text=format_attachment_panel_help(shared))
            return
        if not shared:
            if empty_label is None:
                # Older lightweight views can keep their text placeholder;
                # the real GUI uses the non-selectable drop-state overlay.
                self.attachment_list.insert("end", format_attachment_empty_label())
            self.attachment_help.configure(text=format_attachment_panel_help(shared))
            return
        for path in shared:
            prefix = "이미지" if is_image_path(path) else "파일"
            self.attachment_list.insert("end", f"{prefix} · {path.name}")
        help_text = format_attachment_panel_help(shared)
        if shared == self.__dict__.get("_attachment_dimensions_paths"):
            for path, item in zip(shared, self.__dict__.get("_attachment_dimensions", ())):
                width, height = item.get("width"), item.get("height")
                if type(width) is int and type(height) is int and width > 0 and height > 0:
                    detail = f"{path.name}: {width}x{height}"
                    if width < SMALL_IMAGE_WARNING_THRESHOLD_PX or height < SMALL_IMAGE_WARNING_THRESHOLD_PX:
                        detail += " - 수신 화면에서 흐리게 보일 수 있음"
                    help_text += "\n" + detail
        self.attachment_help.configure(text=help_text)
        if (any(is_image_path(path) for path in shared)
                and shared != self.__dict__.get("_attachment_dimensions_paths")
                and not self.__dict__.get("_attachment_dimensions_pending")
                and "direct_config" in self.__dict__):
            self._attachment_dimensions_pending = True
            config = self.direct_config.to_dict()
            post = self.post

            def worker() -> None:
                bridge = None
                dimensions = []
                try:
                    # Local preflight never logs in, reads the vault or sends.
                    bridge = KakaoDirectBridge(config, direct_enabled=True)
                    result = bridge.prepare_attachments(attachments=shared, attachment_run_id=uuid.uuid4().hex)
                    dimensions = result.get("attachments", [])
                except DirectBridgeError:
                    pass  # Missing SDK/invalid images must not block editing or send.
                finally:
                    try:
                        if bridge is not None:
                            bridge.close()
                    finally:
                        post("attachment_dimensions", (shared, dimensions))

            threading.Thread(target=worker, daemon=True).start()

    def handle_attachment_dimensions(self, payload: Any) -> None:
        paths, dimensions = payload
        self._attachment_dimensions_pending = False
        if self.__dict__.get("_close_requested"):
            return
        self._attachment_dimensions_paths = paths
        self._attachment_dimensions = dimensions
        # A stale result is never shown; refresh can request the new paths.
        self.refresh_attachment_panel(sync_path_input=False)

    def open_search_bar(self, event: tk.Event | None = None) -> str:
        self.search_frame.grid()
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, "end")
        return "break"

    def close_search_bar(self, event: tk.Event | None = None) -> str:
        self.search_var.set("")
        self.search_match_indexes = []
        self.search_match_position = -1
        self._search_query = None
        self.search_frame.grid_remove()
        self.tree.focus_set()
        return "break"

    def preview_search_text(self, row: MessageRow) -> str:
        attachment_text = " ".join(str(path) for path in row.attachments)
        return f"{row.row_number} {row.chat_name} {row.text} {attachment_text} {format_schedule_datetime(row.scheduled_at)}".lower()

    def refresh_search_matches(self) -> list[int]:
        query = self.search_var.get().strip().lower()
        if not query:
            self.search_match_indexes = []
            self.search_match_position = -1
            self._search_query = None
            return []
        snapshot = tuple(self.rows)
        previous = self.__dict__.get("_search_rows_snapshot", ())
        old_texts = self.__dict__.get("_search_texts", ())
        if snapshot != previous:
            self._search_texts = tuple(
                old_texts[index] if index < len(previous) and row is previous[index]
                else self.preview_search_text(row)
                for index, row in enumerate(snapshot)
            )
            self._search_rows_snapshot = snapshot
            self._search_query = None
        if self.__dict__.get("_search_query") != query:
            self.search_match_indexes = [
                index for index, value in enumerate(self.__dict__.get("_search_texts", ())) if query in value
            ]
            self._search_query = query
        if self.search_match_position >= len(self.search_match_indexes):
            self.search_match_position = -1
        return self.search_match_indexes

    def find_preview_match(self, direction: int = 1) -> str:
        if not self.search_frame.winfo_ismapped():
            self.open_search_bar()
        matches = self.refresh_search_matches()
        query = self.search_var.get().strip()
        if not query:
            self.status_var.set(format_search_empty_status())
            return "break"
        if not matches:
            self.status_var.set(format_search_no_result_status(query))
            self.log("검색", f"결과 없음: {query}")
            return "break"
        selected = self.selected_row_indexes()
        if selected and selected[0] in matches:
            current = matches.index(selected[0])
            self.search_match_position = (current + direction) % len(matches)
        else:
            self.search_match_position = 0 if direction >= 0 else len(matches) - 1
        row_index = matches[self.search_match_position]
        item_id = str(row_index)
        if self.tree.exists(item_id):
            self.tree.selection_set(item_id)
            self.tree.focus(item_id)
            self.tree.see(item_id)
        self.status_var.set(format_search_match_status(self.search_match_position + 1, len(matches), query))
        return "break"

    def next_preview_row_number(self) -> int:
        known = [row.row_number for row in self.rows]
        known.extend(self.__dict__.get("_recovery_sources_by_number", {}))
        known.extend(self.__dict__.get("_last_run_sources_by_number", {}))
        return max(known, default=0) + 1

    def tree_index_from_item(self, item_id: str) -> int:
        return int(self.tree.index(item_id))

    def selected_row_indexes(self) -> list[int]:
        return sorted({self.tree_index_from_item(item_id) for item_id in self.tree.selection()})

    def preview_text(self, text: str) -> str:
        preview = text.replace("\n", " / ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        return preview

    def finish_preview_mutation(self, log_message: str, select_index: int | None = None) -> None:
        self.cancel_cell_edit()
        self.reset_preview_results()
        self.populate_table()
        self.refresh_preview_count()
        if select_index is not None and self.rows:
            select_index = max(0, min(select_index, len(self.rows) - 1))
            item_id = str(select_index)
            if self.tree.exists(item_id):
                self.tree.selection_set(item_id)
                self.tree.focus(item_id)
                self.tree.see(item_id)
        self.log("편집", log_message)

    def populate_table(self) -> None:
        """Update changed rows in place; preserve numeric row IDs and ready state."""
        existing = set(self.tree.get_children())
        wanted = {str(index) for index in range(len(self.rows))}
        removed = existing - wanted
        if removed:
            self.tree.delete(*removed)
        previous = self.__dict__.get("_preview_render_rows", ())
        dirty = self.__dict__.pop("_preview_status_dirty", set())
        rendered_routes = self.__dict__.setdefault("_rendered_route_cells", {})
        for index in list(rendered_routes):
            if index >= len(self.rows):
                rendered_routes.pop(index)
        for index, row in enumerate(self.rows):
            item_id = str(index)
            if item_id in existing and index < len(previous) and row is previous[index] and item_id not in dirty:
                continue
            schedule_text = format_schedule_datetime(row.scheduled_at) or "-"
            values = (row.row_number, row.chat_name, self.preview_text(row.text), attachment_summary(row), schedule_text, "대기", "")
            if item_id in existing:
                self.tree.item(item_id, values=values, tags=("ready",))
            else:
                self.tree.insert("", "end", iid=item_id, values=values, tags=("ready",))
            rendered_routes.pop(index, None)
        self._preview_render_rows = tuple(self.rows)
        if self.rows and not self.tree.selection():
            self.tree.selection_set("0")
            self.tree.focus("0")
        self.refresh_attachment_panel()
        self.refresh_delivery_routes()

    def add_preview_row(self) -> str | None:
        if self.preview_edit_locked():
            return "break"
        self.push_undo_snapshot()
        attachments = shared_attachment_paths(self.rows) or ()
        self.rows.append(MessageRow(self.next_preview_row_number(), "", "", attachments))
        self.finish_preview_mutation("빈 행을 추가했습니다.", len(self.rows) - 1)
        self.begin_cell_edit(item_id=str(len(self.rows) - 1), column_id="#2")
        return "break"

    def duplicate_selected_rows(self) -> str | None:
        if self.preview_edit_locked():
            return "break"
        indexes = self.selected_row_indexes()
        if not indexes:
            messagebox.showinfo("행 복제", "복제할 행을 선택해 주세요.")
            return "break"
        self.push_undo_snapshot()
        next_row = self.next_preview_row_number()
        for source in [self.rows[index] for index in indexes]:
            self.rows.append(MessageRow(next_row, source.chat_name, source.text, source.attachments, source.image_bundle, source.scheduled_at))
            next_row += 1
        self.finish_preview_mutation(f"{len(indexes)}개 행을 복제했습니다.", len(self.rows) - len(indexes))
        return "break"

    def delete_selected_rows(self, event: tk.Event | None = None) -> str | None:
        if self.preview_edit_locked():
            return "break"
        indexes = self.selected_row_indexes()
        if not indexes:
            return "break"
        self.push_undo_snapshot()
        for index in sorted(indexes, reverse=True):
            del self.rows[index]
        next_selection = indexes[0] if indexes[0] < len(self.rows) else len(self.rows) - 1
        self.finish_preview_mutation(f"{len(indexes)}개 행을 삭제했습니다.", next_selection if next_selection >= 0 else None)
        return "break"

    def undo_preview_change(self, event: tk.Event | None = None) -> str | None:
        if self.preview_edit_locked():
            return "break"
        if not self.undo_stack:
            self.log("편집", "되돌릴 변경이 없습니다.")
            return "break"
        self.rows = self.undo_stack.pop()
        self.finish_preview_mutation("마지막 미리보기 변경을 되돌렸습니다.", 0)
        return "break"

    def select_all_rows(self, event: tk.Event | None = None) -> str:
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def copy_selected_rows(self, event: tk.Event | None = None) -> str:
        indexes = self.selected_row_indexes()
        if not indexes:
            return "break"
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
        for index in indexes:
            row = self.rows[index]
            writer.writerow([row.chat_name, row.text, format_attachment_paths(row.attachments), format_schedule_datetime(row.scheduled_at)])
        self.clipboard_clear()
        self.clipboard_append(buffer.getvalue().rstrip("\n"))
        self.log("편집", f"{len(indexes)}개 행을 클립보드에 복사했습니다.")
        return "break"

    def paste_rows_from_clipboard(self, event: tk.Event | None = None) -> str:
        if self.preview_edit_locked():
            return "break"
        try:
            raw_text = self.clipboard_get()
        except tk.TclError:
            return "break"
        indexes = self.selected_row_indexes()
        start_index = indexes[0] if indexes else len(self.rows)
        try:
            candidate, count = plan_clipboard_paste(
                self.rows, raw_text, start_index,
                base_dir=self.file_path.parent if self.file_path else None,
                next_row_number=self.next_preview_row_number(),
            )
        except (ValueError, csv.Error) as exc:
            messagebox.showwarning("붙여넣기", str(exc))
            return "break"
        if count:
            self.push_undo_snapshot()
            self.rows = candidate
            self.finish_preview_mutation(f"{count}개 행을 붙여넣었습니다.", start_index)
        return "break"

    def begin_cell_edit(self, event: tk.Event | None = None, item_id: str | None = None, column_id: str | None = None) -> str:
        if self.preview_edit_locked():
            return "break"
        self.cancel_cell_edit()
        if event is not None:
            identified_row = self.tree.identify_row(getattr(event, "y", 0))
            if identified_row:
                item_id = identified_row
                column_id = self.tree.identify_column(getattr(event, "x", 0))
        if not item_id:
            selection = self.tree.selection()
            item_id = selection[0] if selection else ""
        if not column_id:
            column_id = "#2"
        if not item_id or column_id not in {"#2", "#3", "#5"}:
            return "break"
        bbox = self.tree.bbox(item_id, column_id)
        if not bbox:
            return "break"
        row_index = self.tree_index_from_item(item_id)
        row = self.rows[row_index]
        if column_id == "#5":
            schedule_var = tk.StringVar(value=format_schedule_datetime(row.scheduled_at))

            def save_schedule() -> None:
                if self.preview_edit_locked():
                    return
                scheduled_at = parse_schedule_datetime(schedule_var.get())
                if scheduled_at == row.scheduled_at:
                    return
                self.push_undo_snapshot()
                self.rows[row_index] = MessageRow(row.row_number, row.chat_name, row.text, row.attachments, row.image_bundle, scheduled_at)
                self.finish_preview_mutation("예약일시를 수정했습니다.", row_index)

            self.open_schedule_picker(schedule_var, save_schedule)
            return "break"
        if column_id == "#2":
            value = row.chat_name
        elif column_id == "#3":
            value = row.text
        else:
            value = format_schedule_datetime(row.scheduled_at)
        editor = ttk.Entry(self.tree)
        editor.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        editor.insert(0, value)
        editor.select_range(0, "end")
        editor.focus_set()
        self.cell_editor = editor
        self.cell_editor_info = {"row_index": row_index, "column_id": column_id}
        editor.bind("<Return>", self.commit_cell_edit)
        editor.bind("<Escape>", self.cancel_cell_edit)
        editor.bind("<FocusOut>", self.commit_cell_edit)
        return "break"

    def commit_cell_edit(self, event: tk.Event | None = None) -> str:
        if not self.cell_editor or not self.cell_editor_info:
            return "break"
        if self.preview_edit_locked():
            self.cancel_cell_edit()
            return "break"
        editor = self.cell_editor
        info = self.cell_editor_info
        value = editor.get()
        row_index = int(info["row_index"])
        column_id = str(info["column_id"])
        if row_index >= len(self.rows):
            self.cancel_cell_edit()
            return "break"
        row = self.rows[row_index]
        new_chat = value.strip() if column_id == "#2" else row.chat_name
        new_text = value if column_id == "#3" else row.text
        new_attachments = row.attachments
        new_schedule = row.scheduled_at
        if column_id == "#5":
            try:
                new_schedule = parse_schedule_datetime(value)
            except ValueError as exc:
                messagebox.showwarning("셀 편집", str(exc))
                editor.focus_set()
                return "break"
        if column_id == "#2" and not new_chat:
            messagebox.showwarning("셀 편집", "대화명을 비워둘 수 없습니다.")
            editor.focus_set()
            return "break"
        if column_id == "#3" and not new_text.strip() and not row.attachments:
            messagebox.showwarning("셀 편집", "메시지가 없으면 첨부가 필요합니다.")
            editor.focus_set()
            return "break"
        if new_chat != row.chat_name or new_text != row.text or new_attachments != row.attachments or new_schedule != row.scheduled_at:
            self.push_undo_snapshot()
            self.rows[row_index] = MessageRow(row.row_number, new_chat, new_text, new_attachments, row.image_bundle, new_schedule)
            self.finish_preview_mutation("셀 값을 수정했습니다.", row_index)
        else:
            self.cancel_cell_edit()
        return "break"

    def cancel_cell_edit(self, event: tk.Event | None = None) -> str:
        if self.cell_editor:
            self.cell_editor.destroy()
        self.cell_editor = None
        self.cell_editor_info = None
        return "break"

    def edit_selected_row(self, event: tk.Event | None = None) -> None:
        if self.preview_edit_locked():
            return
        if event is not None:
            item_id = self.tree.identify_row(event.y)
            if item_id:
                self.tree.selection_set(item_id)

        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("행 수정", "수정할 미리보기 행을 선택해 주세요.")
            return

        row_index = self.tree_index_from_item(selection[0])
        row = self.rows[row_index]
        dialog = tk.Toplevel(self)
        dialog.title("미리보기 행 수정")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("620x420")
        dialog.columnconfigure(1, weight=1)
        dialog.rowconfigure(3, weight=1)

        chat_var = tk.StringVar(value=row.chat_name)
        schedule_var = tk.StringVar(value=format_schedule_datetime(row.scheduled_at))
        ttk.Label(dialog, text=f"원본 행: {row.row_number}").grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 8))
        ttk.Label(dialog, text="대화명").grid(row=1, column=0, sticky="w", padx=14, pady=8)
        chat_entry = ttk.Entry(dialog, textvariable=chat_var)
        chat_entry.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=8)
        ttk.Label(dialog, text="예약일시").grid(row=2, column=0, sticky="w", padx=14, pady=8)
        schedule_frame = ttk.Frame(dialog)
        schedule_frame.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=8)
        schedule_frame.columnconfigure(0, weight=1)
        ttk.Entry(schedule_frame, textvariable=schedule_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(schedule_frame, text="일시 선택", command=lambda: self.open_schedule_picker(schedule_var, parent=dialog)).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(dialog, text="메시지").grid(row=3, column=0, sticky="nw", padx=14, pady=8)
        message_box = ScrolledText(dialog, height=8, wrap="word", font=("Malgun Gothic", 10))
        message_box.grid(row=3, column=1, sticky="nsew", padx=(0, 14), pady=8)
        message_box.insert("1.0", row.text)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", padx=14, pady=(8, 14))

        def save_edit() -> None:
            if self.preview_edit_locked():
                dialog.destroy()
                return
            chat_name = chat_var.get().strip()
            text = message_box.get("1.0", "end-1c")
            try:
                scheduled_at = parse_schedule_datetime(schedule_var.get())
            except ValueError as exc:
                messagebox.showwarning("행 수정", str(exc), parent=dialog)
                return
            if not chat_name:
                messagebox.showwarning("행 수정", "대화명을 입력해 주세요.", parent=dialog)
                return
            if not text.strip() and not row.attachments:
                messagebox.showwarning("행 수정", "메시지 또는 첨부를 입력해 주세요.", parent=dialog)
                return
            if chat_name != row.chat_name or text != row.text or scheduled_at != row.scheduled_at:
                self.push_undo_snapshot()
                self.rows[row_index] = MessageRow(row.row_number, chat_name, text, row.attachments, row.image_bundle, scheduled_at)
                self.finish_preview_mutation(f"미리보기 행 {row.row_number} 수정: {chat_name}", row_index)
            dialog.destroy()

        ttk.Button(buttons, text="취소", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="저장", command=save_edit, style="Accent.TButton").pack(side="right")
        wire_big_edit_dialog_keyboard(dialog, chat_entry, save_edit)
        dialog.wait_window()

    def update_row_status(self, row_index: int, status: str) -> None:
        item_id = str(row_index)
        if self.tree.exists(item_id):
            values = list(self.tree.item(item_id, "values"))
            values[5] = status
            self.tree.item(item_id, values=values, tags=(row_status_tag(status),))
            self.__dict__.setdefault("_preview_status_dirty", set()).add(item_id)
            if self.__dict__.get("_feedback_batch_active"):
                self._pending_tree_see = item_id
            else:
                self.tree.see(item_id)

    def reset_run_preview_statuses(self, selected: Iterable[tuple[int, MessageRow]]) -> None:
        """Clear previous run colors before the new run can publish results."""
        tree = self.__dict__.get("tree")
        if tree is None:
            return
        selected_ids = {str(index) for index, _ in selected}
        for item_id in tree.get_children():
            values = list(tree.item(item_id, "values"))
            if len(values) > 5:
                values[5] = "대기" if item_id in selected_ids else "미대상"
                tree.item(item_id, values=values, tags=("ready",))
                self.__dict__.setdefault("_preview_status_dirty", set()).add(item_id)

    def filtered_rows(self, row_indexes: list[int] | None = None) -> list[tuple[int, MessageRow]]:
        if row_indexes is not None:
            return [(index, self.rows[index]) for index in row_indexes if 0 <= index < len(self.rows)]
        rows = list(enumerate(self.rows))
        start_at = max(1, int(self.start_at_var.get() or 1))
        rows = rows[start_at - 1 :]
        limit = int(self.limit_var.get() or 0)
        if self.test_one_var.get():
            limit = 1
        if limit > 0:
            rows = rows[:limit]
        return rows

    def apply_test_one_limit(self) -> None:
        if self.test_one_var.get():
            self.previous_limit_before_test_one = int(self.limit_var.get() or 0)
            self.limit_var.set(1)
        elif int(self.limit_var.get() or 0) == 1:
            self.limit_var.set(self.previous_limit_before_test_one if self.previous_limit_before_test_one > 1 else 0)

    def log_check_progress(self, percent: int) -> None:
        self.progress_var.set(percent)
        self.status_var.set(f"점검 중 {percent}%")
        if hasattr(self, "progress_state_label"):
            self.progress_state_label.configure(text=f"{percent}%")
        self.log("점검", f"점검 중, 진행률 {percent}%")

    def preflight_problem(self, message: str) -> bool:
        warning = format_preflight_warning(message)
        self.status_var.set("점검 필요")
        self.log("점검", f"문제: {message} / 다음 행동: {preflight_next_action(message)}")
        messagebox.showwarning("점검 필요", warning)
        return False

    def preflight(
        self,
        selected: list[tuple[int, MessageRow]] | None = None,
        *,
        blocked_direct_names: Iterable[Any] = (),
        direct_enabled_override: bool | None = None,
        send_mode: str = "normal",
    ) -> bool:
        self.log_check_progress(10)
        if not self.rows:
            return self.preflight_problem("발송할 CSV/XLSX 파일을 먼저 선택해 주세요.")
        if selected is None:
            selected = self.filtered_rows(self.run_row_indexes)
        if not selected:
            return self.preflight_problem("발송할 행이 없습니다. 파일 내용이나 오류 재시도 대상을 확인해 주세요.")
        self.log_check_progress(30)
        invalid_rows = [
            row
            for _, row in selected
            if not row.chat_name.strip() or (not row.text.strip() and not row.attachments)
        ]
        if invalid_rows:
            preview = "\n".join(f"행 {row.row_number}: {row.chat_name or '(대화명 없음)'}" for row in invalid_rows[:5])
            return self.preflight_problem(f"대화명 또는 발송 내용이 비어 있는 행이 있습니다.\n\n{preview}")
        if send_mode not in {"normal", "macro_retry"}:
            return self.preflight_problem("지원하지 않는 발송 실행 방식입니다.")
        if send_mode == "macro_retry":
            fallback_schedule = None
        else:
            try:
                fallback_schedule = self.common_schedule_datetime()
            except ValueError as exc:
                return self.preflight_problem(f"공통 예약일시 형식을 확인해 주세요.\n\n{exc}")
        schedule_errors: list[str] = []
        scheduled_count = 0
        effective_schedules: dict[int, datetime | None] = {}
        for _, row in selected:
            scheduled_at = (
                None
                if send_mode == "macro_retry"
                else self.effective_schedule_datetime(row, fallback_schedule)
            )
            effective_schedules[id(row)] = scheduled_at
            if send_mode == "macro_retry":
                continue
            if scheduled_at is None and not self.common_schedule_var.get() and row.scheduled_at is None:
                continue
            try:
                validate_schedule_row(row, scheduled_at)
                scheduled_count += 1
            except ValueError as exc:
                schedule_errors.append(str(exc))
        if schedule_errors:
            preview = "\n".join(schedule_errors[:5])
            return self.preflight_problem(f"예약 메시지 점검이 필요합니다.\n\n{preview}")

        direct_enabled = (
            bool(self.direct_enabled_var.get())
            if direct_enabled_override is None
            else bool(direct_enabled_override)
        )
        # A second-pass retry is always an immediate PC-screen send.  A common
        # schedule selected after the first run must never turn it into a new
        # reservation.
        routing_schedule = fallback_schedule
        requires_macro = any(effective_schedules[id(row)] is None and not row_uses_direct_send(row, None, direct_enabled)
                             for _, row in selected)
        if requires_macro:
            try:
                x = int(float(self.coord_x_var.get()))
                y = int(float(self.coord_y_var.get()))
            except ValueError:
                return self.preflight_problem("예약·화면·2차 전송에 필요한 검색창 좌표를 숫자로 입력하거나 [화면 좌표 설정]을 실행해 주세요.")
            if x <= 0 or y <= 0:
                return self.preflight_problem("예약·화면·2차 전송에 필요한 카카오톡 검색창 좌표를 [화면 좌표 설정]에서 먼저 저장해 주세요.")

        blocked_normalized = {
            normalize_chat_match_name(name)
            for name in blocked_direct_names
            if normalize_chat_match_name(name)
        }
        direct_names = tuple(
            dict.fromkeys(
                row.chat_name.strip()
                for _, row in selected
                if row_uses_direct_send(row, effective_schedules[id(row)], direct_enabled)
                and normalize_chat_match_name(row.chat_name) not in blocked_normalized
            )
        )
        resolved_mappings = {name: resolve_chat_mapping(self.direct_mappings, name) for name in direct_names}
        missing_mappings = [name for name in direct_names if not resolved_mappings[name]]
        if missing_mappings:
            preview = "\n".join(f"- {name}" for name in missing_mappings[:8])
            return self.preflight_problem(f"직접 전송할 대화명의 chat ID 매칭이 없습니다.\n\n{preview}")
        unverified_mappings = [
            name
            for name in direct_names
            if not is_confirmed_mapping_label(
                resolved_mappings[name],
                self.direct_mapping_labels.get(resolved_mappings[name]),
            )
        ]
        if unverified_mappings:
            preview = "\n".join(f"- {name}" for name in unverified_mappings[:8])
            return self.preflight_problem(
                "직접 전송 대상의 확인된 채팅방 이름이 없습니다. "
                "[자동 매칭]을 다시 실행하고, 계속 미일치하면 [수동 매칭]에서 검증된 1:1 방을 직접 지정해 주세요.\n\n"
                f"{preview}"
            )
        unsafe_direct_mappings = unverified_alias_mapping_names(
            direct_names,
            self.direct_mappings,
            self.direct_mapping_labels,
            getattr(self, "_latest_chat_snapshot", ()),
            guarded_mapping_ids=self.__dict__.get("guarded_mapping_ids", ()),
        )
        if unsafe_direct_mappings:
            preview = "\n".join(f"- {name}" for name in unsafe_direct_mappings[:8])
            return self.preflight_problem(
                "수동/별칭 매칭이 최신 목록에서 같은 2인 채팅과 상대 identity로 확인되지 않았습니다. "
                "[수동 매칭]에서 다시 확인해 주세요.\n\n"
                f"{preview}"
            )

        self.log_check_progress(55)
        missing = missing_attachment_paths(row for _, row in selected)
        if missing:
            preview = "\n".join(f"행 {row_number}: {path}" for row_number, path in missing[:5])
            return self.preflight_problem(f"첨부 파일을 찾을 수 없습니다.\n\n{preview}")
        validated_attachment_groups: set[tuple[Path, ...]] = set()
        for _, row in selected:
            if (row.attachments and row_uses_direct_send(row, effective_schedules[id(row)], direct_enabled)
                    and normalize_chat_match_name(row.chat_name) not in blocked_normalized):
                if row.attachments in validated_attachment_groups:
                    continue
                try:
                    validate_direct_attachments(row.attachments)
                    validated_attachment_groups.add(row.attachments)
                except DirectSendError as exc:
                    return self.preflight_problem(f"행 {row.row_number} 직접 첨부 점검: {exc}")
        self.log_check_progress(80)
        validate_send_mode(len(selected), False, False)
        self.log_check_progress(100)
        direct_count, direct_targets = direct_send_target_preview(
            selected,
            routing_schedule,
            direct_enabled,
            self.direct_mappings,
            self.direct_mapping_labels,
            excluded_names=blocked_direct_names,
        )
        details: list[str] = []
        if direct_count:
            details.append(f"직접 {direct_count}건 · 대상 {len(direct_targets)}곳")
            self.log("직접 대상", " / ".join(direct_targets))
        if scheduled_count:
            details.append(f"카카오 예약 {scheduled_count}건")
        blocked_count = sum(
            1
            for _, row in selected
            if direct_enabled
            and row_uses_direct_send(row, effective_schedules[id(row)], direct_enabled)
            and normalize_chat_match_name(row.chat_name) in blocked_normalized
        )
        if blocked_count:
            details.append(f"매칭 실패 제외 {blocked_count}건")
        suffix = f" · {' · '.join(details)}" if details else ""
        self.status_var.set(f"점검 완료: {len(selected)}건{suffix}")
        return True

    def capture_coordinate(self) -> None:
        self.update_idletasks()
        capture_geometry = self.geometry()
        capture_state = self.state()
        try:
            pyautogui = require_pyautogui()
        except Exception as exc:
            self.restore_window_snapshot(capture_geometry, capture_state)
            messagebox.showerror("좌표 찍기 실패", str(exc))
            return
        self.status_var.set("3초 후 현재 마우스 좌표 저장")
        self.log("캘리브레이션", "3초 안에 마우스를 카카오톡 검색창 위로 옮겨 주세요.")

        def tick(remaining: int) -> None:
            if remaining > 0:
                self.status_var.set(f"좌표 찍기까지 {remaining}초")
                self.after(1000, lambda: tick(remaining - 1))
                return
            position = pyautogui.position()
            self.coord_x_var.set(str(int(position.x)))
            self.coord_y_var.set(str(int(position.y)))
            self.save_coordinates_from_fields()
            self.restore_window_snapshot(capture_geometry, capture_state)
            self.status_var.set("좌표 저장 완료")

        tick(3)

    def test_coordinate_click(self) -> None:
        try:
            x = int(float(self.coord_x_var.get()))
            y = int(float(self.coord_y_var.get()))
            if x <= 0 or y <= 0:
                raise ValueError("좌표가 0입니다.")
            pyautogui = require_pyautogui()
        except Exception as exc:
            messagebox.showerror("좌표 테스트 실패", str(exc))
            return
        if not messagebox.askyesno("좌표 테스트", "3초 후 저장된 좌표를 클릭합니다. 카카오톡 검색창이 보이게 해 주세요."):
            return
        self.log("테스트", f"3초 후 좌표 클릭: X={x}, Y={y}")
        self.after(3000, lambda: pyautogui.click(x, y))

    def set_auto_match_send_wait(self, waiting: bool) -> None:
        """Lock only send/mapping controls while a preflight list read runs."""

        self._auto_match_waiting_to_send = waiting
        if waiting:
            if self.__dict__.get("_active_send_preparation_token") is None:
                self._active_send_preparation_token = object()
        else:
            self._active_send_preparation_token = None
        self.set_attachment_edit_state(waiting)
        stop_button = self.__dict__.get("stop_button")
        if stop_button is not None:
            stop_button.state(["!disabled"] if waiting else ["disabled"])
        if not hasattr(self, "start_button"):
            return
        for widget_name in ("start_button", "direct_toggle", "mapping_button", "manual_mapping_button"):
            widget = getattr(self, widget_name, None)
            if widget is None:
                continue
            try:
                widget.state(["disabled"] if waiting else ["!disabled"])
            except tk.TclError:
                pass
        KakaoSenderApp.refresh_primary_action_visual(self)

    def continue_start_after_auto_match(
        self,
        selected: list[tuple[int, MessageRow]],
        verified: bool,
        *,
        preparation_token: object | None = None,
    ) -> None:
        """Resume a send only after the current chat list was read safely."""

        if preparation_token is not None and preparation_token is not self.__dict__.get("_active_send_preparation_token"):
            return
        if not verified:
            self.set_auto_match_send_wait(False)
            self.run_row_indexes = None
            self.status_var.set("채팅방 확인 실패 · 직접 전송을 시작하지 않았습니다.")
            return
        direct_names = self.direct_names_for_selected(selected)
        snapshot = tuple(
            dict(chat)
            for chat in self.__dict__.get("_latest_chat_snapshot", ())
            if isinstance(chat, Mapping)
        )
        unresolved = self.manual_mapping_unresolved_names(
            snapshot,
            target_names=direct_names,
        )
        current_ids = {str(chat.get("chat_id") or "").strip() for chat in snapshot}
        pending_saved = [name for name in direct_names
                         if (chat_id := resolve_chat_mapping(self.direct_mappings, name))
                         and chat_id not in current_ids
                         and is_confirmed_mapping_label(chat_id, self.direct_mapping_labels.get(chat_id))]
        if pending_saved:
            self.set_auto_match_send_wait(False)
            self.run_row_indexes = None
            self.status_var.set(
                f"저장 ID 유지 · {len(pending_saved)}명 서버 조회 미완료 · 발송 버튼으로 다시 확인하세요."
            )
            self.log("서버 조회 미완료", "재매칭하지 않고 저장 ID를 유지합니다: " + ", ".join(pending_saved))
            return
        if unresolved:
            self.status_var.set(f"자동 매칭 후 미해결 {len(unresolved)}명 · 수동 매칭에서 확인하세요.")
            self.log("수동 매칭", "발송 전 미해결 대상: " + ", ".join(unresolved[:12]))
            self.show_manual_chat_mapping_dialog(
                snapshot,
                target_names=unresolved,
                on_close=lambda selected=selected, token=preparation_token: self.continue_start_after_manual_mapping(
                    selected, preparation_token=token,
                ),
            )
            return
        self.set_auto_match_send_wait(False)
        self.start_sending_after_preflight(selected)

    def continue_start_after_manual_mapping(
        self,
        selected: list[tuple[int, MessageRow]],
        *,
        preparation_token: object | None = None,
    ) -> None:
        """Exclude still-unresolved direct targets, then continue the run."""

        if preparation_token is not None and preparation_token is not self.__dict__.get("_active_send_preparation_token"):
            return
        direct_names = self.direct_names_for_selected(selected)
        snapshot = tuple(
            dict(chat)
            for chat in self.__dict__.get("_latest_chat_snapshot", ())
            if isinstance(chat, Mapping)
        )
        unresolved = self.manual_mapping_unresolved_names(
            snapshot,
            target_names=direct_names,
        )
        self.set_auto_match_send_wait(False)
        if unresolved:
            self.status_var.set(f"매칭 실패 {len(unresolved)}명 제외 후 직접 전송을 계속합니다.")
            self.log("매칭 실패", "발송 제외 대상: " + ", ".join(unresolved[:12]))
        self.start_sending_after_preflight(
            selected,
            blocked_direct_names=unresolved,
        )

    def start_sending_after_preflight(
        self,
        selected: list[tuple[int, MessageRow]],
        *,
        blocked_direct_names: Iterable[Any] = (),
        send_mode: str = "normal",
    ) -> None:
        """Run the existing validation/confirmation/worker launch path."""

        if self.active_run is not None:
            messagebox.showinfo("실행 중", "이미 발송 작업이 실행 중입니다.")
            return
        if self.__dict__.get("_journal_error"):
            messagebox.showerror("실행 기록 확인 필요", self._journal_error)
            return
        direct_enabled = bool(self.direct_enabled_var.get()) if send_mode == "normal" else False
        blocked_direct_names = tuple(blocked_direct_names)
        if not self.preflight(
            selected,
            blocked_direct_names=blocked_direct_names,
            direct_enabled_override=direct_enabled,
            send_mode=send_mode,
        ):
            self.run_row_indexes = None
            return
        fallback_schedule = None if send_mode == "macro_retry" else self.common_schedule_datetime()
        direct_count, direct_targets = direct_send_target_preview(
            selected,
            fallback_schedule,
            direct_enabled,
            self.direct_mappings,
            self.direct_mapping_labels,
            excluded_names=blocked_direct_names,
        )
        routes = delivery_routes((row for _, row in selected), fallback_schedule,
                                 direct_enabled, self.direct_mappings, blocked_direct_names)
        target_preview = "\n".join(direct_targets[:12])
        if len(direct_targets) > 12:
            target_preview += f"\n- 그 외 {len(direct_targets) - 12}곳"
        route_text = route_summary(routes)
        screen_note = "\n화면·예약 등록은 마우스와 키보드를 사용합니다. Shift+Esc로 중단할 수 있습니다." if any(r in {"화면", "예약 등록"} for r in routes) else "\n이 작업은 PC 화면을 사용하지 않습니다."
        accepted = messagebox.askyesno(
            "직접 전송 대상 확인" if direct_count else "발송 내용 확인",
            f"{route_text}{screen_note}\n\n{target_preview}\n\n미리보기의 수신자·본문·첨부를 확인했습니다. 계속할까요?",
        )
        if not accepted:
            self.status_var.set("발송 취소")
            self.run_row_indexes = None
            return
        self.run_row_indexes = [index for index, _ in selected]
        if any(schedule_datetime_for_row(row, fallback_schedule) is None and not row_uses_direct_send(row, None, direct_enabled)
               for _, row in selected):
            self.save_coordinates_from_fields(quiet=True)
        request = self.build_send_run_request(
            selected,
            blocked_direct_names=blocked_direct_names,
            send_mode=send_mode,
            direct_enabled_override=direct_enabled,
        )
        supervisor = SendRunSupervisor(lambda event: self.post("run_event", event))
        self.active_run = supervisor
        self._active_send_mode = send_mode
        self.stop_event = supervisor.cancel_event
        self.results = []
        self._progress_results = {}
        self._running_row_indexes = set()
        self.reset_run_preview_statuses(selected)
        self.refresh_retry_failed_action()
        self.refresh_delivery_routes()
        self.progress_var.set(0)
        self.run_total = len(selected)
        self.started_at = time.time()
        self.status_var.set("실행 준비")
        self.set_attachment_edit_state(True)
        self.start_button.state(["disabled"])
        if hasattr(self, "retry_failed_button"):
            self.retry_failed_button.state(["disabled"])
        self.test_one_check.state(["disabled"])
        self.direct_toggle.state(["disabled"])
        self.mapping_button.state(["disabled"])
        if hasattr(self, "manual_mapping_button"):
            self.manual_mapping_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.worker = threading.Thread(target=self.send_worker, args=(request, supervisor), daemon=True)
        self.worker.start()

    def start_sending(
        self,
        row_indexes: list[int] | None = None,
        *,
        send_mode: str = "normal",
    ) -> None:
        if self.__dict__.get("_document_loading"):
            messagebox.showinfo("파일 읽는 중", "파일 읽기가 끝난 뒤 발송을 시작해 주세요.")
            return
        if self.__dict__.get("_diagnostic_busy"):
            messagebox.showinfo("연결 점검 중", "연결 점검을 마친 뒤 발송을 시작해 주세요.")
            return
        if self.__dict__.get("_auto_match_waiting_to_send"):
            messagebox.showinfo("발송 준비 중", "채팅방 확인을 기다리거나 [중지]로 준비를 취소해 주세요.")
            return
        if send_mode == "normal" and self.__dict__.get("_restored_retry_only"):
            messagebox.showinfo("2차 전송 대기 복원", "이 명단은 직접 전송 실패 기록입니다. [직접 실패 2차 전송] 버튼으로 진행하세요.")
            return
        if self.active_run is not None or (self.worker and self.worker.is_alive()):
            messagebox.showinfo("실행 중", "이미 발송 작업이 실행 중입니다.")
            return
        if send_mode not in {"normal", "macro_retry"}:
            messagebox.showerror("실행 오류", "지원하지 않는 발송 실행 방식입니다.")
            return
        self.run_row_indexes = list(row_indexes) if row_indexes is not None else None
        selected = self.filtered_rows(self.run_row_indexes)
        if send_mode == "normal" and bool(self.direct_enabled_var.get()):
            direct_names = self.direct_names_for_selected(selected)
            if direct_names:
                self.set_auto_match_send_wait(True)
                preparation_token = self._active_send_preparation_token
                self.status_var.set("발송 직전 카카오 채팅방 목록을 확인하는 중…")
                self.request_auto_chat_matches(
                    direct_names,
                    reason="before_send",
                    on_complete=lambda verified, selected=selected, token=preparation_token: self.continue_start_after_auto_match(
                        selected, verified, preparation_token=token,
                    ),
                )
                return
        self.start_sending_after_preflight(selected, send_mode=send_mode)

    def stop_sending(self) -> None:
        if self.__dict__.get("_document_loading"):
            self.cancel_document_load()
            self.status_var.set("파일 읽기 취소됨 · 기존 미리보기 유지")
            self.log("중지", "파일 읽기를 취소했습니다. 읽던 파일은 적용하지 않습니다.")
            return
        if self.__dict__.get("_auto_match_waiting_to_send"):
            # Invalidate the attempt before cancelling its read: cancelling
            # invokes the pending callback synchronously. Late automatic and
            # manual callbacks must also be unable to start a newer attempt.
            self._active_send_preparation_token = None
            self.cancel_auto_chat_matches()
            self.set_auto_match_send_wait(False)
            self.run_row_indexes = None
            self.status_var.set("발송 준비 취소됨 · 메시지를 보내지 않았습니다.")
            self.log("중지", "발송 직전 채팅방 확인을 취소했습니다. 다시 시작할 수 있습니다.")
            return
        active_run = getattr(self, "active_run", None)
        if active_run is not None:
            active_run.request_cancel()
        else:
            self.stop_event.set()
        self.status_var.set("중지 요청됨")
        self.log("중지", "현재 행 처리 후 작업을 멈춥니다. 화면 제어 중 즉시 멈추려면 마우스를 화면 왼쪽 위 모서리로 이동하세요.")

    def request_close(self) -> None:
        """Cancel an active run before allowing the Tk window to disappear."""

        self.cancel_document_load()
        active_run = getattr(self, "active_run", None)
        if self._screen_use_is_active():
            self._close_requested = True
            self._close_deadline = time.monotonic() + CLOSE_WAIT_TIMEOUT_SECONDS
            self.request_screen_use_stop(source="창 닫기")
            if active_run is None:
                self._schedule_close_poll()
                return
        if active_run is None:
            self._close_requested = True
            self.destroy()
            return

        self._close_requested = True
        self._close_deadline = time.monotonic() + CLOSE_WAIT_TIMEOUT_SECONDS
        active_run.request_cancel()
        self.status_var.set("종료 요청됨 · 현재 작업을 안전하게 정리하는 중")
        self.log("종료", "발송 작업 취소와 연결 정리를 기다립니다.")
        # The terminal event must follow all queued guard/journal events.
        # Reading completed here would overtake them and discard their state.
        self._schedule_close_poll()

    def _schedule_close_poll(self) -> None:
        if not getattr(self, "_close_requested", False):
            return
        if getattr(self, "_close_poll_after_id", None) is None:
            self._close_poll_after_id = self.after(100, self._poll_close)

    def _poll_close(self) -> None:
        self._close_poll_after_id = None
        if not getattr(self, "_close_requested", False):
            return

        if self._screen_use_is_active():
            deadline = getattr(self, "_close_deadline", None)
            if deadline is not None and time.monotonic() >= deadline:
                # A modal warning would steal focus from the exact KakaoTalk
                # window that the cleanup code is trying to close.  Keep the
                # non-modal guard/status visible and continue waiting.
                self.status_var.set("종료 대기 · 현재 카카오톡 채팅방을 안전하게 정리 중")
                self.log("종료", "화면 자동매칭 정리가 끝난 뒤 창을 닫습니다.")
                self._close_deadline = time.monotonic() + CLOSE_WAIT_TIMEOUT_SECONDS
            self._schedule_close_poll()
            return

        active_run = getattr(self, "active_run", None)
        if active_run is None:
            self.destroy()
            return
        if active_run.completed is not None:
            self._schedule_close_poll()
            return

        worker = getattr(self, "worker", None)
        if worker is not None and not worker.is_alive():
            self.log("종료", "작업 스레드는 끝났지만 종료 스냅샷이 없어 창을 닫습니다.")
            self.destroy()
            return

        deadline = getattr(self, "_close_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            choice = messagebox.askyesnocancel(
                "종료 대기 시간 초과",
                "발송 작업 정리가 5초 안에 끝나지 않았습니다.\n\n"
                "예: 5초 더 기다림\n"
                "아니오: 즉시 종료\n"
                "취소: 창 닫기 취소",
            )
            if choice is True:
                self._close_deadline = time.monotonic() + CLOSE_WAIT_TIMEOUT_SECONDS
            elif choice is False:
                self.destroy()
                return
            else:
                self._close_requested = False
                self._close_deadline = None
                self.status_var.set("창 닫기 취소 · 발송 중지 요청은 유지됩니다.")
                return
        self._schedule_close_poll()

    def build_send_run_request(
        self,
        selected: list[tuple[int, MessageRow]],
        *,
        blocked_direct_names: Iterable[Any] = (),
        send_mode: str = "normal",
        direct_enabled_override: bool | None = None,
    ) -> SendRunRequest:
        coordinate_override: dict[str, Any] = {}
        try:
            coordinate_override = {
                "search_box": {
                    "x": int(float(self.coord_x_var.get())),
                    "y": int(float(self.coord_y_var.get())),
                }
            }
        except (TypeError, ValueError, OverflowError):
            # Pure direct runs and fully blocked runs never use the PC screen.
            # Macro preflight has already rejected invalid coordinates.
            pass
        config = normalize_runtime_config(deep_merge(self.config_data, coordinate_override))
        # A reservation-only run has no need to click a saved absolute search
        # coordinate.  Those coordinates can point at another window after a
        # monitor/layout change; the main-window Ctrl+F path is already gated
        # by the exact KakaoTalk foreground check in focus_main_and_click_search.
        common_schedule = None if send_mode == "macro_retry" else self.common_schedule_datetime()
        if (
            selected
            and send_mode == "normal"
            and all(schedule_datetime_for_row(row, common_schedule) is not None for _, row in selected)
            and not config.get("open_search_shortcut")
        ):
            config["open_search_shortcut"] = ["ctrl", "f"]
        direct_enabled = (
            bool(self.direct_enabled_var.get())
            if direct_enabled_override is None
            else bool(direct_enabled_override)
        )
        sources = dict(self.__dict__.get("_recovery_sources_by_number", {}))
        if send_mode == "macro_retry":
            sources.update(self.__dict__.get("_last_run_sources_by_number", {}))
        return SendRunRequest(
            selected=tuple(selected),
            config=config,
            countdown=int(self.countdown_var.get() or 0),
            delay_scale=float(self.delay_scale_var.get() or 1.0),
            common_schedule=common_schedule,
            continue_on_error=self.continue_error_var.get(),
            confirm_each=self.confirm_each_var.get(),
            direct_enabled=direct_enabled,
            chat_mappings=tuple(self.direct_mappings.items()),
            direct_config=dict(self.direct_config.to_dict()),
            blocked_direct_names=tuple(str(name).strip() for name in blocked_direct_names if str(name).strip()),
            send_mode=send_mode,
            source_path=str(self.file_path) if self.__dict__.get("file_path") else None,
            recovery_of=self.__dict__.get("_recovery_of"),
            recovery_rows=tuple((i, self.__dict__.get("_recovery_source_indexes", {})[i])
                                for i, _ in selected if i in self.__dict__.get("_recovery_source_indexes", {})),
            recovery_sources=tuple((i, *sources[row.row_number]) for i, row in selected if row.row_number in sources),
        )

    def _tablet_reauth_event(self, event: str, detail: str) -> None:
        # This callback runs on the auth worker. Tk mutations are routed back
        # through the existing UI queue.
        self.post("tablet_reauth", (event, detail))

    def _tablet_reauth_coordinator(
        self, config: DirectKakaoConfig | Mapping[str, Any] | None = None,
    ) -> TabletReauthCoordinator:
        resolved = (
            DirectKakaoConfig.from_dict(config.to_dict())
            if isinstance(config, DirectKakaoConfig)
            else DirectKakaoConfig.from_dict(config)
        )
        key = str(Path(resolved.credential_vault).resolve()).casefold()
        coordinators = self.__dict__.setdefault("_tablet_reauth_coordinators", {})
        coordinator = coordinators.get(key)
        if coordinator is None:
            coordinator = TabletReauthCoordinator(
                credential_vault=CredentialVault(resolved.credential_vault),
                authenticator=SpoilerTabletAuthenticator(event_callback=self._tablet_reauth_event),
            )
            coordinators[key] = coordinator
        return coordinator

    def create_direct_bridge(
        self,
        config: DirectKakaoConfig | Mapping[str, Any] | None = None,
        *,
        direct_enabled: bool = True,
    ) -> Any:
        coordinator = self._tablet_reauth_coordinator(config)
        return KakaoDirectBridge(
            config,
            direct_enabled=direct_enabled,
            credential_reloader=coordinator,
        )

    @staticmethod
    def close_direct_bridge(bridge: Any) -> None:
        if bridge is None:
            return
        close = getattr(bridge, "close", None)
        if callable(close):
            close()

    def send_worker(self, request: SendRunRequest, supervisor: SendRunSupervisor | None = None) -> SendRunCompleted:
        """Own durable results and the visible, cancellable PC lease for one run."""
        run = supervisor or SendRunSupervisor(lambda event: self.post("run_event", event))
        journal = self.__dict__.get("run_journal")
        journal_run = None

        def perform_work(context: SendRunContext) -> None:
            nonlocal journal_run
            if self.__dict__.get("_journal_error"):
                raise RunJournalError(self._journal_error)
            if journal is not None:
                stored_rows = tuple((i, replace(row, scheduled_at=(None if request.send_mode == "macro_retry"
                    else schedule_datetime_for_row(row, request.common_schedule)))) for i, row in request.selected)
                journal_run = journal.begin_run(str(context.run_id), stored_rows, source_path=request.source_path,
                    metadata={"send_mode": request.send_mode, "direct_enabled": request.direct_enabled,
                              "common_schedule": format_schedule_datetime(request.common_schedule),
                              "recovery_of": request.recovery_of,
                              "recovery_row_indexes": {str(new): old for new, old in request.recovery_rows},
                              "recovery_sources": {str(new): {"run_id": parent, "row_index": old}
                                                   for new, parent, old in request.recovery_sources}})
                def attempt_observed(index: int, route: str) -> None:
                    try:
                        journal_run.mark_attempt(index, route)
                    except RunJournalError:
                        context.emit("journal_error", "실행 기록을 저장하지 못해 중단했습니다. 저장 공간을 확인하고 앱을 다시 시작한 뒤 [실행 기록]에서 남은 작업을 확인하세요.")
                        raise
                context.observe_attempts(attempt_observed)
                context.emit("journal_started", tuple((i, row.row_number) for i, row in request.selected))

            def result_observed(record: Mapping[str, Any]) -> None:
                if journal_run is not None:
                    try:
                        journal_run.add_result(dict(record))
                    except RunJournalError:
                        context.emit("journal_error", "전송 결과를 기록하지 못했습니다. 현재 행을 다시 보내지 마세요. 저장 공간을 확인하고 앱을 다시 시작한 뒤 [실행 기록]과 대화방에서 발송 여부를 확인하세요.")
                        raise
                context.emit("result", dict(record))
            context.observe_results(result_observed)

            def check_cancel() -> None:
                if context.cancel_requested:
                    if request.send_mode == "macro_retry":
                        preserve_macro_retry_queue(context, request.selected,
                            detail="2차 화면 전송 시작 전 중단되어 아직 시도하지 않았습니다.")
                    context.raise_if_cancelled()
            check_cancel()
            if not selected_rows_require_macro(request.selected, request.common_schedule, request.direct_enabled):
                self._send_worker_impl(request, context)
                return
            token = f"send:{context.run_id}"
            try:
                context.emit("status", "화면 제어 차례를 기다리는 중 · 중지 가능")
                with pc_kakao_ui_lease(cancel_event=context.cancel_event):
                    check_cancel()
                    acknowledgement = threading.Event()
                    context.emit("screen_use_state", (token, -1, True, context.cancel_event, acknowledgement))
                    try:
                        # Real windows must display the warning before the first PC action.
                        # Headless ports have no render loop and never perform real PC input.
                        if "tk" in self.__dict__:
                            deadline = time.monotonic() + SCREEN_USE_ACK_TIMEOUT_SECONDS
                            while not acknowledgement.wait(0.05):
                                check_cancel()
                                if time.monotonic() >= deadline:
                                    raise FocusSafetyError("화면 사용 경고를 표시하지 못해 시작하지 않았습니다.")
                        check_cancel()
                        self._send_worker_impl(request, context)
                    finally:
                        context.emit("screen_use_state", (token, -1, False, context.cancel_event, None))
            except PcUiLeaseCancelled:
                check_cancel()
                raise

        def work(context: SendRunContext) -> None:
            status = "completed"
            try:
                perform_work(context)
            except SendRunCancelled:
                status = "cancelled"
                raise
            except Exception:
                status = "failed"
                raise
            finally:
                if journal_run is not None:
                    try:
                        journal_run.finish("cancelled" if context.cancel_requested else status)
                    except RunJournalError:
                        context.emit("journal_error", "실행 기록의 종료 상태를 저장하지 못했습니다. 앱을 다시 시작한 뒤 [실행 기록]에서 처리 결과를 확인하세요.")
                        raise
        return run.run(work)

    def _send_worker_impl(self, request: SendRunRequest, context: SendRunContext) -> None:
        service = SendService(SendPorts(
            bridge_factory=self.create_direct_bridge, bridge_close=self.close_direct_bridge,
            confirm_row=self.confirm_row, require_pyautogui=require_pyautogui,
            focus_kakao_window=focus_kakao_window, send_one=send_one,
            send_scheduled_one=send_scheduled_one, return_to_recent_chat_top=return_to_recent_chat_top,
            confirm_schedule_ready=self.confirm_schedule_ready,
            native_schedule_factory=lambda options, run_context: NativeScheduleSession(
                SCHEDULE_JOURNAL_PATH, LOG_DIR / 'reservations' / str(run_context.run_id),
                cancel_event=run_context.cancel_event,
                on_capacity=lambda view: run_context.emit('schedule_capacity', view)),
        ))
        service.execute(request, context)

    def confirm_row(
        self,
        row: MessageRow,
        index: int,
        total: int,
        scheduled_at: datetime | None,
        confirm_each: bool,
        context: SendRunContext | None = None,
    ) -> str:
        if not confirm_each:
            return "send"
        cancel_event = context.cancel_event if context is not None else self.stop_event
        response: queue.Queue[str] = queue.Queue(maxsize=1)
        if context is not None:
            context.emit("confirm", (row, index, total, scheduled_at, response))
        else:
            self.post("confirm", (row, index, total, scheduled_at, response))
        while not cancel_event.is_set():
            try:
                decision = response.get(timeout=0.1)
                if cancel_event.is_set():
                    return "stop"
                if decision == "stop":
                    cancel_event.set()
                return decision
            except queue.Empty:
                continue
        return "stop"

    def confirm_schedule_ready(
        self, row: MessageRow, scheduled_at: datetime, context: SendRunContext | None = None,
    ) -> bool:
        cancel_event = context.cancel_event if context is not None else self.stop_event
        if cancel_event.is_set():
            return False
        response: queue.Queue[bool] = queue.Queue(maxsize=1)
        payload = (row, scheduled_at, response)
        if context is not None:
            context.emit("confirm_schedule_ready", payload)
        else:
            self.post("confirm_schedule_ready", payload)
        while not cancel_event.is_set():
            try:
                return response.get(timeout=0.1) is True and not cancel_event.is_set()
            except queue.Empty:
                continue
        return False

    def show_schedule_review(self, row: MessageRow, scheduled_at: datetime, cancel_event: threading.Event) -> bool:
        """Tk-only review; a user compares the form because field readback is unavailable."""
        if cancel_event.is_set():
            return False
        dialog = tk.Toplevel(self)
        dialog.title("예약창 내용 대조")
        dialog.geometry("620x540")
        dialog.minsize(500, 400)
        dialog.transient(self)
        accepted = False
        finished = False
        poll_id = None

        def finish(value: bool = False) -> None:
            nonlocal accepted, finished
            if finished:
                return
            finished = True
            accepted = value is True and not cancel_event.is_set()
            if poll_id is not None:
                dialog.after_cancel(poll_id)
            dialog.destroy()

        def poll_cancel() -> None:
            nonlocal poll_id
            poll_id = None
            if cancel_event.is_set():
                finish(False)
            elif not finished:
                poll_id = dialog.after(100, poll_cancel)

        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        KoreanLabel(body, text="카카오톡 예약창의 실제 내용과 아래 내용을 대조하세요.", wraplength=550).pack(anchor="w")
        KoreanLabel(body, text="다르면 카카오톡에서 수정한 뒤 일치 확인을 누르세요.\n카카오톡의 등록 버튼은 직접 누르지 마세요. 중지하면 Hub는 등록하지 않습니다.", wraplength=550).pack(anchor="w", pady=(8, 12))
        period = "오전" if scheduled_at.hour < 12 else "오후"
        expected = (
            f"대상: {row.chat_name}\n"
            f"날짜: {scheduled_at:%Y-%m-%d}\n"
            f"시각: {period} {scheduled_at.hour % 12 or 12:02d}시 {scheduled_at.minute:02d}분\n\n"
            f"본문:\n{row.text}"
        )
        preview = ScrolledText(body, wrap="word", font=("Malgun Gothic", 10), height=12)
        preview.pack(fill="both", expand=True)
        preview.insert("1.0", expected)
        preview.configure(state="disabled")
        KoreanLabel(body, text="등록 입력과 새 접수 알림을 확인합니다. 슬롯은 등록 기록과 현재 시각으로 계산하며 실제 발송 완료 확인은 아닙니다.", wraplength=550).pack(anchor="w", pady=10)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="중지", command=lambda: finish(False)).pack(side="left")
        ttk.Button(buttons, text="내용 일치 확인 · 등록", command=lambda: finish(True)).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.bind("<Escape>", lambda _event: finish(False))
        dialog.grab_set()
        poll_id = dialog.after(100, poll_cancel)
        try:
            dialog.wait_window()
        finally:
            if not finished:
                finish(False)
        return accepted and not cancel_event.is_set()















    def log(self, category: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] [{category}] {message}\n"
        if self.__dict__.get("_feedback_batch_active"):
            self.__dict__.setdefault("_pending_log_lines", []).append(line)
            return
        self._append_log_text(line)

    def _append_log_text(self, text: str) -> None:
        self.log_text.configure(state="normal")
        try:
            self.log_text.insert("end", text)
            self.log_text.see("end")
        finally:
            self.log_text.configure(state="disabled")

    def flush_pending_visual_feedback(self) -> None:
        """Paint logs, route totals and the last row scroll once per queue slice."""
        lines = self.__dict__.get("_pending_log_lines", ())
        if lines:
            self._append_log_text("".join(lines))
            self._pending_log_lines = []
        item_id = self.__dict__.pop("_pending_tree_see", None)
        tree = self.__dict__.get("tree")
        if item_id is not None and tree is not None and tree.exists(item_id):
            tree.see(item_id)
        if self.__dict__.pop("_route_summary_dirty", False):
            routes = self.__dict__.get("_delivery_routes")
            if routes is not None:
                self.route_summary_var.set(route_summary(routes))

    def log_failed_summary(self, failed_rows: list[dict[str, Any]]) -> None:
        self.log("오류명단", format_failed_summary_count(len(failed_rows)))
        for item in failed_rows:
            detail = item.get("detail") or ""
            self.log("오류명단", f"행 {item.get('row')} | {item.get('chat_name')} | {detail}")
        if any(item.get("retry_transport") == "macro" for item in failed_rows):
            self.log("오류명단", format_failed_retry_instruction())

    def log_uncertain_summary(self, rows: list[dict[str, Any]]) -> None:
        self.log("확인 필요", f"전송 결과 불명확 {len(rows)}건 · 자동 재전송 제외")
        for item in rows:
            detail = item.get("detail") or ""
            self.log("확인 필요", f"행 {item.get('row')} | {item.get('chat_name')} | {detail}")

    def save_visible_log(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="로그 저장",
            defaultextension=".txt",
            initialdir=str(LOG_DIR),
            filetypes=(("텍스트 로그", "*.txt"), ("모든 파일", "*.*")),
        )
        if not path:
            return
        Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
        self.log("로그", f"화면 로그 저장: {path}")

    def open_log_folder(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_DIR)

    def show_help(self) -> None:
        messagebox.showinfo(
            "도움말",
            "[준비]\n"
            "1. CSV/XLSX 파일을 선택합니다.\n"
            "2. 직접 전송을 쓸 때는 파일을 연 뒤 자동 매칭이 실행됩니다. 남은 대상은 [자동 매칭] 재시도 또는 [수동 매칭]에서 검증된 1:1 방을 직접 선택할 수 있습니다.\n"
            "3. 예약·화면 발송·직접 실패 2차 전송이 있으면 카카오톡 메인 창을 열고 [화면 좌표 설정]을 실행합니다.\n"
            "4. 미리보기에서 대화명과 메시지를 수정하고 [파일 첨부], 공통 첨부 경로 또는 파일 드롭으로 모든 행에 같은 첨부를 준비합니다. 새 첨부는 기존 첨부를 교체하며 자동 발송하지 않습니다.\n\n"
            "[발송]\n"
            "5. 처음에는 [테스트 1건]으로 흐름을 확인하세요.\n"
            "6. [발송 시작]을 누르면 파일, 매칭, 좌표, 예약, 첨부를 자동 점검한 뒤 처리합니다.\n"
            "7. 직접 전송에 명시적으로 실패한 행만 [직접 실패 2차 전송]으로 PC 화면에서 다시 보냅니다. 매칭 실패와 [확인 필요]는 제외됩니다.\n\n"
            "[직접/예약/첨부]\n"
            "8. 직접 전송 ON: 예약 없는 텍스트·첨부를 chat ID로 전송합니다. 비공식 방식이라 차단 가능성이 있습니다.\n"
            "9. 예약 행은 ON/OFF와 무관하게 카카오톡 예약창에 지금 등록하므로 등록 후 PC를 꺼도 됩니다.\n"
            "10. 예약 메시지는 텍스트만 지원합니다. 첨부가 있는 행은 즉시 발송으로 분리하세요.\n"
            "11. 여러 이미지는 카카오톡의 이미지 묶어보내기 설정이 켜져 있으면 묶음으로 전송됩니다.\n\n"
            "화면 사용 안내: 자동 매칭·파일 등록·목록 새로고침·직접 전송은 화면을 사용하지 않습니다. 일반 화면 전송·첨부·예약 등록·[선택 대상 PC에서 찾기]·[직접 실패 2차 전송]에서는 상단에 [화면 사용 중]이 표시됩니다.\n"
            "중단하려면 [화면 사용 중단]을 누르거나 Shift+Esc를 누르세요. 현재 채팅방을 정리한 뒤 멈춥니다.\n\n"
            "[실행 기록]에서 미시도·발송 전 중단 행과 2차 전송 대기를 복원할 수 있습니다. 성공·확인 필요 행은 재전송하지 않습니다.\n"
            "[연결 준비 점검]에서 설치·인증·좌표 상태와 인증 재등록 방법을 확인하세요.\n\n"
            "비상 중지: 마우스를 화면 왼쪽 위 모서리로 이동하거나 [중지]를 누르세요.",
        )


def main() -> None:
    app = KakaoSenderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
