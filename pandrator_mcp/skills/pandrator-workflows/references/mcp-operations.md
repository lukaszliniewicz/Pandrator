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

For new local speech work, the catalogue recommends `audio_cpp`; XTTS,
Silero, Kokoro, and Voxtral remain dedicated providers. Compatibility entries
are hidden by default. Retrieve a saved provider with `service_id`, or use
`include_compatibility=true` when needed. Preserve an existing selection unless
the task calls for a switch. For an explicit switch, inspect the target model
and voice IDs and use `pandrator_configure_tts`; do not copy old provider
options or assume that an uploaded voice is available in audio.cpp. Cloning
there needs a ready managed reference link.

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

## Durable work and cancellation

A returned `work` handle is not a finished artifact. Poll `pandrator_get_work`
with that handle until terminal; retain it across timeouts and interruptions.
Prefer waits short enough for the host's request timeout and progress needs.
Use `pandrator_get_work_log` for diagnosis rather than repeatedly dumping logs.

Call `pandrator_cancel_work` once when cancellation is requested, then inspect
until terminal. Quick transcription has its own polling and cancellation tools.

## Generation and delivery

1. Inspect the live TTS and voice catalogues, then use
   `pandrator_configure_tts` against current settings. Preview and review a
   representative voice sample before a long synthesis when appropriate.
2. Execute the reviewed generation plan and wait for terminal work.
3. Inspect `pandrator_list_generation_runs` and
   `pandrator_list_generation_segments`. Use
   `pandrator_update_generation_segment` for actual text/settings changes,
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
