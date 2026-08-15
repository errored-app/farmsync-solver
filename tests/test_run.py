"""The console-holding guard on the packaged entry point.

Under Explorer a PyInstaller console is destroyed the instant the process
exits, so a build that stops early — an abandoned wizard, the startup guard, a
rejected API key, a malformed config.json — prints its reason into a window
that is already gone. `run._pause` is what keeps it readable.

The guard is the whole test surface here: it must fire for a frozen build in
front of a person, and never anywhere else. `python -m src` runs in a terminal
the operator already owns, and a scheduled or redirected run has no one to
press the key — a blocking read there is a process that hangs forever rather
than one that stops.
"""

import run


class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def never(_prompt):
    raise AssertionError("paused a run that has no one watching it")


def test_a_source_run_never_pauses(monkeypatch):
    """python -m src must behave exactly as it does today."""
    monkeypatch.setattr(run.paths, "frozen", lambda: False)
    monkeypatch.setattr(run.sys, "stdin", FakeStdin(tty=True))
    run._pause(ask=never)


def test_a_frozen_run_with_no_terminal_never_pauses(monkeypatch):
    """Redirected or scheduled: nobody is there to press Enter, and a blocking
    read on a closed stdin is a hang rather than a stop."""
    monkeypatch.setattr(run.paths, "frozen", lambda: True)
    monkeypatch.setattr(run.sys, "stdin", FakeStdin(tty=False))
    run._pause(ask=never)


def test_a_frozen_run_with_no_stdin_at_all_never_pauses(monkeypatch):
    """pythonw-style launches leave sys.stdin as None."""
    monkeypatch.setattr(run.paths, "frozen", lambda: True)
    monkeypatch.setattr(run.sys, "stdin", None)
    run._pause(ask=never)


def test_a_frozen_run_in_front_of_a_person_waits(monkeypatch):
    """The double-clicked case the README documents as the install step."""
    monkeypatch.setattr(run.paths, "frozen", lambda: True)
    monkeypatch.setattr(run.sys, "stdin", FakeStdin(tty=True))

    asked = []
    run._pause(ask=asked.append)

    assert len(asked) == 1
    assert "Enter" in asked[0]


def test_stdin_disappearing_mid_check_is_not_an_error(monkeypatch):
    """A detached console answers isatty() with ValueError on a closed file.
    Failing to pause is a cosmetic loss; raising here would turn a clean exit
    into a crash report."""
    class Detached:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(run.paths, "frozen", lambda: True)
    monkeypatch.setattr(run.sys, "stdin", Detached())
    run._pause(ask=never)


def test_a_closed_stdin_at_the_prompt_is_swallowed(monkeypatch):
    """isatty() can say yes and the read still hit EOF. The pause is the last
    thing the process does, so there is nothing left to protect by raising."""
    monkeypatch.setattr(run.paths, "frozen", lambda: True)
    monkeypatch.setattr(run.sys, "stdin", FakeStdin(tty=True))

    def closed(_prompt):
        raise EOFError

    run._pause(ask=closed)
