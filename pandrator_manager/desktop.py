"""Desktop integration that does not leak a frozen runtime into host tools."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Mapping
from pathlib import Path

_HOST_LIBRARY_PATHS = (
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "LIBPATH",
    "SHLIB_PATH",
)


def _is_frozen_temporary_path(value: str) -> bool:
    try:
        selected = Path(value).expanduser().resolve(strict=False)
        temporary = Path(tempfile.gettempdir()).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        selected.parent == temporary
        and selected.name.startswith("_MEI")
    )


def host_process_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment safe for host binaries and nested launchers.

    PyInstaller prepends its extraction directory to ``LD_LIBRARY_PATH``.
    AppImage -> installed-launcher -> tray chains can preserve more than one
    such directory, so restoring only ``LD_LIBRARY_PATH_ORIG`` is insufficient.
    Remove every PyInstaller temporary entry while retaining genuine user and
    system paths.
    """

    selected = dict(os.environ if environment is None else environment)
    selected.pop("_MEIPASS2", None)
    selected.pop("PYTHONHOME", None)
    separator = os.pathsep
    for variable in _HOST_LIBRARY_PATHS:
        original_key = f"{variable}_ORIG"
        raw_value = selected.get(original_key, selected.get(variable, ""))
        retained = [
            value
            for value in str(raw_value).split(separator)
            if value and not _is_frozen_temporary_path(value)
        ]
        if retained:
            selected[variable] = separator.join(retained)
        else:
            selected.pop(variable, None)
        selected.pop(original_key, None)
    return selected


def _reap(process: subprocess.Popen) -> None:
    try:
        process.wait()
    except OSError:
        pass


def open_desktop_url(url: str) -> bool:
    """Open an HTTP(S) URL without passing bundled libraries to the desktop."""

    selected = str(url).strip()
    if not selected.startswith(("http://", "https://")):
        return False
    if sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if opener is None:
            return False
        try:
            process = subprocess.Popen(
                [opener, selected],
                env=host_process_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
            )
        except OSError:
            return False
        threading.Thread(
            target=_reap,
            args=(process,),
            name=f"pandrator-desktop-open-{process.pid}",
            daemon=True,
        ).start()
        return True
    return bool(webbrowser.open(selected))
