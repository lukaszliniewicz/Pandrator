# Passive processing through MCP

Passive dispatch lets Pandrator coordinate language or editorial work without
calling a model provider itself. The model already running in Codex, OpenCode,
Claude Code, or another MCP host performs the work by claiming one bounded
packet, returning a typed result, and continuing until the run is complete.

Three passive workflows are available:

- subtitle correction and translation over stable cue IDs;
- PDF or EPUB source cleanup before audiobook narration preparation; and
- whole-document speech-text optimization before audio generation.

Use passive processing when the host model is capable but a separate model API
is unavailable, undesirable, or not worth configuring. It also keeps the
host's normal agent supervision and tool permissions around every batch.

## What “passive” means

Pandrator is the durable dispatcher, filesystem owner, and validator. It pins
the source and relevant output state, prepares deterministic evidence, leases
one packet at a time, validates typed results, records an audit trail, and
materializes the final artifact only after every packet is accepted.

Pandrator does **not** choose or call a model in this mode. Provider keys,
provider model settings, token budgets, request timeouts, and model-iteration
limits are not part of a passive run. The MCP host decides which model works on
the claimed content.

The general loop is:

```text
create run
   ↓
wait for deterministic preparation when required
   ↓
claim next packet ── none ready ──→ inspect run state
   ↓
inspect/search more of the pinned extraction when needed
   ↓
process the disclosed or explicitly inspected evidence
   ↓
submit typed result ── rejected ──→ repair under the same valid lease
   ↓ accepted
claim next packet
   ↓ final accepted packet
validated artifact materialization
```

Listing or inspecting a run returns metadata, not the source text. Claim is
the content-disclosure boundary and returns a short-lived `lease_token`. That
token belongs to one batch and cannot submit another.

## Subtitle correction and translation

A subtitle claim identifies correction or translation and provides:

- one-based `batch_ordinal` for presentation;
- an explicit source-revision cue-ID namespace;
- `valid_cue_ids` and the only actionable `batch.cues` array;
- bounded previous-output and following-source context;
- timing according to `full`, `overlap_only`, or `none`; and
- the lease token and expiry.

Context is continuity evidence, not additional work. Use the declared
`cue_id`; never guess from an SRT number or array position.

A correction result contains `kind: correction` and operations over declared
cue IDs. A translation result contains `kind: translation`, exactly one item
per actionable cue ID, and optional glossary additions. Raw `response_text`
exists for adapters that can only return model text; MCP workers should prefer
the typed result.

Pandrator validates IDs, operation shape, deletion policy, speaker decisions,
translation coverage, and glossary changes. Correction can explicitly pin an
existing translation artifact; finalization then appends a revision to that
translation lineage instead of publishing it as source-language correction.

## Passive PDF and EPUB cleanup

The document must already be a managed PDF or EPUB source attached to the
audiobook session. An agent can create a session, inspect reusable sources,
attach one with `pandrator_attach_existing_source`, or import a file returned by
`pandrator_browse_local_sources` with `pandrator_import_local_source`, and then
create a cleanup run. The operator must configure the named source root first;
the model receives only its name and relative entries, never arbitrary
host-filesystem access or an absolute path.

Creating a source-cleaning run queues deterministic preparation. PDF
preparation may include local, page-by-page OCR according to the requested OCR
settings. EPUB preparation uses the deterministic cleaned-text baseline and
retains structural metadata and navigation evidence. Neither path calls an
LLM provider.

Poll `pandrator_get_source_cleaning_dispatch_run` until the run is `ready`,
then process these phases sequentially:

1. `metadata`;
2. `navigation`;
3. `boilerplate`;
4. `repeated_elements`;
5. `chapter_marking`; and
6. `text_repair`.

Each claim contains a phase description, capabilities, document summary,
bounded evidence, candidate blocks with stable IDs, server-owned proposals,
and the exact operation types permitted for that phase. A result must decide
every proposal exactly once with `accept` or `reject`. It may also add typed
operations over evidence actually disclosed in that packet.

Heuristic candidates are navigation aids, not a permission boundary. While a
phase lease is active, `pandrator_inspect_source_cleaning_dispatch_extraction`
can browse line ranges, search plain text or regular expressions, inspect
individual blocks and context, inspect document/navigation structure, preview
selectors, review heading/footnote candidates, and request EPUB markup when
the persisted index supports it. Independent inspections can be batched. Use
`view=working` for the document after accepted earlier deletions or
`view=baseline` to compare against the original deterministic extraction. EPUB
runs additionally expose `view=source`: a read-only structured index of the
publisher markup, navigation, links, IDs, classes, and other evidence retained
before deterministic cleanup. This lets the host investigate a suspected
omission without granting raw content authority over the result.

Every returned live block ID is recorded and promoted into that leased batch's
valid evidence scope. Baseline-only blocks that an earlier accepted phase
deleted remain inspectable for diagnosis but cannot be mutated. This preserves
an audit trail without forcing the host model to trust the detector's initial
candidate set. IDs returned only by `view=source` are listed as
`source_only_block_ids`; they are never promoted and cannot be used in an
operation. To restore or repair content found only in the source view, inspect
the surrounding baseline/working extraction and use an allowed operation over
its live block IDs. If no corresponding live block exists, treat that as an
extraction defect requiring explicit review rather than silently copying raw
markup into the cleaned book.

`find_footnote_candidates` is conservative by default: it returns candidates
supported by explicit note semantics, resolved references, backlinks, note
identity, or note-file structure. A bare numeric prefix is not enough because
captions and numbered prose otherwise dominate the result. Set
`include_ambiguous=true` only when you intentionally want those additional
low-confidence numbered lines for investigation.

PDF chapter proposals deliberately exclude weak section, note-number, and
decorative-title guesses. Those headings remain in the evidence and can still
be inspected and marked explicitly. This keeps automation conservative without
hiding the material needed for a model to disagree with it.

Packets are materialized sequentially. Before exposing a later phase,
Pandrator deterministically applies all accepted earlier operations to the
persisted index. Boilerplate, repeated-element, and chapter evidence therefore
describe the current working document rather than a stale copy of the original.

| Phase | Agent-authored operation |
| --- | --- |
| Metadata | `set_metadata` using the supplied metadata-key allowlist |
| Navigation | `delete_blocks` |
| Boilerplate | `delete_blocks` |
| Repeated elements | `delete_blocks` |
| Chapter marking | `mark_chapter` or `unmark_chapter` |
| Text repair | `replace_block` for confirmed extraction defects, or `delete_blocks` for newly discovered extraction debris |

The agent cannot submit a rewritten book or an uninspected block ID. A
`replace_block` may contain up to 50,000 characters so a badly reconstructed
paragraph can be repaired without an artificial model-token budget, but it is
available only in the final text-repair phase and remains an explicit audited
operation. Broader structured selectors may appear in server-owned proposals;
the agent can inspect arbitrary selectors for evidence but cannot silently turn
an unreviewed selector into a bulk mutation.

The `evidence_limit` setting is a per-phase transport bound, defaulting to 500
items with an accepted range of 20–2,000. It is **not** a token budget or turn
budget. Use a larger value for books whose structural review genuinely needs
more candidates, while remembering that very large tool results also consume
the MCP host model's context. MCP application responses allow 8 MiB by default
and retain a 16 MiB safety ceiling.

After the sixth accepted phase, Pandrator reloads the persisted structured
index, applies all accepted operations deterministically, validates the
result, writes the rules/report/diff audit set, and registers a selected
`clean_text` artifact. The agent can then plan and execute narration
preparation through the normal workflow tools.

## Passive speech-text optimization

Speech optimization is available after an audiobook has `prepared_text` or
`clean_text`, or after a voiceover has transcription, correction, or
translation subtitles. PDF/EPUB files must be extracted first, and media must
be transcribed first. The dispatcher consumes only managed SRT, JSON, or TXT
speech text.

Create `pandrator_create_speech_optimization_dispatch_run`. Unlike document
cleanup, creation is immediately ready because there is no OCR or source-index
job. Claim batches sequentially with
`pandrator_claim_speech_optimization_dispatch_batch`.

The claim provides:

- written and target voice languages plus an optional TTS-service hint;
- the built-in meaning-preservation and speech-quality instructions;
- `valid_unit_ids` and the only actionable `batch.units` array;
- optional speaker data and one timing object per actionable SRT unit;
- accepted output from the preceding batch and following source text as
  read-only, timing-free context; and
- the short-lived lease capability.

Return `kind: speech_optimization` and exactly one non-empty `text` item for
every supplied `unit_id`, in the supplied order. Keep a unit unchanged when no
improvement is warranted. Do not translate, summarize, merge, split, omit,
reorder, or invent content. Context entries must not appear in the result.

The defaults are generous: 20,000 target source characters and 100 units per
claim, configurable up to 1,000,000 characters and 500 units. A single source
unit is never split and may exceed the target. These are transport bounds, not
token or reasoning limits. The MCP host may use one model call for the full
claim, subdivide it, or assign its own subagents; Pandrator cares only about
the validated one-for-one result.

After the final accepted batch, Pandrator materializes a normal
`tts_optimized` artifact. SRT timing and revision speakers are retained; JSON
rows retain `source_text` and receive `tts_optimized_sentence`; TXT remains
plain text. The result records no Pandrator provider or model. Review it, then
generate representative takes and listen before a full run.

This passive stage is not an interruptible worker for an already-running TTS
generation job. Finish it before generation. The native UI can instead run
standalone optimization with a configured provider or optimize final speech
units during generation. The
[speech-optimization reference](../reference/speech-optimization.md) compares
those paths and their batching settings.

## Lease, retry, and conflict behavior

- Renew a lease before expiry when work is taking longer.
- Release it when stopping so the run does not wait for expiry.
- Retry the same logical mutation with the same idempotency key.
- A stale or mismatched token cannot submit.
- Another active lease makes the run busy; phases remain sequential.
- Source, revision, selected-stage, or output-head changes fail closed rather
  than silently applying work to different content.
- `finalizing` means all batches were accepted but durable materialization did
  not finish. Retry the same final submission and idempotency key after a
  transient failure.

A validation rejection is not permission to skip a packet or mutate the
source. Repair the typed result while its lease remains valid and resubmit it.

## Security and privacy boundary

Claimed text is disclosed to the model running in the MCP host. Whether that
model runs locally or at an external provider is a property of the host, not
Pandrator. Review the host's model and data policy.

Use least-privilege MCP scopes, bind one sidecar process to one fixed target,
and keep credentials outside model-visible configuration and tool arguments.
Passive dispatch needs application read and run authority. Importing a new
approved local file also needs application write authority; browsing configured
root names does not. Neither path needs Manager mutation or Pandrator credential
authority.

## Getting started through MCP

Install and configure the sidecar, enroll one target, run `doctor`, and add the
generated secret-free stdio fragment to the host you actually use. If an agent
should start from Downloads, a mounted library, or another host directory, the
operator must first expose that directory by an opaque root name. Then ask the
agent to inspect or create the session, browse and import the relative source,
create the appropriate passive run, claim and submit sequentially, renew or
release deliberately, and inspect the finalized artifact before continuing
downstream.

Exact target, scope, host-configuration, and tool behavior belongs to the
[Pandrator MCP guide](../../pandrator_mcp/README.md). The
[subtitle pipeline reference](../reference/subtitle-pipeline.md) documents cue
and timing parameters; the
[document-ingestion reference](../reference/document-ingestion.md) documents
PDF/EPUB extraction and narration preparation. The
[speech-optimization reference](../reference/speech-optimization.md) documents
native and passive speech preparation.
