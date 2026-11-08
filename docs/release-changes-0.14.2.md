# Pandrator 0.14.2 changes

Changes since [0.14.1](release-notes-0.14.1.md). For downloads and a short overview,
see the [release notes](release-notes-0.14.2.md).

## Subtitle and recording review

Subtitle review gains **Review overlaps**, which lists cues whose display times
overlap, including overlaps across pages. Each row shows the overlap and whether
the speakers differ, and keeps **Play** and **Recheck audio** available. The
filter updates as you edit and does not change text or review state.

The recording editor now reports overlapping word times, overlapping captions
and word times that run backwards. **Review timing warnings** filters the
transcript and seeks to the first affected cue. These are read-only observations:
original timestamps and word order remain available, and simultaneous speakers
can be intentional. See [reviewing and previewing work](guides/review-and-preview.md).

## Transcription setup

Transcription settings add **Local model files**, which checks the selected
recognition model, speech detector, word aligner and language detector separately.
It reports cached, missing or unresolved files and never downloads, hashes or
creates them. A cached recognizer does not prove that its VAD or aligner is
present; compatibility and integrity checks still run when the model is used.

Administrators can select **Existing local model file...** for the speech detector
and use a compatible VAD file in place. WebRTC needs no model file. Saving an
enabled VAD with no detector or path is rejected. Automatic engine selection can
remain unresolved until the source language is known. See
[local audio tools](reference/local-audio-tools.md) and
[first subtitles](getting-started/first-subtitles.md).

## Media edits and exports

Media-edit rendering now keeps a whole caption when word timing is incomplete:
the caption text and cue timing are retained, while the word list is dropped and
marked as caption-timed. A cut that would start or end inside such a cue is
rejected with `media_edit_word_timing_incomplete` instead of guessing word
boundaries. Rendering also checks the composed subtitle text against the retained
source cues before publication.

The 12-second mix preview resolves the active reviewed media-edit render, not the
raw source. Without a current render it asks you to render and review the edit
first. Queued previews pin the source, dubbing and edit revision, and exports
require the exact current render. Disabling sound processing now skips sound
rendering and no longer invalidates reusable speech. Completed assemblies are not
overwritten by later progress failures. See
[first voiceover](getting-started/first-voiceover.md).

## Audiobook narration

Narration length is now available in **Generate audio > Settings**,
**Voice and audio**, **Customize workflow** and, in Advanced mode,
**Speech plan > Narration settings**. The dialog shows the effective target from
unsaved model choices.

When generation starts with current settings, Pandrator checks the effective
length before showing the recording preview. If it changed and the plan is
untouched automatic text, the plan is resegmented and the preview uses the new
revision; the previous plan and recordings remain in history. Plans with edits,
annotations, optimized text, sound placements, individual voice selections or
missing provenance keep their boundaries and show an explanation. Historical
settings remain frozen. Changing sentence-packing or dialogue settings still
requires rerunning **Segment narration**.

Breeze's automatic target drops from 540 to 450 characters. VoxCPM2 drops from
1,800 to 350 for all languages, including CJK. VoxCPM1 remains at 1,800 generally
and 450 for CJK; Qwen remains at 350. These are starting policies, not measured
acoustic maxima. See
[narration lengths](reference/narration-segmentation.md).

## Automation and background work

Translation project and branch creation now validates targets, prepares an
authorized file snapshot outside the database writer, verifies staged hashes and
installs each branch atomically. Replaying the same idempotency key returns the
original result without copying again; failures leave no partial branches. MCP
clients should inspect the project or replay the same key after an uncertain
response before creating more branches.

Job logs capture only the handler thread, redact secrets before buffering, and
stay bounded. They flush during heartbeats and terminal drains and never fail the
job. MCP preserves `workflow_conflict` and `media_edit_word_timing_incomplete`
with backend details.

## Updates and compatibility

Manager remains **0.9.31**; this release does not require a Manager update. MCP
moves to **0.7.1**. No database migration is added. Existing projects, recordings
and saved model choices remain in place. See
[updates and repair](operations/updates-data-and-repair.md) and the
[MCP guide](../pandrator_mcp/README.md).
