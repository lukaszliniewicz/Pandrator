"""Correction and translation publication through captured workflow ports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .workflow_text_protocols import (
    BeginAgenticOperationProtocol,
    PreparePassageInputProtocol,
    RecordUsageProtocol,
    ResolveRunPassageSettingsProtocol,
    ResolveSecretReferenceProtocol,
    RunStageWebResearchProtocol,
    SourcePassageRunLedgerProtocol,
    StoreSrtDocumentProtocol,
)

if TYPE_CHECKING:
    from pandrator.runtime import DataPaths

    from .artifacts import ArtifactService
    from .database import Database
    from .models import Artifact
    from .web_research import WebResearchResult
    from .workflow_generation_protocols import Progress


@dataclass(frozen=True, slots=True)
class SubtitleTransformContext:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _operation_dir: Callable[[str, str], Path]
    _resolve_run_passage_settings: ResolveRunPassageSettingsProtocol
    _prepare_passage_input: PreparePassageInputProtocol
    _passage_display_settings: Callable[[str], dict[str, Any]]
    _source_passage_run_ledger: SourcePassageRunLedgerProtocol
    _with_database_llm_settings: Callable[[dict[str, Any], str], dict[str, Any]]
    _begin_agentic_operation: BeginAgenticOperationProtocol
    _run_stage_web_research: RunStageWebResearchProtocol
    _research_metadata: Callable[[WebResearchResult | None, str], dict[str, Any] | None]
    _render_passage_output: Callable[
        [Artifact, Any, list[dict[str, Any]], dict[str, Any], str],
        tuple[list[dict[str, Any]] | None, dict[int, str]],
    ]
    _store_srt_document: StoreSrtDocumentProtocol
    _record_usage: RecordUsageProtocol
    _stage_settings_fingerprint: Callable[[str, dict[str, Any]], dict[str, Any]]
    _scaled_progress_callback: Callable[[Progress, float, float], Progress]
    _normalize_correction_style: Callable[[str | None], str]
    _resolve_secret_reference: ResolveSecretReferenceProtocol
    _database_reference: Callable[[str], str]
    _auxiliary_credential_key: Callable[[object], str]


def correct(context: SubtitleTransformContext, payload, progress, cancel_event):
    from pandrator.logic.dubbing.llm_correction import correct_srt_file_with_result

    from .web_research import evidence_prompt

    if cancel_event.is_set():
        raise RuntimeError("Subtitle correction was canceled.")
    session_id = str(payload.get("session_id") or "")
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
    session_dir = context._operation_dir(session_id, "correct")
    # Resolve once: construction and the run ledger share these exact values.
    run_passage_effective, run_passage_revision = context._resolve_run_passage_settings(
        session_id, payload.get("settings"), database=context.database
    )
    processing_path, input_passages, speaker_by_subtitle = context._prepare_passage_input(
        source_artifact,
        source_path,
        session_dir,
        source_passage_settings=run_passage_effective,
        source_passage_settings_revision=run_passage_revision,
    )
    requested_settings = dict(payload.get("settings") or {})
    if input_passages:
        requested_settings.update(
            {
                "_logical_passages_version": 1,
                "_logical_passage_display": context._passage_display_settings(session_id),
            }
        )
        requested_settings = context._source_passage_run_ledger(
            session_id,
            requested_settings,
            effective=run_passage_effective,
            settings_revision=run_passage_revision,
        )
    settings = context._with_database_llm_settings(requested_settings, "correction")
    settings["correction_style"] = context._normalize_correction_style(
        settings.get("correction_style")
    )
    requested_settings = {
        **requested_settings,
        "correction_style": settings["correction_style"],
    }
    requested_settings_hash = hashlib.sha256(
        json.dumps(
            requested_settings,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    base_instructions = str(payload.get("instructions") or settings.get("instructions") or "")
    run_store, agent_run, persist_checkpoint = context._begin_agentic_operation(
        payload=payload,
        kind="correction",
        source_artifact=source_artifact,
        requested_settings=requested_settings,
        instructions=base_instructions,
        usage_settings=settings,
    )
    research_result = None
    try:
        research_result = context._run_stage_web_research(
            stage="correction",
            session_id=session_id,
            source_artifact=source_artifact,
            source_path=source_path,
            settings=settings,
            progress=progress,
            cancel_event=cancel_event,
            completed_units=agent_run.completed_units,
            persist_checkpoint=persist_checkpoint,
        )
        instructions = base_instructions
        if research_result is not None:
            instructions += evidence_prompt(
                research_result.evidence,
                stage="correction",
            )
        processing_start = 0.2 if research_result is not None else 0.05
        progress(processing_start, "Preparing subtitle correction requests")
        result = correct_srt_file_with_result(
            session_dir,
            processing_path,
            settings,
            correction_instructions=instructions,
            cancel_event=cancel_event,
            speaker_by_subtitle=speaker_by_subtitle,
            completed_units=agent_run.completed_units,
            on_unit_completed=lambda key, output: persist_checkpoint(
                key,
                output,
                phase="correction",
                usage_stage="correction",
                usage_settings=settings,
            ),
            progress_callback=context._scaled_progress_callback(
                progress,
                processing_start,
                0.9,
            ),
        )
        if cancel_event.is_set():
            raise RuntimeError("Subtitle correction was canceled.")
        progress(0.92, "Correction requests complete; preparing artifact")
        if cancel_event.is_set():
            raise RuntimeError("Subtitle correction was canceled.")
        logical_output, display_speakers = context._render_passage_output(
            source_artifact,
            result,
            input_passages,
            settings,
            str(settings.get("original_language") or settings.get("source_language") or ""),
        )
        settings_fingerprint = context._stage_settings_fingerprint("correct", settings)
        if cancel_event.is_set():
            raise RuntimeError("Subtitle correction was canceled.")
        artifact = context.artifacts.register(
            Path(result.output_path),
            kind="srt",
            role="correction_candidate",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={
                "source_artifact_id": source_artifact.id,
                "source_content_hash": source_artifact.content_hash,
                "requested_settings_hash": requested_settings_hash,
                "settings_fingerprint": settings_fingerprint,
                "model": settings_fingerprint["model"],
                "language": str(
                    settings.get("original_language") or settings.get("source_language") or "auto"
                ),
                "agent_run_id": agent_run.id,
                **(
                    {"research": context._research_metadata(research_result, agent_run.id)}
                    if research_result is not None
                    else {}
                ),
            },
        )
        progress(0.97, "Registering corrected subtitle document")
        if cancel_event.is_set():
            raise RuntimeError("Subtitle correction was canceled.")
        context._store_srt_document(
            session_id,
            artifact,
            "correction",
            language=str(settings.get("original_language") or settings.get("source_language") or "")
            or None,
            parent_artifact=source_artifact,
            speaker_overrides=display_speakers,
            logical_passages=logical_output,
        )
        if cancel_event.is_set():
            raise RuntimeError("Subtitle correction was canceled.")
        registration = context.artifacts.prepare_registration(
            Path(result.output_path), settings=settings
        )
        with context.database.session() as session:
            if cancel_event.is_set():
                raise RuntimeError("Subtitle correction was canceled.")
            # Promote the native receipt without replacing its metadata.
            artifact = context.artifacts.register_in_session(
                session,
                Path(result.output_path),
                kind="srt",
                role="correction",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
                _prepared=registration,
            )
            if cancel_event.is_set():
                raise RuntimeError("Subtitle correction was canceled.")
        run_store.finish(agent_run.id, artifact_id=artifact.id)
    except Exception as error:
        run_store.fail(agent_run.id, error, interrupted=cancel_event.is_set())
        raise
    progress(1.0, "Correction ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "cost": result.cost,
        "agent_run_id": agent_run.id,
        "resumed": agent_run.resumed,
    }


def translate(context: SubtitleTransformContext, payload, progress, cancel_event):
    from pandrator.logic.dubbing.llm_translation import (
        normalize_glossary,
        translate_srt_file_deepl_with_result,
        translate_srt_file_with_result,
    )

    from .web_research import evidence_prompt

    if cancel_event.is_set():
        raise RuntimeError("Subtitle translation was canceled.")
    session_id = str(payload.get("session_id") or "")
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
    session_dir = context._operation_dir(session_id, "translate")
    # Resolve once: construction and the run ledger share these exact values.
    run_passage_effective, run_passage_revision = context._resolve_run_passage_settings(
        session_id, payload.get("settings"), database=context.database
    )
    processing_path, input_passages, speaker_by_subtitle = context._prepare_passage_input(
        source_artifact,
        source_path,
        session_dir,
        source_passage_settings=run_passage_effective,
        source_passage_settings_revision=run_passage_revision,
    )
    requested_settings = dict(payload.get("settings") or {})
    if input_passages:
        requested_settings.update(
            {
                "_logical_passages_version": 1,
                "_logical_passage_display": context._passage_display_settings(session_id),
            }
        )
        requested_settings = context._source_passage_run_ledger(
            session_id,
            requested_settings,
            effective=run_passage_effective,
            settings_revision=run_passage_revision,
        )
    requested_settings_hash = hashlib.sha256(
        json.dumps(
            requested_settings,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    settings = requested_settings
    research_result = None
    run_store = None
    agent_run = None
    base_instructions = str(payload.get("instructions") or settings.get("instructions") or "")
    translation_backend = str(
        settings.get("translation_backend") or settings.get("backend") or "llm"
    ).lower()
    if translation_backend == "deepl" and bool(settings.get("web_research_enabled")):
        raise ValueError(
            "Web research currently augments LLM translation only. "
            "Choose the LLM backend or turn web research off."
        )
    if translation_backend == "deepl":
        processing_start = 0.05
        progress(processing_start, "Preparing DeepL translation requests")
        credential = context._resolve_secret_reference(
            context.database,
            context.paths,
            context._database_reference(context._auxiliary_credential_key("deepl")),
            fallback_environment_variable="DEEPL_API_KEY",
        )
        result = translate_srt_file_deepl_with_result(
            session_dir,
            processing_path,
            settings,
            auth_key=credential.resolved_value(),
            speaker_by_subtitle=speaker_by_subtitle,
            cancel_event=cancel_event,
            progress_callback=context._scaled_progress_callback(
                progress,
                processing_start,
                0.9,
            ),
        )
    else:
        settings = context._with_database_llm_settings(settings, "translation")
        run_store, agent_run, persist_checkpoint = context._begin_agentic_operation(
            payload=payload,
            kind="translation",
            source_artifact=source_artifact,
            requested_settings=requested_settings,
            instructions=base_instructions,
            usage_settings=settings,
        )
        try:
            research_result = context._run_stage_web_research(
                stage="translation",
                session_id=session_id,
                source_artifact=source_artifact,
                source_path=source_path,
                settings=settings,
                progress=progress,
                cancel_event=cancel_event,
                completed_units=agent_run.completed_units,
                persist_checkpoint=persist_checkpoint,
            )
            instructions = base_instructions
            if research_result is not None:
                instructions += evidence_prompt(
                    research_result.evidence,
                    stage="translation",
                )
            from .knowledge import KnowledgeLedgerStore

            glossary_store = KnowledgeLedgerStore(context.database)
            manual_glossary = normalize_glossary(settings.get("glossary"))
            if manual_glossary:
                glossary_store.merge_glossary(
                    session_id,
                    source_language=str(
                        settings.get("original_language")
                        or settings.get("source_language")
                        or "auto"
                    ),
                    target_language=str(settings.get("target_language") or ""),
                    entries=[
                        {"source": source, "target": target}
                        for source, target in manual_glossary.items()
                    ],
                    origin="manual",
                    locked=True,
                )
            glossary_payload = glossary_store.get(
                session_id,
                "glossary",
                source_language=str(
                    settings.get("original_language") or settings.get("source_language") or "auto"
                ),
                target_language=str(settings.get("target_language") or ""),
            )["payload"]
            glossary_seed = [
                dict(item)
                for item in glossary_payload.get("entries", [])
                if isinstance(item, dict) and str(item.get("status") or "active") != "disabled"
            ]
            if research_result is not None:
                glossary_seed.extend(research_result.glossary)
            processing_start = 0.2 if research_result is not None else 0.05
            progress(processing_start, "Preparing subtitle translation requests")
            result = translate_srt_file_with_result(
                session_dir,
                processing_path,
                settings,
                translation_instructions=instructions,
                glossary=glossary_seed,
                cancel_event=cancel_event,
                speaker_by_subtitle=speaker_by_subtitle,
                completed_units=agent_run.completed_units,
                on_unit_completed=lambda key, output: persist_checkpoint(
                    key,
                    output,
                    phase="translation",
                    usage_stage="translation",
                    usage_settings=settings,
                ),
                progress_callback=context._scaled_progress_callback(
                    progress,
                    processing_start,
                    0.9,
                ),
            )
            if cancel_event.is_set():
                raise RuntimeError("Subtitle translation was canceled.")
            if result.glossary:
                glossary_store.merge_glossary(
                    session_id,
                    source_language=str(
                        settings.get("original_language")
                        or settings.get("source_language")
                        or "auto"
                    ),
                    target_language=str(settings.get("target_language") or ""),
                    entries=[
                        {"source": source, "target": target}
                        for source, target in result.glossary.items()
                    ],
                    origin="translation",
                )
        except Exception as error:
            run_store.fail(agent_run.id, error, interrupted=cancel_event.is_set())
            raise
    if cancel_event.is_set():
        cancel_error = RuntimeError("Subtitle translation was canceled.")
        if run_store is not None and agent_run is not None:
            run_store.fail(agent_run.id, cancel_error, interrupted=True)
        raise cancel_error
    try:
        progress(0.92, "Translation requests complete; preparing artifact")
        if cancel_event.is_set():
            raise RuntimeError("Subtitle translation was canceled.")
        logical_output, display_speakers = context._render_passage_output(
            source_artifact,
            result,
            input_passages,
            settings,
            str(settings.get("target_language") or ""),
        )
        settings_fingerprint = context._stage_settings_fingerprint("translate", settings)
        if cancel_event.is_set():
            raise RuntimeError("Subtitle translation was canceled.")
        artifact = context.artifacts.register(
            Path(result.output_path),
            kind="srt",
            role="translation_candidate",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={
                "source_artifact_id": source_artifact.id,
                "source_content_hash": source_artifact.content_hash,
                "requested_settings_hash": requested_settings_hash,
                "settings_fingerprint": settings_fingerprint,
                "backend": settings_fingerprint["backend"],
                "model": settings_fingerprint["model"],
                "language": settings_fingerprint["target_language"],
                **({"agent_run_id": agent_run.id} if agent_run is not None else {}),
                **(
                    {"research": context._research_metadata(research_result, agent_run.id)}
                    if research_result is not None and agent_run is not None
                    else {}
                ),
            },
        )
        progress(0.97, "Registering translated subtitle document")
        if cancel_event.is_set():
            raise RuntimeError("Subtitle translation was canceled.")
        context._store_srt_document(
            session_id,
            artifact,
            "translation",
            language=str(settings.get("target_language") or "") or None,
            parent_artifact=source_artifact,
            speaker_overrides=display_speakers,
            logical_passages=logical_output,
        )
        if cancel_event.is_set():
            raise RuntimeError("Subtitle translation was canceled.")
        registration = context.artifacts.prepare_registration(
            Path(result.output_path), settings=settings
        )
        with context.database.session() as session:
            if cancel_event.is_set():
                raise RuntimeError("Subtitle translation was canceled.")
            # Promote the native receipt without replacing its metadata.
            artifact = context.artifacts.register_in_session(
                session,
                Path(result.output_path),
                kind="srt",
                role="translation",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
                _prepared=registration,
            )
            if cancel_event.is_set():
                raise RuntimeError("Subtitle translation was canceled.")
        if run_store is not None and agent_run is not None:
            run_store.finish(agent_run.id, artifact_id=artifact.id)
        else:
            context._record_usage(
                session_id,
                "translation",
                settings,
                result,
                job_id=str(payload.get("_job_id") or "") or None,
                artifact_id=artifact.id,
            )
    except Exception as error:
        if run_store is not None and agent_run is not None:
            run_store.fail(agent_run.id, error, interrupted=cancel_event.is_set())
        raise
    progress(1.0, "Translation ready")
    return {
        "artifact_id": artifact.id,
        "path": artifact.relative_path,
        "cost": result.cost,
        **(
            {
                "agent_run_id": agent_run.id,
                "resumed": agent_run.resumed,
            }
            if agent_run is not None
            else {}
        ),
    }
