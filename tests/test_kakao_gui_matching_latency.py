"""Saved-target matching must reduce reads without trusting cached identities."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import kakao_gui_matching as matching
from kakao_direct import DirectBridgeError
from kakao_mapping_policy import normalize_chat_match_name
from kakao_sender_gui import KakaoSenderApp


def direct_chat(chat_id, label):
    return {
        "chat_id": str(chat_id),
        "type": "DirectChat",
        "active_members": 2,
        "title": label,
        "display_name": label,
        "peer_user_id": str(10000 + int(chat_id)),
        "peer_names": [label],
    }


class ReadBridge:
    def __init__(self, lookup_rows=(), catalog_rows=(), lookup_error=None):
        self.lookup_rows = list(lookup_rows)
        self.catalog_rows = list(catalog_rows)
        self.lookup_error = lookup_error
        self.calls = []

    def ready(self):
        self.calls.append(("ready",))

    def list_chats(self):
        self.calls.append(("list",))
        return [dict(row) for row in self.catalog_rows]

    def get_chats_by_ids(self, chat_ids):
        batch = tuple(chat_ids)
        self.calls.append(("lookup", batch))
        if self.lookup_error is not None:
            raise self.lookup_error
        return [dict(row) for row in self.lookup_rows if row["chat_id"] in batch]

    def close(self):
        self.calls.append(("close",))


class SavedTargetLookupPolicyTests(unittest.TestCase):
    def test_only_complete_confirmed_unique_numeric_saved_targets_are_eligible(self):
        valid = {"김민수": "101", "이서연": "202", "다른 대상": "303"}
        labels = {"101": "김민수", "202": "이서연", "303": "다른 대상"}
        self.assertEqual(
            matching.saved_target_lookup_ids(("김민수", "이서연"), valid, labels),
            ("101", "202"),
        )
        invalid_cases = (
            ({"김민수": "101"}, labels),
            ({**valid, "다른 대상": "101"}, labels),
            ({**valid, "김민수": "opaque-id"}, labels),
            ({**valid, "김민수": "0"}, labels),
            (valid, {"101": "김민수"}),
            (valid, {**labels, "101": "채팅 101"}),
        )
        for mappings, saved_labels in invalid_cases:
            with self.subTest(mappings=mappings, labels=saved_labels):
                self.assertEqual(
                    matching.saved_target_lookup_ids(("김민수", "이서연"), mappings, saved_labels),
                    (),
                )

    def test_fresh_confirmation_requires_exact_rows_and_verified_stable_labels(self):
        names = ("김민수", "이서연")
        mappings = {"김민수": "101", "이서연": "202"}
        labels = {"101": "김민수", "202": "이서연"}
        chats = [direct_chat("101", "김민수"), direct_chat("202", "이서연")]
        self.assertTrue(matching.saved_target_lookup_is_complete(names, mappings, labels, chats))
        changed = dict(chats[0], title="다른 사람", display_name="다른 사람", peer_names=["다른 사람"])
        group = dict(chats[0], type="MultiChat", active_members=3)
        unverified = dict(chats[0], peer_user_id="", peer_names=[])
        inactive = dict(chats[0], active_members=1)
        unsafe_snapshots = (
            chats[:1],
            [changed, chats[1]],
            [group, chats[1]],
            [unverified, chats[1]],
            [inactive, chats[1]],
            [*chats, dict(chats[0])],
            [dict(chats[0], title="이서연", display_name="이서연"), chats[1]],
        )
        for snapshot in unsafe_snapshots:
            with self.subTest(snapshot=snapshot):
                self.assertFalse(
                    matching.saved_target_lookup_is_complete(names, mappings, labels, snapshot)
                )


class AutoMatchingLatencyTests(unittest.TestCase):
    def make_app(self, bridge, mappings=None, labels=None):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_mappings = dict(mappings or {"김민수": "101"})
        app.direct_mapping_labels = dict(labels or {"101": "김민수"})
        app.guarded_mapping_ids = ()
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True})
        app.create_direct_bridge = Mock(return_value=bridge)
        app.save_direct_settings = Mock()
        app.statuses = []
        app.logs = []
        app.status_var = SimpleNamespace(set=app.statuses.append)
        app.log = lambda *args: app.logs.append(args)
        app.posts = []
        app.finished = threading.Event()
        app.post = lambda event, payload: (app.posts.append((event, payload)), app.finished.set())
        return app

    def query(self, app, names=("김민수",), reason="file_load"):
        with (
            patch("kakao_gui_matching.discover_open_pc_chat_ids", side_effect=AssertionError("PC windows")),
            patch("kakao_gui_matching.discover_pc_chat_catalog_ids", side_effect=AssertionError("PC catalog")),
            patch("kakao_gui_matching.resolve_pc_chat_ids_via_window", side_effect=AssertionError("PC UI")),
        ):
            self.assertTrue(app.request_auto_chat_matches(names, reason=reason))
            self.assertTrue(app.finished.wait(3), "matching worker did not finish")
        self.assertEqual(app.posts[-1][0], "auto_match_result")
        return app.posts[-1][1]

    def test_confirmed_saved_targets_query_only_current_ids_and_keep_other_saved_mappings(self):
        fresh = direct_chat("101", "김민수")
        bridge = ReadBridge([fresh], [fresh, direct_chat("202", "관련 없는 방")])
        app = self.make_app(
            bridge,
            {"김민수": "101", "관련 없는 방": "202"},
            {"101": "김민수", "202": "관련 없는 방"},
        )
        payload = self.query(app)

        self.assertEqual([call for call in bridge.calls if call[0] in {"list", "lookup"}], [("lookup", ("101",))])
        self.assertIsNone(payload[4])
        self.assertEqual(payload[3], [fresh])
        self.assertTrue(any("저장 매칭 1/1개" in status and "서버 확인 중" in status for status in app.statuses))
        app.handle_auto_chat_match_result(payload)
        self.assertEqual(app.direct_mappings, {"김민수": "101", "관련 없는 방": "202"})
        self.assertEqual(app.direct_mapping_labels, {"101": "김민수", "202": "관련 없는 방"})
        app.save_direct_settings.assert_not_called()

    def test_mixed_new_target_uses_full_catalog_without_trying_saved_fast_path(self):
        fresh = direct_chat("101", "김민수")
        new = direct_chat("202", "이서연")
        bridge = ReadBridge([fresh], [fresh, new])
        app = self.make_app(bridge)
        payload = self.query(app, names=("김민수", "이서연"))
        self.assertEqual([call for call in bridge.calls if call[0] in {"list", "lookup"}], [("list",)])
        self.assertEqual(payload[3], [fresh, new])
        self.assertIsNone(payload[4])

    def test_incomplete_or_unsafe_fast_snapshot_falls_back_to_full_current_catalog(self):
        fresh = direct_chat("101", "김민수")
        snapshots = (
            [],
            [dict(fresh, title="다른 사람", display_name="다른 사람", peer_names=["다른 사람"])],
            [dict(fresh, type="MultiChat", active_members=3)],
            [dict(fresh, peer_user_id="", peer_names=[])],
            [fresh, dict(fresh)],
        )
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                bridge = ReadBridge(snapshot, [fresh])
                app = self.make_app(bridge)
                payload = self.query(app)
                self.assertEqual(
                    [call for call in bridge.calls if call[0] in {"list", "lookup"}],
                    [("lookup", ("101",)), ("list",)],
                )
                self.assertEqual(payload[3], [fresh])
                self.assertIsNone(payload[4])

    def test_fast_lookup_errors_fail_safely_without_retry_or_cache_success(self):
        for code in ("refresh_rejected", "login_failed", "request_timeout", "transport_closed"):
            with self.subTest(code=code):
                fresh = direct_chat("101", "김민수")
                bridge = ReadBridge([fresh], [fresh], DirectBridgeError("private secret", code=code))
                app = self.make_app(bridge)
                payload = self.query(app)
                self.assertIsNotNone(payload[4])
                self.assertEqual(payload[4].code, code)
                self.assertNotIn("private secret", str(payload))
                self.assertNotIn(("list",), bridge.calls)
                app.handle_auto_chat_match_result(payload)
                app.save_direct_settings.assert_not_called()
                self.assertEqual(app.direct_mappings, {"김민수": "101"})

    def test_large_roster_lookup_is_chunked_and_covers_all_requested_ids(self):
        names = tuple(f"검증 대상 {index}" for index in range(1, 260))
        mappings = {normalize_chat_match_name(name): str(index) for index, name in enumerate(names, 1)}
        labels = {str(index): name for index, name in enumerate(names, 1)}
        rows = [direct_chat(chat_id, label) for chat_id, label in labels.items()]
        bridge = ReadBridge(rows, rows)
        app = self.make_app(bridge, mappings, labels)
        payload = self.query(app, names=names)
        calls = [call[1] for call in bridge.calls if call[0] == "lookup"]
        self.assertEqual([len(batch) for batch in calls], [128, 128, 3])
        self.assertEqual(tuple(chat_id for batch in calls for chat_id in batch), tuple(labels))
        self.assertNotIn(("list",), bridge.calls)
        self.assertEqual(len(payload[3]), 259)
        self.assertIsNone(payload[4])

    def test_before_send_still_uses_full_fresh_catalog(self):
        fresh = direct_chat("101", "김민수")
        bridge = ReadBridge([fresh], [fresh])
        app = self.make_app(bridge)
        payload = self.query(app, reason="before_send")
        self.assertEqual([call for call in bridge.calls if call[0] in {"list", "lookup"}], [("list",)])
        self.assertEqual(payload[3], [fresh])

    def test_metrics_report_real_stages_without_name_or_message_payloads(self):
        fresh = direct_chat("101", "김민수")
        bridge = ReadBridge([fresh], [fresh])
        app = self.make_app(bridge)
        payload = self.query(app)
        self.assertEqual(len(payload), 12)
        metrics = payload[11]
        self.assertIsInstance(metrics, matching.AutoMatchMetrics)
        self.assertEqual(metrics.target_count, 1)
        self.assertEqual(metrics.mode, "saved_ids")
        self.assertEqual(metrics.lookup_ids, 1)
        self.assertEqual(metrics.catalog_count, 1)
        self.assertNotIn("김민수", repr(metrics))
        for name in ("connect_seconds", "list_seconds", "lookup_seconds", "close_seconds", "worker_seconds"):
            self.assertGreaterEqual(getattr(metrics, name), 0)
        app.handle_auto_chat_match_result(payload)
        self.assertTrue(any("file_load" in text for _category, text in app.logs))

    def test_exact_phase_timings_include_connection_fallback_close_queue_and_apply(self):
        class MonotonicClock:
            value = 100.0

            def advance(self, seconds):
                self.value += seconds

            def read(self):
                return self.value

        clock = MonotonicClock()
        fresh = direct_chat("101", "김민수")

        class TimedBridge(ReadBridge):
            def ready(self):
                clock.advance(0.25)
                return super().ready()

            def get_chats_by_ids(self, chat_ids):
                clock.advance(0.75)
                return super().get_chats_by_ids(chat_ids)

            def list_chats(self):
                clock.advance(1.25)
                return super().list_chats()

            def close(self):
                clock.advance(0.5)
                return super().close()

        bridge = TimedBridge([], [fresh])
        app = self.make_app(bridge)

        def create_bridge(*args, **kwargs):
            clock.advance(0.25)
            return bridge

        def apply_result(*args, **kwargs):
            clock.advance(0.25)
            return True

        app.create_direct_bridge.side_effect = create_bridge
        app.apply_auto_chat_match_result = Mock(side_effect=apply_result)
        with patch("kakao_gui_matching.time.perf_counter", side_effect=clock.read):
            payload = self.query(app)
            metrics = payload[11]
            self.assertEqual(metrics.mode, "saved_ids_fallback")
            self.assertEqual(metrics.connect_seconds, 0.5)
            self.assertEqual(metrics.lookup_seconds, 0.75)
            self.assertEqual(metrics.list_seconds, 1.25)
            self.assertEqual(metrics.close_seconds, 0.5)
            self.assertEqual(metrics.started_at, 100.0)
            self.assertEqual(metrics.worker_seconds, 3.0)
            clock.advance(0.75)  # Delivery to Tk is separately included in the total.
            app.handle_auto_chat_match_result(payload)

        timing_logs = [text for category, text in app.logs if category == "매칭 시간"]
        self.assertEqual(len(timing_logs), 1)
        for expected in (
            "저장 대상 확인 후 전체 조회",
            "연결 0.50초",
            "전체 목록 1.25초",
            "ID 조회 0.75초",
            "정리 0.50초",
            "반영 0.25초",
            "화면 대기 0.75초",
            "합계 4.00초",
        ):
            self.assertIn(expected, timing_logs[0])

    def test_new_name_with_duplicate_fresh_rooms_stays_ambiguous_despite_old_snapshot(self):
        name = "이서연"
        fresh = [direct_chat("202", name), direct_chat("303", name)]
        bridge = ReadBridge([], fresh)
        app = self.make_app(
            bridge,
            {"관련 없는 방": "404"},
            {"404": "관련 없는 방"},
        )
        app._latest_chat_snapshot = (direct_chat("101", name),)
        payload = self.query(app, names=(name,))
        self.assertEqual([call for call in bridge.calls if call[0] in {"list", "lookup"}], [("list",)])
        self.assertEqual(payload[3], fresh)
        self.assertIsNone(payload[4])
        app.handle_auto_chat_match_result(payload)
        self.assertEqual(app.direct_mappings, {"관련 없는 방": "404"})
        self.assertNotIn(normalize_chat_match_name(name), app.direct_mappings)
        self.assertEqual(app._latest_chat_snapshot, tuple(fresh))
        self.assertTrue(any("중복/불일치 1개" in status for status in app.statuses))
        self.assertTrue(any("중복 후보: 이서연" in text for _category, text in app.logs))
        app.save_direct_settings.assert_not_called()

    def test_changed_guarded_label_is_preserved_after_full_fallback(self):
        changed = direct_chat("101", "이서연")
        bridge = ReadBridge([changed], [changed])
        app = self.make_app(bridge)
        app.guarded_mapping_ids = ("101",)
        payload = self.query(app)
        self.assertIn(("list",), bridge.calls)
        app.handle_auto_chat_match_result(payload)
        self.assertEqual(app.direct_mapping_labels.get("101"), "김민수")

    def test_superseded_worker_result_and_metrics_cannot_change_current_ui(self):
        entered = threading.Event()
        release = threading.Event()
        first_finished = threading.Event()
        fresh = direct_chat("101", "김민수")

        class BlockingBridge(ReadBridge):
            def get_chats_by_ids(self, chat_ids):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test release timed out")
                return super().get_chats_by_ids(chat_ids)

        first = BlockingBridge([fresh], [fresh])
        second = ReadBridge([fresh], [fresh])
        app = self.make_app(first)
        app.create_direct_bridge.side_effect = [first, second]
        app.post = lambda event, payload: (
            app.posts.append((event, payload)),
            (first_finished if payload[0] == 1 else app.finished).set(),
        )
        try:
            self.assertTrue(app.request_auto_chat_matches(("김민수",), reason="file_load"))
            self.assertTrue(entered.wait(3))
            self.assertTrue(app.request_auto_chat_matches(("김민수",), reason="file_load"))
            self.assertTrue(app.finished.wait(3))
            current = next(payload for _event, payload in app.posts if payload[0] == app._auto_match_generation)
            app.handle_auto_chat_match_result(current)
            before = (list(app.logs), list(app.statuses), dict(app.direct_mappings), app.save_direct_settings.call_count)
            release.set()
            self.assertTrue(first_finished.wait(3))
            stale = next(payload for _event, payload in app.posts if payload[0] == 1)
            self.assertTrue(stale[8])
            app.handle_auto_chat_match_result(stale)
            self.assertEqual((app.logs, app.statuses, app.direct_mappings, app.save_direct_settings.call_count), before)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
