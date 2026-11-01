"""HTTP adapters for the existing project ownership and viewing-grant core."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import g, jsonify, request
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, ProjectNotFound
from .accounts.installation_mode import InstallationModeError, installation_mode_in_session
from .domain_blueprints import DomainBlueprints
from .project_sharing_schemas import ProjectViewingRevisionRequest, SharedProjectsQuery
from .projects.sharing_commands import ProjectSharingCommands
from .projects.sharing_contracts import ProjectSharingError
from .projects.sharing_queries import ProjectSharingQueries
from .route_context import RouteContext


def register_project_sharing_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards

    def audit(
        session: Session, context: AccessContext, *, action: str,
        project_id: str, metadata: dict[str, str | int],
    ) -> None:
        audit_metadata: dict[str, str | int | bool | None] = {
            key: value for key, value in metadata.items()
        }
        services.audit.record_command_in_session(
            session, context, action=action, workspace_id=project_id,
            resource_kind="project", metadata=audit_metadata,
        )

    commands = ProjectSharingCommands(audit=audit)

    def errors(function: Callable[..., Any]):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (ProjectSharingError, InstallationModeError) as error:
                return guards.error_response(error.code, error.message, error.status_code)
            except ProjectNotFound:
                return guards.error_response("not_found", "Project not found.", 404)
        return wrapped

    def collaboration(session: Session) -> AccessContext:
        access = guards.access_context()
        if not installation_mode_in_session(session).enabled:
            raise InstallationModeError(
                "collaboration_disabled", "Enable collaboration to share projects."
            )
        return access

    @app.get("/api/v1/projects/shared-with-me")
    @guards.require_scope("app.read")
    @errors
    def shared_projects_get():
        query = SharedProjectsQuery.model_validate(request.args.to_dict())
        with services.database.snapshot_session() as session:
            access = collaboration(session)
            return jsonify({"items": ProjectSharingQueries.shared_with_me(
                session, access, **query.model_dump(),
            )})

    @app.get("/api/v1/projects/<project_id>/viewers")
    @guards.require_scope("app.read")
    @errors
    def project_viewers_get(project_id: str):
        with services.database.snapshot_session() as session:
            access = collaboration(session)
            return jsonify(ProjectSharingQueries.list_viewers(session, access, project_id))

    @app.get("/api/v1/projects/<project_id>/viewers/users")
    @guards.require_scope("app.read")
    @errors
    def project_viewing_candidates_get(project_id: str):
        with services.database.snapshot_session() as session:
            access = collaboration(session)
            return jsonify({"items": ProjectSharingQueries.search_users(
                session, access, project_id, request.args.get("query", ""),
            )})

    def mutate(project_id: str, user_id: str, *, revoke: bool):
        request.max_content_length = 8 * 1024
        payload = ProjectViewingRevisionRequest.model_validate(request.get_json(silent=True) or {})
        with services.database.immediate_session() as session:
            access = collaboration(session)
            action = commands.revoke if revoke else commands.grant
            result = action(
                session, access, project_id, user_id,
                expected_revision=payload.expected_revision,
            )
            g.audit_resource_kind, g.audit_resource_id = "project", project_id
            return jsonify(result)

    @app.put("/api/v1/projects/<project_id>/viewers/<user_id>")
    @guards.require_scope("app.write")
    @errors
    def project_viewer_put(project_id: str, user_id: str):
        return mutate(project_id, user_id, revoke=False)

    @app.post("/api/v1/projects/<project_id>/viewers/<user_id>/revoke")
    @guards.require_scope("app.write")
    @errors
    def project_viewer_revoke(project_id: str, user_id: str):
        return mutate(project_id, user_id, revoke=True)
