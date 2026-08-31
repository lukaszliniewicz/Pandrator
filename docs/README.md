# Pandrator documentation

This documentation explains how to install, use, operate, and understand
Pandrator. Choose the path that matches the result you want; you do not need to
read it in order.

## Start here

| Goal | Guide |
| --- | --- |
| Install or update Pandrator | [Installation](getting-started/installation.md) |
| Make an audiobook | [Your first audiobook](getting-started/first-audiobook.md) |
| Transcribe, correct, or translate subtitles | [Your first subtitles](getting-started/first-subtitles.md) |
| Create a synchronized voiceover | [Your first voiceover](getting-started/first-voiceover.md) |
| Choose a model, provider, or voice | [Providers and voices](guides/providers-and-voices.md) |
| Decide how correction or translation should run | [Correction and translation](guides/correction-and-translation.md) |
| Use the model already running in an MCP host | [Passive dispatch](guides/passive-dispatch.md) |
| Take a local file through an agent-run workflow and return deliverables | [End-to-end agent workflows](guides/agent-workflows.md) |
| Fix names or prepare text specifically for speech | [Pronunciation and speech text](guides/pronunciation-and-speech.md) |
| Diagnose a problem | [Troubleshooting](operations/troubleshooting.md) |

## Operations

- [Updates, data, repair, and removal](operations/updates-data-and-repair.md)
- [Remote and headless deployments](operations/remote-and-headless.md)
- [Troubleshooting](operations/troubleshooting.md)
- [Privacy and security](security/privacy-and-security.md)

Exact Manager commands, component recipes, recovery behavior, and automation
interfaces live in the [Pandrator Manager guide](../pandrator_manager/README.md).

## Reference

- [Supported formats and exports](reference/formats-and-exports.md)
- [Document ingestion and narration pipeline](reference/document-ingestion.md)
- [Subtitle-to-speech pipeline and parameters](reference/subtitle-pipeline.md)
- [Speech-text optimization and dispatch](reference/speech-optimization.md)

The document reference covers upload lineage, PDF layout/OCR, EPUB structure,
cleanup, narration preparation, and generation segments. The subtitle reference
explains the durable distinction between timed words, display cues, LLM
batches, speech blocks, generation takes, and alignment. The speech reference
compares standalone, generation-time, and passive optimization and documents
their batching and validation contracts.

## MCP and agents

The [Pandrator MCP guide](../pandrator_mcp/README.md) is the canonical reference
for installing the sidecar, enrolling targets, selecting scopes, generating
host configuration, diagnostics, protocol compatibility, and app-down Manager
recovery. Public workflow docs explain why and when to use MCP; the component
guide owns exact commands and security contracts.

The files under `pandrator_mcp/guides/` are packaged, versioned instructions
served to agents by the MCP server. They are not a second public documentation
site and should not be moved or copied into this directory.

## Development

- [Run Pandrator from source](development/from-source.md)
- [Contribute code or documentation](development/contributing.md)

## Documentation boundaries

This directory contains durable, public product documentation. To prevent
several almost-identical sources from drifting:

- the root [README](../README.md) is the product landing page;
- this directory owns task-oriented and conceptual product guidance;
- component READMEs own exact Manager and MCP operational contracts;
- [GitHub Releases](https://github.com/lukaszliniewicz/Pandrator/releases)
  own downloads, checksums, versions, and release notes; and
- experiments, qualification records, incident notes, and implementation
  reviews belong in issues, pull requests, release records, or other internal
  working material—not in the public documentation tree.

Documentation should prefer stable names and the latest-release page over
hard-coded version numbers and filenames. When behavior is version-specific,
say so in that version's release notes.
