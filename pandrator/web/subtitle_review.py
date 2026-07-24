"""Revision-safe subtitle comparison and reviewed-artifact persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import compose_srt, split_speaker_label

from .artifacts import ArtifactService
from .database import Database
from .models import Artifact, Document, DocumentRevision, Segment, SegmentLineage


STAGE_ORDER = ("transcription", "correction", "translation", "tts_optimization")


def _speaker_and_text(segment: Segment) -> tuple[str, str]:
    legacy_speaker, plain_text = split_speaker_label(segment.text)
    return str(segment.speaker or legacy_speaker or "").strip(), plain_text


def _segments_hash(segments: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "start_ms": int(item["start_ms"]),
            "end_ms": int(item["end_ms"]),
            "text": str(item["text"]),
            "speaker": item.get("speaker"),
        }
        for item in segments
    ]
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class SubtitleReviewService:
    def __init__(self, database: Database, artifacts: ArtifactService, session_dir_resolver):
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
                    select(Document).where(Document.session_id == session_id).order_by(Document.created_at.desc())
                ).all()
            )
            by_stage: dict[str, Document] = {}
            for document in documents:
                if document.stage in STAGE_ORDER and document.stage not in by_stage:
                    by_stage[document.stage] = document
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
                        select(Segment).where(Segment.revision_id == revision.id).order_by(Segment.ordinal)
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

    def _comparison_rows(self, session, stage_segments: dict[str, list[Segment]]) -> list[dict[str, Any]]:
        nodes = [(stage, item.id) for stage, records in stage_segments.items() for item in records]
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

        segment_stage = {item.id: stage for stage, records in stage_segments.items() for item in records}
        segment_by_id = {item.id: item for records in stage_segments.values() for item in records}
        ids = list(segment_by_id)
        lineage = list(
            session.scalars(
                select(SegmentLineage).where(
                    SegmentLineage.parent_segment_id.in_(ids),
                    SegmentLineage.child_segment_id.in_(ids),
                )
            ).all()
        ) if ids else []
        lineage_pairs: set[tuple[str, str]] = set()
        for edge in lineage:
            left_stage = segment_stage.get(edge.parent_segment_id)
            right_stage = segment_stage.get(edge.child_segment_id)
            if left_stage and right_stage:
                lineage_pairs.add((left_stage, right_stage))
                union((left_stage, edge.parent_segment_id), (right_stage, edge.child_segment_id))

        present = [stage for stage in STAGE_ORDER if stage in stage_segments]
        for left_stage, right_stage in zip(present, present[1:]):
            if (left_stage, right_stage) in lineage_pairs:
                continue
            for left in stage_segments[left_stage]:
                for right in stage_segments[right_stage]:
                    if left.start_ms is None or left.end_ms is None or right.start_ms is None or right.end_ms is None:
                        continue
                    if min(left.end_ms, right.end_ms) > max(left.start_ms, right.start_ms):
                        union((left_stage, left.id), (right_stage, right.id))

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
                items = [segment_by_id[segment_id] for member_stage, segment_id in members if member_stage == stage]
                items.sort(key=lambda item: item.ordinal)
                row[stage] = [self._payload(item) for item in items]
                values.append("\n".join(_speaker_and_text(item)[1] for item in items))
            row["changed"] = len(set(values)) > 1
            result.append(row)
        return sorted(result, key=lambda item: (item["start_ms"], item["end_ms"]))

    def save_review(self, session_id: str, stage: str, expected_revision: int, values: list[dict[str, Any]]) -> dict[str, Any]:
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unsupported subtitle stage: {stage}")
        normalized = []
        for index, item in enumerate(values):
            start_ms = int(item.get("start_ms") or 0)
            end_ms = int(item.get("end_ms") or 0)
            legacy_speaker, text = split_speaker_label(str(item.get("text") or "").strip())
            if not text:
                continue
            if start_ms < 0 or end_ms <= start_ms:
                raise ValueError(f"Segment {index + 1} has invalid timing.")
            normalized.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text,
                    "speaker": str(item.get("speaker") or legacy_speaker or "").strip() or None,
                }
            )
        if not normalized:
            raise ValueError("A reviewed subtitle document cannot be empty.")

        with self.database.session() as session:
            document = session.scalar(
                select(Document).where(Document.session_id == session_id, Document.stage == stage).order_by(Document.created_at.desc())
            )
            if document is None or not document.active_revision_id:
                raise KeyError(stage)
            previous = session.get(DocumentRevision, document.active_revision_id)
            if previous is None or previous.revision_number != expected_revision:
                actual = previous.revision_number if previous else 0
                raise RuntimeError(f"Subtitle revision changed from {expected_revision} to {actual}.")
            previous_segments = list(
                session.scalars(select(Segment).where(Segment.revision_id == previous.id).order_by(Segment.ordinal)).all()
            )
            for index, item in enumerate(normalized, start=1):
                overlapping_speakers: dict[str, str] = {}
                for previous_segment in previous_segments:
                    if (
                        previous_segment.start_ms is None
                        or previous_segment.end_ms is None
                        or min(item["end_ms"], previous_segment.end_ms)
                        <= max(item["start_ms"], previous_segment.start_ms)
                    ):
                        continue
                    speaker, _text = _speaker_and_text(previous_segment)
                    if speaker:
                        overlapping_speakers.setdefault(speaker.casefold(), speaker)
                if len(overlapping_speakers) > 1:
                    raise ValueError(
                        f"Segment {index} crosses a speaker boundary. Keep each speaker in a separate cue."
                    )
                if not item["speaker"] and overlapping_speakers:
                    item["speaker"] = next(iter(overlapping_speakers.values()))
            revision = DocumentRevision(
                document_id=document.id,
                parent_revision_id=previous.id,
                revision_number=previous.revision_number + 1,
                content_hash=_segments_hash(normalized),
                reviewed=True,
            )
            session.add(revision)
            session.flush()
            children = []
            for ordinal, item in enumerate(normalized):
                child = Segment(revision_id=revision.id, ordinal=ordinal, **item)
                session.add(child)
                children.append(child)
            session.flush()
            for child in children:
                for sequence, parent_segment in enumerate(
                    item for item in previous_segments
                    if item.start_ms is not None and item.end_ms is not None
                    and min(child.end_ms or 0, item.end_ms) > max(child.start_ms or 0, item.start_ms)
                ):
                    session.add(SegmentLineage(parent_segment_id=parent_segment.id, child_segment_id=child.id, relation="reviewed", sequence=sequence))
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
        destination: Path = self.session_dir_resolver(session_id) / f"reviewed_{stage}_r{revision_number}.srt"
        destination.write_text(content, encoding="utf-8", newline="\n")
        with self.database.session() as session:
            parent_artifact = session.scalar(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role == stage,
                    Artifact.state == "current",
                ).order_by(Artifact.created_at.desc())
            )
            parent_id = parent_artifact.id if parent_artifact else None
        artifact = self.artifacts.register(
            destination,
            kind="srt",
            role=stage,
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
        return {"artifact_id": artifact.id, "document_id": document_id, "revision_id": revision_id, "revision": revision_number}
