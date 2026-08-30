# Passive subtitle dispatch

Passive dispatch lets Pandrator coordinate correction or translation without
calling an external model itself. The model already running in Codex,
OpenCode, Claude Code, or another MCP host performs the language work through
the Pandrator MCP tools.

Use it when the host model is capable but a separate model API is unavailable,
undesirable, or not worth configuring. It is also useful when you want the
host's normal agent supervision and tool permissions around each batch.

## What “passive” means

Pandrator is the durable dispatcher and validator. It:

- pins the exact source artifact, revision, content hash, and relevant stage
  selections;
- constructs deterministic semantic batches;
- discloses one batch only after a worker claims it;
- leases that batch to one worker for a bounded period;
- validates typed results against stable cue IDs and task policy; and
- materializes the final artifact only after every batch is accepted.

Pandrator does **not** choose or call a model in this mode. The MCP host decides
which model is running and submits the result. Provider keys and model settings
are not part of the dispatch run.

## The sequential loop

```text
create run
   ↓
claim next batch ── no batch available ──→ inspect run state
   ↓
process only batch.cues
   ↓
submit typed result ── rejected ──→ repair under the same valid lease
   ↓ accepted
claim next batch
   ↓ final accepted batch
atomic artifact materialization
```

Listing or inspecting a run returns metadata, not raw subtitle content. Claim
is the content-disclosure step and returns a short-lived `lease_token`. That
token belongs to one batch and cannot be used to submit another.

## The task packet

The claim identifies the task as correction or translation and provides:

- one-based `batch_ordinal` for presentation;
- an explicit source-revision cue-ID namespace;
- `valid_cue_ids` and the only actionable `batch.cues` array;
- bounded previous-output and following-source context;
- timing according to `full`, `overlap_only`, or `none`; and
- the lease token and expiry.

Context is evidence for continuity, not additional work. Do not edit or submit
it. Use the declared `cue_id`, never an SRT number guessed from its position in
the batch.

## Typed results

A correction result contains `kind: correction` and operations over declared
cue IDs. A translation result contains `kind: translation`, exactly one item
per actionable cue ID, and optional glossary additions. Raw `response_text`
exists for compatibility with adapters that can return only model text; MCP
workers should prefer the typed result.

Pandrator validates IDs, operation shape, deletion policy, speaker decisions,
translation coverage, and glossary changes. A rejected submission is not
permission to skip the batch or change the source. Repair the result while the
lease remains valid and resubmit it.

## Lease and run states

- **Renew** a lease before expiry when language work is taking longer.
- **Release** it when stopping so the run does not wait for expiry.
- A stale or mismatched token cannot submit.
- A run can be busy because another batch lease is active.
- Source or output conflicts mean the pinned revision or relevant current
  selection changed; inspect the conflict instead of forcing finalization.
- `finalizing` means all batches were accepted but durable materialization has
  not completed. Retry the same final submission and idempotency key after a
  transient failure.

Claim reports batch status and run status separately. Replaying an already
accepted idempotent claim cannot therefore masquerade as a completed run.

## Correcting a translation

A correction run can explicitly pin an existing translation artifact. The
task is still correction, but its `output_role` is translation. Finalization
appends a new revision to the target-language translation lineage, preserving
language and downstream voiceover semantics.

This is preferable to pretending target-language cleanup is a new translation
or accidentally publishing it as a source-language correction.

## Security and privacy boundary

Subtitle text is disclosed to the model running in the MCP host when that host
claims a batch. Whether the model runs locally or at an external provider is a
property of the host, not Pandrator. Review the host's model and data policy.

Use least-privilege MCP scopes, bind one sidecar process to one fixed target,
and keep credentials outside model-visible configuration and tool arguments.
Dispatch needs application read and run authority; it does not need Manager
mutation authority.

## Getting started through MCP

Install and configure the sidecar, enroll one target, run `doctor`, and add the
generated secret-free stdio fragment to the host you actually use. Then ask the
agent to:

1. inspect the intended session and selected source revision;
2. create a correction or translation dispatch run with explicit settings;
3. claim, process, and submit batches sequentially;
4. renew or release leases deliberately; and
5. inspect the finalized artifact and lineage before selecting it downstream.

Exact target, scope, host-configuration, and tool behavior belongs to the
[Pandrator MCP guide](../../pandrator_mcp/README.md). The
[subtitle pipeline reference](../reference/subtitle-pipeline.md) documents the
shared quality parameters.
