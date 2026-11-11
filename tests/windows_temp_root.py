"""Short per-run pytest temp roots on Windows.

Unless long paths are enabled, Windows refuses paths over 260 characters, and pytest's
default root under %TEMP% pushes the deepest test paths past that. So every Windows run
without an explicit --basetemp or PYTEST_DEBUG_TEMPROOT gets ``<drive>:\\tmp\\pt\\<name>``,
on the temp directory's drive, instead. The root is restricted to the current user. Each
run holds an open lock file beside its folder, so pruning never touches a running session,
and only the newest finished runs are kept, as pytest's retention settings ask.

Because the root is shared by every checkout and lane of this user, ownership is proved
rather than assumed:

* a root is used only when its marker file holds exactly ``MARKER_TEXT``; anything else
  (a foreign folder, a truncated or unknown marker) is refused, never rewritten or
  re-ACLed, and the caller falls back to pytest's default temp root;
* pruning deletes only folders with a strict run name that also have a run lock file
  beside them, and only after that lock can be deleted, which on Windows proves the
  owning session is gone;
* root preparation and pruning are serialized by an OS file lock, and the marker is
  published with an atomic replace, so concurrent lanes can never reset ACLs against
  each other or observe a half-written marker.
"""

from __future__ import annotations

import errno
import ntpath
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT_PARTS = ("tmp", "pt")
MARKER_NAME = ".pandrator-pytest-root"
# Written beside the marker while the marker is published; ignored when checking ownership.
MARKER_TEMP_NAME = f"{MARKER_NAME}.tmp"
# Serializes preparation and pruning for every process sharing the root.
ROOT_LOCK_NAME = f"{MARKER_NAME}.lock"
RESERVED_NAMES = frozenset({MARKER_NAME, MARKER_TEMP_NAME, ROOT_LOCK_NAME})
# Changing this text makes existing roots be refused; edit it only with a migration.
MARKER_TEXT = (
    "Temp folders for this checkout's pytest runs (tests/windows_temp_root.py).\n"
    "Access: current user only. Finished runs beyond the newest few are pruned.\n"
)
LOCK_SUFFIX = ".lock"
# Separate from the live lock: it survives release so retained finished runs
# can be safely recognized and pruned on *later* pytest invocations.
RUN_MARKER_SUFFIX = ".pandrator-run"
RUN_MARKER_TEXT = "Pandrator pytest run marker v1\n"
# Four characters keep <drive>:\tmp\pt\<name>\popen-gw10 within the ~26 characters W1 allows.
NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
NAME_LENGTH = 4
RUN_NAME_PATTERN = re.compile(rf"[{NAME_ALPHABET}]{{{NAME_LENGTH}}}")
DEFAULT_KEEP = 3
ROOT_LOCK_TIMEOUT = 30.0
ROOT_LOCK_POLL = 0.05
BOOTSTRAP_LOCK_NAME = ".pandrator-pytest-root-init.lock"
BOOTSTRAP_LOCK_TIMEOUT = 90.0


class ShortRootUnavailable(Exception):
    """The short root cannot be used; the caller falls back to pytest's default."""


@dataclass(frozen=True)
class RunDir:
    """A run's basetemp, which pytest creates on first use, and its held lock."""

    path: Path
    lock_fd: int


def short_root_path(temp_dir: str | Path) -> Path:
    # ntpath keeps drive parsing the same on every host; tests run this on Linux.
    drive, _ = ntpath.splitdrive(str(temp_dir))
    if drive.startswith(("\\\\", "//")):
        raise ShortRootUnavailable(f"temp directory {temp_dir} is on a network share")
    anchor = Path(temp_dir).anchor
    if not anchor:
        raise ShortRootUnavailable(f"temp directory {temp_dir} has no drive root")
    return Path(anchor, *ROOT_PARTS)


def _current_account_from_env() -> str:
    # Used by non-Windows tests. USERDOMAIN on a workgroup PC can be
    # "WORKGROUP", which is NOT a resolvable account authority for icacls.
    user = os.environ.get("USERNAME")
    if not user:
        raise ShortRootUnavailable("cannot restrict the temp root: USERNAME is not set")
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def _current_account() -> str:
    if sys.platform != "win32":
        return _current_account_from_env()
    # A Windows user has a stable security identifier independent of the
    # localized machine/domain display name. icacls accepts raw SIDs when
    # prefixed with *, both for /grant:r and /setowner.
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShortRootUnavailable(
            f"cannot resolve current Windows account SID: {error}"
        ) from error
    matched = re.search(r"(?<![\w-])S-1-\d+(?:-\d+)+(?![\w-])", result.stdout or "")
    if result.returncode != 0 or matched is None:
        raise ShortRootUnavailable("cannot resolve current Windows account SID")
    return f"*{matched.group()}"


def _icacls(root: Path, arguments: list[str]) -> None:
    command = ["icacls", str(root), *arguments]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # Windows icacls uses the active console code page; on localized
            # machines an undecodable byte must not crash the pytest hook.
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ShortRootUnavailable(f"cannot run icacls for {root}: {error}") from error
    if result.returncode != 0:
        # subprocess reader-thread decoding failures can leave a stream unset.
        # A failed ACL operation must always raise our recoverable exception.
        detail = " ".join(f"{result.stdout or ''} {result.stderr or ''}".split())
        if len(detail) > ACL_DIAGNOSTIC_LIMIT:
            detail = f"{detail[:ACL_DIAGNOSTIC_LIMIT]}..."
        detail = detail or f"exit code {result.returncode}"
        hint = " (another user may own it)" if "access is denied" in detail.lower() else ""
        raise ShortRootUnavailable(f"cannot restrict {root}: {detail}{hint}")


def _assert_no_reparse_ancestors(root: Path) -> None:
    """Never create or change ACLs through symlinks or Windows junctions."""
    for part in (*root.parents, root):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if part.is_symlink() or (attributes & reparse):
            raise ShortRootUnavailable(f"refusing symlink or junction in temp root: {part}")


# Windows runners invoke powershell.exe, which is Windows PowerShell 5.1, not
# PowerShell 7. The 5.1 parser rejects a line break after a binary operator
# (5.1 needs the operator at the start of the next line or a backtick), so a
# multi-line expression here would fail to parse and fail every ACL check closed.
# Keep every statement on one line and avoid syntax newer than 5.1.
_ACL_VERIFY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$acl = [System.IO.DirectoryInfo]::new($env:PANDRATOR_PYTEST_ACL_ROOT).GetAccessControl()
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
$rules = @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))
$full = [int][System.Security.AccessControl.FileSystemRights]::FullControl
$valid = ($owner -eq $current) -and $acl.AreAccessRulesProtected -and ($rules.Count -eq 1)
if ($valid) {
  $rule = $rules[0]
  $same_user = $rule.IdentityReference.Value -eq $current
  $allowed = $rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow
  $full_control = (([int]$rule.FileSystemRights -band $full) -eq $full)
  $valid = $same_user -and $allowed -and $full_control
}
if (-not $valid) {
  [Console]::Error.WriteLine('Unexpected owner or access rules')
  exit 3
}
"""
ACL_DIAGNOSTIC_LIMIT = 300


def _verify_private_root(root: Path) -> None:
    """Fail closed if a marked Windows root is not *actually* private.

    A marker is public text and can be forged by another local user. Query the
    Windows security descriptor, not localized icacls output, to establish the
    current user's SID owns the root and is its only explicit full-control ACE.
    """
    if sys.platform != "win32":
        return
    env = {**os.environ, "PANDRATOR_PYTEST_ACL_ROOT": str(root)}
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _ACL_VERIFY_SCRIPT,
            ],
            capture_output=True,
            text=True,
            # Localized PowerShell errors must never turn a refusal into a crash.
            errors="replace",
            check=False,
            env=env,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShortRootUnavailable(f"cannot verify private temp root {root}: {error}") from error
    if result.returncode == 0:
        return
    # Surface the reason (for example a parser error) instead of an opaque refusal,
    # but keep the message bounded and single-line.
    detail = " ".join(f"{result.stdout or ''} {result.stderr or ''}".split())
    if len(detail) > ACL_DIAGNOSTIC_LIMIT:
        detail = f"{detail[:ACL_DIAGNOSTIC_LIMIT]}..."
    suffix = f": {detail}" if detail else ""
    raise ShortRootUnavailable(
        f"temp root {root} has unverified ownership or ACL{suffix}; use pytest's default temp root"
    )


def _restore_inherited_access(root: Path, account: str) -> None:
    """Undo a half-applied restriction; best effort, the caller already failed."""
    for arguments in (["/inheritance:e"], ["/grant:r", f"{account}:F"]):
        try:
            _icacls(root, arguments)
        except ShortRootUnavailable:
            pass


def restrict_to_current_user(root: Path) -> None:
    """Replace the root's access with full control for the current user only."""
    account = _current_account()
    # /inheritance:r drops only inherited entries. A folder can also hold explicit
    # ones (GitHub's Windows runners create them), so /reset first turns every
    # entry back into an inherited one that the second call then removes.
    # If the second call fails after /reset, put inherited access back so the
    # folder is not left reachable by nobody, and let the caller fall back.
    _icacls(root, ["/reset"])
    try:
        _icacls(root, ["/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"])
        # Some elevated Windows runners create directories owned by the
        # Administrators group. Set the actual caller as owner too, so the
        # verified private root cannot subsequently be re-ACLed by another
        # non-administrative account.
        _icacls(root, ["/setowner", account])
    except ShortRootUnavailable:
        _restore_inherited_access(root, account)
        raise


def _read_marker(marker: Path) -> str | None:
    try:
        return marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        # Not one of ours, and not safe to treat as absent: adopting it would rewrite it.
        return "\x00"


def _publish_marker(root: Path) -> None:
    """Replace the marker only once it is complete, so readers never see half of it."""
    marker = root / MARKER_NAME
    temp = root / MARKER_TEMP_NAME
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(MARKER_TEXT)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, marker)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _foreign_children(root: Path) -> list[Path]:
    return [child for child in root.iterdir() if child.name not in RESERVED_NAMES]


def _foreign_root(root: Path, found: str) -> ShortRootUnavailable:
    return ShortRootUnavailable(
        f"{root} exists but was not made by these tests (found {found!r}); "
        "remove or rename it, or pass --basetemp"
    )


def _lock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _file_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Hold an OS file lock, including across crashed worker processes."""
    _assert_no_reparse_ancestors(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            # Windows requires a byte to exist before msvcrt locks its range.
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock_fd(fd)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise ShortRootUnavailable(
                        f"timed out waiting for {path}; another pytest lane may be stuck"
                    ) from error
                time.sleep(ROOT_LOCK_POLL)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


@contextmanager
def _bootstrap_lock() -> Iterator[None]:
    """Serialize first creation, before a shared root is even present.

    This lock lives in the caller's private temp directory, outside the shared root.
    Concurrent lanes cannot mistake an unmarked, partly initialized root for
    a foreign directory, nor can a late lane reset another lane's ACL.
    """
    path = Path(tempfile.gettempdir()) / BOOTSTRAP_LOCK_NAME
    with _file_lock(path, timeout=BOOTSTRAP_LOCK_TIMEOUT):
        yield


@contextmanager
def _root_lock(root: Path) -> Iterator[None]:
    with _file_lock(root / ROOT_LOCK_NAME, timeout=ROOT_LOCK_TIMEOUT):
        yield


def _cleanup_abandoned_root(root: Path) -> None:
    """Best-effort cleanup ONLY for a root created by the failing lane.

    Called after closing the root lock, while still holding the user-private
    bootstrap lock. Never remove a marked root or any foreign child.
    """
    try:
        _assert_no_reparse_ancestors(root)
        if not root.is_dir() or _read_marker(root / MARKER_NAME) == MARKER_TEXT:
            return
        if _foreign_children(root):
            return
        shutil.rmtree(root)
    except (OSError, ShortRootUnavailable):
        # Failure to prove ownership or remove a root must fail closed; the
        # next lane refuses an unmarked root rather than modifying foreign data.
        pass


def prepare_root(root: Path, restrict: Callable[[Path], None] = restrict_to_current_user) -> None:
    """Create and mark a fresh private root, or verify a previously marked root."""
    marker = root / MARKER_NAME
    verify = restrict is restrict_to_current_user and sys.platform == "win32"
    try:
        _assert_no_reparse_ancestors(root)
        text = _read_marker(marker)
        if text == MARKER_TEXT:
            if verify:
                _verify_private_root(root)
            return
        if text is not None:
            raise _foreign_root(root, MARKER_NAME)

        # The root's own lock cannot prevent a second lane winning the race
        # before its creator opens that lock. Use a private bootstrap lock
        # outside the shared root to serialize creation and ACL restriction.
        with _bootstrap_lock():
            created = False
            try:
                _assert_no_reparse_ancestors(root)
                try:
                    root.mkdir(parents=True)
                    created = True
                except FileExistsError:
                    pass
                _assert_no_reparse_ancestors(root)
                text = _read_marker(marker)
                if text == MARKER_TEXT:
                    if verify:
                        _verify_private_root(root)
                    return
                if text is not None:
                    raise _foreign_root(root, MARKER_NAME)
                if not created:
                    # An unmarked old/foreign root is never adopted.
                    raise _foreign_root(root, "unmarked pre-existing folder")
                with _root_lock(root):
                    _assert_no_reparse_ancestors(root)
                    if _foreign_children(root):
                        raise _foreign_root(root, "foreign child")
                    restrict(root)
                    if verify:
                        _verify_private_root(root)
                    _publish_marker(root)
            except Exception:
                if created:
                    _cleanup_abandoned_root(root)
                raise
    except OSError as error:
        raise ShortRootUnavailable(f"cannot prepare {root}: {error}") from error


def _lock_path(root: Path, name: str) -> Path:
    return root / f"{name}{LOCK_SUFFIX}"


def _owner_path(root: Path, name: str) -> Path:
    return root / f"{name}{RUN_MARKER_SUFFIX}"


def _new_run(root: Path) -> RunDir:
    for _ in range(100):
        name = "".join(secrets.choice(NAME_ALPHABET) for _ in range(NAME_LENGTH))
        try:
            fd = os.open(_lock_path(root, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        except OSError as error:
            raise ShortRootUnavailable(f"cannot lock a run in {root}: {error}") from error
        if (root / name).exists() or _owner_path(root, name).exists():
            # A finished run kept under this name; its lock was already released.
            os.close(fd)
            _lock_path(root, name).unlink(missing_ok=True)
            continue
        try:
            marker_fd = os.open(
                _owner_path(root, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(marker_fd, "w", encoding="utf-8") as owner:
                owner.write(RUN_MARKER_TEXT)
        except OSError as error:
            os.close(fd)
            _lock_path(root, name).unlink(missing_ok=True)
            raise ShortRootUnavailable(f"cannot mark owned run in {root}: {error}") from error
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


def _owned_run_names(root: Path) -> list[str]:
    """Only a regular, correctly marked file establishes run-folder ownership."""
    names: list[str] = []
    for marker in root.iterdir():
        if not marker.name.endswith(RUN_MARKER_SUFFIX):
            continue
        name = marker.name.removesuffix(RUN_MARKER_SUFFIX)
        if not RUN_NAME_PATTERN.fullmatch(name):
            continue
        try:
            if (
                marker.is_symlink()
                or not marker.is_file()
                or marker.read_text(encoding="utf-8") != RUN_MARKER_TEXT
            ):
                continue
        except (OSError, UnicodeDecodeError):
            continue
        names.append(name)
    return names


def prune_runs(
    root: Path,
    keep: int | None,
    own: str,
    is_finished: Callable[[Path], bool] = release_finished_lock,
) -> None:
    """Remove finished runs beyond the newest ``keep``; running sessions are never touched.

    ``keep=None`` keeps everything. Only folders this helper can prove it owns are
    candidates, so unrelated directories in the same root survive untouched.
    """
    if keep is None:
        return
    try:
        if _read_marker(root / MARKER_NAME) != MARKER_TEXT:
            return
        with _root_lock(root):
            finished: list[tuple[float, Path]] = []
            for name in _owned_run_names(root):
                if name == own:
                    continue
                folder = root / name
                lock = _lock_path(root, name)
                try:
                    # Never follow an attacker-provided folder/lock symlink.
                    if folder.is_symlink() or lock.is_symlink():
                        continue
                    has_folder = folder.is_dir()
                    if lock.exists():
                        if not lock.is_file() or not is_finished(lock):
                            continue
                except OSError:
                    continue
                if not has_folder:
                    # A run that never used tmp_path; keep no orphan marker.
                    _owner_path(root, name).unlink(missing_ok=True)
                    continue
                try:
                    finished.append((folder.stat().st_mtime, folder))
                except OSError:
                    continue
            finished.sort(key=lambda entry: entry[0], reverse=True)
            for _, folder in finished[max(keep, 0) :]:
                remove_tree(folder)
                if not folder.exists():
                    _owner_path(root, folder.name).unlink(missing_ok=True)
    except (OSError, ShortRootUnavailable):
        # Pruning is housekeeping: a root we cannot read or lock is left alone.
        return


def _make_writable_and_retry(func: Callable[[str], object], path: str, _error: object) -> None:
    # Windows won't delete read-only files, such as the git objects some tests create.
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(folder: Path) -> None:
    """Best-effort removal that also clears read-only files, as pytest's own cleanup does."""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(folder, onexc=_make_writable_and_retry)
        else:
            shutil.rmtree(folder, onerror=_make_writable_and_retry)
    except FileNotFoundError:
        pass


def make_run_dir(
    platform: str | None = None,
    temp_dir: str | Path | None = None,
    keep: int | None = DEFAULT_KEEP,
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
