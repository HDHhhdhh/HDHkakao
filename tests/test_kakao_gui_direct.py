import unittest
import threading
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kakao_direct import DirectBridgeError, DirectKakaoConfig, DirectSendError, DirectSendUnknownError
from kakao_sender import FocusSafetyError, MessageRow, ScheduleRegistrationResult
from kakao_send_run import SendRunCompleted, SendRunSupervisor
from kakao_send_service import SendPorts
from kakao_sender_gui import (
    ChatMappingHandoff,
    AutoMatchFailure,
    KakaoSenderApp,
    PC_KAKAO_UI_LOCK,
    PcChatFallbackResult,
    SendRunRequest,
    auto_match_chat_ids,
    chat_choice_title,
    confirmed_chat_label,
    confirmed_peer_local_names,
    confirmed_peer_name,
    direct_send_target_preview,
    direct_chat_names_for_rows,
    discover_pc_chat_catalog_ids,
    discover_open_pc_chat_ids,
    failed_result_indexes,
    ManualChatCandidate,
    apply_manual_chat_mapping,
    manual_chat_candidates,
    normalize_manual_chat_targets,
    is_unknown_direct_send_error,
    is_confirmed_mapping_label,
    is_one_to_one_direct_chat,
    normalize_chat_match_name,
    macro_retry_result_indexes,
    row_uses_direct_send,
    row_status_tag,
    safe_auto_match_failure,
    reconcile_chat_mappings,
    resolve_pc_chat_ids_via_window,
    selected_rows_require_macro,
    shift_escape_is_down,
    structured_direct_chat_aliases,
    unique_blank_memo_chat_id,
    unique_chat_names,
    unverified_alias_mapping_names,
    VK_ESCAPE,
    VK_SHIFT,
    windows_async_key_state_reader,
)


def geometry_resolution(chat_id: str, *, match_kind: str = "exact", candidate_count: int = 1):
    return SimpleNamespace(
        resolved=True,
        chat_id=chat_id,
        status=SimpleNamespace(match_kind=match_kind, candidate_count=candidate_count),
    )


class DirectGuiHelpersTests(unittest.TestCase):
    def setUp(self):
        # GUI worker tests use synthetic catalogs. Direct provider tests below
        # call their imported functions with explicit fake providers instead.
        for target in ("discover_pc_chat_catalog_ids", "discover_open_pc_chat_ids", "record_matching_diagnostic"):
            controller = patch(f"kakao_gui_matching.{target}", return_value=())
            controller.start()
            self.addCleanup(controller.stop)

    @staticmethod
    def _manual_direct(
        chat_id="chat-1",
        *,
        label="조예원",
        peer_name="조예원",
        peer_user_id="101",
        chat_type="DirectChat",
        active_members=2,
    ):
        return {
            "chat_id": chat_id,
            "type": chat_type,
            "active_members": active_members,
            "peer_user_id": peer_user_id,
            "peer_names": [peer_name],
            "title": label,
        }

    def test_manual_candidates_require_verified_unique_direct_room(self):
        chats = [
            self._manual_direct("ok"),
            self._manual_direct("memo", chat_type="MemoChat"),
            self._manual_direct("group", chat_type="MultiChat", active_members=3),
            self._manual_direct("missing-peer", peer_user_id="", peer_name="조예원"),
            self._manual_direct("duplicate"),
            self._manual_direct("duplicate", label="다른 방", peer_name="다른 방", peer_user_id="202"),
        ]

        candidates = manual_chat_candidates(chats)

        self.assertEqual(candidates, (ManualChatCandidate("ok", "조예원", "조예원"),))

    def test_manual_mapping_success_allows_csv_alias_that_auto_match_misses(self):
        candidate = manual_chat_candidates(
            [self._manual_direct("room-1")],
            target_name="A 가좌2 조예원",
        )[0]

        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            candidate.chat_id,
            [self._manual_direct("room-1")],
            {},
            {},
            selected_candidate=candidate,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.mappings, {"a 가좌2 조예원": "room-1"})
        self.assertEqual(result.mapping_labels, {"room-1": "조예원"})

    def test_manual_candidates_hide_id_owned_by_other_name(self):
        chats = [self._manual_direct("room-1")]

        candidates = manual_chat_candidates(chats, {"다른 대상": "room-1"}, target_name="A 가좌2 조예원")
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            chats,
            {"다른 대상": "room-1"},
            {"room-1": "조예원"},
        )

        self.assertEqual(candidates, ())
        self.assertFalse(result.success)
        self.assertIn("eligible candidate", result.error)
        self.assertEqual(result.mappings, {"다른 대상": "room-1"})

    def test_manual_mapping_rejects_duplicate_persisted_id_ownership(self):
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            [self._manual_direct("room-1")],
            {"첫 대상": "room-1", "둘째 대상": "room-1"},
            {"room-1": "조예원"},
        )

        self.assertFalse(result.success)
        self.assertIn("invalid existing chat mapping ownership", result.error)
        self.assertEqual(result.mappings, {})

    def test_manual_mapping_rejects_stale_or_forged_selection(self):
        old_candidate = manual_chat_candidates(
            [self._manual_direct("room-1")],
            target_name="A 가좌2 조예원",
        )[0]
        latest = self._manual_direct("room-1", label="조예원 새 표시명")
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            [latest],
            {},
            {},
            selected_candidate=old_candidate,
        )
        forged = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            [latest],
            {},
            {},
            selected_candidate={"chat_id": "room-1"},
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "stale or forged manual chat selection")
        self.assertFalse(forged.success)
        self.assertEqual(forged.error, "stale or forged manual chat selection")
        self.assertEqual(result.mappings, {})
        self.assertEqual(result.mapping_labels, {})

    def test_manual_mapping_rejects_latest_snapshot_group_even_if_old_candidate_was_direct(self):
        candidate = manual_chat_candidates(
            [self._manual_direct("room-1")],
            target_name="A 가좌2 조예원",
        )[0]
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            [self._manual_direct("room-1", chat_type="MultiChat", active_members=3)],
            {},
            {},
            selected_candidate=candidate,
        )

        self.assertFalse(result.success)
        self.assertIn("eligible candidate", result.error)
        self.assertEqual(result.mappings, {})

    def test_manual_mapping_allows_only_canonical_catalog_owner_handoff(self):
        chats = [self._manual_direct("room-1")]
        candidate = manual_chat_candidates(
            chats,
            {"조예원": "room-1"},
            target_name="A 가좌2 조예원",
        )[0]
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            chats,
            {"조예원": "room-1"},
            {"room-1": "조예원"},
            selected_candidate=candidate,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.mappings, {"a 가좌2 조예원": "room-1"})
        self.assertEqual(result.mapping_labels, {"room-1": "조예원"})
        self.assertEqual(
            result.ownership_handoff,
            ChatMappingHandoff("조예원", "a 가좌2 조예원", "room-1"),
        )

    def test_manual_mapping_can_replace_differently_named_canonical_catalog_owner(self):
        chats = [self._manual_direct("room-1", label="예원 학생방")]
        candidate = manual_chat_candidates(
            chats,
            {"예원 학생방": "room-1"},
            target_name="A 가좌2 조예원",
        )[0]

        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            chats,
            {"예원 학생방": "room-1"},
            {"room-1": "예원 학생방"},
            selected_candidate=candidate,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.mappings, {"a 가좌2 조예원": "room-1"})
        self.assertEqual(result.mapping_labels, {"room-1": "예원 학생방"})

    def test_manual_mapping_blocks_manual_alias_handoff(self):
        chats = [self._manual_direct("room-1")]
        candidates = manual_chat_candidates(
            chats,
            {"수동 지정 별칭": "room-1"},
            target_name="A 가좌2 조예원",
        )
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            chats,
            {"수동 지정 별칭": "room-1"},
            {"room-1": "조예원"},
        )

        self.assertEqual(candidates, ())
        self.assertFalse(result.success)
        self.assertEqual(result.mappings, {"수동 지정 별칭": "room-1"})

    def test_manual_mapping_does_not_steal_id_reserved_by_another_current_csv_target(self):
        chats = [self._manual_direct("room-1")]
        candidate = manual_chat_candidates(
            chats,
            {"조예원": "room-1"},
            target_name="A 가좌2 조예원",
            reserved_chat_ids=("room-1",),
        )
        result = apply_manual_chat_mapping(
            "A 가좌2 조예원",
            "room-1",
            chats,
            {"조예원": "room-1"},
            {"room-1": "조예원"},
            reserved_chat_ids=("room-1",),
        )

        self.assertEqual(candidate, ())
        self.assertFalse(result.success)
        self.assertIn("eligible candidate", result.error)
        self.assertEqual(result.mappings, {"조예원": "room-1"})

    def test_manual_target_normalization_rejects_duplicate_csv_names(self):
        with self.assertRaisesRegex(ValueError, "duplicate normalized CSV chat name"):
            normalize_manual_chat_targets(("A 가좌2 조예원", "ａ 가좌２ 조예원"))

    def test_open_pc_chat_discovery_uses_only_exact_non_generic_window_candidates(self):
        tablet_chats = [
            self._manual_direct("101", label="기존 방"),
            self._manual_direct("101", label="중복 기존 방"),
            {"chat_id": ""},
        ]
        windows = [
            (1, "EVA_Window", "", (0, 0, 100, 100)),
            (2, "EVA_Window", "카카오톡", (100, 0, 200, 100)),
            (3, "EVA_Window", "예약 메시지", (200, 0, 300, 100)),
            (4, "EVA_Window", "브리핑 보드", (300, 0, 400, 100)),
            (5, "EVA_Window", "새 대화", (10, 20, 110, 220)),
            (6, "EVA_Window", "같은 새 대화", (20, 30, 120, 230)),
            (7, "EVA_Window", "이미 조회된 대화", (30, 40, 130, 240)),
            (8, "EVA_Window", "잘못된 후보", (40, 50, 140, 250)),
            (9, "EVA_Window", "해결 안 됨", (50, 60, 150, 260)),
            (10, "EVA_Window", "허용 오차 후보", (60, 70, 160, 270)),
            (11, "EVA_Window", "충돌 후보", (70, 80, 170, 280)),
        ]
        requested_rects = []

        def resolve_candidate(left, top, width, height):
            requested_rects.append((left, top, width, height))
            chat_id = {
                10: "202",
                20: "202",
                30: "101",
                40: "not-numeric",
                60: "606",
                70: "707",
            }.get(left)
            if chat_id is not None:
                if left == 60:
                    return geometry_resolution(chat_id, match_kind="tolerance")
                if left == 70:
                    return geometry_resolution(chat_id, candidate_count=2)
                return geometry_resolution(chat_id)
            return SimpleNamespace(
                resolved=False,
                chat_id=None,
                status=SimpleNamespace(match_kind="none", candidate_count=0),
            )

        provider = SimpleNamespace(
            resolve_window_rect_candidate=resolve_candidate,
            resolve_window_rect=Mock(side_effect=AssertionError("tolerant/tablet-gated resolver must not be used")),
        )
        provider_discovery = Mock(return_value=SimpleNamespace(provider=provider))
        window_finder = Mock(return_value=windows)

        result = discover_open_pc_chat_ids(
            tablet_chats,
            provider_discovery=provider_discovery,
            window_finder=window_finder,
        )

        self.assertEqual(result, ("202",))
        provider_discovery.assert_called_once_with(("101",))
        window_finder.assert_called_once_with()
        self.assertEqual(
            requested_rects,
            [
                (10, 20, 100, 200),
                (20, 30, 100, 200),
                (30, 40, 100, 200),
                (40, 50, 100, 200),
                (50, 60, 100, 200),
                (60, 70, 100, 200),
                (70, 80, 100, 200),
            ],
        )
        provider.resolve_window_rect.assert_not_called()

    def test_pc_catalog_discovery_seeds_account_with_tablet_and_saved_ids_and_filters_ids(self):
        tablet = [self._manual_direct("101"), self._manual_direct("202")]
        discovery = SimpleNamespace(
            provider=SimpleNamespace(
                list_window_chat_id_candidates=Mock(
                    return_value=("101", "202", "303", "404", "0", "-1", "abc")
                )
            )
        )
        provider_discovery = Mock(return_value=discovery)

        result = discover_pc_chat_catalog_ids(
            tablet,
            saved_mapping_ids=("303", "303", "", "-4", "not-id", "404"),
            provider_discovery=provider_discovery,
        )

        self.assertEqual(result, ("303", "404"))
        provider_discovery.assert_called_once_with(("101", "202", "303", "404"))

    def test_pc_catalog_discovery_dedupes_and_caps_candidates_without_leaking_ids(self):
        tablet = [self._manual_direct("1")]
        catalog_ids = {str(index) for index in range(1, 700)}
        discovery = SimpleNamespace(
            provider=SimpleNamespace(
                list_window_chat_id_candidates=Mock(return_value=tuple(catalog_ids))
            )
        )
        provider_discovery = Mock(return_value=discovery)

        result = discover_pc_chat_catalog_ids(
            tablet,
            saved_mapping_ids=("2", "2", "3"),
            provider_discovery=provider_discovery,
        )

        self.assertEqual(len(result), 512)
        self.assertEqual(len(set(result)), 512)
        self.assertNotIn("1", result)
        self.assertTrue(all(item.isdigit() and int(item) > 0 for item in result))

    def test_manual_button_refresh_is_server_only_and_opens_latest_snapshot(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
        app.active_run = None
        app.worker = None
        app._auto_match_loading = False
        app._latest_chat_snapshot = ()
        app.status_var = Mock()
        app.request_auto_chat_matches = Mock(return_value=True)
        app.show_manual_chat_mapping_dialog = Mock()

        app.open_manual_chat_mapping()

        args, kwargs = app.request_auto_chat_matches.call_args
        self.assertEqual(args, (("A 가좌2 조예원",),))
        self.assertEqual(kwargs["reason"], "manual_picker")
        self.assertNotIn("pc_fallback_config", kwargs)
        snapshot = (self._manual_direct("room-1"),)
        app._latest_chat_snapshot = snapshot
        kwargs["on_complete"](True)
        app.show_manual_chat_mapping_dialog.assert_called_once_with(snapshot)

    def test_top_level_auto_match_is_server_only_and_does_not_require_coordinates(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
        app.active_run = None
        app.worker = None
        app._auto_match_loading = False
        app.status_var = Mock()
        app.pc_chat_fallback_config_snapshot = Mock(
            side_effect=AssertionError("top-level auto match must not read PC coordinates")
        )
        app.request_auto_chat_matches = Mock(return_value=True)

        with patch(
            "kakao_gui_matching.resolve_pc_chat_ids_via_window",
            side_effect=AssertionError("top-level auto match must not use PC screen lookup"),
        ) as resolve_pc:
            app.open_chat_mapping()

        app.request_auto_chat_matches.assert_called_once_with(
            ("A 가좌2 조예원",),
            reason="auto_match",
        )
        app.pc_chat_fallback_config_snapshot.assert_not_called()
        resolve_pc.assert_not_called()

    def test_manual_pc_find_is_target_scoped_retry_with_config_and_no_send(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.status_var = Mock()
        app.request_auto_chat_matches = Mock(return_value=True)
        callback = Mock()
        pc_config = {"search_box": {"x": 10, "y": 20}}

        requested = app.request_manual_pc_target_lookup(
            "  A 대신1 학생과  ",
            pc_config,
            on_complete=callback,
        )

        self.assertTrue(requested)
        app.status_var.set.assert_called_once_with("PC에서 선택 대상 확인 중 · A 대신1 학생과")
        app.request_auto_chat_matches.assert_called_once_with(
            ("A 대신1 학생과",),
            reason="manual_retry",
            on_complete=callback,
            pc_fallback_config=pc_config,
        )

    def test_manual_menu_path_is_blocked_during_active_send(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.active_run = object()
        app.worker = None
        app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
        app.request_auto_chat_matches = Mock()

        with patch("kakao_sender_gui.messagebox.showinfo") as showinfo:
            app.open_manual_chat_mapping()

        showinfo.assert_called_once()
        app.request_auto_chat_matches.assert_not_called()

    def test_manual_commit_persists_canonical_handoff_atomically(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
        app.direct_mappings = {"조예원": "room-1"}
        app.direct_mapping_labels = {"room-1": "조예원"}
        app.direct_config = SimpleNamespace(
            enabled=True,
            mappings={"조예원": "room-1"},
            mapping_labels={"room-1": "조예원"},
            guarded_mapping_ids=(),
        )
        app.guarded_mapping_ids = ()
        app.save_direct_settings = Mock()
        chats = (self._manual_direct("room-1"),)
        candidate = manual_chat_candidates(
            chats,
            app.direct_mappings,
            target_name="A 가좌2 조예원",
        )[0]

        success, error = app.commit_manual_chat_mapping("A 가좌2 조예원", candidate, chats)

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(app.direct_mappings, {"a 가좌2 조예원": "room-1"})
        self.assertEqual(app.direct_mapping_labels, {"room-1": "조예원"})
        self.assertEqual(app.guarded_mapping_ids, ("room-1",))
        app.save_direct_settings.assert_called_once_with()

    def test_manual_commit_rolls_back_memory_and_config_when_save_fails(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
        app.direct_mappings = {"조예원": "room-1"}
        app.direct_mapping_labels = {"room-1": "조예원"}
        app.direct_config = SimpleNamespace(
            enabled=True,
            mappings={"조예원": "room-1"},
            mapping_labels={"room-1": "조예원"},
            guarded_mapping_ids=(),
        )
        app.guarded_mapping_ids = ()
        app.save_direct_settings = Mock(side_effect=OSError("disk full"))
        chats = (self._manual_direct("room-1"),)
        candidate = manual_chat_candidates(
            chats,
            app.direct_mappings,
            target_name="A 가좌2 조예원",
        )[0]

        success, error = app.commit_manual_chat_mapping("A 가좌2 조예원", candidate, chats)

        self.assertFalse(success)
        self.assertIn("OSError", error)
        self.assertEqual(app.direct_mappings, {"조예원": "room-1"})
        self.assertEqual(app.direct_mapping_labels, {"room-1": "조예원"})
        self.assertEqual(app.direct_config.mappings, {"조예원": "room-1"})
        self.assertEqual(app.direct_config.mapping_labels, {"room-1": "조예원"})
        self.assertEqual(app.guarded_mapping_ids, ())
        self.assertEqual(app.direct_config.guarded_mapping_ids, ())
        self.assertTrue(app.direct_config.enabled)

    def test_manual_commit_persists_guarded_id_in_direct_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "kakao_direct.json"
            app = KakaoSenderApp.__new__(KakaoSenderApp)
            app.rows = [MessageRow(2, "조예원", "테스트")]
            app.direct_mappings = {}
            app.direct_mapping_labels = {}
            app.guarded_mapping_ids = ()
            app.direct_config = DirectKakaoConfig()
            app.direct_enabled_var = SimpleNamespace(get=lambda: True)
            chats = (self._manual_direct("room-1"),)
            candidate = manual_chat_candidates(chats, target_name="조예원")[0]

            with patch("kakao_sender_gui.DIRECT_CONFIG_PATH", config_path):
                success, error = app.commit_manual_chat_mapping("조예원", candidate, chats)

            self.assertTrue(success)
            self.assertEqual(error, "")
            loaded = DirectKakaoConfig.load(config_path)
            self.assertEqual(loaded.mappings, {"조예원": "room-1"})
            self.assertEqual(loaded.guarded_mapping_ids, ("room-1",))

    def test_manual_unresolved_includes_guarded_mapping_that_needs_reconfirmation(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
        app.direct_mappings = {"A 가좌2 조예원": "room-1"}
        app.direct_mapping_labels = {"room-1": "조예원"}
        app.guarded_mapping_ids = ("room-1",)

        self.assertEqual(
            app.manual_mapping_unresolved_names((self._manual_direct("room-1"),)),
            (),
        )
        self.assertEqual(
            app.manual_mapping_unresolved_names(
                (self._manual_direct("room-1", label="조예원 새 이름"),)
            ),
            ("A 가좌2 조예원",),
        )

    def test_manual_reselection_replaces_stale_guard_and_prunes_old_room(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "kakao_direct.json"
            app = KakaoSenderApp.__new__(KakaoSenderApp)
            app.rows = [MessageRow(2, "A 가좌2 조예원", "테스트")]
            app.direct_mappings = {"A 가좌2 조예원": "room-old"}
            app.direct_mapping_labels = {"room-old": "예전 방"}
            app.guarded_mapping_ids = ("room-old",)
            app.direct_config = DirectKakaoConfig(
                enabled=True,
                mappings=app.direct_mappings,
                mapping_labels=app.direct_mapping_labels,
                guarded_mapping_ids=app.guarded_mapping_ids,
            )
            app.direct_enabled_var = SimpleNamespace(get=lambda: True)
            chats = (
                self._manual_direct("room-old", label="예전 방"),
                self._manual_direct("room-new", label="조예원"),
            )
            candidate = next(
                item
                for item in manual_chat_candidates(
                    chats,
                    app.direct_mappings,
                    target_name="A 가좌2 조예원",
                )
                if item.chat_id == "room-new"
            )

            with patch("kakao_sender_gui.DIRECT_CONFIG_PATH", config_path):
                success, error = app.commit_manual_chat_mapping(
                    "A 가좌2 조예원",
                    candidate,
                    chats,
                )

            self.assertTrue(success)
            self.assertEqual(error, "")
            loaded = DirectKakaoConfig.load(config_path)
            self.assertEqual(loaded.mappings, {"a 가좌2 조예원": "room-new"})
            self.assertEqual(loaded.mapping_labels, {"room-new": "조예원"})
            self.assertEqual(loaded.guarded_mapping_ids, ("room-new",))

    def test_alias_mapping_requires_current_verified_direct_identity(self):
        name = "A 가좌2 조예원"
        mappings = {name: "room-1"}
        labels = {"room-1": "예원 학생방"}
        direct = self._manual_direct("room-1", label="예원 학생방")

        self.assertEqual(
            unverified_alias_mapping_names((name,), mappings, labels, (direct,)),
            (),
        )
        self.assertEqual(
            unverified_alias_mapping_names(
                (name,),
                mappings,
                labels,
                (self._manual_direct("room-1", label="예원 학생방", chat_type="MultiChat", active_members=3),),
            ),
            (name,),
        )
        self.assertEqual(
            unverified_alias_mapping_names(
                (name,),
                mappings,
                labels,
                (self._manual_direct("room-1", label="바뀐 표시명"),),
            ),
            (name,),
        )
        self.assertEqual(
            unverified_alias_mapping_names((name,), mappings, labels, ()),
            (name,),
        )

    def test_exact_label_manual_mapping_uses_persisted_guard(self):
        group = self._manual_direct(
            "room-1",
            label="정확한 단체방",
            chat_type="MultiChat",
            active_members=3,
        )

        self.assertEqual(
            unverified_alias_mapping_names(
                ("정확한 단체방",),
                {"정확한 단체방": "room-1"},
                {"room-1": "정확한 단체방"},
                (group,),
            ),
            (),
        )
        self.assertEqual(
            unverified_alias_mapping_names(
                ("정확한 단체방",),
                {"정확한 단체방": "room-1"},
                {"room-1": "정확한 단체방"},
                (group,),
                guarded_mapping_ids=("room-1",),
            ),
            ("정확한 단체방",),
        )

    def test_unique_chat_names_preserves_source_order(self):
        rows = [
            MessageRow(2, "  ㅎㄷㅎ  ", "첫 메시지"),
            MessageRow(3, "학생갑T", "두 번째"),
            MessageRow(4, "ㅎㄷㅎ", "세 번째"),
        ]

        self.assertEqual(unique_chat_names(rows), ("ㅎㄷㅎ", "학생갑T"))

    def test_unique_chat_names_deduplicates_nfkc_equivalents(self):
        rows = [
            MessageRow(2, "Ａ 방", "첫 메시지"),
            MessageRow(3, "A 방", "두 번째"),
        ]

        self.assertEqual(unique_chat_names(rows), ("Ａ 방",))

    def test_direct_chat_names_for_rows_includes_attachments_but_excludes_scheduled_and_empty_text(self):
        rows = [
            MessageRow(2, "즉시방", "즉시"),
            MessageRow(3, "예약방", "예약", scheduled_at=datetime.now() + timedelta(days=1)),
            MessageRow(4, "첨부방", "", attachments=(Path("a.pdf"),)),
            MessageRow(5, "빈텍스트방", "   "),
        ]

        self.assertEqual(
            direct_chat_names_for_rows(rows, direct_enabled=True),
            ("즉시방", "첨부방"),
        )
        self.assertEqual(direct_chat_names_for_rows(rows[1:], direct_enabled=True), ("첨부방",))
        self.assertEqual(direct_chat_names_for_rows(rows, direct_enabled=False), ())

    def test_auto_match_uses_unique_normalized_title_or_display_name(self):
        chats = [
            {"chat_id": "101", "title": "ㅎㄷㅎ", "display_name": "홍길동"},
            {"chat_id": "202", "title": "선생님 단톡", "display_name": "학생갑T"},
        ]

        result = auto_match_chat_ids([" ㅎㄷㅎ ", "학생갑t", "없는 방"], chats)

        self.assertEqual(result.mappings, {" ㅎㄷㅎ ": "101", "학생갑t": "202"})
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ("없는 방",))

    def test_auto_match_refuses_ambiguous_titles(self):
        result = auto_match_chat_ids(
            ["동명이인"],
            [
                {"chat_id": "1", "title": "동명이인"},
                {"chat_id": "2", "display_name": "동명이인"},
            ],
        )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.ambiguous, ("동명이인",))

    def test_auto_match_refuses_two_names_for_one_chat_id(self):
        result = auto_match_chat_ids(
            ["수학방", "홍길동"],
            [{"chat_id": "777", "title": "수학방", "display_name": "홍길동"}],
        )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.ambiguous, ("수학방", "홍길동"))

    def test_structured_direct_aliases_cover_role_and_class_labels(self):
        self.assertEqual(structured_direct_chat_aliases("학생갑T"), ("학생갑",))
        self.assertEqual(structured_direct_chat_aliases("A 장성중3 학생감"), ("학생감",))
        self.assertEqual(structured_direct_chat_aliases("[도승지] 학생갸"), ("학생갸",))
        self.assertEqual(structured_direct_chat_aliases("내 친구 홍길동"), ())

    def test_auto_match_uses_only_unique_two_person_structured_aliases(self):
        chats = [
            {"chat_id": "1", "type": "DirectChat", "active_members": 2, "title": "학생갑"},
            {"chat_id": "2", "type": "DirectChat", "active_members": 2, "title": "학생갸"},
            {"chat_id": "3", "type": "DirectChat", "active_members": 2, "title": "변하영"},
            {"chat_id": "4", "type": "DirectChat", "active_members": 2, "title": "학생감"},
            {"chat_id": "5", "type": "DirectChat", "active_members": 2, "title": "문종호"},
        ]

        result = auto_match_chat_ids(
            ["학생갑T", "학생갸T", "변하영T", "A 장성중3 학생감", "A 화정2 문종호"],
            chats,
        )

        self.assertEqual(
            result.mappings,
            {
                "학생갑T": "1",
                "학생갸T": "2",
                "변하영T": "3",
                "A 장성중3 학생감": "4",
                "A 화정2 문종호": "5",
            },
        )
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ())

    def test_auto_match_uses_verified_peer_nickname_when_room_labels_differ(self):
        chats = [
            {
                "chat_id": "700",
                "type": "DirectChat",
                "active_members": 2,
                "title": "로컬 제목과 다름",
                "display_name": "표시명도 다름",
                "peer_user_id": "20",
                "peer_names": ["김가현"],
            }
        ]

        result = auto_match_chat_ids(["A 저동2 김가현"], chats)

        self.assertEqual(result.mappings, {"A 저동2 김가현": "700"})
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ())
        self.assertEqual(confirmed_peer_name(chats[0]), "김가현")

    def test_auto_match_uses_account_local_friend_name_with_stable_peer_ids(self):
        chat = {
            "chat_id": "700",
            "type": "DirectChat",
            "active_members": 2,
            "title": "태블릿 표시명",
            "display_name": "프로필명",
            "peer_user_id": "20",
            "peer_names": ["프로필명"],
            "peer_local_names": ["A 저동2 김가현"],
        }

        result = auto_match_chat_ids(["A 저동2 김가현"], [chat])

        self.assertEqual(result.mappings, {"A 저동2 김가현": "700"})
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ())
        self.assertEqual(confirmed_peer_local_names(chat), ("A 저동2 김가현",))

    def test_local_friend_name_is_ignored_without_verified_direct_peer(self):
        unsafe = {
            "chat_id": "700",
            "type": "DirectChat",
            "active_members": 2,
            "peer_local_names": ["A 저동2 김가현"],
        }

        result = auto_match_chat_ids(["A 저동2 김가현"], [unsafe])

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unmatched, ("A 저동2 김가현",))
        self.assertEqual(confirmed_peer_local_names(unsafe), ())

    def test_peer_nickname_mapping_refuses_duplicate_rooms_and_unverified_payloads(self):
        result = auto_match_chat_ids(
            ["A 저동2 김가현", "A 대진2 윤정민"],
            [
                {
                    "chat_id": "1",
                    "type": "DirectChat",
                    "active_members": 2,
                    "peer_user_id": "20",
                    "peer_names": ["김가현"],
                },
                {
                    "chat_id": "2",
                    "type": "DirectChat",
                    "active_members": 2,
                    "peer_user_id": "20",
                    "peer_names": ["김가현"],
                },
                {
                    "chat_id": "3",
                    "type": "MultiChat",
                    "active_members": 2,
                    "peer_user_id": "30",
                    "peer_names": ["윤정민"],
                },
            ],
        )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.ambiguous, ("A 저동2 김가현",))
        self.assertEqual(result.unmatched, ("A 대진2 윤정민",))
        self.assertEqual(confirmed_peer_name({"peer_user_id": "20", "peer_names": ["김가현"]}), "")

    def test_structured_alias_refuses_group_and_duplicate_direct_candidates(self):
        self.assertTrue(is_one_to_one_direct_chat({"type": "DirectChat", "active_members": 2}))
        self.assertFalse(is_one_to_one_direct_chat({"type": "MultiChat", "active_members": 2}))
        self.assertFalse(is_one_to_one_direct_chat({"type": "DirectChat", "active_members": 3}))

        result = auto_match_chat_ids(
            ["학생갑T", "A 장성중3 학생감"],
            [
                {"chat_id": "1", "type": "MultiChat", "active_members": 3, "title": "학생갑"},
                {"chat_id": "2", "type": "DirectChat", "active_members": 2, "title": "학생감"},
                {"chat_id": "3", "type": "DirectChat", "active_members": 2, "title": "학생감"},
            ],
        )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.ambiguous, ("A 장성중3 학생감",))
        self.assertEqual(result.unmatched, ("학생갑T",))

    def test_pc_geometry_requires_exactly_one_blank_memo_chat(self):
        self.assertEqual(unique_blank_memo_chat_id([]), "")
        self.assertEqual(
            unique_blank_memo_chat_id(
                [{"chat_id": "1", "type": "DirectChat", "title": "일반방"}]
            ),
            "",
        )
        self.assertEqual(
            unique_blank_memo_chat_id(
                [
                    {"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""},
                    {"chat_id": "2", "type": "MemoChat", "title": "", "display_name": ""},
                ]
            ),
            "",
        )
        self.assertEqual(
            unique_blank_memo_chat_id(
                [
                    {"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""},
                    {"chat_id": "2", "type": "MemoChat", "title": "이름", "display_name": ""},
                ]
            ),
            "",
        )
        self.assertEqual(
            unique_blank_memo_chat_id(
                [{"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""}]
            ),
            "1",
        )

    def test_auto_match_keeps_manually_confirmed_alias_by_stable_chat_id(self):
        result = auto_match_chat_ids(
            ["학생갑T"],
            [{"chat_id": "202", "title": "학생갑", "display_name": "학생갑"}],
            existing_mappings={"학생갑T": "202"},
        )

        self.assertEqual(result.mappings, {"학생갑T": "202"})
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ())
        self.assertEqual(result.invalidated, ())

    def test_auto_match_invalidates_unverified_missing_or_unlabelled_existing_chat(self):
        result = auto_match_chat_ids(
            ["사라진방"],
            [{"chat_id": "202", "title": "", "display_name": ""}],
            existing_mappings={"사라진방": "404", "빈라벨": "202"},
        )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unmatched, ("사라진방",))
        self.assertEqual(set(result.invalidated), {"사라진방", "빈라벨"})

    def test_auto_match_preserves_verified_mapping_when_fresh_chat_list_is_partial(self):
        result = auto_match_chat_ids(
            ["저장된방"],
            [{"chat_id": "202", "title": "현재 조회된 다른 방", "display_name": ""}],
            existing_mappings={"저장된방": "404"},
            existing_mapping_labels={"404": "저장된방"},
        )

        self.assertEqual(result.mappings, {"저장된방": "404"})
        self.assertEqual(result.unmatched, ())
        self.assertEqual(result.invalidated, ())

    def test_reconcile_blocks_fresh_name_candidate_that_conflicts_with_missing_saved_id(self):
        result = reconcile_chat_mappings(
            {"저장된방": "old-id"},
            {"old-id": "저장된방"},
            ["저장된방"],
            [{"chat_id": "new-id", "title": "저장된방", "display_name": ""}],
        )

        self.assertEqual(result.mappings, {"저장된방": "old-id"})
        self.assertEqual(result.mapping_labels, {"old-id": "저장된방"})
        self.assertEqual(result.matched, ())
        self.assertEqual(result.ambiguous, ("저장된방",))
        self.assertEqual(unverified_alias_mapping_names(
            ("저장된방",), result.mappings, result.mapping_labels,
            [{"chat_id": "new-id", "title": "저장된방", "display_name": ""}],
        ), ("저장된방",))
        self.assertEqual(result.invalidated, ())

    def test_auto_match_preserves_previous_pc_label_for_blank_tablet_chat(self):
        result = auto_match_chat_ids(
            ["ㅎㄷㅎ"],
            [{"chat_id": "memo-1", "title": "", "display_name": ""}],
            existing_mappings={"ㅎㄷㅎ": "memo-1"},
            existing_mapping_labels={"memo-1": "ㅎㄷㅎ"},
        )

        self.assertEqual(result.mappings, {"ㅎㄷㅎ": "memo-1"})
        self.assertEqual(result.invalidated, ())

    def test_reconcile_updates_labels_without_pruning_verified_missing_entries(self):
        result = reconcile_chat_mappings(
            {"학생갑T": "202", "사라진방": "404"},
            {"202": "이전 표시명", "404": "사라진방"},
            ["학생갑T", "새방"],
            [
                {"chat_id": "202", "title": "학생갑", "display_name": "학생갑"},
                {"chat_id": "303", "title": "새방", "display_name": "새방"},
            ],
        )

        self.assertEqual(result.mappings, {"학생갑t": "202", "사라진방": "404", "새방": "303"})
        self.assertEqual(result.mapping_labels, {"202": "학생갑", "404": "사라진방", "303": "새방"})
        self.assertEqual(result.matched, ("학생갑T", "새방"))
        self.assertEqual(result.invalidated, ())

    def test_reconcile_hands_unique_canonical_owner_to_structured_csv_name(self):
        chat = {
            "chat_id": "101",
            "type": "DirectChat",
            "active_members": 2,
            "title": "학생강",
        }

        result = reconcile_chat_mappings(
            {"학생강": "101"},
            {"101": "학생강"},
            ["A 백신2 학생강"],
            [chat],
        )

        self.assertEqual(result.mappings, {"a 백신2 학생강": "101"})
        self.assertEqual(result.mapping_labels, {"101": "학생강"})
        self.assertEqual(result.matched, ("A 백신2 학생강",))
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ())
        self.assertEqual(len(result.ownership_handoffs), 1)
        self.assertEqual(result.ownership_handoffs[0].old_owner, "학생강")

        # The same request is idempotent after ownership has moved.
        repeated = reconcile_chat_mappings(
            result.mappings,
            result.mapping_labels,
            ["A 백신2 학생강"],
            [chat],
        )
        self.assertEqual(repeated.mappings, result.mappings)
        self.assertEqual(repeated.ownership_handoffs, ())

    def test_numeric_candidate_label_matches_and_hands_off_by_same_stable_id(self):
        chat = {
            "chat_id": "10917",
            "type": "DirectChat",
            "active_members": 2,
            "title": "10917 학생개",
        }

        fresh = auto_match_chat_ids(["A 백석1 학생개"], [chat])
        self.assertEqual(fresh.mappings, {"A 백석1 학생개": "10917"})
        self.assertEqual(fresh.unmatched, ())

        reconciled = reconcile_chat_mappings(
            {"10917 학생개": "10917"},
            {"10917": "10917 학생개"},
            ["A 백석1 학생개"],
            [chat],
        )
        self.assertEqual(reconciled.mappings, {"a 백석1 학생개": "10917"})
        self.assertEqual(reconciled.ambiguous, ())
        self.assertEqual(reconciled.unmatched, ())
        self.assertEqual(len(reconciled.ownership_handoffs), 1)

    def test_exact_name_keeps_priority_over_candidate_side_numeric_alias(self):
        chats = [
            {
                "chat_id": "101",
                "type": "DirectChat",
                "active_members": 2,
                "title": "학생개",
            },
            {
                "chat_id": "202",
                "type": "DirectChat",
                "active_members": 2,
                "title": "10917 학생개",
            },
        ]

        exact = auto_match_chat_ids(["학생개"], chats)
        self.assertEqual(exact.mappings, {"학생개": "101"})
        self.assertEqual(exact.ambiguous, ())

        structured = auto_match_chat_ids(["A 백석1 학생개"], chats)
        self.assertEqual(structured.mappings, {})
        self.assertEqual(structured.ambiguous, ("A 백석1 학생개",))

    def test_structured_handoff_refuses_duplicate_rooms_and_duplicate_csv_identity(self):
        duplicate_rooms = reconcile_chat_mappings(
            {"학생강": "101"},
            {"101": "학생강"},
            ["A 백신2 학생강"],
            [
                {
                    "chat_id": "101",
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "학생강",
                },
                {
                    "chat_id": "202",
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "학생강",
                },
            ],
        )
        self.assertEqual(duplicate_rooms.mappings, {"학생강": "101"})
        self.assertEqual(duplicate_rooms.ambiguous, ("A 백신2 학생강",))
        self.assertEqual(duplicate_rooms.ownership_handoffs, ())

        duplicate_csv = reconcile_chat_mappings(
            {"학생강": "101"},
            {"101": "학생강"},
            ["A 백신2 학생강", "A 대화2 학생강"],
            [
                {
                    "chat_id": "101",
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "학생강",
                }
            ],
        )
        self.assertEqual(duplicate_csv.mappings, {"학생강": "101"})
        self.assertEqual(
            duplicate_csv.ambiguous,
            ("A 백신2 학생강", "A 대화2 학생강"),
        )
        self.assertEqual(duplicate_csv.ownership_handoffs, ())

        duplicate_records = reconcile_chat_mappings(
            {"학생강": "101"},
            {"101": "학생강"},
            ["A 백신2 학생강"],
            [
                {
                    "chat_id": "101",
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "학생강",
                },
                {
                    "chat_id": "101",
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "학생강",
                },
            ],
        )
        self.assertEqual(duplicate_records.mappings, {"학생강": "101"})
        self.assertEqual(duplicate_records.ambiguous, ("A 백신2 학생강",))
        self.assertEqual(duplicate_records.ownership_handoffs, ())

    def test_fresh_auto_match_refuses_duplicate_chat_id_records(self):
        exact_records = [
            {"chat_id": "101", "title": "새방", "display_name": "새방"},
            {"chat_id": "101", "title": "새방", "display_name": "새방"},
        ]
        exact = auto_match_chat_ids(["새방"], exact_records)
        self.assertEqual(exact.mappings, {})
        self.assertEqual(exact.ambiguous, ("새방",))

        structured_records = [
            {
                "chat_id": "202",
                "type": "DirectChat",
                "active_members": 2,
                "title": "10917 학생개",
            },
            {
                "chat_id": "202",
                "type": "DirectChat",
                "active_members": 2,
                "title": "10917 학생개",
            },
        ]
        structured = auto_match_chat_ids(["A 백석1 학생개"], structured_records)
        self.assertEqual(structured.mappings, {})
        self.assertEqual(structured.ambiguous, ("A 백석1 학생개",))

    def test_structured_handoff_preserves_manual_prior_alias_and_partial_old_id(self):
        current_chat = {
            "chat_id": "101",
            "type": "DirectChat",
            "active_members": 2,
            "title": "학생강",
        }
        manual = reconcile_chat_mappings(
            {"수동 지정 별칭": "101"},
            {"101": "수동 지정 별칭"},
            ["A 백신2 학생강"],
            [current_chat],
        )
        self.assertEqual(manual.mappings, {"수동 지정 별칭": "101"})
        self.assertEqual(manual.ambiguous, ("A 백신2 학생강",))

        prior_structured = reconcile_chat_mappings(
            {"A 이전반1 학생강": "101"},
            {"101": "A 이전반1 학생강"},
            ["A 백신2 학생강"],
            [{**current_chat, "title": "A 이전반1 학생강"}],
        )
        self.assertEqual(prior_structured.mappings, {"a 이전반1 학생강": "101"})
        self.assertEqual(prior_structured.ambiguous, ("A 백신2 학생강",))

        partial_new_id = reconcile_chat_mappings(
            {"학생강": "old-id"},
            {"old-id": "학생강"},
            ["A 백신2 학생강"],
            [{**current_chat, "chat_id": "new-id"}],
        )
        self.assertEqual(partial_new_id.mappings, {"학생강": "old-id"})
        self.assertEqual(partial_new_id.ambiguous, ("A 백신2 학생강",))
        self.assertEqual(partial_new_id.ownership_handoffs, ())

    def test_structured_handoff_blocks_known_same_identity_on_another_saved_id(self):
        result = reconcile_chat_mappings(
            {"학생강": "101", "A 다른반2 학생강": "202"},
            {"101": "학생강", "202": "A 다른반2 학생강"},
            ["A 백신2 학생강"],
            [
                {
                    "chat_id": "101",
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "학생강",
                }
            ],
        )

        self.assertEqual(
            result.mappings,
            {"학생강": "101", "a 다른반2 학생강": "202"},
        )
        self.assertEqual(result.ambiguous, ("A 백신2 학생강",))
        self.assertEqual(result.ownership_handoffs, ())

    def test_current_nine_name_case_resolves_eight_and_labels_true_unmatched(self):
        owner_by_requested = {
            "A 백신2 학생강": "학생강",
            "A 장성중3 학생감": "학생감",
            "A 행신2 양연준": "양연준",
            "A 일산동2 윤성민": "윤성민",
            "A 백신2 이민용": "이민용",
            "A 대화2 학생거": "학생거",
            "A 대화2 학생게": "학생게",
            "A 백석1 학생개": "10917 학생개",
        }
        mappings = {
            owner: str(index)
            for index, owner in enumerate(owner_by_requested.values(), start=1)
        }
        labels = {chat_id: owner for owner, chat_id in mappings.items()}
        chats = [
            {
                "chat_id": chat_id,
                "type": "DirectChat",
                "active_members": 2,
                "title": owner,
            }
            for owner, chat_id in mappings.items()
        ]
        requested = (*owner_by_requested.keys(), "A 가좌2 조예원")

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = dict(mappings)
        app.direct_mapping_labels = dict(labels)
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        statuses = []
        logs = []
        app.status_var = SimpleNamespace(set=statuses.append)
        app.log = lambda *args: logs.append(args)

        self.assertTrue(
            app.apply_auto_chat_match_result(
                requested,
                chats,
                reason="mode_enabled",
            )
        )

        self.assertEqual(len(app.direct_mappings), 8)
        for requested_name in owner_by_requested:
            self.assertIn(normalize_chat_match_name(requested_name), app.direct_mappings)
        for old_owner in owner_by_requested.values():
            self.assertNotIn(normalize_chat_match_name(old_owner), app.direct_mappings)
        self.assertNotIn(normalize_chat_match_name("A 가좌2 조예원"), app.direct_mappings)
        self.assertIn("자동 매칭 확정 8개", statuses[-1])
        self.assertIn("중복/불일치 0개 · 미일치 1개", statuses[-1])
        self.assertIn("[수동 매칭]에서 확인", statuses[-1])
        self.assertTrue(
            any(
                category == "자동 매칭" and message == "미일치: A 가좌2 조예원"
                for category, message in logs
            )
        )
        self.assertTrue(
            any(
                category == "자동 매칭" and "[수동 매칭]" in message
                for category, message in logs
            )
        )
        self.assertFalse(any("중복 후보:" in message for _category, message in logs))

    def test_structured_handoff_save_failure_restores_previous_owner(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {"학생강": "101"}
        app.direct_mapping_labels = {"101": "학생강"}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = Mock(side_effect=OSError("disk full"))
        statuses = []
        app.status_var = SimpleNamespace(set=statuses.append)
        app.log = Mock()

        self.assertFalse(
            app.apply_auto_chat_match_result(
                ("A 백신2 학생강",),
                [
                    {
                        "chat_id": "101",
                        "type": "DirectChat",
                        "active_members": 2,
                        "title": "학생강",
                    }
                ],
                reason="mode_enabled",
            )
        )

        self.assertEqual(app.direct_mappings, {"학생강": "101"})
        self.assertEqual(app.direct_mapping_labels, {"101": "학생강"})
        self.assertEqual(app.direct_config.mappings, {"학생강": "101"})
        self.assertEqual(app.direct_config.mapping_labels, {"101": "학생강"})
        self.assertIn("자동 매칭 저장 실패", statuses[-1])

    def test_auto_chat_list_query_posts_from_worker_without_touching_tk(self):
        class Bridge:
            def __init__(self, config, direct_enabled=False, credential_reloader=None):
                self.config = config
                self.direct_enabled = direct_enabled
                self.worker_thread = threading.current_thread()

            def list_chats(self):
                return [{"chat_id": "101", "title": "테스트방", "display_name": "테스트방"}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with patch("kakao_sender_gui.KakaoDirectBridge", Bridge):
            self.assertTrue(app.request_auto_chat_matches(["테스트방"], reason="test"))
            self.assertTrue(finished.wait(5))

        self.assertEqual(len(posted), 1)
        event, payload = posted[0]
        self.assertEqual(event, "auto_match_result")
        self.assertEqual(payload[3], [{"chat_id": "101", "title": "테스트방", "display_name": "테스트방"}])
        self.assertIsNone(payload[4])

    def test_manual_picker_and_validation_backfill_open_and_saved_rooms_missing_from_base_snapshot(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")
        open_chat = self._manual_direct("202", label="열린 새 방", peer_name="열린 상대", peer_user_id="2020")
        saved_chat = self._manual_direct("303", label="저장된 누락 방", peer_name="저장 상대", peer_user_id="3030")

        for reason in ("manual_picker", "manual_validate"):
            with self.subTest(reason=reason):
                instances = []

                class Bridge:
                    def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                        self.direct_enabled = direct_enabled
                        self.lookup_calls = []
                        instances.append(self)

                    def list_chats(self):
                        return [dict(base_chat)]

                    def get_chats_by_ids(self, chat_ids):
                        self.lookup_calls.append(tuple(chat_ids))
                        return [dict(open_chat), dict(saved_chat)]

                    def close(self):
                        return None

                app = KakaoSenderApp.__new__(KakaoSenderApp)
                app._auto_match_generation = 0
                app._auto_match_loading = False
                app._auto_match_pending_callback = None
                app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
                app.direct_mappings = {
                    normalize_chat_match_name("기본 목록 방"): "101",
                    normalize_chat_match_name("저장된 누락 방"): "303",
                }
                app.direct_mapping_labels = {"101": "기본 목록 방", "303": "저장된 누락 방"}
                posted = []
                finished = threading.Event()
                app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

                with (
                    patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
                    patch(
                        "kakao_gui_matching.discover_open_pc_chat_ids",
                    return_value=("202", "101", "202"),
                ) as discover_open,
                patch("kakao_gui_matching.discover_pc_chat_catalog_ids", return_value=()),
                ):
                    self.assertTrue(app.request_auto_chat_matches(["새 CSV 대상"], reason=reason))
                    self.assertTrue(finished.wait(5))

                self.assertEqual(len(instances), 1)
                self.assertEqual(len(instances[0].lookup_calls), 1)
                self.assertEqual(set(instances[0].lookup_calls[0]), {"202", "303"})
                self.assertEqual(len(instances[0].lookup_calls[0]), 2)
                discover_open.assert_called_once_with([base_chat])
                self.assertEqual(posted[0][0], "auto_match_result")
                self.assertIsNone(posted[0][1][4])
                self.assertEqual(
                    [chat["chat_id"] for chat in posted[0][1][3]],
                    ["101", "202", "303"],
                )

    def test_manual_picker_discards_supplemental_rooms_without_a_verified_direct_peer(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")
        targets = ("상대 확인 없는 방", "단체 대상")
        unsafe_direct = {
            "chat_id": "202",
            "type": "DirectChat",
            "active_members": 2,
            "title": targets[0],
            "display_name": targets[0],
            "peer_user_id": None,
            "peer_names": [],
        }
        unsafe_group = {
            "chat_id": "303",
            "type": "MultiChat",
            "active_members": 3,
            "title": targets[1],
            "display_name": targets[1],
            "peer_user_id": None,
            "peer_names": [],
        }

        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [dict(base_chat)]

            def get_chats_by_ids(self, chat_ids):
                self.requested_ids = tuple(chat_ids)
                return [dict(unsafe_direct), dict(unsafe_group)]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.discover_open_pc_chat_ids", return_value=("202", "303")),
            patch("kakao_gui_matching.discover_pc_chat_catalog_ids", return_value=()),
        ):
            self.assertTrue(app.request_auto_chat_matches(targets, reason="manual_picker"))
            self.assertTrue(finished.wait(5))

        snapshot = posted[0][1][3]
        self.assertEqual([chat["chat_id"] for chat in snapshot], ["101"])
        self.assertEqual(posted[0][1][9], "lookup_incomplete")
        reconciled = auto_match_chat_ids(targets, snapshot)
        self.assertEqual(reconciled.mappings, {})
        self.assertEqual(reconciled.unmatched, targets)

    def test_manual_catalog_lookup_uses_bounded_server_lookup_chunks(self):
        base_chat = self._manual_direct("1", label="기본 목록 방")
        candidate_ids = tuple(str(index) for index in range(2, 262))
        calls = []

        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [dict(base_chat)]

            def get_chats_by_ids(self, chat_ids):
                batch = tuple(chat_ids)
                calls.append(batch)
                return [
                    self_chat("catalog-%s" % chat_id, chat_id)
                    for chat_id in batch
                ]

            def close(self):
                return None

        def self_chat(label, chat_id):
            return {
                "chat_id": chat_id,
                "type": "DirectChat",
                "active_members": 2,
                "title": label,
                "display_name": label,
                "peer_user_id": "peer-%s" % chat_id,
                "peer_names": [label],
            }

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.discover_pc_chat_catalog_ids", return_value=candidate_ids),
            patch("kakao_gui_matching.discover_open_pc_chat_ids", return_value=()),
        ):
            self.assertTrue(app.request_auto_chat_matches(("새 대상",), reason="manual_picker"))
            self.assertTrue(finished.wait(2))

        self.assertEqual([len(batch) for batch in calls], [128, 128, 4])
        self.assertTrue(all(len(batch) <= 128 for batch in calls))
        self.assertEqual(len({chat_id for batch in calls for chat_id in batch}), 260)
        self.assertEqual(posted[0][0], "auto_match_result")

    def test_file_load_does_not_scan_full_pc_catalog(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [self._chat("101")]

            @staticmethod
            def _chat(chat_id):
                return {
                    "chat_id": chat_id,
                    "type": "DirectChat",
                    "active_members": 2,
                    "title": "현재 저장 대상",
                    "display_name": "현재 저장 대상",
                    "peer_user_id": "peer-101",
                    "peer_names": ["현재 저장 대상"],
                }

            def get_chats_by_ids(self, chat_ids):
                return [self._chat(chat_id) for chat_id in chat_ids]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch(
                "kakao_gui_matching.discover_pc_chat_catalog_ids",
                side_effect=AssertionError("file_load must not scan the PC catalog"),
            ),
        ):
            self.assertTrue(app.request_auto_chat_matches(("현재 저장 대상",), reason="file_load"))
            self.assertTrue(finished.wait(2))

        self.assertEqual(posted[0][0], "auto_match_result")

    def test_manual_retry_scans_pc_catalog_even_without_screen_fallback_config(self):
        calls = []

        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return []

            def get_chats_by_ids(self, chat_ids):
                calls.append(tuple(chat_ids))
                return []

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.discover_pc_chat_catalog_ids", return_value=("909",)) as discover,
        ):
            self.assertTrue(app.request_auto_chat_matches(("새 대상",), reason="manual_retry"))
            self.assertTrue(finished.wait(2))

        discover.assert_called_once()
        self.assertEqual(calls, [("909",)])

    def test_supplemental_lookup_failure_posts_warning_without_losing_base_snapshot(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")

        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [dict(base_chat)]

            def get_chats_by_ids(self, chat_ids):
                self.requested_ids = tuple(chat_ids)
                raise RuntimeError("synthetic supplemental failure")

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.discover_open_pc_chat_ids", return_value=("202",)),
        ):
            self.assertTrue(app.request_auto_chat_matches(["새 대상"], reason="manual_picker"))
            self.assertTrue(finished.wait(5))

        self.assertEqual(len(posted), 1)
        event, payload = posted[0]
        self.assertEqual(event, "auto_match_result")
        self.assertEqual(len(payload), 12)
        self.assertEqual(payload[3], [base_chat])
        self.assertIsNone(payload[4])
        self.assertFalse(payload[8])
        self.assertEqual(payload[9], "lookup_failed")
        self.assertIsInstance(payload[10], AutoMatchFailure)
        self.assertEqual(payload[10].code, "RuntimeError")

    def test_full_list_failure_revalidates_saved_target_by_chat_id(self):
        verified = self._manual_direct(
            "303",
            label="현재 저장 대상",
            peer_name="저장 상대",
            peer_user_id="3030",
        )
        lookup_calls = []

        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                raise DirectBridgeError(code="get_chats_failed")

            def get_chats_by_ids(self, chat_ids):
                lookup_calls.append(tuple(chat_ids))
                return [dict(verified)]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {normalize_chat_match_name("현재 저장 대상"): "303"}
        app.direct_mapping_labels = {"303": "현재 저장 대상"}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with patch("kakao_sender_gui.KakaoDirectBridge", Bridge):
            self.assertTrue(app.request_auto_chat_matches(("현재 저장 대상",), reason="mode_change"))
            self.assertTrue(finished.wait(2))

        event, payload = posted[0]
        self.assertEqual(event, "auto_match_result")
        self.assertEqual(lookup_calls, [("303",)])
        self.assertEqual(payload[3], [verified])
        self.assertIsNone(payload[4])
        self.assertEqual(payload[9], "full_list_failed")
        self.assertEqual(payload[10].code, "get_chats_failed")

    def test_file_load_backfills_only_saved_ids_for_current_targets_without_scanning_pc_windows(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")
        target_chat = self._manual_direct("303", label="현재 저장 대상", peer_name="저장 상대", peer_user_id="3030")
        instances = []

        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled
                self.lookup_calls = []
                instances.append(self)

            def list_chats(self):
                return [dict(base_chat)]

            def get_chats_by_ids(self, chat_ids):
                self.lookup_calls.append(tuple(chat_ids))
                return [dict(target_chat)]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {
            normalize_chat_match_name("현재 저장 대상"): "303",
            normalize_chat_match_name("관련 없는 저장 대상"): "404",
        }
        app.direct_mapping_labels = {"303": "현재 저장 대상", "404": "관련 없는 저장 대상"}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch(
                "kakao_gui_matching.discover_open_pc_chat_ids",
                side_effect=AssertionError("file load must not enumerate PC windows"),
            ) as discover_open,
        ):
            self.assertTrue(app.request_auto_chat_matches(["현재 저장 대상"], reason="file_load"))
            self.assertTrue(finished.wait(5))

        discover_open.assert_not_called()
        self.assertEqual(instances[0].lookup_calls, [("303",)])
        self.assertEqual(
            [chat["chat_id"] for chat in posted[0][1][3]],
            ["303"],
        )
        self.assertIsNone(posted[0][1][4])

    def test_file_load_query_never_enters_pc_fallback_even_with_stale_config(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "type": "MemoChat", "title": "", "display_name": ""}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())
        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch(
                "kakao_gui_matching.resolve_pc_chat_ids_via_window",
                side_effect=AssertionError("file_load must never reach PC screen fallback"),
            ) as resolve_pc,
            patch(
                "kakao_sender_gui.require_pyautogui",
                side_effect=AssertionError("file_load must never initialize pyautogui"),
            ) as require_pyautogui,
        ):
            self.assertTrue(
                app.request_auto_chat_matches(
                    ["PC alias"],
                    reason="file_load",
                    pc_fallback_config={"search_box": {"x": 10, "y": 20}},
                )
            )
            self.assertTrue(finished.wait(5))

        payload = posted[0][1]
        self.assertIsNone(payload[4])
        self.assertEqual(payload[6], {})
        self.assertEqual(payload[7], {})
        resolve_pc.assert_not_called()
        require_pyautogui.assert_not_called()

    def test_reload_file_passes_no_pc_config_and_only_direct_rows(self):
        rows = [
            MessageRow(2, "즉시방", "즉시"),
            MessageRow(3, "예약방", "예약", scheduled_at=datetime.now() + timedelta(days=1)),
            MessageRow(4, "첨부방", "", attachments=(Path("a.pdf"),)),
        ]
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.file_path = Path("messages.csv")
        app.file_var = SimpleNamespace(set=lambda _value: None)
        app.direct_enabled_var = SimpleNamespace(get=lambda: True)
        app.common_schedule_var = SimpleNamespace(get=lambda: False)
        app.schedule_at_var = SimpleNamespace(get=lambda: "")
        app.cancel_auto_chat_matches = lambda: None
        app.populate_table = lambda: None
        app.refresh_preview_count = lambda: None
        app.update_summary = lambda: None
        app.log = lambda *_args: None
        app.request_auto_chat_matches = Mock()

        with patch("kakao_sender_gui.load_messages", return_value=rows):
            app.reload_file()

        app.request_auto_chat_matches.assert_called_once_with(("즉시방", "첨부방"), reason="file_load")
        self.assertNotIn("pc_fallback_config", app.request_auto_chat_matches.call_args.kwargs)

    def test_reload_file_matches_attachment_rows_even_without_text(self):
        rows = [
            MessageRow(2, "예약방", "예약", scheduled_at=datetime.now() + timedelta(days=1)),
            MessageRow(3, "첨부방", "", attachments=(Path("a.pdf"),)),
        ]
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.file_path = Path("scheduled.csv")
        app.file_var = SimpleNamespace(set=lambda _value: None)
        app.direct_enabled_var = SimpleNamespace(get=lambda: True)
        app.common_schedule_var = SimpleNamespace(get=lambda: False)
        app.schedule_at_var = SimpleNamespace(get=lambda: "")
        app.cancel_auto_chat_matches = lambda: None
        app.populate_table = lambda: None
        app.refresh_preview_count = lambda: None
        app.update_summary = lambda: None
        app.log = lambda *_args: None
        app.request_auto_chat_matches = Mock()

        with patch("kakao_sender_gui.load_messages", return_value=rows):
            app.reload_file()

        app.request_auto_chat_matches.assert_called_once_with(("첨부방",), reason="file_load")

    def test_mode_enabled_query_never_enters_pc_fallback_even_with_stale_config(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "type": "MemoChat", "title": "", "display_name": ""}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch(
                "kakao_gui_matching.resolve_pc_chat_ids_via_window",
                side_effect=AssertionError("mode_enabled must never reach PC screen fallback"),
            ) as resolve_pc,
            patch(
                "kakao_sender_gui.require_pyautogui",
                side_effect=AssertionError("mode_enabled must never initialize pyautogui"),
            ) as require_pyautogui,
        ):
            self.assertTrue(
                app.request_auto_chat_matches(
                    ["PC alias"],
                    reason="mode_enabled",
                    pc_fallback_config={"search_box": {"x": 10, "y": 20}},
                )
            )
            self.assertTrue(finished.wait(5))

        self.assertIsNone(posted[0][1][4])
        self.assertEqual(posted[0][1][6], {})
        resolve_pc.assert_not_called()
        require_pyautogui.assert_not_called()

    def test_before_send_exact_tablet_match_does_not_open_pc_fallback(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "title": "테스트방", "display_name": "테스트방"}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.resolve_pc_chat_ids_via_window") as resolve_pc,
            patch(
                "kakao_sender_gui.require_pyautogui",
                side_effect=AssertionError("before_send must never initialize pyautogui"),
            ) as require_pyautogui,
        ):
            app.request_auto_chat_matches(
                ["테스트방"],
                reason="before_send",
                pc_fallback_config={"search_box": {"x": 10, "y": 20}},
            )
            self.assertTrue(finished.wait(5))

        self.assertEqual(posted[0][1][6], {})
        resolve_pc.assert_not_called()
        require_pyautogui.assert_not_called()

    def test_before_send_ambiguous_name_never_enters_pc_fallback(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [
                    {"chat_id": "101", "title": "동명이인", "display_name": ""},
                    {"chat_id": "202", "title": "동명이인", "display_name": ""},
                ]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app._auto_match_cancel_event = threading.Event()
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.resolve_pc_chat_ids_via_window") as resolve_pc,
            patch(
                "kakao_sender_gui.require_pyautogui",
                side_effect=AssertionError("before_send must never initialize pyautogui"),
            ) as require_pyautogui,
        ):
            self.assertTrue(
                app.request_auto_chat_matches(
                    ["동명이인"],
                    reason="before_send",
                    pc_fallback_config={"search_box": {"x": 10, "y": 20}},
                )
            )
            self.assertTrue(finished.wait(5))

        payload = posted[0][1]
        self.assertEqual(payload[6], {})
        resolve_pc.assert_not_called()
        require_pyautogui.assert_not_called()

    def test_before_send_generic_unmatched_name_never_enters_pc_fallback(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "title": "태블릿 표시명", "display_name": ""}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app._auto_match_cancel_event = threading.Event()
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch(
                "kakao_gui_matching.resolve_pc_chat_ids_via_window",
                side_effect=AssertionError("before_send must never reach PC screen fallback"),
            ) as resolve_pc,
            patch(
                "kakao_sender_gui.require_pyautogui",
                side_effect=AssertionError("before_send must never initialize pyautogui"),
            ) as require_pyautogui,
        ):
            self.assertTrue(
                app.request_auto_chat_matches(
                    ["PC alias"],
                    reason="before_send",
                    pc_fallback_config={"search_box": {"x": 10, "y": 20}},
                )
            )
            self.assertTrue(finished.wait(5))

        self.assertEqual(posted[0][1][6], {})
        resolve_pc.assert_not_called()
        require_pyautogui.assert_not_called()

    def test_worker_retries_remaining_pc_aliases_in_friend_tab(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "title": "태블릿 표시명", "display_name": ""}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())
        first = PcChatFallbackResult({}, ("PC alias",), {})
        second = PcChatFallbackResult({"PC alias": "101"}, (), {"101": "PC alias"})

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.resolve_pc_chat_ids_via_window", side_effect=[first, second]) as resolve_pc,
        ):
            app.request_auto_chat_matches(
                ["PC alias"],
                reason="manual_retry",
                pc_fallback_config={"search_box": {"x": 10, "y": 20}},
            )
            self.assertTrue(finished.wait(5))

        self.assertEqual(resolve_pc.call_count, 2)
        self.assertTrue(resolve_pc.call_args_list[1].kwargs["prepare_friend_tab"])
        self.assertEqual(posted[0][1][6], {"PC alias": "101"})

    def test_worker_does_not_retry_friend_tab_after_cleanup_failure(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "title": "태블릿 표시명", "display_name": ""}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()
        app.post = lambda event, payload: (posted.append((event, payload)), finished.set())
        unsafe = PcChatFallbackResult({}, ("PC alias",), {}, retry_safe=False)

        with (
            patch("kakao_sender_gui.KakaoDirectBridge", Bridge),
            patch("kakao_gui_matching.resolve_pc_chat_ids_via_window", return_value=unsafe) as resolve_pc,
        ):
            app.request_auto_chat_matches(
                ["PC alias"],
                reason="manual_retry",
                pc_fallback_config={"search_box": {"x": 10, "y": 20}},
            )
            self.assertTrue(finished.wait(5))

        resolve_pc.assert_called_once()
        self.assertEqual(posted[0][1][6], {})

    def test_cancel_auto_chat_matches_sets_worker_cancel_event(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 4
        app._auto_match_loading = True
        app._auto_match_cancel_event = threading.Event()
        callback_results = []
        app._auto_match_pending_callback = callback_results.append

        app.cancel_auto_chat_matches()

        self.assertTrue(app._auto_match_cancel_event.is_set())
        self.assertEqual(app._auto_match_generation, 5)
        self.assertFalse(app._auto_match_loading)
        self.assertIsNone(app._auto_match_pending_callback)
        self.assertEqual(callback_results, [False])

    def test_auto_match_handler_reports_supplemental_warning_but_keeps_success_callback(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")
        warning_expectations = {
            "lookup_failed": ("추가 조회 실패", "누락 채팅 추가 조회 실패"),
            "lookup_incomplete": ("확인되지 않았습니다", "1:1 검증 미완료"),
            "full_list_failed": ("전체 목록 페이지 오류", "chat ID 개별 검증 경로 사용"),
        }

        for warning, (status_text, log_text) in warning_expectations.items():
            with self.subTest(warning=warning):
                callback_results = []
                callback = callback_results.append
                statuses = []
                logs = []
                app = KakaoSenderApp.__new__(KakaoSenderApp)
                app._auto_match_generation = 7
                app._auto_match_loading = True
                app._auto_match_pending_callback = callback
                app.status_var = SimpleNamespace(set=statuses.append)
                app.log = lambda *args: logs.append(args)
                app.apply_auto_chat_match_result = Mock(return_value=True)

                app.handle_auto_chat_match_result(
                    (
                        7,
                        "manual_picker",
                        ("기본 목록 방",),
                        [base_chat],
                        None,
                        callback,
                        {},
                        {},
                        False,
                        warning,
                    )
                )

                self.assertFalse(app._auto_match_loading)
                self.assertIsNone(app._auto_match_pending_callback)
                self.assertEqual(callback_results, [True])
                self.assertIn(status_text, statuses[-1])
                self.assertTrue(any(log_text in message for _category, message in logs))
                self.assertEqual(app._latest_chat_snapshot, (base_chat,))
                app.apply_auto_chat_match_result.assert_called_once_with(
                    ("기본 목록 방",),
                    [base_chat],
                    reason="manual_picker",
                    pc_mappings={},
                    pc_mapping_labels={},
                )

    def test_auto_match_handler_accepts_supplemental_safe_failure_payload(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")
        callback_results = []
        callback = callback_results.append
        statuses = []
        logs = []
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 8
        app._auto_match_loading = True
        app._auto_match_pending_callback = callback
        app.status_var = SimpleNamespace(set=statuses.append)
        app.log = lambda *args: logs.append(args)
        app.apply_auto_chat_match_result = Mock(return_value=True)
        failure = safe_auto_match_failure(
            DirectBridgeError("secret", code="refresh_rejected")
        )

        app.handle_auto_chat_match_result(
            (
                8,
                "manual_picker",
                ("기본 목록 방",),
                [base_chat],
                None,
                callback,
                {},
                {},
                False,
                "lookup_failed",
                failure,
            )
        )

        self.assertEqual(callback_results, [True])
        self.assertIn("인증정보", statuses[-1])
        self.assertTrue(any("code=refresh_rejected" in message for _category, message in logs))
        self.assertFalse(any("secret" in message for _category, message in logs))

    def test_auto_match_handler_accepts_legacy_six_through_nine_field_payloads(self):
        base_chat = self._manual_direct("101", label="기본 목록 방")

        for field_count in range(6, 10):
            with self.subTest(field_count=field_count):
                callback_results = []
                callback = callback_results.append
                statuses = []
                logs = []
                app = KakaoSenderApp.__new__(KakaoSenderApp)
                app._auto_match_generation = 8
                app._auto_match_loading = True
                app._auto_match_pending_callback = callback
                app.status_var = SimpleNamespace(set=statuses.append)
                app.log = lambda *args: logs.append(args)
                app.apply_auto_chat_match_result = Mock(return_value=True)
                full_payload = (
                    8,
                    "legacy",
                    ("기본 목록 방",),
                    [base_chat],
                    None,
                    callback,
                    {"PC 별칭": "101"},
                    {"101": "PC 별칭"},
                    False,
                )

                app.handle_auto_chat_match_result(full_payload[:field_count])

                expected_pc_mappings = {"PC 별칭": "101"} if field_count >= 7 else {}
                expected_pc_labels = {"101": "PC 별칭"} if field_count >= 8 else {}
                self.assertFalse(app._auto_match_loading)
                self.assertEqual(callback_results, [True])
                self.assertEqual(statuses, [])
                self.assertEqual(logs, [])
                app.apply_auto_chat_match_result.assert_called_once_with(
                    ("기본 목록 방",),
                    [base_chat],
                    reason="legacy",
                    pc_mappings=expected_pc_mappings,
                    pc_mapping_labels=expected_pc_labels,
                )


    def test_auto_chat_match_result_is_saved_after_ui_reconciliation(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {"기존방": "99"}
        app.direct_mapping_labels = {"99": "기존방"}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        statuses = []
        logs = []
        app.status_var = SimpleNamespace(set=statuses.append)
        app.log = lambda *args: logs.append(args)

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("테스트방",),
                [
                    {"chat_id": "101", "title": "테스트방", "display_name": "테스트방"},
                    {"chat_id": "102", "title": "목록에만 있는 방", "display_name": "목록에만 있는 방"},
                ],
                reason="test",
            )
        )
        self.assertEqual(
            app.direct_mappings,
            {"기존방": "99", "테스트방": "101", "목록에만 있는 방": "102"},
        )
        self.assertEqual(
            app.direct_mapping_labels,
            {"99": "기존방", "101": "테스트방", "102": "목록에만 있는 방"},
        )
        self.assertTrue(app.saved)
        self.assertIn("자동 매칭 확정 1개", statuses[-1])
        self.assertIn("전체 저장 3개", statuses[-1])
        self.assertIn("전체 저장 3개", logs[-1][1])

    def test_auto_chat_match_result_preserves_existing_alias_over_catalog_label(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {"PC 별칭": "101"}
        app.direct_mapping_labels = {"101": "PC 별칭"}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("PC 별칭",),
                [
                    {
                        "chat_id": "101",
                        "title": "태블릿 표시명",
                        "display_name": "태블릿 표시명",
                    },
                    {
                        "chat_id": "102",
                        "title": "새 목록 방",
                        "display_name": "새 목록 방",
                    },
                ],
                reason="test",
            )
        )
        self.assertEqual(
            app.direct_mappings,
            {"pc 별칭": "101", "새 목록 방": "102"},
        )
        self.assertNotIn("태블릿 표시명", app.direct_mappings)
        self.assertEqual(app.direct_mapping_labels["101"], "태블릿 표시명")

    def test_verified_pc_alias_takes_over_exact_canonical_catalog_owner(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {"태블릿 표시명": "101"}
        app.direct_mapping_labels = {"101": "태블릿 표시명"}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None
        tablet_chat = {
            "chat_id": "101",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "987654",
            "peer_names": ["태블릿 표시명"],
            "title": "태블릿 표시명",
            "display_name": "태블릿 표시명",
        }

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("A 학교2 PC별칭",),
                [tablet_chat],
                reason="manual_retry",
                pc_mappings={"A 학교2 PC별칭": "101"},
                pc_mapping_labels={"101": "A 학교2 PC별칭"},
            )
        )
        self.assertEqual(
            app.direct_mappings,
            {normalize_chat_match_name("A 학교2 PC별칭"): "101"},
        )
        self.assertNotIn("태블릿 표시명", app.direct_mappings)

    def test_verified_pc_alias_does_not_take_over_differing_manual_owner(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {"수동 지정 별칭": "101"}
        app.direct_mapping_labels = {"101": "수동 지정 별칭"}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None
        tablet_chat = {
            "chat_id": "101",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "987654",
            "peer_names": ["태블릿 표시명"],
            "title": "태블릿 표시명",
            "display_name": "태블릿 표시명",
        }

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("A 학교2 PC별칭",),
                [tablet_chat],
                reason="manual_retry",
                pc_mappings={"A 학교2 PC별칭": "101"},
                pc_mapping_labels={"101": "A 학교2 PC별칭"},
            )
        )
        self.assertEqual(app.direct_mappings, {"수동 지정 별칭": "101"})
        self.assertNotIn("A 학교2 pc별칭", app.direct_mappings)

    def test_guarded_mapping_keeps_operator_label_when_current_identity_changes(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {"조예원": "101"}
        app.direct_mapping_labels = {"101": "조예원"}
        app.guarded_mapping_ids = ("101",)
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = Mock()
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None
        changed_chat = {
            "chat_id": "101",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "222",
            "peer_names": ["다른 사람"],
            "title": "다른 사람",
            "display_name": "다른 사람",
        }

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("조예원",),
                [changed_chat],
                reason="before_send",
            )
        )

        self.assertEqual(app.direct_mapping_labels, {"101": "조예원"})
        self.assertEqual(
            unverified_alias_mapping_names(
                ("조예원",),
                app.direct_mappings,
                app.direct_mapping_labels,
                (changed_chat,),
                guarded_mapping_ids=app.guarded_mapping_ids,
            ),
            ("조예원",),
        )

    def test_auto_chat_match_result_rejects_non_memo_pc_fallback_payload(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("PC alias", "missing"),
                [{"chat_id": "101", "title": "태블릿방", "display_name": "태블릿방"}],
                reason="before_send",
                pc_mappings={"PC alias": "101", "missing": "404"},
                pc_mapping_labels={"101": "PC alias"},
            )
        )
        # The invalid PC fallback must not create its local alias, but the
        # same full tablet snapshot is still eligible for catalog persistence.
        self.assertEqual(app.direct_mappings, {"태블릿방": "101"})
        self.assertEqual(app.direct_mapping_labels, {"101": "태블릿방"})
        self.assertNotIn("pc alias", app.direct_mappings)

    def test_auto_chat_match_result_accepts_verified_direct_pc_alias(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None
        tablet_chat = {
            "chat_id": "101",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "987654",
            "peer_names": ["태블릿 표시명"],
            "title": "태블릿 표시명",
            "display_name": "태블릿 표시명",
        }

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("A 학교2 PC별칭",),
                [tablet_chat],
                reason="file_load",
                pc_mappings={"A 학교2 PC별칭": "101"},
                pc_mapping_labels={"101": "A 학교2 PC별칭"},
            )
        )
        self.assertEqual(app.direct_mappings, {normalize_chat_match_name("A 학교2 PC별칭"): "101"})
        self.assertEqual(app.direct_mapping_labels, {"101": "태블릿 표시명"})
        self.assertTrue(app.saved)

    def test_auto_chat_match_result_persists_pc_label_when_tablet_label_is_blank(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("ㅎㄷㅎ",),
                [{"chat_id": "memo-1", "type": "MemoChat", "title": "", "display_name": ""}],
                reason="before_send",
                pc_mappings={"ㅎㄷㅎ": "memo-1"},
                pc_mapping_labels={"memo-1": "ㅎㄷㅎ"},
            )
        )
        self.assertEqual(app.direct_mappings, {normalize_chat_match_name("ㅎㄷㅎ"): "memo-1"})
        self.assertEqual(app.direct_mapping_labels, {"memo-1": normalize_chat_match_name("ㅎㄷㅎ")})
        self.assertTrue(app.saved)

    def test_auto_chat_match_result_rejects_multiple_pc_fallback_payload_names(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("첫 이름", "둘째 이름"),
                [{"chat_id": "memo-1", "type": "MemoChat", "title": "", "display_name": ""}],
                reason="before_send",
                pc_mappings={"첫 이름": "memo-1", "둘째 이름": "memo-1"},
                pc_mapping_labels={"memo-1": "첫 이름"},
            )
        )
        self.assertEqual(app.direct_mappings, {})
        self.assertEqual(app.direct_mapping_labels, {})

    def test_auto_chat_match_result_rejects_unrequested_pc_fallback_name(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        app.direct_config = SimpleNamespace()
        app.save_direct_settings = lambda: setattr(app, "saved", True)
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.log = lambda *_args: None

        self.assertTrue(
            app.apply_auto_chat_match_result(
                ("요청한 이름",),
                [{"chat_id": "memo-1", "type": "MemoChat", "title": "", "display_name": ""}],
                reason="before_send",
                pc_mappings={"끼어든 이름": "memo-1"},
                pc_mapping_labels={"memo-1": "끼어든 이름"},
            )
        )
        self.assertEqual(app.direct_mappings, {})
        self.assertEqual(app.direct_mapping_labels, {})

    def test_pc_fallback_resolves_only_unmapped_target_and_closes_chat(self):
        provider = SimpleNamespace(
            resolve_window_rect=lambda left, top, width, height: (
                geometry_resolution("tablet-42")
                if (left, top, width, height) == (100, 200, 400, 400)
                else SimpleNamespace(resolved=False)
            )
        )
        discovery = SimpleNamespace(provider=provider)
        context = SimpleNamespace(main_handle=11, chat_handle=22, chat_title="PC alias")
        opened_rows = []
        fake_pyautogui = SimpleNamespace()

        def open_chat_spy(_pyautogui, _config, row, _args):
            opened_rows.append(row)
            return context

        with (
            patch("kakao_gui_matching.open_chat", side_effect=open_chat_spy),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send") as close_chat,
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC alias"],
                [{"chat_id": "tablet-42", "type": "MemoChat", "title": "", "display_name": ""}],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=fake_pyautogui,
                provider_discovery=lambda _ids: discovery,
            )

        self.assertIsInstance(result, PcChatFallbackResult)
        self.assertEqual(result.mappings, {"PC alias": "tablet-42"})
        self.assertEqual(result.unresolved, ())
        self.assertEqual(len(opened_rows), 1)
        self.assertEqual(opened_rows[0].text, "")
        close_chat.assert_called_once_with(
            fake_pyautogui,
            {
                "search_box": {"x": 10, "y": 20},
                "app_titles": ["KakaoTalk"],
                "verify_chat_title": False,
                "open_search_shortcut": ["ctrl", "f"],
            },
            close_chat.call_args.args[2],
            11,
            22,
            "PC alias",
        )

    def test_pc_fallback_opens_empty_chat_with_window_independent_search_shortcut(self):
        """The read-only probe must not depend on a stale calibrated point."""

        chat = self._manual_direct(
            "tray-1",
            label="태블릿 이름",
            peer_name="태블릿 이름",
            peer_user_id="101",
        )
        context = SimpleNamespace(main_handle=11, chat_handle=22, chat_title="PC 별칭")
        fake_pyautogui = SimpleNamespace()

        with (
            patch("kakao_gui_matching.open_chat", return_value=context) as open_chat,
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send"),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC 별칭"],
                [chat],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=fake_pyautogui,
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(resolve_window_rect=lambda *_args: geometry_resolution("tray-1"))
                ),
            )

        open_chat.assert_called_once()
        opened_config = open_chat.call_args.args[1]
        opened_row = open_chat.call_args.args[2]
        self.assertEqual(opened_config["open_search_shortcut"], ["ctrl", "f"])
        self.assertIsInstance(opened_row, MessageRow)
        self.assertEqual(opened_row.text, "")
        self.assertEqual(opened_row.chat_name, "PC 별칭")
        self.assertEqual(result.mappings, {"PC 별칭": "tray-1"})
        self.assertEqual(result.unresolved, ())

    def test_pc_fallback_uses_verified_pc_title_for_blank_tablet_label(self):
        context = SimpleNamespace(main_handle=11, chat_handle=22, chat_title="ㅎㄷㅎ")
        with (
            patch("kakao_gui_matching.open_chat", return_value=context),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send"),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["ㅎㄷㅎ"],
                [{"chat_id": "memo-1", "type": "MemoChat", "title": "", "display_name": ""}],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(
                        resolve_window_rect=lambda *_args: geometry_resolution("memo-1")
                    )
                ),
            )

        self.assertEqual(result.mappings, {"ㅎㄷㅎ": "memo-1"})
        self.assertEqual(result.mapping_labels, {"memo-1": "ㅎㄷㅎ"})
        self.assertEqual(result.unresolved, ())

    def test_pc_fallback_skips_provider_and_window_when_no_coordinates(self):
        with (
            patch("kakao_gui_matching.discover_pc_chat_map") as discover,
            patch("kakao_gui_matching.open_chat") as open_chat,
        ):
            result = resolve_pc_chat_ids_via_window(
                ["미해결"],
                [{"chat_id": "1", "title": "방"}],
                {"search_box": {"x": 0, "y": 0}},
                pyautogui_module=SimpleNamespace(),
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unresolved, ("미해결",))
        discover.assert_not_called()
        open_chat.assert_not_called()

    def test_pc_fallback_rejects_zero_or_nonunique_memo_population(self):
        cases = (
            [],
            [{"chat_id": "1", "type": "DirectChat", "title": "일반방"}],
            [
                {"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""},
                {"chat_id": "2", "type": "MemoChat", "title": "", "display_name": ""},
            ],
            [
                {"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""},
                {"chat_id": "2", "type": "MemoChat", "title": "라벨 있음", "display_name": ""},
            ],
        )
        for chats in cases:
            with self.subTest(chats=chats), patch("kakao_gui_matching.open_chat") as open_chat, patch(
                "kakao_gui_matching.discover_pc_chat_map"
            ) as discover:
                result = resolve_pc_chat_ids_via_window(
                    ["PC alias"],
                    chats,
                    {"search_box": {"x": 10, "y": 20}},
                    pyautogui_module=SimpleNamespace(),
                )

            self.assertEqual(result.mappings, {})
            self.assertEqual(result.unresolved, ("PC alias",))
            open_chat.assert_not_called()
            discover.assert_not_called()

    def test_pc_fallback_keeps_collision_and_no_provider_unresolved(self):
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="방")
        with (
            patch("kakao_gui_matching.open_chat", return_value=context),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send"),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            collision = resolve_pc_chat_ids_via_window(
                ["충돌"],
                [{"chat_id": "1", "title": "방"}],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(provider=None),
            )
            self.assertEqual(collision.mappings, {})
            self.assertEqual(collision.unresolved, ("충돌",))

    def test_pc_fallback_rejects_wrong_opened_title_but_closes_the_window(self):
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="다른 방")
        resolver = lambda *_args: SimpleNamespace(resolved=True, chat_id="1")
        with (
            patch("kakao_gui_matching.open_chat", return_value=context),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send") as close_chat,
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["기대 방"],
                [{"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""}],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(resolve_window_rect=resolver)
                ),
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unresolved, ("기대 방",))
        close_chat.assert_called_once()

    def test_pc_fallback_cleanup_failure_stops_following_targets_without_sending(self):
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="첫 방")
        opened = []
        chats = [
            {
                "chat_id": "1",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "101",
                "peer_names": ["태블릿 첫 이름"],
                "title": "태블릿 첫 이름",
            },
            {
                "chat_id": "2",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "202",
                "peer_names": ["태블릿 둘째 이름"],
                "title": "태블릿 둘째 이름",
            },
        ]

        def open_chat_spy(*args):
            opened.append(args[2])
            return context

        with (
            patch("kakao_gui_matching.open_chat", side_effect=open_chat_spy),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send", side_effect=RuntimeError("close")) as close_chat,
            patch("kakao_gui_matching.return_to_recent_chat_top"),
            patch("kakao_sender_gui.send_one") as send_one,
        ):
            result = resolve_pc_chat_ids_via_window(
                ["첫 방", "둘째 방"],
                chats,
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(
                        resolve_window_rect=lambda *_args: geometry_resolution("1")
                    )
                ),
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unresolved, ("첫 방", "둘째 방"))
        self.assertFalse(result.retry_safe)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].text, "")
        close_chat.assert_called_once()
        send_one.assert_not_called()

    def test_pc_fallback_resolves_multiple_verified_direct_chats_sequentially(self):
        contexts = [
            SimpleNamespace(main_handle=1, chat_handle=11, chat_title="첫 방"),
            SimpleNamespace(main_handle=1, chat_handle=22, chat_title="둘째 방"),
        ]
        chats = [
            {
                "chat_id": "1",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "101",
                "peer_names": ["태블릿 첫 이름"],
                "title": "태블릿 첫 이름",
            },
            {
                "chat_id": "2",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "202",
                "peer_names": ["태블릿 둘째 이름"],
                "title": "태블릿 둘째 이름",
            },
        ]
        provider = SimpleNamespace(
            resolve_window_rect=lambda left, *_args: geometry_resolution("1" if left == 100 else "2")
        )
        with (
            patch("kakao_gui_matching.open_chat", side_effect=contexts) as open_chat,
            patch(
                "kakao_gui_matching.get_window_rect",
                side_effect=[(100, 200, 500, 600), (700, 200, 1100, 600)],
            ),
            patch("kakao_gui_matching.close_chat_after_send"),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["첫 방", "둘째 방"],
                chats,
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(provider=provider),
            )

        self.assertEqual(result.mappings, {"첫 방": "1", "둘째 방": "2"})
        self.assertEqual(result.mapping_labels, {"1": "첫 방", "2": "둘째 방"})
        self.assertEqual(result.unresolved, ())
        self.assertEqual([call.args[2].text for call in open_chat.call_args_list], ["", ""])

    def test_pc_fallback_resets_search_state_between_every_target(self):
        contexts = [
            SimpleNamespace(main_handle=1, chat_handle=11, chat_title="첫 방"),
            SimpleNamespace(main_handle=1, chat_handle=22, chat_title="둘째 방"),
        ]
        chats = [
            {
                "chat_id": "1",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "101",
                "peer_names": ["태블릿 첫 이름"],
                "title": "태블릿 첫 이름",
            },
            {
                "chat_id": "2",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "202",
                "peer_names": ["태블릿 둘째 이름"],
                "title": "태블릿 둘째 이름",
            },
        ]
        events = []

        def open_chat_spy(_pyautogui, _config, row, _args):
            events.append(("open", row.chat_name))
            return contexts.pop(0)

        def close_chat_spy(_pyautogui, _config, _args, _main, _chat, title):
            events.append(("close", title))

        def reset_search_spy(*_args):
            events.append(("reset",))

        with (
            patch("kakao_gui_matching.open_chat", side_effect=open_chat_spy),
            patch(
                "kakao_gui_matching.get_window_rect",
                side_effect=[(100, 200, 500, 600), (700, 200, 1100, 600)],
            ),
            patch("kakao_gui_matching.close_chat_after_send", side_effect=close_chat_spy),
            patch("kakao_gui_matching.return_to_recent_chat_top", side_effect=reset_search_spy),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["첫 방", "둘째 방"],
                chats,
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(
                        resolve_window_rect=lambda left, *_args: geometry_resolution("1" if left == 100 else "2")
                    )
                ),
            )

        self.assertEqual(result.mappings, {"첫 방": "1", "둘째 방": "2"})
        self.assertEqual(
            events,
            [
                ("open", "첫 방"),
                ("close", "첫 방"),
                ("reset",),
                ("open", "둘째 방"),
                ("close", "둘째 방"),
                ("reset",),
            ],
        )

    def test_pc_fallback_resets_after_comments_view_failure_before_next_target(self):
        chats = [
            {
                "chat_id": "1",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "101",
                "peer_names": ["태블릿 첫 이름"],
                "title": "태블릿 첫 이름",
            },
            {
                "chat_id": "2",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "202",
                "peer_names": ["태블릿 둘째 이름"],
                "title": "태블릿 둘째 이름",
            },
        ]
        events = []

        def open_chat_spy(_pyautogui, _config, row, _args):
            events.append(("open", row.chat_name))
            if row.chat_name == "첫 방":
                raise FocusSafetyError("채팅방이 아닌 '댓글 모아보기' 화면이 열려 중단했습니다.")
            return SimpleNamespace(main_handle=1, chat_handle=22, chat_title="둘째 방")

        def close_chat_spy(_pyautogui, _config, _args, _main, _chat, title):
            events.append(("close", title))

        def reset_search_spy(*_args):
            events.append(("reset",))

        with (
            patch("kakao_gui_matching.open_chat", side_effect=open_chat_spy),
            patch("kakao_gui_matching.get_window_rect", return_value=(700, 200, 1100, 600)),
            patch("kakao_gui_matching.close_chat_after_send", side_effect=close_chat_spy),
            patch("kakao_gui_matching.return_to_recent_chat_top", side_effect=reset_search_spy),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["첫 방", "둘째 방"],
                chats,
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(resolve_window_rect=lambda *_args: geometry_resolution("2"))
                ),
            )

        self.assertEqual(result.mappings, {"둘째 방": "2"})
        self.assertEqual(result.unresolved, ("첫 방",))
        self.assertEqual(
            events,
            [
                ("open", "첫 방"),
                ("reset",),
                ("open", "둘째 방"),
                ("close", "둘째 방"),
                ("reset",),
            ],
        )

    def test_pc_fallback_rejects_tolerance_geometry_for_verified_direct_chat(self):
        chat = {
            "chat_id": "1",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "101",
            "peer_names": ["태블릿 이름"],
            "title": "태블릿 이름",
        }
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="PC 별칭")
        with (
            patch("kakao_gui_matching.open_chat", return_value=context),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send"),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC 별칭"],
                [chat],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(
                        resolve_window_rect=lambda *_args: geometry_resolution("1", match_kind="tolerance")
                    )
                ),
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unresolved, ("PC 별칭",))

    def test_pc_fallback_friend_tab_is_focused_before_each_target(self):
        chat = {
            "chat_id": "1",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "101",
            "peer_names": ["태블릿 이름"],
            "title": "태블릿 이름",
        }
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="PC 별칭")
        hotkeys = []
        pyautogui = SimpleNamespace(hotkey=lambda *keys: hotkeys.append(keys))
        focus_calls = []
        with (
            patch("kakao_gui_matching.open_chat", return_value=context),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send"),
            patch("kakao_gui_matching.return_to_recent_chat_top"),
            patch("kakao_sender_gui.time.sleep"),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC 별칭"],
                [chat],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=pyautogui,
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(resolve_window_rect=lambda *_args: geometry_resolution("1"))
                ),
                prepare_friend_tab=True,
                friend_tab_focus=lambda titles: (focus_calls.append(titles), "KakaoTalk")[1],
            )

        self.assertEqual(result.mappings, {"PC 별칭": "1"})
        self.assertEqual(focus_calls, [["KakaoTalk"]])
        self.assertEqual(hotkeys, [("ctrl", "1")])

    def test_pc_fallback_cancellation_during_open_closes_window_and_returns_no_mapping(self):
        cancel_event = threading.Event()
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="첫 방")
        opened = []

        def open_chat_spy(*args):
            opened.append(args[2])
            cancel_event.set()
            return context

        with (
            patch("kakao_gui_matching.open_chat", side_effect=open_chat_spy),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send") as close_chat,
            patch("kakao_gui_matching.return_to_recent_chat_top"),
            patch("kakao_sender_gui.send_one") as send_one,
        ):
            result = resolve_pc_chat_ids_via_window(
                ["첫 방"],
                [{"chat_id": "1", "type": "MemoChat", "title": "", "display_name": ""}],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(
                        resolve_window_rect=lambda *_args: SimpleNamespace(resolved=True, chat_id="1")
                    )
                ),
                cancel_event=cancel_event,
            )

        self.assertEqual([row.chat_name for row in opened], ["첫 방"])
        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unresolved, ("첫 방",))
        close_chat.assert_called_once()
        send_one.assert_not_called()

    def test_unknown_direct_outcome_is_not_retryable(self):
        rows = [MessageRow(2, "ㅎㄷㅎ", "테스트")]
        results = [{"row_index": 0, "status": "unknown", "chat_name": "ㅎㄷㅎ"}]

        self.assertEqual(failed_result_indexes(results, rows), [])

    def test_direct_visible_states_use_success_tag(self):
        self.assertEqual(row_status_tag("직접 전송"), "success")
        self.assertEqual(row_status_tag("예약 대기"), "success")

    def test_chat_choice_title_falls_back_without_blank_rows(self):
        self.assertEqual(chat_choice_title({"chat_id": "123", "title": "", "display_name": "친구"}), "친구")
        self.assertEqual(chat_choice_title({"chat_id": "123"}), "채팅 123")

    def test_display_fallback_is_not_a_confirmed_mapping_label(self):
        chat = {"chat_id": "123"}
        self.assertEqual(confirmed_chat_label(chat), "")
        self.assertFalse(is_confirmed_mapping_label("123", chat_choice_title(chat)))
        self.assertTrue(is_confirmed_mapping_label("123", "실제 방 이름"))

    def test_normalize_chat_match_name_collapses_spacing_and_case(self):
        self.assertEqual(normalize_chat_match_name("  A   CHAT  "), "a chat")
        self.assertEqual(normalize_chat_match_name("Ａ 방"), "a 방")

    def test_unknown_outcome_check_accepts_subclasses(self):
        class AdapterUnknown(DirectSendUnknownError):
            pass

        self.assertTrue(is_unknown_direct_send_error(AdapterUnknown()))

    def test_direct_preview_counts_rows_and_shows_confirmed_target(self):
        rows = (
            (0, MessageRow(2, "수학방", "첫 메시지")),
            (1, MessageRow(3, "수학방", "두 번째")),
        )

        count, targets = direct_send_target_preview(
            rows,
            None,
            True,
            {"수학방": "777"},
            {"777": "홍길동과의 대화"},
        )

        self.assertEqual(count, 2)
        self.assertEqual(targets, ("- 수학방 → 홍길동과의 대화 · chat ID 777",))

    def test_hybrid_routing_uses_direct_attachments_and_keeps_native_reservation(self):
        immediate = MessageRow(2, "즉시방", "안녕하세요")
        scheduled = MessageRow(3, "예약방", "예약", scheduled_at=datetime.now() + timedelta(days=1))
        attached = MessageRow(4, "첨부방", "파일", attachments=(Path("a.pdf"),))

        self.assertTrue(row_uses_direct_send(immediate, None, True))
        self.assertFalse(row_uses_direct_send(scheduled, scheduled.scheduled_at, True))
        self.assertTrue(row_uses_direct_send(attached, None, True))
        self.assertFalse(row_uses_direct_send(attached, None, False))
        self.assertFalse(selected_rows_require_macro([(0, immediate)], None, True))
        self.assertTrue(selected_rows_require_macro([(1, scheduled)], None, True))
        self.assertFalse(selected_rows_require_macro([(2, attached)], None, True))
        self.assertTrue(selected_rows_require_macro([(2, attached)], None, False))

    def test_direct_worker_does_not_initialize_screen_control(self):
        class Bridge:
            def __init__(self):
                self.calls = []
                self.closed = False

            def send_message(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "success": True, "status_code": 0, "chat_id": kwargs["chat_id"],
                    "log_id": "1", "sent_at": 1800000000,
                }

            def close(self):
                self.closed = True

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        events = []
        app.post = lambda event, payload: events.append((event, payload))
        bridge = Bridge()
        app.create_direct_bridge = lambda _config=None: bridge
        request = SendRunRequest(
            selected=((0, MessageRow(2, "ㅎㄷㅎ", "직접 테스트")),),
            config={},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=True,
            chat_mappings=(("ㅎㄷㅎ", "123"),),
            direct_config={"enabled": True, "mappings": {"ㅎㄷㅎ": "123"}},
        )

        with (
            patch("kakao_sender_gui.require_pyautogui", side_effect=AssertionError("screen control must stay unused")),
            patch("kakao_gui_feedback.write_log", return_value=Path("test.csv")),
        ):
            completed = app.send_worker(request)

        self.assertEqual(completed.legacy_results()[0]["status"], "direct_sent")
        self.assertEqual(app.results, [])
        self.assertEqual(bridge.calls, [{"text": "직접 테스트", "chat_id": "123"}])
        self.assertTrue(bridge.closed)
        self.assertEqual(events[-1], ("run_event", completed))

    def test_unknown_direct_worker_result_is_not_marked_as_retryable_error(self):
        class Bridge:
            def send_message(self, **_kwargs):
                raise DirectSendUnknownError()

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        events = []
        app.post = lambda event, payload: events.append((event, payload))
        app.create_direct_bridge = lambda _config=None: Bridge()
        request = SendRunRequest(
            selected=((0, MessageRow(2, "ㅎㄷㅎ", "불명확 테스트")),),
            config={},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=True,
            chat_mappings=(("ㅎㄷㅎ", "123"),),
        )

        with patch("kakao_gui_feedback.write_log", return_value=Path("test.csv")):
            completed = app.send_worker(request)

        results = completed.legacy_results()
        self.assertEqual(results[0]["status"], "unknown")
        self.assertEqual(failed_result_indexes(results, [request.selected[0][1]]), [])
        self.assertEqual(events[-1], ("run_event", completed))

    def test_scheduled_row_uses_native_receipt_without_list_audit_when_direct_is_on(self):
        due_at = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
        row = MessageRow(2, "예약방", "노트북 종료 가능 예약", scheduled_at=due_at)
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        events = []
        app.post = lambda event, payload: events.append((event, payload))
        app.create_direct_bridge = lambda _config=None: (_ for _ in ()).throw(
            AssertionError("scheduled rows must not create a direct bridge")
        )
        request = SendRunRequest(
            selected=((0, row),),
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=True,
        )
        fake_pyautogui = type("FakePyAutoGui", (), {"PAUSE": 0.0})()
        native = Mock()
        native.register.return_value = ScheduleRegistrationResult(False, 'ack', True)
        native.result.return_value = ScheduleRegistrationResult(False, 'unverified', True)

        with (
            patch("kakao_sender_gui.NativeScheduleSession", return_value=native),
            patch("kakao_sender_gui.require_pyautogui", return_value=fake_pyautogui),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender_gui.send_scheduled_one") as send_scheduled,
            patch("kakao_sender_gui.return_to_recent_chat_top"),
            patch("kakao_gui_feedback.write_log", return_value=Path("test.csv")),
        ):
            completed = app.send_worker(request)

        send_scheduled.assert_not_called()
        native.register.assert_called_once_with(row)
        native.verify_pending.assert_not_called()
        self.assertEqual(completed.legacy_results()[0]["status"], "schedule_acknowledged")
        self.assertEqual(completed.legacy_results()[0]["failure_stage"], "")
        self.assertEqual(completed.legacy_results()[0]["retry_transport"], "none")
        self.assertEqual(events[-1], ("run_event", completed))

    def test_native_schedule_waits_for_pc_ui_lock_while_fallback_cleans_up(self):
        due_at = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
        row = MessageRow(2, "예약방", "직렬화 테스트", scheduled_at=due_at)
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        events = []
        app.post = lambda event, payload: events.append((event, payload))
        request = SendRunRequest(
            selected=((0, row),),
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=True,
        )
        fake_pyautogui = type("FakePyAutoGui", (), {"PAUSE": 0.0})()
        pyautogui_started = threading.Event()
        worker_done = threading.Event()

        native = Mock()
        native.register.return_value = ScheduleRegistrationResult(False, 'unverified')

        def native_session_spy(*args, **kwargs):
            pyautogui_started.set()
            return native

        with (
            patch("kakao_sender_gui.NativeScheduleSession", side_effect=native_session_spy),
            patch("kakao_sender_gui.require_pyautogui", side_effect=AssertionError('legacy UI must stay unused')),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender_gui.send_scheduled_one"),
            patch("kakao_sender_gui.return_to_recent_chat_top"),
            patch("kakao_gui_feedback.write_log", return_value=Path("test.csv")),
        ):
            PC_KAKAO_UI_LOCK.acquire()
            try:
                worker = threading.Thread(target=lambda: (app.send_worker(request), worker_done.set()), daemon=True)
                worker.start()
                self.assertFalse(pyautogui_started.wait(0.05))
            finally:
                PC_KAKAO_UI_LOCK.release()
            self.assertTrue(pyautogui_started.wait(1))
            self.assertTrue(worker_done.wait(1))
            worker.join(1)

        self.assertEqual(events[-1][0], "run_event")
        completed = events[-1][1]
        self.assertIsInstance(completed, SendRunCompleted)
        self.assertEqual(completed.legacy_results()[0]["status"], "unknown")

    def test_pc_fallback_screen_state_wraps_real_ui_work_and_cleanup_exception(self):
        events = []
        cancel_event = threading.Event()
        context = SimpleNamespace(main_handle=1, chat_handle=2, chat_title="PC 별칭")
        chat = {
            "chat_id": "1",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "101",
            "peer_names": ["태블릿 이름"],
            "title": "태블릿 이름",
        }

        def screen_callback(active, observed_cancel, acknowledgement):
            events.append(("active" if active else "inactive", observed_cancel))
            if active:
                acknowledgement.set()

        def open_chat_spy(*args):
            events.append(("open",))
            return context

        def close_chat_spy(*args):
            events.append(("close",))

        def reset_search_spy(*args):
            events.append(("reset",))

        with (
            patch("kakao_gui_matching.open_chat", side_effect=open_chat_spy),
            patch("kakao_gui_matching.get_window_rect", return_value=(100, 200, 500, 600)),
            patch("kakao_gui_matching.close_chat_after_send", side_effect=close_chat_spy),
            patch("kakao_gui_matching.return_to_recent_chat_top", side_effect=reset_search_spy),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC 별칭"],
                [chat],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(
                    provider=SimpleNamespace(
                        resolve_window_rect=lambda *_args: (_ for _ in ()).throw(RuntimeError("provider"))
                    )
                ),
                cancel_event=cancel_event,
                screen_use_callback=screen_callback,
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual([item[0] for item in events], ["active", "open", "close", "reset", "inactive"])
        self.assertIs(events[0][1], cancel_event)

    def test_tablet_only_auto_match_posts_no_screen_use_event(self):
        class Bridge:
            def __init__(self, _config, direct_enabled=False, credential_reloader=None):
                self.direct_enabled = direct_enabled

            def list_chats(self):
                return [{"chat_id": "101", "type": "MemoChat", "title": "", "display_name": ""}]

            def close(self):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._auto_match_generation = 0
        app._auto_match_loading = False
        app._auto_match_pending_callback = None
        app.direct_config = SimpleNamespace(to_dict=lambda: {"enabled": True, "mappings": {}})
        app.direct_mappings = {}
        app.direct_mapping_labels = {}
        posted = []
        finished = threading.Event()

        def post(event, payload):
            posted.append((event, payload))
            finished.set()

        app.post = post
        with patch("kakao_sender_gui.KakaoDirectBridge", Bridge):
            self.assertTrue(app.request_auto_chat_matches(["서버 목록만"], reason="file_load"))
            self.assertTrue(finished.wait(5))

        self.assertEqual([event for event, _payload in posted if event == "screen_use_state"], [])
        self.assertEqual(posted[-1][0], "auto_match_result")

    def test_screen_use_guard_renders_active_stop_and_idle_states(self):
        class Var:
            def __init__(self, value=""):
                self.value = value

            def set(self, value):
                self.value = value

        class Widget:
            def __init__(self):
                self.calls = []

            def configure(self, **kwargs):
                self.calls.append(("configure", kwargs))

            def state(self, values):
                self.calls.append(("state", tuple(values)))

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._screen_use_tokens = {}
        app._screen_use_stop_requested = False
        app._screen_use_key_reader = None
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app.screen_use_state_var = Var()
        app.screen_use_state_label = Widget()
        app.screen_use_stop_button = Widget()
        app.status_var = Var()
        app.log = lambda *_args: None
        app.after = lambda *_args: "poll"
        app.after_cancel = lambda *_args: None
        cancel_event = threading.Event()
        acknowledgement = threading.Event()

        app.handle_screen_use_state(("token-1", 3, True, cancel_event, acknowledgement))
        self.assertTrue(acknowledgement.is_set())
        self.assertIn("화면 사용 중", app.screen_use_state_var.value)
        self.assertFalse(any(call == ("state", ("disabled",)) for call in app.screen_use_stop_button.calls))

        self.assertTrue(app.request_screen_use_stop())
        self.assertTrue(cancel_event.is_set())
        self.assertIn("중단 요청됨", app.screen_use_state_var.value)
        self.assertIn(("state", ("disabled",)), app.screen_use_stop_button.calls)

        app.handle_screen_use_state(("token-1", 3, False, cancel_event, None))
        self.assertIn("화면 사용 안 함", app.screen_use_state_var.value)

    def test_shift_escape_stop_sets_current_screen_event_without_advancing_generation(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        cancel_event = threading.Event()
        callback_results = []
        app._screen_use_tokens = {"token-1": (9, cancel_event)}
        app._screen_use_stop_requested = False
        app._screen_use_key_reader = None
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app._auto_match_generation = 9
        app._auto_match_loading = True
        app._auto_match_pending_callback = callback_results.append
        app.status_var = SimpleNamespace(set=lambda value: setattr(app, "last_status", value))
        app.log = lambda *_args: None
        app._render_screen_use_guard = lambda: None

        self.assertTrue(app._on_screen_use_hotkey())
        self.assertTrue(cancel_event.is_set())
        self.assertEqual(app._auto_match_generation, 9)
        self.assertTrue(app._auto_match_loading)
        self.assertEqual(callback_results, [])

        app.handle_auto_chat_match_result((9, "manual_retry", (), [], None, app._auto_match_pending_callback, {}, {}, True))
        self.assertFalse(app._auto_match_loading)
        self.assertEqual(callback_results, [False])

    def test_shift_escape_global_poll_is_debounced_and_fails_closed(self):
        state = {VK_SHIFT: 0x8000, VK_ESCAPE: 0x8000}
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._screen_use_tokens = {"token-1": (1, threading.Event())}
        app._screen_use_key_reader = lambda vk_code: state.get(vk_code, 0)
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app.after = lambda *_args: "poll"
        app.request_screen_use_stop = Mock()

        app.poll_screen_use_hotkey()
        app.poll_screen_use_hotkey()
        self.assertEqual(app.request_screen_use_stop.call_count, 1)
        state[VK_SHIFT] = 0
        state[VK_ESCAPE] = 0
        app.poll_screen_use_hotkey()
        state[VK_SHIFT] = 0x8000
        state[VK_ESCAPE] = 0x8000
        app.poll_screen_use_hotkey()
        self.assertEqual(app.request_screen_use_stop.call_count, 2)
        self.assertFalse(shift_escape_is_down(lambda _vk: (_ for _ in ()).throw(RuntimeError("reader"))))
        with patch("kakao_sender_gui.os.name", "posix"):
            self.assertIsNone(windows_async_key_state_reader())

    def test_stale_screen_use_token_cleanup_does_not_hide_new_generation(self):
        class Var:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Widget:
            def configure(self, **_kwargs):
                return None

            def state(self, _values):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._screen_use_tokens = {}
        app._screen_use_stop_requested = False
        app._screen_use_key_reader = None
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app.screen_use_state_var = Var()
        app.screen_use_state_label = Widget()
        app.screen_use_stop_button = Widget()
        app.after = lambda *_args: "poll"
        app.after_cancel = lambda *_args: None
        old_event = threading.Event()
        new_event = threading.Event()
        old_ack = threading.Event()
        new_ack = threading.Event()

        app.handle_screen_use_state(("old", 1, True, old_event, old_ack))
        app.handle_screen_use_state(("new", 2, True, new_event, new_ack))
        app.handle_screen_use_state(("old", 1, False, old_event, None))
        self.assertEqual(set(app._screen_use_tokens), {"new"})
        self.assertIn("화면 사용 중", app.screen_use_state_var.value)
        app.handle_screen_use_state(("new", 2, False, new_event, None))
        self.assertEqual(app._screen_use_tokens, {})
        self.assertIn("화면 사용 안 함", app.screen_use_state_var.value)

        # A later run can use the same UI objects after a full reset.
        app.handle_screen_use_state(("again", 3, True, new_event, threading.Event()))
        self.assertIn("화면 사용 중", app.screen_use_state_var.value)

    def test_screen_warning_ack_timeout_fails_closed_before_pc_input(self):
        transitions = []
        cancel_event = threading.Event()
        open_chat_spy = Mock()
        chat = {
            "chat_id": "1",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "101",
            "peer_names": ["태블릿 이름"],
            "title": "태블릿 이름",
        }

        def screen_callback(active, observed_cancel, _acknowledgement):
            transitions.append((active, observed_cancel))
            # Deliberately do not acknowledge the active warning.

        with (
            patch("kakao_gui_matching.SCREEN_USE_ACK_TIMEOUT_SECONDS", 0.0),
            patch("kakao_gui_matching.open_chat", open_chat_spy),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC 별칭"],
                [chat],
                {"search_box": {"x": 10, "y": 20}},
                pyautogui_module=SimpleNamespace(),
                provider_discovery=lambda _ids: SimpleNamespace(provider=SimpleNamespace()),
                cancel_event=cancel_event,
                screen_use_callback=screen_callback,
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual([active for active, _event in transitions], [True, False])
        self.assertTrue(cancel_event.is_set())
        open_chat_spy.assert_not_called()

    def test_shift_escape_during_friend_tab_wait_opens_no_next_room(self):
        transitions = []
        cancel_event = threading.Event()
        open_chat_spy = Mock()
        reset_spy = Mock()
        pyautogui = SimpleNamespace(hotkey=Mock())
        chat = {
            "chat_id": "1",
            "type": "DirectChat",
            "active_members": 2,
            "peer_user_id": "101",
            "peer_names": ["태블릿 이름"],
            "title": "태블릿 이름",
        }

        def screen_callback(active, _observed_cancel, acknowledgement):
            transitions.append(active)
            if acknowledgement is not None:
                acknowledgement.set()

        with (
            patch("kakao_gui_matching.open_chat", open_chat_spy),
            patch("kakao_gui_matching.return_to_recent_chat_top", reset_spy),
            patch("kakao_sender_gui.time.sleep", side_effect=lambda _delay: cancel_event.set()),
        ):
            result = resolve_pc_chat_ids_via_window(
                ["PC 별칭"],
                [chat],
                {"search_box": {"x": 10, "y": 20}, "app_titles": ["KakaoTalk"]},
                pyautogui_module=pyautogui,
                provider_discovery=lambda _ids: SimpleNamespace(provider=SimpleNamespace()),
                cancel_event=cancel_event,
                prepare_friend_tab=True,
                friend_tab_focus=lambda _titles: "KakaoTalk",
                screen_use_callback=screen_callback,
            )

        self.assertEqual(result.mappings, {})
        self.assertEqual(transitions, [True, False])
        pyautogui.hotkey.assert_called_once_with("ctrl", "1")
        open_chat_spy.assert_not_called()
        reset_spy.assert_called_once()

    def test_stop_request_covers_screen_session_that_activates_during_cleanup(self):
        class Var:
            def set(self, _value):
                return None

        class Widget:
            def configure(self, **_kwargs):
                return None

            def state(self, _values):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        first_event = threading.Event()
        later_event = threading.Event()
        app._screen_use_tokens = {"first": (1, first_event)}
        app._screen_use_stop_requested = False
        app._screen_use_key_reader = None
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app.screen_use_state_var = Var()
        app.screen_use_state_label = Widget()
        app.screen_use_stop_button = Widget()
        app.status_var = Var()
        app.log = Mock()
        app.after = lambda *_args: "poll"

        self.assertTrue(app.request_screen_use_stop())
        self.assertTrue(first_event.is_set())
        # Idempotent repeated input must not duplicate operator log noise.
        self.assertTrue(app.request_screen_use_stop(source="Shift+Esc"))
        self.assertEqual(app.log.call_count, 1)

        acknowledgement = threading.Event()
        app.handle_screen_use_state(("later", 2, True, later_event, acknowledgement))
        self.assertTrue(acknowledgement.is_set())
        self.assertTrue(later_event.is_set())

    def test_superseded_cancelled_session_does_not_poison_new_screen_session(self):
        class Var:
            def set(self, _value):
                return None

        class Widget:
            def configure(self, **_kwargs):
                return None

            def state(self, _values):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app._screen_use_tokens = {}
        app._screen_use_stop_requested = False
        app._screen_use_key_reader = None
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app.screen_use_state_var = Var()
        app.screen_use_state_label = Widget()
        app.screen_use_stop_button = Widget()
        app.after = lambda *_args: "poll"
        app.after_cancel = lambda *_args: None

        superseded_event = threading.Event()
        superseded_event.set()
        app.handle_screen_use_state(
            ("superseded", 1, True, superseded_event, threading.Event())
        )
        self.assertFalse(app._screen_use_stop_requested)
        app.handle_screen_use_state(("superseded", 1, False, superseded_event, None))

        new_event = threading.Event()
        app.handle_screen_use_state(("new", 2, True, new_event, threading.Event()))
        self.assertFalse(new_event.is_set())

    def test_window_close_waits_for_screen_cleanup_without_modal_focus_steal(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        cancel_event = threading.Event()
        app._screen_use_tokens = {"screen": (1, cancel_event)}
        app._screen_use_stop_requested = False
        app._screen_use_key_reader = None
        app._screen_use_key_down = False
        app._screen_use_poll_after_id = None
        app._close_requested = False
        app._close_deadline = None
        app._close_poll_after_id = None
        app.active_run = None
        app.status_var = SimpleNamespace(set=lambda value: setattr(app, "last_status", value))
        app.log = Mock()
        app._render_screen_use_guard = lambda: None
        app.after = lambda *_args: "close-poll"
        app.destroy = Mock()

        app.request_close()
        self.assertTrue(cancel_event.is_set())
        app.destroy.assert_not_called()

        app._close_poll_after_id = None
        app._close_deadline = time.monotonic() - 1
        with patch("kakao_sender_gui.messagebox.showwarning") as showwarning:
            app._poll_close()
        showwarning.assert_not_called()
        app.destroy.assert_not_called()

        app._screen_use_tokens.clear()
        app._poll_close()
        app.destroy.assert_called_once()

    def test_pre_send_manual_scope_covers_missing_unconfirmed_and_unsafe_only(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.rows = [
            MessageRow(2, "정상", "one"),
            MessageRow(3, "라벨 없음", "two"),
            MessageRow(4, "매핑 없음", "three"),
            MessageRow(5, "별칭 변경", "four"),
            MessageRow(6, "범위 밖", "five"),
        ]
        app.direct_mappings = {
            "정상": "101",
            "라벨 없음": "202",
            "별칭 변경": "303",
        }
        app.direct_mapping_labels = {
            "101": "정상",
            "303": "예전 표시명",
        }
        app.guarded_mapping_ids = ("303",)
        chats = (
            {
                "chat_id": "303",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "9003",
                "peer_names": ["새 표시명"],
                "title": "새 표시명",
            },
        )

        unresolved = app.manual_mapping_unresolved_names(
            chats,
            target_names=("정상", "라벨 없음", "매핑 없음", "별칭 변경"),
        )

        self.assertEqual(unresolved, ("라벨 없음", "매핑 없음", "별칭 변경"))

    def test_before_send_opens_manual_only_for_unresolved_then_excludes_remainder(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        selected = [
            (0, MessageRow(2, "자동 성공", "one")),
            (1, MessageRow(3, "수동 필요", "two")),
        ]
        app.rows = [row for _, row in selected]
        app.direct_mappings = {"자동 성공": "101"}
        app.direct_mapping_labels = {"101": "자동 성공"}
        app.guarded_mapping_ids = ()
        app._latest_chat_snapshot = (
            {
                "chat_id": "101",
                "type": "DirectChat",
                "active_members": 2,
                "peer_user_id": "9001",
                "peer_names": ["자동 성공"],
                "title": "자동 성공",
            },
        )
        app.direct_names_for_selected = lambda _selected: ("자동 성공", "수동 필요")
        app.set_auto_match_send_wait = Mock()
        app.status_var = SimpleNamespace(set=Mock())
        app.log = Mock()
        app.show_manual_chat_mapping_dialog = Mock()
        app.start_sending_after_preflight = Mock()

        app.continue_start_after_auto_match(selected, True)

        app.set_auto_match_send_wait.assert_not_called()
        call = app.show_manual_chat_mapping_dialog.call_args
        self.assertEqual(call.kwargs["target_names"], ("수동 필요",))
        app.start_sending_after_preflight.assert_not_called()

        call.kwargs["on_close"]()

        app.set_auto_match_send_wait.assert_called_once_with(False)
        app.start_sending_after_preflight.assert_called_once_with(
            selected,
            blocked_direct_names=("수동 필요",),
        )

    def test_saved_room_missing_from_server_does_not_open_remapping_or_exclude_recipient(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        selected = [(0, MessageRow(2, "저장된 별칭", "one"))]
        app.rows = [row for _, row in selected]
        app.direct_mappings = {"저장된 별칭": "101"}
        app.direct_mapping_labels = {"101": "상대 이름"}
        app.guarded_mapping_ids = ("101",)
        app._latest_chat_snapshot = (self._manual_direct("202"),)
        app.direct_names_for_selected = lambda _: ("저장된 별칭",)
        app.set_auto_match_send_wait = Mock()
        app.status_var = SimpleNamespace(set=Mock())
        app.log = Mock()
        app.show_manual_chat_mapping_dialog = Mock()
        app.start_sending_after_preflight = Mock()
        app.continue_start_after_auto_match(selected, True)
        app.show_manual_chat_mapping_dialog.assert_not_called()
        app.start_sending_after_preflight.assert_not_called()
        app.set_auto_match_send_wait.assert_called_once_with(False)
        self.assertEqual(app.direct_mappings, {"저장된 별칭": "101"})
        self.assertIn("저장 ID 유지", app.status_var.set.call_args.args[0])
        app._latest_chat_snapshot = ()
        app.continue_start_after_auto_match(selected, True)
        app.show_manual_chat_mapping_dialog.assert_not_called()
        app.start_sending_after_preflight.assert_not_called()

    def test_direct_worker_excludes_unmatched_and_stops_batch_after_dispatch_failure(self):
        class Bridge:
            def __init__(self, failure):
                self.calls = []
                self.failure = failure
                self.closed = False

            def send_message(self, *, text, chat_id):
                self.calls.append((text, chat_id))
                if text == "failure":
                    raise self.failure
                return {
                    "success": True, "status_code": 0, "chat_id": chat_id,
                    "log_id": str(9000 + len(self.calls)), "sent_at": 1800000000,
                }

            def close(self):
                self.closed = True

        rows = (
            (0, MessageRow(2, "미매칭", "blocked")),
            (1, MessageRow(3, "성공", "ok")),
            (2, MessageRow(4, "실패 대상", "failure")),
            (3, MessageRow(5, "후속 대상", "must remain unattempted")),
        )
        request = SendRunRequest(
            selected=rows,
            config={},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=False,
            confirm_each=False,
            direct_enabled=True,
            chat_mappings=(("성공", "101"), ("실패 대상", "202"), ("후속 대상", "303")),
            blocked_direct_names=("미매칭",),
        )

        cases = (
            (DirectSendError(code="server_rejected", status_code=-777), "error", [2]),
            (DirectSendUnknownError(code="process_disconnected"), "unknown", []),
            (DirectBridgeError(code="process_disconnected"), "unknown", []),
        )
        for failure, failure_status, retry_indexes in cases:
            with self.subTest(failure=type(failure).__name__):
                bridge = Bridge(failure)
                app = KakaoSenderApp.__new__(KakaoSenderApp)
                app.stop_event = threading.Event()
                app.results = []
                events = []
                app.post = lambda event, payload: events.append((event, payload))
                app.create_direct_bridge = lambda _config=None: bridge
                virtual_time = 0.0
                waits = []

                def wait_for_cancel(event, seconds):
                    nonlocal virtual_time
                    waits.append(seconds)
                    virtual_time += seconds
                    return event.is_set()

                with (
                    patch("kakao_sender_gui.SendPorts", side_effect=lambda **ports: SendPorts(
                        **ports, monotonic=lambda: virtual_time, wait_for_cancel=wait_for_cancel,
                    )),
                    patch("kakao_sender_gui.require_pyautogui") as require_pyautogui,
                ):
                    completed = app.send_worker(request)

                results = completed.legacy_results()
                self.assertIsNone(completed.failure)
                self.assertEqual([item["status"] for item in results], ["error", "direct_sent", failure_status, "stopped"])
                self.assertEqual(macro_retry_result_indexes(results, [row for _, row in rows]), retry_indexes)
                self.assertEqual(row_status_tag("매칭 실패"), "error")
                self.assertEqual(results[0]["failure_stage"], "mapping")
                self.assertEqual(results[0]["retry_transport"], "none")
                # Once a real dispatch rejects or becomes uncertain, no later row is attempted.
                self.assertEqual(results[3]["failure_stage"], "direct_batch_not_attempted")
                self.assertEqual(results[3]["retry_transport"], "none")
                self.assertEqual(bridge.calls, [("ok", "101"), ("failure", "202")])
                self.assertEqual(waits, [])
                self.assertTrue(bridge.closed)
                require_pyautogui.assert_not_called()
                self.assertEqual(events[-1], ("run_event", completed))

    def test_direct_bridge_initialization_failure_collects_every_direct_row_for_macro_retry(self):
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        app.post = Mock()
        app.create_direct_bridge = Mock(
            side_effect=DirectBridgeError("secret must not surface", code="credentials_missing")
        )
        rows = (
            (0, MessageRow(2, "첫째", "one")),
            (1, MessageRow(3, "둘째", "two")),
        )
        request = SendRunRequest(
            selected=rows,
            config={},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=False,
            confirm_each=False,
            direct_enabled=True,
            chat_mappings=(("첫째", "101"), ("둘째", "202")),
        )

        with patch("kakao_sender_gui.require_pyautogui") as require_pyautogui:
            completed = app.send_worker(request)

        results = completed.legacy_results()
        self.assertIsNone(completed.failure)
        self.assertEqual([item["status"] for item in results], ["error", "error"])
        self.assertEqual(macro_retry_result_indexes(results, [row for _, row in rows]), [0, 1])
        self.assertTrue(all("secret" not in item["detail"] for item in results))
        require_pyautogui.assert_not_called()

    def test_macro_retry_forces_send_one_without_direct_bridge_or_schedule(self):
        due_at = datetime.now().replace(second=0, microsecond=0) + timedelta(days=1)
        row = MessageRow(2, "재시도", "화면 즉시", scheduled_at=due_at)
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        app.post = Mock()
        app.create_direct_bridge = Mock(side_effect=AssertionError("must not create direct bridge"))
        request = SendRunRequest(
            selected=((0, row),),
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=0,
            delay_scale=1.0,
            common_schedule=due_at,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=False,
            send_mode="macro_retry",
        )
        fake_pyautogui = type("FakePyAutoGui", (), {"PAUSE": 0.0})()

        with (
            patch("kakao_sender_gui.require_pyautogui", return_value=fake_pyautogui),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender_gui.send_one") as send_immediately,
            patch("kakao_sender_gui.send_scheduled_one") as send_scheduled,
            patch("kakao_sender_gui.return_to_recent_chat_top"),
        ):
            completed = app.send_worker(request)

        app.create_direct_bridge.assert_not_called()
        send_immediately.assert_called_once()
        send_scheduled.assert_not_called()
        self.assertEqual(completed.legacy_results()[0]["status"], "sent")
        self.assertEqual(completed.legacy_results()[0]["scheduled_at"], "")

    def test_macro_retry_failure_stays_in_second_pass_queue(self):
        row = MessageRow(2, "재시도", "화면 즉시")
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        app.post = Mock()
        app.create_direct_bridge = Mock(side_effect=AssertionError("must not create direct bridge"))
        request = SendRunRequest(
            selected=((0, row),),
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=False,
            confirm_each=False,
            direct_enabled=False,
            send_mode="macro_retry",
        )
        fake_pyautogui = type("FakePyAutoGui", (), {"PAUSE": 0.0})()

        with (
            patch("kakao_sender_gui.require_pyautogui", return_value=fake_pyautogui),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
            patch("kakao_sender_gui.send_one", side_effect=RuntimeError("screen failed")),
            patch("kakao_sender_gui.return_to_recent_chat_top"),
        ):
            completed = app.send_worker(request)

        results = completed.legacy_results()
        self.assertEqual(macro_retry_result_indexes(results, [row]), [0])
        self.assertEqual(results[0]["failure_stage"], "macro_retry")
        app.create_direct_bridge.assert_not_called()

    def test_macro_retry_fatal_first_row_preserves_unattempted_queue_without_more_sends(self):
        rows = tuple(
            (index, MessageRow(index + 2, f"재시도 {index}", f"message {index}"))
            for index in range(3)
        )
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        app.post = Mock()
        request = SendRunRequest(
            selected=rows,
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=False,
            send_mode="macro_retry",
        )
        fake_pyautogui = type("FakePyAutoGui", (), {"PAUSE": 0.0})()

        with (
            patch("kakao_sender_gui.require_pyautogui", return_value=fake_pyautogui),
            patch("kakao_sender_gui.focus_kakao_window", return_value="KakaoTalk"),
            patch(
                "kakao_sender_gui.send_one",
                side_effect=FocusSafetyError("카카오톡 메인 창을 찾지 못했습니다."),
            ) as send_immediately,
            patch("kakao_sender_gui.return_to_recent_chat_top"),
        ):
            completed = app.send_worker(request)

        results = completed.legacy_results()
        self.assertEqual(send_immediately.call_count, 1)
        self.assertEqual(macro_retry_result_indexes(results, [row for _, row in rows]), [0, 1, 2])
        self.assertEqual(results[0]["failure_stage"], "macro_retry")
        self.assertEqual(
            [item["failure_stage"] for item in results[1:]],
            ["macro_retry_not_attempted", "macro_retry_not_attempted"],
        )

    def test_macro_retry_cancelled_before_start_preserves_every_row(self):
        rows = tuple(
            (index, MessageRow(index + 2, f"재시도 {index}", f"message {index}"))
            for index in range(3)
        )
        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.stop_event = threading.Event()
        app.results = []
        app.post = Mock()
        request = SendRunRequest(
            selected=rows,
            config={"app_titles": ["KakaoTalk"], "require_main_window_focus": True},
            countdown=0,
            delay_scale=1.0,
            common_schedule=None,
            continue_on_error=True,
            confirm_each=False,
            direct_enabled=False,
            send_mode="macro_retry",
        )
        supervisor = SendRunSupervisor(lambda event: app.post("run_event", event))
        supervisor.request_cancel()

        with patch("kakao_sender_gui.require_pyautogui") as require_pyautogui:
            completed = app.send_worker(request, supervisor)

        self.assertTrue(completed.cancelled)
        self.assertEqual(
            macro_retry_result_indexes(completed.legacy_results(), [row for _, row in rows]),
            [0, 1, 2],
        )
        self.assertTrue(
            all(item["failure_stage"] == "macro_retry_not_attempted" for item in completed.legacy_results())
        )
        require_pyautogui.assert_not_called()

    def test_auto_match_failure_diagnostic_uses_safe_code_and_next_action(self):
        failure = safe_auto_match_failure(
            DirectBridgeError("token=do-not-log", code="credentials_missing")
        )

        self.assertIsInstance(failure, AutoMatchFailure)
        self.assertEqual(failure.code, "credentials_missing")
        self.assertIn("인증정보", failure.cause)
        self.assertIn("확인", failure.next_action)
        self.assertNotIn("do-not-log", repr(failure))

    def test_auto_match_refresh_rejection_explains_tablet_recapture(self):
        for code in (
            "refresh_rejected",
            "invalid_access_token",
            "login_failed",
            "refresh_unavailable",
        ):
            with self.subTest(code=code):
                failure = safe_auto_match_failure(DirectBridgeError("sensitive", code=code))
                self.assertIn("만료", failure.cause)
                self.assertIn("Spoiler", failure.next_action)
                self.assertIn("태블릿 설정", failure.next_action)
                self.assertIn("다시 등록", failure.next_action)
                self.assertNotIn("sensitive", repr(failure))

    def test_direct_mode_keeps_coordinate_button_name_and_explains_fallback_uses(self):
        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Button:
            def __init__(self):
                self.text = ""

            def configure(self, **kwargs):
                self.text = kwargs.get("text", self.text)

            def state(self, _state):
                return None

        app = KakaoSenderApp.__new__(KakaoSenderApp)
        app.direct_enabled_var = Var(True)
        app.direct_warning_confirmed = True
        app.direct_mode_status_var = Var("")
        app.schedule_mode_hint_var = Var("")
        app.coordinate_button = Button()

        app.on_direct_mode_changed(persist=False)

        self.assertEqual(app.coordinate_button.text, "화면 좌표 설정")
        self.assertIn("즉시 텍스트·첨부 직접 / 예약은 화면 자동 등록", app.direct_mode_status_var.get())
        self.assertIn("대상/시간/본문 자동 대조", app.schedule_mode_hint_var.get())


if __name__ == "__main__":
    unittest.main()
