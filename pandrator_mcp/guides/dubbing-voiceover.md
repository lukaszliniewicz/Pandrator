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

When a continuation plan proposes re-running accepted passive correction or
translation because their settings cannot be verified, create a fresh plan with
`overrides.reuse_stages` naming those stages. Inspect the resulting artifact
IDs and reuse decisions before executing; never replace accepted host work
with an unintended provider pass.

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
selection, and optional generation-run ID. Execute that immutable plan and
poll it to terminal before planning another dependent output.

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
`dubbing_only` for generated speech. Source gain, voice loudness and ducking apply
to a mixed soundtrack. Additional project sounds are a separate inclusion choice.
An export reuses recorded takes and prepares an assembly when required.

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
