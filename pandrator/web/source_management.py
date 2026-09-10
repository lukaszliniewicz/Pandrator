"""Explicit session source changes with previewed, session-scoped reset semantics.

Source-library originals and files referenced outside this session are never
purged. Removed derived files have durable tombstones, so interrupted cleanup is
retryable without resurrecting an old workflow path.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from . import models as m
from .artifact_selection import select_source_path
from .artifacts import sha256_file
from .source_resolution import (
    classify_source,
    resolve_media_source,
    resolve_primary_source,
)
from .subtitle_sources import parse_subtitle_source, subtitle_source_status_in_session
from .workspace import RevisionConflict

TERMINAL = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "expired",
    "deleted",
    "done",
    "superseded",
}
ACTIVE_JOB = {"queued", "running", "cancel_requested"}
DISPATCH_MODELS = (
    m.DispatchRun,
    m.SourceCleaningDispatchRun,
    m.SpeechOptimizationDispatchRun,
    m.MediaEditDispatchRun,
)
# These are owned results, not user configuration or usage/accounting records.
RESET_MODELS = (
    m.WorkflowExecutionPlan,
    *DISPATCH_MODELS,
    m.SubtitleEvidence,
    m.AgentRun,
    m.OutputAssembly,
    m.ExportRecord,
    m.GenerationRun,
    m.GenerationPlan,
    m.MediaEditPlan,
    m.Document,
    m.KnowledgeLedger,
    m.WorkflowRun,
    m.Job,
    m.SourceRecord,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def assert_session_idle(session, session_id: str) -> None:
    """Never race a worker or a leased external editing batch during a reset."""
    if session.scalar(
        select(m.Job.id)
        .where(m.Job.session_id == session_id, m.Job.status.in_(ACTIVE_JOB))
        .limit(1)
    ):
        raise RevisionConflict(
            "Stop or cancel active session work before changing its sources."
        )
    if session.scalar(
        select(m.GenerationRun.id)
        .where(
            m.GenerationRun.session_id == session_id,
            m.GenerationRun.status.in_(
                {"queued", "running", "pausing", "cancel_requested"}
            ),
        )
        .limit(1)
    ):
        raise RevisionConflict(
            "Stop or cancel audio generation before changing its sources."
        )
    for model in DISPATCH_MODELS:
        if session.scalar(
            select(model.id)
            .where(model.session_id == session_id, model.status.not_in(TERMINAL))
            .limit(1)
        ):
            raise RevisionConflict(
                "Finish or cancel the session's open editing dispatch before changing its sources."
            )


def _source_payload(source) -> dict[str, Any] | None:
    if source.artifact is None:
        return None
    return {
        "artifact_id": source.artifact.id,
        "source_asset_id": source.source_asset.id if source.source_asset else None,
        "attachment_id": source.attachment.id if source.attachment else None,
        "filename": source.name,
        "profile": source.profile,
        "size_bytes": source.artifact.size_bytes,
        "content_hash": source.artifact.content_hash,
    }


def source_status(session, session_id: str) -> dict[str, Any]:
    record = session.get(m.SessionRecord, session_id)
    if record is None or record.trashed_at is not None:
        raise KeyError(session_id)
    primary = resolve_primary_source(session, session_id)
    media = resolve_media_source(session, session_id)
    subtitle = subtitle_source_status_in_session(session, session_id)
    blocked = None
    try:
        assert_session_idle(session, session_id)
    except RevisionConflict as error:
        blocked = str(error)
    timing = session.get(m.SessionSetting, (session_id, "_recording_timing_review"))
    return {
        "timing_review": dict(timing.value_json or {}) if timing else {},
        "session_id": session_id,
        "session_revision": record.revision,
        "workflow_kind": record.workflow_kind,
        "primary": _source_payload(primary),
        "media": _source_payload(media)
        if media.resolution == "attached_media"
        else None,
        "subtitle": subtitle,
        "blocked_reason": blocked,
    }


def _protected_artifacts(session, session_id: str) -> set[str]:
    # Library/voice files and ancestors of another session's live artifacts are
    # shared, even if the original artifact row happens to belong to this session.
    protected = set(
        session.scalars(
            select(m.SourceAsset.artifact_id).where(
                m.SourceAsset.artifact_id.is_not(None)
            )
        )
    )
    protected.update(session.scalars(select(m.VoiceSample.artifact_id)))
    protected.update(
        session.scalars(
            select(m.Artifact.id).where(
                m.Artifact.role == "session_bundle", m.Artifact.state != "deleted"
            )
        )
    )
    protected.update(
        session.scalars(
            select(m.AudioTake.artifact_id)
            .join(
                m.GenerationSegment,
                m.GenerationSegment.id == m.AudioTake.generation_segment_id,
            )
            .join(
                m.GenerationPlanRevision,
                m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id,
            )
            .join(
                m.GenerationPlan,
                m.GenerationPlan.id == m.GenerationPlanRevision.plan_id,
            )
            .where(
                m.GenerationPlan.session_id != session_id,
                m.AudioTake.artifact_id.is_not(None),
            )
        )
    )
    protected.update(
        session.scalars(
            select(m.OutputAssembly.artifact_id).where(
                m.OutputAssembly.session_id != session_id,
                m.OutputAssembly.artifact_id.is_not(None),
            )
        )
    )
    protected.update(
        session.scalars(
            select(m.Artifact.id).where(
                m.Artifact.state != "deleted",
                (m.Artifact.session_id != session_id) | m.Artifact.session_id.is_(None),
            )
        )
    )
    parents: dict[str, set[str]] = {}
    for parent, child in session.execute(
        select(m.ArtifactEdge.parent_artifact_id, m.ArtifactEdge.child_artifact_id)
    ):
        parents.setdefault(child, set()).add(parent)
    frontier = list(protected)
    while frontier:
        for parent in parents.get(frontier.pop(), ()):
            if parent not in protected:
                protected.add(parent)
                frontier.append(parent)
    return protected


def _impact(
    session, session_id: str, role: str, new_source_asset_id: str | None
) -> dict[str, Any]:
    status = source_status(session, session_id)
    artifacts = list(
        session.scalars(
            select(m.Artifact).where(
                m.Artifact.session_id == session_id, m.Artifact.state != "deleted"
            )
        )
    )
    owned = {}
    revision_rows = []
    for model in RESET_MODELS:
        rows = list(
            session.scalars(select(model).where(model.session_id == session_id))
        )
        owned[model.__tablename__] = len(rows)
        revision_rows.extend(
            (
                model.__tablename__,
                row.id,
                getattr(row, "status", None),
                getattr(row, "updated_at", None),
            )
            for row in rows
        )
    segments = list(
        session.execute(
            select(
                m.GenerationSegment.id,
                m.GenerationSegment.revision,
                m.GenerationSegment.status,
            )
            .join(
                m.GenerationPlanRevision,
                m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id,
            )
            .join(
                m.GenerationPlan,
                m.GenerationPlan.id == m.GenerationPlanRevision.plan_id,
            )
            .where(m.GenerationPlan.session_id == session_id)
        )
    )
    protected = _protected_artifacts(session, session_id)
    result = {
        "session_revision": status["session_revision"],
        "role": role,
        "new_source_asset_id": new_source_asset_id,
        "blocked_reason": status["blocked_reason"],
        "destructive": role == "primary" and status["primary"] is not None,
        "counts": owned if role == "primary" else {},
        "artifact_counts": dict(Counter(a.role for a in artifacts))
        if role == "primary"
        else {},
        "derived_files": sum(a.id not in protected for a in artifacts)
        if role == "primary"
        else 0,
        "shared_files_retained": sum(a.id in protected for a in artifacts)
        if role == "primary"
        else 0,
        "preserves": [
            "source-library originals",
            "shared files",
            "session settings",
            "voices",
            "usage records",
        ],
    }
    result["impact_token"] = _digest(
        {
            "result": result,
            "primary": status["primary"],
            "media": status["media"],
            "artifacts": sorted(
                (a.id, a.state, a.content_hash, str(a.updated_at)) for a in artifacts
            ),
            "owned": sorted(revision_rows, key=str),
            "segments": sorted(tuple(row) for row in segments),
        }
    )
    return result


def preview_source_change(
    session, session_id: str, role: str, new_source_asset_id: str | None
) -> dict[str, Any]:
    if role not in {"primary", "media"}:
        raise ValueError(
            "Source changes support the primary source or associated media."
        )
    return _impact(session, session_id, role, new_source_asset_id)


def _validate_source(services, session, source_asset_id: str | None, role: str):
    if source_asset_id is None:
        return None
    asset = session.get(m.SourceAsset, source_asset_id)
    artifact = (
        session.get(m.Artifact, asset.artifact_id)
        if asset and asset.artifact_id
        else None
    )
    if (
        asset is None
        or asset.state != "current"
        or artifact is None
        or artifact.state == "deleted"
    ):
        raise ValueError(
            "Choose an available managed source before replacing the current input."
        )
    path = services.paths.managed_path(artifact.relative_path)
    if not path.is_file() or sha256_file(path) != artifact.content_hash:
        raise ValueError(
            "The replacement source is missing or its bytes changed. Upload it again before resetting."
        )
    profile = classify_source(
        name=asset.display_name, kind=asset.kind, mime_type=asset.mime_type or ""
    )
    if role == "media" and profile not in {"audio", "video"}:
        raise ValueError("The associated recording must be an audio or video file.")
    if profile in {"audio", "video"}:
        from .soundtrack_export import probe_soundtrack_media
        import subprocess

        try:
            inspected = probe_soundtrack_media(path)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ValueError(
                "The replacement recording is not readable media. The current session was not reset."
            ) from error
        if not inspected["has_audio"] and not inspected["has_video"]:
            raise ValueError(
                "The replacement contains no usable audio or video stream."
            )
    elif asset.kind.lower().lstrip(".") == "pdf":
        import fitz

        try:
            with fitz.open(path) as document:
                if document.needs_pass or document.page_count == 0:
                    raise ValueError(
                        "Use an unlocked, non-empty PDF as the replacement source."
                    )
        except Exception as error:
            raise ValueError(
                "The replacement PDF cannot be opened. The current session was not reset."
            ) from error
    elif asset.kind.lower().lstrip(".") == "epub":
        import zipfile

        try:
            with zipfile.ZipFile(path) as archive:
                if "META-INF/container.xml" not in archive.namelist():
                    raise ValueError("The replacement is not an EPUB container.")
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError(
                "The replacement EPUB cannot be opened. The current session was not reset."
            ) from error
    if profile == "subtitles":
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Subtitle sources must be 32 MiB or smaller.")
        parse_subtitle_source(
            path.read_text(encoding="utf-8-sig"), asset.kind.lower().lstrip(".")
        )
    return asset


def _reset_results(session, session_id: str) -> None:
    protected = _protected_artifacts(session, session_id)
    for artifact in session.scalars(
        select(m.Artifact).where(m.Artifact.session_id == session_id)
    ):
        if artifact.id in protected:
            # Do not let the session's legacy artifact fallback reselect an old
            # source. The library identity and all external references survive.
            artifact.session_id = None
        elif artifact.state != "deleted":
            artifact.state = "deleted"
            artifact.updated_at = m.utcnow()
            artifact.metadata_json = {
                "source_reset_cleanup_pending": True,
                "reset_session_id": session_id,
            }
    session.flush()
    for model in RESET_MODELS:
        session.execute(delete(model).where(model.session_id == session_id))
    session.execute(
        delete(m.SessionSource).where(
            m.SessionSource.session_id == session_id,
            m.SessionSource.role.in_({"primary", "media", "transcript"}),
        )
    )
    select_source_path(session, session_id, None)
    session.flush()


def _invalidate_recording_outputs(
    session, session_id: str, old_media_id: str | None
) -> None:
    if not old_media_id:
        return
    # Text and raw takes remain useful. Only timing evidence, assemblies, mixes
    # and exports depend on the recording's cut/timeline.
    for artifact in session.scalars(
        select(m.Artifact).where(
            m.Artifact.session_id == session_id, m.Artifact.state != "deleted"
        )
    ):
        role = artifact.role
        timing = any(
            word in role
            for word in ("word_timestamp", "alignment", "aligned_word", "media_edit")
        )
        output = role.startswith("export") or role in {
            "assembled_audio",
            "dubbing_audio",
            "mixed_audio",
            "soundtrack_mix",
            "soundtrack_master",
        }
        if timing or output:
            artifact.state = "stale"
            artifact.updated_at = m.utcnow()
            artifact.metadata_json = {
                **dict(artifact.metadata_json or {}),
                "recording_changed_from": old_media_id,
            }
        elif role in {"transcription", "correction", "translation"}:
            metadata = dict(artifact.metadata_json or {})
            if (
                metadata.get("aligned_word_timestamps_artifact_id")
                or metadata.get("source_media_artifact_id") == old_media_id
            ):
                metadata["timing_requires_review"] = True
                artifact.metadata_json = metadata
    for assembly in session.scalars(
        select(m.OutputAssembly).where(m.OutputAssembly.session_id == session_id)
    ):
        assembly.status = "stale"
    selected_export = session.get(m.SessionStageSelection, (session_id, "export"))
    if selected_export is not None:
        selected_export.artifact_id = None
        selected_export.revision += 1
        selected_export.updated_at = m.utcnow()


def change_source_in_session(
    services,
    session,
    session_id: str,
    *,
    role: str,
    new_source_asset_id: str | None,
    expected_revision: int,
    impact_token: str,
) -> dict[str, Any]:
    status = source_status(session, session_id)
    if status["session_revision"] != expected_revision:
        raise RevisionConflict(
            "The session changed. Review the source-change warning again."
        )
    impact = _impact(session, session_id, role, new_source_asset_id)
    if impact["impact_token"] != impact_token:
        raise RevisionConflict(
            "The affected work changed. Review the source-change warning again."
        )
    assert_session_idle(session, session_id)
    asset = _validate_source(services, session, new_source_asset_id, role)
    current = status[role]
    if current and asset and current["content_hash"] == asset.content_hash:
        return {**status, "unchanged": True}
    if role == "media" and not status["subtitle"]["supported"]:
        raise ValueError(
            "Separate associated media is available for subtitle-first sessions."
        )
    if role == "primary":
        session.execute(
            delete(m.SessionSetting).where(
                m.SessionSetting.session_id == session_id,
                m.SessionSetting.section == "_recording_timing_review",
            )
        )
        _reset_results(session, session_id)
    else:
        _invalidate_recording_outputs(
            session, session_id, current["artifact_id"] if current else None
        )
        session.execute(
            delete(m.SessionSource).where(
                m.SessionSource.session_id == session_id,
                m.SessionSource.role == "media",
            )
        )
    if role == "media":
        timing = session.get(m.SessionSetting, (session_id, "_recording_timing_review"))
        required = bool(
            current or (timing and (timing.value_json or {}).get("required"))
        )
        if timing is None:
            timing = m.SessionSetting(
                session_id=session_id, section="_recording_timing_review", value_json={}
            )
            session.add(timing)
        timing.value_json = {
            "required": required,
            "media_artifact_id": asset.artifact_id if asset else None,
        }
        timing.revision = (timing.revision or 0) + 1
    if asset is not None:
        services.source_library.attach_in_session(
            session, session_id, asset.id, role=role
        )
    record = session.get(m.SessionRecord, session_id)
    record.revision += 1
    record.updated_at = m.utcnow()
    record.status = "idle"
    session.flush()
    return {
        **source_status(session, session_id),
        "unchanged": False,
        "removed": impact["counts"],
    }


def start_new_source_session_in_session(
    services,
    session,
    session_id: str,
    *,
    role: str,
    new_source_asset_id: str | None,
    expected_revision: int,
    impact_token: str,
) -> dict[str, Any]:
    """Start a clean session with the same configuration; never alter old work."""
    from copy import deepcopy

    if role != "primary":
        raise ValueError("Starting a new session applies to a primary-source change.")
    original = session.get(m.SessionRecord, session_id)
    impact = _impact(session, session_id, role, new_source_asset_id)
    if (
        original is None
        or original.revision != expected_revision
        or impact["impact_token"] != impact_token
    ):
        raise RevisionConflict(
            "The source selection changed. Review the source-change dialog again."
        )
    asset = _validate_source(services, session, new_source_asset_id, role)
    created = services.sessions.create(
        f"{original.name[:240]} — new source",
        workflow_kind=original.workflow_kind,
        source_language=original.source_language,
        target_language=original.target_language,
        workflow_preset=original.workflow_preset,
        included_stages=list(original.included_stages_json or []),
        db_session=session,
    )
    for setting in session.scalars(
        select(m.SessionSetting).where(m.SessionSetting.session_id == session_id)
    ):
        if setting.section != "_recording_timing_review":
            session.add(
                m.SessionSetting(
                    session_id=created.id,
                    section=setting.section,
                    value_json=deepcopy(setting.value_json),
                    revision=1,
                )
            )
    outcome = session.get(m.OutcomePlan, session_id)
    if outcome is not None:
        session.add(
            m.OutcomePlan(
                session_id=created.id,
                value_json=deepcopy(outcome.value_json),
                revision=1,
            )
        )
    session.flush()
    if asset is not None:
        services.source_library.attach_in_session(
            session, created.id, asset.id, role="primary"
        )
    session.flush()
    return {
        "session_id": created.id,
        "original_session_id": session_id,
        "original_preserved": True,
        "name": created.name,
    }


def cleanup_reset_files(services, session_id: str) -> dict[str, int]:
    """Retry only this session's tombstoned, unreferenced managed files."""
    removed = 0
    pending = 0
    with services.database.immediate_session() as session:
        protected = _protected_artifacts(session, session_id)
        rows = list(
            session.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == session_id,
                    m.Artifact.state == "deleted",
                    m.Artifact.metadata_json["source_reset_cleanup_pending"]
                    .as_boolean()
                    .is_(True),
                )
            )
        )
        for artifact in rows:
            try:
                if artifact.id in protected:
                    pending += 1
                    continue
                path: Path = services.paths.managed_path(artifact.relative_path)
                path.unlink(missing_ok=True)
                artifact.metadata_json = {
                    "reset_session_id": session_id,
                    "source_reset_cleanup_pending": False,
                }
                removed += 1
            except (OSError, ValueError):
                pending += 1
    return {"files_removed": removed, "cleanup_pending": pending}


def confirm_recording_timing(
    session, session_id: str, *, expected_revision: int, media_artifact_id: str
) -> dict[str, Any]:
    status = source_status(session, session_id)
    assert_session_idle(session, session_id)
    if (
        status["session_revision"] != expected_revision
        or not status["media"]
        or status["media"]["artifact_id"] != media_artifact_id
    ):
        raise RevisionConflict(
            "The associated recording changed. Check the current recording before confirming its timing."
        )
    timing = session.get(m.SessionSetting, (session_id, "_recording_timing_review"))
    if timing is not None:
        timing.value_json = {
            "required": False,
            "media_artifact_id": media_artifact_id,
            "verified_at": m.utcnow().isoformat(),
        }
        timing.revision += 1
    return {
        "session_id": session_id,
        "media_artifact_id": media_artifact_id,
        "timing_verified": True,
    }


def require_recording_timing_review(
    database, session_id: str, settings: dict[str, Any]
) -> None:
    if (
        str(settings.get("export_mode") or "media") not in {"media", "audio"}
        or settings.get("audio_mode") == "preserve"
    ):
        return
    with database.session() as session:
        timing = session.get(m.SessionSetting, (session_id, "_recording_timing_review"))
        if (
            timing
            and (timing.value_json or {}).get("required")
            and resolve_media_source(session, session_id).has_audio
        ):
            raise ValueError(
                "The associated recording changed. Verify speech timing against it in the Source card before exporting a synchronized soundtrack."
            )
