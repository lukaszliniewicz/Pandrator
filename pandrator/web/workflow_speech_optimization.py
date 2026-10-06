"""Speech optimization and incremental persistence with explicit worker ports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pandrator.logic.cancellable_process import ProcessCancelled

from .models import (
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
)
from .workflow_generation_protocols import Progress
from .workflow_text_protocols import (
    BeginAgenticOperationProtocol,
    RecordUsageProtocol,
    SaveSpeechPlanProposalsProtocol,
    StoreSrtDocumentProtocol,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .artifacts import ArtifactService
    from .database import Database


@dataclass(frozen=True, slots=True)
class SpeechOptimizationContext:
    database: Database
    artifacts: ArtifactService
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _session_dir: Callable[[str], Path]
    _store_srt_document: StoreSrtDocumentProtocol
    _with_database_llm_settings: Callable[[dict[str, Any], str], dict[str, Any]]
    _begin_agentic_operation: BeginAgenticOperationProtocol
    _save_speech_plan_proposals: SaveSpeechPlanProposalsProtocol
    _optimization_text_hash: Callable[[str], str]
    _record_usage: RecordUsageProtocol
    _scaled_progress_callback: Callable[[Progress, float, float], Progress]
    new_id: Callable[[], str]
    utcnow: Callable[[], datetime]


def optimize_tts(context: SpeechOptimizationContext, payload, progress, cancel_event):
    """Create a separate, previewable text revision optimized only for speech."""
    from dataclasses import replace
    from types import SimpleNamespace

    from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

    from .pronunciations import (
        PronunciationLibrary,
        apply_reviewed_pronunciations,
        normalize_backend,
    )
    from .speech_structure_analysis import annotate_speech_units
    from .tts_optimization import OptimizationUsage, optimize_texts

    session_id = str(payload.get("session_id") or "")
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
    requested_settings = dict(payload.get("settings") or {})
    requested_settings_hash = hashlib.sha256(
        json.dumps(
            requested_settings,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    settings = context._with_database_llm_settings(requested_settings, "tts_optimization")
    settings["llm_tts_batch_size"] = max(
        1,
        int(settings.get("llm_tts_document_batch_size") or settings.get("llm_tts_batch_size") or 8),
    )
    llm_settings = SimpleNamespace(
        provider_configs=settings["llm_provider_configs"],
        default_model=settings["llm_default_model"],
        request_timeout_seconds=settings["request_timeout_seconds"],
    )
    model_name = str(settings.get("tts_optimization_model") or settings["llm_default_model"])
    run_store, agent_run, persist_checkpoint = context._begin_agentic_operation(
        payload=payload,
        kind="tts_optimization",
        source_artifact=source_artifact,
        requested_settings=requested_settings,
        instructions=str(payload.get("instructions") or settings.get("instructions") or ""),
        usage_settings=settings,
    )
    speech_mode = str(settings.get("speech_optimization_mode") or "").strip().lower()
    structured_mode = speech_mode in {"guarded", "flexible"}
    annotation_mode = (
        str(settings.get("llm_tts_annotation_mode") or settings.get("annotation_mode") or "off")
        .strip()
        .lower()
    )
    if annotation_mode not in {"off", "dialogue", "speakers"}:
        raise ValueError("llm_tts_annotation_mode must be off, dialogue, or speakers")
    annotation_only = bool(
        settings.get(
            "llm_tts_annotation_only",
            settings.get("annotation_only", False),
        )
    )
    default_language = str(
        settings.get("language")
        or settings.get("target_language")
        or settings.get("source_language")
        or (source_artifact.metadata_json or {}).get("language")
        or "en"
    )
    voice_language = str(
        settings.get("voice_language") or settings.get("language") or default_language
    )
    backend = normalize_backend(
        settings.get("service") or settings.get("tts_service") or settings.get("backend") or "*"
    )
    pronunciation_library = PronunciationLibrary(context.database)
    apply_reviewed = settings.get("apply_reviewed_pronunciations", True) is not False
    speech_plans: list[dict[str, Any]] = []
    metadata_markup = (source_artifact.metadata_json or {}).get("speech_markup")
    source_markup: dict[str, str] = {
        str(key): value
        for key, value in (metadata_markup.items() if isinstance(metadata_markup, dict) else [])
        if isinstance(value, str)
    }

    def optimize_units(
        source_texts: list[str],
        languages: list[str],
    ):
        nonlocal speech_plans
        speech_plans = [{} for _ in source_texts]
        known_by_index = (
            {
                index: pronunciation_library.resolve(
                    text,
                    session_id=session_id,
                    language=languages[index],
                    backend=backend,
                )
                for index, text in enumerate(source_texts)
            }
            if apply_reviewed
            else {}
        )

        def resolve_known(text: str, language: str) -> list[dict[str, Any]]:
            for index, source_text in enumerate(source_texts):
                if source_text == text and languages[index] == language:
                    return deepcopy(known_by_index.get(index, []))
            return []

        def keep_plans(items: list[tuple[int, str, dict[str, Any]]]) -> None:
            for index, _revised, plan in items:
                if bool(settings.get("speech_plan_save_proposals", True)):
                    plan["proposals"] = context._save_speech_plan_proposals(
                        library=pronunciation_library,
                        session_id=session_id,
                        plan=plan,
                        backend=backend,
                        model_name=model_name,
                        default_language=languages[index],
                    )
                else:
                    plan["proposals"] = []
                speech_plans[index] = plan

        try:
            if annotation_only:
                optimized = list(source_texts)
                usage = OptimizationUsage()
            else:
                optimized, usage = optimize_texts(
                    source_texts,
                    settings,
                    llm_settings,
                    model_name,
                    cancel_event,
                    context._scaled_progress_callback(progress, 0.05, 0.9),
                    on_plan_batch=keep_plans if structured_mode else None,
                    known_pronunciation_resolver=resolve_known if structured_mode else None,
                    languages=languages,
                    voice_languages=[voice_language for _ in source_texts],
                    completed_units=agent_run.completed_units,
                    on_unit_completed=lambda key, output: persist_checkpoint(
                        key,
                        output,
                        phase="tts_optimization",
                        usage_stage="tts_optimization",
                        usage_settings=settings,
                    ),
                )
            if apply_reviewed and not structured_mode and not annotation_only:
                optimized = [
                    apply_reviewed_pronunciations(
                        revised,
                        known_by_index.get(index, []),
                    )
                    for index, revised in enumerate(optimized)
                ]
            if annotation_mode != "off" or source_markup:

                def record_annotation_usage(result) -> None:
                    response_usage = OptimizationUsage()
                    response_usage.add(result)
                    usage.merge(response_usage)
                    context._record_usage(
                        session_id,
                        "tts_optimization",
                        settings,
                        response_usage,
                        job_id=str(payload.get("_job_id") or "") or None,
                        agent_run_id=agent_run.id,
                        request_key=f"annotation-{context.new_id()}",
                    )

                markup = annotate_speech_units(
                    context.database,
                    session_id,
                    optimized,
                    mode=annotation_mode,
                    llm_settings=llm_settings,
                    model_name=model_name,
                    cancel_event=cancel_event,
                    source_markup=source_markup,
                    on_usage=record_annotation_usage,
                )
                for index, xml in enumerate(markup):
                    if xml:
                        speech_plans[index]["speech_xml"] = xml
            return optimized, usage
        except Exception as error:
            run_store.fail(agent_run.id, error)
            raise

    suffix = source_path.suffix.lower()
    progress(0.02, "Preparing speech optimization preview")
    if suffix == ".srt":
        segments = parse_srt(source_path.read_text(encoding="utf-8-sig"))
        source_texts = [segment.text for segment in segments]
        optimized, usage = optimize_units(
            source_texts,
            [default_language for _ in source_texts],
        )
        if cancel_event.is_set():
            return {}
        segments = [
            replace(segment, text=text) for segment, text in zip(segments, optimized, strict=True)
        ]
        destination = context._session_dir(session_id) / f"tts-optimized-{context.new_id()}.srt"
        destination.write_text(compose_srt(segments), encoding="utf-8")
        kind = "srt"
    elif suffix == ".json":
        rows = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(
                "Speech optimization JSON input must contain a list of generation units."
            )
        rows: list[dict[str, Any]] = [
            row if isinstance(row, dict) else {"text": str(row)} for row in rows
        ]
        source_texts = [
            str(
                row.get("source_text")
                or row.get("text")
                or row.get("processed_sentence")
                or row.get("original_sentence")
                or ""
            )
            if isinstance(row, dict)
            else str(row)
            for row in rows
        ]
        languages = [
            str(row.get("language") or default_language)
            if isinstance(row, dict)
            else default_language
            for row in rows
        ]
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            nested_plan = row.get("speech_plan")
            row_markup = row.get("speech_xml") or (
                nested_plan.get("speech_xml") if isinstance(nested_plan, dict) else None
            )
            if isinstance(row_markup, str):
                source_markup[str(index)] = row_markup
        optimized, usage = optimize_units(source_texts, languages)
        if cancel_event.is_set():
            return {}
        for index, (row, text) in enumerate(zip(rows, optimized, strict=True)):
            if isinstance(row, dict):
                row["source_text"] = str(
                    row.get("source_text")
                    or row.get("text")
                    or row.get("processed_sentence")
                    or row.get("original_sentence")
                    or ""
                )
                row["tts_optimized_sentence"] = text
                if speech_plans[index]:
                    existing_plan = row.get("speech_plan")
                    merged_plan = {
                        **(existing_plan if isinstance(existing_plan, dict) else {}),
                        **speech_plans[index],
                    }
                    row["speech_plan"] = merged_plan
                    if merged_plan.get("speech_xml"):
                        row["speech_xml"] = merged_plan["speech_xml"]
        destination = context._session_dir(session_id) / f"tts-optimized-{context.new_id()}.json"
        destination.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        kind = "json"
    else:
        source_texts = [source_path.read_text(encoding="utf-8-sig")]
        optimized, usage = optimize_units(source_texts, [default_language])
        if cancel_event.is_set():
            return {}
        destination = context._session_dir(session_id) / f"tts-optimized-{context.new_id()}.txt"
        destination.write_text(optimized[0], encoding="utf-8")
        kind = "text"
    progress(0.92, "Speech optimization complete; preparing preview artifact")
    speech_plan_artifact_id = ""
    if any(speech_plans):
        plan_path = context._session_dir(session_id) / f"speech-plans-{context.new_id()}.json"
        plan_path.write_text(
            json.dumps(
                [
                    {
                        "index": index,
                        "source_text": source_text,
                        "speech_plan": plan,
                    }
                    for index, (source_text, plan) in enumerate(
                        zip(source_texts, speech_plans, strict=True)
                    )
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        plan_artifact = context.artifacts.register(
            plan_path,
            kind="json",
            role="speech_plan",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={
                "source_artifact_id": source_artifact.id,
                "model": model_name,
                "mode": speech_mode,
                "plan_count": len(speech_plans),
                "speech_markup": {
                    str(index + 1): str(plan["speech_xml"])
                    for index, plan in enumerate(speech_plans)
                    if isinstance(plan, dict) and isinstance(plan.get("speech_xml"), str)
                },
            },
        )
        speech_plan_artifact_id = plan_artifact.id
    publication_parent_ids = [
        source_artifact.id,
        *([speech_plan_artifact_id] if speech_plan_artifact_id else []),
    ]
    try:
        progress(0.97, "Registering speech optimization artifacts")
        if suffix == ".srt" and cancel_event.is_set():
            raise ProcessCancelled("Speech optimization was canceled.")
        artifact = context.artifacts.register(
            destination,
            kind=kind,
            role="tts_optimization_candidate" if suffix == ".srt" else "tts_optimized",
            session_id=session_id,
            parent_ids=publication_parent_ids,
            settings=settings,
            metadata={
                "source_artifact_id": source_artifact.id,
                "model": model_name,
                "mode": "whole_document",
                "speech_optimization_mode": speech_mode or "legacy",
                "speech_plan_artifact_id": speech_plan_artifact_id or None,
                "speech_plan_count": len([plan for plan in speech_plans if plan]),
                "speech_markup": {
                    str(index + 1): str(plan["speech_xml"])
                    for index, plan in enumerate(speech_plans)
                    if isinstance(plan, dict) and isinstance(plan.get("speech_xml"), str)
                },
                "batch_size": settings["llm_tts_batch_size"],
                "requested_settings_hash": requested_settings_hash,
                "agent_run_id": agent_run.id,
            },
        )
        if suffix == ".srt":
            if cancel_event.is_set():
                raise ProcessCancelled("Speech optimization was canceled.")
            context._store_srt_document(
                session_id,
                artifact,
                "tts_optimization",
                language=str((source_artifact.metadata_json or {}).get("language") or "") or None,
                parent_artifact=source_artifact,
            )
            if cancel_event.is_set():
                raise ProcessCancelled("Speech optimization was canceled.")
            registration = context.artifacts.prepare_registration(destination, settings=settings)
            with context.database.session() as session:
                if cancel_event.is_set():
                    raise ProcessCancelled("Speech optimization was canceled.")
                artifact = context.artifacts.register_in_session(
                    session,
                    destination,
                    kind=kind,
                    role="tts_optimized",
                    session_id=session_id,
                    parent_ids=publication_parent_ids,
                    settings=settings,
                    _prepared=registration,
                )
                if cancel_event.is_set():
                    raise ProcessCancelled("Speech optimization was canceled.")
    except Exception as error:
        run_store.fail(agent_run.id, error, interrupted=cancel_event.is_set())
        raise
    run_store.finish(agent_run.id, artifact_id=artifact.id)
    progress(1.0, "Speech optimization preview ready")
    input_tokens = int(usage.usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.usage.get("completion_tokens") or 0)
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "cost": usage.cost,
        "agent_run_id": agent_run.id,
        "resumed": agent_run.resumed,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": int(usage.usage.get("cached_prompt_tokens") or 0),
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "response_count": usage.response_count,
        },
    }


def save_speech_plan_proposals(
    *,
    library,
    session_id: str,
    plan: dict[str, Any],
    backend: str,
    model_name: str,
    default_language: str,
) -> list[dict[str, Any]]:
    """Persist model pronunciations as review-only entries, never active ones."""
    candidates = {
        str(item.get("id") or ""): item
        for item in list(plan.get("candidates") or [])
        if isinstance(item, dict)
    }
    proposed_items: list[tuple[str, str, str, str]] = []
    for decision in list(plan.get("decisions") or []):
        if (
            isinstance(decision, dict)
            and decision.get("action") == "pronounce"
            and str(decision.get("spoken") or "").strip()
        ):
            candidate = candidates.get(str(decision.get("span_id") or ""))
            if candidate:
                proposed_items.append(
                    (
                        str(candidate.get("text") or ""),
                        str(decision.get("spoken") or ""),
                        str(decision.get("confidence") or "medium"),
                        str(candidate.get("id") or ""),
                    )
                )
    for discovery in list(plan.get("discoveries") or []):
        if (
            isinstance(discovery, dict)
            and discovery.get("action") == "pronounce"
            and str(discovery.get("spoken") or "").strip()
        ):
            proposed_items.append(
                (
                    str(discovery.get("source_text") or ""),
                    str(discovery.get("spoken") or ""),
                    str(discovery.get("confidence") or "medium"),
                    "discovery",
                )
            )

    proposals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source_form, phonetic, confidence, span_id in proposed_items:
        key = (source_form.casefold().strip(), phonetic)
        if not source_form.strip() or key in seen:
            continue
        seen.add(key)
        try:
            proposal = library.propose(
                session_id=session_id,
                source_form=source_form,
                phonetic=phonetic,
                language=str(plan.get("language") or default_language),
                backend=backend,
                metadata={
                    "case_id": plan.get("case_id"),
                    "model": model_name,
                    "confidence": confidence,
                    "span_id": span_id,
                },
            )
        except (KeyError, ValueError) as error:
            proposals.append(
                {
                    "source_form": source_form,
                    "phonetic": phonetic,
                    "status": "not_saved",
                    "error": str(error),
                }
            )
        else:
            proposals.append(
                {
                    "id": proposal["id"],
                    "source_form": proposal["source_form"],
                    "phonetic": proposal["phonetic"],
                    "status": proposal["status"],
                    "revision": proposal["revision"],
                }
            )
    return proposals


def optimize_generation_texts(
    context: SpeechOptimizationContext,
    session_id: str,
    segment_ids: list[str],
    texts: list[str],
    settings: dict[str, Any],
    cancel_event,
    progress,
    *,
    job_id: str | None = None,
    generation_run_id: str | None = None,
    source_artifact_id: str | None = None,
    pronunciation_settings: dict[str, Any] | None = None,
    pronunciation_language: str | None = None,
    pronunciation_voice_language: str | None = None,
) -> tuple[list[str], str]:
    """Resolve reviewed or newly batched inline optimization for generation."""
    from .pronunciations import (
        PronunciationLibrary,
        apply_reviewed_pronunciations,
        normalize_backend,
    )

    if bool(settings.get("llm_tts_optimization")):
        from .models import PerformancePlan

        if bool(settings.get("llm_tts_document_optimization")):
            raise ValueError(
                "Document-level speech optimization is already selected. Prepare and review a speech plan before generation instead of rewriting it again."
            )
        with context.database.session() as session:
            active = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            revision_ids = (
                {active.active_revision_id} if active and active.active_revision_id else set()
            )
            if segment_ids:
                first = session.get(GenerationSegment, segment_ids[0])
                if first is not None:
                    revision_ids.add(first.plan_revision_id)
            artifact_ids = {source_artifact_id} if source_artifact_id else set()
            for revision_id in revision_ids:
                revision = session.get(GenerationPlanRevision, revision_id)
                if revision is None:
                    continue
                revision_settings = dict(revision.settings_json or {})
                if revision_settings.get("llm_tts_document_optimization"):
                    raise ValueError(
                        "This speech plan used document-level optimization. Prepare and review a new speech plan before generation."
                    )
                if revision_settings.get("_source_artifact_id"):
                    artifact_ids.add(str(revision_settings["_source_artifact_id"]))
                if session.scalar(
                    select(PerformancePlan.id)
                    .where(
                        PerformancePlan.plan_revision_id == revision_id,
                        PerformancePlan.status == "adopted",
                    )
                    .limit(1)
                ):
                    raise ValueError(
                        "An adopted speech direction belongs to this plan. Prepare and review a speech plan before generation instead of rewriting its wording."
                    )
                if any(
                    isinstance(value, dict) and bool(value.get("speech_xml"))
                    for value in session.scalars(
                        select(GenerationSegment.speech_plan_json).where(
                            GenerationSegment.plan_revision_id == revision_id
                        )
                    )
                ):
                    raise ValueError(
                        "Speech XML belongs to this plan. Prepare and review a speech plan before generation instead of rewriting its wording."
                    )
            if artifact_ids:
                artifact_ids.update(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id.in_(artifact_ids)
                        )
                    )
                )
            for artifact_id in artifact_ids:
                source = session.get(Artifact, artifact_id)
                if source is not None and (
                    source.role == "tts_optimized"
                    or bool((source.metadata_json or {}).get("speech_markup"))
                ):
                    raise ValueError(
                        "The source already has document-level optimization or speech XML. Prepare and review a speech plan before generation instead of rewriting it again."
                    )

    if settings.get("llm_tts_annotation_mode", "off") != "off":
        raise ValueError(
            "Dialogue and character analysis creates a reviewable document revision. Run speech optimization before generation."
        )
    apply_reviewed = settings.get("apply_reviewed_pronunciations", True) is not False
    # Targeted regeneration may synthesize with a selected alternate TTS
    # runtime while retaining the immutable source-run settings.  Keep
    # pronunciation lookup on an explicit, secret-free context so the
    # alternate provider/language can scope reviewed entries without
    # changing the persisted run snapshot.
    pronunciation_context = (
        pronunciation_settings if pronunciation_settings is not None else settings
    )
    pronunciation_language_override = str(pronunciation_language or "").strip()
    pronunciation_voice_language_override = str(pronunciation_voice_language or "").strip()
    pronunciation_service = str(
        pronunciation_context.get("service")
        or pronunciation_context.get("tts_service")
        or pronunciation_context.get("backend")
        or ""
    ).strip()
    pronunciation_endpoint = str(pronunciation_context.get("openai_audio_endpoint") or "").strip()
    if pronunciation_endpoint and pronunciation_service.casefold().replace("-", "_") in {
        "custom",
        "openai_compatible",
        "openai_compatible_service",
    }:
        pronunciation_backend_value = pronunciation_endpoint
    else:
        pronunciation_backend_value = pronunciation_service or pronunciation_endpoint or "*"
    pronunciation_backend = normalize_backend(pronunciation_backend_value)
    if not bool(settings.get("llm_tts_optimization")):
        pronunciation_library = PronunciationLibrary(context.database)
        default_language = str(
            settings.get("language")
            or settings.get("target_language")
            or settings.get("source_language")
            or "en"
        )
        entries_by_position: dict[int, list[dict[str, Any]]] = {}
        if apply_reviewed:
            with context.database.session() as session:
                for position, (segment_id, text) in enumerate(zip(segment_ids, texts, strict=True)):
                    segment = session.get(GenerationSegment, segment_id)
                    language = (
                        pronunciation_language_override
                        or str(segment.language if segment is not None else "").strip()
                        or default_language
                    )
                    entries_by_position[position] = pronunciation_library.resolve(
                        text,
                        session_id=session_id,
                        language=language,
                        backend=pronunciation_backend,
                    )
        output = list(texts)
        manual_override_positions: set[int] = set()
        model_name = ""
        reuse_saved = bool(settings.get("use_existing_speech_plans"))
        with context.database.session() as session:
            for position, (segment_id, text) in enumerate(zip(segment_ids, texts, strict=True)):
                segment = session.get(GenerationSegment, segment_id)
                if (
                    segment is None
                    or not segment.optimized_text
                    or segment.optimization_source_hash != context._optimization_text_hash(text)
                    or segment.optimization_status not in {"optimized", "reviewed"}
                ):
                    continue
                speech_plan = dict(segment.speech_plan_json or {})
                is_manual_override = speech_plan.get("status") == "manual_override"
                if not is_manual_override and not reuse_saved:
                    continue
                output[position] = segment.optimized_text
                if is_manual_override:
                    manual_override_positions.add(position)
                else:
                    model_name = model_name or str(segment.optimization_model or "")
        if apply_reviewed:
            output = [
                revised
                if position in manual_override_positions
                else apply_reviewed_pronunciations(
                    revised,
                    entries_by_position.get(position, []),
                )
                for position, revised in enumerate(output)
            ]
        return output, model_name

    from copy import deepcopy
    from types import SimpleNamespace

    from .tts_optimization import optimize_texts

    resolved = context._with_database_llm_settings(dict(settings), "tts_optimization")
    llm_settings = SimpleNamespace(
        provider_configs=resolved["llm_provider_configs"],
        default_model=resolved["llm_default_model"],
        request_timeout_seconds=resolved["request_timeout_seconds"],
    )
    model_name = str(resolved.get("tts_optimization_model") or resolved["llm_default_model"])
    speech_mode = str(resolved.get("speech_optimization_mode") or "").strip().lower()
    structured_mode = speech_mode in {"guarded", "flexible"}
    from .speech_planning import SPEECH_PROMPT_REVISION

    default_language = str(
        resolved.get("language")
        or resolved.get("target_language")
        or resolved.get("source_language")
        or "en"
    )
    voice_language = str(
        resolved.get("voice_language") or resolved.get("language") or default_language
    )
    if pronunciation_voice_language_override:
        voice_language = pronunciation_voice_language_override
    elif pronunciation_language_override:
        voice_language = pronunciation_language_override
    backend = normalize_backend(
        resolved.get("service") or resolved.get("tts_service") or resolved.get("backend") or "*"
    )
    if pronunciation_settings is not None:
        backend = pronunciation_backend
    pronunciation_library = PronunciationLibrary(context.database)
    output = list(texts)
    pending_texts: list[str] = []
    pending_positions: list[int] = []
    pending_languages: list[str] = []
    pending_voice_languages: list[str] = []
    segment_state: dict[str, dict[str, Any]] = {}
    with context.database.session() as session:
        for segment_id in segment_ids:
            segment = session.get(GenerationSegment, segment_id)
            if segment is not None:
                segment_state[segment_id] = {
                    "optimized_text": segment.optimized_text,
                    "optimization_source_hash": segment.optimization_source_hash,
                    "optimization_status": segment.optimization_status,
                    "optimization_model": segment.optimization_model,
                    "optimization_reviewed": segment.optimization_reviewed,
                    "speech_plan": deepcopy(segment.speech_plan_json or {}),
                    "language": str(segment.language or default_language),
                }

    known_by_position: dict[int, list[dict[str, Any]]] = {}
    for position, (segment_id, text) in enumerate(zip(segment_ids, texts, strict=True)):
        state = segment_state.get(segment_id, {})
        language = pronunciation_language_override or str(state.get("language") or default_language)
        known = (
            pronunciation_library.resolve(
                text,
                session_id=session_id,
                language=language,
                backend=backend,
            )
            if apply_reviewed
            else []
        )
        known_by_position[position] = known
        source_hash = context._optimization_text_hash(text)
        plan_source_hash = context._optimization_text_hash(" ".join(text.split()))
        plan = dict(state.get("speech_plan") or {})
        current_known_signature = sorted(
            (str(item.get("id") or ""), int(item.get("revision") or 0)) for item in known
        )
        planned_known_signature = sorted(
            (str(item.get("entry_id") or ""), int(item.get("entry_revision") or 0))
            for item in list(plan.get("known_pronunciations") or [])
        )
        persisted_language = str(plan.get("language") or "").strip()
        persisted_voice_language = str(plan.get("voice_language") or "").strip()
        plan_context_matches = True
        if (
            pronunciation_language_override
            or pronunciation_voice_language_override
            or persisted_language
            or persisted_voice_language
        ):
            # An explicitly selected alternate language changes the speech
            # planning contract. Legacy plans without these fields must
            # not silently cross that boundary. When persisted fields are
            # present, ordinary reuse also requires them to match.
            plan_context_matches = bool(
                persisted_language
                and persisted_voice_language
                and persisted_language.casefold() == language.casefold()
                and persisted_voice_language.casefold() == voice_language.casefold()
            )
        reusable = bool(
            state
            and state.get("optimized_text")
            and state.get("optimization_source_hash") == source_hash
            and state.get("optimization_status") in {"optimized", "reviewed"}
        )
        if reusable and state.get("optimization_status") != "reviewed":
            if structured_mode:
                reusable = bool(
                    plan
                    and plan.get("source_hash") == plan_source_hash
                    and plan.get("mode_requested") == speech_mode
                    and plan.get("model") == model_name
                    and plan.get("prompt_revision") == SPEECH_PROMPT_REVISION
                    and planned_known_signature == current_known_signature
                    and plan_context_matches
                )
            else:
                reusable = not plan and state.get("optimization_model") == model_name
        if reusable:
            revised = str(state["optimized_text"])
            if apply_reviewed and not structured_mode:
                revised = apply_reviewed_pronunciations(
                    revised,
                    known_by_position.get(position, []),
                )
            output[position] = revised
            continue
        pending_positions.append(position)
        pending_texts.append(text)
        pending_languages.append(language)
        pending_voice_languages.append(voice_language)

    if cancel_event.is_set():
        return output, model_name

    with context.database.session() as session:
        for position in pending_positions:
            segment = session.get(GenerationSegment, segment_ids[position])
            if segment is not None:
                segment.optimization_status = "running"
                segment.optimization_reviewed = False
                segment.optimization_model = model_name
                segment.speech_plan_json = {}
                segment.updated_at = context.utcnow()

    if not pending_texts:
        return output, model_name

    def persist_batch(items: list[tuple[int, str]]) -> None:
        if cancel_event.is_set() or structured_mode:
            return
        with context.database.session() as session:
            for local_index, revised in items:
                position = pending_positions[local_index]
                if apply_reviewed and not structured_mode:
                    revised = apply_reviewed_pronunciations(
                        revised,
                        known_by_position.get(position, []),
                    )
                output[position] = revised
                segment = session.get(GenerationSegment, segment_ids[position])
                if segment is None:
                    continue
                segment.optimized_text = revised
                segment.optimization_status = "optimized"
                segment.optimization_source_hash = context._optimization_text_hash(texts[position])
                segment.optimization_reviewed = False
                segment.optimization_model = model_name
                segment.updated_at = context.utcnow()

    def resolve_pending_pronunciations(
        _text: str,
        _language: str,
    ) -> list[dict[str, Any]]:
        # Structured optimization calls this from worker threads. Resolution
        # was performed before dispatch so workers never share ORM sessions.
        for local_index, position in enumerate(pending_positions):
            if pending_texts[local_index] == _text and pending_languages[local_index] == _language:
                return deepcopy(known_by_position.get(position, []))
        return []

    def persist_plan_batch(
        items: list[tuple[int, str, dict[str, Any]]],
    ) -> None:
        if cancel_event.is_set():
            return
        for local_index, revised, plan in items:
            position = pending_positions[local_index]
            plan["language"] = pending_languages[local_index]
            plan["voice_language"] = pending_voice_languages[local_index]
            proposals: list[dict[str, Any]] = []
            if bool(resolved.get("speech_plan_save_proposals", True)):
                proposals = context._save_speech_plan_proposals(
                    library=pronunciation_library,
                    session_id=session_id,
                    plan=plan,
                    backend=backend,
                    model_name=model_name,
                    default_language=pending_languages[local_index],
                )
            plan["proposals"] = proposals
            with context.database.session() as session:
                segment = session.get(GenerationSegment, segment_ids[position])
                if segment is None:
                    continue
                segment.optimized_text = revised
                segment.speech_plan_json = plan
                segment.optimization_status = "optimized"
                segment.optimization_source_hash = context._optimization_text_hash(texts[position])
                segment.optimization_reviewed = False
                segment.optimization_model = model_name
                segment.updated_at = context.utcnow()

    recorded_unit_usage = False

    def record_unit_usage(_key: str, unit: dict[str, Any]) -> None:
        nonlocal recorded_unit_usage
        context._record_usage(
            session_id,
            "tts_optimization",
            resolved,
            SimpleNamespace(
                cost=float(unit.get("cost") or 0.0),
                response_count=int(unit.get("response_count") or 0),
                usage=dict(unit.get("usage") or {}),
                cost_sources=tuple(unit.get("cost_sources") or ()),
            ),
            job_id=job_id,
            generation_run_id=generation_run_id,
        )
        recorded_unit_usage = True

    def settle_unfinished() -> None:
        with context.database.session() as session:
            for position in pending_positions:
                segment = session.get(GenerationSegment, segment_ids[position])
                if segment is None or segment.optimization_status != "running":
                    continue
                if cancel_event.is_set():
                    previous = segment_state[segment.id]
                    segment.optimization_status = previous["optimization_status"]
                    segment.optimization_model = previous["optimization_model"]
                    segment.optimization_reviewed = previous["optimization_reviewed"]
                    segment.speech_plan_json = deepcopy(previous["speech_plan"])
                else:
                    segment.optimization_status = "failed"
                segment.updated_at = context.utcnow()

    try:
        optimized, usage = optimize_texts(
            pending_texts,
            resolved,
            llm_settings,
            model_name,
            cancel_event,
            progress,
            on_batch=persist_batch,
            on_plan_batch=persist_plan_batch if structured_mode else None,
            known_pronunciation_resolver=(
                resolve_pending_pronunciations if structured_mode else None
            ),
            languages=pending_languages,
            voice_languages=pending_voice_languages,
            on_unit_completed=record_unit_usage,
        )
    except Exception:
        settle_unfinished()
        raise
    if cancel_event.is_set():
        settle_unfinished()
        return output, model_name
    for local_index, revised in enumerate(optimized):
        position = pending_positions[local_index]
        if apply_reviewed and not structured_mode:
            revised = apply_reviewed_pronunciations(
                revised,
                known_by_position.get(position, []),
            )
        output[position] = revised
    if not recorded_unit_usage:
        context._record_usage(
            session_id,
            "tts_optimization",
            resolved,
            usage,
            job_id=job_id,
            generation_run_id=generation_run_id,
        )
    return output, model_name
