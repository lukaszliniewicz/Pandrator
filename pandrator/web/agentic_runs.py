"""Durable checkpoints for correction, translation, and research work."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .database import Database
from .models import AgentRun, AgentStep, Artifact, Job, UsageEvent, new_id, utcnow
from .usage_identity import usage_creation_ownership


def stable_payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def operation_settings_snapshot(
    *,
    kind: str,
    requested_settings: dict[str, Any],
    instructions: str,
    usage_settings: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    resolved_execution = {
        "model": str(
            usage_settings.get(f"{kind}_model")
            or usage_settings.get("model_name")
            or usage_settings.get("llm_default_model")
            or ""
        ),
        "backend": str(
            usage_settings.get("translation_backend")
            or usage_settings.get("backend")
            or "llm"
        ),
        "reasoning_effort": str(
            usage_settings.get("reasoning_effort")
            or usage_settings.get(f"{kind}_reasoning_effort")
            or ""
        ),
    }
    settings_hash = stable_payload_hash(
        {
            "kind": kind,
            "settings": requested_settings,
            "instructions": instructions,
            "resolved_execution": resolved_execution,
        }
    )
    stored_settings = {
        "settings": requested_settings,
        "instructions": instructions,
        "resolved_execution": resolved_execution,
    }
    return settings_hash, stored_settings


@dataclass(frozen=True, slots=True)
class ResumableAgentRun:
    id: str
    resumed: bool
    completed_units: dict[str, dict[str, Any]]


class AgenticRunStore:
    """Transactionally retain every accepted unit of an expensive operation."""

    RESUMABLE_STATUSES: ClassVar[set[str]] = {
        "failed",
        "interrupted",
        "retrying",
        "running",
    }

    def __init__(self, database: Database):
        self.database = database

    def start(
        self,
        *,
        kind: str,
        session_id: str,
        source_artifact: Artifact,
        settings_hash: str,
        settings: Mapping[str, Any],
        job_id: str | None,
        requested_run_id: str | None = None,
    ) -> ResumableAgentRun:
        with self.database.session() as session:
            return self.start_in_session(
                session,
                kind=kind,
                session_id=session_id,
                source_artifact=source_artifact,
                settings_hash=settings_hash,
                settings=settings,
                job_id=job_id,
                requested_run_id=requested_run_id,
            )

    def start_in_session(
        self,
        session: Session,
        *,
        kind: str,
        session_id: str,
        source_artifact: Artifact,
        settings_hash: str,
        settings: Mapping[str, Any],
        job_id: str | None,
        requested_run_id: str | None = None,
    ) -> ResumableAgentRun:
        source_hash = str(source_artifact.content_hash or "")
        run: AgentRun | None = None
        if requested_run_id:
            run = session.get(AgentRun, requested_run_id)
            if run is None or run.session_id != session_id or run.kind != kind:
                raise ValueError(
                    "The requested resumable operation does not exist."
                )
            if run.status not in {"failed", "interrupted", "retrying"}:
                raise ValueError(
                    "Only a failed or interrupted operation can be resumed."
                )
            if run.result_artifact_id is not None:
                raise ValueError("This operation already produced an artifact.")
            if run.source_artifact_id != source_artifact.id:
                raise ValueError(
                    "The source artifact changed; start a new operation."
                )
            if str(run.source_content_hash or "") != source_hash:
                raise ValueError(
                    "The source content changed; start a new operation."
                )
            if str(run.settings_hash or "") != settings_hash:
                raise ValueError(
                    "The operation settings changed; start a new operation."
                )
        else:
            candidates = list(
                session.scalars(
                    select(AgentRun)
                    .where(
                        AgentRun.kind == kind,
                        AgentRun.session_id == session_id,
                        AgentRun.source_artifact_id == source_artifact.id,
                        AgentRun.source_content_hash == source_hash,
                        AgentRun.settings_hash == settings_hash,
                        AgentRun.status.in_(tuple(self.RESUMABLE_STATUSES)),
                        AgentRun.result_artifact_id.is_(None),
                    )
                    .order_by(AgentRun.updated_at.desc())
                ).all()
            )
            run = candidates[0] if candidates else None
        resumed = run is not None
        if run is None:
            run = AgentRun(
                id=new_id(),
                kind=kind,
                session_id=session_id,
                source_artifact_id=source_artifact.id,
                job_id=job_id,
                status="running",
                source_content_hash=source_hash,
                settings_hash=settings_hash,
                settings_json=dict(settings),
            )
            session.add(run)
            session.flush()
        else:
            run.job_id = job_id
            run.status = "running"
            run.error_message = None
            run.updated_at = utcnow()
        completed = {
            str(step.unit_key): dict(step.output_json or {})
            for step in session.scalars(
                select(AgentStep)
                .where(
                    AgentStep.agent_run_id == run.id,
                    AgentStep.status == "completed",
                    AgentStep.unit_key.is_not(None),
                )
                .order_by(AgentStep.ordinal)
            ).all()
            if step.unit_key
        }
        return ResumableAgentRun(run.id, resumed, completed)

    def checkpoint(
        self,
        run_id: str,
        *,
        unit_key: str,
        ordinal: int,
        input_value: Any,
        output: Mapping[str, Any],
        phase: str = "transform",
        summary: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        with self.database.session() as session:
            self.checkpoint_in_session(
                session, run_id, unit_key=unit_key, ordinal=ordinal,
                input_value=input_value, output=output, phase=phase,
                summary=summary, cost_usd=cost_usd,
            )

    def checkpoint_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        unit_key: str,
        ordinal: int,
        input_value: Any,
        output: Mapping[str, Any],
        phase: str = "transform",
        summary: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        input_hash = stable_payload_hash(input_value)
        run = session.get(AgentRun, run_id)
        if run is None:
            raise KeyError(run_id)
        step = session.scalar(
            select(AgentStep).where(
                AgentStep.agent_run_id == run_id,
                AgentStep.unit_key == unit_key,
            )
        )
        if step is not None and step.input_hash not in {None, input_hash}:
            raise ValueError(
                f"Checkpoint input drifted for unit '{unit_key}'; refusing unsafe reuse."
            )
        if step is None:
            step = AgentStep(
                agent_run_id=run_id,
                ordinal=int(ordinal),
                unit_key=unit_key,
                input_hash=input_hash,
                phase=phase,
            )
            session.add(step)
        step.status = "completed"
        step.summary = summary
        step.input_json = (
            dict(input_value)
            if isinstance(input_value, Mapping)
            else {"value": input_value}
        )
        step.output_json = dict(output)
        step.cost_usd = cost_usd
        step.updated_at = utcnow()
        run.checkpoint_revision += 1
        run.updated_at = utcnow()

    def finish(self, run_id: str, *, artifact_id: str | None = None) -> None:
        with self.database.immediate_session() as session:
            self.finish_in_session(session, run_id, artifact_id=artifact_id)

    def finish_in_session(self, session: Session, run_id: str, *, artifact_id: str | None = None) -> None:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise KeyError(run_id)
        owner = usage_creation_ownership(
            session, agent_run_id=run_id, artifact_id=artifact_id
        )
        inconsistent = session.scalar(
            select(UsageEvent.id)
            .where(
                UsageEvent.agent_run_id == run_id,
                or_(
                    UsageEvent.workspace_id.is_distinct_from(owner.workspace_id),
                    UsageEvent.project_id.is_distinct_from(owner.project_id),
                ),
            )
            .limit(1)
        )
        if inconsistent is not None:
            raise ValueError("The agent usage history belongs to another owner.")
        run.result_artifact_id = artifact_id
        run.status = "completed"
        run.error_message = None
        run.updated_at = utcnow()
        if artifact_id:
            session.execute(
                update(UsageEvent)
                .where(UsageEvent.agent_run_id == run_id)
                .values(artifact_id=artifact_id)
            )

    def fail(
        self, run_id: str, error: BaseException | str, *, interrupted: bool = False
    ) -> None:
        """Mark an error without discarding accepted units needed for resume."""
        with self.database.session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                return
            run.status = "interrupted" if interrupted else "failed"
            run.error_message = str(error)[:8000]
            run.updated_at = utcnow()

    def prepare_resume(self, run_id: str) -> tuple[AgentRun, Job]:
        """Atomically claim a failed run and return snapshots for requeueing."""
        with self.database.immediate_session() as session:
            run, job = self.prepare_resume_in_session(session, run_id)
            session.expunge(run)
            session.expunge(job)
            return run, job

    def prepare_resume_in_session(self, session: Session, run_id: str) -> tuple[AgentRun, Job]:
        """Claim a failed run within the caller's atomic queue handoff."""
        run = session.get(AgentRun, run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {"failed", "interrupted"}:
            raise ValueError("Only a failed or interrupted operation can be resumed.")
        with session.no_autoflush:
            source = (
                session.execute(
                    select(Artifact.id, Artifact.content_hash).where(
                        Artifact.id == run.source_artifact_id
                    )
                ).one_or_none()
                if run.source_artifact_id
                else None
            )
        if source is None or str(source.content_hash or "") != str(run.source_content_hash or ""):
            raise ValueError("The source changed; this operation cannot be resumed safely.")
        with session.no_autoflush:
            job_session_id = (
                session.scalar(select(Job.session_id).where(Job.id == run.job_id))
                if run.job_id
                else None
            )
        if job_session_id != run.session_id:
            raise ValueError("The original job is unavailable; start the stage again.")
        job = session.get(Job, run.job_id) if run.job_id else None
        if job is None:
            raise ValueError("The original job is unavailable; start the stage again.")
        # Claim before enqueueing so competing requests cannot both resume.
        # A caller-owned transaction can commit the claim, new job and link
        # together; the standalone wrapper retains its separate claim API.
        run.status = "retrying"
        run.error_message = None
        run.updated_at = utcnow()
        session.flush()
        return run, job
