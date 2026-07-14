from dataclasses import dataclass, field
from typing import List, Dict, Any


def default_llm_model(model_id: str) -> Dict[str, Any]:
    return {
        "id": str(model_id),
        "default_temperature": None,
        "default_reasoning_effort": "",
        "input_cost_per_million": None,
        "cached_input_cost_per_million": None,
        "output_cost_per_million": None,
    }


def default_llm_provider_configs() -> List[Dict[str, Any]]:
    return [
        {
            "id": "openai",
            "name": "OpenAI",
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": [default_llm_model("gpt-5.4"), default_llm_model("gpt-5.4-mini")],
        },
        {
            "id": "gemini",
            "name": "Gemini",
            "provider": "gemini",
            "api_base": "https://generativelanguage.googleapis.com/v1beta",
            "api_key_env": "GEMINI_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": [
                default_llm_model("gemini-3.1-pro-preview"),
                default_llm_model("gemini-3-flash-preview"),
            ],
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "provider": "anthropic",
            "api_base": "https://api.anthropic.com/v1",
            "api_key_env": "ANTHROPIC_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": [
                default_llm_model("claude-opus-4-7"),
                default_llm_model("claude-sonnet-4-6"),
            ],
        },
    ]


def default_tts_service_configs() -> List[Dict[str, Any]]:
    return [
        {
            "id": "xtts",
            "name": "XTTS",
            "api_base": "http://127.0.0.1:8020",
            "kind": "local",
        },
        {
            "id": "voxcpm",
            "name": "VoxCPM",
            "api_base": "http://127.0.0.1:8020",
            "kind": "local",
        },
        {
            "id": "fishs2",
            "name": "FishS2",
            "api_base": "http://127.0.0.1:8020",
            "kind": "local",
        },
        {
            "id": "voxtral",
            "name": "Voxtral",
            "api_base": "http://127.0.0.1:8000",
            "kind": "local",
        },
        {
            "id": "kokoro",
            "name": "Kokoro",
            "api_base": "http://127.0.0.1:8880",
            "kind": "local",
        },
        {
            "id": "magpie",
            "name": "Magpie",
            "api_base": "http://127.0.0.1:8030",
            "kind": "local",
        },
        {
            "id": "silero",
            "name": "Silero",
            "api_base": "http://127.0.0.1:8001",
            "kind": "local",
        },
        {
            "id": "chatterbox",
            "name": "Chatterbox",
            "api_base": "http://127.0.0.1:8040",
            "kind": "local",
        },
        {
            "id": "kobold_qwen",
            "name": "Qwen3 TTS",
            "api_base": "http://127.0.0.1:8042",
            "kind": "local",
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "kind": "commercial",
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": ["gpt-4o-mini-tts", "tts-1-hd", "tts-1"],
            "default_model": "gpt-4o-mini-tts",
            "voices": [
                "alloy",
                "ash",
                "ballad",
                "coral",
                "echo",
                "fable",
                "nova",
                "onyx",
                "sage",
                "shimmer",
                "verse",
                "marin",
                "cedar",
            ],
            "default_voice": "alloy",
            "supports_prebuilt_voices": True,
        },
        {
            "id": "gemini",
            "name": "Google Gemini",
            "kind": "commercial",
            "provider": "gemini",
            "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key_env": "GEMINI_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": [
                "gemini-3.1-flash-tts-preview",
                "gemini-2.5-flash-preview-tts",
                "gemini-2.5-pro-preview-tts",
            ],
            "default_model": "gemini-3.1-flash-tts-preview",
            "voices": [
                "Achernar",
                "Achird",
                "Algenib",
                "Algieba",
                "Alnilam",
                "Aoede",
                "Autonoe",
                "Callirrhoe",
                "Charon",
                "Despina",
                "Enceladus",
                "Erinome",
                "Fenrir",
                "Gacrux",
                "Iapetus",
                "Kore",
                "Laomedeia",
                "Leda",
                "Orus",
                "Pulcherrima",
                "Puck",
                "Rasalgethi",
                "Sadachbia",
                "Sadaltager",
                "Schedar",
                "Sulafat",
                "Umbriel",
                "Vindemiatrix",
                "Zephyr",
                "Zubenelgenubi",
            ],
            "default_voice": "Kore",
            "supports_prebuilt_voices": True,
        },
    ]


def default_tts_provider_configs() -> List[Dict[str, Any]]:
    return []


def default_source_cleaning_phase_iterations() -> Dict[str, int]:
    return {
        "metadata": 4,
        "navigation": 11,
        "boilerplate": 11,
        "repeated_elements": 8,
        "chapter_marking": 19,
    }


@dataclass
class TextProcessingSettings:
    enable_nemo_normalization: bool = True
    enable_sentence_splitting: bool = True
    max_sentence_length: int = 200
    enable_sentence_appending: bool = True
    remove_diacritics: bool = False
    remove_quotation_marks: bool = False
    disable_paragraph_detection: bool = False
    remove_footnotes: bool = False
    filter_citations: bool = True
    normalize_all_caps: bool = True


@dataclass
class TTSSettings:
    service: str = "XTTS"
    use_external_server: bool = False
    external_server_url: str = "http://127.0.0.1:8020"
    openai_audio_endpoint: str = ""
    service_configs: List[Dict[str, Any]] = field(default_factory=default_tts_service_configs)
    provider_configs: List[Dict[str, Any]] = field(default_factory=default_tts_provider_configs)
    openai_audio_endpoints_json: str = ""
    openai_audio_instructions: str = ""
    xtts_model: str = ""
    silero_model: str = "v5_cis_base_nostress"
    silero_stress_mode: str = "auto"
    silero_sample_rate: int = 48000
    language: str = "en"
    speaker: str = ""
    speed: float = 1.0
    temperature: float = 0.75
    length_penalty: float = 1.0
    repetition_penalty: float = 5.0
    top_k: int = 50
    top_p: float = 0.85
    do_sample: bool = True
    num_beams: int = 1
    enable_text_splitting: bool = True
    stream_chunk_size: int = 100
    gpt_cond_len: int = 12
    gpt_cond_chunk_len: int = 4
    max_ref_len: int = 12
    sound_norm_refs: bool = False
    overlap_wav_len: int = 1024
    xtts_send_temperature: bool = False
    xtts_send_length_penalty: bool = False
    xtts_send_repetition_penalty: bool = False
    xtts_send_top_k: bool = False
    xtts_send_top_p: bool = False
    xtts_send_do_sample: bool = False
    xtts_send_num_beams: bool = False
    xtts_send_stream_chunk_size: bool = False
    xtts_send_enable_text_splitting: bool = False
    xtts_send_gpt_cond_len: bool = False
    xtts_send_gpt_cond_chunk_len: bool = False
    xtts_send_max_ref_len: bool = False
    xtts_send_sound_norm_refs: bool = False
    xtts_send_overlap_wav_len: bool = False
    voxcpm_cfg_value: float = 1.5
    chatterbox_temperature: float = 0.8
    chatterbox_repetition_penalty: float = 1.2
    chatterbox_min_p: float = 0.05
    chatterbox_top_p: float = 0.95
    chatterbox_top_k: int = 1000
    chatterbox_exaggeration: float = 0.5
    chatterbox_cfg_weight: float = 0.5
    chatterbox_norm_loudness: bool = True
    voxcpm_inference_timesteps: int = 15
    voxcpm_normalize: bool = False
    voxcpm_denoise: bool = False
    voxcpm_retry_badcase: bool = True
    voxcpm_retry_badcase_max_times: int = 3
    voxcpm_retry_badcase_ratio_threshold: float = 6.0
    voxcpm_min_len: int = 2
    voxcpm_max_len: int = 4096
    fishs2_temperature: float = 0.7
    fishs2_top_p: float = 0.7
    fishs2_chunk_length: int = 200
    fishs2_latency: str = "balanced"
    fishs2_normalize: bool = True
    fishs2_prosody_volume: float = 0.0
    fishs2_normalize_loudness: bool = True
    kokoro_default_voices: Dict[str, str] = field(default_factory=lambda: {"en": "af_heart"})
    voxtral_max_frames: int = 1024
    voxtral_euler_steps: int = 8
    voxtral_chunk: bool = False
    voxtral_max_chunk_chars: int = 500
    voxtral_chunk_silence_ms: int = 0
    voxtral_strip_quotes: bool = False
    voxtral_strip_diacritics: bool = False
    voxtral_level_audio: bool = False
    # Populated from server
    tts_models: List[str] = field(default_factory=list)
    tts_speakers: List[str] = field(default_factory=list)

@dataclass
class RVCSettings:
    enable_rvc: bool = False
    rvc_model: str = ""
    pitch: int = 0
    filter_radius: int = 3
    index_rate: float = 0.3
    volume_envelope: float = 1.0
    protect: float = 0.3
    f0_method: str = "rmvpe"

@dataclass
class AudioProcessingSettings:
    silence_between_sentences: int = 300
    silence_for_paragraphs: int = 1000
    enable_fade: bool = True
    fade_in_duration: int = 75
    fade_out_duration: int = 75
    output_format: str = "m4b"
    bitrate: str = "64k"

@dataclass
class PromptSettings:
    prompt_text: str = ""
    enabled: bool = False
    evaluation_enabled: bool = False
    model: str = "default"

@dataclass
class LLMSettings:
    processing_enabled: bool = False
    concurrent_calls: int = 1
    default_model: str = "openai/gpt-5.4-mini"
    provider_configs: List[Dict[str, Any]] = field(default_factory=default_llm_provider_configs)
    request_timeout_seconds: int = 600
    # Retained for one settings migration cycle. Requests use per-model defaults.
    reasoning_effort: str = ""
    use_multi_stage: bool = False
    combined_prompt: PromptSettings = field(default_factory=lambda: PromptSettings(
        prompt_text="Your task is to preprocess and clean the sentence(s) you are given to optimize them for text-to-speech (TTS) synthesis.\n\nPlease perform the following adjustments:\n1. Spell out abbreviations and titles (e.g., Prof. to Professor, Dr. to Doctor, et. al. to et alia, etc. to et cetera).\n2. Convert Roman numerals to English words (e.g., Section III to Section Three, Chapter V to Chapter Five).\n3. Correct any punctuation errors, misspelled words, or OCR artifacts (e.g., remove out-of-place page numbers).\n4. Spell difficult foreign, non-English words phonetically so that an English TTS voice can pronounce them naturally.\n\nDon't change anything else and output ONLY the complete processed text. Include ABSOLUTELY NO comments, NO acknowledgments, NO explanations, and NO notes.\n\nThis is your text: ",
        enabled=True
    ))
    first_prompt: PromptSettings = field(default_factory=lambda: PromptSettings(
        prompt_text="Your task is to spell out abbreviations and titles and convert Roman numerals to English words in the sentence(s) you are given. For example: Prof. to Professor, Dr. to Doctor, et. al. to et alia, etc. to et cetera, Section III to Section Three, Chapter V to Chapter Five and so on. Don't change ANYTHING ELSE and output ONLY the complete processed text. If no adjustments are necessary, just output the sentence(s) without changing or appending ANYTHING. Include ABSOLUTELY NO comments, NO acknowledgments, NO explanations, NO notes and so on. This is your text: ",
        enabled=True
    ))
    second_prompt: PromptSettings = field(default_factory=lambda: PromptSettings(
        prompt_text="Your task is to analyze a text fragment carefully and correct punctuation. Also, correct any misspelled words and possible OCR artifacts based on context. If there is a number that looks out of place because it could have been a page number captured by OCR and doesn't fit in the context, remove it. Don't change ANYTHING ELSE and output ONLY the complete processed text (even if no changes were made). No comments, acknowledgments, explanations or notes. This is your text: "
    ))
    third_prompt: PromptSettings = field(default_factory=lambda: PromptSettings(
        prompt_text="Your task is to spell difficult FOREIGN, NON-ENGLISH words phonetically. Don't alter ANYTHING ELSE in the text - English words remain the same. Don't do anything else, don't add anything, don't include any comments, explanations, notes or acknowledgments. Example: Jiyu means freedom in Japanese becomes jeeyou means freedom in Japanese - jiyu is spelled phonetically as a Japanese word, the rest is not changed. This is your text: "
    ))

@dataclass
class DubbingSettings:
    dubbing_enabled: bool = False
    # ``stt_backend`` is retained as a serialized compatibility alias.
    stt_engine: str = "whisper"
    stt_backend: str = "whisper"
    stt_model_quantization: str = "f16"
    stt_compute_backend: str = "auto"
    stt_compute_device: int = 0
    stt_language: str = "English"
    whisper_model: str = "large-v3"
    whisper_prompt: str = "Hello, welcome to this presentation. This is a professional recording with clear speech, proper punctuation, and standard grammar."
    whisper_align_model: str = ""
    whisper_chunk_size: int = 15
    whisper_save_txt: bool = False
    stt_threads: int = 0
    stt_chunk_seconds: float = 0.0
    stt_chunk_overlap_seconds: float = 3.0
    stt_hotwords: str = ""
    stt_lid_backend: str = "whisper"
    stt_beam_size: int = 1
    parakeet_decoder: str = "tdt"
    parakeet_model: str = "nemo-parakeet-tdt-0.6b-v3"
    parakeet_quantization: str = ""
    parakeet_vad_enabled: bool = True
    parakeet_vad_max_speech_seconds: float = 15.0
    parakeet_vad_threshold: float = 0.5
    parakeet_vad_neg_threshold: float = 0.0
    parakeet_vad_min_silence_ms: float = 100.0
    parakeet_vad_min_speech_ms: float = 250.0
    parakeet_vad_speech_pad_ms: float = 30.0
    parakeet_vad_batch_size: int = 8
    parakeet_hf_cache_dir: str = ""
    parakeet_save_txt: bool = False
    crispasr_vad_enabled: bool = True
    crispasr_vad_model: str = "silero"
    crispasr_vad_threshold: float = 0.5
    crispasr_vad_min_speech_ms: int = 250
    crispasr_vad_min_silence_ms: int = 100
    crispasr_vad_max_speech_seconds: float = 300.0
    crispasr_vad_speech_pad_ms: int = 30
    subtitle_max_chars_per_line: int = 48
    subtitle_max_lines: int = 2
    subtitle_min_duration_ms: int = 833
    subtitle_max_duration_ms: int = 7000
    subtitle_max_cps: float = 20.0
    subtitle_min_gap_ms: int = 80
    subtitle_phrase_gap_ms: int = 600
    speech_block_min_chars: int = 10
    speech_block_max_chars: int = 160
    speech_block_merge_threshold: int = 250
    diarization_enabled: bool = False
    boundary_correction_enabled: bool = False
    subtitle_merge_threshold: int = 250
    correction_enabled: bool = False
    correction_model: str = "default"
    custom_correction_prompt: str = ""
    translation_enabled: bool = False
    llm_char: int = 6000
    max_line_length: int = 42
    context: bool = True
    no_remove_subtitles: bool = False
    translate_prompt: str = ""
    sync_delay_start_ms: int = 2000
    sync_speed_up_percent: int = 115
    original_language: str = "English"
    target_language: str = "en"
    glossary_enabled: bool = False
    translation_backend: str = "llm"
    translation_model: str = "default"
    video_file_path: str = ""


@dataclass
class WorkflowSettings:
    workflow_kind: str = "audiobook"
    workflow_preset: str = "custom"
    included_stages: List[str] = field(default_factory=list)


@dataclass
class WizardSettings:
    show_on_startup: bool = True
    setup_completed_version: int = 0
    wizard_version: int = 1

@dataclass
class SourceCleaningSettings:
    max_iterations: int = 53
    phase_max_iterations: Dict[str, int] = field(default_factory=default_source_cleaning_phase_iterations)
    # None deliberately omits temperature so the selected model uses its own default.
    llm_temperature: float | None = None
    pdf_ocr_mode: str = "auto"
    pdf_ocr_language: str = "auto"
    pdf_ocr_dpi: int = 200
    pdf_remove_toc: bool = True
    pdf_remove_repeated_marginals: bool = True

@dataclass
class AppState:
    session_name: str = "Untitled Session"
    source_file_path: str = ""
    source_display_path: str = ""
    original_source_file_path: str = ""
    pdf_preprocessed: bool = False
    raw_text: str = ""
    processed_sentences: List[Dict[str, Any]] = field(default_factory=list)
    active_audio_variant_id: str = "source"
    metadata: Dict[str, str] = field(default_factory=dict)
    cover_image_path: str | None = None
    text_processing: TextProcessingSettings = field(default_factory=TextProcessingSettings)
    tts: TTSSettings = field(default_factory=TTSSettings)
    rvc: RVCSettings = field(default_factory=RVCSettings)
    audio_processing: AudioProcessingSettings = field(default_factory=AudioProcessingSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    dubbing: DubbingSettings = field(default_factory=DubbingSettings)
    source_cleaning: SourceCleaningSettings = field(default_factory=SourceCleaningSettings)
    workflow: WorkflowSettings = field(default_factory=WorkflowSettings)
    wizard: WizardSettings = field(default_factory=WizardSettings)
