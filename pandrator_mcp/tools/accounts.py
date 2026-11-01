"""Typed MCP tools for application-account administration."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, cast, get_args

from pydantic import BaseModel

from ..context import McpRuntime
from ..errors import PandratorMcpError
from ..results import ToolOutcome
from ..schemas.accounts import (
    CreateUserInput,
    IssueUserPasswordResetInput,
    ListUsersInput,
    UpdateUserInput,
)

_ACCOUNT_FIELDS = (
    "user_id",
    "login_name",
    "display_name",
    "role",
    "is_active",
    "auth_revision",
    "password_configured",
)
_RESET_PATH = re.compile(r"^/password-reset#token=[A-Za-z0-9_-]{43,256}$")


def _account_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not all(field in payload for field in _ACCOUNT_FIELDS):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    if any(
        type(payload[field]) is not str or not payload[field].strip()
        for field in ("user_id", "login_name", "display_name")
    ):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    if type(payload["role"]) is not str or payload["role"] not in {"user", "administrator"}:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    if type(payload["is_active"]) is not bool or type(payload["password_configured"]) is not bool:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    if type(payload["auth_revision"]) is not int or payload["auth_revision"] < 1:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    return {field: payload[field] for field in _ACCOUNT_FIELDS}


def list_users(runtime: McpRuntime, arguments: ListUsersInput) -> ToolOutcome:
    del arguments
    payload = runtime.require_application().account_request("list", {})
    items = payload.get("items")
    initial_user_id = payload.get("initial_user_id")
    named_user_access = payload.get("named_user_access")
    if (
        not isinstance(items, list)
        or any(not isinstance(item, Mapping) for item in items)
        or type(initial_user_id) is not str
        or not initial_user_id.strip()
        or type(named_user_access) is not bool
    ):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account list response.",
        )
    return ToolOutcome(
        result={
            "items": [_account_summary(item) for item in items],
            "initial_user_id": initial_user_id,
            "named_user_access": named_user_access,
        }
    )


def create_user(runtime: McpRuntime, arguments: CreateUserInput) -> ToolOutcome:
    payload = runtime.require_application().account_request(
        "create",
        arguments.model_dump(mode="json", exclude_none=True),
    )
    if not isinstance(payload, Mapping):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    return ToolOutcome(result=_account_summary(payload))


def update_user(runtime: McpRuntime, arguments: UpdateUserInput) -> ToolOutcome:
    request = {
        "user_id": arguments.user_id,
        "expected_auth_revision": arguments.expected_auth_revision,
        "changes": arguments.changes.model_dump(mode="json", exclude_none=True),
        "idempotency_key": arguments.idempotency_key,
    }
    payload = runtime.require_application().account_request("update", request)
    if not isinstance(payload, Mapping):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid account response.",
        )
    return ToolOutcome(result=_account_summary(payload))


def issue_user_password_reset(
    runtime: McpRuntime,
    arguments: IssueUserPasswordResetInput,
) -> ToolOutcome:
    application = runtime.require_application()
    payload = application.account_request(
        "issue_reset",
        arguments.model_dump(mode="json", exclude_none=True),
    )
    if not isinstance(payload, Mapping):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid password setup response.",
        )
    grant_id = payload.get("grant_id")
    user_id = payload.get("user_id")
    expires_at = payload.get("expires_at")
    reset_path = payload.get("reset_path")
    if (
        type(grant_id) is not str
        or not grant_id.strip()
        or type(user_id) is not str
        or user_id != arguments.user_id
        or type(expires_at) is not str
        or not expires_at.strip()
        or type(reset_path) is not str
        or not _RESET_PATH.fullmatch(reset_path)
    ):
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned an invalid password setup response.",
        )
    origin = application.binding.resolve().application.origin
    return ToolOutcome(
        result={
            "grant_id": grant_id,
            "user_id": user_id,
            "expires_at": expires_at,
            "reset_path": reset_path,
            "reset_url": f"{origin}{reset_path}",
        }
    )


def register_account_tools(
    server: Any,
    runtime: McpRuntime,
    validated_call: Callable[..., dict[str, Any]],
    response: Callable[[dict[str, Any], str], Any],
    *,
    read_only: Any,
    write_action: Any,
    execute_action: Any,
    destructive_action: Any,
) -> None:
    """Register the four fixed account-administration tools."""

    def register(
        model: type[BaseModel],
        handler: Callable[..., ToolOutcome],
        *,
        name: str,
        title: str,
        description: str,
        annotations: Any,
    ) -> None:
        def invoke(**values: Any) -> Any:
            response_mode = values.pop("response_mode", "standard")
            if response_mode not in {"standard", "structured"}:
                raise ValueError("response_mode must be standard or structured.")
            envelope = validated_call(handler, runtime, model, values)
            return response(envelope, response_mode)

        parameters: list[inspect.Parameter] = []
        annotations_map: dict[str, Any] = {"return": dict[str, Any]}
        for field_name, field in model.model_fields.items():
            field_annotation = field.annotation
            field_args = get_args(field_annotation)
            if type(None) in field_args:
                non_nullable = tuple(item for item in field_args if item is not type(None))
                if len(non_nullable) == 1 and non_nullable[0] is str:
                    field_annotation = str | None
            annotation = (
                field_annotation
                if field.default_factory is not None
                else Annotated[field_annotation, field]
            )
            annotations_map[field_name] = annotation
            default = field.default
            if field.default_factory is not None:
                factory = cast(Callable[[], Any], field.default_factory)
                default = factory()
            parameters.append(
                inspect.Parameter(
                    field_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=inspect.Parameter.empty if field.is_required() else default,
                )
            )

        annotations_map["response_mode"] = Literal["standard", "structured"]
        parameters.append(
            inspect.Parameter(
                "response_mode",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Literal["standard", "structured"],
                default="standard",
            )
        )
        invoke.__name__ = name
        invoke.__doc__ = description
        invoke.__annotations__ = annotations_map
        invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
        server.tool(name=name, title=title, annotations=annotations)(invoke)

    register(
        ListUsersInput,
        list_users,
        name="pandrator_list_users",
        title="List users",
        description=(
            "List accounts, Administrator/User roles and password setup status. "
            "Requires an Administrator application credential. Project access is separate."
        ),
        annotations=read_only,
    )
    register(
        CreateUserInput,
        create_user,
        name="pandrator_create_user",
        title="Create user",
        description=(
            "Create an account with a private personal workspace. User is the default role; "
            "Administrator must be chosen explicitly. Issue a password setup link separately. "
            "Requires an Administrator credential."
        ),
        annotations=write_action,
    )
    register(
        UpdateUserInput,
        update_user,
        name="pandrator_update_user",
        title="Update user",
        description=(
            "Change an account display name, role or enabled state at its expected "
            "authentication revision. Role/state changes sign out affected credentials. "
            "Requires an Administrator credential; initial Administrator retirement remains protected."
        ),
        annotations=execute_action,
    )
    register(
        IssueUserPasswordResetInput,
        issue_user_password_reset,
        name="pandrator_issue_user_password_reset",
        title="Create password setup link",
        description=(
            "Create a secret, one-use password setup link valid for 30 minutes. Reissuing "
            "replaces earlier links. Return it to the operator for private manual delivery; "
            "do not send it automatically. Requires an Administrator credential. The recipient "
            "chooses their password in the browser."
        ),
        annotations=destructive_action,
    )


__all__ = [
    "create_user",
    "issue_user_password_reset",
    "list_users",
    "register_account_tools",
    "update_user",
]
