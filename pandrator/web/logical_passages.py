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
from pandrator.logic.dubbing.source_passage_settings import (
    SOURCE_PASSAGE_POLICY_VERSION,
    effective_source_passage_settings,
    normalize_source_passage_settings,
    source_passage_settings_hash,
    to_build_kwargs,
)
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
# Raw source roles eligible for passage pinning, preview, and explicit rebuild.
# Derived correction/translation artifacts are never rebuilt in place.
RAW_SOURCE_PASSAGE_ROLES = ("transcription", "media_edit_subtitles")
# Bounded preview payload so large sources stay cheap over HTTP.
PREVIEW_ITEM_LIMIT = 50


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
    source_passage_settings: dict[str, Any] | None = None,
    reuse_stored: bool = True,
) -> list[dict[str, Any]]:
    """Prefer preserved passages; otherwise use existing source-word evidence.

    Read-only: never pins or rebuilds. When no bound packet exists (or
    ``reuse_stored`` is false, as used by preview/rebuild paths that must
    construct from candidate settings rather than return the existing
    ledger), fresh rows are built with ``source_passage_settings`` when
    supplied, else the shared defaults. Callers on write paths should use
    :func:`pin_raw_source_passages` so the first construction is pinned in
    their transaction.
    """
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
    passages = stored_passages(artifact) if reuse_stored else None
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
        build_kwargs = {"language_code": language or ""}
        if source_passage_settings is not None:
            build_kwargs.update(
                {
                    key: value
                    for key, value in to_build_kwargs(
                        source_passage_settings,
                        language_code=language or "",
                    ).items()
                    if key != "language_code"
                }
            )
        passages = build_source_passages(cues, words, **build_kwargs)
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
    source_passage_settings: dict[str, Any] | None = None,
    source_passage_settings_revision: int | None = None,
    policy_version: str | None = None,
) -> None:
    if not _valid_rows(rows):
        raise ValueError("Cannot store invalid logical passages.")
    metadata = dict(artifact.metadata_json or {})
    if not metadata.get("revision_id") or not artifact.content_hash:
        raise ValueError("Logical passages require a materialized display artifact.")
    effective = normalize_source_passage_settings(
        source_passage_settings if source_passage_settings is not None else {}
    )
    metadata["logical_passages"] = {
        "schema_version": PASSAGE_VERSION,
        "display_revision_id": metadata["revision_id"],
        "display_content_hash": artifact.content_hash,
        "source_artifact_id": source.id,
        "source_revision_id": (source.metadata_json or {}).get("revision_id"),
        "policy_version": policy_version or SOURCE_PASSAGE_POLICY_VERSION,
        "source_passage_settings": effective,
        "source_passage_settings_hash": source_passage_settings_hash(effective),
        "source_passage_settings_revision": (
            None
            if source_passage_settings_revision is None
            else int(source_passage_settings_revision)
        ),
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


class PassageRevisionConflict(ValueError):
    """Optimistic-concurrency guard for passage preview/rebuild requests."""


class PassageIneligibleSource(ValueError):
    """The selected artifact is not a raw source eligible for passages."""


def source_passage_effective_snapshot(
    session: Session,
    session_id: str,
) -> dict[str, Any]:
    """Read the live `source_passages` effective settings (read-only)."""
    from .workspace import WorkspaceSettingsService

    fetched = WorkspaceSettingsService.get_in_session(
        WorkspaceSettingsService.__new__(WorkspaceSettingsService),
        session,
        session_id,
        "source_passages",
    )
    effective = normalize_source_passage_settings(fetched["effective"])
    return {
        "effective": effective,
        "revision": int(fetched.get("revision") or 0),
        "global_revision": int(fetched.get("global_revision") or 0),
        "settings_hash": source_passage_settings_hash(effective),
        "policy_version": SOURCE_PASSAGE_POLICY_VERSION,
    }


def _require_raw_source(session: Session, session_id: str, artifact_id: str) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or artifact.session_id != session_id:
        raise KeyError(artifact_id)
    if artifact.state == "deleted":
        raise PassageIneligibleSource("The source artifact was deleted.")
    if artifact.role not in RAW_SOURCE_PASSAGE_ROLES:
        raise PassageIneligibleSource(
            "Only raw transcription or media-edit subtitle sources can have "
            "passages pinned or rebuilt. Adopt the subtitle file first."
        )
    return artifact


def _source_document_revision(
    session: Session, artifact: Artifact
) -> tuple[Document, DocumentRevision, list[Segment]]:
    revision_id = str((artifact.metadata_json or {}).get("revision_id") or "")
    if not revision_id:
        raise PassageIneligibleSource(
            "The source artifact has no materialized subtitle revision."
        )
    revision = session.get(DocumentRevision, revision_id)
    if revision is None:
        raise PassageIneligibleSource("The source revision was not found.")
    document = session.get(Document, revision.document_id)
    if (
        document is None
        or document.session_id != artifact.session_id
        or document.stage != artifact.role
    ):
        raise PassageIneligibleSource(
            "The source artifact and revision stages do not match."
        )
    segments = list(
        session.scalars(
            select(Segment)
            .where(Segment.revision_id == revision.id)
            .order_by(Segment.ordinal)
        ).all()
    )
    return document, revision, segments


def pin_raw_source_passages(
    session: Session,
    artifact: Artifact,
    *,
    effective: dict[str, Any] | None = None,
    settings_revision: int | None = None,
    segments: list[Segment] | None = None,
) -> list[dict[str, Any]]:
    """Pin first-use raw passages on the artifact inside the caller txn.

    Returns stored rows unchanged when a bound packet already exists, so
    settings/default changes never rebuild them. Otherwise builds with the
    supplied effective settings (or shared defaults) and attaches the packet
    with policy version and settings provenance. Never touches downstream
    correction/translation ledgers, plans, or takes.
    """
    existing = stored_passages(artifact)
    if existing is not None:
        return existing
    if artifact.role not in RAW_SOURCE_PASSAGE_ROLES:
        return source_passages(
            session, artifact, segments=segments, source_passage_settings=effective
        )
    rows = source_passages(
        session, artifact, segments=segments, source_passage_settings=effective
    )
    if not rows:
        # Legacy imported subtitles may not yet have a materialized revision.
        # Preserve the existing file-based processing fallback without pinning
        # an empty packet or manufacturing source timing evidence.
        return []
    attach_passages(
        artifact,
        rows,
        source=artifact,
        source_passage_settings=effective,
        source_passage_settings_revision=settings_revision,
    )
    return rows


def passage_status(
    session: Session,
    session_id: str,
    artifact_id: str,
) -> dict[str, Any]:
    """Read-only passage pin state plus the live effective settings."""
    artifact = _require_raw_source(session, session_id, artifact_id)
    revision_id = str((artifact.metadata_json or {}).get("revision_id") or "")
    snapshot = source_passage_effective_snapshot(session, session_id)
    packet = (artifact.metadata_json or {}).get("logical_passages")
    pinned = stored_passages(artifact) is not None
    return {
        "artifact_id": artifact.id,
        "revision_id": revision_id or None,
        "content_hash": artifact.content_hash,
        "pinned": pinned,
        "policy_version": (
            str((packet or {}).get("policy_version") or "")
            if pinned
            else SOURCE_PASSAGE_POLICY_VERSION
        ),
        "source_passage_settings": (
            deepcopy((packet or {}).get("source_passage_settings"))
            if pinned
            else snapshot["effective"]
        ),
        "source_passage_settings_hash": (
            str((packet or {}).get("source_passage_settings_hash") or "")
            if pinned
            else snapshot["settings_hash"]
        ),
        "source_passage_settings_revision": (
            (packet or {}).get("source_passage_settings_revision")
            if pinned
            else snapshot["revision"]
        ),
        "passage_count": (
            len((packet or {}).get("items") or []) if pinned else None
        ),
        "display_content_hash": (
            str((packet or {}).get("display_content_hash") or "")
            if pinned
            else None
        ),
        "live_effective_settings": snapshot["effective"],
        "live_settings_hash": snapshot["settings_hash"],
        "live_settings_revision": snapshot["revision"],
    }


def preview_source_passages(
    session: Session,
    session_id: str,
    artifact_id: str,
    *,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, read-only preview without mutating any artifact."""
    artifact = _require_raw_source(session, session_id, artifact_id)
    _document, _revision, segments = _source_document_revision(session, artifact)
    snapshot = source_passage_effective_snapshot(session, session_id)
    candidate = effective_source_passage_settings(
        snapshot["effective"], override or {}
    )
    rows = source_passages(
        session,
        artifact,
        segments=segments,
        source_passage_settings=candidate,
        reuse_stored=False,
    )
    settings_hash = source_passage_settings_hash(candidate)
    return {
        "artifact_id": artifact.id,
        "revision_id": str((artifact.metadata_json or {}).get("revision_id") or ""),
        "content_hash": artifact.content_hash,
        "policy_version": SOURCE_PASSAGE_POLICY_VERSION,
        "effective_settings": candidate,
        "settings_hash": settings_hash,
        "settings_revision": snapshot["revision"],
        "passage_count": len(rows),
        "items": deepcopy(rows[:PREVIEW_ITEM_LIMIT]),
        "truncated": len(rows) > PREVIEW_ITEM_LIMIT,
        "pinned": stored_passages(artifact) is not None,
        "warnings": (
            ["Stored passages remain unchanged; this preview built no writes."]
            if stored_passages(artifact) is not None
            else []
        ),
    }


def rebuild_source_passages_branch(
    session: Session,
    session_id: str,
    artifact_id: str,
    *,
    expected_source_revision_id: str,
    expected_source_content_hash: str,
    expected_settings_revision: int,
    expected_settings_hash: str,
    override: dict[str, Any] | None = None,
    write_artifact_file=None,
) -> dict[str, Any]:
    """Create a NEW selectable source branch with rebuilt passages.

    Guard order is load-bearing: ownership/role/state, then expected source
    revision/hash, then expected settings revision/hash are all verified
    BEFORE building anything, so a stale request never mutates the original.
    The original artifact, its revision, and all downstream artifacts,
    selections, plans, and takes are preserved; the branch is registered with
    ``state="current"`` but is never auto-selected.
    """

    def _fail_conflict(message: str) -> PassageRevisionConflict:
        return PassageRevisionConflict(message)

    # 1. Ownership, role, state. No writes before every stale check passes.
    artifact = _require_raw_source(session, session_id, artifact_id)
    live_revision_id = str((artifact.metadata_json or {}).get("revision_id") or "")
    live_content_hash = str(artifact.content_hash or "")
    if (
        not expected_source_revision_id
        or expected_source_revision_id != live_revision_id
    ):
        raise _fail_conflict(
            "The source revision changed; refresh passage status before rebuilding."
        )
    if (
        not expected_source_content_hash
        or expected_source_content_hash != live_content_hash
    ):
        raise _fail_conflict(
            "The source content changed; refresh passage status before rebuilding."
        )
    snapshot = source_passage_effective_snapshot(session, session_id)
    try:
        live_revision = int(snapshot["revision"])
        wanted_revision = int(expected_settings_revision)
    except (TypeError, ValueError) as error:
        raise PassageRevisionConflict(
            "A current settings revision is required to rebuild."
        ) from error
    if wanted_revision != live_revision:
        raise _fail_conflict(
            "Source-passage settings changed; refresh preview before rebuilding."
        )
    candidate = effective_source_passage_settings(
        snapshot["effective"], override or {}
    )
    candidate_hash = source_passage_settings_hash(candidate)
    if not expected_settings_hash or expected_settings_hash != candidate_hash:
        raise _fail_conflict(
            "Source-passage settings changed (including global defaults); "
            "refresh preview before rebuilding."
        )

    # 2. Gather inputs directly (never via a pinning path) and build.
    document, revision, segments = _source_document_revision(session, artifact)
    if revision.id != live_revision_id:
        raise _fail_conflict(
            "The source revision changed; refresh passage status before rebuilding."
        )
    rows = source_passages(
        session,
        artifact,
        segments=segments,
        source_passage_settings=candidate,
        reuse_stored=False,
    )
    if not _valid_rows(rows):
        raise ValueError("The rebuilt passages are invalid.")

    # 3. New branch: new Document + revision + copied segments/words + artifact.
    record = session.get(SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    branch_document = Document(
        session_id=session_id,
        stage=document.stage,
        language=document.language,
    )
    session.add(branch_document)
    session.flush()
    branch_revision = DocumentRevision(
        document_id=branch_document.id,
        parent_revision_id=revision.id,
        revision_number=1,
        content_hash=_hash(
            [
                {
                    "ordinal": item.ordinal,
                    "text": item.text,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "speaker": item.speaker,
                }
                for item in segments
            ]
        ),
        reviewed=False,
        settings_hash=candidate_hash,
    )
    session.add(branch_revision)
    session.flush()
    old_to_new_segment: dict[str, str] = {}
    for item in segments:
        copied = Segment(
            revision_id=branch_revision.id,
            ordinal=item.ordinal,
            node_kind=item.node_kind,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            text=item.text,
            speaker=item.speaker,
            metadata_json={
                **(dict(item.metadata_json or {})),
                "passage_branch_of_revision_id": revision.id,
            },
        )
        session.add(copied)
        session.flush()
        old_to_new_segment[item.id] = copied.id
    referenced_word_ids = {
        word_id for row in rows for word_id in row.get("source_word_ids", [])
    }
    old_to_new_word: dict[str, str] = {}
    if referenced_word_ids:
        for word in session.scalars(
            select(TimedWord).where(
                TimedWord.revision_id == revision.id,
                TimedWord.id.in_(sorted(referenced_word_ids)),
            )
        ).all():
            copied_word = TimedWord(
                revision_id=branch_revision.id,
                segment_id=old_to_new_segment.get(word.segment_id or ""),
                ordinal=word.ordinal,
                text=word.text,
                start_ms=word.start_ms,
                end_ms=word.end_ms,
                speaker=word.speaker,
                confidence=word.confidence,
                metadata_json={
                    **(dict(word.metadata_json or {})),
                    "passage_branch_of_revision_id": revision.id,
                },
            )
            session.add(copied_word)
            session.flush()
            old_to_new_word[word.id] = copied_word.id
        session.flush()
    # Remap branch rows onto the new segment/word identities so the pinned
    # ledger references evidence that actually exists in the branch revision.
    # Original identities are preserved explicitly in branch_ancestry.
    remapped_rows = []
    for row in rows:
        rebuilt = deepcopy(row)
        rebuilt["source_cue_ids"] = [
            old_to_new_segment.get(value, value)
            for value in row.get("source_cue_ids", [])
        ]
        rebuilt["source_word_ids"] = [
            old_to_new_word.get(value, value)
            for value in row.get("source_word_ids", [])
        ]
        rebuilt["source_unit_ids"] = list(row.get("source_unit_ids", []))
        if isinstance(row.get("source_token_ranges"), list):
            rebuilt["source_token_ranges"] = [
                {
                    **entry,
                    "source_cue_id": old_to_new_segment.get(
                        entry.get("source_cue_id"), entry.get("source_cue_id")
                    ),
                }
                if isinstance(entry, dict)
                else entry
                for entry in row["source_token_ranges"]
            ]
        remapped_rows.append(rebuilt)
    rows = remapped_rows
    branch_document.active_revision_id = branch_revision.id
    session.flush()

    srt_text = passage_srt(
        [
            {
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "text": item.text,
                "speaker": item.speaker or "",
            }
            for item in segments
        ]
    )
    digest = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    # Unique managed path per branch (branch document id): no digest-based
    # sharing, so branches can never collide on one file.
    relative_path = (
        f"sessions/{session_id}/passage-branches/{branch_document.id}.srt"
    )
    if write_artifact_file is None:
        raise ValueError("An artifact file writer is required to branch passages.")
    write_artifact_file(relative_path, srt_text)
    branch = Artifact(
        session_id=session_id,
        kind="srt",
        role=artifact.role,
        relative_path=relative_path,
        mime_type="application/x-subrip",
        size_bytes=len(srt_text.encode("utf-8")),
        content_hash=digest,
        settings_hash=candidate_hash,
        # Non-current by design: the legacy current-artifact fallback and
        # newest-current consumers keep resolving the original until the new
        # branch is explicitly selected. choose_artifact accepts any
        # non-deleted artifact, so the branch stays selectable.
        state="stale",
        metadata_json={
            "document_id": branch_document.id,
            "revision_id": branch_revision.id,
            "stage": document.stage,
            "language": document.language,
            "passage_branch_of_artifact_id": artifact.id,
            "passage_branch_of_revision_id": revision.id,
        },
    )
    session.add(branch)
    session.flush()
    session.add(
        ArtifactEdge(
            parent_artifact_id=artifact.id,
            child_artifact_id=branch.id,
            relation="passage_rebuild",
        )
    )
    session.flush()
    attach_passages(
        branch,
        rows,
        source=artifact,
        source_passage_settings=candidate,
        source_passage_settings_revision=snapshot["revision"],
    )
    packet = dict(branch.metadata_json.get("logical_passages") or {})
    packet["branch_ancestry"] = {
        "segment_ids": dict(old_to_new_segment),
        "word_ids": dict(old_to_new_word),
        "source_artifact_id": artifact.id,
        "source_revision_id": revision.id,
    }
    metadata = dict(branch.metadata_json or {})
    metadata["logical_passages"] = packet
    branch.metadata_json = metadata
    session.flush()
    return {
        "branch_artifact_id": branch.id,
        "branch_revision_id": branch_revision.id,
        "branch_document_id": branch_document.id,
        "branch_state": branch.state,
        "passage_count": len(rows),
        "policy_version": SOURCE_PASSAGE_POLICY_VERSION,
        "effective_settings": candidate,
        "settings_hash": candidate_hash,
        "settings_revision": snapshot["revision"],
        "preserved": {
            "original_artifact_id": artifact.id,
            "original_revision_id": revision.id,
            "downstream_untouched": True,
            "selection_unchanged": True,
        },
    }
