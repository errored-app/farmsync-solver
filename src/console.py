"""Stop the Windows console from freezing the tool.

A legacy conhost ships with **QuickEdit** on. One click anywhere in the window
puts it into selection mode — the title gains a `Select ` prefix — and every
write to stdout blocks until the operator presses Enter or Esc. The process is
not hung, but nothing on screen says so, and there is no way for it to say so
either: the one channel that could explain it is the channel being blocked.

That is exactly how it was found. An outage printed 65 failures, the operator
clicked into the window to read them, and the tool went silent for minutes. It
looked like a crash. Pressing Enter released the whole backlog at once.

Worse than cosmetic on a daemon: a blocked write inside `Output` is held under
the module lock every worker shares, so one stray click can stall all 65 of
them behind a lock that is waiting on a mouse.

The trade is real and deliberate: with QuickEdit off, dragging the mouse no
longer selects text in a legacy console. Use the window menu (right-click the
title bar → Edit → Mark) to copy, or Windows Terminal, which does its own
selection and is unaffected either way. Not stalling is worth more than
click-to-select on a tool meant to run unattended for hours.

Everything here is best-effort and silent on failure. Not being able to change
a console flag must never stop the tool from solving.
"""

import sys

# From the Win32 console API. Clearing QUICK_EDIT only takes effect when
# EXTENDED_FLAGS is set in the same call, which is the part that is easy to
# miss — without it the call succeeds and changes nothing.
ENABLE_QUICK_EDIT = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10


def disable_quick_edit(stream=None) -> bool:
    """Turn off click-to-freeze. True if it was actually changed.

    A False return is not an error and is not reported: every non-Windows
    platform, every redirected run, and every terminal that does not offer the
    flag lands there, and none of them have the problem in the first place.
    """
    stream = sys.stdout if stream is None else stream
    if not sys.platform.startswith("win"):
        return False
    # `python -m src > log.txt` has no console to fix, and a pipe cannot be
    # clicked into.
    if not getattr(stream, "isatty", lambda: False)():
        return False

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # Every other flag is preserved. Line and echo input are what the
        # settings menu reads keys with, and clearing them here would break a
        # screen that has nothing to do with this.
        wanted = (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED_FLAGS
        if wanted == mode.value:
            return False
        return bool(kernel32.SetConsoleMode(handle, wanted))
    except Exception:
        # Wine, a stripped ctypes, a handle we are not allowed to touch. The
        # tool runs fine without this; it just stays clickable-to-freeze.
        return False
