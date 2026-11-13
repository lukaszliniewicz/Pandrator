[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.4/PandratorManager-0.9.31-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.14.4/PandratorManager-0.9.31-x86_64.AppImage)

# Pandrator 0.14.4

**A maintenance release that keeps long, cast projects fast and stops heavy work from stalling the server.**

- **Long cast plans are much faster.** Segment listings, plan status, revision history and generation start no longer rebuild the voice catalogue for every cast span, a listing works out audio reuse only for its own page, and the run history polled during generation no longer decodes every run's settings. On a 5,167-segment cast book, a segment listing that took over four minutes now answers in seconds.
- **The server stays responsive under load.** Reads whose client has given up stop early. Routine requests, status polls and static files no longer wait for SQLite's single writer, and slow WAL checkpoints run in the background instead of inside a request. Generation start, regrouping and repairs prepare their audio identities outside the writer, and the WAL shrinks back after heavy load.
- **MCP plan tools answer in time.** Plan status and revision history read summaries and check audio reuse for the active revision only.
- **Interface fixes.** Quick Transcribe keeps the options you change while its saved defaults load, and retrying a failed upload cannot start a second job. Voice settings panels show a loading state and offer Retry if loading fails, and saving a project's settings as workspace defaults no longer drops other models' tuning. Reading-view actions stay open while the pointer moves up to them, and buttons no longer shift under a click while a page finishes loading.

[Detailed changes](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.14.4/docs/release-changes-0.14.4.md).

Includes **Pandrator 0.14.4**, **MCP 0.7.2**, and **Manager 0.9.31** (unchanged). Use the Windows or Linux Manager download to install or update; the Windows launcher is unsigned. `SHA256SUMS` covers release assets. GitHub's source archives are intended for developers.
