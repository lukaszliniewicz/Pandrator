"""Read-only generation admission in the caller's workflow snapshot."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Artifact, GenerationPlan, GenerationPlanRevision, GenerationSegment


@dataclass(frozen=True, slots=True)
class GenerationPreflightContext:
    generation_language_for_source: Callable[[Session, str, Artifact, dict[str, Any]], str]
    preflight_error_type: Callable[[], type[ValueError]]


def validate_generation_language_payload(
    context: GenerationPreflightContext, session: Session, payload: dict[str, Any]
) -> None:
    """Preflight the exact selected language/model before a workflow enqueue.

        A pinned speech revision is checked through the same freezer used by the
        synthesis worker, but all settings are copied and the caller only reads
        database state. Unpinned multi-voice requests must first be prepared and
        reviewed because an assignment can select a different provider/model.
        """
    if str(payload.get("target_stage") or "") != "generate_audio":
        return

    settings = deepcopy(payload.get("settings") or {})
    if not isinstance(settings, dict):
        settings = {}
    top_level_revision_id = str(
        payload.get("speech_plan_revision_id") or ""
    ).strip()
    settings_revision_id = str(
        settings.get("speech_plan_revision_id") or ""
    ).strip()
    if (
        top_level_revision_id
        and settings_revision_id
        and top_level_revision_id != settings_revision_id
    ):
        raise context.preflight_error_type()(
            "The requested speech plan revision conflicts with the current selected revision."
        )
    revision_id = top_level_revision_id or settings_revision_id
    input_selected = bool(payload.get("_tts_language_preflight_input_selected"))
    if revision_id and not input_selected:
        raise context.preflight_error_type()(
            "Select or prepare the generation input before using an explicit speech plan revision."
        )
    if not input_selected and not revision_id:
        return

    source_id = str(payload.get("source_artifact_id") or "")
    source = session.get(Artifact, source_id) if source_id else None
    if source is None or not input_selected:
        raise context.preflight_error_type()(
            "Select or prepare the narration input before validating audio generation."
        )
    session_id = str(payload.get("session_id") or "")
    language = context.generation_language_for_source(
        session, session_id, source, settings
    )
    settings.update(language=language, target_language=language)

    resolved = payload.get("resolved_settings_snapshot")
    snapshot = deepcopy(resolved) if isinstance(resolved, dict) else {}
    safe_settings = deepcopy(settings)
    for key in (
        "audio_cpp_voice_ref",
        "audio_cpp_voice_ref_hash",
        "audio_cpp_reference_text",
    ):
        safe_settings.pop(key, None)
    snapshot["tts"] = {
        **dict(snapshot.get("tts") or {}),
        **safe_settings,
    }
    snapshot["audio"] = {
        **dict(snapshot.get("audio") or {}),
        **safe_settings,
    }
    snapshot["text"] = {
        **dict(snapshot.get("text") or {}),
        "llm_tts_optimization": bool(settings.get("llm_tts_optimization")),
        "apply_reviewed_pronunciations": settings.get(
            "apply_reviewed_pronunciations", True
        ),
        "use_existing_speech_plans": source.role == "tts_optimized",
    }
    snapshot["source_artifact_id"] = source.id

    if revision_id:
        plan = session.scalar(
            select(GenerationPlan).where(
                GenerationPlan.session_id == session_id
            )
        )
        revision = session.get(GenerationPlanRevision, revision_id)
        if (
            plan is None
            or revision is None
            or revision.plan_id != plan.id
        ):
            raise context.preflight_error_type()(
                "The requested speech plan revision is not available in this session."
            )
        if plan.active_revision_id != revision_id:
            raise context.preflight_error_type()(
                "The requested speech plan revision is no longer current. Refresh and select the current revision."
            )
        revision_settings = (
            revision.settings_json
            if isinstance(revision.settings_json, dict)
            else {}
        )
        planned_source_id = str(
            revision_settings.get("_source_artifact_id") or ""
        )
        if planned_source_id and planned_source_id != source.id:
            raise context.preflight_error_type()(
                "The requested speech plan revision belongs to a different generation input. Select the matching input or prepare a new speech plan."
            )
        snapshot["speech_plan_revision_id"] = revision_id
        from .speech_plan_workspace import freeze_speech_snapshot

        try:
            freeze_speech_snapshot(
                session,
                revision_id,
                snapshot,
                explicit=True,
            )
        except ValueError as error:
            raise context.preflight_error_type()(str(error)) from error
        return

    effective_tts = {
        **dict(snapshot.get("audio") or {}),
        **dict(snapshot.get("tts") or {}),
    }
    from .generation_rendering import is_strict_single_voice

    has_route_changing_voice = bool(effective_tts.get("casting_enabled"))
    plan = session.scalar(
        select(GenerationPlan).where(
            GenerationPlan.session_id == str(payload.get("session_id") or "")
        )
    )
    active_revision_id = str(plan.active_revision_id or "") if plan else ""
    if active_revision_id and not is_strict_single_voice(effective_tts):
        has_route_changing_voice = has_route_changing_voice or (
            session.scalar(
                select(GenerationSegment.id)
                .where(
                    GenerationSegment.plan_revision_id == active_revision_id,
                    GenerationSegment.removed.is_(False),
                    or_(
                        GenerationSegment.voice_id.is_not(None),
                        GenerationSegment.voice.is_not(None),
                    ),
                )
                .limit(1)
            )
            is not None
        )
    if has_route_changing_voice:
        raise context.preflight_error_type()(
            "Prepare and review the speech plan before generation; an unpinned "
            "voice assignment may use a different TTS model."
        )

    from pandrator.logic.tts_language_preflight import validate_tts_language

    try:
        validate_tts_language(effective_tts)
    except ValueError as error:
        raise context.preflight_error_type()(str(error)) from error
