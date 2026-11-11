"""Short per-run pytest temp roots on Windows.

Unless long paths are enabled, Windows refuses paths over 260 characters, and pytest's
default root under %TEMP% pushes the deepest test paths past that. So every Windows run
without an explicit --basetemp or PYTEST_DEBUG_TEMPROOT gets ``<drive>:\\tmp\\pt\\<name>``,
on the temp directory's drive, instead. The root is restricted to the current user. Each
run holds an open lock file beside its folder, so pruning never touches a running session,
and only the newest finished runs are kept, as pytest's own retention count does.
"""

from __future__ import annotations

import ntpath
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT_PARTS = ("tmp", "pt")
MARKER_NAME = ".pandrator-pytest-root"
# Changing this text makes existing roots be restricted and re-marked once.
MARKER_TEXT = (
    "Temp folders for this checkout's pytest runs (tests/windows_temp_root.py).\n"
    "Access: current user only. Finished runs beyond the newest few are pruned.\n"
)
LOCK_SUFFIX = ".lock"
# Four characters keep <drive>:\tmp\pt\<name>\popen-gw10 within the ~26 characters W1 allows.
NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
NAME_LENGTH = 4
DEFAULT_KEEP = 3


class ShortRootUnavailable(Exception):
    """The short root cannot be used; the caller falls back to pytest's default."""


@dataclass(frozen=True)
class RunDir:
    """A run's basetemp, which pytest creates on first use, and its held lock."""

    path: Path
    lock_fd: int


def short_root_path(temp_dir: str | Path) -> Path:
    drive, _ = ntpath.splitdrive(str(temp_dir))
    if drive.startswith(("\\\\", "//")):
        raise ShortRootUnavailable(f"temp directory {temp_dir} is on a network share")
    anchor = Path(temp_dir).anchor
    if not anchor:
        raise ShortRootUnavailable(f"temp directory {temp_dir} has no drive root")
    return Path(anchor, *ROOT_PARTS)


def restrict_to_current_user(root: Path) -> None:
    """Replace the root's access with full control for the current user only."""
    user = os.environ.get("USERNAME")
    if not user:
        raise ShortRootUnavailable(f"cannot restrict {root}: USERNAME is not set")
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if domain else user
    # /inheritance:r drops only inherited entries. A folder can also hold explicit
    # ones (GitHub's Windows runners create them), so /reset first turns every
    # entry back into an inherited one that the second call then removes.
    commands = (
        ["icacls", str(root), "/reset"],
        ["icacls", str(root), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
    )
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise ShortRootUnavailable(f"cannot restrict {root}: {error}") from error
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()
            raise ShortRootUnavailable(f"cannot restrict {root} to {account}: {detail}")


def _read_marker(marker: Path) -> str | None:
    try:
        return marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def prepare_root(root: Path, restrict: Callable[[Path], None] = restrict_to_current_user) -> None:
    """Create, restrict and mark the root, or accept one this code already marked."""
    marker = root / MARKER_NAME
    try:
        # exist_ok: a concurrent lane may create the root first.
        root.mkdir(parents=True, exist_ok=True)
        text = _read_marker(marker)
        if text == MARKER_TEXT:
            return
        if text is None and any(child.name != MARKER_NAME for child in root.iterdir()):
            # A racing lane may have marked the root and started a run since the first read.
            text = _read_marker(marker)
            if text is None:
                raise ShortRootUnavailable(
                    f"{root} exists but was not made by these tests; "
                    "remove or rename it, or pass --basetemp"
                )
            if text == MARKER_TEXT:
                return
        # Empty and unmarked (a racing or interrupted first run), or marked by an older version.
        restrict(root)
        marker.write_text(MARKER_TEXT, encoding="utf-8")
    except OSError as error:
        raise ShortRootUnavailable(f"cannot prepare {root}: {error}") from error


def _lock_path(root: Path, name: str) -> Path:
    return root / f"{name}{LOCK_SUFFIX}"


def _new_run(root: Path) -> RunDir:
    for _ in range(100):
        name = "".join(secrets.choice(NAME_ALPHABET) for _ in range(NAME_LENGTH))
        try:
            fd = os.open(_lock_path(root, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        except OSError as error:
            raise ShortRootUnavailable(f"cannot lock a run in {root}: {error}") from error
        if (root / name).exists():
            # A finished run kept under this name; its lock was already released.
            os.close(fd)
            _lock_path(root, name).unlink(missing_ok=True)
            continue
        return RunDir(root / name, fd)
    raise ShortRootUnavailable(f"no free run name in {root}")


def release_finished_lock(lock: Path) -> bool:
    """Delete a finished run's lock; False while its session still holds it open.

    Windows refuses to delete a file another process has open, so this doubles as a
    liveness check. Python opens files without delete sharing and never lets child
    processes inherit them, so only the session itself keeps the lock held.
    """
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def prune_runs(
    root: Path,
    keep: int,
    own: str,
    is_finished: Callable[[Path], bool] = release_finished_lock,
) -> None:
    """Remove finished runs beyond the newest ``keep``; running sessions are never touched."""
    try:
        children = list(root.iterdir())
    except OSError:
        return
    names = {
        child.name.removesuffix(LOCK_SUFFIX)
        for child in children
        if child.name.endswith(LOCK_SUFFIX) or child.is_dir()
    }
    names.discard(own)
    finished: list[tuple[float, Path]] = []
    for name in names:
        if not is_finished(_lock_path(root, name)):
            continue
        folder = root / name
        try:
            finished.append((folder.stat().st_mtime, folder))
        except OSError:
            continue  # A lock with no folder: a run that never used tmp_path.
    finished.sort(reverse=True)
    for _, folder in finished[max(keep, 0) :]:
        remove_tree(folder)


def _make_writable_and_retry(func: Callable[[str], object], path: str, _error: object) -> None:
    # Windows won't delete read-only files, such as the git objects some tests create.
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(folder: Path) -> None:
    """Best-effort removal that also clears read-only files, as pytest's own cleanup does."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(folder, onexc=_make_writable_and_retry)
    else:
        shutil.rmtree(folder, onerror=_make_writable_and_retry)


def make_run_dir(
    platform: str | None = None,
    temp_dir: str | Path | None = None,
    keep: int = DEFAULT_KEEP,
    explicit_basetemp: object = None,
    environ: Mapping[str, str] | None = None,
    restrict: Callable[[Path], None] = restrict_to_current_user,
    is_finished: Callable[[Path], bool] = release_finished_lock,
) -> RunDir | None:
    """Return a new short run with its lock held, or None when pytest's default applies.

    Raises ShortRootUnavailable when an override was wanted but could not be made.
    """
    env = os.environ if environ is None else environ
    if (
        (platform or sys.platform) != "win32"
        or explicit_basetemp is not None
        or env.get("PYTEST_DEBUG_TEMPROOT")
    ):
        return None
    if temp_dir is None:
        try:
            temp_dir = tempfile.gettempdir()
        except OSError as error:
            raise ShortRootUnavailable(f"no usable temp directory: {error}") from error
    root = short_root_path(temp_dir)
    prepare_root(root, restrict)
    run = _new_run(root)
    prune_runs(root, keep, run.path.name, is_finished)
    return run
