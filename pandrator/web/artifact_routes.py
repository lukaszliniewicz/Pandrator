"""Artifact listing, review, lineage and browser media HTTP routes."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import PurePosixPath

from flask import jsonify, request, send_file
from sqlalchemy import false, func, or_, select

from .access.artifact_policy import (
    ArtifactNotFound,
    require_artifact_access,
    require_artifact_content_access,
    visible_artifact_ids_statement,
    visible_usage_event_ids_statement,
)
from .access.contracts import AccessContext, AccessDenied, ProjectAction
from .access.job_policy import job_visibility_clause
from .access.session_policy import SessionNotFound, require_session_access
from .accounts.actor_authorization import ActorAuthorizationDenied
from .accounts.execution_actor import execution_actor_scope
from .artifact_commands import can_run_artifact, require_artifact_run_access
from .domain_blueprints import DomainBlueprints
from .http_serialization import model_payload as _model_dict
from .job_inputs import artifact_job_ownership
from .models import (
    Artifact,
    ArtifactEdge,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
    UsageEvent,
    new_id,
)
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
        view = request.args.get("view", "full")
        media_type = request.args.get("media_type")
        if view not in {"full", "compact", "summary"} or media_type not in {None, "audio", "text"}:
            return error_response("validation_error", "Invalid artifact view or media type.", 400)
        try:
            offset = int(request.args.get("offset", 0))
            if offset < 0:
                raise ValueError
        except ValueError:
            return error_response("validation_error", "offset must be a nonnegative integer.", 400)
        access = context.guards.access_context()
        with database.snapshot_session() as db_session:
            output_only = request.args.get("output_only") == "true"
            visible = visible_artifact_ids_statement(db_session, access).correlate(None)
            statement = select(Artifact).where(Artifact.id.in_(visible))
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
            for field in ("role", "kind"):
                value = request.args.get(field)
                if value:
                    statement = statement.where(getattr(Artifact, field) == value)
            if media_type:
                kinds = (
                    (
                        "wav",
                        "mp3",
                        "flac",
                        "ogg",
                        "m4a",
                        "aac",
                        "opus",
                        "wma",
                        "aiff",
                        "aif",
                        "pcm",
                        "au",
                    )
                    if media_type == "audio"
                    else (
                        "txt",
                        "text",
                        "srt",
                        "vtt",
                        "json",
                        "md",
                        "html",
                        "htm",
                        "pdf",
                        "epub",
                        "docx",
                    )
                )
                statement = statement.where(
                    or_(
                        func.lower(Artifact.mime_type).startswith(f"{media_type}/"),
                        func.ltrim(func.lower(Artifact.kind), ".").in_(kinds),
                    )
                )
            try:
                limit = max(1, min(500, int(request.args.get("limit") or 500)))
            except ValueError:
                limit = 500
            if view == "compact":
                total = int(
                    db_session.scalar(
                        select(func.count()).select_from(
                            statement.with_only_columns(Artifact.id, maintain_column_froms=True)
                            .order_by(None)
                            .subquery()
                        )
                    )
                    or 0
                )
                compact_fields = (
                    "id",
                    "session_id",
                    "kind",
                    "role",
                    "relative_path",
                    "mime_type",
                    "size_bytes",
                    "state",
                    "created_at",
                )
                page = statement.with_only_columns(
                    *(getattr(Artifact, key) for key in compact_fields),
                    SessionRecord.name.label("session_name"),
                    maintain_column_froms=True,
                ).outerjoin(SessionRecord, SessionRecord.id == Artifact.session_id)
                rows = (
                    db_session.execute(page.order_by(Artifact.id).limit(limit).offset(offset))
                    .mappings()
                    .all()
                )
                ids = [row["id"] for row in rows]
                take_labels = {}
                if ids and media_type != "text":
                    for row in db_session.execute(
                        select(
                            AudioTake.artifact_id,
                            GenerationSegment.ordinal,
                            func.substr(GenerationSegment.text, 1, 120).label("segment_text"),
                            GenerationSegment.speaker,
                        )
                        .join(
                            GenerationSegment,
                            GenerationSegment.id == AudioTake.generation_segment_id,
                        )
                        .join(Artifact, Artifact.id == AudioTake.artifact_id)
                        .join(
                            GenerationPlanRevision,
                            GenerationPlanRevision.id == GenerationSegment.plan_revision_id,
                        )
                        .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
                        .where(
                            AudioTake.artifact_id.in_(ids),
                            GenerationPlan.session_id == Artifact.session_id,
                        )
                        .order_by(GenerationSegment.ordinal, GenerationSegment.id, AudioTake.id)
                    ).mappings():
                        take_labels.setdefault(
                            row["artifact_id"],
                            {
                                "segment_ordinal": row["ordinal"],
                                "segment_text": row["segment_text"],
                                "speaker": row["speaker"],
                            },
                        )
                items = []
                for row in rows:
                    item = dict(row)
                    item["created_at"] = row["created_at"].isoformat()
                    item["display_name"] = PurePosixPath(
                        item.pop("relative_path").replace("\\", "/")
                    ).name
                    item.update(take_labels.get(row["id"], {}))
                    items.append(item)
                return jsonify(
                    {
                        "items": items,
                        "total": total,
                        "offset": offset,
                        "next_offset": offset + limit if offset + limit < total else None,
                    }
                )
            records = list(db_session.scalars(statement.limit(limit).offset(offset)).all())
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
                if view == "summary":
                    metadata = serialized.get("metadata_json") or {}
                    serialized["metadata_json"] = {
                        key: metadata[key] for key in
                        ("language", "subtitle_diagnostics", "subtitle_tracks")
                        if key in metadata
                    }
                    serialized.pop("relative_path", None)
                    items.append(serialized)
                    continue
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
        except AccessDenied:
            raise
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
        access = context.guards.access_context()
        try:
            with database.snapshot_session() as db_session:
                source = require_artifact_access(db_session, access, artifact_id)
                if source.state == "deleted" or source.session_id is None:
                    raise ArtifactNotFound()
                require_session_access(
                    db_session,
                    access,
                    source.session_id,
                    ProjectAction.EDIT,
                )
        except (ArtifactNotFound, SessionNotFound):
            return error_response("not_found", "Speech-optimized artifact not found.", 404)
        source_path = paths.managed_path(source.relative_path)
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

        def cleanup_owned_destination() -> None:
            if not owns_destination:
                return
            try:
                # A failed commit may have persisted the artifact. Keep its file.
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

        try:
            with destination.open("x", encoding="utf-8") as stream:
                owns_destination = True
                stream.write(json.dumps(rows, ensure_ascii=False, indent=2))
            prepared = artifacts.prepare_registration(destination)
            with database.immediate_session() as db_session:
                current_source = require_artifact_access(
                    db_session,
                    access,
                    source.id,
                    expected_session_id=source.session_id,
                )
                if (
                    current_source.state == "deleted"
                    or current_source.session_id != source.session_id
                    or current_source.role != "tts_optimized"
                ):
                    raise ArtifactNotFound()
                require_session_access(
                    db_session,
                    access,
                    source.session_id,
                    ProjectAction.EDIT,
                )
                reviewed = artifacts.register_in_session(
                    db_session,
                    destination,
                    kind="json",
                    role="tts_optimized",
                    session_id=source.session_id,
                    parent_ids=[source.id],
                    metadata={
                        **(current_source.metadata_json or {}),
                        "reviewed": True,
                        "reviewed_from": source.id,
                    },
                    _prepared=prepared,
                )
        except (ArtifactNotFound, SessionNotFound):
            cleanup_owned_destination()
            return error_response("not_found", "Speech-optimized artifact not found.", 404)
        except Exception:
            cleanup_owned_destination()
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
        access = context.guards.access_context()
        with database.snapshot_session() as db_session:
            try:
                artifact = require_artifact_access(db_session, access, artifact_id)
            except ArtifactNotFound:
                return error_response("not_found", "Artifact not found.", 404)
            visible = visible_artifact_ids_statement(db_session, access).correlate(None)
            parent_ids = list(
                db_session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == artifact_id,
                        ArtifactEdge.parent_artifact_id.in_(visible),
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
                        ArtifactEdge.parent_artifact_id == artifact_id,
                        ArtifactEdge.child_artifact_id.in_(visible),
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
            metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
            generation_run_id = str(metadata.get("generation_run_id") or "")
            output_assembly_id = str(metadata.get("output_assembly_id") or "")
            if output_assembly_id and artifact.session_id is not None:
                assembly = db_session.execute(
                    select(OutputAssembly.generation_run_id).where(
                        OutputAssembly.id == output_assembly_id,
                        OutputAssembly.session_id == artifact.session_id,
                        OutputAssembly.artifact_id == artifact.id,
                    )
                ).one_or_none()
                if assembly is not None:
                    generation_run_id = str(assembly.generation_run_id or "")
            valid_run_id = (
                db_session.scalar(
                    select(GenerationRun.id).where(
                        GenerationRun.id == generation_run_id,
                        GenerationRun.session_id == artifact.session_id,
                    )
                )
                if generation_run_id and artifact.session_id is not None
                else None
            )
            usage = select(UsageEvent).where(
                UsageEvent.id.in_(
                    visible_usage_event_ids_statement(db_session, access).correlate(None)
                ),
                or_(
                    UsageEvent.artifact_id.in_(usage_artifact_ids),
                    UsageEvent.generation_run_id == valid_run_id
                    if valid_run_id is not None
                    else false(),
                ),
            )
            events = list(db_session.scalars(usage).all())
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
        access = context.guards.access_context()
        try:
            with database.snapshot_session() as db_session:
                artifact = require_artifact_content_access(db_session, access, artifact_id)
                if artifact.state == "deleted":
                    raise ArtifactNotFound()
                path = paths.managed_path(artifact.relative_path)
                if not path.is_file():
                    return error_response("artifact_missing", "The artifact file is missing.", 410)
                return send_file(
                    path,
                    mimetype=artifact.mime_type,
                    conditional=True,
                    etag=artifact.content_hash if artifact.content_hash is not None else False,
                )
        except ArtifactNotFound:
            return error_response("not_found", "Artifact not found.", 404)

    def preparation_required(db_session, access: AccessContext, artifact_id: str):
        return error_response(
            "preview_preparation_required", "This preview has not been prepared.", 409,
            {"can_prepare": can_run_artifact(db_session, access, artifact_id),
             "required_scopes": ["app.read", "app.run"]},
        )

    @app.get("/api/v1/artifacts/<artifact_id>/video-preview")
    @app.post("/api/v1/artifacts/<artifact_id>/video-preview")
    @require_auth
    def artifact_video_preview(artifact_id: str):
        try:
            result, status = VideoPreviewService(database, paths, artifacts, jobs).request_for_actor(
                artifact_id, context.guards.access_context(), prepare=request.method == "POST",
            )
            if status == 409:
                return error_response(
                    "preview_preparation_required", "This preview has not been prepared.", 409,
                    {"can_prepare": result["can_prepare"],
                     "required_scopes": ["app.read", "app.run"]},
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

    def readable_preview_source(db_session, access: AccessContext, artifact_id: str):
        source = require_artifact_access(db_session, access, artifact_id)
        if source.state == "deleted":
            raise ArtifactNotFound()
        return source

    @app.get("/api/v1/artifacts/<artifact_id>/audio-preview")
    @app.post("/api/v1/artifacts/<artifact_id>/audio-preview")
    @require_auth
    def artifact_audio_preview(artifact_id: str):
        """Read a preview, or explicitly prepare one through POST."""
        try:
            return audio_preview_response(artifact_id, context.guards.access_context())
        except ArtifactNotFound:
            return error_response("not_found", "Artifact not found.", 404)

    def audio_preview_response(artifact_id: str, access: AccessContext):
        prepare = request.method == "POST"
        with database.immediate_session() as db_session:
            source = (
                require_artifact_run_access(db_session, access, artifact_id) if prepare
                else readable_preview_source(db_session, access, artifact_id)
            )
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
                    Artifact.workspace_id == source.workspace_id,
                    Artifact.project_id == source.project_id,
                    Artifact.session_id == source.session_id,
                    Artifact.id.in_(
                        visible_artifact_ids_statement(db_session, access).correlate(None)
                    ),
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            )
            if preview is not None:
                preview_path = paths.managed_path(preview.relative_path)
                if preview_path.is_file():
                    return jsonify({
                        "status": "ready", "artifact_id": preview.id,
                        "content_url": f"/api/v1/artifacts/{preview.id}/content",
                        "mime_type": preview.mime_type or "audio/mpeg",
                    })
            owner = artifact_job_ownership(db_session, [source.id])
            active_job = None
            for candidate in db_session.scalars(
                select(Job).where(
                    Job.kind == "audio.preview",
                    Job.status.in_(("queued", "running")),
                    Job.workspace_id == owner.workspace_id,
                    Job.project_id == owner.project_id,
                    job_visibility_clause(db_session, access),
                ).order_by(Job.created_at.asc(), Job.id.asc())
            ).all():
                payload = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
                if str(payload.get("source_artifact_id") or "") == source.id:
                    active_job = candidate
                    break
            if active_job is None:
                if not prepare:
                    return preparation_required(db_session, access, source.id)
                require_artifact_run_access(db_session, access, source.id)
                with execution_actor_scope(access.actor):
                    active_job = jobs.enqueue_in_session(
                        db_session, "audio.preview", {"source_artifact_id": source.id},
                        session_id=None, workspace_id=owner.workspace_id,
                        project_id=owner.project_id,
                        resource_keys=[f"artifact:audio-preview:{source.id}"],
                    )
            return jsonify({"status": active_job.status, "job_id": active_job.id}), 202

    @app.get("/api/v1/artifacts/<artifact_id>/waveform")
    @app.post("/api/v1/artifacts/<artifact_id>/waveform")
    @require_auth
    def artifact_waveform(artifact_id: str):
        try:
            return waveform_response(artifact_id, context.guards.access_context())
        except ArtifactNotFound:
            return error_response("not_found", "Audio artifact not found.", 404)

    def waveform_response(artifact_id: str, access: AccessContext):
        prepare = request.method == "POST"
        with database.snapshot_session() as db_session:
            if prepare:
                require_artifact_run_access(db_session, access, artifact_id)
            else:
                readable_preview_source(db_session, access, artifact_id)
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
                start_ms < 0 or end_ms is None or end_ms <= start_ms
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
        job_payload = {
            "source_artifact_id": artifact_id, "max_points": points,
            "start_ms": start_ms, "end_ms": end_ms,
        }

        def cached_peak_id() -> str | None:
            with database.snapshot_session() as db_session:
                source = readable_preview_source(db_session, access, artifact_id)
                candidates = db_session.scalars(
                    select(Artifact).join(
                        ArtifactEdge, ArtifactEdge.child_artifact_id == Artifact.id,
                    ).where(
                        ArtifactEdge.parent_artifact_id == artifact_id,
                        Artifact.role == role, Artifact.state == "current",
                        Artifact.workspace_id == source.workspace_id,
                        Artifact.project_id == source.project_id,
                        Artifact.session_id == source.session_id,
                        Artifact.id.in_(
                            visible_artifact_ids_statement(db_session, access).correlate(None)
                        ),
                    ).order_by(Artifact.created_at.desc())
                ).all()
                for candidate in candidates:
                    metadata = candidate.metadata_json
                    if not isinstance(metadata, dict):
                        continue
                    try:
                        matches = (
                            int(metadata.get("max_points") or 0) == points
                            and int(metadata.get("start_ms") or 0) == start_ms
                            and (not bounded or int(metadata.get("end_ms") or 0) == end_ms)
                        )
                    except (TypeError, ValueError):
                        continue
                    if matches:
                        return candidate.id
                return None

        def cached_response(peak_id: str | None):
            if not peak_id:
                return None
            with database.snapshot_session() as db_session:
                source = readable_preview_source(db_session, access, artifact_id)
                try:
                    peak = require_artifact_access(db_session, access, peak_id)
                except ArtifactNotFound:
                    return None
                if (peak.state != "current" or peak.workspace_id != source.workspace_id
                        or peak.project_id != source.project_id
                        or peak.session_id != source.session_id):
                    return None
                path = paths.managed_path(peak.relative_path)
                if not path.is_file():
                    return None
                return send_file(
                    path, mimetype="application/json", conditional=True,
                    etag=peak.content_hash if peak.content_hash is not None else False,
                )

        def active_waveform_job(db_session, source):
            candidates = db_session.scalars(
                select(Job).where(
                    Job.kind == "audio.waveform", Job.status.in_(("queued", "running")),
                    Job.workspace_id == source.workspace_id,
                    Job.project_id == source.project_id,
                    job_visibility_clause(db_session, access),
                ).order_by(Job.created_at.asc(), Job.id.asc())
            ).all()
            return next((candidate for candidate in candidates
                         if isinstance(candidate.payload_json, dict)
                         and all(candidate.payload_json.get(key) == value
                                 for key, value in job_payload.items())), None)

        cached = cached_response(cached_peak_id())
        if cached is not None:
            return cached
        if not prepare:
            with database.snapshot_session() as db_session:
                source = readable_preview_source(db_session, access, artifact_id)
                active = active_waveform_job(db_session, source)
                if active is not None:
                    return jsonify({"status": active.status, "job_id": active.id}), 202
                return preparation_required(db_session, access, source.id)
        # Close the admission writer before bounded synchronous media processing.
        if bounded and end_ms is not None and end_ms - start_ms <= 120_000:
            with database.immediate_session() as db_session:
                source = readable_preview_source(db_session, access, artifact_id)
                active = active_waveform_job(db_session, source)
                if active is not None:
                    return jsonify({"status": active.status, "job_id": active.id}), 202
                require_artifact_run_access(db_session, access, artifact_id)
            try:
                with execution_actor_scope(access.actor):
                    workflow_handlers.generate_waveform(
                        job_payload, lambda *_args: None, threading.Event(),
                    )
            except (AccessDenied, ActorAuthorizationDenied):
                raise
            except (OSError, RuntimeError, ValueError):
                # Preserve the durable fallback for media/runtime failures only.
                pass
            else:
                cached = cached_response(cached_peak_id())
                if cached is not None:
                    return cached

        with database.immediate_session() as db_session:
            source = readable_preview_source(db_session, access, artifact_id)
            active = active_waveform_job(db_session, source)
            if active is not None:
                return jsonify({"status": active.status, "job_id": active.id}), 202
            require_artifact_run_access(db_session, access, artifact_id)
            owner = artifact_job_ownership(db_session, [source.id])
            with execution_actor_scope(access.actor):
                job = jobs.enqueue_in_session(
                    db_session, "audio.waveform", job_payload, session_id=source.session_id,
                    workspace_id=owner.workspace_id, project_id=owner.project_id,
                    resource_keys=[f"session:{source.session_id}"] if source.session_id
                    else [f"artifact:waveform:{source.id}"],
                )
        return jsonify({"status": "queued", "job_id": job.id}), 202
