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

Plan transcription and execute only the exact reviewed plan. Poll the returned
work handle with `pandrator_get_work` until terminal before creating a passive
correction or translation run from its artifact.

## Dispatch before dubbing

When subtitles need external correction or translation before speech
generation, use a subtitle dispatch run and process batches in order. Listing
or inspecting the run never includes raw batch content; claim is the only
content-disclosure step. Work only on canonical `batch.cues`, use their stable
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

## Passive speech optimization before generation

Once transcription, correction, or translation is selected, a separate
speech-optimization dispatch run can prepare the target text without calling a
Pandrator LLM provider. Claim `batch.units`, treat boundary context as
read-only, and return every stable `unit_id` exactly once. SRT timing appears
only on actionable units and is retained in the finalized artifact; it is not a
request to alter cue duration.

The final batch materializes `tts_optimized`. Inspect that artifact and listen
to representative generated takes before running the complete voiceover. This
is a pre-generation whole-document stage, not a callback queue inside an
already-running TTS job.

## Catalog-backed generation and deliverables

Do not infer a provider, model, or voice ID from a user's example. Call
`pandrator_get_tts_catalog` (with refresh when current service readiness
matters), match the requested qualities against advertised compatible choices,
and inspect `pandrator_get_session_settings` for section `tts`. Apply the exact
catalog IDs with `pandrator_configure_tts` and the reported settings revision.
If no match exists or several materially different matches remain, ask the
user; never silently substitute a provider.

Plan and execute generation, then poll its durable work handle. Use
`pandrator_list_generation_runs` to select the completed run rather than
assuming “latest.” For each deliverable, call
`pandrator_plan_export_variant` with its audio mode, subtitle mode, subtitle
selection, and optional generation-run ID. Execute that immutable plan and
poll it to terminal before planning another dependent output.

Typical separate variants include translated voiceover with mixed or
dubbing-only audio and burned translated subtitles, plus original audio with
corrected source subtitles. Keep them as separate export plans so each output
contract and artifact lineage is reviewable. Finish with
`pandrator_download_artifact`; it resumes and verifies the immutable artifact
inside the operator-approved output root.
