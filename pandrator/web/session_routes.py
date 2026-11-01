"""HTTP routes for listing and managing sessions."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from flask import g, jsonify, request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .access.contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from .access.folder_policy import (
    FolderNotFound,
    require_folder_inputs,
    require_folder_workspace,
)
from .access.project_policy import personal_workspace_id, require_project_creation
from .access.session_policy import SessionNotFound, require_session_access
from .access.session_presentation import session_access_payloads
from .access.session_queries import (
    find_session_by_name,
    get_session_record,
    list_session_records,
)
from .database import Database
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Job, SessionRecord, TranslationProject, TranslationProjectBranch, new_id
from .project_folders import set_session_project_folders
from .projects.models import Project
from .route_context import RouteContext
from .schemas import SessionCreate, SessionForkRequest, SessionUpdate
from .sessions import RevisionConflict
from .settings_policy import RevisionConflict as FolderRevisionConflict


def session_project_memberships(
    database: Database,
    session_ids: Sequence[str],
    *,
    db_session: Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Read navigation memberships without loading project histories or settings."""
    if not session_ids:
        return {}
    if db_session is None:
        with database.session() as session:
            return session_project_memberships(database, session_ids, db_session=session)
    memberships: dict[str, dict[str, Any]] = {}
    for row in db_session.execute(
        select(
            TranslationProject.id,
            TranslationProject.name,
            TranslationProject.source_session_id,
        ).where(TranslationProject.source_session_id.in_(session_ids))
    ).mappings():
        memberships[row["source_session_id"]] = {**row, "role": "source"}
    for row in db_session.execute(
        select(
            TranslationProjectBranch.session_id,
            TranslationProject.id,
            TranslationProject.name,
            TranslationProject.source_session_id,
            TranslationProjectBranch.target_language,
        )
        .join(TranslationProject, TranslationProject.id == TranslationProjectBranch.project_id)
        .where(TranslationProjectBranch.session_id.in_(session_ids))
    ).mappings():
        memberships[row["session_id"]] = {
            "id": row["id"],
            "name": row["name"],
            "source_session_id": row["source_session_id"],
            "role": "branch",
            "target_language": row["target_language"],
        }
    return memberships


def register_session_list_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    _session_payload: Callable[[SessionRecord], dict[str, Any]],
) -> None:
    database = context.services.database
    require_auth = context.guards.require_auth

    @app.get("/api/v1/sessions")
    @require_auth
    def session_list():
        access = context.guards.access_context()
        query = request.args.get("q") or request.args.get("query")
        with database.snapshot_session() as db_session:
            records = list_session_records(
                db_session,
                access,
                include_trashed=request.args.get("include_trashed") == "true",
                query=query,
                workspace_id=request.args.get("workspace_id"),
            )
            items = [_session_payload(item) for item in records]
            ids = [item["id"] for item in items]
            memberships = session_project_memberships(database, ids, db_session=db_session)
            metadata = (
                session_access_payloads(db_session, access, ids)
                if context.services.auth.collaboration_enabled(db_session=db_session)
                else {}
            )
            for item in items:
                item["translation_project"] = memberships.get(item["id"])
                item.update(metadata.get(item["id"], {}))
        return jsonify({"items": items})


def register_session_lifecycle_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    idempotency: MutationIdempotency,
    _session_payload: Callable[[SessionRecord], dict[str, Any]],
) -> None:
    services = context.services
    paths = services.paths
    database = services.database
    sessions = services.sessions
    session_forks = services.session_forks
    artifacts = services.artifacts
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    abandon_idempotency = idempotency.abandon

    @app.post("/api/v1/sessions")
    @require_auth
    def session_create():
        payload = SessionCreate.model_validate(request.get_json(silent=True) or {})
        if payload.overwrite_session_id is not None and payload.overwrite_revision is None:
            return error_response(
                "precondition_required",
                "The current overwrite_revision is required to replace a session.",
                428,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        request_payload = payload.model_dump(mode="json")
        if payload.workspace_id is None:
            request_payload.pop("workspace_id", None)
        if payload.overwrite_revision is None:
            request_payload.pop("overwrite_revision", None)
        if payload.folder_id is None:
            # Keep old no-folder requests at their established idempotency digest.
            request_payload.pop("folder_id", None)
            request_payload.pop("folder_revision", None)
        created_directory: Path | None = None
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                selected_workspace_id = payload.workspace_id
                if selected_workspace_id is None:
                    selected_workspace_id = personal_workspace_id(db_session, access)
                selected_workspace_id = require_project_creation(
                    db_session,
                    access,
                    selected_workspace_id,
                )
                if payload.folder_id is not None:
                    require_folder_workspace(
                        db_session,
                        access,
                        selected_workspace_id,
                        write=True,
                    )

                if payload.overwrite_session_id is not None:
                    old_access = require_session_access(
                        db_session,
                        access,
                        payload.overwrite_session_id,
                        ProjectAction.READ,
                    )
                    if old_access.workspace_id != selected_workspace_id:
                        raise SessionNotFound()
                    # A replacement target outside the chosen workspace has the
                    # same result as a missing target, even if it is readable.
                    if ProjectAction.TRASH not in old_access.allowed_actions:
                        raise AccessDenied()

                reservation = None
                if idempotency_key is not None:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="createSession",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)

                    if reservation.response is not None:
                        result, status_code = reservation.response
                        resource_id = reservation.record.resource_id
                        if (
                            reservation.record.resource_kind != "session"
                            or not isinstance(resource_id, str)
                            or not resource_id
                            or result.get("id") != resource_id
                        ):
                            raise SessionNotFound()
                        replay_access = require_session_access(
                            db_session,
                            access,
                            resource_id,
                            ProjectAction.READ,
                        )
                        if replay_access.workspace_id != selected_workspace_id:
                            raise SessionNotFound()
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
                        return response

                if payload.folder_id is not None:
                    require_folder_inputs(
                        db_session,
                        selected_workspace_id,
                        folder_ids=(payload.folder_id,),
                    )

                existing = find_session_by_name(
                    db_session,
                    access,
                    payload.name,
                    workspace_id=selected_workspace_id,
                )
                if payload.overwrite_session_id is None and existing is not None:
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response(
                        "duplicate_session",
                        f'A session named "{existing.name}" already exists.',
                        409,
                        {"existing_session": _session_payload(existing)},
                    )

                if payload.overwrite_session_id is not None:
                    if existing is None or existing.id != payload.overwrite_session_id:
                        if reservation is not None:
                            abandon_idempotency(db_session, reservation)
                        return error_response(
                            "overwrite_conflict",
                            "The session selected for replacement no longer matches this name.",
                            409,
                        )
                    if existing.revision != payload.overwrite_revision:
                        if reservation is not None:
                            abandon_idempotency(db_session, reservation)
                        return error_response(
                            "revision_conflict",
                            f"Expected revision {payload.overwrite_revision}, found {existing.revision}.",
                            409,
                            {"current_revision": existing.revision},
                        )
                    active = db_session.scalar(
                        select(Job).where(
                            Job.session_id == existing.id,
                            Job.status.in_(
                                ("queued", "running", "cancel_requested")
                            ),
                        )
                    )
                    if active is not None:
                        if reservation is not None:
                            abandon_idempotency(db_session, reservation)
                        return error_response(
                            "session_busy",
                            "Stop or cancel active work before replacing this session.",
                            409,
                        )
                    sessions.trash(
                        existing.id,
                        cast(int, payload.overwrite_revision),
                        db_session=db_session,
                    )

                record_id = new_id()
                storage_key = new_id()
                record = sessions.create(
                    payload.name,
                    workflow_kind=payload.workflow_kind,
                    interface_mode=payload.interface_mode,
                    source_language=payload.source_language,
                    target_language=payload.target_language,
                    workflow_preset=payload.workflow_preset,
                    included_stages=payload.included_stages,
                    multilingual_setup=payload.multilingual_setup,
                    record_id=record_id,
                    storage_key=storage_key,
                    workspace_id=selected_workspace_id,
                    db_session=db_session,
                )
                project = db_session.get(Project, record.project_id)
                if project is None:
                    raise RuntimeError("The new session project was not created.")
                project.created_by_user_id = access.actor.user_id
                # Ownership and content rights are inherited from the workspace;
                # creation records provenance without a redundant per-project grant.
                db_session.flush()
                if payload.folder_id is not None:
                    try:
                        set_session_project_folders(
                            db_session,
                            record.id,
                            folder_ids=[payload.folder_id],
                            expected_revisions={
                                payload.folder_id: cast(int, payload.folder_revision)
                            },
                        )
                    except KeyError as error:
                        raise FolderNotFound() from error

                new_directory = paths.sessions / record.storage_key
                new_directory.mkdir(parents=True, exist_ok=False)
                created_directory = new_directory
                result = _session_payload(record)
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="session",
                        resource_id=record.id,
                    )
                g.audit_resource_kind = "session"
                g.audit_resource_id = record.id

            response = jsonify(result)
            response.status_code = 201
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        except WorkspaceNotFound:
            if created_directory is not None and created_directory.is_dir():
                try:
                    created_directory.rmdir()
                except OSError:
                    pass
            return error_response("not_found", "Workspace not found.", 404)
        except SessionNotFound:
            if created_directory is not None and created_directory.is_dir():
                try:
                    created_directory.rmdir()
                except OSError:
                    pass
            return error_response("not_found", "Session not found.", 404)
        except FolderNotFound:
            if created_directory is not None and created_directory.is_dir():
                try:
                    created_directory.rmdir()
                except OSError:
                    pass
            return error_response("not_found", "Folder or session not found.", 404)
        except FolderRevisionConflict as error:
            if created_directory is not None and created_directory.is_dir():
                try:
                    created_directory.rmdir()
                except OSError:
                    pass
            return error_response("revision_conflict", str(error), 409)
        except Exception:
            if created_directory is not None and created_directory.is_dir():
                try:
                    created_directory.rmdir()
                except OSError:
                    pass
            raise

    @app.get("/api/v1/sessions/<session_id>")
    @require_auth
    def session_get(session_id: str):
        access = context.guards.access_context()
        try:
            with database.snapshot_session() as db_session:
                record = get_session_record(db_session, access, session_id)
                payload = _session_payload(record)
                if services.auth.collaboration_enabled(db_session=db_session):
                    payload.update(session_access_payloads(db_session, access, [session_id]).get(session_id, {}))
                payload["translation_project"] = session_project_memberships(
                    database, [session_id], db_session=db_session
                ).get(session_id)
                revision = record.revision
        except SessionNotFound:
            return error_response("not_found", "Session not found.", 404)
        response = jsonify(payload)
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/forks")
    @require_auth
    def session_fork(session_id: str):
        payload = SessionForkRequest.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        created_directory: Path | None = None
        try:
            with database.immediate_session() as db_session:
                reservation = None
                request_payload = {
                    "session_id": session_id,
                    **payload.model_dump(mode="json"),
                }
                if idempotency_key is not None:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="forkSession",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    if reservation.response is not None:
                        replayed, status_code = reservation.response
                        response = jsonify(replayed)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response

                forked = session_forks.fork_in_session(
                    db_session,
                    session_id,
                    payload.checkpoint_artifact_id,
                    name=payload.name or "",
                    interface_mode=payload.interface_mode,
                    expected_revision=payload.expected_revision,
                    carry_media_assets=payload.carry_media_assets,
                    target_language=payload.target_language,
                )
                created_directory = forked.directory
                result = {
                    **_session_payload(forked.record),
                    "forked_from_session_id": session_id,
                    "checkpoint_artifact_id": forked.checkpoint_artifact_id,
                    "copied_stages": list(forked.copied_stages),
                    "copied_media_artifact_ids": list(forked.copied_media_artifact_ids),
                }
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="session",
                        resource_id=forked.record.id,
                    )
                g.audit_resource_kind = "session"
                g.audit_resource_id = forked.record.id
            response = jsonify(result)
            response.status_code = 201
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        except KeyError:
            return error_response(
                "not_found",
                "The session or selected checkpoint was not found.",
                404,
            )
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except FileNotFoundError as error:
            return error_response("checkpoint_missing", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        except OSError as error:
            return error_response(
                "fork_failed",
                f"The session fork could not be created: {error}",
                409,
            )
        except Exception:
            if created_directory is not None:
                shutil.rmtree(created_directory, ignore_errors=True)
            raise

    @app.patch("/api/v1/sessions/<session_id>")
    @require_auth
    def session_update(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current revision.",
                428,
            )
        payload = SessionUpdate.model_validate(request.get_json(silent=True) or {})
        raw_changes = payload.model_dump(exclude_unset=True)
        changes = {
            key: value
            for key, value in raw_changes.items()
            if value is not None or key in {"target_language", "multilingual_setup"}
        }
        if "included_stages" in changes:
            changes["included_stages_json"] = changes.pop("included_stages")
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    access = context.guards.access_context()
                    require_session_access(
                        db_session,
                        access,
                        session_id,
                        ProjectAction.EDIT,
                    )
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="updateSession",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "expected_revision": revision,
                            "changes": changes,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    current = db_session.get(
                        SessionRecord,
                        session_id,
                    )
                    if current is None:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "not_found",
                            "Session not found.",
                            404,
                        )
                    if current.revision != revision:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "revision_conflict",
                            f"Expected revision {revision}, found {current.revision}.",
                            409,
                            {"current_revision": (current.revision)},
                        )
                    record = sessions.update(
                        session_id,
                        revision,
                        changes,
                        db_session=db_session,
                    )
                    result = _session_payload(record)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="session",
                        resource_id=session_id,
                    )
                    g.audit_resource_kind = "session"
                    g.audit_resource_id = session_id
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                RevisionConflict,
                ValueError,
            ) as error:
                if isinstance(error, RevisionConflict):
                    return error_response(
                        "revision_conflict",
                        str(error),
                        409,
                    )
                return idempotency_failure(error)
            except KeyError:
                return error_response("not_found", "Session not found.", 404)
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                require_session_access(
                    db_session,
                    access,
                    session_id,
                    ProjectAction.EDIT,
                )
                record = sessions.update(
                    session_id,
                    revision,
                    changes,
                    db_session=db_session,
                )
                result = _session_payload(record)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("session_busy", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/sessions/<session_id>")
    @require_auth
    def session_trash(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current session revision.",
                428,
            )
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                require_session_access(
                    db_session,
                    access,
                    session_id,
                    ProjectAction.TRASH,
                )
                active = db_session.scalar(
                    select(Job).where(
                        Job.session_id == session_id,
                        Job.status.in_(("queued", "running", "cancel_requested")),
                    )
                )
                if active is not None:
                    return error_response(
                        "session_busy",
                        "Stop or cancel active work before moving this session to trash.",
                        409,
                    )
                record = sessions.trash(session_id, revision, db_session=db_session)
                result = _session_payload(record)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("session_busy", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/restore")
    @require_auth
    def session_restore(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current session revision.",
                428,
            )
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                require_session_access(
                    db_session,
                    access,
                    session_id,
                    ProjectAction.RESTORE,
                )
                record = sessions.restore(
                    session_id,
                    revision,
                    db_session=db_session,
                )
                result = _session_payload(record)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("session_busy", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/reindex")
    @require_auth
    def session_reindex(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"session_id": session_id, "reports": artifacts.reconcile(session_id)})
