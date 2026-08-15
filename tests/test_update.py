"""The update path, end to end, offline.

Like test_credit.py and test_health.py, every boundary here is arithmetic or a
fake — no waiting, no network. `apply` is the exception and it works against
real files under tmp_path, because a mocked rename would happily accept the
rollback bug this is written to catch.
"""

import os
import pytest
from pathlib import Path

from src import update


@pytest.mark.parametrize("text,expected", [
    ("1.0.0", (1, 0, 0)),
    ("v1.0.0", (1, 0, 0)),
    ("V2.10.3", (2, 10, 3)),
    ("  v1.2.3  ", (1, 2, 3)),
    ("1.0", None),
    ("1.0.0.0", None),
    ("1.0.0-beta", None),
    ("latest", None),
    ("", None),
    (None, None),
    (123, None),
])
def test_parse_version(text, expected):
    assert update.parse_version(text) == expected


@pytest.mark.parametrize("latest,current,expected", [
    ("1.1.0", "1.0.0", True),
    ("v1.1.0", "1.0.0", True),
    ("1.0.1", "1.0.0", True),
    ("2.0.0", "1.9.9", True),
    ("1.0.0", "1.0.0", False),
    ("1.0.0", "1.1.0", False),
    ("1.10.0", "1.9.0", True),      # not a string comparison
    ("garbage", "1.0.0", False),    # unparseable is never newer
    ("1.1.0", "garbage", False),
])
def test_is_newer(latest, current, expected):
    assert update.is_newer(latest, current) is expected


class FakeGitHub:
    """Replays one scripted response, or raises. Local to this module rather
    than reusing conftest's FakeSession, which has no streaming support and is
    shared with the farmsync and solver tests."""

    def __init__(self, payload=None, error=None, status=200, text="", json_error=None):
        self.payload = payload
        self.error = error
        self.status = status
        self.text = text
        self.json_error = json_error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return FakeGitHubResponse(self.payload, self.status, self.text, self.json_error)


class FakeGitHubResponse:
    def __init__(self, payload, status, text, json_error=None):
        self._payload = payload
        self.status_code = status
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def release(tag="v1.1.0", assets=(update.ASSET_NAME, update.SUMS_NAME)):
    return {"tag_name": tag,
            "assets": [{"name": name, "browser_download_url": f"https://x/{name}"}
                       for name in assets]}


def test_check_finds_a_newer_release():
    session = FakeGitHub(release())
    found = update.check(session, "1.0.0")
    assert found["version"] == "1.1.0"
    assert found["tag"] == "v1.1.0"
    assert found["exe_url"] == f"https://x/{update.ASSET_NAME}"
    assert found["sums_url"] == f"https://x/{update.SUMS_NAME}"


def test_check_uses_https_and_no_credentials():
    """The repo is public precisely so no token is compiled into the binary."""
    session = FakeGitHub(release())
    update.check(session, "1.0.0")
    url, kwargs = session.calls[0]
    assert url.startswith("https://")
    assert "auth" not in kwargs
    assert "Authorization" not in kwargs.get("headers", {})


def test_check_returns_nothing_when_already_current():
    assert update.check(FakeGitHub(release("v1.0.0")), "1.0.0") is None


def test_check_returns_nothing_when_the_release_is_older():
    assert update.check(FakeGitHub(release("v0.9.0")), "1.0.0") is None


def test_check_survives_github_being_unreachable():
    """A failed update check must never cost the operator a run."""
    assert update.check(FakeGitHub(error=OSError("no route to host")), "1.0.0") is None


def test_check_survives_a_rate_limit():
    assert update.check(FakeGitHub(release(), status=403), "1.0.0") is None


def test_check_survives_a_response_that_is_not_json():
    assert update.check(FakeGitHub(payload=None), "1.0.0") is None


def test_check_ignores_a_release_missing_the_exe():
    """A release published without its asset, or mid-upload."""
    assert update.check(FakeGitHub(release(assets=(update.SUMS_NAME,))), "1.0.0") is None


def test_check_ignores_a_release_missing_the_checksums():
    """No checksums means nothing to verify against, so there is no safe
    update to offer."""
    assert update.check(FakeGitHub(release(assets=(update.ASSET_NAME,))), "1.0.0") is None


def test_check_honours_the_timeout_it_is_given():
    session = FakeGitHub(release())
    update.check(session, "1.0.0", timeout=0.5)
    assert session.calls[0][1]["timeout"] == 0.5


def test_check_survives_a_json_decode_error():
    """A response body that is not valid JSON must not cost the operator a run."""
    assert update.check(FakeGitHub(release(), json_error=ValueError("No JSON object")), "1.0.0") is None


class FakeDownload:
    """A session whose get() streams fixed bytes."""

    def __init__(self, body=b"", text="", status=200):
        self.body = body
        self.text = text
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeDownloadResponse(self.body, self.text, self.status)


class FakeDownloadResponse:
    def __init__(self, body, text, status):
        self.body = body
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]


SUMS = (
    "d2a84f4b8b650937ec8f73cd8be2c74add5a911ba64df27458ed8229da804a26  OTHER.txt\n"
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08  FarmsyncSolver.exe\n"
)


def test_sha256_of_a_known_file(tmp_path):
    target = tmp_path / "f.bin"
    target.write_bytes(b"test")
    # sha256("test")
    assert update.sha256_of(target) == \
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def test_expected_sha_picks_the_right_line():
    assert update.expected_sha(SUMS) == \
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def test_expected_sha_tolerates_the_binary_star_prefix():
    """`sha256sum -b` writes '*name'. certutil and PowerShell do not, but the
    file may be produced by any of them."""
    assert update.expected_sha(
        "abc123  *FarmsyncSolver.exe\n") == "abc123"


def test_expected_sha_is_none_when_the_asset_is_absent():
    assert update.expected_sha("abc  something-else.exe\n") is None


def test_expected_sha_is_none_for_an_empty_file():
    assert update.expected_sha("") is None


def test_download_writes_the_body_to_disk(tmp_path):
    session = FakeDownload(body=b"binary-payload")
    dest = tmp_path / "nested" / "FarmsyncSolver.exe"

    update.download(session, "https://x/exe", dest)

    assert dest.read_bytes() == b"binary-payload"


def test_download_streams_rather_than_buffering(tmp_path):
    """A 30 MB .exe read into memory on a machine already running 65 worker
    threads is avoidable."""
    session = FakeDownload(body=b"x")
    update.download(session, "https://x/exe", tmp_path / "e.exe")
    assert session.calls[0][1]["stream"] is True


def test_download_raises_on_an_http_error(tmp_path):
    session = FakeDownload(status=404)
    with pytest.raises(RuntimeError):
        update.download(session, "https://x/exe", tmp_path / "e.exe")


def test_a_downloaded_file_matching_its_checksum_verifies(tmp_path):
    target = tmp_path / "FarmsyncSolver.exe"
    target.write_bytes(b"test")
    assert update.sha256_of(target) == update.expected_sha(SUMS)


def test_one_changed_byte_fails_verification(tmp_path):
    """The whole point: a truncated or tampered download never gets installed."""
    target = tmp_path / "FarmsyncSolver.exe"
    target.write_bytes(b"tesT")
    assert update.sha256_of(target) != update.expected_sha(SUMS)


def test_apply_puts_the_new_file_in_place(tmp_path):
    current = tmp_path / "app" / "FarmsyncSolver.exe"
    current.parent.mkdir()
    current.write_bytes(b"old")
    new = tmp_path / "updates" / "FarmsyncSolver.exe"
    new.parent.mkdir()
    new.write_bytes(b"new")

    backup = update.apply(new, current)

    assert current.read_bytes() == b"new"
    assert backup.read_bytes() == b"old"
    assert backup.name == "FarmsyncSolver.exe.old"


def test_apply_moves_the_running_file_aside_before_writing(tmp_path):
    """Order matters. The target name has to be free before anything is
    written to it — Windows will not let the running .exe be overwritten in
    place, only renamed."""
    current = tmp_path / "FarmsyncSolver.exe"
    current.write_bytes(b"old")
    new = tmp_path / "new.exe"
    new.write_bytes(b"new")

    update.apply(new, current)

    assert not new.exists()          # moved, not copied
    assert (tmp_path / "FarmsyncSolver.exe.old").exists()


def test_apply_rolls_back_when_the_move_fails(tmp_path, monkeypatch):
    """The failure that must never leave an installation without a .exe."""
    current = tmp_path / "FarmsyncSolver.exe"
    current.write_bytes(b"old")
    new = tmp_path / "new.exe"
    new.write_bytes(b"new")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(update.shutil, "move", boom)

    with pytest.raises(OSError):
        update.apply(new, current)

    assert current.read_bytes() == b"old"
    assert not (tmp_path / "FarmsyncSolver.exe.old").exists()


def test_apply_replaces_a_leftover_backup(tmp_path):
    """Two updates without an intervening restart, so the previous .old is
    still sitting there."""
    current = tmp_path / "FarmsyncSolver.exe"
    current.write_bytes(b"v2")
    (tmp_path / "FarmsyncSolver.exe.old").write_bytes(b"v1")
    new = tmp_path / "new.exe"
    new.write_bytes(b"v3")

    update.apply(new, current)

    assert current.read_bytes() == b"v3"
    assert (tmp_path / "FarmsyncSolver.exe.old").read_bytes() == b"v2"


def test_apply_raises_rollback_failed_if_the_rollback_fails(tmp_path, monkeypatch):
    """The catastrophic case: the update failed and we cannot put the good
    binary back. The installation has no working .exe."""
    current = tmp_path / "FarmsyncSolver.exe"
    current.write_bytes(b"old")
    new = tmp_path / "new.exe"
    new.write_bytes(b"new")
    backup = tmp_path / "FarmsyncSolver.exe.old"

    call_count = 0
    original_os_replace = os.replace

    def os_replace_fails_on_rollback(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: rename current to backup (must succeed)
            return original_os_replace(*args, **kwargs)
        else:
            # Second call: rollback (must fail)
            raise OSError("backup is locked")

    def move_fails(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(update.os, "replace", os_replace_fails_on_rollback)
    monkeypatch.setattr(update.shutil, "move", move_fails)

    with pytest.raises(update.RollbackFailed) as exc_info:
        update.apply(new, current)

    # The error message must name the backup path
    assert str(backup) in str(exc_info.value)
    # And the good binary should still be at the backup
    assert backup.read_bytes() == b"old"


def test_sweep_deletes_leftovers(tmp_path):
    (tmp_path / "FarmsyncSolver.exe.old").write_bytes(b"x")
    (tmp_path / "FarmsyncSolver.exe").write_bytes(b"y")

    assert update.sweep(tmp_path) == 1
    assert not (tmp_path / "FarmsyncSolver.exe.old").exists()
    assert (tmp_path / "FarmsyncSolver.exe").exists()


def test_sweep_tolerates_a_file_it_cannot_delete(tmp_path, monkeypatch):
    """The previous process may still hold it. The next launch gets it."""
    (tmp_path / "FarmsyncSolver.exe.old").write_bytes(b"x")

    def locked(_self):
        raise OSError("in use")

    monkeypatch.setattr(Path, "unlink", locked)
    assert update.sweep(tmp_path) == 0


def test_sweep_leaves_every_other_dot_old_file_alone(tmp_path):
    """The .exe sits in a folder the operator chose, which may hold their own
    backups. `*.old` swept all of them; only this tool's own is ours."""
    (tmp_path / "FarmsyncSolver.exe.old").write_bytes(b"ours")
    (tmp_path / "taxes.xlsx.old").write_bytes(b"theirs")
    (tmp_path / "notes.old").write_bytes(b"theirs")

    assert update.sweep(tmp_path) == 1
    assert not (tmp_path / "FarmsyncSolver.exe.old").exists()
    assert (tmp_path / "taxes.xlsx.old").read_bytes() == b"theirs"
    assert (tmp_path / "notes.old").read_bytes() == b"theirs"


def test_sweep_follows_a_renamed_executable(tmp_path):
    """`apply` names the backup after whatever the .exe is called, so a
    hard-coded FarmsyncSolver.exe.old would leave a renamed one forever."""
    (tmp_path / "solver.exe.old").write_bytes(b"x")

    assert update.sweep(tmp_path, "solver.exe") == 1
    assert not (tmp_path / "solver.exe.old").exists()


def test_sweep_survives_a_folder_it_cannot_even_list(tmp_path, monkeypatch):
    """The never-fatal promise, stated in two docstrings, applies to the
    listing as well as the deletes — an unlistable exe folder raised straight
    into startup while the glob sat outside the try."""
    def unlistable(_self, _pattern):
        raise OSError("permission denied")
        yield  # pragma: no cover - never reached; makes this a generator

    monkeypatch.setattr(Path, "glob", unlistable)
    assert update.sweep(tmp_path) == 0


def test_sweep_of_none_is_a_no_op():
    """exe_dir() is None from source."""
    assert update.sweep(None) == 0


def test_writable_says_yes_for_a_normal_directory(tmp_path):
    assert update.writable(tmp_path) is True


def test_writable_says_no_for_a_directory_that_is_not_there(tmp_path):
    assert update.writable(tmp_path / "missing") is False


def test_writable_leaves_no_probe_file_behind(tmp_path):
    update.writable(tmp_path)
    assert list(tmp_path.iterdir()) == []
