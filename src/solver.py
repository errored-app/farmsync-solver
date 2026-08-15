import threading
from time import sleep

from curl_cffi import requests

from .util import Util

API_KEY = Util.config()["api_key"]
API_URL = "https://api.dibycap.com"
POLL_ATTEMPTS = 180
DEFAULT_RETRY_MS = 1000
# A ceiling on the *first* wait only. The server asks for 1500ms but finishes
# the fast path in 240-490ms every time, so the first poll is spent early and
# every poll after it follows the server's own cadence.
FIRST_POLL_SECONDS = 0.3
STATUS_FIELDS = ("estimated_solves", "balance", "active", "max_concurrent", "price_per_1k")

# One session per worker thread, not one per dispatch and not one for the
# process. A curl_cffi session wraps a libcurl handle, which is not safe to
# share across the 65 threads this pool runs; keeping it thread-local still
# means the connection is established 65 times per process instead of once per
# account.
_local = threading.local()


class SolverError(RuntimeError):
    pass


def _session():
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session


def _json(resp):
    """Parse a response, naming a 5xx instead of tripping over its HTML body.

    dibycap returns an error page rather than JSON when it is down, and
    `.json()` on that raises a decode error the circuit breaker cannot
    recognise as an outage. 4xx bodies are left alone — a rejected key arrives
    as JSON with a useful `error` field.
    """
    if resp.status_code >= 500:
        raise SolverError(f"dibycap unavailable: HTTP {resp.status_code}")
    return resp.json()


def status() -> dict:
    """Read dibycap's credit and plan limits.

    One call serves four purposes: credit readout, liveness probe, pool sizing,
    and an independent check on our own concurrency accounting.
    """
    resp = _json(_session().post(f"{API_URL}/balance",
                                 headers={"X-API-Key": API_KEY}, timeout=15))
    if not resp.get("success"):
        raise SolverError(resp.get("error") or resp.get("message") or "balance check failed")
    return {field: resp.get(field) for field in STATUS_FIELDS}


def solve(cookie: str) -> dict:
    session = _session()
    headers = {"X-API-Key": API_KEY}

    resp = _json(session.post(f"{API_URL}/createTask", json={"cookie": cookie},
                              headers=headers, timeout=15))
    task_id = resp.get("task_id")
    if not task_id:
        raise SolverError(resp.get("error") or resp.get("message") or "createTask failed")

    polls = 0
    for _ in range(POLL_ATTEMPTS):
        result = _json(session.post(f"{API_URL}/getTask", json={"task_id": task_id},
                                    headers=headers, timeout=15))
        if result.get("status") in ("pending", "solving", "processing"):
            wait = max(0.2, (result.get("retry_after_ms") or DEFAULT_RETRY_MS) / 1000)
            polls += 1
            if polls == 1:
                wait = min(wait, FIRST_POLL_SECONDS)
            sleep(wait)
            continue
        if not result.get("success"):
            raise SolverError(result.get("error") or "solve failed")
        return result.get("timings") or {}

    raise SolverError("timeout")
