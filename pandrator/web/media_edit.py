"""Revisioned media-edit preparation and manual timeline updates."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.logic.media_edit import (
    DEFAULT_ALIGNMENT_PADDING_MS,
    MAX_ALIGNMENT_WORD_SPAN_MS,
    BoundaryEvidence,
    KeepRange,
    MediaCue,
    MediaWord,
    align_cues_to_words,
    keep_ranges_from_cuts,
    normalize_keep_ranges,
    parse_caption_text,
    refine_boundary,
)

from .artifacts import ArtifactService
from .database import Database
from .media_process import probe_audio_stream
from .models import (
    Artifact,
    ArtifactEdge,
    MediaEditPlan,
    MediaEditPlanRevision,
    SessionRecord,
    SessionSource,
    SourceAsset,
    utcnow,
)
from .source_resolution import classify_source, resolve_primary_source

_BOUNDARY_MAX_CUE_TEXT_CHARS = 4_000
_BOUNDARY_MAX_SPEAKER_CHARS = 500
_BOUNDARY_MAX_WORDS = 400
_BOUNDARY_MAX_WORD_TEXT_CHARS = 256
_BOUNDARY_MAX_GAPS = 200
_CUT_MAX_REASONS = 8


class MediaEditRevisionConflict(RuntimeError):
    """The caller attempted to update a revision which is no longer active."""

    def __init__(self, expected: int, current: int | None):
        self.expected = expected
        self.current = current
        current_text = "none" if current is None else str(current)
        super().__init__(
            f"Media-edit revision conflict: expected {expected}, current is {current_text}."
        )


class MediaEditInputsChanged(RuntimeError):
    """The prepared media inputs changed before the snapshot could be stored."""

    def __init__(self) -> None:
        super().__init__(
            "Media-edit inputs changed while preparing; retry preparation."
        )


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshot:
    id: str
    content_hash: str | None
    relative_path: str
    metadata: dict[str, Any]

    @classmethod
    def from_artifact(cls, artifact: Artifact) -> _ArtifactSnapshot:
        return cls(
            id=artifact.id,
            content_hash=artifact.content_hash,
            relative_path=artifact.relative_path,
            metadata=dict(artifact.metadata_json or {}),
        )


@dataclass(frozen=True, slots=True)
class _MediaEditInputSnapshot:
    session_id: str
    primary: _ArtifactSnapshot
    editorial: _ArtifactSnapshot
    timing: _ArtifactSnapshot | None
    external_editorial: bool


class MediaEditService:
    def __init__(
        self,
        database: Database,
        artifacts: ArtifactService,
        session_directory: Callable[[str], Path],
        duration_probe: Callable[[Path], Any] | None = None,
    ):
        self.database = database
        self.artifacts = artifacts
        self.session_directory = session_directory
        self.duration_probe = duration_probe or probe_audio_stream

    @staticmethod
    def _artifact_payload(artifact: Artifact | None) -> dict[str, Any] | None:
        if artifact is None:
            return None
        metadata = dict(artifact.metadata_json or {})
        return {
            "id": artifact.id,
            "kind": artifact.kind,
            "role": artifact.role,
            "mime_type": artifact.mime_type,
            "size_bytes": artifact.size_bytes,
            "content_hash": artifact.content_hash,
            "state": artifact.state,
            "filename": metadata.get("original_filename")
            or Path(artifact.relative_path).name,
            "content_url": f"/api/v1/artifacts/{artifact.id}/content",
            "created_at": artifact.created_at.isoformat(),
        }

    @staticmethod
    def _lookup_current_artifact(
        session, session_id: str, role: str
    ) -> Artifact | None:
        return session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == role,
                Artifact.state == "current",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        )

    @staticmethod
    def _invalidate_rendered_outputs(session, session_id: str) -> None:
        """Make every artifact derived from the previous render unusable.

        The files and provenance edges remain available for inspection, but a
        newly active edit revision must not inherit media, subtitles, corrected
        text, translations, generated audio, or exports from an older cut.
        """

        rendered = list(
            session.scalars(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role.in_(("media_edit_media", "media_edit_subtitles")),
                    Artifact.state == "current",
                )
            ).all()
        )
        for artifact in rendered:
            artifact.state = "stale"
            ArtifactService._mark_descendants_stale(session, artifact.id)

    @staticmethod
    def _lookup_external_transcript(session, session_id: str) -> Artifact | None:
        return session.scalar(
            select(Artifact)
            .join(SourceAsset, SourceAsset.artifact_id == Artifact.id)
            .join(SessionSource, SessionSource.source_asset_id == SourceAsset.id)
            .where(
                SessionSource.session_id == session_id,
                SessionSource.role == "transcript",
                SessionSource.is_current.is_(True),
                SourceAsset.state == "current",
                Artifact.state == "current",
            )
            .order_by(SessionSource.updated_at.desc(), SessionSource.id.desc())
        )

    @staticmethod
    def _is_direct_derivative(session, source_id: str, artifact_id: str) -> bool:
        return session.get(ArtifactEdge, (source_id, artifact_id)) is not None

    @staticmethod
    def _snapshot_artifact(
        artifact: Artifact | None,
        *,
        label: str,
    ) -> _ArtifactSnapshot:
        if artifact is None:
            raise ValueError(f"No current {label} artifact is available.")
        return _ArtifactSnapshot.from_artifact(artifact)

    def _input_snapshot(self, session, session_id: str) -> _MediaEditInputSnapshot:
        record = session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError("session")
        if record.workflow_kind != "media_edit":
            raise ValueError(
                "Media-edit operations require a media_edit workflow session."
            )
        primary = resolve_primary_source(session, session_id).artifact
        if primary is None:
            raise ValueError(
                "Attach a primary source media artifact before preparing an edit."
            )
        primary_metadata = dict(primary.metadata_json or {})
        if (
            classify_source(
                name=str(
                    primary_metadata.get("original_filename") or primary.relative_path
                ),
                kind=str(primary.kind or ""),
                mime_type=str(primary.mime_type or ""),
            )
            != "video"
        ):
            raise ValueError(
                "Transcript-guided media editing currently requires video."
            )
        external = self._lookup_external_transcript(session, session_id)
        transcription = self._lookup_current_artifact(
            session, session_id, "transcription"
        )
        if transcription is not None and not self._is_direct_derivative(
            session, primary.id, transcription.id
        ):
            transcription = None
        editorial = external or transcription
        if editorial is None:
            raise ValueError(
                "Attach captions or run transcription before preparing an edit."
            )
        timing = self._lookup_current_artifact(session, session_id, "word_timestamps")
        if timing is not None and not self._is_direct_derivative(
            session, primary.id, timing.id
        ):
            timing = None
        return _MediaEditInputSnapshot(
            session_id=session_id,
            primary=self._snapshot_artifact(primary, label="primary source media"),
            editorial=self._snapshot_artifact(editorial, label="editorial transcript"),
            timing=(
                _ArtifactSnapshot.from_artifact(timing) if timing is not None else None
            ),
            external_editorial=external is not None,
        )

    def _snapshot_paths(
        self, snapshot: _MediaEditInputSnapshot
    ) -> tuple[Path, Path, Path | None]:
        return (
            self.artifacts.paths.managed_path(snapshot.primary.relative_path),
            self.artifacts.paths.managed_path(snapshot.editorial.relative_path),
            (
                self.artifacts.paths.managed_path(snapshot.timing.relative_path)
                if snapshot.timing is not None
                else None
            ),
        )

    def _resolve_snapshot_artifacts(
        self, session, snapshot: _MediaEditInputSnapshot
    ) -> tuple[Artifact, Artifact, Artifact | None]:
        try:
            current = self._input_snapshot(session, snapshot.session_id)
        except (KeyError, ValueError) as error:
            raise MediaEditInputsChanged() from error
        if current != snapshot:
            raise MediaEditInputsChanged()
        primary = session.get(Artifact, snapshot.primary.id)
        editorial = session.get(Artifact, snapshot.editorial.id)
        timing = session.get(Artifact, snapshot.timing.id) if snapshot.timing else None
        if primary is None or editorial is None:
            raise MediaEditInputsChanged()
        return primary, editorial, timing

    @staticmethod
    def _duration(value: Any) -> int:
        raw = getattr(value, "duration_ms", value)
        try:
            duration_ms = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "The duration probe returned an invalid duration."
            ) from error
        if duration_ms <= 0:
            raise ValueError("The source media duration must be greater than zero.")
        return duration_ms

    @staticmethod
    def _word_payload(word: Any) -> dict[str, Any]:
        return {
            "text": str(getattr(word, "text", "")),
            "start_ms": int(getattr(word, "start_ms", 0)),
            "end_ms": int(getattr(word, "end_ms", 0)),
            "confidence": getattr(word, "confidence", None),
        }

    @classmethod
    def _cue_payload(cls, cue: Any) -> dict[str, Any]:
        return {
            "id": str(getattr(cue, "id", "")),
            "start_ms": int(getattr(cue, "start_ms", 0)),
            "end_ms": int(getattr(cue, "end_ms", 0)),
            "text": str(getattr(cue, "text", "")),
            "speaker": getattr(cue, "speaker", None),
            "words": [cls._word_payload(word) for word in getattr(cue, "words", ())],
            "timing_confidence": getattr(cue, "timing_confidence", None),
            "timing_source": str(getattr(cue, "timing_source", "caption")),
        }

    @staticmethod
    def _required_int(item: dict[str, Any], key: str) -> int:
        value = item.get(key)
        if value is None:
            raise ValueError(f"Media-edit field '{key}' is required.")
        return int(value)

    @staticmethod
    def _keep_range_from_payload(item: dict[str, Any], index: int) -> KeepRange:
        if not isinstance(item, dict):
            raise TypeError("Each keep range must be an object.")
        return KeepRange(
            str(item.get("id") or f"keep-{index:06d}"),
            MediaEditService._required_int(item, "start_ms"),
            MediaEditService._required_int(item, "end_ms"),
            item.get("label"),
        )

    @staticmethod
    def _keep_range_payload(item: Any) -> dict[str, Any]:
        return {
            "id": str(getattr(item, "id", "")),
            "start_ms": int(getattr(item, "start_ms", 0)),
            "end_ms": int(getattr(item, "end_ms", 0)),
            "label": getattr(item, "label", None),
        }

    @staticmethod
    def _content_hash(snapshot: dict[str, Any]) -> str:
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalised_tokens(text: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (surface, normalized)
            for surface in re.findall(r"\S+", text)
            if (
                normalized := "".join(
                    character
                    for character in unicodedata.normalize("NFKC", surface).casefold()
                    if character.isalnum()
                )
            )
        )

    @classmethod
    def _consume_pre_aligned_cues(
        cls,
        cues: list[MediaCue],
        transcript,
    ) -> tuple[MediaCue, ...] | None:
        """Consume cue-owned timing without performing another projection.

        A media-edit alignment artifact is trusted only when every canonical
        segment is identified by the authoritative cue ID, has equivalent
        normalized text, and its words remain plausible evidence for that
        cue's original padded interval.
        """

        segments = tuple(transcript.segments)
        segment_by_id: dict[str, Any] = {}
        for segment in segments:
            identifier = str(getattr(segment, "identifier", "") or "")
            if not identifier or identifier in segment_by_id:
                return None
            segment_by_id[identifier] = segment
        cue_ids = {cue.id for cue in cues}
        if set(segment_by_id) != cue_ids:
            return None

        consumed: list[MediaCue] = []
        for cue in cues:
            segment = segment_by_id[cue.id]
            if cls._normalised_tokens(
                str(segment.text or "")
            ) != cls._normalised_tokens(cue.text):
                return None
            cue_tokens = cls._normalised_tokens(cue.text)
            segment_start = getattr(segment, "start_ms", None)
            segment_end = getattr(segment, "end_ms", None)
            if (
                not isinstance(segment_start, int)
                or isinstance(segment_start, bool)
                or not isinstance(segment_end, int)
                or isinstance(segment_end, bool)
                or segment_end <= segment_start
            ):
                return None
            window_start = max(0, cue.start_ms - DEFAULT_ALIGNMENT_PADDING_MS)
            window_end = cue.end_ms + DEFAULT_ALIGNMENT_PADDING_MS
            if segment_start < window_start or segment_end > window_end:
                return None
            raw_words = tuple(getattr(segment, "words", ()) or ())
            if not raw_words:
                consumed.append(
                    replace(
                        cue,
                        words=(),
                        timing_confidence=0.0,
                        timing_source="caption",
                    )
                )
                continue

            words: list[MediaWord] = []
            word_keys: list[str] = []
            previous_start = -1
            for raw_word in raw_words:
                try:
                    word = MediaWord(
                        text=str(getattr(raw_word, "text", "") or ""),
                        start_ms=int(getattr(raw_word, "start_ms", None)),
                        end_ms=int(getattr(raw_word, "end_ms", None)),
                        confidence=getattr(raw_word, "confidence", None),
                    )
                except (TypeError, ValueError):
                    return None
                if (
                    word.start_ms < window_start
                    or word.end_ms > window_end
                    or word.start_ms < segment_start
                    or word.end_ms > segment_end
                    or word.end_ms - word.start_ms > MAX_ALIGNMENT_WORD_SPAN_MS
                    or word.start_ms < previous_start
                ):
                    return None
                key = cls._normalised_tokens(word.text)
                if not key:
                    return None
                words.append(word)
                word_keys.append(key[0][1])
                previous_start = word.start_ms

            surfaces: list[str] = []
            token_index = 0
            for key in word_keys:
                match_index = next(
                    (
                        index
                        for index in range(token_index, len(cue_tokens))
                        if cue_tokens[index][1] == key
                    ),
                    None,
                )
                if match_index is None:
                    return None
                surfaces.append(cue_tokens[match_index][0])
                token_index = match_index + 1
            lexical_coverage = len(surfaces) / max(1, len(cue_tokens))
            if len(cue_tokens) > 1 and lexical_coverage <= 0.5:
                return None
            aligned_words = tuple(
                replace(word, text=surface)
                for word, surface in zip(words, surfaces, strict=True)
            )
            confidence = lexical_coverage
            segment_metadata = dict(getattr(segment, "metadata", {}) or {})
            try:
                stored_confidence = segment_metadata.get("timing_confidence")
                if stored_confidence is not None:
                    confidence = float(stored_confidence)
            except (TypeError, ValueError):
                return None
            if not 0 <= confidence <= 1:
                return None
            stored_timing_source = str(
                segment_metadata.get("timing_source") or "asr_alignment"
            )
            if stored_timing_source not in {"asr_alignment", "ctc_alignment"}:
                stored_timing_source = "asr_alignment"
            consumed.append(
                replace(
                    cue,
                    start_ms=segment_start,
                    end_ms=segment_end,
                    words=aligned_words,
                    timing_confidence=confidence,
                    timing_source=stored_timing_source,
                )
            )
        return tuple(consumed)

    @staticmethod
    def _token_coverage(cues: list[MediaCue]) -> float:
        total = 0
        matched = 0
        for cue in cues:
            token_count = len(MediaEditService._normalised_tokens(cue.text))
            total += token_count
            confidence = cue.timing_confidence
            if (
                cue.timing_source in {"asr_alignment", "ctc_alignment"}
                and cue.words
                and confidence is not None
            ):
                if cue.timing_source == "ctc_alignment":
                    # Forced alignment returns one validated timing for every
                    # authoritative caption token.  Its confidence field is a
                    # VAD/temporal quality score, not lexical coverage.
                    matched += min(token_count, len(cue.words))
                else:
                    matched += min(
                        token_count,
                        max(0, round(token_count * confidence)),
                    )
        return matched / total if total else 0.0

    @classmethod
    def _revision_snapshot(cls, revision: MediaEditPlanRevision) -> dict[str, Any]:
        return {
            "source_media_artifact_id": revision.source_media_artifact_id,
            "editorial_transcript_artifact_id": revision.editorial_transcript_artifact_id,
            "timing_artifact_id": revision.timing_artifact_id,
            "duration_ms": revision.duration_ms,
            "instructions": revision.instructions,
            "keep_ranges": list(revision.keep_ranges_json or []),
            "cues": list(revision.cues_json or []),
            "evidence": dict(revision.evidence_json or {}),
            "operation": dict(revision.operation_json or {}),
            "reviewed": bool(revision.reviewed),
        }

    @classmethod
    def _payload(
        cls,
        revision: MediaEditPlanRevision,
        artifacts_by_id: dict[str, Artifact | None],
    ) -> dict[str, Any]:
        return {
            "plan_id": revision.plan_id,
            "revision_id": revision.id,
            "revision": revision.revision_number,
            "parent_revision_id": revision.parent_revision_id,
            "source_media_artifact": cls._artifact_payload(
                artifacts_by_id.get(revision.source_media_artifact_id)
            ),
            "editorial_transcript_artifact": cls._artifact_payload(
                artifacts_by_id.get(revision.editorial_transcript_artifact_id)
            ),
            "timing_artifact": cls._artifact_payload(
                artifacts_by_id.get(revision.timing_artifact_id)
            )
            if revision.timing_artifact_id
            else None,
            "duration_ms": revision.duration_ms,
            "instructions": revision.instructions,
            "keep_ranges": list(revision.keep_ranges_json or []),
            "cues": list(revision.cues_json or []),
            "evidence": dict(revision.evidence_json or {}),
            "operation": dict(revision.operation_json or {}),
            "reviewed": bool(revision.reviewed),
            "content_hash": revision.content_hash,
            "created_at": revision.created_at.isoformat(),
        }

    def _state_in_session(self, session, session_id: str) -> dict[str, Any]:
        record = session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError("session")
        if record.workflow_kind != "media_edit":
            raise ValueError(
                "Media-edit operations require a media_edit workflow session."
            )
        primary = resolve_primary_source(session, session_id).artifact
        external = self._lookup_external_transcript(session, session_id)
        transcription = self._lookup_current_artifact(
            session, session_id, "transcription"
        )
        timing = self._lookup_current_artifact(session, session_id, "word_timestamps")
        if primary is not None:
            if transcription is not None and not self._is_direct_derivative(
                session, primary.id, transcription.id
            ):
                transcription = None
            if timing is not None and not self._is_direct_derivative(
                session, primary.id, timing.id
            ):
                timing = None
        plan = session.scalar(
            select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
        )
        active = (
            session.get(MediaEditPlanRevision, plan.active_revision_id)
            if plan and plan.active_revision_id
            else None
        )
        artifact_ids: set[str] = set()
        if active is not None:
            artifact_ids.update(
                item
                for item in (
                    active.source_media_artifact_id,
                    active.editorial_transcript_artifact_id,
                    active.timing_artifact_id,
                )
                if item
            )
        artifacts_by_id = {
            artifact.id: artifact
            for artifact in session.scalars(
                select(Artifact).where(Artifact.id.in_(artifact_ids))
            ).all()
        }
        ready = primary is not None and (
            external is not None or transcription is not None
        )
        readiness = {
            "ready": ready,
            "source_media_artifact": self._artifact_payload(primary),
            "external_transcript_artifact": self._artifact_payload(external),
            "transcription_artifact": self._artifact_payload(transcription),
            "timing_artifact": self._artifact_payload(timing),
        }
        return {
            "session_id": session_id,
            "workflow_kind": record.workflow_kind,
            "readiness": readiness,
            "plan": self._payload(active, artifacts_by_id) if active else None,
        }

    def state(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            return self._state_in_session(session, session_id)

    def _parse_editorial(
        self,
        editorial_path: Path,
        timing_path: Path | None,
        duration_ms: int,
        *,
        external: bool,
        timing_metadata: dict[str, Any] | None = None,
        authoritative_artifact_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        text = editorial_path.read_text(encoding="utf-8-sig")
        cues = parse_caption_text(text)
        warnings: list[str] = []
        timing_words: list[MediaWord] = []
        reused_alignment = False
        if timing_path is not None:
            try:
                transcript = load_transcript(timing_path)
                metadata = dict(timing_metadata or {})
                is_alignment_artifact = bool(metadata.get("alignment_method"))
                same_authoritative_source = is_alignment_artifact and str(
                    metadata.get("authoritative_transcript_artifact_id") or ""
                ) == str(authoritative_artifact_id or "")
                if is_alignment_artifact and not same_authoritative_source:
                    aligned = cues
                    warnings.append(
                        "The pre-aligned word-timing artifact does not match the "
                        "current authoritative transcript; caption timing was preserved."
                    )
                elif same_authoritative_source:
                    try:
                        stored_coverage = float(
                            metadata.get(
                                "alignment_coverage",
                                metadata.get("coverage"),
                            )
                        )
                    except (TypeError, ValueError):
                        stored_coverage = 0.0
                    if stored_coverage >= 0.5:
                        direct = self._consume_pre_aligned_cues(cues, transcript)
                        if (
                            direct is not None
                            and self._token_coverage(list(direct)) >= 0.5
                        ):
                            aligned = direct
                            reused_alignment = True
                        else:
                            aligned = cues
                            warnings.append(
                                "The pre-aligned word-timing artifact failed its "
                                "cue provenance or timing checks; caption timing was preserved."
                            )
                    else:
                        aligned = cues
                        warnings.append(
                            "The pre-aligned word-timing artifact has coverage below "
                            "0.5; caption timing was preserved."
                        )
                else:
                    timing_words = [
                        MediaWord(
                            text=word.text,
                            start_ms=word.start_ms,
                            end_ms=word.end_ms,
                            confidence=word.confidence,
                        )
                        for word in transcript.words
                    ]
                    aligned = align_cues_to_words(cues, timing_words)
            except (OSError, ValueError, TypeError):
                timing_words = []
                warnings.append(
                    "The word-timing artifact could not be used for alignment."
                )
                aligned = cues
        else:
            aligned = cues
        cue_payloads = [self._cue_payload(cue) for cue in aligned]
        coverage = self._token_coverage(aligned)
        confidence_values = [float(cue.timing_confidence or 0.0) for cue in aligned]
        if (
            external
            and not timing_words
            and max(int(getattr(cue, "end_ms", 0)) for cue in cues) > duration_ms
        ):
            warnings.append(
                "External transcript timing extends beyond the probed media duration; "
                "caption spans were preserved without clipping."
            )
        if external and not timing_words and not reused_alignment:
            warnings.append(
                "No ASR timing was available to verify that the attached captions "
                "match this recording; confirm the pairing before rendering."
            )
        elif external and coverage < 0.5:
            warnings.append(
                "Fewer than half of the attached caption cues aligned reliably to "
                "this recording; verify the transcript pairing and cut boundaries."
            )
        evidence = {
            "source_kind": "external_transcript" if external else "asr_transcription",
            "alignment_coverage": round(coverage, 6),
            "alignment_confidence": round(
                sum(confidence_values) / len(confidence_values), 6
            )
            if confidence_values
            else 0.0,
            "timing_available": bool(timing_words) or reused_alignment,
            "warnings": warnings,
        }
        if timing_metadata and timing_metadata.get("alignment_method"):
            evidence.update(
                {
                    "alignment_method": timing_metadata.get("alignment_method"),
                    "alignment_engine": timing_metadata.get(
                        "ctc_engine", timing_metadata.get("engine", "")
                    ),
                    "alignment_model": timing_metadata.get(
                        "ctc_model", timing_metadata.get("model", "")
                    ),
                    "timing_quality_basis": timing_metadata.get(
                        "timing_quality_basis", ""
                    ),
                    "alignment_request_strategy": timing_metadata.get(
                        "alignment_request_strategy", ""
                    ),
                    "alignment_endpoint_strategy": timing_metadata.get(
                        "alignment_endpoint_strategy", ""
                    ),
                    "alignment_artifact_reused": reused_alignment,
                }
            )
            if reused_alignment:
                for source_key, evidence_key in (
                    ("alignment_coverage", "alignment_coverage"),
                    ("eligible_alignment_coverage", "alignment_eligible_coverage"),
                    ("alignment_confidence", "alignment_quality"),
                ):
                    try:
                        value = float(timing_metadata[source_key])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if 0 <= value <= 1:
                        evidence[evidence_key] = round(value, 6)
                evidence.update(
                    {
                        "word_count": timing_metadata.get(
                            "word_count", timing_metadata.get("accepted_token_count", 0)
                        ),
                        "cue_count": timing_metadata.get("cue_count", len(cues)),
                        "fallback_triggered": bool(
                            timing_metadata.get("fallback_triggered", False)
                        ),
                        "alignment_counts": {
                            key: timing_metadata.get(key)
                            for key in (
                                "accepted_cue_count",
                                "accepted_token_count",
                                "overlap_cluster_count",
                                "oversized_cluster_count",
                                "oversized_cue_count",
                                "ctc_request_count",
                                "first_pass_batch_count",
                                "cluster_retries",
                                "individual_retries",
                                "outside_media_count",
                            )
                            if key in timing_metadata
                        },
                    }
                )
        return cue_payloads, evidence

    def prepare(self, session_id: str, *, force: bool = False) -> dict[str, Any]:
        # Keep the write transaction short: probing media and parsing captions
        # can involve substantial I/O and must not hold SQLite's write lock.
        with self.database.session() as read_session:
            input_snapshot = self._input_snapshot(read_session, session_id)
        primary_path, editorial_path, timing_path = self._snapshot_paths(input_snapshot)
        duration_ms = self._duration(self.duration_probe(primary_path))
        cues, evidence = self._parse_editorial(
            editorial_path,
            timing_path,
            duration_ms,
            external=input_snapshot.external_editorial,
            timing_metadata=(
                dict(input_snapshot.timing.metadata)
                if input_snapshot.timing is not None
                else None
            ),
            authoritative_artifact_id=input_snapshot.editorial.id,
        )
        keep_ranges = [
            {"id": "keep-000001", "start_ms": 0, "end_ms": duration_ms, "label": None}
        ]
        operation = {"type": "prepare"}

        with self.database.immediate_session() as session:
            primary, editorial, timing = self._resolve_snapshot_artifacts(
                session, input_snapshot
            )
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
            )
            active = (
                session.get(MediaEditPlanRevision, plan.active_revision_id)
                if plan and plan.active_revision_id
                else None
            )
            if active is not None and not force:
                active_artifacts = {
                    artifact.id: artifact
                    for artifact in session.scalars(
                        select(Artifact).where(
                            Artifact.id.in_(
                                [
                                    active.source_media_artifact_id,
                                    active.editorial_transcript_artifact_id,
                                    active.timing_artifact_id,
                                ]
                            )
                        )
                    ).all()
                }
                active_media_hash = (
                    active_artifacts[active.source_media_artifact_id].content_hash
                    if active.source_media_artifact_id in active_artifacts
                    else None
                )
                active_editorial_hash = (
                    active_artifacts[
                        active.editorial_transcript_artifact_id
                    ].content_hash
                    if active.editorial_transcript_artifact_id in active_artifacts
                    else None
                )
                active_timing_hash = (
                    active_artifacts[active.timing_artifact_id].content_hash
                    if active.timing_artifact_id in active_artifacts
                    else None
                )
                if (
                    active.duration_ms == duration_ms
                    and active_media_hash == primary.content_hash
                    and active_editorial_hash == editorial.content_hash
                    and (
                        (active.timing_artifact_id is None and timing is None)
                        or (
                            active.timing_artifact_id is not None
                            and timing is not None
                            and active_timing_hash == timing.content_hash
                        )
                    )
                ):
                    return self._state_in_session(session, session_id)
            if plan is None:
                plan = MediaEditPlan(session_id=session_id)
                session.add(plan)
                session.flush()
            revision_number = (active.revision_number + 1) if active else 1
            revision_snapshot = {
                "source_media_artifact_id": primary.id,
                "editorial_transcript_artifact_id": editorial.id,
                "timing_artifact_id": timing.id if timing else None,
                "duration_ms": duration_ms,
                "instructions": "",
                "keep_ranges": keep_ranges,
                "cues": cues,
                "evidence": evidence,
                "operation": operation,
                "reviewed": False,
            }
            revision = MediaEditPlanRevision(
                plan_id=plan.id,
                parent_revision_id=active.id if active else None,
                revision_number=revision_number,
                source_media_artifact_id=primary.id,
                editorial_transcript_artifact_id=editorial.id,
                timing_artifact_id=timing.id if timing else None,
                duration_ms=duration_ms,
                instructions="",
                keep_ranges_json=keep_ranges,
                cues_json=cues,
                evidence_json=evidence,
                operation_json=operation,
                reviewed=False,
                content_hash=self._content_hash(revision_snapshot),
            )
            session.add(revision)
            session.flush()
            self._invalidate_rendered_outputs(session, session_id)
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            session.flush()
            return self._state_in_session(session, session_id)

    def apply_proposal_in_session(
        self,
        session,
        *,
        session_id: str,
        expected_revision: int,
        cuts: list[dict[str, Any]],
        allow_empty: bool = False,
        provenance: dict[str, Any] | None = None,
        proposal_instructions: str | None = None,
        reject_duplicate_pairs: bool = False,
    ) -> dict[str, Any]:
        """Create an unreviewed revision inside a caller-owned transaction.

        The active provider-backed proposal route keeps its historical
        non-empty invariant. Passive dispatch uses ``allow_empty`` to record a
        valid global no-op while retaining the pinned keep ranges.
        """

        if not cuts and not allow_empty:
            raise ValueError("The proposal contains no usable cuts.")
        record = session.get(SessionRecord, session_id)
        if record is None:
            raise KeyError("session")
        if record.workflow_kind != "media_edit":
            raise ValueError(
                "Media-edit operations require a media_edit workflow session."
            )
        plan = session.scalar(
            select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
        )
        active = (
            session.get(MediaEditPlanRevision, plan.active_revision_id)
            if plan and plan.active_revision_id
            else None
        )
        if active is None or active.revision_number != expected_revision:
            raise MediaEditRevisionConflict(
                expected_revision,
                active.revision_number if active else None,
            )

        normalized_cuts: list[
            tuple[int, int, str, str, str, tuple[Any, Any], bool, bool]
        ] = []
        cues = [self._cue_from_payload(item) for item in active.cues_json or []]
        cue_by_id = {cue.id: (index, cue) for index, cue in enumerate(cues)}
        trustworthy_words: list[MediaWord] = []
        word_keys: set[tuple[str, int, int, float | None]] = set()
        for cue in cues:
            if (
                cue.timing_source not in {"asr_alignment", "ctc_alignment"}
                or cue.timing_confidence is None
                or cue.timing_confidence < 0.5
            ):
                continue
            for word in cue.words:
                key = (word.text, word.start_ms, word.end_ms, word.confidence)
                if key not in word_keys:
                    word_keys.add(key)
                    trustworthy_words.append(word)

        def refine_cue_boundary(
            cue: MediaCue, boundary_ms: int, *, side: Literal["start", "end"]
        ) -> BoundaryEvidence:
            if (
                cue.timing_source in {"asr_alignment", "ctc_alignment"}
                and cue.timing_confidence is not None
                and cue.timing_confidence >= 0.5
                and cue.words
            ):
                return refine_boundary(boundary_ms, trustworthy_words, side=side)
            return BoundaryEvidence(
                original_ms=boundary_ms,
                refined_ms=boundary_ms,
                confidence=0.0,
                method="caption_boundary",
                warnings=(
                    (
                        "caption boundary preserved because no reliable ASR word "
                        "alignment was available"
                    ),
                ),
            )

        seen: set[tuple[str, str]] = set()
        for item in cuts:
            if not isinstance(item, dict):
                raise TypeError("Each proposal cut must be an object.")
            start_at_media_start = bool(item.get("start_at_media_start", False))
            end_at_media_end = bool(item.get("end_at_media_end", False))
            start_value = item.get("start_cue_id")
            end_value = item.get("end_cue_id")
            if (start_value is not None) == start_at_media_start:
                raise ValueError(
                    "Exactly one of start_cue_id or start_at_media_start=true is required."
                )
            if (end_value is not None) == end_at_media_end:
                raise ValueError(
                    "Exactly one of end_cue_id or end_at_media_end=true is required."
                )
            start_id = str(start_value or "")
            end_id = str(end_value or "")
            reason_value = item.get("reason")
            if not isinstance(reason_value, str):
                raise TypeError("Proposal cut reason must be a string.")
            reason = reason_value.strip()
            if not start_at_media_start and start_id not in cue_by_id:
                raise ValueError("Proposal cut references an unknown cue ID.")
            if not end_at_media_end and end_id not in cue_by_id:
                raise ValueError("Proposal cut references an unknown cue ID.")
            start_index, start_cue = (
                cue_by_id[start_id] if not start_at_media_start else (-1, None)
            )
            end_index, end_cue = (
                cue_by_id[end_id] if not end_at_media_end else (len(cues), None)
            )
            if end_index < start_index:
                raise ValueError("Proposal cut cue IDs are out of order.")
            if not reason:
                raise ValueError("Proposal cut reasons must not be empty.")
            if len(reason) > 500:
                raise ValueError("Proposal cut reasons must be at most 500 characters.")
            key = (
                "__media_start__" if start_at_media_start else start_id,
                "__media_end__" if end_at_media_end else end_id,
            )
            if key in seen:
                if reject_duplicate_pairs:
                    raise ValueError("Proposal cut cue IDs must be unique.")
                continue
            seen.add(key)
            start_evidence = (
                BoundaryEvidence(0, 0, 1.0, "media_start")
                if start_at_media_start
                else refine_cue_boundary(start_cue, start_cue.start_ms, side="start")
            )
            end_evidence = (
                BoundaryEvidence(
                    active.duration_ms, active.duration_ms, 1.0, "media_end"
                )
                if end_at_media_end
                else refine_cue_boundary(end_cue, end_cue.end_ms, side="end")
            )
            if end_evidence.refined_ms <= start_evidence.refined_ms:
                raise ValueError(
                    "Proposal cut boundaries do not form a positive interval."
                )
            normalized_cuts.append(
                (
                    start_evidence.refined_ms,
                    end_evidence.refined_ms,
                    start_id,
                    end_id,
                    reason,
                    (start_evidence, end_evidence),
                    start_at_media_start,
                    end_at_media_end,
                )
            )

        if normalized_cuts:
            keep_ranges = keep_ranges_from_cuts(
                [(item[0], item[1]) for item in normalized_cuts], active.duration_ms
            )
            if not keep_ranges:
                raise ValueError("The proposal would remove the entire recording.")
            normalized_ranges = [self._keep_range_payload(item) for item in keep_ranges]
        else:
            normalized_ranges = list(active.keep_ranges_json or [])

        next_instructions = (
            active.instructions
            if proposal_instructions is None
            else proposal_instructions.strip()
        )
        if proposal_instructions is not None and not next_instructions:
            raise ValueError("Proposal instructions must not be empty.")

        evidence = dict(active.evidence_json or {})
        warnings = list(evidence.get("warnings") or [])
        boundary_records: list[dict[str, Any]] = []
        for (
            start_ms,
            end_ms,
            start_id,
            end_id,
            reason,
            boundaries,
            start_at_media_start,
            end_at_media_end,
        ) in normalized_cuts:
            start_evidence, end_evidence = boundaries
            boundary_records.append(
                {
                    "start_cue_id": None if start_at_media_start else start_id,
                    "start_at_media_start": start_at_media_start,
                    "end_cue_id": None if end_at_media_end else end_id,
                    "end_at_media_end": end_at_media_end,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "reason": reason,
                    "start": self._boundary_payload(start_evidence),
                    "end": self._boundary_payload(end_evidence),
                }
            )
            warnings.extend(start_evidence.warnings)
            warnings.extend(end_evidence.warnings)
        for cue in cues:
            if cue.timing_confidence is not None and cue.timing_confidence < 0.5:
                warnings.append(f"Low-confidence timing for cue {cue.id}.")
        evidence["warnings"] = list(dict.fromkeys(str(item) for item in warnings))
        evidence["agent_proposal"] = {"cuts": boundary_records}
        operation = {
            "type": "agent_proposal",
            "cuts": boundary_records,
        }
        if provenance:
            operation = {
                "type": "passive_dispatch",
                **provenance,
                "cuts": boundary_records,
            }
            evidence["passive_dispatch"] = dict(provenance)
        snapshot = {
            **self._revision_snapshot(active),
            "instructions": next_instructions,
            "keep_ranges": normalized_ranges,
            "evidence": evidence,
            "operation": operation,
            "reviewed": False,
        }
        revision = MediaEditPlanRevision(
            plan_id=active.plan_id,
            parent_revision_id=active.id,
            revision_number=active.revision_number + 1,
            source_media_artifact_id=active.source_media_artifact_id,
            editorial_transcript_artifact_id=active.editorial_transcript_artifact_id,
            timing_artifact_id=active.timing_artifact_id,
            duration_ms=active.duration_ms,
            instructions=next_instructions,
            keep_ranges_json=normalized_ranges,
            cues_json=list(active.cues_json or []),
            evidence_json=evidence,
            operation_json=operation,
            reviewed=False,
            content_hash=self._content_hash(snapshot),
        )
        session.add(revision)
        session.flush()
        self._invalidate_rendered_outputs(session, session_id)
        plan.active_revision_id = revision.id
        plan.updated_at = utcnow()
        session.flush()
        return self._state_in_session(session, session_id)

    def update(
        self,
        session_id: str,
        expected_revision: int,
        *,
        keep_ranges: list[dict[str, Any]],
        instructions: str | None = None,
        reviewed: bool | None = None,
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError("session")
            if record.workflow_kind != "media_edit":
                raise ValueError(
                    "Media-edit operations require a media_edit workflow session."
                )
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
            )
            active = (
                session.get(MediaEditPlanRevision, plan.active_revision_id)
                if plan and plan.active_revision_id
                else None
            )
            if active is None or active.revision_number != expected_revision:
                raise MediaEditRevisionConflict(
                    expected_revision, active.revision_number if active else None
                )
            normalized = normalize_keep_ranges(
                [
                    self._keep_range_from_payload(item, index)
                    for index, item in enumerate(keep_ranges, start=1)
                ],
                active.duration_ms,
            )
            normalized_ranges = [self._keep_range_payload(item) for item in normalized]
            next_instructions = (
                active.instructions if instructions is None else instructions
            )
            content_changed = (
                normalized_ranges != list(active.keep_ranges_json or [])
                or next_instructions != active.instructions
            )
            next_reviewed = (
                False
                if content_changed and reviewed is None
                else active.reviewed
                if reviewed is None
                else bool(reviewed)
            )
            if (
                normalized_ranges == list(active.keep_ranges_json or [])
                and next_instructions == active.instructions
                and next_reviewed == active.reviewed
            ):
                return self._state_in_session(session, session_id)
            operation = {"type": "manual_update"}
            snapshot = {
                **self._revision_snapshot(active),
                "instructions": next_instructions,
                "keep_ranges": normalized_ranges,
                "operation": operation,
                "reviewed": next_reviewed,
            }
            revision = MediaEditPlanRevision(
                plan_id=active.plan_id,
                parent_revision_id=active.id,
                revision_number=active.revision_number + 1,
                source_media_artifact_id=active.source_media_artifact_id,
                editorial_transcript_artifact_id=active.editorial_transcript_artifact_id,
                timing_artifact_id=active.timing_artifact_id,
                duration_ms=active.duration_ms,
                instructions=next_instructions,
                keep_ranges_json=normalized_ranges,
                cues_json=list(active.cues_json or []),
                evidence_json=dict(active.evidence_json or {}),
                operation_json=operation,
                reviewed=next_reviewed,
                content_hash=self._content_hash(snapshot),
            )
            session.add(revision)
            session.flush()
            self._invalidate_rendered_outputs(session, session_id)
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            session.flush()
            return self._state_in_session(session, session_id)

    def revision(
        self, session_id: str, revision_number: int | None = None
    ) -> dict[str, Any] | None:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError("session")
            if record.workflow_kind != "media_edit":
                raise ValueError(
                    "Media-edit operations require a media_edit workflow session."
                )
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
            )
            if plan is None:
                return None
            revision = (
                session.get(MediaEditPlanRevision, plan.active_revision_id)
                if revision_number is None
                else session.scalar(
                    select(MediaEditPlanRevision).where(
                        MediaEditPlanRevision.plan_id == plan.id,
                        MediaEditPlanRevision.revision_number == revision_number,
                    )
                )
            )
            if revision is None:
                return None
            ids = {
                revision.source_media_artifact_id,
                revision.editorial_transcript_artifact_id,
                revision.timing_artifact_id,
            }
            artifacts_by_id = {
                artifact.id: artifact
                for artifact in session.scalars(
                    select(Artifact).where(
                        Artifact.id.in_([item for item in ids if item])
                    )
                ).all()
            }
            return self._payload(revision, artifacts_by_id)

    @staticmethod
    def _cue_from_payload(item: dict[str, Any]) -> MediaCue:
        words = tuple(
            MediaWord(
                text=str(word.get("text") or ""),
                start_ms=MediaEditService._required_int(word, "start_ms"),
                end_ms=MediaEditService._required_int(word, "end_ms"),
                confidence=(
                    float(word["confidence"])
                    if word.get("confidence") is not None
                    else None
                ),
            )
            for word in (item.get("words") or [])
            if isinstance(word, dict)
        )
        return MediaCue(
            id=str(item.get("id") or ""),
            start_ms=MediaEditService._required_int(item, "start_ms"),
            end_ms=MediaEditService._required_int(item, "end_ms"),
            text=str(item.get("text") or ""),
            speaker=item.get("speaker"),
            words=words,
            timing_confidence=(
                float(item["timing_confidence"])
                if item.get("timing_confidence") is not None
                else None
            ),
            timing_source=str(item.get("timing_source") or "caption"),
        )

    @staticmethod
    def _boundary_payload(evidence) -> dict[str, Any]:
        return {
            "original_ms": evidence.original_ms,
            "refined_ms": evidence.refined_ms,
            "confidence": evidence.confidence,
            "method": evidence.method,
            "warnings": list(evidence.warnings),
        }

    @staticmethod
    def _cut_reason_records(revision: MediaEditPlanRevision) -> list[dict[str, Any]]:
        """Return proposal cut records without exposing the stored cue array."""

        evidence = dict(revision.evidence_json or {})
        operation = dict(revision.operation_json or {})
        records: list[dict[str, Any]] = []
        agent = evidence.get("agent_proposal")
        if isinstance(agent, dict):
            records.extend(
                item for item in agent.get("cuts") or [] if isinstance(item, dict)
            )
        passive = evidence.get("passive_dispatch")
        if isinstance(passive, dict):
            records.extend(
                item for item in passive.get("cuts") or [] if isinstance(item, dict)
            )
        records.extend(
            item for item in operation.get("cuts") or [] if isinstance(item, dict)
        )
        return records

    @classmethod
    def _cut_payloads(cls, revision: MediaEditPlanRevision) -> list[dict[str, Any]]:
        """Derive removal cuts as the exact complement of normalized keep ranges."""

        duration_ms = int(revision.duration_ms)
        keep_ranges = normalize_keep_ranges(
            [
                cls._keep_range_from_payload(item, index)
                for index, item in enumerate(revision.keep_ranges_json or [], start=1)
            ],
            duration_ms,
        )
        cuts: list[tuple[int, int]] = []
        cursor = 0
        for keep in keep_ranges:
            if keep.start_ms > cursor:
                cuts.append((cursor, keep.start_ms))
            cursor = max(cursor, keep.end_ms)
        if cursor < duration_ms:
            cuts.append((cursor, duration_ms))

        records = cls._cut_reason_records(revision)
        keep_by_end = {item.end_ms: item for item in keep_ranges}
        keep_by_start = {item.start_ms: item for item in keep_ranges}
        payloads: list[dict[str, Any]] = []
        for index, (start_ms, end_ms) in enumerate(cuts, start=1):
            preceding = keep_by_end.get(start_ms)
            following = keep_by_start.get(end_ms)
            reasons: list[str] = []
            for record in records:
                try:
                    record_start = int(record.get("start_ms"))
                    record_end = int(record.get("end_ms"))
                except (TypeError, ValueError):
                    continue
                reason = str(record.get("reason") or "").strip()
                if record_end <= start_ms or record_start >= end_ms or not reason:
                    continue
                if reason not in reasons:
                    reasons.append(reason)
            reason_count = len(reasons)
            reasons = reasons[:_CUT_MAX_REASONS]
            payloads.append(
                {
                    "index": index,
                    "id": f"cut-{index:06d}",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms,
                    "reasons": reasons,
                    "reason_count": reason_count,
                    "reasons_truncated": reason_count > len(reasons),
                    "start": {
                        "position_ms": start_ms,
                        "editable": preceding is not None,
                        "adjacent_keep_range_id": preceding.id if preceding else None,
                    },
                    "end": {
                        "position_ms": end_ms,
                        "editable": following is not None,
                        "adjacent_keep_range_id": following.id if following else None,
                    },
                }
            )
        return payloads

    @classmethod
    def _cuts_payload(
        cls, session_id: str, revision: MediaEditPlanRevision
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "session_id": session_id,
            "revision": revision.revision_number,
            "revision_id": revision.id,
            "content_hash": revision.content_hash,
            "duration_ms": revision.duration_ms,
            "reviewed": bool(revision.reviewed),
            "cuts": cls._cut_payloads(revision),
        }

    @classmethod
    def _boundary_word_payload(cls, cue_id: str, word: MediaWord) -> dict[str, Any]:
        text = word.text[:_BOUNDARY_MAX_WORD_TEXT_CHARS]
        return {
            "cue_id": cue_id,
            "text": text,
            "text_truncated": len(word.text) > len(text),
            "start_ms": word.start_ms,
            "end_ms": word.end_ms,
            "confidence": word.confidence,
        }

    @classmethod
    def _boundary_inspection_payload(
        cls,
        session_id: str,
        revision: MediaEditPlanRevision,
        cut: dict[str, Any],
        edge: str,
        *,
        context_ms: int,
        cue_limit: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(context_ms, int)
            or isinstance(context_ms, bool)
            or not 250 <= context_ms <= 30_000
        ):
            raise ValueError("context_ms must be between 250 and 30000")
        if (
            not isinstance(cue_limit, int)
            or isinstance(cue_limit, bool)
            or not 1 <= cue_limit <= 100
        ):
            raise ValueError("cue_limit must be between 1 and 100")
        if edge not in {"start", "end"}:
            raise ValueError("edge must be 'start' or 'end'")
        boundary_ms = int(cut["start_ms"] if edge == "start" else cut["end_ms"])
        window_start_ms = max(0, boundary_ms - context_ms)
        window_end_ms = min(int(revision.duration_ms), boundary_ms + context_ms)
        intersecting: list[MediaCue] = []
        for item in revision.cues_json or []:
            if not isinstance(item, dict):
                continue
            start_ms = cls._required_int(item, "start_ms")
            end_ms = cls._required_int(item, "end_ms")
            if end_ms > window_start_ms and start_ms < window_end_ms:
                intersecting.append(cls._cue_from_payload(item))
        nearest = sorted(
            intersecting,
            key=lambda cue: (
                0
                if cue.start_ms <= boundary_ms <= cue.end_ms
                else min(
                    abs(cue.start_ms - boundary_ms), abs(cue.end_ms - boundary_ms)
                ),
                cue.start_ms,
                cue.end_ms,
                cue.id,
            ),
        )[:cue_limit]
        selected = sorted(nearest, key=lambda cue: (cue.start_ms, cue.end_ms, cue.id))
        cues: list[dict[str, Any]] = []
        reliable_words: list[tuple[str, MediaWord]] = []
        for cue in intersecting:
            if (
                cue.timing_source in {"asr_alignment", "ctc_alignment"}
                and cue.timing_confidence is not None
                and cue.timing_confidence >= 0.5
            ):
                reliable_words.extend(
                    (cue.id, word)
                    for word in cue.words
                    if word.end_ms > window_start_ms and word.start_ms < window_end_ms
                )
        selected_words = sorted(
            (
                (cue.id, word)
                for cue in selected
                for word in cue.words
                if word.end_ms > window_start_ms and word.start_ms < window_end_ms
            ),
            key=lambda item: (item[1].start_ms, item[1].end_ms, item[0]),
        )

        def nearest_word_slice(
            values: list[tuple[str, MediaWord]], limit: int
        ) -> list[tuple[str, MediaWord]]:
            if len(values) <= limit:
                return values
            center = next(
                (
                    index
                    for index, (_cue_id, word) in enumerate(values)
                    if word.end_ms >= boundary_ms
                ),
                len(values),
            )
            start = max(0, center - limit // 2)
            end = min(len(values), start + limit)
            start = max(0, end - limit)
            return values[start:end]

        bounded_selected_words = nearest_word_slice(selected_words, _BOUNDARY_MAX_WORDS)
        included_word_ids = {id(word) for _cue_id, word in bounded_selected_words}
        cue_text_truncated = False
        for cue in selected:
            words = [
                cls._boundary_word_payload(cue.id, word)
                for word in cue.words
                if id(word) in included_word_ids
                if word.end_ms > window_start_ms and word.start_ms < window_end_ms
            ]
            text = cue.text[:_BOUNDARY_MAX_CUE_TEXT_CHARS]
            speaker = (
                str(cue.speaker)[:_BOUNDARY_MAX_SPEAKER_CHARS]
                if cue.speaker is not None
                else None
            )
            text_was_truncated = len(cue.text) > len(text)
            speaker_was_truncated = cue.speaker is not None and len(
                str(cue.speaker)
            ) > len(speaker or "")
            cue_text_truncated = (
                cue_text_truncated or text_was_truncated or speaker_was_truncated
            )
            cues.append(
                {
                    "id": cue.id,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "text": text,
                    "text_truncated": text_was_truncated,
                    "speaker": speaker,
                    "speaker_truncated": speaker_was_truncated,
                    "timing_source": cue.timing_source,
                    "timing_confidence": cue.timing_confidence,
                    "words": words,
                }
            )
        reliable_words.sort(
            key=lambda item: (item[1].start_ms, item[1].end_ms, item[0])
        )
        bounded_reliable_words = nearest_word_slice(reliable_words, _BOUNDARY_MAX_WORDS)
        gaps: list[dict[str, int]] = []
        for (_left_id, left), (_right_id, right) in itertools.pairwise(
            bounded_reliable_words
        ):
            gap_start = max(window_start_ms, left.end_ms)
            gap_end = min(window_end_ms, right.start_ms)
            if gap_end > gap_start:
                gaps.append(
                    {
                        "start_ms": gap_start,
                        "end_ms": gap_end,
                        "duration_ms": gap_end - gap_start,
                    }
                )
        gap_count = len(gaps)
        if gap_count > _BOUNDARY_MAX_GAPS:
            gaps = sorted(
                gaps,
                key=lambda gap: (
                    0
                    if gap["start_ms"] <= boundary_ms <= gap["end_ms"]
                    else min(
                        abs(gap["start_ms"] - boundary_ms),
                        abs(gap["end_ms"] - boundary_ms),
                    ),
                    gap["start_ms"],
                ),
            )[:_BOUNDARY_MAX_GAPS]
            gaps.sort(key=lambda gap: (gap["start_ms"], gap["end_ms"]))
        preceding = next(
            (
                cls._boundary_word_payload(cue_id, word)
                for cue_id, word in reversed(reliable_words)
                if word.end_ms <= boundary_ms
            ),
            None,
        )
        following = next(
            (
                cls._boundary_word_payload(cue_id, word)
                for cue_id, word in reliable_words
                if word.start_ms >= boundary_ms
            ),
            None,
        )
        return {
            "schema_version": "1",
            "session_id": session_id,
            "revision": revision.revision_number,
            "revision_id": revision.id,
            "content_hash": revision.content_hash,
            "duration_ms": revision.duration_ms,
            "reviewed": bool(revision.reviewed),
            "selected_cut": cut,
            "cut_index": cut["index"],
            "edge": edge,
            "boundary_ms": boundary_ms,
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "cues": cues,
            "speech_gaps": gaps,
            "nearest_preceding_word": preceding,
            "nearest_following_word": following,
            "limits": {
                "cue_limit": cue_limit,
                "word_limit": _BOUNDARY_MAX_WORDS,
                "speech_gap_limit": _BOUNDARY_MAX_GAPS,
            },
            "truncated": {
                "cues": len(intersecting) > len(selected),
                "cue_words": len(selected_words) > len(bounded_selected_words),
                "cue_text": cue_text_truncated,
                "speech_gaps": len(reliable_words) > len(bounded_reliable_words)
                or gap_count > len(gaps),
            },
        }

    def list_cuts(
        self,
        session_id: str,
        revision_number: int | None = None,
        *,
        cut_index: int | None = None,
        edge: Literal["start", "end"] | None = None,
        context_ms: int = 5_000,
        cue_limit: int = 40,
    ) -> dict[str, Any]:
        if (
            not isinstance(context_ms, int)
            or isinstance(context_ms, bool)
            or not 250 <= context_ms <= 30_000
        ):
            raise ValueError("context_ms must be between 250 and 30000")
        if (
            not isinstance(cue_limit, int)
            or isinstance(cue_limit, bool)
            or not 1 <= cue_limit <= 100
        ):
            raise ValueError("cue_limit must be between 1 and 100")
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError("session")
            if record.workflow_kind != "media_edit":
                raise ValueError(
                    "Media-edit operations require a media_edit workflow session."
                )
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
            )
            if plan is None:
                raise ValueError(
                    "No media-edit revision is available for this session."
                )
            revision = (
                session.get(MediaEditPlanRevision, plan.active_revision_id)
                if revision_number is None
                else session.scalar(
                    select(MediaEditPlanRevision).where(
                        MediaEditPlanRevision.plan_id == plan.id,
                        MediaEditPlanRevision.revision_number == revision_number,
                    )
                )
            )
            if revision is None:
                raise ValueError("The requested media-edit revision is not available.")
            payload = self._cuts_payload(session_id, revision)
            if (cut_index is None) != (edge is None):
                raise ValueError("cut_index and edge must be supplied together.")
            if cut_index is None:
                return payload
            if (
                not isinstance(cut_index, int)
                or isinstance(cut_index, bool)
                or cut_index < 1
            ):
                raise ValueError("cut_index must be at least 1")
            selected = next(
                (item for item in payload["cuts"] if item["index"] == cut_index), None
            )
            if selected is None:
                raise ValueError("The requested media-edit cut is not available.")
            return self._boundary_inspection_payload(
                session_id,
                revision,
                selected,
                str(edge),
                context_ms=context_ms,
                cue_limit=cue_limit,
            )

    def refine_boundary(
        self,
        session_id: str,
        expected_revision: int,
        *,
        cut_index: int,
        edge: Literal["start", "end"],
        position_ms: int | None = None,
        delta_ms: int | None = None,
    ) -> dict[str, Any]:
        """Atomically refine one removal boundary against the active revision."""

        if (position_ms is None) == (delta_ms is None):
            raise ValueError("Exactly one of position_ms or delta_ms is required.")
        if position_ms is not None and (
            not isinstance(position_ms, int) or isinstance(position_ms, bool)
        ):
            raise TypeError("position_ms must be an integer.")
        if delta_ms is not None and (
            not isinstance(delta_ms, int) or isinstance(delta_ms, bool) or delta_ms == 0
        ):
            raise ValueError("delta_ms must be a nonzero integer.")
        with self.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError("session")
            if record.workflow_kind != "media_edit":
                raise ValueError(
                    "Media-edit operations require a media_edit workflow session."
                )
            plan = session.scalar(
                select(MediaEditPlan).where(MediaEditPlan.session_id == session_id)
            )
            active = (
                session.get(MediaEditPlanRevision, plan.active_revision_id)
                if plan and plan.active_revision_id
                else None
            )
            if active is None or active.revision_number != expected_revision:
                raise MediaEditRevisionConflict(
                    expected_revision,
                    active.revision_number if active else None,
                )
            cuts = self._cut_payloads(active)
            if (
                not isinstance(cut_index, int)
                or isinstance(cut_index, bool)
                or cut_index < 1
                or cut_index > len(cuts)
            ):
                raise ValueError("The requested media-edit cut is not available.")
            if edge not in {"start", "end"}:
                raise ValueError("edge must be 'start' or 'end'")
            selected = cuts[cut_index - 1]
            active_keep_ranges = normalize_keep_ranges(
                [
                    self._keep_range_from_payload(item, index)
                    for index, item in enumerate(active.keep_ranges_json or [], start=1)
                ],
                active.duration_ms,
            )
            old_boundary = int(selected[f"{edge}_ms"])
            target = (
                int(position_ms)
                if position_ms is not None
                else old_boundary + int(delta_ms)
            )
            duration_ms = int(active.duration_ms)
            if delta_ms is not None and abs(int(delta_ms)) > duration_ms:
                raise ValueError("delta_ms must be within the media duration.")
            if target < 0 or target > duration_ms:
                raise ValueError(
                    "The refined boundary must be within the media duration."
                )
            adjacent = selected[edge].get("adjacent_keep_range_id")
            if not selected[edge].get("editable") or not adjacent:
                raise ValueError("The selected media-edit boundary is fixed.")
            keep = next(item for item in active_keep_ranges if item.id == adjacent)
            if edge == "start":
                # The preceding keep range's start is the furthest permitted expansion.
                lower = keep.start_ms
                if target < lower or target >= int(selected["end_ms"]):
                    raise ValueError(
                        "The refined boundary crosses the cut or adjacent keep range."
                    )
            else:
                upper = keep.end_ms
                if target <= int(selected["start_ms"]) or target > upper:
                    raise ValueError(
                        "The refined boundary crosses the cut or adjacent keep range."
                    )
            if target == old_boundary:
                return {
                    "schema_version": "1",
                    "session_id": session_id,
                    "previous_revision": expected_revision,
                    "current_revision": {
                        "revision": active.revision_number,
                        "revision_id": active.id,
                        "content_hash": active.content_hash,
                        "reviewed": bool(active.reviewed),
                    },
                    "change": {
                        "type": "boundary_refinement",
                        "cut_index": cut_index,
                        "edge": edge,
                        "from_ms": old_boundary,
                        "to_ms": target,
                        "no_op": True,
                    },
                    "affected_cut": selected,
                }
            adjusted_keep_ranges: list[KeepRange] = []
            for item in active_keep_ranges:
                if item.id != adjacent:
                    adjusted_keep_ranges.append(item)
                    continue
                adjusted = (
                    KeepRange(item.id, item.start_ms, target, item.label)
                    if edge == "start" and target > item.start_ms
                    else KeepRange(item.id, target, item.end_ms, item.label)
                    if edge == "end" and target < item.end_ms
                    else None
                )
                if adjusted is not None:
                    adjusted_keep_ranges.append(adjusted)
            if not adjusted_keep_ranges:
                raise ValueError("The refinement would remove the entire recording.")
            normalized_ranges = [
                self._keep_range_payload(item)
                for item in normalize_keep_ranges(adjusted_keep_ranges, duration_ms)
            ]
            snapshot = {
                **self._revision_snapshot(active),
                "keep_ranges": normalized_ranges,
                "operation": {
                    "type": "boundary_refinement",
                    "cut_index": cut_index,
                    "edge": edge,
                    "from_ms": old_boundary,
                    "to_ms": target,
                },
                "reviewed": False,
            }
            revision = MediaEditPlanRevision(
                plan_id=active.plan_id,
                parent_revision_id=active.id,
                revision_number=active.revision_number + 1,
                source_media_artifact_id=active.source_media_artifact_id,
                editorial_transcript_artifact_id=active.editorial_transcript_artifact_id,
                timing_artifact_id=active.timing_artifact_id,
                duration_ms=active.duration_ms,
                instructions=active.instructions,
                keep_ranges_json=normalized_ranges,
                cues_json=list(active.cues_json or []),
                evidence_json=dict(active.evidence_json or {}),
                operation_json=snapshot["operation"],
                reviewed=False,
                content_hash=self._content_hash(snapshot),
            )
            session.add(revision)
            session.flush()
            self._invalidate_rendered_outputs(session, session_id)
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            session.flush()
            refreshed = self._cut_payloads(revision)
            if len(refreshed) == len(cuts):
                affected = refreshed[cut_index - 1]
            else:
                affected = next(
                    (
                        item
                        for item in refreshed
                        if int(item["start_ms"]) <= target <= int(item["end_ms"])
                    ),
                    None,
                )
            return {
                "schema_version": "1",
                "session_id": session_id,
                "previous_revision": expected_revision,
                "current_revision": {
                    "revision": revision.revision_number,
                    "revision_id": revision.id,
                    "content_hash": revision.content_hash,
                    "reviewed": False,
                },
                "change": {
                    "type": "boundary_refinement",
                    "cut_index": cut_index,
                    "edge": edge,
                    "from_ms": old_boundary,
                    "to_ms": target,
                    "no_op": False,
                },
                "affected_cut": affected,
            }

    def apply_proposal(
        self,
        session_id: str,
        expected_revision: int,
        cuts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply validated agent cuts as one immutable, unreviewed revision."""

        if not cuts:
            raise ValueError("The proposal contains no usable cuts.")
        with self.database.immediate_session() as session:
            return self.apply_proposal_in_session(
                session,
                session_id=session_id,
                expected_revision=expected_revision,
                cuts=cuts,
            )
