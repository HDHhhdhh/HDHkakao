"""Owner indexing must preserve the original manual-candidate safety policy."""
from collections.abc import Mapping
from copy import deepcopy
from itertools import product
from unittest.mock import patch

import pytest

import kakao_mapping_policy as policy


def reference_candidates(chats, existing_mappings=None, *, target_name=None, reserved_chat_ids=()):
    """Original helper copied before replacing its repeated owner scan."""
    target = None
    if target_name is not None:
        target = policy.normalize_manual_chat_targets((target_name,))[0]
    try:
        normalized_existing = policy.normalize_mappings(existing_mappings or {})
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid existing chat mapping ownership: {exc}") from exc
    try:
        reserved_ids = policy._normalize_manual_reserved_chat_ids(reserved_chat_ids)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid reserved chat ID: {exc}") from exc

    rows_by_id = {}
    for raw_chat in chats:
        if not isinstance(raw_chat, Mapping):
            continue
        try:
            chat_id = policy.normalize_chat_id(raw_chat.get("chat_id"))
        except (TypeError, ValueError):
            continue
        rows_by_id.setdefault(chat_id, []).append(raw_chat)

    candidates = []
    for chat_id, rows in rows_by_id.items():
        if len(rows) != 1:
            continue
        chat = rows[0]
        if not policy.is_one_to_one_direct_chat(chat):
            continue
        peer_name = policy.confirmed_peer_name(chat)
        if not peer_name:
            continue
        label = policy.confirmed_chat_label(dict(chat))
        if not label or not policy.is_confirmed_mapping_label(chat_id, label):
            continue
        try:
            policy.normalize_mapping_labels({chat_id: label})
        except (TypeError, ValueError):
            continue
        owner_name = ""
        for existing_name, existing_chat_id in normalized_existing.items():
            if existing_chat_id == chat_id:
                owner_name = existing_name
                break
        if chat_id in reserved_ids and owner_name != target:
            continue
        if owner_name and not policy._manual_candidate_owner_is_allowed(target, owner_name, label):
            continue
        candidates.append(policy.ManualChatCandidate(chat_id, label, peer_name, owner_name))
    return tuple(candidates)


def direct(chat_id, label, **updates):
    row = {
        "chat_id": chat_id, "type": "DirectChat", "active_members": 2,
        "title": label, "peer_user_id": "101", "peer_names": [label],
    }
    row.update(updates)
    return row


@pytest.mark.parametrize("target,reserved", list(product(
    (None, "Owner 3", "New Alias", " Ｎｅｗ  Alias "),
    ((), ("room-2",), ("room-3", "free")),
)))
def test_full_parity_for_handoff_alias_reserved_ids_duplicate_rooms_and_order(target, reserved):
    chats = [direct(f"room-{index}", f"Owner {index}") for index in (3, 1, 2, 0)]
    chats += [
        direct("free", "Free Room"), direct("alias", "Different Label"),
        direct("duplicate", "Duplicate A"), direct("duplicate", "Duplicate B"),
        direct("group", "Group", type="MultiChat", active_members=3),
        direct("no-peer", "Unverified", peer_user_id=""),
        direct(None, "Missing ID"), {"chat_id": "partial"}, "not a mapping",
    ]
    mappings = {f"Owner {index}": f"room-{index}" for index in range(4)}
    mappings["Saved Manual Alias"] = "alias"
    before_chats, before_mappings = deepcopy(chats), dict(mappings)
    expected = reference_candidates(chats, mappings, target_name=target, reserved_chat_ids=reserved)
    assert policy.manual_chat_candidates(iter(chats), mappings, target_name=target,
                                         reserved_chat_ids=iter(reserved)) == expected
    assert all(candidate.chat_id not in {"duplicate", "group", "no-peer", "partial"} for candidate in expected)
    assert all(candidate.chat_id != "alias" for candidate in expected)
    assert chats == before_chats
    assert mappings == before_mappings


def test_normalized_duplicate_name_with_same_id_preserves_owner_and_chat_order():
    mappings = {" Ｏｗｎｅｒ  One ": " room-1 ", "owner one": "room-1", "Owner Two": "room-2"}
    chats = [direct("room-2", "Owner Two"), direct("room-1", "Owner One"), direct("free", "Free")]
    expected = reference_candidates(chats, mappings, target_name="New Alias")
    actual = policy.manual_chat_candidates(chats, mappings, target_name="New Alias")
    assert actual == expected
    assert [(candidate.chat_id, candidate.owner_name) for candidate in actual] == [
        ("room-2", "owner two"), ("room-1", "owner one"), ("free", ""),
    ]


@pytest.mark.parametrize("mappings", [
    {"First": "room-1", "Second": "room-1"},
    {"First": "room-1", " FIRST ": "room-2"},
    {"": "room-1"}, {None: "room-1"}, {"First": ""},
    {"First": "bad\nID"}, ["not a mapping"],
])
def test_invalid_mapping_errors_match_reference_and_never_bypass_duplicate_id_guard(mappings):
    chats = [direct("room-1", "First")]
    with pytest.raises(ValueError) as expected:
        reference_candidates(chats, mappings, target_name="Target")
    with pytest.raises(ValueError) as actual:
        policy.manual_chat_candidates(chats, mappings, target_name="Target")
    assert str(actual.value) == str(expected.value)
    assert "invalid existing chat mapping ownership" in str(actual.value)


@pytest.mark.parametrize("target,reserved", [("", ()), ("Target", ("bad\nID",)), ("Target", (None,))])
def test_invalid_target_and_reservation_errors_keep_the_original_validation_order(target, reserved):
    chats = [direct("room-1", "First")]
    with pytest.raises(ValueError) as expected:
        reference_candidates(chats, {}, target_name=target, reserved_chat_ids=reserved)
    with pytest.raises(ValueError) as actual:
        policy.manual_chat_candidates(chats, {}, target_name=target, reserved_chat_ids=reserved)
    assert str(actual.value) == str(expected.value)


def test_reserved_owner_can_keep_its_own_id_but_canonical_handoff_cannot_take_it():
    chats = [direct("room-1", "First")]
    mappings = {"First": "room-1"}
    own = policy.manual_chat_candidates(chats, mappings, target_name=" FIRST ", reserved_chat_ids=("room-1",))
    other = policy.manual_chat_candidates(chats, mappings, target_name="CSV Alias", reserved_chat_ids=("room-1",))
    assert own == reference_candidates(chats, mappings, target_name=" FIRST ", reserved_chat_ids=("room-1",))
    assert len(own) == 1 and own[0].owner_name == "first"
    assert other == ()


def test_large_catalog_builds_owner_index_once_after_real_normalization():
    class CountedOwners(dict):
        passes = 0

        def items(self):
            self.passes += 1
            return super().items()

    mappings = {f"Owner {index}": f"room-{index}" for index in range(300)}
    normalized = CountedOwners(policy.normalize_mappings(mappings))
    chats = [direct(f"room-{index}", f"Owner {index}") for index in range(300)]
    with patch("kakao_mapping_policy.normalize_mappings", return_value=normalized):
        actual = policy.manual_chat_candidates(chats, mappings, target_name="New CSV Alias")
    assert len(actual) == 300
    assert normalized.passes == 1
    assert [candidate.owner_name for candidate in actual] == [f"owner {index}" for index in range(300)]
