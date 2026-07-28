<p align="center">
  <img src="pandrator.png" alt="Pandrator" width="180" />
</p>

# Pandrator

Pandrator is a local-first workspace for creating audiobooks, subtitles, and voiceovers. It combines document preparation, transcription, optional LLM correction and translation, speech generation, detailed review, and export in one browser-based interface.

Pandrator's browser interface is becoming the primary place to configure and
operate optional local model services. An independent, per-user
`pandrator-manager` process performs installation, update, repair, removal,
and process supervision, so those operations continue if the browser, tray,
or Pandrator itself exits. Docker and WSL are not required.

Pandrator Manager is the primary installer and launcher. Its Qt-free Windows
and Linux artifacts share the same per-user control plane, remember the chosen
installation location, and can update the Manager through project-signed
release manifests. The older Qt installer remains in the source tree as a
feature-frozen migration fallback. See
[Installer and manager architecture](INSTALLER_MANAGER_ARCHITECTURE.md).

## TL;DR

| If you want to… | Start with… |
|---|---|
| Create an audiobook with ready-made voices | **Kokoro** is the simplest lightweight starting point. Consult the [language table](#speech-generation-language-support) for alternatives. |
| Clone a voice from a reference recording | Install **Qwen3 TTS Base** or another cloning model supporting your language. |
| Create subtitles from audio or video | Install **CrispASR**. MOSS Transcribe-Diarize is the guided default; Whisper large-v3 and Parakeet TDT remain available alternatives. |
| Correct or translate text and subtitles | Configure a local or cloud LLM provider. This is optional for basic speech generation. |
| Convert generated speech to another trained voice | Install **RVC**. It runs after speech generation and is optional. |

The current packaged release is
[Pandrator 0.6.0](https://github.com/lukaszliniewicz/Pandrator/releases/tag/v.0.6.0),
with Pandrator Manager 0.9.0 for Windows and Linux. You can begin with only the
components you need and add others later.

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

### Current packaged releases

Pandrator Manager is the supported packaged setup and recovery path. It
installs Pandrator first, then any selected speech components, and can add,
update, repair, or remove components on later runs without changing the
remembered workspace.

### Windows

Download
[`PandratorManager-0.9.0-windows-x86_64.exe`](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.6.0/PandratorManager-0.9.0-windows-x86_64.exe)
and run it.
Choose the parent installation folder; the managed installation is created in
its `Pandrator` subdirectory. The same Manager is used later to launch,
update, repair, or extend the installation.

The Windows executable is not Authenticode-signed and may be shown as
**Unknown publisher** or trigger a SmartScreen warning. Where a release
publishes a SHA-256 checksum, verify it before running the download. The
[0.6.0 release page](https://github.com/lukaszliniewicz/Pandrator/releases/tag/v.0.6.0)
contains the checksum and all other package formats.

### Linux

Download
[`PandratorManager-0.9.0-x86_64.AppImage`](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.6.0/PandratorManager-0.9.0-x86_64.AppImage),
make it executable, and run it:

```bash
chmod +x PandratorManager-0.9.0-x86_64.AppImage
./PandratorManager-0.9.0-x86_64.AppImage
```

The first desktop launch offers a folder chooser. Headless systems can pass
`setup --workspace /path/to/parent --no-open`. The Manager keeps Pixi
environments, services, model caches, and application data under the selected
workspace. It does not install system packages. Calibre is optional and is
needed only for MOBI conversion.

### Manager package and automation

`pandrator-manager` is a separate, Qt-free Python distribution. Installing its
wheel has no service-registration, elevation, component-download, or other
host side effects. The wheel is suitable for developers, automation, and
advanced users; this README does not claim that it has been uploaded to PyPI.
Self-contained native artifacts remain the recommended consumer path because
they provide their own tested runtime and desktop integration.

Product-update signatures are project-controlled Ed25519 signatures over
release manifests and artifact hashes; they do not require an EV certificate.
The Windows bootstrap remains deliberately Authenticode-unsigned, so this
cryptographic verification does not remove Windows' first-download
Unknown-publisher warning.

To exercise the current source checkout in an isolated environment:

```bash
python -m pip install ./pandrator_manager
pandrator-manager --workspace /path/to/workspace start-manager
pandrator-manager --workspace /path/to/workspace open --recovery
```

On Linux, build the self-contained Qt-free manager AppImage with:

```bash
python scripts/build_manager_appimage.py
sha256sum --check dist/PandratorManager-0.9.0-x86_64.AppImage.sha256
chmod +x dist/PandratorManager-0.9.0-x86_64.AppImage
APPIMAGE_EXTRACT_AND_RUN=1 \
  dist/PandratorManager-0.9.0-x86_64.AppImage setup --workspace "$HOME"
```

`APPIMAGE_EXTRACT_AND_RUN=1` is a compatibility fallback for systems where
FUSE AppImage mounting is unavailable; ordinary desktops may run the AppImage
directly.

Use `pandrator-manager --help` for planning, component, runtime, operation,
cancellation, and per-user autostart commands. Mutations always produce an
exact reviewable plan followed by a durable operation; a normal package
installation never downloads models or enables autostart.

Common recovery and release commands are:

```bash
pandrator-manager --workspace /path/to/workspace doctor
pandrator-manager --workspace /path/to/workspace legacy
pandrator-manager --workspace /path/to/workspace legacy-import --yes
pandrator-manager --workspace /path/to/workspace releases
pandrator-manager --workspace /path/to/workspace release-plan \
  --manifest /path/to/signed-manifest.json
pandrator-manager --workspace /path/to/workspace release-update \
  --manifest /path/to/signed-manifest.json --yes --wait
pandrator-manager --workspace /path/to/workspace uninstall \
  --preserve-data --yes --wait
```

`legacy` is read-only. `legacy-import` requires the exact inspected source
digest internally and is idempotent. Uninstall preserves `Pandrator/data` by
default; `--purge-data` is a separate destructive confirmation, while
`--export-data NEW_ARCHIVE.zip` creates and verifies an archive before
removal.

Each non-empty plan includes typed host preflight results and repeats them
immediately before the first mutation. Checks cover disk headroom, supported
runtime-tool artifacts, custom CA paths, source transport, offline
availability, conservative Windows path pressure, and occupied ports. When a
backend needs Pixi, the manager installs one pinned, SHA-256-verified copy in
its private runtime path and journals its promotion so failure restores the
previous owned executable. It never terminates an unknown process merely to
free a port.

Tray support is optional:

```bash
python -m pip install "./pandrator_manager[tray]"
pandrator-tray --workspace /path/to/workspace --check
pandrator-tray --workspace /path/to/workspace
```

Closing or omitting the tray does not stop the manager, Pandrator, or any
managed backend. Headless Linux systems can use the same manager and CLI
without installing the tray extra. With the extra installed but no graphical
session, `--check` returns a concise unavailable capability instead of an X11
traceback.

Kokoro and VoxCPM have Manager-owned bootstrap adapters with fixed service
ports and persistent model/data roots. CrispASR uses a pinned native runtime
and defaults to the MOSS diarization model; Qwen3 TTS defaults to the 1.7B
model. XTTS fine-tuning remains explicitly unavailable until its training
environment receives the same qualification.
The WebUI derives whether to show an action from the manager's canonical
capabilities; it must not invent an installation path for an unavailable
action.

### Launching and access

The Manager starts the web application, worker, and selected speech services,
waits for Pandrator to become ready, and opens it in your browser. The native
launcher and optional tray are clients; the independent Manager owns the
processes and durable operations.

Pandrator and its setup manager listen on `127.0.0.1` by default. Explicit
private-network and HTTPS-ingress profiles support a home server, rented GPU
machine, or pod while preserving that safe default. A headless native first
run can configure both workstation-facing addresses:

```bash
pandrator-manager-launcher setup \
  --workspace /srv/pandrator \
  --remote-setup-url https://setup.example.test \
  --remote-pandrator-url https://pandrator.example.test \
  --trusted-proxy-hops 1 \
  --no-open
```

The command prints an expiring, one-use recovery URL instead of trying to
depend on a browser on the server. HTTPS mode expects an operated reverse
proxy and binds to loopback by default; cross-namespace pod ingress requires
an explicit `--network-bind-host 0.0.0.0` plus network policy. Trusted-LAN
HTTP requires exact `http://host:port` URLs and
`--allow-insecure-private-network`. Non-local Pandrator access requires owner
authentication, and passwords cannot be submitted over remote plain HTTP.
The recovery manager controls one host/workspace rather than acting as a fleet
manager.

Closing the browser does not stop generation. Use the launcher or Manager
recovery UI to reopen the interface or explicitly stop managed processes.

### Updating

Open Pandrator Manager and choose **Maintenance → Check for update** to update
the Manager from the project-signed release channel. Pandrator and backend
updates use the contextual **Review update** actions in **Install & launch**.
The Manager stages a fresh repository checkout, validates it, and switches the
active release only after the operation succeeds, so the Manager itself can
also be updated without downloading a new bootstrap executable.

Install, update, repair, and removal share the same staged, journalled
operation model. A running backend is stopped before replacement, its previous
running intent is restored after validation, and an activation or
database-commit failure returns to the previous slot. User data is separate
and preserved unless a purge is explicitly reviewed and confirmed.

Before the first web-based launch, Pandrator backs up legacy metadata and imports existing sessions into its new database without rewriting the original Qt data. Keep a separate backup of important work before any major application upgrade.

## Choosing local services

| Service | Voice type | Typical hardware path | Notes |
|---|---|---|---|
| Kokoro 82M | Pre-built | CPU, CUDA; ROCm only on explicitly supported modern AMD hardware | Lightweight and a good first installation; legacy AMD cards use CPU guidance. |
| Qwen3 TTS | Pre-built and cloning | CPU, CUDA, Vulkan, Metal | The 1.7B model is the default. CustomVoice supplies named voices; Base clones references. |
| XTTS v2 | Cloning | CPU or CUDA | Mature multilingual cloning; GPU is much faster. |
| VoxCPM2 | Cloning | CUDA | Large multilingual model intended for capable NVIDIA hardware. |
| Fish S2 Pro | Cloning | Configurable native backend and quantization | Very broad declared language coverage. |
| Voxtral 4B | Pre-built | WGPU-compatible accelerator | Preset voices only in the packaged local service; no CPU path. |
| Silero | Pre-built | CPU | Efficient regional, East European, and legacy language packs. |
| Chatterbox | Cloning | CPU or CUDA | English and multilingual models. |
| Magpie 357M | Pre-built | CPU or CUDA | Five speakers shared across nine languages. |
| CrispASR | Transcription | CPU, CUDA, Vulkan; Metal on Apple Silicon | MOSS with diarization is the default; Whisper and Parakeet remain available. |
| RVC | Speech-to-speech | CPU or CUDA | Applies a `.pth` model and matching `.index` after generation. |

Hardware requirements vary with model size, quantization, input length, and the selected compute backend. The installer shows the available variants and model licences before downloading them.

In the manager-enabled WebUI, each compatible TTS card has an explicit
connection mode:

- **Managed local** binds the provider to a stable manager service identity.
  The endpoint is resolved at execution time, so a port change does not rewrite
  the provider profile.
- **External endpoint** retains the user-entered URL and works without a local
  manager.

Install, update, repair, remove, compute selection, and start/stop actions are
shown contextually under **Providers & services**. The canonical component
inventory and exact operation plan remain manager-owned. Installing a local
backend never silently changes the default TTS provider, and a removal plan
reports provider profiles that still depend on that component.

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

Correction and LLM translation can optionally use bounded Jina web research to resolve uncertain names, terminology, and source-language details. Research is opt-in, uses explicit search and extraction budgets, and produces a reviewable source ledger alongside the stage artifact. Store the Jina key under **Providers & services → Other API keys**.

Speech optimization uses a structured speech plan. Guarded mode, the default, limits the model to typed changes over detected spans; flexible mode permits a full-sentence speech rewrite while protecting important spans and validating retention. Pronunciation suggestions use readable syllabic spelling such as `ee-mah-oh-kah`; Pandrator removes separators deterministically only when compiling text for a TTS backend.

Display and speech text are separate. Correction and translation create viewer-facing revisions, while speech plans and optimized delivery text are synthesis-only. They therefore do not leak into exported subtitles or other display-oriented outputs. The pronunciation library exposes proposed, reviewed, and disabled entries for editing and reuse, with session-specific entries taking precedence over global ones.

Correction, translation, whole-document speech planning, and generation-time speech planning remain separate operations and artifacts. Their results can be compared with the source and edited before later stages.

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

> [!NOTE]
> Concurrent workers may share one data directory. Claims, resource leases, and
> terminal job updates are transactionally serialized and fenced by lease
> generation. The launcher starts one managed worker by default; additional
> standalone workers should use distinct worker IDs. A presence warning makes
> accidental duplicate launches visible, but does not block intentional worker
> scaling. Do not start a second web supervisor for the same data directory.

Hardware and installed-runtime capability discovery is cached in the Pandrator
database for five minutes, so normal page loads and job events never rerun GPU,
FFmpeg, or model-cache probes. **Settings → Capability snapshot → Probe again**
forces a fresh diagnostic scan. Developers and managed deployments can change
the cache duration by setting `PANDRATOR_CAPABILITY_TTL_SECONDS` to a
non-negative number before starting the web process. Live TTS service health
and saved provider configuration remain separate from this stable snapshot.

Audio output assembly uses bounded-memory streaming by default. Pandrator
normalizes only incompatible takes with FFmpeg, streams compatible PCM WAV
takes directly, writes silence and subtitle timing in chunks, and atomically
replaces the finished output. Temporary PCM data is kept beside the session
assembly and removed after success, cancellation, or failure, so very long
projects require temporary disk capacity but do not require memory proportional
to their duration. Waveform peaks are likewise calculated in FFmpeg at the
requested browser resolution rather than decoding the complete file in Python.
During the compatibility period, set `PANDRATOR_AUDIO_ASSEMBLER=pydub` before
starting the worker to restore the legacy in-memory assembler. Remove the
variable (or set it to `streaming`) to use the default path.

Workflow snapshots load only current state and a ten-version preview for each
stage; the history control retrieves older versions in separate pages. Ranked
job, artifact, lineage, and selected-take queries are backed by workload-specific
indexes installed automatically by the database migration. On an unusually
large existing database, the first startup after this migration may therefore
spend a short period building those indexes before the server becomes ready.
Legacy per-session source rows are likewise promoted into the reusable source
library by a marked, idempotent database migration. Retention and expired-upload
cleanup run in a background maintenance thread after the application is ready,
so their work does not delay request serving.

`pandrator/logic/state_db_handler.py` is deprecated, migration-only Qt
compatibility code. The browser database models are authoritative, and new web
features must not depend on the legacy module. It remains temporarily available
for the existing dubbing compatibility paths and migration qualification; it
can be removed after the external Qt cutover gates are complete and those
callers have moved to the web data model.

To capture the current concurrency, query, audio, capability, and browser request
baselines on disposable data:

```bash
python scripts/phase0_baseline.py --include-browser --output logs/phase0-baseline.json
```

The command reports known target failures but exits successfully when the
diagnostic itself completes. The output under `logs/` is local and ignored by
Git.

The `pandrator` CLI also exposes session, source, workflow, job, artifact, provider, voice, RVC, training, export, authentication, migration, and doctor commands. Use `pandrator --help` and the subcommand help for the current interface. Stable JSON output is available through `--json`.

During migration, the legacy `pandrator_installer` package still supplies the
Qt compatibility workflows. The new `pandrator-manager` distribution also
exports a temporary `pandrator-installer` command alias, but that alias is only
a bridge to the manager CLI and will be removed after the documented
deprecation window. Manager uninstall preserves user data unless
`--purge-data` is explicitly reviewed and confirmed.

## Security and privacy

- The default local server binds only to loopback.
- LAN or remote access requires explicit configuration and authentication.
- Provider credentials are write-only through the API; responses report only
  their storage source and non-secret locator.
- Imported and generated files remain under the selected data root unless an explicitly allowed local reference is used.
- Local processing does not make cloud processing private: review the provider’s terms before sending documents, subtitles, voices, or media to an external API.

Pandrator offers four credential-storage routes in provider settings:

1. **Pandrator database (default).** Paste the value in the UI. This is the
   easiest route and requires no operating-system setup. The value is kept out
   of normal settings and remains write-only, but it is stored in the local
   SQLite database without separate at-rest encryption. Database backups
   therefore contain database-stored credentials. Protect the data directory
   with account permissions and use full-disk encryption where local data theft
   is a concern.
2. **Operating-system credential store.** Install the optional integration with
   `pip install "pandrator[credential-stores]"`, restart Pandrator, and choose
   this route in the UI. Pandrator detects whether Python can use Windows
   Credential Manager, macOS Keychain, or a Linux Secret Service backend,
   verifies the saved value, and keeps only its service/user reference in the
   database. Headless Linux installations need an unlocked Secret Service
   backend available to the service account.
3. **Environment variable.** Set the variable for the account that starts
   Pandrator, then enter only its name in the UI. For example, a Windows user
   can persist a value from PowerShell with
   `[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "<value>", "User")`;
   macOS/Linux shells can export `OPENAI_API_KEY` or configure it in the process
   supervisor. Restart Pandrator after changing a persistent user or service
   environment. The availability check never returns the value.
4. **Secret file.** Create a UTF-8 text file containing only the credential and
   select its absolute path. On macOS/Linux, make it owner-only, for example
   `chmod 600 /run/secrets/openai_api_key`. Windows access should be restricted
   with the file ACL. Managed and container secret mounts are supported.
   Pandrator stores the path, validates that the file is readable and no larger
   than 1 MiB, and never returns its contents.

Changing storage is an explicit, verified move: Pandrator confirms that the new
backend resolves before changing the reference. Deleting the previous
app-managed value is opt-in, and shared provider credentials are retained when
another connection may still use them. Environment variables and secret files
remain externally managed. Session bundles contain session-scoped records and
artifacts, not the global stored-credential table.

Known configured values and credential-shaped fields are redacted from durable
job events, progress details, results, failures, and worker tracebacks. Still
inspect diagnostics before sharing them, particularly when a third-party
library formats an unfamiliar credential type.

For internet-facing use, place Pandrator behind an HTTPS reverse proxy, keep
host and proxy validation enabled, and use a dedicated data root. Remote login
attempts are throttled per client address, and the UI warns when remote access
arrives over plain HTTP. Pandrator is designed for a single owner, not as a
multi-user hosted service.

## Dependency manifests

`pyproject.toml` is authoritative for the Pandrator package and
`pandrator_installer/pyproject.toml` is authoritative for the separately
packaged compatibility installer. `pandrator_manager/pyproject.toml` is the
independent Qt-free manager distribution and intentionally has its own
Python-version range and optional `tray`, `build`, and `dev` extras. The
pip-compatible application/compatibility-installer files are generated
projections:

```bash
python scripts/generate_requirements.py
python scripts/generate_requirements.py --check
```

The first command refreshes `requirements.txt` and
`requirements-installer.txt`; the second is suitable for CI and fails if
either file drifted. Update the relevant `pyproject.toml`, never a generated
requirements file by itself.

## Building the installer

Use the pinned installer build environment:

```bash
pixi run -e installer-build build-installer
```

On Windows this produces `dist/PandratorInstaller.exe`. On Linux it produces `dist/PandratorInstaller-x86_64.AppImage`. Linux AppImages must be built on Linux; use the oldest glibc baseline you intend to support.

The build runs packaged self-checks. The Linux build additionally checks the GUI bundle and TLS trust store. See `scripts/build_linux_appimage.py --help` for AppImage-specific options.

For a release-compatible AppImage from Windows, macOS, or Linux, use the container wrapper:

```bash
python scripts/build_appimage_container.py
```

The wrapper auto-detects Docker or Podman, builds inside the pinned Debian 11/Pixi environment, and writes only the final artifact to `dist/`. The checkout is mounted read-only, so host `.pixi`, `build`, and `dist` contents cannot leak into the Linux build. The locked release target is currently `linux/amd64`; the host may be Windows, macOS, x86_64 Linux, or an ARM machine with x86_64 container emulation. Run `python scripts/build_appimage_container.py --help` for runtime, output, rebuild, and smoke-test options.

## Building and qualifying Pandrator Manager

Use Python 3.11 or 3.12 in an isolated environment:

```bash
python -m pip install -e "./pandrator_manager[build,dev]"
python -m pytest -q tests/test_manager_*.py
ruff check --config pandrator_manager/pyproject.toml \
  pandrator_manager \
  scripts/build_manager_appimage.py \
  scripts/build_manager_bootstrap.py \
  scripts/build_manager_release_bundle.py \
  scripts/generate_manager_release_key.py \
  scripts/qualify_manager_lifecycle.py
python -m build pandrator_manager --outdir manager-dist
python scripts/build_manager_bootstrap.py --wheel-dir manager-dist
python scripts/build_manager_release_bundle.py
python scripts/qualify_manager_lifecycle.py
```

The bootstrap builder extracts and analyzes the one wheel in `manager-dist`;
it fails if PyInstaller resolves manager code from the checkout, records the
wheel SHA-256 in its report, uses a disposable PyInstaller cache, runs a frozen
self-check, and excludes Qt. The lifecycle qualification uses a disposable
workspace and ephemeral test signing key to exercise bootstrap setup,
authenticated manager takeover, and preserve-data uninstall. It must never be
pointed at a real workspace.

On Windows the output is `dist/PandratorManagerBootstrap.exe` plus
`dist/pandrator-manager-<version>-windows-x86_64.zip`. The executable is
intentionally not Authenticode-signed. On Linux the corresponding bootstrap is
a native ELF and the release bundle suffix is `linux-x86_64`; a universal
release build still needs the oldest supported glibc baseline.

To smoke-test the wheel as a tool before public PyPI publication:

```bash
pipx install manager-dist/pandrator_manager-*.whl
# or
uv tool install manager-dist/pandrator_manager-*.whl
```

The cross-platform workflow in `.github/workflows/manager-ci.yml` repeats
manager tests, packaging, exact-wheel freezing, signed lifecycle handoff, and
uninstall on Python 3.11/3.12 and Windows/Ubuntu runners. Publishing,
production release signing, SBOM/provenance generation, and channel promotion
remain separate release gates.

## Contributing

Bug reports, workflow descriptions, documentation corrections, and focused pull requests are welcome. Please include the operating system, relevant service/model, reproduction steps, and logs with secrets removed.

Backend contributors should read
[`BACKEND_ARCHITECTURE.md`](BACKEND_ARCHITECTURE.md) before adding
routes, durable job kinds, or TTS providers.

Frontend contributors should read
[`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md) before adding API
operations, shared server state, invalidation behavior, or workflow UI.

## License

Pandrator is released under the [MIT License](LICENSE). This applies to
Pandrator's own source code and does not relicense third-party dependencies,
speech models, transcription models, LLMs, or voice-conversion models. Review
the licences and usage conditions shown by the installer before downloading
those components.
