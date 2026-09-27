"""Durable, all-or-nothing optimization of prepared speech units."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from . import models as m
from .credentials import is_sensitive_field
from .provider_settings import build_llm_settings
from .settings_policy import RevisionConflict
from .speech_plan_workspace import (
    preparation_guard,
    prepare_speech_plan,
    prepare_speech_plan_data,
    selected_text,
)


def _job_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Persist the resolved controls, never credentials or provider configs."""
    return {key: deepcopy(value) for key, value in settings.items() if not is_sensitive_field(key)}


def resolve_speech_model(services, settings: dict[str, Any]) -> str:
    """Resolve a default before enqueue, without making an inference request."""
    if bool(settings.get("llm_tts_document_optimization")):
        raise ValueError("Document and final-unit speech optimization cannot both run for one preparation. Review the document optimization first, then prepare the speech plan without final-unit optimization.")
    if str(settings.get("llm_tts_annotation_mode") or "off").strip().lower() != "off":
        raise ValueError("Dialogue or speaker annotation must be completed and reviewed before final-unit speech optimization.")
    requested = str(settings.get("tts_optimization_model") or settings.get("llm_model") or "").strip()
    if requested == "default":
        requested = ""
    _llm_settings, model = build_llm_settings(
        services.database, services.paths, requested_model=requested,
        request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
    )
    return model


def enqueue_speech_preparation(
    services,
    session,
    session_id: str,
    *,
    expected_revision: int,
    expected_plan_revision_id: str | None,
    source_artifact_id: str,
    settings: dict[str, Any],
    model: str,
    guard: str,
    selected_artifact_id: str,
) -> dict[str, Any]:
    """Reserve one worker job inside the route's idempotent write transaction."""
    from sqlalchemy import select

    from .source_management import assert_session_idle

    record = session.get(m.SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    if record.revision != expected_revision or preparation_guard(session, session_id) != guard:
        raise RevisionConflict("The selected text or settings changed. Refresh and try again.")
    if selected_artifact_id != source_artifact_id:
        raise RevisionConflict("The selected text changed. Review its version before preparing a plan.")
    source = session.get(m.Artifact, source_artifact_id)
    if source is None or source.session_id != session_id:
        raise ValueError("The planning input must belong to this session.")
    if source.role == "tts_optimized":
        raise ValueError("The selected text has already been optimized at document level. Review that text and prepare without final-unit optimization.")
    assert_session_idle(session, session_id)
    plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id))
    if (plan.active_revision_id if plan else None) != expected_plan_revision_id:
        raise RevisionConflict("The selected speech plan changed. Refresh before rebuilding it.")
    if bool(settings.get("llm_tts_document_optimization")):
        raise ValueError("Document and final-unit speech optimization cannot both run for one preparation. Review the document optimization first, then prepare the speech plan without final-unit optimization.")
    if str(settings.get("llm_tts_annotation_mode") or "off").strip().lower() != "off":
        raise ValueError("Dialogue or speaker annotation must be completed and reviewed before final-unit speech optimization.")
    frozen = _job_settings(settings)
    frozen["tts_optimization_model"] = model
    job = services.jobs.enqueue_in_session(
        session,
        "speech.prepare",
        {
            "session_id": session_id,
            "source_artifact_id": source_artifact_id,
            "expected_revision": expected_revision,
            "expected_plan_revision_id": expected_plan_revision_id,
            "guard": guard,
            "settings": frozen,
            "model": model,
        },
        session_id=session_id,
        resource_keys=[f"session:{session_id}"],
    )
    job.payload_json = {**job.payload_json, "job_id": job.id}
    session.flush()
    return {"job_id": job.id, "status": "queued", "synthesis_started": False}


def run_speech_preparation(handlers, payload: dict[str, Any], progress, cancel_event) -> dict[str, Any]:
    """Compute outside the write lock, then atomically select a validated plan."""
    from .pronunciations import (
        PronunciationLibrary,
        apply_reviewed_pronunciations,
        normalize_backend,
    )
    from .tts_optimization import OptimizationUsage, optimize_texts
    from .workflows import WorkflowService
    from .workspace_settings import WorkspaceSettingsService

    services = SimpleNamespace(
        database=handlers.database,
        paths=handlers.paths,
        jobs=handlers.jobs,
        workflow_handlers=handlers,
        workflows=WorkflowService(handlers.database, handlers.jobs),
        workspace_settings=WorkspaceSettingsService(handlers.database),
    )
    session_id = str(payload["session_id"])
    job_id = str(payload["job_id"])
    settings = dict(payload["settings"])
    model = str(payload["model"])
    if not model or settings.get("tts_optimization_model") != model or not settings.get("llm_tts_optimization"):
        raise ValueError("The queued speech optimization configuration is invalid.")
    if cancel_event.is_set():
        return {}
    current = selected_text(services, session_id)
    if not current or current["artifact_id"] != payload["source_artifact_id"]:
        raise RevisionConflict("The selected text changed before speech preparation.")
    with handlers.database.session() as session:
        if preparation_guard(session, session_id) != payload["guard"]:
            raise RevisionConflict("The selected text or settings changed before speech preparation.")
    prepared = prepare_speech_plan_data(
        services, session_id, str(payload["source_artifact_id"]),
        frozen_settings=settings, expected_guard=str(payload["guard"]),
    )
    records = prepared["records"]
    positions = [
        index for index, row in enumerate(records)
        if str(row.get("text") or row.get("original_sentence") or "").strip()
    ]
    if not positions:
        raise ValueError("No final speech units remain for optimization.")
    texts = [
        str(records[index].get("tts_optimized_sentence") or records[index].get("text") or records[index].get("original_sentence") or "").strip()
        for index in positions
    ]
    languages = [str(records[index].get("language") or settings.get("language") or "en") for index in positions]
    voice_languages = [
        str(records[index].get("voice_language") or settings.get("voice_language") or languages[local_index])
        for local_index, index in enumerate(positions)
    ]
    apply_reviewed = settings.get("apply_reviewed_pronunciations", True) is not False
    backend = normalize_backend(
        settings.get("service") or settings.get("tts_service") or settings.get("backend") or "*"
    )
    library = PronunciationLibrary(handlers.database)
    known = [
        library.resolve(text, session_id=session_id, language=languages[index], backend=backend)
        if apply_reviewed else []
        for index, text in enumerate(texts)
    ]

    def resolve_known(text: str, language: str) -> list[dict[str, Any]]:
        for index, source_text in enumerate(texts):
            if source_text == text and languages[index] == language:
                return deepcopy(known[index])
        return []

    llm_settings, resolved_model = build_llm_settings(
        handlers.database, handlers.paths, requested_model=model,
        request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
    )
    if resolved_model != model:
        raise RevisionConflict("The queued speech optimization model is no longer available.")
    plans: dict[int, dict[str, Any]] = {}
    captured_usage: dict[str, dict[str, Any]] = {}

    def save_plans(batch):
        for index, _text, plan in batch:
            if index in plans:
                raise ValueError("The optimizer returned a duplicate speech unit.")
            plans[index] = dict(plan)

    def capture_usage(key: str, value: dict[str, Any]) -> None:
        captured_usage[key] = dict(value)

    progress(0.05, "Optimizing final speech units")
    usage_settings = {**settings, "tts_optimization_model": model, "llm_provider_configs": llm_settings.provider_configs}
    try:
        optimized, usage = optimize_texts(
            texts, settings, llm_settings, model, cancel_event,
            lambda fraction, detail: progress(0.05 + 0.85 * fraction, detail),
            on_plan_batch=save_plans,
            known_pronunciation_resolver=resolve_known if apply_reviewed else None,
            languages=languages,
            voice_languages=voice_languages,
            on_unit_completed=capture_usage,
        )
        handlers._record_usage(session_id, "tts_optimization", usage_settings, usage, job_id=job_id)
        captured_usage.clear()
    finally:
        for key, raw in captured_usage.items():
            handlers._record_usage(
                session_id, "tts_optimization", usage_settings,
                OptimizationUsage(
                    cost=float(raw.get("cost") or 0),
                    response_count=int(raw.get("response_count") or 0),
                    usage=dict(raw.get("usage") or {}),
                    cost_sources=list(raw.get("cost_sources") or []),
                ),
                job_id=job_id,
                request_key=key,
            )
    if cancel_event.is_set():
        return {}
    if len(optimized) != len(positions):
        raise ValueError("The optimizer returned a different number of speech units.")
    updated = deepcopy(records)
    for local_index, (index, revised) in enumerate(zip(positions, optimized, strict=True)):
        if not isinstance(revised, str) or not revised.strip():
            raise ValueError(f"The optimizer returned an empty speech unit at position {index + 1}.")
        if apply_reviewed and str(settings.get("speech_optimization_mode") or "").strip().lower() not in {"guarded", "flexible"}:
            revised = apply_reviewed_pronunciations(revised, known[local_index])
        row = updated[index]
        source_markup = row.get("speech_xml") or (row.get("speech_plan") or {}).get("speech_xml")
        if source_markup and revised.strip() != texts[local_index]:
            raise ValueError("Speech markup anchors would no longer match optimized wording. Optimize and review the document before preparing this speech plan.")
        row["tts_optimized_sentence"] = revised.strip()
        if local_index in plans:
            row["speech_plan"] = {**dict(row.get("speech_plan") or {}), **plans[local_index], "model": model}
    prepared["records"] = updated
    prepared["settings"] = {
        **settings,
        "llm_tts_optimization": False,
        "_prepared_for_review": True,
        "_final_unit_optimization_model": model,
        "_final_unit_optimization_settings": {
            key: value for key, value in settings.items()
            if key.startswith(("llm_tts_", "speech_optimization_", "speech_plan_"))
            or key in {
                "tts_optimization_model", "llm_concurrent_calls", "llm_multi_stage",
                "combined_prompt", "first_prompt", "second_prompt", "third_prompt",
                "apply_reviewed_pronunciations",
            }
        },
    }
    if cancel_event.is_set():
        return {}
    progress(0.95, "Saving optimized speech plan")
    with handlers.database.immediate_session() as session:
        if cancel_event.is_set():
            return {}
        result = prepare_speech_plan(
            services, session, session_id,
            expected_revision=int(payload["expected_revision"]),
            expected_plan_revision_id=payload.get("expected_plan_revision_id"),
            prepared=prepared, owning_job_id=job_id,
        )
    progress(1.0, "Optimized speech plan ready for review")
    return result
