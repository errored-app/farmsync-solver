import pytest
import requests

from conftest import FakeResponse, FakeSession
from src.farmsync import Farmsync, FarmsyncError, _as_list


def make_farm(script):
    farm = Farmsync("TOKEN")
    farm._s = FakeSession(script)
    return farm


# --------------------------------------------------------------------------
# _as_list — tolerates both bare-list and envelope-object responses
# --------------------------------------------------------------------------

def test_as_list_passes_through_a_bare_list():
    assert _as_list([{"id": 1}]) == [{"id": 1}]


def test_as_list_unwraps_the_first_list_in_an_envelope():
    assert _as_list({"count": 2, "results": [{"id": 1}]}) == [{"id": 1}]


def test_as_list_returns_empty_for_shapes_it_cannot_read():
    assert _as_list({"count": 0}) == []
    assert _as_list(None) == []
    assert _as_list("nope") == []


# --------------------------------------------------------------------------
# _get — retry layer 1: timeouts retry, every other error fails fast
# --------------------------------------------------------------------------

def test_get_retries_timeouts_then_raises(no_sleep):
    farm = make_farm([requests.Timeout()] * 3)
    with pytest.raises(FarmsyncError, match="timeout"):
        farm._get("http://x")
    assert len(farm._s.calls) == 3
    assert no_sleep == [2, 2]


def test_get_recovers_when_a_retry_succeeds(no_sleep):
    ok = FakeResponse({"ok": True})
    farm = make_farm([requests.Timeout(), ok])
    assert farm._get("http://x") is ok
    assert len(farm._s.calls) == 2


def test_get_retries_a_dropped_connection(no_sleep):
    """Measured live: farmsync resets connections often enough that
    retrying timeouts alone lost half of all rounds. A reset is about the wire,
    not about the request, and recovers within seconds."""
    ok = FakeResponse({"ok": True})
    farm = make_farm([requests.ConnectionError("connection aborted"), ok])
    assert farm._get("http://x") is ok
    assert len(farm._s.calls) == 2


def test_get_gives_up_on_a_connection_that_never_comes_back(no_sleep):
    """A dead DNS name is also a ConnectionError and will never recover.
    Retrying it costs 4 seconds before the round fails anyway — the price of
    not discarding every other round."""
    farm = make_farm([requests.ConnectionError("dns")] * 3)
    with pytest.raises(FarmsyncError, match="dns"):
        farm._get("http://x")
    assert len(farm._s.calls) == 3
    assert no_sleep == [2, 2]


def test_get_does_not_retry_http_status_errors(no_sleep):
    """A 401 means the token is wrong and a 500 means farmsync is broken.
    Neither improves by asking three times, so both fail the round at once."""
    farm = make_farm([requests.HTTPError("500 Server Error"), FakeResponse({"ok": True})])
    with pytest.raises(FarmsyncError, match="500"):
        farm._get("http://x")
    assert len(farm._s.calls) == 1
    assert no_sleep == []


def test_get_surfaces_http_status_errors(no_sleep):
    farm = make_farm([FakeResponse(raise_for_status=requests.HTTPError("401 Unauthorized"))])
    with pytest.raises(FarmsyncError, match="401"):
        farm._get("http://x")
    assert len(farm._s.calls) == 1


# --------------------------------------------------------------------------
# solvable_accounts — device/account filtering and ordering
# --------------------------------------------------------------------------

def device(id=1, **kw):
    d = {"id": id, "is_enabled": True, "device_name": f"Device {id}",
         "last_updated": 1_700_000_000_000, "active_accounts": 7}
    d.update(kw)
    return d


def test_disabled_devices_are_never_queried(no_sleep):
    farm = make_farm([
        FakeResponse([device(1, is_enabled=False), device(2)]),
        FakeResponse([]),
    ])
    farm.solvable_accounts()
    urls = [url for _, url, _ in farm._s.calls]
    assert len(urls) == 2
    assert "/devices/2/accounts" in urls[1]


def test_filters_and_orders_accounts(no_sleep):
    farm = make_farm([
        FakeResponse([device(1)]),
        FakeResponse([
            {"id": "a1", "username": "plain", "cookie": "c1",
             "enabled": True, "running": False},
            {"id": "a2", "username": "busy", "cookie": "c2",
             "enabled": True, "running": True},
            {"id": "a3", "username": "off", "cookie": "c3",
             "enabled": False, "running": False},
            {"id": "a4", "username": "rejoin", "cookie": "c4",
             "enabled": True, "running": False, "rejoining": True},
            {"id": "a5", "username": "nocookie", "cookie": "",
             "enabled": True, "running": False},
        ]),
    ])
    got = farm.solvable_accounts()
    # rejoining sorts first; running / disabled / cookieless are dropped
    assert [a["username"] for a in got] == ["rejoin", "plain"]
    assert [a["cookie"] for a in got] == ["c4", "c1"]


def test_falls_back_to_the_roblosecurity_key(no_sleep):
    farm = make_farm([
        FakeResponse([device(1)]),
        FakeResponse([{"id": "a1", "username": "alt", ".ROBLOSECURITY": "c9",
                       "enabled": True, "running": False}]),
    ])
    assert farm.solvable_accounts()[0]["cookie"] == "c9"


def test_missing_username_becomes_empty_string(no_sleep):
    farm = make_farm([
        FakeResponse([device(1)]),
        FakeResponse([{"id": "a1", "cookie": "c1", "enabled": True, "running": False}]),
    ])
    assert farm.solvable_accounts()[0]["username"] == ""


# --------------------------------------------------------------------------
# The widened account record the dispatch filter needs
#
# The dead-host filter and the state store both live outside this module, so
# every field they key off has to be carried out of the device/account loop
# here or it is unreachable downstream.
# --------------------------------------------------------------------------

def test_record_carries_the_account_id_and_device_fields(no_sleep):
    farm = make_farm([
        FakeResponse([device(42, device_name="Device 42",
                             last_updated=1_700_000_123_000, active_accounts=22)]),
        FakeResponse([{"id": "ff00", "username": "u", "cookie": "c",
                       "enabled": True, "running": False}]),
    ])
    assert farm.solvable_accounts() == [{
        "username": "u",
        "cookie": "c",
        "id": "ff00",
        "device_id": 42,
        "device_name": "Device 42",
        "device_last_updated": 1_700_000_123_000,
        "device_active_accounts": 22,
    }]


def test_device_label_is_read_from_device_name_not_name(no_sleep):
    """`name` is empty on every real device; `device_name` carries the label.

    Reading the wrong one yields an empty string from all 81 devices, which
    makes every suppression log line unattributable.
    """
    farm = make_farm([
        FakeResponse([device(1, device_name="Device 1", name="")]),
        FakeResponse([{"id": "a1", "username": "u", "cookie": "c",
                       "enabled": True, "running": False}]),
    ])
    assert farm.solvable_accounts()[0]["device_name"] == "Device 1"


def test_missing_device_heartbeat_fields_default_to_unsuppressable_values(no_sleep):
    """A device that reports no heartbeat must never be read as dead.

    `last_updated` defaults to 0, which would look infinitely stale, so the
    filter's corroboration requirement is what saves it — but the count must
    still default non-zero rather than 0 so a missing field cannot silently
    agree that the host is dark.
    """
    farm = make_farm([
        FakeResponse([{"id": 1, "is_enabled": True}]),
        FakeResponse([{"id": "a1", "username": "u", "cookie": "c",
                       "enabled": True, "running": False}]),
    ])
    got = farm.solvable_accounts()[0]
    assert got["device_name"] == ""
    assert got["device_last_updated"] == 0
    assert got["device_active_accounts"] is None


def test_accounts_from_several_devices_keep_their_own_device_fields(no_sleep):
    farm = make_farm([
        FakeResponse([device(1, active_accounts=0), device(2, active_accounts=9)]),
        FakeResponse([{"id": "a1", "cookie": "c1", "enabled": True, "running": False}]),
        FakeResponse([{"id": "a2", "cookie": "c2", "enabled": True, "running": False}]),
    ])
    got = {a["id"]: a for a in farm.solvable_accounts()}
    assert got["a1"]["device_active_accounts"] == 0
    assert got["a2"]["device_active_accounts"] == 9


def test_session_ignores_ambient_proxy_env_vars():
    """trust_env = False is deliberate; a proxy env var must not reroute traffic."""
    assert Farmsync("TOKEN")._s.trust_env is False


def test_token_is_sent_as_a_bearer_header():
    assert Farmsync("TOKEN")._s.headers["Authorization"] == "Bearer TOKEN"
