"""Set Windows DPI awareness before Tk creates any windows.

PyAutoGUI requests process awareness when first imported. Doing that after Tk
starts changes the existing GUI's coordinate system, shrinking its window.
"""
import ctypes
import sys


def initialize_dpi_awareness():
    if sys.platform != 'win32':
        return
    user = ctypes.WinDLL('user32', use_last_error=True)
    try:
        configure = user.SetProcessDpiAwarenessContext
    except AttributeError:
        user.SetProcessDPIAware()
        return
    configure.argtypes = [ctypes.c_void_p]
    configure.restype = ctypes.c_int
    # Once configured (including by a host manifest), Windows refuses later
    # process changes. Do not replace the host's existing awareness context.
    configure(ctypes.c_void_p(-4))
