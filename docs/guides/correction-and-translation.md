# Correction and translation

Pandrator supports four complementary language-work paths. The best choice is
the least complicated one that produces a result you can review.

| Method | Model call made by Pandrator? | Best for | Important limit |
| --- | --- | --- | --- |
| Manual review | No | Precise final editing and sensitive text | Slow for a large first pass |
| Configured LLM | Yes | Context-aware correction or translation using a local or cloud provider | Requires a working provider configuration and may send text remotely |
| DeepL | Yes | Direct translation through a dedicated translation API | LLM instructions, reasoning, and batch context do not apply |
| Passive dispatch | No | Using the capable model already running in an MCP host | The host must actively claim and submit batches |

These methods produce reviewable revisions. They do not overwrite the original
transcription or silently change text used by an already generated take.

## Correction is not translation

Correction operates in the input track's language. It can edit, delete, merge,
or split cues while preserving or deriving their timing. Translation produces
one target-language item for every actionable source cue and retains lineage
to the selected source revision.

Correct obvious recognition mistakes before translation so they are not
carried into the target language. If a translation later needs stylistic or
terminological cleanup, correct the translation artifact itself. The result is
a new translation revision in the same target language—not a source-language
correction track.

## Shared quality controls

Configured LLM work and passive dispatch share the quality-oriented batch and
editorial model:

- semantic batches stop at both a character and cue limit;
- processing is sequential by default so accepted output and glossary choices
  can flow into the next batch;
- bounded previous-output and following-source context protects boundaries;
- a deletion policy can forbid cue removal;
- explicit instructions are appended to the built-in task contract;
- stable source-revision cue IDs prevent batch-local numbering mistakes; and
- deterministic finalization handles wrapping and durable lineage.

The default is 6,000 source characters and at most 40 actionable cues per
batch, with eight previous output cues and two following source cues as
non-actionable context. Reduce batch size before removing continuity context
when quality matters more than latency.

## Timing disclosure

Timing can help a model avoid merging across a large pause or misunderstanding
an overlap, but repeating it wastes tokens. Pandrator exposes it at most once
per actionable cue:

- `full` supplies start/end time and the relevant preceding gap or overlap;
- `overlap_only` supplies only positive overlap evidence; and
- `none` supplies no timing key or timing value.

Boundary context never contains timing. Timing disclosure guides editing; it
does not hand subtitle retiming over to the model.

## Glossaries and research

Manual glossary entries are authoritative. An LLM translation can propose new
glossary additions, but it cannot silently replace the mappings you supplied.
Review consistency across batches, especially for names, titles, honorifics,
technical vocabulary, and repeated phrases.

Optional Jina research is a separate bounded grounding step. It can help
resolve uncertain names and terminology and retains a source ledger. Research
evidence is not editable subtitle text and should not be treated as proof that
the resulting translation is correct.

## LLM concurrency

Sequential execution is the quality-first default. Higher native LLM
concurrency can reduce elapsed time, but separate batches then lose some
previous accepted output and evolving glossary continuity. Passive dispatch is
sequential. Increase concurrency only after deciding that latency matters more
than cross-batch consistency.

## DeepL differences

DeepL starts from the same deterministic cue mapping and safely repacks text
for its request limits, but it is not prompted like an LLM. LLM model,
reasoning, context, timing, instruction, deletion, and batch controls therefore
do not apply to DeepL itself and are hidden when it is selected.

## Review before continuing

For correction, inspect deletions, cross-speaker merges, punctuation, names,
and split timing. For translation, inspect omissions, glossary consistency,
speaker labels, reading speed, and whether a natural target-language phrase can
fit the source window. Select the exact reviewed artifact before generating
speech or exporting.

For the no-extra-API workflow, read [passive dispatch](passive-dispatch.md).
For exact parameters and cue operations, read the
[subtitle pipeline reference](../reference/subtitle-pipeline.md).
