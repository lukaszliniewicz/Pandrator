"""Subtitle-first source readiness and explicit authoritative-text alignment."""

from flask import jsonify, request

from .models import Artifact, SessionRecord
from .schemas import SubtitleAlignRequest
from .subtitle_sources import (
    adopt_subtitle_source_in_session,
    subtitle_source_status_in_session,
)
from .workspace import RevisionConflict


def register_subtitle_source_routes(app, services, require_auth, error_response):
    @app.get("/api/v1/sessions/<session_id>/sources/subtitle-status")
    @require_auth
    def session_subtitle_source_status(session_id: str):
        try:
            with services.database.session() as session:
                return jsonify(subtitle_source_status_in_session(session, session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.post("/api/v1/sessions/<session_id>/sources/align-subtitles")
    @require_auth
    def session_align_subtitle_source(session_id: str):
        payload = SubtitleAlignRequest.model_validate(request.get_json(silent=True) or {})
        try:
            resolved, _settings_hash = services.workspace_settings.resolve(
                session_id, sections=["stt", "subtitles"]
            )
            settings = {
                **resolved.get("subtitles", {}), **resolved.get("stt", {}),
                "caption_alignment_method": payload.method,
            }
            with services.database.immediate_session() as session:
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                if record.revision != payload.expected_revision:
                    raise RevisionConflict("The source or media changed; refresh before aligning.")
                status = subtitle_source_status_in_session(session, session_id)
                if record.workflow_kind not in {"subtitles", "voiceover"} or not status["can_align"]:
                    raise ValueError("Attach an original audio/video recording to an SRT/VTT subtitle source before aligning.")
                imported = adopt_subtitle_source_in_session(
                    session, services.artifacts, session_id, status["source_artifact_id"]
                )
                media = session.get(Artifact, status["media_artifact_id"])
                caption = session.get(Artifact, imported["artifact_id"])
                settings["original_language"] = record.source_language or "auto"
                job = services.jobs.enqueue_in_session(session, "dubbing.transcribe", {
                    "session_id": session_id,
                    "source_artifact_id": media.id,
                    "caption_artifact_id": caption.id,
                    "source_content_hash": media.content_hash,
                    "caption_content_hash": caption.content_hash,
                    "settings": settings,
                }, session_id=session_id, resource_keys=services.workflows._resource_keys(session_id, "transcribe", settings))
                # A retry with the old revision must not enqueue a duplicate job.
                record.revision += 1
                session.flush()
                result = {
                    "id": job.id, "job_id": job.id, "status": job.status,
                    "session_id": session_id, "session_revision": record.revision,
                    "source_artifact_id": media.id, "caption_artifact_id": caption.id,
                }
            return jsonify(result), 202
        except KeyError:
            return error_response("not_found", "Session or subtitle source not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
