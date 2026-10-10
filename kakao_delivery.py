"""Delivery route presentation and progress counts without Tk state."""
from __future__ import annotations
from collections import Counter
from datetime import datetime
from typing import Iterable, Mapping, Any
from kakao_mapping_policy import normalize_chat_match_name, resolve_chat_mapping
from kakao_sender import MessageRow, schedule_datetime_for_row

SUCCESS_STATUSES = frozenset({"sent", "pasted", "scheduled", "schedule_acknowledged", "direct_sent"})


def delivery_route(row: MessageRow, common_schedule: datetime | None, direct_enabled: bool,
                   mappings: Mapping[str, str], excluded_names: Iterable[str] = ()) -> str:
    if schedule_datetime_for_row(row, common_schedule) is not None:
        return "예약 등록"
    if not direct_enabled:
        return "화면"
    if normalize_chat_match_name(row.chat_name) in {normalize_chat_match_name(n) for n in excluded_names}:
        return "매칭 제외"
    return "직접" if resolve_chat_mapping(mappings, row.chat_name) else "매칭 필요"


def delivery_routes(rows: Iterable[MessageRow], common_schedule: datetime | None,
                    direct_enabled: bool, mappings: Mapping[str, str],
                    excluded_names: Iterable[str] = ()) -> list[str]:
    """Calculate a table's routes with one exclusion and fallback index.

    Exact mapping keys retain priority over normalized name matches. The
    indexes are built only when a row needs them; scheduled and screen routes
    do not read mapping data. The caller's mappings are never changed.
    """
    routes: list[str] = []
    excluded: set[str] | None = None
    normalized_ids: dict[str, str] | None = None
    for row in rows:
        if schedule_datetime_for_row(row, common_schedule) is not None:
            routes.append("예약 등록")
            continue
        if not direct_enabled:
            routes.append("화면")
            continue
        if excluded is None:
            excluded = {normalize_chat_match_name(name) for name in excluded_names}
        name = normalize_chat_match_name(row.chat_name)
        if name in excluded:
            routes.append("매칭 제외")
            continue
        exact = str(mappings.get(row.chat_name.strip()) or "").strip()
        if exact:
            routes.append("직접")
            continue
        if normalized_ids is None:
            candidates: dict[str, set[str]] = {}
            for mapped_name, chat_id in mappings.items():
                value = str(chat_id).strip()
                if value:
                    candidates.setdefault(normalize_chat_match_name(mapped_name), set()).add(value)
            normalized_ids = {
                mapped_name: next(iter(values)) if len(values) == 1 else ""
                for mapped_name, values in candidates.items()
            }
        routes.append("직접" if normalized_ids.get(name) else "매칭 필요")
    return routes


def route_summary(routes: Iterable[str]) -> str:
    counts = Counter(routes)
    return " · ".join(f"{label} {counts[label]}건" for label in
                      ("직접", "화면", "예약 등록", "매칭 제외", "매칭 필요") if counts[label]) or "발송 대상 없음"


def progress_summary(total: int, results: Iterable[Mapping[str, Any]], running: Iterable[int] = ()) -> str:
    records = list(results)
    done_indexes = {int(r["row_index"]) for r in records if r.get("row_index") is not None}
    active = len(set(running) - done_indexes)
    success = sum(r.get("status") in SUCCESS_STATUSES for r in records)
    failed = sum(r.get("status") == "error" and r.get("transport") != "excluded" for r in records)
    unknown = sum(r.get("status") == "unknown" for r in records)
    stopped = sum(r.get("status") == "stopped" for r in records)
    excluded = sum(r.get("transport") == "excluded" or r.get("status") == "skipped" for r in records)
    retryable = sum(r.get("status") == "error" and r.get("retry_transport") == "macro" for r in records)
    pending = max(0, total - len(records) - active)
    return (f"대기: {pending} | 처리 중: {active} | 성공: {success} | 실패: {failed} | "
            f"확인: {unknown} | 중단: {stopped} | 제외: {excluded} | 재시도: {retryable}")
