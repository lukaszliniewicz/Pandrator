"""Revision-safe subtitle comparison and reviewed-artifact persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from sqlalchemy import func, select

from pandrator.logic.dubbing.correction_splits import split_boundaries
from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import compose_srt, split_speaker_label

from .artifacts import ArtifactService
from .database import Database
from .logical_passages import (
    attach_passages,
    load_timing_reference,
    passage_review_metadata,
    same_timing_language,
    source_passages,
    stored_passages,
)
from .models import (
    Artifact,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionRecord,
    SubtitleEvidence,
)
from .subtitle_media import resolve_subtitle_media

STAGE_ORDER = ("transcription", "correction", "translation", "tts_optimization")
ARTIFACT_ROLE_TO_STAGE = {
    "transcription": "transcription",
    "correction": "correction",
    "translation": "translation",
    "tts_optimized": "tts_optimization",
}
MAX_REVIEW_ARTIFACTS = 4


class ReviewedSubtitleSegment(TypedDict):
    id: str | None
    turn_id: str | None
    source_passage_ids: list[str]
    starts_new_turn: bool
    origin_segment_id: str | None
    split_boundary_id: str | None
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None
    review_state: str
    review_note: str
    evidence_ids: list[str]
    uncertain_source_cue_ids: list[int]
    _source_word_ids: NotRequired[list[str]]


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
            "review_state": item.get("review_state") or "clear",
            "review_note": item.get("review_note") or "",
            "evidence_ids": list(item.get("evidence_ids") or []),
            "uncertain_source_cue_ids": list(
                item.get("uncertain_source_cue_ids") or []
            ),
            "turn_id": item.get("turn_id"),
            "source_passage_ids": list(item.get("source_passage_ids") or []),
            "starts_new_turn": bool(item.get("starts_new_turn")),
            "origin_segment_id": item.get("origin_segment_id"),
            "split_boundary_id": item.get("split_boundary_id"),
        }
        for item in segments
    ]
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _overlaps(start: int, end: int, row: Mapping[str, Any]) -> bool:
    return min(end, int(row["end_ms"])) > max(start, int(row["start_ms"]))


def _review_passage_rows(
    source_rows: list[dict[str, Any]],
    source_segments: list[Segment],
    reviewed: list[ReviewedSubtitleSegment],
    *,
    source_hash: str,
) -> list[dict[str, Any]]:
    """Project reviewed display cues through explicit source identity and windows."""
    by_segment = {item.id: item for item in source_segments}
    by_passage = {row["id"]: row for row in source_rows}
    output: list[dict[str, Any]] = []
    used: set[str] = set()
    carried: dict[str, dict[str, Any]] = {}
    carried_reviews: dict[str, list[ReviewedSubtitleSegment]] = {}
    fragments: dict[str, list[tuple[Segment, ReviewedSubtitleSegment]]] = {}
    track_turns = any(row.get("turn_id") for row in source_rows) or any(
        cue.get("starts_new_turn") for cue in reviewed
    )
    active_turn = (
        "turn-" + hashlib.sha256(source_hash.encode()).hexdigest()[:24]
        if track_turns else ""
    )
    previous_source_turn = ""
    for ordinal, cue in enumerate(reviewed):
        segment_id = cue.get("origin_segment_id") or cue.get("id")
        segment = by_segment.get(segment_id) if segment_id else None
        if segment_id and segment is None:
            raise ValueError("Reviewed segment identity does not belong to the selected source.")
        if segment is not None and (
            segment.start_ms is None
            or segment.end_ms is None
            or not _overlaps(cue["start_ms"], cue["end_ms"], {
                "start_ms": segment.start_ms, "end_ms": segment.end_ms,
            })
        ):
            raise ValueError("Reviewed segment identity does not overlap its selected source.")
        if segment is None:
            exact = [
                item for item in source_segments
                if item.start_ms == cue["start_ms"]
                and item.end_ms == cue["end_ms"]
            ]
            if len(exact) > 1 and cue["speaker"]:
                exact = [
                    item for item in exact
                    if _speaker_and_text(item)[0].casefold() == cue["speaker"].casefold()
                ]
            if len(exact) == 1:
                segment = exact[0]
        candidates = [
            row for row in source_rows
            if _overlaps(cue["start_ms"], cue["end_ms"], row)
        ]
        owned = (
            [row for row in candidates if segment.id in row.get("source_cue_ids", [])]
            if segment is not None else []
        )
        if not owned and segment is not None:
            owned = [
                row for row in candidates
                if row["start_ms"] == segment.start_ms
                and row["end_ms"] == segment.end_ms
            ]
        if not owned:
            owned = candidates
        supplied = cue.get("source_passage_ids") or []
        if supplied:
            if len(set(supplied)) != len(supplied) or any(value not in by_passage for value in supplied):
                raise ValueError("Logical source references do not belong to the selected source.")
            selected = [by_passage[value] for value in supplied]
            if any(row not in owned for row in selected) or any(row not in selected for row in owned):
                raise ValueError("Logical source references conflate or omit selected source passages.")
            owned = selected
        if not owned:
            raise ValueError("Reviewed cue has no selected logical source passage.")
        exact_inherited = (
            segment is not None
            and segment.start_ms == cue["start_ms"]
            and segment.end_ms == cue["end_ms"]
        )
        supplied_turn = str(cue.get("turn_id") or "")
        owned_turns = {str(row.get("turn_id") or "") for row in owned}
        if supplied_turn and (len(owned_turns) != 1 or supplied_turn not in owned_turns):
            raise ValueError("Reviewed turn_id does not belong to the selected source.")
        display_reflow = exact_inherited and (
            len(owned) > 1
            or owned[0]["start_ms"] != cue["start_ms"]
            or owned[0]["end_ms"] != cue["end_ms"]
        )
        if display_reflow and segment is not None:
            if cue.get("starts_new_turn"):
                raise ValueError("A display reflow cannot start a turn without a separate logical passage.")
            if cue.get("origin_segment_id") or cue.get("split_boundary_id"):
                raise ValueError("A display reflow cannot be split without one logical source passage.")
            if cue.get("id") != segment.id or supplied != [row["id"] for row in owned]:
                raise ValueError("Display fragment requires its exact source segment and logical references.")
            if len(owned) > 1 and _speaker_and_text(segment)[1] != cue["text"]:
                raise ValueError("Display cue contains multiple logical passages; edit their source passages separately.")
            # Keep each canonical row once even when display cues split or
            # combine it. Fragment text is reconstructed only after complete
            # source ownership has been verified below.
            for row in owned:
                row_id = row["id"]
                if row_id not in carried:
                    carried[row_id] = dict(row)
                    output.append(carried[row_id])
                    used.add(row_id)
                carried_reviews.setdefault(row_id, []).append(cue)
                if len(owned) == 1:
                    fragments.setdefault(row_id, []).append((segment, cue))
            continue
        turns = {
            str(row.get("turn_id") or "")
            for row in (owned if exact_inherited or cue.get("origin_segment_id") else candidates)
        }
        if len(turns) > 1:
            raise ValueError("Cannot merge across a preserved utterance turn boundary.")
        inherited_turn = str(owned[0].get("turn_id") or "")
        if inherited_turn and inherited_turn != previous_source_turn:
            active_turn = inherited_turn
        previous_source_turn = inherited_turn
        if cue.get("starts_new_turn"):
            active_turn = "turn-" + hashlib.sha256(
                json.dumps([source_hash, [row["id"] for row in owned], ordinal]).encode()
            ).hexdigest()[:24]
        turn_id = active_turn
        for row in owned:
            if row["id"] in used and not cue.get("origin_segment_id"):
                raise ValueError("One logical source passage is assigned to multiple reviewed cues.")
        if len(owned) > 1 and cue.get("origin_segment_id"):
            raise ValueError("A split must originate from one logical source passage.")
        if len(owned) > 1 and len(turns) > 1:
            raise ValueError("Cannot merge across a preserved utterance turn boundary.")
        if len(owned) > 1 and segment is not None and len(owned) != len(candidates):
            raise ValueError("Reviewed merge omits overlapping logical source passages.")
        if len(owned) > 1 and segment is not None and _speaker_and_text(segment)[1] != cue["text"]:
            # Multiple logical rows in one display cue cannot be edited as a
            # single text value without an explicit passage-level edit.
            raise ValueError("Display cue contains multiple logical passages; edit their source passages separately.")
        source_ids = [row["id"] for row in owned]
        source_words = list(dict.fromkeys(
            word_id for row in owned for word_id in row.get("source_word_ids") or []
        ))
        evidence_ids = list(dict.fromkeys(
            value for row in owned for value in row.get("evidence_ids") or []
        ))
        evidence_ids = list(dict.fromkeys([*evidence_ids, *cue["evidence_ids"]]))
        uncertain_ids = list(dict.fromkeys(
            value for row in owned for value in row.get("uncertain_source_cue_ids") or []
        ))
        uncertain_ids = list(dict.fromkeys([*uncertain_ids, *cue["uncertain_source_cue_ids"]]))
        source_cue_ids = list(dict.fromkeys(
            value for row in owned for value in row.get("source_cue_ids") or []
        ))
        mapped = {
            "id": f"p{len(output) + 1:06d}",
            "text": cue["text"],
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "speaker": cue["speaker"] or "",
            **({"turn_id": turn_id} if turn_id else {}),
            "source_passage_ids": source_ids,
            "source_cue_ids": source_cue_ids,
            "timing_basis": "source_word_split" if cue.get("split_boundary_id") else "source_passage_window",
            "review_state": cue["review_state"],
            "review_note": cue["review_note"],
            "evidence_ids": evidence_ids,
            "uncertain_source_cue_ids": uncertain_ids,
            **({"source_word_ids": source_words} if source_words else {}),
        }
        output.append(mapped)
        used.update(source_ids)
    for row_id, stored in carried.items():
        source = by_passage[row_id]
        reviews: list[dict[str, Any]] = [
            source,
            *(dict(cue) for cue in carried_reviews[row_id]),
        ]
        stored.update(passage_review_metadata(reviews))
        if row_id not in fragments:
            continue
        direct = [
            segment for segment in source_segments
            if segment.id in source.get("source_cue_ids", [])
        ]
        expected = direct or [
            segment for segment in source_segments
            if segment.start_ms is not None
            and segment.end_ms is not None
            and _overlaps(segment.start_ms, segment.end_ms, source)
        ]
        actual = fragments[row_id]
        if (
            len(actual) != len(expected)
            or {segment.id for segment, _cue in actual} != {segment.id for segment in expected}
        ):
            raise ValueError("Display fragment review must include every inherited source segment exactly once.")
        ordered = sorted(actual, key=lambda pair: (pair[0].start_ms, pair[0].end_ms, pair[0].ordinal))
        if any(_speaker_and_text(segment)[1] != cue["text"] for segment, cue in ordered):
            stored["text"] = " ".join(cue["text"] for _segment, cue in ordered)
    return output


class SubtitleReviewService:
    def __init__(
        self, database: Database, artifacts: ArtifactService, session_dir_resolver
    ):
        self.database = database
        self.artifacts = artifacts
        self.session_dir_resolver = session_dir_resolver

    def inspect_review_split_boundaries(
        self,
        session_id: str,
        source_artifact_id: str,
        segment_id: str,
        expected_revision: int,
        offset: int = 0,
        limit: int = 30,
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid split boundary page.")
        with self.database.session() as session:
            source = session.get(Artifact, source_artifact_id)
            if (
                source is None
                or source.session_id != session_id
                or source.role not in ARTIFACT_ROLE_TO_STAGE
                or source.state == "deleted"
            ):
                raise KeyError(source_artifact_id)
            revision_id = str((source.metadata_json or {}).get("revision_id") or "")
            revision = session.get(DocumentRevision, revision_id)
            if revision is None or revision.revision_number != expected_revision:
                raise RuntimeError("Subtitle source revision changed.")
            document = session.get(Document, revision.document_id)
            if (
                document is None
                or document.session_id != session_id
                or document.stage != ARTIFACT_ROLE_TO_STAGE[source.role]
            ):
                raise KeyError(source_artifact_id)
            segment = session.get(Segment, segment_id)
            if segment is None or segment.revision_id != revision_id:
                raise KeyError(segment_id)
            result: dict[str, Any] = {
                "source_artifact_id": source.id,
                "source_revision_id": revision_id,
                "source_content_hash": source.content_hash,
                "segment_id": segment_id,
                "status": "unavailable",
                "reason": None,
                "total": 0,
                "offset": offset,
                "boundaries": [],
                "next_offset": None,
            }
            if not self._source_hash_matches(source):
                result["reason"] = "The selected source content hash is unavailable or changed."
                return result
            rows = source_passages(session, source)
            owned = [row for row in rows if segment.id in row.get("source_cue_ids", [])]
            if not owned:
                owned = [
                    row for row in rows
                    if segment.start_ms is not None
                    and segment.end_ms is not None
                    and row["start_ms"] == segment.start_ms
                    and row["end_ms"] == segment.end_ms
                ]
            if len(owned) != 1:
                result["reason"] = "The display cue does not identify one logical source passage."
                return result
            words, reference = load_timing_reference(session, source)
            record = session.get(SessionRecord, session_id)
            language = document.language or (record.source_language if record else None)
            if not same_timing_language(language, (reference or {}).get("language")):
                result["reason"] = "Matching source-word timing language is unavailable."
                return result
            boundaries = split_boundaries(
                owned[0], words, str((reference or {}).get("revision_id") or revision_id)
            )
            result.update(
                status="available" if boundaries else "unavailable",
                reason=None if boundaries else "Complete matching source-word timing is unavailable; no split timings are guessed.",
                total=len(boundaries),
                boundaries=boundaries[offset : offset + limit],
                next_offset=offset + limit if offset + limit < len(boundaries) else None,
                source_passage_id=owned[0]["id"],
                source_window={"start_ms": owned[0]["start_ms"], "end_ms": owned[0]["end_ms"]},
            )
            return result

    def _source_hash_matches(self, source: Artifact) -> bool:
        if not source.content_hash:
            return False
        try:
            path = self.artifacts.paths.managed_path(source.relative_path)
            return hashlib.sha256(path.read_bytes()).hexdigest() == source.content_hash
        except (OSError, ValueError):
            return False

    @staticmethod
    def _payload(segment: Segment) -> dict[str, Any]:
        speaker, text = _speaker_and_text(segment)
        metadata = dict(segment.metadata_json or {})
        return {
            "id": segment.id,
            "turn_id": metadata.get("turn_id"),
            "source_passage_ids": list(metadata.get("source_passage_ids") or []),
            "ordinal": segment.ordinal,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": text,
            "speaker": speaker or None,
            "review_state": str(metadata.get("review_state") or "clear"),
            "review_note": str(metadata.get("review_note") or ""),
            "evidence_ids": [
                str(value)
                for value in metadata.get("evidence_ids") or []
                if str(value).strip()
            ][:20],
            "uncertain_source_cue_ids": list(
                metadata.get("uncertain_source_cue_ids") or []
            )[:20],
        }

    @classmethod
    def _payload_with_passages(
        cls, segment: Segment, passages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = cls._payload(segment)
        owned = [
            row for row in passages
            if segment.id in row.get("source_cue_ids", [])
        ]
        if not owned:
            owned = [
                row for row in passages
                if segment.start_ms is not None
                and segment.end_ms is not None
                and _overlaps(segment.start_ms, segment.end_ms, row)
            ]
        payload["source_passage_ids"] = [row["id"] for row in owned]
        turns = {str(row.get("turn_id") or "") for row in owned}
        payload["turn_id"] = next(iter(turns)) if len(turns) == 1 else None
        return payload

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
                passages = stored_passages(artifact) or []
                source_media_artifact_id: str | None = None
                source_media_error: str | None = None
                source_media_mime_type: str | None = None
                source_media_kind: str | None = None
                try:
                    media_artifact = resolve_subtitle_media(
                        session, session_id, artifact
                    )
                    source_media_artifact_id = media_artifact.id
                    source_media_mime_type = media_artifact.mime_type
                    source_media_kind = media_artifact.kind
                except ValueError as error:
                    source_media_error = str(error)
                columns.append(
                    {
                        "artifact_id": artifact_id,
                        "source_content_hash": artifact.content_hash,
                        "role": artifact.role,
                        "stage": document.stage,
                        "document_id": document.id,
                        "revision_id": revision.id,
                        "revision": revision.revision_number,
                        "reviewed": revision.reviewed,
                        "language": document.language,
                        "source_media_artifact_id": source_media_artifact_id,
                        "source_media_mime_type": source_media_mime_type,
                        "source_media_kind": source_media_kind,
                        "source_media_error": source_media_error,
                        "segments": [
                            self._payload_with_passages(item, passages) for item in records
                        ],
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
        expected_source_hash: str | None = None,
        db_session=None,
        published_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        if db_session is None:
            owned_paths: list[Path] = []
            try:
                with self.database.session() as session:
                    return self.save_review(
                        session_id,
                        stage,
                        expected_revision,
                        values,
                        source_artifact_id=source_artifact_id,
                        expected_source_hash=expected_source_hash,
                        db_session=session,
                        published_paths=owned_paths,
                    )
            except Exception:
                for path in owned_paths:
                    path.unlink(missing_ok=True)
                raise
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
                    "id": str(item.get("id") or "").strip() or None,
                    "turn_id": str(item.get("turn_id") or "").strip() or None,
                    "source_passage_ids": list(item.get("source_passage_ids") or []),
                    "starts_new_turn": bool(item.get("starts_new_turn")),
                    "origin_segment_id": str(item.get("origin_segment_id") or "").strip() or None,
                    "split_boundary_id": str(item.get("split_boundary_id") or "").strip() or None,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text,
                    "speaker": str(item.get("speaker") or legacy_speaker or "").strip()
                    or None,
                    "review_state": (
                        "uncertain"
                        if str(item.get("review_state") or "").strip().lower()
                        == "uncertain"
                        else "clear"
                    ),
                    "review_note": " ".join(
                        str(item.get("review_note") or "").split()
                    ).strip()[:4_000],
                    "evidence_ids": list(
                        dict.fromkeys(
                            str(value).strip()
                            for value in item.get("evidence_ids") or []
                            if str(value).strip()
                        )
                    )[:20],
                    "uncertain_source_cue_ids": list(
                        dict.fromkeys(
                            int(value)
                            for value in item.get("uncertain_source_cue_ids") or []
                        )
                    )[:20],
                }
            )
        if not normalized:
            raise ValueError("A reviewed subtitle document cannot be empty.")

        with (
            self.database.session()
            if db_session is None
            else _SessionContext(db_session)
        ) as session:
            evidence_ids = {
                evidence_id
                for item in normalized
                for evidence_id in item["evidence_ids"]
            }
            evidence_by_id = {
                item.id: item
                for item in session.scalars(
                    select(SubtitleEvidence).where(
                        SubtitleEvidence.id.in_(evidence_ids)
                    )
                ).all()
            }
            for item in normalized:
                for evidence_id in item["evidence_ids"]:
                    evidence = evidence_by_id.get(evidence_id)
                    if evidence is None or evidence.session_id != session_id:
                        raise ValueError(
                            "Subtitle evidence must belong to this session."
                        )
                    if evidence.status in {"queued", "running"}:
                        raise ValueError(
                            "Subtitle evidence cannot be attached while it is running."
                        )
                    if min(item["end_ms"], evidence.end_ms) <= max(
                        item["start_ms"], evidence.start_ms
                    ):
                        raise ValueError(
                            "Subtitle evidence must overlap the reviewed cue timing."
                        )
            source_artifact = (
                session.get(Artifact, source_artifact_id)
                if source_artifact_id
                else None
            )
            if source_artifact_id and (
                source_artifact is None
                or source_artifact.session_id != session_id
                or ARTIFACT_ROLE_TO_STAGE.get(source_artifact.role) != stage
                or source_artifact.state == "deleted"
            ):
                raise KeyError(source_artifact_id)
            if source_artifact is not None and (
                (expected_source_hash is not None and source_artifact.content_hash != expected_source_hash)
                or not self._source_hash_matches(source_artifact)
            ):
                raise RuntimeError("Subtitle source content hash changed.")
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
            if source_artifact is None and previous is not None:
                source_artifact = session.scalar(
                    select(Artifact)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role == ("tts_optimized" if stage == "tts_optimization" else stage),
                        Artifact.state != "deleted",
                    )
                    .order_by(Artifact.created_at.desc())
                )
                if source_artifact is not None and (
                    (source_artifact.metadata_json or {}).get("revision_id") != previous.id
                    or not self._source_hash_matches(source_artifact)
                ):
                    source_artifact = None
            if expected_source_hash is not None and (
                source_artifact is None
                or source_artifact.content_hash != expected_source_hash
            ):
                raise RuntimeError("Subtitle source content hash changed.")
            if source_artifact is not None:
                source_rows = source_passages(session, source_artifact, segments=previous_segments)
                if not source_rows:
                    raise ValueError("The selected source has no valid logical passages.")
                if (source_artifact.metadata_json or {}).get("revision_id") != (previous.id if previous else None):
                    raise RuntimeError("Subtitle source revision changed.")
                for origin_id in {item["origin_segment_id"] for item in normalized if item["origin_segment_id"]}:
                    children = [item for item in normalized if item["origin_segment_id"] == origin_id]
                    if len(children) != 2 or not children[0]["split_boundary_id"] or children[0]["split_boundary_id"] != children[1]["split_boundary_id"]:
                        raise ValueError("A review split requires two children with one shared verified boundary.")
                    if any(item["id"] is not None and item["id"] != origin_id for item in children):
                        raise ValueError("Split child id must be absent or match origin_segment_id.")
                    inspection = self.inspect_review_split_boundaries(
                        session_id, source_artifact.id, origin_id, expected_revision,
                        offset=0, limit=100,
                    )
                    boundary = next((row for row in inspection["boundaries"] if row["id"] == children[0]["split_boundary_id"]), None)
                    while boundary is None and inspection["next_offset"] is not None:
                        inspection = self.inspect_review_split_boundaries(
                            session_id, source_artifact.id, origin_id,
                            expected_revision, offset=inspection["next_offset"], limit=100,
                        )
                        boundary = next((row for row in inspection["boundaries"] if row["id"] == children[0]["split_boundary_id"]), None)
                    if boundary is None:
                        raise ValueError("Split boundary is unavailable or stale; inspect source anchors.")
                    origin = next(item for item in previous_segments if item.id == origin_id)
                    if (children[0]["start_ms"], children[0]["end_ms"]) != (origin.start_ms, boundary["left_end_ms"]) or (children[1]["start_ms"], children[1]["end_ms"]) != (boundary["right_start_ms"], origin.end_ms):
                        raise ValueError("Review split timing must match verified source-word windows.")
                    source_row = next(row for row in source_rows if row["id"] == inspection["source_passage_id"])
                    if any(
                        item["source_passage_ids"]
                        and item["source_passage_ids"] != [source_row["id"]]
                        for item in children
                    ):
                        raise ValueError("Split child logical source references must match the verified origin passage.")
                    split_at = boundary["after_word"]
                    children[0]["_source_word_ids"] = list(source_row.get("source_word_ids") or [])[:split_at]
                    children[1]["_source_word_ids"] = list(source_row.get("source_word_ids") or [])[split_at:]
                if any(item["split_boundary_id"] and not item["origin_segment_id"] for item in normalized):
                    raise ValueError("Split boundary requires origin_segment_id.")
                logical_rows = _review_passage_rows(
                    source_rows, previous_segments, normalized,
                    source_hash=source_artifact.content_hash or "",
                )
                for item in normalized:
                    if "_source_word_ids" not in item:
                        continue
                    matching = [
                        row for row in logical_rows
                        if row["start_ms"] == item["start_ms"]
                        and row["end_ms"] == item["end_ms"]
                    ]
                    if len(matching) != 1:
                        raise ValueError("Split child has ambiguous logical passage timing.")
                    matching[0]["source_word_ids"] = item["_source_word_ids"]
            else:
                if any(item["id"] or item["turn_id"] or item["source_passage_ids"] or item["origin_segment_id"] or item["split_boundary_id"] for item in normalized):
                    raise ValueError("Reviewed identity requires an exact selected source artifact.")
                logical_rows = []
                active_turn = ""
                for index, item in enumerate(normalized, start=1):
                    if item["starts_new_turn"]:
                        active_turn = "turn-" + hashlib.sha256(
                            json.dumps([session_id, stage, index, item["start_ms"]]).encode()
                        ).hexdigest()[:24]
                    logical_rows.append({
                        "id": f"p{index:06d}",
                        "text": item["text"],
                        "start_ms": item["start_ms"],
                        "end_ms": item["end_ms"],
                        "speaker": item["speaker"] or "",
                        **({"turn_id": active_turn} if active_turn else {}),
                        "review_state": item["review_state"],
                        "review_note": item["review_note"],
                        "evidence_ids": item["evidence_ids"],
                        "uncertain_source_cue_ids": item["uncertain_source_cue_ids"],
                    })
            for index, reviewed in enumerate(normalized, start=1):
                # Genuine crosstalk can make an existing cue overlap another speaker.
                # Preserve that inherited overlap when the reviewed cue keeps the same
                # timing, while still rejecting newly created cues whose timing spans
                # multiple speakers. Exact-timing cues may also correct speaker labels.
                exact_speakers: dict[str, str] = {}
                for previous_segment in previous_segments:
                    if (
                        previous_segment.start_ms != reviewed["start_ms"]
                        or previous_segment.end_ms != reviewed["end_ms"]
                    ):
                        continue
                    speaker, _text = _speaker_and_text(previous_segment)
                    if speaker:
                        exact_speakers.setdefault(speaker.casefold(), speaker)

                reviewed_speaker = str(reviewed["speaker"] or "").strip()
                exact_timing_match = len(exact_speakers) == 1
                if not reviewed_speaker and exact_timing_match:
                    reviewed["speaker"] = next(iter(exact_speakers.values()))

                # Exact inherited timing proves this is the same cue, even if the
                # editor is correcting its speaker label. Crosstalk validation is
                # only needed when timing is widened, merged, or otherwise changed.
                if exact_timing_match:
                    continue

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
                linked = [
                    row for row in logical_rows
                    if _overlaps(reviewed["start_ms"], reviewed["end_ms"], row)
                ]
                linked_turns = {str(row.get("turn_id") or "") for row in linked}
                child = Segment(
                    revision_id=revision.id,
                    ordinal=ordinal,
                    start_ms=reviewed["start_ms"],
                    end_ms=reviewed["end_ms"],
                    text=reviewed["text"],
                    speaker=reviewed["speaker"],
                    metadata_json={
                        "review_state": reviewed["review_state"],
                        "review_note": reviewed["review_note"],
                        "evidence_ids": reviewed["evidence_ids"],
                        "uncertain_source_cue_ids": reviewed[
                            "uncertain_source_cue_ids"
                        ],
                        "source_passage_ids": list(dict.fromkeys(
                            source_id
                            for row in linked
                            for source_id in row.get("source_passage_ids") or [row["id"]]
                        )),
                        **({"turn_id": next(iter(linked_turns))} if len(linked_turns) == 1 and next(iter(linked_turns)) else {}),
                    },
                )
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
                        "uncertain_segment_count": sum(
                            1
                            for item in normalized
                            if item["review_state"] == "uncertain"
                        ),
                    },
                )
                source_packet = (
                    ((source_artifact.metadata_json or {}).get("logical_passages") or {})
                    if source_artifact is not None else {}
                )
                attach_passages(
                    artifact,
                    logical_rows,
                    source=source_artifact or artifact,
                    source_passage_settings=source_packet.get("source_passage_settings"),
                    source_passage_settings_revision=source_packet.get("source_passage_settings_revision"),
                    policy_version=source_packet.get("policy_version"),
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
        expected_source_hash: str | None = None,
        published_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Persist a review without committing the caller-owned transaction."""
        return self.save_review(
            session_id,
            stage,
            expected_revision,
            values,
            source_artifact_id=source_artifact_id,
            expected_source_hash=expected_source_hash,
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

    def __exit__(self, _exc_type, _exc, _traceback):
        return False
