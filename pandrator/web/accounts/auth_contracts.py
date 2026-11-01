"""Leaf credential scope and principal contracts; scopes do not grant user roles."""

from __future__ import annotations

from typing import Literal

ALL_SCOPES = frozenset(
    {
        "app.read",
        "app.write",
        "app.run",
        "app.cancel",
        "app.workspaces.enroll",
        "app.credentials.read",
        "app.credentials.write",
        "manager.read",
        "manager.runtime",
        "manager.mutate",
        "app.admin",
    }
)

USER_BROWSER_SCOPES = frozenset(
    {
        "app.read", "app.write", "app.run", "app.cancel",
        "app.credentials.read", "app.credentials.write", "app.workspaces.enroll",
    }
)

MCP_BOOTSTRAP_SCOPES = frozenset(
    {
        "app.read",
        "app.write",
        "app.run",
        "app.cancel",
        "manager.read",
        "manager.runtime",
        "manager.mutate",
    }
)

PrincipalKind = Literal[
    "owner_session",
    "project_link",
    "api_token",
    "manager_bootstrap",
    "automation_client",
    "service",
]

NetworkZone = Literal["loopback", "private", "public"]

PRINCIPAL_KINDS = frozenset(
    {
        "owner_session",
        "project_link",
        "api_token",
        "manager_bootstrap",
        "automation_client",
        "service",
    }
)


def normalize_scopes(
    scopes: object,
    *,
    allow_empty: bool = False,
) -> frozenset[str]:
    if isinstance(scopes, str):
        values = scopes.split()
    elif isinstance(scopes, (list, tuple, set, frozenset)):
        values = [str(value) for value in scopes]
    else:
        values = []
    normalized = frozenset(value.strip() for value in values if value.strip())
    unknown = normalized - ALL_SCOPES
    if unknown:
        raise ValueError(f"Unknown API scope(s): {', '.join(sorted(unknown))}.")
    if not normalized and not allow_empty:
        raise ValueError("At least one API scope is required.")
    return normalized
