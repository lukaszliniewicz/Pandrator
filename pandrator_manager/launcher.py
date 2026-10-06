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
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .context import WorkspaceLayout
from .desktop import open_desktop_url
from .errors import ManagerError
from .launcher_runtime import (
    LAUNCHER_METADATA_NAME,
    LAUNCHER_SCHEMA_VERSION,
    MAXIMUM_LAUNCHER_BYTES,
    LauncherRuntime,
    _atomic_json,
    _is_link_or_junction,
    _protect_executable,
    _require_real_directory,
    _require_regular_file,
    _sha256,
    current_runtime_executable,
    external_cleanup_runtime,
    install_stable_launcher,
    installed_launcher,
    launcher_filename,
    launcher_metadata_path,
    native_manager_installation,
    runtime_command,
    stable_launcher_path,
    stage_cleanup_launcher,
)
from .network import AccessMode, EndpointExposure
from .workspace_selection import (
    WorkspaceSelectionUnavailable,
    load_remembered_workspace,
    remember_workspace,
    select_workspace_directory,
)

__all__ = [
    'LAUNCHER_METADATA_NAME',
    'LAUNCHER_SCHEMA_VERSION',
    'LauncherRuntime',
    'LauncherWorkspaceResolution',
    'MAXIMUM_LAUNCHER_BYTES',
    '_atomic_json',
    '_is_link_or_junction',
    '_protect_executable',
    '_require_real_directory',
    '_require_regular_file',
    '_sha256',
    'current_runtime_executable',
    'deployment_endpoint',
    'external_cleanup_runtime',
    'install_stable_launcher',
    'installed_launcher',
    'launcher_filename',
    'launcher_metadata_path',
    'main',
    'native_manager_installation',
    'resolve_launcher_workspace',
    'runtime_command',
    'stable_launcher_path',
    'stage_cleanup_launcher',
]


@dataclass(frozen=True, slots=True)
class LauncherWorkspaceResolution:
    """One launcher invocation's canonical workspace decision."""

    workspace: Path | None
    source: str
    warning: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.workspace is None


def _emit_launcher_result(
    payload: dict,
    *,
    result_file: str | os.PathLike[str] | None = None,
) -> None:
    """Emit launcher JSON to a terminal and/or a windowed-build result file."""

    serialized = json.dumps(payload, sort_keys=True)
    if result_file is not None:
        selected = Path(result_file).expanduser().resolve(strict=False)
        selected.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{selected.name}.",
            suffix=".tmp",
            dir=selected.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, selected)
        finally:
            temporary.unlink(missing_ok=True)
    if sys.stdout is not None:
        print(serialized)


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
    from .tls import select_ca_bundle
    from .uninstall import run_uninstall_handoff

    del create_api, run_daemon, run_uninstall_handoff
    static = Path(str(recovery_package)).resolve().parent / "static"
    assets = tuple(
        name
        for name in ("index.html", "app.js", "styles.css")
        if (static / name).is_file()
    )
    ca_bundle = select_ca_bundle()
    return {
        "ok": len(assets) == 3 and ca_bundle.path.is_file(),
        "service": "pandrator-manager-launcher",
        "manager_version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "recovery_assets": list(assets),
        "ca_bundle": ca_bundle.diagnostic_payload(),
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
    setup.add_argument("--result-file", help=argparse.SUPPRESS)
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
    start.add_argument("--result-file", help=argparse.SUPPRESS)

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

    self_check = subparsers.add_parser(
        "self-check",
        help="Validate the packaged launcher.",
    )
    self_check.add_argument("--result-file", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["setup"]
    args = _parser().parse_args(arguments)

    if args.command == "self-check":
        report = _self_check()
        _emit_launcher_result(report, result_file=args.result_file)
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
        _emit_launcher_result(
            {
                "status": "cancelled",
                "reason": "workspace_selection_cancelled",
            },
            result_file=getattr(args, "result_file", None),
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
        open_desktop_url(recovery_url)
        if recovery_url and should_open_browser
        else False
    )
    tray_started = False
    if getattr(sys, "frozen", False) and should_open_browser:
        from .tray import launch_tray_background

        tray_started, tray_reason = launch_tray_background(layout)
        if tray_reason and should_open_browser:
            warnings.append(f"The desktop tray did not start: {tray_reason}")
    _emit_launcher_result(
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
        result_file=getattr(args, "result_file", None),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
