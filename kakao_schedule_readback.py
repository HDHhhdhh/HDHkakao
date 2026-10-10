"""Strict field comparison for a saved KakaoTalk reservation-form image.

This module neither registers a reservation nor treats a form as a receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import math
from statistics import median
import unicodedata

from kakao_windows_ocr import OcrRect, OcrResult, OcrWord


class ScheduleReadbackError(ValueError):
    pass


@dataclass(frozen=True)
class ScheduleFormReadback:
    chat_name: str
    scheduled_at: datetime


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def _region_text(result: OcrResult, region: tuple[float, float, float, float]) -> str:
    left, top, right, bottom = region
    words = [
        word for line in result.lines for word in line.words
        if left * result.width <= word.rect.center[0] <= right * result.width
        and top * result.height <= word.rect.center[1] <= bottom * result.height
    ]
    # A time field can be returned as three OCR lines ("오후", "1", ": 05").
    # These regions each contain exactly one UI row; use its horizontal order.
    return " ".join(word.text for word in sorted(words, key=lambda w: w.rect.x))


def read_schedule_form(result: OcrResult) -> ScheduleFormReadback:
    if not 0.57 <= result.width / result.height <= 0.67:
        raise ScheduleReadbackError("예약 입력창만 잘라낸 이미지가 필요합니다.")
    recipient = _region_text(result, (0.17, 0.65, 0.96, 0.715)).strip()
    date_text = _compact(_region_text(result, (0.18, 0.72, 0.645, 0.79)))
    time_text = _compact(_region_text(result, (0.66, 0.72, 0.96, 0.79)))
    date_match = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\.?\([월화수목금토일]\)", date_text)
    # The native colon is two tiny dots. Windows OCR sometimes emits '•.'
    # for that fixed separator. Keep period/hour/minute mandatory and exact;
    # only accept observed punctuation variants between the numeric fields.
    time_match = re.fullmatch(r"(오전|오후)([0-9]{1,2})[:·•.]+([0-9]{2})", time_text)
    if not recipient or date_match is None or time_match is None:
        raise ScheduleReadbackError("예약창의 대화명·날짜·오전/오후·시각을 모두 읽지 못했습니다.")
    hour, minute = int(time_match[2]), int(time_match[3])
    if not 1 <= hour <= 12 or minute not in range(0, 60, 5):
        raise ScheduleReadbackError("예약창에서 읽은 시각이 올바르지 않습니다.")
    hour = hour % 12 + (12 if time_match[1] == "오후" else 0)
    try:
        value = datetime(*(int(part) for part in date_match.groups()), hour, minute)
    except ValueError as exc:
        raise ScheduleReadbackError("예약창에서 읽은 날짜가 올바르지 않습니다.") from exc
    return ScheduleFormReadback(recipient, value)


def verify_schedule_form(
    result: OcrResult, *, chat_name: str, scheduled_at: datetime,
    expected_body: str, actual_body: str,
) -> ScheduleFormReadback:
    """Verify OCR fields plus exact native text readback; never infer success."""
    observed = read_schedule_form(result)
    if _compact(observed.chat_name) != _compact(chat_name):
        raise ScheduleReadbackError("예약창의 대화명이 CSV 대상과 다릅니다.")
    if observed.scheduled_at != scheduled_at:
        raise ScheduleReadbackError("예약창의 날짜 또는 시각이 CSV 예약일시와 다릅니다.")
    def native_text(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    if not expected_body.strip() or native_text(actual_body) != native_text(expected_body):
        raise ScheduleReadbackError("예약창의 본문이 CSV 본문과 다릅니다.")
    return observed


PixelRegion = tuple[float, float, float, float]


@dataclass(frozen=True)
class _VisualRow:
    text: str
    rect: OcrRect


def _visual_rows(result: OcrResult, region: PixelRegion | None = None) -> tuple[_VisualRow, ...]:
    """Group same-baseline OCR words; never combine separate vertical rows.

    WinRT sometimes returns each time-field token as a separate OcrLine.
    Regions, when supplied, are original-image pixel bounds (L, T, R, B).
    """
    if result.width <= 0 or result.height <= 0:
        raise ScheduleReadbackError("OCR 이미지 크기가 올바르지 않습니다.")
    if region is None:
        region = (0, 0, result.width, result.height)
    if len(region) != 4 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in region):
        raise ScheduleReadbackError("OCR 판독 영역이 올바르지 않습니다.")
    left, top, right, bottom = region
    if not 0 <= left < right <= result.width or not 0 <= top < bottom <= result.height:
        raise ScheduleReadbackError("OCR 판독 영역이 이미지 범위를 벗어났습니다.")
    words: list[OcrWord] = []
    for line in result.lines:
        for word in line.words:
            rect = word.rect
            if rect.right <= left or rect.x >= right or rect.bottom <= top or rect.y >= bottom:
                continue
            # A clipped word could hide a prefix/suffix and create a false exact match.
            if rect.x < left or rect.y < top or rect.right > right or rect.bottom > bottom:
                raise ScheduleReadbackError("OCR 판독 영역 가장자리에서 문자가 잘렸습니다.")
            if rect.width <= 0 or rect.height <= 0 or not word.text.strip():
                raise ScheduleReadbackError("OCR 단어의 문자 또는 좌표가 올바르지 않습니다.")
            words.append(word)
    groups: list[list[OcrWord]] = []
    for word in sorted(words, key=lambda value: (value.rect.center[1], value.rect.x)):
        matching = [group for group in groups if abs(word.rect.center[1] - median(item.rect.center[1] for item in group))
                    <= max(2.0, 0.5 * min(word.rect.height, median(item.rect.height for item in group)))]
        if len(matching) > 1:
            raise ScheduleReadbackError("OCR 문자 행을 유일하게 구분하지 못했습니다.")
        if matching:
            matching[0].append(word)
        else:
            groups.append([word])
    rows = []
    for group in groups:
        ordered = sorted(group, key=lambda word: word.rect.x)
        # Duplicate/overlapping detections must not manufacture a selectable label.
        if any(first.rect.right > second.rect.x + 1 for first, second in zip(ordered, ordered[1:])):
            raise ScheduleReadbackError("OCR 문자 영역이 겹쳐 정확한 값을 구분하지 못했습니다.")
        x = min(word.rect.x for word in group)
        y = min(word.rect.y for word in group)
        right = max(word.rect.right for word in group)
        bottom = max(word.rect.bottom for word in group)
        text = " ".join(unicodedata.normalize("NFC", word.text).strip() for word in ordered)
        rows.append(_VisualRow(text, OcrRect(x, y, right - x, bottom - y)))
    return tuple(rows)


def read_calendar_month(result: OcrResult, *, region: PixelRegion | None = None) -> tuple[int, int]:
    """Read exactly one Korean calendar heading, e.g. '2026년 9월'."""
    candidates = []
    for row in _visual_rows(result, region):
        match = re.fullmatch(r"([0-9]{4})\s*년\s*([0-9]{1,2})\s*월", row.text.strip())
        if match:
            year, month = map(int, match.groups())
            if not 1 <= year <= 9999 or not 1 <= month <= 12:
                raise ScheduleReadbackError("달력에서 읽은 연도 또는 월이 올바르지 않습니다.")
            candidates.append((year, month))
    if len(candidates) != 1:
        raise ScheduleReadbackError("달력의 연도·월 헤더를 유일하게 읽지 못했습니다.")
    return candidates[0]


def find_dropdown_option_center(
    result: OcrResult, target: str | int, *, region: PixelRegion | None = None,
) -> tuple[float, float]:
    """Return the unique exact label's center in ORIGINAL image coordinates.

    Only 오전/오후 and ASCII digit-only numeric labels are supported. Numeric
    leading zeros are equivalent, but punctuation, units and OCR lookalikes are not.
    """
    if isinstance(target, bool) or not isinstance(target, (str, int)):
        raise ScheduleReadbackError("드롭다운 목표 값이 올바르지 않습니다.")
    value = str(target)
    numeric = re.fullmatch(r"[0-9]+", value) is not None
    if value not in ("오전", "오후") and not numeric:
        raise ScheduleReadbackError("드롭다운 목표는 오전·오후 또는 숫자여야 합니다.")
    matches = []
    for row in _visual_rows(result, region):
        text = row.text.strip()
        if (numeric and re.fullmatch(r"[0-9]+", text) and int(text) == int(value)) or (not numeric and text == value):
            matches.append(row.rect.center)
    if len(matches) != 1:
        raise ScheduleReadbackError("드롭다운 목표 항목을 유일하게 읽지 못했습니다.")
    return matches[0]


def has_schedule_success_toast(result: OcrResult, *, region: PixelRegion | None = None) -> bool:
    """True only for one exact '메시지를 예약했습니다.' visual row.

    Whitespace/token boundaries may differ; punctuation and wording must not.
    Never infer success from '예약', disappearance of a dialog, or a similar toast.
    The caller must supply a toast-only crop/region and establish its appearance
    after submission; matching a chat message with this text is not a receipt.
    """
    try:
        rows = _visual_rows(result, region)
    except ScheduleReadbackError:
        return False
    return sum(_compact(row.text) == "메시지를예약했습니다." for row in rows) == 1
