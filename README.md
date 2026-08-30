<p align="center">
  <img src="https://raw.githubusercontent.com/lukaszliniewicz/Pandrator/main/pandrator.png" alt="Pandrator" width="180" />
</p>

# Pandrator

Pandrator is a local-first workspace for creating audiobooks, subtitles, and
voiceovers. It brings document preparation, transcription, optional AI
correction and translation, speech generation, review, and export into one
browser interface.

Local speech and transcription models can keep media on your own computer.
Cloud language, translation, and speech providers are optional and used only
when you configure and select them.

## Install Pandrator

Open the [latest release](https://github.com/lukaszliniewicz/Pandrator/releases/latest)
and choose the Manager for your operating system:

| System | Download | Notes |
| --- | --- | --- |
| Windows 10 or 11, 64-bit | `PandratorManager-…-windows-x86_64.exe` | Run the executable and choose the parent folder for the installation. Windows may show an unsigned-publisher warning. |
| Linux desktop, 64-bit | `PandratorManager-…-x86_64.AppImage` | Make the AppImage executable, run it, and choose the parent folder. |
| Headless Linux | Linux AppImage or Python package | Pass an explicit workspace and `--no-open`; use HTTPS or a VPN for remote access. |
| Developers | Source checkout with Pixi | Use the committed lockfile and the separate web-build environment. |

The Manager creates and owns one `Pandrator` workspace under the selected
parent directory. It installs the application and only the local components
you choose; Docker and WSL are not required. Start with one speech or
transcription engine and add others later.

See the [installation guide](docs/getting-started/installation.md) for checksum
verification, workspace selection, headless setup, and updates.

## Choose a workflow

| I want to… | Start here |
| --- | --- |
| Turn a document into narrated audio or an M4B audiobook | [Create your first audiobook](docs/getting-started/first-audiobook.md) |
| Transcribe media, correct subtitles, translate them, or export SRT/VTT | [Create your first subtitles](docs/getting-started/first-subtitles.md) |
| Generate synchronized speech and produce a dubbed video | [Create your first voiceover](docs/getting-started/first-voiceover.md) |
| Let the model in Codex, OpenCode, Claude Code, or another MCP host correct or translate queued subtitle batches | [Use passive dispatch](docs/guides/passive-dispatch.md) |
| Connect an agent safely to Pandrator or recover a managed installation | [Pandrator MCP](pandrator_mcp/README.md) |
| Install, update, repair, or operate local components | [Pandrator Manager](pandrator_manager/README.md) |

The [documentation index](docs/README.md) links the complete workflow,
operations, security, reference, and development guides.

## What Pandrator can do

### Audiobooks

- Import TXT, PDF, EPUB, DOCX, MOBI, or pasted text.
- Clean difficult documents with deterministic tools, OCR, and an optional
  reviewable AI workflow.
- Detect structure and chapters, normalize text, and split it for speech.
- Generate speech in segments, compare takes, edit text, regenerate selected
  passages, and apply optional RVC conversion.
- Export WAV, MP3, Opus, FLAC, or M4B with chapters, metadata, and cover art.

### Subtitles and voiceovers

- Start from SRT subtitles or common audio and video formats. WebVTT, ASS, and
  SSA uploads are recognized while the editing pipeline uses SRT as its
  working subtitle format.
- Transcribe speech with timestamps and optional diarization.
- Correct, translate, split, merge, and retime cues in a reviewable editor.
- Generate synchronized speech without changing the source media.
- Export subtitles, audio, or video with selectable audio and subtitle tracks.

### Voices and providers

- Use local TTS services or configured cloud speech providers.
- Connect local OpenAI-compatible LLM servers and supported cloud LLMs.
- Record or upload reference voices, retain their transcripts, and preview
  built-in voices before generation.
- Import fine-tuned XTTS bundles and RVC models while keeping original and
  converted takes available for review.

The [providers and voices guide](docs/guides/providers-and-voices.md) explains
the local engines, compute choices, voice cloning, and when data leaves the
Pandrator host.

## Reviewable AI, including no-extra-API dispatch

LLMs are optional. Pandrator can use a configured provider for document
cleanup, subtitle correction, glossary-aware translation, research, and
speech-text optimization. Each transformation creates a distinct revision or
artifact; it does not silently replace the source text.

Passive dispatch provides a different route. Pandrator makes no model call:
it snapshots the selected subtitle revision and queues deterministic, leased
batches. The model already running in an MCP host claims one batch, returns a
typed result over stable cue IDs, and continues sequentially. The final
subtitle artifact appears only after every batch is validated.

Read [correction and translation](docs/guides/correction-and-translation.md)
for the choice between manual review, a configured LLM, DeepL, and passive
dispatch. The exact cue, batch, timing, speech-block, and alignment model is in
the [subtitle pipeline reference](docs/reference/subtitle-pipeline.md).

## Local-first does not mean local-only

Pandrator binds to loopback by default and is designed for one owner. A home
server, VPN host, external HTTPS server, or GPU pod is possible when its data,
Manager state, identity, and network boundary are deliberately preserved.
Pandrator is not a public multi-user service.

The [remote and headless guide](docs/operations/remote-and-headless.md) covers
supported topologies. The [privacy and security guide](docs/security/privacy-and-security.md)
explains local and cloud data flows, credentials, diagnostics, MCP access, and
safe remote exposure.

## Agent access with Pandrator MCP

`pandrator-mcp` is a local stdio sidecar that lets an MCP-capable agent explain
Pandrator, inspect one fixed installation, edit sessions safely, execute
reviewed workflow plans, process passive subtitle batches, and perform bounded
Manager recovery. Target origins and credentials are process configuration,
not model-selected tool arguments.

The component guide documents:

- local, LAN/VPN, external HTTPS, and pod targets;
- least-privilege application and Manager scopes;
- owner-approved enrollment, identity pinning, rotation, and revocation;
- secret-free host configuration for Codex, Claude Code, OpenCode, and
  Antigravity;
- passive correction and translation; and
- diagnostics and optional recovery while Pandrator is stopped.

See the [Pandrator MCP guide](pandrator_mcp/README.md) for exact installation
and configuration commands.

## Documentation

| Area | Canonical documentation |
| --- | --- |
| Product setup and workflows | [Public documentation](docs/README.md) |
| Manager installation, CLI, recovery, and component operations | [Manager guide](pandrator_manager/README.md) |
| MCP installation, targets, scopes, host configuration, and protocol behavior | [MCP guide](pandrator_mcp/README.md) |
| Version history, downloads, and checksums | [GitHub Releases](https://github.com/lukaszliniewicz/Pandrator/releases) |
| Bugs, support requests, and proposals | [GitHub Issues](https://github.com/lukaszliniewicz/Pandrator/issues) |

Public product documentation is intentionally version-agnostic where possible.
Release-specific behavior and filenames belong on the corresponding release;
package-specific operational contracts remain beside their packages.

## Development

For source development, install [Pixi](https://pixi.sh/) and use the committed
lockfile:

```bash
git clone https://github.com/lukaszliniewicz/Pandrator.git
cd Pandrator
pixi install --locked
pixi install --environment web-build --locked
pixi run --environment web-build web-build
pixi run serve-web
```

Run `pixi run run-worker` in a second terminal. See
[development from source](docs/development/from-source.md) for environments,
test lanes, API/client regeneration, and package checks. Read
[contributing](docs/development/contributing.md) before preparing a change.

## Getting help

Start with [troubleshooting](docs/operations/troubleshooting.md). For a bug,
open a [GitHub issue](https://github.com/lukaszliniewicz/Pandrator/issues) with
your operating system, the affected model or provider, the action you took,
and the smallest reproducible sequence. When an installation or service action
fails, include the Manager's reviewed **Download diagnostics** support bundle.
Always inspect diagnostics before sharing them.

## License

Pandrator is released under the [MIT License](LICENSE). This covers Pandrator's
source code, not third-party dependencies, speech models, transcription
models, LLMs, or voice-conversion models. Review the licence and usage terms
shown before installing a model, and make sure you have the necessary rights
to source media and voice material.
