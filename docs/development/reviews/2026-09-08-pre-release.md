# Pre-release review — 2026-09-08

**Assessment: hold the release until the confirmed media-edit defects and failing test gates are resolved.** Most declared features are present and the exercised workflows work, but the current code is not a clean release candidate.

## Scope and provenance

- Last published release: [Pandrator 0.8.19](https://github.com/lukaszliniewicz/Pandrator/releases/tag/v.0.8.19), published September 4, 2026; tag commit `dc5df1df`.
- Reviewed application state: `74bf65298872a89c08f82e459fc5e0055fc1f9b5`, including all pending feature changes committed and pushed during this review. There are 28 commits after the release.
- Excluding generated OpenAPI/TypeScript contracts and the bundled static files, the range changes 231 files, with 46,559 insertions and 1,940 deletions.
- Review combined commit/goal mapping, scoped source inspection, independent backend reviews, automated tests, browser interaction and screenshots, and finding-specific reproduction. It is not a claim of exhaustive proof for every line or every provider/platform combination.
- Local databases, recordings, scratch files, and personal notes were excluded from the commit and release scope. Product source was not changed to fix findings during the review.

## Confirmed product findings

### 1. P2 — Successful media-edit jobs are shown as failures and results are not refreshed

Location: `web/src/routes/sessions/[id]/edit/+page.svelte:607`.

The editor's `watchJob` recognizes `completed` as success. The jobs API returns the durable job status `succeeded`; `jobApi.get` does not translate it. Both configured-agent proposals and media renders use this watcher. A successful job therefore takes the error branch and skips `load()`, leaving the editor on the old plan/result until manual navigation or refresh.

Browser reproduction against the packaged UI returned `status: succeeded` for a render job. The visible alert was **“The media_edit.render job failed.”** The media-edit state endpoint was read once, with no completion refresh. The fixture produced no JavaScript errors.

Required acceptance: test the real API's success vocabulary for both proposal and render flows; show success and refresh the resulting plan/artifacts. This is a missing integration test, not something that lint or the current `status: string` TypeScript definition can catch.

### 2. P2 — Overlapping speakers can acquire the wrong cue ownership and speaker label

Locations: `pandrator/web/workflow_handlers.py:4608`, `:2593`, and `:2638`.

After word-timed subtitle recomposition, media-edit persistence calls `_store_timed_words` without a word-to-composed-cue map. The fallback assigns each word to the first cue with temporal overlap, without checking speaker/provenance. It then replaces cue speakers using all overlapping words, including speech from other speakers.

A reproduction using the actual composer and document/word persistence used Alice's `alpha` at 0–3000 ms and Bob's `beta` at 1000–1300 ms. Before timed-word persistence, the composed document contained `alpha / Alice` and `beta / Bob`. Afterwards, `beta`'s document cue was labeled **Alice**, and both words referenced Alice's `segment_id`, although beta's word-level speaker remained Bob. This creates contradictory stored evidence and exposes incorrect ownership through the revision-words API.

Required acceptance: preserve explicit composed speaker labels and map each word to its actual composed cue, including simultaneous speech. Assert both saved cue speakers and word `segment_id`s. Existing tests overlap cue envelopes but do not overlap the actual words, so they miss this case.

### 3. P3 — General source uploads are labeled “Video file”

Location: `web/src/lib/AddSourceDialog.svelte:276`.

The label uses only `role === 'transcript'`; all other uploads are called “Video file,” including audiobook and subtitle workspaces where `allowTranscriptRole` is false and any supported source may be uploaded. This is misleading copy introduced while adding media-edit source roles. Use the recording-specific label only in the media-edit context and retain the general source label elsewhere. Upload mechanics are not blocked.

## Release gates and test maintenance

### Missing test-lane registration

`scripts/test_lanes.py` omits:

- `tests/test_mcp_quick_transcription.py`
- `tests/test_subtitle_rebalancing.py`
- `tests/test_web_quick_transcription.py`

The manifest validates exact coverage before executing lanes. Consequently, `python scripts/test_lanes.py check` and normal lane execution fail before the release suite starts. Add each file to exactly one appropriate lane. The omitted files include tests added in the latest implementation; their focused passes do not replace registration in CI.

### Browser suite: three stale assertions, each failing in both browsers

The complete Chromium/Firefox run produced **93 passed, 6 failed, 9 skipped**:

- `web/tests/parameter-help.spec.ts:56` assumes STT Engine is the first field. The new attached-caption alignment controls now come first. Subsequent visibility expectations also need to account for the independently selected caption-alignment method.
- `web/tests/workflow-generation-voice-ui.spec.ts:66` requires an exact `<div class="w-full">` source string. The workspace now has additional sizing/overflow classes. The assertion does not test rendered behavior.
- `web/tests/workspace.spec.ts:582` expects Tab after Delete to reach Next. The new Recheck audio button now follows Delete. The expected keyboard sequence must include it while continuing to verify dialog containment.

These failures are test maintenance issues, distinct from the reproduced media-edit completion defect, which the existing browser suite does not test.

### Backend checks

| Check | Result | Interpretation |
| --- | --- | --- |
| Focused changed backend behavior | 166 passed | Media editing, subtitles, generation and quick-transcription behavior under controlled fixtures. |
| MCP lane plus new Quick Transcribe test | 145 passed | Used an isolated MCP SDK 2.1.1 environment; all 20 MCP test files passed. |
| Fast lane file set | 556 passed, 4 failed | Three manifest failures above and one stale transcription mock target. Executed directly because the lane runner rejects the incomplete manifest. |
| Non-MCP/non-Manager serial file set | 852 passed, 3 failed | One stale settings assertion and two process/timing failures. Both process/timing cases passed a focused rerun; the settings failure remained. |
| Dependency manifest contract | 5 passed | Declared dependency contract checks passed. |
| Ruff | Passed | Newly committed Python changes since `92eb43fe`, plus the Manager CI scope. This is not a claim of repository-wide lint cleanliness. |
| Compile / CLI help | Passed | Application, MCP, Manager and installer compile; runtime CLI help works. Existing invalid-escape warnings remain. |
| Manager test execution | Environment blocked | The repository environment lacks `dulwich` and `dbus_next`; Manager source review, Ruff and compilation were completed. |

Two Python assertions also need updating for changes in this release range:

- `tests/test_dubbing_transcription.py:779` patches `pandrator.logic.dubbing_handler.correct_srt_file_with_result`, an unused import removed by `92eb43fe`. The patch fails before exercising transcription. Preserve the intended separation-of-transcription/correction check using a valid target.
- `tests/test_web_parity_workspace.py:990` expects the removed `merge_threshold_ms` → `subtitle_merge_threshold` alias. Commit `92eb43fe` replaced the old subtitle composition settings without updating this assertion. Assert the current supported settings contract.

These failures existed before the latest commit but **are new relative to the reviewed release baseline**. The two timing-related failures were cancellation promptness and cross-process job claiming; they passed individually in a three-test rerun (2 passed, the settings assertion failed). They should be observed in the next full release run rather than presented as confirmed product regressions.

Counts describe separate, overlapping runs and must not be added as unique test coverage.

## Declared goals versus evidence

| Change group | Assessment | Evidence and remaining limits |
| --- | --- | --- |
| Workflow review/generation UX (`64da2567`) | Implemented, exercised | Artifact history/selection/trash, compact generation controls, spoken overrides, and settings/reuse flows are present. Browser behavior mostly passes; brittle source-string and focus-order assertions need updating. |
| Workspace and service performance (`82d358b8`, `378433ed`, `9ec80067`) | Implemented, exercised at bounded scale | Lazy component loading, bounded snapshots and review pages, explicit full-corpus search, PDF raster reuse and canceled preview requests are present. The opt-in 100/500/1000-segment browser probe passes. This is not a quantitative speedup claim against a separately built 0.8.19 baseline. |
| Regeneration and revisioned speech topology (`ba5b58a5`, `72b92dda`) | Implemented, exercised | Immutable plan revisions/provenance, split/merge/restore, active-run selection and regeneration baton handling are present. Browser tests exercise reversible edits and repeated regeneration/take selection. A pre-existing concurrent initial-plan risk is listed separately below. |
| Subtitle quality escalation (`7d32070b` and follow-ups) | Implemented; overlap persistence gap | Bounded evidence requests, provider/model modality controls, review states and artifact lineage are present. Overlap ownership/speaker corruption remains a confirmed defect in media-edit persistence. Live model recognition quality was not assessed. |
| Transcript-guided media editing and cue-local CTC (`57a21b1c` through `f9a1fe90`, plus later refinements) | Core implemented; UI completion gap | Source/caption attachment, immutable edit revisions, target-local alignment, cut refinement, manual boundary controls and review-before-render are present. The editor fits desktop/mobile screens, but successful jobs are misreported. Real provider/CTC model accuracy was not benchmarked. |
| Passive media-edit dispatch and MCP planning (`31f097dd`, `d1ebdfd8`) | Implemented; contracts and tests pass | Revision/hash pinning, leased proposal flow and planner contracts were traced. No concrete route/payload mismatch was found across registration, schemas, clients and backend routes. All 145 MCP tests passed. |
| Subtitle publication before video and stable edit identity (`7b0e1c39`, `dac1d78f`, `92eb43fe`) | Implemented, with overlap defect | Subtitles and word artifacts are published before encoding; each invocation gets a unique stage-run directory, preserving prior output files. Recomposition and subtitle-only rendering are present. Explicit speaker attribution must survive persistence. |
| Bounded subtitle reflow and review-state preservation (`74bf6529`) | Implemented; focused coverage present | Balanced display partitions, bounded neighboring-cue reflow, ancestry timing and protected review boundaries are present and tested. This review did not judge publication quality across representative real multilingual recordings. |
| Breeze voice design (`98ca3b68`, `332e5ad0`) | Implemented; UI and backend contracts checked | Design/preview/promotion/linking flow, catalogue refresh, optional reference-free generation and license disclosure are present. Dialog and language selection work at desktop/mobile sizes. No live Breeze inference was run. |
| Manager SQLite resilience / audio.cpp CA propagation (`4350a54d`, `e09a6184`) | Implemented | Cancellation polling tolerates transient locks, and model-install commands receive the selected CA bundle. Native platform/install behavior was not exercised end-to-end. |
| Quick Transcribe (`74bf6529`) | Implemented, exercised; CI registration missing | HTTP multipart/resumable upload, MCP local/base64 sources, format retrieval, record/preview UI, ownership, cancellation, lease fencing and cleanup are implemented. Real FFmpeg and MCP transport were checked; ASR was stubbed. No permanent session is created. |
| Optional Graphify policy (`fd64949f`) | Met | Repository guidance explicitly makes it opt-in. No Graphify commands were run during this work. |

## UI review

Personally inspected the affected UI implementation and exercised the packaged application with Playwright. Coverage included session creation/source roles, workflow/history/settings, subtitle review/pagination, generation boundaries and take selection, output controls, voice recording/design, and Quick Transcribe. New media-edit and voice-design surfaces were additionally inspected at 1440px and 390px widths; the focused fixtures had no document-level horizontal overflow. Core accessibility tests passed.

Media-edit completion was tested with a controlled successful API response, not a live long encode. Voice design used a controlled service catalogue. The existing recorder integration uses a fake browser microphone and real media normalization. Screenshots of synthetic media fixtures establish layout, not media playback/recognition quality.

## Code quality assessment

The main direction is sound: revision and content-hash pinning, immutable operation directories, bounded uploads/results, explicit resource locking, optimistic preconditions, and generation lease handling are coherent. New behavior generally reuses existing services instead of creating duplicate pipelines.

The weak point is consistency across boundaries. Stringly typed job states let the media editor disagree with the backend; timing persistence infers ownership after composition has already established it; and source-text assertions can pass or fail without establishing that user-facing behavior works. Fix those concrete boundaries and add outcome-level regressions. A lower-severity MCP schema gap remains: generation tool registrations advertise bare strings/lists while runtime models enforce ID lengths and array bounds (`pandrator_mcp/server.py:2825` and `pandrator_mcp/schemas/generation.py:19`). Invalid input is rejected at runtime; exposing those constraints would improve client guidance.

Large existing modules such as `workflow_handlers.py`, `workspace.py`, and `SessionWorkspace.svelte` increase review cost, but a general refactor is not a prerequisite for this release.

## Pre-existing concern, not a new release regression

`GenerationService.create_plan` (`pandrator/web/workspace.py:1892`) allocates plan/revision numbers inside a deferred transaction. Concurrent callers can race into a database lock/uniqueness error instead of receiving a deliberate conflict. The same transaction and uniqueness design exists in 0.8.19. This is a source-traced follow-up, not an independently reproduced defect introduced by this release range.

## Validation and limits

- Frontend: `npm --prefix web run check`, `lint`, `dead-code`, and `format:check` all passed.
- Full browser suite: `npx playwright test --reporter=line` — 93 passed, 6 failed, 9 skipped in 10.9 minutes.
- Opt-in scale probe: `PANDRATOR_UI_PERF=1 npx playwright test tests/generation-performance.spec.ts --project=chromium --grep 'records generation drawer costs'` — passed.
- Finding-specific probes: media-edit successful-job response rendered as failure; actual composer/document/word persistence corrupted overlapping speaker ownership. A suspected voice-language-selection defect did **not** reproduce and is not reported as a finding.
- The last implementation phase also passed 77 focused Python tests and tested the 0042→0043 migration, repeated upgrade, FFmpeg normalization and SDK-backed MCP registration/stdio behavior. Current broader checks are summarized above; overlapping runs are not summed as unique coverage.
- Current source was built into the bundled web assets before review; all browser tests used that bundle. Generated contract checks passed in GitHub Actions for `74bf6529`, as did Documentation and locked-runtime checks. Those three successful workflows do not replace the broader failing suite.
- No release was published, production data migrated, or live application/worker restarted. No live ASR/CTC/Breeze quality benchmark, cloud-provider round trip, or native Windows packaging/install test was performed.
- Local logs, synthetic repro scripts and screenshots are under `/tmp/pandrator-release-review/` on the review host.

## Review participants

Parent: scope, architecture and release judgment, all UI source/interaction/visual review, finding-directed verification and report. Terra/xhigh specialists: media/subtitle and generation/Manager backend reviews. Luna/xhigh specialists: MCP contract review and backend test execution. No external OpenCode/model route was used. Specialist findings were independently checked; an initial artifact-overwrite suspicion was rejected because operation directories are unique.

## Commit inventory

```text
74bf6529 Add quick transcription and refine subtitle finalization
92eb43fe feat(subtitles): recompose timed cues before editing
dac1d78f fix(media-edit): preserve edit identity in subtitle artifacts
7b0e1c39 feat(media-edit): publish subtitles before video render
d5520d3b feat(media-edit): refine cut boundaries and workflow UX
d1ebdfd8 Add end-to-end media edit MCP planner
fd64949f Make Graphify explicitly optional
31f097dd Add passive media edit dispatch
0aa60b80 Hydrate providers for media edit proposals
f9a1fe90 Stabilize target-local CTC alignment
6c37fef3 Align caption cues independently with CTC
332e5ad0 Refresh Breeze catalogue in voice designer
4350a54d Keep manager operations alive through transient database locks
e09a6184 Pass CA bundle to audio.cpp model installer
98ca3b68 Add Breeze voice design workflow
06a4d00d Add cue-local CTC caption alignment
06c11c92 Harden Zoom caption alignment feedback
71597c48 Improve media editing and caption alignment
2dcacecc Fix captioned media edit readiness
3d14dc0b Fix revisioned media edit session creation
57a21b1c Add transcript-guided media editing workflow
9ec80067 Improve heavy UI preview responsiveness
72b92dda Add revisioned generation topology controls
ba5b58a5 Fix regeneration scheduling and subtitle playback
7d32070b Add quality escalation for subtitle transcription
378433ed Optimize service and workspace snapshots
82d358b8 Improve workspace loading performance
64da2567 Improve workflow review and generation UX
```
