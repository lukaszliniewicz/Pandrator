# Generation controls implementation and verification

Implemented the approved XML, character dictionary, casting, speech-direction
review, model conversion, and passive MCP work in the working tree. The
functional reference is [generation controls](../docs/reference/generation-controls.md).

## Delivered behavior

- Bounded XML parsing with an exact, clean transcript; stable speaker IDs,
  category-only dialogue, explicit narrator and phrase voices, inherited
  directions, emotional controls, and nonspoken events.
- Revisioned session character dictionaries with aliases, proposals, locks,
  categories, and separate casting; managed voice category editing.
- Optional document-stage dialogue/character recognition, including
  annotation-only execution and passive XML-only submissions. Authored
  controls survive subdivision and ID remapping; rewriting annotated source
  without preserving its exact transcript is rejected.
- Provider-neutral internal voice parts assembled into one logical segment
  take, with no added seam silence. Both generation entry points support
  casting. Failed/canceled parts cannot publish partial takes; cancellation
  during verification is checked again before publication.
- Timed cue envelopes remain integral. Long-form dialogue and continuation
  boundaries use the appropriate sentence/no-added-pause behavior.
- Frozen run context/cast snapshots and effective-request audio identity.
  Character notes and aliases do not invalidate sound; actual cast changes do.
- One editor state for forms and XML/legacy pSSML, dirty-state protection,
  revision-aware previews, explicit save/adopt behavior, and passive plan
  creation/review/preview/adoption through MCP.
- Mobile spacing and field widths improved at 320/390 px. One mobile header
  combines the hamburger, session title, and section selector. Drawer focus,
  backdrop, close button, and unsaved section navigation were checked.

## Checks performed

- Consolidated backend/MCP regression run: **287 passed** over 26 test files,
  using `.venv/bin/python -m pytest -p no:cacheprovider -q`. Coverage included
  markup, rendering, dictionary revisions/locks, XML optimization, provider
  request consistency, frozen plans, voice metadata, portable MCP tools,
  audio identity, regeneration, and legacy generation.
- After final corrections, focused pytest runs: **122 passed**, **17 passed**
  for structural-only annotation checks, and **38 passed** for cast generation,
  cancellation, and regeneration. These runs overlap; their counts must not be
  added to the consolidated count.
- Browser suite: **9 passed** across `generation-controls.spec.ts`,
  `contextual-performance.spec.ts`, and `session-view-improvements.spec.ts`,
  using installed Chrome. Real authenticated dictionary/voice/settings APIs
  were exercised; plan fixtures and synthesis requests were controlled.
  Mobile checks cover overflow, usable field widths, drawer focus and Escape,
  section selection, Save/Discard/Stay, and axe serious/critical violations.
- Parent visual review of desktop directions, mobile character editing at
  320/390 px, optimization settings, voice-category editing, navigation, and
  the unsaved-change dialog. UI work and visual verification were not delegated.
- `npm run check`: zero errors/warnings. `npm run build`: passed and rebuilt
  tracked static assets. OpenAPI and TypeScript API types regenerated.
- Ruff 0.16.8: new backend/MCP modules pass. Input-validation modules retain
  documented `TRY004` exceptions because their API boundary uses `ValueError`
  for 422 responses. Focused syntax/undefined-name checks and compilation pass
  for the large workflow module.
- Pyright 1.1.414: new Python modules pass with zero errors/warnings.
- Focused ESLint on rewritten/new UI, navigation, and browser tests passes.
  The broader SessionWorkspace lint run still reports three pre-existing
  findings; the workflow module retains two pre-existing unused imports.
- Eager-import scan: 394 modules, 1,038 edges, no cycle involving new modules.
  Vulture's ten reports are unused `cls` parameters on Pydantic classmethod
  validators, treated as false positives.
- `git diff --check`: passed. Unrelated pre-existing files were preserved.

## Limits and specialist use

No live provider acoustic comparison was performed. Stubbed audio verifies
request routing, composition, cancellation, and persistence, not whether a
particular model obeys an emotion or changes voices convincingly. Model
request previews explicitly distinguish applied, approximated, and unsupported
controls. Casting intentionally uses one selected service/model per run.

Native Luna/xhigh specialists performed bounded backend implementation,
research, and verification. Terra specialists mapped backend behavior and
independently reviewed the non-UI integration. The parent owned architecture,
integration, all UI work, and final inspection. No OpenCode/external model route
was used. Deployment was outside this change.
