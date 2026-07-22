"""Engine-neutral timed transcript normalization.

STT runtimes disagree on container names, time units, speaker labels, and
whether words are nested below segments.  Downstream subtitle and persistence
code should consume this module's small canonical model instead of learning
each engine's wire format.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


class TranscriptFormatError(ValueError):
    """Raised when no registered transcript adapter accepts an input."""


@dataclass(frozen=True)
class TimedWord:
    text: str
    start_ms: int
    end_ms: int
    speaker: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TimedSegment:
    text: str
    start_ms: int
    end_ms: int
    speaker: str = ""
    identifier: str = ""
    words: tuple[TimedWord, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedTranscript:
    segments: tuple[TimedSegment, ...]
    source_format: str
    language: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def words(self) -> tuple[TimedWord, ...]:
        return tuple(
            sorted(
                (word for segment in self.segments for word in segment.words),
                key=lambda word: (word.start_ms, word.end_ms),
            )
        )

    @property
    def speakers(self) -> tuple[str, ...]:
        values = {
            speaker
            for segment in self.segments
            for speaker in (segment.speaker, *(word.speaker for word in segment.words))
            if speaker
        }
        return tuple(sorted(values, key=str.casefold))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "pandrator.transcript.v1",
            "source_format": self.source_format,
            "language": self.language,
            "metadata": dict(self.metadata),
            "segments": [
                {
                    "id": segment.identifier,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "speaker": segment.speaker or None,
                    "text": segment.text,
                    "metadata": dict(segment.metadata),
                    "words": [
                        {
                            "text": word.text,
                            "start_ms": word.start_ms,
                            "end_ms": word.end_ms,
                            "speaker": word.speaker or None,
                            "confidence": word.confidence,
                            "metadata": dict(word.metadata),
                        }
                        for word in segment.words
                    ],
                }
                for segment in self.segments
            ],
        }


AdapterPredicate = Callable[[Any], bool]
AdapterParser = Callable[[Any], NormalizedTranscript]


@dataclass(frozen=True)
class TranscriptAdapter:
    name: str
    accepts: AdapterPredicate
    parse: AdapterParser


_CUSTOM_ADAPTERS: list[TranscriptAdapter] = []


def register_transcript_adapter(
    name: str,
    accepts: AdapterPredicate,
    parser: AdapterParser,
) -> None:
    """Register a higher-priority adapter for a future transcript format."""

    normalized_name = str(name or "").strip().casefold()
    if not normalized_name:
        raise ValueError("Transcript adapter name must not be empty.")
    unregister_transcript_adapter(normalized_name)
    _CUSTOM_ADAPTERS.insert(0, TranscriptAdapter(normalized_name, accepts, parser))


def unregister_transcript_adapter(name: str) -> None:
    normalized_name = str(name or "").strip().casefold()
    _CUSTOM_ADAPTERS[:] = [adapter for adapter in _CUSTOM_ADAPTERS if adapter.name != normalized_name]


def _speaker(value: Any) -> str:
    speaker = "" if value is None else str(value).strip().rstrip(":").strip()
    if len(speaker) >= 2 and (speaker[0], speaker[-1]) in {("(", ")"), ("[", "]")}:
        speaker = speaker[1:-1].strip()
    return speaker


def _speaker_from(item: dict[str, Any], fallback: str = "") -> str:
    for key in ("speaker", "speaker_id"):
        value = item.get(key)
        if value is not None and str(value).strip():
            return _speaker(value)
    return _speaker(fallback)


def _identifier_from(item: dict[str, Any], explicit: Any = "") -> str:
    for value in (explicit, item.get("id"), item.get("segment_id")):
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def format_speaker_label(value: Any) -> str:
    """Return a safe, non-spoken subtitle label for an opaque speaker ID."""

    speaker = _speaker(value)
    if not speaker:
        return ""
    match = re.fullmatch(r"speaker[\s_-]*(.+)", speaker, flags=re.IGNORECASE)
    if match:
        speaker = match.group(1).strip()
    moss = re.fullmatch(r"s(\d+)", speaker, flags=re.IGNORECASE)
    if moss:
        speaker = moss.group(1)
    safe = re.sub(r"\s+", "_", speaker)
    return f"SPEAKER_{safe}"


def _confidence(item: dict[str, Any]) -> float | None:
    for key in ("confidence", "probability", "score", "p"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _metadata(item: dict[str, Any], excluded: Iterable[str]) -> dict[str, Any]:
    excluded_keys = set(excluded)
    return {key: value for key, value in item.items() if key not in excluded_keys}


def _valid_span(start: Any, end: Any, *, scale: float = 1.0) -> tuple[int, int] | None:
    try:
        start_ms = round(float(start) * scale)
        end_ms = round(float(end) * scale)
    except (TypeError, ValueError):
        return None
    if start_ms < 0 or end_ms <= start_ms:
        return None
    return start_ms, end_ms


def _offset_span(item: dict[str, Any]) -> tuple[int, int] | None:
    offsets = item.get("offsets")
    if isinstance(offsets, dict):
        span = _valid_span(offsets.get("from"), offsets.get("to"))
        if span:
            return span
    if item.get("start_ms") is not None or item.get("end_ms") is not None:
        return _valid_span(item.get("start_ms"), item.get("end_ms"))
    return None


def _seconds_span(item: dict[str, Any]) -> tuple[int, int] | None:
    span = _offset_span(item)
    if span:
        return span
    return _valid_span(item.get("start"), item.get("end"), scale=1000.0)


def _crisp_span(item: dict[str, Any]) -> tuple[int, int] | None:
    span = _offset_span(item)
    if span:
        return span
    if item.get("t0") is not None or item.get("t1") is not None:
        return _valid_span(item.get("t0"), item.get("t1"), scale=10.0)
    return _valid_span(item.get("start"), item.get("end"), scale=1000.0)


def _word(
    item: dict[str, Any],
    *,
    span_parser: Callable[[dict[str, Any]], tuple[int, int] | None],
    fallback_speaker: str = "",
) -> TimedWord | None:
    span = span_parser(item)
    text = str(item.get("text") or item.get("word") or "").strip()
    if not span or not text:
        return None
    excluded = {
        "text", "word", "start", "end", "start_ms", "end_ms", "offsets",
        "t0", "t1", "speaker", "speaker_id", "confidence", "probability", "score", "p",
        "metadata",
    }
    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
    metadata.update(_metadata(item, excluded))
    return TimedWord(
        text=text,
        start_ms=span[0],
        end_ms=span[1],
        speaker=_speaker_from(item, fallback_speaker),
        confidence=_confidence(item),
        metadata=metadata,
    )


def _segment(
    item: dict[str, Any],
    *,
    span_parser: Callable[[dict[str, Any]], tuple[int, int] | None],
    word_span_parser: Callable[[dict[str, Any]], tuple[int, int] | None],
    identifier: Any = "",
) -> TimedSegment | None:
    speaker = _speaker_from(item)
    words = tuple(
        sorted(
            (
                parsed
                for value in (item.get("words") if isinstance(item.get("words"), list) else [])
                if isinstance(value, dict)
                and (parsed := _word(value, span_parser=word_span_parser, fallback_speaker=speaker))
            ),
            key=lambda value: (value.start_ms, value.end_ms),
        )
    )
    span = span_parser(item)
    if span is None and words:
        span = words[0].start_ms, max(word.end_ms for word in words)
    text = str(item.get("text") or "").strip()
    if not text and words:
        text = " ".join(word.text for word in words).strip()
    if not span or not text:
        return None
    excluded = {
        "id", "segment_id", "text", "start", "end", "start_ms", "end_ms",
        "offsets", "timestamps", "t0", "t1", "speaker", "speaker_id", "words",
        "metadata",
    }
    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}
    metadata.update(_metadata(item, excluded))
    return TimedSegment(
        text=text,
        start_ms=span[0],
        end_ms=span[1],
        speaker=speaker,
        identifier=_identifier_from(item, identifier),
        words=words,
        metadata=metadata,
    )


def _sorted_segments(segments: Iterable[TimedSegment]) -> tuple[TimedSegment, ...]:
    return tuple(sorted(segments, key=lambda value: (value.start_ms, value.end_ms)))


def _accepts_canonical(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("schema") == "pandrator.transcript.v1"


def _parse_canonical(payload: dict[str, Any]) -> NormalizedTranscript:
    segments = (
        parsed
        for item in payload.get("segments") or []
        if isinstance(item, dict)
        and (parsed := _segment(item, span_parser=_offset_span, word_span_parser=_offset_span))
    )
    return NormalizedTranscript(
        segments=_sorted_segments(segments),
        source_format=str(payload.get("source_format") or "pandrator-v1"),
        language=str(payload.get("language") or ""),
        metadata=dict(payload.get("metadata") or {}),
    )


def _accepts_crispasr(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("transcription"), list)


def _parse_crispasr(payload: dict[str, Any]) -> NormalizedTranscript:
    groups = payload.get("transcription") or []
    segments = (
        parsed
        for item in groups
        if isinstance(item, dict)
        and (parsed := _segment(item, span_parser=_crisp_span, word_span_parser=_crisp_span))
    )
    header = payload.get("crispasr") if isinstance(payload.get("crispasr"), dict) else {}
    backend = str(header.get("backend") or "")
    diarization = "native" if backend == "moss-diarize" else "external-or-channel"
    return NormalizedTranscript(
        segments=_sorted_segments(segments),
        source_format="crispasr",
        language=str(header.get("language_detected") or header.get("language") or payload.get("language") or ""),
        metadata={"engine": backend, "diarization": diarization, "header": dict(header)},
    )


def _accepts_whisperx(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("segments"), list)


def _parse_whisperx(payload: dict[str, Any]) -> NormalizedTranscript:
    raw_segments = payload.get("segments") or []
    segments = [
        parsed
        for item in raw_segments
        if isinstance(item, dict)
        and (parsed := _segment(item, span_parser=_seconds_span, word_span_parser=_seconds_span))
    ]
    if not any(segment.words for segment in segments) and isinstance(payload.get("word_segments"), list):
        top_words = [
            parsed
            for item in payload["word_segments"]
            if isinstance(item, dict)
            and (parsed := _word(item, span_parser=_seconds_span))
        ]
        rebuilt: list[TimedSegment] = []
        for segment in segments:
            words = tuple(
                word
                for word in top_words
                if min(segment.end_ms, word.end_ms) > max(segment.start_ms, word.start_ms)
            )
            rebuilt.append(
                TimedSegment(
                    text=segment.text,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    speaker=segment.speaker,
                    identifier=segment.identifier,
                    words=words,
                    metadata=segment.metadata,
                )
            )
        segments = rebuilt
    has_speakers = any(segment.speaker or any(word.speaker for word in segment.words) for segment in segments)
    return NormalizedTranscript(
        segments=_sorted_segments(segments),
        source_format="whisperx",
        language=str(payload.get("language") or ""),
        metadata={"diarization": "pyannote-or-supplied" if has_speakers else "none"},
    )


def _accepts_moss_ctc(payload: Any) -> bool:
    return isinstance(payload, list) and bool(payload) and all(
        isinstance(item, dict)
        and ("moss_segment_id" in item or "segment_id" in item)
        and ("start" in item or "start_ms" in item)
        for item in payload
    )


def _parse_moss_ctc(payload: list[dict[str, Any]]) -> NormalizedTranscript:
    groups: list[tuple[str, list[TimedWord]]] = []
    positions: dict[str, int] = {}
    for item in payload:
        parsed = _word(item, span_parser=_seconds_span)
        if not parsed:
            continue
        raw_key = item.get("moss_segment_id")
        if raw_key is None or not str(raw_key).strip():
            raw_key = item.get("segment_id")
        key = str(raw_key if raw_key is not None and str(raw_key).strip() else f"word-{len(groups)}")
        if key not in positions:
            positions[key] = len(groups)
            groups.append((key, []))
        groups[positions[key]][1].append(parsed)
    segments = []
    for identifier, words in groups:
        ordered = tuple(sorted(words, key=lambda value: (value.start_ms, value.end_ms)))
        if not ordered:
            continue
        speakers = [word.speaker for word in ordered if word.speaker]
        segments.append(
            TimedSegment(
                text=" ".join(word.text for word in ordered),
                start_ms=ordered[0].start_ms,
                end_ms=max(word.end_ms for word in ordered),
                speaker=speakers[0] if speakers else "",
                identifier=identifier,
                words=ordered,
            )
        )
    return NormalizedTranscript(
        segments=_sorted_segments(segments),
        source_format="moss-ctc-words",
        metadata={"diarization": "preserved-from-native"},
    )


def _accepts_moss_segments(payload: Any) -> bool:
    return isinstance(payload, list) and (not payload or all(
        isinstance(item, dict)
        and "text" in item
        and ("start" in item or "start_ms" in item)
        and ("end" in item or "end_ms" in item)
        for item in payload
    ))


def _parse_moss_segments(payload: list[dict[str, Any]]) -> NormalizedTranscript:
    segments = (
        parsed
        for item in payload
        if (parsed := _segment(item, span_parser=_seconds_span, word_span_parser=_seconds_span))
    )
    return NormalizedTranscript(
        segments=_sorted_segments(segments),
        source_format="moss-transcribe-cpp",
        metadata={"diarization": "native"},
    )


_BUILTIN_ADAPTERS = (
    TranscriptAdapter("pandrator-v1", _accepts_canonical, _parse_canonical),
    TranscriptAdapter("crispasr", _accepts_crispasr, _parse_crispasr),
    TranscriptAdapter("whisperx", _accepts_whisperx, _parse_whisperx),
    TranscriptAdapter("moss-ctc-words", _accepts_moss_ctc, _parse_moss_ctc),
    TranscriptAdapter("moss-transcribe-cpp", _accepts_moss_segments, _parse_moss_segments),
)


def normalize_transcript(payload: Any, *, format_hint: str = "") -> NormalizedTranscript:
    if isinstance(payload, NormalizedTranscript):
        return payload
    adapters = (*_CUSTOM_ADAPTERS, *_BUILTIN_ADAPTERS)
    hint = str(format_hint or "").strip().casefold()
    if hint:
        adapters = tuple(adapter for adapter in adapters if adapter.name == hint)
        if not adapters:
            raise TranscriptFormatError(f"Unknown transcript format hint: {format_hint}")
    for adapter in adapters:
        if adapter.accepts(payload):
            result = adapter.parse(payload)
            if not isinstance(result, NormalizedTranscript):
                raise TranscriptFormatError(f"Transcript adapter {adapter.name} returned an invalid result.")
            return result
    raise TranscriptFormatError("Unsupported timed transcript JSON format.")


def load_transcript(path: str | Path, *, format_hint: str = "") -> NormalizedTranscript:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise TranscriptFormatError(f"Could not read timed transcript JSON: {source}") from error
    return normalize_transcript(payload, format_hint=format_hint)
