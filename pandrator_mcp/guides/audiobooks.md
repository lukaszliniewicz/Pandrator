# Audiobook workflow

An audiobook session starts from an uploaded, downloaded, or deliberately
reused document artifact. Its normal stages are:

1. **Clean source** — deterministic extraction with optional agent-assisted
   cleanup, producing reviewable clean text.
2. **Segment narration** — editable generation segments controlling text
   boundaries and pauses.
3. **Optimize narration** — optional, separate before-and-after text revision.
4. **Generate audio** — reviewable narration takes. Missing preparation may be
   included by an exact workflow plan.
5. **Assemble** — select takes and construct the intended audio sequence.
6. **Export** — package assembled audio with format, metadata, and cover choices.

Whole-document speech optimization and generation-time batch optimization are
alternative places to perform the same kind of transformation. Review the plan
and avoid enabling both unintentionally.

PDF layout/OCR, EPUB spine and navigation handling, deterministic and optional
model-assisted cleanup, artifacts, and narration segmentation are described in
the public [document-ingestion reference](https://github.com/lukaszliniewicz/Pandrator/blob/main/docs/reference/document-ingestion.md).
Cleanup may be app-executed through Pandrator's configured provider or passive:
the MCP host model can claim six sequential PDF/EPUB editorial phases, return
typed proposal decisions and bounded operations, and let Pandrator validate and
materialize `clean_text`. Passive cleanup has no provider token/iteration
budget; `evidence_limit` is a transport bound, default 500 and maximum 2,000.

For passive cleanup, create `pandrator_create_source_cleaning_dispatch_run`,
poll its durable preparation, then claim and submit phases sequentially. The
source must already be a managed, attached PDF or EPUB. It may be reused from
`pandrator_list_sources` or imported from an operator-approved named root with
`pandrator_browse_local_sources` and `pandrator_import_local_source`. Continue
with the normal workflow plan only after the final cleaned-text artifact is
selected.

Speech optimization can also be passive. After `prepared_text` or `clean_text`
exists, create a speech-optimization dispatch run, claim its sequential units,
and return one optimized text for every `unit_id`. Pandrator uses no LLM
provider or token budget and materializes the result as `tts_optimized` before
generation. Prefer this whole-document route when review is important; use
generation-time optimization only when final segment context is required, and
avoid enabling both accidentally.

Generation can send narration text to a configured TTS provider. Cleanup or
optimization can send text to an LLM provider. A useful plan therefore states
which provider receives which data before execution. Generated takes and final
exports are different artifacts; generation does not imply final assembly.
