"""Safely refresh persisted Kakao chat mappings from a names-only CSV.

The command is a no-send workflow.  It reads the ordinary server chat list,
uses the selected PC account's read-only chat ID catalog to recover omitted
rooms, and accepts only server-verified two-person DirectChat rows with one
stable peer identity.  ``--apply`` is fail-closed: every CSV target must be
verified before an adjacent backup and atomic config save are allowed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from kakao_direct import (
    DirectKakaoConfig,
    KakaoDirectBridge,
    MAX_CHAT_LOOKUP_IDS,
    direct_config_lock,
)
from kakao_sender import NAME_ALIASES, csv_encoding_candidates, find_column, sniff_csv_dialect
from kakao_app_paths import CONFIG_PATH, DIRECT_CONFIG_PATH
from kakao_chat_catalog import MAX_PC_CHAT_CATALOG_IDS, discover_pc_chat_catalog_ids
from kakao_sender import chat_title_matches
from kakao_mapping_policy import (
    confirmed_chat_label,
    confirmed_peer_name,
    is_one_to_one_direct_chat,
    manual_chat_candidates,
    merge_chat_snapshots,
    normalize_chat_match_name,
    normalize_manual_chat_targets,
    positive_numeric_chat_id,
    preserve_guarded_mapping_labels,
    reconcile_chat_mappings,
    resolve_chat_mapping,
    apply_manual_chat_mapping,
)
from recover_chat_mappings import create_adjacent_backup


def load_relaxed_config_data(path: Path) -> tuple[dict[str, Any], str | None]:
    """Load the optional PC adapter only when an explicit PC probe uses it."""

    from kakao_sender_gui import load_relaxed_config_data as load_pc_config

    return load_pc_config(path)


def resolve_pc_chat_ids_via_window(*args: Any, **kwargs: Any) -> Any:
    """Keep the ordinary CLI path independent of Tk and PC window control."""

    from kakao_sender_gui import resolve_pc_chat_ids_via_window as resolve_pc_window

    return resolve_pc_window(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    chats: tuple[dict[str, Any], ...]
    base_count: int
    catalog_candidate_count: int
    supplemental_requested_count: int
    supplemental_verified_count: int
    supplemental_unverified_count: int = 0


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    initial_config: DirectKakaoConfig
    final_config: DirectKakaoConfig
    target_names: tuple[str, ...]
    snapshot: SnapshotResult
    already_mapped_count: int
    changed_target_count: int
    verified_target_count: int
    missing: tuple[str, ...]
    unsafe: tuple[str, ...]
    ambiguous: tuple[str, ...]
    unmatched: tuple[str, ...]
    invalidated: tuple[str, ...]

    @property
    def ready_to_apply(self) -> bool:
        return (
            self.verified_target_count == len(self.target_names)
            and not self.missing
            and not self.unsafe
            and not self.ambiguous
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "status": "COMPLETE" if self.ready_to_apply else "PARTIAL",
            "csv_targets": len(self.target_names),
            "already_mapped_before_refresh": self.already_mapped_count,
            "base_server_chats": self.snapshot.base_count,
            "pc_catalog_candidates": self.snapshot.catalog_candidate_count,
            "supplemental_ids_requested": self.snapshot.supplemental_requested_count,
            "supplemental_direct_chats_verified": self.snapshot.supplemental_verified_count,
            "supplemental_ids_unverified": self.snapshot.supplemental_unverified_count,
            "verified_targets": self.verified_target_count,
            "changed_targets": self.changed_target_count,
            "missing_count": len(self.missing),
            "unsafe_count": len(self.unsafe),
            "ambiguous_count": len(self.ambiguous),
            "invalidated_count": len(self.invalidated),
            "final_total_mappings": len(self.final_config.mappings),
            "ready_to_apply": self.ready_to_apply,
            "missing_names": list(self.missing),
            "unsafe_names": list(self.unsafe),
            "ambiguous_names": list(self.ambiguous),
        }


@dataclass(frozen=True, slots=True)
class DistinctPairConfirmation:
    target_name: str
    protected_owner_name: str


@dataclass(frozen=True, slots=True)
class ConfirmedPcSelection:
    target_name: str
    chat_id: str
    pc_title: str
    peer_user_id: str
    protected_owner_name: str
    protected_chat_id: str
    protected_peer_user_id: str
    protected_mapping_label: str
    protected_was_guarded: bool
    exact_geometry: bool = True


@dataclass(frozen=True, slots=True)
class PcProbeResult:
    mappings: dict[str, str]
    mapping_labels: dict[str, str]
    unresolved: tuple[str, ...]
    confirmed_selections: tuple[ConfirmedPcSelection, ...] = ()


def _decode_csv(path: Path) -> str:
    data = path.read_bytes()
    if not data:
        raise ValueError("CSV file is empty")
    errors: list[str] = []
    for encoding in csv_encoding_candidates(data):
        try:
            text = data.decode(encoding).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        try:
            list(csv.reader(text.splitlines(), dialect=sniff_csv_dialect(text)))
        except csv.Error as exc:
            errors.append(type(exc).__name__)
            continue
        return text
    raise ValueError(f"CSV encoding or structure is invalid ({','.join(errors[:2]) or 'decode_failed'})")


def load_target_names(path: Path) -> tuple[str, ...]:
    """Load only the chat-name column; every other CSV cell is inert data."""

    text = _decode_csv(path)
    rows = list(csv.reader(text.splitlines(), dialect=sniff_csv_dialect(text)))
    header_index = -1
    name_index = -1
    for index, raw_headers in enumerate(rows):
        headers = [str(value or "").strip().lstrip("\ufeff") for value in raw_headers]
        name_column = find_column(headers, NAME_ALIASES)
        if name_column:
            header_index = index
            name_index = headers.index(name_column)
            break
    if header_index < 0 or name_index < 0:
        raise ValueError("CSV chat-name column was not found")

    names: list[str] = []
    for row_number, values in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(str(value or "").strip() for value in values):
            continue
        name = str(values[name_index] if name_index < len(values) else "").strip()
        if not name:
            raise ValueError(f"CSV row {row_number} has an empty chat name")
        names.append(name)
    if not names:
        raise ValueError("CSV has no chat names")
    normalize_manual_chat_targets(names)
    return tuple(names)


def _verified_supplemental(
    records: Iterable[Mapping[str, Any]],
    requested_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        dict(record)
        for record in records
        if positive_numeric_chat_id(record.get("chat_id")) in requested_ids
        and is_one_to_one_direct_chat(record)
        and bool(confirmed_peer_name(record))
    ]


def _unique_verified_direct_snapshot(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Admit only one unique, peer-verified two-person row per stable ID."""

    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        chat_id = positive_numeric_chat_id(record.get("chat_id"))
        if not chat_id:
            continue
        rows_by_id.setdefault(chat_id, []).append(dict(record))
    return [
        rows[0]
        for rows in rows_by_id.values()
        if len(rows) == 1
        and is_one_to_one_direct_chat(rows[0])
        and bool(confirmed_peer_name(rows[0]))
    ]


def fetch_snapshot(config: DirectKakaoConfig, target_names: tuple[str, ...]) -> SnapshotResult:
    """Fetch a read-only, server-verified snapshot without any send command."""

    bridge = KakaoDirectBridge(config.to_dict(), direct_enabled=False)
    try:
        base = [dict(chat) for chat in bridge.list_chats() if isinstance(chat, Mapping)]
        base_ids = {
            chat_id
            for chat_id in (positive_numeric_chat_id(chat.get("chat_id")) for chat in base)
            if chat_id
        }
        requested: list[str] = []
        seen = set(base_ids)

        def add(raw_chat_id: Any) -> None:
            chat_id = positive_numeric_chat_id(raw_chat_id)
            if not chat_id or chat_id in seen or len(requested) >= MAX_PC_CHAT_CATALOG_IDS:
                return
            seen.add(chat_id)
            requested.append(chat_id)

        # Current target mappings are verification-critical and take priority
        # over catalog hints if the global safety cap is ever reached.
        for name in target_names:
            add(resolve_chat_mapping(config.mappings, name))
        catalog_ids = discover_pc_chat_catalog_ids(
            base,
            saved_mapping_ids=config.mappings.values(),
        )
        for chat_id in catalog_ids:
            add(chat_id)

        supplemental: list[dict[str, Any]] = []
        for offset in range(0, len(requested), MAX_CHAT_LOOKUP_IDS):
            batch = requested[offset : offset + MAX_CHAT_LOOKUP_IDS]
            supplemental.extend(bridge.get_chats_by_ids(batch))
        requested_ids = set(requested)
        verified = _verified_supplemental(supplemental, requested_ids)
        merged = merge_chat_snapshots(base, verified)
        safe_snapshot = _unique_verified_direct_snapshot(merged)
        verified_ids = {
            positive_numeric_chat_id(chat.get("chat_id"))
            for chat in verified
        }
        return SnapshotResult(
            chats=tuple(safe_snapshot),
            base_count=len(base),
            catalog_candidate_count=len(catalog_ids),
            supplemental_requested_count=len(requested),
            supplemental_verified_count=len(verified),
            supplemental_unverified_count=len(set(requested).difference(verified_ids)),
        )
    finally:
        bridge.close()


def build_refresh_plan(
    config: DirectKakaoConfig,
    target_names: tuple[str, ...],
    snapshot: SnapshotResult,
) -> RefreshPlan:
    """Build a target-scoped plan that preserves unrelated saved mappings."""

    chats = [dict(chat) for chat in snapshot.chats]
    reconciled = reconcile_chat_mappings(
        config.mappings,
        config.mapping_labels,
        list(target_names),
        chats,
        guarded_mapping_ids=config.guarded_mapping_ids,
    )
    target_normalized = set(normalize_manual_chat_targets(target_names))
    final_mappings = dict(config.mappings)
    final_labels = dict(config.mapping_labels)

    # Remove only explicitly approved, unguarded canonical owners whose stable
    # ID is being handed to one of the CSV targets.  Manual/guarded owners and
    # every unrelated mapping remain immutable.
    approved_handoff_targets: set[str] = set()
    guarded_ids = set(config.guarded_mapping_ids)
    for handoff in reconciled.ownership_handoffs:
        old_owner = normalize_chat_match_name(handoff.old_owner)
        new_owner = normalize_chat_match_name(handoff.new_owner)
        current_owner_id = final_mappings.get(old_owner)
        current_label = config.mapping_labels.get(handoff.chat_id, "")
        if (
            new_owner in target_normalized
            and handoff.chat_id not in guarded_ids
            and current_owner_id == handoff.chat_id
            and old_owner == normalize_chat_match_name(current_label)
        ):
            final_mappings.pop(old_owner, None)
            approved_handoff_targets.add(new_owner)

    for normalized_name in target_normalized:
        old_chat_id = resolve_chat_mapping(config.mappings, normalized_name)
        # A guarded ID records an operator-confirmed ownership decision.  A
        # missing/stale server row may make that decision unsafe to *use*, but
        # unattended refresh must never erase or replace it.  Preserve it and
        # let the verification pass below report the target as unsafe.
        if old_chat_id and old_chat_id in guarded_ids:
            continue
        final_mappings.pop(normalized_name, None)
    for name in target_names:
        normalized_name = normalize_chat_match_name(name)
        chat_id = resolve_chat_mapping(reconciled.mappings, name)
        existing_owner = next(
            (
                owner
                for owner, owned_id in final_mappings.items()
                if owned_id == chat_id and owner != normalized_name
            ),
            None,
        ) if chat_id else None
        if chat_id and (
            existing_owner is None
            or normalized_name in approved_handoff_targets
        ):
            final_mappings[normalized_name] = chat_id

    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for chat in chats:
        chat_id = positive_numeric_chat_id(chat.get("chat_id"))
        if chat_id:
            rows_by_id.setdefault(chat_id, []).append(chat)
    for name in target_names:
        chat_id = resolve_chat_mapping(final_mappings, name)
        rows = rows_by_id.get(chat_id, ()) if chat_id else ()
        if len(rows) == 1:
            label = confirmed_chat_label(rows[0])
            if label:
                final_labels[chat_id] = label

    mapped_ids = set(final_mappings.values())
    final_labels = {
        chat_id: label
        for chat_id, label in final_labels.items()
        if chat_id in mapped_ids
    }
    final_labels = preserve_guarded_mapping_labels(
        final_mappings,
        final_labels,
        config.mapping_labels,
        config.guarded_mapping_ids,
    )
    final_config = replace(
        config,
        mappings=final_mappings,
        mapping_labels=final_labels,
        guarded_mapping_ids=config.guarded_mapping_ids,
    )

    missing: list[str] = []
    unsafe: list[str] = []
    verified = 0
    changed = 0
    already_mapped = 0
    for name in target_names:
        old_chat_id = resolve_chat_mapping(config.mappings, name)
        new_chat_id = resolve_chat_mapping(final_config.mappings, name)
        if old_chat_id:
            already_mapped += 1
        if old_chat_id != new_chat_id:
            changed += 1
        if not new_chat_id:
            missing.append(name)
            continue
        rows = rows_by_id.get(new_chat_id, ())
        saved_label = final_config.mapping_labels.get(new_chat_id, "")
        if len(rows) != 1:
            unsafe.append(name)
            continue
        chat = rows[0]
        current_label = confirmed_chat_label(chat)
        if (
            not is_one_to_one_direct_chat(chat)
            or not confirmed_peer_name(chat)
            or not current_label
            or normalize_chat_match_name(current_label)
            != normalize_chat_match_name(saved_label)
        ):
            unsafe.append(name)
            continue
        verified += 1

    return RefreshPlan(
        initial_config=config,
        final_config=final_config,
        target_names=target_names,
        snapshot=snapshot,
        already_mapped_count=already_mapped,
        changed_target_count=changed,
        verified_target_count=verified,
        missing=tuple(missing),
        unsafe=tuple(unsafe),
        ambiguous=tuple(reconciled.ambiguous),
        unmatched=tuple(reconciled.unmatched),
        invalidated=tuple(reconciled.invalidated),
    )


def _strict_operator_name(value: Any) -> str:
    """Normalize Unicode/whitespace without erasing meaningful separators."""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _verified_peer_user_id(chat: Mapping[str, Any]) -> str:
    if not is_one_to_one_direct_chat(chat) or not confirmed_peer_name(chat):
        return ""
    peer_user_id = str(chat.get("peer_user_id") or "").strip()
    if not peer_user_id.isdigit() or peer_user_id.startswith("0"):
        return ""
    return peer_user_id


def _rows_by_chat_id(chats: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for chat in chats:
        chat_id = positive_numeric_chat_id(chat.get("chat_id"))
        if chat_id:
            rows.setdefault(chat_id, []).append(dict(chat))
    return rows


def _validated_distinct_pair_confirmations(
    plan: RefreshPlan,
    confirmations: Iterable[DistinctPairConfirmation],
) -> tuple[DistinctPairConfirmation, ...]:
    values = tuple(confirmations)
    if not values:
        return ()
    targets_by_strict = {
        _strict_operator_name(name): name
        for name in plan.target_names
    }
    owners_by_strict = {
        _strict_operator_name(name): name
        for name in plan.initial_config.mappings
    }
    missing = {_strict_operator_name(name) for name in plan.missing}
    ambiguous = {_strict_operator_name(name) for name in plan.ambiguous}
    seen_targets: set[str] = set()
    seen_owners: set[str] = set()
    validated: list[DistinctPairConfirmation] = []
    for value in values:
        target_key = _strict_operator_name(value.target_name)
        owner_key = _strict_operator_name(value.protected_owner_name)
        target = targets_by_strict.get(target_key)
        owner = owners_by_strict.get(owner_key)
        if (
            not target
            or not owner
            or target_key == owner_key
            or target_key not in missing
            or target_key not in ambiguous
            or target_key in seen_targets
            or owner_key in seen_owners
        ):
            raise ValueError("invalid or duplicate distinct-pair confirmation")
        if resolve_chat_mapping(plan.initial_config.mappings, target):
            raise ValueError("distinct-pair target already owns a saved mapping")
        protected_chat_id = resolve_chat_mapping(plan.initial_config.mappings, owner)
        if not protected_chat_id:
            raise ValueError("distinct-pair protected owner has no saved mapping")
        if resolve_chat_mapping(plan.final_config.mappings, owner) != protected_chat_id:
            raise ValueError("distinct-pair protected owner changed during preflight")
        seen_targets.add(target_key)
        seen_owners.add(owner_key)
        validated.append(
            DistinctPairConfirmation(target, value.protected_owner_name)
        )
    return tuple(validated)


def _bind_confirmed_selection(
    plan: RefreshPlan,
    confirmation: DistinctPairConfirmation,
    mappings: Mapping[str, str],
    labels: Mapping[str, str],
) -> ConfirmedPcSelection | None:
    target = confirmation.target_name
    protected_owner = confirmation.protected_owner_name
    chat_id = str(mappings.get(target) or "").strip()
    pc_title = str(labels.get(chat_id) or "").strip()
    if not chat_id or _strict_operator_name(pc_title) != _strict_operator_name(target):
        return None
    initial = plan.initial_config
    protected_chat_id = resolve_chat_mapping(initial.mappings, protected_owner)
    if not protected_chat_id or chat_id == protected_chat_id:
        return None
    if chat_id in set(initial.mappings.values()):
        return None
    rows_by_id = _rows_by_chat_id(plan.snapshot.chats)
    candidate_rows = rows_by_id.get(chat_id, ())
    protected_rows = rows_by_id.get(protected_chat_id, ())
    if len(candidate_rows) != 1 or len(protected_rows) != 1:
        return None
    peer_user_id = _verified_peer_user_id(candidate_rows[0])
    protected_peer_user_id = _verified_peer_user_id(protected_rows[0])
    if (
        not peer_user_id
        or not protected_peer_user_id
        or peer_user_id == protected_peer_user_id
    ):
        return None
    peer_chat_ids = {
        candidate_id
        for candidate_id, rows in rows_by_id.items()
        if len(rows) == 1 and _verified_peer_user_id(rows[0]) == peer_user_id
    }
    if peer_chat_ids != {chat_id}:
        return None
    mapped_peer_ids = {
        _verified_peer_user_id(rows_by_id[mapped_id][0])
        for mapped_id in initial.mappings.values()
        if len(rows_by_id.get(mapped_id, ())) == 1
    }
    if peer_user_id in mapped_peer_ids:
        return None
    return ConfirmedPcSelection(
        target_name=target,
        chat_id=chat_id,
        pc_title=pc_title,
        peer_user_id=peer_user_id,
        protected_owner_name=protected_owner,
        protected_chat_id=protected_chat_id,
        protected_peer_user_id=protected_peer_user_id,
        protected_mapping_label=str(initial.mapping_labels.get(protected_chat_id) or ""),
        protected_was_guarded=protected_chat_id in set(initial.guarded_mapping_ids),
    )


def probe_pc_targets(
    plan: RefreshPlan,
    *,
    distinct_pair_confirmations: Iterable[DistinctPairConfirmation] = (),
) -> PcProbeResult:
    """Open only unresolved targets with empty text and return exact candidates."""

    confirmations = _validated_distinct_pair_confirmations(
        plan,
        distinct_pair_confirmations,
    )
    confirmed_targets = tuple(item.target_name for item in confirmations)
    ambiguous_normalized = {
        normalize_chat_match_name(name)
        for name in plan.ambiguous
    }
    # A PC search opens KakaoTalk's first exact-title result.  It is useful for
    # a truly missing alias, but it is not operator-selection evidence when
    # the server snapshot already proves two distinct same-name candidates.
    probe_names = tuple(
        dict.fromkeys(
            (
                *(
                    name
                    for name in plan.missing
                    if normalize_chat_match_name(name) not in ambiguous_normalized
                ),
                *confirmed_targets,
            )
        )
    )
    if not probe_names:
        return PcProbeResult({}, {}, ())
    runtime_config, warning = load_relaxed_config_data(CONFIG_PATH)
    search_box = runtime_config.get("search_box") if isinstance(runtime_config, Mapping) else None
    try:
        x = int(float(search_box.get("x", 0))) if isinstance(search_box, Mapping) else 0
        y = int(float(search_box.get("y", 0))) if isinstance(search_box, Mapping) else 0
    except (TypeError, ValueError, OverflowError):
        x = y = 0
    if warning or x <= 0 or y <= 0:
        raise RuntimeError("PC search coordinates are unavailable")

    # Reserve only rooms already assigned to another target in this CSV.  A
    # canonical server-label owner outside the CSV may still be nominated, but
    # the manual apply boundary below permits its handoff only when current
    # server evidence proves that exact canonical owner.
    probe_normalized = {normalize_chat_match_name(name) for name in probe_names}
    reserved_ids = tuple(
        chat_id
        for name in plan.target_names
        if normalize_chat_match_name(name) not in probe_normalized
        for chat_id in (resolve_chat_mapping(plan.final_config.mappings, name),)
        if chat_id
    )
    if confirmations:
        # Explicit disambiguation may add a new owner, but it must never move
        # any currently saved room (including the separately confirmed
        # 학생나B room).  Reserve every existing ID, not only CSV-target IDs.
        reserved_ids = tuple(
            dict.fromkeys((*reserved_ids, *plan.final_config.mappings.values()))
        )
    chats = [dict(chat) for chat in plan.snapshot.chats]
    first = resolve_pc_chat_ids_via_window(
        list(probe_names),
        chats,
        runtime_config,
        reserved_chat_ids=reserved_ids,
    )
    mappings = dict(first.mappings)
    labels = dict(first.mapping_labels)
    unresolved = tuple(first.unresolved)
    if first.retry_safe and unresolved:
        second = resolve_pc_chat_ids_via_window(
            list(unresolved),
            chats,
            runtime_config,
            reserved_chat_ids=(*reserved_ids, *mappings.values()),
            prepare_friend_tab=True,
        )
        mappings.update(second.mappings)
        labels.update(second.mapping_labels)
        unresolved = tuple(second.unresolved)
    if len(set(mappings.values())) != len(mappings):
        return PcProbeResult({}, {}, probe_names)
    selections: list[ConfirmedPcSelection] = []
    unresolved_names = list(unresolved)
    for confirmation in confirmations:
        selection = _bind_confirmed_selection(plan, confirmation, mappings, labels)
        if selection is None:
            mappings.pop(confirmation.target_name, None)
            if confirmation.target_name not in unresolved_names:
                unresolved_names.append(confirmation.target_name)
            continue
        selections.append(selection)
    return PcProbeResult(
        mappings,
        labels,
        tuple(dict.fromkeys(unresolved_names)),
        tuple(selections),
    )


def _confirmed_selection_is_fresh(
    plan: RefreshPlan,
    selection: ConfirmedPcSelection,
) -> bool:
    initial = plan.initial_config
    if (
        not selection.exact_geometry
        or resolve_chat_mapping(initial.mappings, selection.target_name)
        or resolve_chat_mapping(initial.mappings, selection.protected_owner_name)
        != selection.protected_chat_id
        or str(initial.mapping_labels.get(selection.protected_chat_id) or "")
        != selection.protected_mapping_label
        or (selection.protected_chat_id in set(initial.guarded_mapping_ids))
        != selection.protected_was_guarded
        or selection.chat_id in set(initial.mappings.values())
        or _strict_operator_name(selection.pc_title)
        != _strict_operator_name(selection.target_name)
    ):
        return False
    rows_by_id = _rows_by_chat_id(plan.snapshot.chats)
    candidate_rows = rows_by_id.get(selection.chat_id, ())
    protected_rows = rows_by_id.get(selection.protected_chat_id, ())
    if len(candidate_rows) != 1 or len(protected_rows) != 1:
        return False
    if (
        _verified_peer_user_id(candidate_rows[0]) != selection.peer_user_id
        or _verified_peer_user_id(protected_rows[0])
        != selection.protected_peer_user_id
        or selection.peer_user_id == selection.protected_peer_user_id
    ):
        return False
    peer_chat_ids = {
        chat_id
        for chat_id, rows in rows_by_id.items()
        if len(rows) == 1
        and _verified_peer_user_id(rows[0]) == selection.peer_user_id
    }
    return peer_chat_ids == {selection.chat_id}


def _protected_selection_invariant(
    config: DirectKakaoConfig,
    selection: ConfirmedPcSelection,
) -> bool:
    protected_id = resolve_chat_mapping(
        config.mappings,
        selection.protected_owner_name,
    )
    owners = [
        owner
        for owner, chat_id in config.mappings.items()
        if chat_id == selection.protected_chat_id
    ]
    return (
        protected_id == selection.protected_chat_id
        and len(owners) == 1
        and str(config.mapping_labels.get(selection.protected_chat_id) or "")
        == selection.protected_mapping_label
        and (selection.protected_chat_id in set(config.guarded_mapping_ids))
        == selection.protected_was_guarded
    )


def _blocked_confirmation_plan(
    plan: RefreshPlan,
    selections: Iterable[ConfirmedPcSelection],
) -> RefreshPlan:
    """Return an explicit no-change/PARTIAL plan for stale operator evidence."""

    blocked_names = tuple(
        dict.fromkeys(selection.target_name for selection in selections)
    )
    return replace(
        plan,
        final_config=plan.initial_config,
        verified_target_count=0,
        changed_target_count=0,
        missing=tuple(dict.fromkeys((*plan.missing, *blocked_names))),
        ambiguous=tuple(dict.fromkeys((*plan.ambiguous, *blocked_names))),
    )


def apply_pc_probe_to_plan(
    plan: RefreshPlan,
    probe: PcProbeResult,
) -> RefreshPlan:
    """Apply exact PC candidates through the manual, server-backed boundary."""

    config = plan.final_config
    mappings = dict(config.mappings)
    labels = dict(config.mapping_labels)
    guarded_ids = list(config.guarded_mapping_ids)
    chats = [dict(chat) for chat in plan.snapshot.chats]

    ambiguous_normalized = {
        normalize_chat_match_name(name)
        for name in plan.ambiguous
    }
    selections_by_target = {
        normalize_chat_match_name(item.target_name): item
        for item in probe.confirmed_selections
    }
    if len(selections_by_target) != len(probe.confirmed_selections):
        raise ValueError("duplicate confirmed PC selection evidence")
    if any(
        not _confirmed_selection_is_fresh(plan, selection)
        for selection in probe.confirmed_selections
    ):
        return _blocked_confirmation_plan(plan, probe.confirmed_selections)
    confirmed_targets = tuple(
        selection.target_name
        for selection in probe.confirmed_selections
    )
    for target in tuple(dict.fromkeys((*plan.missing, *confirmed_targets))):
        normalized_target = normalize_chat_match_name(target)
        selection = selections_by_target.get(normalized_target)
        target_is_confirmed_selection = selection is not None
        if (
            normalized_target in ambiguous_normalized
            and not target_is_confirmed_selection
        ):
            continue
        chat_id = probe.mappings.get(target)
        pc_label = str(probe.mapping_labels.get(chat_id, "") if chat_id else "").strip()
        title_matches = (
            _strict_operator_name(target) == _strict_operator_name(pc_label)
            if target_is_confirmed_selection
            else chat_title_matches(target, pc_label)
        )
        if not chat_id or not pc_label or not title_matches:
            continue
        if target_is_confirmed_selection and (
            selection.chat_id != chat_id
            or selection.pc_title != pc_label
            or not _confirmed_selection_is_fresh(plan, selection)
        ):
            continue
        if target_is_confirmed_selection:
            reserved_ids = tuple(
                mapped_id
                for owner_name, mapped_id in mappings.items()
                if normalize_chat_match_name(owner_name) != normalized_target
            )
        else:
            reserved_ids = tuple(
                mapped_id
                for other_name in plan.target_names
                if normalize_chat_match_name(other_name) != normalized_target
                for mapped_id in (resolve_chat_mapping(mappings, other_name),)
                if mapped_id
            )
        candidate = next(
            (
                item
                for item in manual_chat_candidates(
                    chats,
                    mappings,
                    target_name=target,
                    reserved_chat_ids=reserved_ids,
                )
                if item.chat_id == chat_id
            ),
            None,
        )
        if candidate is None:
            continue
        applied = apply_manual_chat_mapping(
            target,
            chat_id,
            chats,
            mappings,
            labels,
            selected_candidate=candidate,
            reserved_chat_ids=reserved_ids,
        )
        if not applied.success:
            continue
        mappings = dict(applied.mappings)
        labels = dict(applied.mapping_labels)
        guarded_ids.append(chat_id)

    probed_config = replace(
        config,
        mappings=mappings,
        mapping_labels=labels,
        guarded_mapping_ids=tuple(dict.fromkeys(guarded_ids)),
    )
    refreshed = build_refresh_plan(probed_config, plan.target_names, plan.snapshot)
    if any(
        not _protected_selection_invariant(refreshed.final_config, selection)
        or resolve_chat_mapping(
            refreshed.final_config.mappings,
            selection.target_name,
        )
        != selection.chat_id
        or selection.chat_id not in set(
            refreshed.final_config.guarded_mapping_ids
        )
        for selection in probe.confirmed_selections
    ):
        return _blocked_confirmation_plan(plan, probe.confirmed_selections)
    original = plan.initial_config
    changed = sum(
        1
        for name in plan.target_names
        if resolve_chat_mapping(original.mappings, name)
        != resolve_chat_mapping(refreshed.final_config.mappings, name)
    )
    return replace(
        refreshed,
        initial_config=original,
        already_mapped_count=plan.already_mapped_count,
        changed_target_count=changed,
    )


def apply_plan(config_path: Path, initial_bytes: bytes, plan: RefreshPlan) -> Path:
    if not plan.ready_to_apply:
        raise RuntimeError("refresh plan is not fully verified")
    with direct_config_lock(config_path):
        current_bytes = config_path.read_bytes()
        if hashlib.sha256(current_bytes).digest() != hashlib.sha256(initial_bytes).digest():
            raise RuntimeError("direct config changed after preflight")
        backup_path = create_adjacent_backup(config_path)
        plan.final_config.save(config_path)
        reloaded = DirectKakaoConfig.load(config_path)
        if (
            reloaded.mappings != plan.final_config.mappings
            or reloaded.mapping_labels != plan.final_config.mapping_labels
            or reloaded.guarded_mapping_ids != plan.final_config.guarded_mapping_ids
        ):
            raise RuntimeError("saved direct config failed verification")
    return backup_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh verified Kakao chat mappings from a names-only CSV")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--config", type=Path, default=DIRECT_CONFIG_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--pc-ui",
        action="store_true",
        help="explicitly search/open unresolved PC KakaoTalk rooms with empty message text",
    )
    parser.add_argument(
        "--confirm-distinct-pair",
        action="append",
        nargs=2,
        default=[],
        metavar=("TARGET", "PROTECTED_OWNER"),
        help=(
            "repeatable operator-confirmed distinct pair; the target must be "
            "current missing/ambiguous and the protected owner must keep its saved room"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pair_confirmations = tuple(
        DistinctPairConfirmation(target, protected_owner)
        for target, protected_owner in args.confirm_distinct_pair
    )
    if pair_confirmations and not args.pc_ui:
        raise ValueError("--confirm-distinct-pair requires --pc-ui")
    csv_path = args.csv_path.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    initial_bytes = config_path.read_bytes()
    config = DirectKakaoConfig.load(config_path)
    targets = load_target_names(csv_path)
    snapshot = fetch_snapshot(config, targets)
    plan = build_refresh_plan(config, targets, snapshot)
    if pair_confirmations and plan.ready_to_apply:
        raise ValueError("distinct-pair confirmation is not eligible for a complete plan")
    pc_probe = PcProbeResult({}, {}, ())
    if args.pc_ui and not plan.ready_to_apply:
        pc_probe = probe_pc_targets(
            plan,
            distinct_pair_confirmations=pair_confirmations,
        )
        plan = apply_pc_probe_to_plan(plan, pc_probe)
    pre_apply_revalidated = False
    if args.apply and plan.ready_to_apply:
        # The PC lookup can take long enough for a room/member snapshot to
        # change.  Re-read every server-backed row immediately before the
        # filesystem transaction, then replay only the already fail-closed PC
        # evidence against that fresh snapshot.
        fresh_snapshot = fetch_snapshot(plan.final_config, targets)
        fresh_plan = build_refresh_plan(config, targets, fresh_snapshot)
        if pc_probe.mappings:
            fresh_plan = apply_pc_probe_to_plan(fresh_plan, pc_probe)
        plan = fresh_plan
        pre_apply_revalidated = True
    report = plan.to_safe_dict()
    report["mode"] = "apply" if args.apply else "dry_run"
    report["pc_ui_used"] = bool(args.pc_ui)
    report["pc_targets_resolved"] = len(pc_probe.mappings)
    report["pc_targets_unresolved"] = len(pc_probe.unresolved)
    report["confirmed_distinct_pairs"] = [
        {
            "target": item.target_name,
            "protected_owner": item.protected_owner_name,
        }
        for item in pc_probe.confirmed_selections
    ]
    report["pre_apply_revalidated"] = pre_apply_revalidated
    if args.apply:
        if not plan.ready_to_apply:
            report["apply_status"] = "NO_CHANGE"
            report["apply_error"] = "preflight_incomplete"
            report["saved"] = False
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        try:
            backup_path = apply_plan(config_path, initial_bytes, plan)
        except Exception as exc:
            report["apply_status"] = "NO_CHANGE"
            report["apply_error"] = type(exc).__name__
            report["saved"] = False
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        report["backup_path"] = str(backup_path)
        report["apply_status"] = "SAVED"
        report["saved"] = True
    else:
        report["saved"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (plan.ready_to_apply or not args.apply) else 2


if __name__ == "__main__":
    raise SystemExit(main())
