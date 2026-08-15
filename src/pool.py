"""The work source for the continuous pool.

Rounds are gone. A producer refreshes farmsync on its own cadence and posts the
survivors of the dispatch filter here; workers claim from here continuously, so
`round_delay` becomes the refresh interval rather than dead time at the end of
every pass. Under the round barrier the whole pool waited for its slowest
worker before anything else could start — and the slow path is a 47-second
median against a 1.8-second fast path, so the tail was long.

Two jobs, and only the second needs code of our own:

  * handing each queued *item* to exactly one worker is `queue.Queue`'s job;
  * not queueing the same *account* twice is ours. An account still being
    solved is still `enabled and not running` in farmsync, so it reappears on
    the very next refresh — roughly every 60 seconds, against dispatches whose
    median is 47 seconds. Without the dedup below the pool would hand the same
    account to a second worker and buy its captcha twice.

Like `dispatch.py` and `progress.py` this module reads no config and calls no
clock; the claim timeout arrives as an argument.
"""

import queue
import threading

# How long a worker waits for work before looping. It is a re-check interval,
# not a tuning knob: the loop exists so a worker notices the health breaker and
# a closed queue, and blocking forever would leave a halted pool with 65
# threads parked inside `Queue.get`.
CLAIM_TIMEOUT = 0.25


class WorkQueue:
    """Accounts waiting for a worker, plus the ones a worker already holds."""

    def __init__(self):
        self._q = queue.Queue()
        self._waiting = set()      # posted, not yet claimed
        self._inflight = set()     # claimed, not yet released
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, accounts) -> int:
        """Post accounts, skipping any already waiting or in flight.

        Returns how many were actually posted, which is what the producer logs
        — the difference between that and what the filter passed is the work
        the previous refresh has not finished yet.

        An account with no `id` is always posted. `id` is the only stable key
        (see the design note on `cookie` and `username`), and folding every
        id-less record onto one bucket would silently drop all but the first.
        """
        posted = 0
        for account in accounts:
            key = account.get("id")
            if key is not None:
                with self._lock:
                    if key in self._waiting or key in self._inflight:
                        continue
                    self._waiting.add(key)
            self._q.put(account)
            posted += 1
        return posted

    def claim(self, timeout: float = CLAIM_TIMEOUT):
        """The next account for this worker, or None if none arrived in time.

        None does not mean "no more work" — see `drained` for that. A worker
        that treats it as an exit condition ends a daemon run the first time a
        refresh is slower than the pool.
        """
        try:
            account = self._q.get(timeout=timeout) if timeout else self._q.get_nowait()
        except queue.Empty:
            return None
        key = account.get("id")
        if key is not None:
            with self._lock:
                self._waiting.discard(key)
                self._inflight.add(key)
        return account

    def release(self, account) -> None:
        """This dispatch is finished; the account may be queued again later.

        Every claim must reach this, failures included. A claim that is never
        released leaves the account suppressed for the life of the process, and
        nothing in the output would say so — the account simply stops appearing.
        """
        key = account.get("id")
        if key is not None:
            with self._lock:
                self._inflight.discard(key)

    def close(self) -> None:
        """No further refreshes. Workers drain what is queued and then exit."""
        with self._lock:
            self._closed = True

    def drained(self) -> bool:
        """True only when the producer is finished *and* nothing is waiting.

        This is what ends a one-shot run (`round_delay <= 0`): one refresh, one
        close, workers drain and the process exits. A persistent pool has no
        natural end, so without this the documented one-shot mode would
        silently become a daemon.
        """
        return self._closed and self._q.empty()

    @property
    def pending(self) -> int:
        """Accounts waiting for a worker. Approximate under concurrent claims."""
        return self._q.qsize()

    @property
    def inflight(self) -> int:
        with self._lock:
            return len(self._inflight)


def of(accounts) -> WorkQueue:
    """A queue holding exactly these accounts and nothing more.

    One refresh, then closed — which is both what one-shot mode does and what a
    test wants when it is asserting a worker's behaviour rather than the
    producer's.
    """
    work = WorkQueue()
    work.submit(accounts)
    work.close()
    return work
