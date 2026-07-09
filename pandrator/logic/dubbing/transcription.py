"""WhisperX transcription helpers for the Pandrator-native dubbing pipeline."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from .languages import normalize_language_code
from ..source_media import is_audio_source
from .stt_backends import STT_BACKEND_PARAKEET_ONNX, normalize_stt_backend
from .srt_utils import merge_subtitles_with_speaker_awareness, renumber_subtitles

logger = logging.getLogger(__name__)

DEFAULT_WHISPER_PROMPT = (
    "Hello, welcome to this presentation. This is a professional recording with clear speech, "
    "proper punctuation, and standard grammar."
)
DEFAULT_WHISPER_CHUNK_SIZE = 15
WHISPERX_PIXI_EXE_ENV = "WHISPERX_PIXI_EXE"
WHISPERX_PIXI_MANIFEST_ENV = "WHISPERX_PIXI_MANIFEST"

ALIGN_MODELS_BY_LANGUAGE = {
    "pl": "jonatasgrosman/wav2vec2-xls-r-1b-polish",
    "nl": "GroNLP/wav2vec2-dutch-large-ft-cgn",
    "de": "aware-ai/wav2vec2-xls-r-1b-german",
    "en": "jonatasgrosman/wav2vec2-xls-r-1b-english",
    "fr": "jonatasgrosman/wav2vec2-xls-r-1b-french",
    "it": "jonatasgrosman/wav2vec2-xls-r-1b-italian",
    "ru": "jonatasgrosman/wav2vec2-xls-r-1b-russian",
    "es": "jonatasgrosman/wav2vec2-xls-r-1b-spanish",
    "ja": "vumichien/wav2vec2-xls-r-1b-japanese",
    "hu": "sarpba/wav2vec2-large-xlsr-53-hungarian",
    "sq": "Alimzhan/wav2vec2-large-xls-r-300m-albanian-colab",
}


class ExternalToolError(RuntimeError):
    """Raised when an external transcription tool fails."""


def safe_decode(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        return output.decode("utf-8", errors="ignore")


def resolve_align_model(language: str, explicit_align_model: str = "") -> str:
    explicit = str(explicit_align_model or "").strip()
    if explicit:
        return explicit
    return ALIGN_MODELS_BY_LANGUAGE.get(normalize_language_code(language, default=""), "")


def extract_audio(
    video_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    video_name: str,
    *,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
) -> str:
    audio_path = Path(session_dir) / f"{video_name}.wav"
    try:
        if audio_path.resolve() == Path(video_path).resolve():
            audio_path = Path(session_dir) / f"{video_name}_transcription.wav"
    except OSError:
        pass

    command = [
        ffmpeg_executable,
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-af",
        "aresample,loudnorm",
        "-y",
        str(audio_path),
    ]
    try:
        result = run_func(command, check=True, capture_output=True)
        if getattr(result, "stderr", None):
            logger.warning("FFmpeg warning: %s", safe_decode(result.stderr))
    except subprocess.CalledProcessError as error:
        logger.error("FFmpeg audio extraction failed:\n%s", safe_decode(getattr(error, "stderr", None)))
        raise ExternalToolError("FFmpeg failed to extract audio.") from error
    return str(audio_path)


def transcribe_source_file(
    session_dir: str | os.PathLike[str],
    source_file: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    ffmpeg_executable: str = "ffmpeg",
    pixi_executable: str = "",
    pixi_manifest: str = "",
    parakeet_pixi_executable: str = "",
    parakeet_pixi_manifest: str = "",
    run_func: Callable[..., Any] = subprocess.run,
    boundary_audio_loader: Callable[[str | os.PathLike[str], int], Any] | None = None,
) -> str:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    source_path = Path(source_file)
    source_name = source_path.stem

    audio_path = extract_audio(
        source_path,
        session_path,
        source_name,
        ffmpeg_executable=ffmpeg_executable,
        run_func=run_func,
    )
    if is_audio_source(str(source_path)):
        logger.info("Normalized audio source for transcription: %s", audio_path)
    boundary_correction_enabled = bool(
        settings.get("boundary_correction_enabled")
        or settings.get("boundary_correction")
    )
    stt_backend = normalize_stt_backend(settings.get("stt_backend"))
    if stt_backend == STT_BACKEND_PARAKEET_ONNX:
        from .parakeet_onnx import transcribe_audio_with_parakeet

        if boundary_correction_enabled:
            logger.warning(
                "WhisperX JSON boundary correction is not available for ONNX Parakeet yet; "
                "using Parakeet VAD segment boundaries."
            )
        if bool(settings.get("diarization_enabled") or settings.get("diarize")):
            logger.warning("Diarization is currently only supported by the WhisperX STT backend.")

        transcribed_srt = transcribe_audio_with_parakeet(
            audio_path,
            session_dir=session_path,
            video_name=source_name,
            settings=settings,
            pixi_executable=parakeet_pixi_executable,
            pixi_manifest=parakeet_pixi_manifest,
            run_func=run_func,
        )
    else:
        transcribed_output = transcribe_audio(
            audio_path,
            language=str(settings.get("stt_language") or settings.get("whisper_language") or "English"),
            session_dir=session_path,
            video_name=source_name,
            whisper_model=str(settings.get("whisper_model") or "large-v3"),
            align_model=str(settings.get("whisper_align_model") or settings.get("align_model") or ""),
            initial_prompt=str(settings.get("whisper_prompt") or DEFAULT_WHISPER_PROMPT),
            diarize=bool(settings.get("diarization_enabled") or settings.get("diarize")),
            hf_token=str(settings.get("hf_token") or os.environ.get("HF_TOKEN") or ""),
            chunk_size=int(settings.get("whisper_chunk_size") or DEFAULT_WHISPER_CHUNK_SIZE),
            output_format="json" if boundary_correction_enabled else "srt",
            save_txt=bool(settings.get("whisper_save_txt") or settings.get("save_txt")),
            pixi_executable=pixi_executable or os.environ.get(WHISPERX_PIXI_EXE_ENV, ""),
            pixi_manifest=pixi_manifest or os.environ.get(WHISPERX_PIXI_MANIFEST_ENV, ""),
            run_func=run_func,
        )
        if boundary_correction_enabled:
            from .boundary_correction import correct_boundaries_from_json_file, load_audio

            correction_result = correct_boundaries_from_json_file(
                transcribed_output,
                audio_path,
                output_srt_path=session_path / f"{source_name}_corrected.srt",
                audio_loader=boundary_audio_loader or load_audio,
            )
            transcribed_srt = correction_result.srt_path
        else:
            transcribed_srt = transcribed_output

    return postprocess_transcribed_srt(
        transcribed_srt,
        merge_threshold=int(settings.get("merge_threshold") or settings.get("subtitle_merge_threshold") or 250),
    )


def build_whisperx_args(
    audio_path: str | os.PathLike[str],
    *,
    language: str,
    session_dir: str | os.PathLike[str],
    whisper_model: str,
    align_model: str = "",
    initial_prompt: str = "",
    diarize: bool = False,
    hf_token: str = "",
    chunk_size: int = DEFAULT_WHISPER_CHUNK_SIZE,
    output_format: str = "srt",
    save_txt: bool = False,
) -> list[str]:
    if diarize and not str(hf_token or "").strip():
        raise ValueError("HF_TOKEN is required for WhisperX diarization.")

    normalized_language = normalize_language_code(language, default="en")
    args = [
        str(audio_path),
        "--model",
        str(whisper_model or "large-v3"),
        "--language",
        normalized_language,
        "--output_format",
        "all" if save_txt else output_format,
        "--output_dir",
        str(session_dir),
        "--print_progress",
        "True",
        "--vad_method",
        "silero",
        "--chunk_size",
        str(max(1, int(chunk_size or DEFAULT_WHISPER_CHUNK_SIZE))),
    ]

    resolved_align_model = resolve_align_model(normalized_language, align_model)
    if resolved_align_model:
        args.extend(["--align_model", resolved_align_model])

    prompt = str(initial_prompt or "").strip()
    if prompt:
        args.extend(["--initial_prompt", prompt])

    if diarize:
        args.extend(["--diarize", "--hf_token", str(hf_token).strip()])

    return args


def build_whisperx_commands(
    whisperx_args: list[str],
    *,
    pixi_executable: str = "",
    pixi_manifest: str = "",
) -> list[list[str]]:
    commands = [["whisperx", *whisperx_args]]
    if pixi_executable and pixi_manifest:
        commands.append(
            [
                pixi_executable,
                "run",
                "--manifest-path",
                pixi_manifest,
                "--executable",
                "python",
                "-m",
                "whisperx",
                *whisperx_args,
            ]
        )
    return commands


def run_whisperx_commands(
    commands: list[list[str]],
    *,
    run_func: Callable[..., Any] = subprocess.run,
) -> None:
    errors: list[str] = []
    for command in commands:
        try:
            logger.info("Running WhisperX command: %s", " ".join(command))
            result = run_func(command, check=True, capture_output=True)
            if getattr(result, "stderr", None):
                logger.warning("WhisperX warning: %s", safe_decode(result.stderr))
            return
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            stderr = safe_decode(getattr(error, "stderr", None))
            suffix = f"\n{stderr}" if stderr else ""
            errors.append(f"{command[0]}: {error}{suffix}")
            logger.warning("WhisperX command failed: %s", error)

    raise ExternalToolError("WhisperX failed with all configured execution paths:\n" + "\n".join(errors))


def _move_whisperx_output(
    audio_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    video_name: str,
    expected_format: str,
) -> str:
    session_path = Path(session_dir)
    output_path = session_path / f"{video_name}.{expected_format}"
    generated_base = Path(audio_path).stem
    generated_path = session_path / f"{generated_base}.{expected_format}"

    if generated_path.exists():
        generated_path.replace(output_path)
        return str(output_path)

    candidates = sorted(session_path.glob(f"{generated_base}*.{expected_format}"))
    if candidates:
        candidates[0].replace(output_path)
        return str(output_path)

    raise ExternalToolError(
        f"WhisperX did not produce the expected {expected_format.upper()} output file. "
        f"Looked for {generated_path} and matching {generated_base}*.{expected_format} files."
    )


def transcribe_audio(
    audio_path: str | os.PathLike[str],
    *,
    language: str,
    session_dir: str | os.PathLike[str],
    video_name: str,
    whisper_model: str,
    align_model: str = "",
    initial_prompt: str = DEFAULT_WHISPER_PROMPT,
    diarize: bool = False,
    hf_token: str = "",
    chunk_size: int = DEFAULT_WHISPER_CHUNK_SIZE,
    output_format: str = "srt",
    save_txt: bool = False,
    pixi_executable: str = "",
    pixi_manifest: str = "",
    run_func: Callable[..., Any] = subprocess.run,
) -> str:
    whisperx_args = build_whisperx_args(
        audio_path,
        language=language,
        session_dir=session_dir,
        whisper_model=whisper_model,
        align_model=align_model,
        initial_prompt=initial_prompt,
        diarize=diarize,
        hf_token=hf_token,
        chunk_size=chunk_size,
        output_format=output_format,
        save_txt=save_txt,
    )
    commands = build_whisperx_commands(
        whisperx_args,
        pixi_executable=pixi_executable,
        pixi_manifest=pixi_manifest,
    )
    run_whisperx_commands(commands, run_func=run_func)
    return _move_whisperx_output(audio_path, session_dir, video_name, output_format)


def postprocess_transcribed_srt(
    srt_path: str | os.PathLike[str],
    *,
    merge_threshold: int = 250,
) -> str:
    path = Path(srt_path)
    with path.open("r", encoding="utf-8-sig") as handle:
        content = handle.read()
    renumbered = renumber_subtitles(content)
    merged, _has_diarization = merge_subtitles_with_speaker_awareness(renumbered, merge_threshold)
    if merged == content:
        return str(path)

    output_path = path.with_name(f"{path.stem}_merged{path.suffix}")
    output_path.write_text(merged, encoding="utf-8")
    return str(output_path)


def transcribe_video_file(
    session_dir: str | os.PathLike[str],
    video_file: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    ffmpeg_executable: str = "ffmpeg",
    pixi_executable: str = "",
    pixi_manifest: str = "",
    parakeet_pixi_executable: str = "",
    parakeet_pixi_manifest: str = "",
    run_func: Callable[..., Any] = subprocess.run,
    boundary_audio_loader: Callable[[str | os.PathLike[str], int], Any] | None = None,
) -> str:
    return transcribe_source_file(
        session_dir=session_dir,
        source_file=video_file,
        settings=settings,
        ffmpeg_executable=ffmpeg_executable,
        pixi_executable=pixi_executable,
        pixi_manifest=pixi_manifest,
        parakeet_pixi_executable=parakeet_pixi_executable,
        parakeet_pixi_manifest=parakeet_pixi_manifest,
        run_func=run_func,
        boundary_audio_loader=boundary_audio_loader,
    )
