# Pandrator 0.14.2 changes

Changes since [0.14.1](release-notes-0.14.1.md). For downloads and a short overview,
see the [release notes](release-notes-0.14.2.md).

## Early-finish voiceover repair

The optional `speech_block_early_repair_enabled` pass now works for passage-first
speech plans as well as legacy planning, and can use semantic text context. It
still runs only after a completed voiceover, only moves whole cues between speech
blocks, and never changes the authored text or cue timings.

A block is considered only when it contains several whole cues, has a current
take, and ends at least the configured shortfall (1000 ms and 20% by default)
before its cue span would end, counting start delay. Blocks already carrying
incoming delay, blocks with estimated internal timing or shared-passage timing,
and blocks without a reliable sentence or clause boundary are left alone.

The boundary must promise at least the configured improvement (1000 ms by
default) at a later cue's start, estimated from spoken-text proportions rather
than word alignment. At most one boundary is tried per block.

An accepted split reuses every unaffected take, synthesises the two new children,
and re-synthesises any neighbour whose frozen context changed (see below). The
staged revision becomes active only if synthesis succeeds, both children keep
separate cue anchors, the first child is not substantially late, forward
re-measurement shows no added delay for later blocks, and the active plan and
selected takes are unchanged.

Added delay, a missed internal anchor, or a longer refreshed neighbour skips that
block and continues with later blocks. A synthesis failure, an internal
consistency failure, or a changed plan or take selection stops the pass. Original
takes and plan revisions remain available throughout.

### Semantic context and neighbours

When `tts_context_mode` is not off, repair uses the run's frozen
`semantic_context_snapshot` and the frozen preceding/following window values. It
does not rebuild windows from today's session settings.

A split changes the context of nearby blocks. Both new children receive freshly
frozen context bound to the staged revision. An unchanged neighbour whose frozen
before or after window differs from the one used for its existing take is
re-synthesised as well; unchanged neighbours with the same window are reused.

If the frozen context is missing, stale, has an unsupported schema, or does not
cover a selected block, the pass records `skipped` without checking any block (see
below). It never invalidates neighbours or re-synthesises the whole project. A
refreshed neighbour that is longer than the take it replaces rejects that block's
repair. Raw take duration is compared with no tolerance, so even 1 ms of growth is
refused rather than assuming that cue slack would absorb it.

### One automatic pass

Early repair and passage regroup never run together. If both are enabled, repair
wins and regroup does not run. Passage regroup also does not run while semantic
text context is enabled. A child run staged by either pass carries a parent marker
and cannot start another automatic pass, and the explicit repair endpoint refuses
such children with "timing repair runs at most once".

Performance and casting plans skip automatic repair because their directions are
bound to block IDs. Review and regenerate those plans explicitly.

### History and retry

History reports `running`, `completed`, `partial`, `rejected`, `no_effect`,
`skipped`, `failed`, or `stopped`.

- `completed`: at least one repair was applied and the pass finished with no
  unresolved rejection.
- `partial`: the pass checked at least one block but did not finish (a stop, or a
  timing-inspection failure after a block was checked), or some repairs were
  applied and others rejected.
- `rejected`: blocks were checked, at least one candidate or attempt was rejected,
  and none was applied.
- `no_effect`: blocks were checked and no split was needed or attempted.
- `skipped`: no block was checked. The reason names missing or unusable frozen
  context, a changed active plan, no measurable speech blocks, or a stop or
  inspection failure before the first block.
- `failed` and `stopped` retain the last accepted plan and audio.

The payload adds checked, candidate and attempted counts, `pass_completed` and
`no_effect`; applied and rejected totals remain available. Older receipts without
those fields fall back to the recorded repair versions.

Passage regroup uses the same status vocabulary with group counters. A pass that
stops, loses the plan or take selection, or fails before every selected group was
examined reports `partial` with `pass_completed: false`, even when no group was
merged; it is never shown as `completed` or as all merges rejected. A pass that
inspected no group reports `skipped` with a reason (no source run, changed active
plan, invalid settings, not passage mode, missing first-pass evidence, or an
unavailable edited-source cut mapping). Replay protection is not a check and
keeps the established completed projection. Older receipts without per-attempt
ordinals remain a best-effort count: applied blocks come from `repaired_blocks`
and each non-applied child counts once, so duplicate ordinal-less rows cannot be
separated from distinct blocks, but an applied pass is never turned into a
rejection.

Rejected staged revisions and takes remain inspectable in
[repair history](guides/repair-history.md). Pandrator does not delete them
automatically. Removing the originating generation run deletes its repair runs,
takes and rendered audio, while the staged plan revisions remain in plan history.

### Requesting the check for an existing run

A completed voiceover that predates automatic checking can request the same
non-destructive pass without synthesising its original blocks again:

```http
POST /api/v1/generation-runs/{run_id}/repair-timing
```

Send an empty JSON body and, optionally, an `Idempotency-Key`. The endpoint needs
run permission and accepts only a completed voiceover on the active speech plan
whose frozen settings already enable early-finish repair. Audio-version runs such
as recase or voice conversion are excluded. A new job returns `202`; a reused job
returns `200`.

The endpoint returns an active job for the same root, or a completed job that
finished checking at least one block. It starts a fresh job after a failed,
canceled or stopped pass, a skipped pass that checked no block, or a partial pass
that stopped or failed inspection. Older receipts without `repair_completed` are
recognised by their recorded `generation_stopped` reason. Retrying requires the
original root to remain the active plan, so an applied change makes it stale and
the request is refused. While a previous pass is still stopping, the request is
refused instead of racing its worker. These job records cover explicit requests
only: an automatic pass inside a generation run writes no such receipt, so it
never blocks a later explicit check by itself. That check is still admitted only
while the automatic pass left the original root active, meaning it applied no
change.

### Cancellation and job state

The repair job owns only the children it stages, never the completed root.
Cancelling or pausing the pass keeps the last accepted plan and audio and closes
unfinished staged children. A repair job that ends successfully cannot mark a
child that never produced audio as completed; that orphan is failed instead. The
generation card and job API report the repair job as the active generation, so
progress and cancellation follow the optional pass.

## Subtitle and recording review

Subtitle review gains a **Timing overlaps** filter, also enabled by the
**Review overlaps** banner in Display cues mode. It lists cues whose display times
overlap, including overlaps across pages. Each flagged cue shows its largest
overlap and whether a different speaker is involved, and keeps **Play** and
**Recheck audio** available. The filter updates as you edit and does not change
text, timing or review state.

Overlapping source segments keep their word sequence during cue composition, so
caption text is no longer reordered around interleaved timestamps.

The recording editor reports overlapping word times, overlapping captions and
word starts that run backwards. **Review timing warnings** filters the transcript
and seeks to the first affected cue. These are read-only observations: original
timestamps and source word order remain available, and simultaneous speakers can
be intentional. See [reviewing and previewing work](guides/review-and-preview.md).

## Transcription setup

Transcription settings add **Local model files**, which reports the selected
recognition model, the speech detector, and, where they apply, the word aligner
and language detector. It reports cached, missing or unresolved files and never
downloads, hashes or creates them. The aligner is listed for Qwen3 and for MOSS
when CTC alignment is enabled; a language-detector file appears when a local LID
backend is selected. Automatic engine selection can remain unresolved until the
source language is known.

A cached recognizer does not prove that its VAD or aligner is present.
Compatibility and integrity checks still run when the model is used. Administrators
can select **Existing local model file...** for the speech detector. A custom
speech-detector path must be saved before the panel can assess it; an empty or
missing file is reported as missing. WebRTC needs no model file. See
[local audio tools](reference/local-audio-tools.md) and
[first subtitles](getting-started/first-subtitles.md).

## Media edits and exports

Media-edit rendering now keeps a whole caption when word timing is incomplete:
the caption text and cue timing are retained, the word list is dropped and marked
as caption-timed. A cut that would start or end inside such a cue is rejected with
`media_edit_word_timing_incomplete` instead of guessing word boundaries.
Rendering also checks the composed subtitle text against the retained source cues
before publication. Edit plans report per-cue `timing_flags` and a
`timing_diagnostics` summary; renders report how many cues fell back to caption
timing.

The 12-second mix preview resolves the active reviewed media-edit render, not the
raw source. Without a current render it asks you to render and review the edit
first. Queued previews pin the source, dubbing and edit revision, and exports
require the exact current render. Disabling sound processing skips sound
rendering and input resolution instead of failing on a missing assembled artifact.
Completed assemblies are not overwritten by later progress failures. See
[first voiceover](getting-started/first-voiceover.md).

## Audiobook narration

Narration length is now available in **Generate audio > Settings**,
**Voice and audio**, **Customize workflow** and, with **Review each stage** in
Advanced mode, **Speech plan > Narration settings**. The dialog shows the
effective target from unsaved model choices.

When generation starts with current settings, Pandrator checks the effective
length before showing the recording preview. A short preparation job may run
first, and the preview waits for its refreshed plan. If the length changed and the
plan is untouched automatic text, the plan is resegmented and the preview uses the
new revision; the previous plan and recordings remain in history. Plans with
edits, annotations, performance plans, optimized text, sound placements,
individual voice selections or unverified provenance keep their boundaries and
show an explanation. Historical settings remain frozen. Changing sentence-packing
or dialogue settings still requires rerunning **Segment narration**.

Breeze's automatic target drops from 540 to 450 characters. VoxCPM2 now uses
350 characters for every language, including CJK (previously 1,800 outside CJK
and 450 for CJK). VoxCPM1 remains at 1,800 generally and 450 for CJK; Qwen
remains at 350. These are starting policies, not measured acoustic maxima. See
[narration lengths](reference/narration-segmentation.md).

## Automation and background work

Translation project and branch creation now validates targets, prepares an
authorized file snapshot outside the database writer, verifies staged hashes and
installs each branch atomically. Replaying the same idempotency key returns the
original result without copying again; failures leave no partial branches. MCP
clients should inspect the project or replay the same key after an uncertain
response before creating more branches.

Job logs capture only the handler thread, redact secrets before buffering, and
stay bounded. Records logged from child threads are not captured by design. They
flush during heartbeats and terminal drains, and logging never fails the job. MCP
preserves `workflow_conflict` and `media_edit_word_timing_incomplete` with backend
details.

RVC model uploads also prove current administrator authority before inspecting
upload identifiers or resolving artifacts. This closes a preauthorization error
path while preserving authorized command-line host execution.

## Mobile controls and verification

The Generation workspace guarantees at least a 44 CSS-pixel target for playback
and Generate controls in narrow layouts, even when a browser's root font size
is smaller than the default. The responsive browser test also checks layout
after viewport changes rather than accepting transient resize measurements.
Long course-folder titles now wrap inside their flex layout on narrow screens,
without introducing horizontal page overflow.

Release test qualification now splits the previously oversized `web-07` Python
suite into twelve independent serial lanes on both Windows and Linux. The
manifest checks exact file coverage and caps serial web lanes at 60 files; the
manual Windows workflow uses the same updated names. Cross-platform test
fixtures were aligned with the current copy contract and workspace ownership
rules without relaxing production authorization.

## Updates and compatibility

Manager remains **0.9.31**; this release does not require a Manager update. MCP
moves to **0.7.1**; restart or reconnect MCP clients after upgrading so they load
the current tool schemas. No database migration is added. Existing projects,
recordings and saved model choices remain in place. See
[updates and repair](operations/updates-data-and-repair.md) and the
[MCP guide](../pandrator_mcp/README.md).
