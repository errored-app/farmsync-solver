import threading

import pytest

from conftest import FakeResponse, FakeSession
from src import solver as solver_mod
from src.solver import FIRST_POLL_SECONDS, POLL_ATTEMPTS, SolverError, solve, status

COOKIE = ".ROBLOSECURITY=abc"

# The field set the endpoint really returns; the numbers are invented. What is
# under test is which fields survive `status()`, never what they add up to, so
# a placeholder wallet asserts exactly as much as a real one.
BALANCE = {"active": 0, "balance": 5.6789, "estimated_solves": 1234,
           "max_concurrent": 65, "price_per_1k": 1.5, "success": True, "type": "limited"}


def _reset_sessions(monkeypatch):
    """Clear the per-thread session cache so each test starts cold."""
    monkeypatch.setattr(solver_mod, "_local", threading.local(), raising=False)


def install_session(monkeypatch, script):
    """Point solver at a scripted session instead of curl_cffi."""
    session = FakeSession(script)
    monkeypatch.setattr(solver_mod, "requests", type("R", (), {"Session": lambda: session}))
    _reset_sessions(monkeypatch)
    return session


def install_counting_sessions(monkeypatch, script_for):
    """Hand out a fresh scripted session per construction, recording each one."""
    made = []

    def Session():
        session = FakeSession(script_for())
        made.append(session)
        return session

    monkeypatch.setattr(solver_mod, "requests",
                        type("R", (), {"Session": staticmethod(Session)}))
    _reset_sessions(monkeypatch)
    return made


def created(task_id="t1"):
    return FakeResponse({"task_id": task_id})


def solve_script(n=1):
    """Enough scripted responses for `n` back-to-back successful solves."""
    script = []
    for i in range(n):
        script += [created(f"t{i}"), FakeResponse({"success": True, "timings": {}})]
    return script


def test_api_key_snapshotted_at_import_is_the_fake_one():
    assert solver_mod.API_KEY == "TEST_API_KEY"


def test_returns_timings_on_immediate_success(monkeypatch, no_sleep):
    install_session(monkeypatch, [
        created(),
        FakeResponse({"success": True, "timings": {"total_ms": 1500, "solve_ms": 900}}),
    ])
    assert solve(COOKIE) == {"total_ms": 1500, "solve_ms": 900}


def test_sends_the_api_key_header_and_cookie(monkeypatch, no_sleep):
    session = install_session(monkeypatch, [
        created(),
        FakeResponse({"success": True, "timings": {}}),
    ])
    solve(COOKIE)
    _, url, kwargs = session.calls[0]
    assert url.endswith("/createTask")
    assert kwargs["headers"] == {"X-API-Key": "TEST_API_KEY"}
    assert kwargs["json"] == {"cookie": COOKIE}


def test_missing_task_id_raises_with_the_server_error(monkeypatch, no_sleep):
    install_session(monkeypatch, [FakeResponse({"error": "invalid_api_key"})])
    with pytest.raises(SolverError, match="invalid_api_key"):
        solve(COOKIE)


def test_missing_task_id_falls_back_through_message_then_generic(monkeypatch, no_sleep):
    install_session(monkeypatch, [FakeResponse({"message": "insufficient_balance"})])
    with pytest.raises(SolverError, match="insufficient_balance"):
        solve(COOKIE)

    install_session(monkeypatch, [FakeResponse({})])
    with pytest.raises(SolverError, match="createTask failed"):
        solve(COOKIE)


@pytest.mark.parametrize("status", ["pending", "solving", "processing"])
def test_polls_until_terminal_status(monkeypatch, no_sleep, status):
    install_session(monkeypatch, [
        created(),
        FakeResponse({"status": status, "retry_after_ms": 500}),
        FakeResponse({"success": True, "timings": {"total_ms": 3000, "solve_ms": 0}}),
    ])
    assert solve(COOKIE) == {"total_ms": 3000, "solve_ms": 0}
    assert no_sleep == [FIRST_POLL_SECONDS]


def test_the_first_poll_comes_early_rather_than_at_the_servers_hint(monkeypatch, no_sleep):
    """The fast path is *always* exactly two polls, and the server reports a
    `total_ms` of 240-490ms while its `retry_after_ms` asks for 1500. Waiting
    the full hint burns ~1.2s of a worker slot per in-grace dispatch for
    nothing, and in-grace dispatches are about half of every pass.
    """
    install_session(monkeypatch, [
        created(),
        FakeResponse({"status": "pending", "retry_after_ms": 1500}),
        FakeResponse({"success": True, "timings": {"total_ms": 400, "solve_ms": 0}}),
    ])
    solve(COOKIE)
    assert no_sleep == [0.3]


def test_polls_after_the_first_honour_the_servers_hint(monkeypatch, no_sleep):
    """Still pending at 300ms means this is the slow path — a real solve, median
    47s. Polling it every 300ms would be 150 pointless round trips, so the early
    poll is spent once and the server's cadence takes over.
    """
    install_session(monkeypatch, [
        created(),
        FakeResponse({"status": "pending", "retry_after_ms": 1500}),
        FakeResponse({"status": "pending", "retry_after_ms": 1500}),
        FakeResponse({"success": True, "timings": {}}),
    ])
    solve(COOKIE)
    assert no_sleep == [0.3, 1.5]


def test_the_first_poll_never_waits_longer_than_the_server_asked(monkeypatch, no_sleep):
    """300ms is a ceiling on the first wait, not a floor under it."""
    install_session(monkeypatch, [
        created(),
        FakeResponse({"status": "pending", "retry_after_ms": 250}),
        FakeResponse({"success": True, "timings": {}}),
    ])
    solve(COOKIE)
    assert no_sleep == [0.25]


def test_honors_server_retry_after_with_a_floor(monkeypatch, no_sleep):
    """A server asking for 50ms is held to the 200ms floor; no busy-spin."""
    install_session(monkeypatch, [
        created(),
        FakeResponse({"status": "pending", "retry_after_ms": 50}),
        FakeResponse({"status": "pending"}),  # absent -> DEFAULT_RETRY_MS
        FakeResponse({"success": True, "timings": {}}),
    ])
    solve(COOKIE)
    assert no_sleep == [0.2, 1.0]


def test_unsuccessful_result_raises(monkeypatch, no_sleep):
    install_session(monkeypatch, [
        created(),
        FakeResponse({"success": False, "error": "cookie dead"}),
    ])
    with pytest.raises(SolverError, match="cookie dead"):
        solve(COOKIE)


def test_success_without_timings_returns_empty_dict(monkeypatch, no_sleep):
    install_session(monkeypatch, [created(), FakeResponse({"success": True})])
    assert solve(COOKIE) == {}


def test_gives_up_after_poll_attempts(monkeypatch, no_sleep):
    session = install_session(monkeypatch, [created()] +
                              [FakeResponse({"status": "pending"})] * POLL_ATTEMPTS)
    with pytest.raises(SolverError, match="timeout"):
        solve(COOKIE)
    assert len(session.calls) == POLL_ATTEMPTS + 1


# --- status() -----------------------------------------------------------


def test_status_returns_the_documented_fields(monkeypatch):
    install_session(monkeypatch, [FakeResponse(BALANCE)])
    assert status() == {"active": 0, "balance": 5.6789, "estimated_solves": 1234,
                        "max_concurrent": 65, "price_per_1k": 1.5}


def test_status_posts_to_balance_with_the_api_key(monkeypatch):
    """POST, not GET — every GET path on this endpoint 404s."""
    session = install_session(monkeypatch, [FakeResponse(BALANCE)])
    status()
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/balance")
    assert kwargs["headers"] == {"X-API-Key": "TEST_API_KEY"}


def test_status_raises_when_the_service_reports_failure(monkeypatch):
    install_session(monkeypatch, [FakeResponse({"success": False, "error": "invalid_api_key"})])
    with pytest.raises(SolverError, match="invalid_api_key"):
        status()


def test_status_raises_on_a_payload_with_no_error_text(monkeypatch):
    install_session(monkeypatch, [FakeResponse({})])
    with pytest.raises(SolverError, match="balance check failed"):
        status()


# --- server errors ------------------------------------------------------


@pytest.mark.parametrize("code", [500, 502, 503])
def test_a_server_error_is_named_rather_than_left_to_fail_parsing(monkeypatch, code):
    """A 5xx carries an HTML error page. Parsing it produces a decode error the
    circuit breaker cannot recognise as dibycap being down."""
    install_session(monkeypatch, [FakeResponse(None, status_code=code)])
    with pytest.raises(SolverError, match=f"HTTP {code}"):
        status()


def test_a_client_error_still_reads_the_json_error_body(monkeypatch, no_sleep):
    """dibycap answers a bad key with 4xx and a useful JSON body; keep reading it."""
    install_session(monkeypatch, [
        FakeResponse({"error": "invalid_api_key"}, status_code=401)])
    with pytest.raises(SolverError, match="invalid_api_key"):
        solve(COOKIE)


def test_a_server_error_mid_poll_is_named_too(monkeypatch, no_sleep):
    install_session(monkeypatch, [created(), FakeResponse(None, status_code=503)])
    with pytest.raises(SolverError, match="HTTP 503"):
        solve(COOKIE)


# --- session reuse ------------------------------------------------------


def test_one_thread_reuses_a_single_session(monkeypatch, no_sleep):
    """Two dispatches on one worker must not build two curl handles."""
    made = install_counting_sessions(monkeypatch, lambda: solve_script(2))
    solve(COOKIE)
    solve(COOKIE)
    assert len(made) == 1
    assert len(made[0].calls) == 4


def test_each_thread_gets_its_own_session(monkeypatch, no_sleep):
    """curl_cffi sessions are not safe to share, so the cache is per thread."""
    made = install_counting_sessions(monkeypatch, lambda: solve_script(1))
    workers = [threading.Thread(target=solve, args=(COOKIE,)) for _ in range(3)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert len(made) == 3


def test_status_shares_the_session_with_solve_on_the_same_thread(monkeypatch, no_sleep):
    made = install_counting_sessions(
        monkeypatch, lambda: [FakeResponse(BALANCE)] + solve_script(1))
    status()
    solve(COOKIE)
    assert len(made) == 1
