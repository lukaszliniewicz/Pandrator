"""Optional non-Qt tray client."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

import psutil

from ..client import ManagerClient
from ..context import WorkspaceLayout
from ..workspace_selection import default_workspace


def tray_available() -> tuple[bool, str]:
    # pystray selects and initializes its X11 backend during import.  Check
    # session availability first so a headless Linux host gets a normal
    # optional-feature result instead of an Xlib traceback.
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False, "No graphical desktop session is available."
    try:
        import pystray
        from PIL import Image

        del pystray, Image
    except ImportError:
        return False, "Install the optional pandrator-manager[tray] extra."
    except Exception as error:
        return (
            False,
            "The desktop tray backend could not initialize "
            f"({type(error).__name__}).",
        )
    return True, ""


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
    from ..launcher import installed_launcher

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
) -> Path:
    path = _tray_autostart_path(layout)
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
        content = (
            "@echo off\n"
            f"start \"\" /b \"{executable}\" {argument_text}\n"
        )
    else:
        escaped_command = " ".join(
            str(value).replace("\\", "\\\\").replace(" ", "\\ ")
            for value in command
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


def launch_tray_background(layout: WorkspaceLayout) -> tuple[bool, str]:
    available, reason = tray_available()
    if not available:
        return False, reason
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(_tray_command(layout), **kwargs)
    except OSError as error:
        return False, str(error)
    return True, ""


def _claim_tray_instance(layout: WorkspaceLayout) -> Path | None:
    layout.state.mkdir(parents=True, exist_ok=True)
    path = layout.state / "tray.pid"
    for _attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                pid = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pid = 0
            if pid > 0 and psutil.pid_exists(pid):
                return None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        return path
    return None


class TrayApplication:
    def __init__(self, client: ManagerClient) -> None:
        self.client = client
        self.icon = None

    def _image(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), "#211b2b")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((7, 7, 57, 57), radius=13, fill="#ad8ce8")
        draw.polygon(((22, 18), (48, 32), (22, 46)), fill="#211b2b")
        return image

    def _status_label(self, _item=None) -> str:
        try:
            services = self.client.services()
            healthy = sum(
                1
                for service in services
                if (service.get("health") or {}).get("state") == "healthy"
            )
            return f"Pandrator Manager — {healthy}/{len(services)} healthy"
        except Exception:
            return "Pandrator Manager — unavailable"

    def open_pandrator(self, _icon=None, _item=None) -> None:
        for service in self.client.services():
            if service["id"] == "pandrator.api" and service.get("endpoint"):
                webbrowser.open(service["endpoint"])
                return
        self.open_recovery()

    def open_recovery(self, _icon=None, _item=None) -> None:
        webbrowser.open(self.client.recovery_url())

    def start_pandrator(self, _icon=None, _item=None) -> None:
        self.client.runtime(
            "start",
            ("pandrator.api", "pandrator.worker"),
        )

    def stop_pandrator(self, _icon=None, _item=None) -> None:
        self.client.runtime(
            "stop",
            ("pandrator.worker", "pandrator.api"),
        )

    def quit_tray(self, icon=None, _item=None) -> None:
        selected = icon or self.icon
        if selected is not None:
            selected.stop()

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon(
            "pandrator",
            self._image(),
            "Pandrator Manager",
            menu=pystray.Menu(
                pystray.MenuItem(self._status_label, None, enabled=False),
                pystray.MenuItem("Open Pandrator", self.open_pandrator, default=True),
                pystray.MenuItem("Open setup / recovery", self.open_recovery),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Start Pandrator", self.start_pandrator),
                pystray.MenuItem("Stop Pandrator", self.stop_pandrator),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit tray", self.quit_tray),
            ),
        )
        self.icon.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pandrator-tray")
    parser.add_argument("--workspace", default=str(default_workspace()))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--remove-autostart", action="store_true")
    args = parser.parse_args(argv)
    layout = WorkspaceLayout.from_value(args.workspace)
    if args.install_autostart:
        print(configure_tray_autostart(layout, enabled=True))
        return 0
    if args.remove_autostart:
        print(configure_tray_autostart(layout, enabled=False))
        return 0
    available, reason = tray_available()
    if args.check:
        print("available" if available else f"unavailable: {reason}")
        return 0 if available else 1
    if not available:
        print(f"Pandrator tray is unavailable: {reason}", file=sys.stderr)
        return 1
    instance = _claim_tray_instance(layout)
    if instance is None:
        return 0
    try:
        client = ManagerClient.ensure_running(layout.workspace)
        TrayApplication(client).run()
    finally:
        try:
            if instance.read_text(encoding="ascii").strip() == str(os.getpid()):
                instance.unlink()
        except OSError:
            pass
    return 0
