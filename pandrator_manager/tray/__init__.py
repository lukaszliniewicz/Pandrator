"""Optional non-Qt tray client."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import psutil

from ..client import ManagerClient
from ..context import WorkspaceLayout
from ..desktop import host_process_environment, open_desktop_url
from ..workspace_selection import default_workspace
from .menu import (
    EngineMenuSnapshot,
    build_engine_menu_snapshot,
    unavailable_engine_snapshot,
)

_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def tray_available() -> tuple[bool, str]:
    if (
        sys.platform.startswith("linux")
        and os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    ):
        try:
            from .status_notifier import run_status_notifier

            del run_status_notifier
            return True, ""
        except ImportError:
            pass
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
) -> Path | str:
    path = _tray_autostart_path(layout)
    if sys.platform.startswith("win"):
        import winreg

        suffix = hashlib.sha256(
            str(layout.workspace).encode("utf-8")
        ).hexdigest()[:10]
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
        "env": host_process_environment(),
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


def _tray_instance_path(layout: WorkspaceLayout) -> Path:
    return layout.state / "tray.pid"


def stop_tray_background(
    layout: WorkspaceLayout,
    *,
    timeout_seconds: float = 10,
) -> tuple[bool, str]:
    """Stop this workspace's tray without risking an unrelated reused PID."""

    path = _tray_instance_path(layout)
    try:
        raw_identity = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return False, ""
    try:
        decoded = json.loads(raw_identity)
        if isinstance(decoded, dict):
            pid = int(decoded["pid"])
            expected_create_time = float(decoded["create_time"])
        else:
            pid = int(decoded)
            expected_create_time = None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False, "The tray process identity file is invalid."

    try:
        process = psutil.Process(pid)
        actual_create_time = process.create_time()
        command = process.cmdline()
    except psutil.NoSuchProcess:
        path.unlink(missing_ok=True)
        return False, ""
    except (OSError, psutil.Error) as error:
        return False, f"The tray process identity could not be inspected: {error}"

    if (
        expected_create_time is not None
        and abs(actual_create_time - expected_create_time) > 1e-3
    ):
        return False, "The tray process identity no longer matches."
    normalized = [str(argument).casefold() for argument in command]
    workspace = str(layout.workspace).casefold()
    is_tray = "tray" in normalized or "pandrator_manager.tray" in normalized
    has_workspace = any(argument == workspace for argument in normalized)
    if not is_tray or not has_workspace:
        return False, "The tray process command no longer matches this workspace."

    try:
        process.terminate()
        _gone, alive = psutil.wait_procs(
            [process],
            timeout=max(0.1, timeout_seconds),
        )
        for remaining in alive:
            remaining.kill()
        if alive:
            _gone, still_alive = psutil.wait_procs(
                alive,
                timeout=max(0.1, timeout_seconds),
            )
            if still_alive:
                return False, "The desktop tray did not exit before uninstall."
    except psutil.NoSuchProcess:
        pass
    except (OSError, psutil.Error) as error:
        return False, f"The desktop tray could not be stopped: {error}"
    path.unlink(missing_ok=True)
    return True, ""


def _claim_tray_instance(layout: WorkspaceLayout) -> Path | None:
    layout.state.mkdir(parents=True, exist_ok=True)
    path = _tray_instance_path(layout)
    for _attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                raw_identity = path.read_text(encoding="ascii").strip()
                decoded = json.loads(raw_identity)
                pid = (
                    int(decoded["pid"])
                    if isinstance(decoded, dict)
                    else int(decoded)
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                pid = 0
            if pid > 0 and psutil.pid_exists(pid):
                return None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "create_time": psutil.Process().create_time(),
                    },
                    sort_keys=True,
                )
            )
        return path
    return None


class TrayApplication:
    def __init__(self, client: ManagerClient) -> None:
        self.client = client
        self.icon = None
        self._engine_snapshot = unavailable_engine_snapshot(
            "Loading engine status"
        )
        self._menu_refresh_stop = threading.Event()

    def _image(self):
        from .icon import load_tray_icon

        return load_tray_icon()

    def _status_label(self, _item=None) -> str:
        return f"Pandrator Manager — {self._engine_snapshot.summary}"

    def engine_snapshot(self) -> EngineMenuSnapshot:
        try:
            status = self.client.status()
            snapshot = build_engine_menu_snapshot(
                self.client.components(),
                self.client.services(),
                busy=bool(status.get("active_operation_id")),
            )
        except Exception:
            logging.exception("Could not refresh desktop tray engine status.")
            snapshot = unavailable_engine_snapshot()
        self._engine_snapshot = snapshot
        return snapshot

    def _engine_group_label(self, _item=None) -> str:
        snapshot = self._engine_snapshot
        if not snapshot.available or not snapshot.items:
            return "Speech engines"
        return (
            "Speech engines — "
            f"{snapshot.running_count}/{len(snapshot.items)} running"
        )

    def control_engine(
        self,
        service_id: str,
        action: str,
        icon=None,
        _item=None,
    ) -> None:
        try:
            self.client.runtime(action, (service_id,))
        except Exception:
            logging.exception(
                "Desktop tray could not %s engine %s.",
                action,
                service_id,
            )
        self.engine_snapshot()
        selected = icon or self.icon
        if selected is not None:
            selected.title = self._status_label()
            selected.update_menu()

    def _pystray_engine_items(self):
        import pystray

        snapshot = self._engine_snapshot
        if not snapshot.available:
            return (
                pystray.MenuItem(
                    snapshot.message or "Engine status unavailable",
                    None,
                    enabled=False,
                ),
            )
        if not snapshot.items:
            return (
                pystray.MenuItem(
                    "No optional engines installed",
                    None,
                    enabled=False,
                ),
            )

        result = []
        for engine in snapshot.items:
            if engine.action is None:
                actions = pystray.Menu(
                    pystray.MenuItem(
                        engine.action_label,
                        None,
                        enabled=False,
                    )
                )
            else:
                callback = functools.partial(
                    self.control_engine,
                    engine.service_id,
                    engine.action,
                )
                actions = pystray.Menu(
                    pystray.MenuItem(
                        engine.action_label,
                        callback,
                        enabled=engine.enabled,
                    )
                )
            result.append(
                pystray.MenuItem(
                    f"{engine.label} — {engine.state_label}",
                    actions,
                )
            )
        return tuple(result)

    def _pystray_menu_items(self):
        import pystray

        return (
            pystray.MenuItem(self._status_label, None, enabled=False),
            pystray.MenuItem(
                "Open Pandrator",
                self.open_pandrator,
                default=True,
            ),
            pystray.MenuItem(
                "Open setup / recovery",
                self.open_recovery,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start Pandrator", self.start_pandrator),
            pystray.MenuItem("Stop Pandrator", self.stop_pandrator),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                self._engine_group_label,
                pystray.Menu(self._pystray_engine_items),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit tray", self.quit_tray),
        )

    def _refresh_pystray_menu(self, icon) -> None:
        icon.visible = True
        while not self._menu_refresh_stop.is_set():
            self.engine_snapshot()
            icon.title = self._status_label()
            icon.update_menu()
            if self._menu_refresh_stop.wait(2.5):
                break

    def open_pandrator(self, _icon=None, _item=None) -> None:
        for service in self.client.services():
            if service["id"] == "pandrator.api" and service.get("endpoint"):
                open_desktop_url(service["endpoint"])
                return
        self.open_recovery()

    def open_recovery(self, _icon=None, _item=None) -> None:
        open_desktop_url(self.client.recovery_url())

    def start_pandrator(self, _icon=None, _item=None) -> None:
        self.client.application("start")

    def stop_pandrator(self, _icon=None, _item=None) -> None:
        self.client.application("stop")

    def quit_tray(self, icon=None, _item=None) -> None:
        self._menu_refresh_stop.set()
        selected = icon or self.icon
        if selected is not None:
            selected.stop()

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon(
            "pandrator",
            self._image(),
            "Pandrator Manager",
            menu=pystray.Menu(self._pystray_menu_items),
        )
        self.icon.run(setup=self._refresh_pystray_menu)


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
        application = TrayApplication(client)
        if (
            sys.platform.startswith("linux")
            and os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        ):
            try:
                from .status_notifier import run_status_notifier

                run_status_notifier(
                    {
                        "open_pandrator": application.open_pandrator,
                        "open_recovery": application.open_recovery,
                        "start_pandrator": application.start_pandrator,
                        "stop_pandrator": application.stop_pandrator,
                    },
                    engine_provider=application.engine_snapshot,
                    engine_action=application.control_engine,
                )
                return 0
            except (ImportError, RuntimeError) as error:
                if (
                    os.environ.get("WAYLAND_DISPLAY")
                    or os.environ.get("XDG_SESSION_TYPE", "").casefold()
                    == "wayland"
                    or not os.environ.get("DISPLAY")
                ):
                    print(
                        f"Pandrator native tray is unavailable: {error}",
                        file=sys.stderr,
                    )
                    return 1
        application.run()
    finally:
        try:
            identity = json.loads(instance.read_text(encoding="ascii"))
            if (
                isinstance(identity, dict)
                and int(identity.get("pid", 0)) == os.getpid()
            ):
                instance.unlink()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return 0
