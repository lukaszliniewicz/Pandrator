# Audit verification record — 2 October 2026

Repository: `/home/lliniewicz/Projects/Pandrator_dev/Pandrator`; audited HEAD `ca14815d516a1e8e55d4b24ee8c26e0b89dd98f4`. Commands below were run during the audit, before product fixes. Counts are separate and overlapping. Scratch fixtures/providers were isolated; no production activation or paid inference occurred.

## Release and quality

```bash
gh release list --limit 12 --json tagName,name,isDraft,isPrerelease,publishedAt,createdAt
gh release view v.0.10.1 --json tagName,targetCommitish,url,publishedAt,assets,body
git show --no-patch --format='%H %cI %s' v.0.10.1
git rev-list --count v.0.10.1..HEAD
git log --format='%h %cI %s' v.0.10.1..HEAD
PYTHONDONTWRITEBYTECODE=1 .pixi/envs/default/bin/ruff check --no-cache --output-format concise .
PYTHONDONTWRITEBYTECODE=1 .pixi/envs/default/bin/vulture
PYTHONDONTWRITEBYTECODE=1 .pixi/envs/default/bin/python scripts/test_lanes.py check
CI=true PYTHONDONTWRITEBYTECODE=1 .pixi/envs/default/bin/python scripts/check_types.py --outputjson
```

Release range: 12 commits. Ruff: 2 I001 failures. Vulture passed. Lane manifest failed on six unassigned files. Basedpyright checked 508 files with zero unrecorded diagnostics; committed baseline suppressions remained in effect.

From `web/`:

```bash
npm run check
npm run lint
npm run format:check
npm run dead-code
```

All passed; Svelte reported zero errors and warnings. Current CI was read through GitHub APIs/logs, including Web preview run `36951206044` and Python quality run `36951205952`; see the audit for the actual failing gates.

## Session lifecycle and projects

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp=/tmp/pandrator-audit-20261002-lifecycle/pytest tests/test_session_purge.py tests/test_translation_projects.py tests/test_session_fork_assets.py tests/test_web_session_forks.py tests/test_multilingual_setup.py tests/test_workflow_inputs.py tests/test_mcp_session_branches.py tests/test_mcp_session_purge.py tests/test_mcp_workflow_inputs.py -q
.venv/bin/ruff check --no-cache pandrator/web/session_forks.py pandrator/web/session_purge.py pandrator/web/sessions.py pandrator/web/multilingual_setup.py pandrator/web/translation_projects.py pandrator/web/translation_project_routes.py pandrator/web/migrations/versions/0050_session_purges.py pandrator/web/migrations/versions/0051_translation_projects.py pandrator/web/models.py pandrator/web/session_flow_routes.py pandrator/web/workflow_inputs.py pandrator_mcp/tools/session_branches.py pandrator_mcp/tools/session_purge.py pandrator_mcp/tools/workflow_inputs.py pandrator_mcp/schemas/session_branches.py pandrator_mcp/schemas/session_purge.py pandrator_mcp/schemas/workflow_inputs.py
```

82 passed, one existing audioop warning, 60.06 seconds; scoped Ruff passed.

The separate pending-upload reproduction initialized a ten-byte chunk upload belonging to a session, wrote `1234567890`, trashed the session, obtained a purge preview/impact token, and purged it. Observations: purge complete; upload database row absent; chunk still present; subsequent `cleanup_expired()` returned zero and the chunk still existed. This uses actual upload and purge services.

## Subtitles, dispatch and settings

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/pandrator-audit-20261002-subtitles/pytest tests/test_subtitle_review_turns.py tests/test_dubbing_llm_correction.py tests/test_dubbing_llm_translation.py tests/test_dubbing_subtitle_projection.py tests/test_subtitle_evidence_cache.py tests/test_dispatch_lifecycle_controls.py tests/test_dispatch_preview.py tests/test_mcp_compact_dispatch.py tests/test_correction_chain_freshness.py tests/test_subtitle_diagnostics.py
TMPDIR=/tmp/pandrator-audit-20261002-subtitles PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/pandrator-audit-20261002-subtitles/pytest-second tests/test_web_dispatch.py tests/test_mcp_dispatch.py tests/test_web_subtitle_review.py tests/test_web_subtitle_evidence.py tests/test_mcp_subtitle_evidence.py tests/test_mcp_application_client.py tests/test_mcp_generation_batch_contract.py tests/test_passage_regroup.py tests/test_web_voiceover_regroup.py tests/test_subtitle_finalization.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m ruff check --no-cache pandrator/logic/dubbing/correction_splits.py pandrator/logic/dubbing/llm_correction.py pandrator/logic/dubbing/llm_translation.py pandrator/logic/dubbing/natural_boundaries.py pandrator/logic/dubbing/passage_regroup.py pandrator/logic/dubbing/subtitle_finalization.py pandrator/logic/dubbing/subtitle_projection.py pandrator/web/subtitle_review.py pandrator/web/subtitle_evidence.py pandrator/web/dispatch.py pandrator/web/dispatch_preview.py pandrator/web/dispatch_routes.py pandrator/web/settings_policy.py pandrator/web/workspace_settings.py pandrator/web/logical_passages.py pandrator/web/voiceover_regroup.py pandrator_mcp/tools/dispatch.py pandrator_mcp/tools/subtitle_evidence.py pandrator_mcp/tools/workflow_controls.py pandrator_mcp/tools/generation.py pandrator_mcp/schemas/dispatch.py pandrator_mcp/schemas/subtitle_evidence.py pandrator_mcp/schemas/workflow_controls.py pandrator_mcp/schemas/generation.py pandrator_mcp/clients/application.py
```

First batch: 100 passed in 23.03 seconds. Second: 267 passed in 120.38 seconds. Each reported the existing audioop deprecation. Scoped Ruff passed.

Separate service reproductions:

- Valid TTS source: document stage `tts_optimization`, artifact role `tts_optimized`; review save returned `ValueError: The selected source has no valid logical passages.`
- Mixed projection: four display cues Intro 0–1s, First 1–2s, Second 2–3s, Last 3–4s. Logical rows `p000001` Intro, `p000002` First+Second, `p000003` Last. Deleting Intro made review save fail with `Cannot store invalid logical passages.`
- Same projection, no deletion: set both First and Second speakers to Alice. Saved segment speakers were Alice, but canonical passage and materialized speech speakers remained blank.

## Speech, generation, exports and Manager

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/pytest tests/test_voice_setup.py tests/test_speech_plan_preparation.py tests/test_performance_passes.py tests/test_generation_cast_runtime.py tests/test_speech_plan_preview.py tests/test_generation_audio_identity.py tests/test_export_input_resolution.py tests/test_export_video_single_pass_tail.py tests/test_video_tail_fast.py tests/test_video_tail_freeze.py tests/test_export_video_tail_decision.py tests/test_export_video_cleanup.py tests/test_export_video_commands.py tests/test_web_media_process.py tests/test_manager_supervisor_persistence.py tests/test_web_supervisor.py -q
```

Initial result: 163 passed / 41 setup errors caused by a missing scratch parent. Corrected rerun:

```bash
mkdir -p /tmp/pandrator-audit-20261002-export
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/retry tests/test_performance_passes.py tests/test_generation_cast_runtime.py tests/test_speech_plan_preview.py tests/test_voice_setup.py -q
```

51 passed: the 41 formerly errored cases and 10 overlapping passes. These two runs establish 204 distinct passes.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/blocks tests/test_speech_block_natural_splitting.py tests/test_speech_block_passage_capacity.py tests/test_speech_block_prosody_regressions.py tests/test_dubbing_speech_blocks_integration.py tests/test_manager_audiocpp.py tests/test_audiocpp_expanded_packages.py tests/test_audiocpp_release_assets.py tests/test_audiocpp_model_reuse.py tests/test_soundtrack_export.py -q
```

114 passed / 5 failed; the five failures retain audio.cpp 0.8.1 expectations after the 0.9.0 runtime pin.

The isolated strict-voice regression test used the repository's real generation fixture, persisted Puck on its middle segment, and started a run with session voice Kore, `casting_enabled=false`, `voice_mode_version=1`, `tts_batch_size=1`. The synthesis stub recorded the real compiled requests and returned silent audio. Expected all Kore; actual `[Kore, Puck, Kore]`.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/repro-tmp /tmp/pandrator-audit-20261002-export/test_stored_voice.py -q -s
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --collect-only tests/test_voice_setup.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/voice-collect --override-ini 'python_classes=*Tests' tests/test_voice_setup.py -q
```

Reproduction: one intentional expected-voice failure. Normal collection: 2 tests. Override probe: 7 passed, including five normally uncollected class cases.

## Browser protocol and fixtures

The parent used CUA for all browser interactions. `scripts/run_web_e2e_server.py` provided authenticated temporary storage at port 8098, CPU capability fixtures and the repository's bundled frontend. After that process stopped, a temporary equivalent server reopened the same disposable data root without background maintenance to finish the default-settings reproduction. No production data was opened through this fixture server.

The review fixture used a four-second blue 320×180 FFV1/PCM MKV generated by FFmpeg, the four SRT cues above, and pinned canonical passage metadata. A project was created with Japanese and Polish branches; German was subsequently added through the UI. A separate empty multilingual draft used English/Polish targets and subtitles-only output.

Measured UI outcomes:

- Languages creation/addition succeeded; effective 390 CSS-pixel view had no document-level horizontal overflow.
- Enabled Add languages contrast approximately 8.1186:1, white foreground on RGB(96,65,132).
- FFV1 source video: playback time advanced with audio, `readyState=4`, no media error, but intrinsic video dimensions were zero and the picture was blank.
- Start-new-utterance on a display fragment was offered and save was rejected.
- Delete Intro then Save reproduced invalid passage IDs.
- Close after failed save dismissed and lost the draft without warning.
- Saving untouched Transcribe settings on a fresh draft returned the full unknown-STT-keys error retained in the screenshot.

No complete local browser test run, live recognition/synthesis, or full screen-reader audit was claimed.


## Additional language/provider checks

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/language-tests tests/test_model_catalogue.py tests/test_audio_cpp_metadata_projection.py tests/test_audio_cpp_expanded_translation.py tests/test_audio_cpp_inventory_parameters.py tests/test_tts_catalogue_pass2.py tests/test_tts_provider_profiles.py tests/test_audiocpp_model_reuse.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp /tmp/pandrator-audit-20261002-export/language-boundary tests/test_tts_handler.py -k 'silero or audio_cpp or kokoro or chatterbox' -q
```

67 passed; 24 passed / 59 deselected respectively. Source/metadata probes compared catalogue filters with request normalizers, including SanoTTS German and IndexTTS Japanese, Qwen Chinese locale, Silero regional language, and Azure Norwegian evidence versus actual request definition. Primary-source pages/configs were inspected without weights or inference. Parent counted 50 concrete global picker options and confirmed a Swahili search returned No matching languages in the browser.
