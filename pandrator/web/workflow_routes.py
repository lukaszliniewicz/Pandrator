"""Workflow history, selection, enqueue and export decision HTTP ownership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from flask import Request, Response
from sqlalchemy.orm import Session

from .database import Database
from .domain_blueprints import DomainBlueprints
from .models import Job
from .schemas import StageSelectionUpdate
from .sessions import SessionService
from .workflow_handlers import WorkflowHandlers
from .workflows import WorkflowService

RouteResponse = Response | tuple[Response, int]


class StageHistoryReader(Protocol):
    def __call__(
        self, session: Session, session_id: str, stage_key: str, *,
        limit: int = 50, before_version: int | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowRouteContext:
    database: Database
    sessions: SessionService
    workflows: WorkflowService
    workflow_handlers: WorkflowHandlers
    require_auth: Callable[[Callable[..., Any]], Callable[..., Any]]
    error_response: Callable[[str, str, int], RouteResponse]
    inline_credential_error: Callable[[dict[str, Any]], RouteResponse | None]
    jsonify: Callable[[Any], Response]
    request: Callable[[], Request]
    stage_history: StageHistoryReader
    trash_stage_artifact: Callable[[Session, str, str, str], dict[str, Any]]
    rerun_impact: Callable[[Session, str, str], dict[str, Any]]
    choose_artifact: Callable[[Session, str, str, str], dict[str, Any]]
    clear_selection: Callable[[Session, str, str], dict[str, Any]]
    selection_update_type: Callable[[], type[StageSelectionUpdate]]
    job_payload: Callable[[Job], dict[str, Any]]


def register_workflow_session_routes(
    app: DomainBlueprints, context: WorkflowRouteContext,
) -> None:
    database = context.database
    sessions = context.sessions
    workflows = context.workflows
    workflow_handlers = context.workflow_handlers
    require_auth = context.require_auth
    error_response = context.error_response
    inline_credential_error = context.inline_credential_error

    @app.get("/api/v1/sessions/<session_id>/workflow")
    @require_auth
    def workflow_get(session_id: str):
        try:
            return context.jsonify(workflows.snapshot(session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.get("/api/v1/sessions/<session_id>/stages/<stage_key>/artifacts")
    @require_auth
    def workflow_stage_artifacts(session_id: str, stage_key: str):
        try:
            sessions.get(session_id)
            with database.session() as db_session:
                return context.jsonify(
                    context.stage_history(
                        db_session,
                        session_id,
                        stage_key,
                        limit=context.request().args.get("limit", 50, type=int),
                        before_version=context.request().args.get(
                            "before_version",
                            type=int,
                        ),
                    )
                )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("stage_unavailable", str(error), 409)

    @app.delete(
        "/api/v1/sessions/<session_id>/stages/<stage_key>/artifacts/<artifact_id>"
    )
    @require_auth
    def workflow_stage_artifact_delete(
        session_id: str,
        stage_key: str,
        artifact_id: str,
    ):
        try:
            with database.session() as db_session:
                return context.jsonify(
                    context.trash_stage_artifact(
                        db_session,
                        session_id,
                        stage_key,
                        artifact_id,
                    )
                )
        except KeyError:
            return error_response("not_found", "Session or artifact not found.", 404)
        except ValueError as error:
            return error_response("artifact_in_use", str(error), 409)

    @app.get("/api/v1/sessions/<session_id>/stages/<stage_key>/impact")
    @require_auth
    def workflow_stage_impact(session_id: str, stage_key: str):
        try:
            sessions.get(session_id)
            with database.session() as db_session:
                return context.jsonify(context.rerun_impact(db_session, session_id, stage_key))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("stage_unavailable", str(error), 409)

    @app.get("/api/v1/sessions/<session_id>/stages/<stage_key>/settings-mismatches")
    @require_auth
    def workflow_stage_settings_mismatches(session_id: str, stage_key: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return context.jsonify(
            {"mismatches": workflow_handlers.settings_mismatches(session_id, stage_key)}
        )

    @app.put("/api/v1/sessions/<session_id>/stages/<stage_key>/selection")
    @require_auth
    def workflow_stage_selection(session_id: str, stage_key: str):
        payload = context.selection_update_type().model_validate(
            context.request().get_json(silent=True) or {}
        )
        raw_etag = context.request().headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current selection revision.",
                428,
            )
        try:
            with database.immediate_session() as db_session:
                history = context.stage_history(db_session, session_id, stage_key)
                if int(history["revision"]) != expected:
                    return error_response(
                        "revision_conflict",
                        "The selected stage artifact changed in another client.",
                        409,
                    )
                if payload.artifact_id:
                    result = context.choose_artifact(
                        db_session, session_id, stage_key, payload.artifact_id
                    )
                else:
                    result = context.clear_selection(db_session, session_id, stage_key)
            return context.jsonify(result)
        except KeyError:
            return error_response("not_found", "Session or artifact not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.post("/api/v1/sessions/<session_id>/stages/<stage_key>/run")
    @require_auth
    def workflow_run_stage(session_id: str, stage_key: str):
        settings = context.request().get_json(silent=True) or {}
        if not isinstance(settings, dict):
            return error_response(
                "validation_error", "Stage settings must be an object.", 422
            )
        if rejected := inline_credential_error(settings):
            return rejected
        try:
            job = workflows.run_stage(session_id, stage_key, settings)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("stage_unavailable", str(error), 409)
        return context.jsonify(context.job_payload(job)), 202



def register_workflow_job_routes(
    app: DomainBlueprints, context: WorkflowRouteContext,
) -> None:
    workflows = context.workflows
    require_auth = context.require_auth
    error_response = context.error_response

    @app.post("/api/v1/jobs/<job_id>/video-tail-decision")
    @require_auth
    def job_video_tail_decision(job_id: str):
        body = context.request().get_json(silent=True)
        if not isinstance(body, dict) or body.get("action") not in ("stop", "extend"):
            return error_response("validation_error", "Choose stop or extend.", 422)
        try:
            job = workflows.decide_video_tail(job_id, body["action"])
        except KeyError:
            return error_response("not_found", "Job not found.", 404)
        except ValueError as error:
            return error_response("export_conflict", str(error), 409)
        return context.jsonify(context.job_payload(job)), 200 if body["action"] == "stop" else 202
