"""HTTP routes for revisioned media-edit plans."""

from __future__ import annotations

from typing import Any

from flask import jsonify, request
from pydantic import ValidationError
from sqlalchemy import select

from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .media_edit import MediaEditInputsChanged, MediaEditRevisionConflict
from .models import Job
from .route_context import RouteContext
from .schemas import (
    MediaEditBoundaryRequest,
    MediaEditPrepareRequest,
    MediaEditProposeRequest,
    MediaEditRenderRequest,
    MediaEditUpdateRequest,
)
from .workspace import adapt_runtime_settings, stable_hash


def _safe_job_payload(job) -> dict[str, Any]:
    """Serialize the public job shape without importing api route internals."""

    return {
        "id": job.id,
        "kind": job.kind,
        "session_id": job.session_id,
        "workflow_run_id": job.workflow_run_id,
        "status": job.status,
        "result": job.result_json,
        "progress": float(job.progress or 0.0),
        "progress_detail": job.progress_detail,
        "error": (
            {"code": job.error_code, "message": job.error_message}
            if job.error_code or job.error_message
            else None
        ),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat(),
    }


def register_media_edit_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    media_edit = services.media_edit
    error_response = context.guards.error_response
    require_scope = context.guards.require_scope

    def _mutation_key():
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        principal = context.guards.principal()
        if key:
            return key, None
        if principal is not None and principal.kind in {
            "automation_client",
            "manager_bootstrap",
        }:
            return None, error_response(
                "idempotency_key_required",
                "This automation write requires Idempotency-Key.",
                400,
            )
        return None, None

    def _idempotency_error(error: Exception):
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return error_response(
                error.code,
                str(error),
                409,
                {"retryable": error.retryable},
            )
        return error_response("validation_error", str(error), 422)

    def _replay_response(reservation):
        replay = reservation.response
        if replay is None:
            return None
        payload, status_code = replay
        response = jsonify(payload)
        response.status_code = status_code
        response.headers["Idempotency-Replayed"] = "true"
        revision = (payload.get("plan") or {}).get("revision")
        if revision is None:
            revision = (payload.get("current_revision") or {}).get("revision")
        if revision is not None:
            response.headers["ETag"] = f'"{revision}"'
        return response

    def _begin_detached_reservation(operation_id: str, payload: dict[str, Any]):
        key, key_error = _mutation_key()
        if key_error is not None or key is None:
            return None, None, key_error
        principal = context.guards.principal()
        try:
            with media_edit.database.immediate_session() as db_session:
                reservation = services.idempotency.begin(
                    db_session,
                    principal=principal,
                    operation_id=operation_id,
                    idempotency_key=key,
                    payload=payload,
                )
                replay = _replay_response(reservation)
                if replay is not None:
                    return None, replay, None
                return reservation.record.id, None, None
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return None, None, _idempotency_error(error)

    def _finish_detached_reservation(
        reservation_id: str | None,
        payload: dict[str, Any],
        status_code: int,
        *,
        resource_kind: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        if reservation_id is None:
            return
        principal = context.guards.principal()
        with media_edit.database.immediate_session() as db_session:
            reservation = services.idempotency.load_in_progress(
                db_session,
                reservation_id,
                principal=principal,
            )
            services.idempotency.complete(
                db_session,
                reservation,
                response=payload,
                status_code=status_code,
                resource_kind=resource_kind,
                resource_id=resource_id,
            )

    def _abandon_detached_reservation(reservation_id: str | None) -> None:
        if reservation_id is None:
            return
        principal = context.guards.principal()
        with media_edit.database.immediate_session() as db_session:
            try:
                reservation = services.idempotency.load_in_progress(
                    db_session,
                    reservation_id,
                    principal=principal,
                )
            except (KeyError, IdempotencyInProgress):
                return
            services.idempotency.abandon_in_progress(db_session, reservation)

    def _active_revision(session_id: str, revision: int) -> dict[str, Any]:
        plan = media_edit.state(session_id).get("plan")
        if not isinstance(plan, dict) or int(plan.get("revision") or 0) != revision:
            current_value = plan.get("revision") if isinstance(plan, dict) else None
            current = int(current_value) if current_value is not None else None
            raise MediaEditRevisionConflict(revision, current)
        return plan

    def _settings_snapshot(session_id: str, section: str) -> tuple[dict[str, Any], str]:
        resolved = services.workspace_settings.get(session_id, section)
        settings = adapt_runtime_settings(
            section, dict(resolved.get("effective") or {})
        )
        return settings, stable_hash(settings)

    def _matching_job(
        db_session,
        *,
        kind: str,
        session_id: str,
        payload: dict[str, Any],
        statuses: tuple[str, ...],
    ):
        for candidate in db_session.scalars(
            select(Job)
            .where(
                Job.kind == kind,
                Job.session_id == session_id,
                Job.status.in_(statuses),
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
        ).all():
            stored = candidate.payload_json or {}
            if all(stored.get(key) == value for key, value in payload.items()):
                return candidate
        return None

    def service_error(error: Exception):
        if isinstance(error, KeyError):
            return error_response("not_found", "Session not found.", 404)
        if isinstance(error, MediaEditRevisionConflict):
            return error_response(
                "revision_conflict",
                str(error),
                409,
                {
                    "expected_revision": error.expected,
                    "current_revision": error.current,
                },
            )
        if isinstance(error, MediaEditInputsChanged):
            return error_response(
                "media_edit_inputs_changed",
                "Media-edit inputs changed while preparing; retry preparation.",
                409,
                {"retryable": True},
            )
        if isinstance(error, ValueError):
            message = str(error)
            status = 409 if "media_edit workflow" in message else 422
            return error_response(
                "workflow_conflict" if status == 409 else "validation_error",
                message,
                status,
            )
        return error_response(
            "media_edit_error", "The media-edit operation failed.", 500
        )

    @app.get("/api/v1/sessions/<session_id>/media-edit")
    @require_scope("app.read")
    def media_edit_state(session_id: str):
        try:
            return jsonify(media_edit.state(session_id))
        except (
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            ValueError,
        ) as error:
            return service_error(error)

    @app.get("/api/v1/sessions/<session_id>/media-edit/cuts")
    @require_scope("app.read")
    def media_edit_cuts(session_id: str):
        def _query_int(
            name: str, *, minimum: int | None = None, maximum: int | None = None
        ):
            raw = request.args.get(name)
            if raw is None or raw == "":
                return None
            try:
                value = int(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be an integer.") from error
            if minimum is not None and value < minimum:
                raise ValueError(f"{name} must be at least {minimum}.")
            if maximum is not None and value > maximum:
                raise ValueError(f"{name} must be at most {maximum}.")
            return value

        try:
            revision = _query_int("revision", minimum=1)
            cut_index = _query_int("cut_index", minimum=1)
            edge = request.args.get("edge")
            if edge is not None and edge not in {"start", "end"}:
                raise ValueError("edge must be 'start' or 'end'.")
            context_ms = _query_int("context_ms", minimum=250, maximum=30_000)
            cue_limit = _query_int("cue_limit", minimum=1, maximum=100)
            return jsonify(
                media_edit.list_cuts(
                    session_id,
                    revision,
                    cut_index=cut_index,
                    edge=edge,
                    context_ms=5_000 if context_ms is None else context_ms,
                    cue_limit=40 if cue_limit is None else cue_limit,
                )
            )
        except (
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            ValueError,
        ) as error:
            return service_error(error)

    @app.patch("/api/v1/sessions/<session_id>/media-edit/boundary")
    @require_scope("app.write")
    def media_edit_boundary(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if not raw_etag:
            return error_response(
                "precondition_required",
                "If-Match must contain the current media-edit revision.",
                428,
            )
        try:
            expected_revision = int(raw_etag)
            if expected_revision < 1:
                raise ValueError
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current media-edit revision.",
                428,
            )
        reservation_id = None
        try:
            payload = MediaEditBoundaryRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            reservation_id, replay, key_error = _begin_detached_reservation(
                "refineMediaEditBoundary",
                {
                    "session_id": session_id,
                    "expected_revision": expected_revision,
                    **payload.model_dump(mode="json"),
                },
            )
            if key_error is not None:
                return key_error
            if replay is not None:
                return replay
            result = media_edit.refine_boundary(
                session_id,
                expected_revision,
                cut_index=payload.cut_index,
                edge=payload.edge,
                position_ms=payload.position_ms,
                delta_ms=payload.delta_ms,
            )
            status_code = 200
            _finish_detached_reservation(
                reservation_id,
                result,
                status_code,
                resource_kind="media_edit_revision",
                resource_id=str(
                    (result.get("current_revision") or {}).get("revision_id") or ""
                )
                or None,
            )
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["current_revision"]["revision"]}"'
            return response
        except ValidationError:
            _abandon_detached_reservation(reservation_id)
            raise
        except (
            IdempotencyConflict,
            IdempotencyInProgress,
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            _abandon_detached_reservation(reservation_id)
            if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
                return _idempotency_error(error)
            return service_error(error)

    @app.post("/api/v1/sessions/<session_id>/media-edit/prepare")
    @require_scope("app.write")
    def media_edit_prepare(session_id: str):
        reservation_id = None
        try:
            payload = MediaEditPrepareRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            reservation_id, replay, key_error = _begin_detached_reservation(
                "prepareMediaEdit",
                {"session_id": session_id, **payload.model_dump(mode="json")},
            )
            if key_error is not None:
                return key_error
            if replay is not None:
                return replay
            had_plan = media_edit.state(session_id).get("plan") is not None
            result = media_edit.prepare(session_id, force=payload.force)
            status_code = 200 if had_plan else 201
            _finish_detached_reservation(
                reservation_id,
                result,
                status_code,
                resource_kind="media_edit_plan",
                resource_id=str((result.get("plan") or {}).get("plan_id") or "")
                or None,
            )
            response = jsonify(result)
            response.status_code = status_code
            return response
        except ValidationError:
            _abandon_detached_reservation(reservation_id)
            raise
        except (
            IdempotencyConflict,
            IdempotencyInProgress,
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            _abandon_detached_reservation(reservation_id)
            if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
                return _idempotency_error(error)
            return service_error(error)

    @app.put("/api/v1/sessions/<session_id>/media-edit")
    @require_scope("app.write")
    def media_edit_update(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if not raw_etag:
            return error_response(
                "precondition_required",
                "If-Match must contain the current media-edit revision.",
                428,
            )
        try:
            expected_revision = int(raw_etag)
            if expected_revision < 1:
                raise ValueError
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current media-edit revision.",
                428,
            )
        reservation_id = None
        try:
            payload = MediaEditUpdateRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            reservation_id, replay, key_error = _begin_detached_reservation(
                "updateMediaEdit",
                {
                    "session_id": session_id,
                    "expected_revision": expected_revision,
                    **payload.model_dump(mode="json"),
                },
            )
            if key_error is not None:
                return key_error
            if replay is not None:
                return replay
            result = media_edit.update(
                session_id,
                expected_revision,
                **payload.model_dump(mode="json"),
            )
            _finish_detached_reservation(
                reservation_id,
                result,
                200,
                resource_kind="media_edit_revision",
                resource_id=str((result.get("plan") or {}).get("revision_id") or "")
                or None,
            )
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["plan"]["revision"]}"'
            return response
        except ValidationError:
            _abandon_detached_reservation(reservation_id)
            raise
        except (
            IdempotencyConflict,
            IdempotencyInProgress,
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            _abandon_detached_reservation(reservation_id)
            if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
                return _idempotency_error(error)
            return service_error(error)

    @app.post("/api/v1/sessions/<session_id>/media-edit/propose")
    @require_scope("app.run")
    def media_edit_propose(session_id: str):
        try:
            payload = MediaEditProposeRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            key, key_error = _mutation_key()
            if key_error is not None:
                return key_error
            _active_revision(session_id, payload.revision)
            settings, settings_hash = _settings_snapshot(session_id, "correction")
            if payload.model is not None:
                settings["correction_model"] = payload.model
                settings_hash = stable_hash(settings)
            job_payload = {
                "session_id": session_id,
                "revision": payload.revision,
                "instructions": payload.instructions,
                "settings": settings,
                "settings_hash": settings_hash,
            }
            with media_edit.database.immediate_session() as db_session:
                reservation = None
                if key is not None:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="proposeMediaEdit",
                        idempotency_key=key,
                        payload=job_payload,
                    )
                    replay = _replay_response(reservation)
                    if replay is not None:
                        return replay
                job = _matching_job(
                    db_session,
                    kind="media_edit.propose",
                    session_id=session_id,
                    payload=job_payload,
                    statuses=("queued", "running", "cancel_requested"),
                ) or services.jobs.enqueue_in_session(
                    db_session,
                    "media_edit.propose",
                    job_payload,
                    session_id=session_id,
                    resource_keys=[f"session:{session_id}", "service:llm"],
                )
                result = _safe_job_payload(job)
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            return jsonify(result), 202
        except (
            IdempotencyConflict,
            IdempotencyInProgress,
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            ValueError,
        ) as error:
            if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
                return _idempotency_error(error)
            return service_error(error)

    @app.post("/api/v1/sessions/<session_id>/media-edit/render")
    @require_scope("app.run")
    def media_edit_render(session_id: str):
        try:
            payload = MediaEditRenderRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            key, key_error = _mutation_key()
            if key_error is not None:
                return key_error
            plan = _active_revision(session_id, payload.revision)
            if not bool(plan.get("reviewed")):
                raise ValueError(
                    "The media-edit revision must be reviewed before rendering."
                )
            settings, settings_hash = _settings_snapshot(session_id, "output")
            job_payload = {
                "session_id": session_id,
                "revision": payload.revision,
                "settings": settings,
                "settings_hash": settings_hash,
            }
            with media_edit.database.immediate_session() as db_session:
                reservation = None
                if key is not None:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="renderMediaEdit",
                        idempotency_key=key,
                        payload=job_payload,
                    )
                    replay = _replay_response(reservation)
                    if replay is not None:
                        return replay
                job = _matching_job(
                    db_session,
                    kind="media_edit.render",
                    session_id=session_id,
                    payload=job_payload,
                    statuses=("queued", "running"),
                ) or services.jobs.enqueue_in_session(
                    db_session,
                    "media_edit.render",
                    job_payload,
                    session_id=session_id,
                    resource_keys=[f"session:{session_id}", "service:ffmpeg"],
                )
                result = _safe_job_payload(job)
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            return jsonify(result), 202
        except (
            IdempotencyConflict,
            IdempotencyInProgress,
            KeyError,
            MediaEditInputsChanged,
            MediaEditRevisionConflict,
            ValueError,
        ) as error:
            if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
                return _idempotency_error(error)
            return service_error(error)
