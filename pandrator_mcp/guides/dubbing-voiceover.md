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
