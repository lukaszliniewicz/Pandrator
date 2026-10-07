# Multiple-voice audiobooks

Prepare the text and speaker identities first, then choose or design voices for
those identities, bind the cast, and generate. Speaker attribution preserves the
accepted words. Pronunciation changes and performance directions are separate,
optional passes; multiple voices do not require either.

## 1. Prepare and identify speakers

Create or reuse an audiobook session and attach its source. Read
`pandrator_get_voice_setup`, then use its revision with
`pandrator_configure_voice_setup(mode="multi_voice")` and an idempotency key.
This selects voice mode and starts no work.

Clean the source as needed and run `prepare_text`. For passive annotation, use
its **prepared_text JSON** artifact with
`pandrator_create_speech_optimization_dispatch_run`,
`annotation_mode="speakers"`, and `annotation_only=true`. Plain `clean_text`
cannot supply the structured units required for speaker annotation.

Claim and submit units in order, retaining stable speaker IDs across batches.
For untouched text, prefer the advertised annotation-span result: submit each
unit's ID, source hash, and dialogue ranges. Gaps remain narration. Use XML for
units with existing authored markup or directions. Reporting clauses such as
“he said” belong to the narrator; do not rewrite words or punctuation.

Submit new identities as outer `character_proposals` alongside the batch result,
using `id`, `display_name`, aliases, category, and concise `notes` grounded in
the text. Accepted proposals merge into the session dictionary atomically with
the annotation. Keep uncertain attribution explicit; do not invent a named
speaker just to fill an unknown span.

Finish the run and verify the selected generation input. Use
`pandrator_get_workflow_inputs` and the revisioned
`pandrator_configure_speech_optimization` operation for document mode when
needed, then select the exact structured result. Completing annotation and
selecting that result for generation are separate state changes.

## 2. Use the derived cast list

Read `pandrator_get_generation_controls` once after annotation completes. Its
`characters` list is the derived speaker dictionary, including the submitted
notes. Use those IDs when designing and assigning voices; narrator is a separate
cast role. Do not reconstruct or replace the dictionary from memory.

The response currently supplies identities and notes, not per-speaker dialogue
counts or example passages. Retain a few representative lines from annotation
when useful for auditions. Distinguish evidence from desired design traits:
text may establish age or temperament without establishing an accent or pitch.

Existing accepted or locked characters must survive later chapters. Resolve
proposed identities and aliases before voice work. Avoid running another
speaker pass over already accepted attribution unless it needs repair.

## 3. Find or design reusable voices

Choose the generation renderer independently of the design model. For example,
Breeze may design reference audio that Qwen Base later clones. Inspect only the
relevant model capabilities and missing voice references; keep catalogue queries
filtered by renderer, language, readiness, and a bounded limit.

Reuse a suitable saved voice unless new design was requested. For each voice
that needs design:

1. Call `pandrator_audition_voice` with the design service/model, a representative
   sample passage, and `generation_prompt`. Follow its durable work, listen, and
   review the exact sample transcript. Keep the character ID associated with the
   returned artifact and voice IDs in the host's working notes; audition itself
   has no session-role field.
2. Create managed metadata with `pandrator_create_voice`. When advertised, use
   `pandrator_setup_designed_voice` to promote the reviewed preview and publish
   or link its normalized reference to the **generation** service. Supply the
   returned voice revision and `transcript_reviewed=true` only after review.
3. Follow the setup work handle and resume action until `stage="ready"`. Its
   receipt contains the managed `voice_id`, provider voice ID, sample identity,
   and current revision. Reuse that receipt; do not re-list the entire catalogue
   after every voice or create another candidate merely because setup is queued.

The lower-level promote/import/publish tools remain useful for individual steps
and recovery. Voice design never assigns a project role automatically.

## 4. Assign and verify the cast

Build one complete cast update from the current controls plus all reviewed
voice receipts. Preserve existing category, source-speaker, narrator, and
character bindings. `pandrator_update_generation_controls` replaces a supplied
`cast` object; it is not a single-role merge. Omit `characters` when only changing
voice assignments. Example fragment:

```json
{
  "cast": {
    "narrator": {"voice_id": "returned-narrator-voice-id", "service": "audio_cpp", "model": "selected-generation-model"},
    "characters": {
      "c-fred": {"voice_id": "returned-fred-voice-id", "service": "audio_cpp", "model": "selected-generation-model"}
    },
    "categories": {},
    "source_speakers": {}
  }
}
```

Use actual returned IDs and the current `expected_revision` with an idempotency
key. The update returns the complete current controls and new revision; an
immediate `get_generation_controls` call is unnecessary. A conflict requires a
fresh read and deliberate merge, not an overwrite.

Check that every role intended to have a distinct voice is assigned. A known
character without an explicit binding can use a category, narrator, or base
voice; successful preparation alone does not prove the cast is complete.

Prepare/select the speech-plan revision. Compile representative mixed-speaker
blocks with `pandrator_preview_speech_segment(session_id, revision_id, segment_id)`.
Inspect resolved voices and fallback flags, including returns to narration.
This creates no performance draft and synthesizes no audio. The default response
is compact; `include_request=true` is for provider diagnosis. A short acoustic
preview remains a separate check when needed.

## 5. Generate and deliver

Review the selected plan and generate it. Retain the returned work ID and use
`pandrator_get_work(wait_for="terminal", wait_seconds=30)` when the transport
permits. Assemble the selected completed takes, then export and download the
requested result. Generation, assembly, and export are distinct outcomes.

If delivery analysis is wanted, run it after accepting speaker structure. Qwen
Base cloning does not support instruction-based delivery on this route, while
speaker switching and reference voices still work. Do not require a performance
pass solely to assign multiple voices.

Narration length uses model-specific application budgets with headroom. Preserve
short dialogue and paragraph/chapter boundaries. Changing the preparation policy
affects new plans; use the immutable resegmentation review flow for existing ones.

Use `pandrator_patch_session_settings` for partial settings changes; PUT-style
`pandrator_update_session_settings` replaces a complete override section. Avoid
repeated discovery and full-settings retrieval when existing receipts already
supply current revisions and identities.
