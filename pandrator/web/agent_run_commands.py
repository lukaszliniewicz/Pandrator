"""Atomic admission and state changes for durable agent runs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agentic_runs import AgenticRunStore, stable_payload_hash
from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .models import AgentRun, Artifact, SessionRecord, SessionSource, SourceAsset, new_id, utcnow


class AgentRunAdmissionError(ValueError):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AgentRunStateError(ValueError):
    """The requested agent-run state transition is unavailable."""


class AgentRunCommands:
    def __init__(self, database: Database, jobs: JobQueue, artifacts: ArtifactService):
        self.database = database
        self.jobs = jobs
        self.artifacts = artifacts

    @staticmethod
    def _current_attachment(session: Session, session_id: str, artifact_id: str) -> str:
        attachment_id = session.scalar(
            select(SessionSource.id)
            .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
            .where(
                SessionSource.session_id == session_id,
                SourceAsset.artifact_id == artifact_id,
                SessionSource.is_current.is_(True),
            )
            .order_by(SessionSource.updated_at.desc())
            .limit(1)
        )
        if attachment_id is None:
            raise AgentRunAdmissionError(
                "invalid_source",
                "Source cleaning requires a source attached to this session.",
                422,
            )
        return attachment_id

    def create(
        self, session_id: str, source_artifact_id: str, settings: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
        source, source_path = self.artifacts.resolve(source_artifact_id)
        source_hash = str(source.content_hash or "")
        with self.database.session() as session:
            attachment_id = self._current_attachment(session, session_id, source.id)
        if source_path.suffix.lower() not in {".docx", ".epub", ".mobi", ".pdf", ".txt"}:
            raise AgentRunAdmissionError(
                "unsupported_source",
                "Source cleaning is available for text documents, not audio, video, or subtitle sources.",
                422,
            )
        final_settings = {**settings, "agentic": True}
        with self.database.immediate_session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            managed_source = session.get(Artifact, source_artifact_id)
            if managed_source is None:
                raise KeyError(source_artifact_id)
            current_attachment = self._current_attachment(session, session_id, source_artifact_id)
            if (
                current_attachment != attachment_id
                or str(managed_source.content_hash or "") != source_hash
            ):
                raise AgentRunAdmissionError(
                    "invalid_source",
                    "The source changed before source cleaning could be started.",
                    422,
                )
            run = AgentRun(
                id=new_id(),
                kind="source_cleaning",
                session_id=session_id,
                source_artifact_id=source_artifact_id,
                status="queued",
                source_content_hash=str(managed_source.content_hash or ""),
                settings_hash=stable_payload_hash(final_settings),
                settings_json=final_settings,
            )
            session.add(run)
            session.flush()
            job = self.jobs.enqueue_in_session(
                session,
                "source.clean",
                {
                    "session_id": session_id,
                    "source_artifact_id": source_artifact_id,
                    "agent_run_id": run.id,
                    "settings": final_settings,
                },
                session_id=session_id,
                resource_keys=[f"session:{session_id}", "service:llm"],
            )
            run.job_id = job.id
            run.updated_at = utcnow()
            session.flush()
            result = {"id": run.id, "job_id": job.id, "status": "queued"}
        return result

    def resume(self, run_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            try:
                run, previous_job = AgenticRunStore(self.database).prepare_resume_in_session(
                    session, run_id
                )
            except ValueError as error:
                raise AgentRunStateError(str(error)) from error
            payload = dict(previous_job.payload_json or {})
            run_ids = dict(payload.get("_agent_run_ids") or {})
            run_ids[run.kind] = run.id
            payload["_agent_run_ids"] = run_ids
            payload["_agent_run_id"] = run.id
            payload["agent_run_id"] = run.id
            job = self.jobs.enqueue_in_session(
                session,
                previous_job.kind,
                payload,
                session_id=run.session_id,
                workflow_run_id=previous_job.workflow_run_id,
                max_attempts=previous_job.max_attempts,
                resource_keys=list(previous_job.resource_keys_json or []),
            )
            run.job_id = job.id
            run.status = "retrying"
            run.error_message = None
            run.updated_at = utcnow()
            session.flush()
            result = {"id": run.id, "job_id": job.id, "status": "retrying"}
        return result

    def accept(self, run_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status != "completed" or not run.result_artifact_id:
                raise AgentRunStateError("Only a completed cleaning result can be accepted.")
            run.status = "accepted"
            run.updated_at = utcnow()
            result = {
                "id": run.id,
                "status": run.status,
                "result_artifact_id": run.result_artifact_id,
                "updated_at": run.updated_at.isoformat(),
            }
        return result
