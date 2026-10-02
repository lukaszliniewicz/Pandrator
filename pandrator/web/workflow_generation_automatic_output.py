"""Automatic take publication and final WAV assembly with explicit requests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import update

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
    assembly_inputs: list[tuple[Path, int, int]]
    take_artifact_ids: list[str]
    segment_count: int
    plan_revision_id: str


def assemble_automatic_output(
    artifacts: ArtifactService,
    output: AutomaticAudioOutput,
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Assemble and register the automatic route's final WAV output."""
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
    plan = build_audio_assembly_plan(
        [
            AudioAssemblyPart(
                path=path,
                expected_duration_ms=duration_ms,
                silence_after_ms=(
                    max(0, int(silence_after_ms or 0)) if index < len(assembly_inputs) - 1 else 0
                ),
                fade_in_ms=fade_in_ms,
                fade_out_ms=fade_out_ms,
            )
            for index, (path, duration_ms, silence_after_ms) in enumerate(assembly_inputs)
        ],
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
        },
    )
    progress(1.0, "Audio ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "segments": output.segment_count,
        "generation_plan_revision_id": revision_id,
    }
