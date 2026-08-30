# Pandrator 0.8.16

This quality-focused release makes subtitle correction and translation easier
to run, inspect, and reuse—especially from an MCP host without a separately
configured model API.

## Highlights

- **Passive model dispatch.** Pandrator can queue correction or translation
  batches while the model already running in Codex, OpenCode, Claude Code, or
  another MCP host performs the language work. Pandrator itself makes no model
  call in this mode.
- **Safer, leaner subtitle batches.** Stable cue IDs, typed results, sequential
  leases, bounded boundary context, and exact timing modes remove duplicated
  payloads and make retries deterministic without sacrificing editorial
  continuity.
- **Translation-aware correction.** A translated subtitle track can be
  corrected as a new translation revision while preserving its language,
  lineage, timing, and downstream voiceover role.
- **One quality contract.** Native correction/translation and passive dispatch
  now share batch sizing, context, timing, glossary, deletion, and correction
  policies. The UI exposes the relevant quality controls and hides native-model
  options when DeepL is selected.
- **July MCP compatibility.** `pandrator-mcp` 0.2.0 uses the official SDK 2.1.1,
  negotiates the final `2026-07-28` protocol through `server/discover`, retains
  the legacy handshake for maintained clients, and protects both tools and
  resources from stdout framing corruption.

## Upgrade note

Update Pandrator and `pandrator-mcp` together: MCP 0.2.0 requires Pandrator
0.8.16 or newer. Pandrator Manager remains at 0.9.17 and can install this
application update normally.

### Which file should I download?

- **Windows 10 or 11 (64-bit):** `PandratorManager-0.9.17-windows-x86_64.exe`
- **Linux desktop (64-bit):** `PandratorManager-0.9.17-x86_64.AppImage`
- **MCP-only Python install:** use `pipx install "pandrator-mcp[credential-stores,manager]"`; the release also includes `pandrator_mcp-0.2.0-py3-none-any.whl` for managed/offline packaging.

This release contains Pandrator 0.8.16, Pandrator Manager 0.9.17, and Pandrator
MCP 0.2.0. All downloadable-file hashes are collected in `SHA256SUMS`.

[See every change since Pandrator 0.8.15](https://github.com/lukaszliniewicz/Pandrator/compare/v.0.8.15...v.0.8.16)
