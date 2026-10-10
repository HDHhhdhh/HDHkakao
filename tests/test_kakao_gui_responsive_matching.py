"""Manual chooser rendering uses synthetic Tk ports and verified fake rooms."""
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import kakao_gui_matching as matching
from kakao_sender import MessageRow


class Var:
    def __init__(self, value=""):
        self.value, self.traces = value, []
        self.owner = threading.get_ident()

    def get(self):
        assert self.owner == threading.get_ident()
        return self.value

    def set(self, value):
        assert self.owner == threading.get_ident()
        self.value = value
        for callback in self.traces:
            callback("variable", "", "write")

    def trace_add(self, mode, callback):
        self.traces.append(callback)


class Widget:
    def __init__(self, kind, *args, **options):
        self.kind, self.options = kind, options
        self.owner = threading.get_ident()
        self.alive, self.disabled = True, options.get("state") == "disabled"
        self.bindings, self.protocols, self.pending = {}, {}, {}
        self.rows, self.selected = {}, ()
        self.next_id = 0

    def check(self):
        assert self.owner == threading.get_ident(), "Tk port accessed off owner thread"
        assert self.alive, "closed Tk port accessed"

    def title(self, *args): self.check()
    def transient(self, *args): self.check()
    def geometry(self, *args): self.check()
    def minsize(self, *args): self.check()
    def columnconfigure(self, *args, **kwargs): self.check()
    def rowconfigure(self, *args, **kwargs): self.check()
    def grab_set(self): self.check()
    def grid(self, *args, **kwargs): self.check()
    def pack(self, *args, **kwargs): self.check()
    def heading(self, *args, **kwargs): self.check()
    def column(self, *args, **kwargs): self.check()
    def yview(self, *args): self.check()
    def set(self, *args): self.check()
    def focus_set(self): self.check()

    def configure(self, **options):
        self.check()
        self.options.update(options)

    def state(self, states):
        self.check()
        self.disabled = "disabled" in states

    def bind(self, event, callback, **options):
        self.check()
        self.bindings[event] = callback

    def protocol(self, event, callback):
        self.check()
        self.protocols[event] = callback

    def after(self, delay, callback):
        self.check()
        self.next_id += 1
        identifier = "after-" + str(self.next_id)
        self.pending[identifier] = (delay, callback)
        return identifier

    def after_cancel(self, identifier):
        self.check()
        self.pending.pop(identifier, None)

    def run_next(self):
        self.check()
        identifier = min(self.pending, key=lambda key: self.pending[key][0])
        _, callback = self.pending.pop(identifier)
        callback()

    def drain(self):
        while self.pending:
            self.run_next()

    def get_children(self):
        self.check()
        return tuple(self.rows)

    def insert(self, *args, **options):
        self.check()
        self.next_id += 1
        identifier = "item-" + str(self.next_id)
        self.rows[identifier] = options["values"]
        return identifier

    def delete(self, *identifiers):
        self.check()
        for identifier in identifiers:
            self.rows.pop(identifier, None)
        self.selected = tuple(identifier for identifier in self.selected if identifier in self.rows)

    def selection(self):
        self.check()
        return self.selected

    def selection_remove(self, *identifiers):
        self.check()
        self.selected = tuple(identifier for identifier in self.selected if identifier not in identifiers)

    def winfo_exists(self):
        assert self.owner == threading.get_ident()
        return self.alive

    def destroy(self):
        self.check()
        if "<Destroy>" in self.bindings:
            self.bindings["<Destroy>"](SimpleNamespace(widget=self))
        self.alive = False


def verified_rooms(count=420):
    return tuple(dict(chat_id=str(i + 101), type="DirectChat", active_members=2,
                      title=f"synthetic-{i}", peer_user_id=str(10000 + i), peer_names=[f"synthetic-{i}"])
                 for i in range(count))


class App(matching.HubChatMatchingMixin):
    def __init__(self):
        self.rows = [MessageRow(2, "Target A", "synthetic body"), MessageRow(3, "Target B", "synthetic body")]
        self.direct_mappings, self.direct_mapping_labels = {}, {}
        self.status_var = Var()
        self.log = Mock()
        self.manual_mapping_unresolved_names = Mock(return_value=("Target A", "Target B"))
        self.request_auto_chat_matches = Mock(return_value=True)
        self.commit_manual_chat_mapping = Mock(return_value=(True, ""))
        self.after_idle = Mock()


class ResponsiveMatchingTests(unittest.TestCase):
    def setUp(self):
        self.widgets = []
        def factory(kind):
            def create(*args, **options):
                widget = Widget(kind, *args, **options)
                self.widgets.append(widget)
                return widget
            return create
        patches = [patch.object(matching.tk, "Toplevel", factory("Toplevel")), patch.object(matching.tk, "StringVar", Var)]
        patches.extend(patch.object(matching.ttk, kind, factory(kind))
                       for kind in ("Label", "Frame", "Combobox", "Entry", "Treeview", "Scrollbar", "Button"))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def open(self, app=None, rooms=None):
        app = app or App()
        rooms = rooms or verified_rooms()
        app._latest_chat_snapshot = rooms
        app.show_manual_chat_mapping_dialog(rooms)
        dialog = next(widget for widget in self.widgets if widget.kind == "Toplevel")
        tree = next(widget for widget in self.widgets if widget.kind == "Treeview")
        combo = next(widget for widget in self.widgets if widget.kind == "Combobox")
        search = next(widget.options["textvariable"] for widget in self.widgets if widget.kind == "Entry")
        connect = next(widget for widget in self.widgets if widget.kind == "Button" and widget.options["text"] == "선택 연결")
        return app, dialog, tree, combo, search, connect

    def test_initial_render_yields_and_partial_list_cannot_connect(self):
        app, dialog, tree, _combo, _search, connect = self.open()
        self.assertEqual(tree.rows, {})
        self.assertTrue(connect.disabled)
        dialog.run_next()
        self.assertGreater(len(tree.rows), 0)
        self.assertLessEqual(len(tree.rows), 150)
        self.assertTrue(connect.disabled)
        tree.selected = (next(iter(tree.rows)),)
        connect.options["command"]()
        app.request_auto_chat_matches.assert_not_called()
        app.commit_manual_chat_mapping.assert_not_called()
        dialog.drain()
        self.assertEqual(len(tree.rows), 420)
        self.assertFalse(connect.disabled)

    def test_fast_typing_runs_one_debounced_search_for_latest_text(self):
        with patch.object(matching, "manual_chat_candidates", wraps=matching.manual_chat_candidates) as build:
            _app, dialog, tree, _combo, search, connect = self.open()
            search.set("synthetic-1")
            search.set("synthetic-12")
            self.assertEqual(len(dialog.pending), 1)
            self.assertEqual(next(iter(dialog.pending.values()))[0], 120)
            build.assert_not_called()
            dialog.drain()
            build.assert_called_once()
        self.assertEqual(len(tree.rows), 11)
        self.assertTrue(all("synthetic-12" in values[0] for values in tree.rows.values()))
        self.assertFalse(connect.disabled)
        self.assertEqual(tree.selection(), ())

    def test_slow_insert_budget_yields_before_row_limit(self):
        _app, dialog, tree, _combo, _search, connect = self.open()
        ticks = iter(index * 0.003 for index in range(100))
        with patch.object(matching, "time", SimpleNamespace(monotonic=lambda: next(ticks))):
            dialog.run_next()
        self.assertEqual(len(tree.rows), 3)
        self.assertEqual(len(dialog.pending), 1)
        self.assertTrue(connect.disabled)

    def test_filter_reuses_candidates_but_ownership_or_rows_changes_rebuild_them(self):
        with patch.object(matching, "manual_chat_candidates", wraps=matching.manual_chat_candidates) as build:
            app, dialog, _tree, _combo, search, _connect = self.open()
            dialog.drain()
            search.set("synthetic-1")
            dialog.drain()
            self.assertEqual(build.call_count, 1)
            app.direct_mappings = {"different-owner": "101"}
            search.set("")
            dialog.drain()
            self.assertEqual(build.call_count, 2)
            app.rows.append(MessageRow(4, "another current target", "synthetic body"))
            search.set("synthetic-2")
            dialog.drain()
            self.assertEqual(build.call_count, 3)

    def test_stale_render_callback_cannot_insert_rows_for_previous_filter(self):
        _app, dialog, tree, _combo, search, _connect = self.open()
        dialog.run_next()
        stale = next(iter(dialog.pending.values()))[1]
        search.set("synthetic-419")
        before = dict(tree.rows)
        stale()
        self.assertEqual(tree.rows, before)
        dialog.drain()
        self.assertEqual([values[0] for values in tree.rows.values()], ["synthetic-419"])

    def test_target_change_clears_old_selection_and_blocks_connection_until_ready(self):
        app, dialog, tree, combo, _search, connect = self.open()
        dialog.drain()
        tree.selected = (next(iter(tree.rows)),)
        combo.options["textvariable"].set("Target B")
        combo.bindings["<<ComboboxSelected>>"]()
        self.assertEqual(tree.selection(), ())
        self.assertTrue(connect.disabled)
        connect.options["command"]()
        app.request_auto_chat_matches.assert_not_called()
        dialog.drain()
        self.assertEqual(tree.selection(), ())

    def test_closed_dialog_cancels_pending_search_and_stale_callback_touches_no_widgets(self):
        _app, dialog, _tree, _combo, search, _connect = self.open()
        search.set("synthetic")
        stale = next(iter(dialog.pending.values()))[1]
        dialog.destroy()
        self.assertEqual(dialog.pending, {})
        stale()

    def test_confirmed_choice_still_requires_fresh_validation_and_next_target_renders_once(self):
        app = App()
        app.manual_mapping_unresolved_names.side_effect = [("Target A", "Target B"), ("Target B",)]
        app, dialog, tree, _combo, _search, connect = self.open(app, verified_rooms(2))
        dialog.drain()
        tree.selected = (next(iter(tree.rows)),)
        with patch.object(matching.messagebox, "askyesno", return_value=True):
            connect.options["command"]()
        app.commit_manual_chat_mapping.assert_not_called()
        self.assertEqual(app.request_auto_chat_matches.call_args.kwargs["reason"], "manual_validate")
        self.assertTrue(connect.disabled)
        callback = app.request_auto_chat_matches.call_args.kwargs["on_complete"]
        callback(True)
        app.commit_manual_chat_mapping.assert_called_once()
        self.assertEqual(app.commit_manual_chat_mapping.call_args.args[0], "Target A")
        self.assertEqual(len(dialog.pending), 1)
        self.assertEqual(next(iter(dialog.pending.values()))[0], 120)
        self.assertTrue(connect.disabled)
        self.assertEqual(tree.selection(), ())
        dialog.drain()

    def test_selection_changed_during_confirmation_cannot_start_validation_for_old_choice(self):
        app, dialog, tree, _combo, search, connect = self.open(rooms=verified_rooms(2))
        dialog.drain()
        tree.selected = (next(iter(tree.rows)),)
        def answer(*args, **kwargs):
            search.set("synthetic-1")
            return True
        with patch.object(matching.messagebox, "askyesno", side_effect=answer):
            connect.options["command"]()
        app.request_auto_chat_matches.assert_not_called()
        app.commit_manual_chat_mapping.assert_not_called()


if __name__ == "__main__":
    unittest.main()
