# Pandrator 0.10.0

[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.10.0/PandratorManager-0.9.25-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.10.0/PandratorManager-0.9.25-x86_64.AppImage)

**Pandrator 0.10.0 brings a richer voice catalogue, multi-voice generation, and detailed control over speech delivery.** Find suitable voices, assign them to narrators and characters, and direct individual passages with instructions, emotions, and supported vocalizations.

Download the Windows `.exe` or Linux `.AppImage` above to install or update through Pandrator Manager. GitHub’s **Source code** archives are for developers.

Includes **Pandrator 0.10.0**, **Manager 0.9.25**, and **MCP 0.5.0**. The audio.cpp runtime remains **0.8.1**.

**Explore the expanded Voice Library**

- Browse saved voices and provider catalogues, with profiles describing available voice characteristics.
- Search and filter by language, accent, presentation, pitch, texture, perceived age, delivery, intended use, tags, and compatible services or models.
- Listen to samples and compare voices before choosing your cast.
- Organize voices into collections, manage reference recordings, and keep descriptions, provider links, and provenance together. Voice design workflows let you create and save new voices with supported models.

**Generate narration and dialogue with multiple voices**

- Assign voices to a narrator, named characters, or source speakers, and reuse those assignments throughout a project.
- Use different voices within the same speech segment, with overrides for an entire segment or a selected phrase. Pandrator renders the voice parts and assembles them into the resulting segment.
- Review casting and preview each part’s synthesis request before generating audio.
- Generation runs retain their chosen voices and settings, so resuming a run preserves the original cast and completed work.

**Direct how the speech is performed**

- Add general instructions or direct individual segments and phrases with emotion, pace, cadence, emphasis, and supported vocal events.
- Prepare directions manually or use optional contextual performance analysis, then review, edit, and lock the choices you want to keep.
- Preview how directions will reach the selected model, including controls that are approximated or unsupported. Available controls depend on the model and backend.
- **Gemini TTS:** automatically include preceding text, or both preceding and following text, alongside your instructions. Regenerating an individual block retains its surrounding context.
- **ElevenLabs:** Eleven v3 supports inline performance directions and mapped vocal events such as laughter, sighs, and throat clearing. Supported earlier models use native previous/next-text stitching. Native voice settings are validated and included in generation previews.

**Choose models and automate setup**

The unified model catalogue now exposes languages, licences, reference requirements, instruction scope, context support, and availability. Expanded audio.cpp discovery and grouped model selection make variants easier to compare.

MCP-connected assistants can browse voices and model capabilities, configure speech directions and context, manage casting, and preview or apply speech edits. Provider switching keeps the model and voice selection consistent. Update to **MCP 0.5.0** and reconnect your clients to discover the new tools and parameters.

**Other improvements**

- Qwen3 transcription and forced alignment through audio.cpp, vocal isolation in Quick Transcription, and improved CJK and native-script subtitle processing.
- Better audiobook chunking, generation recovery, missing-only generation, history browsing, and performance in large sessions.
- A clearer session workflow, improved casting controls and mobile navigation, and collapsible completed stages.
- More reliable plan review, settings saves, export progress, and Manager service recovery.
- When generated audio outlasts a video, export can extend the final frame for the full required duration.

**Upgrading**

Install Manager **0.9.25**, then update Pandrator. Existing workspace data and generation history are retained. Keep a backup of valuable projects before upgrading.

Learn more: [Speech performance](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/docs/speech-performance.md) · [Generation and casting controls](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/docs/reference/generation-controls.md) · [MCP guide](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.10.0/pandrator_mcp/README.md)
