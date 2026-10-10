"""Hub panels responsibilities; app state remains on the Tk owner thread."""
from __future__ import annotations
from kakao_gui_text import KoreanLabel

import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from kakao_run_journal import RunJournalError
from kakao_diagnostics import diagnose_local, connection_guidance
from kakao_gui_matching import safe_auto_match_failure


@dataclass(frozen=True)
class RecoveryOfferResult:
    token: str
    journal: Any
    count: int = 0
    error: str = ""


@dataclass(frozen=True)
class RunHistoryResult:
    token: str
    request: int
    journal: Any
    kind: str
    data: Any = None
    error: str = ""


class HubRunPanelsMixin:
    def offer_run_recovery(self) -> None:
        if self.__dict__.get("_journal_error"):
            self.log("기록", self._journal_error)
            self.status_var.set("실행 기록 점검 필요 · 연결 준비 점검 / 실행 기록을 확인하세요")
            return
        journal = self.__dict__.get("run_journal")
        if journal is None:
            return
        self._recovery_offer_token = token = str(time.monotonic_ns())
        self._recovery_offer_status = self.status_var.get()
        post = self.post

        def worker() -> None:
            try:
                count = len(journal.list_recoverable_runs())
                result = RecoveryOfferResult(token, journal, count)
            except RunJournalError as exc:
                result = RecoveryOfferResult(token, journal, error=str(exc))
            except Exception:
                result = RecoveryOfferResult(token, journal, error="이전 실행 기록을 점검하지 못했습니다. [실행 기록]에서 저장소를 확인하세요.")
            post("recovery_offer_result", result)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError:
            self.handle_recovery_offer_result(RecoveryOfferResult(
                token, journal, error="이전 실행 기록 점검을 시작하지 못했습니다. [실행 기록]에서 다시 확인하세요.",
            ))

    def handle_recovery_offer_result(self, result: RecoveryOfferResult) -> None:
        if (result.token != self.__dict__.get("_recovery_offer_token")
                or result.journal is not self.__dict__.get("run_journal")
                or self.__dict__.get("_close_requested")):
            return
        self._recovery_offer_token = None
        if result.error:
            self.log("기록", result.error)
        elif result.count:
            self.log("복원", f"확인할 이전 실행 기록 {result.count}개가 있습니다. [실행 기록]에서 미시도·실패·확인 필요를 확인하세요.")
            # The scan can finish after a file load or send has changed the
            # primary status. Keep that newer operation visible.
            if (self.status_var.get() == self.__dict__.get("_recovery_offer_status")
                    and self.__dict__.get("active_run") is None
                    and not self.__dict__.get("_auto_match_loading")
                    and not self.__dict__.get("_journal_error")):
                self.status_var.set(f"이전 실행 기록 {result.count}개 확인 가능")


    def restore_run_preview(self, recovery: Any, *, retry_only: bool = False) -> bool:
        """Restore only unattempted work or explicit macro retry candidates; never send."""
        if self.preview_edit_locked():
            return False
        if getattr(recovery, "is_active", False):
            messagebox.showinfo("실행 중인 기록", "다른 창에서 아직 실행 중인 작업입니다. 종료 후 복원해 주세요.")
            return False
        if retry_only:
            eligible = {r["row_index"] for r in recovery.results
                        if r.get("status") == "error" and r.get("retry_transport") == "macro"}
        else:
            eligible = set(recovery.pending_row_indexes)
            eligible.update(r["row_index"] for r in recovery.results if r.get("status") == "stopped")
        selected = [(i, row) for i, row in recovery.selected if i in eligible]
        if not selected:
            messagebox.showinfo("복원 대상 없음", "이 종류의 남은 작업이 없습니다. 성공·확인 필요 행은 재전송하지 않습니다.")
            return False
        index_map = {old: new for new, (old, _) in enumerate(selected)}
        self.cancel_auto_chat_matches()
        self.cancel_cell_edit()
        self.rows = [row for _, row in selected]
        self.file_path = Path(recovery.source_path) if recovery.source_path else None
        self.results = [dict(r, row_index=index_map[r["row_index"]]) for r in recovery.results
                        if retry_only and r["row_index"] in index_map]
        self._progress_results = {}
        self._running_row_indexes = set()
        self.run_total = len(self.rows)
        self.run_row_indexes = None
        self.undo_stack = []
        self._recovery_of = recovery.run_id
        self._recovery_source_indexes = {new: old for old, new in index_map.items()}
        self._recovery_sources_by_number = {row.row_number: (recovery.run_id, old) for old, row in selected}
        self._recovery_retry_records_by_number = {self.rows[r["row_index"]].row_number: dict(r) for r in self.results}
        self._last_run_sources_by_number = {}
        self._restored_retry_only = retry_only
        self.common_schedule_var.set(False)
        self.schedule_at_var.set("")
        self.progress_var.set(0)
        self.populate_table()
        for record in self.results:
            self.update_row_status(record["row_index"], "오류")
        self.refresh_preview_count()
        if self.file_path is None:
            self.file_var.set("실행 기록에서 복원한 명단")
        self.update_summary()
        self.status_var.set("2차 전송 대기 복원 · 전용 버튼으로 진행" if retry_only else "미시도 명단 복원 · 확인 후 발송 시작")
        self.log("복원", f"기록에서 {len(self.rows)}건을 불러왔습니다. 아직 발송하지 않았습니다.")
        return True


    def open_run_history(self) -> None:
        if self.__dict__.get("run_journal") is None:
            messagebox.showerror("실행 기록", self.__dict__.get("_journal_error") or "실행 기록 저장소를 열 수 없습니다.")
            return
        existing = self.__dict__.get("_run_history_view")
        if existing is not None and existing["dialog"].winfo_exists():
            existing["dialog"].lift()
            return
        dialog = tk.Toplevel(self)
        dialog.title("실행 기록 / 중단 작업 복원")
        dialog.geometry("820x520")
        dialog.transient(self)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)
        KoreanLabel(dialog, text="성공·확인 필요는 재전송 대상에서 제외됩니다. 기록을 불러와도 자동 발송하지 않습니다.", wraplength=780).grid(row=0, column=0, sticky="w", padx=14, pady=12)
        token = str(time.monotonic_ns())
        value = tk.StringVar(value="실행 기록 목록을 불러오는 중...")
        combo = ttk.Combobox(dialog, textvariable=value, state="disabled")
        combo.grid(row=1, column=0, sticky="ew", padx=14)
        table = ttk.Treeview(dialog, columns=("row", "chat", "state", "route"), show="headings")
        for col, title, width in (("row", "행", 55), ("chat", "대화명", 250), ("state", "결과", 180), ("route", "방식", 140)):
            table.heading(col, text=title)
            table.column(col, width=width)
        table.grid(row=2, column=0, sticky="nsew", padx=14, pady=10)
        detail = tk.StringVar(value="실행 기록 목록을 불러오는 중입니다.")
        KoreanLabel(dialog, textvariable=detail, wraplength=780).grid(row=3, column=0, sticky="w", padx=14)
        def inspect_record(_event: Any = None) -> None:
            view = self.__dict__.get("_run_history_view")
            if view is not None and view["token"] == token:
                run_id = view["choices"].get(value.get())
                if run_id:
                    self._request_run_history("record", run_id)

        def restore(retry: bool) -> None:
            view = self.__dict__.get("_run_history_view")
            if (view is not None and view["token"] == token and not view["loading"]
                    and view["recovery"] is not None
                    and self.restore_run_preview(view["recovery"], retry_only=retry)):
                dialog.destroy()

        def acknowledge() -> None:
            view = self.__dict__.get("_run_history_view")
            if (view is not None and view["token"] == token and not view["loading"]
                    and view["recovery"] is not None):
                self._request_run_history("acknowledge", view["recovery"].run_id)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=4, column=0, sticky="e", padx=14, pady=14)
        actions = []
        for label, command in (("미시도 / 발송 전 중단 불러오기", lambda: restore(False)),
                               ("2차 전송 대기 불러오기", lambda: restore(True)),
                               ("기록 확인 완료", acknowledge)):
            button = ttk.Button(buttons, text=label, command=command, state="disabled")
            button.pack(side="left", padx=4)
            actions.append(button)
        self._run_history_view = {
            "token": token, "request": 0, "dialog": dialog, "table": table,
            "detail": detail, "value": value, "combo": combo, "choices": {},
            "recovery": None, "actions": tuple(actions), "loading": True,
            "journal": self.run_journal, "cancel": threading.Event(),
        }

        def close_history(event: Any) -> None:
            if event.widget is dialog:
                view = self.__dict__.get("_run_history_view")
                if view is not None and view["token"] == token:
                    view["cancel"].set()
                    self._run_history_view = None

        dialog.bind("<Destroy>", close_history, add="+")
        combo.bind("<<ComboboxSelected>>", inspect_record)
        self._request_run_history("summaries")

    def _request_run_history(self, kind: str, run_id: str | None = None) -> None:
        """Snapshot a disk operation; the worker never sees dialog widgets."""
        view = self.__dict__.get("_run_history_view")
        if view is None:
            return
        view["cancel"].set()
        view["cancel"] = cancel = threading.Event()
        view["request"] += 1
        request, token, journal = view["request"], view["token"], view["journal"]
        view["loading"] = True
        if kind != "acknowledge":
            view["recovery"] = None
            view["table"].delete(*view["table"].get_children())
        for action in view["actions"]:
            action.state(["disabled"])
        view["combo"].configure(state="readonly" if kind == "record" else "disabled")
        view["detail"].set("기록 확인 완료를 저장하는 중입니다." if kind == "acknowledge" else "실행 기록을 불러오는 중입니다.")
        post = self.post

        def worker() -> None:
            if cancel.is_set():
                return
            try:
                if kind == "summaries":
                    data = tuple((f"{s.created_at[:19]} UTC · {s.row_count}건 · {s.status} · {s.run_id[:8]}", s.run_id)
                                 for s in journal.list_runs())
                elif kind == "record":
                    recovery = journal.load_recovery(run_id)
                    records = {r["row_index"]: r for r in recovery.results}
                    labels = {"sent": "전송", "direct_sent": "직접 전송", "pasted": "붙여넣음", "scheduled": "예약 등록", "schedule_acknowledged": "예약 접수", "unknown": "확인 필요", "error": "실패", "skipped": "건너뜀", "stopped": "발송 전 중단", "transferred": "후속 실행으로 이전"}
                    rows = []
                    for index, row in recovery.selected:
                        if cancel.is_set():
                            return
                        record = records.get(index, {})
                        route = ("실행 " + record["child_run_id"][:8]) if record.get("child_run_id") else record.get("transport", "-")
                        rows.append((row.row_number, row.chat_name, labels.get(record.get("status"), "미시도"), route))
                    data = (recovery, tuple(rows))
                else:
                    journal.acknowledge(run_id)
                    data = None
                result = RunHistoryResult(token, request, journal, kind, data)
            except RunJournalError as exc:
                result = RunHistoryResult(token, request, journal, kind, error=str(exc))
            except Exception:
                result = RunHistoryResult(token, request, journal, kind, error="실행 기록을 처리하지 못했습니다. 저장소 상태를 확인하세요.")
            if not cancel.is_set():
                post("run_history_result", result)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError:
            message = ("기록 확인 완료를 저장하는 작업을 시작하지 못했습니다. 다시 시도해 주세요."
                       if kind == "acknowledge" else
                       "실행 기록을 읽는 작업을 시작하지 못했습니다. 창을 닫고 다시 열어 주세요.")
            self.handle_run_history_result(RunHistoryResult(token, request, journal, kind, error=message))

    def handle_run_history_result(self, result: RunHistoryResult) -> None:
        view = self.__dict__.get("_run_history_view")
        if (view is None or result.token != view["token"] or result.request != view["request"]
                or result.journal is not self.__dict__.get("run_journal")
                or self.__dict__.get("_close_requested") or not view["dialog"].winfo_exists()):
            return
        if result.error:
            view["loading"] = False
            view["detail"].set(result.error)
            if result.kind == "summaries":
                view["value"].set("실행 기록 목록 읽기 실패")
            view["combo"].configure(state="readonly" if view["choices"] else "disabled")
            if view["recovery"] is not None:
                for action in view["actions"]:
                    action.state(["!disabled"])
            return
        if result.kind == "summaries":
            view["choices"] = dict(result.data)
            view["combo"].configure(values=tuple(view["choices"]), state="readonly" if view["choices"] else "disabled")
            view["value"].set(next(iter(view["choices"]), "저장된 실행 없음"))
            if view["choices"]:
                self._request_run_history("record", view["choices"][view["value"].get()])
            else:
                view["loading"] = False
                view["detail"].set("저장된 실행 기록이 없습니다.")
        elif result.kind == "record":
            view["recovery"], rows = result.data
            self._render_run_history_rows(result, rows, 0)
        else:
            view["loading"] = False
            view["detail"].set("확인 완료로 표시했습니다. 기록은 계속 보관됩니다.")
            view["combo"].configure(state="readonly")
            for action in view["actions"]:
                action.state(["!disabled"])

    def _render_run_history_rows(self, result: RunHistoryResult, rows: tuple[Any, ...], offset: int) -> None:
        view = self.__dict__.get("_run_history_view")
        if (view is None or result.token != view["token"] or result.request != view["request"]
                or result.journal is not self.__dict__.get("run_journal")
                or self.__dict__.get("_close_requested") or not view["dialog"].winfo_exists()):
            return
        deadline = time.monotonic() + 0.008
        stop = min(offset + 150, len(rows))
        while offset < stop:
            view["table"].insert("", "end", values=rows[offset])
            offset += 1
            if time.monotonic() >= deadline:
                break
        if offset < len(rows):
            view["detail"].set(f"실행 기록 표시 중 · {offset}/{len(rows)}행")
            view["dialog"].after(1, lambda: self._render_run_history_rows(result, rows, offset))
            return
        view["loading"] = False
        view["detail"].set("현재 다른 창에서 실행 중 · 복원 불가" if getattr(view["recovery"], "is_active", False) else
                           "미시도 행은 새로 점검 후 발송하고, 명시적 직접 실패는 2차 전송 버튼으로 처리합니다.")
        for action in view["actions"]:
            action.state(["!disabled"])


    def open_connection_diagnostics(self) -> None:
        if self.__dict__.get("active_run") is not None or self.__dict__.get("_auto_match_loading"):
            messagebox.showinfo("작업 중", "발송 또는 매칭 작업을 마친 뒤 연결 상태를 점검해 주세요.")
            return
        existing = self.__dict__.get("_diagnostic_dialog")
        if existing is not None and existing.winfo_exists():
            if self._diagnostic_config_is_current():
                existing.lift()
                return
            existing.destroy()
        if self.__dict__.get("_diagnostic_busy"):
            messagebox.showinfo("연결 점검 중", "이전 연결 점검이 끝나면 다시 열어 주세요.")
            return
        dialog = tk.Toplevel(self)
        dialog.title("연결 준비 점검 / 인증 복구")
        dialog.geometry("760x430")
        dialog.transient(self)
        self._diagnostic_dialog = dialog
        self._diagnostic_token = token = str(time.monotonic_ns())
        self._diagnostic_config_snapshot = deepcopy(self.direct_config)
        self._diagnostic_pc_config_snapshot = deepcopy(self.config_data)
        self._diagnostic_local_ready = False
        text = ScrolledText(dialog, wrap="word", font=("Malgun Gothic", 10))
        text.pack(fill="both", expand=True, padx=14, pady=14)
        text.insert("end", "로컬 준비 상태를 점검하고 있습니다.\n")
        text.configure(state="disabled")
        self._diagnostic_text = text

        def check_server() -> None:
            if self.__dict__.get("_diagnostic_busy") or not self.__dict__.get("_diagnostic_local_ready"):
                return
            if not self._diagnostic_config_is_current():
                self._invalidate_diagnostic_config()
                return
            if self.__dict__.get("active_run") is not None or self.__dict__.get("_auto_match_loading"):
                messagebox.showinfo("작업 중", "발송 또는 매칭 작업을 마친 뒤 직접 연결을 확인해 주세요.")
                return
            self._diagnostic_busy = True
            self._diagnostic_check_button.state(["disabled"])
            config = deepcopy(self.direct_config)
            bridge_factory, post = self.create_direct_bridge, self.post
            def worker() -> None:
                try:
                    with bridge_factory(config, direct_enabled=False) as bridge:
                        chats = bridge.list_chats()
                    message = f"직접 연결 준비됨 · 채팅 목록 {len(chats)}개 확인\n메시지는 보내지 않았습니다."
                except Exception as exc:
                    failure = safe_auto_match_failure(exc)
                    message = f"{failure.cause} ({failure.code})\n" + "\n".join(connection_guidance(failure.code))
                post("diagnostic_result", (token, message, True))
            threading.Thread(target=worker, daemon=True).start()

        self._diagnostic_check_button = ttk.Button(dialog, text="직접 연결 확인 · 목록 조회만", command=check_server, state="disabled")
        self._diagnostic_check_button.pack(side="bottom", before=text, pady=(0, 14))
        config, pc_config = deepcopy(self.direct_config), deepcopy(self.config_data)
        post = self.post
        def local_worker() -> None:
            try:
                items = diagnose_local(config, pc_config)
                labels = {"ok": "준비", "warning": "확인", "error": "문제"}
                message = "\n\n".join(f"[{labels[i.state]}] {i.label}\n{i.detail}" for i in items)
                message += "\n\n인증이 refresh_rejected로 거절되면:\n" + "\n".join(connection_guidance("refresh_rejected"))
            except Exception:
                message = "로컬 준비 점검을 완료하지 못했습니다. 설치 경로와 실행환경을 확인하세요."
            post("diagnostic_result", (token, message, False))
        threading.Thread(target=local_worker, daemon=True).start()

    def _diagnostic_config_is_current(self) -> bool:
        return (self.__dict__.get("_diagnostic_config_snapshot") == self.direct_config
                and self.__dict__.get("_diagnostic_pc_config_snapshot") == self.config_data)

    def _invalidate_diagnostic_config(self) -> None:
        self._diagnostic_local_ready = False
        self._diagnostic_check_button.state(["disabled"])
        text = self._diagnostic_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", "연결 설정이 바뀌었습니다. 이 창을 닫고 다시 점검해 주세요.")
        text.configure(state="disabled")

    def handle_diagnostic_result(self, payload: Any) -> None:
        token, message, server = payload
        if token != self.__dict__.get("_diagnostic_token"):
            return
        if server:
            self._diagnostic_busy = False
        dialog = self.__dict__.get("_diagnostic_dialog")
        if dialog is None or not dialog.winfo_exists():
            return
        if ("_diagnostic_config_snapshot" in self.__dict__
                and not self._diagnostic_config_is_current()):
            self._invalidate_diagnostic_config()
            return
        if not server:
            self._diagnostic_local_ready = True
        if "_diagnostic_check_button" in self.__dict__:
            self._diagnostic_check_button.state(["!disabled"])
        text = self._diagnostic_text
        text.configure(state="normal")
        if not server:
            text.delete("1.0", "end")
        text.insert("end", ("\n\n" if server else "") + message)
        text.configure(state="disabled")
