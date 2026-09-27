"""Durable, conservative cleanup of trashed session data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .database import Database
from .models import (
    AgentRun,
    AppSetting,
    Artifact,
    Base,
    DispatchRun,
    GenerationRun,
    Job,
    MediaEditDispatchRun,
    OutputAssembly,
    SessionPurge,
    SessionRecord,
    SourceAsset,
    SourceCleaningDispatchRun,
    SourceRecord,
    SpeechOptimizationDispatchRun,
    StageRun,
    TrainingRun,
    VoiceSample,
    WorkflowRun,
    utcnow,
)
from .sessions import RevisionConflict


class PurgeBlocked(ValueError):
    def __init__(self, blockers: list[str]):
        self.blockers = blockers
        super().__init__("Session cannot be purged: " + ", ".join(blockers))


_TERMINAL = {"complete", "completed", "succeeded", "success", "cancelled", "canceled", "failed", "error", "partial", "interrupted"}
_WORK = (Job, GenerationRun, AgentRun, DispatchRun, SourceCleaningDispatchRun,
         SpeechOptimizationDispatchRun, MediaEditDispatchRun, OutputAssembly, WorkflowRun)


class SessionPurgeService:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths

    def policy(self) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(AppSetting, "session.trash_policy")
            return {"days": (row.value_json or {}).get("days") if row else None,
                    "revision": row.revision if row else 0}

    def update_policy(self, expected_revision: int, days: int | None) -> dict[str, Any]:
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650):
            raise ValueError("days must be null or an integer from 1 to 3650.")
        with self.database.immediate_session() as session:
            row = session.get(AppSetting, "session.trash_policy")
            current = row.revision if row else 0
            if current != expected_revision:
                raise RevisionConflict(f"Expected revision {expected_revision}, found {current}.")
            if row is None:
                row = AppSetting(key="session.trash_policy", value_json={"days": days}, revision=1)
                session.add(row)
            else:
                row.value_json = {"days": days}
                row.revision += 1
                row.updated_at = utcnow()
            return {"days": days, "revision": row.revision}

    @staticmethod
    def _absolute(root: Path, relative: str) -> Path:
        """Reject traversal, symlinks at every component, and nonregular files."""
        candidate = Path(relative)
        if candidate.is_absolute() or not candidate.parts or any(p in {"..", "."} for p in candidate.parts):
            raise ValueError(f"Unsafe managed path: {relative}")
        root = root.resolve(strict=True)
        path = root
        for part in candidate.parts:
            path = path / part
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError(f"Symlink in managed path: {relative}")
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Path escapes managed root: {relative}")
        if path.exists() and not path.is_file():
            raise ValueError(f"Managed path is not a regular file: {relative}")
        return path

    def _session_files(self, storage_key: str) -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", storage_key):
            raise ValueError("Session storage key is not a safe directory name.")
        base = self.paths.sessions / storage_key
        if base.is_symlink():
            raise ValueError("Session directory is a symlink.")
        if not base.exists():
            return []
        if (not base.is_dir() or base.resolve() == self.paths.sessions.resolve()
                or not base.resolve().is_relative_to(self.paths.sessions.resolve())):
            raise ValueError("Session directory is outside managed storage.")
        paths: list[str] = []
        for directory, dirs, files in os.walk(base, followlinks=False):
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("Session directory contains a symlink.")
            for name in files:
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Session directory contains an unsafe file.")
                paths.append(path.relative_to(self.paths.root).as_posix())
        return sorted(paths)

    def _canonical_managed(self, relative: str) -> str:
        path = self._absolute(self.paths.root, relative)
        return path.relative_to(self.paths.root.resolve()).as_posix()

    def _canonical_shared_path(self, raw: str) -> str | None:
        candidate = Path(raw)
        if ".." in candidate.parts:
            raise ValueError("Shared path traverses directories.")
        if candidate.is_absolute():
            try:
                relative = candidate.relative_to(self.paths.root)
            except ValueError:
                if candidate.resolve().is_relative_to(self.paths.root.resolve()):
                    raise ValueError("Shared path enters managed storage through an alias.") from None
                return None
        else:
            relative = candidate
        return self._canonical_managed(str(relative))

    def _remove_managed_entry(self, relative: str, *, directory: bool = False) -> None:
        """Remove through no-follow directory descriptors where supported."""
        path = self._absolute(self.paths.root, relative) if not directory else self.paths.root / relative
        if (os.open not in os.supports_dir_fd
                or (os.rmdir if directory else os.unlink) not in os.supports_dir_fd):
            if directory:
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
            return
        parts = Path(relative).parts
        descriptors: list[int] = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            parent_fd = os.open(self.paths.root, flags)
            descriptors.append(parent_fd)
            for part in parts[:-1]:
                parent_fd = os.open(part, flags, dir_fd=parent_fd)
                descriptors.append(parent_fd)
            name = parts[-1]
            if directory:
                os.rmdir(name, dir_fd=parent_fd)
            else:
                try:
                    mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    return
                if not stat.S_ISREG(mode):
                    raise ValueError(f"Managed path is no longer a regular file: {relative}")
                os.unlink(name, dir_fd=parent_fd)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _busy(self, session: Session, session_id: str) -> list[str]:
        blockers: list[str] = []
        for model in _WORK:
            row = session.scalar(select(model).where(model.session_id == session_id,
                model.status.not_in(_TERMINAL)).limit(1))
            if row is not None:
                blockers.append(f"unfinished:{model.__tablename__}:{row.status}")
        workflow_ids = select(WorkflowRun.id).where(WorkflowRun.session_id == session_id)
        stage = session.scalar(select(StageRun).where(StageRun.workflow_run_id.in_(workflow_ids),
            StageRun.status.not_in(_TERMINAL)).limit(1))
        if stage is not None:
            blockers.append(f"unfinished:stage_runs:{stage.status}")
        # Training runs have no session_id; jobs and artifacts connect them.
        job_ids = select(Job.id).where(Job.session_id == session_id)
        artifact_ids = select(Artifact.id).where(Artifact.session_id == session_id)
        training = session.scalar(select(TrainingRun).where(
            TrainingRun.status.not_in(_TERMINAL),
            (TrainingRun.job_id.in_(job_ids) | TrainingRun.source_artifact_id.in_(artifact_ids)
             | TrainingRun.source_text_artifact_id.in_(artifact_ids)
             | TrainingRun.output_artifact_id.in_(artifact_ids))).limit(1))
        if training is not None:
            blockers.append(f"unfinished:training_runs:{training.status}")
        return blockers

    def _external_artifact_refs(self, session: Session, artifact: Artifact) -> tuple[bool, list[str]]:
        """Classify known shared references; block every other external FK."""
        preserved = bool(session.scalar(select(SourceAsset.id).where(SourceAsset.artifact_id == artifact.id).limit(1)))
        preserved |= bool(session.scalar(select(VoiceSample.id).where(VoiceSample.artifact_id == artifact.id).limit(1)))
        blockers: list[str] = []
        for table in Base.metadata.tables.values():
            if table.name in {"source_assets", "voice_samples", "artifacts", "session_purges", "artifact_edges", "training_runs"}:
                continue
            for column in table.columns:
                if not any(fk.target_fullname == "artifacts.id" for fk in column.foreign_keys):
                    continue
                query = select(func.count()).select_from(table).where(column == artifact.id)
                if "session_id" in table.c:
                    query = query.where(table.c.session_id != artifact.session_id)
                else:
                    # No owner identity: preserve the row only when explicitly known.
                    continue
                if session.scalar(query):
                    blockers.append(f"external_reference:{table.name}.{column.name}")
        from .models import ArtifactEdge

        for edge in session.scalars(select(ArtifactEdge).where(
            (ArtifactEdge.parent_artifact_id == artifact.id)
            | (ArtifactEdge.child_artifact_id == artifact.id))).all():
            other_id = edge.child_artifact_id if edge.parent_artifact_id == artifact.id else edge.parent_artifact_id
            other = session.get(Artifact, other_id)
            if other is not None and other.session_id != artifact.session_id:
                blockers.append("external_reference:artifact_edges")
        return preserved, blockers

    def _inspect(self, session: Session, record: SessionRecord) -> dict[str, Any]:
        blockers = self._busy(session, record.id)
        if record.trashed_at is None or record.status not in {"trashed", "purging"}:
            blockers.append("not_trashed")
        retained: set[str] = set()
        artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == record.id)).all())
        try:
            files = set(self._session_files(record.storage_key))
        except ValueError as error:
            blockers.append(f"unsafe_session_storage:{error}")
            files = set()
        owned_artifact_ids: list[str] = []
        for artifact in artifacts:
            shared, external = self._external_artifact_refs(session, artifact)
            blockers.extend(external)
            try:
                relative = self._canonical_managed(artifact.relative_path)
            except ValueError:
                blockers.append(f"unsafe_artifact_path:{artifact.id}")
                continue
            if shared:
                retained.add(relative)
                continue
            owned_artifact_ids.append(artifact.id)
            if relative.startswith(f"sessions/{record.storage_key}/") or relative.startswith("artifacts/"):
                files.add(relative)
            else:
                blockers.append(f"unmanaged_artifact:{artifact.id}")
        # Legacy/shared source rows can carry a managed file path without an
        # Artifact FK. Keep the file and its containing directory in place.
        for source in session.scalars(select(SourceAsset).where(SourceAsset.external_path.is_not(None))).all():
            if source.external_path is None:
                continue
            try:
                relative = self._canonical_shared_path(source.external_path)
            except (OSError, ValueError):
                blockers.append(f"unsafe_shared_path:source_assets:{source.id}")
                continue
            if relative is not None and relative in files:
                retained.add(relative)
        for source in session.scalars(select(SourceRecord).where(
            SourceRecord.session_id != record.id, SourceRecord.external_path.is_not(None))).all():
            if source.external_path is None:
                continue
            try:
                relative = self._canonical_shared_path(source.external_path)
            except (OSError, ValueError):
                blockers.append(f"unsafe_shared_path:sources:{source.id}")
                continue
            if relative is not None and relative in files:
                retained.add(relative)
        files -= retained
        # A different session may own an artifact physically stored under this
        # directory. Its row would survive deletion of the target session.
        prefix = f"sessions/{record.storage_key}/"
        for other in session.scalars(select(Artifact).where(
            or_(Artifact.session_id != record.id, Artifact.session_id.is_(None)))).all():
            try:
                relative = self._canonical_managed(other.relative_path)
            except ValueError:
                continue
            if relative.startswith(prefix):
                retained.add(relative)
                blockers.append(f"external_file_owner:{other.id}")
        files -= retained
        safe: list[str] = []
        bytes_total = 0
        for relative in sorted(files):
            try:
                path = self._absolute(self.paths.root, relative)
                if path.exists():
                    bytes_total += path.stat().st_size
                safe.append(relative)
            except ValueError:
                blockers.append(f"unsafe_path:{relative}")
        payload = {"revision": record.revision, "owned_file_count": len(safe),
                   "owned_bytes": bytes_total, "retained_shared_count": len(retained),
                   "scheduled_delete_at": record.purge_after.isoformat() if record.purge_after else None,
                   "blockers": sorted(set(blockers))}
        token_input = {**payload, "files": safe, "retained": sorted(retained)}
        payload["impact_token"] = hashlib.sha256(json.dumps(token_input, sort_keys=True).encode()).hexdigest()
        payload["can_purge"] = not payload["blockers"]
        payload["_files"] = safe
        payload["_retained"] = retained
        payload["_owned_artifact_ids"] = owned_artifact_ids
        return payload

    def _preflight_delete(self, session: Session, session_id: str, retained: set[str]) -> None:
        """Exercise the exact FK cascade under a savepoint before any unlink."""
        savepoint = session.begin_nested()
        try:
            for artifact in session.scalars(select(Artifact).where(Artifact.session_id == session_id)).all():
                if self._canonical_managed(artifact.relative_path) in retained:
                    artifact.session_id = None
            session.flush()
            session.execute(delete(SessionRecord).where(SessionRecord.id == session_id))
            session.flush()
        finally:
            savepoint.rollback()

    def preview(self, session_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            result = self._inspect(session, record)
            journal = session.get(SessionPurge, session_id)
            if journal is not None and journal.state in {"purging", "failed"}:
                result["revision"] = journal.expected_revision
                result["impact_token"] = journal.impact_token
            elif result["can_purge"]:
                try:
                    self._preflight_delete(session, session_id, result["_retained"])
                except Exception as error:
                    result["blockers"].append(f"delete_constraint:{type(error).__name__}")
                    result["can_purge"] = False
            return {key: value for key, value in result.items() if not key.startswith("_")}

    def purge(self, session_id: str, expected_revision: int, impact_token: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            journal = session.get(SessionPurge, session_id)
            if journal is not None:
                if journal.expected_revision != expected_revision or journal.impact_token != impact_token:
                    raise RevisionConflict("Purge request does not match the claimed revision and impact token.")
                if journal.state == "complete":
                    return {"state": "complete"}
            else:
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                if record.revision != expected_revision:
                    raise RevisionConflict(f"Expected revision {expected_revision}, found {record.revision}.")
                impact = self._inspect(session, record)
                if impact["impact_token"] != impact_token:
                    raise RevisionConflict("Purge impact changed; refresh the preview.")
                if impact["blockers"]:
                    raise PurgeBlocked(impact["blockers"])
                self._preflight_delete(session, session_id, impact["_retained"])
                journal = SessionPurge(session_id=session_id, state="purging",
                    manifest_json=impact["_files"], expected_revision=expected_revision,
                    impact_token=impact_token)
                session.add(journal)
                record.status = "purging"
                record.revision += 1
                record.updated_at = utcnow()
        return self._finish(session_id)

    def _finish(self, session_id: str) -> dict[str, Any]:
        try:
            # Hold the writer reservation through reference checks and unlink;
            # a failed unlink leaves the durable claim available for retry.
            with self.database.immediate_session() as session:
                journal = session.get(SessionPurge, session_id)
                if journal is None:
                    raise KeyError(session_id)
                if journal.state == "complete":
                    return {"state": "complete"}
                record = session.get(SessionRecord, session_id)
                if record is None or record.status != "purging":
                    raise PurgeBlocked(["purge_claim_lost"])
                impact = self._inspect(session, record)
                if impact["blockers"]:
                    raise PurgeBlocked(impact["blockers"])
                self._preflight_delete(session, session_id, impact["_retained"])
                manifest = sorted(set(journal.manifest_json) | set(impact["_files"]))
                journal.manifest_json = manifest
                session.flush()
                for relative in manifest:
                    path = self._absolute(self.paths.root, relative)
                    if relative in impact["_retained"]:
                        raise PurgeBlocked([f"new_shared_reference:{relative}"])
                    if path.exists():
                        self._remove_managed_entry(relative)
                # rmdir only empties; shared files keep their containing directory.
                base = self.paths.sessions / record.storage_key
                if base.is_dir() and not base.is_symlink():
                    for directory, _dirs, _files in os.walk(base, topdown=False, followlinks=False):
                        try:
                            relative = Path(directory).relative_to(self.paths.root).as_posix()
                            self._remove_managed_entry(relative, directory=True)
                        except OSError:
                            pass
                # Persist shared artifacts independently from the deleted session.
                for artifact in session.scalars(select(Artifact).where(Artifact.session_id == session_id)).all():
                    if self._canonical_managed(artifact.relative_path) in impact["_retained"]:
                        artifact.session_id = None
                session.flush()
                session.execute(delete(SessionRecord).where(SessionRecord.id == session_id))
                journal.state = "complete"
                journal.error_message = None
                journal.updated_at = utcnow()
            return {"state": "complete"}
        except Exception as error:
            with self.database.immediate_session() as session:
                journal = session.get(SessionPurge, session_id)
                if journal is not None and journal.state != "complete":
                    journal.state = "failed"
                    journal.error_message = f"{type(error).__name__}: {error}"[:2000]
                    journal.updated_at = utcnow()
            return {"state": "failed", "error": str(error)}

    def sweep(self, *, limit: int = 20, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(UTC)
        policy = self.policy()
        with self.database.session() as session:
            ids = []
            if policy["days"] is not None:
                ids = list(session.scalars(select(SessionRecord.id).where(
                    SessionRecord.trashed_at.is_not(None), SessionRecord.purge_after.is_not(None),
                    SessionRecord.purge_after <= now, SessionRecord.status == "trashed")
                    .order_by(SessionRecord.purge_after).limit(limit)).all())
            retry_ids = list(session.scalars(select(SessionPurge.session_id).where(
                SessionPurge.state.in_(("purging", "failed"))).limit(limit)).all())
        counts = {"attempted": 0, "complete": 0, "failed": 0}
        for session_id in list(dict.fromkeys(retry_ids + ids))[:limit]:
            try:
                with self.database.session() as session:
                    journal = session.get(SessionPurge, session_id)
                    params = (journal.expected_revision, journal.impact_token) if journal else None
                if params is None:
                    preview = self.preview(session_id)
                    if not preview["can_purge"]:
                        continue
                    params = (preview["revision"], preview["impact_token"])
                counts["attempted"] += 1
                counts[self.purge(session_id, *params)["state"]] += 1
            except (KeyError, PurgeBlocked, RevisionConflict, ValueError):
                counts["failed"] += 1
        return counts
