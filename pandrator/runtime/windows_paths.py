"""Windows MAX_PATH checks for the data root, shared by the CLI and the worker."""

from __future__ import annotations

import logging
import ntpath
import os
import sys

logger = logging.getLogger(__name__)

# MAX_PATH is 260 including the terminating NUL, so with long paths off a path
# holds at most 259 characters.
WINDOWS_MAX_PATH_CHARS = 259

# The deepest file the app writes below the data root is the video export's
# subtitle scratch:
#   sessions\<36>\exports\.<36>-video-<8>\.<36>-translation-final-<36>.srt
# (203 characters). It has no user-supplied part: session, take and job ids are
# 36-character uuids. The runners-up are the soundtrack temp (200), book
# alignment (199) and the video render scratch (192). The soundtrack temp is 200
# only because export file-name stems are capped at 80 characters
# (``pandrator/web/export_names.py``); its version suffix adds a character per
# digit past ``-9``.
DEEPEST_PATH_BELOW_DATA_ROOT = 203

# One separator joins the root to the relative path.
LONGEST_DATA_ROOT = WINDOWS_MAX_PATH_CHARS - 1 - DEEPEST_PATH_BELOW_DATA_ROOT

_LONG_PATHS_HOW = (
    "(LongPathsEnabled=1 under "
    "HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem; that needs "
    "administrator rights and takes effect after restarting Pandrator)."
)
_ENABLE_LONG_PATHS = "Enable Windows long paths " + _LONG_PATHS_HOW
_SHORTEN_FOLDER = (
    "Choose a shorter data folder (for example C:\\Pandrator\\data) or enable "
    "Windows long paths " + _LONG_PATHS_HOW
)
_MAX_CHAIN_STEPS = 8


def _is_windows() -> bool:
    return sys.platform == "win32"


def _registry_long_paths_enabled() -> bool:
    """Read LongPathsEnabled; a missing key or value means off, the Windows default."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "LongPathsEnabled")
        return isinstance(value, int) and value != 0
    except (OSError, ImportError):
        return False


def long_paths_limited() -> bool:
    """True on Windows when long paths are off."""
    return _is_windows() and not _registry_long_paths_enabled()


def data_root_length_warning(root: str | os.PathLike[str]) -> str | None:
    """The warning for a data root over the limit, or None."""
    text = os.fspath(root)
    if len(text) <= LONGEST_DATA_ROOT or not long_paths_limited():
        return None
    return (
        f"The data folder {text} is {len(text)} characters long. With Windows "
        f"long paths off, Pandrator needs a data folder of at most "
        f"{LONGEST_DATA_ROOT} characters. Exports and generation can fail with "
        f"a file-not-found error once files nest deeper. {_SHORTEN_FOLDER}"
    )


def warn_if_data_root_too_long(root: str | os.PathLike[str]) -> None:
    """Log the warning when it applies; never raise."""
    try:
        message = data_root_length_warning(root)
        if message:
            logger.warning("%s", message)
    except Exception:  # noqa: BLE001 - a warning must never stop startup
        pass


def _is_under(path: str, root: str) -> bool:
    return ntpath.normcase(path).startswith(ntpath.normcase(root).rstrip("\\") + "\\")


def explain_long_path_error(
    error: BaseException, root: str | os.PathLike[str]
) -> str | None:
    """A message naming the data root when the error comes from a too-long path."""
    if not long_paths_limited():
        return None
    root_text = os.fspath(root)
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    steps = 0
    while pending and steps < _MAX_CHAIN_STEPS:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        steps += 1
        if isinstance(current, OSError):
            for name in (current.filename, current.filename2):
                if name is None:
                    continue
                try:
                    text = os.fsdecode(name)
                except (TypeError, ValueError):
                    continue
                if len(text) > WINDOWS_MAX_PATH_CHARS and _is_under(text, root_text):
                    if len(root_text) > LONGEST_DATA_ROOT:
                        advice = (
                            f"The data folder should be at most "
                            f"{LONGEST_DATA_ROOT} characters. {_SHORTEN_FOLDER}"
                        )
                    else:
                        advice = _ENABLE_LONG_PATHS
                    return (
                        f"{error} -- A file path under the data folder "
                        f"{root_text} ({len(root_text)} characters) is "
                        f"{len(text)} characters long, over the Windows limit of "
                        f"{WINDOWS_MAX_PATH_CHARS}. {advice}"
                    )
        pending.append(current.__context__)
        pending.append(current.__cause__)
    return None
