"""Resumable per-segment generation with explicit dependencies."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from .jobs import JobQueue
from .models import Artifact, AudioTake, GenerationRun, GenerationSegment, new_id, utcnow
from .workflow_generation_protocols import (
    ApplySegmentTtsOverridesProtocol,
    EnsureQwenVoiceProtocol,
    HydrateTtsSettingsProtocol,
    NegotiatedTtsBatchProtocol,
    OptimizeGenerationTextsProtocol,
    Progress,
    StreamingTtsBatchProtocol,
    TtsUsageEventProtocol,
    VoiceoverSecondPassProtocol,
)

if TYPE_CHECKING:
    from pydub import AudioSegment

    from pandrator.runtime import DataPaths

    from .artifacts import ArtifactService
    from .database import Database
    from .manager_proxy import LocalManagerProxy
    from .models import SessionRecord
    from .tts_providers import TtsProviderRegistry


@dataclass(frozen=True, slots=True)
class GenerationExecutionContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    manager_bridge: LocalManagerProxy | None
    tts_providers: TtsProviderRegistry
    _session_record: Callable[[str], SessionRecord]
    _session_dir: Callable[[str], Path]
    _usable_language: Callable[[Any], str]
    _optimize_generation_texts: OptimizeGenerationTextsProtocol
    _tts_urls: Callable[[dict[str, Any]], dict[str, str]]
    _negotiated_tts_batch_size: NegotiatedTtsBatchProtocol
    prepare_audio_cpp_voice_reference: Callable[[dict[str, Any]], dict[str, Any]]
    _ensure_qwen_cloned_voice: EnsureQwenVoiceProtocol
    _start_streaming_tts_batch: StreamingTtsBatchProtocol
    _verification_metadata: Callable[[AudioSegment, str, dict[str, Any]], dict[str, Any] | None]
    _tts_usage_event: TtsUsageEventProtocol
    _resume_generation_after_regeneration: Callable[[str, str], str | None]
    _finalize_run_audio_verification: Callable[[str], int]
    _hydrate_tts_settings: HydrateTtsSettingsProtocol
    _apply_segment_tts_overrides: ApplySegmentTtsOverridesProtocol
    _apply_selected_segment_tts_override: Callable[
        [dict[str, Any], dict[str, Any] | None], dict[str, Any]
    ]
    _secret_free_tts_settings: Callable[[dict[str, Any]], dict[str, Any]]
    _voiceover_second_pass: VoiceoverSecondPassProtocol
    _logger: logging.Logger
    _repair_early_generation_blocks: Callable[[str, Progress, threading.Event], dict[str, Any]]
    _regroup_generation_blocks: Callable[[str, Progress, threading.Event], dict[str, Any]]


def _restore_optional_pass_status(database: Database, run_id: str, final_status: str) -> str:
    """Restore the first-pass result while retaining durable cancellation."""
    with database.immediate_session() as session:
        current = session.get(GenerationRun, run_id)
        if current is None:
            return final_status
        if current.cancel_requested or current.status in {"cancel_requested", "canceled"}:
            return current.status
        current.status = final_status
        return final_status


def run_generation(
    context: GenerationExecutionContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Generate immutable per-segment takes with safe pause and resume boundaries."""
    from pydub import AudioSegment

    from pandrator.logic import rvc_handler

    from .media_process import MediaProcessCancelled

    run_id = str(payload.get("generation_run_id") or "")
    selected_ids = {
        str(value) for value in (payload.get("segment_ids") or []) if str(value)
    }
    operation = str(payload.get("operation") or "generate")
    with context.database.session() as session:
        run = session.get(GenerationRun, run_id)
        if run is None:
            raise KeyError(run_id)
        output_run_id = str(run.output_generation_run_id or run.id)
        output_run = session.get(GenerationRun, output_run_id)
        if (
            output_run is None
            or output_run.session_id != run.session_id
            or output_run.plan_revision_id != run.plan_revision_id
            or (output_run.operation == "rvc" and output_run_id != run.id)
        ):
            raise ValueError(
                "The output generation run does not match this session and plan."
            )
        run.status = "running"
        run.updated_at = utcnow()
        settings_snapshot = dict(run.settings_snapshot_json or {})
        from .generation_audio_identity import plan_audio_identities
        from .speech_plan_workspace import plan_signature

        plan_changed = bool(settings_snapshot.get("speech_plan_signature") and settings_snapshot["speech_plan_signature"] != plan_signature(session, run.plan_revision_id))
        audio_identities = plan_audio_identities(session, run.plan_revision_id, settings_snapshot)
        audio_identity_changed = bool(settings_snapshot.get("generation_audio_identities") and settings_snapshot["generation_audio_identities"] != audio_identities)
        if plan_changed or audio_identity_changed:
            run.status = "failed"
        session_id = run.session_id
        plan_revision_id = run.plan_revision_id
        run_sequence_number = run.sequence_number
        job_id = run.job_id or str(payload.get("_job_id") or "") or None

    if plan_changed:
        raise ValueError("The selected speech plan changed after generation was queued. Review and submit it again.")
    if audio_identity_changed:
        raise ValueError("A voice reference changed after generation was queued. Start a new run to use the current voice consistently.")
    completion_progress = progress
    second_pass = context._voiceover_second_pass(
        settings_snapshot,
        operation=operation,
        has_selected_ids=bool(selected_ids),
        workflow_kind=context._session_record(session_id).workflow_kind,
    )
    # Passage-first planning never runs the old early split repair: a
    # split pass followed by a regroup pass could undo each other in a
    # loop. The optional regroup second pass runs in passage mode only.
    repair_requested = second_pass == "repair"
    regroup_requested = second_pass == "regroup"
    if repair_requested:
        def generation_progress(value, detail=None):
            completion_progress(0.85 * value, detail)

        progress = generation_progress
    elif regroup_requested:
        def regroup_generation_progress(value, detail=None):
            completion_progress(0.85 * value, detail)

        progress = regroup_generation_progress
    statement = (
        select(GenerationSegment)
        .where(
            GenerationSegment.plan_revision_id == plan_revision_id,
            GenerationSegment.removed.is_(False),
        )
        .order_by(GenerationSegment.ordinal)
    )
    if selected_ids:
        statement = statement.where(GenerationSegment.id.in_(selected_ids))
    with context.database.session() as session:
        selected_segments = list(session.scalars(statement).all())
        segment_ids = [item.id for item in selected_segments]
        segment_seeds = {
            item.id: {
                "text": item.text,
                "optimized_text": item.optimized_text,
                "speaker": item.speaker,
                "language": context._usable_language(item.language),
                "voice": str(item.voice or "").strip() or None,
                "status": item.status,
            }
            for item in selected_segments
        }
        from .generation_audio_identity import take_reuse_reason

        selected_by_id = {segment.id: segment for segment in selected_segments}
        completed_take_ids = {
            take.generation_segment_id
            for take, artifact in session.execute(
                select(AudioTake, Artifact).join(Artifact, Artifact.id == AudioTake.artifact_id).where(
                    AudioTake.generation_run_id == output_run_id,
                    AudioTake.status == "completed",
                    AudioTake.artifact_id.is_not(None),
                )
            )
            if take.generation_segment_id in selected_by_id
            and take_reuse_reason(selected_by_id[take.generation_segment_id], take, artifact,
                                  audio_identities[take.generation_segment_id]) == "reusable"
        }
    if not segment_ids:
        with context.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is not None:
                run.status = "failed"
                run.updated_at = utcnow()
        raise ValueError("No generation segments match this request.")

    from .settings_policy import adapt_runtime_settings
    from .workspace import mark_output_assemblies_stale

    tts_settings = {
        **adapt_runtime_settings("tts", dict(settings_snapshot.get("tts") or {})),
        **adapt_runtime_settings(
            "audio", dict(settings_snapshot.get("audio") or {})
        ),
    }
    selected_segment_override = dict(
        settings_snapshot.get("selected_segment_override") or {}
    )
    selected_tts_override = dict(selected_segment_override.get("tts") or {})
    selected_rvc_override = dict(selected_segment_override.get("rvc") or {})
    tts_settings = context._hydrate_tts_settings(
        context.database,
        context.paths,
        tts_settings,
        manager_bridge=context.manager_bridge,
    )
    selected_tts_runtime: dict[str, Any] | None = None
    if selected_tts_override:
        # Alternate settings are a small UI payload, not a complete TTS
        # configuration.  Adapt and hydrate them against the effective
        # run settings so a catalogue/custom provider retains its saved
        # endpoint and credential metadata.  Keep this copy run-local:
        # persistent/session settings must remain unchanged.
        selected_tts_runtime = context._apply_selected_segment_tts_override(
            tts_settings,
            selected_tts_override,
        )
        explicit_endpoint_keys = {
            "audio_cpp_base_url",
            "openai_audio_endpoint",
            "xtts_base_url",
            "voxcpm_base_url",
            "fishs2_base_url",
            "voxtral_base_url",
            "kokoro_base_url",
            "silero_base_url",
            "chatterbox_base_url",
            "kobold_qwen_base_url",
            "magpie_base_url",
        }
        explicit_endpoints = {
            key: deepcopy(selected_tts_override[key])
            for key in explicit_endpoint_keys
            if key in selected_tts_override
        }
        selected_tts_runtime = adapt_runtime_settings(
            "tts",
            selected_tts_runtime,
        )
        # ``adapt_runtime_settings`` fills service-derived endpoint aliases
        # for normal catalogue choices.  An alternate payload that names
        # an endpoint explicitly is authoritative, however.
        selected_tts_runtime.update(explicit_endpoints)
        selected_tts_runtime = context._hydrate_tts_settings(
            context.database,
            context.paths,
            selected_tts_runtime,
            manager_bridge=context.manager_bridge,
        )
        selected_tts_runtime.update(explicit_endpoints)

    def _assert_current_audio_identity(segment_id: str) -> None:
        from .generation_audio_identity import AudioIdentityContext

        with context.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            if segment is None or AudioIdentityContext(session, settings_snapshot).for_segment(segment) != audio_identities[segment_id]:
                raise ValueError("A voice reference or segment delivery setting changed during generation. Start a new run to keep its audio consistent.")

    def _tts_settings_for_segment(
        segment_settings: dict[str, Any],
        *,
        language: str,
        voice: str | None,
    ) -> dict[str, Any]:
        if selected_tts_runtime is None:
            return context._apply_selected_segment_tts_override(
                segment_settings,
                selected_tts_override,
            )
        resolved = deepcopy(selected_tts_runtime)
        if not str(
            selected_tts_override.get("language")
            or selected_tts_override.get("target_language")
            or ""
        ).strip():
            resolved = context._apply_segment_tts_overrides(
                resolved,
                language=language,
            )
        if not str(
            selected_tts_override.get("voice")
            or selected_tts_override.get("speaker")
            or ""
        ).strip():
            resolved = context._apply_segment_tts_overrides(
                resolved,
                voice=voice,
            )
        return resolved

    text_settings = adapt_runtime_settings(
        "text", dict(settings_snapshot.get("text") or {})
    )
    optimization_model = ""
    optimized_by_id: dict[str, str] = {
        key: value["optimized_text"] or value["text"] for key, value in segment_seeds.items()
    } if settings_snapshot.get("speech_plan_frozen") else {}
    optimization_share = (
        0.2
        if operation != "rvc" and bool(text_settings.get("llm_tts_optimization"))
        else 0.0
    )
    if operation != "rvc" and not settings_snapshot.get("speech_plan_frozen"):
        try:
            alternate_pronunciation_settings = None
            alternate_pronunciation_language = None
            alternate_pronunciation_voice_language = None
            if selected_tts_runtime is not None:
                # Only copy provider identity and the explicit language
                # choice into optimization.  The hydrated runtime may
                # contain API keys; it must remain confined to synthesis.
                alternate_pronunciation_settings = {
                    key: deepcopy(selected_tts_runtime[key])
                    for key in (
                        "service",
                        "tts_service",
                        "backend",
                        "openai_audio_endpoint",
                    )
                    if key in selected_tts_runtime
                }
                alternate_pronunciation_language = (
                    str(
                        selected_tts_override.get("language")
                        or selected_tts_override.get("target_language")
                        or ""
                    ).strip()
                    or None
                )
                alternate_pronunciation_voice_language = (
                    str(selected_tts_override.get("voice_language") or "").strip()
                    or None
                )
            optimization_segment_ids = [
                segment_id
                for segment_id in segment_ids
                if operation != "resume" or segment_id not in completed_take_ids
            ]
            if optimization_segment_ids:
                optimized, optimization_model = context._optimize_generation_texts(
                    session_id,
                    optimization_segment_ids,
                    [
                        segment_seeds[segment_id]["text"]
                        for segment_id in optimization_segment_ids
                    ],
                    {**text_settings, **tts_settings},
                    cancel_event,
                    lambda value, detail=None: progress(
                        float(value) * optimization_share, detail
                    ),
                    job_id=job_id,
                    generation_run_id=output_run_id,
                    source_artifact_id=str(settings_snapshot.get("source_artifact_id") or "") or None,
                    pronunciation_settings=alternate_pronunciation_settings,
                    pronunciation_language=alternate_pronunciation_language,
                    pronunciation_voice_language=alternate_pronunciation_voice_language,
                )
                optimized_by_id = dict(
                    zip(optimization_segment_ids, optimized, strict=True)
                )
        except Exception:
            with context.database.session() as session:
                run = session.get(GenerationRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.updated_at = utcnow()
            raise
    rvc_settings = adapt_runtime_settings(
        "rvc", dict(settings_snapshot.get("rvc") or {})
    )
    if selected_rvc_override:
        rvc_settings.update(adapt_runtime_settings("rvc", selected_rvc_override))
    rvc_source_sequence = None
    if operation == "rvc":
        source_run_id = str(rvc_settings.get("source_run_id") or "").strip()
        if source_run_id:
            with context.database.session() as session:
                source_run = session.get(GenerationRun, source_run_id)
                if (
                    source_run is None
                    or source_run.session_id != session_id
                    or source_run.plan_revision_id != plan_revision_id
                ):
                    raise ValueError(
                        "The selected source generation run is unavailable."
                    )
                rvc_source_sequence = source_run.sequence_number
    generated = 0
    skipped = 0
    verified_qwen_voices: set[str] = set()
    batch_results = None
    batch_contexts: dict[str, dict[str, Any]] = {}
    parallel_batch_boundaries: set[str] = set()
    parallel_synthesis_batch = False
    # The selected alternate owns the provider endpoint for this run.  In
    # particular, a first-class service's explicit base URL must reach the
    # legacy synthesis boundary instead of the source provider's URL.
    tts_urls = context._tts_urls(selected_tts_runtime or tts_settings)
    from .speech_plan_workspace import (
        frozen_semantic_contexts,
        segment_performance_settings,
    )

    performance_contexts = frozen_semantic_contexts(settings_snapshot)
    if operation != "rvc":
        # The selected-only setting set may switch provider.  Do not reuse
        # the source provider's streaming/batching capabilities for it.
        batch_capabilities = None
        if not selected_tts_override:
            batch_capabilities = context.tts_providers.synthesis_capabilities(
                tts_settings,
                **tts_urls,
            )
        effective_batch_size = (
            1
            if selected_tts_override
            else context._negotiated_tts_batch_size(
                tts_settings,
                tts_urls,
                capabilities=batch_capabilities,
            )
        )
        # A composite segment owns its internal requests and publishes one
        # take only after every voice part succeeds.
        if (selected_tts_runtime or tts_settings).get("casting_enabled"):
            effective_batch_size = 1
        if effective_batch_size > 1:
            batch_items: list[tuple[str, str, dict[str, Any]]] = []
            for segment_id in segment_ids:
                if operation == "resume" and segment_id in completed_take_ids:
                    continue
                seed = segment_seeds[segment_id]
                segment_tts_settings = context._apply_segment_tts_overrides(
                    tts_settings,
                    language=seed["language"],
                    voice=seed["voice"],
                )
                segment_tts_settings = _tts_settings_for_segment(
                    segment_tts_settings,
                    language=seed["language"],
                    voice=seed["voice"],
                )
                synthesized_text = optimized_by_id.get(
                    segment_id,
                    str(seed["text"]),
                )
                segment_tts_settings = segment_performance_settings(
                    segment_tts_settings, settings_snapshot, segment_id, synthesized_text,
                    contexts=performance_contexts,
                )
                segment_tts_settings = context.prepare_audio_cpp_voice_reference(
                    segment_tts_settings
                )
                context._ensure_qwen_cloned_voice(
                    segment_tts_settings,
                    base_url=tts_urls["kobold_qwen_base_url"],
                    verified=verified_qwen_voices,
                    cancel_event=cancel_event,
                )
                _assert_current_audio_identity(segment_id)
                batch_contexts[segment_id] = {
                    "text": str(seed["text"]),
                    "synthesized_text": synthesized_text,
                    "settings": segment_tts_settings,
                }
                batch_items.append(
                    (segment_id, synthesized_text, segment_tts_settings)
                )
            if batch_items:
                batch_results = context._start_streaming_tts_batch(
                    batch_items,
                    batch_size=effective_batch_size,
                    tts_urls=tts_urls,
                    cancel_event=cancel_event,
                )
                if (
                    batch_capabilities is not None
                    and batch_capabilities.parallel_synthesis
                ):
                    parallel_synthesis_batch = True
                    ordered_batch_ids = list(batch_contexts)
                    parallel_batch_boundaries = set(
                        ordered_batch_ids[::effective_batch_size]
                    )
                context._logger.info(
                    "Using grouped %s-item TTS batches for %d generation segments.",
                    effective_batch_size,
                    len(batch_items),
                )
    parallel_wave_error: Exception | None = None
    for index, segment_id in enumerate(segment_ids):
        if (
            parallel_wave_error is not None
            and segment_id in parallel_batch_boundaries
        ):
            raise parallel_wave_error
        with context.database.session() as session:
            run = session.get(GenerationRun, run_id)
            segment = session.get(GenerationSegment, segment_id)
            if run is None:
                raise KeyError(run_id)
            if run.cancel_requested or cancel_event.is_set():
                run.status = "canceled"
                run.updated_at = utcnow()
                return {
                    "generation_run_id": run_id,
                    "status": "canceled",
                    "generated": generated,
                }
            if run.pause_requested and (
                not parallel_batch_boundaries
                or segment_id in parallel_batch_boundaries
            ):
                run.status = "paused"
                run.updated_at = utcnow()
                return {
                    "generation_run_id": run_id,
                    "status": "paused",
                    "generated": generated,
                }
            if operation == "resume" and segment_id in completed_take_ids:
                skipped += 1
                progress(
                    optimization_share
                    + ((index + 1) / len(segment_ids)) * (1.0 - optimization_share),
                    f"Kept completed segment {index + 1} of {len(segment_ids)}",
                )
                continue
            if segment is None:
                raise KeyError(segment_id)
            segment.status = "running"
            segment.updated_at = utcnow()
            text = segment.text
            segment_speaker = segment.speaker
            segment_language = context._usable_language(segment.language)
            segment_voice = str(segment.voice or "").strip() or None
        progress(
            optimization_share
            + (index / len(segment_ids)) * (1.0 - optimization_share),
            f"Generating segment {index + 1} of {len(segment_ids)}",
        )
        take_path: Path | None = None
        take_committed = False
        take_parent_ids: list[str] = []
        render_manifest: list[dict[str, Any]] = []
        cast_render = operation != "rvc" and bool(
            (selected_tts_runtime or tts_settings).get("casting_enabled")
        )
        try:
            synthesized_text = text
            if operation == "rvc":
                with context.database.session() as session:
                    source_take = session.scalar(
                        select(AudioTake)
                        .join(
                            GenerationRun,
                            AudioTake.generation_run_id == GenerationRun.id,
                        )
                        .where(
                            AudioTake.generation_segment_id == segment_id,
                            AudioTake.status.in_(("completed", "stale")),
                            GenerationRun.session_id == session_id,
                            GenerationRun.plan_revision_id == plan_revision_id,
                            GenerationRun.sequence_number
                            <= (
                                rvc_source_sequence
                                if rvc_source_sequence is not None
                                else run_sequence_number - 1
                            ),
                        )
                        .order_by(
                            GenerationRun.sequence_number.desc(),
                            AudioTake.created_at.desc(),
                        )
                    )
                    if source_take is None:
                        source_take = session.scalar(
                            select(AudioTake)
                            .where(
                                AudioTake.generation_segment_id == segment_id,
                                AudioTake.generation_run_id.is_(None),
                                AudioTake.is_active.is_(True),
                                AudioTake.status.in_(("completed", "stale")),
                            )
                            .order_by(AudioTake.created_at.desc())
                        )
                    if source_take is None or source_take.artifact_id is None:
                        raise ValueError(
                            "The selected segment has no active audio take for RVC."
                        )
                    source_artifact = session.get(Artifact, source_take.artifact_id)
                    if source_artifact is None:
                        raise KeyError(source_take.artifact_id)
                    take_parent_ids = [source_artifact.id]
                    source_take_id = source_take.id
                source_path = context.paths.managed_path(source_artifact.relative_path)
                source_audio = AudioSegment.from_file(source_path)
                audio = rvc_handler.process_with_rvc(source_audio, rvc_settings)
                take_kind = "rvc"
                parent_take_id = source_take_id
                take_settings = rvc_settings
            else:
                previous_active_take_id = None
                if operation == "regenerate":
                    with context.database.session() as session:
                        previous_active_take_id = session.scalar(
                            select(AudioTake.id)
                            .where(
                                AudioTake.generation_segment_id == segment_id,
                                AudioTake.generation_run_id == output_run_id,
                                AudioTake.status.in_(("completed", "stale")),
                                AudioTake.artifact_id.is_not(None),
                            )
                            .order_by(AudioTake.created_at.desc())
                        )
                        if previous_active_take_id is None:
                            previous_active_take_id = session.scalar(
                                select(AudioTake.id)
                                .where(
                                    AudioTake.generation_segment_id == segment_id,
                                    AudioTake.is_active.is_(True),
                                )
                                .order_by(AudioTake.created_at.desc())
                            )
                batch_context = batch_contexts.get(segment_id)
                if batch_context is not None:
                    text = str(batch_context["text"])
                    synthesized_text = str(batch_context["synthesized_text"])
                    segment_tts_settings = dict(batch_context["settings"])
                else:
                    synthesized_text = optimized_by_id.get(segment_id, text)
                    segment_tts_settings = context._apply_segment_tts_overrides(
                        tts_settings,
                        language=segment_language,
                        voice=segment_voice,
                    )
                    segment_tts_settings = _tts_settings_for_segment(
                        segment_tts_settings,
                        language=segment_language,
                        voice=segment_voice,
                    )
                    segment_tts_settings = segment_performance_settings(
                        segment_tts_settings, settings_snapshot, segment_id, synthesized_text,
                        contexts=performance_contexts,
                    )
                    if not segment_tts_settings.get("casting_enabled"):
                        segment_tts_settings = context.prepare_audio_cpp_voice_reference(
                            segment_tts_settings
                        )
                        context._ensure_qwen_cloned_voice(
                            segment_tts_settings,
                            base_url=tts_urls["kobold_qwen_base_url"],
                            verified=verified_qwen_voices,
                            cancel_event=cancel_event,
                        )
                    _assert_current_audio_identity(segment_id)

                def synthesize_request(
                    *,
                    text_to_synthesize: str = synthesized_text,
                    settings_for_segment: dict[str, Any] = segment_tts_settings,
                    segment_index: int = index,
                ) -> AudioSegment | None:
                    return context.tts_providers.synthesize(
                        text_to_synthesize,
                        settings_for_segment,
                        max_attempts=int(
                            settings_for_segment.get("max_attempts") or 5
                        ),
                        cancel_event=cancel_event,
                        retry_callback=lambda attempt, total, delay: progress(
                            optimization_share
                            + (segment_index / len(segment_ids))
                            * (1.0 - optimization_share),
                            f"Retrying segment {segment_index + 1} ({attempt}/{total}) in {delay:.1f}s",
                        ),
                        recovery_callback=lambda cycle, total, timeout: progress(
                            optimization_share
                            + (segment_index / len(segment_ids))
                            * (1.0 - optimization_share),
                            f"Waiting for Qwen3 TTS before segment {segment_index + 1} ({cycle}/{total}, up to {timeout:.0f}s)",
                        ),
                        **tts_urls,
                    )

                def synthesize_one(
                    *,
                    segment_tts_settings: dict[str, Any] = segment_tts_settings,
                    segment_id: str = segment_id,
                    synthesized_text: str = synthesized_text,
                ) -> AudioSegment | None:
                    nonlocal render_manifest
                    if not segment_tts_settings.get("casting_enabled"):
                        return synthesize_request()
                    from .generation_cast_runtime import segment_render_parts
                    from .generation_rendering import execute_render_parts

                    parts = segment_render_parts(
                        segment_tts_settings, settings_snapshot,
                        segment_id, synthesized_text,
                    )
                    def render_part(
                        part_text: str,
                        part_settings: dict[str, Any],
                        segment_id: str = segment_id,
                    ) -> AudioSegment | None:
                        prepared = context.prepare_audio_cpp_voice_reference(part_settings)
                        context._ensure_qwen_cloned_voice(
                            prepared, base_url=tts_urls["kobold_qwen_base_url"],
                            verified=verified_qwen_voices, cancel_event=cancel_event,
                        )
                        _assert_current_audio_identity(segment_id)
                        return synthesize_request(
                            text_to_synthesize=part_text, settings_for_segment=prepared,
                        )
                    def cancelled():
                        if cancel_event.is_set():
                            return True
                        with context.database.session() as db:
                            current_run = db.get(GenerationRun, run_id)
                            return current_run is None or current_run.cancel_requested
                    assembled, render_manifest = execute_render_parts(
                        parts, synthesize=render_part, cancelled=cancelled,
                    )
                    _assert_current_audio_identity(segment_id)
                    return assembled

                if batch_results is not None and batch_context is not None:
                    try:
                        batch_result = next(batch_results)
                    except StopIteration as error:
                        raise RuntimeError(
                            "The grouped TTS batch ended before every segment completed."
                        ) from error
                    if batch_result.id != segment_id:
                        raise RuntimeError(
                            "The grouped TTS batch returned segments out of order."
                        )
                    if batch_result.error is not None:
                        if parallel_synthesis_batch:
                            parallel_wave_error = (
                                parallel_wave_error or batch_result.error
                            )
                            with context.database.session() as session:
                                failed_segment = session.get(
                                    GenerationSegment, segment_id
                                )
                                if failed_segment is not None:
                                    failed_segment.status = "failed"
                                    failed_segment.updated_at = utcnow()
                                failed_run = session.get(GenerationRun, run_id)
                                if failed_run is not None:
                                    failed_run.status = "failed"
                                    failed_run.updated_at = utcnow()
                            continue
                        if not batch_result.error.retryable:
                            raise batch_result.error
                        context._logger.warning(
                            "Grouped TTS batch failed for segment %s; retrying it through the ordinary synthesis path.",
                            segment_id,
                        )
                        audio = synthesize_one()
                    else:
                        audio = batch_result.audio
                else:
                    audio = synthesize_one()
                if audio is None:
                    raise RuntimeError("The speech service returned no audio.")
                take_kind = "tts"
                parent_take_id = previous_active_take_id
                take_settings = segment_tts_settings
                if bool(selected_rvc_override.get("enabled")):
                    if not str(
                        rvc_settings.get("model")
                        or rvc_settings.get("rvc_model")
                        or ""
                    ).strip():
                        raise ValueError(
                            "Choose an RVC model before generating an alternate RVC take."
                        )
                    audio = rvc_handler.process_with_rvc(audio, rvc_settings)
                    take_kind = "tts_rvc"
                    take_settings = {**segment_tts_settings, "rvc": rvc_settings}
            verification = context._verification_metadata(
                audio,
                synthesized_text,
                {**tts_settings, **take_settings},
            )
            if cast_render and cancel_event.is_set():
                raise MediaProcessCancelled("Audio generation was canceled.")
            take_dir = (
                context._session_dir(session_id)
                / "generation"
                / plan_revision_id
                / segment_id
            )
            take_dir.mkdir(parents=True, exist_ok=True)
            take_path = take_dir / f"{take_kind}-{new_id()}.wav"
            with take_path.open("wb") as output:
                audio.export(output, format="wav")
            stored_take_settings = context._secret_free_tts_settings(take_settings)
            prepared_artifact = context.artifacts.prepare_registration(
                take_path,
                settings=stored_take_settings,
            )
            with context.database.immediate_session() as session:
                if cast_render:
                    current_run = session.get(GenerationRun, run_id)
                    if cancel_event.is_set() or current_run is None or current_run.cancel_requested:
                        raise MediaProcessCancelled("Audio generation was canceled.")
                segment = session.get(GenerationSegment, segment_id)
                if segment is None:
                    raise KeyError(segment_id)
                artifact = context.artifacts.register_in_session(
                    session,
                    take_path,
                    kind="audio",
                    role="generation_take",
                    session_id=session_id,
                    parent_ids=take_parent_ids,
                    settings=stored_take_settings,
                    metadata={
                        "generation_segment_id": segment_id,
                        "generation_run_id": output_run_id,
                        **({"generation_audio_identity": audio_identities[segment_id]} if operation != "rvc" else {}),
                        **(
                            {"generation_task_run_id": run_id}
                            if output_run_id != run_id
                            else {}
                        ),
                        "kind": take_kind,
                        "speaker": segment_speaker,
                        "source_text": text,
                        "synthesized_text": synthesized_text,
                        **({"render_parts": render_manifest} if render_manifest else {}),
                        "llm_optimized": (
                            operation != "rvc" and synthesized_text != text
                        ),
                        "llm_model": optimization_model or None,
                        **(
                            {"audio_verification": verification}
                            if verification is not None
                            else {}
                        ),
                    },
                    _prepared=prepared_artifact,
                )
                if operation != "rvc":
                    usage_event = context._tts_usage_event(
                        session_id,
                        take_settings,
                        synthesized_text,
                        len(audio),
                        job_id=job_id,
                        artifact_id=artifact.id,
                        generation_run_id=output_run_id,
                    )
                    if usage_event is not None:
                        session.add(usage_event)
                from .generation_edit_audio import selection_is_unchanged

                selected_before = session.scalar(select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_id,
                    AudioTake.is_active.is_(True),
                ))
                expected_selection = (settings_snapshot.get("generation_selection_guards") or {}).get(segment_id)
                activate_new = selection_is_unchanged(selected_before, expected_selection)
                if activate_new:
                    deactivate = update(AudioTake).where(
                        AudioTake.generation_segment_id == segment_id,
                        AudioTake.is_active.is_(True),
                    )
                    session.execute(
                        deactivate.values(
                            is_active=False,
                            revision=AudioTake.revision + 1,
                        ).execution_options(synchronize_session=False)
                    )
                new_take = AudioTake(
                    generation_segment_id=segment_id,
                    generation_run_id=output_run_id,
                    artifact_id=artifact.id,
                    parent_take_id=parent_take_id,
                    kind=take_kind,
                    status="completed",
                    settings_hash=artifact.settings_hash,
                    duration_ms=len(audio),
                    is_active=activate_new,
                )
                session.add(new_take)
                session.flush()
                from .generation_edit_audio import publish_to_edit_copy

                publish_to_edit_copy(session, segment, new_take, artifact, expected_selection)
                segment.status = "completed" if activate_new else (selected_before.status if selected_before else "ready")
                if (
                    verification is not None
                    and verification.get("status") != "passed"
                ):
                    segment.marked = True
                segment.updated_at = utcnow()
                mark_output_assemblies_stale(
                    session,
                    session_id,
                    generation_run_id=output_run_id,
                    include_later_runs=output_run_id != run_id,
                )
            take_committed = True
            generated += 1
            progress(
                optimization_share
                + ((index + 1) / len(segment_ids)) * (1.0 - optimization_share),
                f"Generated segment {index + 1} of {len(segment_ids)}",
            )
        except Exception:
            if take_path is not None and not take_committed:
                try:
                    take_path.unlink(missing_ok=True)
                except OSError:
                    context._logger.warning(
                        "Could not remove uncommitted generation take %s",
                        take_path,
                        exc_info=True,
                    )
            with context.database.immediate_session() as session:
                segment = session.get(GenerationSegment, segment_id)
                run = session.get(GenerationRun, run_id)
                canceled_cast = cast_render and (
                    cancel_event.is_set() or run is None or run.cancel_requested
                )
                if segment is not None and not take_committed:
                    if canceled_cast:
                        has_active_take = session.scalar(select(AudioTake.id).where(
                            AudioTake.generation_segment_id == segment_id,
                            AudioTake.is_active.is_(True),
                            AudioTake.status == "completed",
                        ).limit(1))
                        segment.status = "completed" if has_active_take else "ready"
                    else:
                        segment.status = "failed"
                    segment.updated_at = utcnow()
                if run is not None and run.status in JobQueue.GENERATION_ACTIVE_STATUSES:
                    run.status = "canceled" if canceled_cast else "failed"
                    run.updated_at = utcnow()
            if canceled_cast:
                return {"generation_run_id": run_id, "status": "canceled", "generated": generated}
            raise

    if parallel_wave_error is not None:
        raise parallel_wave_error

    verification_warning_count = context._finalize_run_audio_verification(output_run_id)
    with context.database.session() as session:
        run = session.get(GenerationRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if operation == "rvc" or (
            operation == "regenerate" and run.output_generation_run_id is None
        ):
            incomplete = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        GenerationSegment.status != "completed",
                    )
                )
                or 0
            )
        else:
            completed_segments = (
                select(AudioTake.generation_segment_id)
                .where(
                    AudioTake.generation_run_id == output_run_id,
                    AudioTake.status == "completed",
                    AudioTake.artifact_id.is_not(None),
                )
                .distinct()
            )
            incomplete = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        ~GenerationSegment.id.in_(completed_segments),
                    )
                )
                or 0
            )
        final_status = "partial" if incomplete else "completed"
        run.status = "running" if repair_requested and final_status == "completed" else final_status
        run.updated_at = utcnow()
        if output_run_id != run_id:
            output_run = session.get(GenerationRun, output_run_id)
            if output_run is not None and output_run.status in {
                "completed",
                "partial",
                "failed",
                "canceled",
            }:
                output_run.status = final_status
                output_run.updated_at = utcnow()
    progress(
        1.0,
        "Generation run complete"
        if final_status == "completed"
        else f"Generation saved; {incomplete} segment(s) remain",
    )
    auto_resume_source_id = str(
        payload.get("auto_resume_source_generation_run_id") or ""
    )
    resumed_job_id = (
        context._resume_generation_after_regeneration(
            run_id,
            auto_resume_source_id,
        )
        if auto_resume_source_id
        else None
    )
    result = {
        "generation_run_id": run_id,
        "status": final_status,
        "generated": generated,
        "skipped": skipped,
        "remaining": incomplete,
        "verification_warnings": verification_warning_count,
    }
    if auto_resume_source_id:
        result["resumed_source_job_id"] = resumed_job_id
    if final_status == "completed" and repair_requested:
        try:
            result.update(context._repair_early_generation_blocks(
                run_id,
                lambda value, detail=None: completion_progress(0.85 + 0.14 * value, detail),
                cancel_event,
            ))
        except Exception:
            context._logger.warning("Optional voiceover repair could not finish; the generated audio remains available.", exc_info=True)
            result["early_repair_status"] = "failed"
        finally:
            result["status"] = _restore_optional_pass_status(
                context.database, run_id, final_status
            )
        if result.get("repaired_blocks"):
            result["source_generation_run_id"] = run_id
            result["generation_run_id"] = result["repaired_generation_run_id"]
    if repair_requested:
        completion_progress(1.0, f"Voiceover timing checked; {result.get('repaired_blocks', 0)} block(s) repaired")
    if final_status == "completed" and regroup_requested:
        try:
            result.update(context._regroup_generation_blocks(
                run_id,
                lambda value, detail=None: completion_progress(0.85 + 0.14 * value, detail),
                cancel_event,
            ))
        except Exception:
            context._logger.warning("Optional voiceover regroup could not finish; the generated audio remains available.", exc_info=True)
            result["regroup_status"] = "failed"
        finally:
            result["status"] = _restore_optional_pass_status(
                context.database, run_id, final_status
            )
        if result.get("regrouped_groups"):
            result["source_generation_run_id"] = run_id
            result["generation_run_id"] = result["regrouped_generation_run_id"]
    if regroup_requested:
        # Frozen wrapper contract: skipped_active_plan_changed carries
        # regroup_status/regroup_reason zero attempt/group counts plus
        # source/active plan IDs via result.update above; surface the skip
        # instead of "0 groups regenerated".
        if result.get("regroup_status") == "skipped_active_plan_changed":
            completion_progress(1.0, "Regroup skipped: speech plan was edited during generation")
        else:
            completion_progress(1.0, f"Voiceover regroup checked; {result.get('regrouped_groups', 0)} group(s) regenerated")
    return result

