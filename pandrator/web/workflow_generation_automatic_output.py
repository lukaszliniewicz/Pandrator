"""Automatic take publication and final WAV assembly with explicit requests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from .artifacts import ArtifactService
from .database import Database
from .models import Artifact, AudioTake, GenerationSegment
from .workflow_generation_protocols import Progress, RecordTtsUsageProtocol


@dataclass(frozen=True, slots=True)
class AutomaticTakePublication:
    path: Path
    session_id: str
    segment_id: str
    source_artifact_id: str
    stored_settings: dict[str, Any]
    usage_settings: dict[str, Any]
    synthesized_text: str
    duration_ms: int
    job_id: str | None
    metadata: dict[str, Any]
    verification: dict[str, Any] | None


def publish_automatic_take(
    database: Database,
    artifacts: ArtifactService,
    publication: AutomaticTakePublication,
    *,
    record_tts_usage: RecordTtsUsageProtocol,
) -> tuple[Artifact, GenerationSegment]:
    """Retain the automatic route's separate artifact, usage, and take commits."""
    take_artifact = artifacts.register(
        publication.path,
        kind="audio",
        role="generation_take",
        session_id=publication.session_id,
        parent_ids=[publication.source_artifact_id],
        settings=publication.stored_settings,
        metadata=publication.metadata,
    )
    record_tts_usage(
        publication.session_id,
        publication.usage_settings,
        publication.synthesized_text,
        publication.duration_ms,
        job_id=publication.job_id,
        artifact_id=take_artifact.id,
    )
    with database.immediate_session() as session:
        segment = session.get(GenerationSegment, publication.segment_id)
        if segment is None:
            raise KeyError(publication.segment_id)
        session.execute(
            update(AudioTake)
            .where(
                AudioTake.generation_segment_id == publication.segment_id,
                AudioTake.is_active.is_(True),
            )
            .values(is_active=False, revision=AudioTake.revision + 1)
            .execution_options(synchronize_session=False)
        )
        segment.status = "completed"
        if (
            publication.verification is not None
            and publication.verification.get("status") != "passed"
        ):
            segment.marked = True
        session.add(
            AudioTake(
                generation_segment_id=publication.segment_id,
                artifact_id=take_artifact.id,
                kind="tts",
                status="completed",
                settings_hash=take_artifact.settings_hash,
                duration_ms=publication.duration_ms,
                is_active=True,
            )
        )
    return take_artifact, segment


@dataclass(frozen=True, slots=True)
class AutomaticAudioOutput:
    session_id: str
    source_artifact_id: str
    role: str
    destination: Path
    settings: dict[str, Any]
    assembly_inputs: list[tuple[Path, int, int] | tuple[Path, int, int, dict[str, Any]]]
    take_artifact_ids: list[str]
    segment_count: int
    plan_revision_id: str
    allow_internal_silences: bool = True


def assemble_automatic_output(
    artifacts: ArtifactService,
    output: AutomaticAudioOutput,
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Assemble and register the automatic route's final WAV output."""
    from .assembly_silences import render_part_silences, silence_manifest
    from .audio_assembly import (
        AudioAssemblyPart,
        assemble_audio_plan,
        build_audio_assembly_plan,
        preferred_pcm_format,
    )
    from .media_process import MediaProcessCancelled

    session_id = output.session_id
    role = output.role
    destination = output.destination
    settings = output.settings
    assembly_inputs = output.assembly_inputs
    take_artifact_ids = output.take_artifact_ids
    revision_id = output.plan_revision_id
    fade_enabled = bool(settings.get("fade_enabled", settings.get("enable_fade", False)))
    fade_in_ms = (
        max(
            0,
            int(settings.get("fade_in_ms", settings.get("fade_in_duration", 0)) or 0),
        )
        if fade_enabled
        else 0
    )
    fade_out_ms = (
        max(
            0,
            int(settings.get("fade_out_ms", settings.get("fade_out_duration", 0)) or 0),
        )
        if fade_enabled
        else 0
    )
    sample_rate_hz, channels = preferred_pcm_format(
        assembly_inputs[0][0],
        cancel_event=cancel_event,
    )
    planned_parts = []
    take_rows = []
    unique_take_artifact_ids = list(dict.fromkeys(take_artifact_ids))
    take_metadata_by_artifact: dict[str, dict[str, Any]] = {}
    with artifacts.database.session() as session:
        for offset in range(0, len(unique_take_artifact_ids), 500):
            artifact_id_chunk = unique_take_artifact_ids[offset : offset + 500]
            rows = session.execute(
                select(AudioTake, GenerationSegment)
                .join(
                    GenerationSegment,
                    GenerationSegment.id == AudioTake.generation_segment_id,
                )
                .where(AudioTake.artifact_id.in_(artifact_id_chunk))
            )
            for take, segment in rows:
                take_metadata_by_artifact.setdefault(
                    take.artifact_id,
                    {
                        "take_id": take.id,
                        "take_revision": take.revision,
                        "segment_id": segment.id,
                        "segment_revision": segment.revision,
                        "kind": take.kind,
                        "node_kind": segment.node_kind,
                        "speaker": segment.speaker,
                    },
                )
    for index, item in enumerate(assembly_inputs):
        path, duration_ms, silence_after_ms = item[:3]
        if len(item) == 4:
            metadata = item[3]
        else:
            retained, _ = artifacts.resolve(take_artifact_ids[index])
            metadata = retained.metadata_json or {}
        take_rows.append(
            {
                "artifact_id": take_artifact_ids[index],
                **take_metadata_by_artifact.get(take_artifact_ids[index], {}),
            }
        )
        planned_parts.append(AudioAssemblyPart(
            path=path,
            expected_duration_ms=duration_ms,
            silence_after_ms=max(0, int(silence_after_ms or 0)) if index < len(assembly_inputs) - 1 else 0,
            fade_in_ms=fade_in_ms,
            fade_out_ms=fade_out_ms,
            internal_silences=render_part_silences(metadata, settings) if output.allow_internal_silences else (),
        ))
    plan = build_audio_assembly_plan(
        planned_parts,
        output_format="wav",
        sample_rate_hz=sample_rate_hz,
        channels=channels,
    )
    try:
        assembly_result = assemble_audio_plan(
            plan,
            destination,
            cancel_event=cancel_event,
        )
    except MediaProcessCancelled:
        return {}
    artifact = artifacts.register(
        destination,
        kind="audio",
        role=role,
        session_id=session_id,
        parent_ids=[output.source_artifact_id, *take_artifact_ids],
        settings=settings,
        metadata={
            "segment_count": output.segment_count,
            "service": settings.get("service") or settings.get("tts_service") or "XTTS",
            "duration_ms": assembly_result.duration_ms,
            "assembly_backend": assembly_result.backend,
            "audio_timeline": {"version": 1, "sample_rate_hz": assembly_result.sample_rate_hz,
                               "total_frames": assembly_result.total_frames},
            "takes": [
                {**take_rows[index],
                 "duration_ms": assembly_result.part_duration_ms[index],
                 "start_frame": assembly_result.part_start_frames[index],
                 "end_frame": assembly_result.part_end_frames[index],
                 "silence_after_ms": part.silence_after_ms,
                 "silence_policy": "assembly-v1",
                 "internal_silences": silence_manifest(part.internal_silences)}
                for index, part in enumerate(plan.parts)
            ],
        },
    )
    progress(1.0, "Audio ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "segments": output.segment_count,
        "generation_plan_revision_id": revision_id,
    }
