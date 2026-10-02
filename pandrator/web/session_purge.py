"""Durable, conservative cleanup of trashed session data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
from copy import deepcopy
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
    ArtifactEdge,
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
    TranslationProject,
    TranslationProjectBranch,
    TranslationProjectOperation,
    UploadSessionRecord,
    VoiceSample,
    WorkflowRun,
    utcnow,
)
from .sessions import RevisionConflict
from .settings_policy import stable_hash
from .upload_activity import UploadBusy, upload_activity
from .uploads import managed_upload_directory


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

    def _upload_storage(self, upload: UploadSessionRecord) -> tuple[list[str], list[str]]:
        base = managed_upload_directory(self.paths, upload.id, upload.temporary_relative_path)
        files: list[str] = []
        if not base.exists():
            return files, []
        directories = [upload.temporary_relative_path]
        def walk_error(error: OSError) -> None:
            raise error

        for directory, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
            for name in dirs:
                path = Path(directory) / name
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError("Upload directory contains a symlink or non-directory.")
                directories.append(path.relative_to(self.paths.root).as_posix())
            for name in names:
                path = Path(directory) / name
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("Upload directory contains a symlink or nonregular file.")
                files.append(path.relative_to(self.paths.root).as_posix())
        return files, directories

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
        # Project bundle jobs belong to the source session, but their frozen
        # input files still require each selected language session to survive.
        owned_artifacts = set(session.scalars(artifact_ids).all())
        for job in session.scalars(select(Job).where(
            Job.kind == "project.exports.bundle",
            Job.status.in_(("queued", "running", "cancel_requested")),
        )).all():
            languages = (job.payload_json or {}).get("languages") or []
            references_owner = any(
                isinstance(language, dict) and (
                    language.get("session_id") == session_id
                    or any(isinstance(file, dict) and (
                        file.get("session_id") == session_id
                        or file.get("artifact_id") in owned_artifacts
                    ) for file in language.get("files") or [])
                ) for language in languages
            )
            if references_owner:
                blockers.append(f"unfinished:project.exports.bundle:{job.status}")
        return blockers

    def _committed_project_export_pair(self, session: Session, job: Job, source_ids: list[str], source_id: str) -> bool:
        """Recognize both outputs committed before a worker's terminal failure."""
        payload, result = job.payload_json or {}, job.result_json or {}
        receipt = result.get("publication_receipt")
        if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
                or payload.get("bundle_job_id") != job.id):
            return False
        expected = {
            "schema_version": 1, "job_id": job.id,
            "project_id": payload.get("project_id"),
            "source_operation_id": payload.get("operation_id"),
            "source_session_id": job.session_id, "source_artifact_ids": source_ids,
            "input_digest": payload.get("input_digest"),
            "manifest_digest": payload.get("manifest_digest"),
            "bundle_artifact_id": result.get("artifact_id"),
            "bundle_sha256": result.get("sha256"),
            "bundle_size_bytes": result.get("size_bytes"),
            "manifest_artifact_id": result.get("manifest_artifact_id"),
            "manifest_sha256": result.get("manifest_sha256"),
            "manifest_size_bytes": result.get("manifest_size_bytes"),
        }
        if any(receipt.get(key) != value for key, value in expected.items()):
            return False
        # Earlier language purges legitimately cascade their source rows/edges.
        # Every surviving source parent, including this branch, must still be
        # linked to both members of the committed pair, without extra parents.
        surviving_sources = set(session.scalars(select(Artifact.id).where(Artifact.id.in_(source_ids))).all())
        if source_id not in surviving_sources:
            return False
        for role, kind, prefix in (
            ("project_export_bundle", "zip", "bundle"),
            ("project_export_manifest", "json", "manifest"),
        ):
            artifact_id = receipt.get(f"{prefix}_artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                return False
            artifact = session.get(Artifact, artifact_id)
            expected_hash, expected_size = receipt.get(f"{prefix}_sha256"), receipt.get(f"{prefix}_size_bytes")
            if (artifact is None or artifact.session_id != job.session_id or artifact.state != "current"
                    or artifact.role != role or artifact.kind != kind
                    or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash)
                    or type(expected_size) is not int or expected_size <= 0
                    or artifact.content_hash != expected_hash or artifact.size_bytes != expected_size):
                return False
            metadata = artifact.metadata_json or {}
            if any(metadata.get(key) != value for key, value in {
                "project_id": payload.get("project_id"), "operation_id": payload.get("operation_id"),
                "source_operation_id": payload.get("operation_id"), "source_artifact_ids": source_ids,
                "input_digest": payload.get("input_digest"), "manifest_digest": payload.get("manifest_digest"),
                "output_kind": role,
            }.items()):
                return False
            parents = set(session.scalars(select(ArtifactEdge.parent_artifact_id).where(
                ArtifactEdge.child_artifact_id == artifact.id,
            )).all())
            if parents != surviving_sources:
                return False
            try:
                path = self._absolute(self.paths.root, artifact.relative_path)
                if not path.is_file() or path.stat().st_size != expected_size:
                    return False
            except (OSError, ValueError):
                return False
        return True

    def _retained_project_export(self, session: Session, output: Artifact, source: Artifact) -> bool:
        """Recognize a committed independent bundle copy from its exact receipt."""
        roles = {
            "project_export_bundle": ("zip", "artifact_id", "sha256", "size_bytes"),
            "project_export_manifest": ("json", "manifest_artifact_id", "manifest_sha256", "manifest_size_bytes"),
        }
        contract = roles.get(output.role)
        if contract is None or output.state != "current" or output.kind != contract[0]:
            return False
        metadata = output.metadata_json or {}
        operation_id = metadata.get("source_operation_id")
        project_id = metadata.get("project_id")
        if not operation_id or not project_id or metadata.get("operation_id") != operation_id:
            return False
        operation = session.get(TranslationProjectOperation, operation_id)
        project = session.get(TranslationProject, project_id)
        if (operation is None or project is None or operation.project_id != project.id
                or operation.action != "export" or output.session_id != project.source_session_id
                or metadata.get("output_kind") != output.role):
            return False
        try:
            path = self._absolute(self.paths.root, output.relative_path)
            if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size != output.size_bytes:
                return False
        except (OSError, ValueError):
            return False
        branch = session.scalar(select(TranslationProjectBranch).where(
            TranslationProjectBranch.project_id == project.id,
            TranslationProjectBranch.session_id == source.session_id,
        ))
        if branch is None or not any(
            child.get("branch_id") == branch.id and child.get("session_id") == source.session_id
            and not child.get("session_deleted")
            for child in operation.children_json or []
        ):
            return False
        for job in session.scalars(select(Job).where(
            Job.kind == "project.exports.bundle", Job.status.in_(("succeeded", "failed")),
            Job.session_id == project.source_session_id,
        )).all():
            payload, result = job.payload_json or {}, job.result_json or {}
            if (payload.get("operation_id") != operation.id or payload.get("project_id") != project.id
                    or payload.get("source_session_id") != project.source_session_id
                    or payload.get("principal_subject") != operation.principal_subject
                    or (payload.get("target_identity") or {}).get("instance_id") != operation.target_instance_id
                    or payload.get("project_revision") != operation.preview_json.get("project_revision")
                    or result.get(contract[1]) != output.id
                    or not output.content_hash or result.get(contract[2]) != output.content_hash
                    or result.get(contract[3]) != output.size_bytes):
                continue
            if any(not metadata.get(key) or metadata.get(key) != payload.get(key)
                   or metadata.get(key) != result.get(key)
                   for key in ("input_digest", "manifest_digest")):
                continue
            languages = payload.get("languages") or []
            files = [file for language in languages for file in language.get("files") or []]
            source_ids = sorted({file.get("artifact_id") for file in files if file.get("artifact_id")})
            if metadata.get("source_artifact_ids") != source_ids:
                continue
            if job.status == "failed" and not self._committed_project_export_pair(session, job, source_ids, source.id):
                continue
            for language in languages:
                if language.get("branch_id") != branch.id or language.get("session_id") != source.session_id:
                    continue
                if language.get("language") != branch.target_language:
                    continue
                if language.get("operation_state") not in {"completed", "existing"}:
                    continue
                if any(file.get("artifact_id") == source.id
                       and file.get("session_id") == source.session_id
                       and file.get("relative_path") == source.relative_path
                       and file.get("role") == source.role
                       and file.get("sha256") == source.content_hash
                       and source.role.startswith("export")
                       for file in language.get("files") or []):
                    return True
        return False

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
        for edge in session.scalars(select(ArtifactEdge).where(
            (ArtifactEdge.parent_artifact_id == artifact.id)
            | (ArtifactEdge.child_artifact_id == artifact.id))).all():
            other_id = edge.child_artifact_id if edge.parent_artifact_id == artifact.id else edge.parent_artifact_id
            other = session.get(Artifact, other_id)
            if other is not None and other.session_id != artifact.session_id:
                if edge.parent_artifact_id == artifact.id and self._retained_project_export(session, other, artifact):
                    continue
                blockers.append("external_reference:artifact_edges")
        return preserved, blockers

    def _inspect(self, session: Session, record: SessionRecord, *, activity_blockers: list[str] | None = None) -> dict[str, Any]:
        blockers = self._busy(session, record.id)
        blockers.extend(activity_blockers or [])
        if record.trashed_at is None or record.status not in {"trashed", "purging"}:
            blockers.append("not_trashed")
        retained: set[str] = set()
        artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == record.id)).all())
        retained_project_exports: dict[str, set[str]] = {
            "project_export_bundle": set(), "project_export_manifest": set(),
        }
        for artifact in artifacts:
            for edge in session.scalars(select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == artifact.id,
            )).all():
                output = session.get(Artifact, edge.child_artifact_id)
                if output is not None and self._retained_project_export(session, output, artifact):
                    retained_project_exports[output.role].add(output.id)
        try:
            files = set(self._session_files(record.storage_key))
        except ValueError as error:
            blockers.append(f"unsafe_session_storage:{error}")
            files = set()
        uploads = list(session.scalars(select(UploadSessionRecord).where(
            UploadSessionRecord.session_id == record.id)).all())
        upload_directories: set[str] = set()
        upload_prefixes: list[str] = []
        upload_identity: list[dict[str, str]] = []
        other_uploads = list(session.scalars(select(UploadSessionRecord).where(
            or_(UploadSessionRecord.session_id != record.id, UploadSessionRecord.session_id.is_(None)))).all())
        for upload in uploads:
            upload_identity.append({"id": upload.id, "state": upload.state,
                                    "temporary_relative_path": upload.temporary_relative_path})
            try:
                base = managed_upload_directory(self.paths, upload.id, upload.temporary_relative_path)
                collision = False
                for other in other_uploads:
                    # Check aliases too, but never inventory another owner's tree.
                    other_path = Path(os.path.abspath(self.paths.root / other.temporary_relative_path))
                    aliases = [other_path]
                    try:
                        aliases.append(other_path.resolve())
                    except (OSError, RuntimeError):
                        pass
                    if any(path.is_relative_to(base) or base.is_relative_to(path) for path in aliases):
                        blockers.append(f"external_upload_owner:{other.id}")
                        collision = True
                if collision:
                    continue
                upload_files, directories = self._upload_storage(upload)
                files.update(upload_files)
                upload_directories.update(directories)
                upload_prefixes.append(upload.temporary_relative_path + "/")
            except (OSError, ValueError) as error:
                blockers.append(f"unsafe_upload_storage:{upload.id}:{error}")
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
            if (relative.startswith(f"sessions/{record.storage_key}/") or relative.startswith("artifacts/")
                    or any(relative.startswith(prefix) for prefix in upload_prefixes)):
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
            if relative.startswith(prefix) or any(relative.startswith(root) for root in upload_prefixes):
                retained.add(relative)
                blockers.append(f"external_file_owner:{other.id}")
        files -= retained
        if any(relative.startswith(prefix) for relative in retained for prefix in upload_prefixes):
            blockers.append("shared_upload_storage")
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
        payload["retained_project_exports"] = {
            "bundle_count": len(retained_project_exports["project_export_bundle"]),
            "manifest_count": len(retained_project_exports["project_export_manifest"]),
            "artifact_ids": sorted(set().union(*retained_project_exports.values())),
        }
        token_input = {**payload, "files": safe, "retained": sorted(retained),
                       "uploads": sorted(upload_identity, key=lambda upload: upload["id"]),
                       "upload_directories": sorted(upload_directories)}
        payload["impact_token"] = hashlib.sha256(json.dumps(token_input, sort_keys=True).encode()).hexdigest()
        payload["can_purge"] = not payload["blockers"]
        payload["_files"] = safe
        payload["_retained"] = retained
        payload["_owned_artifact_ids"] = owned_artifact_ids
        payload["_upload_directories"] = upload_directories
        return payload

    def _scrub_project_operations(self, session: Session, session_id: str) -> None:
        """Drop private branch captures in the same transaction as its cascade."""
        branches = {
            branch.id: branch for branch in session.scalars(select(TranslationProjectBranch).where(
                TranslationProjectBranch.session_id == session_id,
            )).all()
        }
        for operation in session.scalars(select(TranslationProjectOperation)).all():
            private = deepcopy(operation.preview_json or {})
            children = deepcopy(operation.children_json or [])
            deleted_ids = set(branches)
            for child in [*children, *(private.get("children") or [])]:
                if child.get("session_id") == session_id:
                    deleted_ids.add(child["branch_id"])
            captures = private.get("captures") or {}
            for branch_id, capture in captures.items():
                if (capture.get("guard") or {}).get("session_id") == session_id:
                    deleted_ids.add(branch_id)

            def scrub_child(child: dict[str, Any], removed_ids: set[str]) -> dict[str, Any]:
                branch_id = child.get("branch_id")
                if branch_id not in removed_ids and child.get("session_id") != session_id:
                    return child
                branch = branches.get(branch_id) if isinstance(branch_id, str) else None
                return {
                    "branch_id": branch_id,
                    "target_language": child.get("target_language") or (branch.target_language if branch else ""),
                    "attempt": child.get("attempt", 1),
                    "state": "skipped", "eligible": False,
                    "reason": "Language session permanently deleted.",
                    "session_deleted": True,
                }

            scrubbed = [scrub_child(child, deleted_ids) for child in children]
            if "children" in private:
                private["children"] = [scrub_child(child, deleted_ids) for child in private["children"]]
            if "captures" in private:
                private["captures"] = {
                    branch_id: capture for branch_id, capture in captures.items()
                    if branch_id not in deleted_ids
                }
            if scrubbed != operation.children_json or private != operation.preview_json:
                operation.children_json = scrubbed
                operation.preview_json = private
                operation.preview_digest = stable_hash(private)
                operation.revision += 1
                operation.updated_at = utcnow()

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
        with ExitStack() as stack:
            blockers = []
            try:
                stack.enter_context(upload_activity(self.paths, session_id=session_id))
            except UploadBusy:
                blockers.append("upload_writer_active")
            except (OSError, ValueError) as error:
                blockers.append(f"unsafe_upload_activity:{error}")
            return self._preview(session_id, blockers)

    def _preview(self, session_id: str, activity_blockers: list[str]) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            result = self._inspect(session, record, activity_blockers=activity_blockers)
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
        try:
            with upload_activity(self.paths, session_id=session_id):
                return self._purge_locked(session_id, expected_revision, impact_token)
        except UploadBusy as error:
            raise PurgeBlocked(["upload_writer_active"]) from error
        except (OSError, ValueError) as error:
            if isinstance(error, (PurgeBlocked, RevisionConflict)):
                raise
            raise PurgeBlocked([f"unsafe_upload_activity:{error}"]) from error

    def _purge_locked(self, session_id: str, expected_revision: int, impact_token: str) -> dict[str, Any]:
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
        return self._finish_locked(session_id)

    def _finish(self, session_id: str) -> dict[str, Any]:
        try:
            with upload_activity(self.paths, session_id=session_id):
                return self._finish_locked(session_id)
        except UploadBusy as error:
            raise PurgeBlocked(["upload_writer_active"]) from error

    def _finish_locked(self, session_id: str) -> dict[str, Any]:
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
                # Keep upload records until every temporary directory is empty
                # and removed; failed cleanup retains their identities for retry.
                for relative in sorted(impact["_upload_directories"], key=lambda path: (-len(Path(path).parts), path)):
                    try:
                        self._remove_managed_entry(relative, directory=True)
                    except FileNotFoundError:
                        pass
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
                self._scrub_project_operations(session, session_id)
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
