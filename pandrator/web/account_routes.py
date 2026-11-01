"""HTTP adapters for the solo instance's account administration commands."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

from flask import Response, g, jsonify, request
from sqlalchemy.orm import Session

from .accounts.actor_authorization import require_actor_in_session
from .accounts.administration import AdministratorRequired
from .accounts.api_schemas import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResetIssueRequest,
    AccountResetIssueResponse,
    AccountResetRedeemRequest,
    AccountRole,
    AccountSummaryResponse,
    AccountUpdateRequest,
)
from .accounts.auth_state import AccountState, initial_user_id
from .accounts.commands import (
    AccountActor,
    AccountCommandError,
    AccountCommands,
    AccountSummary,
)
from .accounts.execution_actor import UserActor, current_execution_actor
from .accounts.installation_mode import installation_mode_in_session
from .auth import Principal
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext


def _summary_payload(summary: AccountSummary) -> dict[str, Any]:
    return AccountSummaryResponse(
        user_id=summary.user_id,
        login_name=summary.login_name,
        display_name=summary.display_name,
        role=cast(AccountRole, summary.role),
        is_active=summary.is_active,
        auth_revision=summary.auth_revision,
        password_configured=summary.password_configured,
    ).model_dump(mode="json")


def _idempotency_error(
    context: RouteContext,
    error: IdempotencyConflict | IdempotencyInProgress,
) -> tuple[Response, int]:
    return context.guards.error_response(
        getattr(error, "code", "idempotency_conflict"),
        str(error),
        409,
        {"retryable": bool(getattr(error, "retryable", False))},
    )


def register_account_routes(app: Any, context: RouteContext) -> None:
    """Register the account-administration and one-use password-reset routes."""

    services = context.services
    guards = context.guards
    commands = AccountCommands(services.database)

    def authorize_admin(
        session: Session,
    ) -> tuple[AccountActor, UserActor, Principal]:
        principal = guards.principal()
        actor = current_execution_actor.get()
        if (
            principal is None
            or type(actor) is not UserActor
            or principal.user_id != actor.user_id
            or principal.auth_revision != actor.auth_revision
            or principal.subject != actor.subject
            or principal.scopes != actor.scopes
            or ((actor.token_id is None) == (actor.browser_session_id is None))
        ):
            raise AdministratorRequired()
        try:
            state = require_actor_in_session(
                session,
                actor,
                required_scopes={"app.admin"},
                require_admin=True,
                allow_host=False,
            )
        except PermissionError:
            raise AdministratorRequired() from None
        if not isinstance(state, AccountState):
            raise AdministratorRequired()
        return (
            AccountActor(state.user_id, state.auth_revision, actor.scopes),
            actor,
            principal,
        )

    def validate_idempotency_key() -> tuple[str | None, tuple[Response, int] | None]:
        try:
            return (
                services.idempotency.validate_key(request.headers.get("Idempotency-Key")),
                None,
            )
        except ValueError as error:
            return (
                None,
                guards.error_response("validation_error", str(error), 422),
            )

    def set_user_audit(resource_id: str) -> None:
        g.audit_resource_kind = "user"
        g.audit_resource_id = resource_id

    def read_json_payload() -> Any:
        request.max_content_length = 16 * 1024
        return request.get_json(silent=True) or {}

    @app.get("/api/v1/admin/users")
    @guards.require_scope("app.admin")
    def account_users_list():
        with services.database.immediate_session() as session:
            _account_actor, _transport, _principal = authorize_admin(session)
            summaries = commands.list_users_in_session(session, _account_actor)
            payload = AccountListResponse(
                items=[
                    AccountSummaryResponse(
                        user_id=summary.user_id,
                        login_name=summary.login_name,
                        display_name=summary.display_name,
                        role=cast(AccountRole, summary.role),
                        is_active=summary.is_active,
                        auth_revision=summary.auth_revision,
                        password_configured=summary.password_configured,
                    )
                    for summary in summaries
                ],
                initial_user_id=initial_user_id(session),
                named_user_access=installation_mode_in_session(session).enabled,
            ).model_dump(mode="json")
        return jsonify(payload)

    @app.post("/api/v1/admin/users")
    @guards.require_scope("app.admin")
    def account_users_create():
        payload = AccountCreateRequest.model_validate(read_json_payload())
        idempotency_key, key_error = validate_idempotency_key()
        if key_error is not None:
            return key_error
        assert idempotency_key is not None
        resource_id: str | None = None
        try:
            with services.database.immediate_session() as session:
                account_actor, transport, principal = authorize_admin(session)
                reservation = services.idempotency.begin(
                    session,
                    principal=principal,
                    operation_id=f"admin.users.{transport.user_id}.create",
                    idempotency_key=idempotency_key,
                    payload=payload.model_dump(mode="json"),
                )
                if reservation.replayed:
                    replay = reservation.response
                    if replay is None:
                        raise RuntimeError("The completed account response is unavailable.")
                    response_payload, status_code = replay
                    response = jsonify(response_payload)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    resource_id = reservation.record.resource_id
                else:
                    summary = commands.create_user_in_session(
                        session,
                        account_actor,
                        login_name=payload.login_name,
                        display_name=payload.display_name,
                        role=payload.role,
                    )
                    response_payload = _summary_payload(summary)
                    resource_id = summary.user_id
                    services.idempotency.complete(
                        session,
                        reservation,
                        response=response_payload,
                        status_code=201,
                        resource_kind="user",
                        resource_id=resource_id,
                    )
                    response = jsonify(response_payload)
                    response.status_code = 201
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return _idempotency_error(context, error)
        except AccountCommandError as error:
            return guards.error_response(error.code, error.message, error.status_code)
        if resource_id is not None:
            set_user_audit(resource_id)
        return response

    @app.patch("/api/v1/admin/users/<user_id>")
    @guards.require_scope("app.admin")
    def account_users_update(user_id: str):
        payload = AccountUpdateRequest.model_validate(read_json_payload())
        idempotency_key, key_error = validate_idempotency_key()
        if key_error is not None:
            return key_error
        assert idempotency_key is not None
        resource_id: str | None = None
        try:
            with services.database.immediate_session() as session:
                account_actor, transport, principal = authorize_admin(session)
                initial_id = initial_user_id(session)
                if not installation_mode_in_session(session).enabled and user_id == initial_id and (
                    ("role" in payload.model_fields_set and payload.role != "administrator")
                    or payload.is_active is False
                ):
                    raise AccountCommandError(
                        "initial_administrator_required",
                        "Enable team access before retiring the initial Administrator.",
                        409,
                    )
                reservation = services.idempotency.begin(
                    session,
                    principal=principal,
                    operation_id=f"admin.users.{transport.user_id}.update:{user_id}",
                    idempotency_key=idempotency_key,
                    payload=payload.model_dump(mode="json"),
                )
                if reservation.replayed:
                    replay = reservation.response
                    if replay is None:
                        raise RuntimeError("The completed account response is unavailable.")
                    response_payload, status_code = replay
                    response = jsonify(response_payload)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                else:
                    summary = commands.update_user_in_session(
                        session,
                        account_actor,
                        user_id,
                        expected_auth_revision=payload.expected_auth_revision,
                        display_name=payload.display_name,
                        role=payload.role,
                        is_active=payload.is_active,
                    )
                    response_payload = _summary_payload(summary)
                    services.idempotency.complete(
                        session,
                        reservation,
                        response=response_payload,
                        status_code=200,
                        resource_kind="user",
                        resource_id=user_id,
                    )
                    response = jsonify(response_payload)
                    response.status_code = 200
                resource_id = user_id
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return _idempotency_error(context, error)
        except AccountCommandError as error:
            return guards.error_response(error.code, error.message, error.status_code)
        if resource_id is not None:
            set_user_audit(resource_id)
        return response

    @app.post("/api/v1/admin/users/<user_id>/password-reset")
    @guards.require_scope("app.admin")
    def account_users_password_reset(user_id: str):
        payload = AccountResetIssueRequest.model_validate(read_json_payload())
        try:
            with services.database.immediate_session() as session:
                account_actor, _transport, _principal = authorize_admin(session)
                grant = commands.issue_password_reset_in_session(
                    session,
                    account_actor,
                    user_id,
                    expected_auth_revision=payload.expected_auth_revision,
                )
                response_payload = AccountResetIssueResponse(
                    grant_id=grant.grant_id,
                    user_id=grant.user_id,
                    expires_at=grant.expires_at,
                    reset_path=f"/password-reset#token={quote(grant.raw_token, safe='')}",
                ).model_dump(mode="json")
        except AccountCommandError as error:
            return guards.error_response(error.code, error.message, error.status_code)
        set_user_audit(grant.user_id)
        return jsonify(response_payload)

    @app.post("/api/v1/auth/password-reset")
    def account_password_reset_redeem():
        payload = AccountResetRedeemRequest.model_validate(read_json_payload())
        throttle_key = f"password-reset:{request.remote_addr or 'unknown'}"
        retry_after = services.login_throttle.retry_after(throttle_key)
        if retry_after > 0:
            response, status = guards.error_response(
                "password_reset_throttled",
                "Too many invalid reset attempts. Try again later.",
                429,
                {"retry_after_seconds": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, status
        try:
            commands.redeem_password_reset(payload.token, payload.new_password)
        except AccountCommandError as error:
            if error.code == "invalid_reset":
                services.login_throttle.record_failure(throttle_key)
            return guards.error_response(error.code, error.message, error.status_code)
        services.login_throttle.reset(throttle_key)
        return "", 204


__all__ = ["register_account_routes"]
