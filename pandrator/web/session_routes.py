"""HTTP routes for listing and managing sessions."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import g, jsonify, request
from sqlalchemy import select

from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Job, SessionRecord, TranslationProject, TranslationProjectBranch, new_id
from .route_context import RouteContext
from .schemas import SessionCreate, SessionForkRequest, SessionUpdate
from .sessions import RevisionConflict


def register_session_list_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    _session_payload: Callable[[SessionRecord], dict[str, Any]],
) -> None:
    database = context.services.database
    sessions = context.services.sessions
    require_auth = context.guards.require_auth

    @app.get("/api/v1/sessions")
    @require_auth
    def session_list():
        query = request.args.get("q") or request.args.get("query")
        items = [
            _session_payload(item)
            for item in sessions.list(
                include_trashed=request.args.get("include_trashed") == "true",
                query=query,
            )
        ]
        ids = [item["id"] for item in items]
        memberships = {}
        with database.session() as db_session:
            for project in db_session.scalars(
                select(TranslationProject).where(TranslationProject.source_session_id.in_(ids))
            ):
                memberships[project.source_session_id] = {
                    "id": project.id,
                    "name": project.name,
                    "source_session_id": project.source_session_id,
                    "role": "source",
                }
            for branch, project in db_session.execute(
                select(TranslationProjectBranch, TranslationProject)
                .join(
                    TranslationProject, TranslationProject.id == TranslationProjectBranch.project_id
                )
                .where(TranslationProjectBranch.session_id.in_(ids))
            ):
                memberships[branch.session_id] = {
                    "id": project.id,
                    "name": project.name,
                    "source_session_id": project.source_session_id,
                    "role": "branch",
                    "target_language": branch.target_language,
                }
        for item in items:
            item["translation_project"] = memberships.get(item["id"])
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
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            request_payload = payload.model_dump(mode="json")
            created_directory: Path | None = None
            try:
                with database.immediate_session() as db_session:
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
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    existing = sessions.find_active_by_name_in_session(
                        db_session,
                        payload.name,
                    )
                    if existing is not None and payload.overwrite_session_id != existing.id:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "duplicate_session",
                            f'A session named "{existing.name}" already exists.',
                            409,
                            {"existing_session": _session_payload(existing)},
                        )
                    if payload.overwrite_session_id and (
                        existing is None or existing.id != payload.overwrite_session_id
                    ):
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "overwrite_conflict",
                            "The session selected for replacement no longer matches this name.",
                            409,
                        )
                    if existing is not None:
                        active = db_session.scalar(
                            select(Job).where(
                                Job.session_id == existing.id,
                                Job.status.in_(
                                    (
                                        "queued",
                                        "running",
                                        "cancel_requested",
                                    )
                                ),
                            )
                        )
                        if active is not None:
                            abandon_idempotency(
                                db_session,
                                reservation,
                            )
                            return error_response(
                                "session_busy",
                                "Stop or cancel active work before replacing this session.",
                                409,
                            )
                    if existing is not None:
                        sessions.trash(
                            existing.id,
                            existing.revision,
                            db_session=db_session,
                        )
                    record_id = new_id()
                    storage_key = new_id()
                    record = sessions.create(
                        payload.name,
                        workflow_kind=payload.workflow_kind,
                        source_language=payload.source_language,
                        target_language=payload.target_language,
                        workflow_preset=payload.workflow_preset,
                        included_stages=payload.included_stages,
                        multilingual_setup=payload.multilingual_setup,
                        record_id=record_id,
                        storage_key=storage_key,
                        db_session=db_session,
                    )
                    created_directory = paths.sessions / record.storage_key
                    created_directory.mkdir(
                        parents=True,
                        exist_ok=False,
                    )
                    result = _session_payload(record)
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
            except Exception:
                if created_directory is not None and created_directory.is_dir():
                    try:
                        created_directory.rmdir()
                    except OSError:
                        pass
                raise
        existing = sessions.find_active_by_name(payload.name)
        if existing is not None and payload.overwrite_session_id != existing.id:
            return error_response(
                "duplicate_session",
                f'A session named "{existing.name}" already exists.',
                409,
                {"existing_session": _session_payload(existing)},
            )
        if payload.overwrite_session_id and (
            existing is None or existing.id != payload.overwrite_session_id
        ):
            return error_response(
                "overwrite_conflict",
                "The session selected for replacement no longer matches this name. Review the current session list and try again.",
                409,
            )
        if existing is not None:
            with database.session() as db_session:
                active = db_session.scalar(
                    select(Job).where(
                        Job.session_id == existing.id,
                        Job.status.in_(("queued", "running", "cancel_requested")),
                    )
                )
                if active is not None:
                    return error_response(
                        "session_busy",
                        "Stop or cancel active work before replacing this session.",
                        409,
                    )
            sessions.trash(existing.id, existing.revision)
        record = sessions.create(
            payload.name,
            workflow_kind=payload.workflow_kind,
            source_language=payload.source_language,
            target_language=payload.target_language,
            workflow_preset=payload.workflow_preset,
            included_stages=payload.included_stages,
            multilingual_setup=payload.multilingual_setup,
        )
        (paths.sessions / record.storage_key).mkdir(parents=True, exist_ok=False)
        response = jsonify(_session_payload(record))
        response.status_code = 201
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.get("/api/v1/sessions/<session_id>")
    @require_auth
    def session_get(session_id: str):
        try:
            record = sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
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
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            record = sessions.update(session_id, revision, changes)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("session_busy", str(error), 409)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
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
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("session_busy", str(error), 409)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
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
            record = sessions.restore(session_id, revision)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("session_busy", str(error), 409)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/reindex")
    @require_auth
    def session_reindex(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"session_id": session_id, "reports": artifacts.reconcile(session_id)})
