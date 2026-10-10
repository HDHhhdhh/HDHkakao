"""Read an existing image with the Windows OCR engine (no screen/input access)."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Any


_active_session = ContextVar("kakao_saved_image_ocr_session", default=None)


@contextmanager
def use_ocr_session(session):
    """Reuse OCR only in the calling context; unrelated jobs keep one-shot OCR."""
    token = _active_session.set(session)
    try:
        yield session
    finally:
        _active_session.reset(token)


class WindowsOcrError(RuntimeError):
    """The image could not be read reliably by the Windows OCR engine."""


@dataclass(frozen=True)
class OcrRect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(frozen=True)
class OcrWord:
    text: str
    rect: OcrRect


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: tuple[OcrWord, ...]
    rect: OcrRect


@dataclass(frozen=True)
class OcrResult:
    width: int
    height: int
    language: str
    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise WindowsOcrError("Windows OCR returned a non-text field.")
    # Retain Korean and other Unicode; discard terminal/control characters.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).replace("\r\n", "\n").strip()


def _parse_rect(value: Any, width: int, height: int) -> OcrRect:
    if not isinstance(value, dict):
        raise WindowsOcrError("Windows OCR returned an invalid rectangle.")
    coords = []
    for key in ("x", "y", "width", "height"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise WindowsOcrError("Windows OCR returned a non-finite rectangle.")
        coords.append(float(item))
    rect = OcrRect(*coords)
    if min(coords) < 0 or rect.right > width + 1 or rect.bottom > height + 1:
        raise WindowsOcrError("Windows OCR rectangle lies outside the original image.")
    return rect


def _parse_result(output: bytes | str) -> OcrResult:
    try:
        raw = output.decode("utf-8-sig") if isinstance(output, bytes) else output.lstrip("\ufeff")
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise WindowsOcrError("Windows OCR did not return valid UTF-8 JSON.") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise WindowsOcrError("Unsupported Windows OCR response.")
    dimensions = (value.get("width"), value.get("height"))
    if any(type(n) is not int or n <= 0 for n in dimensions):
        raise WindowsOcrError("Windows OCR returned invalid image dimensions.")
    width, height = dimensions
    language = _clean_text(value.get("language"))
    if not language:
        raise WindowsOcrError("Windows OCR returned no recognition language.")
    raw_lines = value.get("lines")
    if not isinstance(raw_lines, list):
        raise WindowsOcrError("Windows OCR returned an invalid line collection.")
    lines = []
    for line in raw_lines:
        if not isinstance(line, dict) or not isinstance(line.get("words"), list):
            raise WindowsOcrError("Windows OCR returned an invalid line.")
        words = []
        for word in line["words"]:
            if not isinstance(word, dict):
                raise WindowsOcrError("Windows OCR returned an invalid word.")
            words.append(OcrWord(_clean_text(word.get("text")), _parse_rect(word.get("rect"), width, height)))
        lines.append(OcrLine(_clean_text(line.get("text")), tuple(words), _parse_rect(line.get("rect"), width, height)))
    return OcrResult(width, height, language, tuple(lines))


def _validate_request(path, language, timeout, scale):
    image_path = Path(path).expanduser().resolve(strict=True)
    if not image_path.is_file():
        raise ValueError("OCR input must be an existing image file.")
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language):
        raise ValueError("OCR language must be a language tag such as ko or en-US.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("OCR timeout must be a positive finite number.")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or not 1 <= scale <= 3:
        raise ValueError("OCR scale must be between 1 and 3.")
    if os.name != "nt":
        raise WindowsOcrError("Windows OCR requires Windows.")
    script = Path(__file__).resolve().parent / "scripts" / "kakao_windows_ocr.ps1"
    if not script.is_file():
        raise WindowsOcrError("Windows OCR helper script is missing.")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return image_path, powershell, script


def recognize_image(
    path: str | os.PathLike[str], language: str = "ko", *, timeout: float = 45, scale: float = 2.0
) -> OcrResult:
    """Read saved pixels. All rectangles use the original image coordinates.

    Native batches bind a private worker; other callers retain one-shot OCR.
    Neither path captures the screen or operates a native control.
    """
    image_path, powershell, script = _validate_request(path, language, timeout, scale)
    session = _active_session.get()
    if session is not None:
        return session.recognize_image(image_path, language, timeout=timeout, scale=scale)
    command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-ImagePath", str(image_path), "-Language", language, "-Scale", str(float(scale))]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise WindowsOcrError(f"Windows OCR timed out after {timeout:g} seconds.") from exc
    except OSError as exc:
        raise WindowsOcrError(f"Could not start Windows OCR: {exc}") from exc
    if completed.returncode:
        detail = _clean_text(completed.stderr.decode("utf-8", errors="replace"))[:1200]
        raise WindowsOcrError(f"Windows OCR failed (exit {completed.returncode}): {detail or 'no error detail'}")
    return _parse_result(completed.stdout)
