"""Account administration and one-use password recovery contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .accounts.api_schemas import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResetIssueRequest,
    AccountResetIssueResponse,
    AccountResetRedeemRequest,
    AccountSummaryResponse,
    AccountUpdateRequest,
)
from .accounts.auth_contracts import PrincipalKind
from .workspace_schemas import InstallationModeResponse


class AuthPrincipalResponse(BaseModel):
    subject: str
    kind: PrincipalKind
    scopes: list[str]
    client_id: str | None
    user_id: str | None
    auth_revision: int | None
    display_name: str | None
    login_name: str | None
    role: Literal["administrator", "user"] | None


class AuthStatusResponse(BaseModel):
    instance_id: str
    installation: InstallationModeResponse
    initialized: bool
    authenticated: bool
    csrf_token: str | None
    principal: AuthPrincipalResponse | None
    remote_access: bool
    secure_transport: bool
    security_warning: str


ACCOUNT_SCHEMAS = {
    model.__name__: model
    for model in (
        AccountCreateRequest,
        AccountUpdateRequest,
        AccountResetIssueRequest,
        AccountResetRedeemRequest,
        AccountSummaryResponse,
        AccountListResponse,
        AccountResetIssueResponse,
        AuthStatusResponse,
    )
}


def _json_schema(name: str) -> dict[str, Any]:
    return {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}


def account_paths() -> dict[str, Any]:
    def operation(
        operation_id: str,
        *,
        request_schema: str | None = None,
        response_schema: str | None = None,
        status: str = "200",
        idempotency: bool = False,
        public: bool = False,
        description: str,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {"description": "Completed"}
        if response_schema is not None:
            response["content"] = _json_schema(response_schema)
        result: dict[str, Any] = {
            "operationId": operation_id,
            "description": description,
            "security": []
            if public
            else [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": ["app.admin"]}],
            "responses": {
                status: response,
                "400": {"description": "Invalid account or reset request"},
                "401": {"description": "Authentication required"},
                "403": {"description": "Current Administrator authority required"},
                "409": {"description": "Account revision or idempotency conflict"},
                "413": {"description": "Metadata exceeds 16 KiB"},
                "422": {"description": "Invalid request fields"},
            },
        }
        if not public:
            result["x-required-scope"] = "app.admin"
            result["x-required-role"] = "administrator"
        if request_schema is not None:
            result["requestBody"] = {"required": True, "content": _json_schema(request_schema)}
        if idempotency:
            result["parameters"] = [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            ]
        return result

    return {
        "/api/v1/admin/users": {
            "get": operation(
                "listUsers",
                response_schema="AccountListResponse",
                description="List account identity, role and password setup status. Password hashes and reset links are never listed.",
            ),
            "post": operation(
                "createUser",
                request_schema="AccountCreateRequest",
                response_schema="AccountSummaryResponse",
                status="201",
                idempotency=True,
                description="Create an account and its private personal workspace. The default role is User. Issue a password setup link separately.",
            ),
        },
        "/api/v1/admin/users/{userId}": {
            "patch": operation(
                "updateUser",
                request_schema="AccountUpdateRequest",
                response_schema="AccountSummaryResponse",
                idempotency=True,
                description="Change display name, role or active state at the expected authentication revision. Role/state changes revoke credentials. Initial Administrator retirement remains blocked while single-user mode is active.",
            ),
        },
        "/api/v1/admin/users/{userId}/password-reset": {
            "post": operation(
                "issueUserPasswordReset",
                request_schema="AccountResetIssueRequest",
                response_schema="AccountResetIssueResponse",
                description="Issue a one-use 30-minute password setup link. Returned only in this response; never stored in idempotency responses. Reissuing invalidates earlier links. Deliver the link privately; existing passwords change only on redemption.",
            ),
        },
        "/api/v1/auth/password-reset": {
            "post": {
                **operation(
                    "redeemPasswordReset",
                    request_schema="AccountResetRedeemRequest",
                    status="204",
                    public=True,
                    description="Redeem the one-use bearer from a reset link and set a password. Revokes affected credentials and does not sign the recipient in. Requires no browser session or CSRF cookie.",
                ),
                "x-one-use-grant": True,
                "responses": {
                    "204": {"description": "Password saved; sign in separately"},
                    "400": {"description": "Reset link invalid or expired"},
                    "413": {"description": "Metadata exceeds 16 KiB"},
                    "422": {"description": "Invalid input; sensitive values omitted"},
                    "429": {"description": "Too many invalid reset attempts"},
                },
            },
        },
    }
