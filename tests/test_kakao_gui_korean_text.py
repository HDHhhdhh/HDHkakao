"""Korean typography: word boundaries, overflow, raw values and real Tk sizing."""
import re
import tkinter as tk
from tkinter import font as tkfont
import pytest

from kakao_gui_text import KoreanLabel, _clusters, wrap_korean
from tests.test_kakao_gui_layout import isolated_gui_fixture


def test_keep_korean_words_whole_and_overflow_only_an_oversized_word():
    raw = "우리 가족 맞춤형 미국여행 코스"
    wrapped = wrap_korean(raw, 11, len)
    assert wrapped == "우리 가족 맞춤형\n미국여행 코스"
    assert wrapped.replace("\n", " ") == raw
    oversized = "개인정보처리방침동의안내문" * 3
    broken = wrap_korean(oversized, 8, len)
    assert all(len(line) <= 8 for line in broken.splitlines())
    assert broken.replace("\n", "") == oversized


def test_heading_balance_and_paragraph_pretty_have_distinct_roles():
    heading = "여행을 떠나는 가장 쉬운 방법"
    ordinary = wrap_korean(heading, 12, len, mode="wrap").splitlines()
    balanced = wrap_korean(heading, 12, len, mode="balance").splitlines()
    assert len(ordinary) == len(balanced)
    assert max(map(len, balanced)) - min(map(len, balanced)) < max(map(len, ordinary)) - min(map(len, ordinary))
    paragraph = "하나 둘셋 넷다섯 여섯"
    assert wrap_korean(paragraph, 10, len, mode="wrap") == "하나 둘셋 넷다섯\n여섯"
    assert wrap_korean(paragraph, 10, len) == "하나 둘셋\n넷다섯 여섯"


def test_explicit_line_breaks_and_blank_paragraphs_are_not_joined():
    text = "첫 문단\r\n\r\n둘째 문단\n셋째 문단\r마지막"
    assert wrap_korean(text, 100, len) == text
    assert wrap_korean(text, 0, len) == text


def test_overflow_keeps_combining_marks_and_emoji_join_sequences_together():
    assert _clusters("가\u0301👩🏽‍🏫") == ["가\u0301", "👩🏽‍🏫"]
    assert _clusters("🇰🇷🇺🇸") == ["🇰🇷", "🇺🇸"]
    assert _clusters("\u1100\u1161\u11a8\u1100\u1161") == ["\u1100\u1161\u11a8", "\u1100\u1161"]
    emoji = "👩🏽‍🏫"
    assert wrap_korean(emoji * 3, len(emoji), len) == "\n".join([emoji] * 3)


@pytest.fixture(scope="module")
def gui_fixture():
    # Match the production single-interpreter lifetime; repeated Tk DLL loading
    # is unreliable on some Windows test desktops.
    with isolated_gui_fixture(populate=True) as fixture:
        yield fixture


def test_real_font_word_wrap_and_raw_variable_trace_cleanup(gui_fixture):
    app = gui_fixture.app
    raw = tk.StringVar(app, value="우리 가족 맞춤형 미국여행 코스")
    font = tkfont.Font(app, font=("Malgun Gothic", 12))
    width = font.measure("우리 가족 맞춤형") + 1
    label = KoreanLabel(app, textvariable=raw, font=font, wraplength=width)
    app.update_idletasks()
    assert label.source_text == raw.get()
    assert label._display_variable.get() == "우리 가족 맞춤형\n미국여행 코스"
    assert label.cget("wraplength") == width
    assert all(font.measure(line) <= width for line in label._display_variable.get().splitlines())
    trace_count = len(raw.trace_info())
    raw.set("예약 등록 전에 대상과 시간을 확인하세요.")
    assert raw.get() == label.source_text
    assert "확인하세요." in label._display_variable.get()
    label.destroy()
    assert len(raw.trace_info()) == trace_count - 1
    raw.set("창을 닫은 뒤에도 원본 변수는 안전합니다.")


def test_resize_does_not_change_original_rows_buttons_or_status(gui_fixture):
    app = gui_fixture.app
    original_rows = list(app.rows)
    original_buttons = {name: getattr(app, name).cget("text") for name in ("start_button", "open_file_button", "stop_button")}
    message = "예약 등록 전에는 대화명과 발송 시간을 확인해 주세요."
    app.status_var.set(message)
    for width, height in [(1024, 720), (1440, 900)]:
        app.geometry(f"{width}x{height}+0+0")
        app.update()
        app.rebalance_layout()
        app.update()
        assert app.rows == original_rows
        assert app.status_var.get() == message
        assert app.status_label.source_text == message
        assert all(getattr(app, name).cget("text") == text for name, text in original_buttons.items())
        assert gui_fixture.blocked == []
