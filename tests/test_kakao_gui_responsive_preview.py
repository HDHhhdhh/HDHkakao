"""Preview responsiveness checks with deterministic widgets and no Kakao IO."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kakao_sender import MessageRow
from kakao_sender_gui import KakaoSenderApp


class Value:
    def __init__(self, value=None):
        self.value = value
        self.updates = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        self.updates.append(value)


class PreviewTree:
    def __init__(self):
        self.items = {}
        self.selected = ()
        self.writes = []
        self.scrolls = []

    def get_children(self):
        return tuple(self.items)

    def exists(self, iid):
        return iid in self.items

    def delete(self, *items):
        self.writes.append(('delete', items))
        for iid in items:
            self.items.pop(iid)
        self.selected = tuple(iid for iid in self.selected if iid in self.items)

    def insert(self, _parent, _where, *, iid, values, tags):
        self.items[iid] = {'values': tuple(values), 'tags': tags}
        self.writes.append(('insert', iid))

    def item(self, iid, option=None, **updates):
        if updates:
            self.items[iid].update(updates)
            self.writes.append(('item', iid))
        return self.items[iid][option] if option else self.items[iid]

    def set(self, iid, _column, value):
        values = list(self.items[iid]['values'])
        values[6] = value
        self.items[iid]['values'] = tuple(values)
        self.writes.append(('route', iid))

    def selection(self):
        return self.selected

    def selection_set(self, *items):
        self.selected = items

    def focus(self, iid):
        pass

    def see(self, iid):
        self.scrolls.append(iid)


def preview_app(count=1000):
    app = KakaoSenderApp.__new__(KakaoSenderApp)
    app.rows = [MessageRow(i + 2, f'fixture-{i}', f'message {i}') for i in range(count)]
    app.tree = PreviewTree()
    app.results = []
    app.active_run = None
    app.route_summary_var = Value()
    app.direct_enabled_var = Value(True)
    app.direct_mappings = {row.chat_name: str(i + 1) for i, row in enumerate(app.rows)}
    app.common_schedule_datetime = lambda: None
    app.refresh_attachment_panel = Mock()
    app.search_var = Value('message')
    app.search_match_indexes = []
    app.search_match_position = -1
    return app


class ResponsivePreviewTests(unittest.TestCase):
    def test_one_cell_edit_updates_only_its_row(self):
        app = preview_app()
        app.populate_table()
        app.tree.writes.clear()
        app.rows[537] = replace(app.rows[537], text='edited')
        app.populate_table()
        self.assertEqual(app.tree.writes, [('item', '537'), ('route', '537')])
        self.assertEqual(app.tree.item('537', 'values')[2], 'edited')
        self.assertEqual(len(app.tree.items), 1000)

    def test_previous_result_colors_reset_without_rebuilding_other_rows(self):
        app = preview_app(5)
        app.populate_table()
        app.update_row_status(3, '직접 전송')
        app.tree.writes.clear()
        app.populate_table()
        self.assertEqual(app.tree.item('3', 'values')[5], '대기')
        self.assertEqual(app.tree.item('3', 'tags'), ('ready',))
        self.assertNotIn(('insert', '0'), app.tree.writes)
        self.assertNotIn(('item', '0'), app.tree.writes)

    def test_delete_shift_and_undo_keep_row_numbers_and_route_cells(self):
        app = preview_app(5)
        original = list(app.rows)
        app.populate_table()
        del app.rows[1]
        app.populate_table()
        self.assertEqual(tuple(app.tree.items), ('0', '1', '2', '3'))
        self.assertEqual([app.tree.item(str(i), 'values')[0] for i in range(4)], [2, 4, 5, 6])
        app.rows = original
        app.populate_table()
        self.assertEqual([app.tree.item(str(i), 'values')[0] for i in range(5)], [2, 3, 4, 5, 6])
        self.assertTrue(all(app.tree.item(str(i), 'values')[6] == '직접' for i in range(5)))

    def test_repeated_search_reuses_text_and_edit_refreshes_only_changed_text(self):
        app = preview_app()
        with patch.object(app, 'preview_search_text', wraps=app.preview_search_text) as search_text:
            self.assertEqual(len(app.refresh_search_matches()), 1000)
            self.assertEqual(search_text.call_count, 1000)
            for _ in range(20):
                app.refresh_search_matches()
            self.assertEqual(search_text.call_count, 1000)
            app.rows[13] = replace(app.rows[13], text='different')
            self.assertEqual(len(app.refresh_search_matches()), 999)
            self.assertEqual(search_text.call_count, 1001)
            app.search_var.set('different')
            self.assertEqual(app.refresh_search_matches(), [13])

    def test_search_matches_full_text_and_path_not_only_truncated_preview(self):
        app = preview_app(1)
        app.rows[0] = replace(app.rows[0], text='x' * 130 + 'needle', attachments=(Path('archive.pdf'),))
        app.search_var.set('needle')
        self.assertEqual(app.refresh_search_matches(), [0])
        app.search_var.set('archive.pdf')
        self.assertEqual(app.refresh_search_matches(), [0])

    def test_clear_search_and_enter_same_query_restores_matches(self):
        app = preview_app(3)
        self.assertEqual(app.refresh_search_matches(), [0, 1, 2])
        app.search_var.set('')
        self.assertEqual(app.refresh_search_matches(), [])
        app.search_var.set('message')
        self.assertEqual(app.refresh_search_matches(), [0, 1, 2])

    def test_close_search_and_reopen_same_query_restores_matches(self):
        app = preview_app(3)
        app.search_frame = Mock()
        app.tree.focus_set = Mock()
        self.assertEqual(app.refresh_search_matches(), [0, 1, 2])
        app.close_search_bar()
        app.search_var.set('message')
        self.assertEqual(app.refresh_search_matches(), [0, 1, 2])

    def test_selection_does_not_rebuild_shared_attachments_or_overwrite_draft(self):
        app = preview_app(2)
        app.populate_table()
        app.refresh_attachment_panel.reset_mock()
        app.on_preview_selection_changed()
        app.refresh_attachment_panel.assert_not_called()

    def test_feedback_batches_log_scroll_and_route_summary_without_losing_results(self):
        app = preview_app(3)
        app.populate_table()
        app.log_text = Mock()
        app.route_summary_var.updates.clear()
        app._feedback_batch_active = True
        with patch('kakao_sender_gui.time.strftime', return_value='12:00:00'):
            for i in range(3):
                app.log('검사', f'event {i}')
                app.update_row_status(i, '직접 전송')
                app.refresh_result_delivery_route({'row_index': i, 'transport': 'direct'})
        app.log_text.insert.assert_not_called()
        self.assertEqual(app.tree.scrolls, [])
        self.assertEqual(app.route_summary_var.updates, [])
        app.flush_pending_visual_feedback()
        app.log_text.insert.assert_called_once_with('end', ''.join(f'[12:00:00] [검사] event {i}\n' for i in range(3)))
        self.assertEqual(app.tree.scrolls, ['2'])
        self.assertEqual(app.route_summary_var.updates, ['직접 3건'])
        self.assertTrue(all(app.tree.item(str(i), 'values')[5] == '직접 전송' for i in range(3)))

    def test_loading_blocks_send_and_stop_cancels_before_any_send_action(self):
        app = preview_app(1)
        app._document_loading = True
        app.cancel_document_load = Mock()
        app.status_var = Value()
        app.log = Mock()
        app.filtered_rows = Mock()
        with patch('kakao_sender_gui.messagebox.showinfo') as info:
            app.start_sending()
            info.assert_called_once()
        app.filtered_rows.assert_not_called()
        app.stop_sending()
        app.cancel_document_load.assert_called_once()
        self.assertIn('파일 읽기 취소', app.status_var.get())

    def test_layout_duplicate_configurations_are_skipped_but_changed_values_apply(self):
        app = preview_app(1)
        widget = Mock()
        app._configure_layout_widget(widget, wraplength=300, padding=(2, 4))
        app._configure_layout_widget(widget, wraplength=300, padding=(2, 4))
        app._configure_layout_widget(widget, wraplength=320, padding=(2, 4))
        self.assertEqual(widget.configure.call_count, 2)
        widget.configure.assert_called_with(wraplength=320)


if __name__ == '__main__':
    unittest.main()
