[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.13.0/PandratorManager-0.9.30-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.13.0/PandratorManager-0.9.30-x86_64.AppImage)

**Pandrator 0.13.0 improves audiobook preparation, subtitle exports, and project organization.**

- **Audiobook pacing.** Short dialogue exchanges can stay together, long sentences split more carefully, and pauses are applied during assembly rather than added to individual recordings. AI dialogue annotation remains optional.
- **More control over EPUBs.** Choose how tables and illustration text are read. Extraction also handles footnotes, ruby readings, and paragraph boundaries more carefully.
- **Subtitle editing.** Display-only edits preserve recordings when the spoken text stays the same. Japanese and Chinese captions use better word and punctuation boundaries, and subtitle comparisons keep your output selection intact.
- **Project folders and language versions.** Keep translations together as one project and organize projects into virtual folders.
- **Local speech options.** audio.cpp moves to 0.9.2, with French PocketTTS, Voxtral TTS, and clearer preset-voice choices. Kokoro can install optional Japanese support; the dictionaries require an extra 502 MiB download. Voxtral's weights are licensed for noncommercial use.
- **Clearer generation recovery.** Failed generation offers continuation choices, while background activity distinguishes running and queued jobs. Voice controls also avoid unnecessary cloud-credential probes.

**Caption export default:** New exports use the current display text. Choose **Historical text from selected audio version** when you need the wording saved with an older recording. Existing recordings and audio history remain available.

[Detailed changes and guide links](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.13.0/docs/release-changes-0.13.0.md).

Includes **Pandrator 0.13.0**, **Manager 0.9.30**, and **MCP 0.6.3**.

Use the Windows or Linux download above to install or update through Pandrator Manager. Model downloads are separate. The Windows launcher is unsigned; see the [installation guide](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.13.0/docs/getting-started/installation.md) for help. `SHA256SUMS` covers the release assets. GitHub's **Source code** archives are for developers.
