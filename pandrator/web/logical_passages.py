"""Revision-bound logical text, independent of subtitle display layout.

Passage windows locate source content. They are not target word timings. A
language-model merge is stored as one row; ancestral IDs are traceability only.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.logical_passages import build_source_passages
from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import compose_srt

from .models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
    TimedWord,
)

PASSAGE_VERSION = 1
PASSAGE_NODE_KIND = "logical_passage"


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def same_timing_language(left: str | None, right: str | None) -> bool:
    aliases = {"english": "en", "german": "de", "polish": "pl"}

    def key(value: str | None) -> str:
        normalized = str(value or "").strip().casefold().replace("_", "-")
        return aliases.get(normalized, normalized.split("-")[0])

    return key(left) not in {"", "auto"} and key(left) == key(right)


def load_timing_reference(
    session: Session,
    source: Artifact,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Read existing words on the unambiguous pinned, post-cut source path."""
    frontier = {source.id}
    seen: set[str] = set()
    while frontier and len(seen) < 32:
        if len(frontier | seen) > 32:
            return [], None
        next_frontier: set[str] = set()
        candidates: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
        cut_boundary = False
        for artifact_id in sorted(frontier):
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            artifact = session.get(Artifact, artifact_id)
            if (
                artifact is None
                or artifact.state == "deleted"
                or artifact.session_id != source.session_id
            ):
                continue
            metadata = artifact.metadata_json or {}
            revision_id = str(metadata.get("revision_id") or "")
            revision = (
                session.get(DocumentRevision, revision_id) if revision_id else None
            )
            document = session.get(Document, revision.document_id) if revision else None
            if (
                document is not None
                and document.session_id == source.session_id
                and document.stage == artifact.role
            ):
                words = list(
                    session.scalars(
                        select(TimedWord)
                        .where(
                            TimedWord.revision_id == revision_id,
                        )
                        .order_by(TimedWord.ordinal)
                    )
                )
                if words:
                    record = session.get(SessionRecord, source.session_id)
                    language = document.language or (
                        record.source_language if record else None
                    )
                    candidates[revision_id] = (
                        [
                            {
                                "id": word.id,
                                "ordinal": word.ordinal,
                                "segment_id": word.segment_id,
                                "text": word.text,
                                "start_ms": word.start_ms,
                                "end_ms": word.end_ms,
                                "speaker": word.speaker or "",
                                "confidence": word.confidence,
                            }
                            for word in words
                        ],
                        {"revision_id": revision_id, "language": language},
                    )
            if artifact.role == "media_edit_subtitles":
                cut_boundary = True
                continue
            parent_ids = set(
                session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == artifact.id,
                    )
                )
            )
            if metadata.get("source_artifact_id"):
                parent_id = str(metadata["source_artifact_id"])
                parent = session.get(Artifact, parent_id)
                expected_revision = metadata.get("source_revision_id")
                if expected_revision and (
                    parent is None
                    or (parent.metadata_json or {}).get("revision_id")
                    != expected_revision
                ):
                    return [], None
                parent_ids.add(parent_id)
            next_frontier.update(parent_ids - seen)
        if candidates:
            return (
                next(iter(candidates.values())) if len(candidates) == 1 else ([], None)
            )
        if cut_boundary:
            return [], None
        frontier = next_frontier
    return [], None


def _valid_rows(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    ids: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            return False
        row_id, start, end = row.get("id"), row.get("start_ms"), row.get("end_ms")
        if not isinstance(row_id, str) or not row_id or row_id in ids:
            return False
        ids.add(row_id)
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 0
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end <= start
        ):
            return False
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            return False
        if not isinstance(row.get("speaker", ""), str):
            return False
    return True


def stored_passages(artifact: Artifact) -> list[dict[str, Any]] | None:
    """Return only rows still bound to this exact display artifact/revision."""
    metadata = artifact.metadata_json or {}
    packet = metadata.get("logical_passages")
    if (
        artifact.state == "deleted"
        or not isinstance(packet, dict)
        or isinstance(packet.get("schema_version"), bool)
        or packet.get("schema_version") != PASSAGE_VERSION
        or not metadata.get("revision_id")
        or packet.get("display_revision_id") != metadata.get("revision_id")
        or not artifact.content_hash
        or packet.get("display_content_hash") != artifact.content_hash
        or not _valid_rows(packet.get("items"))
    ):
        return None
    return deepcopy(packet["items"])


def passage_review_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep unresolved review evidence when passages are grouped or projected."""
    return {
        "review_state": "uncertain"
        if any(row.get("review_state") == "uncertain" for row in rows)
        else "clear",
        "review_note": " ".join(
            dict.fromkeys(
                str(row["review_note"]) for row in rows if row.get("review_note")
            )
        ),
        **{
            key: list(
                dict.fromkeys(value for row in rows for value in row.get(key) or [])
            )
            for key in ("evidence_ids", "uncertain_source_cue_ids")
        },
    }


def source_passages(
    session: Session,
    artifact: Artifact,
    *,
    segments: list[Segment] | None = None,
) -> list[dict[str, Any]]:
    """Prefer preserved passages; otherwise use existing source-word evidence."""
    revision_id = str((artifact.metadata_json or {}).get("revision_id") or "")
    revision = session.get(DocumentRevision, revision_id) if revision_id else None
    document = session.get(Document, revision.document_id) if revision else None
    if (
        artifact.state == "deleted"
        or document is None
        or document.session_id != artifact.session_id
        or document.stage != artifact.role
    ):
        return []
    if segments is None:
        segments = list(
            session.scalars(
                select(Segment)
                .where(
                    Segment.revision_id == revision_id,
                )
                .order_by(Segment.ordinal)
            )
        )
    passages = stored_passages(artifact)
    if passages is None:
        cues = [
            {
                "id": item.id,
                "ordinal": item.ordinal,
                "text": item.text,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "speaker": item.speaker or "",
            }
            for item in segments
            if item.start_ms is not None
            and item.end_ms is not None
            and item.end_ms > item.start_ms
            and item.text.strip()
        ]
        words, reference = load_timing_reference(session, artifact)
        record = session.get(SessionRecord, artifact.session_id)
        language = document.language or (record.source_language if record else None)
        if not same_timing_language(language, (reference or {}).get("language")):
            words = []
        passages = build_source_passages(cues, words, language_code=language or "")
    for row in passages:
        # Evidence tools still address real display cues in the selected artifact.
        # These are explicitly separate from model-visible passage ordinals.
        overlapping = [
            item
            for item in segments
            if item.start_ms is not None
            and item.end_ms is not None
            and min(row["end_ms"], item.end_ms) > max(row["start_ms"], item.start_ms)
        ]
        owned = [item for item in segments if item.id in row.get("source_cue_ids", [])]
        # New source candidates know their exact cue ownership, including
        # overlapping speech. Preserved downstream passages use compatible
        # display intervals because display reflow has different cue IDs.
        overlapping = owned or [
            item
            for item in overlapping
            if not item.speaker
            or not row.get("speaker")
            or item.speaker.casefold() == str(row["speaker"]).casefold()
        ]
        row["evidence_cue_ids"] = [item.ordinal + 1 for item in overlapping]
        if overlapping and not row.get("review_state"):
            row.update(
                passage_review_metadata(
                    [item.metadata_json or {} for item in overlapping]
                )
            )
    return passages


def passage_srt(rows: list[dict[str, Any]]) -> str:
    return compose_srt(
        [
            SubtitleSegment(
                index=index + 1,
                start_ms=row["start_ms"],
                end_ms=row["end_ms"],
                text=row["text"],
                speaker=str(row.get("speaker") or ""),
            )
            for index, row in enumerate(rows)
        ]
    )


def map_output_passages(
    output: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace each model-owned group with one row, retaining ancestry only."""
    by_index = {index + 1: row for index, row in enumerate(inputs)}
    mapped: list[dict[str, Any]] = []
    for raw in output:
        text = " ".join(str(raw.get("text") or "").split())
        if not text or text.upper() == "[REMOVE]":
            continue
        ids = raw.get("source_cue_ids") or []
        if (
            not isinstance(ids, list)
            or not ids
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in by_index
                for value in ids
            )
        ):
            raise ValueError("Logical output has no valid source passage references.")
        sources = [by_index[value] for value in ids]
        start = min(row["start_ms"] for row in sources)
        end = max(row["end_ms"] for row in sources)
        if raw.get("start_ms") != start or raw.get("end_ms") != end:
            raise ValueError("Logical output must retain its combined source window.")
        mapped.append(
            {
                **passage_review_metadata([*sources, raw]),
                "id": f"p{len(mapped) + 1:06d}",
                "text": text,
                "start_ms": start,
                "end_ms": end,
                "speaker": str(raw.get("speaker") or ""),
                "source_passage_ids": [row["id"] for row in sources],
                "source_cue_ids": list(
                    dict.fromkeys(
                        value
                        for row in sources
                        for value in row.get("source_cue_ids", [])
                    )
                ),
                "timing_basis": "source_passage_window",
            }
        )
    if not _valid_rows(mapped):
        raise ValueError("A language stage must retain at least one valid passage.")
    return mapped


def attach_passages(
    artifact: Artifact,
    rows: list[dict[str, Any]],
    *,
    source: Artifact,
) -> None:
    if not _valid_rows(rows):
        raise ValueError("Cannot store invalid logical passages.")
    metadata = dict(artifact.metadata_json or {})
    if not metadata.get("revision_id") or not artifact.content_hash:
        raise ValueError("Logical passages require a materialized display artifact.")
    metadata["logical_passages"] = {
        "schema_version": PASSAGE_VERSION,
        "display_revision_id": metadata["revision_id"],
        "display_content_hash": artifact.content_hash,
        "source_artifact_id": source.id,
        "source_revision_id": (source.metadata_json or {}).get("revision_id"),
        "items": deepcopy(rows),
    }
    artifact.metadata_json = metadata


def materialize_speech_source(
    session: Session,
    artifact: Artifact,
) -> tuple[list[dict[str, Any]], str] | None:
    """Pin actual passage Segment rows for unchanged assembly/repair consumers.

    Call within the caller's write transaction. This never changes the display
    document's active revision or the selected stage artifact.
    """
    rows = stored_passages(artifact)
    if rows is None:
        return None
    metadata = dict(artifact.metadata_json or {})
    display_revision = session.get(DocumentRevision, str(metadata["revision_id"]))
    document = (
        session.get(Document, display_revision.document_id)
        if display_revision
        else None
    )
    if (
        document is None
        or document.session_id != artifact.session_id
        or document.stage != artifact.role
    ):
        return None
    packet = dict(metadata["logical_passages"])
    digest = _hash(rows)
    cached_id = str(packet.get("speech_source_revision_id") or "")
    cached = session.get(DocumentRevision, cached_id) if cached_id else None
    if (
        cached is not None
        and cached.document_id == document.id
        and cached.id != document.active_revision_id
        and cached.content_hash == digest
    ):
        cached_rows = list(
            session.scalars(
                select(Segment)
                .where(
                    Segment.revision_id == cached.id,
                )
                .order_by(Segment.ordinal)
            )
        )
        if len(cached_rows) == len(rows) and all(
            item.node_kind == PASSAGE_NODE_KIND
            and item.text == row["text"]
            and item.start_ms == row["start_ms"]
            and item.end_ms == row["end_ms"]
            and str(item.speaker or "") == str(row.get("speaker") or "")
            for item, row in zip(cached_rows, rows, strict=True)
        ):
            return rows, cached.id
    revision_number = (
        int(
            session.scalar(
                select(func.max(DocumentRevision.revision_number)).where(
                    DocumentRevision.document_id == document.id,
                )
            )
            or 0
        )
        + 1
    )
    revision = DocumentRevision(
        document_id=document.id,
        parent_revision_id=display_revision.id,
        revision_number=revision_number,
        content_hash=digest,
    )
    session.add(revision)
    session.flush()
    display_rows = list(
        session.scalars(
            select(Segment)
            .where(
                Segment.revision_id == display_revision.id,
            )
            .order_by(Segment.ordinal)
        )
    )
    for index, row in enumerate(rows):
        item = Segment(
            revision_id=revision.id,
            ordinal=index,
            node_kind=PASSAGE_NODE_KIND,
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            text=row["text"],
            speaker=str(row.get("speaker") or "") or None,
            metadata_json={"logical_passage": deepcopy(row)},
        )
        session.add(item)
        session.flush()
        for sequence, display in enumerate(
            value
            for value in display_rows
            if value.start_ms is not None
            and value.end_ms is not None
            and min(row["end_ms"], value.end_ms) > max(row["start_ms"], value.start_ms)
        ):
            session.add(
                SegmentLineage(
                    parent_segment_id=display.id,
                    child_segment_id=item.id,
                    relation="temporal_overlap",
                    sequence=sequence,
                )
            )
    packet["speech_source_revision_id"] = revision.id
    metadata["logical_passages"] = packet
    artifact.metadata_json = metadata
    return rows, revision.id
