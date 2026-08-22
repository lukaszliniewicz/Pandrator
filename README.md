<p align="center">
  <img src="https://raw.githubusercontent.com/lukaszliniewicz/Pandrator/main/pandrator.png" alt="Pandrator" width="180" />
</p>

# Pandrator

Pandrator is a local-first workspace for creating audiobooks, subtitles, and
voiceovers. It brings document preparation, transcription, optional AI
correction and translation, speech generation, review, and export into one
browser interface.

Local speech and transcription models can keep your media on your own
computer. Cloud language and speech providers are optional.

## Quick start

The current release is
[Pandrator 0.8.13](https://github.com/lukaszliniewicz/Pandrator/releases/tag/v.0.8.13)
with Pandrator Manager 0.9.15. The Manager installs Pandrator, launches it, and
lets you add or remove speech components later. Docker and WSL are not
required.

### Windows

1. Download
   [PandratorManager-0.9.15-windows-x86_64.exe](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.8.13/PandratorManager-0.9.15-windows-x86_64.exe).
2. Run it and choose the parent folder for your installation.
3. Let the Manager install Pandrator, then open the browser interface.
4. Under **Providers & services**, install only the local models you want.

The Windows executable is not Authenticode-signed, so Windows may show
**Unknown publisher** or a SmartScreen warning. The
[release page](https://github.com/lukaszliniewicz/Pandrator/releases/tag/v.0.8.13)
collects the checksums for all downloadable files in one `SHA256SUMS` file.

### Linux

Download the AppImage, make it executable, and run it:

```bash
chmod +x PandratorManager-0.9.15-x86_64.AppImage
./PandratorManager-0.9.15-x86_64.AppImage
```

The first launch asks where to create the managed `Pandrator` folder. On a
headless machine, choose the workspace explicitly:

```bash
./PandratorManager-0.9.15-x86_64.AppImage \
  setup --workspace /srv/pandrator --no-open
```

If AppImage mounting is unavailable, prefix the command with
`APPIMAGE_EXTRACT_AND_RUN=1`.

### Updating

Download and run the newer Manager file for your operating system. It reuses
the remembered installation when possible; if it asks for a folder, choose the
same parent folder as before. Then use **Review update** in the Manager for
Pandrator or any installed engine. Projects, generated media, and other user
data are kept separately from replaceable application runtimes.

The Manager keeps the application, model environments, caches, services, and
data inside the selected workspace. It does not install operating-system
packages. Calibre is optional and is needed only for MOBI conversion.

### A good first setup

- For a lightweight ready-made voice, start with **Kokoro**.
- For voice cloning, start with **Qwen3 TTS Base** or **XTTS v2**.
- For transcription and subtitles, install **CrispASR**.
- Add an LLM provider only if you want AI-assisted correction, translation,
  research, or speech-text optimization.
- Add **RVC** only if you want to convert generated speech into a trained
  target voice.

You can begin with one component and add the others whenever you need them.

## What you can make

### Audiobooks

- Import TXT, PDF, EPUB, DOCX, MOBI, or pasted text.
- Clean difficult documents with deterministic tools, OCR, and an optional
  reviewable AI workflow.
- Detect structure and chapters, normalize text, and split it for speech.
- Generate speech in segments, compare takes, edit text, regenerate selected
  passages, and apply optional RVC conversion.
- Export WAV, MP3, Opus, FLAC, or M4B with chapters, metadata, and cover art.

### Subtitles and voiceovers

- Start from SRT subtitle files or common audio and video files. WebVTT, ASS,
  and SSA uploads are recognized as subtitle sources, while the current
  subtitle-editing pipeline uses SRT as its working input.
- Transcribe speech with word timestamps and optional diarization.
- Correct, translate, split, merge, and retime subtitles in a side-by-side
  editor.
- Generate and synchronize dubbed speech without changing the source media.
- Export subtitles alone or create dubbed video with original, mixed, or
  replacement audio and soft, burned, original-language, translated, or
  bilingual subtitles.

### Voices and providers

- Use local TTS services, OpenAI, Google Gemini, native ElevenLabs, or a
  compatible custom speech endpoint.
- Connect local OpenAI-compatible LLM servers such as LM Studio or supported
  cloud providers.
- Record or upload reference voices, keep transcripts with them, and preview
  built-in voices before generation.
- Import RVC models and retain both original and converted takes.

## Choosing local models

The Manager shows the available compute variants, model licences, and
downloads before making any changes.

| Component | Best suited to | Typical compute |
|---|---|---|
| Kokoro 82M | Lightweight built-in voices | CPU, CUDA, and supported modern AMD GPUs |
| Qwen3 TTS | Built-in voices and multilingual cloning | CPU, CUDA, Vulkan, or Metal |
| XTTS v2 | Mature multilingual voice cloning | CPU or CUDA |
| VoxCPM2 | Large multilingual voice cloning | CUDA |
| Fish S2 Pro | Broad-language voice cloning | Backend-dependent |
| Voxtral 4B | Preset voices | WGPU-compatible accelerator |
| Silero | Efficient regional language packs | CPU |
| Chatterbox | English and multilingual cloning | CPU or CUDA |
| Magpie 357M | Preset multilingual voices | CPU or CUDA |
| CrispASR | Transcription, timestamps, and diarization | CPU, CUDA, Vulkan, or Apple Silicon |
| RVC | Speech-to-speech voice conversion | CPU or CUDA |

Language support differs by model. Qwen3, XTTS, VoxCPM, Fish, and Chatterbox
cover many common multilingual cloning workflows; Silero and Fish add
especially broad regional coverage. CrispASR offers Whisper large-v3 for the
broadest transcription coverage and Parakeet TDT for a smaller set of
primarily European languages. Pandrator filters choices by the language and
capabilities reported by each installed service.

Hardware needs vary with model size, quantization, input length, and compute
backend. A GPU makes many models faster, but several useful configurations run
on CPU.

### Fine-tuned XTTS model bundles

Pandrator can install a complete fine-tuned XTTS bundle and make it available
in the **Model** picker. First update Pandrator and, on an existing
installation, update or repair **XTTS v2** in Pandrator Manager as well. The
XTTS service must support model uploads; the upload screen explains this if an
older installed component does not.

In a session's **Generate audio** settings, select **XTTS**, enter a friendly
model ID, and choose these four files together from one flat bundle directory:

- `config.json`
- `model.pth`
- `speakers_xtts.pth`
- `vocab.json`

The selection must contain exactly those four names. Training run directories,
nested checkpoint folders, and incomplete exports are rejected. Pandrator
streams the bundle to XTTS and installs it under managed user data, so do not
place models in a versioned service source directory. Installed model IDs
cannot be overwritten in this release; choose a new ID when importing a newer
bundle.

## Files and exports

| Category | Supported formats |
|---|---|
| Documents | TXT, PDF, EPUB, DOCX, MOBI, or pasted text |
| Subtitle sources | SRT working input; WebVTT, ASS, and SSA are also recognized as subtitle sources |
| Subtitle output | SRT or WebVTT |
| Audio input | AAC, AIFF, FLAC, M4A/MKA, MP3, OGG, Opus, WAV, WMA |
| Video input | MP4, MKV, WebM, AVI, MOV |
| Audiobook and audio output | M4B, MP3, Opus, FLAC, WAV |
| Video output | MP4-oriented export with selectable audio and subtitle tracks |

URL imports use `yt-dlp` for supported public media sources. You are
responsible for following the source service's terms and applicable law.

## AI correction and translation

LLMs are optional. Pandrator can use them for subtitle correction,
glossary-aware translation, document-cleaning assistance, and pronunciation
or speech-text improvements. Each step creates a separate, reviewable result;
AI-optimized speech text does not silently replace the text shown in exported
subtitles.

Models can have their own temperature, reasoning, and token-price settings.
When a provider reports an authoritative cost, Pandrator records it.
Otherwise, it estimates the cost from token usage and your configured rates.

Optional Jina research can help resolve uncertain names and terminology. It
uses explicit search limits and keeps a source ledger with the result.

### Speech optimization and pronunciation

Pandrator keeps the text shown in a session separate from the text sent to a
speech provider. Speech-text optimization is optional: it creates a separate,
reviewable speech revision and does not silently rewrite the source or subtitle
text. It uses Pandrator's configured multi-provider LLM adapter; Ollama is one
possible LLM provider, not a requirement.

The **Pronunciation library** is the deterministic path for known names, terms,
and respellings:

1. Open **Pronunciations**, add the written form and a lowercase structured
   respelling, then leave it as **Needs review** until you have checked it.
2. Approve it as **Reviewed**, choose **Global** or one session, and optionally
   restrict it to a language and TTS backend. `und` matches any language and `*`
   matches any backend.
3. In a session's **Text** settings, under **Segmentation and deterministic
   processing**, enable **Apply reviewed pronunciation-library overrides**.
   It is enabled by default and can be turned off independently of LLM speech
   optimization.

Only reviewed entries in the applicable scope are used. Matching is bounded,
case-insensitive, and longest-first without overlapping replacements. For
example, a reviewed entry `existential threat` → `egzistenszial fret` makes
the TTS request receive `egzistenszial fret` when its language/backend and
session scope match. This is a host-side deterministic replacement, not an
LLM suggestion. The source/display text remains `existential threat`; change
the entry or setting, then regenerate the affected audio to hear the result.
If you deliberately reuse an older saved speech plan, rebuild that plan too:
the toggle does not try to reverse respellings already stored in a historical
speech revision.

This is different from generic **Search/Replace** in a text or speech revision:
Search/Replace edits the selected segment/text revision and therefore changes
what is stored and displayed there. The Pronunciation library changes only the
speech payload at synthesis time. Guarded and flexible LLM speech planning can
propose new pronunciation entries, but proposals remain inactive until you
review them in the library.

### Mixed-language speech

Most local wrappers, including XTTS, accept one language for one synthesis
request. They do not interpret tags such as `[en]...[/en]`; those tags are sent
literally or otherwise treated as ordinary text. True inline code switching is
therefore still experimental.

When a foreign word or phrase can be isolated as its own segment, use a
per-segment language/voice override, or select/mark the affected segments and
choose **Generate alternate take**. The alternate dialog can choose a provider,
model, voice, language, speech prompt, and RVC settings where that service
supports them. A compatible multilingual voice may be reusable across the
selected languages; otherwise choose the provider's appropriate voice. These
are segment- or selection-level takes, not inline language tags.

A possible future surgical workflow is being evaluated: generate a
dominant-language baseline, align the tagged foreign span, regenerate only that
span with the same compatible voice in the other language, and splice it back
with duration/silence matching and confidence gating for review. It is not a
shipped feature yet, and it depends on provider voice compatibility and usable
word or character timing.

### Native ElevenLabs speech

ElevenLabs is available as a first-class native service under **Providers &
services → TTS**. Configure its ElevenLabs API key in the service settings, or
use the supported `ELEVENLABS_API_KEY` environment-variable fallback, then
refresh the model and voice catalogues before selecting a voice. Pandrator uses
the [native ElevenLabs API contract](https://elevenlabs.io/docs/api-reference/text-to-speech/convert);
it is **not** an OpenAI-compatible endpoint. An OpenAI-compatible ElevenLabs
intermediary can still be added as a custom provider using the normal
custom-provider settings. This integration has not been live-validated here
without an ElevenLabs key.

## Updating, repair, and data

Open Pandrator Manager and choose **Maintenance → Check for update** to update
the Manager. Updates for Pandrator and local services appear as reviewable
actions under **Install & launch** or **Providers & services**.

Install, update, repair, and removal actions show what will change before they
run. Components are staged and checked before becoming active. User data is
kept separately and is preserved by default during uninstall; deleting it
requires a separate confirmation.

Closing the browser does not stop an active generation job. Reopen Pandrator
from the Manager, launcher, or optional tray. Keep an independent backup of
important projects before a major update.

For command-line recovery, automation, and advanced deployment details, see
the
[Pandrator Manager guide](https://github.com/lukaszliniewicz/Pandrator/tree/main/pandrator_manager).

## Remote and headless use

Pandrator and its recovery interface listen on `127.0.0.1` by default. You can
also run one Pandrator installation on a home server, a LAN or VPN host, an
external server, or a GPU pod.

For an Internet-facing installation, use stable HTTPS addresses through a
reverse proxy or ingress:

```bash
pandrator-manager-launcher setup \
  --workspace /srv/pandrator \
  --remote-setup-url https://recovery.example.com \
  --remote-pandrator-url https://pandrator.example.com \
  --trusted-proxy-hops 1 \
  --no-open
```

When the ingress runs in another container or network namespace, add
`--network-bind-host 0.0.0.0` and restrict the listener with the platform's
network policy. Persist both the Pandrator data directory and the Manager
workspace. Use a VPN or HTTPS for regular remote use; plain HTTP is available
only as an explicit trusted-private-network option.

Pandrator is designed for one owner, not as a public multi-user service.

## Agent access with Pandrator MCP

`pandrator-mcp` lets an MCP-capable agent explain Pandrator, inspect an
installation, edit sessions safely, run workflow plans, and carry out bounded
Manager recovery actions. The sidecar runs on the same computer as the agent
and can connect to either a local installation or one fixed remote Pandrator
server.

It includes secret-free configuration generators for Codex, Claude Code,
OpenCode, and Antigravity. Credentials are enrolled with owner approval and
stored in the operating-system credential store rather than in MCP
configuration or model-visible arguments.

The
[Pandrator MCP guide](https://github.com/lukaszliniewicz/Pandrator/tree/main/pandrator_mcp)
walks through:

- local, LAN/VPN, external HTTPS, and pod targets;
- least-privilege scopes and identity pinning;
- owner-approved login and credential revocation;
- optional recovery while the Pandrator application is stopped;
- diagnostics and host configuration; and
- a guarded prompt an agent can use to help prepare a server or pod.

## Privacy and security

- Local models keep processing on your machine. Cloud providers may receive
  documents, subtitles, media, or voice samples and may charge for use.
- Local access binds to loopback by default. Remote access must be enabled
  explicitly and requires owner authentication.
- Provider credentials are write-only through the normal API and are redacted
  from routine diagnostics and job events.
- The simplest credential option stores the secret in Pandrator's local
  database. Protect the data directory and use full-disk encryption where
  appropriate.
- The optional `credential-stores` extra can use Windows Credential Manager,
  macOS Keychain, or Linux Secret Service. Environment variables and
  owner-restricted secret files are also supported.
- For remote use, keep proxy and host validation enabled, use a dedicated data
  root, and expose only the interfaces you need.

Always inspect diagnostics before sharing them, especially when third-party
libraries or providers are involved.

## Running from source

Packaged Manager releases are the easiest way to use Pandrator. For source
development, install [Pixi](https://pixi.sh/) and use the committed lockfile:

```bash
git clone https://github.com/lukaszliniewicz/Pandrator.git
cd Pandrator
pixi install --locked
pixi install --environment web-build --locked
pixi run --environment web-build web-build
pixi run serve-web
```

The default (`dev`) environment supplies Python 3.11, the editable workspace
packages, and development tools. `web-build` supplies the locked Node.js toolchain; its
`web-build` task uses `npm ci`. Run the worker in a second terminal:

```bash
pixi run run-worker
```

### Test lanes

Validate the test-lane manifest with `pixi run check-test-lanes`.
`test-fast` runs the measured fast lane with two file-affinity workers;
`test-full` is the authoritative serial suite. Use `test-profile` for the
serial suite with the 30 slowest tests reported.

Use `pandrator --help` for command-line session, workflow, provider, voice,
export, authentication, migration, and diagnostic commands. Add `--json` when
you need machine-readable output.

Python packages are also available for advanced installations and automation:

```bash
python -m pip install pandrator
pipx install pandrator-manager
pipx install "pandrator-mcp[credential-stores,manager]"
```

The native Manager remains the simplest choice for a complete installation
because it supplies its own runtime and guides component setup.

## Getting help and contributing

Use [GitHub Issues](https://github.com/lukaszliniewicz/Pandrator/issues) for
bug reports, workflow suggestions, and documentation corrections. Include
your operating system, the model or provider involved, steps to reproduce the
problem, and the Manager's reviewed **Download diagnostics** support bundle
when an installation or service operation fails.

Focused pull requests are welcome. Please keep changes scoped and include
tests where practical.

## License

Pandrator is released under the
[MIT License](https://github.com/lukaszliniewicz/Pandrator/blob/main/LICENSE).
This covers
Pandrator's source code, not third-party dependencies, speech models,
transcription models, LLMs, or voice-conversion models. Review the licence and
usage terms shown before installing a model.
