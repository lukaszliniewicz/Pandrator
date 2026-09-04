"""Revision-safe subtitle comparison and reviewed-artifact persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import func, select

from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import compose_srt, split_speaker_label

from .artifacts import ArtifactService
from .database import Database
from .models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
)

STAGE_ORDER = ("transcription", "correction", "translation", "tts_optimization")
ARTIFACT_ROLE_TO_STAGE = {
    "transcription": "transcription",
    "correction": "correction",
    "translation": "translation",
    "tts_optimized": "tts_optimization",
}
MAX_REVIEW_ARTIFACTS = 4


class ReviewedSubtitleSegment(TypedDict):
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None


def _speaker_and_text(segment: Segment) -> tuple[str, str]:
    legacy_speaker, plain_text = split_speaker_label(segment.text)
    return str(segment.speaker or legacy_speaker or "").strip(), plain_text


def _segments_hash(segments: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        {
            "start_ms": int(item["start_ms"]),
            "end_ms": int(item["end_ms"]),
            "text": str(item["text"]),
            "speaker": item.get("speaker"),
        }
        for item in segments
    ]
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class SubtitleReviewService:
    def __init__(
        self, database: Database, artifacts: ArtifactService, session_dir_resolver
    ):
        self.database = database
        self.artifacts = artifacts
        self.session_dir_resolver = session_dir_resolver

    @staticmethod
    def _payload(segment: Segment) -> dict[str, Any]:
        speaker, text = _speaker_and_text(segment)
        return {
            "id": segment.id,
            "ordinal": segment.ordinal,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": text,
            "speaker": speaker or None,
        }

    def documents(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            documents = list(
                session.scalars(
                    select(Document)
                    .where(Document.session_id == session_id)
                    .order_by(Document.created_at.desc())
                ).all()
            )
            by_stage: dict[str, Document] = {}
            for stored_document in documents:
                if (
                    stored_document.stage in STAGE_ORDER
                    and stored_document.stage not in by_stage
                ):
                    by_stage[stored_document.stage] = stored_document
            stages: dict[str, Any] = {}
            revisions: dict[str, DocumentRevision] = {}
            segment_sets: dict[str, list[Segment]] = {}
            for stage in STAGE_ORDER:
                document = by_stage.get(stage)
                if document is None or not document.active_revision_id:
                    continue
                revision = session.get(DocumentRevision, document.active_revision_id)
                if revision is None:
                    continue
                records = list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == revision.id)
                        .order_by(Segment.ordinal)
                    ).all()
                )
                revisions[stage] = revision
                segment_sets[stage] = records
                stages[stage] = {
                    "document_id": document.id,
                    "revision_id": revision.id,
                    "revision": revision.revision_number,
                    "reviewed": revision.reviewed,
                    "language": document.language,
                    "segments": [self._payload(item) for item in records],
                }
            rows = self._comparison_rows(session, segment_sets)
            return {"session_id": session_id, "stages": stages, "rows": rows}

    def catalog(self, session_id: str) -> dict[str, Any]:
        """Return lightweight metadata for every reviewable subtitle artifact.

        Segment bodies deliberately stay out of this response.  The review UI
        can therefore open a single selected revision without downloading the
        session's complete subtitle history.
        """

        with self.database.session() as session:
            artifacts = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role.in_(tuple(ARTIFACT_ROLE_TO_STAGE)),
                        Artifact.state != "deleted",
                    )
                    .order_by(Artifact.created_at.asc(), Artifact.id.asc())
                ).all()
            )
            revision_ids = {
                str((artifact.metadata_json or {}).get("revision_id") or "")
                for artifact in artifacts
            }
            revision_ids.discard("")
            rows = (
                list(
                    session.execute(
                        select(DocumentRevision, Document, func.count(Segment.id))
                        .join(Document, Document.id == DocumentRevision.document_id)
                        .outerjoin(Segment, Segment.revision_id == DocumentRevision.id)
                        .where(
                            Document.session_id == session_id,
                            DocumentRevision.id.in_(revision_ids),
                        )
                        .group_by(DocumentRevision.id, Document.id)
                    ).all()
                )
                if revision_ids
                else []
            )
            revision_by_id = {
                revision.id: (revision, document, int(segment_count))
                for revision, document, segment_count in rows
            }

            role_versions: dict[str, int] = {}
            items: list[dict[str, Any]] = []
            for artifact in artifacts:
                role_versions[artifact.role] = role_versions.get(artifact.role, 0) + 1
                revision_id = str(
                    (artifact.metadata_json or {}).get("revision_id") or ""
                )
                revision_record = revision_by_id.get(revision_id)
                if revision_record is None:
                    # An SRT without a materialized document cannot be aligned
                    # exactly.  It remains available through the ordinary file
                    # preview rather than being presented as reviewable here.
                    continue
                revision, document, segment_count = revision_record
                expected_stage = ARTIFACT_ROLE_TO_STAGE[artifact.role]
                if document.stage != expected_stage:
                    continue
                items.append(
                    {
                        "artifact_id": artifact.id,
                        "role": artifact.role,
                        "stage": document.stage,
                        "version": role_versions[artifact.role],
                        "document_id": document.id,
                        "revision_id": revision.id,
                        "revision": revision.revision_number,
                        "reviewed": revision.reviewed,
                        "language": document.language,
                        "segment_count": segment_count,
                        "state": artifact.state,
                        "created_at": artifact.created_at.isoformat(),
                    }
                )
            items.reverse()
            return {"session_id": session_id, "items": items}

    def review(self, session_id: str, artifact_ids: list[str]) -> dict[str, Any]:
        """Load one to four exact immutable artifact revisions for comparison."""

        ordered_ids = list(
            dict.fromkeys(
                str(item).strip() for item in artifact_ids if str(item).strip()
            )
        )
        if not ordered_ids:
            raise ValueError("Choose at least one subtitle artifact to review.")
        if len(ordered_ids) > MAX_REVIEW_ARTIFACTS:
            raise ValueError(
                f"At most {MAX_REVIEW_ARTIFACTS} subtitle artifacts can be compared at once."
            )

        with self.database.session() as session:
            artifacts_by_id = {
                artifact.id: artifact
                for artifact in session.scalars(
                    select(Artifact).where(Artifact.id.in_(ordered_ids))
                ).all()
            }
            if any(
                artifact_id not in artifacts_by_id
                or artifacts_by_id[artifact_id].session_id != session_id
                or artifacts_by_id[artifact_id].role not in ARTIFACT_ROLE_TO_STAGE
                or artifacts_by_id[artifact_id].state == "deleted"
                for artifact_id in ordered_ids
            ):
                raise KeyError("subtitle artifact")

            revision_id_by_artifact = {
                artifact_id: str(
                    (artifacts_by_id[artifact_id].metadata_json or {}).get(
                        "revision_id"
                    )
                    or ""
                )
                for artifact_id in ordered_ids
            }
            if any(not revision_id for revision_id in revision_id_by_artifact.values()):
                raise ValueError(
                    "One of the selected artifacts has no exact subtitle revision metadata."
                )
            revision_ids = list(revision_id_by_artifact.values())
            revision_rows = list(
                session.execute(
                    select(DocumentRevision, Document)
                    .join(Document, Document.id == DocumentRevision.document_id)
                    .where(
                        Document.session_id == session_id,
                        DocumentRevision.id.in_(revision_ids),
                    )
                ).all()
            )
            revision_by_id = {
                revision.id: (revision, document)
                for revision, document in revision_rows
            }
            if any(revision_id not in revision_by_id for revision_id in revision_ids):
                raise ValueError(
                    "One of the selected subtitle revisions is no longer available."
                )

            segment_rows = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id.in_(revision_ids))
                    .order_by(Segment.revision_id, Segment.ordinal)
                ).all()
            )
            segments_by_revision: dict[str, list[Segment]] = {
                revision_id: [] for revision_id in revision_ids
            }
            for segment in segment_rows:
                segments_by_revision[segment.revision_id].append(segment)

            columns: list[dict[str, Any]] = []
            segment_sets: dict[str, list[Segment]] = {}
            for artifact_id in ordered_ids:
                artifact = artifacts_by_id[artifact_id]
                revision_id = revision_id_by_artifact[artifact_id]
                revision, document = revision_by_id[revision_id]
                expected_stage = ARTIFACT_ROLE_TO_STAGE[artifact.role]
                if document.stage != expected_stage:
                    raise ValueError(
                        "A selected artifact points to a subtitle revision from a different stage."
                    )
                records = segments_by_revision[revision_id]
                segment_sets[artifact_id] = records
                columns.append(
                    {
                        "artifact_id": artifact_id,
                        "role": artifact.role,
                        "stage": document.stage,
                        "document_id": document.id,
                        "revision_id": revision.id,
                        "revision": revision.revision_number,
                        "reviewed": revision.reviewed,
                        "language": document.language,
                        "segments": [self._payload(item) for item in records],
                    }
                )
            rows = self._comparison_rows_by_key(session, segment_sets, ordered_ids)
            return {
                "session_id": session_id,
                "primary_artifact_id": ordered_ids[0],
                "columns": columns,
                "rows": rows,
            }

    def _comparison_rows(
        self, session, stage_segments: dict[str, list[Segment]]
    ) -> list[dict[str, Any]]:
        return self._comparison_rows_by_key(
            session,
            stage_segments,
            [stage for stage in STAGE_ORDER if stage in stage_segments],
        )

    def _comparison_rows_by_key(
        self,
        session,
        stage_segments: dict[str, list[Segment]],
        present: list[str],
    ) -> list[dict[str, Any]]:
        nodes = [
            (stage, item.id)
            for stage, records in stage_segments.items()
            for item in records
        ]
        parent = {node: node for node in nodes}

        def find(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left, right):
            if left not in parent or right not in parent:
                return
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        segment_stage = {
            item.id: stage
            for stage, records in stage_segments.items()
            for item in records
        }
        segment_by_id = {
            item.id: item for records in stage_segments.values() for item in records
        }
        ids = list(segment_by_id)
        lineage = (
            list(
                session.scalars(
                    select(SegmentLineage).where(
                        SegmentLineage.parent_segment_id.in_(ids),
                        SegmentLineage.child_segment_id.in_(ids),
                    )
                ).all()
            )
            if ids
            else []
        )
        lineage_pairs: set[tuple[str, str]] = set()
        for edge in lineage:
            left_stage = segment_stage.get(edge.parent_segment_id)
            right_stage = segment_stage.get(edge.child_segment_id)
            if left_stage and right_stage:
                lineage_pairs.add((left_stage, right_stage))
                union(
                    (left_stage, edge.parent_segment_id),
                    (right_stage, edge.child_segment_id),
                )

        for left_stage, right_stage in pairwise(present):
            if (left_stage, right_stage) in lineage_pairs:
                continue
            # Legacy revisions do not always have lineage edges.  Both lists
            # are ordinal/time ordered, so a two-pointer interval sweep aligns
            # them in O(n + m), including one-to-many overlaps, instead of the
            # former quadratic nested scan.
            left_records = sorted(
                (
                    item
                    for item in stage_segments[left_stage]
                    if item.start_ms is not None and item.end_ms is not None
                ),
                key=lambda item: (item.start_ms, item.end_ms, item.ordinal),
            )
            right_records = sorted(
                (
                    item
                    for item in stage_segments[right_stage]
                    if item.start_ms is not None and item.end_ms is not None
                ),
                key=lambda item: (item.start_ms, item.end_ms, item.ordinal),
            )
            left_index = right_index = 0
            while left_index < len(left_records) and right_index < len(right_records):
                left = left_records[left_index]
                right = right_records[right_index]
                assert left.start_ms is not None and left.end_ms is not None
                assert right.start_ms is not None and right.end_ms is not None
                if min(left.end_ms, right.end_ms) > max(left.start_ms, right.start_ms):
                    union((left_stage, left.id), (right_stage, right.id))
                if left.end_ms <= right.end_ms:
                    left_index += 1
                else:
                    right_index += 1

        groups: dict[Any, list[tuple[str, str]]] = {}
        for node in nodes:
            groups.setdefault(find(node), []).append(node)
        result = []
        for members in groups.values():
            records = [segment_by_id[segment_id] for _stage, segment_id in members]
            row: dict[str, Any] = {
                "start_ms": min(item.start_ms or 0 for item in records),
                "end_ms": max(item.end_ms or 0 for item in records),
            }
            values = []
            for stage in present:
                items = [
                    segment_by_id[segment_id]
                    for member_stage, segment_id in members
                    if member_stage == stage
                ]
                items.sort(key=lambda item: item.ordinal)
                row[stage] = [self._payload(item) for item in items]
                values.append("\n".join(_speaker_and_text(item)[1] for item in items))
            row["changed"] = len(set(values)) > 1
            if present and any(stage not in STAGE_ORDER for stage in present):
                row["cells"] = {stage: row.pop(stage) for stage in present}
            result.append(row)
        return sorted(result, key=lambda item: (item["start_ms"], item["end_ms"]))

    def save_review(
        self,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        source_artifact_id: str | None = None,
        db_session=None,
        published_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unsupported subtitle stage: {stage}")
        normalized: list[ReviewedSubtitleSegment] = []
        for index, item in enumerate(values):
            start_ms = int(item.get("start_ms") or 0)
            end_ms = int(item.get("end_ms") or 0)
            legacy_speaker, text = split_speaker_label(
                str(item.get("text") or "").strip()
            )
            if not text:
                continue
            if start_ms < 0 or end_ms <= start_ms:
                raise ValueError(f"Segment {index + 1} has invalid timing.")
            normalized.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text,
                    "speaker": str(item.get("speaker") or legacy_speaker or "").strip()
                    or None,
                }
            )
        if not normalized:
            raise ValueError("A reviewed subtitle document cannot be empty.")

        with (
            self.database.session()
            if db_session is None
            else _SessionContext(db_session)
        ) as session:
            source_artifact = (
                session.get(Artifact, source_artifact_id)
                if source_artifact_id
                else None
            )
            if source_artifact_id and (
                source_artifact is None
                or source_artifact.session_id != session_id
                or ARTIFACT_ROLE_TO_STAGE.get(source_artifact.role) != stage
            ):
                raise KeyError(source_artifact_id)
            source_metadata = (
                source_artifact.metadata_json or {} if source_artifact else {}
            )
            if source_artifact:
                document = session.get(
                    Document, str(source_metadata.get("document_id") or "")
                )
                previous = session.get(
                    DocumentRevision, str(source_metadata.get("revision_id") or "")
                )
            else:
                document = session.scalar(
                    select(Document)
                    .where(Document.session_id == session_id, Document.stage == stage)
                    .order_by(Document.created_at.desc())
                )
                previous = (
                    session.get(DocumentRevision, document.active_revision_id)
                    if document and document.active_revision_id
                    else None
                )
            if document is None:
                if source_artifact is not None:
                    raise KeyError(stage)
                if expected_revision != 0:
                    raise RuntimeError(
                        f"Subtitle revision changed from {expected_revision} to 0."
                    )
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                language = (
                    record.target_language
                    if stage in {"translation", "tts_optimization"}
                    else record.source_language
                )
                document = Document(
                    session_id=session_id,
                    stage=stage,
                    language=(None if language == "auto" else language),
                )
                session.add(document)
                session.flush()
                previous = None
                previous_segments: list[Segment] = []
                next_revision_number = 1
            else:
                if (
                    document.session_id != session_id
                    or document.stage != stage
                    or previous is None
                ):
                    raise KeyError(stage)
                if (
                    previous.document_id != document.id
                    or previous.revision_number != expected_revision
                ):
                    actual = previous.revision_number if previous else 0
                    raise RuntimeError(
                        f"Subtitle revision changed from {expected_revision} to {actual}."
                    )
                previous_segments = list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == previous.id)
                        .order_by(Segment.ordinal)
                    ).all()
                )
            for index, reviewed in enumerate(normalized, start=1):
                overlapping_speakers: dict[str, str] = {}
                for previous_segment in previous_segments:
                    if (
                        previous_segment.start_ms is None
                        or previous_segment.end_ms is None
                        or min(reviewed["end_ms"], previous_segment.end_ms)
                        <= max(reviewed["start_ms"], previous_segment.start_ms)
                    ):
                        continue
                    speaker, _text = _speaker_and_text(previous_segment)
                    if speaker:
                        overlapping_speakers.setdefault(speaker.casefold(), speaker)
                if len(overlapping_speakers) > 1:
                    raise ValueError(
                        f"Segment {index} crosses a speaker boundary. Keep each speaker in a separate cue."
                    )
                if not reviewed["speaker"] and overlapping_speakers:
                    reviewed["speaker"] = next(iter(overlapping_speakers.values()))
            next_revision_number = (
                int(
                    session.scalar(
                        select(func.max(DocumentRevision.revision_number)).where(
                            DocumentRevision.document_id == document.id
                        )
                    )
                    or 0
                )
                + 1
            )
            revision = DocumentRevision(
                document_id=document.id,
                parent_revision_id=previous.id if previous else None,
                revision_number=next_revision_number,
                content_hash=_segments_hash(normalized),
                reviewed=True,
            )
            session.add(revision)
            session.flush()
            children = []
            for ordinal, reviewed in enumerate(normalized):
                child = Segment(revision_id=revision.id, ordinal=ordinal, **reviewed)
                session.add(child)
                children.append(child)
            session.flush()
            for child in children:
                for sequence, parent_segment in enumerate(
                    item
                    for item in previous_segments
                    if item.start_ms is not None
                    and item.end_ms is not None
                    and min(child.end_ms or 0, item.end_ms)
                    > max(child.start_ms or 0, item.start_ms)
                ):
                    session.add(
                        SegmentLineage(
                            parent_segment_id=parent_segment.id,
                            child_segment_id=child.id,
                            relation="reviewed",
                            sequence=sequence,
                        )
                    )
            document.active_revision_id = revision.id
            language = document.language
            document_id = document.id
            revision_id = revision.id
            revision_number = revision.revision_number

        content = compose_srt(
            [
                SubtitleSegment(
                    index=index,
                    start_ms=item["start_ms"],
                    end_ms=item["end_ms"],
                    text=item["text"],
                    speaker=str(item.get("speaker") or ""),
                )
                for index, item in enumerate(normalized, start=1)
            ]
        )
        speakers = {
            str(item.get("speaker") or "").strip().casefold()
            for item in normalized
            if str(item.get("speaker") or "").strip()
        }
        destination: Path = (
            self.session_dir_resolver(session_id)
            / f"reviewed_{stage}_r{revision_number}.srt"
        )
        newly_published = self._publish_reviewed_file(destination, content)
        if newly_published and published_paths is not None:
            published_paths.append(destination)
        with (
            self.database.session()
            if db_session is None
            else _SessionContext(db_session)
        ) as session:
            parent_artifact = source_artifact or session.scalar(
                select(Artifact)
                .where(
                    Artifact.session_id == session_id,
                    Artifact.role
                    == ("tts_optimized" if stage == "tts_optimization" else stage),
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc())
            )
            parent_id = parent_artifact.id if parent_artifact else None
            try:
                artifact = self.artifacts.register_in_session(
                    session,
                    destination,
                    kind="srt",
                    role="tts_optimized" if stage == "tts_optimization" else stage,
                    session_id=session_id,
                    parent_ids=[parent_id] if parent_id else [],
                    settings={"reviewed": True, "revision": revision_number},
                    metadata={
                        "document_id": document_id,
                        "revision_id": revision_id,
                        "stage": stage,
                        "language": language,
                        "reviewed": True,
                        "has_speaker_metadata": bool(speakers),
                        "speaker_count": len(speakers),
                    },
                )
            except Exception:
                if newly_published:
                    destination.unlink(missing_ok=True)
                    if published_paths is not None:
                        published_paths.remove(destination)
                raise
        return {
            "artifact_id": artifact.id,
            "document_id": document_id,
            "revision_id": revision_id,
            "revision": revision_number,
        }

    def save_review_in_session(
        self,
        session,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        source_artifact_id: str | None = None,
        published_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Persist a review without committing the caller-owned transaction."""
        return self.save_review(
            session_id,
            stage,
            expected_revision,
            values,
            source_artifact_id=source_artifact_id,
            db_session=session,
            published_paths=published_paths,
        )

    @staticmethod
    def _publish_reviewed_file(destination: Path, content: str) -> bool:
        """Durably replace a deterministic reviewed subtitle file.

        SQLite cannot roll back the filesystem.  Callers receive whether this
        attempt created the destination so a surrounding DB rollback can remove
        only a file it safely owns; retries overwrite the same revision path.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        newly_published = not destination.exists()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The replacement itself is durable on common local filesystems;
                # directory fsync is a best-effort portability enhancement.
                pass
            return newly_published
        finally:
            temporary.unlink(missing_ok=True)


class _SessionContext:
    """Adapt an existing SQLAlchemy session to a no-commit context manager."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, traceback):
        return False
