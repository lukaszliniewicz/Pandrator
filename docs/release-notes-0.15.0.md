[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.15.0/PandratorManager-0.9.32-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.15.0/PandratorManager-0.9.32-x86_64.AppImage)

# Pandrator 0.15.0

**A release shaped by a real job: a 70-minute webinar edited, subtitled and dubbed into four languages through MCP. Dubbed exports keep the original soundtrack, speech stays closer to the picture, audio is reused instead of regenerated, and the queue says what it is waiting for.**

- **Dubs keep the original sound.** New voiceover sessions and language branches mix the voice over the original soundtrack when there is one, and an export plan warns before it would replace it.
- **Speech stays in sync.** Silence that providers pad around short takes is trimmed before fitting, the default speed-up is 1.15×, and export plans report how far speech lags the picture before anything renders. Timing repair works on any finished voiceover.
- **Less regeneration.** Takes are no longer marked stale after every rebuild of a corrected or translated project, a timing-only subtitle change keeps the speech plan, and takes are found in older plan revisions too.
- **Cleaner subtitles.** Transcription trims overlapping cue ends, except where a cue cannot keep the minimum duration and stays overlapping, Parakeet's glued numbers ("for16") get their space back, and export diagnostics name the affected cues.
- **A queue you can read.** Waiting work says why and behind what, retries show at any concurrency, generation estimates the time left, and subtitle evidence no longer waits for a whole render.
- **MCP for long jobs.** Waits are capped at 25 seconds, responses are compact by default, translation projects are created as durable work, sources can be imported from a URL, and the guidance covers the multilingual route end to end.
- **Speaker plans survive more edits.** In a speaker plan made with 0.15, an edit now affects only the blocks it touches (#167, Patrick Nadeau).

Some defaults change: read the upgrade notes in the [detailed changes](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.15.0/docs/release-changes-0.15.0.md) before updating scripted MCP workflows.

Includes **Pandrator 0.15.0**, **MCP 0.8.0**, and **Manager 0.9.32**. Use the Windows or Linux Manager download to install or update; the Windows launcher is unsigned. `SHA256SUMS` covers release assets. GitHub's source archives are intended for developers.
