"""Slow synthetic journals must not hold the Tk owner thread."""
import queue
import threading
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import kakao_gui_panels as panels
from kakao_sender import MessageRow


class OwnerVar:
    def __init__(self, value=""):
        self.owner = threading.get_ident()
        self.value = value

    def get(self):
        assert threading.get_ident() == self.owner, "worker read a Tk variable"
        return self.value

    def set(self, value):
        assert threading.get_ident() == self.owner, "worker wrote a Tk variable"
        self.value = value


class OwnerWidget:
    def __init__(self, *args, **kwargs):
        self.owner = threading.get_ident()
        self.options = kwargs
        self.alive = True
        self.callbacks = []
        self.bindings = {}
        self.rows = []
        self.text = ""
        self.disabled = kwargs.get("state") == "disabled"

    def check(self):
        assert threading.get_ident() == self.owner, "worker touched a Tk widget"

    def title(self, value): self.check()
    def geometry(self, value): self.check()
    def transient(self, value): self.check()
    def columnconfigure(self, *args, **kwargs): self.check()
    def rowconfigure(self, *args, **kwargs): self.check()
    def heading(self, *args, **kwargs): self.check()
    def column(self, *args, **kwargs): self.check()
    def pack(self, *args, **kwargs): self.check()
    def grid(self, *args, **kwargs): self.check()
    def lift(self): self.check()

    def bind(self, event, callback, **kwargs):
        self.check()
        self.bindings[event] = callback

    def configure(self, **kwargs):
        self.check()
        self.options.update(kwargs)

    def state(self, values):
        self.check()
        self.disabled = "disabled" in values

    def get_children(self):
        self.check()
        return tuple(range(len(self.rows)))

    def delete(self, *args):
        self.check()
        self.rows.clear()
        self.text = ""

    def insert(self, *args, **kwargs):
        self.check()
        if "values" in kwargs:
            self.rows.append(kwargs["values"])
        else:
            self.text += args[-1]

    def winfo_exists(self):
        self.check()
        return self.alive

    def after(self, delay, callback):
        self.check()
        self.callbacks.append(callback)

    def destroy(self):
        self.check()
        self.alive = False
        if "<Destroy>" in self.bindings:
            self.bindings["<Destroy>"](SimpleNamespace(widget=self))


class PanelApp(panels.HubRunPanelsMixin):
    def __init__(self, journal=None):
        self.run_journal = journal
        self.status_var = OwnerVar("준비")
        self.log = Mock()
        self.events = queue.Queue()
        self.post = lambda event, payload: self.events.put((event, payload))
        self.direct_config = SimpleNamespace(node_path="synthetic-node", mappings={})
        self.config_data = {"search_box": {"x": 12, "y": 34}}

    def receive(self):
        event, payload = self.events.get(timeout=3)
        if event == "run_history_result":
            self.handle_run_history_result(payload)
        elif event == "recovery_offer_result":
            self.handle_recovery_offer_result(payload)
        elif event == "diagnostic_result":
            self.handle_diagnostic_result(payload)
        else:
            raise AssertionError(event)
        return payload


def recovery(count=1):
    return SimpleNamespace(
        run_id="synthetic-run", is_active=False,
        selected=tuple((i, MessageRow(i + 2, f"synthetic-{i}", "synthetic body")) for i in range(count)),
        results=({"row_index": 0, "status": "unknown", "transport": "direct"},),
    )


class ResponsivePanelTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(panels.tk, "Toplevel", OwnerWidget),
                        patch.object(panels.tk, "StringVar", OwnerVar),
                        # Its ttk base was bound at import time; stub the panel label too.
                        patch.object(panels, "KoreanLabel", OwnerWidget),
                        patch.object(panels, "ScrolledText", OwnerWidget)]
        self.patches.extend(patch.object(panels.ttk, name, OwnerWidget)
                            for name in ("Label", "Combobox", "Treeview", "Frame", "Button"))
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def view(self, app, request=1):
        view = dict(token="dialog", request=request, journal=app.run_journal,
                    dialog=OwnerWidget(), table=OwnerWidget(), detail=OwnerVar(),
                    value=OwnerVar("first"), combo=OwnerWidget(), choices={"first": "synthetic-run"},
                    recovery=None, actions=tuple(OwnerWidget(state="disabled") for _ in range(3)),
                    loading=True, cancel=threading.Event())
        app._run_history_view = view
        return view

    def test_history_window_and_loading_state_exist_before_slow_disk_read_finishes(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        owner = threading.get_ident()
        calls = []
        def list_runs():
            calls.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return []
        app = PanelApp(SimpleNamespace(list_runs=list_runs))
        app.open_run_history()
        self.assertTrue(entered.wait(1))
        view = app._run_history_view
        self.assertTrue(view["dialog"].alive)
        self.assertIn("불러오는 중", view["detail"].get())
        self.assertTrue(all(action.disabled for action in view["actions"]))
        self.assertEqual(view["combo"].options["state"], "disabled")
        # A Tk variable remains usable while the synthetic disk read waits.
        app.status_var.set("owner thread is responsive")
        self.assertNotEqual(calls, [owner])
        release.set()
        app.receive()
        self.assertEqual(view["value"].get(), "저장된 실행 없음")
        self.assertFalse(view["loading"])

    def test_history_thread_start_failure_releases_loading_without_enabling_restore(self):
        journal = SimpleNamespace(list_runs=Mock())
        app = PanelApp(journal)
        with patch.object(panels.threading.Thread, "start", side_effect=RuntimeError("synthetic thread unavailable")):
            app.open_run_history()
        view = app._run_history_view
        self.assertFalse(view["loading"])
        self.assertTrue(all(action.disabled for action in view["actions"]))
        self.assertEqual(view["value"].get(), "실행 기록 목록 읽기 실패")
        self.assertIn("작업을 시작하지 못했습니다", view["detail"].get())
        self.assertNotIn("저장된 실행 기록이 없습니다", view["detail"].get())
        self.assertIsNone(view["recovery"])
        journal.list_runs.assert_not_called()
        self.assertTrue(app.events.empty())

    def test_acknowledge_thread_start_failure_preserves_loaded_record_for_retry(self):
        journal = SimpleNamespace(acknowledge=Mock())
        app = PanelApp(journal)
        view = self.view(app, request=0)
        view["recovery"] = loaded = recovery()
        with patch.object(panels.threading.Thread, "start", side_effect=RuntimeError("synthetic thread unavailable")):
            app._request_run_history("acknowledge", "synthetic-run")
        self.assertFalse(view["loading"])
        self.assertIs(view["recovery"], loaded)
        self.assertTrue(all(not action.disabled for action in view["actions"]))
        self.assertEqual(view["combo"].options["state"], "readonly")
        self.assertIn("작업을 시작하지 못했습니다", view["detail"].get())
        self.assertNotIn("확인 완료로 표시했습니다", view["detail"].get())
        journal.acknowledge.assert_not_called()

    def test_history_thread_start_failure_cannot_change_closed_or_newer_request(self):
        for superseded in (False, True):
            with self.subTest(superseded=superseded):
                app = PanelApp(object())
                view = self.view(app, request=0)
                def fail_start():
                    if superseded:
                        view["request"] += 1
                        view["detail"].set("newer request is loading")
                    else:
                        app._run_history_view = None
                        view["dialog"].alive = False
                    raise RuntimeError("synthetic thread unavailable")
                with patch.object(panels.threading.Thread, "start", side_effect=fail_start):
                    app._request_run_history("record", "synthetic-run")
                self.assertTrue(view["loading"])
                if superseded:
                    self.assertEqual(view["detail"].get(), "newer request is loading")
                self.assertTrue(all(action.disabled for action in view["actions"]))

    def test_record_selection_reads_on_worker_and_preserves_unknown_label(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def load(run_id):
            calls.append((threading.get_ident(), run_id))
            entered.set()
            release.wait(3)
            return recovery(2)
        app = PanelApp(SimpleNamespace(load_recovery=load))
        view = self.view(app, request=0)
        app._request_run_history("record", "synthetic-run")
        self.assertTrue(entered.wait(1))
        self.assertIsNone(view["recovery"])
        self.assertTrue(all(action.disabled for action in view["actions"]))
        self.assertEqual(view["combo"].options["state"], "readonly")
        view["detail"].set("owner can still update the window")
        release.set()
        app.receive()
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertEqual(view["table"].rows, [(2, "synthetic-0", "확인 필요", "direct"), (3, "synthetic-1", "미시도", "-")])
        self.assertFalse(view["loading"])
        self.assertTrue(all(not action.disabled for action in view["actions"]))

    def test_large_record_yields_and_previous_render_cannot_fill_new_selection(self):
        app = PanelApp(object())
        view = self.view(app)
        rows = tuple((i, "synthetic", "미시도", "-") for i in range(1000))
        result = panels.RunHistoryResult("dialog", 1, app.run_journal, "record", (recovery(), rows))
        app.handle_run_history_result(result)
        self.assertGreater(len(view["table"].rows), 0)
        self.assertLessEqual(len(view["table"].rows), 150)
        self.assertTrue(view["loading"])
        self.assertTrue(all(action.disabled for action in view["actions"]))
        self.assertEqual(len(view["dialog"].callbacks), 1)
        view["request"] = 2
        view["table"].rows.clear()
        view["dialog"].callbacks.pop()()
        self.assertEqual(view["table"].rows, [])

    def test_large_record_enables_restore_only_after_last_render_chunk(self):
        app = PanelApp(object())
        view = self.view(app)
        rows = tuple((i, "synthetic", "미시도", "-") for i in range(420))
        result = panels.RunHistoryResult("dialog", 1, app.run_journal, "record", (recovery(), rows))
        app.handle_run_history_result(result)
        while view["dialog"].callbacks:
            self.assertTrue(all(action.disabled for action in view["actions"]))
            view["dialog"].callbacks.pop(0)()
        self.assertEqual(tuple(view["table"].rows), rows)
        self.assertFalse(view["loading"])
        self.assertTrue(all(not action.disabled for action in view["actions"]))

    def test_superseded_worker_does_not_post_or_overwrite_new_record(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def load(run_id):
            if run_id == "old":
                entered.set()
                release.wait(3)
            record = recovery()
            record.selected = ((0, MessageRow(2, run_id, "synthetic body")),)
            return record
        app = PanelApp(SimpleNamespace(load_recovery=load))
        view = self.view(app, request=0)
        real_thread_type, threads = threading.Thread, []
        def start_worker(**kwargs):
            worker = real_thread_type(**kwargs)
            threads.append(worker)
            return worker
        with patch.object(panels.threading, "Thread", start_worker):
            app._request_run_history("record", "old")
            self.assertTrue(entered.wait(1))
            app._request_run_history("record", "new")
            app.receive()
            release.set()
            for worker in threads:
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
        self.assertTrue(app.events.empty())
        self.assertEqual(view["table"].rows[0][1], "new")

    def test_closed_replaced_or_stale_record_result_does_not_touch_current_widgets(self):
        app = PanelApp(object())
        view = self.view(app, request=2)
        stale = panels.RunHistoryResult("dialog", 1, app.run_journal, "record", (recovery(), ((2, "old", "미시도", "-"),)))
        app.handle_run_history_result(stale)
        self.assertEqual(view["table"].rows, [])
        app._run_history_view = None
        app.handle_run_history_result(panels.RunHistoryResult("dialog", 2, app.run_journal, "summaries", (("old", "old-run"),)))
        new_view = self.view(app, request=2)
        new_view["token"] = "reopened"
        app.handle_run_history_result(panels.RunHistoryResult("dialog", 2, app.run_journal, "summaries", (("old", "old-run"),)))
        self.assertEqual(new_view["value"].get(), "first")
        new_view["token"] = "dialog"
        app.run_journal = object()
        app.handle_run_history_result(panels.RunHistoryResult("dialog", 2, view["journal"], "summaries", (("old", "old-run"),)))
        self.assertEqual(new_view["value"].get(), "first")

    def test_history_read_error_leaves_restore_disabled_and_shows_error(self):
        app = PanelApp(object())
        view = self.view(app)
        app.handle_run_history_result(panels.RunHistoryResult("dialog", 1, app.run_journal, "record", error="synthetic storage error"))
        self.assertEqual(view["detail"].get(), "synthetic storage error")
        self.assertTrue(all(action.disabled for action in view["actions"]))
        self.assertFalse(view["loading"])

    def test_acknowledge_waits_off_thread_and_does_not_report_success_early(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def acknowledge(run_id):
            calls.append((threading.get_ident(), run_id))
            entered.set()
            release.wait(3)
        app = PanelApp(SimpleNamespace(acknowledge=acknowledge))
        view = self.view(app, request=0)
        view["recovery"] = recovery()
        app._request_run_history("acknowledge", "synthetic-run")
        self.assertTrue(entered.wait(1))
        self.assertIn("저장하는 중", view["detail"].get())
        self.assertEqual(view["combo"].options["state"], "disabled")
        self.assertTrue(all(action.disabled for action in view["actions"]))
        release.set()
        app.receive()
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertIn("확인 완료로 표시", view["detail"].get())

    def test_recovery_scan_is_async_and_cannot_replace_newer_operation_status(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def list_recoverable():
            self.assertNotEqual(threading.get_ident(), owner)
            entered.set()
            release.wait(3)
            return [object(), object()]
        owner = threading.get_ident()
        app = PanelApp(SimpleNamespace(list_recoverable_runs=list_recoverable))
        app.offer_run_recovery()
        self.assertTrue(entered.wait(1))
        app.status_var.set("발송 준비 취소됨")
        release.set()
        app.receive()
        self.assertEqual(app.status_var.get(), "발송 준비 취소됨")
        self.assertIn("2개", app.log.call_args.args[1])

    def test_stale_startup_scan_cannot_log_for_replaced_journal(self):
        app = PanelApp(object())
        app._recovery_offer_token = "latest"
        app._recovery_offer_status = "준비"
        app.handle_recovery_offer_result(panels.RecoveryOfferResult("old", app.run_journal, 3))
        app.handle_recovery_offer_result(panels.RecoveryOfferResult("latest", object(), 3))
        app.log.assert_not_called()
        self.assertEqual(app.status_var.get(), "준비")

    def test_recovery_thread_start_failure_releases_token_and_logs_error(self):
        journal = SimpleNamespace(list_recoverable_runs=Mock())
        app = PanelApp(journal)
        with patch.object(panels.threading.Thread, "start", side_effect=RuntimeError("synthetic thread unavailable")):
            app.offer_run_recovery()
        self.assertIsNone(app._recovery_offer_token)
        self.assertEqual(app.status_var.get(), "준비")
        self.assertIn("점검을 시작하지 못했습니다", app.log.call_args.args[1])
        journal.list_recoverable_runs.assert_not_called()
        self.assertTrue(app.events.empty())

    def test_superseded_recovery_start_failure_cannot_release_newer_token(self):
        app = PanelApp(object())
        def fail_start():
            app._recovery_offer_token = "newer scan"
            raise RuntimeError("synthetic thread unavailable")
        with patch.object(panels.threading.Thread, "start", side_effect=fail_start):
            app.offer_run_recovery()
        self.assertEqual(app._recovery_offer_token, "newer scan")
        app.log.assert_not_called()

    def test_local_diagnostics_use_config_snapshot_and_reject_changed_account_result(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        snapshots = []
        def diagnose(config, pc_config):
            self.assertNotEqual(threading.get_ident(), owner)
            entered.set()
            release.wait(3)
            snapshots.append((config.node_path, dict(config.mappings), pc_config["search_box"]["x"]))
            return (SimpleNamespace(state="ok", label="synthetic local", detail="old account detail"),)
        app = PanelApp()
        owner = threading.get_ident()
        with patch.object(panels, "diagnose_local", diagnose):
            app.open_connection_diagnostics()
            self.assertTrue(entered.wait(1))
            app.direct_config.node_path = "changed-account-node"
            app.direct_config.mappings["new"] = "123"
            app.config_data["search_box"]["x"] = 99
            release.set()
            app.receive()
        self.assertEqual(snapshots, [("synthetic-node", {}, 12)])
        self.assertNotIn("old account detail", app._diagnostic_text.text)
        self.assertIn("연결 설정이 바뀌었습니다", app._diagnostic_text.text)
        self.assertFalse(app._diagnostic_local_ready)
        self.assertTrue(app._diagnostic_check_button.disabled)

    def test_changed_pc_settings_do_not_publish_previous_readiness(self):
        app = PanelApp()
        app._diagnostic_token = "current"
        app._diagnostic_config_snapshot = deepcopy(app.direct_config)
        app._diagnostic_pc_config_snapshot = deepcopy(app.config_data)
        app._diagnostic_dialog = OwnerWidget()
        app._diagnostic_text = OwnerWidget()
        app._diagnostic_check_button = OwnerWidget()
        app.config_data["search_box"]["x"] = 99
        app.handle_diagnostic_result(("current", "previous coordinate readiness", False))
        self.assertNotIn("previous coordinate readiness", app._diagnostic_text.text)
        self.assertFalse(app._diagnostic_local_ready)
        self.assertTrue(app._diagnostic_check_button.disabled)


if __name__ == "__main__":
    unittest.main()
