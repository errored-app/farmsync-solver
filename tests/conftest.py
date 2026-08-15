"""Shared fixtures and the import-time config shim.

Two modules read configuration the moment they are imported:

  * ``src/util.py``   opens ``input/config.json`` at module scope
  * ``src/solver.py`` binds ``API_KEY = Util.config()["api_key"]`` at module scope

So the suite cannot simply ``import src.solver`` without a config file on disk.
Rather than stub ``src.util`` outright (which would leave ``Util.short`` untested),
we import the *real* module with fake bytes fed to ``open``. The real code runs;
it just never sees the developer's live credentials, and the suite still works on
a fresh clone where ``input/config.json`` is absent because it is gitignored.
"""

import json
import sys
from pathlib import Path
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_CONFIG = {
    "api_key": "TEST_API_KEY",
    "farm_token": "TEST_FARM_TOKEN",
    "threads": 4,
    "round_delay": 0,
}

# Must happen before any test module imports src.solver, which snapshots api_key.
if "src.util" not in sys.modules:
    with patch("builtins.open", mock_open(read_data=json.dumps(FAKE_CONFIG))):
        import src.util  # noqa: F401

import pytest  # noqa: E402

from src import farmsync as farmsync_mod  # noqa: E402
from src import roblox as roblox_mod  # noqa: E402
from src import solver as solver_mod  # noqa: E402


class FakeResponse:
    """Stands in for a requests/curl_cffi response."""

    def __init__(self, payload=None, raise_for_status=None, status_code=200):
        self._payload = payload
        self._raise = raise_for_status
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise


class FakeSession:
    """Session whose get/post replay a scripted queue of responses.

    Queue entries are either a ``FakeResponse`` (returned) or an ``Exception``
    instance (raised). Every call is recorded in ``self.calls``.
    """

    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.headers = {}
        self.trust_env = True

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._script:
            raise AssertionError(f"unscripted {method} to {url}")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    @property
    def remaining(self):
        return len(self._script)


@pytest.fixture
def no_sleep(monkeypatch):
    """Neutralize every backoff/poll sleep so tests run instantly.

    Records the durations that *would* have been slept, so tests can assert on
    backoff behavior without waiting for it.
    """
    slept = []
    monkeypatch.setattr(farmsync_mod.time, "sleep", slept.append)
    monkeypatch.setattr(roblox_mod.time, "sleep", slept.append)
    monkeypatch.setattr(solver_mod, "sleep", slept.append)
    return slept


@pytest.fixture
def account():
    return {"username": "tester", "cookie": "COOKIEVALUE"}
