# Dependency review, 8 September 2026

This inventory compares tracked manifests and locks with primary registry and
release metadata. It does not claim that a newer version has passed Pandrator's
compatibility checks. No dependency pins were changed by this review.

## Native runtimes

- **audio.cpp: 0.7.2 to 0.7.3 is worth qualifying.** The release includes
  Chatterbox multilingual normalization fixes, Qwen prefill changes, PocketTTS
  streaming, Breeze streaming changes, and new version reporting. It also adds
  models that Pandrator does not yet expose. Updating requires replacement of
  every platform asset URL and digest, plus rebuilding the custom Linux CUDA
  archive. Keep the current pin until those artifacts and adapter contracts
  have been checked together. Qwen3 VoiceDesign already works at the source
  contract level in 0.7.2; it does not require this upgrade.
  [Upstream release](https://github.com/0xShug0/audio.cpp/releases/tag/v0.7.3)
- **CrispASR 0.8.32 is current.** Its published platform assets cover the
  platforms declared by Pandrator.
  [Upstream release](https://github.com/CrispStrobe/CrispASR/releases/tag/v0.8.32)
- **Python:** the app supports 3.11 and 3.12. Pixi requests 3.11 and locks
  3.11.15; investigate the 3.11.16 patch update without changing the supported
  Python range. Moving the managed environment to 3.12 is a separate dependency
  compatibility change. Do not jump to 3.13 or 3.14 while audioop-era dependencies
  remain in use. [Python downloads](https://www.python.org/downloads/)
- **Node 24.18.0 is current within the declared Node 24 line.** Do not switch
  to Node 26 merely because it is the newest major.
  [Node release](https://github.com/nodejs/node/releases/tag/v24.18.0)
- The custom CUDA build pins its toolchain and container. Preserve that
  reproducibility while qualifying a new runtime. CMake's available 4.x line
  is a major change from the pinned 3.31.6, not a routine lock refresh.

## Python packages

Useful candidates allowed by current ranges include SQLAlchemy 2.0.52,
Pydantic 2.13.5, Alembic 1.19.2, google-auth 2.57.1, regex 2026.9.3,
yt-dlp 2026.8.19, DeepL 1.32.0, Authlib 1.8.0, and ONNX Runtime 1.29.0.
The first group needs focused storage/schema tests; provider SDKs need their
adapter tests; ONNX Runtime needs model-loading compatibility checks. A newer
version number alone is not evidence of a security fix.

Several upgrades cross intentional constraints: cryptography 45.x to 50.x,
LiteLLM 1.91.x to 1.100.0, MCP 2.1.1 to 2.2.0, NumPy 1.26.4 to 2.5.x,
Dulwich 0.25.x to 1.x, and Hatchling's upper bound. Review those bounds before
changing them. NumPy 2 also changes compatibility expectations for native and
speech-model dependencies, and the latest NumPy requires Python 3.12.

Packaging tools can be considered separately: PyInstaller 6.20.0 to 6.22.2,
Ruff 0.16.0 to 0.16.6, and build 1.5.0 to 1.6.0.

The inventory found a possible manifest/lock inconsistency: a Pixi packaging
constraint below 26 alongside a 26.2 lock entry. Check the exact environment
and feature resolution before the next lock regeneration; the presence of an
entry in a multi-environment lock does not alone prove that every environment
uses it.

Primary registry examples: [SQLAlchemy](https://pypi.org/project/SQLAlchemy/),
[Pydantic](https://pypi.org/project/pydantic/),
[cryptography](https://pypi.org/project/cryptography/),
[LiteLLM](https://pypi.org/project/litellm/),
[MCP](https://pypi.org/project/mcp/).

## JavaScript packages

A focused refresh within current ranges can cover SvelteKit 2.70.3,
Svelte 5.57.0, Tailwind 4.3.3, svelte-check 4.7.6, ESLint 10.10.0,
eslint-plugin-svelte 3.23.0, typescript-eslint 8.70.0, globals 17.12.0,
Knip 6.35.0, Playwright 1.63.0, and axe-core/Playwright 4.13.0.
Re-run frontend checks and browser coverage after the lock update. Playwright
also changes browser binaries, so it should be coordinated with CI caches.

Keep major migrations separate: Vite 7 to 8, vite-plugin-svelte 6 to 7,
TypeScript 5 to 7, and Lucide 0.x to 1.x. The exact PDF.js pin also warrants
PDF rendering and worker-loading checks before moving from 6.2.108 to 6.3.289.

The inventory used npm registry metadata and `npm outdated --json --prefix web`.
[Command documentation](https://docs.npmjs.com/cli/commands/npm-outdated)

## Validation boundary

A Terra/high researcher gathered the manifest, lock, and upstream inventory;
the parent selected the compatibility priorities above. No untracked uv.lock
or user data was used. This was an update assessment, not a vulnerability scan
or certification of the proposed versions.

No validation should run hardware-encoding smoke tests. The automatic VA-API
test was removed from capability discovery after a GPU fault. Use mocked
hardware metadata and CPU tests; actual GPU exports remain explicit user jobs.
