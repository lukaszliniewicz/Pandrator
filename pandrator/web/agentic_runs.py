"""Durable checkpoints for correction, translation, and research work."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy import select, update

from .database import Database
from .models import AgentRun, AgentStep, Artifact, Job, UsageEvent, new_id, utcnow


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
        source_hash = str(source_artifact.content_hash or "")
        with self.database.session() as session:
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
        input_hash = stable_payload_hash(input_value)
        with self.database.session() as session:
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
        with self.database.session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise KeyError(run_id)
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

    def fail(self, run_id: str, error: BaseException | str) -> None:
        with self.database.session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.error_message = str(error)[:8000]
            run.updated_at = utcnow()

    def prepare_resume(self, run_id: str) -> tuple[AgentRun, Job]:
        """Atomically claim a failed run and return snapshots for requeueing."""
        with self.database.immediate_session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status not in {"failed", "interrupted"}:
                raise ValueError(
                    "Only a failed or interrupted operation can be resumed."
                )
            source = (
                session.get(Artifact, run.source_artifact_id)
                if run.source_artifact_id
                else None
            )
            if source is None or str(source.content_hash or "") != str(
                run.source_content_hash or ""
            ):
                raise ValueError(
                    "The source changed; this operation cannot be resumed safely."
                )
            job = session.get(Job, run.job_id) if run.job_id else None
            if job is None:
                raise ValueError(
                    "The original job is unavailable; start the stage again."
                )
            # Claim the resume before the new job is created. This makes a
            # double click (or two browser tabs) deterministic instead of
            # enqueueing duplicate paid work. Startup reconciliation returns
            # this run to ``failed`` if the process dies before the new job ID
            # is attached.
            run.status = "retrying"
            run.error_message = None
            run.updated_at = utcnow()
            session.flush()
            session.expunge(run)
            session.expunge(job)
            return run, job
