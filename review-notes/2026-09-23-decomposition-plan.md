# Whole-app decomposition and correctness plan

Status: **Phase 1 and W1 complete; W2 next** (2026-09-23). The approved sequence was
committed as `bf987e13`; S1 and S2 implementations are committed on `main` as
`292f4eec` and `050d1133` respectively; S3 is committed as `60fdc2c7`,
and S4 as `93c325ee`. W1 is committed as `6bf8e877` (save races) and
`1c0199f4` (settings ownership and type debt).
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

At the planning baseline, the type baseline contained **1,267 diagnostics**,
including **43 import-cycle entries**. These are not counts of proven runtime bugs or independent cycles.
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
| R4 | Fixed in `60fdc2c7`: final video publication could ignore durable cancellation or lease takeover, leak output on registration/commit failure, and let a stale attempt overwrite/delete a newer owner's committed file. | Serialized claim validation, final path allocation, promotion and registration; rejected output is removed, committed output survives later cancellation/loss. Before/during/after tests verify queue state, files and provenance. Process termination and other output domains are not covered by this guarantee. | S3 closed (final video) |
| R5 | Fixed in `93c325ee`: direct stages and continuation could queue old settings with a newly selected source; multi-section settings resolution could also mix committed versions. | Explicit SQLite read snapshot covers settings sections, service connections and stage input selection. Concurrent edits commit normally and apply to subsequent runs; prepared input stays immutable through enqueue. Planned workflows retain their stale-state rejection. | S4 closed |
| R6 | Multipart voice upload accepts `expected_revision` in the form, while OpenAPI describes required If-Match. | Clarify the contract and generated client/schema coverage when that endpoint is next changed; retain both working input paths. This does not currently invalidate the documented header path. | F3 / contract backlog |
| R7 | Source-level inconsistency: `retire_sample_artifact` preserves files shared by other sample rows but marks their common artifact deleted first. The schema permits sharing; creation by a normal supported API sequence was not established. | During voice-worker extraction, check supported import/legacy/bundled cases before changing retirement semantics. Current sample APIs normalize to a unique destination; artifact registration reuses IDs by path, not content hash. No shared user data was inspected or runtime defect claimed here. | G1 / bounded evidence backlog |
| R8 | Fixed in `6bf8e877`: standalone settings replacements could both accept the same revision, losing an update. | Immediate transaction owns revision check, mutation, history and response snapshot. Competing-writer regression proves one success and one conflict. | W1 closed |
| R9 | Fixed in `6bf8e877`: standalone settings update/patch could return another writer's values with the earlier revision. | Capture response before commit; a later committed edit remains visible to the next read without contaminating the saved response. | W1 closed |

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

### S3 complete — 2026-09-23 — `60fdc2c7`

- Starting checkpoint: clean `main` at `4eb949b9`.
- Executable scope: R4's final video export publication boundary, using real
  queue/worker transitions with disposable databases and stubbed media rendering.
  Other export formats and helper artifacts are not an all-or-nothing export job.
- Completion rule: cancellation or lost claim recorded before publication must
  prevent a new final video artifact and remove its uncommitted destination.
  Artifact publication and the guard must share a serialized write transaction.
  Hashing/rendering stay outside it. After publication commits, cancellation or
  lease loss may prevent successful job completion but must not delete the
  committed artifact, its file or provenance. Job outcome and durable output
  existence are deliberately distinct; this does not promise crash-atomic file/DB
  publication or atomic job completion with the artifact.
- Verification will inspect job status, claim generation, artifacts, lineage,
  stage selections and files before, during and after the commit boundary.
  Record reproduced defects before changing production behavior.
- Reproduced: five initial failing cases (durable cancellation and reclaimed
  lease before registration, in-memory cancellation after registration flush,
  registration failure, commit rejection); two post-commit retention controls
  passed. A separate stale-attempt case reproduced deletion of the newer owner's
  committed file: the old renderer had allocated the same path before rendering,
  overwrote it on promotion, then removed it when noticing the lost claim.
- The final stale-attempt regression was run against the `4eb949b9` renderer
  loaded into an isolated Python module from `git show`, without replacing working
  files. Its failure was the winner's missing file. The fixture advances only the
  claim clock while excluding concurrent heartbeat updates from that artificial
  clock; reclaims use the real queue API. The winning file/record is seeded via
  real artifact registration to represent the newer owner's completed work.
- Implementation: `export_publication.py` owns the final video commit boundary.
  It hashes scratch output outside the writer transaction, validates current
  job/session/kind/generation/lease and cancellation, allocates the final path,
  atomically renames, registers artifact/provenance and rechecks before commit.
  Failed publication removes only its allocated output. The output-settings
  snapshot now commits with the artifact. Direct invocations retain event-based
  cancellation; durable workers must provide both internal claim fields.
- Initial acceptance: 71 tests passed across video cleanup/commands, queue
  concurrency and audio assembly. The 10 new cases include real competing writers
  during publication: the transaction holding the writer lock publishes first,
  then cancellation/reclaim proceeds, retaining that committed output. An older
  allocation-failure test now expects rendering before final-path allocation.
- Final acceptance: another 35 export/video compatibility tests passed, including
  existing audio/subtitle matrix and tail-decision cases: **106 distinct tests
  passed** across the two runs. Ruff, Vulture, test-lane manifest, documentation
  and whitespace checks passed. Basedpyright reported 0 errors/warnings/notes;
  scratch baseline remained byte-identical (1,266 legacy entries).
- Researcher configured GPT-6 Luna/max traced queue and existing publication
  contracts; reviewer configured GPT-6 Sol/high found no material regression.
  Parent retained policy, implementation and acceptance. Review clarified that
  an assigned owner means non-null, matching the queue's existing allowance of
  empty-string worker IDs; fencing uses the per-job claim generation, without
  introducing a new worker-name validation rule.
- Limits: Linux/disposable data, stubbed rendering in concurrency tests and
  existing export compatibility checks; no Windows/browser/live-provider/full
  project qualification or deployment. A process killed between rename and
  database commit can still leave an unregistered file. Durable sidecars and
  other export formats retain their existing independent publication semantics.
- Next unit: **S4**, direct workflow input/settings freshness (R5), followed by
  the phase-1 state-contract review checkpoint. No reprioritization.

Commands (repository root; `.pixi/envs/default/bin` tools):

```bash
python -m pytest -q tests/test_export_video_cleanup.py -k 'publication or registration_rolls' --tb=short
python -m pytest -q tests/test_export_video_cleanup.py tests/test_export_video_commands.py tests/test_web_job_concurrency.py tests/test_web_audio_assembly.py --tb=short
python -m pytest -q tests/test_web_workflow_handlers.py tests/test_video_tail_freeze.py tests/test_export_video_tail_decision.py -k 'export or video' --tb=short
ruff check .
vulture
python scripts/test_lanes.py check
python scripts/check_types.py --baselinefile /tmp/pandrator-s3-type-baseline.json
python scripts/check_docs.py
git diff --check
```

The first command captured five pre-fix failures and two passing late-interruption
controls. The baseline stale-file reproduction used the isolated module described
above; the final acceptance runs passed 71 and 35 tests respectively.


### S4 complete — 2026-09-23 — `93c325ee`

- Starting checkpoint: clean `main` at `139192e5`.
- Contract: direct workflow preparation captures one coherent database version
  across settings and selected inputs. A committed edit during preparation may
  affect the next run, but must not partially enter the current payload. Run Now
  overrides retain precedence. The read snapshot ends before queue insertion;
  this is input capture, not a requirement to reject every subsequent edit.
- Reproduced three failures against the starting implementations: `clean_source`
  and direct `generate_audio` continuation each queued the new source with old
  text settings; multi-section settings resolution mixed old text with new audio
  settings. The same fixture also changes the TTS connection selection. Source
  and text changes are committed together on a separate connection, after a
  settings read and before source selection. Settings/connection changes use the
  same deterministic boundary. All data lives in temporary test roots.
- Baseline methods were loaded from `git show 139192e5` into isolated Python
  modules, then substituted only in the reproduction process. No working files
  were replaced. The final three regression cases all failed on their intended
  assertions against that baseline and passed with the fix.
- Implementation: `Database.snapshot_session` explicitly issues `BEGIN`, since
  SQLite's legacy mode does not start a transaction for SELECT. WAL permits the
  competing writer to commit while reads retain the original version.
  `WorkspaceSettingsService.resolve_in_session` resolves all sections and TTS/STT
  connection settings using that caller-owned session; the public `resolve`
  wrapper opens its own snapshot. `WorkflowService.resolve_stage` shares one
  snapshot across settings and source/outcome/media/generation selection.
  Transaction ownership is now separate from the preparation body, without
  copying the planner's broad fingerprint into direct-run code.
- Five new tests cover the three regressions, Run Now/queued-payload immutability
  when settings change immediately before enqueue, and existing planning-conflict
  rejection when an edit commits during resolution. Following direct runs see
  both new source and settings; concurrent edits are not rolled back.
- Acceptance: 124 tests passed across workflow plans, workspace parity and
  workflow handlers. The added single-stage variant and final fixture refinement
  were followed by all 14 workflow-plan tests passing: **125 distinct tests
  passed** overall. Ruff, Vulture, test-lane, documentation and whitespace checks
  passed. Basedpyright reported 0 errors/warnings/notes; the scratch baseline was
  byte-identical to the committed baseline (1,266 legacy diagnostics).
- Researcher configured GPT-6 Luna/max traced the existing planner guard and
  settings helpers; reviewer configured GPT-6 Sol/high found no material
  regression. Parent retained snapshot policy, implementation and acceptance.
- Limits: database input coherence and captured payloads, not filesystem-content
  locking or whole-workflow isolation across future continuation stages. Inputs
  deleted after capture retain existing worker validation/failure behavior.
  Existing planned-workflow fingerprint coverage was preserved, not extended to
  external secrets or runtime configuration. No live providers, Windows/browser
  qualification, full-project test run, push or deployment.

Commands (repository root; `.pixi/envs/default/bin` tools):

```bash
python -m pytest -q tests/test_web_workflow_plans.py -k 'direct_run_captures or settings_resolution_captures' --tb=short
python -m pytest -q tests/test_web_workflow_plans.py tests/test_web_parity_workspace.py tests/test_web_workflow_handlers.py --tb=short
python -m pytest -q tests/test_web_workflow_plans.py --tb=short
ruff check .
vulture
python scripts/test_lanes.py check
python scripts/check_types.py --baselinefile /tmp/pandrator-s4-type-baseline.json
cmp .basedpyright/baseline.json /tmp/pandrator-s4-type-baseline.json
python scripts/check_docs.py
git diff --check
```

The baseline regression replay used `pytest.main` after loading the two original
methods as described above; it selected both direct source cases and the settings
case. Early fixture errors (wrong stage prerequisite, response field and repeated
injection) were corrected before counting the final regression evidence.

### Phase-1 checkpoint — complete, 2026-09-23

Review of the committed S1–S4 evidence establishes the contracts to carry into
structural work; it is not a fresh retest of every earlier unit:

| Boundary | Contract to preserve |
| --- | --- |
| Regeneration scheduling (S1) | Durable resume permission has one owner; terminal children transfer it; explicit user pause revokes it, including legacy nested markers. |
| Voice normalization (S2) | Worker owns new files until artifact/sample/settings commit; failures clean only its uncommitted output. |
| Final video publication (S3) | Cancellation/lease checks and artifact publication share a serialized writer; committed output survives later interruption. Job completion is a separate transition. |
| Workflow preparation (S4) | Direct runs capture coherent input in a read snapshot; reviewed plans additionally retain stale-state and transactional execution checks. |

Phase 1 closes R1–R5 within their recorded scope. Retain S3's process-kill gap
between file rename and database commit and independent sidecar/other-format
publication semantics. R6 remains in F3 (upload header/form documentation), and R7
remains in G1 (establish supported shared-sample reachability before changing
retirement). No new proven defect requires changing the ordered queue.

**Next executable unit: W1.** Separate settings defaults, validation and snapshot
policy from workspace orchestration. First map the current settings service's
non-UI callers and imports; then choose the smallest cohesive extraction that
preserves builtin/global/service/session/Run Now precedence, secret removal,
validation, revisions/history and S4's supplied-session boundary. Keep write
transactions intact and retain a compatibility facade where needed. Verify the
existing settings/workflow tests plus focused defects found in that boundary;
do not combine W2 source/outcome ownership or generic route relocation into W1.

### W1 complete — 2026-09-23 — `1c0199f4`

- Starting checkpoint: clean `main` at `10aec9d8`; the user's request to commit
  everything was already satisfied before this unit began.
- Owned boundary: settings defaults, validation, normalization, hashing and
  secret-free snapshots; `WorkspaceSettingsService` reads/writes/resolution and
  its non-UI import callers. Preserve default/global/service/session/Run Now
  precedence, PUT replacement versus shallow PATCH merge, revisions/history,
  provider switching, caller-owned transactions and S4 read snapshots. Outcome,
  source-library, generation and route bodies stay in their existing owners.
- Reproduced three failures before changing production behavior: two standalone
  replacements both accepted revision 1 and overwrote each other; standalone
  update and patch could return revision 1 with another writer's revision-2
  values. Tests use disposable databases, a coordinated pair of real writer
  threads, and an injected real commit between the first commit and its response.
- Fix `6bf8e877`: standalone replacement begins an immediate transaction before
  the revision read. Update and patch capture their complete response inside the
  transaction that saves it. Supplied-session paths keep the caller's transaction;
  idempotent HTTP paths already provided an immediate transaction. All three
  regressions passed after the fix, separately from the structural move.
- Extraction: `settings_policy.py` owns defaults, aliases, normalizers, validators,
  recursive merge/secret removal and stable hashing. `workspace_settings.py` owns
  persistence and effective settings resolution. The 37 production consumer
  changes are imports only. Compatibility exports retain the exact same class,
  exception, function and constant objects under the old `workspace` names.
  Neither new settings module imports the workspace facade. Workspace falls
  from 5,621 to 4,258 lines; the new modules are 643 and 767 lines respectively.
- Mechanical evidence: moved declarations initially matched their pre-extraction
  ASTs exactly; all 37 consumer bodies match after excluding imports, and the
  nine remaining workspace definitions are unchanged. Final settings-service
  refinements bind mapping values once before `isinstance` checks, eliminating
  ten inherited type diagnostics without casts, ignores or baseline relocation.
- Acceptance: 278 settings/defaults/normalization/planning tests passed initially.
  Following the type refinements, all 150 settings API, workspace parity, workflow
  plan and workflow-handler tests passed: **349 distinct tests** across both
  runs. Existing tests exercise provider defaults, aliases, credential exclusion,
  validation, idempotency, revisions/history and the S4 concurrent-read cases.
  Ruff, Vulture, test-lane, documentation and whitespace checks passed.
- Type result: Basedpyright reported 0 errors/warnings/notes; the reviewed
  baseline falls from 1,266 to 1,251 entries, with cycle reports falling from
  43 to 38. Three cycle reports now attributed to
  `performance_plans.py` describe existing generation dependencies, not new
  settings dependencies. Their five directed edges were individually verified
  with `git show 10aec9d8` and AST inspection. The reviewed baseline adjustment
  records that reporting change; no moved settings diagnostics were added.
- Reviewer configured GPT-6 Sol/high found no material transaction or compatibility
  regression. Researcher configured GPT-6 Luna/max supplied caller/patch mappings
  and original import-edge evidence. Parent selected boundaries, reproduced and
  fixed the races, implemented the extraction and accepted the result.
- Limits: focused Linux/disposable-data verification, not Windows/live-provider,
  browser or full-project qualification. Existing generation cycles remain for
  later generation ownership work. No push or deployment.
- Next unit: **W2**, outcome/source-library ownership. Preserve attachment and
  artifact identities, revisions, selected-source provenance and transaction
  boundaries; inspect their supported lifecycle before extracting services.

Type-report evidence (line references at `10aec9d8`):

- `performance_plans -> speech_plan_workspace`: module import at 37–41.
- `speech_plan_workspace -> performance_plans`: local imports at 321, 326 and 370.
- `performance_plans -> workspace`: `adopt_plan` imports assembly invalidation at
  853 (the former exception import at 42 is now removed).
- `workspace -> performance_plans`: assembly preparation imports snapshot capture
  at 5543.
- `workspace -> speech_plan_workspace`: segment editing, topology and preparation
  imports at 3044, 3119, 4016, 4028 and 4373.

These unchanged edges establish the two-node performance/speech and
performance/workspace cycles and the three-node performance/workspace/speech
cycle. They are recorded transparently instead of widening W1 into generation
refactoring or restoring an unnecessary facade import to alter checker traversal.

Commands (repository root; `.pixi/envs/default/bin` tools):

```bash
python -m pytest -q tests/test_web_settings_api.py -k 'reply_matches or competing_settings' --tb=short
python -m pytest -q tests/test_web_settings_api.py tests/test_web_parity_workspace.py tests/test_web_workflow_plans.py tests/test_web_parameter_definitions.py tests/test_tts_default_migration.py tests/test_tts_voice_aliases.py tests/test_video_tail_extension.py tests/test_passage_regroup.py tests/test_cjk_text_pipeline.py --tb=short
python -m pytest -q tests/test_web_settings_api.py tests/test_web_parity_workspace.py tests/test_web_workflow_plans.py tests/test_web_workflow_handlers.py --tb=short
ruff check .
vulture
python scripts/test_lanes.py check
python scripts/check_types.py --baselinefile /tmp/pandrator-w1-type-baseline.json
python scripts/check_docs.py
git diff --check
```

The first command recorded three baseline failures and then three passes. Import
and definition parity checks used Python AST comparisons and direct identity
assertions against the compatibility exports. Type checks first exposed ten moved
settings diagnostics and three existing-cycle reports; settings diagnostics were
corrected before the final check. An intermediate scratch-baseline construction
omitted seven unchanged performance-plan entries; those were restored before the
final run, not treated as fixes or new debt.
