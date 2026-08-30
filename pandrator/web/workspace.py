"""Revisioned settings, outcome plans, sources, and generation workspace services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .artifact_selection import select_source_path
from .database import Database
from .jobs import JobQueue
from .models import (
    AppSetting,
    Artifact,
    AudioTake,
    DocumentRevision,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    GenerationSegmentRevision,
    Job,
    OutcomePlan,
    OutcomePlanHistory,
    OutputAssembly,
    SessionRecord,
    SessionSetting,
    SessionSettingHistory,
    SessionSource,
    SourceAsset,
    SourceRecord,
    UsageEvent,
    utcnow,
)
from .source_resolution import resolve_primary_source
from .tts_optimization import (
    DEFAULT_FIRST_PROMPT,
    DEFAULT_PROMPT,
    DEFAULT_SECOND_PROMPT,
    DEFAULT_THIRD_PROMPT,
)

SETTING_SECTIONS = (
    "text",
    "stt",
    "subtitles",
    "correction",
    "translation",
    "tts",
    "audio",
    "rvc",
    "source_cleaning",
    "output",
)

BUILTIN_DEFAULTS: dict[str, dict[str, Any]] = {
    "text": {
        "enable_sentence_splitting": True,
        "max_sentence_length": 200,
        "enable_sentence_appending": True,
        "remove_diacritics": False,
        "remove_quotation_marks": False,
        "disable_paragraph_detection": False,
        "remove_footnotes": False,
        "filter_citations": True,
        "enable_nemo_normalization": True,
        "normalize_all_caps": True,
        "llm_tts_optimization": False,
        "llm_tts_document_optimization": False,
        "apply_reviewed_pronunciations": True,
        "llm_processing_enabled": False,
        "llm_tts_batch_size": 3,
        "llm_tts_document_batch_size": 8,
        "tts_optimization_model": "",
        "llm_concurrent_calls": 1,
        "speech_optimization_mode": "guarded",
        "speech_plan_min_retention": 0.9,
        "speech_plan_save_proposals": True,
        "llm_multi_stage": False,
        "combined_prompt": DEFAULT_PROMPT,
        "first_prompt": DEFAULT_FIRST_PROMPT,
        "second_prompt": DEFAULT_SECOND_PROMPT,
        "third_prompt": DEFAULT_THIRD_PROMPT,
    },
    "stt": {
        "stt_engine": "whisper",
        "stt_model_quantization": "f16",
        "stt_compute_backend": "auto",
        "stt_language": "auto",
        "stt_compute_device": 0,
        "whisper_prompt": "",
        "stt_threads": 0,
        "stt_chunk_seconds": 0,
        "stt_chunk_overlap_seconds": 3.0,
        "stt_hotwords": "",
        "stt_lid_backend": "whisper",
        "stt_beam_size": 1,
        "parakeet_decoder": "tdt",
        "moss_max_chunk_seconds": 120,
        "moss_chunk_overlap_seconds": 0.0,
        "moss_vad_enabled": False,
        "moss_ctc_alignment_enabled": True,
        "moss_ctc_aligner_model": "auto",
        "moss_ctc_padding_seconds": 0.5,
        "crispasr_vad_enabled": True,
        "crispasr_vad_model": "silero",
        "crispasr_vad_threshold": 0.5,
        "crispasr_vad_min_speech_ms": 250,
        "crispasr_vad_min_silence_ms": 800,
        "crispasr_vad_speech_pad_ms": 30,
        "crispasr_vad_max_speech_seconds": 300,
        "diarization_enabled": False,
    },
    "subtitles": {
        "max_lines": 2,
        "max_chars_per_line": 48,
        "max_cps": 20.0,
        "min_duration_ms": 833,
        "max_duration_ms": 7000,
        "min_gap_ms": 80,
        "phrase_gap_ms": 600,
        "hard_gap_ms": 1500,
        "sentence_boundary_threshold": 0.25,
        "boundary_correction_enabled": False,
        "merge_threshold_ms": 250,
    },
    "correction": {
        "enabled": False,
        "model_name": "",
        "reasoning_effort": "",
        "instructions": "",
        "char_limit": 6000,
        "max_segments_per_batch": 40,
        "llm_concurrent_calls": 1,
        "timing_context_mode": "full",
        "substantial_gap_ms": 2000,
        "no_remove_subtitles": False,
        "context_before": 8,
        "context_after": 2,
        "request_timeout_seconds": 600,
        "web_research_enabled": False,
        "web_research_provider": "jina",
        "web_research_model_name": "",
        "web_research_mode": "global",
        "web_research_context_fraction": 0.8,
        "web_research_language": "",
        "web_research_max_searches": 3,
        "web_research_max_extractions": 2,
        "web_research_preferred_domains": "",
        "web_research_blocked_domains": "",
        "web_research_max_iterations": 8,
        "web_research_timeout_seconds": 90,
        "web_research_source_chars": 14000,
        "web_research_result_chars": 10000,
    },
    "translation": {
        "enabled": False,
        "backend": "llm",
        "source_language": "auto",
        "target_language": "en",
        "model_name": "",
        "reasoning_effort": "",
        "instructions": "",
        "glossary": "",
        "char_limit": 6000,
        "max_segments_per_batch": 40,
        "llm_concurrent_calls": 1,
        "timing_context_mode": "full",
        "substantial_gap_ms": 2000,
        "context_before": 8,
        "context_after": 2,
        "no_remove_subtitles": False,
        "context": True,
        "glossary_enabled": False,
        "request_timeout_seconds": 600,
        "web_research_enabled": False,
        "web_research_provider": "jina",
        "web_research_model_name": "",
        "web_research_mode": "global",
        "web_research_context_fraction": 0.8,
        "web_research_language": "",
        "web_research_max_searches": 3,
        "web_research_max_extractions": 2,
        "web_research_preferred_domains": "",
        "web_research_blocked_domains": "",
        "web_research_max_iterations": 8,
        "web_research_timeout_seconds": 90,
        "web_research_source_chars": 14000,
        "web_research_result_chars": 10000,
    },
    "tts": {
        "service": "XTTS",
        "use_external_server": False,
        "external_server_url": "",
        "model": "",
        "language": "en",
        "voice": "",
        "speed": 1.0,
        "max_attempts": 5,
        "tts_batch_size": 10,
        "temperature": 0.75,
        "length_penalty": 1.0,
        "repetition_penalty": 5.0,
        "top_k": 50,
        "top_p": 0.85,
        "do_sample": True,
        "num_beams": 1,
        "enable_text_splitting": True,
        "stream_chunk_size": 100,
        "gpt_cond_len": 12,
        "gpt_cond_chunk_len": 4,
        "max_ref_len": 12,
        "sound_norm_refs": False,
        "overlap_wav_len": 1024,
        "xtts_send_temperature": False,
        "xtts_send_length_penalty": False,
        "xtts_send_repetition_penalty": False,
        "xtts_send_top_k": False,
        "xtts_send_top_p": False,
        "xtts_send_do_sample": False,
        "xtts_send_num_beams": False,
        "xtts_send_stream_chunk_size": False,
        "xtts_send_enable_text_splitting": False,
        "xtts_send_gpt_cond_len": False,
        "xtts_send_gpt_cond_chunk_len": False,
        "xtts_send_max_ref_len": False,
        "xtts_send_sound_norm_refs": False,
        "xtts_send_overlap_wav_len": False,
        "voxcpm_cfg_value": 1.5,
        "voxcpm_inference_timesteps": 15,
        "voxcpm_normalize": False,
        "voxcpm_denoise": False,
        "voxcpm_retry_badcase": True,
        "voxcpm_retry_badcase_max_times": 3,
        "voxcpm_retry_badcase_ratio_threshold": 6.0,
        "voxcpm_min_len": 2,
        "voxcpm_max_len": 4096,
        "fishs2_temperature": 0.7,
        "fishs2_top_p": 0.7,
        "fishs2_chunk_length": 200,
        "fishs2_latency": "balanced",
        "fishs2_normalize": True,
        "fishs2_prosody_volume": 0,
        "fishs2_normalize_loudness": True,
        "kokoro_default_voices": {"en": "af_heart"},
        "silero_stress_mode": "auto",
        "silero_sample_rate": 48000,
        "voxtral_max_frames": 1024,
        "voxtral_euler_steps": 8,
        "voxtral_chunk": False,
        "voxtral_max_chunk_chars": 500,
        "voxtral_chunk_silence_ms": 0,
        "voxtral_strip_quotes": False,
        "voxtral_strip_diacritics": False,
        "voxtral_level_audio": False,
        "chatterbox_temperature": 0.8,
        "chatterbox_repetition_penalty": 1.2,
        "chatterbox_min_p": 0.05,
        "chatterbox_top_p": 0.95,
        "chatterbox_top_k": 1000,
        "chatterbox_exaggeration": 0.5,
        "chatterbox_cfg_weight": 0.5,
        "chatterbox_norm_loudness": True,
        "openai_audio_endpoint": "",
        "openai_audio_instructions": "",
        "generation_prompt": "",
        "speech_block_min_chars": 10,
        "speech_block_max_chars": 220,
        "speech_block_merge_threshold": 250,
        "speech_block_continuation_threshold_ms": 3000,
        "speech_block_max_internal_gap_ms": 1800,
    },
    "audio": {
        "audio_verification_mode": "off",
        "sentence_silence_ms": 250,
        "paragraph_silence_ms": 700,
        "fade_enabled": False,
        "fade_in_ms": 0,
        "fade_out_ms": 0,
        "synchronization_delay_ms": 800,
        "synchronization_speed": 1.2,
        "synchronization_sentence_gap_ms": 100,
    },
    "rvc": {
        "enabled": False,
        "model": "",
        "pitch": 0,
        "filter_radius": 3,
        "index_rate": 0.3,
        "volume_envelope": 1.0,
        "protect": 0.3,
        "f0_method": "rmvpe",
    },
    "source_cleaning": {
        "agentic": False,
        "max_iterations": 53,
        "pdf_ocr_mode": "auto",
        "pdf_ocr_language": "auto",
        "pdf_ocr_dpi": 200,
        "pdf_remove_toc": True,
        "pdf_remove_repeated_marginals": True,
        "remove_footnotes": False,
        "filter_citations": True,
        "phase_max_iterations": {},
        "request_timeout_seconds": 600,
    },
    "output": {
        "format": "wav",
        "bitrate": "192k",
        "export_mode": "media",
        "audio_mode": "mixed",
        "subtitle_mode": "none",
        "subtitle_selection": "translation",
        "subtitle_format": "srt",
        "video_transcode": False,
        "burn_video_encoder": "libx264",
        "burn_video_resolution": "source",
        "burn_video_quality": 18,
        "burn_video_speed": "balanced",
        "burn_audio_codec": "copy",
        "burn_audio_bitrate": "192k",
        "mix_source_gain_db": 0.0,
        "mix_voice_gain_db": 0.0,
        "mix_voice_lufs": -16.0,
        "mix_ducking": "strong",
        "mix_attack_ms": 25,
        "mix_release_ms": 350,
        "mix_audio_bitrate": "192k",
        "title": "",
        "artist": "",
        "album": "",
        "genre": "Audiobook",
        "language": "",
        "cover_artifact_id": "",
    },
}


# The web settings API deliberately uses concise, presentation-friendly names.
# Existing generation/transcription logic predates that API and consumes the Qt
# names below.  Keep the translation at the service boundary so persisted web
# records remain stable and every handler receives one canonical runtime shape.
RUNTIME_SETTING_ALIASES: dict[str, dict[str, str]] = {
    "tts": {"model": "xtts_model", "voice": "speaker"},
    "rvc": {"enabled": "enable_rvc", "model": "rvc_model"},
    "audio": {
        "sentence_silence_ms": "silence_between_sentences",
        "paragraph_silence_ms": "silence_for_paragraphs",
        "fade_enabled": "enable_fade",
        "fade_in_ms": "fade_in_duration",
        "fade_out_ms": "fade_out_duration",
    },
    "subtitles": {
        "max_lines": "subtitle_max_lines",
        "max_chars_per_line": "subtitle_max_chars_per_line",
        "max_cps": "subtitle_max_cps",
        "min_duration_ms": "subtitle_min_duration_ms",
        "max_duration_ms": "subtitle_max_duration_ms",
        "min_gap_ms": "subtitle_min_gap_ms",
        "phrase_gap_ms": "subtitle_phrase_gap_ms",
        "hard_gap_ms": "subtitle_hard_gap_ms",
        "sentence_boundary_threshold": "subtitle_sentence_boundary_threshold",
        "merge_threshold_ms": "subtitle_merge_threshold",
    },
    "correction": {
        "enabled": "correction_enabled",
        "model_name": "correction_model",
        "instructions": "custom_correction_prompt",
        "char_limit": "llm_char",
        "max_segments_per_batch": "max_subtitles_per_call",
        "substantial_gap_ms": "timing_context_gap_ms",
    },
    "translation": {
        "enabled": "translation_enabled",
        "backend": "translation_backend",
        "source_language": "original_language",
        "model_name": "translation_model",
        "instructions": "translate_prompt",
        "char_limit": "llm_char",
        "max_segments_per_batch": "max_subtitles_per_call",
        "substantial_gap_ms": "timing_context_gap_ms",
    },
}


def adapt_runtime_settings(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Add legacy runtime aliases without overwriting explicit expert values."""
    result = deepcopy(values or {})
    for web_key, runtime_key in RUNTIME_SETTING_ALIASES.get(section, {}).items():
        if runtime_key not in result and web_key in result:
            result[runtime_key] = deepcopy(result[web_key])
    if section in {"correction", "translation"}:
        mode = str(result.get("timing_context_mode") or "").strip().lower()
        if mode and "timing_context_enabled" not in result:
            result["timing_context_enabled"] = mode != "none"
    if section == "tts":
        # Web settings persist stable service IDs (for example ``kokoro``), while
        # the legacy synthesis boundary still dispatches on canonical labels.
        # Adapt both current and already-frozen run snapshots at that boundary.
        from pandrator.logic import tts_handler

        selected_value = str(
            result.get("service") or result.get("tts_service") or ""
        ).strip()
        selected = (
            tts_handler.get_service_config(result, selected_value)
            if selected_value
            else None
        )
        canonical = tts_handler.get_first_class_service_name(selected_value)
        if canonical:
            result["service"] = canonical
            result["tts_service"] = canonical
            if selected:
                base_url = str(selected.get("api_base") or "").strip().rstrip("/")
                base_url_keys = {
                    "XTTS": "xtts_base_url",
                    "VoxCPM": "voxcpm_base_url",
                    "FishS2": "fishs2_base_url",
                    "Voxtral": "voxtral_base_url",
                    "Kokoro": "kokoro_base_url",
                    "Silero": "silero_base_url",
                    "Chatterbox": "chatterbox_base_url",
                    "Qwen3 TTS": "kobold_qwen_base_url",
                    "Magpie": "magpie_base_url",
                }
                if base_url and canonical in base_url_keys:
                    result.setdefault(base_url_keys[canonical], base_url)
        elif selected:
            # Custom services keep their stable catalogue ID in storage but
            # use the legacy OpenAI-compatible dispatch boundary at runtime.
            result["service"] = tts_handler.OPENAI_COMPAT_SERVICE
            result["tts_service"] = tts_handler.OPENAI_COMPAT_SERVICE
            result["openai_audio_endpoint"] = str(selected.get("id") or selected_value)
    return result


SECRET_KEYS = {
    "secret",
    "password",
    "api_key",
    "token",
    "access_token",
    "refresh_token",
    "credential",
    "credentials",
}


def _merge(*values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        for key, item in (value or {}).items():
            if isinstance(item, dict) and isinstance(result.get(key), dict):
                result[key] = _merge(result[key], item)
            else:
                result[key] = deepcopy(item)
    return result


def _secret_free(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _secret_free(item)
            for key, item in value.items()
            if not (
                key.lower() in SECRET_KEYS
                or key.lower().endswith(
                    (
                        "_secret",
                        "_password",
                        "_api_key",
                        "_access_token",
                        "_refresh_token",
                        "_credential",
                        "_credentials",
                    )
                )
            )
        }
    if isinstance(value, list):
        return [_secret_free(item) for item in value]
    return value


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def mark_output_assemblies_stale(
    session, session_id: str, *, generation_run_id: str | None = None
) -> None:
    """Invalidate completed assemblies and their exports after audio-plan changes.

    Assemblies are scoped to the run whose takes were produced or replaced:
    historical assemblies of other completed runs remain previewable.  When no
    run is given (segment edits, take selection), only current-selection
    assemblies without a run are affected, because run-scoped assemblies keep
    reproducing the immutable takes of their own run.
    """
    from .artifacts import ArtifactService

    filters = [
        OutputAssembly.session_id == session_id,
        OutputAssembly.status == "completed",
    ]
    if generation_run_id is None:
        filters.append(OutputAssembly.generation_run_id.is_(None))
    else:
        filters.append(
            (OutputAssembly.generation_run_id.is_(None))
            | (OutputAssembly.generation_run_id == generation_run_id)
        )
    records = list(session.scalars(select(OutputAssembly).where(*filters)).all())
    for record in records:
        record.status = "stale"
        record.updated_at = utcnow()
        if record.artifact_id:
            artifact = session.get(Artifact, record.artifact_id)
            if artifact is not None:
                artifact.state = "stale"
                ArtifactService._mark_descendants_stale(session, artifact.id)


class RevisionConflict(ValueError):
    pass


class WorkspaceSettingsService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validate_section(section: str) -> str:
        normalized = str(section or "").strip().lower()
        if normalized not in SETTING_SECTIONS:
            raise ValueError(f"Unknown settings section: {section}")
        return normalized

    @staticmethod
    def _output_context(session, session_record: SessionRecord) -> dict[str, Any]:
        source = resolve_primary_source(session, session_record.id)
        source_profile = source.profile
        has_source_video = source.has_video
        has_source_audio = source.has_audio
        workflow_kind = session_record.workflow_kind
        available_subtitle_roles = set(
            session.scalars(
                select(Artifact.role).where(
                    Artifact.session_id == session_record.id,
                    Artifact.state == "current",
                    Artifact.role.in_(("transcription", "correction", "translation")),
                )
            ).all()
        )
        subtitle_selection = next(
            (
                selection
                for role, selection in (
                    ("translation", "translation"),
                    ("correction", "correction"),
                    ("transcription", "source"),
                )
                if role in available_subtitle_roles
            ),
            "",
        )
        has_generated_voiceover = (
            session.scalar(
                select(GenerationRun.id)
                .where(
                    GenerationRun.session_id == session_record.id,
                    GenerationRun.status == "completed",
                )
                .limit(1)
            )
            is not None
        )
        if not has_generated_voiceover:
            has_generated_voiceover = (
                session.scalar(
                    select(Artifact.id)
                    .where(
                        Artifact.session_id == session_record.id,
                        Artifact.state == "current",
                        Artifact.role.in_(("assembled_audio", "dubbing_audio")),
                    )
                    .limit(1)
                )
                is not None
            )
        if workflow_kind == "audiobook":
            applicable_groups = ["audiobook_audio", "audiobook_metadata", "cover"]
        elif workflow_kind == "subtitles":
            applicable_groups = ["export_target", "subtitle_document"]
        else:
            applicable_groups = ["export_target", "subtitle_document"]
            if has_source_video:
                applicable_groups.extend(["video_audio", "video_subtitles", "mix"])
            elif has_source_audio:
                applicable_groups.extend(["standalone_audio", "mix"])
            else:
                applicable_groups.append("standalone_audio")
        return {
            "workflow_kind": workflow_kind,
            "source_profile": source_profile,
            "source_name": source.name,
            "source_kind": source.kind,
            "source_mime_type": source.mime_type,
            "source_artifact_id": source.artifact.id if source.artifact else None,
            "source_resolution": source.resolution,
            "has_source_video": has_source_video,
            "has_source_audio": has_source_audio,
            "has_generated_voiceover": has_generated_voiceover,
            "subtitle_selection": subtitle_selection or None,
            "applicable_groups": applicable_groups,
        }

    def get(self, session_id: str, section: str) -> dict[str, Any]:
        section = self._validate_section(section)
        with self.database.session() as session:
            return self.get_in_session(session, session_id, section)

    def get_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
    ) -> dict[str, Any]:
        """Return the complete settings representation from one transaction."""

        section = self._validate_section(section)
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        global_record = session.get(AppSetting, f"defaults.{section}")
        override = session.get(SessionSetting, (session_id, section))
        global_value = (
            global_record.value_json
            if global_record and isinstance(global_record.value_json, dict)
            else {}
        )
        override_value = override.value_json if override else {}
        source_language = str(session_record.source_language or "auto")
        target_language = str(session_record.target_language or "")
        outcome = session.get(OutcomePlan, session_id)
        outcome_value = (
            outcome.value_json
            if outcome and isinstance(outcome.value_json, dict)
            else {}
        )
        inputs = (
            outcome_value.get("inputs")
            if isinstance(outcome_value.get("inputs"), dict)
            else {}
        )
        generation_input = str(inputs.get("generation") or "").strip().lower()
        if not generation_input:
            has_translation = (
                session.scalar(
                    select(Artifact.id)
                    .where(
                        Artifact.session_id == session_id,
                        Artifact.role == "translation",
                        Artifact.state == "current",
                    )
                    .limit(1)
                )
                is not None
            )
            generation_input = "translation" if has_translation else "source"
        speech_language = (
            target_language
            if generation_input == "translation" and target_language
            else source_language
            if source_language != "auto"
            else ""
        )
        session_context: dict[str, Any] = {}
        output_context: dict[str, Any] = {}
        if section == "stt":
            session_context = {"stt_language": source_language}
        elif section == "translation":
            session_context = {
                "source_language": source_language,
                **({"target_language": target_language} if target_language else {}),
            }
        elif section == "tts" and speech_language:
            session_context = {"language": speech_language}
        elif section == "output":
            output_context = self._output_context(session, session_record)
            # A subtitle workspace should produce a portable subtitle file
            # without requiring users to opt out of application-wide media
            # defaults. Session overrides still win when deliberately set.
            if session_record.workflow_kind == "subtitles":
                session_context = {
                    "export_mode": "subtitles",
                    "audio_mode": "preserve",
                    "subtitle_mode": "none",
                    "subtitle_selection": "source",
                }
            elif session_record.workflow_kind == "voiceover":
                subtitle_first = bool(
                    output_context["has_source_audio"]
                    and output_context["subtitle_selection"]
                    and not output_context["has_generated_voiceover"]
                )
                session_context = {
                    "export_mode": "media",
                    "audio_mode": (
                        "preserve"
                        if subtitle_first
                        else "mixed"
                        if output_context["has_source_audio"]
                        else "dubbing_only"
                    ),
                    "format": "wav",
                }
                if subtitle_first:
                    session_context.update(
                        {
                            "subtitle_selection": output_context["subtitle_selection"],
                            "subtitle_mode": (
                                "soft" if output_context["has_source_video"] else "none"
                            ),
                        }
                    )
            if speech_language:
                session_context["language"] = speech_language
        effective = _merge(
            BUILTIN_DEFAULTS[section],
            global_value,
            session_context,
            override_value,
        )
        if section == "output" and session_record.workflow_kind == "subtitles":
            if str(effective.get("export_mode") or "").lower() not in {
                "subtitles",
                "text",
            }:
                effective["export_mode"] = "subtitles"
            effective["audio_mode"] = "preserve"
            effective["subtitle_mode"] = "none"
        elif section == "output" and session_record.workflow_kind == "voiceover":
            if str(effective.get("export_mode") or "").lower() not in {
                "media",
                "subtitles",
                "text",
            }:
                effective["export_mode"] = "media"
            if not output_context["has_source_audio"]:
                effective["audio_mode"] = "dubbing_only"
            elif str(effective.get("audio_mode") or "").lower() not in {
                "preserve",
                "mixed",
                "dubbing_only",
            }:
                effective["audio_mode"] = "mixed"
            if output_context["has_source_video"]:
                # Video uses a lossless assembly intermediate; the final MP4
                # path encodes new audio as AAC when replacement/mixing occurs.
                effective["format"] = "wav"
            elif str(effective.get("format") or "").lower() not in {
                "wav",
                "mp3",
                "opus",
                "flac",
            }:
                effective["format"] = "wav"
        return {
            "section": section,
            "builtin": deepcopy(BUILTIN_DEFAULTS[section]),
            "global": deepcopy(global_value),
            "override": deepcopy(override_value),
            "session_context": session_context,
            "effective": effective,
            "context": output_context,
            "revision": override.revision if override else 0,
            "global_revision": global_record.revision if global_record else 0,
        }

    def update(
        self,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
        *,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        section = self._validate_section(section)
        if db_session is not None:
            self.update_in_session(
                db_session,
                session_id,
                section,
                expected_revision,
                value,
            )
            return self.get_in_session(db_session, session_id, section)
        with self.database.session() as session:
            result = self.update_in_session(
                session,
                session_id,
                section,
                expected_revision,
                value,
            )
            revision = int(result["revision"])
        return self.get(session_id, section) | {"revision": revision}

    def update_in_session(
        self,
        session: Session,
        session_id: str,
        section: str,
        expected_revision: int,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        section = self._validate_section(section)
        session_record = session.get(SessionRecord, session_id)
        if session_record is None:
            raise KeyError(session_id)
        value = dict(value)
        if section == "output" and session_record.workflow_kind != "audiobook":
            for key in (
                "title",
                "artist",
                "album",
                "genre",
                "cover_artifact_id",
            ):
                value.pop(key, None)
            output_context = self._output_context(
                session,
                session_record,
            )
            if session_record.workflow_kind == "subtitles":
                allowed = {
                    "export_mode",
                    "subtitle_selection",
                    "subtitle_format",
                    "language",
                }
                value = {key: item for key, item in value.items() if key in allowed}
            else:
                if output_context["has_source_video"]:
                    value.pop("format", None)
                    value.pop("bitrate", None)
                else:
                    for key in (
                        "subtitle_mode",
                        "video_transcode",
                        "burn_video_encoder",
                        "burn_video_resolution",
                        "burn_video_quality",
                        "burn_video_speed",
                        "burn_audio_codec",
                        "burn_audio_bitrate",
                    ):
                        value.pop(key, None)
                if not output_context["has_source_audio"]:
                    value.pop("audio_mode", None)
                    for key in (
                        "mix_source_gain_db",
                        "mix_voice_gain_db",
                        "mix_voice_lufs",
                        "mix_ducking",
                        "mix_attack_ms",
                        "mix_release_ms",
                        "mix_audio_bitrate",
                    ):
                        value.pop(key, None)
        record = session.get(
            SessionSetting,
            (session_id, section),
        )
        if record is None:
            if expected_revision != 0:
                raise RevisionConflict(
                    "Session settings were created in another client."
                )
            record = SessionSetting(
                session_id=session_id,
                section=section,
                value_json=value,
                revision=1,
            )
            session.add(record)
        else:
            if expected_revision != record.revision:
                raise RevisionConflict("Session settings changed in another client.")
            session.add(
                SessionSettingHistory(
                    session_id=session_id,
                    section=section,
                    value_json=record.value_json,
                    revision=record.revision,
                )
            )
            record.value_json = value
            record.revision += 1
            record.updated_at = utcnow()
        session.flush()
        return {
            "section": section,
            "override": deepcopy(value),
            "revision": record.revision,
        }

    def resolve(
        self,
        session_id: str,
        sections: list[str] | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        requested = sections or list(SETTING_SECTIONS)
        override = run_override or {}
        snapshots = {section: self.get(session_id, section) for section in requested}
        resolved = {
            section: _merge(snapshots[section]["effective"], override.get(section, {}))
            for section in requested
        }
        if "tts" in resolved:
            with self.database.session() as session:
                connections = session.get(AppSetting, "services.tts")
                connection_value = (
                    connections.value_json
                    if connections and isinstance(connections.value_json, dict)
                    else {}
                )
            from pandrator.logic import tts_handler

            snapshot = snapshots["tts"]
            selection_seed = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("tts", {}),
            )
            selected = tts_handler.get_service_config(
                selection_seed, str(selection_seed.get("service") or "XTTS")
            )
            provider_defaults = (
                selected.get("settings")
                if selected and isinstance(selected.get("settings"), dict)
                else {}
            )
            resolved["tts"] = _merge(
                snapshot["builtin"],
                snapshot["global"],
                connection_value,
                provider_defaults,
                snapshot.get("session_context", {}),
                snapshot["override"],
                override.get("tts", {}),
            )
            if selected:
                if str(selected.get("id") or "").lower() == "kobold_qwen":
                    model = tts_handler.resolve_kobold_qwen_model(
                        resolved["tts"],
                        fallback=str(selected.get("default_model") or ""),
                    )
                else:
                    model = str(
                        resolved["tts"].get("model")
                        or selected.get("default_model")
                        or ""
                    )
                default_voices = (
                    selected.get("default_voices")
                    if isinstance(selected.get("default_voices"), dict)
                    else {}
                )
                language_defaults = (
                    selected.get("default_voices_by_language")
                    if isinstance(selected.get("default_voices_by_language"), dict)
                    else {}
                )
                model_language_defaults = (
                    language_defaults.get(model)
                    if isinstance(language_defaults.get(model), dict)
                    else {}
                )
                language = (
                    str(
                        resolved["tts"].get("language")
                        or resolved["tts"].get("target_language")
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if str(selected.get("id") or "").lower() == "kokoro":
                    language = tts_handler.normalize_kokoro_language_code(language)
                voice = str(
                    resolved["tts"].get("voice")
                    or model_language_defaults.get(language)
                    or default_voices.get(model)
                    or selected.get("default_voice")
                    or ""
                )
                if model:
                    resolved["tts"]["model"] = model
                if voice:
                    resolved["tts"]["voice"] = voice
        safe = _secret_free(resolved)
        return safe, stable_hash(safe)


def derive_legacy_outcome(record: SessionRecord) -> dict[str, Any]:
    included = set(record.included_stages_json or [])
    kind = record.workflow_kind
    return {
        "version": 1,
        "workflow_kind": kind,
        "focus": "custom" if record.workflow_preset == "custom" else "guided",
        "deliverables": {
            "audiobook": kind == "audiobook",
            "subtitles": kind == "subtitles" or "export" in included,
            "voiceover": kind == "voiceover" or "generate_audio" in included,
        },
        "transformations": {
            "transcribe": "transcribe" in included,
            "correct": "correct" in included,
            "translate": "translate" in included,
            "deterministic_normalization": True,
            "llm_tts_optimization": False,
            "llm_tts_document_optimization": False,
            "generate_audio": "generate_audio" in included or kind == "audiobook",
            "rvc": False,
        },
        "inputs": {
            "translation": "correction" if "correct" in included else "source",
            "generation": "translation"
            if "translate" in included
            else "correction"
            if "correct" in included
            else "source",
        },
        "export": {
            "audio": "generated" if kind in {"audiobook", "voiceover"} else "preserve",
            "subtitles": "translation" if "translate" in included else "source",
        },
    }


def resolve_pipeline(
    plan: dict[str, Any], *, source_requires_transcription: bool = False
) -> list[dict[str, str]]:
    kind = str(plan.get("workflow_kind") or "audiobook")
    transformations = (
        plan.get("transformations")
        if isinstance(plan.get("transformations"), dict)
        else {}
    )
    deliverables = (
        plan.get("deliverables") if isinstance(plan.get("deliverables"), dict) else {}
    )
    stages: list[tuple[str, str]] = []
    if kind == "audiobook":
        stages.append(("clean_source", "Clean source"))
        stages.append(("prepare_text", "Segment narration"))
    elif source_requires_transcription or transformations.get("transcribe"):
        stages.append(("transcribe", "Transcribe"))
    if transformations.get("correct"):
        stages.append(("correct", "Correct subtitles"))
    if transformations.get("translate"):
        stages.append(("translate", "Translate"))
    if transformations.get("llm_tts_document_optimization") or transformations.get(
        "llm_tts_optimization"
    ):
        timing = (
            "before generation"
            if transformations.get("llm_tts_document_optimization")
            else "while generating"
        )
        stages.append(("optimize_tts", f"Optimize for speech {timing}"))
    if (
        transformations.get("generate_audio")
        or deliverables.get("audiobook")
        or deliverables.get("voiceover")
    ):
        stages.append(("generate_audio", "Generate audio"))
    if transformations.get("rvc"):
        stages.append(("apply_rvc", "Apply RVC"))
    if any(bool(value) for value in deliverables.values()):
        stages.append(("export", "Export"))
    return [{"key": key, "title": title} for key, title in stages]


class OutcomePlanService:
    def __init__(self, database: Database):
        self.database = database

    def get(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            plan = session.get(OutcomePlan, session_id)
            if plan is None:
                plan = OutcomePlan(
                    session_id=session_id, value_json=derive_legacy_outcome(record)
                )
                session.add(plan)
                session.flush()
            value = deepcopy(plan.value_json)
            revision = plan.revision
        return {
            "value": value,
            "revision": revision,
            "pipeline": resolve_pipeline(value),
        }

    def update(
        self, session_id: str, expected_revision: int, value: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            plan = session.get(OutcomePlan, session_id)
            previous_value = deepcopy(plan.value_json) if plan is not None else {}
            if plan is None:
                if expected_revision != 0:
                    raise RevisionConflict(
                        "The workflow plan was created in another client."
                    )
                plan = OutcomePlan(session_id=session_id, value_json=value, revision=1)
                session.add(plan)
            else:
                if expected_revision != plan.revision:
                    raise RevisionConflict(
                        "The workflow plan changed in another client."
                    )
                session.add(
                    OutcomePlanHistory(
                        session_id=session_id,
                        value_json=plan.value_json,
                        revision=plan.revision,
                    )
                )
                plan.value_json = value
                plan.revision += 1
                plan.updated_at = utcnow()
            previous_inputs = (
                previous_value.get("inputs")
                if isinstance(previous_value.get("inputs"), dict)
                else {}
            )
            next_inputs = (
                value.get("inputs") if isinstance(value.get("inputs"), dict) else {}
            )
            if str(previous_inputs.get("translation") or "correction") != str(
                next_inputs.get("translation") or "correction"
            ):
                # The chosen translation and speech-optimized descendants may
                # belong to the other source branch. Preserve their immutable
                # history, but do not continue presenting that branch as the
                # selected/current workflow result.
                from .artifact_selection import clear_selection

                clear_selection(session, session_id, "translate")
            record.workflow_kind = str(
                value.get("workflow_kind") or record.workflow_kind
            )
            record.workflow_preset = "custom"
            pipeline_keys = {item["key"] for item in resolve_pipeline(value)}
            record.included_stages_json = [
                key
                for key in (
                    "transcribe",
                    "correct",
                    "translate",
                    "optimize_tts",
                    "generate_audio",
                    "export",
                )
                if key in pipeline_keys
            ]
            record.revision += 1
            record.updated_at = utcnow()
            session.flush()
            revision = plan.revision
        return {
            "value": deepcopy(value),
            "revision": revision,
            "pipeline": resolve_pipeline(value),
        }


class SourceLibraryService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _asset_payload(
        asset: SourceAsset,
        *,
        reference_count: int = 0,
        current_reference_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "id": asset.id,
            "artifact_id": asset.artifact_id,
            "display_name": asset.display_name,
            "kind": asset.kind,
            "mime_type": asset.mime_type,
            "external_path": asset.external_path,
            "size_bytes": asset.size_bytes,
            "content_hash": asset.content_hash,
            "state": asset.state,
            "metadata": asset.metadata_json,
            "revision": asset.revision,
            "reference_count": reference_count,
            "current_reference_count": current_reference_count,
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat(),
        }

    def ensure_for_artifact(
        self,
        artifact_id: str,
        *,
        display_name: str | None = None,
        kind: str | None = None,
    ) -> SourceAsset:
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            asset = session.scalar(
                select(SourceAsset).where(SourceAsset.artifact_id == artifact_id)
            )
            if asset is None:
                name = display_name or str(
                    (artifact.metadata_json or {}).get("original_filename")
                    or Path(artifact.relative_path).name
                )
                asset = SourceAsset(
                    artifact_id=artifact.id,
                    display_name=name,
                    kind=kind or Path(name).suffix.lower().lstrip(".") or artifact.kind,
                    mime_type=artifact.mime_type,
                    size_bytes=artifact.size_bytes,
                    content_hash=artifact.content_hash,
                    metadata_json={"legacy_session_id": artifact.session_id}
                    if artifact.session_id
                    else {},
                )
                session.add(asset)
                session.flush()
            session.expunge(asset)
            return asset

    def attach(
        self,
        session_id: str,
        source_asset_id: str,
        *,
        role: str = "primary",
        expected_session_revision: int | None = None,
        db_session: Session | None = None,
    ) -> dict[str, Any]:
        if db_session is not None:
            return self.attach_in_session(
                db_session,
                session_id,
                source_asset_id,
                role=role,
                expected_session_revision=(expected_session_revision),
            )
        with self.database.session() as session:
            return self.attach_in_session(
                session,
                session_id,
                source_asset_id,
                role=role,
                expected_session_revision=(expected_session_revision),
            )

    @staticmethod
    def attach_in_session(
        session: Session,
        session_id: str,
        source_asset_id: str,
        *,
        role: str = "primary",
        expected_session_revision: int | None = None,
    ) -> dict[str, Any]:
        session_record = session.get(SessionRecord, session_id)
        asset = session.get(SourceAsset, source_asset_id)
        if session_record is None or asset is None:
            raise KeyError(session_id)
        if (
            expected_session_revision is not None
            and session_record.revision != expected_session_revision
        ):
            raise RevisionConflict(
                "The session changed before its source was attached."
            )
        for current in session.scalars(
            select(SessionSource).where(
                SessionSource.session_id == session_id,
                SessionSource.role == role,
                SessionSource.is_current.is_(True),
            )
        ).all():
            current.is_current = False
            current.revision += 1
            current.updated_at = utcnow()
        attachment = session.scalar(
            select(SessionSource).where(
                SessionSource.session_id == session_id,
                SessionSource.source_asset_id == source_asset_id,
                SessionSource.role == role,
            )
        )
        if attachment is None:
            attachment = SessionSource(
                session_id=session_id,
                source_asset_id=source_asset_id,
                role=role,
            )
            session.add(attachment)
        else:
            attachment.is_current = True
            attachment.revision += 1
            attachment.updated_at = utcnow()
        if role == "primary":
            select_source_path(
                session,
                session_id,
                asset.artifact_id,
            )
        if expected_session_revision is not None:
            session_record.revision += 1
            session_record.updated_at = utcnow()
        session.flush()
        return {
            "id": attachment.id,
            "session_id": session_id,
            "source_asset_id": source_asset_id,
            "role": role,
            "is_current": True,
            "revision": attachment.revision,
            "session_revision": session_record.revision,
        }

    def detach(
        self, session_id: str, attachment_id: str, expected_revision: int
    ) -> None:
        with self.database.session() as session:
            attachment = session.get(SessionSource, attachment_id)
            if attachment is None or attachment.session_id != session_id:
                raise KeyError(attachment_id)
            if attachment.revision != expected_revision:
                raise RevisionConflict(
                    "The source attachment changed in another client."
                )
            if attachment.role == "primary" and attachment.is_current:
                select_source_path(session, session_id, None)
            session.delete(attachment)

    def rename(
        self, source_asset_id: str, expected_revision: int, display_name: str
    ) -> dict[str, Any]:
        with self.database.session() as session:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise KeyError(source_asset_id)
            if asset.revision != expected_revision:
                raise RevisionConflict("The source asset changed in another client.")
            asset.display_name = display_name.strip()
            asset.revision += 1
            asset.updated_at = utcnow()
            session.flush()
            references = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(SessionSource.source_asset_id == asset.id)
                )
                or 0
            )
            current = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(
                        SessionSource.source_asset_id == asset.id,
                        SessionSource.is_current.is_(True),
                    )
                )
                or 0
            )
            return self._asset_payload(
                asset, reference_count=references, current_reference_count=current
            )

    def set_state(
        self, source_asset_id: str, expected_revision: int, state: str
    ) -> dict[str, Any]:
        if state not in {"current", "trashed"}:
            raise ValueError("Unsupported source lifecycle state.")
        with self.database.session() as session:
            asset = session.get(SourceAsset, source_asset_id)
            if asset is None:
                raise KeyError(source_asset_id)
            if asset.revision != expected_revision:
                raise RevisionConflict("The source asset changed in another client.")
            references = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(SessionSource.source_asset_id == asset.id)
                )
                or 0
            )
            current = int(
                session.scalar(
                    select(func.count())
                    .select_from(SessionSource)
                    .where(
                        SessionSource.source_asset_id == asset.id,
                        SessionSource.is_current.is_(True),
                    )
                )
                or 0
            )
            if state == "trashed" and references:
                raise ValueError(
                    f"Detach this source from {references} session attachment(s) before moving it to trash."
                )
            asset.state = state
            asset.revision += 1
            asset.updated_at = utcnow()
            session.flush()
            return self._asset_payload(
                asset, reference_count=references, current_reference_count=current
            )

    def list(
        self, *, session_id: str | None = None, include_trashed: bool = False
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            if session_id:
                rows = session.execute(
                    select(SessionSource, SourceAsset)
                    .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
                    .where(SessionSource.session_id == session_id)
                    .order_by(SessionSource.updated_at.desc())
                ).all()
                asset_ids = {asset.id for _link, asset in rows}
                counts = (
                    dict(
                        session.execute(
                            select(SessionSource.source_asset_id, func.count())
                            .where(SessionSource.source_asset_id.in_(asset_ids))
                            .group_by(SessionSource.source_asset_id)
                        ).all()
                    )
                    if asset_ids
                    else {}
                )
                current_counts = (
                    dict(
                        session.execute(
                            select(SessionSource.source_asset_id, func.count())
                            .where(
                                SessionSource.source_asset_id.in_(asset_ids),
                                SessionSource.is_current.is_(True),
                            )
                            .group_by(SessionSource.source_asset_id)
                        ).all()
                    )
                    if asset_ids
                    else {}
                )
                return [
                    self._asset_payload(
                        asset,
                        reference_count=int(counts.get(asset.id, 0)),
                        current_reference_count=int(current_counts.get(asset.id, 0)),
                    )
                    | {
                        "attachment": {
                            "id": link.id,
                            "role": link.role,
                            "is_current": link.is_current,
                            "revision": link.revision,
                        }
                    }
                    for link, asset in rows
                ]
            statement = select(SourceAsset).order_by(SourceAsset.updated_at.desc())
            if not include_trashed:
                statement = statement.where(SourceAsset.state != "trashed")
            assets = list(session.scalars(statement).all())
            counts = dict(
                session.execute(
                    select(SessionSource.source_asset_id, func.count()).group_by(
                        SessionSource.source_asset_id
                    )
                ).all()
            )
            current_counts = dict(
                session.execute(
                    select(SessionSource.source_asset_id, func.count())
                    .where(SessionSource.is_current.is_(True))
                    .group_by(SessionSource.source_asset_id)
                ).all()
            )
            return [
                self._asset_payload(
                    asset,
                    reference_count=int(counts.get(asset.id, 0)),
                    current_reference_count=int(current_counts.get(asset.id, 0)),
                )
                for asset in assets
            ]

    def backfill_legacy(self) -> int:
        """Deprecated compatibility hook.

        Migration ``0022_source_asset_backfill`` owns this promotion for normal
        installations.  The method remains temporarily available to older
        integrations, but application startup must not call it.
        """
        created = 0
        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(SourceRecord).where(SourceRecord.artifact_id.is_not(None))
                ).all()
            )
            for legacy in records:
                artifact = session.get(Artifact, legacy.artifact_id)
                if artifact is None:
                    continue
                asset = session.scalar(
                    select(SourceAsset).where(SourceAsset.artifact_id == artifact.id)
                )
                if asset is None:
                    asset = SourceAsset(
                        artifact_id=artifact.id,
                        display_name=legacy.display_name,
                        kind=legacy.kind,
                        mime_type=artifact.mime_type,
                        size_bytes=artifact.size_bytes,
                        content_hash=artifact.content_hash,
                        metadata_json={"legacy_source_id": legacy.id},
                    )
                    session.add(asset)
                    session.flush()
                    created += 1
                link = session.scalar(
                    select(SessionSource).where(
                        SessionSource.session_id == legacy.session_id,
                        SessionSource.source_asset_id == asset.id,
                        SessionSource.role == "primary",
                    )
                )
                if link is None:
                    session.add(
                        SessionSource(
                            session_id=legacy.session_id,
                            source_asset_id=asset.id,
                            role="primary",
                        )
                    )
        return created


class GenerationService:
    def __init__(
        self,
        database: Database,
        jobs: JobQueue,
        settings: WorkspaceSettingsService,
        artifacts=None,
        plan_refresher: Callable[[str, dict[str, Any]], str | None] | None = None,
    ):
        self.database = database
        self.jobs = jobs
        self.settings = settings
        self.artifacts = artifacts
        self.plan_refresher = plan_refresher

    def _selected_tts_provider_binding(
        self,
        session_id: str,
        selected_segment_override: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Copy only the selected provider's current, secret-free catalogue record.

        Targeted regeneration starts from an immutable historical run.  Its
        TTS catalogue may predate a provider that is now configured in the
        session, so carry a binding for the selected provider into the new run
        without copying the current provider catalogue or any inline secret.
        Runtime hydration resolves the retained ``secret_ref`` when the worker
        executes the run.
        """
        selected_tts = selected_segment_override.get("tts")
        if not isinstance(selected_tts, dict) or not selected_tts:
            return None

        from pandrator.logic import tts_handler

        resolved, _settings_hash = self.settings.resolve(
            session_id,
            ["tts"],
            run_override={"tts": selected_tts},
        )
        current_tts = resolved.get("tts")
        if not isinstance(current_tts, dict):
            return None
        service_value = str(
            selected_tts.get("service")
            or selected_tts.get("tts_service")
            or ""
        ).strip()
        endpoint_value = str(
            selected_tts.get("openai_audio_endpoint") or ""
        ).strip()
        generic_service = service_value.casefold().replace("-", "_") in {
            "custom",
            "openai_compatible",
            "openai_compatible_service",
        }
        candidates = (
            [endpoint_value, service_value]
            if generic_service and endpoint_value
            else [service_value]
        )
        selected = next(
            (
                service
                for candidate in candidates
                if candidate
                for service in [
                    tts_handler.get_service_config(current_tts, candidate)
                ]
                if service is not None
            ),
            None,
        )
        if selected is None:
            return None
        selected_id = str(selected.get("id") or "").strip()
        if not selected_id:
            return None

        configured_records = [
            item
            for key in ("provider_configs", "service_configs")
            for item in (current_tts.get(key) or [])
            if isinstance(item, dict)
        ]
        configured = next(
            (
                item
                for item in configured_records
                if str(
                    item.get("id") or item.get("name") or item.get("provider") or ""
                )
                .strip()
                .casefold()
                .replace("-", "_")
                == selected_id.casefold().replace("-", "_")
            ),
            None,
        )
        # A built-in provider without an explicit current record should remain
        # eligible for its normal defaults/Manager binding.  Custom providers,
        # and explicitly configured built-ins, need the copied record.
        if not bool(selected.get("is_custom")) and configured is None:
            return None
        return _secret_free(deepcopy(configured or selected))

    def create_plan(
        self,
        session_id: str,
        *,
        source_revision_id: str | None,
        segments: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_segments = [
            item for item in segments if str(item.get("text") or "").strip()
        ]
        if not clean_segments:
            raise ValueError("At least one generation segment is required.")
        content = {
            "source_revision_id": source_revision_id,
            "segments": clean_segments,
            "settings": settings or {},
        }
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            if (
                source_revision_id
                and session.get(DocumentRevision, source_revision_id) is None
            ):
                raise KeyError(source_revision_id)
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            if plan is None:
                plan = GenerationPlan(session_id=session_id)
                session.add(plan)
                session.flush()
            revision_number = (
                int(
                    session.scalar(
                        select(func.max(GenerationPlanRevision.revision_number)).where(
                            GenerationPlanRevision.plan_id == plan.id
                        )
                    )
                    or 0
                )
                + 1
            )
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                source_revision_id=source_revision_id,
                revision_number=revision_number,
                settings_json=settings or {},
                content_hash=stable_hash(content),
            )
            session.add(revision)
            session.flush()
            for index, item in enumerate(clean_segments):
                session.add(
                    GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=index,
                        source_segment_ids_json=list(
                            item.get("source_segment_ids") or []
                        ),
                        alignment_group=str(item.get("alignment_group") or "").strip()
                        or None,
                        node_kind=str(
                            item.get("node_kind")
                            or (
                                "chapter_marker"
                                if str(item.get("chapter") or "").lower() == "yes"
                                else "paragraph"
                            )
                        ),
                        paragraph_break_after=bool(
                            item.get(
                                "paragraph_break_after",
                                str(item.get("paragraph") or "").lower() == "yes",
                            )
                        ),
                        speaker=str(item.get("speaker") or "").strip() or None,
                        text=str(item.get("text") or "").strip(),
                        optimized_text=(
                            str(item.get("tts_optimized_sentence") or "").strip()
                            or None
                        ),
                        speech_plan_json=dict(item.get("speech_plan") or {}),
                        optimization_status=(
                            "optimized"
                            if str(item.get("tts_optimized_sentence") or "").strip()
                            else "not_requested"
                        ),
                        optimization_source_hash=(
                            hashlib.sha256(
                                str(item.get("text") or "").strip().encode("utf-8")
                            ).hexdigest()
                            if str(item.get("tts_optimized_sentence") or "").strip()
                            else None
                        ),
                        optimization_model=(
                            str(
                                (item.get("speech_plan") or {}).get("model") or ""
                            ).strip()
                            or None
                        ),
                        voice_id=item.get("voice_id"),
                        voice=item.get("voice"),
                        language=item.get("language"),
                        silence_after_ms=max(0, int(item.get("silence_after_ms") or 0)),
                    )
                )
            plan.active_revision_id = revision.id
            plan.updated_at = utcnow()
            session.flush()
            return {
                "id": plan.id,
                "active_revision_id": revision.id,
                "revision_number": revision_number,
                "segment_count": len(clean_segments),
            }

    def list_segments(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        status: str | None = None,
        marked: bool | None = None,
        verification: str | None = None,
        generation_run_id: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 250))
        if verification not in {None, "issues"}:
            raise ValueError("verification must be 'issues' when supplied.")
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            selected_run = None
            if generation_run_id:
                selected_run = session.get(GenerationRun, generation_run_id)
                if selected_run is None or selected_run.session_id != session_id:
                    raise KeyError(generation_run_id)
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            if plan is None or not plan.active_revision_id:
                return {
                    "items": [],
                    "next_cursor": None,
                    "total": 0,
                    "plan_revision_id": None,
                }
            plan_revision_id = plan.active_revision_id
            if selected_run is not None:
                plan_revision_id = selected_run.plan_revision_id
            filters = [GenerationSegment.plan_revision_id == plan_revision_id]
            if status:
                filters.append(GenerationSegment.status == status)
            if marked is not None:
                filters.append(GenerationSegment.marked.is_(marked))
            if verification == "issues":
                verification_status = Artifact.metadata_json["audio_verification"][
                    "status"
                ].as_string()
                filters.append(
                    select(AudioTake.id)
                    .join(Artifact, Artifact.id == AudioTake.artifact_id)
                    .where(
                        AudioTake.generation_segment_id == GenerationSegment.id,
                        AudioTake.is_active.is_(True),
                        verification_status.in_(("warning", "failed")),
                    )
                    .exists()
                )
            page_filters = [*filters, GenerationSegment.ordinal >= max(0, cursor)]
            rows = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(*page_filters)
                    .order_by(GenerationSegment.ordinal)
                    .limit(limit + 1)
                ).all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            takes_by_segment: dict[str, list[AudioTake]] = {}
            artifacts_by_id: dict[str, Artifact] = {}
            if rows:
                takes = list(
                    session.scalars(
                        select(AudioTake)
                        .where(
                            AudioTake.generation_segment_id.in_(
                                [item.id for item in rows]
                            )
                        )
                        .order_by(AudioTake.created_at.desc())
                    ).all()
                )
                for take in takes:
                    takes_by_segment.setdefault(take.generation_segment_id, []).append(
                        take
                    )
                artifact_ids = [take.artifact_id for take in takes if take.artifact_id]
                if artifact_ids:
                    artifacts_by_id = {
                        artifact.id: artifact
                        for artifact in session.scalars(
                            select(Artifact).where(Artifact.id.in_(artifact_ids))
                        ).all()
                    }
            items = [
                {
                    "id": item.id,
                    "ordinal": item.ordinal,
                    "node_kind": item.node_kind,
                    "paragraph_break_after": item.paragraph_break_after,
                    "speaker": item.speaker,
                    "text": item.text,
                    "source_segment_ids": list(item.source_segment_ids_json or []),
                    "alignment_group": item.alignment_group,
                    "optimized_text": item.optimized_text,
                    "speech_plan": dict(item.speech_plan_json or {}),
                    "optimization_status": item.optimization_status,
                    "optimization_reviewed": item.optimization_reviewed,
                    "optimization_model": item.optimization_model,
                    "voice_id": item.voice_id,
                    "voice": item.voice,
                    "language": item.language,
                    "silence_after_ms": item.silence_after_ms,
                    "marked": item.marked,
                    "removed": item.removed,
                    "status": item.status,
                    "revision": item.revision,
                    "takes": [
                        {
                            "id": take.id,
                            "generation_run_id": take.generation_run_id,
                            "artifact_id": take.artifact_id,
                            "parent_take_id": take.parent_take_id,
                            "kind": take.kind,
                            "status": take.status,
                            "duration_ms": take.duration_ms,
                            "is_active": take.is_active,
                            "revision": take.revision,
                            "created_at": take.created_at.isoformat(),
                            "source_text": (
                                artifacts_by_id.get(take.artifact_id).metadata_json
                                or {}
                            ).get("source_text")
                            if take.artifact_id
                            and artifacts_by_id.get(take.artifact_id)
                            else None,
                            "synthesized_text": (
                                artifacts_by_id.get(take.artifact_id).metadata_json
                                or {}
                            ).get("synthesized_text")
                            if take.artifact_id
                            and artifacts_by_id.get(take.artifact_id)
                            else None,
                            "llm_optimized": bool(
                                (
                                    artifacts_by_id.get(take.artifact_id).metadata_json
                                    or {}
                                ).get("llm_optimized")
                            )
                            if take.artifact_id
                            and artifacts_by_id.get(take.artifact_id)
                            else False,
                            "llm_model": (
                                artifacts_by_id.get(take.artifact_id).metadata_json
                                or {}
                            ).get("llm_model")
                            if take.artifact_id
                            and artifacts_by_id.get(take.artifact_id)
                            else None,
                            "audio_verification": (
                                artifacts_by_id.get(take.artifact_id).metadata_json
                                or {}
                            ).get("audio_verification")
                            if take.artifact_id
                            and artifacts_by_id.get(take.artifact_id)
                            else None,
                        }
                        for take in takes_by_segment.get(item.id, [])
                    ],
                }
                for item in rows
            ]
            total = int(
                session.scalar(
                    select(func.count()).select_from(GenerationSegment).where(*filters)
                )
                or 0
            )
            return {
                "items": items,
                "next_cursor": rows[-1].ordinal + 1 if rows and has_more else None,
                "total": total,
                "plan_revision_id": plan_revision_id,
            }

    @staticmethod
    def _updated_segment_payload(segment: GenerationSegment) -> dict[str, Any]:
        return {
            "id": segment.id,
            "node_kind": segment.node_kind,
            "paragraph_break_after": segment.paragraph_break_after,
            "speaker": segment.speaker,
            "alignment_group": segment.alignment_group,
            "text": segment.text,
            "optimized_text": segment.optimized_text,
            "speech_plan": dict(segment.speech_plan_json or {}),
            "optimization_status": segment.optimization_status,
            "optimization_reviewed": segment.optimization_reviewed,
            "optimization_model": segment.optimization_model,
            "voice_id": segment.voice_id,
            "voice": segment.voice,
            "language": segment.language,
            "silence_after_ms": segment.silence_after_ms,
            "marked": segment.marked,
            "removed": segment.removed,
            "status": segment.status,
            "revision": segment.revision,
        }

    def _apply_segment_changes(
        self,
        session,
        segment: GenerationSegment,
        changes: dict[str, Any],
        *,
        completed_takes: list[AudioTake] | None = None,
        mark_assemblies: bool = True,
    ) -> dict[str, Any]:
        allowed = {
            "text",
            "optimized_text",
            "node_kind",
            "paragraph_break_after",
            "voice_id",
            "voice",
            "language",
            "silence_after_ms",
            "marked",
            "removed",
        }
        session.add(
            GenerationSegmentRevision(
                generation_segment_id=segment.id,
                revision=segment.revision,
                alignment_group=segment.alignment_group,
                node_kind=segment.node_kind,
                paragraph_break_after=segment.paragraph_break_after,
                speaker=segment.speaker,
                text=segment.text,
                optimized_text=segment.optimized_text,
                speech_plan_json=dict(segment.speech_plan_json or {}),
                optimization_status=segment.optimization_status,
                optimization_reviewed=segment.optimization_reviewed,
                marked=segment.marked,
                removed=segment.removed,
                voice_id=segment.voice_id,
                voice=segment.voice,
                language=segment.language,
                silence_after_ms=segment.silence_after_ms,
            )
        )
        text_changed = (
            "text" in changes and str(changes["text"]).strip() != segment.text
        )
        optimized_changed = (
            "optimized_text" in changes
            and (str(changes["optimized_text"] or "").strip() or None)
            != segment.optimized_text
        )
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key == "text":
                value = str(value).strip()
                if not value:
                    raise ValueError(
                        "Generation text cannot be blank; remove the segment instead."
                    )
            if key == "optimized_text":
                value = str(value or "").strip() or None
            if key == "silence_after_ms":
                value = max(0, int(value))
            if key == "node_kind" and value not in {
                "paragraph",
                "heading",
                "chapter_marker",
                "subtitle_cue",
            }:
                raise ValueError("Unsupported generation segment type.")
            setattr(segment, key, value)
        if text_changed:
            segment.optimized_text = None
            segment.speech_plan_json = {}
            segment.optimization_status = "stale"
            segment.optimization_source_hash = None
            segment.optimization_reviewed = False
            segment.optimization_model = None
        elif optimized_changed:
            segment.optimization_status = (
                "reviewed" if segment.optimized_text else "pending"
            )
            segment.optimization_source_hash = hashlib.sha256(
                segment.text.encode("utf-8")
            ).hexdigest()
            segment.optimization_reviewed = bool(segment.optimized_text)
            speech_plan = dict(segment.speech_plan_json or {})
            if segment.optimized_text:
                segment.speech_plan_json = {
                    **speech_plan,
                    "version": int(speech_plan.get("version") or 1),
                    "status": "manual_override",
                    "compiled_text": segment.optimized_text,
                    "reviewed": True,
                }
            else:
                segment.speech_plan_json = {}
        audio_stale = (
            text_changed
            or optimized_changed
            or any(key in changes for key in ("voice_id", "voice", "language"))
        )
        if audio_stale:
            segment.status = "stale"
            takes = completed_takes
            if takes is None:
                takes = list(
                    session.scalars(
                        select(AudioTake).where(
                            AudioTake.generation_segment_id == segment.id,
                            AudioTake.status == "completed",
                        )
                    ).all()
                )
            for take in takes:
                take.status = "stale"
        assembly_changed = any(
            key in changes
            for key in (
                "text",
                "node_kind",
                "paragraph_break_after",
                "voice_id",
                "voice",
                "language",
                "silence_after_ms",
                "removed",
            )
        )
        if mark_assemblies and assembly_changed:
            plan_revision = session.get(
                GenerationPlanRevision, segment.plan_revision_id
            )
            plan = (
                session.get(GenerationPlan, plan_revision.plan_id)
                if plan_revision
                else None
            )
            if plan is not None:
                mark_output_assemblies_stale(session, plan.session_id)
        segment.revision += 1
        segment.updated_at = utcnow()
        session.flush()
        return self._updated_segment_payload(segment)

    def update_segment(
        self, segment_id: str, expected_revision: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            if segment is None:
                raise KeyError(segment_id)
            if segment.revision != expected_revision:
                raise RevisionConflict(
                    "The generation segment changed in another client."
                )
            return self._apply_segment_changes(session, segment, changes)

    def update_segments(
        self, session_id: str, updates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not updates:
            raise ValueError("At least one generation segment update is required.")
        segment_ids = [str(item.get("id") or "").strip() for item in updates]
        if any(not segment_id for segment_id in segment_ids):
            raise ValueError("Every generation segment update requires an ID.")
        if len(set(segment_ids)) != len(segment_ids):
            raise ValueError(
                "A generation segment can only be updated once per request."
            )
        if any(not dict(item.get("changes") or {}) for item in updates):
            raise ValueError(
                "Every generation segment update requires at least one change."
            )

        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            segments = {
                segment.id: segment
                for segment in session.scalars(
                    select(GenerationSegment).where(
                        GenerationSegment.id.in_(segment_ids)
                    )
                ).all()
            }
            if len(segments) != len(segment_ids):
                missing = next(
                    segment_id
                    for segment_id in segment_ids
                    if segment_id not in segments
                )
                raise KeyError(missing)

            revision_ids = {segment.plan_revision_id for segment in segments.values()}
            plan_revisions = {
                revision.id: revision
                for revision in session.scalars(
                    select(GenerationPlanRevision).where(
                        GenerationPlanRevision.id.in_(revision_ids)
                    )
                ).all()
            }
            plan_ids = {revision.plan_id for revision in plan_revisions.values()}
            plans = {
                plan.id: plan
                for plan in session.scalars(
                    select(GenerationPlan).where(GenerationPlan.id.in_(plan_ids))
                ).all()
            }
            for segment_id in segment_ids:
                segment = segments[segment_id]
                revision = plan_revisions.get(segment.plan_revision_id)
                plan = plans.get(revision.plan_id) if revision is not None else None
                if plan is None or plan.session_id != session_id:
                    raise KeyError(segment_id)

            for item in updates:
                segment = segments[str(item["id"])]
                if segment.revision != int(item["revision"]):
                    raise RevisionConflict(
                        "One or more generation segments changed in another client; no replacements were applied."
                    )

            completed_takes_by_segment: dict[str, list[AudioTake]] = {}
            for take in session.scalars(
                select(AudioTake).where(
                    AudioTake.generation_segment_id.in_(segment_ids),
                    AudioTake.status == "completed",
                )
            ).all():
                completed_takes_by_segment.setdefault(
                    take.generation_segment_id, []
                ).append(take)

            assembly_keys = {
                "text",
                "node_kind",
                "paragraph_break_after",
                "voice_id",
                "voice",
                "language",
                "silence_after_ms",
                "removed",
            }
            assembly_changed = False
            results = []
            for item in updates:
                segment = segments[str(item["id"])]
                changes = dict(item["changes"])
                assembly_changed = assembly_changed or bool(
                    assembly_keys.intersection(changes)
                )
                results.append(
                    self._apply_segment_changes(
                        session,
                        segment,
                        changes,
                        completed_takes=completed_takes_by_segment.get(segment.id, []),
                        mark_assemblies=False,
                    )
                )
            if assembly_changed:
                mark_output_assemblies_stale(session, session_id)
            session.flush()
            return {"items": results}

    def select_take(
        self, segment_id: str, take_id: str, expected_revision: int
    ) -> dict[str, Any]:
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            take = session.get(AudioTake, take_id)
            if (
                segment is None
                or take is None
                or take.generation_segment_id != segment_id
            ):
                raise KeyError(take_id)
            if segment.revision != expected_revision:
                raise RevisionConflict(
                    "The generation segment changed in another client."
                )
            if take.status not in {"completed", "stale"} or not take.artifact_id:
                raise ValueError("Only an available audio take can be selected.")
            session.add(
                GenerationSegmentRevision(
                    generation_segment_id=segment.id,
                    revision=segment.revision,
                    alignment_group=segment.alignment_group,
                    node_kind=segment.node_kind,
                    paragraph_break_after=segment.paragraph_break_after,
                    speaker=segment.speaker,
                    text=segment.text,
                    optimized_text=segment.optimized_text,
                    speech_plan_json=dict(segment.speech_plan_json or {}),
                    optimization_status=segment.optimization_status,
                    optimization_reviewed=segment.optimization_reviewed,
                    marked=segment.marked,
                    removed=segment.removed,
                    voice_id=segment.voice_id,
                    voice=segment.voice,
                    language=segment.language,
                    silence_after_ms=segment.silence_after_ms,
                )
            )
            for item in session.scalars(
                select(AudioTake).where(AudioTake.generation_segment_id == segment_id)
            ).all():
                item.is_active = item.id == take_id
                item.revision += 1
            segment.revision += 1
            segment.updated_at = utcnow()
            plan_revision = session.get(
                GenerationPlanRevision, segment.plan_revision_id
            )
            plan = (
                session.get(GenerationPlan, plan_revision.plan_id)
                if plan_revision
                else None
            )
            if plan is not None:
                mark_output_assemblies_stale(session, plan.session_id)
            return {
                "id": segment.id,
                "active_take_id": take.id,
                "revision": segment.revision,
            }

    def start(
        self,
        session_id: str,
        *,
        run_override: dict[str, Any] | None = None,
        selected_segment_override: dict[str, Any] | None = None,
        segment_ids: list[str] | None = None,
        generation_run_id: str | None = None,
        operation: str = "generate",
    ) -> dict[str, Any]:
        requested_segment_ids = [
            str(value) for value in (segment_ids or []) if str(value)
        ]
        run_override = dict(run_override or {})
        selected_segment_override = _secret_free(
            deepcopy(selected_segment_override or {})
        )
        if selected_segment_override and not requested_segment_ids:
            raise ValueError(
                "An alternate segment setting set requires one or more selected segments."
            )
        if selected_segment_override:
            selected_tts = selected_segment_override.get("tts")
            if isinstance(selected_tts, dict):
                # Provider metadata is server-owned.  The compact UI override
                # may name a provider but must not smuggle an arbitrary
                # catalogue or secret into an immutable run snapshot.
                selected_tts.pop("provider_configs", None)
                selected_tts.pop("service_configs", None)
                binding = self._selected_tts_provider_binding(
                    session_id,
                    selected_segment_override,
                )
                if binding is not None:
                    selected_tts["provider_configs"] = [binding]
        resolved_for_new: tuple[dict[str, Any], str] | None = None
        if operation == "generate" and not requested_segment_ids:
            resolved_for_new = self.settings.resolve(
                session_id,
                run_override=run_override,
            )
            if self.plan_refresher is not None:
                self.plan_refresher(session_id, resolved_for_new[0])
                resolved_for_new = (
                    resolved_for_new[0],
                    stable_hash(resolved_for_new[0]),
                )

        with self.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            if plan is None or not plan.active_revision_id:
                raise ValueError(
                    "Create generation segments before starting audio generation."
                )
            plan_revision_id = plan.active_revision_id
            if requested_segment_ids:
                unique_requested_ids = set(requested_segment_ids)
                requested_rows = list(
                    session.execute(
                        select(
                            GenerationSegment.id,
                            GenerationSegment.plan_revision_id,
                        ).where(GenerationSegment.id.in_(requested_segment_ids))
                    ).all()
                )
                requested_revisions = {
                    revision_value for _segment_id, revision_value in requested_rows
                }
                if (
                    len(requested_rows) != len(unique_requested_ids)
                    or len(requested_revisions) != 1
                ):
                    raise ValueError(
                        "Selected generation segments must belong to one available plan revision."
                    )
                requested_revision_id = next(iter(requested_revisions))
                requested_revision = session.get(
                    GenerationPlanRevision,
                    requested_revision_id,
                )
                if requested_revision is None or requested_revision.plan_id != plan.id:
                    raise ValueError(
                        "Selected generation segments do not belong to this session."
                    )
                plan_revision_id = requested_revision_id
            source_run = None
            if requested_segment_ids and generation_run_id:
                source_run = session.get(GenerationRun, generation_run_id)
                if (
                    source_run is None
                    or source_run.session_id != session_id
                    or source_run.plan_revision_id != plan_revision_id
                    or source_run.operation == "rvc"
                ):
                    raise ValueError(
                        "The selected source generation run does not match these segments."
                    )
            elif requested_segment_ids and operation != "rvc":
                source_run = session.scalar(
                    select(GenerationRun)
                    .where(
                        GenerationRun.session_id == session_id,
                        GenerationRun.plan_revision_id == plan_revision_id,
                        GenerationRun.operation != "rvc",
                    )
                    .order_by(
                        GenerationRun.sequence_number.desc(),
                        GenerationRun.created_at.desc(),
                    )
                )
            if source_run is not None:
                if source_run.status in {
                    "queued",
                    "running",
                    "pausing",
                    "pause_requested",
                    "cancel_requested",
                    "paused",
                }:
                    raise ValueError(
                        "The source generation run is still active; stop or resume it before regenerating segments."
                    )
                source_snapshot = dict(source_run.settings_snapshot_json or {})
                # A previous alternate take is a useful source for ordinary
                # settings, but its *selected-only* precedence must not leak
                # into a later regeneration unless it is requested again.
                source_snapshot.pop("selected_segment_override", None)
                snapshot = _merge(
                    source_snapshot,
                    run_override,
                    selected_segment_override,
                )
                settings_hash = stable_hash(snapshot)
            else:
                if resolved_for_new is None:
                    resolved_for_new = self.settings.resolve(
                        session_id,
                        run_override=run_override,
                    )
                snapshot, settings_hash = resolved_for_new
                if selected_segment_override:
                    snapshot = _merge(snapshot, selected_segment_override)
                    settings_hash = stable_hash(snapshot)
            if selected_segment_override:
                snapshot["selected_segment_override"] = deepcopy(
                    selected_segment_override
                )
                settings_hash = stable_hash(snapshot)
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
                source_generation_run_id=source_run.id if source_run else None,
                sequence_number=sequence_number,
                operation=operation,
                status="queued",
                settings_snapshot_json=snapshot,
                settings_hash=settings_hash,
            )
            session.add(run)
            session.flush()
            run_id = run.id
        resource_keys = self._resource_keys(session_id, snapshot)
        job = self.jobs.enqueue(
            "generation.run",
            {
                "generation_run_id": run_id,
                "segment_ids": requested_segment_ids,
                "operation": operation,
            },
            session_id=session_id,
            resource_keys=resource_keys,
        )
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            run.job_id = job.id
            run.updated_at = utcnow()
        with self.database.session() as session:
            result = self._run_payload(session, session.get(GenerationRun, run_id))
            # Kept for compatibility with clients that used the old response
            # flag. Targeted generation now always creates a new immutable run.
            result["reused_run"] = False
            return result

    @staticmethod
    def _resource_keys(session_id: str, snapshot: dict[str, Any]) -> list[str]:
        tts = snapshot.get("tts", {})
        service = str(tts.get("service") or "tts").lower().replace(" ", "_")
        resource_keys = [f"session:{session_id}", f"service:tts:{service}"]
        compute = str(tts.get("compute_backend") or tts.get("device") or "auto").lower()
        if compute in {"cuda", "vulkan", "metal", "gpu"}:
            resource_keys.append(f"gpu:{compute}")
        return resource_keys

    def request_pause(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status not in {"queued", "running", "pausing"}:
                raise ValueError(f"Run cannot be paused from {run.status}.")
            run.pause_requested = True
            run.status = "pausing"
            run.updated_at = utcnow()
            return {"id": run.id, "job_id": run.job_id, "status": run.status}

    def resume(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status != "paused":
                raise ValueError("Only a paused generation run can be resumed.")
            run.pause_requested = False
            run.cancel_requested = False
            run.status = "queued"
            run.updated_at = utcnow()
            session_id = run.session_id
            snapshot = dict(run.settings_snapshot_json or {})
        job = self.jobs.enqueue(
            "generation.run",
            {"generation_run_id": run_id, "segment_ids": [], "operation": "resume"},
            session_id=session_id,
            resource_keys=self._resource_keys(session_id, snapshot),
        )
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            run.job_id = job.id
        return {"id": run_id, "job_id": job.id, "status": "queued"}

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            run.cancel_requested = True
            run.status = "cancel_requested"
            run.updated_at = utcnow()
            job_id = run.job_id
            if job_id:
                try:
                    self.jobs.request_cancel_in_session(session, job_id)
                except KeyError:
                    run.status = "canceled"
                    run.cancel_requested = False
                    run.updated_at = utcnow()
            else:
                run.status = "canceled"
                run.cancel_requested = False
                run.updated_at = utcnow()
            return {"id": run.id, "job_id": job_id, "status": run.status}

    @staticmethod
    def _run_label(run: GenerationRun) -> str:
        snapshot = dict(run.settings_snapshot_json or {})
        tts = dict(snapshot.get("tts") or {})
        rvc = dict(snapshot.get("rvc") or {})
        details = []
        for value in (
            tts.get("service") or tts.get("tts_service") or tts.get("backend"),
            tts.get("model") or tts.get("xtts_model"),
            tts.get("voice") or tts.get("voice_name"),
        ):
            normalized = str(value or "").strip()
            if normalized and normalized.lower() not in {
                item.lower() for item in details
            }:
                details.append(normalized)
        if run.operation == "rvc":
            model = str(rvc.get("model") or rvc.get("rvc_model") or "").strip()
            details.append(f"RVC {model}".strip())
        if not details:
            details.append("Speech generation")
        return f"Run {run.sequence_number}: " + " · ".join(details)

    def _run_payload(self, session, run: GenerationRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        job = session.get(Job, run.job_id) if run.job_id else None
        assembly = session.scalar(
            select(OutputAssembly)
            .where(OutputAssembly.generation_run_id == run.id)
            .order_by(OutputAssembly.created_at.desc())
        )
        take_count = int(
            session.scalar(
                select(func.count())
                .select_from(AudioTake)
                .where(AudioTake.generation_run_id == run.id)
            )
            or 0
        )
        from .usage import usage_summary

        usage = list(
            session.scalars(
                select(UsageEvent).where(UsageEvent.generation_run_id == run.id)
            ).all()
        )
        snapshot = dict(run.settings_snapshot_json or {})
        modal_snapshot = {
            key: deepcopy(snapshot[key])
            for key in ("tts", "rvc", "selected_segment_override")
            if isinstance(snapshot.get(key), dict)
        }
        return {
            "id": run.id,
            "session_id": run.session_id,
            "plan_revision_id": run.plan_revision_id,
            "source_generation_run_id": run.source_generation_run_id,
            "sequence_number": run.sequence_number,
            "operation": run.operation,
            "label": self._run_label(run),
            "job_id": run.job_id,
            "status": run.status,
            "progress": float(job.progress)
            if job
            else (1.0 if run.status == "completed" else 0.0),
            "pause_requested": run.pause_requested,
            "cancel_requested": run.cancel_requested,
            "settings_hash": run.settings_hash,
            # A run has a full reproducibility snapshot, but the history UI
            # needs only speech settings to seed an alternate take.  Do not
            # turn this list endpoint into a settings-data side channel.
            "settings_snapshot": modal_snapshot,
            "error_message": job.error_message if job else None,
            "take_count": take_count,
            "usage": usage_summary(usage),
            "progress_detail": job.progress_detail if job else None,
            "assembly": (
                self._assembly_payload(
                    assembly,
                    session.get(Job, assembly.job_id) if assembly.job_id else None,
                )
                if assembly
                else None
            ),
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            runs = list(
                session.scalars(
                    select(GenerationRun)
                    .where(GenerationRun.session_id == session_id)
                    .order_by(
                        GenerationRun.sequence_number.desc(),
                        GenerationRun.created_at.desc(),
                    )
                ).all()
            )
            return [self._run_payload(session, run) for run in runs]

    def latest_run(self, session_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            run = session.scalar(
                select(GenerationRun)
                .where(GenerationRun.session_id == session_id)
                .order_by(
                    GenerationRun.sequence_number.desc(),
                    GenerationRun.created_at.desc(),
                )
            )
            return self._run_payload(session, run)

    def delete_run(self, run_id: str) -> dict[str, Any]:
        paths_to_remove: list[Path] = []
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status in {"queued", "running", "pausing", "cancel_requested"}:
                raise ValueError("Stop or cancel this run before deleting it.")
            takes = list(
                session.scalars(
                    select(AudioTake).where(AudioTake.generation_run_id == run.id)
                ).all()
            )
            assemblies = list(
                session.scalars(
                    select(OutputAssembly).where(
                        OutputAssembly.generation_run_id == run.id
                    )
                ).all()
            )
            affected_segment_ids = {take.generation_segment_id for take in takes}
            artifact_ids = {take.artifact_id for take in takes if take.artifact_id}
            artifact_ids.update(
                item.artifact_id for item in assemblies if item.artifact_id
            )
            artifacts = (
                list(
                    session.scalars(
                        select(Artifact).where(Artifact.id.in_(artifact_ids))
                    ).all()
                )
                if artifact_ids
                else []
            )
            if self.artifacts is not None:
                for artifact in artifacts:
                    try:
                        paths_to_remove.append(
                            self.artifacts.paths.managed_path(artifact.relative_path)
                        )
                    except ValueError:
                        pass
            from .artifacts import ArtifactService

            for artifact in artifacts:
                ArtifactService._mark_descendants_stale(session, artifact.id)
            for assembly in assemblies:
                session.delete(assembly)
            for take in takes:
                session.delete(take)
            session.flush()
            for segment_id in affected_segment_ids:
                remaining = session.scalar(
                    select(AudioTake)
                    .where(
                        AudioTake.generation_segment_id == segment_id,
                        AudioTake.artifact_id.is_not(None),
                    )
                    .order_by(AudioTake.created_at.desc())
                )
                for candidate in session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_segment_id == segment_id
                    )
                ).all():
                    candidate.is_active = (
                        remaining is not None and candidate.id == remaining.id
                    )
            for artifact in artifacts:
                session.delete(artifact)
            session.delete(run)
        for path in paths_to_remove:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"id": run_id, "status": "deleted"}

    @staticmethod
    def _assembly_payload(
        record: OutputAssembly,
        job: Job | None = None,
    ) -> dict[str, Any]:
        return {
            "id": record.id,
            "session_id": record.session_id,
            "generation_run_id": record.generation_run_id,
            "job_id": record.job_id,
            "artifact_id": record.artifact_id,
            "status": record.status,
            "progress": (
                1.0
                if record.status in {"completed", "stale"}
                else float(job.progress)
                if job
                else 0.0
            ),
            "progress_detail": job.progress_detail if job else None,
            "settings_hash": record.settings_hash,
            "error_message": record.error_message,
            "settings": deepcopy(record.settings_json or {}),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    def create_assembly(
        self,
        session_id: str,
        *,
        generation_run_id: str | None = None,
        run_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot, settings_hash = self.settings.resolve(
            session_id,
            sections=["audio", "output"],
            run_override=run_override,
        )
        output_format = str(
            (snapshot.get("output") or {}).get("format") or "wav"
        ).lower()
        from .audio_assembly import OUTPUT_FORMATS

        if output_format not in OUTPUT_FORMATS:
            raise ValueError(f"Unsupported audio output format: {output_format}")
        with self.database.session() as session:
            if session.get(SessionRecord, session_id) is None:
                raise KeyError(session_id)
            run = (
                session.get(GenerationRun, generation_run_id)
                if generation_run_id
                else None
            )
            if generation_run_id and (run is None or run.session_id != session_id):
                raise KeyError(generation_run_id)
            if run is not None and run.status != "completed":
                raise ValueError("Only a completed generation run can be assembled.")
            plan = session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            plan_revision_id = (
                run.plan_revision_id
                if run
                else plan.active_revision_id
                if plan
                else None
            )
            if not plan_revision_id:
                raise ValueError("Create generation segments before assembling audio.")
            record = OutputAssembly(
                session_id=session_id,
                generation_run_id=run.id if run else None,
                status="queued",
                settings_json={
                    "resolved": snapshot,
                    "plan_revision_id": plan_revision_id,
                },
                settings_hash=settings_hash,
            )
            session.add(record)
            session.flush()
            assembly_id = record.id
        job = self.jobs.enqueue(
            "generation.assemble",
            {"output_assembly_id": assembly_id},
            session_id=session_id,
            resource_keys=[f"session:{session_id}"],
        )
        with self.database.session() as session:
            record = session.get(OutputAssembly, assembly_id)
            record.job_id = job.id
            record.updated_at = utcnow()
            payload = self._assembly_payload(record, job)
        return payload

    def latest_assembly(self, session_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            record = session.scalar(
                select(OutputAssembly)
                .where(OutputAssembly.session_id == session_id)
                .order_by(OutputAssembly.created_at.desc())
            )
            job = (
                session.get(Job, record.job_id)
                if record is not None and record.job_id
                else None
            )
            return self._assembly_payload(record, job) if record is not None else None


class ResourceClaimService:
    """Compatibility facade over the queue's fenced resource-lease operations."""

    def __init__(self, database: Database):
        self.queue = JobQueue(database)

    def acquire(
        self,
        job_id: str,
        owner: str,
        keys: list[str],
        lease_seconds: int = 60,
        *,
        lease_generation: int,
    ) -> bool:
        return self.queue.acquire_resources(
            job_id,
            owner,
            keys,
            lease_generation=lease_generation,
            lease_seconds=lease_seconds,
        )

    def release(
        self,
        job_id: str,
        owner: str,
        *,
        lease_generation: int,
    ) -> None:
        self.queue.release_resources(
            job_id,
            owner,
            lease_generation=lease_generation,
        )
