"""How the producer answers a pool that stopped itself, and how it exits.

`react_to_health` is the single place health.py's four terminal classes become three
different reactions, so each branch is asserted here rather than left to a live
run to discover. `Depletion` and `CreditAlerter` are only reached on the park
branch, which the tests that do not park pass `None` for.

The `serve` tests below cover the one thing the continuous pool can silently
lose: a persistent pool has no natural end, so `round_delay <= 0` turning into
a daemon would look exactly like normal operation.
"""

import threading
import time

from src import main
from src import paths
from src import pool as pool_mod
from src import solver as solver_mod
from src.credit import CreditAlerter, Depletion
from src.health import FAILURE_THRESHOLD, SolverHealth
from src.output import TitleBar
from src.progress import Progress

STATUS = {"estimated_solves": 5000, "balance": 7.5, "max_concurrent": 65,
          "price_per_1k": 1.5}


def sick_health():
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD):
        health.record_failure(RuntimeError("SERVICE_PAUSED"))
    return health


def test_a_healthy_pool_is_left_completely_alone():
    keep_running, status = main.react_to_health(
        SolverHealth(), None, None, STATUS, 60)
    assert keep_running is True
    assert status is STATUS


def test_a_rejected_key_stops_the_run_and_says_how_to_fix_it(capsys):
    """One message, not one per account — and it has to say what to do.

    "what to do" is the settings menu, not a text editor. The operator this
    ships to is not the one who wrote it: an instruction to edit a key inside
    a JSON file is a dead end for anyone who would not have found that file on
    their own, and the tool already carries a screen that does the edit. The
    path stays on the line below for whoever wants it.
    """
    health = SolverHealth()
    health.record_failure(RuntimeError("INVALID_API_KEY"))
    keep_running, _ = main.react_to_health(health, None, None, STATUS, 60)
    printed = capsys.readouterr().out
    assert keep_running is False
    assert "INVALID_API_KEY" in printed
    assert "press S" in printed
    # The real path, not a hardcoded one: a frozen build's config lives in
    # %LOCALAPPDATA% and has no input/ directory to point anyone at.
    assert str(paths.config_file()) in printed


def test_the_rejected_key_message_never_tells_anyone_to_edit_the_file(capsys):
    """The regression, stated as the thing that must not come back.

    Asserting only that "press S" is present would pass a message that says
    both — and both is worse than either, because the harder instruction is
    the one an anxious operator follows.
    """
    health = SolverHealth()
    health.record_failure(RuntimeError("INVALID_API_KEY"))
    main.react_to_health(health, None, None, STATUS, 60)
    printed = capsys.readouterr().out.lower()
    assert "fix 'api_key'" not in printed
    assert "edit" not in printed


def test_the_post_run_stop_report_also_points_at_the_menu(capsys):
    """`report_stop` says the same thing after the workers have gone, and it
    is a second copy of the wording — so it gets a second test rather than
    trusting the two to be changed together."""
    health = SolverHealth()
    health.record_failure(RuntimeError("INVALID_API_KEY"))
    main.report_stop(health)
    printed = capsys.readouterr().out
    assert "press S" in printed
    assert "fix 'api_key'" not in printed.lower()


def test_a_rejected_key_is_answered_before_an_outage_is():
    """A key that will never work must not sit in a probe loop forever."""
    health = sick_health()
    health.record_failure(RuntimeError("INVALID_API_KEY"))
    keep_running, _ = main.react_to_health(health, None, None, STATUS, 60)
    assert keep_running is False
    assert health.open is True  # untouched — nothing probed


def test_an_outage_is_waited_out_and_the_run_continues(monkeypatch):
    health = sick_health()
    monkeypatch.setattr(main.health_mod, "PROBE_DELAY_SECONDS", 0)
    monkeypatch.setattr(main.solver, "status", lambda: STATUS)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)

    keep_running, _ = main.react_to_health(health, None, None, STATUS, 60)
    assert keep_running is True
    assert health.open is False


def test_a_one_shot_run_reports_an_outage_instead_of_waiting_it_out(capsys):
    """`round_delay <= 0` does one pass and exits. Sitting on a probe loop would
    silently turn a one-shot run into a daemon, and nothing else would say so.
    """
    health = sick_health()
    keep_running, _ = main.react_to_health(health, None, None, STATUS, 60,
                                           wait=False)
    assert keep_running is False
    assert "dibycap is not answering" in capsys.readouterr().out


def test_an_empty_wallet_parks_and_then_carries_on(monkeypatch):
    """The credit/health seam: `insufficient_balance` waits for a top-up, never exits."""
    topped_up = {"estimated_solves": 9000, "balance": 13.5, "max_concurrent": 65}
    health = SolverHealth()
    health.record_failure(RuntimeError("insufficient_balance"))
    monkeypatch.setattr(main.solver, "status", lambda: topped_up)
    monkeypatch.setattr(main.credit, "wait_for_top_up",
                        lambda *a, **k: topped_up)

    keep_running, status = main.react_to_health(
        health, CreditAlerter(""), Depletion(), {"estimated_solves": 0}, 60)
    assert keep_running is True
    assert status == topped_up
    assert health.stopped() is False


# --------------------------------------------------------------------------
# The continuous pool
#
# A persistent pool has no natural end, so the failure to guard against is
# `round_delay <= 0` quietly becoming a daemon — which looks identical to
# normal operation until someone notices the process never came back.
# --------------------------------------------------------------------------

SETTINGS = {
    "threads": 2,
    "round_delay": 0,
    "dead_device_minutes": 30,
    "grace_minutes": 60,
    "grace_probe_rate": 0.0,
    "ban_recheck_minutes": 120,
    "status_poll_seconds": 60,
}


class FakeFarm:
    """farmsync, returning the same live-host accounts on every refresh."""

    def __init__(self, count=4):
        now_ms = time.time() * 1000
        self.accounts = [{"id": f"a{i}", "username": f"u{i}", "cookie": f"c{i}",
                          "device_id": 1, "device_name": "Device 1",
                          "device_last_updated": now_ms,
                          "device_active_accounts": 5}
                         for i in range(count)]
        self.refreshes = 0

    def solvable_accounts(self):
        self.refreshes += 1
        return list(self.accounts)


def run_serve(monkeypatch, tmp_path, settings, farm, health=None, solve=None):
    """Wire `serve` to fakes and hand back what it needs to be asserted on."""
    from src.state import State

    calls = []

    def fake_solve(cookie):
        calls.append((cookie, threading.current_thread().ident))
        return {"total_ms": 10, "solve_ms": 0} if solve is None else solve

    monkeypatch.setattr(solver_mod, "solve", fake_solve)
    monkeypatch.setattr(main.solver, "status", lambda: STATUS)

    health = SolverHealth() if health is None else health
    work = pool_mod.WorkQueue()
    args = (farm, State(tmp_path / "state.db"), work, health,
            CreditAlerter(""), Depletion(), Progress(time.time()),
            TitleBar(stream=None), settings, lambda outcome: None)
    return calls, work, args


def test_a_one_shot_run_refreshes_once_and_exits(monkeypatch, tmp_path, capsys):
    """`round_delay <= 0` is a documented mode. Under a pool that never ends on
    its own, losing it would look exactly like the tool working."""
    farm = FakeFarm(4)
    calls, work, args = run_serve(monkeypatch, tmp_path, SETTINGS, farm)

    done = threading.Event()
    thread = threading.Thread(target=lambda: (main.serve(*args), done.set()),
                              daemon=True)
    thread.start()
    assert done.wait(timeout=10), "a one-shot run did not exit"

    assert farm.refreshes == 1
    assert sorted(c for c, _ in calls) == \
        sorted(f".ROBLOSECURITY=c{i}" for i in range(4))
    assert work.drained() is True


def test_a_daemon_run_keeps_the_same_workers_across_refreshes(
        monkeypatch, tmp_path):
    """The whole point of the change: workers outlive a refresh instead of
    being rebuilt behind a barrier that waits for the slowest one."""
    settings = dict(SETTINGS, round_delay=0.05)
    farm = FakeFarm(4)
    health = SolverHealth()
    calls, _, args = run_serve(monkeypatch, tmp_path, settings, farm, health)

    thread = threading.Thread(target=lambda: main.serve(*args), daemon=True)
    thread.start()
    deadline = time.time() + 10
    while farm.refreshes < 3 and time.time() < deadline:
        time.sleep(0.02)
    health.halt_reason = "stop"          # the one condition that ends a daemon
    thread.join(timeout=10)

    assert thread.is_alive() is False
    assert farm.refreshes >= 3
    assert len(calls) > 4, "later refreshes dispatched nothing"
    workers = {ident for _, ident in calls}
    assert len(workers) <= settings["threads"], \
        "the pool was rebuilt per refresh instead of persisting"


def test_an_account_still_in_flight_is_not_queued_again_by_the_next_refresh(
        monkeypatch, tmp_path):
    """farmsync still lists an account a worker is holding, and a 47-second
    median dispatch outlives a 60-second refresh often enough to matter."""
    settings = dict(SETTINGS, round_delay=0.05)
    farm = FakeFarm(2)
    health = SolverHealth()
    holding = threading.Event()

    def slow_solve(cookie):
        holding.set()
        time.sleep(0.4)
        return {"total_ms": 10, "solve_ms": 0}

    calls, work, args = run_serve(monkeypatch, tmp_path, settings, farm, health)
    monkeypatch.setattr(solver_mod, "solve", slow_solve)

    thread = threading.Thread(target=lambda: main.serve(*args), daemon=True)
    thread.start()
    assert holding.wait(timeout=5)
    time.sleep(0.15)                      # at least one more refresh lands
    assert work.pending == 0, "an in-flight account was queued a second time"
    health.halt_reason = "stop"
    thread.join(timeout=10)


def test_ctrl_c_gives_the_prompt_back_without_waiting_on_a_slow_dispatch(
        monkeypatch, tmp_path):
    """Under the round loop the operator got their prompt back at once. A worker
    can be inside a 180-poll wait, so a permanent pool must not make Ctrl-C
    inherit that — the workers are daemon threads and die with the process."""
    holding = threading.Event()

    class InterruptingFarm(FakeFarm):
        def solvable_accounts(self):
            if self.refreshes:               # the second refresh is the Ctrl-C
                holding.wait(timeout=5)
                raise KeyboardInterrupt
            return super().solvable_accounts()

    farm = InterruptingFarm(2)
    _, _, args = run_serve(monkeypatch, tmp_path,
                           dict(SETTINGS, round_delay=0.05), farm)
    monkeypatch.setattr(main, "SHUTDOWN_GRACE_SECONDS", 0.2)

    def slow_solve(cookie):
        holding.set()
        time.sleep(30)                       # a worker deep in a poll loop

    monkeypatch.setattr(solver_mod, "solve", slow_solve)

    raised = []

    def run():
        started = time.monotonic()
        try:
            main.serve(*args)
        except KeyboardInterrupt:
            raised.append(time.monotonic() - started)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=15)

    assert raised, "KeyboardInterrupt did not reach the caller"
    assert raised[0] < 5, "Ctrl-C waited on a dispatch instead of giving up"


def test_a_one_shot_run_says_why_the_pool_stopped_early(monkeypatch, tmp_path,
                                                        capsys):
    """The round loop answered health once more after the pool joined. A
    one-shot run has no next refresh to do that, so an outage would exit with a
    screenful of failures and nothing saying what happened — which is exactly
    what a live dibycap 503 produced."""
    from src.solver import SolverError

    farm = FakeFarm(20)
    _, _, args = run_serve(monkeypatch, tmp_path, SETTINGS, farm)
    monkeypatch.setattr(solver_mod, "solve",
                        lambda cookie: (_ for _ in ()).throw(
                            SolverError("dibycap unavailable: HTTP 503")))

    done = threading.Event()
    thread = threading.Thread(target=lambda: (main.serve(*args), done.set()),
                              daemon=True)
    thread.start()
    assert done.wait(timeout=15)

    printed = capsys.readouterr().out
    assert "not answering" in printed, "an outage exited without explaining itself"


def test_a_refresh_says_it_is_fetching_before_the_wait_and_what_it_got_after(
        monkeypatch, tmp_path, capsys):
    """The farm walk is one HTTP call per device and prints nothing of its own —
    minutes of it on a farm of any size, all of it silence directly under the
    Refresh banner, which reads as a hung process. The announcement
    has to land *before* the wait to be worth anything, so the fake farm prints
    a marker from inside the call and the order is asserted rather than the
    presence of the two lines.
    """

    class NoisyFarm(FakeFarm):
        def solvable_accounts(self):
            print("FETCH-ENTERED", flush=True)
            return super().solvable_accounts()

    farm = NoisyFarm(3)
    _, _, args = run_serve(monkeypatch, tmp_path, SETTINGS, farm)
    done = threading.Event()
    thread = threading.Thread(target=lambda: (main.serve(*args), done.set()),
                              daemon=True)
    thread.start()
    assert done.wait(timeout=15)

    printed = capsys.readouterr().out
    assert "fetching accounts from farmsync" in printed
    assert "fetched 3 accounts" in printed
    assert printed.index("fetching accounts from farmsync") \
        < printed.index("FETCH-ENTERED"), "announced the fetch only after it finished"
    assert printed.index("FETCH-ENTERED") < printed.index("fetched 3 accounts")


def test_a_failed_fetch_does_not_claim_it_fetched_anything(
        monkeypatch, tmp_path, capsys):
    """The 'fetched N' line is evidence the walk finished. Printing it on a
    farmsync outage would contradict the error immediately above it."""

    class DeadFarm(FakeFarm):
        def solvable_accounts(self):
            raise main.FarmsyncError("connection reset")

    _, _, args = run_serve(monkeypatch, tmp_path, SETTINGS, DeadFarm(3))
    done = threading.Event()
    thread = threading.Thread(target=lambda: (main.serve(*args), done.set()),
                              daemon=True)
    thread.start()
    assert done.wait(timeout=15)

    printed = capsys.readouterr().out
    assert "fetching accounts from farmsync" in printed
    assert "fetched" not in printed.split("unreachable")[1]


def test_a_daemon_says_when_the_next_refresh_lands(monkeypatch, tmp_path,
                                                   capsys):
    """The gap between refreshes is the second silent stretch. It says the
    workers keep running because they do — `round_delay` is a refresh interval,
    not dead time, and calling it 'waiting' would teach the operator that the
    tool stops between passes."""
    settings = dict(SETTINGS, round_delay=0.05)
    farm = FakeFarm(2)
    health = SolverHealth()
    _, _, args = run_serve(monkeypatch, tmp_path, settings, farm, health)

    thread = threading.Thread(target=lambda: main.serve(*args), daemon=True)
    thread.start()
    deadline = time.time() + 10
    while farm.refreshes < 2 and time.time() < deadline:
        time.sleep(0.02)
    health.halt_reason = "stop"
    thread.join(timeout=10)

    printed = capsys.readouterr().out
    assert "next refresh in 0.05s" in printed
    assert "workers keep running" in printed


def test_a_one_shot_run_promises_no_next_refresh(monkeypatch, tmp_path, capsys):
    """`round_delay <= 0` exits after one pass. Announcing a refresh that will
    never come is the same class of lie as the 'fetched N' line on an outage."""
    _, _, args = run_serve(monkeypatch, tmp_path, SETTINGS, FakeFarm(2))
    done = threading.Event()
    thread = threading.Thread(target=lambda: (main.serve(*args), done.set()),
                              daemon=True)
    thread.start()
    assert done.wait(timeout=15)
    assert "next refresh" not in capsys.readouterr().out


def test_a_one_shot_run_that_finishes_cleanly_explains_nothing(
        monkeypatch, tmp_path, capsys):
    """The explanation is for a pool that stopped itself. Printing it on a
    healthy run would train the operator to ignore it."""
    farm = FakeFarm(2)
    _, _, args = run_serve(monkeypatch, tmp_path, SETTINGS, farm)
    done = threading.Event()
    thread = threading.Thread(target=lambda: (main.serve(*args), done.set()),
                              daemon=True)
    thread.start()
    assert done.wait(timeout=15)
    assert "not answering" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# The canary
# --------------------------------------------------------------------------
#
# Written from a live outage, not from the design. dibycap answered
# `POST /balance` on the first 10-second probe while every `POST /createTask`
# still returned HTTP 503, so the breaker closed, the producer spent 113
# seconds walking farmsync, dispatched 2,148 accounts into the same wall and
# paused again — every cycle, for as long as the outage lasted.

class FakeWorker:
    """A `Roblox` stand-in that reports scripted outcomes and feeds health."""

    def __init__(self, outcomes, health, service_shaped=True):
        self.outcomes = list(outcomes)
        self.health = health
        self.service_shaped = service_shaped
        self.dispatched = []

    def dispatch_one(self, account):
        self.dispatched.append(account)
        outcome = self.outcomes.pop(0)
        # Mirrors `_run_account`: every finished dispatch tells health how it went.
        if outcome == "fail":
            self.health.record_failure(
                RuntimeError("dibycap unavailable: HTTP 503")
                if self.service_shaped else RuntimeError("ACCOUNT_BANNED"))
        else:
            self.health.record_success()
        return outcome


ACCOUNTS = [{"id": n, "cookie": f"c{n}", "username": f"u{n}"} for n in range(5)]


def test_the_canary_clears_the_pass_as_soon_as_one_dispatch_gets_through():
    health = SolverHealth()
    worker = FakeWorker(["fail", "solved", "fail"], health)

    assert main.canary(worker, ACCOUNTS, health) is True
    # Stops at the answer rather than spending the whole allowance on it.
    assert len(worker.dispatched) == 2


def test_the_canary_holds_the_pool_when_every_dispatch_hits_the_service():
    """The bug this exists for: balance says yes, createTask says 503."""
    health = SolverHealth()
    worker = FakeWorker(["fail"] * 3, health)

    assert main.canary(worker, ACCOUNTS, health) is False
    assert len(worker.dispatched) == main.CANARY_ACCOUNTS


def test_three_dead_cookies_are_not_an_outage():
    """Account-shaped failures say nothing about dibycap, so the pass goes on.

    Reading them as an outage would park a healthy farm behind three banned
    accounts, and nothing in the output would explain why.
    """
    health = SolverHealth()
    worker = FakeWorker(["fail"] * 3, health, service_shaped=False)

    assert main.canary(worker, ACCOUNTS, health) is True


def test_the_canary_carries_on_when_it_has_nothing_to_dispatch():
    """The first refresh of a run has no previous pass to borrow accounts from."""
    health = SolverHealth()
    worker = FakeWorker([], health)

    assert main.canary(worker, [], health) is True
    assert worker.dispatched == []


def test_the_canary_stops_the_moment_the_pool_is_halted():
    health = SolverHealth()
    health.halt_reason = "INVALID_API_KEY"
    worker = FakeWorker(["solved"], health)

    assert main.canary(worker, ACCOUNTS, health) is False
    assert worker.dispatched == []


def test_a_good_balance_read_does_not_forgive_the_breaker(monkeypatch):
    """`POST /balance` is not `POST /createTask` and may not speak for it.

    Forgiving the count here wiped the run of service failures every 60
    seconds, which is the other half of why the outage never latched.
    """
    health = SolverHealth()
    for _ in range(FAILURE_THRESHOLD - 1):
        health.record_failure(RuntimeError("dibycap unavailable: HTTP 503"))
    armed = health.consecutive

    monkeypatch.setattr(solver_mod, "status", lambda: dict(STATUS))
    main.poll_credit(health, Depletion(), time.time())

    assert health.consecutive == armed
