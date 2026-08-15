import time

import requests

BASE_URL = "https://api.farmsync.cloud"

# Failures worth asking again about, because they are about the wire rather
# than about the request. A dropped connection recovers in seconds; measured
# live, farmsync reset the connection often enough to lose half of all rounds
# when only timeouts were retried.
#
# `requests.ConnectionError` also covers a genuinely dead DNS name, which will
# never recover. Retrying that costs 4 seconds before the round fails anyway —
# a cheap price for not throwing away every other round.
#
# HTTP status errors are deliberately *not* here. A 401 means the token is
# wrong and a 500 means farmsync is broken; neither improves by asking three
# times, and failing fast surfaces them immediately.
RETRYABLE = (requests.Timeout, requests.ConnectionError)


class FarmsyncError(RuntimeError):
    pass


class Farmsync:
    def __init__(self, token: str):
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {token}"
        self._s.trust_env = False

    def _get(self, url: str):
        for attempt in range(3):
            try:
                r = self._s.get(url, timeout=30)
                r.raise_for_status()
                return r
            except RETRYABLE as e:
                if attempt == 2:
                    raise FarmsyncError(str(e) or "timeout")
                time.sleep(2)
            except requests.RequestException as e:
                raise FarmsyncError(str(e))

    def _devices(self) -> list:
        return _as_list(self._get(f"{BASE_URL}/api/devices/").json())

    def _accounts(self, device_id) -> list:
        return _as_list(self._get(f"{BASE_URL}/api/devices/{device_id}/accounts").json())

    def solvable_accounts(self) -> list:
        raw = []
        for d in self._devices():
            if not d.get("is_enabled"):
                continue
            for a in self._accounts(d.get("id")):
                if a.get("enabled") and not a.get("running"):
                    raw.append((a, d))
        raw.sort(key=lambda pair: not pair[0].get("rejoining", False))
        accounts = []
        for a, d in raw:
            cookie = a.get("cookie") or a.get(".ROBLOSECURITY") or ""
            if cookie:
                accounts.append({
                    "username": a.get("username") or "",
                    "cookie": cookie,
                    # farmsync's own account id: 64-hex, unique across the farm,
                    # stable across logins. The key src/state.py rows are stored under.
                    "id": a.get("id"),
                    # The device is only in scope inside the loop above. Carry it
                    # out or nothing downstream can check a heartbeat.
                    "device_id": d.get("id"),
                    # `device_name`, not `name` — `name` is empty on every device.
                    "device_name": d.get("device_name") or "",
                    # epoch ms; 0 when absent, which reads as infinitely stale.
                    # Safe only because the dead-host rule also requires
                    # active_accounts == 0, and a missing count is None.
                    "device_last_updated": d.get("last_updated") or 0,
                    "device_active_accounts": d.get("active_accounts"),
                })
        return accounts


def _as_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []
