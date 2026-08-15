"""Everything that must happen before the rest of the tool can be imported.

``src/util.py`` opens the config at module scope and ``src/solver.py`` binds
``API_KEY`` at module scope, so by the time ``src.main`` is importable the
config file must already exist on disk. This module is what makes that true,
which is why it imports ``paths``, ``update``, and ``output`` — never
``util``, ``solver``, or ``main``. ``src/output.py`` and ``src/update.py``
both read no config, so they are safe.
"""

import json
import sys
from pathlib import Path

import requests

from . import paths, settings, update
from .output import Output
from .version import __version__

# The packaged build reads and writes exactly one config, at
# `paths.config_file()`, and nothing else on the machine is a candidate.
#
# It used to also adopt a `config.json` or `input/config.json` found beside the
# .exe, as an upgrade path off the source version. That ran in the wild and
# failed: the .exe sat on a Desktop that already held an unrelated folder named
# `input` with a `config.json` in it, and the tool started up on credentials
# the operator had never given it — no wizard, no error, just a rejected key
# and a thread count nobody chose. The folder holding the .exe is the operator's
# folder, not the tool's, and the Desktop is the likeliest place on the machine
# to hold a stray config. Re-typing two credentials once is the cheaper side of
# that trade.


# Everything a config needs besides the two credentials. A third copy of the
# key list — config.json, config.example.json, and this — so a test pins it to
# the example rather than trusting it. The drift it guards against is silent:
# a key that reaches config.json and not config.example.json is simply missing
# from a fresh clone, with nothing saying so.
DEFAULTS = {
    "threads": 45,
    "round_delay": 60,
    "dead_device_minutes": 30,
    "grace_minutes": 0,
    "grace_probe_rate": 0.02,
    "ban_recheck_minutes": 120,
    "status_poll_seconds": 60,
    "alert_below_solves": 5000,
    "discord_webhook_url": "",
}

# Three tries per field, then give up. A closed stdin raises EOFError on every
# read, and an unbounded loop there is an invisible spin in a process the
# operator has no way to interrupt.
WIZARD_TRIES = 3

# The four values worth asking for, in the order they are asked. The two
# credentials have no default and no sensible guess; the other two do, so Enter
# accepts one. Labels, hints, and validation all come from `settings.FIELDS` —
# one copy, because a rule enforced in the editor and not here is a rule the
# first run walks straight past.
PROMPTS = (
    ("api_key", None),
    ("farm_token", None),
    ("threads", DEFAULTS["threads"]),
    ("discord_webhook_url", DEFAULTS["discord_webhook_url"]),
)

# Distinguishes "gave up" from an answer that is legitimately empty or zero.
ABANDON = object()


def build_config(answers: dict) -> dict:
    """The exact document the wizard writes. Separated from the prompting so
    the key set can be asserted without driving a fake terminal.

    Answers land on top of DEFAULTS, so a key the wizard never asks about still
    reaches the file with its documented value.
    """
    return {**DEFAULTS, **answers}


def _ask_once(ask, echo, field, default):
    """One field, up to WIZARD_TRIES attempts. ABANDON means give up."""
    echo(f"  {field.hint}")
    if default is not None:
        echo(f"  Press Enter for {default if default != '' else 'none'}.")
    for _ in range(WIZARD_TRIES):
        try:
            answer = ask(f"  {field.label}: ")
        except (EOFError, KeyboardInterrupt):
            return ABANDON
        if not answer.strip() and default is not None:
            return default
        try:
            return settings.parse(field, answer)
        except ValueError as e:
            echo(f"  {e}")
    return ABANDON


def run_wizard(dest, ask=input, echo=Output.info) -> bool:
    """Ask for the four settings and write a complete config. False = give up.

    Nothing is written until every answer is in hand, so an abandoned wizard
    leaves no half-written config for the next launch to trip over.
    """
    echo("")
    echo(f"First run. Setting up {dest}")
    echo("Four values are needed. Nothing is sent anywhere while you type.")
    echo("The last two have defaults — press Enter to accept them.")

    answers = {}
    for key, default in PROMPTS:
        answer = _ask_once(ask, echo, settings.field(key), default)
        if answer is ABANDON:
            echo("Setup abandoned. Nothing was written. Run again when ready.")
            return False
        answers[key] = answer

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build_config(answers), indent=4) + "\n",
                    encoding="utf-8")
    echo(f"Saved to {dest}")
    echo("Press S at the menu on any launch to change these again.")
    return True


def _new_session() -> requests.Session:
    """The update channel's own session, ignoring ambient proxy and CA-bundle
    environment variables the way ``src/farmsync.py`` does.

    Higher stakes here than there. This is the only path in the tool that
    downloads a file and then executes it, and the asset *and* its
    SHA256SUMS.txt travel over this one session — so anything that can
    substitute the channel substitutes both, and the SHA-256 check anchors
    nothing. ``HTTPS_PROXY`` and ``REQUESTS_CA_BUNDLE`` are exactly that
    ability, handed over by whatever set an environment variable.
    """
    session = requests.Session()
    session.trust_env = False
    return session


def maybe_update(argv, session=None, ask=input, current=__version__) -> bool:
    """Offer the newer release. False means "we relaunched — stop here".

    Source runs skip this entirely: there is no .exe to replace, and a
    developer's git checkout is not something an updater should be touching.

    The whole body is wrapped, because nothing about updating is worth losing
    a run over. A GitHub outage, a rate limit, a half-published release, a
    disk that fills mid-download — all of them end with the operator running
    the version they already have, which is the correct answer every time.
    """
    if not paths.frozen() or "--no-update" in argv:
        return True
    # Only a session this function built is a session this function may close.
    # A caller-supplied one — every test, and any future caller — outlives the
    # call.
    own_session = None
    if session is None:
        session = own_session = _new_session()
    try:
        return _offer_update(argv, session, ask, current)
    except update.RollbackFailed as e:
        Output.error(str(e))
        return True
    except Exception as e:
        Output.warn(f"update check skipped: {e}")
        return True
    finally:
        if own_session is not None:
            own_session.close()


def _offer_update(argv, session, ask, current) -> bool:
    found = update.check(session, current)
    if not found:
        return True

    here = paths.exe_dir()
    Output.info(f"Current version: {current}")
    Output.info(f"Latest version:  {found['version']}")

    if not update.writable(here):
        Output.warn("Cannot update in place — this folder is read-only.")
        Output.warn(f"Move FarmsyncSolver.exe out of Program Files to update. "
                    f"Running {current}.")
        return True

    try:
        answer = ask("  A new version is available. Update now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return True
    if answer and not answer.startswith("y"):
        Output.info(f"Staying on {current}.")
        return True

    Output.info(f"downloading {update.ASSET_NAME} {found['version']}...")
    destination = paths.updates_dir() / update.ASSET_NAME
    update.download(session, found["exe_url"], destination)

    wanted = update.expected_sha(update.fetch_text(session, found["sums_url"]))
    actual = update.sha256_of(destination)
    if not wanted or wanted != actual:
        destination.unlink(missing_ok=True)
        Output.error("Download failed its checksum. Nothing was installed.")
        Output.error(f"Running {current}. Try again later.")
        return True

    # Resolved, not raw: `apply` names the backup after this path, and `sweep`
    # scans paths.exe_dir(), which resolves. Under an 8.3 short path, a subst
    # drive, or a symlink the two disagree and the backup is written where the
    # sweep never looks — left behind forever, with nothing saying so.
    running = Path(sys.executable).resolve()
    update.apply(destination, running)

    # `--no-update` ahead of the forwarded arguments, because the process being
    # started is by construction the newest version there is. Without it, a
    # release whose tag disagrees with the __version__ compiled into its asset
    # updates, relaunches, finds itself "out of date" again, and never reaches
    # any work at all. CI's tag check guards the producer; this guards the
    # consumer, which is the side that cannot be fixed after the fact.
    try:
        update.relaunch(running, ["--no-update", *argv[1:]])
    except Exception as e:
        # The .exe on disk is already the new one. Carrying on would run this
        # process's old in-memory code against it, so say what is true and stop.
        Output.warn(f"could not restart automatically: {e}")
        Output.info(f"update installed — restart to use {found['version']}.")
        return False
    Output.info(f"updated to {found['version']} — restarting")
    return False


def prepare(argv, ask=input) -> bool:
    """Make the tool runnable, or return False to stop.

    Ordered so the cheapest answer wins: an existing config short-circuits
    the wizard, and the wizard is the only other way a config comes into
    existence.
    """
    # First, always: the backup left by the update that produced this process.
    # The name comes from sys.executable so a renamed .exe still cleans up
    # after itself; from source exe_dir() is None and the sweep is a no-op.
    # Resolved before the name is taken, because .resolve() rewrites the final
    # component too — under a symlinked .exe or an 8.3 short path the backup
    # `apply` wrote and the name swept for would otherwise be different strings.
    update.sweep(paths.exe_dir(), Path(sys.executable).resolve().name)
    if not maybe_update(argv, ask=ask):
        return False

    config = paths.config_file()
    if not config.exists() and not run_wizard(config, ask=ask):
        return False

    # Last, so it sees whatever the wizard or the migration just wrote — and so
    # the operator gets one chance to fix a value before any credit is spent.
    return settings.screen(config, argv, ask=ask)


def launch(argv) -> None:
    """The whole startup sequence. Both entry points are one call to this.

    ``src.main`` is imported *inside* the function, not at module scope: it
    pulls in ``util`` and ``solver``, both of which read the config the moment
    they are imported, so the import cannot happen until ``prepare`` has put a
    config on disk.
    """
    if not prepare(argv):
        return
    from .main import main
    main()
