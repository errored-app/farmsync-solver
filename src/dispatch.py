"""Decide which solvable accounts are actually worth dispatching.

Between a third and four fifths of every pass is spent solving captchas for
accounts whose host machine is switched off. The solve is bought, grace expires
in hours, the machine has been down for a day, and the solve is bought again
next pass. Measured live, most of the solvable pool sat on dark hosts at the
peak, and every solve bought for one of them is unrecoverable spend.

Two farmsync device fields tell the truth about a host — `last_updated`, an
epoch-ms heartbeat, and `active_accounts`, a live count. The per-account flags
(`running`, `logged_in`, `error`) do not, and nothing here keys off them.

The other three rules need memory, which `src/state.py` supplies as a plain
dict. Most of the solvable pool is either permanently banned or inside its
grace window; both are only discoverable by dispatching, so without persistence
the tool rediscovers them every 60 seconds forever.

This module is pure: it takes the account list, the remembered state, the
clock, and the thresholds, and returns a decision. No network, no database, no
I/O. `now` is an argument rather than a call to `time.time()` so the tests
assert both sides of every boundary with plain arithmetic.

Filtering happens here rather than inside `Farmsync.solvable_accounts` on
purpose. Doing it in that loop would be shorter, but it would bury the
highest-value rule in the project inside the network client, where it could not
be tested without a FakeSession and where the suppression log line could not be
produced at all.
"""

import random

DEAD_DEVICE_MINUTES = 30

# Off, on measured evidence. This was 60, justified by the claim that a dispatch
# inside the grace window is a free no-op worth suppressing to save slot-time.
# The first half is true; the second is not.
#
# The measurement: two matched live devices for an hour, one dispatched every
# cycle and one never touched. The dispatched device more than doubled the
# accounts it had *running* and emptied its solvable list entirely. The holdout
# did not move at all — the same accounts still unjoined at the end of the hour,
# its heartbeat fresh throughout. The decisive part is that the dispatched
# device's first cycle spent *zero* solves: nearly every dispatch came back
# `joined`, free, and most of the climb happened before the run's only real
# solve. The free dispatches did the joining.
#
# So suppression does not save money and never did: an in-grace account is free
# to dispatch, and an expired one buys a solve either way. It only bought
# slot-time, and the adaptive first poll cut an in-grace dispatch from ~1.8s to
# a measured 0.275s. Across the whole pool that is a few seconds of pool time
# per refresh, weighed against holding working accounts back for a whole window.
#
# Zero also removes the censoring problem in the grace survival curve: every
# dispatch is now observed, at every age, so `--grace-report` fills in below the
# threshold instead of only above it.
#
# Limits worth knowing before raising it back: one device pair, one hour.
GRACE_MINUTES = 0

# Suppressing every dispatch inside the window means never observing inside it,
# so the grace survival curve could ratchet up but never down. This fraction of
# in-grace accounts is dispatched anyway, at their natural spread of ages.
GRACE_PROBE_RATE = 0.02

BACKOFF_CAP_MINUTES = 30

# A ban is not always permanent. ACCOUNT_BANNED is deterministic while it
# lasts, but the operator can repair an account in farmsync by hand, and that
# takes a couple of hours. Without a re-check those accounts would be invisible
# to the tool forever — repaired and still never dispatched.
#
# The re-check is close to free. A dispatch that comes back ACCOUNT_BANNED was
# measured never to move the balance at all, so it costs slot-time only:
# ~1.85s each, and across the pool a sweep of the banned accounts is a few
# seconds of wall clock.
BAN_RECHECK_MINUTES = 120


class Plan:
    """The outcome of one filtering pass."""

    def __init__(self, queued, counts, unclassified):
        self.queued = queued
        self.counts = counts
        # (device_name, heartbeat_age_minutes, active_accounts) for every device
        # the dead-host rule could not classify. See _dead_host below.
        self.unclassified = unclassified

    def summary(self) -> str:
        c = self.counts
        return (f"{c['solvable']} solvable | {c['dead_host']} dead host | "
                f"{c['banned']} banned | {c['in_grace']} in grace | "
                f"{c['backing_off']} backing off | {c['queued']} queued")


def plan_dispatch(accounts, state=None, *, now,
                  dead_device_minutes=DEAD_DEVICE_MINUTES,
                  grace_minutes=GRACE_MINUTES,
                  grace_probe_rate=GRACE_PROBE_RATE,
                  ban_recheck_minutes=BAN_RECHECK_MINUTES,
                  roll=random.random) -> Plan:
    """Drop accounts that cannot benefit from a solve. Order is preserved.

    `state` maps account id to the row `State.load` returns, and may be omitted
    entirely — the dead-host rule needs no memory.

    Rules run cheapest-first, and the first one to fire wins, so a banned
    account on a dark host is reported as one suppression rather than two.
    Grace is checked last because the probe exemption below bypasses that rule
    only: a probed account must still clear dead-host, banned and backoff.
    """
    state = state or {}
    counts = {"solvable": len(accounts), "dead_host": 0, "banned": 0,
              "in_grace": 0, "backing_off": 0, "queued": 0, "grace_probes": 0,
              "ban_rechecks": 0}
    queued = []
    unclassified = {}

    for a in accounts:
        dead, note = _dead_host(a, now, dead_device_minutes)
        if note is not None:
            unclassified.setdefault(note[0], note)
        if dead:
            counts["dead_host"] += 1
            continue

        row = state.get(a.get("id")) or {}
        if row.get("banned"):
            if _ban_is_stale(row, now, ban_recheck_minutes):
                counts["ban_rechecks"] += 1
            else:
                counts["banned"] += 1
                continue
        if _backing_off(row, now):
            counts["backing_off"] += 1
            continue
        if _in_grace(row, now, grace_minutes):
            if roll() < grace_probe_rate:
                counts["grace_probes"] += 1
            else:
                counts["in_grace"] += 1
                continue

        queued.append(a)

    counts["queued"] = len(queued)
    return Plan(queued, counts, list(unclassified.values()))


def _dead_host(account, now, dead_device_minutes):
    """Is this account's host switched off? Returns (dead, unclassified_note).

    Both halves must agree before anything is suppressed. The heartbeat alone
    decides staleness; the count only corroborates. Live devices have been seen
    reading `active_accounts == 0` while holding the richest solvable pools
    available, so suppressing on the count alone would have dropped the best
    hosts there were.

    Three states, not two:

      * fresh heartbeat            -> alive, dispatch
      * stale heartbeat + 0 active -> dark, suppress
      * stale heartbeat + N active -> unknown; queue it and say so loudly

    The third is real. `active_accounts` decays on a dark host rather than
    freezing at its last value, but the decay lag is unmeasured, so a device
    that died mid-run can sit stale-but-nonzero for a while. A device stuck in
    that log line for hours is the signal that the corroboration requirement
    needs dropping.

    A heartbeat of 0 means the field was absent, not that the host last spoke in
    1970. Treating it as infinitely stale would suppress the entire farm the day
    farmsync renames the field, and the tool would keep printing healthy rounds
    while solving nothing. It is reported as unknown instead.
    """
    heartbeat = account.get("device_last_updated") or 0
    active = account.get("device_active_accounts")
    name = account.get("device_name") or f"device {account.get('device_id')}"

    if not heartbeat:
        return False, (name, None, active)

    age_minutes = (now * 1000 - heartbeat) / 60000
    if age_minutes <= dead_device_minutes:
        return False, None
    if active == 0:
        return True, None
    return False, (name, age_minutes, active)


def _ban_is_stale(row, now, ban_recheck_minutes):
    """Is this ban old enough to be worth testing again?

    The operator repairs banned accounts in farmsync by hand, which takes
    hours. Nothing in the account record announces that, so the only way to
    find out is to dispatch once and look. A ban with no timestamp predates
    this column, so it is re-checked at the first opportunity.

    A re-check that finds the account still banned re-stamps `banned_at`, so
    each account costs one free dispatch per window and no more.
    """
    banned_at = row.get("banned_at")
    if banned_at is None:
        return True
    return now - banned_at >= ban_recheck_minutes * 60


def _backing_off(row, now):
    """Held back after a failure, for min(2^n, 30) minutes.

    The failure count is checked before the timestamp, so a stale
    `last_failure_at` left behind by a later success is harmless — every
    success resets the count to 0.
    """
    failures = row.get("consecutive_failures") or 0
    if failures <= 0:
        return False
    since = row.get("last_failure_at")
    if since is None:
        return False  # no clock to measure from; never stall on a missing value
    window = min(2 ** failures, BACKOFF_CAP_MINUTES) * 60
    return now < since + window


def _in_grace(row, now, grace_minutes):
    """Still inside the window a successful solve bought.

    A success timestamp in the future is clock skew, and it releases the
    account rather than suppressing it. Both errors are possible; only one is
    cheap. A needless dispatch costs 1.8s and no credit, while a needless
    suppression stalls a working account for a whole window.
    """
    last = row.get("last_success_at")
    if last is None:
        return False
    return 0 <= now - last < grace_minutes * 60
