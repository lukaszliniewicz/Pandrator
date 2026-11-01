"""Managed artifact registration and containment checks."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .access.artifact_policy import require_artifact_access
from .access.contracts import AccessContext, ProjectAction
from .access.current_session_policy import require_current_session_access
from .accounts.execution_actor import UserActor, current_execution_actor
from .artifact_selection import activate_registered_artifact
from .database import Database
from .models import (
    Artifact,
    ArtifactEdge,
    AudioTake,
    ExportRecord,
    Job,
    OutputAssembly,
    SessionRecord,
    SessionStageSelection,
    SourceAsset,
    VoiceSample,
    utcnow,
)
from .projects.artifact_identity import resolve_artifact_ownership

SINGLETON_SESSION_ROLES = {
    "transcription",
    "media_edit_word_timestamps",
    "media_edit_media",
    "media_edit_subtitles",
    "correction",
    "translation",
    "tts_optimized",
    "reviewed_transcription",
    "reviewed_correction",
    "reviewed_translation",
    "clean_text",
    "prepared_text",
    "speech_blocks",
    "dubbing_audio",
    "audiobook_audio",
    "bilingual_subtitle_overlay",
}

#: Generated-audio outputs removable through the session Output tab alongside
#: finalized exports. ``output_assembly`` is deliberately absent: it names the
#: planning row, never an artifact role, so there is nothing to delete.
#: ``rvc_audio`` is deliberately absent: voice-conversion intermediates feed
#: take chains and stay under their own stage tooling.
REMOVABLE_GENERATED_AUDIO_ROLES = frozenset(
    {
        "assembled_audio",
        "audiobook_audio",
        "dubbing_audio",
    }
)

OUTPUT_JOB_KIND_PREFIXES = ("generation.assemble", "export.")
OUTPUT_JOB_ACTIVE_STATUSES = frozenset({"queued", "running", "cancel_requested"})
OUTPUT_ASSEMBLY_ACTIVE_STATUSES = frozenset(
    {"queued", "running", "cancel_requested"}
)


@dataclass(frozen=True, slots=True)
class PreparedArtifactRegistration:
    relative_path: str
    mime_type: str | None
    size_bytes: int
    content_hash: str | None
    settings_hash: str | None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def copy_stream_and_hash(source: BinaryIO, destination: Path, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while chunk := source.read(chunk_size):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


class ArtifactService:
    def __init__(self, database: Database, paths: DataPaths):
        self.database = database
        self.paths = paths

    def register(
        self,
        path: Path,
        *,
        kind: str,
        role: str = "artifact",
        session_id: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
        parent_ids: list[str] | None = None,
        calculate_hash: bool = True,
        metadata: dict | None = None,
        settings: dict | None = None,
        replace_parent_ids: bool = False,
    ) -> Artifact:
        prepared = self.prepare_registration(
            path,
            calculate_hash=calculate_hash,
            settings=settings,
        )
        with self.database.session() as session:
            artifact = self.register_in_session(
                session,
                path,
                kind=kind,
                role=role,
                session_id=session_id,
                project_id=project_id,
                workspace_id=workspace_id,
                parent_ids=parent_ids,
                calculate_hash=calculate_hash,
                metadata=metadata,
                settings=settings,
                replace_parent_ids=replace_parent_ids,
                _prepared=prepared,
            )
            session.flush()
            session.expunge(artifact)
            return artifact

    def prepare_registration(
        self,
        path: Path,
        *,
        calculate_hash: bool = True,
        settings: dict | None = None,
    ) -> PreparedArtifactRegistration:
        """Read file metadata before opening a database write transaction."""

        relative_path = self.paths.relative_managed_path(path)
        stat = path.stat()
        content_hash = sha256_file(path) if calculate_hash else None
        settings_hash = (
            hashlib.sha256(
                json.dumps(
                    settings,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if settings is not None
            else None
        )
        return PreparedArtifactRegistration(
            relative_path=relative_path,
            mime_type=mimetypes.guess_type(path.name)[0],
            size_bytes=stat.st_size,
            content_hash=content_hash,
            settings_hash=settings_hash,
        )

    def next_available_path(self, path: Path) -> Path:
        """Allocate a managed path that has never been registered before.

        Finalized outputs are immutable records. A missing or deleted file must
        not cause a later export to reuse its historical Artifact row merely
        because the relative path is free on disk.
        """
        with self.database.session() as session:
            for version in range(1, 100_000):
                candidate = (
                    path
                    if version == 1
                    else path.with_name(f"{path.stem}-{version}{path.suffix}")
                )
                if candidate.exists():
                    continue
                relative_path = self.paths.relative_managed_path(candidate)
                registered = session.scalar(
                    select(Artifact.id)
                    .where(Artifact.relative_path == relative_path)
                    .limit(1)
                )
                if registered is None:
                    return candidate
        raise RuntimeError(f"Could not allocate a new managed path for {path.name}.")

    def register_in_session(
        self,
        session: Session,
        path: Path,
        *,
        kind: str,
        role: str = "artifact",
        session_id: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
        parent_ids: list[str] | None = None,
        calculate_hash: bool = True,
        metadata: dict | None = None,
        settings: dict | None = None,
        replace_parent_ids: bool = False,
        _prepared: PreparedArtifactRegistration | None = None,
    ) -> Artifact:
        """Register an artifact inside the caller's transaction.

        Callers that also update takes, usage, or workflow state can use this
        method to make those related writes one atomic unit of work.
        """

        prepared = _prepared or self.prepare_registration(
            path,
            calculate_hash=calculate_hash,
            settings=settings,
        )
        relative_path = prepared.relative_path
        with session.no_autoflush:
            artifact = session.scalar(
                select(Artifact).where(Artifact.relative_path == relative_path)
            )
            ownership = resolve_artifact_ownership(
                session,
                artifact=artifact,
                session_id=session_id,
                project_id=project_id,
                workspace_id=workspace_id,
            )
        replaced = list(
            session.scalars(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role == role,
                    Artifact.state == "current",
                    Artifact.relative_path != relative_path,
                )
            ).all()
        ) if session_id and role in SINGLETON_SESSION_ROLES else []
        for previous in replaced:
            previous.state = "stale"
            self._mark_descendants_stale(session, previous.id)

        created = artifact is None
        if artifact is None:
            artifact = Artifact(
                session_id=session_id,
                project_id=ownership.project_id,
                workspace_id=ownership.workspace_id,
                kind=kind,
                role=role,
                relative_path=relative_path,
                mime_type=prepared.mime_type,
                size_bytes=prepared.size_bytes,
                content_hash=prepared.content_hash,
                settings_hash=prepared.settings_hash,
                metadata_json=metadata or {},
            )
            session.add(artifact)
            session.flush()
        else:
            was_deleted = artifact.state == "deleted"
            artifact.session_id = session_id or artifact.session_id
            artifact.kind = kind
            artifact.role = role
            artifact.mime_type = prepared.mime_type
            artifact.size_bytes = prepared.size_bytes
            artifact.content_hash = (
                prepared.content_hash or artifact.content_hash
            )
            artifact.settings_hash = (
                prepared.settings_hash or artifact.settings_hash
            )
            artifact.state = "current"
            restored_metadata = dict(
                metadata
                if metadata is not None
                else artifact.metadata_json or {}
            )
            if was_deleted:
                restored_metadata.pop("deleted_at", None)
            artifact.metadata_json = restored_metadata
            artifact.updated_at = utcnow()

        normalized_parent_ids = list(dict.fromkeys(parent_ids or []))
        if replace_parent_ids and not created:
            session.execute(
                delete(ArtifactEdge).where(
                    ArtifactEdge.child_artifact_id == artifact.id
                )
            )
        existing_parent_ids: set[str] = set()
        if normalized_parent_ids and not created:
            existing_parent_ids = set(
                session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == artifact.id,
                        ArtifactEdge.parent_artifact_id.in_(
                            normalized_parent_ids
                        ),
                    )
                ).all()
            )
        session.add_all(
            [
                ArtifactEdge(
                    parent_artifact_id=parent_id,
                    child_artifact_id=artifact.id,
                )
                for parent_id in normalized_parent_ids
                if parent_id not in existing_parent_ids
            ]
        )
        session.flush()
        activate_registered_artifact(session, artifact)
        session.flush()
        return artifact

    @staticmethod
    def _mark_descendants_stale(session, artifact_id: str) -> None:
        """Invalidate derived artifacts while preserving every file for review."""
        pending = [artifact_id]
        visited: set[str] = set()
        while pending:
            parent_id = pending.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            child_ids = list(
                session.scalars(
                    select(ArtifactEdge.child_artifact_id).where(ArtifactEdge.parent_artifact_id == parent_id)
                ).all()
            )
            for child_id in child_ids:
                child = session.get(Artifact, child_id)
                if child is not None and child.state == "current":
                    child.state = "stale"
                pending.append(child_id)

    def invalidate_descendants(self, artifact_id: str) -> None:
        with self.database.session() as session:
            if session.get(Artifact, artifact_id) is None:
                raise KeyError(artifact_id)
            self._mark_descendants_stale(session, artifact_id)

    def resolve(self, artifact_id: str) -> tuple[Artifact, Path]:
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.state == "deleted":
                raise KeyError(artifact_id)
            path = self.paths.managed_path(artifact.relative_path)
            session.expunge(artifact)
        return artifact, path

    def remove_output(self, session_id: str, artifact_id: str) -> dict[str, str]:
        """Remove a finalized export or generated-audio output.

        Every removable output is checked for protected take/source/voice
        references, cross-session sharing, active assembly/export jobs,
        in-flight assemblies, and dependent intermediates before its file is
        touched. Independently materialized final exports and their provenance
        are kept.
        Referencing assemblies are retired to stale with their artifact link
        cleared so latest-assembly and export selection never resolve a
        deleted file; stage selections pointing at the file are cleared.
        """
        with self.database.session() as session:
            # Keep validation and retirement serialized with job submissions.
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            require_current_session_access(session, session_id, ProjectAction.EDIT)
            actor = current_execution_actor.get()
            if type(actor) is UserActor:
                artifact = require_artifact_access(
                    session,
                    AccessContext(actor),
                    artifact_id,
                    expected_session_id=session_id,
                )
                if artifact.state == "deleted":
                    raise KeyError(artifact_id)
            else:
                artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.state == "deleted" or artifact.session_id != session_id:
                raise KeyError(artifact_id)
            is_removable_output = (
                artifact.kind == "export"
                or artifact.role == "export"
                or artifact.role.startswith("export_")
                or artifact.role in REMOVABLE_GENERATED_AUDIO_ROLES
            )
            if not is_removable_output:
                raise ValueError(
                    "Only finalized exports and generated audio can be removed from the Output tab."
                )
            self._ensure_output_removable(session, session_id, artifact)
            try:
                path = self.paths.managed_path(artifact.relative_path)
            except ValueError as error:
                raise ValueError(f"The output file is outside managed storage: {error}") from error
            self._ensure_within_session_storage(session, session_id, artifact, path)
            alias_id = session.scalar(
                select(Artifact.id)
                .where(
                    Artifact.id != artifact.id,
                    Artifact.state != "deleted",
                    Artifact.relative_path.in_(
                        [artifact.relative_path, path.relative_to(self.paths.root).as_posix()]
                    ),
                )
                .limit(1)
            )
            if alias_id is not None:
                raise ValueError(
                    "Another artifact uses this same file; it cannot be removed safely."
                )
            if path.exists():
                path.unlink()
            removed_at = utcnow()
            artifact.state = "deleted"
            artifact.metadata_json = {
                **dict(artifact.metadata_json or {}),
                "deleted_at": removed_at.isoformat(),
            }
            artifact.updated_at = removed_at
            for export in session.scalars(
                select(ExportRecord).where(ExportRecord.artifact_id == artifact.id)
            ).all():
                export.status = "deleted"
            self._retire_output_references(session, session_id, artifact, removed_at)
        return {"artifact_id": artifact_id, "state": "deleted"}

    def _ensure_within_session_storage(
        self,
        session: Session,
        session_id: str,
        artifact: Artifact,
        path: Path,
    ) -> None:
        """Confine output deletion to its own session directory.

        Legacy layouts (assemblies/, stage-runs/, session root) all resolve
        inside the session directory, so older files stay removable without a
        migration; anything outside refuses instead of unlinking blindly.
        """

        record = session.get(SessionRecord, session_id)
        if record is None:
            raise ValueError(
                "This output cannot be removed without its session record."
            )
        storage_root = (self.paths.sessions / record.storage_key).resolve()
        try:
            path.relative_to(storage_root)
        except ValueError:
            raise ValueError(
                "This output can only be removed from its own session storage."
            ) from None

    @staticmethod
    def _ensure_output_removable(
        session: Session,
        session_id: str,
        artifact: Artifact,
    ) -> None:
        """Refuse output deletion while anything depends on the file."""

        artifact_id = artifact.id
        take_id = session.scalar(
            select(AudioTake.id)
            .where(AudioTake.artifact_id == artifact_id)
            .limit(1)
        )
        if take_id is not None:
            raise ValueError(
                "This output is still referenced by a generation take and cannot be removed."
            )
        source_id = session.scalar(
            select(SourceAsset.id)
            .where(
                SourceAsset.artifact_id == artifact_id,
                SourceAsset.state != "deleted",
            )
            .limit(1)
        )
        if source_id is not None:
            raise ValueError(
                "This output is still attached as a session source and cannot be removed."
            )
        sample_id = session.scalar(
            select(VoiceSample.id)
            .where(VoiceSample.artifact_id == artifact_id)
            .limit(1)
        )
        if sample_id is not None:
            raise ValueError(
                "This output is still used as a voice sample and cannot be removed."
            )
        shared_selection = session.scalar(
            select(SessionStageSelection.session_id).where(
                SessionStageSelection.session_id != session_id,
                SessionStageSelection.artifact_id == artifact_id,
            ).limit(1)
        )
        if shared_selection is not None:
            raise ValueError(
                "This output is selected in another session and cannot be removed."
            )
        shared_assembly_id = session.scalar(
            select(OutputAssembly.id)
            .where(
                OutputAssembly.session_id != session_id,
                OutputAssembly.artifact_id == artifact_id,
                OutputAssembly.status.in_(OUTPUT_ASSEMBLY_ACTIVE_STATUSES | {"completed"}),
            )
            .limit(1)
        )
        if shared_assembly_id is not None:
            raise ValueError(
                "This output is still referenced by another session and cannot be removed."
            )
        shared_child_id = session.scalar(
            select(Artifact.id)
            .join(
                ArtifactEdge,
                ArtifactEdge.child_artifact_id == Artifact.id,
            )
            .where(
                ArtifactEdge.parent_artifact_id == artifact_id,
                Artifact.session_id != session_id,
                Artifact.state == "current",
            )
            .limit(1)
        )
        if shared_child_id is not None:
            raise ValueError(
                "This output is still referenced by another session and cannot be removed."
            )
        active_job_id = session.scalar(
            select(Job.id)
            .where(
                Job.session_id == session_id,
                Job.status.in_(OUTPUT_JOB_ACTIVE_STATUSES),
                or_(
                    *[
                        Job.kind.startswith(prefix)
                        for prefix in OUTPUT_JOB_KIND_PREFIXES
                    ]
                ),
            )
            .limit(1)
        )
        if active_job_id is not None:
            raise ValueError(
                "An assembly or export job is still running for this session; "
                "remove this output after it finishes."
            )
        live_assembly_id = session.scalar(
            select(OutputAssembly.id)
            .where(
                OutputAssembly.session_id == session_id,
                OutputAssembly.artifact_id == artifact_id,
                OutputAssembly.status.in_(OUTPUT_ASSEMBLY_ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if live_assembly_id is not None:
            raise ValueError(
                "An assembly using this output has not finished; "
                "remove this output after it completes."
            )
        current_children = session.scalars(
            select(Artifact)
            .join(ArtifactEdge, ArtifactEdge.child_artifact_id == Artifact.id)
            .where(
                ArtifactEdge.parent_artifact_id == artifact_id,
                Artifact.state == "current",
            )
        ).all()
        for child in current_children:
            if (
                child.kind == "export"
                or child.role == "export"
                or child.role.startswith("export_")
                or child.role == "soundtrack_master"
            ):
                # Finished exports and cached soundtrack masters are
                # independently materialized files, not consumers of the
                # assembly at playback time. Keep their provenance edge.
                continue
            raise ValueError(
                "This output still has intermediate results that depend "
                "on it; remove those results first."
            )

    @staticmethod
    def _retire_output_references(
        session: Session,
        session_id: str,
        artifact: Artifact,
        removed_at,
    ) -> None:
        """Retire assembly rows and selections that pointed at the removed file."""

        for assembly in session.scalars(
            select(OutputAssembly).where(
                OutputAssembly.session_id == session_id,
                OutputAssembly.artifact_id == artifact.id,
            )
        ).all():
            if assembly.status == "completed":
                assembly.status = "stale"
            assembly.artifact_id = None
            assembly.updated_at = removed_at
        for selection in session.scalars(
            select(SessionStageSelection).where(
                SessionStageSelection.session_id == session_id,
                SessionStageSelection.artifact_id == artifact.id,
            )
        ).all():
            selection.artifact_id = None
            selection.revision += 1
            selection.updated_at = removed_at

    def reconcile(self, session_id: str | None = None) -> list[dict]:
        reports: list[dict] = []
        with self.database.session() as session:
            statement = select(Artifact).where(Artifact.state != "deleted")
            if session_id:
                statement = statement.where(Artifact.session_id == session_id)
            artifacts = list(session.scalars(statement).all())
            for artifact in artifacts:
                try:
                    path = self.paths.managed_path(artifact.relative_path)
                except ValueError as error:
                    reports.append({"artifact_id": artifact.id, "status": "escaped", "detail": str(error)})
                    continue
                if not path.is_file():
                    reports.append({"artifact_id": artifact.id, "status": "missing", "path": str(path)})
                    continue
                stat = path.stat()
                if artifact.size_bytes is not None and stat.st_size != artifact.size_bytes:
                    reports.append(
                        {
                            "artifact_id": artifact.id,
                            "status": "changed",
                            "path": str(path),
                            "expected_size": artifact.size_bytes,
                            "actual_size": stat.st_size,
                        }
                    )
        return reports
