"""Independent session branches created from reviewed subtitle checkpoints."""

from __future__ import annotations

import shutil
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    OutcomePlan,
    Segment,
    SegmentLineage,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    TimedWord,
    new_id,
)
from .sessions import SessionService

CHECKPOINT_STAGES = ("transcription", "correction", "translation")
CHECKPOINT_RANK = {stage: index for index, stage in enumerate(CHECKPOINT_STAGES)}


@dataclass(frozen=True, slots=True)
class SessionForkResult:
    record: SessionRecord
    directory: Path
    checkpoint_artifact_id: str
    copied_stages: tuple[str, ...]


class SessionForkService:
    """Clone one coherent text path and deliberately leave later work behind."""

    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        artifacts: ArtifactService,
    ) -> None:
        self.database = database
        self.paths = paths
        self.artifacts = artifacts

    @staticmethod
    def _unique_name(
        session: Session, requested: str, source: SessionRecord, stage: str
    ) -> str:
        base = str(requested or "").strip() or f"{source.name} — {stage.title()} fork"
        if len(base) > 255:
            raise ValueError("A fork name cannot be longer than 255 characters.")
        active_names = {
            str(name or "").strip().casefold()
            for name in session.scalars(
                select(SessionRecord.name).where(SessionRecord.trashed_at.is_(None))
            ).all()
        }
        if base.casefold() not in active_names:
            return base
        for number in range(2, 10_000):
            suffix = f" ({number})"
            candidate = f"{base[: 255 - len(suffix)].rstrip()}{suffix}"
            if candidate.casefold() not in active_names:
                return candidate
        raise ValueError("Could not create a unique fork name.")

    @staticmethod
    def _coherent_checkpoints(
        session: Session,
        source_session_id: str,
        checkpoint: Artifact,
    ) -> list[Artifact]:
        artifacts = {
            artifact.id: artifact
            for artifact in session.scalars(
                select(Artifact).where(Artifact.session_id == source_session_id)
            ).all()
        }
        parent_ids: dict[str, list[str]] = {}
        if artifacts:
            for parent_id, child_id in session.execute(
                select(
                    ArtifactEdge.parent_artifact_id,
                    ArtifactEdge.child_artifact_id,
                ).where(ArtifactEdge.child_artifact_id.in_(tuple(artifacts)))
            ):
                parent_ids.setdefault(child_id, []).append(parent_id)

        maximum_rank = CHECKPOINT_RANK[checkpoint.role]
        selected: dict[str, Artifact] = {}
        pending: deque[str] = deque([checkpoint.id])
        visited: set[str] = set()
        while pending:
            artifact_id = pending.popleft()
            if artifact_id in visited:
                continue
            visited.add(artifact_id)
            artifact = artifacts.get(artifact_id)
            if artifact is not None:
                rank = CHECKPOINT_RANK.get(artifact.role)
                if rank is not None and rank <= maximum_rank:
                    selected.setdefault(artifact.role, artifact)
            pending.extend(parent_ids.get(artifact_id, ()))

        if checkpoint.role not in selected:
            raise ValueError(
                "The selected checkpoint is not part of this session's text lineage."
            )
        return [selected[stage] for stage in CHECKPOINT_STAGES if stage in selected]

    @staticmethod
    def _revision_records(
        session: Session,
        source_session_id: str,
        artifact: Artifact,
        source_path: Path,
    ) -> tuple[
        str | None,
        bool,
        str,
        str | None,
        list[dict[str, Any]],
        list[TimedWord],
    ]:
        metadata = dict(artifact.metadata_json or {})
        revision_id = str(metadata.get("revision_id") or "")
        if revision_id:
            revision = session.get(DocumentRevision, revision_id)
            document = session.get(Document, revision.document_id) if revision else None
            if (
                revision is not None
                and document is not None
                and document.session_id == source_session_id
                and document.stage == artifact.role
            ):
                segments = list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == revision.id)
                        .order_by(Segment.ordinal)
                    ).all()
                )
                words = list(
                    session.scalars(
                        select(TimedWord)
                        .where(TimedWord.revision_id == revision.id)
                        .order_by(TimedWord.ordinal)
                    ).all()
                )
                return (
                    document.language,
                    revision.reviewed,
                    revision.content_hash,
                    revision.settings_hash,
                    [
                        {
                            "source_id": item.id,
                            "ordinal": item.ordinal,
                            "node_kind": item.node_kind,
                            "start_ms": item.start_ms,
                            "end_ms": item.end_ms,
                            "text": item.text,
                            "speaker": item.speaker,
                            "metadata_json": deepcopy(item.metadata_json or {}),
                        }
                        for item in segments
                    ],
                    words,
                )

        if source_path.suffix.lower() != ".srt":
            raise ValueError(
                f"The {artifact.role} checkpoint has no recoverable subtitle document."
            )
        parsed = parse_srt(source_path.read_text(encoding="utf-8-sig"))
        return (
            str(metadata.get("language") or "") or None,
            bool(metadata.get("reviewed")),
            str(artifact.content_hash or "forked-checkpoint"),
            artifact.settings_hash,
            [
                {
                    "source_id": None,
                    "ordinal": index,
                    "node_kind": "subtitle_cue",
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": item.text,
                    "speaker": item.speaker or None,
                    "metadata_json": {},
                }
                for index, item in enumerate(parsed)
            ],
            [],
        )

    def fork_in_session(
        self,
        session: Session,
        source_session_id: str,
        checkpoint_artifact_id: str,
        *,
        name: str = "",
    ) -> SessionForkResult:
        source = session.get(SessionRecord, source_session_id)
        checkpoint = session.get(Artifact, checkpoint_artifact_id)
        if source is None or source.trashed_at is not None:
            raise KeyError(source_session_id)
        if checkpoint is None or checkpoint.session_id != source_session_id:
            raise KeyError(checkpoint_artifact_id)
        if checkpoint.role not in {"correction", "translation"}:
            raise ValueError(
                "A session can be forked only after correction or translation."
            )
        if checkpoint.state == "deleted":
            raise ValueError("The selected checkpoint is no longer available.")

        checkpoints = self._coherent_checkpoints(
            session,
            source_session_id,
            checkpoint,
        )
        checkpoint_sources = [
            (artifact, self.paths.managed_path(artifact.relative_path))
            for artifact in checkpoints
        ]
        for artifact, path in checkpoint_sources:
            if not path.is_file():
                raise FileNotFoundError(
                    f"The {artifact.role} checkpoint file is missing: {path}"
                )

        record_id = new_id()
        storage_key = new_id()
        destination_dir = self.paths.sessions / storage_key
        try:
            destination_dir.mkdir(parents=True, exist_ok=False)
            record = SessionService(self.database).create(
                self._unique_name(session, name, source, checkpoint.role),
                workflow_kind=source.workflow_kind,
                source_language=source.source_language,
                target_language=source.target_language,
                workflow_preset=source.workflow_preset,
                included_stages=list(source.included_stages_json or []),
                record_id=record_id,
                storage_key=storage_key,
                db_session=session,
            )

            for setting in session.scalars(
                select(SessionSetting).where(
                    SessionSetting.session_id == source_session_id
                )
            ).all():
                session.add(
                    SessionSetting(
                        session_id=record.id,
                        section=setting.section,
                        value_json=deepcopy(setting.value_json or {}),
                    )
                )
            outcome = session.get(OutcomePlan, source_session_id)
            if outcome is not None:
                session.add(
                    OutcomePlan(
                        session_id=record.id,
                        value_json=deepcopy(outcome.value_json or {}),
                    )
                )

            source_attachments = list(
                session.scalars(
                    select(SessionSource).where(
                        SessionSource.session_id == source_session_id,
                        SessionSource.is_current.is_(True),
                    )
                ).all()
            )
            for attachment in source_attachments:
                session.add(
                    SessionSource(
                        session_id=record.id,
                        source_asset_id=attachment.source_asset_id,
                        role=attachment.role,
                    )
                )
            session.flush()

            # Source assets are intentionally shared library objects. Their
            # managed artifact remains the first immutable parent in the fork.
            source_parent_id = None
            if source_attachments:
                primary = next(
                    (item for item in source_attachments if item.role == "primary"),
                    source_attachments[0],
                )
                source_asset = session.get(SourceAsset, primary.source_asset_id)
                source_parent_id = source_asset.artifact_id if source_asset else None

            old_to_new_segments: dict[str, str] = {}
            previous_artifact_id = source_parent_id
            copied_artifacts: dict[str, Artifact] = {}
            for artifact, source_path in checkpoint_sources:
                (
                    language,
                    reviewed,
                    revision_content_hash,
                    revision_settings_hash,
                    segments,
                    words,
                ) = self._revision_records(
                    session,
                    source_session_id,
                    artifact,
                    source_path,
                )
                document = Document(
                    session_id=record.id,
                    stage=artifact.role,
                    language=language,
                )
                session.add(document)
                session.flush()
                revision = DocumentRevision(
                    document_id=document.id,
                    revision_number=1,
                    content_hash=revision_content_hash,
                    reviewed=reviewed,
                    settings_hash=revision_settings_hash,
                )
                session.add(revision)
                session.flush()
                new_segments: list[Segment] = []
                for item in segments:
                    segment = Segment(
                        revision_id=revision.id,
                        ordinal=int(item["ordinal"]),
                        node_kind=str(item["node_kind"]),
                        start_ms=item["start_ms"],
                        end_ms=item["end_ms"],
                        text=str(item["text"]),
                        speaker=item["speaker"],
                        metadata_json=deepcopy(item["metadata_json"]),
                    )
                    session.add(segment)
                    new_segments.append(segment)
                session.flush()
                for item, segment in zip(segments, new_segments, strict=True):
                    if item["source_id"]:
                        old_to_new_segments[str(item["source_id"])] = segment.id
                for word in words:
                    session.add(
                        TimedWord(
                            revision_id=revision.id,
                            segment_id=(
                                old_to_new_segments.get(str(word.segment_id))
                                if word.segment_id
                                else None
                            ),
                            ordinal=word.ordinal,
                            text=word.text,
                            start_ms=word.start_ms,
                            end_ms=word.end_ms,
                            speaker=word.speaker,
                            confidence=word.confidence,
                            metadata_json=deepcopy(word.metadata_json or {}),
                        )
                    )
                document.active_revision_id = revision.id

                destination = (
                    destination_dir
                    / "checkpoints"
                    / f"{artifact.role}-{new_id()}{source_path.suffix.lower()}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
                metadata = deepcopy(artifact.metadata_json or {})
                metadata.update(
                    {
                        "document_id": document.id,
                        "revision_id": revision.id,
                        "stage": artifact.role,
                        "language": language,
                        "forked_from_session_id": source_session_id,
                        "forked_from_artifact_id": artifact.id,
                    }
                )
                if previous_artifact_id:
                    metadata["source_artifact_id"] = previous_artifact_id
                cloned = self.artifacts.register_in_session(
                    session,
                    destination,
                    kind=artifact.kind,
                    role=artifact.role,
                    session_id=record.id,
                    parent_ids=([previous_artifact_id] if previous_artifact_id else []),
                    metadata=metadata,
                )
                cloned.settings_hash = artifact.settings_hash
                copied_artifacts[artifact.role] = cloned
                previous_artifact_id = cloned.id

            if old_to_new_segments:
                old_ids = tuple(old_to_new_segments)
                for lineage in session.scalars(
                    select(SegmentLineage).where(
                        SegmentLineage.parent_segment_id.in_(old_ids),
                        SegmentLineage.child_segment_id.in_(old_ids),
                    )
                ).all():
                    session.add(
                        SegmentLineage(
                            parent_segment_id=old_to_new_segments[
                                lineage.parent_segment_id
                            ],
                            child_segment_id=old_to_new_segments[
                                lineage.child_segment_id
                            ],
                            relation=lineage.relation,
                            sequence=lineage.sequence,
                        )
                    )
            session.flush()
            return SessionForkResult(
                record=record,
                directory=destination_dir,
                checkpoint_artifact_id=copied_artifacts[checkpoint.role].id,
                copied_stages=tuple(copied_artifacts),
            )
        except Exception:
            shutil.rmtree(destination_dir, ignore_errors=True)
            raise
