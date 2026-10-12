# Pandrator documentation

These guides explain how to install Pandrator, create audio and subtitles,
choose providers, and look after your projects. Start with the task you want to
complete.

## Getting started

| Goal | Guide |
| --- | --- |
| Install or update Pandrator | [Installation](getting-started/installation.md) |
| Make an audiobook | [Your first audiobook](getting-started/first-audiobook.md) |
| Transcribe, correct, or translate subtitles | [Your first subtitles](getting-started/first-subtitles.md) |
| Transcribe a file or microphone recording | [Quick transcription](reference/quick-transcription.md) |
| Create a synchronized voiceover | [Your first voiceover](getting-started/first-voiceover.md) |
| Choose a provider, model, or voice | [Providers and voices](guides/providers-and-voices.md) |
| Correct or translate text | [Correction and translation](guides/correction-and-translation.md) |
| Prepare names and wording for speech | [Pronunciation and speech text](guides/pronunciation-and-speech.md) |
| Work through an AI assistant | [Agent workflows](guides/agent-workflows.md) and [passive processing](guides/passive-dispatch.md) |

## Running and maintaining Pandrator

- [Updates, data, repair, and removal](operations/updates-data-and-repair.md)
- [Remote and headless installations](operations/remote-and-headless.md)
- [Connecting an assistant](operations/agent-connections.md)
- [Troubleshooting](operations/troubleshooting.md)
- [Privacy and security](security/privacy-and-security.md)

The [Manager guide](../pandrator_manager/README.md) covers Manager commands,
component installation, and recovery. The [MCP guide](../pandrator_mcp/README.md)
covers assistant connections, access permissions, and workflow operations.

## Reference

- [Supported formats and exports](reference/formats-and-exports.md)
- [Importing and preparing documents](reference/document-ingestion.md)
- [Subtitle-to-speech pipeline and parameters](reference/subtitle-pipeline.md)
- [Speech directions, dialogue, and character voices](reference/generation-controls.md)
- [Model identities and speech controls](reference/model-capabilities.md)
- [Sounds, music, project placement, and mixing](reference/sounds-and-mixing.md)
- [Choosing a sound or music model](reference/sound-and-music-models.md)
- [Sound and music generation requests](reference/audio-generation-models.md)
- [Advanced sound and music settings](reference/audio-generation-settings.md)
- [Speech-text optimization](reference/speech-optimization.md)
- [Automatic repair history and undo](guides/repair-history.md)
- [Local models, transcription, and audio preprocessing](reference/local-audio-tools.md)
- [Qwen forced alignment](guides/qwen-forced-alignment.md)
- [Contextual performance planning](speech-performance.md)

## Releases and documentation

[GitHub Releases](https://github.com/lukaszliniewicz/Pandrator/releases) has the
latest downloads, checksums, and release notes. Read the
[0.12.0 release notes](release-notes-0.12.0.md) for the current release.
Earlier changes are described in the [0.11.0](release-notes-0.11.0.md),
[0.10.1](release-notes-0.10.1.md), and
[0.10.0](release-notes-0.10.0.md) notes.

Public documentation describes features and workflows that readers can use.
Development plans, research logs, implementation reviews, and local acceptance
records are kept in private maintainer storage. The workflow instructions
packaged with the MCP server explain the same features to connected assistants.
