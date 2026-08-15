"""Notice a newer GitHub release, verify it, and take its place.

Reads no config and calls no clock, the same rule dispatch.py, credit.py, and
health.py follow: the HTTP session, every timeout, and every path arrive as
arguments, so the tests are arithmetic and fakes rather than waiting on a
network.

Nothing here is ever allowed to be fatal. A daemon built to survive a dibycap
outage must also survive a GitHub outage, so the entry points return rather
than raise and the caller goes on running the current version.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "errored-app/farmsync-solver"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Stable across every version, so the checker never has to guess an asset name
# from a tag. release.yml publishes exactly these two.
ASSET_NAME = "FarmsyncSolver.exe"
SUMS_NAME = "SHA256SUMS.txt"


class RollbackFailed(RuntimeError):
    """The update failed and the rollback to the original .exe also failed.

    The good binary is at the path named in the message. The operator must
    rename it back to the original name to restore the installation.
    """


def parse_version(text):
    """('v1.2.3' | '1.2.3') -> (1, 2, 3). None for anything else.

    Strict on purpose. A tag the tool cannot read must compare as "not newer"
    rather than as "newer than everything", which is what a lexicographic
    fallback would do.
    """
    if not text or not isinstance(text, str):
        return None
    parts = text.strip().lstrip("vV").split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_newer(latest, current) -> bool:
    """Tuple comparison, not string: 1.10.0 is newer than 1.9.0."""
    there, here = parse_version(latest), parse_version(current)
    if there is None or here is None:
        return False
    return there > here


# Three seconds. The check runs before any work starts, so a slow GitHub must
# cost the operator three seconds at most, not a round.
CHECK_TIMEOUT = 3.0


def check(session, current, timeout=CHECK_TIMEOUT):
    """Ask GitHub for the newest release. None means "nothing to do".

    Unauthenticated, because the repo is public — which is the whole reason no
    token is compiled into the binary.

    Every failure returns None: offline, rate limited, a body that is not JSON,
    a tag that cannot be parsed, a release still mid-upload with one asset
    missing. A release with no SHA256SUMS.txt is treated as no release at all,
    because there would be nothing to verify the download against.
    """
    try:
        response = session.get(
            LATEST_URL, timeout=timeout,
            headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        payload = response.json() or {}
    except Exception:
        return None

    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not is_newer(tag, current):
        return None

    assets = {asset.get("name"): asset.get("browser_download_url")
              for asset in (payload.get("assets") or [])}
    if not assets.get(ASSET_NAME) or not assets.get(SUMS_NAME):
        return None

    return {"version": tag.strip().lstrip("vV"),
            "tag": tag,
            "exe_url": assets[ASSET_NAME],
            "sums_url": assets[SUMS_NAME]}


# Generous: a 30 MB asset on a slow line. Nothing waits on this except the
# operator who already said yes.
DOWNLOAD_TIMEOUT = 120.0

CHUNK = 1 << 16


def fetch_text(session, url, timeout=CHECK_TIMEOUT) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_sha(sums_text, asset_name=ASSET_NAME):
    """Pull one hash out of a sha256sum-style file. None if it is not listed.

    The '*' prefix is what `sha256sum -b` writes for a binary file; PowerShell
    and certutil do not, and the file may have been produced by any of them.
    """
    for line in (sums_text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset_name:
            return parts[0].strip().lower()
    return None


def download(session, url, dest, timeout=DOWNLOAD_TIMEOUT):
    """Stream a release asset to `dest`. Raises on any HTTP failure.

    Streamed rather than buffered: the asset is ~30 MB and the process may
    already be running 65 worker threads.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    response = session.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    with open(dest, "wb") as handle:
        for chunk in response.iter_content(CHUNK):
            if chunk:
                handle.write(chunk)
    return dest


BACKUP_SUFFIX = ".old"

# CREATE_NEW_CONSOLE. The replacement needs its own window, because this
# process is about to exit and take the current one with it.
CREATE_NEW_CONSOLE = 0x00000010


def writable(folder) -> bool:
    """Can the .exe rewrite its own folder?

    Checked before anything is downloaded. An .exe under Program Files cannot,
    and finding that out after a 30 MB download and a checksum is a waste of
    the operator's time and bandwidth.
    """
    probe = Path(folder) / ".farmsync-write-probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def apply(new_exe, current_exe):
    """Put `new_exe` where `current_exe` is. Returns the backup path.

    Windows permits renaming a *running* executable, which is what makes an
    in-process update legal and why there is no separate updater program here.

    The order is load-bearing. The running file is moved aside first, so the
    target name is free before anything is written to it — Windows refuses to
    overwrite a running image in place but is happy to rename it.

    This function can raise at three points:
    - Before the backup exists (backup.unlink() or os.replace(current, backup)):
      harmless, nothing moved.
    - After a successful rollback (inside the except handler, after os.replace
      succeeds): the original exception is re-raised unchanged.
    - If the rollback itself fails (os.replace(backup, current)): raises
      RollbackFailed. This is the one case where the operator must act —
      the good binary is still at the backup path.

    shutil.move rather than os.replace for the second step: the download lives
    under %LOCALAPPDATA%, which can be on a different drive from the .exe, and
    os.replace cannot cross a drive boundary.
    """
    current_exe = Path(current_exe)
    backup = Path(str(current_exe) + BACKUP_SUFFIX)
    if backup.exists():
        backup.unlink()
    os.replace(current_exe, backup)
    try:
        shutil.move(str(new_exe), str(current_exe))
    except Exception as original_error:
        try:
            os.replace(backup, current_exe)
        except Exception as rollback_error:
            raise RollbackFailed(
                f"Update failed and rollback failed. The good binary is at {backup}. "
                f"Rename it to {current_exe}."
            ) from rollback_error
        raise
    return backup


def sweep(folder, exe_name=ASSET_NAME) -> int:
    """Delete this tool's leftover backup from a past update. Never raises.

    A file still held open by the process that was replaced simply survives to
    the next launch — which is why this is called at startup rather than
    immediately after the swap.

    Scoped to one name rather than every `*.old` in the folder. That folder is
    one the operator chose and may hold their own files; deleting somebody
    else's backups is not this program's business. `exe_name` is the running
    executable's own name, because `apply` names the backup after whatever the
    .exe is actually called and an operator may have renamed it.

    The listing is inside the try as well as the deletes. An exe folder that
    cannot be listed at all would otherwise raise straight into startup, which
    is exactly the never-fatal promise this module makes everywhere else.
    """
    if folder is None:
        return 0
    removed = 0
    try:
        for stale in Path(folder).glob(f"{exe_name}{BACKUP_SUFFIX}"):
            try:
                stale.unlink()
                removed += 1
            except OSError:
                pass
    except Exception:
        # Broader than the per-file OSError on purpose: "never fatal" has to
        # hold for whatever a hostile folder does to the listing, not only for
        # the failures that were thought of.
        return removed
    return removed


def relaunch(exe, args) -> None:
    """Start the replacement in its own console and leave it running."""
    subprocess.Popen([str(exe), *args], creationflags=CREATE_NEW_CONSOLE,
                     close_fds=True)
