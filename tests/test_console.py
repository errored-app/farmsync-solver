"""QuickEdit is the flag that made a working tool look like a hung one.

These pin the guards rather than the Win32 call itself: the failure worth
catching is `disable_quick_edit` raising or misbehaving somewhere it has no
business running at all — a redirected run, a Linux box, a console that will
not answer — because every one of those paths runs before the first line of
output and would take the whole tool down with it.
"""

import sys

import pytest

from src import console


class FakeStream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_a_redirected_run_is_left_alone(monkeypatch):
    """`python -m src > log.txt` has no console, and a pipe cannot be clicked."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert console.disable_quick_edit(FakeStream(tty=False)) is False


def test_other_platforms_are_left_alone(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert console.disable_quick_edit(FakeStream(tty=True)) is False


def test_a_stream_that_cannot_say_whether_it_is_a_tty_is_left_alone(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    class Bare:
        pass

    assert console.disable_quick_edit(Bare()) is False


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="ctypes.windll only exists on Windows")
def test_a_console_that_refuses_is_reported_not_raised(monkeypatch):
    """The tool must run on a host where the flag cannot be changed."""
    import ctypes

    class Refusing:
        def GetStdHandle(self, _):
            return 1

        def GetConsoleMode(self, *_):
            return 0        # the documented failure return

        def SetConsoleMode(self, *_):
            raise AssertionError("must not be reached after a failed read")

    monkeypatch.setattr(ctypes, "windll",
                        type("W", (), {"kernel32": Refusing()})())
    assert console.disable_quick_edit(FakeStream(tty=True)) is False


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="ctypes.windll only exists on Windows")
def test_quick_edit_is_cleared_and_every_other_flag_survives(monkeypatch):
    """Line and echo input are how the settings menu reads keys.

    Clearing them here would break a screen that has nothing to do with this,
    which is why the mode is edited rather than replaced.
    """
    import ctypes

    seen = {}
    other_flags = 0x0001 | 0x0002 | 0x0004      # processed, line, echo input

    class Console:
        def GetStdHandle(self, which):
            seen["handle"] = which
            return 1

        def GetConsoleMode(self, _handle, out):
            out._obj.value = other_flags | console.ENABLE_QUICK_EDIT
            return 1

        def SetConsoleMode(self, _handle, mode):
            seen["mode"] = mode
            return 1

    monkeypatch.setattr(ctypes, "windll",
                        type("W", (), {"kernel32": Console()})())
    assert console.disable_quick_edit(FakeStream(tty=True)) is True
    assert seen["handle"] == console.STD_INPUT_HANDLE
    assert not seen["mode"] & console.ENABLE_QUICK_EDIT
    # Without EXTENDED_FLAGS the call succeeds and changes nothing — the whole
    # fix silently does not happen.
    assert seen["mode"] & console.ENABLE_EXTENDED_FLAGS
    assert seen["mode"] & other_flags == other_flags
