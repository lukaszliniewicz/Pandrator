"""Optional live smoke checks for Pandrator-native dubbing services.

The default run only attempts the local FFmpeg/audio sync path. Provider-backed
checks are opt-in because they can be slow, consume API quota, or require model
downloads.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pandrator.logic import llm_handler
try:
    from pandrator.logic.dubbing import audio_sync, llm_translation, transcription, zoom
except ModuleNotFoundError as error:
    audio_sync = None
    llm_translation = None
    transcription = None
    zoom = None
    DUBBING_IMPORT_ERROR = f"Required Python dependency is not installed: {error.name}."
else:
    DUBBING_IMPORT_ERROR = ""


SAMPLE_SRT = """1
00:00:00,500 --> 00:00:01,200
Hello there.
"""

SAMPLE_ZOOM_VTT = """WEBVTT

00:00:00.500 --> 00:00:01.200
Alice: hello there um
"""


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_sample_sync_files(session_dir: Path, ffmpeg: str) -> tuple[Path, Path, Path, Path]:
    wavs_dir = session_dir / "Sentence_wavs"
    wavs_dir.mkdir(parents=True, exist_ok=True)
    video_path = session_dir / "source.mp4"
    srt_path = session_dir / "source.srt"
    speech_blocks_path = session_dir / "source_speech_blocks.json"
    sentence_wav_path = wavs_dir / "sentence_1.wav"

    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            str(video_path),
        ]
    )
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=0.45",
            "-ac",
            "1",
            "-ar",
            "44100",
            str(sentence_wav_path),
        ]
    )
    srt_path.write_text(SAMPLE_SRT, encoding="utf-8")
    speech_blocks_path.write_text(
        json.dumps(
            [
                {
                    "number": "1",
                    "time": "00:00:00.500",
                    "text": "Hello there.",
                    "subtitles": [1],
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return video_path, srt_path, speech_blocks_path, wavs_dir


def _status(name: str, status: str, detail: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def smoke_ffmpeg_sync(ffmpeg: str) -> dict[str, str]:
    if not ffmpeg:
        return _status("ffmpeg_sync_mux", "skipped", "FFmpeg executable was not found.")
    if audio_sync is None:
        return _status("ffmpeg_sync_mux", "skipped", DUBBING_IMPORT_ERROR)

    try:
        import pydub  # noqa: F401
    except ImportError:
        return _status("ffmpeg_sync_mux", "skipped", "pydub is not installed in this Python environment.")

    try:
        with tempfile.TemporaryDirectory(prefix="pandrator_dubbing_smoke_") as temp_root:
            session_dir = Path(temp_root)
            video_path, srt_path, speech_blocks_path, wavs_dir = _write_sample_sync_files(session_dir, ffmpeg)
            output_path = audio_sync.synchronize_audio_video(
                session_dir=session_dir,
                video_file=video_path,
                srt_file=srt_path,
                speech_blocks_file=speech_blocks_path,
                sentence_wavs_dir=wavs_dir,
                delay_start_ms=0,
                speed_up_percent=115,
                ffmpeg_executable=ffmpeg,
            )
            output = Path(output_path)
            if not output.is_file() or output.stat().st_size <= 0:
                return _status("ffmpeg_sync_mux", "failed", f"No valid output produced at {output_path}")
            return _status("ffmpeg_sync_mux", "passed", str(output))
    except Exception as error:
        return _status("ffmpeg_sync_mux", "failed", str(error))


def _llm_settings(model_name: str, provider_id: str = "") -> dict[str, Any]:
    settings = {
        "translation_model": model_name,
        "translation_provider": provider_id,
        "llm_provider_configs": llm_handler.get_provider_configs(None),
        "request_timeout_seconds": 180,
        "reasoning_effort": "",
        "llm_char": 6000,
        "max_tokens": 1000,
    }
    return settings


def smoke_llm(model_name: str, provider_id: str = "") -> dict[str, str]:
    if not model_name:
        return _status("llm_provider", "skipped", "Pass --llm-model to run this check.")

    try:
        result = llm_handler.chat_completion_with_metadata(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            model_name=model_name,
            llm_settings={
                "provider_configs": llm_handler.get_provider_configs(None),
                "request_timeout_seconds": 180,
                "reasoning_effort": "",
            },
            max_tokens=16,
            temperature=0.0,
        )
        content = str(result.content or "").strip()
        if not content:
            return _status("llm_provider", "failed", "Provider returned an empty response.")
        return _status("llm_provider", "passed", content[:80])
    except Exception as error:
        return _status("llm_provider", "failed", str(error))


def smoke_zoom_vtt(vtt_path: str, output_dir: Path, model_name: str, provider_id: str = "") -> dict[str, str]:
    if not vtt_path:
        return _status("zoom_vtt", "skipped", "Pass --zoom-vtt to run this check.")
    if not model_name:
        return _status("zoom_vtt", "skipped", "Pass --llm-model with --zoom-vtt.")

    input_path = Path(vtt_path)
    if not input_path.is_file():
        return _status("zoom_vtt", "failed", f"Input file does not exist: {input_path}")
    if zoom is None:
        return _status("zoom_vtt", "skipped", DUBBING_IMPORT_ERROR)

    try:
        result = zoom.correct_zoom_vtt_file(
            input_path,
            output_dir,
            _llm_settings(model_name, provider_id=provider_id),
        )
        if not result.transcript_text.strip():
            return _status("zoom_vtt", "failed", "Corrected transcript was empty.")
        return _status("zoom_vtt", "passed", result.output_path)
    except Exception as error:
        return _status("zoom_vtt", "failed", str(error))


def smoke_deepl(enabled: bool, output_dir: Path) -> dict[str, str]:
    if not enabled:
        return _status("deepl", "skipped", "Pass --deepl to run this check.")
    if not os.environ.get("DEEPL_API_KEY", "").strip():
        return _status("deepl", "skipped", "DEEPL_API_KEY is not set.")
    if llm_translation is None:
        return _status("deepl", "skipped", DUBBING_IMPORT_ERROR)

    try:
        srt_path = output_dir / "deepl_sample.srt"
        srt_path.write_text(SAMPLE_SRT, encoding="utf-8")
        result = llm_translation.translate_srt_file_deepl_with_result(
            output_dir,
            srt_path,
            {
                "original_language": "English",
                "target_language": "PL",
                "llm_char": 6000,
            },
        )
        if not Path(result.output_path).is_file():
            return _status("deepl", "failed", "DeepL translation did not produce an output file.")
        return _status("deepl", "passed", result.output_path)
    except Exception as error:
        return _status("deepl", "failed", str(error))


def smoke_whisperx(
    video_path: str,
    output_dir: Path,
    ffmpeg: str,
    whisper_model: str,
    pixi_executable: str = "",
    pixi_manifest: str = "",
) -> dict[str, str]:
    if not video_path:
        return _status("whisperx", "skipped", "Pass --whisperx-video to run this check.")
    input_path = Path(video_path)
    if not input_path.is_file():
        return _status("whisperx", "failed", f"Input file does not exist: {input_path}")
    if not ffmpeg:
        return _status("whisperx", "skipped", "FFmpeg executable was not found.")
    if transcription is None:
        return _status("whisperx", "skipped", DUBBING_IMPORT_ERROR)

    try:
        result_path = transcription.transcribe_video_file(
            output_dir,
            input_path,
            {
                "whisper_language": "English",
                "whisper_model": whisper_model,
                "whisper_prompt": transcription.DEFAULT_WHISPER_PROMPT,
                "whisper_chunk_size": transcription.DEFAULT_WHISPER_CHUNK_SIZE,
                "subtitle_merge_threshold": 250,
            },
            ffmpeg_executable=ffmpeg,
            pixi_executable=pixi_executable,
            pixi_manifest=pixi_manifest,
        )
        if not Path(result_path).is_file():
            return _status("whisperx", "failed", "WhisperX did not produce an SRT file.")
        return _status("whisperx", "passed", result_path)
    except Exception as error:
        return _status("whisperx", "failed", str(error))


def _print_results(results: list[dict[str, str]]) -> int:
    failed = False
    for result in results:
        name = result["name"]
        status = result["status"]
        detail = result.get("detail", "")
        print(f"[{status.upper()}] {name}: {detail}")
        failed = failed or status == "failed"
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg") or "")
    parser.add_argument("--output-dir", default="", help="Directory for provider-backed smoke outputs.")
    parser.add_argument("--llm-model", default=os.environ.get("PANDRATOR_SMOKE_LLM_MODEL", ""))
    parser.add_argument("--llm-provider", default=os.environ.get("PANDRATOR_SMOKE_LLM_PROVIDER", ""))
    parser.add_argument("--zoom-vtt", default="", help="Zoom/WebVTT file to correct with --llm-model.")
    parser.add_argument("--deepl", action="store_true", help="Run a live DeepL translation check.")
    parser.add_argument("--whisperx-video", default="", help="Media file to transcribe with WhisperX.")
    parser.add_argument("--whisperx-model", default=os.environ.get("PANDRATOR_SMOKE_WHISPERX_MODEL", "small"))
    parser.add_argument("--whisperx-pixi-exe", default=os.environ.get("WHISPERX_PIXI_EXE", ""))
    parser.add_argument("--whisperx-pixi-manifest", default=os.environ.get("WHISPERX_PIXI_MANIFEST", ""))
    parser.add_argument("--skip-ffmpeg", action="store_true", help="Skip the default local FFmpeg sync/mux check.")
    args = parser.parse_args(argv)

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        return _run_checks(args, output_dir)

    with tempfile.TemporaryDirectory(prefix="pandrator_dubbing_live_smoke_") as temp_root:
        return _run_checks(args, Path(temp_root))


def _run_checks(args: argparse.Namespace, output_dir: Path) -> int:
    results: list[dict[str, str]] = []
    if not args.skip_ffmpeg:
        results.append(smoke_ffmpeg_sync(args.ffmpeg))
    results.append(smoke_llm(args.llm_model, provider_id=args.llm_provider))
    results.append(smoke_zoom_vtt(args.zoom_vtt, output_dir, args.llm_model, provider_id=args.llm_provider))
    results.append(smoke_deepl(args.deepl, output_dir))
    results.append(
        smoke_whisperx(
            args.whisperx_video,
            output_dir,
            args.ffmpeg,
            args.whisperx_model,
            pixi_executable=args.whisperx_pixi_exe,
            pixi_manifest=args.whisperx_pixi_manifest,
        )
    )
    return _print_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
