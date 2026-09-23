"""Generation output assembly worker workflow."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import and_, func, select

from .artifacts import ArtifactService
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    OutputAssembly,
    Segment,
    utcnow,
)
from .output_settings_snapshot import build_output_settings_snapshot
from .workflow_output_context import OutputWorkflowContext

# Preserve the historical logger name for existing log filters and dashboards.
logger = logging.getLogger("pandrator.web.workflow_handlers")


def assemble_generation_output(
    context: OutputWorkflowContext,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Assemble the current selected takes in plan order into an immutable artifact."""
    import tempfile

    from pandrator.logic.dubbing.audio_sync import (
        align_audio_blocks,
        sentence_gap_ms_from_settings,
        slowdown_enabled_from_settings,
    )
    from pandrator.logic.dubbing.models import AudioAlignmentBlock

    from .audio_assembly import (
        AudioAssemblyPart,
        assemble_audio_plan,
        build_audio_assembly_plan,
        preferred_pcm_format,
        resolve_assembly_backend,
    )
    from .media_process import MediaProcessCancelled, probe_audio_stream

    assembly_id = str(payload.get("output_assembly_id") or "")
    destination: Path | None = None
    output_registered = False
    with context.database.session() as session:
        assembly_row = session.execute(
            select(OutputAssembly, GenerationPlan)
            .select_from(OutputAssembly)
            .outerjoin(
                GenerationPlan,
                and_(
                    OutputAssembly.generation_run_id.is_(None),
                    GenerationPlan.session_id == OutputAssembly.session_id,
                ),
            )
            .where(OutputAssembly.id == assembly_id)
        ).one_or_none()
        if assembly_row is None:
            raise KeyError(assembly_id)
        assembly, current_plan = assembly_row
        session_id = assembly.session_id
        settings_container = dict(assembly.settings_json or {})
        plan_revision_id = str(settings_container.get("plan_revision_id") or "")
        if (
            cancel_event.is_set()
            or assembly.status in {"stale", "canceled", "cancel_requested"}
            or (
                assembly.generation_run_id is None
                and (
                    current_plan is None
                    or str(current_plan.active_revision_id or "")
                    != plan_revision_id
                )
            )
        ):
            assembly.status = "canceled"
            assembly.error_message = None
            assembly.updated_at = utcnow()
            return {}
        assembly.status = "running"
        assembly.error_message = None
        assembly.updated_at = utcnow()
        resolved = settings_container.get("resolved")
        if not isinstance(resolved, dict):
            resolved = {}
        audio_settings = dict(resolved.get("audio") or {})
        output_settings = dict(resolved.get("output") or {})
        selected_run = (
            session.get(GenerationRun, assembly.generation_run_id)
            if assembly.generation_run_id
            else None
        )
        selected_run_sequence = (
            selected_run.sequence_number if selected_run else None
        )
        plan_revision = session.get(
            GenerationPlanRevision,
            plan_revision_id,
        )
        source_revision_id = (
            plan_revision.source_revision_id if plan_revision is not None else None
        )

    try:
        with context.database.session() as session:
            source_timings = []
            if source_revision_id:
                source_timings = [
                    (item.id, item.ordinal, item.start_ms, item.end_ms)
                    for item in session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == source_revision_id)
                        .order_by(Segment.ordinal)
                    ).all()
                ]
            if selected_run_sequence is not None:
                ranked_takes = (
                    select(
                        AudioTake.id.label("take_id"),
                        AudioTake.generation_segment_id.label("segment_id"),
                        func.row_number()
                        .over(
                            partition_by=AudioTake.generation_segment_id,
                            order_by=(
                                GenerationRun.sequence_number.desc(),
                                AudioTake.created_at.desc(),
                                AudioTake.id.desc(),
                            ),
                        )
                        .label("take_rank"),
                    )
                    .join(
                        GenerationRun,
                        AudioTake.generation_run_id == GenerationRun.id,
                    )
                    .where(
                        AudioTake.status.in_(("completed", "stale")),
                        GenerationRun.session_id == session_id,
                        GenerationRun.plan_revision_id == plan_revision_id,
                        GenerationRun.sequence_number <= selected_run_sequence,
                    )
                    .subquery()
                )
            else:
                ranked_takes = (
                    select(
                        AudioTake.id.label("take_id"),
                        AudioTake.generation_segment_id.label("segment_id"),
                        func.row_number()
                        .over(
                            partition_by=AudioTake.generation_segment_id,
                            order_by=(
                                AudioTake.created_at.desc(),
                                AudioTake.id.desc(),
                            ),
                        )
                        .label("take_rank"),
                    )
                    .join(
                        GenerationSegment,
                        AudioTake.generation_segment_id == GenerationSegment.id,
                    )
                    .where(
                        AudioTake.is_active.is_(True),
                        AudioTake.status == "completed",
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                    )
                    .subquery()
                )
            selected_rows = list(
                session.execute(
                    select(GenerationSegment, AudioTake, Artifact)
                    .outerjoin(
                        ranked_takes,
                        and_(
                            ranked_takes.c.segment_id == GenerationSegment.id,
                            ranked_takes.c.take_rank == 1,
                        ),
                    )
                    .outerjoin(
                        AudioTake,
                        AudioTake.id == ranked_takes.c.take_id,
                    )
                    .outerjoin(
                        Artifact,
                        Artifact.id == AudioTake.artifact_id,
                    )
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            selected: list[tuple[GenerationSegment, AudioTake, Artifact]] = []
            for segment, take, artifact in selected_rows:
                allowed_statuses = (
                    {"completed", "stale"}
                    if selected_run is not None
                    else {"completed"}
                )
                if (
                    take is None
                    or take.status not in allowed_statuses
                    or not take.artifact_id
                ):
                    if selected_run is None:
                        raise ValueError(
                            f"Segment {segment.ordinal + 1} has no current completed audio take."
                        )
                    raise ValueError(
                        f"Segment {segment.ordinal + 1} has no available audio take in Run {selected_run.sequence_number}."
                    )
                if artifact is None or artifact.state != "current":
                    raise ValueError(
                        f"Segment {segment.ordinal + 1} references an unavailable audio artifact."
                    )
                selected.append((segment, take, artifact))
        if not selected:
            raise ValueError(
                "No active generation segments are available for assembly."
            )

        loaded: list[tuple[GenerationSegment, AudioTake, Artifact, Path, int]] = []
        manifest: list[dict[str, Any]] = []
        chapter_markers: list[tuple[float, str]] = []
        parent_ids: list[str] = []
        for index, (segment, take, artifact) in enumerate(selected):
            if cancel_event.is_set():
                with context.database.session() as session:
                    assembly = session.get(OutputAssembly, assembly_id)
                    if assembly is not None:
                        assembly.status = "canceled"
                        assembly.error_message = None
                        assembly.updated_at = utcnow()
                return {}
            progress(
                0.2 * (index / len(selected)),
                f"Validating audio segment {index + 1} of {len(selected)}",
            )
            path = context.paths.managed_path(artifact.relative_path)
            if not path.is_file():
                raise ValueError(
                    f"Audio take file is missing for segment {segment.ordinal + 1}."
                )
            duration_ms = int(
                take.duration_ms
                or (artifact.metadata_json or {}).get("duration_ms")
                or 0
            )
            if duration_ms <= 0:
                duration_ms = probe_audio_stream(
                    path, cancel_event=cancel_event
                ).duration_ms
            loaded.append((segment, take, artifact, path, duration_ms))
            parent_ids.append(artifact.id)
        progress(0.2, f"Validated {len(loaded)} audio segments")

        source_timing_by_ref: dict[str, tuple[int, int, int]] = {}
        for source_id, ordinal, start_ms, end_ms in source_timings:
            if start_ms is None or end_ms is None:
                continue
            timing = (int(start_ms), int(end_ms), int(ordinal) + 1)
            source_timing_by_ref[str(source_id)] = timing
            source_timing_by_ref[str(int(ordinal) + 1)] = timing
        subtitle_timed = bool(source_timing_by_ref) and any(
            segment.node_kind == "subtitle_cue" for segment, *_rest in loaded
        )
        alignment_diagnostics: dict[str, Any] = {
            "mode": "sequential",
            "block_count": len(loaded),
            "speed_adjusted_block_count": 0,
        }

        session_record = context._session_record(session_id)
        output_format = str(output_settings.get("format") or "wav").lower()
        if session_record.workflow_kind != "audiobook" and output_format == "m4b":
            output_format = "wav"
        bitrate = str(output_settings.get("bitrate") or "192k")
        assemblies_dir = context._session_dir(session_id) / "assemblies"
        assemblies_dir.mkdir(parents=True, exist_ok=True)
        destination = assemblies_dir / f"assembly-{assembly_id}.{output_format}"
        backend = resolve_assembly_backend()
        fade_enabled = bool(
            audio_settings.get(
                "fade_enabled", audio_settings.get("enable_fade", False)
            )
        )
        fade_in_ms = (
            max(
                0,
                int(
                    audio_settings.get(
                        "fade_in_ms",
                        audio_settings.get("fade_in_duration", 0),
                    )
                    or 0
                ),
            )
            if fade_enabled
            else 0
        )
        fade_out_ms = (
            max(
                0,
                int(
                    audio_settings.get(
                        "fade_out_ms",
                        audio_settings.get("fade_out_duration", 0),
                    )
                    or 0
                ),
            )
            if fade_enabled
            else 0
        )
        sample_rate_hz, channels = preferred_pcm_format(
            loaded[0][3],
            cancel_event=cancel_event,
        )

        if subtitle_timed:
            alignment_blocks: list[AudioAlignmentBlock] = []
            previous_alignment_group: str | None = None
            with tempfile.TemporaryDirectory(
                prefix=f".assembly-{assembly_id}-",
                dir=assemblies_dir,
            ) as temporary:
                temporary_path = Path(temporary)
                for index, (
                    segment,
                    take,
                    artifact,
                    source_path,
                    duration_ms,
                ) in enumerate(loaded):
                    timings = sorted(
                        {
                            source_timing_by_ref[str(reference)]
                            for reference in segment.source_segment_ids_json
                            if str(reference) in source_timing_by_ref
                        },
                        key=lambda value: (value[0], value[1], value[2]),
                    )
                    if not timings:
                        raise ValueError(
                            f"Subtitle generation segment {segment.ordinal + 1} has no source timing references. Regenerate its speech-block plan."
                        )
                    input_path = temporary_path / f"segment-{index + 1:06d}.wav"
                    segment_plan = build_audio_assembly_plan(
                        [
                            AudioAssemblyPart(
                                path=source_path,
                                expected_duration_ms=duration_ms,
                                fade_in_ms=fade_in_ms,
                                fade_out_ms=fade_out_ms,
                            )
                        ],
                        output_format="wav",
                        sample_rate_hz=sample_rate_hz,
                        channels=channels,
                    )
                    segment_result = assemble_audio_plan(
                        segment_plan,
                        input_path,
                        backend=backend,
                        work_dir=temporary_path,
                        cancel_event=cancel_event,
                    )
                    block = AudioAlignmentBlock(
                        number=str(index + 1).zfill(4),
                        text=segment.text,
                        start_ms=timings[0][0],
                        end_ms=timings[-1][1],
                        audio_files=[input_path],
                        subtitles=[value[2] for value in timings],
                    )
                    alignment_group = (
                        str(segment.alignment_group or "").strip() or None
                    )
                    same_explicit_group = bool(
                        alignment_blocks
                        and alignment_group
                        and alignment_group == previous_alignment_group
                    )
                    legacy_shared_boundary = bool(
                        alignment_blocks
                        and not alignment_group
                        and not previous_alignment_group
                        and alignment_blocks[-1].subtitles[-1:]
                        == block.subtitles[:1]
                    )
                    if same_explicit_group or legacy_shared_boundary:
                        previous = alignment_blocks[-1]
                        alignment_blocks[-1] = AudioAlignmentBlock(
                            number=f"{previous.number}-{block.number}",
                            text=f"{previous.text} {block.text}".strip(),
                            start_ms=previous.start_ms,
                            end_ms=max(previous.end_ms, block.end_ms),
                            audio_files=[*previous.audio_files, *block.audio_files],
                            subtitles=sorted(
                                {*previous.subtitles, *block.subtitles}
                            ),
                        )
                    else:
                        alignment_blocks.append(block)
                    previous_alignment_group = alignment_group
                    manifest.append(
                        {
                            "segment_id": segment.id,
                            "segment_revision": segment.revision,
                            "node_kind": segment.node_kind,
                            "speaker": segment.speaker,
                            "alignment_group": alignment_group,
                            "take_id": take.id,
                            "take_revision": take.revision,
                            "artifact_id": artifact.id,
                            "kind": take.kind,
                            "duration_ms": segment_result.part_duration_ms[0],
                            "silence_after_ms": 0,
                            "target_start_ms": timings[0][0],
                            "target_end_ms": timings[-1][1],
                            "source_subtitles": [value[2] for value in timings],
                        }
                    )
                    progress(
                        0.2 + 0.25 * ((index + 1) / len(loaded)),
                        f"Prepared timing block {index + 1} of {len(loaded)}",
                    )
                raw_speed = float(
                    audio_settings.get("synchronization_speed") or 1.0
                )
                speed_up_percent = round(
                    raw_speed * 100 if raw_speed <= 10 else raw_speed
                )
                logger.info(
                    "Assembling %s with subtitle timing: blocks=%d max_speed=%.3fx max_delay=%dms sentence_gap=%dms slowdown=%s",
                    assembly_id,
                    len(alignment_blocks),
                    max(1.0, speed_up_percent / 100.0),
                    max(
                        0,
                        int(audio_settings.get("synchronization_delay_ms") or 0),
                    ),
                    sentence_gap_ms_from_settings(audio_settings),
                    slowdown_enabled_from_settings(audio_settings),
                )
                progress(
                    0.48,
                    f"Synchronizing {len(alignment_blocks)} speech blocks",
                )
                aligned_path = Path(
                    align_audio_blocks(
                        alignment_blocks,
                        temporary_path,
                        delay_start_ms=max(
                            0,
                            int(
                                audio_settings.get("synchronization_delay_ms") or 0
                            ),
                        ),
                        speed_up_percent=max(100, speed_up_percent),
                        allow_slowdown=slowdown_enabled_from_settings(audio_settings),
                        sentence_gap_ms=sentence_gap_ms_from_settings(audio_settings),
                        output_path=temporary_path / "aligned.wav",
                        diagnostics=alignment_diagnostics,
                        backend=backend,
                        cancel_event=cancel_event,
                    )
                )
                aligned_duration_ms = probe_audio_stream(
                    aligned_path,
                    cancel_event=cancel_event,
                ).duration_ms
                output_plan = build_audio_assembly_plan(
                    [
                        AudioAssemblyPart(
                            path=aligned_path,
                            expected_duration_ms=aligned_duration_ms,
                        )
                    ],
                    output_format=output_format,
                    bitrate=bitrate,
                    sample_rate_hz=sample_rate_hz,
                    channels=channels,
                )
                assembly_result = assemble_audio_plan(
                    output_plan,
                    destination,
                    backend=backend,
                    work_dir=temporary_path,
                    cancel_event=cancel_event,
                    progress=lambda fraction, detail: progress(
                        0.62 + 0.2 * fraction,
                        detail,
                    ),
                )
            progress(0.82, "Subtitle-timed audio synchronized")
            logger.info(
                "Assembly %s synchronization applied speed-up to %d/%d blocks (max effective %.3fx, final drift %dms)",
                assembly_id,
                int(alignment_diagnostics.get("speed_adjusted_block_count") or 0),
                int(alignment_diagnostics.get("block_count") or 0),
                float(
                    alignment_diagnostics.get("max_effective_speed_factor") or 1.0
                ),
                int(alignment_diagnostics.get("final_drift_ms") or 0),
            )
        else:
            from .speech_boundaries import assembly_pause

            planned_parts: list[AudioAssemblyPart] = []
            planned_chapters: list[tuple[int, str]] = []
            for index, (
                segment,
                _take,
                _artifact,
                source_path,
                duration_ms,
            ) in enumerate(loaded):
                silence_after_ms = (
                    assembly_pause(segment, resolved)
                    if index < len(loaded) - 1
                    else 0
                )
                planned_parts.append(
                    AudioAssemblyPart(
                        path=source_path,
                        expected_duration_ms=duration_ms,
                        silence_after_ms=silence_after_ms,
                        fade_in_ms=fade_in_ms,
                        fade_out_ms=fade_out_ms,
                        label=segment.id,
                    )
                )
                if segment.node_kind == "chapter_marker":
                    planned_chapters.append((index, segment.text))
            output_plan = build_audio_assembly_plan(
                planned_parts,
                output_format=output_format,
                bitrate=bitrate,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
                chapters=planned_chapters,
            )
            assembly_result = assemble_audio_plan(
                output_plan,
                destination,
                backend=backend,
                work_dir=assemblies_dir,
                cancel_event=cancel_event,
                progress=lambda fraction, detail: progress(
                    0.2 + 0.62 * fraction,
                    detail,
                ),
            )
            chapter_markers = [
                (start_ms / 1000, chapter.title)
                for chapter, start_ms in zip(
                    output_plan.chapters,
                    assembly_result.chapter_starts_ms,
                    strict=True,
                )
            ]
            for index, (
                segment,
                take,
                artifact,
                _source_path,
                _duration_ms,
            ) in enumerate(loaded):
                manifest.append(
                    {
                        "segment_id": segment.id,
                        "segment_revision": segment.revision,
                        "node_kind": segment.node_kind,
                        "speaker": segment.speaker,
                        "take_id": take.id,
                        "take_revision": take.revision,
                        "artifact_id": artifact.id,
                        "kind": take.kind,
                        "duration_ms": assembly_result.part_duration_ms[index],
                        "silence_after_ms": planned_parts[index].silence_after_ms,
                    }
                )

        progress(0.9, "Applying output metadata")
        metadata: dict[str, str] = {}
        cover_artifact_id = ""
        cover_path = None
        if session_record.workflow_kind == "audiobook":
            metadata = {
                "title": str(output_settings.get("title") or session_record.name),
                "artist": str(output_settings.get("artist") or ""),
                "album": str(output_settings.get("album") or ""),
                "genre": str(output_settings.get("genre") or ""),
                "language": str(output_settings.get("language") or ""),
            }
            cover_artifact_id = str(
                output_settings.get("cover_artifact_id") or ""
            ).strip()
            if cover_artifact_id:
                cover_artifact, candidate = context._resolve_input(cover_artifact_id)
                if (
                    cover_artifact.state != "current"
                    or not candidate.is_file()
                    or not str(cover_artifact.mime_type or "").startswith("image/")
                ):
                    raise ValueError(
                        "The selected cover artifact is not an available image."
                    )
                cover_path = candidate
                parent_ids.append(cover_artifact.id)
            from pandrator.logic.audio_processor import (
                _add_chapters_to_m4b,
                _save_metadata_and_cover,
            )

            _save_metadata_and_cover(
                str(destination),
                output_format,
                metadata,
                str(cover_path) if cover_path else None,
                raise_on_error=True,
            )
            if output_format == "m4b" and chapter_markers:
                _add_chapters_to_m4b(
                    str(destination),
                    chapter_markers,
                    total_duration_sec=assembly_result.duration_ms / 1000,
                    raise_on_error=True,
                )
                _save_metadata_and_cover(
                    str(destination),
                    output_format,
                    metadata,
                    str(cover_path) if cover_path else None,
                    raise_on_error=True,
                )
        progress(0.96, "Registering assembled audio")
        output_settings_snapshot = build_output_settings_snapshot(
            {},
            {
                "audio": audio_settings,
                "output": output_settings,
            },
        )
        if cancel_event.is_set():
            raise MediaProcessCancelled("Output assembly was canceled.")
        artifact = context.artifacts.register(
            destination,
            kind="audio",
            role="assembled_audio",
            session_id=session_id,
            parent_ids=parent_ids,
            settings={
                "audio": audio_settings,
                "output": output_settings,
                "takes": manifest,
            },
            metadata={
                "output_assembly_id": assembly_id,
                "duration_ms": assembly_result.duration_ms,
                "segment_count": len(selected),
                "format": output_format,
                "bitrate": bitrate,
                "assembly_backend": assembly_result.backend,
                "metadata": metadata,
                "cover_artifact_id": cover_artifact_id or None,
                "chapters": [
                    {"start_ms": int(start * 1000), "title": title}
                    for start, title in chapter_markers
                ],
                "takes": manifest,
                "synchronization": alignment_diagnostics,
                "output_settings": output_settings_snapshot,
            },
        )
        output_registered = True
        invalidated = False
        with context.database.immediate_session() as session:
            assembly = session.get(OutputAssembly, assembly_id)
            if assembly is None:
                raise KeyError(assembly_id)
            current_plan = (
                session.scalar(
                    select(GenerationPlan).where(
                        GenerationPlan.session_id == session_id
                    )
                )
                if assembly.generation_run_id is None
                else None
            )
            invalidated = (
                cancel_event.is_set()
                or assembly.status in {"stale", "canceled", "cancel_requested"}
                or (
                    assembly.generation_run_id is None
                    and (
                        current_plan is None
                        or str(current_plan.active_revision_id or "")
                        != plan_revision_id
                    )
                )
            )
            assembly.artifact_id = artifact.id
            if invalidated:
                stored_artifact = session.get(Artifact, artifact.id)
                if stored_artifact is not None:
                    stored_artifact.state = "stale"
                    ArtifactService._mark_descendants_stale(
                        session, stored_artifact.id
                    )
                assembly.status = "canceled"
                assembly.error_message = None
            else:
                assembly.status = "completed"
                assembly.error_message = None
                assembly.settings_json = {
                    **dict(assembly.settings_json or {}),
                    "takes": manifest,
                    "duration_ms": assembly_result.duration_ms,
                    "assembly_backend": assembly_result.backend,
                    "synchronization": alignment_diagnostics,
                }
            assembly.updated_at = utcnow()
        if invalidated:
            return {}
        progress(1.0, "Output assembly ready")
        return {
            "output_assembly_id": assembly_id,
            "artifact_id": artifact.id,
            "duration_ms": assembly_result.duration_ms,
            "segment_count": len(selected),
            "format": output_format,
            "assembly_backend": assembly_result.backend,
            "synchronization": {
                key: value
                for key, value in alignment_diagnostics.items()
                if key != "blocks"
            },
        }
    except MediaProcessCancelled:
        if destination is not None and not output_registered:
            destination.unlink(missing_ok=True)
        with context.database.session() as session:
            assembly = session.get(OutputAssembly, assembly_id)
            if assembly is not None:
                assembly.status = "canceled"
                assembly.error_message = None
                assembly.updated_at = utcnow()
        return {}
    except Exception as error:
        if destination is not None and not output_registered:
            destination.unlink(missing_ok=True)
        with context.database.session() as session:
            assembly = session.get(OutputAssembly, assembly_id)
            if assembly is not None:
                assembly.status = "failed"
                assembly.error_message = str(error)
                assembly.updated_at = utcnow()
        raise
