"""Which failures are worth reacting to, and how the pool reacts to them.

A dispatch can fail for four different reasons and they want four different
answers, so one flat `TERMINAL` tuple cannot express them:

  * **account** — this cookie is dead. Fail it, keep the pool running.
  * **credit**  — the wallet is empty. Park and wait for a top-up; never exit.
  * **service** — dibycap is sick. Stop dispatching and probe until it answers.
  * **global**  — the API key is dead. Nothing recovers without the operator
    editing config, so halt once instead of reproducing the same error against
    every account in the farm.

Separately from that split, some *retryable* failures are still evidence that
the service rather than the account is at fault — timeouts, dropped
connections, 5xx. Those keep their retries but feed the breaker, which is why
`classify` and `is_service_shaped` are two questions rather than one.

Like `dispatch.py` and `credit.py` this module reads no config and calls no
clock: the probe sleep arrives as an argument, so the tests are arithmetic
rather than waiting.
"""

import re
import threading
import time

ACCOUNT_TERMINAL = ("moderated", "cookie dead", "cookie_dead", "banned")
CREDIT_TERMINAL = ("insufficient_balance",)
SERVICE_TERMINAL = ("service_paused",)
GLOBAL_TERMINAL = ("invalid_api_key", "key_disabled", "key_expired")

# Retryable — asking again in a second can genuinely work — but still a
# statement about dibycap rather than about the account. Class names are matched
# as well as messages because curl_cffi raises several of these with an opaque
# string: a host that will not resolve arrives as `DNSError("Failed to perform,
# curl: (6) ...")`, where neither the leaf class name nor the message says
# "connection" anywhere. Its *base classes* do, which is why the whole MRO is
# searched rather than `type(error).__name__` alone. Measured against an
# unreachable host: matching the leaf class read a real outage as twelve
# ordinary account failures.
SERVICE_SHAPED = ("timeout", "timed out", "connection", "curlerror",
                  "bad gateway", "temporarily unavailable")

# `solver` names a 5xx exactly this way. Matching the shape rather than a fixed
# list keeps 507s and the like from being read as account failures.
SERVER_ERROR = re.compile(r"http 5\d\d")

# The wire format is uppercase (`ACCOUNT_BANNED`, `COOKIE_DEAD`) and matching is
# case-insensitive. It runs against the same 120 characters the log line shows,
# so a marker buried further into a payload than that is missed — acceptable for
# every code observed, worth remembering if a new one appears.
DETAIL_CHARS = 120

# Consecutive service-shaped *dispatch* failures before the pool stops. Five is
# reached in seconds across 65 workers during a real outage, and is far out of
# reach of the ordinary background failure rate.
FAILURE_THRESHOLD = 5

PROBE_DELAY_SECONDS = 10
PROBE_DELAY_CAP = 300


def detail_of(error) -> str:
    """The operator-visible text for a failure, truncated the same way everywhere."""
    return str(error)[:DETAIL_CHARS]


def _haystack(error) -> str:
    text = detail_of(error)
    if isinstance(error, BaseException):
        names = " ".join(cls.__name__ for cls in type(error).__mro__)
        text = f"{names} {text}"
    return text.lower()


def classify(error):
    """Which terminal class this failure belongs to, or None if it is retryable.

    Ordered most-consequential first. A message realistically carries one
    marker, but a dead key outranks anything else it might be reported beside.
    """
    hay = _haystack(error)
    for markers, name in ((GLOBAL_TERMINAL, "global"),
                          (CREDIT_TERMINAL, "credit"),
                          (SERVICE_TERMINAL, "service"),
                          (ACCOUNT_TERMINAL, "account")):
        if any(marker in hay for marker in markers):
            return name
    return None


def is_service_shaped(error) -> bool:
    """True when the failure says dibycap is unwell rather than the account."""
    hay = _haystack(error)
    return (classify(error) == "service"
            or any(marker in hay for marker in SERVICE_SHAPED)
            or SERVER_ERROR.search(hay) is not None)


class SolverHealth:
    """Shared across every worker: is dibycap sick, is the wallet empty, is the key dead.

    Counts consecutive service-shaped failures per *dispatch*, not per attempt,
    mirroring `Roblox._record` — a flaky account that recovers on attempt 3 is
    not evidence of an outage. Account-shaped failures neither open the breaker
    nor reset it: a farm full of dead cookies says nothing about the service
    either way. Only a success clears the run.
    """

    def __init__(self, threshold: int = FAILURE_THRESHOLD):
        self.threshold = threshold
        self._lock = threading.Lock()
        self._consecutive = 0
        self.open = False
        self.out_of_credit = False
        self.halt_reason = None

    @property
    def consecutive(self) -> int:
        return self._consecutive

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0

    def record_failure(self, error):
        """Fold one finished dispatch's failure in. Returns its terminal class."""
        kind = classify(error)
        with self._lock:
            if kind == "global":
                # Workers already in flight report the same thing; the operator
                # should read it once.
                if self.halt_reason is None:
                    self.halt_reason = detail_of(error)
            elif kind == "credit":
                self.out_of_credit = True
            elif is_service_shaped(error):
                self._consecutive += 1
                if self._consecutive >= self.threshold:
                    self.open = True
        return kind

    def stopped(self) -> bool:
        """True when workers must stop taking accounts."""
        return self.open or self.out_of_credit or self.halt_reason is not None

    def recover(self, status_fn, sleep_fn=time.sleep, on_error=None,
                first_delay: float = PROBE_DELAY_SECONDS,
                cap: float = PROBE_DELAY_CAP):
        """Probe until dibycap answers again, then close the breaker."""
        status = wait_for_recovery(status_fn, sleep_fn=sleep_fn, on_error=on_error,
                                   first_delay=first_delay, cap=cap)
        self.clear()
        return status

    def clear(self) -> None:
        """Back to healthy, after a top-up or a successful recovery probe.

        `halt_reason` deliberately survives: a rejected API key does not repair
        itself, so forgetting it would put the pool straight back into one
        identical error per account in the farm.
        """
        with self._lock:
            self._consecutive = 0
            self.open = False
            self.out_of_credit = False


def wait_for_recovery(status_fn, sleep_fn=time.sleep, on_error=None,
                      first_delay: float = PROBE_DELAY_SECONDS,
                      cap: float = PROBE_DELAY_CAP):
    """One prober, backing off 10s → 20s → 40s to a five-minute ceiling.

    Never exits and never raises, for the same reason `credit.wait_for_top_up`
    does not: the moment dibycap is unreachable is exactly the moment the daemon
    must stay up.
    """
    delay = first_delay
    while True:
        sleep_fn(delay)
        try:
            return status_fn()
        except Exception as e:
            if on_error is not None:
                on_error(e)
            delay = min(delay * 2, cap)
