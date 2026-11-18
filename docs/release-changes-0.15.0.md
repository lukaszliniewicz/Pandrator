# Pandrator 0.15.0 changes

Changes since [0.14.5](release-notes-0.14.5.md). Most of them come from a field test on 17 November 2026. A 70-minute webinar was cut, transcribed, corrected and dubbed into German, Dutch, Japanese and Hungarian, entirely through MCP, and every point of friction was logged. The release also includes Patrick Nadeau's per-block speaker-plan review ([#167](https://github.com/lukaszliniewicz/Pandrator/pull/167)).

## Upgrade notes

These change what existing setups do:

- **Default speed-up.** The built-in `synchronization_speed` is now 1.15 instead of 1.2. Sessions that never set it use the new value. Higher values still work; the export plan warns above 1.2.
- **Soundtrack for new voiceovers.** New voiceover sessions and new language branches store `audio_mode=mixed` when the source has audio, and `dubbing_only` otherwise. Existing sessions and branches keep what they stored.
- **Omitted `audio_mode`.** `pandrator_plan_export_variant` without `audio_mode` now uses the session's effective value, as `pandrator_plan_workflow` already did. It used to force `mixed`.
- **Assemblies are rebuilt once.** Assembly fitting changed (algorithm 2, below), so an assembly made before 0.15 is rebuilt at the next export. Takes are reused; only the assembly is redone.
- **Timing repair keeps more.** A regenerated neighbour that comes out longer is now kept when it pushes no later speech.
- **MCP, for scripted callers.** `pandrator_create_translation_project` returns a work handle instead of the project. Waits stop at 25 seconds. Several tools return a summary unless `view="full"` is passed. The MCP contract revision is now `2026-11-18.1`, so reconnect MCP clients after upgrading.
- **Older speaker plans.** Judging a speaker plan block by block needs each block's speaker, language, node kind and section, which plans record from 0.15 onwards. After any change to its revision, every block of a plan adopted earlier needs review, as the whole plan did in 0.14.5.
- **Database.** Migration `0089_job_progress_state` adds one nullable column to `jobs`.

## Dubbed output

- New voiceover sessions and language branches mix the voice over the original soundtrack, as above. Every export plan reports the effective `audio_mode` and where it came from. It warns when a voiceover over a recording with sound would export `dubbing_only` and drop the original.
- The dubbed audio track is tagged with the speech language: the output language, otherwise the session's target language, otherwise its source language. Exports record the language and its origin.
- When speech runs past the end of the video, the export plan says so before anything is queued: the last frame will be held, the export will stop before rendering, or an audio export will fail. The plan shows the tail-extension step. The setting's description now says that usually only the added tail is encoded.

## Timing

- **Silence trimming.** Providers pad short takes with silence, and assembly used to fit whole takes, padding included. Drift built up across short cues: one closing Dutch "Ja." held 270 ms of speech in a 960 ms take. Both assembly backends now trim each block's leading and trailing silence (below -50 dBFS), keeping 40 ms before the speech and 80 ms after. The new audio setting `synchronization_trim_silence` is on by default. Diagnostics report the trimmed time per block and in total, and timing repair measures blocks the same way.
- **Drift report.** Assemblies record where each block's speech lands and a `timing_drift` summary: the worst block lag, blocks more than 1, 3 and 10 seconds late, late cues and the final drift. Late cues are counted separately, because a cue inside a merged block can be late while its block looks fine. Export plans with a matching assembly carry it as `speech_timing`. They warn when speech drifts far behind the subtitles, pointing to `synchronization_speed` and, above 1.2×, to condensing the text instead.
- **Timing repair on any finished voiceover.** Repair works on any completed voiceover run with the current thresholds, in the app and through the new `pandrator_repair_generation_timing`. A preview job lists the candidate blocks and the estimated synthesis calls without spending any. Pre-checks skip candidates whose split cannot cover the pause between the halves. The run summary reports the repair pass's own usage (`usage_total` for the whole run), its effect on late cues and the worst lag, and counts that agree with the job result. Progress names the pass it is in.
- `settings_source="run"` accepts a run from an earlier revision of the same speech plan. It reports how that run's frozen TTS settings differ from today's in `settings_source_diff`.

## Reuse and planning

- **Takes no longer look stale after every rebuild.** The run start froze a derived timing key into each take's audio identity, and plan preparation computed the expected identity without it. Every take of a project that used correction or translation therefore looked stale on every rebuild. Derived keys are now kept out of the identity. Older takes still match.
- **Timing-only subtitle changes.** A speech plan records text and timing signatures of its subtitle source. A change that only moves timing keeps the plan usable and asks for it to be prepared again; re-preparing keeps every take.
- **Takes from older revisions.** A rebuilt plan looks for each block's take through the plan's earlier revisions, nearest first and up to 20, with every check applied per revision.
- **Stale counters add up.** Each block falls in one bucket (reusable, settings changed, not generated, identity unknown), so stale equals active minus reusable.
- **Accepted passive work is reused.** Accepted correction and translation dispatch runs record what they were given, so continuation plans stop re-running them. Plan steps report `reuse_basis`. `pandrator_plan_workflow` takes top-level `reuse_stages` and `rerun_stages`; rerun wins over reuse.
- **Sibling export plans.** Several export plans made together can all be executed: a plan no longer goes stale because a sibling export added its outputs. Plans stored before 0.15 keep the old, stricter comparison.
- **Effective routes, with their origin.** Transcription, generation and media-edit plans, and branch readiness, show the decisive STT and TTS settings and where each came from. They warn when an engine other than the built-in default, or a voice route not verified for the language, was inherited rather than chosen.
- **Pre-edit timeline.** In a session with a reviewed media edit, starting correction or translation on the pre-edit transcript returns a warning naming the edited subtitles to use instead. Refusing a translation project for that reason names the artifact and the remedy. The media-edit planner recognises a reviewed revision that descends from an accepted proposal.
- **Estimates.** A workflow plan's `generate_audio` step gives `estimated_seconds` when the workspace has a recent completed run with the same provider, model and request count.

## Subtitles and transcription

- Transcription no longer produces overlapping cues. Each cue's end is trimmed to the next cue's start minus the minimum gap; starts and word timings never move. The result counts `overlaps_clamped` and `overlaps_unresolved`.
- For subtitles made earlier, the subtitle setting `clamp_overlaps` (off by default) trims overlaps at export without changing the text.
- Parakeet output that glues a number to the word before it ("for16", "in1900", "know,70") gets a space. Other engines keep their text.
- Export diagnostics list subtitle issues as flat rows with cue numbers (overlaps as cue pairs), instead of strings truncated to `[TRUNCATED]`.
- A publishable English correction lists passages that still contain "uh", "um" or "erm" in `filler_check`, pointing to `pandrator_patch_subtitle_passages`.
- When a named voice-activity model is not cached, the transcription plan and the web panel say what will happen. It downloads when transcription starts, or, for someone who may not download models, transcription will fail until an Administrator prepares it.

## Queue and progress

- Subtitle evidence may run beside a render in the same project and runs on the delivery lane, so a few seconds of re-transcription no longer wait for a whole render. Two evidence requests in one project still run in turn. On a local recogniser, evidence waits for the GPU and for any transcription.
- Queued work reports its lane, its position, why it is waiting (its project is busy, a resource is in use, the worker is busy elsewhere, or a retry is scheduled) and, where the caller may see it, the job in the way.
- Rate-limit retries show at every concurrency level, for example "3 requests waiting on rate limits (Vertex AI, attempt 4 of 10, next in 38 s)". Before, they only showed with one request at a time. The work view carries the same as `retry`.
- A running generation estimates its time left from its own pace once it has done 10 blocks or run for 2 minutes.
- A job put back in the queue because its resources were busy no longer keeps its first start time. Settings that name a provider by its display name ("Google Vertex AI") now share that provider's queue lock.

## MCP

- Waits stop at 25 seconds by default (`PANDRATOR_MCP_MAX_WAIT_SECONDS` or `--max-wait-seconds`) and report `timed_out`, so a host's request timeout no longer cuts them off.
- `pandrator_create_translation_project` queues the creation and returns a work handle; creating four branches took 45 to 80 seconds inside one request. The web UI is unchanged.
- Media-edit prepare, get and update, speech-plan topology receipts, translation projects and structured workflow plans return a summary by default, with `view="full"` for the complete response. Topology receipts list changed blocks only.
- New `pandrator_import_source_url` downloads a source from a public URL on the Pandrator host. Non-http(s) URLs, URLs without a host and URLs with credentials are refused before any job exists.
- Errors name the way out. Cue tools used on passage-based subtitles point to `pandrator_patch_subtitle_passages`. Submitting while evidence is still running is a 409 `evidence_pending`, not `invalid_model_response`. Blank context-capsule values are ignored.
- Render and caption-alignment receipts no longer include internal storage paths.
- The packaged guidance describes the multilingual route (edit, correct the edited subtitles, check fillers, then the translation project), the checks before a voiceover export, the queue fields, and that hotwords do not fix names with Parakeet. `pandrator_recommend_next_steps` no longer sends voiceover goals to voice casting, and points multilingual goals to the translation project.

## Speaker plans

An adopted speaker plan is judged block by block after an edit ([#167](https://github.com/lukaszliniewicz/Pandrator/pull/167), Patrick Nadeau). Unchanged blocks keep their directions, and so does an edited block whose change fits its markup. Only a block whose markup no longer fits needs review. A run that includes it is refused naming it; a run without it can start, and the block preview gives the review message. Project copies keep an adopted plan with changed blocks.

An edit that adds, removes or replaces a quotation mark sends its block to review, in narration as well as dialogue and in both annotation formats, since narration that gains one may now hold a spoken line. A dash that opens a line of dialogue ("— Idę do domu — powiedziała.") counts as a quotation mark, and a single quote between Chinese, Japanese or Thai characters is not taken for an apostrophe.

## Components

Pandrator **0.15.0**, MCP **0.8.0** and Manager **0.9.32**.
