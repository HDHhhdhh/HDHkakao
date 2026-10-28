import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kakao_direct import DirectKakaoConfig
from kakao_gui_matching import resolve_pc_chat_ids_via_window
from kakao_gui_matching import PcChatFallbackResult
from kakao_mapping_policy import (
    auto_match_chat_ids, normalize_chat_match_name, reconcile_chat_mappings,
    merge_verified_chat_aliases,
)
from kakao_sender_gui import KakaoSenderApp


TARGET = "A 학교1 학생가"


def chat(chat_id="101", *, local_name=TARGET, peer="1001"):
    return {
        "chat_id": chat_id, "type": "DirectChat", "active_members": 2,
        "title": "별명", "display_name": "별명", "peer_user_id": peer,
        "peer_names": ["별명"], "peer_local_names": [local_name] if local_name else [],
    }


def geometry(chat_id, *, exact=True):
    return SimpleNamespace(resolved=True, chat_id=chat_id,
                           status=SimpleNamespace(match_kind="exact" if exact else "tolerance", candidate_count=1))


class SavedAliasRecoveryTests(unittest.TestCase):
    def test_alias_snapshot_must_keep_the_same_unique_room_and_peer(self):
        original = chat(local_name=None)
        for records in ([chat(peer="1002")], [chat(), chat()], [{**chat(), "title": "다른 방 이름"}]):
            with self.subTest(records=records):
                self.assertEqual(merge_verified_chat_aliases([original], records)[0]["peer_local_names"], [])
        self.assertEqual(merge_verified_chat_aliases([original], [chat()])[0]["peer_local_names"], [TARGET])

    def test_verified_local_name_can_take_over_its_unguarded_profile_nickname(self):
        result = reconcile_chat_mappings({"별명": "101"}, {"101": "별명"}, [TARGET], [chat()])
        self.assertEqual(result.mappings, {normalize_chat_match_name(TARGET): "101"})
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.ownership_handoffs[0].old_owner, "별명")

    def test_alias_handoff_does_not_steal_guarded_or_current_csv_owner(self):
        guarded = auto_match_chat_ids([TARGET], [chat()], {"별명": "101"}, {"101": "별명"},
                                      guarded_mapping_ids=("101",))
        self.assertEqual(guarded.mappings, {})
        current = auto_match_chat_ids(["별명", TARGET], [chat()], {"별명": "101"}, {"101": "별명"})
        self.assertEqual(current.mappings, {"별명": "101"})
        self.assertEqual(current.ambiguous, (TARGET,))

    def test_similar_nickname_and_different_manual_alias_do_not_authorize_handoff(self):
        for owner, local_name in (("별명", "A 학교2 학생나"), ("수동 별칭", TARGET)):
            with self.subTest(owner=owner):
                result = auto_match_chat_ids([TARGET], [chat(local_name=local_name)],
                                             {owner: "101"}, {"101": "별명"})
                self.assertEqual(result.mappings, {})

    def test_recovered_alias_survives_save_reload_and_missing_local_name(self):
        result = reconcile_chat_mappings({"별명": "101"}, {"101": "별명"}, [TARGET], [chat()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            DirectKakaoConfig(mappings=result.mappings, mapping_labels=result.mapping_labels).save(path)
            saved = DirectKakaoConfig.load(path)
            fresh = reconcile_chat_mappings(saved.mappings, saved.mapping_labels, [TARGET], [chat(local_name=None)])
        self.assertEqual(fresh.mappings, {normalize_chat_match_name(TARGET): "101"})


class NewWindowIdRecoveryTests(unittest.TestCase):
    def run_probe(self, lookup, *, candidate=None, cancel=None, cleanup_error=None):
        provider = SimpleNamespace(resolve_window_rect=lambda *_: SimpleNamespace(resolved=False),
                                   resolve_window_rect_candidate=lambda *_: candidate or geometry("103"))
        with (
            patch("kakao_gui_matching.open_chat", return_value=SimpleNamespace(
                main_handle=11, chat_handle=22, chat_title=TARGET)),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send", side_effect=cleanup_error),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            return resolve_pc_chat_ids_via_window(
                [TARGET], [chat(local_name=None)], {"search_box": {"x": 10, "y": 20}},
                pyautogui_module=SimpleNamespace(), provider_discovery=lambda _: SimpleNamespace(provider=provider),
                chat_lookup=lookup, cancel_event=cancel,
            )

    def test_new_exact_window_candidate_is_verified_then_returned_with_snapshot(self):
        calls = []
        def lookup(ids):
            calls.append(ids)
            return [chat("103", local_name=None)]
        result = self.run_probe(lookup)
        self.assertEqual(calls, [("103",)])
        self.assertEqual(result.mappings, {TARGET: "103"})
        self.assertEqual(result.verified_chats[0]["chat_id"], "103")

    def test_unknown_id_without_server_proof_never_maps(self):
        for records in ([], [chat("104")], [chat("103"), chat("103")],
                        [{**chat("103"), "type": "MultiChat"}],
                        [{**chat("103"), "peer_names": []}]):
            with self.subTest(records=records):
                result = self.run_probe(lambda _: records)
                self.assertEqual(result.mappings, {})
                self.assertEqual(result.verified_chats, ())

    def test_tolerance_and_cancelled_lookups_never_map(self):
        calls = []
        result = self.run_probe(lambda ids: calls.append(ids), candidate=geometry("103", exact=False))
        self.assertEqual(result.mappings, {})
        self.assertEqual(calls, [])
        cancel = threading.Event()
        def lookup(_):
            cancel.set()
            return [chat("103")]
        result = self.run_probe(lookup, cancel=cancel)
        self.assertEqual(result.mappings, {})
        self.assertEqual(result.verified_chats, ())

    def test_cleanup_failure_does_not_publish_new_snapshot(self):
        result = self.run_probe(lambda _: [chat("103")], cleanup_error=RuntimeError("fixture"))
        self.assertEqual(result.mappings, {})
        self.assertEqual(result.verified_chats, ())
        self.assertFalse(result.retry_safe)


class AliasWorkerTests(unittest.TestCase):
    def test_manual_worker_releases_only_unguarded_catalog_owner_and_persists_new_id(self):
        for guarded in (False, True):
            with self.subTest(guarded=guarded), tempfile.TemporaryDirectory() as directory:
                class Bridge:
                    def list_chats(self): return [chat(local_name=None)]
                    def get_chats_by_ids(self, ids): return [chat(ids[0], local_name=None, peer="1003")]
                    def close(self): pass
                bridge = Bridge()
                app = KakaoSenderApp.__new__(KakaoSenderApp)
                app._auto_match_generation = 0
                app._auto_match_loading = False
                app._auto_match_pending_callback = None
                app.direct_config = DirectKakaoConfig(mappings={"별명": "101"}, mapping_labels={"101": "별명"},
                                                     guarded_mapping_ids=("101",) if guarded else ())
                app.direct_mappings, app.direct_mapping_labels = {"별명": "101"}, {"101": "별명"}
                app.guarded_mapping_ids = app.direct_config.guarded_mapping_ids
                app.direct_enabled_var = SimpleNamespace(get=lambda: False)
                app.create_direct_bridge = lambda *_args, **_kwargs: bridge
                app.status_var, app.log = SimpleNamespace(set=lambda _: None), lambda *_: None
                finished, posted, reserved = threading.Event(), [], []
                app.post = lambda _, payload: (posted.append(payload), finished.set())
                def probe(names, chats, config, **options):
                    reserved.extend(options["reserved_chat_ids"])
                    verified = options["chat_lookup"](("103",))
                    return PcChatFallbackResult({TARGET: "103"}, (), {"103": TARGET},
                                                verified_chats=tuple(verified))
                with patch("kakao_gui_matching.resolve_pc_chat_ids_via_window", side_effect=probe):
                    app.request_auto_chat_matches([TARGET], reason="manual_retry",
                                                  pc_fallback_config={"search_box": {"x": 10, "y": 20}})
                    self.assertTrue(finished.wait(5))
                self.assertEqual("101" in reserved, guarded)
                self.assertIsNone(posted[0][4])
                self.assertIn("103", [row["chat_id"] for row in posted[0][3]])
                path = Path(directory) / "kakao_direct.json"
                with patch("kakao_sender_gui.DIRECT_CONFIG_PATH", path):
                    self.assertTrue(app.apply_auto_chat_match_result(
                        [TARGET], posted[0][3], reason="manual_retry",
                        pc_mappings=posted[0][6], pc_mapping_labels=posted[0][7]))
                saved = DirectKakaoConfig.load(path)
                self.assertEqual(saved.mappings[normalize_chat_match_name(TARGET)], "103")
                self.assertEqual(saved.mappings["별명"], "101")

    def test_new_alias_recovery_queries_metadata_only_and_apply_saves_the_handoff(self):
        class Bridge:
            def list_chats(self): return [chat(local_name=None)]
            def resolve_chat_aliases(self, ids):
                self.alias_ids = ids
                return [chat()]
            def close(self): pass
        bridge = Bridge()
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = DirectKakaoConfig(mappings={"별명": "101"}, mapping_labels={"101": "별명"})
        app.direct_mappings, app.direct_mapping_labels = {"별명": "101"}, {"101": "별명"}
        app.create_direct_bridge = lambda *_args, **_kwargs: bridge
        app.status_var = SimpleNamespace(set=lambda _: None)
        app.log = lambda *_: None
        app.save_direct_settings = lambda: None
        finished, posted = threading.Event(), []
        app.post = lambda _, payload: (posted.append(payload), finished.set())
        with patch("kakao_gui_matching.resolve_pc_chat_ids_via_window") as screen:
            app.request_auto_chat_matches([TARGET], reason="file_load")
            self.assertTrue(finished.wait(5))
        screen.assert_not_called()
        self.assertEqual(bridge.alias_ids, ("101",))
        self.assertIsNone(posted[0][4])
        self.assertTrue(app.apply_auto_chat_match_result([TARGET], posted[0][3], reason="file_load"))
        self.assertEqual(app.direct_mappings, {normalize_chat_match_name(TARGET): "101"})


if __name__ == "__main__":
    unittest.main()
