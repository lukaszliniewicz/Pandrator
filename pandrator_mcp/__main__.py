"""Command-line entry point for stdio MCP and target diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .clients import (
    ApplicationClient,
    bootstrap_local_application,
    discover_local_application,
)
from .context import build_managed_runtime, build_runtime
from .credentials import CredentialReference, CredentialResolver
from .doctor import diagnose_target
from .enrollment import (
    enroll_manager_recovery,
    enroll_target,
    registry_for_store,
)
from .errors import PandratorMcpError
from .host_config import render_host_config, render_http_host_config
from .http import MCP_HTTP_HOST, MCP_HTTP_PATH, MCP_HTTP_PORT, read_bearer_token, run_http_server
from .network_policy import TargetMode
from .server import build_server
from .settings import McpSettings, default_configuration_path
from .targets import (
    APPLICATION_SCOPES,
    MANAGER_RECOVERY_SCOPES,
    LocalSourceRoot,
    TargetIdentityExpectation,
    TargetProfile,
    TargetRegistry,
    TargetStore,
)

_MODE_ALIASES = {
    "local": TargetMode.LOCAL_MANAGED,
    "lan": TargetMode.PRIVATE_NETWORK,
    "external": TargetMode.EXTERNAL_HTTPS,
    "external-application": TargetMode.EXTERNAL_APPLICATION,
}


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=default_configuration_path(),
    )


def _credential_reference(
    backend: str | None,
    reference: str | None,
    *,
    audience: str,
) -> CredentialReference | None:
    if bool(backend) != bool(reference):
        raise ValueError("Credential backend and reference must be supplied together.")
    if not backend:
        return None
    return CredentialReference(
        backend=backend,
        reference=str(reference),
        audience=audience,
    )


def _profile_from_args(args: argparse.Namespace) -> TargetProfile:
    mode = _MODE_ALIASES[args.mode]
    application_client_id = (
        str(args.client_id or uuid.uuid4()) if mode != TargetMode.LOCAL_MANAGED else None
    )
    manager_client_id = (
        str(args.recovery_client_id or application_client_id or uuid.uuid4())
        if args.recovery_origin
        else None
    )
    return TargetProfile(
        name=args.name,
        mode=mode,
        workspace=str(args.workspace) if args.workspace else None,
        application_origin=args.origin,
        manager_recovery_origin=args.recovery_origin,
        allowed_private_cidrs=tuple(args.allowed_cidr or ()),
        allow_insecure_private_network=bool(args.allow_insecure_http),
        ca_bundle=(str(args.ca_bundle.expanduser().resolve()) if args.ca_bundle else None),
        proxy_origin=args.proxy_origin,
        automation_client_id=application_client_id,
        automation_client_name=args.client_name,
        requested_application_scopes=tuple(args.scope or ("app.read",)),
        application_credential=_credential_reference(
            args.credential_backend,
            args.credential_reference,
            audience="application",
        ),
        manager_automation_client_id=manager_client_id,
        manager_automation_client_name=args.recovery_client_name,
        manager_requested_scopes=tuple(args.recovery_scope or ("manager.read",)),
        manager_recovery_credential=_credential_reference(
            args.recovery_credential_backend,
            args.recovery_credential_reference,
            audience="manager_recovery",
        ),
    )


def _public_profile(profile: TargetProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "mode": profile.mode.value,
        "application_origin": profile.application_origin,
        "workspace": profile.workspace,
        "tls_ca_configured": bool(profile.ca_bundle),
        "explicit_proxy_configured": bool(profile.proxy_origin),
        "private_cidr_count": len(profile.allowed_private_cidrs),
        "application_credential_configured": (profile.application_credential is not None),
        "automation_client_configured": bool(profile.automation_client_id),
        "requested_application_scopes": list(profile.requested_application_scopes),
        "enrolled_subject": profile.enrolled_subject,
        "credential_expires_at": profile.credential_expires_at,
        "manager_recovery_configured": (profile.manager_recovery_origin is not None),
        "manager_recovery_credential_configured": (profile.manager_recovery_credential is not None),
        "manager_automation_client_configured": bool(profile.manager_automation_client_id),
        "manager_requested_scopes": list(profile.manager_requested_scopes),
        "manager_enrolled_subject": (profile.manager_enrolled_subject),
        "manager_credential_expires_at": (profile.manager_credential_expires_at),
        "local_source_root_names": [item.name for item in profile.local_source_roots],
        "local_output_root_configured": bool(profile.local_output_root),
        "identity_pinned": bool(profile.expected_identity.application_instance_id),
    }


def _target_profile(store: TargetStore, name: str) -> TargetProfile:
    return TargetRegistry(store.load(missing_ok=False)).get(name)


def _revocation_guidance(
    profile: TargetProfile,
) -> list[dict[str, str]]:
    guidance: list[dict[str, str]] = []
    if profile.automation_client_id:
        guidance.append(
            {
                "audience": "application",
                "client_id": profile.automation_client_id,
                "owner_command": (
                    f"pandrator auth automation-client revoke {profile.automation_client_id} --yes"
                ),
            }
        )
    if profile.manager_automation_client_id:
        guidance.append(
            {
                "audience": "manager_recovery",
                "client_id": profile.manager_automation_client_id,
                "owner_command": (
                    "pandrator-manager automation-client revoke "
                    f"{profile.manager_automation_client_id} --yes"
                ),
            }
        )
    return guidance


def _delete_local_enrollment(
    store: TargetStore,
    name: str,
    *,
    audience: str,
) -> tuple[TargetProfile, bool]:
    profile = _target_profile(store, name)
    if audience == "application":
        reference = profile.application_credential
    else:
        reference = profile.manager_recovery_credential
    if reference is None:
        return profile, False
    CredentialResolver().delete(
        reference,
        audience=audience,
    )
    return (
        store.clear_enrollment(
            name,
            audience=audience,
        ),
        True,
    )


def _print_json(value: object, *, stream=None) -> None:
    print(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False),
        file=stream or sys.stdout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pandrator-mcp")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    stdio = subcommands.add_parser("stdio", help="Run the local stdio MCP server.")
    stdio.add_argument("--target", required=True)
    _add_config_argument(stdio)

    http = subcommands.add_parser(
        "http",
        help="Run the Manager-owned authenticated loopback HTTP MCP server.",
    )
    http.add_argument("--workspace", type=Path, required=True)
    http.add_argument("--token-file", type=Path, required=True)
    http.add_argument("--host", default=MCP_HTTP_HOST)
    http.add_argument("--port", type=int, default=MCP_HTTP_PORT)
    _add_config_argument(http)

    target = subcommands.add_parser(
        "target",
        help="Manage non-secret target profiles.",
    )
    _add_config_argument(target)
    target_commands = target.add_subparsers(
        dest="target_command",
        required=True,
    )

    add = target_commands.add_parser("add", help="Add a target profile.")
    add.add_argument("name")
    add.add_argument("--mode", choices=tuple(_MODE_ALIASES), required=True)
    add.add_argument("--workspace", type=Path)
    add.add_argument("--origin")
    add.add_argument("--allowed-cidr", action="append", default=[])
    add.add_argument("--allow-insecure-http", action="store_true")
    add.add_argument("--ca-bundle", type=Path)
    add.add_argument("--proxy-origin")
    add.add_argument("--client-id", type=uuid.UUID)
    add.add_argument("--client-name", default="Pandrator MCP")
    add.add_argument(
        "--scope",
        action="append",
        choices=sorted(APPLICATION_SCOPES),
        help="Application scope to request during enrollment; repeat as needed.",
    )
    add.add_argument(
        "--credential-backend",
        choices=("environment", "keyring"),
    )
    add.add_argument("--credential-reference")
    add.add_argument("--recovery-origin")
    add.add_argument("--recovery-client-id", type=uuid.UUID)
    add.add_argument(
        "--recovery-client-name",
        default="Pandrator MCP recovery",
    )
    add.add_argument(
        "--recovery-scope",
        action="append",
        choices=sorted(MANAGER_RECOVERY_SCOPES),
        help=(
            "Manager recovery scope to request during its separate enrollment; repeat as needed."
        ),
    )
    add.add_argument(
        "--recovery-credential-backend",
        choices=("environment", "keyring"),
    )
    add.add_argument("--recovery-credential-reference")
    add.add_argument("--replace", action="store_true")

    target_commands.add_parser("list", help="List target profiles.")

    source_root_add = target_commands.add_parser(
        "source-root-add",
        help="Expose one human-approved local directory by an opaque name.",
    )
    source_root_add.add_argument("name", help="Target profile name.")
    source_root_add.add_argument("root_name")
    source_root_add.add_argument("path", type=Path)
    source_root_add.add_argument("--replace", action="store_true")

    source_root_list = target_commands.add_parser(
        "source-root-list",
        help="List local source roots for one target.",
    )
    source_root_list.add_argument("name")

    source_root_remove = target_commands.add_parser(
        "source-root-remove",
        help="Stop exposing one named local source root.",
    )
    source_root_remove.add_argument("name")
    source_root_remove.add_argument("root_name")

    output_root_set = target_commands.add_parser(
        "output-root-set",
        help="Choose where downloaded artifacts are materialized locally.",
    )
    output_root_set.add_argument("name")
    output_root_set.add_argument("path", type=Path)

    output_root_clear = target_commands.add_parser(
        "output-root-clear",
        help="Disable local artifact materialization for one target.",
    )
    output_root_clear.add_argument("name")

    remove = target_commands.add_parser("remove", help="Remove a target profile.")
    remove.add_argument("name")
    remove.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal without an interactive prompt.",
    )
    removal_credentials = remove.add_mutually_exclusive_group()
    removal_credentials.add_argument(
        "--delete-local-credentials",
        action="store_true",
        help=(
            "Delete configured native credentials before removing the "
            "profile. This does not revoke server-side clients."
        ),
    )
    removal_credentials.add_argument(
        "--keep-local-credentials",
        action="store_true",
        help=(
            "Deliberately leave externally managed or native credentials "
            "untouched when removing the profile."
        ),
    )

    logout = target_commands.add_parser(
        "logout",
        help=(
            "Delete one local enrolled credential and clear its profile "
            "metadata without revoking the server-side client."
        ),
    )
    logout.add_argument("name")
    logout.add_argument(
        "--manager-recovery",
        action="store_true",
        help="Logout the separate Manager recovery audience.",
    )
    logout.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion from the local credential backend.",
    )

    test = target_commands.add_parser("test", help="Test one target profile.")
    test.add_argument("name")

    login = target_commands.add_parser(
        "login",
        help="Enroll this native client through trusted owner consent.",
    )
    login.add_argument("name")
    login.add_argument(
        "--scope",
        action="append",
        choices=sorted(APPLICATION_SCOPES),
        help="Override the profile's requested scopes; repeat as needed.",
    )
    login.add_argument(
        "--expires-in-days",
        type=int,
        choices=range(1, 91),
        default=30,
    )
    login.add_argument(
        "--headless",
        action="store_true",
        help="Use the hidden-TTY copy/paste fallback.",
    )
    login.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Print the authorization URL instead of opening it.",
    )
    login.add_argument(
        "--timeout",
        type=float,
        default=180.0,
    )
    login.add_argument(
        "--credential-backend",
        choices=("keyring",),
        default="keyring",
    )
    login.add_argument("--credential-reference")
    login.add_argument(
        "--manager-recovery",
        action="store_true",
        help=(
            "Enroll the separate HTTPS Manager recovery audience "
            "instead of the application audience."
        ),
    )
    login.add_argument(
        "--recovery-scope",
        action="append",
        choices=sorted(MANAGER_RECOVERY_SCOPES),
        help=("Override the profile's Manager recovery scopes; repeat as needed."),
    )

    configure_recovery = target_commands.add_parser(
        "configure-recovery",
        help=(
            "Add an exact HTTPS Manager recovery origin without replacing the application target."
        ),
    )
    configure_recovery.add_argument("name")
    configure_recovery.add_argument("--origin", required=True)
    configure_recovery.add_argument(
        "--client-id",
        type=uuid.UUID,
        help=(
            "Optional recovery client UUID; defaults to the target's application client identity."
        ),
    )
    configure_recovery.add_argument(
        "--client-name",
        default="Pandrator MCP recovery",
    )
    configure_recovery.add_argument(
        "--recovery-scope",
        action="append",
        choices=sorted(MANAGER_RECOVERY_SCOPES),
        help=("Manager recovery scope to request; repeat as needed. Defaults to manager.read."),
    )

    pin = target_commands.add_parser(
        "pin",
        help="Capture the authenticated target identity.",
    )
    pin.add_argument("name")
    pin.add_argument(
        "--replace-identity",
        action="store_true",
        help="Deliberately replace an existing identity pin.",
    )

    doctor = subcommands.add_parser(
        "doctor",
        help="Run layered target diagnostics.",
    )
    doctor.add_argument("--target", required=True)
    _add_config_argument(doctor)

    host_config = subcommands.add_parser(
        "host-config",
        help="Render a secret-free local-stdio host configuration.",
    )
    host_config.add_argument(
        "host",
        choices=(
            "codex",
            "claude-code",
            "opencode",
            "antigravity",
        ),
    )
    host_config.add_argument("--target", required=True)
    host_config.add_argument(
        "--server-name",
        help="Override the host-visible MCP server name.",
    )
    host_config.add_argument(
        "--executable",
        default="pandrator-mcp",
        help=("Executable name or absolute path the host should launch."),
    )
    _add_config_argument(host_config)

    managed_host_config = subcommands.add_parser(
        "managed-host-config",
        help=(
            "Render a local HTTP host configuration containing the managed "
            "MCP bearer credential."
        ),
    )
    managed_host_config.add_argument(
        "host",
        choices=(
            "codex",
            "claude-code",
            "opencode",
            "antigravity",
        ),
    )
    managed_host_config.add_argument("--workspace", type=Path, required=True)
    managed_host_config.add_argument(
        "--server-name",
        default="pandrator",
        help="Override the host-visible MCP server name.",
    )
    managed_host_config.add_argument(
        "--endpoint",
        default=f"http://{MCP_HTTP_HOST}:{MCP_HTTP_PORT}{MCP_HTTP_PATH}",
    )
    managed_host_config.add_argument(
        "--include-credential",
        action="store_true",
        help=(
            "Acknowledge that the generated fragment contains a secret and "
            "must be stored in a private user configuration."
        ),
    )

    print_config = subcommands.add_parser(
        "print-config",
        help="Print the public, secret-free target configuration.",
    )
    _add_config_argument(print_config)
    return parser


def _pin_target(args: argparse.Namespace) -> dict[str, object]:
    store = TargetStore(args.config)
    profiles = store.load(missing_ok=False)
    registry = TargetRegistry(
        profiles,
        local_discovery=discover_local_application,
    )
    profile = registry.get(args.name)
    existing = profile.expected_identity
    if existing.application_instance_id and not args.replace_identity:
        raise ValueError(
            "This target already has an identity pin; use "
            "--replace-identity only after verifying the instance change."
        )
    application = ApplicationClient(
        registry.bind(args.name),
        CredentialResolver(),
        local_bootstrap=bootstrap_local_application,
    )
    identity = application.identity(validate_expected=not args.replace_identity)
    expectation = TargetIdentityExpectation(
        application_instance_id=str(identity["instance_id"]),
        canonical_application_origin=str(identity["canonical_origin"]),
        manager_instance_id=(
            str(identity["manager_instance_id"]) if identity.get("manager_instance_id") else None
        ),
    )
    updated = store.update_identity(args.name, expectation)
    return {
        "target": updated.name,
        "identity_pinned": True,
        "application_instance_id": expectation.application_instance_id,
        "canonical_application_origin": (expectation.canonical_application_origin),
        "manager_instance_id": expectation.manager_instance_id,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "stdio":
            runtime = build_runtime(
                McpSettings(
                    target_name=args.target,
                    configuration_path=args.config,
                )
            )
            build_server(runtime).run()
            return 0
        if args.command == "http":
            runtime = build_managed_runtime(
                args.workspace,
                configuration_path=args.config,
            )
            run_http_server(
                runtime,
                token_file=args.token_file,
                host=args.host,
                port=args.port,
            )
            return 0
        if args.command == "doctor":
            report = diagnose_target(
                McpSettings(
                    target_name=args.target,
                    configuration_path=args.config,
                )
            )
            _print_json(report.model_dump(mode="json"))
            return 0 if report.healthy else 1
        if args.command == "host-config":
            store = TargetStore(args.config)
            profiles = store.load(missing_ok=False)
            registry = TargetRegistry(profiles)
            registry.get(args.target)
            rendered = render_host_config(
                args.host,
                target=args.target,
                configuration_path=args.config,
                executable=args.executable,
                server_name=args.server_name,
            )
            print(rendered.content, end="")
            return 0
        if args.command == "managed-host-config":
            if not args.include_credential:
                raise ValueError(
                    "Refusing to print a bearer credential without "
                    "--include-credential."
                )
            workspace = args.workspace.expanduser().resolve(strict=False)
            token = read_bearer_token(
                workspace / "Pandrator" / "state" / "mcp.secret"
            )
            rendered = render_http_host_config(
                args.host,
                endpoint=args.endpoint,
                bearer_token=token,
                server_name=args.server_name,
            )
            print(rendered.content, end="")
            return 0
        if args.command == "print-config":
            store = TargetStore(args.config)
            profiles = store.load(missing_ok=False)
            _print_json(
                {
                    "schema_version": "1",
                    "configuration_path": str(args.config.expanduser().resolve(strict=False)),
                    "targets": [_public_profile(profile) for profile in profiles],
                }
            )
            return 0
        if args.command == "target":
            store = TargetStore(args.config)
            if args.target_command == "add":
                profile = _profile_from_args(args)
                store.put(profile, replace=args.replace)
                _print_json(
                    {
                        "saved": True,
                        "target": _public_profile(profile),
                    }
                )
                return 0
            if args.target_command == "list":
                _print_json({"targets": [_public_profile(profile) for profile in store.load()]})
                return 0
            if args.target_command == "source-root-add":
                profile = _target_profile(store, args.name)
                roots = list(profile.local_source_roots)
                matching = next(
                    (
                        index
                        for index, item in enumerate(roots)
                        if item.name.casefold() == args.root_name.casefold()
                    ),
                    None,
                )
                root = LocalSourceRoot(
                    name=args.root_name,
                    path=str(args.path.expanduser().resolve(strict=False)),
                )
                if matching is not None:
                    if not args.replace:
                        raise ValueError(
                            "That local source root already exists; use --replace explicitly."
                        )
                    roots[matching] = root
                else:
                    roots.append(root)
                updated = store.configure_local_paths(
                    args.name,
                    source_roots=tuple(roots),
                )
                _print_json(
                    {
                        "saved": True,
                        "target": updated.name,
                        "source_root": root.model_dump(mode="json"),
                    }
                )
                return 0
            if args.target_command == "source-root-list":
                profile = _target_profile(store, args.name)
                _print_json(
                    {
                        "target": profile.name,
                        "source_roots": [
                            item.model_dump(mode="json") for item in profile.local_source_roots
                        ],
                    }
                )
                return 0
            if args.target_command == "source-root-remove":
                profile = _target_profile(store, args.name)
                remaining_roots = tuple(
                    item
                    for item in profile.local_source_roots
                    if item.name.casefold() != args.root_name.casefold()
                )
                if len(remaining_roots) == len(profile.local_source_roots):
                    raise ValueError("That local source root is not configured.")
                store.configure_local_paths(args.name, source_roots=remaining_roots)
                _print_json(
                    {
                        "saved": True,
                        "target": profile.name,
                        "removed_source_root": args.root_name,
                    }
                )
                return 0
            if args.target_command == "output-root-set":
                path = str(args.path.expanduser().resolve(strict=False))
                updated = store.configure_local_paths(
                    args.name,
                    output_root=path,
                )
                _print_json(
                    {
                        "saved": True,
                        "target": updated.name,
                        "local_output_root": updated.local_output_root,
                    }
                )
                return 0
            if args.target_command == "output-root-clear":
                updated = store.configure_local_paths(
                    args.name,
                    output_root=None,
                )
                _print_json(
                    {
                        "saved": True,
                        "target": updated.name,
                        "local_output_root": None,
                    }
                )
                return 0
            if args.target_command == "remove":
                if not args.yes:
                    raise ValueError("Target removal requires --yes confirmation.")
                profile = _target_profile(store, args.name)
                configured_audiences = [
                    audience
                    for audience, reference in (
                        (
                            "application",
                            profile.application_credential,
                        ),
                        (
                            "manager_recovery",
                            profile.manager_recovery_credential,
                        ),
                    )
                    if reference is not None
                ]
                if (
                    configured_audiences
                    and not args.delete_local_credentials
                    and not args.keep_local_credentials
                ):
                    raise ValueError(
                        "This target still references local credentials. "
                        "Use --delete-local-credentials to delete them or "
                        "--keep-local-credentials to preserve them "
                        "deliberately. Neither option revokes remote clients."
                    )
                deleted_audiences: list[str] = []
                if args.delete_local_credentials:
                    for audience in configured_audiences:
                        _updated, deleted = _delete_local_enrollment(
                            store,
                            args.name,
                            audience=audience,
                        )
                        if deleted:
                            deleted_audiences.append(audience)
                removed = store.remove(args.name)
                _print_json(
                    {
                        "removed": removed.name,
                        "local_credentials_deleted": (deleted_audiences),
                        "local_credentials_preserved": bool(
                            configured_audiences and args.keep_local_credentials
                        ),
                        "remote_clients_revoked": False,
                        "remote_revocation": _revocation_guidance(profile),
                    }
                )
                return 0
            if args.target_command == "logout":
                if not args.yes:
                    raise ValueError("Credential logout requires --yes confirmation.")
                audience = "manager_recovery" if args.manager_recovery else "application"
                before = _target_profile(store, args.name)
                _updated, deleted = _delete_local_enrollment(
                    store,
                    args.name,
                    audience=audience,
                )
                guidance = [
                    item for item in _revocation_guidance(before) if item["audience"] == audience
                ]
                _print_json(
                    {
                        "target": args.name,
                        "audience": audience,
                        "local_credential_deleted": deleted,
                        "profile_enrollment_cleared": deleted,
                        "remote_client_revoked": False,
                        "remote_revocation": guidance,
                    }
                )
                return 0
            if args.target_command == "test":
                report = diagnose_target(
                    McpSettings(
                        target_name=args.name,
                        configuration_path=args.config,
                    )
                )
                _print_json(report.model_dump(mode="json"))
                return 0 if report.healthy else 1
            if args.target_command == "login":
                registry = registry_for_store(store)
                profile = registry.get(args.name)
                if args.manager_recovery:
                    if args.scope:
                        raise ValueError("Use --recovery-scope with --manager-recovery.")
                    if args.expires_in_days > 30:
                        raise ValueError("Manager recovery credentials may last at most 30 days.")
                    summary = enroll_manager_recovery(
                        profile=profile,
                        binding=registry.bind(args.name),
                        store=store,
                        credentials=CredentialResolver(),
                        scopes=tuple(args.recovery_scope or profile.manager_requested_scopes),
                        expires_in_days=args.expires_in_days,
                        headless=args.headless,
                        open_browser=not args.no_open_browser,
                        timeout_seconds=args.timeout,
                        credential_backend=args.credential_backend,
                        credential_reference=(args.credential_reference),
                    )
                else:
                    if args.recovery_scope:
                        raise ValueError("--recovery-scope requires --manager-recovery.")
                    summary = enroll_target(
                        profile=profile,
                        binding=registry.bind(args.name),
                        store=store,
                        credentials=CredentialResolver(),
                        scopes=tuple(args.scope or profile.requested_application_scopes),
                        expires_in_days=args.expires_in_days,
                        headless=args.headless,
                        open_browser=not args.no_open_browser,
                        timeout_seconds=args.timeout,
                        credential_backend=args.credential_backend,
                        credential_reference=(args.credential_reference),
                    )
                _print_json(summary.as_dict())
                return 0
            if args.target_command == "configure-recovery":
                updated = store.configure_manager_recovery(
                    args.name,
                    origin=args.origin,
                    requested_scopes=tuple(args.recovery_scope or ("manager.read",)),
                    client_name=args.client_name,
                    client_id=(str(args.client_id) if args.client_id else None),
                )
                _print_json(
                    {
                        "saved": True,
                        "target": _public_profile(updated),
                    }
                )
                return 0
            if args.target_command == "pin":
                _print_json(_pin_target(args))
                return 0
    except (PandratorMcpError, ValidationError, ValueError) as error:
        code = error.code if isinstance(error, PandratorMcpError) else "validation_error"
        _print_json(
            {
                "error": {
                    "code": code,
                    "message": str(error),
                }
            },
            stream=sys.stderr,
        )
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
