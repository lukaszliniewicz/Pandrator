# Passive MCP Dispatcher: Field Notes and Hardening Plan

Date: 2026-08-30

## Hardening status after the field test

The failure narrative below is intentionally retained as historical evidence.
The repository contract has since addressed the execution hazards that caused
the second failure and several related quality gaps:

- claimed operations now use stable source-revision cue numbers with explicit
  `id_namespace: source_revision_cue` and `valid_cue_ids`;
- the model sees one canonical `batch.cues` array rather than duplicated cue
  JSON inside a prose prompt plus rendered SRT;
- correction and translation accept typed `result` objects; raw
  `response_text` remains a compatibility path;
- claim and submit have explicit OpenAPI response schemas, and the MCP projects
  only that canonical nested shape;
- `batch_ordinal` is one-based for model/human presentation, while database
  ordinals remain an internal zero-based implementation detail; cues carry only
  their stable `cue_id`, because a second per-cue ordinal was redundant and
  ambiguous;
- timing has explicit `full`, `overlap_only`, and `none` modes. Values occur
  once under a cue's `timing` object, and `none` leaks no overlap;
- native UI/API processing and passive dispatch share `char_limit`,
  `max_segments_per_batch`, context, timing, gap, and deletion semantics;
- sequential claims carry bounded previous accepted output and following source
  context outside the actionable cue array, with no timing duplication; and
- correction/translation instructions now describe the actual typed passive
  envelope instead of the native provider's raw response format;
- claim responses distinguish `run_status` from `batch_status`, preserving the
  correct next action when an accepted claim is replayed idempotently.
- correction runs may now explicitly consume a translation artifact. The run
  persists `output_role=translation`, preserves the materialized language,
  appends a child revision to the same translation document, and fences the
  translation selection/head instead of misclassifying the result as a
  source-language correction.

Still open from this document: explicit pause/resume/cancel and accepted-batch
amendment, preview-before-commit, opaque sidecar claim handles, automatic MCP
idempotency, adaptive planning, and worker provenance. The hardened
implementation was exercised through fresh stdio MCP processes. The original
incomplete run was deliberately left unleased; deploying the hardened code did
not silently resume it.

## Translation-correction acceptance run

On 2026-08-30 the German Pascal-course translation was used as the live
acceptance test for correcting an already translated track:

- operation kind / output role: `correction` / `translation`;
- language: German (`de`), with no target language on the non-translating run;
- three sequential batches containing 500, 499, and 286 cues;
- `timing_context_mode=none`; no timing appeared in any claimed task packet;
- all three batches accepted through a fresh stdio MCP process;
- the result was materialized as revision 2 of the original translation
  document;
- 1,285 source/result cues, zero timing changes, zero speaker changes, and one
  text change: cue 800 from `Das wichtig ist,` to `Das ist wichtig.`;
- artifact parent edge, 1,285 segment-lineage edges, current translation
  selection, and stored/file SHA-256 were all verified.

The test installation migrated from `0030_dispatch_runs` to
`0031_dispatch_output_roles`; a private pre-migration backup was retained.

## Purpose and intended execution model

The passive dispatcher must never invoke an LLM itself.

The intended execution path is:

1. A model running in an MCP host creates a correction or translation run.
2. Pandrator deterministically snapshots the selected subtitle revision and queues batches.
3. That same model, or an MCP-capable subagent acting for it, claims one batch.
4. The claiming model edits the batch using its own inference capability.
5. The model submits structured results under the active lease.
6. Pandrator validates and stores the accepted result, then exposes the next batch.
7. After all batches are accepted, Pandrator atomically materializes and selects the final artifact.

Pandrator is a queue, lease coordinator, validator, and artifact materializer in this path. It is not a model router.

Sequential execution is acceptable for the initial version. It preserves corrected-context continuity and substantially simplifies output ordering and conflict handling. Multiple workers may take turns or recover abandoned leases, but only one batch should be active for a run at a time for now.

## Initial experiment state

The first run used seven correction batches. One batch was accepted and the
remaining six were left ready with no active lease and no result artifact.
That incomplete run demonstrated that accepted partial work does not replace
the selected source. It was intentionally superseded by fresh end-to-end
acceptance runs after the contract was hardened; private installation paths,
session names, identifiers, and content hashes are omitted from this public
engineering record.

## What worked well

### Deterministic creation without model execution

Creating the run performed only deterministic subtitle parsing, source/revision/hash capture, batching, and database writes. No external model was invoked.

### Source and output fencing

The run records the exact source artifact, subtitle revision, content hash, session state, and relevant stage selections. Finalization is designed to fail if those invariants change. This is the correct foundation for long-running passive work.

### Lease fencing prevented a stale worker commit

After a claimed batch was explicitly released, a submission using its old lease token was rejected with `lease_conflict`. No output was stored. This is a successful safety property, not merely an error.

### Invalid output retained the current lease

When a batch submission used IDs outside the batch-local namespace, Pandrator rejected it with `invalid_model_response` while leaving the valid current lease intact. This permits correction and resubmission without losing ownership of the batch.

### Claim replay was idempotent

Replaying a claim with the same idempotency key returned the same batch and current lease. This is valuable after uncertain network or client failures.

### Release made the run safely resumable

Both experimental interruptions ended with the active lease explicitly released. The queue returned to a clean state without waiting for lease expiry.

### Final artifact isolation

Accepting batch 0 did not mutate the selected correction artifact or produce a partially corrected final file. Materialization remains deferred until the run is complete.

## The two execution failures

These were real model/operator failures. The backend contained them correctly, but the workflow made them too easy to commit.

### Failure 1: submission under a released lease

Sequence:

1. Batch 0 was claimed.
2. Work was paused and the lease was released.
3. The correction operations were prepared later.
4. The old, released token was accidentally reused for submission.
5. Pandrator rejected the submission with `lease_conflict`.

The immediate mistake was failure to re-claim before submitting. The deeper ergonomic causes were:

- the model had to reconstruct mutable lease state from conversation history;
- `batch_id` and `lease_token` were raw, independently copied arguments;
- release did not invalidate any client-side submission object;
- the stale token remained visible in model context;
- the error returned no recovery-oriented `next_actions`.

The backend behaved correctly. The client contract was not sufficiently mistake-resistant.

### Failure 2: document IDs used against a batch-local namespace

Sequence:

1. Batch 1 represented document cues 201–398.
2. Its claimed payload renumbered them locally as 1–198.
3. The worker consulted the managed source file and prepared operations using document-global IDs beginning at 201.
4. Pandrator rejected the first operation because ID 201 was outside the claimed block.

The immediate mistake was leaving the claimed payload and reasoning from the source file. The deeper contract problems were:

- claimed cue IDs are not stable document identifiers;
- the batch payload does not expose both local and original document identity;
- IDs 1–200 recur in every batch;
- the submit schema cannot distinguish a document cue ID from a batch-local ID;
- the validation error did not return the valid ID namespace or a recovery template.

This is especially risky for agents that use tools to inspect source artifacts, retain prior-batch context, or delegate work to subagents.

## Primary architectural improvements

### P0: add a complete run lifecycle

Add explicit tools/endpoints for:

- `pause_dispatch_run`
- `resume_dispatch_run`
- `cancel_dispatch_run`
- `reopen_dispatch_batch` or `amend_dispatch_batch`

Stopping work should not mean merely releasing the current lease while the run remains indefinitely `running`. Cancellation must leave the source artifact and stage selections unchanged. Before finalization, an accepted batch should be reviewable and replaceable without recreating the entire run.

### P0: use stable cue identities

Do not make the model translate between document and batch-local numbering.

Each claimed cue should expose at least:

- a stable `cue_id` used by submission operations;
- `document_ordinal` for human readability;
- `batch_ordinal` for presentation only.

Prefer immutable subtitle-revision cue UUIDs as `cue_id`. If numeric document ordinals must remain the operation identifiers, preserve the original document numbers across every batch. Include an explicit `id_namespace` and `valid_cue_ids` list in the claim.

Never silently reinterpret an invalid local/global ID because that can edit the wrong cue.

### P0: accept structured operations directly

The MCP submit tool currently accepts `response_text`, forcing the model to JSON-encode an object inside a string. That design is useful when adapting an arbitrary external model response, but unnecessary and fragile when the MCP caller is itself the model.

Add a structured submission form such as:

```json
{
  "claim_handle": "...",
  "operations": [
    {"action": "edit", "cue_ids": ["..."], "texts": ["..."]}
  ]
}
```

`response_text` can remain as a compatibility path, but native MCP clients should use typed operations. A simpler `replacements` map should also be accepted for the common edit-only case.

### P0: separate validation from commit

Add `validate_dispatch_batch` or `preview_dispatch_submission`.

It should:

- validate operation shape and cue identity;
- produce a normalized before/after diff;
- report untouched, edited, merged, split, and deleted cue counts;
- flag empty text, suspicious expansion, likely hallucinated names, or excessive edit ratios;
- retain the lease;
- return a validation revision/hash that can be committed by `submit_dispatch_batch`.

The model can then inspect a compact diff before making the irreversible accepted-batch transition.

### P0: replace raw lease-token handling with a claim handle

The MCP sidecar should keep the backend lease token out of model-visible content when possible.

The model should receive an opaque `claim_handle`; subsequent renew, validate, submit, and release tools should accept that handle. The sidecar binds it to the target, run, batch, lease generation, and authenticated principal.

To survive sidecar restart, either:

- add a backend `recover_active_claim` operation bound to the authenticated automation client and worker ID; or
- persist the minimal claim mapping in a protected local store.

If raw lease tokens remain part of the public MCP contract, return a single composite capability rather than independent `batch_id` and token fields, and include the lease generation explicitly.

### P0: make errors recovery-oriented

`lease_conflict` should explain whether the batch is ready, leased by the same worker, leased elsewhere, completed, or expired, and return a safe next action.

`invalid_model_response` should include:

- the ID namespace;
- valid cue IDs or their bounded range;
- the index of the invalid operation;
- whether the current lease remains valid;
- a resubmission template.

The current validators are good; the recovery contract needs to catch up.

### P0: make idempotency mostly automatic for MCP callers

Requiring the model to invent and preserve every idempotency key adds another mutable identifier to context.

The MCP sidecar can generate and persist keys automatically. For content submissions, a safe deterministic key can include the claim generation and a hash of the normalized operations. Explicit keys should remain available for advanced clients and test cases.

## Batch design and editorial ergonomics

### Prefer smaller, adaptive batches

The experimental run deliberately used 200 cues and about 24,000 characters per batch to reduce round trips. That made careful editorial review, identifier tracking, and operation construction significantly harder.

For model-as-worker execution, the defaults of about 40 cues and 6,000 characters are safer. Pandrator should recommend a batch size using:

- model context/output limits supplied by the client;
- expected correction density;
- cue length and language;
- continuity requirements;
- maximum acceptable lease work duration.

A dry-run planning tool should report estimated batch count before creation.

### Return a concise, authoritative batch packet

The claim response currently contains overlapping representations: a long instruction prompt, structured cue JSON inside the prompt, and rendered `source_text`. This increases context and creates competing sources of truth.

For a native MCP model client, return:

- structured instructions;
- a structured cue array;
- bounded prior corrected context;
- the submission schema;
- stable identifiers;
- a compact human-readable rendering only when requested.

The precomposed prose prompt can remain for compatibility clients.

### Add accepted-batch inspection

Run metadata correctly redacts raw internal JSON and lease tokens, but the creator needs a scoped way to review accepted editorial changes before finalization. Provide a redacted diff view, not the raw model response or secret lease state.

### Track worker provenance

Record a non-secret worker identity per claim and accepted submission:

- automation client ID;
- optional parent/subagent label;
- model/provider label supplied by the client;
- claim and submit timestamps;
- validation summary.

This is important when several sequential subagents take turns and for diagnosing quality differences without turning Pandrator into a model router.

## Authentication, installation, and host integration findings

### The managed installation omitted a required keyring dependency

The initial owner-approved OAuth enrollment successfully issued a credential, then failed to store it because `keyring` was not installed. This left an unusable approved client and forced a second consent flow.

Fixes:

- include the `credential-stores` extra in the managed/default MCP installation;
- preflight the selected credential backend before opening the consent page;
- if credential persistence fails after issuance, revoke or invalidate the just-issued credential when safely possible;
- make `doctor` report missing storage support before login.

### Secret Service environment was lost under stdio spawning

The Python MCP SDK intentionally inherited a restricted environment and omitted `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR`. Direct keyring access worked, while the stdio child could not read the same credential.

The local Codex and OpenCode entries needed:

- `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<UID>/bus`
- `XDG_RUNTIME_DIR=/run/user/<UID>`

Hardcoding these is acceptable for the local test but not a portable product solution. Prefer a Pandrator MCP launcher that derives the user bus safely at runtime, or a credential backend that does not depend on ambient desktop-bus variables.

### Local-managed mode conflicts with a LAN canonical origin

The application is managed locally but advertises a private-LAN URL as its
canonical origin. Local-managed MCP discovery selected loopback, and strict
identity checking rejected the mismatch. The test had to use an explicitly
CIDR-pinned LAN target and owner-approved OAuth.

Pandrator should support a signed relationship between:

- the canonical public/LAN application origin; and
- a local manager-discovered control origin.

Do not weaken identity checks. Instead, have Manager discovery return the canonical application identity plus an authenticated local route, or advertise both origins as explicitly bound identities.

### Generated OpenCode configuration did not match the installed schema

`pandrator-mcp host-config opencode` generated an `mcp.servers` wrapper, while the installed OpenCode configuration stores server names directly under `mcp`. The configuration had to be adapted manually.

Add version-aware generation and round-trip validation against supported Codex/OpenCode schemas.

### MCP configuration does not hot-load into an existing Codex task

The current task did not gain Pandrator tools after editing `~/.codex/config.toml`. A fresh task is required for native tool exposure. Direct stdio was therefore used for this experiment. Documentation and diagnostics should state this explicitly.

### Pixi activation was vulnerable to the per-user `/tmp` quota

Service startup initially failed because Pixi could not write a tiny activation script to a completely full per-user tmpfs quota, despite ample disk space under `/home`.

Manager launch specs should set a controlled `TMPDIR` under the Pandrator workspace or preflight temporary-space/quota availability and report it directly.

### Runtime version reporting was not specific enough

The development slot reported only the package version. Health and diagnostics
should also expose the active slot/build identifier and source revision so
operators can prove which code is running.

## Proposed native MCP protocol

An easier and more resilient model-facing sequence would be:

1. `plan_dispatch_run(session_id, kind, model_limits, policy)`
   - Returns source identity, estimated batches, and warnings without creating state.
2. `create_dispatch_run(plan_hash)`
   - Creates the immutable queued run.
3. `claim_dispatch_batch(run_id, worker_id)`
   - Returns structured cues with stable IDs and an opaque claim handle.
4. `validate_dispatch_batch(claim_handle, operations)`
   - Returns a normalized diff, warnings, and validation hash; retains the lease.
5. `submit_dispatch_batch(claim_handle, validation_hash)`
   - Commits exactly the previewed result.
6. Repeat claim/validate/submit until complete.
7. `get_dispatch_result(run_id)`
   - Returns final artifact identity and a bounded audit summary.

At any point:

- `renew_dispatch_claim(claim_handle)`
- `release_dispatch_claim(claim_handle)`
- `pause_dispatch_run(run_id)`
- `resume_dispatch_run(run_id)`
- `cancel_dispatch_run(run_id)`
- `reopen_dispatch_batch(run_id, ordinal)` before finalization

This remains fully passive. Every linguistic decision still comes from the MCP client's own model.

## Acceptance tests to add

1. A model-host simulation completes a multi-batch run without any configured Pandrator model provider or outbound inference call.
2. Network/inference clients are monkeypatched to fail if called during create, claim, validate, submit, and finalization.
3. Reusing a released/expired claim handle fails and returns an actionable re-claim instruction.
4. Stable cue IDs remain valid across batches; local presentation ordinals cannot be submitted accidentally.
5. Invalid operations retain the lease and return valid IDs plus a corrected submission template.
6. A sidecar restart during an active lease can recover or safely release the claim.
7. Pause prevents new claims; resume continues from the next unaccepted batch.
8. Cancel leaves source and stage selections unchanged and produces no result artifact.
9. An accepted batch can be previewed and amended before finalization.
10. Finalization remains atomic under source revision, source hash, session state, input selection, and output-head conflicts.
11. Managed installation includes a working native credential store before enrollment begins.
12. Generated Codex and OpenCode configurations parse and can launch an authenticated stdio server under restricted environment inheritance.
13. Default/adaptive batch sizes stay within declared model input and output budgets.
14. Tool results and logs never expose backend lease secrets when claim handles are used.

## Recommended implementation order

1. Add pause/resume/cancel and accepted-batch reopen/amend semantics.
2. Add validate/preview-before-submit and accepted-batch amendment.
3. Introduce sidecar claim handles and automatic idempotency.
4. Fix keyring packaging, credential preflight, and stdio launcher environment.
5. Fix version-aware host configuration generation.
6. Add adaptive batch planning and worker provenance.

## Bottom line

The backend's core safety invariants worked: both operator mistakes were contained without corrupting the source or producing a partial artifact. That is encouraging.

The initial experiment also showed that containment is not enough. Stable cue
identity and structured operations now remove the worst ambiguity. The next
resilience gains are preview-before-commit, opaque claim handles, automatic
idempotency, and explicit lifecycle control.
