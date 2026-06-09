from dataclasses import dataclass, field
from typing import List, Dict, Any


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
            "models": ["gpt-5.4", "gpt-5.4-mini"],
        },
        {
            "id": "gemini",
            "name": "Gemini",
            "provider": "gemini",
            "api_base": "https://generativelanguage.googleapis.com/v1beta",
            "api_key_env": "GEMINI_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": ["gemini-3.1-pro-preview", "gemini-3-flash-preview"],
        },
        {
            "id": "anthropic",
            "name": "Anthropic",
            "provider": "anthropic",
            "api_base": "https://api.anthropic.com/v1",
            "api_key_env": "ANTHROPIC_API_KEY",
            "api_key": "",
            "is_custom": False,
            "models": ["claude-opus-4-7", "claude-sonnet-4-6"],
        },
    ]


def default_tts_provider_configs() -> List[Dict[str, Any]]:
    return [
        {
            "id": "openai",
            "name": "OpenAI",
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
            "name": "Gemini",
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

@dataclass
class TextProcessingSettings:
    enable_sentence_splitting: bool = True
    max_sentence_length: int = 160
    enable_sentence_appending: bool = True
    remove_diacritics: bool = False
    remove_quotation_marks: bool = False
    disable_paragraph_detection: bool = False
    remove_footnotes: bool = False
    filter_citations: bool = True


@dataclass
class TTSSettings:
    service: str = "XTTS"
    use_external_server: bool = False
    external_server_url: str = "http://127.0.0.1:8020"
    openai_audio_endpoint: str = "openai"
    provider_configs: List[Dict[str, Any]] = field(default_factory=default_tts_provider_configs)
    openai_audio_endpoints_json: str = ""
    openai_audio_instructions: str = ""
    xtts_model: str = ""
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
    request_timeout_seconds: int = 180
    # reasoning_effort: "" = don't send (model default), "low"/"medium"/"high" = explicit level.
    # LiteLLM translates this to native provider format and drops it if unsupported.
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
    whisper_language: str = "English"
    whisper_model: str = "large-v3"
    correction_enabled: bool = False
    custom_correction_prompt: str = ""
    translation_enabled: bool = False
    original_language: str = "English"
    target_language: str = "en"
    chain_of_thought_enabled: bool = False
    glossary_enabled: bool = False
    translation_provider: str = "anthropic"
    translation_model: str = "claude-sonnet-4-6"
    custom_translation_model: str = ""
    custom_api_base: str = ""
    video_file_path: str = ""

@dataclass
class SourceCleaningSettings:
    max_iterations: int = 53

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
