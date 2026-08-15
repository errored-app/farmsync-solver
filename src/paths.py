"""The single answer to "where does this file live".

Frozen under PyInstaller, ``__file__`` points inside ``sys._MEIPASS`` — a
directory unpacked on every launch and deleted on exit. ``src/util.py`` and
``src/state.py`` both computed their paths that way, so a packaged build would
never find its config, and would recreate ``state.db`` from empty every run.
The second half is the dangerous one: the tool keeps working, it just forgets
every ban and every grace stamp, and nothing in the output says so.

Source runs are deliberately unchanged. The test suite, the operator's
diagnostic probes, and the operator's own workflow all assume
``input/config.json`` and ``data/state.db`` sit beside ``src/``, and none of
those are ever packaged.

These are functions rather than module constants so that the environment can
be changed and the answer change with it — which is what makes the portable
layout testable without freezing anything.
"""

import os
import sys
from pathlib import Path

APP_NAME = "FarmsyncSolver"

# Forces the portable layout from a source run. The only way to exercise a
# frozen build's paths without building one.
OVERRIDE_ENV = "FARMSYNC_DATA_DIR"

REPO_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    """True inside a PyInstaller build. It sets ``sys.frozen``; nothing else does."""
    return bool(getattr(sys, "frozen", False))


def portable() -> bool:
    """True when the tool keeps its files outside the source tree."""
    return frozen() or bool(os.environ.get(OVERRIDE_ENV))


def user_dir() -> Path:
    """The one directory holding everything that must survive an update.

    %LOCALAPPDATA% rather than %APPDATA%: state.db is machine-local account
    state with no business following a user to another PC, and roaming live
    credentials between machines is a surprise rather than a feature.
    """
    override = os.environ.get(OVERRIDE_ENV)
    if override:
        return Path(override)
    if frozen():
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    return REPO_ROOT


def config_file() -> Path:
    if portable():
        return user_dir() / "config.json"
    return REPO_ROOT / "input" / "config.json"


def state_db() -> Path:
    if portable():
        return user_dir() / "state.db"
    return REPO_ROOT / "data" / "state.db"


def updates_dir() -> Path:
    """Scratch space for a download, kept out of the .exe's own folder so a
    half-finished download can never be mistaken for an installed build."""
    return user_dir() / "updates"


def exe_dir():
    """The folder holding the running .exe, or None when running from source.

    ``sys.executable`` is the .exe itself in a frozen build and the Python
    interpreter otherwise, which is why this is gated on ``frozen()``.
    """
    if not frozen():
        return None
    return Path(sys.executable).resolve().parent
