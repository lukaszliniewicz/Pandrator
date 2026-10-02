"""Automatic narration synthesis and WAV assembly with explicit dependencies."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .generation_plan_store import DefaultSilenceProtocol
from .models import Artifact, GenerationPlan, GenerationSegment, new_id, utcnow
from .workflow_generation_automatic_output import (
    AutomaticAudioOutput,
    AutomaticTakePublication,
    assemble_automatic_output,
    publish_automatic_take,
)
from .workflow_generation_binding import GenerationPlanStoreProtocol
from .workflow_generation_protocols import (
    ApplySegmentTtsOverridesProtocol,
    EnsureQwenVoiceProtocol,
    HydrateTtsSettingsProtocol,
    NegotiatedTtsBatchProtocol,
    OptimizeGenerationTextsProtocol,
    Progress,
    RecordTtsUsageProtocol,
    StreamingTtsBatchProtocol,
)

if TYPE_CHECKING:
    from pydub import AudioSegment

    from pandrator.runtime import DataPaths

    from .artifacts import ArtifactService
    from .database import Database
    from .manager_proxy import LocalManagerProxy
    from .tts_providers import TtsProviderRegistry


@dataclass(frozen=True, slots=True)
class AutomaticGenerationContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    manager_bridge: LocalManagerProxy | None
    tts_providers: TtsProviderRegistry
    _session_dir: Callable[[str], Path]
    _operation_dir: Callable[[str, str], Path]
    _store_generation_plan: GenerationPlanStoreProtocol
    _usable_language: Callable[[Any], str]
    _optimize_generation_texts: OptimizeGenerationTextsProtocol
    _tts_urls: Callable[[dict[str, Any]], dict[str, str]]
    _negotiated_tts_batch_size: NegotiatedTtsBatchProtocol
    prepare_audio_cpp_voice_reference: Callable[[dict[str, Any]], dict[str, Any]]
    _ensure_qwen_cloned_voice: EnsureQwenVoiceProtocol
    _start_streaming_tts_batch: StreamingTtsBatchProtocol
    _verification_metadata: Callable[[AudioSegment, str, dict[str, Any]], dict[str, Any] | None]
    _record_tts_usage: RecordTtsUsageProtocol
    _is_subtitle_generation_record: Callable[[dict[str, Any]], bool]
    _hydrate_tts_settings: HydrateTtsSettingsProtocol
    _apply_segment_tts_overrides: ApplySegmentTtsOverridesProtocol
    _secret_free_tts_settings: Callable[[dict[str, Any]], dict[str, Any]]
    _default_silence_after_ms: DefaultSilenceProtocol
    _logger: logging.Logger


def generate_audio(
    context: AutomaticGenerationContext,
    session_id: str,
    source_artifact: Artifact,
    source_path: Path,
    settings: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
    *,
    role: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    from .media_process import MediaProcessCancelled

    settings = context._hydrate_tts_settings(
        context.database,
        context.paths,
        settings,
        manager_bridge=context.manager_bridge,
    )

    if source_path.suffix.lower() != ".json":
        raise ValueError(
            "Audio generation requires segmented narration. Run Segment narration first."
        )
    try:
        records = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise ValueError(
            "The segmented narration artifact is invalid JSON. Run Segment narration again."
        ) from error
    if not isinstance(records, list) or not records:
        raise ValueError("No narration segments were found.")
    records = [
        record
        for record in records
        if str(record.get("text") or record.get("original_sentence") or "").strip()
    ]
    if not records:
        raise ValueError("No non-empty narration segments were found.")
    assembly_inputs: list[tuple[Path, int, int]] = []
    take_artifact_ids: list[str] = []
    revision_id = ""
    generation_segment_ids: list[str] = []
    if source_artifact.role == "prepared_text":
        with context.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == session_id
                )
            )
            if plan and plan.active_revision_id:
                segments = list(
                    session.scalars(
                        select(GenerationSegment)
                        .where(
                            GenerationSegment.plan_revision_id
                            == plan.active_revision_id,
                            GenerationSegment.removed.is_(False),
                        )
                        .order_by(GenerationSegment.ordinal)
                    ).all()
                )
                if segments:
                    revision_id = plan.active_revision_id
                    generation_segment_ids = [segment.id for segment in segments]
                    records = [
                        {
                            "text": segment.text,
                            "language": segment.language,
                            "voice": segment.voice,
                            "speaker": segment.speaker,
                            "node_kind": segment.node_kind,
                            "paragraph_break_after": segment.paragraph_break_after,
                            "silence_after_ms": segment.silence_after_ms,
                            "source_segment_ids": segment.source_segment_ids_json,
                        }
                        for segment in segments
                    ]
    if not revision_id:
        revision_id, generation_segment_ids = context._store_generation_plan(
            session_id,
            records,
            settings=settings,
            source_revision_id=str(
                (source_artifact.metadata_json or {}).get("revision_id") or ""
            )
            or None,
            source_artifact_id=source_artifact.id,
        )
    source_texts = [
        str(record.get("text") or record.get("original_sentence") or "").strip()
        for record in records
    ]
    from .speech_plan_workspace import (
        freeze_speech_snapshot,
        frozen_semantic_contexts,
        segment_performance_settings,
    )

    performance_snapshot = {"tts": dict(settings)}
    with context.database.session() as session:
        freeze_speech_snapshot(session, revision_id, performance_snapshot)
    performance_contexts = frozen_semantic_contexts(performance_snapshot)
    if performance_snapshot.get("speech_plan_frozen"):
        settings = {**settings, **dict(performance_snapshot.get("text") or {})}
    optimization_share = 0.25 if bool(settings.get("llm_tts_optimization")) else 0.0
    if performance_snapshot.get("speech_plan_frozen"):
        # Even a disabled LLM optimizer can apply newly reviewed dictionary
        # substitutions. A frozen plan must use its accepted spoken layer
        # verbatim instead, just like the resumable generation runner.
        with context.database.session() as session:
            accepted = {
                item.id: item.optimized_text or item.text
                for item in session.scalars(select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == revision_id))
            }
        optimized_texts = [accepted[key] for key in generation_segment_ids]
        optimization_model = ""
        optimization_share = 0.0
    else:
        optimized_texts, optimization_model = context._optimize_generation_texts(
            session_id, generation_segment_ids, source_texts, settings, cancel_event,
            lambda value, detail=None: progress(float(value) * optimization_share, detail),
            job_id=job_id,
            source_artifact_id=source_artifact.id,
        )
    verified_qwen_voices: set[str] = set()
    tts_urls = context._tts_urls(settings)
    batch_results = None
    batch_contexts: dict[str, dict[str, Any]] = {}
    casting_enabled = bool(settings.get("casting_enabled"))
    batch_capabilities = None
    if casting_enabled:
        # A logical segment owns all of its cast parts and publishes one
        # take only after they have been rendered and concatenated.
        effective_batch_size = 1
    else:
        batch_capabilities = context.tts_providers.synthesis_capabilities(
            settings,
            **tts_urls,
        )
        effective_batch_size = context._negotiated_tts_batch_size(
            settings,
            tts_urls,
            capabilities=batch_capabilities,
        )
    parallel_synthesis_batch = bool(
        effective_batch_size > 1
        and batch_capabilities is not None
        and batch_capabilities.parallel_synthesis
    )
    if effective_batch_size > 1:
        batch_items: list[tuple[str, str, dict[str, Any]]] = []
        for record, generation_segment_id, synthesized_text in zip(
            records,
            generation_segment_ids,
            optimized_texts,
            strict=True,
        ):
            segment_tts_settings = context._apply_segment_tts_overrides(
                settings,
                language=context._usable_language(record.get("language")),
                voice=str(record.get("voice") or "").strip() or None,
            )
            segment_tts_settings = segment_performance_settings(
                segment_tts_settings, performance_snapshot, generation_segment_id,
                synthesized_text, contexts=performance_contexts,
            )
            if not casting_enabled:
                segment_tts_settings = context.prepare_audio_cpp_voice_reference(
                    segment_tts_settings
                )
                context._ensure_qwen_cloned_voice(
                    segment_tts_settings,
                    base_url=tts_urls["kobold_qwen_base_url"],
                    verified=verified_qwen_voices,
                    cancel_event=cancel_event,
                )
            batch_contexts[generation_segment_id] = {
                "settings": segment_tts_settings,
                "synthesized_text": synthesized_text,
            }
            batch_items.append(
                (
                    generation_segment_id,
                    synthesized_text,
                    segment_tts_settings,
                )
            )
        if batch_items:
            batch_results = context._start_streaming_tts_batch(
                batch_items,
                batch_size=effective_batch_size,
                tts_urls=tts_urls,
                cancel_event=cancel_event,
            )
            context._logger.info(
                "Using grouped %s-item TTS batches for %d automatic generation segments.",
                effective_batch_size,
                len(batch_items),
            )
    parallel_wave_error: Exception | None = None
    for index, (record, generation_segment_id) in enumerate(
        zip(records, generation_segment_ids, strict=True),
        start=1,
    ):
        if (
            parallel_wave_error is not None
            and (index - 1) % effective_batch_size == 0
        ):
            raise parallel_wave_error
        if cancel_event.is_set():
            return {}
        text = str(
            record.get("text") or record.get("original_sentence") or ""
        ).strip()
        if not text:
            continue
        synthesis_share = 1.0 - optimization_share
        progress(
            optimization_share + ((index - 1) / len(records)) * synthesis_share,
            f"Generating segment {index} of {len(records)}",
        )
        batch_context = batch_contexts.get(generation_segment_id)
        if batch_context is not None:
            synthesized_text = str(batch_context["synthesized_text"])
            segment_tts_settings = dict(batch_context["settings"])
        else:
            synthesized_text = optimized_texts[index - 1]
            segment_tts_settings = context._apply_segment_tts_overrides(
                settings,
                language=context._usable_language(record.get("language")),
                voice=str(record.get("voice") or "").strip() or None,
            )
            segment_tts_settings = segment_performance_settings(
                segment_tts_settings, performance_snapshot, generation_segment_id,
                synthesized_text, contexts=performance_contexts,
            )
            if not casting_enabled:
                segment_tts_settings = context.prepare_audio_cpp_voice_reference(
                    segment_tts_settings
                )
                context._ensure_qwen_cloned_voice(
                    segment_tts_settings,
                    base_url=tts_urls["kobold_qwen_base_url"],
                    verified=verified_qwen_voices,
                    cancel_event=cancel_event,
                )

        render_manifest: list[dict[str, Any]] = []

        def synthesize_request(
            *,
            text_to_synthesize: str = synthesized_text,
            settings_for_segment: dict[str, Any] = segment_tts_settings,
            segment_index: int = index,
            synthesis_progress_share: float = synthesis_share,
        ):
            return context.tts_providers.synthesize(
                text_to_synthesize,
                settings_for_segment,
                max_attempts=int(settings_for_segment.get("max_attempts") or 5),
                cancel_event=cancel_event,
                retry_callback=lambda attempt, total, delay: progress(
                    optimization_share
                    + ((segment_index - 1) / len(records))
                    * synthesis_progress_share,
                    f"Retrying segment {segment_index} ({attempt}/{total}) in {delay:.1f}s",
                ),
                recovery_callback=lambda cycle, total, timeout: progress(
                    optimization_share
                    + ((segment_index - 1) / len(records))
                    * synthesis_progress_share,
                    f"Waiting for Qwen3 TTS before segment {segment_index} ({cycle}/{total}, up to {timeout:.0f}s)",
                ),
                **tts_urls,
            )

        def synthesize_one(
            *,
            text_to_synthesize: str = synthesized_text,
            settings_for_segment: dict[str, Any] = segment_tts_settings,
            segment_index: int = index,
            synthesis_progress_share: float = synthesis_share,
            generation_segment_id: str = generation_segment_id,
            render_manifest: list[dict[str, Any]] = render_manifest,
        ):
            if casting_enabled:
                from .generation_cast_runtime import segment_render_parts
                from .generation_rendering import execute_render_parts

                parts = segment_render_parts(
                    settings_for_segment,
                    performance_snapshot,
                    generation_segment_id,
                    text_to_synthesize,
                )

                def render_part(
                    part_text: str, part_settings: dict[str, Any]
                ):
                    if cancel_event.is_set():
                        raise MediaProcessCancelled(
                            "Audio generation was canceled."
                        )
                    prepared = context.prepare_audio_cpp_voice_reference(
                        part_settings
                    )
                    context._ensure_qwen_cloned_voice(
                        prepared,
                        base_url=tts_urls["kobold_qwen_base_url"],
                        verified=verified_qwen_voices,
                        cancel_event=cancel_event,
                    )
                    audio_part = synthesize_request(
                        text_to_synthesize=part_text,
                        settings_for_segment=prepared,
                    )
                    if cancel_event.is_set():
                        raise MediaProcessCancelled(
                            "Audio generation was canceled."
                        )
                    return audio_part

                try:
                    assembled, manifest = execute_render_parts(
                        parts,
                        synthesize=render_part,
                        cancelled=cancel_event.is_set,
                    )
                except RuntimeError as error:
                    if cancel_event.is_set():
                        raise MediaProcessCancelled(
                            "Audio generation was canceled."
                        ) from error
                    raise
                if cancel_event.is_set():
                    raise MediaProcessCancelled("Audio generation was canceled.")
                render_manifest.extend(manifest)
                return assembled
            return context.tts_providers.synthesize(
                text_to_synthesize,
                settings_for_segment,
                max_attempts=int(settings_for_segment.get("max_attempts") or 5),
                cancel_event=cancel_event,
                retry_callback=lambda attempt, total, delay: progress(
                    optimization_share
                    + ((segment_index - 1) / len(records))
                    * synthesis_progress_share,
                    f"Retrying segment {segment_index} ({attempt}/{total}) in {delay:.1f}s",
                ),
                recovery_callback=lambda cycle, total, timeout: progress(
                    optimization_share
                    + ((segment_index - 1) / len(records))
                    * synthesis_progress_share,
                    f"Waiting for Qwen3 TTS before segment {segment_index} ({cycle}/{total}, up to {timeout:.0f}s)",
                ),
                **tts_urls,
            )

        if batch_results is not None and batch_context is not None:
            try:
                batch_result = next(batch_results)
            except StopIteration as error:
                raise RuntimeError(
                    "The grouped TTS batch ended before every segment completed."
                ) from error
            if batch_result.id != generation_segment_id:
                raise RuntimeError(
                    "The grouped TTS batch returned segments out of order."
                )
            if batch_result.error is not None:
                if parallel_synthesis_batch:
                    parallel_wave_error = parallel_wave_error or batch_result.error
                    with context.database.session() as session:
                        failed_segment = session.get(
                            GenerationSegment, generation_segment_id
                        )
                        if failed_segment is not None:
                            failed_segment.status = "failed"
                            failed_segment.updated_at = utcnow()
                    continue
                if not batch_result.error.retryable:
                    raise batch_result.error
                context._logger.warning(
                    "Grouped TTS batch failed for segment %s; retrying it through the ordinary synthesis path.",
                    generation_segment_id,
                )
                audio = synthesize_one()
            else:
                audio = batch_result.audio
        else:
            audio = synthesize_one()
        if audio is None:
            raise RuntimeError(f"Speech generation failed at segment {index}.")
        if casting_enabled and cancel_event.is_set():
            raise MediaProcessCancelled("Audio generation was canceled.")
        verification = context._verification_metadata(
            audio,
            synthesized_text,
            segment_tts_settings,
        )
        if casting_enabled and cancel_event.is_set():
            raise MediaProcessCancelled("Audio generation was canceled.")
        take_dir = (
            context._session_dir(session_id)
            / "generation"
            / revision_id
            / generation_segment_id
        )
        take_dir.mkdir(parents=True, exist_ok=True)
        sentence_path = take_dir / f"tts-{new_id()}.wav"
        with sentence_path.open("wb") as output:
            audio.export(output, format="wav")
        if casting_enabled and cancel_event.is_set():
            sentence_path.unlink(missing_ok=True)
            raise MediaProcessCancelled("Audio generation was canceled.")
        take_artifact, segment = publish_automatic_take(
            context.database,
            context.artifacts,
            AutomaticTakePublication(
                path=sentence_path,
                session_id=session_id,
                segment_id=generation_segment_id,
                source_artifact_id=source_artifact.id,
                stored_settings=context._secret_free_tts_settings(segment_tts_settings),
                usage_settings=segment_tts_settings,
                synthesized_text=synthesized_text,
                duration_ms=len(audio),
                job_id=job_id,
                metadata={
                    "generation_segment_id": generation_segment_id,
                    "kind": "tts",
                    "source_text": text,
                    "synthesized_text": synthesized_text,
                    "llm_optimized": synthesized_text != text,
                    "llm_model": optimization_model or None,
                    **(
                        {"render_parts": render_manifest}
                        if render_manifest
                        else {}
                    ),
                    **(
                        {"audio_verification": verification}
                        if verification is not None
                        else {}
                    ),
                },
                verification=verification,
            ),
            record_tts_usage=context._record_tts_usage,
        )
        take_artifact_ids.append(take_artifact.id)
        silence_after = context._default_silence_after_ms(
            record,
            settings,
            is_subtitle=source_artifact.role == "speech_blocks"
            or context._is_subtitle_generation_record(record),
        )
        if performance_snapshot.get("speech_boundaries"):
            from .speech_boundaries import assembly_pause

            silence_after = assembly_pause(segment, performance_snapshot)
        assembly_inputs.append((sentence_path, len(audio), silence_after))
        progress(
            optimization_share + (index / len(records)) * synthesis_share,
            f"Generated segment {index} of {len(records)}",
        )
    if not assembly_inputs:
        raise RuntimeError("The speech service returned no audio.")

    destination = context._operation_dir(session_id, "generate-audio") / (
        "dubbing_audio.wav" if role == "dubbing_audio" else "audiobook_audio.wav"
    )
    if parallel_wave_error is not None:
        raise parallel_wave_error

    return assemble_automatic_output(
        context.artifacts,
        AutomaticAudioOutput(
            session_id=session_id,
            source_artifact_id=source_artifact.id,
            role=role,
            destination=destination,
            settings=settings,
            assembly_inputs=assembly_inputs,
            take_artifact_ids=take_artifact_ids,
            segment_count=len(records),
            plan_revision_id=revision_id,
        ),
        progress,
        cancel_event,
    )
