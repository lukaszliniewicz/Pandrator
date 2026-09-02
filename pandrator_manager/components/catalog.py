"""Manager-owned presentation catalogue for setup and component management.

This module intentionally contains no Qt imports and no installation behavior.
The same typed metadata can be projected by the recovery UI, the Pandrator
WebUI, the CLI, and future native clients.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import (
    ComponentCapability,
    ComponentInstallOption,
    ComponentModel,
    ComponentOptionChoice,
    ComponentSection,
    SizeProvenance,
)

MiB = 1024**2
GiB = 1024**3


def capability(
    capability_id: str,
    label: str,
    *,
    available: bool = True,
    description: str = "",
) -> ComponentCapability:
    return ComponentCapability(
        id=capability_id,
        label=label,
        available=available,
        description=description,
    )


def model(
    model_id: str,
    label: str,
    *,
    description: str = "",
    license_name: str | None = None,
    license_url: str | None = None,
    usage_note: str = "",
    capabilities: tuple[str, ...] = (),
    estimated_download_bytes: int | None = None,
) -> ComponentModel:
    return ComponentModel(
        id=model_id,
        label=label,
        description=description,
        license_name=license_name,
        license_url=license_url,
        usage_note=usage_note,
        capabilities=capabilities,
        estimated_download_bytes=estimated_download_bytes,
        size_provenance=SizeProvenance.ESTIMATE,
    )


def choice(
    value: str,
    label: str,
    description: str = "",
    *,
    requires: dict[str, tuple[str, ...]] | None = None,
) -> ComponentOptionChoice:
    return ComponentOptionChoice(
        value=value,
        label=label,
        description=description,
        requires=requires or {},
    )


@dataclass(frozen=True, slots=True)
class ComponentPresentation:
    section: ComponentSection
    order: int
    summary: str
    guidance: str
    languages: tuple[str, ...] = ()
    capabilities: tuple[ComponentCapability, ...] = ()
    models: tuple[ComponentModel, ...] = ()
    install_options: tuple[ComponentInstallOption, ...] = ()
    estimated_download_bytes: int | None = None
    estimated_installed_bytes: int | None = None
    size_provenance: SizeProvenance = SizeProvenance.ESTIMATE
    size_note: str = (
        "Approximate requirement; the exact transfer depends on platform, "
        "compute variant, caches, and selected models."
    )


COMMERCIAL_APACHE = "Commercial use is permitted under the Apache-2.0 terms."
COMMERCIAL_MIT = "Commercial use is permitted under the MIT terms."


PRESENTATIONS: dict[str, ComponentPresentation] = {
    "pandrator": ComponentPresentation(
        section=ComponentSection.CORE,
        order=0,
        summary="The Pandrator browser application and background worker.",
        guidance=(
            "Install this first. It provides the workspace used to create "
            "audiobooks, voiceovers, subtitles, and dubbing projects. Speech "
            "engines are optional and can be added now or later."
        ),
        capabilities=(
            capability("web_workspace", "Browser workspace"),
            capability("durable_jobs", "Background jobs"),
            capability("recovery", "Manager-backed recovery"),
        ),
        estimated_download_bytes=650 * MiB,
        estimated_installed_bytes=2 * GiB,
        size_note=(
            "Estimate includes Pandrator's private Python runtime. Project "
            "files and generated media are stored separately."
        ),
    ),
    "audio_cpp": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=5,
        summary=(
            "A pinned native audio.cpp runtime with selectable local GGUF "
            "models for cloning and pre-built voices."
        ),
        guidance=(
            "The manager installs audio.cpp v0.7.1 and the selected models into "
            "one versioned service slot. Choose at least one model package. CPU, "
            "Vulkan, and CUDA are available on Windows and Linux x86_64. The "
            "best-effort Linux CUDA build has not yet been tested on NVIDIA "
            "hardware. FireRedTTS3 Base is experimental. Breeze is not included "
            "until a stable package is available."
        ),
        languages=(
            "Qwen3: Chinese, English, French, German, Italian, Japanese, Korean, "
            "Portuguese, Russian, Spanish",
            "Fish Audio: English, Chinese, Japanese",
            "VoxCPM2: 30+ languages",
            "Magpie: 9 languages",
            "OmniVoice: 600+ languages",
            "PocketTTS: English, German, Italian, Portuguese, Spanish",
            "FireRedTTS3: 24 languages plus Chinese dialects",
        ),
        capabilities=(
            capability("voice_cloning", "Voice cloning"),
            capability("prebuilt_voices", "Pre-built voices"),
            capability("multilingual", "Multilingual"),
            capability("native_runtime", "Native runtime"),
        ),
        models=(
            model(
                "qwen3_tts_1_7b_base_q8_0",
                "Qwen3 TTS 1.7B Base Q8_0",
                description="Reference-audio cloning with the default Q8_0 GGUF package.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("voice_cloning",),
                estimated_download_bytes=2_695_175_104,
            ),
            model(
                "qwen3_tts_1_7b_customvoice_q8_0",
                "Qwen3 TTS 1.7B CustomVoice Q8_0",
                description="Named built-in speakers without a reference recording.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("prebuilt_voices",),
                estimated_download_bytes=2_817_044_064,
            ),
            model(
                "fish_audio_s2_pro_q8_0",
                "Fish Audio S2 Pro Q8_0",
                description="Expressive multilingual synthesis and reference-audio cloning.",
                license_name="Fish Audio Research License",
                license_url="https://huggingface.co/rodrigomt/s2-pro-gguf/blob/main/LICENSE.md",
                usage_note=(
                    "Research and non-commercial use; commercial use requires a "
                    "separate Fish Audio licence."
                ),
                capabilities=("voice_cloning", "multilingual"),
                estimated_download_bytes=6_317_911_232,
            ),
            model(
                "voxcpm2_q8_0",
                "VoxCPM2 Q8_0",
                description="High-fidelity multilingual speech with a reference voice.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/openbmb/VoxCPM2",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("voice_cloning", "multilingual"),
                estimated_download_bytes=2_955_000_480,
            ),
            model(
                "magpie_tts_q8_0",
                "MagpieTTS Multilingual 357M Q8_0",
                description="Five pre-built speakers across nine supported languages.",
                license_name="NVIDIA Open Model License",
                license_url="https://huggingface.co/nvidia/magpie_tts_multilingual_357m",
                usage_note=(
                    "Review NVIDIA's model terms and the model card before "
                    "commercial distribution."
                ),
                capabilities=("prebuilt_voices", "multilingual"),
                estimated_download_bytes=1_562_142_912,
            ),
            model(
                "chatterbox_q8_0",
                "Chatterbox Q8_0",
                description="Expressive multilingual synthesis from a reference recording.",
                license_name="MIT",
                license_url="https://huggingface.co/ResembleAI/chatterbox",
                usage_note=COMMERCIAL_MIT,
                capabilities=("voice_cloning", "multilingual"),
                estimated_download_bytes=2_088_393_668,
            ),
            model(
                "omnivoice_q8_0",
                "OmniVoice Q8_0",
                description="Massively multilingual cloning and voice design.",
                license_name="See model card",
                license_url="https://huggingface.co/k2-fsa/OmniVoice",
                usage_note="Review the authoritative model card before use or redistribution.",
                capabilities=("voice_cloning", "multilingual"),
                estimated_download_bytes=1_350_288_416,
            ),
            model(
                "pocket_tts_english_q8_0",
                "PocketTTS English Q8_0",
                description="Small CPU-friendly English TTS and voice-cloning package.",
                license_name="See model card",
                license_url="https://huggingface.co/kyutai/pocket-tts",
                usage_note="Review the model card's licence and gated-use conditions.",
                capabilities=("voice_cloning", "prebuilt_voices"),
                estimated_download_bytes=134_051_128,
            ),
            model(
                "fireredtts3_base_q8_0",
                "FireRedTTS3 Base Q8_0 (experimental)",
                description="Experimental multilingual reference-audio cloning package.",
                license_name="See model card",
                license_url="https://huggingface.co/FireRedTeam/FireRedTTS3",
                usage_note="Experimental selection; review the authoritative model card before use.",
                capabilities=("voice_cloning", "multilingual"),
                estimated_download_bytes=4_180_334_848,
            ),
        ),
        estimated_download_bytes=24 * GiB,
        estimated_installed_bytes=30 * GiB,
        size_note=(
            "Estimate includes the pinned native runtime and all nine selectable "
            "Q8_0 model packages. Per-model sizes were sampled from the upstream "
            "package manager on 2026-09-02. Model files are fetched from an immutable "
            "repository revision and verified with Pandrator-pinned SHA-256 digests."
        ),
    ),
    "kokoro": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=10,
        summary=(
            "Fast, lightweight local speech with a generous built-in voice "
            "catalogue."
        ),
        guidance=(
            "A good first local engine when speed, CPU support, and ready-made "
            "voices matter more than cloning a particular speaker."
        ),
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
        capabilities=(
            capability("voice_cloning", "Voice cloning", available=False),
            capability("prebuilt_voices", "Pre-built voices"),
            capability("cpu_friendly", "CPU-friendly"),
        ),
        models=(
            model(
                "kokoro-82m-v1",
                "Kokoro-82M v1.0",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/hexgrad/Kokoro-82M",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("prebuilt_voices",),
                estimated_download_bytes=350 * MiB,
            ),
        ),
        estimated_download_bytes=1 * GiB,
        estimated_installed_bytes=3 * GiB,
        size_note=(
            "Upstream Kokoro does not publish manager installation-size "
            "metadata. These values are deliberately labelled estimates and "
            "include its Python and model dependencies."
        ),
    ),
    "qwen_tts": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=20,
        summary=(
            "Flexible local speech with both reference-audio cloning and "
            "named built-in speakers."
        ),
        guidance=(
            "Choose which family is prepared when the service starts. The "
            "other family downloads automatically when it is first selected "
            "in Pandrator. The 1.7B model is recommended; 0.6B Base remains "
            "available for constrained systems."
        ),
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
        capabilities=(
            capability("voice_cloning", "Voice cloning"),
            capability("prebuilt_voices", "Pre-built voices"),
            capability("multilingual", "Multilingual"),
        ),
        models=(
            model(
                "qwen3-tts-0.6b-base",
                "Qwen3-TTS 0.6B Base",
                description="Small Base model for reference-audio cloning.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("voice_cloning",),
                estimated_download_bytes=2 * GiB,
            ),
            model(
                "qwen3-tts-1.7b-base",
                "Qwen3-TTS 1.7B Base",
                description="Higher-capacity Base model for voice cloning.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("voice_cloning",),
                estimated_download_bytes=4 * GiB,
            ),
            model(
                "qwen3-tts-1.7b-customvoice",
                "Qwen3-TTS 1.7B CustomVoice",
                description="Named built-in speakers; no reference audio required.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("prebuilt_voices",),
                estimated_download_bytes=4 * GiB,
            ),
        ),
        install_options=(
            ComponentInstallOption(
                key="initial_model",
                label="Voice mode",
                description="Which Qwen model family should be prepared first.",
                default="base",
                choices=(
                    choice("base", "Base — clone a reference voice"),
                    choice("customvoice", "CustomVoice — named speakers"),
                ),
            ),
            ComponentInstallOption(
                key="model_size",
                label="Model size",
                description="Larger models need more memory and disk space.",
                default="1.7b",
                choices=(
                    choice(
                        "0.6b",
                        "0.6B — smaller",
                        requires={"initial_model": ("base",)},
                    ),
                    choice("1.7b", "1.7B — higher capacity"),
                ),
            ),
            ComponentInstallOption(
                key="quantization",
                label="Model precision",
                description="Q8_0 is smaller; FP16 preserves full precision.",
                state_field="quantization",
                default="q8_0",
                choices=(
                    choice("q8_0", "Q8_0 — smaller download"),
                    choice("f16", "FP16 — full precision"),
                ),
            ),
        ),
        estimated_download_bytes=4 * GiB,
        estimated_installed_bytes=8 * GiB,
    ),
    "xtts": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=30,
        summary=(
            "Mature multilingual speech generation from short reference "
            "recordings."
        ),
        guidance=(
            "Choose XTTS when cross-language voice cloning is the priority. "
            "CUDA is considerably faster; CPU mode remains available."
        ),
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
        capabilities=(
            capability("voice_cloning", "Voice cloning"),
            capability("prebuilt_voices", "Pre-built voices", available=False),
            capability("cross_language", "Cross-language cloning"),
        ),
        models=(
            model(
                "xtts-v2",
                "XTTS v2",
                license_name="Coqui Public Model License 1.0.0",
                license_url="https://huggingface.co/coqui/XTTS-v2/blob/main/LICENSE.txt",
                usage_note="The model and its outputs are licensed for non-commercial use only.",
                capabilities=("voice_cloning", "cross_language"),
                estimated_download_bytes=2 * GiB,
            ),
        ),
        estimated_download_bytes=3 * GiB,
        estimated_installed_bytes=7 * GiB,
    ),
    "voxcpm": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=40,
        summary=(
            "High-fidelity multilingual speech conditioned by an uploaded "
            "reference voice."
        ),
        guidance=(
            "A comparatively large model. CUDA is strongly recommended for "
            "normal use; CPU mode is available for compatibility and testing "
            "but generation will be much slower. Its model is downloaded into "
            "the shared Pandrator data directory the first time the service starts."
        ),
        languages=(
            "Arabic",
            "Chinese",
            "English",
            "French",
            "German",
            "Hindi",
            "Italian",
            "Japanese",
            "Korean",
            "Polish",
            "Portuguese",
            "Russian",
            "Spanish",
            "Turkish",
            "Vietnamese",
        ),
        capabilities=(
            capability("voice_cloning", "Voice cloning"),
            capability("prebuilt_voices", "Pre-built voices", available=False),
            capability("cuda_recommended", "CUDA recommended"),
        ),
        models=(
            model(
                "voxcpm2",
                "VoxCPM2 (BF16)",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/openbmb/VoxCPM2",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("voice_cloning",),
                estimated_download_bytes=9 * GiB,
            ),
        ),
        estimated_download_bytes=10 * GiB,
        estimated_installed_bytes=18 * GiB,
    ),
    "fish_speech": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=50,
        summary=(
            "Expressive, broad-language synthesis and rapid cloning through "
            "the native Fish S2 runtime."
        ),
        guidance=(
            "Japanese, English, and Chinese have the strongest support. "
            "Quantization trades model size against quality."
        ),
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
            "Polish",
            "Italian",
            "Turkish",
            "Hindi",
            "Ukrainian",
        ),
        capabilities=(
            capability("voice_cloning", "Voice cloning"),
            capability("prebuilt_voices", "Pre-built voices", available=False),
            capability("multilingual", "Broad language coverage"),
        ),
        models=(
            model(
                "fish-s2-pro-gguf",
                "Fish Audio S2 Pro GGUF",
                license_name="Fish Audio Research License",
                license_url="https://huggingface.co/rodrigomt/s2-pro-gguf/blob/main/LICENSE.md",
                usage_note=(
                    "Research and non-commercial use; commercial use requires "
                    "a separate Fish Audio licence."
                ),
                capabilities=("voice_cloning",),
                estimated_download_bytes=4 * GiB,
            ),
        ),
        install_options=(
            ComponentInstallOption(
                key="quantization",
                label="Model quantization",
                state_field="quantization",
                description="Smaller quantizations use less memory and disk.",
                default="q6_k",
                choices=tuple(
                    choice(value, label)
                    for value, label in (
                        ("f16", "F16 — largest"),
                        ("q8_0", "Q8_0"),
                        ("q6_k", "Q6_K — recommended"),
                        ("q5_k_m", "Q5_K_M"),
                        ("q4_k_m", "Q4_K_M"),
                        ("q3_k", "Q3_K"),
                        ("q2_k", "Q2_K — smallest"),
                    )
                ),
            ),
        ),
        estimated_download_bytes=5 * GiB,
        estimated_installed_bytes=9 * GiB,
    ),
    "voxtral": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=60,
        summary="GPU speech generation with a curated catalogue of preset voices.",
        guidance=(
            "Uses preset speakers and a WGPU-compatible accelerator on Windows "
            "and Linux. There is no supported CPU-only path."
        ),
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
        capabilities=(
            capability("voice_cloning", "Voice cloning", available=False),
            capability("prebuilt_voices", "Pre-built voices"),
            capability("gpu_required", "GPU required"),
        ),
        models=(
            model(
                "voxtral-4b-tts-2603",
                "Voxtral 4B TTS 2603 (BF16)",
                license_name="CC BY-NC 4.0",
                license_url="https://huggingface.co/mistralai/Voxtral-4B-TTS-2603",
                usage_note="Non-commercial use only under the stated terms.",
                capabilities=("prebuilt_voices",),
                estimated_download_bytes=9 * GiB,
            ),
        ),
        estimated_download_bytes=10 * GiB,
        estimated_installed_bytes=16 * GiB,
    ),
    "silero": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=70,
        summary=(
            "Fast, CPU-friendly speech with modern East European and regional "
            "voices."
        ),
        guidance=(
            "A strong lightweight choice for CPU systems. Pandrator exposes "
            "verified official voice packs; some packs have non-commercial terms."
        ),
        languages=(
            "Armenian",
            "Azerbaijani",
            "Belarusian",
            "English",
            "Spanish",
            "French",
            "Georgian",
            "German",
            "Kazakh",
            "Russian",
            "Ukrainian",
            "Uzbek",
        ),
        capabilities=(
            capability("voice_cloning", "Voice cloning", available=False),
            capability("prebuilt_voices", "Pre-built voices"),
            capability("cpu_friendly", "CPU-friendly"),
        ),
        models=(
            model(
                "silero-official-packs",
                "Official Silero voice packs",
                description="Commercial MIT and non-commercial CC variants are available.",
                license_name="MIT / CC BY-NC-SA 4.0",
                license_url="https://github.com/snakers4/silero-models",
                usage_note="Usage depends on the selected official voice pack.",
                capabilities=("prebuilt_voices",),
                estimated_download_bytes=250 * MiB,
            ),
        ),
        estimated_download_bytes=500 * MiB,
        estimated_installed_bytes=1 * GiB,
    ),
    "chatterbox": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=80,
        summary=(
            "Expressive, cross-language speech generated from a reference "
            "recording."
        ),
        guidance=(
            "CUDA is recommended for interactive generation. CPU works but is "
            "substantially slower."
        ),
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
        capabilities=(
            capability("voice_cloning", "Voice cloning"),
            capability("prebuilt_voices", "Pre-built voices", available=False),
            capability("expressive", "Expressive speech"),
        ),
        models=(
            model(
                "chatterbox-turbo",
                "Chatterbox Turbo (English, 350M)",
                license_name="MIT",
                license_url="https://huggingface.co/ResembleAI/chatterbox-turbo",
                usage_note=COMMERCIAL_MIT,
                capabilities=("voice_cloning",),
                estimated_download_bytes=2 * GiB,
            ),
            model(
                "chatterbox-multilingual",
                "Chatterbox Multilingual (500M)",
                license_name="MIT",
                license_url="https://huggingface.co/ResembleAI/chatterbox",
                usage_note=COMMERCIAL_MIT,
                capabilities=("voice_cloning", "multilingual"),
                estimated_download_bytes=3 * GiB,
            ),
            model(
                "chatterbox-en",
                "Chatterbox English",
                description="The original English-only Chatterbox model.",
                license_name="MIT",
                license_url="https://huggingface.co/ResembleAI/chatterbox",
                usage_note=COMMERCIAL_MIT,
                capabilities=("voice_cloning",),
                estimated_download_bytes=3 * GiB,
            ),
        ),
        estimated_download_bytes=4 * GiB,
        estimated_installed_bytes=8 * GiB,
    ),
    "magpie": ComponentPresentation(
        section=ComponentSection.TEXT_TO_SPEECH,
        order=90,
        summary="A multilingual local service with five expressive preset speakers.",
        guidance=(
            "All five speakers can speak every supported language. The first "
            "NeMo installation is comparatively large."
        ),
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
        capabilities=(
            capability("voice_cloning", "Voice cloning", available=False),
            capability("prebuilt_voices", "Five pre-built voices"),
            capability("multilingual", "Multilingual"),
        ),
        models=(
            model(
                "magpie-tts-multilingual-357m",
                "Magpie TTS Multilingual 357M",
                license_name="NVIDIA Open Model License",
                license_url="https://huggingface.co/nvidia/magpie_tts_multilingual_357m",
                usage_note=(
                    "The model card marks this checkpoint ready for commercial "
                    "use under NVIDIA's terms."
                ),
                capabilities=("prebuilt_voices", "multilingual"),
                estimated_download_bytes=2 * GiB,
            ),
        ),
        estimated_download_bytes=5 * GiB,
        estimated_installed_bytes=10 * GiB,
    ),
    "crispasr": ComponentPresentation(
        section=ComponentSection.SPEECH_TO_TEXT,
        order=10,
        summary=(
            "One verified native transcription runtime with several selectable "
            "speech-recognition models."
        ),
        guidance=(
            "Whisper offers the broadest language coverage. Parakeet is fast "
            "and accurate for its supported languages. MOSS adds native speaker "
            "diarization. Models are downloaded on demand after the small native "
            "runtime is installed."
        ),
        languages=(
            "Whisper: 100 languages",
            "Parakeet: 25 European languages",
            "MOSS: multilingual auto-detection",
        ),
        capabilities=(
            capability("transcription", "Transcription"),
            capability("word_timestamps", "Word timestamps"),
            capability("speaker_diarization", "Speaker diarization", description="MOSS model"),
        ),
        models=(
            model(
                "whisper-large-v3",
                "Whisper large-v3",
                description="Broad multilingual recognition with DTW word timestamps.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/openai/whisper-large-v3",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("transcription", "word_timestamps"),
                estimated_download_bytes=3 * GiB,
            ),
            model(
                "parakeet-tdt-0.6b-v3",
                "Parakeet TDT 0.6B v3",
                description="Fast 25-language recognition with word timestamps.",
                license_name="CC BY 4.0",
                license_url="https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
                usage_note="Commercial use is permitted with attribution.",
                capabilities=("transcription", "word_timestamps"),
                estimated_download_bytes=1 * GiB,
            ),
            model(
                "moss-transcribe-diarize-0.9b",
                "MOSS Transcribe-Diarize 0.9B",
                description="Multilingual transcription with native speaker turns.",
                license_name="Apache-2.0",
                license_url="https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize",
                usage_note=COMMERCIAL_APACHE,
                capabilities=("transcription", "speaker_diarization"),
                estimated_download_bytes=2 * GiB,
            ),
        ),
        install_options=(
            ComponentInstallOption(
                key="engine",
                label="Default transcription model",
                description="The model is downloaded only when first used.",
                default="moss-transcribe-diarize-0.9b",
                choices=(
                    choice("whisper-large-v3", "Whisper large-v3 — broadest coverage"),
                    choice("parakeet-tdt-0.6b-v3", "Parakeet 0.6B — fast"),
                    choice("moss-transcribe-diarize-0.9b", "MOSS 0.9B — speaker diarization"),
                ),
            ),
            ComponentInstallOption(
                key="quantization",
                label="Model precision",
                description="Available choices depend on the selected model.",
                state_field="quantization",
                default="q8_0",
                choices=(
                    choice("f16", "FP16 — full precision"),
                    choice(
                        "q8_0",
                        "Q8_0",
                        requires={
                            "engine": (
                                "parakeet-tdt-0.6b-v3",
                                "moss-transcribe-diarize-0.9b",
                            )
                        },
                    ),
                    choice(
                        "q5_0",
                        "Q5_0",
                        requires={
                            "engine": (
                                "whisper-large-v3",
                                "parakeet-tdt-0.6b-v3",
                            )
                        },
                    ),
                    choice(
                        "q4_k",
                        "Q4_K",
                        requires={
                            "engine": (
                                "parakeet-tdt-0.6b-v3",
                                "moss-transcribe-diarize-0.9b",
                            )
                        },
                    ),
                ),
            ),
        ),
        estimated_download_bytes=80 * MiB,
        estimated_installed_bytes=250 * MiB,
        size_note=(
            "Estimate is for the native CrispASR runtime. The selected model is "
            "downloaded on first use; its separate estimate is shown above."
        ),
    ),
    "rvc": ComponentPresentation(
        section=ComponentSection.SPEECH_TO_SPEECH,
        order=10,
        summary="Convert generated or recorded speech into a trained target voice.",
        guidance=(
            "RVC is a post-processing engine. It uses compatible RVC voice "
            "models rather than creating speech directly from text."
        ),
        capabilities=(
            capability("voice_conversion", "Voice conversion"),
            capability("voice_cloning", "Voice cloning", available=False),
            capability("custom_models", "Custom RVC models"),
        ),
        estimated_download_bytes=3 * GiB,
        estimated_installed_bytes=7 * GiB,
    ),
    "xtts_finetuning": ComponentPresentation(
        section=ComponentSection.TRAINING,
        order=10,
        summary="Prepare and fine-tune custom XTTS voices from your own recordings.",
        guidance=(
            "This is an advanced training tool and depends on XTTS. A capable "
            "CUDA GPU is strongly recommended."
        ),
        capabilities=(
            capability("model_training", "Voice model training"),
            capability("cuda_recommended", "CUDA recommended"),
        ),
        estimated_download_bytes=4 * GiB,
        estimated_installed_bytes=9 * GiB,
    ),
}


def presentation_for(component_id: str) -> ComponentPresentation:
    try:
        return PRESENTATIONS[component_id]
    except KeyError as error:  # pragma: no cover - every built-in is validated
        raise KeyError(f"Missing presentation metadata for {component_id}.") from error
