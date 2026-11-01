"""Revision-safe subtitle comparison and reviewed-artifact persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from uuid import uuid4

from sqlalchemy import func, select

from pandrator.logic.dubbing.correction_splits import split_boundaries
from pandrator.logic.dubbing.models import SubtitleSegment
from pandrator.logic.dubbing.srt_utils import compose_srt, split_speaker_label
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
    finalize_segments,
)

from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .artifacts import ArtifactService, PreparedArtifactRegistration, sha256_file
from .database import Database
from .document_roles import ARTIFACT_ROLE_TO_STAGE
from .export_subtitles import subtitle_export_diagnostics
from .logical_passages import (
    attach_passages,
    load_timing_reference,
    passage_review_metadata,
    passage_srt,
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
from .settings_policy import adapt_runtime_settings
from .source_resolution import PrimarySourceResolution
from .subtitle_media import resolve_subtitle_media
from .temporal_overlap import overlap_indexes
from .workspace_settings import WorkspaceSettingsService

STAGE_ORDER = ("transcription", "correction", "translation", "tts_optimization")
MAX_REVIEW_ARTIFACTS = 4
logger = logging.getLogger(__name__)


def cleanup_review_publications(paths: Sequence[Path]) -> None:
    """Best-effort removal must preserve the publication's primary outcome."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not remove subtitle publication %s", path, exc_info=True,
            )


@dataclass(frozen=True, slots=True)
class PreparedSubtitleReview:
    """Detached review data; preparation owns no database writes.

    Only the staged file belongs to this attempt. The publishing transaction's
    owner must remove newly published paths if its commit fails.
    """

    session_id: str
    stage: str
    document: dict[str, Any]
    previous: dict[str, Any] | None
    next_revision_number: int
    source: dict[str, Any] | None
    parent: dict[str, Any] | None
    source_stat: tuple[int, ...] | None
    evidence: dict[str, dict[str, Any]]
    settings_hash: str
    composition_hash: str | None
    revision_id: str
    content_hash: str
    children: list[dict[str, Any]]
    metadata: dict[str, Any]
    settings: dict[str, Any]
    staged_path: Path
    destination: Path
    registration: PreparedArtifactRegistration

    def cleanup(self) -> None:
        cleanup_review_publications([self.staged_path])


def _record_values(record, fields: Sequence[str]) -> dict[str, Any]:
    return {field: deepcopy(getattr(record, field)) for field in fields}


def _stat_witness(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


_SOURCE_FIELDS = (
    "id", "session_id", "project_id", "workspace_id", "kind", "role", "state",
    "relative_path", "content_hash", "size_bytes", "settings_hash", "metadata_json",
)
_REVISION_FIELDS = (
    "id", "document_id", "parent_revision_id", "revision_number", "content_hash",
    "reviewed", "settings_hash",
)
_EVIDENCE_FIELDS = ("id", "session_id", "status", "start_ms", "end_ms")

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
    _canonical_passage_id: NotRequired[str]


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


def _require_unchanged_combined_speaker(
    segment: Segment,
    cue: ReviewedSubtitleSegment,
    owned: list[dict[str, Any]],
) -> None:
    source_speaker = _speaker_and_text(segment)[0]
    reviewed_speaker = str(cue.get("speaker") or "").strip()
    if reviewed_speaker.casefold() != source_speaker.casefold():
        passages = ", ".join(str(row["id"]) for row in owned)
        raise ValueError(
            f"Display cue contains logical passages [{passages}]; align speakers or edit each passage separately."
        )


def _merge_prepared_canonical_rows(
    source_rows: list[dict[str, Any]],
    prepared_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply only passage-edit fields to the freshly guarded source ledger."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in prepared_rows:
        row_id = str(row.get("id") or "")
        if not row_id or row_id in by_id:
            raise ValueError("Canonical passage identities must be unique.")
        by_id[row_id] = row
    source_ids = [str(row["id"]) for row in source_rows]
    if set(by_id) != set(source_ids) or len(by_id) != len(source_ids):
        raise ValueError(
            "Canonical passage review must include every selected source passage exactly once."
        )
    output: list[dict[str, Any]] = []
    for source in source_rows:
        prepared = by_id[str(source["id"])]
        if prepared.get("_deleted") is True:
            continue
        row = deepcopy(source)
        for key in (
            "text",
            "speaker",
            "start_ms",
            "end_ms",
            "review_state",
            "review_note",
        ):
            if key in prepared:
                row[key] = prepared[key]
        turn_id = str(prepared.get("turn_id") or "").strip()
        if turn_id:
            row["turn_id"] = turn_id
        else:
            row.pop("turn_id", None)
        output.append(row)
    if not output:
        raise ValueError("A reviewed subtitle document cannot be empty.")
    return output


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
    exact_segments: dict[tuple[int | None, int | None], list[Segment]] = {}
    source_owners: dict[str, list[Segment]] = {}
    for item in source_segments:
        exact_segments.setdefault((item.start_ms, item.end_ms), []).append(item)
        source_owners.setdefault(item.id, []).append(item)
    candidate_indexes = overlap_indexes(
        [(row["start_ms"], row["end_ms"]) for row in source_rows],
        [(row["start_ms"], row["end_ms"]) for row in reviewed],
    )
    source_segment_indexes = overlap_indexes(
        [(item.start_ms or 0, item.end_ms or 0) for item in source_segments],
        [(row["start_ms"], row["end_ms"]) for row in source_rows],
    )
    source_indexes_by_id = {row["id"]: index for index, row in enumerate(source_rows)}
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
            exact = exact_segments.get((cue["start_ms"], cue["end_ms"]), [])
            if len(exact) > 1 and cue["speaker"]:
                exact = [
                    item for item in exact
                    if _speaker_and_text(item)[0].casefold() == cue["speaker"].casefold()
                ]
            if len(exact) == 1:
                segment = exact[0]
        candidates = [source_rows[index] for index in candidate_indexes[ordinal]]
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
            if {row["id"] for row in selected} != {row["id"] for row in owned}:
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
            if len(owned) > 1:
                _require_unchanged_combined_speaker(segment, cue, owned)
                if _speaker_and_text(segment)[1] != cue["text"]:
                    raise ValueError(
                        "Display cue contains multiple logical passages; edit their source passages separately."
                    )
            # Keep each canonical row once even when display cues split or
            # combine it. Fragment text is reconstructed only after complete
            # source ownership has been verified below.
            for row in owned:
                row_id = row["id"]
                if row_id not in carried:
                    carried[row_id] = dict(row)
                    carried[row_id]["source_passage_ids"] = [row_id]
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
        if len(owned) > 1 and segment is not None:
            _require_unchanged_combined_speaker(segment, cue, owned)
            if _speaker_and_text(segment)[1] != cue["text"]:
                # Multiple logical rows in one display cue cannot be edited as
                # a single text value without an explicit passage-level edit.
                raise ValueError(
                    "Display cue contains multiple logical passages; edit their source passages separately."
                )
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
        direct_ids = set(source.get("source_cue_ids", []))
        direct = [
            segment for segment_id in direct_ids
            for segment in source_owners.get(segment_id, [])
        ]
        expected = direct or [
            source_segments[index]
            for index in source_segment_indexes[source_indexes_by_id[row_id]]
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
        fragment_speakers = [
            str(cue.get("speaker") or "").strip()
            for _segment, cue in ordered
        ]
        original_fragment_speakers = [
            _speaker_and_text(segment)[0].strip() for segment, _cue in ordered
        ]
        speaker_changed = any(
            submitted.casefold() != original.casefold()
            for submitted, original in zip(
                fragment_speakers, original_fragment_speakers, strict=True
            )
        )
        if speaker_changed:
            if len({speaker.casefold() for speaker in fragment_speakers}) > 1:
                source_row_ids = ", ".join(segment.id for segment, _cue in ordered)
                raise ValueError(
                    f"Logical passage {row_id!r} has conflicting speakers across source rows "
                    f"[{source_row_ids}]; align speakers or use a passage edit."
                )
            stored["speaker"] = fragment_speakers[0]
    for index, row in enumerate(output, start=1):
        row["id"] = f"p{index:06d}"
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

    def _source_file_witness(self, source: Artifact) -> tuple[int, ...] | None:
        if not source.content_hash:
            return None
        try:
            path = self.artifacts.paths.managed_path(source.relative_path)
            before = _stat_witness(path)
            digest = sha256_file(path)
            after = _stat_witness(path)
            return after if before == after and digest == source.content_hash else None
        except (OSError, ValueError):
            return None

    def _source_hash_matches(self, source: Artifact) -> bool:
        return self._source_file_witness(source) is not None

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
        cls, segment: Segment, passages: list[dict[str, Any]], *,
        passage_by_id: dict[str, dict[str, Any]] | None = None,
        cue_owners: dict[str, list[dict[str, Any]]] | None = None,
        fallback: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = cls._payload(segment)
        segment_metadata = dict(segment.metadata_json or {})
        canonical_ids = segment_metadata.get("source_passage_ids") or []
        if passage_by_id is None:
            passage_by_id = {str(row["id"]): row for row in passages}
        owned = (
            [passage_by_id[str(row_id)] for row_id in canonical_ids]
            if segment_metadata.get("canonical_passage_review")
            and canonical_ids
            and all(str(row_id) in passage_by_id for row_id in canonical_ids)
            else cue_owners.get(segment.id, []) if cue_owners is not None
            else [row for row in passages if segment.id in row.get("source_cue_ids", [])]
        )
        if not owned:
            owned = fallback if fallback is not None else [
                row for row in passages
                if segment.start_ms is not None and segment.end_ms is not None
                and _overlaps(segment.start_ms, segment.end_ms, row)
            ]
        payload["source_passage_ids"] = [row["id"] for row in owned]
        turns = {str(row.get("turn_id") or "") for row in owned}
        payload["turn_id"] = next(iter(turns)) if len(turns) == 1 else None
        payload["owned_passages"] = [
            {
                "id": row["id"],
                "text": row["text"],
                "speaker": str(row.get("speaker") or ""),
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "turn_id": row.get("turn_id"),
                "review_state": str(row.get("review_state") or "clear"),
                "review_note": str(row.get("review_note") or ""),
            }
            for row in owned
        ]
        capabilities = {
            "ownership": "unverified",
            "text": False,
            "display_timing": False,
            "speaker": False,
            "start_new_utterance": False,
            "split": False,
            "merge": False,
            "delete": False,
            "reason": (
                "Capabilities are unknown because this display cue has no verified "
                "source passage."
            ),
        }
        if len(owned) > 1:
            passage_ids = ", ".join(str(row["id"]) for row in owned)
            capabilities.update(
                ownership="combined",
                reason=(
                    f"This display cue combines spoken passages [{passage_ids}]. "
                    "Edit spoken passages separately."
                ),
            )
        elif len(owned) == 1:
            row = owned[0]
            if (
                segment.start_ms == row["start_ms"]
                and segment.end_ms == row["end_ms"]
            ):
                capabilities.update(
                    ownership="exact",
                    text=True,
                    display_timing=True,
                    speaker=True,
                    start_new_utterance=True,
                    split=True,
                    merge=True,
                    delete=True,
                    reason="",
                )
            else:
                capabilities.update(
                    ownership="fragment",
                    text=True,
                    reason=(
                        f"This display cue is part of spoken passage {row['id']}. "
                        "Change its speaker, timing or utterance at passage level; "
                        "keep every fragment when saving."
                    ),
                )
        payload["edit_capabilities"] = capabilities
        return payload

    @classmethod
    def _payloads_with_passages(
        cls, segments: list[Segment], passages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        passage_by_id = {str(row["id"]): row for row in passages}
        cue_owners: dict[str, list[dict[str, Any]]] = {}
        for row in passages:
            # A repeated source cue id still owns this passage only once.
            for cue_id in dict.fromkeys(row.get("source_cue_ids", [])):
                cue_owners.setdefault(cue_id, []).append(row)
        fallback_ordinals = []
        for ordinal, item in enumerate(segments):
            metadata = item.metadata_json or {}
            canonical_ids = metadata.get("source_passage_ids") or []
            has_canonical_ownership = (
                metadata.get("canonical_passage_review") and canonical_ids
                and all(str(row_id) in passage_by_id for row_id in canonical_ids)
            )
            if not has_canonical_ownership and not cue_owners.get(item.id):
                fallback_ordinals.append(ordinal)
        fallback_indexes = overlap_indexes(
            [(row["start_ms"], row["end_ms"]) for row in passages],
            [(segments[index].start_ms or 0, segments[index].end_ms or 0)
             for index in fallback_ordinals],
        ) if fallback_ordinals else []
        fallbacks = {
            ordinal: [passages[index] for index in matches]
            for ordinal, matches in zip(fallback_ordinals, fallback_indexes, strict=True)
        }
        return [cls._payload_with_passages(
            item, passages, passage_by_id=passage_by_id, cue_owners=cue_owners,
            fallback=fallbacks.get(ordinal, []),
        ) for ordinal, item in enumerate(segments)]

    def documents(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            require_current_session_access(session, session_id, ProjectAction.READ)
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
            require_current_session_access(session, session_id, ProjectAction.READ)
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

        with self.database.snapshot_session() as session:
            fallback_cache: dict[str, PrimarySourceResolution] = {}
            resolved_settings, composition_hash = WorkspaceSettingsService(
                self.database
            ).resolve_in_session(session, session_id, sections=["subtitles"])
            composition_settings = deepcopy(resolved_settings.get("subtitles") or {})
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
                passages = stored_passages(artifact)
                if not passages:
                    passages = source_passages(session, artifact, segments=records)
                source_media_artifact_id: str | None = None
                source_media_error: str | None = None
                source_media_mime_type: str | None = None
                source_media_kind: str | None = None
                try:
                    media_artifact = resolve_subtitle_media(
                        session, session_id, artifact,
                        fallback_cache=fallback_cache,
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
                        "composition_hash": composition_hash,
                        "composition_settings": deepcopy(composition_settings),
                        "source_media_artifact_id": source_media_artifact_id,
                        "source_media_mime_type": source_media_mime_type,
                        "source_media_kind": source_media_kind,
                        "source_media_error": source_media_error,
                        "logical_passages": deepcopy(passages),
                        "segments": self._payloads_with_passages(records, passages),
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
            # A full interval join handles nested/unsorted legacy windows.
            left_records = [item for item in stage_segments[left_stage]
                            if item.start_ms is not None and item.end_ms is not None]
            right_records = [item for item in stage_segments[right_stage]
                             if item.start_ms is not None and item.end_ms is not None]
            indexes = overlap_indexes(
                [(item.start_ms or 0, item.end_ms or 0) for item in left_records],
                [(item.start_ms or 0, item.end_ms or 0) for item in right_records],
            )
            for right, matches in zip(right_records, indexes, strict=True):
                for index in matches:
                    union((left_stage, left_records[index].id), (right_stage, right.id))

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

    def prepare_review(
        self,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        source_artifact_id: str | None = None,
        expected_source_hash: str | None = None,
        db_session=None,
        _prepared_canonical: list[dict[str, Any]] | None = None,
        _prepared_composition_settings: dict[str, Any] | None = None,
        _prepared_composition_hash: str | None = None,
    ) -> PreparedSubtitleReview | dict[str, Any]:
        if db_session is None:
            with self.database.snapshot_session() as session:
                return self.prepare_review(
                    session_id, stage, expected_revision, values,
                    dry_run=dry_run, source_artifact_id=source_artifact_id,
                    expected_source_hash=expected_source_hash, db_session=session,
                    _prepared_canonical=_prepared_canonical,
                    _prepared_composition_settings=_prepared_composition_settings,
                    _prepared_composition_hash=_prepared_composition_hash,
                )
        require_current_session_access(db_session, session_id, ProjectAction.EDIT)
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
            normalized_segment: ReviewedSubtitleSegment = {
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
            canonical_passage_id = str(
                item.get("_canonical_passage_id") or ""
            ).strip()
            if canonical_passage_id:
                normalized_segment["_canonical_passage_id"] = canonical_passage_id
            normalized.append(normalized_segment)
        if not normalized:
            raise ValueError("A reviewed subtitle document cannot be empty.")

        with (
            self.database.session()
            if db_session is None
            else _SessionContext(db_session)
        ) as session:
            composition_settings: dict[str, Any] = {}
            composition_hash: str | None = None
            if _prepared_canonical is not None:
                resolved_settings, composition_hash = WorkspaceSettingsService(
                    self.database
                ).resolve_in_session(session, session_id, sections=["subtitles"])
                composition_settings = deepcopy(
                    resolved_settings.get("subtitles") or {}
                )
                if (
                    not _prepared_composition_hash
                    or composition_hash != _prepared_composition_hash
                    or composition_settings
                    != (_prepared_composition_settings or {})
                ):
                    raise RuntimeError(
                        "Subtitle composition settings changed; refresh the review."
                    )
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
            prepared_by_id = {
                str(row.get("id") or ""): row
                for row in (_prepared_canonical or [])
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
                    evidence_window = prepared_by_id.get(
                        str(item.get("_canonical_passage_id") or "")
                    )
                    source_start = (
                        evidence_window.get("_source_start_ms")
                        if evidence_window is not None
                        else None
                    )
                    source_end = (
                        evidence_window.get("_source_end_ms")
                        if evidence_window is not None
                        else None
                    )
                    window_start = (
                        source_start
                        if isinstance(source_start, int)
                        else item["start_ms"]
                    )
                    window_end = (
                        source_end
                        if isinstance(source_end, int)
                        else item["end_ms"]
                    )
                    if min(window_end, evidence.end_ms) <= max(
                        window_start, evidence.start_ms
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
            source_stat = (
                self._source_file_witness(source_artifact)
                if source_artifact is not None else None
            )
            if source_artifact is not None and (
                (expected_source_hash is not None and source_artifact.content_hash != expected_source_hash)
                or source_stat is None
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
                if not dry_run:
                    document.id = str(uuid4())
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
                if source_artifact is not None:
                    source_stat = self._source_file_witness(source_artifact)
                    if (
                        (source_artifact.metadata_json or {}).get("revision_id") != previous.id
                        or source_stat is None
                    ):
                        source_artifact = None
                        source_stat = None
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
                    split_children = [item for item in normalized if item["origin_segment_id"] == origin_id]
                    if len(split_children) != 2 or not split_children[0]["split_boundary_id"] or split_children[0]["split_boundary_id"] != split_children[1]["split_boundary_id"]:
                        raise ValueError("A review split requires two split_children with one shared verified boundary.")
                    if any(item["id"] is not None and item["id"] != origin_id for item in split_children):
                        raise ValueError("Split child id must be absent or match origin_segment_id.")
                    inspection = self.inspect_review_split_boundaries(
                        session_id, source_artifact.id, origin_id, expected_revision,
                        offset=0, limit=100,
                    )
                    boundary = next((row for row in inspection["boundaries"] if row["id"] == split_children[0]["split_boundary_id"]), None)
                    while boundary is None and inspection["next_offset"] is not None:
                        inspection = self.inspect_review_split_boundaries(
                            session_id, source_artifact.id, origin_id,
                            expected_revision, offset=inspection["next_offset"], limit=100,
                        )
                        boundary = next((row for row in inspection["boundaries"] if row["id"] == split_children[0]["split_boundary_id"]), None)
                    if boundary is None:
                        raise ValueError("Split boundary is unavailable or stale; inspect source anchors.")
                    origin = next(item for item in previous_segments if item.id == origin_id)
                    if (split_children[0]["start_ms"], split_children[0]["end_ms"]) != (origin.start_ms, boundary["left_end_ms"]) or (split_children[1]["start_ms"], split_children[1]["end_ms"]) != (boundary["right_start_ms"], origin.end_ms):
                        raise ValueError("Review split timing must match verified source-word windows.")
                    source_row = next(row for row in source_rows if row["id"] == inspection["source_passage_id"])
                    if any(
                        item["source_passage_ids"]
                        and item["source_passage_ids"] != [source_row["id"]]
                        for item in split_children
                    ):
                        raise ValueError("Split child logical source references must match the verified origin passage.")
                    split_at = boundary["after_word"]
                    split_children[0]["_source_word_ids"] = list(source_row.get("source_word_ids") or [])[:split_at]
                    split_children[1]["_source_word_ids"] = list(source_row.get("source_word_ids") or [])[split_at:]
                if any(item["split_boundary_id"] and not item["origin_segment_id"] for item in normalized):
                    raise ValueError("Split boundary requires origin_segment_id.")
                if _prepared_canonical is not None:
                    logical_rows = _merge_prepared_canonical_rows(
                        source_rows, _prepared_canonical
                    )
                    canonical_ids = {str(row["id"]) for row in logical_rows}
                    if any(
                        item.get("_canonical_passage_id") not in canonical_ids
                        for item in normalized
                    ):
                        raise ValueError(
                            "Finalized display cues must identify a retained canonical passage."
                        )
                else:
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
            exact_speakers_by_timing: dict[tuple[int | None, int | None], dict[str, str]] = {}
            previous_speakers: list[str] = []
            for item in previous_segments:
                speaker = _speaker_and_text(item)[0]
                previous_speakers.append(speaker)
                if speaker:
                    exact_speakers_by_timing.setdefault(
                        (item.start_ms, item.end_ms), {}
                    ).setdefault(speaker.casefold(), speaker)
            speaker_indexes = overlap_indexes(
                [(item.start_ms or 0, item.end_ms or 0) for item in previous_segments],
                [(item["start_ms"], item["end_ms"]) for item in normalized],
            )
            for index, reviewed in enumerate(normalized, start=1):
                if _prepared_canonical is not None:
                    continue
                # Genuine crosstalk can make an existing cue overlap another speaker.
                # Preserve that inherited overlap when the reviewed cue keeps the same
                # timing, while still rejecting newly created cues whose timing spans
                # multiple speakers. Exact-timing cues may also correct speaker labels.
                exact_speakers = exact_speakers_by_timing.get(
                    (reviewed["start_ms"], reviewed["end_ms"]), {}
                )

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
                for parent_index in speaker_indexes[index - 1]:
                    speaker = previous_speakers[parent_index]
                    if speaker:
                        overlapping_speakers.setdefault(speaker.casefold(), speaker)
                if len(overlapping_speakers) > 1:
                    raise ValueError(
                        f"Segment {index} crosses a speaker boundary. Keep each speaker in a separate cue."
                    )
                if not reviewed["speaker"] and overlapping_speakers:
                    reviewed["speaker"] = next(iter(overlapping_speakers.values()))
            if dry_run:
                return {
                    "dry_run": True,
                    "validated": True,
                    "session_id": session_id,
                    "stage": stage,
                    "document_id": document.id,
                    "revision": expected_revision,
                    "source_artifact_id": (
                        source_artifact.id if source_artifact is not None else None
                    ),
                    "source_content_hash": (
                        source_artifact.content_hash
                        if source_artifact is not None
                        else None
                    ),
                    **(
                        {"composition_hash": composition_hash}
                        if _prepared_canonical is not None
                        else {}
                    ),
                    "cue_count": len(normalized),
                    "logical_passage_count": len(logical_rows),
                }
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
            revision_id = str(uuid4())
            children: list[dict[str, Any]] = []
            logical_rows_by_id = {
                str(row["id"]): row for row in logical_rows
            }
            linked_indexes = overlap_indexes(
                [(row["start_ms"], row["end_ms"]) for row in logical_rows],
                [(row["start_ms"], row["end_ms"]) for row in normalized],
            )
            parent_indexes = overlap_indexes(
                [(row.start_ms or 0, row.end_ms or 0) for row in previous_segments],
                [(row["start_ms"], row["end_ms"]) for row in normalized],
            )
            for ordinal, reviewed in enumerate(normalized):
                canonical_passage_id = reviewed.get("_canonical_passage_id")
                linked = (
                    [logical_rows_by_id[str(canonical_passage_id)]]
                    if canonical_passage_id is not None
                    else [logical_rows[index] for index in linked_indexes[ordinal]]
                )
                linked_turns = {str(row.get("turn_id") or "") for row in linked}
                canonical_metadata = (
                    linked[0]
                    if canonical_passage_id is not None and linked
                    else None
                )
                child: dict[str, Any] = dict(
                    ordinal=ordinal,
                    start_ms=reviewed["start_ms"],
                    end_ms=reviewed["end_ms"],
                    text=reviewed["text"],
                    speaker=reviewed["speaker"],
                    metadata_json={
                        "review_state": (
                            canonical_metadata.get("review_state", "clear")
                            if canonical_metadata is not None
                            else reviewed["review_state"]
                        ),
                        "review_note": (
                            canonical_metadata.get("review_note", "")
                            if canonical_metadata is not None
                            else reviewed["review_note"]
                        ),
                        "evidence_ids": (
                            list(canonical_metadata.get("evidence_ids") or [])
                            if canonical_metadata is not None
                            else reviewed["evidence_ids"]
                        ),
                        "uncertain_source_cue_ids": (
                            list(
                                canonical_metadata.get(
                                    "uncertain_source_cue_ids"
                                )
                                or []
                            )
                            if canonical_metadata is not None
                            else reviewed["uncertain_source_cue_ids"]
                        ),
                        "source_passage_ids": (
                            [str(canonical_passage_id)]
                            if canonical_passage_id is not None
                            else list(dict.fromkeys(
                                source_id
                                for row in linked
                                for source_id in row.get("source_passage_ids") or [row["id"]]
                            ))
                        ),
                        **(
                            {"canonical_passage_review": True}
                            if canonical_passage_id is not None
                            else {}
                        ),
                        **({"turn_id": next(iter(linked_turns))} if len(linked_turns) == 1 and next(iter(linked_turns)) else {}),
                    },
                )
                child["parent_ids"] = [
                    previous_segments[index].id for index in parent_indexes[ordinal]
                ]
                children.append(child)
            document_values = {
                "id": document.id, "session_id": session_id, "stage": stage,
                "language": document.language,
                "active_revision_id": document.active_revision_id,
                "exists": previous is not None,
            }
            source_values = (
                _record_values(source_artifact, _SOURCE_FIELDS)
                if source_artifact is not None else None
            )
            parent_artifact = source_artifact or session.scalar(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role == ("tts_optimized" if stage == "tts_optimization" else stage),
                    Artifact.state == "current",
                ).order_by(Artifact.created_at.desc())
            )
            parent_values = (
                _record_values(parent_artifact, _SOURCE_FIELDS)
                if parent_artifact is not None else None
            )
            previous_values = (
                _record_values(previous, _REVISION_FIELDS)
                if previous is not None else None
            )
            evidence_values = {
                key: _record_values(value, _EVIDENCE_FIELDS)
                for key, value in evidence_by_id.items()
            }
            resolved, settings_hash = WorkspaceSettingsService(self.database).resolve_in_session(
                session, session_id, sections=["subtitles"]
            )
            diagnostic_settings = resolved.get("subtitles") or {}
            if _prepared_canonical is not None and settings_hash != composition_hash:
                raise RuntimeError("Subtitle composition settings changed; refresh the review.")

        content = compose_srt([
            SubtitleSegment(
                index=index, start_ms=item["start_ms"], end_ms=item["end_ms"],
                text=item["text"], speaker=str(item.get("speaker") or ""),
            )
            for index, item in enumerate(normalized, start=1)
        ])
        speakers = {
            str(item.get("speaker") or "").strip().casefold()
            for item in normalized if str(item.get("speaker") or "").strip()
        }
        settings = {
            "reviewed": True, "revision": next_revision_number,
            **({"composition_hash": composition_hash,
                "composition_settings": composition_settings}
               if _prepared_canonical is not None else {}),
        }
        metadata = {
            "document_id": document.id, "revision_id": revision_id, "stage": stage,
            "language": document.language,
            "subtitle_diagnostics": subtitle_export_diagnostics(
                passage_srt(logical_rows), content,
                adapt_runtime_settings("subtitles", diagnostic_settings), None,
                language=document.language or "", language_origin="reviewed_document",
            ),
            "reviewed": True, "has_speaker_metadata": bool(speakers),
            "speaker_count": len(speakers),
            "uncertain_segment_count": sum(
                item["review_state"] == "uncertain" for item in normalized
            ),
            **({"composition_hash": composition_hash,
                "composition_settings": deepcopy(composition_settings)}
               if _prepared_canonical is not None else {}),
        }
        # Precompute the logical packet on transient records, including its
        # validation/hashes. Only self-source identity needs filling at publish.
        output_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        detached = Artifact(content_hash=output_hash, metadata_json=metadata)
        detached_source = (
            Artifact(**source_values) if source_values is not None else detached
        )
        source_packet = (
            ((source_values["metadata_json"] or {}).get("logical_passages") or {})
            if source_values is not None else {}
        )
        attach_passages(
            detached, logical_rows, source=detached_source,
            source_passage_settings=source_packet.get("source_passage_settings"),
            source_passage_settings_revision=source_packet.get("source_passage_settings_revision"),
            policy_version=source_packet.get("policy_version"),
        )
        directory = self.session_dir_resolver(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"reviewed_{stage}_r{next_revision_number}_{uuid4().hex}.srt"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=directory
        )
        staged = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            registration = self.artifacts.prepare_registration(staged, settings=settings)
            registration = replace(
                registration,
                relative_path=self.artifacts.paths.relative_managed_path(destination),
                mime_type=mimetypes.guess_type(destination.name)[0],
            )
            return PreparedSubtitleReview(
                session_id=session_id, stage=stage, document=document_values,
                previous=previous_values, next_revision_number=next_revision_number,
                source=source_values, parent=parent_values, source_stat=source_stat, evidence=evidence_values,
                settings_hash=settings_hash, composition_hash=composition_hash,
                revision_id=revision_id, content_hash=_segments_hash(normalized),
                children=children, metadata=deepcopy(detached.metadata_json),
                settings=settings, staged_path=staged, destination=destination,
                registration=registration,
            )
        except BaseException:
            cleanup_review_publications([staged])
            raise

    def save_review(
        self, session_id: str, stage: str, expected_revision: int,
        values: list[dict[str, Any]], *, dry_run: bool = False,
        source_artifact_id: str | None = None,
        expected_source_hash: str | None = None, db_session=None,
        published_paths: list[Path] | None = None,
        _prepared_canonical: list[dict[str, Any]] | None = None,
        _prepared_composition_settings: dict[str, Any] | None = None,
        _prepared_composition_hash: str | None = None,
    ) -> dict[str, Any]:
        """Prepare outside our writer; a caller-owned transaction sets its limit.

        Compatibility callers supplying db_session own transaction duration and
        must retain published_paths for cleanup if their eventual commit fails.
        """
        prepared = self.prepare_review(
            session_id, stage, expected_revision, values, dry_run=dry_run,
            source_artifact_id=source_artifact_id, expected_source_hash=expected_source_hash,
            db_session=db_session, _prepared_canonical=_prepared_canonical,
            _prepared_composition_settings=_prepared_composition_settings,
            _prepared_composition_hash=_prepared_composition_hash,
        )
        if isinstance(prepared, dict):
            return prepared
        owned_paths: list[Path] = []
        try:
            with (self.database.immediate_session() if db_session is None
                  else _SessionContext(db_session)) as session:
                result = self.publish_prepared_review_in_session(session, prepared, owned_paths)
            if published_paths is not None:
                published_paths.extend(owned_paths)
            return result
        except BaseException:
            cleanup_review_publications(owned_paths)
            raise
        finally:
            prepared.cleanup()

    def publish_prepared_review_in_session(
        self, session, prepared: PreparedSubtitleReview, published_paths: list[Path],
    ) -> dict[str, Any]:
        """Guard detached witnesses, then publish in the caller's short writer."""
        require_current_session_access(session, prepared.session_id, ProjectAction.EDIT)
        document_values = prepared.document
        document = session.get(Document, document_values["id"], populate_existing=True)
        if document_values["exists"]:
            if prepared.previous is None:
                raise RuntimeError("Subtitle source revision changed.")
            if document is None or any(
                getattr(document, field) != document_values[field]
                for field in ("session_id", "stage", "language", "active_revision_id")
            ):
                raise RuntimeError("Subtitle document head changed; refresh the review.")
            previous = session.get(DocumentRevision, prepared.previous["id"], populate_existing=True)
            if previous is None or _record_values(previous, _REVISION_FIELDS) != prepared.previous:
                raise RuntimeError("Subtitle source revision changed.")
            maximum = int(session.scalar(select(func.max(DocumentRevision.revision_number)).where(
                DocumentRevision.document_id == document.id
            )) or 0)
            if maximum + 1 != prepared.next_revision_number:
                raise RuntimeError("Subtitle revision changed; refresh the review.")
        else:
            existing = session.scalar(select(Document.id).where(
                Document.session_id == prepared.session_id, Document.stage == prepared.stage,
            ).limit(1))
            record = session.get(SessionRecord, prepared.session_id, populate_existing=True)
            language = (
                record.target_language if prepared.stage in {"translation", "tts_optimization"}
                else record.source_language
            ) if record is not None else None
            if (existing is not None or record is None
                    or (None if language == "auto" else language) != document_values["language"]):
                raise RuntimeError("Subtitle document head changed; refresh the review.")
        source = None
        if prepared.source is not None:
            source = session.get(Artifact, prepared.source["id"], populate_existing=True)
            if source is None or _record_values(source, _SOURCE_FIELDS) != prepared.source:
                raise RuntimeError("Subtitle source artifact changed; refresh the review.")
            try:
                stat = _stat_witness(self.artifacts.paths.managed_path(source.relative_path))
            except (OSError, ValueError):
                raise RuntimeError("Subtitle source content hash changed.") from None
            if stat != prepared.source_stat:
                raise RuntimeError("Subtitle source content hash changed.")
        if prepared.source is None and prepared.parent is not None:
            parent_artifact = session.get(Artifact, prepared.parent["id"], populate_existing=True)
            if parent_artifact is None or _record_values(parent_artifact, _SOURCE_FIELDS) != prepared.parent:
                raise RuntimeError("Subtitle parent artifact changed; refresh the review.")
        evidence = {
            item.id: _record_values(item, _EVIDENCE_FIELDS)
            for item in session.scalars(select(SubtitleEvidence).where(
                SubtitleEvidence.id.in_(prepared.evidence)
            ).execution_options(populate_existing=True)).all()
        } if prepared.evidence else {}
        if evidence != prepared.evidence:
            raise RuntimeError("Subtitle evidence changed; refresh the review.")
        _, settings_hash = WorkspaceSettingsService(self.database).resolve_in_session(
            session, prepared.session_id, sections=["subtitles"]
        )
        if settings_hash != prepared.settings_hash:
            raise RuntimeError("Subtitle composition settings changed; refresh the review.")
        if prepared.destination.exists() or session.scalar(select(Artifact.id).where(
            Artifact.relative_path == prepared.registration.relative_path
        ).limit(1)) is not None:
            raise RuntimeError("Subtitle publication path already exists.")
        if document is None:
            document = Document(**{key: value for key, value in document_values.items() if key != "exists"})
            session.add(document)
            session.flush()
        revision = DocumentRevision(
            id=prepared.revision_id, document_id=document.id,
            parent_revision_id=prepared.previous["id"] if prepared.previous else None,
            revision_number=prepared.next_revision_number, content_hash=prepared.content_hash,
            reviewed=True, settings_hash=prepared.composition_hash,
        )
        session.add(revision)
        session.flush()
        children = [Segment(
            revision_id=revision.id,
            **{key: value for key, value in row.items() if key != "parent_ids"},
        ) for row in prepared.children]
        session.add_all(children)
        session.flush()
        session.add_all([
            SegmentLineage(parent_segment_id=parent_id, child_segment_id=child.id,
                           relation="reviewed", sequence=sequence)
            for child, row in zip(children, prepared.children, strict=True)
            for sequence, parent_id in enumerate(row["parent_ids"])
        ])
        document.active_revision_id = revision.id
        # This UUID destination belongs only to this attempt; the explicit
        # existence guard above prevents replacing an existing publication.
        # Same-directory replacement also works on data roots without hard links.
        os.replace(prepared.staged_path, prepared.destination)
        published_paths.append(prepared.destination)
        # File fsync during preparation does not persist the renamed directory
        # entry. Keep the previous publication's best-effort crash durability.
        try:
            directory_fd = os.open(prepared.destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is not supported on every platform/filesystem.
            pass
        artifact = self.artifacts.register_in_session(
            session, prepared.destination, kind="srt",
            role="tts_optimized" if prepared.stage == "tts_optimization" else prepared.stage,
            session_id=prepared.session_id,
            parent_ids=[prepared.parent["id"]] if prepared.parent else [],
            settings=prepared.settings, metadata=deepcopy(prepared.metadata),
            _prepared=prepared.registration,
        )
        if source is None:
            metadata = deepcopy(artifact.metadata_json)
            metadata["logical_passages"]["source_artifact_id"] = artifact.id
            artifact.metadata_json = metadata
        return {"artifact_id": artifact.id, "document_id": document.id,
                "revision_id": revision.id, "revision": revision.revision_number}

    def save_passage_review(
        self,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        source_artifact_id: str,
        expected_source_hash: str,
        expected_composition_hash: str,
    ) -> dict[str, Any]:
        """Prepare canonical edits before acquiring our publication writer."""
        prepared = self.prepare_passage_review(
            session_id, stage, expected_revision, values, dry_run=dry_run,
            source_artifact_id=source_artifact_id,
            expected_source_hash=expected_source_hash,
            expected_composition_hash=expected_composition_hash,
        )
        if isinstance(prepared, dict):
            return prepared
        paths: list[Path] = []
        try:
            with self.database.immediate_session() as session:
                return self.publish_prepared_review_in_session(session, prepared, paths)
        except BaseException:
            cleanup_review_publications(paths)
            raise
        finally:
            prepared.cleanup()

    def prepare_passage_review(
        self, session_id: str, stage: str, expected_revision: int,
        values: list[dict[str, Any]], *, source_artifact_id: str,
        expected_source_hash: str, expected_composition_hash: str,
        dry_run: bool = False,
    ) -> PreparedSubtitleReview | dict[str, Any]:
        with self.database.snapshot_session() as session:
            require_current_session_access(session, session_id, ProjectAction.EDIT)
            rows, display_values, settings = self._prepare_passage_review(
                session, session_id, stage, expected_revision, values,
                source_artifact_id=source_artifact_id,
                expected_source_hash=expected_source_hash,
                expected_composition_hash=expected_composition_hash,
            )
            return self.prepare_review(
                session_id, stage, expected_revision, display_values,
                dry_run=dry_run, source_artifact_id=source_artifact_id,
                expected_source_hash=expected_source_hash, db_session=session,
                _prepared_canonical=rows, _prepared_composition_settings=settings,
                _prepared_composition_hash=expected_composition_hash,
            )

    def save_passage_review_in_session(
        self,
        session,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        source_artifact_id: str,
        expected_source_hash: str,
        expected_composition_hash: str,
        published_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Prepare source-owned canonical edits within the caller's transaction."""
        prepared, display_values, composition_settings = (
            self._prepare_passage_review(
                session,
                session_id,
                stage,
                expected_revision,
                values,
                source_artifact_id=source_artifact_id,
                expected_source_hash=expected_source_hash,
                expected_composition_hash=expected_composition_hash,
            )
        )
        return self.save_review(
            session_id,
            stage,
            expected_revision,
            display_values,
            dry_run=dry_run,
            source_artifact_id=source_artifact_id,
            expected_source_hash=expected_source_hash,
            db_session=session,
            published_paths=published_paths,
            _prepared_canonical=prepared,
            _prepared_composition_settings=composition_settings,
            _prepared_composition_hash=expected_composition_hash,
        )

    def _prepare_passage_review(
        self,
        session,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        source_artifact_id: str,
        expected_source_hash: str,
        expected_composition_hash: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        require_current_session_access(session, session_id, ProjectAction.EDIT)
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unsupported subtitle stage: {stage}")
        source = session.get(Artifact, source_artifact_id)
        if (
            source is None
            or source.session_id != session_id
            or ARTIFACT_ROLE_TO_STAGE.get(source.role) != stage
            or source.state == "deleted"
        ):
            raise KeyError(source_artifact_id)
        if (
            not source.content_hash
            or source.content_hash != expected_source_hash
            or not self._source_hash_matches(source)
        ):
            raise RuntimeError("Subtitle source content hash changed.")
        source_metadata = source.metadata_json or {}
        document = session.get(
            Document, str(source_metadata.get("document_id") or "")
        )
        previous = session.get(
            DocumentRevision, str(source_metadata.get("revision_id") or "")
        )
        if (
            document is None
            or previous is None
            or document.session_id != session_id
            or document.stage != stage
            or previous.document_id != document.id
        ):
            raise KeyError(source_artifact_id)
        if previous.revision_number != expected_revision:
            raise RuntimeError(
                f"Subtitle revision changed from {expected_revision} to "
                f"{previous.revision_number}."
            )
        source_segments = list(
            session.scalars(
                select(Segment)
                .where(Segment.revision_id == previous.id)
                .order_by(Segment.ordinal)
            ).all()
        )
        source_rows = source_passages(session, source, segments=source_segments)
        if not source_rows:
            raise ValueError("The selected source has no valid logical passages.")
        if source_metadata.get("revision_id") != previous.id:
            raise RuntimeError("Subtitle source revision changed.")

        resolved, composition_hash = WorkspaceSettingsService(
            self.database
        ).resolve_in_session(session, session_id, sections=["subtitles"])
        if not expected_composition_hash or composition_hash != expected_composition_hash:
            raise RuntimeError(
                "Subtitle composition settings changed; refresh the review."
            )
        composition_settings = deepcopy(resolved.get("subtitles") or {})

        source_by_id = {str(row["id"]): row for row in source_rows}
        submitted_ids: list[str] = []
        submitted_by_id: dict[str, dict[str, Any]] = {}
        for raw in values:
            row_id = str(raw.get("id") or "").strip()
            if not row_id:
                raise ValueError("Every edited passage requires its source id.")
            submitted_ids.append(row_id)
            if row_id in submitted_by_id:
                raise ValueError(f"Duplicate source passage id: {row_id}.")
            submitted_by_id[row_id] = raw
        if len(submitted_ids) != len(source_by_id) or set(submitted_ids) != set(
            source_by_id
        ):
            missing = sorted(set(source_by_id) - set(submitted_ids))
            foreign = sorted(set(submitted_ids) - set(source_by_id))
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if foreign:
                detail.append(f"foreign {', '.join(foreign)}")
            raise ValueError(
                "Passage review must include every selected source passage exactly "
                f"once ({'; '.join(detail) or 'duplicate ids'})."
            )

        prepared_by_id: dict[str, dict[str, Any]] = {}
        for row_id, raw in submitted_by_id.items():
            source_row = source_by_id[row_id]
            start_ms = raw.get("start_ms")
            end_ms = raw.get("end_ms")
            if (
                isinstance(start_ms, bool)
                or not isinstance(start_ms, int)
                or start_ms < 0
                or isinstance(end_ms, bool)
                or not isinstance(end_ms, int)
                or end_ms <= start_ms
            ):
                raise ValueError(f"Passage {row_id} has invalid timing.")
            text = str(raw.get("text") or "").strip()
            if not text:
                raise ValueError(f"Passage {row_id} cannot have empty text.")
            if type(raw.get("deleted", False)) is not bool:
                raise ValueError(f"Passage {row_id} has an invalid deleted flag.")
            if type(raw.get("starts_new_turn", False)) is not bool:
                raise ValueError(
                    f"Passage {row_id} has an invalid starts_new_turn flag."
                )
            review_state = str(raw.get("review_state") or "clear").strip().lower()
            if review_state not in {"clear", "uncertain"}:
                raise ValueError(f"Passage {row_id} has an invalid review state.")
            speaker = str(raw.get("speaker") or "").strip()
            edited = deepcopy(source_row)
            edited.update(
                text=text,
                speaker=speaker,
                start_ms=start_ms,
                end_ms=end_ms,
                review_state=review_state,
                review_note=" ".join(
                    str(raw.get("review_note") or "").split()
                ).strip()[:4_000],
                _deleted=raw.get("deleted", False),
                _starts_new_turn=raw.get("starts_new_turn", False),
                _source_start_ms=source_row["start_ms"],
                _source_end_ms=source_row["end_ms"],
            )
            prepared_by_id[row_id] = edited

        retained = [
            prepared_by_id[str(row["id"])]
            for row in source_rows
            if not prepared_by_id[str(row["id"])]["_deleted"]
        ]
        if not retained:
            raise ValueError("A reviewed subtitle document cannot be empty.")

        timing_order = sorted(retained, key=lambda row: (row["start_ms"], row["end_ms"]))
        for index, row in enumerate(timing_order):
            row_speaker = str(row.get("speaker") or "").strip().casefold()
            for other_index in range(index + 1, len(timing_order)):
                other = timing_order[other_index]
                if other["start_ms"] >= row["end_ms"]:
                    break
                other_speaker = str(other.get("speaker") or "").strip().casefold()
                if (
                    not row_speaker
                    or not other_speaker
                    or row_speaker == other_speaker
                    or not _overlaps(row["start_ms"], row["end_ms"], other)
                ):
                    continue
                source_row = source_by_id[str(row["id"])]
                source_other = source_by_id[str(other["id"])]
                timing_changed = (
                    row["start_ms"] != source_row["start_ms"]
                    or row["end_ms"] != source_row["end_ms"]
                    or other["start_ms"] != source_other["start_ms"]
                    or other["end_ms"] != source_other["end_ms"]
                )
                source_speaker = str(
                    source_row.get("speaker") or ""
                ).strip().casefold()
                source_other_speaker = str(
                    source_other.get("speaker") or ""
                ).strip().casefold()
                inherited_crosstalk = (
                    _overlaps(source_row["start_ms"], source_row["end_ms"], source_other)
                    and source_speaker
                    and source_other_speaker
                    and source_speaker != source_other_speaker
                )
                if timing_changed and not inherited_crosstalk:
                    raise ValueError(
                        f"Passage {row['id']} timing creates cross-speaker overlap "
                        f"with passage {other['id']}."
                    )

        active_turn = ""
        previous_source_turn: str | None = None
        prepared: list[dict[str, Any]] = []
        display_values: list[dict[str, Any]] = []
        runtime_settings = adapt_runtime_settings(
            "subtitles", composition_settings
        )
        full_text = " ".join(str(row["text"]) for row in retained)
        finalization = SubtitleFinalizationConfig.from_settings(
            runtime_settings,
            language=document.language or "",
            text=full_text,
        )
        for ordinal, source_row in enumerate(source_rows):
            row = prepared_by_id[str(source_row["id"])]
            source_turn = str(source_row.get("turn_id") or "").strip()
            if ordinal == 0 or source_turn != previous_source_turn:
                active_turn = source_turn
            previous_source_turn = source_turn
            if not row["_deleted"] and row["_starts_new_turn"]:
                active_turn = "turn-" + hashlib.sha256(
                    json.dumps(
                        [
                            "canonical-passage-review",
                            source.id,
                            source.content_hash,
                            row["id"],
                            ordinal,
                        ],
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:24]
            if active_turn:
                row["turn_id"] = active_turn
            else:
                row.pop("turn_id", None)
            prepared.append(row)
            if row["_deleted"]:
                continue
            passage_id = str(row["id"])
            finalized = finalize_segments(
                [
                    SubtitleSegment(
                        index=1,
                        start_ms=row["start_ms"],
                        end_ms=row["end_ms"],
                        text=row["text"],
                        speaker=str(row.get("speaker") or ""),
                    )
                ],
                finalization,
            )
            if not finalized:
                raise ValueError(
                    f"Passage {passage_id} produced no display cues during composition."
                )
            for cue in finalized:
                start = max(row["start_ms"], int(cue.start_ms))
                end = min(row["end_ms"], int(cue.end_ms))
                if end <= start:
                    raise ValueError(
                        f"Passage {passage_id} produced invalid display timing."
                    )
                display_values.append(
                    {
                        "_canonical_passage_id": passage_id,
                        "source_passage_ids": [passage_id],
                        "turn_id": row.get("turn_id"),
                        "starts_new_turn": False,
                        "start_ms": start,
                        "end_ms": end,
                        "text": cue.text,
                        "speaker": str(row.get("speaker") or ""),
                        "review_state": row.get("review_state", "clear"),
                        "review_note": row.get("review_note", ""),
                        "evidence_ids": list(row.get("evidence_ids") or []),
                        "uncertain_source_cue_ids": list(
                            row.get("uncertain_source_cue_ids") or []
                        ),
                    }
                )
        return prepared, display_values, composition_settings

    def save_review_in_session(
        self,
        session,
        session_id: str,
        stage: str,
        expected_revision: int,
        values: list[dict[str, Any]],
        *,
        dry_run: bool = False,
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
            dry_run=dry_run,
            source_artifact_id=source_artifact_id,
            expected_source_hash=expected_source_hash,
            db_session=session,
            published_paths=published_paths,
        )



class _SessionContext:
    """Adapt an existing SQLAlchemy session to a no-commit context manager."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, _exc_type, _exc, _traceback):
        return False
