"""CrispASR command construction, execution, and word-timing metadata."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .languages import normalize_language_code

CRISPASR_VERSION = "0.8.20"
CRISPASR_EXECUTABLE_ENV = "CRISPASR_EXECUTABLE"
CRISPASR_CACHE_DIR_ENV = "CRISPASR_CACHE_DIR"

STT_ENGINE_WHISPER = "whisper"
STT_ENGINE_PARAKEET = "parakeet"
STT_ENGINE_MOSS = "moss"

COMPUTE_BACKENDS = ("auto", "cpu", "cuda", "vulkan", "metal")


@dataclass(frozen=True)
class CrispASRModel:
    engine: str
    label: str
    repository: str
    filenames: dict[str, str]
    word_timing: str
    default_quantization: str = "f16"

    def filename_for(self, quantization: str | None) -> str:
        normalized = normalize_model_quantization(quantization, self.engine)
        return self.filenames[normalized]

    @property
    def hf_spec(self) -> str:
        return f"{self.repository}:{self.filename_for(self.default_quantization)}"

    @property
    def filename(self) -> str:
        return self.filename_for(self.default_quantization)


MODELS = {
    STT_ENGINE_WHISPER: CrispASRModel(
        engine=STT_ENGINE_WHISPER,
        label="Whisper large-v3 (F16)",
        repository="ggerganov/whisper.cpp",
        filenames={
            "f16": "ggml-large-v3.bin",
            "q5_0": "ggml-large-v3-q5_0.bin",
        },
        word_timing="dtw",
    ),
    STT_ENGINE_PARAKEET: CrispASRModel(
        engine=STT_ENGINE_PARAKEET,
        label="Parakeet TDT 0.6B v3 (F16)",
        repository="cstr/parakeet-tdt-0.6b-v3-GGUF",
        filenames={
            "f16": "parakeet-tdt-0.6b-v3.gguf",
            "q8_0": "parakeet-tdt-0.6b-v3-q8_0.gguf",
            "q5_0": "parakeet-tdt-0.6b-v3-q5_0.gguf",
            "q4_k": "parakeet-tdt-0.6b-v3-q4_k.gguf",
        },
        word_timing="native",
    ),
    STT_ENGINE_MOSS: CrispASRModel(
        engine=STT_ENGINE_MOSS,
        label="MOSS Transcribe-Diarize 0.9B (Q8_0)",
        repository="cstr/MOSS-Transcribe-Diarize-GGUF",
        filenames={
            "f16": "moss-transcribe-diarize-0.9b-f16.gguf",
            "q8_0": "moss-transcribe-diarize-0.9b-q8_0.gguf",
            "q4_k": "moss-transcribe-diarize-0.9b-q4_k.gguf",
        },
        word_timing="ctc",
        default_quantization="q8_0",
    ),
}


MODEL_QUANTIZATION_ALIASES = {
    "": "f16",
    "none": "f16",
    "fp16": "f16",
    "float16": "f16",
    "full": "f16",
    "int8": "q8_0",
    "q8": "q8_0",
    "q5": "q5_0",
    "q4": "q4_k",
    "q4_k_m": "q4_k",
}


def normalize_model_quantization(value: str | None, engine: str | None = None) -> str:
    normalized_engine = normalize_engine(engine)
    model = MODELS[normalized_engine]
    normalized = str(value or model.default_quantization).strip().lower().replace("-", "_")
    normalized = MODEL_QUANTIZATION_ALIASES.get(normalized, normalized)
    return normalized if normalized in model.filenames else model.default_quantization


@dataclass(frozen=True)
class CrispASRTranscriptionResult:
    srt_path: str
    word_timestamps_path: str
    engine: str
    compute_backend: str


class CrispASRError(RuntimeError):
    """Raised when CrispASR cannot complete a transcription."""


def normalize_engine(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {
        "moss",
        "moss_diarize",
        "moss_transcribe_diarize",
        "moss_transcribe_diarize_0.9b",
        "moss_diarize_0.9b",
    }:
        return STT_ENGINE_MOSS
    if normalized in {
        "parakeet",
        "parakeet_onnx",
        "onnx_parakeet",
        "parakeet_tdt_0.6b_v3",
        "nemo_parakeet_tdt_0.6b_v3",
    }:
        return STT_ENGINE_PARAKEET
    return STT_ENGINE_WHISPER


def _setting(settings: dict[str, Any], name: str, default: Any) -> Any:
    value = settings.get(name)
    return default if value is None or value == "" else value


def _cache_dir(settings: dict[str, Any]) -> str:
    return str(
        settings.get("crispasr_cache_dir")
        or os.environ.get(CRISPASR_CACHE_DIR_ENV)
        or ""
    ).strip()


def _append_runtime_options(command: list[str], settings: dict[str, Any]) -> None:
    cache_dir = _cache_dir(settings)
    if cache_dir:
        command.extend(("--cache-dir", cache_dir))
    threads = int(_setting(settings, "stt_threads", 0))
    if threads > 0:
        command.extend(("--threads", str(threads)))
    compute_backend = normalize_compute_backend(settings.get("stt_compute_backend"))
    if compute_backend != "auto":
        command.extend(("--gpu-backend", compute_backend))
    device = settings.get("stt_compute_device")
    if device not in (None, "") and compute_backend not in {"auto", "cpu"}:
        command.extend(("--device", str(max(0, int(device)))))


def moss_ctc_alignment_enabled(settings: dict[str, Any]) -> bool:
    return bool(_setting(settings, "moss_ctc_alignment_enabled", True))


def normalize_compute_backend(value: str | None) -> str:
    normalized = str(value or "auto").strip().lower()
    return normalized if normalized in COMPUTE_BACKENDS else "auto"


def candidate_executables(environ: dict[str, str] | None = None) -> tuple[Path, ...]:
    active = os.environ if environ is None else environ
    explicit = str(active.get(CRISPASR_EXECUTABLE_ENV) or "").strip()
    executable_name = "crispasr.exe" if os.name == "nt" else "crispasr"
    repo_root = Path(__file__).resolve().parents[3]
    install_root = repo_root.parent
    candidates = [Path(explicit)] if explicit else []
    for root in (repo_root, install_root):
        candidates.extend(
            (
                root / "CrispASR" / executable_name,
                root / "CrispASR" / "bin" / executable_name,
                root / "bin" / executable_name,
            )
        )
    return tuple(candidates)


def resolve_executable(explicit: str = "", environ: dict[str, str] | None = None) -> str:
    if str(explicit or "").strip():
        return str(Path(explicit).expanduser())
    for candidate in candidate_executables(environ):
        if candidate.is_file():
            return str(candidate)
    discovered = shutil.which("crispasr")
    return discovered or "crispasr"


def build_command(
    audio_path: str | os.PathLike[str],
    output_base: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    executable: str = "",
) -> list[str]:
    engine = normalize_engine(settings.get("stt_engine") or settings.get("stt_backend"))
    model = MODELS[engine]
    quantization = normalize_model_quantization(
        settings.get("stt_model_quantization")
        or settings.get("crispasr_model_quantization")
        or settings.get("parakeet_quantization"),
        engine,
    )
    model_filename = model.filename_for(quantization)
    language = normalize_language_code(
        str(settings.get("stt_language") or settings.get("whisper_language") or "auto"),
        default="auto",
    )
    backend = "moss-diarize" if engine == STT_ENGINE_MOSS else engine

    command = [
        resolve_executable(executable),
        "--backend",
        backend,
        "--hf-repo",
        f"{model.repository}:{model_filename}",
        "-m",
        model_filename,
        "-f",
        str(audio_path),
        "-of",
        str(output_base),
        "-osrt",
        "-ojf",
        "-pp",
        "--split-on-punct",
    ]
    _append_runtime_options(command, settings)
    vad_enabled = (
        bool(_setting(settings, "moss_vad_enabled", False))
        if engine == STT_ENGINE_MOSS
        else bool(settings.get("crispasr_vad_enabled", True))
    )
    if vad_enabled:
        command.append("--vad")
        vad_model = str(settings.get("crispasr_vad_model") or "silero").strip().lower()
        if vad_model not in {"", "auto", "silero"}:
            command.extend(("--vad-model", vad_model))
        command.extend(
            (
                "--vad-threshold",
                str(max(0.0, min(1.0, float(_setting(settings, "crispasr_vad_threshold", 0.5))))),
                "--vad-min-speech-duration-ms",
                str(max(0, int(_setting(settings, "crispasr_vad_min_speech_ms", 250)))),
                "--vad-min-silence-duration-ms",
                str(max(0, int(_setting(settings, "crispasr_vad_min_silence_ms", 800)))),
                "--vad-max-speech-duration-s",
                str(max(1.0, float(_setting(settings, "crispasr_vad_max_speech_seconds", 300.0)))),
                "--vad-speech-pad-ms",
                str(max(0, int(_setting(settings, "crispasr_vad_speech_pad_ms", 30)))),
            )
        )
    chunk_seconds = float(_setting(settings, "stt_chunk_seconds", 0))
    if engine == STT_ENGINE_MOSS and chunk_seconds <= 0:
        # Crisp's MOSS decoder currently has a hard-coded 1,024-token output
        # ceiling.  A 120 s window remained safely below it in dense meeting
        # speech while still giving native diarization a long context.  With
        # VAD off, Crisp seeks the lowest-energy 100 ms in the final five
        # seconds rather than cutting blindly at this limit.
        chunk_seconds = max(30.0, min(120.0, float(_setting(settings, "moss_max_chunk_seconds", 120.0))))
    if chunk_seconds > 0:
        command.extend(("--chunk-seconds", f"{chunk_seconds:g}"))
    chunk_overlap = float(_setting(settings, "stt_chunk_overlap_seconds", 3.0))
    if chunk_seconds > 0 and chunk_overlap >= 0:
        command.extend(("--chunk-overlap", f"{chunk_overlap:g}"))
    hotwords = str(settings.get("stt_hotwords") or "").strip()
    if hotwords:
        command.extend(("--hotwords", hotwords))
    lid_backend = str(settings.get("stt_lid_backend") or "whisper").strip().lower()
    if lid_backend not in {"", "auto", "whisper"}:
        command.extend(("--lid-backend", lid_backend))
    beam_size = max(1, int(_setting(settings, "stt_beam_size", 1)))
    if beam_size > 1:
        command.extend(("--beam-size", str(beam_size)))
    if engine == STT_ENGINE_PARAKEET:
        decoder = str(settings.get("parakeet_decoder") or "tdt").strip().lower()
        if decoder in {"ctc", "tdt", "maes"} and decoder != "tdt":
            command.extend(("--parakeet-decoder", decoder))
    if engine != STT_ENGINE_MOSS and bool(settings.get("diarization_enabled") or settings.get("diarize")):
        command.append("--diarize")

    if engine == STT_ENGINE_WHISPER:
        command.extend(("-l", language, "-dtw", "large.v3"))
        prompt = str(settings.get("whisper_prompt") or "").strip()
        if prompt:
            command.extend(("--prompt", prompt))
    return command


def _validate_word_timestamps(
    path: Path,
    *,
    require_words: bool = True,
    require_words_per_segment: bool = False,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrispASRError(f"CrispASR produced invalid JSON metadata: {path}") from error
    transcription = payload.get("transcription")
    if not isinstance(transcription, list):
        raise CrispASRError("CrispASR JSON did not contain a transcription array.")
    has_timed_words = False
    has_transcribed_text = False
    for index, segment in enumerate(transcription):
        if not isinstance(segment, dict):
            continue
        segment_has_text = bool(str(segment.get("text") or "").strip())
        has_transcribed_text = has_transcribed_text or segment_has_text
        words = segment.get("words")
        if words is not None and not isinstance(words, list):
            raise CrispASRError("CrispASR JSON contained malformed word timing data.")
        segment_has_timed_words = False
        for word in words or []:
            if not isinstance(word, dict):
                raise CrispASRError("CrispASR JSON contained a malformed word timing entry.")
            offsets = word.get("offsets")
            if not isinstance(offsets, dict) or offsets.get("from") is None or offsets.get("to") is None:
                raise CrispASRError("CrispASR JSON word timing entry had no offsets.")
            has_timed_words = True
            segment_has_timed_words = True
        if require_words_per_segment and segment_has_text and not segment_has_timed_words:
            raise CrispASRError(
                f"CrispASR segment {index + 1} contained text but no word timestamps."
            )
    if require_words and has_transcribed_text and not has_timed_words:
        raise CrispASRError("CrispASR transcribed text but did not return word timestamps.")


def build_moss_alignment_command(
    audio_path: str | os.PathLike[str],
    text_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    settings: dict[str, Any],
    *,
    executable: str = "",
) -> list[str]:
    """Build one padded-turn CTC alignment command for native MOSS output."""

    aligner = str(_setting(settings, "moss_ctc_aligner_model", "auto")).strip() or "auto"
    command = [
        resolve_executable(executable),
        "--align-only",
        "-am",
        aligner,
        "--auto-download",
        "-f",
        str(audio_path),
        "--text-file",
        str(text_path),
        "--align-granularity",
        "word",
        "--align-format",
        "json",
        "--align-output",
        str(output_path),
    ]
    _append_runtime_options(command, settings)
    return command


def _align_moss_segments(
    audio_path: str | os.PathLike[str],
    metadata_path: Path,
    settings: dict[str, Any],
    *,
    executable: str,
    run_func: Callable[..., Any],
) -> None:
    """Attach CTC words to each MOSS turn using a small acoustic margin."""

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrispASRError(f"CrispASR produced invalid MOSS JSON metadata: {metadata_path}") from error
    segments = payload.get("transcription")
    if not isinstance(segments, list):
        raise CrispASRError("CrispASR MOSS JSON did not contain a transcription array.")

    padding = max(0.0, min(2.0, float(_setting(settings, "moss_ctc_padding_seconds", 0.5))))
    try:
        source = wave.open(str(audio_path), "rb")
    except (OSError, wave.Error) as error:
        raise CrispASRError("MOSS CTC alignment requires the normalized 16 kHz PCM WAV input.") from error
    with source:
        params = source.getparams()
        if (
            params.nchannels != 1
            or params.sampwidth != 2
            or params.framerate != 16000
            or params.comptype != "NONE"
        ):
            raise CrispASRError("MOSS CTC alignment requires mono 16-bit 16 kHz PCM WAV audio.")
        audio_frames = source.readframes(params.nframes)

    frame_size = params.nchannels * params.sampwidth
    with tempfile.TemporaryDirectory(prefix="pandrator-moss-ctc-", dir=str(metadata_path.parent)) as temp_dir:
        temp_root = Path(temp_dir)
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise CrispASRError(f"MOSS segment {index + 1} was malformed.")
            text = str(segment.get("text") or "").strip()
            offsets = segment.get("offsets")
            if not text:
                continue
            if not isinstance(offsets, dict):
                raise CrispASRError(f"MOSS segment {index + 1} had text but no native timestamps.")
            try:
                native_start_ms = max(0, int(round(float(offsets["from"]))))
                native_end_ms = max(native_start_ms + 1, int(round(float(offsets["to"]))))
            except (KeyError, TypeError, ValueError) as error:
                raise CrispASRError(f"MOSS segment {index + 1} has invalid native timestamps.") from error

            start_frame = max(0, int((native_start_ms / 1000.0 - padding) * params.framerate))
            end_frame = min(params.nframes, int((native_end_ms / 1000.0 + padding) * params.framerate))
            if end_frame <= start_frame:
                raise CrispASRError(f"MOSS segment {index + 1} has no audio to align.")
            actual_start_ms = round(start_frame * 1000.0 / params.framerate)
            actual_end_ms = round(end_frame * 1000.0 / params.framerate)

            stem = f"segment-{index + 1:05d}"
            clip_path = temp_root / f"{stem}.wav"
            text_path = temp_root / f"{stem}.txt"
            aligned_path = temp_root / f"{stem}.json"
            with wave.open(str(clip_path), "wb") as clip:
                clip.setparams(params)
                clip.writeframes(audio_frames[start_frame * frame_size : end_frame * frame_size])
            text_path.write_text(text, encoding="utf-8")
            command = build_moss_alignment_command(
                clip_path,
                text_path,
                aligned_path,
                settings,
                executable=executable,
            )
            try:
                run_func(command, check=True, capture_output=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as error:
                stderr = getattr(error, "stderr", b"")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                raise CrispASRError(
                    f"CTC alignment failed for MOSS segment {index + 1}: {str(stderr or error).strip()}"
                ) from error
            try:
                aligned = json.loads(aligned_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CrispASRError(f"CTC alignment returned no valid JSON for MOSS segment {index + 1}.") from error
            if not isinstance(aligned, list):
                raise CrispASRError(f"CTC alignment returned malformed words for MOSS segment {index + 1}.")

            speaker = str(segment.get("speaker") or "").strip()
            segment_id = str(segment.get("id") or f"moss-{index + 1}")
            words: list[dict[str, Any]] = []
            for word_index, raw_word in enumerate(aligned):
                if not isinstance(raw_word, dict):
                    raise CrispASRError(
                        f"CTC alignment returned a malformed word for MOSS segment {index + 1}."
                    )
                word_text = str(raw_word.get("word") or raw_word.get("text") or "").strip()
                try:
                    word_start = actual_start_ms + round(float(raw_word["start"]) * 1000.0)
                    word_end = actual_start_ms + round(float(raw_word["end"]) * 1000.0)
                except (KeyError, TypeError, ValueError) as error:
                    raise CrispASRError(
                        f"CTC word {word_index + 1} for MOSS segment {index + 1} had invalid timestamps."
                    ) from error
                word_start = max(actual_start_ms, min(actual_end_ms - 1, word_start))
                word_end = max(word_start + 1, min(actual_end_ms, word_end))
                if not word_text:
                    raise CrispASRError(
                        f"CTC word {word_index + 1} for MOSS segment {index + 1} had no text."
                    )
                words.append(
                    {
                        "text": word_text,
                        "offsets": {"from": word_start, "to": word_end},
                        "speaker": speaker,
                        "moss_segment_id": segment_id,
                    }
                )
            if not words:
                raise CrispASRError(f"CTC alignment returned no timed words for MOSS segment {index + 1}.")
            segment.setdefault("id", segment_id)
            segment["words"] = words

    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def transcribe(
    audio_path: str | os.PathLike[str],
    *,
    session_dir: str | os.PathLike[str],
    output_name: str,
    settings: dict[str, Any],
    executable: str = "",
    run_func: Callable[..., Any] = subprocess.run,
) -> CrispASRTranscriptionResult:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    temporary_base = session_path / f"{output_name}_crispasr"
    engine = normalize_engine(settings.get("stt_engine") or settings.get("stt_backend"))
    command = build_command(audio_path, temporary_base, settings, executable=executable)
    try:
        completed = run_func(command, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        stderr = getattr(error, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = str(stderr or error).strip()
        raise CrispASRError(f"CrispASR transcription failed: {detail}") from error

    # CrispASR appends output extensions to ``-of``.  ``Path.with_suffix()``
    # cannot model that contract because source stems may legitimately contain
    # dots (for example, ``meeting.v2_crispasr``), which it treats as a suffix.
    srt_generated = Path(f"{temporary_base}.srt")
    json_generated = Path(f"{temporary_base}.json")
    if not srt_generated.is_file() or not json_generated.is_file():
        stderr = getattr(completed, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise CrispASRError(
            "CrispASR did not produce both SRT and full JSON output. "
            + str(stderr or "").strip()
        )
    align_moss_words = engine == STT_ENGINE_MOSS and moss_ctc_alignment_enabled(settings)
    if align_moss_words:
        _align_moss_segments(
            audio_path,
            json_generated,
            settings,
            executable=executable,
            run_func=run_func,
        )
    _validate_word_timestamps(
        json_generated,
        require_words=engine != STT_ENGINE_MOSS or align_moss_words,
        require_words_per_segment=align_moss_words,
    )

    srt_path = session_path / f"{output_name}.srt"
    words_path = session_path / f"{output_name}_words.json"
    srt_generated.replace(srt_path)
    json_generated.replace(words_path)
    return CrispASRTranscriptionResult(
        srt_path=str(srt_path),
        word_timestamps_path=str(words_path),
        engine=engine,
        compute_backend=normalize_compute_backend(settings.get("stt_compute_backend")),
    )
