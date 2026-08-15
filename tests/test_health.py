"""The four terminal classes and the circuit breaker.

`src/health.py` reads no config and calls no clock — the probe sleep arrives as
an argument — so every boundary here is arithmetic rather than waiting, exactly
like `test_credit.py`.
"""

import threading

import pytest

from src import health as health_mod
from src.health import FAILURE_THRESHOLD, SolverHealth, classify, is_service_shaped


# --------------------------------------------------------------------------
# Classification — one wire string, exactly one class
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message,expected", [
    ("ACCOUNT_BANNED", "account"),
    ("COOKIE_DEAD", "account"),
    ("cookie dead", "account"),
    ("account moderated", "account"),
    ("insufficient_balance", "credit"),
    ("SERVICE_PAUSED", "service"),
    ("invalid_api_key", "global"),
    ("key_disabled", "global"),
    ("key_expired", "global"),
])
def test_each_wire_string_lands_in_its_own_class(message, expected):
    """Wire formats are uppercase; matching stays case-insensitive."""
    assert classify(RuntimeError(message)) == expected


def test_a_transient_error_is_not_terminal_at_all():
    """CLASSIFICATION_ERROR recovers 63% of the time — it must keep its retries."""
    assert classify(RuntimeError("CLASSIFICATION_ERROR")) is None


def test_classification_reads_plain_strings_too():
    assert classify("ACCOUNT_BANNED") == "account"


def test_a_marker_past_the_truncation_point_is_missed():
    """Matching runs against the same 120 characters the log line shows.

    Pinned so the limit is a deliberate choice rather than a surprise if a new
    error code ever arrives buried in a long payload.
    """
    assert classify(RuntimeError("x" * 130 + " invalid_api_key")) is None


# --------------------------------------------------------------------------
# Service-shaped — the failures that mean dibycap is sick, not the account
# --------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    RuntimeError("SERVICE_PAUSED"),
    RuntimeError("timeout"),
    TimeoutError("read timed out"),
    ConnectionError("connection reset by peer"),
    RuntimeError("dibycap unavailable: HTTP 503"),
    RuntimeError("dibycap unavailable: HTTP 500"),
])
def test_service_shaped_failures_are_recognised(error):
    assert is_service_shaped(error) is True


@pytest.mark.parametrize("error", [
    RuntimeError("ACCOUNT_BANNED"),
    RuntimeError("insufficient_balance"),
    RuntimeError("CLASSIFICATION_ERROR"),
    RuntimeError("dibycap unavailable: HTTP 404"),
])
def test_account_and_wallet_failures_are_not_service_shaped(error):
    assert is_service_shaped(error) is False


def test_a_connection_error_is_recognised_by_its_type_not_its_text():
    """curl_cffi raises these with an empty or opaque message."""
    assert is_service_shaped(ConnectionError()) is True


class DNSError(ConnectionError):
    """The shape curl_cffi actually raises when a host will not resolve."""


def test_a_failure_is_recognised_through_its_base_classes():
    """Live-verified against an unreachable host: curl_cffi raises
    `DNSError`, whose own name says nothing and whose message is the generic
    'Failed to perform, curl: (6) ...'. Matching the leaf class alone let a real
    outage read as twelve ordinary account failures and never opened the breaker.
    """
    error = DNSError("Failed to perform, curl: (6) Could not resolve host: api")
    assert is_service_shaped(error) is True
    assert classify(error) is None  # still retryable — DNS can come back


def test_a_service_shaped_failure_is_still_retryable():
    """A timeout feeds the breaker but must not short-circuit the retry layer."""
    error = TimeoutError("read timed out")
    assert is_service_shaped(error) is True
    assert classify(error) is None


# --------------------------------------------------------------------------
# The breaker
# --------------------------------------------------------------------------

def test_the_breaker_opens_only_on_the_fifth_consecutive_service_failure():
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD - 1):
        health.record_failure(RuntimeError("SERVICE_PAUSED"))
        assert health.open is False
    health.record_failure(RuntimeError("SERVICE_PAUSED"))
    assert health.open is True
    assert health.stopped() is True


def test_a_success_resets_the_run_of_failures():
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD - 1):
        health.record_failure(ConnectionError("reset"))
    health.record_success()
    for _ in range(FAILURE_THRESHOLD - 1):
        health.record_failure(ConnectionError("reset"))
    assert health.open is False


def test_account_shaped_failures_never_open_the_breaker():
    """An empty farm of dead cookies is not a sick service."""
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD * 4):
        health.record_failure(RuntimeError("ACCOUNT_BANNED"))
    assert health.open is False
    assert health.stopped() is False


def test_an_empty_wallet_never_opens_the_breaker():
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD * 4):
        health.record_failure(RuntimeError("insufficient_balance"))
    assert health.open is False
    assert health.out_of_credit is True


def test_concurrent_failures_are_counted_exactly_once_each():
    """65 workers report into one counter; the threshold must not be racy."""
    health = SolverHealth(threshold=100)
    workers = [threading.Thread(
        target=lambda: [health.record_failure(ConnectionError("reset"))
                        for _ in range(50)]) for _ in range(8)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert health.consecutive == 400


# --------------------------------------------------------------------------
# The two conditions that stop the pool without the breaker
# --------------------------------------------------------------------------

def test_a_dead_key_halts_on_first_sight():
    """One identical error per account is the failure this replaces — one is enough."""
    health = SolverHealth()
    health.record_failure(RuntimeError("invalid_api_key"))
    assert health.halt_reason == "invalid_api_key"
    assert health.stopped() is True


def test_the_first_halt_reason_wins():
    """Workers already in flight report the same thing; the operator reads one."""
    health = SolverHealth()
    health.record_failure(RuntimeError("invalid_api_key"))
    health.record_failure(RuntimeError("key_expired"))
    assert health.halt_reason == "invalid_api_key"


def test_an_empty_wallet_stops_the_pool_but_is_not_a_halt():
    """An empty wallet parks and waits for a top-up; only the operator's key is
    unrecoverable."""
    health = SolverHealth()
    health.record_failure(RuntimeError("insufficient_balance"))
    assert health.stopped() is True
    assert health.halt_reason is None


def test_clearing_reopens_the_pool_after_recovery():
    health = SolverHealth()
    health.record_failure(RuntimeError("insufficient_balance"))
    for _ in range(FAILURE_THRESHOLD):
        health.record_failure(RuntimeError("SERVICE_PAUSED"))
    health.clear()
    assert health.stopped() is False
    assert health.consecutive == 0


def test_a_dead_key_never_reaches_the_breaker():
    """A rejected key halts on first sight, so the five-failure threshold is
    unreachable."""
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD * 4):
        health.record_failure(RuntimeError("invalid_api_key"))
    assert health.open is False
    assert health.consecutive == 0


def test_a_successful_probe_closes_the_breaker():
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD):
        health.record_failure(RuntimeError("SERVICE_PAUSED"))
    assert health.open is True

    result = health.recover(lambda: {"estimated_solves": 10},
                            sleep_fn=lambda _: None)
    assert result == {"estimated_solves": 10}
    assert health.open is False
    assert health.stopped() is False


def test_recovery_passes_its_backoff_through_to_the_prober():
    """A caller shortening the first probe must not be silently ignored."""
    slept = []
    health = SolverHealth()
    health.recover(lambda: {"ok": True}, sleep_fn=slept.append, first_delay=1)
    assert slept == [1]


def test_clearing_never_forgives_a_dead_key():
    """Nothing recovers a bad key without the operator editing config."""
    health = SolverHealth()
    health.record_failure(RuntimeError("invalid_api_key"))
    health.clear()
    assert health.stopped() is True


# --------------------------------------------------------------------------
# The recovery prober
# --------------------------------------------------------------------------

def test_the_prober_backs_off_and_returns_the_first_good_status():
    slept = []
    answers = [RuntimeError("SERVICE_PAUSED")] * 3 + [{"estimated_solves": 10}]

    def status_fn():
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result = health_mod.wait_for_recovery(status_fn, sleep_fn=slept.append)
    assert result == {"estimated_solves": 10}
    assert slept == [10, 20, 40, 80]


def test_the_prober_caps_its_backoff():
    """Five minutes between probes, never an hour."""
    slept = []
    tries = [0]

    def status_fn():
        tries[0] += 1
        if tries[0] < 10:
            raise ConnectionError("down")
        return {"ok": True}

    health_mod.wait_for_recovery(status_fn, sleep_fn=slept.append)
    assert max(slept) == health_mod.PROBE_DELAY_CAP
    assert slept[-1] == health_mod.PROBE_DELAY_CAP


def test_the_prober_reports_each_failed_probe_without_dying():
    seen = []
    answers = [ConnectionError("down"), {"ok": True}]

    def status_fn():
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    health_mod.wait_for_recovery(status_fn, sleep_fn=lambda _: None,
                                 on_error=seen.append)
    assert len(seen) == 1


# --------------------------------------------------------------------------
# Proof of life while the prober waits
# --------------------------------------------------------------------------
#
# A five-minute wait with nothing on screen is indistinguishable from a hung
# process — the outage that prompted this printed 65 failures and then went
# silent, and the operator read it as a crash. `tick` chops the wait so the
# caller can say "still alive" on the way through.

def test_a_long_wait_is_chopped_into_ticks_the_caller_can_announce():
    slept, announced = [], []
    answers = [ConnectionError("down"), {"ok": True}]

    def status_fn():
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    health_mod.wait_for_recovery(status_fn, sleep_fn=slept.append,
                                 on_wait=announced.append,
                                 first_delay=20, tick=5)
    # One 20s wait, then the failed probe doubles it to 40s. Every slice is
    # announced with the time still to go — a countdown, not a repeated
    # constant, which is the difference between proof of life and noise.
    assert slept == [5] * (4 + 8)
    assert announced == [20, 15, 10, 5] + [40, 35, 30, 25, 20, 15, 10, 5]


def test_ticking_does_not_change_the_backoff_ladder():
    """The slices must still add up to 10s, 20s, 40s — the ladder is the contract.

    Asserted against a *ticked* run on purpose: pinning the ladder only on the
    unticked path would let production drift away from the tests silently,
    since production is the side that passes a tick.
    """
    slept = []
    tries = [0]

    def status_fn():
        tries[0] += 1
        if tries[0] < 4:
            raise ConnectionError("down")
        return {"ok": True}

    def sleep_fn(seconds):
        slept.append(seconds)

    health_mod.wait_for_recovery(status_fn, sleep_fn=sleep_fn, tick=5)
    assert sum(slept) == 10 + 20 + 40 + 80
    assert set(slept) == {5}


def test_a_wait_shorter_than_one_tick_is_not_chopped():
    slept, announced = [], []
    health_mod.wait_for_recovery(lambda: {"ok": True}, sleep_fn=slept.append,
                                 on_wait=announced.append,
                                 first_delay=3, tick=5)
    assert slept == [3]
    assert announced == [3]


def test_the_prober_still_waits_whole_delays_when_no_tick_is_asked_for():
    """`tick=None` is the default, and the tests above it depend on that."""
    slept = []
    health_mod.wait_for_recovery(lambda: {"ok": True}, sleep_fn=slept.append)
    assert slept == [health_mod.PROBE_DELAY_SECONDS]


# --------------------------------------------------------------------------
# A recovery the probe cannot vouch for
# --------------------------------------------------------------------------
#
# `recover` probes `POST /balance`; solving happens on `POST /createTask`.
# Measured live: balance answered on the first 10-second probe throughout an
# outage that failed every solve, so the breaker closed, the pass re-walked
# farmsync (113s), failed again, and paused again — from 10 seconds, every
# time. The ladder only bites if it survives the pause.

def test_the_pause_lengthens_when_a_recovery_does_not_stick():
    health = SolverHealth()
    slept = []

    for expected in (10, 20, 40):
        health.open = True
        health.recover(lambda: {"ok": True}, sleep_fn=slept.append)
        assert slept[-1] == expected
        # What the probe promised did not hold: solving still fails, so the
        # canary trips the breaker straight back open.
        health.trip()


def test_a_dispatch_that_works_puts_the_pause_back_to_ten_seconds():
    """Only evidence from the solve path may shorten the wait."""
    health = SolverHealth()
    health.open = True
    health.recover(lambda: {"ok": True}, sleep_fn=lambda _: None)
    assert health.recovery_delay > health_mod.PROBE_DELAY_SECONDS

    health.record_success()
    assert health.recovery_delay == health_mod.PROBE_DELAY_SECONDS


def test_the_lengthening_pause_stops_at_the_ceiling():
    health = SolverHealth()
    for _ in range(20):
        health.recover(lambda: {"ok": True}, sleep_fn=lambda _: None)
    assert health.recovery_delay == health_mod.PROBE_DELAY_CAP


def test_clearing_does_not_shorten_the_next_pause():
    """`clear` runs on every recovery, and a probe has proved nothing."""
    health = SolverHealth()
    health.recover(lambda: {"ok": True}, sleep_fn=lambda _: None)
    lengthened = health.recovery_delay
    health.clear()
    assert health.recovery_delay == lengthened


def test_a_caller_naming_its_own_first_delay_is_still_obeyed():
    slept = []
    health = SolverHealth()
    health.recovery_delay = 80
    health.recover(lambda: {"ok": True}, sleep_fn=slept.append, first_delay=1)
    assert slept == [1]


def test_tripping_opens_the_breaker_without_five_failures():
    """After `clear` the count is zero, so three canary failures never reach
    the threshold on their own — and the pass they exist to stop would run."""
    health = SolverHealth()
    assert health.stopped() is False
    health.trip()
    assert health.open is True
    assert health.stopped() is True
