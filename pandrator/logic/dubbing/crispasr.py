"""CrispASR command construction, execution, and word-timing metadata."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .languages import normalize_language_code

CRISPASR_VERSION = "0.8.9"
CRISPASR_EXECUTABLE_ENV = "CRISPASR_EXECUTABLE"
CRISPASR_CACHE_DIR_ENV = "CRISPASR_CACHE_DIR"

STT_ENGINE_WHISPER = "whisper"
STT_ENGINE_PARAKEET = "parakeet"

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
    normalized = str(value or "f16").strip().lower().replace("-", "_")
    normalized = MODEL_QUANTIZATION_ALIASES.get(normalized, normalized)
    model = MODELS[normalized_engine]
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
        "parakeet",
        "parakeet_onnx",
        "onnx_parakeet",
        "parakeet_tdt_0.6b_v3",
        "nemo_parakeet_tdt_0.6b_v3",
    }:
        return STT_ENGINE_PARAKEET
    return STT_ENGINE_WHISPER


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
    def setting(name: str, default: Any) -> Any:
        value = settings.get(name)
        return default if value is None or value == "" else value

    engine = normalize_engine(settings.get("stt_engine") or settings.get("stt_backend"))
    model = MODELS[engine]
    quantization = normalize_model_quantization(
        settings.get("stt_model_quantization")
        or settings.get("crispasr_model_quantization")
        or settings.get("parakeet_quantization"),
        engine,
    )
    model_filename = model.filename_for(quantization)
    compute_backend = normalize_compute_backend(settings.get("stt_compute_backend"))
    language = normalize_language_code(
        str(settings.get("stt_language") or settings.get("whisper_language") or "auto"),
        default="auto",
    )
    cache_dir = str(
        settings.get("crispasr_cache_dir")
        or os.environ.get(CRISPASR_CACHE_DIR_ENV)
        or ""
    ).strip()

    command = [
        resolve_executable(executable),
        "--backend",
        engine,
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
    if cache_dir:
        command.extend(("--cache-dir", cache_dir))
    threads = int(setting("stt_threads", 0))
    if threads > 0:
        command.extend(("--threads", str(threads)))
    if compute_backend != "auto":
        command.extend(("--gpu-backend", compute_backend))
    device = settings.get("stt_compute_device")
    if device not in (None, "") and compute_backend not in {"auto", "cpu"}:
        command.extend(("--device", str(max(0, int(device)))))
    if bool(settings.get("crispasr_vad_enabled", True)):
        command.append("--vad")
        vad_model = str(settings.get("crispasr_vad_model") or "silero").strip().lower()
        if vad_model not in {"", "auto", "silero"}:
            command.extend(("--vad-model", vad_model))
        command.extend(
            (
                "--vad-threshold",
                str(max(0.0, min(1.0, float(setting("crispasr_vad_threshold", 0.5))))),
                "--vad-min-speech-duration-ms",
                str(max(0, int(setting("crispasr_vad_min_speech_ms", 250)))),
                "--vad-min-silence-duration-ms",
                str(max(0, int(setting("crispasr_vad_min_silence_ms", 800)))),
                "--vad-max-speech-duration-s",
                str(max(1.0, float(setting("crispasr_vad_max_speech_seconds", 300.0)))),
                "--vad-speech-pad-ms",
                str(max(0, int(setting("crispasr_vad_speech_pad_ms", 30)))),
            )
        )
    chunk_seconds = float(setting("stt_chunk_seconds", 0))
    if chunk_seconds > 0:
        command.extend(("--chunk-seconds", f"{chunk_seconds:g}"))
    chunk_overlap = float(setting("stt_chunk_overlap_seconds", 3.0))
    if chunk_seconds > 0 and chunk_overlap >= 0:
        command.extend(("--chunk-overlap", f"{chunk_overlap:g}"))
    hotwords = str(settings.get("stt_hotwords") or "").strip()
    if hotwords:
        command.extend(("--hotwords", hotwords))
    lid_backend = str(settings.get("stt_lid_backend") or "whisper").strip().lower()
    if lid_backend not in {"", "auto", "whisper"}:
        command.extend(("--lid-backend", lid_backend))
    beam_size = max(1, int(setting("stt_beam_size", 1)))
    if beam_size > 1:
        command.extend(("--beam-size", str(beam_size)))
    if engine == STT_ENGINE_PARAKEET:
        decoder = str(settings.get("parakeet_decoder") or "tdt").strip().lower()
        if decoder in {"ctc", "tdt", "maes"} and decoder != "tdt":
            command.extend(("--parakeet-decoder", decoder))
    if bool(settings.get("diarization_enabled") or settings.get("diarize")):
        command.append("--diarize")

    if engine == STT_ENGINE_WHISPER:
        command.extend(("-l", language, "-dtw", "large.v3"))
        prompt = str(settings.get("whisper_prompt") or "").strip()
        if prompt:
            command.extend(("--prompt", prompt))
    return command


def _validate_word_timestamps(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrispASRError(f"CrispASR produced invalid JSON metadata: {path}") from error
    transcription = payload.get("transcription")
    if not isinstance(transcription, list):
        raise CrispASRError("CrispASR JSON did not contain a transcription array.")
    has_timed_words = False
    has_transcribed_text = False
    for segment in transcription:
        if not isinstance(segment, dict):
            continue
        has_transcribed_text = has_transcribed_text or bool(str(segment.get("text") or "").strip())
        words = segment.get("words")
        if words is not None and not isinstance(words, list):
            raise CrispASRError("CrispASR JSON contained malformed word timing data.")
        for word in words or []:
            if not isinstance(word, dict):
                raise CrispASRError("CrispASR JSON contained a malformed word timing entry.")
            offsets = word.get("offsets")
            if not isinstance(offsets, dict) or offsets.get("from") is None or offsets.get("to") is None:
                raise CrispASRError("CrispASR JSON word timing entry had no offsets.")
            has_timed_words = True
    if has_transcribed_text and not has_timed_words:
        raise CrispASRError("CrispASR transcribed text but did not return word timestamps.")


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
    _validate_word_timestamps(json_generated)

    srt_path = session_path / f"{output_name}.srt"
    words_path = session_path / f"{output_name}_words.json"
    srt_generated.replace(srt_path)
    json_generated.replace(words_path)
    engine = normalize_engine(settings.get("stt_engine") or settings.get("stt_backend"))
    return CrispASRTranscriptionResult(
        srt_path=str(srt_path),
        word_timestamps_path=str(words_path),
        engine=engine,
        compute_backend=normalize_compute_backend(settings.get("stt_compute_backend")),
    )
