import threading

import pytest

from src import pool as pool_mod
from src import roblox as roblox_mod
from src import solver as solver_mod
from src.health import FAILURE_THRESHOLD, SolverHealth
from src.roblox import MAX_ATTEMPTS, Roblox
from src.solver import SolverError
from src.state import State
from src.thread_lock import lock


def make_worker(accounts, health=None):
    counts = {"joined": 0, "solved": 0, "fail": 0}
    return Roblox(lock, pool_mod.of(accounts), counts, health=health), counts


def some_accounts(n):
    return [{"username": f"u{i}", "cookie": f"c{i}"} for i in range(n)]


def stub_solve(monkeypatch, *results):
    """Script solver.solve: each entry is a timings dict to return or an exception.

    The last entry repeats, so a single exception covers every retry.
    """
    calls = []
    queue = list(results)

    def fake(cookie):
        calls.append(cookie)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(solver_mod, "solve", fake)
    return calls


# --------------------------------------------------------------------------
# Outcome classification — derived purely from the solver's timings
# --------------------------------------------------------------------------

def test_nonzero_solve_ms_classifies_as_solved(monkeypatch, no_sleep, account):
    stub_solve(monkeypatch, {"total_ms": 4200, "solve_ms": 1800})
    worker, counts = make_worker([account])
    worker.check()
    assert counts == {"joined": 0, "solved": 1, "fail": 0}


@pytest.mark.parametrize("timings", [
    {"total_ms": 900, "solve_ms": 0},
    {"total_ms": 900},
    {},
])
def test_absent_or_zero_solve_ms_classifies_as_joined(monkeypatch, no_sleep, account, timings):
    stub_solve(monkeypatch, timings)
    worker, counts = make_worker([account])
    worker.check()
    assert counts == {"joined": 1, "solved": 0, "fail": 0}


def test_cookie_is_passed_with_the_roblosecurity_prefix(monkeypatch, no_sleep, account):
    calls = stub_solve(monkeypatch, {})
    worker, _ = make_worker([account])
    worker.check()
    assert calls == [".ROBLOSECURITY=COOKIEVALUE"]


# --------------------------------------------------------------------------
# Retry layer 3 — bounded attempts, capped backoff, terminal short-circuit
# --------------------------------------------------------------------------

def test_transient_failure_is_retried_then_succeeds(monkeypatch, no_sleep, account):
    calls = stub_solve(monkeypatch, RuntimeError("network blip"),
                       {"total_ms": 1000, "solve_ms": 500})
    worker, counts = make_worker([account])
    worker.check()
    assert len(calls) == 2
    assert counts["solved"] == 1


def test_persistent_failure_exhausts_attempts(monkeypatch, no_sleep, account):
    calls = stub_solve(monkeypatch, RuntimeError("network blip"))
    worker, counts = make_worker([account])
    worker.check()
    assert len(calls) == MAX_ATTEMPTS
    assert counts["fail"] == 1
    assert no_sleep == [1, 2]  # 2**0, 2**1


def test_backoff_is_clamped_by_the_cap(monkeypatch, no_sleep, account):
    """BACKOFF_CAP is currently inert — at MAX_ATTEMPTS=3 the sequence is 1s, 2s
    and never reaches the 2s ceiling. Raising the attempt count is the only way
    to observe the clamp, so this test is what stops the cap from silently
    rotting into dead code if someone bumps MAX_ATTEMPTS later.
    """
    monkeypatch.setattr(roblox_mod, "MAX_ATTEMPTS", 5)
    stub_solve(monkeypatch, RuntimeError("network blip"))
    worker, counts = make_worker([account])
    worker.check()
    assert no_sleep == [1, 2, 2, 2]  # not 1, 2, 4, 8
    assert counts["fail"] == 1


@pytest.mark.parametrize("message", [
    "invalid_api_key",
    "insufficient_balance",
    "cookie dead",
    "account moderated",
    "key_expired",
    "MODERATED",  # matching is case-insensitive
])
def test_terminal_errors_are_not_retried(monkeypatch, no_sleep, account, message):
    """Burning retries on a dead cookie or bad key wastes balance and time."""
    calls = stub_solve(monkeypatch, SolverError(message))
    worker, counts = make_worker([account])
    worker.check()
    assert len(calls) == 1
    assert counts["fail"] == 1
    assert no_sleep == []


def test_failure_detail_is_truncated(monkeypatch, no_sleep, account, capsys):
    stub_solve(monkeypatch, RuntimeError("x" * 500))
    worker, _ = make_worker([account])
    worker.check()
    printed = capsys.readouterr().out
    assert "x" * 120 in printed
    assert "x" * 121 not in printed


# --------------------------------------------------------------------------
# Account iteration
# --------------------------------------------------------------------------

def test_worker_processes_every_account_then_stops(monkeypatch, no_sleep):
    accounts = [{"username": f"u{i}", "cookie": f"c{i}"} for i in range(5)]
    calls = stub_solve(monkeypatch, {})
    worker, counts = make_worker(accounts)
    worker.check()
    assert len(calls) == 5
    assert counts["joined"] == 5


def test_workers_sharing_a_queue_never_double_solve(monkeypatch, no_sleep):
    accounts = [{"username": f"u{i}", "cookie": f"c{i}"} for i in range(20)]
    calls = stub_solve(monkeypatch, {})
    counts = {"joined": 0, "solved": 0, "fail": 0}
    work = pool_mod.of(accounts)
    for _ in range(4):
        Roblox(lock, work, counts).check()
    assert sorted(calls) == sorted(f".ROBLOSECURITY=c{i}" for i in range(20))
    assert counts["joined"] == 20


def test_a_worker_releases_its_account_even_when_the_dispatch_fails(
        monkeypatch, no_sleep):
    """A claim that is never released suppresses that account for the life of
    the process, and nothing in the output would say so — it simply stops
    appearing. Failures are the path that would leak."""
    stub_solve(monkeypatch, SolverError("COOKIE_DEAD"))
    work = pool_mod.WorkQueue()
    work.submit([{"id": "a1", "username": "u", "cookie": "c"}])
    work.close()
    Roblox(lock, work, {"joined": 0, "solved": 0, "fail": 0}).check()
    assert work.inflight == 0


def test_a_worker_waits_on_an_open_queue_instead_of_exiting(monkeypatch, no_sleep):
    """An empty queue in daemon mode means the producer has not refreshed yet.
    Treating that as "no more work" would end the pool after the first lull."""
    stub_solve(monkeypatch, {})
    work = pool_mod.WorkQueue()
    counts = {"joined": 0, "solved": 0, "fail": 0}
    worker = Roblox(lock, work, counts)

    thread = threading.Thread(target=worker.check, daemon=True)
    thread.start()
    thread.join(timeout=0.6)
    assert thread.is_alive(), "worker exited while the producer was still open"

    work.submit([{"id": "a1", "username": "u", "cookie": "c"}])
    work.close()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert counts["joined"] == 1


def test_blank_username_falls_back_to_shortened_cookie(monkeypatch, no_sleep, capsys):
    stub_solve(monkeypatch, {})
    worker, _ = make_worker([{"username": "", "cookie": "C" * 40}])
    worker.check()
    assert "C" * 17 + "..." in capsys.readouterr().out


# --------------------------------------------------------------------------
# Outcome -> persistent state
#
# These failures are silent: the tool keeps running and simply stops
# suppressing the right accounts, so nothing shows up without a test.
# --------------------------------------------------------------------------

def solving_worker(monkeypatch, tmp_path, *results, id="a1"):
    state = State(tmp_path / "state.db")
    stub_solve(monkeypatch, *results)
    counts = {"joined": 0, "solved": 0, "fail": 0}
    accounts = [{"id": id, "username": "tester", "cookie": "COOKIEVALUE"}]
    return Roblox(lock, pool_mod.of(accounts), counts, state), state


def test_a_real_solve_is_written_to_state(monkeypatch, no_sleep, tmp_path):
    worker, state = solving_worker(monkeypatch, tmp_path,
                                   {"total_ms": 30000, "solve_ms": 28000})
    worker.check()
    assert state.load()["a1"]["last_success_at"] is not None
    state.close()


def test_a_free_in_grace_dispatch_leaves_the_success_time_alone(
        monkeypatch, no_sleep, tmp_path):
    worker, state = solving_worker(monkeypatch, tmp_path, {"total_ms": 1800})
    worker.check()
    assert state.load()["a1"]["last_success_at"] is None
    state.close()


def test_three_attempts_inside_one_dispatch_count_as_one_failure(
        monkeypatch, no_sleep, tmp_path):
    """CLASSIFICATION_ERROR is transient and 63% of those accounts recover on
    attempt 2 or 3. Counting each internal attempt would push a merely flaky
    account straight into a 4-minute backoff."""
    worker, state = solving_worker(monkeypatch, tmp_path,
                                   SolverError("CLASSIFICATION_ERROR"))
    worker.check()
    assert state.load()["a1"]["consecutive_failures"] == 1
    state.close()


def test_a_ban_is_persisted(monkeypatch, no_sleep, tmp_path):
    worker, state = solving_worker(monkeypatch, tmp_path,
                                   SolverError("ACCOUNT_BANNED"))
    worker.check()
    assert state.load()["a1"]["banned"] is True
    state.close()


def test_an_account_without_an_id_is_dispatched_but_not_stored(
        monkeypatch, no_sleep, tmp_path):
    worker, state = solving_worker(monkeypatch, tmp_path, {}, id=None)
    worker.check()
    assert worker.counts["joined"] == 1
    assert state.load() == {}
    state.close()


# --------------------------------------------------------------------------
# The four terminal classes at the worker
#
# All four short-circuit the retry layer. What differs is what happens to the
# rest of the pool afterwards.
# --------------------------------------------------------------------------

def test_a_dead_key_stops_the_worker_taking_more_accounts(monkeypatch, no_sleep):
    """The failure this replaces is one identical error per account for one bad key."""
    calls = stub_solve(monkeypatch, SolverError("invalid_api_key"))
    health = SolverHealth()
    worker, counts = make_worker(some_accounts(50), health)
    worker.check()
    assert len(calls) == 1
    assert counts["fail"] == 1
    assert health.halt_reason == "invalid_api_key"


def test_an_empty_wallet_stops_the_worker_without_halting(monkeypatch, no_sleep):
    """An empty wallet parks and waits for a top-up rather than treating this
    as fatal."""
    stub_solve(monkeypatch, SolverError("insufficient_balance"))
    health = SolverHealth()
    worker, _ = make_worker(some_accounts(50), health)
    worker.check()
    assert health.out_of_credit is True
    assert health.halt_reason is None
    assert health.open is False


def test_a_paused_service_short_circuits_retries_and_feeds_the_breaker(
        monkeypatch, no_sleep):
    calls = stub_solve(monkeypatch, SolverError("SERVICE_PAUSED"))
    health = SolverHealth()
    worker, _ = make_worker(some_accounts(50), health)
    worker.check()
    assert no_sleep == []          # terminal: no backoff burned
    assert len(calls) == FAILURE_THRESHOLD
    assert health.open is True


def test_a_dead_cookie_fails_only_its_own_account(monkeypatch, no_sleep):
    """Account-shaped failures must never stop the pool."""
    calls = stub_solve(monkeypatch, SolverError("COOKIE_DEAD"))
    health = SolverHealth()
    worker, counts = make_worker(some_accounts(6), health)
    worker.check()
    assert len(calls) == 6
    assert counts["fail"] == 6
    assert health.stopped() is False


def test_a_worker_started_with_the_breaker_open_dispatches_nothing(
        monkeypatch, no_sleep):
    """Parking beats burning 65 slots against a service that is down."""
    calls = stub_solve(monkeypatch, {})
    health = SolverHealth()
    health.record_failure(SolverError("SERVICE_PAUSED"))
    health.open = True
    worker, counts = make_worker(some_accounts(10), health)
    worker.check()
    assert calls == []
    assert counts == {"joined": 0, "solved": 0, "fail": 0}


def test_repeated_timeouts_open_the_breaker(monkeypatch, no_sleep):
    """Timeouts are retried, so one dispatch spends all three attempts — but it
    is still one failure, so the breaker counts dispatches, not attempts."""
    stub_solve(monkeypatch, TimeoutError("read timed out"))
    health = SolverHealth()
    worker, counts = make_worker(some_accounts(50), health)
    worker.check()
    assert counts["fail"] == FAILURE_THRESHOLD
    assert health.open is True


def test_a_success_between_outages_keeps_the_breaker_closed(monkeypatch, no_sleep):
    stub_solve(monkeypatch, TimeoutError("down"), TimeoutError("down"),
               TimeoutError("down"), {"total_ms": 900}, TimeoutError("down"))
    health = SolverHealth()
    worker, _ = make_worker(some_accounts(4), health)
    worker.check()
    assert health.open is False


# --------------------------------------------------------------------------
# Live progress — one callback per dispatch, for the terminal title
# --------------------------------------------------------------------------

def test_a_dispatch_reports_its_outcome_to_the_progress_callback(monkeypatch, no_sleep):
    seen = []
    worker, _ = make_worker(some_accounts(2))
    worker.on_dispatch = seen.append
    stub_solve(monkeypatch, {"total_ms": 4200, "solve_ms": 1800},
               {"total_ms": 900, "solve_ms": 0})
    worker.check()
    assert seen == ["solved", "joined"]


def test_three_attempts_inside_one_dispatch_report_once(monkeypatch, no_sleep, account):
    """The callback mirrors `_record`: it fires per dispatch, not per attempt,
    so a flaky account cannot inflate the session totals in the title."""
    seen = []
    worker, _ = make_worker([account])
    worker.on_dispatch = seen.append
    stub_solve(monkeypatch, SolverError("CLASSIFICATION_ERROR"))
    worker.check()
    assert seen == ["fail"]
