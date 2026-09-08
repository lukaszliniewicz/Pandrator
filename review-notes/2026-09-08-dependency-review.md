# Dependency review, 8 September 2026

This inventory compares tracked manifests and locks with primary registry and
release metadata. It does not claim that a newer version has passed Pandrator's
compatibility checks. The initial inventory made no pin changes; the release
qualification below records the subsequently authorized updates.

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


## Pandrator 0.9.1 dependency qualification

The release keeps audio.cpp 0.7.2, CrispASR 0.8.32, Python 3.11.15,
NumPy 1.26.4, ONNX Runtime 1.28.0, and the existing JavaScript lock.
Native model-runtime and major JavaScript migrations remain separate work.
Cryptography remains within the existing 43 through 45 range.

Selected updates:

| Dependency | Qualified version |
| --- | --- |
| LiteLLM | 1.100.0 |
| yt-dlp | 2026.8.19 |
| MCP SDK / mcp-types | 2.2.0 |
| Pixi | 0.80.0 |
| PyInstaller | 6.22.2 |
| Hatchling | 1.32.0 |
| build | 1.6.0 |
| packaging | 26.3 |
| Ruff | 0.16.6 |
| SQLAlchemy | 2.0.52 |
| Pydantic | 2.13.5 |
| Alembic | 1.19.2 |
| Authlib | 1.8.0 |
| google-auth | 2.57.1 |
| regex | 2026.9.3 |
| DeepL | 1.32.0 |
| PyMuPDF | 1.28.2 |

Pixi 0.80.0 regenerated the version-7 lock for Windows and Linux across
all four environments. The solve used an isolated source copy because
editable Python packages require a solve environment; `--no-install` cannot
resolve those source dependencies. The user's development environment and
untracked `uv.lock` were preserved. Pixi bootstrap versions and verified
archive digests were updated in the Manager, installer, workspace, and CI.
[Official Pixi release](https://github.com/prefix-dev/pixi/releases/tag/v0.80.0).

The existing SDK 2.1.1 already supports protocol `2026-07-28`; SDK 2.2.0
adds transport and schema fixes. Modern discovery and maintained legacy
initialization both remain supported. LiteLLM's base installation resolves
with MCP 2.2.0; LiteLLM's optional proxy/MCP extras are not installed.
[Official MCP SDK release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0).

Local qualification used a disposable Python 3.11.15 environment:

- All 137 installed packages passed `uv pip check`.
- Focused provider, storage, transcription, credential, settings, and manifest
  tests: 147 passed.
- Manager suite: 282 passed, 4 skipped. After the Pixi pin update, Manager core,
  installer architecture, and test-lane checks: 132 passed.
- Actual LiteLLM completion and usage parsing against a loopback-only fake
  OpenAI endpoint passed. Actual yt-dlp download of a synthetic WAV from
  loopback matched the source bytes.
- MCP suite: 155 passed, including modern HTTP/stdio discovery and legacy
  initialization.
- The locked Linux runtime passed manifest tests, CLI startup, and both
  loopback smoke tests.
- Ruff, targeted mypy, high-confidence Vulture, and a bootstrap dependency
  cycle check passed.
- Svelte check passed with zero errors or warnings; production frontend
  build passed. No JavaScript dependency updates were included.
- PyInstaller 6.22.2 built the native Manager executable and its self-check
  passed. This exposed and fixed a packaging defect: the consolidated
  provider policy JSON must be copied from the validated wheel into the
  frozen executable.

Luna/xhigh researchers checked official MCP and Pixi/build-tool evidence;
a Luna/xhigh verifier performed the focused dependency and loopback tests.
The parent selected updates, audited changes, and owns release validation.
No GPU encoding, local model inference, external media downloads, or paid
provider calls were used for this qualification.
