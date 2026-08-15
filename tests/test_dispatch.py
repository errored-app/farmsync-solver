"""The dispatch filter — the rule that decides an account is not worth solving.

This is where a bug silently stops all work: over-suppress and the tool runs
happily forever while solving nothing, and no error is ever printed. So every
boundary is asserted from both sides.

`now` is injected rather than read from the clock, so each boundary is plain
arithmetic instead of a sleep or a monkeypatched `time.time`.
"""

from src.dispatch import (BACKOFF_CAP_MINUTES, BAN_RECHECK_MINUTES,
                          DEAD_DEVICE_MINUTES, plan_dispatch)

NOW = 1_700_000_000.0  # epoch seconds

# The grace tests pass this explicitly rather than leaning on
# `dispatch.GRACE_MINUTES`. They pin the *rule* — where the boundary falls and
# which side is exclusive — and the rule needs a non-zero window to exercise at
# all. The shipped default is 0 and has its own test below, so tying these to
# the constant would have silently deleted the boundary coverage the day the
# default changed.
WINDOW = 60


def row(**kw):
    """One account's persisted state, as `State.load` returns it."""
    r = {"last_success_at": None, "consecutive_failures": 0,
         "last_failure_at": None, "banned": False, "banned_at": None}
    r.update(kw)
    return r


def banned_now(**kw):
    """A ban too recent to be worth re-checking."""
    return row(banned=True, banned_at=NOW - 60, **kw)


def never_probe():
    return 1.0


def account(id="a1", *, minutes_stale=0.0, active=5, **kw):
    """A solvable-account record as `Farmsync.solvable_accounts` now returns it."""
    rec = {
        "username": id,
        "cookie": f"cookie-{id}",
        "id": id,
        "device_id": 1,
        "device_name": "Device 1",
        "device_last_updated": int((NOW - minutes_stale * 60) * 1000),
        "device_active_accounts": active,
    }
    rec.update(kw)
    return rec


def ids(plan):
    return [a["id"] for a in plan.queued]


# --------------------------------------------------------------------------
# The dead-host rule — both halves must agree before anything is suppressed
# --------------------------------------------------------------------------

def test_an_account_on_a_live_host_is_queued():
    plan = plan_dispatch([account(minutes_stale=0.5)], now=NOW)
    assert ids(plan) == ["a1"]
    assert plan.counts["dead_host"] == 0


def test_a_stale_host_reporting_no_active_accounts_is_suppressed():
    plan = plan_dispatch([account(minutes_stale=600, active=0)], now=NOW)
    assert plan.queued == []
    assert plan.counts["dead_host"] == 1


def test_a_fresh_host_reporting_zero_active_accounts_is_still_queued():
    """The count alone must never suppress — the heartbeat decides.

    Live devices have been seen reading `active_accounts == 0` while
    holding the richest live pools available. Suppressing on the count
    alone would have dropped the best hosts there were.
    """
    plan = plan_dispatch([account(minutes_stale=0.5, active=0)], now=NOW)
    assert ids(plan) == ["a1"]
    assert plan.counts["dead_host"] == 0


def test_a_stale_host_still_reporting_active_accounts_is_queued_and_flagged():
    """The one state the rule cannot classify: log it, do not suppress it.

    `active_accounts` decays on a dark host rather than freezing, but the
    decay lag is unmeasured — so a device that died mid-run may sit
    stale-but-nonzero for a while. Being stuck in this log line for hours is
    the signal that the corroboration requirement needs dropping.
    """
    plan = plan_dispatch([account(minutes_stale=600, active=12)], now=NOW)
    assert ids(plan) == ["a1"]
    assert plan.counts["dead_host"] == 0
    assert plan.unclassified == [("Device 1", 600.0, 12)]


def test_a_device_reporting_no_heartbeat_at_all_is_never_suppressed():
    """Guards against a farmsync field rename silently muting the whole farm.

    A missing `last_updated` arrives as 0, which is epoch zero and reads as
    infinitely stale. Suppressing on that would drop every account in the
    farm the day the API renames the field, and the tool would keep printing
    healthy rounds while doing nothing. Treat it as unknown, not as dead.
    """
    plan = plan_dispatch([account(active=0, device_last_updated=0)], now=NOW)
    assert ids(plan) == ["a1"]
    assert plan.counts["dead_host"] == 0
    assert plan.unclassified[0][0] == "Device 1"


def test_a_missing_active_count_does_not_corroborate():
    plan = plan_dispatch([account(minutes_stale=600, active=None)], now=NOW)
    assert ids(plan) == ["a1"]


# --------------------------------------------------------------------------
# The staleness boundary
# --------------------------------------------------------------------------

def test_a_host_exactly_at_the_threshold_is_not_yet_stale():
    plan = plan_dispatch([account(minutes_stale=DEAD_DEVICE_MINUTES, active=0)], now=NOW)
    assert ids(plan) == ["a1"]


def test_a_host_just_past_the_threshold_is_stale():
    plan = plan_dispatch([account(minutes_stale=DEAD_DEVICE_MINUTES + 0.1, active=0)],
                         now=NOW)
    assert plan.queued == []


def test_the_threshold_is_configurable():
    accounts = [account(minutes_stale=45, active=0)]
    assert plan_dispatch(accounts, now=NOW, dead_device_minutes=60).queued != []
    assert plan_dispatch(accounts, now=NOW, dead_device_minutes=30).queued == []


def test_a_clock_ahead_of_the_heartbeat_never_reads_as_stale():
    """Negative age from clock skew must not wrap into a suppression."""
    plan = plan_dispatch([account(minutes_stale=-120, active=0)], now=NOW)
    assert ids(plan) == ["a1"]


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------

def test_counts_and_summary_describe_the_whole_pass():
    plan = plan_dispatch([
        account("live1", minutes_stale=1),
        account("live2", minutes_stale=1),
        account("dark1", minutes_stale=500, active=0),
    ], now=NOW)
    assert plan.counts["solvable"] == 3
    assert plan.counts["dead_host"] == 1
    assert plan.counts["queued"] == 2
    assert plan.summary() == (
        "3 solvable | 1 dead host | 0 banned | 0 in grace | "
        "0 backing off | 2 queued"
    )


def test_queue_order_is_preserved():
    """`solvable_accounts` sorts rejoining accounts first; filtering must not
    reshuffle that priority."""
    plan = plan_dispatch([account("a"), account("b"), account("c")], now=NOW)
    assert ids(plan) == ["a", "b", "c"]


def test_the_input_list_is_not_mutated():
    accounts = [account("keep"), account("drop", minutes_stale=500, active=0)]
    plan_dispatch(accounts, now=NOW)
    assert len(accounts) == 2


def test_an_empty_pool_produces_an_empty_plan():
    plan = plan_dispatch([], now=NOW)
    assert plan.queued == []
    assert plan.counts["solvable"] == 0


# --------------------------------------------------------------------------
# Banned — permanent, deterministic, and most of the solvable pool
# --------------------------------------------------------------------------

def test_a_freshly_banned_account_is_suppressed():
    plan = plan_dispatch([account()], {"a1": row(banned=True, banned_at=NOW - 60)},
                         now=NOW, roll=never_probe)
    assert plan.queued == []
    assert plan.counts["banned"] == 1


def test_a_stale_ban_is_re_checked():
    """The operator repairs banned accounts in farmsync by hand, over a couple
    of hours, and nothing in the account record announces it. One free dispatch
    per window is the only way to notice — and a dispatch that comes back
    ACCOUNT_BANNED was measured never moving the balance, so it costs
    slot-time and nothing else."""
    stale = row(banned=True, banned_at=NOW - (BAN_RECHECK_MINUTES + 1) * 60)
    plan = plan_dispatch([account()], {"a1": stale}, now=NOW, roll=never_probe)
    assert ids(plan) == ["a1"]
    assert plan.counts["ban_rechecks"] == 1
    assert plan.counts["banned"] == 0


def test_the_re_check_window_boundary():
    at = row(banned=True, banned_at=NOW - BAN_RECHECK_MINUTES * 60)
    just_before = row(banned=True, banned_at=NOW - (BAN_RECHECK_MINUTES * 60 - 1))
    assert plan_dispatch([account()], {"a1": at}, now=NOW, roll=never_probe).queued != []
    assert plan_dispatch([account()], {"a1": just_before}, now=NOW,
                         roll=never_probe).queued == []


def test_the_re_check_window_is_configurable():
    banned = row(banned=True, banned_at=NOW - 180 * 60)
    assert plan_dispatch([account()], {"a1": banned}, now=NOW,
                         ban_recheck_minutes=120, roll=never_probe).queued != []
    assert plan_dispatch([account()], {"a1": banned}, now=NOW,
                         ban_recheck_minutes=240, roll=never_probe).queued == []


def test_a_ban_with_no_timestamp_is_re_checked_at_once():
    """Rows written before banned_at existed. Unknown means re-check, not
    suppress forever — the whole point of the column."""
    plan = plan_dispatch([account()], {"a1": row(banned=True, banned_at=None)},
                         now=NOW, roll=never_probe)
    assert ids(plan) == ["a1"]
    assert plan.counts["ban_rechecks"] == 1


def test_a_re_check_still_obeys_the_dead_host_rule():
    """Testing a ban on a switched-off machine tells you nothing about the
    machine and buys a solve that expires unused."""
    stale = row(banned=True, banned_at=NOW - 10 * 3600)
    plan = plan_dispatch([account(minutes_stale=500, active=0)], {"a1": stale},
                         now=NOW, roll=never_probe)
    assert plan.queued == []
    assert plan.counts["dead_host"] == 1
    assert plan.counts["ban_rechecks"] == 0


def test_an_account_with_no_state_row_is_dispatched():
    plan = plan_dispatch([account()], {}, now=NOW, roll=never_probe)
    assert ids(plan) == ["a1"]


def test_state_may_be_omitted_entirely():
    """The dead-host rule needs no memory, so the filter runs with no
    database at all — which is how it shipped before there was one."""
    assert ids(plan_dispatch([account()], now=NOW)) == ["a1"]


# --------------------------------------------------------------------------
# Grace — a successful solve buys a window in which no captcha is issued
# --------------------------------------------------------------------------

def test_an_account_inside_its_grace_window_is_suppressed():
    state = {"a1": row(last_success_at=NOW - 10 * 60)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=WINDOW,
                         roll=never_probe)
    assert plan.queued == []
    assert plan.counts["in_grace"] == 1


def test_an_account_whose_grace_has_expired_is_dispatched():
    state = {"a1": row(last_success_at=NOW - (WINDOW + 1) * 60)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=WINDOW,
                         roll=never_probe)
    assert ids(plan) == ["a1"]


def test_the_grace_boundary_is_exclusive_at_the_far_edge():
    just_inside = {"a1": row(last_success_at=NOW - (WINDOW * 60 - 1))}
    exactly_on = {"a1": row(last_success_at=NOW - WINDOW * 60)}
    assert plan_dispatch([account()], just_inside, now=NOW,
                         grace_minutes=WINDOW, roll=never_probe).queued == []
    assert plan_dispatch([account()], exactly_on, now=NOW,
                         grace_minutes=WINDOW, roll=never_probe).queued != []


def test_grace_suppression_is_off_by_default():
    """The shipped default is 0, on measured evidence: free in-grace dispatches
    are what makes farmsync join, so suppressing them stalls working accounts.
    An account that solved one second ago must still be dispatched when no
    window is configured.
    """
    state = {"a1": row(last_success_at=NOW - 1)}
    plan = plan_dispatch([account()], state, now=NOW, roll=never_probe)
    assert ids(plan) == ["a1"]
    assert plan.counts["in_grace"] == 0


def test_the_grace_window_is_configurable():
    state = {"a1": row(last_success_at=NOW - 90 * 60)}
    assert plan_dispatch([account()], state, now=NOW, grace_minutes=60,
                         roll=never_probe).queued != []
    assert plan_dispatch([account()], state, now=NOW, grace_minutes=150,
                         roll=never_probe).queued == []


def test_a_grace_window_of_zero_turns_grace_suppression_off():
    """The kill switch, and the way the question below stays answerable.

    Whether dispatching an in-grace account nudges farmsync toward joining or
    is a pure no-op is unmeasured, and grace suppression rests on it being a
    no-op. `grace_minutes: 0` runs the dead-host filter alone so the question
    can be answered before the larger behaviour change is trusted.
    """
    state = {"a1": row(last_success_at=NOW - 1)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=0,
                         grace_probe_rate=1.0, roll=lambda: 0.0)
    assert ids(plan) == ["a1"]
    assert plan.counts["in_grace"] == 0
    assert plan.counts["grace_probes"] == 0


def test_a_success_timestamp_in_the_future_does_not_suppress():
    """Clock skew must fail toward dispatching. A wrong dispatch costs 1.8s and
    no credit; a wrong suppression stalls the account for a whole window."""
    state = {"a1": row(last_success_at=NOW + 3600)}
    assert ids(plan_dispatch([account()], state, now=NOW, roll=never_probe)) == ["a1"]


# --------------------------------------------------------------------------
# Backoff — exponential in the failure count, measured from last_failure_at
# --------------------------------------------------------------------------

def test_a_recent_failure_holds_the_account_back():
    state = {"a1": row(consecutive_failures=1, last_failure_at=NOW - 60)}
    plan = plan_dispatch([account()], state, now=NOW, roll=never_probe)
    assert plan.queued == []
    assert plan.counts["backing_off"] == 1


def test_the_backoff_window_doubles_with_each_failure():
    two_failures = row(consecutive_failures=2, last_failure_at=NOW - 3 * 60)
    one_failure = row(consecutive_failures=1, last_failure_at=NOW - 3 * 60)
    # 2 failures -> 4 minutes, still held; 1 failure -> 2 minutes, released
    assert plan_dispatch([account()], {"a1": two_failures}, now=NOW,
                         roll=never_probe).queued == []
    assert plan_dispatch([account()], {"a1": one_failure}, now=NOW,
                         roll=never_probe).queued != []


def test_the_backoff_window_is_capped():
    long_ago = NOW - (BACKOFF_CAP_MINUTES + 1) * 60
    state = {"a1": row(consecutive_failures=20, last_failure_at=long_ago)}
    assert ids(plan_dispatch([account()], state, now=NOW, roll=never_probe)) == ["a1"]


def test_no_failures_means_no_backoff_however_old_the_failure_stamp():
    """A stale last_failure_at left behind by a later success is harmless:
    the count is checked first, and every success resets it to 0."""
    state = {"a1": row(consecutive_failures=0, last_failure_at=NOW - 1)}
    assert ids(plan_dispatch([account()], state, now=NOW, roll=never_probe)) == ["a1"]


def test_a_failure_count_with_no_timestamp_does_not_stall_the_account():
    state = {"a1": row(consecutive_failures=3, last_failure_at=None)}
    assert ids(plan_dispatch([account()], state, now=NOW, roll=never_probe)) == ["a1"]


# --------------------------------------------------------------------------
# The grace probe — beats the censoring problem
# --------------------------------------------------------------------------

def test_a_small_fraction_of_in_grace_accounts_is_dispatched_anyway():
    """Suppressing every dispatch inside the window means never observing
    inside it, so the survival curve could ratchet up but never down."""
    state = {"a1": row(last_success_at=NOW - 10 * 60)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=WINDOW,
                         grace_probe_rate=0.02, roll=lambda: 0.01)
    assert ids(plan) == ["a1"]
    assert plan.counts["grace_probes"] == 1
    assert plan.counts["in_grace"] == 0


def test_a_losing_roll_leaves_the_account_suppressed():
    state = {"a1": row(last_success_at=NOW - 10 * 60)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=WINDOW,
                         grace_probe_rate=0.02, roll=lambda: 0.5)
    assert plan.queued == []
    assert plan.counts["grace_probes"] == 0


def test_a_zero_probe_rate_never_probes():
    state = {"a1": row(last_success_at=NOW - 10 * 60)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=WINDOW,
                         grace_probe_rate=0.0, roll=lambda: 0.0)
    assert plan.queued == []


def test_the_probe_bypasses_grace_only():
    """Resurrecting a dead-host account buys a solve that expires unused, and a
    banned account can never re-solve, so neither is ever worth probing."""
    always = lambda: 0.0  # noqa: E731
    in_grace = row(last_success_at=NOW - 10 * 60)

    dark = plan_dispatch([account(minutes_stale=500, active=0)],
                         {"a1": in_grace}, now=NOW, grace_probe_rate=1.0, roll=always)
    banned = plan_dispatch([account()], {"a1": banned_now(last_success_at=NOW - 600)},
                           now=NOW, grace_probe_rate=1.0, roll=always)
    held = plan_dispatch([account()], {"a1": row(last_success_at=NOW - 600,
                                                 consecutive_failures=1,
                                                 last_failure_at=NOW - 30)},
                         now=NOW, grace_probe_rate=1.0, roll=always)
    assert dark.queued == [] and dark.counts["dead_host"] == 1
    assert banned.queued == [] and banned.counts["banned"] == 1
    assert held.queued == [] and held.counts["backing_off"] == 1


def test_an_account_outside_grace_does_not_consume_a_probe():
    state = {"a1": row(last_success_at=NOW - (WINDOW + 10) * 60)}
    plan = plan_dispatch([account()], state, now=NOW, grace_minutes=WINDOW,
                         grace_probe_rate=1.0, roll=lambda: 0.0)
    assert ids(plan) == ["a1"]
    assert plan.counts["grace_probes"] == 0


# --------------------------------------------------------------------------
# Rule precedence and the full log line
# --------------------------------------------------------------------------

def test_the_cheapest_rule_wins_so_counts_never_double_report():
    """A banned account on a dark host is one suppression, not two."""
    plan = plan_dispatch([account(minutes_stale=500, active=0)],
                         {"a1": banned_now()}, now=NOW, roll=never_probe)
    assert plan.counts["dead_host"] == 1
    assert plan.counts["banned"] == 0
    assert sum(plan.counts[k] for k in
               ("dead_host", "banned", "in_grace", "backing_off", "queued")) == 1


def test_every_category_appears_in_the_summary_line():
    accounts = [account("live"), account("dark", minutes_stale=500, active=0),
                account("gone"), account("resting"), account("held")]
    state = {
        "gone": banned_now(),
        "resting": row(last_success_at=NOW - 60),
        "held": row(consecutive_failures=2, last_failure_at=NOW - 60),
    }
    plan = plan_dispatch(accounts, state, now=NOW, grace_minutes=WINDOW,
                         roll=never_probe)
    assert ids(plan) == ["live"]
    assert plan.summary() == (
        "5 solvable | 1 dead host | 1 banned | 1 in grace | "
        "1 backing off | 1 queued"
    )
