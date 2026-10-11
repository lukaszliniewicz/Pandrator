# Narration, cast design and provider settings

Inspect the connected server's schemas and the relevant compact TTS catalogue.
Use exact returned provider, model and voice IDs. Provider names, releases,
prices, voice support and request limits change; retrieve current capabilities
and pricing provenance rather than treating this skill as a static catalogue.
Read full model or voice records only for a specific decision.

## Reuse narration and cast state

For one voice, reuse the requested managed narrator or exact built-in voice.
For multiple voices, reuse accepted speaker attribution and derive the cast
dictionary before assigning voices. If the user asks to replace one cast voice,
design and assign that character without discarding accepted narrator or cast
assignments. Keep voice IDs and settings in the session, not only in chat.

New plain-text sources also need deterministic `clean_source` before
`prepare_text`. Follow the ready stage or orchestration plan; leave agentic
cleanup disabled when the text needs no model processing. A plan for a later
stage may require its input artifact to exist first.

A prepared voice has its reference or native provider registration. Service
availability is independent and time limited: inspect `prepared`,
`service_available`, `health_checked_at` and `availability_reason`. An expired
health observation means unknown availability. Request one explicit refresh
when health is relevant; ordinary catalogue reads must not become repeated
provider probes. Keep unknown health distinct from missing reference, missing
local model, scope mismatch and an observed unavailable service.

## Gemini and Vertex

Keep Google AI Studio and Vertex routes explicit. Before starting, inspect the
resolved service/model, project/location where applicable, and redacted
credential origin. A browser Google sign-in is separate from the application's
configured API credentials. Never print credential contents or copy credential
paths into routine receipts. Preserve the user's selected route.

Use a built-in voice when requested. For voice design, inspect the advertised
design capability, choose the requested Gemini service, and use
`pandrator_design_google_voice` if the connected server provides it. Save the
returned native voice registration and ID. Observe its credential/project scope
and expiration; do not substitute a different voice silently. Native Google
voice creation and synthesis are separate operations: creation quota or
permissions can fail while built-in speech synthesis remains usable.

Set narration direction through the model's advertised instruction fields.
For previous/following context, use the advertised speech-performance settings
and short boundaries from the selected source. Context is read-only surrounding
text, not another passage to speak. Explicitly disable both context and
instructions when the user requests their absence; do not confuse context with
voice design prompts or cast descriptions. Preserve the resolved setting
snapshot and revision so comparisons are reproducible.

## Other speech providers

For local audio.cpp, inspect installed/loaded models and their model-specific
parameters. A prepared sample does not imply that its model is installed. Use
the managed service operation returned by the tool and refresh discovery once
after it completes. For ElevenLabs, inspect the exact model's voice and direction
capabilities rather than carrying settings from a different model. Keep local
and paid provider work within the user's authorized scope.

## Edit voices and delivery without generating audio

Read `pandrator_get_speech_selection_context` for the current segment/range. Its
markup reference is the allowed XML vocabulary; its capabilities describe the
selected adapter and reference mode. Preview through
`pandrator_preview_speech_selection`, then use `pandrator_apply_speech_selection`
with the returned preview revision and an idempotency key. Retrieve full context
only for the range being edited, not repeatedly for the whole project.

Field edits support narrator, character or category-only dialogue, voice,
delivery, and `vocal_event: {kind, position: before|after, duration_ms?}`. Duration
is permitted only for a pause (50–5000 ms). At a voice boundary, after belongs to
preceding words and before to following words; XML uses
`affinity="previous|following"`. XML and field/cue edits cannot be combined.
Omission retains values; null clears only schema-permitted fields. Enable casting,
delivery or vocalizations explicitly when that matches the user's request.

For multiple edits, use `pandrator_preview_speech_selection_batch` with up to 100
`changes`, then `pandrator_apply_speech_selection_batch` with its preview revision
and the same reviewed changes. Offsets are Unicode code points in the original
speech text/revision, even when several edits target one segment. Each change
pins the plan and segment revision; overlapping changes to the same field and
ambiguous duplicate cursor events fail atomically. The batch creates and adopts
one plan. It preserves display/subtitle text, timing, takes and sound anchors;
it generates no audio. `detailed_preview=true` requests compiled details only
when needed. Agent changes to protected annotations require `unlock_locked=true`
on the relevant changes. Return to targeted generation after applying.

Valid unsupported annotations remain saved; preview reports their omission.
Voice-description channels are different from acting instructions and do not
accept phrase delivery. Request descriptions can be reused without guaranteeing
identical voices; Gemini's saved-resource design is a separate operation. Model
catalogues exclude support components and non-model selectors, while service
configuration retains routing selectors. Read structured `expressive_capabilities`
separately from upstream features. Pandrator assembles separate cast requests;
native multi-speaker input remains unimplemented.

## Finish

Follow preparation and synthesis work to their terminal states, assemble the
requested output, export it and deliver the usable file. Keep a small receipt:
session/revision, voice IDs, route/model, instructions/context summary,
admission/job/run IDs, terminal state and file path. The user reviews listening
quality. See [recovery](recovery.md) for errors and interruptions.
