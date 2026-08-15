"""The startup menu and the settings editor.

The wizard in ``bootstrap.py`` runs once, on the first launch. Everything after
that used to require finding ``config.json`` by hand and editing JSON — and a
packaged build hides it under ``%LOCALAPPDATA%``, where an operator who typed
the wrong thread count has no reason to look. This module is the answer to
both: it prints the path on every launch, and it lets the four values worth
changing be changed in place.

Only four. ``config.json`` holds eleven keys, and the other seven are the
measured ones — grace, backoff, poll intervals, the alert threshold. Those are
deliberate settings backed by evidence in CLAUDE.md, not knobs to nudge from a
menu, and an editor that offered them would invite exactly that. They stay
editable by hand, in the file whose path this screen prints.

Like ``dispatch.py``, ``credit.py``, and ``health.py``, this module reads no
config and calls no clock: the clock, the sleep, and the key reader all arrive
as arguments. So the ten-second countdown is asserted by arithmetic rather than
by waiting ten seconds. It is also why it can be imported from ``bootstrap.py``,
which runs before ``util.py`` and ``solver.py`` are importable at all.
"""

import json
import math
import sys
import time
from collections import namedtuple

from .output import Output
from .version import __version__

# How long the menu waits before starting on its own. The operator who
# double-clicks the .exe and walks away is the normal case; a menu that waits
# forever turns that into a tool that silently never ran.
MENU_SECONDS = 10

# How often the console is asked whether a key is waiting. Small enough that
# Enter feels instant, large enough that the wait is not a spin.
POLL_SECONDS = 0.05

# Only these get a line. A countdown printing all ten is nine lines of noise.
COUNTDOWN_AT = (10, 5, 3, 2, 1)

# Three tries, then give up — the same bound the wizard takes, for the same
# reason: a closed stdin raises on every read, and an unbounded loop there is an
# invisible spin in a process the operator has no way to interrupt.
TRIES = 3

MASK_KEEP = 4
MASK_STARS = 10

# `min(config["threads"], status["max_concurrent"])` sizes the pool, so the
# dibycap plan is the real ceiling and this is only a guard against a typo that
# would try to start ten thousand threads before that comparison is ever made.
MAX_THREADS = 500

OFF = "(off)"
UNSET = "(not set)"

Field = namedtuple("Field", "key label kind hint")

FIELDS = (
    Field("api_key", "dibycap API key", "secret",
          "Get this from your dibycap account."),
    Field("farm_token", "farmsync token", "secret",
          "Get this from farmsync.cloud."),
    Field("threads", "threads", "int",
          f"How many accounts to work on at once, 1 to {MAX_THREADS}. "
          f"Your dibycap plan lowers this on its own if it is too high."),
    Field("discord_webhook_url", "Discord webhook", "url",
          "Where the low-credit alert goes. Leave empty for no alerts."),
)

# Enter starts, S opens the settings, Q stops. Ctrl-C and Escape arrive as
# characters rather than as exceptions, because the key reader polls the
# console instead of blocking inside input().
ACTIONS = {"\r": "start", "\n": "start", "s": "settings",
           "q": "quit", "\x03": "quit", "\x1b": "quit"}


def field(key) -> Field:
    """The one Field with this key. Raises rather than returning None, because
    every caller names a key that is written into FIELDS just above."""
    for f in FIELDS:
        if f.key == key:
            return f
    raise KeyError(key)


def mask(value: str) -> str:
    """Enough to recognise which key it is, never enough to use it.

    The run of stars is a fixed length rather than the value's length: a real
    dibycap key and a real farmsync token are different sizes, and printing
    that difference tells anyone reading over a shoulder which is which.
    """
    value = str(value)
    if len(value) <= MASK_KEEP * 2:
        return "*" * MASK_STARS
    return value[:MASK_KEEP] + "*" * MASK_STARS


def show(f: Field, config: dict) -> str:
    """What the screen prints for one field. Never the secret itself."""
    if f.key not in config:
        return UNSET
    value = config[f.key]
    if f.kind == "int":
        return str(value)
    value = str(value)
    if not value.strip():
        # A webhook is genuinely optional; a credential is genuinely missing.
        return OFF if f.kind == "url" else UNSET
    return mask(value)


def credentials_ok(config: dict) -> bool:
    """Whether ``main.py``'s startup guard would let this config run.

    Asked one screen earlier than the guard asks it, because this is the screen
    that can fix it. A countdown into an error message helps nobody.
    """
    for key in ("api_key", "farm_token"):
        value = str(config.get(key, "")).strip()
        if not value or "REPLACE" in value:
            return False
    return True


def parse(f: Field, answer: str):
    """The value to store, or ValueError carrying a line to show the operator.

    One copy, shared with the wizard: a rule enforced in the editor and not in
    the wizard is a rule the first run walks straight past.
    """
    answer = answer.strip()
    if "REPLACE" in answer:
        raise ValueError("That is the placeholder text, not a real value.")
    if f.kind == "int":
        try:
            number = int(answer)
        except ValueError:
            raise ValueError("That must be a number.") from None
        if not 1 <= number <= MAX_THREADS:
            raise ValueError(f"That must be a number between 1 and {MAX_THREADS}.")
        return number
    if f.kind == "url":
        if not answer:
            return ""
        if not answer.lower().startswith("https://"):
            raise ValueError("A webhook URL must start with https://")
        return answer
    if not answer:
        raise ValueError("That cannot be empty.")
    return answer


def load(path) -> dict:
    """The config as a dict, or an empty one if it cannot be read.

    Deliberately silent about the failure. A file hand-edited into invalid JSON
    must still reach this screen, which is the one place it can be repaired —
    every other reader of that file crashes on it at import time.
    """
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save(path, key, value) -> None:
    """Change one key and leave the rest of the file exactly as it was.

    The editor offers four keys; the file holds eleven. Writing back only what
    the screen showed would delete the other seven, and the defaults that
    replaced them would be indistinguishable from the operator's own choices.
    """
    config = load(path)
    config[key] = value
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
        f.write("\n")


def _console_poll():
    """The key waiting on the console, or None. Never blocks.

    ``msvcrt`` is Windows-only and this is a Windows tool, but a source run on
    anything else must not crash — it simply never sees a key, so the menu
    counts down and starts, which is the right answer there anyway.
    """
    try:
        import msvcrt
    except ImportError:
        return None
    if not msvcrt.kbhit():
        return None
    return msvcrt.getwch()


def wait_for_key(deadline, poll, clock, sleep, tick=None):
    """A key, or None once the deadline passes.

    Takes a deadline rather than a duration on purpose: an ignored keypress
    sends the caller round again, and a duration would restart the countdown
    every time. Resting a hand on the keyboard would hold the tool on the menu
    forever, one ignored key at a time.
    """
    shown = None
    while True:
        key = poll()
        if key is not None:
            return key
        now = clock()
        if now >= deadline:
            return None
        if tick is not None:
            remaining = math.ceil(deadline - now)
            if remaining != shown:
                shown = remaining
                tick(remaining)
        sleep(POLL_SECONDS)


def _blocking_choice(ask, echo) -> str:
    """The menu without a timer, for a config that cannot start."""
    for _ in range(TRIES):
        try:
            answer = ask("  [S] Settings   [Q] Quit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if answer.startswith("s"):
            return "settings"
        if answer.startswith("q"):
            return "quit"
        echo("  Press S to fix the settings, or Q to stop.")
    return "quit"


def choose(path, config, *, ask=input, echo=Output.info, poll=_console_poll,
           clock=time.monotonic, sleep=time.sleep, timeout=MENU_SECONDS) -> str:
    """Draw the menu and answer "start", "settings", or "quit"."""
    echo("")
    echo(f"FarmsyncSolver {__version__}")
    echo(f"Settings file: {path}")

    if not credentials_ok(config):
        echo("")
        echo("The dibycap API key or the farmsync token is not set.")
        echo("The tool cannot start until both are set.")
        return _blocking_choice(ask, echo)

    echo("")
    echo("[Enter] Start now   [S] Settings   [Q] Quit")

    def countdown(remaining):
        if remaining in COUNTDOWN_AT:
            echo(f"starting in {remaining}...")

    deadline = clock() + timeout
    while True:
        key = wait_for_key(deadline, poll, clock, sleep, tick=countdown)
        if key is None:
            return "start"
        action = ACTIONS.get(key.lower())
        if action is not None:
            return action


def edit(path, ask=input, echo=Output.info) -> None:
    """The editor loop. Returns when the operator is done."""
    while True:
        config = load(path)
        echo("")
        echo(f"Settings file: {path}")
        for number, f in enumerate(FIELDS, start=1):
            echo(f"  {number}  {f.label:<16} {show(f, config)}")
        echo("")
        try:
            answer = ask("  [number] change   [Enter] back: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not answer:
            return
        if not answer.isdigit() or not 1 <= int(answer) <= len(FIELDS):
            echo("  Type one of the numbers above, or press Enter to go back.")
            continue

        chosen = FIELDS[int(answer) - 1]
        echo(f"  {chosen.hint}")
        try:
            typed = ask(f"  New {chosen.label} (Enter keeps it): ")
        except (EOFError, KeyboardInterrupt):
            return
        if not typed.strip():
            continue
        try:
            value = parse(chosen, typed)
        except ValueError as e:
            echo(f"  {e}")
            continue
        save(path, chosen.key, value)
        echo(f"  Saved. {chosen.label} is now {show(chosen, load(path))}")


def screen(path, argv=(), *, ask=input, echo=Output.info, poll=_console_poll,
           clock=time.monotonic, sleep=time.sleep, timeout=MENU_SECONDS,
           tty=None) -> bool:
    """The whole screen. True means start the tool, False means stop.

    Two runs never see it. ``python -m src > log.txt`` is unattended, and a menu
    there is a hang with no visible prompt — the worst shape this can take.
    ``--grace-report`` is a read-only report the operator asked for by name, so
    interrupting it with a menu would be answering a question nobody asked.
    """
    if tty is None:
        tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    if not tty or "--grace-report" in argv:
        return True

    if "--settings" in argv:
        edit(path, ask=ask, echo=echo)

    while True:
        action = choose(path, load(path), ask=ask, echo=echo, poll=poll,
                        clock=clock, sleep=sleep, timeout=timeout)
        if action == "start":
            return True
        if action == "quit":
            return False
        edit(path, ask=ask, echo=echo)
