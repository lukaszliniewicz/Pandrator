# Voice catalog and model-driven voice lifecycle

Implementation against `17af358f6c976263eb1daa24abc410bdd9e4e6df`, following
[the approved plan](2026-09-19-voice-catalog-and-casting-plan.md).
The catalog, lifecycle, library UI, and deletion additions are implemented in the
working tree. The live Christmas Carol audio acceptance remains pending; this
document does not claim that a Scottish voice or chapter has passed listening review.

## Implemented behavior

- A shared HTTP/UI/MCP catalog combines managed references and provider speakers.
  Queries support language, accent, presentation, pitch, texture, suitability,
  collection, origin, renderer compatibility, readiness, evidence, sorting and
  bounded cursor pagination. Provider names and metadata remain scoped to their
  model; cloning registrations can appear alongside built-in speakers.
- Profiles distinguish requested, described and audition-reviewed traits. Model
  language support does not establish a voice's accent. Reviewed evidence requires
  an available audio artifact. Changing presentation alone removes obsolete
  presentation evidence. Existing voice IDs, samples and registrations remain intact.
- Revisioned collections and provider metadata overlays survive catalog refresh.
  Collections can contain managed and provider voices without copying audio.
  Migration `0048_voice_collections` adds the supporting tables.
- Capability discovery exposes known/listed/available models, modes, language
  support, licensing metadata, the speech XML guide and feature schema. Model
  filters trim nested maps without hiding known uninstalled models from an
  unfiltered discovery response.
- MCP can create profiles, request auditions, promote a selected design, import a
  managed audio artifact, transcribe/review a sample, publish a reference and
  organise collections. These operations reuse native jobs and revision checks.
  Retry keys replay accepted results before checking mutable preconditions, so a
  retired source artifact or changed voice revision does not trigger duplicate work.
- Imported draft transcripts remain drafts. Reference normalization writes a unique
  destination rather than overwriting an existing reference file.
- The library provides filters, search, collections, profile editing, sample tools,
  comparison on a common passage, design entry and the shared cast picker. Mobile
  uses a filter drawer and one-column content. Navigation preserves the query and
  scroll position, while unsaved profile changes receive a save/discard guard.
- MCP now exposes `pandrator_trash_session`, `pandrator_restore_session`, and
  `pandrator_delete_output`. Trash/restore require the current session revision;
  session listing can include trash. Output deletion targets one session artifact
  and is explicitly described as permanent. Existing native authorization and
  output-role restrictions apply. Destructive verification used disposable data only.

## Verification

Focused Python coverage passed: 70 catalog/storage/lifecycle tests, 13 backend
architecture checks, 10 frontend architecture checks, and 86 MCP/test-lane checks.
The frontend architecture assertion was corrected to permit character identity
UUIDs and explicit retry keys while retaining centralized request-key creation.
Route/job inventory expectations now include the existing speech-performance stage
and the new library endpoints.

The MCP integration test uses the actual SDK server/client and HTTP application
client against a disposable native application, including its worker. It exercises
capability discovery, catalog lookup, profile creation/retry, collections, cast
assignment, reference import, stale revisions, session trash/restore and output
deletion. Provider inventory is a fixture and the audio fixture is a short silent
WAV: this is protocol/state verification, not synthesis or acoustic verification.

```sh
.venv/bin/python -m pytest -q \
  tests/test_voice_catalog.py tests/test_voice_collections.py \
  tests/test_voice_profile_schema.py tests/test_voice_categories.py \
  tests/test_voice_lifecycle_idempotency.py tests/test_voice_reference_import.py \
  tests/test_web_voice_library.py tests/test_web_backend_architecture.py
.venv/bin/python -m pytest -q tests/test_frontend_architecture.py
.pixi/envs/default/bin/python -m pytest -q \
  tests/test_mcp_voice_catalog.py tests/test_mcp_voice_lifecycle.py \
  tests/test_mcp_voice_metadata.py tests/test_mcp_voice_integration.py \
  tests/test_mcp_server.py tests/test_mcp_architecture.py \
  tests/test_mcp_application_client.py tests/test_mcp_preview_and_generation.py \
  tests/test_test_lanes.py
```

The combined backend run initially had a stale frontend architecture assertion;
the corrected frontend suite was rerun separately and passed all 10 checks.

Browser verification passed 11 selected checks for profiles, collections, dirty
state, filters, cast assignment, mobile navigation, provider selection and existing
XTTS controls. The final mobile reference-editor adjustment was then checked with
the two affected browser cases. Browser tests use the installed Chrome channel
because the default bundled Chromium executable is absent on this host.

```sh
cd web
npm run check
npm run build
PANDRATOR_E2E_CHROME_CHANNEL=chrome npx playwright test \
  tests/voice-catalog.spec.ts tests/generation-controls.spec.ts \
  tests/workflow-generation-voice-ui.spec.ts --project=chromium \
  --grep-invert 'generation settings make source'
PANDRATOR_E2E_CHROME_CHANNEL=chrome npx playwright test \
  tests/voice-catalog.spec.ts tests/workflow-generation-voice-ui.spec.ts \
  --project=chromium --grep 'phone filters|standalone voice library'
```

Svelte reported zero errors and warnings; the production build completed and its
tracked static assets were regenerated. Parent visual inspection covered 390 px,
768 px and desktop layouts, including filters, details, profile editing, the
designer and reference tools. It caught and corrected the mobile collection
control sizing and long-name overflow in the reference editor.

Focused Ruff checks passed. Pyright reported zero errors/warnings for the new
feature modules. An AST import check found no cycles involving the eight new
runtime modules; Vulture at 90% found only unused validator `cls` arguments.
`workflow_handlers.py` retains 16 Ruff findings identical to the baseline by
code/message; none were introduced by this patch. `git diff --check` passed.

## Remaining acceptance and limits

- One older browser case, “generation settings make source, availability, voice
  language, and reuse choices visible,” still expects the former “Generate from:
  Translation v3” workflow. It fails before reaching the changed library surface
  and was excluded from the passing browser subset. The complete browser suite is
  therefore not claimed green.
- The configured live MCP was still serving the older tool inventory. A refreshed
  app/MCP and an available voice-design model are needed for the live chapter test.
  The inspected live local inventory listed Qwen Base; this is not a VoiceDesign
  model. No production restart, model installation or chapter synthesis occurred.
- Follow the approved plan's bounded Stave I protocol after refreshing the live
  services: discover capabilities, audition/design Scrooge, establish stable cast,
  annotate passively, compile the short exchange, generate, listen and export.
  Report exact-text/cast checks separately from accent, identity and audio quality.
- The one-renderer-per-run boundary and deferral of environmental sound design
  remain in place. This patch does not implement a sound-effects mixing system.
- Initial verification finished before committing or updating the running app.
  The user subsequently authorized a commit to `main` and a local application
  update. Unrelated untracked workspace files remain outside the feature commit.

## Specialists used

- `deep-researcher` — GPT-5.6 Terra/high: existing non-UI backend map.
- `researcher` — GPT-5.6 Luna/xhigh: official model capability evidence and native
  deletion/API anchors.
- `implementer` — GPT-5.6 Luna/xhigh: exact non-UI schema/storage, lifecycle,
  MCP/client, and SDK-to-native integration-test packets.
- `reviewer` — GPT-5.6 Terra/xhigh: independent non-UI lifecycle audit; its retry
  finding was corrected and covered by regression tests.

The parent owned the architecture, integration, all UI changes, browser tests,
visual inspection and final audit. No external OpenCode/provider route was used.
