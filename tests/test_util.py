from src.util import Util


def test_config_is_the_faked_one_not_real_credentials():
    """Guards the conftest shim: a regression here means tests read live secrets."""
    assert Util.config()["api_key"] == "TEST_API_KEY"
    assert Util.config()["farm_token"] == "TEST_FARM_TOKEN"


def test_config_returns_the_same_dict_every_call():
    """Documented behavior: parsed once at import, so mutation is global."""
    assert Util.config() is Util.config()


def test_short_returns_string_untouched_at_or_below_limit():
    assert Util.short("abc", 18) == "abc"
    assert Util.short("x" * 18, 18) == "x" * 18


def test_short_truncates_above_limit():
    assert Util.short("x" * 19, 18) == "x" * 17 + "..."


def test_short_output_can_exceed_the_requested_limit():
    """CHARACTERIZATION — current behavior, not necessarily desired behavior.

    ``s[:n-1] + "..."`` yields n+2 characters, so short(s, 18) can return 20.
    That happens to fit the 20-wide user column in Output.line, which is why it
    has never surfaced as a bug. Locked in so a future change to `short` is a
    deliberate decision rather than an accident.
    """
    assert len(Util.short("x" * 50, 18)) == 20
