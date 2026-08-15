"""The work source that replaces the round barrier.

Two properties here are load-bearing and silent when broken. An account handed
to two workers at once is solved twice and billed twice; an account that is
claimed and never released is suppressed for the life of the process. Neither
shows up in a log line, so both are asserted directly — including under real
thread contention, because the dedup set is ours rather than the standard
library's.
"""

import threading

from src.pool import CLAIM_TIMEOUT, WorkQueue


def account(id=None, name="u"):
    return {"id": id, "username": name, "cookie": f"c-{name}"}


def test_submitted_accounts_come_back_in_the_order_they_were_posted():
    work = WorkQueue()
    work.submit([account("a"), account("b")])
    assert work.claim()["id"] == "a"
    assert work.claim()["id"] == "b"


def test_submit_reports_how_many_it_actually_posted():
    work = WorkQueue()
    assert work.submit([account("a"), account("b")]) == 2


def test_an_account_already_waiting_is_not_queued_a_second_time():
    """The producer re-reads the same farm every refresh, so the same account
    arrives again long before a worker has reached the first copy."""
    work = WorkQueue()
    work.submit([account("a")])
    assert work.submit([account("a")]) == 0
    assert work.claim() is not None
    assert work.claim(timeout=0) is None


def test_an_account_still_being_solved_is_not_handed_to_a_second_worker():
    """An account in flight is still `enabled and not running` in farmsync, so
    it comes back on the very next refresh while a worker still holds it."""
    work = WorkQueue()
    work.submit([account("a")])
    work.claim()
    assert work.submit([account("a")]) == 0


def test_a_released_account_is_eligible_again_on_the_next_refresh():
    """Release has to clear the claim, or one dispatch suppresses an account
    for the life of the process and nothing ever says so."""
    work = WorkQueue()
    work.submit([account("a")])
    work.release(work.claim())
    assert work.submit([account("a")]) == 1


def test_accounts_carrying_no_id_are_never_deduplicated():
    """`id` is the only stable key. Folding every id-less record onto one bucket
    would silently drop all but the first of them."""
    work = WorkQueue()
    assert work.submit([account(None, "x"), account(None, "y")]) == 2


def test_claim_gives_up_rather_than_blocking_forever():
    """Workers re-check the health breaker between claims, so a claim on an
    empty queue has to return rather than park a thread indefinitely."""
    work = WorkQueue()
    assert work.claim(timeout=0) is None


def test_an_open_queue_is_never_drained_even_when_empty():
    """Daemon mode: an empty queue means the producer has not refreshed yet,
    not that there is no more work."""
    work = WorkQueue()
    assert work.drained() is False


def test_a_closed_and_empty_queue_is_drained():
    """This is what ends a one-shot run: `round_delay <= 0` closes the queue
    and every worker exits once it is empty."""
    work = WorkQueue()
    work.close()
    assert work.drained() is True


def test_closing_still_lets_workers_finish_what_is_already_queued():
    work = WorkQueue()
    work.submit([account("a")])
    work.close()
    assert work.drained() is False
    assert work.claim()["id"] == "a"
    assert work.drained() is True


def test_pending_counts_what_is_waiting_for_a_worker():
    work = WorkQueue()
    work.submit([account("a"), account("b")])
    work.claim()
    assert work.pending == 1


def test_every_account_is_claimed_exactly_once_under_contention():
    """The guarantee the whole design rests on: no account is solved twice and
    none is skipped, while the producer keeps re-posting the same farm.

    The duplicate submissions are the point. `queue.Queue` already hands each
    *item* to one worker; what it cannot do is stop the producer putting the
    same account in twice, which is the bug this class exists to prevent.

    Nothing is released here on purpose: a released account is *meant* to be
    eligible again on the next refresh, so releasing would make the count
    legitimately exceed 60 and the assertion would stop meaning anything.
    """
    work = WorkQueue()
    total = 60
    workers = 8
    accounts = [account(f"a{i}", f"u{i}") for i in range(total)]
    start = threading.Barrier(workers + 1)
    claimed = [[] for _ in range(workers)]

    def worker(slot):
        start.wait()  # maximize overlap; no thread gets a head start
        while not work.drained():
            got = work.claim(timeout=CLAIM_TIMEOUT)
            if got is None:
                continue
            claimed[slot].append(got["id"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    start.wait()
    for _ in range(4):          # the producer, refreshing the same farm
        work.submit(accounts)
    work.close()
    for t in threads:
        t.join()

    got = sorted(i for slot in claimed for i in slot)
    assert got == sorted(a["id"] for a in accounts), \
        "an account was claimed twice or skipped"
