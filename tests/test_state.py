"""Persistent per-account state.

Without this file the tool re-dispatches the whole farm on every restart and
forfeits the primary win, so the two properties worth the most here are that it
survives a restart and that it does not grow without bound.
"""

import sqlite3
from threading import Thread

import pytest

from src.state import (BUCKET_MINUTES, MAX_BUCKET, MIN_PROBES, RETENTION_DAYS,
                       State, bucket_label, recommend_grace_minutes)

NOW = 1_700_000_000.0
DAY = 86400.0


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.db")
    yield s
    s.close()


# --------------------------------------------------------------------------
# The outcome table, row by row
# --------------------------------------------------------------------------

def test_a_real_solve_stamps_the_success_time_and_clears_failures(state):
    state.record("a1", "fail", "boom", NOW)
    state.record("a1", "solved", "Solved Captcha in 30.0s", NOW + 10)
    row = state.load()["a1"]
    assert row["last_success_at"] == NOW + 10
    assert row["consecutive_failures"] == 0


def test_a_free_in_grace_dispatch_never_stamps_the_success_time(state):
    """`joined` means the account was *already inside* its grace window.

    Stamping it would refresh the window on every free dispatch, so the
    account would be suppressed forever and never re-checked, and the grace
    histogram would read 0% re-captcha at every age for the rest of time — a
    wrong answer that looks like a confident one. Only a real solve grants
    grace, so only a real solve records one.
    """
    state.record("a1", "solved", "solved", NOW)
    state.record("a1", "joined", "Joined in 1.8s", NOW + 3600)
    assert state.load()["a1"]["last_success_at"] == NOW


def test_a_first_ever_joined_leaves_the_success_time_empty(state):
    state.record("a1", "joined", "Joined in 1.8s", NOW)
    assert state.load()["a1"]["last_success_at"] is None


def test_a_joined_still_clears_a_failure_streak(state):
    state.record("a1", "fail", "boom", NOW)
    state.record("a1", "fail", "boom", NOW + 1)
    state.record("a1", "joined", "Joined in 1.8s", NOW + 2)
    assert state.load()["a1"]["consecutive_failures"] == 0


def test_a_ban_is_recorded_permanently_and_does_not_count_as_a_failure(state):
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    row = state.load()["a1"]
    assert row["banned"] is True
    assert row["consecutive_failures"] == 0


def test_a_plain_failure_increments_the_streak_and_stamps_the_clock(state):
    state.record("a1", "fail", "CLASSIFICATION_ERROR", NOW)
    state.record("a1", "fail", "CLASSIFICATION_ERROR", NOW + 60)
    row = state.load()["a1"]
    assert row["consecutive_failures"] == 2
    assert row["last_failure_at"] == NOW + 60


def test_only_the_real_ban_marker_bans(state):
    """`banned` is permanent, so a false positive silently retires a good
    account forever. Match the observed wire code, not a loose substring."""
    state.record("a1", "fail", "the account was banned from the game", NOW)
    assert state.load()["a1"]["banned"] is False


def test_a_ban_records_when_it_happened(state):
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    assert state.load()["a1"]["banned_at"] == NOW


def test_a_success_after_a_ban_clears_it(state):
    """The operator repairs banned accounts in farmsync by hand. Nothing in the
    account record announces that, so a successful dispatch is the only signal
    the tool ever gets — and ACCOUNT_BANNED is deterministic, so a success
    proves the ban is gone. Without this an account stays dead forever after
    being repaired."""
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    state.record("a1", "solved", "solved", NOW + 10)
    row = state.load()["a1"]
    assert row["banned"] is False
    assert row["banned_at"] is None


def test_a_free_dispatch_after_a_ban_also_clears_it(state):
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    state.record("a1", "joined", "Joined in 1.8s", NOW + 10)
    assert state.load()["a1"]["banned"] is False


def test_a_re_check_that_is_still_banned_restarts_the_clock(state):
    """Otherwise every pass would re-check it, not one pass per window."""
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW + 7200)
    assert state.load()["a1"]["banned_at"] == NOW + 7200


def test_an_ordinary_failure_does_not_clear_a_ban(state):
    """Only a success proves the ban is gone. A CLASSIFICATION_ERROR proves
    nothing either way, so the ban has to survive it."""
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    state.record("a1", "fail", "CLASSIFICATION_ERROR", NOW + 10)
    assert state.load()["a1"]["banned"] is True


def test_a_database_written_before_banned_at_existed_still_opens(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to an existing table, so a new
    column has to be added explicitly or an older state.db breaks on read."""
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY, "
                "last_success_at REAL, consecutive_failures INTEGER NOT NULL "
                "DEFAULT 0, last_failure_at REAL, banned INTEGER NOT NULL "
                "DEFAULT 0, last_error TEXT, last_seen_at REAL)")
    old.execute("INSERT INTO accounts (account_id, banned) VALUES ('old', 1)")
    old.commit()
    old.close()

    s = State(path)
    row = s.load()["old"]
    s.close()
    assert row["banned"] is True
    assert row["banned_at"] is None  # unknown, so the filter re-checks it


def test_dispatches_with_no_account_id_are_ignored(state):
    state.record(None, "solved", "solved", NOW)
    state.record("", "solved", "solved", NOW)
    assert state.load() == {}


# --------------------------------------------------------------------------
# Restart survival
# --------------------------------------------------------------------------

def test_state_survives_a_restart(tmp_path):
    first = State(tmp_path / "state.db")
    first.record("a1", "solved", "solved", NOW)
    first.record("a2", "fail", "ACCOUNT_BANNED", NOW)
    first.close()

    second = State(tmp_path / "state.db")
    rows = second.load()
    second.close()
    assert rows["a1"]["last_success_at"] == NOW
    assert rows["a2"]["banned"] is True


def test_the_database_directory_is_created_on_demand(tmp_path):
    s = State(tmp_path / "made" / "up" / "state.db")
    s.record("a1", "solved", "solved", NOW)
    s.close()
    assert (tmp_path / "made" / "up" / "state.db").exists()


# --------------------------------------------------------------------------
# Bounded growth — one row per account, one row per age bucket, forever
# --------------------------------------------------------------------------

def test_repeated_dispatches_never_add_rows(state):
    for i in range(50):
        state.record("a1", "solved", "solved", NOW + i)
    assert len(state.load()) == 1


def test_the_grace_tally_increments_in_place(state):
    """The whole bounded-growth argument rests on this.

    An earlier draft appended one row per dispatch: gigabytes a year against
    an account table of about a megabyte. A regression back to that is
    invisible until the file is enormous, so it is asserted directly.
    """
    state.record("a1", "solved", "solved", NOW)
    for i in range(100):
        state.record("a1", "joined", "joined", NOW + 60 + i)
    histogram = state.grace_histogram()
    assert len(histogram) == 1
    assert histogram[0]["probes"] == 100


def test_ages_beyond_the_last_bucket_all_land_in_one_overflow_row(state):
    state.record("a1", "solved", "solved", NOW)
    for days in (1, 2, 30, 300):
        state.record("a1", "joined", "joined", NOW + days * DAY)
    assert len(state.grace_histogram()) == 1


# --------------------------------------------------------------------------
# The grace histogram
# --------------------------------------------------------------------------

def test_an_in_grace_dispatch_is_tallied_as_not_re_captchaed(state):
    state.record("a1", "solved", "solved", NOW)
    state.record("a1", "joined", "joined", NOW + 10 * 60)
    bucket = state.grace_histogram()[0]
    assert bucket["age_bucket"] == 0
    assert bucket == {"age_bucket": 0, "probes": 1, "recaptchaed": 0}


def test_a_re_solve_is_tallied_at_the_age_it_happened(state):
    state.record("a1", "solved", "solved", NOW)
    state.record("a1", "solved", "solved", NOW + (BUCKET_MINUTES * 4 + 5) * 60)
    bucket = state.grace_histogram()[0]
    assert bucket["age_bucket"] == BUCKET_MINUTES * 4
    assert bucket["recaptchaed"] == 1


def test_an_account_that_never_solved_yields_no_observation(state):
    """There is no age to bucket, so it must not be counted as a 0-minute probe."""
    state.record("a1", "joined", "joined", NOW)
    state.record("a2", "fail", "boom", NOW)
    assert state.grace_histogram() == []


def test_failures_are_not_tallied_into_the_grace_curve(state):
    state.record("a1", "solved", "solved", NOW)
    state.record("a1", "fail", "CLASSIFICATION_ERROR", NOW + 600)
    assert state.grace_histogram() == []


def test_buckets_are_reported_in_age_order(state):
    state.record("a1", "solved", "solved", NOW)
    state.record("a2", "solved", "solved", NOW)
    state.record("a1", "joined", "joined", NOW + BUCKET_MINUTES * 3 * 60)
    state.record("a2", "joined", "joined", NOW + BUCKET_MINUTES * 60)
    assert [b["age_bucket"] for b in state.grace_histogram()] == [
        BUCKET_MINUTES, BUCKET_MINUTES * 3]


# --------------------------------------------------------------------------
# Pruning orphans
# --------------------------------------------------------------------------

def test_a_row_unseen_past_the_retention_window_is_deleted(state):
    state.mark_seen(["gone"], NOW)
    assert state.prune(NOW + (RETENTION_DAYS + 1) * DAY) == 1
    assert state.load() == {}


def test_a_row_seen_this_refresh_is_kept(state):
    state.mark_seen(["here"], NOW)
    assert state.prune(NOW + 60) == 0
    assert "here" in state.load()


def test_a_banned_row_that_reappears_in_farmsync_survives_pruning(state):
    """Pruning too eagerly is self-healing — one 1.85s free dispatch re-learns
    the ban. Pruning a live account's grace state is not."""
    state.record("a1", "fail", "ACCOUNT_BANNED", NOW)
    state.mark_seen(["a1"], NOW + 40 * DAY)
    state.prune(NOW + 41 * DAY)
    assert state.load()["a1"]["banned"] is True


def test_a_dispatch_counts_as_having_seen_the_account(state):
    """Otherwise a row written by a dispatch but never by a refresh would have
    no `last_seen_at` and could never be pruned."""
    state.record("a1", "solved", "solved", NOW)
    assert state.prune(NOW + (RETENTION_DAYS + 1) * DAY) == 1


def test_marking_seen_does_not_disturb_existing_state(state):
    state.record("a1", "solved", "solved", NOW)
    state.mark_seen(["a1"], NOW + 100)
    assert state.load()["a1"]["last_success_at"] == NOW


def test_marking_seen_tolerates_an_empty_list(state):
    state.mark_seen([], NOW)
    assert state.load() == {}


# --------------------------------------------------------------------------
# Concurrency — writes arrive from 65 worker threads
# --------------------------------------------------------------------------

def test_concurrent_writes_from_many_threads_all_land(state):
    def work(n):
        for i in range(20):
            state.record(f"a{n}", "fail", "CLASSIFICATION_ERROR", NOW + i)

    threads = [Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = state.load()
    assert len(rows) == 8
    assert all(r["consecutive_failures"] == 20 for r in rows.values())


def test_concurrent_writes_to_one_account_lose_no_increments(state):
    def work():
        for _ in range(25):
            state.record("shared", "fail", "CLASSIFICATION_ERROR", NOW)

    threads = [Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state.load()["shared"]["consecutive_failures"] == 100


# --------------------------------------------------------------------------
# The recommendation read off the curve — reported, never applied
# --------------------------------------------------------------------------

def bucket(edge, probes=MIN_PROBES, recaptchaed=0):
    return {"age_bucket": edge, "probes": probes, "recaptchaed": recaptchaed}


def test_the_recommendation_stops_at_the_first_bucket_over_the_threshold():
    histogram = [bucket(0), bucket(30), bucket(60),
                 bucket(90, recaptchaed=MIN_PROBES)]
    # safe through the 60-90 bucket, so 90 minutes, less 10% margin
    assert recommend_grace_minutes(histogram) == 81


def test_a_thin_bucket_stops_the_walk_rather_than_counting_as_clean():
    """One lucky probe at a long age must not talk the window up."""
    histogram = [bucket(0), bucket(30, probes=1)]
    assert recommend_grace_minutes(histogram) == 27


def test_no_recommendation_when_the_very_first_bucket_is_thin():
    assert recommend_grace_minutes([bucket(0, probes=1)]) is None


def test_no_recommendation_when_re_captcha_starts_immediately():
    assert recommend_grace_minutes([bucket(0, recaptchaed=MIN_PROBES)]) is None


def test_no_recommendation_from_an_empty_history():
    assert recommend_grace_minutes([]) is None


def test_the_overflow_bucket_does_not_recommend_past_its_own_edge():
    assert recommend_grace_minutes([bucket(MAX_BUCKET)]) == int(MAX_BUCKET * 0.9)


def test_the_threshold_is_five_percent():
    just_under = [bucket(0, probes=100, recaptchaed=4)]
    just_over = [bucket(0, probes=100, recaptchaed=5)]
    assert recommend_grace_minutes(just_under) == 27
    assert recommend_grace_minutes(just_over) is None


def test_bucket_labels_read_as_ranges_with_an_open_top():
    assert bucket_label(60) == f"60-{60 + BUCKET_MINUTES}"
    assert bucket_label(MAX_BUCKET) == f"{MAX_BUCKET}+"


def test_the_recommendation_reads_a_real_database(state):
    for i in range(MIN_PROBES):
        state.record(f"a{i}", "solved", "solved", NOW)
        state.record(f"a{i}", "joined", "joined", NOW + 10 * 60)
    assert recommend_grace_minutes(state.grace_histogram()) == 27


# --------------------------------------------------------------------------
# Storage shape
# --------------------------------------------------------------------------

def test_the_account_id_is_the_primary_key(tmp_path):
    s = State(tmp_path / "state.db")
    s.close()
    db = sqlite3.connect(tmp_path / "state.db")
    cols = {r[1]: r for r in db.execute("PRAGMA table_info(accounts)")}
    db.close()
    assert cols["account_id"][5] == 1  # pk flag
