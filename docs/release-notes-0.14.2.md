[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.2/PandratorManager-0.9.31-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.2/PandratorManager-0.9.31-x86_64.AppImage)

**Pandrator 0.14.2 makes subtitle timing problems easier to find, checks local transcription files before a run, and hardens media edits, narration preparation and background jobs.**

- **Find timing problems before export.** Subtitle and recording review now flag overlapping cues, overlapping word times and word times that run backwards. The warnings point to affected cues without changing text or timestamps; simultaneous speakers can be intentional, so listen before editing.
- **Check transcription files first.** A new **Local model files** panel checks the selected recognizer, speech detector, word aligner and language detector separately. Administrators can reuse an existing local VAD file. The check never downloads anything.
- **Keep captions intact.** Media-edit rendering preserves whole captions when word timing is incomplete, refuses cuts that would split a caption, and verifies the composed subtitle text against the retained source before publishing.
- **Preview the edit you will export.** The 12-second mix preview now follows the reviewed media-edit timeline, and exports require that exact current render.
- **Refresh narration length automatically.** Generation checks the effective narration block length before previewing. Untouched automatic plans are resegmented when the target changes; edited plans keep their boundaries. Breeze and VoxCPM2 use shorter automatic targets.
- **More reliable automation and long jobs.** Translation branch creation is atomic and idempotent, job logs are bounded and redacted, and completed speech assemblies survive later progress failures.

[Detailed changes and guide links](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.14.2/docs/release-changes-0.14.2.md).

Includes **Pandrator 0.14.2**, **Manager 0.9.31** (unchanged), and **MCP 0.7.1**.

Use the Windows or Linux button to install or update through Manager. Model downloads are separate; refreshed weights may require downloading again. The Windows launcher is unsigned. See the [installation guide](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.14.2/docs/getting-started/installation.md) for help. `SHA256SUMS` covers the release assets. GitHub's **Source code** archives are for developers.
