"""Adopt managed subtitle assets without a second upload or destructive text edits."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

from .artifacts import ArtifactService, sha256_file
from .models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Segment,
    SessionRecord,
)

_SUBTITLE_KINDS = {"srt", "vtt"}
_VTT_TIMESTAMP = re.compile(r"^(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})$")


def parse_subtitle_source(content: str, kind: str) -> list:
    """Use the shared SRT parser, also accepting ordinary timed WebVTT cues.

    Do not quietly import a partial file: malformed timed cues are an error.
    WebVTT positioning/style metadata is not part of the authoritative cue text.
    """
    content = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if kind == "vtt":
        blocks = []
        for block in re.split(r"\n\s*\n+", content.strip()):
            lines = block.splitlines()
            if not lines or lines[0].strip().split(" ", 1)[0] in {"NOTE", "STYLE", "REGION"}:
                continue
            timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
            if timing_index is None:
                if block.strip().startswith("WEBVTT") or not block.strip():
                    continue
                raise ValueError("WebVTT contains a cue without readable timing.")
            left, right = lines[timing_index].split("-->", 1)
            timestamps = []
            for value in (left.strip(), right.strip().split()[0]):
                match = _VTT_TIMESTAMP.fullmatch(value)
                if match is None:
                    raise ValueError("WebVTT contains an invalid cue timestamp.")
                hours, minutes, seconds, milliseconds = match.groups()
                timestamps.append(f"{hours or '00'}:{minutes}:{seconds},{milliseconds}")
            body = "\n".join(lines[timing_index + 1:]).strip()
            # Preserve a WebVTT voice annotation as an explicit speaker label.
            body = re.sub(r"^<v(?:\.[^\s>]+)?\s+([^>]+)>(.*?)(?:</v>)?$", r"[\1] \2", body, flags=re.DOTALL)
            blocks.append(f"{len(blocks) + 1}\n{' --> '.join(timestamps)}\n{body}")
        content = "\n\n".join(blocks)
    cues = parse_srt(content, infer_speakers=False)
    timed_count = sum("-->" in line for line in content.splitlines())
    if not cues or len(cues) != timed_count:
        raise ValueError("The subtitle source contains empty or invalid timed cues; correct it before importing.")
    if len(cues) > 100_000:
        raise ValueError("Subtitle sources are limited to 100,000 cues.")
    return cues


def adopt_subtitle_source_in_session(
    session: Session,
    artifacts: ArtifactService,
    session_id: str,
    source_artifact_id: str,
) -> dict[str, Any]:
    """Register one transcription revision, idempotently, in the caller transaction.

    The source stays a library asset. A canonical session-local SRT is a derived
    artifact, with explicit parent/hash provenance. Re-adoption never reselects an
    older revision or invalidates later correction/translation work.
    """
    record = session.get(SessionRecord, session_id)
    source = session.get(Artifact, source_artifact_id)
    if record is None or source is None or source.state == "deleted":
        raise KeyError(session_id if record is None else source_artifact_id)
    path = artifacts.paths.managed_path(source.relative_path)
    kind = path.suffix.lower().lstrip(".")
    if kind not in _SUBTITLE_KINDS:
        kind = str(source.kind or "").lower()
    if kind not in _SUBTITLE_KINDS:
        raise ValueError("Only managed SRT or VTT sources can be adopted as subtitles.")
    if not path.is_file():
        raise ValueError("The managed subtitle source is no longer available.")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Subtitle source files must be 32 MiB or smaller.")
    digest = sha256_file(path)
    existing = session.scalar(
        select(Artifact).where(
            Artifact.session_id == session_id,
            Artifact.role == "transcription",
            Artifact.state != "deleted",
            Artifact.metadata_json["imported_source_hash"].as_string() == digest,
        ).order_by(Artifact.created_at.asc())
    )
    if existing is not None:
        metadata = dict(existing.metadata_json or {})
        revision = session.get(DocumentRevision, metadata.get("revision_id"))
        if revision is not None:
            edge = session.scalar(select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == source.id,
                ArtifactEdge.child_artifact_id == existing.id,
            ))
            if edge is None:
                session.add(ArtifactEdge(parent_artifact_id=source.id, child_artifact_id=existing.id))
                session.flush()
            return {
                "artifact_id": existing.id,
                "document_id": metadata.get("document_id"),
                "revision_id": revision.id,
                "revision": revision.revision_number,
                "source_artifact_id": source.id,
                "imported_cues": metadata.get("imported_cues", 0),
                "reused": True,
            }
    cues = parse_subtitle_source(path.read_text(encoding="utf-8-sig"), kind)
    content = compose_srt(cues)
    # An immutable content-addressed derivative avoids duplicate uploads, makes
    # interrupted imports retryable and never overwrites a user's reviewed file.
    destination = artifacts.paths.managed_path(f"sessions/{session_id}/imported-subtitles/{digest}.srt")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_text(encoding="utf-8") != content:
            raise ValueError("The existing imported subtitle derivative has changed.")
    else:
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(content)
    document = Document(
        session_id=session_id,
        stage="transcription",
        language=None if record.source_language == "auto" else record.source_language,
    )
    session.add(document)
    session.flush()
    revision = DocumentRevision(
        document_id=document.id,
        revision_number=1,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        reviewed=False,
    )
    session.add(revision)
    session.flush()
    for ordinal, cue in enumerate(cues):
        session.add(Segment(
            revision_id=revision.id,
            ordinal=ordinal,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=cue.text,
            speaker=cue.speaker or None,
            metadata_json={
                "text_origin": "imported_subtitles",
                "timing_origin": "imported_subtitles",
                "source_artifact_id": source.id,
                "source_cue_id": cue.index,
            },
        ))
    document.active_revision_id = revision.id
    metadata = {
        "document_id": document.id,
        "revision_id": revision.id,
        "stage": "transcription",
        "language": document.language,
        "text_origin": "imported_subtitles",
        "timing_origin": "imported_subtitles",
        "imported_source_artifact_id": source.id,
        "imported_source_hash": digest,
        "imported_cues": len(cues),
        "has_speaker_metadata": any(cue.speaker for cue in cues),
    }
    artifact = artifacts.register_in_session(
        session, destination, kind="srt", role="transcription",
        session_id=session_id, parent_ids=[source.id], metadata=metadata,
        settings={"imported_source_hash": digest},
    )
    return {
        "artifact_id": artifact.id,
        "document_id": document.id,
        "revision_id": revision.id,
        "revision": 1,
        "source_artifact_id": source.id,
        "imported_cues": len(cues),
        "reused": False,
    }


def subtitle_source_status_in_session(session: Session, session_id: str) -> dict[str, Any]:
    """Read-only subtitle-first readiness, shared by the UI and MCP planner."""
    from .source_resolution import resolve_media_source, resolve_primary_source

    record = session.get(SessionRecord, session_id)
    if record is None:
        raise KeyError(session_id)
    primary = resolve_primary_source(session, session_id)
    media = resolve_media_source(session, session_id)
    supported = primary.profile == "subtitles" and (
        str(primary.kind).lower().lstrip(".") in _SUBTITLE_KINDS
        or primary.name.lower().endswith((".srt", ".vtt"))
    )
    revision = None
    imported = None
    if supported and primary.artifact:
        imported = session.scalar(select(Artifact).where(
            Artifact.session_id == session_id, Artifact.role == "transcription", Artifact.state != "deleted",
            Artifact.metadata_json["imported_source_hash"].as_string() == primary.artifact.content_hash,
        ).order_by(Artifact.created_at.desc()).limit(1))
        if imported:
            revision = session.get(DocumentRevision, (imported.metadata_json or {}).get("revision_id"))
    aligned = session.scalar(select(Artifact).where(
        Artifact.session_id == session_id, Artifact.role == "transcription", Artifact.state == "current",
        Artifact.metadata_json["authoritative_transcript_artifact_id"].as_string() == (imported.id if imported else ""),
        Artifact.metadata_json["source_artifact_id"].as_string() == (media.artifact.id if media.artifact else ""),
    ).order_by(Artifact.created_at.desc()).limit(1)) if imported else None
    word_timing_id = (aligned.metadata_json or {}).get("aligned_word_timestamps_artifact_id") if aligned else None
    word_timing = session.get(Artifact, word_timing_id) if word_timing_id else None
    if word_timing is None or word_timing.state != "current":
        word_timing_id = None
    return {
        "supported": supported,
        "session_revision": record.revision,
        "source_asset_id": primary.source_asset.id if primary.source_asset else None,
        "source_artifact_id": primary.artifact.id if primary.artifact else None,
        "filename": primary.name,
        "subtitle_artifact_id": imported.id if imported else None,
        "subtitle_revision_id": revision.id if revision else None,
        "cue_count": (imported.metadata_json or {}).get("imported_cues", 0) if imported else 0,
        "adoption_required": supported and revision is None,
        "media_artifact_id": media.artifact.id if media.artifact and media.has_audio else None,
        "media_filename": media.name if media.has_audio else None,
        "has_video": media.has_video,
        "can_align": supported and media.has_audio,
        "word_timing_artifact_id": word_timing_id,
        "alignment_note": None if media.has_audio else "Attach the original audio or video to align words. Subtitle editing and audio-only generation do not require media.",
    }
