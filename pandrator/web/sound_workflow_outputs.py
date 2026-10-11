"""Apply independent sound plans at the final audio stage of a workflow."""

from __future__ import annotations

import threading
from typing import Any, Callable

from sqlalchemy import exists, select

from .models import Artifact
from .sound_project_models import ProjectSoundCue, ProjectSoundPlan
from .workflow_output_context import OutputWorkflowContext


def has_output_sounds(context: OutputWorkflowContext, session_id: str, settings: dict) -> bool:
    if settings.get("include_sounds", True) is False or getattr(context, "_render_sound_mix", None) is None:
        return False
    latest = select(ProjectSoundPlan.id).where(
        ProjectSoundPlan.session_id == session_id
    ).order_by(ProjectSoundPlan.revision.desc()).limit(1).scalar_subquery()
    with context.database.snapshot_session() as session:
        return bool(session.scalar(select(exists().where(
            ProjectSoundCue.plan_id == latest, ProjectSoundCue.included.is_(True),
        ))))


def render_output_sounds(
    context: OutputWorkflowContext, audio: Artifact, *, session_id: str,
    settings: dict[str, Any], progress: Callable, cancel_event: threading.Event,
    timeline_artifact_id: str | None = None,
) -> Artifact:
    """Keep the original speech/master artifact and publish a separate sound mix."""
    callback = getattr(context, "_render_sound_mix", None)
    if settings.get("include_sounds", True) is False or callback is None:
        return audio
    if context._session_record(session_id).workflow_kind == "subtitles":
        return audio
    if audio.role == "sound_mix.audio":
        if audio.state != "current":
            raise ValueError("The selected sound mix is stale. Render the current sound plan first.")
        return audio
    if not has_output_sounds(context, session_id, settings):
        return audio
    return callback(
        session_id, audio.id, timeline_artifact_id=timeline_artifact_id,
        progress=progress, cancel_event=cancel_event,
    )


def finish_sound_assembly(
    context: OutputWorkflowContext, result: dict[str, Any], payload: dict[str, Any],
    progress: Callable, cancel_event: threading.Event,
) -> dict[str, Any]:
    """A pending cue must not discard successfully assembled speech."""
    if not result.get("artifact_id") or getattr(context, "_render_sound_mix", None) is None:
        return result
    audio, _path = context._resolve_input(result["artifact_id"])
    # Timed speech is mixed after the source soundtrack master, during export.
    if context._session_record(str(audio.session_id)).workflow_kind != "audiobook":
        return result
    settings = dict(payload.get("settings") or {})
    if settings.get("include_sounds", True) is False:
        return result
    try:
        mixed = render_output_sounds(
            context, audio, session_id=str(audio.session_id), settings=settings,
            progress=lambda value, detail: progress(0.96 + value * 0.04, detail),
            cancel_event=cancel_event,
        )
    except ValueError as error:
        return {**result, "sound_mix_status": "not_rendered", "sound_mix_error": str(error)}
    if mixed.id == audio.id:
        return result
    return {**result, "sound_mix_artifact_id": mixed.id, "sound_mix_status": "ready"}
