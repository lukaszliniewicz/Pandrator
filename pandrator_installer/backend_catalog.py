"""Presentation metadata for speech engines offered by the installer.

Keep this catalogue independent from Qt so language and capability claims can be
covered by tests and updated without rewriting the installer layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True)
class ModelLicence:
    name: str
    licence: str
    licence_url: str
    usage: str

    def formatted(self) -> str:
        return (
            f"<b>{escape(self.name)}</b> — "
            f'<a style="color:#C2A5F1; text-decoration:none" '
            f'href="{escape(self.licence_url, quote=True)}">{escape(self.licence)}</a>'
            f" · {escape(self.usage)}"
        )


@dataclass(frozen=True)
class BackendPresentation:
    summary: str
    languages: tuple[str, ...]
    voice_cloning: bool
    prebuilt_voices: bool
    note: str
    source_url: str
    models: tuple[ModelLicence, ...] = ()

    @property
    def formatted_languages(self) -> str:
        return ", ".join(self.languages) + "."

    @property
    def formatted_model_licences(self) -> str:
        return "<br><br>".join(model.formatted() for model in self.models)


APACHE_COMMERCIAL = "Commercial use permitted under the stated terms."
MIT_COMMERCIAL = "Commercial use permitted under the stated terms."
NONCOMMERCIAL = "Non-commercial use only under the stated terms."


TTS_BACKENDS: dict[str, BackendPresentation] = {
    "kokoro": BackendPresentation(
        summary="Fast, lightweight multilingual speech with a broad built-in voice catalogue.",
        languages=(
            "English (US)",
            "English (UK)",
            "Spanish",
            "French",
            "Hindi",
            "Italian",
            "Japanese",
            "Brazilian Portuguese",
            "Mandarin Chinese",
        ),
        voice_cloning=False,
        prebuilt_voices=True,
        note="CPU-friendly, with CUDA, experimental ROCm, and Apple Silicon paths supported by the upstream service.",
        source_url="https://github.com/remsky/Kokoro-FastAPI",
        models=(
            ModelLicence(
                "Kokoro-82M v1.0",
                "Apache-2.0",
                "https://huggingface.co/hexgrad/Kokoro-82M",
                APACHE_COMMERCIAL,
            ),
        ),
    ),
    "kobold_qwen": BackendPresentation(
        summary="Flexible local speech through KoboldCpp, with small and high-capacity model choices.",
        languages=(
            "Chinese",
            "English",
            "Japanese",
            "Korean",
            "German",
            "French",
            "Russian",
            "Portuguese",
            "Spanish",
            "Italian",
        ),
        voice_cloning=True,
        prebuilt_voices=True,
        note="Base models clone reference audio. The CustomVoice model supplies named built-in speakers; model size, quantization, and compute backend are configurable.",
        source_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        models=(
            ModelLicence(
                "Qwen3-TTS 12Hz 0.6B Base (FP16 or Q8_0)",
                "Apache-2.0",
                "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                APACHE_COMMERCIAL,
            ),
            ModelLicence(
                "Qwen3-TTS 12Hz 1.7B Base (FP16 or Q8_0)",
                "Apache-2.0",
                "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                APACHE_COMMERCIAL,
            ),
            ModelLicence(
                "Qwen3-TTS 12Hz 1.7B CustomVoice (FP16 or Q8_0)",
                "Apache-2.0",
                "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                APACHE_COMMERCIAL,
            ),
        ),
    ),
    "xtts": BackendPresentation(
        summary="Mature multilingual speech generation from short reference recordings.",
        languages=(
            "English",
            "Spanish",
            "French",
            "German",
            "Italian",
            "Portuguese",
            "Polish",
            "Turkish",
            "Russian",
            "Dutch",
            "Czech",
            "Arabic",
            "Chinese",
            "Japanese",
            "Hungarian",
            "Korean",
        ),
        voice_cloning=True,
        prebuilt_voices=False,
        note="Cross-language cloning is supported. GPU generation is considerably faster; CPU mode is available for compatibility.",
        source_url="https://github.com/coqui-ai/TTS/blob/dev/docs/source/models/xtts.md",
        models=(
            ModelLicence(
                "XTTS v2",
                "Coqui Public Model License 1.0.0",
                "https://huggingface.co/coqui/XTTS-v2/blob/main/LICENSE.txt",
                "The model and its outputs are licensed for non-commercial use only.",
            ),
        ),
    ),
    "voxcpm": BackendPresentation(
        summary="High-fidelity multilingual speech conditioned by an uploaded reference voice.",
        languages=(
            "Arabic",
            "Burmese",
            "Chinese",
            "Danish",
            "Dutch",
            "English",
            "Finnish",
            "French",
            "German",
            "Greek",
            "Hebrew",
            "Hindi",
            "Indonesian",
            "Italian",
            "Japanese",
            "Khmer",
            "Korean",
            "Lao",
            "Malay",
            "Norwegian",
            "Polish",
            "Portuguese",
            "Russian",
            "Spanish",
            "Swahili",
            "Swedish",
            "Tagalog",
            "Thai",
            "Turkish",
            "Vietnamese",
        ),
        voice_cloning=True,
        prebuilt_voices=False,
        note="VoxCPM2 also supports several Chinese dialects. It is a comparatively large model intended for capable CUDA hardware.",
        source_url="https://huggingface.co/openbmb/VoxCPM2",
        models=(
            ModelLicence(
                "VoxCPM2 (BF16)",
                "Apache-2.0",
                "https://huggingface.co/openbmb/VoxCPM2",
                APACHE_COMMERCIAL,
            ),
        ),
    ),
    "fishs2": BackendPresentation(
        summary="Expressive, broad-language synthesis and rapid cloning through the native S2 runtime.",
        languages=(
            "Japanese",
            "English",
            "Chinese",
            "Korean",
            "Spanish",
            "Portuguese",
            "Arabic",
            "Russian",
            "French",
            "German",
            "Swedish",
            "Italian",
            "Turkish",
            "Norwegian",
            "Dutch",
            "Welsh",
            "Basque",
            "Catalan",
            "Danish",
            "Galician",
            "Tamil",
            "Hungarian",
            "Finnish",
            "Polish",
            "Estonian",
            "Hindi",
            "Latin",
            "Urdu",
            "Thai",
            "Vietnamese",
            "Javanese",
            "Bengali",
            "Yoruba",
            "South Slavey",
            "Czech",
            "Swahili",
            "Norwegian Nynorsk",
            "Hebrew",
            "Malay",
            "Ukrainian",
            "Indonesian",
            "Kazakh",
            "Bulgarian",
            "Latvian",
            "Burmese",
            "Tagalog",
            "Slovak",
            "Nepali",
            "Persian",
            "Afrikaans",
            "Greek",
            "Tibetan",
            "Croatian",
            "Romanian",
            "Shona",
            "Maori",
            "Yiddish",
            "Amharic",
            "Belarusian",
            "Khmer",
            "Icelandic",
            "Azerbaijani",
            "Sindhi",
            "Breton",
            "Albanian",
            "Pashto",
            "Mongolian",
            "Haitian Creole",
            "Malayalam",
            "Serbian",
            "Sanskrit",
            "Telugu",
            "Georgian",
            "Bosnian",
            "Punjabi",
            "Lithuanian",
            "Kannada",
            "Sinhala",
            "Armenian",
            "Marathi",
            "Assamese",
            "Gujarati",
            "Faroese",
        ),
        voice_cloning=True,
        prebuilt_voices=False,
        note="Japanese, English, and Chinese are the highest-support tier. Quantization and compute backend are selectable during setup.",
        source_url="https://github.com/fishaudio/fish-speech",
        models=(
            ModelLicence(
                "Fish Audio S2 Pro GGUF (F16, Q8_0, Q6_K, Q5_K_M, Q4_K_M, or Q2_K)",
                "Fish Audio Research License",
                "https://huggingface.co/rodrigomt/s2-pro-gguf/blob/main/LICENSE.md",
                "Research and non-commercial use; commercial use requires a separate Fish Audio licence.",
            ),
        ),
    ),
    "voxtral": BackendPresentation(
        summary="GPU speech generation with a curated catalogue of preset voices.",
        languages=(
            "Arabic",
            "English",
            "German",
            "Spanish",
            "French",
            "Hindi",
            "Italian",
            "Dutch",
            "Portuguese",
        ),
        voice_cloning=False,
        prebuilt_voices=True,
        note="The packaged service uses preset speakers and a WGPU-compatible accelerator on Windows and Linux; it does not provide a CPU path.",
        source_url="https://github.com/lukaszliniewicz/voxtral-fastapi",
        models=(
            ModelLicence(
                "Voxtral 4B TTS 2603 (BF16)",
                "CC BY-NC 4.0",
                "https://huggingface.co/mistralai/Voxtral-4B-TTS-2603",
                NONCOMMERCIAL,
            ),
        ),
    ),
    "silero": BackendPresentation(
        summary="Fast, CPU-friendly speech with modern East European and regional voices.",
        languages=(
            "Armenian",
            "Azerbaijani",
            "Bashkir",
            "Belarusian",
            "Chuvash",
            "English",
            "Erzya",
            "Spanish",
            "French",
            "Georgian",
            "German",
            "Indic languages",
            "Kabardian-Cherkess",
            "Kalmyk",
            "Kazakh",
            "Khakas",
            "Kyrgyz",
            "Moksha",
            "Russian",
            "Tajik",
            "Tatar",
            "Udmurt",
            "Ukrainian",
            "Uzbek",
            "Yakut",
        ),
        voice_cloning=False,
        prebuilt_voices=True,
        note=(
            "Pandrator's first-party service uses verified official model files and automatic "
            "stress handling. Every supported official pack is available, including the "
            "non-commercial variants. CPU on Windows and Linux."
        ),
        source_url="https://github.com/lukaszliniewicz/silero-fastapi",
        models=(
            ModelLicence(
                "v5_cis_base",
                "MIT",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE_CIS",
                MIT_COMMERCIAL,
            ),
            ModelLicence(
                "v5_cis_base_nostress",
                "MIT",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE_CIS",
                MIT_COMMERCIAL,
            ),
            ModelLicence(
                "v5_cis_ext",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v5_5_ru",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v3_en",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v3_en_indic",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v3_de",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v3_es",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v3_fr",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
            ModelLicence(
                "v3_indic",
                "CC BY-NC-SA 4.0",
                "https://github.com/snakers4/silero-models/blob/master/LICENSE",
                NONCOMMERCIAL,
            ),
        ),
    ),
    "chatterbox": BackendPresentation(
        summary="Expressive, cross-language speech generated from a reference recording.",
        languages=(
            "Arabic",
            "Danish",
            "German",
            "Greek",
            "English",
            "Spanish",
            "Finnish",
            "French",
            "Hebrew",
            "Hindi",
            "Italian",
            "Japanese",
            "Korean",
            "Malay",
            "Dutch",
            "Norwegian",
            "Polish",
            "Portuguese",
            "Russian",
            "Swedish",
            "Swahili",
            "Turkish",
            "Chinese",
        ),
        voice_cloning=True,
        prebuilt_voices=False,
        note="The multilingual model supports 23 languages. CUDA is recommended for interactive generation; CPU mode is available but slower.",
        source_url="https://github.com/resemble-ai/chatterbox",
        models=(
            ModelLicence(
                "Chatterbox Turbo (English, 350M)",
                "MIT",
                "https://huggingface.co/ResembleAI/chatterbox-turbo",
                MIT_COMMERCIAL,
            ),
            ModelLicence(
                "Chatterbox English (500M)",
                "MIT",
                "https://huggingface.co/ResembleAI/chatterbox",
                MIT_COMMERCIAL,
            ),
            ModelLicence(
                "Chatterbox Multilingual (500M)",
                "MIT",
                "https://huggingface.co/ResembleAI/chatterbox",
                MIT_COMMERCIAL,
            ),
        ),
    ),
    "magpie": BackendPresentation(
        summary="A multilingual local service with five expressive preset speakers.",
        languages=(
            "English",
            "Spanish",
            "German",
            "French",
            "Vietnamese",
            "Italian",
            "Mandarin Chinese",
            "Hindi",
            "Japanese",
        ),
        voice_cloning=False,
        prebuilt_voices=True,
        note="All five speakers can speak every supported language. CPU and NVIDIA CUDA runtimes are available on Windows and Linux; the first NeMo installation is comparatively large.",
        source_url="https://huggingface.co/nvidia/magpie_tts_multilingual_357m",
        models=(
            ModelLicence(
                "Magpie TTS Multilingual 357M (v2602)",
                "NVIDIA Open Model License",
                "https://huggingface.co/nvidia/magpie_tts_multilingual_357m",
                "The model card marks this checkpoint ready for commercial use under NVIDIA's terms.",
            ),
        ),
    ),
}


TTS_BACKEND_ORDER = (
    "kokoro",
    "kobold_qwen",
    "xtts",
    "voxcpm",
    "fishs2",
    "voxtral",
    "silero",
    "chatterbox",
    "magpie",
)


WHISPER_LARGE_V3_LANGUAGES = (
    "English", "Chinese", "German", "Spanish", "Russian", "Korean", "French",
    "Japanese", "Portuguese", "Turkish", "Polish", "Catalan", "Dutch", "Arabic",
    "Swedish", "Italian", "Indonesian", "Hindi", "Finnish", "Vietnamese", "Hebrew",
    "Ukrainian", "Greek", "Malay", "Czech", "Romanian", "Danish", "Hungarian",
    "Tamil", "Norwegian", "Thai", "Urdu", "Croatian", "Bulgarian", "Lithuanian",
    "Latin", "Maori", "Malayalam", "Welsh", "Slovak", "Telugu", "Persian",
    "Latvian", "Bengali", "Serbian", "Azerbaijani", "Slovenian", "Kannada",
    "Estonian", "Macedonian", "Breton", "Basque", "Icelandic", "Armenian", "Nepali",
    "Mongolian", "Bosnian", "Kazakh", "Albanian", "Swahili", "Galician", "Marathi",
    "Punjabi", "Sinhala", "Khmer", "Shona", "Yoruba", "Somali", "Afrikaans",
    "Occitan", "Georgian", "Belarusian", "Tajik", "Sindhi", "Gujarati", "Amharic",
    "Yiddish", "Lao", "Uzbek", "Faroese", "Haitian Creole", "Pashto", "Turkmen",
    "Nynorsk", "Maltese", "Sanskrit", "Luxembourgish", "Myanmar", "Tibetan",
    "Tagalog", "Malagasy", "Assamese", "Tatar", "Hawaiian", "Lingala", "Hausa",
    "Bashkir", "Javanese", "Sundanese", "Cantonese",
)

PARAKEET_06B_V3_LANGUAGES = (
    "Bulgarian", "Croatian", "Czech", "Danish", "Dutch", "English", "Estonian",
    "Finnish", "French", "German", "Greek", "Hungarian", "Italian", "Latvian",
    "Lithuanian", "Maltese", "Polish", "Portuguese", "Romanian", "Slovak",
    "Slovenian", "Spanish", "Swedish", "Russian", "Ukrainian",
)

CRISPASR_MODELS = (
    ModelLicence(
        "Whisper large-v3 (FP16 or Q5_0)",
        "Apache-2.0",
        "https://huggingface.co/openai/whisper-large-v3",
        APACHE_COMMERCIAL,
    ),
    ModelLicence(
        "Parakeet TDT 0.6B v3 (FP16, Q8_0, Q5_0, or Q4_K)",
        "CC BY 4.0",
        "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
        "Commercial use permitted with the licence's attribution requirements.",
    ),
)


def formatted_crispasr_languages() -> str:
    whisper = ", ".join(WHISPER_LARGE_V3_LANGUAGES)
    parakeet = ", ".join(PARAKEET_06B_V3_LANGUAGES)
    return (
        f"Whisper large-v3 (100): {whisper}.\n\n"
        f"Parakeet TDT 0.6B v3 (25): {parakeet}."
    )


def formatted_crispasr_model_licences() -> str:
    return "<br><br>".join(model.formatted() for model in CRISPASR_MODELS)
