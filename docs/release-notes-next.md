# Next release

These changes are in development and are not included in the published 0.14.4
release.

## Performance

A revision's detail repeats less work for cast segments. The audio identity
check reuses the cast freeze's parse of the same speech XML, and retained voice
checks share one provider registry. On a 5,167-segment cast book, the detail
read went from 6.55 s to 5.85 s, measured by Patrick Nadeau (#146).

## Subtitle review

Fixing the wording of one passage no longer re-times the whole subtitle.
Passage review, in the app and through `pandrator_patch_subtitle_passages`,
keeps every display cue not linked to your edit through a shared passage. A
wording fix keeps its cue's timing, even inside a cue that holds several short
passages, when the cue shares no passage with another cue, its passages have
one speaker, and the new text fits the line and reading-speed limits (or is no
longer than before). Previously each save rebuilt every cue from its passage
alone, which could split a line such as "Yes. Traditional Christianity? Yes."
into three cues, some as short as 80 ms. A passage whose timing, speaker or turn
changes is still recomposed, and a cue that comes out shorter than the minimum
duration now borrows free time around it, without widening an overlap between
speakers. A dry run reports how many cues change timing.

## Storage

Language branches and other project branches no longer copy the files they
carry from the source. A branch hard-links its checkpoint, edited media, renders,
transcripts and evidence clips, so a video with four target languages stores
its edited media once instead of five times. Where the data folder cannot use
hard links (another volume, FAT or exFAT, some network shares), branches copy
as before. Branches created earlier keep their own copies. Deleting a project
now shows only the space it frees, which excludes files another branch still
uses.

## MCP

- Creating translation branches and reading artifact details get longer
  deadlines (120 s and 60 s), since branch creation prepares each branch's
  files before it replies.
- `pandrator_revise_speech_block_plan_batch` now publishes the typed shape of
  its operations, with split and merge examples, and accepts up to 50 at once.
- When a segment edit lands in a new speech-plan revision (the previous one
  was reviewed or used by a generation run), the update tools now say so,
  keep `previous_segment_id` on the receipt, and point to the generation runs.
  Runs already started keep the earlier text.
- `pandrator_get_generation_operation` also accepts `generation_operation_id`,
  the name start and regenerate results use.
- Passing a session id where a translation project id is expected now says so
  and names the session's translation project, if it has one. The hint is
  shown only to callers who can read that session.
- The subtitle provenance error names the artifact and the parent that blocks
  it, and whether that parent is missing, deleted or belongs elsewhere.

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
  provider's message (paused and cancelled runs report the list too). Reword
  those blocks and resume with `missing_only`. A regenerated segment that
  already had audio keeps it, and the run still ends `partial`. An empty audio
  field counts as empty audio, not as a decode error. If three segments in a
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

## Output

- Exported videos now name the dubbed audio track. The track carries the
  target language, a title and a handler name (for example `hun`,
  "Hungarian"), so players no longer list it as "Unknown". Three-letter codes
  such as `eng` work as well as `en`. With a target language Pandrator does not
  recognise, the track is marked undetermined rather than keeping a language
  tag from the audio it was made from.
- Merging two Japanese or Chinese speech blocks, by hand, through passage
  regroup or through audiobook range re-segmentation, no longer inserts a space
  between them in the text, the speech text or the speech markup. Automatic
  re-segmentation also finds 。, ！ and ？ without a following space, so a
  Japanese range no longer fails with "A word exceeds max_chars".
- Japanese subtitle lines no longer break inside a dotted name such as
  「ラリー・キング」 or between a number and its counter such as 「2014年」,
  unless no other two-line split fits.

## Media edits

Rendering a media edit no longer fails when two speakers overlap and the later
speaker's first word is aligned a few milliseconds before the earlier speaker's
interjection. Composed subtitles now keep the source order instead of sorting
by start time, and a cue that would start before the previous one starts with
it instead. When the text check still fails, the error names the first source
cue and composed cue that differ.
