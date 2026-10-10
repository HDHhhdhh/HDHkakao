"""Pure document operations. Failed edits never mutate the active document."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from kakao_sender import MessageRow, load_messages, parse_attachment_paths, parse_schedule_datetime, format_schedule_datetime


@dataclass(frozen=True)
class PreviewDocument:
    source_path: Path
    rows: tuple[MessageRow, ...]


def load_document(path: Path, *, loader: Callable = load_messages) -> PreviewDocument:
    """Load completely before returning a new path/rows pair."""
    candidate = Path(path).resolve()
    return PreviewDocument(candidate, tuple(loader(candidate)))


def plan_clipboard_paste(
    rows: Sequence[MessageRow], raw_text: str, start_index: int, *, base_dir: Path | None = None,
    next_row_number: int | None = None,
) -> tuple[list[MessageRow], int]:
    parsed = [cells for cells in csv.reader(io.StringIO(raw_text), delimiter="\t")
              if any(cell.strip() for cell in cells)]
    if not parsed:
        return list(rows), 0
    if not 0 <= start_index <= len(rows):
        raise ValueError("붙여넣을 행 위치가 변경됐습니다. 다시 선택해 주세요.")
    candidate = list(rows)
    next_number = max(max((row.row_number for row in rows), default=1) + 1, next_row_number or 1)
    for offset, cells in enumerate(parsed):
        chat_name = cells[0].strip()
        text = cells[1] if len(cells) > 1 else ""
        attachments = parse_attachment_paths(cells[2], base_dir=base_dir) if len(cells) > 2 else ()
        try:
            scheduled_at = parse_schedule_datetime(cells[3]) if len(cells) > 3 else None
        except ValueError as exc:
            raise ValueError(f"붙여넣기 {offset + 1}번째 행: {exc}") from None
        target = start_index + offset
        old = rows[target] if target < len(rows) else None
        row = MessageRow(old.row_number if old else next_number, chat_name, text, attachments,
                         old.image_bundle if old else True, scheduled_at)
        if old:
            candidate[target] = row
        else:
            candidate.append(row)
            next_number += 1
    return candidate, len(parsed)


PREVIEW_HEADERS = ("카카오톡 대화명", "텍스트", "첨부", "예약일시")

def rows_to_csv_text(rows: list[MessageRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PREVIEW_HEADERS)
    for row in rows:
        writer.writerow([row.chat_name, row.text, format_attachment_paths(row.attachments), format_schedule_datetime(row.scheduled_at)])
    return buffer.getvalue()


def format_attachment_paths(paths: tuple[Path, ...]) -> str:
    return ";".join(str(path) for path in paths)


def attachment_summary(row: MessageRow) -> str:
    count = row.attachment_count
    if not count:
        return "-"
    images = row.image_count
    files = count - images
    parts: list[str] = []
    if images:
        parts.append(f"이미지 {images}")
    if files:
        parts.append(f"파일 {files}")
    return " · ".join(parts)


def row_with_attachments(row: MessageRow, attachments: tuple[Path, ...]) -> MessageRow:
    return MessageRow(row.row_number, row.chat_name, row.text, attachments, row.image_bundle, row.scheduled_at)


def rows_with_common_attachments(rows: list[MessageRow], attachments: tuple[Path, ...]) -> list[MessageRow]:
    return [row_with_attachments(row, attachments) for row in rows]


def shared_attachment_paths(rows: list[MessageRow]) -> tuple[Path, ...] | None:
    if not rows:
        return ()
    first = rows[0].attachments
    if all(row.attachments == first for row in rows):
        return first
    return None


def attachment_status_text(rows: list[MessageRow]) -> tuple[str, str]:
    shared = shared_attachment_paths(rows)
    if shared is not None:
        count = len(shared)
        return f"{count}개", f"파일 {count}개 · 모든 대화명 공통"
    total = sum(row.attachment_count for row in rows)
    return f"행별 {total}개", f"행별 첨부 섞임 · 파일 {total}개"
