from pathlib import Path
import queue
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kakao_document import load_document, plan_clipboard_paste
from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp


class DocumentTransactionsTests(TestCase):
    def test_invalid_later_row_keeps_document_results_and_undo_intact(self):
        original = [MessageRow(2, "old-A", "old body"), MessageRow(3, "old-B", "body")]
        app = SimpleNamespace(
            rows=list(original), file_path=None, results=[{"status": "sent"}],
            preview_edit_locked=lambda: False,
            clipboard_get=lambda: "new-A\tchanged\t\t\nnew-B\tbody\t\tinvalid-date",
            selected_row_indexes=lambda: [0], push_undo_snapshot=Mock(), finish_preview_mutation=Mock(),
        )
        app.next_preview_row_number = lambda: KakaoSenderApp.next_preview_row_number(app)
        with patch("kakao_sender_gui.messagebox.showwarning"):
            KakaoSenderApp.paste_rows_from_clipboard(app)
        self.assertEqual(app.rows, original)
        self.assertEqual(app.results, [{"status": "sent"}])
        app.push_undo_snapshot.assert_not_called()
        app.finish_preview_mutation.assert_not_called()

    def test_success_commits_one_snapshot_after_all_rows_validate(self):
        original = [MessageRow(2, "old", "body")]
        app = SimpleNamespace(rows=list(original), file_path=None,
            preview_edit_locked=lambda: False, clipboard_get=lambda: "new\ttext\nextra\tmore",
            selected_row_indexes=lambda: [0], finish_preview_mutation=Mock())
        snapshots = []
        app.next_preview_row_number = lambda: KakaoSenderApp.next_preview_row_number(app)
        app.push_undo_snapshot = lambda: snapshots.append(list(app.rows))
        KakaoSenderApp.paste_rows_from_clipboard(app)
        self.assertEqual(snapshots, [original])
        self.assertEqual([r.chat_name for r in app.rows], ["new", "extra"])
        self.assertEqual([r.row_number for r in app.rows], [2, 3])
        self.assertTrue(app.rows[1].image_bundle)
        app.finish_preview_mutation.assert_called_once()

    def test_failed_file_selection_keeps_old_source_rows_results(self):
        old = Path("old.csv")
        rows = [MessageRow(2, "old", "body")]
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.file_path = old
        app.rows = list(rows)
        app.file_var = Mock()
        app.results = [{"status": "sent"}]
        app.document_change_locked = lambda: False
        app.cancel_auto_chat_matches = Mock()
        app.log = Mock()
        app.status_var = Mock()
        app.set_document_loading = Mock()
        app.ui_queue = queue.Queue()
        with patch("kakao_sender_gui.filedialog.askopenfilename", return_value="broken.xlsx"), \
             patch("kakao_sender_gui.load_messages", side_effect=ValueError("broken workbook")), \
             patch("kakao_sender_gui.messagebox.showerror"):
            KakaoSenderApp.select_file(app)
            event, payload = app.ui_queue.get(timeout=3)
            self.assertEqual(event, "document_load_result")
            app.handle_document_load_result(payload)
        self.assertFalse(app._document_loading)
        self.assertEqual(app.file_path, old)
        self.assertEqual(app.rows, rows)
        self.assertEqual(app.results, [{"status": "sent"}])
        app.file_var.set.assert_not_called()
        app.cancel_auto_chat_matches.assert_not_called()

    def test_relative_attachment_resolves_against_committed_source(self):
        base = Path.cwd() / "examples"
        rows, count = plan_clipboard_paste([], "A\tbody\tdoc.pdf", 0, base_dir=base)
        self.assertEqual(count, 1)
        self.assertEqual(rows[0].attachments, ((base / "doc.pdf").resolve(),))

    def test_paste_new_rows_respects_retired_recovery_row_numbers(self):
        rows, _ = plan_clipboard_paste([MessageRow(2, "existing", "body")], "new\tbody", 1,
                                      next_row_number=42)
        self.assertEqual([row.row_number for row in rows], [2, 42])
