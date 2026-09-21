# Audiobook workflow

For narrator-plus-character casting, use the `multivoice-audiobooks` guide.
Start with `pandrator_get_audiobook_setup` and the atomic
`pandrator_configure_audiobook` mode switch. Compiler inspection is available
through `pandrator_preview_speech_segment`; no performance draft is needed.

An audiobook session starts from an uploaded, downloaded, deliberately reused,
or inline text source. For short text supplied directly by the user, use
`pandrator_create_text_source` to create and attach a managed UTF-8 source
without first writing a temporary host file. Its normal stages are:

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

Session settings writes are revision guarded. Use
`pandrator_patch_session_settings` for ordinary partial edits: it shallow-merges
the submitted top-level fields into the stored override, leaves omitted fields
in place, and replaces nested values as whole fields. Use
`pandrator_update_session_settings` only when the complete override should be
replaced; omitted overrides are removed. Neither operation copies inherited
defaults, and an explicit `null` remains a stored value.

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

Speaker/dialogue annotation specifically requires the **prepared_text JSON**
artifact. Set `annotation_mode=speakers` and `annotation_only=true` to retain
the exact words. Plain `clean_text` remains eligible for ordinary text cleanup,
but cannot supply structured speech-plan units.

Generation can send narration text to a configured TTS provider. Cleanup or
optimization can send text to an LLM provider. A useful plan therefore states
which provider receives which data before execution. Generated takes and final
exports are different artifacts; generation does not imply final assembly.

For deterministic speech-block review, use `pandrator_list_generation_segments`
to inspect provenance, alignment groups, and the active plan revision. Apply
only typed immutable topology edits with
`pandrator_revise_speech_block_plan` (split, merge, restore, or resegment), then follow
its next action to re-list the new revision.

Split and merge preserve exact speaker/narrator spans and delivery markup.
For a larger repair, use `action=resegment` with 1–100 contiguous `segment_ids`
in reading order. Choose `max_chars` (40–4,000; default 300), or explicit
`boundaries`: increasing Unicode code-point offsets into the selected texts
joined with one space. An empty boundary list merges the range. The range is
limited to 24,000 characters and must have matching display/speech text and
speaker, voice and language overrides. Keep chapter headings outside it.

Resegmentation creates an inactive draft with a before/after preview and source
offset lineage. Inspect that revision, then use `action=restore` with its
`target_revision_id` to adopt it. Adoption checks that the original plan has
not changed and the session is idle. Drafts cannot be generated directly.
This operation changes boundaries, not wording: correct invented punctuation
separately. Unchanged blocks outside the range retain take lineage; affected
blocks need generation and fresh performance review. A new performance plan
can use `copy_from_id` to retain annotations on demonstrably unchanged blocks.
If trimming would discard a vocal event, the edit is rejected for explicit repair.

Performance previews report the generation revision and source artifact. Verify
these before generation: creating a later optimized artifact does not select it
as the generation source automatically.

## Optional contextual performance

After selecting the accepted speech plan, `pandrator_create_performance_plan`
adds a separate pSSML delivery draft, not another text rewrite. Use `mode=passive`
to claim/submit bounded performance batches in the MCP host, `mode=llm` for a
queued configured-provider job, or `mode=manual` for explicit annotation edits.
The planner sees surrounding semantic text and should intervene sparsely. It
must not merge, split, rewrite or add spoken content. For narration, supply a
stable narrator description and optional scene/chapter guidance rather than
assigning an independent emotion to every sentence.

Inspect `pandrator_get_performance_plan` and
`pandrator_preview_performance_plan` before adopting the saved version with
`pandrator_adopt_performance_plan`. Phrase anchors target the exact accepted
spoken text, including pronunciation changes. Manual locks survive reanalysis;
adopted plans require an editable copy before changing them. Analysis does not
adopt itself or start speech synthesis. The separate pronunciation optimisation
option remains independent.

General direction and Gemini before/after text context also work without this
pass. Generation freezes the adopted annotations and context. Vocalizations are
off by default; no previous generated audio is used as an implicit voice prompt.
Explicit block changes require fresh performance review. Automatic split/regroup
passes are disabled while performance or semantic context is enabled.

## Multiple voices and audio drama

Read `voice-casting` and `generation-controls` before preparing a cast audiobook.
These guides cover voice discovery/design, persistent character identities,
compact speech XML, and dialogue-only annotation that preserves accepted text.
Speech directions are optional and independent of assigning multiple voices.
