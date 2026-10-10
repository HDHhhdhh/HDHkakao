"""OCR saved numeric menus by rearranging existing glyph pixels horizontally.

No capture/input APIs or expected option values are used. The source remains
unchanged; the derived strip and its coordinate map are retained for inspection.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from statistics import median

from PIL import Image

from kakao_schedule_readback import ScheduleReadbackError, find_dropdown_option_center
from kakao_windows_ocr import OcrLine, OcrRect, OcrResult, OcrWord, recognize_image


class NumericDropdownOcrError(ValueError):
    pass


@dataclass(frozen=True)
class _Cell:
    source: tuple[int, int, int, int]
    destination: tuple[int, int]


def _build_strip(image: Image.Image) -> tuple[Image.Image, tuple[_Cell, ...]]:
    image = image.convert("RGB")
    width, height = image.size
    if not 24 <= width <= 240 or not 35 <= height <= 2400:
        raise NumericDropdownOcrError("숫자 드롭다운만 잘라낸 이미지가 필요합니다.")
    # Exclude the panel outline and right-hand scrollbar. Labels must lie wholly
    # inside this central column; text touching its edges is rejected below.
    side = max(4, min(18, round(width * 0.12)))
    edge = 4
    gray = image.convert("L")
    pixels = gray.load()
    bands: list[list[tuple[int, int, int]]] = []
    for y in range(edge, height - edge):
        xs = [x for x in range(side, width - side) if pixels[x, y] < 100]
        if not xs:
            continue
        item = (y, min(xs), max(xs))
        if bands and y <= bands[-1][-1][0] + 2:
            bands[-1].append(item)
        else:
            bands.append([item])
    boxes = [(min(row[1] for row in band), band[0][0], max(row[2] for row in band) + 1, band[-1][0] + 1)
             for band in bands]
    if not 2 <= len(boxes) <= 60:
        raise NumericDropdownOcrError("숫자 메뉴의 행을 충분히 구분하지 못했습니다.")
    heights = [box[3] - box[1] for box in boxes]
    glyph_height = median(heights)
    if not 6 <= glyph_height <= 80:
        raise NumericDropdownOcrError("메뉴에 잘렸거나 불규칙한 문자 행이 있습니다.")
    irregular = [index for index, h in enumerate(heights) if not 0.8 * glyph_height <= h <= 1.25 * glyph_height]
    if irregular:
        # Scrolling can leave half a glyph at the viewport boundary. Keep only
        # complete rows; never recognize or infer the cropped option. Require
        # at least three full, evenly spaced rows to identify such a boundary.
        complete = [box for index, box in enumerate(boxes) if index not in irregular]
        if len(complete) < 3 or any(index not in (0, len(boxes)-1) for index in irregular):
            raise NumericDropdownOcrError("메뉴에 잘렸거나 불규칙한 문자 행이 있습니다.")
        full_centers = [(box[1]+box[3])/2 for box in complete]
        full_gaps = [b-a for a,b in zip(full_centers,full_centers[1:])]
        full_pitch = median(full_gaps)
        if full_pitch < glyph_height*1.4 or any(abs(gap-full_pitch)>max(3,full_pitch*.10) for gap in full_gaps):
            raise NumericDropdownOcrError("메뉴 숫자 행의 간격이 일정하지 않습니다.")
        for index in irregular:
            box = boxes[index]
            if heights[index] >= .8*glyph_height:
                raise NumericDropdownOcrError("메뉴에 불규칙한 문자 행이 있습니다.")
            if index == 0:
                near_edge = box[1] < full_pitch
                expected_edge = complete[0][3]-full_pitch
                observed_edge = box[3]
            else:
                near_edge = box[3] > height-full_pitch
                expected_edge = complete[-1][1]+full_pitch
                observed_edge = box[1]
            if not near_edge or abs(observed_edge-expected_edge)>max(3,full_pitch*.10):
                raise NumericDropdownOcrError("잘린 숫자가 메뉴 경계의 행과 일치하지 않습니다.")
        boxes = complete
        heights = [box[3]-box[1] for box in boxes]
    if any(box[0] <= side or box[2] >= width - side or box[1] <= edge or box[3] >= height - edge for box in boxes):
        raise NumericDropdownOcrError("메뉴의 가장자리에서 숫자가 잘렸습니다.")
    centers = [(box[1] + box[3]) / 2 for box in boxes]
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    pitch = median(gaps)
    if pitch < glyph_height * 1.4 or any(abs(gap - pitch) > max(3, pitch * 0.10) for gap in gaps):
        raise NumericDropdownOcrError("메뉴 숫자 행의 간격이 일정하지 않습니다.")
    x_pad = max(2, round(glyph_height * 0.18))
    left = min(box[0] for box in boxes) - x_pad
    right = max(box[2] for box in boxes) + x_pad
    if left < side or right > width - side:
        raise NumericDropdownOcrError("숫자 양옆의 여백을 확보하지 못했습니다.")
    y_pad = max(4, round(glyph_height * 0.5))
    if glyph_height + 2 * y_pad >= pitch:
        y_pad = max(2, int((pitch - glyph_height) / 2) - 1)
    cell_width = right - left + max(8, round(glyph_height * 0.7))
    margin = 20
    strip = Image.new("RGB", (cell_width * len(boxes) + margin * 2, int(max(heights) + y_pad * 2 + margin * 2)), "white")
    cells = []
    for index, box in enumerate(boxes):
        source = (left, box[1] - y_pad, right, box[3] + y_pad)
        if source[1] < 0 or source[3] > height:
            raise NumericDropdownOcrError("숫자의 위아래 여백이 이미지 밖으로 잘렸습니다.")
        destination = (margin + cell_width * index, margin)
        strip.paste(image.crop(source), destination)
        cells.append(_Cell(source, destination))
    return strip, tuple(cells)


def find_complete_numeric_option_center(path: str | Path, result: OcrResult, target: str | int) -> tuple[float, float]:
    """Accept a raw OCR option only inside a complete, observed glyph row."""
    point = find_dropdown_option_center(result, target)
    with Image.open(path) as image:
        if image.size != (result.width, result.height):
            raise NumericDropdownOcrError('메뉴 이미지와 OCR 좌표계가 다릅니다.')
        _, cells = _build_strip(image)
    matching = [cell for cell in cells
                if cell.source[0] <= point[0] < cell.source[2]
                and cell.source[1] <= point[1] < cell.source[3]]
    if len(matching) != 1:
        raise ScheduleReadbackError('목표 숫자가 온전히 보이는 메뉴 행에 속하지 않습니다.')
    return point


def _restore_coordinates(result: OcrResult, cells: tuple[_Cell, ...], original_size: tuple[int, int]) -> OcrResult:
    found: dict[int, OcrWord] = {}
    values: set[int] = set()
    for line in result.lines:
        for word in line.words:
            if re.fullmatch(r"[0-9]+", word.text) is None:
                raise NumericDropdownOcrError("숫자 메뉴에서 숫자가 아닌 문자를 인식했습니다.")
            if word.rect.width <= 0 or word.rect.height <= 0:
                raise NumericDropdownOcrError("인식된 숫자의 영역이 비어 있습니다.")
            matches = []
            for index, cell in enumerate(cells):
                x, y = cell.destination
                width = cell.source[2] - cell.source[0]
                height = cell.source[3] - cell.source[1]
                if x <= word.rect.x and y <= word.rect.y and word.rect.right <= x + width and word.rect.bottom <= y + height:
                    matches.append(index)
            if len(matches) != 1:
                raise NumericDropdownOcrError("OCR 숫자가 여러 항목에 걸치거나 행 밖에 있습니다.")
            index = matches[0]
            number = int(word.text)
            if index in found or number in values:
                raise NumericDropdownOcrError("메뉴에 중복되거나 분리된 숫자 후보가 있습니다.")
            cell = cells[index]
            rect = OcrRect(word.rect.x - cell.destination[0] + cell.source[0],
                           word.rect.y - cell.destination[1] + cell.source[1], word.rect.width, word.rect.height)
            found[index] = OcrWord(word.text, rect)
            values.add(number)
    if len(found) != len(cells):
        raise NumericDropdownOcrError("메뉴의 일부 숫자를 읽지 못했습니다.")
    lines = tuple(OcrLine(found[index].text, (found[index],), found[index].rect) for index in range(len(cells)))
    return OcrResult(*original_size, result.language, lines)


def recognize_numeric_dropdown_image(path: str | Path, *, artifact_dir: str | Path | None = None) -> OcrResult:
    """Read all complete numeric rows, returning only original-image coordinates.

    Saves ``<stem>-numeric-strip.png`` and ``<stem>-numeric-map.json`` alongside
    the unchanged original unless artifact_dir is supplied. Recognition must
    yield one ASCII numeric word per observed glyph row, with no duplicates.
    """
    source = Path(path).resolve(strict=True)
    with Image.open(source) as image:
        original_size = image.size
        strip, cells = _build_strip(image)
    folder = Path(artifact_dir) if artifact_dir is not None else source.parent
    folder.mkdir(parents=True, exist_ok=True)
    strip_path = folder / f"{source.stem}-numeric-strip.png"
    strip.save(strip_path)
    mapping = {"source": str(source), "original_size": original_size,
               "cells": [{"source": cell.source, "destination": cell.destination} for cell in cells]}
    (folder / f"{source.stem}-numeric-map.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    errors = []
    for language in ("en-US", "ko"):
        recognized = recognize_image(strip_path, language=language, scale=3)
        try:
            return _restore_coordinates(recognized, cells, original_size)
        except NumericDropdownOcrError as exc:
            # English can read a pair of zeros as letters 'oo'; Korean often
            # resolves it. Never convert those letters into guessed digits.
            errors.append(f"{language}: {exc}")
    raise NumericDropdownOcrError(" / ".join(errors))
