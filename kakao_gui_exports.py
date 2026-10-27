"""Save immutable GUI snapshots with serialized local file writes."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import os
import tempfile
import threading
from tkinter import filedialog, messagebox
from typing import Any, Callable, Literal

from kakao_app_paths import LOG_DIR
from kakao_document import rows_to_csv_text
from kakao_sender import MessageRow


ExportKind = Literal["csv", "log"]


@dataclass(frozen=True)
class _ExportRequest:
    token: int
    kind: ExportKind
    path: Path
    snapshot: tuple[MessageRow, ...] | str
    post: Callable[[str, Any], None]
    log_directory: Path | None = None


@dataclass(frozen=True)
class ExportResult:
    token: int
    kind: ExportKind
    path: Path
    error: str | None = None


def _write_csv_atomically(path: Path, text: str, *, encoding: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_export(request: _ExportRequest) -> None:
    error = None
    try:
        if request.kind == "csv":
            # Keep the existing writer, UTF-8 encoding, BOM, and newline rules.
            text = "\ufeff" + rows_to_csv_text(request.snapshot)
            _write_csv_atomically(request.path, text, encoding="utf-8")
        else:
            if request.log_directory is not None:
                request.log_directory.mkdir(parents=True, exist_ok=True)
            text = request.snapshot
            request.path.write_text(text, encoding="utf-8")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    request.post("export_result", ExportResult(request.token, request.kind, request.path, error))


class _ExportWriter:
    """Serialize all accepted writes, including different aliases of one file.

    The queue belongs to this writer; it needs no Tk callback to start its next
    write. Accepted snapshots therefore finish even after the window closes.
    """

    def __init__(self) -> None:
        self._requests: deque[_ExportRequest] = deque()
        self._lock = threading.Lock()
        self._running = False

    def submit(self, request: _ExportRequest) -> None:
        with self._lock:
            self._requests.append(request)
            if self._running:
                return
            self._running = True
            try:
                # Saving is finite local work. Let accepted writes complete
                # after Tk exits rather than terminating them mid-file.
                threading.Thread(target=self._drain, name="kakao-file-export", daemon=False).start()
            except Exception:
                self._running = False
                self._requests.pop()
                raise

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._requests:
                    self._running = False
                    return
                request = self._requests.popleft()
            _write_export(request)


class HubExportsMixin:
    """Only chooser, snapshot capture, and completion rendering access Tk."""

    def request_preview_csv_export(self) -> bool:
        if self.__dict__.get("_close_requested"):
            return False
        if not self.rows:
            messagebox.showinfo("저장할 내용 없음", "먼저 CSV 또는 XLSX 파일을 선택하거나 행을 추가해 주세요.")
            return False
        file_path = self.__dict__.get("file_path")
        initial_name = f"{file_path.stem}_edited.csv" if file_path else "kakao_messages_edited.csv"
        path = filedialog.asksaveasfilename(
            title="편집본 CSV 저장", defaultextension=".csv", initialfile=initial_name,
            filetypes=(("CSV UTF-8", "*.csv"), ("모든 파일", "*.*")),
        )
        if not path or self.__dict__.get("_close_requested"):
            return False
        # MessageRow is frozen and owns immutable attachment/date values.
        return self._request_export("csv", Path(path), tuple(self.rows))

    def request_visible_log_export(self) -> bool:
        if self.__dict__.get("_close_requested"):
            return False
        path = filedialog.asksaveasfilename(
            title="로그 저장", defaultextension=".txt", initialdir=str(LOG_DIR),
            filetypes=(("텍스트 로그", "*.txt"), ("모든 파일", "*.*")),
        )
        if not path or self.__dict__.get("_close_requested"):
            return False
        # Include logs already received but buffered by a GUI feedback slice.
        flusher = self.__dict__.get("flush_pending_visual_feedback")
        if callable(flusher):
            flusher()
        else:
            flusher = getattr(type(self), "flush_pending_visual_feedback", None)
            if callable(flusher):
                flusher(self)
        text = self.log_text.get("1.0", "end")
        return self._request_export("log", Path(path), text)

    def _request_export(self, kind: ExportKind, path: Path, snapshot: tuple[MessageRow, ...] | str) -> bool:
        token = int(self.__dict__.get("_export_generation", 0)) + 1
        self._export_generation = token
        request = _ExportRequest(token, kind, path, snapshot, self.post, LOG_DIR if kind == "log" else None)
        pending = self.__dict__.setdefault("_exports_pending", {})
        pending[token] = request
        writer = self.__dict__.get("_export_writer")
        if writer is None:
            writer = _ExportWriter()
            self._export_writer = writer
        try:
            writer.submit(request)
        except Exception as exc:
            self.handle_export_result(ExportResult(token, kind, path, f"{type(exc).__name__}: {exc}"))
            return False
        self.log("저장" if kind == "csv" else "로그", f"{'편집본 CSV' if kind == 'csv' else '화면 로그'} 저장 중: {path}")
        return True

    def handle_export_result(self, result: ExportResult) -> None:
        pending = self.__dict__.get("_exports_pending", {})
        request = pending.get(result.token)
        if request is None or result.kind != request.kind or result.path != request.path:
            return
        pending.pop(result.token)
        if self.__dict__.get("_close_requested"):
            return
        if result.error is not None:
            title = "CSV 저장 실패" if result.kind == "csv" else "로그 저장 실패"
            messagebox.showerror(title, f"{result.path}\n\n{result.error}")
            self.log("오류", f"{title}: {result.path} · {result.error}")
        elif result.kind == "csv":
            self.log("저장", f"편집본 CSV 저장 완료: {result.path}")
        else:
            self.log("로그", f"화면 로그 저장: {result.path}")
