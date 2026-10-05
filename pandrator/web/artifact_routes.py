"""Artifact listing, review, lineage and browser media HTTP routes."""

from __future__ import annotations

import json
import logging
import threading

from flask import jsonify, request, send_file
from sqlalchemy import select

from .domain_blueprints import DomainBlueprints
from .http_serialization import model_payload as _model_dict
from .models import Artifact, ArtifactEdge, Job, OutputAssembly, UsageEvent, new_id
from .route_context import RouteContext
from .schemas import OptimizationReviewRequest
from .video_previews import VideoPreviewService, VideoPreviewUnsupported


def register_artifact_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    paths = services.paths
    database = services.database
    artifacts = services.artifacts
    jobs = services.jobs
    workflow_handlers = services.workflow_handlers
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth

    @app.get("/api/v1/artifacts")
    @require_auth
    def artifact_list():
        with database.session() as db_session:
            output_only = request.args.get("output_only") == "true"
            statement = select(Artifact)
            if output_only:
                statement = statement.where(
                    (Artifact.role == "export")
                    | Artifact.role.startswith("export_")
                    | Artifact.role.in_(
                        (
                            "assembled_audio",
                            "audiobook_audio",
                            "dubbing_audio",
                            "output_assembly",
                            "rvc_audio",
                        )
                    )
                ).order_by(Artifact.updated_at.desc(), Artifact.created_at.desc())
            else:
                statement = statement.order_by(Artifact.created_at.desc())
            if request.args.get("include_deleted") != "true":
                statement = statement.where(Artifact.state != "deleted")
            requested_session = str(request.args.get("session_id") or "")
            if requested_session:
                statement = statement.where(Artifact.session_id == requested_session)
            try:
                limit = max(1, min(500, int(request.args.get("limit") or 500)))
            except ValueError:
                limit = 500
            records = list(db_session.scalars(statement.limit(limit)).all())
            items = []
            for item in records:
                serialized = _model_dict(
                    item,
                    (
                        "id",
                        "session_id",
                        "kind",
                        "role",
                        "relative_path",
                        "mime_type",
                        "size_bytes",
                        "content_hash",
                        "state",
                        "metadata_json",
                        "created_at",
                    ),
                )
                # The owner UI may copy the server-local path for completed
                # outputs. The relative managed key remains the durable API
                # identifier; this presentation field is intentionally
                # read-only and never accepted back as an input path.
                serialized["path"] = str(paths.managed_path(item.relative_path).resolve())
                items.append(serialized)
            return jsonify({"items": items})

    @app.delete("/api/v1/sessions/<session_id>/outputs/<artifact_id>")
    @require_auth
    def output_artifact_delete(session_id: str, artifact_id: str):
        try:
            result = artifacts.remove_output(session_id, artifact_id)
        except KeyError:
            return error_response("not_found", "Output not found.", 404)
        except ValueError as error:
            return error_response("invalid_artifact", str(error), 409)
        except OSError as error:
            return error_response(
                "artifact_delete_failed",
                f"The output file could not be removed: {error}",
                409,
            )
        return jsonify(result)

    @app.post("/api/v1/artifacts/<artifact_id>/optimization-review")
    @require_auth
    def artifact_optimization_review(artifact_id: str):
        payload = OptimizationReviewRequest.model_validate(request.get_json(silent=True) or {})
        try:
            source, source_path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Speech-optimized artifact not found.", 404)
        if source.role != "tts_optimized" or source_path.suffix.lower() != ".json":
            return error_response(
                "validation_error",
                "Only JSON speech-optimization artifacts use this review endpoint.",
                422,
            )
        try:
            rows = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            return error_response(
                "artifact_invalid",
                f"The optimization artifact cannot be reviewed: {error}",
                422,
            )
        if not isinstance(rows, list):
            return error_response(
                "artifact_invalid",
                "The optimization artifact must contain a list.",
                422,
            )
        edits = {item.index: item.text.strip() for item in payload.items}
        if len(payload.items) != len(rows) or set(edits) != set(range(len(rows))):
            return error_response(
                "validation_error",
                "Reviewed text must preserve every item index exactly once.",
                422,
            )
        if any(not text for text in edits.values()):
            return error_response(
                "validation_error",
                "Reviewed text cannot be empty.",
                422,
            )
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return error_response(
                    "artifact_invalid",
                    "Every optimization item must be an object.",
                    422,
                )
            row["source_text"] = str(
                row.get("source_text") or row.get("original_sentence") or row.get("text") or ""
            )
            row["text"] = edits[index]
            row["processed_sentence"] = edits[index]
            row["tts_optimized_sentence"] = edits[index]
            row["optimization_reviewed"] = True
        destination = source_path.parent / f"tts-optimized-reviewed-{new_id()}.json"
        owns_destination = False
        try:
            with destination.open("x", encoding="utf-8") as stream:
                owns_destination = True
                stream.write(json.dumps(rows, ensure_ascii=False, indent=2))
            reviewed = artifacts.register(
                destination,
                kind="json",
                role="tts_optimized",
                session_id=source.session_id,
                parent_ids=[source.id],
                metadata={
                    **(source.metadata_json or {}),
                    "reviewed": True,
                    "reviewed_from": source.id,
                },
            )
        except Exception:
            if owns_destination:
                try:
                    # Commit hooks can fail after persistence. Keep any registered file.
                    with database.session() as db_session:
                        registered = db_session.scalar(
                            select(Artifact.id).where(
                                Artifact.relative_path == paths.relative_managed_path(destination)
                            )
                        )
                    if registered is None:
                        destination.unlink(missing_ok=True)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not clean up the failed optimization review."
                    )
            raise
        return jsonify(
            _model_dict(
                reviewed,
                (
                    "id",
                    "session_id",
                    "kind",
                    "role",
                    "relative_path",
                    "mime_type",
                    "size_bytes",
                    "content_hash",
                    "state",
                    "metadata_json",
                    "created_at",
                ),
            )
        ), 201

    @app.get("/api/v1/artifacts/<artifact_id>/context")
    @require_auth
    def artifact_context(artifact_id: str):
        """Return lightweight lineage metadata used by review and comparison UIs."""
        fields = (
            "id",
            "session_id",
            "kind",
            "role",
            "relative_path",
            "mime_type",
            "size_bytes",
            "content_hash",
            "state",
            "metadata_json",
            "created_at",
        )
        with database.session() as db_session:
            artifact = db_session.get(Artifact, artifact_id)
            if artifact is None:
                return error_response("not_found", "Artifact not found.", 404)
            parent_ids = list(
                db_session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == artifact_id
                    )
                ).all()
            )
            parents = (
                list(db_session.scalars(select(Artifact).where(Artifact.id.in_(parent_ids))).all())
                if parent_ids
                else []
            )
            parents.sort(key=lambda item: (item.role != "extracted_text", item.created_at))
            child_ids = list(
                db_session.scalars(
                    select(ArtifactEdge.child_artifact_id).where(
                        ArtifactEdge.parent_artifact_id == artifact_id
                    )
                ).all()
            )
            children = (
                list(db_session.scalars(select(Artifact).where(Artifact.id.in_(child_ids))).all())
                if child_ids
                else []
            )
            children.sort(key=lambda item: (item.created_at, item.id))
            usage_artifact_ids = {artifact.id, *parent_ids}
            events = list(
                db_session.scalars(
                    select(UsageEvent).where(UsageEvent.artifact_id.in_(usage_artifact_ids))
                ).all()
            )
            generation_run_id = str((artifact.metadata_json or {}).get("generation_run_id") or "")
            output_assembly_id = str((artifact.metadata_json or {}).get("output_assembly_id") or "")
            if output_assembly_id:
                assembly = db_session.get(OutputAssembly, output_assembly_id)
                generation_run_id = (
                    str(assembly.generation_run_id or "")
                    if assembly is not None
                    else generation_run_id
                )
            if generation_run_id:
                events.extend(
                    db_session.scalars(
                        select(UsageEvent).where(UsageEvent.generation_run_id == generation_run_id)
                    ).all()
                )
            from .usage import usage_summary

            return jsonify(
                {
                    "artifact": _model_dict(artifact, fields),
                    "parents": [_model_dict(item, fields) for item in parents],
                    "children": [_model_dict(item, fields) for item in children],
                    "usage": usage_summary(events),
                }
            )

    @app.get("/api/v1/artifacts/<artifact_id>/content")
    @require_auth
    def artifact_content(artifact_id: str):
        try:
            artifact, path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Artifact not found.", 404)
        if not path.is_file():
            return error_response("artifact_missing", "The artifact file is missing.", 410)
        return send_file(
            path,
            mimetype=artifact.mime_type,
            conditional=True,
            etag=artifact.content_hash if artifact.content_hash is not None else False,
        )

    @app.get("/api/v1/artifacts/<artifact_id>/video-preview")
    @require_auth
    def artifact_video_preview(artifact_id: str):
        try:
            result, status = VideoPreviewService(database, paths, artifacts, jobs).request(
                artifact_id
            )
            return jsonify(result), status
        except KeyError:
            return error_response("not_found", "Artifact not found.", 404)
        except FileNotFoundError:
            return error_response("artifact_missing", "The artifact file is missing.", 410)
        except VideoPreviewUnsupported as error:
            return error_response("video_preview_unsupported", str(error), 422)
        except ValueError as error:
            return error_response("video_preview_conflict", str(error), 409)

    @app.get("/api/v1/artifacts/<artifact_id>/audio-preview")
    @require_auth
    def artifact_audio_preview(artifact_id: str):
        """Return a browser-safe source preview or queue its derivation."""
        with database.immediate_session() as db_session:
            source = db_session.get(Artifact, artifact_id)
            if source is None or source.state == "deleted":
                return error_response("not_found", "Artifact not found.", 404)
            source_path = paths.managed_path(source.relative_path)
            if not source_path.is_file():
                return error_response("artifact_missing", "The artifact file is missing.", 410)

            preview = db_session.scalar(
                select(Artifact)
                .join(ArtifactEdge, ArtifactEdge.child_artifact_id == Artifact.id)
                .where(
                    ArtifactEdge.parent_artifact_id == source.id,
                    Artifact.role == "source_audio_preview",
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            )
            if preview is not None:
                preview_path = paths.managed_path(preview.relative_path)
                if preview_path.is_file():
                    return jsonify(
                        {
                            "status": "ready",
                            "artifact_id": preview.id,
                            "content_url": f"/api/v1/artifacts/{preview.id}/content",
                            "mime_type": preview.mime_type or "audio/mpeg",
                        }
                    )

            active_job = None
            for candidate in db_session.scalars(
                select(Job)
                .where(
                    Job.kind == "audio.preview",
                    Job.status.in_(("queued", "running")),
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
            ).all():
                payload = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
                if str(payload.get("source_artifact_id") or "") == source.id:
                    active_job = candidate
                    break
            if active_job is None:
                active_job = jobs.enqueue_in_session(
                    db_session,
                    "audio.preview",
                    {"source_artifact_id": source.id},
                    session_id=None,
                    resource_keys=[f"artifact:audio-preview:{source.id}"],
                )
            return jsonify({"status": active_job.status, "job_id": active_job.id}), 202

    @app.get("/api/v1/artifacts/<artifact_id>/waveform")
    @require_auth
    def artifact_waveform(artifact_id: str):
        try:
            source, _path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Audio artifact not found.", 404)
        try:
            points = int(request.args.get("points") or 1600)
            if not 128 <= points <= 5000:
                raise ValueError
            raw_start = request.args.get("start_ms")
            raw_end = request.args.get("end_ms")
            bounded = raw_start is not None or raw_end is not None
            start_ms = int(raw_start or 0)
            end_ms = int(raw_end) if raw_end is not None else None
            if bounded and (
                start_ms < 0
                or end_ms is None
                or end_ms <= start_ms
                or end_ms - start_ms > 10 * 60 * 1000
            ):
                raise ValueError
        except (TypeError, ValueError):
            return error_response(
                "validation_error",
                "Waveform points must be 128–5000; bounded windows must be positive and at most 10 minutes.",
                422,
            )
        role = "waveform_peaks_window" if bounded else "waveform_peaks"

        def cached_peak_id() -> str | None:
            with database.session() as db_session:
                peak_candidates = list(
                    db_session.scalars(
                        select(Artifact)
                        .join(
                            ArtifactEdge,
                            ArtifactEdge.child_artifact_id == Artifact.id,
                        )
                        .where(
                            ArtifactEdge.parent_artifact_id == artifact_id,
                            Artifact.role == role,
                            Artifact.state == "current",
                        )
                        .order_by(Artifact.created_at.desc())
                    ).all()
                )
                peak_artifact = next(
                    (
                        candidate
                        for candidate in peak_candidates
                        if int((candidate.metadata_json or {}).get("max_points") or 0) == points
                        and int((candidate.metadata_json or {}).get("start_ms") or 0) == start_ms
                        and (
                            not bounded
                            or int((candidate.metadata_json or {}).get("end_ms") or 0) == end_ms
                        )
                    ),
                    None,
                )
                return peak_artifact.id if peak_artifact is not None else None

        def cached_response(peak_id: str | None):
            if not peak_id:
                return None
            _artifact, peak_path = artifacts.resolve(peak_id)
            return send_file(
                peak_path,
                mimetype="application/json",
                conditional=True,
                etag=_artifact.content_hash if _artifact.content_hash is not None else False,
            )

        cached = cached_response(cached_peak_id())
        if cached is not None:
            return cached
        job_payload = {
            "source_artifact_id": artifact_id,
            "max_points": points,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }

        # Short detail windows are requested interactively while scrubbing. Do
        # the same generation and registration work as the durable handler
        # inline so a small cache miss is not queued behind unrelated ASR jobs.
        if bounded and end_ms is not None and end_ms - start_ms <= 120_000:
            try:
                workflow_handlers.generate_waveform(
                    job_payload,
                    lambda *_args: None,
                    threading.Event(),
                )
            except (OSError, RuntimeError, ValueError):
                # The durable queue remains the fallback for media/runtime
                # failures (including unavailable FFmpeg/FFprobe).
                pass
            else:
                cached = cached_response(cached_peak_id())
                if cached is not None:
                    return cached

        with database.immediate_session() as db_session:
            active_job = next(
                (
                    candidate
                    for candidate in db_session.scalars(
                        select(Job)
                        .where(
                            Job.kind == "audio.waveform",
                            Job.status.in_(("queued", "running")),
                        )
                        .order_by(Job.created_at.asc(), Job.id.asc())
                    ).all()
                    if all(
                        (candidate.payload_json or {}).get(key) == value
                        for key, value in job_payload.items()
                    )
                ),
                None,
            )
            job = active_job or jobs.enqueue_in_session(
                db_session,
                "audio.waveform",
                job_payload,
                session_id=source.session_id,
                resource_keys=[f"session:{source.session_id}"]
                if source.session_id
                else [f"artifact:waveform:{source.id}"],
            )
        return jsonify({"status": "queued", "job_id": job.id}), 202
