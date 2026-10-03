"""Worker adapters that run existing Pandrator engines without Qt."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pandrator.logic.dubbing.settings import normalize_correction_style
from pandrator.logic.dubbing.srt_utils import (
    split_speaker_label,
    timing_context_mode_from_settings,
)
from pandrator.logic.dubbing.transcript_normalization import load_transcript
from pandrator.runtime import DataPaths

from .artifact_selection import canonical_stage_key, selected_artifacts
from .artifacts import ArtifactService, sha256_file
from .audio_verification import add_run_rms_warning, run_rms_outliers, verify_audio
from .credentials import (
    auxiliary_credential_key,
    database_reference,
    hydrate_stt_settings,
    hydrate_tts_settings,
    redact_inline_secrets,
    resolve_secret_reference,
)
from .database import Database
from .export_contract import export_requires_generation_assembly
from .generation_plan_store import GenerationPlanStoreContext
from .generation_plan_store import store_generation_plan as _store_generation_plan_impl
from .jobs import JobQueue
from .logical_passages import (
    attach_passages,
    load_timing_reference,
    map_output_passages,
    passage_review_metadata,
    passage_srt,
    pin_raw_source_passages,
    same_timing_language,
)
from .models import (
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    OutputAssembly,
    Segment,
    SegmentLineage,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    TimedWord,
    UsageEvent,
    Voice,
    new_id,
    utcnow,
)
from .source_resolution import resolve_primary_source
from .workflow_export import export as _export
from .workflow_generation_automatic import AutomaticGenerationContext
from .workflow_generation_automatic import generate_audio as _automatic_generate_audio
from .workflow_generation_binding import GenerationBindingContext
from .workflow_generation_binding import generation_language as _binding_generation_language
from .workflow_generation_binding import (
    generation_source_for_plan_refresh as _binding_generation_source_for_plan_refresh,
)
from .workflow_generation_binding import (
    materialize_subtitle_generation_plan as _binding_materialize_subtitle_generation_plan,
)
from .workflow_generation_binding import refresh_generation_plan as _binding_refresh_generation_plan
from .workflow_generation_binding import (
    subtitle_generation_records as _binding_subtitle_generation_records,
)
from .workflow_generation_binding import subtitle_speaker_map as _binding_subtitle_speaker_map
from .workflow_generation_execution import GenerationExecutionContext
from .workflow_generation_execution import run_generation as _execution_run_generation
from .workflow_generation_finalization import finalize_run_audio_verification
from .workflow_generation_protocols import Progress
from .workflow_generation_start import GenerationStartContext
from .workflow_generation_start import run_reviewable_generation as _start_run_reviewable_generation
from .workflow_inputs import workflow_transformations
from .workflow_output_assembly import (
    assemble_generation_output as _assemble_generation_output,
)
from .workflow_output_context import OutputWorkflowContext
from .workflow_source import SourceWorkflowContext
from .workflow_source import clean_source as _source_clean_source
from .workflow_source import download_source_url as _source_download_source_url
from .workflow_source import (
    prepare_source_cleaning_dispatch as _source_prepare_source_cleaning_dispatch,
)
from .workflow_source import prepare_text as _source_prepare_text
from .workflow_source import reuse_source as _source_reuse_source
from .workflow_voice import VOICE_CLEANUP_INPUT_SAMPLE_RATE as VOICE_CLEANUP_INPUT_SAMPLE_RATE
from .workflow_voice import (
    VOICE_NOISE_REDUCTION_DEEPFILTERNET2 as VOICE_NOISE_REDUCTION_DEEPFILTERNET2,
)
from .workflow_voice import VOICE_NOISE_REDUCTION_NONE as VOICE_NOISE_REDUCTION_NONE
from .workflow_voice import VOICE_NOISE_REDUCTION_OPTIONS as VOICE_NOISE_REDUCTION_OPTIONS
from .workflow_voice import VoiceWorkflowContext
from .workflow_voice import _managed_provider_voice_id as _managed_provider_voice_id
from .workflow_voice import _provider_endpoint_fingerprint as _provider_endpoint_fingerprint
from .workflow_voice import convert_with_rvc as _voice_convert_with_rvc
from .workflow_voice import normalize_voice_recording as _voice_normalize_voice_recording
from .workflow_voice import publish_voice as _voice_publish_voice
from .workflow_voice import train_xtts as _voice_train_xtts
from .workflow_voice import transcribe_voice as _voice_transcribe_voice
from .workflow_voice import unpublish_voice as _voice_unpublish_voice
from .workflow_voice import upload_rvc_model as _voice_upload_rvc_model

if TYPE_CHECKING:
    from .manager_proxy import LocalManagerProxy
    from .subtitle_evidence import SubtitleEvidenceService
    from .tts_providers import TtsProviderRegistry


logger = logging.getLogger(__name__)

CLAUSE_PAUSE_RATIO = 1 / 3
GENERATION_SEGMENT_POLICY_VERSION = 5


def _media_edit_token_count(text: str) -> int:
    """Count authoritative lexical tokens using media-edit normalization."""

    return sum(
        bool(
            "".join(
                character
                for character in unicodedata.normalize("NFKC", token).casefold()
                if character.isalnum()
            )
        )
        for token in re.findall(r"\S+", text)
    )


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
            "correction_style": normalize_correction_style(
                settings.get("correction_style")
            ),
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
            else settings.get("subtitle_merge_threshold", 1500)
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
        integer_setting("speech_block_max_internal_gap_ms", 4000),
    )
    return (
        min_chars,
        max_chars,
        merge_threshold,
        continuation_threshold,
        max_internal_gap,
    )


def _speech_block_generation_mode(settings: dict[str, Any]) -> str:
    """Planning mode for dubbing speech blocks (passage-first by default)."""

    from pandrator.logic.dubbing.passage_regroup import normalize_generation_mode

    try:
        return normalize_generation_mode(settings.get("speech_block_generation_mode"))
    except ValueError:
        return "passage"


def _voiceover_second_pass(
    settings_snapshot: dict[str, Any],
    *,
    operation: str,
    has_selected_ids: bool,
    workflow_kind: str,
) -> str | None:
    """Select the optional post-generation pass: repair, regroup, or neither.

    Thin wrapper over the logic-layer helper so the runner, status
    reporting, and tests share one wiring point.
    """

    from pandrator.logic.dubbing.passage_regroup import select_second_pass

    return select_second_pass(
        settings_snapshot,
        operation=operation,
        has_selected_ids=has_selected_ids,
        workflow_kind=workflow_kind,
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
        "audiobook_chunking": settings.get("audiobook_chunking"),
        "max_sentence_length": settings.get("max_sentence_length"),
        "audiobook_chunk_budget": settings.get("_audiobook_chunk_budget"),
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
    boundary = record.get("speech_boundary_after")
    if boundary == "continuation":
        return 0
    if boundary == "dialogue_turn":
        return sentence_silence
    if boundary in {"scene", "chapter", "paragraph"}:
        is_paragraph = True
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
        from .media_edit import MediaEditService

        self.media_edit = MediaEditService(database, self.artifacts, self._session_dir)
        if tts_providers is None:
            from .tts_providers import TtsProviderRegistry

            tts_providers = TtsProviderRegistry()
        self.tts_providers = tts_providers
        self.manager_bridge = manager_bridge
        self.jobs = jobs or JobQueue(database)
        from .quick_transcription import QuickTranscriptionService

        self.quick_transcriptions = QuickTranscriptionService(database, paths, self.jobs)
        if subtitle_evidence is None:
            from .subtitle_evidence import SubtitleEvidenceService
            from .workspace_settings import WorkspaceSettingsService

            subtitle_evidence = SubtitleEvidenceService(
                database,
                self.artifacts,
                self.jobs,
                WorkspaceSettingsService(database),
                self._session_dir,
                paths,
            )
        self.subtitle_evidence = subtitle_evidence
        self._audio_cpp_voice_ref_cache: OrderedDict[str, str] = OrderedDict()
        self._audio_cpp_voice_ref_cache_lock = threading.Lock()
        from .job_handler_domains import build_workflow_handler_registry

        self.handler_registry = build_workflow_handler_registry(self)
        self.handler_registry.register(
            "transcription.quick", self.quick_transcriptions.run, domain="transcription"
        )

    def run_subtitle_evidence(self, payload, progress, cancel_event):
        """Delegate the durable subtitle-evidence job to its service."""
        if self.subtitle_evidence is None:
            raise RuntimeError("Subtitle evidence service is not configured.")
        return self.subtitle_evidence.run_request(
            str(payload.get("evidence_id") or ""), progress, cancel_event,
            **({"force_refresh": True} if payload.get("force_refresh", False) else {}),
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
            from .generation_scheduling import interrupted_run_id, release_interrupted_run

            if (
                child_run is None
                or interrupted_run_id(child_run) != source_run_id
                or not child_run.resume_source_on_completion
                or source_run is None
                or child_run.session_id != source_run.session_id
            ):
                return None
            return release_interrupted_run(session, self.jobs, child_run)

    @staticmethod
    def _verification_metadata(
        audio,
        synthesized_text: str,
        settings: dict[str, Any],
    ) -> dict[str, Any] | None:
        return verify_audio(audio, synthesized_text, settings)

    def _finalize_run_audio_verification(self, run_id: str) -> int:
        """Check the latest available take per segment within this output run."""
        return finalize_run_audio_verification(
            self.database,
            run_id,
            find_outliers=run_rms_outliers,
            add_warning=add_run_rms_warning,
        )

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

        from .voice_library import resolve_audio_cpp_voice_reference

        with self.database.session() as session:
            match = resolve_audio_cpp_voice_reference(session, self.paths, prepared)

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
        generation_prompt = str(settings.get("generation_prompt") or "").strip()
        seed = settings.get("seed")
        if seed is None:
            seed = settings.get("audio_cpp_seed")
        preview_identity = {
            "service_id": service_id,
            "service_adapter": str(settings.get("preview_adapter") or ""),
            "model": str(settings.get("model") or ""),
            "voice": str(settings.get("voice") or ""),
            "language": str(settings.get("language") or ""),
            "preview_text": text,
            "generation_prompt": generation_prompt,
            "seed": seed if seed is not None else None,
        }
        # A preview is an auditioned source artifact, not a cache entry.  Keep
        # each job on its own immutable path so regenerating the same inputs
        # cannot replace audio that a promotion job has already selected.
        preview_job_id = str(payload.get("_job_id") or "").strip() or new_id()
        preview_key = hashlib.sha256(
            json.dumps(
                {**preview_identity, "preview_job_id": preview_job_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        target_dir = self.paths.artifacts / "tts-previews"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{preview_key}.wav"
        with target.open("wb") as output:
            audio.export(output, format="wav")
        artifact = self.artifacts.register(
            target,
            kind="audio",
            role="tts_voice_preview",
            settings=_secret_free_tts_settings(settings),
            metadata={
                **preview_identity,
                "preview_job_id": preview_job_id,
                "service": settings.get("service"),
                "generation_settings": redact_inline_secrets(
                    _secret_free_tts_settings(settings)
                ),
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

    def _source_workflow_context(self) -> SourceWorkflowContext:
        return SourceWorkflowContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            _resolve_input=self._resolve_input,
            _session_dir=self._session_dir,
            _operation_dir=self._operation_dir,
            _session_record=self._session_record,
            _store_generation_plan=self._store_generation_plan,
            _validate_download_url=self._validate_download_url,
            _scaled_progress_callback=_scaled_progress_callback,
            _fraction_message_callback=_fraction_message_callback,
            _source_cleaning_progress_callback=_source_cleaning_progress_callback,
        )

    def download_source_url(self, payload, progress, cancel_event):
        return _source_download_source_url(self._source_workflow_context(), payload, progress, cancel_event)

    def reuse_source(self, payload, progress, cancel_event):
        return _source_reuse_source(self._source_workflow_context(), payload, progress, cancel_event)

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
                or candidate.role
                not in {
                    "media_edit_subtitles",
                    "transcription",
                    "correction",
                    "upload",
                }
                or Path(candidate.relative_path).suffix.lower() != ".srt"
                or (
                    candidate.session_id != session_id
                    and candidate.id not in attached_ids
                )
            ):
                return None
            session.expunge(candidate)
            return candidate

    def _matches_active_media_edit_revision(
        self, session_id: str, artifact: Artifact | None
    ) -> bool:
        if artifact is None or artifact.role != "media_edit_subtitles":
            return False
        with self.database.session() as session:
            revision = session.scalar(
                select(MediaEditPlanRevision)
                .join(
                    MediaEditPlan,
                    MediaEditPlan.active_revision_id == MediaEditPlanRevision.id,
                )
                .where(MediaEditPlan.session_id == session_id)
            )
        from .workflows import WorkflowService

        return WorkflowService._matches_active_media_edit_revision(artifact, revision)

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
                else (
                    ("media_edit_subtitles",)
                    if workflow_kind == "media_edit"
                    else ("transcription", "upload")
                )
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
        if workflow_kind == "media_edit" and generation_parent in {
            "source",
            "media_edit",
        }:
            return ("media_edit_subtitles",)
        return {
            "translation": ("translation",),
            "correction": ("correction",),
            "source": ("transcription", "upload"),
        }.get(generation_parent, default_roles)

    def continue_workflow(self, payload, progress, cancel_event):
        """Run only missing/stale included prerequisites, then the requested outcome stage."""
        from .workflows import AUDIOBOOK_STAGES, DUBBING_STAGES, MEDIA_EDIT_STAGES

        session_id = str(payload.get("session_id") or "")
        target_key = str(payload.get("target_stage") or "generate_audio")
        expected_speech_revision = str(payload.get("speech_plan_revision_id") or "")
        if expected_speech_revision:
            with self.database.session() as session:
                current_plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
                if current_plan is None or current_plan.active_revision_id != expected_speech_revision:
                    from .settings_policy import RevisionConflict

                    raise RevisionConflict("The speech plan changed after this workflow was queued. Review the current revision and start again.")
        record = self._session_record(session_id)
        definitions = (
            AUDIOBOOK_STAGES
            if record.workflow_kind == "audiobook"
            else MEDIA_EDIT_STAGES
            if record.workflow_kind == "media_edit"
            else DUBBING_STAGES
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
            is_srt_source = filename.endswith((".srt", ".vtt"))
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
            transformations = workflow_transformations(session, session_id, outcome, self.database)
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
        if record.workflow_kind == "media_edit" and target_key not in {
            "transcribe",
            "edit_media",
        }:
            rendered_subtitles = self._latest_stage_input(
                session_id, ("media_edit_subtitles",)
            )
            if not self._matches_active_media_edit_revision(
                session_id, rendered_subtitles
            ):
                raise ValueError(
                    "Review and render the media edit before running downstream stages."
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
            raw_stage_settings = stage_settings.get(definition.key)
            settings: dict[str, Any] = (
                dict(raw_stage_settings)
                if isinstance(raw_stage_settings, dict)
                else {}
            )
            if definition.key == target_key:
                settings = {**settings, **direct_settings}
            if definition.key == "generate_audio":
                settings["llm_tts_optimization"] = bool(
                    transformations.get("llm_tts_optimization")
                )
            deferred_export_assembly = (
                definition.key == "export"
                and export_requires_generation_assembly(
                    workflow_kind=record.workflow_kind,
                    settings=settings,
                )
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
            if (
                definition.prerequisite_roles
                and source is None
                and not deferred_export_assembly
            ):
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
                if "display_subtitle_snapshot" in payload:
                    handler_payload["display_subtitle_snapshot"] = payload[
                        "display_subtitle_snapshot"
                    ]
            if definition.key == "export" and deferred_export_assembly:
                result = self.export_variant(
                    handler_payload,
                    stage_progress,
                    cancel_event,
                )
            elif definition.key == "generate_audio":
                handler_payload["speech_plan_revision_id"] = expected_speech_revision or None
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
            if workflow_kind != "media_edit" and not is_srt_source:
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
        from .settings_policy import adapt_runtime_settings
        from .workflows import AUDIOBOOK_STAGES, DUBBING_STAGES, MEDIA_EDIT_STAGES
        from .workspace_settings import WorkspaceSettingsService

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
            AUDIOBOOK_STAGES
            if record.workflow_kind == "audiobook"
            else MEDIA_EDIT_STAGES
            if record.workflow_kind == "media_edit"
            else DUBBING_STAGES
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
            transformations = workflow_transformations(session, session_id, outcome, self.database)
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

    def _current_media_edit_transcript(self, session_id: str) -> Artifact | None:
        """Return the current caption attachment for a media-edit session."""

        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None or record.workflow_kind != "media_edit":
                return None
            artifact = session.scalar(
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
            if artifact is not None:
                session.expunge(artifact)
            return artifact

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

    def _generation_binding_context(self) -> GenerationBindingContext:
        return GenerationBindingContext(
            database=self.database,
            artifacts=self.artifacts,
            _session_record=self._session_record,
            _resolve_input=self._resolve_input,
            _operation_dir=self._operation_dir,
            _latest_stage_input=self._latest_stage_input,
            _usable_language=self._usable_language,
            _subtitle_speaker_map=self._subtitle_speaker_map,
            _subtitle_generation_records=self._subtitle_generation_records,
            _store_generation_plan=self._store_generation_plan,
            _generation_source_for_plan_refresh=self._generation_source_for_plan_refresh,
            _generation_language=self._generation_language,
            _materialize_subtitle_generation_plan=self._materialize_subtitle_generation_plan,
            _structured_speaker=_structured_speaker,
            _speech_block_settings=_speech_block_settings,
            _speech_block_generation_mode=_speech_block_generation_mode,
            _next_available_path=_next_available_path,
            _generation_segmentation_settings=_generation_segmentation_settings,
            _logger=logger,
        )

    def _generation_language(
        self,
        session_id: str,
        source_artifact: Artifact,
        settings: dict[str, Any],
    ) -> str:
        return _binding_generation_language(
            self._generation_binding_context(), session_id, source_artifact, settings
        )

    def _subtitle_speaker_map(
        self,
        artifact: Artifact,
        source_path: Path | None = None,
    ) -> dict[int, str]:
        return _binding_subtitle_speaker_map(
            self._generation_binding_context(), artifact, source_path
        )

    def _subtitle_generation_records(
        self,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        language: str,
        session_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, Artifact]:
        return _binding_subtitle_generation_records(
            self._generation_binding_context(), source_artifact, source_path, settings, language, session_id=session_id
        )

    def _materialize_subtitle_generation_plan(
        self,
        session_id: str,
        source_artifact: Artifact,
        source_path: Path,
        settings: dict[str, Any],
        language: str,
    ) -> str:
        return _binding_materialize_subtitle_generation_plan(
            self._generation_binding_context(), session_id, source_artifact, source_path, settings, language
        )

    def _generation_source_for_plan_refresh(
        self,
        session_id: str,
    ) -> Artifact | None:
        return _binding_generation_source_for_plan_refresh(
            self._generation_binding_context(), session_id
        )

    def refresh_generation_plan(
        self,
        session_id: str,
        resolved_snapshot: dict[str, Any],
    ) -> str | None:
        return _binding_refresh_generation_plan(
            self._generation_binding_context(), session_id, resolved_snapshot
        )

    @staticmethod
    def _resolve_run_passage_settings(
        session_id: str,
        payload_settings: dict[str, Any] | None,
        *,
        database=None,
    ) -> tuple[dict[str, int], int]:
        """Resolve the exact effective settings one run constructs with.

        Payload-provided values (resolved job settings or per-run overrides)
        win; otherwise the live session effective settings are read once.
        Invalid values raise instead of silently falling back to defaults.
        """
        from pandrator.logic.dubbing.source_passage_settings import (
            RUNTIME_TO_WEB,
            from_runtime_keys,
            normalize_source_passage_settings,
        )

        from .workspace_settings import WorkspaceSettingsService

        payload = dict(payload_settings or {})
        has_nested = isinstance(payload.get("source_passages"), dict)
        has_prefixed = any(key in payload for key in RUNTIME_TO_WEB)
        if has_nested or has_prefixed:
            effective = from_runtime_keys(payload)
            try:
                revision = int(payload.get("source_passage_settings_revision") or 0)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "source_passage_settings_revision must be an integer."
                ) from error
            if revision == 0 and database is not None:
                # Provenance only: values stay exactly as the payload resolved
                # them; the live revision is recorded when readable.
                try:
                    with database.session() as session:
                        live = WorkspaceSettingsService(database).get_in_session(
                            session, session_id, "source_passages"
                        )
                    revision = int(live.get("revision") or 0)
                except (KeyError, TypeError, ValueError):
                    revision = 0
            return effective, revision
        if database is None:
            raise ValueError(
                "Source-passage settings are unavailable for this session."
            )
        with database.session() as session:
            snapshot = WorkspaceSettingsService(database).get_in_session(
                session, session_id, "source_passages"
            )
        return (
            normalize_source_passage_settings(snapshot["effective"]),
            int(snapshot.get("revision") or 0),
        )

    def _prepare_passage_input(
        self,
        artifact: Artifact,
        source_path: Path,
        directory: Path,
        *,
        source_passage_settings: dict[str, Any] | None = None,
        source_passage_settings_revision: int | None = None,
    ) -> tuple[Path, list[dict[str, Any]], dict[int, str]]:
        with self.database.session() as session:
            managed = session.get(Artifact, artifact.id)
            if (
                managed is not None
                and managed.content_hash
                and sha256_file(source_path) != managed.content_hash
            ):
                raise ValueError(
                    "The subtitle file changed after its revision was saved. Import the updated file before processing it."
                )
            if source_passage_settings is None:
                # Resolved once here so construction matches the run ledger;
                # never a second live read mid-job.
                effective, revision = self._resolve_run_passage_settings(
                    managed.session_id if managed is not None else "",
                    None,
                    database=self.database,
                )
            else:
                from pandrator.logic.dubbing.source_passage_settings import (
                    normalize_source_passage_settings,
                )

                effective = normalize_source_passage_settings(
                    source_passage_settings
                )
                revision = (
                    0
                    if source_passage_settings_revision is None
                    else int(source_passage_settings_revision)
                )
            rows = (
                pin_raw_source_passages(
                    session,
                    managed,
                    effective=effective,
                    settings_revision=revision,
                )
                if managed else []
            )
        if not rows:
            return source_path, [], self._subtitle_speaker_map(artifact, source_path)
        path = directory / f"{source_path.stem}.passages.srt"
        path.write_text(passage_srt(rows), encoding="utf-8")
        return (
            path,
            rows,
            {
                index + 1: str(row.get("speaker") or "")
                for index, row in enumerate(rows)
                if row.get("speaker")
            },
        )

    def _passage_display_settings(self, session_id: str) -> dict[str, Any]:
        from .settings_policy import adapt_runtime_settings
        from .workspace_settings import WorkspaceSettingsService

        with self.database.session() as session:
            effective = WorkspaceSettingsService(self.database).get_in_session(
                session,
                session_id,
                "subtitles",
            )["effective"]
        return adapt_runtime_settings("subtitles", effective)

    def _source_passage_run_ledger(
        self,
        session_id: str,
        requested_settings: dict[str, Any],
        *,
        effective: dict[str, int] | None = None,
        settings_revision: int | None = None,
    ) -> dict[str, Any]:
        """Pin effective source-passage settings into a run ledger (no writes).

        The caller resolves ``(effective, revision)`` once via
        :meth:`_resolve_run_passage_settings` and shares it with construction,
        so the ledger always describes what was actually built. Existing
        explicit keys (e.g. per-run overrides) are preserved.
        """
        from pandrator.logic.dubbing.source_passage_settings import (
            SOURCE_PASSAGE_POLICY_VERSION,
            normalize_source_passage_settings,
            source_passage_settings_hash,
            to_runtime_keys,
        )

        if effective is None or settings_revision is None:
            effective, settings_revision = self._resolve_run_passage_settings(
                session_id, requested_settings, database=self.database
            )
        live_effective = normalize_source_passage_settings(effective)
        live_revision = int(settings_revision)
        ledger = dict(requested_settings)
        # Flattened payloads carry only the prefixed runtime keys (never bare
        # min_chars-style names) plus the nested web-key snapshot.
        for key, value in to_runtime_keys(live_effective).items():
            ledger.setdefault(key, value)
        ledger.setdefault(
            "source_passage_policy_version", SOURCE_PASSAGE_POLICY_VERSION
        )
        ledger.setdefault(
            "source_passage_settings_hash",
            source_passage_settings_hash(live_effective),
        )
        ledger.setdefault("source_passage_settings_revision", live_revision)
        ledger.setdefault("source_passages", deepcopy(live_effective))
        return ledger

    def _render_passage_output(
        self,
        source: Artifact,
        result: Any,
        inputs: list[dict[str, Any]],
        settings: dict[str, Any],
        language: str,
    ) -> tuple[list[dict[str, Any]] | None, dict[int, str]]:
        from pandrator.logic.dubbing.subtitle_projection import project_subtitle_display

        raw_rows = getattr(result, "logical_passages", None)
        if not inputs or raw_rows is None:
            return None, getattr(result, "speaker_by_subtitle", {})
        rows = map_output_passages(raw_rows, inputs)
        with self.database.session() as session:
            managed = session.get(Artifact, source.id)
            words, reference = (
                load_timing_reference(session, managed) if managed else ([], None)
            )
        display = project_subtitle_display(
            rows,
            {**dict(settings.get("_logical_passage_display") or {}), "subtitle_language": language},
            timing_words=words,
            match_source_words=same_timing_language(
                language, (reference or {}).get("language")
            ),
        )
        Path(result.output_path).write_text(passage_srt(display), encoding="utf-8")
        return rows, {
            index + 1: str(row.get("speaker") or "")
            for index, row in enumerate(display)
            if row.get("speaker")
        }

    def _store_srt_document(
        self,
        session_id: str,
        artifact: Artifact,
        stage: str,
        *,
        language: str | None = None,
        parent_artifact: Artifact | None = None,
        speaker_overrides: dict[int, str] | None = None,
        logical_passages: list[dict[str, Any]] | None = None,
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
            speech_markup = (artifact.metadata_json or {}).get("speech_markup") or {}
            if speech_markup:
                from .generation_cast_runtime import remap_markup
                from .generation_controls import get_generation_controls
                characters = get_generation_controls(session, session_id)["characters"]
            for ordinal, item in enumerate(resolved_segments):
                cue_markup = speech_markup.get(str(ordinal + 1))
                child = Segment(
                    revision_id=revision.id,
                    ordinal=ordinal,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    text=item.text,
                    speaker=item.speaker or None,
                    metadata_json={
                        "speaker_source": speaker_sources[ordinal],
                        **({"speech_xml": remap_markup(cue_markup, str(ordinal + 1), item.text, characters)} if cue_markup else {}),
                        **(
                            passage_review_metadata(
                                [
                                    row
                                    for row in logical_passages
                                    if min(item.end_ms, row["end_ms"])
                                    > max(item.start_ms, row["start_ms"])
                                ]
                            )
                            if logical_passages is not None
                            else {}
                        ),
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
            if logical_passages is not None and parent_artifact is not None:
                attach_passages(managed, logical_passages, source=parent_artifact)
                managed.metadata_json = {
                    **managed.metadata_json,
                    "uncertain_segment_count": sum(
                        child.metadata_json.get("review_state") == "uncertain"
                        for child in child_records
                    ),
                }
            return document.id, revision.id

    def _store_timed_words(
        self,
        revision_id: str,
        metadata_path: Path,
        *,
        segment_by_source_cue_id: dict[str, int] | None = None,
        segment_by_word_ordinal: dict[int, int] | None = None,
    ) -> int:
        transcript = load_transcript(metadata_path)
        canonical_words = transcript.words
        words = [
            {
                "text": word.text,
                "start_ms": word.start_ms,
                "end_ms": word.end_ms,
                "speaker": word.speaker or None,
                "confidence": word.confidence,
                "metadata": dict(word.metadata),
            }
            for word in canonical_words
        ]
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == revision_id)
                    .order_by(Segment.ordinal)
                ).all()
            )
            segments_by_ordinal = {segment.ordinal: segment for segment in segments}
            if segment_by_word_ordinal is not None and any(
                target not in segments_by_ordinal
                for target in segment_by_word_ordinal.values()
            ):
                raise ValueError("Timed-word ownership references an unknown segment ordinal.")
            assigned_speakers: dict[str, list[Any]] = {}
            for ordinal, word in enumerate(words):
                source_cue_id = str(
                    word["metadata"].get("source_cue_id") or ""
                )
                owner = None
                if segment_by_word_ordinal is not None:
                    owner_ordinal = segment_by_word_ordinal.get(ordinal)
                    if owner_ordinal is not None:
                        owner = segments_by_ordinal[owner_ordinal]
                elif segment_by_source_cue_id and source_cue_id:
                    owner_ordinal = segment_by_source_cue_id.get(source_cue_id)
                    owner = next(
                        (
                            segment
                            for segment in segments
                            if segment.ordinal == owner_ordinal
                        ),
                        None,
                    )
                if owner is None and segment_by_word_ordinal is None:
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
                if owner is not None and word["speaker"]:
                    assigned_speakers.setdefault(owner.id, []).append(canonical_words[ordinal])
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

            segment_speaker_candidates = [
                item for item in transcript.segments if item.speaker
            ]
            for segment in segments:
                if segment.start_ms is None or segment.end_ms is None:
                    continue
                if str((segment.metadata_json or {}).get("speaker_source") or "") == "model_reviewed":
                    continue
                speaker_candidates = (
                    assigned_speakers.get(segment.id, [])
                    if words
                    else segment_speaker_candidates
                )
                if not speaker_candidates:
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

    def run_speech_preparation(self, payload, progress, cancel_event):
        from .speech_plan_preparation import run_speech_preparation

        return run_speech_preparation(self, payload, progress, cancel_event)

    def run_performance_analysis(self, payload, progress, cancel_event):
        """Analyse immutable speech blocks into a reviewable pSSML sidecar."""
        from .performance_plans import run_analysis

        return run_analysis(self, payload, progress, cancel_event)

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

    def _transcribe_media_edit_with_caption(
        self,
        *,
        session_id: str,
        source_artifact: Artifact,
        caption_artifact: Artifact,
        transcription_result: Any,
        submitted_settings: dict[str, Any],
        progress,
    ) -> dict[str, Any]:
        """Project ASR timing onto the authoritative attached captions."""

        from pandrator.logic.media_edit import (
            MediaWord,
            align_cues_to_words,
            caption_to_srt,
            media_cues_to_transcript,
            parse_caption_text,
        )

        raw_srt_path = Path(transcription_result.srt_path)
        raw_words_path = Path(transcription_result.word_timestamps_path)
        requested_language = str(submitted_settings.get("original_language") or submitted_settings.get("stt_language") or "auto")
        resolved_language = str(getattr(transcription_result, "resolved_language", "") or "")
        output_language = resolved_language if resolved_language.lower() not in {"", "auto", "und", "unknown"} else requested_language
        routing = deepcopy(dict(getattr(transcription_result, "routing", {}) or {}))
        isolation_metadata = None
        raw_payload: dict[str, Any] = {}
        try:
            raw_payload = json.loads(raw_words_path.read_text(encoding="utf-8"))
            supplied_isolation = (raw_payload.get("metadata") or {}).get("vocal_isolation")
            if isinstance(supplied_isolation, dict):
                isolation_metadata = {
                    key: deepcopy(value) for key, value in supplied_isolation.items()
                    if key in {"transcription_vocal_isolation", "vocal_isolation_model", "vocal_isolation_status", "original_audio_retained", "model_id", "family", "cli_family", "revision", "sha256", "size_bytes", "method", "status", "backend", "compute_backend", "requested_backend", "threads"}
                }
                if isolation_metadata.get("original_audio_retained"):
                    isolation_metadata["original_audio_retained"] = Path(str(isolation_metadata["original_audio_retained"]).replace("\\", "/")).name
        except (OSError, ValueError, TypeError, AttributeError):
            # Evidence registration precedes the existing transcript parse gate.
            pass
        if raw_payload:
            payload_metadata = raw_payload.get("metadata")
            if not isinstance(payload_metadata, dict):
                payload_metadata = {}
                raw_payload["metadata"] = payload_metadata
            payload_metadata["stt_routing"] = deepcopy(routing)
            payload_metadata["requested_language"] = requested_language
            if isolation_metadata is not None:
                payload_metadata["vocal_isolation"] = deepcopy(isolation_metadata)
            raw_words_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        raw_metadata = {
            "engine": transcription_result.engine,
            "model": transcription_result.engine,
            "model_quantization": str(
                submitted_settings.get("stt_model_quantization") or "f16"
            ),
            "compute_backend": transcription_result.compute_backend,
            "language": output_language,
            "resolved_language": output_language,
            "requested_language": requested_language,
            "requested_settings": redact_inline_secrets(submitted_settings),
            "stt_routing": routing,
            "alignment_role": "evidence",
            "source_artifact_id": source_artifact.id,
        }
        if isolation_metadata is not None:
            raw_metadata["vocal_isolation"] = isolation_metadata
        raw_srt_artifact = self.artifacts.register(
            raw_srt_path,
            kind="srt",
            role="transcription_evidence",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=submitted_settings,
            metadata=raw_metadata,
        )
        raw_words_artifact = self.artifacts.register(
            raw_words_path,
            kind="json",
            role="recognition_word_timestamps",
            session_id=session_id,
            parent_ids=[source_artifact.id, raw_srt_artifact.id],
            settings={
                **submitted_settings,
                "stt_engine": transcription_result.engine,
                "stt_compute_backend": transcription_result.compute_backend,
            },
            metadata={
                **raw_metadata,
                "transcription_evidence_artifact_id": raw_srt_artifact.id,
            },
        )

        _caption_record, caption_path = self.artifacts.resolve(caption_artifact.id)
        try:
            cues = parse_caption_text(
                caption_path.read_text(encoding="utf-8-sig")
            )
            transcript = load_transcript(raw_words_path)
            asr_words = tuple(
                MediaWord(
                    text=word.text,
                    start_ms=word.start_ms,
                    end_ms=word.end_ms,
                    confidence=word.confidence,
                )
                for word in transcript.words
            )
            aligned_cues = align_cues_to_words(cues, asr_words)
            word_count = sum(len(cue.words) for cue in aligned_cues)
            if not aligned_cues:
                raise ValueError(
                    "The attached transcript could not be aligned to ASR evidence."
                )
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise ValueError(
                "The attached transcript could not be parsed and aligned to ASR evidence."
            ) from error

        operation_dir = self._operation_dir(session_id, "transcribe")
        aligned_srt_path = operation_dir / "aligned-transcription.srt"
        aligned_json_path = operation_dir / "aligned-word-timestamps.json"
        caption_token_count = sum(
            _media_edit_token_count(cue.text) for cue in cues
        )
        matched_token_count = sum(
            min(
                _media_edit_token_count(cue.text),
                max(
                    0,
                    round(
                        _media_edit_token_count(cue.text)
                        * float(cue.timing_confidence or 0.0)
                    ),
                ),
            )
            for cue in aligned_cues
            if cue.timing_source == "asr_alignment" and cue.words
        )
        coverage = min(
            1.0,
            max(0.0, matched_token_count / max(1, caption_token_count)),
        )
        confidences = [
            float(cue.timing_confidence or 0.0)
            for cue in aligned_cues
        ]
        alignment_confidence = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
        if coverage < 0.5:
            raise ValueError(
                f"Aligned transcription coverage is {coverage:.6f}, below 0.5; "
                "no aligned transcription was promoted."
            )
        aligned_srt_path.write_text(
            caption_to_srt(aligned_cues), encoding="utf-8"
        )
        alignment_metadata = {
            "resolved_language": output_language,
            "stt_routing": deepcopy(routing),
            "requested_language": requested_language,
            "requested_settings": redact_inline_secrets(submitted_settings),
            "alignment_method": "asr_lexical_projection",
            "authoritative_transcript_artifact_id": caption_artifact.id,
            "raw_asr_srt_artifact_id": raw_srt_artifact.id,
            "raw_asr_word_timestamps_artifact_id": raw_words_artifact.id,
            "alignment_coverage": coverage,
            "coverage": coverage,
            "alignment_confidence": alignment_confidence,
            "confidence": alignment_confidence,
            "word_count": word_count,
            "source_artifact_id": source_artifact.id,
        }
        if isolation_metadata is not None:
            alignment_metadata["vocal_isolation"] = deepcopy(isolation_metadata)
        aligned_payload = media_cues_to_transcript(
            aligned_cues,
            language=output_language,
            source_format="media_edit_alignment",
            metadata=alignment_metadata,
        )
        aligned_json_path.write_text(
            json.dumps(aligned_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifact_metadata = {
            **raw_metadata,
            **alignment_metadata,
        }
        aligned_srt_artifact = self.artifacts.register(
            aligned_srt_path,
            kind="srt",
            role="transcription_alignment",
            session_id=session_id,
            parent_ids=[
                source_artifact.id,
                caption_artifact.id,
                raw_srt_artifact.id,
                raw_words_artifact.id,
            ],
            settings=submitted_settings,
            metadata=artifact_metadata,
        )
        aligned_words_artifact = self.artifacts.register(
            aligned_json_path,
            kind="json",
            role="word_timestamps",
            session_id=session_id,
            parent_ids=[
                source_artifact.id,
                caption_artifact.id,
                aligned_srt_artifact.id,
                raw_srt_artifact.id,
                raw_words_artifact.id,
            ],
            settings={
                **submitted_settings,
                "stt_engine": transcription_result.engine,
                "stt_compute_backend": transcription_result.compute_backend,
            },
            metadata=artifact_metadata,
        )
        language = output_language or None
        _document_id, revision_id = self._store_srt_document(
            session_id,
            aligned_srt_artifact,
            "transcription",
            language=language,
            parent_artifact=caption_artifact,
            speaker_overrides={
                index: cue.speaker
                for index, cue in enumerate(aligned_cues, start=1)
                if cue.speaker
            },
        )
        stored_word_count = self._store_timed_words(
            revision_id,
            aligned_json_path,
            segment_by_source_cue_id={
                cue.id: index for index, cue in enumerate(aligned_cues)
            },
        )
        # Promote only after native persistence succeeds.  A parse/alignment
        # or persistence failure therefore leaves evidence and a non-stage
        # candidate, never a selectable half-built transcription.
        aligned_srt_artifact = self.artifacts.register(
            aligned_srt_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[
                source_artifact.id,
                caption_artifact.id,
                raw_srt_artifact.id,
                raw_words_artifact.id,
            ],
            settings=submitted_settings,
            metadata=artifact_metadata,
        )
        progress(0.97, "Aligned transcription ready")
        return {
            "artifact_id": aligned_srt_artifact.id,
            "path": aligned_srt_artifact.relative_path,
            "word_timestamps_artifact_id": aligned_words_artifact.id,
            "word_timestamps_path": aligned_words_artifact.relative_path,
            "word_count": stored_word_count,
            "speaker_count": len(
                {cue.speaker.casefold() for cue in aligned_cues if cue.speaker}
            ),
            "revision_id": revision_id,
            "alignment_method": "asr_lexical_projection",
            "authoritative_transcript_artifact_id": caption_artifact.id,
            "raw_asr_srt_artifact_id": raw_srt_artifact.id,
            "raw_asr_word_timestamps_artifact_id": raw_words_artifact.id,
            "alignment_coverage": coverage,
            "coverage": coverage,
            "alignment_confidence": alignment_confidence,
            "confidence": alignment_confidence,
        }

    def _transcribe_media_edit_with_ctc(
        self,
        *,
        session_id: str,
        source_artifact: Artifact,
        source_path: Path,
        caption_artifact: Artifact,
        submitted_settings: dict[str, Any],
        runtime_settings: dict[str, Any],
        ffmpeg_executable: str,
        crispasr_executable: str,
        progress,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        """Align authoritative attached captions without whole-recording ASR."""

        from pandrator.logic.cancellable_process import ProcessCancelled
        from pandrator.logic.dubbing import crispasr, qwen_alignment
        from pandrator.logic.dubbing.caption_alignment import (
            CaptionAlignmentError,
            align_caption_cues,
            normalize_alignment_settings,
            parse_vad_export,
            validate_normalized_wav,
            write_diagnostics,
        )
        from pandrator.logic.dubbing.crispasr import CrispASRError, run_vad_export
        from pandrator.logic.dubbing.transcription import (
            apply_vocal_isolation,
            extract_audio,
        )
        from pandrator.logic.media_edit import (
            MediaWord,
            align_cues_to_words,
            caption_to_srt,
            media_cues_to_transcript,
            parse_caption_text,
        )

        options = normalize_alignment_settings(runtime_settings)
        # Runtime hydration may contain resolved provider credentials for an
        # optional ASR fallback. Those values are execution-only and must
        # never enter artifact settings or their settings hash.
        persisted_settings = redact_inline_secrets(
            normalize_alignment_settings(submitted_settings)
        )
        operation_dir = self._operation_dir(session_id, "transcribe")
        progress(0.06, "Normalizing source audio")
        if cancel_event.is_set():
            raise ProcessCancelled("Caption alignment was canceled.")
        normalized_path = Path(
            extract_audio(
                source_path,
                operation_dir,
                source_path.stem,
                ffmpeg_executable=ffmpeg_executable,
                cancel_event=cancel_event,
            )
        )
        duration_ms = validate_normalized_wav(normalized_path)
        progress(0.18, "Source audio normalized")
        # Attached-caption transcription settings may request vocal isolation.
        # Alignment evidence (VAD + CTC/Qwen) uses the isolated derivative;
        # the normalized original stays the export source and the ASR-fallback
        # leg below re-applies isolation internally from that original.
        alignment_audio_path, isolation_provenance = apply_vocal_isolation(
            normalized_path,
            operation_dir,
            normalized_path.stem,
            options,
            cancel_event=cancel_event,
            progress=progress,
            ffmpeg_executable=ffmpeg_executable,
        )
        isolation_record: dict[str, Any] | None = None
        if isolation_provenance is not None:
            duration_ms = validate_normalized_wav(alignment_audio_path)
            isolation_record = {
                "transcription_vocal_isolation": isolation_provenance.get("method"),
                "vocal_isolation_model": isolation_provenance.get("model"),
                "vocal_isolation_status": isolation_provenance.get("status"),
                "original_audio_retained": normalized_path.name,
            }
            for key in ("model_id", "family", "cli_family", "revision", "sha256", "size_bytes", "method", "status", "backend", "compute_backend", "requested_backend", "threads"):
                if key in isolation_provenance:
                    isolation_record[key] = deepcopy(isolation_provenance[key])
        _caption_record, caption_path = self.artifacts.resolve(caption_artifact.id)
        cues = parse_caption_text(caption_path.read_text(encoding="utf-8-sig"))
        # Caption alignment owns its VAD policy.  It must remain enabled (or
        # disabled) according to the CrispASR VAD setting even when the
        # configured whole-recording STT engine is cloud/MOSS/etc.
        vad_enabled = bool(options.get("crispasr_vad_enabled", True))
        vad_path: Path | None = None
        vad_spans = None
        vad_options = {
            key: options.get(key)
            for key in (
                "crispasr_vad_model",
                "crispasr_vad_threshold",
                "crispasr_vad_min_speech_ms",
                "crispasr_vad_min_silence_ms",
                "crispasr_vad_max_speech_seconds",
                "crispasr_vad_speech_pad_ms",
            )
        }
        if vad_enabled:
            progress(0.22, "Detecting speech")
            if cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
            vad_path = operation_dir / "vad-segments.json"
            try:
                vad_path = run_vad_export(
                    alignment_audio_path,
                    vad_path,
                    options,
                    executable=crispasr_executable,
                    cancel_event=cancel_event,
                )
                vad_spans = parse_vad_export(vad_path, duration_ms=duration_ms)
            except (CaptionAlignmentError, CrispASRError, OSError, ValueError) as error:
                raise ValueError(f"CrispASR VAD evidence failed: {error}") from error
            if cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
        else:
            progress(0.22, "Speech detection disabled; validating cue timing")
            vad_path = operation_dir / "vad-segments.json"
            vad_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "vad_segments",
                        "sample_rate": 16000,
                        "enabled": False,
                        "segments": [],
                        "crispasr_vad": {
                            "version": 1,
                            "kind": "vad_segments",
                            "sample_rate": 16000,
                            "num_slices": 0,
                            "slices": [],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        def ctc_runner(clip, text, output, run_settings, event):
            return crispasr.run_ctc_alignment(
                clip,
                text,
                output,
                run_settings,
                executable=crispasr_executable,
                cancel_event=event,
            )

        try:
            alignment = align_caption_cues(
                alignment_audio_path,
                cues,
                options,
                vad_spans=vad_spans,
                ctc_runner=None if qwen_alignment.uses_qwen(options, " ".join(cue.text for cue in cues)) else ctc_runner,
                cancel_event=cancel_event,
                progress=lambda value, detail: progress(0.24 + 0.52 * value, detail),
            )
        except ProcessCancelled:
            raise
        except (CaptionAlignmentError, OSError, ValueError, TypeError) as error:
            raise ValueError(f"Caption CTC alignment failed: {error}") from error
        aligned_cues = alignment.cues
        diagnostics = alignment.diagnostics
        diagnostics.vad_model = str(options.get("crispasr_vad_model") or "silero")
        diagnostics.vad_options = vad_options
        output_language = alignment.resolved_language
        output_language_resolution = alignment.language_resolution
        routing: dict[str, Any] = {}
        requested_language = str(submitted_settings.get("original_language") or submitted_settings.get("stt_language") or "auto")

        raw_asr_srt_artifact = None
        raw_asr_words_artifact = None
        if (
            options["caption_alignment_method"] == "ctc_asr_fallback"
            and diagnostics.eligible_alignment_coverage
            < options["caption_alignment_fallback_coverage"]
        ):
            progress(0.78, "Running ASR fallback for rejected cues")
            if cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
            fallback_dir = operation_dir / "fallback"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            from pandrator.logic.dubbing.transcription import (
                transcribe_source_file_with_metadata,
            )

            fallback_result = transcribe_source_file_with_metadata(
                fallback_dir,
                normalized_path,
                options,
                ffmpeg_executable=ffmpeg_executable,
                crispasr_executable=crispasr_executable,
                cancel_event=cancel_event,
                source_is_normalized=True,
            )
            if cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
            fallback_language = str(getattr(fallback_result, "resolved_language", "") or "")
            if fallback_language.lower() not in {"", "auto", "und", "unknown"}:
                output_language = fallback_language
            routing = deepcopy(dict(getattr(fallback_result, "routing", {}) or {}))
            if fallback_language.lower() not in {"", "auto", "und", "unknown"}:
                output_language_resolution = {
                    "requested_language": requested_language,
                    "resolved_language": output_language,
                    "source": "asr",
                    "language_source": routing.get("language_source", "unknown"),
                    "is_detection_proof": routing.get("language_source") == "detected",
                }
            fallback_isolation = None
            fallback_payload: dict[str, Any] = {}
            fallback_words_path = Path(fallback_result.word_timestamps_path)
            try:
                fallback_payload = json.loads(fallback_words_path.read_text(encoding="utf-8"))
                fallback_isolation = (fallback_payload.get("metadata") or {}).get("vocal_isolation")
                if isinstance(fallback_isolation, dict) and fallback_isolation.get("original_audio_retained"):
                    fallback_isolation["original_audio_retained"] = Path(str(fallback_isolation["original_audio_retained"]).replace("\\", "/")).name
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            if fallback_payload:
                payload_metadata = fallback_payload.get("metadata")
                if not isinstance(payload_metadata, dict):
                    payload_metadata = {}
                    fallback_payload["metadata"] = payload_metadata
                payload_metadata["stt_routing"] = deepcopy(routing)
                payload_metadata["requested_language"] = requested_language
                fallback_words_path.write_text(json.dumps(fallback_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            fallback_metadata = {
                "alignment_role": "evidence",
                "source_artifact_id": source_artifact.id,
                "alignment_method": "ctc_asr_fallback",
                "engine": fallback_result.engine,
                "model": fallback_result.engine,
                "compute_backend": fallback_result.compute_backend,
                "language": output_language,
                "resolved_language": output_language,
                "requested_language": requested_language,
                "requested_settings": deepcopy(persisted_settings),
                "stt_routing": deepcopy(routing),
            }
            if isolation_record is not None:
                fallback_metadata["vocal_isolation"] = deepcopy(isolation_record)
            raw_asr_srt_artifact = self.artifacts.register(
                Path(fallback_result.srt_path),
                kind="srt",
                role="transcription_evidence",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=persisted_settings,
                metadata=fallback_metadata,
            )
            raw_asr_words_artifact = self.artifacts.register(
                Path(fallback_result.word_timestamps_path),
                kind="json",
                role="recognition_word_timestamps",
                session_id=session_id,
                parent_ids=[source_artifact.id, raw_asr_srt_artifact.id],
                settings=persisted_settings,
                metadata={
                    **fallback_metadata,
                    "transcription_evidence_artifact_id": raw_asr_srt_artifact.id,
                },
            )
            fallback_transcript = load_transcript(Path(fallback_result.word_timestamps_path))
            asr_words = tuple(
                MediaWord(
                    text=word.text,
                    start_ms=word.start_ms,
                    end_ms=word.end_ms,
                    confidence=word.confidence,
                )
                for word in fallback_transcript.words
            )
            projected = align_cues_to_words(cues, asr_words, padding_ms=options["caption_alignment_padding_ms"])
            replaced = 0
            replaced_tokens = 0
            updated: list[Any] = []
            accepted_ids = {cue.id for cue in aligned_cues if cue.words}
            for ctc_cue, asr_cue in zip(aligned_cues, projected, strict=True):
                if ctc_cue.id in accepted_ids or ctc_cue.start_ms >= duration_ms:
                    updated.append(ctc_cue)
                    continue
                if asr_cue.words:
                    updated.append(asr_cue)
                    replaced += 1
                    replaced_tokens += len(asr_cue.words)
                else:
                    updated.append(ctc_cue)
            aligned_cues = tuple(updated)
            diagnostics.fallback_triggered = True
            diagnostics.fallback_engine = fallback_result.engine
            diagnostics.fallback_filled_cue_count = replaced
            diagnostics.fallback_filled_token_count = replaced_tokens
            diagnostics.method = "qwen3_with_asr_fallback" if diagnostics.ctc_engine == "audio.cpp" else "ctc_with_asr_fallback"
            diagnostics.timing_quality_basis = (
                f"{diagnostics.timing_quality_basis};"
                "asr_lexical_projection_for_ctc_rejections"
            )
            diagnostics.alignment_confidence = sum(
                float(cue.timing_confidence or 0) for cue in aligned_cues
            ) / max(1, len(aligned_cues))
            diagnostics.accepted_cue_count = sum(bool(cue.words) for cue in aligned_cues)
            diagnostics.accepted_token_count = sum(
                len(cue.text.split()) if diagnostics.ctc_engine == "audio.cpp" else len(cue.words)
                for cue in aligned_cues if cue.words
            )
            diagnostics.alignment_coverage = diagnostics.accepted_token_count / max(1, diagnostics.all_token_count)
            diagnostics.eligible_alignment_coverage = diagnostics.accepted_token_count / max(1, diagnostics.eligible_token_count)

        diagnostics.cue_count = len(aligned_cues)
        diagnostics.word_count = sum(len(cue.words) for cue in aligned_cues)
        final_failed_ids = {cue.id for cue in aligned_cues if not cue.words}
        diagnostics.failed_cue_ids = {
            cue_id: reasons
            for cue_id, reasons in diagnostics.failed_cue_ids.items()
            if cue_id in final_failed_ids
        }
        # Evidence is durable even when the final promotion gate rejects the
        # alignment.  This keeps low-coverage runs inspectable without
        # creating a canonical transcription artifact.
        progress(0.82, "Persisting alignment evidence")
        if cancel_event.is_set():
            raise ProcessCancelled("Caption alignment was canceled.")
        diagnostics_path = operation_dir / "alignment-diagnostics.json"
        write_diagnostics(
            replace(alignment, cues=aligned_cues, diagnostics=diagnostics), diagnostics_path
        )
        if isolation_record is not None:
            diagnostic_payload = json.loads(diagnostics_path.read_text(encoding="utf-8"))
            diagnostic_payload["vocal_isolation"] = deepcopy(isolation_record)
            diagnostics_path.write_text(json.dumps(diagnostic_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence_ids = [source_artifact.id, caption_artifact.id]
        vad_artifact = None
        if vad_path is not None:
            vad_artifact = self.artifacts.register(
                vad_path,
                kind="json",
                role="transcription_vad_evidence",
                session_id=session_id,
                parent_ids=[source_artifact.id],
                settings=persisted_settings,
                metadata={
                    "alignment_method": diagnostics.method,
                    "vad_schema_version": 1,
                    "vad_enabled": vad_enabled,
                    "duration_ms": duration_ms,
                },
            )
            evidence_ids.append(vad_artifact.id)
        diagnostics_artifact = self.artifacts.register(
            diagnostics_path,
            kind="json",
            role="transcription_alignment_diagnostics",
            session_id=session_id,
            parent_ids=[source_artifact.id, caption_artifact.id]
            + ([vad_artifact.id] if vad_artifact else []),
            settings=persisted_settings,
            metadata={
                **diagnostics.as_dict(),
                "requested_language": requested_language,
                "requested_settings": deepcopy(persisted_settings),
                **(
                    {"vocal_isolation": isolation_record}
                    if isolation_record is not None
                    else {}
                ),
            },
        )
        evidence_ids.append(diagnostics_artifact.id)
        for artifact in (raw_asr_srt_artifact, raw_asr_words_artifact):
            if artifact is not None:
                evidence_ids.append(artifact.id)
        if diagnostics.alignment_coverage < 0.5 or diagnostics.eligible_alignment_coverage < 0.5:
            raise ValueError(
                f"Aligned transcription coverage is {diagnostics.alignment_coverage:.6f}, "
                f"eligible coverage is {diagnostics.eligible_alignment_coverage:.6f}; "
                "no aligned transcription was promoted."
            )
        progress(0.86, "Persisting aligned transcription")
        aligned_srt_path = operation_dir / "aligned-transcription.srt"
        aligned_json_path = operation_dir / "aligned-word-timestamps.json"
        aligned_srt_path.write_text(caption_to_srt(aligned_cues), encoding="utf-8")
        metadata = {
            **diagnostics.as_dict(),
            "language": output_language,
            "resolved_language": output_language,
            "language_resolution": deepcopy(output_language_resolution),
            "alignment_language_resolution": alignment.language_resolution,
            "requested_language": requested_language,
            "requested_settings": deepcopy(persisted_settings),
            "alignment_method": diagnostics.method,
            "authoritative_transcript_artifact_id": caption_artifact.id,
            "source_artifact_id": source_artifact.id,
            "vad_artifact_id": vad_artifact.id if vad_artifact else None,
            "alignment_diagnostics_artifact_id": diagnostics_artifact.id,
            "raw_asr_srt_artifact_id": raw_asr_srt_artifact.id if raw_asr_srt_artifact else None,
            "raw_asr_word_timestamps_artifact_id": raw_asr_words_artifact.id if raw_asr_words_artifact else None,
            "evidence_artifact_ids": list(dict.fromkeys(evidence_ids)),
        }
        if routing:
            metadata["stt_routing"] = deepcopy(routing)
        if isolation_record is not None:
            metadata["vocal_isolation"] = isolation_record
        aligned_payload = media_cues_to_transcript(
            aligned_cues,
            language=output_language,
            source_format="media_edit_alignment",
            metadata=metadata,
        )
        aligned_json_path.write_text(json.dumps(aligned_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        aligned_srt_artifact = self.artifacts.register(
            aligned_srt_path,
            kind="srt",
            role="transcription_alignment",
            session_id=session_id,
            parent_ids=list(dict.fromkeys(evidence_ids)),
            settings=persisted_settings,
            metadata=metadata,
        )
        aligned_words_artifact = self.artifacts.register(
            aligned_json_path,
            kind="json",
            role="word_timestamps",
            session_id=session_id,
            # Keep the word artifact as a sibling derived from the same
            # evidence.  The promoted SRT points to it directly below; making
            # both artifacts parents of one another would create a lineage
            # cycle because the canonical SRT path is promoted in place.
            parent_ids=list(dict.fromkeys(evidence_ids)),
            settings=persisted_settings,
            metadata=metadata,
        )
        metadata.update(
            {
                "transcription_alignment_artifact_id": aligned_srt_artifact.id,
                "aligned_word_timestamps_artifact_id": aligned_words_artifact.id,
            }
        )
        language = output_language or None
        _document_id, revision_id = self._store_srt_document(
            session_id,
            aligned_srt_artifact,
            "transcription",
            language=language,
            parent_artifact=caption_artifact,
            speaker_overrides={
                index: cue.speaker for index, cue in enumerate(aligned_cues, start=1) if cue.speaker
            },
        )
        stored_word_count = self._store_timed_words(
            revision_id,
            aligned_json_path,
            segment_by_source_cue_id={cue.id: index for index, cue in enumerate(aligned_cues)},
        )
        final_artifact = self.artifacts.register(
            aligned_srt_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            # The SRT alignment artifact is the same managed path that is
            # promoted to the singleton transcription role, so it cannot be
            # its own parent.  Keep a direct lineage edge through the aligned
            # word-timestamps artifact instead.
            parent_ids=list(dict.fromkeys(evidence_ids + [aligned_words_artifact.id])),
            settings=persisted_settings,
            metadata=metadata,
        )
        progress(1.0, "Aligned transcription ready")
        return {
            "artifact_id": final_artifact.id,
            "path": final_artifact.relative_path,
            "word_timestamps_artifact_id": aligned_words_artifact.id,
            "word_timestamps_path": aligned_words_artifact.relative_path,
            "word_count": stored_word_count,
            "speaker_count": len({cue.speaker.casefold() for cue in aligned_cues if cue.speaker}),
            "revision_id": revision_id,
            **metadata,
            "alignment_coverage": diagnostics.alignment_coverage,
            "eligible_alignment_coverage": diagnostics.eligible_alignment_coverage,
            "alignment_confidence": diagnostics.alignment_confidence,
            "confidence": diagnostics.alignment_confidence,
        }

    def transcribe(self, payload, progress, cancel_event):
        from pandrator.logic.dubbing.transcription import (
            transcribe_source_file_with_metadata,
        )

        session_id = str(payload.get("session_id") or "")
        source_artifact, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        progress(0.05, "Preparing transcription")
        if cancel_event.is_set():
            return {}
        submitted_settings = dict(payload.get("settings") or {})
        runtime_settings = hydrate_stt_settings(
            self.database,
            self.paths,
            submitted_settings,
        )
        from pandrator.logic.dubbing.caption_alignment import (
            normalize_alignment_settings,
        )

        runtime_settings = normalize_alignment_settings(runtime_settings)
        caption_artifact = self._current_media_edit_transcript(session_id)
        if payload.get("caption_artifact_id"):
            from .source_resolution import resolve_media_source

            with self.database.session() as session:
                current_media = resolve_media_source(session, session_id)
                if current_media.artifact is None or current_media.artifact.id != source_artifact.id:
                    raise ValueError("The attached recording changed after alignment was queued.")
            caption_artifact, _caption_path = self._resolve_input(str(payload["caption_artifact_id"]))
            if caption_artifact.session_id != session_id or caption_artifact.state == "deleted":
                raise ValueError("The selected subtitle revision is not available in this session.")
            if (source_artifact.content_hash != payload.get("source_content_hash")
                    or caption_artifact.content_hash != payload.get("caption_content_hash")):
                raise ValueError("The recording or subtitle source changed after alignment was queued.")
        alignment_method = str(
            runtime_settings.get("caption_alignment_method") or "ctc"
        ).strip().lower()
        if caption_artifact is not None and alignment_method in {
            "ctc",
            "ctc_asr_fallback",
        }:
            return self._transcribe_media_edit_with_ctc(
                session_id=session_id,
                source_artifact=source_artifact,
                source_path=source_path,
                caption_artifact=caption_artifact,
                submitted_settings=submitted_settings,
                runtime_settings=runtime_settings,
                ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
                crispasr_executable=str(payload.get("crispasr_executable") or ""),
                progress=progress,
                cancel_event=cancel_event,
            )
        session_dir = self._operation_dir(session_id, "transcribe")
        transcription_result = transcribe_source_file_with_metadata(
            session_dir,
            source_path,
            runtime_settings,
            ffmpeg_executable=str(payload.get("ffmpeg_executable") or "ffmpeg"),
            crispasr_executable=str(payload.get("crispasr_executable") or ""),
            progress_callback=_scaled_progress_callback(progress, 0.05, 0.85),
            cancel_event=cancel_event,
        )
        if caption_artifact is not None:
            return self._transcribe_media_edit_with_caption(
                session_id=session_id,
                source_artifact=source_artifact,
                caption_artifact=caption_artifact,
                transcription_result=transcription_result,
                submitted_settings=submitted_settings,
                progress=progress,
            )
        output_path = Path(transcription_result.srt_path)
        progress(0.9, "Registering transcription")
        requested_language = str(
            submitted_settings.get("original_language")
            or submitted_settings.get("stt_language") or "auto"
        )
        resolved_language = str(getattr(transcription_result, "resolved_language", "") or "")
        output_language = (
            resolved_language if resolved_language.lower() not in {"", "auto", "und", "unknown"}
            else requested_language
        )
        routing = dict(getattr(transcription_result, "routing", {}) or {})
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
                "language": output_language,
                "requested_language": requested_language,
                "stt_routing": routing,
                "requested_settings": redact_inline_secrets(submitted_settings),
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
            metadata={
                "language": output_language, "requested_language": requested_language,
                "stt_routing": routing,
                "requested_settings": redact_inline_secrets(submitted_settings),
            },
        )
        _document_id, revision_id = self._store_srt_document(
            session_id,
            artifact,
            "transcription",
            language=output_language or None,
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
        session_dir = self._operation_dir(session_id, "correct")
        # Resolve once: construction and the run ledger share these exact values.
        run_passage_effective, run_passage_revision = (
            self._resolve_run_passage_settings(
                session_id, payload.get("settings"), database=self.database
            )
        )
        processing_path, input_passages, speaker_by_subtitle = (
            self._prepare_passage_input(
                source_artifact,
                source_path,
                session_dir,
                source_passage_settings=run_passage_effective,
                source_passage_settings_revision=run_passage_revision,
            )
        )
        requested_settings = dict(payload.get("settings") or {})
        if input_passages:
            requested_settings.update(
                {
                    "_logical_passages_version": 1,
                    "_logical_passage_display": self._passage_display_settings(
                        session_id
                    ),
                }
            )
            requested_settings = self._source_passage_run_ledger(
                session_id,
                requested_settings,
                effective=run_passage_effective,
                settings_revision=run_passage_revision,
            )
        settings = self._with_database_llm_settings(requested_settings, "correction")
        settings["correction_style"] = normalize_correction_style(
            settings.get("correction_style")
        )
        requested_settings = {
            **requested_settings,
            "correction_style": settings["correction_style"],
        }
        requested_settings_hash = hashlib.sha256(
            json.dumps(
                requested_settings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
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
                processing_path,
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
            logical_output, display_speakers = self._render_passage_output(
                source_artifact,
                result,
                input_passages,
                settings,
                str(
                    settings.get("original_language")
                    or settings.get("source_language")
                    or ""
                ),
            )
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
                speaker_overrides=display_speakers,
                logical_passages=logical_output,
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

    def media_edit_propose(self, payload, progress, cancel_event):
        """Ask the configured correction model for removal-only cue spans."""

        from types import SimpleNamespace

        from pandrator.logic import llm_handler

        session_id = str(payload.get("session_id") or "")
        try:
            revision_number = int(payload.get("revision"))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Media-edit proposal revision must be an integer."
            ) from error
        revision = self.media_edit.revision(session_id, revision_number)
        if revision is None:
            raise ValueError(
                f"Media-edit revision {revision_number} is not available for this session."
            )
        instructions = str(payload.get("instructions") or "").strip()
        if not instructions:
            raise ValueError("Media-edit proposal instructions must not be empty.")
        cues = [
            {
                "id": str(item.get("id") or ""),
                "start_ms": int(item.get("start_ms")),
                "end_ms": int(item.get("end_ms")),
                "speaker": item.get("speaker"),
                "text": str(item.get("text") or ""),
            }
            for item in revision.get("cues") or []
            if isinstance(item, dict)
        ]
        if not cues:
            raise ValueError("The selected media-edit revision has no cues to review.")
        settings = dict(payload.get("settings") or {})
        settings = self._with_database_llm_settings(settings, "correction")
        correction_model = str(
            settings.get("correction_model") or settings.get("llm_default_model") or ""
        ).strip()
        settings["media_edit_model"] = correction_model
        llm_settings = SimpleNamespace(
            provider_configs=settings["llm_provider_configs"],
            default_model=settings["llm_default_model"],
            request_timeout_seconds=settings["request_timeout_seconds"],
        )
        request_payload = {
            "instructions": instructions,
            "keep_ranges": revision.get("keep_ranges") or [],
            "cues": cues,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "Review this immutable transcript for removal-only video edits. "
                    "Existing cuts are retained; propose only additional removals. "
                    "Return JSON only, with exactly this shape: "
                    '{"cuts":[{"start_cue_id":"cue-...",'
                    '"end_cue_id":"cue-...","reason":"..."}]}. '
                    "Use only the supplied cue IDs; a cut includes every cue from "
                    "start through end. Do not return markdown or any other fields."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    request_payload, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ]
        progress(0.08, "Requesting media-edit proposal")
        result = llm_handler.chat_completion_with_metadata(
            messages=messages,
            model_name=correction_model or None,
            llm_settings=llm_settings,
            cancel_event=cancel_event,
        )
        self._record_usage(
            session_id,
            "media_edit",
            settings,
            result,
            job_id=str(payload.get("_job_id") or "") or None,
        )
        if cancel_event.is_set():
            return {}
        content = (
            result.get("content")
            if isinstance(result, dict) and "content" in result
            else getattr(result, "content", result)
        )
        if isinstance(content, dict):
            decoded = content
        else:
            raw_content = str(content or "").strip()
            if raw_content.startswith("```"):
                raw_content = re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", raw_content, flags=re.IGNORECASE
                ).strip()
            try:
                decoded = json.loads(raw_content)
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("Media-edit proposal must be valid JSON.") from error
        if not isinstance(decoded, dict) or set(decoded) != {"cuts"}:
            raise ValueError("Media-edit proposal must contain only a cuts array.")
        proposed = decoded.get("cuts")
        if not isinstance(proposed, list):
            raise TypeError("Media-edit proposal cuts must be an array.")
        if len(proposed) > 500:
            raise ValueError("Media-edit proposal contains more than 500 cuts.")
        cue_order = {item["id"]: index for index, item in enumerate(cues)}
        cuts: list[dict[str, str]] = []
        for item in proposed:
            if not isinstance(item, dict) or set(item) != {
                "start_cue_id",
                "end_cue_id",
                "reason",
            }:
                raise ValueError("Each proposal cut must contain exactly three fields.")
            start_id = item.get("start_cue_id")
            end_id = item.get("end_cue_id")
            if not isinstance(item.get("reason"), str):
                raise TypeError("Proposal cut reason must be a string.")
            reason = item["reason"].strip()
            if not isinstance(start_id, str) or start_id not in cue_order:
                raise ValueError("Proposal cut references an unknown start cue ID.")
            if not isinstance(end_id, str) or end_id not in cue_order:
                raise ValueError("Proposal cut references an unknown end cue ID.")
            if cue_order[end_id] < cue_order[start_id]:
                raise ValueError("Proposal cut cue IDs are out of order.")
            if not reason:
                raise ValueError("Proposal cut reasons must not be empty.")
            if len(reason) > 500:
                raise ValueError("Proposal cut reasons must be at most 500 characters.")
            cuts.append(
                {
                    "start_cue_id": start_id,
                    "end_cue_id": end_id,
                    "reason": reason,
                }
            )
        if not cuts:
            progress(1.0, "No removals proposed")
            return {
                "plan_id": revision.get("plan_id"),
                "revision_id": revision.get("revision_id"),
                "revision": revision.get("revision"),
                "cut_count": 0,
            }
        progress(0.72, "Applying media-edit proposal")
        state = self.media_edit.apply_proposal(session_id, revision_number, cuts)
        plan = state.get("plan") or {}
        progress(1.0, "Media-edit proposal ready")
        return {
            "plan_id": plan.get("plan_id"),
            "revision_id": plan.get("revision_id"),
            "revision": plan.get("revision"),
            "cut_count": len(cuts),
        }

    @staticmethod
    def _media_edit_cues(payload: dict[str, Any]):
        from pandrator.logic.media_edit import MediaCue, MediaWord

        cues = []
        for item in payload.get("cues") or []:
            if not isinstance(item, dict):
                continue
            words = tuple(
                MediaWord(
                    text=str(word.get("text") or ""),
                    start_ms=int(word.get("start_ms")),
                    end_ms=int(word.get("end_ms")),
                    confidence=(
                        float(word["confidence"])
                        if word.get("confidence") is not None
                        else None
                    ),
                )
                for word in item.get("words") or []
                if isinstance(word, dict)
            )
            cues.append(
                MediaCue(
                    id=str(item.get("id") or ""),
                    start_ms=int(item.get("start_ms")),
                    end_ms=int(item.get("end_ms")),
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
            )
        return cues

    def media_edit_render(self, payload, progress, cancel_event):
        """Render one reviewed immutable media-edit revision."""

        from pandrator.logic.dubbing.audio_sync import media_has_audio_stream
        from pandrator.logic.dubbing.srt_utils import compose_srt
        from pandrator.logic.dubbing.video_muxing import (
            build_removal_only_video_command,
            normalize_video_resolution,
        )
        from pandrator.logic.media_edit import (
            KeepRange,
            caption_to_srt,
            media_cues_to_transcript,
            retime_cues,
        )
        from pandrator.web.capabilities import ffmpeg_video_encoder_ids

        from .media_process import (
            MediaProcessCancelled,
            MediaProcessError,
            resolve_ffmpeg_executable,
            resolve_ffprobe_executable,
            run_media_process,
        )

        session_id = str(payload.get("session_id") or "")
        try:
            revision_number = int(payload.get("revision"))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Media-edit render revision must be an integer."
            ) from error
        revision = self.media_edit.revision(session_id, revision_number)
        if revision is None:
            raise ValueError(
                f"Media-edit revision {revision_number} is not available for this session."
            )
        if not bool(revision.get("reviewed")):
            raise ValueError(
                "The media-edit revision must be reviewed before rendering."
            )
        source_info = revision.get("source_media_artifact")
        source_id = str((source_info or {}).get("id") or "")
        source_artifact, source_path = self._resolve_input(source_id)
        expected_hash = str(source_artifact.content_hash or "").strip()
        if not expected_hash:
            raise ValueError(
                "The pinned source artifact has no registered content hash."
            )
        if sha256_file(source_path) != expected_hash:
            raise ValueError(
                "The pinned source artifact changed after the revision was created."
            )

        settings = dict(payload.get("settings") or {})
        settings_hash = str(payload.get("settings_hash") or "")
        cues = self._media_edit_cues(revision)
        keep_ranges = tuple(
            KeepRange(
                str(item.get("id") or f"keep-{index:06d}"),
                int(item.get("start_ms")),
                int(item.get("end_ms")),
                item.get("label"),
            )
            for index, item in enumerate(revision.get("keep_ranges") or [], start=1)
            if isinstance(item, dict)
        )
        if not keep_ranges:
            raise ValueError("The selected media-edit revision has no retained ranges.")
        retimed_cues = retime_cues(cues, keep_ranges)
        operation_dir = self._operation_dir(session_id, "media-edit-render")
        plan_id = str(revision.get("plan_id") or "")
        revision_id = str(revision.get("revision_id") or "")
        revision_tag = f"{plan_id}-r{revision_number}-{revision_id}"
        subtitle_path = operation_dir / f"media-edit-{revision_tag}.srt"
        word_timestamps_path = operation_dir / f"media-edit-{revision_tag}.json"
        output_path = operation_dir / f"media-edit-{revision_tag}.mp4"
        word_count = sum(len(cue.words) for cue in retimed_cues)
        word_timestamps_path.write_text(
            json.dumps(
                media_cues_to_transcript(
                    retimed_cues,
                    source_format="media_edit",
                    metadata={
                        "plan_id": plan_id,
                        "revision_id": revision_id,
                        "revision": revision_number,
                        "timing_method": "media_edit_retime",
                        "word_count": word_count,
                    },
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if word_count:
            from pandrator.logic.dubbing.subtitle_finalization import (
                compose_transcript_segments_with_ownership,
            )

            composition = compose_transcript_segments_with_ownership(
                word_timestamps_path,
                settings,
            )
            published_cues = composition.segments
            word_segment_ordinals = composition.word_segment_ordinals
            subtitle_path.write_text(compose_srt(published_cues), encoding="utf-8")
            composition_mode = "word_timed_semantic"
        else:
            published_cues = list(retimed_cues)
            word_segment_ordinals = None
            subtitle_path.write_text(caption_to_srt(retimed_cues), encoding="utf-8")
            composition_mode = "source_cues"
        composition_metadata = {
            "source_cue_count": len(retimed_cues),
            "composed_cue_count": len(published_cues),
            "composition_mode": composition_mode,
        }
        encoder = str(settings.get("burn_video_encoder") or "libx264").strip().lower()
        parent_ids = [
            source_id,
            str((revision.get("editorial_transcript_artifact") or {}).get("id") or ""),
            str((revision.get("timing_artifact") or {}).get("id") or ""),
        ]
        parent_ids = [item for item in parent_ids if item]
        metadata = {
            "plan_id": plan_id,
            "media_edit_revision_id": revision_id,
            "revision_id": revision_id,
            "revision": revision_number,
            "content_hash": revision.get("content_hash"),
            "source_artifact_id": source_id,
            "source_content_hash": expected_hash,
            "settings_hash": settings_hash,
            "effective_settings": settings,
            "video_encoder": encoder,
            "word_count": word_count,
            **composition_metadata,
        }
        # Subtitle and timed-word artifacts are intentionally published before
        # the potentially long video encode so correction can consume the
        # immutable subtitle revision while FFmpeg is still running.
        subtitle_artifact = self.artifacts.register(
            subtitle_path,
            kind="srt",
            role="media_edit_subtitles",
            session_id=session_id,
            parent_ids=parent_ids,
            settings=settings,
            metadata=metadata,
        )
        word_timestamps_artifact = self.artifacts.register(
            word_timestamps_path,
            kind="json",
            role="media_edit_word_timestamps",
            session_id=session_id,
            parent_ids=[subtitle_artifact.id],
            settings=settings,
            metadata={
                **metadata,
                "subtitle_artifact_id": subtitle_artifact.id,
                "timing_method": "media_edit_retime",
            },
        )
        editorial_id = str(
            (revision.get("editorial_transcript_artifact") or {}).get("id") or ""
        )
        editorial_artifact = None
        if editorial_id:
            editorial_artifact, _ = self._resolve_input(editorial_id)
        document_id, document_revision_id = self._store_srt_document(
            session_id,
            subtitle_artifact,
            "media_edit_subtitles",
            parent_artifact=editorial_artifact,
            speaker_overrides={
                index: cue.speaker
                for index, cue in enumerate(published_cues, start=1)
                if cue.speaker
            },
        )
        stored_word_count = self._store_timed_words(
            document_revision_id,
            word_timestamps_path,
            segment_by_word_ordinal=word_segment_ordinals,
        )
        duration_ms = sum(item.end_ms - item.start_ms for item in keep_ranges)
        result = {
            "subtitle_artifact_id": subtitle_artifact.id,
            "plan_id": plan_id,
            "revision_id": revision_id,
            "revision": revision_number,
            "duration_ms": duration_ms,
            "media_edit_word_timestamps_artifact_id": word_timestamps_artifact.id,
            "word_timestamps_artifact_id": word_timestamps_artifact.id,
            "word_timestamps_path": word_timestamps_artifact.relative_path,
            "word_count": stored_word_count,
            **composition_metadata,
            "document_id": document_id,
            "document_revision_id": document_revision_id,
        }

        if payload.get("subtitles_only") is True:
            progress(1.0, "Resegmented subtitles and timed words ready")
            return {**result, "subtitles_only": True}

        ffmpeg_executable = resolve_ffmpeg_executable(
            str(settings.get("ffmpeg_executable") or "") or None
        )
        encoder = str(settings.get("burn_video_encoder") or "libx264").strip().lower()
        if encoder not in ffmpeg_video_encoder_ids(ffmpeg_executable):
            raise ValueError(
                f"The selected FFmpeg build does not provide the {encoder} video encoder."
            )
        resolution = normalize_video_resolution(
            settings.get("burn_video_resolution", "source")
        )
        try:
            has_audio = media_has_audio_stream(
                source_path,
                ffprobe_executable=resolve_ffprobe_executable(
                    str(settings.get("ffprobe_executable") or "") or None
                ),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(
                "The pinned source media could not be inspected for audio."
            ) from error
        command = build_removal_only_video_command(
            str(source_path),
            str(output_path),
            tuple((item.start_ms, item.end_ms) for item in keep_ranges),
            has_audio=has_audio,
            ffmpeg_executable=ffmpeg_executable,
            video_encoder=encoder,
            video_quality=settings.get("burn_video_quality", 18),
            video_speed=str(settings.get("burn_video_speed") or "balanced"),
            audio_bitrate=str(settings.get("burn_audio_bitrate") or "192k"),
            hardware_device=(
                str(
                    settings.get("burn_video_hardware_device")
                    or settings.get("hardware_device")
                    or ""
                ).strip()
                or None
            ),
            video_resolution=resolution,
            include_progress=True,
        )
        progress(0.2, "Subtitle revision and timed words ready; rendering edited media")
        last_processed_ms = 0
        last_reported_progress = 0.2
        last_reported_percent = 0.0
        last_reported_at = time.monotonic()

        def report_render_progress(record: dict[str, str]) -> None:
            nonlocal last_processed_ms
            nonlocal last_reported_progress
            nonlocal last_reported_percent
            nonlocal last_reported_at

            out_times_us = []
            for field in ("out_time_us", "out_time_ms"):
                try:
                    parsed = int(record.get(field, ""))
                except (TypeError, ValueError):
                    continue
                if parsed >= 0:
                    out_times_us.append(parsed)
            if not out_times_us:
                return

            processed_ms = min(duration_ms, max(out_times_us) // 1000)
            processed_ms = max(last_processed_ms, processed_ms)
            if processed_ms <= last_processed_ms:
                return
            last_processed_ms = processed_ms

            fraction = min(1.0, processed_ms / duration_ms)
            mapped_progress = min(0.95, 0.2 + 0.75 * fraction)
            mapped_progress = max(last_reported_progress, mapped_progress)
            percent = fraction * 100
            now = time.monotonic()
            if (
                now - last_reported_at < 1.0
                and percent - last_reported_percent < 1.0
            ):
                return
            if mapped_progress <= last_reported_progress:
                return

            progress(
                mapped_progress,
                f"Rendering edited media: {processed_ms / 1000:.1f}s / "
                f"{duration_ms / 1000:.1f}s ({percent:.0f}%)",
            )
            last_reported_progress = mapped_progress
            last_reported_percent = percent
            last_reported_at = now

        try:
            run_media_process(
                command,
                cancel_event=cancel_event,
                progress_callback=report_render_progress,
            )
        except MediaProcessCancelled:
            output_path.unlink(missing_ok=True)
            return {}
        except MediaProcessError as error:
            output_path.unlink(missing_ok=True)
            raise ValueError(
                "Media-edit rendering requires a video source and FFmpeg could not produce the MP4."
            ) from error
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        if cancel_event.is_set():
            output_path.unlink(missing_ok=True)
            return {}
        media_artifact = self.artifacts.register(
            output_path,
            kind="video",
            role="media_edit_media",
            session_id=session_id,
            parent_ids=parent_ids,
            settings=settings,
            metadata=metadata,
        )
        progress(1.0, "Edited media ready")
        return {**result, "media_artifact_id": media_artifact.id}

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
        session_dir = self._operation_dir(session_id, "translate")
        # Resolve once: construction and the run ledger share these exact values.
        run_passage_effective, run_passage_revision = (
            self._resolve_run_passage_settings(
                session_id, payload.get("settings"), database=self.database
            )
        )
        processing_path, input_passages, speaker_by_subtitle = (
            self._prepare_passage_input(
                source_artifact,
                source_path,
                session_dir,
                source_passage_settings=run_passage_effective,
                source_passage_settings_revision=run_passage_revision,
            )
        )
        requested_settings = dict(payload.get("settings") or {})
        if input_passages:
            requested_settings.update(
                {
                    "_logical_passages_version": 1,
                    "_logical_passage_display": self._passage_display_settings(
                        session_id
                    ),
                }
            )
            requested_settings = self._source_passage_run_ledger(
                session_id,
                requested_settings,
                effective=run_passage_effective,
                settings_revision=run_passage_revision,
            )
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
                processing_path,
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
                    processing_path,
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
            logical_output, display_speakers = self._render_passage_output(
                source_artifact,
                result,
                input_passages,
                settings,
                str(settings.get("target_language") or ""),
            )
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
                speaker_overrides=display_speakers,
                logical_passages=logical_output,
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
        from .speech_structure_analysis import annotate_speech_units
        from .tts_optimization import OptimizationUsage, optimize_texts

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
        annotation_mode = str(
            settings.get("llm_tts_annotation_mode")
            or settings.get("annotation_mode")
            or "off"
        ).strip().lower()
        if annotation_mode not in {"off", "dialogue", "speakers"}:
            raise ValueError(
                "llm_tts_annotation_mode must be off, dialogue, or speakers"
            )
        annotation_only = bool(
            settings.get(
                "llm_tts_annotation_only",
                settings.get("annotation_only", False),
            )
        )
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
        metadata_markup = (source_artifact.metadata_json or {}).get("speech_markup")
        source_markup: dict[str, str] = {
            str(key): value
            for key, value in (metadata_markup.items() if isinstance(metadata_markup, dict) else [])
            if isinstance(value, str)
        }

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
                if annotation_only:
                    optimized = list(source_texts)
                    usage = OptimizationUsage()
                else:
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
                if apply_reviewed and not structured_mode and not annotation_only:
                    optimized = [
                        apply_reviewed_pronunciations(
                            revised,
                            known_by_index.get(index, []),
                        )
                        for index, revised in enumerate(optimized)
                    ]
                if annotation_mode != "off" or source_markup:
                    markup = annotate_speech_units(
                        self.database,
                        session_id,
                        optimized,
                        mode=annotation_mode,
                        llm_settings=llm_settings,
                        model_name=model_name,
                        cancel_event=cancel_event,
                        source_markup=source_markup,
                        on_usage=usage.add,
                    )
                    for index, xml in enumerate(markup):
                        if xml:
                            speech_plans[index]["speech_xml"] = xml
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
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    continue
                nested_plan = row.get("speech_plan")
                row_markup = row.get("speech_xml") or (
                    nested_plan.get("speech_xml")
                    if isinstance(nested_plan, dict)
                    else None
                )
                if isinstance(row_markup, str):
                    source_markup[str(index)] = row_markup
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
                    if speech_plans[index]:
                        existing_plan = row.get("speech_plan")
                        merged_plan = {
                            **(existing_plan if isinstance(existing_plan, dict) else {}),
                            **speech_plans[index],
                        }
                        row["speech_plan"] = merged_plan
                        if merged_plan.get("speech_xml"):
                            row["speech_xml"] = merged_plan["speech_xml"]
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
        if any(speech_plans):
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
                    "speech_markup": {
                        str(index + 1): str(plan["speech_xml"])
                        for index, plan in enumerate(speech_plans)
                        if isinstance(plan, dict)
                        and isinstance(plan.get("speech_xml"), str)
                    },
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
                "speech_markup": {
                    str(index + 1): str(plan["speech_xml"])
                    for index, plan in enumerate(speech_plans)
                    if isinstance(plan, dict) and isinstance(plan.get("speech_xml"), str)
                },
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

    def _voice_workflow_context(self) -> VoiceWorkflowContext:
        return VoiceWorkflowContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            tts_providers=self.tts_providers,
            manager_bridge=self.manager_bridge,
            _resolve_input=self._resolve_input,
            _session_dir=self._session_dir,
            _scaled_progress_callback=_scaled_progress_callback,
        )

    def transcribe_voice(self, payload, progress, cancel_event):
        return _voice_transcribe_voice(self._voice_workflow_context(), payload, progress, cancel_event)

    def normalize_voice_recording(self, payload, progress, cancel_event):
        return _voice_normalize_voice_recording(self._voice_workflow_context(), payload, progress, cancel_event)

    def publish_voice(self, payload, progress, cancel_event):
        return _voice_publish_voice(self._voice_workflow_context(), payload, progress, cancel_event)

    def unpublish_voice(self, payload, progress, cancel_event):
        return _voice_unpublish_voice(self._voice_workflow_context(), payload, progress, cancel_event)

    def upload_rvc_model(self, payload, progress, cancel_event):
        return _voice_upload_rvc_model(self._voice_workflow_context(), payload, progress, cancel_event)

    def convert_with_rvc(self, payload, progress, cancel_event):
        return _voice_convert_with_rvc(self._voice_workflow_context(), payload, progress, cancel_event)

    def train_xtts(self, payload, progress, cancel_event):
        return _voice_train_xtts(self._voice_workflow_context(), payload, progress, cancel_event)

    def clean_source(self, payload, progress, cancel_event):
        return _source_clean_source(self._source_workflow_context(), payload, progress, cancel_event)

    def prepare_source_cleaning_dispatch(self, payload, progress, cancel_event):
        return _source_prepare_source_cleaning_dispatch(self._source_workflow_context(), payload, progress, cancel_event)

    def prepare_text(self, payload, progress, cancel_event):
        return _source_prepare_text(self._source_workflow_context(), payload, progress, cancel_event)

    def _generation_plan_store_context(self) -> GenerationPlanStoreContext:
        return GenerationPlanStoreContext(
            database=self.database,
            _is_subtitle_generation_record=self._is_subtitle_generation_record,
            _usable_language=self._usable_language,
            _optimization_text_hash=self._optimization_text_hash,
            _generation_segmentation_settings=_generation_segmentation_settings,
            _secret_free_tts_settings=_secret_free_tts_settings,
            _default_silence_after_ms=_default_silence_after_ms,
        )

    def _store_generation_plan(
        self,
        session_id: str,
        records: list[dict[str, Any]],
        *,
        settings: dict[str, Any],
        source_revision_id: str | None = None,
        source_artifact_id: str | None = None,
        db_session=None,
        force_new: bool = False,
    ) -> tuple[str, list[str]]:
        return _store_generation_plan_impl(
            self._generation_plan_store_context(),
            session_id,
            records,
            settings=settings,
            source_revision_id=source_revision_id,
            source_artifact_id=source_artifact_id,
            db_session=db_session,
            force_new=force_new,
        )

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
        *,
        capabilities: Any | None = None,
    ) -> int:
        """Return the selected provider's bounded synthesis group size."""
        if capabilities is None:
            capabilities = self.tts_providers.synthesis_capabilities(
                settings,
                **tts_urls,
            )
        if capabilities.parallel_synthesis:
            try:
                return max(
                    1,
                    min(8, int(settings.get("tts_concurrent_requests") or 1)),
                )
            except (TypeError, ValueError):
                return 1
        try:
            requested = max(
                1,
                min(32, int(settings.get("tts_batch_size") or 10)),
            )
        except (TypeError, ValueError):
            requested = 10
        if requested == 1:
            return 1

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
        source_artifact_id: str | None = None,
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

        if bool(settings.get("llm_tts_optimization")):
            from .models import PerformancePlan

            if bool(settings.get("llm_tts_document_optimization")):
                raise ValueError(
                    "Document-level speech optimization is already selected. Prepare and review a speech plan before generation instead of rewriting it again."
                )
            with self.database.session() as session:
                active = session.scalar(
                    select(GenerationPlan).where(GenerationPlan.session_id == session_id)
                )
                revision_ids = {active.active_revision_id} if active and active.active_revision_id else set()
                if segment_ids:
                    first = session.get(GenerationSegment, segment_ids[0])
                    if first is not None:
                        revision_ids.add(first.plan_revision_id)
                artifact_ids = {source_artifact_id} if source_artifact_id else set()
                for revision_id in revision_ids:
                    revision = session.get(GenerationPlanRevision, revision_id)
                    if revision is None:
                        continue
                    revision_settings = dict(revision.settings_json or {})
                    if revision_settings.get("llm_tts_document_optimization"):
                        raise ValueError(
                            "This speech plan used document-level optimization. Prepare and review a new speech plan before generation."
                        )
                    if revision_settings.get("_source_artifact_id"):
                        artifact_ids.add(str(revision_settings["_source_artifact_id"]))
                    if session.scalar(
                        select(PerformancePlan.id).where(
                            PerformancePlan.plan_revision_id == revision_id,
                            PerformancePlan.status == "adopted",
                        ).limit(1)
                    ):
                        raise ValueError(
                            "An adopted speech direction belongs to this plan. Prepare and review a speech plan before generation instead of rewriting its wording."
                        )
                    if any(
                        isinstance(value, dict) and bool(value.get("speech_xml"))
                        for value in session.scalars(
                            select(GenerationSegment.speech_plan_json).where(
                                GenerationSegment.plan_revision_id == revision_id
                            )
                        )
                    ):
                        raise ValueError(
                            "Speech XML belongs to this plan. Prepare and review a speech plan before generation instead of rewriting its wording."
                        )
                if artifact_ids:
                    artifact_ids.update(
                        session.scalars(
                            select(ArtifactEdge.parent_artifact_id).where(
                                ArtifactEdge.child_artifact_id.in_(artifact_ids)
                            )
                        )
                    )
                for artifact_id in artifact_ids:
                    source = session.get(Artifact, artifact_id)
                    if source is not None and (
                        source.role == "tts_optimized"
                        or bool((source.metadata_json or {}).get("speech_markup"))
                    ):
                        raise ValueError(
                            "The source already has document-level optimization or speech XML. Prepare and review a speech plan before generation instead of rewriting it again."
                        )

        if settings.get("llm_tts_annotation_mode", "off") != "off":
            raise ValueError("Dialogue and character analysis creates a reviewable document revision. Run speech optimization before generation.")
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

    def _automatic_generation_context(self) -> AutomaticGenerationContext:
        return AutomaticGenerationContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            manager_bridge=self.manager_bridge,
            tts_providers=self.tts_providers,
            _session_dir=self._session_dir,
            _operation_dir=self._operation_dir,
            _store_generation_plan=self._store_generation_plan,
            _usable_language=self._usable_language,
            _optimize_generation_texts=self._optimize_generation_texts,
            _tts_urls=self._tts_urls,
            _negotiated_tts_batch_size=self._negotiated_tts_batch_size,
            prepare_audio_cpp_voice_reference=self.prepare_audio_cpp_voice_reference,
            _ensure_qwen_cloned_voice=self._ensure_qwen_cloned_voice,
            _start_streaming_tts_batch=self._start_streaming_tts_batch,
            _verification_metadata=self._verification_metadata,
            _record_tts_usage=self._record_tts_usage,
            _is_subtitle_generation_record=self._is_subtitle_generation_record,
            _hydrate_tts_settings=hydrate_tts_settings,
            _apply_segment_tts_overrides=_apply_segment_tts_overrides,
            _secret_free_tts_settings=_secret_free_tts_settings,
            _default_silence_after_ms=_default_silence_after_ms,
            _logger=logger,
        )

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
        return _automatic_generate_audio(
            self._automatic_generation_context(),
            session_id,
            source_artifact,
            source_path,
            settings,
            progress,
            cancel_event,
            role=role,
            job_id=job_id,
        )

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
                generation_mode=_speech_block_generation_mode(settings),
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

    def _generation_start_context(self) -> GenerationStartContext:
        return GenerationStartContext(
            database=self.database,
            _resolve_input=self._resolve_input,
            _generation_language=self._generation_language,
            _materialize_subtitle_generation_plan=self._materialize_subtitle_generation_plan,
            _store_generation_plan=self._store_generation_plan,
            run_generation=self.run_generation,
            _secret_free_tts_settings=_secret_free_tts_settings,
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
        return _start_run_reviewable_generation(
            self._generation_start_context(),
            payload,
            progress,
            cancel_event,
            resolved_snapshot=resolved_snapshot,
            settings_hash=settings_hash,
            job_id=job_id,
        )

    def run_generation(self, payload, progress, cancel_event):
        """Run synthesis and release any temporary scheduling interruption."""
        try:
            return self._run_generation(payload, progress, cancel_event)
        finally:
            try:
                from .generation_scheduling import interrupted_run_id

                # A queued sibling's cancellation can transfer permission
                # after this worker loaded its payload. Read durable ownership;
                # the resume transaction rechecks it against concurrent pauses.
                with self.database.session() as session:
                    child = session.get(GenerationRun, str(payload.get("generation_run_id") or ""))
                    source_id = (
                        interrupted_run_id(child)
                        if child is not None and child.resume_source_on_completion
                        and child.status in {"completed", "partial", "failed", "canceled", "cancelled"}
                        else None
                    )
                    child_id = child.id if child is not None else None
                if child_id and source_id:
                    self._resume_generation_after_regeneration(child_id, source_id)
            except Exception:
                logger.exception("Could not release the temporary regeneration pause.")

    def _generation_execution_context(self) -> GenerationExecutionContext:
        return GenerationExecutionContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            manager_bridge=self.manager_bridge,
            tts_providers=self.tts_providers,
            _session_record=self._session_record,
            _session_dir=self._session_dir,
            _usable_language=self._usable_language,
            _optimize_generation_texts=self._optimize_generation_texts,
            _tts_urls=self._tts_urls,
            _negotiated_tts_batch_size=self._negotiated_tts_batch_size,
            prepare_audio_cpp_voice_reference=self.prepare_audio_cpp_voice_reference,
            _ensure_qwen_cloned_voice=self._ensure_qwen_cloned_voice,
            _start_streaming_tts_batch=self._start_streaming_tts_batch,
            _verification_metadata=self._verification_metadata,
            _tts_usage_event=self._tts_usage_event,
            _resume_generation_after_regeneration=self._resume_generation_after_regeneration,
            _finalize_run_audio_verification=self._finalize_run_audio_verification,
            _hydrate_tts_settings=hydrate_tts_settings,
            _apply_segment_tts_overrides=_apply_segment_tts_overrides,
            _apply_selected_segment_tts_override=_apply_selected_segment_tts_override,
            _secret_free_tts_settings=_secret_free_tts_settings,
            _voiceover_second_pass=_voiceover_second_pass,
            _logger=logger,
            _repair_early_generation_blocks=self._repair_early_generation_blocks,
            _regroup_generation_blocks=self._regroup_generation_blocks,
        )

    def _repair_early_generation_blocks(
        self, run_id: str, progress: Progress, cancel_event: threading.Event
    ) -> dict[str, Any]:
        from .voiceover_repair import repair_early_blocks

        return repair_early_blocks(self, run_id, progress, cancel_event)

    def _regroup_generation_blocks(
        self, run_id: str, progress: Progress, cancel_event: threading.Event
    ) -> dict[str, Any]:
        from .voiceover_regroup import repair_regroup_blocks

        return repair_regroup_blocks(self, run_id, progress, cancel_event)

    def _run_generation(self, payload, progress, cancel_event):
        """Generate immutable per-segment takes with safe pause and resume boundaries."""
        return _execution_run_generation(
            self._generation_execution_context(), payload, progress, cancel_event
        )

    def _output_workflow_context(self) -> OutputWorkflowContext:
        return OutputWorkflowContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            _resolve_input=self._resolve_input,
            _session_dir=self._session_dir,
            _session_record=self._session_record,
            _operation_dir=self._operation_dir,
        )

    def assemble_generation_output(self, payload, progress, cancel_event):
        """Assemble the current selected takes in plan order into an immutable artifact."""
        return _assemble_generation_output(
            self._output_workflow_context(), payload, progress, cancel_event
        )

    def generate_waveform(self, payload, progress, cancel_event):
        """Create a compact, reusable peak artifact for browser review."""
        from .media_process import MediaProcessCancelled
        from .waveform import generate_waveform_peaks

        source, source_path = self._resolve_input(
            str(payload.get("source_artifact_id") or "")
        )
        max_points = max(128, min(5000, int(payload.get("max_points") or 1600)))
        start_ms = max(0, int(payload.get("start_ms") or 0))
        raw_end_ms = payload.get("end_ms")
        end_ms = int(raw_end_ms) if raw_end_ms is not None else None
        bounded = raw_end_ms is not None or start_ms > 0
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
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except MediaProcessCancelled:
            return {}
        if cancel_event.is_set():
            return {}
        progress(0.9, "Writing waveform peaks")
        destination = destination_dir / (
            f"waveform-{source.id}-{waveform.start_ms}-{waveform.end_ms}-"
            f"{max_points}.json"
        )
        destination.write_text(
            json.dumps(
                {
                    "duration_ms": waveform.duration_ms,
                    "start_ms": waveform.start_ms,
                    "end_ms": waveform.end_ms,
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
            role="waveform_peaks_window" if bounded else "waveform_peaks",
            session_id=source.session_id,
            parent_ids=[source.id],
            settings={
                "max_points": max_points,
                "start_ms": waveform.start_ms,
                "end_ms": waveform.end_ms,
            },
            metadata={
                "source_artifact_id": source.id,
                "duration_ms": waveform.duration_ms,
                "start_ms": waveform.start_ms,
                "end_ms": waveform.end_ms,
                "max_points": max_points,
                "analysis_sample_rate_hz": waveform.analysis_sample_rate_hz,
            },
        )
        progress(1.0, "Waveform ready")
        return {
            "artifact_id": artifact.id,
            "source_artifact_id": source.id,
            "point_count": len(waveform.points),
            "start_ms": waveform.start_ms,
            "end_ms": waveform.end_ms,
        }

    def generate_project_export_bundle(self, payload, progress, cancel_event):
        """Publish frozen export receipts without constructing request services."""
        from types import SimpleNamespace

        from .project_export_bundles import ProjectExportBundleService

        worker_services = SimpleNamespace(
            database=self.database, paths=self.paths, artifacts=self.artifacts
        )
        return ProjectExportBundleService(worker_services).generate(payload, progress, cancel_event)

    def generate_video_preview(self, payload, progress, cancel_event):
        """Generate a bounded compatible derivative without changing its source."""
        from .video_previews import VideoPreviewService

        return VideoPreviewService(self.database, self.paths, self.artifacts, self.jobs).generate(
            payload, progress, cancel_event
        )

    def generate_audio_preview(self, payload, progress, cancel_event):
        """Transcode the first source audio stream to a browser-safe MP3."""
        from .media_process import (
            MediaProcessCancelled,
            MediaProcessError,
            resolve_ffmpeg_executable,
            run_media_process,
        )

        try:
            source, source_path = self._resolve_input(
                str(payload.get("source_artifact_id") or "")
            )
            destination_dir = (
                self._session_dir(source.session_id)
                if source.session_id
                else self.paths.artifacts / "audio-previews"
            )
            destination_dir.mkdir(parents=True, exist_ok=True)
        except (KeyError, OSError, ValueError):
            raise RuntimeError(
                "The source audio preview could not be prepared."
            ) from None
        destination = destination_dir / f"audio-preview-v1-{source.id}.mp3"
        temporary_destination = (
            destination_dir / f".audio-preview-v1-{source.id}-{new_id()}.mp3"
        )
        previous_destination = (
            destination_dir / f".audio-preview-v1-{source.id}-{new_id()}.previous"
        )
        settings = {
            "preview_version": "v1",
            "codec": "mp3",
            "bitrate_kbps": 64,
            "channels": 1,
            "sample_rate_hz": 48000,
        }
        replaced_destination = False
        previous_destination_staged = False
        progress(0.05, "Preparing source audio preview")
        command = [
            resolve_ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(temporary_destination),
        ]
        try:
            progress(0.1, "Transcoding source audio preview")
            try:
                run_media_process(command, cancel_event=cancel_event)
            except MediaProcessCancelled:
                raise
            except MediaProcessError:
                raise RuntimeError(
                    "The source audio preview could not be transcoded."
                ) from None
            if cancel_event.is_set():
                temporary_destination.unlink(missing_ok=True)
                return {}
            if destination.is_file():
                os.replace(destination, previous_destination)
                previous_destination_staged = True
            os.replace(temporary_destination, destination)
            replaced_destination = True
            if cancel_event.is_set():
                destination.unlink(missing_ok=True)
                if previous_destination_staged:
                    os.replace(previous_destination, destination)
                    previous_destination_staged = False
                replaced_destination = False
                return {}
            progress(0.9, "Registering source audio preview")
            if cancel_event.is_set():
                destination.unlink(missing_ok=True)
                if previous_destination_staged:
                    os.replace(previous_destination, destination)
                    previous_destination_staged = False
                replaced_destination = False
                return {}
            artifact = self.artifacts.register(
                destination,
                kind="audio",
                role="source_audio_preview",
                session_id=source.session_id,
                parent_ids=[source.id],
                replace_parent_ids=True,
                settings=settings,
                metadata={
                    "source_artifact_id": source.id,
                    "source_content_hash": source.content_hash,
                    "source_artifact_hash": source.content_hash,
                    "preview_version": "v1",
                },
            )
            previous_destination.unlink(missing_ok=True)
            previous_destination_staged = False
            replaced_destination = False
            return {"artifact_id": artifact.id, "source_artifact_id": source.id}
        except MediaProcessCancelled:
            temporary_destination.unlink(missing_ok=True)
            if replaced_destination:
                destination.unlink(missing_ok=True)
            if previous_destination_staged:
                os.replace(previous_destination, destination)
            return {}
        # This is the final cleanup and redaction boundary for filesystem,
        # artifact-registration, and database failures.
        except Exception:  # noqa: BLE001
            temporary_destination.unlink(missing_ok=True)
            if replaced_destination:
                destination.unlink(missing_ok=True)
            if previous_destination_staged:
                os.replace(previous_destination, destination)
            raise RuntimeError(
                "The source audio preview could not be prepared."
            ) from None

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

        from .workspace import find_matching_output_assembly

        with self.database.session() as session:
            run = session.get(GenerationRun, generation_run_id)
            if run is None or run.session_id != session_id:
                raise ValueError(
                    "The selected generation run does not belong to this session."
                )
            if run.status != "completed":
                raise ValueError("Only a completed generation run can be exported.")
            existing, assembly_snapshot, settings_hash = (
                find_matching_output_assembly(
                    session,
                    session_id=session_id,
                    run=run,
                    resolved_settings_snapshot=resolved_settings_snapshot,
                )
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
        needs_assembly = export_requires_generation_assembly(
            workflow_kind=record.workflow_kind,
            settings=settings,
        )
        pinned_assembly_artifact_id = None
        if needs_assembly:
            pinned_assembly_artifact_id = self._ensure_export_generation_assembly(
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
        export_payload = (
            {
                **payload,
                "resolved_settings_snapshot": resolved_snapshot,
                "pinned_assembly_artifact_id": pinned_assembly_artifact_id,
            }
            if pinned_assembly_artifact_id
            else payload
        )
        return self.export(export_payload, export_progress, cancel_event)

    def export(self, payload, progress, cancel_event):
        """Create immutable, managed exports from the explicitly selected inputs."""
        return _export(
            self._output_workflow_context(), payload, progress, cancel_event
        )

    def apply_pdf_edits(self, payload, progress, cancel_event):
        import tempfile

        from .pdf_editor import PdfEditPlan, apply_pdf_edit_plan
        from .pdf_publication import publish_pdf_edit

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
        progress(0.1, "Validating PDF edit plan")
        plan = PdfEditPlan.from_value(dict(payload.get("plan") or {}))
        if cancel_event.is_set():
            return {}
        job_id = str(payload.get("_job_id") or "").strip() or None
        raw_generation = payload.get("_lease_generation")
        lease_generation = int(raw_generation) if raw_generation is not None else None
        with tempfile.TemporaryDirectory(prefix=".pdf-edit-", dir=output_dir) as directory:
            destination, manifest, provenance = apply_pdf_edit_plan(
                source_path,
                Path(directory) / output_path.name,
                plan,
                parent_artifact_id=source_artifact.id,
            )
            progress(0.85, "Registering edited PDF")
            output_artifact, manifest_artifact = publish_pdf_edit(
                self.database,
                self.paths,
                self.artifacts,
                destination,
                manifest,
                provenance,
                output_path,
                source_artifact_id=source_artifact.id,
                session_id=session_id,
                cancel_event=cancel_event,
                job_id=job_id,
                lease_generation=lease_generation,
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
