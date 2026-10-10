"""Real Tk layout regressions; every external action is blocked.

The application is mapped with alpha=0 solely to let Tk compute real widget
geometry. These tests neither inspect nor control another application. The
DnD check inspects the registered Tcl target and calls the application handler;
it does not claim an operating-system drag gesture was performed.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager
from itertools import combinations
from pathlib import Path
import platform
import tempfile
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

from kakao_direct import DirectKakaoConfig
from kakao_sender import DEFAULT_CONFIG
from kakao_sender_gui import KakaoSenderApp


@contextmanager
def isolated_gui_fixture(*, visible=False, geometry=(1024, 720), scaling=4 / 3, populate=False):
    """Yield an isolated app and synthetic files for layout/visual QA.

    ``visible=True`` is reserved for the parent's explicitly authorized review
    of this application. ``scaling`` sets Tk pixels per point before fonts are
    built. A caller must remain inside this context for the entire mainloop.
    No credentials, saved configuration, run journal, network, or workers are
    available. ``populate=True`` loads three synthetic rows and a common PDF.
    """
    # The DnD loader queries platform.system()/machine(). On Windows Python
    # may run its read-only version probe once. Resolve that metadata before
    # the strict no-process guard; the displayed fixture still cannot launch
    # workers or any external action.
    platform.uname()
    with ExitStack() as stack:
        temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="kakao-layout-"))
        document = Path(temp) / "recipients.csv"
        document.write_text("chat_name,text\nfixture-one,first message\nfixture-two,second message\nfixture-three,third message\n", encoding="utf-8-sig")
        pdf = Path(temp) / "common attachment.pdf"
        pdf.write_bytes(b"%PDF-1.4 synthetic fixture")
        second_file = Path(temp) / "replacement.txt"
        second_file.write_text("synthetic fixture", encoding="utf-8")
        blocked = []
        pending_documents = []

        def capture_document_read(app, request):
            # Keep this layout fixture free of worker execution. Production
            # worker timing is covered by the responsive-loading tests.
            app._document_load_active_token = request.token
            app._document_load_pending_request = None
            pending_documents.append(request)

        stack.enter_context(patch.object(KakaoSenderApp, "_start_document_read", capture_document_read))

        def forbidden(*_args, **_kwargs):
            blocked.append("external action")
            raise AssertionError("GUI layout fixture must not launch processes, workers, network, or Kakao control")

        for target in (
            "subprocess.Popen", "threading.Thread.start", "socket.create_connection",
            "socket.socket.connect", "socket.socket.connect_ex",
            "kakao_sender_gui.require_pyautogui", "kakao_sender_gui.save_config",
        ):
            stack.enter_context(patch(target, side_effect=forbidden))
        for method in ("create_direct_bridge", "save_direct_settings"):
            stack.enter_context(patch.object(KakaoSenderApp, method, side_effect=forbidden))
        stack.enter_context(patch("kakao_sender_gui.RunJournal", return_value=None))
        stack.enter_context(patch.object(KakaoSenderApp, "load_relaxed_config", return_value=copy.deepcopy(DEFAULT_CONFIG)))
        stack.enter_context(patch.object(KakaoSenderApp, "load_direct_config", return_value=DirectKakaoConfig(enabled=False)))
        for method in ("offer_run_recovery", "drain_queue", "tick_elapsed", "tick_schedule_capacity"):
            stack.enter_context(patch.object(KakaoSenderApp, method))
        for method in ("showinfo", "showwarning", "showerror", "askyesno", "askyesnocancel"):
            stack.enter_context(patch(f"kakao_sender_gui.messagebox.{method}", return_value=False))
        for method in ("askopenfilename", "askopenfilenames", "asksaveasfilename", "askdirectory"):
            stack.enter_context(patch(f"kakao_sender_gui.filedialog.{method}", return_value=""))
        # This reusable fixture may be shown for a visual review. Make every
        # external toolbar/menu entry inert even if accidentally activated.
        for method in ("start_sending", "retry_failed_rows", "open_chat_mapping", "open_manual_chat_mapping",
                       "capture_coordinate", "test_coordinate_click", "open_log_folder",
                       "open_connection_diagnostics", "open_run_history", "refresh_schedule_capacity"):
            stack.enter_context(patch.object(KakaoSenderApp, method, return_value=None))
        original_init = tk.Tk.__init__
        created_roots = []

        def hidden_init(root, *args, **kwargs):
            try:
                original_init(root, *args, **kwargs)
            except tk.TclError as exc:
                raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
            created_roots.append(root)
            root.withdraw()
            root.attributes("-alpha", 0.0)
            if scaling is not None:
                root.tk.call("tk", "scaling", scaling)

        stack.enter_context(patch.object(tk.Tk, "__init__", hidden_init))
        stack.enter_context(patch.object(KakaoSenderApp, "apply_display_half_geometry", lambda root: root.geometry(f"{geometry[0]}x{geometry[1]}+0+0")))
        try:
            app = KakaoSenderApp()
        except BaseException:
            for root in created_roots:
                root.destroy()
            raise
        try:
            if not visible:
                # A small CI desktop otherwise caps WM geometry at its work
                # area. The transparent test surface may exceed that area.
                app.maxsize(4096, 2160)
                # Explicit pixel-size fixtures also exercise windows smaller
                # than the DPI-scaled user minimum, without changing production.
                app.minsize(1, 1)
            app.geometry(f"{geometry[0]}x{geometry[1]}+0+0")
            app.deiconify()
            app.update()
            app.rebalance_layout()
            app.update()
            if populate:
                app.reload_file(document)
                app.apply_common_attachment_paths((pdf,))
            if visible:
                app.title("KakaoTalk Hub - Synthetic Layout QA (sending disabled)")
                app.attributes("-alpha", 1.0)
            yield SimpleNamespace(app=app, document=document, pdf=pdf, second_file=second_file, blocked=blocked,
                                  pending_documents=pending_documents)
        finally:
            try:
                if app.winfo_exists():
                    for timer in app.tk.splitlist(app.tk.call("after", "info")):
                        app.after_cancel(timer)
                    app.destroy()
            except tk.TclError:
                pass  # A visible preview may already have been closed by its reviewer.


class GuiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_stack = ExitStack()
        cls.addClassCleanup(cls.fixture_stack.close)
        # Reuse a single interpreter for these widget interactions, just as
        # the application does. Repeated Tcl/Tk DLL initialization can fail
        # intermittently on restricted Windows test desktops.
        cls.fixture = cls.fixture_stack.enter_context(isolated_gui_fixture())

    def setUp(self):
        for name in ("app", "document", "pdf", "second_file", "blocked", "pending_documents"):
            setattr(self, name, getattr(self.fixture, name))
        self.addCleanup(lambda: self.assertEqual(self.blocked, [], "a layout interaction attempted an external action"))
        self.app.rows = []
        self.app.results = []
        self.app.undo_stack = []
        self.app.file_path = None
        self.app.file_var.set("선택된 파일 없음")
        self.app.status_var.set("준비")
        self.app.screen_use_state_var.set("화면 사용 안 함")
        self.app.common_schedule_var.set(False)
        self.app.direct_enabled_var.set(False)
        self.app.set_auto_match_send_wait(False)
        self.app.stop_button.state(["disabled"])
        self.app.show_sidebar_tab("attachments")
        self.app.populate_table()
        self.app.refresh_preview_count()
        self.resize(1024, 720)

    def resize(self, width, height):
        self.app.geometry(f"{width}x{height}+0+0")
        self.app.deiconify()
        self.app.update()
        self.app.rebalance_layout()
        self.app.update()
        self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), (width, height))

    def test_busy_presentation_keeps_callback_lock_and_button_position(self):
        button = self.app.start_button
        command = button.cget("command")
        original_bounds = self.bounds(button)
        try:
            self.app.set_auto_match_send_wait(True)
            self.app.update()
            self.assertTrue(button.instate(["disabled"]))
            self.assertEqual(button.cget("text"), "준비 중")
            self.assertEqual(button.cget("style"), "Busy.Accent.TButton")
            self.assertEqual(button.cget("command"), command)
            self.assertEqual(self.bounds(button), original_bounds)

            self.app.set_auto_match_send_wait(False)
            self.app.active_run = SimpleNamespace(run_id="visual-fixture")
            button.state(["disabled"])
            self.app.status_var.set("실행 중")
            self.app.update()
            self.assertEqual(button.cget("text"), "처리 중")
            self.assertTrue(button.instate(["disabled"]))
            self.assertEqual(button.cget("command"), command)
            self.assertEqual(self.bounds(button), original_bounds)
        finally:
            self.app.active_run = None
            self.app.set_auto_match_send_wait(False)
            self.app.status_var.set("준비")
            self.app.update()
        self.assertEqual(button.cget("style"), "Accent.TButton")
        self.assertEqual(button.cget("text"), "발송 시작")
        self.assertTrue(button.instate(["!disabled"]))

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from GuiLayoutTests.descendants(child)

    def bounds(self, widget):
        return (widget.winfo_rootx() - self.app.winfo_rootx(),
                widget.winfo_rooty() - self.app.winfo_rooty(),
                widget.winfo_width(), widget.winfo_height())

    def fully_visible(self, widget):
        if not widget.winfo_ismapped():
            return False
        x, y, width, height = self.bounds(widget)
        ancestor = widget.master
        while ancestor is not None:
            # Canvas window children can keep ismapped=1 when their canvas
            # tab is hidden; visibility requires the entire ancestor chain.
            if not ancestor.winfo_ismapped():
                return False
            ax, ay, aw, ah = self.bounds(ancestor)
            if x < ax or y < ay or x + width > ax + aw or y + height > ay + ah:
                return False
            ancestor = ancestor.master
        return True

    def in_settings_scroll_content(self, widget):
        content = self.app.__dict__.get("settings_content")
        while widget is not None:
            if widget is content:
                return True
            widget = widget.master
        return False

    def assert_inside(self, widget):
        self.assertTrue(widget.winfo_ismapped(), f"{widget} is hidden")
        x, y, width, height = self.bounds(widget)
        self.assertGreater(width, 1, f"{widget} has no usable width")
        self.assertGreater(height, 1, f"{widget} has no usable height")
        self.assertGreaterEqual(x, 0, str(widget))
        self.assertGreaterEqual(y, 0, str(widget))
        self.assertLessEqual(x + width, self.app.winfo_width(), str(widget))
        self.assertLessEqual(y + height, self.app.winfo_height(), str(widget))

    def assert_controls_accessible(self):
        controls = [widget for widget in self.descendants(self.app)
                    if widget.winfo_class() in {"TButton", "TCheckbutton", "TRadiobutton"}
                    and widget.winfo_ismapped()
                    and (not self.in_settings_scroll_content(widget) or self.fully_visible(widget))]
        self.assertGreaterEqual(len(controls), 8)
        for widget in controls:
            self.assert_inside(widget)
            self.assertTrue(self.fully_visible(widget), f"control is clipped by its parent: {widget.cget('text')}")
            self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth() - 2,
                                    f"button text is compressed: {widget.cget('text')}")
        for left, right in combinations(controls, 2):
            lx, ly, lw, lh = self.bounds(left)
            rx, ry, rw, rh = self.bounds(right)
            overlap = min(lx + lw, rx + rw) > max(lx, rx) and min(ly + lh, ry + rh) > max(ly, ry)
            self.assertFalse(overlap, f"controls overlap: {left.cget('text')} / {right.cget('text')}")

    def test_three_sizes_preserve_table_log_and_primary_actions(self):
        for width, height in ((1024, 720), (1280, 800), (1600, 900)):
            with self.subTest(width=width, height=height):
                self.resize(width, height)
                for name in ("tree", "log_frame", "start_button", "stop_button", "screen_use_stop_button", "attachment_path_button"):
                    self.assert_inside(getattr(self.app, name))
                self.assertGreaterEqual(self.app.tree.winfo_height(), 180, "preview must retain several visible rows")
                self.assertGreaterEqual(self.app.log_text.winfo_height(), 45, "log must show at least two lines")
                self.assert_controls_accessible()
                before = (self.bounds(self.app.start_button), self.bounds(self.app.stop_button))
                self.app.start_button.state(["disabled"])
                self.app.stop_button.state(["!disabled"])
                self.app.update()
                self.assert_inside(self.app.stop_button)
                self.assert_controls_accessible()
                self.assertEqual((self.bounds(self.app.start_button), self.bounds(self.app.stop_button)), before,
                                 "run state must preserve the primary action positions")
                self.app.start_button.state(["!disabled"])
                self.app.stop_button.state(["disabled"])

    def test_attachment_panel_is_bounded_and_table_has_horizontal_navigation(self):
        self.resize(1280, 800)
        preview = self.bounds(self.app.preview_frame)
        attachment = self.bounds(self.app.attachment_panel)
        self.assertGreaterEqual(attachment[0], preview[0] + preview[2], "attachments belong beside the preview")
        self.assertLessEqual(attachment[2], 420, "path input must not stretch across the workspace")
        self.assertGreaterEqual(int(self.app.attachment_list.cget("height")), 3)
        self.assert_inside(self.app.table_xscroll)
        self.resize(1024, 720)
        self.app.tree.xview_moveto(1)
        self.app.update()
        right = self.app.tree.xview()
        self.assertGreater(right[0], 0, "the rightmost delivery columns must be reachable")
        self.assertAlmostEqual(right[1], 1.0)
        self.app.tree.xview_moveto(0)
        self.assertAlmostEqual(self.app.tree.xview()[0], 0.0)

    def test_long_path_and_status_do_not_push_actions_outside_window(self):
        self.app.file_var.set("C:\\fixture\\" + "긴 파일 이름 " * 18 + "recipients.csv")
        self.app.status_var.set("점검 완료 · 직접 전송 120건 · 매칭 실패 20건 제외 · 예약 등록 10건")
        self.app.screen_use_state_var.set("화면 사용 중 · 수동 매칭을 위해 카카오톡을 확인하고 있습니다. Shift+Esc로 중단할 수 있습니다.")
        self.resize(1024, 720)
        self.assert_controls_accessible()
        self.assertGreaterEqual(self.app.tree.winfo_height(), 180)
        self.assert_inside(self.app.log_frame)

    def test_compact_tabs_keep_both_settings_and_attachments_reachable(self):
        self.resize(1024, 720)
        self.assertTrue(self.app.sidebar_is_compact)
        self.app.show_sidebar_tab("settings")
        self.app.update()
        self.assertEqual(self.app.sidebar_tab_var.get(), "settings")
        self.assert_inside(self.app.settings_panel)
        for name in ("mapping_button", "manual_mapping_button", "schedule_at_entry", "coordinate_button"):
            widget = getattr(self.app, name)
            widget.event_generate("<FocusIn>")
            self.app.update()
            self.assert_inside(widget)
            self.assertTrue(self.fully_visible(widget), f"keyboard focus did not reveal {name}")
        self.assert_controls_accessible()
        self.app.show_sidebar_tab("attachments")
        self.app.update()
        self.assert_inside(self.app.attachment_panel)
        self.assert_inside(self.app.attachment_path_button)
        self.assert_controls_accessible()

    def test_file_loading_and_attachment_controls_prepare_all_rows_without_sending(self):
        with patch("kakao_sender_gui.filedialog.askopenfilename", return_value=str(self.document)):
            self.app.select_file()
        self.assertEqual(self.app.rows, [])
        self.assertTrue(self.app._document_loading)
        self.assertTrue(self.app.start_button.instate(["disabled"]))
        self.assertEqual(len(self.pending_documents), 1)
        request = self.pending_documents.pop()
        self.app.handle_document_load_result((request.token, request.read(request.path), None))
        self.assertFalse(self.app._document_loading)
        self.assertEqual(len(self.app.rows), 3)
        self.assertEqual(len(self.app.tree.get_children()), 3)
        self.app.attachment_path_var.set(str(self.pdf))
        self.app.attachment_path_button.invoke()
        self.assertTrue(all(row.attachments == (self.pdf.resolve(),) for row in self.app.rows))
        self.assertEqual(self.app.attachment_list.size(), 1)
        self.assertFalse(self.app.attachment_empty_label.winfo_ismapped())
        self.assertIsNone(self.app.active_run)
        self.assertIsNone(self.app.worker)
        self.assertIn("첨부", self.app.log_text.get("1.0", "end"))
        draft = str(self.second_file)
        self.app.attachment_path_var.set(draft)
        self.app.tree.selection_set("1")
        self.app.on_preview_selection_changed()
        self.assertEqual(self.app.attachment_path_var.get(), draft, "selection must preserve an unapplied path draft")

    def test_dnd_targets_are_registered_and_drop_respects_preflight_lock(self):
        self.assertTrue(self.app.file_drop_enabled, self.app.attachment_drop_hint.cget("text"))
        self.assertEqual(self.app.attachment_list.size(), 0, "empty-state guidance must not be a selectable fake file")
        self.assertTrue(self.app.attachment_empty_label.winfo_ismapped())
        targets = (self.app.tree, self.app.attachment_panel, *self.descendants(self.app.attachment_panel))
        for widget in targets:
            self.assertTrue(widget.bind("<<DropTargetTypes>>"), f"{widget} lacks Tcl DnD target registration")
            self.assertTrue(widget.bind("<<Drop>>"), f"{widget} lacks a drop callback")
        self.app.reload_file(self.document)
        def drop_event(path):
            data = self.app.tk.call("format", "%s", self.app.tk.call("list", str(path)))
            return SimpleNamespace(data=data)

        dropped = drop_event(self.pdf)
        self.assertEqual(self.app.on_attachment_drop(dropped), "copy")
        before = list(self.app.rows)
        self.app.set_auto_match_send_wait(True)
        for name in ("attachment_path_entry", "attachment_path_button", "attachment_pick_button", "attachment_clear_button"):
            self.assertTrue(getattr(self.app, name).instate(["disabled"]), name)
        self.assertEqual(self.app.on_attachment_drop(drop_event(self.second_file)), "refuse_drop")
        self.assertEqual(self.app.rows, before)
        self.app.set_auto_match_send_wait(False)
        for name in ("attachment_path_entry", "attachment_path_button", "attachment_pick_button", "attachment_clear_button"):
            self.assertTrue(getattr(self.app, name).instate(["!disabled"]), name)
        self.assertIsNone(self.app.active_run)


class GuiLayoutScalingTests(unittest.TestCase):
    def test_geometry_is_usable_at_normal_and_large_tk_scaling(self):
        for scaling in (4 / 3, 2.0, 8 / 3):
            with self.subTest(scaling=scaling), isolated_gui_fixture(scaling=scaling, populate=True) as fixture:
                audit = GuiLayoutTests()
                audit.app = fixture.app
                # Direct mode has the longer explanatory label, exercising
                # the scrollregion after a runtime text-height change.
                fixture.app.direct_enabled_var.set(True)
                fixture.app.on_direct_mode_changed(persist=False)
                sizes = ((2048, 1440), (2560, 1600)) if scaling > 2 else ((1024, 720), (1280, 800), (1600, 900))
                for width, height in sizes:
                    with self.subTest(scaling=scaling, size=(width, height)):
                        audit.resize(width, height)
                        for tab in ("attachments", "settings", "attachments"):
                            with self.subTest(scaling=scaling, size=(width, height), tab=tab):
                                fixture.app.show_sidebar_tab(tab)
                                fixture.app.update()
                                audit.assert_controls_accessible()
                                for name in ("tree", "log_frame", "start_button", "stop_button", "screen_use_stop_button"):
                                    audit.assert_inside(getattr(fixture.app, name))
                                if tab == "settings":
                                    audit.assert_inside(fixture.app.settings_panel)
                                    audit.assert_inside(fixture.app.settings_scrollbar)
                                    canvas = fixture.app.settings_canvas
                                    canvas.yview_moveto(1)
                                    fixture.app.update()
                                    if fixture.app.settings_content.winfo_reqheight() > canvas.winfo_height():
                                        self.assertGreater(canvas.yview()[0], 0, "settings scrollbar must reach overflowing content")
                                    canvas.yview_moveto(0)
                                    self.assertAlmostEqual(canvas.yview()[0], 0.0)
                                    if fixture.app.settings_content.winfo_reqheight() > canvas.winfo_height():
                                        canvas.event_generate("<MouseWheel>", delta=-120)
                                        fixture.app.update()
                                        down = canvas.yview()[0]
                                        self.assertGreater(down, 0, "local mouse wheel must scroll down")
                                        canvas.event_generate("<MouseWheel>", delta=120)
                                        fixture.app.update()
                                        self.assertLess(canvas.yview()[0], down, "local mouse wheel must scroll back up")
                                    for name in ("direct_toggle", "mapping_button", "manual_mapping_button", "common_schedule_check",
                                                 "schedule_at_entry", "schedule_picker_button", "schedule_counter_button", "coordinate_button", "retry_failed_button"):
                                        widget = getattr(fixture.app, name)
                                        self.assertTrue(widget.bind("<FocusIn>"), f"{name} lacks keyboard reveal binding")
                                        self.assertTrue(widget.bind("<MouseWheel>"), f"{name} lacks local mouse-wheel binding")
                                        widget.event_generate("<FocusIn>")
                                        fixture.app.update()
                                        audit.assert_inside(widget)
                                        self.assertTrue(audit.fully_visible(widget), f"keyboard focus did not reveal {name}")
                                        audit.assert_controls_accessible()
                                    sx, sy, sw, sh = audit.bounds(fixture.app.settings_panel)
                                    bx, by, bw, bh = audit.bounds(fixture.app.sidebar_frame)
                                    self.assertGreaterEqual(sx, bx)
                                    self.assertGreaterEqual(sy, by)
                                    self.assertLessEqual(sx + sw, bx + bw)
                                    self.assertLessEqual(sy + sh, by + bh, "settings must fit its visible sidebar area")
                                else:
                                    audit.assert_inside(fixture.app.attachment_panel)
                                self.assertGreaterEqual(fixture.app.tree.winfo_height(), 180)
                                self.assertGreaterEqual(fixture.app.log_text.winfo_height(), 45)
                self.assertEqual(fixture.blocked, [])


if __name__ == "__main__":
    unittest.main()
