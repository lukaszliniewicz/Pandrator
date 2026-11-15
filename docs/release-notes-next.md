# Next release

These changes are in development and are not included in the published 0.14.4
release.

## Performance

A revision's detail repeats less work for cast segments. The audio identity
check reuses the cast freeze's parse of the same speech XML, and retained voice
checks share one provider registry. On a 5,167-segment cast book, the detail
read went from 6.55 s to 5.85 s, measured by Patrick Nadeau (#146).

## Storage

Language branches and other project branches no longer copy the files they
carry from the source. A branch hard-links its checkpoint, edited media, renders,
transcripts and evidence clips, so a video with four target languages stores
its edited media once instead of five times. Where the data folder cannot use
hard links (another volume, FAT or exFAT, some network shares), branches copy
as before. Branches created earlier keep their own copies. Deleting a project
now shows only the space it frees, which excludes files another branch still
uses.

## Manager

Restarting Pandrator through the Manager's runtime API or `pandrator-manager
runtime-restart` after an update now runs the newly activated version.
Previously the API, worker and MCP came back on the previous slot until they
were stopped and started again. When the launch configuration has changed, a
restart that includes the API or a worker first stops every running Pandrator
service, so old and new processes never share the database, then starts them
all on the active slot. Without such a change, a restart still affects only the
selected services. This ships with the next Manager release.

## Speech generation

- When Gemini or Vertex AI declines a line (a safety block, for example),
  Pandrator no longer retries it ten times and then fails the whole run. A
  stated refusal is not retried, and audio that comes back empty with no
  reason is tried at most three times. The segment is marked failed, the rest
  of the run carries on, and the run finishes `partial` with a
  `failed_segments` list giving each segment's ordinal, reason and the
  provider's message. Reword those blocks and resume with `missing_only`. A
  regenerated segment that already had audio keeps it. If three segments in a
  row come back empty with no reason, or the retry budget runs out, the run
  still stops, since the provider is then the more likely problem. The
  one-step automatic audiobook and dubbing pipelines still stop at the first
  failed segment, but now after one attempt for a refusal and three for empty
  audio instead of ten.
- Retry progress now says why it is retrying, for example "Retrying segment
  42 (4/10) in 19.1s: rate limited".

## Exports

An export no longer waits for a long job in another project to finish. Each
worker now runs a second loop that takes only exports, variants, comparison
exports, mix previews and bundle exports, so exporting the French branch can
start while the German branch is still generating. Jobs within one project
still run one at a time and in the order they were queued. Set
`PANDRATOR_DELIVERY_LANE=0` for the worker to turn this off.

## Media edits

Rendering a media edit no longer fails when two speakers overlap and the later
speaker's first word is aligned a few milliseconds before the earlier speaker's
interjection. Composed subtitles now keep the source order instead of sorting
by start time, and a cue that would start before the previous one starts with
it instead. When the text check still fails, the error names the first source
cue and composed cue that differ.
