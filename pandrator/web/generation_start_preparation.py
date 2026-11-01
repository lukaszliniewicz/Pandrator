"""Database-only guard for generation snapshots compiled before the write lock."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .access.contracts import AccessDenied, WorkspaceNotFound
from .access.current_session_policy import require_current_session_access
from .access.voice_policy import require_current_voice_library_read
from .projects.identity import session_project
from .voice_identity import voice_creation_workspace


def snapshot_guard(session: Session, session_id: str, revision_id: str) -> str:
    from .generation_controls import get_generation_controls
    from .settings_policy import stable_hash
    from .speech_plan_workspace import plan_signature

    require_current_session_access(session, session_id)
    workspace = voice_creation_workspace(
        session, workspace_id=session_project(session, session_id).workspace_id
    )
    revision = session.get(m.GenerationPlanRevision, revision_id)
    plan = session.get(m.GenerationPlan, revision.plan_id) if revision else None
    if plan is None or plan.session_id != session_id:
        raise ValueError("The speech plan does not belong to this session.")
    record = session.get(m.SessionRecord, session_id)
    review = session.get(m.SpeechPlanReview, revision_id)
    performance = session.execute(select(
        m.PerformancePlan.id, m.PerformancePlan.version, m.PerformancePlan.status,
    ).where(m.PerformancePlan.plan_revision_id == revision_id).order_by(m.PerformancePlan.id))
    # Voice references can be selected by provider name as well as managed ID.
    # Compare library metadata, without hashing or opening any media files here.
    try:
        require_current_voice_library_read(session, workspace.id)
    except (AccessDenied, WorkspaceNotFound):
        library_visible = False
    else:
        library_visible = True
    voices = session.execute(select(
        m.Voice.id, m.Voice.name, m.Voice.revision, m.Voice.metadata_json,
    ).where(m.Voice.workspace_id == workspace.id).order_by(m.Voice.id)) if library_visible else ()
    samples = session.execute(select(
        m.VoiceSample.id, m.VoiceSample.voice_id, m.VoiceSample.transcript,
        m.VoiceSample.transcript_reviewed, m.VoiceSample.transcript_language,
        m.Artifact.id, m.Artifact.content_hash, m.Artifact.state, m.Artifact.relative_path,
    ).join(m.Voice, m.Voice.id == m.VoiceSample.voice_id)
    .join(m.Artifact, m.Artifact.id == m.VoiceSample.artifact_id).where(
        m.Voice.workspace_id == workspace.id,
        m.VoiceSample.workspace_id == workspace.id,
        m.Artifact.workspace_id == workspace.id,
    ).order_by(m.VoiceSample.id)) if library_visible else ()
    # Project-owned retained selections remain usable by project credentials.
    # A caller without library authority never hashes its private source bodies;
    # any actual fresh source use has a separate required library proof.
    references = session.execute(select(
        m.ProjectSpeechReference.slot_key,
        m.ProjectSpeechReference.selector_hash,
        m.ProjectSpeechReference.binding_json,
        m.ProjectSpeechReference.input_artifact_id,
        m.ProjectSpeechReference.input_content_hash,
        m.ProjectSpeechReference.revision,
    ).where(m.ProjectSpeechReference.session_id == session_id).order_by(
        m.ProjectSpeechReference.slot_key,
    ))
    return stable_hash({
        "session_revision": record.revision if record else None,
        "plan": plan_signature(session, revision_id),
        "revision": [revision.settings_json, revision.operation_json] if revision else None,
        "review": review.content_hash if review else None,
        "controls": get_generation_controls(session, session_id),
        "performance": [tuple(row) for row in performance],
        "project_speech_references": [tuple(row) for row in references],
        "voice_library_visible": library_visible,
        "voices": [tuple(row) for row in voices],
        "samples": [tuple(row) for row in samples],
    })
