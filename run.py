"""PyInstaller entry point.

``src`` is a package built on relative imports, so ``src/__main__.py`` cannot
be handed to PyInstaller as a script — it would be compiled outside the
package and every ``from .`` would fail. This module exists only to import the
package the normal way and hand off. ``python -m src`` still goes through
``src/__main__.py``; both are one call to the same function.

The console-holding is here rather than in ``launch`` on purpose: it must not
change ``python -m src``, which runs in a terminal the operator already owns.
"""

import sys

from src import paths
from src.bootstrap import launch
from src.output import Output


def _pause(ask=input) -> None:
    """Hold the window open long enough to read why the tool stopped.

    Launched from Explorer, a PyInstaller build's console is destroyed the
    instant the process exits, so every early return — an abandoned wizard,
    the startup guard, a rejected API key, a malformed config.json — prints
    its reason to a window that is gone before it can be read. The README's
    install step is "double-click it", so that is the normal case, not an
    exotic one.

    Gated on both halves. ``frozen()`` keeps it out of ``python -m src``, and
    the tty check keeps it out of a scripted or redirected run, where a
    blocking read on a closed stdin is a process that hangs forever instead of
    one that stops.
    """
    if not paths.frozen():
        return
    stdin = getattr(sys, "stdin", None)
    try:
        if stdin is None or not stdin.isatty():
            return
    except (AttributeError, ValueError, OSError):
        return
    try:
        ask("Press Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    try:
        launch(sys.argv)
    except (KeyboardInterrupt, SystemExit):
        # Both already carry the operator's intent and their own exit status.
        # Re-raised rather than reported, so Ctrl-C still terminates and a
        # requested exit code still reaches the shell.
        _pause()
        raise
    except BaseException as e:
        Output.error(f"{type(e).__name__}: {e}")
        _pause()
        raise
    _pause()
