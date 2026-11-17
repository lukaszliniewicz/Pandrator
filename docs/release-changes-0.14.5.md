# Pandrator 0.14.5 changes

Changes since [0.14.4](release-notes-0.14.4.md). This maintenance release fixes speaker plans lost on edit ([#165](https://github.com/lukaszliniewicz/Pandrator/issues/165)), reported by Patrick Nadeau, and integrates his work in PRs [#148](https://github.com/lukaszliniewicz/Pandrator/pull/148), [#149](https://github.com/lukaszliniewicz/Pandrator/pull/149), [#150](https://github.com/lukaszliniewicz/Pandrator/pull/150), [#151](https://github.com/lukaszliniewicz/Pandrator/pull/151), [#152](https://github.com/lukaszliniewicz/Pandrator/pull/152), [#163](https://github.com/lukaszliniewicz/Pandrator/pull/163) and [#164](https://github.com/lukaszliniewicz/Pandrator/pull/164), measured on a 5,167-segment cast book. The original contribution commits retain their authorship. It also ships the Manager restart fix from [#147](https://github.com/lukaszliniewicz/Pandrator/pull/147).

## Speakers and delivery

Editing a cast book no longer quietly drops its speaker plan ([#165](https://github.com/lukaszliniewicz/Pandrator/issues/165)).

- Restoring a speech-plan revision keeps its adopted speaker and delivery plan. The restored blocks are exact copies, so no new review is needed and their audio stays reusable. The copy Pandrator makes when you edit a reviewed or already generated revision carries the plan too, but the edit then makes it stale: copy it as described next, and adopt the copy, before generating.
- A plan made stale by an edit can be copied. The copy keeps every entry whose block and neighbouring blocks are unchanged (locked entries need only their own block), so after appending a word to one line only that line and the blocks planned with it as context need review, not the whole book. In the app, a stale plan offers "Copy unchanged blocks" and "Copy and analyse the rest"; through the API and MCP, `copy_from_id` now accepts it and `keep_unchanged` limits AI or passive analysis to the changed blocks. The response reports `copied_count` and `skipped_count`.
- With casting on, generation no longer starts silently without a speaker plan when an earlier revision had one (for example after a split, merge, re-segmentation or batch edit), which voiced every line of dialogue as narration. The start dialog names the earlier revision and links to Speakers & delivery, which offers "Copy earlier plan"; starting anyway needs an explicit tick, or `allow_without_speaker_plan` through the API and MCP.

## Long cast books

- A recast preview whose client has gone stops at the next segment instead of running to the end, and each preview passes the request's voice catalogue, pin proofs and provider registry on instead of rebuilding them for every segment. One preview fell from 317-348 s to about 5 s, and one abandoned after a second answers 499 at once. Abandoned previews had been slowing every other request until the app needed a restart ([#150](https://github.com/lukaszliniewicz/Pandrator/pull/150)).
- A revision's detail repeats less work for cast segments. The audio identity check reuses the cast freeze's parse of the same speech XML, and retained voice checks share one provider registry. The detail read went from 6.55 s to 5.85 s ([#146](https://github.com/lukaszliniewicz/Pandrator/pull/146)).
- Segment listings leave out each top-level `operation_json` value over 4 KB by default and name the keys they left out in `operation_json_omitted`. After a restore, a one-segment page fell from 877 KB to under 4 KB. `operation=full` returns everything; an unknown value is refused ([#149](https://github.com/lukaszliniewicz/Pandrator/pull/149)).

## Database writer

These hold SQLite's single writer for less time, so other requests wait less behind them.

- Starting a missing-only run copies the preserved takes in bulk. With 5,159 preserved takes, admission's final write transaction fell from 3.73 s to 1.20 s ([#148](https://github.com/lukaszliniewicz/Pandrator/pull/148)).
- Saving a stage selection walks only the chosen artifact's lineage instead of loading every artifact edge in the database. On a project with 15,324 artifacts, the writer hold fell from 0.56 s to 0.02 s ([#151](https://github.com/lukaszliniewicz/Pandrator/pull/151)).
- Prepare text builds the plan's segment rows before it takes the writer and stores them with one insert. For 5,167 blocks the writer hold fell from 0.77 s to 0.20 s ([#152](https://github.com/lukaszliniewicz/Pandrator/pull/152)).

## Speech plan

A batch of split and merge edits (`pandrator_revise_speech_block_plan_batch`) now becomes one speech-plan revision instead of one per edit. The edits run on an unsaved copy of the plan and the result is written once, so a 31-edit batch on a 900-block audiobook takes about 1 s instead of 12 s, and the receipt is less than half the size. The batch is still all or nothing, the revision history lists it as "Batch of N speech-block edits", and restoring goes back to the state before or after the whole batch, not to a step inside it. Labels, original-id lineage, take reuse for untouched blocks and sound-cue anchors work as before. The receipt no longer repeats the revision's `operation_json` or lists every segment id; it gives `segment_count` instead ([#162](https://github.com/lukaszliniewicz/Pandrator/pull/162)).

## Speech generation

- When Gemini or Vertex AI declines a line (a safety block, for example), Pandrator no longer retries it ten times and then fails the whole run. A stated refusal is not retried, and audio that comes back empty with no reason is tried at most three times. The segment is marked failed, the rest of the run carries on, and the run finishes `partial` with a `failed_segments` list giving each segment's ordinal, reason and the provider's message (paused and cancelled runs report the list too). Reword those blocks and resume with `missing_only`. A regenerated segment that already had audio keeps it, and the run still ends `partial`. An empty audio field counts as empty audio, not as a decode error. If three segments in a row come back empty with no reason, or the retry budget runs out, the run still stops, since the provider is then the more likely problem. The one-step automatic audiobook and dubbing pipelines still stop at the first failed segment, but now after one attempt for a refusal and three for empty audio instead of ten ([#160](https://github.com/lukaszliniewicz/Pandrator/pull/160)).
- Retry progress now says why it is retrying, for example "Retrying segment 42 (4/10) in 19.1s: rate limited" ([#160](https://github.com/lukaszliniewicz/Pandrator/pull/160)).

## Subtitle review

Fixing the wording of one passage no longer re-times the whole subtitle. Passage review, in the app and through `pandrator_patch_subtitle_passages`, keeps every display cue not linked to your edit through a shared passage. A wording fix keeps its cue's timing, even inside a cue that holds several short passages, when the cue shares no passage with another cue, its passages have one speaker, and the new text fits the line and reading-speed limits (or is no longer than before). Previously each save rebuilt every cue from its passage alone, which could split a line such as "Yes. Traditional Christianity? Yes." into three cues, some as short as 80 ms. A passage whose timing, speaker or turn changes is still recomposed, and a cue that comes out shorter than the minimum duration now borrows free time around it, without widening an overlap between speakers. A dry run reports how many cues change timing ([#156](https://github.com/lukaszliniewicz/Pandrator/pull/156)).

## Media edits

Rendering a media edit no longer fails when two speakers overlap and the later speaker's first word is aligned a few milliseconds before the earlier speaker's interjection. Composed subtitles now keep the source order instead of sorting by start time, and a cue that would start before the previous one starts with it instead. When the text check still fails, the error names the first source cue and composed cue that differ ([#157](https://github.com/lukaszliniewicz/Pandrator/pull/157)).

## Output

- Exported videos now name the dubbed audio track. The track carries the target language, a title and a handler name (for example `hun`, "Hungarian"), so players no longer list it as "Unknown". Three-letter codes such as `eng` work as well as `en`. With a target language Pandrator does not recognise, the track is marked undetermined rather than keeping a language tag from the audio it was made from ([#161](https://github.com/lukaszliniewicz/Pandrator/pull/161)).
- Merging two Japanese or Chinese speech blocks, by hand, through passage regroup or through audiobook range re-segmentation, no longer inserts a space between them in the text, the speech text or the speech markup. Automatic re-segmentation also finds 。, ！ and ？ without a following space, so a Japanese range no longer fails with "A word exceeds max_chars" ([#161](https://github.com/lukaszliniewicz/Pandrator/pull/161)).
- Japanese subtitle lines no longer break inside a dotted name such as 「ラリー・キング」 or between a number and its counter such as 「2014年」, unless no other two-line split fits ([#161](https://github.com/lukaszliniewicz/Pandrator/pull/161)).

## Exports and storage

- An export no longer waits for a long job in another project to finish. Each worker now runs a second loop that takes only exports, variants, comparison exports, mix previews and bundle exports, so exporting the French branch can start while the German branch is still generating. Jobs within one project still run one at a time and in the order they were queued. Set `PANDRATOR_DELIVERY_LANE=0` for the worker to turn this off ([#155](https://github.com/lukaszliniewicz/Pandrator/pull/155)).
- Language branches and other project branches no longer copy the files they carry from the source. A branch hard-links its checkpoint, edited media, renders, transcripts and evidence clips, so a video with four target languages stores its edited media once instead of five times. Where the data folder cannot use hard links (another volume, FAT or exFAT, some network shares), branches copy as before. Branches created earlier keep their own copies. Deleting a project now shows only the space it frees, which excludes files another branch still uses ([#154](https://github.com/lukaszliniewicz/Pandrator/pull/154)).

## MCP

- `pandrator_create_performance_plan` takes `keep_unchanged` with `copy_from_id`, and `pandrator_generate_speech_plan` and `pandrator_regenerate_segments` take `allow_without_speaker_plan`. Listing performance plans for one revision reports `ancestor_adopted_plan` when an earlier revision adopted a plan this one lacks ([#165](https://github.com/lukaszliniewicz/Pandrator/issues/165)).
- The recast tools (`pandrator_preview_recast`, `pandrator_recast_voices`, `pandrator_preview_undo_recast` and `pandrator_undo_recast`) wait at least 60 s for the application instead of the client's default 15 s, so a recast preview on a long book no longer times out. A longer configured limit still applies ([#163](https://github.com/lukaszliniewicz/Pandrator/pull/163)).
- `pandrator_list_generation_segments` takes the same `operation` option as the API (default `summary`), and its shorter views name every operation key they leave out ([#149](https://github.com/lukaszliniewicz/Pandrator/pull/149)).
- Creating translation branches and reading artifact details get longer deadlines (120 s and 60 s), since branch creation prepares each branch's files before it replies ([#159](https://github.com/lukaszliniewicz/Pandrator/pull/159)).
- `pandrator_revise_speech_block_plan_batch` now publishes the typed shape of its operations, with split and merge examples, and accepts up to 50 at once ([#159](https://github.com/lukaszliniewicz/Pandrator/pull/159)).
- When a segment edit lands in a new speech-plan revision (the previous one was reviewed or used by a generation run), the update tools now say so, keep `previous_segment_id` on the receipt, and point to the generation runs. Runs already started keep the earlier text ([#159](https://github.com/lukaszliniewicz/Pandrator/pull/159)).
- `pandrator_get_generation_operation` also accepts `generation_operation_id`, the name start and regenerate results use ([#159](https://github.com/lukaszliniewicz/Pandrator/pull/159)).
- Passing a session id where a translation project id is expected now says so and names the session's translation project, if it has one. The hint is shown only to callers who can read that session ([#159](https://github.com/lukaszliniewicz/Pandrator/pull/159)).
- The subtitle provenance error names the artifact and the parent that blocks it, and whether that parent is missing, deleted or belongs elsewhere ([#159](https://github.com/lukaszliniewicz/Pandrator/pull/159)).

Restart or reconnect MCP clients after upgrading.

## Manager

Restarting Pandrator through the Manager's runtime API or `pandrator-manager runtime-restart` after an update now runs the newly activated version. Previously the API, worker and MCP came back on the previous slot until they were stopped and started again. When the launch configuration has changed, a restart that includes the API or a worker first stops every running Pandrator service, so old and new processes never share the database, then starts them all on the active slot. A service the new launch no longer registers stays stopped. Without such a change, a restart still affects only the selected services ([#147](https://github.com/lukaszliniewicz/Pandrator/pull/147)).

## Build and tests

- A newer push to `main` now cancels the older run of the web CI workflow, as pull request runs already did, so a burst of merges no longer holds every CI slot. Manual runs are never cancelled ([#153](https://github.com/lukaszliniewicz/Pandrator/pull/153)).
- Tests now fail by name if waitress renames the internals the abandoned-read check relies on, so a waitress update cannot quietly turn that check off ([#164](https://github.com/lukaszliniewicz/Pandrator/pull/164)).

## Components

Pandrator **0.14.5**, MCP **0.7.3** and Manager **0.9.32**.
