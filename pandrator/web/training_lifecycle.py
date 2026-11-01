"""Shared transactional training admission, cancellation, retry and reads."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.xtts_model_paths import validate_training_model_name
from pandrator.runtime import DataPaths

from .access.artifact_policy import ArtifactNotFound
from .access.contracts import WorkspaceNotFound
from .access.local_tools_policy import (
    LocalToolOperation,
    require_local_tool_inputs,
    require_local_tool_workspace,
)
from .credentials import contains_inline_secret, redact_inline_secrets
from .database import Database
from .jobs import JobQueue
from .models import Artifact, Job, TrainingRun, Voice, new_id, utcnow
from .training_state import (
    TRAINING_ACTIVE_STATUSES,
    TRAINING_RETRYABLE_STATUSES,
    effective_training_state,
    is_training_job_owner,
)
from .voice_identity import training_workspace, voice_creation_workspace

if TYPE_CHECKING:
    from .schemas import TrainingCreateRequest


class TrainingNotFound(KeyError):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class TrainingActive(Exception):
    def __init__(self):
        self.message = "Only failed, canceled, or interrupted training can be retried."
        super().__init__(self.message)


class TrainingService:
    def __init__(self, database: Database, paths: DataPaths, jobs: JobQueue):
        self.database = database
        self.paths = paths
        self.jobs = jobs

    def _validate_inputs(
        self,
        session: Session,
        source_artifact_id: str | None,
        source_text_artifact_id: str | None,
        voice_id: str | None,
        settings: dict[str, Any],
        workspace_id: str,
    ) -> None:
        source_ids = [source_artifact_id]
        if source_text_artifact_id:
            source_ids.append(source_text_artifact_id)
        try:
            require_local_tool_inputs(
                session, LocalToolOperation.TRAINING_RUN,
                artifact_ids=[identifier for identifier in source_ids if identifier],
                voice_id=voice_id, workspace_id=workspace_id,
            )
        except ArtifactNotFound:
            raise TrainingNotFound("A training source artifact was not found.") from None
        except WorkspaceNotFound:
            raise TrainingNotFound("Voice not found.") from None
        if contains_inline_secret(settings):
            raise ValueError("API keys and other credentials must be saved in provider settings.")
        for artifact_id in source_ids:
            artifact = session.get(Artifact, artifact_id) if artifact_id else None
            if artifact is None or artifact.state == "deleted":
                raise TrainingNotFound("A training source artifact was not found.")
            self.paths.managed_path(artifact.relative_path)
        if voice_id and session.get(Voice, voice_id) is None:
            raise TrainingNotFound("Voice not found.")

    @staticmethod
    def _require_workspace(
        session: Session, training: TrainingRun, workspace_id: str
    ) -> None:
        try:
            training_workspace(session, training, workspace_id=workspace_id)
        except ValueError as error:
            raise TrainingNotFound("Training run not found.") from error

    def _enqueue(self, session: Session, training: TrainingRun) -> tuple[TrainingRun, Job]:
        session.add(training)
        session.flush()
        job = self.jobs.enqueue_in_session(
            session,
            "training.xtts",
            {
                "training_id": training.id,
                "workspace_id": training.workspace_id,
                "model_name": training.model_name,
                "source_artifact_id": training.source_artifact_id,
                "source_text_artifact_id": training.source_text_artifact_id,
                "settings": deepcopy(training.settings_json),
            },
            resource_keys=["training:xtts", "gpu:default"],
            workspace_id=training.workspace_id,
        )
        training.job_id = job.id
        training.updated_at = utcnow()
        session.flush()
        session.expunge(training)
        session.expunge(job)
        return training, job

    def start(
        self,
        payload: TrainingCreateRequest,
        *,
        workspace_id: str | None = None,
    ) -> tuple[TrainingRun, Job]:
        validate_training_model_name(payload.model_name)
        with self.database.immediate_session() as session:
            workspace_id = require_local_tool_workspace(
                session, LocalToolOperation.TRAINING_RUN, workspace_id=workspace_id
            )
            workspace = voice_creation_workspace(session, workspace_id=workspace_id)
            self._validate_inputs(
                session,
                payload.source_artifact_id,
                payload.source_text_artifact_id,
                payload.voice_id,
                payload.settings,
                workspace.id,
            )
            return self._enqueue(
                session,
                TrainingRun(
                    id=new_id(),
                    workspace_id=workspace.id,
                    kind="xtts",
                    voice_id=payload.voice_id,
                    source_artifact_id=payload.source_artifact_id,
                    source_text_artifact_id=payload.source_text_artifact_id,
                    model_name=payload.model_name,
                    settings_json=deepcopy(payload.settings),
                ),
            )

    def retry(
        self, training_id: str, *, workspace_id: str | None = None
    ) -> tuple[TrainingRun, Job]:
        with self.database.immediate_session() as session:
            workspace_id = require_local_tool_workspace(
                session, LocalToolOperation.TRAINING_RUN, workspace_id=workspace_id
            )
            workspace = voice_creation_workspace(session, workspace_id=workspace_id)
            previous = session.scalar(select(TrainingRun).where(
                TrainingRun.id == training_id, TrainingRun.workspace_id == workspace.id
            ))
            if previous is None:
                raise TrainingNotFound("Training run not found.")
            self._require_workspace(session, previous, workspace.id)
            job = session.get(Job, previous.job_id) if previous.job_id else None
            state = effective_training_state(previous, job)
            if state.status not in TRAINING_RETRYABLE_STATUSES:
                raise TrainingActive()
            validate_training_model_name(previous.model_name)
            settings = deepcopy(previous.settings_json or {})
            self._validate_inputs(
                session,
                previous.source_artifact_id,
                previous.source_text_artifact_id,
                previous.voice_id,
                settings,
                workspace.id,
            )
            previous.status = state.status
            previous.error_message = state.error_message
            previous.updated_at = state.updated_at
            return self._enqueue(
                session,
                TrainingRun(
                    id=new_id(),
                    workspace_id=workspace.id,
                    kind=previous.kind,
                    voice_id=previous.voice_id,
                    source_artifact_id=previous.source_artifact_id,
                    source_text_artifact_id=previous.source_text_artifact_id,
                    model_name=previous.model_name,
                    settings_json=settings,
                ),
            )

    def cancel(
        self, training_id: str, *, workspace_id: str | None = None
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            workspace_id = require_local_tool_workspace(
                session, LocalToolOperation.TRAINING_CANCEL, workspace_id=workspace_id
            )
            workspace = voice_creation_workspace(session, workspace_id=workspace_id)
            training = session.scalar(select(TrainingRun).where(
                TrainingRun.id == training_id, TrainingRun.workspace_id == workspace.id
            ))
            if training is None:
                raise TrainingNotFound("Training run not found.")
            self._require_workspace(session, training, workspace.id)
            if training.status in TRAINING_ACTIVE_STATUSES:
                job = session.get(Job, training.job_id) if training.job_id else None
                if not is_training_job_owner(training, job):
                    training.status = "canceled"
                    training.updated_at = utcnow()
                else:
                    state = effective_training_state(training, job)
                    if state.status in TRAINING_ACTIVE_STATUSES:
                        assert job is not None
                        self.jobs.request_cancel_in_session(session, job.id)
                        state = effective_training_state(training, job)
                    training.status = state.status
                    training.error_message = state.error_message
                    training.updated_at = state.updated_at
            return {
                "id": training.id,
                "job_id": training.job_id,
                "status": training.status,
            }

    def list(
        self,
        *,
        limit: int | None = 200,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.snapshot_session() as session:
            workspace_id = require_local_tool_workspace(
                session, LocalToolOperation.TRAINING_READ, workspace_id=workspace_id
            )
            workspace = voice_creation_workspace(session, workspace_id=workspace_id)
            statement = (
                select(TrainingRun, Job)
                .outerjoin(Job, TrainingRun.job_id == Job.id)
                .where(TrainingRun.workspace_id == workspace.id)
                .order_by(TrainingRun.created_at.desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            result: list[dict[str, Any]] = []
            for training, job in session.execute(statement):
                training_workspace(session, training, workspace_id=workspace.id)
                state = effective_training_state(training, job)
                result.append(
                    {
                        "id": training.id,
                        "kind": training.kind,
                        "voice_id": training.voice_id,
                        "job_id": training.job_id,
                        "source_artifact_id": training.source_artifact_id,
                        "source_text_artifact_id": training.source_text_artifact_id,
                        "output_artifact_id": training.output_artifact_id,
                        "model_name": training.model_name,
                        "status": state.status,
                        "settings_json": redact_inline_secrets(training.settings_json),
                        "error_message": state.error_message,
                        "created_at": training.created_at.isoformat(),
                        "updated_at": state.updated_at.isoformat(),
                    }
                )
            return result
