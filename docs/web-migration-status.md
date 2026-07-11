# Web migration implementation status

This checklist is the cutover record for `codex/webui-migration`. A checked item is backed by implementation and automated tests in this repository; release qualification that needs another operating system, hardware, credentials, or signed production artifacts remains an explicit gate rather than being inferred from local tests.

## Complete

- [x] Injected `DataPaths`/runtime paths and read-only-package-safe static asset loading.
- [x] Flask application factory, Waitress entry point, same-origin versioned API, OpenAPI document, and generated TypeScript contract.
- [x] SQLite/SQLAlchemy repositories, initial forward importer, immutable subtitle revisions, artifact dependency edges, job leases/events, and session bundles.
- [x] Local bootstrap authentication, remote owner login/API tokens, CSRF checks, host validation, proxy configuration, upload containment, and range responses.
- [x] Svelte 5/Tailwind 4 editorial shell, startup task launcher, docked setup return, provider/model editor, voice recording/transcription, guided dubbing cards, subtitle comparison/review, and PDF stack editor.
- [x] Independent transcription, correction, translation, TTS, export, deterministic source extraction, voice transcription/normalization, PDF edit, and bundle jobs.
- [x] PDF original-page geometry, all/left/right/single stacks, first-page side, crop, whiteout, deletion, opacity, undo/redo, and derived-artifact provenance.
- [x] Basic installer lifecycle commands and API/worker supervision.

## Completed in the parity-completion checkpoint

- [x] Agentic source cleaning resolves database-backed providers/models, runs the multi-phase worker pipeline, preserves the audit report, and records normalized cached-token usage.
- [x] RVC upload/conversion and XTTS training have authoritative training records, durable jobs, API/CLI surfaces, artifacts, capability-aware web controls, and cancellation.
- [x] The operational CLI supports authenticated API-client mode and exposes RVC/training commands in addition to the existing session, workflow, job, artifact, provider, voice, and export groups.
- [x] Signed live update verifies Ed25519 metadata, blocks intake, drains/cancels work, snapshots package plus SQLite, migrates, health-checks, restores on failure, and restarts supervision.
- [x] The installer is independently buildable as a headless distribution with optional Qt GUI/build extras; the Qt launcher starts the web supervisor and implements tray close behavior.
- [x] Restartable guided tours cover workspace, dubbing/export, model configuration, voice library, subtitle review, and PDF preprocessing.
- [x] Playwright runs authenticated Chromium and Firefox flows for wizard/session creation, setup return, keyboard focus, tours, and theme state.
- [x] Cross-browser visual baselines are committed for the editorial workspace.
- [x] A repository-scoped threat model was generated for remote-access qualification, with maintenance, path-containment, and update-boundary tests added.
- [x] Preview CI builds both wheels on Windows/Linux, checks generated contracts, builds the SPA, runs Python tests, and executes Chromium/Firefox workflows.
- [x] Media/SRT workflows are source-aware and renumber applicable stages; continuation progress, failures, cancellation, locked prerequisites, reusable sources, URL downloads, and detailed outcome presets are surfaced in the browser.
- [x] Selected TTS/RVC child services run as owned `ProcessSupervisor` children with health checks and ordered shutdown instead of remaining launcher-owned background processes.

## Remaining release-infrastructure gate

- [ ] Provision release-CI key custody/key rotation and production manifest signing. Verification and rollback are implemented; the repository intentionally contains no private release key.

## External release gates

- [ ] Windows clean-install and legacy-migration qualification.
- [ ] Linux clean-install, systemd/headless, AppImage, and Caddy-proxied remote qualification.
- [ ] Representative STT/TTS/RVC/XTTS and OpenAI-compatible local endpoint smoke tests on suitable hardware.
- [ ] Credentialed provider-family qualification and signed production update/rollback exercise.
- [ ] Final Qt tag and `qt-maintenance` branch, followed by removal of Qt application code from the shipping Pandrator wheel.

The migration must not merge into `main` while any implementation gap is open. External gates can only be signed off in their required environments; they cannot be replaced by mocks.
