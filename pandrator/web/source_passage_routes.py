"""Read-only passage status, explicit preview, and safe-branch rebuild."""

from __future__ import annotations

from flask import jsonify, request

from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .logical_passages import (
    PassageIneligibleSource,
    PassageRevisionConflict,
    passage_status,
    preview_source_passages,
    rebuild_source_passages_branch,
)
from .models import SessionRecord
from .route_context import RouteContext
from .schemas import SourcePassagePreviewRequest, SourcePassageRebuildRequest
from .workspace import RevisionConflict


def register_source_passage_routes(app: DomainBlueprints, context: RouteContext) -> None:
    """Register source-passage endpoints on the shared workflow Blueprint."""

    services = context.services
    database = services.database
    require_scope = context.guards.require_scope
    error_response = context.guards.error_response

    def idempotency_error(error: Exception):
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return error_response(
                error.code,
                str(error),
                409,
                {"retryable": error.retryable},
            )
        return error_response("idempotency_key_required", str(error), 400)

    @app.get("/api/v1/sessions/<session_id>/sources/<artifact_id>/passages")
    @require_scope("app.read")
    def source_passage_status(session_id: str, artifact_id: str):
        try:
            with database.session() as session:
                return jsonify(
                    passage_status(session, session_id, artifact_id)
                )
        except KeyError:
            return error_response("not_found", "Session or source not found.", 404)
        except PassageIneligibleSource as error:
            return error_response("ineligible_source", str(error), 422)

    @app.post(
        "/api/v1/sessions/<session_id>/sources/<artifact_id>/passages/preview"
    )
    @require_scope("app.read")
    def source_passage_preview(session_id: str, artifact_id: str):
        try:
            payload = SourcePassagePreviewRequest.model_validate(
                request.get_json(silent=True) or {}
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        try:
            with database.session() as session:
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                return jsonify(
                    preview_source_passages(
                        session,
                        session_id,
                        artifact_id,
                        override=dict(payload.source_passages or {}),
                    )
                )
        except KeyError:
            return error_response("not_found", "Session or source not found.", 404)
        except PassageIneligibleSource as error:
            return error_response("ineligible_source", str(error), 422)
        except (TypeError, ValueError) as error:
            return error_response("validation_error", str(error), 422)

    @app.post(
        "/api/v1/sessions/<session_id>/sources/<artifact_id>/passages/rebuild"
    )
    @require_scope("app.run")
    def source_passage_rebuild(session_id: str, artifact_id: str):
        try:
            payload = SourcePassageRebuildRequest.model_validate(
                request.get_json(silent=True) or {}
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        raw_key = str(request.headers.get("Idempotency-Key") or "").strip()
        body = payload.model_dump(mode="json")
        try:
            with database.immediate_session() as session:
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                reservation = None
                if raw_key:
                    try:
                        reservation = services.idempotency.begin(
                            session,
                            principal=context.guards.principal(),
                            operation_id="rebuildSourcePassages",
                            idempotency_key=raw_key,
                            payload={
                                "session_id": session_id,
                                "artifact_id": artifact_id,
                                **body,
                            },
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_error(error)
                    if reservation.response is not None:
                        replay_payload, replay_status = reservation.response
                        response = jsonify(replay_payload)
                        response.status_code = replay_status
                        response.headers["Idempotency-Replayed"] = "true"
                        return response

                def _write_artifact_file(relative_path: str, text: str) -> None:
                    destination = services.artifacts.paths.managed_path(
                        relative_path
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        destination.write_text(text, encoding="utf-8")

                result = rebuild_source_passages_branch(
                    session,
                    session_id,
                    artifact_id,
                    expected_source_revision_id=payload.expected_source_revision_id,
                    expected_source_content_hash=(
                        payload.expected_source_content_hash
                    ),
                    expected_settings_revision=payload.expected_settings_revision,
                    expected_settings_hash=payload.expected_settings_hash,
                    override=dict(payload.source_passages or {}),
                    write_artifact_file=_write_artifact_file,
                )
                if reservation is not None:
                    services.idempotency.complete(
                        session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="passage_branch",
                        resource_id=result["branch_artifact_id"],
                    )
            response = jsonify(result)
            response.status_code = 201
            return response
        except KeyError:
            return error_response("not_found", "Session or source not found.", 404)
        except PassageIneligibleSource as error:
            return error_response("ineligible_source", str(error), 422)
        except (PassageRevisionConflict, RevisionConflict) as error:
            return error_response("revision_conflict", str(error), 409)
        except (TypeError, ValueError) as error:
            return error_response("validation_error", str(error), 422)
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return idempotency_error(error)
