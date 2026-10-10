"""Local attachment preparation; importing this module never opens a window."""
from __future__ import annotations

import importlib
import csv
import io
import os
from pathlib import Path
from typing import Any, Callable, Iterable
import tkinter as tk

def validated_attachment_paths(paths: Iterable[str | Path], *, base_dir: Path | None = None) -> tuple[Path, ...]:
    """Validate the entire selection before a caller changes its document."""
    result: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        try:
            path = path.resolve(strict=True)
            if not path.is_file():
                raise ValueError(f"일반 파일만 첨부할 수 있습니다: {path}")
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"첨부 파일을 찾거나 읽을 수 없습니다: {path}") from exc
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    if not result:
        raise ValueError("첨부할 파일 경로를 입력하거나 파일을 선택해 주세요.")
    return tuple(result)


def attachment_paths_from_text(text: str, *, base_dir: Path | None = None) -> tuple[Path, ...]:
    # A single actual filename may itself contain a semicolon. Prefer that
    # literal path before applying the existing CSV-style separator grammar.
    literal = _unquote_path(text)
    if literal and "\n" not in literal and "\r" not in literal:
        try:
            return validated_attachment_paths((literal,), base_dir=base_dir)
        except ValueError:
            pass
    try:
        records = csv.reader(io.StringIO(_normalize_path_separators(text.strip())),
                             delimiter=";", skipinitialspace=True, strict=True)
        paths = [_unquote_path(value) for record in records for value in record if value.strip()]
    except csv.Error as exc:
        raise ValueError("첨부 경로의 인용부호를 확인해 주세요. 파일 선택이나 드롭도 사용할 수 있습니다.") from exc
    return validated_attachment_paths(paths, base_dir=base_dir)


def _unquote_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _normalize_path_separators(text: str) -> str:
    """Keep the legacy pipe separator outside CSV-style quoted filenames."""
    result: list[str] = []
    quoted = False
    for character in text:
        if character == '"':
            quoted = not quoted
        result.append(";" if character == "|" and not quoted else character)
    return "".join(result)


def format_attachment_path_input(paths: Iterable[Path]) -> str:
    """Lossless GUI field format; the document CSV format remains separate."""
    stream = io.StringIO()
    csv.writer(stream, delimiter=";", lineterminator="\n").writerow(str(path) for path in paths)
    return stream.getvalue().removesuffix("\n")


def attachment_paths_from_drop(data: str, splitlist: Callable[[str], tuple[str, ...]]) -> tuple[Path, ...]:
    if not isinstance(data, str):
        raise ValueError("파일 드롭 정보를 읽을 수 없습니다.")
    try:
        paths = splitlist(data)
    except (tk.TclError, ValueError, TypeError) as exc:
        raise ValueError("파일 드롭 경로 형식을 읽을 수 없습니다. 파일 선택을 사용해 주세요.") from exc
    return validated_attachment_paths(paths)


def enable_file_drop(root: Any, widgets: Iterable[Any], callback: Callable, *, importer: Callable = importlib.import_module) -> tuple[bool, str]:
    """Register only the supplied widget subtrees, retaining input fallback.

    Windows sends a drop to the child under the pointer, not necessarily its
    parent frame. Register descendants as well so entries, labels, buttons
    and empty frame space share the same attachment preparation callback.
    """
    registered = []
    try:
        provider = importer("tkinterdnd2")
        provider.TkinterDnD.require(root)
        pending = list(widgets)
        seen: set[int] = set()
        while pending:
            widget = pending.pop(0)
            identity = id(widget)
            if identity in seen:
                continue
            seen.add(identity)
            # Track before registration too: a provider can partially install
            # the target before raising. Cleanup remains safe and best effort.
            registered.append(widget)
            widget.drop_target_register(provider.DND_FILES)
            widget.dnd_bind("<<Drop>>", callback)
            children_reader = getattr(widget, "winfo_children", None)
            children = children_reader() if callable(children_reader) else ()
            # Real Tk returns a list; accept tuple-based test doubles, but do
            # not recurse into Mock's auto-created, non-iterable attributes.
            if isinstance(children, (list, tuple)):
                pending.extend(children)
    except (ImportError, AttributeError, TypeError, RuntimeError, tk.TclError, OSError):
        for widget in reversed(registered):
            try:
                widget.drop_target_unregister()
            except (AttributeError, TypeError, RuntimeError, tk.TclError, OSError):
                pass
        return False, "파일 드롭을 사용할 수 없습니다. 경로 입력 또는 파일 첨부를 사용해 주세요."
    return True, "파일을 첨부 영역에 놓으세요. 첨부 준비만 하며 자동으로 발송하지 않습니다."
