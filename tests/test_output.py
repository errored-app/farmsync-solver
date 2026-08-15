"""The one thing about `Output` that can kill the daemon.

Everything else in this module is colour and layout — wrong looks wrong and the
operator says so. This one fails hard and only when nobody is watching.
"""

import os
import subprocess
import sys

from conftest import ROOT
from src.output import TitleBar


def run_redirected(code: str):
    """Run a snippet with stdout piped, exactly as `python -m src > log.txt` does.

    `PYTHONIOENCODING` is cleared so the child falls back to the console's own
    code page — cp1252 on this machine — which is the condition that broke it.
    """
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, env=env)


def test_a_banner_survives_being_redirected_to_a_file():
    """Found live: `python -m src > log.txt` died on the Round 1
    banner. colorama writes straight through to a cp1252 stream, which cannot
    encode the box-drawing rule, so the daemon crashed the moment its output was
    logged rather than watched.
    """
    result = run_redirected(
        "from src.output import Output; Output.banner('PARKED — out of credit')")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_a_result_line_survives_being_redirected_to_a_file():
    result = run_redirected(
        "from src.output import Output; Output.info('resuming — dibycap is back')")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


# --- the terminal title -------------------------------------------------


class FakeStream:
    """A stream that knows whether it is a terminal, as `sys.stdout` does."""

    def __init__(self, tty=True):
        self.tty = tty
        self.written = []

    def isatty(self):
        return self.tty

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_title_is_written_as_an_osc_escape():
    stream = FakeStream()
    TitleBar(stream=stream).update("670q 412done")
    assert stream.written == ["\033]0;670q 412done\007"]


def test_nothing_is_written_when_stdout_is_not_a_terminal():
    """`python -m src > log.txt` would otherwise collect one escape sequence per
    dispatch as garbage in the middle of the log."""
    stream = FakeStream(tty=False)
    bar = TitleBar(stream=stream)
    bar.update("670q 412done")
    bar.restore()
    assert stream.written == []


def test_updates_inside_the_interval_are_dropped():
    """65 workers finishing dispatches would otherwise write the title hundreds
    of times a second."""
    clock = FakeClock()
    stream = FakeStream()
    bar = TitleBar(stream=stream, clock=clock, interval=0.25)
    bar.update("first")
    clock.now = 0.1
    bar.update("second")
    assert len(stream.written) == 1


def test_an_update_after_the_interval_is_written():
    clock = FakeClock()
    stream = FakeStream()
    bar = TitleBar(stream=stream, clock=clock, interval=0.25)
    bar.update("first")
    clock.now = 0.25
    bar.update("second")
    assert stream.written[-1] == "\033]0;second\007"


def test_restoring_the_title_ignores_the_throttle():
    """Exit is the one update that cannot be dropped, or the operator is left
    with a stale title on a process that has gone."""
    clock = FakeClock()
    stream = FakeStream()
    bar = TitleBar(stream=stream, clock=clock, interval=0.25)
    bar.update("working")
    bar.restore()
    assert stream.written[-1] == "\033]0;\007"
