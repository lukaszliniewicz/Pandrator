"""Settings defaults, validation, runtime normalization and snapshot primitives."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from pandrator.logic.dubbing.source_passage_settings import (
    SOURCE_PASSAGE_DEFAULTS as _SOURCE_PASSAGE_DEFAULTS,
)
from pandrator.logic.tts_provider_policy import DEFAULT_TTS_SERVICE_ID

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
    "source_passages",
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
        "audiobook_chunking": "model",
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
        "llm_tts_annotation_mode": "off",
        "llm_tts_annotation_only": False,
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
        "stt_transcribe_style": "readability",
        "stt_lid_backend": "whisper",
        "stt_beam_size": 1,
        "parakeet_decoder": "tdt",
        "qwen_asr_model": "qwen3_asr_0_6b",
        "transcription_vocal_isolation": "off",
        "qwen_asr_chunk_seconds": 30,
        "qwen_asr_max_tokens": 512,
        "qwen_asr_chunk_mode": "auto",
        "qwen_asr_timeout_seconds": 3600,
        "qwen_asr_clamp_timestamps": False,
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
        "caption_alignment_method": "ctc",
        "caption_alignment_ctc_model": "auto",
        "caption_alignment_padding_ms": 2000,
        "caption_alignment_batch_seconds": 30,
        "caption_alignment_min_confidence": 0.5,
        "caption_alignment_fallback_coverage": 0.9,
        "diarization_enabled": False,
    },
    "subtitles": {
        "language_defaults": True,
        "max_lines": 2,
        "max_chars_per_line": 60,
        "max_cps": 20.0,
        "min_duration_ms": 833,
        "max_duration_ms": 7000,
        "min_gap_ms": 80,
        "phrase_gap_ms": 900,
        "hard_gap_ms": 1500,
        "sentence_boundary_threshold": 0.25,
        "boundary_correction_enabled": False,
    },
    "source_passages": dict(_SOURCE_PASSAGE_DEFAULTS),
    "correction": {
        "enabled": False,
        "correction_style": "publishable",
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
        "service": DEFAULT_TTS_SERVICE_ID,
        "use_external_server": False,
        "external_server_url": "",
        "model": "",
        "language": "en",
        "voice": "",
        "speed": 1.0,
        "max_attempts": 5,
        "tts_batch_size": 10,
        "tts_concurrent_requests": 1,
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
        "elevenlabs_voice_settings": {},
        "openai_audio_endpoint": "",
        "openai_audio_instructions": "",
        "generation_prompt": "",
        "performance_enabled": False,
        "casting_enabled": False,
        "performance_allow_vocalizations": False,
        "tts_context_mode": "off",
        "performance_context_before": 2,
        "performance_context_after": 1,
        "performance_context_max_chars": 4000,
        "speech_block_min_chars": 10,
        "speech_block_max_chars": 220,
        "speech_block_merge_threshold": 1500,
        "speech_block_continuation_threshold_ms": 3000,
        "speech_block_max_internal_gap_ms": 4000,
        "speech_block_early_repair_enabled": False,
        "speech_block_early_repair_min_shortfall_ms": 1000,
        "speech_block_early_repair_min_shortfall_percent": 20,
        "speech_block_early_repair_min_advance_ms": 1000,
        "speech_block_early_repair_min_child_span_ms": 1000,
        "speech_block_generation_mode": "passage",
        "speech_block_regroup_enabled": False,
        "speech_block_regroup_max_mismatch_ms": 500,
        "speech_block_regroup_max_mismatch_percent": 15,
        "speech_block_regroup_max_gap_ms": 300,
        "speech_block_regroup_max_passages": 3,
        "speech_block_regroup_max_boundary_shift_ms": 500,
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
        "synchronization_slowdown_enabled": True,
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
        "audio_match_source_duration": True,
        "video_tail_extension_policy": "ask",
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
        "language_defaults": "subtitle_language_defaults",
        "max_lines": "subtitle_max_lines",
        "max_chars_per_line": "subtitle_max_chars_per_line",
        "max_cps": "subtitle_max_cps",
        "min_duration_ms": "subtitle_min_duration_ms",
        "max_duration_ms": "subtitle_max_duration_ms",
        "min_gap_ms": "subtitle_min_gap_ms",
        "phrase_gap_ms": "subtitle_phrase_gap_ms",
        "hard_gap_ms": "subtitle_hard_gap_ms",
        "sentence_boundary_threshold": "subtitle_sentence_boundary_threshold",
    },
    "correction": {
        "enabled": "correction_enabled",
        "correction_style": "correction_style",
        "model_name": "correction_model",
        "instructions": "custom_correction_prompt",
        "char_limit": "llm_char",
        "max_segments_per_batch": "max_subtitles_per_call",
        "substantial_gap_ms": "timing_context_gap_ms",
    },
    "source_passages": {
        "min_chars": "source_passage_min_chars",
        "preferred_chars": "source_passage_preferred_chars",
        "sentence_lookahead_chars": "source_passage_sentence_lookahead_chars",
        "cue_join_gap_ms": "source_passage_cue_join_gap_ms",
        "diagnostic_span_ms": "source_passage_diagnostic_span_ms",
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


def normalize_subtitle_limit_override(
    values: dict[str, Any], *, runtime: bool = False,
) -> dict[str, Any]:
    """Explicit per-run limits beat inherited presets, unless auto is requested.

    Apply only to raw overrides, never to an already merged settings snapshot.
    Old clients do not know the new language-defaults switch.
    """
    result = dict(values)
    prefix = "subtitle_" if runtime else ""
    flag = prefix + "language_defaults"
    if flag not in result and any(
        result.get(prefix + key) not in (None, "")
        for key in ("max_chars_per_line", "max_cps")
    ):
        result[flag] = False
    return result


def adapt_runtime_settings(section: str, values: dict[str, Any], _service_config_cache=None) -> dict[str, Any]:
    """Adapt runtime aliases; a selected UI voice overrides a stale speaker."""
    result = deepcopy(values or {})
    for web_key, runtime_key in RUNTIME_SETTING_ALIASES.get(section, {}).items():
        if runtime_key not in result and web_key in result:
            result[runtime_key] = deepcopy(result[web_key])
    if section in {"correction", "translation"}:
        mode = str(result.get("timing_context_mode") or "").strip().lower()
        if mode and "timing_context_enabled" not in result:
            result["timing_context_enabled"] = mode != "none"
    if section == "tts":
        # The run label and UI use voice; legacy adapters consume speaker.
        # Repair old conflicting snapshots without mutating them. An empty
        # built-in voice must not erase a speaker-only legacy configuration.
        if str(result.get("voice") or "").strip():
            result["speaker"] = deepcopy(result["voice"])
        # Web settings persist stable service IDs (for example ``kokoro``), while
        # the legacy synthesis boundary still dispatches on canonical labels.
        # Adapt both current and already-frozen run snapshots at that boundary.
        from pandrator.logic import tts_handler

        selected_value = str(
            result.get("service") or result.get("tts_service") or ""
        ).strip()
        selected = (
            tts_handler.get_service_config(result, selected_value, _service_config_cache)
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


class RevisionConflict(ValueError):
    pass


def validate_stt_settings(value: dict[str, Any]) -> None:
    """Reject explicitly invalid Qwen3 ASR selections on the save path.

    Only keys present in the submitted override are checked; absent keys
    keep inheriting defaults. Present-but-invalid enum values fail here so
    migration can never silently swap the model or skip preprocessing.
    """

    from pandrator.logic.dubbing.qwen_asr import (
        QWEN3_ASR_MODELS,
        QWEN3_CHUNK_MODES,
        VOCAL_ISOLATION_CHOICES,
    )

    if not isinstance(value, dict):
        raise ValueError("stt settings must be an object.")
    model = value.get("qwen_asr_model")
    if model is not None and model not in QWEN3_ASR_MODELS:
        raise ValueError(
            f"qwen_asr_model must be one of {', '.join(QWEN3_ASR_MODELS)}."
        )
    isolation = value.get("transcription_vocal_isolation")
    if isolation is not None and isolation not in VOCAL_ISOLATION_CHOICES:
        raise ValueError(
            "transcription_vocal_isolation must be one of "
            f"{', '.join(VOCAL_ISOLATION_CHOICES)}."
        )
    chunk_mode = value.get("qwen_asr_chunk_mode")
    if chunk_mode is not None and chunk_mode not in QWEN3_CHUNK_MODES:
        raise ValueError(
            f"qwen_asr_chunk_mode must be one of {', '.join(QWEN3_CHUNK_MODES)}."
        )
    chunk_seconds = value.get("qwen_asr_chunk_seconds")
    if chunk_seconds is not None:
        if (
            isinstance(chunk_seconds, bool)
            or not isinstance(chunk_seconds, (int, float))
            or not (chunk_seconds == 0 or 10 <= chunk_seconds <= 120)
        ):
            raise ValueError("qwen_asr_chunk_seconds must be 0 or 10-120.")
    max_tokens = value.get("qwen_asr_max_tokens")
    if max_tokens is not None:
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 32 <= max_tokens <= 4096
        ):
            raise ValueError("qwen_asr_max_tokens must be an integer 32-4096.")
    timeout_seconds = value.get("qwen_asr_timeout_seconds")
    if timeout_seconds is not None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 60 <= timeout_seconds <= 14400
        ):
            raise ValueError("qwen_asr_timeout_seconds must be 60-14400.")
    clamp = value.get("qwen_asr_clamp_timestamps")
    if clamp is not None and not isinstance(clamp, bool):
        raise ValueError("qwen_asr_clamp_timestamps must be a boolean.")


def validate_voiceover_repair_settings(value: dict[str, Any]) -> None:
    for suffix, minimum, maximum in (
        ("min_shortfall_ms", 100, 60000),
        ("min_shortfall_percent", 1, 95),
        ("min_advance_ms", 100, 60000),
        ("min_child_span_ms", 250, 60000),
    ):
        key = f"speech_block_early_repair_{suffix}"
        if key not in value:
            continue
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int) or not minimum <= number <= maximum:
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}.")
    # Passage-first planning (default) plus the optional second-pass regroup
    # share this TTS-section validator. Bounds live next to the pure
    # selection logic in pandrator.logic.dubbing.passage_regroup so stored
    # values and runtime normalization cannot drift apart.
    from pandrator.logic.dubbing.passage_regroup import validate_regroup_settings

    validate_regroup_settings(value)


def validate_output_settings(value: dict[str, Any]) -> None:
    """Validate stored output overrides without touching unrelated keys."""
    if value.get("video_tail_extension_policy", "ask") not in ("ask", "extend"):
        raise ValueError("video_tail_extension_policy must be ask or extend.")
