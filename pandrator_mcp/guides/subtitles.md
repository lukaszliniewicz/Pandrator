# Subtitle workflow

A subtitle session operates on an audio or video source:

1. **Transcribe** creates timed source-language cues.
2. **Correct** creates a distinct revision for punctuation, wording, cue merges,
   and splits without translating.
3. **Translate** creates a target-language revision and retains lineage to the
   source or corrected cues.
4. **Preview** compares source, correction, and translation selections.
5. **Export** writes selected cues as SRT or VTT, or concatenates a transcript.

Correction and translation are separate review decisions. Inspect the workflow
snapshot and selected artifact IDs before continuing downstream. Selecting an
older upstream revision can invalidate or clear dependent selections; use the
reported impact rather than assuming downstream work is still current.

Speech recognition may run locally or through a provider depending on the
configured engine. Translation may use an LLM or a translation provider. An
exact plan should disclose remote data transfer and identify the selected input
revision.

## External-model dispatch

For a correction or translation that an external model will process, create a
dispatch run and pull its batches sequentially. Run metadata is safe to list or
inspect, but the canonical task packet is disclosed only by claiming a batch.
Correction may explicitly target either a transcription or an existing
translation artifact. In the latter case `kind` remains `correction`, while
`output_role` is `translation`: the result appends a same-language translation
revision instead of being mislabeled as a source-language correction.
Compact claims default to actionable `batch.cue_rows`, decoded with
`cue_columns` and `turns`; explicit `packet_format=standard` uses `batch.cues`.
`batch.context` contains
bounded continuity evidence that must not be submitted. Use only the stable
`cue_id` values declared by `id_namespace: source_revision_cue`. Keep the
short-lived `lease_token` paired with that batch.

Submit typed correction operations or typed translation items. Timing appears
once with each actionable cue when requested; `none` mode has no timing values.
`response_text` is retained for legacy raw-model adapters, not as
the preferred MCP path. A validation rejection means repair and resubmit under
the same active lease; it does not authorize changing the source or skipping a
batch. Renew long work, release abandoned work, and inspect the automatically
finalized artifact after the last accepted batch. Handle source/output
conflicts, stale leases, busy runs, and `finalizing` as explicit states.

Use `pandrator_get_workflow_inputs` and `pandrator_select_workflow_input` to pin
an exact corrected revision for translation or speech. The selection is atomic
and revision fenced. Read an accepted sample with `pandrator_get_dispatch_preview`
when style needs checking; cancel or supersede an abandoned run through
`pandrator_terminate_dispatch_run` before restarting.

For readable source subtitles, `correction_style=publishable` removes incidental
disfluencies. Keep meaningful repetition and utterance boundaries independently
of speaker names. Translation preserves the cleaned source's meaning and turns.

Compact claims (the default `packet_format=compact`) encode cues as `cue_columns`/`cue_rows`
and turns as a lookup table. Retain the first `manifest` by `manifest_hash` before
supplying `known_manifest_hash` on subsequent claims. Grouped correction edits
and translation `items` reduce repeated fields without changing validation.
Use `response_mode=structured` only when the host consumes `structuredContent`;
otherwise retain the default full text response. Fragment evidence engines come
from `pandrator_get_subtitle_evidence_routes`, with `force_refresh` available to
bypass matching successful evidence. See the repository passive-dispatch guide
for the complete protocol.
