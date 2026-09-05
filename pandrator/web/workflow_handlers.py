"""Worker adapters that run existing Pandrator engines without Qt."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select, update

from pandrator.logic.dubbing.languages import (
    normalize_language_code,
    subtitle_language_title,
)
from pandrator.logic.dubbing.srt_utils import (
    split_speaker_label,
    timing_context_mode_from_settings,
)
from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.runtime import DataPaths

from .artifact_selection import canonical_stage_key, selected_artifacts
from .artifacts import ArtifactService
from .audio_verification import add_run_rms_warning, run_rms_outliers, verify_audio
from .credentials import (
    auxiliary_credential_key,
    database_reference,
    hydrate_stt_settings,
    hydrate_tts_settings,
    resolve_secret_reference,
)
from .database import Database
from .export_contract import ExportContract, normalize_audio_mode
from .jobs import JobQueue
from .models import (
    AgentRun,
    AgentStep,
    AppSetting,
    Artifact,
    ArtifactEdge,
    AudioTake,
    Document,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    OutcomePlan,
    OutputAssembly,
    Segment,
    SegmentLineage,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    SourceRecord,
    TimedWord,
    TrainingRun,
    UsageEvent,
    Voice,
    VoiceSample,
    new_id,
    utcnow,
)
from .output_settings_snapshot import build_output_settings_snapshot
from .source_resolution import resolve_primary_source
from .voice_library import (
    mark_provider_registrations_stale,
    remove_managed_files,
    retire_sample_artifact,
    sample_file_status,
)

if TYPE_CHECKING:
    from .manager_proxy import LocalManagerProxy
    from .subtitle_evidence import SubtitleEvidenceService
    from .tts_providers import TtsProviderRegistry


logger = logging.getLogger(__name__)

CLAUSE_PAUSE_RATIO = 1 / 3
GENERATION_SEGMENT_POLICY_VERSION = 5


def _effective_subtitle_language(*candidates: object) -> str:
    """Choose the first concrete language without letting ``auto`` mask it."""
    for candidate in candidates:
        normalized = normalize_language_code(str(candidate or ""), default="")
        if normalized and normalized not in {"auto", "und", "unknown"}:
            return normalized
    return "und"


def _scaled_progress_callback(progress, start: float, end: float):
    """Map a child operation's 0..1 progress into a reserved job span."""
    lower = max(0.0, min(1.0, float(start)))
    upper = max(lower, min(1.0, float(end)))

    def report(value: float, detail: str | None = None) -> None:
        try:
            fraction = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            fraction = 0.0
        progress(lower + (upper - lower) * fraction, detail)

    return report


def _fraction_message_callback(progress, start: float, end: float):
    """Map messages containing ``current/total`` into a bounded progress span."""
    mapped = _scaled_progress_callback(progress, start, end)
    last_fraction = 0.0

    def report(message: str) -> None:
        nonlocal last_fraction
        detail = str(message)
        matches = re.findall(r"(\d+)\s*/\s*(\d+)", detail)
        if matches:
            current, total = (int(value) for value in matches[0])
            if total > 0:
                # These messages are emitted immediately before the numbered
                # unit starts, so only earlier units are complete.
                last_fraction = max(
                    last_fraction,
                    max(0.0, min(1.0, (current - 1) / total)),
                )
        mapped(last_fraction, detail)

    return report


def _source_cleaning_progress_callback(
    progress,
    start: float,
    end: float,
    *,
    phase_names: list[str],
    phase_budgets: dict[str, int],
):
    """Turn phase/LLM-turn messages into progress across the full agent budget."""
    names = list(phase_names)
    budgets = [max(1, int(phase_budgets.get(name, 1))) for name in names]
    total_budget = max(1, sum(budgets))
    mapped = _scaled_progress_callback(progress, start, end)
    current_phase = 0
    last_fraction = 0.0

    def report(message: str) -> None:
        nonlocal current_phase, last_fraction
        detail = str(message)
        phase_match = re.search(r"\bPhase\s+(\d+)\s*/\s*(\d+)", detail, re.IGNORECASE)
        if phase_match and names:
            current_phase = max(0, min(len(names) - 1, int(phase_match.group(1)) - 1))
            completed_budget = sum(budgets[:current_phase])
            last_fraction = max(last_fraction, completed_budget / total_budget)
        else:
            turn_match = re.search(
                r"\bLLM turn\s+(\d+)\s*/\s*(\d+)", detail, re.IGNORECASE
            )
            if turn_match and names:
                turn = max(1, int(turn_match.group(1)))
                phase_budget = budgets[current_phase]
                completed_budget = sum(budgets[:current_phase]) + min(
                    phase_budget,
                    turn - 1,
                )
                last_fraction = max(last_fraction, completed_budget / total_budget)
        mapped(last_fraction, detail)

    return report


def _structured_speaker(segment: Any) -> str:
    speaker = str(getattr(segment, "speaker", None) or "").strip()
    if speaker:
        return speaker
    legacy_speaker, _text = split_speaker_label(str(getattr(segment, "text", "") or ""))
    return str(legacy_speaker or "").strip()


def _dominant_speaker(start_ms: int, end_ms: int, candidates: list[Any]) -> str:
    weighted: dict[str, tuple[str, int, int]] = {}
    for order, candidate in enumerate(candidates):
        speaker = _structured_speaker(candidate)
        candidate_start = getattr(candidate, "start_ms", None)
        candidate_end = getattr(candidate, "end_ms", None)
        if not speaker or candidate_start is None or candidate_end is None:
            continue
        overlap = min(end_ms, int(candidate_end)) - max(start_ms, int(candidate_start))
        if overlap <= 0:
            continue
        key = speaker.casefold()
        raw, total, first_order = weighted.get(key, (speaker, 0, order))
        weighted[key] = (raw, total + overlap, first_order)
    if not weighted:
        return ""
    return max(weighted.values(), key=lambda value: (value[1], -value[2]))[0]


def _hash_segments(segments) -> str:
    payload = [
        {
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
            "speaker": _structured_speaker(segment) or None,
        }
        for segment in segments
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _next_available_path(path: Path) -> Path:
    """Return a sibling path without overwriting an existing managed output."""
    if not path.exists():
        return path
    for version in range(2, 100_000):
        candidate = path.with_name(f"{path.stem}-{version}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate a new output filename for {path.name}.")


def _stage_settings_fingerprint(
    stage_key: str, settings: dict[str, Any]
) -> dict[str, Any]:
    """Semantic identity of a stage's settings, independent of submission shape.

    Only values that can change the produced artifact are included.  Raw hashes
    of whole settings dictionaries were unstable: the same configuration could
    arrive flat from a stage dialog or section-shaped from resolved settings,
    and hydrated dictionaries carry volatile provider data (keys, costs).  Both
    caused prerequisite stages such as translation to rerun spuriously.
    """

    def _text(*keys: str) -> str:
        for key in keys:
            value = settings.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _model(*keys: str) -> str:
        value = _text(*keys)
        return "" if value.lower() == "default" else value

    def _positive_int(key: str, default: int = 1) -> int:
        try:
            return max(1, int(settings.get(key) or default))
        except (TypeError, ValueError):
            return default

    def _nonnegative_int(*keys: str, default: int) -> int:
        value: Any = None
        for key in keys:
            if settings.get(key) not in {None, ""}:
                value = settings[key]
                break
        try:
            return max(0, int(default if value is None or value == "" else value))
        except (TypeError, ValueError):
            return default

    def _processing_shape() -> dict[str, Any]:
        shape: dict[str, Any] = {}
        char_limit = _nonnegative_int("char_limit", "llm_char", default=6000)
        segment_limit = _nonnegative_int(
            "max_segments_per_batch",
            "max_subtitles_per_call",
            default=40,
        )
        if char_limit != 6000:
            shape["char_limit"] = char_limit
        if segment_limit != 40:
            shape["max_segments_per_batch"] = segment_limit
        if bool(settings.get("no_remove_subtitles", False)):
            shape["no_remove_subtitles"] = True
        if settings.get("context") is False:
            shape["context"] = False
        context_before = _nonnegative_int("context_before", default=8)
        context_after = _nonnegative_int("context_after", default=2)
        if context_before != 8:
            shape["context_before"] = context_before
        if context_after != 2:
            shape["context_after"] = context_after
        mode = timing_context_mode_from_settings(settings)
        if mode != "full":
            shape["timing_context_mode"] = mode
        elif (
            gap := _nonnegative_int(
                "substantial_gap_ms",
                "timing_context_gap_ms",
                default=2000,
            )
        ) != 2000:
            shape["substantial_gap_ms"] = gap
        return shape

    if stage_key == "translate":
        backend = _text("translation_backend", "backend").lower() or "llm"
        model = _model("translation_model", "translate_model", "model_name")
        if not model and backend == "llm":
            model = _text("llm_default_model")
        result = {
            "backend": backend,
            "target_language": _text("target_language").lower(),
            "model": model,
            "instructions": _text("translate_prompt", "instructions"),
        }
        reasoning_effort = _text("reasoning_effort")
        if backend == "llm" and reasoning_effort:
            result["reasoning_effort"] = reasoning_effort
        concurrent_calls = _positive_int("llm_concurrent_calls")
        if backend == "llm" and concurrent_calls > 1:
            result["llm_concurrent_calls"] = concurrent_calls
        result.update(_processing_shape())
        if bool(settings.get("glossary_enabled", False)) and settings.get("glossary"):
            result["glossary"] = settings["glossary"]
        research = _research_fingerprint(settings)
        return {**result, **({"web_research": research} if research else {})}
    if stage_key == "correct":
        model = _model("correction_model", "correct_model", "model_name") or _text(
            "llm_default_model"
        )
        result = {
            "model": model,
            "instructions": _text("custom_correction_prompt", "instructions"),
        }
        reasoning_effort = _text("reasoning_effort")
        if reasoning_effort:
            result["reasoning_effort"] = reasoning_effort
        concurrent_calls = _positive_int("llm_concurrent_calls")
        if concurrent_calls > 1:
            result["llm_concurrent_calls"] = concurrent_calls
        result.update(_processing_shape())
        research = _research_fingerprint(settings)
        return {**result, **({"web_research": research} if research else {})}
    return {}


def _research_fingerprint(settings: dict[str, Any]) -> dict[str, Any]:
    if not bool(settings.get("web_research_enabled", False)):
        # Keep pre-feature artifact fingerprints reusable when research is off.
        return {}
    try:
        context_fraction = min(
            0.8,
            max(0.1, float(settings.get("web_research_context_fraction") or 0.8)),
        )
    except (TypeError, ValueError):
        context_fraction = 0.8
    return {
        "enabled": True,
        "provider": str(settings.get("web_research_provider") or "jina")
        .strip()
        .lower(),
        "model": str(settings.get("web_research_model_name") or "").strip(),
        "mode": str(settings.get("web_research_mode") or "global").strip().lower(),
        "context_fraction": context_fraction,
        "language": str(settings.get("web_research_language") or "").strip().lower(),
        "max_searches": max(0, int(settings.get("web_research_max_searches") or 3)),
        "max_extractions": max(
            0, int(settings.get("web_research_max_extractions") or 2)
        ),
        "preferred_domains": str(
            settings.get("web_research_preferred_domains") or ""
        ).strip(),
        "blocked_domains": str(
            settings.get("web_research_blocked_domains") or ""
        ).strip(),
    }


def _speech_block_settings(settings: dict[str, Any]) -> tuple[int, int, int, int, int]:
    def integer_setting(key: str, default: int) -> int:
        value = settings.get(key)
        return int(default if value is None or value == "" else value)

    min_chars = max(1, int(settings.get("speech_block_min_chars") or 10))
    max_chars = max(
        min_chars,
        int(settings.get("speech_block_max_chars") or 220),
    )
    merge_threshold = max(
        0,
        int(
            settings.get("speech_block_merge_threshold")
            if settings.get("speech_block_merge_threshold") is not None
            else settings.get("subtitle_merge_threshold", 250)
        ),
    )
    # These are independent policies.  In particular, zero is meaningful and
    # must not be replaced through truthiness-based defaulting.
    continuation_threshold = max(
        0,
        integer_setting("speech_block_continuation_threshold_ms", 3000),
    )
    max_internal_gap = max(
        0,
        integer_setting("speech_block_max_internal_gap_ms", 1800),
    )
    return (
        min_chars,
        max_chars,
        merge_threshold,
        continuation_threshold,
        max_internal_gap,
    )


def _generation_segmentation_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Subset of generation settings that can change stored plan segments.

    Voice, service, and model choices must not invalidate a segment plan, so
    they are deliberately excluded from the plan revision content hash.
    """
    (
        min_chars,
        max_chars,
        merge_threshold,
        continuation_threshold,
        max_internal_gap,
    ) = _speech_block_settings(settings)
    return {
        "segment_policy_version": GENERATION_SEGMENT_POLICY_VERSION,
        "speech_block_min_chars": min_chars,
        "speech_block_max_chars": max_chars,
        "speech_block_merge_threshold": merge_threshold,
        "speech_block_continuation_threshold_ms": continuation_threshold,
        "speech_block_max_internal_gap_ms": max_internal_gap,
        "paragraph_silence_ms": settings.get(
            "paragraph_silence_ms", settings.get("silence_for_paragraphs", 700)
        ),
        "sentence_silence_ms": settings.get(
            "sentence_silence_ms", settings.get("silence_between_sentences", 250)
        ),
        "clause_pause_ratio": CLAUSE_PAUSE_RATIO,
    }


def _record_continues_sentence(record: dict[str, Any]) -> bool:
    value = record.get("sentence_continues_after")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "clause"}
    if value is not None:
        return bool(value)
    if str(record.get("pause_kind") or "").strip().lower() == "clause":
        return True
    # Prepared narration created before the explicit continuation flag still
    # carries ``split_part``. Internal pieces lack terminal sentence
    # punctuation, while the final piece retains it.
    if record.get("split_part") is not None:
        text = str(record.get("text") or record.get("original_sentence") or "").rstrip()
        return re.search(r"[.!?…。！？…][\"'”’)\]}]*$", text) is None
    return False


def _default_silence_after_ms(
    record: dict[str, Any],
    settings: dict[str, Any],
    *,
    is_subtitle: bool = False,
) -> int:
    explicit = record.get("silence_after_ms")
    if explicit is not None:
        return max(0, int(explicit or 0))
    if is_subtitle:
        return 0

    sentence_silence = max(
        0,
        int(
            settings.get(
                "sentence_silence_ms", settings.get("silence_between_sentences", 250)
            )
            or 0
        ),
    )
    is_paragraph = (
        bool(record.get("paragraph_break_after"))
        or str(record.get("paragraph") or "").lower() == "yes"
    )
    if is_paragraph:
        return max(
            0,
            int(
                settings.get(
                    "paragraph_silence_ms", settings.get("silence_for_paragraphs", 700)
                )
                or 0
            ),
        )
    if _record_continues_sentence(record):
        return max(0, round(sentence_silence * CLAUSE_PAUSE_RATIO))
    return sentence_silence


def _apply_segment_tts_overrides(
    settings: dict[str, Any],
    *,
    language: str | None = None,
    voice: str | None = None,
) -> dict[str, Any]:
    if not language and not voice:
        return settings
    resolved = dict(settings)
    if language:
        resolved.update({"language": language, "target_language": language})
    if voice:
        # Runtime adapters consume the legacy ``speaker`` alias, while newer
        # and OpenAI-compatible adapters consume ``voice``.
        resolved.update({"voice": voice, "speaker": voice})
    return resolved


def _apply_selected_segment_tts_override(
    settings: dict[str, Any], override: dict[str, Any] | None
) -> dict[str, Any]:
    """Apply an immutable run-local override after persistent segment settings."""
    selected = dict(override or {})
    if not selected:
        return settings
    resolved = deepcopy(settings)
    for key, value in selected.items():
        if isinstance(value, dict) and isinstance(resolved.get(key), dict):
            resolved[key] = {**resolved[key], **deepcopy(value)}
        else:
            resolved[key] = deepcopy(value)
    return _apply_segment_tts_overrides(
        resolved,
        language=str(
            selected.get("language") or selected.get("target_language") or ""
        ).strip()
        or None,
        voice=str(selected.get("voice") or selected.get("speaker") or "").strip()
        or None,
    )


def _provider_endpoint_fingerprint(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _managed_provider_voice_id(voice: Voice) -> str:
    label = re.sub(r"[^a-z0-9]+", "-", voice.name.casefold()).strip("-")
    label = label[:40].rstrip("-") or "voice"
    return f"pandrator-{label}-{voice.id.replace('-', '')[:10]}"


def _normalized_provider_registration_id(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _secret_free_tts_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep useful runtime settings while excluding inline audio references."""
    result = deepcopy(settings or {})
    for key in (
        "audio_cpp_voice_ref",
        "audio_cpp_voice_ref_hash",
        "audio_cpp_reference_text",
    ):
        result.pop(key, None)
    return result


class WorkflowHandlers:
    def __init__(
        self,
        database: Database,
        paths: DataPaths,
        *,
        tts_providers: TtsProviderRegistry | None = None,
        manager_bridge: LocalManagerProxy | None = None,
        jobs: JobQueue | None = None,
        subtitle_evidence: SubtitleEvidenceService | None = None,
    ):
        self.database = database
        self.paths = paths
        self.artifacts = ArtifactService(database, paths)
        if tts_providers is None:
            from .tts_providers import TtsProviderRegistry

            tts_providers = TtsProviderRegistry()
        self.tts_providers = tts_providers
        self.manager_bridge = manager_bridge
        self.jobs = jobs or JobQueue(database)
        self.subtitle_evidence = subtitle_evidence
        self._audio_cpp_voice_ref_cache: OrderedDict[str, str] = OrderedDict()
        self._audio_cpp_voice_ref_cache_lock = threading.Lock()
        from .job_handler_domains import build_workflow_handler_registry

        self.handler_registry = build_workflow_handler_registry(self)

    def run_subtitle_evidence(self, payload, progress, cancel_event):
        """Delegate the durable subtitle-evidence job to its service."""
        if self.subtitle_evidence is None:
            raise RuntimeError("Subtitle evidence service is not configured.")
        return self.subtitle_evidence.run_request(
            str(payload.get("evidence_id") or ""), progress, cancel_event
        )

    def _resume_generation_after_regeneration(
        self,
        child_run_id: str,
        source_run_id: str,
    ) -> str | None:
        """Queue a checkpoint-preserving resume after a temporary regen pause."""
        with self.database.immediate_session() as session:
            child_run = session.get(GenerationRun, child_run_id)
            source_run = session.get(GenerationRun, source_run_id)
            if (
                child_run is None
                or child_run.source_generation_run_id != source_run_id
                or not child_run.resume_source_on_completion
            ):
                return None
            child_run.resume_source_on_completion = False
            if (
                source_run is None
                or source_run.status != "paused"
                or not source_run.pause_requested
                or source_run.cancel_requested
            ):
                return None
            snapshot = dict(source_run.settings_snapshot_json or {})
            source_run.pause_requested = False
            source_run.cancel_requested = False
            source_run.status = "queued"
            source_run.updated_at = utcnow()
            from .workspace import GenerationService

            resource_keys = GenerationService._resource_keys(
                source_run.session_id,
                snapshot,
            )
            job = self.jobs.enqueue_in_session(
                session,
                "generation.run",
                {
                    "generation_run_id": source_run.id,
                    "segment_ids": [],
                    "operation": "resume",
                },
                session_id=source_run.session_id,
                resource_keys=resource_keys,
            )
            source_run.job_id = job.id
            return job.id

    @staticmethod
    def _verification_metadata(
        audio,
        synthesized_text: str,
        settings: dict[str, Any],
    ) -> dict[str, Any] | None:
        return verify_audio(audio, synthesized_text, settings)

    def _finalize_run_audio_verification(self, run_id: str) -> int:
        """Add conservative run-relative RMS warnings after all takes exist."""
        marked_segment_ids: set[str] = set()
        with self.database.session() as session:
            rows = list(
                session.execute(
                    select(AudioTake, Artifact)
                    .join(Artifact, AudioTake.artifact_id == Artifact.id)
                    .where(AudioTake.generation_run_id == run_id)
                ).all()
            )
            grouped: dict[
                tuple[str, str], list[tuple[AudioTake, Artifact, dict[str, Any]]]
            ] = {}
            for take, artifact in rows:
                metadata = dict(artifact.metadata_json or {})
                verification = metadata.get("audio_verification")
                if (
                    not isinstance(verification, dict)
                    or verification.get("mode") != "signal"
                ):
                    continue
                if verification.get("status") != "passed":
                    marked_segment_ids.add(take.generation_segment_id)
                key = (str(take.kind or ""), str(take.settings_hash or ""))
                grouped.setdefault(key, []).append((take, artifact, verification))

            for entries in grouped.values():
                values = [
                    (entry[2].get("metrics") or {}).get("rms_dbfs") for entry in entries
                ]
                for index, detail in run_rms_outliers(values).items():
                    take, artifact, verification = entries[index]
                    metadata = dict(artifact.metadata_json or {})
                    metadata["audio_verification"] = add_run_rms_warning(
                        verification, detail
                    )
                    artifact.metadata_json = metadata
                    artifact.updated_at = utcnow()
                    marked_segment_ids.add(take.generation_segment_id)

            for segment_id in marked_segment_ids:
                segment = session.get(GenerationSegment, segment_id)
                if segment is not None:
                    segment.marked = True
                    segment.updated_at = utcnow()
        return len(marked_segment_ids)

    def handlers(self):
        """Compatibility mapping for callers not yet accepting a registry."""

        return self.handler_registry.as_dict()

    def prepare_audio_cpp_voice_reference(
        self,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Link the selected local voice to the newest ready WAV reference."""
        prepared = deepcopy(settings or {})
        if self.tts_providers.service_id_for_settings(prepared) != "audio_cpp":
            return prepared
        if "audio_cpp_voice_ref" in prepared:
            return prepared

        from pandrator.logic import tts_handler

        registration_ids = {"audio_cpp"}
        endpoint, _error = tts_handler.resolve_openai_audio_endpoint(prepared)
        if endpoint is not None and (
            str(endpoint.get("adapter") or "").strip().casefold().replace("-", "_")
            == "audio_cpp"
        ):
            registration_ids.add(
                _normalized_provider_registration_id(endpoint.get("name"))
            )

        selected = str(prepared.get("voice") or prepared.get("speaker") or "").strip()
        if not selected:
            return prepared
        selected_key = selected.casefold()
        match: tuple[str, Path, Artifact, VoiceSample] | None = None
        with self.database.session() as session:
            for voice in session.scalars(select(Voice)).all():
                providers = dict((voice.metadata_json or {}).get("providers") or {})
                registration = None
                for provider_id, raw_registration in providers.items():
                    normalized_provider = _normalized_provider_registration_id(
                        provider_id
                    )
                    if normalized_provider not in registration_ids or not isinstance(
                        raw_registration, dict
                    ):
                        continue
                    if (
                        str(raw_registration.get("resource_kind") or "")
                        != "linked_reference"
                        or str(raw_registration.get("status") or "") != "ready"
                    ):
                        continue
                    candidate_names = {
                        voice.name,
                        str(raw_registration.get("voice_id") or ""),
                        str(raw_registration.get("provider_voice_id") or ""),
                    }
                    if selected_key not in {
                        name.strip().casefold()
                        for name in candidate_names
                        if name.strip()
                    }:
                        continue
                    registration = raw_registration
                    break
                if registration is None:
                    continue

                samples = list(
                    session.scalars(
                        select(VoiceSample)
                        .where(VoiceSample.voice_id == voice.id)
                        .order_by(VoiceSample.created_at.desc())
                    ).all()
                )
                sample = next(
                    (
                        item
                        for item in samples
                        if sample_file_status(session, self.paths, item)[0] == "ready"
                    ),
                    None,
                )
                if sample is None:
                    raise ValueError(
                        f"Linked audio.cpp voice '{voice.name}' has no readable sample."
                    )
                artifact = session.get(Artifact, sample.artifact_id)
                status, path = sample_file_status(session, self.paths, sample)
                if artifact is None or status != "ready" or path is None:
                    raise ValueError(
                        f"Linked audio.cpp voice '{voice.name}' has no readable sample."
                    )
                if path.suffix.lower() != ".wav":
                    raise ValueError(
                        "audio.cpp linked voice references require a normalized WAV sample."
                    )
                try:
                    size_bytes = path.stat().st_size
                except OSError as error:
                    raise ValueError(
                        "The audio.cpp linked voice sample could not be read."
                    ) from error
                if size_bytes > 5 * 1024 * 1024:
                    raise ValueError(
                        "audio.cpp linked voice references must be at most 5 MiB."
                    )
                content_hash = str(artifact.content_hash or "").strip()
                if not content_hash:
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            digest.update(chunk)
                    content_hash = digest.hexdigest()
                match = (content_hash, path, artifact, sample)
                break

        if match is None:
            return prepared
        content_hash, path, _artifact, sample = match
        with self._audio_cpp_voice_ref_cache_lock:
            encoded = self._audio_cpp_voice_ref_cache.get(content_hash)
            if encoded is None:
                encoded = "data:audio/wav;base64," + base64.b64encode(
                    path.read_bytes()
                ).decode("ascii")
                self._audio_cpp_voice_ref_cache[content_hash] = encoded
                self._audio_cpp_voice_ref_cache.move_to_end(content_hash)
                while len(self._audio_cpp_voice_ref_cache) > 8:
                    self._audio_cpp_voice_ref_cache.popitem(last=False)
            else:
                self._audio_cpp_voice_ref_cache.move_to_end(content_hash)
        prepared["audio_cpp_voice_ref"] = {
            "type": "base64",
            "data": encoded,
        }
        prepared["audio_cpp_voice_ref_hash"] = content_hash
        prepared["audio_cpp_reference_text"] = (
            str(sample.transcript or "").strip() if sample.transcript_reviewed else ""
        )
        return deepcopy(prepared)

    _prepare_audio_cpp_voice_reference = prepare_audio_cpp_voice_reference

    def preview_tts_voice(self, payload, progress, cancel_event):
        """Generate a short managed preview without mutating a session plan."""

        text = str(payload.get("text") or "").strip()
        settings = hydrate_tts_settings(
            self.database,
            self.paths,
            dict(payload.get("settings") or {}),
            manager_bridge=self.manager_bridge,
        )
        if not text:
            raise ValueError("Preview text is required.")
        if cancel_event.is_set():
            return {}
        progress(0.1, "Requesting voice preview")
        urls = self._tts_urls(settings)
        service_id = str(settings.get("preview_service_id") or "").lower()
        api_base = str(settings.get("preview_api_base") or "").strip()
        url_key = {
            "audio_cpp": "audio_cpp_base_url",
            "xtts": "xtts_base_url",
            "voxcpm": "voxcpm_base_url",
            "fishs2": "fishs2_base_url",
            "voxtral": "voxtral_base_url",
            "kokoro": "kokoro_base_url",
            "silero": "silero_base_url",
            "chatterbox": "chatterbox_base_url",
            "kobold_qwen": "kobold_qwen_base_url",
            "magpie": "magpie_base_url",
        }.get(service_id)
        if url_key and api_base:
            urls[url_key] = api_base
        settings = self.prepare_audio_cpp_voice_reference(settings)
        self._ensure_qwen_cloned_voice(
            settings,
            base_url=urls["kobold_qwen_base_url"],
            verified=set(),
            cancel_event=cancel_event,
        )
        audio = self.tts_providers.synthesize(
            text,
            settings,
            max_attempts=int(settings.get("max_attempts") or 5),
            cancel_event=cancel_event,
            retry_callback=lambda attempt, total, delay: progress(
                0.1,
                f"Voice preview retry {attempt} of {total} in {delay:.1f}s",
            ),
            recovery_callback=lambda cycle, total, timeout: progress(
                0.1,
                f"Waiting for Qwen3 TTS to recover ({cycle}/{total}, up to {timeout:.0f}s)",
            ),
            **urls,
        )
        if audio is None:
            raise RuntimeError("The speech service did not return preview audio.")
        preview_identity = {
            "service_id": service_id,
            "model": str(settings.get("model") or ""),
            "voice": str(settings.get("voice") or ""),
            "language": str(settings.get("language") or ""),
        }
        preview_key = hashlib.sha256(
            json.dumps(preview_identity, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        target_dir = self.paths.artifacts / "tts-previews"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{preview_key}.wav"
        exported = audio.export(target, format="wav")
        exported.close()
        artifact = self.artifacts.register(
            target,
            kind="audio",
            role="tts_voice_preview",
            settings=_secret_free_tts_settings(settings),
            metadata={
                **preview_identity,
                "service": settings.get("service"),
                "preview_text": text,
            },
        )
        self._record_tts_usage(
            "",
            settings,
            text,
            len(audio),
            job_id=str(payload.get("_job_id") or "") or None,
            artifact_id=artifact.id,
        )
        progress(1.0, "Preview ready")
        return {"artifact_id": artifact.id, "duration_ms": len(audio)}

    @staticmethod
    def _validate_download_url(raw_url: str) -> str:
        import ipaddress
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(str(raw_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Source URL must use http or https.")
        for _family, _type, _proto, _canon, address in socket.getaddrinfo(
            parsed.hostname, parsed.port or 443
        ):
            ip = ipaddress.ip_address(address[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                raise ValueError("Source URL resolves to a non-public network address.")
        return parsed.geturl()

    def download_source_url(self, payload, progress, cancel_event):
        import yt_dlp

        from .workspace import SourceLibraryService

        session_id = str(payload.get("session_id") or "")
        url = self._validate_download_url(str(payload.get("url") or ""))
        destination_dir = self._session_dir(session_id) / "sources"
        destination_dir.mkdir(parents=True, exist_ok=True)
        progress(0.03, "Inspecting source URL")
        download_fraction = 0.0
        last_reported_fraction = -1.0
        last_reported_bytes = 0
        last_reported_at = 0.0

        def download_progress(status: dict[str, Any]) -> None:
            nonlocal \
                download_fraction, \
                last_reported_fraction, \
                last_reported_bytes, \
                last_reported_at
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadError("Source download was canceled.")
            state = str(status.get("status") or "")
            if state == "downloading":
                downloaded = max(0, int(status.get("downloaded_bytes") or 0))
                total = max(
                    0,
                    int(
                        status.get("total_bytes")
                        or status.get("total_bytes_estimate")
                        or 0
                    ),
                )
                if total:
                    download_fraction = max(
                        download_fraction,
                        min(1.0, downloaded / total),
                    )
                now = time.monotonic()
                should_report = (
                    last_reported_fraction < 0
                    or (total and download_fraction - last_reported_fraction >= 0.005)
                    or (
                        not total
                        and downloaded - last_reported_bytes >= 4 * 1024 * 1024
                    )
                    or now - last_reported_at >= 1.0
                )
                if not should_report:
                    return
                detail = (
                    f"Downloading source — {round(download_fraction * 100)}%"
                    if total
                    else f"Downloading source — {downloaded / (1024 * 1024):.1f} MiB received"
                )
                progress(0.05 + download_fraction * 0.8, detail)
                last_reported_fraction = download_fraction
                last_reported_bytes = downloaded
                last_reported_at = now
            elif state == "finished":
                download_fraction = 1.0
                progress(0.88, "Source download complete; processing media")

        options = {
            "outtmpl": str(destination_dir / "%(title).160B-%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [download_progress],
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            information = downloader.extract_info(url, download=True)
            output = Path(downloader.prepare_filename(information)).resolve()
        if cancel_event.is_set():
            return {}
        if destination_dir.resolve() not in output.parents or not output.is_file():
            raise RuntimeError(
                "Downloaded source was not created in the managed session directory."
            )
        progress(0.93, "Registering downloaded source")
        source_metadata = {
            "original_filename": output.name,
            "source_url": url,
            "downloader": "yt-dlp",
        }
        artifact = self.artifacts.register(
            output,
            kind="source",
            role="upload",
            session_id=session_id,
            metadata=source_metadata,
        )
        with self.database.session() as session:
            session.add(
                SourceRecord(
                    session_id=session_id,
                    kind=output.suffix.lower().lstrip(".") or "url",
                    display_name=output.name,
                    artifact_id=artifact.id,
                    content_hash=artifact.content_hash,
                    metadata_json={"url": url, "downloader": "yt-dlp"},
                )
            )
        library = SourceLibraryService(self.database)
        asset = library.ensure_for_artifact(
            artifact.id,
            display_name=output.name,
            kind=output.suffix.lower().lstrip(".") or "url",
        )
        library.attach(session_id, asset.id)
        progress(1.0, "Source download ready")
        return {"artifact_id": artifact.id, "filename": output.name}

    def reuse_source(self, payload, progress, cancel_event):
        from .workspace import SourceLibraryService

        session_id = str(payload.get("session_id") or "")
        source, source_path = self._resolve_input(str(payload.get("artifact_id") or ""))
        destination_dir = self._session_dir(session_id) / "sources"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{source.id}-{source_path.name}"
        progress(0.2, "Copying reusable source")
        shutil.copy2(source_path, destination)
        if cancel_event.is_set():
            destination.unlink(missing_ok=True)
            return {}
        artifact = self.artifacts.register(
            destination,
            kind="source",
            role="upload",
            session_id=session_id,
            parent_ids=[source.id],
            metadata={"original_filename": source_path.name, "reused_from": source.id},
        )
        with self.database.session() as session:
            session.add(
                SourceRecord(
                    session_id=session_id,
                    kind=destination.suffix.lower().lstrip(".") or "file",
                    display_name=source_path.name,
                    artifact_id=artifact.id,
                    content_hash=artifact.content_hash,
                    metadata_json={"reused_from": source.id},
                )
            )
        library = SourceLibraryService(self.database)
        asset = library.ensure_for_artifact(
            artifact.id,
            display_name=source_path.name,
            kind=destination.suffix.lower().lstrip(".") or "file",
        )
        library.attach(session_id, asset.id)
        progress(1.0, "Reusable source ready")
        return {"artifact_id": artifact.id, "filename": source_path.name}

    def _latest_stage_input(
        self, session_id: str, prerequisite_roles: tuple[str, ...]
    ) -> Artifact | None:
        with self.database.session() as session:
            candidates = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role.in_(prerequisite_roles),
                    )
                    .order_by(Artifact.created_at.desc())
                ).all()
            )
            selected = selected_artifacts(session, session_id, candidates)
            by_role: dict[str, Artifact] = {}
            for item in selected.values():
                if item.role in prerequisite_roles:
                    by_role.setdefault(item.role, item)
            primary_source = resolve_primary_source(session, session_id).artifact
            if primary_source and primary_source.role in prerequisite_roles:
                by_role.setdefault(primary_source.role, primary_source)
            for item in candidates:
                if item.state == "current" and item.role != "upload":
                    by_role.setdefault(item.role, item)
            result = next(
                (by_role[role] for role in prerequisite_roles if role in by_role), None
            )
            if result is not None:
                session.expunge(result)
            return result

    def _persisted_translation_input(
        self, session_id: str, artifact_id: str
    ) -> Artifact | None:
        """Return a safe persisted translation input, never a foreign artifact.

        Forked sessions may use a source attached from the source library, but
        no other cross-session artifact is a valid workflow input.  Invalid
        legacy settings deliberately fall back to the ordinary local
        prerequisite path instead of leaking an artifact across sessions.
        """

        if not artifact_id:
            return None
        with self.database.session() as session:
            candidate = session.get(Artifact, artifact_id)
            attached_ids = set(
                session.scalars(
                    select(Artifact.id)
                    .join(SourceAsset, SourceAsset.artifact_id == Artifact.id)
                    .join(
                        SessionSource,
                        SessionSource.source_asset_id == SourceAsset.id,
                    )
                    .where(SessionSource.session_id == session_id)
                ).all()
            )
            if (
                candidate is None
                or candidate.state == "deleted"
                or candidate.role not in {"transcription", "correction", "upload"}
                or Path(candidate.relative_path).suffix.lower() != ".srt"
                or (
                    candidate.session_id != session_id
                    and candidate.id not in attached_ids
                )
            ):
                return None
            session.expunge(candidate)
            return candidate

    @staticmethod
    def _continuation_input_roles(
        definition_key: str,
        default_roles: tuple[str, ...],
        workflow_kind: str,
        input_choices: dict[str, Any],
        transformations: dict[str, Any],
    ) -> tuple[str, ...]:
        if definition_key == "translate":
            translation_parent = str(input_choices.get("translation") or "correction")
            return (
                ("correction",)
                if translation_parent == "correction"
                else ("transcription", "upload")
            )
        if definition_key not in {"optimize_document", "generate_audio"}:
            return default_roles
        if definition_key == "generate_audio" and bool(
            transformations.get("llm_tts_document_optimization")
        ):
            return ("tts_optimized",)
        if workflow_kind == "audiobook":
            return ("prepared_text",)
        generation_parent = str(input_choices.get("generation") or "translation")
        return {
            "translation": ("translation",),
            "correction": ("correction",),
            "source": ("transcription", "upload"),
        }.get(generation_parent, default_roles)

    def continue_workflow(self, payload, progress, cancel_event):
        """Run only missing/stale included prerequisites, then the requested outcome stage."""
        from .workflows import AUDIOBOOK_STAGES, DUBBING_STAGES

        session_id = str(payload.get("session_id") or "")
        target_key = str(payload.get("target_stage") or "generate_audio")
        record = self._session_record(session_id)
        definitions = (
            AUDIOBOOK_STAGES if record.workflow_kind == "audiobook" else DUBBING_STAGES
        )
        is_srt_source = False
        if record.workflow_kind != "audiobook":
            upload = self._latest_stage_input(session_id, ("upload",))
            filename = (
                str(
                    (upload.metadata_json or {}).get("original_filename")
                    or upload.relative_path
                ).lower()
                if upload
                else ""
            )
            is_srt_source = filename.endswith(".srt")
            if is_srt_source:
                definitions = tuple(
                    item for item in definitions if item.key != "transcribe"
                )
        target_index = next(
            (index for index, item in enumerate(definitions) if item.key == target_key),
            None,
        )
        if target_index is None:
            raise ValueError(f"Unknown continuation stage: {target_key}")
        included = set(record.included_stages_json or [])
        with self.database.session() as session:
            outcome = session.scalar(
                select(OutcomePlan).where(OutcomePlan.session_id == session_id)
            )
            outcome_value = dict(outcome.value_json or {}) if outcome else {}
            translation_setting = session.get(
                SessionSetting,
                (session_id, "translation"),
            )
            persisted_translation_settings = (
                dict(translation_setting.value_json or {})
                if translation_setting is not None
                and isinstance(translation_setting.value_json, dict)
                else {}
            )
        input_choices = (
            outcome_value.get("inputs")
            if isinstance(outcome_value.get("inputs"), dict)
            else {}
        )
        transformations = (
            outcome_value.get("transformations")
            if isinstance(outcome_value.get("transformations"), dict)
            else {}
        )
        stage_settings = (
            payload.get("stage_settings")
            if isinstance(payload.get("stage_settings"), dict)
            else {}
        )
        direct_settings = (
            payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
        )
        reuse_stages = {
            str(value) for value in (payload.get("reuse_stages") or []) if str(value)
        }
        required = self._continuation_required_stages(
            record.workflow_kind,
            target_key,
            is_srt_source,
            input_choices,
            transformations,
        )
        runnable = [
            item
            for index, item in enumerate(definitions)
            if index <= target_index
            and item.executable
            and item.job_kind
            and (item.key in included or item.key in required)
        ]
        produced: list[dict[str, Any]] = []
        handlers = self.handler_registry
        stage_weights = {
            "clean_source": 0.12,
            "transcribe": 0.18,
            "correct": 0.10,
            "translate": 0.10,
            "optimize_document": 0.10,
            "prepare_text": 0.05,
            # Speech synthesis is normally the dominant part of this action.
            "generate_audio": 0.65,
            "export": 0.10,
        }
        weights = [stage_weights.get(item.key, 0.08) for item in runnable]
        weight_total = sum(weights) or 1.0
        completed_weight = 0.0
        for index, definition in enumerate(runnable):
            if cancel_event.is_set():
                return {"artifacts": produced}
            settings = (
                stage_settings.get(definition.key)
                if isinstance(stage_settings.get(definition.key), dict)
                else {}
            )
            if definition.key == target_key:
                settings = {**settings, **direct_settings}
            if definition.key == "generate_audio":
                settings["llm_tts_optimization"] = bool(
                    transformations.get("llm_tts_optimization")
                )
            with self.database.session() as session:
                existing = (
                    selected_artifacts(session, session_id).get(
                        canonical_stage_key(definition.key)
                    )
                    if definition.output_role
                    else None
                )
            input_roles = self._continuation_input_roles(
                definition.key,
                definition.prerequisite_roles,
                record.workflow_kind,
                input_choices,
                transformations,
            )
            source = None
            if definition.key == "translate":
                for persisted_source_id in (
                    str(settings.get("source_artifact_id") or ""),
                    str(persisted_translation_settings.get("source_artifact_id") or ""),
                ):
                    if source is None and persisted_source_id:
                        source = self._persisted_translation_input(
                            session_id,
                            persisted_source_id,
                        )
            if source is None:
                source = self._latest_stage_input(session_id, input_roles)
            if definition.prerequisite_roles and source is None:
                raise ValueError(
                    f"Stage '{definition.key}' is missing a required input artifact."
                )
            if existing is not None and definition.key != target_key:
                if definition.key in reuse_stages:
                    # The caller explicitly chose to keep the current artifact
                    # even though settings or source lineage changed. This is
                    # meaningful only for prerequisites; the target stage is
                    # always run by the continuation request.
                    continue
                freshness = self._continuation_freshness(
                    session_id,
                    definition.key,
                    settings,
                    existing,
                    source,
                )
                if freshness["settings_match"] and freshness["source_match"]:
                    continue
            handler = handlers[definition.job_kind]
            width = weights[index] / weight_total
            start = completed_weight / weight_total

            def stage_progress(value, detail=None, start=start, width=width):
                progress(
                    min(0.99, start + max(0.0, min(1.0, float(value))) * width),
                    detail,
                )

            handler_payload = {
                "session_id": session_id,
                "source_artifact_id": source.id if source else None,
                "settings": settings,
                "_job_id": str(payload.get("_job_id") or "") or None,
            }
            requested_agent_runs = payload.get("_agent_run_ids")
            if isinstance(requested_agent_runs, dict):
                run_kind = {
                    "correct": "correction",
                    "translate": "translation",
                    "optimize_document": "tts_optimization",
                }.get(definition.key)
                if run_kind and requested_agent_runs.get(run_kind):
                    handler_payload["_agent_run_id"] = str(
                        requested_agent_runs[run_kind]
                    )
            if definition.key == "export":
                handler_payload["export_contract"] = payload.get("export_contract")
                handler_payload["resolved_settings_snapshot"] = payload.get(
                    "resolved_settings_snapshot"
                )
            if definition.key == "generate_audio":
                result = self._run_reviewable_generation(
                    handler_payload,
                    stage_progress,
                    cancel_event,
                    resolved_snapshot=payload.get("resolved_settings_snapshot"),
                    settings_hash=str(payload.get("settings_hash") or "") or None,
                    job_id=str(payload.get("_job_id") or "") or None,
                )
            else:
                result = handler(handler_payload, stage_progress, cancel_event)
            if result:
                produced.append({"stage": definition.key, **result})
            completed_weight += weights[index]
            if definition.key == "generate_audio" and str(
                (result or {}).get("status") or ""
            ) in {"paused", "canceled"}:
                return {"artifacts": produced, "target_stage": target_key}
        progress(1.0, "Workflow continuation finished")
        return {"artifacts": produced, "target_stage": target_key}

    @staticmethod
    def _continuation_required_stages(
        workflow_kind: str,
        target_key: str,
        is_srt_source: bool,
        input_choices: dict[str, Any],
        transformations: dict[str, Any],
    ) -> set[str]:
        required = {target_key}
        if workflow_kind == "audiobook" and target_key in {"generate_audio", "export"}:
            required.update({"clean_source", "prepare_text"})
        elif target_key in {"generate_audio", "export"}:
            if not is_srt_source:
                required.add("transcribe")
            translation_parent = str(input_choices.get("translation") or "correction")
            generation_parent = str(input_choices.get("generation") or "translation")
            translation_required = (
                bool(transformations.get("translation"))
                or generation_parent == "translation"
            )
            if (
                bool(transformations.get("correction"))
                or generation_parent == "correction"
                or (translation_required and translation_parent == "correction")
            ):
                required.add("correct")
            if translation_required:
                required.add("translate")
        if bool(
            transformations.get("llm_tts_document_optimization")
        ) and target_key in {"generate_audio", "export"}:
            required.add("optimize_document")
        return required

    def _current_stage_fingerprint(
        self, stage_key: str, settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Fingerprint of the settings a stage would run with right now.

        LLM-backed stages are hydrated first so the effective (default) model
        is compared instead of the raw request shape.  ``None`` means the
        fingerprint cannot be computed (for example the provider is no longer
        configured), in which case the artifact must simply be reused.
        """
        backend = (
            str(settings.get("translation_backend") or settings.get("backend") or "llm")
            .strip()
            .lower()
        )
        if stage_key == "translate" and backend == "deepl":
            return _stage_settings_fingerprint(stage_key, settings)
        stage_alias = "correction" if stage_key == "correct" else "translation"
        try:
            hydrated = self._with_database_llm_settings(dict(settings), stage_alias)
        except ValueError:
            return None
        return _stage_settings_fingerprint(stage_key, hydrated)

    def _continuation_freshness(
        self,
        session_id: str,
        stage_key: str,
        settings: dict[str, Any],
        existing: Artifact | None,
        source: Artifact | None,
    ) -> dict[str, Any]:
        """Return the exact prerequisite-reuse decision and its UI reasons."""

        if existing is None:
            return {
                "settings_match": False,
                "source_match": False,
                "reasons": ["missing_artifact"],
                "changed_fields": [],
                "stored": None,
                "current": None,
            }
        expected_settings_hash = hashlib.sha256(
            json.dumps(
                settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        metadata = (
            existing.metadata_json if isinstance(existing.metadata_json, dict) else {}
        )
        expected_hashes = {expected_settings_hash}
        raw_settings_match = bool(
            existing.settings_hash == expected_settings_hash
            or str(metadata.get("requested_settings_hash") or "")
            == expected_settings_hash
        )
        if not raw_settings_match and stage_key in {"correct", "translate"}:
            stage_alias = "correction" if stage_key == "correct" else "translation"
            try:
                hydrated = self._with_database_llm_settings(dict(settings), stage_alias)
            except ValueError:
                hydrated = None
            if hydrated is not None:
                expected_hashes.add(
                    hashlib.sha256(
                        json.dumps(
                            hydrated,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                )
        fallback = bool(
            existing.settings_hash in expected_hashes
            or str(metadata.get("requested_settings_hash") or "")
            == expected_settings_hash
        )
        stored = metadata.get("settings_fingerprint")
        current = None
        changed_fields: list[str] = []
        settings_match = fallback
        settings_reason = None
        if (
            stage_key in {"correct", "translate"}
            and isinstance(stored, dict)
            and stored
        ):
            current = self._current_stage_fingerprint(stage_key, settings)
            if current is None:
                # Keep the current artifact if the old provider configuration
                # cannot be reconstructed. This is the continuation behavior
                # that preflight must mirror.
                settings_match = True
            else:
                settings_match = stored == current
                if not settings_match:
                    settings_reason = "settings_changed"
                    changed_fields = sorted(
                        key
                        for key in set(stored) | set(current)
                        if stored.get(key) != current.get(key)
                    )
        elif not settings_match:
            # A legacy artifact can still be safely reused when its exact raw
            # hash matches. Without that evidence, continuation will rerun it.
            settings_reason = "settings_unverifiable"

        source_match = source is None
        if source is not None:
            source_match = str(
                metadata.get("source_artifact_id") or ""
            ) == source.id or (
                stage_key != "translate"
                and bool(source.content_hash)
                and str(metadata.get("source_content_hash") or "")
                == source.content_hash
            )
            if not source_match:
                with self.database.session() as session:
                    source_match = (
                        session.get(ArtifactEdge, (source.id, existing.id)) is not None
                    )
        reasons = [reason for reason in (settings_reason,) if reason]
        if not source_match:
            reasons.append("source_lineage_changed")
        return {
            "settings_match": settings_match,
            "source_match": source_match,
            "reasons": reasons,
            "changed_fields": changed_fields,
            "stored": stored if isinstance(stored, dict) else None,
            "current": current,
        }

    def settings_mismatches(
        self, session_id: str, target_stage: str = "generate_audio"
    ) -> list[dict[str, Any]]:
        """Report every prerequisite that continuation would rerun today.

        The response preserves the legacy ``stage`` and ``changed_fields``
        contract, while ``reasons`` distinguishes semantic changes from a
        legacy hash that cannot prove freshness and from broken source lineage.
        """
        from .workflows import AUDIOBOOK_STAGES, DUBBING_STAGES
        from .workspace import WorkspaceSettingsService, adapt_runtime_settings

        record = self._session_record(session_id)
        upload = self._latest_stage_input(session_id, ("upload",))
        filename = (
            str(
                (upload.metadata_json or {}).get("original_filename")
                or upload.relative_path
            ).lower()
            if upload
            else ""
        )
        definitions = (
            AUDIOBOOK_STAGES if record.workflow_kind == "audiobook" else DUBBING_STAGES
        )
        if record.workflow_kind != "audiobook" and filename.endswith(".srt"):
            definitions = tuple(
                definition
                for definition in definitions
                if definition.key != "transcribe"
            )
        target_index = next(
            (
                index
                for index, definition in enumerate(definitions)
                if definition.key == target_stage
            ),
            None,
        )
        if target_index is None:
            raise ValueError(f"Unknown continuation stage: {target_stage}")
        with self.database.session() as session:
            outcome = session.scalar(
                select(OutcomePlan).where(OutcomePlan.session_id == session_id)
            )
            outcome_value = dict(outcome.value_json or {}) if outcome else {}
            selected = selected_artifacts(session, session_id)
            translation_setting = session.get(
                SessionSetting,
                (session_id, "translation"),
            )
            persisted_translation_settings = (
                dict(translation_setting.value_json or {})
                if translation_setting is not None
                and isinstance(translation_setting.value_json, dict)
                else {}
            )
        input_choices = (
            outcome_value.get("inputs")
            if isinstance(outcome_value.get("inputs"), dict)
            else {}
        )
        transformations = (
            outcome_value.get("transformations")
            if isinstance(outcome_value.get("transformations"), dict)
            else {}
        )
        required = self._continuation_required_stages(
            record.workflow_kind,
            target_stage,
            filename.endswith(".srt"),
            input_choices,
            transformations,
        )
        included = set(record.included_stages_json or [])
        runnable = [
            definition
            for index, definition in enumerate(definitions)
            if index < target_index
            and definition.executable
            and definition.job_kind
            and (definition.key in included or definition.key in required)
        ]
        section_map: dict[str, tuple[str, ...]] = {
            "clean_source": ("source_cleaning", "text"),
            "transcribe": ("stt", "subtitles"),
            "correct": ("correction", "subtitles"),
            "translate": ("translation", "subtitles"),
            "optimize_document": ("text",),
            "prepare_text": ("text", "audio"),
            "generate_audio": ("text", "tts", "audio", "rvc", "output"),
            "export": ("output", "audio", "subtitles"),
        }
        settings_service = WorkspaceSettingsService(self.database)
        sections = list(
            dict.fromkeys(
                section
                for definition in runnable
                for section in section_map.get(definition.key, ())
            )
        )
        resolved, _ = settings_service.resolve(session_id, sections)
        mismatches: list[dict[str, Any]] = []
        for definition in runnable:
            stage_settings: dict[str, Any] = {}
            for section in section_map.get(definition.key, ()):
                stage_settings.update(
                    adapt_runtime_settings(section, resolved.get(section, {}))
                )
            input_roles = self._continuation_input_roles(
                definition.key,
                definition.prerequisite_roles,
                record.workflow_kind,
                input_choices,
                transformations,
            )
            source = None
            if definition.key == "translate":
                for persisted_source_id in (
                    str(stage_settings.get("source_artifact_id") or ""),
                    str(persisted_translation_settings.get("source_artifact_id") or ""),
                ):
                    if source is None and persisted_source_id:
                        source = self._persisted_translation_input(
                            session_id,
                            persisted_source_id,
                        )
            if source is None:
                source = self._latest_stage_input(session_id, input_roles)
            existing = selected.get(canonical_stage_key(definition.key))
            if existing is None:
                continue
            freshness = self._continuation_freshness(
                session_id,
                definition.key,
                stage_settings,
                existing,
                source,
            )
            if freshness["settings_match"] and freshness["source_match"]:
                continue
            mismatches.append(
                {
                    "stage": definition.key,
                    "changed_fields": freshness["changed_fields"],
                    "reasons": freshness["reasons"],
                    "stored": freshness["stored"],
                    "current": freshness["current"],
                }
            )
        return mismatches

    def _session_dir(self, session_id: str) -> Path:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise ValueError(f"Session not found: {session_id}")
            path = self.paths.sessions / record.storage_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _operation_dir(self, session_id: str, stage_key: str) -> Path:
        """Allocate a unique directory so reruns never overwrite prior files."""
        path = self._session_dir(session_id) / "stage-runs" / f"{stage_key}-{new_id()}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _session_record(self, session_id: str) -> SessionRecord:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise ValueError(f"Session not found: {session_id}")
            session.expunge(record)
            return record

    def _resolve_input(self, artifact_id: str) -> tuple[Artifact, Path]:
        artifact, path = self.artifacts.resolve(artifact_id)
        if not path.is_file():
            raise FileNotFoundError(path)
        return artifact, path

    @staticmethod
    def _is_subtitle_generation_record(record: dict[str, Any]) -> bool:
        """Identify generation units whose timing comes from subtitle cues."""
        return str(record.get("node_kind") or "") == "subtitle_cue" or bool(
            record.get("subtitles")
        )

    @staticmethod
    def _usable_language(value: Any) -> str:
        normalized = str(value or "").strip()
        return (
            "" if normalized.lower() in {"", "auto", "und", "unknown"} else normalized
        )

    def _generation_language(
        self,
        session_id: str,
        source_artifact: Artifact,
        settings: dict[str, Any],
    ) -> str:
        """Resolve TTS language from the artifact actually selected for generation."""
        language = self._usable_language(
            (source_artifact.metadata_json or {}).get("language")
        )
        role = str(source_artifact.role or "")

        # Whole-document speech optimization preserves the language and role of
        # its source. Older JSON optimization artifacts did not copy language,
        # so follow their explicit source link once for compatibility.
        if not language and role == "tts_optimized":
            parent_id = str(
                (source_artifact.metadata_json or {}).get("source_artifact_id") or ""
            )
            if parent_id:
                with self.database.session() as session:
                    parent = session.get(Artifact, parent_id)
                    if parent is not None:
                        language = self._usable_language(
                            (parent.metadata_json or {}).get("language")
                        )
                        role = str(parent.role or role)

        record = self._session_record(session_id)
        if not language and role == "translation":
            language = self._usable_language(record.target_language)
        if not language and role in {
            "transcription",
            "correction",
            "upload",
            "prepared_text",
            "source",
        }:
            language = self._usable_language(record.source_language)
        if not language:
            language = self._usable_language(
                settings.get("language") or settings.get("target_language")
            )
        return language or "en"

    def _subtitle_speaker_map(
        self,
        artifact: Artifact,
        source_path: Path | None = None,
    ) -> dict[int, str]:
        """Resolve cue speakers without exposing them as subtitle text."""

        mapping: dict[int, str] = {}
        with self.database.session() as session:
            managed = session.get(Artifact, artifact.id)
            revision_id = str(
                (
                    (
                        managed.metadata_json
                        if managed is not None
                        else artifact.metadata_json
                    )
                    or {}
                ).get("revision_id")
                or ""
            )
        if revision_id:
            with self.database.session() as session:
                records = list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == revision_id)
                        .order_by(Segment.ordinal)
                    ).all()
                )
            for record in records:
                speaker = _structured_speaker(record)
                if speaker:
                    mapping[record.ordinal + 1] = speaker

        try:
            if source_path is None:
                _record, source_path = self.artifacts.resolve(artifact.id)
            if source_path.suffix.lower() == ".srt":
                from pandrator.logic.dubbing.srt_utils import parse_srt

                for segment in parse_srt(source_path.read_text(encoding="utf-8-sig")):
                    if segment.speaker and segment.index not in mapping:
                        mapping[segment.index] = segment.speaker
        except (KeyError, OSError):
            logger.warning(
                "Could not resolve speaker metadata for artifact %s", artifact.id
            )
        return mapping

    def _subtitle_generation_records(
        self,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        language: str,
    ) -> tuple[list[dict[str, Any]], str | None, Artifact]:
        """Build one speaker-safe partition for display and speech text."""
        from pandrator.logic.dubbing.speech_blocks import create_speech_blocks
        from pandrator.logic.dubbing.srt_utils import parse_srt

        (
            min_chars,
            max_chars,
            merge_threshold,
            continuation_threshold,
            max_internal_gap,
        ) = _speech_block_settings(settings)
        display_artifact = source_artifact
        display_path = source_path
        display_segments = None
        speech_segments = None
        plan_by_position: dict[int, dict[str, Any]] = {}

        if source_artifact.role == "tts_optimized":
            display_artifact_id = str(
                (source_artifact.metadata_json or {}).get("source_artifact_id") or ""
            )
            if display_artifact_id:
                candidate_artifact, candidate_path = self._resolve_input(
                    display_artifact_id
                )
                if candidate_path.suffix.lower() == ".srt":
                    display_artifact = candidate_artifact
                    display_path = candidate_path
                    display_segments = parse_srt(
                        display_path.read_text(encoding="utf-8-sig")
                    )
                    speech_segments = parse_srt(
                        source_path.read_text(encoding="utf-8-sig")
                    )
                    if [item.index for item in display_segments] != [
                        item.index for item in speech_segments
                    ]:
                        raise ValueError(
                            "The reviewed speech revision no longer aligns with "
                            "its display subtitles."
                        )
                    plan_artifact_id = str(
                        (source_artifact.metadata_json or {}).get(
                            "speech_plan_artifact_id"
                        )
                        or ""
                    )
                    if plan_artifact_id:
                        _plan_artifact, plan_path = self._resolve_input(
                            plan_artifact_id
                        )
                        plan_rows = json.loads(
                            plan_path.read_text(encoding="utf-8-sig")
                        )
                        if isinstance(plan_rows, list):
                            for position, row in enumerate(plan_rows):
                                if isinstance(row, dict):
                                    try:
                                        plan_position = int(row.get("index", position))
                                    except (TypeError, ValueError):
                                        plan_position = position
                                    plan_by_position[plan_position] = dict(
                                        row.get("speech_plan") or {}
                                    )

        speaker_by_subtitle = self._subtitle_speaker_map(
            display_artifact,
            display_path,
        )
        if display_segments is None:
            display_segments = parse_srt(display_path.read_text(encoding="utf-8-sig"))
        blocks = create_speech_blocks(
            display_path.read_text(encoding="utf-8-sig"),
            target_language=language,
            min_chars=min_chars,
            max_chars=max_chars,
            merge_threshold=merge_threshold,
            continuation_threshold_ms=continuation_threshold,
            max_internal_gap_ms=max_internal_gap,
            speech_srt_content=(
                source_path.read_text(encoding="utf-8-sig")
                if speech_segments is not None
                else None
            ),
            **(
                {"speaker_by_subtitle": speaker_by_subtitle}
                if speaker_by_subtitle
                else {}
            ),
        )
        if not blocks:
            raise ValueError("No dubbing speech blocks were produced.")

        position_by_index = {
            item.index: position for position, item in enumerate(display_segments)
        }
        records: list[dict[str, Any]] = []
        for block in blocks:
            subtitle_ids = [int(value) for value in block.get("subtitles") or []]
            record = {
                **{
                    key: value
                    for key, value in block.items()
                    if not str(key).startswith("_")
                },
                "source_segment_ids": subtitle_ids,
                "node_kind": "subtitle_cue",
                "language": language,
            }
            if speech_segments is not None:
                optimized_text = str(block.get("_optimized_text") or "").strip()
                record["tts_optimized_sentence"] = optimized_text
                cue_plans = [
                    {
                        "subtitle": index,
                        "speech_plan": plan_by_position.get(
                            position_by_index.get(index, -1),
                            {},
                        ),
                    }
                    for index in subtitle_ids
                    if plan_by_position.get(position_by_index.get(index, -1))
                ]
                if len(subtitle_ids) == 1 and cue_plans:
                    record["speech_plan"] = cue_plans[0]["speech_plan"]
                elif cue_plans:
                    record["speech_plan"] = {
                        "version": 1,
                        "status": "reviewed_aggregate",
                        "mode_used": "document",
                        "compiled_text": optimized_text,
                        "cue_plans": cue_plans,
                    }
            records.append(record)

        source_revision_id = (
            str((display_artifact.metadata_json or {}).get("revision_id") or "") or None
        )
        return records, source_revision_id, display_artifact

    def _materialize_subtitle_generation_plan(
        self,
        session_id: str,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        language: str,
    ) -> str:
        """Create a new versioned plan revision only when its topology changed."""
        with self.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            previous_revision_id = plan.active_revision_id if plan else None

        records, source_revision_id, display_artifact = (
            self._subtitle_generation_records(
                source_artifact,
                source_path,
                settings,
                language,
            )
        )
        revision_id, _segment_ids = self._store_generation_plan(
            session_id,
            records,
            settings=settings,
            source_revision_id=source_revision_id,
            source_artifact_id=source_artifact.id,
        )
        if revision_id != previous_revision_id:
            destination = _next_available_path(
                self._operation_dir(session_id, "speech-blocks")
                / f"{source_path.stem}-speech-blocks.json"
            )
            destination.write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            parent_ids = [source_artifact.id]
            if display_artifact.id != source_artifact.id:
                parent_ids.append(display_artifact.id)
            self.artifacts.register(
                destination,
                kind="json",
                role="speech_blocks",
                session_id=session_id,
                parent_ids=parent_ids,
                settings=settings,
                metadata={
                    "generation_plan_revision_id": revision_id,
                    "source_artifact_id": source_artifact.id,
                    "display_artifact_id": display_artifact.id,
                    "segment_count": len(records),
                    **_generation_segmentation_settings(settings),
                },
            )
        return revision_id

    def _generation_source_for_plan_refresh(
        self,
        session_id: str,
    ) -> Artifact | None:
        """Resolve the currently selected source, with legacy plan fallbacks."""
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            outcome = session.scalar(
                select(OutcomePlan).where(OutcomePlan.session_id == session_id)
            )
            outcome_value = (
                dict(outcome.value_json or {})
                if outcome and isinstance(outcome.value_json, dict)
                else {}
            )
            inputs = (
                outcome_value.get("inputs")
                if isinstance(outcome_value.get("inputs"), dict)
                else {}
            )
            transformations = (
                outcome_value.get("transformations")
                if isinstance(outcome_value.get("transformations"), dict)
                else {}
            )
            generation_input = (
                str(inputs.get("generation") or "translation").strip().lower()
            )
            has_explicit_source_choice = bool(
                str(inputs.get("generation") or "").strip()
            ) or bool(transformations.get("llm_tts_document_optimization"))
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            active = (
                session.get(GenerationPlanRevision, plan.active_revision_id)
                if plan and plan.active_revision_id
                else None
            )
            stored_source_id = str(
                ((active.settings_json if active else {}) or {}).get(
                    "_source_artifact_id"
                )
                or ""
            )
            source_revision_id = str(
                (active.source_revision_id if active else "") or ""
            )

        if stored_source_id and not has_explicit_source_choice:
            try:
                source, _path = self._resolve_input(stored_source_id)
                return source
            except KeyError:
                pass
        if record.workflow_kind != "audiobook":
            if bool(transformations.get("llm_tts_document_optimization")):
                roles = ("tts_optimized",)
            elif generation_input == "correction":
                roles = ("correction",)
            elif generation_input == "source":
                roles = ("transcription", "upload")
            else:
                roles = ("translation",)
            selected = self._latest_stage_input(session_id, roles)
            if selected is not None:
                return selected

        if stored_source_id:
            try:
                source, _path = self._resolve_input(stored_source_id)
                return source
            except KeyError:
                pass
        if source_revision_id:
            with self.database.session() as session:
                candidates = list(
                    session.scalars(
                        select(Artifact)
                        .where(
                            Artifact.session_id == session_id,
                            Artifact.state != "deleted",
                        )
                        .order_by(Artifact.created_at.desc())
                    ).all()
                )
                for candidate in candidates:
                    if (
                        str((candidate.metadata_json or {}).get("revision_id") or "")
                        == source_revision_id
                    ):
                        session.expunge(candidate)
                        return candidate
        return None

    def refresh_generation_plan(
        self,
        session_id: str,
        resolved_snapshot: dict[str, Any],
    ) -> str | None:
        """Apply current subtitle segmentation settings before a full new run."""
        source_artifact = self._generation_source_for_plan_refresh(session_id)
        if source_artifact is None:
            return None
        source_artifact, source_path = self._resolve_input(source_artifact.id)
        resolved_snapshot["source_artifact_id"] = source_artifact.id
        resolved_snapshot["text"] = {
            **dict(resolved_snapshot.get("text") or {}),
            "use_existing_speech_plans": source_artifact.role == "tts_optimized",
        }
        if source_path.suffix.lower() != ".srt":
            return None

        from .workspace import adapt_runtime_settings

        settings: dict[str, Any] = {}
        for section in ("text", "subtitles", "tts", "audio", "rvc", "output"):
            value = resolved_snapshot.get(section)
            if isinstance(value, dict):
                settings.update(adapt_runtime_settings(section, value))
        language = self._generation_language(
            session_id,
            source_artifact,
            settings,
        )
        settings = {
            **settings,
            "language": language,
            "target_language": language,
        }
        return self._materialize_subtitle_generation_plan(
            session_id,
            source_artifact,
            source_path,
            settings,
            language,
        )

    def _store_srt_document(
        self,
        session_id: str,
        artifact: Artifact,
        stage: str,
        *,
        language: str | None = None,
        parent_artifact: Artifact | None = None,
        speaker_overrides: dict[int, str] | None = None,
    ) -> tuple[str, str]:
        from pandrator.logic.dubbing.srt_utils import parse_srt

        _record, path = self.artifacts.resolve(artifact.id)
        segments = parse_srt(path.read_text(encoding="utf-8-sig"))
        parent_revision_id = ""
        if parent_artifact:
            with self.database.session() as session:
                managed_parent = session.get(Artifact, parent_artifact.id)
                parent_revision_id = str(
                    (
                        (
                            managed_parent.metadata_json
                            if managed_parent is not None
                            else parent_artifact.metadata_json
                        )
                        or {}
                    ).get("revision_id")
                    or ""
                )
        parent_file_segments: list[Any] = []
        if parent_artifact and not parent_revision_id:
            try:
                _parent_record, parent_path = self.artifacts.resolve(parent_artifact.id)
                if parent_path.suffix.lower() == ".srt":
                    parent_file_segments = parse_srt(
                        parent_path.read_text(encoding="utf-8-sig")
                    )
            except (KeyError, OSError):
                logger.warning(
                    "Could not load parent subtitle metadata for artifact %s",
                    parent_artifact.id,
                )

        with self.database.session() as session:
            parents = (
                list(
                    session.scalars(
                        select(Segment)
                        .where(Segment.revision_id == parent_revision_id)
                        .order_by(Segment.ordinal)
                    ).all()
                )
                if parent_revision_id
                else []
            )
            speaker_candidates: list[Any] = parents or parent_file_segments
            resolved_segments = []
            speaker_sources: list[str] = []
            for item in segments:
                reviewed_speaker = str(
                    (speaker_overrides or {}).get(item.index) or ""
                ).strip()
                inherited_speaker = str(
                    item.speaker
                    or _dominant_speaker(
                        item.start_ms,
                        item.end_ms,
                        speaker_candidates,
                    )
                    or ""
                ).strip()
                resolved_segments.append(
                    replace(
                        item,
                        speaker=reviewed_speaker or inherited_speaker,
                    )
                )
                speaker_sources.append(
                    "model_reviewed" if reviewed_speaker else "timing_inherited"
                )
            document = Document(session_id=session_id, stage=stage, language=language)
            session.add(document)
            session.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_number=1,
                content_hash=_hash_segments(resolved_segments),
            )
            session.add(revision)
            session.flush()
            child_records: list[Segment] = []
            for ordinal, item in enumerate(resolved_segments):
                child = Segment(
                    revision_id=revision.id,
                    ordinal=ordinal,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text=item.text,
                    speaker=item.speaker or None,
                    metadata_json={
                        "speaker_source": speaker_sources[ordinal],
                    },
                )
                session.add(child)
                child_records.append(child)
            session.flush()
            document.active_revision_id = revision.id

            for child in child_records:
                overlaps = [
                    parent
                    for parent in parents
                    if child.start_ms is not None
                    and child.end_ms is not None
                    and parent.start_ms is not None
                    and parent.end_ms is not None
                    and min(child.end_ms, parent.end_ms)
                    > max(child.start_ms, parent.start_ms)
                ]
                for sequence, parent in enumerate(overlaps):
                    session.add(
                        SegmentLineage(
                            parent_segment_id=parent.id,
                            child_segment_id=child.id,
                            relation="temporal_overlap",
                            sequence=sequence,
                        )
                    )

            managed = session.get(Artifact, artifact.id)
            speakers = {
                child.speaker.casefold(): child.speaker
                for child in child_records
                if child.speaker
            }
            managed.metadata_json = {
                **(managed.metadata_json or {}),
                "document_id": document.id,
                "revision_id": revision.id,
                "stage": stage,
                "language": language,
                "has_speaker_metadata": bool(speakers),
                "speaker_count": len(speakers),
                "speaker_reviewed_by_model": any(
                    source == "model_reviewed" for source in speaker_sources
                ),
            }
            return document.id, revision.id

    def _store_timed_words(self, revision_id: str, metadata_path: Path) -> int:
        transcript = load_transcript(metadata_path)
        words = [
            {
                "text": word.text,
                "start_ms": word.start_ms,
                "end_ms": word.end_ms,
                "speaker": word.speaker or None,
                "confidence": word.confidence,
                "metadata": dict(word.metadata),
            }
            for word in transcript.words
        ]
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == revision_id)
                    .order_by(Segment.ordinal)
                ).all()
            )
            for ordinal, word in enumerate(words):
                owner = next(
                    (
                        segment
                        for segment in segments
                        if segment.start_ms is not None
                        and segment.end_ms is not None
                        and min(segment.end_ms, word["end_ms"])
                        > max(segment.start_ms, word["start_ms"])
                    ),
                    None,
                )
                session.add(
                    TimedWord(
                        revision_id=revision_id,
                        segment_id=owner.id if owner else None,
                        ordinal=ordinal,
                        text=word["text"],
                        start_ms=word["start_ms"],
                        end_ms=word["end_ms"],
                        speaker=word["speaker"],
                        confidence=(
                            float(word["confidence"])
                            if word["confidence"] is not None
                            else None
                        ),
                        metadata_json=word["metadata"],
                    )
                )

            speaker_candidates: list[Any] = [
                word for word in transcript.words if word.speaker
            ] or [item for item in transcript.segments if item.speaker]
            for segment in segments:
                if segment.start_ms is None or segment.end_ms is None:
                    continue
                speaker = _dominant_speaker(
                    segment.start_ms,
                    segment.end_ms,
                    speaker_candidates,
                )
                if speaker:
                    segment.speaker = speaker
            revision = session.get(DocumentRevision, revision_id)
            if revision is not None:
                revision.content_hash = _hash_segments(segments)
        return len(words)

    @staticmethod
    def _llm_usage_is_commercial(
        settings: dict[str, Any], model: str, has_price: bool
    ) -> bool:
        if has_price:
            return True
        if (
            str(
                settings.get("translation_backend") or settings.get("backend") or ""
            ).lower()
            == "deepl"
        ):
            return True
        provider = model.split("/", 1)[0].lower() if "/" in model else ""
        if provider in {"ollama", "lm_studio", "local", "custom_local"}:
            return False
        for record in settings.get("llm_provider_configs", []):
            if not isinstance(record, dict):
                continue
            models = {str(item) for item in record.get("models", [])}
            if model not in models and str(record.get("default_model") or "") != model:
                continue
            api_base = str(record.get("api_base") or "").lower()
            if any(host in api_base for host in ("127.0.0.1", "localhost", "0.0.0.0")):
                return False
            return str(record.get("kind") or "commercial").lower() != "local"
        return bool(provider and provider not in {"ollama", "local"})

    def _record_usage(
        self,
        session_id: str,
        stage: str,
        settings: dict[str, Any],
        result,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
        generation_run_id: str | None = None,
        agent_run_id: str | None = None,
        request_key: str | None = None,
    ) -> None:
        sources = tuple(getattr(result, "cost_sources", ()) or ())
        single_source = str(getattr(result, "cost_source", "") or "")
        if single_source and single_source not in sources:
            sources = (*sources, single_source)
        raw_cost = getattr(result, "cost", None)
        cost = (
            float(raw_cost)
            if raw_cost is not None and (sources or float(raw_cost) != 0.0)
            else None
        )
        response_count = int(getattr(result, "response_count", 0) or 0)
        raw_usage = getattr(result, "usage", {})
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        if cost is None and not response_count and not usage:
            return
        model = str(
            settings.get(f"{stage}_model")
            or settings.get("model_name")
            or settings.get("llm_default_model")
            or settings.get("default_model")
            or "default"
        )
        if (
            stage == "translation"
            and str(
                settings.get("translation_backend") or settings.get("backend") or ""
            ).lower()
            == "deepl"
        ):
            model = "deepl"
        provider = model.split("/", 1)[0] if "/" in model else "default"
        commercial = self._llm_usage_is_commercial(settings, model, cost is not None)
        with self.database.session() as session:
            event = None
            if agent_run_id and request_key:
                event = session.scalar(
                    select(UsageEvent).where(
                        UsageEvent.agent_run_id == agent_run_id,
                        UsageEvent.request_key == request_key,
                    )
                )
            if event is None:
                event = UsageEvent(
                    session_id=session_id,
                    stage=stage,
                    job_id=job_id or None,
                    artifact_id=artifact_id or None,
                    generation_run_id=generation_run_id or None,
                    agent_run_id=agent_run_id or None,
                    request_key=request_key or None,
                    provider_key=provider,
                    model_id=model,
                )
                session.add(event)
            event.job_id = job_id or event.job_id
            event.artifact_id = artifact_id or event.artifact_id
            event.provider_key = provider
            event.model_id = model
            event.input_tokens = int(usage.get("prompt_tokens") or 0)
            event.cached_input_tokens = int(usage.get("cached_prompt_tokens") or 0)
            event.output_tokens = int(usage.get("completion_tokens") or 0)
            event.cost_usd = cost
            event.cost_source = ",".join(sources) or None
            event.raw_usage_json = {
                "response_count": response_count,
                "commercial": commercial,
                "estimated": False,
                **usage,
            }

    def _record_tts_usage(
        self,
        session_id: str,
        settings: dict[str, Any],
        text: str,
        duration_ms: int,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
        generation_run_id: str | None = None,
    ) -> None:
        event = self._tts_usage_event(
            session_id,
            settings,
            text,
            duration_ms,
            job_id=job_id,
            artifact_id=artifact_id,
            generation_run_id=generation_run_id,
        )
        if event is None:
            return
        with self.database.session() as session:
            session.add(event)

    @staticmethod
    def _tts_usage_event(
        session_id: str,
        settings: dict[str, Any],
        text: str,
        duration_ms: int,
        *,
        job_id: str | None = None,
        artifact_id: str | None = None,
        generation_run_id: str | None = None,
    ) -> UsageEvent | None:
        from pandrator.logic.tts_handler import estimate_tts_usage

        usage = estimate_tts_usage(text, duration_ms, settings)
        if usage is None:
            return None
        return UsageEvent(
            session_id=session_id or None,
            job_id=job_id or None,
            artifact_id=artifact_id or None,
            generation_run_id=generation_run_id or None,
            stage="tts_generation",
            provider_key=str(usage["provider"]),
            model_id=str(usage["model"]),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_audio_tokens") or 0),
            cost_usd=usage.get("cost_usd"),
            cost_source=str(usage.get("cost_source") or "") or None,
            raw_usage_json=usage,
        )

    def _with_database_llm_settings(
        self, settings: dict[str, Any], stage: str
    ) -> dict[str, Any]:
        from .provider_settings import build_llm_settings

        aliases = {
            "correction": ("correction_model", "correct_model"),
            "translation": ("translation_model", "translate_model"),
            "tts_optimization": ("tts_optimization_model", "llm_model"),
        }
        requested = str(settings.get("model_name") or "").strip()
        for key in aliases[stage]:
            requested = requested or str(settings.get(key) or "").strip()
        if requested == "default":
            requested = ""
        llm_settings, resolved_model = build_llm_settings(
            self.database,
            self.paths,
            requested_model=requested,
            request_timeout_seconds=int(settings.get("request_timeout_seconds") or 600),
        )
        hydrated = {
            **settings,
            "llm_provider_configs": llm_settings.provider_configs,
            "llm_default_model": llm_settings.default_model,
            "request_timeout_seconds": llm_settings.request_timeout_seconds,
        }
        hydrated[aliases[stage][0]] = requested or resolved_model
        return hydrated

    def transcribe(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import (
            transcribe_source_file_with_metadata,
        )

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        session_dir = self._operation_dir(session_id, "transcribe")
        progress(0.05, "Preparing transcription")
        if cancel_event.is_set():
            return {}
        submitted_settings = dict(payload.get("settings") or {})
        runtime_settings = hydrate_stt_settings(
            self.database,
            self.paths,
            submitted_settings,
        )
        transcription_result = transcribe_source_file_with_metadata(
            session_dir,
            source_path,
            runtime_settings,
            ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
            crispasr_executable=str(payload.get("crispasr_executable") or ""),
            progress_callback=_scaled_progress_callback(progress, 0.05, 0.85),
            cancel_event=cancel_event,
        )
        output_path = Path(transcription_result.srt_path)
        progress(0.9, "Registering transcription")
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=dict(payload.get("settings") or {}),
            metadata={
                "engine": transcription_result.engine,
                "model": transcription_result.engine,
                "model_quantization": str(
                    (payload.get("settings") or {}).get("stt_model_quantization")
                    or "f16"
                ),
                "compute_backend": transcription_result.compute_backend,
                "language": str(
                    (payload.get("settings") or {}).get("original_language")
                    or (payload.get("settings") or {}).get("stt_language")
                    or "auto"
                ),
            },
        )
        timing_artifact = self.artifacts.register(
            Path(transcription_result.word_timestamps_path),
            kind="json",
            role="word_timestamps",
            session_id=session_id,
            parent_ids=[source_artifact.id, artifact.id],
            settings={
                **dict(payload.get("settings") or {}),
                "stt_engine": transcription_result.engine,
                "stt_compute_backend": transcription_result.compute_backend,
            },
        )
        _document_id, revision_id = self._store_srt_document(
            session_id,
            artifact,
            "transcription",
            language=str(
                (payload.get("settings") or {}).get("original_language")
                or (payload.get("settings") or {}).get("stt_language")
                or ""
            )
            or None,
        )
        word_count = self._store_timed_words(
            revision_id, Path(transcription_result.word_timestamps_path)
        )
        speaker_by_subtitle = self._subtitle_speaker_map(artifact, output_path)
        with self.database.session() as session:
            managed = session.get(Artifact, artifact.id)
            if managed is not None:
                speakers = {
                    speaker.casefold() for speaker in speaker_by_subtitle.values()
                }
                managed.metadata_json = {
                    **(managed.metadata_json or {}),
                    "has_speaker_metadata": bool(speakers),
                    "speaker_count": len(speakers),
                }
        progress(1.0, "Transcription ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "word_timestamps_artifact_id": timing_artifact.id,
            "word_timestamps_path": timing_artifact.relative_path,
            "word_count": word_count,
            "speaker_count": len(
                {speaker.casefold() for speaker in speaker_by_subtitle.values()}
            ),
            "revision_id": revision_id,
        }

    def _begin_agentic_operation(
        self,
        *,
        payload: dict[str, Any],
        kind: str,
        source_artifact: Artifact,
        requested_settings: dict[str, Any],
        instructions: str,
        usage_settings: dict[str, Any],
    ):
        """Start or resume a transform and return its durable checkpoint sink."""
        from types import SimpleNamespace

        from .agentic_runs import AgenticRunStore, stable_payload_hash

        store = AgenticRunStore(self.database)
        resolved_execution = {
            "model": str(
                usage_settings.get(f"{kind}_model")
                or usage_settings.get("model_name")
                or usage_settings.get("llm_default_model")
                or ""
            ),
            "backend": str(
                usage_settings.get("translation_backend")
                or usage_settings.get("backend")
                or "llm"
            ),
            "reasoning_effort": str(
                usage_settings.get("reasoning_effort")
                or usage_settings.get(f"{kind}_reasoning_effort")
                or ""
            ),
        }
        settings_hash = stable_payload_hash(
            {
                "kind": kind,
                "settings": requested_settings,
                "instructions": instructions,
                "resolved_execution": resolved_execution,
            }
        )
        started = store.start(
            kind=kind,
            session_id=str(payload.get("session_id") or ""),
            source_artifact=source_artifact,
            settings_hash=settings_hash,
            settings={
                "settings": requested_settings,
                "instructions": instructions,
                "resolved_execution": resolved_execution,
            },
            job_id=str(payload.get("_job_id") or "") or None,
            requested_run_id=str(payload.get("_agent_run_id") or "") or None,
        )
        checkpoint_lock = threading.Lock()
        known_keys = set(started.completed_units)
        next_ordinal = [len(known_keys)]

        def persist_checkpoint(
            unit_key: str,
            output: dict[str, Any],
            *,
            phase: str = "transform",
            usage_stage: str = kind,
            usage_settings: dict[str, Any] = usage_settings,
        ) -> None:
            with checkpoint_lock:
                is_new = unit_key not in known_keys
                store.checkpoint(
                    started.id,
                    unit_key=unit_key,
                    ordinal=next_ordinal[0],
                    input_value={
                        "kind": output.get("kind"),
                        "stage": output.get("stage"),
                        "source_hash": output.get("source_hash"),
                        "original_indices": output.get("original_indices", []),
                    },
                    output=output,
                    phase=phase,
                    summary=(f"Saved {phase.replace('_', ' ')} checkpoint {unit_key}."),
                    cost_usd=(
                        float(output.get("cost") or 0.0)
                        if output.get("cost_sources")
                        else None
                    ),
                )
                if is_new:
                    known_keys.add(unit_key)
                    next_ordinal[0] += 1
            usage_result = SimpleNamespace(
                cost=float(output.get("cost") or 0.0),
                response_count=int(output.get("response_count") or 0),
                cost_sources=tuple(output.get("cost_sources") or ()),
                usage=(
                    dict(output.get("usage") or {})
                    if isinstance(output.get("usage"), dict)
                    else {}
                ),
            )
            self._record_usage(
                str(payload.get("session_id") or ""),
                usage_stage,
                usage_settings,
                usage_result,
                job_id=str(payload.get("_job_id") or "") or None,
                agent_run_id=started.id,
                request_key=unit_key,
            )

        return store, started, persist_checkpoint

    def _run_stage_web_research(
        self,
        *,
        stage: str,
        session_id: str,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        progress,
        cancel_event,
        completed_units: dict[str, dict[str, Any]],
        persist_checkpoint,
    ):
        if not bool(settings.get("web_research_enabled", False)):
            return None
        provider_id = (
            str(settings.get("web_research_provider") or "jina").strip().lower()
        )
        if provider_id != "jina":
            raise ValueError(f"Unsupported web research provider: {provider_id}")
        if (
            stage == "translation"
            and str(
                settings.get("translation_backend") or settings.get("backend") or "llm"
            ).lower()
            != "llm"
        ):
            raise ValueError(
                "Web research currently grounds the LLM translation backend. "
                "Choose the LLM backend or disable web research."
            )

        from pandrator.logic.dubbing.srt_utils import parse_srt

        from .context_budget import ContextBudgetService
        from .knowledge import KnowledgeLedgerStore
        from .provider_settings import build_llm_settings
        from .web_research import (
            JinaResearchProvider,
            PersistentResearchCache,
            ResearchAgentConfig,
            WebResearchResult,
            merge_web_research_results,
            parse_domain_list,
            run_web_research_agent,
        )

        credential = resolve_secret_reference(
            self.database,
            self.paths,
            database_reference(auxiliary_credential_key("jina")),
            fallback_environment_variable="JINA_API_KEY",
        )
        research_provider = JinaResearchProvider(
            api_key=credential.resolved_value(),
            cache=PersistentResearchCache(self.database),
            timeout_seconds=int(settings.get("web_research_timeout_seconds") or 90),
        )
        model_key = "correction_model" if stage == "correction" else "translation_model"
        task_model = str(
            settings.get(model_key) or settings.get("llm_default_model") or ""
        )
        requested_researcher = str(
            settings.get("web_research_model_name") or ""
        ).strip()
        llm_settings, model_name = build_llm_settings(
            self.database,
            self.paths,
            requested_model=requested_researcher or task_model,
            request_timeout_seconds=int(
                settings.get("web_research_timeout_seconds")
                or settings.get("request_timeout_seconds")
                or 600
            ),
        )
        source_language = str(
            settings.get("original_language")
            or settings.get("source_language")
            or "auto"
        )
        target_language = (
            str(settings.get("target_language") or "") if stage == "translation" else ""
        )
        knowledge = KnowledgeLedgerStore(self.database)
        saved_research = knowledge.get(
            session_id,
            "research",
            source_language=source_language,
            target_language=target_language,
        )["payload"]
        saved_glossary = knowledge.get(
            session_id,
            "glossary",
            source_language=source_language,
            target_language=target_language,
        )["payload"]
        accumulated = WebResearchResult(
            evidence=[
                dict(item)
                for item in saved_research.get("evidence", [])
                if isinstance(item, dict)
            ],
            glossary=[
                {
                    "source": str(item.get("source") or ""),
                    "target": str(item.get("target") or ""),
                }
                for item in saved_glossary.get("entries", [])
                if isinstance(item, dict)
                and str(item.get("status") or "active") != "disabled"
            ],
            summary=str(saved_research.get("summary") or ""),
            warnings=[str(item) for item in saved_research.get("warnings", [])],
        )
        try:
            cues = parse_srt(source_path.read_text(encoding="utf-8-sig"))
            speaker_map = self._subtitle_speaker_map(source_artifact, source_path)
            records = [
                {
                    "id": cue.index,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "speaker": str(speaker_map.get(cue.index) or cue.speaker or ""),
                    "text": cue.text,
                }
                for cue in cues
            ]
        except (OSError, ValueError):
            records = [
                {"id": index, "text": text}
                for index, text in enumerate(
                    source_path.read_text(encoding="utf-8-sig").splitlines(),
                    start=1,
                )
                if text.strip()
            ]

        mode = str(settings.get("web_research_mode") or "global").strip().lower()
        if mode not in {"global", "per_chunk"}:
            raise ValueError("Web research mode must be 'global' or 'per_chunk'.")
        context_fraction = min(
            0.8,
            max(0.1, float(settings.get("web_research_context_fraction") or 0.8)),
        )
        budget = ContextBudgetService(self.database).resolve(
            model_name,
            fraction=context_fraction,
            fixed_prompt={
                "stage": stage,
                "source_language": source_language,
                "target_language": target_language,
                "instruction": "Research terminology and uncertain proper names using bounded web tools.",
            },
            ledger={
                "evidence": accumulated.evidence,
                "glossary": accumulated.glossary,
            },
            tools=["search_web", "read_url", "finish"],
        )
        partitioner = ContextBudgetService.partition
        if mode == "global":
            record_groups = partitioner(
                records,
                model=model_name,
                budget_tokens=budget.input_budget_tokens,
            )
        else:
            chunk_size = max(
                1,
                int(
                    settings.get("max_segments_per_batch")
                    or settings.get("max_subtitles_per_call")
                    or 40
                ),
            )
            record_groups = []
            for offset in range(0, len(records), chunk_size):
                record_groups.extend(
                    partitioner(
                        records[offset : offset + chunk_size],
                        model=model_name,
                        budget_tokens=budget.input_budget_tokens,
                    )
                )

        run_settings = {
            "stage": stage,
            "provider": provider_id,
            "model": model_name,
            "mode": mode,
            "context_window_tokens": budget.context_window_tokens,
            "context_fraction": budget.fraction,
            "input_budget_tokens": budget.input_budget_tokens,
            "source_language": source_language,
            "target_language": target_language,
            "research_language": str(settings.get("web_research_language") or ""),
            "max_searches": max(0, int(settings.get("web_research_max_searches") or 3)),
            "max_extractions": max(
                0, int(settings.get("web_research_max_extractions") or 2)
            ),
            "preferred_domains": list(
                parse_domain_list(settings.get("web_research_preferred_domains"))
            ),
            "blocked_domains": list(
                parse_domain_list(settings.get("web_research_blocked_domains"))
            ),
        }
        configured_iterations = max(
            2,
            int(settings.get("web_research_max_iterations") or 8),
        )
        progress(0.02, f"Preparing {stage} web research")
        results = [accumulated]
        total_batches = max(1, len(record_groups))
        for batch_index, group in enumerate(record_groups):
            research_source = json.dumps(group, ensure_ascii=False)
            unit_key = f"research:{mode}:{batch_index}"
            resume_state = completed_units.get(unit_key)

            def save_research_state(
                state: dict[str, Any],
                *,
                checkpoint_key: str = unit_key,
            ) -> None:
                checkpoint_result = (
                    state.get("result") if isinstance(state.get("result"), dict) else {}
                )
                persist_checkpoint(
                    checkpoint_key,
                    {
                        **state,
                        "cost": checkpoint_result.get("cost", 0.0),
                        "response_count": checkpoint_result.get("response_count", 0),
                        "cost_sources": checkpoint_result.get("cost_sources", []),
                        "usage": checkpoint_result.get("usage", {}),
                    },
                    phase="web_research",
                    usage_stage="web_research",
                    usage_settings={
                        **settings,
                        "web_research_model": model_name,
                    },
                )

            batch_start = 0.02 + (0.16 * batch_index / total_batches)
            batch_end = 0.02 + (0.16 * (batch_index + 1) / total_batches)
            result = run_web_research_agent(
                research_source,
                provider=research_provider,
                model_name=model_name,
                llm_settings=llm_settings,
                config=ResearchAgentConfig(
                    stage=stage,
                    source_language=run_settings["source_language"],
                    target_language=run_settings["target_language"],
                    research_language=run_settings["research_language"],
                    max_searches=run_settings["max_searches"],
                    max_extractions=run_settings["max_extractions"],
                    max_iterations=configured_iterations,
                    max_source_chars=max(2_000, len(research_source) + 1),
                    max_tool_result_chars=max(
                        2_000,
                        int(settings.get("web_research_result_chars") or 10_000),
                    ),
                    preferred_domains=tuple(run_settings["preferred_domains"]),
                    blocked_domains=tuple(run_settings["blocked_domains"]),
                    context_window_tokens=budget.context_window_tokens,
                    context_input_fraction=budget.fraction,
                ),
                cancel_event=cancel_event,
                progress_callback=_fraction_message_callback(
                    progress,
                    batch_start,
                    batch_end,
                ),
                resume_state=resume_state,
                on_checkpoint=save_research_state,
                initial_ledger=merge_web_research_results(results),
            )
            results.append(result)
        result = merge_web_research_results(results)
        progress(
            0.2,
            (
                f"Web research complete across {len(record_groups)} context batch(es) "
                f"after {result.response_count} model turn(s)"
            ),
        )
        knowledge.merge_research(
            session_id,
            source_language=source_language,
            target_language=target_language,
            evidence=result.evidence,
            warnings=result.warnings,
            summary=result.summary,
        )
        if result.glossary:
            knowledge.merge_glossary(
                session_id,
                source_language=source_language,
                target_language=target_language,
                entries=result.glossary,
                origin="research",
            )
        return result

    @staticmethod
    def _research_metadata(result, run_id: str) -> dict[str, Any] | None:
        if result is None or not run_id:
            return None
        sources = []
        seen: set[str] = set()
        for item in result.evidence:
            url = str(item.get("source_url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "url": url,
                    "title": str(item.get("source_title") or ""),
                }
            )
        return {
            "agent_run_id": run_id,
            "summary": result.summary,
            "evidence_count": len(result.evidence),
            "glossary": list(result.glossary),
            "sources": sources,
            "warnings": list(result.warnings),
        }

    def correct(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.llm_correction import correct_srt_file_with_result

        from .web_research import evidence_prompt

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        speaker_by_subtitle = self._subtitle_speaker_map(source_artifact, source_path)
        session_dir = self._operation_dir(session_id, "correct")
        requested_settings = dict(payload.get("settings") or {})
        requested_settings_hash = hashlib.sha256(
            json.dumps(
                requested_settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        settings = self._with_database_llm_settings(requested_settings, "correction")
        base_instructions = str(
            payload.get("instructions") or settings.get("instructions") or ""
        )
        run_store, agent_run, persist_checkpoint = self._begin_agentic_operation(
            payload=payload,
            kind="correction",
            source_artifact=source_artifact,
            requested_settings=requested_settings,
            instructions=base_instructions,
            usage_settings=settings,
        )
        research_result = None
        try:
            research_result = self._run_stage_web_research(
                stage="correction",
                session_id=session_id,
                source_artifact=source_artifact,
                source_path=source_path,
                settings=settings,
                progress=progress,
                cancel_event=cancel_event,
                completed_units=agent_run.completed_units,
                persist_checkpoint=persist_checkpoint,
            )
            instructions = base_instructions
            if research_result is not None:
                instructions += evidence_prompt(
                    research_result.evidence,
                    stage="correction",
                )
            processing_start = 0.2 if research_result is not None else 0.05
            progress(processing_start, "Preparing subtitle correction requests")
            result = correct_srt_file_with_result(
                session_dir,
                source_path,
                settings,
                correction_instructions=instructions,
                cancel_event=cancel_event,
                speaker_by_subtitle=speaker_by_subtitle,
                completed_units=agent_run.completed_units,
                on_unit_completed=lambda key, output: persist_checkpoint(
                    key,
                    output,
                    phase="correction",
                    usage_stage="correction",
                    usage_settings=settings,
                ),
                progress_callback=_scaled_progress_callback(
                    progress,
                    processing_start,
                    0.9,
                ),
            )
            if cancel_event.is_set():
                raise RuntimeError("Subtitle correction was canceled.")
            progress(0.92, "Correction requests complete; preparing artifact")
            settings_fingerprint = _stage_settings_fingerprint("correct", settings)
            artifact = self.artifacts.register(
                Path(result.output_path),
                kind="srt",
                role="correction",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
                metadata={
                    "source_artifact_id": source_artifact.id,
                    "source_content_hash": source_artifact.content_hash,
                    "requested_settings_hash": requested_settings_hash,
                    "settings_fingerprint": settings_fingerprint,
                    "model": settings_fingerprint["model"],
                    "language": str(
                        settings.get("original_language")
                        or settings.get("source_language")
                        or "auto"
                    ),
                    "agent_run_id": agent_run.id,
                    **(
                        {
                            "research": self._research_metadata(
                                research_result, agent_run.id
                            )
                        }
                        if research_result is not None
                        else {}
                    ),
                },
            )
            progress(0.97, "Registering corrected subtitle document")
            self._store_srt_document(
                session_id,
                artifact,
                "correction",
                language=str(
                    settings.get("original_language")
                    or settings.get("source_language")
                    or ""
                )
                or None,
                parent_artifact=source_artifact,
                speaker_overrides=result.speaker_by_subtitle,
            )
            run_store.finish(agent_run.id, artifact_id=artifact.id)
        except Exception as error:
            run_store.fail(agent_run.id, error)
            raise
        progress(1.0, "Correction ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "cost": result.cost,
            "agent_run_id": agent_run.id,
            "resumed": agent_run.resumed,
        }

    def translate(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.llm_translation import (
            normalize_glossary,
            translate_srt_file_deepl_with_result,
            translate_srt_file_with_result,
        )

        from .web_research import evidence_prompt

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        speaker_by_subtitle = self._subtitle_speaker_map(source_artifact, source_path)
        session_dir = self._operation_dir(session_id, "translate")
        requested_settings = dict(payload.get("settings") or {})
        requested_settings_hash = hashlib.sha256(
            json.dumps(
                requested_settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        settings = requested_settings
        research_result = None
        run_store = None
        agent_run = None
        base_instructions = str(
            payload.get("instructions") or settings.get("instructions") or ""
        )
        translation_backend = str(
            settings.get("translation_backend") or settings.get("backend") or "llm"
        ).lower()
        if translation_backend == "deepl" and bool(
            settings.get("web_research_enabled")
        ):
            raise ValueError(
                "Web research currently augments LLM translation only. "
                "Choose the LLM backend or turn web research off."
            )
        if translation_backend == "deepl":
            processing_start = 0.05
            progress(processing_start, "Preparing DeepL translation requests")
            credential = resolve_secret_reference(
                self.database,
                self.paths,
                database_reference(auxiliary_credential_key("deepl")),
                fallback_environment_variable="DEEPL_API_KEY",
            )
            result = translate_srt_file_deepl_with_result(
                session_dir,
                source_path,
                settings,
                auth_key=credential.resolved_value(),
                speaker_by_subtitle=speaker_by_subtitle,
                cancel_event=cancel_event,
                progress_callback=_scaled_progress_callback(
                    progress,
                    processing_start,
                    0.9,
                ),
            )
        else:
            settings = self._with_database_llm_settings(settings, "translation")
            run_store, agent_run, persist_checkpoint = self._begin_agentic_operation(
                payload=payload,
                kind="translation",
                source_artifact=source_artifact,
                requested_settings=requested_settings,
                instructions=base_instructions,
                usage_settings=settings,
            )
            try:
                research_result = self._run_stage_web_research(
                    stage="translation",
                    session_id=session_id,
                    source_artifact=source_artifact,
                    source_path=source_path,
                    settings=settings,
                    progress=progress,
                    cancel_event=cancel_event,
                    completed_units=agent_run.completed_units,
                    persist_checkpoint=persist_checkpoint,
                )
                instructions = base_instructions
                if research_result is not None:
                    instructions += evidence_prompt(
                        research_result.evidence,
                        stage="translation",
                    )
                from .knowledge import KnowledgeLedgerStore

                glossary_store = KnowledgeLedgerStore(self.database)
                manual_glossary = normalize_glossary(settings.get("glossary"))
                if manual_glossary:
                    glossary_store.merge_glossary(
                        session_id,
                        source_language=str(
                            settings.get("original_language")
                            or settings.get("source_language")
                            or "auto"
                        ),
                        target_language=str(settings.get("target_language") or ""),
                        entries=[
                            {"source": source, "target": target}
                            for source, target in manual_glossary.items()
                        ],
                        origin="manual",
                        locked=True,
                    )
                glossary_payload = glossary_store.get(
                    session_id,
                    "glossary",
                    source_language=str(
                        settings.get("original_language")
                        or settings.get("source_language")
                        or "auto"
                    ),
                    target_language=str(settings.get("target_language") or ""),
                )["payload"]
                glossary_seed = [
                    dict(item)
                    for item in glossary_payload.get("entries", [])
                    if isinstance(item, dict)
                    and str(item.get("status") or "active") != "disabled"
                ]
                if research_result is not None:
                    glossary_seed.extend(research_result.glossary)
                processing_start = 0.2 if research_result is not None else 0.05
                progress(processing_start, "Preparing subtitle translation requests")
                result = translate_srt_file_with_result(
                    session_dir,
                    source_path,
                    settings,
                    translation_instructions=instructions,
                    glossary=glossary_seed,
                    cancel_event=cancel_event,
                    speaker_by_subtitle=speaker_by_subtitle,
                    completed_units=agent_run.completed_units,
                    on_unit_completed=lambda key, output: persist_checkpoint(
                        key,
                        output,
                        phase="translation",
                        usage_stage="translation",
                        usage_settings=settings,
                    ),
                    progress_callback=_scaled_progress_callback(
                        progress,
                        processing_start,
                        0.9,
                    ),
                )
                if result.glossary:
                    glossary_store.merge_glossary(
                        session_id,
                        source_language=str(
                            settings.get("original_language")
                            or settings.get("source_language")
                            or "auto"
                        ),
                        target_language=str(settings.get("target_language") or ""),
                        entries=[
                            {"source": source, "target": target}
                            for source, target in result.glossary.items()
                        ],
                        origin="translation",
                    )
            except Exception as error:
                run_store.fail(agent_run.id, error)
                raise
        if cancel_event.is_set():
            cancel_error = RuntimeError("Subtitle translation was canceled.")
            if run_store is not None and agent_run is not None:
                run_store.fail(agent_run.id, cancel_error)
            raise cancel_error
        try:
            progress(0.92, "Translation requests complete; preparing artifact")
            settings_fingerprint = _stage_settings_fingerprint("translate", settings)
            artifact = self.artifacts.register(
                Path(result.output_path),
                kind="srt",
                role="translation",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
                metadata={
                    "source_artifact_id": source_artifact.id,
                    "source_content_hash": source_artifact.content_hash,
                    "requested_settings_hash": requested_settings_hash,
                    "settings_fingerprint": settings_fingerprint,
                    "backend": settings_fingerprint["backend"],
                    "model": settings_fingerprint["model"],
                    "language": settings_fingerprint["target_language"],
                    **({"agent_run_id": agent_run.id} if agent_run is not None else {}),
                    **(
                        {
                            "research": self._research_metadata(
                                research_result, agent_run.id
                            )
                        }
                        if research_result is not None and agent_run is not None
                        else {}
                    ),
                },
            )
            progress(0.97, "Registering translated subtitle document")
            self._store_srt_document(
                session_id,
                artifact,
                "translation",
                language=str(settings.get("target_language") or "") or None,
                parent_artifact=source_artifact,
                speaker_overrides=getattr(result, "speaker_by_subtitle", {}),
            )
            if run_store is not None and agent_run is not None:
                run_store.finish(agent_run.id, artifact_id=artifact.id)
            else:
                self._record_usage(
                    session_id,
                    "translation",
                    settings,
                    result,
                    job_id=str(payload.get("_job_id") or "") or None,
                    artifact_id=artifact.id,
                )
        except Exception as error:
            if run_store is not None and agent_run is not None:
                run_store.fail(agent_run.id, error)
            raise
        progress(1.0, "Translation ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "cost": result.cost,
            **(
                {
                    "agent_run_id": agent_run.id,
                    "resumed": agent_run.resumed,
                }
                if agent_run is not None
                else {}
            ),
        }

    def optimize_tts(self, payload, progress, cancel_event):
        """Create a separate, previewable text revision optimized only for speech."""
        from dataclasses import replace
        from types import SimpleNamespace

        from pandrator.logic.dubbing.srt_utils import compose_srt, parse_srt

        from .pronunciations import (
            PronunciationLibrary,
            apply_reviewed_pronunciations,
            normalize_backend,
        )
        from .tts_optimization import optimize_texts

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        requested_settings = dict(payload.get("settings") or {})
        requested_settings_hash = hashlib.sha256(
            json.dumps(
                requested_settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        settings = self._with_database_llm_settings(
            requested_settings, "tts_optimization"
        )
        settings["llm_tts_batch_size"] = max(
            1,
            int(
                settings.get("llm_tts_document_batch_size")
                or settings.get("llm_tts_batch_size")
                or 8
            ),
        )
        llm_settings = SimpleNamespace(
            provider_configs=settings["llm_provider_configs"],
            default_model=settings["llm_default_model"],
            request_timeout_seconds=settings["request_timeout_seconds"],
        )
        model_name = str(
            settings.get("tts_optimization_model") or settings["llm_default_model"]
        )
        run_store, agent_run, persist_checkpoint = self._begin_agentic_operation(
            payload=payload,
            kind="tts_optimization",
            source_artifact=source_artifact,
            requested_settings=requested_settings,
            instructions=str(
                payload.get("instructions") or settings.get("instructions") or ""
            ),
            usage_settings=settings,
        )
        speech_mode = (
            str(settings.get("speech_optimization_mode") or "").strip().lower()
        )
        structured_mode = speech_mode in {"guarded", "flexible"}
        default_language = str(
            settings.get("language")
            or settings.get("target_language")
            or settings.get("source_language")
            or (source_artifact.metadata_json or {}).get("language")
            or "en"
        )
        voice_language = str(
            settings.get("voice_language")
            or settings.get("language")
            or default_language
        )
        backend = normalize_backend(
            settings.get("service")
            or settings.get("tts_service")
            or settings.get("backend")
            or "*"
        )
        pronunciation_library = PronunciationLibrary(self.database)
        apply_reviewed = (
            settings.get("apply_reviewed_pronunciations", True) is not False
        )
        speech_plans: list[dict[str, Any]] = []

        def optimize_units(
            source_texts: list[str],
            languages: list[str],
        ):
            nonlocal speech_plans
            speech_plans = [{} for _ in source_texts]
            known_by_index = (
                {
                    index: pronunciation_library.resolve(
                        text,
                        session_id=session_id,
                        language=languages[index],
                        backend=backend,
                    )
                    for index, text in enumerate(source_texts)
                }
                if apply_reviewed
                else {}
            )

            def resolve_known(text: str, language: str) -> list[dict[str, Any]]:
                for index, source_text in enumerate(source_texts):
                    if source_text == text and languages[index] == language:
                        return deepcopy(known_by_index.get(index, []))
                return []

            def keep_plans(items: list[tuple[int, str, dict[str, Any]]]) -> None:
                for index, _revised, plan in items:
                    if bool(settings.get("speech_plan_save_proposals", True)):
                        plan["proposals"] = self._save_speech_plan_proposals(
                            library=pronunciation_library,
                            session_id=session_id,
                            plan=plan,
                            backend=backend,
                            model_name=model_name,
                            default_language=languages[index],
                        )
                    else:
                        plan["proposals"] = []
                    speech_plans[index] = plan

            try:
                optimized, usage = optimize_texts(
                    source_texts,
                    settings,
                    llm_settings,
                    model_name,
                    cancel_event,
                    _scaled_progress_callback(progress, 0.05, 0.9),
                    on_plan_batch=keep_plans if structured_mode else None,
                    known_pronunciation_resolver=resolve_known
                    if structured_mode
                    else None,
                    languages=languages,
                    voice_languages=[voice_language for _ in source_texts],
                    completed_units=agent_run.completed_units,
                    on_unit_completed=lambda key, output: persist_checkpoint(
                        key,
                        output,
                        phase="tts_optimization",
                        usage_stage="tts_optimization",
                        usage_settings=settings,
                    ),
                )
                if apply_reviewed and not structured_mode:
                    optimized = [
                        apply_reviewed_pronunciations(
                            revised,
                            known_by_index.get(index, []),
                        )
                        for index, revised in enumerate(optimized)
                    ]
                return optimized, usage
            except Exception as error:
                run_store.fail(agent_run.id, error)
                raise

        suffix = source_path.suffix.lower()
        progress(0.02, "Preparing speech optimization preview")
        if suffix == ".srt":
            segments = parse_srt(source_path.read_text(encoding="utf-8-sig"))
            source_texts = [segment.text for segment in segments]
            optimized, usage = optimize_units(
                source_texts,
                [default_language for _ in source_texts],
            )
            if cancel_event.is_set():
                return {}
            segments = [
                replace(segment, text=text)
                for segment, text in zip(segments, optimized, strict=True)
            ]
            destination = (
                self._session_dir(session_id) / f"tts-optimized-{new_id()}.srt"
            )
            destination.write_text(compose_srt(segments), encoding="utf-8")
            kind = "srt"
        elif suffix == ".json":
            rows = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError(
                    "Speech optimization JSON input must contain a list of generation units."
                )
            source_texts = [
                str(
                    row.get("source_text")
                    or row.get("text")
                    or row.get("processed_sentence")
                    or row.get("original_sentence")
                    or ""
                )
                if isinstance(row, dict)
                else str(row)
                for row in rows
            ]
            languages = [
                str(row.get("language") or default_language)
                if isinstance(row, dict)
                else default_language
                for row in rows
            ]
            optimized, usage = optimize_units(source_texts, languages)
            if cancel_event.is_set():
                return {}
            for index, (row, text) in enumerate(zip(rows, optimized, strict=True)):
                if isinstance(row, dict):
                    row["source_text"] = str(
                        row.get("source_text")
                        or row.get("text")
                        or row.get("processed_sentence")
                        or row.get("original_sentence")
                        or ""
                    )
                    row["tts_optimized_sentence"] = text
                    if structured_mode:
                        row["speech_plan"] = speech_plans[index]
            destination = (
                self._session_dir(session_id) / f"tts-optimized-{new_id()}.json"
            )
            destination.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            kind = "json"
        else:
            source_text = source_path.read_text(encoding="utf-8-sig")
            optimized, usage = optimize_units([source_text], [default_language])
            if cancel_event.is_set():
                return {}
            destination = (
                self._session_dir(session_id) / f"tts-optimized-{new_id()}.txt"
            )
            destination.write_text(optimized[0], encoding="utf-8")
            kind = "text"
        progress(0.92, "Speech optimization complete; preparing preview artifact")
        speech_plan_artifact_id = ""
        if structured_mode and any(speech_plans):
            plan_path = self._session_dir(session_id) / f"speech-plans-{new_id()}.json"
            plan_path.write_text(
                json.dumps(
                    [
                        {
                            "index": index,
                            "source_text": source_text,
                            "speech_plan": plan,
                        }
                        for index, (source_text, plan) in enumerate(
                            zip(source_texts, speech_plans, strict=True)
                        )
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            plan_artifact = self.artifacts.register(
                plan_path,
                kind="json",
                role="speech_plan",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=settings,
                metadata={
                    "source_artifact_id": source_artifact.id,
                    "model": model_name,
                    "mode": speech_mode,
                    "plan_count": len(speech_plans),
                },
            )
            speech_plan_artifact_id = plan_artifact.id
        progress(0.97, "Registering speech optimization artifacts")
        artifact = self.artifacts.register(
            destination,
            kind=kind,
            role="tts_optimized",
            session_id=session_id,
            parent_ids=[
                source_artifact.id,
                *([speech_plan_artifact_id] if speech_plan_artifact_id else []),
            ],
            settings=settings,
            metadata={
                "source_artifact_id": source_artifact.id,
                "model": model_name,
                "mode": "whole_document",
                "speech_optimization_mode": speech_mode or "legacy",
                "speech_plan_artifact_id": speech_plan_artifact_id or None,
                "speech_plan_count": len([plan for plan in speech_plans if plan]),
                "batch_size": settings["llm_tts_batch_size"],
                "requested_settings_hash": requested_settings_hash,
                "agent_run_id": agent_run.id,
            },
        )
        if suffix == ".srt":
            self._store_srt_document(
                session_id,
                artifact,
                "tts_optimization",
                language=str(
                    (source_artifact.metadata_json or {}).get("language") or ""
                )
                or None,
                parent_artifact=source_artifact,
            )
        run_store.finish(agent_run.id, artifact_id=artifact.id)
        progress(1.0, "Speech optimization preview ready")
        input_tokens = int(usage.usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.usage.get("completion_tokens") or 0)
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "cost": usage.cost,
            "agent_run_id": agent_run.id,
            "resumed": agent_run.resumed,
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": int(
                    usage.usage.get("cached_prompt_tokens") or 0
                ),
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "response_count": usage.response_count,
            },
        }

    def transcribe_voice(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import (
            transcribe_source_file_with_metadata,
        )

        sample_artifact, sample_path = self._resolve_input(
            str(payload.get("sample_artifact_id") or "")
        )
        operation_dir = self.paths.voices / str(
            payload.get("voice_id") or "transcription"
        )
        operation_dir.mkdir(parents=True, exist_ok=True)
        progress(0.05, "Preparing reference transcription")
        runtime_settings = hydrate_stt_settings(
            self.database,
            self.paths,
            dict(payload.get("settings") or {}),
        )
        transcription_result = transcribe_source_file_with_metadata(
            operation_dir,
            sample_path,
            runtime_settings,
            ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
            crispasr_executable=str(payload.get("crispasr_executable") or ""),
            progress_callback=_scaled_progress_callback(progress, 0.05, 0.85),
            cancel_event=cancel_event,
        )
        output_path = Path(transcription_result.srt_path)
        if cancel_event.is_set():
            return {}
        progress(0.9, "Registering reference transcription")
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="voice_transcription",
            parent_ids=[sample_artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        timing_artifact = self.artifacts.register(
            Path(transcription_result.word_timestamps_path),
            kind="json",
            role="voice_word_timestamps",
            parent_ids=[sample_artifact.id, artifact.id],
            settings=dict(payload.get("settings") or {}),
        )
        # Read the canonical transcript so voice-reference text stays
        # independent from subtitle layout and structured speaker metadata.
        transcript = " ".join(
            segment.text.replace("\n", " ").strip()
            for segment in load_transcript(
                transcription_result.word_timestamps_path
            ).segments
        )
        progress(1.0, "Reference transcription ready for review")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "word_timestamps_artifact_id": timing_artifact.id,
            "sample_id": payload.get("sample_id"),
            "transcript": transcript,
        }

    def normalize_voice_recording(self, payload, progress, cancel_event):
        voice_id = str(payload.get("voice_id") or "")
        replace_sample_id = str(payload.get("replace_sample_id") or "") or None
        expected_raw = payload.get("expected_voice_revision")
        expected_revision = int(expected_raw) if expected_raw is not None else None
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice not found.")
            if expected_revision is not None and voice.revision != expected_revision:
                raise ValueError("The voice changed before the sample could be saved.")
            if replace_sample_id:
                existing = session.get(VoiceSample, replace_sample_id)
                if existing is None or existing.voice_id != voice_id:
                    raise ValueError("Voice sample not found.")
        voice_dir = self.paths.voices / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True)
        destination = voice_dir / f"sample-{source_artifact.id}.wav"
        progress(0.1, "Normalizing recording")
        command = [
            str(payload.get("ffmpeg_executable") or "ffmpeg"),
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        if cancel_event.is_set():
            destination.unlink(missing_ok=True)
            return {}
        prepared = self.artifacts.prepare_registration(destination)
        removable: list[Path] = []
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                destination.unlink(missing_ok=True)
                raise ValueError("Voice was removed before the sample could be saved.")
            if expected_revision is not None and voice.revision != expected_revision:
                destination.unlink(missing_ok=True)
                raise ValueError("The voice changed before the sample could be saved.")
            artifact = self.artifacts.register_in_session(
                session,
                destination,
                kind="audio",
                role="voice_sample",
                parent_ids=[source_artifact.id],
                _prepared=prepared,
            )
            if replace_sample_id:
                sample = session.get(VoiceSample, replace_sample_id)
                if sample is None or sample.voice_id != voice_id:
                    destination.unlink(missing_ok=True)
                    raise ValueError(
                        "Voice sample was removed before it could be replaced."
                    )
                old_path = retire_sample_artifact(session, self.paths, sample)
                if old_path is not None:
                    removable.append(old_path)
                sample.artifact_id = artifact.id
                sample.transcript = None
                sample.transcript_language = None
                sample.transcript_reviewed = False
                sample.created_at = utcnow()
            else:
                sample = VoiceSample(voice_id=voice_id, artifact_id=artifact.id)
                session.add(sample)
            session.flush()
            sample_id = sample.id
            mark_provider_registrations_stale(
                voice,
                "The local reference audio changed.",
                sample_id=replace_sample_id,
            )
            voice.revision += 1
            voice.updated_at = utcnow()
            voice_revision = voice.revision
        remove_managed_files(removable)
        progress(1.0, "Voice sample ready")
        return {
            "sample_id": sample_id,
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "voice_revision": voice_revision,
            "replaced": bool(replace_sample_id),
        }

    def publish_voice(self, payload, progress, cancel_event):
        """Upload the newest managed sample and persist the provider's voice ID."""
        from pandrator.logic import tts_handler

        voice_id = str(payload.get("voice_id") or "")
        service_id = str(payload.get("service_id") or "").strip()
        service_name = str(payload.get("service") or service_id).strip()
        expected_raw = payload.get("expected_voice_revision")
        expected_revision = int(expected_raw) if expected_raw is not None else None
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice not found.")
            if expected_revision is not None and voice.revision != expected_revision:
                raise ValueError("The voice changed before provider upload began.")
            samples = list(
                session.scalars(
                    select(VoiceSample)
                    .where(VoiceSample.voice_id == voice_id)
                    .order_by(VoiceSample.created_at.desc())
                ).all()
            )
            sample = next(
                (
                    item
                    for item in samples
                    if sample_file_status(session, self.paths, item)[0] == "ready"
                ),
                None,
            )
            if sample is None:
                raise ValueError(
                    "Add or replace a readable voice sample before uploading this voice."
                )
            provider_records = dict((voice.metadata_json or {}).get("providers") or {})
            existing = dict(provider_records.get(service_id) or {})
            requested_provider_voice_id = str(
                existing.get("voice_id") or _managed_provider_voice_id(voice)
            ).strip()
            voice_name = voice.name
            source_voice_revision = voice.revision

        sample_artifact, sample_path = self._resolve_input(sample.artifact_id)
        if sample_path.suffix.lower() != ".wav":
            raise ValueError("Provider voice uploads require a normalized WAV sample.")
        if cancel_event.is_set():
            return {}
        progress(0.1, f"Uploading {voice_name} to {service_name}")
        with self.database.session() as session:
            connections = session.get(AppSetting, "services.tts")
            defaults = session.get(AppSetting, "defaults.tts")
            connection_value = (
                dict(connections.value_json or {})
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            default_value = (
                dict(defaults.value_json or {})
                if defaults and isinstance(defaults.value_json, dict)
                else {}
            )
        runtime_settings = hydrate_tts_settings(
            self.database,
            self.paths,
            {
                **default_value,
                **connection_value,
                "service": service_id,
            },
            manager_bridge=self.manager_bridge,
        )
        service_config = (
            tts_handler.get_service_config(
                runtime_settings,
                service_id,
            )
            or {}
        )
        normalized_service_id = (
            str(service_config.get("id") or service_id)
            .strip()
            .lower()
            .replace("-", "_")
        )
        base_url = str(service_config.get("api_base") or "").strip()
        service_name = str(service_config.get("name") or service_name).strip()
        service_adapter = (
            str(service_config.get("adapter") or "").strip().lower().replace("-", "_")
        )
        if service_adapter == "audio_cpp":
            registration_service_id = str(
                service_config.get("id") or service_id
            ).strip()
            reviewed_transcript = (
                str(sample.transcript or "").strip()
                if sample.transcript_reviewed
                else ""
            )
            provider_voice_id = _managed_provider_voice_id(voice)
            sample_hash = str(sample_artifact.content_hash or "").strip()
            if not sample_hash:
                sample_hash = hashlib.sha256(sample_path.read_bytes()).hexdigest()
            registration = {
                "voice_id": provider_voice_id,
                "provider_voice_id": provider_voice_id,
                "sample_id": sample.id,
                "sample_hash": sample_hash,
                "source_audio_hash": sample_hash,
                "status": "ready",
                "updated_at": utcnow().isoformat(),
                "managed_by": "pandrator",
                "protocol": "pandrator-linked-voices-v1",
                "resource_kind": "linked_reference",
                "endpoint_fingerprint": _provider_endpoint_fingerprint(base_url),
                "reference_text_mode": "optional",
                "reference_text_hash": (
                    hashlib.sha256(reviewed_transcript.encode("utf-8")).hexdigest()
                    if reviewed_transcript
                    else None
                ),
            }
            with self.database.session() as session:
                voice = session.get(Voice, voice_id)
                if voice is None:
                    raise ValueError("Voice was removed before it could be linked.")
                current_sample = session.get(VoiceSample, sample.id)
                if (
                    current_sample is None
                    or current_sample.artifact_id != sample.artifact_id
                ):
                    raise ValueError(
                        "The voice sample changed before it could be linked."
                    )
                metadata = deepcopy(voice.metadata_json or {})
                providers = dict(metadata.get("providers") or {})
                providers[registration_service_id] = registration
                metadata["providers"] = providers
                voice.metadata_json = metadata
                voice.revision += 1
                voice.updated_at = utcnow()
                next_voice_revision = voice.revision
            progress(1.0, f"{voice_name} is ready in {service_name}")
            return {
                "voice_id": voice_id,
                "service_id": service_id,
                "provider_voice_id": provider_voice_id,
                "voice_revision": next_voice_revision,
                "linked": True,
            }
        reference_text_mode = str(
            service_config.get("voice_reference_text") or "ignored"
        )
        reviewed_transcript = (
            str(sample.transcript or "").strip() if sample.transcript_reviewed else ""
        )
        if reference_text_mode == "required" and not reviewed_transcript:
            raise ValueError(
                f"{service_name} requires a reviewed sample transcript before "
                "this voice can be used."
            )
        uploaded_reference_text = (
            reviewed_transcript
            if reference_text_mode in {"required", "optional"}
            else ""
        )
        provider_voice_id = self.tts_providers.upload_voice(
            normalized_service_id,
            str(sample_path),
            base_url=base_url,
            service=service_name,
            prompt_text=uploaded_reference_text or None,
            voice_id=requested_provider_voice_id,
            api_key=str(service_config.get("api_key") or ""),
        )
        # A remote mutation has happened. Persist its ownership record even if
        # cancellation was requested in the meantime; otherwise the provider
        # copy becomes an unmanageable orphan.
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice was removed while it was being uploaded.")
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            current_sample = session.get(VoiceSample, sample.id)
            still_current = bool(
                voice.revision == source_voice_revision
                and current_sample is not None
                and current_sample.artifact_id == sample.artifact_id
            )
            providers[service_id] = {
                "voice_id": provider_voice_id,
                "sample_id": sample.id,
                "status": "ready" if still_current else "stale",
                "updated_at": utcnow().isoformat(),
                "managed_by": "pandrator",
                "protocol": "pandrator-voices-v1",
                "resource_kind": "uploaded_reference",
                "endpoint_fingerprint": _provider_endpoint_fingerprint(base_url),
                "source_audio_hash": sample_artifact.content_hash,
                "reference_text_mode": reference_text_mode,
                "reference_text_hash": (
                    hashlib.sha256(uploaded_reference_text.encode("utf-8")).hexdigest()
                    if uploaded_reference_text
                    else None
                ),
                **(
                    {}
                    if still_current
                    else {
                        "stale_reason": (
                            "The local reference changed while it was being uploaded."
                        )
                    }
                ),
            }
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_voice_revision = voice.revision
        progress(1.0, f"{voice_name} is ready in {service_name}")
        return {
            "voice_id": voice_id,
            "service_id": service_id,
            "provider_voice_id": provider_voice_id,
            "voice_revision": next_voice_revision,
            "cancellation_requested_after_upload": cancel_event.is_set(),
        }

    def unpublish_voice(self, payload, progress, cancel_event):
        """Remove a Pandrator-owned provider voice and then clear registration."""
        from pandrator.logic import tts_handler

        voice_id = str(payload.get("voice_id") or "")
        service_id = str(payload.get("service_id") or "").strip()
        service_name = str(payload.get("service") or service_id).strip()
        expected_raw = payload.get("expected_voice_revision")
        expected_revision = int(expected_raw) if expected_raw is not None else None
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                raise ValueError("Voice not found.")
            if expected_revision is not None and voice.revision != expected_revision:
                raise ValueError("The voice changed before provider removal began.")
            registration = dict(
                ((voice.metadata_json or {}).get("providers") or {}).get(service_id)
                or {}
            )
            if not registration:
                return {
                    "voice_id": voice_id,
                    "service_id": service_id,
                    "already_absent": True,
                    "voice_revision": voice.revision,
                }
            if registration.get("managed_by") != "pandrator":
                raise ValueError(
                    "The provider registration has no Pandrator ownership proof."
                )
            provider_voice_id = str(
                registration.get("voice_id")
                or registration.get("provider_voice_id")
                or ""
            ).strip()
            if not provider_voice_id:
                raise ValueError("The provider registration has no remote voice ID.")

            connections = session.get(AppSetting, "services.tts")
            defaults = session.get(AppSetting, "defaults.tts")
            connection_value = (
                dict(connections.value_json or {})
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            default_value = (
                dict(defaults.value_json or {})
                if defaults and isinstance(defaults.value_json, dict)
                else {}
            )

        runtime_settings = hydrate_tts_settings(
            self.database,
            self.paths,
            {
                **default_value,
                **connection_value,
                "service": service_id,
            },
            manager_bridge=self.manager_bridge,
        )
        service_config = (
            tts_handler.get_service_config(runtime_settings, service_id) or {}
        )
        service_adapter = (
            str(service_config.get("adapter") or "").strip().lower().replace("-", "_")
        )
        if (
            registration.get("resource_kind") == "linked_reference"
            and service_adapter == "audio_cpp"
        ):
            with self.database.session() as session:
                voice = session.get(Voice, voice_id)
                if voice is None:
                    raise ValueError("Voice not found.")
                metadata = deepcopy(voice.metadata_json or {})
                providers = dict(metadata.get("providers") or {})
                current = dict(providers.get(service_id) or {})
                if current.get("resource_kind") != "linked_reference":
                    raise ValueError(
                        "The linked voice registration changed while removing it."
                    )
                providers.pop(service_id, None)
                metadata["providers"] = providers
                voice.metadata_json = metadata
                voice.revision += 1
                voice.updated_at = utcnow()
                next_revision = voice.revision
            progress(1.0, f"{service_name} link removed")
            return {
                "voice_id": voice_id,
                "service_id": service_id,
                "provider_voice_id": provider_voice_id,
                "remote_deleted": False,
                "linked": True,
                "voice_revision": next_revision,
            }
        if not bool(service_config.get("supports_voice_deletion")):
            raise ValueError(
                f"{service_name} does not advertise provider-side voice deletion."
            )
        normalized_service_id = (
            str(service_config.get("id") or service_id)
            .strip()
            .lower()
            .replace("-", "_")
        )
        base_url = str(service_config.get("api_base") or "").strip()
        current_fingerprint = _provider_endpoint_fingerprint(base_url)
        if registration.get("endpoint_fingerprint") != current_fingerprint:
            raise ValueError(
                "The service endpoint changed since this voice was uploaded; "
                "automatic removal was stopped to avoid deleting the wrong resource."
            )
        if cancel_event.is_set():
            return {}
        progress(0.1, f"Removing {provider_voice_id} from {service_name}")
        remote_deleted = self.tts_providers.delete_voice(
            normalized_service_id,
            provider_voice_id,
            base_url=base_url,
            service=str(service_config.get("name") or service_name),
            api_key=str(service_config.get("api_key") or ""),
        )
        # As with upload, once the remote side effect starts we reconcile local
        # state even if cancellation arrives during the request.
        with self.database.session() as session:
            voice = session.get(Voice, voice_id)
            if voice is None:
                return {
                    "voice_id": voice_id,
                    "service_id": service_id,
                    "provider_voice_id": provider_voice_id,
                    "remote_deleted": remote_deleted,
                    "local_voice_missing": True,
                }
            metadata = deepcopy(voice.metadata_json or {})
            providers = dict(metadata.get("providers") or {})
            current = dict(providers.get(service_id) or {})
            if (
                current.get("voice_id") != provider_voice_id
                or current.get("endpoint_fingerprint") != current_fingerprint
            ):
                raise ValueError(
                    "The provider registration changed while removal was running."
                )
            providers.pop(service_id, None)
            metadata["providers"] = providers
            voice.metadata_json = metadata
            voice.revision += 1
            voice.updated_at = utcnow()
            next_revision = voice.revision
        progress(1.0, f"{service_name} copy removed")
        return {
            "voice_id": voice_id,
            "service_id": service_id,
            "provider_voice_id": provider_voice_id,
            "remote_deleted": remote_deleted,
            "voice_revision": next_revision,
            "cancellation_requested_after_delete": cancel_event.is_set(),
        }

    def upload_rvc_model(self, payload, progress, cancel_event):
        from pandrator.logic import rvc_handler

        pth_artifact, pth_path = self._resolve_input(
            str(payload.get("pth_artifact_id") or "")
        )
        index_artifact, index_path = self._resolve_input(
            str(payload.get("index_artifact_id") or "")
        )
        if pth_path.suffix.lower() != ".pth":
            raise ValueError("The RVC weights artifact must be a .pth file.")
        if index_path.suffix.lower() not in {".index", ".idx"}:
            raise ValueError("The RVC index artifact must be an .index or .idx file.")
        if not rvc_handler.is_rvc_available():
            raise RuntimeError("The RVC service is not available.")
        progress(0.15, "Installing RVC model")
        model_root = self.paths.models / "rvc"
        model_root.mkdir(parents=True, exist_ok=True)
        model_name = rvc_handler.upload_rvc_model(
            str(pth_path), str(index_path), str(model_root)
        )
        if cancel_event.is_set():
            return {}
        manifest = model_root / model_name / "pandrator-model.json"
        manifest.write_text(
            json.dumps(
                {
                    "kind": "rvc",
                    "model_name": model_name,
                    "weights_artifact_id": pth_artifact.id,
                    "index_artifact_id": index_artifact.id,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact = self.artifacts.register(
            manifest,
            kind="model",
            role="rvc_model",
            parent_ids=[pth_artifact.id, index_artifact.id],
            metadata={"model_name": model_name},
        )
        progress(1.0, "RVC model ready")
        return {
            "model_name": model_name,
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
        }

    def convert_with_rvc(self, payload, progress, cancel_event):
        from pydub import AudioSegment

        from pandrator.logic import rvc_handler

        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        session_id = str(payload.get("session_id") or "") or source_artifact.session_id
        settings = dict(payload.get("settings") or {})
        if not str(settings.get("rvc_model") or "").strip():
            raise ValueError("Select an RVC model before conversion.")
        if not rvc_handler.is_rvc_available():
            raise RuntimeError("The RVC service is not available.")
        progress(0.1, "Loading source audio")
        audio = AudioSegment.from_file(source_path)
        if cancel_event.is_set():
            return {}
        progress(0.3, "Converting voice with RVC")
        converted = rvc_handler.process_with_rvc(
            audio, {**settings, "raise_on_error": True}
        )
        destination_dir = (
            self._session_dir(session_id)
            if session_id
            else self.paths.artifacts / "rvc"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = (
            destination_dir
            / f"{source_path.stem}-rvc-{hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:10]}.wav"
        )
        converted.export(destination, format="wav")
        artifact = self.artifacts.register(
            destination,
            kind="audio",
            role="rvc_audio",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"rvc_model": settings["rvc_model"]},
        )
        progress(1.0, "RVC audio ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "model_name": settings["rvc_model"],
        }

    def train_xtts(self, payload, progress, cancel_event):
        from pandrator.logic import xtts_trainer_handler

        training_id = str(payload.get("training_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        source_text_path = ""
        source_text_id = str(payload.get("source_text_artifact_id") or "")
        if source_text_id:
            _text_artifact, text_path = self._resolve_input(source_text_id)
            source_text_path = str(text_path)
        settings = dict(payload.get("settings") or {})
        model_name = str(
            payload.get("model_name") or settings.get("model_name") or ""
        ).strip()
        if not model_name:
            raise ValueError("An XTTS model name is required.")
        with self.database.session() as session:
            training = session.get(TrainingRun, training_id)
            if training is None:
                raise ValueError("Training record not found.")
            training.status = "running"
            training.updated_at = utcnow()
        progress(0.02, "Validating XTTS trainer")
        try:
            total_epochs = max(1, int(settings.get("epochs") or 6))
        except (TypeError, ValueError):
            total_epochs = 6
        last_training_fraction = 0.0
        zero_based_epochs: bool | None = None

        def training_output(line: str) -> None:
            nonlocal last_training_fraction, zero_based_epochs
            detail = str(line)[-500:]
            epoch_match = re.search(
                r"\bepoch\s*[:#-]?\s*(\d+)(?:\s*(?:/|of)\s*(\d+))?",
                detail,
                re.IGNORECASE,
            )
            percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", detail)
            if epoch_match:
                raw_epoch = max(0, int(epoch_match.group(1)))
                if raw_epoch == 0:
                    zero_based_epochs = True
                reported_total = max(
                    1,
                    int(epoch_match.group(2) or total_epochs),
                )
                # Training logs vary between zero- and one-based epoch labels.
                completed_before = (
                    raw_epoch if zero_based_epochs else max(0, raw_epoch - 1)
                )
                within_epoch = (
                    max(0.0, min(100.0, float(percent_match.group(1)))) / 100
                    if percent_match
                    else 0.0
                )
                last_training_fraction = max(
                    last_training_fraction,
                    min(1.0, (completed_before + within_epoch) / reported_total),
                )
                display_epoch = min(
                    reported_total,
                    raw_epoch + 1 if zero_based_epochs else max(1, raw_epoch),
                )
                detail = (
                    f"Training epoch {display_epoch} of {reported_total} — {detail}"
                )
            elif percent_match and total_epochs == 1:
                last_training_fraction = max(
                    last_training_fraction,
                    min(1.0, float(percent_match.group(1)) / 100),
                )
            progress(0.1 + 0.8 * last_training_fraction, detail)

        def training_status(line: str) -> None:
            detail = str(line)[-500:]
            normalized = detail.casefold()
            if "building" in normalized:
                value = 0.05
            elif "in progress" in normalized:
                value = 0.1
            elif "copying model" in normalized or "training finished" in normalized:
                value = 0.92
            elif "completed" in normalized:
                value = 0.98
            else:
                value = 0.1
            progress(value, detail)

        try:
            success, message = xtts_trainer_handler.start_training(
                {
                    **settings,
                    "model_name": model_name,
                    "source_audio_path": str(source_path),
                    "source_text_path": source_text_path,
                },
                output_callback=training_output,
                status_callback=training_status,
                stop_event=cancel_event,
            )
            if cancel_event.is_set():
                with self.database.session() as session:
                    training = session.get(TrainingRun, training_id)
                    training.status = "canceled"
                return {}
            if not success:
                raise RuntimeError(message)
            manifest_dir = self.paths.models / "xtts" / model_name
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest = manifest_dir / "pandrator-training.json"
            manifest.write_text(
                json.dumps(
                    {"kind": "xtts", "model_name": model_name, "message": message},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            artifact = self.artifacts.register(
                manifest,
                kind="model",
                role="xtts_model",
                parent_ids=[source_artifact.id]
                + ([source_text_id] if source_text_id else []),
                settings=settings,
                metadata={"model_name": model_name},
            )
            with self.database.session() as session:
                training = session.get(TrainingRun, training_id)
                training.status = "succeeded"
                training.output_artifact_id = artifact.id
                training.updated_at = utcnow()
            progress(1.0, "XTTS model ready")
            return {
                "training_id": training_id,
                "artifact_id": artifact.id,
                "model_name": model_name,
                "message": message,
            }
        except Exception as error:
            with self.database.session() as session:
                training = session.get(TrainingRun, training_id)
                if training is not None:
                    training.status = "failed"
                    training.error_message = str(error)
                    training.updated_at = utcnow()
            raise

    def clean_source(self, payload, progress, cancel_event):
        """Run deterministic extraction and the optional auditable agentic pipeline."""
        from pandrator.logic import file_handler, source_cleaning

        session_id = str(payload.get("session_id") or "")
        agent_run_id = str(payload.get("agent_run_id") or "")
        if agent_run_id:
            with self.database.session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run is not None:
                    run.status = "running"
                    run.updated_at = utcnow()
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        settings = dict(payload.get("settings") or {})
        pdf_config = source_cleaning.PDFIngestionConfig(
            ocr_mode=str(settings.get("pdf_ocr_mode") or "auto"),
            ocr_language=str(settings.get("pdf_ocr_language") or "auto"),
            ocr_dpi=int(settings.get("pdf_ocr_dpi") or 200),
        )
        deterministic_operations: list[dict[str, Any]] = []
        baseline_text = ""
        progress(0.05, "Extracting source text")
        extension = source_path.suffix.lower()
        if extension == ".txt":
            cleaned_text = source_path.read_text(encoding="utf-8-sig")
            baseline_text = cleaned_text
        elif extension == ".epub":
            cleaned_text = file_handler.extract_text_from_epub(
                str(source_path),
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
                filter_citations=bool(settings.get("filter_citations", True)),
            )
            baseline_text = cleaned_text
        elif extension == ".pdf":
            document = source_cleaning.build_source_document(
                str(source_path),
                pdf_config=pdf_config,
                artifact_dir=str(self._session_dir(session_id) / "source_ingestion"),
                progress_callback=_fraction_message_callback(
                    progress,
                    0.05,
                    0.35,
                ),
            )
            deterministic_operations = source_cleaning.propose_deterministic_operations(
                document,
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
                remove_toc=bool(settings.get("pdf_remove_toc", True)),
                remove_repeated_marginals=bool(
                    settings.get("pdf_remove_repeated_marginals", True)
                ),
            )
            baseline_text = document.plain_text()
            cleaned_text = source_cleaning.apply_cleaning_operations(
                document, deterministic_operations
            ).cleaned_text
        elif extension in {".docx", ".mobi"}:
            extracted = (
                self._session_dir(session_id) / f"{source_path.stem}_extracted.txt"
            )
            if not file_handler.convert_doc_to_text(str(source_path), str(extracted)):
                raise RuntimeError(f"Could not extract text from {source_path.name}.")
            cleaned_text = extracted.read_text(encoding="utf-8-sig")
            baseline_text = cleaned_text
        else:
            raise ValueError(f"Unsupported document type: {extension or 'unknown'}")
        if cancel_event.is_set():
            return {}
        progress(0.38, "Source extraction complete")
        extraction = "deterministic"
        report: dict[str, Any] = {}
        if bool(settings.get("agentic", False)):
            from .provider_settings import build_llm_settings

            progress(0.4, "Building source-cleaning index")
            if extension == ".epub":
                document = source_cleaning.build_cleaned_epub_source_document(
                    str(source_path),
                    cleaned_text,
                )
                deterministic_operations = (
                    source_cleaning.propose_embedded_chapter_operations(document)
                )
            elif extension == ".pdf":
                document = source_cleaning.build_source_document(
                    str(source_path),
                    pdf_config=pdf_config,
                    artifact_dir=str(
                        self._session_dir(session_id) / "source_ingestion"
                    ),
                    progress_callback=_fraction_message_callback(
                        progress,
                        0.4,
                        0.45,
                    ),
                )
            else:
                from pandrator.logic.source_cleaning.pdf_text_adapter import (
                    build_source_document_from_text,
                )

                document = build_source_document_from_text(
                    cleaned_text,
                    source_path=str(source_path),
                    filename=source_path.name,
                )
            llm_settings, model_name = build_llm_settings(
                self.database,
                self.paths,
                requested_model=str(
                    settings.get("model_name") or settings.get("default_model") or ""
                ),
                request_timeout_seconds=int(
                    settings.get("request_timeout_seconds") or 600
                ),
            )
            total_iterations = max(1, int(settings.get("max_iterations") or 53))
            phase_iterations = settings.get("phase_max_iterations")
            requested_phase_names = (
                settings.get("phase_names")
                if isinstance(settings.get("phase_names"), list)
                else None
            )
            phase_names = list(requested_phase_names or source_cleaning.PHASE_ORDER)
            phase_budgets = source_cleaning.resolve_phase_max_iterations(
                phase_iterations if isinstance(phase_iterations, dict) else None,
                total=total_iterations,
                phase_names=phase_names,
            )
            pipeline = source_cleaning.run_cleaning_pipeline(
                document,
                llm_settings=llm_settings,
                config=source_cleaning.SourceCleaningPipelineConfig(
                    model_name=model_name,
                    remove_footnotes=bool(settings.get("remove_footnotes", False)),
                    filter_citations=bool(settings.get("filter_citations", True)),
                    total_max_iterations=total_iterations,
                    phase_max_iterations=phase_iterations
                    if isinstance(phase_iterations, dict)
                    else None,
                    phase_names=requested_phase_names,
                    baseline_operations=deterministic_operations,
                ),
                progress_callback=_source_cleaning_progress_callback(
                    progress,
                    0.45,
                    0.9,
                    phase_names=phase_names,
                    phase_budgets=phase_budgets,
                ),
                stop_event=cancel_event,
            )
            if cancel_event.is_set():
                return {}
            progress(0.9, "Source-cleaning analysis complete")
            all_operations = [*deterministic_operations, *pipeline.all_operations]
            cleaning_result = source_cleaning.apply_cleaning_operations(
                document, all_operations
            )
            validation = source_cleaning.validate_cleaning_result(
                document,
                cleaning_result,
                remove_footnotes=bool(settings.get("remove_footnotes", False)),
            )
            cleaned_text = cleaning_result.cleaned_text
            report = {
                **cleaning_result.report,
                "pipeline": pipeline.to_dict(),
                "validation": validation.to_dict(),
                "warnings": pipeline.warnings
                + validation.warnings
                + cleaning_result.warnings,
            }
            audit_dir = self._session_dir(session_id) / "source_cleaning"
            source_cleaning.write_cleaning_artifacts(
                document,
                all_operations,
                cleaning_result,
                str(audit_dir),
            )
            usage = pipeline.llm_usage
            models = list(usage.get("models") or [])
            details = (
                usage.get("token_details")
                if isinstance(usage.get("token_details"), dict)
                else {}
            )
            with self.database.session() as session:
                session.add(
                    UsageEvent(
                        session_id=session_id,
                        stage="source_cleaning",
                        provider_key=(
                            models[0].split("/", 1)[0]
                            if models
                            else model_name.split("/", 1)[0]
                        ),
                        model_id=(models[0] if models else model_name),
                        input_tokens=int(usage.get("prompt_tokens") or 0),
                        cached_input_tokens=int(details.get("cached_tokens") or 0),
                        output_tokens=int(usage.get("completion_tokens") or 0),
                        cost_usd=float(usage["cost_usd"])
                        if usage.get("cost_usd") is not None
                        else None,
                        cost_source=",".join(usage.get("cost_sources") or []) or None,
                        raw_usage_json=usage,
                    )
                )
            extraction = "agentic"
        progress(0.93, "Saving source-cleaning artifacts")
        comparison_dir = self._session_dir(session_id) / "source_cleaning"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = comparison_dir / f"extracted-{new_id()}.txt"
        baseline_path.write_text(baseline_text, encoding="utf-8", newline="\n")
        baseline_artifact = self.artifacts.register(
            baseline_path,
            kind="text",
            role="extracted_text",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            metadata={"comparison_source": True, "source_filename": source_path.name},
        )
        destination = (
            self._operation_dir(session_id, "clean-source")
            / f"{source_path.stem}_cleaned.txt"
        )
        destination.write_text(cleaned_text, encoding="utf-8", newline="\n")
        artifact = self.artifacts.register(
            destination,
            kind="text",
            role="clean_text",
            session_id=session_id,
            parent_ids=[source_artifact.id, baseline_artifact.id],
            settings=settings,
            metadata={"extraction": extraction, "report": report},
        )
        if agent_run_id:
            pipeline_report = (
                report.get("pipeline")
                if isinstance(report.get("pipeline"), dict)
                else {}
            )
            phases = (
                pipeline_report.get("phases")
                if isinstance(pipeline_report.get("phases"), list)
                else []
            )
            with self.database.session() as session:
                run = session.get(AgentRun, agent_run_id)
                if run is not None:
                    run.status = "completed"
                    run.result_artifact_id = artifact.id
                    run.updated_at = utcnow()
                    for ordinal, phase in enumerate(phases):
                        safe_phase = (
                            phase if isinstance(phase, dict) else {"name": str(phase)}
                        )
                        operations = (
                            safe_phase.get("operations")
                            if isinstance(safe_phase.get("operations"), list)
                            else []
                        )
                        warnings = (
                            safe_phase.get("warnings")
                            if isinstance(safe_phase.get("warnings"), list)
                            else []
                        )
                        operation_types = sorted(
                            {
                                str(item.get("type") or item.get("operation") or "edit")
                                for item in operations
                                if isinstance(item, dict)
                            }
                        )
                        session.add(
                            AgentStep(
                                agent_run_id=agent_run_id,
                                ordinal=ordinal,
                                phase=str(
                                    safe_phase.get("name")
                                    or safe_phase.get("phase")
                                    or f"Phase {ordinal + 1}"
                                ),
                                status=str(safe_phase.get("status") or "completed"),
                                summary=str(
                                    safe_phase.get("summary")
                                    or f"{len(operations)} proposed operation(s), {len(warnings)} warning(s)."
                                ),
                                input_json={"operation_count": len(operations)},
                                output_json={
                                    "warnings": warnings,
                                    "operation_types": operation_types,
                                },
                            )
                        )
        progress(0.98, "Cleaned source artifacts registered")
        progress(1.0, "Source text ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "characters": len(cleaned_text),
            "report": report,
        }

    def prepare_source_cleaning_dispatch(self, payload, progress, cancel_event):
        """Prepare durable PDF/EPUB evidence without invoking a model provider."""
        from .source_cleaning_dispatch import prepare_source_cleaning_dispatch_job

        return prepare_source_cleaning_dispatch_job(
            self.database,
            self.artifacts,
            self._session_dir,
            payload,
            progress,
            cancel_event,
        )

    def prepare_text(self, payload, progress, cancel_event):
        from pandrator.logic.text_preprocessor import preprocess_text

        from .workspace import BUILTIN_DEFAULTS

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        settings = dict(payload.get("settings") or {})
        if source_path.suffix.lower() not in {".txt", ".md"}:
            raise ValueError("Prepare narration requires a cleaned text artifact.")
        text = source_path.read_text(encoding="utf-8-sig")
        record = self._session_record(session_id)
        source_language = str(record.source_language or "auto")
        text_defaults = BUILTIN_DEFAULTS["text"]
        progress(0.1, "Segmenting narration")
        prepared = preprocess_text(
            text,
            {
                "source_file": str(source_path),
                "language": source_language,
                # Segmentation is intentionally provider-independent.  This
                # selects the shared multilingual sentence tokenizer only.
                "tts_service": "XTTS",
                "max_sentence_length": int(
                    settings.get("max_sentence_length")
                    or text_defaults["max_sentence_length"]
                ),
                "enable_sentence_splitting": bool(
                    settings.get(
                        "enable_sentence_splitting",
                        text_defaults["enable_sentence_splitting"],
                    )
                ),
                "enable_sentence_appending": bool(
                    settings.get(
                        "enable_sentence_appending",
                        text_defaults["enable_sentence_appending"],
                    )
                ),
                "enable_nemo_normalization": bool(
                    settings.get(
                        "enable_nemo_normalization",
                        text_defaults["enable_nemo_normalization"],
                    )
                ),
                "remove_diacritics": bool(
                    settings.get(
                        "remove_diacritics", text_defaults["remove_diacritics"]
                    )
                ),
                "remove_quotation_marks": bool(
                    settings.get(
                        "remove_quotation_marks",
                        text_defaults["remove_quotation_marks"],
                    )
                ),
                "normalize_all_caps": bool(
                    settings.get(
                        "normalize_all_caps", text_defaults["normalize_all_caps"]
                    )
                ),
            },
            progress_callback=_scaled_progress_callback(progress, 0.1, 0.85),
        )
        if cancel_event.is_set():
            return {}
        progress(0.9, "Saving narration segments")
        destination = (
            self._operation_dir(session_id, "prepare-text") / "prepared_narration.json"
        )
        destination.write_text(
            json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        artifact = self.artifacts.register(
            destination,
            kind="json",
            role="prepared_text",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
            metadata={"segment_count": len(prepared)},
        )
        generation_revision_id, _segment_ids = self._store_generation_plan(
            session_id,
            prepared,
            settings=settings,
            source_revision_id=str(
                (source_artifact.metadata_json or {}).get("revision_id") or ""
            )
            or None,
            source_artifact_id=artifact.id,
        )
        progress(1.0, "Narration segments ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "segments": len(prepared),
            "generation_plan_revision_id": generation_revision_id,
        }

    def _store_generation_plan(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        source_revision_id: str | None = None,
        source_artifact_id: str | None = None,
    ) -> tuple[str, list[str]]:
        clean = [
            item
            for item in records
            if str(item.get("text") or item.get("original_sentence") or "").strip()
        ]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "records": clean,
                    "settings": _generation_segmentation_settings(settings),
                    "source_revision_id": source_revision_id,
                    "source_artifact_id": source_artifact_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        with self.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            if plan is not None and plan.active_revision_id:
                active = session.get(GenerationPlanRevision, plan.active_revision_id)
                if active is not None and active.content_hash == digest:
                    # Identical source content and segmentation settings: keep
                    # the existing segments so takes, edits, and run history
                    # stay attached instead of being orphaned by a new revision.
                    segment_ids = list(
                        session.scalars(
                            select(GenerationSegment.id)
                            .where(GenerationSegment.plan_revision_id == active.id)
                            .order_by(GenerationSegment.ordinal)
                        ).all()
                    )
                    return active.id, [str(segment_id) for segment_id in segment_ids]
            if plan is None:
                plan = GenerationPlan(session_id=session_id)
                session.add(plan)
                session.flush()
            maximum = (
                session.scalar(
                    select(func.max(GenerationPlanRevision.revision_number)).where(
                        GenerationPlanRevision.plan_id == plan.id
                    )
                )
                or 0
            )
            stored_settings = _secret_free_tts_settings(settings)
            if source_artifact_id:
                stored_settings["_source_artifact_id"] = source_artifact_id
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                source_revision_id=source_revision_id,
                revision_number=int(maximum) + 1,
                settings_json=stored_settings,
                content_hash=digest,
            )
            session.add(revision)
            session.flush()
            segment_ids = []
            for ordinal, record in enumerate(clean):
                is_subtitle = self._is_subtitle_generation_record(record)
                explicit_language = (
                    self._usable_language(record.get("language")) or None
                )
                explicit_voice = str(record.get("voice") or "").strip() or None
                segment = GenerationSegment(
                    plan_revision_id=revision.id,
                    ordinal=ordinal,
                    source_segment_ids_json=list(
                        record.get("source_segment_ids")
                        or record.get("subtitles")
                        or []
                    ),
                    alignment_group=str(record.get("alignment_group") or "").strip()
                    or None,
                    node_kind=str(
                        record.get("node_kind")
                        or (
                            "subtitle_cue"
                            if is_subtitle
                            else "chapter_marker"
                            if str(record.get("chapter") or "").lower() == "yes"
                            else "paragraph"
                        )
                    ),
                    paragraph_break_after=False
                    if is_subtitle
                    else bool(
                        record.get(
                            "paragraph_break_after",
                            str(record.get("paragraph") or "").lower() == "yes",
                        )
                    ),
                    speaker=str(record.get("speaker") or "").strip() or None,
                    text=str(
                        record.get("text") or record.get("original_sentence") or ""
                    ).strip(),
                    optimized_text=(
                        str(record.get("tts_optimized_sentence") or "").strip() or None
                    ),
                    speech_plan_json=dict(record.get("speech_plan") or {}),
                    optimization_status=(
                        "optimized"
                        if str(record.get("tts_optimized_sentence") or "").strip()
                        else "not_requested"
                    ),
                    optimization_source_hash=(
                        self._optimization_text_hash(
                            str(
                                record.get("text")
                                or record.get("original_sentence")
                                or ""
                            ).strip()
                        )
                        if str(record.get("tts_optimized_sentence") or "").strip()
                        else None
                    ),
                    optimization_model=(
                        str(
                            (record.get("speech_plan") or {}).get("model") or ""
                        ).strip()
                        or None
                    ),
                    voice_id=record.get("voice_id"),
                    voice=explicit_voice,
                    # A missing value is meaningful: it follows the session TTS
                    # language and remains responsive to later settings changes.
                    language=explicit_language,
                    silence_after_ms=_default_silence_after_ms(
                        record, settings, is_subtitle=is_subtitle
                    ),
                    marked=bool(record.get("marked", False)),
                )
                session.add(segment)
                session.flush()
                segment_ids.append(segment.id)
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            return revision.id, segment_ids

    @staticmethod
    def _tts_urls(settings: dict[str, Any]) -> dict[str, str]:
        return {
            key: str(settings.get(setting_key) or default)
            for key, setting_key, default in (
                ("audio_cpp_base_url", "audio_cpp_base_url", "http://127.0.0.1:8060"),
                ("xtts_base_url", "xtts_base_url", "http://127.0.0.1:8020"),
                ("voxcpm_base_url", "voxcpm_base_url", "http://127.0.0.1:8020"),
                ("fishs2_base_url", "fishs2_base_url", "http://127.0.0.1:8020"),
                ("voxtral_base_url", "voxtral_base_url", "http://127.0.0.1:8000"),
                ("kokoro_base_url", "kokoro_base_url", "http://127.0.0.1:8880"),
                ("silero_base_url", "silero_base_url", "http://127.0.0.1:8001"),
                ("chatterbox_base_url", "chatterbox_base_url", "http://127.0.0.1:8040"),
                (
                    "kobold_qwen_base_url",
                    "kobold_qwen_base_url",
                    "http://127.0.0.1:8042",
                ),
                ("magpie_base_url", "magpie_base_url", "http://127.0.0.1:8030"),
            )
        }

    def _negotiated_tts_batch_size(
        self,
        settings: dict[str, Any],
        tts_urls: dict[str, str],
    ) -> int:
        """Return one unless the selected service advertises streaming batches."""
        try:
            requested = max(
                1,
                min(32, int(settings.get("tts_batch_size") or 10)),
            )
        except (TypeError, ValueError):
            requested = 10
        if requested == 1:
            return 1

        capabilities = self.tts_providers.synthesis_capabilities(
            settings,
            **tts_urls,
        )
        if not (capabilities.batch_synthesis and capabilities.streaming_batch):
            return 1
        return min(requested, max(1, capabilities.max_batch_size))

    def _start_streaming_tts_batch(
        self,
        items: list[tuple[str, str, dict[str, Any]]],
        *,
        batch_size: int,
        tts_urls: dict[str, str],
        cancel_event,
    ):
        from .tts_providers import TtsBatchItem

        return iter(
            self.tts_providers.synthesize_batch(
                [
                    TtsBatchItem(id=item_id, text=text, settings=item_settings)
                    for item_id, text, item_settings in items
                ],
                batch_size=batch_size,
                cancel_event=cancel_event,
                **tts_urls,
            )
        )

    def _ensure_qwen_cloned_voice(
        self,
        settings: dict[str, Any],
        *,
        base_url: str,
        verified: set[str],
        cancel_event,
    ) -> None:
        """Restore a stale managed Qwen voice once, without silent fallback."""
        from pandrator.logic import tts_handler

        if self.tts_providers.service_id_for_settings(settings) != "kobold_qwen":
            return
        model = tts_handler.resolve_kobold_qwen_model(settings)
        if str(model).strip().lower() not in {
            "voice cloning",
            "qwen3-tts",
            "qwen3-tts-base",
        }:
            return
        requested_voice = str(
            settings.get("speaker")
            or settings.get("voice")
            or tts_handler.KOBOLD_QWEN_SAMPLE_VOICE
        ).strip()
        voice_key = requested_voice.removesuffix(".wav").lower()
        if not voice_key or voice_key == tts_handler.KOBOLD_QWEN_SAMPLE_VOICE.lower():
            return
        if voice_key in verified:
            return

        catalogue = tts_handler.get_kobold_qwen_voice_catalog(base_url)
        available = {
            str(item.get("id") or "").removesuffix(".wav").lower()
            for item in catalogue
            if str(item.get("type") or "").lower() != "preset"
        }
        if voice_key in available:
            verified.add(voice_key)
            return

        managed_voice_id = ""
        metadata_service_id = "kobold_qwen"
        with self.database.session() as session:
            for managed_voice in session.scalars(select(Voice)).all():
                providers = dict(
                    (managed_voice.metadata_json or {}).get("providers") or {}
                )
                for provider_id, record in providers.items():
                    normalized_provider = (
                        str(provider_id)
                        .strip()
                        .lower()
                        .replace("-", "_")
                        .replace(" ", "_")
                    )
                    if normalized_provider not in {
                        "kobold_qwen",
                        "qwen",
                        "qwen3",
                        "qwen3_tts",
                    } or not isinstance(record, dict):
                        continue
                    provider_voice = (
                        str(record.get("voice_id") or managed_voice.name)
                        .removesuffix(".wav")
                        .lower()
                    )
                    if provider_voice == voice_key:
                        managed_voice_id = managed_voice.id
                        metadata_service_id = str(provider_id)
                        break
                if managed_voice_id:
                    break

        if not managed_voice_id:
            raise ValueError(
                f"Qwen voice reference '{requested_voice}' is not installed and "
                "has no managed sample to restore. Publish it from the Voice Library."
            )
        if cancel_event is not None and cancel_event.is_set():
            return
        logger.info(
            "Qwen voice '%s' is absent from the live catalogue; republishing managed voice %s.",
            requested_voice,
            managed_voice_id,
        )
        self.publish_voice(
            {
                "voice_id": managed_voice_id,
                "service_id": metadata_service_id,
                "service": "Qwen3 TTS",
                "base_url": base_url,
            },
            lambda _value, detail=None: logger.info("%s", detail) if detail else None,
            cancel_event,
        )
        verified.add(voice_key)

    @staticmethod
    def _optimization_text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _save_speech_plan_proposals(
        self,
        *,
        library,
        session_id: str,
        plan: dict[str, Any],
        backend: str,
        model_name: str,
        default_language: str,
    ) -> list[dict[str, Any]]:
        """Persist model pronunciations as review-only entries, never active ones."""
        candidates = {
            str(item.get("id") or ""): item
            for item in list(plan.get("candidates") or [])
            if isinstance(item, dict)
        }
        proposed_items: list[tuple[str, str, str, str]] = []
        for decision in list(plan.get("decisions") or []):
            if (
                isinstance(decision, dict)
                and decision.get("action") == "pronounce"
                and str(decision.get("spoken") or "").strip()
            ):
                candidate = candidates.get(str(decision.get("span_id") or ""))
                if candidate:
                    proposed_items.append(
                        (
                            str(candidate.get("text") or ""),
                            str(decision.get("spoken") or ""),
                            str(decision.get("confidence") or "medium"),
                            str(candidate.get("id") or ""),
                        )
                    )
        for discovery in list(plan.get("discoveries") or []):
            if (
                isinstance(discovery, dict)
                and discovery.get("action") == "pronounce"
                and str(discovery.get("spoken") or "").strip()
            ):
                proposed_items.append(
                    (
                        str(discovery.get("source_text") or ""),
                        str(discovery.get("spoken") or ""),
                        str(discovery.get("confidence") or "medium"),
                        "discovery",
                    )
                )

        proposals: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source_form, phonetic, confidence, span_id in proposed_items:
            key = (source_form.casefold().strip(), phonetic)
            if not source_form.strip() or key in seen:
                continue
            seen.add(key)
            try:
                proposal = library.propose(
                    session_id=session_id,
                    source_form=source_form,
                    phonetic=phonetic,
                    language=str(plan.get("language") or default_language),
                    backend=backend,
                    metadata={
                        "case_id": plan.get("case_id"),
                        "model": model_name,
                        "confidence": confidence,
                        "span_id": span_id,
                    },
                )
            except (KeyError, ValueError) as error:
                proposals.append(
                    {
                        "source_form": source_form,
                        "phonetic": phonetic,
                        "status": "not_saved",
                        "error": str(error),
                    }
                )
            else:
                proposals.append(
                    {
                        "id": proposal["id"],
                        "source_form": proposal["source_form"],
                        "phonetic": proposal["phonetic"],
                        "status": proposal["status"],
                        "revision": proposal["revision"],
                    }
                )
        return proposals

    def _optimize_generation_texts(
        self,
        session_id: str,
        segment_ids: list[str],
        texts: list[str],
        settings: dict[str, Any],
        cancel_event,
        progress,
        *,
        job_id: str | None = None,
        generation_run_id: str | None = None,
        pronunciation_settings: dict[str, Any] | None = None,
        pronunciation_language: str | None = None,
        pronunciation_voice_language: str | None = None,
    ) -> tuple[list[str], str]:
        """Resolve reviewed or newly batched inline optimization for generation."""
        from .pronunciations import (
            PronunciationLibrary,
            apply_reviewed_pronunciations,
            normalize_backend,
        )

        apply_reviewed = (
            settings.get("apply_reviewed_pronunciations", True) is not False
        )
        # Targeted regeneration may synthesize with a selected alternate TTS
        # runtime while retaining the immutable source-run settings.  Keep
        # pronunciation lookup on an explicit, secret-free context so the
        # alternate provider/language can scope reviewed entries without
        # changing the persisted run snapshot.
        pronunciation_context = (
            pronunciation_settings if pronunciation_settings is not None else settings
        )
        pronunciation_language_override = str(pronunciation_language or "").strip()
        pronunciation_voice_language_override = str(
            pronunciation_voice_language or ""
        ).strip()
        pronunciation_service = str(
            pronunciation_context.get("service")
            or pronunciation_context.get("tts_service")
            or pronunciation_context.get("backend")
            or ""
        ).strip()
        pronunciation_endpoint = str(
            pronunciation_context.get("openai_audio_endpoint") or ""
        ).strip()
        if pronunciation_endpoint and pronunciation_service.casefold().replace(
            "-", "_"
        ) in {
            "custom",
            "openai_compatible",
            "openai_compatible_service",
        }:
            pronunciation_backend_value = pronunciation_endpoint
        else:
            pronunciation_backend_value = (
                pronunciation_service or pronunciation_endpoint or "*"
            )
        pronunciation_backend = normalize_backend(pronunciation_backend_value)
        if not bool(settings.get("llm_tts_optimization")):
            pronunciation_library = PronunciationLibrary(self.database)
            default_language = str(
                settings.get("language")
                or settings.get("target_language")
                or settings.get("source_language")
                or "en"
            )
            entries_by_position: dict[int, list[dict[str, Any]]] = {}
            if apply_reviewed:
                with self.database.session() as session:
                    for position, (segment_id, text) in enumerate(
                        zip(segment_ids, texts, strict=True)
                    ):
                        segment = session.get(GenerationSegment, segment_id)
                        language = (
                            pronunciation_language_override
                            or str(
                                segment.language if segment is not None else ""
                            ).strip()
                            or default_language
                        )
                        entries_by_position[position] = pronunciation_library.resolve(
                            text,
                            session_id=session_id,
                            language=language,
                            backend=pronunciation_backend,
                        )
            output = list(texts)
            manual_override_positions: set[int] = set()
            model_name = ""
            reuse_saved = bool(settings.get("use_existing_speech_plans"))
            with self.database.session() as session:
                for position, (segment_id, text) in enumerate(
                    zip(segment_ids, texts, strict=True)
                ):
                    segment = session.get(GenerationSegment, segment_id)
                    if (
                        segment is None
                        or not segment.optimized_text
                        or segment.optimization_source_hash
                        != self._optimization_text_hash(text)
                        or segment.optimization_status not in {"optimized", "reviewed"}
                    ):
                        continue
                    speech_plan = dict(segment.speech_plan_json or {})
                    is_manual_override = speech_plan.get("status") == "manual_override"
                    if not is_manual_override and not reuse_saved:
                        continue
                    output[position] = segment.optimized_text
                    if is_manual_override:
                        manual_override_positions.add(position)
                    else:
                        model_name = model_name or str(segment.optimization_model or "")
            if apply_reviewed:
                output = [
                    revised
                    if position in manual_override_positions
                    else apply_reviewed_pronunciations(
                        revised,
                        entries_by_position.get(position, []),
                    )
                    for position, revised in enumerate(output)
                ]
            return output, model_name

        from copy import deepcopy
        from types import SimpleNamespace

        from .tts_optimization import optimize_texts

        resolved = self._with_database_llm_settings(dict(settings), "tts_optimization")
        llm_settings = SimpleNamespace(
            provider_configs=resolved["llm_provider_configs"],
            default_model=resolved["llm_default_model"],
            request_timeout_seconds=resolved["request_timeout_seconds"],
        )
        model_name = str(
            resolved.get("tts_optimization_model") or resolved["llm_default_model"]
        )
        speech_mode = (
            str(resolved.get("speech_optimization_mode") or "guarded").strip().lower()
        )
        structured_mode = speech_mode in {"guarded", "flexible"}
        from .speech_planning import SPEECH_PROMPT_REVISION

        default_language = str(
            resolved.get("language")
            or resolved.get("target_language")
            or resolved.get("source_language")
            or "en"
        )
        voice_language = str(
            resolved.get("voice_language")
            or resolved.get("language")
            or default_language
        )
        if pronunciation_voice_language_override:
            voice_language = pronunciation_voice_language_override
        elif pronunciation_language_override:
            voice_language = pronunciation_language_override
        backend = normalize_backend(
            resolved.get("service")
            or resolved.get("tts_service")
            or resolved.get("backend")
            or "*"
        )
        if pronunciation_settings is not None:
            backend = pronunciation_backend
        pronunciation_library = PronunciationLibrary(self.database)
        output = list(texts)
        pending_texts: list[str] = []
        pending_positions: list[int] = []
        pending_languages: list[str] = []
        pending_voice_languages: list[str] = []
        segment_state: dict[str, dict[str, Any]] = {}
        with self.database.session() as session:
            for segment_id in segment_ids:
                segment = session.get(GenerationSegment, segment_id)
                if segment is not None:
                    segment_state[segment_id] = {
                        "optimized_text": segment.optimized_text,
                        "optimization_source_hash": segment.optimization_source_hash,
                        "optimization_status": segment.optimization_status,
                        "optimization_model": segment.optimization_model,
                        "speech_plan": deepcopy(segment.speech_plan_json or {}),
                        "language": str(segment.language or default_language),
                    }

        known_by_position: dict[int, list[dict[str, Any]]] = {}
        for position, (segment_id, text) in enumerate(
            zip(segment_ids, texts, strict=True)
        ):
            state = segment_state.get(segment_id, {})
            language = pronunciation_language_override or str(
                state.get("language") or default_language
            )
            known = (
                pronunciation_library.resolve(
                    text,
                    session_id=session_id,
                    language=language,
                    backend=backend,
                )
                if apply_reviewed
                else []
            )
            known_by_position[position] = known
            source_hash = self._optimization_text_hash(text)
            plan_source_hash = self._optimization_text_hash(" ".join(text.split()))
            plan = dict(state.get("speech_plan") or {})
            current_known_signature = sorted(
                (str(item.get("id") or ""), int(item.get("revision") or 0))
                for item in known
            )
            planned_known_signature = sorted(
                (str(item.get("entry_id") or ""), int(item.get("entry_revision") or 0))
                for item in list(plan.get("known_pronunciations") or [])
            )
            persisted_language = str(plan.get("language") or "").strip()
            persisted_voice_language = str(plan.get("voice_language") or "").strip()
            plan_context_matches = True
            if (
                pronunciation_language_override
                or pronunciation_voice_language_override
                or persisted_language
                or persisted_voice_language
            ):
                # An explicitly selected alternate language changes the speech
                # planning contract. Legacy plans without these fields must
                # not silently cross that boundary. When persisted fields are
                # present, ordinary reuse also requires them to match.
                plan_context_matches = bool(
                    persisted_language
                    and persisted_voice_language
                    and persisted_language.casefold() == language.casefold()
                    and persisted_voice_language.casefold() == voice_language.casefold()
                )
            reusable = bool(
                state
                and state.get("optimized_text")
                and state.get("optimization_source_hash") == source_hash
                and state.get("optimization_status") in {"optimized", "reviewed"}
            )
            if reusable and state.get("optimization_status") != "reviewed":
                if structured_mode:
                    reusable = bool(
                        plan
                        and plan.get("source_hash") == plan_source_hash
                        and plan.get("mode_requested") == speech_mode
                        and plan.get("model") == model_name
                        and plan.get("prompt_revision") == SPEECH_PROMPT_REVISION
                        and planned_known_signature == current_known_signature
                        and plan_context_matches
                    )
                else:
                    reusable = (
                        not plan and state.get("optimization_model") == model_name
                    )
            if reusable:
                revised = str(state["optimized_text"])
                if apply_reviewed and not structured_mode:
                    revised = apply_reviewed_pronunciations(
                        revised,
                        known_by_position.get(position, []),
                    )
                output[position] = revised
                continue
            pending_positions.append(position)
            pending_texts.append(text)
            pending_languages.append(language)
            pending_voice_languages.append(voice_language)

        with self.database.session() as session:
            for position in pending_positions:
                segment = session.get(GenerationSegment, segment_ids[position])
                if segment is not None:
                    segment.optimization_status = "running"
                    segment.optimization_reviewed = False
                    segment.optimization_model = model_name
                    segment.speech_plan_json = {}
                    segment.updated_at = utcnow()

        if not pending_texts:
            return output, model_name

        def persist_batch(items: list[tuple[int, str]]) -> None:
            with self.database.session() as session:
                for local_index, revised in items:
                    position = pending_positions[local_index]
                    if apply_reviewed and not structured_mode:
                        revised = apply_reviewed_pronunciations(
                            revised,
                            known_by_position.get(position, []),
                        )
                    output[position] = revised
                    segment = session.get(GenerationSegment, segment_ids[position])
                    if segment is None:
                        continue
                    segment.optimized_text = revised
                    segment.optimization_status = "optimized"
                    segment.optimization_source_hash = self._optimization_text_hash(
                        texts[position]
                    )
                    segment.optimization_reviewed = False
                    segment.optimization_model = model_name
                    segment.updated_at = utcnow()

        def resolve_pending_pronunciations(
            _text: str,
            _language: str,
        ) -> list[dict[str, Any]]:
            # Structured optimization calls this from worker threads. Resolution
            # was performed before dispatch so workers never share ORM sessions.
            for local_index, position in enumerate(pending_positions):
                if (
                    pending_texts[local_index] == _text
                    and pending_languages[local_index] == _language
                ):
                    return deepcopy(known_by_position.get(position, []))
            return []

        def persist_plan_batch(
            items: list[tuple[int, str, dict[str, Any]]],
        ) -> None:
            for local_index, revised, plan in items:
                position = pending_positions[local_index]
                plan["language"] = pending_languages[local_index]
                plan["voice_language"] = pending_voice_languages[local_index]
                proposals: list[dict[str, Any]] = []
                if bool(resolved.get("speech_plan_save_proposals", True)):
                    proposals = self._save_speech_plan_proposals(
                        library=pronunciation_library,
                        session_id=session_id,
                        plan=plan,
                        backend=backend,
                        model_name=model_name,
                        default_language=pending_languages[local_index],
                    )
                plan["proposals"] = proposals
                with self.database.session() as session:
                    segment = session.get(GenerationSegment, segment_ids[position])
                    if segment is None:
                        continue
                    segment.optimized_text = revised
                    segment.speech_plan_json = plan
                    segment.optimization_status = "optimized"
                    segment.optimization_source_hash = self._optimization_text_hash(
                        texts[position]
                    )
                    segment.optimization_reviewed = False
                    segment.optimization_model = model_name
                    segment.updated_at = utcnow()

        try:
            optimized, usage = optimize_texts(
                pending_texts,
                resolved,
                llm_settings,
                model_name,
                cancel_event,
                progress,
                on_batch=persist_batch,
                on_plan_batch=persist_plan_batch if structured_mode else None,
                known_pronunciation_resolver=(
                    resolve_pending_pronunciations if structured_mode else None
                ),
                languages=pending_languages,
                voice_languages=pending_voice_languages,
            )
        except Exception:
            with self.database.session() as session:
                for position in pending_positions:
                    segment = session.get(GenerationSegment, segment_ids[position])
                    if segment is not None and segment.optimization_status == "running":
                        segment.optimization_status = "failed"
                        segment.updated_at = utcnow()
            raise
        for local_index, revised in enumerate(optimized):
            position = pending_positions[local_index]
            if apply_reviewed and not structured_mode:
                revised = apply_reviewed_pronunciations(
                    revised,
                    known_by_position.get(position, []),
                )
            output[position] = revised
        self._record_usage(
            session_id,
            "tts_optimization",
            resolved,
            usage,
            job_id=job_id,
            generation_run_id=generation_run_id,
        )
        return output, model_name

    def _generate_audio(
        self,
        session_id: str,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        progress,
        cancel_event,
        *,
        role: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        from .audio_assembly import (
            AudioAssemblyPart,
            assemble_audio_plan,
            build_audio_assembly_plan,
            preferred_pcm_format,
        )
        from .media_process import MediaProcessCancelled

        settings = hydrate_tts_settings(
            self.database,
            self.paths,
            settings,
            manager_bridge=self.manager_bridge,
        )

        if source_path.suffix.lower() != ".json":
            raise ValueError(
                "Audio generation requires segmented narration. Run Segment narration first."
            )
        try:
            records = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(
                "The segmented narration artifact is invalid JSON. Run Segment narration again."
            ) from error
        if not isinstance(records, list) or not records:
            raise ValueError("No narration segments were found.")
        records = [
            record
            for record in records
            if str(record.get("text") or record.get("original_sentence") or "").strip()
        ]
        if not records:
            raise ValueError("No non-empty narration segments were found.")
        assembly_inputs: list[tuple[Path, int, int]] = []
        take_artifact_ids: list[str] = []
        revision_id = ""
        generation_segment_ids: list[str] = []
        if source_artifact.role == "prepared_text":
            with self.database.session() as session:
                plan = session.scalar(
                    select(GenerationPlan).where(
                        GenerationPlan.session_id == session_id
                    )
                )
                if plan and plan.active_revision_id:
                    segments = list(
                        session.scalars(
                            select(GenerationSegment)
                            .where(
                                GenerationSegment.plan_revision_id
                                == plan.active_revision_id,
                                GenerationSegment.removed.is_(False),
                            )
                            .order_by(GenerationSegment.ordinal)
                        ).all()
                    )
                    if segments:
                        revision_id = plan.active_revision_id
                        generation_segment_ids = [segment.id for segment in segments]
                        records = [
                            {
                                "text": segment.text,
                                "language": segment.language,
                                "voice": segment.voice,
                                "speaker": segment.speaker,
                                "node_kind": segment.node_kind,
                                "paragraph_break_after": segment.paragraph_break_after,
                                "silence_after_ms": segment.silence_after_ms,
                                "source_segment_ids": segment.source_segment_ids_json,
                            }
                            for segment in segments
                        ]
        if not revision_id:
            revision_id, generation_segment_ids = self._store_generation_plan(
                session_id,
                records,
                settings=settings,
                source_revision_id=str(
                    (source_artifact.metadata_json or {}).get("revision_id") or ""
                )
                or None,
                source_artifact_id=source_artifact.id,
            )
        source_texts = [
            str(record.get("text") or record.get("original_sentence") or "").strip()
            for record in records
        ]
        optimization_share = 0.25 if bool(settings.get("llm_tts_optimization")) else 0.0
        optimized_texts, optimization_model = self._optimize_generation_texts(
            session_id,
            generation_segment_ids,
            source_texts,
            settings,
            cancel_event,
            lambda value, detail=None: progress(
                float(value) * optimization_share, detail
            ),
            job_id=job_id,
        )
        verified_qwen_voices: set[str] = set()
        tts_urls = self._tts_urls(settings)
        batch_results = None
        batch_contexts: dict[str, dict[str, Any]] = {}
        effective_batch_size = self._negotiated_tts_batch_size(
            settings,
            tts_urls,
        )
        if effective_batch_size > 1:
            batch_items: list[tuple[str, str, dict[str, Any]]] = []
            for record, generation_segment_id, synthesized_text in zip(
                records,
                generation_segment_ids,
                optimized_texts,
                strict=True,
            ):
                segment_tts_settings = _apply_segment_tts_overrides(
                    settings,
                    language=self._usable_language(record.get("language")),
                    voice=str(record.get("voice") or "").strip() or None,
                )
                segment_tts_settings = self.prepare_audio_cpp_voice_reference(
                    segment_tts_settings
                )
                self._ensure_qwen_cloned_voice(
                    segment_tts_settings,
                    base_url=tts_urls["kobold_qwen_base_url"],
                    verified=verified_qwen_voices,
                    cancel_event=cancel_event,
                )
                batch_contexts[generation_segment_id] = {
                    "settings": segment_tts_settings,
                    "synthesized_text": synthesized_text,
                }
                batch_items.append(
                    (
                        generation_segment_id,
                        synthesized_text,
                        segment_tts_settings,
                    )
                )
            if batch_items:
                batch_results = self._start_streaming_tts_batch(
                    batch_items,
                    batch_size=effective_batch_size,
                    tts_urls=tts_urls,
                    cancel_event=cancel_event,
                )
                logger.info(
                    "Using streaming %s-item TTS batches for %d automatic generation segments.",
                    effective_batch_size,
                    len(batch_items),
                )
        for index, (record, generation_segment_id) in enumerate(
            zip(records, generation_segment_ids, strict=True),
            start=1,
        ):
            if cancel_event.is_set():
                return {}
            text = str(
                record.get("text") or record.get("original_sentence") or ""
            ).strip()
            if not text:
                continue
            synthesis_share = 1.0 - optimization_share
            progress(
                optimization_share + ((index - 1) / len(records)) * synthesis_share,
                f"Generating segment {index} of {len(records)}",
            )
            batch_context = batch_contexts.get(generation_segment_id)
            if batch_context is not None:
                synthesized_text = str(batch_context["synthesized_text"])
                segment_tts_settings = dict(batch_context["settings"])
            else:
                synthesized_text = optimized_texts[index - 1]
                segment_tts_settings = _apply_segment_tts_overrides(
                    settings,
                    language=self._usable_language(record.get("language")),
                    voice=str(record.get("voice") or "").strip() or None,
                )
                segment_tts_settings = self.prepare_audio_cpp_voice_reference(
                    segment_tts_settings
                )
                self._ensure_qwen_cloned_voice(
                    segment_tts_settings,
                    base_url=tts_urls["kobold_qwen_base_url"],
                    verified=verified_qwen_voices,
                    cancel_event=cancel_event,
                )

            def synthesize_one(
                *,
                text_to_synthesize: str = synthesized_text,
                settings_for_segment: dict[str, Any] = segment_tts_settings,
                segment_index: int = index,
            ):
                return self.tts_providers.synthesize(
                    text_to_synthesize,
                    settings_for_segment,
                    max_attempts=int(settings_for_segment.get("max_attempts") or 5),
                    cancel_event=cancel_event,
                    retry_callback=lambda attempt, total, delay: progress(
                        optimization_share
                        + ((segment_index - 1) / len(records)) * synthesis_share,
                        f"Retrying segment {segment_index} ({attempt}/{total}) in {delay:.1f}s",
                    ),
                    recovery_callback=lambda cycle, total, timeout: progress(
                        optimization_share
                        + ((segment_index - 1) / len(records)) * synthesis_share,
                        f"Waiting for Qwen3 TTS before segment {segment_index} ({cycle}/{total}, up to {timeout:.0f}s)",
                    ),
                    **tts_urls,
                )

            if batch_results is not None and batch_context is not None:
                try:
                    batch_result = next(batch_results)
                except StopIteration as error:
                    raise RuntimeError(
                        "The streaming TTS batch ended before every segment completed."
                    ) from error
                if batch_result.id != generation_segment_id:
                    raise RuntimeError(
                        "The streaming TTS batch returned segments out of order."
                    )
                if batch_result.error is not None:
                    if not batch_result.error.retryable:
                        raise batch_result.error
                    logger.warning(
                        "Streaming TTS batch failed for segment %s; retrying it through the ordinary synthesis path.",
                        generation_segment_id,
                    )
                    audio = synthesize_one()
                else:
                    audio = batch_result.audio
            else:
                audio = synthesize_one()
            if audio is None:
                raise RuntimeError(f"Speech generation failed at segment {index}.")
            verification = self._verification_metadata(
                audio,
                synthesized_text,
                segment_tts_settings,
            )
            take_dir = (
                self._session_dir(session_id)
                / "generation"
                / revision_id
                / generation_segment_id
            )
            take_dir.mkdir(parents=True, exist_ok=True)
            sentence_path = take_dir / f"tts-{new_id()}.wav"
            exported = audio.export(sentence_path, format="wav")
            exported.close()
            take_artifact = self.artifacts.register(
                sentence_path,
                kind="audio",
                role="generation_take",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=_secret_free_tts_settings(segment_tts_settings),
                metadata={
                    "generation_segment_id": generation_segment_id,
                    "kind": "tts",
                    "source_text": text,
                    "synthesized_text": synthesized_text,
                    "llm_optimized": synthesized_text != text,
                    "llm_model": optimization_model or None,
                    **(
                        {"audio_verification": verification}
                        if verification is not None
                        else {}
                    ),
                },
            )
            take_artifact_ids.append(take_artifact.id)
            self._record_tts_usage(
                session_id,
                segment_tts_settings,
                synthesized_text,
                len(audio),
                job_id=job_id,
                artifact_id=take_artifact.id,
            )
            with self.database.session() as session:
                segment = session.get(GenerationSegment, generation_segment_id)
                segment.status = "completed"
                if verification is not None and verification.get("status") != "passed":
                    segment.marked = True
                session.add(
                    AudioTake(
                        generation_segment_id=generation_segment_id,
                        artifact_id=take_artifact.id,
                        kind="tts",
                        status="completed",
                        settings_hash=take_artifact.settings_hash,
                        duration_ms=len(audio),
                        is_active=True,
                    )
                )
            silence_after = _default_silence_after_ms(
                record,
                settings,
                is_subtitle=source_artifact.role == "speech_blocks"
                or self._is_subtitle_generation_record(record),
            )
            assembly_inputs.append((sentence_path, len(audio), silence_after))
            progress(
                optimization_share + (index / len(records)) * synthesis_share,
                f"Generated segment {index} of {len(records)}",
            )
        if not assembly_inputs:
            raise RuntimeError("The speech service returned no audio.")

        destination = self._operation_dir(session_id, "generate-audio") / (
            "dubbing_audio.wav" if role == "dubbing_audio" else "audiobook_audio.wav"
        )
        fade_enabled = bool(
            settings.get("fade_enabled", settings.get("enable_fade", False))
        )
        fade_in_ms = (
            max(
                0,
                int(
                    settings.get("fade_in_ms", settings.get("fade_in_duration", 0)) or 0
                ),
            )
            if fade_enabled
            else 0
        )
        fade_out_ms = (
            max(
                0,
                int(
                    settings.get("fade_out_ms", settings.get("fade_out_duration", 0))
                    or 0
                ),
            )
            if fade_enabled
            else 0
        )
        sample_rate_hz, channels = preferred_pcm_format(
            assembly_inputs[0][0],
            cancel_event=cancel_event,
        )
        plan = build_audio_assembly_plan(
            [
                AudioAssemblyPart(
                    path=path,
                    expected_duration_ms=duration_ms,
                    silence_after_ms=(
                        max(0, int(silence_after_ms or 0))
                        if index < len(assembly_inputs) - 1
                        else 0
                    ),
                    fade_in_ms=fade_in_ms,
                    fade_out_ms=fade_out_ms,
                )
                for index, (path, duration_ms, silence_after_ms) in enumerate(
                    assembly_inputs
                )
            ],
            output_format="wav",
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        try:
            assembly_result = assemble_audio_plan(
                plan,
                destination,
                cancel_event=cancel_event,
            )
        except MediaProcessCancelled:
            return {}
        artifact = self.artifacts.register(
            destination,
            kind="audio",
            role=role,
            session_id=session_id,
            parent_ids=[source_artifact.id, *take_artifact_ids],
            settings=settings,
            metadata={
                "segment_count": len(records),
                "service": settings.get("service")
                or settings.get("tts_service")
                or "XTTS",
                "duration_ms": assembly_result.duration_ms,
                "assembly_backend": assembly_result.backend,
            },
        )
        progress(1.0, "Audio ready")
        return {
            "artifact_id": artifact.id,
            "path": artifact.relative_path,
            "segments": len(records),
            "generation_plan_revision_id": revision_id,
        }

    def generate_dubbing_audio(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.speech_blocks import generate_speech_blocks_file

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        settings = dict(payload.get("settings") or {})
        if source_path.suffix.lower() != ".srt":
            raise ValueError(
                "Dubbing audio requires a transcription, correction, or translation SRT artifact."
            )
        language = self._generation_language(session_id, source_artifact, settings)
        settings = {**settings, "language": language, "target_language": language}
        speaker_by_subtitle = self._subtitle_speaker_map(source_artifact, source_path)
        speaker_options = (
            {"speaker_by_subtitle": speaker_by_subtitle} if speaker_by_subtitle else {}
        )
        (
            min_chars,
            max_chars,
            merge_threshold,
            continuation_threshold,
            max_internal_gap,
        ) = _speech_block_settings(settings)
        blocks_path = Path(
            generate_speech_blocks_file(
                str(self._operation_dir(session_id, "speech-blocks")),
                str(source_path),
                target_language=language,
                min_chars=min_chars,
                max_chars=max_chars,
                merge_threshold=merge_threshold,
                continuation_threshold_ms=continuation_threshold,
                max_internal_gap_ms=max_internal_gap,
                **speaker_options,
            )
        )
        blocks_artifact = self.artifacts.register(
            blocks_path,
            kind="json",
            role="speech_blocks",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=settings,
        )
        return self._generate_audio(
            session_id,
            blocks_artifact,
            blocks_path,
            settings,
            progress,
            cancel_event,
            role="dubbing_audio",
            job_id=str(payload.get("_job_id") or "") or None,
        )

    def generate_audiobook_audio(self, payload, progress, cancel_event):
        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        settings = dict(payload.get("settings") or {})
        if source_artifact.role not in {"prepared_text", "tts_optimized"}:
            raise ValueError(
                "Audiobook generation requires a current Segment narration artifact or its reviewed speech-optimized revision."
            )
        return self._generate_audio(
            session_id,
            source_artifact,
            source_path,
            settings,
            progress,
            cancel_event,
            role="audiobook_audio",
            job_id=str(payload.get("_job_id") or "") or None,
        )

    def _run_reviewable_generation(
        self,
        payload: dict[str, Any],
        progress,
        cancel_event,
        *,
        resolved_snapshot: Any = None,
        settings_hash: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Create/resolve the segment plan and generate takes without assembly.

        The workflow card and the generation drawer must describe the same
        operation.  The former compatibility path generated a combined WAV in
        the workflow job, leaving no GenerationRun for the drawer to observe.
        This boundary deliberately stops after immutable per-segment takes;
        output assembly remains an explicit review action.
        """
        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        settings = dict(payload.get("settings") or {})
        language = self._generation_language(session_id, source_artifact, settings)
        settings = {**settings, "language": language, "target_language": language}
        progress(0.0, "Preparing generation segments")

        plan_revision_id: str | None = None
        if source_path.suffix.lower() == ".srt":
            plan_revision_id = self._materialize_subtitle_generation_plan(
                session_id,
                source_artifact,
                source_path,
                settings,
                language,
            )
        elif source_path.suffix.lower() == ".json":
            # Segment narration already creates a plan. Preserve any edits the
            # user made in the drawer. A separately reviewed optimization
            # artifact, however, is a new source and therefore a new plan.
            with self.database.session() as session:
                plan = session.scalar(
                    select(GenerationPlan).where(
                        GenerationPlan.session_id == session_id
                    )
                )
                if source_artifact.role == "prepared_text" and plan is not None:
                    plan_revision_id = plan.active_revision_id
            if not plan_revision_id:
                records = json.loads(source_path.read_text(encoding="utf-8-sig"))
                if not isinstance(records, list) or not records:
                    raise ValueError("No narration segments were found.")
                plan_revision_id, _ = self._store_generation_plan(
                    session_id,
                    records,
                    settings=settings,
                    source_revision_id=str(
                        (source_artifact.metadata_json or {}).get("revision_id") or ""
                    )
                    or None,
                    source_artifact_id=source_artifact.id,
                )
        else:
            raise ValueError(
                "Audio generation requires subtitle cues or segmented narration."
            )

        if not plan_revision_id:
            raise ValueError(
                "Create generation segments before starting audio generation."
            )

        snapshot = (
            deepcopy(resolved_snapshot) if isinstance(resolved_snapshot, dict) else {}
        )
        snapshot = _secret_free_tts_settings(snapshot)
        # The resolved sections are the immutable source of truth. Merge the
        # flattened stage values as compatibility aliases so direct Run Now
        # choices (service, model, voice, and language) cannot be lost.
        safe_settings = _secret_free_tts_settings(settings)
        snapshot["tts"] = {**dict(snapshot.get("tts") or {}), **safe_settings}
        snapshot["audio"] = {**dict(snapshot.get("audio") or {}), **safe_settings}
        snapshot["text"] = {
            **dict(snapshot.get("text") or {}),
            "llm_tts_optimization": bool(settings.get("llm_tts_optimization")),
            "apply_reviewed_pronunciations": settings.get(
                "apply_reviewed_pronunciations", True
            ),
            "use_existing_speech_plans": source_artifact.role == "tts_optimized",
        }
        snapshot["source_artifact_id"] = source_artifact.id
        frozen_hash = hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        with self.database.session() as session:
            sequence_number = (
                int(
                    session.scalar(
                        select(func.max(GenerationRun.sequence_number)).where(
                            GenerationRun.session_id == session_id
                        )
                    )
                    or 0
                )
                + 1
            )
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=plan_revision_id,
                job_id=job_id,
                sequence_number=sequence_number,
                operation="generate",
                status="queued",
                settings_snapshot_json=snapshot,
                settings_hash=frozen_hash or settings_hash,
            )
            session.add(run)
            session.flush()
            run_id = run.id

        progress(0.03, "Generation segments ready")
        try:
            return self.run_generation(
                {
                    "generation_run_id": run_id,
                    "segment_ids": [],
                    "operation": "generate",
                },
                lambda value, detail=None: progress(
                    0.03 + max(0.0, min(1.0, float(value))) * 0.97, detail
                ),
                cancel_event,
            )
        except Exception:
            with self.database.session() as session:
                failed = session.get(GenerationRun, run_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.updated_at = utcnow()
            raise

    def run_generation(self, payload, progress, cancel_event):
        """Generate immutable per-segment takes with safe pause and resume boundaries."""
        from pydub import AudioSegment

        from pandrator.logic import rvc_handler

        run_id = str(payload.get("generation_run_id") or "")
        selected_ids = {
            str(value) for value in (payload.get("segment_ids") or []) if str(value)
        }
        operation = str(payload.get("operation") or "generate")
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            run.status = "running"
            run.updated_at = utcnow()
            settings_snapshot = dict(run.settings_snapshot_json or {})
            session_id = run.session_id
            plan_revision_id = run.plan_revision_id
            run_sequence_number = run.sequence_number
            job_id = run.job_id or str(payload.get("_job_id") or "") or None

        statement = (
            select(GenerationSegment)
            .where(
                GenerationSegment.plan_revision_id == plan_revision_id,
                GenerationSegment.removed.is_(False),
            )
            .order_by(GenerationSegment.ordinal)
        )
        if selected_ids:
            statement = statement.where(GenerationSegment.id.in_(selected_ids))
        with self.database.session() as session:
            selected_segments = list(session.scalars(statement).all())
            segment_ids = [item.id for item in selected_segments]
            source_texts = [item.text for item in selected_segments]
            segment_seeds = {
                item.id: {
                    "text": item.text,
                    "speaker": item.speaker,
                    "language": self._usable_language(item.language),
                    "voice": str(item.voice or "").strip() or None,
                    "status": item.status,
                }
                for item in selected_segments
            }
        if not segment_ids:
            with self.database.session() as session:
                run = session.get(GenerationRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.updated_at = utcnow()
            raise ValueError("No generation segments match this request.")

        from .workspace import adapt_runtime_settings, mark_output_assemblies_stale

        tts_settings = {
            **adapt_runtime_settings("tts", dict(settings_snapshot.get("tts") or {})),
            **adapt_runtime_settings(
                "audio", dict(settings_snapshot.get("audio") or {})
            ),
        }
        selected_segment_override = dict(
            settings_snapshot.get("selected_segment_override") or {}
        )
        selected_tts_override = dict(selected_segment_override.get("tts") or {})
        selected_rvc_override = dict(selected_segment_override.get("rvc") or {})
        tts_settings = hydrate_tts_settings(
            self.database,
            self.paths,
            tts_settings,
            manager_bridge=self.manager_bridge,
        )
        selected_tts_runtime: dict[str, Any] | None = None
        if selected_tts_override:
            # Alternate settings are a small UI payload, not a complete TTS
            # configuration.  Adapt and hydrate them against the effective
            # run settings so a catalogue/custom provider retains its saved
            # endpoint and credential metadata.  Keep this copy run-local:
            # persistent/session settings must remain unchanged.
            selected_tts_runtime = _apply_selected_segment_tts_override(
                tts_settings,
                selected_tts_override,
            )
            explicit_endpoint_keys = {
                "audio_cpp_base_url",
                "openai_audio_endpoint",
                "xtts_base_url",
                "voxcpm_base_url",
                "fishs2_base_url",
                "voxtral_base_url",
                "kokoro_base_url",
                "silero_base_url",
                "chatterbox_base_url",
                "kobold_qwen_base_url",
                "magpie_base_url",
            }
            explicit_endpoints = {
                key: deepcopy(selected_tts_override[key])
                for key in explicit_endpoint_keys
                if key in selected_tts_override
            }
            selected_tts_runtime = adapt_runtime_settings(
                "tts",
                selected_tts_runtime,
            )
            # ``adapt_runtime_settings`` fills service-derived endpoint aliases
            # for normal catalogue choices.  An alternate payload that names
            # an endpoint explicitly is authoritative, however.
            selected_tts_runtime.update(explicit_endpoints)
            selected_tts_runtime = hydrate_tts_settings(
                self.database,
                self.paths,
                selected_tts_runtime,
                manager_bridge=self.manager_bridge,
            )
            selected_tts_runtime.update(explicit_endpoints)

        def _tts_settings_for_segment(
            segment_settings: dict[str, Any],
            *,
            language: str,
            voice: str | None,
        ) -> dict[str, Any]:
            if selected_tts_runtime is None:
                return _apply_selected_segment_tts_override(
                    segment_settings,
                    selected_tts_override,
                )
            resolved = deepcopy(selected_tts_runtime)
            if not str(
                selected_tts_override.get("language")
                or selected_tts_override.get("target_language")
                or ""
            ).strip():
                resolved = _apply_segment_tts_overrides(
                    resolved,
                    language=language,
                )
            if not str(
                selected_tts_override.get("voice")
                or selected_tts_override.get("speaker")
                or ""
            ).strip():
                resolved = _apply_segment_tts_overrides(
                    resolved,
                    voice=voice,
                )
            return resolved

        text_settings = adapt_runtime_settings(
            "text", dict(settings_snapshot.get("text") or {})
        )
        optimization_model = ""
        optimized_by_id: dict[str, str] = {}
        optimization_share = (
            0.2
            if operation != "rvc" and bool(text_settings.get("llm_tts_optimization"))
            else 0.0
        )
        if operation != "rvc":
            try:
                alternate_pronunciation_settings = None
                alternate_pronunciation_language = None
                alternate_pronunciation_voice_language = None
                if selected_tts_runtime is not None:
                    # Only copy provider identity and the explicit language
                    # choice into optimization.  The hydrated runtime may
                    # contain API keys; it must remain confined to synthesis.
                    alternate_pronunciation_settings = {
                        key: deepcopy(selected_tts_runtime[key])
                        for key in (
                            "service",
                            "tts_service",
                            "backend",
                            "openai_audio_endpoint",
                        )
                        if key in selected_tts_runtime
                    }
                    alternate_pronunciation_language = (
                        str(
                            selected_tts_override.get("language")
                            or selected_tts_override.get("target_language")
                            or ""
                        ).strip()
                        or None
                    )
                    alternate_pronunciation_voice_language = (
                        str(selected_tts_override.get("voice_language") or "").strip()
                        or None
                    )
                optimized, optimization_model = self._optimize_generation_texts(
                    session_id,
                    segment_ids,
                    source_texts,
                    {**text_settings, **tts_settings},
                    cancel_event,
                    lambda value, detail=None: progress(
                        float(value) * optimization_share, detail
                    ),
                    job_id=job_id,
                    generation_run_id=run_id,
                    pronunciation_settings=alternate_pronunciation_settings,
                    pronunciation_language=alternate_pronunciation_language,
                    pronunciation_voice_language=alternate_pronunciation_voice_language,
                )
                optimized_by_id = dict(zip(segment_ids, optimized, strict=True))
            except Exception:
                with self.database.session() as session:
                    run = session.get(GenerationRun, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.updated_at = utcnow()
                raise
        rvc_settings = adapt_runtime_settings(
            "rvc", dict(settings_snapshot.get("rvc") or {})
        )
        if selected_rvc_override:
            rvc_settings.update(adapt_runtime_settings("rvc", selected_rvc_override))
        rvc_source_sequence = None
        if operation == "rvc":
            source_run_id = str(rvc_settings.get("source_run_id") or "").strip()
            if source_run_id:
                with self.database.session() as session:
                    source_run = session.get(GenerationRun, source_run_id)
                    if (
                        source_run is None
                        or source_run.session_id != session_id
                        or source_run.plan_revision_id != plan_revision_id
                    ):
                        raise ValueError(
                            "The selected source generation run is unavailable."
                        )
                    rvc_source_sequence = source_run.sequence_number
        generated = 0
        skipped = 0
        verified_qwen_voices: set[str] = set()
        batch_results = None
        batch_contexts: dict[str, dict[str, Any]] = {}
        # The selected alternate owns the provider endpoint for this run.  In
        # particular, a first-class service's explicit base URL must reach the
        # legacy synthesis boundary instead of the source provider's URL.
        tts_urls = self._tts_urls(selected_tts_runtime or tts_settings)
        if operation != "rvc":
            # The selected-only setting set may switch provider.  Do not reuse
            # the source provider's streaming/batching capabilities for it.
            effective_batch_size = (
                1
                if selected_tts_override
                else self._negotiated_tts_batch_size(tts_settings, tts_urls)
            )
            if effective_batch_size > 1:
                batch_items: list[tuple[str, str, dict[str, Any]]] = []
                for segment_id in segment_ids:
                    seed = segment_seeds[segment_id]
                    if operation == "resume" and seed["status"] == "completed":
                        continue
                    segment_tts_settings = _apply_segment_tts_overrides(
                        tts_settings,
                        language=seed["language"],
                        voice=seed["voice"],
                    )
                    segment_tts_settings = _tts_settings_for_segment(
                        segment_tts_settings,
                        language=seed["language"],
                        voice=seed["voice"],
                    )
                    segment_tts_settings = self.prepare_audio_cpp_voice_reference(
                        segment_tts_settings
                    )
                    self._ensure_qwen_cloned_voice(
                        segment_tts_settings,
                        base_url=tts_urls["kobold_qwen_base_url"],
                        verified=verified_qwen_voices,
                        cancel_event=cancel_event,
                    )
                    synthesized_text = optimized_by_id.get(
                        segment_id,
                        str(seed["text"]),
                    )
                    batch_contexts[segment_id] = {
                        "text": str(seed["text"]),
                        "synthesized_text": synthesized_text,
                        "settings": segment_tts_settings,
                    }
                    batch_items.append(
                        (segment_id, synthesized_text, segment_tts_settings)
                    )
                if batch_items:
                    batch_results = self._start_streaming_tts_batch(
                        batch_items,
                        batch_size=effective_batch_size,
                        tts_urls=tts_urls,
                        cancel_event=cancel_event,
                    )
                    logger.info(
                        "Using streaming %s-item TTS batches for %d generation segments.",
                        effective_batch_size,
                        len(batch_items),
                    )
        for index, segment_id in enumerate(segment_ids):
            with self.database.session() as session:
                run = session.get(GenerationRun, run_id)
                segment = session.get(GenerationSegment, segment_id)
                if run.cancel_requested or cancel_event.is_set():
                    run.status = "canceled"
                    run.updated_at = utcnow()
                    return {
                        "generation_run_id": run_id,
                        "status": "canceled",
                        "generated": generated,
                    }
                if run.pause_requested:
                    run.status = "paused"
                    run.updated_at = utcnow()
                    return {
                        "generation_run_id": run_id,
                        "status": "paused",
                        "generated": generated,
                    }
                if operation == "resume" and segment.status == "completed":
                    skipped += 1
                    progress(
                        optimization_share
                        + ((index + 1) / len(segment_ids)) * (1.0 - optimization_share),
                        f"Kept completed segment {index + 1} of {len(segment_ids)}",
                    )
                    continue
                segment.status = "running"
                segment.updated_at = utcnow()
                text = segment.text
                segment_speaker = segment.speaker
                segment_language = self._usable_language(segment.language)
                segment_voice = str(segment.voice or "").strip() or None
            progress(
                optimization_share
                + (index / len(segment_ids)) * (1.0 - optimization_share),
                f"Generating segment {index + 1} of {len(segment_ids)}",
            )
            take_path: Path | None = None
            take_committed = False
            try:
                synthesized_text = text
                if operation == "rvc":
                    with self.database.session() as session:
                        source_take = session.scalar(
                            select(AudioTake)
                            .join(
                                GenerationRun,
                                AudioTake.generation_run_id == GenerationRun.id,
                            )
                            .where(
                                AudioTake.generation_segment_id == segment_id,
                                AudioTake.status.in_(("completed", "stale")),
                                GenerationRun.session_id == session_id,
                                GenerationRun.plan_revision_id == plan_revision_id,
                                GenerationRun.sequence_number
                                <= (
                                    rvc_source_sequence
                                    if rvc_source_sequence is not None
                                    else run_sequence_number - 1
                                ),
                            )
                            .order_by(
                                GenerationRun.sequence_number.desc(),
                                AudioTake.created_at.desc(),
                            )
                        )
                        if source_take is None:
                            source_take = session.scalar(
                                select(AudioTake)
                                .where(
                                    AudioTake.generation_segment_id == segment_id,
                                    AudioTake.generation_run_id.is_(None),
                                    AudioTake.is_active.is_(True),
                                    AudioTake.status.in_(("completed", "stale")),
                                )
                                .order_by(AudioTake.created_at.desc())
                            )
                        if source_take is None or source_take.artifact_id is None:
                            raise ValueError(
                                "The selected segment has no active audio take for RVC."
                            )
                        source_artifact = session.get(Artifact, source_take.artifact_id)
                        source_take_id = source_take.id
                    source_path = self.paths.managed_path(source_artifact.relative_path)
                    source_audio = AudioSegment.from_file(source_path)
                    audio = rvc_handler.process_with_rvc(source_audio, rvc_settings)
                    take_kind = "rvc"
                    parent_take_id = source_take_id
                    take_settings = rvc_settings
                else:
                    batch_context = batch_contexts.get(segment_id)
                    if batch_context is not None:
                        text = str(batch_context["text"])
                        synthesized_text = str(batch_context["synthesized_text"])
                        segment_tts_settings = dict(batch_context["settings"])
                    else:
                        synthesized_text = optimized_by_id.get(segment_id, text)
                        segment_tts_settings = _apply_segment_tts_overrides(
                            tts_settings,
                            language=segment_language,
                            voice=segment_voice,
                        )
                        segment_tts_settings = _tts_settings_for_segment(
                            segment_tts_settings,
                            language=segment_language,
                            voice=segment_voice,
                        )
                        segment_tts_settings = self.prepare_audio_cpp_voice_reference(
                            segment_tts_settings
                        )
                        self._ensure_qwen_cloned_voice(
                            segment_tts_settings,
                            base_url=tts_urls["kobold_qwen_base_url"],
                            verified=verified_qwen_voices,
                            cancel_event=cancel_event,
                        )

                    def synthesize_one(
                        *,
                        text_to_synthesize: str = synthesized_text,
                        settings_for_segment: dict[str, Any] = segment_tts_settings,
                        segment_index: int = index,
                    ):
                        return self.tts_providers.synthesize(
                            text_to_synthesize,
                            settings_for_segment,
                            max_attempts=int(
                                settings_for_segment.get("max_attempts") or 5
                            ),
                            cancel_event=cancel_event,
                            retry_callback=lambda attempt, total, delay: progress(
                                optimization_share
                                + (segment_index / len(segment_ids))
                                * (1.0 - optimization_share),
                                f"Retrying segment {segment_index + 1} ({attempt}/{total}) in {delay:.1f}s",
                            ),
                            recovery_callback=lambda cycle, total, timeout: progress(
                                optimization_share
                                + (segment_index / len(segment_ids))
                                * (1.0 - optimization_share),
                                f"Waiting for Qwen3 TTS before segment {segment_index + 1} ({cycle}/{total}, up to {timeout:.0f}s)",
                            ),
                            **tts_urls,
                        )

                    if batch_results is not None and batch_context is not None:
                        try:
                            batch_result = next(batch_results)
                        except StopIteration as error:
                            raise RuntimeError(
                                "The streaming TTS batch ended before every segment completed."
                            ) from error
                        if batch_result.id != segment_id:
                            raise RuntimeError(
                                "The streaming TTS batch returned segments out of order."
                            )
                        if batch_result.error is not None:
                            if not batch_result.error.retryable:
                                raise batch_result.error
                            logger.warning(
                                "Streaming TTS batch failed for segment %s; retrying it through the ordinary synthesis path.",
                                segment_id,
                            )
                            audio = synthesize_one()
                        else:
                            audio = batch_result.audio
                    else:
                        audio = synthesize_one()
                    if audio is None:
                        raise RuntimeError("The speech service returned no audio.")
                    take_kind = "tts"
                    parent_take_id = None
                    take_settings = segment_tts_settings
                    if bool(selected_rvc_override.get("enabled")):
                        if not str(
                            rvc_settings.get("model")
                            or rvc_settings.get("rvc_model")
                            or ""
                        ).strip():
                            raise ValueError(
                                "Choose an RVC model before generating an alternate RVC take."
                            )
                        audio = rvc_handler.process_with_rvc(audio, rvc_settings)
                        take_kind = "tts_rvc"
                        take_settings = {**segment_tts_settings, "rvc": rvc_settings}
                verification = self._verification_metadata(
                    audio,
                    synthesized_text,
                    {**tts_settings, **take_settings},
                )
                take_dir = (
                    self._session_dir(session_id)
                    / "generation"
                    / plan_revision_id
                    / segment_id
                )
                take_dir.mkdir(parents=True, exist_ok=True)
                take_path = take_dir / f"{take_kind}-{new_id()}.wav"
                exported = audio.export(take_path, format="wav")
                exported.close()
                stored_take_settings = _secret_free_tts_settings(take_settings)
                prepared_artifact = self.artifacts.prepare_registration(
                    take_path,
                    settings=stored_take_settings,
                )
                with self.database.session() as session:
                    segment = session.get(GenerationSegment, segment_id)
                    artifact = self.artifacts.register_in_session(
                        session,
                        take_path,
                        kind="audio",
                        role="generation_take",
                        session_id=session_id,
                        parent_ids=([source_artifact.id] if operation == "rvc" else []),
                        settings=stored_take_settings,
                        metadata={
                            "generation_segment_id": segment_id,
                            "generation_run_id": run_id,
                            "kind": take_kind,
                            "speaker": segment_speaker,
                            "source_text": text,
                            "synthesized_text": synthesized_text,
                            "llm_optimized": (
                                operation != "rvc" and synthesized_text != text
                            ),
                            "llm_model": optimization_model or None,
                            **(
                                {"audio_verification": verification}
                                if verification is not None
                                else {}
                            ),
                        },
                        _prepared=prepared_artifact,
                    )
                    if operation != "rvc":
                        usage_event = self._tts_usage_event(
                            session_id,
                            take_settings,
                            synthesized_text,
                            len(audio),
                            job_id=job_id,
                            artifact_id=artifact.id,
                            generation_run_id=run_id,
                        )
                        if usage_event is not None:
                            session.add(usage_event)
                    deactivate = update(AudioTake).where(
                        AudioTake.generation_segment_id == segment_id,
                        AudioTake.is_active.is_(True),
                    )
                    session.execute(
                        deactivate.values(
                            is_active=False,
                            revision=AudioTake.revision + 1,
                        ).execution_options(synchronize_session=False)
                    )
                    session.add(
                        AudioTake(
                            generation_segment_id=segment_id,
                            generation_run_id=run_id,
                            artifact_id=artifact.id,
                            parent_take_id=parent_take_id,
                            kind=take_kind,
                            status="completed",
                            settings_hash=artifact.settings_hash,
                            duration_ms=len(audio),
                            is_active=True,
                        )
                    )
                    segment.status = "completed"
                    if (
                        verification is not None
                        and verification.get("status") != "passed"
                    ):
                        segment.marked = True
                    segment.updated_at = utcnow()
                    mark_output_assemblies_stale(
                        session, session_id, generation_run_id=run_id
                    )
                take_committed = True
                generated += 1
                progress(
                    optimization_share
                    + ((index + 1) / len(segment_ids)) * (1.0 - optimization_share),
                    f"Generated segment {index + 1} of {len(segment_ids)}",
                )
            except Exception:
                if take_path is not None and not take_committed:
                    try:
                        take_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(
                            "Could not remove uncommitted generation take %s",
                            take_path,
                            exc_info=True,
                        )
                with self.database.session() as session:
                    segment = session.get(GenerationSegment, segment_id)
                    if segment is not None:
                        segment.status = "failed"
                        segment.updated_at = utcnow()
                    run = session.get(GenerationRun, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.updated_at = utcnow()
                raise

        verification_warning_count = self._finalize_run_audio_verification(run_id)
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            incomplete = int(
                session.scalar(
                    select(func.count())
                    .select_from(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == plan_revision_id,
                        GenerationSegment.removed.is_(False),
                        GenerationSegment.status != "completed",
                    )
                )
                or 0
            )
            final_status = "partial" if incomplete else "completed"
            run.status = final_status
            run.updated_at = utcnow()
        progress(
            1.0,
            "Generation run complete"
            if final_status == "completed"
            else f"Generation saved; {incomplete} segment(s) remain",
        )
        auto_resume_source_id = str(
            payload.get("auto_resume_source_generation_run_id") or ""
        )
        resumed_job_id = (
            self._resume_generation_after_regeneration(
                run_id,
                auto_resume_source_id,
            )
            if auto_resume_source_id
            else None
        )
        result = {
            "generation_run_id": run_id,
            "status": final_status,
            "generated": generated,
            "skipped": skipped,
            "remaining": incomplete,
            "verification_warnings": verification_warning_count,
        }
        if auto_resume_source_id:
            result["resumed_source_job_id"] = resumed_job_id
        return result

    def assemble_generation_output(self, payload, progress, cancel_event):
        """Assemble the current selected takes in plan order into an immutable artifact."""
        import tempfile

        from pandrator.logic.dubbing.audio_sync import align_audio_blocks
        from pandrator.logic.dubbing.models import AudioAlignmentBlock

        from .audio_assembly import (
            AudioAssemblyPart,
            assemble_audio_plan,
            build_audio_assembly_plan,
            preferred_pcm_format,
            resolve_assembly_backend,
        )
        from .media_process import MediaProcessCancelled, probe_audio_stream

        assembly_id = str(payload.get("output_assembly_id") or "")
        destination: Path | None = None
        output_registered = False
        with self.database.session() as session:
            assembly = session.get(OutputAssembly, assembly_id)
            if assembly is None:
                raise KeyError(assembly_id)
            assembly.status = "running"
            assembly.error_message = None
            assembly.updated_at = utcnow()
            session_id = assembly.session_id
            settings_container = dict(assembly.settings_json or {})
            plan_revision_id = str(settings_container.get("plan_revision_id") or "")
            resolved = (
                settings_container.get("resolved")
                if isinstance(settings_container.get("resolved"), dict)
                else {}
            )
            audio_settings = dict(resolved.get("audio") or {})
            output_settings = dict(resolved.get("output") or {})
            selected_run = (
                session.get(GenerationRun, assembly.generation_run_id)
                if assembly.generation_run_id
                else None
            )
            selected_run_sequence = (
                selected_run.sequence_number if selected_run else None
            )
            plan_revision = session.get(
                GenerationPlanRevision,
                plan_revision_id,
            )
            source_revision_id = (
                plan_revision.source_revision_id if plan_revision is not None else None
            )

        try:
            with self.database.session() as session:
                source_timings = []
                if source_revision_id:
                    source_timings = [
                        (item.id, item.ordinal, item.start_ms, item.end_ms)
                        for item in session.scalars(
                            select(Segment)
                            .where(Segment.revision_id == source_revision_id)
                            .order_by(Segment.ordinal)
                        ).all()
                    ]
                if selected_run_sequence is not None:
                    ranked_takes = (
                        select(
                            AudioTake.id.label("take_id"),
                            AudioTake.generation_segment_id.label("segment_id"),
                            func.row_number()
                            .over(
                                partition_by=AudioTake.generation_segment_id,
                                order_by=(
                                    GenerationRun.sequence_number.desc(),
                                    AudioTake.created_at.desc(),
                                    AudioTake.id.desc(),
                                ),
                            )
                            .label("take_rank"),
                        )
                        .join(
                            GenerationRun,
                            AudioTake.generation_run_id == GenerationRun.id,
                        )
                        .where(
                            AudioTake.status.in_(("completed", "stale")),
                            GenerationRun.session_id == session_id,
                            GenerationRun.plan_revision_id == plan_revision_id,
                            GenerationRun.sequence_number <= selected_run_sequence,
                        )
                        .subquery()
                    )
                else:
                    ranked_takes = (
                        select(
                            AudioTake.id.label("take_id"),
                            AudioTake.generation_segment_id.label("segment_id"),
                            func.row_number()
                            .over(
                                partition_by=AudioTake.generation_segment_id,
                                order_by=(
                                    AudioTake.created_at.desc(),
                                    AudioTake.id.desc(),
                                ),
                            )
                            .label("take_rank"),
                        )
                        .join(
                            GenerationSegment,
                            AudioTake.generation_segment_id == GenerationSegment.id,
                        )
                        .where(
                            AudioTake.is_active.is_(True),
                            AudioTake.status == "completed",
                            GenerationSegment.plan_revision_id == plan_revision_id,
                            GenerationSegment.removed.is_(False),
                        )
                        .subquery()
                    )
                selected_rows = list(
                    session.execute(
                        select(GenerationSegment, AudioTake, Artifact)
                        .outerjoin(
                            ranked_takes,
                            and_(
                                ranked_takes.c.segment_id == GenerationSegment.id,
                                ranked_takes.c.take_rank == 1,
                            ),
                        )
                        .outerjoin(
                            AudioTake,
                            AudioTake.id == ranked_takes.c.take_id,
                        )
                        .outerjoin(
                            Artifact,
                            Artifact.id == AudioTake.artifact_id,
                        )
                        .where(
                            GenerationSegment.plan_revision_id == plan_revision_id,
                            GenerationSegment.removed.is_(False),
                        )
                        .order_by(GenerationSegment.ordinal)
                    ).all()
                )
                selected: list[tuple[GenerationSegment, AudioTake, Artifact]] = []
                for segment, take, artifact in selected_rows:
                    allowed_statuses = (
                        {"completed", "stale"}
                        if selected_run is not None
                        else {"completed"}
                    )
                    if (
                        take is None
                        or take.status not in allowed_statuses
                        or not take.artifact_id
                    ):
                        if selected_run is None:
                            raise ValueError(
                                f"Segment {segment.ordinal + 1} has no current completed audio take."
                            )
                        raise ValueError(
                            f"Segment {segment.ordinal + 1} has no available audio take in Run {selected_run.sequence_number}."
                        )
                    if artifact is None or artifact.state != "current":
                        raise ValueError(
                            f"Segment {segment.ordinal + 1} references an unavailable audio artifact."
                        )
                    selected.append((segment, take, artifact))
            if not selected:
                raise ValueError(
                    "No active generation segments are available for assembly."
                )

            loaded: list[tuple[GenerationSegment, AudioTake, Artifact, Path, int]] = []
            manifest: list[dict[str, Any]] = []
            chapter_markers: list[tuple[float, str]] = []
            parent_ids: list[str] = []
            for index, (segment, take, artifact) in enumerate(selected):
                if cancel_event.is_set():
                    with self.database.session() as session:
                        assembly = session.get(OutputAssembly, assembly_id)
                        if assembly is not None:
                            assembly.status = "canceled"
                            assembly.error_message = None
                            assembly.updated_at = utcnow()
                    return {}
                progress(
                    0.2 * (index / len(selected)),
                    f"Validating audio segment {index + 1} of {len(selected)}",
                )
                path = self.paths.managed_path(artifact.relative_path)
                if not path.is_file():
                    raise ValueError(
                        f"Audio take file is missing for segment {segment.ordinal + 1}."
                    )
                duration_ms = int(
                    take.duration_ms
                    or (artifact.metadata_json or {}).get("duration_ms")
                    or 0
                )
                if duration_ms <= 0:
                    duration_ms = probe_audio_stream(
                        path, cancel_event=cancel_event
                    ).duration_ms
                loaded.append((segment, take, artifact, path, duration_ms))
                parent_ids.append(artifact.id)
            progress(0.2, f"Validated {len(loaded)} audio segments")

            source_timing_by_ref: dict[str, tuple[int, int, int]] = {}
            for source_id, ordinal, start_ms, end_ms in source_timings:
                if start_ms is None or end_ms is None:
                    continue
                timing = (int(start_ms), int(end_ms), int(ordinal) + 1)
                source_timing_by_ref[str(source_id)] = timing
                source_timing_by_ref[str(int(ordinal) + 1)] = timing
            subtitle_timed = bool(source_timing_by_ref) and any(
                segment.node_kind == "subtitle_cue" for segment, *_rest in loaded
            )
            alignment_diagnostics: dict[str, Any] = {
                "mode": "sequential",
                "block_count": len(loaded),
                "speed_adjusted_block_count": 0,
            }

            session_record = self._session_record(session_id)
            output_format = str(output_settings.get("format") or "wav").lower()
            if session_record.workflow_kind != "audiobook" and output_format == "m4b":
                output_format = "wav"
            bitrate = str(output_settings.get("bitrate") or "192k")
            assemblies_dir = self._session_dir(session_id) / "assemblies"
            assemblies_dir.mkdir(parents=True, exist_ok=True)
            destination = assemblies_dir / f"assembly-{assembly_id}.{output_format}"
            backend = resolve_assembly_backend()
            fade_enabled = bool(
                audio_settings.get(
                    "fade_enabled", audio_settings.get("enable_fade", False)
                )
            )
            fade_in_ms = (
                max(
                    0,
                    int(
                        audio_settings.get(
                            "fade_in_ms",
                            audio_settings.get("fade_in_duration", 0),
                        )
                        or 0
                    ),
                )
                if fade_enabled
                else 0
            )
            fade_out_ms = (
                max(
                    0,
                    int(
                        audio_settings.get(
                            "fade_out_ms",
                            audio_settings.get("fade_out_duration", 0),
                        )
                        or 0
                    ),
                )
                if fade_enabled
                else 0
            )
            sample_rate_hz, channels = preferred_pcm_format(
                loaded[0][3],
                cancel_event=cancel_event,
            )

            if subtitle_timed:
                alignment_blocks: list[AudioAlignmentBlock] = []
                previous_alignment_group: str | None = None
                with tempfile.TemporaryDirectory(
                    prefix=f".assembly-{assembly_id}-",
                    dir=assemblies_dir,
                ) as temporary:
                    temporary_path = Path(temporary)
                    for index, (
                        segment,
                        take,
                        artifact,
                        source_path,
                        duration_ms,
                    ) in enumerate(loaded):
                        timings = sorted(
                            {
                                source_timing_by_ref[str(reference)]
                                for reference in segment.source_segment_ids_json
                                if str(reference) in source_timing_by_ref
                            },
                            key=lambda value: (value[0], value[1], value[2]),
                        )
                        if not timings:
                            raise ValueError(
                                f"Subtitle generation segment {segment.ordinal + 1} has no source timing references. Regenerate its speech-block plan."
                            )
                        input_path = temporary_path / f"segment-{index + 1:06d}.wav"
                        segment_plan = build_audio_assembly_plan(
                            [
                                AudioAssemblyPart(
                                    path=source_path,
                                    expected_duration_ms=duration_ms,
                                    fade_in_ms=fade_in_ms,
                                    fade_out_ms=fade_out_ms,
                                )
                            ],
                            output_format="wav",
                            sample_rate_hz=sample_rate_hz,
                            channels=channels,
                        )
                        segment_result = assemble_audio_plan(
                            segment_plan,
                            input_path,
                            backend=backend,
                            work_dir=temporary_path,
                            cancel_event=cancel_event,
                        )
                        block = AudioAlignmentBlock(
                            number=str(index + 1).zfill(4),
                            text=segment.text,
                            start_ms=timings[0][0],
                            end_ms=timings[-1][1],
                            audio_files=[input_path],
                            subtitles=[value[2] for value in timings],
                        )
                        alignment_group = (
                            str(segment.alignment_group or "").strip() or None
                        )
                        same_explicit_group = bool(
                            alignment_blocks
                            and alignment_group
                            and alignment_group == previous_alignment_group
                        )
                        legacy_shared_boundary = bool(
                            alignment_blocks
                            and not alignment_group
                            and not previous_alignment_group
                            and alignment_blocks[-1].subtitles[-1:]
                            == block.subtitles[:1]
                        )
                        if same_explicit_group or legacy_shared_boundary:
                            previous = alignment_blocks[-1]
                            alignment_blocks[-1] = AudioAlignmentBlock(
                                number=f"{previous.number}-{block.number}",
                                text=f"{previous.text} {block.text}".strip(),
                                start_ms=previous.start_ms,
                                end_ms=max(previous.end_ms, block.end_ms),
                                audio_files=[*previous.audio_files, *block.audio_files],
                                subtitles=sorted(
                                    set([*previous.subtitles, *block.subtitles])
                                ),
                            )
                        else:
                            alignment_blocks.append(block)
                        previous_alignment_group = alignment_group
                        manifest.append(
                            {
                                "segment_id": segment.id,
                                "segment_revision": segment.revision,
                                "node_kind": segment.node_kind,
                                "speaker": segment.speaker,
                                "alignment_group": alignment_group,
                                "take_id": take.id,
                                "take_revision": take.revision,
                                "artifact_id": artifact.id,
                                "kind": take.kind,
                                "duration_ms": segment_result.part_duration_ms[0],
                                "silence_after_ms": 0,
                                "target_start_ms": timings[0][0],
                                "target_end_ms": timings[-1][1],
                                "source_subtitles": [value[2] for value in timings],
                            }
                        )
                        progress(
                            0.2 + 0.25 * ((index + 1) / len(loaded)),
                            f"Prepared timing block {index + 1} of {len(loaded)}",
                        )
                    raw_speed = float(
                        audio_settings.get("synchronization_speed") or 1.0
                    )
                    speed_up_percent = int(
                        round(raw_speed * 100 if raw_speed <= 10 else raw_speed)
                    )
                    logger.info(
                        "Assembling %s with subtitle timing: blocks=%d max_speed=%.3fx max_delay=%dms sentence_gap=%dms",
                        assembly_id,
                        len(alignment_blocks),
                        max(1.0, speed_up_percent / 100.0),
                        max(
                            0,
                            int(audio_settings.get("synchronization_delay_ms") or 0),
                        ),
                        max(
                            0,
                            int(
                                audio_settings.get("synchronization_sentence_gap_ms")
                                or 100
                            ),
                        ),
                    )
                    progress(
                        0.48,
                        f"Synchronizing {len(alignment_blocks)} speech blocks",
                    )
                    aligned_path = Path(
                        align_audio_blocks(
                            alignment_blocks,
                            temporary_path,
                            delay_start_ms=max(
                                0,
                                int(
                                    audio_settings.get("synchronization_delay_ms") or 0
                                ),
                            ),
                            speed_up_percent=max(100, speed_up_percent),
                            sentence_gap_ms=max(
                                0,
                                int(
                                    audio_settings.get(
                                        "synchronization_sentence_gap_ms"
                                    )
                                    or 100
                                ),
                            ),
                            output_path=temporary_path / "aligned.wav",
                            diagnostics=alignment_diagnostics,
                            backend=backend,
                            cancel_event=cancel_event,
                        )
                    )
                    aligned_duration_ms = probe_audio_stream(
                        aligned_path,
                        cancel_event=cancel_event,
                    ).duration_ms
                    output_plan = build_audio_assembly_plan(
                        [
                            AudioAssemblyPart(
                                path=aligned_path,
                                expected_duration_ms=aligned_duration_ms,
                            )
                        ],
                        output_format=output_format,
                        bitrate=bitrate,
                        sample_rate_hz=sample_rate_hz,
                        channels=channels,
                    )
                    assembly_result = assemble_audio_plan(
                        output_plan,
                        destination,
                        backend=backend,
                        work_dir=temporary_path,
                        cancel_event=cancel_event,
                        progress=lambda fraction, detail: progress(
                            0.62 + 0.2 * fraction,
                            detail,
                        ),
                    )
                progress(0.82, "Subtitle-timed audio synchronized")
                logger.info(
                    "Assembly %s synchronization applied speed-up to %d/%d blocks (max effective %.3fx, final drift %dms)",
                    assembly_id,
                    int(alignment_diagnostics.get("speed_adjusted_block_count") or 0),
                    int(alignment_diagnostics.get("block_count") or 0),
                    float(
                        alignment_diagnostics.get("max_effective_speed_factor") or 1.0
                    ),
                    int(alignment_diagnostics.get("final_drift_ms") or 0),
                )
            else:
                planned_parts: list[AudioAssemblyPart] = []
                planned_chapters: list[tuple[int, str]] = []
                for index, (
                    segment,
                    _take,
                    _artifact,
                    source_path,
                    duration_ms,
                ) in enumerate(loaded):
                    silence_after_ms = (
                        max(0, int(segment.silence_after_ms or 0))
                        if index < len(loaded) - 1
                        else 0
                    )
                    planned_parts.append(
                        AudioAssemblyPart(
                            path=source_path,
                            expected_duration_ms=duration_ms,
                            silence_after_ms=silence_after_ms,
                            fade_in_ms=fade_in_ms,
                            fade_out_ms=fade_out_ms,
                            label=segment.id,
                        )
                    )
                    if segment.node_kind == "chapter_marker":
                        planned_chapters.append((index, segment.text))
                output_plan = build_audio_assembly_plan(
                    planned_parts,
                    output_format=output_format,
                    bitrate=bitrate,
                    sample_rate_hz=sample_rate_hz,
                    channels=channels,
                    chapters=planned_chapters,
                )
                assembly_result = assemble_audio_plan(
                    output_plan,
                    destination,
                    backend=backend,
                    work_dir=assemblies_dir,
                    cancel_event=cancel_event,
                    progress=lambda fraction, detail: progress(
                        0.2 + 0.62 * fraction,
                        detail,
                    ),
                )
                chapter_markers = [
                    (start_ms / 1000, chapter.title)
                    for chapter, start_ms in zip(
                        output_plan.chapters,
                        assembly_result.chapter_starts_ms,
                        strict=True,
                    )
                ]
                for index, (
                    segment,
                    take,
                    artifact,
                    _source_path,
                    _duration_ms,
                ) in enumerate(loaded):
                    manifest.append(
                        {
                            "segment_id": segment.id,
                            "segment_revision": segment.revision,
                            "node_kind": segment.node_kind,
                            "speaker": segment.speaker,
                            "take_id": take.id,
                            "take_revision": take.revision,
                            "artifact_id": artifact.id,
                            "kind": take.kind,
                            "duration_ms": assembly_result.part_duration_ms[index],
                            "silence_after_ms": planned_parts[index].silence_after_ms,
                        }
                    )

            progress(0.9, "Applying output metadata")
            metadata: dict[str, str] = {}
            cover_artifact_id = ""
            cover_path = None
            if session_record.workflow_kind == "audiobook":
                metadata = {
                    "title": str(output_settings.get("title") or session_record.name),
                    "artist": str(output_settings.get("artist") or ""),
                    "album": str(output_settings.get("album") or ""),
                    "genre": str(output_settings.get("genre") or ""),
                    "language": str(output_settings.get("language") or ""),
                }
                cover_artifact_id = str(
                    output_settings.get("cover_artifact_id") or ""
                ).strip()
                if cover_artifact_id:
                    cover_artifact, candidate = self._resolve_input(cover_artifact_id)
                    if (
                        cover_artifact.state != "current"
                        or not candidate.is_file()
                        or not str(cover_artifact.mime_type or "").startswith("image/")
                    ):
                        raise ValueError(
                            "The selected cover artifact is not an available image."
                        )
                    cover_path = candidate
                    parent_ids.append(cover_artifact.id)
                from pandrator.logic.audio_processor import (
                    _add_chapters_to_m4b,
                    _save_metadata_and_cover,
                )

                _save_metadata_and_cover(
                    str(destination),
                    output_format,
                    metadata,
                    str(cover_path) if cover_path else None,
                    raise_on_error=True,
                )
                if output_format == "m4b" and chapter_markers:
                    _add_chapters_to_m4b(
                        str(destination),
                        chapter_markers,
                        total_duration_sec=assembly_result.duration_ms / 1000,
                        raise_on_error=True,
                    )
                    _save_metadata_and_cover(
                        str(destination),
                        output_format,
                        metadata,
                        str(cover_path) if cover_path else None,
                        raise_on_error=True,
                    )
            progress(0.96, "Registering assembled audio")
            output_settings_snapshot = build_output_settings_snapshot(
                {},
                {
                    "audio": audio_settings,
                    "output": output_settings,
                },
            )
            artifact = self.artifacts.register(
                destination,
                kind="audio",
                role="assembled_audio",
                session_id=session_id,
                parent_ids=parent_ids,
                settings={
                    "audio": audio_settings,
                    "output": output_settings,
                    "takes": manifest,
                },
                metadata={
                    "output_assembly_id": assembly_id,
                    "duration_ms": assembly_result.duration_ms,
                    "segment_count": len(selected),
                    "format": output_format,
                    "bitrate": bitrate,
                    "assembly_backend": assembly_result.backend,
                    "metadata": metadata,
                    "cover_artifact_id": cover_artifact_id or None,
                    "chapters": [
                        {"start_ms": int(start * 1000), "title": title}
                        for start, title in chapter_markers
                    ],
                    "takes": manifest,
                    "synchronization": alignment_diagnostics,
                    "output_settings": output_settings_snapshot,
                },
            )
            output_registered = True
            with self.database.session() as session:
                assembly = session.get(OutputAssembly, assembly_id)
                assembly.artifact_id = artifact.id
                assembly.status = "completed"
                assembly.error_message = None
                assembly.settings_json = {
                    **dict(assembly.settings_json or {}),
                    "takes": manifest,
                    "duration_ms": assembly_result.duration_ms,
                    "assembly_backend": assembly_result.backend,
                    "synchronization": alignment_diagnostics,
                }
                assembly.updated_at = utcnow()
            progress(1.0, "Output assembly ready")
            return {
                "output_assembly_id": assembly_id,
                "artifact_id": artifact.id,
                "duration_ms": assembly_result.duration_ms,
                "segment_count": len(selected),
                "format": output_format,
                "assembly_backend": assembly_result.backend,
                "synchronization": {
                    key: value
                    for key, value in alignment_diagnostics.items()
                    if key != "blocks"
                },
            }
        except MediaProcessCancelled:
            if destination is not None and not output_registered:
                destination.unlink(missing_ok=True)
            with self.database.session() as session:
                assembly = session.get(OutputAssembly, assembly_id)
                if assembly is not None:
                    assembly.status = "canceled"
                    assembly.error_message = None
                    assembly.updated_at = utcnow()
            return {}
        except Exception as error:
            if destination is not None and not output_registered:
                destination.unlink(missing_ok=True)
            with self.database.session() as session:
                assembly = session.get(OutputAssembly, assembly_id)
                if assembly is not None:
                    assembly.status = "failed"
                    assembly.error_message = str(error)
                    assembly.updated_at = utcnow()
            raise

    def generate_waveform(self, payload, progress, cancel_event):
        """Create a compact, reusable peak artifact for browser review."""
        from .media_process import MediaProcessCancelled
        from .waveform import generate_waveform_peaks

        source, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        max_points = max(128, min(5000, int(payload.get("max_points") or 1600)))
        destination_dir = (
            self._session_dir(source.session_id)
            if source.session_id
            else self.paths.artifacts / "waveforms"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        progress(0.1, "Downsampling audio for waveform")
        try:
            waveform = generate_waveform_peaks(
                source_path,
                max_points=max_points,
                work_dir=destination_dir,
                cancel_event=cancel_event,
            )
        except MediaProcessCancelled:
            return {}
        if cancel_event.is_set():
            return {}
        progress(0.9, "Writing waveform peaks")
        destination = destination_dir / f"waveform-{source.id}-{max_points}.json"
        destination.write_text(
            json.dumps(
                {
                    "duration_ms": waveform.duration_ms,
                    "channels": waveform.channels,
                    "points": waveform.points,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        artifact = self.artifacts.register(
            destination,
            kind="json",
            role="waveform_peaks",
            session_id=source.session_id,
            parent_ids=[source.id],
            settings={"max_points": max_points},
            metadata={
                "source_artifact_id": source.id,
                "duration_ms": waveform.duration_ms,
                "analysis_sample_rate_hz": waveform.analysis_sample_rate_hz,
            },
        )
        progress(1.0, "Waveform ready")
        return {
            "artifact_id": artifact.id,
            "source_artifact_id": source.id,
            "point_count": len(waveform.points),
        }

    def preview_output_mix(self, payload, progress, cancel_event):
        """Render a short, managed sample with the exact export mix graph."""

        from pandrator.logic.dubbing.audio_sync import build_mix_preview_command

        from .media_process import (
            MediaProcessCancelled,
            find_first_audible_seconds,
            probe_audio_stream,
            resolve_ffmpeg_executable,
            run_media_process,
        )

        session_id = str(payload.get("session_id") or "")
        source, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        dubbing, dubbing_path = self._resolve_input(
            str(payload.get("dubbing_artifact_id") or "")
        )
        if source.session_id != session_id or dubbing.session_id != session_id:
            raise ValueError("Mix preview inputs do not belong to this session.")

        settings = dict(payload.get("settings") or {})
        automatic_start = payload.get("start_seconds") is None
        automatic_start_method = "manual"
        try:
            requested_start = (
                0.0
                if automatic_start
                else max(0.0, float(payload.get("start_seconds")))
            )
            requested_duration = min(
                30.0,
                max(4.0, float(payload.get("duration_seconds") or 12.0)),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Mix preview timing must use numeric seconds.") from error

        progress(0.08, "Inspecting preview audio")
        try:
            source_info = probe_audio_stream(source_path, cancel_event=cancel_event)
            dubbing_info = probe_audio_stream(dubbing_path, cancel_event=cancel_event)
            if automatic_start:
                timeline_starts = [
                    float(item["target_start_ms"])
                    for item in (dubbing.metadata_json or {}).get("takes", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("target_start_ms"), (int, float))
                ]
                if timeline_starts:
                    requested_start = max(0.0, min(timeline_starts) / 1000.0 - 1.0)
                    automatic_start_method = "assembly_timeline"
                else:
                    requested_start = find_first_audible_seconds(
                        dubbing_path,
                        cancel_event=cancel_event,
                    )
                    automatic_start_method = "audio_detection"
        except MediaProcessCancelled:
            return {}
        available_seconds = (
            min(
                source_info.duration_ms,
                dubbing_info.duration_ms,
            )
            / 1000.0
        )
        remaining_seconds = available_seconds - requested_start
        if remaining_seconds < 0.25:
            raise ValueError(
                "The preview start is beyond the available source and voiceover audio."
            )
        duration_seconds = min(requested_duration, remaining_seconds)

        destination_dir = self._session_dir(session_id) / "previews"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / "soundtrack-mix-preview.wav"
        temporary_destination = (
            destination_dir / f".soundtrack-mix-preview-{new_id()}.wav"
        )
        command = build_mix_preview_command(
            source_path,
            dubbing_path,
            temporary_destination,
            start_seconds=requested_start,
            duration_seconds=duration_seconds,
            source_gain_db=settings.get("mix_source_gain_db", 0.0),
            voice_gain_db=settings.get("mix_voice_gain_db", 0.0),
            voice_lufs=settings.get("mix_voice_lufs", -16.0),
            ducking=str(settings.get("mix_ducking") or "strong"),
            attack_ms=settings.get("mix_attack_ms", 25),
            release_ms=settings.get("mix_release_ms", 350),
            ffmpeg_executable=resolve_ffmpeg_executable(),
        )
        progress(0.2, "Rendering soundtrack mix preview")
        try:
            run_media_process(command, cancel_event=cancel_event)
            if cancel_event.is_set():
                temporary_destination.unlink(missing_ok=True)
                return {}
            os.replace(temporary_destination, destination)
        except MediaProcessCancelled:
            temporary_destination.unlink(missing_ok=True)
            return {}
        except Exception:
            temporary_destination.unlink(missing_ok=True)
            raise

        progress(0.9, "Registering soundtrack mix preview")
        artifact = self.artifacts.register(
            destination,
            kind="audio",
            role="mix_preview",
            session_id=session_id,
            parent_ids=[source.id, dubbing.id],
            replace_parent_ids=True,
            settings=settings,
            metadata={
                "generation_run_id": str(payload.get("generation_run_id") or ""),
                "source_artifact_id": source.id,
                "dubbing_artifact_id": dubbing.id,
                "start_seconds": requested_start,
                "duration_seconds": duration_seconds,
                "automatic_start": automatic_start,
                "automatic_start_method": automatic_start_method,
                "mix": {
                    "source_gain_db": settings.get("mix_source_gain_db", 0.0),
                    "voice_gain_db": settings.get("mix_voice_gain_db", 0.0),
                    "voice_lufs": settings.get("mix_voice_lufs", -16.0),
                    "ducking": settings.get("mix_ducking", "strong"),
                    "attack_ms": settings.get("mix_attack_ms", 25),
                    "release_ms": settings.get("mix_release_ms", 350),
                },
            },
        )
        progress(1.0, "Soundtrack mix preview ready")
        return {
            "artifact_id": artifact.id,
            "artifact": {
                "id": artifact.id,
                "session_id": artifact.session_id,
                "kind": artifact.kind,
                "role": artifact.role,
                "relative_path": artifact.relative_path,
                "mime_type": artifact.mime_type,
                "size_bytes": artifact.size_bytes,
                "content_hash": artifact.content_hash,
                "state": artifact.state,
                "metadata_json": artifact.metadata_json,
                "created_at": artifact.created_at.isoformat(),
            },
            "start_seconds": requested_start,
            "duration_seconds": duration_seconds,
            "automatic_start": automatic_start,
            "automatic_start_method": automatic_start_method,
        }

    def _ensure_export_generation_assembly(
        self,
        *,
        session_id: str,
        generation_run_id: str,
        resolved_settings_snapshot: dict[str, Any],
        progress,
        cancel_event: threading.Event,
    ) -> str | None:
        """Reuse or synchronously build the exact assembly required by an export plan."""

        from .workspace import (
            output_assembly_settings_hash,
            output_assembly_settings_snapshot,
        )

        assembly_snapshot = output_assembly_settings_snapshot(
            resolved_settings_snapshot
        )
        settings_hash = output_assembly_settings_hash(resolved_settings_snapshot)
        with self.database.session() as session:
            run = session.get(GenerationRun, generation_run_id)
            if run is None or run.session_id != session_id:
                raise ValueError(
                    "The selected generation run does not belong to this session."
                )
            if run.status != "completed":
                raise ValueError("Only a completed generation run can be exported.")
            existing_candidates = list(
                session.scalars(
                    select(OutputAssembly)
                    .join(Artifact, Artifact.id == OutputAssembly.artifact_id)
                    .where(
                        OutputAssembly.session_id == session_id,
                        OutputAssembly.generation_run_id == generation_run_id,
                        OutputAssembly.status == "completed",
                        Artifact.state == "current",
                    )
                    .order_by(OutputAssembly.created_at.desc())
                ).all()
            )
            existing = next(
                (
                    candidate
                    for candidate in existing_candidates
                    if candidate.settings_hash == settings_hash
                    or output_assembly_settings_hash(
                        dict((candidate.settings_json or {}).get("resolved") or {})
                    )
                    == settings_hash
                ),
                None,
            )
            if existing is not None and existing.artifact_id:
                progress(0.68, "Using the selected generation run assembly")
                return existing.artifact_id
            assembly = OutputAssembly(
                session_id=session_id,
                generation_run_id=generation_run_id,
                status="queued",
                settings_json={
                    "resolved": assembly_snapshot,
                    "plan_revision_id": run.plan_revision_id,
                },
                settings_hash=settings_hash,
            )
            session.add(assembly)
            session.flush()
            assembly_id = assembly.id

        result = self.assemble_generation_output(
            {"output_assembly_id": assembly_id},
            _scaled_progress_callback(progress, 0.0, 0.68),
            cancel_event,
        )
        if cancel_event.is_set():
            return None
        artifact_id = str((result or {}).get("artifact_id") or "")
        if not artifact_id:
            raise ValueError(
                "The selected generation run could not be assembled for export."
            )
        return artifact_id

    def export_variant(self, payload, progress, cancel_event):
        """Assemble the selected completed generation run when needed, then export once."""

        settings = dict(payload.get("settings") or {})
        generation_run_id = str(settings.get("generation_run_id") or "").strip()
        resolved_snapshot = payload.get("resolved_settings_snapshot")
        resolved_snapshot = (
            resolved_snapshot if isinstance(resolved_snapshot, dict) else {}
        )
        record = self._session_record(str(payload.get("session_id") or ""))
        export_mode = str(settings.get("export_mode") or "media").lower()
        audio_mode = normalize_audio_mode(settings.get("audio_mode"))
        needs_assembly = bool(generation_run_id) and (
            record.workflow_kind == "audiobook"
            or (export_mode == "media" and audio_mode in {"mixed", "dubbing_only"})
        )
        if needs_assembly:
            self._ensure_export_generation_assembly(
                session_id=record.id,
                generation_run_id=generation_run_id,
                resolved_settings_snapshot=resolved_snapshot,
                progress=progress,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                return {}
            export_progress = _scaled_progress_callback(progress, 0.7, 1.0)
        else:
            export_progress = progress
        return self.export(payload, export_progress, cancel_event)

    def export(self, payload, progress, cancel_event):
        """Create immutable, managed exports from the explicitly selected inputs."""
        from werkzeug.utils import secure_filename

        from pandrator.logic.dubbing.audio_sync import (
            build_mix_audio_command,
            build_mix_video_audio_command,
            media_has_audio_stream,
        )
        from pandrator.logic.dubbing.bilingual_ass import write_bilingual_ass
        from pandrator.logic.dubbing.srt_utils import (
            concatenate_subtitle_text,
            srt_to_vtt,
        )
        from pandrator.logic.dubbing.subtitle_finalization import finalize_srt_file
        from pandrator.logic.dubbing.video_muxing import (
            build_add_subtitles_command,
            build_multi_soft_subtitle_command,
            build_replace_video_audio_command,
            build_video_transcode_command,
            normalize_video_resolution,
        )
        from pandrator.logic.dubbing_handler import resolve_ffmpeg_for_burned_subtitles
        from pandrator.web.capabilities import ffmpeg_video_encoder_ids

        session_id = str(payload.get("session_id") or "")
        settings = dict(payload.get("settings") or {})
        raw_export_contract = payload.get("export_contract")
        if raw_export_contract is not None and not isinstance(
            raw_export_contract, dict
        ):
            raise ValueError(
                "The queued export contract is malformed; submit the export again."
            )
        resolved_settings_snapshot = payload.get("resolved_settings_snapshot")
        output_settings_snapshot = build_output_settings_snapshot(
            settings,
            (
                resolved_settings_snapshot
                if isinstance(resolved_settings_snapshot, dict)
                else None
            ),
        )
        expected_assembly_settings_hash = None
        if isinstance(resolved_settings_snapshot, dict):
            from .workspace import output_assembly_settings_hash

            expected_assembly_settings_hash = output_assembly_settings_hash(
                resolved_settings_snapshot
            )
        record = self._session_record(session_id)
        with self.database.session() as session:
            current = list(
                session.scalars(
                    select(Artifact).where(
                        Artifact.session_id == session_id, Artifact.state == "current"
                    )
                ).all()
            )
            selected_text = selected_artifacts(session, session_id)
            contract_source_id = (
                str(raw_export_contract.get("source_artifact_id") or "")
                if isinstance(raw_export_contract, dict)
                else ""
            )
            if contract_source_id:
                contract_source = session.get(Artifact, contract_source_id)
                if contract_source is None:
                    raise ValueError(
                        "The source captured by this export contract is no longer available. "
                        "Submit the export again."
                    )
                expected_source_hash = str(
                    raw_export_contract.get("source_content_hash") or ""
                )
                if (
                    expected_source_hash
                    and contract_source.content_hash != expected_source_hash
                ):
                    raise ValueError(
                        "The source captured by this export contract changed. Submit the export again."
                    )
                attached_sources = [contract_source]
            elif isinstance(raw_export_contract, dict):
                # An explicit no-source contract must stay no-source even if the
                # session is edited before the queued worker starts.
                attached_sources = []
            else:
                compatibility_source = resolve_primary_source(
                    session, session_id
                ).artifact
                attached_sources = (
                    [compatibility_source] if compatibility_source else []
                )
            known_ids = {item.id for item in current}
            current.extend(
                item for item in attached_sources if item.id not in known_ids
            )
            selected_assembly = None
            selected_audio = None
            selected_run_id = str(settings.get("generation_run_id") or "").strip()
            if selected_run_id:
                assembly_candidates = list(
                    session.scalars(
                        select(OutputAssembly)
                        .join(Artifact, Artifact.id == OutputAssembly.artifact_id)
                        .where(
                            OutputAssembly.session_id == session_id,
                            OutputAssembly.generation_run_id == selected_run_id,
                            OutputAssembly.status == "completed",
                            Artifact.state == "current",
                        )
                        .order_by(OutputAssembly.created_at.desc())
                    ).all()
                )
                if expected_assembly_settings_hash:
                    selected_assembly = next(
                        (
                            candidate
                            for candidate in assembly_candidates
                            if candidate.settings_hash
                            == expected_assembly_settings_hash
                            or output_assembly_settings_hash(
                                dict(
                                    (candidate.settings_json or {}).get("resolved")
                                    or {}
                                )
                            )
                            == expected_assembly_settings_hash
                        ),
                        None,
                    )
                else:
                    selected_assembly = (
                        assembly_candidates[0] if assembly_candidates else None
                    )
                if selected_assembly is None:
                    if expected_assembly_settings_hash:
                        stale_assembly = session.scalar(
                            select(OutputAssembly.id).where(
                                OutputAssembly.session_id == session_id,
                                OutputAssembly.generation_run_id == selected_run_id,
                                OutputAssembly.status == "completed",
                                OutputAssembly.artifact_id.is_not(None),
                            )
                        )
                        if stale_assembly is not None:
                            raise ValueError(
                                "Synchronization or output settings changed after this audio version was assembled. Reassemble it before exporting."
                            )
                    raise ValueError(
                        "Assemble the selected generation run before exporting it."
                    )
                selected_audio = session.get(Artifact, selected_assembly.artifact_id)
                if selected_audio is None:
                    raise ValueError(
                        "The selected generation run assembly is unavailable."
                    )
        by_role: dict[str, Artifact] = {}
        for item in current:
            by_role.setdefault(item.role, item)
        for item in selected_text.values():
            by_role[item.role] = item
        output_dir = self._session_dir(session_id) / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        export_name = secure_filename(record.name) or record.storage_key
        progress(0.1, "Preparing export")
        produced: list[Artifact] = []

        if record.workflow_kind == "audiobook":
            audio = (
                selected_audio
                if selected_assembly is not None
                else by_role.get("assembled_audio") or by_role.get("audiobook_audio")
            )
            if audio is None:
                raise ValueError("Audiobook export requires generated audio.")
            _audio_record, audio_path = self._resolve_input(audio.id)
            destination = _next_available_path(
                output_dir / f"{export_name}{audio_path.suffix.lower()}"
            )
            progress(0.25, "Copying assembled audiobook")
            shutil.copy2(audio_path, destination)
            progress(0.9, "Registering audiobook export")
            produced.append(
                self.artifacts.register(
                    destination,
                    kind="export",
                    role="export",
                    session_id=session_id,
                    parent_ids=[audio.id],
                    settings=settings,
                )
            )
        else:
            upload_media = next(
                (
                    item
                    for item in attached_sources
                    if Path(item.relative_path).suffix.lower()
                    in {
                        ".mp4",
                        ".mkv",
                        ".mov",
                        ".avi",
                        ".webm",
                        ".m4v",
                        ".mpeg",
                        ".mpg",
                    }
                ),
                None,
            )
            upload_audio = next(
                (
                    item
                    for item in attached_sources
                    if Path(item.relative_path).suffix.lower()
                    in {
                        ".wav",
                        ".mp3",
                        ".flac",
                        ".m4a",
                        ".aac",
                        ".ogg",
                        ".opus",
                        ".wma",
                    }
                ),
                None,
            )
            translated = by_role.get("translation")
            source_subtitle = (
                by_role.get("correction")
                or by_role.get("transcription")
                or next(
                    (
                        item
                        for item in attached_sources
                        if Path(item.relative_path).suffix.lower() == ".srt"
                    ),
                    None,
                )
            )
            export_mode = str(
                settings.get("export_mode")
                or ("subtitles" if record.workflow_kind == "subtitles" else "media")
            ).lower()
            if record.workflow_kind == "subtitles" and export_mode not in {
                "subtitles",
                "text",
            }:
                export_mode = "subtitles"
            if export_mode not in {"media", "subtitles", "text"}:
                export_mode = "media"
            contract = (
                ExportContract.verify(
                    raw_export_contract,
                    workflow_kind=record.workflow_kind,
                    settings=settings,
                )
                if isinstance(raw_export_contract, dict)
                else None
            )
            if contract is not None:
                export_mode = contract.export_mode
            subtitle_format = str(settings.get("subtitle_format") or "srt").lower()
            if subtitle_format not in {"srt", "vtt"}:
                subtitle_format = "srt"
            subtitle_mode = str(settings.get("subtitle_mode") or "none").lower()
            subtitle_mode = {"burn": "burned"}.get(subtitle_mode, subtitle_mode)
            subtitle_selection = str(
                settings.get("subtitle_selection")
                or ("dual" if translated and source_subtitle else "translation")
            ).lower()
            subtitle_selection = {"both": "dual"}.get(
                subtitle_selection, subtitle_selection
            )
            if (
                subtitle_selection == "translation"
                and translated is None
                and source_subtitle is not None
            ):
                subtitle_selection = "source"
            elif (
                subtitle_selection == "source"
                and source_subtitle is None
                and translated is not None
            ):
                subtitle_selection = "translation"
            selected_subtitles = (
                [source_subtitle]
                if source_subtitle and subtitle_selection in {"source", "dual"}
                else []
            ) + (
                [translated]
                if translated and subtitle_selection in {"translation", "dual"}
                else []
            )
            # "No subtitles" only suppresses tracks on a media render. A
            # subtitle/text-only request still uses the selected document.
            selected_subtitles = (
                [item for item in selected_subtitles if item]
                if export_mode != "media"
                or subtitle_mode != "none"
                or upload_media is None
                else []
            )
            dubbing_audio = (
                selected_audio
                if selected_assembly is not None
                else by_role.get("assembled_audio") or by_role.get("dubbing_audio")
            )
            if record.workflow_kind == "voiceover" and export_mode == "media":
                canonical_audio_mode = (
                    contract.audio_mode
                    if contract is not None
                    else normalize_audio_mode(settings.get("audio_mode"))
                )
                if canonical_audio_mode in {"preserve", "mixed"} and not (
                    upload_media or upload_audio
                ):
                    raise ValueError(
                        "The requested source-audio export has no attached source. "
                        "Attach the intended source or choose Voiceover only."
                    )
                audio_mode = {
                    "preserve": "source",
                    "dubbing_only": "dubbed",
                    "mixed": "mixed",
                }[canonical_audio_mode]
            else:
                audio_mode = "source"
            if (
                export_mode == "media"
                and audio_mode in {"dubbed", "mixed"}
                and dubbing_audio is None
            ):
                raise ValueError(
                    "This media export requires assembled generated audio. Select a completed audio version and assemble it before exporting."
                )
            if (
                export_mode == "media"
                and upload_media
                and audio_mode in {"source", "mixed"}
            ):
                _source_record, source_media_path = self._resolve_input(upload_media.id)
                ffprobe_executable = str(
                    os.environ.get("PANDRATOR_FFPROBE_EXE")
                    or shutil.which("ffprobe")
                    or ""
                )
                if not ffprobe_executable:
                    raise RuntimeError(
                        "FFprobe is required to verify the requested source soundtrack."
                    )
                try:
                    source_has_audio = media_has_audio_stream(
                        source_media_path,
                        ffprobe_executable=ffprobe_executable,
                    )
                except subprocess.CalledProcessError as error:
                    raise ValueError(
                        "The source video soundtrack could not be inspected. "
                        "Verify the source file and submit the export again."
                    ) from error
                if not source_has_audio:
                    action = "preserve" if audio_mode == "source" else "mix"
                    raise ValueError(
                        f"The source video has no audio stream to {action}. "
                        "Choose Voiceover only and submit the export again."
                    )
            finalized_subtitles: list[Artifact] = []
            progress(
                0.12,
                (
                    f"Preparing {len(selected_subtitles)} subtitle track"
                    f"{'s' if len(selected_subtitles) != 1 else ''}"
                    if selected_subtitles
                    else "Subtitle selection complete"
                ),
            )
            for index, item in enumerate(selected_subtitles, start=1):
                _subtitle_record, subtitle_path = self._resolve_input(item.id)
                track_name = "translation" if item.role == "translation" else "source"
                finalized_path = _next_available_path(
                    output_dir / f"{record.storage_key}_{track_name}_final.srt"
                )
                finalize_srt_file(subtitle_path, finalized_path, settings)
                finalized = self.artifacts.register(
                    finalized_path,
                    kind="srt",
                    role=f"final_subtitle_{track_name}",
                    session_id=session_id,
                    parent_ids=[item.id],
                    settings=settings,
                    metadata={
                        "language": _effective_subtitle_language(
                            (item.metadata_json or {}).get("language"),
                            (
                                record.target_language
                                if track_name == "translation"
                                else record.source_language
                            ),
                            (
                                settings.get("target_language")
                                if track_name == "translation"
                                else settings.get("original_language")
                                or settings.get("source_language")
                            ),
                        ),
                        "source_role": item.role,
                    },
                )
                finalized_subtitles.append(finalized)
                progress(
                    0.12 + 0.18 * (index / len(selected_subtitles)),
                    f"Prepared subtitle track {index} of {len(selected_subtitles)}",
                )
            selected_subtitles = finalized_subtitles

            def is_translation_track(item: Artifact) -> bool:
                return (
                    str((item.metadata_json or {}).get("source_role") or item.role)
                    == "translation"
                )

            def track_details(item: Artifact) -> tuple[str, str, str, bool]:
                translation_track = is_translation_track(item)
                track_name = "translation" if translation_track else "source"
                language = _effective_subtitle_language(
                    (item.metadata_json or {}).get("language"),
                    record.target_language
                    if translation_track
                    else record.source_language,
                    (
                        settings.get("target_language")
                        if translation_track
                        else settings.get("original_language")
                        or settings.get("source_language")
                    ),
                )
                title = subtitle_language_title(language)
                is_default = translation_track or len(selected_subtitles) == 1
                return track_name, language, title, is_default

            if export_mode in {"subtitles", "text"}:
                if not selected_subtitles:
                    raise ValueError(
                        "No subtitle artifact is available for this export."
                    )
                for index, item in enumerate(selected_subtitles, start=1):
                    progress(
                        0.35 + 0.5 * ((index - 1) / len(selected_subtitles)),
                        f"Writing export track {index} of {len(selected_subtitles)}",
                    )
                    _artifact, subtitle_path = self._resolve_input(item.id)
                    track_name, language, title, _default = track_details(item)
                    if export_mode == "text":
                        destination = _next_available_path(
                            output_dir / f"{export_name}_{track_name}.txt"
                        )
                        destination.write_text(
                            concatenate_subtitle_text(
                                subtitle_path.read_text(encoding="utf-8-sig")
                            ),
                            encoding="utf-8",
                        )
                        kind = "text"
                        role = f"export_text_{track_name}"
                    else:
                        destination = _next_available_path(
                            output_dir / f"{export_name}_{track_name}.{subtitle_format}"
                        )
                        if subtitle_format == "vtt":
                            destination.write_text(
                                srt_to_vtt(
                                    subtitle_path.read_text(encoding="utf-8-sig")
                                ),
                                encoding="utf-8",
                            )
                        else:
                            shutil.copy2(subtitle_path, destination)
                        kind = subtitle_format
                        role = f"export_subtitle_{track_name}"
                    produced.append(
                        self.artifacts.register(
                            destination,
                            kind=kind,
                            role=role,
                            session_id=session_id,
                            parent_ids=[item.id],
                            settings=settings,
                            metadata={
                                "language": language,
                                "title": title,
                                "source_role": (item.metadata_json or {}).get(
                                    "source_role"
                                ),
                            },
                        )
                    )
                    progress(
                        0.35 + 0.55 * (index / len(selected_subtitles)),
                        f"Exported track {index} of {len(selected_subtitles)}",
                    )
            elif upload_media:
                progress(0.35, "Preparing source media")
                _media_record, media_path = self._resolve_input(upload_media.id)
                working_video = media_path
                audio_parent_ids: list[str] = [upload_media.id]
                temporary_video: Path | None = None
                ffmpeg_executable = str(
                    os.environ.get("PANDRATOR_FFMPEG_EXE")
                    or shutil.which("ffmpeg")
                    or "ffmpeg"
                )
                video_audio_bitrate = str(
                    settings.get("burn_audio_bitrate") or "192k"
                ).strip()
                video_audio_codec = (
                    str(settings.get("burn_audio_codec") or "copy").strip().lower()
                )
                if (
                    audio_mode in {"dubbed", "mixed"} or video_audio_codec == "aac"
                ) and not re.fullmatch(
                    r"[1-9][0-9]*(?:[kKmM])?",
                    video_audio_bitrate,
                ):
                    raise ValueError("Video AAC bitrate must look like 192k or 2M.")
                if dubbing_audio and audio_mode in {"dubbed", "mixed"}:
                    _audio_record, audio_path = self._resolve_input(dubbing_audio.id)
                    audio_video = (
                        output_dir / f".{record.storage_key}-audio-{new_id()}.mp4"
                    )
                    if audio_mode == "dubbed":
                        command = build_replace_video_audio_command(
                            str(media_path),
                            str(audio_path),
                            str(audio_video),
                            ffmpeg_executable=ffmpeg_executable,
                            audio_bitrate=video_audio_bitrate,
                        )
                        progress(0.4, "Replacing source audio with generated speech")
                    else:
                        command = build_mix_video_audio_command(
                            str(media_path),
                            str(audio_path),
                            str(audio_video),
                            source_gain_db=settings.get("mix_source_gain_db", 0.0),
                            voice_gain_db=settings.get("mix_voice_gain_db", 0.0),
                            voice_lufs=settings.get("mix_voice_lufs", -16.0),
                            ducking=str(settings.get("mix_ducking") or "strong"),
                            attack_ms=settings.get("mix_attack_ms", 25),
                            release_ms=settings.get("mix_release_ms", 350),
                            audio_bitrate=video_audio_bitrate,
                            ffmpeg_executable=ffmpeg_executable,
                        )
                        progress(0.4, "Mixing source audio with generated speech")
                    subprocess.run(command, check=True, capture_output=True, text=True)
                    progress(0.58, "Media audio track ready")
                    working_video = audio_video
                    temporary_video = audio_video
                    audio_parent_ids.append(dubbing_audio.id)
                video_transcode = bool(settings.get("video_transcode")) or (
                    subtitle_mode == "burned" and bool(selected_subtitles)
                )
                video_encoder = (
                    str(settings.get("burn_video_encoder") or "libx264").strip().lower()
                )
                output_video_resolution = (
                    normalize_video_resolution(
                        settings.get("burn_video_resolution", "source")
                    )
                    if video_transcode
                    else "source"
                )
                # Replacement and mixed soundtracks were encoded to AAC in the
                # preparation step above. Copy them during the video render so
                # the user's chosen bitrate is not lost to a second encode.
                render_audio_codec = (
                    video_audio_codec if audio_mode == "source" else "copy"
                )
                variant = (
                    f"_{subtitle_mode}"
                    if subtitle_mode in {"soft", "burned"} and selected_subtitles
                    else ""
                )
                destination = _next_available_path(
                    output_dir / f"{export_name}{variant}.mp4"
                )
                render_destination = (
                    output_dir / f".{record.storage_key}-render-{new_id()}.mp4"
                )
                video_track_artifacts: list[Artifact] = []
                try:
                    if subtitle_mode == "soft" and selected_subtitles:
                        tracks = []
                        for index, item in enumerate(selected_subtitles, start=1):
                            _subtitle, subtitle_path = self._resolve_input(item.id)
                            track_name, language, title, is_default = track_details(
                                item
                            )
                            tracks.append(
                                {
                                    "path": str(subtitle_path),
                                    "language": language,
                                    "title": title,
                                    "default": is_default,
                                }
                            )
                            vtt_path = _next_available_path(
                                output_dir
                                / f"{record.storage_key}_{track_name}_player.vtt"
                            )
                            vtt_path.write_text(
                                srt_to_vtt(
                                    subtitle_path.read_text(encoding="utf-8-sig")
                                ),
                                encoding="utf-8",
                            )
                            video_track_artifacts.append(
                                self.artifacts.register(
                                    vtt_path,
                                    kind="vtt",
                                    role=f"video_subtitle_track_{track_name}",
                                    session_id=session_id,
                                    parent_ids=[item.id],
                                    settings=settings,
                                    metadata={
                                        "language": language,
                                        "title": title,
                                        "default": is_default,
                                    },
                                )
                            )
                            progress(
                                0.58 + 0.06 * (index / len(selected_subtitles)),
                                f"Prepared selectable subtitle track {index} of {len(selected_subtitles)}",
                            )
                        if (
                            video_transcode
                            and video_encoder
                            not in ffmpeg_video_encoder_ids(ffmpeg_executable)
                        ):
                            raise RuntimeError(
                                f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                            )
                        command = build_multi_soft_subtitle_command(
                            str(working_video),
                            tracks,
                            str(render_destination),
                            ffmpeg_executable=ffmpeg_executable,
                            transcode_video=video_transcode,
                            video_encoder=video_encoder,
                            video_resolution=output_video_resolution,
                            video_quality=settings.get("burn_video_quality", 18),
                            video_speed=str(
                                settings.get("burn_video_speed") or "balanced"
                            ),
                            audio_codec=render_audio_codec,
                            audio_bitrate=video_audio_bitrate,
                        )
                        progress(
                            0.65,
                            "Transcoding media with selectable subtitles"
                            if video_transcode
                            else "Rendering media with selectable subtitles",
                        )
                        subprocess.run(
                            command, check=True, capture_output=True, text=True
                        )
                    elif subtitle_mode == "burned" and selected_subtitles:
                        subtitle_paths = [
                            self._resolve_input(item.id)[1]
                            for item in selected_subtitles
                        ]
                        burn_path = subtitle_paths[-1]
                        if len(subtitle_paths) == 2:
                            burn_path = Path(
                                write_bilingual_ass(
                                    str(subtitle_paths[0]),
                                    str(subtitle_paths[1]),
                                    str(
                                        _next_available_path(
                                            output_dir / "bilingual_subtitles.ass"
                                        )
                                    ),
                                )
                            )
                            self.artifacts.register(
                                burn_path,
                                kind="ass",
                                role="bilingual_subtitle_overlay",
                                session_id=session_id,
                                parent_ids=[item.id for item in selected_subtitles],
                                settings=settings,
                            )
                        burn_ffmpeg = resolve_ffmpeg_for_burned_subtitles()
                        if not burn_ffmpeg:
                            raise RuntimeError(
                                "Burned subtitles require an FFmpeg build with the subtitles/libass filter. Install or select Pandrator's bundled FFmpeg, or use soft subtitles."
                            )
                        if video_encoder not in ffmpeg_video_encoder_ids(burn_ffmpeg):
                            raise RuntimeError(
                                f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                            )
                        command = build_add_subtitles_command(
                            str(working_video),
                            str(burn_path),
                            str(render_destination),
                            subtitle_mode="burned",
                            subtitle_language=str(
                                settings.get("target_language") or "und"
                            ),
                            ffmpeg_executable=burn_ffmpeg,
                            video_encoder=video_encoder,
                            video_resolution=output_video_resolution,
                            video_quality=settings.get("burn_video_quality", 18),
                            video_speed=str(
                                settings.get("burn_video_speed") or "balanced"
                            ),
                            audio_codec=render_audio_codec,
                            audio_bitrate=video_audio_bitrate,
                        )
                        progress(0.65, "Rendering burned subtitles into video")
                        try:
                            subprocess.run(
                                command, check=True, capture_output=True, text=True
                            )
                        except subprocess.CalledProcessError as error:
                            detail = (
                                str(error.stderr or error.stdout or "")
                                .strip()
                                .splitlines()
                            )
                            reason = (
                                detail[-1]
                                if detail
                                else "FFmpeg returned a non-zero exit status."
                            )
                            raise RuntimeError(
                                f"Burned-subtitle transcoding with {video_encoder} failed: {reason}"
                            ) from error
                    elif video_transcode:
                        if video_encoder not in ffmpeg_video_encoder_ids(
                            ffmpeg_executable
                        ):
                            raise RuntimeError(
                                f"The selected FFmpeg build does not provide the {video_encoder} video encoder."
                            )
                        command = build_video_transcode_command(
                            str(working_video),
                            str(render_destination),
                            ffmpeg_executable=ffmpeg_executable,
                            video_encoder=video_encoder,
                            video_resolution=output_video_resolution,
                            video_quality=settings.get("burn_video_quality", 18),
                            video_speed=str(
                                settings.get("burn_video_speed") or "balanced"
                            ),
                            audio_codec=render_audio_codec,
                            audio_bitrate=video_audio_bitrate,
                        )
                        progress(0.65, "Transcoding video output")
                        try:
                            subprocess.run(
                                command, check=True, capture_output=True, text=True
                            )
                        except subprocess.CalledProcessError as error:
                            detail = (
                                str(error.stderr or error.stdout or "")
                                .strip()
                                .splitlines()
                            )
                            reason = (
                                detail[-1]
                                if detail
                                else "FFmpeg returned a non-zero exit status."
                            )
                            raise RuntimeError(
                                f"Video transcoding with {video_encoder} failed: {reason}"
                            ) from error
                    else:
                        progress(0.65, "Copying prepared media output")
                        shutil.copy2(working_video, render_destination)
                    os.replace(render_destination, destination)
                    progress(0.9, "Rendered media output ready")
                finally:
                    if render_destination.exists():
                        render_destination.unlink()
                    if temporary_video is not None and temporary_video.exists():
                        temporary_video.unlink()
                subtitle_track_metadata = [
                    {
                        "artifact_id": item.id,
                        "language": str(
                            (item.metadata_json or {}).get("language") or "und"
                        ),
                        "title": str(
                            (item.metadata_json or {}).get("title") or "Subtitles"
                        ),
                        "default": bool((item.metadata_json or {}).get("default")),
                    }
                    for item in video_track_artifacts
                ]
                produced.append(
                    # Register after the potentially long render so 100% is
                    # reserved for a durable, discoverable output.
                    self.artifacts.register(
                        destination,
                        kind="export",
                        role="export",
                        session_id=session_id,
                        parent_ids=audio_parent_ids
                        + [item.id for item in selected_subtitles]
                        + [item.id for item in video_track_artifacts],
                        settings=settings,
                        metadata={
                            "audio_mode": audio_mode,
                            "subtitle_mode": subtitle_mode,
                            "video_resolution": output_video_resolution,
                            "video_transcoded": video_transcode,
                            "video_encoder": video_encoder if video_transcode else None,
                            "audio_bitrate": (
                                video_audio_bitrate
                                if audio_mode in {"dubbed", "mixed"}
                                or render_audio_codec == "aac"
                                else None
                            ),
                            "subtitle_tracks": subtitle_track_metadata,
                            "mix": {
                                "source_gain_db": settings.get(
                                    "mix_source_gain_db", 0.0
                                ),
                                "voice_gain_db": settings.get("mix_voice_gain_db", 0.0),
                                "voice_lufs": settings.get("mix_voice_lufs", -16.0),
                                "ducking": settings.get("mix_ducking", "strong"),
                                "attack_ms": settings.get("mix_attack_ms", 25),
                                "release_ms": settings.get("mix_release_ms", 350),
                            }
                            if audio_mode == "mixed"
                            else None,
                        },
                    )
                )
            else:
                # Preserve the historical behavior for SRT/audio-only sessions:
                # a media export falls back to managed standalone artifacts.
                for index, item in enumerate(selected_subtitles, start=1):
                    progress(
                        0.35 + 0.3 * ((index - 1) / max(1, len(selected_subtitles))),
                        f"Writing standalone subtitle {index} of {len(selected_subtitles)}",
                    )
                    _artifact, item_path = self._resolve_input(item.id)
                    track_name, language, title, _default = track_details(item)
                    destination = _next_available_path(
                        output_dir / f"{export_name}_{track_name}.srt"
                    )
                    shutil.copy2(item_path, destination)
                    produced.append(
                        self.artifacts.register(
                            destination,
                            kind="srt",
                            role=f"export_subtitle_{track_name}",
                            session_id=session_id,
                            parent_ids=[item.id],
                            settings=settings,
                            metadata={
                                "language": language,
                                "title": title,
                                "source_role": (item.metadata_json or {}).get(
                                    "source_role"
                                ),
                            },
                        )
                    )
                    progress(
                        0.35 + 0.3 * (index / len(selected_subtitles)),
                        f"Exported standalone subtitle {index} of {len(selected_subtitles)}",
                    )
                if upload_audio and audio_mode == "source":
                    progress(0.7, "Copying source audio")
                    _source_record, source_audio_path = self._resolve_input(
                        upload_audio.id
                    )
                    destination = _next_available_path(
                        output_dir / f"{export_name}{source_audio_path.suffix.lower()}"
                    )
                    shutil.copy2(source_audio_path, destination)
                    produced.append(
                        self.artifacts.register(
                            destination,
                            kind="export",
                            role="export_source_audio",
                            session_id=session_id,
                            parent_ids=[upload_audio.id],
                            settings=settings,
                        )
                    )
                elif dubbing_audio and audio_mode != "source":
                    _artifact, item_path = self._resolve_input(dubbing_audio.id)
                    if upload_audio and audio_mode == "mixed":
                        _source_record, source_audio_path = self._resolve_input(
                            upload_audio.id
                        )
                        output_format = str(settings.get("format") or "wav").lower()
                        if output_format not in {"wav", "mp3", "opus", "flac"}:
                            output_format = "wav"
                        destination = _next_available_path(
                            output_dir / f"{export_name}_mixed.{output_format}"
                        )
                        ffmpeg_executable = str(
                            os.environ.get("PANDRATOR_FFMPEG_EXE")
                            or shutil.which("ffmpeg")
                            or "ffmpeg"
                        )
                        command = build_mix_audio_command(
                            str(source_audio_path),
                            str(item_path),
                            str(destination),
                            source_gain_db=settings.get("mix_source_gain_db", 0.0),
                            voice_gain_db=settings.get("mix_voice_gain_db", 0.0),
                            voice_lufs=settings.get("mix_voice_lufs", -16.0),
                            ducking=str(settings.get("mix_ducking") or "strong"),
                            attack_ms=settings.get("mix_attack_ms", 25),
                            release_ms=settings.get("mix_release_ms", 350),
                            ffmpeg_executable=ffmpeg_executable,
                        )
                        progress(0.7, "Mixing standalone audio")
                        subprocess.run(
                            command, check=True, capture_output=True, text=True
                        )
                        parents = [upload_audio.id, dubbing_audio.id]
                        role = "export_mixed_audio"
                    else:
                        progress(0.7, "Copying generated speech audio")
                        destination = _next_available_path(
                            output_dir / f"{export_name}{item_path.suffix.lower()}"
                        )
                        shutil.copy2(item_path, destination)
                        parents = [dubbing_audio.id]
                        role = f"export_{dubbing_audio.role}"
                    produced.append(
                        self.artifacts.register(
                            destination,
                            kind="export",
                            role=role,
                            session_id=session_id,
                            parent_ids=parents,
                            settings=settings,
                        )
                    )
                    progress(0.92, "Standalone audio export ready")
                if not produced:
                    raise ValueError(
                        "No subtitle or audio artifact is available to export."
                    )
        with self.database.session() as session:
            for produced_artifact in produced:
                managed = session.get(Artifact, produced_artifact.id)
                if managed is None:
                    continue
                managed.metadata_json = {
                    **dict(managed.metadata_json or {}),
                    "output_settings": output_settings_snapshot,
                }
        progress(
            0.98,
            f"Registered {len(produced)} export artifact{'s' if len(produced) != 1 else ''}",
        )
        progress(1.0, "Export ready")
        return {
            "artifact_ids": [item.id for item in produced],
            "paths": [item.relative_path for item in produced],
        }

    def apply_pdf_edits(self, payload, progress, cancel_event):
        from .pdf_editor import PdfEditPlan, apply_pdf_edit_plan

        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        if source_path.suffix.lower() != ".pdf":
            raise ValueError("PDF edit jobs require a PDF source artifact.")
        session_id = (
            str(payload.get("session_id") or source_artifact.session_id or "") or None
        )
        output_dir = (
            self._session_dir(session_id) if session_id else self.paths.artifacts
        )
        output_path = output_dir / f"{source_path.stem}_edited.pdf"
        suffix = 2
        while output_path.exists():
            output_path = output_dir / f"{source_path.stem}_edited_{suffix}.pdf"
            suffix += 1
        progress(0.1, "Validating PDF edit plan")
        plan = PdfEditPlan.from_value(dict(payload.get("plan") or {}))
        if cancel_event.is_set():
            return {}
        destination, manifest, provenance = apply_pdf_edit_plan(
            source_path,
            output_path,
            plan,
            parent_artifact_id=source_artifact.id,
        )
        progress(0.85, "Registering edited PDF")
        output_artifact = self.artifacts.register(
            destination,
            kind="pdf",
            role="pdf_edited",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            metadata={
                "provenance_manifest": self.paths.relative_managed_path(manifest)
            },
        )
        manifest_artifact = self.artifacts.register(
            manifest,
            kind="json",
            role="provenance",
            session_id=session_id,
            parent_ids=[source_artifact.id, output_artifact.id],
        )
        progress(1.0, "Edited PDF ready")
        return {
            "artifact_id": output_artifact.id,
            "manifest_artifact_id": manifest_artifact.id,
            "page_count": provenance["output"]["page_count"],
        }

    def export_session_bundle(self, payload, progress, cancel_event):
        from .bundles import SessionBundleService

        session_id = str(payload.get("session_id") or "")
        record = self._session_record(session_id)
        destination = (
            self._session_dir(session_id)
            / "exports"
            / f"{record.storage_key}.pandrator-session"
        )
        progress(0.02, "Preparing session bundle export")
        result = SessionBundleService(self.database, self.paths).export_bundle(
            session_id,
            destination,
            include_sources=bool(payload.get("include_sources", True)),
            progress_callback=_scaled_progress_callback(progress, 0.02, 0.95),
        )
        if cancel_event.is_set():
            return {}
        progress(0.97, "Registering session bundle")
        artifact = self.artifacts.register(
            destination, kind="bundle", role="session_bundle", session_id=session_id
        )
        progress(1.0, "Session bundle ready")
        return {**result, "artifact_id": artifact.id}

    def import_session_bundle(self, payload, progress, cancel_event):
        from .bundles import SessionBundleService

        _artifact, source = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        progress(0.02, "Preparing session bundle import")
        if cancel_event.is_set():
            return {}
        result = SessionBundleService(self.database, self.paths).import_bundle(
            source,
            name=payload.get("name"),
            progress_callback=_scaled_progress_callback(progress, 0.02, 0.98),
        )
        progress(1.0, "Session imported")
        return result
