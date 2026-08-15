"""What this process has done since it started, and how fast.

The per-round summary answers "what just happened". This answers "is the tool
keeping up", which is the question the operator actually watches — and it is the
one thing a round-shaped summary cannot show, because every round resets it.

Like `dispatch.py` and `credit.py` this module reads no config and calls no
clock: `now` arrives as an argument, so the tests are arithmetic.
"""

import threading

OUTCOMES = ("joined", "solved", "fail")


class Progress:
    """Session-cumulative outcome counts, written by every worker thread."""

    def __init__(self, started_at: float):
        self.started_at = started_at
        self.counts = {outcome: 0 for outcome in OUTCOMES}
        self.queued = 0
        # Its own lock, like `state.py`. The `ThreadLock` singleton guards the
        # per-round `counts` dict; sharing it would serialise a title-bar
        # update against every counter update for no reason.
        self._lock = threading.Lock()

    def start_pass(self, size: int) -> None:
        with self._lock:
            self.queued = size

    def record(self, outcome: str) -> None:
        with self._lock:
            self.counts[outcome] += 1
            # A worker can only ever report an account the producer queued, but
            # a negative count in the title bar would be a nonsense the operator
            # has no way to interpret.
            self.queued = max(0, self.queued - 1)

    @property
    def done(self) -> int:
        return sum(self.counts.values())

    def solves_per_min(self, now: float):
        """Real solves per minute. None until any time has passed.

        Solves rather than dispatches: about half of every pass is free in-grace
        dispatches, so counting those would report a throughput no credit was
        spent on and no captcha was cleared for.
        """
        elapsed = (now - self.started_at) / 60.0
        if elapsed <= 0:
            return None
        return self.counts["solved"] / elapsed

    def spent(self, price_per_1k) -> float:
        """Money spent this session, charged against real solves only.

        A whole sweep of failed and in-grace dispatches was measured never
        moving the balance at all, so billing them here would overstate spend
        several times over.
        """
        return self.counts["solved"] * (price_per_1k or 0) / 1000.0


def title_text(
    progress: Progress, now: float, status: dict = None, depletion=None
) -> str:
    """The one line the terminal title carries.

    Everything in it is already known — no extra call is made to build it.
    """
    parts = [
        "FarmsyncSolver",
        f"queue:{progress.queued} done:{progress.done}",
        f"✓{progress.counts['joined']} ⚡{progress.counts['solved']} "
        f"✗{progress.counts['fail']}",
    ]
    if status:
        parts.append(f"{status.get('estimated_solves') or 0:,} solves")
    eta = depletion.text() if depletion is not None else None
    if eta:
        parts.append(eta)
    rate = progress.solves_per_min(now)
    if rate is not None:
        parts.append(f"{rate:.0f}/min")
    return " | ".join(parts)


def session_line(progress: Progress, now: float, status: dict = None) -> str:
    """The same figures, for the terminal itself rather than its title bar."""
    parts = [
        f"session: {progress.done:,} dispatched  "
        f"{progress.counts['solved']:,} solved  {progress.counts['fail']:,} failed"
    ]
    rate = progress.solves_per_min(now)
    if rate is not None:
        parts.append(f"{rate:.1f} solves/min")
    if status:
        parts.append(f"${progress.spent(status.get('price_per_1k')):.4f} spent")
    return "  |  ".join(parts)
