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
  For a public video page or media URL, `pandrator_import_source_url` downloads
  on the Pandrator host and returns a work handle.
- A transcription plan names the effective recognition engine, which may be
  inherited from saved settings. Hotwords do not reliably fix names (with
  Parakeet they made no visible difference in testing); plan to correct names.
- Filter `pandrator_describe_parameters` with `sections`, `names`,
  `workflow_kind`, or `query`; at least one filter is required.
- Inspect provider, TTS, and voice catalogues only when those choices matter.
  Use actual service/model/voice IDs, not labels guessed from user examples.
  For routine voice choice, use `get_voice_catalog` with `service_id`, `model`,
  `view="compact"`; choose `response_mode="structured"` only when the
  host consumes `structuredContent` (otherwise use `standard`). Optional
  `fields` projects only requested safe fields; full profiles remain available
  when relevant.

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

## Several languages

One source session feeds a translation project; each language is a branch,
itself a voiceover session with pinned copies of the source text and timeline.

1. If the recording is cut, the source is a `media_edit` session (its branches
   are still voiceover sessions). Render the reviewed media edit first
   (`pandrator_render_media_edit`) and wait for it. Correction comes after:
   select the render's `media_edit_subtitles`, not the pre-edit transcript.
   `pandrator_create_dispatch_run` warns when the source is on the pre-edit
   timeline; a correction there does not reach the edited video, and the
   translation project refuses its text.
2. After the correction finalises, read `filler_check` from
   `pandrator_get_dispatch_run`. Passages listed under `fillers_remaining`
   kept an "uh" or "um"; fix them with `pandrator_patch_subtitle_passages`.
   Every translation copies what the source says, and later source edits do
   not reach existing branches.
3. `pandrator_create_translation_project` takes the corrected checkpoint and
   returns a work handle, not the project. Poll `pandrator_get_work` to
   terminal, then read `pandrator_get_translation_project` (summary by
   default, `view="full"` on request). Add languages with
   `pandrator_create_translation_branches`.
4. Translate, generate or export several branches at once with
   `pandrator_preview_translation_project_operation` and
   `pandrator_execute_translation_project_operation`, or work in one branch.
   Edit passage-based translations with `pandrator_patch_subtitle_passages`.

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
Every server-side wait stops at the process ceiling, 25 seconds by default, and
reports `timed_out`; poll again while it is true instead of raising the request.
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

Queued work reports `queue`: its `lane`, `position` (1 is next), a `reason`
(`session_busy`, `resource_busy`, `worker_busy` or `scheduled`) and, where the
caller may see it, `blocked_by`. Waiting behind a render or another session's
job is normal; do not cancel and resubmit. A job first in line with
`worker_busy`, no `blocked_by` and nothing else running may have no worker:
tell the operator. Running generation adds `retry`
(provider retries in flight) and, after 10 blocks or 2 minutes, `estimate`
with `remaining_seconds`. A workflow plan's `generate_audio` step carries
`estimated_seconds` when comparable history exists.

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
`settings_source_run_id` to reproduce frozen settings, or
`settings_source="matching_run"` for the latest compatible snapshot.
`settings_source_run_id` is valid only with `settings_source="run"`.
The named run may come from an earlier revision of the same speech plan in the
same session (its frozen TTS settings are replayed and each take's identity is
still checked). When it does, the result carries `settings_source_diff`: the
material `tts` keys whose run value differs from today's resolved value, as
`{key: {"run": …, "current": …}}`. Credentials and keys that never change the
audio are left out.
`generation_run_id` identifies the audio origin;
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
   `pandrator_execute_workflow_plan`. Omitting `audio_mode` uses the session's
   effective value; check it and its origin (a voiceover over sound normally
   wants `mixed`). Read `speech_timing`: `timing_drift` gives the worst block
   and cue lag and the late cues, `tail` says whether speech outruns the
   video. `synchronization_speed` defaults to 1.15 and the plan warns above
   1.2; for a language that runs long, condense the text rather than raise the
   speed. Older subtitles with overlapping cues can be trimmed at export with
   the subtitle setting `clamp_overlaps`. For a requested bilingual video,
   `subtitle_selection="dual"` selects source then translation, provided
   both documents exist: `subtitle_mode="soft"` retains separate selectable
   tracks, while `subtitle_mode="burned"` makes one bilingual overlay.
   Do not substitute a missing language or confuse this with speech-only text.
   Pass the run's `generation_run_id`; without it the export uses the
   assembly made from the active speech plan. Poll again.
6. Locate outputs with `pandrator_list_artifacts` and deliver them with
   `pandrator_download_artifact`. Supply an optional plain `filename`, not a
   destination path. The configured output root determines the destination;
   without one, ask the operator to set it on the MCP host.

After a subtitle change that only moves timing, prepare the speech plan again
(every take is kept), assemble with `pandrator_assemble_generation_run` without
a `generation_run_id`, and plan the export without one. A new generation run
would synthesise every block again; the plan warns when every take is reusable.

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

## Automatic timing repair and history (0.14.2)

For voiceovers, an enabled early-finish check may run **one** automatic pass after
synthesis. It can split an early speech block at a whole-cue boundary while
reusing unaffected takes; changed semantic-context neighbours may be refreshed.
A candidate whose split gain cannot cover the pause between the new halves is
skipped as `neighbour_growth_precheck` before any call. A refreshed
neighbour whose take comes out longer is accepted while a forward placement
shows it does not push later speech; otherwise the attempt is rejected as
`context_neighbour_added_delay`.
A rejected change leaves the original selected audio active. Applied repairs
may activate a new plan revision, while the original takes remain available.
Repair takes precedence over passage regroup, and regroup is also disabled
with semantic text context. Performance or casting plans are not automatically
repaired.

Inspect the root and child generation history before interpreting the result:
`completed` means repairs were applied and the pass finished, `partial`
means applied and rejected repairs were mixed or a pass stopped before
inspection finished, `rejected` means checked attempts were rejected
without an applied change, `no_effect` means blocks were checked
but required no split, and `skipped` means **no block was checked**. `failed`
and `stopped` retain the last accepted plan/audio. A `skipped` result is
a fail-closed pass with no checked blocks, not proof that timing was reviewed.
Inspect checked, candidate,
attempted, applied and rejected counts, plus `pass_completed` and skip reason;
a successful synthesis job alone does not establish repair completion.
`timing_repair.usage` is the second pass's own spending and `usage_total` is the
whole run; `effect` reports late cues and worst lag before and after for the
blocks the pass touched.

An explicit check of a completed voiceover is available through
`pandrator_repair_generation_timing`. `preview=true` (the default) queues a
read-only `generation.repair-preview` job and returns its work handle; poll it
with `pandrator_get_work` to read the candidate blocks, the thresholds in force
and the estimated synthesis calls. Measuring a full dub takes about a minute,
so previews are jobs rather than synchronous replies. `preview=false` with an
`idempotency_key` queues the repair with the thresholds the session resolves
now, then returns a work handle to poll with `pandrator_get_work`. The frozen
`speech_block_early_repair_enabled` flag decides only whether the automatic
second pass of a full run happens; it does not gate this request. It requires
run permission, an active original speech plan and a run that is **not** an
assembled/recast audio version. Second-pass child runs are ineligible. The same
route exists as `POST /api/v1/generation-runs/{run_id}/repair-timing` on the
application. Retain history and
selection identity on retries. See [recovery](recovery.md).

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

In 0.14.2, `pandrator_get_media_edit` exposes read-only
`plan.timing_diagnostics` in summary and per-cue `timing_flags` in full
state: overlapping cues/words and backwards word starts need review against
the actual recording, not silent timestamp sorting. Simultaneous speech may be
intentional. When word timing is incomplete, rendering retains the entire
caption with its cue timing; a cut inside that cue fails with
`media_edit_word_timing_incomplete` rather than guessing a word boundary.

The mix preview (12 seconds by default, selectable from 4 to 30 seconds)
reflects the **current reviewed media-edit render**, not the raw recording. Render and review the exact active revision first.
Export requires that same current rendered revision and source/content hash:
when an edit or source changes, render again and create a fresh export plan
instead of reusing a stale preview or plan.

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
  `response_mode="structured"` when the host reads `structuredContent`
  (otherwise use `standard`). It creates an immutable export-only snapshot
  without language/synthesis prerequisites. `plan_workflow(continuation=false)`
  provides the same direct-stage control with arbitrary safe settings overrides.
  Orchestration accepts `generation_run_id` for export from completed audio;
  it rejects upstream passive stages with that historical selection.
- Plan creation persists a snapshot; execution is separate. Review its exact
  effects/confirmations and follow the canonical execute next action. Structured
  responses preserve the whole plan once without repeating it in text.
