"""Fail-closed recovery for persisted direct Kakao chat mappings.

The recovery input is an already-sanitized ``list_chats`` payload.  This
module deliberately does not start the bridge, inspect credentials, or print
chat labels/IDs.  It only builds a merge plan whose public report contains
counts.  Applying a plan is a separate, explicit operation that creates an
adjacent backup before delegating the write to ``DirectKakaoConfig.save``.
"""

from __future__ import annotations

import os
import shutil
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from kakao_direct import (
    DEFAULT_CONFIG_LOCK_TIMEOUT,
    DirectKakaoConfig,
    direct_config_lock,
    normalize_chat_id,
    normalize_chat_name,
)


@dataclass(frozen=True)
class RecoveryCounts:
    """Non-sensitive recovery report.

    Duplicate counters count affected input records.  A malformed record can
    be counted in both duplicate categories; no record in either category is
    eligible for automatic recovery.
    """

    input_records: int
    candidate_records: int
    eligible_records: int
    retained_mappings: int
    retained_labels: int
    added_mappings: int
    added_labels: int
    invalid_records: int
    duplicate_name_records: int
    duplicate_chat_id_records: int
    existing_name_conflicts: int
    existing_chat_id_conflicts: int
    final_mappings: int
    final_labels: int


@dataclass(frozen=True)
class RecoveryPlan:
    """A non-mutating merge result; its representation never reveals data."""

    counts: RecoveryCounts
    merged_config: DirectKakaoConfig = field(repr=False)


@dataclass(frozen=True)
class AppliedRecovery:
    """Result of an explicit backup-and-save operation."""

    counts: RecoveryCounts
    backup_path: Path
    config_path: Path


@dataclass(frozen=True, repr=False)
class _Candidate:
    normalized_name: str
    chat_id: str
    label: str


def _clean_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    label = unicodedata.normalize("NFKC", value).strip()
    label = " ".join(label.split())
    if not label or len(label) > 200:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in label):
        return ""
    return label


def _verified_peer_label(record: Mapping[str, Any]) -> str:
    """Accept the same narrow two-person peer identity shape as the bridge."""

    if str(record.get("type") or "").strip().casefold() != "directchat":
        return ""
    try:
        if int(record.get("active_members")) != 2:
            return ""
    except (TypeError, ValueError, OverflowError):
        return ""
    peer_user_id = str(record.get("peer_user_id") or "").strip()
    peer_names = record.get("peer_names")
    if (
        not peer_user_id.isdigit()
        or peer_user_id.startswith("0")
        or not isinstance(peer_names, list)
        or len(peer_names) != 1
    ):
        return ""
    return _clean_label(peer_names[0])


def _confirmed_label(record: Mapping[str, Any]) -> str:
    # ``label`` supports an intentionally reduced exported shape.  The other
    # fields are the sanitized bridge shape, in the GUI's canonical order.
    for key in ("label", "title", "display_name"):
        label = _clean_label(record.get(key))
        if label:
            return label
    return _verified_peer_label(record)


def _candidate_from_record(record: Any) -> _Candidate | None:
    if not isinstance(record, Mapping):
        return None
    try:
        chat_id = normalize_chat_id(record.get("chat_id"))
    except (TypeError, ValueError):
        return None
    label = _confirmed_label(record)
    if not label or label == f"채팅 {chat_id}":
        return None
    try:
        normalized_name = normalize_chat_name(label)
    except (TypeError, ValueError):
        return None
    return _Candidate(normalized_name=normalized_name, chat_id=chat_id, label=label)


def merge_sanitized_chat_list(
    existing: DirectKakaoConfig,
    sanitized_chats: Iterable[Mapping[str, Any]],
) -> RecoveryPlan:
    """Merge only unambiguous chat labels into a copy of ``existing``.

    Existing mappings own both their normalized name and stable chat ID.  A
    new candidate that collides with either is ignored, never substituted.
    This preserves manually confirmed aliases.  Existing mapping labels are
    also immutable; a current confirmed label only fills a missing label for
    an already-owned ID.
    """

    records = list(sanitized_chats)
    parsed: list[_Candidate] = []
    invalid_records = 0
    for record in records:
        candidate = _candidate_from_record(record)
        if candidate is None:
            invalid_records += 1
        else:
            parsed.append(candidate)

    name_counts = Counter(candidate.normalized_name for candidate in parsed)
    chat_id_counts = Counter(candidate.chat_id for candidate in parsed)
    duplicate_name_records = sum(
        1 for candidate in parsed if name_counts[candidate.normalized_name] > 1
    )
    duplicate_chat_id_records = sum(
        1 for candidate in parsed if chat_id_counts[candidate.chat_id] > 1
    )
    eligible = [
        candidate
        for candidate in parsed
        if name_counts[candidate.normalized_name] == 1
        and chat_id_counts[candidate.chat_id] == 1
    ]

    mappings = dict(existing.mappings)
    labels = dict(existing.mapping_labels)
    owners_by_chat_id = {chat_id: name for name, chat_id in mappings.items()}
    added_mappings = 0
    added_labels = 0
    existing_name_conflicts = 0
    existing_chat_id_conflicts = 0

    for candidate in eligible:
        owned_chat_id = mappings.get(candidate.normalized_name)
        if owned_chat_id is not None:
            if owned_chat_id != candidate.chat_id:
                existing_name_conflicts += 1
                continue
            if candidate.chat_id not in labels:
                labels[candidate.chat_id] = candidate.label
                added_labels += 1
            continue

        owner = owners_by_chat_id.get(candidate.chat_id)
        if owner is not None:
            # The differing owner is commonly a manually confirmed PC alias.
            # Keep it and only fill a missing verification label.
            existing_chat_id_conflicts += 1
            if candidate.chat_id not in labels:
                labels[candidate.chat_id] = candidate.label
                added_labels += 1
            continue

        mappings[candidate.normalized_name] = candidate.chat_id
        owners_by_chat_id[candidate.chat_id] = candidate.normalized_name
        labels[candidate.chat_id] = candidate.label
        added_mappings += 1
        added_labels += 1

    merged_config = replace(existing, mappings=mappings, mapping_labels=labels)
    counts = RecoveryCounts(
        input_records=len(records),
        candidate_records=len(parsed),
        eligible_records=len(eligible),
        retained_mappings=len(existing.mappings),
        retained_labels=len(existing.mapping_labels),
        added_mappings=added_mappings,
        added_labels=added_labels,
        invalid_records=invalid_records,
        duplicate_name_records=duplicate_name_records,
        duplicate_chat_id_records=duplicate_chat_id_records,
        existing_name_conflicts=existing_name_conflicts,
        existing_chat_id_conflicts=existing_chat_id_conflicts,
        final_mappings=len(merged_config.mappings),
        final_labels=len(merged_config.mapping_labels),
    )
    return RecoveryPlan(counts=counts, merged_config=merged_config)


def create_adjacent_backup(
    config_path: str | os.PathLike[str],
    *,
    timestamp: datetime | None = None,
) -> Path:
    """Copy an existing config to a new timestamped file without overwrite."""

    source = Path(config_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError("direct Kakao config does not exist")
    instant = timestamp or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = source.with_name(f"{source.name}.{stamp}.bak")

    created = False
    try:
        with source.open("rb") as reader, backup.open("xb") as writer:
            created = True
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        # Remove only a partial file created by this call.  An exclusive-open
        # collision must leave the pre-existing backup byte-for-byte intact.
        if created:
            try:
                backup.unlink()
            except OSError:
                pass
        raise
    return backup


def apply_recovery(
    config_path: str | os.PathLike[str],
    sanitized_chats: Iterable[Mapping[str, Any]],
    *,
    timestamp: datetime | None = None,
    lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
) -> AppliedRecovery:
    """Run load, merge, backup, and atomic save as one writer transaction."""

    destination = Path(config_path).expanduser()
    with direct_config_lock(destination, timeout=lock_timeout):
        existing = DirectKakaoConfig.load(destination)
        plan = merge_sanitized_chat_list(existing, sanitized_chats)
        backup_path = create_adjacent_backup(destination, timestamp=timestamp)
        # ``DirectKakaoConfig.save`` uses this same re-entrant lock.  Keeping
        # that ordinary path intact prevents an unlocked save from appearing
        # later while avoiding a nested-lock deadlock here.
        plan.merged_config.save(destination)
    return AppliedRecovery(
        counts=plan.counts,
        backup_path=backup_path,
        config_path=destination,
    )


__all__ = [
    "AppliedRecovery",
    "RecoveryCounts",
    "RecoveryPlan",
    "apply_recovery",
    "create_adjacent_backup",
    "merge_sanitized_chat_list",
]
