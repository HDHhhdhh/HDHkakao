"""Hub screen responsibilities; app state remains on the Tk owner thread."""
from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from kakao_gui_constants import VK_ESCAPE, VK_SHIFT

def windows_async_key_state_reader() -> Callable[[int], int] | None:
    """Return a Windows ``GetAsyncKeyState`` reader, or ``None`` safely.

    The reader is deliberately created only on Windows and only consumed
    while a PC-screen fallback is active.  This is a state poll, not a
    keyboard hook: no key names, characters, or other keystrokes are stored.
    """

    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_async_key_state = user32.GetAsyncKeyState
        get_async_key_state.argtypes = [ctypes.c_int]
        get_async_key_state.restype = ctypes.c_short
    except Exception:
        return None

    def read(vk_code: int) -> int:
        return int(get_async_key_state(int(vk_code)))

    return read


def shift_escape_is_down(reader: Callable[[int], int] | None = None) -> bool:
    """Return whether Shift+Escape is currently held, failing closed.

    ``reader`` is injectable for deterministic tests.  A missing reader, a
    non-Windows host, and every reader/OS error all mean ``False``.
    """

    state_reader = reader if reader is not None else windows_async_key_state_reader()
    if state_reader is None:
        return False
    try:
        shift_state = int(state_reader(VK_SHIFT))
        escape_state = int(state_reader(VK_ESCAPE))
        return bool((shift_state & 0x8000) and (escape_state & 0x8000))
    except Exception:
        return False
