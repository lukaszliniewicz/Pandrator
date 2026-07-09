# Dubbing Migration Test Commands

The Subdub integration migration uses a small Pixi task for deterministic tests that do not require PyQt, audio devices, live LLM providers, TTS servers, or FFmpeg. A separate Pixi environment supplies PyQt for focused offscreen widget tests.

From the Pandrator repository root:

```powershell
pixi run test-dubbing
```

On Windows, prefer the repository wrapper because it keeps Pixi cache and home directories inside the workspace:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_dubbing_tests.ps1
```

The task currently runs:

```powershell
python -m unittest tests.test_dubbing_subtitle_logic tests.test_dubbing_speech_blocks_integration tests.test_dubbing_llm_correction tests.test_dubbing_llm_translation tests.test_dubbing_transcription tests.test_dubbing_audio_sync tests.test_dubbing_boundary_correction tests.test_dubbing_manual_timing tests.test_dubbing_run_usage tests.test_dubbing_status_definitions tests.test_dubbing_artifacts tests.test_dubbing_credentials tests.test_dubbing_settings tests.test_llm_handler tests.test_state_db_handler
```

Current focused coverage: 107 tests passing through Pixi on 2026-07-09.

Backend-aware transcription widget coverage:

```powershell
pixi run -e gui-tests test-dubbing-ui
```

Current GUI coverage: 5 offscreen PyQt tests passing on 2026-07-09. They verify Whisper model visibility, native/custom LLM model catalog binding, Parakeet VAD visibility and enablement, and zero-value settings round-tripping.

Installer cleanup coverage:

```powershell
pixi run -e gui-tests python -m unittest tests.test_installer_architecture tests.test_installer_launcher_chatterbox
```

Current installer cleanup coverage: 47 tests passing through Pixi on 2026-07-09.

Use these commands before replacing a Subdub subprocess stage with Pandrator-native logic. The broader application suite still requires the full Pandrator runtime environment.

## Optional Live Smoke Checks

The live smoke harness exercises runtime tools and provider-backed paths that the deterministic Pixi suite intentionally skips.

Default local check in the active Python environment:

```powershell
python scripts\dubbing_live_smoke.py
```

This tries the native FFmpeg sync/mux path with synthetic media. It skips cleanly if FFmpeg or `pydub` is missing from the active Python/runtime environment.

For reproducible local FFmpeg validation, use the Pixi smoke environment:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_dubbing_local_smoke.ps1
# or:
pixi run -e smoke smoke-dubbing-local
```

The smoke environment includes FFmpeg and `pydub`; it still skips provider-backed checks unless their inputs are supplied.

Provider-backed checks are opt-in because they can download models or consume API quota:

```powershell
# One configured LLM provider
$env:PANDRATOR_SMOKE_LLM_MODEL="openai/gpt-5.4-mini"
python scripts\dubbing_live_smoke.py --skip-ffmpeg --llm-model $env:PANDRATOR_SMOKE_LLM_MODEL

# Zoom VTT correction through the same LLM path
python scripts\dubbing_live_smoke.py --zoom-vtt "C:\path\meeting.vtt" --llm-model $env:PANDRATOR_SMOKE_LLM_MODEL --output-dir ".smoke-output"

# DeepL translation
$env:DEEPL_API_KEY="..."
python scripts\dubbing_live_smoke.py --skip-ffmpeg --deepl --output-dir ".smoke-output"

# WhisperX transcription
python scripts\dubbing_live_smoke.py --whisperx-video "C:\path\sample.mp4" --whisperx-model small --output-dir ".smoke-output"
```

Any `FAILED` check exits non-zero. `SKIPPED` means required input, credentials, or runtime dependencies were not supplied.
