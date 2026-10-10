"""Offline theme checks using real Tk state resolution and widget geometry.

Only the presentation helper is imported. No application, credentials,
configuration, sending service, network connection, or worker is created.
"""
from __future__ import annotations

import gc
import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest

from kakao_gui_theme import configure_hub_style


def contrast_ratio(root, foreground, background):
    def luminance(color):
        components = [value / 65535 for value in root.winfo_rgb(color)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in components]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    values = sorted((luminance(foreground), luminance(background)))
    return (values[1] + 0.05) / (values[0] + 0.05)


class GuiThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
        cls.addClassCleanup(cls.root.destroy)
        cls.root.withdraw()
        cls.original_scaling = float(cls.root.tk.call("tk", "scaling"))
        cls.colors, cls.line_height = configure_hub_style(cls.root)
        cls.style = ttk.Style(cls.root)

    def test_helper_preserves_display_scaling_and_returns_real_font_height(self):
        self.assertAlmostEqual(float(self.root.tk.call("tk", "scaling")), self.original_scaling)
        font = tkfont.Font(self.root, font=self.style.lookup("TLabel", "font"))
        self.assertEqual(self.line_height, font.metrics("linespace"))
        self.assertGreater(self.line_height, 0)
        for key in ("bg", "header", "nav", "surface", "meta", "line", "text", "muted",
                    "accent", "accent_dark", "blue", "danger", "warning", "success", "kakao"):
            self.assertIn(key, self.colors)
            self.root.winfo_rgb(self.colors[key])
        self.assertEqual(self.style.theme_use(), "clam")

    def test_button_text_is_readable_in_every_interaction_state(self):
        states = ((), ("active",), ("pressed",), ("focus",), ("active", "focus"),
                  ("pressed", "focus"), ("disabled",), ("disabled", "active", "focus"))
        for name in ("TButton", "Compact.TButton", "Secondary.TButton", "Blue.TButton",
                     "Accent.TButton", "Busy.Accent.TButton", "Danger.TButton", "Toolbutton"):
            self.assertTrue(self.style.layout(name), name)
            for state in states:
                with self.subTest(style=name, state=state):
                    foreground = self.style.lookup(name, "foreground", state)
                    background = self.style.lookup(name, "background", state)
                    self.assertGreaterEqual(contrast_ratio(self.root, foreground, background), 4.5)

    def test_busy_disabled_style_remains_distinct_and_readable(self):
        generic_background = self.style.lookup("Accent.TButton", "background", ("disabled",))
        busy_background = self.style.lookup("Busy.Accent.TButton", "background", ("disabled",))
        busy_foreground = self.style.lookup("Busy.Accent.TButton", "foreground", ("disabled",))
        self.assertNotEqual(self.root.winfo_rgb(generic_background), self.root.winfo_rgb(busy_background))
        self.assertGreaterEqual(contrast_ratio(self.root, busy_foreground, busy_background), 4.5)
        self.assertEqual(busy_foreground, self.style.lookup("Busy.Accent.TButton", "foreground"))
        self.assertEqual(busy_background, self.style.lookup("Busy.Accent.TButton", "background"))

    def test_secondary_and_semantic_text_is_readable(self):
        for name in ("SurfaceMuted.TLabel", "SoftMuted.TLabel", "CommandMuted.TLabel",
                     "HeaderMuted.TLabel", "MetaKey.TLabel", "ScreenIdle.TLabel",
                     "ScreenActive.TLabel", "ScreenStop.TLabel", "DropEmpty.TLabel"):
            with self.subTest(style=name):
                self.assertGreaterEqual(contrast_ratio(
                    self.root, self.style.lookup(name, "foreground"), self.style.lookup(name, "background")), 4.5)
                font = tkfont.Font(self.root, font=self.style.lookup(name, "font"))
                self.assertGreaterEqual(font.actual("size"), 9)
        for name in ("Treeview", "Toolbutton"):
            with self.subTest(style=name):
                self.assertGreaterEqual(contrast_ratio(
                    self.root, self.style.lookup(name, "foreground", ("selected",)),
                    self.style.lookup(name, "background", ("selected",))), 4.5)

    def test_focus_border_is_visible_and_distinct(self):
        for name in ("TButton", "Accent.TButton", "Danger.TButton", "Toolbutton", "TEntry", "TCombobox", "TSpinbox"):
            with self.subTest(style=name):
                focused = self.style.lookup(name, "bordercolor", ("focus",))
                normal = self.style.lookup(name, "bordercolor")
                self.assertNotEqual(self.root.winfo_rgb(focused), self.root.winfo_rgb(normal))
                self.assertEqual(int(self.style.lookup(name, "borderwidth", ("focus",))), 2)
                self.assertGreaterEqual(contrast_ratio(self.root, focused, self.colors["surface"]), 3)

    def test_selected_tab_hover_changes_background_with_and_without_focus(self):
        for focus in ((), ("focus",)):
            with self.subTest(focus=focus):
                selected = ("selected",) + focus
                hovered = ("selected", "active") + focus
                background = self.style.lookup("Toolbutton", "background", selected)
                hovered_background = self.style.lookup("Toolbutton", "background", hovered)
                self.assertNotEqual(self.root.winfo_rgb(background), self.root.winfo_rgb(hovered_background))
                self.assertGreaterEqual(contrast_ratio(
                    self.root, self.style.lookup("Toolbutton", "foreground", hovered), hovered_background), 4.5)
                if focus:
                    self.assertEqual(int(self.style.lookup("Toolbutton", "borderwidth", hovered)), 2)
                self.assertEqual(
                    self.style.lookup("Toolbutton", "background", hovered + ("pressed",)),
                    self.style.lookup("Toolbutton", "background", ("pressed",)),
                )

    def test_cards_and_other_widget_styles_register(self):
        for name in ("Card.TFrame", "SoftCard.TFrame", "Header.Surface.TFrame", "Treeview",
                     "Treeview.Heading", "Horizontal.TProgressbar", "Horizontal.TScrollbar", "Vertical.TScrollbar"):
            self.assertTrue(self.style.layout(name), name)
        for name in ("Card.TFrame", "SoftCard.TFrame"):
            self.assertEqual(int(self.style.lookup(name, "borderwidth")), 1)
        self.assertGreaterEqual(int(self.style.lookup("Treeview", "rowheight")), self.line_height)

    def test_image_borders_survive_collection_and_reconfiguration(self):
        retained = self.root._hub_theme_images
        self.assertTrue(retained)
        image_names = {str(image) for image in retained.values()}
        gc.collect()
        self.assertTrue(image_names <= set(self.root.tk.splitlist(self.root.tk.call("image", "names"))))
        colors, line_height = configure_hub_style(self.root)
        self.assertEqual(set(map(str, self.root._hub_theme_images.values())), image_names)
        self.assertEqual(colors, self.colors)
        self.assertEqual(line_height, self.line_height)

    def test_button_geometry_and_enabled_state_stay_stable_at_display_scales(self):
        frame = ttk.Frame(self.root)
        frame.pack()
        button = ttk.Button(frame, text="발송 시작", width=8, style="Accent.TButton")
        button.pack()
        self.addCleanup(frame.destroy)

        def restore_theme():
            self.root.tk.call("tk", "scaling", self.original_scaling)
            configure_hub_style(self.root)

        self.addCleanup(restore_theme)
        for scaling in (self.original_scaling, 2.0, 8 / 3):
            with self.subTest(scaling=scaling):
                self.root.tk.call("tk", "scaling", scaling)
                # Tk quantizes scaling to the monitor's integer pixel size.
                # The helper must preserve the value Tk actually accepted.
                accepted_scaling = float(self.root.tk.call("tk", "scaling"))
                configure_hub_style(self.root)
                button.configure(style="Accent.TButton")
                button.state(["!disabled", "!active", "!pressed", "!focus"])
                self.root.update_idletasks()
                normal_size = (button.winfo_reqwidth(), button.winfo_reqheight())
                button.state(["disabled"])
                button.configure(style="Busy.Accent.TButton", text="처리 중")
                self.root.update_idletasks()
                self.assertEqual((button.winfo_reqwidth(), button.winfo_reqheight()), normal_size)
                self.assertTrue(button.instate(["disabled"]))
                configure_hub_style(self.root)
                self.assertTrue(button.instate(["disabled"]))
                self.assertEqual(float(self.root.tk.call("tk", "scaling")), accepted_scaling)
                self.assertGreater(normal_size[1], self.line_height)
                button.configure(text="발송 시작")


if __name__ == "__main__":
    unittest.main()
