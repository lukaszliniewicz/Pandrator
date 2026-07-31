# Frontend review, implementation plan, and results

## Scope and evidence

This review covers `web/src`, the frontend test suite, the generated browser
artifact boundary, the standalone recovery-manager browser shell, and the
frontend architecture checks.

Baseline evidence:

- `svelte-check` fails because the Playwright sources use Node APIs without
  `@types/node`.
- The Chromium suite has one functional failure: output export can skip a
  required assembly rebuild when inherited `export_mode`/`audio_mode` values
  are absent from the saved response.
- The production build did not complete within the initial two-minute
  diagnostic window.
- Madge reports no circular dependencies across the 76 frontend source files.
- Knip reports two unnecessarily exported runtime values and 17 unnecessarily
  exported types.
- The largest Svelte coordinators are:
  - `SessionWorkspace.svelte`: 919 lines, 780 script lines, 89 state runes,
    37 functions, and eager settings-catalogue requests.
  - `GenerationDrawer.svelte`: 906 lines, 702 script lines, and 43 functions.
  - `LocalComponentsPanel.svelte`: 980 lines, including duplicated manager
    operation state/polling concerns and a large confirmation dialog.
- `pandrator_manager/recovery_ui/static/app.js` is a separate, zero-build
  2,589-line browser application. Its imperative rendering is appropriate for
  the recovery boundary, but transport, refresh scheduling, and view rendering
  currently share one module.
- Modal surfaces generally declare `role="dialog"` but do not consistently
  trap focus, restore focus, or close with Escape. A few dialogs or icon-only
  close buttons also lack an accessible name.
- Non-generated TypeScript still contains explicit `any` escape hatches in
  shared settings/voice utilities and browser tests.
- Error normalization (`caught instanceof Error ? ...`) is duplicated across
  the frontend.
- The focused source graph confirms that `api-models`, `domain-api`,
  `SessionWorkspace`, and `GenerationDrawer` are the major dependency hubs.
  The graph is only corroborating evidence: its extractor reports 70 dangling
  endpoints and four directed same-endpoint edge collapses for Svelte imports.

## Implementation workstreams

### 1. Reproducible quality gates

- Add Node typings so `svelte-check` covers Playwright sources.
- Add ESLint with TypeScript and Svelte rules.
- Add Prettier with the Svelte plugin and format the maintained frontend
  sources/configuration.
- Add Knip as a repository script with generated-code exclusions.
- Add Axe-backed Playwright accessibility coverage.
- Provide one `quality` command that runs formatting, lint, type/Svelte
  checking, and dead-code analysis.

Acceptance:

- `npm run quality` passes with no ignored maintained source.
- Generated OpenAPI code remains excluded from stylistic/dead-export checks.

### 2. TypeScript and JavaScript correctness

- Replace explicit `any` types with `unknown` plus narrow record types.
- Import Node globals explicitly in browser tests.
- Remove exports that are private implementation details and remove obsolete
  API re-exports.
- Centralize unknown-error-to-message conversion.
- Fix output assembly decision defaults so a generated-audio export cannot
  silently bypass assembly.
- Serialize recovery-manager refresh requests so a user action cannot be
  overwritten by a stale poll already in flight.

Acceptance:

- No explicit `any` remains outside generated code or literal HTML
  `step="any"` values.
- Knip reports no actionable unused exports.
- The previously failing output browser test passes.
- A runtime stop/start action is immediately reconciled even when it overlaps
  the recovery shell's background refresh.

### 3. Svelte decomposition and state ownership

- Remove stage-settings catalogues and modal components from the ordinary
  `SessionWorkspace` startup path. Load each catalogue only for the stages
  that use it, and dynamically import the large secondary settings surfaces.
- Extract generation playlist/audio lifecycle into a dedicated reactive
  controller with deterministic disposal.
- Extract the manager plan confirmation surface from `LocalComponentsPanel`.
- Move manager operation state, polling, terminal-state rules, persistence,
  cancellation, and reference-counted lifecycle into one shared reactive
  store used by both manager views.
- Preserve the documented dependency direction: feature stores/controllers
  own transport and server state; presentation components receive typed state
  and callbacks.

Acceptance:

- Ordinary workspace entry does not load capabilities, provider models, TTS
  catalogues, voice libraries, or secondary settings-modal bundles.
- `GenerationDrawer` no longer owns raw `Audio` lifecycle/token bookkeeping.
- Manager operation types and terminal-state rules have one definition.

### 4. Accessibility

- Add a reusable modal-focus action that:
  - moves focus into a dialog,
  - traps Tab/Shift+Tab,
  - closes on Escape when permitted,
  - restores focus to the opener on destruction.
- Apply it to maintained modal/dialog surfaces.
- Add missing dialog labels and icon-button names.
- Ensure status/error/progress UI uses appropriate live-region semantics.
- Add browser tests for automated WCAG A/AA checks and modal keyboard behavior.

Acceptance:

- Axe finds no serious/critical WCAG A/AA violations on the covered core
  surfaces.
- A keyboard-only user can open, traverse, escape, and return from dialogs.

### 5. Performance and lifecycle

- Lazy-load secondary settings, TTS service, and voice-library modal bundles.
- Remove duplicate eager settings-catalogue loads from workspace mount.
- Centralize/coordinate manager operation observation so the global banner and
  local-manager view do not independently poll the same operation.
- Dispose long-lived audio, timer, subscription, and polling work when feature
  components unmount.
- Compare production chunk output before and after the split.

Acceptance:

- Normal workspace entry does not request TTS catalogues, voice libraries, or
  provider models until stage settings need them.
- Only one manager-operation polling loop is active per browser document.
- Timers, subscriptions, polling, and audio objects touched by this change have
  explicit cleanup.

### 6. Regression verification

Run and resolve failures from:

1. `npm run quality`
2. `npm run build`
3. `python -m pytest -q tests/test_frontend_architecture.py`
4. targeted output, workspace, manager, and accessibility Playwright tests
5. the full Chromium and Firefox Playwright projects
6. final Knip and circular-dependency scans

The implementation is complete only when the maintained frontend passes these
gates or a remaining environmental limitation is documented with exact
evidence.

## Implemented changes

### Quality and dependency controls

- Added ESLint flat configuration for TypeScript and Svelte, Prettier with the
  Svelte plugin, Knip, Axe-backed Playwright coverage, Node typings, and the
  `format`, `lint`, `dead-code`, `test:a11y`, and aggregate `quality` scripts.
- Excluded generated OpenAPI output from style and dead-export ownership while
  retaining TypeScript compilation of maintained consumers.
- Added an explicit Windows-safe Knip invocation because its OXC raw-transfer
  fast path attempted a multi-gigabyte aligned allocation on this workstation.
- Updated the dependency lockfile and constrained vulnerable transitive
  `brace-expansion` and `cookie` versions. OpenAPI generation was smoke-tested
  after the override.
- Formatted all maintained frontend source, configuration, and browser tests.

### Correctness, typing, and cleanup

- Replaced maintained-source `any` escapes with `unknown` and narrowed records;
  imported `Buffer` from `node:buffer` in browser tests.
- Removed the unused runtime exports and private exported model/store types
  reported by Knip, along with obsolete API facade re-exports.
- Added `errors.ts` as the single unknown-error normalization helper and
  replaced the repeated catch-expression pattern across the frontend.
- Corrected export assembly decisions to apply workflow-aware output defaults
  and to merge the values just submitted by the UI when an API response omits
  them from `effective`.
- Replaced the recovery shell's boolean refresh guard with a queued,
  promise-backed drain loop. Action-triggered refreshes are no longer discarded
  when the periodic poll already owns the request slot.
- Removed unused handlers/state discovered by ESLint and corrected writable
  Svelte derivations and bindable component values.

### Ownership and performance

- Added `generation-playback.svelte.ts`, which owns the `Audio` instance,
  resolve token, sequential playback, pause/stop, and deterministic disposal.
- Added `manager-operation-store.svelte.ts`, replacing two independent manager
  polling implementations with one reference-counted observer.
- Added `ManagerPlanDialog.svelte` and removed the large confirmation surface
  from `LocalComponentsPanel`.
- Changed `SessionWorkspace` startup to load only its workflow. Capabilities,
  LLM provider models, TTS catalogues, and voice libraries now load on demand
  for the applicable stage. Settings, TTS service, voice-library, and PDF
  editor surfaces are dynamically imported.
- Changed generation page preservation to refetch the already-open ordinal
  range in one bounded request rather than replaying every 100-item page
  sequentially. Active work now has an SSE-independent safety reconciliation,
  plus one prompt authoritative refresh after a user-started run, so a segment
  cannot remain absent from a status-filtered page after regeneration.
- Kept the large PDF runtime lazy: the final manifest marks `PdfEditor` and the
  three secondary workspace modals as dynamic entries.

### Accessibility

- Added `modal-focus.ts` for initial focus, Tab/Shift+Tab containment, Escape
  handling, and opener-focus restoration.
- Applied it across maintained dialogs and full-screen modal surfaces.
- Added missing dialog roles/names and accessible names for icon-only close and
  destructive buttons.
- Added Axe WCAG A/AA coverage for core authenticated surfaces and focused
  keyboard tests for modal and setup-checklist behavior.

## Residual findings and recommended next slices

These are recorded rather than hidden by the completed high-value work:

1. **P1 — Split the stage-settings form.** `SessionWorkspace.svelte` still
   renders and persists the multi-stage settings form. Its transport is now
   demand-loaded, but the next safe decomposition is a stage-settings
   controller plus per-stage field components. Move one stage at a time and
   retain the current workspace browser tests as characterization coverage.
2. **P1 — Split provider/service administration forms.**
   `ProviderManager.svelte` and `ServiceManager.svelte` remain large because
   each combines list orchestration with add/edit/delete forms. Extract the
   forms only after adding provider-specific validation tests.
3. **P1 — Modularize the recovery-manager shell.** The recovery UI deliberately
   avoids the main application toolchain so it remains available during broken
   installs, but `app.js` still combines API transport, state transitions,
   rendering, and event wiring. Split it into dependency-free ES modules behind
   the existing integration test, preserving the no-build/no-framework recovery
   contract.
4. **P2 — Improve generated API response schemas.** Several OpenAPI operations
   still generate `never` response bodies because the backend specification
   omits schemas. Manual response generics remain necessary until the backend
   contract is corrected.
5. **P2 — Add request cancellation at the API boundary.** Component-owned
   timers and media now dispose deterministically, but the shared API facade
   does not yet expose `AbortSignal`; navigation can therefore leave harmless
   in-flight reads that complete after unmount.
6. **P2 — Expand automated accessibility states.** The new suite covers core
   routes and representative dialogs at serious/critical impact. Destructive
   confirmations, validation-error states, high zoom, reduced motion, and
   screen-reader announcements deserve dedicated cases.
7. **P3 — Revisit Svelte keyed-list policy.** Enabling
   `svelte/require-each-key` currently reports many static option/menu lists.
   Apply keys to mutable component lists first, then enable the rule once
   remaining static false positives are intentionally documented.

## Verification record

| Gate | Final result |
| --- | --- |
| `npm run quality` | Passed: Prettier, ESLint, `svelte-check` (0 errors, 0 warnings), and Knip |
| `npm run build` | Passed with Vite 7.3.6; static adapter output written in 2m01s |
| `npm audit --audit-level=low` | 0 vulnerabilities |
| Python frontend/manager contracts | 59 passed; one external `pydub` Python 3.13 deprecation warning |
| Chromium Playwright project | 22 passed, 2 intentional skips |
| Firefox Playwright project | 19 passed, 5 intentional platform skips |
| Recovery-shell JavaScript parse | `node --check` passed |
| Circular-dependency scan | Madge processed 81 files; no cycles |
| Graphify source graph | 419 nodes, 943 raw edges, no self-loops |

The Graphify result is intentionally treated as corroborating evidence. Its
final diagnostic reports 83 dangling Svelte extraction endpoints and five
directed same-endpoint collapses (principally dynamic-import/import pairs).
`api-models` and the API facades remain expected transport hubs;
`SessionWorkspace` remains the leading UI decomposition boundary, and the new
shared `errorMessage` helper is itself visible as a cross-cutting hub.

The final client artifact keeps the PDF worker (1,203.4 kB) and PDF.js editor
chunk (421.6 kB) behind a dynamic entry. The two largest ordinary route chunks
are 95.1 kB and 72.0 kB, close to the 93.5 kB and 70.0 kB baseline; the small
increase reflects the added accessibility/state handling, while settings, TTS,
voice-library, and PDF surfaces now have separate on-demand entry points.
