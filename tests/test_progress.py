"""Session-cumulative totals and the throughput they imply.

Pure arithmetic against an injected clock, in the same style as `test_credit.py`
and `test_dispatch.py`: nothing here sleeps and nothing here patches `time`.
"""

import threading

from src.progress import Progress, title_text

STATUS = {"estimated_solves": 1234, "balance": 5.6789, "price_per_1k": 1.5}


def filled(started_at=0.0, joined=0, solved=0, fail=0):
    progress = Progress(started_at)
    for outcome, n in (("joined", joined), ("solved", solved), ("fail", fail)):
        for _ in range(n):
            progress.record(outcome)
    return progress


def test_done_is_every_dispatch_whatever_it_returned():
    assert filled(joined=380, solved=31, fail=1).done == 412


def test_a_pass_reports_how_much_of_its_queue_is_left():
    progress = Progress(0.0)
    progress.start_pass(3)
    assert progress.queued == 3
    progress.record("solved")
    assert progress.queued == 2


def test_a_new_pass_resets_the_queue_but_not_the_session_totals():
    progress = filled(solved=5)
    progress.start_pass(10)
    progress.record("solved")
    assert progress.queued == 9
    assert progress.counts["solved"] == 6


def test_the_queue_never_reads_below_zero():
    """A worker recording an outcome the producer never queued must not print
    a negative count in the title bar."""
    progress = Progress(0.0)
    progress.start_pass(1)
    progress.record("fail")
    progress.record("fail")
    assert progress.queued == 0


def test_throughput_counts_real_solves_per_minute():
    """Solves, not dispatches. A pass is mostly free in-grace dispatches, so
    counting those would report a throughput that no credit was spent on."""
    progress = filled(joined=100, solved=30, fail=10)
    assert progress.solves_per_min(now=120.0) == 15.0


def test_throughput_is_silent_before_any_time_has_passed():
    assert filled(solved=5).solves_per_min(now=0.0) is None


def test_cost_is_charged_against_real_solves_only():
    """Failed and in-grace dispatches were measured never moving the balance
    at all, so counting them would overstate spend fourfold."""
    progress = filled(joined=175, solved=20, fail=202)
    assert progress.spent(price_per_1k=1.5) == 0.03


def test_cost_is_zero_when_the_price_is_unknown():
    assert filled(solved=20).spent(price_per_1k=None) == 0.0


def test_every_worker_thread_is_counted():
    """65 workers share one Progress; an unlocked read-modify-write would lose
    dispatches silently and only under load."""
    progress = Progress(0.0)

    def burst():
        for _ in range(200):
            progress.record("solved")

    workers = [threading.Thread(target=burst) for _ in range(8)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert progress.counts["solved"] == 1600


# --- title text ---------------------------------------------------------


def test_the_title_carries_the_pass_the_session_and_the_credit():
    progress = filled(joined=380, solved=31, fail=1)
    progress.start_pass(670)
    text = title_text(progress, now=60.0, status=STATUS)
    assert text == ("FarmsyncSolver | queue:670 done:412 | ✓380 ⚡31 ✗1 "
                    "| 1,234 solves | 31/min")


def test_the_title_drops_credit_it_has_not_read_yet():
    """The first round runs before the first status poll can fail or succeed."""
    progress = filled(solved=1)
    progress.start_pass(5)
    assert title_text(progress, now=60.0, status=None) == (
        "FarmsyncSolver | queue:5 done:1 | ✓0 ⚡1 ✗0 | 1/min")


def test_the_title_carries_the_depletion_eta_when_there_is_one():
    class Eta:
        def text(self):
            return "~7h 50m left"

    text = title_text(filled(solved=1), now=60.0, status=STATUS, depletion=Eta())
    assert "~7h 50m left" in text
