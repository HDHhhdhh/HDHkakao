"""Presentation-only ttk theme for the local KakaoTalk Hub.

The image borders are authored here with Tk PhotoImage. They do not depend on
an image package, native theme APIs, a browser, or application state. The root
owns their Python references for the lifetime of its Tcl interpreter.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk


def _rounded_border_image(
    root: tk.Misc, *, fill: str, border: str, thickness: int = 1
) -> tk.PhotoImage:
    """Draw one small border; ttk stretches its center with nine-slice sizing."""
    radius = 6
    size = radius * 2 + 4
    photo = tk.PhotoImage(master=root, width=size, height=size)

    def inside(x: int, y: int, inset: int) -> bool:
        left = top = inset
        right = bottom = size - inset
        corner_radius = max(0, radius - inset)
        # Pixel centers avoid an uneven extra row on the right/bottom edges.
        px, py = x + 0.5, y + 0.5
        if not (left <= px < right and top <= py < bottom):
            return False
        cx = min(max(px, left + corner_radius), right - corner_radius)
        cy = min(max(py, top + corner_radius), bottom - corner_radius)
        return (px - cx) ** 2 + (py - cy) ** 2 <= corner_radius ** 2

    for y in range(size):
        start = 0
        previous = None
        for x in range(size):
            color = (fill if inside(x, y, thickness) else border) if inside(x, y, 0) else None
            if color != previous:
                if previous is not None:
                    photo.put(previous, to=(start, y, x, y + 1))
                start, previous = x, color
        if previous is not None:
            photo.put(previous, to=(start, y, size, y + 1))
    return photo


def _button_style(
    root: tk.Misc,
    style: ttk.Style,
    images: dict[str, tk.PhotoImage],
    name: str,
    colors: dict[str, str],
    *,
    normal: str,
    active: str,
    pressed: str,
    foreground: str,
    border: str,
    disabled: str | None = None,
    disabled_foreground: str | None = None,
    padding: tuple[int, int] = (10, 5),
    selected: str | None = None,
) -> None:
    """Give each button variant a complete, ordered ttk state palette."""
    disabled = disabled or colors["disabled"]
    disabled_foreground = disabled_foreground or colors["disabled_text"]
    fills = {
        "normal": normal,
        "active": active,
        "pressed": pressed,
        "focus": normal,
        "active_focus": active,
        "pressed_focus": pressed,
        "disabled": disabled,
    }
    if selected is not None:
        fills.update(selected=selected, selected_focus=selected,
                     selected_active=active, selected_active_focus=active)

    element = f"Hub.{name}.border"
    # Reconfiguration in the same interpreter reuses the images referenced by
    # the existing Tcl element. Replacing them would leave dangling image IDs.
    if element not in style.element_names():
        for state, fill in fills.items():
            stroke = colors["focus"] if "focus" in state else border
            if state == "disabled":
                stroke = colors["disabled_line"]
            images[f"{name}:{state}"] = _rounded_border_image(
                root, fill=fill, border=stroke, thickness=2 if "focus" in state else 1
            )
        state_images = [
            ("disabled", images[f"{name}:disabled"]),
            ("pressed", "focus", "!disabled", images[f"{name}:pressed_focus"]),
            ("pressed", "!disabled", images[f"{name}:pressed"]),
        ]
        if selected is not None:
            state_images.extend([
                ("selected", "active", "focus", "!disabled", images[f"{name}:selected_active_focus"]),
                ("selected", "active", "!disabled", images[f"{name}:selected_active"]),
                ("selected", "focus", "!disabled", images[f"{name}:selected_focus"]),
                ("selected", "!disabled", images[f"{name}:selected"]),
            ])
        state_images.extend([
            ("active", "focus", "!disabled", images[f"{name}:active_focus"]),
            ("active", "!disabled", images[f"{name}:active"]),
            ("focus", "!disabled", images[f"{name}:focus"]),
        ])
        style.element_create(
            element, "image", images[f"{name}:normal"], *state_images,
            border=6, padding=0, sticky="nsew",
        )
    style.layout(name, [(element, {"sticky": "nsew", "children": [
        ("Button.padding", {"sticky": "nsew", "children": [
            ("Button.label", {"sticky": "nsew"}),
        ]}),
    ]})])
    style.configure(
        name, padding=padding, background=normal, foreground=foreground,
        bordercolor=border, borderwidth=1, relief="flat", anchor="center",
        focusthickness=0, font=("Malgun Gothic", 10),
    )
    backgrounds = [("disabled", disabled), ("pressed", "!disabled", pressed)]
    if selected is not None:
        backgrounds.append(("selected", "active", "!disabled", active))
        backgrounds.append(("selected", "!disabled", selected))
    backgrounds.extend([
        ("active", "!disabled", active), ("focus", "!disabled", normal),
        ("!disabled", normal),
    ])
    style.map(
        name,
        background=backgrounds,
        foreground=[
            ("disabled", disabled_foreground), ("pressed", "!disabled", foreground),
            ("active", "!disabled", foreground), ("focus", "!disabled", foreground),
            ("!disabled", foreground),
        ],
        bordercolor=[
            ("disabled", colors["disabled_line"]), ("focus", "!disabled", colors["focus"]),
            ("pressed", "!disabled", border), ("active", "!disabled", border),
            ("!disabled", border),
        ],
        borderwidth=[("focus", "!disabled", 2), ("!focus", 1)],
    )


def configure_hub_style(root: tk.Misc) -> tuple[dict[str, str], int]:
    """Apply the Hub styles and return the palette and body-font line height.

    This helper changes presentation only. It preserves Tk scaling and does
    not install bindings, start timers, or change any widget's enabled state.
    """
    colors = {
        "bg": "#f7f7f8", "header": "#ffffff", "nav": "#ffffff",
        "surface": "#ffffff", "meta": "#f4f4f5", "soft": "#f4f4f5",
        "line": "#e4e4e7", "text": "#18181b", "muted": "#52525b",
        "accent": "#2563eb", "accent_dark": "#1d4ed8", "blue": "#2563eb",
        "danger": "#b91c1c", "warning": "#92400e", "success": "#166534",
        "kakao": "#fee500", "focus": "#18181b",
        "select": "#e4e4e7", "select_text": "#18181b",
        "danger_wash": "#fef2f2", "danger_text": "#991b1b",
        "warning_wash": "#fffbeb", "warning_text": "#92400e",
        "success_wash": "#f0fdf4", "success_text": "#166534",
        "busy": "#dbeafe", "busy_text": "#1e3a8a",
        "disabled": "#f4f4f5", "disabled_text": "#62626a", "disabled_line": "#dedee2",
    }
    root.configure(background=colors["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    base_font = ("Malgun Gothic", 10)
    small_font = ("Malgun Gothic", 9)
    small_bold = ("Malgun Gothic", 9, "bold")
    heading_font = ("Malgun Gothic", 11, "bold")
    title_font = ("Malgun Gothic", 15, "bold")
    ui_line_height = tkfont.Font(root, font=base_font).metrics("linespace")
    style.configure(".", font=base_font, background=colors["bg"], foreground=colors["text"])

    frames = {
        "TFrame": "bg", "Header.TFrame": "header", "Nav.TFrame": "nav",
        "Surface.TFrame": "surface", "Meta.TFrame": "meta", "Soft.TFrame": "soft",
        "Sidebar.TFrame": "bg", "Header.Surface.TFrame": "surface",
        "Card.TFrame": "surface", "SoftCard.TFrame": "soft",
    }
    for name, background in frames.items():
        style.configure(name, background=colors[background])
    for name in ("Card.TFrame", "SoftCard.TFrame"):
        style.configure(name, borderwidth=1, relief="solid", bordercolor=colors["line"])

    # All text remains at least 9pt, including metadata and sidebar hints.
    labels = {
        "TLabel": ("bg", "text", base_font),
        "Surface.TLabel": ("surface", "text", base_font),
        "SurfaceMuted.TLabel": ("surface", "muted", small_font),
        "Soft.TLabel": ("soft", "text", small_font),
        "SoftMuted.TLabel": ("soft", "muted", small_font),
        "Sidebar.TLabel": ("bg", "text", base_font),
        "SidebarMuted.TLabel": ("bg", "muted", small_font),
        "ScreenIdle.TLabel": ("soft", "muted", small_bold),
        "ScreenActive.TLabel": ("danger_wash", "danger_text", small_bold),
        "ScreenStop.TLabel": ("warning_wash", "warning_text", small_bold),
        "CommandTitle.TLabel": ("surface", "text", title_font),
        "CommandMuted.TLabel": ("surface", "muted", small_font),
        "NavTitle.TLabel": ("nav", "text", title_font),
        "NavMuted.TLabel": ("nav", "muted", small_font),
        "HeaderMuted.TLabel": ("header", "muted", small_font),
        "MetaKey.TLabel": ("meta", "muted", small_bold),
        "MetaValue.TLabel": ("meta", "text", small_font),
        "Muted.TLabel": ("bg", "muted", base_font),
        "Title.TLabel": ("bg", "text", title_font),
        "AppTitle.TLabel": ("header", "text", heading_font),
        "Section.TLabel": ("surface", "text", heading_font),
        "Metric.TLabel": ("surface", "text", ("Malgun Gothic", 10, "bold")),
        "PanelTitle.TLabel": ("soft", "text", heading_font),
        "DropEmpty.TLabel": ("surface", "muted", base_font),
    }
    for name, (background, foreground, font) in labels.items():
        style.configure(name, background=colors[background], foreground=colors[foreground], font=font)
    style.configure("DropEmpty.TLabel", anchor="center", justify="center", padding=(8, 12))

    images = getattr(root, "_hub_theme_images", None)
    if images is None:
        images = {}
        root._hub_theme_images = images
    common = dict(normal=colors["surface"], active=colors["soft"], pressed=colors["line"],
                  foreground=colors["text"], border="#d4d4d8")
    for name in ("TButton", "Secondary.TButton", "Blue.TButton"):
        _button_style(root, style, images, name, colors, **common)
    _button_style(root, style, images, "Compact.TButton", colors, padding=(8, 4), **common)
    _button_style(root, style, images, "Accent.TButton", colors,
                  normal=colors["accent"], active=colors["accent_dark"], pressed="#1e40af",
                  foreground="#ffffff", border=colors["accent_dark"])
    _button_style(root, style, images, "Busy.Accent.TButton", colors,
                  normal=colors["busy"], active=colors["busy"], pressed=colors["busy"],
                  foreground=colors["busy_text"], border="#93c5fd",
                  disabled=colors["busy"], disabled_foreground=colors["busy_text"])
    _button_style(root, style, images, "Danger.TButton", colors,
                  normal=colors["surface"], active=colors["danger_wash"], pressed="#fee2e2",
                  foreground=colors["danger"], border="#fca5a5")
    _button_style(root, style, images, "Toolbutton", colors, padding=(10, 5),
                  normal=colors["bg"], active=colors["soft"], pressed=colors["line"],
                  foreground=colors["text"], border=colors["line"], selected=colors["surface"])

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=colors["surface"], foreground=colors["text"],
                        background=colors["surface"], bordercolor="#a1a1aa", borderwidth=1,
                        lightcolor="#a1a1aa", darkcolor="#a1a1aa", insertcolor=colors["text"],
                        padding=(5, 4), selectbackground=colors["select"], selectforeground=colors["select_text"])
        style.map(name, bordercolor=[("focus", colors["focus"]), ("disabled", colors["line"])],
                  lightcolor=[("focus", colors["focus"])], darkcolor=[("focus", colors["focus"])],
                  borderwidth=[("focus", 2), ("!focus", 1)],
                  fieldbackground=[("disabled", colors["disabled"]), ("readonly", colors["soft"])],
                  foreground=[("disabled", colors["disabled_text"])])
    for name in ("TCheckbutton", "TRadiobutton", "Direct.TCheckbutton"):
        style.configure(name, background=colors["surface"], foreground=colors["text"],
                        focuscolor=colors["focus"], indicatorbackground=colors["surface"],
                        indicatorforeground=colors["text"], indicatormargin=(1, 1, 5, 1), padding=0)
        style.map(name, background=[("active", colors["surface"])],
                  foreground=[("disabled", colors["disabled_text"])],
                  indicatorbackground=[("selected", colors["text"]), ("disabled", colors["disabled"])],
                  indicatorforeground=[("selected", colors["surface"])])
    style.configure("Direct.TCheckbutton", font=small_bold)

    style.configure("Treeview", rowheight=max(32, ui_line_height + 12),
                    background=colors["surface"], fieldbackground=colors["surface"],
                    foreground=colors["text"], bordercolor=colors["line"],
                    lightcolor=colors["line"], darkcolor=colors["line"], borderwidth=1, font=base_font)
    style.map("Treeview", background=[("selected", colors["select"])],
              foreground=[("selected", colors["select_text"])])
    style.configure("Treeview.Heading", font=small_bold, background=colors["soft"],
                    foreground=colors["muted"], bordercolor=colors["line"],
                    lightcolor=colors["line"], darkcolor=colors["line"], relief="flat", padding=(8, 6))
    style.map("Treeview.Heading", background=[("active", colors["line"])],
              foreground=[("active", colors["text"])])
    for orientation in ("Horizontal", "Vertical"):
        style.configure(f"{orientation}.TScrollbar", background="#c4c4cb", troughcolor=colors["bg"],
                        bordercolor=colors["bg"], lightcolor="#c4c4cb", darkcolor="#c4c4cb",
                        arrowcolor=colors["muted"], arrowsize=12, borderwidth=0, relief="flat")
        style.map(f"{orientation}.TScrollbar", background=[("pressed", "#71717a"), ("active", "#a1a1aa")],
                  lightcolor=[("pressed", "#71717a"), ("active", "#a1a1aa")],
                  darkcolor=[("pressed", "#71717a"), ("active", "#a1a1aa")])
    style.configure("Horizontal.TProgressbar", troughcolor=colors["soft"], background=colors["accent"],
                    bordercolor=colors["line"], lightcolor=colors["accent"], darkcolor=colors["accent"],
                    borderwidth=0, thickness=5)
    return colors, ui_line_height
