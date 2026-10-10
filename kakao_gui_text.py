"""Word-based Korean wrapping for presentation labels; source values stay raw."""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata
import tkinter as tk
from tkinter import font as tkfont, ttk
from collections.abc import Callable


def _clusters(text: str) -> list[str]:
    def hangul_kind(code: int) -> str:
        if 0x1100 <= code <= 0x115F or 0xA960 <= code <= 0xA97C:
            return "L"
        if 0x1160 <= code <= 0x11A7 or 0xD7B0 <= code <= 0xD7C6:
            return "V"
        if 0x11A8 <= code <= 0x11FF or 0xD7CB <= code <= 0xD7FB:
            return "T"
        if 0xAC00 <= code <= 0xD7A3:
            return "LV" if (code - 0xAC00) % 28 == 0 else "LVT"
        return ""

    result: list[str] = []
    for char in text:
        code = ord(char)
        previous = hangul_kind(ord(result[-1][-1])) if result else ""
        current = hangul_kind(code)
        hangul_extend = (previous == "L" and current in {"L", "V", "LV", "LVT"}
                         or previous in {"LV", "V"} and current in {"V", "T"}
                         or previous in {"LVT", "T"} and current == "T")
        flag_pair = bool(result) and 0x1F1E6 <= code <= 0x1F1FF and (
            len(result[-1]) == 1 and 0x1F1E6 <= ord(result[-1]) <= 0x1F1FF
        )
        extend = bool(result) and (
            unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Mc", "Me"}
            or char == "\u200d" or result[-1].endswith("\u200d")
            or 0xFE00 <= code <= 0xFE0F or 0x1F3FB <= code <= 0x1F3FF
            or hangul_extend or flag_pair
        )
        if extend:
            result[-1] += char
        else:
            result.append(char)
    return result


def wrap_korean(text: str, width: float, measure: Callable[[str], float], *, mode: str = "pretty") -> str:
    """Preserve hard breaks; prefer spaces, with a grapheme-safe overflow break.

    Only presentation labels use the result. CSV/message/editor values do not.
    Balance is bounded to short headings; pretty avoids a final one-word line
    when moving a whole word fits without adding a line.
    """
    if width <= 0 or not text:
        return text

    def greedy(paragraph: str, limit: float) -> list[str]:
        lines: list[str] = []
        line = ""
        for token in re.findall(r"\s+|\S+", paragraph):
            if token.isspace():
                line += token
                continue
            if line and measure(line + token) > limit:
                lines.append(line.rstrip())
                line = ""
            if measure(token) <= limit:
                line += token
                continue
            # A file path/URL/unspaced term must not expand the application.
            for cluster in _clusters(token):
                if line and measure(line + cluster) > limit:
                    lines.append(line.rstrip())
                    line = ""
                line += cluster
        lines.append(line.rstrip())
        return lines

    output: list[str] = []
    for part in re.split(r"(\r\n|\r|\n)", text):
        if part in {"\r\n", "\r", "\n"}:
            output.append(part)
            continue
        lines = greedy(part, width)
        if mode == "balance" and 1 < len(lines) <= 3:
            # Keep the greedy line count; reduce excessive heading imbalance.
            low, high = max(1.0, width / len(lines)), width
            for _ in range(10):
                middle = (low + high) / 2
                if len(greedy(part, middle)) <= len(lines):
                    high = middle
                else:
                    low = middle
            lines = greedy(part, high)
        elif mode == "pretty" and len(lines) > 1 and len(lines[-1].split()) == 1:
            previous = lines[-2].rstrip()
            match = re.search(r"\s+(\S+)$", previous)
            if match and measure(match.group(1) + " " + lines[-1]) <= width:
                lines[-2] = previous[:match.start()].rstrip()
                lines[-1] = match.group(1) + " " + lines[-1]
        output.append("\n".join(lines))
    return "".join(output)


class KoreanLabel(ttk.Label):
    """ttk label with a separate display variable and bounded measurement cache."""
    def __init__(self, master=None, *, wrap_mode="pretty", **options):
        self._wrap_mode = wrap_mode
        self._requested_wrap = float(options.pop("wraplength", 0))
        self._source_variable = options.pop("textvariable", None)
        self._source_text = str(options.pop("text", ""))
        self._display_variable = tk.StringVar(master=master)
        self._source_trace = None
        self._font_key = None
        self._font_measure = None
        self._last_display = None
        super().__init__(master, textvariable=self._display_variable, wraplength=0, **options)
        if self._source_variable is not None:
            self._source_trace = self._source_variable.trace_add("write", self._render)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._render()

    @property
    def source_text(self) -> str:
        return self._source_variable.get() if self._source_variable is not None else self._source_text

    def configure(self, cnf=None, **options):
        if isinstance(cnf, dict):
            options = {**cnf, **options}
            cnf = None
        if not options:
            return super().configure(cnf)
        if "text" in options:
            self._source_text = str(options.pop("text"))
        if "wraplength" in options:
            self._requested_wrap = float(options.pop("wraplength"))
        result = super().configure(cnf, **options) if options or cnf is not None else None
        self._render()
        return result

    config = configure

    def cget(self, key):
        if key == "text":
            return self.source_text
        if key == "wraplength":
            return self._requested_wrap
        if key == "textvariable" and self._source_variable is not None:
            return str(self._source_variable)
        return super().cget(key)

    def _render(self, *_args):
        try:
            if not self.winfo_exists():
                return
            font_spec = super().cget("font") or ttk.Style(self).lookup(super().cget("style") or "TLabel", "font") or "TkDefaultFont"
            if str(font_spec) != self._font_key:
                font = tkfont.Font(self, font=font_spec)
                self._font_key = str(font_spec)
                self._font_measure = lru_cache(maxsize=512)(font.measure)
            text = wrap_korean(self.source_text, self._requested_wrap, self._font_measure, mode=self._wrap_mode)
            if text != self._last_display:
                self._last_display = text
                self._display_variable.set(text)
        except tk.TclError:
            # The source variable can outlive a dialog during destruction.
            return

    def _on_destroy(self, event):
        if event.widget is self and self._source_trace is not None:
            self._source_variable.trace_remove("write", self._source_trace)
            self._source_trace = None
