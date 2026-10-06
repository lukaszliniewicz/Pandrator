# Passive semantic dispatch

Pandrator owns source snapshots, preparation, leases, validation, and final
artifacts. The host model owns language/editorial decisions within the user's
request. No Pandrator LLM provider is called by these passive runs.

## Efficient packets and results

Inspect the connected tool schema before using these controls; an older local
installation may expose fewer options. Subtitle, speech-optimization and
performance claims, and orchestrated plans, use `packet_format=compact` by
default. Request `standard` when object fields or compatibility are needed.

For a compact claim, retain `manifest` with its `manifest_hash`. Supply
`known_manifest_hash` on later claims only when the exact manifest is still
available to the processing model. Omit it after a restart or lost context.
When delegating, supply the manifest to the worker; a parent's cache alone is
insufficient. A changed hash brings an updated manifest, including changed
character dictionaries or policies. Decode subtitle rows with `cue_columns`
and `turns`. Speech manifests supply task fields and `character_dictionary`;
performance manifests supply batch policy/schema fields alongside `batch.items`.
The current batch, boundary context and capsule still arrive on every claim.

On tools advertising `response_mode`, choose `structured` only if the host
consumes `structuredContent`; full data appears there once and text is a short
receipt. Text-only hosts need `standard`. This transport choice is separate
from compact packet format and editorial context.

Submit the smallest valid typed result: correction operations only for changes
(an empty list keeps every cue); ordered translation items for every expected
ID; speech items containing text or XML; performance annotations or XML without
transcript edits; source-cleaning proposal decisions and necessary custom
operations; recording cuts using boundary IDs and reasons. Use advertised
grouped correction/translation forms when helpful. Do not echo source text,
timing, policies or the claim packet. Return only new information in optional
context deltas, glossary updates or character proposals.

After an accepted submission, continue its next action. Do not fetch the whole
run or plan after each batch just to confirm the receipt. Inspect details when
validation fails, progress is uncertain, finalization needs recovery, or the
completed artifact is due for review. Retain exact retry arguments and keys;
compact output is not permission to discard recovery state.

## Choose context at setup

Keep the live context defaults when continuity is uncertain. One model can
choose `context_before=0` if the preceding accepted output remains available
and the new batch is understandable without a repeated boundary excerpt.
Use neighbouring context and a capsule for pronouns, quotations, speaker turns,
terminology, parallel workers and cold resumes. `context_after` supplies unseen
following source; choose it independently. Zero disables the corresponding
boundary arrays, not the actionable source or capsule.

In an orchestrated plan, subtitle controls belong in `overrides.correction` or
`overrides.translation`; speech controls belong in `overrides.text`:
`context_before`, `context_after`, `include_timing`, `char_limit`, and the saved
`llm_tts_document_batch_size` setting. Direct create tools expose their typed
controls. Performance also has `context_max_chars` for bounded excerpts.
Choose subtitle `timing_context_mode=none` or speech `include_timing=false` only
when reading speed, pauses, overlap or recording timing do not affect the task.
Batch character limits cover source text, not the entire packet or model token
budget; leave room for manifests, context and the required result.

## Shared context and execution

For substantial work, maintain a compact capsule: topic, languages, names,
terminology, register, punctuation, speaker state, permitted removals, and
uncertainties. Verify and merge worker context deltas; do not let independent
lanes silently diverge on the glossary.

Subtitle and speech-optimization runs default to serial processing. They also
support `execution_mode=parallel` with `max_parallel_batches` from 2 through 8.
Only use this when the host supports delegation and the material tolerates
bounded parallel work. Finish every sibling in a wave before claiming the
next wave. PDF/EPUB cleanup phases remain sequential. Recording-edit dispatch
uses one whole-recording batch and its own review procedure.

Claimed packets contain actionable items and read-only context. Preserve the
provided identities; a display ordinal or SRT number is not necessarily a cue
ID. Renew leases during long work, release abandoned packets, and submit only
under a valid lease. On interruption, inspect the existing run and resume its
remaining work instead of creating duplicate runs.

Retry an uncertain logical submission with its original idempotency key.
Repair validation errors under the same valid lease, following returned retry
actions. If a run is `finalizing`, inspect its state and follow the indicated
recovery action; do not assume that creating another run will recover it.

## Subtitle correction and translation

Use `pandrator_create_dispatch_run`, then the matching
`pandrator_claim_dispatch_batch`, `pandrator_renew_dispatch_batch`,
`pandrator_release_dispatch_batch`, and `pandrator_submit_dispatch_batch`
actions. Inspect progress with `pandrator_get_dispatch_run`.

**Correction:** preserve meaning and source language. Return typed `edit`,
`delete`, `merge`, or `split` operations over the actionable cue IDs. Correct
ASR errors and punctuation; remove fillers or repetitions only under the
requested removal policy. Preserve meaningful hesitation, quoted speech,
and legitimate speaker style. Do not translate or invent missing content.

Preserve verified speaker labels and boundaries when diarization exists. Do
not add fictional labels when it does not. Flag uncertainty when a correction
needs audio; consult the dispatch audio-evidence request/get/resolve tools
advertised by the server when available.

**Translation:** return every expected cue exactly once, in source order,
using the batch's cue IDs. Preserve meaning, terminology, register, and
uncertainty. Keep subtitles readable without adding explanations. A correction
run can target an existing translation artifact; inspect the pinned artifact
and language before treating it as original-language correction.

Use web research only when available, appropriate for the content's privacy,
and useful to resolve a specific uncertainty such as a name or quotation.
Record source and confidence. Searchable text is not proof of what the speaker
said; preserve uncertainty where the evidence does not settle it.

## PDF/EPUB source cleanup

Create a run with `pandrator_create_source_cleaning_dispatch_run` against a
managed source attached to an audiobook session. Wait for deterministic
preparation, then follow the source-cleaning claim/renew/release/submit actions.

The six ordered phases are `metadata`, `navigation`, `boilerplate`,
`repeated_elements`, `chapter_marking`, and `text_repair`. Work only on the
current phase. Use the typed result schema and supported operations such as
`set_metadata`, `delete_blocks`, `mark_chapter`, `unmark_chapter`, and
`replace_block`; do not substitute subtitle edit operations.

Use `pandrator_inspect_source_cleaning_dispatch_extraction` under a valid lease
when claimed evidence is insufficient. Inspect only the bounded relevant
extraction. Do not guess unseen text or perform document-wide deletions based
on a short preview. The server may supply proposal IDs for operations that
cannot be reconstructed through the custom-operation schema; accept or reject
those proposals using their documented IDs.

Keep source meaning and the user's footnote/citation policy. Cleanup prepares
the text for narration; it is not translation or permission to abridge the book.
Inspect the final artifacts and review material before narration preparation.

## Speech-text optimization

Use `pandrator_create_speech_optimization_dispatch_run` and its matching
claim/renew/release/submit/get actions. This processes managed text after
extraction or transcription and before TTS. It does not join an already-running
generation job.

Return every expected speech unit exactly once, in order, with nonempty
optimized text or valid speech XML and the exact supplied identity. XML-only
items let the server derive the transcript; preserve existing authored markup
and the unchanged transcript in annotation-only mode. Respect the target language,
voice language, TTS service, and instructions. Preserve timing, speaker
identity, meaning, and source structure; change how the text is spoken rather
than translating, summarizing, or rewriting the document. The finalized
`tts_optimized` artifact is separate from the source.

## Before submission

Check run/batch identity, required item coverage, unique IDs, ordering where
required, operation shapes, deletion policy, and exclusion of context-only
items. Mechanical acceptance is not a semantic quality review: also check
meaning, terminology, and continuity across the batch boundaries.
