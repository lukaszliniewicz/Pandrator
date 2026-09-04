"""Explanations for the stable, user-facing workflow settings.

The registry is deliberately separate from the settings persistence layer.  It
describes the concise web names in :mod:`pandrator.web.workspace` and never
contains runtime aliases or provider credentials.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .workspace import BUILTIN_DEFAULTS, SETTING_SECTIONS

DOCUMENTED_SECTIONS = SETTING_SECTIONS
WORKFLOW_SECTIONS = {
    "audiobook": frozenset(
        {"text", "source_cleaning", "tts", "audio", "rvc", "output"}
    ),
    "subtitles": frozenset({"stt", "subtitles", "correction", "translation", "output"}),
    "voiceover": frozenset(
        {
            "stt",
            "subtitles",
            "correction",
            "translation",
            "tts",
            "audio",
            "rvc",
            "output",
        }
    ),
}
VALUE_TYPES = frozenset({"boolean", "integer", "number", "string", "object", "array"})

_WEB_RESEARCH_DESCRIPTIONS = {
    "web_research_enabled": "Runs a bounded web-research pass before the LLM transformation so uncertain names and terminology can be grounded in cited evidence.",
    "web_research_provider": "Selects the web-research provider; the current runtime supports Jina and rejects other values.",
    "web_research_model_name": "Selects the model used by the research agent; blank reuses the correction or translation model.",
    "web_research_mode": "Chooses one global research pass over context-sized source groups or separate research aligned with each transformation chunk.",
    "web_research_context_fraction": "Limits the fraction of the research model's context window budgeted for prompts, accumulated evidence, and source material; the runtime clamps it to 0.1 through 0.8.",
    "web_research_language": "Requests a language for research queries and evidence; blank lets the agent infer a suitable language from the task.",
    "web_research_max_searches": "Limits search operations available to the research agent in each research run.",
    "web_research_max_extractions": "Limits full-page extraction operations available to the research agent in each research run.",
    "web_research_preferred_domains": "Provides comma- or line-separated domains the research agent should prefer when choosing sources.",
    "web_research_blocked_domains": "Provides comma- or line-separated domains the research agent must exclude from its sources.",
    "web_research_max_iterations": "Limits model/tool turns for each bounded research run; the runtime always permits at least two turns so the agent can act and finish.",
    "web_research_timeout_seconds": "Sets the network and model request timeout used by web research, in seconds.",
    "web_research_source_chars": "Provides the legacy target source-character budget for research input; current context-aware execution derives its actual groups from the selected model's context budget.",
    "web_research_result_chars": "Limits characters returned by an individual research tool result before it is added to the agent context.",
}

_TRANSFORMATION_DESCRIPTIONS = {
    "enabled": "Enables this optional LLM transformation stage in workflows that include it.",
    "model_name": "Selects the LLM used for this transformation; blank uses the configured default model.",
    "reasoning_effort": "Requests a reasoning-effort level from providers that support it; blank leaves the provider or model default unchanged.",
    "instructions": "Adds task-specific instructions to the built-in transformation contract without replacing its structural and identity-preservation rules.",
    "char_limit": "Targets this many source characters per model batch; batching also respects the segment limit and never splits an individual subtitle segment.",
    "max_segments_per_batch": "Limits subtitle segments placed in one model batch, independently of the character target.",
    "llm_concurrent_calls": "Limits transformation model requests that may run concurrently; higher values improve throughput but reduce sequential cross-batch continuity.",
    "timing_context_mode": "Controls timing disclosed to the model: full includes cue timing and substantial gaps, overlap_only exposes only overlap relationships, and none omits timing context.",
    "substantial_gap_ms": "Defines the millisecond gap considered substantial when full timing context is included in model packets.",
    "no_remove_subtitles": "Forbids the model from deleting subtitle segments during correction or translation, while still allowing text edits.",
    "context_before": "Includes up to this many accepted segments from the preceding batch as read-only continuity context.",
    "context_after": "Includes up to this many source segments from the following batch as read-only look-ahead context.",
    "request_timeout_seconds": "Sets the timeout for each transformation model request, in seconds.",
}

_WEB_RESEARCH_METADATA: dict[str, dict[str, object]] = {
    "web_research_provider": {
        "choices": ["jina"],
        "applicability": "Correction and LLM translation only.",
    },
    "web_research_mode": {"choices": ["global", "per_chunk"]},
    "web_research_context_fraction": {"minimum": 0.1, "maximum": 0.8},
    "web_research_max_searches": {"minimum": 0},
    "web_research_max_extractions": {"minimum": 0},
    "web_research_max_iterations": {"minimum": 2, "unit": "model/tool turns"},
    "web_research_timeout_seconds": {"minimum": 1, "unit": "seconds"},
    "web_research_source_chars": {
        "minimum": 1,
        "unit": "characters",
        "caveat": "Retained for compatibility; current workflow execution partitions research input from the selected model's context budget.",
    },
    "web_research_result_chars": {"minimum": 2000, "unit": "characters"},
}

_TRANSFORMATION_METADATA: dict[str, dict[str, object]] = {
    "char_limit": {"minimum": 1, "maximum": 100_000, "unit": "characters"},
    "max_segments_per_batch": {"minimum": 1, "maximum": 500, "unit": "segments"},
    "llm_concurrent_calls": {"minimum": 1, "maximum": 16},
    "timing_context_mode": {"choices": ["full", "overlap_only", "none"]},
    "substantial_gap_ms": {"minimum": 0, "maximum": 60_000, "unit": "milliseconds"},
    "context_before": {"minimum": 0, "maximum": 20, "unit": "segments"},
    "context_after": {"minimum": 0, "maximum": 20, "unit": "segments"},
    "request_timeout_seconds": {"minimum": 1, "unit": "seconds"},
}


def _label(name: str) -> str:
    """Turn a canonical setting key into a readable fallback label."""

    words = name.split("_")
    acronyms = {
        "cps": "CPS",
        "f0": "F0",
        "lid": "LID",
        "rvc": "RVC",
        "stt": "STT",
        "tts": "TTS",
        "vad": "VAD",
        "cjk": "CJK",
        "lufs": "LUFS",
        "ms": "ms",
        "db": "dB",
    }
    return " ".join(acronyms.get(word, word.capitalize()) for word in words)


# Every description is intentionally keyed by canonical name.  This makes a
# newly added default fail at import time until it receives an explanation.
_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "text": {
        "enable_sentence_splitting": "Splits narration sentences that exceed max_sentence_length at suitable linguistic boundaries before speech-block creation.",
        "max_sentence_length": "Sets the target maximum characters for narration sentence splitting and short-sentence appending; structural constraints can still produce a longer unit.",
        "enable_sentence_appending": "Combines adjacent short narration sentences when their combined text stays within max_sentence_length.",
        "remove_diacritics": "Transliterates narration text to remove diacritics before segmentation; this can change spelling and pronunciation and should be used only when the TTS voice requires it.",
        "remove_quotation_marks": "Removes quotation-mark characters from narration text before segmentation and synthesis.",
        "disable_paragraph_detection": "Preserves single line breaks instead of applying source-aware paragraph normalization during narration preprocessing.",
        "remove_footnotes": "Drops resolved footnote text while cleaning EPUB or PDF source material instead of injecting it into narration.",
        "filter_citations": "Filters bibliographic citation references while resolving source footnotes, without indiscriminately removing ordinary numeric text.",
        "enable_nemo_normalization": "Runs NeMo text normalization before narration segmentation to verbalize forms such as numbers and abbreviations; availability is platform-dependent.",
        "normalize_all_caps": "Converts eligible all-caps narration words to ordinary casing while preserving likely acronyms and headings.",
        "llm_tts_optimization": "Runs speech optimization during generation so each narration unit is revised or planned immediately before synthesis.",
        "llm_tts_document_optimization": "Runs speech optimization as a reviewable document-level stage before audio generation and stores a TTS-optimized artifact.",
        "apply_reviewed_pronunciations": "Applies active, reviewed pronunciation replacements to speech-optimization input and final TTS text.",
        "llm_processing_enabled": "Stores the legacy master LLM-processing switch; current web workflows use the explicit correction, translation, and speech-optimization switches instead.",
        "llm_tts_batch_size": "Sets the number of narration units sent in one generation-time speech-optimization model request.",
        "llm_tts_document_batch_size": "Sets the number of narration units sent in one document-level speech-optimization model request.",
        "tts_optimization_model": "Selects the LLM used for speech optimization; blank uses the configured default model.",
        "llm_concurrent_calls": "Limits speech-optimization model requests that may run concurrently.",
        "speech_optimization_mode": "Selects guarded planning, which preserves source wording, or flexible planning, which permits bounded rewriting subject to the retention threshold.",
        "speech_plan_min_retention": "Sets the minimum fraction of source wording that flexible speech optimization must retain before a proposal is accepted.",
        "speech_plan_save_proposals": "Stores rejected or alternate speech-plan proposals for later inspection instead of retaining only the accepted result.",
        "llm_multi_stage": "Runs the non-empty first, second, and third speech-optimization prompts in sequence instead of using the combined prompt.",
        "combined_prompt": "Supplies the single-pass speech-optimization instructions used when multi-stage processing is disabled.",
        "first_prompt": "Supplies the first speech-optimization pass when multi-stage processing is enabled.",
        "second_prompt": "Supplies the second speech-optimization pass when multi-stage processing is enabled.",
        "third_prompt": "Supplies the third speech-optimization pass when multi-stage processing is enabled.",
    },
    "stt": {
        "stt_engine": "Selects the speech-to-text engine: local CrispASR Whisper, Parakeet, or MOSS, or the supported Azure MAI-Transcribe-1.5 cloud profile.",
        "stt_model_quantization": "Selects the quantized model file used by local CrispASR engines; the available files depend on the selected engine, and cloud STT does not use a local model file.",
        "stt_compute_backend": "Selects the accelerator backend for local CrispASR inference; auto lets CrispASR choose, while CPU, CUDA, Vulkan, and Metal target specific local runtimes.",
        "stt_language": "Supplies the transcript language to local Whisper and to Azure cloud STT; auto lets the engine detect or omit a locale where supported.",
        "stt_compute_device": "Selects the non-CPU local accelerator index passed to CrispASR; it is ignored for auto/CPU execution and for remote cloud STT.",
        "whisper_prompt": "Provides an optional initial prompt to local CrispASR Whisper decoding to bias vocabulary and style; Parakeet, MOSS, and Azure cloud STT do not consume it.",
        "stt_threads": "Sets the local CrispASR worker-thread count; zero leaves thread selection to CrispASR, and cloud STT does not use it.",
        "stt_chunk_seconds": "Sets the local CrispASR audio window length before transcription; a non-positive value lets MOSS use its bounded 120-second context fallback, while Azure cloud chunking uses provider-specific settings.",
        "stt_chunk_overlap_seconds": "Sets overlap between non-MOSS local CrispASR chunks so neighboring windows can be stitched; MOSS uses its separate overlap control and Azure cloud STT ignores this local setting.",
        "stt_hotwords": "Provides comma- or line-separated terms for local CrispASR hotword biasing and Azure's phrase list; empty input sends no vocabulary hints.",
        "stt_transcribe_style": "Chooses Azure MAI-Transcribe-1.5's readability or verbatim transcription style; this provider-only control is ignored by local CrispASR engines.",
        "stt_lid_backend": "Selects the local CrispASR language-identification backend; the default Whisper path is omitted from the command, and non-default backend names are passed through to CrispASR.",
        "stt_beam_size": "Sets the local CrispASR beam-search width for non-MOSS engines; values above one are passed as a beam-search option, while MOSS and Azure cloud STT do not use it.",
        "parakeet_decoder": "Selects the decoder variant passed to local Parakeet; CTC and MAES are optional alternatives to the default TDT decoder, and other engines ignore this setting.",
        "moss_max_chunk_seconds": "Sets the bounded fallback context window used by local MOSS when the generic chunk length is non-positive; it does not change Whisper, Parakeet, or Azure cloud chunking.",
        "moss_chunk_overlap_seconds": "Sets overlap between local MOSS chunks; MOSS keeps this independent because native speaker turns cannot use CrispASR's token-based overlap stitcher, and other engines use stt_chunk_overlap_seconds.",
        "moss_vad_enabled": "Enables the VAD pass for local MOSS transcription; MOSS uses this switch instead of crispasr_vad_enabled, while non-MOSS local engines use the CrispASR switch.",
        "moss_ctc_alignment_enabled": "Enables the local MOSS CTC aligner that attaches word timings to native speaker turns; it is only meaningful for MOSS and does not affect other engines or cloud STT.",
        "moss_ctc_aligner_model": "Selects the local CTC aligner model used for MOSS word timing; auto downloads the bundled Canary aligner, and other engines do not invoke it.",
        "moss_ctc_padding_seconds": "Adds an acoustic margin around each local MOSS turn before CTC alignment; the runtime bounds this provider-specific padding to zero through two seconds.",
        "crispasr_vad_enabled": "Enables VAD for local CrispASR Whisper and Parakeet transcription; MOSS uses moss_vad_enabled instead, and Azure cloud STT has its own remote chunking behavior.",
        "crispasr_vad_model": "Selects the VAD model used whenever local CrispASR VAD is enabled, including MOSS when moss_vad_enabled is on; the enable switch is engine-specific as described above.",
        "crispasr_vad_threshold": "Sets the local CrispASR VAD speech-confidence threshold used to classify audio as speech; the runtime clamps it to the provider's 0-to-1 range.",
        "crispasr_vad_min_speech_ms": "Sets the minimum local CrispASR speech run, in milliseconds, required for a VAD region; it is not an Azure cloud setting.",
        "crispasr_vad_min_silence_ms": "Sets the minimum local CrispASR silence run, in milliseconds, required to close a VAD region; it is not used by Azure cloud STT.",
        "crispasr_vad_speech_pad_ms": "Adds this many milliseconds of local CrispASR audio around each detected speech region so VAD does not trim word edges; Azure cloud STT does not consume it.",
        "crispasr_vad_max_speech_seconds": "Sets the maximum duration of a local CrispASR VAD speech region in seconds before it is split; the runtime enforces a minimum of one second.",
        "diarization_enabled": "Requests speaker diarization from local non-MOSS CrispASR engines; MOSS already supplies native speaker turns and Azure MAI-Transcribe-1.5 rejects diarization.",
    },
    "subtitles": {
        "max_lines": "Limits each finalized subtitle cue to this many display lines; the subtitle compositor bounds the value to one through three lines.",
        "max_chars_per_line": "Limits the visible characters on each finalized subtitle line; wrapping and cue splitting use a compositor range of 20 through 100 characters.",
        "max_cps": "Sets the maximum reading rate used when sizing finalized subtitle cue durations, measured in visible characters per second; the compositor bounds it to 5 through 40.",
        "min_duration_ms": "Sets the shortest display duration for a finalized subtitle cue in milliseconds, subject to available neighboring timing; the compositor bounds it to 250 through 3000 milliseconds.",
        "max_duration_ms": "Sets the longest display duration for a finalized subtitle cue in milliseconds before text is split or timing is capped; the compositor bounds it to 1000 through 15000 milliseconds.",
        "min_gap_ms": "Requests a minimum silent gap between finalized subtitle cues in milliseconds; when source timing is too tight, the compositor preserves ordering rather than inventing room.",
        "phrase_gap_ms": "Marks a phrase-level timing gap in milliseconds for subtitle boundary scoring; it is used by finalization as a softer break than hard silence.",
        "hard_gap_ms": "Marks a hard subtitle boundary after a silence gap in milliseconds; the finalizer will not make a cue cross that long pause.",
        "sentence_boundary_threshold": "Sets the sentence-boundary probability threshold used by subtitle finalization when scoring semantic cue breaks; the compositor bounds it to 0.01 through 0.99.",
        "boundary_correction_enabled": "Would enable a boundary-correction pass for subtitle timings, but this setting currently has no consumer in the finalization or workflow runtime.",
        "merge_threshold_ms": "Provides the legacy subtitle-level fallback merge gap in milliseconds for local speech-block packing when no dedicated TTS merge threshold is supplied; current TTS defaults provide that dedicated value.",
    },
    "correction": {
        **_TRANSFORMATION_DESCRIPTIONS,
        **_WEB_RESEARCH_DESCRIPTIONS,
    },
    "translation": {
        **_TRANSFORMATION_DESCRIPTIONS,
        "backend": "Selects LLM or DeepL translation; web research and model reasoning controls apply only to the LLM backend.",
        "source_language": "Sets the source language passed to translation, or auto to let the selected backend detect it.",
        "target_language": "Sets the required target language for translated subtitle or text output.",
        "glossary": "Provides translation terminology as JSON or supported text pairs; entries are normalized into exact source-to-target replacements for the model contract.",
        "context": "Includes surrounding subtitle context in translation prompts so wording can remain coherent across batch boundaries.",
        "glossary_enabled": "Includes the configured glossary in translation requests; when disabled, stored glossary text is retained but not applied.",
        **_WEB_RESEARCH_DESCRIPTIONS,
    },
    "tts": {
        "service": "Selects the TTS adapter that receives synthesis requests, such as XTTS, VoxCPM, FishS2, Kokoro, Silero, or another configured compatible service.",
        "use_external_server": "Requests that the selected first-class TTS service resolve its synthesis endpoint from external_server_url instead of its saved service profile; practical use depends on the selected service and endpoint wiring.",
        "external_server_url": "Provides the optional base URL considered by the TTS resolver when use_external_server is enabled for the selected service; practical reachability and adapter compatibility remain deployment-dependent.",
        "model": "Selects the model identifier sent to the active TTS provider; each provider interprets available model IDs and may map the concise web value at the runtime boundary.",
        "language": "Selects the language or locale sent with each TTS request; the active provider determines supported language identifiers and interpretation.",
        "voice": "Selects the speaker, preset voice, or provider-managed voice reference sent to the active TTS provider; the available identifiers are provider-defined.",
        "speed": "Sets the speech speed value sent to the active TTS provider; its units and supported range are provider-defined, although supported adapters generally interpret one as normal speed.",
        "max_attempts": "Limits retry attempts for one TTS synthesis item after transient or recoverable provider failures; the runtime bounds it to one through twenty attempts.",
        "tts_batch_size": "Sets the requested number of speech segments in one streaming TTS batch; workflow negotiation reduces it to the active provider's advertised capability, with a local bound of one through thirty-two.",
        "temperature": "Sets the XTTS sampling temperature when the matching xtts_send_temperature switch is enabled; XTTS interprets this provider-defined value, and other adapters may use their own controls.",
        "length_penalty": "Sets the XTTS length-penalty value when xtts_send_length_penalty is enabled; XTTS applies the provider-defined value while other engines may ignore it.",
        "repetition_penalty": "Sets the XTTS repetition penalty when xtts_send_repetition_penalty is enabled; the value is provider-defined and can also be used as a fallback by some compatible adapters.",
        "top_k": "Sets the XTTS top-k sampling limit when xtts_send_top_k is enabled; the provider interprets the sampling count.",
        "top_p": "Sets the XTTS nucleus-sampling probability when xtts_send_top_p is enabled; the provider interprets the probability value.",
        "do_sample": "Enables XTTS sampling when xtts_send_do_sample is enabled; the stored value is not forwarded unless that corresponding switch is on.",
        "num_beams": "Sets the XTTS beam count when xtts_send_num_beams is enabled; the provider uses it for non-sampling search behavior.",
        "enable_text_splitting": "Controls XTTS-side text splitting when xtts_send_enable_text_splitting is enabled; it is distinct from Pandrator's local speech-block segmentation.",
        "stream_chunk_size": "Sets the XTTS streaming chunk size when xtts_send_stream_chunk_size is enabled; the unit and accepted range are defined by the XTTS server.",
        "gpt_cond_len": "Sets XTTS's reference-conditioning length when xtts_send_gpt_cond_len is enabled; XTTS defines the interpretation of this value.",
        "gpt_cond_chunk_len": "Sets XTTS's conditioning chunk length when xtts_send_gpt_cond_chunk_len is enabled; XTTS defines the interpretation of this value.",
        "max_ref_len": "Sets XTTS's maximum reference length when xtts_send_max_ref_len is enabled; XTTS defines the interpretation of this value.",
        "sound_norm_refs": "Requests XTTS reference-audio normalization when xtts_send_sound_norm_refs is enabled; the switch controls whether the stored provider option is forwarded.",
        "overlap_wav_len": "Sets XTTS streaming waveform overlap when xtts_send_overlap_wav_len is enabled; XTTS defines the unit and accepted range.",
        "xtts_send_temperature": "Gates forwarding temperature to XTTS; when false, the stored temperature is omitted from the XTTS request.",
        "xtts_send_length_penalty": "Gates forwarding length_penalty to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_repetition_penalty": "Gates forwarding repetition_penalty to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_top_k": "Gates forwarding top_k to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_top_p": "Gates forwarding top_p to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_do_sample": "Gates forwarding do_sample to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_num_beams": "Gates forwarding num_beams to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_stream_chunk_size": "Gates forwarding stream_chunk_size to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_enable_text_splitting": "Gates forwarding enable_text_splitting to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_gpt_cond_len": "Gates forwarding gpt_cond_len to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_gpt_cond_chunk_len": "Gates forwarding gpt_cond_chunk_len to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_max_ref_len": "Gates forwarding max_ref_len to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_sound_norm_refs": "Gates forwarding sound_norm_refs to XTTS; when false, the stored value is omitted from the XTTS request.",
        "xtts_send_overlap_wav_len": "Gates forwarding overlap_wav_len to XTTS; when false, the stored value is omitted from the XTTS request.",
        "voxcpm_cfg_value": "Sets the VoxCPM classifier-free guidance value in the provider payload; the adapter clamps it to 0.01 through 20.",
        "voxcpm_inference_timesteps": "Sets the number of VoxCPM inference steps; the adapter clamps it to one through 200 provider steps.",
        "voxcpm_normalize": "Requests VoxCPM output normalization in the provider payload; only VoxCPM consumes this switch.",
        "voxcpm_denoise": "Requests VoxCPM denoising in the provider payload; only VoxCPM consumes this switch.",
        "voxcpm_retry_badcase": "Enables VoxCPM's provider-side retry for detected bad cases; other TTS services ignore this VoxCPM-only switch.",
        "voxcpm_retry_badcase_max_times": "Limits VoxCPM bad-case retries; the adapter clamps the count to one through twenty.",
        "voxcpm_retry_badcase_ratio_threshold": "Sets the VoxCPM bad-case ratio threshold that triggers its retry path; the adapter clamps the provider value to 0.01 through 50.",
        "voxcpm_min_len": "Sets the minimum VoxCPM generated length accepted by its provider adapter; the adapter enforces at least one provider unit.",
        "voxcpm_max_len": "Sets the maximum VoxCPM generated length accepted by its provider adapter; it is kept at least as large as one provider unit and may be adjusted relative to the minimum.",
        "fishs2_temperature": "Sets FishS2 sampling temperature; the FishS2 adapter clamps this provider-defined probability control to 0 through 1.",
        "fishs2_top_p": "Sets FishS2 nucleus-sampling probability; the FishS2 adapter clamps it to 0 through 1.",
        "fishs2_chunk_length": "Sets the FishS2 request chunk length in provider-defined units; the adapter clamps it to 100 through 300.",
        "fishs2_latency": "Selects FishS2's normal or balanced latency mode; only FishS2 consumes this provider option.",
        "fishs2_normalize": "Requests FishS2 provider-side audio normalization; only FishS2 consumes this switch.",
        "fishs2_prosody_volume": "Sets FishS2 prosody volume in provider-defined units; the adapter clamps the value to -20 through 20.",
        "fishs2_normalize_loudness": "Requests FishS2 loudness normalization within its prosody options; only FishS2 consumes this switch.",
        "kokoro_default_voices": "Stores language-to-voice defaults intended for Kokoro selection, but this setting currently has no consumer in the active TTS runtime.",
        "silero_stress_mode": "Selects the stress-handling mode sent to the Silero provider; the accepted modes are provider-defined.",
        "silero_sample_rate": "Sets the sample rate requested from Silero in hertz; only the Silero adapter consumes this value.",
        "voxtral_max_frames": "Sets the Voxtral maximum generated frame count in provider-defined units; the value is embedded in Voxtral options.",
        "voxtral_euler_steps": "Sets the Voxtral Euler sampling-step count in provider-defined units; the value is embedded in Voxtral options.",
        "voxtral_chunk": "Enables Voxtral input chunking before synthesis; only the Voxtral adapter consumes this switch.",
        "voxtral_max_chunk_chars": "Sets the maximum characters in a Voxtral input chunk; it is used only when Voxtral chunking is active.",
        "voxtral_chunk_silence_ms": "Sets the silence inserted or recognized between Voxtral chunks in milliseconds; only Voxtral consumes this value.",
        "voxtral_strip_quotes": "Requests removal of quote marks from text before Voxtral synthesis; only Voxtral consumes this switch.",
        "voxtral_strip_diacritics": "Requests removal of diacritics from text before Voxtral synthesis; only Voxtral consumes this switch.",
        "voxtral_level_audio": "Requests Voxtral output audio leveling; only Voxtral consumes this switch.",
        "chatterbox_temperature": "Sets Chatterbox sampling temperature in its provider payload; Chatterbox falls back to the common temperature when this value is absent.",
        "chatterbox_repetition_penalty": "Sets Chatterbox repetition penalty; the adapter enforces a minimum provider value of one.",
        "chatterbox_min_p": "Sets Chatterbox minimum-p sampling control; only Chatterbox consumes this provider-defined value.",
        "chatterbox_top_p": "Sets Chatterbox nucleus-sampling probability; only Chatterbox consumes this provider-defined value.",
        "chatterbox_top_k": "Sets Chatterbox top-k sampling count; only Chatterbox consumes this provider-defined value.",
        "chatterbox_exaggeration": "Sets Chatterbox expressive exaggeration; only Chatterbox consumes this provider-defined value.",
        "chatterbox_cfg_weight": "Sets Chatterbox classifier-free guidance weight; only Chatterbox consumes this provider-defined value.",
        "chatterbox_norm_loudness": "Requests Chatterbox output loudness normalization; only Chatterbox consumes this switch.",
        "openai_audio_endpoint": "Selects a configured OpenAI-compatible or custom audio endpoint by its stored identifier; endpoint URLs and credentials remain in provider configuration rather than this registry.",
        "openai_audio_instructions": "Provides optional provider instructions for OpenAI-compatible, Gemini-compatible, or custom audio adapters; the active adapter determines whether and how they affect synthesis.",
        "generation_prompt": "Provides speaking directions for adapters that support guided speech, such as Gemini, Qwen, and compatible TTS services; it is distinct from transcript text and may be ignored by other providers.",
        "speech_block_min_chars": "Sets the preferred minimum characters for local Pandrator speech-block segmentation before TTS; it does not change the visible subtitle cues.",
        "speech_block_max_chars": "Sets the maximum characters in a local Pandrator speech block sent toward TTS; it does not impose a display-subtitle line limit.",
        "speech_block_merge_threshold": "Sets the maximum local timing gap, in milliseconds, for packing complete speech blocks together; this affects TTS segmentation only, not subtitle finalization.",
        "speech_block_continuation_threshold_ms": "Sets how many milliseconds of pause a local speech-block builder may bridge when an utterance appears to continue; it affects segmentation only, not displayed subtitle boundaries.",
        "speech_block_max_internal_gap_ms": "Sets the maximum internal silent gap, in milliseconds, permitted inside one local TTS speech block; it prevents a block from spanning a long pause.",
    },
    "audio": {
        "audio_verification_mode": "Selects whether generated speech takes receive the optional raw-signal screen; off disables checks and signal records RMS, clipping, DC offset, tails, and duration anomalies.",
        "sentence_silence_ms": "Adds this many milliseconds between generated sentence audio parts during local assembly; it controls audiobook and voiceover pacing rather than subtitle timing.",
        "paragraph_silence_ms": "Adds this many milliseconds between generated paragraph audio parts during local assembly; it controls long-form narration pacing.",
        "fade_enabled": "Enables applying the configured fade-in and fade-out durations to assembled audio parts.",
        "fade_in_ms": "Sets the fade-in duration applied to each assembled audio part when fade_enabled is true, measured in milliseconds.",
        "fade_out_ms": "Sets the fade-out duration applied to each assembled audio part when fade_enabled is true, measured in milliseconds.",
        "synchronization_delay_ms": "Sets the maximum initial voiceover delay, in milliseconds, allowed while aligning generated speech blocks to subtitle timing.",
        "synchronization_speed": "Sets the maximum synchronization catch-up speed: values at or below 10 are interpreted as a multiplier and values above 10 as a percentage; alignment caps the effective speed at 1x through 4x.",
        "synchronization_sentence_gap_ms": "Adds this many milliseconds between sentence files inside one subtitle-timed speech block before alignment computes catch-up speed.",
    },
    "rvc": {
        "enabled": "Enables applying the selected RVC voice-conversion model to generated or explicitly converted audio; a model is still required for conversion.",
        "model": "Selects the RVC model identifier sent to the local RVC conversion service; the available identifiers come from that service's model catalogue.",
        "pitch": "Sets the integer pitch shift sent to the RVC conversion service; the service defines the audible semitone interpretation.",
        "filter_radius": "Sets the RVC filter radius sent to the conversion service; the service defines the smoothing behavior and accepted range.",
        "index_rate": "Sets the RVC index influence sent to the conversion service; the service defines how this provider value blends indexed voice features.",
        "volume_envelope": "Sets the RVC volume-envelope mix sent to the conversion service; the service defines how the value preserves source dynamics.",
        "protect": "Sets the RVC protection value sent to the conversion service; the service defines which consonant or voiceless regions it protects.",
        "f0_method": "Selects the fundamental-frequency extraction method sent to the RVC service; the method names and support are service-defined.",
    },
    "source_cleaning": {
        "agentic": "Enables the multi-phase LLM source-cleaning pipeline after deterministic cleanup; when disabled, only deterministic source operations run.",
        "max_iterations": "Provides the total legacy model-turn budget distributed across source-cleaning phases when explicit per-phase limits are absent.",
        "pdf_ocr_mode": "Controls PDF OCR: auto uses OCR when extracted text is inadequate, off never uses OCR, and force always renders and recognizes pages.",
        "pdf_ocr_language": "Selects the OCR language hint for PDF recognition, or auto to infer a suitable language.",
        "pdf_ocr_dpi": "Sets the resolution used when rendering PDF pages for OCR; higher values can improve recognition at greater memory and runtime cost.",
        "pdf_remove_toc": "Removes PDF blocks identified as table-of-contents or navigation material during source cleaning.",
        "pdf_remove_repeated_marginals": "Removes repeated PDF headers, footers, and page-number-like marginal text detected across pages.",
        "remove_footnotes": "Permits deterministic and agentic source cleaning to remove footnote content instead of preserving resolved notes in narration.",
        "filter_citations": "Filters bibliographic citation references while resolving footnotes and validating the cleaned source.",
        "phase_max_iterations": "Overrides model-turn limits by source-cleaning phase; supported keys are metadata, navigation, boilerplate, repeated_elements, and chapter_marking.",
        "request_timeout_seconds": "Sets the timeout for each source-cleaning model request, in seconds.",
    },
    "output": {
        "format": "Selects the container used for assembled audio output; supported values are WAV, MP3, M4B, Opus, and FLAC, with M4B restricted to audiobook workflows.",
        "bitrate": "Sets the codec bitrate string used for lossy audio formats such as MP3, M4B, and Opus; the codec and provider/FFmpeg parser define accepted bitrate syntax.",
        "export_mode": "Selects whether export produces media, subtitle files, or concatenated text; subtitle workflows are restricted to subtitle or text export.",
        "audio_mode": "Selects the source-audio policy for voiceover media export: preserve source audio, mix source with generated speech, or export dubbing only.",
        "subtitle_mode": "Selects whether selected subtitles are omitted, muxed as soft tracks, or burned into a rendered video; burning forces video transcoding.",
        "subtitle_selection": "Selects the source subtitle track, translation track, or both when exporting or attaching subtitle tracks.",
        "subtitle_format": "Selects SRT or WebVTT for exported subtitle files and soft subtitle tracks.",
        "video_transcode": "Requests video transcoding even when subtitle rendering does not already require it; the selected encoder, resolution, quality, speed, and audio settings then apply.",
        "burn_video_encoder": "Selects the FFmpeg video encoder used when video is transcoded or subtitles are burned; the encoder must be available in the active FFmpeg build.",
        "burn_video_resolution": "Selects the output video height preset, from source resolution through 360p, 480p, 720p, 1080p, 1440p, or 2160p, when video is transcoded.",
        "burn_video_quality": "Sets the FFmpeg video quality value for transcoding; the runtime accepts the encoder quality scale from 0 through 51.",
        "burn_video_speed": "Selects FFmpeg video encoding speed: fast, balanced, or quality; the chosen preset trades encoding time against compression efficiency.",
        "burn_audio_codec": "Selects whether source or rendered video audio is copied or encoded as AAC during media output; AAC also requires a valid bitrate string.",
        "burn_audio_bitrate": "Sets the AAC bitrate string used for dubbed or mixed video audio; video export validates forms such as 192k or 2M.",
        "mix_source_gain_db": "Applies this source-soundtrack gain in dB before voiceover mixing; the mix filter bounds it to -60 through 12 dB.",
        "mix_voice_gain_db": "Applies this generated-voice gain in dB before voiceover mixing; the mix filter bounds it to -30 through 12 dB.",
        "mix_voice_lufs": "Sets the generated-voice loudness target in LUFS for normalization before mixing; the mix filter bounds it to -30 through -8 LUFS.",
        "mix_ducking": "Selects the source-ducking preset under generated speech: off, gentle, balanced, strong, or very strong.",
        "mix_attack_ms": "Sets the ducking compressor attack in milliseconds; the mix filter bounds it to 1 through 2000 milliseconds.",
        "mix_release_ms": "Sets the ducking compressor release in milliseconds; the mix filter bounds it to 10 through 5000 milliseconds.",
        "mix_audio_bitrate": "Sets the bitrate string used when exporting a mixed standalone audio file; FFmpeg validates the value for the selected output format.",
        "title": "Sets the exported media title metadata when the output container supports title tags.",
        "artist": "Sets the exported media artist metadata when the output container supports artist tags.",
        "album": "Sets the exported media album metadata when the output container supports album tags.",
        "genre": "Sets the exported media genre metadata when the output container supports genre tags.",
        "language": "Sets the language metadata attached to exported subtitle tracks or media where the selected output path supports it.",
        "cover_artifact_id": "Selects an existing image artifact to embed as audiobook cover artwork; it is resolved during audiobook export and is not a general audio setting.",
    },
}


_METADATA: dict[str, dict[str, dict[str, object]]] = {
    "text": {
        "max_sentence_length": {"minimum": 1, "unit": "characters"},
        "remove_diacritics": {
            "caveat": "Transliteration is destructive and can alter proper names or intended pronunciation."
        },
        "enable_nemo_normalization": {
            "applicability": "Available where the NeMo text-normalization dependency is installed."
        },
        "llm_tts_optimization": {
            "caveat": "Generation-time and document-level speech optimization represent different workflow stages; avoid enabling both unless the text should intentionally be processed twice."
        },
        "llm_tts_document_optimization": {
            "caveat": "Generation-time and document-level speech optimization represent different workflow stages; avoid enabling both unless the text should intentionally be processed twice."
        },
        "llm_processing_enabled": {
            "caveat": "Legacy compatibility setting with no independent web-workflow stage; use the specific correction, translation, or speech-optimization setting."
        },
        "llm_tts_batch_size": {"minimum": 1, "maximum": 64, "unit": "units"},
        "llm_tts_document_batch_size": {
            "minimum": 1,
            "maximum": 64,
            "unit": "units",
        },
        "llm_concurrent_calls": {"minimum": 1, "maximum": 16},
        "speech_optimization_mode": {"choices": ["guarded", "flexible"]},
        "speech_plan_min_retention": {
            "minimum": 0,
            "maximum": 1,
            "applicability": "Flexible speech-optimization mode.",
        },
        "first_prompt": {"applicability": "Multi-stage speech optimization."},
        "second_prompt": {"applicability": "Multi-stage speech optimization."},
        "third_prompt": {"applicability": "Multi-stage speech optimization."},
        "combined_prompt": {"applicability": "Single-stage speech optimization."},
    },
    "stt": {
        "stt_engine": {
            "choices": ["whisper", "parakeet", "moss", "azure_mai_transcribe_1_5"]
        },
        "stt_model_quantization": {
            "applicability": "Local CrispASR engines only; available choices depend on the selected engine."
        },
        "stt_compute_backend": {
            "choices": ["auto", "cpu", "cuda", "vulkan", "metal"],
            "applicability": "Local CrispASR engines only.",
        },
        "stt_compute_device": {
            "minimum": 0,
            "applicability": "Local non-CPU CrispASR engines only.",
        },
        "stt_threads": {"minimum": 0, "applicability": "Local CrispASR engines only."},
        "stt_chunk_seconds": {
            "minimum": 0,
            "unit": "seconds",
            "applicability": "Local CrispASR engines; Azure cloud STT uses provider-specific chunk planning.",
        },
        "stt_chunk_overlap_seconds": {
            "minimum": 0,
            "unit": "seconds",
            "applicability": "Local non-MOSS CrispASR engines only.",
        },
        "stt_transcribe_style": {
            "choices": ["readability", "verbatim"],
            "applicability": "Azure MAI-Transcribe-1.5 cloud STT only.",
        },
        "stt_beam_size": {
            "minimum": 1,
            "applicability": "Local non-MOSS CrispASR engines only.",
        },
        "parakeet_decoder": {
            "choices": ["ctc", "tdt", "maes"],
            "applicability": "Local Parakeet only.",
        },
        "moss_max_chunk_seconds": {
            "minimum": 30,
            "maximum": 120,
            "unit": "seconds",
            "applicability": "Local MOSS fallback context only.",
        },
        "moss_chunk_overlap_seconds": {
            "minimum": 0,
            "unit": "seconds",
            "applicability": "Local MOSS only.",
        },
        "moss_vad_enabled": {"applicability": "Local MOSS only."},
        "moss_ctc_alignment_enabled": {"applicability": "Local MOSS only."},
        "moss_ctc_padding_seconds": {
            "minimum": 0,
            "maximum": 2,
            "unit": "seconds",
            "applicability": "Local MOSS CTC alignment only.",
        },
        "crispasr_vad_enabled": {
            "applicability": "Local Whisper and Parakeet only; MOSS uses moss_vad_enabled."
        },
        "crispasr_vad_model": {
            "choices": ["silero", "firered", "marblenet", "whisper-vad"],
            "applicability": "Local CrispASR VAD, including MOSS when its VAD switch is enabled.",
        },
        "crispasr_vad_threshold": {
            "minimum": 0,
            "maximum": 1,
            "applicability": "Local CrispASR VAD only.",
        },
        "crispasr_vad_min_speech_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Local CrispASR VAD only.",
        },
        "crispasr_vad_min_silence_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Local CrispASR VAD only.",
        },
        "crispasr_vad_speech_pad_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Local CrispASR VAD only.",
        },
        "crispasr_vad_max_speech_seconds": {
            "minimum": 1,
            "unit": "seconds",
            "applicability": "Local CrispASR VAD only.",
        },
        "diarization_enabled": {
            "applicability": "Local non-MOSS CrispASR only; MOSS has native turns and Azure cloud STT does not support diarization."
        },
    },
    "subtitles": {
        "max_lines": {"minimum": 1, "maximum": 3},
        "max_chars_per_line": {"minimum": 20, "maximum": 100, "unit": "characters"},
        "max_cps": {"minimum": 5, "maximum": 40, "unit": "characters per second"},
        "min_duration_ms": {"minimum": 250, "maximum": 3000, "unit": "milliseconds"},
        "max_duration_ms": {"minimum": 1000, "maximum": 15000, "unit": "milliseconds"},
        "min_gap_ms": {"minimum": 0, "maximum": 500, "unit": "milliseconds"},
        "phrase_gap_ms": {"minimum": 100, "maximum": 3000, "unit": "milliseconds"},
        "hard_gap_ms": {"minimum": 250, "maximum": 5000, "unit": "milliseconds"},
        "sentence_boundary_threshold": {"minimum": 0.01, "maximum": 0.99},
        "boundary_correction_enabled": {
            "caveat": "No current consumer is wired for this setting; changing it does not currently run boundary correction."
        },
        "merge_threshold_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "caveat": "Used only as a legacy speech-block fallback when the dedicated TTS merge threshold is absent.",
        },
    },
    "correction": {
        **_TRANSFORMATION_METADATA,
        **_WEB_RESEARCH_METADATA,
    },
    "translation": {
        **_TRANSFORMATION_METADATA,
        "backend": {"choices": ["llm", "deepl"]},
        "source_language": {"applicability": "Translation only."},
        "target_language": {"applicability": "Translation only."},
        "glossary": {"applicability": "Translation when glossary_enabled is true."},
        "context": {"applicability": "Translation only."},
        "glossary_enabled": {"applicability": "Translation only."},
        **_WEB_RESEARCH_METADATA,
        "web_research_enabled": {
            "applicability": "LLM translation only; DeepL translation rejects web research."
        },
    },
    "tts": {
        "use_external_server": {
            "caveat": "The resolver has conditional support, but practical endpoint wiring depends on the selected service profile and deployment."
        },
        "external_server_url": {
            "caveat": "The URL is only considered for the selected service when use_external_server is enabled; connectivity and adapter compatibility are not guaranteed by this registry."
        },
        "speed": {"unit": "provider-defined"},
        "max_attempts": {"minimum": 1, "maximum": 20},
        "tts_batch_size": {"minimum": 1, "maximum": 32, "unit": "segments"},
        "temperature": {"applicability": "XTTS when xtts_send_temperature is enabled."},
        "length_penalty": {
            "applicability": "XTTS when xtts_send_length_penalty is enabled."
        },
        "repetition_penalty": {
            "applicability": "XTTS when xtts_send_repetition_penalty is enabled, with compatible adapters potentially using the common fallback."
        },
        "top_k": {"applicability": "XTTS when xtts_send_top_k is enabled."},
        "top_p": {"applicability": "XTTS when xtts_send_top_p is enabled."},
        "do_sample": {"applicability": "XTTS when xtts_send_do_sample is enabled."},
        "num_beams": {"applicability": "XTTS when xtts_send_num_beams is enabled."},
        "enable_text_splitting": {
            "applicability": "XTTS when xtts_send_enable_text_splitting is enabled."
        },
        "stream_chunk_size": {
            "unit": "provider-defined",
            "applicability": "XTTS when xtts_send_stream_chunk_size is enabled.",
        },
        "gpt_cond_len": {
            "unit": "provider-defined",
            "applicability": "XTTS when xtts_send_gpt_cond_len is enabled.",
        },
        "gpt_cond_chunk_len": {
            "unit": "provider-defined",
            "applicability": "XTTS when xtts_send_gpt_cond_chunk_len is enabled.",
        },
        "max_ref_len": {
            "unit": "provider-defined",
            "applicability": "XTTS when xtts_send_max_ref_len is enabled.",
        },
        "sound_norm_refs": {
            "applicability": "XTTS when xtts_send_sound_norm_refs is enabled."
        },
        "overlap_wav_len": {
            "unit": "provider-defined",
            "applicability": "XTTS when xtts_send_overlap_wav_len is enabled.",
        },
        **{
            key: {"applicability": "XTTS forwarding switch."}
            for key in (
                "xtts_send_temperature",
                "xtts_send_length_penalty",
                "xtts_send_repetition_penalty",
                "xtts_send_top_k",
                "xtts_send_top_p",
                "xtts_send_do_sample",
                "xtts_send_num_beams",
                "xtts_send_stream_chunk_size",
                "xtts_send_enable_text_splitting",
                "xtts_send_gpt_cond_len",
                "xtts_send_gpt_cond_chunk_len",
                "xtts_send_max_ref_len",
                "xtts_send_sound_norm_refs",
                "xtts_send_overlap_wav_len",
            )
        },
        "voxcpm_cfg_value": {
            "minimum": 0.01,
            "maximum": 20,
            "applicability": "VoxCPM only.",
        },
        "voxcpm_inference_timesteps": {
            "minimum": 1,
            "maximum": 200,
            "unit": "provider-defined steps",
            "applicability": "VoxCPM only.",
        },
        "voxcpm_retry_badcase_max_times": {
            "minimum": 1,
            "maximum": 20,
            "applicability": "VoxCPM only.",
        },
        "voxcpm_retry_badcase_ratio_threshold": {
            "minimum": 0.01,
            "maximum": 50,
            "applicability": "VoxCPM only.",
        },
        "voxcpm_min_len": {
            "minimum": 1,
            "unit": "provider-defined",
            "applicability": "VoxCPM only.",
        },
        "voxcpm_max_len": {
            "minimum": 1,
            "unit": "provider-defined",
            "applicability": "VoxCPM only.",
        },
        "fishs2_temperature": {
            "minimum": 0,
            "maximum": 1,
            "applicability": "FishS2 only.",
        },
        "fishs2_top_p": {"minimum": 0, "maximum": 1, "applicability": "FishS2 only."},
        "fishs2_chunk_length": {
            "minimum": 100,
            "maximum": 300,
            "unit": "provider-defined",
            "applicability": "FishS2 only.",
        },
        "fishs2_latency": {
            "choices": ["normal", "balanced"],
            "applicability": "FishS2 only.",
        },
        "fishs2_prosody_volume": {
            "minimum": -20,
            "maximum": 20,
            "unit": "provider-defined",
            "applicability": "FishS2 only.",
        },
        "fishs2_normalize": {"applicability": "FishS2 only."},
        "fishs2_normalize_loudness": {"applicability": "FishS2 only."},
        "kokoro_default_voices": {
            "caveat": "No current consumer is wired for this setting; active Kokoro voice selection uses the selected voice/provider catalogue."
        },
        "silero_stress_mode": {"applicability": "Silero only."},
        "silero_sample_rate": {
            "minimum": 1,
            "unit": "hertz",
            "applicability": "Silero only.",
        },
        "voxtral_max_frames": {
            "minimum": 0,
            "unit": "provider-defined frames",
            "applicability": "Voxtral only.",
        },
        "voxtral_euler_steps": {
            "minimum": 0,
            "unit": "provider-defined steps",
            "applicability": "Voxtral only.",
        },
        "voxtral_max_chunk_chars": {
            "minimum": 0,
            "unit": "characters",
            "applicability": "Voxtral only when chunking is enabled.",
        },
        "voxtral_chunk_silence_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Voxtral only when chunking is enabled.",
        },
        "voxtral_chunk": {"applicability": "Voxtral only."},
        "voxtral_strip_quotes": {"applicability": "Voxtral only."},
        "voxtral_strip_diacritics": {"applicability": "Voxtral only."},
        "voxtral_level_audio": {"applicability": "Voxtral only."},
        "chatterbox_temperature": {
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_repetition_penalty": {
            "minimum": 1,
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_min_p": {
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_top_p": {
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_top_k": {
            "minimum": 0,
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_exaggeration": {
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_cfg_weight": {
            "unit": "provider-defined",
            "applicability": "Chatterbox only.",
        },
        "chatterbox_norm_loudness": {"applicability": "Chatterbox only."},
        "openai_audio_endpoint": {
            "applicability": "OpenAI-compatible or custom audio endpoint selection."
        },
        "openai_audio_instructions": {
            "applicability": "OpenAI-compatible, Gemini-compatible, or custom audio adapters when supported."
        },
        "generation_prompt": {
            "applicability": "Providers whose adapter supports guided speech."
        },
        "speech_block_min_chars": {
            "minimum": 1,
            "unit": "characters",
            "applicability": "Local Pandrator speech-block segmentation only.",
        },
        "speech_block_max_chars": {
            "minimum": 1,
            "unit": "characters",
            "applicability": "Local Pandrator speech-block segmentation only.",
        },
        "speech_block_merge_threshold": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Local Pandrator speech-block segmentation only.",
        },
        "speech_block_continuation_threshold_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Local Pandrator speech-block segmentation only.",
        },
        "speech_block_max_internal_gap_ms": {
            "minimum": 0,
            "unit": "milliseconds",
            "applicability": "Local Pandrator speech-block segmentation only.",
        },
    },
    "audio": {
        "audio_verification_mode": {"choices": ["off", "signal"]},
        "sentence_silence_ms": {"minimum": 0, "unit": "milliseconds"},
        "paragraph_silence_ms": {"minimum": 0, "unit": "milliseconds"},
        "fade_in_ms": {"minimum": 0, "unit": "milliseconds"},
        "fade_out_ms": {"minimum": 0, "unit": "milliseconds"},
        "synchronization_delay_ms": {"minimum": 0, "unit": "milliseconds"},
        "synchronization_speed": {
            "minimum": 1,
            "unit": "multiplier or percentage",
            "caveat": "Values <=10 are converted as multipliers; values >10 are treated as percentages, and alignment caps effective catch-up at 4x.",
        },
        "synchronization_sentence_gap_ms": {
            "minimum": 0,
            "maximum": 5000,
            "unit": "milliseconds",
        },
    },
    "rvc": {
        "pitch": {"unit": "provider-defined"},
        "filter_radius": {"unit": "provider-defined"},
        "index_rate": {"unit": "provider-defined"},
        "volume_envelope": {"unit": "provider-defined"},
        "protect": {"unit": "provider-defined"},
        "f0_method": {"unit": "provider-defined"},
    },
    "source_cleaning": {
        "max_iterations": {
            "minimum": 1,
            "unit": "model/tool turns",
            "caveat": "Used to distribute a legacy total only when phase_max_iterations does not provide explicit phase limits.",
        },
        "pdf_ocr_mode": {"choices": ["auto", "off", "force"]},
        "pdf_ocr_dpi": {"minimum": 120, "maximum": 400, "unit": "DPI"},
        "phase_max_iterations": {
            "minimum": 1,
            "maximum": 100,
            "unit": "model/tool turns per phase",
        },
        "request_timeout_seconds": {"minimum": 1, "unit": "seconds"},
    },
    "output": {
        "format": {"choices": ["wav", "mp3", "m4b", "opus", "flac"]},
        "export_mode": {"choices": ["media", "subtitles", "text"]},
        "audio_mode": {"choices": ["mixed", "preserve", "dubbing_only"]},
        "subtitle_mode": {"choices": ["none", "soft", "burned"]},
        "subtitle_selection": {"choices": ["source", "translation", "dual"]},
        "subtitle_format": {"choices": ["srt", "vtt"]},
        "burn_video_quality": {"minimum": 0, "maximum": 51},
        "burn_video_speed": {"choices": ["fast", "balanced", "quality"]},
        "burn_audio_codec": {"choices": ["copy", "aac"]},
        "mix_source_gain_db": {"minimum": -60, "maximum": 12, "unit": "dB"},
        "mix_voice_gain_db": {"minimum": -30, "maximum": 12, "unit": "dB"},
        "mix_voice_lufs": {"minimum": -30, "maximum": -8, "unit": "LUFS"},
        "mix_ducking": {
            "choices": ["off", "gentle", "balanced", "strong", "very_strong"]
        },
        "mix_attack_ms": {"minimum": 1, "maximum": 2000, "unit": "milliseconds"},
        "mix_release_ms": {"minimum": 10, "maximum": 5000, "unit": "milliseconds"},
    },
}


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def _build_registry() -> dict[str, dict[str, dict[str, object]]]:
    registry: dict[str, dict[str, dict[str, object]]] = {}
    for section in DOCUMENTED_SECTIONS:
        defaults = BUILTIN_DEFAULTS.get(section, {})
        descriptions = _DESCRIPTIONS.get(section, {})
        missing = [name for name in defaults if name not in descriptions]
        if missing:
            raise RuntimeError(
                f"Missing parameter descriptions for {section}: {', '.join(missing)}"
            )
        registry[section] = {}
        for name, default in defaults.items():
            item: dict[str, object] = {
                "section": section,
                "name": name,
                "label": _label(name),
                "description": descriptions[name],
                "default": deepcopy(default),
                "value_type": _value_type(default),
            }
            item.update(deepcopy(_METADATA.get(section, {}).get(name, {})))
            registry[section][name] = item
    return registry


_REGISTRY = _build_registry()
# Public inspection copy: mutating it cannot alter the source used by
# describe_parameters.  Consumers should use describe_parameters for output.
PARAMETER_DEFINITIONS = deepcopy(_REGISTRY)
PARAMETER_DEFINITION_REGISTRY = PARAMETER_DEFINITIONS


def _filter_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    else:
        try:
            values = list(value)
        except TypeError as error:
            raise ValueError(
                "Filters must be strings or iterables of strings."
            ) from error
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(  # noqa: TRY004 - public filter validation uses ValueError
                "Filters must contain only strings."
            )
        item = item.strip()
        if item:
            result.append(item)
    return result


def describe_parameters(
    *,
    sections=(),
    names=(),
    workflow_kind=None,
    query=None,
    limit=100,
) -> dict[str, object]:
    """Return matching parameter explanations in deterministic order."""

    requested_sections = _filter_values(sections)
    requested_names = _filter_values(names)
    if workflow_kind is not None and not isinstance(workflow_kind, str):
        raise ValueError("workflow_kind must be a string")
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be a string")
    requested_workflow = workflow_kind.strip() if workflow_kind is not None else None
    requested_query = query.strip() if query is not None else None
    if requested_workflow == "":
        requested_workflow = None
    if requested_query == "":
        requested_query = None
    if requested_workflow is not None and requested_workflow not in WORKFLOW_SECTIONS:
        raise ValueError(f"Unsupported workflow kind: {requested_workflow}")
    invalid_sections = [
        section for section in requested_sections if section not in SETTING_SECTIONS
    ]
    if invalid_sections:
        raise ValueError(f"Unsupported section: {invalid_sections[0]}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 300:
        raise ValueError("limit must be an integer from 1 through 300")
    if not (
        requested_sections or requested_names or requested_workflow or requested_query
    ):
        raise ValueError("At least one parameter filter is required")

    section_filter: set[str] | None = set(requested_sections) or None
    if requested_workflow is not None:
        workflow_allowed = WORKFLOW_SECTIONS[requested_workflow]
        section_filter = (
            workflow_allowed
            if section_filter is None
            else section_filter.intersection(workflow_allowed)
        )
    name_filter = set(requested_names)
    query_lower = requested_query.casefold() if requested_query else None
    matches: list[dict[str, object]] = []
    for section in SETTING_SECTIONS:
        if section not in _REGISTRY or (
            section_filter is not None and section not in section_filter
        ):
            continue
        for name in BUILTIN_DEFAULTS.get(section, {}):
            if name_filter and name not in name_filter:
                continue
            item = _REGISTRY[section][name]
            if query_lower is not None:
                searchable = " ".join(
                    str(item.get(field, ""))
                    for field in (
                        "section",
                        "name",
                        "label",
                        "description",
                        "applicability",
                        "caveat",
                    )
                ).casefold()
                if query_lower not in searchable:
                    continue
            matches.append(deepcopy(item))
    return {
        "schema_version": 1,
        "items": matches[:limit],
        "matched_count": len(matches),
        "returned_count": min(limit, len(matches)),
        "truncated": len(matches) > limit,
        "available_sections": list(DOCUMENTED_SECTIONS),
    }


__all__ = [
    "DOCUMENTED_SECTIONS",
    "PARAMETER_DEFINITIONS",
    "PARAMETER_DEFINITION_REGISTRY",
    "WORKFLOW_SECTIONS",
    "describe_parameters",
]
