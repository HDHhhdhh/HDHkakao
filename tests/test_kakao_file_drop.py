from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from kakao_file_drop import attachment_paths_from_drop, attachment_paths_from_text, enable_file_drop, format_attachment_path_input, validated_attachment_paths
from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp


class FileDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Share one Tcl interpreter for list parsing. Repeated Windows Tcl
        # startup is unrelated to each attachment transaction under test.
        cls.tcl = tk.Tcl()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.image = self.base / '한글 사진 1.png'
        self.pdf = self.base / '수업 자료.pdf'
        self.image.write_bytes(b'local synthetic image')
        self.pdf.write_bytes(b'local synthetic pdf')

    def test_tcl_multifile_list_preserves_spaces_korean_and_braces(self):
        brace = self.base / '자료 {최종}.pdf'
        brace.write_bytes(b'fixture')
        data = '{' + str(self.image) + '} {' + str(self.pdf) + '} {' + str(brace) + '}'
        self.assertEqual(attachment_paths_from_drop(data, self.tcl.splitlist), (self.image, self.pdf, brace))

    def test_drop_deduplicates_and_never_splits_a_filename_at_semicolon(self):
        name = self.base / '자료;최종.pdf'
        name.write_bytes(b'fixture')
        data = '{' + str(name) + '} {' + str(name) + '}'
        self.assertEqual(attachment_paths_from_drop(data, self.tcl.splitlist), (name,))
        self.assertEqual(attachment_paths_from_text(str(name)), (name,))

    def test_text_supports_relative_paths_quotes_separators_and_newlines(self):
        text = '"한글 사진 1.png";\n"수업 자료.pdf"'
        self.assertEqual(attachment_paths_from_text(text, base_dir=self.base), (self.image, self.pdf))
        self.assertEqual(attachment_paths_from_text('한글 사진 1.png|수업 자료.pdf', base_dir=self.base), (self.image, self.pdf))

    def test_quoted_multiple_semicolon_paths_roundtrip(self):
        first = self.base / '자료;최종.pdf'
        second = self.base / '사진;최종.png'
        first.write_bytes(b'fixture')
        second.write_bytes(b'fixture')
        paths = (first, self.pdf, second)
        self.assertEqual(attachment_paths_from_text(format_attachment_path_input(paths)), paths)
        with self.assertRaises(ValueError):
            attachment_paths_from_text('"unclosed;path.pdf')

    def test_missing_directory_empty_or_malformed_input_is_rejected(self):
        for values in ((self.image, self.base / 'missing.pdf'), (self.base,), ()):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validated_attachment_paths(values)
        with self.assertRaises(ValueError):
            attachment_paths_from_drop('{unclosed', self.tcl.splitlist)
        with self.assertRaises(ValueError):
            attachment_paths_from_drop('', self.tcl.splitlist)

    def make_app(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.tk = self.tcl.tk
        app.file_path = self.base / 'recipients.csv'
        app.rows = [MessageRow(2, '대상 A', '메시지 A', (self.pdf,), scheduled_at=datetime(2027, 1, 1, 12)),
                    MessageRow(3, '대상 B', '메시지 B', (self.pdf,))]
        app.undo_stack = []
        app.preview_edit_locked = Mock(return_value=False)
        app.log = Mock()
        app.finish_preview_mutation = Mock()
        app.start_sending = Mock()
        app.send_worker = Mock()
        app.attachment_path_var = SimpleNamespace(get=lambda: str(self.image))
        return app

    def test_drop_changes_all_rows_once_and_undo_restores_messages_and_schedules(self):
        app = self.make_app()
        original = list(app.rows)
        result = app.on_attachment_drop(SimpleNamespace(data='{' + str(self.image) + '}'))
        self.assertEqual(result, 'copy')
        self.assertEqual([row.attachments for row in app.rows], [(self.image,), (self.image,)])
        self.assertEqual([row.text for row in app.rows], ['메시지 A', '메시지 B'])
        self.assertEqual(app.rows[0].scheduled_at, original[0].scheduled_at)
        self.assertEqual(len(app.undo_stack), 1)
        app.finish_preview_mutation.assert_called_once()
        app.start_sending.assert_not_called()
        app.send_worker.assert_not_called()
        app.undo_preview_change()
        self.assertEqual(app.rows, original)

    def test_dropped_semicolon_filename_with_another_file_can_be_reapplied_unchanged(self):
        app = self.make_app()
        semicolon_file = self.base / '자료;최종.pdf'
        semicolon_file.write_bytes(b'fixture')
        variable = SimpleNamespace(value='')
        variable.get = lambda: variable.value
        variable.set = lambda value: setattr(variable, 'value', value)
        app.attachment_path_var = variable
        app.attachment_list = Mock()
        app.attachment_help = Mock()
        app.finish_preview_mutation = lambda *_args: app.refresh_attachment_panel()
        data = '{' + str(semicolon_file) + '} {' + str(self.pdf) + '}'

        self.assertEqual(app.on_attachment_drop(SimpleNamespace(data=data)), 'copy')
        self.assertEqual(variable.get(), format_attachment_path_input((semicolon_file, self.pdf)))
        with patch('kakao_sender_gui.messagebox.showwarning') as warning:
            self.assertEqual(app.apply_attachment_path_input(), 'break')
        warning.assert_not_called()
        self.assertEqual([row.attachments for row in app.rows], [(semicolon_file, self.pdf)] * 2)
        self.assertEqual(len(app.undo_stack), 1)
        app.start_sending.assert_not_called()
        app.send_worker.assert_not_called()

    def test_path_input_uses_same_transaction_without_sending(self):
        app = self.make_app()
        self.assertEqual(app.apply_attachment_path_input(), 'break')
        self.assertEqual(app.rows[0].attachments, (self.image,))
        self.assertEqual(len(app.undo_stack), 1)
        app.start_sending.assert_not_called()
        app.send_worker.assert_not_called()

    def test_file_picker_rechecks_lock_after_dialog_returns(self):
        app = self.make_app()
        original = app.rows
        app.preview_edit_locked = Mock(side_effect=[False, True])
        app.pick_attachments = Mock(return_value=(self.image,))
        app.attach_files_to_all_rows()
        self.assertIs(app.rows, original)
        self.assertEqual(app.undo_stack, [])

    def test_drop_invalid_selection_is_atomic_and_refused(self):
        app = self.make_app()
        original = app.rows
        data = '{' + str(self.image) + '} {' + str(self.base / 'missing.pdf') + '}'
        with patch('kakao_sender_gui.messagebox.showwarning') as warning:
            self.assertEqual(app.on_attachment_drop(SimpleNamespace(data=data)), 'refuse_drop')
        self.assertIs(app.rows, original)
        self.assertEqual(app.undo_stack, [])
        warning.assert_called_once()
        app.start_sending.assert_not_called()

    def test_drop_and_path_application_are_blocked_during_run(self):
        app = self.make_app()
        original = app.rows
        app.preview_edit_locked = Mock(return_value=True)
        self.assertEqual(app.on_attachment_drop(SimpleNamespace(data='{' + str(self.image) + '}')), 'refuse_drop')
        app.apply_attachment_path_input()
        self.assertIs(app.rows, original)
        self.assertEqual(app.undo_stack, [])
        app.finish_preview_mutation.assert_not_called()

    def test_drop_without_rows_does_not_invent_a_recipient(self):
        app = self.make_app()
        app.rows = []
        with patch('kakao_sender_gui.messagebox.showinfo') as info:
            self.assertEqual(app.on_attachment_drop(SimpleNamespace(data='{' + str(self.image) + '}')), 'refuse_drop')
        self.assertEqual(app.rows, [])
        self.assertEqual(app.undo_stack, [])
        info.assert_called_once()

    def test_duplicate_selection_is_not_an_extra_undo_step(self):
        app = self.make_app()
        self.assertTrue(app.apply_common_attachment_paths((self.pdf, self.pdf)))
        self.assertEqual(app.undo_stack, [])
        app.finish_preview_mutation.assert_not_called()

    def test_dnd_dependency_failure_keeps_fallback_and_unregisters_partial_target(self):
        widget = Mock()
        widget.dnd_bind.side_effect = tk.TclError('unsupported')
        provider = SimpleNamespace(TkinterDnD=SimpleNamespace(require=Mock()), DND_FILES='DND_Files')
        enabled, hint = enable_file_drop(object(), [widget], Mock(), importer=lambda _: provider)
        self.assertFalse(enabled)
        self.assertIn('경로 입력', hint)
        widget.drop_target_unregister.assert_called_once()
        enabled, hint = enable_file_drop(object(), [], Mock(), importer=Mock(side_effect=ImportError))
        self.assertFalse(enabled)

    def test_dnd_registers_only_requested_subtrees_once_with_one_callback(self):
        panel, nested, entry, label, listing, button, tree, outside = [Mock() for _ in range(8)]
        panel.winfo_children.return_value = [entry, label, nested]
        nested.winfo_children.return_value = [listing, button]
        # Duplicate roots and repeated children must not register a target
        # twice; bare Mock children must not create an unbounded traversal.
        tree.winfo_children.return_value = [listing]
        callback = Mock(return_value='copy')
        provider = SimpleNamespace(TkinterDnD=SimpleNamespace(require=Mock()), DND_FILES='DND_Files')
        enabled, _ = enable_file_drop(outside, [panel, tree, entry, panel], callback, importer=lambda _: provider)
        self.assertTrue(enabled)
        provider.TkinterDnD.require.assert_called_once_with(outside)
        for widget in (panel, nested, entry, label, listing, button, tree):
            widget.drop_target_register.assert_called_once_with('DND_Files')
            widget.dnd_bind.assert_called_once_with('<<Drop>>', callback)
        outside.drop_target_register.assert_not_called()
        callback.assert_not_called()

    def test_dnd_child_registration_failure_cleans_up_entire_subtree_attempt(self):
        panel, entry, label = Mock(), Mock(), Mock()
        panel.winfo_children.return_value = [entry, label]
        entry.winfo_children.return_value = []
        # Include the failing target in cleanup because registration can
        # install provider state before reporting a failure.
        label.drop_target_register.side_effect = tk.TclError('registration failed')
        provider = SimpleNamespace(TkinterDnD=SimpleNamespace(require=Mock()), DND_FILES='DND_Files')
        enabled, hint = enable_file_drop(object(), [panel], Mock(), importer=lambda _: provider)
        self.assertFalse(enabled)
        self.assertIn('경로 입력', hint)
        for widget in (panel, entry, label):
            widget.drop_target_unregister.assert_called_once()
        label.dnd_bind.assert_not_called()

    def test_controls_disable_and_restore(self):
        app = self.make_app()
        app.attachment_path_entry = Mock()
        app.attachment_path_button = Mock()
        app.set_attachment_edit_state(True)
        app.attachment_path_entry.state.assert_called_with(['disabled'])
        app.set_attachment_edit_state(False)
        app.attachment_path_entry.state.assert_called_with(['!disabled'])

    def preflight_app(self, attachments):
        app = self.make_app()
        app.rows = [MessageRow(2, '대상 A', '', attachments)]
        app.log_check_progress = Mock()
        app.common_schedule_datetime = lambda: None
        app.common_schedule_var = SimpleNamespace(get=lambda: False)
        app.effective_schedule_datetime = lambda row, fallback: row.scheduled_at
        app.direct_enabled_var = SimpleNamespace(get=lambda: True)
        app.direct_mappings = {'대상 A': '101'}
        app.direct_mapping_labels = {'101': '대상 A'}
        app._latest_chat_snapshot = ()
        app.preflight_problem = Mock(return_value=False)
        app.status_var = SimpleNamespace(set=Mock())
        return app

    def test_direct_attachment_preflight_checks_metadata_without_needing_coordinates(self):
        app = self.preflight_app((self.pdf,))
        with patch('kakao_sender_gui.is_confirmed_mapping_label', return_value=True), patch('kakao_sender_gui.unverified_alias_mapping_names', return_value=()):
            self.assertTrue(app.preflight([(0, app.rows[0])]))
        app.preflight_problem.assert_not_called()
        app.start_sending.assert_not_called()

    def test_direct_attachment_preflight_rejects_unsupported_empty_and_excess_files(self):
        unsupported = self.base / 'program.exe'
        unsupported.write_bytes(b'local fixture')
        empty = self.base / 'empty.pdf'
        empty.touch()
        for paths in ((unsupported,), (empty,), (self.pdf,) * 11):
            with self.subTest(paths=paths):
                app = self.preflight_app(paths)
                with patch('kakao_sender_gui.is_confirmed_mapping_label', return_value=True), patch('kakao_sender_gui.unverified_alias_mapping_names', return_value=()):
                    self.assertFalse(app.preflight([(0, app.rows[0])]))
                self.assertIn('직접 첨부 점검', app.preflight_problem.call_args.args[0])
                app.start_sending.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('tkinterdnd2'), 'optional TkDnD runtime is not installed')
    def test_hidden_real_tk_loads_tkdnd_and_registers_a_drop_target(self):
        # This creates only our own hidden test window; no desktop manipulation,
        # physical drag, Kakao connection, network request or send takes place.
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        root.withdraw()
        self.addCleanup(root.destroy)
        panel = tk.Frame(root)
        entry = tk.Entry(panel)
        label = tk.Label(panel, text='파일을 놓으세요')
        nested = tk.Frame(panel)
        listing = tk.Listbox(nested)
        button = tk.Button(nested, text='경로 적용')
        outside = tk.Listbox(root)
        callback = Mock(return_value='copy')
        enabled, hint = enable_file_drop(root, [panel, entry], callback)
        self.assertTrue(enabled, hint)
        self.assertTrue(root.tk.call('package', 'present', 'tkdnd'))
        for widget in (panel, entry, label, nested, listing, button):
            self.assertTrue(widget.dnd_bind('<<Drop>>'), str(widget))
        self.assertFalse(outside.dnd_bind('<<Drop>>'))
        self.assertFalse(root.tk.call('bind', str(root), '<<Drop>>'))
        callback.assert_not_called()
        root.update_idletasks()


if __name__ == '__main__':
    unittest.main()
