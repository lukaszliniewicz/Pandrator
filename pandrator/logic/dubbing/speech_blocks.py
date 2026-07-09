"""Speech-block generation for Pandrator-native dubbing."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .languages import normalize_language_code
from .models import SpeechBlock, SubtitleSegment
from .srt_utils import parse_srt

logger = logging.getLogger(__name__)

try:
    from sentence_splitter import SentenceSplitter
except Exception:  # pragma: no cover - optional runtime dependency
    SentenceSplitter = None  # type: ignore[assignment]


SENTENCE_SPLITTER_LANGUAGES = {
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "pl",
    "tr",
    "ru",
    "nl",
    "cs",
    "hu",
    "ca",
    "da",
    "fi",
    "el",
    "is",
    "lv",
    "lt",
    "no",
    "ro",
    "sk",
    "sl",
    "sv",
}

CONJUNCTIONS = {
    "en": ["and", "but", "or", "because", "although", "so", "while", "if", "then"],
    "es": ["y", "pero", "o", "porque", "aunque", "mientras", "si", "entonces"],
    "fr": ["et", "mais", "ou", "parce que", "bien que", "pendant que", "si"],
    "de": ["und", "aber", "oder", "weil", "obwohl", "wenn", "dann", "dass"],
    "it": ["e", "ma", "o", "perche", "sebbene", "mentre", "se"],
    "pt": ["e", "mas", "ou", "porque", "embora", "enquanto", "se"],
    "pl": ["i", "ale", "lub", "poniewaz", "chociaz", "jesli", "wtedy"],
    "nl": ["en", "maar", "of", "omdat", "hoewel", "terwijl", "als"],
}

_FALLBACK_SENTENCE_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")


@dataclass
class _SpeechPart:
    text: str
    subtitles: list[int]
    start_ms: int
    end_ms: int


def _check_split_validity(text: str, split_index: int, max_chars: int, min_chars: int) -> bool:
    if split_index <= 0 or split_index >= len(text):
        return False
    first = text[:split_index].strip()
    second = text[split_index:].strip()
    return bool(first and second and min_chars <= len(first) <= max_chars and len(second) >= min_chars)


def _split_further(text: str, language_code: str, max_chars: int, min_chars: int) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    midpoint = len(text) // 2
    for punctuation_set in (".!?", ",;:"):
        best_index = -1
        best_distance = float("inf")
        for idx in range(len(text) - 1, min_chars - 1, -1):
            if text[idx] not in punctuation_set:
                continue
            split_index = idx + 1
            if not _check_split_validity(text, split_index, max_chars, min_chars):
                continue
            distance = abs(split_index - midpoint)
            if distance < best_distance or (distance == best_distance and split_index > best_index):
                best_distance = distance
                best_index = split_index
        if best_index >= 0:
            return [
                part
                for segment in (
                    text[:best_index].strip(),
                    *_split_further(text[best_index:].strip(), language_code, max_chars, min_chars),
                )
                for part in ([segment] if segment else [])
            ]

    best_index = -1
    best_distance = float("inf")
    for conjunction in CONJUNCTIONS.get(language_code, []):
        for match in re.finditer(r"\b" + re.escape(conjunction) + r"\b", text, re.IGNORECASE):
            split_index = match.start()
            if not _check_split_validity(text, split_index, max_chars, min_chars):
                continue
            distance = abs(split_index - midpoint)
            if distance < best_distance or (distance == best_distance and split_index > best_index):
                best_distance = distance
                best_index = split_index
    if best_index >= 0:
        return [
            part
            for segment in (
                text[:best_index].strip(),
                *_split_further(text[best_index:].strip(), language_code, max_chars, min_chars),
            )
            for part in ([segment] if segment else [])
        ]

    best_index = -1
    best_distance = float("inf")
    for idx in range(min(len(text) - 1, max_chars), min_chars - 1, -1):
        if not text[idx].isspace():
            continue
        first = text[:idx].strip()
        second = text[idx + 1:].strip()
        if not (min_chars <= len(first) <= max_chars and len(second) >= min_chars):
            continue
        distance = abs(len(first) - midpoint)
        if distance < best_distance or (distance == best_distance and idx > best_index):
            best_distance = distance
            best_index = idx
    if best_index >= 0:
        return [
            part
            for segment in (
                text[:best_index].strip(),
                *_split_further(text[best_index + 1:].strip(), language_code, max_chars, min_chars),
            )
            for part in ([segment] if segment else [])
        ]

    hard_cut = max_chars
    cut_text = text[:hard_cut]
    last_space = cut_text.rfind(" ")
    if last_space >= min_chars:
        hard_cut = last_space
    return [
        part
        for segment in (
            text[:hard_cut].strip(),
            *_split_further(text[hard_cut:].strip(), language_code, max_chars, min_chars),
        )
        for part in ([segment] if segment else [])
    ]


def _split_subtitle_text(text: str, language_code: str, min_chars: int, max_chars: int) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    if SentenceSplitter is not None and language_code in SENTENCE_SPLITTER_LANGUAGES:
        try:
            splitter = SentenceSplitter(language=language_code)
            sentences = [sentence.strip() for sentence in splitter.split(text=text) if sentence.strip()]
        except Exception as error:
            logger.warning("Could not initialize SentenceSplitter for %s: %s", language_code, error)
            sentences = []
    else:
        sentences = []

    if not sentences:
        sentences = [part.strip() for part in _FALLBACK_SENTENCE_RE.split(text) if part.strip()]
    if not sentences:
        sentences = [text]

    parts: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            parts.append(sentence)
        else:
            parts.extend(_split_further(sentence, language_code, max_chars, min_chars))
    return [part for part in parts if part]


def _subtitle_to_parts(
    subtitle: SubtitleSegment,
    language_code: str,
    min_chars: int,
    max_chars: int,
) -> list[_SpeechPart]:
    parts = _split_subtitle_text(subtitle.text, language_code, min_chars, max_chars)
    return [
        _SpeechPart(
            text=part,
            subtitles=[subtitle.index],
            start_ms=subtitle.start_ms,
            end_ms=subtitle.end_ms,
        )
        for part in parts
    ]


def _should_merge_parts(
    previous: _SpeechPart,
    current: _SpeechPart,
    min_chars: int,
    max_chars: int,
    merge_threshold: int,
) -> bool:
    combined_length = len(previous.text) + len(current.text) + 1
    if combined_length > max_chars:
        return False

    gap_ms = max(0, current.start_ms - previous.end_ms)
    if gap_ms > merge_threshold:
        return False

    return len(previous.text) < min_chars or len(current.text) < min_chars


def _parts_to_blocks(parts: list[_SpeechPart]) -> list[SpeechBlock]:
    blocks: list[SpeechBlock] = []
    for index, part in enumerate(parts, start=1):
        blocks.append(
            SpeechBlock(
                number=str(index).zfill(4),
                text=part.text.strip(),
                subtitles=sorted(set(part.subtitles)),
            )
        )
    return blocks


def create_speech_blocks(
    srt_content: str,
    target_language: str = "en",
    min_chars: int = 10,
    max_chars: int = 160,
    merge_threshold: int = 250,
) -> list[dict[str, object]]:
    """Create Pandrator/Subdub-compatible speech blocks from SRT content."""
    language_code = normalize_language_code(target_language)
    subtitles = parse_srt(srt_content)
    all_parts: list[_SpeechPart] = []
    for subtitle in subtitles:
        all_parts.extend(_subtitle_to_parts(subtitle, language_code, min_chars, max_chars))

    merged_parts: list[_SpeechPart] = []
    for part in all_parts:
        if not part.text:
            continue
        if merged_parts and _should_merge_parts(
            merged_parts[-1],
            part,
            min_chars=min_chars,
            max_chars=max_chars,
            merge_threshold=merge_threshold,
        ):
            previous = merged_parts[-1]
            merged_parts[-1] = _SpeechPart(
                text=f"{previous.text} {part.text}".strip(),
                subtitles=sorted(set(previous.subtitles + part.subtitles)),
                start_ms=previous.start_ms,
                end_ms=part.end_ms,
            )
        else:
            merged_parts.append(part)

    return [block.to_dict() for block in _parts_to_blocks(merged_parts)]


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def generate_speech_blocks_file(
    session_dir: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    target_language: str = "en",
    min_chars: int = 10,
    max_chars: int = 160,
    merge_threshold: int = 250,
) -> str:
    """Generate a speech-block JSON file next to a dubbing run/session."""
    session_path = Path(session_dir)
    srt_path = Path(srt_file)
    with srt_path.open("r", encoding="utf-8-sig") as handle:
        srt_content = handle.read()

    blocks = create_speech_blocks(
        srt_content,
        target_language=target_language,
        min_chars=min_chars,
        max_chars=max_chars,
        merge_threshold=merge_threshold,
    )

    output_path = session_path / f"{srt_path.stem}_speech_blocks.json"
    _write_json_atomic(output_path, blocks)
    logger.info("Generated %d speech block(s): %s", len(blocks), output_path)
    return str(output_path)
