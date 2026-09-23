# Pandrator 0.10.0

[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.10.0/PandratorManager-0.9.25-windows-x86_64.exe)
[![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.10.0/PandratorManager-0.9.25-x86_64.AppImage)

For a normal installation, download the Windows `.exe` or Linux `.AppImage`
above. The Manager installs and updates Pandrator and local services. GitHub's
**Source code** archives are for developers.

This release includes **Pandrator 0.10.0**, **Manager 0.9.25**, and **MCP 0.5.0**.
The audio.cpp runtime remains **0.8.1**.

## Speech direction and multiple voices

- Direct whole-segment and phrase editing for speakers, voices, instructions,
  emotions, pace, cadence, emphasis, and supported vocal events. Compact speech
  XML preserves the accepted transcript and character identities.
- Contextual performance planning, manual review and locks, casting, and frozen
  generation settings. Preview shows the compiled input, instructions, provider
  options, and support report for each voice part before synthesis.
- Gemini can use general directions plus preceding and following text from the
  accepted speech plan. Setup can enable both context directions through MCP;
  regeneration still receives its neighbouring context. Native Google TTS now
  handles direct requests and LiteLLM fallback, and Vertex defaults to `global`.
- Eleven v3 directions and mapped laugh, sigh, and throat-clearing tags now reach
  the native API. Supported older ElevenLabs models receive previous/next text
  in stitching fields. Native voice settings are validated, previewed, and
  included in audio-reuse identity.

Phrase cues remain model-interpreted: they are not guaranteed acoustic boundaries.
Gemini context is labelled prompt text, not an enforced hidden channel. Eleven
v3 does not use the older-model stitching path. Its non-stability voice settings
are forwarded but marked approximate because provider documentation differs on
their effect. Vocal events are distinct from a background sound-effects workflow.

## Models, voices, and automation

- A unified catalogue with model details, languages, licences, reference
  requirements, instruction scope, context support, and availability. Expanded
  audio.cpp discovery and grouped Manager model selection make variants easier
  to compare. Catalogue membership does not imply an installed or runnable model.
- Voice Library collections, saved references, metadata and provenance, voice
  design, publishing and casting. The default library view focuses on saved
  references.
- MCP adds catalogue/model-management workflows, atomic TTS setup for directions,
  context and vocalizations, and speech-selection preview/apply tools with
  revision, lock and idempotency checks. Provider switching now updates model and
  voice aliases together so an old local model cannot silently override a cloud
  selection. Update to MCP 0.5.0 and reconnect clients to discover the new schema.
- Capability corrections include Gemini vocal events, FireRed Instruct's
  reference-mode restriction, voice-specific Azure styles, and ElevenLabs model
  distinctions. Deprecated Turbo v2.5 stays discoverable; Flash v2.5 is the
  upstream recommendation for new selections.

## Transcription, generation, and export

- Qwen3 transcription and forced alignment through audio.cpp, quick-transcription
  access to Qwen and vocal isolation, and improvements to CJK/native-script
  subtitles and speech processing.
- Audiobook chunking, speech editing, generation recovery and missing-only
  generation preserve completed takes. Generation history, progress counts,
  virtualized rows, and on-demand model/history loading improve large sessions.
- Generation search remains available with Ctrl+K when segment option menus
  are closed, while open dialogs retain their own keyboard input.
- Block settings wait for the selected plan to load, so saving changes reliably
  offers preparation of a new plan even on a slow connection.
- Plan review waits for refreshed speech rows and their review status, keeping
  the approval button unavailable while the inspected content is loading.
- Live export progress survives older HTTP snapshots arriving afterward.
- Manager retains process ownership and recovery state when a service's exit
  cannot be confirmed, instead of reporting it stopped or starting a replacement.
- A simpler session workflow, casting controls, mobile navigation, and completed
  stage folding keep active work visible.
- Video export measures an overlong audio tail and asks whether to extend the
  final video frame for the full required duration. Existing work is retained;
  there is no arbitrary two-second truncation cap.

## Development quality and debt reduction

- A pinned Ruff policy now covers all Python packages, scripts, and tests.
  The cleanup resolves import/name issues, makes existing `zip` truncation
  explicit, and fixes callbacks that captured changing loop variables.
- Basedpyright replaces ad hoc mypy tooling, with explicit cross-platform type
  checks and import-cycle detection. A committed legacy baseline prevents new
  diagnostics from being silently accepted; it does not imply all historical
  typing debt is resolved. The catalogue, speech/provider and MCP cleanup fixes
  138 existing type errors; the initial repository baseline contains 1,288
  remaining diagnostics, including import cycles.
- High-confidence Python dead-code checks and the existing frontend formatter,
  linter, type checker, and dead-code checks are documented and enforced in CI.
  Unused frontend helpers and an unused subtitle component have been removed.
- The full cross-platform test workflow now runs on `main`, and every Python
  test file is assigned to an explicit execution lane.

See the [code quality policy](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/docs/development/code-quality.md) for commands and the
remaining type-debt boundary.

## Upgrade and verification

Install Manager 0.9.25 to receive the new launcher/catalogue package, then update
Pandrator. Existing workspace data and generation history remain in place;
database upgrades include performance plans, voice collections, and a segment
count index. As usual, retain a backup of valuable project data before upgrading.

The release-readiness pass covered model documentation, catalogue projection,
compiler/request consistency, MCP transport, migration preservation, generation
recovery, export-tail behavior, and browser workflows at desktop/mobile sizes.
Provider HTTP tests use mocked responses; no paid cloud synthesis or exhaustive
acoustic comparison of every model was performed. Native Windows/Linux Manager
packages, Python distribution audits, signed update metadata, and SHA-256
checksums accompany the release.

See [speech performance](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/docs/speech-performance.md),
[generation controls](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/docs/reference/generation-controls.md), and the
[MCP guide](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/pandrator_mcp/README.md) for details.
