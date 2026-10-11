"""Frozen selection and transactional admission for speech-take comparisons."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .artifacts import sha256_file
from .comparison_export_schemas import ComparisonExportRequest
from .generation_audio_version_assembly import audio_version_assembly_selection
from .jobs import JobQueue
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    SessionRecord,
)
from .settings_policy import stable_hash

COMPARISON_EXPORT_JOB_KIND = "export.comparison"
COMPARISON_EXPORT_VERSION = 1
TERMINAL_TAKE_STATUSES = frozenset({"completed", "stale"})


@dataclass(frozen=True, slots=True)
class PreparedComparisonExport:
    """JSON-safe immutable inputs captured before the enqueue transaction."""

    session_id: str
    output_format: str
    gap_ms: int
    clips: tuple[dict[str, Any], ...]
    selection_hash: str

    def job_payload(self) -> dict[str, Any]:
        return {
            "version": COMPARISON_EXPORT_VERSION,
            "session_id": self.session_id,
            "output_format": self.output_format,
            "gap_ms": self.gap_ms,
            "clips": [dict(clip) for clip in self.clips],
            "selection_hash": self.selection_hash,
        }


def _direction_source_refs(segment: GenerationSegment) -> dict[str, Any]:
    """Keep stable direction/source identifiers without copying spoken text."""

    source_ids = segment.source_segment_ids_json or []
    return {
        "source_segment_ids": list(source_ids),
        "speaker": segment.speaker,
        "voice_id": segment.voice_id,
        "voice": segment.voice,
        "language": segment.language,
    }


def _direction_source_hash(segment: GenerationSegment) -> str:
    return stable_hash(
        {
            "direction_source_refs": _direction_source_refs(segment),
            "speech_plan": segment.speech_plan_json or {},
            "speech_block_provenance": segment.speech_block_provenance_json or {},
        }
    )


def _run_settings_snapshot_hash(run: GenerationRun) -> str:
    return stable_hash(run.settings_snapshot_json or {})


def comparison_export_selection_hash(payload: dict[str, Any]) -> str:
    return stable_hash(
        {
            "version": COMPARISON_EXPORT_VERSION,
            "session_id": payload.get("session_id"),
            "output_format": payload.get("output_format"),
            "gap_ms": payload.get("gap_ms"),
            "clips": payload.get("clips"),
        }
    )


def _require_session(session, session_id: str) -> SessionRecord:
    record = session.get(SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    if record.trashed_at is not None or record.status == "purging":
        raise ValueError("Restore the session before exporting comparison audio.")
    return record


def _require_run(session, session_id: str, run_id: str) -> tuple[GenerationRun, GenerationPlanRevision]:
    run = session.get(GenerationRun, run_id)
    if run is None or run.session_id != session_id:
        raise KeyError(run_id)
    revision = session.get(GenerationPlanRevision, run.plan_revision_id)
    plan = session.get(GenerationPlan, revision.plan_id) if revision else None
    if revision is None or plan is None or plan.session_id != session_id:
        raise ValueError("The selected generation run has no speech plan in this session.")
    return run, revision


def _require_segment(session, segment_id: str, plan_revision_id: str) -> GenerationSegment:
    segment = session.get(GenerationSegment, segment_id)
    if (
        segment is None
        or segment.plan_revision_id != plan_revision_id
        or segment.removed
    ):
        raise KeyError(segment_id)
    return segment


def _select_regular_take(
    session,
    *,
    session_id: str,
    selected_run: GenerationRun,
    segment: GenerationSegment,
    explicit_take_id: str | None,
) -> tuple[AudioTake, Artifact]:
    if explicit_take_id is not None:
        take = session.get(AudioTake, explicit_take_id)
        origin_run = session.get(GenerationRun, take.generation_run_id) if take and take.generation_run_id else None
        if (
            take is None
            or take.generation_segment_id != segment.id
            or take.status not in TERMINAL_TAKE_STATUSES
            or origin_run is None
            or origin_run.session_id != session_id
            or origin_run.plan_revision_id != selected_run.plan_revision_id
            or origin_run.sequence_number > selected_run.sequence_number
        ):
            raise ValueError(
                "The explicit take must be a completed or stale take for this segment, session, and run cutoff."
            )
    else:
        selected = session.execute(
            select(AudioTake, GenerationRun, Artifact)
            .join(GenerationRun, AudioTake.generation_run_id == GenerationRun.id)
            .outerjoin(Artifact, Artifact.id == AudioTake.artifact_id)
            .where(
                AudioTake.generation_segment_id == segment.id,
                AudioTake.status.in_(tuple(TERMINAL_TAKE_STATUSES)),
                GenerationRun.session_id == session_id,
                GenerationRun.plan_revision_id == selected_run.plan_revision_id,
                GenerationRun.sequence_number <= selected_run.sequence_number,
            )
            .order_by(
                GenerationRun.sequence_number.desc(),
                AudioTake.created_at.desc(),
                AudioTake.id.desc(),
            )
            .limit(1)
        ).one_or_none()
        if selected is None:
            raise ValueError(
                f"Segment {segment.ordinal + 1} has no completed or stale audio take at the selected run cutoff."
            )
        take, _origin_run, artifact = selected
        if artifact is None:
            raise ValueError(f"Segment {segment.ordinal + 1} references a missing audio artifact.")
        return take, artifact

    artifact = session.get(Artifact, take.artifact_id) if take.artifact_id else None
    if artifact is None:
        raise ValueError("The selected audio take has no available artifact.")
    return take, artifact


def _validate_source_file(
    paths: DataPaths,
    artifact: Artifact,
    *,
    session_id: str,
    segment_ordinal: int,
) -> tuple[Path, str, int]:
    if (
        artifact.state != "current"
        or artifact.session_id != session_id
        or artifact.kind != "audio"
        or not artifact.relative_path
        or not artifact.content_hash
    ):
        raise ValueError(f"Segment {segment_ordinal + 1} references an unavailable audio artifact.")
    try:
        relative = Path(artifact.relative_path)
        if relative.is_absolute():
            raise ValueError("Audio artifact path is not a managed relative path.")
        unmanaged = paths.root / relative
        path = paths.managed_path(artifact.relative_path)
    except (OSError, ValueError) as error:
        raise ValueError("The selected audio artifact path is not managed by this application.") from error
    if unmanaged.is_symlink() or not path.is_file():
        raise ValueError(f"Audio take file is missing or unsafe for segment {segment_ordinal + 1}.")
    try:
        content_hash = sha256_file(path)
    except OSError as error:
        raise ValueError(f"Audio take file could not be read for segment {segment_ordinal + 1}.") from error
    if content_hash != artifact.content_hash:
        raise ValueError(f"Audio take content hash changed for segment {segment_ordinal + 1}.")
    duration_ms = _audio_duration_ms(path)
    if duration_ms <= 0:
        raise ValueError(f"Audio take has no usable duration for segment {segment_ordinal + 1}.")
    return path, paths.relative_managed_path(path), duration_ms


def _audio_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getcomptype() == "NONE" and reader.getframerate() > 0:
                return int((reader.getnframes() * 1000 + reader.getframerate() - 1) // reader.getframerate())
    except (EOFError, OSError, wave.Error):
        pass
    from .media_process import probe_audio_stream

    return int(probe_audio_stream(path).duration_ms)


def _descriptor(
    *,
    ordinal: int,
    requested: dict[str, Any],
    selected_run: GenerationRun,
    revision: GenerationPlanRevision,
    segment: GenerationSegment,
    take: AudioTake,
    origin_run: GenerationRun,
    artifact: Artifact,
    paths: DataPaths,
    recast_selection_hash: str | None,
) -> dict[str, Any]:
    path, managed_relative_path, duration_ms = _validate_source_file(
        paths,
        artifact,
        session_id=selected_run.session_id,
        segment_ordinal=segment.ordinal,
    )
    del path
    direction_source_refs = _direction_source_refs(segment)
    run_settings = dict(origin_run.settings_snapshot_json or {})
    return {
        "ordinal": ordinal,
        "generation_run_id": selected_run.id,
        "generation_run_sequence_number": selected_run.sequence_number,
        "generation_run_operation": selected_run.operation,
        "source_generation_run_id": selected_run.source_generation_run_id,
        "run_settings_hash": selected_run.settings_hash,
        "run_settings_snapshot_hash": _run_settings_snapshot_hash(selected_run),
        "plan_revision_id": revision.id,
        "plan_content_hash": revision.content_hash,
        "audio_version_side": requested["audio_version_side"],
        "audio_version_selection_hash": recast_selection_hash,
        "requested_take_id": requested.get("take_id"),
        "segment_id": segment.id,
        "segment_ordinal": segment.ordinal,
        "segment_revision": segment.revision,
        "segment_source_ids": list(segment.source_segment_ids_json or []),
        "direction_source_refs": direction_source_refs,
        "direction_source_hash": _direction_source_hash(segment),
        "take_id": take.id,
        "take_revision": take.revision,
        "take_parent_id": take.parent_take_id,
        "take_status": take.status,
        "take_kind": take.kind,
        "take_settings_hash": take.settings_hash,
        "origin_generation_run_id": origin_run.id,
        "origin_sequence_number": origin_run.sequence_number,
        "origin_run_operation": origin_run.operation,
        "origin_source_generation_run_id": origin_run.source_generation_run_id,
        "origin_run_settings_hash": origin_run.settings_hash,
        "origin_run_settings_snapshot_hash": stable_hash(run_settings),
        "artifact_id": artifact.id,
        "artifact_relative_path": managed_relative_path,
        "artifact_content_hash": artifact.content_hash,
        "duration_ms": duration_ms,
    }


def prepare_comparison_export(
    database,
    paths: DataPaths,
    session_id: str,
    request: dict[str, Any],
) -> PreparedComparisonExport:
    """Resolve exact selected takes and hash all source bytes before enqueue."""

    body = ComparisonExportRequest.model_validate(request).model_dump(mode="json")
    clips: list[dict[str, Any]] = []
    with database.session() as session:
        _require_session(session, session_id)
        for ordinal, requested in enumerate(body["clips"], start=1):
            run, revision = _require_run(
                session, session_id, requested["generation_run_id"]
            )
            segment = _require_segment(session, requested["segment_id"], revision.id)
            run_audio_version = run.audio_version_json
            is_recast = bool(
                isinstance(run_audio_version, dict)
                and run_audio_version.get("version") == 1
                and run_audio_version.get("kind") == "recast"
            )
            if run_audio_version is not None and not is_recast:
                raise ValueError("The selected generation run has an invalid audio-version selection.")
            recast_selection_hash = None
            if is_recast:
                selection, pinned_rows = audio_version_assembly_selection(
                    session, session_id, run, requested["audio_version_side"]
                )
                if selection is None:
                    raise ValueError("The selected recast audio side is unavailable.")
                chosen = next(
                    (row for row in pinned_rows if row[0].id == segment.id), None
                )
                if chosen is None:
                    raise ValueError("The selected segment is not part of this recast audio version.")
                selected_segment, take, artifact = chosen
                if requested.get("take_id") is not None and requested["take_id"] != take.id:
                    raise ValueError("An explicit recast take must match the selected pinned side.")
                origin_run = session.get(GenerationRun, take.generation_run_id) if take.generation_run_id else None
                if origin_run is None:
                    raise ValueError("The pinned recast take has no origin generation run.")
                recast_selection_hash = selection["audio_version_selection_hash"]
                segment = selected_segment
            else:
                if requested["audio_version_side"] != "after":
                    raise ValueError("Before audio is available only for a recast generation run.")
                take, artifact = _select_regular_take(
                    session,
                    session_id=session_id,
                    selected_run=run,
                    segment=segment,
                    explicit_take_id=requested.get("take_id"),
                )
                origin_run = session.get(GenerationRun, take.generation_run_id) if take.generation_run_id else None
                if origin_run is None:
                    raise ValueError("The selected take has no origin generation run.")
            clips.append(
                _descriptor(
                    ordinal=ordinal,
                    requested=requested,
                    selected_run=run,
                    revision=revision,
                    segment=segment,
                    take=take,
                    origin_run=origin_run,
                    artifact=artifact,
                    paths=paths,
                    recast_selection_hash=recast_selection_hash,
                )
            )
        selection_hash = comparison_export_selection_hash(
            {
                "session_id": session_id,
                "output_format": body["output_format"],
                "gap_ms": body["gap_ms"],
                "clips": clips,
            }
        )
    return PreparedComparisonExport(
        session_id=session_id,
        output_format=body["output_format"],
        gap_ms=body["gap_ms"],
        clips=tuple(clips),
        selection_hash=selection_hash,
    )


def revalidate_comparison_export_in_session(
    session,
    prepared_or_payload: PreparedComparisonExport | dict[str, Any],
) -> None:
    """Check that the queued exact descriptors still identify the same rows."""

    payload = (
        prepared_or_payload.job_payload()
        if isinstance(prepared_or_payload, PreparedComparisonExport)
        else prepared_or_payload
    )
    session_id = str(payload.get("session_id") or "")
    _require_session(session, session_id)
    if payload.get("version") != COMPARISON_EXPORT_VERSION:
        raise ValueError("The comparison export payload version is unsupported.")
    if (
        payload.get("output_format") not in ("wav", "mp3")
        or type(payload.get("gap_ms")) is not int
        or not 0 <= payload["gap_ms"] <= 10_000
        or payload.get("selection_hash") != comparison_export_selection_hash(payload)
    ):
        raise ValueError("The frozen comparison export options or selection hash are invalid.")
    clips = payload.get("clips")
    if not isinstance(clips, list) or not 1 <= len(clips) <= 100:
        raise ValueError("The frozen comparison export has an invalid clip list.")
    recast_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for expected_ordinal, descriptor in enumerate(clips, start=1):
        if not isinstance(descriptor, dict):
            raise ValueError("The frozen comparison export contains an invalid clip descriptor.")
        if descriptor.get("ordinal") != expected_ordinal:
            raise ValueError("The frozen comparison export clip order is invalid.")
        run_id = str(descriptor.get("generation_run_id") or "")
        run, revision = _require_run(session, session_id, run_id)
        segment = _require_segment(
            session, str(descriptor.get("segment_id") or ""), revision.id
        )
        if (
            revision.content_hash != descriptor.get("plan_content_hash")
            or run.sequence_number != descriptor.get("generation_run_sequence_number")
            or run.operation != descriptor.get("generation_run_operation")
            or run.source_generation_run_id != descriptor.get("source_generation_run_id")
            or run.settings_hash != descriptor.get("run_settings_hash")
            or _run_settings_snapshot_hash(run) != descriptor.get("run_settings_snapshot_hash")
            or segment.ordinal != descriptor.get("segment_ordinal")
            or segment.revision != descriptor.get("segment_revision")
            or list(segment.source_segment_ids_json or []) != descriptor.get("segment_source_ids")
            or _direction_source_refs(segment) != descriptor.get("direction_source_refs")
            or _direction_source_hash(segment) != descriptor.get("direction_source_hash")
        ):
            raise ValueError("The comparison export source selection changed after it was prepared.")

        side = str(descriptor.get("audio_version_side") or "")
        recast = isinstance(run.audio_version_json, dict) and run.audio_version_json.get("kind") == "recast"
        if recast:
            cache_key = (run.id, side)
            selection = recast_cache.get(cache_key)
            if selection is None:
                selection, rows = audio_version_assembly_selection(session, session_id, run, side)
                if selection is None:
                    raise ValueError("The selected recast audio side is no longer available.")
                recast_cache[cache_key] = {
                    "selection": selection,
                    "rows": {row[0].id: row for row in rows},
                }
            cached = recast_cache[cache_key]
            if cached["selection"].get("audio_version_selection_hash") != descriptor.get("audio_version_selection_hash"):
                raise ValueError("The pinned recast audio side changed after it was prepared.")
            chosen = cached["rows"].get(segment.id)
            if chosen is None or chosen[1].id != descriptor.get("take_id"):
                raise ValueError("The pinned recast take changed after it was prepared.")
            take, artifact = chosen[1], chosen[2]
            origin_run = session.get(GenerationRun, take.generation_run_id) if take.generation_run_id else None
            if (
                take.revision != descriptor.get("take_revision")
                or take.parent_take_id != descriptor.get("take_parent_id")
                or take.status != descriptor.get("take_status")
                or take.kind != descriptor.get("take_kind")
                or take.settings_hash != descriptor.get("take_settings_hash")
                or origin_run is None
                or origin_run.id != descriptor.get("origin_generation_run_id")
                or origin_run.sequence_number != descriptor.get("origin_sequence_number")
                or origin_run.operation != descriptor.get("origin_run_operation")
                or origin_run.source_generation_run_id != descriptor.get("origin_source_generation_run_id")
                or origin_run.settings_hash != descriptor.get("origin_run_settings_hash")
                or stable_hash(origin_run.settings_snapshot_json or {}) != descriptor.get("origin_run_settings_snapshot_hash")
            ):
                raise ValueError("The pinned recast take provenance changed after it was prepared.")
            if descriptor.get("requested_take_id") and descriptor.get("requested_take_id") != take.id:
                raise ValueError("The explicit recast take no longer matches its pinned side.")
        else:
            if side != "after" or descriptor.get("audio_version_selection_hash") is not None:
                raise ValueError("The selected generation run has an invalid audio-version side.")
            take = session.get(AudioTake, str(descriptor.get("take_id") or ""))
            origin_run = session.get(GenerationRun, take.generation_run_id) if take and take.generation_run_id else None
            if (
                take is None
                or take.generation_segment_id != segment.id
                or take.status not in TERMINAL_TAKE_STATUSES
                or take.status != descriptor.get("take_status")
                or take.revision != descriptor.get("take_revision")
                or take.parent_take_id != descriptor.get("take_parent_id")
                or take.kind != descriptor.get("take_kind")
                or take.settings_hash != descriptor.get("take_settings_hash")
                or origin_run is None
                or origin_run.session_id != session_id
                or origin_run.plan_revision_id != run.plan_revision_id
                or origin_run.sequence_number > run.sequence_number
                or origin_run.id != descriptor.get("origin_generation_run_id")
                or origin_run.sequence_number != descriptor.get("origin_sequence_number")
                or origin_run.operation != descriptor.get("origin_run_operation")
                or origin_run.source_generation_run_id != descriptor.get("origin_source_generation_run_id")
                or origin_run.settings_hash != descriptor.get("origin_run_settings_hash")
                or stable_hash(origin_run.settings_snapshot_json or {}) != descriptor.get("origin_run_settings_snapshot_hash")
            ):
                raise ValueError("The selected comparison take changed after it was prepared.")
            artifact = session.get(Artifact, take.artifact_id) if take.artifact_id else None

        if (
            artifact is None
            or artifact.id != descriptor.get("artifact_id")
            or artifact.state != "current"
            or artifact.kind != "audio"
            or artifact.session_id != session_id
            or artifact.relative_path != descriptor.get("artifact_relative_path")
            or artifact.content_hash != descriptor.get("artifact_content_hash")
        ):
            raise ValueError("A comparison source artifact was removed or changed after it was prepared.")


def admit_comparison_export_in_session(
    session,
    jobs: JobQueue,
    prepared: PreparedComparisonExport,
) -> dict[str, Any]:
    """Enqueue and freeze the idempotent receipt in the caller's transaction."""

    payload = prepared.job_payload()
    revalidate_comparison_export_in_session(session, payload)
    job = jobs.enqueue_in_session(
        session,
        COMPARISON_EXPORT_JOB_KIND,
        payload,
        session_id=prepared.session_id,
        max_attempts=1,
        resource_keys=[f"session:{prepared.session_id}"],
    )
    return {
        "id": job.id,
        "job_id": job.id,
        "work_id": job.id,
        "session_id": prepared.session_id,
        "kind": job.kind,
        "status": job.status,
        "clip_count": len(prepared.clips),
    }


__all__ = [
    "COMPARISON_EXPORT_JOB_KIND",
    "COMPARISON_EXPORT_VERSION",
    "PreparedComparisonExport",
    "admit_comparison_export_in_session",
    "comparison_export_selection_hash",
    "prepare_comparison_export",
    "revalidate_comparison_export_in_session",
]
