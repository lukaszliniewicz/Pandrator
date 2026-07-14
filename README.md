<p align="center">
  <img src="pandrator.png" alt="Pandrator" width="180" />
</p>

# Pandrator

Pandrator is a local-first workspace for creating audiobooks, subtitles, and voiceovers. It combines document preparation, transcription, optional LLM correction and translation, speech generation, detailed review, and export in one browser-based interface.

The desktop installer and launcher manage Pandrator and its optional local model services. Docker and WSL are not required.

The application itself is web-only. The retired Qt application is preserved on the `qt-maintenance` branch; Qt remains in `main` only for the desktop installer and launcher.

## TL;DR

| If you want to… | Start with… |
|---|---|
| Create an audiobook with ready-made voices | **Kokoro** is the simplest lightweight starting point. Consult the [language table](#speech-generation-language-support) for alternatives. |
| Clone a voice from a reference recording | Install **Qwen3 TTS Base** or another cloning model supporting your language. |
| Create subtitles from audio or video | Install **CrispASR**. Use Whisper large-v3 for broad coverage or Parakeet TDT 0.6B v3 for its supported languages. |
| Correct or translate text and subtitles | Configure a local or cloud LLM provider. This is optional for basic speech generation. |
| Convert generated speech to another trained voice | Install **RVC**. It runs after speech generation and is optional. |

Download the launcher from [GitHub Releases](https://github.com/lukaszliniewicz/Pandrator/releases). You can begin with only the components you need and add others later.

Local models process content on your machine. Cloud LLM and speech providers are optional; when used, they may send content to an external service and incur charges.

## What Pandrator does

### Audiobooks

- Imports plain text, pasted text, PDF, EPUB, DOCX, and MOBI sources.
- Extracts structure and chapter markers, with OCR and a reviewable cleaning workflow for difficult PDF and EPUB files.
- Includes a browser PDF editor with page stacks, left/right stacks, cropping, whiteouts, and deletion.
- Applies deterministic text normalization and configurable segmentation before speech generation.
- Optionally uses an LLM to clean a complete document or optimize small batches while generating.
- Keeps generated speech as reviewable segments: edit, play as a playlist, mark, regenerate, compare takes, and select RVC variants.
- Exports WAV, MP3, Opus, FLAC, or M4B with chapters, metadata, and cover art.

### Subtitles and voiceovers

- Starts from SRT subtitles or common audio and video formats.
- Transcribes media through CrispASR with word timestamps, VAD, and optional diarization controls.
- Keeps transcription, correction, translation, subtitle composition, speech generation, synchronization, and export as separate, rerunnable steps.
- Supports professional translation directly from an original transcript or from a corrected revision.
- Provides side-by-side subtitle review with timing, text, split, and merge editing.
- Creates subtitle-only exports or dubbed media with original, mixed, or dubbing-only audio and soft, burned, translated, original-language, or bilingual subtitles.

### Providers and voices

- Connects to local TTS services, OpenAI, Google Gemini, and configurable custom speech endpoints.
- Connects to local OpenAI-compatible LLM servers such as LM Studio as well as supported cloud providers.
- Stores model-specific LLM temperature, reasoning, and cached/uncached token pricing defaults.
- Manages recorded and uploaded reference samples, transcripts, and persistent previews of pre-built voices in the voice library.
- Supports RVC model management and XTTS training as separate workflows.

## Installation

### Windows

Download `PandratorInstaller.exe` from [Releases](https://github.com/lukaszliniewicz/Pandrator/releases) and run it. Choose an installation location and the local services you need. The same application is used later to launch, update, repair, or extend the installation.

### Linux

Download `PandratorInstaller-x86_64.AppImage`, make it executable, and run it:

```bash
chmod +x PandratorInstaller-x86_64.AppImage
./PandratorInstaller-x86_64.AppImage
```

The default installation location is `~/Pandrator`. The installer keeps Pixi environments, services, model caches, and application data under the selected workspace. It does not install system packages. Calibre is optional and is needed only for MOBI conversion.

### Launching and access

The launcher starts the web application, worker, and selected speech services, waits for Pandrator to become ready, and opens it in your browser. Its **Open Web UI** action reopens the page if you close the tab.

Pandrator listens on `127.0.0.1` by default. The installer can enable access from other devices; non-local access requires owner authentication. Use HTTPS through a reverse proxy when exposing Pandrator beyond a trusted local network.

Closing the browser does not stop generation. Use the launcher to reopen the interface or stop the managed processes.

### Updating

Use **Update** in the installer. If Pandrator or one of its services is running, the installer can stop it before continuing. User data is preserved unless removal with data purging is explicitly requested.

Before the first web-based launch, Pandrator backs up legacy metadata and imports existing sessions into its new database without rewriting the original Qt data. Keep a separate backup of important work before any major application upgrade.

## Choosing local services

| Service | Voice type | Typical hardware path | Notes |
|---|---|---|---|
| Kokoro 82M | Pre-built | CPU, CUDA; experimental ROCm and Apple Silicon paths | Lightweight and a good first installation. |
| Qwen3 TTS | Pre-built and cloning | CPU, CUDA, Vulkan, Metal | CustomVoice 1.7B supplies named voices; Base 0.6B/1.7B clones references. |
| XTTS v2 | Cloning | CPU or CUDA | Mature multilingual cloning; GPU is much faster. |
| VoxCPM2 | Cloning | CUDA | Large multilingual model intended for capable NVIDIA hardware. |
| Fish S2 Pro | Cloning | Configurable native backend and quantization | Very broad declared language coverage. |
| Voxtral 4B | Pre-built | WGPU-compatible accelerator | Preset voices only in the packaged local service; no CPU path. |
| Silero | Pre-built | CPU | Efficient regional, East European, and legacy language packs. |
| Chatterbox | Cloning | CPU or CUDA | English and multilingual models. |
| Magpie 357M | Pre-built | CPU or CUDA | Five speakers shared across nine languages. |
| CrispASR | Transcription | CPU, CUDA, Vulkan; Metal on Apple Silicon | Whisper and Parakeet engines with word timestamps. |
| RVC | Speech-to-speech | CPU or CUDA | Applies a `.pth` model and matching `.index` after generation. |

Hardware requirements vary with model size, quantization, input length, and the selected compute backend. The installer shows the available variants and model licences before downloading them.

## Speech generation language support

“Pre-built voices” require no reference recording. “Voice cloning” uses a sample from the voice library. You need only **one** compatible model from the applicable column.

| Language(s) | Pre-built voices | Voice cloning |
|---|---|---|
| English, French, Spanish | Kokoro 82M, Qwen3 CustomVoice 1.7B, Voxtral 4B, Silero, Magpie 357M | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| German | Qwen3 CustomVoice 1.7B, Voxtral 4B, Silero, Magpie 357M | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Italian | Kokoro 82M, Qwen3 CustomVoice 1.7B, Voxtral 4B, Magpie 357M | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Portuguese | Kokoro 82M, Qwen3 CustomVoice 1.7B, Voxtral 4B | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Russian | Qwen3 CustomVoice 1.7B, Silero | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Chinese, Japanese | Kokoro 82M, Qwen3 CustomVoice 1.7B, Magpie 357M | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Korean | Qwen3 CustomVoice 1.7B | Qwen3 Base 0.6B/1.7B, XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Hindi | Kokoro 82M, Voxtral 4B, Silero, Magpie 357M | VoxCPM2, Fish S2 Pro, Chatterbox |
| Arabic, Dutch | Voxtral 4B | XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Polish, Turkish | — | XTTS v2, VoxCPM2, Fish S2 Pro, Chatterbox |
| Czech, Hungarian | — | XTTS v2, Fish S2 Pro |
| Vietnamese | Magpie 357M | VoxCPM2, Fish S2 Pro |
| Danish, Finnish, Greek, Hebrew, Malay, Norwegian, Swahili, Swedish | — | VoxCPM2, Fish S2 Pro, Chatterbox |
| Burmese, Indonesian, Khmer, Tagalog, Thai | — | VoxCPM2, Fish S2 Pro |
| Lao | — | VoxCPM2 |
| Armenian, Azerbaijani, Belarusian, Bengali, Georgian, Gujarati, Kannada, Kazakh, Malayalam, Tamil, Telugu, Ukrainian | Silero | Fish S2 Pro |
| Bashkir, Chuvash, Erzya, Kabardian-Cherkess, Kalmyk, Khakas, Kyrgyz, Manipuri, Moksha, Rajasthani, Tajik, Tatar, Udmurt, Uzbek, Yakut | Silero | — |
| Afrikaans, Albanian, Amharic, Assamese, Basque, Bosnian, Breton, Bulgarian, Catalan, Croatian, Estonian, Faroese, Galician, Haitian Creole, Icelandic, Javanese, Latin, Latvian, Lithuanian, Maori, Marathi, Mongolian, Nepali, Norwegian Nynorsk, Pashto, Persian, Punjabi, Romanian, Sanskrit, Serbian, Shona, Sindhi, Sinhala, Slovak, South Slavey, Tibetan, Urdu, Welsh, Yiddish, Yoruba | — | Fish S2 Pro |

Additional notes:

- Qwen3 CustomVoice provides its named pre-built voices. Qwen3 Base performs voice cloning.
- VoxCPM2 also supports several Chinese dialects.
- Silero’s legacy Indic model supports Hindi, Malayalam, Manipuri, Bengali, Rajasthani, Tamil, Telugu, Gujarati, and Kannada. It expects [ISO-romanized input](https://github.com/snakers4/silero-models#indic-languages-v4).
- A declared language indicates backend support, not equal quality across every model or voice.
- Model licences differ. The installer displays the applicable licence and usage conditions before installation.
- Custom and commercial endpoints may provide languages not listed here.

## Transcription language support

Both transcription engines run through CrispASR and produce word-level timestamps. Model choice, quantization, compute backend, language, and VAD settings are configurable.

| Model | Available variants | Coverage |
|---|---|---|
| Whisper large-v3 | FP16 or Q5_0 | 100 languages; the broadest option |
| Parakeet TDT 0.6B v3 | FP16, Q8_0, Q5_0, or Q4_K | 25 primarily European languages |

<details>
<summary>Whisper large-v3 language list</summary>

English, Chinese, German, Spanish, Russian, Korean, French, Japanese, Portuguese, Turkish, Polish, Catalan, Dutch, Arabic, Swedish, Italian, Indonesian, Hindi, Finnish, Vietnamese, Hebrew, Ukrainian, Greek, Malay, Czech, Romanian, Danish, Hungarian, Tamil, Norwegian, Thai, Urdu, Croatian, Bulgarian, Lithuanian, Latin, Maori, Malayalam, Welsh, Slovak, Telugu, Persian, Latvian, Bengali, Serbian, Azerbaijani, Slovenian, Kannada, Estonian, Macedonian, Breton, Basque, Icelandic, Armenian, Nepali, Mongolian, Bosnian, Kazakh, Albanian, Swahili, Galician, Marathi, Punjabi, Sinhala, Khmer, Shona, Yoruba, Somali, Afrikaans, Occitan, Georgian, Belarusian, Tajik, Sindhi, Gujarati, Amharic, Yiddish, Lao, Uzbek, Faroese, Haitian Creole, Pashto, Turkmen, Nynorsk, Maltese, Sanskrit, Luxembourgish, Myanmar, Tibetan, Tagalog, Malagasy, Assamese, Tatar, Hawaiian, Lingala, Hausa, Bashkir, Javanese, Sundanese, and Cantonese.

</details>

Parakeet TDT 0.6B v3 supports Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French, German, Greek, Hungarian, Italian, Latvian, Lithuanian, Maltese, Polish, Portuguese, Romanian, Russian, Slovak, Slovenian, Spanish, Swedish, and Ukrainian.

## Input and output formats

| Category | Formats |
|---|---|
| Documents | TXT, PDF, EPUB, DOCX, MOBI, or pasted text |
| Subtitles | SRT |
| Audio sources | AAC, AIFF, FLAC, M4A/MKA, MP3, OGG, Opus, WAV, WMA |
| Video sources | MP4, MKV, WebM, AVI, MOV |
| Audiobook/audio output | M4B, MP3, Opus, FLAC, WAV |
| Video output | MP4-oriented export with selectable audio and subtitle tracks |

URL imports use `yt-dlp` for supported public media sources. Users are responsible for complying with the source service’s terms and applicable law.

## LLM processing and costs

LLMs are optional. Pandrator uses them for tasks where deterministic processing may not be sufficient:

- subtitle correction;
- translation and glossary-aware cleanup;
- document-cleaning assistance after deterministic extraction;
- optional text optimization for speech, such as expanding difficult numerals or improving phonetic spelling.

Correction, translation, whole-document optimization, and generation-time optimization remain separate operations and artifacts. Their results can be compared with the source and edited before later stages.

Providers and models are configured individually. A model may define optional temperature and reasoning defaults plus uncached input, cached input, and output rates. Pandrator prefers an authoritative cost returned by the provider and otherwise calculates a fallback from normalized token usage and the configured rates.

## Voice library and RVC

The voice library contains uploaded or recorded reference samples and the pre-built catalogues reported by installed services. You can:

- record from a browser-visible microphone and normalize the result through FFmpeg;
- play stored samples and edit their transcripts;
- transcribe selected or missing references through CrispASR;
- filter pre-built voices by language and generate persistent previews;
- generate previews for every pre-built voice in a selected language.

RVC is speech-to-speech conversion, not TTS training. Import a named `.pth` model and its matching `.index`, configure conversion per generation or per existing audio asset, and retain the original and converted takes for comparison.

## Running from source

The installers are recommended for normal use. For development, Pandrator requires Python 3.11, Node.js 24 for frontend builds, and Pixi.

```bash
git clone https://github.com/lukaszliniewicz/Pandrator.git
cd Pandrator
pixi install
pixi run -e web-build web-build
pixi run serve-web
```

Run the worker in a second terminal:

```bash
pixi run run-worker
```

The `pandrator` CLI also exposes session, source, workflow, job, artifact, provider, voice, RVC, training, export, authentication, migration, and doctor commands. Use `pandrator --help` and the subcommand help for the current interface. Stable JSON output is available through `--json`.

The separately packaged `pandrator-installer` CLI supports probing, planning, installing, updating, repairing, launching, stopping, and uninstalling. Uninstall preserves user data unless `--purge-data` is explicitly supplied.

## Security and privacy

- The default local server binds only to loopback.
- LAN or remote access requires explicit configuration and authentication.
- API credentials are resolved through supported secret stores or environment configuration rather than being stored as plaintext application settings.
- Imported and generated files remain under the selected data root unless an explicitly allowed local reference is used.
- Local processing does not make cloud processing private: review the provider’s terms before sending documents, subtitles, voices, or media to an external API.

For internet-facing use, place Pandrator behind an HTTPS reverse proxy, keep host and proxy validation enabled, and use a dedicated data root. Pandrator is designed for a single owner, not as a multi-user hosted service.

## Building the installer

Use the pinned installer build environment:

```bash
pixi run -e installer-build build-installer
```

On Windows this produces `dist/PandratorInstaller.exe`. On Linux it produces `dist/PandratorInstaller-x86_64.AppImage`. Linux AppImages must be built on Linux; use the oldest glibc baseline you intend to support.

The build runs packaged self-checks. The Linux build additionally checks the GUI bundle and TLS trust store. See `scripts/build_linux_appimage.py --help` for AppImage-specific options.

## Contributing

Bug reports, workflow descriptions, documentation corrections, and focused pull requests are welcome. Please include the operating system, relevant service/model, reproduction steps, and logs with secrets removed.

## Licence

Pandrator is released under [GNU AGPLv3](LICENSE). Speech, transcription, LLM, and voice-conversion models have their own licences and usage conditions; review the information shown by the installer and the linked upstream licence before use.
