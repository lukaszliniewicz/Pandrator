# Whole-app decomposition and correctness plan

Status: **S1 and S2 complete; S3 next** (2026-09-23). The approved sequence was
committed as `bf987e13`; S1 and S2 implementations are committed on `main` as
`292f4eec` and `050d1133` respectively.
Planning source baseline: `10fb2bb5` (2026-09-23).
Update this file as units finish; do not select a new area solely because it is
large or interesting. This is internal engineering working material, not a
release promise or a claim that the remaining app has been audited.

## Objective and priority rules

Make responsibilities and state ownership explicit while correcting defects
that the work exposes. Preserve working sessions, original media, immutable
revisions, artifact provenance, authentication, retry behavior and compatibility.

Rank work by: (1) credible data/state or recovery risk, (2) dependencies that block
clean extraction, (3) mixed responsibilities and frequent changes, and (4) the
ability to verify the change. File length is a navigation aid, not an acceptance
target. Large cohesive/declarative modules can remain large.

Work on **one executable unit at a time**, usually one focused commit or a small
series with one outcome. A phase is not a giant PR. A newly reproduced data-loss,
authorization, corruption or blocking recovery defect can interrupt the order;
record the evidence and the reason for the change here. Other adjacent findings
enter the issue register rather than silently broadening the current patch.

## Completed foundation

| Commits | Completed scope | Preserved boundary / defect addressed |
| --- | --- | --- |
| `95dd817c`, `0320b70f` | Output assembly/export ownership; input resolution and contracts | Explicit context, immutable input selection, audiobook contract validation |
| `6dedfa62`, `a3fb4859` | Video preparation, scratch ownership, encoding commands | Failure cleanup and cancellable child processes; not an atomic job/publication guarantee |
| `26f46eab` | Generation scheduling and annotation reads | Resume permission survives paused replacement runs; explicit user pause wins; fewer dependency cycles |
| `10fb2bb5` | Voice HTTP domain, retry/projection helpers, recording queue transaction | Upload artifact and job commit together; failed requests remove their scratch recording |

These slices are locally committed and tested, not deployed by this exercise.
Do not repeat their extraction without new evidence. Their limits feed the
specific checks below.

## Current structural map

Counts are handwritten source lines at the baseline, including comments and
blank lines. Function spans include nested definitions. Declarative registration
functions are not comparable to a single algorithm of the same size.

| Surface | Current evidence | Proposed responsibility boundary |
| --- | --- | --- |
| `pandrator/web/workflow_handlers.py` — 10,521 lines | `_run_generation`: 997 lines; `_generate_audio`: 576; voice normalization/publication still grouped here | Keep job facade/registration; separate bound inputs, execution, take publication and terminal-state handling |
| `pandrator/web/workspace.py` — 5,609 | Settings, outcome, source library, generation and resource claims; topology transaction: 505 lines | Explicit domain services and lower-level policies; keep each write transaction intact |
| `pandrator/logic/tts_handler.py` — 7,619; `pandrator/web/tts_providers.py` — 2,421 | Config, discovery, voice operations, payloads and synthesis; adapters call private handler helpers | Shared endpoint/transport ownership below provider adapters |
| `pandrator/web/workflows.py` — 2,176 | `snapshot`: 1,008 lines; `resolve_stage`: 450 | Batched reads, status projection and immutable run preparation |
| `pandrator/web/api_routes.py` — 6,736 | 159 remaining nested route/helper functions | Domain registration using existing RouteContext/guards; split alongside domain work |
| `pandrator/web/voice_routes.py` — 1,459 | Now a domain owner, but preview adoption and mutation branches remain substantial | Move use cases only when further duplication or ownership problems justify it |
| Source/media/dispatch | PDF adapter: 2,432; media edit: 2,001; three dispatch modules: 1,835–2,228 | Inspect provenance, lease/retry and proposal application before selecting an extraction |
| Manager | Operation handler class: 2,028 lines; store: 1,647 / 59 methods; API factory: 1,863 | Separate lifecycle task families and read models while retaining atomic operation commit and rollback ownership |
| Installer | Component mixin: 2,158 lines / 72 methods | Process identity, runtime preparation, engine installers and migrations; retain dependency and cleanup ordering |
| MCP | Server factory: 3,346 lines / 120 nested functions; application client: 2,873 / 137 methods | Domain tool registration over shared transport policy; preserve schemas and bounded downloads |
| Main UI | SessionWorkspace: 6,175; GenerationDrawer: 3,712 | Complete settings and interaction features, with one owner for edits, requests and playback |

The current type baseline contains **1,267 diagnostics**, including **43 import-cycle
entries**. These are not counts of proven runtime bugs or independent cycles.
The installer has substantial baseline debt too: workflows 185 entries, runtime
166 and components 89. Address debt in touched responsibilities; do not scatter
casts or suppressions across the repository to chase a zero counter.

## Ordered work queue

| Phase | Units, in order | Why now / dependencies | Exit gate |
| --- | --- | --- | --- |
| **1. Close state-safety questions** | S1 regeneration resume ownership; S2 voice-worker output cleanup; S3 cancellation/lease loss at publication; S4 direct workflow preparation freshness | Complete the failure-path picture before moving stateful code | Each question is reproduced and fixed, shown safe with evidence, or explicitly deferred with a reason; no vague “audited” status |
| **2. Separate workspace responsibilities** | W1 settings defaults/validation/snapshot policy and settings service; W2 outcome/source-library services; W3 generation read/history projection; W4 topology operations | These services are shared by workers, providers and workflow views | Callers use explicit boundaries without new helper-to-caller imports; existing settings precedence, IDs and atomic topology transactions preserved |
| **3. Separate worker execution** | G1 voice/source worker operations; G2 generation input binding and execution; G3 take publication and finalization | Uses phases 1–2 to preserve state semantics while shrinking WorkflowHandlers | Existing job kinds, payloads and late-bound handler overrides remain valid; serial/parallel synthesis, failed batches, pause/resume and edit-copy publication behave identically except documented fixes |
| **4. Separate TTS/provider ownership** | T1 endpoint identity and shared lock/transport owner; T2 provider configuration/catalogue projection; T3 provider payload/voice operations and synthesis adapters | Private helper back-references and shared resources require an intentional boundary | One lock identity per endpoint; preserve job resource claims shared with ASR/alignment/cleanup; no accidental concurrent model use, duplicated pools, changed provider IDs or credential exposure; representative adapter/concurrency tests pass |
| **5. Simplify workflow reads and remaining HTTP domains** | F1 snapshot reads/projection; F2 stage input preparation; F3 remaining route groups | Benefits from separated settings/workers/providers; S4 determines any write-stage guard | Preserve batched queries, lazy history, selected sources and settings; URL/method/endpoint/auth/idempotency/response contracts stay stable |
| **6. Inspect source, media and passive dispatch** | D1 ingestion/PDF lineage; D2 media proposal application; D3 dispatch claim/submit/materialization | These were surveyed structurally, not deeply audited | A bounded evidence pass first; preserve physical-page/cue/block identities, lease ownership, replay protection and atomic adoption; extract only demonstrated mixed responsibilities |
| **7. Supporting app lifecycle and MCP** | O1 Manager operation/state boundaries; O2 installer runtime/component workflows; O3 MCP client/tool grouping | Separate safety-sensitive lifecycle work from media execution refactors | Disposable-root recovery/rollback evidence and platform checks; preserve reviewed plan identity, data ownership, tool names, schemas, scopes and retry identity |
| **8. UI and retirement, selectively** | U1 stage-settings features; U2 drawer search/alternate settings/history; U3 recovery UI/tray; L1 legacy Qt retirement decision | UI remains parent-owned; stable backend contracts reduce simultaneous change | Keep existing stores/edit queue/playback ownership; production-build browser checks at desktop/narrow widths; legacy deletion requires packaging and dynamic-caller evidence |

**Review checkpoints:** after phase 1, confirm the state contracts before structural
moves. After phase 3, review the achieved boundaries and remaining evidence before
starting phase 4. Phases 4–8 are an ordered backlog, not a promise to refactor every
listed file. A bounded check that finds adequate ownership can close a unit with
no production change.

Route extraction is paired with the service it exposes: session/settings routes
with phase 2, provider routes with phase 4, remaining workflow/job/media routes
with phase 5. We will not spend a separate round merely relocating every handler.
Manager/installer **correctness findings are promoted immediately if verified**;
phase 7 is their decomposition order, not permission to ignore a current hazard.

### Supporting-app boundaries to carry into phase 7

- **O1 Manager:** `operations/handlers.py` combines staging, release activation,
  service control, reconciliation and uninstall. `ManagerStore.commit_operation_success`
  deliberately commits component state, ownership, release acceptance, revision
  and terminal operation together. Filesystem activation precedes that transaction;
  retain rollback material until it succeeds. Do not fragment this transaction
  merely to shorten the store. API route extraction must preserve endpoint names
  because the automation scope map uses them. Verification anchors:
  `test_manager_operations.py`, `test_manager_releases.py`,
  `test_manager_control_plane.py` (rollback, recovery, exact/idempotent plans,
  maintenance guards and scope contracts).
- **O2 Installer:** `components.py` mixes process identification/termination,
  repositories, engine installation and migrations. Preserve validated download
  and replacement ordering, original models, and the RVC rule that service
  preparation completes before legacy packages are removed. Start with the
  relevant cases in `test_installer_update_migrations.py`; use disposable roots
  and process doubles. A Linux-only run does not establish Windows process or
  filesystem behavior. Inspect runtime/workflow collaborators before defining
  a write packet; their diagnostic counts are evidence of debt, not a design.
- **O3 MCP:** separate domain registrations only where this improves ownership.
  Preserve the three post-registration execution-policy schema augmentations.
  The application client has separate JSON and binary transport paths, plus
  resumable downloads; keep target identity, credentials, CSRF/bootstrap,
  redirect policy, bounded bodies, retries and error mapping coherent rather
  than copying them into each domain client. Verification anchors:
  `test_mcp_server.py`, `test_mcp_architecture.py`,
  `test_mcp_application_client.py` (schema/negotiation, package boundaries,
  identity checks, bounded requests and resumable download safety).

## Issue register: evidence, not presumed defects

These entries began as questions during planning. S1 reproduction results are
recorded below; the other open entries still need bounded reproduction.

| ID | Current evidence / uncertainty | Check and intended invariant | Unit |
| --- | --- | --- | --- |
| R1 | Fixed in `292f4eec`: a resumed older replacement could wait behind the current owner; that owner's failure or success requeued the parent too early. Also, cancellation could transfer permission to an already-running sibling whose loaded payload lacked the marker, leaving the parent paused. | Terminal outcomes now transfer permission to remaining active replacements; terminal cleanup reads durable ownership. Real worker tests cover queued/running siblings, unchanged/edited plans and explicit pause. | S1 closed |
| R2 | Fixed in `292f4eec`: revocation via a different output ancestor left a stale job marker in current edit-copy and legacy nested chains. Durable permission checks already prevented that stale marker from resuming the parent. | Revocation removes the child's marker regardless of ancestor ID. Tests separately prove explicit pause cannot be undone and no new job is queued. | S1 closed |
| R3 | Fixed in `050d1133`: normalization left a new WAV after final FFmpeg, preparation, registration or commit failure. Database rollback already preserved the existing reference. | One cleanup boundary retains ownership of the destination until successful transaction exit. Sixteen failure regressions plus cancellation and post-commit preservation controls pass. Request-side upload cleanup remains unchanged. | S2 closed |
| R4 | Video export checks cancellation before artifact registration; job completion is a separate operation. Existing video tests cover pre-registration cancellation and ordinary success, not durable cancel/lease loss during or after registration. | Trace the commit boundary and record artifact, selection, job and lease outcomes. Distinguish cancellation before publication from cancellation after committed output; define the completion rule before changing it. | S3 |
| R5 | Direct `WorkflowService.resolve_stage` uses separate settings, selection and queue operations. Planned workflows have stale-state tests; equivalent direct-run protection was not established here. | Change source/settings at the handoff boundary. A queued run must use one coherent explicit input snapshot or reject stale preparation, never silently mix versions. | S4 |
| R6 | Multipart voice upload accepts `expected_revision` in the form, while OpenAPI describes required If-Match. | Clarify the contract and generated client/schema coverage when that endpoint is next changed; retain both working input paths. This does not currently invalidate the documented header path. | F3 / contract backlog |
| R7 | Source-level inconsistency: `retire_sample_artifact` preserves files shared by other sample rows but marks their common artifact deleted first. The schema permits sharing; creation by a normal supported API sequence was not established. | During voice-worker extraction, check supported import/legacy/bundled cases before changing retirement semantics. Current sample APIs normalize to a unique destination; artifact registration reuses IDs by path, not content hash. No shared user data was inspected or runtime defect claimed here. | G1 / bounded evidence backlog |

## S1 execution contract (completed)

**Outcome:** establish whether regeneration completion/failure/cancellation can
lose or duplicate permission to resume an interrupted run, and fix only confirmed
violations in this scheduling responsibility.

**Ownership:** `pandrator/web/generation_scheduling.py`; related caller changes in
`jobs.py`, `workspace.py` or `_resume_generation_after_regeneration` only if the
reproduction proves they are necessary. Focused tests: `test_generation_edit_audio.py`,
`test_web_generation_regeneration.py`, and `test_web_job_concurrency.py`.
No UI, provider behavior, topology rewrite, schema migration or generic queue redesign.

**Protocol:**

1. Use disposable databases and deterministic fake synthesis. Construct valid
   source/child/sibling runs through existing service/queue APIs. Explicitly mark
   any fixture manipulation used to simulate a real transaction boundary.
2. Exercise failed owner plus waiting sibling, canceled owner plus waiting sibling,
   explicit parent pause before child completion, and legacy nested revocation.
   Retain the existing terminal-child replacement and paused-child resume cases
   as controls. Cover both same-plan and edit-copy scheduling where the paths differ.
3. Record run flags/statuses, source/output/interrupted IDs, current job IDs,
   queued job payloads, resume markers and selected segment IDs. Check the queue
   handoff as well as the handler's return value.
4. If a valid sequence violates the existing interruption contract, first keep a
   failing regression and then correct the smallest owner of the transition.
   Do not manufacture a bug with an impossible fixture or infer one from a
   leftover marker whose permission flag is already revoked.
5. Stop after the matrix and focused regression/quality checks pass. If fixing it
   requires choosing new user-visible behavior or changing a durable contract,
   document that specific decision before widening the unit. If all cases are
   safe, record that result and proceed to S2 without inventing a refactor.

**Acceptance:** explicit user pause is never undone; only the current authorized
owner may cause one checkpoint-preserving resume; requested segments remain
unchanged; immutable output lineage stays separate from scheduling ownership;
all writes in a transition retain their caller-owned transaction.

## Acceptance and stopping rules for every later unit

- State the owned functions, invariants, tests and non-goals before editing.
  Specialists gather evidence or implement exact non-UI packets; the parent owns
  architecture, integration and acceptance. One writer per surface.
- A pure move should have normalized AST/command or response parity where useful.
  A behavior fix needs a representative regression that fails before and passes
  after. Prefer separate small fix/extraction commits when that improves review.
- Keep IDs, revisions, original media and existing artifacts. Check failure paths
  at file creation, database commit, queue enqueue and publication where relevant.
  Do not move network/media work under a long SQLite write lock.
- Keep API/job/MCP contracts stable by default. Deliberate changes require matching
  schema/client/documentation changes and explicit contract tests.
- Use the repository's [quality policy](../docs/development/code-quality.md):
  Ruff, basedpyright with a scratch baseline, Vulture and test-lane validation.
  Review baseline reductions; no new entries or diagnostic relocations used to
  hide debt. Run the focused affected suites, not an automatic full-suite loop.
- For query refactors, compare representative SQL/query counts and output payloads.
  Do not promise speed gains without measurements. For shared TTS resources, test
  lock identity, cancellation and concurrency, not just successful fake requests.
- UI work additionally runs web quality/check/build sequentially where generated
  metadata is shared, then relevant browser scenarios with real rendered evidence.
  Include pending edits, request cancellation, focus/keyboard and narrow viewports.
- Before a broad release, use the authoritative full test lanes and relevant
  Linux/Windows/browser/package checks. A local focused pass is not cross-platform
  or live-provider qualification. Deployment remains a separate release task.
- A unit is done when its responsibility has a clear owner, required contracts
  and failure behavior are verified, and discovered in-scope defects are closed.
  We do not chase a line-count ceiling or zero legacy diagnostics as a completion rule.

## Explicit deferrals

No rewrite of routing, jobs, ORM, frontend state or MCP frameworks. No hand-editing
of generated API clients, lockfiles or mirrored model catalogues. Do not split
`models.py`, OpenAPI declarations or MCP registration merely because they are long.
Do not delete the deprecated Qt store based only on absence of static imports.
No mass utility module, new abstraction layer without a current caller, speculative
performance tuning, provider/model expansion or release/deployment work in this plan.

## Evidence and future progress record

The initial plan used source inspection, AST spans, existing test coverage and
Git history. Tests were mapped, not rerun for that documentation-only pass;
implementation verification is recorded per unit below. Earlier slice checks
are historical results, not a new whole-app certification. The main UI was
inspected in source only, not exercised in a browser during planning.

Evidence collection used the deep-researcher profile (configured GPT-6 Sol/high)
and researcher profile (configured GPT-6 Luna/max); model identity was not separately
reported at runtime. The parent owns this ordering and the next-unit protocol.

For each completed unit, append: ID; commit(s); specific outcome; checks/results;
remaining limitation; next unit. If priorities change, record why. Keep proposals
and confirmed defects visibly distinct.

### S1 complete — 2026-09-23 — `292f4eec`

- Starting checkpoint: `main` at `bf987e13`, clean; all earlier work committed.
- Reproduction: 18 new parameterized cases. Before production changes, the
  16-case queued/running-sibling matrix had 6 failures and 10 passes; the two
  nested-marker cases both failed their cleanup assertion, after passing the
  durable revocation/no-extra-job assertions.
- The queued-sibling ordering is created through generation services and a real
  Worker: pause the first replacement, create the second, resume the first behind
  it, then finish/fail/cancel the second. Running-sibling transfer is triggered
  by canceling the queued owner inside the earlier worker's fake synthesis call.
  Only the legacy database shape is seeded directly, because current APIs flatten
  it. No live providers or user databases are involved.
- Production changes: terminal outcomes transfer permission to remaining active
  replacements; the worker's final cleanup reads durable permission after any
  in-flight transfer; revocation always removes that child's marker. No output
  lineage, original segment selection, schemas or queue contracts are changed.
- Verification: 64 distinct focused tests passed across the final runs. The
  three-file run below passed 63 and exposed one unrealistic older fixture:
  it invoked a terminal callback while both children were still queued, expecting
  the parent to resume. That fixture now marks the replacements terminal, and
  its individual rerun passed. All 18 new cases passed in the three-file run.
- Quality: repository-wide Ruff, Vulture, test-lane manifest, documentation and
  whitespace checks passed. Basedpyright reported 0 new errors/warnings/notes;
  scratch-baseline comparison removed one optional-member diagnostic in the
  worker wrapper, with no additions. Committed baseline: 1,267 -> 1,266 entries.
- Bounded read-only research (GPT-6 Luna/max configured) confirmed the legacy
  guards; patch review (GPT-6 Sol/high configured) found no material production
  issue and independently identified the older fixture mismatch. These are
  configured profile identities, not separately verified runtime model reports.
- Scope remains S1. No S2 cleanup or wider decomposition changes are included.
- Limits: disposable SQLite databases, Linux, fake TTS; no live-provider,
  Windows, browser, full-project suite or deployment qualification was claimed.
  Cancellation/lease loss at artifact publication remains S3.
- Next unit: **S2**, final voice-worker destination cleanup under partial FFmpeg
  writes, registration failures and transaction failures (R3). No reprioritization.

Commands (repository root; tool binaries from `.pixi/envs/default/bin`):

```bash
python -m pytest -q tests/test_generation_edit_audio.py tests/test_web_generation_regeneration.py tests/test_web_job_concurrency.py --tb=short
python -m pytest -q tests/test_web_generation_regeneration.py::GenerationRegenerationTests::test_repeated_default_regeneration_replaces_single_root_baton --tb=short
ruff check .
ruff check tests/test_web_generation_regeneration.py
vulture
python scripts/test_lanes.py check
python scripts/check_types.py --baselinefile /tmp/pandrator-s1-type-baseline.json
python scripts/check_docs.py
git diff --check
```

The scratch type baseline was copied from the committed baseline before checking;
only its inspected reduction was copied back. Pre-fix regression runs selected
the three new parameterized tests in `test_generation_edit_audio.py` and preserved
the failing assertions before product changes.

### S2 complete — 2026-09-23 — `050d1133`

- Starting checkpoint: clean `main` at `f267b20c`.
- Contract: a new normalized WAV remains worker-owned until artifact, sample,
  voice revision and provider-staleness changes commit together. Failure before
  commit removes that new destination and cleanup intermediates; original upload,
  existing reference and their records remain unchanged. Failure after commit
  must not remove the new durable sample. No queue, provider or UI redesign.
- Reproduction: 16 cases failed before product changes, covering partial final
  FFmpeg output, preparation/hash failure, registration after flush, and commit
  rejection, for add/replace and cleanup off/on. All database-state assertions
  passed before the orphan-file assertions failed. Four controls passed: two
  cancellation cases and post-commit retirement/progress exceptions.
- Disposable SQLite/files only; final FFmpeg writes and DeepFilterNet2 are stubbed
  for fault injection. Existing real-FFmpeg tests remain part of acceptance.
- Implementation: one `try/finally` covers preparation and publication, with
  destination ownership transferred only after successful database-session exit.
  Reuses best-effort managed-file cleanup; no media/hash I/O moved under the
  database write transaction. Retirement of old files and final progress reporting
  remain after publication, so their errors cannot discard a committed sample.
- Verification: **70 tests passed**, including all 20 new cases, existing real
  FFmpeg normalization and replacement tests, upload routes and lifecycle retries.
  Ruff, Vulture, test-lane manifest, documentation and whitespace checks passed.
  Basedpyright: 0 errors/warnings/notes; baseline unchanged at 1,266 entries.
- Evidence support: researcher profile configured GPT-6 Luna/max traced artifact
  preparation/registration, commit/rollback, and retirement ownership. Parent
  designed the failure matrix, implemented the fix and inspected the result.
- Limits: no live DeepFilterNet2 model, browser, Windows or full-project suite;
  no deployment. Unlink permission/I/O errors remain best-effort, consistent with
  existing managed-file cleanup. Process death and cancellation/lease loss around
  committed publication remain separate from exception cleanup in S3.
- Adjacent finding R7 was recorded with its reachability limit rather than added
  to this exception-cleanup patch. No change to the planned sequence.
- Next unit: **S3**, cancellation/lease loss at artifact publication (R4).

Commands (repository root; `.pixi/envs/default/bin` tools):

```bash
python -m pytest -q tests/test_web_voice_cleanup.py -k 'normalization_failure_removes or postcommit_failure or canceled_normalization' --tb=short
python -m pytest -q tests/test_web_voice_cleanup.py tests/test_web_voice_library.py tests/test_voice_lifecycle_idempotency.py --tb=short
ruff check .
vulture
python scripts/test_lanes.py check
python scripts/check_types.py --baselinefile /tmp/pandrator-s2-type-baseline.json
python scripts/check_docs.py
git diff --check
```

The first command was run before the fix (16 failures, 4 controls passed);
the three-file acceptance run passed all 70 tests after it. The type check used
a copy of the committed baseline, verified byte-identical after checking.
