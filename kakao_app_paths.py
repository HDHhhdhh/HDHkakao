"""Shared application data locations, independent of Tk and runtime state."""

from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / "KakaoSender"

def local_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "KakaoSender"

CONFIG_PATH = app_data_dir() / "kakao_sender.local.json"

DIRECT_CONFIG_PATH = app_data_dir() / "kakao_direct.json"

LOG_DIR = local_data_dir() / "logs"

SCHEDULE_JOURNAL_PATH = local_data_dir() / "schedule" / "reservations.sqlite3"
