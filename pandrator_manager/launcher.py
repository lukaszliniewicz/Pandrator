"""Stable native bootstrap and handoff launcher.

The manager's versioned private runtime is deliberately replaceable.  Native
installations therefore keep one small, Qt-free executable in ``bin`` that can
start the active manager, coordinate a manager-version handoff, and copy itself
outside the managed root before whole-product uninstall.

When this module runs from a normal Python installation it remains useful as a
testable entry point, but it does not pretend that the Python interpreter is a
self-contained native bootstrap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from . import __version__
from .auth import protect_path
from .context import WorkspaceLayout
from .errors import ManagerError
from .network import AccessMode, EndpointExposure
from .workspace_selection import (
    WorkspaceSelectionUnavailable,
    load_remembered_workspace,
    remember_workspace,
    select_workspace_directory,
)

LAUNCHER_METADATA_NAME = "launcher.json"
LAUNCHER_SCHEMA_VERSION = 1
MAXIMUM_LAUNCHER_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LauncherRuntime:
    """A runtime that can survive the operation it coordinates."""

    mode: Literal["native_launcher", "python"]
    executable: Path
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class LauncherWorkspaceResolution:
    """One launcher invocation's canonical workspace decision."""

    workspace: Path | None
    source: str
    warning: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.workspace is None


def current_runtime_executable() -> Path:
    """Return the invocation path without dereferencing a virtualenv symlink."""

    return Path(os.path.abspath(os.path.expanduser(sys.executable)))


def launcher_filename() -> str:
    return (
        "pandrator-manager-launcher.exe"
        if os.name == "nt"
        else "pandrator-manager-launcher"
    )


def stable_launcher_path(layout: WorkspaceLayout) -> Path:
    return layout.bin / launcher_filename()


def launcher_metadata_path(layout: WorkspaceLayout) -> Path:
    return layout.bin / LAUNCHER_METADATA_NAME


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(
        junction is not None and junction()
    )


def _require_real_directory(path: Path, *, create: bool = False) -> Path:
    if create and not os.path.lexists(path):
        path.mkdir(parents=True, mode=0o700)
    if (
        not os.path.lexists(path)
        or _is_link_or_junction(path)
        or not path.is_dir()
    ):
        raise ManagerError(
            "unsafe_stable_launcher",
            "The stable launcher directory is missing or redirected.",
            {"path": str(path)},
            409,
        )
    lexical = Path(os.path.abspath(os.fspath(path)))
    if path.resolve(strict=True) != lexical:
        raise ManagerError(
            "unsafe_stable_launcher",
            "The stable launcher directory resolves unexpectedly.",
            {"path": str(path)},
            409,
        )
    return path


def _require_regular_file(path: Path, *, description: str) -> None:
    if (
        not os.path.lexists(path)
        or _is_link_or_junction(path)
        or not path.is_file()
    ):
        raise ManagerError(
            "unsafe_stable_launcher",
            f"The {description} is not a regular file.",
            {"path": str(path)},
            409,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _protect_executable(path: Path) -> None:
    if os.name == "nt":
        protect_path(path)
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def installed_launcher(
    layout: WorkspaceLayout,
    *,
    strict: bool = False,
) -> LauncherRuntime | None:
    """Return the installed launcher only when its local manifest matches."""

    executable = stable_launcher_path(layout)
    metadata = launcher_metadata_path(layout)
    if not os.path.lexists(executable) and not os.path.lexists(metadata):
        return None
    try:
        _require_real_directory(layout.bin)
        _require_regular_file(
            executable,
            description="stable launcher executable",
        )
        _require_regular_file(
            metadata,
            description="stable launcher metadata",
        )
        raw = json.loads(metadata.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != LAUNCHER_SCHEMA_VERSION
            or raw.get("filename") != executable.name
            or not isinstance(raw.get("sha256"), str)
            or len(raw["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in raw["sha256"])
            or not isinstance(raw.get("size_bytes"), int)
            or raw["size_bytes"] <= 0
            or raw["size_bytes"] > MAXIMUM_LAUNCHER_BYTES
        ):
            raise ValueError("launcher metadata is invalid")
        metadata_workspace = raw.get("workspace")
        if metadata_workspace is not None and (
            not isinstance(metadata_workspace, str)
            or Path(metadata_workspace).expanduser().resolve(strict=False)
            != layout.workspace
        ):
            raise ValueError("launcher workspace does not match metadata")
        if executable.stat().st_size != raw["size_bytes"]:
            raise ValueError("launcher size does not match metadata")
        digest = _sha256(executable)
        if digest != raw["sha256"]:
            raise ValueError("launcher digest does not match metadata")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise ValueError("launcher is not executable")
        return LauncherRuntime(
            mode="native_launcher",
            executable=executable.resolve(strict=True),
            sha256=digest,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        if strict:
            raise ManagerError(
                "invalid_stable_launcher",
                "The installed stable launcher failed validation.",
                {
                    "path": str(executable),
                    "reason": str(error),
                },
                409,
            ) from error
        return None


def install_stable_launcher(
    layout: WorkspaceLayout,
    *,
    source: Path | None = None,
) -> LauncherRuntime:
    """Install the current frozen bootstrap atomically into ``bin``.

    ``source`` is injectable for packaging tests.  Production callers omit it;
    a source-mode Python interpreter is never copied and mislabeled as a native
    launcher.
    """

    if source is None:
        if not bool(getattr(sys, "frozen", False)):
            raise ManagerError(
                "native_bootstrap_required",
                "Installing the stable launcher requires the packaged native "
                "bootstrap executable.",
                {"executable": str(Path(sys.executable).resolve(strict=False))},
                409,
            )
        source = Path(sys.executable)
    selected_source = source.expanduser().resolve(strict=True)
    _require_regular_file(
        selected_source,
        description="bootstrap source executable",
    )
    size = selected_source.stat().st_size
    if size <= 0 or size > MAXIMUM_LAUNCHER_BYTES:
        raise ManagerError(
            "invalid_stable_launcher",
            "The bootstrap executable has an invalid size.",
            {"path": str(selected_source), "size_bytes": size},
            409,
        )
    source_digest = _sha256(selected_source)

    layout.bin.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(layout.bin)
    protect_path(layout.bin, directory=True)
    destination = stable_launcher_path(layout)
    if destination.resolve(strict=False) != selected_source:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=layout.bin,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(selected_source, temporary)
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            _protect_executable(temporary)
            if (
                temporary.stat().st_size != size
                or _sha256(temporary) != source_digest
            ):
                raise RuntimeError(
                    "The staged launcher copy failed digest verification."
                )
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    _protect_executable(destination)
    _atomic_json(
        launcher_metadata_path(layout),
        {
            "schema_version": LAUNCHER_SCHEMA_VERSION,
            "filename": destination.name,
            "sha256": source_digest,
            "size_bytes": size,
            "manager_version": __version__,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "authenticode_signed": False if os.name == "nt" else None,
            "workspace": str(layout.workspace),
        },
    )
    protect_path(launcher_metadata_path(layout))
    runtime = installed_launcher(layout, strict=True)
    assert runtime is not None
    return runtime


def external_cleanup_runtime(
    layout: WorkspaceLayout,
) -> LauncherRuntime | None:
    """Select a cleanup runtime that will remain after ``layout.root`` moves."""

    stable = installed_launcher(layout)
    if stable is not None:
        return stable
    executable = current_runtime_executable()
    if layout.contains(layout.root, executable):
        return None
    return LauncherRuntime(
        mode=(
            "native_launcher"
            if bool(getattr(sys, "frozen", False))
            else "python"
        ),
        executable=executable,
        sha256=_sha256(executable) if bool(getattr(sys, "frozen", False)) else None,
    )


def native_manager_installation(layout: WorkspaceLayout) -> bool:
    """Whether this workspace is controlled by the native release channel."""

    if installed_launcher(layout) is not None:
        return True
    executable = current_runtime_executable().resolve(strict=False)
    return layout.contains(layout.manager_versions, executable)


def stage_cleanup_launcher(
    runtime: LauncherRuntime,
    destination: Path,
) -> LauncherRuntime:
    """Copy a native launcher to an external, operation-specific path."""

    if runtime.mode != "native_launcher":
        raise ValueError("Only a native launcher can be staged for cleanup.")
    _require_regular_file(
        runtime.executable,
        description="cleanup launcher source",
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_real_directory(destination.parent)
    if os.path.lexists(destination):
        _require_regular_file(
            destination,
            description="staged cleanup launcher",
        )
        digest = _sha256(destination)
        expected = runtime.sha256 or _sha256(runtime.executable)
        if digest != expected:
            raise ManagerError(
                "invalid_stable_launcher",
                "The existing staged cleanup launcher has another digest.",
                {"path": str(destination)},
                409,
            )
        _protect_executable(destination)
        return LauncherRuntime(
            mode="native_launcher",
            executable=destination.resolve(strict=True),
            sha256=digest,
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(runtime.executable, temporary)
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _protect_executable(temporary)
        expected = runtime.sha256 or _sha256(runtime.executable)
        digest = _sha256(temporary)
        if digest != expected:
            raise RuntimeError(
                "The external cleanup launcher failed digest verification."
            )
        os.replace(temporary, destination)
        _protect_executable(destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return LauncherRuntime(
        mode="native_launcher",
        executable=destination.resolve(strict=True),
        sha256=digest,
    )


def runtime_command(
    runtime: LauncherRuntime,
    *,
    action: Literal["daemon", "handoff", "uninstall"],
    workspace: Path,
    operation_id: str | None = None,
) -> list[str]:
    if runtime.mode == "native_launcher":
        command = [
            str(runtime.executable),
            action,
            "--workspace",
            str(workspace),
        ]
    else:
        module = {
            "daemon": "pandrator_manager.daemon",
            "handoff": "pandrator_manager.releases.handoff",
            "uninstall": "pandrator_manager.uninstall",
        }[action]
        command = [
            str(runtime.executable),
            "-m",
            module,
            "--workspace",
            str(workspace),
        ]
    if operation_id is not None:
        command.extend(("--operation-id", operation_id))
    return command


def _process_options() -> dict:
    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        }
    return {"start_new_session": True}


def _self_check() -> dict:
    from .api import create_api
    from .daemon import run_daemon
    from .recovery_ui import __file__ as recovery_package
    from .uninstall import run_uninstall_handoff

    del create_api, run_daemon, run_uninstall_handoff
    static = Path(str(recovery_package)).resolve().parent / "static"
    assets = tuple(
        name
        for name in ("index.html", "app.js", "styles.css")
        if (static / name).is_file()
    )
    return {
        "ok": len(assets) == 3,
        "service": "pandrator-manager-launcher",
        "manager_version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "recovery_assets": list(assets),
        "authenticode_signed": False if os.name == "nt" else None,
    }


def deployment_endpoint(
    raw_url: str | None,
    current: EndpointExposure,
    *,
    bind_host: str | None,
    configured_port: int | None,
    default_port: int,
    trusted_proxy_hops: int,
    allow_insecure_private_network: bool,
) -> EndpointExposure:
    """Resolve one setup CLI endpoint into a validated network profile.

    Keeping this conversion independent of ``main`` makes first-run server
    configuration testable without installing or starting the native launcher.
    The model remains the authoritative validation boundary.
    """

    if not raw_url:
        if configured_port is None:
            return current
        return EndpointExposure.model_validate(
            {
                **current.model_dump(mode="python"),
                "port": configured_port,
            }
        )
    public_url = str(raw_url).strip().rstrip("/")
    parsed = urlsplit(public_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Remote URLs must begin with http:// or https://.")
    try:
        external_port = parsed.port
    except ValueError as error:
        raise ValueError("Remote URL contains an invalid port.") from error
    if parsed.scheme == "http" and not allow_insecure_private_network:
        raise ValueError(
            "An http:// remote URL requires "
            "--allow-insecure-private-network."
        )
    if parsed.scheme == "http":
        external_port = external_port or 80
        internal_port = configured_port or external_port
        if internal_port != external_port:
            raise ValueError(
                "A private-network URL port must match the listening "
                "port because no reverse proxy is configured."
            )
        mode = AccessMode.PRIVATE_NETWORK
        selected_bind_host = bind_host or "0.0.0.0"
        proxy_hops = 0
        insecure = True
    else:
        internal_port = configured_port or default_port
        mode = AccessMode.HTTPS_PROXY
        selected_bind_host = bind_host or "127.0.0.1"
        proxy_hops = trusted_proxy_hops
        insecure = False
    return EndpointExposure(
        mode=mode,
        bind_host=selected_bind_host,
        port=internal_port,
        public_url=public_url,
        proxy_hops=proxy_hops,
        allow_insecure_remote=insecure,
    )


def _installed_launcher_workspace(
    executable: Path | None = None,
) -> Path | None:
    """Infer the workspace only from a validated stable-launcher location."""

    if executable is None and not bool(getattr(sys, "frozen", False)):
        return None
    selected = (
        executable if executable is not None else current_runtime_executable()
    ).expanduser().resolve(strict=False)
    if (
        selected.name.casefold() != launcher_filename().casefold()
        or selected.parent.name.casefold() != "bin"
        or selected.parent.parent.name.casefold() != "pandrator"
    ):
        return None
    workspace = selected.parent.parent.parent
    try:
        layout = WorkspaceLayout.from_value(workspace)
        installed = installed_launcher(layout)
        if (
            installed is not None
            and installed.executable.resolve(strict=False) == selected
        ):
            return layout.workspace
    except (ManagerError, OSError):
        return None
    return None


def resolve_launcher_workspace(
    explicit: str | os.PathLike[str] | None,
    *,
    command: str,
    choose_workspace: bool = False,
    allow_selection: bool = True,
    environ: dict[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    executable: Path | None = None,
) -> LauncherWorkspaceResolution:
    """Resolve a launcher workspace without letting later runs drift home.

    Explicit CLI and environment choices are authoritative.  An installed
    stable launcher then prefers its own validated location over the global
    "last selected" preference, allowing multiple installations to coexist.
    Only an interactive setup with no prior choice opens the native picker.
    """

    values = os.environ if environ is None else environ
    selected_home = Path(home if home is not None else Path.home())
    selected_home = selected_home.expanduser().resolve(strict=False)
    explicit_value = str(explicit or "").strip()
    environment_value = str(values.get("PANDRATOR_WORKSPACE") or "").strip()

    if explicit_value:
        return LauncherWorkspaceResolution(
            Path(explicit_value).expanduser().resolve(strict=False),
            "command_line",
        )

    candidates = (
        (
            Path(environment_value).expanduser().resolve(strict=False),
            "environment",
        )
        if environment_value
        else None
    )
    if candidates is not None and not choose_workspace:
        return LauncherWorkspaceResolution(*candidates)

    installed = _installed_launcher_workspace(executable)
    remembered = load_remembered_workspace(
        environ=values,
        home=selected_home,
    )
    # Windows can surface the same directory through an 8.3 alias (for
    # example RUNNER~1) or its long name.  Normalize every persisted or
    # launcher-derived candidate just as we do CLI and environment values so
    # later comparisons, ownership checks, and preference writes cannot drift
    # between the two spellings.
    if installed is not None:
        installed = installed.expanduser().resolve(strict=False)
    if remembered is not None:
        remembered = remembered.expanduser().resolve(strict=False)
    if not choose_workspace:
        if installed is not None:
            return LauncherWorkspaceResolution(installed, "installed_launcher")
        if remembered is not None:
            return LauncherWorkspaceResolution(remembered, "remembered")

    should_select = command == "setup" and (
        choose_workspace
        or (
            allow_selection
            and candidates is None
            and installed is None
            and remembered is None
        )
    )
    if should_select:
        initial = (
            (candidates[0] if candidates is not None else None)
            or installed
            or remembered
            or selected_home
        )
        try:
            selected = select_workspace_directory(
                initial,
                environ=values,
            )
        except WorkspaceSelectionUnavailable as error:
            if choose_workspace:
                raise ManagerError(
                    "workspace_selection_unavailable",
                    "The installation folder chooser could not be opened. "
                    "Run setup with --workspace followed by the desired "
                    "parent folder.",
                    {"reason": str(error)},
                    409,
                ) from error
            return LauncherWorkspaceResolution(
                selected_home,
                "default",
                warning=(
                    "The installation folder chooser was unavailable; "
                    f"using {selected_home}. Pass --workspace to choose "
                    "another parent folder."
                ),
            )
        if selected is None:
            return LauncherWorkspaceResolution(None, "cancelled")
        return LauncherWorkspaceResolution(
            selected.expanduser().resolve(strict=False),
            "folder_chooser",
        )

    if candidates is not None:
        return LauncherWorkspaceResolution(*candidates)
    if installed is not None:
        return LauncherWorkspaceResolution(installed, "installed_launcher")
    if remembered is not None:
        return LauncherWorkspaceResolution(remembered, "remembered")
    return LauncherWorkspaceResolution(selected_home, "default")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pandrator-manager-launcher",
        description="Bootstrap and recover the Pandrator Manager.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{setup,start,tray,self-check}",
    )

    setup = subparsers.add_parser(
        "setup",
        help="Install this native launcher and open setup/recovery.",
    )
    setup_location = setup.add_mutually_exclusive_group()
    setup_location.add_argument(
        "--workspace",
        help=(
            "Parent directory in which the Pandrator folder is created. "
            "The choice is remembered for future launcher and CLI runs."
        ),
    )
    setup_location.add_argument(
        "--choose-workspace",
        action="store_true",
        help="Open the native installation-folder chooser even when a choice is remembered.",
    )
    setup.add_argument("--autostart", action="store_true")
    setup.add_argument("--no-open", action="store_true")
    setup.add_argument(
        "--remote-setup-url",
        help=(
            "Exact workstation-facing URL for remotely opening setup/recovery "
            "(for example https://setup.example or http://server.local:8098)."
        ),
    )
    setup.add_argument(
        "--remote-pandrator-url",
        help=(
            "Exact workstation-facing URL for Pandrator. Remote first startup "
            "also requires PANDRATOR_OWNER_PASSWORD."
        ),
    )
    setup.add_argument(
        "--manager-port",
        type=int,
        help="Internal manager port (default 8098 for a remote setup URL).",
    )
    setup.add_argument(
        "--pandrator-port",
        type=int,
        help="Internal Pandrator port (default 8097).",
    )
    setup.add_argument(
        "--network-bind-host",
        help=(
            "Listening IP for remote services. Private HTTP defaults to "
            "0.0.0.0; HTTPS proxy mode defaults to 127.0.0.1. Pod ingress "
            "usually requires an explicit 0.0.0.0."
        ),
    )
    setup.add_argument(
        "--trusted-proxy-hops",
        type=int,
        choices=range(1, 4),
        default=1,
        help="Number of operated reverse-proxy/ingress hops for HTTPS URLs.",
    )
    setup.add_argument(
        "--allow-insecure-private-network",
        action="store_true",
        help="Acknowledge that an http:// remote URL is suitable only for a trusted LAN/VPN.",
    )

    start = subparsers.add_parser("start", help="Start or connect to the manager.")
    start.add_argument(
        "--workspace",
        help="Override the remembered parent directory for this launch.",
    )
    start.add_argument("--open-recovery", action="store_true")

    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--workspace", required=True)
    daemon.add_argument("--port", type=int)
    daemon.add_argument("--handoff-child")

    tray = subparsers.add_parser("tray", help="Run the desktop tray client.")
    tray.add_argument("--workspace", required=True)
    tray.add_argument("--check", action="store_true")
    tray.add_argument("--install-autostart", action="store_true")
    tray.add_argument("--remove-autostart", action="store_true")

    for command in ("handoff", "uninstall"):
        helper = subparsers.add_parser(command)
        helper.add_argument("--workspace", required=True)
        helper.add_argument("--operation-id", required=True)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--workspace", required=True)
    probe.add_argument("--operation-id", required=True)
    probe.add_argument("--probe-database", type=Path, required=True)
    probe.add_argument("--expected-version", required=True)

    subparsers.add_parser("self-check", help="Validate the packaged launcher.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["setup"]
    args = _parser().parse_args(arguments)

    if args.command == "self-check":
        report = _self_check()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ok"] else 2
    if args.command == "daemon":
        from .daemon import run_daemon

        return run_daemon(
            args.workspace,
            port=args.port,
            handoff_child=args.handoff_child,
        )
    if args.command == "tray":
        from .tray import main as tray_main

        tray_arguments = ["--workspace", args.workspace]
        if args.check:
            tray_arguments.append("--check")
        if args.install_autostart:
            tray_arguments.append("--install-autostart")
        if args.remove_autostart:
            tray_arguments.append("--remove-autostart")
        return tray_main(tray_arguments)
    if args.command == "handoff":
        from .releases.handoff import run_handoff

        return run_handoff(args.workspace, args.operation_id)
    if args.command == "uninstall":
        from .uninstall import run_uninstall_handoff

        return run_uninstall_handoff(args.workspace, args.operation_id)
    if args.command == "probe":
        from .releases.handoff import probe_runtime

        return probe_runtime(
            args.workspace,
            args.probe_database,
            args.expected_version,
            args.operation_id,
        )

    resolution = resolve_launcher_workspace(
        getattr(args, "workspace", None),
        command=args.command,
        choose_workspace=bool(getattr(args, "choose_workspace", False)),
        allow_selection=not bool(getattr(args, "no_open", False)),
    )
    if resolution.cancelled:
        print(
            json.dumps(
                {
                    "status": "cancelled",
                    "reason": "workspace_selection_cancelled",
                },
                sort_keys=True,
            )
        )
        return 0

    assert resolution.workspace is not None
    layout = WorkspaceLayout.from_value(resolution.workspace)
    from .client import ManagerClient

    installed = None
    settings_path = None
    warnings = [resolution.warning] if resolution.warning else []
    if args.command == "setup":
        # Pod/server deployments can provide the validated network profile via
        # environment variables for the first native launch.  Persist the
        # resulting non-secret policy so later autostart and manager handoffs
        # retain the same bind/public URLs without retaining credentials.
        from .network import (
            NetworkConfiguration,
            load_network_configuration,
            save_network_configuration,
        )

        configured_network = load_network_configuration(layout)

        configured_network = NetworkConfiguration(
            manager=deployment_endpoint(
                args.remote_setup_url,
                configured_network.manager,
                bind_host=args.network_bind_host,
                configured_port=args.manager_port,
                default_port=8098,
                trusted_proxy_hops=args.trusted_proxy_hops,
                allow_insecure_private_network=(
                    args.allow_insecure_private_network
                ),
            ),
            application=deployment_endpoint(
                args.remote_pandrator_url,
                configured_network.application,
                bind_host=args.network_bind_host,
                configured_port=args.pandrator_port,
                default_port=8097,
                trusted_proxy_hops=args.trusted_proxy_hops,
                allow_insecure_private_network=(
                    args.allow_insecure_private_network
                ),
            ),
        )
        installed = install_stable_launcher(layout)
        save_network_configuration(
            layout,
            configured_network,
        )
        try:
            settings_path = remember_workspace(layout.workspace)
        except (OSError, RuntimeError, ValueError) as error:
            warnings.append(
                "Pandrator will use the selected location now, but the "
                f"launcher could not remember it for future runs: {error}"
            )
        if args.autostart:
            from .autostart import autostart_adapter

            autostart_adapter(layout).install(activate=True)
        if getattr(sys, "frozen", False):
            try:
                from .tray import configure_tray_autostart

                configure_tray_autostart(layout, enabled=True)
            except (OSError, RuntimeError, ValueError) as error:
                warnings.append(
                    f"The desktop tray could not be registered for login: {error}"
                )
    client = ManagerClient.ensure_running(layout.workspace)
    should_prepare_recovery = (
        args.command == "setup" or bool(args.open_recovery)
    )
    should_open_browser = (
        not args.no_open
        if args.command == "setup"
        else bool(args.open_recovery)
    )
    recovery_url = (
        client.recovery_url() if should_prepare_recovery else None
    )
    opened = (
        bool(webbrowser.open(recovery_url))
        if recovery_url and should_open_browser
        else False
    )
    tray_started = False
    if getattr(sys, "frozen", False) and should_open_browser:
        from .tray import launch_tray_background

        tray_started, tray_reason = launch_tray_background(layout)
        if tray_reason and should_open_browser:
            warnings.append(f"The desktop tray did not start: {tray_reason}")
    print(
        json.dumps(
            {
                "status": "ready",
                "workspace": str(layout.workspace),
                "launcher": (
                    str(installed.executable)
                    if installed is not None
                    else (
                        str(current.executable)
                        if (current := installed_launcher(layout)) is not None
                        else None
                    )
                ),
                "recovery_url": recovery_url if not opened else None,
                "browser_opened": opened,
                "tray_started": tray_started,
                "workspace_source": resolution.source,
                "workspace_remembered": (
                    settings_path is not None
                    or resolution.source in {"remembered", "installed_launcher"}
                ),
                "workspace_settings": (
                    str(settings_path) if settings_path is not None else None
                ),
                "warnings": warnings,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
