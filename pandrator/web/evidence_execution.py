"""Fresh authority and attempt fences for the existing subtitle evidence worker."""

from __future__ import annotations

import json
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.runtime import DataPaths

from . import models as m
from .access.artifact_visibility import artifact_roots_clause, resource_visibility_clause
from .access.contracts import AccessDenied, ProjectAction
from .access.media_execution_policy import _active_legacy_job
from .access.project_visibility import project_ids_for_user, workspace_authority
from .accounts.credential_resources import credential_project_clause, credential_workspace_clause
from .accounts.execution_actor import (
    HostActor,
    UserActor,
    actor_from_storage,
    current_execution_actor,
    execution_actor_scope,
)
from .accounts.job_authorization import (
    _aware,
    require_execution_actor_in_session,
)
from .database import Database
from .job_execution import JobLease, current_job_lease
from .jobs import JobQueue
from .projects.models import Project

_INPUTS = (
    "session_id",
    "job_id",
    "reason",
    "routes_json",
    "audio_model_ids_json",
    "source_artifact_id",
    "source_media_artifact_id",
    "source_revision_id",
    "source_segment_id",
    "cue_id",
    "start_ms",
    "end_ms",
    "clip_start_ms",
    "clip_end_ms",
)
_FIXED_FAILURE = "Subtitle evidence execution failed."


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class _Start:
    input_json: str
    artifacts: tuple[tuple[Any, ...], ...]
    models: tuple[tuple[Any, ...], ...]


class EvidenceExecution:
    """Captured execution identity; every operation re-proves its current authority."""

    def __init__(
        self,
        database: Database,
        jobs: JobQueue,
        actor: UserActor | HostActor | None,
        lease: JobLease,
        payload: dict[str, Any],
    ):
        self.database, self.jobs, self.actor = database, jobs, actor
        self.lease: JobLease = dict(lease)  # type: ignore[assignment]
        self.payload = dict(payload)
        self.evidence_id = payload["evidence_id"]
        self.session_id = payload["session_id"]
        self.start: _Start | None = None
        self.workspace_id: str | None = None
        self.project_id: str | None = None
        self.cancel_event: threading.Event | None = None
        self.last_check = 0.0
        self.attempt: tuple[Path, int, int] | None = None
        self.paths: DataPaths | None = None
        self.retained: dict[str, tuple[Any, ...]] = {}
        self.uncertain = False

    @contextmanager
    def _scope(self):
        token = current_job_lease.set(self.lease)
        try:
            with execution_actor_scope(self.actor):
                yield
        finally:
            current_job_lease.reset(token)

    def _identity(self, session: Session) -> None:
        with self._scope():
            if self.actor is None:
                _active_legacy_job(session, self.lease, self.payload)
            else:
                require_execution_actor_in_session(
                    session,
                    self.actor,
                    required_scopes=("app.read", "app.run"),
                    allow_host=type(self.actor) is HostActor,
                )

    def _artifact(self, session: Session, artifact_id: str) -> tuple[Any, ...]:
        clauses = [m.Artifact.id == artifact_id, m.Artifact.state != "deleted"]
        if type(self.actor) is UserActor:
            clauses.append(
                resource_visibility_clause(m.Artifact, self.actor, action=ProjectAction.READ)
            )
        else:
            clauses.append(artifact_roots_clause(m.Artifact))
        row = session.execute(
            select(
                m.Artifact.id,
                m.Artifact.workspace_id,
                m.Artifact.project_id,
                m.Artifact.session_id,
                m.Artifact.relative_path,
                m.Artifact.content_hash,
                m.Artifact.size_bytes,
            ).where(*clauses)
        ).one_or_none()
        if row is None:
            raise AccessDenied()
        return tuple(row)

    def _read(self, session: Session) -> _Start:
        self._identity(session)
        job = session.execute(
            select(
                m.Job.kind,
                m.Job.workspace_id,
                m.Job.project_id,
                m.Job.session_id,
                m.Job.workflow_run_id,
            ).where(m.Job.id == self.lease["job_id"])
        ).one_or_none()
        evidence = session.execute(
            select(m.SubtitleEvidence.session_id, m.SubtitleEvidence.job_id).where(
                m.SubtitleEvidence.id == self.evidence_id
            )
        ).one_or_none()
        if (
            job is None
            or evidence is None
            or job.kind != "subtitle.evidence"
            or job.workflow_run_id is not None
            or job.session_id != self.session_id
            or evidence.session_id != self.session_id
            or evidence.job_id != self.lease["job_id"]
        ):
            raise AccessDenied()
        clauses = [
            m.SessionRecord.id == self.session_id,
            m.SessionRecord.trashed_at.is_(None),
            m.SessionRecord.status != "purging",
            ~select(m.SessionPurge.session_id)
            .where(m.SessionPurge.session_id == self.session_id)
            .exists(),
            m.Workspace.state == "active",
        ]
        if type(self.actor) is UserActor:
            clauses += [
                Project.id.in_(project_ids_for_user(self.actor.user_id, action=ProjectAction.RUN)),
                credential_project_clause(self.actor),
            ]
        root = session.execute(
            select(Project.id, Project.workspace_id)
            .join(m.SessionRecord, m.SessionRecord.project_id == Project.id)
            .join(m.Workspace, m.Workspace.id == Project.workspace_id)
            .where(*clauses)
        ).one_or_none()
        if root is None or (root.id, root.workspace_id) != (job.project_id, job.workspace_id):
            raise AccessDenied()
        if self.project_id is not None and (self.project_id, self.workspace_id) != tuple(root):
            raise AccessDenied()
        stored = session.scalar(select(m.Job.payload_json).where(m.Job.id == self.lease["job_id"]))
        if (
            type(stored) is not dict
            or stored.get("evidence_id") != self.evidence_id
            or stored.get("session_id") != self.session_id
            or type(stored.get("force_refresh", False)) is not bool
            or stored.get("force_refresh", False) != self.payload.get("force_refresh", False)
        ):
            raise AccessDenied()
        row = session.execute(
            select(*(getattr(m.SubtitleEvidence, key) for key in _INPUTS)).where(
                m.SubtitleEvidence.id == self.evidence_id
            )
        ).one()
        values: dict[str, Any] = dict(zip(_INPUTS, row, strict=True))
        models = values["audio_model_ids_json"]
        if type(models) is not list or any(type(v) is not str for v in models):
            raise AccessDenied()
        if models and type(self.actor) is UserActor:
            visible = session.scalar(
                select(m.Workspace.id).where(
                    m.Workspace.id == root.workspace_id,
                    m.Workspace.state == "active",
                    workspace_authority(self.actor.user_id),
                    credential_workspace_clause(self.actor),
                )
            )
            if visible is None:
                raise AccessDenied()
        revision = session.execute(
            select(
                m.DocumentRevision.id,
                m.DocumentRevision.document_id,
                m.DocumentRevision.content_hash,
            )
            .join(m.Document, m.Document.id == m.DocumentRevision.document_id)
            .where(
                m.DocumentRevision.id == values["source_revision_id"],
                m.Document.session_id == self.session_id,
            )
        ).one_or_none()
        if revision is None:
            raise AccessDenied()
        values["revision_header"] = list(revision)
        artifacts = tuple(
            self._artifact(session, key)
            for key in dict.fromkeys(
                [values["source_artifact_id"], values["source_media_artifact_id"]]
            )
        )
        model_headers = []
        for model_id in models:
            model = session.execute(
                select(
                    m.ProviderModel.id,
                    m.ProviderModel.provider_id,
                    m.ProviderModel.model_id,
                    m.ProviderModel.revision,
                    m.ProviderModel.is_active,
                    m.ProviderModel.is_default,
                    m.ProviderModel.input_modalities_json,
                    m.Provider.id,
                    m.Provider.workspace_id,
                    m.Provider.revision,
                    m.Provider.enabled,
                    m.Provider.kind,
                )
                .join(m.Provider, m.Provider.id == m.ProviderModel.provider_id)
                .where(m.ProviderModel.id == model_id, m.Provider.workspace_id == root.workspace_id)
            ).one_or_none()
            if (
                model is None
                or not (model.is_active or model.is_default)
                or not model.enabled
                or model.kind != "llm"
                or "audio" not in model.input_modalities_json
            ):
                raise AccessDenied()
            model_headers.append(
                tuple(_json(v) if isinstance(v, (list, dict)) else v for v in model)
            )
        self.project_id, self.workspace_id = root.id, root.workspace_id
        return _Start(_json(values), artifacts, tuple(model_headers))

    def check_in_session(self, session: Session) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ProcessCancelled("Evidence transcription was canceled.")
        actual = self._read(session)
        for artifact_id, expected in self.retained.items():
            if self._artifact(session, artifact_id) != expected:
                raise AccessDenied()
        if self.start is None:
            self.start = actual
        elif actual != self.start:
            raise AccessDenied()

    def check(self) -> None:
        with self.database.snapshot_session() as session:
            self.check_in_session(session)
        self.last_check = time.monotonic()

    def checkpoint(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ProcessCancelled("Evidence transcription was canceled.")
        if time.monotonic() - self.last_check >= 1:
            self.check()

    def check_artifacts(self, artifact_ids: list[str]) -> None:
        with self.database.snapshot_session() as session:
            self.check_in_session(session)
            for artifact_id in dict.fromkeys(artifact_ids):
                actual = self._artifact(session, artifact_id)
                if artifact_id in self.retained and self.retained[artifact_id] != actual:
                    raise AccessDenied()
                self.retained[artifact_id] = actual

    def complete_in_session(self, session: Session, result: dict[str, Any]) -> dict[str, Any]:
        self.check_in_session(session)
        self.jobs.complete_in_session(
            session,
            self.lease["job_id"],
            self.lease["owner_id"],
            result,
            lease_generation=self.lease["lease_generation"],
        )
        session.flush()
        sealed = session.scalar(select(m.Job.result_json).where(m.Job.id == self.lease["job_id"]))
        if type(sealed) is not dict:
            raise RuntimeError("Subtitle evidence execution failed.")
        return sealed

    def validate_output(self, path: Path) -> None:
        if self.attempt is None:
            raise AccessDenied()
        attempt, device, inode = self.attempt
        info = attempt.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != (device, inode)
            or not path.resolve().is_relative_to(attempt.resolve())
            or not stat.S_ISREG(path.lstat().st_mode)
            or any(
                parent.is_symlink()
                for parent in (path, *path.parents)
                if parent.is_relative_to(attempt)
            )
        ):
            raise AccessDenied()

    def create_attempt(self, root: Path, paths: DataPaths) -> Path:
        self.check()
        self.paths = paths
        managed = paths.root.resolve()
        if not root.resolve().is_relative_to(managed) or any(
            p.is_symlink() for p in (root, *root.parents) if p != managed.parent
        ):
            raise AccessDenied()
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.resolve().is_relative_to(managed):
            raise AccessDenied()
        attempt = root / ("attempt-" + uuid4().hex)
        attempt.mkdir(exist_ok=False)
        info = attempt.lstat()
        self.attempt = (attempt, info.st_dev, info.st_ino)
        return attempt

    def failure_metadata(self) -> None:
        try:
            with self.database.immediate_session() as session:
                header = session.execute(
                    select(
                        m.Job.status,
                        m.Job.lease_owner,
                        m.Job.lease_generation,
                        m.Job.lease_expires_at,
                    ).where(m.Job.id == self.lease["job_id"])
                ).one_or_none()
                if (
                    header is None
                    or header.status not in {"running", "cancel_requested"}
                    or header.lease_owner != self.lease["owner_id"]
                    or header.lease_generation != self.lease["lease_generation"]
                    or header.lease_expires_at is None
                    or _aware(header.lease_expires_at) <= _aware(m.utcnow())
                ):
                    return
                saved = session.execute(
                    select(
                        m.JobActorRecord.user_id,
                        m.JobActorRecord.auth_revision,
                        m.JobActorRecord.context_json,
                    ).where(m.JobActorRecord.job_id == self.lease["job_id"])
                ).one_or_none()
                if self.actor is None:
                    if saved is not None:
                        return
                elif saved is None or actor_from_storage(*saved) != self.actor:
                    return
                session.query(m.SubtitleEvidence).filter(
                    m.SubtitleEvidence.id == self.evidence_id,
                    m.SubtitleEvidence.session_id == self.session_id,
                    m.SubtitleEvidence.job_id == self.lease["job_id"],
                ).update(
                    {"status": "failed", "error_message": _FIXED_FAILURE, "updated_at": m.utcnow()},
                    synchronize_session=False,
                )
        except Exception:
            return

    def cleanup_attempt(self) -> None:
        if self.attempt is None or self.uncertain:
            return
        attempt, device, inode = self.attempt
        try:
            info = attempt.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (device, inode)
            ):
                return
            if self.paths is None:
                return
            relative = self.paths.relative_managed_path(attempt) + "/"
            with self.database.snapshot_session() as session:
                registered = session.scalar(
                    select(m.Artifact.id)
                    .where(m.Artifact.relative_path.startswith(relative, autoescape=True))
                    .limit(1)
                )
            if registered is None:
                shutil.rmtree(attempt)
        except Exception:
            return


def prepare_evidence_execution(
    database: Database, jobs: JobQueue, payload: dict[str, Any]
) -> EvidenceExecution | None:
    actor, lease = current_execution_actor.get(), current_job_lease.get()
    if lease is None:
        if "_job_id" in payload or "_lease_generation" in payload:
            raise AccessDenied()
        if actor is None:
            return None
        if type(actor) is not HostActor:
            raise AccessDenied()
        with database.snapshot_session() as session:
            require_execution_actor_in_session(
                session, actor, required_scopes=("app.read", "app.run"), allow_host=True
            )
        return None
    if actor is not None and type(actor) not in (UserActor, HostActor):
        raise AccessDenied()
    if (
        type(payload.get("evidence_id")) is not str
        or not payload["evidence_id"]
        or type(payload.get("session_id")) is not str
        or not payload["session_id"]
        or type(payload.get("force_refresh", False)) is not bool
        or payload.get("_job_id") != lease.get("job_id")
        or type(payload.get("_lease_generation", lease.get("lease_generation"))) is not int
        or payload.get("_lease_generation", lease.get("lease_generation"))
        != lease.get("lease_generation")
    ):
        raise AccessDenied()
    execution = EvidenceExecution(database, jobs, actor, lease, payload)
    execution.check()
    return execution
