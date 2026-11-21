[![Download for Windows (.exe)](https://img.shields.io/badge/Download_for_Windows-.exe-2563eb?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.15.1/PandratorManager-0.9.32-windows-x86_64.exe) [![Download for Linux (.AppImage)](https://img.shields.io/badge/Download_for_Linux-.AppImage-168572?style=for-the-badge)](https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.15.1/PandratorManager-0.9.32-x86_64.AppImage)

# Pandrator 0.15.1

**A maintenance release that closes more of the ways a cast project could be voiced without its speaker plan, speeds up cast comparisons on long books, and fixes the security event log and export names on Windows.**

- **Cast runs keep their speaker plan.** The workflow's audio stage, its preflight and recast now refuse a revision that lost its adopted speaker plan when a spoken block would fall back to the narrator, and name those blocks. An explicit timing repair is refused for a voiceover made with casting or performance directions, as the automatic pass already was (#169, #170).
- **Faster cast comparisons on long books.** A recast preview works out each cast assignment once, about 8 s instead of 16 s on a 5,167-segment book, and the Cast panel sends one comparison after a burst of changes instead of one per change (#173, #174).
- **Windows fixes.** A security event log set to a file reported itself on while dropping every event. It now stays off there and says so at startup, pointing to `stdout` (#171). Export file names taken from long project names are capped (#172).
- **MCP says why a workflow needs preparation.** A plan that needs its speech plan prepared now answers with that reason instead of a generic "resource changed" conflict.
- **Updated dependencies.** Fourteen locked packages with published security advisories are updated, among them aiohttp, urllib3, Werkzeug and PyJWT.

Thanks to Patrick Nadeau (@ValtorX), who reported the four issues behind this release and wrote the speaker plan guards and both cast comparison changes.

[Detailed changes](https://github.com/lukaszliniewicz/Pandrator/blob/v.0.15.1/docs/release-changes-0.15.1.md).

Includes **Pandrator 0.15.1**, **MCP 0.8.1**, and **Manager 0.9.32**. Use the Windows or Linux Manager download to install or update; the Windows launcher is unsigned. `SHA256SUMS` covers release assets. GitHub's source archives are intended for developers.
