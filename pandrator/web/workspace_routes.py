"""Thin HTTP adapters for installation mode and shared workspace commands."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import g, jsonify, request
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, WorkspaceNotFound
from .access.workspace_policy import (
    WorkspaceAction,
    require_workspace_access,
    workspace_payload,
)
from .accounts.actor_authorization import ActorAuthorizationDenied, require_actor_in_session
from .accounts.installation_mode import (
    InstallationModeError,
    enable_collaboration_in_session,
    installation_mode_in_session,
)
from .accounts.workspace_commands import WorkspaceCommands
from .accounts.workspace_contracts import WorkspaceCommandError
from .accounts.workspace_queries import WorkspaceQueries
from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .workspace_schemas import (
    EnableCollaborationRequest,
    WorkspaceCreateRequest,
    WorkspaceInviteRequest,
    WorkspaceRenameRequest,
    WorkspaceRevisionRequest,
    WorkspaceRoleRequest,
)


def register_workspace_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards
    commands = WorkspaceCommands(audit=services.audit.record_command_in_session)

    def errors(function: Callable[..., Any]):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (InstallationModeError, WorkspaceCommandError) as error:
                return guards.error_response(error.code, error.message, error.status_code)
            except ActorAuthorizationDenied:
                return guards.error_response("forbidden", "The requested action is not permitted.", 403)
            except WorkspaceNotFound:
                return guards.error_response("not_found", "Workspace not found.", 404)
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return guards.error_response(error.code, str(error), 409)
        return wrapped

    def read_payload(model):
        request.max_content_length = 16 * 1024
        return model.model_validate(request.get_json(silent=True) or {})

    def collaboration(session: Session) -> AccessContext:
        access = guards.access_context()
        if not installation_mode_in_session(session).enabled:
            raise InstallationModeError(
                "collaboration_disabled", "Enable collaboration to use shared workspaces."
            )
        return access

    def key() -> str:
        # Validate before reserving anything. Row creation alone needs a receipt.
        try:
            return services.idempotency.validate_key(request.headers.get("Idempotency-Key"))
        except ValueError as error:
            raise WorkspaceCommandError("validation_error", str(error), 422) from None

    def mark(workspace_id: str) -> None:
        g.audit_resource_kind, g.audit_resource_id = "workspace", workspace_id

    @app.get("/api/v1/admin/installation")
    @guards.require_scope("app.admin")
    @errors
    def installation_get():
        with services.database.snapshot_session() as session:
            access = guards.access_context()
            require_actor_in_session(session, access.actor, required_scopes={"app.admin"}, require_admin=True)
            return jsonify(installation_mode_in_session(session).payload())

    @app.post("/api/v1/admin/installation/collaboration")
    @guards.require_scope("app.admin")
    @errors
    def installation_enable():
        payload = read_payload(EnableCollaborationRequest)
        with services.database.immediate_session() as session:
            access = guards.access_context()
            before = installation_mode_in_session(session)
            result = enable_collaboration_in_session(session, access, expected_revision=payload.expected_revision)
            if not before.enabled:
                services.audit.record_command_in_session(
                    session, access, action="installation.collaboration.enable",
                    workspace_id="auth.installation", resource_kind="installation",
                    metadata={"mode": "collaboration", "revision": result.revision},
                )
            return jsonify(result.payload())

    @app.get("/api/v1/workspaces")
    @guards.require_scope("app.read")
    @errors
    def workspace_list():
        with services.database.snapshot_session() as session:
            access = collaboration(session)
            return jsonify({"items": WorkspaceQueries.list_workspaces(
                session, access, include_archived=request.args.get("include_archived") == "true"
            )})

    @app.post("/api/v1/workspaces")
    @guards.require_scope("app.workspaces.enroll")
    @errors
    def workspace_create():
        payload, idempotency_key = read_payload(WorkspaceCreateRequest), key()
        with services.database.immediate_session() as session:
            access = collaboration(session)
            require_actor_in_session(session, access.actor, required_scopes={"app.read", "app.write", "app.workspaces.enroll"})
            principal = guards.principal()
            assert principal is not None
            reservation = services.idempotency.begin(
                session, principal=principal,
                operation_id=f"workspaces.{access.actor.user_id}.create",
                idempotency_key=idempotency_key, payload=payload.model_dump(mode="json"),
            )
            if reservation.replayed:
                identifier = reservation.record.resource_id
                if identifier is None:
                    raise WorkspaceNotFound()
                result = workspace_payload(require_workspace_access(session, access, identifier))
            else:
                result = commands.create(session, access, name=payload.name)
                services.idempotency.complete(
                    session, reservation, response=result, status_code=201,
                    resource_kind="workspace", resource_id=result["id"],
                )
            mark(result["id"])
            response = jsonify(result)
            if reservation.replayed:
                response.headers["Idempotency-Replayed"] = "true"
            return response, 201

    @app.get("/api/v1/workspaces/<workspace_id>")
    @guards.require_scope("app.read")
    @errors
    def workspace_get(workspace_id: str):
        with services.database.snapshot_session() as session:
            access = collaboration(session)
            # Archived workspaces expose management metadata only to a Manager.
            try:
                result = require_workspace_access(session, access, workspace_id)
            except WorkspaceNotFound:
                result = require_workspace_access(session, access, workspace_id, action=WorkspaceAction.MANAGE_READ, allow_archived=True)
            return jsonify(workspace_payload(result))

    @app.patch("/api/v1/workspaces/<workspace_id>")
    @guards.require_scope("app.write")
    @errors
    def workspace_rename(workspace_id: str):
        payload = read_payload(WorkspaceRenameRequest)
        with services.database.immediate_session() as session:
            result = commands.rename(session, collaboration(session), workspace_id, name=payload.name, expected_revision=payload.expected_revision)
            mark(workspace_id)
            return jsonify(result)

    @app.get("/api/v1/workspaces/<workspace_id>/members")
    @guards.require_scope("app.read")
    @errors
    def workspace_members(workspace_id: str):
        with services.database.snapshot_session() as session:
            return jsonify({"items": WorkspaceQueries.members(session, collaboration(session), workspace_id)})

    @app.get("/api/v1/workspaces/<workspace_id>/user-search")
    @guards.require_scope("app.read")
    @errors
    def workspace_user_search(workspace_id: str):
        with services.database.snapshot_session() as session:
            return jsonify({"items": WorkspaceQueries.search_users(session, collaboration(session), workspace_id, str(request.args.get("q") or ""))})

    @app.patch("/api/v1/workspaces/<workspace_id>/members/<user_id>")
    @guards.require_scope("app.write")
    @errors
    def workspace_member_role(workspace_id: str, user_id: str):
        payload = read_payload(WorkspaceRoleRequest)
        with services.database.immediate_session() as session:
            result = commands.set_member_role(session, collaboration(session), workspace_id, user_id=user_id, role=payload.role, expected_revision=payload.expected_revision)
            mark(workspace_id)
            return jsonify(result)

    @app.delete("/api/v1/workspaces/<workspace_id>/members/<user_id>")
    @guards.require_scope("app.write")
    @errors
    def workspace_member_remove(workspace_id: str, user_id: str):
        payload = read_payload(WorkspaceRevisionRequest)
        with services.database.immediate_session() as session:
            result = commands.remove_member(session, collaboration(session), workspace_id, user_id=user_id, expected_revision=payload.expected_revision)
            mark(workspace_id)
            return jsonify(result)

    def lifecycle(workspace_id: str, action: str):
        payload = read_payload(WorkspaceRevisionRequest)
        with services.database.immediate_session() as session:
            access = collaboration(session)
            command = {"leave": commands.leave, "archive": commands.archive, "restore": commands.restore}[action]
            result = command(session, access, workspace_id, expected_revision=payload.expected_revision)
            mark(workspace_id)
            return jsonify(result)

    @app.post("/api/v1/workspaces/<workspace_id>/leave")
    @guards.require_scope("app.write")
    @errors
    def workspace_leave(workspace_id: str):
        return lifecycle(workspace_id, "leave")

    @app.post("/api/v1/workspaces/<workspace_id>/archive")
    @guards.require_scope("app.write")
    @errors
    def workspace_archive(workspace_id: str):
        return lifecycle(workspace_id, "archive")

    @app.post("/api/v1/workspaces/<workspace_id>/restore")
    @guards.require_scope("app.write")
    @errors
    def workspace_restore(workspace_id: str):
        return lifecycle(workspace_id, "restore")

    @app.get("/api/v1/workspaces/<workspace_id>/invitations")
    @guards.require_scope("app.read")
    @errors
    def workspace_invitations(workspace_id: str):
        with services.database.snapshot_session() as session:
            return jsonify({"items": WorkspaceQueries.invitations(session, collaboration(session), workspace_id)})

    @app.post("/api/v1/workspaces/<workspace_id>/invitations")
    @guards.require_scope("app.write")
    @errors
    def workspace_invite(workspace_id: str):
        payload, idempotency_key = read_payload(WorkspaceInviteRequest), key()
        with services.database.immediate_session() as session:
            access = collaboration(session)
            require_workspace_access(session, access, workspace_id, action=WorkspaceAction.MANAGE)
            principal = guards.principal()
            assert principal is not None
            reservation = services.idempotency.begin(
                session, principal=principal,
                operation_id=f"workspaces.{access.actor.user_id}.{workspace_id}.invite",
                idempotency_key=idempotency_key, payload=payload.model_dump(mode="json"),
            )
            if reservation.replayed:
                result = next((row for row in WorkspaceQueries.invitations(session, access, workspace_id) if row["id"] == reservation.record.resource_id), None)
                if result is None:
                    raise WorkspaceCommandError("invitation_not_found", "Invitation not found.", 404)
            else:
                result = commands.invite(session, access, workspace_id, user_id=payload.user_id, role=payload.role, expected_revision=payload.expected_revision)
                services.idempotency.complete(session, reservation, response=result, status_code=201, resource_kind="workspace_invitation", resource_id=result["id"])
            mark(workspace_id)
            response = jsonify(result)
            if reservation.replayed:
                response.headers["Idempotency-Replayed"] = "true"
            return response, 201

    @app.delete("/api/v1/workspaces/<workspace_id>/invitations/<invitation_id>")
    @guards.require_scope("app.write")
    @errors
    def workspace_invite_revoke(workspace_id: str, invitation_id: str):
        payload = read_payload(WorkspaceRevisionRequest)
        with services.database.immediate_session() as session:
            result = commands.revoke_invitation(session, collaboration(session), workspace_id, invitation_id=invitation_id, expected_revision=payload.expected_revision)
            mark(workspace_id)
            return jsonify(result)

    @app.get("/api/v1/workspace-invitations")
    @guards.require_scope("app.read")
    @errors
    def workspace_inbox():
        with services.database.snapshot_session() as session:
            return jsonify({"items": WorkspaceQueries.inbox(session, collaboration(session))})

    def respond(invitation_id: str, accept: bool):
        with services.database.immediate_session() as session:
            access = collaboration(session)
            command = commands.accept_invitation if accept else commands.decline_invitation
            result = command(session, access, invitation_id)
            mark(result["workspace_id"])
            return jsonify(result)

    @app.post("/api/v1/workspace-invitations/<invitation_id>/accept")
    @guards.require_scope("app.workspaces.enroll")
    @errors
    def workspace_invite_accept(invitation_id: str):
        return respond(invitation_id, True)

    @app.post("/api/v1/workspace-invitations/<invitation_id>/decline")
    @guards.require_scope("app.write")
    @errors
    def workspace_invite_decline(invitation_id: str):
        return respond(invitation_id, False)
