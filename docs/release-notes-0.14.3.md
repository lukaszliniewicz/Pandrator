# Pandrator 0.14.3

**A maintenance release improving generation reliability, cross-platform builds and Windows test setup.**

- **More reliable long-running generation.** Generation identity checks no longer hold a SQLite writer lock or stop job heartbeats. Identity checks use bounded database queries even for long projects, while preserving per-segment authorisation. Cancellation remains effective if it arrives near job completion.
- **More predictable browser builds.** Browser source and generated text assets now have consistent LF line endings on Windows and Linux. The build checks detect mixed and CRLF endings, including in generated assets, before publication.
- **Safer Windows tests.** Pytest uses a short, private temporary directory when available, with ownership and ACL checks, cross-process initialisation locking and safe cleanup. Local fallback to the normal pytest directory remains supported.
- **Additional stability fixes.** ElevenLabs streaming timeouts are clamped to valid bounds, and language-scoped workflow readiness waits for the initial settings load.

[Detailed changes](release-changes-0.14.3.md).

Includes **Pandrator 0.14.3**, **Manager 0.9.31** (unchanged), and **MCP 0.7.1** (unchanged). Use the Windows or Linux Manager download to install or update; the Windows launcher is unsigned. `SHA256SUMS` covers release assets. GitHub's source archives are intended for developers.
