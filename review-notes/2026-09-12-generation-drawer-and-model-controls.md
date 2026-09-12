# Generation drawer, source controls, and audio.cpp model settings

Baseline: `b93e42e6e88d0ed73257a2da187b4c3dd177d904`.

## Requested changes

1. Expanding the generation drawer now opens it at full height. Half height remains an explicit display choice.
2. The block-flag badge filters all flagged blocks in the selected plan. Its count includes the entire plan, not just loaded rows. Both true and false boundary filters exclude removed blocks.
3. Mark controls retain native checkbox semantics with round styling and a visible checked state.
4. Literal search runs on the backend over the complete selected plan. The drawer fetches compact matching records, displays matching rows, navigates directly to distant matches, and uses the existing revision-checked atomic batch endpoint for replacement. Script and spoken overrides are distinct search/edit layers. Empty results keep the drawer and search controls open. Unicode casefold matches return original UTF-16 positions, including expansion and emoji cases; replacements cannot cut half a character expansion.
5. While generation is queued or running, the stage card uses that job’s progress and detail. Reused mutable plan rows from a previous run no longer produce a false 100%. Idle/paused views retain the existing segment summary.
6. Source preview/replacement is embedded in the first source-consuming card. Downstream correction, translation, speech optimization, and plan preparation expose their relevant input/version selection and preview locally. Upstream cards retain saved-result history and nondestructive previews. Selection still uses existing guarded lineage APIs.
7. audio.cpp reference voices use one expandable picker. The collapsed summary names the selected voice; missing required selections start expanded. Native/prebuilt provider choices retain their appropriate controls.
8. The collapsed drawer has one outer border, without the expanded header divider.
9. Speech settings filter by provider and model. audio.cpp exposes a scalar request-control registry for all 11 canonical models. Values are saved by exact model ID, selected-model settings override legacy tuning, reset-to-default values mask inherited overrides, and inactive model tuning does not invalidate reusable audio. Request payloads also correct OmniVoice speed, Magpie voice ID, and Pocket clone-transcript placement.
10. The redundant Sessions backlink beneath the header is removed.
11. Clicking the session title starts inline editing. Enter saves through the revision-guarded API, Escape cancels, and errors preserve the draft.

## audio.cpp evidence and limits

Controls were checked against the official v0.7.2 direct speech route and model parsers/specs. The route prepares and runs each request; only a limited set of top-level controls is promoted to options. Model-specific settings are sent under `options`.

- [Direct request construction and option promotion](https://github.com/0xShug0/audio.cpp/blob/v0.7.2/app/server/runtime.cpp#L1937-L1949)
- [Direct route preparation and execution](https://github.com/0xShug0/audio.cpp/blob/v0.7.2/app/server/runtime.cpp#L2096-L2112)
- [VoxCPM2 option validation](https://github.com/0xShug0/audio.cpp/blob/v0.7.2/src/models/voxcpm2/session.cpp#L547-L624)
- [OmniVoice options](https://github.com/0xShug0/audio.cpp/blob/v0.7.2/src/models/omnivoice/session.cpp#L77-L121)
- [Pocket preparation and execution](https://github.com/0xShug0/audio.cpp/blob/v0.7.2/src/models/pocket_tts/session.cpp#L745-L777)

The new controls do not introduce a streaming batch transport. The earlier correction that hides unsupported streaming batching for audio.cpp remains in place. No inference-throughput claim is made. Payload validation covers all canonical models; actual synthesis with every model was not run.

## Validation

- `python -m pytest -q tests/test_generation_search.py tests/test_audio_cpp_parameters.py tests/test_web_audio_preview.py tests/test_tts_provider_profiles.py tests/test_tts_parallel_generation.py tests/test_session_source_plan_controls.py`: **68 passed**.
- Focused Ruff: passed. Svelte check: **0 errors, 0 warnings**. ESLint on changed UI files: passed. Production static build: passed. OpenAPI and generated client types updated. `git diff --check`: passed.
- Focused mypy on both new Python modules: passed. Vulture at 100% confidence: no findings. pydeps and a scoped static import graph: no cycles in the new modules/dependency edges.
- Parent-owned browser review used an isolated authenticated workspace with 413 blocks and mocked speech synthesis. Verified full-height opening, 17 flags across the plan, flag filtering, round mark controls, a single collapsed border, inline rename, local input role selection, single expandable voice picker, per-model controls, invalid numeric rejection, independent persisted Qwen/VoxCPM values, and reset-to-default persistence.
- Search review found eight matches in blocks 1, 112, 313, and 403; replace-all persisted all eight changes. Navigation wrapped from first to last of 413 matches and back without rendering every row. Empty search results stayed editable. Spoken replacement preserved the script, and search combined with flags. Layout inspected at desktop and phone width; viewport restored. No browser console errors in the final disposable build.
- A broader backend run exposed `SourceAwareWorkflowTests.test_enabled_document_optimization_locks_generation_until_its_artifact_exists`; the same failure was reproduced on the baseline in a disposable archive. Its fixture registers a subtitle upload without adopting its timed revision, so current readiness correctly reports unavailable. No unrelated fixture/readiness behavior was changed.
- Knip reports existing unused `SubtitleSourceTools.svelte`, the `SpeechPlanRevision` type, and an unlisted ffmpeg test binary. These are outside this change. This is not a claim that the full repository passes every quality check.

The live 413-block generation was left running throughout implementation and browser verification. Rollout uses a separate reflinked application slot, locked dependencies, installed-runtime tests, an idle-work guard, and backups of the database and current-app pointer.

## Specialist routes

Parent retained all architecture, integration, UI implementation, and browser review. `batch_transport_evidence` (Terra/high) checked audio.cpp source contracts; `drawer_backend_evidence` (Luna/xhigh) traced current backend behavior; `drawer_search_progress` and `audio_cpp_model_options` (Luna/xhigh implementers) executed exact backend packets. `drawer_model_backend_review` (Terra/xhigh) reviewed the backend and verified fixes to two discovered edge cases. No external OpenCode route was used.
