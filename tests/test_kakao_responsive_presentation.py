"""Batch routes preserve policy; fewer Tcl writes preserve every border pixel."""
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
import tkinter as tk
from unittest.mock import patch

import pytest

from kakao_delivery import delivery_route, delivery_routes
from kakao_gui_theme import _rounded_border_image
from kakao_sender import MessageRow


class CountingMapping(Mapping):
    def __init__(self, values):
        self.values = dict(values)
        self.iterations = 0
        self.item_reads = 0

    def __getitem__(self, name):
        return self.values[name]

    def __iter__(self):
        self.iterations += 1
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def items(self):
        self.item_reads += 1
        return self.values.items()


class CountingNames:
    def __init__(self, names):
        self.names = names
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return iter(self.names)


@pytest.mark.parametrize("common_schedule", [None, datetime(2026, 10, 8, 9)])
@pytest.mark.parametrize("direct_enabled", [False, True])
def test_batch_matches_single_routes_for_exact_normalized_ambiguous_and_scheduled_names(
    common_schedule, direct_enabled,
):
    values = {
        "exact": " 101 ", "NORMALIZED": "202", "Ambiguous": "303",
        " AMBIGUOUS ": "404", "same": "505", " SAME ": " 505 ",
        "Excluded": "606", "empty": "", "zero": "0",
    }
    mappings = MappingProxyType(values)
    names = ("exact", " normalized ", "Ambiguous", " ambiguous ", " SAME ",
             "same", "EXCLUDED", "missing", "empty", "zero")
    rows = [MessageRow(index + 2, name, "fixture") for index, name in enumerate(names)]
    rows.append(MessageRow(20, "missing", "fixture", scheduled_at=datetime(2026, 10, 9, 9)))
    exclusions = (" excluded ", "unrelated")
    expected = [delivery_route(row, common_schedule, direct_enabled, mappings, exclusions) for row in rows]
    assert delivery_routes(iter(rows), common_schedule, direct_enabled, mappings, iter(exclusions)) == expected
    assert values == dict(mappings)
    if common_schedule is None and direct_enabled:
        assert expected == ["직접", "직접", "직접", "매칭 필요", "직접", "직접",
                            "매칭 제외", "매칭 필요", "매칭 필요", "직접", "예약 등록"]


def test_batch_indexes_normalized_fallback_once_and_consumes_exclusions_once():
    mappings = CountingMapping({f" Fixture {index} ": str(index + 1000) for index in range(150)})
    excluded = CountingNames(["fixture 17", "unrelated"])
    rows = [MessageRow(index + 2, f"fixture {index}", "fixture") for index in range(150)]
    original = dict(mappings.values)
    routes = delivery_routes(rows, None, True, mappings, excluded)
    assert routes.count("직접") == 149
    assert routes[17] == "매칭 제외"
    assert mappings.item_reads == 1
    assert mappings.iterations == 0
    assert excluded.iterations == 1
    assert mappings.values == original


def test_exact_batch_and_single_route_do_not_copy_or_scan_mapping():
    mappings = CountingMapping({f"fixture {index}": str(index + 1000) for index in range(100)})
    rows = [MessageRow(index + 2, f"fixture {index}", "fixture") for index in range(100)]
    assert delivery_routes(rows, None, True, mappings) == ["직접"] * 100
    assert delivery_route(rows[0], None, True, mappings) == "직접"
    assert mappings.item_reads == 0
    assert mappings.iterations == 0


@pytest.mark.parametrize("common_schedule,direct_enabled,expected", [
    (datetime(2026, 10, 8, 9), True, "예약 등록"),
    (None, False, "화면"),
])
def test_non_direct_batch_does_not_read_unneeded_mapping_or_exclusions(
    common_schedule, direct_enabled, expected,
):
    class Unused:
        def __iter__(self):
            raise AssertionError("Unneeded input consumed")

        def get(self, _key):
            raise AssertionError("Unneeded mapping read")

    rows = [MessageRow(2, "fixture", "fixture")]
    assert delivery_routes(rows, common_schedule, direct_enabled, Unused(), Unused()) == [expected]
    assert delivery_routes([], None, True, Unused(), Unused()) == []


# These masks are saved from the original per-pixel renderer. Dots are pixels
# that it leaves transparent; B/F represent the border and fill colors.
BORDER_MASKS = {
    1: (
        "....BBBBBBBB....", "..BBFFFFFFFFBB..", ".BFFFFFFFFFFFFB.", ".BFFFFFFFFFFFFB.",
        "BFFFFFFFFFFFFFFB", "BFFFFFFFFFFFFFFB", "BFFFFFFFFFFFFFFB", "BFFFFFFFFFFFFFFB",
        "BFFFFFFFFFFFFFFB", "BFFFFFFFFFFFFFFB", "BFFFFFFFFFFFFFFB", "BFFFFFFFFFFFFFFB",
        ".BFFFFFFFFFFFFB.", ".BFFFFFFFFFFFFB.", "..BBFFFFFFFFBB..", "....BBBBBBBB....",
    ),
    2: (
        "....BBBBBBBB....", "..BBBBBBBBBBBB..", ".BBBFFFFFFFFBBB.", ".BBFFFFFFFFFFBB.",
        "BBFFFFFFFFFFFFBB", "BBFFFFFFFFFFFFBB", "BBFFFFFFFFFFFFBB", "BBFFFFFFFFFFFFBB",
        "BBFFFFFFFFFFFFBB", "BBFFFFFFFFFFFFBB", "BBFFFFFFFFFFFFBB", "BBFFFFFFFFFFFFBB",
        ".BBFFFFFFFFFFBB.", ".BBBFFFFFFFFBBB.", "..BBBBBBBBBBBB..", "....BBBBBBBB....",
    ),
}


class PixelImage:
    def __init__(self, *, width, height, **_kwargs):
        self.pixels = [["." for _ in range(width)] for _ in range(height)]
        self.calls = []

    def put(self, color, to):
        self.calls.append((color, to))
        x0, y0, x1, y1 = to
        for y in range(y0, y1):
            for x in range(x0, x1):
                self.pixels[y][x] = color


@pytest.mark.parametrize("thickness", [1, 2])
def test_row_runs_keep_saved_border_and_focus_pixels_with_fewer_tcl_writes(thickness):
    with patch("kakao_gui_theme.tk.PhotoImage", PixelImage):
        image = _rounded_border_image(None, fill="F", border="B", thickness=thickness)
    assert tuple("".join(row) for row in image.pixels) == BORDER_MASKS[thickness]
    assert len(image.calls) < 64  # The original image used 224 Tcl writes.
    assert all(y1 == y0 + 1 for _color, (_x0, y0, _x1, y1) in image.calls)


@pytest.fixture(scope="module")
def root():
    try:
        value = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    value.withdraw()
    yield value
    value.destroy()


@pytest.mark.parametrize("thickness", [1, 2])
def test_real_tk_rectangles_keep_transparency_and_rgb_values(root, thickness):
    fill, border = "#1155aa", "#223344"
    expected_colors = {"F": (17, 85, 170), "B": (34, 51, 68)}
    image = _rounded_border_image(root, fill=fill, border=border, thickness=thickness)
    assert (image.width(), image.height()) == (16, 16)
    for y, row in enumerate(BORDER_MASKS[thickness]):
        for x, color in enumerate(row):
            assert image.transparency_get(x, y) == (color == ".")
            if color != ".":
                assert image.get(x, y) == expected_colors[color]
