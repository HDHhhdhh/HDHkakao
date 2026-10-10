from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from kakao_direct import DirectKakaoConfig
from kakao_sender_gui import PcChatFallbackResult
from kakao_mapping_policy import normalize_chat_match_name, unverified_alias_mapping_names
from refresh_student_chat_mappings import (
    ConfirmedPcSelection,
    DistinctPairConfirmation,
    PcProbeResult,
    SnapshotResult,
    apply_plan,
    apply_pc_probe_to_plan,
    build_refresh_plan,
    load_target_names,
    probe_pc_targets,
)


def direct_chat(chat_id: str, title: str, *, peer_name: str | None = None, **extra: object) -> dict[str, object]:
    """Create the smallest server row accepted as a verified direct chat."""

    row: dict[str, object] = {
        "chat_id": chat_id,
        "title": title,
        "type": "DirectChat",
        "active_members": 2,
        "peer_user_id": "123456",
        "peer_names": [peer_name or title],
    }
    row.update(extra)
    return row


def snapshot(*chats: dict[str, object]) -> SnapshotResult:
    return SnapshotResult(
        chats=tuple(chats),
        base_count=len(chats),
        catalog_candidate_count=0,
        supplemental_requested_count=0,
        supplemental_verified_count=0,
    )


PAIR_TARGET = "A 저동2 학생나"
PAIR_OWNER = "A 주엽2 학생나B"


def pair_probe(
    *,
    chat_id: str = "1602",
    pc_title: str = PAIR_TARGET,
    candidate_peer_id: str = "1602001",
    owner_name: str = PAIR_OWNER,
    owner_chat_id: str = "1601",
    owner_peer_id: str = "1601001",
    target: str = PAIR_TARGET,
) -> PcProbeResult:
    """Build bound PC/server evidence expected by the pair-confirmation seam."""

    return PcProbeResult(
        mappings={target: chat_id},
        mapping_labels={chat_id: pc_title},
        unresolved=(),
        confirmed_selections=(
            ConfirmedPcSelection(
                target_name=target,
                chat_id=chat_id,
                pc_title=pc_title,
                peer_user_id=candidate_peer_id,
                protected_owner_name=owner_name,
                protected_chat_id=owner_chat_id,
                protected_peer_user_id=owner_peer_id,
                protected_mapping_label="학생나B",
                protected_was_guarded=True,
            ),
        ),
    )


class LoadTargetNamesTests(unittest.TestCase):
    def test_reads_only_chat_name_column_and_ignores_message_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "students.csv"
            path.write_text(
                "카카오톡 대화명,텍스트,첨부\n홍길동,민감한 내용,secret.png\n김철수,,\n",
                encoding="utf-8-sig",
            )

            self.assertEqual(load_target_names(path), ("홍길동", "김철수"))

    def test_rejects_empty_name_and_normalized_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_path = Path(temp_dir) / "empty-name.csv"
            empty_path.write_text("대화명,텍스트\n,메시지\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty chat name"):
                load_target_names(empty_path)

            duplicate_path = Path(temp_dir) / "duplicate.csv"
            duplicate_path.write_text("대화명\n홍길동\n 홍길동 \n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate normalized"):
                load_target_names(duplicate_path)


class RefreshPlanTests(unittest.TestCase):
    def test_cli_import_does_not_load_tk_or_gui(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys; import refresh_student_chat_mappings; "
                "assert 'kakao_sender_gui' not in sys.modules; "
                "assert 'tkinter' not in sys.modules",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_guarded_label_change_stays_unsafe_and_cannot_be_applied(self) -> None:
        config = DirectKakaoConfig(
            mappings={"Synthetic target": "101"},
            mapping_labels={"101": "Original synthetic alias"},
            guarded_mapping_ids=("101",),
        )
        changed_chat = direct_chat("101", "Changed synthetic alias")
        plan = build_refresh_plan(config, ("Synthetic target",), snapshot(changed_chat))

        self.assertFalse(plan.ready_to_apply)
        self.assertEqual(plan.unsafe, ("Synthetic target",))
        self.assertEqual(plan.final_config.mapping_labels, config.mapping_labels)
        self.assertEqual(
            unverified_alias_mapping_names(
                plan.target_names,
                plan.final_config.mappings,
                plan.final_config.mapping_labels,
                (changed_chat,),
                guarded_mapping_ids=plan.final_config.guarded_mapping_ids,
            ),
            ("Synthetic target",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "direct.json"
            config.save(config_path)
            original_bytes = config_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "not fully verified"):
                apply_plan(config_path, original_bytes, plan)
            self.assertEqual(config_path.read_bytes(), original_bytes)
            self.assertEqual(list(Path(temporary).glob("*.bak")), [])

    def test_unguarded_label_still_refreshes_and_guarded_unchanged_label_verifies(self) -> None:
        for guarded, current_label in (((), "Updated synthetic alias"), (("101",), "Original synthetic alias")):
            with self.subTest(guarded=bool(guarded)):
                config = DirectKakaoConfig(
                    mappings={"Synthetic target": "101"},
                    mapping_labels={"101": "Original synthetic alias"},
                    guarded_mapping_ids=guarded,
                )
                plan = build_refresh_plan(config, ("Synthetic target",), snapshot(direct_chat("101", current_label)))
                self.assertTrue(plan.ready_to_apply)
                self.assertEqual(plan.final_config.mapping_labels, {"101": current_label})

    def test_preserves_unrelated_mapping_when_refreshing_selected_targets(self) -> None:
        config = DirectKakaoConfig(
            mappings={"대상 학생": "101", "다른 학생": "202"},
            mapping_labels={"101": "대상 학생", "202": "다른 학생"},
        )
        plan = build_refresh_plan(
            config,
            ("대상 학생",),
            snapshot(
                direct_chat("101", "대상 학생"),
                direct_chat("202", "다른 학생"),
            ),
        )

        self.assertTrue(plan.ready_to_apply)
        self.assertEqual(plan.final_config.mappings[normalize_chat_match_name("대상 학생")], "101")
        self.assertEqual(plan.final_config.mappings[normalize_chat_match_name("다른 학생")], "202")
        self.assertEqual(plan.final_config.mapping_labels, {"101": "대상 학생", "202": "다른 학생"})
        self.assertEqual(plan.changed_target_count, 0)

    def test_plan_is_ready_when_every_csv_target_is_server_verified(self) -> None:
        config = DirectKakaoConfig()
        plan = build_refresh_plan(
            config,
            ("홍길동", "김철수"),
            snapshot(
                direct_chat("301", "홍길동"),
                direct_chat("302", "김철수"),
            ),
        )

        self.assertTrue(plan.ready_to_apply)
        self.assertEqual(plan.verified_target_count, 2)
        self.assertEqual(plan.missing, ())
        self.assertEqual(plan.unsafe, ())
        self.assertEqual(plan.ambiguous, ())
        self.assertEqual(plan.final_config.mappings, {"홍길동": "301", "김철수": "302"})
        self.assertEqual(plan.to_safe_dict()["status"], "COMPLETE")

    def test_plan_is_not_ready_when_one_target_is_missing_or_unsafe(self) -> None:
        config = DirectKakaoConfig()
        plan = build_refresh_plan(
            config,
            ("홍길동", "김철수"),
            snapshot(
                {"chat_id": "401", "title": "홍길동", "type": "MultiChat", "active_members": 3},
            ),
        )

        self.assertFalse(plan.ready_to_apply)
        self.assertIn("김철수", plan.missing)
        self.assertIn("홍길동", plan.unsafe)
        self.assertEqual(plan.verified_target_count, 0)
        self.assertFalse(plan.to_safe_dict()["ready_to_apply"])
        self.assertEqual(plan.to_safe_dict()["status"], "PARTIAL")

    def test_exact_pc_probe_title_applies_verified_candidate_and_guards_id_without_sending(self) -> None:
        config = DirectKakaoConfig()
        plan = build_refresh_plan(
            config,
            ("PC Alias",),
            snapshot(direct_chat("501", "홍길동")),
        )
        self.assertEqual(plan.missing, ("PC Alias",))

        # Mock only the PC resolver boundary.  The candidate itself still has
        # to pass the fresh, server-backed manual-candidate validation below.
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("refresh_student_chat_mappings.CONFIG_PATH", Path(temp_dir) / "runtime.json"),
                patch(
                    "refresh_student_chat_mappings.load_relaxed_config_data",
                    return_value=({"search_box": {"x": 100, "y": 100}}, None),
                ),
                patch(
                    "refresh_student_chat_mappings.resolve_pc_chat_ids_via_window",
                    return_value=PcChatFallbackResult(
                        mappings={"PC Alias": "501"},
                        unresolved=(),
                        mapping_labels={"501": "PC Alias"},
                    ),
                ) as resolver,
                patch("kakao_sender_gui.send_one") as send_one,
                patch("kakao_sender_gui.send_scheduled_one") as send_scheduled_one,
            ):
                probe = probe_pc_targets(plan)
                refreshed = apply_pc_probe_to_plan(plan, probe)

        resolver.assert_called_once()
        self.assertEqual(probe.mappings, {"PC Alias": "501"})
        self.assertTrue(refreshed.ready_to_apply)
        self.assertEqual(refreshed.final_config.mappings["pc alias"], "501")
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("501",))
        send_one.assert_not_called()
        send_scheduled_one.assert_not_called()

    def test_mismatched_pc_title_is_rejected_and_does_not_guard_id(self) -> None:
        plan = build_refresh_plan(
            DirectKakaoConfig(),
            ("PC Alias",),
            snapshot(direct_chat("511", "홍길동")),
        )
        refreshed = apply_pc_probe_to_plan(
            plan,
            PcProbeResult({"PC Alias": "511"}, {"511": "Different PC Title"}, ()),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(refreshed.final_config.mappings, {})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ())
        self.assertIn("PC Alias", refreshed.missing)

    def test_manual_owned_id_is_rejected_even_when_pc_probe_label_matches(self) -> None:
        config = DirectKakaoConfig(
            mappings={"Manual Alias": "521"},
            mapping_labels={"521": "Saved Manual Alias"},
        )
        plan = build_refresh_plan(
            config,
            ("PC Alias",),
            snapshot(direct_chat("521", "홍길동")),
        )
        refreshed = apply_pc_probe_to_plan(
            plan,
            PcProbeResult({"PC Alias": "521"}, {"521": "PC Alias"}, ()),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(refreshed.final_config.mappings, {"manual alias": "521"})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ())
        self.assertIn("PC Alias", refreshed.missing)

    def test_guarded_target_keeps_old_id_when_same_name_reappears_under_new_id(self) -> None:
        config = DirectKakaoConfig(
            mappings={"대상": "701"},
            mapping_labels={"701": "대상"},
            guarded_mapping_ids=("701",),
        )
        plan = build_refresh_plan(
            config,
            ("대상",),
            snapshot(direct_chat("702", "대상", peer_user_id="702001")),
        )

        self.assertFalse(plan.ready_to_apply)
        self.assertIn("대상", plan.unsafe)
        self.assertEqual(plan.final_config.mappings, {"대상": "701"})
        self.assertEqual(plan.final_config.mapping_labels, {"701": "대상"})
        self.assertEqual(plan.final_config.guarded_mapping_ids, ("701",))

        # A PC probe cannot replace a guarded mapping when the old stable ID
        # disappeared from the snapshot and a same-name ID appeared.
        refreshed = apply_pc_probe_to_plan(
            plan,
            PcProbeResult({"대상": "702"}, {"702": "대상"}, ()),
        )
        self.assertFalse(refreshed.ready_to_apply)
        self.assertIn("대상", refreshed.unsafe)
        self.assertEqual(refreshed.final_config.mappings, {"대상": "701"})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("701",))

    def test_ambiguous_verified_duplicate_titles_stay_partial_after_pc_probe(self) -> None:
        plan = build_refresh_plan(
            DirectKakaoConfig(),
            ("홍길동",),
            snapshot(
                direct_chat("601", "홍길동", peer_user_id="601001"),
                direct_chat("602", "홍길동", peer_user_id="602001"),
            ),
        )
        self.assertEqual(plan.ambiguous, ("홍길동",))

        refreshed = apply_pc_probe_to_plan(
            plan,
            PcProbeResult({"홍길동": "602"}, {"602": "홍길동"}, ()),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(refreshed.to_safe_dict()["status"], "PARTIAL")
        self.assertEqual(refreshed.final_config.mappings, {})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ())
        self.assertEqual(refreshed.ambiguous, ("홍길동",))

    def test_ambiguous_pc_result_without_explicit_confirmation_stays_partial(self) -> None:
        plan = build_refresh_plan(
            DirectKakaoConfig(),
            ("A 저동2 학생나",),
            snapshot(
                direct_chat("1001", "학생나", peer_user_id="1001001"),
                direct_chat("1002", "학생나", peer_user_id="1002001"),
            ),
        )
        self.assertEqual(plan.ambiguous, ("A 저동2 학생나",))

        refreshed = apply_pc_probe_to_plan(
            plan,
            PcProbeResult({"A 저동2 학생나": "1001"}, {"1001": "A 저동2 학생나"}, ()),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(refreshed.to_safe_dict()["status"], "PARTIAL")
        self.assertEqual(refreshed.final_config.mappings, {})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ())
        self.assertEqual(refreshed.ambiguous, ("A 저동2 학생나",))

    def test_exact_pair_confirmation_allows_only_selected_unowned_verified_candidate(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1601"},
            mapping_labels={"1601": "학생나B"},
            guarded_mapping_ids=("1601",),
        )
        plan = build_refresh_plan(
            config,
            (PAIR_TARGET, PAIR_OWNER),
            snapshot(
                direct_chat("1601", "학생나B", peer_name="학생나B", peer_user_id="1601001"),
                direct_chat("1602", "학생나", peer_name="학생나", peer_user_id="1602001"),
                direct_chat("1603", "학생나", peer_name="학생나", peer_user_id="1603001"),
            ),
        )
        self.assertIn(PAIR_TARGET, plan.ambiguous)
        confirmation = DistinctPairConfirmation(PAIR_TARGET, PAIR_OWNER)

        # The resolver is mocked at the PC boundary; probe/apply must bind its
        # result to the server snapshot and the exact pair confirmation.
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("refresh_student_chat_mappings.CONFIG_PATH", Path(temp_dir) / "runtime.json"),
                patch(
                    "refresh_student_chat_mappings.load_relaxed_config_data",
                    return_value=({"search_box": {"x": 100, "y": 100}}, None),
                ),
                patch(
                    "refresh_student_chat_mappings.resolve_pc_chat_ids_via_window",
                    return_value=PcChatFallbackResult(
                        mappings={PAIR_TARGET: "1602"},
                        unresolved=(),
                        mapping_labels={"1602": PAIR_TARGET},
                    ),
                ) as resolver,
            ):
                probe = probe_pc_targets(plan, distinct_pair_confirmations=(confirmation,))
                refreshed = apply_pc_probe_to_plan(plan, probe)

        resolver.assert_called_once()
        self.assertTrue(refreshed.ready_to_apply)
        self.assertEqual(
            refreshed.final_config.mappings,
            {"a 주엽2 학생나b": "1601", "a 저동2 학생나": "1602"},
        )
        self.assertEqual(refreshed.final_config.mapping_labels["1601"], "학생나B")
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("1601", "1602"))

    def test_confirmation_for_one_target_cannot_authorize_another_csv_target(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1651"},
            mapping_labels={"1651": "학생나B"},
            guarded_mapping_ids=("1651",),
        )
        other_target = "A 제3반 박서준"
        plan = build_refresh_plan(
            config,
            (PAIR_TARGET, other_target),
            snapshot(
                direct_chat("1651", "학생나B", peer_name="학생나B", peer_user_id="1651001"),
                direct_chat("1652", "학생나", peer_name="학생나", peer_user_id="1652001"),
                direct_chat("1653", "학생나", peer_name="학생나", peer_user_id="1653001"),
            ),
        )
        self.assertIn(PAIR_TARGET, plan.ambiguous)
        self.assertIn(other_target, plan.missing)

        evidence = pair_probe(
            chat_id="1652",
            owner_chat_id="1651",
            candidate_peer_id="1652001",
            owner_peer_id="1651001",
        )
        forged = replace(
            evidence,
            mappings={other_target: "1652"},
            confirmed_selections=(replace(evidence.confirmed_selections[0], target_name=PAIR_TARGET),),
        )
        refreshed = apply_pc_probe_to_plan(plan, forged)

        self.assertFalse(refreshed.ready_to_apply)
        self.assertNotIn(other_target.casefold(), refreshed.final_config.mappings)
        self.assertNotIn(PAIR_TARGET.casefold(), refreshed.final_config.mappings)
        self.assertEqual(refreshed.final_config.mappings, {"a 주엽2 학생나b": "1651"})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("1651",))

    def test_confirmed_candidate_owned_by_kim_jimin_b_preserves_owner_and_guard(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1701"},
            mapping_labels={"1701": "학생나B"},
            guarded_mapping_ids=("1701",),
        )
        plan = build_refresh_plan(
            config,
            (PAIR_TARGET, PAIR_OWNER),
            snapshot(direct_chat("1701", "학생나B", peer_name="학생나B", peer_user_id="1701001")),
        )

        refreshed = apply_pc_probe_to_plan(
            plan,
            pair_probe(
                chat_id="1701",
                owner_chat_id="1701",
                candidate_peer_id="1701001",
                owner_peer_id="1701001",
            ),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(refreshed.final_config.mappings, {"a 주엽2 학생나b": "1701"})
        self.assertEqual(refreshed.final_config.mapping_labels, {"1701": "학생나B"})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("1701",))
        self.assertIn(PAIR_TARGET, refreshed.missing)

    def test_candidate_peer_must_differ_from_protected_and_all_existing_mapped_peers(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1801", "다른 학생": "1802"},
            mapping_labels={"1801": "학생나B", "1802": "다른 학생"},
            guarded_mapping_ids=("1801",),
        )
        plan = build_refresh_plan(
            config,
            (PAIR_TARGET, PAIR_OWNER),
            snapshot(
                direct_chat("1801", "학생나B", peer_name="학생나B", peer_user_id="1801001"),
                direct_chat("1802", "다른 학생", peer_name="다른 학생", peer_user_id="1802001"),
                direct_chat("1803", "학생나", peer_name="학생나", peer_user_id="1801001"),
                direct_chat("1804", "학생나", peer_name="학생나", peer_user_id="1804001"),
            ),
        )
        self.assertIn(PAIR_TARGET, plan.ambiguous)

        refreshed = apply_pc_probe_to_plan(
            plan,
            pair_probe(
                chat_id="1803",
                owner_chat_id="1801",
                candidate_peer_id="1801001",
                owner_peer_id="1801001",
            ),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(
            refreshed.final_config.mappings,
            {"a 주엽2 학생나b": "1801", "다른 학생": "1802"},
        )
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("1801",))
        self.assertIn(PAIR_TARGET, refreshed.ambiguous)

    def test_pc_title_is_raw_nfkc_whitespace_exact_and_does_not_cross_match_kim_jimin_b(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1901"},
            mapping_labels={"1901": "학생나B"},
            guarded_mapping_ids=("1901",),
        )
        plan = build_refresh_plan(
            config,
            (PAIR_TARGET, PAIR_OWNER),
            snapshot(
                direct_chat("1901", "학생나B", peer_name="학생나B", peer_user_id="1901001"),
                direct_chat("1902", "학생나", peer_name="학생나", peer_user_id="1902001"),
                direct_chat("1903", "학생나", peer_name="학생나", peer_user_id="1903001"),
            ),
        )
        refreshed = apply_pc_probe_to_plan(
            plan,
            pair_probe(
                chat_id="1902",
                owner_chat_id="1901",
                pc_title="A 저동2 학생나B",
                candidate_peer_id="1902001",
                owner_peer_id="1901001",
            ),
        )

        self.assertFalse(refreshed.ready_to_apply)
        self.assertEqual(refreshed.final_config.mappings, {"a 주엽2 학생나b": "1901"})
        self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("1901",))
        self.assertIn(PAIR_TARGET, refreshed.ambiguous)

    def test_invalid_pair_confirmation_unknown_duplicate_nonambiguous_or_unprotected_fails_closed(self) -> None:
        ambiguous_plan = build_refresh_plan(
            DirectKakaoConfig(mappings={PAIR_OWNER: "2001"}, mapping_labels={"2001": "학생나B"}),
            (PAIR_TARGET, PAIR_OWNER),
            snapshot(
                direct_chat("2001", "학생나B", peer_name="학생나B", peer_user_id="2001001"),
                direct_chat("2002", "학생나", peer_name="학생나", peer_user_id="2002001"),
                direct_chat("2003", "학생나", peer_name="학생나", peer_user_id="2003001"),
            ),
        )
        nonambiguous_plan = build_refresh_plan(
            DirectKakaoConfig(mappings={PAIR_OWNER: "2101"}, mapping_labels={"2101": "학생나B"}),
            (PAIR_TARGET, PAIR_OWNER),
            snapshot(direct_chat("2101", "학생나B", peer_name="학생나B", peer_user_id="2101001")),
        )

        cases = (
            (ambiguous_plan, (DistinctPairConfirmation("없는 대상", PAIR_OWNER),)),
            (ambiguous_plan, (DistinctPairConfirmation(PAIR_TARGET, PAIR_OWNER),) * 2),
            (nonambiguous_plan, (DistinctPairConfirmation(PAIR_TARGET, PAIR_OWNER),)),
            (
                build_refresh_plan(
                    DirectKakaoConfig(),
                    (PAIR_TARGET, PAIR_OWNER),
                    snapshot(
                        direct_chat("2201", "학생나", peer_name="학생나", peer_user_id="2201001"),
                        direct_chat("2202", "학생나", peer_name="학생나", peer_user_id="2202001"),
                    ),
                ),
                (DistinctPairConfirmation(PAIR_TARGET, PAIR_OWNER),),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("refresh_student_chat_mappings.CONFIG_PATH", Path(temp_dir) / "runtime.json"),
                patch(
                    "refresh_student_chat_mappings.load_relaxed_config_data",
                    return_value=({"search_box": {"x": 100, "y": 100}}, None),
                ),
            ):
                for plan, confirmations in cases:
                    with self.subTest(plan=plan, confirmations=confirmations), self.assertRaisesRegex(ValueError, "confirmation"):
                        probe_pc_targets(plan, distinct_pair_confirmations=confirmations)

    def test_confirmed_probe_is_rejected_when_fresh_candidate_peer_changes_or_disappears(self) -> None:
        original = snapshot(
            direct_chat("2301", "학생나B", peer_name="학생나B", peer_user_id="2301001"),
            direct_chat("2302", "학생나", peer_name="학생나", peer_user_id="2302001"),
            direct_chat("2303", "학생나", peer_name="학생나", peer_user_id="2303001"),
        )
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "2301"},
            mapping_labels={"2301": "학생나B"},
            guarded_mapping_ids=("2301",),
        )
        plan = build_refresh_plan(config, (PAIR_TARGET, PAIR_OWNER), original)
        evidence = pair_probe(chat_id="2302", owner_chat_id="2301")

        changed_peer = replace(
            plan,
            snapshot=snapshot(
                direct_chat("2301", "학생나B", peer_name="학생나B", peer_user_id="2301001"),
                direct_chat("2302", "학생나", peer_name="학생나", peer_user_id="2309999"),
                direct_chat("2303", "학생나", peer_name="학생나", peer_user_id="2303001"),
            ),
        )
        disappeared = replace(plan, snapshot=snapshot(original.chats[0]))
        for stale_plan in (changed_peer, disappeared):
            with self.subTest(snapshot=stale_plan.snapshot):
                refreshed = apply_pc_probe_to_plan(stale_plan, evidence)
                self.assertFalse(refreshed.ready_to_apply)
                self.assertEqual(refreshed.final_config.mappings, {"a 주엽2 학생나b": "2301"})
                self.assertEqual(refreshed.final_config.guarded_mapping_ids, ("2301",))
                self.assertIn(PAIR_TARGET, refreshed.ambiguous)

    def test_fresh_rebuild_does_not_replay_stale_evidence_to_remaining_candidate(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1601"},
            mapping_labels={"1601": "학생나B"},
            guarded_mapping_ids=("1601",),
        )
        initial_snapshot = snapshot(
            direct_chat("1601", "학생나B", peer_name="학생나B", peer_user_id="1601001"),
            direct_chat("1602", "학생나", peer_name="학생나", peer_user_id="1602001"),
            direct_chat("1603", "학생나", peer_name="학생나", peer_user_id="1603001"),
        )
        initial_plan = build_refresh_plan(config, (PAIR_TARGET, PAIR_OWNER), initial_snapshot)
        self.assertIn(PAIR_TARGET, initial_plan.ambiguous)
        evidence = pair_probe(chat_id="1602", owner_chat_id="1601")

        # The real main flow rebuilds from a fresh list before replaying PC
        # evidence.  The evidence ID is gone; only the other same-name room
        # remains and would otherwise auto-resolve to 1603.
        fresh_snapshot = snapshot(
            direct_chat("1601", "학생나B", peer_name="학생나B", peer_user_id="1601001"),
            direct_chat("1603", "학생나", peer_name="학생나", peer_user_id="1603001"),
        )
        fresh_plan = build_refresh_plan(config, (PAIR_TARGET, PAIR_OWNER), fresh_snapshot)
        self.assertTrue(fresh_plan.ready_to_apply)
        self.assertEqual(fresh_plan.final_config.mappings["a 저동2 학생나"], "1603")

        replayed = apply_pc_probe_to_plan(fresh_plan, evidence)

        self.assertFalse(replayed.ready_to_apply)
        self.assertNotEqual(replayed.final_config.mappings.get("a 저동2 학생나"), "1603")
        self.assertNotIn("a 저동2 학생나", replayed.final_config.mappings)
        self.assertEqual(replayed.final_config.guarded_mapping_ids, ("1601",))
        self.assertIn(PAIR_TARGET, replayed.missing + replayed.unsafe + replayed.ambiguous)

    def test_fresh_rebuild_with_evidence_candidate_forces_target_and_guard(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1611"},
            mapping_labels={"1611": "학생나B"},
            guarded_mapping_ids=("1611",),
        )
        fresh_snapshot = snapshot(
            direct_chat("1611", "학생나B", peer_name="학생나B", peer_user_id="1611001"),
            direct_chat("1612", "학생나", peer_name="학생나", peer_user_id="1612001"),
        )
        fresh_plan = build_refresh_plan(config, (PAIR_TARGET, PAIR_OWNER), fresh_snapshot)
        self.assertTrue(fresh_plan.ready_to_apply)
        self.assertEqual(fresh_plan.final_config.mappings["a 저동2 학생나"], "1612")

        replayed = apply_pc_probe_to_plan(
            fresh_plan,
            pair_probe(
                chat_id="1612",
                owner_chat_id="1611",
                candidate_peer_id="1612001",
                owner_peer_id="1611001",
            ),
        )

        self.assertTrue(replayed.ready_to_apply)
        self.assertEqual(replayed.final_config.mappings["a 저동2 학생나"], "1612")
        self.assertEqual(replayed.final_config.guarded_mapping_ids, ("1611", "1612"))

    def test_fresh_rebuild_blocks_replay_when_evidence_peer_identity_changes(self) -> None:
        config = DirectKakaoConfig(
            mappings={PAIR_OWNER: "1621"},
            mapping_labels={"1621": "학생나B"},
            guarded_mapping_ids=("1621",),
        )
        fresh_snapshot = snapshot(
            direct_chat("1621", "학생나B", peer_name="학생나B", peer_user_id="1621001"),
            direct_chat("1622", "학생나", peer_name="학생나", peer_user_id="1622999"),
        )
        fresh_plan = build_refresh_plan(config, (PAIR_TARGET, PAIR_OWNER), fresh_snapshot)
        self.assertTrue(fresh_plan.ready_to_apply)

        stale_evidence = pair_probe(
            chat_id="1622",
            owner_chat_id="1621",
            candidate_peer_id="1622001",
            owner_peer_id="1621001",
        )
        replayed = apply_pc_probe_to_plan(fresh_plan, stale_evidence)

        self.assertFalse(replayed.ready_to_apply)
        self.assertEqual(replayed.final_config.guarded_mapping_ids, ("1621",))
        self.assertNotIn("1622", replayed.final_config.guarded_mapping_ids)

    def test_apply_plan_creates_adjacent_original_backup_and_verifies_saved_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            config = DirectKakaoConfig()
            config.save(config_path)
            initial_bytes = config_path.read_bytes()
            plan = build_refresh_plan(
                config,
                ("홍길동",),
                snapshot(direct_chat("801", "홍길동")),
            )
            self.assertTrue(plan.ready_to_apply)

            backup_path = apply_plan(config_path, initial_bytes, plan)

            self.assertEqual(backup_path.parent, config_path.parent)
            self.assertEqual(backup_path.read_bytes(), initial_bytes)
            saved = DirectKakaoConfig.load(config_path)
            self.assertEqual(saved.mappings, plan.final_config.mappings)
            self.assertEqual(saved.mapping_labels, plan.final_config.mapping_labels)
            self.assertEqual(saved.guarded_mapping_ids, plan.final_config.guarded_mapping_ids)

    def test_apply_plan_blocks_toctou_change_before_backup_or_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "direct.json"
            initial_config = DirectKakaoConfig(mappings={"기존": "901"}, mapping_labels={"901": "기존"})
            initial_config.save(config_path)
            initial_bytes = config_path.read_bytes()
            plan = build_refresh_plan(
                initial_config,
                ("홍길동",),
                snapshot(direct_chat("902", "홍길동")),
            )
            changed_config = DirectKakaoConfig(
                mappings={"외부 변경": "999"},
                mapping_labels={"999": "외부 변경"},
            )
            changed_config.save(config_path)
            changed_bytes = config_path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "direct config changed"):
                apply_plan(config_path, initial_bytes, plan)

            self.assertEqual(config_path.read_bytes(), changed_bytes)
            self.assertEqual(DirectKakaoConfig.load(config_path).mappings, {"외부 변경": "999"})
            self.assertEqual(list(config_path.parent.glob("direct.json.*.bak")), [])


if __name__ == "__main__":
    unittest.main()
