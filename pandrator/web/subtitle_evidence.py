"""Durable, bounded re-transcription evidence for subtitle cues."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import select

from pandrator.logic.audio_evidence import transcribe_audio_evidence
from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing.crispasr import MODELS as CRISPASR_MODELS
from pandrator.logic.dubbing.stt_backends import (
    CLOUD_STT_ENGINE_IDS,
    STT_BACKEND_LABELS,
    detect_stt_backend_statuses,
)
from pandrator.logic.dubbing.stt_languages import (
    normalize_stt_language,
    supported_stt_languages,
)
from pandrator.logic.dubbing.stt_provider_profiles import (
    get_stt_provider_profile,
)
from pandrator.logic.dubbing.transcript_normalization import (
    NormalizedTranscript,
    load_transcript,
)
from pandrator.logic.dubbing.transcription import (
    extract_audio_excerpt,
    transcribe_source_file_with_metadata,
)

from .artifacts import ArtifactService, sha256_file
from .credentials import hydrate_stt_settings
from .database import Database
from .models import (
    Artifact,
    ArtifactEdge,
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
from .stt_providers import stt_catalogue_snapshot
from .subtitle_media import artifact_accessible_in_session, resolve_subtitle_media

EVIDENCE_STATUSES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "failed",
        "resolved",
        "uncertain",
        "dismissed",
    }
)


def evidence_stt_routes() -> list[str]:
    """Return the stable engine IDs from the canonical STT backend registry."""

    return list(STT_BACKEND_LABELS)


EVIDENCE_ROUTES = frozenset((*evidence_stt_routes(), "audio_llm"))
RESOLUTION_ACTIONS = frozenset(
    {"accepted", "edited", "deleted", "uncertain", "dismissed"}
)
MAX_EXCERPT_MS = 60_000
EVIDENCE_CACHE_VERSION = 1
EVIDENCE_CACHE_LOOKBACK = 50
_STT_STATUS_CACHE_SECONDS = 30.0
_STT_STATUS_CACHE_LOCK = threading.Lock()
_STT_STATUS_CACHE_AT = 0.0
_STT_STATUS_CACHE: dict[str, Any] = {}


def _cached_stt_backend_statuses() -> dict[str, Any]:
    """Cache the lightweight canonical runtime probe across batch claims."""

    global _STT_STATUS_CACHE_AT, _STT_STATUS_CACHE
    with _STT_STATUS_CACHE_LOCK:
        now = time.monotonic()
        if _STT_STATUS_CACHE and now - _STT_STATUS_CACHE_AT < _STT_STATUS_CACHE_SECONDS:
            return dict(_STT_STATUS_CACHE)
        statuses = detect_stt_backend_statuses()
        _STT_STATUS_CACHE = dict(statuses)
        _STT_STATUS_CACHE_AT = now
        return dict(_STT_STATUS_CACHE)


def _route_timing_method(route: str) -> str:
    if route in CLOUD_STT_ENGINE_IDS:
        profile = get_stt_provider_profile(route)
        return str((profile or {}).get("word_timing") or "native")
    model = CRISPASR_MODELS.get(route)
    return str(model.word_timing) if model is not None else "bounded_clip"


def _route_language_support(route: str) -> list[str] | None:
    if route == "audio_llm":
        return None
    if route == "qwen3":
        from pandrator.logic.dubbing.qwen_asr import timed_supported_languages

        return list(timed_supported_languages())
    if route in CLOUD_STT_ENGINE_IDS:
        profile = get_stt_provider_profile(route) or {}
        languages = profile.get("supported_locales")
        return list(languages) if isinstance(languages, list) else None
    languages = supported_stt_languages(route)
    return list(languages) if languages is not None else None


def _language_supported(route: str, language: str | None) -> bool | None:
    if language is None or not str(language).strip():
        return None
    if route == "audio_llm":
        return None
    normalized = normalize_stt_language(language)
    if route == "qwen3":
        from pandrator.logic.dubbing.qwen_asr import (
            QwenASRError,
            normalize_qwen_asr_language,
        )

        try:
            normalized = normalize_qwen_asr_language(language)
        except QwenASRError:
            return False
        if normalized == "auto":
            return False
        supported = _route_language_support(route)
        return normalized in supported if supported is not None else None
    supported = _route_language_support(route)
    if normalized == "auto":
        return True
    if supported is None:
        return True
    return normalized in supported or normalized.split("-", 1)[0] in supported


def evidence_route_catalog(
    session,
    language: str | None = None,
    *,
    database: Database | None = None,
    paths=None,
) -> list[dict[str, Any]]:
    """Describe evidence routes without returning credential material.

    ``ready`` is tri-state: ``True`` means discovery found the route usable,
    ``False`` means discovery found a blocker, and ``None`` means discovery was
    unavailable or route readiness depends on a caller-selected model. Local
    routes are considered ready when the canonical STT runtime probe says
    their executable is available; models may still be downloaded on demand.
    Remote STT readiness reflects the existing provider credential catalogue.
    The opaque
    ``session`` parameter is retained for caller compatibility; callers may
    also pass ``database`` and ``paths`` directly to enable discovery.
    """

    database = database or getattr(session, "database", None)
    paths = paths or getattr(session, "paths", None)
    discovered_local: dict[str, Any] = {}
    discovered_remote: dict[str, Any] = {}
    discovery_error = "Route readiness requires database and paths for discovery."
    if database is not None and paths is not None:
        try:
            discovered_local = _cached_stt_backend_statuses()
        except Exception:  # noqa: BLE001
            discovery_error = "Local STT capability discovery is unavailable."
        try:
            payload, _revision = stt_catalogue_snapshot(database, paths)
            profiles = payload.get("profiles") if isinstance(payload, dict) else None
            discovered_remote = {
                str(item.get("id") or "").strip().lower().replace("-", "_"): item
                for item in profiles or []
                if isinstance(item, dict)
            }
        except Exception:  # noqa: BLE001
            if not discovered_local:
                discovery_error = "STT provider discovery is unavailable."

    entries: list[dict[str, Any]] = []
    for route in (*evidence_stt_routes(), "audio_llm"):
        remote = route in CLOUD_STT_ENGINE_IDS
        local: bool | None = not remote if route != "audio_llm" else None
        reason = ""
        ready: bool | None
        if route == "audio_llm":
            ready = None
            reason = "Readiness depends on the selected audio-capable LLM model."
        elif database is None or paths is None:
            ready = None
            reason = discovery_error
        elif remote:
            profile = discovered_remote.get(route)
            configured = (
                profile.get("credential_configured")
                if isinstance(profile, dict)
                else None
            )
            if isinstance(configured, bool):
                ready = configured
                reason = (
                    "Remote STT credential is configured."
                    if configured
                    else "Remote STT credential is not configured."
                )
            else:
                ready = None
                reason = "Remote STT credential readiness is unavailable."
        else:
            status = discovered_local.get(route)
            available = getattr(status, "installed", None)
            if isinstance(available, bool):
                ready = available
                if not ready:
                    reason = str(
                        getattr(status, "reason", "")
                        or "The local STT runtime is unavailable."
                    )
            else:
                ready = None
                reason = "Local STT capability discovery is unavailable."

        language_supported = _language_supported(route, language)
        language_reason = (
            f"Language {normalize_stt_language(language)!r} is unsupported by {route}."
            if language_supported is False
            else ""
        )
        entries.append(
            {
                "route": route,
                "label": (
                    "Audio-capable LLM"
                    if route == "audio_llm"
                    else STT_BACKEND_LABELS[route]
                ),
                "local": local,
                "remote": remote if route != "audio_llm" else None,
                "timing_method": _route_timing_method(route),
                "supported_languages": _route_language_support(route),
                "language_supported": language_supported,
                "ready": ready,
                "reason": reason or language_reason,
                "language_reason": language_reason,
            }
        )
    return entries


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
        if len(normalized) > len(EVIDENCE_ROUTES) or len(set(normalized)) != len(normalized):
            raise ValueError(
                "Evidence routes must be unique and contain at most seven routes."
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
        force_refresh = values.get("force_refresh", False)
        if not isinstance(force_refresh, bool):
            raise ValueError("force_refresh must be a boolean.")
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
        media = resolve_subtitle_media(session, session_id, artifact)
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
        job_payload: dict[str, Any] = {
            "evidence_id": evidence.id,
            "session_id": session_id,
        }
        if force_refresh:
            job_payload["force_refresh"] = True
        job = self.jobs.enqueue_in_session(
            session,
            "subtitle.evidence",
            job_payload,
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
        kind = str(usage.get("kind") or "").strip().lower()
        estimated_cost = usage.get("estimated_cost_usd")
        estimated_amount = -1.0
        if isinstance(estimated_cost, (int, float)) and not isinstance(
            estimated_cost, bool
        ):
            try:
                estimated_amount = float(estimated_cost)
            except OverflowError:
                pass
        if (
            kind not in {"actual", "billed"}
            and isinstance(estimated_cost, (int, float))
            and not isinstance(estimated_cost, bool)
            and math.isfinite(estimated_amount)
            and estimated_amount >= 0
        ):
            result: dict[str, Any] = {
                "kind": "estimate",
                "amount": estimated_amount,
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
        if kind == "not_applicable":
            # A remote request may be unpriced, but its cost is never
            # inapplicable.  Preserve that distinction as unknown.
            return {"kind": "unknown"}
        if kind not in {"actual", "billed", "estimate", "unknown"}:
            return {"kind": "unknown"}
        result: dict[str, Any] = {"kind": kind}
        for key in ("amount", "currency", "unit"):
            value = usage.get(key)
            if key == "amount" and isinstance(value, (int, float)):
                if value < 0 or not math.isfinite(float(value)):
                    continue
            if (
                isinstance(value, (str, int, float))
                and not isinstance(value, bool)
                and not (
                    isinstance(value, float) and not math.isfinite(value)
                )
            ):
                result[key] = value
        for key in (
            "cost_source",
            "price_effective_until",
            "usage_reported_by_provider",
            "submitted_audio_seconds",
            "billable_audio_seconds",
            "billing_increment_seconds",
        ):
            value = usage.get(key)
            if isinstance(value, (str, int, float, bool)) and not (
                isinstance(value, float) and not math.isfinite(value)
            ):
                result[key] = value
        return result

    @staticmethod
    def _timing_method(
        route: str,
        transcript: NormalizedTranscript,
        settings: Mapping[str, Any],
    ) -> str:
        """Return observed timing provenance or the route's canonical mode."""

        allowed = {
            "canary_ctc_fallback",
            "ctc",
            "dtw",
            "forced_aligner",
            "native",
            "native_turn",
            "qwen3_alignment",
            "qwen3_forced_aligner",
        }
        for word in transcript.words:
            method = str(word.metadata.get("timing_source") or "").strip().lower()
            if method in allowed:
                return method
        if route == "qwen3":
            from pandrator.logic.dubbing.qwen_asr import (
                normalize_qwen_asr_language,
                timing_plan_for_language,
            )

            language = transcript.language or str(settings.get("stt_language") or "")
            method = timing_plan_for_language(normalize_qwen_asr_language(language))
            if method in allowed:
                return method
        if route == "moss" and not transcript.words:
            return "native_turn"
        return _route_timing_method(route)

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
        if media is None or not artifact_accessible_in_session(
            session, evidence.session_id, media
        ):
            raise ValueError(
                "The source media pinned by this evidence request is no longer available."
            )
        return media

    @staticmethod
    def _sha256_json(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _sha256_media(path: Path, cancel_event: threading.Event) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if cancel_event.is_set():
                    raise ProcessCancelled("Evidence transcription was canceled.")
                digest.update(chunk)
        if cancel_event.is_set():
            raise ProcessCancelled("Evidence transcription was canceled.")
        return digest.hexdigest()

    @staticmethod
    def _sensitive_fingerprint_key(value: Any) -> bool:
        key = str(value or "").strip().lower().replace("-", "_")
        return (
            key.startswith("api_key")
            or any(
                marker in key
                for marker in (
                    "authorization",
                    "api_key",
                    "credential",
                    "password",
                    "secret",
                    "signature",
                    "access_token",
                    "refresh_token",
                    "vertex_credentials",
                )
            )
            or (key.endswith("_key") and key != "provider_key")
            or key.endswith("_token")
            or key.startswith("token_")
            or key in {"secret_ref", "sig", "token"}
        )

    @staticmethod
    def _safe_fingerprint_url(value: str) -> str:
        try:
            parts = urlsplit(value)
            if not parts.scheme or not parts.netloc:
                raise ValueError("URL has no authority")
            hostname = parts.hostname or ""
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            try:
                port = parts.port
            except ValueError:
                port = None
            netloc = f"{hostname}:{port}" if port else hostname
            query = [
                (key, item)
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
                if not SubtitleEvidenceService._sensitive_fingerprint_key(key)
            ]
            return urlunsplit(
                (parts.scheme, netloc, parts.path, urlencode(query), "")
            )
        except ValueError:
            safe = re.sub(r"(?<=://)[^/@?#]+@", "<redacted>@", value)
            safe = safe.split("#", 1)[0]
            prefix, separator, query = safe.partition("?")
            if not separator:
                return safe
            safe_pairs = []
            for item in query.split("&"):
                key, equals, content = item.partition("=")
                if SubtitleEvidenceService._sensitive_fingerprint_key(key):
                    content = "<redacted>"
                safe_pairs.append(f"{key}{equals}{content}")
            return f"{prefix}?{'&'.join(safe_pairs)}"

    @classmethod
    def _fingerprint_value(cls, value: Any) -> Any:
        """Build a credential-free value used only as SHA-256 input."""

        if isinstance(value, Mapping):
            return {
                str(key): (
                    cls._safe_fingerprint_url(str(item))
                    if str(key).strip().lower() in {"api_base", "base_url", "endpoint"}
                    and isinstance(item, str)
                    else cls._fingerprint_value(item)
                )
                for key, item in value.items()
                if not cls._sensitive_fingerprint_key(key)
            }
        if isinstance(value, (list, tuple)):
            return [cls._fingerprint_value(item) for item in value]
        if hasattr(value, "__dict__"):
            return cls._fingerprint_value(vars(value))
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @classmethod
    def _configuration_fingerprint(
        cls,
        route: str,
        settings_hash: Any,
        runtime_settings: Any,
        *,
        model_identity: Mapping[str, Any] | None = None,
    ) -> str:
        """Hash effective safe settings without persisting their raw values."""

        declared_hash = str(settings_hash or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", declared_hash):
            declared_hash = ""
        material = {
            "route": route,
            "effective_settings_sha256": declared_hash or None,
            "runtime_settings": cls._fingerprint_value(runtime_settings),
            "model_identity": cls._fingerprint_value(model_identity or {}),
        }
        return cls._sha256_json(material)

    @staticmethod
    def _source_language_scope(
        session, evidence: SubtitleEvidence
    ) -> dict[str, str | None]:
        revision = session.get(DocumentRevision, evidence.source_revision_id)
        document = session.get(Document, revision.document_id) if revision else None
        session_record = session.get(SessionRecord, evidence.session_id)

        def normalized(value: Any) -> str | None:
            result = str(value or "").strip().casefold()
            return result or None

        return {
            "document": normalized(document.language if document else None),
            "session": normalized(
                session_record.source_language if session_record else None
            ),
        }

    @staticmethod
    def _candidate_id(route: str) -> str:
        return f"{route}-{uuid4().hex}"

    @classmethod
    def _cache_key(
        cls,
        *,
        session_id: str,
        media_sha256: str,
        clip_start_ms: int,
        clip_end_ms: int,
        cue_start_ms: int,
        cue_end_ms: int,
        source_language: Mapping[str, str | None],
        route: str,
        model_id: str,
        configuration_sha256: str,
        prompt_sha256: str | None = None,
    ) -> str:
        return cls._sha256_json(
            {
                "version": EVIDENCE_CACHE_VERSION,
                "session_id": session_id,
                "media_sha256": media_sha256,
                "clip_window_ms": [clip_start_ms, clip_end_ms],
                "cue_window_ms": [cue_start_ms, cue_end_ms],
                "source_language": source_language,
                "route": route,
                "model_id": model_id,
                "configuration_sha256": configuration_sha256,
                "prompt_sha256": prompt_sha256,
            }
        )

    @classmethod
    def _cache_metadata(
        cls,
        *,
        key_sha256: str,
        media_sha256: str,
        configuration_sha256: str,
        prompt_sha256: str | None,
        route: str,
        model_id: str,
        clip_start_ms: int,
        clip_end_ms: int,
        cue_start_ms: int,
        cue_end_ms: int,
        source_language: Mapping[str, str | None],
    ) -> dict[str, Any]:
        return {
            "version": EVIDENCE_CACHE_VERSION,
            "key_sha256": key_sha256,
            "media_sha256": media_sha256,
            "configuration_sha256": configuration_sha256,
            "prompt_sha256": prompt_sha256,
            "route": route,
            "model_id": model_id,
            "clip_window_ms": [clip_start_ms, clip_end_ms],
            "cue_window_ms": [cue_start_ms, cue_end_ms],
            "source_language_sha256": cls._sha256_json(source_language),
        }

    def _find_cached_candidate(
        self,
        *,
        session_id: str,
        evidence_id: str,
        route: str,
        expected_cache: Mapping[str, Any],
        current_media_artifact_id: str,
        current_media_path: Path,
        cancel_event: threading.Event,
    ) -> dict[str, Any] | None:
        """Return a recent successful witness only when all provenance is live."""

        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(SubtitleEvidence)
                    .where(
                        SubtitleEvidence.session_id == session_id,
                        SubtitleEvidence.id != evidence_id,
                    )
                    .order_by(
                        SubtitleEvidence.created_at.desc(), SubtitleEvidence.id.desc()
                    )
                    .limit(EVIDENCE_CACHE_LOOKBACK)
                ).all()
            )
            for record in records:
                for candidate in record.candidates_json or []:
                    if (
                        not isinstance(candidate, dict)
                        or candidate.get("status") != "success"
                        or candidate.get("route") != route
                    ):
                        continue
                    cache = candidate.get("cache")
                    if (
                        not isinstance(cache, dict)
                        or any(
                            cache.get(key) != value
                            for key, value in expected_cache.items()
                        )
                    ):
                        continue
                    transcript_id = str(
                        candidate.get("transcript_artifact_id") or ""
                    ).strip()
                    media_id = str(record.source_media_artifact_id or "").strip()
                    clip_id = str(record.clip_artifact_id or "").strip()
                    if not transcript_id or not media_id or not clip_id:
                        continue
                    source = session.get(Artifact, record.source_artifact_id)
                    media = session.get(Artifact, media_id)
                    clip = session.get(Artifact, clip_id)
                    transcript = session.get(Artifact, transcript_id)
                    if (
                        source is None
                        or media is None
                        or clip is None
                        or transcript is None
                    ):
                        continue
                    if not all(
                        artifact_accessible_in_session(session, session_id, item)
                        for item in (source, media, clip, transcript)
                    ):
                        continue
                    source_metadata = (
                        source.metadata_json
                        if isinstance(source.metadata_json, dict)
                        else {}
                    )
                    clip_metadata = (
                        clip.metadata_json
                        if isinstance(clip.metadata_json, dict)
                        else {}
                    )
                    transcript_metadata = (
                        transcript.metadata_json
                        if isinstance(transcript.metadata_json, dict)
                        else {}
                    )
                    if (
                        str(source_metadata.get("revision_id") or "")
                        != record.source_revision_id
                        or str(clip_metadata.get("evidence_id") or "") != record.id
                        or str(clip_metadata.get("source_media_artifact_id") or "")
                        != media.id
                        or str(transcript_metadata.get("evidence_id") or "")
                        != record.id
                        or transcript.role != "subtitle_evidence_transcript"
                    ):
                        continue
                    parent_ids = set(
                        session.scalars(
                            select(ArtifactEdge.parent_artifact_id).where(
                                ArtifactEdge.child_artifact_id == transcript.id
                            )
                        ).all()
                    )
                    if not {source.id, clip.id}.issubset(parent_ids):
                        continue
                    try:
                        source_path = self.paths.managed_path(source.relative_path)
                        transcript_path = self.paths.managed_path(
                            transcript.relative_path
                        )
                        clip_path = self.paths.managed_path(clip.relative_path)
                        media_path = (
                            current_media_path
                            if media.id == current_media_artifact_id
                            else self.paths.managed_path(media.relative_path)
                        )
                        if (
                            not source_path.is_file()
                            or not transcript_path.is_file()
                            or not clip_path.is_file()
                            or not transcript.content_hash
                            or sha256_file(transcript_path) != transcript.content_hash
                        ):
                            continue
                        if (
                            media.id != current_media_artifact_id
                            and self._sha256_media(media_path, cancel_event)
                            != expected_cache["media_sha256"]
                        ):
                            continue
                    except (OSError, ValueError):
                        continue

                    return {
                        "candidate": deepcopy(candidate),
                        "transcript_path": transcript_path,
                        "source_evidence_id": record.id,
                        "source_candidate_id": str(candidate.get("id") or ""),
                        "source_transcript_artifact_id": transcript.id,
                        "source_artifact_id": source.id,
                        "source_revision_id": record.source_revision_id,
                        "source_media_artifact_id": media.id,
                    }
        return None

    def _materialize_cached_candidate(
        self,
        cached: Mapping[str, Any],
        *,
        evidence_id: str,
        session_id: str,
        source_artifact_id: str,
        clip_artifact_id: str,
        route: str,
        route_dir: Path,
        cache_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate = deepcopy(cached["candidate"])
        old_transcript_path = Path(cached["transcript_path"])
        route_dir.mkdir(parents=True, exist_ok=True)
        copy_path = route_dir / f"reused-{uuid4().hex}.json"
        shutil.copyfile(old_transcript_path, copy_path)
        transcript_artifact = self.artifacts.register(
            copy_path,
            kind="json",
            role="subtitle_evidence_transcript",
            session_id=session_id,
            parent_ids=[clip_artifact_id, source_artifact_id],
            metadata={
                "evidence_id": evidence_id,
                "route": route,
                "language": candidate.get("language"),
                "engine": candidate.get("engine"),
                "model": candidate.get("model"),
                "timing_kind": candidate.get("timing_kind"),
                "timing_method": candidate.get("timing_method"),
                "reused_from_evidence_id": cached["source_evidence_id"],
                "reused_from_transcript_artifact_id": cached[
                    "source_transcript_artifact_id"
                ],
            },
        )
        candidate["id"] = self._candidate_id(route)
        candidate["transcript_artifact_id"] = transcript_artifact.id
        candidate["cache"] = dict(cache_metadata)
        candidate["reused_from"] = {
            "evidence_id": cached["source_evidence_id"],
            "candidate_id": cached["source_candidate_id"],
            "transcript_artifact_id": cached["source_transcript_artifact_id"],
            "source_artifact_id": cached["source_artifact_id"],
            "source_revision_id": cached["source_revision_id"],
            "source_media_artifact_id": cached["source_media_artifact_id"],
        }
        candidate.pop("resolution", None)
        return candidate

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
        self,
        evidence_id: str,
        progress,
        cancel_event: threading.Event,
        *,
        force_refresh: bool = False,
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
            cue_start_ms = int(evidence.start_ms)
            cue_end_ms = int(evidence.end_ms)
            clip_start_ms = int(evidence.clip_start_ms)
            clip_end_ms = int(evidence.clip_end_ms)
            source_artifact_id = evidence.source_artifact_id
            source_media_artifact_id = str(evidence.source_media_artifact_id or "")
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
                source_language = self._source_language_scope(session, evidence)
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            media_sha256 = self._sha256_media(source_path, cancel_event)
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
                    settings, settings_hash = self.workspace_settings.resolve(
                        session_id,
                        ["stt"],
                        run_override={"stt": {"stt_engine": route}},
                    )
                    if route == "azure_mai_transcribe_2":
                        settings = self._legacy_mai_v2_config(settings)
                    runtime_settings = hydrate_stt_settings(
                        self.database, self.paths, settings["stt"]
                    )
                    model_id = str(
                        runtime_settings.get("stt_model")
                        or (
                            runtime_settings.get("qwen_asr_model")
                            if route == "qwen3"
                            else None
                        )
                        or route
                    )
                    configuration_sha256 = self._configuration_fingerprint(
                        route,
                        settings_hash,
                        runtime_settings,
                        model_identity={"model_id": model_id},
                    )
                    key_sha256 = self._cache_key(
                        session_id=session_id,
                        media_sha256=media_sha256,
                        clip_start_ms=clip_start_ms,
                        clip_end_ms=clip_end_ms,
                        cue_start_ms=cue_start_ms,
                        cue_end_ms=cue_end_ms,
                        source_language=source_language,
                        route=route,
                        model_id=model_id,
                        configuration_sha256=configuration_sha256,
                    )
                    cache_metadata = self._cache_metadata(
                        key_sha256=key_sha256,
                        media_sha256=media_sha256,
                        configuration_sha256=configuration_sha256,
                        prompt_sha256=None,
                        route=route,
                        model_id=model_id,
                        clip_start_ms=clip_start_ms,
                        clip_end_ms=clip_end_ms,
                        cue_start_ms=cue_start_ms,
                        cue_end_ms=cue_end_ms,
                        source_language=source_language,
                    )
                    cached = (
                        None
                        if force_refresh
                        else self._find_cached_candidate(
                            session_id=session_id,
                            evidence_id=evidence_id,
                            route=route,
                            expected_cache=cache_metadata,
                            current_media_artifact_id=source_media_artifact_id,
                            current_media_path=source_path,
                            cancel_event=cancel_event,
                        )
                    )
                    if cached is not None:
                        candidate = self._materialize_cached_candidate(
                            cached,
                            evidence_id=evidence_id,
                            session_id=session_id,
                            source_artifact_id=source_artifact_id,
                            clip_artifact_id=clip_artifact.id,
                            route=route,
                            route_dir=route_dir,
                            cache_metadata=cache_metadata,
                        )
                        candidates.append(candidate)
                        self._persist_candidates(
                            evidence_id, candidates, clip_artifact.id
                        )
                        progress(
                            (index + 1.0) / witness_count,
                            f"Reused {route} evidence from this session",
                        )
                        continue
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
                    timing_method = self._timing_method(
                        route, transcript, runtime_settings
                    )
                    candidate = {
                        "id": self._candidate_id(route),
                        "route": route,
                        "status": "success",
                        "text": cue_text,
                        "context_text": context_text,
                        "selection_method": selection_method,
                        "language": transcript.language or None,
                        # ``timing_kind`` is retained for existing consumers;
                        # timing_method records each engine's real provenance.
                        "timing_kind": "native_word",
                        "timing_method": timing_method,
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
                            commercial=route in CLOUD_STT_ENGINE_IDS,
                        ),
                        "cache": cache_metadata,
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
                            "timing_method": timing_method,
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
                            "id": self._candidate_id(route),
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
                    prompt = self._audio_prompt(
                        cue_start_ms, cue_end_ms, clip_start_ms
                    )
                    prompt_sha256 = hashlib.sha256(
                        prompt.encode("utf-8")
                    ).hexdigest()
                    configuration_sha256 = self._configuration_fingerprint(
                        "audio_llm",
                        None,
                        runtime.get("llm_settings"),
                        model_identity={
                            "record_id": runtime.get("record_id"),
                            "model_id": runtime.get("model_id"),
                            "provider_id": runtime.get("provider_id"),
                            "provider_key": runtime.get("provider_key"),
                            "canonical_model": runtime.get("canonical_model"),
                            "resolved_model": runtime.get("resolved_model"),
                        },
                    )
                    model_id = str(
                        runtime.get("model_id") or runtime.get("resolved_model")
                    )
                    key_sha256 = self._cache_key(
                        session_id=session_id,
                        media_sha256=media_sha256,
                        clip_start_ms=clip_start_ms,
                        clip_end_ms=clip_end_ms,
                        cue_start_ms=cue_start_ms,
                        cue_end_ms=cue_end_ms,
                        source_language=source_language,
                        route="audio_llm",
                        model_id=f"{model_record_id}:{model_id}",
                        configuration_sha256=configuration_sha256,
                        prompt_sha256=prompt_sha256,
                    )
                    audio_model_id = f"{model_record_id}:{model_id}"
                    cache_metadata = self._cache_metadata(
                        key_sha256=key_sha256,
                        media_sha256=media_sha256,
                        configuration_sha256=configuration_sha256,
                        prompt_sha256=prompt_sha256,
                        route="audio_llm",
                        model_id=audio_model_id,
                        clip_start_ms=clip_start_ms,
                        clip_end_ms=clip_end_ms,
                        cue_start_ms=cue_start_ms,
                        cue_end_ms=cue_end_ms,
                        source_language=source_language,
                    )
                    cached = (
                        None
                        if force_refresh
                        else self._find_cached_candidate(
                            session_id=session_id,
                            evidence_id=evidence_id,
                            route="audio_llm",
                            expected_cache=cache_metadata,
                            current_media_artifact_id=source_media_artifact_id,
                            current_media_path=source_path,
                            cancel_event=cancel_event,
                        )
                    )
                    if cached is not None:
                        candidate = self._materialize_cached_candidate(
                            cached,
                            evidence_id=evidence_id,
                            session_id=session_id,
                            source_artifact_id=source_artifact_id,
                            clip_artifact_id=clip_artifact.id,
                            route="audio_llm",
                            route_dir=root / "audio_llm" / model_record_id,
                            cache_metadata=cache_metadata,
                        )
                        candidates.append(candidate)
                        self._persist_candidates(
                            evidence_id, candidates, clip_artifact.id
                        )
                        progress(
                            (index + 1.0) / witness_count,
                            "Reused audio evidence from this session",
                        )
                        continue
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
                        prompt,
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
                        "id": self._candidate_id("audio_llm"),
                        "route": "audio_llm",
                        "status": "success",
                        "text": audio_result.transcript,
                        "context_text": audio_result.transcript,
                        "selection_method": "bounded_clip",
                        "language": None,
                        "timing_kind": "bounded_clip",
                        "timing_method": "bounded_clip",
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
                        "cache": cache_metadata,
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
                            "timing_method": "bounded_clip",
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
                            "id": self._candidate_id("audio_llm"),
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
