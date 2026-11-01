"""Per-workspace desktop login registration without importing the tray UI."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .context import WorkspaceLayout

_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _tray_autostart_path(layout: WorkspaceLayout) -> Path:
    suffix = hashlib.sha256(str(layout.workspace).encode("utf-8")).hexdigest()[:10]
    if sys.platform.startswith("win"):
        return (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / f"PandratorTray-{suffix}.cmd"
        )
    configured_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(configured_home) if configured_home else Path.home() / ".config"
    return config_home / "autostart" / f"pandrator-tray-{suffix}.desktop"


def _tray_command(layout: WorkspaceLayout) -> tuple[str, ...]:
    from .launcher_runtime import installed_launcher

    installed = installed_launcher(layout)
    if installed is not None:
        return (
            str(installed.executable),
            "tray",
            "--workspace",
            str(layout.workspace),
        )
    if getattr(sys, "frozen", False):
        return (
            str(sys.executable),
            "tray",
            "--workspace",
            str(layout.workspace),
        )
    return (
        str(sys.executable),
        "-m",
        "pandrator_manager.tray",
        "--workspace",
        str(layout.workspace),
    )


def configure_tray_autostart(
    layout: WorkspaceLayout,
    *,
    enabled: bool,
) -> Path | str:
    path = _tray_autostart_path(layout)
    if sys.platform.startswith("win"):
        import winreg

        suffix = hashlib.sha256(str(layout.workspace).encode("utf-8")).hexdigest()[:10]
        value_name = f"PandratorTray-{suffix}"
        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                _WINDOWS_RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                if enabled:
                    winreg.SetValueEx(
                        key,
                        value_name,
                        0,
                        winreg.REG_SZ,
                        subprocess.list2cmdline(_tray_command(layout)),
                    )
                else:
                    try:
                        winreg.DeleteValue(key, value_name)
                    except FileNotFoundError:
                        pass
        finally:
            # Retire the legacy batch-file integration so login never creates
            # a transient cmd.exe window.
            path.unlink(missing_ok=True)
        return f"HKCU\\{_WINDOWS_RUN_KEY}\\{value_name}"
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    command = _tray_command(layout)
    if sys.platform.startswith("win"):
        executable, *arguments = command
        argument_text = " ".join(f'"{argument}"' for argument in arguments)
        content = f'@echo off\nstart "" /b "{executable}" {argument_text}\n'
    else:
        escaped_command = " ".join(
            str(value).replace("\\", "\\\\").replace(" ", "\\ ") for value in command
        )
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Pandrator Tray\n"
            f"Exec={escaped_command}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-KDE-autostart-after=panel\n"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path
