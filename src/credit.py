"""Solving credit: how much is left, how fast it is going, and what to do at zero.

The operator treats *solves* as the depleting resource and money as how it is
topped up, so solves is the primary display unit everywhere here.

Nothing in this module reads config or calls the clock. Every timestamp, every
sleep, and every HTTP session arrives as an argument, which is what lets the
tests assert both sides of a boundary by arithmetic instead of by waiting.
"""

import time

import requests

ALERT_BELOW_SOLVES = 5000
STATUS_POLL_SECONDS = 60

# Re-arm only once credit is clear of the threshold by a margin, so a balance
# hovering on the boundary cannot spam the webhook.
REARM_MULTIPLIER = 1.1

# Weight of the newest interval in the burn-rate average. Low enough that one
# idle poll does not erase an hour of history.
ALPHA = 0.3
MIN_SAMPLES = 2
MIN_HISTORY_SECONDS = 300

ALERT_COLOR = 0xE06C5A
PARKED_COLOR = 0xC0392B


def has_credit(status: dict) -> bool:
    return ((status or {}).get("estimated_solves") or 0) > 0


def _humanize(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes >= 1440:
        days, rest = divmod(minutes, 1440)
        return f"~{days}d {rest // 60}h left"
    if minutes >= 60:
        hours, rest = divmod(minutes, 60)
        return f"~{hours}h {rest}m left"
    return f"~{minutes}m left"


class Depletion:
    """Exponentially weighted burn rate in solves per wall-clock hour."""

    def __init__(self, alpha: float = ALPHA, min_samples: int = MIN_SAMPLES,
                 min_history_seconds: float = MIN_HISTORY_SECONDS):
        self.alpha = alpha
        self.min_samples = min_samples
        self.min_history_seconds = min_history_seconds
        self._first_at = None
        self._last_at = None
        self._last_solves = None
        self._samples = 0
        self._ewma = None

    def sample(self, solves: int, now: float) -> None:
        if self._last_at is None:
            self._first_at = self._last_at = now
            self._last_solves = solves
            self._samples = 1
            return

        elapsed = now - self._last_at
        if elapsed <= 0:
            return

        consumed = self._last_solves - solves
        self._last_at, self._last_solves = now, solves
        self._samples += 1

        # Credit going *up* is the operator topping up, not negative
        # consumption. Re-anchor on the new balance but leave the rate alone.
        if consumed < 0:
            return

        instant = consumed / (elapsed / 3600.0)
        self._ewma = instant if self._ewma is None else \
            self.alpha * instant + (1 - self.alpha) * self._ewma

    def rate_per_hour(self):
        if self._ewma is None or self._samples < self.min_samples:
            return None
        if (self._last_at - self._first_at) < self.min_history_seconds:
            return None
        return self._ewma

    def eta_seconds(self):
        rate = self.rate_per_hour()
        if not rate or rate <= 0:
            return None
        return self._last_solves / rate * 3600.0

    def text(self):
        eta = self.eta_seconds()
        return None if eta is None else _humanize(eta)


def credit_line(status: dict, depletion: Depletion) -> str:
    """One-line credit readout for the terminal."""
    parts = [f"{status.get('estimated_solves') or 0:,} solves  ${status.get('balance') or 0:.2f}"]
    rate = depletion.rate_per_hour()
    if rate:
        parts.append(f"{rate:,.0f}/h")
    eta = depletion.text()
    if eta:
        parts.append(eta)
    return "  |  ".join(parts)


class CreditAlerter:
    """Posts a Discord embed when solving credit runs low, and when it runs out.

    A webhook failure is logged and swallowed — it must never interrupt
    solving — and leaves the alert armed so the next poll tries again.
    """

    def __init__(self, webhook_url, threshold: int = ALERT_BELOW_SOLVES,
                 session=None, on_error=None):
        self.webhook_url = webhook_url or ""
        self.threshold = threshold
        self.on_error = on_error
        self._session = session
        self._armed = True

    @property
    def enabled(self) -> bool:
        """An untouched `config.example.json` must behave exactly like no webhook."""
        return bool(self.webhook_url) and "REPLACE" not in self.webhook_url

    def check(self, status: dict, depletion: Depletion = None, pool: str = None) -> bool:
        """Alert on a crossing below the threshold. Returns True if it posted."""
        solves = status.get("estimated_solves") or 0
        if solves > self.threshold * REARM_MULTIPLIER:
            self._armed = True
            return False
        if solves >= self.threshold or not self._armed:
            return False

        sent = self._send("FarmsyncSolver: solving credit low", ALERT_COLOR,
                          status, depletion, pool)
        # Only a delivered alert disarms; a failed POST retries on the next poll.
        self._armed = not sent
        return sent

    def notify_parked(self, status: dict, pool: str = None) -> bool:
        """Announce the parked state, ignoring the armed flag.

        Credit reaches zero precisely when nobody is watching the terminal, and
        the low-credit alert has almost always fired and disarmed long before.
        """
        return self._send("FarmsyncSolver: parked, out of credit", PARKED_COLOR,
                          status, None, pool)

    def _send(self, title: str, color: int, status: dict,
              depletion: Depletion, pool: str) -> bool:
        if not self.enabled:
            return False

        solves = status.get("estimated_solves") or 0
        fields = [{"name": "Solves remaining", "value": f"{solves:,}", "inline": True}]
        rate = depletion.rate_per_hour() if depletion is not None else None
        if rate:
            fields.append({"name": "Burn rate", "value": f"{rate:,.0f}/h", "inline": True})
        eta = depletion.text() if depletion is not None else None
        if eta:
            fields.append({"name": "Runs out in", "value": eta, "inline": True})
        if pool:
            fields.append({"name": "Pool", "value": pool, "inline": False})

        embed = {"title": title, "color": color,
                 "description": f"${status.get('balance') or 0:.2f} remaining",
                 "fields": fields}
        try:
            self._post({"embeds": [embed]})
        except Exception as e:
            if self.on_error is not None:
                self.on_error(e)
            return False
        return True

    def _post(self, payload: dict) -> None:
        if self._session is None:
            self._session = requests.Session()
        resp = self._session.post(self.webhook_url, json=payload, timeout=10)
        # Discord answers a wrong webhook with 404, not a connection error, so
        # without this a typo'd URL reports success on every poll forever and
        # the operator never learns the alert is dead.
        resp.raise_for_status()


def wait_for_top_up(status_fn, poll_seconds: float = STATUS_POLL_SECONDS,
                    sleep_fn=time.sleep, on_error=None) -> dict:
    """Block until dibycap reports credit again, then return that status.

    Never exits and never raises. dibycap being unreachable while we sit at
    zero is exactly the moment the daemon must not die, so a failing status
    call is reported and the wait continues.
    """
    while True:
        sleep_fn(poll_seconds)
        try:
            latest = status_fn()
        except Exception as e:
            if on_error is not None:
                on_error(e)
            continue
        if has_credit(latest):
            return latest
