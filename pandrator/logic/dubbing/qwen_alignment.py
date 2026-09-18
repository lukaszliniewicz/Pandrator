"""Qwen3 forced alignment via audio.cpp, with bounded, reusable CLI sessions.

Qwen is not a CTC model. The existing *_ctc_model setting names remain wire
compatibility aliases; diagnostics identify the actual model and algorithm.
No transcript or audio is uploaded. Only the pinned public GGUF is downloaded.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import unicodedata
import wave
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

import regex

from ..cancellable_process import ProcessCancelled, run_cancellable
from .languages import normalize_language_code
from .text_units import infer_cjk_language, subtitle_units

logger = logging.getLogger(__name__)
MODEL_ID = "qwen3-forced-aligner"
MODEL_FILENAME = "qwen3-forced-aligner-0.6b-q8_0.gguf"
MODEL_REVISION = "dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c"
MODEL_SHA256 = "75209490b11cec2b0db749ca5f4ff92266f58efd30f7fd04d9eb2a3ac9cc929f"
MODEL_SIZE = 1_129_966_496
MODEL_URL = (
    "https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/"
    f"{MODEL_REVISION}/Qwen3-ForcedAligner-0.6B-GGUF/{MODEL_FILENAME}"
)
# Official Qwen model card: ASR's larger language list does not apply here.
LANGUAGES = {
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "es": "Spanish",
}
ALIASES = frozenset(
    {MODEL_ID, "qwen3_forced_aligner", "qwen3-forced-aligner-0.6b", MODEL_FILENAME}
)
MAX_BATCH_REQUESTS = 8
MAX_AUDIO_SECONDS = 300
_CACHE_LOCK = threading.Lock()
_VERIFIED: set[tuple[str, int, int, int]] = set()


class QwenAlignmentError(RuntimeError):
    """A Qwen result or prerequisite cannot safely be used as timing evidence."""


def check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Qwen forced alignment was canceled.")


def source_language(settings: dict, text: str = "") -> str:
    for key in (
        "original_language",
        "source_language",
        "stt_language",
        "whisper_language",
    ):
        value = str(settings.get(key) or "").strip()
        if value.lower() == "cantonese":
            return "yue"
        code = normalize_language_code(value, default="")
        if code not in {"", "auto", "und", "unknown"}:
            return code.split("-")[0]
    return infer_cjk_language(text)


def uses_qwen(
    settings: dict, text: str = "", *, model_key: str = "caption_alignment_ctc_model"
) -> bool:
    model = str(settings.get(model_key) or "auto").strip().lower()
    return model in ALIASES or (
        model == "auto" and source_language(settings, text) in {"ja", "zh", "ko", "yue"}
    )


def resolve_executable(settings: dict) -> Path:
    explicit = str(
        settings.get("qwen_aligner_executable") or os.environ.get("AUDIO_CPP_CLI") or ""
    ).strip()
    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        workspace = os.environ.get("PANDRATOR_WORKSPACE", "")
        candidate = None
        if workspace:
            pointer = Path(workspace) / "Pandrator/services/audio_cpp/current.json"
            try:
                slot = Path(json.loads(pointer.read_text(encoding="utf-8"))["path"])
                candidate = slot / (
                    "audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli"
                )
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if candidate is None:
            found = shutil.which("audiocpp_cli")
            candidate = Path(found) if found else None
    if candidate is None or not candidate.is_file():
        raise QwenAlignmentError(
            "Qwen alignment requires audio.cpp's audiocpp_cli. Install/update Local speech models (audio.cpp), or set AUDIO_CPP_CLI to that executable."
        )
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise QwenAlignmentError(
            "The installed audiocpp_cli is not executable. Repair/update the audio.cpp runtime or correct its executable permission."
        )
    return candidate.resolve()


def language_problem(settings: dict, text: str = "") -> str | None:
    code = source_language(settings, text)
    if code not in LANGUAGES:
        return f"unsupported_qwen_alignment_language:{code or 'unknown'}: Set the source/audio language to one of {', '.join(LANGUAGES)}. The translation target is not used for alignment."
    return None


def cache_path(settings: dict) -> Path:
    configured = settings.get("qwen_aligner_cache_dir")
    if configured:
        root = Path(str(configured)).expanduser()
    elif os.environ.get("PANDRATOR_WORKSPACE"):
        root = (
            Path(os.environ["PANDRATOR_WORKSPACE"]) / "Pandrator/cache/aligners/qwen3"
        )
    else:
        root = Path.home() / ".cache/pandrator/aligners/qwen3"
    return root / MODEL_FILENAME


def _verified(path: Path, event: threading.Event | None) -> bool:
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stat.st_size != MODEL_SIZE:
            return False
        if key in _VERIFIED:
            return True
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                check_cancelled(event)
                digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            return False
        _VERIFIED.add(key)
        return True
    except OSError:
        return False


@contextmanager
def _download_lock(destination: Path, event: threading.Event | None):
    # Thread and process locks: concurrent workers must not download 1 GB each.
    while not _CACHE_LOCK.acquire(timeout=0.1):
        check_cancelled(event)
    try:
        with destination.with_suffix(".lock").open("a+b") as lock:
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            while True:
                check_cancelled(event)
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    (event or threading.Event()).wait(0.1)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        _CACHE_LOCK.release()


def ensure_model(
    settings: dict, event: threading.Event | None = None, *, opener: Callable = urlopen
) -> Path:
    check_cancelled(event)
    custom = str(settings.get("qwen_aligner_model_path") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        if not path.is_file() or path.stat().st_size < 4:
            raise QwenAlignmentError(
                "Custom Qwen aligner GGUF does not exist or is empty."
            )
        with path.open("rb") as source:
            if source.read(4) != b"GGUF":
                raise QwenAlignmentError(
                    "Custom Qwen aligner must be a compatible GGUF, not an ASR/TTS model."
                )
        return path.resolve()
    destination = cache_path(settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _download_lock(destination, event):
        if _verified(destination, event):
            return destination.resolve()
        if shutil.disk_usage(destination.parent).free < MODEL_SIZE + 64 * 1024 * 1024:
            raise QwenAlignmentError(
                "Not enough free space for the 1.13 GB Qwen alignment model."
            )
        temporary = None
        try:
            logger.info(
                "Downloading the pinned Qwen3 forced aligner (%s bytes).", MODEL_SIZE
            )
            request = Request(
                MODEL_URL, headers={"User-Agent": "Pandrator-Qwen-Alignment"}
            )
            with (
                opener(request, timeout=30) as response,
                tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=".qwen-download-",
                    delete=False,
                ) as output,
            ):
                temporary = Path(output.name)
                digest = hashlib.sha256()
                size = 0
                while True:
                    check_cancelled(event)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MODEL_SIZE:
                        raise QwenAlignmentError(
                            "Qwen model download exceeded its pinned size."
                        )
                    digest.update(chunk)
                    output.write(chunk)
                if size != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
                    raise QwenAlignmentError(
                        "Qwen model download failed its pinned size/SHA-256 verification."
                    )
                output.flush()
                os.fsync(output.fileno())
            check_cancelled(event)
            os.replace(temporary, destination)
            temporary = None
            stat = destination.stat()
            _VERIFIED.add(
                (
                    str(destination.resolve()),
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
        except OSError as error:
            raise QwenAlignmentError(
                f"Could not cache the Qwen forced aligner: {error}"
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return destination.resolve()


def alignment_key(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum()
    )


def restore_surfaces(text: str, words: Sequence[dict]) -> list[dict]:
    """Map native units to exact original grapheme spans, never interpolate time."""
    positions: list[tuple[int, int]] = []
    key = ""
    for match in regex.finditer(r"\X", text):
        fragment = alignment_key(match.group())
        key += fragment
        positions.extend([(match.start(), match.end())] * len(fragment))
    if not key or not words:
        raise QwenAlignmentError("Alignment has no lexical content or timed units.")
    cursor = 0
    cuts = [0]
    retained = []
    for word in words:
        fragment = alignment_key(str(word.get("word") or ""))
        if not fragment or not key.startswith(fragment, cursor):
            raise QwenAlignmentError(
                "Qwen output does not match the exact transcript; original timings are retained."
            )
        cursor += len(fragment)
        if cursor < len(key) and positions[cursor - 1] == positions[cursor]:
            raise QwenAlignmentError("Qwen output would divide a source grapheme.")
        cuts.append(positions[cursor][0] if cursor < len(key) else len(text))
        retained.append(word)
    if cursor != len(key):
        raise QwenAlignmentError("Qwen alignment omitted transcript content.")
    return [
        {**word, "word": text[start:end].strip()}
        for word, start, end in zip(retained, cuts, cuts[1:])
    ]


def alignment_text(text: str, language: str) -> str:
    """Use alignment-only CJK units; never rewrite the authoritative transcript.

    audio.cpp 0.8.1 segments Han but not kana runs. Explicit grapheme/kinsoku
    units prevent a kana-only sentence becoming one several-second timestamp.
    These are acoustic alignment units, not linguistic words or speech cuts.
    """
    normalized = unicodedata.normalize("NFC", text)
    if language in {"ja", "zh", "yue"}:
        return " ".join(
            unit.strip() for unit in subtitle_units(normalized) if alignment_key(unit)
        )
    return normalized


def validate_audio(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 16000
                or wav.getcomptype() != "NONE"
            ):
                raise QwenAlignmentError(
                    "Qwen alignment requires mono 16-bit 16 kHz PCM WAV audio."
                )
            duration = wav.getnframes() / wav.getframerate()
    except (OSError, wave.Error) as error:
        raise QwenAlignmentError("Invalid Qwen alignment WAV.") from error
    if not 0 < duration <= MAX_AUDIO_SECONDS:
        raise QwenAlignmentError(
            "Qwen alignment requires an exact transcript/audio pair of at most 300 seconds; split on existing timed boundaries."
        )
    return duration


def parse_words(payload: object, duration: float) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("word_timestamps", payload.get("words"))
    if not isinstance(payload, list) or not payload:
        raise QwenAlignmentError("Qwen returned no timestamp array.")
    words = []
    previous_start = previous_end = -1.0
    for item in payload:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("word"), str)
            or not alignment_key(item["word"])
        ):
            raise QwenAlignmentError("Qwen returned an invalid timed unit.")
        try:
            # audio.cpp's native output uses samples in its 16 kHz input.
            start = float(item["start_sample"]) / 16000
            end = float(item["end_sample"]) / 16000
        except (KeyError, TypeError, ValueError) as error:
            raise QwenAlignmentError(
                "Qwen timestamp sample offsets are invalid."
            ) from error
        if (
            not all(math.isfinite(v) for v in (start, end))
            or not 0 <= start < end <= duration
            or start <= previous_start
            or end <= previous_end
        ):
            raise QwenAlignmentError(
                "Qwen timestamps are nonmonotonic or outside the audio; no repaired timing was invented."
            )
        words.append({"word": item["word"], "start": start, "end": end})
        previous_start, previous_end = start, end
    return words


def run_batch(
    requests: Sequence[tuple[Path, Path, Path]],
    settings: dict,
    *,
    cancel_event: threading.Event | None = None,
    run_func: Callable = subprocess.run,
) -> list[list[dict] | QwenAlignmentError]:
    """Load one model for up to eight bounded requests; JSON avoids shell/argv text."""
    if not 1 <= len(requests) <= MAX_BATCH_REQUESTS:
        raise QwenAlignmentError(
            "Qwen alignment batches must contain one through eight requests."
        )
    check_cancelled(cancel_event)
    inputs = []
    durations = []
    for index, (audio, text_path, _output) in enumerate(requests):
        text = Path(text_path).read_text(encoding="utf-8-sig")
        problem = language_problem(settings, text)
        if problem:
            raise QwenAlignmentError(problem)
        code = source_language(settings, text)
        if not alignment_key(text) or len(text) > 16000:
            raise QwenAlignmentError(
                "Qwen input must contain lexical text within a bounded transcript (16000 characters maximum)."
            )
        durations.append(validate_audio(Path(audio)))
        inputs.append(
            {
                "id": f"request_{index}",
                "audio": str(Path(audio).resolve()),
                "text": alignment_text(text, code),
                "language": LANGUAGES[code],
                "audio_chunk_mode": "none",
                "return_timestamps": True,
            }
        )
    executable = resolve_executable(settings)
    model = ensure_model(settings, cancel_event)
    backend = str(
        settings.get("qwen_aligner_backend")
        or settings.get("stt_compute_backend")
        or "auto"
    ).lower()
    backend = "best" if backend == "auto" else backend
    if backend not in {"cpu", "best", "cuda", "vulkan", "hip", "metal"}:
        raise QwenAlignmentError(f"Unsupported audio.cpp alignment backend: {backend}")
    with tempfile.TemporaryDirectory(prefix="pandrator-qwen-align-") as temporary:
        root = Path(temporary)
        sequence = root / "requests.json"
        sequence.write_text(
            json.dumps({"requests": inputs}, ensure_ascii=False), encoding="utf-8"
        )
        command = [
            str(executable),
            "--task",
            "align",
            "--family",
            "qwen3_forced_aligner",
            "--model",
            str(model),
            "--backend",
            backend,
            "--device",
            str(max(0, int(settings.get("stt_compute_device") or 0))),
            "--threads",
            str(max(1, int(settings.get("stt_threads") or 4))),
            "--request-sequence",
            str(sequence),
            "--words-out",
            str(root / "words.json"),
        ]
        try:
            check_cancelled(cancel_event)
            if run_func is subprocess.run:
                run_cancellable(
                    command,
                    cancel_event=cancel_event,
                    check=True,
                    capture_output=True,
                    cwd=str(executable.parent),
                )
            else:
                run_func(
                    command, check=True, capture_output=True, cwd=str(executable.parent)
                )
        except ProcessCancelled:
            raise
        except (OSError, subprocess.CalledProcessError) as error:
            # Native logs can contain source text; expose only the failure kind.
            raise QwenAlignmentError(
                f"audio.cpp Qwen alignment failed ({type(error).__name__}). Original timings remain available."
            ) from error
        check_cancelled(cancel_event)
        results = []
        for index, (audio, text, output) in enumerate(requests):
            generated = root / f"words_request_{index}.json"
            try:
                payload = json.loads(generated.read_text(encoding="utf-8"))
                result = parse_words(payload, durations[index])
                # A rejected request must not discard valid siblings in a batch.
                restore_surfaces(Path(text).read_text(encoding="utf-8-sig"), result)
            except (OSError, ValueError, QwenAlignmentError) as error:
                result = QwenAlignmentError(str(error))
            results.append(result)
        for (_audio, _text, output), result in zip(requests, results):
            if not isinstance(result, Exception):
                Path(output).write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
        return results


def run_alignment(
    audio_path,
    text_path,
    output_path,
    settings,
    *,
    cancel_event=None,
    run_func=subprocess.run,
):
    result = run_batch(
        [(Path(audio_path), Path(text_path), Path(output_path))],
        settings,
        cancel_event=cancel_event,
        run_func=run_func,
    )[0]
    if isinstance(result, Exception):
        raise result
    return result


def align_moss_turns(
    audio_path,
    payload: dict,
    settings: dict,
    *,
    cancel_event=None,
    run_func=subprocess.run,
) -> None:
    """Align native MOSS turns without losing the transcript on a model failure."""
    segments = payload.get("transcription", [])
    text = " ".join(
        str(item.get("text") or "") for item in segments if isinstance(item, dict)
    )
    options = {
        **settings,
        "original_language": source_language(settings, text),
        "caption_alignment_ctc_model": MODEL_ID,
    }
    diagnostics = {
        "engine": "audio.cpp",
        "model": MODEL_ID,
        "accepted_turns": 0,
        "failed_turns": 0,
    }
    padding = max(0.0, min(2.0, float(settings.get("moss_ctc_padding_seconds", 0.5))))
    with (
        wave.open(str(audio_path), "rb") as source,
        tempfile.TemporaryDirectory(prefix="pandrator-qwen-moss-") as temporary,
    ):
        params = source.getparams()
        if (params.nchannels, params.sampwidth, params.framerate, params.comptype) != (
            1,
            2,
            16000,
            "NONE",
        ):
            raise QwenAlignmentError(
                "MOSS Qwen alignment requires mono 16-bit 16 kHz PCM WAV."
            )
        root = Path(temporary)
        for group_start in range(0, len(segments), MAX_BATCH_REQUESTS):
            requests, owners = [], []
            for index, segment in enumerate(
                segments[group_start : group_start + MAX_BATCH_REQUESTS],
                start=group_start,
            ):
                check_cancelled(cancel_event)
                if (
                    not isinstance(segment, dict)
                    or not str(segment.get("text") or "").strip()
                ):
                    continue
                try:
                    offsets = segment["offsets"]
                    start, end = float(offsets["from"]), float(offsets["to"])
                    if (
                        not math.isfinite(start)
                        or not math.isfinite(end)
                        or not 0 <= start < end
                    ):
                        raise ValueError("invalid native timing")
                    first = max(0, round((start / 1000 - padding) * 16000))
                    last = min(params.nframes, round((end / 1000 + padding) * 16000))
                    if not 0 < last - first <= MAX_AUDIO_SECONDS * 16000:
                        raise ValueError(
                            "native turn is outside audio or exceeds 300 seconds"
                        )
                    clip, transcript, output = (
                        root / f"turn-{index}{suffix}"
                        for suffix in (".wav", ".txt", ".json")
                    )
                    source.setpos(first)
                    with wave.open(str(clip), "wb") as wav:
                        wav.setparams(params)
                        wav.writeframes(source.readframes(last - first))
                    transcript.write_text(str(segment["text"]), encoding="utf-8")
                    requests.append((clip, transcript, output))
                    owners.append((segment, first / 16, index))
                except (KeyError, TypeError, ValueError, OSError) as error:
                    segment.pop("words", None)
                    segment["pandrator_forced_alignment"] = {
                        "status": "rejected",
                        "reason": str(error),
                        "timing_source": "native_turn",
                    }
                    diagnostics["failed_turns"] += 1
            if not requests:
                continue
            try:
                results = run_batch(
                    requests, options, cancel_event=cancel_event, run_func=run_func
                )
            except ProcessCancelled:
                raise
            except (QwenAlignmentError, OSError, ValueError) as error:
                results = [QwenAlignmentError(str(error))] * len(requests)
            for (segment, offset_ms, index), result in zip(owners, results):
                if isinstance(result, Exception):
                    segment.pop("words", None)
                    segment["pandrator_forced_alignment"] = {
                        "status": "rejected",
                        "reason": str(result),
                        "timing_source": "native_turn",
                    }
                    diagnostics["failed_turns"] += 1
                    continue
                segment_id = str(segment.get("id") or f"moss-{index + 1}")
                restored = restore_surfaces(str(segment["text"]), result)
                segment["id"] = segment_id
                segment["words"] = [
                    {
                        "text": item["word"],
                        "offsets": {
                            "from": round(offset_ms + item["start"] * 1000),
                            "to": round(offset_ms + item["end"] * 1000),
                        },
                        "speaker": str(segment.get("speaker") or ""),
                        "moss_segment_id": segment_id,
                        "timing_source": "qwen3_alignment",
                    }
                    for item in restored
                ]
                segment["pandrator_forced_alignment"] = {
                    "status": "aligned",
                    "engine": "audio.cpp",
                    "model": MODEL_ID,
                }
                diagnostics["accepted_turns"] += 1
            for request in requests:
                for path in request:
                    path.unlink(missing_ok=True)
    payload["pandrator_forced_alignment"] = diagnostics


def capabilities(settings: dict | None = None) -> dict:
    """Cheap, read-only discovery; full model verification happens before use."""
    values = dict(settings or {})
    error = ""
    try:
        resolve_executable(values)
        available = True
    except QwenAlignmentError as problem:
        available, error = False, str(problem)
    path = cache_path(values)
    try:
        cached = path.is_file() and path.stat().st_size == MODEL_SIZE
    except OSError:
        cached = False
    return {
        "id": MODEL_ID,
        "engine": "audio.cpp",
        "algorithm": "non_autoregressive",
        "available": available,
        "reason": error,
        "supported_languages": list(LANGUAGES),
        "model_cached": cached,
        "download_on_demand": available and not cached,
        "model_download_bytes": MODEL_SIZE,
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "max_batch_requests": MAX_BATCH_REQUESTS,
    }
