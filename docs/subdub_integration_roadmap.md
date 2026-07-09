# Pandrator and Subdub Integration Roadmap

Date: 2026-07-09

## Goal

Deprecate Subdub as a separate application and make Pandrator own the complete dubbing workflow: transcription, subtitle correction, translation, speech-block generation, per-block TTS through Pandrator's existing TTS providers, audio alignment/sync, subtitle equalization, and final video rendering.

The migration should remove duplicated LLM handling, improve Pandrator's Python structure, preserve Subdub's useful features, and avoid porting explicitly excluded or obsolete features.

## Verified Repository State

- Pandrator was fetched from `origin`, fast-forwarded, and verified clean on `main`.
- Pandrator `HEAD` and `origin/main`: `656dc8eab2e407cd6cbe80c8cc1fca0be461cf71` (`656dc8e Rename Qwen3-TTS models to Prebuilt Voices and Voice Cloning`).
- Subdub was cloned as a sibling repository at `C:/Users/user/Downloads/Pandrator_dev/Subdub` to keep the Pandrator worktree clean while reviewing.
- Subdub `HEAD` and `origin/main`: `4f9a314cf56dffe2b4c5808261ed0492eb7ac827` (`4f9a314 Add explicit -y to FFmpeg mix commands`).

## Scope Decisions

Port into Pandrator:

- Subdub's subtitle and dubbing domain logic.
- Subdub's non-Gemini transcription/correction/translation/sync features.
- Subdub's manual boundary correction editor.
- Subdub's Zoom transcript correction feature.
- Subdub's reusable tests and fixture coverage, adapted to Pandrator's state and provider abstractions.

Do not port as-is:

- Gemini audio transcription. The current Subdub clone does not contain a distinct Gemini audio transcription implementation, but if it exists in older branches or local history it should be excluded as requested.
- Subdub's legacy `-task tts` parser mode. Subdub documents that this mode is present for compatibility but does not execute a standalone TTS-only stage.
- Subdub's hardcoded XTTS-only TTS request implementation as a separate subsystem. The feature requirement is "generate dubbed speech"; in Pandrator this should route through the existing richer TTS provider layer rather than duplicating `http://localhost:8020/tts_to_audio/`.
- Subdub's translation evaluation workflow.
- Subdub's task-specific OpenRouter routing flags.
- A separate Subdub clone/install/update requirement in the final installer.

Baseline decisions recorded on 2026-07-09:

- Advanced dubbing controls should be added, preferably in modal dialogs so the primary session UI stays focused.
- Pixi should be the canonical test/runtime command surface for migration work.
- The proposed `pandrator.logic.dubbing` architecture split is accepted.
- Dubbing TTS belongs fully to Pandrator. Subdub's unused `tts` task and XTTS-only workflow should be removed rather than preserved.
- Diarization is in scope and should use Pandrator's general credentials store for Hugging Face tokens or equivalent provider secrets.
- SQLite dubbing artifacts should be the source of truth for stage inputs and outputs.
- Long-term support for Subdub-style artifacts can be dropped immediately. The migration should preserve current Pandrator session behavior, but it does not need to read arbitrary old Subdub work folders.
- Soft subtitle language metadata must use the target language instead of hardcoded English.
- `merge_threshold` should be implemented rather than carried forward as a dead parameter.
- Whisper prompts should be implemented and passed to WhisperX.
- Manual timing correction should be integrated into the correction/translation workflow rather than treated as a separate launched application.
- WhisperX JSON input is important, but can be deferred until the later STT abstraction work.
- Resegment correction/translation modes are deferred until the future agentic/tool-based correction and translation framework.
- STT backend selection belongs on the primary transcription surface; backend-specific tuning remains in the advanced modal.
- Dubbing correction and LLM translation use Pandrator-native fully qualified model identifiers and default to the global Pandrator LLM model.
- Correction and translation model selection are independent. DeepL is a translation backend and must not disable LLM correction.
- Persisted dubbing run snapshots must not contain provider credentials.

## Codebase Inventory

### Pandrator

Pandrator is a PyQt desktop application with a large application coordinator and a partially modular logic layer.

Important reviewed areas:

- `main.py`: Qt entry point, logging setup, optional TTS autoconnect flags.
- `pandrator/app_logic.py`: central coordinator. Current size is about 6426 lines, 223 methods on `AppLogic`.
- `pandrator/app_state.py`: dataclass state model. `DubbingSettings` now carries the baseline advanced dubbing options; later agentic/resegment work should avoid overloading it with transient tool state.
- `pandrator/logic/llm_handler.py`: Pandrator's LiteLLM/provider abstraction for text tasks.
- `pandrator/logic/tts_handler.py`: large TTS abstraction for XTTS, VoxCPM, FishS2, Voxtral, Kokoro, Silero, Chatterbox, Qwen3 TTS, Magpie, OpenAI-compatible audio, and commercial providers.
- `pandrator/logic/dubbing/artifacts.py`: SQLite-first active artifact lookup plus deterministic SRT/suffix fallback discovery.
- `pandrator/logic/dubbing/credentials.py`: deterministic credential preflight checks for LLM, DeepL, and WhisperX diarization paths.
- `pandrator/logic/dubbing/settings.py`: current dubbing settings schema plus one-way migration from legacy Subdub-shaped provider/model fields.
- `pandrator/logic/dubbing/llm_config.py`: stage-specific correction, translation, and Zoom model resolution through Pandrator's native LLM contract.
- `pandrator/logic/dubbing_handler.py`: Pandrator-native adapter plus local FFmpeg helpers. The deprecated `subdub_handler.py` shim has been removed.
- `pandrator/logic/session_handler.py` and `pandrator/logic/state_db_handler.py`: session files, dubbing run tracking, artifact roles, and active-run selection.
- `pandrator/gui/widgets/session_sections.py` and `pandrator/gui/widgets/session_tab.py`: current dubbing controls and status display.
- `pandrator_installer/*`: Subdub clone/update/install/runtime-check paths have been removed from the installer.

Current Pandrator dubbing flow:

1. The user selects a video or SRT source.
2. `AppLogic.run_dubbing_task()` starts a background thread.
3. `AppLogic` creates or reuses a `dubbing_run` and records step status in SQLite.
4. `pandrator.logic.dubbing_handler` is the active compatibility adapter for remaining AppLogic seams; the old `pandrator.logic.subdub_handler` import shim has been removed.
5. Pandrator imports native dubbing speech blocks into its sentence JSON.
6. Pandrator generates sentence WAVs using its own TTS pipeline.
7. Pandrator-native sync aligns/mixes the generated WAVs back to video.
8. Pandrator adds soft or burned subtitles and writes final outputs.

Useful Pandrator strengths to preserve:

- Centralized run/artifact tracking through `dubbing_runs`, `dubbing_steps`, and `dubbing_artifacts`.
- Rich TTS provider support.
- Voice library and audio variant support.
- Session restoration and legacy session import.
- Existing source-cleaning package shows a better modular shape than `AppLogic`.

Current Pandrator structural risks:

- `AppLogic` owns GUI-facing state, file operations, subprocess orchestration, LLM settings migration, TTS connection logic, dubbing workflows, playback, RVC, session lifecycle, and generation. This is the main maintainability problem.
- Native correction/translation use `llm_handler` for provider/model handling. Dubbing stores native model IDs, while DeepL is modeled separately as a translation backend.
- Dubbing stages are still partly discovered by modification time even though the database can track exact artifacts.
- Installer Subdub clone/install/runtime-check logic has been removed; remaining installer work is limited to concrete runtime/package requirements.

### Subdub

Subdub is a `src/` package with a better domain split than Pandrator's current dubbing layer.

Important reviewed areas:

- `src/subdub/cli.py`, `src/subdub/cli_args.py`: CLI entry point and all user-facing flags.
- `src/subdub/app.py`: task ordering and prompt selection.
- `src/subdub/app_helpers.py`: app config, provider params, input/session preparation, preflight sync/equalize.
- `src/subdub/tasks/*`: input, correction, translation, speech blocks, transcription, runtime state.
- `src/subdub/ai/*`: LiteLLM calls, correction, translation, evaluation, DeepL, WhisperX wrapper, translation memory.
- `src/subdub/subtitles/*`: SRT parsing/equalization, translation block creation, word block creation, speech-block generation, Zoom VTT parsing.
- `src/subdub/workflows/*`: boundary correction, manual correction, sync workflow.
- `src/subdub/media/*`: FFmpeg extraction/mixing, audio alignment, XTTS-only TTS calls.
- `src/subdub/corrector/*`: automatic energy-based boundary correction and PyQt manual timing editor.

Subdub task modes:

- `full`
- `transcribe`
- `translate`
- `correct`
- `speech_blocks`
- `sync`
- `equalize`
- `zoom-transcript`
- `tts` parser compatibility mode only, not a functional standalone stage.

## Subdub Feature Port Matrix

| Feature | Subdub implementation | Pandrator current state | Port decision |
|---|---|---|---|
| Media/audio input preparation | `tasks/input.py`, `media/ffmpeg.py` | Pandrator has native FFmpeg extraction for transcription and manual timing audio, plus existing video/SRT source selection and URL download. | Keep extracting input/media prep into smaller domain services as `AppLogic` is decomposed. |
| URL input | `app_helpers.prepare_context_from_input()` via `yt_dlp` | Pandrator already has `file_handler.download_video_from_url()` and GUI flow. | Keep Pandrator implementation; ensure it feeds the new dubbing pipeline. |
| WhisperX transcription | `ai/transcribe.py` | Native wrapper supports SRT output, JSON output for automatic boundary correction, direct/Pixi execution, prompt propagation, diarization, and post-processing; `dubbing_handler.transcribe_video_with_result()` returns the produced SRT path to `AppLogic`. | Live-test WhisperX on media and continue improving STT artifact handling. |
| WhisperX align model selection | `app_helpers.apply_default_align_model()` and `-align_model` | Native wrapper supports explicit and auto-default align models, and the advanced dubbing dialog exposes the field. | Live-test with WhisperX media. |
| Whisper initial prompt | `-whisper_prompt` and `transcribe_audio(initial_prompt=...)` | Native wrapper passes `--initial_prompt`, the advanced dubbing dialog exposes the field, and regression coverage verifies prompt propagation. | Keep regression coverage and live-test with WhisperX media. |
| Whisper chunk size | `-chunk_size` | Native wrapper supports chunk size, and the advanced dubbing dialog exposes the field. | Live-test with WhisperX media. |
| Diarization | `-diarize`, `--hf_token` | Native wrapper supports `--diarize` and `--hf_token`; `HF_TOKEN` is available through the API Keys tab and the advanced dubbing dialog exposes the toggle. | Live-test with WhisperX/HF credentials. |
| Boundary correction | `corrector/engine.py`, `workflows/pipeline.py` | Native energy-based segment correction is implemented as `dubbing.boundary_correction`; word-level helpers are ported but not yet exposed as a workflow. | Live-test on media and decide how word-level JSON imports fit into broader STT integration. |
| Manual boundary editor | `corrector/gui/*` | Baseline Pandrator-native dialog is wired in-process; waveform/search-replace parity remains optional follow-up. | Keep the baseline, then decide whether to port richer waveform/search tooling after the agentic correction/translation workflow is designed. |
| SRT input | `tasks/input.py` | Pandrator supports SRT source and optional matching video. | Preserve and make source/video pairing explicit in run context. |
| WhisperX JSON input | `tasks/input.py`, `preprocess_words_from_json()` | Not clearly supported by Pandrator source picker. | Defer until broader STT integration work; keep the new data model compatible with word-timestamp imports. |
| Subtitle renumbering | `subtitles/srt_utils.py` | Native `dubbing.srt_utils` supports parse/compose/renumber. | Keep as deterministic subtitle service. |
| Speaker-aware subtitle merging | `merge_subtitles_with_speaker_awareness()` | Native implementation is covered by transcription tests. | Extend fixtures as diarization edge cases appear. |
| LLM correction | `ai/correction.py` | Native implementation uses Pandrator `llm_handler`, structured operations, custom correction instructions, and an independent native model selector defaulting to Pandrator's global model. | Continue improving prompts and future agentic/tool workflow. |
| Resegment correction | `resegment_and_correct_with_llm()` | Not exposed. | Defer until the future agentic/tool-based correction and translation framework. Do not include in the baseline. |
| LLM translation | `ai/translation.py` | Native LLM and DeepL paths exist. LLM translation selects directly from Pandrator's native model catalog; DeepL is a separate backend. App workflows validate the selected stage credentials before launching paid/long-running work. | Live-provider smoke validation and future agentic/tool workflow remain. |
| DeepL translation | `translate_blocks_deepl()` | Native DeepL provider path exists in `llm_translation.py`; `dubbing_handler.translate_subtitles()` no longer shells out for DeepL, and app workflows validate `DEEPL_API_KEY` before launching DeepL translation. | Keep dependency/install coverage and add live-provider smoke validation outside the fast suite. |
| Translation memory/glossary | `ai/memory.py` and translation code | Native LLM translation loads/saves `translation_glossary.json` and updates glossary entries returned by the model. | Keep live-provider smoke validation and revisit glossary prompt customization in the future agentic workflow. |
| Translation evaluation | `ai/evaluation.py`, `-evaluate` | Not exposed in Pandrator dubbing UI. | Do not port. Remove from the Pandrator baseline. |
| Context passing across LLM blocks | `-context` | Native correction/translation honors an advanced `context` setting. | Preserve default and test prompt context behavior as prompts evolve. |
| No-remove-subtitles mode | `--no-remove-subtitles` | Native correction honors an advanced `no_remove_subtitles` setting. | Keep as advanced control. |
| Custom translation/system/glossary prompts | `-t_prompt`, `-gloss_prompt`, `-sys_prompt`, `-translate_prompt` | Native translation exposes translation instructions; evaluation prompts are intentionally excluded. | Decide later whether glossary/system prompts need separate advanced fields. |
| OpenRouter provider routing params | `-provider`, `-sort`, `-fallbacks`, `-ignore`, `-data-collection`, `-require-parameters` | Pandrator LLM providers can represent OpenRouter models, but not these task params. | Do not port for the baseline. Use Pandrator's normal provider/model configuration. |
| Speech block generation | `subtitles/chunking.py` | Native `dubbing.speech_blocks` writes Pandrator/Subdub-compatible JSON, returns the generated JSON path to `AppLogic`, and imports it into sentence state. | Keep JSON schema compatible and add fixtures as edge cases appear. |
| Speech block language-aware splitting | `SentenceSplitter` and fallback splitting | Native splitting uses `sentence-splitter` when available and deterministic fallbacks otherwise. | Keep target-language mapping aligned with Pandrator TTS languages. |
| XTTS-only Subdub TTS generation | `media/tts.py` | Pandrator already has multi-provider TTS generation. | Do not copy as separate system. Map feature to Pandrator `start_generation()` and provider abstraction. |
| Audio alignment of generated WAVs | `media/audio.py` | Native alignment-block construction and pydub alignment are implemented with explicit SRT/speech-block/WAV inputs; `dubbing_handler.synchronize_audio_with_metadata()` now returns the sync artifact paths to `AppLogic`; the Pixi `smoke` environment proves the local FFmpeg sync/mux path with synthetic media. | Add live-media verification with representative user media. |
| Original/dubbed audio mixing | `media/ffmpeg.py` | Native FFmpeg mix path exists; original-audio extraction, volume analysis, amplification, mix, and mux command builders are deterministic and covered; dubbed-only replacement also uses a tested command builder; `smoke-dubbing-local` validates the basic FFmpeg mux path. | Add live-media verification outside the fast deterministic suite. |
| Sync existing generated audio to video | `workflows/dubbing.py`, task `sync` | Pandrator exposes "Add Dubbing to Video"; runtime sync receives explicit SRT and speech-block paths from `AppLogic`, then registers the returned mixed-video path and consumes the returned dubbed-audio path directly. | Add live-media verification and continue reducing fallback discovery in compatibility helpers. |
| Subtitle equalization | `srt_equalizer` wrapper | Native equalization wraps subtitle lines using Pandrator settings, no longer shells out, and returns the equalized SRT path directly to `AppLogic`. | Keep line-length controls in the advanced dialog and live-test rendered output. |
| Soft subtitles | Pandrator local FFmpeg helper | Native command construction maps the selected target language to subtitle metadata. | Keep command coverage and live-test muxed output. |
| Burned subtitles | Pandrator local FFmpeg helper with libass/subtitles filter probing | Already Pandrator-owned. | Keep and test escaping/capability detection. |
| Dubbed-only video output | Pandrator local FFmpeg helper | Dubbed-only replacement now uses tested native command construction. | Keep live-media verification. |
| Zoom VTT transcript correction | `subtitles/zoom.py`, `workflows/pipeline.py`, `ai/correction.py` | Native parser, grouping, chunking, and LLM correction service exist in `dubbing.zoom`; `Add Source -> Correct Zoom VTT` imports the corrected transcript as a text source. | Live-test with a real Zoom VTT and configured LLM provider. |
| Cost logging | `ai/client.py` callbacks and `PipelineState.total_cost` | `dubbing_runs` now stores `llm_cost_total`, `llm_response_count`, and per-stage `llm_usage_json`; native app-level correction/translation, Zoom correction, and transcription-internal correction record usage when they have an active run. | Live-validate provider-reported costs. |
| API key validation | `cli_helpers.py` | Pandrator-native validation now uses `llm_handler.validate_model_credentials()` plus `dubbing.credentials` checks for LLM correction/translation, Zoom correction, DeepL, and WhisperX diarization/HF token requirements. | Keep the validation deterministic and use the live smoke harness only for real provider/runtime checks. |
| Gemini audio transcription | Not found in current Subdub `main`; user explicitly excludes it. | N/A. | Do not port. |

## Concrete Findings and Gaps

### Pandrator Findings

1. `pandrator/app_logic.py` is the largest maintainability issue.
   - It is about 6426 lines and mixes session lifecycle, GUI-facing notifications, source import, dubbing orchestration, TTS connection, generation, playback, RVC, provider management, and output rendering.
   - Dubbing should be extracted first because it is the immediate integration target and has a clean set of domain stages.

2. The old Subdub-named adapter duplicated LLM provider resolution.
   - It had its own provider/model aliases, API key env mapping, local endpoint handling, and model option construction.
   - The same concepts already exist in `pandrator/logic/llm_handler.py`.
   - Resolved for native paths: ported Subdub LLM calls use `llm_handler` as the single source of truth.

3. Pandrator currently exposes only a subset of Subdub's options.
   - Current UI/state covers source language, Whisper model, correction enable, custom correction prompt, translation enable, source/target language, glossary, translation provider/model, matching video selection, advanced Whisper options, diarization, boundary correction, save TXT, LLM block sizing, max line length, no-remove-subtitles, translation prompt, delay start, and speed-up cap.
   - Separate system-prompt controls remain deferred because the baseline now routes prompts through Pandrator-owned correction/translation services and the later agentic workflow may change the prompt surface.
   - Resegment modes are deferred, while translation evaluation and task-specific OpenRouter routing flags are intentionally out of scope for the baseline.

4. `DubbingSettings.chain_of_thought_enabled` dead state is removed.
   - Reasoning controls now live in general LLM settings as `reasoning_effort`.
   - Legacy session payloads containing the old key are ignored by dataclass state loading.

5. Manual timing status is now visible in the dubbing stage grid.
   - `AppLogic` records `manual_timing`.
   - `TaskStatusPanel.DUBBING_STAGE_DEFINITIONS` includes the same ordered step keys as `state_db_handler.DUBBING_STEPS`.
   - `tests/test_dubbing_status_definitions.py` now guards against future drift between persisted steps and visible badges without importing the GUI.

6. Artifact tracking is stronger than remaining file discovery.
   - Pandrator already records active dubbing artifacts in SQLite.
   - Normal SRT, speech-block JSON, equalized SRT, and manual timing audio lookups now prefer active artifact roles before falling back to filesystem discovery.
   - Transcription, speech-block generation, sync, and subtitle equalization now register explicit output paths returned by native stage APIs instead of rediscovering those outputs by modification time.
   - Artifact lookup now prefers SQLite roles and explicit native return values. Sync exposes its aligned WAV, amplified dubbed WAV, mixed WAV, and final video paths directly, so generated-audio scans have been removed.
   - SRT and suffix discovery rules now live in `dubbing.artifacts` instead of being embedded in `AppLogic`.

7. Soft subtitle language metadata now follows the target language in the native path.
   - `dubbing.video_muxing` maps Pandrator target languages to FFmpeg subtitle metadata codes.
   - Adapter tests cover command construction so the previous hardcoded English metadata does not regress.

8. Installer and packaging required Subdub.
   - Resolved in the current branch: clone/update/install/runtime-check paths and shared packaging entries were removed.

9. Dubbing had native LLM execution but retained a Subdub-shaped configuration contract.
   - Resolved: correction and translation now have independent native model IDs, `default` delegates to Pandrator's global model, custom providers retain the `custom:<provider>/<model>` form, and DeepL is a translation backend.

10. STT controls were not backend-aware in the UI.
    - Resolved: backend selection is on the primary transcription surface, Whisper model controls are hidden for Parakeet, and the advanced modal shows only the selected backend's settings. Zero-valued VAD/output controls now round-trip correctly.

11. Dubbing run snapshots could duplicate inline provider credentials.
    - Resolved: execution settings and persisted snapshots are separate, the database write boundary strips provider catalogs and redacts credentials, and schema migration sanitizes existing run snapshots.

### Subdub Findings

1. Whisper prompt is accepted but not applied.
   - `cli_args.py` exposes `-whisper_prompt`.
   - `tasks/input.py` passes it into `transcribe_audio()`.
   - `ai/transcribe.py` accepts `initial_prompt` but does not add it to the WhisperX command.
   - This should be fixed during the port with a characterization test.

2. `create_speech_blocks()` accepts `merge_threshold` but does not use it.
   - `subtitles/chunking.py` has a `merge_threshold` argument, but the function does not consult it.
   - Either implement intended behavior or remove it from the domain API after confirming no user-visible promise depends on it.

3. Sync chooses subtitles implicitly.
   - `workflows/dubbing.py:create_alignment_blocks()` selects the newest `.srt` in the session folder.
   - In Pandrator this can select the wrong artifact if source, corrected, translated, manual, and equalized SRTs coexist.
   - Resolved for the native path: Pandrator passes the selected active SRT artifact path into sync.

4. Sync infers language from speech-block filenames that speech-block generation does not emit.
   - `sync_audio_video()` looks for names like `_<lang>_speech_blocks`.
   - `create_speech_blocks()` writes `{video_name}_speech_blocks.json`.
   - This means sync can silently fall back to English language assumptions.
   - Pandrator should store target language in run metadata and pass it directly.

5. Subdub's standalone TTS is intentionally narrower than Pandrator's TTS layer.
   - It posts to `http://localhost:8020/tts_to_audio/` only.
   - Pandrator should not inherit that limitation.

6. Subdub's manual editor logic is reusable but the window is standalone.
   - The model/controller/persistence/presenter split is useful.
   - The app window creates/owns a `QApplication` when needed, which is correct for CLI but should be adapted for Pandrator's existing Qt process.

7. Subdub's CLI validation is env-var oriented.
   - Pandrator stores provider configs and explicit API keys differently.
   - Validation should move to Pandrator's provider config layer instead of copying Subdub's CLI checks.

8. Console output can appear mojibake under the default Windows shell encoding.
   - The files are valid UTF-8 when read with Python UTF-8 output.
   - Ported tools and subprocess logging should keep `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` behavior where applicable.

## Target Decomposition

Prefer an incremental package under the existing `pandrator.logic` namespace so imports and tests can migrate gradually.

Proposed structure:

```text
pandrator/
  logic/
    dubbing/
      __init__.py
      models.py
      pipeline.py
      ports.py
      artifacts.py
      input_preparation.py
      transcription.py
      boundary_correction.py
      subtitle_correction.py
      translation.py
      translation_memory.py
      speech_blocks.py
      audio_alignment.py
      video_muxing.py
      equalization.py
      zoom_transcript.py
      prompts/
        __init__.py
        correction.py
        translation.py
        zoom.py
    media/
      ffmpeg_tools.py
    llm_handler.py
    tts_handler.py
  gui/
    dialogs/
      boundary_editor/
        model.py
        controller.py
        persistence.py
        presenter.py
        player.py
        canvas.py
        dialogs.py
        window.py
    widgets/
      session_sections.py
      session_tab.py
```

Key boundaries:

- `dubbing.models`: typed dataclasses for `DubbingRunContext`, `DubbingSettings`, `SubtitleSegment`, `WordTimestamp`, `SpeechBlock`, `DubbingArtifact`, `DubbingStageResult`, and cost metadata.
- `dubbing.ports`: protocols for LLM, TTS, transcription process runner, FFmpeg runner, filesystem/artifact store, and progress reporting.
- `dubbing.pipeline`: orchestration only. It should not parse SRT, call LiteLLM directly, or build FFmpeg commands inline.
- `dubbing.artifacts`: translates domain artifact names to `state_db_handler` roles and filesystem paths.
- `dubbing.translation`: uses Pandrator `llm_handler.chat_completion_with_metadata()` rather than direct `litellm.completion()`.
- `dubbing.speech_blocks`: pure SRT-to-JSON logic, deterministic and heavily tested.
- `dubbing.audio_alignment`: pure-ish pydub/FFmpeg alignment logic with injectable temp directory and process runner.
- `dubbing.video_muxing`: FFmpeg command construction, probing, and output validation.
- `gui.dialogs.manual_timing_dialog`: Pandrator-owned boundary editor with no subprocess or second `QApplication`.

The end state should leave `AppLogic` as a coordinator that delegates to services and emits UI signals. It should not contain the media or LLM business rules.

## Current Working Plan

Use a narrow first implementation branch to prove the new architecture before touching the highest-risk LLM, WhisperX, and FFmpeg paths.

Implementation progress on 2026-07-09:

- Added repository-level Pixi tasks, `test-dubbing` and `smoke-dubbing-local`, plus Windows wrappers at `scripts/run_dubbing_tests.ps1` and `scripts/run_dubbing_local_smoke.ps1`.
- Added `pandrator.logic.dubbing` scaffolding with deterministic SRT utilities, Zoom transcript helpers, and speech-block generation.
- Implemented `merge_threshold` behavior for speech-block generation.
- Replaced Pandrator's speech-block generation adapter so it no longer shells out to `python -m subdub` for that stage.
- Added native subtitle equalization and replaced Pandrator's equalization adapter so it no longer shells out to `python -m subdub` for that stage.
- Fixed soft-subtitle FFmpeg metadata to use the selected target language instead of hardcoded English.
- Extracted subtitle mux FFmpeg command construction into `pandrator.logic.dubbing.video_muxing`.
- Added native LLM subtitle correction through Pandrator's `llm_handler` and replaced the standalone correction subprocess path.
- Added native non-DeepL LLM subtitle translation through Pandrator's `llm_handler`, including `[REMOVE]` handling, final block JSON, and `translation_glossary.json` updates.
- Fixed `llm_handler.chat_completion_with_metadata()` so `max_tokens` is forwarded to LiteLLM requests.
- Added native DeepL translation, DeepL language-code mapping, final block JSON output, and wrapper tests proving the DeepL path no longer shells out to Subdub.
- Added native WhisperX transcription orchestration with FFmpeg extraction, direct/Pixi WhisperX commands, align-model defaulting, prompt propagation, chunk size, diarization/HF token support, and SRT post-processing.
- Added ONNX Parakeet as a second optional STT backend with isolated Pixi execution, Silero VAD controls, backend installation detection, backend-specific language filtering, segment-based SRT output, persisted Parakeet JSON, and installer metadata under the STT backends group.
- Added `HF_TOKEN` to the API Keys tab and new dubbing state fields for advanced transcription controls.
- Added an advanced dubbing settings dialog for transcription, LLM, and sync controls that should not live on the primary session surface.
- Made the primary transcription UI backend-aware: installed backend selection is visible, languages are filtered by backend, and Whisper model controls are hidden for Parakeet.
- Split the advanced transcription modal into shared, WhisperX, and Parakeet groups; Parakeet VAD controls are directly visible and valid zero settings no longer reset to defaults.
- Replaced the Subdub-shaped translation provider/model contract with native `correction_model`, `translation_backend`, and `translation_model` fields, including one-way legacy session migration.
- Routed correction, translation, and Zoom through stage-specific native LLM resolution; each LLM stage supports Pandrator built-ins, custom providers, and the global `default` model.
- Separated live execution credentials from persisted dubbing run settings and added database-level snapshot sanitization.
- Added a dedicated `gui-tests` Pixi environment and offscreen regression coverage for backend-specific transcription controls.
- Added native audio sync orchestration with explicit SRT/speech-block/Sentence_wavs inputs, pydub alignment, FFmpeg mix commands, and wrapper tests proving sync no longer shells out to Subdub.
- Added native manual timing model/controller/persistence logic with split, merge, boundary edit, text edit, SRT export, and JSON correction export coverage.
- Added an in-process Pandrator manual timing dialog and an `AppLogic` main-thread signal bridge so the dubbing worker can pause for UI edits without launching Subdub.
- Removed the remaining Subdub manual-timing subprocess bridge and stale duplicated model-resolution code from the native dubbing adapter.
- Added native automatic boundary correction from WhisperX JSON, plus word-boundary correction helpers for future STT/word-timestamp integration.
- Moved dubbed-only video audio replacement behind `dubbing.video_muxing.build_replace_video_audio_command()` and added adapter coverage.
- Added native Zoom VTT transcript correction using Pandrator's shared LLM provider handling, exposed through `Add Source -> Correct Zoom VTT`.
- Added SQLite-backed dubbing LLM usage aggregation with run-level totals and per-stage usage JSON; native correction/translation helpers now return file paths plus cost metadata.
- Added `scripts/dubbing_live_smoke.py` for FFmpeg sync/mux, WhisperX, DeepL, Zoom VTT, and LLM-provider runtime checks outside the deterministic Pixi suite. The Pixi `smoke` environment now supplies FFmpeg and `pydub` for the synthetic local sync/mux check.
- Added focused unittest coverage for the ported deterministic logic and verified it through the Pixi wrapper.

First branch:

1. Establish Pixi as the documented test command.
2. Add `pandrator.logic.dubbing` scaffolding and domain models.
3. Port deterministic subtitle utilities, Zoom transcript parsing helpers, and speech-block generation.
4. Implement `merge_threshold`.
5. Add fixture tests and run them through Pixi.
6. Replace Pandrator's `generate_speech_blocks()` Subdub subprocess path. Completed; the native adapter now returns the generated speech-block JSON path.

Second branch:

1. Extend `llm_handler` for the correction/translation baseline.
2. Port LLM correction, LLM translation, DeepL translation, glossary handling, and prompt overrides. Baseline correction, LLM translation, DeepL translation, glossary load/save, and translation-instructions UI are native.
3. Add advanced settings dialogs for non-primary controls. Initial dialog is complete for transcription, LLM, and sync controls; more manual-timing controls may be added as that stage moves.
4. Keep resegment and agentic/tool-based workflows deferred.

Later branches should run full live media/provider validation and decide whether richer waveform/search-replace tooling belongs in the timing dialog before the agentic correction/translation redesign.

## Migration Phases

### Phase 0: Baseline and Guardrails

Deliverables:

- Keep Pandrator aligned with origin before each migration branch.
- Keep this roadmap in `docs/`.
- Add a migration tracking checklist in a future issue/PR description.
- Establish Pixi test environment instructions. The active interpreter currently lacks the runtime deps needed to run the suite.

Tests:

- Add a fast test subset that does not require PyQt/audio/LLM/network.
- Capture current Subdub behavior with fixture tests before changing algorithms, except for deliberately fixed bugs such as missing Whisper prompt propagation and unused `merge_threshold`.

### Phase 1: Characterization Tests

Port or recreate tests for:

- SRT renumbering.
- Speaker-aware merging.
- Translation block creation.
- Word block creation.
- Speech-block generation.
- DeepL response parsing and provider mapping.
- LLM response parsing for correction/translation.
- Zoom transcript parsing and correction helpers.
- Boundary correction invariants.
- Manual editor model/controller/persistence.
- Audio alignment command construction and timing math with fake audio segments.
- Video mux command construction for soft/burned subtitles.

Add new tests for known gaps:

- Whisper prompt is passed to WhisperX.
- `merge_threshold` behavior is implemented.
- Sync consumes explicit SRT/speech-block artifacts rather than newest-file discovery.
- Soft subtitle language metadata follows target language.

### Phase 2: Port Pure Subtitle and Speech-Block Logic

Move into Pandrator:

- `subtitles/srt_utils.py`
- `subtitles/chunking.py`
- `subtitles/zoom.py`
- Prompt templates needed by correction/translation/zoom.

Adjustments:

- Use Pandrator naming and `pathlib` at boundaries.
- Keep JSON schema compatible with current Subdub/Pandrator speech-block files.
- Add typed models at the boundary and preserve current Pandrator session compatibility. Do not add broad support for arbitrary old Subdub work folders.

### Phase 3: Consolidate LLM Handling

Extend `llm_handler` rather than copying `subdub.ai.client`.

Needed additions:

- Optional system prompt.
- Optional structured output schema or JSON mode support.
- Provider/request overrides such as `api_base`, `max_tokens`, and `reasoning_effort` where they fit Pandrator's provider config.
- Cost metadata returned consistently.
- Stronger JSON response parsing helpers.
- Dubbing-specific prompt calls that preserve Subdub behavior.

Then port:

- `correct_subtitles`
- `translate_blocks`
- `manage_glossary` and `save_glossary`
- DeepL translation behind a `TranslationProvider` interface.

Implementation status:

- `correct_subtitles` baseline is native and uses Pandrator `llm_handler`.
- Non-DeepL `translate_blocks` baseline is native and uses Pandrator `llm_handler`.
- Glossary load/save is native for LLM translation via `translation_glossary.json`.
- DeepL translation baseline is native and uses the `deepl` package behind an injectable client boundary.

Explicitly defer:

- `resegment_and_correct_with_llm`
- `resegment_and_translate_with_llm`
- all translation evaluation paths
- task-specific OpenRouter routing flags

### Phase 4: Port Transcription and Boundary Correction

Move WhisperX and boundary correction into Pandrator:

- FFmpeg audio extraction.
- Direct `whisperx` command.
- Pandrator Pixi fallback.
- Align model defaulting.
- Diarization/HF token via Pandrator's general credentials store.
- Boundary correction and word-level correction.
- Manual correction audio preparation.

Fix while porting:

- Pass the Whisper prompt to the actual WhisperX invocation.
- Preserve UTF-8 subprocess environment behavior.
- Make boundary correction configurable and testable.

Implementation status:

- Baseline FFmpeg extraction, direct/Pixi WhisperX command construction, align-model defaulting, prompt propagation, chunk size, diarization/HF token handling, and SRT post-processing are native and covered by fake-runner tests.
- Automatic segment boundary correction is native and can switch WhisperX to JSON output when enabled. Word-level correction helpers are ported but not yet exposed as an end-user workflow.

### Phase 5: Integrate Manual Boundary Editor

Port Subdub's manual editor into `pandrator.gui.dialogs.manual_timing_dialog`.

Requirements:

- Use the existing `QApplication`.
- Do not block unrelated Pandrator cleanup/shutdown logic.
- Return explicit saved SRT/JSON artifact paths.
- Fold manual timing into the correction/translation workflow and expose detailed progress inside that workflow.
- Keep split, merge up/down, boundary drag, search/replace, context playback, SRT export, JSON export, and waveform visualization.

Implementation status:

- Native `manual_timing` model/controller/persistence is complete and covered by deterministic tests.
- A baseline Pandrator-owned PyQt dialog is wired through `AppLogic` with a main-thread signal/result bridge. It supports table text editing, start/end boundary controls, adjacent subtitle shifting, split/merge, selected-segment playback when QtMultimedia is available, and SRT/JSON save.
- The remaining manual-timing subprocess bridge has been removed from the native dubbing adapter.
- Remaining: port waveform visualization, drag-to-boundary editing, search/replace, and richer context playback from Subdub's GUI if those remain worth keeping after the agentic correction/translation workflow is designed.

### Phase 6: Port Sync, Audio Alignment, and Video Muxing

Move into Pandrator:

- Audio block alignment.
- Speed-up via FFmpeg `atempo`.
- Original audio extraction.
- Dubbed audio normalization/amplification.
- Original+dub mix.
- Dubbed-only replacement.
- Soft subtitle mux.
- Burned subtitle render.
- Equalization.

Adjustments:

- Accept explicit artifact paths instead of scanning directories.
- Store target language in the run and use it in subtitle metadata.
- Use temporary files and atomic replace consistently.
- Put FFmpeg command construction behind testable helpers.

Implementation status:

- Native alignment-block construction, sentence-WAV matching, pydub alignment, FFmpeg original/dub mix, and `dubbing_handler.synchronize_audio()` routing are complete.
- Runtime sync now receives explicit SRT and speech-block paths from `AppLogic` and returns synced-video plus intermediate audio paths directly instead of asking Subdub or filesystem discovery to choose the newest generated audio.
- Remaining cleanup: run `scripts/dubbing_live_smoke.py` in a full runtime with representative media/provider credentials and preserve the resulting output/logs. The synthetic local FFmpeg sync/mux smoke check now passes through Pixi.

### Phase 7: Replace Subdub Subprocess Calls Stage by Stage

Recommended order:

1. Speech blocks. Completed in the first native slice.
2. Zoom transcript utilities. Parser/chunking helpers and native LLM correction are complete; the UI exposes import through `Add Source -> Correct Zoom VTT`.
3. Equalization. Completed in the second native slice.
4. Final subtitle muxing cleanup is Pandrator-owned. Soft-subtitle language metadata, subtitle command construction, dubbed-only replacement, and audio sync/mix command construction tests are complete.
5. Translation/correction using unified LLM layer. Baseline LLM correction, non-DeepL LLM translation, DeepL translation, glossary load/save, advanced prompt UI, run-level LLM usage aggregation, and deterministic provider credential validation are complete; live-provider smoke validation remains.
6. Transcription and diarization. Baseline WhisperX wrapper, prompt propagation, diarization token handling, merge threshold, and automatic boundary correction are complete; live-media verification remains.
7. Sync/audio alignment/mix. Native baseline, command-builder cleanup, and explicit sync artifact metadata are complete; live-media verification remains.
8. Manual correction GUI. Baseline native dialog is complete; waveform/search-replace parity remains optional follow-up.

The deprecated `subdub_handler.py` import shim has been removed. Active Pandrator code uses `dubbing_handler.py`, and tests assert no `python -m subdub` command is built for any native stage.

### Phase 8: Installer, Packaging, and Documentation Cleanup

Current status:

- Removed Subdub clone/update/install from `pandrator_installer/workflows.py`.
- Removed `SUBDUB_REPO_URL`, `SUBDUB_RUNTIME_CHECK_COMMAND`, and Subdub repair specs from installer constants.
- Removed `WorkspacePaths.subdub_repo`.
- Removed `Subdub` from packaging shared paths.
- Updated README install/dependency language so users are no longer instructed to clone or install Subdub for Pandrator dubbing.
- Added a README migration note for existing workspaces that still contain a stale `Subdub` checkout.

Remaining:

- Run the provider/media live smoke harness in a packaged/runtime environment with WhisperX media, DeepL, Zoom VTT input, and the selected LLM provider. The local synthetic FFmpeg sync/mux check is covered by `pixi run -e smoke smoke-dubbing-local`.
- Revisit optional manual-timing waveform dependencies only if waveform visualization/search tooling is promoted from deferred follow-up into the baseline.

## Best-Practice Guidelines for the Refactor

- Prefer small services with explicit inputs/outputs over methods that inspect global UI state.
- Use dataclasses or typed dicts at module boundaries. Avoid unvalidated dict plumbing for new code.
- Keep domain logic independent of PyQt. GUI code should call services and render progress, not parse SRT or build FFmpeg commands.
- Keep package initializers lightweight. Optional runtime dependencies such as NumPy/WhisperX/DeepL should be imported by the feature modules that need them, not by unrelated dubbing imports.
- Use `pathlib.Path` internally where practical, but keep compatibility with existing string paths at public boundaries.
- Keep reducing modification-time artifact selection for active runs. Use artifact roles in SQLite first and prefer explicit return paths from stage services.
- Keep file writes atomic for JSON/SRT outputs where possible.
- Make external tools injectable in tests: WhisperX runner, FFmpeg runner, DeepL client, LLM client, TTS client.
- Add fixture-based tests before changing algorithms.
- Preserve current Pandrator session behavior, but do not add long-term support for arbitrary old Subdub work folders.
- Do not duplicate provider/model resolution. `llm_handler` and `tts_handler` should be the only provider configuration sources.
- Separate deterministic algorithms from network calls so CI can cover most behavior without provider keys.
- Keep cancellation and progress reporting as first-class pipeline concerns.
- Keep future installer changes scoped to concrete runtime/package requirements.

## Test Baseline Observed

Commands attempted:

```powershell
python -m pytest -q
$env:PYTHONPATH='src'; python -m pytest -q
python -m unittest discover -s tests -v
$env:PYTHONPATH='src'; python -m unittest discover -s tests -v
```

Results:

- `pytest` is not installed in the active Python interpreter.
- Pandrator `unittest` discovery ran 114 tests and failed with 15 errors.
- The Pandrator failures were mostly missing dependencies in the active interpreter, including `pydub`, `fitz`, `ebooklib`, and `unidecode`.
- The earlier Windows temp cleanup failures in `StateDBHandlerTests` were traced to test-owned `sqlite3.connect(...)` handles that were not explicitly closed; the tests now wrap those handles with `contextlib.closing`.
- Subdub `unittest` discovery ran 16 tests and failed with 3 import errors due to missing `litellm` and `numpy`.
- No lingering Python/RVC/uvicorn/FastAPI/pixi processes were found after the test attempts.

This means the current environment does not prove either suite green. The migration should start by making Pixi the documented test command and ensuring the fast deterministic subset runs there.

## Acceptance Criteria for Deprecating Subdub

Subdub can be removed from Pandrator's installer/runtime only when all of the following are true:

- Pandrator can transcribe video/audio inputs with WhisperX direct/Pixi fallback and with optional ONNX Parakeet through its dedicated Pixi environment.
- Pandrator can perform diarization using credentials from Pandrator's general credentials store.
- Pandrator can correct subtitles with the same or better LLM behavior.
- Pandrator can translate subtitles with LLM and DeepL providers.
- Pandrator can perform translation memory/glossary workflows.
- Pandrator can correct Zoom VTT transcripts.
- Pandrator can generate compatible speech-block JSON from SRT.
- Pandrator can import speech blocks into sentence generation and produce sentence WAVs through its own TTS providers.
- Pandrator can align generated audio to source video timing.
- Pandrator can produce mixed original+dubbed output and dubbed-only output.
- Pandrator can equalize subtitles.
- Pandrator can create soft-subtitle, burned-subtitle, and both-mode final videos.
- Pandrator can open and save manual timing edits inside correction/translation workflows without launching a Subdub subprocess.
- Pandrator can handle existing Pandrator sessions without depending on arbitrary Subdub work-folder compatibility.
- Installer no longer clones or installs Subdub.
- README and installer docs no longer instruct users to clone Subdub for Pandrator dubbing.
- Pixi test commands cover the ported domain logic and the main workflow seams without requiring live LLM/TTS services.

## Recommended Next Action

The remaining planning/implementation decisions are now narrower:

1. Decide how word-level WhisperX JSON imports and Parakeet token JSON should fit into the broader STT integration work; future subtitle segmentation may use `wtpsplit-lite` over backend text/tokens instead of relying only on backend-provided segments.
2. Run `scripts/dubbing_live_smoke.py` in a full runtime with WhisperX media, DeepL credentials, Zoom VTT input, and one LLM provider configured; local synthetic FFmpeg sync/mux is covered by the Pixi `smoke` environment.
3. Decide whether waveform visualization, drag-to-boundary editing, search/replace, and richer context playback should be ported into the native timing dialog before the agentic correction/translation redesign.
