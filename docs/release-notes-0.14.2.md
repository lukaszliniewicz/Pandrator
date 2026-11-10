[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.2/PandratorManager-0.9.31-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.2/PandratorManager-0.9.31-x86_64.AppImage)

**Pandrator 0.14.2 makes timing problems easier to find and repair, helps check local transcription files before a run, and hardens media edits, narration preparation and long jobs.**

- **Repair an early finish without redoing the run.** Optional early-finish repair now works for passage-first voiceovers and can use semantic context. Each repair splits one early block at a whole-cue boundary, reuses unaffected takes, and refreshes only neighbours whose context changed. A refreshed neighbour that grows, or a split that adds delay, is rejected, so the original audio stays active. One automatic post-generation pass runs, and repair takes precedence over passage regroup.
- **See what a repair actually did.** History distinguishes a completed pass, a partial pass, all attempts rejected, no split needed, and a skip that checked nothing, with checked, candidate and applied counts. Passage regroup reports its own group counts, and a regroup that stopped early reads as partial, not completed. Failed, canceled, stopped, partial or skipped passes can be retried while the root is still active; cancelling a repair closes its unfinished work.
- **Find timing problems before export.** Subtitle review flags overlapping cue times. The recording editor also flags overlapping word times, overlapping captions and word starts that run backwards. Warnings never rewrite text or timestamps, and simultaneous speakers can be intentional.
- **Keep captions intact.** Media-edit rendering preserves whole captions that lack complete word timing, refuses cuts inside those cues, and checks the composed subtitle text before publishing. Edit plans now report per-cue timing flags.
- **Preview the edit you will export.** The **Preview 12 seconds** preview follows the reviewed media-edit timeline, and exports require that exact render.
- **Check transcription files first.** A new **Local model files** panel reports the selected recognizer, speech detector and, where they apply, the word aligner and language detector. Administrators can reuse an existing speech-detector (VAD) file. The panel downloads nothing.
- **Refresh narration length automatically.** Generation checks the effective narration length before previewing. Untouched automatic plans are resegmented when the target changes; edited plans keep their boundaries. Breeze and VoxCPM2 use shorter automatic targets.
- **More reliable automation and long jobs.** Translation branch creation is atomic and idempotent. Job logs are bounded and redacted, and completed speech assemblies survive later progress failures.
- **Cheaper assembly authorization.** Output assembly now uses constant-size database authorization checks per job, however many segments it renders, and still proves current access before publishing.
- **Easier mobile controls.** On phone-sized screens, playback and Generate controls keep at least a 44-pixel touch target even when the browser uses smaller text scaling.

[Detailed changes and guide links](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.14.2/docs/release-changes-0.14.2.md).

Includes **Pandrator 0.14.2**, **Manager 0.9.31** (unchanged), and **MCP 0.7.1**.

Use the Windows or Linux button to install or update through Manager. Model downloads are separate. The Windows launcher is unsigned. See the [installation guide](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.14.2/docs/getting-started/installation.md) for help. `SHA256SUMS` covers the release assets. GitHub's **Source code** archives are for developers.
