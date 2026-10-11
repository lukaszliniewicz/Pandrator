"""Render and atomically publish frozen selected speech-take comparisons."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    preferred_pcm_format,
    resolve_assembly_backend,
)
from .comparison_exports import (
    COMPARISON_EXPORT_JOB_KIND,
    _audio_duration_ms,
    revalidate_comparison_export_in_session,
)
from .job_execution import current_job_lease
from .media_process import MediaProcessCancelled
from .models import Artifact, Job, SessionRecord, utcnow


@dataclass(frozen=True, slots=True)
class ComparisonExportContext:
    database: Any
    paths: DataPaths
    artifacts: ArtifactService


def _cancel(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise MediaProcessCancelled("Comparison export was canceled.")


def _durable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not str(key).startswith("_")}


def _require_worker_claim(session, payload: dict[str, Any], cancel_event: threading.Event) -> None:
    _cancel(cancel_event)
    lease = current_job_lease.get()
    if lease is None:
        if payload.get("_job_id") or payload.get("_lease_generation"):
            raise InterruptedError("Comparison export worker lease is unavailable.")
        return
    job_id = str(payload.get("_job_id") or "")
    generation = payload.get("_lease_generation")
    job = session.get(Job, job_id) if job_id else None
    expires_at = job.lease_expires_at if job is not None else None
    # SQLite drops DateTime's timezone on read; stored lease times are UTC.
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        not job_id
        or generation is None
        or lease.get("job_id") != job_id
        or lease.get("lease_generation") != generation
        or job is None
        or job.kind != COMPARISON_EXPORT_JOB_KIND
        or job.session_id != payload.get("session_id")
        or job.status != "running"
        or job.lease_owner != lease.get("owner_id")
        or job.lease_generation != generation
        or expires_at is None
        or expires_at <= utcnow()
        or job.payload_json != _durable_payload(payload)
    ):
        raise InterruptedError("Comparison export was canceled or its worker lease is no longer current.")


def _source_paths(
    context: ComparisonExportContext,
    payload: dict[str, Any],
    cancel_event: threading.Event,
) -> list[tuple[dict[str, Any], Artifact, Path]]:
    _cancel(cancel_event)
    with context.database.session() as session:
        revalidate_comparison_export_in_session(session, payload)
        _require_worker_claim(session, payload, cancel_event)
        descriptors = list(payload["clips"])
        artifact_rows = {
            artifact.id: artifact
            for artifact in session.scalars(
                select(Artifact).where(
                    Artifact.id.in_([str(item["artifact_id"]) for item in descriptors])
                )
            )
        }
        session_record = session.get(SessionRecord, str(payload["session_id"]))
        if session_record is None:
            raise KeyError(payload["session_id"])
        storage_key = session_record.storage_key
    resolved: list[tuple[dict[str, Any], Artifact, Path]] = []
    for descriptor in descriptors:
        _cancel(cancel_event)
        artifact_id = str(descriptor.get("artifact_id") or "")
        artifact = artifact_rows.get(artifact_id)
        relative_path = str(descriptor.get("artifact_relative_path") or "")
        if (
            artifact is None
            or artifact.session_id != payload["session_id"]
            or artifact.state != "current"
            or artifact.kind != "audio"
            or artifact.relative_path != relative_path
            or artifact.content_hash != descriptor.get("artifact_content_hash")
        ):
            raise ValueError("A comparison source artifact is no longer available.")
        raw_path = context.paths.root / relative_path
        path = context.paths.managed_path(relative_path)
        if raw_path.is_symlink() or not path.is_file():
            raise ValueError("A comparison source audio file is missing or unsafe.")
        if sha256_file(path) != artifact.content_hash:
            raise ValueError("A comparison source audio file changed after it was selected.")
        duration_ms = _audio_duration_ms(path)
        if duration_ms != descriptor.get("duration_ms"):
            raise ValueError("A comparison source audio duration changed after it was selected.")
        resolved.append((descriptor, artifact, path))
    output_dir = context.paths.sessions / storage_key / "comparison-exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    return resolved


def _manifest_clip(
    descriptor: dict[str, Any],
    result,
    index: int,
) -> dict[str, Any]:
    rate = max(1, int(result.sample_rate_hz))
    start_frame = int(result.part_start_frames[index])
    end_frame = int(result.part_end_frames[index])
    return {
        "ordinal": descriptor["ordinal"],
        "generation_run_id": descriptor["generation_run_id"],
        "generation_run_sequence_number": descriptor["generation_run_sequence_number"],
        "generation_run_operation": descriptor["generation_run_operation"],
        "source_generation_run_id": descriptor["source_generation_run_id"],
        "run_settings_hash": descriptor["run_settings_hash"],
        "run_settings_snapshot_hash": descriptor["run_settings_snapshot_hash"],
        "plan_revision_id": descriptor["plan_revision_id"],
        "plan_content_hash": descriptor["plan_content_hash"],
        "audio_version_side": descriptor["audio_version_side"],
        "audio_version_selection_hash": descriptor["audio_version_selection_hash"],
        "segment_id": descriptor["segment_id"],
        "segment_ordinal": descriptor["segment_ordinal"],
        "segment_revision": descriptor["segment_revision"],
        "segment_source_ids": descriptor["segment_source_ids"],
        "direction_source_refs": descriptor["direction_source_refs"],
        "direction_source_hash": descriptor["direction_source_hash"],
        "take_id": descriptor["take_id"],
        "take_revision": descriptor["take_revision"],
        "take_parent_id": descriptor["take_parent_id"],
        "take_status": descriptor["take_status"],
        "take_kind": descriptor["take_kind"],
        "take_settings_hash": descriptor["take_settings_hash"],
        "origin_generation_run_id": descriptor["origin_generation_run_id"],
        "origin_sequence_number": descriptor["origin_sequence_number"],
        "origin_run_operation": descriptor["origin_run_operation"],
        "origin_source_generation_run_id": descriptor["origin_source_generation_run_id"],
        "origin_run_settings_hash": descriptor["origin_run_settings_hash"],
        "origin_run_settings_snapshot_hash": descriptor["origin_run_settings_snapshot_hash"],
        "artifact_id": descriptor["artifact_id"],
        "input_content_hash": descriptor["artifact_content_hash"],
        "input_duration_ms": descriptor["duration_ms"],
        "start_frame": start_frame,
        "end_frame": end_frame,
        "start_ms": int(round(start_frame * 1000 / rate)),
        "end_ms": int(round(end_frame * 1000 / rate)),
        "rendered_duration_ms": result.part_duration_ms[index],
    }


def _publish_pair(
    context: ComparisonExportContext,
    *,
    payload: dict[str, Any],
    audio_path: Path,
    manifest_path: Path,
    output_dir: Path,
    input_artifact_ids: list[str],
    settings: dict[str, Any],
    expected_audio_hash: str,
    audio_metadata: dict[str, Any],
    manifest_metadata: dict[str, Any],
    cancel_event: threading.Event,
) -> tuple[Artifact, Artifact]:
    audio_prepared = context.artifacts.prepare_registration(audio_path, settings=settings)
    manifest_prepared = context.artifacts.prepare_registration(manifest_path, settings=settings)
    if (
        not audio_prepared.content_hash
        or audio_prepared.content_hash != expected_audio_hash
        or not manifest_prepared.content_hash
    ):
        raise ValueError("Comparison export output hashes could not be calculated.")
    stem = f"comparison-{os.urandom(12).hex()}"
    final_audio = context.artifacts.next_available_path(
        output_dir / f"{stem}.{payload['output_format']}"
    )
    final_manifest = context.artifacts.next_available_path(
        output_dir / f"{stem}.manifest.json"
    )
    moved_audio = False
    moved_manifest = False
    committed = False
    registered_audio: Artifact | None = None
    registered_manifest: Artifact | None = None
    try:
        with context.database.immediate_session() as session:
            _require_worker_claim(session, payload, cancel_event)
            revalidate_comparison_export_in_session(session, payload)
            if final_audio.exists() or final_manifest.exists():
                raise FileExistsError("Allocated comparison output path already exists.")
            os.replace(audio_path, final_audio)
            moved_audio = True
            os.replace(manifest_path, final_manifest)
            moved_manifest = True
            audio_registration = replace(
                audio_prepared,
                relative_path=context.paths.relative_managed_path(final_audio),
            )
            manifest_registration = replace(
                manifest_prepared,
                relative_path=context.paths.relative_managed_path(final_manifest),
            )
            registered_audio = context.artifacts.register_in_session(
                session,
                final_audio,
                kind="audio",
                role="comparison_export.audio",
                session_id=str(payload["session_id"]),
                parent_ids=input_artifact_ids,
                metadata=audio_metadata,
                settings=settings,
                _prepared=audio_registration,
            )
            registered_manifest = context.artifacts.register_in_session(
                session,
                final_manifest,
                kind="json",
                role="comparison_export.manifest",
                session_id=str(payload["session_id"]),
                parent_ids=[*input_artifact_ids, registered_audio.id],
                metadata=manifest_metadata,
                settings=settings,
                _prepared=manifest_registration,
            )
            registered_audio.metadata_json = {
                **dict(registered_audio.metadata_json or {}),
                "manifest_artifact_id": registered_manifest.id,
            }
            _require_worker_claim(session, payload, cancel_event)
            revalidate_comparison_export_in_session(session, payload)
            session.expunge(registered_audio)
            session.expunge(registered_manifest)
        committed = True
    finally:
        if not committed:
            if moved_audio:
                final_audio.unlink(missing_ok=True)
            if moved_manifest:
                final_manifest.unlink(missing_ok=True)
    assert registered_audio is not None and registered_manifest is not None
    return registered_audio, registered_manifest


def render_comparison_export(
    context: ComparisonExportContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Worker callback: render selected takes only and publish audio plus manifest."""

    if payload.get("version") != 1:
        raise ValueError("The comparison export payload version is unsupported.")
    if payload.get("output_format") not in ("wav", "mp3"):
        raise ValueError("Comparison exports support WAV or MP3 output.")
    _cancel(cancel_event)
    resolved = _source_paths(context, payload, cancel_event)
    descriptors = [item[0] for item in resolved]
    source_paths = [item[2] for item in resolved]
    session_id = str(payload["session_id"])
    with context.database.session() as session:
        revalidate_comparison_export_in_session(session, payload)
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        output_dir = context.paths.sessions / session_record.storage_key / "comparison-exports"
    staging_root = output_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    output_format = str(payload["output_format"])
    gap_ms = int(payload["gap_ms"])
    with tempfile.TemporaryDirectory(prefix="comparison-export-", dir=staging_root) as temporary:
        scratch = Path(temporary)
        scratch_audio = scratch / f"comparison.{output_format}"
        scratch_manifest = scratch / "comparison.manifest.json"
        _cancel(cancel_event)
        sample_rate_hz, channels = preferred_pcm_format(
            source_paths[0], cancel_event=cancel_event
        )
        parts = [
            AudioAssemblyPart(
                path=source_path,
                expected_duration_ms=int(descriptor["duration_ms"]),
                silence_after_ms=(gap_ms if index < len(source_paths) - 1 else 0),
                label=str(descriptor["take_id"]),
            )
            for index, (descriptor, source_path) in enumerate(zip(descriptors, source_paths, strict=True))
        ]
        plan = build_audio_assembly_plan(
            parts,
            output_format=output_format,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        backend = resolve_assembly_backend()
        progress(0.03, "Preparing selected speech takes")
        result = assemble_audio_plan(
            plan,
            scratch_audio,
            backend=backend,
            work_dir=scratch,
            cancel_event=cancel_event,
            progress=lambda fraction, detail: progress(0.08 + 0.70 * fraction, detail),
        )
        _cancel(cancel_event)
        if (
            len(result.part_duration_ms) != len(descriptors)
            or len(result.part_start_frames) != len(descriptors)
            or len(result.part_end_frames) != len(descriptors)
            or result.sample_rate_hz <= 0
            or result.total_frames <= 0
            or not scratch_audio.is_file()
            or scratch_audio.is_symlink()
        ):
            raise ValueError("The comparison audio renderer returned an incomplete output.")

        progress(0.82, "Rechecking selected audio sources")
        for descriptor, source_path in zip(descriptors, source_paths, strict=True):
            _cancel(cancel_event)
            if sha256_file(source_path) != descriptor["artifact_content_hash"]:
                raise ValueError("A comparison source audio file changed while rendering.")

        audio_prepared = context.artifacts.prepare_registration(scratch_audio)
        if not audio_prepared.content_hash:
            raise ValueError("Comparison export output hash could not be calculated.")
        manifest_clips = [
            _manifest_clip(descriptor, result, index)
            for index, descriptor in enumerate(descriptors)
        ]
        manifest = {
            "version": 1,
            "kind": "speech_take_comparison",
            "session_id": session_id,
            "job_id": payload.get("_job_id"),
            "audio_source": "speech_take",
            "sound_mix_included": False,
            "sound_mix_exclusion": "The separately assembled sound mix is not included.",
            "output_format": output_format,
            "gap_ms": gap_ms,
            "duration_ms": result.duration_ms,
            "sample_rate_hz": result.sample_rate_hz,
            "channels": channels,
            "clip_count": len(descriptors),
            "selection_hash": payload["selection_hash"],
            "output_hash": audio_prepared.content_hash,
            "clips": manifest_clips,
        }
        scratch_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        settings = {
            "comparison_export_version": 1,
            "output_format": output_format,
            "gap_ms": gap_ms,
            "selection_hash": payload["selection_hash"],
            "audio_source": "speech_take",
            "sound_mix_included": False,
        }
        input_artifact_ids = list(dict.fromkeys(str(item["artifact_id"]) for item in descriptors))
        audio_metadata = {
            "comparison_export_version": 1,
            "selection_hash": payload["selection_hash"],
            "format": output_format,
            "duration_ms": result.duration_ms,
            "segment_count": len(descriptors),
            "audio_source": "speech_take",
            "sound_mix_included": False,
        }
        manifest_metadata = {
            "comparison_export_version": 1,
            "selection_hash": payload["selection_hash"],
            "audio_artifact_hash": audio_prepared.content_hash,
            "audio_source": "speech_take",
            "sound_mix_included": False,
        }
        progress(0.92, "Publishing comparison audio and manifest")
        audio_artifact, manifest_artifact = _publish_pair(
            context,
            payload=payload,
            audio_path=scratch_audio,
            manifest_path=scratch_manifest,
            output_dir=output_dir,
            input_artifact_ids=input_artifact_ids,
            settings=settings,
            expected_audio_hash=audio_prepared.content_hash,
            audio_metadata=audio_metadata,
            manifest_metadata=manifest_metadata,
            cancel_event=cancel_event,
        )
    progress(1.0, "Comparison export ready")
    return {
        "session_id": session_id,
        "output_artifact_id": audio_artifact.id,
        "manifest_artifact_id": manifest_artifact.id,
        "artifact_ids": {
            "audio": audio_artifact.id,
            "manifest": manifest_artifact.id,
        },
        "duration_ms": result.duration_ms,
        "clip_count": len(descriptors),
        "format": output_format,
        "audio_source": "speech_take",
        "sound_mix_included": False,
        "selection_hash": payload["selection_hash"],
    }


__all__ = ["ComparisonExportContext", "render_comparison_export"]
