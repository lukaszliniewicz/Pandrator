"""Explicit per-user manager autostart adapters for Windows and Linux."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .context import WorkspaceLayout
from .launcher import installed_launcher


@dataclass(frozen=True, slots=True)
class AutostartStatus:
    supported: bool
    installed: bool
    path: str | None
    message: str = ""
    enabled: bool | None = None
    active: bool | None = None


def _manager_start_command(layout: WorkspaceLayout) -> tuple[str, ...]:
    stable = installed_launcher(layout)
    if stable is not None:
        return (
            str(stable.executable),
            "start",
            "--workspace",
            str(layout.workspace),
        )
    return (
        sys.executable,
        "-m",
        "pandrator_manager.cli",
        "--workspace",
        str(layout.workspace),
        "start-manager",
    )


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


class WindowsAutostart:
    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        startup_directory: Path | None = None,
    ) -> None:
        self.layout = layout
        self.startup_directory = startup_directory or (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        suffix = hashlib.sha256(
            str(layout.workspace).encode("utf-8")
        ).hexdigest()[:10]
        self.path = self.startup_directory / f"PandratorManager-{suffix}.cmd"

    @staticmethod
    def _cmd_quote(value: str) -> str:
        return '"' + value.replace("%", "%%").replace('"', '""') + '"'

    def install(self, *, activate: bool = False) -> AutostartStatus:
        del activate
        command = _manager_start_command(self.layout)
        content = (
            "@echo off\n"
            "start \"\" /b "
            + " ".join(self._cmd_quote(value) for value in command)
            + "\n"
        )
        _atomic_text(self.path, content)
        return self.status()

    def remove(self) -> AutostartStatus:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return self.status()

    def restore(self, *, enabled: bool | None = None) -> AutostartStatus:
        del enabled
        return self.install(activate=False)

    def status(self) -> AutostartStatus:
        return AutostartStatus(
            supported=True,
            installed=self.path.is_file(),
            path=str(self.path),
            enabled=self.path.is_file(),
        )


class LinuxSystemdAutostart:
    def __init__(
        self,
        layout: WorkspaceLayout,
        *,
        unit_directory: Path | None = None,
        systemctl: str | None = None,
    ) -> None:
        self.layout = layout
        self.unit_directory = unit_directory or (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "systemd"
            / "user"
        )
        suffix = hashlib.sha256(
            str(layout.workspace).encode("utf-8")
        ).hexdigest()[:10]
        self.unit_name = f"pandrator-manager-{suffix}.service"
        self.path = self.unit_directory / self.unit_name
        self.systemctl = systemctl or shutil.which("systemctl")

    @staticmethod
    def _unit_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    def install(self, *, activate: bool = False) -> AutostartStatus:
        command = _manager_start_command(self.layout)
        content = "\n".join(
            (
                "[Unit]",
                "Description=Pandrator Manager",
                "After=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                "ExecStart="
                + " ".join(self._unit_quote(value) for value in command),
                "Restart=on-failure",
                "RestartSec=3",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            )
        )
        _atomic_text(self.path, content)
        if activate:
            if not self.systemctl:
                raise RuntimeError("systemctl is unavailable; unit was written but not enabled.")
            self._systemctl("daemon-reload")
            self._systemctl("enable", "--now", self.unit_name)
        return self.status()

    def remove(self) -> AutostartStatus:
        if self.systemctl and self.path.exists():
            self._systemctl("disable", "--now", self.unit_name, check=False)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if self.systemctl:
            self._systemctl("daemon-reload", check=False)
        return self.status()

    def restore(self, *, enabled: bool | None = None) -> AutostartStatus:
        self.install(activate=False)
        if enabled and self.systemctl:
            self._systemctl("daemon-reload")
            self._systemctl("enable", self.unit_name)
        return self.status()

    def _systemctl(self, *arguments: str, check: bool = True) -> None:
        subprocess.run(
            [self.systemctl, "--user", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            timeout=60,
            check=check,
        )

    def status(self) -> AutostartStatus:
        supported = bool(self.systemctl)
        enabled: bool | None = None
        active: bool | None = None
        if self.systemctl and self.path.is_file():
            enabled = self._systemctl_state("is-enabled") == "enabled"
            active = self._systemctl_state("is-active") == "active"
        return AutostartStatus(
            supported=supported,
            installed=self.path.is_file(),
            path=str(self.path),
            message="" if supported else "systemd user services are unavailable.",
            enabled=enabled,
            active=active,
        )

    def _systemctl_state(self, action: str) -> str:
        try:
            result = subprocess.run(
                [self.systemctl, "--user", action, self.unit_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip().lower()


def autostart_adapter(
    layout: WorkspaceLayout,
    **overrides,
) -> WindowsAutostart | LinuxSystemdAutostart:
    if os.name == "nt":
        return WindowsAutostart(layout, **overrides)
    if sys.platform.startswith("linux"):
        return LinuxSystemdAutostart(layout, **overrides)
    raise RuntimeError("Manager autostart is not supported on this platform.")
