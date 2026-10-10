"""Pure Kakao chat matching, ownership, and confirmation policy.

No Tk widgets, PC window operations, filesystem writes, or network calls live here.
GUI and batch refresh use the same deterministic policy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from kakao_direct import (
    normalize_chat_name,
    normalize_chat_id,
    normalize_guarded_mapping_ids,
    normalize_mapping_labels,
    normalize_mappings,
)

POSITIVE_CHAT_ID_PATTERN: Final = re.compile(r"^[1-9]\d{0,31}$")


@dataclass(frozen=True, slots=True)
class ChatMappingHandoff:
    """One explicitly approved stable-ID ownership transfer."""

    old_owner: str
    new_owner: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class ChatAutoMatchResult:
    mappings: dict[str, str]
    ambiguous: tuple[str, ...]
    unmatched: tuple[str, ...]
    # Existing persisted mappings that were not corroborated by the current
    # Kakao chat list.  These are deliberately separate from ``unmatched``:
    # the latter describes requested file names, while this field describes
    # stale/invalid persisted state that must not be trusted silently.
    invalidated: tuple[str, ...] = ()
    ownership_handoffs: tuple[ChatMappingHandoff, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatMappingReconcileResult:
    """Safe result of reconciling persisted mappings with one chat-list read."""

    mappings: dict[str, str]
    mapping_labels: dict[str, str]
    matched: tuple[str, ...]
    ambiguous: tuple[str, ...]
    unmatched: tuple[str, ...]
    invalidated: tuple[str, ...]
    ownership_handoffs: tuple[ChatMappingHandoff, ...] = ()


@dataclass(frozen=True, slots=True)
class ManualChatCandidate:
    """One tablet-backed room that is safe to offer for manual selection.

    ``label`` and ``peer_name`` are both copied from the same sanitized
    tablet snapshot.  Keeping them in the candidate makes the UI selection
    self-describing and lets the persistence boundary reject a stale row when
    the latest snapshot no longer agrees with it.
    """

    chat_id: str
    label: str
    peer_name: str
    owner_name: str = ""


@dataclass(frozen=True, slots=True)
class ManualChatMappingResult:
    """Atomic result of applying one explicitly selected chat mapping.

    A failed result carries the normalized pre-change state and a non-empty
    ``error``.  Callers must commit both dictionaries together only when
    :attr:`success` is true; no partially selected mapping is ever returned.
    """

    mappings: dict[str, str]
    mapping_labels: dict[str, str]
    error: str = ""
    candidate: ManualChatCandidate | None = None
    ownership_handoff: ChatMappingHandoff | None = None

    @property
    def success(self) -> bool:
        return not self.error


def normalize_chat_match_name(value: Any) -> str:
    try:
        return normalize_chat_name(value)
    except ValueError:
        return ""


def chat_choice_title(chat: dict[str, Any]) -> str:
    confirmed = confirmed_chat_label(chat)
    return confirmed or f"채팅 {chat.get('chat_id', '')}".strip()


def confirmed_peer_name(chat: Mapping[str, Any]) -> str:
    """Return one bridge-verified DirectChat peer nickname, or ``""``."""

    chat_type = str(chat.get("type") or "").strip().casefold()
    try:
        active_members = int(chat.get("active_members"))
    except (TypeError, ValueError):
        return ""
    peer_user_id = str(chat.get("peer_user_id") or "").strip()
    peer_names = chat.get("peer_names")
    if (
        chat_type != "directchat"
        or active_members != 2
        or not peer_user_id.isdigit()
        or peer_user_id.startswith("0")
        or not isinstance(peer_names, list)
        or len(peer_names) != 1
        or not isinstance(peer_names[0], str)
    ):
        return ""
    peer_name = peer_names[0].strip()
    if not peer_name or len(peer_name) > 100 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in peer_name):
        return ""
    return peer_name


def confirmed_peer_local_names(chat: Mapping[str, Any]) -> tuple[str, ...]:
    """Return one account-local friend alias behind a verified peer ID."""

    if not confirmed_peer_name(chat):
        return ()
    raw_names = chat.get("peer_local_names")
    if not isinstance(raw_names, list) or len(raw_names) != 1 or not isinstance(raw_names[0], str):
        return ()
    name = raw_names[0].strip()
    if not name or len(name) > 100 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        return ()
    return (name,)


def confirmed_chat_label(chat: dict[str, Any]) -> str:
    """Return a real Kakao-provided title or verified peer nickname."""

    title = str(chat.get("title") or "").strip()
    display_name = str(chat.get("display_name") or "").strip()
    return title or display_name or confirmed_peer_name(chat)


def is_confirmed_mapping_label(chat_id: str, label: str | None) -> bool:
    text = str(label or "").strip()
    return bool(text and text != f"채팅 {chat_id}".strip())


_KOREAN_PERSON_NAME = re.compile(r"^[가-힣]{2,5}$")


def structured_direct_chat_aliases(value: Any) -> tuple[str, ...]:
    """Return conservative PC-label aliases for one-to-one chats.

    PC KakaoTalk can keep local friend labels that the tablet session does
    not receive.  This project commonly uses ``학생갑T`` and
    ``A 장성중3 학생감``-style labels, while the tablet exposes only
    ``학생갑`` or ``학생감``.  These two deterministic conventions are
    useful without resorting to fuzzy/substring matching:

    * a 2-5 syllable Korean name followed by the role suffix ``T``;
    * a final Korean name preceded by class metadata containing a digit, or
      by a bracketed role label such as ``[도승지] 학생갸``.

    The caller must still require a unique ``DirectChat`` with two active
    members.  Arbitrary final-word and similarity guesses are not returned.
    """

    normalized = normalize_chat_match_name(value)
    if not normalized:
        return ()
    aliases: list[str] = []
    if normalized.endswith("t") and _KOREAN_PERSON_NAME.fullmatch(normalized[:-1]):
        aliases.append(normalized[:-1])

    parts = normalized.split()
    if len(parts) >= 2 and _KOREAN_PERSON_NAME.fullmatch(parts[-1]):
        prefix = " ".join(parts[:-1])
        if any(char.isdigit() for char in prefix) or (prefix.startswith("[") and "]" in prefix):
            aliases.append(parts[-1])
    return tuple(dict.fromkeys(aliases))


def direct_chat_person_identities(value: Any) -> tuple[str, ...]:
    """Return only deterministic Korean-person identities for a label.

    Bare Korean names participate so a saved canonical owner such as
    ``학생강`` can be compared with a structured CSV label.  Arbitrary room
    names do not become identities.
    """

    normalized = normalize_chat_match_name(value)
    if not normalized:
        return ()
    identities = list(structured_direct_chat_aliases(normalized))
    if _KOREAN_PERSON_NAME.fullmatch(normalized):
        identities.insert(0, normalized)
    return tuple(dict.fromkeys(identities))


def canonical_handoff_owner_identity(value: Any) -> str:
    """Return the identity of an allowed catalog owner, or ``""``.

    Ownership handoff is deliberately narrower than general structured alias
    matching.  A bare Korean name and the observed numeric roster form
    ``10917 학생개`` are catalog-like labels.  A prior ``A 학교2 이름`` or
    arbitrary manual alias is not eligible to hand ownership to another CSV
    label, preventing file-to-file alias ping-pong.
    """

    normalized = normalize_chat_match_name(value)
    if _KOREAN_PERSON_NAME.fullmatch(normalized):
        return normalized
    parts = normalized.split()
    if (
        len(parts) == 2
        and parts[0].isdigit()
        and _KOREAN_PERSON_NAME.fullmatch(parts[1])
    ):
        return parts[1]
    return ""


def is_one_to_one_direct_chat(chat: Mapping[str, Any]) -> bool:
    """Whether tablet metadata identifies a normal two-person chat."""

    chat_type = str(chat.get("type") or "").strip().casefold()
    try:
        active_members = int(chat.get("active_members"))
    except (TypeError, ValueError):
        return False
    return chat_type == "directchat" and active_members == 2


def positive_numeric_chat_id(value: Any) -> str:
    """Return one bounded positive numeric chat ID, or ``""``."""

    text = str(value or "").strip()
    return text if POSITIVE_CHAT_ID_PATTERN.fullmatch(text) else ""


def merge_chat_snapshots(
    primary: Iterable[Mapping[str, Any]],
    supplemental: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append absent supplemental IDs while preserving duplicate evidence."""

    merged = [dict(chat) for chat in primary if isinstance(chat, Mapping)]
    primary_ids = {
        str(chat.get("chat_id") or "").strip()
        for chat in merged
        if str(chat.get("chat_id") or "").strip()
    }
    for chat in supplemental:
        if not isinstance(chat, Mapping):
            continue
        chat_id = str(chat.get("chat_id") or "").strip()
        if not chat_id or chat_id in primary_ids:
            continue
        # Do not add supplemental IDs to ``primary_ids`` here.  If a malformed
        # bridge repeats a newly looked-up ID, retaining both rows lets the
        # existing duplicate-ID gates fail closed instead of hiding evidence.
        merged.append(dict(chat))
    return merged


def unique_blank_memo_chat_id(chats: Iterable[Mapping[str, Any]]) -> str:
    """Return the only blank-label MemoChat ID, or ``""`` if not unique.

    A PC window rectangle can identify a local window, but it cannot prove
    that a non-Memo local alias is the intended recipient.  The one safe
    exception is the tablet's singleton blank-label ``MemoChat`` (the local
    self-chat), whose stable chat ID is the only metadata-backed target left.
    The complete ``MemoChat`` population must contain exactly one row; a
    blank MemoChat alongside a labeled MemoChat is still ambiguous.  Duplicate
    IDs are rejected as well, so malformed/mock payloads cannot turn a
    non-unique MemoChat list into an automatic mapping.
    """

    rows = list(chats)
    memo_chats: list[tuple[str, Mapping[str, Any]]] = []
    for chat in rows:
        chat_id = str(chat.get("chat_id") or "").strip()
        if str(chat.get("type") or "").strip().casefold() != "memochat":
            continue
        memo_chats.append((chat_id, chat))
    if len(memo_chats) != 1:
        return ""
    candidate_id, candidate = memo_chats[0]
    if not candidate_id or confirmed_chat_label(dict(candidate)):
        return ""
    if sum(1 for chat in rows if str(chat.get("chat_id") or "").strip() == candidate_id) != 1:
        return ""
    return candidate_id


def auto_match_chat_ids(
    chat_names: tuple[str, ...] | list[str],
    chats: list[dict[str, Any]],
    existing_mappings: Mapping[Any, Any] | None = None,
    existing_mapping_labels: Mapping[Any, Any] | None = None,
) -> ChatAutoMatchResult:
    """Find only unambiguous PC-name -> current Kakao chat-ID matches.

    ``existing_mappings`` is optional for backwards compatibility with the
    original pure helper.  When supplied, a persisted mapping is considered
    valid when its stable ID is present in the current list and the chat has a
    confirmed title or display name.  A previously verified mapping label also
    keeps the stable ID valid when a fresh ``list_chats`` response temporarily
    omits that room; partial server reads must never erase confirmed mappings.
    A valid ID is reserved by its existing owner so an automatic refresh
    cannot silently assign one Kakao room to another PC name.  A manually
    confirmed alias (for example ``학생갑T`` -> ``학생갑``) remains valid by
    stable ID; exact-name matching is only used for new automatic assignments.
    Entries without either current or persisted verification are returned
    through ``invalidated`` for the caller to remove.

    New assignments prefer exact names.  If no exact candidate exists, the
    two structured local-label conventions returned by
    :func:`structured_direct_chat_aliases` may match a unique two-person
    ``DirectChat``.  No fuzzy or arbitrary substring matching is used.
    """
    request_names = list(chat_names)
    # Exact labels keep priority over aliases.  Candidate-side structured
    # aliases live in a separate index so an exact ``학생개`` room still wins
    # over a different ``10917 학생개`` room for an exact-name request.
    candidates: dict[str, set[str]] = {}
    structured_candidates: dict[str, set[str]] = {}
    chats_by_id: dict[str, dict[str, Any]] = {}
    chat_id_counts: dict[str, int] = {}
    for chat in chats:
        chat_id = str(chat.get("chat_id") or "").strip()
        if not chat_id:
            continue
        chat_id_counts[chat_id] = chat_id_counts.get(chat_id, 0) + 1
        chats_by_id[chat_id] = chat
        verified_direct = is_one_to_one_direct_chat(chat)
        peer_name = confirmed_peer_name(chat)
        for value in (chat.get("title"), chat.get("display_name"), peer_name, *confirmed_peer_local_names(chat)):
            normalized = normalize_chat_match_name(value)
            if normalized:
                candidates.setdefault(normalized, set()).add(chat_id)
                if verified_direct:
                    for alias in structured_direct_chat_aliases(value):
                        structured_candidates.setdefault(alias, set()).add(chat_id)

    # Validate every persisted mapping before using it as an ownership
    # reservation.  A fresh list can be partial even when ``all_chats`` was
    # requested, so absence from this one response is not deletion evidence.
    # Preserve a missing stable ID only when a previously verified label exists;
    # entries with neither current nor persisted verification remain invalid.
    valid_existing_owners: dict[str, str] = {}
    valid_existing_by_name: dict[str, str] = {}
    invalidated_chat_ids: set[str] = set()
    invalidated: list[str] = []
    saved_labels = dict(existing_mapping_labels or {})
    if existing_mappings:
        for raw_name, raw_chat_id in existing_mappings.items():
            name = str(raw_name).strip()
            normalized_name = normalize_chat_match_name(name)
            chat_id = str(raw_chat_id or "").strip()
            chat = chats_by_id.get(chat_id)
            current_label = confirmed_chat_label(chat) if chat is not None else ""
            saved_label = str(saved_labels.get(chat_id) or "").strip()
            if (
                not normalized_name
                or not chat_id
                or (
                    not current_label
                    and not is_confirmed_mapping_label(chat_id, saved_label)
                )
            ):
                if name:
                    invalidated.append(name)
                if chat_id:
                    invalidated_chat_ids.add(chat_id)
                continue
            if chat_id in invalidated_chat_ids:
                if name:
                    invalidated.append(name)
                continue
            previous = valid_existing_owners.get(chat_id)
            if previous is not None and previous != normalized_name:
                # A malformed legacy mapping can contain duplicate ownership;
                # neither owner is safe to use automatically.
                for existing_name in list(existing_mappings):
                    if str(existing_mappings.get(existing_name) or "").strip() == chat_id:
                        existing_text = str(existing_name).strip()
                        if existing_text and existing_text not in invalidated:
                            invalidated.append(existing_text)
                invalidated_chat_ids.add(chat_id)
                valid_existing_owners.pop(chat_id, None)
                for existing_name, existing_id in list(valid_existing_by_name.items()):
                    if existing_id == chat_id:
                        valid_existing_by_name.pop(existing_name, None)
                continue
            valid_existing_owners[chat_id] = normalized_name
            valid_existing_by_name[normalized_name] = chat_id

    # A person identity that is already owned by two stable IDs is never safe
    # to hand off, even when this particular server response happens to show
    # only one of those rooms.
    existing_identity_chat_ids: dict[str, set[str]] = {}
    for existing_name, existing_chat_id in valid_existing_by_name.items():
        for identity in direct_chat_person_identities(existing_name):
            existing_identity_chat_ids.setdefault(identity, set()).add(existing_chat_id)

    # Likewise, two current CSV labels that reduce to the same person must not
    # be resolved in input order.  Both remain ambiguous and no owner moves.
    requested_identity_counts: dict[str, int] = {}
    for requested_name in request_names:
        for identity in direct_chat_person_identities(requested_name):
            requested_identity_counts[identity] = requested_identity_counts.get(identity, 0) + 1

    mappings: dict[str, str] = {}
    ambiguous: list[str] = []
    unmatched: list[str] = []
    ownership_handoffs: list[ChatMappingHandoff] = []
    for name in request_names:
        normalized_name = normalize_chat_match_name(name)
        person_identities = set(direct_chat_person_identities(name))
        if any(requested_identity_counts.get(identity, 0) > 1 for identity in person_identities):
            ambiguous.append(name)
            continue
        existing_chat_id = valid_existing_by_name.get(normalized_name)
        matches = candidates.get(normalized_name, set())
        if not matches:
            alias_matches = {
                chat_id
                for alias in structured_direct_chat_aliases(name)
                for chat_id in (
                    candidates.get(alias, set())
                    | structured_candidates.get(alias, set())
                )
                if is_one_to_one_direct_chat(chats_by_id.get(chat_id, {}))
            }
            matches = alias_matches
        if existing_chat_id is not None:
            if existing_chat_id in chats_by_id:
                # A currently visible stable ID keeps a manually confirmed
                # alias even when the tablet uses different display spelling.
                mappings[name] = existing_chat_id
            elif matches:
                # The saved ID is absent but a fresh room now claims the same
                # exact/structured name.  Do not guess whether the old chat was
                # recreated or the list is partial: require operator review.
                ambiguous.append(name)
            else:
                # No contradictory current candidate exists, so a previously
                # verified stable ID survives this partial list response.
                mappings[name] = existing_chat_id
            continue
        if len(matches) == 1:
            chat_id = next(iter(matches))
            if chat_id_counts.get(chat_id) != 1:
                # A malformed/duplicated list row must not become a fresh
                # mapping merely because the set of stable IDs has size one.
                # Existing exact owners are handled above; every new mapping
                # and ownership handoff fails closed here.
                ambiguous.append(name)
                continue
            owner = valid_existing_owners.get(chat_id)
            identity_owned_ids = {
                owned_id
                for identity in person_identities
                for owned_id in existing_identity_chat_ids.get(identity, set())
            }
            if identity_owned_ids and identity_owned_ids != {chat_id}:
                # A saved stable ID for this identity differs from the fresh
                # candidate.  Treat the response as partial/recreated rather
                # than silently switching recipients.
                ambiguous.append(name)
            elif owner is not None and owner != normalized_name:
                canonical_identity = canonical_handoff_owner_identity(owner)
                owner_is_current_exact_label = chat_id in candidates.get(owner, set())
                if (
                    person_identities
                    and canonical_identity in person_identities
                    and owner_is_current_exact_label
                    and is_one_to_one_direct_chat(chats_by_id.get(chat_id, {}))
                ):
                    mappings[name] = chat_id
                    ownership_handoffs.append(
                        ChatMappingHandoff(owner, name, chat_id)
                    )
                else:
                    ambiguous.append(name)
            else:
                mappings[name] = chat_id
        elif len(matches) > 1:
            ambiguous.append(name)
        else:
            unmatched.append(name)

    # A single Kakao chat may expose both a room title and a display name.
    # Never let those aliases automatically map two intended PC recipients to
    # one destination; the operator must resolve that case manually.
    names_by_chat_id: dict[str, list[str]] = {}
    for name, chat_id in mappings.items():
        names_by_chat_id.setdefault(chat_id, []).append(name)
    many_to_one = {
        name
        for names in names_by_chat_id.values()
        if len(names) > 1
        for name in names
    }
    if many_to_one:
        for name in many_to_one:
            mappings.pop(name, None)
        ambiguous_set = set(ambiguous) | many_to_one
        ambiguous = [name for name in request_names if name in ambiguous_set]
        ownership_handoffs = [
            handoff
            for handoff in ownership_handoffs
            if handoff.new_owner in mappings
            and mappings.get(handoff.new_owner) == handoff.chat_id
        ]
    return ChatAutoMatchResult(
        mappings=mappings,
        ambiguous=tuple(ambiguous),
        unmatched=tuple(unmatched),
        invalidated=tuple(dict.fromkeys(invalidated)),
        ownership_handoffs=tuple(ownership_handoffs),
    )


def reconcile_chat_mappings(
    mappings: Mapping[Any, Any] | None,
    mapping_labels: Mapping[Any, Any] | None,
    chat_names: tuple[str, ...] | list[str],
    chats: list[dict[str, Any]],
) -> ChatMappingReconcileResult:
    """Reconcile mappings with a fresh list and return safe persisted state.

    Exact unique labels and the deliberately narrow structured aliases from
    :func:`structured_direct_chat_aliases` may be newly auto-applied. Existing
    entries remain valid by stable ID when either the tablet reports a real
    label or a previously verified mapping label is saved.  The latter also
    protects confirmed mappings from a partial ``list_chats`` response.
    Entries with no current or persisted verification are removed. Requested
    ambiguous/unmatched entries without a valid existing mapping are also
    removed so the next preflight can try the PC-window fallback or leave them
    for exception review. Returned mappings are normalized and contain no
    duplicate chat ID.
    """

    current_mappings = normalize_mappings(mappings or {})
    current_labels = normalize_mapping_labels(mapping_labels or {})
    result = auto_match_chat_ids(
        chat_names,
        chats,
        existing_mappings=current_mappings,
        existing_mapping_labels=current_labels,
    )

    # Drop every invalid persisted entry first, then drop unresolved requested
    # names.  This makes an auto refresh conservative: a stale mapping cannot
    # remain usable merely because a previous session saved it.
    names_to_remove = set(result.invalidated)
    names_to_remove.update(result.ambiguous)
    names_to_remove.update(result.unmatched)
    for raw_name in names_to_remove:
        normalized = normalize_chat_match_name(raw_name)
        for key in list(current_mappings):
            if normalize_chat_match_name(key) == normalized:
                current_mappings.pop(key, None)

    chats_by_id = {
        str(chat.get("chat_id") or "").strip(): chat
        for chat in chats
        if str(chat.get("chat_id") or "").strip()
    }
    approved_handoffs = {
        (normalize_chat_match_name(handoff.new_owner), handoff.chat_id): handoff
        for handoff in result.ownership_handoffs
    }
    matched: list[str] = []
    for name, chat_id in result.mappings.items():
        normalized_name = normalize_chat_match_name(name)
        normalized_chat_id = str(chat_id).strip()
        handoff = approved_handoffs.get((normalized_name, normalized_chat_id))
        if handoff is not None:
            old_owner = normalize_chat_match_name(handoff.old_owner)
            if current_mappings.get(old_owner) != normalized_chat_id:
                raise ValueError("approved chat mapping handoff owner changed")
            # Transfer the stable ID's sole ownership before adding the new
            # CSV label.  Adding first would violate normalize_mappings' one
            # owner per chat-ID invariant.
            current_mappings.pop(old_owner, None)
        # Replace a stale ID for the same normalized PC name with the current
        # exact match.  ``auto_match_chat_ids`` has already reserved IDs owned
        # by other valid persisted mappings or emitted an explicit handoff.
        for key in list(current_mappings):
            if normalize_chat_match_name(key) == normalized_name:
                current_mappings.pop(key, None)
        current_mappings[normalized_name] = normalized_chat_id
        matched.append(name)
        confirmed = confirmed_chat_label(chats_by_id.get(normalized_chat_id, {}))
        if confirmed:
            current_labels[normalized_chat_id] = confirmed

    # Refresh the operator-visible label for every mapping that survived the
    # reconciliation, including mappings belonging to rows outside this file.
    # A changed title/display name must not leave an old label looking valid.
    for chat_id in list(current_mappings.values()):
        current_chat = chats_by_id.get(str(chat_id).strip())
        confirmed = confirmed_chat_label(current_chat or {})
        if confirmed:
            current_labels[str(chat_id).strip()] = confirmed
        elif not is_confirmed_mapping_label(str(chat_id).strip(), current_labels.get(str(chat_id).strip())):
            current_labels.pop(str(chat_id).strip(), None)

    mapped_ids = set(current_mappings.values())
    current_labels = {
        str(chat_id): str(label)
        for chat_id, label in current_labels.items()
        if str(chat_id) in mapped_ids and is_confirmed_mapping_label(str(chat_id), label)
    }
    # Re-normalize once more after replacing keys and filtering labels.  This
    # also gives callers the same duplicate-ID guard as DirectKakaoConfig.
    current_mappings = normalize_mappings(current_mappings)
    current_labels = normalize_mapping_labels(current_labels)
    return ChatMappingReconcileResult(
        mappings=current_mappings,
        mapping_labels=current_labels,
        matched=tuple(matched),
        ambiguous=result.ambiguous,
        unmatched=result.unmatched,
        invalidated=result.invalidated,
        ownership_handoffs=result.ownership_handoffs,
    )


def normalize_manual_chat_targets(values: Iterable[Any]) -> tuple[str, ...]:
    """Normalize CSV targets and reject normalized-name collisions.

    A manual chooser is allowed to select a room whose Kakao label differs
    from the CSV name, but the CSV side still has to identify exactly one
    target.  This helper keeps that invariant independent of Tk widgets.
    """

    if isinstance(values, (str, bytes)):
        values = (values,)
    normalized_targets: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_chat_match_name(value)
        if not normalized:
            raise ValueError("CSV chat name is required")
        if normalized in seen:
            raise ValueError(f"duplicate normalized CSV chat name: {normalized}")
        seen.add(normalized)
        normalized_targets.append(normalized)
    return tuple(normalized_targets)


def _manual_candidate_owner_is_allowed(
    target_name: str | None,
    owner_name: str,
    candidate_label: str,
) -> bool:
    """Return whether an already-owned candidate may be offered.

    An existing mapping is normally a hard reservation.  The one deliberate
    exception matches ``apply_auto_chat_match_result``: a sole, canonical
    catalog owner may be handed to the explicitly confirmed CSV target when
    the current tablet label is still exactly that owner.  Arbitrary/manual
    aliases never qualify for this transfer.  No name-similarity rule is
    needed here because the operator confirms both names in the modal dialog.
    """

    if not owner_name or target_name is None:
        return False
    if owner_name == target_name:
        return True
    candidate_label_normalized = normalize_chat_match_name(candidate_label)
    return bool(candidate_label_normalized and candidate_label_normalized == owner_name)


def _normalize_manual_reserved_chat_ids(values: Iterable[Any]) -> frozenset[str]:
    """Normalize IDs reserved by other already-matched CSV targets."""

    if isinstance(values, (str, bytes)):
        values = (values,)
    reserved: set[str] = set()
    for value in values:
        reserved.add(normalize_chat_id(value))
    return frozenset(reserved)


def manual_chat_candidates(
    chats: Iterable[Mapping[str, Any]],
    existing_mappings: Mapping[Any, Any] | None = None,
    *,
    target_name: Any | None = None,
    reserved_chat_ids: Iterable[Any] = (),
) -> tuple[ManualChatCandidate, ...]:
    """Build safe tablet-backed candidates for an operator's manual choice.

    Only one-to-one ``DirectChat`` rows with a bridge-confirmed peer identity
    and a real label are returned.  Every duplicate stable ID is rejected as
    a group, and an ID owned by another mapping is hidden unless it is the
    narrow canonical-catalog handoff described above.  ``target_name`` is
    optional so callers can build a general chooser; passing it enables the
    safe handoff exception for a particular CSV row.

    The function is deliberately independent of PC window geometry.  The
    returned candidate is a display/selection record only; persistence must
    call :func:`apply_manual_chat_mapping` with a fresh tablet snapshot.

    ``reserved_chat_ids`` is the set of IDs already assigned to *other* rows
    in the current CSV request.  It is checked in addition to persisted
    ownership because a caller may be reconciling several rows before the
    configuration is saved.
    """

    target: str | None = None
    if target_name is not None:
        target = normalize_manual_chat_targets((target_name,))[0]
    try:
        normalized_existing = normalize_mappings(existing_mappings or {})
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid existing chat mapping ownership: {exc}") from exc
    try:
        reserved_ids = _normalize_manual_reserved_chat_ids(reserved_chat_ids)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid reserved chat ID: {exc}") from exc

    # The normalizer rejects duplicate ownership. Keep its first-owner order
    # explicit while indexing the same display lookup once for this snapshot.
    owners_by_id: dict[str, str] = {}
    for existing_name, existing_chat_id in normalized_existing.items():
        owners_by_id.setdefault(existing_chat_id, existing_name)

    rows_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for raw_chat in chats:
        if not isinstance(raw_chat, Mapping):
            continue
        try:
            chat_id = normalize_chat_id(raw_chat.get("chat_id"))
        except (TypeError, ValueError):
            continue
        rows_by_id.setdefault(chat_id, []).append(raw_chat)

    candidates: list[ManualChatCandidate] = []
    for chat_id, rows in rows_by_id.items():
        # Duplicate IDs make the snapshot itself ambiguous.  Do not pick the
        # first row just because its label happens to look better.
        if len(rows) != 1:
            continue
        chat = rows[0]
        if not is_one_to_one_direct_chat(chat):
            continue
        peer_name = confirmed_peer_name(chat)
        if not peer_name:
            continue
        label = confirmed_chat_label(dict(chat))
        if not label or not is_confirmed_mapping_label(chat_id, label):
            continue
        try:
            # Keep the eventual label write compatible with the direct config
            # normalizer, even when a third-party bridge returned odd text.
            normalize_mapping_labels({chat_id: label})
        except (TypeError, ValueError):
            continue

        owner_name = owners_by_id.get(chat_id, "")
        if chat_id in reserved_ids and owner_name != target:
            # A room already selected for another current CSV target cannot
            # be stolen by a canonical-owner handoff or a new manual alias.
            continue
        if owner_name and not _manual_candidate_owner_is_allowed(target, owner_name, label):
            continue
        candidates.append(
            ManualChatCandidate(
                chat_id=chat_id,
                label=label,
                peer_name=peer_name,
                owner_name=owner_name,
            )
        )
    return tuple(candidates)


def _manual_mapping_failure(
    error: str,
    mappings: Mapping[Any, Any] | None = None,
    mapping_labels: Mapping[Any, Any] | None = None,
) -> ManualChatMappingResult:
    """Create a failure result without exposing a partially changed state."""

    try:
        safe_mappings = normalize_mappings(mappings or {})
    except (TypeError, ValueError):
        safe_mappings = {}
    try:
        safe_labels = normalize_mapping_labels(mapping_labels or {})
    except (TypeError, ValueError):
        safe_labels = {}
    mapped_ids = set(safe_mappings.values())
    safe_labels = {
        chat_id: label
        for chat_id, label in safe_labels.items()
        if chat_id in mapped_ids and is_confirmed_mapping_label(chat_id, label)
    }
    return ManualChatMappingResult(safe_mappings, safe_labels, error=error)


def apply_manual_chat_mapping(
    target_name: Any,
    selected_chat_id: Any,
    latest_chats: Iterable[Mapping[str, Any]],
    existing_mappings: Mapping[Any, Any] | None = None,
    existing_mapping_labels: Mapping[Any, Any] | None = None,
    *,
    selected_candidate: ManualChatCandidate | None = None,
    reserved_chat_ids: Iterable[Any] = (),
) -> ManualChatMappingResult:
    """Apply one manual choice after re-validating the latest chat snapshot.

    ``latest_chats`` must be the fresh snapshot taken at the selection/apply
    boundary.  The selected ID is looked up again through
    :func:`manual_chat_candidates`, so a stale, forged, duplicated, group,
    memo, or identity-less row cannot be persisted.  The returned mapping and
    mapping-label dictionaries are a single atomic result; failures preserve
    the normalized pre-change state and carry a clear ``error``.
    """

    try:
        normalized_target = normalize_manual_chat_targets((target_name,))[0]
    except (TypeError, ValueError) as exc:
        return _manual_mapping_failure(f"invalid CSV target: {exc}", existing_mappings, existing_mapping_labels)
    try:
        normalized_selected_id = normalize_chat_id(selected_chat_id)
    except (TypeError, ValueError) as exc:
        return _manual_mapping_failure(f"invalid selected chat ID: {exc}", existing_mappings, existing_mapping_labels)
    try:
        current_mappings = normalize_mappings(existing_mappings or {})
        current_labels = normalize_mapping_labels(existing_mapping_labels or {})
    except (TypeError, ValueError) as exc:
        return _manual_mapping_failure(
            f"invalid existing chat mapping ownership: {exc}",
            existing_mappings,
            existing_mapping_labels,
        )

    try:
        candidates = manual_chat_candidates(
            latest_chats,
            current_mappings,
            target_name=normalized_target,
            reserved_chat_ids=reserved_chat_ids,
        )
    except (TypeError, ValueError) as exc:
        return ManualChatMappingResult(
            dict(current_mappings),
            {
                chat_id: label
                for chat_id, label in current_labels.items()
                if chat_id in set(current_mappings.values())
            },
            error=f"latest chat snapshot rejected: {exc}",
        )
    selected = next(
        (candidate for candidate in candidates if candidate.chat_id == normalized_selected_id),
        None,
    )
    if selected is None:
        return ManualChatMappingResult(
            dict(current_mappings),
            {
                chat_id: label
                for chat_id, label in current_labels.items()
                if chat_id in set(current_mappings.values())
            },
            error="selected chat is not an eligible candidate in the latest snapshot",
        )
    if selected_candidate is not None:
        if not isinstance(selected_candidate, ManualChatCandidate) or selected_candidate != selected:
            return ManualChatMappingResult(
                dict(current_mappings),
                {
                    chat_id: label
                    for chat_id, label in current_labels.items()
                    if chat_id in set(current_mappings.values())
                },
                error="stale or forged manual chat selection",
            )

    owner_name = selected.owner_name
    if owner_name and owner_name != normalized_target:
        # ``manual_chat_candidates`` has already checked this handoff against
        # the latest label and target identity.  Retain the explicit guard at
        # the write boundary so this function stays safe if that helper is
        # later refactored.
        if not _manual_candidate_owner_is_allowed(normalized_target, owner_name, selected.label):
            return ManualChatMappingResult(
                dict(current_mappings),
                {
                    chat_id: label
                    for chat_id, label in current_labels.items()
                    if chat_id in set(current_mappings.values())
                },
                error="selected chat ID is already owned by another mapping",
            )

    next_mappings = dict(current_mappings)
    next_labels = dict(current_labels)
    previous_target_id = next_mappings.pop(normalized_target, None)
    if owner_name and owner_name != normalized_target:
        # Canonical catalog owner transfer: remove only that exact sole owner.
        if next_mappings.get(owner_name) != normalized_selected_id:
            return ManualChatMappingResult(
                dict(current_mappings),
                {
                    chat_id: label
                    for chat_id, label in current_labels.items()
                    if chat_id in set(current_mappings.values())
                },
                error="selected chat owner changed before apply",
            )
        next_mappings.pop(owner_name, None)
        handoff = ChatMappingHandoff(owner_name, normalized_target, normalized_selected_id)
    else:
        handoff = None
    if previous_target_id and previous_target_id != normalized_selected_id:
        if previous_target_id not in next_mappings.values():
            next_labels.pop(previous_target_id, None)
    next_mappings[normalized_target] = normalized_selected_id
    next_labels[normalized_selected_id] = selected.label
    mapped_ids = set(next_mappings.values())
    next_labels = {
        chat_id: label
        for chat_id, label in next_labels.items()
        if chat_id in mapped_ids and is_confirmed_mapping_label(chat_id, label)
    }
    try:
        # Final normalization gives callers the same one-owner guarantee as
        # the persisted DirectKakaoConfig and ensures both dictionaries are
        # safe to commit together.
        next_mappings = normalize_mappings(next_mappings)
        next_labels = normalize_mapping_labels(next_labels)
    except (TypeError, ValueError) as exc:
        return ManualChatMappingResult(
            dict(current_mappings),
            {
                chat_id: label
                for chat_id, label in current_labels.items()
                if chat_id in set(current_mappings.values())
            },
            error=f"manual mapping result rejected: {exc}",
        )
    return ManualChatMappingResult(
        mappings=next_mappings,
        mapping_labels=next_labels,
        candidate=selected,
        ownership_handoff=handoff,
    )


def unverified_alias_mapping_names(
    chat_names: Iterable[Any],
    mappings: Mapping[Any, Any] | None,
    mapping_labels: Mapping[Any, Any] | None,
    latest_chats: Iterable[Mapping[str, Any]],
    *,
    guarded_mapping_ids: Iterable[Any] = (),
) -> tuple[str, ...]:
    """Return alias mappings not re-proven as the same current 1:1 room.

    A manual mapping is identified by persisted ``guarded_mapping_ids`` even
    when its CSV target exactly equals the Kakao label.  A PC-local alias also
    requires this gate by its differing text.  Before direct send either kind
    must be backed by exactly one current ``DirectChat`` row, two active
    members, one verified peer identity, and the same confirmed tablet label
    saved with the ID.  Unguarded exact-name mappings retain the existing
    automatic/group-room behavior.
    """

    try:
        safe_mappings = normalize_mappings(mappings or {})
        safe_labels = normalize_mapping_labels(mapping_labels or {})
        guarded_ids = set(normalize_guarded_mapping_ids(guarded_mapping_ids))
    except (TypeError, ValueError):
        return tuple(str(name).strip() for name in chat_names if str(name).strip())

    rows_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for chat in latest_chats:
        if not isinstance(chat, Mapping):
            continue
        try:
            chat_id = normalize_chat_id(chat.get("chat_id"))
        except (TypeError, ValueError):
            continue
        rows_by_id.setdefault(chat_id, []).append(chat)

    failures: list[str] = []
    for raw_name in chat_names:
        name = str(raw_name).strip()
        normalized_name = normalize_chat_match_name(name)
        chat_id = resolve_chat_mapping(safe_mappings, name)
        saved_label = safe_labels.get(chat_id, "") if chat_id else ""
        normalized_label = normalize_chat_match_name(saved_label)
        if not normalized_name or not chat_id or not normalized_label:
            continue
        if normalized_name == normalized_label and chat_id not in guarded_ids:
            # Exact Kakao labels are governed by the existing automatic
            # mapping policy unless explicit manual provenance says otherwise.
            continue
        rows = rows_by_id.get(chat_id, ())
        if len(rows) != 1:
            failures.append(name)
            continue
        chat = rows[0]
        current_label = confirmed_chat_label(dict(chat))
        if (
            not is_one_to_one_direct_chat(chat)
            or not confirmed_peer_name(chat)
            or normalize_chat_match_name(current_label) != normalized_label
        ):
            failures.append(name)
    return tuple(dict.fromkeys(failures))


def resolve_chat_mapping(mappings: dict[str, str], chat_name: str) -> str:
    exact = str(mappings.get(chat_name.strip()) or "").strip()
    if exact:
        return exact
    normalized = normalize_chat_match_name(chat_name)
    matches = {
        str(chat_id).strip()
        for name, chat_id in mappings.items()
        if normalize_chat_match_name(name) == normalized and str(chat_id).strip()
    }
    return next(iter(matches)) if len(matches) == 1 else ""


def preserve_guarded_mapping_labels(
    mappings: Mapping[Any, Any],
    mapping_labels: Mapping[Any, Any],
    previous_mapping_labels: Mapping[Any, Any],
    guarded_mapping_ids: Iterable[Any],
) -> dict[str, str]:
    """Keep the operator's original evidence for every surviving guarded ID.

    A changed server label is evidence to compare, not permission to replace a
    prior manual confirmation. Unprotected labels retain ordinary refresh
    behavior. The returned copy includes labels only for currently mapped IDs.
    """

    mapped_ids = set(normalize_mappings(mappings).values())
    labels = normalize_mapping_labels(mapping_labels)
    previous_labels = normalize_mapping_labels(previous_mapping_labels)
    for chat_id in normalize_guarded_mapping_ids(guarded_mapping_ids):
        previous_label = previous_labels.get(chat_id)
        if chat_id in mapped_ids and is_confirmed_mapping_label(chat_id, previous_label):
            labels[chat_id] = previous_label
    return {chat_id: label for chat_id, label in labels.items() if chat_id in mapped_ids}


__all__ = [
    "ChatMappingHandoff",
    "ChatAutoMatchResult",
    "ChatMappingReconcileResult",
    "ManualChatCandidate",
    "ManualChatMappingResult",
    "normalize_chat_match_name",
    "chat_choice_title",
    "confirmed_peer_name",
    "confirmed_peer_local_names",
    "confirmed_chat_label",
    "is_confirmed_mapping_label",
    "structured_direct_chat_aliases",
    "direct_chat_person_identities",
    "canonical_handoff_owner_identity",
    "is_one_to_one_direct_chat",
    "positive_numeric_chat_id",
    "merge_chat_snapshots",
    "unique_blank_memo_chat_id",
    "auto_match_chat_ids",
    "reconcile_chat_mappings",
    "normalize_manual_chat_targets",
    "manual_chat_candidates",
    "apply_manual_chat_mapping",
    "unverified_alias_mapping_names",
    "resolve_chat_mapping",
    "preserve_guarded_mapping_labels",
]
