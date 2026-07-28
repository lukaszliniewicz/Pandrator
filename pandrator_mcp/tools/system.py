"""Read-only system tool handlers."""

from __future__ import annotations

from typing import Any

from ..compatibility import negotiate_compatibility
from ..context import McpRuntime
from ..errors import PandratorMcpError
from ..network_policy import TargetMode
from ..results import ToolOutcome
from ..schemas import CapabilitiesInput, SystemStatusInput, TargetStatusInput
from ..schemas.common import WarningMessage
from .manager import manager_status_projection


def target_status(
    runtime: McpRuntime,
    arguments: TargetStatusInput,
) -> dict[str, Any]:
    if runtime.application is None:
        error = runtime.startup_error
        return {
            "schema_version": "1",
            "target": runtime.settings.target_name,
            "available": False,
            "error": {
                "code": error.code if error else "application_unavailable",
                "message": str(error or "The target is unavailable."),
            },
        }
    application = runtime.application
    result: dict[str, Any] = {"schema_version": "1"}
    try:
        result["target"] = application.target_summary()
    except PandratorMcpError as error:
        profile = runtime.profile
        result["target"] = {
            "schema_version": "1",
            "name": runtime.settings.target_name,
            "mode": profile.mode.value if profile else None,
            "identity_pinned": bool(profile and profile.expected_identity.application_instance_id),
        }
        result["available"] = False
        result["error"] = {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        }
        return result
    try:
        result["health"] = application.health()
        result["available"] = True
    except PandratorMcpError as error:
        result["available"] = False
        result["error"] = {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        }
        return result
    if arguments.include_authenticated_identity:
        try:
            result["identity"] = application.identity()
            result["authenticated"] = True
        except PandratorMcpError as error:
            result["authenticated"] = False
            result["identity_error"] = {
                "code": error.code,
                "message": str(error),
            }
    return result


def capabilities(
    runtime: McpRuntime,
    _arguments: CapabilitiesInput,
) -> dict[str, Any]:
    return runtime.require_application().capabilities()


def system_status(
    runtime: McpRuntime,
    arguments: SystemStatusInput,
) -> ToolOutcome:
    application = runtime.require_application()
    result: dict[str, Any] = {
        "health": application.health(),
        "identity": application.identity(),
    }
    warnings: list[WarningMessage] = []
    if arguments.include_capabilities:
        result["capabilities"] = application.capabilities()
    if arguments.include_manager:
        try:
            result["manager"] = manager_status_projection(
                runtime.manager.status()
            )
        except PandratorMcpError as error:
            result["manager"] = {
                "available": False,
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                },
            }
            warnings.append(
                WarningMessage(
                    code=error.code,
                    message=(
                        "Application status is available, but optional Manager "
                        f"context could not be loaded: {error}"
                    ),
                )
            )
    openapi = application.openapi()
    mode = runtime.profile.mode if runtime.profile else None
    result["compatibility"] = negotiate_compatibility(
        result["identity"],
        openapi,
        manager_expected=mode != TargetMode.EXTERNAL_APPLICATION,
    )
    return ToolOutcome(result=result, warnings=warnings)
