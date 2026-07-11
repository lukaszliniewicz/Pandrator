import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from .dubbing.equalization import equalize_srt_file
from .dubbing.credentials import settings_use_deepl
from .dubbing.llm_correction import correct_srt_file, correct_srt_file_with_result
from .dubbing.llm_translation import translate_srt_file, translate_srt_file_deepl
from .dubbing.audio_sync import AudioSyncResult, synchronize_audio_video_with_result
from .dubbing.speech_blocks import generate_speech_blocks_file
from .dubbing.transcription import transcribe_video_file
from .dubbing.video_muxing import (
    build_add_subtitles_command,
    build_replace_video_audio_command,
    escape_ffmpeg_subtitles_filter_path,
)


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INSTALL_ROOT = os.path.abspath(os.path.join(PROJECT_ROOT, ".."))
WHISPERX_PIXI_EXE_ENV = "WHISPERX_PIXI_EXE"
WHISPERX_PIXI_MANIFEST_ENV = "WHISPERX_PIXI_MANIFEST"
PARAKEET_PIXI_EXE_ENV = "PARAKEET_PIXI_EXE"
PARAKEET_PIXI_MANIFEST_ENV = "PARAKEET_PIXI_MANIFEST"
WHISPERX_PIXI_MANIFEST_PATH = os.path.join(PROJECT_ROOT, "envs", "whisperx_installer", "pixi.toml")
PARAKEET_PIXI_MANIFEST_PATH = os.path.join(PROJECT_ROOT, "envs", "parakeet_onnx_installer", "pixi.toml")
WHISPERX_PIXI_MANIFEST_CANDIDATES = (
    WHISPERX_PIXI_MANIFEST_PATH,
    os.path.join(INSTALL_ROOT, "envs", "whisperx_installer", "pixi.toml"),
)
PARAKEET_PIXI_MANIFEST_CANDIDATES = (
    PARAKEET_PIXI_MANIFEST_PATH,
    os.path.join(INSTALL_ROOT, "envs", "parakeet_onnx_installer", "pixi.toml"),
)
WHISPERX_PIXI_EXE_CANDIDATES = (
    os.path.join(PROJECT_ROOT, "bin", "pixi.exe"),
    os.path.join(PROJECT_ROOT, "bin", "pixi"),
    os.path.join(INSTALL_ROOT, "bin", "pixi.exe"),
    os.path.join(INSTALL_ROOT, "bin", "pixi"),
)
BUNDLED_FFMPEG_CANDIDATES = (
    os.path.join(PROJECT_ROOT, "bin", "ffmpeg.exe"),
    os.path.join(PROJECT_ROOT, "bin", "ffmpeg"),
)
SYNC_OVERWRITE_GUARDED_AUDIO_STEMS = (
    "original_audio",
    "mixed_audio",
    "amplified_dubbed_audio",
    "aligned_audio",
)
SYNC_STALE_SYNC_SRT_SUFFIXES = (
    "_equalized",
    "_final",
)
SYNC_OUTPUT_VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
)

MANUAL_CORRECTION_AUDIO_EXTENSIONS = (
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".aac",
    ".opus",
)

_FFMPEG_SUBTITLES_SUPPORT_CACHE: dict[str, bool] = {}


@dataclass(frozen=True)
class DubbingTranscriptionResult:
    output_path: str = ""
    correction_cost: float = 0.0
    correction_response_count: int = 0


def _resolve_dubbing_path(path: str) -> str:
    expanded = os.path.expanduser(str(path or "").strip())
    return os.path.abspath(expanded) if expanded else expanded


def _first_existing_path(candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


def transcribe_video_with_metadata(
    session_dir: str,
    video_file: str,
    settings: dict,
    correction_prompt: str = "",
) -> DubbingTranscriptionResult:
    """Transcribes an audio/video media file and returns output path plus correction usage."""
    try:
        output_path = transcribe_video_file(
            session_dir=session_dir,
            video_file=video_file,
            settings=settings,
            ffmpeg_executable=_first_existing_path(BUNDLED_FFMPEG_CANDIDATES) or "ffmpeg",
            pixi_executable=os.environ.get(WHISPERX_PIXI_EXE_ENV, "") or _first_existing_path(WHISPERX_PIXI_EXE_CANDIDATES),
            pixi_manifest=os.environ.get(WHISPERX_PIXI_MANIFEST_ENV, "")
            or _first_existing_path(WHISPERX_PIXI_MANIFEST_CANDIDATES),
            parakeet_pixi_executable=os.environ.get(PARAKEET_PIXI_EXE_ENV, "")
            or _first_existing_path(WHISPERX_PIXI_EXE_CANDIDATES),
            parakeet_pixi_manifest=os.environ.get(PARAKEET_PIXI_MANIFEST_ENV, "")
            or _first_existing_path(PARAKEET_PIXI_MANIFEST_CANDIDATES),
        )
        logging.info("Transcription completed: %s", output_path)

        return DubbingTranscriptionResult(output_path=output_path)
    except Exception as error:
        logging.error(
            "Transcription failed for '%s': %s",
            video_file,
            error,
            exc_info=True,
        )
        return DubbingTranscriptionResult()


def transcribe_source_with_metadata(
    session_dir: str,
    source_file: str,
    settings: dict,
    correction_prompt: str = "",
) -> DubbingTranscriptionResult:
    """Transcribes an audio/video media source and returns output path plus correction usage."""
    return transcribe_video_with_metadata(session_dir, source_file, settings, correction_prompt)


def transcribe_source_with_result(session_dir: str, source_file: str, settings: dict, correction_prompt: str = "") -> str:
    """Transcribes an audio/video media source and returns the produced SRT path."""
    return transcribe_source_with_metadata(
        session_dir=session_dir,
        source_file=source_file,
        settings=settings,
        correction_prompt=correction_prompt,
    ).output_path


def transcribe_source(session_dir: str, source_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Transcribes an audio/video media source using Pandrator-native STT orchestration."""
    return bool(transcribe_source_with_result(session_dir, source_file, settings, correction_prompt))


def transcribe_video_with_result(session_dir: str, video_file: str, settings: dict, correction_prompt: str = "") -> str:
    """Transcribes an audio/video media file and returns the produced SRT path."""
    return transcribe_video_with_metadata(
        session_dir=session_dir,
        video_file=video_file,
        settings=settings,
        correction_prompt=correction_prompt,
    ).output_path


def transcribe_video(session_dir: str, video_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Transcribes an audio/video media file using Pandrator-native STT orchestration."""
    return bool(transcribe_video_with_result(session_dir, video_file, settings, correction_prompt))


def correct_subtitles(session_dir: str, srt_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Corrects an SRT file using Pandrator-native LLM logic."""
    try:
        output_path = correct_srt_file(
            session_dir=session_dir,
            srt_file=srt_file,
            settings=settings,
            correction_instructions=correction_prompt,
        )
        logging.info("Subtitle correction completed: %s", output_path)
        return True
    except Exception as error:
        logging.error(
            "Subtitle correction failed for '%s': %s",
            srt_file,
            error,
            exc_info=True,
        )
        return False


def translate_subtitles(session_dir: str, srt_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Translates an SRT file using Pandrator-native LLM or DeepL logic."""
    if not settings_use_deepl(settings):
        try:
            output_path = translate_srt_file(
                session_dir=session_dir,
                srt_file=srt_file,
                settings=settings,
                translation_instructions=str(settings.get("translate_prompt") or ""),
            )
            logging.info("Subtitle translation completed: %s", output_path)
            return True
        except Exception as error:
            logging.error(
                "Subtitle translation failed for '%s': %s",
                srt_file,
                error,
                exc_info=True,
            )
            return False

    if settings.get("glossary_enabled"):
        logging.warning("Glossary memory is ignored for DeepL translation.")
    try:
        output_path = translate_srt_file_deepl(
            session_dir=session_dir,
            srt_file=srt_file,
            settings=settings,
        )
        logging.info("DeepL subtitle translation completed: %s", output_path)
        return True
    except Exception as error:
        logging.error(
            "DeepL subtitle translation failed for '%s': %s",
            srt_file,
            error,
            exc_info=True,
        )
        return False


def generate_speech_blocks_with_result(session_dir: str, srt_file: str, target_language: str = "en") -> str:
    """Generates speech blocks from an SRT file and returns the JSON path."""
    try:
        output_path = generate_speech_blocks_file(
            session_dir=session_dir,
            srt_file=srt_file,
            target_language=target_language,
        )
        logging.info("Speech block generation completed: %s", output_path)
        return output_path
    except Exception as error:
        logging.error(
            "Speech block generation failed for '%s': %s",
            srt_file,
            error,
            exc_info=True,
        )
        return ""


def generate_speech_blocks(session_dir: str, srt_file: str, target_language: str = "en") -> bool:
    """Generates speech blocks from an SRT file using Pandrator-native logic."""
    return bool(generate_speech_blocks_with_result(session_dir, srt_file, target_language))


def _clear_previous_sync_outputs(session_dir: str) -> None:
    """Removes stale sync artifacts that can block FFmpeg overwrite."""
    if not os.path.isdir(session_dir):
        return

    removed_files: list[str] = []
    for file_name in os.listdir(session_dir):
        file_path = os.path.join(session_dir, file_name)
        if not os.path.isfile(file_path):
            continue

        lower_name = file_name.lower()
        stem, extension = os.path.splitext(lower_name)

        should_remove = False
        if extension == ".wav" and any(
            stem.startswith(audio_stem)
            for audio_stem in SYNC_OVERWRITE_GUARDED_AUDIO_STEMS
        ):
            should_remove = True
        elif extension in SYNC_OUTPUT_VIDEO_EXTENSIONS and stem.startswith("final_output"):
            should_remove = True
        elif extension == ".srt" and any(stem.endswith(suffix) for suffix in SYNC_STALE_SYNC_SRT_SUFFIXES):
            should_remove = True

        if not should_remove:
            continue

        try:
            os.remove(file_path)
            removed_files.append(file_path)
        except OSError as error:
            logging.warning("Could not remove stale sync artifact '%s': %s", file_path, error)

    if removed_files:
        logging.info(
            "Removed %d stale sync artifact(s) before synchronization.",
            len(removed_files),
        )


def _latest_file_with_suffix(session_dir: str, suffix: str) -> str:
    if not os.path.isdir(session_dir):
        return ""
    candidates = [
        os.path.join(session_dir, file_name)
        for file_name in os.listdir(session_dir)
        if file_name.lower().endswith(suffix.lower())
        and os.path.isfile(os.path.join(session_dir, file_name))
    ]
    if not candidates:
        return ""
    return max(candidates, key=lambda path: (os.path.getmtime(path), path.lower()))


def _latest_sync_srt(session_dir: str) -> str:
    if not os.path.isdir(session_dir):
        return ""
    candidates = []
    for file_name in os.listdir(session_dir):
        lower_name = file_name.lower()
        if not lower_name.endswith(".srt"):
            continue
        stem = os.path.splitext(lower_name)[0]
        if stem.endswith("_equalized") or stem.endswith("_final"):
            continue
        full_path = os.path.join(session_dir, file_name)
        if os.path.isfile(full_path):
            candidates.append(full_path)
    if not candidates:
        return ""
    return max(candidates, key=lambda path: (os.path.getmtime(path), path.lower()))


def synchronize_audio_with_metadata(
    session_dir: str,
    video_file: str = "",
    srt_file: str = "",
    speech_blocks_file: str = "",
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
) -> AudioSyncResult:
    """Synchronizes generated audio and returns the produced sync artifacts."""
    resolved_session_dir = _resolve_dubbing_path(session_dir)
    resolved_video_file = _resolve_dubbing_path(video_file)
    resolved_srt_file = _resolve_dubbing_path(srt_file) if srt_file else _latest_sync_srt(resolved_session_dir)
    resolved_speech_blocks_file = (
        _resolve_dubbing_path(speech_blocks_file)
        if speech_blocks_file
        else _latest_file_with_suffix(resolved_session_dir, "_speech_blocks.json")
    )
    _clear_previous_sync_outputs(resolved_session_dir)

    try:
        result = synchronize_audio_video_with_result(
            session_dir=resolved_session_dir,
            video_file=resolved_video_file,
            srt_file=resolved_srt_file,
            speech_blocks_file=resolved_speech_blocks_file,
            delay_start_ms=delay_start_ms,
            speed_up_percent=speed_up_percent,
            ffmpeg_executable=_first_existing_path(BUNDLED_FFMPEG_CANDIDATES) or "ffmpeg",
        )
        logging.info("Audio synchronization completed: %s", result.output_video_path)
        return result
    except Exception as error:
        logging.error(
            "Audio synchronization failed for session '%s': %s",
            session_dir,
            error,
            exc_info=True,
        )
        return AudioSyncResult()


def synchronize_audio_with_result(
    session_dir: str,
    video_file: str = "",
    srt_file: str = "",
    speech_blocks_file: str = "",
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
) -> str:
    """Synchronizes generated audio and returns the mixed video path."""
    return synchronize_audio_with_metadata(
        session_dir=session_dir,
        video_file=video_file,
        srt_file=srt_file,
        speech_blocks_file=speech_blocks_file,
        delay_start_ms=delay_start_ms,
        speed_up_percent=speed_up_percent,
    ).output_video_path


def synchronize_audio(
    session_dir: str,
    video_file: str = "",
    srt_file: str = "",
    speech_blocks_file: str = "",
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
) -> bool:
    """Synchronizes generated audio with the original video using Pandrator-native logic."""
    return bool(
        synchronize_audio_with_result(
            session_dir=session_dir,
            video_file=video_file,
            srt_file=srt_file,
            speech_blocks_file=speech_blocks_file,
            delay_start_ms=delay_start_ms,
            speed_up_percent=speed_up_percent,
        )
    )


def equalize_subtitles_with_result(srt_file: str) -> str:
    """Equalizes subtitle line breaks and returns the output SRT path."""
    try:
        output_path = equalize_srt_file(srt_file)
        logging.info("Subtitle equalization completed: %s", output_path)
        return output_path
    except Exception as error:
        logging.error(
            "Subtitle equalization failed for '%s': %s",
            srt_file,
            error,
            exc_info=True,
        )
        return ""


def equalize_subtitles(srt_file: str) -> bool:
    """Equalizes subtitle line breaks using Pandrator-native logic."""
    return bool(equalize_subtitles_with_result(srt_file))


def find_latest_audio_file(session_dir: str) -> str | None:
    """Finds the newest audio file in a session directory."""
    if not os.path.isdir(session_dir):
        return None

    candidates: list[str] = []
    for file_name in os.listdir(session_dir):
        if not file_name.lower().endswith(MANUAL_CORRECTION_AUDIO_EXTENSIONS):
            continue

        full_path = os.path.join(session_dir, file_name)
        if os.path.isfile(full_path):
            candidates.append(full_path)

    if not candidates:
        return None

    return max(candidates, key=lambda path: (os.path.getmtime(path), path.lower()))


def extract_audio_for_manual_correction(video_file: str, session_dir: str) -> str | None:
    """Extracts a mono WAV reference track from an audio/video source for the timing dialog."""
    if not video_file or not os.path.exists(video_file):
        logging.error("Cannot extract manual correction audio: media path is invalid (%s).", video_file)
        return None

    os.makedirs(session_dir, exist_ok=True)

    video_stem = os.path.splitext(os.path.basename(video_file))[0]
    audio_path = os.path.join(session_dir, f"{video_stem}_manual_correction.wav")

    if os.path.exists(audio_path):
        try:
            if os.path.getsize(audio_path) > 0 and os.path.getmtime(audio_path) >= os.path.getmtime(video_file):
                return audio_path
        except OSError:
            pass

    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-i",
        video_file,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-af",
        "aresample,loudnorm",
        audio_path,
    ]

    logging.info("Extracting audio for manual timing correction: %s", " ".join(ffmpeg_command))
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is not None:
            for line in process.stdout:
                logging.info("FFmpeg (Manual Correction): %s", line.strip())
        process.wait()

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command)

        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            raise RuntimeError("FFmpeg did not produce a valid audio file for manual correction.")

        return audio_path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error("Failed to extract audio for manual correction: %s", e)
        return None
    except Exception as e:
        logging.error("Unexpected error while preparing manual correction audio: %s", e)
        return None


def _escape_ffmpeg_subtitles_filter_path(path: str) -> str:
    """Escapes an absolute subtitle path for FFmpeg subtitles filter usage."""
    return escape_ffmpeg_subtitles_filter_path(path)


def _normalize_executable_cache_key(executable: str) -> str:
    normalized = str(executable or "").strip()
    if not normalized:
        return ""

    expanded = os.path.expandvars(os.path.expanduser(normalized))
    resolved = shutil.which(expanded) if not os.path.isabs(expanded) else expanded
    final_path = resolved or expanded
    return os.path.normcase(os.path.abspath(final_path))


def _ffmpeg_supports_subtitles_filter(ffmpeg_executable: str) -> bool:
    cache_key = _normalize_executable_cache_key(ffmpeg_executable)
    if not cache_key:
        return False

    cached = _FFMPEG_SUBTITLES_SUPPORT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    supported = False
    try:
        probe = subprocess.run(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-v",
                "error",
                "-h",
                "filter=subtitles",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        probe_output = str(probe.stdout or "")
        lowered_output = probe_output.lower()
        supported = (
            "unknown filter 'subtitles'" not in lowered_output
            and "no such filter" not in lowered_output
        )
    except (FileNotFoundError, OSError):
        supported = False

    _FFMPEG_SUBTITLES_SUPPORT_CACHE[cache_key] = supported
    return supported


def _discover_ffmpeg_candidates() -> list[str]:
    candidates: list[str] = []

    explicit_ffmpeg = str(os.environ.get("PANDRATOR_FFMPEG_EXE") or "").strip()
    if explicit_ffmpeg:
        candidates.append(explicit_ffmpeg)

    for bundled_candidate in BUNDLED_FFMPEG_CANDIDATES:
        if os.path.isfile(bundled_candidate):
            candidates.append(bundled_candidate)

    default_ffmpeg = shutil.which("ffmpeg")
    if default_ffmpeg:
        candidates.append(default_ffmpeg)

    candidates.append("ffmpeg")

    if os.name == "nt":
        try:
            where_result = subprocess.run(
                ["where.exe", "ffmpeg"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
            if where_result.returncode == 0:
                for raw_line in str(where_result.stdout or "").splitlines():
                    candidate = raw_line.strip().strip('"')
                    if candidate:
                        candidates.append(candidate)
        except Exception:
            pass

    unique_candidates: list[str] = []
    seen_keys: set[str] = set()
    for candidate in candidates:
        cache_key = _normalize_executable_cache_key(candidate)
        if not cache_key or cache_key in seen_keys:
            continue
        seen_keys.add(cache_key)
        unique_candidates.append(candidate)

    return unique_candidates


def _resolve_ffmpeg_for_burned_subtitles() -> str | None:
    for candidate in _discover_ffmpeg_candidates():
        if _ffmpeg_supports_subtitles_filter(candidate):
            return candidate
    return None


def replace_video_audio_track(video_path: str, audio_path: str, output_video_path: str) -> bool:
    """Creates a copy of a video with its audio replaced by the provided track."""
    if not video_path or not os.path.exists(video_path):
        logging.error("Cannot replace video audio track: video path is invalid (%s).", video_path)
        return False

    if not audio_path or not os.path.exists(audio_path):
        logging.error("Cannot replace video audio track: audio path is invalid (%s).", audio_path)
        return False

    output_abs_path = os.path.abspath(output_video_path)
    output_dir = os.path.dirname(output_abs_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    output_name = os.path.basename(output_abs_path)
    output_stem, output_ext = os.path.splitext(output_name)
    temp_ext = output_ext or ".mp4"
    temp_output_path = os.path.join(output_dir or ".", f".{output_stem}_tmp{temp_ext}")

    ffmpeg_command = build_replace_video_audio_command(
        video_path=video_path,
        audio_path=audio_path,
        temp_output_path=temp_output_path,
        ffmpeg_executable=_first_existing_path(BUNDLED_FFMPEG_CANDIDATES) or "ffmpeg",
    )

    logging.info(
        "Executing FFmpeg command to replace video audio track: %s",
        " ".join(ffmpeg_command),
    )
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in process.stdout:
            logging.info(f"FFmpeg: {line.strip()}")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command)

        if not os.path.exists(temp_output_path) or os.path.getsize(temp_output_path) == 0:
            raise RuntimeError("FFmpeg did not create a valid dubbed-only video.")

        os.replace(temp_output_path, output_abs_path)
        logging.info("Created dubbed-only video output: %s", output_abs_path)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error("Failed to replace video audio track: %s", e)
        return False
    except Exception as e:
        logging.error("Unexpected error while replacing video audio track: %s", e)
        return False
    finally:
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError:
                pass


def add_subtitles_to_video(
    synced_video_path: str,
    equalized_srt_path: str,
    output_video_path: str,
    subtitle_mode: str = "soft",
    subtitle_language: str = "en",
) -> bool:
    """Adds subtitles to a video file using FFmpeg.

    Writes to a temporary file first and only replaces the target output on
    success, so a failed remux does not destroy an existing final video.
    """
    normalized_subtitle_mode = str(subtitle_mode or "soft").strip().lower()
    if normalized_subtitle_mode not in {"soft", "burned"}:
        normalized_subtitle_mode = "soft"

    output_abs_path = os.path.abspath(output_video_path)
    output_dir = os.path.dirname(output_abs_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    output_name = os.path.basename(output_abs_path)
    output_stem, output_ext = os.path.splitext(output_name)
    temp_ext = output_ext or ".mp4"
    temp_output_path = os.path.join(output_dir or ".", f".{output_stem}_tmp{temp_ext}")

    if normalized_subtitle_mode == "burned":
        ffmpeg_executable = _resolve_ffmpeg_for_burned_subtitles()
        if not ffmpeg_executable:
            logging.error(
                "Cannot burn subtitles: no FFmpeg executable with the subtitles filter was found. "
                "Use an FFmpeg build compiled with --enable-libass, or use soft subtitles."
            )
            return False

        ffmpeg_command = build_add_subtitles_command(
            synced_video_path=synced_video_path,
            equalized_srt_path=equalized_srt_path,
            temp_output_path=temp_output_path,
            subtitle_mode=normalized_subtitle_mode,
            subtitle_language=subtitle_language,
            ffmpeg_executable=ffmpeg_executable,
        )
    else:
        ffmpeg_command = build_add_subtitles_command(
            synced_video_path=synced_video_path,
            equalized_srt_path=equalized_srt_path,
            temp_output_path=temp_output_path,
            subtitle_mode=normalized_subtitle_mode,
            subtitle_language=subtitle_language,
            ffmpeg_executable="ffmpeg",
        )

    logging.info(
        "Executing FFmpeg command to add subtitles (%s): %s",
        normalized_subtitle_mode,
        " ".join(ffmpeg_command),
    )
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in process.stdout:
            logging.info(f"FFmpeg: {line.strip()}")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command)

        if not os.path.exists(temp_output_path) or os.path.getsize(temp_output_path) == 0:
            raise RuntimeError("FFmpeg did not create a valid output video.")

        os.replace(temp_output_path, output_abs_path)
        logging.info(
            "Subtitles have been successfully added to the final video (%s mode).",
            normalized_subtitle_mode,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error(f"Failed to add subtitles: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error while adding subtitles: {e}")
        return False
    finally:
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError:
                pass
