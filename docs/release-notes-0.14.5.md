[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.5/PandratorManager-0.9.32-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.5/PandratorManager-0.9.32-x86_64.AppImage)

# Pandrator 0.14.5

**A maintenance release that keeps a cast book's speaker plan through edits, keeps long projects responsive, and makes Manager updates restart into the new version.**

- **Speaker plans survive edits.** Restoring a revision keeps its adopted speaker and delivery plan. A plan an edit made stale can be copied with only the changed blocks and their neighbours left to review, and a cast run no longer starts silently without the plan an earlier revision had, which voiced all dialogue as narration (#165, reported by Patrick Nadeau).
- **Long cast books stay responsive.** A recast preview stops when its client leaves and takes about 5 s instead of over 5 minutes on a 5,167-segment book. Starting a missing-only run, Prepare text and saving a stage selection hold the database writer for a fraction of the time, and segment listings leave out bulky revision data unless asked.
- **Generation carries on past declined lines.** When Gemini or Vertex AI refuses a line, that segment is marked failed and the rest of the run continues, ending `partial` with the failed segments listed.
- **Editing and output fixes.** Fixing a subtitle passage's wording keeps its cue timing, exports no longer wait for long jobs in other projects, language branches hard-link their media instead of copying it, dubbed videos name their audio track, and merging Japanese or Chinese blocks no longer inserts a space.
- **Manager updates restart the new version.** Restarting Pandrator through the Manager after an update now runs the newly activated version instead of the previous one.

[Detailed changes](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.14.5/docs/release-changes-0.14.5.md).

Includes **Pandrator 0.14.5**, **MCP 0.7.3**, and **Manager 0.9.32**. Use the Windows or Linux Manager download to install or update; the Windows launcher is unsigned. `SHA256SUMS` covers release assets. GitHub's source archives are intended for developers.
