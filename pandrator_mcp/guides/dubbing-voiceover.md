# Voiceover and dubbing workflow

Pandrator stores this as a `voiceover` session and uses the dubbing processing
pipeline. It begins with transcription, correction, and optional translation,
then adds:

- optional whole-document or generation-time speech optimization;
- per-segment speech generation with reviewable takes;
- deliberate take selection and audio assembly;
- optional synchronization, source-audio ducking, or a dubbing-only mix; and
- export of audio, subtitle tracks, or rendered video.

Generated segments are not automatically a finished mix. Inspect selected
voices, provider readiness, timing constraints, and output mode first. Source
media, subtitle text, and voice samples may be sensitive; a workflow plan should
say which external providers receive each kind of data.

For subtitle-only outcomes, use a subtitle session and stop before speech
generation. For narration without timed media, use an audiobook session.

## Starting from a sidecar-host file

Call `pandrator_browse_local_sources` without a root to discover the approved
root names, then browse one root and select a returned relative file. Create or
inspect the voiceover session first because `pandrator_import_local_source`
requires its current revision. Import streams through Pandrator's resumable
upload API and attaches the source; file bytes never enter model context.

For a public video page or media URL, `pandrator_import_source_url` downloads
the source on the Pandrator host and attaches it; it returns a work handle.

Plan transcription and execute only the exact reviewed plan. The plan names the
effective speech-recognition engine and warns about a voice-activity model that
is not cached: it is downloaded when transcription starts, but only an
Administrator's run may download it. Poll the returned
work handle with `pandrator_get_work` until terminal before creating a passive
correction or translation run from its artifact.

For sessionless quick transcription, omitted settings inherit saved defaults,
including language; explicit `language="auto"` requests detection. Unsupported
explicit quantizations are rejected for the selected engine before upload. An
automatic engine choice remains unresolved until routing and is then validated
before inference; an explicit quantization is never silently substituted.
Create/status responses and JSON results expose `selection.requested`,
`selection.resolved`, adapter-reported `selection.runtime`, and any legacy
quantization normalization. Unknown runtime checkpoint or quantization stays
null. Per-request choices do not change global defaults.

## Dispatch before dubbing

When subtitles need external correction or translation before speech
generation, use a subtitle dispatch run and process batches in order. Listing
or inspecting the run never includes raw batch content; claim is the only
content-disclosure step. Work only on the compact packet's `batch.cue_rows`
(decode with `cue_columns` and `turns`), or request `packet_format=standard`
for `batch.cues`. Use their stable
source-revision `cue_id` values, and treat `batch.context` as non-actionable
continuity evidence. The short-lived `lease_token` belongs to that batch only.
For target-language cleanup, create a correction run pinned to the translation
artifact; `output_role: translation` confirms that the corrected result remains
the target-language track used by downstream voiceover generation.

Submit a typed correction or translation `result`; raw `response_text` exists
only for compatibility adapters. Accepted batches advance to the next claim;
repair a rejected result under its still-valid lease. Renew slow work or
release it when stopping. After the final accepted batch, inspect the finalized
artifact before selecting it for dubbing. Busy, stale-lease, source/output
conflict, and `finalizing` responses are states to handle, not reasons to
bypass the queue.

## Several languages from one recording

Use one source session and a translation project, not one session per language.
The order matters, because each step pins the one before it:

1. **Edit first.** If the recording needs cutting, the source session is a
   `media_edit` session (`pandrator_create_session` with
   `workflow_kind="media_edit"`); its translation project still gives voiceover
   branches. Finish the media edit before any correction: plan it with
   `pandrator_plan_media_edit_workflow`, review the cuts, and render with
   `pandrator_render_media_edit`. The render writes `media_edit_subtitles`, the
   subtitles on the edited timeline. A recording that is not cut skips this
   step, and the voiceover session itself can be the source.
2. **Correct the edited subtitles.** Wait for the render, then correct
   `media_edit_subtitles`, not the pre-edit transcript. A correction on the
   pre-edit timeline does not carry onto the edited video, and a translation
   project refuses text that does not descend from the edited subtitles.
   `pandrator_create_dispatch_run` warns when the selected source is on the
   pre-edit timeline; stop and switch sources when it does.
3. **Check for fillers.** When the correction style removes fillers, read
   `filler_check` from `pandrator_get_dispatch_run` after the run finalises.
   `status: "fillers_remaining"` lists the passages that still contain "uh",
   "um" or "erm" (English only). Fix them with
   `pandrator_patch_subtitle_passages` now: every translation copies them, and
   a later edit of the source does not reach the branches.
4. **Create the project.** Call `pandrator_create_translation_project` with the
   corrected checkpoint; `create_planned_branches=true` also creates the
   branches already planned for the session. It returns a work handle: poll
   `pandrator_get_work` until terminal, then read
   `pandrator_get_translation_project` (`view="summary"` by default,
   `view="full"` for the whole record). Add languages later with
   `pandrator_create_translation_branches`.
5. **Work per branch.** Each branch is a voiceover session with pinned copies
   of the checkpoint and the edited timeline. Run translation, generation or
   export across branches with `pandrator_preview_translation_project_operation`
   and `pandrator_execute_translation_project_operation`, or work in one branch
   directly. Fix a branch's text in that branch: passage-based translations take
   `pandrator_patch_subtitle_passages`, not the cue tools.

## Passive speech optimization before generation

Once transcription, correction, or translation is selected, a separate
speech-optimization dispatch run can prepare the target text without calling a
Pandrator LLM provider. Claim `batch.units`, treat boundary context as
read-only, and return every stable `unit_id` exactly once. Compact packets keep
task policy and the character dictionary in a cacheable `manifest`; retain it
before supplying `known_manifest_hash`. SRT timing appears
only on actionable units and is retained in the finalized artifact; it is not a
request to alter cue duration.

The final batch materializes `tts_optimized`. Inspect that artifact and listen
to representative generated takes before running the complete voiceover. This
is a pre-generation whole-document stage, not a callback queue inside an
already-running TTS job.

## Catalog-backed generation and deliverables

Before generation, inspect speech blocks separately from display captions.
Use `pandrator_update_generation_segments` for atomic speech wording and
exclusion edits, with each current block ID and revision. Set `removed=true`
to remove material from speech and projected display captions; restore it with
`removed=false`. To retain display-only labels while excluding their speech,
choose `subtitle_text_source="selected_artifact"` for export: the selected
caption revision then stays independent of speech-plan exclusions. Keep spoken
responses such as “Amen” in separate included blocks. Use `optimized_text` for spoken wording while keeping
display text. Keep utterance boundaries intact, then review the new speech-plan
content signature and preview representative provider requests.

Accepted passive correction and translation are reused by default: their
dispatch evidence stands in for the managed settings fingerprint. So is a saved
review of either, such as a `pandrator_patch_subtitle_passages` edit and its
copy in a language branch (`reuse_basis: reviewed_revision`); a reviewed
translation is still rerun when the target language changes. A plan reports
`reuse_basis` for each reused stage. Use the top-level `reuse_stages` argument
to keep another changed prerequisite result anyway, and `rerun_stages` to run a
current one again. Inspect the resulting artifact IDs and reuse decisions before
executing; never replace accepted host work with an unintended provider pass.

Do not infer a provider, model, or voice ID from a user's example. Call
`pandrator_get_tts_catalog` with the project's `session_id`. If observations are
stale, refresh explicitly with `pandrator_refresh_tts_catalog` at the matching
workspace/service scope and with `app.write`. Match the requested qualities
against advertised compatible choices,
and inspect `pandrator_get_session_settings` for section `tts`. Apply the exact
catalog IDs with `pandrator_configure_tts` and the reported settings revision.
If no match exists or several materially different matches remain, ask the
user; never silently substitute a provider.

Plan and execute generation, then poll its durable work handle. Use
`pandrator_list_generation_runs` to select the completed run rather than
assuming “latest.” For each deliverable, call
`pandrator_plan_export_variant` with its audio mode, subtitle mode, subtitle
selection, and the run's `generation_run_id`: the plan then lists the assembly
it reuses or prepares and reports `speech_timing`. Without a run ID the export
uses the session's current assembly, the one made from the active speech plan.
Execute that immutable plan and poll it to terminal before planning another
dependent output.

After a subtitle change that only moves timing, prepare the speech plan again
with `pandrator_prepare_speech_plan`; every take is kept. Then assemble the
current takes with `pandrator_assemble_generation_run` without a
`generation_run_id`, and plan the export without one. Do not plan
`generate_audio` for it: a new run synthesises every block again, and the plan
warns that every take is reusable. Timing repair needs a run on the active
speech plan, so run it before such an edit.

Omitting `subtitle_selection` uses saved output settings; `auto` explicitly
includes the available documents. `source`, `translation` and `dual` are binding
choices (`dual` needs both). Missing requested documents stop planning or export
instead of substituting another language. Prepare the missing document or choose
an available one. Audio-only exports and video with `subtitle_mode="none"` do not
require captions.

Typical separate variants include translated voiceover with mixed or
dubbing-only audio and burned translated subtitles, plus original audio with
corrected source subtitles. Keep them as separate export plans so each output
contract and artifact lineage is reviewable. Finish with
`pandrator_download_artifact`; it resumes and verifies the immutable artifact
inside the operator-approved output root.

Output settings identify the effective soundtrack and caption sources. Choose
`mixed` for source audio plus voiceover, `preserve` for source audio, or
`dubbing_only` for generated speech. Omitting `audio_mode` keeps the session's
effective value; the export plan reports that value and where it came from.
Source gain, voice loudness and ducking apply to a mixed soundtrack. Additional
project sounds are a separate inclusion choice. An export reuses recorded takes
and prepares an assembly when required.

## Checks before exporting a voiceover

Read the export plan before executing it:

- **Soundtrack.** Check the effective `audio_mode` and its origin. A voiceover
  over a recording with sound normally wants `mixed`; new branches default to
  it, but branches made before 0.15 keep what they stored. The plan warns when
  such an export resolves to `dubbing_only`, which drops the original
  soundtrack. Pass `audio_mode` explicitly when the user asked for one.
- **Timing.** `speech_timing.timing_drift` reports how far the speech lags the
  picture: `worst_block_lag_ms`, `worst_cue_lag_ms` (a cue inside a merged
  block can be late on its own), `late_cues` and `final_drift_ms`.
  `speech_timing.tail.outcome` says whether speech runs past the end and what the
  export will do: `extend` holds the last video frame, `stops` refuses before
  rendering, and `fails` means an audio export held to the source length,
  which never cuts speech, will fail. The plan's warnings name the setting to
  change. Report large lags to the user rather than exporting silently.
- **Speed.** `synchronization_speed` defaults to 1.15. Up to about 1.2 usually
  sounds natural; the plan warns above 1.2, and 1.3 sounds rushed. For a
  language that runs long, condense the translated text (shorter wording, or
  a `tts_optimized` layer) instead of raising the speed.
- **Timing repair after generation.** `pandrator_repair_generation_timing`
  checks a completed run with the current thresholds. `preview=true`, the
  default, queues a read-only check and returns a work handle; `preview=false`
  with an `idempotency_key` queues the repair itself.
- **Overlapping cues.** New transcriptions no longer produce overlapping cues.
  For subtitles made earlier, set the subtitle setting `clamp_overlaps` to trim
  each cue's end before the next cue starts; the text never changes, and the
  export reports `overlaps_clamped` and `overlaps_unresolved`.

New exports freeze their selected subtitle artifact identities and display
revision when submitted. Later correction, translation or selection changes do
not replace queued tracks. `subtitle_text_source="generation_run"` requests the
selected audio version's historical wording; its display master must match a
selected track. On a mismatch, select the matching subtitle revision or choose
current display text. Inspect preservation/layout diagnostics before delivery.
`selected_artifact` uses the chosen caption revision without speech-plan display
projection; queued track identities and normal formatting checks still apply.
Rendered voiceover video exports omit inherited chapters and record measured
video, audio and container durations separately from requested timing settings.
