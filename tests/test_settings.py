"""The startup menu and the settings editor.

`src/settings.py` reads no config and calls no clock, the same rule
`dispatch.py`, `credit.py`, and `health.py` follow — the clock, the sleep, and
the key reader all arrive as arguments. So the ten-second countdown is asserted
by arithmetic rather than by waiting ten seconds, and every menu path is driven
by a scripted list of keypresses.

The editor is tested against a real file under tmp_path rather than a mocked
one, for the same reason `test_state.py` builds a real database: the bug worth
catching is a save that drops the keys it was not asked to touch, and a mock
would happily accept it.
"""

import json

import pytest

from src import settings


def write_config(tmp_path, **overrides):
    config = {
        "api_key": "sk-abcdef123456",
        "farm_token": "eyJhbGciOiJIUzI1NiJ9",
        "threads": 45,
        "round_delay": 60,
        "grace_minutes": 0,
        "discord_webhook_url": "",
    }
    config.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=4), encoding="utf-8")
    return path


# --- masking ---------------------------------------------------------------

def test_mask_keeps_only_the_first_few_characters():
    """Enough to recognise which key it is, never enough to use it."""
    masked = settings.mask("sk-abcdef123456")
    assert masked.startswith("sk-a")
    assert "bcdef123456" not in masked


def test_mask_does_not_leak_the_length():
    """A fixed run of stars, so a short secret and a long one look alike."""
    assert len(settings.mask("sk-abcdef123456")) == len(settings.mask("sk-ab" + "x" * 90))


def test_a_short_secret_is_masked_entirely():
    """Keeping four characters of a six-character value would show most of it."""
    assert "abc" not in settings.mask("abcdef")


def test_an_empty_webhook_reads_as_off():
    """The alerter treats "" as disabled, so the screen must say so in words."""
    field = settings.field("discord_webhook_url")
    assert settings.show(field, {"discord_webhook_url": ""}) == settings.OFF


def test_a_set_webhook_is_masked_like_any_other_secret():
    """Anyone holding the URL can post to the operator's channel."""
    field = settings.field("discord_webhook_url")
    shown = settings.show(field, {"discord_webhook_url": "https://discord.com/api/webhooks/1/xyz"})
    assert "xyz" not in shown and shown != settings.OFF


def test_a_number_is_shown_as_itself():
    assert settings.show(settings.field("threads"), {"threads": 45}) == "45"


def test_a_missing_key_shows_as_unset_rather_than_raising():
    """An older config predates a key. The screen must still draw."""
    assert settings.show(settings.field("threads"), {}) == settings.UNSET


# --- credential check ------------------------------------------------------

def test_credentials_are_ok_when_both_are_real():
    assert settings.credentials_ok({"api_key": "real", "farm_token": "real"})


@pytest.mark.parametrize("config", [
    {"farm_token": "real"},
    {"api_key": "", "farm_token": "real"},
    {"api_key": "REPLACE_WITH_DIBYCAP_API_KEY", "farm_token": "real"},
    {"api_key": "real", "farm_token": "REPLACE_WITH_FARMSYNC_BEARER_TOKEN"},
])
def test_credentials_are_not_ok(config):
    """The same three faults main.py's startup guard rejects, asked one screen
    earlier — because this is the screen that can fix them."""
    assert not settings.credentials_ok(config)


# --- parsing ---------------------------------------------------------------

def test_an_empty_credential_is_refused():
    with pytest.raises(ValueError):
        settings.parse(settings.field("api_key"), "")


def test_the_placeholder_text_is_refused():
    """Pasting REPLACE_WITH_DIBYCAP_API_KEY out of the example is the obvious
    mistake, and the startup guard would reject it one screen later."""
    with pytest.raises(ValueError):
        settings.parse(settings.field("api_key"), "REPLACE_WITH_DIBYCAP_API_KEY")


def test_threads_must_be_a_number():
    with pytest.raises(ValueError):
        settings.parse(settings.field("threads"), "lots")


@pytest.mark.parametrize("answer", ["0", "-5", str(settings.MAX_THREADS + 1)])
def test_threads_outside_the_allowed_range_is_refused(answer):
    with pytest.raises(ValueError):
        settings.parse(settings.field("threads"), answer)


def test_threads_is_stored_as_a_number_not_a_string():
    """`min(config["threads"], max_concurrent)` in main.py compares it against
    an int. A string there sizes the pool by a comparison that cannot fail."""
    assert settings.parse(settings.field("threads"), " 45 ") == 45


def test_an_empty_webhook_is_accepted_and_means_off():
    assert settings.parse(settings.field("discord_webhook_url"), "") == ""


def test_a_webhook_that_is_not_https_is_refused():
    with pytest.raises(ValueError):
        settings.parse(settings.field("discord_webhook_url"), "discord.com/api/webhooks/1/x")


def test_a_real_webhook_is_accepted():
    url = "https://discord.com/api/webhooks/1/xyz"
    assert settings.parse(settings.field("discord_webhook_url"), url) == url


# --- saving ----------------------------------------------------------------

def test_saving_one_field_keeps_every_other_key(tmp_path):
    """The editor offers four keys; the file holds eleven. A save that wrote
    only what it was shown would silently delete the other seven, and the
    defaults that replaced them would look like the operator's own choices."""
    path = write_config(tmp_path)

    settings.save(path, "threads", 20)

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["threads"] == 20
    assert after["round_delay"] == 60
    assert after["grace_minutes"] == 0
    assert after["api_key"] == "sk-abcdef123456"


def test_saving_writes_a_config_that_loads_back(tmp_path):
    path = write_config(tmp_path)
    settings.save(path, "api_key", "new-key")
    assert settings.load(path)["api_key"] == "new-key"


def test_loading_a_broken_file_gives_an_empty_config(tmp_path):
    """A hand-edited file with a trailing comma must reach the editor, which is
    the one screen that can repair it, rather than crash before it draws."""
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    assert settings.load(path) == {}


# --- the countdown ---------------------------------------------------------

def test_a_keypress_ends_the_wait_immediately():
    keys = iter([None, None, "s"])
    slept = []
    assert settings.wait_for_key(
        deadline=100, poll=lambda: next(keys), clock=lambda: 0,
        sleep=slept.append) == "s"
    assert len(slept) == 2


def test_the_wait_gives_up_at_the_deadline():
    """None is the whole reason the timer exists: nobody pressed anything."""
    ticking = iter([0.0, 4.0, 8.0, 12.0])
    assert settings.wait_for_key(
        deadline=10.0, poll=lambda: None, clock=lambda: next(ticking),
        sleep=lambda _: None) is None


def test_the_countdown_is_announced_once_per_whole_second():
    """65 workers repainting a line is `output.py`'s problem; here the problem
    is a 50ms poll printing the same "9" twenty times."""
    ticking = iter([0.0, 0.2, 0.4, 1.2, 1.4, 2.5, 99.0])
    shown = []
    settings.wait_for_key(
        deadline=10.0, poll=lambda: None, clock=lambda: next(ticking),
        sleep=lambda _: None, tick=shown.append)
    assert shown == [10, 9, 8]


# --- the menu --------------------------------------------------------------

def menu_args(**kw):
    """Defaults every menu test overrides one piece of."""
    args = dict(poll=lambda: None, clock=lambda: 0.0, sleep=lambda _: None,
                ask=lambda _: "", echo=lambda _: None, timeout=10.0)
    args.update(kw)
    return args


def test_the_menu_starts_on_its_own_when_nobody_presses_anything(tmp_path):
    """The operator double-clicked the .exe and walked away. Waiting forever on
    a menu is the failure this timer exists to prevent."""
    ticking = iter([0.0, 11.0])
    path = write_config(tmp_path)
    assert settings.choose(path, settings.load(path),
                           **menu_args(clock=lambda: next(ticking))) == "start"


@pytest.mark.parametrize("key", ["\r", "\n"])
def test_enter_starts_the_tool(tmp_path, key):
    path = write_config(tmp_path)
    assert settings.choose(path, settings.load(path),
                           **menu_args(poll=lambda: key)) == "start"


@pytest.mark.parametrize("key", ["s", "S"])
def test_s_opens_the_settings(tmp_path, key):
    path = write_config(tmp_path)
    assert settings.choose(path, settings.load(path),
                           **menu_args(poll=lambda: key)) == "settings"


@pytest.mark.parametrize("key", ["q", "\x03", "\x1b"])
def test_quitting(tmp_path, key):
    """Ctrl-C and Escape arrive as characters here, not as exceptions: the key
    reader polls the console rather than blocking in input()."""
    path = write_config(tmp_path)
    assert settings.choose(path, settings.load(path),
                           **menu_args(poll=lambda: key)) == "quit"


def test_a_stray_key_does_not_restart_the_countdown(tmp_path):
    """Resting a hand on the keyboard would otherwise hold the tool on the menu
    forever, one ignored keypress at a time."""
    path = write_config(tmp_path)
    keys = iter(["x", "z"] + [None] * 20)
    ticking = iter([0.0] + [float(n) for n in range(1, 25)])
    assert settings.choose(path, settings.load(path),
                           **menu_args(poll=lambda: next(keys),
                                       clock=lambda: next(ticking))) == "start"


def test_bad_credentials_stop_the_countdown_and_wait(tmp_path):
    """Starting the tool on a config the startup guard will reject just prints
    an error and exits. There is nothing to count down to."""
    path = write_config(tmp_path, api_key="REPLACE_WITH_DIBYCAP_API_KEY")
    polled = []

    def poll():
        polled.append(1)
        return None

    said = []
    assert settings.choose(path, settings.load(path),
                           **menu_args(poll=poll, ask=lambda _: "s",
                                       echo=said.append)) == "settings"
    assert not polled, "the timer must not run against a config that cannot start"
    assert any("api key" in line.lower() or "token" in line.lower() for line in said)


def test_an_abandoned_menu_stops_rather_than_starting(tmp_path):
    """Closed stdin raises EOFError on every read. Starting anyway would run
    the tool nobody asked to run."""
    path = write_config(tmp_path, api_key="")

    def ask(_):
        raise EOFError

    assert settings.choose(path, settings.load(path),
                           **menu_args(ask=ask)) == "quit"


def test_the_menu_prints_the_settings_path(tmp_path):
    """The whole point of the request: an operator who typed the wrong thread
    count must be able to find the file without being told where it is."""
    path = write_config(tmp_path)
    said = []
    settings.choose(path, settings.load(path),
                    **menu_args(poll=lambda: "\r", echo=said.append))
    assert any(str(path) in line for line in said)


# --- the editor ------------------------------------------------------------

def test_editing_threads_writes_the_new_value(tmp_path):
    path = write_config(tmp_path)
    answers = iter(["3", "20", ""])

    settings.edit(path, ask=lambda _: next(answers), echo=lambda _: None)

    assert settings.load(path)["threads"] == 20


def test_pressing_enter_at_a_field_keeps_the_current_value(tmp_path):
    """The way to look at a masked key without changing it."""
    path = write_config(tmp_path)
    answers = iter(["1", "", ""])

    settings.edit(path, ask=lambda _: next(answers), echo=lambda _: None)

    assert settings.load(path)["api_key"] == "sk-abcdef123456"


def test_a_refused_value_writes_nothing(tmp_path):
    path = write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    answers = iter(["3", "lots", ""])
    said = []

    settings.edit(path, ask=lambda _: next(answers), echo=said.append)

    assert path.read_text(encoding="utf-8") == before
    assert any("number" in line.lower() for line in said)


def test_the_editor_ignores_a_choice_that_is_not_on_the_list(tmp_path):
    path = write_config(tmp_path)
    answers = iter(["9", "banana", ""])

    settings.edit(path, ask=lambda _: next(answers), echo=lambda _: None)

    assert settings.load(path)["threads"] == 45


def test_the_editor_survives_a_closed_stdin(tmp_path):
    """Same fault the wizard guards: an unbounded loop over a stdin that only
    ever raises is an invisible spin in a process nobody can interrupt."""
    def ask(_):
        raise EOFError

    settings.edit(write_config(tmp_path), ask=ask, echo=lambda _: None)


def test_the_editor_shows_the_path_it_writes(tmp_path):
    path = write_config(tmp_path)
    said = []
    settings.edit(path, ask=lambda _: "", echo=said.append)
    assert any(str(path) in line for line in said)


def test_the_editor_never_prints_a_secret_in_full(tmp_path):
    path = write_config(tmp_path)
    said = []
    settings.edit(path, ask=lambda _: "", echo=said.append)
    assert not any("sk-abcdef123456" in line for line in said)


# --- the whole screen ------------------------------------------------------

def test_a_redirected_run_never_draws_the_menu(tmp_path):
    """`python -m src > log.txt` is an unattended run. A menu there is a hang
    with no visible prompt, which is the worst shape this bug can take."""
    path = write_config(tmp_path)

    def ask(_):
        raise AssertionError("must not prompt when stdout is not a tty")

    assert settings.screen(path, [], ask=ask, tty=False)


def test_the_grace_report_skips_the_menu(tmp_path):
    """A read-only report the operator asked for by name. Do not interrupt it."""
    path = write_config(tmp_path)

    def ask(_):
        raise AssertionError("must not prompt for --grace-report")

    assert settings.screen(path, ["src", "--grace-report"], ask=ask, tty=True)


def test_the_settings_flag_opens_the_editor_without_the_menu(tmp_path):
    path = write_config(tmp_path)
    answers = iter(["3", "20", "", "\r"])

    started = settings.screen(path, ["exe", "--settings"],
                              ask=lambda _: next(answers), echo=lambda _: None,
                              poll=lambda: "\r", clock=lambda: 0.0,
                              sleep=lambda _: None, tty=True)

    assert settings.load(path)["threads"] == 20
    assert started


def test_the_menu_comes_back_after_the_editor(tmp_path):
    """Changing one setting must not force a restart to change the next."""
    path = write_config(tmp_path)
    keys = iter(["s", "\r"])
    answers = iter(["3", "20", ""])

    assert settings.screen(path, [], ask=lambda _: next(answers),
                           echo=lambda _: None, poll=lambda: next(keys),
                           clock=lambda: 0.0, sleep=lambda _: None, tty=True)
    assert settings.load(path)["threads"] == 20


def test_quitting_the_menu_stops_the_tool(tmp_path):
    path = write_config(tmp_path)
    assert not settings.screen(path, [], ask=lambda _: "", echo=lambda _: None,
                               poll=lambda: "q", clock=lambda: 0.0,
                               sleep=lambda _: None, tty=True)
