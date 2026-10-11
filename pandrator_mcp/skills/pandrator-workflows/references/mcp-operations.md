# MCP operations

Live tool schemas, target capabilities, and returned plans are authoritative.
Use `pandrator_explain_system` for the packaged workflow-specific guide when
an operation is unfamiliar or the server differs from this reference.

## Discovery and source setup

- Inspect `pandrator_get_target_status` and `pandrator_get_capabilities` for
  connectivity, granted scopes, and approved source-root names.
- Use `pandrator_get_session`, `pandrator_get_workflow`, and
  `pandrator_get_session_settings` for the relevant session. Reuse a source
  only when its role, lineage, and revision match the requested work.
- Browse an approved root with `pandrator_browse_local_sources`, then pass the
  returned relative path to `pandrator_import_local_source`. The sidecar
  transfers bytes and attaches the source without exposing them to the model.
- Filter `pandrator_describe_parameters` with `sections`, `names`,
  `workflow_kind`, or `query`; at least one filter is required.
- Inspect provider, TTS, and voice catalogues only when those choices matter.
  Use actual service/model/voice IDs, not labels guessed from user examples.
  For routine voice choice, use `get_voice_catalog` with `service_id`, `model`,
  `view="compact"` and `response_mode="structured"`. Optional `fields` projects
  only requested safe fields; full profiles remain available when relevant.

For new local speech work, the catalogue recommends `audio_cpp`; XTTS,
Silero, Kokoro, and Voxtral remain dedicated providers. Compatibility entries
are hidden by default. Retrieve a saved provider with `service_id`, or use
`include_compatibility=true` when needed. Preserve an existing selection unless
the task calls for a switch. For an explicit switch, inspect the target model
and voice IDs and use `pandrator_configure_tts`; do not copy old provider
options or assume that an uploaded voice is available in audio.cpp. Cloning
there needs a ready managed reference link.

For a designed voice, audition first and review the actual sample and transcript.
When advertised, `pandrator_setup_designed_voice` promotes that reviewed preview,
then links or publishes its exact normalized sample. Set `transcript_reviewed=true`
only after review, retain the original arguments and key, and follow its work
handle/resume action through both stages. Identical current references and native
links can be reused; changed bytes, transcript, language or preparation profile
need fresh preparation. Native links use the managed voice's current reference.
On expired retry state or revision conflict, inspect the existing sample and
registration before starting another setup.

For speech optimization, inspect `pandrator_get_workflow_inputs` and use the
advertised `pandrator_configure_speech_optimization` operation to set off,
document, or inline processing with the returned outcome and text revisions.
This synchronizes the saved workflow and text controls. Annotation-only speaker
work uses document mode. Audiobook generation resolves `prepared_text` or
`tts_optimized`; select an exact revision with `pandrator_select_workflow_input`
when adoption is needed. A completed passive artifact and an enabled workflow
step are separate state: verify the resolved generation artifact before preparing
the speech plan.

Application read, write, run, and cancel scopes are distinct. Passive work
normally needs read and run; importing a source also needs write. Inspect
missing-scope errors rather than broadening permissions automatically.

## Passive stages followed by generation or export

`pandrator_plan_orchestrated_workflow` returns a live procedure for passive
`correction`, `translation`, and `speech_optimization` before `generate_audio`
or `export`. It is not an immutable transaction: accepted language work changes
artifact revisions.

Follow each phase's returned create/claim/submit actions and the
[semantic contracts](semantic-dispatch.md). Wait for the artifact to finalize.
Then obtain a fresh `pandrator_plan_workflow`, inspect its effects, reuse,
providers, locks, and confirmations, and execute the unchanged plan/digest
through `pandrator_execute_workflow_plan` with an idempotency key. Re-plan on
revision conflicts, expiry, or a stale digest.

## Multiple-voice narration

Use the packaged `multivoice-audiobooks` guide for the complete tool sequence.
Reuse accepted speaker attribution. When attribution is needed, annotate the
prepared structured text before designing voices, submitting `character_proposals`
alongside annotation results. Read `pandrator_get_generation_controls` once after
the run completes; its `characters` are the derived cast list with stable IDs and
notes. Keep a few representative lines from the annotation for auditions when
useful; the dictionary itself has no usage counts or sample passages.

Choose/design voices for those roles. Design may use a different model from
generation. Follow `pandrator_setup_designed_voice` through promotion and renderer
publication, retaining each ready receipt and its role association. Then save one
complete cast update, preserving existing mappings. A supplied `cast` replaces
that section. The update returns current controls/revision, so an immediate read
is unnecessary. Check all intended roles and compile a mixed-speaker preview:
an unassigned known character can fall back to the narrator or default voice.
Delivery analysis and pronunciation optimization remain independent.

## Durable work and cancellation

A returned `work` handle is not a finished artifact. Poll `pandrator_get_work`
with that handle until terminal; retain it across timeouts and interruptions.
Prefer waits short enough for the host's request timeout and progress needs.
Use `pandrator_get_work_log` for diagnosis rather than repeatedly dumping logs.
For generation, prefer `pandrator_get_generation_operation` with the stable
`generation_operation_id`; it follows preparation and synthesis and reports
selected-run audio coverage. See [recovery](recovery.md) for legacy servers.

Use compact generation admissions and run lists for routine orchestration. Request
`view=full` when inspecting frozen settings. Retain run, job, plan and work IDs
with errors and progress; save large detailed receipts privately when needed.
Changing this presentation view does not change a generation mutation or its
idempotency key. Avoid repeatedly fetching full settings or catalogues while
polling work that already supplies its identity and status.

Call `pandrator_cancel_work` once when cancellation is requested, then inspect
until terminal. Quick transcription has its own polling and cancellation tools.

## Generation and delivery

For routine inspection, list runs with `view="compact"`; set `generation_run_id`
to retrieve a retained run directly. List speech plans with `view="summary"`,
`include_units=false`. Read only affected units with `view="annotations"`,
`include_units=true`, and `segment_ids` (up to 100 unique IDs); otherwise page with
the returned cursor. `full` retains XML and detailed snapshots for diagnosis.
Segment `fields` is an advertised enum, not an arbitrary field list. A historical
segment read returns `effective_take` with its originating run, artifact and hash,
including reused audio. Current active audio is a separate selection.

`pandrator_regenerate_segments` and `pandrator_generate_speech_plan` default to
`settings_source="current"`. Use `settings_source="run"` with
`settings_generation_run_id` to reproduce frozen settings, or `matching` for the
latest compatible snapshot. `generation_run_id` identifies the audio origin;
it does not select historical settings. A missing historical snapshot fails
instead of substituting current settings. Returned origins make this explicit.

1. Inspect the live TTS and voice catalogues, then use
   `pandrator_configure_tts` against current settings. Preview and review a
   representative voice sample before a long synthesis when appropriate.
2. Execute the reviewed generation plan and wait for terminal work.
3. Inspect `pandrator_list_generation_runs` and
   `pandrator_list_generation_segments`. Use
   `pandrator_update_generation_segment` for actual text, voice, or language changes,
   `pandrator_regenerate_segments` for new takes, and `pandrator_select_take`
   for selection. Timing/block changes may require
   `pandrator_revise_speech_block_plan`; inspect its contract first.
4. Assemble the selected completed run with
   `pandrator_assemble_generation_run` and wait for it to finish.
5. Create one `pandrator_plan_export_variant` per requested output, inspect
   its media/text/subtitle options, and execute the exact plan with
   `pandrator_execute_workflow_plan`. Poll again.
6. Locate outputs with `pandrator_list_artifacts` and deliver them with
   `pandrator_download_artifact`. Supply an optional plain `filename`, not a
   destination path. The configured output root determines the destination.

The downloader resumes partial transfers and verifies size and SHA-256. It
refuses conflicting existing content. Report its final path and verification
result; do not treat a remote artifact as an already-delivered local file.

## Comparing selected takes

Use `pandrator_create_comparison_export` with two `generation_run_ids` and explicit
`segment_ids` for all lines from the first run followed by all lines from the
second. Or pass ordered `clips` with run, segment, `audio_version_side` and optional
exact `take_id`. Recast sides use their pinned selections; ordinary runs use the
last eligible take at that run's cutoff. Missing selections fail rather than
using current audio. Keep one idempotency key and poll the returned work.

The output preserves loudness, uses an explicit `gap_ms`, and creates audio plus
a manifest recording chosen takes, hashes, clip boundaries and settings origins.
It compares speech takes; separately rendered environmental SFX are excluded.
Use the returned artifact IDs with the checked downloader for delivery.

## Recording edits

Use `pandrator_plan_media_edit_workflow` for a live procedure that inspects
source setup, transcript/caption timing, preparation, proposal, review, and
rendering. If needed, create a `media_edit` session and attach the recording as
`primary` and captions as `transcript`. Follow the planner's next action after
each revision change.

Attached captions remain authoritative. A conflict between attached captions
and explicitly requested ASR is a blocking choice to resolve, not permission
to discard the captions. CTC alignment aligns existing text; ASR generates a
new transcript.

For in-harness cut proposals, follow the
`pandrator_create_media_edit_dispatch_run` family: it pins one revision and
leases **one whole-recording batch**. Submit cuts using the claimed cue IDs
or documented media-boundary flags. Do not partition it into unrelated subtitle
batches or invent timestamps absent from the evidence.

A proposal is not an approved edit. List cuts with
`pandrator_list_media_edit_cuts`, inspect relevant start/end edges with
`pandrator_inspect_media_edit_boundary`, and refine one edge at a time through
`pandrator_refine_media_edit_boundary` when needed. Re-list and inspect changed
revisions before the gated `pandrator_update_media_edit(reviewed=true)` action.
Render only a reviewed revision. Apply the user's review policy and obtain
any missing approval required by the returned action. Preserve the original
recording and verify the requested rendered outputs.

For delivery/emotion changes on a whole segment or part, use
`pandrator_preview_speech_selection` then `pandrator_apply_speech_selection`
with its preview token, or edit and adopt a performance plan. Inspect the full
TTS catalogue before `pandrator_configure_tts`; the setup tool can save directions,
Gemini context windows, vocalization opt-in, and model-specific ElevenLabs voice
settings together. Read `generation-controls` and `providers-and-voices` for
exact tool contracts. Do not put native provider tags in the saved transcript.

## Pronunciation and rendering from completed audio

- `list_pronunciations` filters written forms, language, review status and scope.
  `create_pronunciation` uses `source_form` plus `phonetic`; session scope requires
  `session_id`. Update/delete use the entry's `revision` as `expected_revision`.
  Creation has no idempotency key: after an uncertain create, search for the entry
  rather than creating it again blindly. PATCH omission differs from explicit null.
- Dictionary edits preserve existing recordings and manual speech overrides.
  Re-prepare speech text and refresh changed audio to apply rules to saved plans.
  For a one-off speech-only correction, `update_generation_segments` supports
  `optimized_text`; it preserves display/subtitle words. Do not use `text` unless
  the displayed wording should change too.
- For a display/subtitle repair with audio to retain, capture each affected
  segment's exact effective speech first. Where the live contract supports a
  combined update, submit corrected `text` with that speech as `optimized_text`;
  a display edit alone can invalidate a speech override. Check the spoken text
  and selected audio are preserved. Historical runs pin their earlier display
  revision: inspect the export's text snapshot rather than assuming a current
  display edit changes a historical-run export.
- For existing audio, prefer `plan_export_variant` with the exact run ID and
  `response_mode="structured"`. It creates an immutable export-only snapshot
  without language/synthesis prerequisites. `plan_workflow(continuation=false)`
  provides the same direct-stage control with arbitrary safe settings overrides.
  Orchestration accepts `generation_run_id` for export from completed audio;
  it rejects upstream passive stages with that historical selection.
- Plan creation persists a snapshot; execution is separate. Review its exact
  effects/confirmations and follow the canonical execute next action. Structured
  responses preserve the whole plan once without repeating it in text.
