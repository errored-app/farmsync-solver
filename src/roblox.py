import time

from . import health as health_mod
from . import solver
from .health import SolverHealth
from .output import Output
from .util import Util

MAX_ATTEMPTS = 3
BACKOFF_CAP = 2


class RobloxError(RuntimeError):
    pass


class Roblox:
    def __init__(self, lock, source, counts, state=None, health=None,
                 on_dispatch=None):
        self.lock = lock
        # A `pool.WorkQueue`. The worker no longer owns a slice of a round's
        # account list — it lives as long as the process and claims whatever the
        # producer has posted, which is what removes the round barrier.
        self.source = source
        self.counts = counts
        self.state = state
        # Fires once per dispatch, so the terminal title moves while a pass is
        # running instead of only between passes.
        self.on_dispatch = on_dispatch
        # A worker given no shared health object gets a private one, so the
        # breaker still short-circuits its own run and nothing needs a None check.
        self.health = health if health is not None else SolverHealth()
        self._account = {}

    def check(self):
        # `stopped` is checked first so a halted pool does not claim an account
        # on its way out — and it is re-checked every `CLAIM_TIMEOUT`, which is
        # the only reason `claim` has a timeout at all.
        while not self.health.stopped():
            self._account = self.source.claim()
            if self._account is None:
                # No work *right now* is not the same as no work left. Only a
                # closed and empty queue ends the worker, which is what makes
                # `round_delay <= 0` still exit and a daemon run still wait.
                if self.source.drained():
                    return
                continue
            try:
                self._run_account()
            finally:
                # Unconditional: a claim that is never released leaves the
                # account permanently invisible to the producer's dedup, and
                # nothing in the output would say so.
                self.source.release(self._account)

    def dispatch_one(self, account) -> str:
        """One dispatch on the caller's thread, off the queue, returning its outcome.

        The canary in `main` needs to know whether dibycap will actually solve
        before it spends two minutes walking farmsync, and the only honest way
        to ask is to dispatch. Deliberately not `claim`/`release`: the account
        never enters the queue, so there is no claim to leak — and every other
        rule still applies, because this runs the same `_run_account` the pool
        does. The counters, the database and the title bar all see it as the
        ordinary dispatch it is.
        """
        self._account = account
        return self._run_account()

    def _user(self) -> str:
        return self._account["username"] or Util.short(self._account["cookie"])

    def _run_account(self):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                outcome, detail = self._solve()
                self.health.record_success()
                return self._record(outcome, detail)
            except Exception as e:
                error = e
            # All four terminal classes short-circuit the retries; they differ
            # only in what `record_failure` then does to the rest of the pool.
            if health_mod.classify(error) or attempt == MAX_ATTEMPTS:
                self.health.record_failure(error)
                return self._record("fail", health_mod.detail_of(error))
            time.sleep(min(2 ** (attempt - 1), BACKOFF_CAP))

    def _record(self, outcome: str, detail: str):
        """One dispatch, one place that touches the counters and the database.

        Reached once per *dispatch*, after `_run_account` has spent all its
        internal attempts, so a flaky account that recovers on attempt 3 never
        registers a failure at all.

        Returns the outcome so `dispatch_one` can hand it back to the canary.
        The pool itself ignores the return — it reads the shared counters.
        """
        with self.lock.get_lock():
            self.counts[outcome] += 1
        if self.state is not None:
            self.state.record(self._account.get("id"), outcome, detail, time.time())
        if self.on_dispatch is not None:
            self.on_dispatch(outcome)
        Output.result(self._user(), outcome, detail)
        return outcome

    def _solve(self) -> tuple:
        timings = solver.solve(f".ROBLOSECURITY={self._account['cookie']}")
        total = (timings.get("total_ms") or 0) / 1000
        if (timings.get("solve_ms") or 0) > 0:
            return "solved", f"Solved Captcha in {total:.1f}s"
        return "joined", f"Joined in {total:.1f}s"
