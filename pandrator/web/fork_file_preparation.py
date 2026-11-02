"""Private, verified file preparation for atomic synchronous session branches.

Collection uses an authorized read snapshot. Materialization must run only after
that snapshot closes; consumption uses stat checks and rename, never file reads.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import PreparedArtifactRegistration, sha256_file
from .models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    Segment,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    SubtitleEvidence,
    TimedWord,
    TranslationProject,
    new_id,
)
from .settings_policy import RevisionConflict


class _ForkService(Protocol):
    paths: DataPaths

    def validate_media_checkpoint(
        self, session: Session, source: SessionRecord, checkpoint: Artifact,
        *, carry_media_assets: bool = True,
    ) -> tuple[MediaEditPlanRevision | None, list[Artifact], list[Artifact]]: ...

    def _revision_records(
        self, session: Session | None, source_session_id: str,
        artifact: Artifact, source_path: Path,
    ) -> Any: ...

_FORBIDDEN_ROLES = {
    "assembled_audio", "audiobook_audio", "dubbing_audio", "export",
    "export_media", "generated_audio", "tts_audio",
}


def _row(value: Any) -> dict[str, Any]:
    return {
        column.key: deepcopy(getattr(value, column.key))
        for column in inspect(type(value)).column_attrs
    }


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    if not path.is_file():
        raise FileNotFoundError(f"Referenced fork artifact file is missing: {path}")
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(slots=True)
class _TargetFiles:
    storage_key: str
    staging: Path
    destination: Path
    registrations: dict[str, PreparedArtifactRegistration] = field(default_factory=dict)
    signatures: dict[str, tuple[int, int, int, int, int]] = field(default_factory=dict)
    installed: bool = False
    created: bool = False


@dataclass(slots=True)
class _PreparedForkFiles:
    """A request-owned manifest and independent per-target file trees."""

    service: _ForkService
    source_session_id: str
    checkpoint_artifact_id: str
    target_count: int
    project_id: str | None
    fingerprint: str
    artifacts: dict[str, dict[str, Any]]
    shared_ids: set[str]
    checkpoint_ids: set[str]
    revision_records: dict[str, Any]
    fallback_artifacts: dict[str, Artifact]
    source_signatures: dict[str, tuple[int, int, int, int, int]] = field(default_factory=dict)
    targets: list[_TargetFiles] = field(default_factory=list)
    next_target: int = 0

    @classmethod
    def collect(
        cls, db: Session, service: _ForkService, source_session_id: str,
        checkpoint_artifact_id: str, target_count: int, *, project_id: str | None = None,
    ) -> _PreparedForkFiles:
        """Collect only database data; never inspect or open source files here."""
        source = db.get(SessionRecord, source_session_id)
        checkpoint = db.get(Artifact, checkpoint_artifact_id)
        if source is None or source.trashed_at is not None:
            raise KeyError(source_session_id)
        if checkpoint is None or checkpoint.session_id != source_session_id:
            raise KeyError(checkpoint_artifact_id)
        active_edit, renders, checkpoints = service.validate_media_checkpoint(
            db, source, checkpoint, carry_media_assets=True,
        )
        rows: dict[str, Any] = {"source": _row(source)}
        if project_id:
            project = db.get(TranslationProject, project_id)
            if project is None:
                raise KeyError(project_id)
            rows["project"] = _row(project)
        for model in (SessionSetting, SessionStageSelection, SessionSource):
            values = list(db.scalars(select(model).where(model.session_id == source_session_id)))
            rows[model.__name__] = sorted((_row(value) for value in values), key=str)
        outcome = db.get(OutcomePlan, source_session_id)
        rows["outcome"] = _row(outcome) if outcome else None
        attachments = list(db.scalars(select(SessionSource).where(
            SessionSource.session_id == source_session_id,
            SessionSource.is_current.is_(True),
        )))
        shared_ids: set[str] = set()
        for attachment in attachments:
            asset = db.get(SourceAsset, attachment.source_asset_id)
            if asset is None or asset.state != "current":
                raise ValueError("A current source attachment is unavailable for this fork.")
            rows[f"asset:{asset.id}"] = _row(asset)
            if asset.artifact_id:
                shared_ids.add(asset.artifact_id)
        plan = db.scalar(select(MediaEditPlan).where(MediaEditPlan.session_id == source_session_id))
        rows["plan"] = _row(plan) if plan else None
        rows["edit"] = _row(active_edit) if active_edit else None
        selected_ids = {item.id for item in checkpoints}
        artifacts: dict[str, dict[str, Any]] = {}
        revisions: set[str] = set()
        evidence_ids: set[str] = set()
        evidence_done: set[str] = set()
        visiting: set[str] = set()
        revision_records: dict[str, Any] = {}
        fallback_artifacts: dict[str, Artifact] = {}

        def add_evidence(value: Any) -> None:
            if isinstance(value, dict):
                evidence_ids.update(str(item) for item in value.get("evidence_ids") or [])

        def revision(revision_id: str) -> None:
            if revision_id in revisions:
                return
            original = db.get(DocumentRevision, revision_id)
            document = db.get(Document, original.document_id) if original else None
            if original is None or document is None or document.session_id != source_session_id:
                raise ValueError(f"Referenced subtitle revision is unavailable: {revision_id}")
            revisions.add(revision_id)
            rows[f"revision:{revision_id}"] = _row(original)
            rows[f"document:{document.id}"] = _row(document)
            segments = list(db.scalars(select(Segment).where(Segment.revision_id == revision_id)))
            rows[f"segments:{revision_id}"] = sorted((_row(item) for item in segments), key=str)
            for segment in segments:
                add_evidence(segment.metadata_json)
            rows[f"words:{revision_id}"] = sorted((
                _row(item) for item in db.scalars(select(TimedWord).where(TimedWord.revision_id == revision_id))
            ), key=str)

        def artifact(artifact_id: str) -> None:
            if artifact_id in visiting:
                raise ValueError("Artifact provenance contains a cycle.")
            if artifact_id in artifacts:
                return
            original = db.get(Artifact, artifact_id)
            if original is None or original.state == "deleted":
                raise ValueError(f"Referenced fork artifact is unavailable: {artifact_id}")
            if artifact_id not in shared_ids and original.session_id != source_session_id:
                raise ValueError(f"Referenced artifact belongs to another session: {artifact_id}")
            if artifact_id not in shared_ids and original.role in _FORBIDDEN_ROLES:
                raise ValueError(f"A generated output cannot be used as a fork dependency: {original.role}")
            artifacts[artifact_id] = _row(original)
            if artifact_id in shared_ids:
                return
            metadata = original.metadata_json or {}
            add_evidence(metadata)
            packet = metadata.get("logical_passages")
            if isinstance(packet, dict):
                for item in packet.get("items") or []:
                    add_evidence(item)
            revision_id = str(metadata.get("revision_id") or "")
            linked = db.get(DocumentRevision, revision_id) if revision_id else None
            document = db.get(Document, linked.document_id) if linked else None
            if (
                document is not None and document.session_id == source_session_id
                and (artifact_id not in selected_ids or document.stage == original.role)
            ):
                revision(revision_id)
            if artifact_id in selected_ids:
                return  # Checkpoint provenance is rebuilt by the clone service.
            visiting.add(artifact_id)
            parents = sorted(db.scalars(select(ArtifactEdge.parent_artifact_id).where(
                ArtifactEdge.child_artifact_id == artifact_id,
            )))
            rows[f"parents:{artifact_id}"] = parents
            for parent_id in parents:
                artifact(parent_id)
            visiting.remove(artifact_id)

        for shared_id in shared_ids:
            artifact(shared_id)
        for item in checkpoints:
            artifact(item.id)
            # Cache the database-backed path; defer legacy SRT parsing until the
            # read snapshot is closed, alongside all other file reads.
            metadata = item.metadata_json or {}
            linked = db.get(DocumentRevision, str(metadata.get("revision_id") or ""))
            document = db.get(Document, linked.document_id) if linked else None
            if document and document.session_id == source_session_id and document.stage == item.role:
                revision_records[item.id] = service._revision_records(
                    db, source_session_id, item, service.paths.managed_path(item.relative_path),
                )
            else:
                fallback_artifacts[item.id] = item
        if active_edit:
            for artifact_id in (
                active_edit.source_media_artifact_id,
                active_edit.editorial_transcript_artifact_id,
                active_edit.timing_artifact_id,
                *(item.id for item in renders),
            ):
                if artifact_id:
                    artifact(artifact_id)
            for item in checkpoints:
                parents = set(db.scalars(select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == item.id,
                )))
                declared = str((item.metadata_json or {}).get("source_artifact_id") or "")
                if declared:
                    parents.add(declared)
                rows[f"parents:{item.id}"] = sorted(parents)
                for parent_id in parents:
                    artifact(parent_id)
        # Evidence may introduce more artifacts and revisions which themselves
        # carry evidence. Resolve this complete closure before touching files.
        while evidence_ids - evidence_done:
            evidence_id = sorted(evidence_ids - evidence_done)[0]
            evidence_done.add(evidence_id)
            evidence = db.get(SubtitleEvidence, evidence_id)
            if evidence is None or evidence.session_id != source_session_id:
                raise ValueError(f"Referenced subtitle evidence is unavailable: {evidence_id}")
            if evidence.status in {"queued", "running"}:
                raise ValueError("Pending subtitle evidence cannot be copied into a fork.")
            rows[f"evidence:{evidence_id}"] = _row(evidence)
            revision(evidence.source_revision_id)
            for artifact_id in (
                evidence.source_artifact_id, evidence.source_media_artifact_id,
                evidence.clip_artifact_id,
                *(str(item.get("transcript_artifact_id") or "")
                  for item in evidence.candidates_json or [] if isinstance(item, dict)),
            ):
                if artifact_id:
                    artifact(artifact_id)
        rows["artifacts"] = artifacts
        fingerprint = json.dumps(rows, sort_keys=True, default=str, separators=(",", ":"))
        return cls(service, source_session_id, checkpoint_artifact_id, target_count,
                   project_id, fingerprint, artifacts, shared_ids, selected_ids,
                   revision_records, fallback_artifacts)

    def materialize(self) -> None:
        """Hash each original once, then copy and verify each independent target."""
        try:
            hashes: dict[str, str] = {}
            for artifact_id, row in self.artifacts.items():
                path = self.service.paths.managed_path(row["relative_path"])
                before = _signature(path)
                digest = sha256_file(path)
                if (row["content_hash"] and digest != row["content_hash"]) or before != _signature(path):
                    raise RevisionConflict(f"Referenced fork artifact hash changed: {artifact_id}")
                hashes[artifact_id] = digest
                self.source_signatures[artifact_id] = before
            for artifact_id, original in self.fallback_artifacts.items():
                # A deliberately metadata-free artifact makes _revision_records
                # use its existing SRT fallback without querying a closed session.
                fallback = Artifact(role=original.role, content_hash=original.content_hash,
                                    settings_hash=original.settings_hash,
                                    metadata_json={key: value for key, value in (original.metadata_json or {}).items()
                                                   if key != "revision_id"})
                path = self.service.paths.managed_path(original.relative_path)
                self.revision_records[artifact_id] = self.service._revision_records(
                    None, self.source_session_id, fallback, path,
                )
                if self.source_signatures[artifact_id] != _signature(path):
                    raise RevisionConflict("The source checkpoint changed during preparation.")
            for _ in range(self.target_count):
                storage_key = new_id()
                target = _TargetFiles(storage_key,
                    self.service.paths.sessions / f".fork-preparation-{new_id()}",
                    self.service.paths.sessions / storage_key)
                self.targets.append(target)
                target.staging.mkdir(mode=0o700, parents=True, exist_ok=False)
                target.created = True
                for artifact_id, row in self.artifacts.items():
                    if artifact_id in self.shared_ids:
                        continue
                    source = self.service.paths.managed_path(row["relative_path"])
                    kind = "checkpoints" if artifact_id in self.checkpoint_ids else "dependencies"
                    relative = Path(kind) / f"{new_id()}{source.suffix.lower()}"
                    staged = target.staging / relative
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, staged)
                    staged_signature = _signature(staged)
                    digest = sha256_file(staged)
                    if (
                        digest != hashes[artifact_id]
                        or staged_signature != _signature(staged)
                        or self.source_signatures[artifact_id] != _signature(source)
                    ):
                        raise RevisionConflict(f"Referenced fork artifact hash changed: {artifact_id}")
                    target.signatures[artifact_id] = staged_signature
                    target.registrations[artifact_id] = PreparedArtifactRegistration(
                        self.service.paths.relative_managed_path(target.destination / relative),
                        mimetypes.guess_type(staged.name)[0], staged.stat().st_size, digest, None,
                    )
        except BaseException:
            self.cleanup(committed=False)
            raise

    def revalidate(self, db: Session) -> None:
        try:
            current = self.collect(db, self.service, self.source_session_id,
                                   self.checkpoint_artifact_id, self.target_count,
                                   project_id=self.project_id)
        except (KeyError, ValueError, OSError) as error:
            raise RevisionConflict("The branch source changed during file preparation.") from error
        if current.fingerprint != self.fingerprint:
            raise RevisionConflict("The branch source changed during file preparation.")
        for artifact_id, expected in self.source_signatures.items():
            path = self.service.paths.managed_path(self.artifacts[artifact_id]["relative_path"])
            if _signature(path) != expected:
                raise RevisionConflict("The branch source file changed during preparation.")

    def install_target(self) -> _TargetFiles:
        if self.next_target >= len(self.targets):
            raise RevisionConflict("A branch has no prepared file tree.")
        target = self.targets[self.next_target]
        self.next_target += 1
        for artifact_id, expected in target.signatures.items():
            registration = target.registrations[artifact_id]
            relative = self.service.paths.managed_path(registration.relative_path).relative_to(target.destination)
            if _signature(target.staging / relative) != expected:
                raise RevisionConflict("A prepared branch file changed before installation.")
        if target.destination.exists():
            raise RevisionConflict("The prepared branch destination already exists.")
        target.staging.rename(target.destination)
        target.installed = True
        return target

    def cleanup(self, *, committed: bool) -> None:
        for target in self.targets:
            if target.created:
                shutil.rmtree(target.staging, ignore_errors=True)
            if target.installed and not committed:
                shutil.rmtree(target.destination, ignore_errors=True)
