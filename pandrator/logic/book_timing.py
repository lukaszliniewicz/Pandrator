"""Exact transcript timing and conservative original-spelling projection for books."""

from __future__ import annotations

import json
import shutil
import threading
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import regex

from .cancellable_process import run_cancellable
from .dubbing import crispasr, qwen_alignment
from .dubbing.caption_alignment import parse_ctc_words
from .dubbing.languages import normalize_language_code

IDENTITY_VERSION = 1


def _integer(value: Any, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _json_copy(value: Any) -> Any:
    def validate(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Diagnostics keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("Diagnostics must be JSON values")

    validate(value)
    try:
        return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise ValueError("Diagnostics must contain finite JSON values") from error


@dataclass(frozen=True)
class BookWord:
    text: str
    start_ms: int
    end_ms: int
    start_char: int
    end_char: int

    def __post_init__(self):
        if not isinstance(self.text, str) or not qwen_alignment.alignment_key(self.text):
            raise ValueError("A book word must contain lexical text")
        for name in ("start_ms", "end_ms", "start_char", "end_char"):
            _integer(getattr(self, name), name)
        if self.end_ms <= self.start_ms or self.end_char <= self.start_char:
            raise ValueError("Book word ranges must have positive length")

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start_char": self.start_char,
            "end_char": self.end_char,
        }

    @classmethod
    def from_dict(cls, value: dict) -> BookWord:
        if not isinstance(value, dict):
            raise ValueError("Book word must be an object")
        try:
            return cls(
                **{
                    key: value[key]
                    for key in ("text", "start_ms", "end_ms", "start_char", "end_char")
                }
            )
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid book word object") from error


@dataclass(frozen=True)
class AlignedBookText:
    text: str
    words: tuple[BookWord, ...]
    engine: str
    diagnostics: dict

    def __post_init__(self):
        if not isinstance(self.text, str) or not isinstance(self.engine, str) or not self.engine:
            raise ValueError("Aligned book text and engine must be strings")
        if not isinstance(self.words, tuple) or not self.words:
            raise ValueError("Aligned book text must have a tuple of timed words")
        if not isinstance(self.diagnostics, dict):
            raise ValueError("Diagnostics must be an object")
        _json_copy(self.diagnostics)
        duration = self.diagnostics.get("duration_ms")
        if duration is not None:
            _integer(duration, "duration_ms")
            if not duration:
                raise ValueError("Audio duration must be positive")
        boundaries = {0, *(match.end() for match in regex.finditer(r"\X", self.text))}
        previous_start = previous_end = -1
        cursor = 0
        for word in self.words:
            if not isinstance(word, BookWord):
                raise ValueError("Invalid book word")
            if (
                word.start_char < cursor
                or word.end_char > len(self.text)
                or word.start_char not in boundaries
                or word.end_char not in boundaries
                or self.text[word.start_char : word.end_char] != word.text
                or qwen_alignment.alignment_key(self.text[cursor : word.start_char])
            ):
                raise ValueError("Book word spans do not cover the exact transcript")
            if (
                word.start_ms <= previous_start
                or word.end_ms <= previous_end
                or word.start_ms < previous_end
            ):
                raise ValueError("Book word timestamps must increase monotonically without overlap")
            if duration is not None and word.end_ms > duration:
                raise ValueError("Book word timestamps exceed audio duration")
            previous_start, previous_end = word.start_ms, word.end_ms
            cursor = word.end_char
        if qwen_alignment.alignment_key(self.text[cursor:]):
            raise ValueError("Book word spans omit transcript content")

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "words": [word.as_dict() for word in self.words],
            "engine": self.engine,
            "diagnostics": _json_copy(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: dict) -> AlignedBookText:
        if not isinstance(value, dict) or not isinstance(value.get("words"), list):
            raise ValueError("Aligned book text must be an object with a word array")
        try:
            return cls(
                text=value["text"],
                words=tuple(BookWord.from_dict(word) for word in value["words"]),
                engine=value["engine"],
                diagnostics=_json_copy(value["diagnostics"]),
            )
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid aligned book text object") from error


def _options(settings: dict) -> dict:
    # Only execution settings consumed by forced alignment cross this boundary.
    options = {
        key: settings[key]
        for key in (
            "caption_alignment_ctc_model",
            "crispasr_executable",
            "crispasr_cache_dir",
            "qwen_aligner_executable",
            "qwen_aligner_model_path",
            "qwen_aligner_cache_dir",
            "qwen_aligner_backend",
            "stt_compute_backend",
            "stt_compute_device",
            "stt_threads",
        )
        if settings.get(key) not in (None, "")
    }
    for key in (
        "language",
        "stt_language",
        "original_language",
        "source_language",
        "whisper_language",
    ):
        language = str(settings.get(key) or "").strip()
        normalized = (
            "yue"
            if language.lower() == "cantonese"
            else normalize_language_code(language, default="")
        )
        if normalized not in {"", "auto", "und", "unknown"}:
            options["original_language"] = normalized
            break
    engine = str(settings.get("book_alignment_engine") or "auto").strip().lower()
    model = str(options.get("caption_alignment_ctc_model") or "auto").strip() or "auto"
    if engine == "qwen":
        model = qwen_alignment.MODEL_ID
    elif engine == "crispasr":
        if model.lower() == "auto" or model.lower() in qwen_alignment.ALIASES:
            model = "canary-ctc-aligner"
    elif engine != "auto":
        raise ValueError("book_alignment_engine must be auto, crispasr, or qwen")
    options["caption_alignment_ctc_model"] = model
    return options


def _fingerprint(path: str | Path) -> dict | None:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        resolved = shutil.which(str(path))
        if not resolved:
            return None
        candidate = Path(resolved)
    try:
        stat = candidate.stat()
        return {
            "path": str(candidate.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
        }
    except OSError:
        return None


def alignment_identity(settings: dict, text: str) -> dict:
    """Read-only cache identity; no credentials, provider configuration or downloads."""
    options = _options(settings)
    qwen = qwen_alignment.uses_qwen(options, text)
    model = options["caption_alignment_ctc_model"]
    identity = {
        "version": IDENTITY_VERSION,
        "language": qwen_alignment.source_language(options, text),
        "engine": "qwen" if qwen else "crispasr",
        "model": qwen_alignment.MODEL_ID if qwen else model,
        "runtime": {
            key: options[key]
            for key in (
                "qwen_aligner_backend",
                "stt_compute_backend",
                "stt_compute_device",
                "stt_threads",
            )
            if key in options
        },
        "normalization": {
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "pcm_s16le",
            "executable": _fingerprint(str(settings.get("ffmpeg_executable") or "ffmpeg")),
        },
    }
    if qwen:
        try:
            executable = qwen_alignment.resolve_executable(options)
        except qwen_alignment.QwenAlignmentError:
            executable = str(options.get("qwen_aligner_executable") or "audiocpp_cli")
        custom_model = options.get("qwen_aligner_model_path")
        model_path = custom_model or qwen_alignment.cache_path(options)
        identity["model_revision"] = "custom" if custom_model else qwen_alignment.MODEL_REVISION
        identity["model_sha256"] = None if custom_model else qwen_alignment.MODEL_SHA256
        identity["model_file"] = _fingerprint(model_path)
    else:
        executable = crispasr.resolve_executable(str(options.get("crispasr_executable") or ""))
        if model.lower() in {
            "auto",
            "canary-ctc-aligner",
            crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT.filename,
        }:
            identity["model"] = crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT.filename
            identity["model_revision"] = crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT.url
            model_path = crispasr._cached_artifact_path(
                options, crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT
            )
        else:
            identity["model_revision"] = "custom"
            model_path = model
        identity["model_file"] = _fingerprint(model_path) if model_path else None
        identity["adapter_version"] = crispasr.CRISPASR_VERSION
    identity["executable"] = _fingerprint(executable)
    return _json_copy(identity)


def _restore_words(text: str, words) -> tuple[BookWord, ...]:
    """Prove complete NFKC lexical coverage at whole grapheme boundaries."""
    positions = []
    key = ""
    for match in regex.finditer(r"\X", text):
        fragment = qwen_alignment.alignment_key(match.group())
        key += fragment
        positions.extend([(match.start(), match.end())] * len(fragment))
    if not key or not words:
        raise ValueError("Alignment must cover lexical transcript content")
    cursor = 0
    cuts = [0]
    for word in words:
        surface = word.text if isinstance(word, BookWord) else word["word"]
        fragment = qwen_alignment.alignment_key(surface)
        if not fragment or not key.startswith(fragment, cursor):
            raise ValueError("Alignment invented or changed transcript content")
        cursor += len(fragment)
        if cursor < len(key) and positions[cursor - 1] == positions[cursor]:
            raise ValueError("Alignment divides a transcript grapheme")
        cuts.append(positions[cursor][0] if cursor < len(key) else len(text))
    if cursor != len(key):
        raise ValueError("Alignment omitted transcript content")
    result = []
    for word, start, end in zip(words, cuts[:-1], cuts[1:], strict=True):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        start_ms = word.start_ms if isinstance(word, BookWord) else round(word["start"] * 1000)
        end_ms = word.end_ms if isinstance(word, BookWord) else round(word["end"] * 1000)
        result.append(BookWord(text[start:end], start_ms, end_ms, start, end))
    return tuple(result)


def align_book_text(
    audio_path: Path,
    text: str,
    settings: dict,
    work_dir: Path,
    cancel_event: threading.Event,
    *,
    runner=None,
) -> AlignedBookText:
    """Align an exact transcript/audio pair; normalization never alters its source."""
    qwen_alignment.check_cancelled(cancel_event)
    if not isinstance(text, str) or not qwen_alignment.alignment_key(text):
        raise ValueError("Book alignment requires lexical transcript text")
    options = _options(settings)
    problem = crispasr.ctc_language_problem(options, text)
    if problem:
        raise ValueError(problem)
    qwen = qwen_alignment.uses_qwen(options, text)
    if qwen and len(text) > 16000:
        raise ValueError("Qwen book alignment requires at most 16000 transcript characters")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    normalized = work_dir / "book-alignment.wav"
    if Path(audio_path).resolve() == normalized.resolve():
        raise ValueError("Alignment scratch audio must differ from the source")
    run_cancellable(
        [
            str(settings.get("ffmpeg_executable") or "ffmpeg"),
            "-nostdin",
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(normalized),
        ],
        cancel_event=cancel_event,
        check=True,
        capture_output=True,
    )
    qwen_alignment.check_cancelled(cancel_event)
    with wave.open(str(normalized), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (
            1,
            2,
            16000,
            "NONE",
        ):
            raise ValueError("Alignment scratch WAV is not mono PCM16 at 16 kHz")
        duration = wav.getnframes() / 16000
    if duration <= 0:
        raise ValueError("Alignment audio must have positive duration")
    if qwen and duration > qwen_alignment.MAX_AUDIO_SECONDS:
        raise ValueError("Qwen book alignment requires audio of at most 300 seconds")
    transcript = work_dir / "book-alignment.txt"
    transcript.write_text(text, encoding="utf-8")
    payload = (runner or crispasr.run_ctc_alignment)(
        normalized,
        transcript,
        work_dir / "book-alignment.json",
        options,
        executable=str(options.get("crispasr_executable") or ""),
        cancel_event=cancel_event,
    )
    qwen_alignment.check_cancelled(cancel_event)
    parsed = parse_ctc_words(payload)
    previous_end = -1.0
    for word in parsed:
        if word["end"] > duration or word["end"] <= previous_end or word["start"] < previous_end:
            raise ValueError("Alignment times are nonmonotonic or outside the audio")
        previous_end = word["end"]
    return AlignedBookText(
        text,
        _restore_words(text, parsed),
        "qwen" if qwen else "crispasr",
        {
            "duration_ms": round(duration * 1000),
            "word_count": len(parsed),
            "coverage": "complete_grapheme_lexical",
            "identity": alignment_identity(settings, text),
        },
    )


def _tokens(text: str) -> list[tuple[str, int, int]]:
    tokens = []
    pending = None
    for match in regex.finditer(r"\X", text):
        key = qwen_alignment.alignment_key(match.group())
        cjk = bool(regex.search(r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]", match.group()))
        if not key or cjk:
            if pending is not None:
                tokens.append(tuple(pending))
                pending = None
            if key:
                tokens.append((key, match.start(), match.end()))
        elif pending is None:
            pending = [key, match.start(), match.end()]
        else:
            pending[0] += key
            pending[2] = match.end()
    if pending is not None:
        tokens.append(tuple(pending))
    return tokens


def _unique_block(sequence, block) -> bool:
    return (
        sum(sequence[i : i + len(block)] == block for i in range(len(sequence) - len(block) + 1))
        == 1
    )


def map_original_text(aligned: AlignedBookText, display_text: str) -> AlignedBookText | None:
    """Map spelling only; replacement islands receive one real union interval."""
    if not isinstance(display_text, str) or not qwen_alignment.alignment_key(display_text):
        return None
    if display_text == aligned.text:
        return aligned
    diagnostics = {**aligned.diagnostics, "mapping_type": "normalization", "replacement_count": 0}
    if qwen_alignment.alignment_key(display_text) == qwen_alignment.alignment_key(aligned.text):
        try:
            return AlignedBookText(
                display_text,
                _restore_words(display_text, aligned.words),
                aligned.engine,
                diagnostics,
            )
        except ValueError:
            return None
    source = _tokens(aligned.text)
    target = _tokens(display_text)
    a, b = [token[0] for token in source], [token[0] for token in target]
    opcodes = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    if any(tag in {"insert", "delete"} for tag, *_ in opcodes):
        return None
    if not any(tag == "equal" for tag, *_ in opcodes) and not len(a) == len(b) == 1:
        return None
    for tag, i, j, k, target_end in opcodes:
        if tag == "equal" and (
            not _unique_block(a, a[i:j]) or not _unique_block(b, b[k:target_end])
        ):
            return None
    words = []
    word_index = 0
    replacements = 0
    for tag, i, j, k, target_end in opcodes:
        expected = "".join(a[i:j])
        contributors = []
        observed = ""
        while len(observed) < len(expected) and word_index < len(aligned.words):
            word = aligned.words[word_index]
            observed += qwen_alignment.alignment_key(word.text)
            contributors.append(word)
            word_index += 1
        if observed != expected or not contributors:
            return None
        start = 0 if k == 0 else target[k][1]
        end = len(display_text) if target_end == len(target) else target[target_end][1]
        section = display_text[start:end]
        try:
            if tag == "equal":
                restored = _restore_words(section, contributors)
                words.extend(
                    BookWord(
                        word.text,
                        word.start_ms,
                        word.end_ms,
                        start + word.start_char,
                        start + word.end_char,
                    )
                    for word in restored
                )
            else:
                left = len(section) - len(section.lstrip())
                right = len(section.rstrip())
                words.append(
                    BookWord(
                        section[left:right],
                        contributors[0].start_ms,
                        contributors[-1].end_ms,
                        start + left,
                        start + right,
                    )
                )
                replacements += 1
        except ValueError:
            return None
    if word_index != len(aligned.words):
        return None
    diagnostics.update(mapping_type="anchored_lexical_replacements", replacement_count=replacements)
    try:
        return AlignedBookText(display_text, tuple(words), aligned.engine, diagnostics)
    except ValueError:
        return None
