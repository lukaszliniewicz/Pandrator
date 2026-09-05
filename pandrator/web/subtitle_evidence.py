"""Durable, bounded re-transcription evidence for subtitle cues."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.logic.audio_evidence import transcribe_audio_evidence
from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing.transcript_normalization import (
    NormalizedTranscript,
    load_transcript,
)
from pandrator.logic.dubbing.transcription import (
    extract_audio_excerpt,
    transcribe_source_file_with_metadata,
)

from .artifacts import ArtifactService
from .credentials import hydrate_stt_settings
from .database import Database
from .models import (
    Artifact,
    Document,
    DocumentRevision,
    Job,
    Provider,
    ProviderModel,
    Segment,
    SessionRecord,
    SubtitleEvidence,
    utcnow,
)
from .provider_settings import build_llm_settings
from .source_resolution import resolve_primary_source

EVIDENCE_STATUSES = frozenset(
    {"queued", "running", "completed", "failed", "resolved", "uncertain", "dismissed"}
)
EVIDENCE_ROUTES = frozenset({"whisper", "moss", "azure_mai_transcribe_2", "audio_llm"})
RESOLUTION_ACTIONS = frozenset(
    {"accepted", "edited", "deleted", "uncertain", "dismissed"}
)
MAX_EXCERPT_MS = 60_000


class SubtitleEvidenceService:
    """Create and execute subtitle-evidence requests with durable provenance."""

    def __init__(
        self,
        database: Database,
        artifacts: ArtifactService,
        jobs,
        workspace_settings,
        session_dir_resolver,
        paths,
    ):
        self.database = database
        self.artifacts = artifacts
        self.jobs = jobs
        self.workspace_settings = workspace_settings
        self.session_dir_resolver = session_dir_resolver
        self.paths = paths

    @staticmethod
    def _job_payload(job: Job | None) -> dict[str, Any] | None:
        if job is None:
            return None
        return {
            "id": job.id,
            "kind": job.kind,
            "session_id": job.session_id,
            "status": job.status,
            "progress": float(job.progress or 0.0),
            "progress_detail": job.progress_detail,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }

    @staticmethod
    def _record_payload(record: SubtitleEvidence) -> dict[str, Any]:
        return {
            "id": record.id,
            "session_id": record.session_id,
            "source_artifact_id": record.source_artifact_id,
            "source_media_artifact_id": record.source_media_artifact_id,
            "source_revision_id": record.source_revision_id,
            "source_segment_id": record.source_segment_id,
            "cue_id": record.cue_id,
            "start_ms": record.start_ms,
            "end_ms": record.end_ms,
            "clip_start_ms": record.clip_start_ms,
            "clip_end_ms": record.clip_end_ms,
            "reason": record.reason,
            "routes": list(record.routes_json or []),
            "audio_model_ids": list(record.audio_model_ids_json or []),
            "status": record.status,
            "job_id": record.job_id,
            "clip_artifact_id": record.clip_artifact_id,
            "candidates": deepcopy(record.candidates_json or []),
            "resolution": deepcopy(record.resolution_json or {}),
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        }

    @classmethod
    def _projection(
        cls, record: SubtitleEvidence, job: Job | None = None
    ) -> dict[str, Any]:
        return {"record": cls._record_payload(record), "job": cls._job_payload(job)}

    @staticmethod
    def _normalize_routes(routes: Any) -> list[str]:
        if not isinstance(routes, list) or not routes:
            raise ValueError("At least one evidence route is required.")
        normalized = [str(route).strip().lower().replace("-", "_") for route in routes]
        if len(normalized) > 4 or len(set(normalized)) != len(normalized):
            raise ValueError(
                "Evidence routes must be unique and contain at most four routes."
            )
        if any(route not in EVIDENCE_ROUTES for route in normalized):
            raise ValueError("Evidence route is not supported.")
        return normalized

    @staticmethod
    def _normalize_audio_model_ids(values: Any) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            raise TypeError("audio_model_ids must be a list.")
        normalized = [str(value).strip() for value in values]
        if len(normalized) > 3 or len(set(normalized)) != len(normalized):
            raise ValueError(
                "Audio model IDs must be unique and contain at most three models."
            )
        if any(not 1 <= len(value) <= 80 for value in normalized):
            raise ValueError("Audio model IDs must be between 1 and 80 characters.")
        return normalized

    @staticmethod
    def _validate_audio_models(session, model_ids: list[str]) -> None:
        rows = {
            row.id: row
            for row in session.scalars(
                select(ProviderModel).where(ProviderModel.id.in_(model_ids))
            ).all()
        }
        for model_id in model_ids:
            row = rows.get(model_id)
            if row is None:
                raise ValueError(f"Audio model {model_id!r} was not found.")
            provider = session.get(Provider, row.provider_id)
            if provider is None or provider.kind != "llm" or not provider.enabled:
                raise ValueError(
                    f"Audio model {row.model_id!r} belongs to a disabled LLM provider."
                )
            if not (row.is_active or row.is_default):
                raise ValueError(f"Audio model {row.model_id!r} is not active.")
            if "audio" not in (row.input_modalities_json or ["text"]):
                raise ValueError(
                    f"Model {row.model_id!r} is not configured for audio input."
                )

    @staticmethod
    def _clip_bounds(
        start_ms: int,
        end_ms: int,
        padding_before_ms: int,
        padding_after_ms: int,
    ) -> tuple[int, int]:
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("Subtitle cue has invalid timing.")
        cue_duration = end_ms - start_ms
        if cue_duration > MAX_EXCERPT_MS:
            raise ValueError("Subtitle cue duration must not exceed 60 seconds.")
        before = min(max(0, padding_before_ms), start_ms)
        after = max(0, padding_after_ms)
        available = MAX_EXCERPT_MS - cue_duration
        if before + after > available:
            # Remove excess padding from both sides of the cue as evenly as
            # possible while retaining all available context.
            before = min(before, available // 2)
            after = min(after, available - before)
            remaining = available - before - after
            if remaining:
                before = min(start_ms, before + remaining)
                remaining = available - before - after
                after += max(0, remaining)
        return max(0, start_ms - before), end_ms + after

    def _load_cue(
        self,
        session,
        session_id: str,
        source_artifact_id: str,
        cue_id: int,
    ) -> tuple[Artifact, DocumentRevision, Segment]:
        record = session.get(SessionRecord, session_id)
        if record is None or record.trashed_at is not None:
            raise KeyError(session_id)
        artifact = session.get(Artifact, source_artifact_id)
        if (
            artifact is None
            or artifact.session_id != session_id
            or artifact.state == "deleted"
        ):
            raise KeyError(source_artifact_id)
        source_name = str(
            (artifact.metadata_json or {}).get("original_filename")
            or artifact.relative_path
        )
        source_kind = str(artifact.kind or "").strip().lower().lstrip(".")
        if Path(source_name).suffix.lower() not in {
            ".srt",
            ".vtt",
            ".ass",
            ".ssa",
        } and source_kind not in {
            "srt",
            "vtt",
            "ass",
            "ssa",
            "subtitle",
            "subtitles",
        }:
            raise ValueError("The selected artifact is not a subtitle artifact.")
        metadata = (
            artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        )
        revision_id = str(metadata.get("revision_id") or "").strip()
        if not revision_id:
            raise ValueError("The subtitle artifact has no exact revision metadata.")
        revision = session.get(DocumentRevision, revision_id)
        if revision is None:
            raise ValueError("The subtitle revision is no longer available.")
        # A revision can only be used by an artifact in this session when its
        # parent document is in the same session.  Querying by revision id
        # avoids relying on an ORM relationship that the model intentionally
        # does not declare.
        document = session.get(Document, revision.document_id)
        if document is None or document.session_id != session_id:
            raise ValueError("The subtitle revision does not belong to this session.")
        segment = session.scalar(
            select(Segment).where(
                Segment.revision_id == revision.id,
                Segment.ordinal == cue_id - 1,
            )
        )
        if segment is None:
            raise ValueError(f"Subtitle cue {cue_id} was not found in this revision.")
        if segment.start_ms is None or segment.end_ms is None:
            raise ValueError("Subtitle cue has no usable timing.")
        if int(segment.start_ms) < 0 or int(segment.end_ms) <= int(segment.start_ms):
            raise ValueError("Subtitle cue has invalid timing.")
        return artifact, revision, segment

    def request(self, session_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, persist, and enqueue one evidence request atomically."""

        with self.database.immediate_session() as session:
            return self.request_in_session(session, session_id, values)

    def request_in_session(
        self,
        session,
        session_id: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create an evidence request inside an existing mutation transaction."""

        source_artifact_id = str(values.get("source_artifact_id") or "").strip()
        if not 1 <= len(source_artifact_id) <= 80:
            raise ValueError("source_artifact_id must be between 1 and 80 characters.")
        try:
            cue_id = int(values.get("cue_id"))
        except (TypeError, ValueError) as error:
            raise ValueError("cue_id must be a positive integer.") from error
        if cue_id < 1:
            raise ValueError("cue_id must be a positive integer.")
        reason = str(values.get("reason") or "")
        if not 1 <= len(reason) <= 4000:
            raise ValueError("reason must be between 1 and 4000 characters.")
        routes = self._normalize_routes(values.get("routes"))
        audio_model_ids = self._normalize_audio_model_ids(values.get("audio_model_ids"))
        if ("audio_llm" in routes) != bool(audio_model_ids):
            raise ValueError(
                "audio_llm requires one or more audio_model_ids, and audio_model_ids "
                "require the audio_llm route."
            )
        try:
            padding_before = int(values.get("padding_before_ms", 2000))
            padding_after = int(values.get("padding_after_ms", 2000))
        except (TypeError, ValueError) as error:
            raise ValueError("Evidence padding must be an integer.") from error
        if not 0 <= padding_before <= 15000 or not 0 <= padding_after <= 15000:
            raise ValueError(
                "Evidence padding must be between 0 and 15000 milliseconds."
            )

        artifact, revision, segment = self._load_cue(
            session, session_id, source_artifact_id, cue_id
        )
        primary = resolve_primary_source(session, session_id)
        media = primary.artifact
        if media is None or not primary.has_audio:
            raise ValueError("A managed primary audio or video source is required.")
        self._validate_audio_models(session, audio_model_ids)
        start_ms, end_ms = self._clip_bounds(
            int(segment.start_ms),
            int(segment.end_ms),
            padding_before,
            padding_after,
        )
        evidence = SubtitleEvidence(
            session_id=session_id,
            source_artifact_id=artifact.id,
            source_media_artifact_id=media.id,
            source_revision_id=revision.id,
            source_segment_id=segment.id,
            cue_id=cue_id,
            start_ms=int(segment.start_ms),
            end_ms=int(segment.end_ms),
            clip_start_ms=start_ms,
            clip_end_ms=end_ms,
            reason=reason,
            routes_json=routes,
            audio_model_ids_json=audio_model_ids,
            status="queued",
            candidates_json=[],
            resolution_json={},
        )
        session.add(evidence)
        session.flush()
        job = self.jobs.enqueue_in_session(
            session,
            "subtitle.evidence",
            {"evidence_id": evidence.id, "session_id": session_id},
            session_id=session_id,
            max_attempts=1,
            resource_keys=[f"session:{session_id}", f"subtitle-evidence:{evidence.id}"],
        )
        evidence.job_id = job.id
        evidence.updated_at = utcnow()
        session.flush()
        return self._projection(evidence, job)

    def list(
        self, session_id: str, source_artifact_id: str | None = None
    ) -> dict[str, Any]:
        with self.database.session() as session:
            statement = select(SubtitleEvidence).where(
                SubtitleEvidence.session_id == session_id
            )
            if source_artifact_id:
                statement = statement.where(
                    SubtitleEvidence.source_artifact_id == source_artifact_id
                )
            records = list(
                session.scalars(
                    statement.order_by(
                        SubtitleEvidence.created_at.desc(), SubtitleEvidence.id.desc()
                    )
                ).all()
            )
            jobs = {
                job.id: job
                for job in session.scalars(
                    select(Job).where(
                        Job.id.in_(
                            [record.job_id for record in records if record.job_id]
                        )
                    )
                ).all()
            }
            return {
                "session_id": session_id,
                "items": [
                    self._projection(item, jobs.get(item.job_id)) for item in records
                ],
            }

    def get(self, evidence_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            evidence = session.get(SubtitleEvidence, evidence_id)
            if evidence is None:
                raise KeyError(evidence_id)
            job = session.get(Job, evidence.job_id) if evidence.job_id else None
            return self._projection(evidence, job)

    def resolve(
        self,
        session_id: str,
        evidence_id: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            return self.resolve_in_session(session, session_id, evidence_id, values)

    def resolve_in_session(
        self,
        session,
        session_id: str,
        evidence_id: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve evidence inside an existing mutation transaction."""

        action = str(values.get("action") or "").strip().lower()
        if action not in RESOLUTION_ACTIONS:
            raise ValueError("Unsupported evidence resolution action.")
        candidate_id = str(values.get("candidate_id") or "").strip()
        text = values.get("text")
        text_value = str(text) if text is not None else ""
        note = str(values.get("note") or "")
        if action == "accepted" and not candidate_id:
            raise ValueError("Accepted evidence requires candidate_id.")
        if action == "edited" and not text_value.strip():
            raise ValueError("Edited evidence requires nonblank text.")
        if action == "uncertain" and not note.strip():
            raise ValueError("Uncertain evidence requires a concrete note.")
        if action != "accepted" and candidate_id:
            raise ValueError("Only accepted evidence may select a candidate.")
        if action != "edited" and text is not None:
            raise ValueError("Only edited evidence may provide text.")
        if len(candidate_id) > 120 or len(text_value) > 16000 or len(note) > 4000:
            raise ValueError("Evidence resolution input is too long.")

        evidence = session.get(SubtitleEvidence, evidence_id)
        if evidence is None or evidence.session_id != session_id:
            raise KeyError(evidence_id)
        if evidence.status not in {
            "completed",
            "failed",
            "resolved",
            "uncertain",
            "dismissed",
        }:
            raise ValueError(
                "Evidence can only be resolved after processing has stopped "
                f"(not {evidence.status})."
            )
        candidates = list(evidence.candidates_json or [])
        selected: dict[str, Any] | None = None
        if action == "accepted":
            selected = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict)
                    and str(item.get("id") or "") == candidate_id
                ),
                None,
            )
            if selected is None or str(selected.get("status") or "") != "success":
                raise ValueError(
                    "Accepted evidence candidate is unavailable or failed."
                )
        resolution: dict[str, Any] = {"action": action}
        if candidate_id:
            resolution["candidate_id"] = candidate_id
        if action == "edited":
            resolution["text"] = text_value
        if note:
            resolution["note"] = note
        evidence.resolution_json = resolution
        evidence.status = (
            "uncertain"
            if action == "uncertain"
            else ("dismissed" if action == "dismissed" else "resolved")
        )
        evidence.error_message = (
            None if action in {"accepted", "edited"} else evidence.error_message
        )
        evidence.updated_at = utcnow()
        session.flush()
        job = session.get(Job, evidence.job_id) if evidence.job_id else None
        return self._projection(evidence, job)

    @staticmethod
    def _safe_error(error: BaseException, jobs) -> str:
        try:
            value = jobs.redact_diagnostic(str(error))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            value = str(error)
        # Diagnostics are useful, but managed filesystem locations are an
        # implementation detail and must not become part of the REST surface.
        message = str(value or "Evidence route failed.").strip()
        message = re.sub(
            r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^\s,;]+", "<managed-path>", message
        )
        return message[:1000]

    @staticmethod
    def _legacy_mai_v2_config(settings: dict[str, Any]) -> dict[str, Any]:
        hydrated = deepcopy(settings)
        # Evidence rechecks should preserve the literal utterance.  Whole-file
        # transcription may prefer the cleaner style, but a bounded witness
        # must not silently remove repetitions or fillers before review.
        hydrated["stt_transcribe_style"] = "verbatim"
        records = [
            dict(item)
            for item in hydrated.get("provider_configs", [])
            if isinstance(item, dict)
        ]
        normalized_ids = {
            str(item.get("id") or "").strip().lower().replace("-", "_")
            for item in records
        }
        if "azure_mai_transcribe_2" not in normalized_ids:
            legacy = next(
                (
                    item
                    for item in records
                    if str(item.get("id") or "").strip().lower().replace("-", "_")
                    == "azure_mai_transcribe_1_5"
                ),
                None,
            )
            if legacy is not None:
                # Reuse only connection/credential locators. Capability,
                # endpoint, model, limits, and pricing metadata belong to the
                # MAI-2 built-in profile and must not leak from MAI-1.5.
                clone = {
                    key: legacy[key]
                    for key in (
                        "api_base",
                        "base_url",
                        "secret_ref",
                        "api_key",
                        "api_key_env",
                    )
                    if key in legacy
                }
                clone.update(
                    {
                        "id": "azure_mai_transcribe_2",
                        "engine": "azure_mai_transcribe_2",
                        "model": "MAI-Transcribe-2",
                        "name": "Azure MAI Transcribe 2",
                    }
                )
                records.append(clone)
        hydrated["provider_configs"] = records
        return hydrated

    @staticmethod
    def _safe_cost(metadata: Mapping[str, Any], *, commercial: bool) -> dict[str, Any]:
        if not commercial:
            return {"kind": "not_applicable"}
        usage = metadata.get("usage")
        if not isinstance(usage, Mapping):
            return {"kind": "unknown"}
        estimated_cost = usage.get("estimated_cost_usd")
        if (
            isinstance(estimated_cost, (int, float))
            and not isinstance(estimated_cost, bool)
            and estimated_cost >= 0
        ):
            result: dict[str, Any] = {
                "kind": "estimate",
                "amount": float(estimated_cost),
                "currency": str(usage.get("currency") or "USD"),
                "unit": "request",
                "usage_reported_by_provider": bool(
                    usage.get("usage_reported_by_provider", False)
                ),
            }
            for key in (
                "submitted_audio_seconds",
                "billable_audio_seconds",
                "billing_increment_seconds",
                "cost_source",
                "price_effective_until",
            ):
                value = usage.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    result[key] = value
            return result
        kind = str(usage.get("kind") or "").strip().lower()
        if kind not in {"estimate", "not_applicable", "unknown"}:
            return {"kind": "unknown"}
        result: dict[str, Any] = {"kind": kind}
        for key in ("amount", "currency", "unit"):
            value = usage.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                result[key] = value
        return result

    @staticmethod
    def _safe_llm_cost(cost: Any, source: str | None) -> dict[str, Any]:
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return {
                "kind": "actual",
                "amount": float(cost),
                "currency": "USD",
                "unit": "request",
                "cost_source": str(source or "provider_or_litellm"),
            }
        return {"kind": "unknown"}

    def _audio_model_runtime(self, model_record_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(ProviderModel, model_record_id)
            if row is None:
                raise ValueError(f"Audio model {model_record_id!r} was not found.")
            provider = session.get(Provider, row.provider_id)
            if provider is None or provider.kind != "llm" or not provider.enabled:
                raise ValueError(
                    f"Audio model {row.model_id!r} belongs to a disabled LLM provider."
                )
            if not (row.is_active or row.is_default):
                raise ValueError(f"Audio model {row.model_id!r} is not active.")
            if "audio" not in (row.input_modalities_json or ["text"]):
                raise ValueError(
                    f"Model {row.model_id!r} is not configured for audio input."
                )
            options = dict(provider.options_json or {})
            provider_key = str(provider.provider_key or "").strip().lower()
            settings_custom = bool(
                options.get("is_custom")
                or provider_key not in {"openai", "gemini", "anthropic"}
            )
            provider_id = str(
                options.get("provider_id") or provider.provider_key or provider.id
            )
            if settings_custom:
                provider_id = str(options.get("provider_id") or provider.id)
            canonical_model = (
                f"custom:{provider_id}/{row.model_id}"
                if settings_custom
                else f"{provider_key}/{row.model_id}"
            )
            snapshot = {
                "record_id": row.id,
                "model_id": row.model_id,
                "provider_id": provider.id,
                "provider_key": provider_key,
                "provider_label": provider.label,
                "canonical_model": canonical_model,
                "openai_compatible_custom": bool(options.get("is_custom"))
                and provider_key == "openai",
            }
        llm_settings, resolved_model = build_llm_settings(
            self.database,
            self.paths,
            requested_model=canonical_model,
            request_timeout_seconds=180,
        )
        snapshot["llm_settings"] = llm_settings
        snapshot["resolved_model"] = resolved_model
        return snapshot

    @staticmethod
    def _audio_prompt(
        cue_start_ms: int,
        cue_end_ms: int,
        clip_start_ms: int,
    ) -> str:
        relative_start = max(0.0, (cue_start_ms - clip_start_ms) / 1000)
        relative_end = max(relative_start, (cue_end_ms - clip_start_ms) / 1000)
        return (
            "Act as an acoustic transcription witness. Listen to the attached audio "
            f"and transcribe only the speech from {relative_start:.3f} to "
            f"{relative_end:.3f} seconds in the clip. Return only the literal spoken "
            "words, in their original language, with no commentary or Markdown. "
            "Preserve repetitions, false starts, short acknowledgements, names, and "
            "titles. Do not guess from topic or likely context. If that interval has "
            "no intelligible speech, return exactly [UNCERTAIN]."
        )

    @staticmethod
    def _rebase_transcript(
        transcript: NormalizedTranscript,
        clip_start_ms: int,
        clip_end_ms: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        segments: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []
        for segment in transcript.segments:
            segment_start = max(clip_start_ms, clip_start_ms + int(segment.start_ms))
            segment_end = min(clip_end_ms, clip_start_ms + int(segment.end_ms))
            if segment_end <= segment_start:
                continue
            segment_words: list[dict[str, Any]] = []
            for word in segment.words:
                word_start = max(clip_start_ms, clip_start_ms + int(word.start_ms))
                word_end = min(clip_end_ms, clip_start_ms + int(word.end_ms))
                if word_end <= word_start:
                    continue
                item = {
                    "text": str(word.text),
                    "start_ms": word_start,
                    "end_ms": word_end,
                    "speaker": word.speaker or None,
                    "confidence": word.confidence,
                }
                segment_words.append(item)
                words.append(item)
            segments.append(
                {
                    "id": segment.identifier or None,
                    "start_ms": segment_start,
                    "end_ms": segment_end,
                    "text": str(segment.text),
                    "speaker": segment.speaker or None,
                    "words": segment_words,
                }
            )
        return segments, words

    @staticmethod
    def _cue_text(
        segments: list[dict[str, Any]],
        words: list[dict[str, Any]],
        cue_start_ms: int,
        cue_end_ms: int,
    ) -> tuple[str, str]:
        """Select cue-overlapping speech while retaining the full clip transcript."""

        overlapping_words = [
            str(word.get("text") or "").strip()
            for word in words
            if min(cue_end_ms, int(word.get("end_ms") or 0))
            > max(cue_start_ms, int(word.get("start_ms") or 0))
            and str(word.get("text") or "").strip()
        ]
        if overlapping_words:
            return " ".join(overlapping_words), "word_overlap"
        overlapping_segments = [
            str(segment.get("text") or "").strip()
            for segment in segments
            if min(cue_end_ms, int(segment.get("end_ms") or 0))
            > max(cue_start_ms, int(segment.get("start_ms") or 0))
            and str(segment.get("text") or "").strip()
        ]
        if overlapping_segments:
            return " ".join(overlapping_segments), "segment_overlap"
        # Context belongs in ``context_text`` only. Substituting neighboring
        # speech when no timed content overlaps the cue would turn a timing
        # failure into confidently wrong evidence.
        return "", "no_overlap"

    @staticmethod
    def _pinned_media(session, evidence: SubtitleEvidence) -> Artifact:
        media_id = str(evidence.source_media_artifact_id or "").strip()
        if not media_id:
            raise ValueError(
                "This legacy evidence request does not pin a source media artifact. "
                "Create a new evidence request."
            )
        media = session.get(Artifact, media_id)
        if (
            media is None
            or media.session_id != evidence.session_id
            or media.state == "deleted"
        ):
            raise ValueError(
                "The source media pinned by this evidence request is no longer available."
            )
        return media

    def _persist_candidates(
        self,
        evidence_id: str,
        candidates: list[dict[str, Any]],
        clip_artifact_id: str | None,
    ) -> None:
        with self.database.immediate_session() as session:
            evidence = session.get(SubtitleEvidence, evidence_id)
            if evidence is None:
                raise KeyError(evidence_id)
            evidence.candidates_json = deepcopy(candidates)
            if clip_artifact_id:
                evidence.clip_artifact_id = clip_artifact_id
            evidence.updated_at = utcnow()

    def _set_failure(
        self,
        evidence_id: str,
        message: str,
        *,
        candidates: list[dict[str, Any]] | None = None,
        clip_artifact_id: str | None = None,
    ) -> None:
        with self.database.immediate_session() as session:
            evidence = session.get(SubtitleEvidence, evidence_id)
            if evidence is not None:
                evidence.status = "failed"
                evidence.error_message = message[:1000]
                if candidates is not None:
                    evidence.candidates_json = deepcopy(candidates)
                if clip_artifact_id:
                    evidence.clip_artifact_id = clip_artifact_id
                evidence.updated_at = utcnow()

    def run_request(
        self, evidence_id: str, progress, cancel_event: threading.Event
    ) -> dict[str, Any]:
        """Run each selected STT route independently and persist safe results."""

        with self.database.immediate_session() as session:
            evidence = session.get(SubtitleEvidence, evidence_id)
            if evidence is None:
                raise KeyError(evidence_id)
            if evidence.status in {"completed", "resolved", "uncertain", "dismissed"}:
                return {
                    "evidence_id": evidence.id,
                    "status": evidence.status,
                    "candidates": deepcopy(evidence.candidates_json or []),
                    "clip_artifact_id": evidence.clip_artifact_id,
                }
            if evidence.status == "running":
                raise RuntimeError("Subtitle evidence request is already running.")
            evidence.status = "running"
            evidence.error_message = None
            evidence.updated_at = utcnow()
            session.flush()
            session_id = evidence.session_id
            source_artifact_id = evidence.source_artifact_id
            cue_start_ms = int(evidence.start_ms)
            cue_end_ms = int(evidence.end_ms)
            clip_start_ms = int(evidence.clip_start_ms)
            clip_end_ms = int(evidence.clip_end_ms)
            routes = list(evidence.routes_json or [])
            audio_model_ids = list(evidence.audio_model_ids_json or [])

        candidates: list[dict[str, Any]] = []
        clip_artifact_id: str | None = None
        try:
            with self.database.session() as session:
                evidence = session.get(SubtitleEvidence, evidence_id)
                if evidence is None:
                    raise KeyError(evidence_id)
                media = self._pinned_media(session, evidence)
                source_path = self.paths.managed_path(media.relative_path)
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            root = (
                Path(self.session_dir_resolver(session_id))
                / "subtitle-evidence"
                / evidence_id
            )
            clip_dir = root / "clip"
            clip_dir.mkdir(parents=True, exist_ok=True)
            clip_path = Path(
                extract_audio_excerpt(
                    source_path,
                    clip_dir,
                    "excerpt",
                    clip_start_ms,
                    clip_end_ms,
                    cancel_event=cancel_event,
                )
            )
            clip_artifact = self.artifacts.register(
                clip_path,
                kind="wav",
                role="subtitle_evidence_audio",
                session_id=session_id,
                parent_ids=list(dict.fromkeys([media.id, source_artifact_id])),
                metadata={
                    "source_artifact_id": source_artifact_id,
                    "source_media_artifact_id": media.id,
                    "clip_start_ms": clip_start_ms,
                    "clip_end_ms": clip_end_ms,
                    "evidence_id": evidence_id,
                },
            )
            clip_artifact_id = clip_artifact.id
            with self.database.immediate_session() as session:
                evidence = session.get(SubtitleEvidence, evidence_id)
                if evidence is None:
                    raise KeyError(evidence_id)
                evidence.clip_artifact_id = clip_artifact.id
                evidence.updated_at = utcnow()

            stt_routes = [route for route in routes if route != "audio_llm"]
            witness_count = max(1, len(stt_routes) + len(audio_model_ids))
            for index, route in enumerate(stt_routes):
                if cancel_event.is_set():
                    self._set_failure(
                        evidence_id,
                        "Evidence transcription was canceled.",
                        candidates=candidates,
                        clip_artifact_id=clip_artifact.id,
                    )
                    return {
                        "evidence_id": evidence_id,
                        "status": "failed",
                        "candidates": candidates,
                        "clip_artifact_id": clip_artifact.id,
                    }
                route_dir = root / route
                route_dir.mkdir(parents=True, exist_ok=True)

                def report(
                    value,
                    detail=None,
                    *,
                    route_index=index,
                    total_routes=witness_count,
                ):
                    progress(
                        (route_index + max(0.0, min(1.0, float(value))))
                        / max(1, total_routes),
                        detail,
                    )

                try:
                    settings, _settings_hash = self.workspace_settings.resolve(
                        session_id,
                        ["stt"],
                        run_override={"stt": {"stt_engine": route}},
                    )
                    if route == "azure_mai_transcribe_2":
                        settings = self._legacy_mai_v2_config(settings)
                    runtime_settings = hydrate_stt_settings(
                        self.database, self.paths, settings["stt"]
                    )
                    transcription = transcribe_source_file_with_metadata(
                        route_dir,
                        clip_path,
                        runtime_settings,
                        progress_callback=report,
                        cancel_event=cancel_event,
                        source_is_normalized=True,
                    )
                    transcript = load_transcript(transcription.word_timestamps_path)
                    rebased_segments, rebased_words = self._rebase_transcript(
                        transcript, clip_start_ms, clip_end_ms
                    )
                    if not rebased_segments:
                        raise ValueError(
                            "The route returned no timed transcript segments."
                        )
                    context_text = " ".join(
                        item["text"] for item in rebased_segments
                    ).strip()
                    cue_text, selection_method = self._cue_text(
                        rebased_segments,
                        rebased_words,
                        cue_start_ms,
                        cue_end_ms,
                    )
                    if not cue_text:
                        raise ValueError(
                            "The route returned no speech overlapping this cue."
                        )
                    candidate_id = f"{route}-{index + 1}"
                    candidate = {
                        "id": candidate_id,
                        "route": route,
                        "status": "success",
                        "text": cue_text,
                        "context_text": context_text,
                        "selection_method": selection_method,
                        "language": transcript.language or None,
                        "timing_kind": "native_word",
                        "provider": str(transcript.metadata.get("provider") or route),
                        "model": str(
                            transcript.metadata.get("model")
                            or runtime_settings.get("stt_model")
                            or route
                        ),
                        "engine": str(transcription.engine or route),
                        "compute_backend": str(
                            transcription.compute_backend or "unknown"
                        ),
                        "segments": rebased_segments,
                        "words": rebased_words,
                        "cost": self._safe_cost(
                            transcript.metadata,
                            commercial=route == "azure_mai_transcribe_2",
                        ),
                    }
                    transcript_artifact = self.artifacts.register(
                        Path(transcription.word_timestamps_path),
                        kind="json",
                        role="subtitle_evidence_transcript",
                        session_id=session_id,
                        parent_ids=[clip_artifact.id, source_artifact_id],
                        metadata={
                            "evidence_id": evidence_id,
                            "route": route,
                            "language": transcript.language or None,
                            "engine": candidate["engine"],
                            "model": candidate["model"],
                            "timing_kind": "native_word",
                        },
                    )
                    candidate["transcript_artifact_id"] = transcript_artifact.id
                except ProcessCancelled:
                    self._set_failure(
                        evidence_id,
                        "Evidence transcription was canceled.",
                        candidates=candidates,
                        clip_artifact_id=clip_artifact.id,
                    )
                    return {
                        "evidence_id": evidence_id,
                        "status": "failed",
                        "candidates": candidates,
                        "clip_artifact_id": clip_artifact.id,
                    }
                # A failed witness must not discard successful independent
                # witnesses. Persist a bounded diagnostic and keep going.
                except Exception as error:  # noqa: BLE001
                    candidates.append(
                        {
                            "id": f"{route}-{index + 1}",
                            "route": route,
                            "status": "failed",
                            "error": self._safe_error(error, self.jobs),
                        }
                    )
                    self._persist_candidates(evidence_id, candidates, clip_artifact.id)
                    continue
                candidates.append(candidate)
                self._persist_candidates(evidence_id, candidates, clip_artifact.id)

            for offset, model_record_id in enumerate(audio_model_ids):
                index = len(stt_routes) + offset
                if cancel_event.is_set():
                    self._set_failure(
                        evidence_id,
                        "Evidence transcription was canceled.",
                        candidates=candidates,
                        clip_artifact_id=clip_artifact.id,
                    )
                    return {
                        "evidence_id": evidence_id,
                        "status": "failed",
                        "candidates": candidates,
                        "clip_artifact_id": clip_artifact.id,
                    }
                runtime: dict[str, Any] | None = None
                try:
                    runtime = self._audio_model_runtime(model_record_id)
                    progress(
                        (index + 0.05) / witness_count,
                        f"Sending bounded audio to {runtime['model_id']}",
                    )

                    def retry_audio(
                        attempt,
                        maximum,
                        delay,
                        *,
                        route_index=index,
                        total_routes=witness_count,
                    ):
                        progress(
                            (route_index + 0.25) / total_routes,
                            f"Audio model retry {attempt}/{maximum} in {delay:.1f}s",
                        )

                    audio_result = transcribe_audio_evidence(
                        clip_path,
                        self._audio_prompt(cue_start_ms, cue_end_ms, clip_start_ms),
                        str(runtime["resolved_model"]),
                        runtime["llm_settings"],
                        provider_key=str(runtime["provider_key"]),
                        is_custom=bool(runtime["openai_compatible_custom"]),
                        cancel_event=cancel_event,
                        retry_callback=retry_audio,
                    )
                    if cancel_event.is_set():
                        raise ProcessCancelled("Audio evidence was canceled.")
                    if audio_result.transcript.strip().upper() == "[UNCERTAIN]":
                        raise ValueError(
                            "The audio model reported that the cue was unintelligible."
                        )
                    transport = dict(audio_result.transport_metadata)
                    candidate = {
                        "id": f"audio_llm-{model_record_id}",
                        "route": "audio_llm",
                        "status": "success",
                        "text": audio_result.transcript,
                        "context_text": audio_result.transcript,
                        "selection_method": "bounded_clip",
                        "language": None,
                        "timing_kind": "bounded_clip",
                        "provider": str(runtime["provider_label"]),
                        "model": str(runtime["model_id"]),
                        "engine": "audio_llm",
                        "compute_backend": "provider",
                        # This witness heard the bounded clip but supplied no
                        # model-derived timing. Cue and clip bounds remain in
                        # request provenance, never in transcript segments.
                        "segments": [],
                        "words": [],
                        "transport": transport,
                        "usage": deepcopy(audio_result.completion.usage or {}),
                        "cost": self._safe_llm_cost(
                            audio_result.completion.cost,
                            audio_result.completion.cost_source,
                        ),
                    }
                    route_dir = root / "audio_llm" / model_record_id
                    route_dir.mkdir(parents=True, exist_ok=True)
                    transcript_path = route_dir / "transcript.json"
                    transcript_path.write_text(
                        json.dumps(
                            {
                                "text": candidate["text"],
                                "provider": candidate["provider"],
                                "model": candidate["model"],
                                "timing_kind": candidate["timing_kind"],
                                "transport": transport,
                                "usage": candidate["usage"],
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    transcript_artifact = self.artifacts.register(
                        transcript_path,
                        kind="json",
                        role="subtitle_evidence_transcript",
                        session_id=session_id,
                        parent_ids=[clip_artifact.id, source_artifact_id],
                        metadata={
                            "evidence_id": evidence_id,
                            "route": "audio_llm",
                            "provider": candidate["provider"],
                            "model": candidate["model"],
                            "timing_kind": "bounded_clip",
                            "transport": transport,
                        },
                    )
                    candidate["transcript_artifact_id"] = transcript_artifact.id
                    progress(
                        (index + 1.0) / witness_count,
                        f"Audio evidence returned by {runtime['model_id']}",
                    )
                except ProcessCancelled:
                    self._set_failure(
                        evidence_id,
                        "Evidence transcription was canceled.",
                        candidates=candidates,
                        clip_artifact_id=clip_artifact.id,
                    )
                    return {
                        "evidence_id": evidence_id,
                        "status": "failed",
                        "candidates": candidates,
                        "clip_artifact_id": clip_artifact.id,
                    }
                except Exception as error:  # noqa: BLE001
                    candidates.append(
                        {
                            "id": f"audio_llm-{model_record_id}",
                            "route": "audio_llm",
                            "status": "failed",
                            "model": (
                                str(runtime["model_id"])
                                if runtime is not None
                                else model_record_id
                            ),
                            "provider": (
                                str(runtime["provider_label"])
                                if runtime is not None
                                else None
                            ),
                            "error": self._safe_error(error, self.jobs),
                        }
                    )
                    self._persist_candidates(evidence_id, candidates, clip_artifact.id)
                    continue
                candidates.append(candidate)
                self._persist_candidates(evidence_id, candidates, clip_artifact.id)

            status = (
                "completed"
                if any(item.get("status") == "success" for item in candidates)
                else "failed"
            )
            error_message = None
            if status == "failed":
                error_message = "All selected transcription routes failed."
            with self.database.immediate_session() as session:
                evidence = session.get(SubtitleEvidence, evidence_id)
                if evidence is None:
                    raise KeyError(evidence_id)
                evidence.status = status
                evidence.candidates_json = candidates
                evidence.error_message = error_message
                evidence.updated_at = utcnow()
            progress(1.0, "Subtitle evidence complete")
            return {
                "evidence_id": evidence_id,
                "status": status,
                "candidates": candidates,
                "clip_artifact_id": clip_artifact.id,
            }
        except Exception as error:
            message = (
                "Evidence transcription was canceled."
                if isinstance(error, ProcessCancelled)
                else self._safe_error(error, self.jobs)
            )
            self._set_failure(
                evidence_id,
                message,
                candidates=candidates,
                clip_artifact_id=clip_artifact_id,
            )
            raise
