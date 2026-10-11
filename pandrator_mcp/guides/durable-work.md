# Durable work and failure diagnosis

Long-running application jobs and Manager operations are durable work. Every
work item has a typed ID, normalized state, progress, cancellation capability,
and polling interval. Application work uses type `job`; host-management work
uses type `manager_operation`.

The MCP work projection deliberately omits raw queue payloads. Logs are bounded
and redacted. They include operational events and safe identifiers, not source
documents, prompts, credentials, arbitrary Manager task inputs, or raw results.

Generation and regeneration return `generation_operation_id`. Use
`pandrator_get_generation_operation` with that ID to follow preparation into
synthesis without changing handles. Bounded waits run up to 60 seconds; structured
responses keep the routine receipt small. Terminal synthesis includes selected-run
coverage: included, effective, generated, inherited and missing audio counts, missing-file
counts and `export_ready`. Read it independently with
`pandrator_get_generation_run_coverage` for any saved run. Readiness concerns
available selected audio, not artistic quality or rendering success.

`audio_version_side="before"` selects the pinned baseline of a recast audio
version. Ordinary runs, including RVC runs, use `after`; they have no separate
before/after recast manifest. The generated and inherited counts partition
effective selections. Run-owned takes can be inherited shared-artifact copies,
so `run_audio_count` overlaps those counts.

The operation ID is the durable admission job ID. Cancellation through
`pandrator_cancel_work` also follows its validated child after preparation succeeds.
Legacy `get_work` still returns individual jobs and `followup_work`.

To diagnose work:

1. inspect the work item;
2. read its bounded log;
3. distinguish retryable infrastructure failures from validation or revision
   failures;
4. inspect current session, provider, or Manager state; and
5. explain a safe next action before mutating anything.

Queued, running, waiting, succeeded, failed, and cancelled are the normalized
states. Cancellation is a request and may take time while a task reaches a safe
boundary. Retrying or re-executing must use the idempotency key associated with
the exact reviewed action.

`application_response_timeout` means the app did not reply within the transport
deadline. A mutation may still have committed. Follow the returned recovery
actions: inspect current state and replay the exact request with the same
idempotency key when supported. Never invent a new key or assume failure means
no change. A connection failure remains `application_unavailable`.

Compilation-only POST previews explicitly report `safe_to_retry_read` on a
transport timeout. They do not enqueue, adopt, synthesize or refresh provider
health. Their stage `timings_ms` separates database/cast reads, normalization,
context, markup parsing, cast resolution and compilation. This is not a promise
that every POST is read-only. Unkeyed writes with an unknown outcome require
inspection, and keyed writes require exact replay with the original key.

Permanent session cleanup also uses durable work. Inspection and deletion
admission return small receipts; the full stored inventory is retrieved through
`pandrator_get_session_purge_inspection`. Work is deliberately independent of
the target session's lifetime. Deletion reuses its revision-bound purge journal
and rejects cancellation after journal claim. Idempotency reservations normally
last 24 hours (configurable up to 168); retained work remains available after
that reservation expires.

Comparison export queues `export.comparison` and publishes audio plus an ordered
take/checksum manifest. It does not synthesize or alter active audio selections.
Download the returned audio and manifest artifacts rather than requesting raw
queue payloads or embedding audio in status responses.

## Passive dispatch runs

Subtitle correction and translation dispatch is passive pull work. Create a
dispatch run, then claim and process one batch at a time; the server does not
push source text to the MCP sidecar. Run list/get responses contain metadata
only. A claim is the disclosure boundary for that batch's canonical task,
source cues, timing policy, bounded boundary context, and short-lived lease
capability. The authoritative source is `batch.cue_rows` in the default compact
packet, decoded with `cue_columns` and `turns`, or `batch.cues` when requesting
`packet_format=standard`. Cue text and optional timing each occur in
one place. Boundary context is evidence only.

Keep the returned `lease_token` scoped to the matching batch ID. Renew it when
model work needs more time, or release the batch when abandoning it so another
worker can claim it. Lease expiry or a stale lease is a state conflict, not a
reason to submit with a different batch ID. Use idempotency keys for claim,
renew, release, and submit, retrying a logical request with the same key.

Submit a typed correction or translation `result`; raw `response_text` is a
legacy adapter path. Accepted batches advance to the next sequential claim. A
rejected result remains repairable under its valid lease. The final accepted
batch automatically finalizes the run. If transient materialization trouble
leaves it `finalizing`, retry the same final submission and idempotency key.

PDF/EPUB source cleaning uses the same lease and idempotency principles but a
separate run type. Creation first queues deterministic extraction and indexing;
claiming while it is still preparing returns `run_preparing`. Once ready, the
run exposes six sequential editorial phases. Each phase requires an explicit
accept/reject decision for every server proposal and permits only phase-scoped
typed operations over disclosed block IDs.

Initial evidence is bounded, but models are not confined to detector output.
Use the leased extraction-inspection tool to browse, search, inspect context or
structure, and batch independent lookups. Returned live blocks become audited
valid targets. The final text-repair phase can replace a confirmed damaged
block without rewriting the whole document.

The source-cleaning dispatcher has no provider token or iteration budget.
`evidence_limit` bounds per-phase disclosure rather than model effort. The
final accepted phase deterministically applies and validates all operations,
then registers a selected `clean_text` artifact. A source, selection, or output
head change produces a finalization conflict instead of rebasing silently.

Speech-text optimization is the third passive run family. It starts ready from
a managed SRT, narration JSON, or TXT artifact, then exposes sequential batches
of stable `unit_id` values. Timing is present once on actionable SRT units and
absent from read-only boundary context. Submit every unit exactly once and in
order. Character and unit limits bound transport, not model tokens or effort;
Pandrator calls no model. The final batch registers `tts_optimized` only if the
pinned source, relevant selections, and previous output head remain unchanged.

Media-edit dispatch is a provider-free whole-recording run. Create it against
the active prepared media-edit revision, then claim its single immutable batch.
The claim discloses cue-level transcript evidence and current keep ranges, never
word arrays; cues wholly outside the recording duration are omitted. Return
only ordered whole-cue removal spans with reasons; use
`start_at_media_start=true` for captionless setup before the first cue and
`end_at_media_end=true` for trailing material after the last cue. An empty
`cuts` list is valid when no removal is warranted. Renew or release the lease
while reasoning, and submit with a stable idempotency key. Finalization creates
one new unreviewed revision only when the pinned revision ID, number, and
content hash are still active; a changed source fails without rebasing. After a
completed submission, list cuts with `pandrator_list_media_edit_cuts`, inspect
the relevant boundaries, refine one edge at a time when needed, then re-list
and re-inspect before approval and rendering.
