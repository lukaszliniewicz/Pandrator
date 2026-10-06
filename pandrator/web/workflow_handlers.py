"""Worker adapters that run existing Pandrator engines without Qt."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os as os  # Preserve the shared module seam for preview monkeypatches.
import re
import subprocess as subprocess
import threading
import time
import unicodedata
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from sqlalchemy import select

from pandrator.logic.dubbing.settings import normalize_correction_style
from pandrator.logic.dubbing.srt_utils import (
    split_speaker_label,
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
    Document,
    DocumentRevision,
    GenerationPlan,
    GenerationRun,
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
from .workflow_audio_preview import AudioPreviewContext
from .workflow_audio_preview import generate_audio_preview as _generate_audio_preview_impl
from .workflow_audio_preview import preview_output_mix as _preview_output_mix_impl
from .workflow_caption_alignment import CaptionAlignmentContext
from .workflow_caption_alignment import (
    transcribe_media_edit_with_caption as _transcribe_media_edit_with_caption_impl,
)
from .workflow_caption_alignment import (
    transcribe_media_edit_with_ctc as _transcribe_media_edit_with_ctc_impl,
)
from .workflow_export import export as _export
from .workflow_fingerprints import _research_fingerprint as _research_fingerprint
from .workflow_fingerprints import _stage_settings_fingerprint as _stage_settings_fingerprint
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
from .workflow_media_edit_render import MediaEditRenderContext
from .workflow_media_edit_render import media_edit_render as _media_edit_render_impl
from .workflow_output_assembly import (
    assemble_generation_output as _assemble_generation_output,
)
from .workflow_output_context import OutputWorkflowContext
from .workflow_prerequisites import WorkflowPrerequisiteService
from .workflow_source import SourceWorkflowContext
from .workflow_source import clean_source as _source_clean_source
from .workflow_source import download_source_url as _source_download_source_url
from .workflow_source import (
    prepare_source_cleaning_dispatch as _source_prepare_source_cleaning_dispatch,
)
from .workflow_source import prepare_text as _source_prepare_text
from .workflow_source import reuse_source as _source_reuse_source
from .workflow_speech_optimization import SpeechOptimizationContext
from .workflow_speech_optimization import (
    optimize_generation_texts as _optimize_generation_texts_impl,
)
from .workflow_speech_optimization import optimize_tts as _optimize_tts_impl
from .workflow_speech_optimization import (
    save_speech_plan_proposals as _save_speech_plan_proposals_impl,
)
from .workflow_subtitle_transforms import SubtitleTransformContext
from .workflow_subtitle_transforms import correct as _correct_impl
from .workflow_subtitle_transforms import translate as _translate_impl
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
from .workflow_web_research import WebResearchContext
from .workflow_web_research import research_metadata as _research_metadata_impl
from .workflow_web_research import run_stage_web_research as _run_stage_web_research_impl

if TYPE_CHECKING:
    from .manager_proxy import LocalManagerProxy
    from .subtitle_evidence import SubtitleEvidenceService
    from .tts_providers import TtsProviderRegistry


logger = logging.getLogger(__name__)

CLAUSE_PAUSE_RATIO = 1 / 3
GENERATION_SEGMENT_POLICY_VERSION = 5


class _SpeechBlockSpeakerOptions(TypedDict, total=False):
    speaker_by_subtitle: dict[int, str]


def _required_media_edit_time(record: dict[str, Any], field: str) -> int:
    value = record.get(field)
    if value is None:
        raise TypeError(f"Media-edit {field} is required.")
    return int(value)


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


def _speech_block_settings(settings: dict[str, Any]) -> tuple[int, int, int, int, int]:
    def integer_setting(key: str, default: int) -> int:
        value = settings.get(key)
        return int(default if value is None or value == "" else value)

    min_chars = max(1, int(settings.get("speech_block_min_chars") or 10))
    max_chars = max(
        min_chars,
        int(settings.get("speech_block_max_chars") or 220),
    )
    merge_setting = settings.get("speech_block_merge_threshold")
    merge_threshold = max(
        0,
        int(
            merge_setting
            if merge_setting is not None
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


class WorkflowHandlers(WorkflowPrerequisiteService):
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
        raw_input_choices = outcome_value.get("inputs")
        input_choices = raw_input_choices if isinstance(raw_input_choices, dict) else {}
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
            job_kind = definition.job_kind
            if job_kind is None:
                raise RuntimeError("An executable continuation stage requires a job kind.")
            handler = handlers[job_kind]
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
                settings_session_id = managed.session_id if managed is not None else ""
                if settings_session_id is None:
                    raise KeyError(None)
                effective, revision = self._resolve_run_passage_settings(
                    settings_session_id,
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
            managed = session.get(Artifact, artifact.id)
            if managed is None:
                raise KeyError(artifact.id)
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
            speech_metadata: dict[int, dict[str, str]] = {}
            if speech_markup:
                from .generation_cast_runtime import remap_markup
                from .generation_controls import get_generation_controls
                characters = get_generation_controls(session, session_id)["characters"]
                for ordinal, item in enumerate(resolved_segments):
                    cue_markup = speech_markup.get(str(ordinal + 1))
                    if cue_markup:
                        speech_metadata[ordinal] = {
                            "speech_xml": remap_markup(
                                cue_markup, str(ordinal + 1), item.text, characters
                            )
                        }
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
                        **speech_metadata.get(ordinal, {}),
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

    def _transcribe_media_edit_with_caption(
        self,
        *,
        session_id: str,
        source_artifact: Artifact,
        caption_artifact: Artifact,
        transcription_result: Any,
        submitted_settings: dict[str, Any],
        progress,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Project ASR timing onto the authoritative attached captions."""
        return _transcribe_media_edit_with_caption_impl(
            self._caption_alignment_context(),
            session_id=session_id,
            source_artifact=source_artifact,
            caption_artifact=caption_artifact,
            transcription_result=transcription_result,
            submitted_settings=submitted_settings,
            progress=progress,
            cancel_event=cancel_event,
        )

    def _caption_alignment_context(self) -> CaptionAlignmentContext:
        return CaptionAlignmentContext(
            artifacts=self.artifacts,
            _operation_dir=self._operation_dir,
            _store_srt_document=self._store_srt_document,
            _store_timed_words=self._store_timed_words,
            _media_edit_token_count=_media_edit_token_count,
        )

    def _speech_optimization_context(self) -> SpeechOptimizationContext:
        return SpeechOptimizationContext(
            database=self.database,
            artifacts=self.artifacts,
            _resolve_input=self._resolve_input,
            _session_dir=self._session_dir,
            _store_srt_document=self._store_srt_document,
            _with_database_llm_settings=self._with_database_llm_settings,
            _begin_agentic_operation=self._begin_agentic_operation,
            _save_speech_plan_proposals=self._save_speech_plan_proposals,
            _optimization_text_hash=self._optimization_text_hash,
            _record_usage=self._record_usage,
            _scaled_progress_callback=_scaled_progress_callback,
            new_id=new_id,
            utcnow=utcnow,
        )

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
        return _transcribe_media_edit_with_ctc_impl(
            self._caption_alignment_context(),
            session_id=session_id,
            source_artifact=source_artifact,
            source_path=source_path,
            caption_artifact=caption_artifact,
            submitted_settings=submitted_settings,
            runtime_settings=runtime_settings,
            ffmpeg_executable=ffmpeg_executable,
            crispasr_executable=crispasr_executable,
            progress=progress,
            cancel_event=cancel_event,
        )

    def transcribe(self, payload, progress, cancel_event):
        from pandrator.logic.cancellable_process import ProcessCancelled
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
                cancel_event=cancel_event,
            )
        output_path = Path(transcription_result.srt_path)
        progress(0.9, "Registering transcription")
        if cancel_event.is_set():
            raise ProcessCancelled("Transcription cancelled before publication.")
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
            role="transcription_candidate",
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
        if cancel_event.is_set():
            raise ProcessCancelled("Transcription cancelled before publication.")
        stored_artifact, _ = self.artifacts.resolve(artifact.id)
        artifact = self.artifacts.register(
            output_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[source_artifact.id],
            settings=submitted_settings,
            metadata=stored_artifact.metadata_json,
        )
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

    def _web_research_context(self) -> WebResearchContext:
        return WebResearchContext(
            database=self.database,
            paths=self.paths,
            _subtitle_speaker_map=self._subtitle_speaker_map,
            _resolve_secret_reference=resolve_secret_reference,
            _database_reference=database_reference,
            _auxiliary_credential_key=auxiliary_credential_key,
            _fraction_message_callback=_fraction_message_callback,
        )

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
        return _run_stage_web_research_impl(
            self._web_research_context(),
            stage=stage,
            session_id=session_id,
            source_artifact=source_artifact,
            source_path=source_path,
            settings=settings,
            progress=progress,
            cancel_event=cancel_event,
            completed_units=completed_units,
            persist_checkpoint=persist_checkpoint,
        )

    @staticmethod
    def _research_metadata(result, run_id: str) -> dict[str, Any] | None:
        return _research_metadata_impl(result, run_id)

    def _subtitle_transform_context(self) -> SubtitleTransformContext:
        return SubtitleTransformContext(
            database=self.database,
            paths=self.paths,
            artifacts=self.artifacts,
            _resolve_input=self._resolve_input,
            _operation_dir=self._operation_dir,
            _resolve_run_passage_settings=self._resolve_run_passage_settings,
            _prepare_passage_input=self._prepare_passage_input,
            _passage_display_settings=self._passage_display_settings,
            _source_passage_run_ledger=self._source_passage_run_ledger,
            _with_database_llm_settings=self._with_database_llm_settings,
            _begin_agentic_operation=self._begin_agentic_operation,
            _run_stage_web_research=self._run_stage_web_research,
            _research_metadata=self._research_metadata,
            _render_passage_output=self._render_passage_output,
            _store_srt_document=self._store_srt_document,
            _record_usage=self._record_usage,
            _stage_settings_fingerprint=_stage_settings_fingerprint,
            _scaled_progress_callback=_scaled_progress_callback,
            _normalize_correction_style=normalize_correction_style,
            _resolve_secret_reference=resolve_secret_reference,
            _database_reference=database_reference,
            _auxiliary_credential_key=auxiliary_credential_key,
        )

    def correct(self, payload, progress, cancel_event):
        return _correct_impl(
            self._subtitle_transform_context(), payload, progress, cancel_event
        )

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
                "start_ms": _required_media_edit_time(item, "start_ms"),
                "end_ms": _required_media_edit_time(item, "end_ms"),
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
                    start_ms=_required_media_edit_time(word, "start_ms"),
                    end_ms=_required_media_edit_time(word, "end_ms"),
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
                    start_ms=_required_media_edit_time(item, "start_ms"),
                    end_ms=_required_media_edit_time(item, "end_ms"),
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

    def _media_edit_render_context(self) -> MediaEditRenderContext:
        return MediaEditRenderContext(
            database=self.database,
            artifacts=self.artifacts,
            media_edit=self.media_edit,
            _resolve_input=self._resolve_input,
            _media_edit_cues=self._media_edit_cues,
            _operation_dir=self._operation_dir,
            _store_srt_document=self._store_srt_document,
            _store_timed_words=self._store_timed_words,
            _sha256_file=sha256_file,
            _required_media_edit_time=_required_media_edit_time,
            _monotonic=time.monotonic,
        )

    def media_edit_render(self, payload, progress, cancel_event):
        """Render one reviewed immutable media-edit revision."""

        return _media_edit_render_impl(
            self._media_edit_render_context(), payload, progress, cancel_event
        )

    def translate(self, payload, progress, cancel_event):
        return _translate_impl(
            self._subtitle_transform_context(), payload, progress, cancel_event
        )

    def optimize_tts(self, payload, progress, cancel_event):
        """Create a separate, previewable text revision optimized only for speech."""
        return _optimize_tts_impl(
            self._speech_optimization_context(),
            payload,
            progress,
            cancel_event,
        )

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
        return _save_speech_plan_proposals_impl(
            library=library,
            session_id=session_id,
            plan=plan,
            backend=backend,
            model_name=model_name,
            default_language=default_language,
        )

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
        return _optimize_generation_texts_impl(
            self._speech_optimization_context(),
            session_id,
            segment_ids,
            texts,
            settings,
            cancel_event,
            progress,
            job_id=job_id,
            generation_run_id=generation_run_id,
            source_artifact_id=source_artifact_id,
            pronunciation_settings=pronunciation_settings,
            pronunciation_language=pronunciation_language,
            pronunciation_voice_language=pronunciation_voice_language,
        )

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
        speaker_options: _SpeechBlockSpeakerOptions = {}
        if speaker_by_subtitle:
            speaker_options["speaker_by_subtitle"] = speaker_by_subtitle
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

    def _audio_preview_context(self) -> AudioPreviewContext:
        return AudioPreviewContext(
            paths=self.paths,
            artifacts=self.artifacts,
            resolve_input=lambda artifact_id: self._resolve_input(artifact_id),
            session_dir=lambda session_id: self._session_dir(session_id),
            new_id=lambda: new_id(),
        )

    def generate_audio_preview(self, payload, progress, cancel_event):
        return _generate_audio_preview_impl(
            self._audio_preview_context(), payload, progress, cancel_event
        )

    def preview_output_mix(self, payload, progress, cancel_event):
        return _preview_output_mix_impl(
            self._audio_preview_context(), payload, progress, cancel_event
        )

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
