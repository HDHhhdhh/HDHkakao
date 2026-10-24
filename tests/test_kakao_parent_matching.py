import tempfile
import unittest
from pathlib import Path

from kakao_direct import DirectKakaoConfig

from kakao_mapping_policy import (
    auto_match_chat_ids,
    direct_chat_person_identities,
    reconcile_chat_mappings,
    resolve_chat_mapping,
    structured_direct_chat_aliases,
    unverified_alias_mapping_names,
)


def direct_chat(chat_id, label, peer_id):
    return {
        "chat_id": chat_id,
        "title": label,
        "type": "DirectChat",
        "active_members": 2,
        "peer_user_id": peer_id,
        "peer_names": [label],
    }


class ParentChatMatchingTests(unittest.TestCase):
    def test_shared_parent_roles_are_not_person_identities(self):
        for role in ("어머님", "어머니", "엄마", "아버님", "아버지", "아빠", "부모님", "학부모", "보호자"):
            with self.subTest(role=role):
                self.assertEqual(structured_direct_chat_aliases(f"B 학교2 학생가 {role}"), ())
                self.assertEqual(direct_chat_person_identities(role), ())

    def test_different_exact_parent_labels_match_without_role_collision(self):
        names = ["B 학교1 학생가 어머님", "B 학교2 학생나 어머님"]
        chats = [direct_chat("101", names[0], "1001"), direct_chat("102", names[1], "1002")]

        result = auto_match_chat_ids(names, chats)

        self.assertEqual(result.mappings, dict(zip(names, ("101", "102"))))
        self.assertEqual(result.ambiguous, ())
        self.assertEqual(result.unmatched, ())

    def test_confirmed_parent_aliases_survive_public_profile_names(self):
        names = ["A 학교1 학생가 어머님", "A 학교2 학생나 어머님"]
        chats = [direct_chat("101", "학부모가", "1001"), direct_chat("102", "학부모나", "1002")]

        result = auto_match_chat_ids(
            names,
            chats,
            {names[0]: "101", names[1]: "102"},
            {"101": "학부모가", "102": "학부모나"},
        )

        self.assertEqual(result.mappings, dict(zip(names, ("101", "102"))))
        self.assertEqual(result.ambiguous, ())

    def test_role_only_profile_does_not_match_unrelated_parent_targets(self):
        names = ["A 학교1 학생가 어머님", "A 학교2 학생나 어머님"]

        result = auto_match_chat_ids(names, [direct_chat("101", "어머님", "1001")])

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unmatched, tuple(names))

    def test_parent_mappings_persist_with_existing_students_and_revalidate_after_reload(self):
        parents = ["B 학교1 학생가 어머님", "B 학교2 학생나 어머님"]
        student = "A 학교1 학생가"
        chats = [direct_chat("101", "공개이름가", "1001"), direct_chat("102", "공개이름나", "1002")]
        mappings = {student: "201", parents[0]: "101", parents[1]: "102"}
        labels = {"201": "학생가", "101": "공개이름가", "102": "공개이름나"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kakao_direct.json"
            config = DirectKakaoConfig(
                mappings=mappings, mapping_labels=labels, guarded_mapping_ids=("101", "102"),
            )
            config.save(path)
            saved = DirectKakaoConfig.load(path)
            refreshed = reconcile_chat_mappings(saved.mappings, saved.mapping_labels, parents, chats)

            self.assertEqual(refreshed.ambiguous, ())
            self.assertEqual(refreshed.unmatched, ())
            self.assertEqual(refreshed.mappings, saved.mappings)
            self.assertEqual(resolve_chat_mapping(refreshed.mappings, student), "201")
            self.assertEqual(unverified_alias_mapping_names(
                parents, refreshed.mappings, refreshed.mapping_labels, chats,
                guarded_mapping_ids=saved.guarded_mapping_ids,
            ), ())
            self.assertEqual(unverified_alias_mapping_names(
                parents, refreshed.mappings, refreshed.mapping_labels, chats[:1],
                guarded_mapping_ids=saved.guarded_mapping_ids,
            ), (parents[1],))

    def test_missing_parent_never_falls_back_to_student_or_different_prefix(self):
        name = "A 학교1 학생가 어머님"
        chats = [
            direct_chat("101", "학생가", "1001"),
            direct_chat("102", "B 학교1 학생가 어머님", "1002"),
        ]

        result = auto_match_chat_ids([name], chats)

        self.assertEqual(result.mappings, {})
        self.assertEqual(result.unmatched, (name,))


if __name__ == "__main__":
    unittest.main()
