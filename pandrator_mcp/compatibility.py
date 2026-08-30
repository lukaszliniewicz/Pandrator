"""Version and OpenAPI negotiation for the read-only MCP contract."""

from __future__ import annotations

import re
from typing import Any

MINIMUM_APPLICATION_VERSION = (0, 8, 16)
SUPPORTED_API_VERSIONS = frozenset({"v1"})
REQUIRED_READ_OPERATION_IDS = frozenset(
    {
        "getCapabilities",
        "getHealth",
        "getSession",
        "getSystemIdentity",
        "getWorkflow",
        "getWork",
        "listArtifacts",
        "listProviders",
        "listSessions",
        "listVoices",
        "listWork",
        "listWorkEvents",
    }
)
REQUIRED_MANAGER_OPERATION_IDS = frozenset(
    {
        "getManagerDoctorReport",
        "getManagerStatus",
    }
)
REQUIRED_DISPATCH_OPERATION_IDS = frozenset(
    {
        "claimDispatchBatch",
        "createDispatchRun",
        "getDispatchRun",
        "listDispatchRuns",
        "releaseDispatchBatch",
        "renewDispatchBatch",
        "submitDispatchBatch",
    }
)
_VERSION_PREFIX = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _operations(document: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return values
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str) and operation_id:
                values.add(operation_id)
    return values


def negotiate_compatibility(
    identity: dict[str, Any],
    openapi: dict[str, Any],
    *,
    manager_expected: bool,
) -> dict[str, Any]:
    """Return a deterministic compatibility report without loose fallback."""

    errors: list[str] = []
    warnings: list[str] = []
    api_version = str(identity.get("api_version") or "")
    protocol_version = str(identity.get("protocol_version") or "")
    if api_version not in SUPPORTED_API_VERSIONS:
        errors.append(f"Unsupported application API version: {api_version or 'missing'}.")
    if protocol_version not in SUPPORTED_API_VERSIONS:
        errors.append(f"Unsupported application protocol version: {protocol_version or 'missing'}.")
    if openapi.get("openapi") != "3.1.0":
        errors.append("The application does not advertise the required OpenAPI 3.1 contract.")

    application_version = str(identity.get("application_version") or "")
    match = _VERSION_PREFIX.match(application_version)
    if match:
        parsed_version = tuple(int(value) for value in match.groups())
        if parsed_version < MINIMUM_APPLICATION_VERSION:
            errors.append(
                f"Pandrator {application_version} is older than the supported 0.8.16 baseline."
            )
    else:
        warnings.append(
            "The application version is not a comparable semantic version; "
            "OpenAPI operations were checked directly."
        )

    required = set(REQUIRED_READ_OPERATION_IDS)
    required.update(REQUIRED_DISPATCH_OPERATION_IDS)
    if manager_expected:
        required.update(REQUIRED_MANAGER_OPERATION_IDS)
    missing = sorted(required - _operations(openapi))
    if missing:
        errors.append("Required API operations are missing.")

    return {
        "schema_version": "1",
        "compatible": not errors,
        "application_version": application_version,
        "api_version": api_version,
        "protocol_version": protocol_version,
        "openapi_version": openapi.get("openapi"),
        "missing_operations": missing,
        "warnings": warnings,
        "errors": errors,
    }
