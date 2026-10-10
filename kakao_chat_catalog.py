"""Read-only catalog discovery; candidate IDs are never mapping evidence."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from kakao_mapping_policy import positive_numeric_chat_id
from kakao_pc_chat_map import discover_pc_chat_map

MAX_PC_CHAT_CATALOG_IDS: Final[int] = 512


def discover_pc_chat_catalog_ids(
    tablet_chats: Iterable[Mapping[str, Any]],
    *,
    saved_mapping_ids: Iterable[Any] = (),
    provider_discovery: Callable[[Any], Any] | None = None,
) -> tuple[str, ...]:
    """Return bounded PC-metadata IDs absent from the aggregate chat list.

    IDs come only from the selected account's read-only ``chatWindowUi``
    metadata.  They are discovery hints, never mapping evidence: callers must
    independently fetch CHATINFO and a stable member snapshot before a row can
    enter reconciliation or the manual chooser.
    """

    tablet_ids: list[str] = []
    seen_tablet_ids: set[str] = set()
    for chat in tablet_chats:
        if not isinstance(chat, Mapping):
            continue
        chat_id = positive_numeric_chat_id(chat.get("chat_id"))
        if not chat_id or chat_id in seen_tablet_ids:
            continue
        seen_tablet_ids.add(chat_id)
        tablet_ids.append(chat_id)

    seed_ids = list(tablet_ids)
    seen_seed_ids = set(tablet_ids)
    if not isinstance(saved_mapping_ids, (str, bytes, bytearray, Mapping)):
        try:
            raw_seed_ids = tuple(saved_mapping_ids)
        except TypeError:
            raw_seed_ids = ()
        for raw_chat_id in raw_seed_ids:
            chat_id = positive_numeric_chat_id(raw_chat_id)
            if not chat_id or chat_id in seen_seed_ids:
                continue
            seen_seed_ids.add(chat_id)
            seed_ids.append(chat_id)
    if not seed_ids:
        return ()

    discover = provider_discovery if provider_discovery is not None else discover_pc_chat_map
    if discover is None:
        return ()
    try:
        discovery = discover(tuple(seed_ids))
        provider = getattr(discovery, "provider", None)
        catalog_reader = getattr(provider, "list_window_chat_id_candidates", None)
        if not callable(catalog_reader):
            return ()
        raw_candidates = tuple(catalog_reader())
    except Exception:
        return ()

    candidates: list[str] = []
    seen_candidates: set[str] = set()
    normalized_candidates = {
        chat_id
        for chat_id in (positive_numeric_chat_id(value) for value in raw_candidates)
        if chat_id
    }
    for chat_id in sorted(normalized_candidates, key=lambda value: (len(value), value)):
        if (
            chat_id in seen_tablet_ids
            or chat_id in seen_candidates
        ):
            continue
        seen_candidates.add(chat_id)
        candidates.append(chat_id)
        if len(candidates) >= MAX_PC_CHAT_CATALOG_IDS:
            break
    return tuple(candidates)
