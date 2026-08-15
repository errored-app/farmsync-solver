"""Credit readout, depletion ETA, Discord alerting, and the parked state.

`src/credit.py` reads no config at import and takes every clock and every
network call as an argument, so these tests are plain arithmetic against real
objects — no patched clock, no mocked webhook.
"""

import pytest

from conftest import FakeResponse, FakeSession
from src.credit import (CreditAlerter, Depletion, credit_line, has_credit,
                        wait_for_top_up)

HOUR = 3600.0
WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def status(solves, balance=None, active=0, max_concurrent=65):
    return {"estimated_solves": solves,
            "balance": balance if balance is not None else (solves or 0) * 0.0015,
            "active": active, "max_concurrent": max_concurrent, "price_per_1k": 1.5}


# --- Depletion ETA ------------------------------------------------------


def test_no_eta_from_a_single_sample():
    d = Depletion()
    d.sample(5300, 0.0)
    assert d.rate_per_hour() is None
    assert d.text() is None


def test_no_eta_until_five_minutes_of_history_exist():
    """A cold start must not print a wild number off two samples 60s apart."""
    d = Depletion()
    d.sample(5300, 0.0)
    d.sample(5290, 60.0)
    assert d.text() is None

    d.sample(5250, 301.0)
    assert d.text() is not None


def test_eta_from_a_steady_burn_rate():
    d = Depletion()
    d.sample(5300, 0.0)
    d.sample(4700, HOUR)          # 600 solves in one hour
    assert d.rate_per_hour() == pytest.approx(600.0)
    assert d.text() == "~7h 50m left"


def test_eta_under_an_hour_is_reported_in_minutes():
    d = Depletion()
    d.sample(900, 0.0)
    d.sample(300, HOUR)
    assert d.text() == "~30m left"


def test_eta_over_a_day_is_reported_in_days():
    d = Depletion()
    d.sample(19200, 0.0)
    d.sample(18600, HOUR)
    assert d.text() == "~1d 7h left"


def test_no_eta_while_nothing_is_being_consumed():
    """A flat balance means no depletion, so there is no honest ETA to give."""
    d = Depletion()
    d.sample(5000, 0.0)
    d.sample(5000, HOUR)
    assert d.rate_per_hour() == pytest.approx(0.0)
    assert d.text() is None


def test_a_top_up_does_not_poison_the_rate():
    """Credit going up is a top-up, not negative consumption."""
    d = Depletion()
    d.sample(5300, 0.0)
    d.sample(4700, HOUR)
    d.sample(20000, 2 * HOUR)     # operator topped up
    assert d.rate_per_hour() == pytest.approx(600.0)
    assert d.text() == "~1d 9h left"


def test_the_rate_is_smoothed_rather_than_jumping_to_the_latest_interval():
    d = Depletion()
    d.sample(5000, 0.0)
    d.sample(4000, HOUR)          # 1000/h seeds the average
    d.sample(3000, 2 * HOUR)      # still 1000/h
    assert d.rate_per_hour() == pytest.approx(1000.0)

    d.sample(3000, 3 * HOUR)      # a quiet hour drags it down, but only partly
    assert 0.0 < d.rate_per_hour() < 1000.0


def test_a_repeated_timestamp_is_ignored():
    d = Depletion()
    d.sample(5300, 0.0)
    d.sample(4700, HOUR)
    before = d.rate_per_hour()
    d.sample(4600, HOUR)
    assert d.rate_per_hour() == before


# --- credit line --------------------------------------------------------


def test_credit_line_without_history_shows_just_the_balance():
    assert credit_line(status(1234, 5.6789), Depletion()) == "1,234 solves  $5.68"


def test_credit_line_adds_rate_and_eta_once_measured():
    d = Depletion()
    d.sample(5300, 0.0)
    d.sample(4700, HOUR)
    assert credit_line(status(4700, 7.05), d) == "4,700 solves  $7.05  |  600/h  |  ~7h 50m left"


# --- has_credit ---------------------------------------------------------


@pytest.mark.parametrize("solves,expected", [(1, True), (0, False), (None, False)])
def test_has_credit_reads_estimated_solves(solves, expected):
    assert has_credit(status(solves)) is expected


# --- Discord alerter ----------------------------------------------------


def alerter(url=WEBHOOK, threshold=5000, script=None):
    session = FakeSession(script if script is not None else [FakeResponse({})] * 10)
    return CreditAlerter(url, threshold, session=session), session


def test_disabled_when_no_webhook_is_configured():
    a, session = alerter(url="")
    assert a.enabled is False
    a.check(status(10))
    assert session.calls == []


def test_disabled_while_the_example_placeholder_is_still_in_place():
    """A fresh clone must not POST to REPLACE_WITH_DISCORD_WEBHOOK_URL forever."""
    a, session = alerter(url="REPLACE_WITH_DISCORD_WEBHOOK_URL")
    assert a.enabled is False
    a.check(status(10))
    assert session.calls == []


def test_fires_once_when_credit_crosses_below_the_threshold():
    a, session = alerter()
    assert a.check(status(6000)) is False
    assert a.check(status(4900)) is True
    assert a.check(status(4800)) is False
    assert len(session.calls) == 1


def test_fires_when_the_very_first_poll_is_already_below():
    """A restart while low would otherwise never alert at all."""
    a, session = alerter()
    assert a.check(status(4000)) is True
    assert len(session.calls) == 1


def test_stays_quiet_while_hovering_at_the_boundary():
    a, session = alerter()
    a.check(status(4900))
    for solves in (5100, 4900, 5400, 4800, 5500):
        a.check(status(solves))
    assert len(session.calls) == 1


def test_rearms_only_above_ten_percent_over_the_threshold():
    a, session = alerter()
    a.check(status(4900))
    a.check(status(5500))         # exactly 1.1x — not enough
    assert a.check(status(4900)) is False

    a.check(status(5600))         # clear of the band
    assert a.check(status(4900)) is True
    assert len(session.calls) == 2


def test_a_failing_webhook_never_interrupts_solving():
    a, session = alerter(script=[RuntimeError("discord is down")])
    assert a.check(status(4000)) is False
    assert len(session.calls) == 1


def test_a_rejected_webhook_counts_as_a_failure():
    """Discord answers a typo'd URL with 404, not a connection error.

    Without checking the response status, a wrong webhook would report success
    on every poll forever and the operator would never learn the alert is dead.
    """
    rejected = FakeResponse(None, raise_for_status=RuntimeError("404 Not Found"))
    a, _ = alerter(script=[rejected, FakeResponse({})])
    assert a.check(status(4000)) is False
    assert a.check(status(3900)) is True     # still armed, retried, delivered


def test_a_failing_webhook_leaves_the_alert_armed_to_retry():
    a, _ = alerter(script=[RuntimeError("discord is down"), FakeResponse({})])
    a.check(status(4000))
    assert a.check(status(3900)) is True


def test_the_embed_carries_solves_rate_eta_and_pool():
    d = Depletion()
    d.sample(5300, 0.0)
    d.sample(4700, HOUR)
    a, session = alerter()
    a.check(status(4700, 7.05), depletion=d, pool="425 solvable | 146 dead host | 14 queued")

    _, url, kwargs = session.calls[0]
    assert url == WEBHOOK
    embed = kwargs["json"]["embeds"][0]
    values = {f["name"]: f["value"] for f in embed["fields"]}
    assert values["Solves remaining"] == "4,700"
    assert values["Burn rate"] == "600/h"
    assert values["Runs out in"] == "~7h 50m left"
    assert values["Pool"] == "425 solvable | 146 dead host | 14 queued"


def test_the_embed_omits_fields_that_are_not_measured_yet():
    a, session = alerter()
    a.check(status(4700, 7.05))
    embed = session.calls[0][2]["json"]["embeds"][0]
    names = [f["name"] for f in embed["fields"]]
    assert names == ["Solves remaining"]


def test_parking_alerts_even_though_the_low_credit_alert_already_fired():
    """Credit reaches zero precisely when nobody is watching the terminal."""
    a, session = alerter()
    a.check(status(4000))
    assert a.notify_parked(status(0, 0.0)) is True
    assert len(session.calls) == 2
    assert "parked" in session.calls[1][2]["json"]["embeds"][0]["title"].lower()


def test_parking_is_silent_when_no_webhook_is_configured():
    a, session = alerter(url="")
    assert a.notify_parked(status(0, 0.0)) is False
    assert session.calls == []


# --- park at zero -------------------------------------------------------


def poller(*payloads):
    """A status callable replaying payloads; exceptions are raised."""
    queue = list(payloads)

    def call():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return call


def test_the_wait_polls_until_credit_returns():
    slept = []
    resumed = wait_for_top_up(poller(status(0), status(0), status(500)),
                              poll_seconds=60, sleep_fn=slept.append)
    assert resumed["estimated_solves"] == 500
    assert slept == [60, 60, 60]


def test_a_failing_status_call_does_not_end_the_wait():
    """dibycap being down at zero credit must not exit the daemon."""
    seen = []
    resumed = wait_for_top_up(poller(RuntimeError("service_paused"), status(500)),
                              poll_seconds=5, sleep_fn=lambda _: None,
                              on_error=seen.append)
    assert resumed["estimated_solves"] == 500
    assert [str(e) for e in seen] == ["service_paused"]


def test_the_wait_honours_the_configured_poll_interval():
    slept = []
    wait_for_top_up(poller(status(1)), poll_seconds=15, sleep_fn=slept.append)
    assert slept == [15]
