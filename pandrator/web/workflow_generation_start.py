"""Reviewable generation plan and run start with explicit dependencies."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .credentials import redact_inline_secrets
from .database import Database
from .jobs import JobQueue
from .models import Artifact, GenerationPlan, GenerationPlanRevision, GenerationRun, utcnow
from .workflow_generation_binding import GenerationPlanStoreProtocol

Progress = Callable[[float, str | None], None]


@dataclass(frozen=True, slots=True)
class GenerationStartContext:
    database: Database
    _resolve_input: Callable[[str], tuple[Artifact, Path]]
    _generation_language: Callable[[str, Artifact, dict[str, Any]], str]
    _materialize_subtitle_generation_plan: Callable[[str, Artifact, Path, dict[str, Any], str], str]
    _store_generation_plan: GenerationPlanStoreProtocol
    run_generation: Callable[[dict[str, Any], Progress, threading.Event], dict[str, Any]]
    _secret_free_tts_settings: Callable[[dict[str, Any]], dict[str, Any]]


def run_reviewable_generation(
    context: GenerationStartContext,
    payload: dict[str, Any],
    progress: Progress,
    cancel_event: threading.Event,
    *,
    resolved_snapshot: Any = None,
    settings_hash: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Create/resolve the segment plan and generate takes without assembly.

        The workflow card and the generation drawer must describe the same
        operation.  The former compatibility path generated a combined WAV in
        the workflow job, leaving no GenerationRun for the drawer to observe.
        This boundary deliberately stops after immutable per-segment takes;
        output assembly remains an explicit review action.
        """
    session_id = str(payload.get("session_id") or "")
    source_artifact, source_path = context._resolve_input(
        str(payload.get("source_artifact_id") or "")
    )
    settings = dict(payload.get("settings") or {})
    language = context._generation_language(session_id, source_artifact, settings)
    settings = {**settings, "language": language, "target_language": language}
    top_level_revision_id = str(
        payload.get("speech_plan_revision_id") or ""
    ).strip()
    settings_revision_id = str(
        settings.get("speech_plan_revision_id") or ""
    ).strip()
    from .settings_policy import RevisionConflict

    if (
        top_level_revision_id
        and settings_revision_id
        and top_level_revision_id != settings_revision_id
    ):
        raise RevisionConflict(
            "The requested speech plan revision conflicts with the selected revision."
        )
    expected_revision_id = top_level_revision_id or settings_revision_id

    def assert_current_revision(session, revision_id: str):
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
            or plan.active_revision_id != revision_id
        ):
            raise RevisionConflict(
                "The selected speech plan revision is no longer available in this session."
            )
        revision_settings = (
            revision.settings_json
            if isinstance(revision.settings_json, dict)
            else {}
        )
        planned_source_id = str(
            revision_settings.get("_source_artifact_id") or ""
        )
        if planned_source_id and planned_source_id != source_artifact.id:
            raise RevisionConflict(
                "The selected speech plan revision belongs to a different generation input."
            )
        return revision

    if expected_revision_id:
        settings["speech_plan_revision_id"] = expected_revision_id
        with context.database.session() as session:
            assert_current_revision(session, expected_revision_id)
    progress(0.0, "Preparing generation segments")

    plan_revision_id: str | None = None
    if source_path.suffix.lower() == ".srt":
        plan_revision_id = context._materialize_subtitle_generation_plan(
            session_id,
            source_artifact,
            source_path,
            settings,
            language,
        )
    elif source_path.suffix.lower() == ".json":
        # Segment narration already creates a plan. Preserve any edits the
        # user made in the drawer. A separately reviewed optimization
        # artifact, however, is a new source and therefore a new plan.
        with context.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == session_id
                )
            )
            if plan is not None and (
                source_artifact.role == "prepared_text" or expected_revision_id
            ):
                active_revision_id = str(plan.active_revision_id or "")
                if active_revision_id:
                    assert_current_revision(session, active_revision_id)
                    if (
                        expected_revision_id
                        and active_revision_id != expected_revision_id
                    ):
                        raise RevisionConflict(
                            "The selected speech plan revision changed before JSON narration reuse."
                        )
                    plan_revision_id = active_revision_id
        if not plan_revision_id:
            records = json.loads(source_path.read_text(encoding="utf-8-sig"))
            if not isinstance(records, list) or not records:
                raise ValueError("No narration segments were found.")
            plan_revision_id, _ = context._store_generation_plan(
                session_id,
                records,
                settings=settings,
                source_revision_id=str(
                    (source_artifact.metadata_json or {}).get("revision_id") or ""
                )
                or None,
                source_artifact_id=source_artifact.id,
            )
    else:
        raise ValueError(
            "Audio generation requires subtitle cues or segmented narration."
        )

    if not plan_revision_id:
        raise ValueError(
            "Create generation segments before starting audio generation."
        )

    snapshot = (
        deepcopy(resolved_snapshot) if isinstance(resolved_snapshot, dict) else {}
    )
    snapshot = redact_inline_secrets(context._secret_free_tts_settings(snapshot))
    # The resolved sections are the immutable source of truth. Merge the
    # flattened stage values as compatibility aliases so direct Run Now
    # choices (service, model, voice, and language) cannot be lost.
    safe_settings = redact_inline_secrets(context._secret_free_tts_settings(settings))
    snapshot["tts"] = {**dict(snapshot.get("tts") or {}), **safe_settings}
    snapshot["audio"] = {**dict(snapshot.get("audio") or {}), **safe_settings}
    snapshot["text"] = {
        **dict(snapshot.get("text") or {}),
        "llm_tts_optimization": bool(settings.get("llm_tts_optimization")),
        "apply_reviewed_pronunciations": settings.get(
            "apply_reviewed_pronunciations", True
        ),
        "use_existing_speech_plans": source_artifact.role == "tts_optimized",
    }
    snapshot["source_artifact_id"] = source_artifact.id
    snapshot["speech_plan_revision_id"] = plan_revision_id
    frozen_hash = hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    with context.database.immediate_session() as session:
        assert_current_revision(session, plan_revision_id)
        if expected_revision_id and expected_revision_id != plan_revision_id:
            raise RevisionConflict(
                "The selected speech plan revision changed before workflow generation could start."
            )
        from .speech_plan_workspace import freeze_speech_snapshot

        freeze_speech_snapshot(session, plan_revision_id, snapshot, explicit=bool(expected_revision_id))
        from .generation_audio_identity import plan_audio_identities

        snapshot["generation_audio_identities"] = plan_audio_identities(session, plan_revision_id, snapshot)
        frozen_hash = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        sequence_number = (
            int(
                session.scalar(
                    select(func.max(GenerationRun.sequence_number)).where(
                        GenerationRun.session_id == session_id
                    )
                )
                or 0
            )
            + 1
        )
        run = GenerationRun(
            session_id=session_id,
            plan_revision_id=plan_revision_id,
            job_id=job_id,
            sequence_number=sequence_number,
            operation="generate",
            status="queued",
            settings_snapshot_json=snapshot,
            settings_hash=frozen_hash or settings_hash,
        )
        session.add(run)
        session.flush()
        run_id = run.id

    progress(0.03, "Generation segments ready")
    try:
        return context.run_generation(
            {
                "generation_run_id": run_id,
                "segment_ids": [],
                "operation": "generate",
            },
            lambda value, detail=None: progress(
                0.03 + max(0.0, min(1.0, float(value))) * 0.97, detail
            ),
            cancel_event,
        )
    except Exception:
        with context.database.immediate_session() as session:
            failed = session.get(GenerationRun, run_id)
            # A late reporting error must not replace a finished domain result.
            if failed is not None and failed.status in JobQueue.GENERATION_ACTIVE_STATUSES:
                failed.status = "failed"
                failed.updated_at = utcnow()
        raise
