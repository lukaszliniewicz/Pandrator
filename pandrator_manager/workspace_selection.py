"""Per-user workspace selection for the native launcher and command-line tools.

The managed installation cannot store the only pointer to itself: a freshly
downloaded launcher needs to find the user's previous choice before it knows
where that installation lives.  This module therefore keeps one small,
non-secret preference in the platform's per-user configuration directory.

The native directory chooser deliberately uses operating-system facilities
instead of Qt.  Headless and automated deployments retain the explicit
``--workspace`` and ``PANDRATOR_WORKSPACE`` paths.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .auth import protect_path

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_DIRECTORY = "pandrator"
SETTINGS_FILENAME = "manager-launcher.json"
LEGACY_SETTINGS_FILENAME = "installer.json"
MAXIMUM_SETTINGS_BYTES = 64 * 1024


class WorkspaceSelectionUnavailable(RuntimeError):
    """The current desktop has no supported native directory chooser."""


def normalized_system(system: str | None = None) -> str:
    value = (system or platform.system() or os.name).casefold()
    if value in {"nt", "win32", "cygwin", "windows"}:
        return "windows"
    if value in {"posix", "linux"}:
        return "linux"
    if value in {"darwin", "mac", "macos"}:
        return "macos"
    return value


def _resolved_home(home: str | os.PathLike[str] | None = None) -> Path:
    return Path(home if home is not None else Path.home()).expanduser().resolve(
        strict=False
    )


def launcher_settings_directory(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    selected_home = _resolved_home(home)
    if normalized_system(system) == "windows":
        configured = str(values.get("LOCALAPPDATA") or "").strip()
        root = (
            Path(configured).expanduser()
            if configured
            else selected_home / "AppData" / "Local"
        )
    else:
        configured = str(values.get("XDG_CONFIG_HOME") or "").strip()
        expanded = Path(configured).expanduser() if configured else None
        root = (
            expanded
            if expanded is not None and expanded.is_absolute()
            else selected_home / ".config"
        )
    return root.resolve(strict=False) / SETTINGS_DIRECTORY


def launcher_settings_path(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    return launcher_settings_directory(
        system=system,
        environ=environ,
        home=home,
    ) / SETTINGS_FILENAME


def legacy_launcher_settings_path(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    return launcher_settings_directory(
        system=system,
        environ=environ,
        home=home,
    ) / LEGACY_SETTINGS_FILENAME


def _workspace_from_settings(path: Path, *, legacy: bool) -> Path | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAXIMUM_SETTINGS_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not legacy and payload.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        return None
    value = payload.get("workspace")
    if not isinstance(value, str) or not value.strip():
        return None
    selected = Path(value.strip()).expanduser()
    if not selected.is_absolute():
        return None
    resolved = selected.resolve(strict=False)
    return resolved if resolved.is_dir() else None


def load_remembered_workspace(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Load the current preference, falling back to the legacy Qt setting."""

    current_path = launcher_settings_path(
        system=system,
        environ=environ,
        home=home,
    )
    if os.path.lexists(current_path):
        return _workspace_from_settings(current_path, legacy=False)
    return _workspace_from_settings(
        legacy_launcher_settings_path(
            system=system,
            environ=environ,
            home=home,
        ),
        legacy=True,
    )


def _redirected_directory(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction is not None and junction())


def remember_workspace(
    workspace: str | os.PathLike[str],
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically persist an existing canonical workspace for later launches."""

    selected = Path(workspace).expanduser().resolve(strict=False)
    if not selected.is_dir():
        raise ValueError(f"Manager workspace does not exist: {selected}")
    destination = launcher_settings_path(
        system=system,
        environ=environ,
        home=home,
    )
    directory = destination.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if _redirected_directory(directory) or not directory.is_dir():
        raise RuntimeError(
            f"Manager launcher settings directory is redirected: {directory}"
        )
    protect_path(directory, directory=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": SETTINGS_SCHEMA_VERSION,
                    "workspace": str(selected),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        protect_path(temporary)
        os.replace(temporary, destination)
        protect_path(destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def default_workspace(
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the shared default used by CLI and optional tray entry points."""

    values = os.environ if environ is None else environ
    configured = str(values.get("PANDRATOR_WORKSPACE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    remembered = load_remembered_workspace(environ=values, home=home)
    if remembered is not None:
        return remembered
    return _resolved_home(home)


def _existing_initial_directory(initial: Path) -> Path:
    selected = initial.expanduser().resolve(strict=False)
    while selected != selected.parent and not selected.is_dir():
        selected = selected.parent
    return selected if selected.is_dir() else Path.home().resolve(strict=False)


def _select_windows_directory(initial: Path) -> Path | None:
    """Open the Windows shell's dependency-free folder browser."""

    from ctypes import wintypes

    browse_callback = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.LPARAM,
        wintypes.LPARAM,
    )
    bffm_initialized = 1
    bffm_setselection_w = 0x467
    bif_returnonlyfsdirs = 0x0001
    bif_newdialogstyle = 0x0040
    bif_editbox = 0x0010

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", browse_callback),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    initial_buffer = ctypes.create_unicode_buffer(str(initial))

    @browse_callback
    def initialized(hwnd, message, _lparam, _data):
        if message == bffm_initialized:
            user32.SendMessageW(
                hwnd,
                bffm_setselection_w,
                1,
                ctypes.addressof(initial_buffer),
            )
        return 0

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPWSTR,
    ]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoUninitialize.argtypes = []

    display_name = ctypes.create_unicode_buffer(260)
    result_path = ctypes.create_unicode_buffer(32768)
    info = BrowseInfo(
        None,
        None,
        ctypes.cast(display_name, wintypes.LPWSTR),
        (
            "Choose the parent folder for Pandrator. "
            "A Pandrator folder will be created inside it."
        ),
        bif_returnonlyfsdirs | bif_newdialogstyle | bif_editbox,
        initialized,
        0,
        0,
    )
    initialized_com = ole32.CoInitializeEx(None, 0x2) >= 0
    try:
        item_id = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not item_id:
            return None
        try:
            if not shell32.SHGetPathFromIDListW(item_id, result_path):
                raise WorkspaceSelectionUnavailable(
                    "Windows did not return the selected directory."
                )
            return Path(result_path.value).resolve(strict=False)
        finally:
            ole32.CoTaskMemFree(item_id)
    finally:
        if initialized_com:
            ole32.CoUninitialize()


def _desktop_command(
    initial: Path,
    *,
    system: str,
) -> list[str] | None:
    title = "Choose where Pandrator should be installed"
    if system == "linux":
        if executable := shutil.which("zenity"):
            initial_value = f"{initial}{os.sep}"
            return [
                executable,
                "--file-selection",
                "--directory",
                f"--title={title}",
                f"--filename={initial_value}",
            ]
        if executable := shutil.which("kdialog"):
            return [
                executable,
                "--title",
                title,
                "--getexistingdirectory",
                str(initial),
            ]
        if executable := shutil.which("yad"):
            initial_value = f"{initial}{os.sep}"
            return [
                executable,
                "--file-selection",
                "--directory",
                f"--title={title}",
                f"--filename={initial_value}",
            ]
        return None
    if system == "macos" and (executable := shutil.which("osascript")):
        escaped = str(initial).replace("\\", "\\\\").replace('"', '\\"')
        return [
            executable,
            "-e",
            (
                'POSIX path of (choose folder with prompt "Choose the parent '
                'folder for Pandrator" default location POSIX file '
                f'"{escaped}")'
            ),
        ]
    return None


def _select_command_directory(
    initial: Path,
    *,
    system: str,
    environ: Mapping[str, str],
) -> Path | None:
    if system == "linux" and not (
        environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")
    ):
        raise WorkspaceSelectionUnavailable(
            "No graphical Linux desktop session is available."
        )
    command = _desktop_command(initial, system=system)
    if command is None:
        raise WorkspaceSelectionUnavailable(
            f"No supported {system} directory chooser is installed."
        )
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        env=dict(environ),
    )
    error_text = result.stderr.strip()
    if result.returncode in {1, 130} and (
        not error_text or "cancel" in error_text.casefold()
    ):
        return None
    if result.returncode != 0:
        message = error_text or result.stdout.strip()
        raise WorkspaceSelectionUnavailable(
            message or "The desktop directory chooser could not be opened."
        )
    value = result.stdout.strip()
    if not value:
        raise WorkspaceSelectionUnavailable(
            "The desktop directory chooser returned no directory."
        )
    selected = Path(value).expanduser().resolve(strict=False)
    if not selected.is_dir():
        raise WorkspaceSelectionUnavailable(
            f"The selected directory is unavailable: {selected}"
        )
    return selected


def select_workspace_directory(
    initial: str | os.PathLike[str],
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Select an existing parent directory, or return ``None`` on cancellation."""

    selected_system = normalized_system(system)
    selected_initial = _existing_initial_directory(Path(initial))
    if selected_system == "windows":
        return _select_windows_directory(selected_initial)
    if selected_system in {"linux", "macos"}:
        return _select_command_directory(
            selected_initial,
            system=selected_system,
            environ=os.environ if environ is None else environ,
        )
    raise WorkspaceSelectionUnavailable(
        f"Workspace selection is unavailable on {selected_system or 'this platform'}."
    )
