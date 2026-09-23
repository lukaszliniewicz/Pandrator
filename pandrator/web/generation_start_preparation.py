"""Database-only guard for generation snapshots compiled before the write lock."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m


def snapshot_guard(session: Session, session_id: str, revision_id: str) -> str:
    from .generation_controls import get_generation_controls
    from .settings_policy import stable_hash
    from .speech_plan_workspace import plan_signature

    revision = session.get(m.GenerationPlanRevision, revision_id)
    record = session.get(m.SessionRecord, session_id)
    review = session.get(m.SpeechPlanReview, revision_id)
    performance = session.execute(select(
        m.PerformancePlan.id, m.PerformancePlan.version, m.PerformancePlan.status,
    ).where(m.PerformancePlan.plan_revision_id == revision_id).order_by(m.PerformancePlan.id))
    # Voice references can be selected by provider name as well as managed ID.
    # Compare library metadata, without hashing or opening any media files here.
    voices = session.execute(select(
        m.Voice.id, m.Voice.name, m.Voice.revision, m.Voice.metadata_json,
    ).order_by(m.Voice.id))
    samples = session.execute(select(
        m.VoiceSample.id, m.VoiceSample.voice_id, m.VoiceSample.transcript,
        m.VoiceSample.transcript_reviewed, m.VoiceSample.transcript_language,
        m.Artifact.id, m.Artifact.content_hash, m.Artifact.state, m.Artifact.relative_path,
    ).join(m.Artifact, m.Artifact.id == m.VoiceSample.artifact_id).order_by(m.VoiceSample.id))
    return stable_hash({
        "session_revision": record.revision if record else None,
        "plan": plan_signature(session, revision_id),
        "revision": [revision.settings_json, revision.operation_json] if revision else None,
        "review": review.content_hash if review else None,
        "controls": get_generation_controls(session, session_id),
        "performance": [tuple(row) for row in performance],
        "voices": [tuple(row) for row in voices],
        "samples": [tuple(row) for row in samples],
    })
