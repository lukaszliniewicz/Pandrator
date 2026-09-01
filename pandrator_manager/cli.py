"""Thin API-client CLI with stopped-manager bootstrap and recovery commands."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from pydantic import ValidationError

from .autostart import autostart_adapter
from .client import (
    ManagerApiError,
    ManagerClient,
    ManagerUnavailable,
    ProductUninstalled,
)
from .context import WorkspaceLayout
from .daemon import run_daemon
from .desktop import open_desktop_url
from .errors import ManagerError
from .models import (
    TERMINAL_OPERATION_STATES,
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
    OperationRecord,
)
from .runtime_specs import application_root, runtime_python
from .uninstall import (
    clear_uninstall_status,
    read_uninstall_status,
)
from .workspace_selection import default_workspace


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pandrator-manager",
        description="Install, supervise, update, repair, and recover Pandrator.",
    )
    parser.add_argument(
        "--workspace",
        default=str(default_workspace()),
        help="Parent directory containing the Pandrator workspace.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show manager state.")
    doctor = subparsers.add_parser(
        "doctor",
        help="Run non-mutating installation diagnostics.",
    )
    doctor.add_argument(
        "--errors-only",
        action="store_true",
        help="Show only warnings and errors.",
    )
    subparsers.add_parser(
        "legacy",
        help="Inspect a legacy installer workspace without changing it.",
    )
    legacy_import = subparsers.add_parser(
        "legacy-import",
        help="Review or import positively identified legacy state.",
    )
    legacy_import.add_argument("--source-digest")
    legacy_import.add_argument("--idempotency-key")
    legacy_import.add_argument(
        "--yes",
        action="store_true",
        help="Confirm import of the exact inspected legacy configuration.",
    )
    subparsers.add_parser(
        "releases",
        help="Show accepted application and manager release slots.",
    )
    subparsers.add_parser("list", help="List component definitions and state.")
    probe = subparsers.add_parser("probe", help="Inspect component state.")
    probe.add_argument("components", nargs="*")

    daemon = subparsers.add_parser("daemon", help="Run the manager in the foreground.")
    daemon.add_argument("--port", type=int)
    daemon.add_argument("--no-silero", action="store_true")
    subparsers.add_parser("start-manager", help="Start or connect to the manager.")
    subparsers.add_parser("stop-manager", help="Stop only the manager daemon.")

    mcp_config = subparsers.add_parser(
        "mcp-config",
        help="Print a local HTTP MCP configuration for an agent host.",
    )
    mcp_config.add_argument(
        "host",
        choices=("codex", "claude-code", "opencode", "antigravity"),
    )
    mcp_config.add_argument(
        "--server-name",
        default="pandrator",
        help="Override the host-visible MCP server name.",
    )
    mcp_config.add_argument(
        "--include-credential",
        action="store_true",
        help=(
            "Acknowledge that the generated fragment contains a secret and "
            "must be stored in a private user configuration."
        ),
    )

    mcp_paths = subparsers.add_parser(
        "mcp-paths",
        help="Manage the local directories exposed to the managed MCP.",
    )
    mcp_path_commands = mcp_paths.add_subparsers(
        dest="mcp_path_command",
        required=True,
    )
    mcp_path_commands.add_parser("list", help="List approved local paths.")
    source_add = mcp_path_commands.add_parser(
        "source-add",
        help="Expose one local input directory under an opaque name.",
    )
    source_add.add_argument("name")
    source_add.add_argument("path", type=Path)
    source_add.add_argument("--replace", action="store_true")
    source_remove = mcp_path_commands.add_parser(
        "source-remove",
        help="Stop exposing one named local input directory.",
    )
    source_remove.add_argument("name")
    output_set = mcp_path_commands.add_parser(
        "output-set",
        help="Choose the local directory for downloaded artifacts.",
    )
    output_set.add_argument("path", type=Path)
    mcp_path_commands.add_parser(
        "output-clear",
        help="Disable local artifact materialization.",
    )

    for command in ("release-plan", "release-update"):
        release = subparsers.add_parser(
            command,
            help=(
                "Review a signed product release plan."
                if command == "release-plan"
                else "Review or execute a signed product release."
            ),
        )
        release.add_argument(
            "--manifest",
            type=Path,
            required=True,
            help="Path to the signed JSON release manifest.",
        )
        release.add_argument("--expected-revision", type=int)
        release.add_argument("--offline", action="store_true")
        release.add_argument(
            "--keep-stopped",
            action="store_true",
            help="Health-check the application but leave it stopped afterward.",
        )
        release.add_argument("--idempotency-key")
        release.add_argument("--yes", action="store_true")
        release.add_argument("--wait", action="store_true")

    uninstall = subparsers.add_parser(
        "uninstall",
        help="Review or execute complete product uninstall.",
    )
    uninstall_mode = uninstall.add_mutually_exclusive_group()
    uninstall_mode.add_argument(
        "--preserve-data",
        action="store_true",
        help="Preserve user data (the default).",
    )
    uninstall_mode.add_argument(
        "--purge-data",
        action="store_true",
        help="Permanently remove user data after any requested export.",
    )
    uninstall.add_argument(
        "--export-data",
        type=Path,
        help="Create a verified ZIP archive at this new path before removal.",
    )
    uninstall.add_argument("--expected-revision", type=int)
    uninstall.add_argument("--idempotency-key")
    uninstall.add_argument(
        "--yes",
        action="store_true",
        help="Accept the exact destructive confirmations and execute.",
    )
    uninstall.add_argument("--wait", action="store_true")

    open_command = subparsers.add_parser(
        "open",
        help="Open Pandrator or the setup/recovery interface.",
    )
    open_command.add_argument("--recovery", action="store_true")

    plan_help = {
        "plan": "Prepare an exact operation plan.",
        "install": "Prepare an installation plan.",
        "update": "Prepare an update plan.",
        "repair": "Prepare a repair plan.",
        "remove": "Prepare a removal plan.",
    }
    for command, kind in (
        ("plan", None),
        ("install", OperationKind.INSTALL),
        ("update", OperationKind.UPDATE),
        ("repair", OperationKind.REPAIR),
        ("remove", OperationKind.REMOVE),
    ):
        plan = subparsers.add_parser(command, help=plan_help[command])
        if kind is None:
            plan.add_argument(
                "--kind",
                choices=[value.value for value in OperationKind],
                default=OperationKind.INSTALL.value,
            )
        plan.add_argument(
            "--component",
            action="append",
            default=[],
            metavar="ID[:COMPUTE[:QUANTIZATION]]",
        )
        plan.add_argument("--absent", action="append", default=[], metavar="ID")
        plan.add_argument("--expected-revision", type=int)
        plan.add_argument("--idempotency-key")
        plan.add_argument(
            "--yes",
            action="store_true",
            help="Accept all listed confirmations and execute the exact plan.",
        )
        plan.add_argument("--wait", action="store_true")
        plan.set_defaults(operation_kind=kind)

    for action in ("start", "stop", "restart"):
        runtime = subparsers.add_parser(
            f"runtime-{action}",
            help=f"{action.title()} selected managed services.",
        )
        runtime.add_argument("services", nargs="*")
        runtime.add_argument("--idempotency-key")
        runtime.set_defaults(runtime_action=action)

    operations = subparsers.add_parser("operations", help="List durable operations.")
    operations.add_argument("--active", action="store_true")
    operation = subparsers.add_parser("operation", help="Show one durable operation.")
    operation.add_argument("operation_id")
    cancel = subparsers.add_parser("cancel", help="Request operation cancellation.")
    cancel.add_argument("operation_id")
    cancel.add_argument("--idempotency-key")

    automation_client = subparsers.add_parser(
        "automation-client",
        help=(
            "List or revoke Manager recovery automation clients as the "
            "local workspace owner."
        ),
    )
    automation_client_commands = automation_client.add_subparsers(
        dest="automation_client_command",
        required=True,
    )
    automation_client_commands.add_parser("list")
    automation_client_revoke = automation_client_commands.add_parser(
        "revoke"
    )
    automation_client_revoke.add_argument("client_id", type=uuid.UUID)
    automation_client_revoke.add_argument("--idempotency-key")
    automation_client_revoke.add_argument(
        "--yes",
        action="store_true",
        help="Confirm revocation of the client and all of its tokens.",
    )

    autostart = subparsers.add_parser(
        "autostart",
        help="Configure per-user manager startup.",
    )
    autostart.add_argument("action", choices=("status", "enable", "disable"))
    autostart.add_argument(
        "--no-activate",
        action="store_true",
        help="Write integration files without enabling them now.",
    )
    return parser


def _desired(
    args: argparse.Namespace,
    *,
    listed_components_present: bool = True,
) -> dict[str, DesiredComponentState]:
    desired: dict[str, DesiredComponentState] = {}
    for value in args.component:
        parts = value.split(":", 2)
        component_id = parts[0].strip()
        if not component_id:
            raise ValueError("Component ID cannot be empty.")
        compute = (
            ComputeVariant(parts[1].lower())
            if len(parts) > 1 and parts[1]
            else ComputeVariant.AUTO
        )
        quantization = parts[2].strip() or None if len(parts) > 2 else None
        desired[component_id] = DesiredComponentState(
            present=listed_components_present,
            compute=compute,
            quantization=quantization,
        )
    for component_id in args.absent:
        selected = str(component_id).strip()
        if not selected:
            raise ValueError("Absent component ID cannot be empty.")
        desired[selected] = DesiredComponentState(
            present=False,
            compute=ComputeVariant.AUTO,
        )
    if not desired:
        raise ValueError("At least one --component or --absent value is required.")
    return desired


def _render(payload: Any, *, as_json: bool) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if isinstance(payload, list) and payload and "definition" in payload[0]:
        for item in payload:
            definition = item.get("definition", {})
            inspection = item.get("inspection", {})
            print(
                f"{definition.get('id', '?'):20} "
                f"{inspection.get('state', 'unknown'):12} "
                f"{definition.get('label', '')}"
            )
        return
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _wait_for_operation(
    client: ManagerClient,
    operation: OperationRecord,
) -> OperationRecord | dict[str, Any]:
    uninstall_resume: subprocess.Popen | None = None
    while operation.state not in TERMINAL_OPERATION_STATES:
        if operation.kind == OperationKind.UNINSTALL:
            status = read_uninstall_status(
                client.layout,
                operation.id,
            )
            if status is not None:
                state = str(status.get("status") or "")
                if state.startswith("succeeded"):
                    finalized = clear_uninstall_status(
                        client.layout,
                        operation.id,
                    )
                    return finalized or status
                if state in {"failed", "recovery_required"}:
                    return status
                if state == "cleanup_interrupted":
                    if (
                        uninstall_resume is None
                        or uninstall_resume.poll() is not None
                    ):
                        uninstall_resume = ManagerClient.start_daemon(
                            client.layout.workspace
                        )
                    time.sleep(0.2)
                    continue
        time.sleep(0.5)
        try:
            operation = OperationRecord.model_validate(
                client.request(
                    "GET",
                    f"/v1/operations/{operation.id}",
                ).json()
            )
        except (ManagerApiError, ManagerUnavailable, requests.RequestException):
            if operation.kind == OperationKind.UNINSTALL:
                continue
            try:
                client = ManagerClient.discover(client.layout.workspace)
            except ManagerUnavailable:
                continue
    return operation


def _run_application_mcp(
    client: ManagerClient,
    arguments: tuple[str, ...],
) -> str:
    """Run a bounded MCP administration command in the application runtime."""

    layout = client.layout
    python = runtime_python(layout)
    root = application_root(layout)
    if not python.is_file():
        raise ValueError(
            "The Pandrator application runtime is unavailable; repair the "
            "installation before administering MCP."
        )
    command = (str(python), "-m", "pandrator_mcp", *arguments)
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            "The Pandrator application runtime could not run the MCP "
            "administration command."
        ) from error
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError(
            "The installed application runtime could not complete the MCP "
            "administration command; repair or update the installation."
        )
    return completed.stdout


def _print_mcp_host_config(
    args: argparse.Namespace,
    client: ManagerClient,
) -> int:
    """Print a credential-bearing host fragment only after acknowledgement."""

    output = _run_application_mcp(
        client,
        (
            "managed-host-config",
            args.host,
            "--workspace",
            str(client.layout.workspace),
            "--server-name",
            args.server_name,
            "--include-credential",
        ),
    )
    print(output, end="")
    return 0


def _configure_mcp_paths(
    args: argparse.Namespace,
    client: ManagerClient,
) -> int:
    """Manage the fixed managed-local target without exposing target internals."""

    configuration = str(client.layout.mcp_configuration)
    if not client.layout.mcp_configuration.is_file():
        raise ValueError(
            "The managed MCP target has not been initialized; start Pandrator "
            "once, then configure its local paths."
        )
    prefix = ("target", "--config", configuration)
    if args.mcp_path_command == "list":
        source_payload: dict[str, Any] = json.loads(
            _run_application_mcp(
                client,
                (*prefix, "source-root-list", "managed-local"),
            )
        )
        public_payload: dict[str, Any] = json.loads(
            _run_application_mcp(
                client,
                ("print-config", "--config", configuration),
            )
        )
        managed: dict[str, Any] = next(
            (
                item
                for item in public_payload.get("targets", ())
                if item.get("name") == "managed-local"
            ),
            {},
        )
        _render(
            {
                "source_roots": source_payload.get("source_roots", ()),
                "output_root_configured": bool(
                    managed.get("local_output_root_configured")
                ),
            },
            as_json=True,
        )
        return 0
    command: tuple[str, ...]
    if args.mcp_path_command == "source-add":
        command = (
            *prefix,
            "source-root-add",
            "managed-local",
            str(args.name),
            str(args.path.expanduser().resolve(strict=False)),
        )
        if args.replace:
            command = (*command, "--replace")
    elif args.mcp_path_command == "source-remove":
        command = (
            *prefix,
            "source-root-remove",
            "managed-local",
            str(args.name),
        )
    elif args.mcp_path_command == "output-set":
        command = (
            *prefix,
            "output-root-set",
            "managed-local",
            str(args.path.expanduser().resolve(strict=False)),
        )
    else:
        command = (*prefix, "output-root-clear", "managed-local")
    output = _run_application_mcp(client, command)
    print(output, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "daemon":
            return run_daemon(
                args.workspace,
                port=args.port,
                register_silero=not args.no_silero,
            )
        if args.command == "autostart":
            adapter = autostart_adapter(
                WorkspaceLayout.from_value(args.workspace)
            )
            if args.action == "enable":
                payload: Any = adapter.install(activate=not args.no_activate)
            elif args.action == "disable":
                payload = adapter.remove()
            else:
                payload = adapter.status()
            _render(
                {
                    "supported": payload.supported,
                    "installed": payload.installed,
                    "path": payload.path,
                    "message": payload.message,
                    "enabled": payload.enabled,
                    "active": payload.active,
                },
                as_json=args.json,
            )
            return 0

        if args.command == "mcp-config" and not args.include_credential:
            raise ValueError(
                "Refusing to print a bearer credential without "
                "--include-credential."
            )

        client = ManagerClient.ensure_running(args.workspace)
        if args.command == "mcp-config":
            return _print_mcp_host_config(args, client)
        if args.command == "mcp-paths":
            return _configure_mcp_paths(args, client)
        if args.command == "automation-client":
            if args.automation_client_command == "list":
                payload = client.request(
                    "GET",
                    "/v1/automation/clients",
                ).json()
            else:
                if not args.yes:
                    raise ValueError(
                        "Automation-client revocation requires --yes."
                    )
                payload = client.request(
                    "DELETE",
                    (
                        "/v1/automation/clients/"
                        f"{args.client_id}"
                    ),
                    idempotency_key=(
                        args.idempotency_key or str(uuid.uuid4())
                    ),
                ).json()
        elif args.command in {"status", "start-manager"}:
            payload = client.status()
        elif args.command == "doctor":
            report = client.doctor().model_dump(mode="json")
            if args.errors_only:
                report["checks"] = [
                    check
                    for check in report["checks"]
                    if check["status"] != "pass"
                ]
            payload = report
        elif args.command == "legacy":
            payload = client.legacy_report()
        elif args.command == "legacy-import":
            inspected = client.legacy_report()
            report = inspected.get("report")
            if not inspected.get("available") or not isinstance(report, dict):
                raise ValueError(
                    "No legacy installer configuration was found."
                )
            digest = str(
                args.source_digest or report.get("source_digest") or ""
            )
            if digest != str(report.get("source_digest") or ""):
                raise ValueError(
                    "The supplied source digest does not match the current "
                    "legacy inspection."
                )
            if not args.yes:
                payload = {
                    **inspected,
                    "confirmation_required": True,
                }
            else:
                payload = client.import_legacy(
                    digest,
                    idempotency_key=args.idempotency_key,
                )
        elif args.command == "releases":
            payload = client.releases()
        elif args.command in {"release-plan", "release-update"}:
            try:
                manifest = json.loads(
                    args.manifest.expanduser().read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"Could not read signed release manifest: {error}"
                ) from error
            if not isinstance(manifest, dict):
                raise ValueError(
                    "Signed release manifest must be a JSON object."
                )
            plan = client.create_release_plan(
                manifest,
                expected_revision=args.expected_revision,
                offline=args.offline,
                start_after_activation=not args.keep_stopped,
                idempotency_key=(
                    f"{args.idempotency_key}:plan"
                    if args.idempotency_key
                    else None
                ),
            )
            if (
                args.command == "release-plan"
                or not args.yes
                or not plan.tasks
            ):
                payload = plan
            else:
                operation = client.submit_operation(
                    plan,
                    accepted_confirmations=tuple(
                        confirmation.key
                        for confirmation in plan.confirmations
                    ),
                    idempotency_key=(
                        f"{args.idempotency_key}:operation"
                        if args.idempotency_key
                        else str(uuid.uuid4())
                    ),
                )
                payload = (
                    _wait_for_operation(client, operation)
                    if args.wait
                    else operation
                )
        elif args.command == "uninstall":
            plan = client.create_uninstall_plan(
                expected_revision=args.expected_revision,
                purge_data=args.purge_data,
                export_data=(
                    str(args.export_data.expanduser())
                    if args.export_data is not None
                    else None
                ),
                idempotency_key=(
                    f"{args.idempotency_key}:plan"
                    if args.idempotency_key
                    else None
                ),
            )
            if not args.yes:
                payload = plan
            else:
                operation = client.submit_operation(
                    plan,
                    accepted_confirmations=tuple(
                        confirmation.key
                        for confirmation in plan.confirmations
                    ),
                    idempotency_key=(
                        f"{args.idempotency_key}:operation"
                        if args.idempotency_key
                        else str(uuid.uuid4())
                    ),
                )
                payload = (
                    _wait_for_operation(client, operation)
                    if args.wait
                    else operation
                )
        elif args.command == "stop-manager":
            client.stop_manager()
            payload = {"status": "manager_stopping"}
        elif args.command == "open":
            if args.recovery:
                url = client.recovery_url()
            else:
                app_service = next(
                    (
                        service
                        for service in client.services()
                        if service["id"] == "pandrator.api"
                        and (service.get("health") or {}).get("state") == "healthy"
                    ),
                    None,
                )
                url = (
                    app_service["endpoint"]
                    if app_service and app_service.get("endpoint")
                    else client.recovery_url()
                )
            opened = open_desktop_url(url)
            payload = {"url": url, "browser_opened": bool(opened)}
        elif args.command in {"list", "probe"}:
            items = client.components()
            if args.command == "probe" and args.components:
                selected = set(args.components)
                items = [
                    item
                    for item in items
                    if item["definition"]["id"] in selected
                ]
            payload = items
        elif args.command in {"plan", "install", "update", "repair", "remove"}:
            kind = args.operation_kind or OperationKind(args.kind)
            plan = client.create_plan(
                kind,
                _desired(
                    args,
                    listed_components_present=kind != OperationKind.REMOVE,
                ),
                expected_revision=args.expected_revision,
                idempotency_key=(
                    f"{args.idempotency_key}:plan"
                    if args.idempotency_key
                    else None
                ),
            )
            if args.command == "plan" or not args.yes:
                payload = plan
            else:
                operation = client.submit_operation(
                    plan,
                    accepted_confirmations=tuple(
                        confirmation.key
                        for confirmation in plan.confirmations
                    ),
                    idempotency_key=(
                        f"{args.idempotency_key}:operation"
                        if args.idempotency_key
                        else str(uuid.uuid4())
                    ),
                )
                payload = (
                    _wait_for_operation(client, operation)
                    if args.wait
                    else operation
                )
        elif hasattr(args, "runtime_action"):
            payload = client.runtime(
                args.runtime_action,
                tuple(args.services),
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "operations":
            payload = client.request("GET", "/v1/operations").json()["items"]
            if args.active:
                terminal = {value.value for value in TERMINAL_OPERATION_STATES}
                payload = [
                    operation
                    for operation in payload
                    if operation.get("state") not in terminal
                ]
        elif args.command == "operation":
            payload = client.request(
                "GET",
                f"/v1/operations/{args.operation_id}",
            ).json()
        elif args.command == "cancel":
            payload = client.request(
                "POST",
                f"/v1/operations/{args.operation_id}/cancel",
                json_payload={},
                idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            ).json()
        else:  # pragma: no cover - argparse prevents this
            parser.error("Unknown command.")
            return 2
        _render(payload, as_json=args.json)
        if isinstance(payload, dict):
            outcome = str(payload.get("status") or "")
            if outcome == "recovery_required":
                return 3
            if outcome in {
                "failed",
                "cleanup_interrupted",
                "succeeded_with_cleanup_residue",
            }:
                return 2
        return 0
    except ProductUninstalled as error:
        _render(error.status, as_json=args.json)
        return 0
    except (
        ManagerError,
        ManagerApiError,
        ManagerUnavailable,
        ValidationError,
        ValueError,
        KeyError,
    ) as error:
        if args.json:
            code = (
                error.payload.get("error", {}).get("code", "manager_error")
                if isinstance(error, ManagerApiError)
                else getattr(error, "code", "invalid_request")
            )
            print(
                json.dumps(
                    {
                        "error": {
                            "code": code,
                            "message": str(error),
                        }
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        else:
            print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
