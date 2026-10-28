"""Bounded local matching evidence. Never store messages or credentials."""
from datetime import datetime, timezone

from kakao_app_paths import LOG_DIR
from kakao_direct import atomic_write_json
from kakao_mapping_policy import resolve_chat_mapping, unverified_alias_mapping_names


def record_matching_diagnostic(reason, names, mappings, labels, chats, *,
                               guarded_ids=(), error_code=None, directory=None):
    """Keep the latest check and latest problem independently across restarts."""
    missing = [name for name in names if not resolve_chat_mapping(mappings, name)]
    unverified = unverified_alias_mapping_names(
        names, mappings, labels, chats, guarded_mapping_ids=guarded_ids)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "error_code": error_code,
        "target_count": len(names),
        "missing": missing,
        "unverified": list(unverified),
        "targets": [{"name": name, "chat_id": resolve_chat_mapping(mappings, name)} for name in names],
        # Explicit allowlist: never serialize raw bridge payloads (which may
        # include last_message), SDK errors, or runtime configuration.
        "rooms": [{key: row.get(key) for key in (
            "chat_id", "type", "active_members", "title", "display_name",
            "peer_user_id", "peer_names",
        )} for row in chats],
    }
    root = directory if directory is not None else LOG_DIR
    try:
        atomic_write_json(root / "matching-latest.json", report)
        if missing or unverified or error_code:
            atomic_write_json(root / "matching-last-problem.json", report)
    except (OSError, ValueError):
        return False
    return True
