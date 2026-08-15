"""Persistent per-account state — what the tool remembers between passes.

Without this the tool holds no memory: every 60 seconds it rediscovers that an
account is banned, or is still inside its captcha grace window, and dispatches
it again. Measured live, most of the solvable pool was either permanently
banned or in grace — a whole sweep of them produced no chargeable dispatches
and never moved the balance. All of it was slot-time spent on accounts that
could not benefit.

SQLite rather than JSON: writes arrive from 65 worker threads and a crash
mid-write must not corrupt the file. One connection with
``check_same_thread=False`` behind one lock is ample at this write volume.

The lock is private, not the ``ThreadLock`` singleton in ``src/thread_lock.py``.
That one guards the shared ``counts`` dict; reusing it would serialise every
database write against every counter update for no reason.

**Growth is bounded by farm size, not by runtime.** Both tables are upserted,
never appended: one row per account that exists, and one row per age bucket. At
~250 bytes a row, a farm of any plausible size comes to single-digit megabytes
and stays there however long the tool runs. That is a design constraint rather
than an accident — any future column must be a fixed-width fact *about an
account*, never a log of events.
"""

import sqlite3
import threading
from pathlib import Path

from .paths import state_db

# Answered by paths.py, which is the only module allowed to know a location.
# From source this is still <repo>/data/state.db — data/, not input/, because
# input/ holds files the operator writes and this one the tool writes. From a
# frozen build it is %LOCALAPPDATA%\FarmsyncSolver\state.db, because resolving
# from __file__ there points into a temp directory that is deleted on exit,
# which would quietly discard every ban and grace stamp on every run.
DB_PATH = state_db()

# A module constant, deliberately not a config key. Every key added has to be
# mirrored into config.example.json or a fresh clone silently misses it, and
# this value has no plausible reason to be tuned — the file is about a megabyte
# whether the window is 30 days or 300.
RETENTION_DAYS = 30

# The grace histogram. Fixed edges: bucket boundaries are decided at write time
# and cannot be re-cut later, which is an acceptable trade for a number read
# once every few weeks. Everything past MAX_BUCKET lands in one overflow row, so
# the table is at most 25 rows however long the tool runs.
BUCKET_MINUTES = 30
MAX_BUCKET = 720  # 12 hours

# A bucket holding a handful of observations is not evidence. Recommending a
# grace window off one lucky probe would be worse than recommending nothing.
MIN_PROBES = 20

# The observed wire code. Matching a looser substring such as "banned" would
# risk a false positive, and `banned` is permanent — one bad match retires a
# working account forever.
BAN_MARKER = "account_banned"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id           TEXT PRIMARY KEY,
    last_success_at      REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at      REAL,
    banned               INTEGER NOT NULL DEFAULT 0,
    banned_at            REAL,
    last_error           TEXT,
    last_seen_at         REAL
);
CREATE TABLE IF NOT EXISTS grace_probes (
    age_bucket  INTEGER PRIMARY KEY,
    probes      INTEGER NOT NULL DEFAULT 0,
    recaptchaed INTEGER NOT NULL DEFAULT 0
);
"""


class State:
    def __init__(self, path=DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first written.

        `CREATE TABLE IF NOT EXISTS` does nothing at all to an existing table,
        so a new column has to be added explicitly or an older state.db keeps
        its old shape and every read of the new field raises. Adding a column
        is cheap and leaves existing rows with NULL, which each caller reads as
        "not known yet".
        """
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(accounts)")}
        for column, kind in (("banned_at", "REAL"),):
            if column not in have:
                self._db.execute(f"ALTER TABLE accounts ADD COLUMN {column} {kind}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ----------------------------------------------------------------- reads

    def load(self) -> dict:
        """Everything the dispatch filter needs, keyed by account id."""
        with self._lock:
            rows = self._db.execute(
                "SELECT account_id, last_success_at, consecutive_failures, "
                "last_failure_at, banned, banned_at FROM accounts").fetchall()
        return {r["account_id"]: {
            "last_success_at": r["last_success_at"],
            "consecutive_failures": r["consecutive_failures"],
            "last_failure_at": r["last_failure_at"],
            "banned": bool(r["banned"]),
            "banned_at": r["banned_at"],
        } for r in rows}

    def grace_histogram(self) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT age_bucket, probes, recaptchaed FROM grace_probes "
                "ORDER BY age_bucket").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- writes

    def record(self, account_id, outcome: str, detail: str, now: float) -> None:
        """Apply one dispatch outcome. The only place a dispatch touches the db.

        ``consecutive_failures`` counts *dispatches*, not attempts.
        ``Roblox._run_account`` already retries up to MAX_ATTEMPTS internally and
        CLASSIFICATION_ERROR is transient — 63% of those accounts recover on
        attempt 2 or 3 — so counting each internal attempt would push a merely
        flaky account straight into a 4-minute backoff.
        """
        if not account_id:
            return
        with self._lock:
            row = self._db.execute(
                "SELECT last_success_at, consecutive_failures FROM accounts "
                "WHERE account_id = ?", (account_id,)).fetchone()
            previous_success = row["last_success_at"] if row else None
            failures = row["consecutive_failures"] if row else 0

            if outcome == "solved":
                # Read the age off the *previous* success before overwriting it.
                self._observe(previous_success, now, needed_captcha=True)
                # Any success clears a ban. ACCOUNT_BANNED is deterministic, so
                # a solve proves the account is no longer banned — which is how
                # an account the operator repaired in farmsync gets back into
                # rotation without anyone touching the database.
                self._upsert(account_id, now, last_success_at=now,
                             consecutive_failures=0, banned=0, banned_at=None)
            elif outcome == "joined":
                # Deliberately does not touch last_success_at. A `joined` result
                # means the account was already inside its grace window;
                # stamping it would refresh the window on every free dispatch,
                # suppressing the account forever and flattening the grace
                # histogram to 0% re-captcha at every age. Only a real solve
                # grants grace, so only a real solve records one.
                self._observe(previous_success, now, needed_captcha=False)
                self._upsert(account_id, now, consecutive_failures=0,
                             banned=0, banned_at=None)
            elif BAN_MARKER in (detail or "").lower():
                # banned_at is re-stamped on every ban, including a re-check
                # that finds the account still banned, so the next re-check is
                # always one full window away rather than immediate.
                self._upsert(account_id, now, banned=1, banned_at=now,
                             consecutive_failures=0, last_error=detail)
            else:
                self._upsert(account_id, now, consecutive_failures=failures + 1,
                             last_failure_at=now, last_error=detail)
            self._db.commit()

    def mark_seen(self, account_ids, now: float) -> None:
        """Stamp every account this refresh returned, so pruning can spot orphans."""
        rows = [(a, now) for a in account_ids if a]
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO accounts (account_id, last_seen_at) VALUES (?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                rows)
            self._db.commit()

    def prune(self, now: float) -> int:
        """Delete rows for accounts farmsync has not listed in RETENTION_DAYS.

        Safe rather than merely tidy: if a pruned account returns, the tool
        re-learns `banned` on a single dispatch costing 1.85s and no credit.
        Losing a row is cheap; keeping a dead one forever is not.
        """
        cutoff = now - RETENTION_DAYS * 86400
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM accounts WHERE last_seen_at IS NOT NULL "
                "AND last_seen_at < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    # --------------------------------------------------------------- private

    def _upsert(self, account_id, now, **fields):
        """Caller holds the lock. Column names come from this module only."""
        fields["last_seen_at"] = now
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        sets = ", ".join(f"{c} = excluded.{c}" for c in fields)
        self._db.execute(
            f"INSERT INTO accounts (account_id, {cols}) VALUES (?, {marks}) "
            f"ON CONFLICT(account_id) DO UPDATE SET {sets}",
            (account_id, *fields.values()))

    def _observe(self, previous_success, now, needed_captcha):
        """One point on the grace survival curve: (age since last solve, re-captchaed?).

        A dispatch with no recorded success yields nothing — there is no age to
        bucket, and counting it as a 0-minute probe would invent data.
        """
        if previous_success is None:
            return
        age = (now - previous_success) / 60
        if age < 0:
            return
        bucket = min(int(age // BUCKET_MINUTES) * BUCKET_MINUTES, MAX_BUCKET)
        self._db.execute(
            "INSERT INTO grace_probes (age_bucket, probes, recaptchaed) "
            "VALUES (?, 1, ?) ON CONFLICT(age_bucket) DO UPDATE SET "
            "probes = probes + 1, recaptchaed = recaptchaed + excluded.recaptchaed",
            (bucket, 1 if needed_captcha else 0))


def bucket_label(age_bucket: int) -> str:
    if age_bucket >= MAX_BUCKET:
        return f"{age_bucket}+"
    return f"{age_bucket}-{age_bucket + BUCKET_MINUTES}"


def recommend_grace_minutes(histogram, threshold=0.05, min_probes=MIN_PROBES):
    """The largest age still safely inside the window, less 10% for margin.

    Walks the buckets youngest-first and stops at the first one that re-captchas
    at or above `threshold`, or at the first one too thin to be evidence.
    Returns None when no bucket qualifies.

    Reported, never applied. A bad automatic value would silently halt all work,
    and this number changes rarely enough that a manual config edit is fine.
    Note the walk treats a missing bucket as a gap rather than as a zero, so a
    recommendation that skips ages is extrapolating — read the printed table,
    not just the number.
    """
    safe_edge = None
    for b in histogram:
        if b["probes"] < min_probes:
            break
        if b["recaptchaed"] / b["probes"] >= threshold:
            break
        safe_edge = min(b["age_bucket"] + BUCKET_MINUTES, MAX_BUCKET)
    if safe_edge is None:
        return None
    return int(safe_edge * 0.9)
