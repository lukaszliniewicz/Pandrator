# Whole-app decomposition and correctness plan

Status: proposed sequence; the next executable unit is **S1** below. This planning
pass changes documentation only. Source baseline: `10fb2bb5` (2026-09-23).
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

All open entries below need bounded reproduction. No new runtime defect was
proven during this planning pass.

| ID | Current evidence / uncertainty | Check and intended invariant | Unit |
| --- | --- | --- | --- |
| R1 | `generation_scheduling.release_interrupted_run` transfers permission to a waiting sibling only when the completing child is canceled. Tests cover cancellation with a sibling and failure without one. | Exercise failure with a valid queued/running sibling; determine whether the parent can restart before replacement work finishes. User pause must still dominate. | S1 |
| R2 | `clear_regeneration_baton` removes the job marker only if it matches the supplied source ID. Legacy nested lineage is supported, but current tests primarily check root selection. | Follow nested interruption/revocation through both durable run flags and job markers; prevent stale ownership or duplicate resume. A stale marker alone is not proof of an unwanted resume because the durable flag is checked. | S1 |
| R3 | `normalize_voice_recording` removes cleanup intermediates in `finally`; its final destination has explicit cleanup for only selected failures. Request-side upload cleanup is already fixed. | Inject final FFmpeg partial-write, registration and transaction failures; original reference stays intact, unpublished output does not remain, no dangling sample/artifact record. | S2 |
| R4 | Video export checks cancellation before artifact registration; job completion is a separate operation. Existing video tests cover pre-registration cancellation and ordinary success, not durable cancel/lease loss during or after registration. | Trace the commit boundary and record artifact, selection, job and lease outcomes. Distinguish cancellation before publication from cancellation after committed output; define the completion rule before changing it. | S3 |
| R5 | Direct `WorkflowService.resolve_stage` uses separate settings, selection and queue operations. Planned workflows have stale-state tests; equivalent direct-run protection was not established here. | Change source/settings at the handoff boundary. A queued run must use one coherent explicit input snapshot or reject stale preparation, never silently mix versions. | S4 |
| R6 | Multipart voice upload accepts `expected_revision` in the form, while OpenAPI describes required If-Match. | Clarify the contract and generated client/schema coverage when that endpoint is next changed; retain both working input paths. This does not currently invalidate the documented header path. | F3 / contract backlog |

## Next executable unit: S1

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

This plan uses source inspection, AST spans, existing test coverage and current
Git history. Tests were mapped, not rerun for this documentation pass. Earlier
slice checks are historical results, not a new whole-app certification. The main
UI was inspected in source only, not exercised in a browser during planning.

Evidence collection used the deep-researcher profile (configured GPT-6 Sol/high)
and researcher profile (configured GPT-6 Luna/max); model identity was not separately
reported at runtime. The parent owns this ordering and the next-unit protocol.

For each completed unit, append: ID; commit(s); specific outcome; checks/results;
remaining limitation; next unit. If priorities change, record why. Keep proposals
and confirmed defects visibly distinct.
