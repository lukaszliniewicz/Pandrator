"""Speech-block generation for Pandrator-native dubbing."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from .languages import normalize_language_code
from .models import SpeechBlock, SubtitleSegment
from .srt_utils import parse_srt

logger = logging.getLogger(__name__)

try:
    from sentence_splitter import SentenceSplitter  # type: ignore[import-untyped]
except Exception:  # noqa: BLE001  # pragma: no cover - optional runtime dependency
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
    "en": [
        "and",
        "but",
        "or",
        "because",
        "although",
        "so",
        "while",
        "if",
        "then",
        "that",
        "as",
        "for",
        "since",
        "until",
        "whether",
    ],
    "es": [
        "y",
        "pero",
        "o",
        "porque",
        "aunque",
        "así",
        "mientras",
        "si",
        "entonces",
        "que",
        "como",
        "pues",
        "desde",
        "hasta",
    ],
    "fr": [
        "et",
        "mais",
        "ou",
        "parce que",
        "bien que",
        "donc",
        "pendant que",
        "si",
        "alors",
        "que",
        "comme",
        "car",
        "depuis",
        "jusqu'à",
    ],
    "de": [
        "und",
        "aber",
        "oder",
        "weil",
        "obwohl",
        "also",
        "während",
        "wenn",
        "dann",
        "dass",
        "als",
        "denn",
        "seit",
        "bis",
        "ob",
    ],
    "it": [
        "e",
        "ma",
        "o",
        "perché",
        "sebbene",
        "quindi",
        "mentre",
        "se",
        "allora",
        "che",
        "come",
        "poiché",
        "da quando",
        "fino a",
    ],
    "pt": [
        "e",
        "mas",
        "ou",
        "porque",
        "embora",
        "então",
        "enquanto",
        "se",
        "logo",
        "que",
        "como",
        "pois",
        "desde",
        "até",
    ],
    "pl": [
        "i",
        "ale",
        "lub",
        "ponieważ",
        "chociaż",
        "więc",
        "podczas gdy",
        "jeśli",
        "wtedy",
        "że",
        "jak",
        "gdyż",
        "od",
        "aż",
        "czy",
    ],
    "tr": [
        "ve",
        "ama",
        "veya",
        "çünkü",
        "rağmen",
        "bu yüzden",
        "iken",
        "eğer",
        "o zaman",
        "ki",
        "gibi",
        "zira",
    ],
    "ru": [
        "и",
        "но",
        "или",
        "потому что",
        "хотя",
        "так что",
        "пока",
        "если",
        "тогда",
        "что",
        "как",
        "ибо",
        "с",
        "до",
        "ли",
    ],
    "nl": [
        "en",
        "maar",
        "of",
        "omdat",
        "hoewel",
        "dus",
        "terwijl",
        "als",
        "dan",
        "dat",
        "zoals",
        "want",
        "sinds",
        "tot",
    ],
    "cs": [
        "a",
        "ale",
        "nebo",
        "protože",
        "ačkoli",
        "takže",
        "zatímco",
        "jestli",
        "pak",
        "že",
        "jako",
        "neboť",
        "od",
        "až",
        "zda",
    ],
    "hu": [
        "és",
        "de",
        "vagy",
        "mert",
        "bár",
        "tehát",
        "míg",
        "ha",
        "akkor",
        "hogy",
        "mint",
        "hiszen",
        "óta",
        "ameddig",
        "vajon",
    ],
    "ar": [
        "و",
        "لكن",
        "أو",
        "لأن",
        "رغم أن",
        "لذلك",
        "بينما",
        "إذا",
        "ثم",
        "أن",
        "كما",
        "ف",
        "منذ",
        "حتى",
        "هل",
    ],
    "zh-cn": [
        "和",
        "但是",
        "或者",
        "因为",
        "虽然",
        "所以",
        "当",
        "如果",
        "那么",
        "的",
        "作为",
        "由于",
        "从",
        "直到",
        "是否",
    ],
    "ja": [
        "そして",
        "しかし",
        "または",
        "なぜなら",
        "にもかかわらず",
        "だから",
        "もし",
        "その時",
        "と",
        "ように",
        "から",
        "以来",
        "まで",
        "かどうか",
    ],
    "ko": [
        "그리고",
        "하지만",
        "또는",
        "왜냐하면",
        "비록",
        "그래서",
        "동안",
        "만약",
        "그때",
        "것",
        "처럼",
        "때문에",
        "이후",
        "까지",
        "인지",
    ],
}

_FALLBACK_SENTENCE_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")
_SPEAKER_PREFIX_RE = re.compile(r"^\[(?P<speaker>SPEAKER[^\]]*)\]:\s*", re.IGNORECASE)
_TERMINAL_SENTENCE_RE = re.compile(
    r"[.!?\u2026\u3002\uff01\uff1f][\"'\u201d\u2019)\]}]*$"
)
SAME_SPEAKER_OVERLAP_TOLERANCE_MS = 120


def _event(
    action: str,
    reason_code: str,
    summary: str,
    *,
    measurements: dict[str, object] | None = None,
    source_references: list[int] | None = None,
) -> dict[str, object]:
    """Build one compact, factual provenance event.

    Measurements are intentionally kept as raw values.  Consumers can render
    a human explanation from the stable action/reason code without treating
    generated prose as an authoritative decision record.
    """

    return {
        "action": action,
        "reason_code": reason_code,
        "summary": summary,
        "measurements": dict(measurements or {}),
        "source_references": list(source_references or []),
    }


def _boundary(
    action: str,
    reason_code: str,
    summary: str,
    *,
    measurements: dict[str, object] | None = None,
    source_references: list[int] | None = None,
) -> dict[str, object]:
    return _event(
        action,
        reason_code,
        summary,
        measurements=measurements,
        source_references=source_references,
    )


@dataclass
class _SpeechPart:
    text: str
    optimized_text: str
    subtitles: list[int]
    start_ms: int
    end_ms: int
    speaker: str = ""
    speaker_key: str = ""
    # A span is ``(start, end, cue_index)`` in the corresponding canonical
    # text variant.  Keeping both maps lets reviewed display and TTS text be
    # partitioned independently without duplicating either one.
    text_spans: list[tuple[int, int, int]] = field(default_factory=list)
    optimized_spans: list[tuple[int, int, int]] = field(default_factory=list)
    timings: dict[int, tuple[int, int]] = field(default_factory=dict)
    cue_texts: dict[int, tuple[str, str]] = field(default_factory=dict)
    formation_events: list[dict[str, object]] = field(default_factory=list)
    boundary_before: dict[str, object] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)


def _check_split_validity(
    text: str, split_index: int, max_chars: int, min_chars: int
) -> bool:
    if split_index <= 0 or split_index >= len(text):
        return False
    first = text[:split_index].strip()
    second = text[split_index:].strip()
    return bool(
        first
        and second
        and min_chars <= len(first) <= max_chars
        and len(second) >= min_chars
    )


def _split_further(
    text: str, language_code: str, max_chars: int, min_chars: int
) -> list[str]:
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
            if distance < best_distance or (
                distance == best_distance and split_index > best_index
            ):
                best_distance = distance
                best_index = split_index
        if best_index >= 0:
            return [
                part
                for segment in (
                    text[:best_index].strip(),
                    *_split_further(
                        text[best_index:].strip(), language_code, max_chars, min_chars
                    ),
                )
                for part in ([segment] if segment else [])
            ]

    best_index = -1
    best_distance = float("inf")
    for conjunction in CONJUNCTIONS.get(language_code, []):
        for match in re.finditer(
            r"\b" + re.escape(conjunction) + r"\b", text, re.IGNORECASE
        ):
            split_index = match.start()
            if not _check_split_validity(text, split_index, max_chars, min_chars):
                continue
            distance = abs(split_index - midpoint)
            if distance < best_distance or (
                distance == best_distance and split_index > best_index
            ):
                best_distance = distance
                best_index = split_index
    if best_index >= 0:
        return [
            part
            for segment in (
                text[:best_index].strip(),
                *_split_further(
                    text[best_index:].strip(), language_code, max_chars, min_chars
                ),
            )
            for part in ([segment] if segment else [])
        ]

    best_index = -1
    best_distance = float("inf")
    for idx in range(min(len(text) - 1, max_chars), min_chars - 1, -1):
        if not text[idx].isspace():
            continue
        first = text[:idx].strip()
        second = text[idx + 1 :].strip()
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
                *_split_further(
                    text[best_index + 1 :].strip(), language_code, max_chars, min_chars
                ),
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
            *_split_further(
                text[hard_cut:].strip(), language_code, max_chars, min_chars
            ),
        )
        for part in ([segment] if segment else [])
    ]


def _split_subtitle_text(
    text: str, language_code: str, min_chars: int, max_chars: int
) -> list[str]:
    # SRT line breaks are presentation metadata, not pauses or literal input
    # for a speech engine.  Keep speech-block text canonical and single-line.
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    if SentenceSplitter is not None and language_code in SENTENCE_SPLITTER_LANGUAGES:
        try:
            splitter = SentenceSplitter(language=language_code)
            sentences = [
                sentence.strip()
                for sentence in splitter.split(text=text)
                if sentence.strip()
            ]
        except Exception as error:  # noqa: BLE001 - third-party splitter boundary
            logger.warning(
                "Could not initialize SentenceSplitter for %s: %s", language_code, error
            )
            sentences = []
    else:
        sentences = []

    if not sentences:
        sentences = [
            part.strip() for part in _FALLBACK_SENTENCE_RE.split(text) if part.strip()
        ]
    if not sentences:
        sentences = [text]

    parts: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            parts.append(sentence)
        else:
            parts.extend(_split_further(sentence, language_code, max_chars, min_chars))
    return [part for part in parts if part]


def _canonical_cue_text(value: object) -> tuple[str, str]:
    """Return canonical spoken text plus any inline speaker label."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    speaker_match = _SPEAKER_PREFIX_RE.match(text)
    if speaker_match is None:
        return text, ""
    return text[speaker_match.end() :].strip(), speaker_match.group("speaker").strip()


def _subtitle_to_part(
    subtitle: SubtitleSegment,
    optimized_subtitle: SubtitleSegment | None,
    speaker_override: str = "",
    speaker_metadata_expected: bool = False,
) -> _SpeechPart | None:
    display_text, inline_speaker = _canonical_cue_text(subtitle.text)
    optimized_text, optimized_inline_speaker = _canonical_cue_text(
        optimized_subtitle.text if optimized_subtitle is not None else subtitle.text
    )
    if not display_text or not optimized_text:
        return None
    speaker = str(
        speaker_override
        or subtitle.speaker
        or (optimized_subtitle.speaker if optimized_subtitle is not None else "")
        or inline_speaker
        or optimized_inline_speaker
        or ""
    ).strip()
    return _SpeechPart(
        text=display_text,
        optimized_text=optimized_text,
        subtitles=[subtitle.index],
        start_ms=subtitle.start_ms,
        end_ms=subtitle.end_ms,
        speaker=speaker,
        speaker_key=(
            speaker.casefold()
            if speaker
            else f"unknown-cue:{subtitle.index}"
            if speaker_metadata_expected
            else ""
        ),
        text_spans=[(0, len(display_text), subtitle.index)],
        optimized_spans=[(0, len(optimized_text), subtitle.index)],
        timings={subtitle.index: (subtitle.start_ms, subtitle.end_ms)},
        cue_texts={subtitle.index: (display_text, optimized_text)},
        boundary_before=_boundary(
            "keep_boundary",
            "cue_boundary",
            "Subtitle cue boundary retained.",
            measurements={
                "gap_ms": None,
                "speaker": speaker or None,
            },
            source_references=[subtitle.index],
        ),
        risk_flags=(
            ["speaker_unknown"] if speaker_metadata_expected and not speaker else []
        ),
    )


def _should_merge_parts(
    previous: _SpeechPart,
    current: _SpeechPart,
    max_chars: int,
    merge_threshold: int,
    max_internal_gap_ms: int,
) -> bool:
    if not (
        _sentence_is_complete(previous.text) and _sentence_is_complete(current.text)
    ):
        return False
    display_length = len(previous.text) + len(current.text) + 1
    speech_length = len(previous.optimized_text) + len(current.optimized_text) + 1
    if max(display_length, speech_length) > max_chars:
        return False

    if previous.speaker_key != current.speaker_key:
        return False

    gap_ms = current.start_ms - previous.end_ms
    # The merge threshold is a user-facing timing rule, so sentence-final
    # punctuation must not silently override it. Punctuation remains in the
    # combined text for TTS prosody; speaker, gap, and capacity are the hard
    # block boundaries.
    return not (
        gap_ms < -SAME_SPEAKER_OVERLAP_TOLERANCE_MS
        or gap_ms > merge_threshold
        or gap_ms > max_internal_gap_ms
    )


def _sentence_is_complete(text: str) -> bool:
    return _TERMINAL_SENTENCE_RE.search(str(text or "").rstrip()) is not None


def _repair_diarization_flicker(
    parts: list[_SpeechPart],
    continuation_threshold_ms: int,
) -> list[_SpeechPart]:
    """Repair high-confidence local speaker-ID flicker within one sentence.

    Chunk-local diarization can alternate two speaker IDs across a series of
    tiny, non-overlapping fragments even though the text is one continuous
    sentence.  A normal speaker hand-off remains a hard boundary: repair is
    limited to short runs with at least three ID changes before the first
    sentence ending.  This catches the observed MOSS seam artifact without
    merging an ordinary A/B exchange or simultaneous speech.
    """

    repaired = list(parts)
    start = 0
    while start < len(repaired):
        end = start + 1
        while end < len(repaired):
            previous = repaired[end - 1]
            current = repaired[end]
            if _sentence_is_complete(previous.text):
                break
            gap_ms = current.start_ms - previous.end_ms
            if (
                gap_ms < -SAME_SPEAKER_OVERLAP_TOLERANCE_MS
                or gap_ms > continuation_threshold_ms
            ):
                break
            if current.end_ms - repaired[start].start_ms > 30_000:
                break
            end += 1

        run = repaired[start:end]
        speaker_keys = [part.speaker_key for part in run]
        distinct_speakers = set(speaker_keys)
        transitions = sum(left != right for left, right in pairwise(speaker_keys))
        short_fragments = sum(len(part.text) <= 40 for part in run)
        has_opaque_speaker = any(
            not key or key.startswith("unknown-cue:") for key in speaker_keys
        )
        if (
            len(run) >= 4
            and transitions >= 3
            and len(distinct_speakers) == 2
            and short_fragments * 2 >= len(run)
            and not has_opaque_speaker
        ):
            anchor = run[0]
            references = sorted(
                {subtitle for item in run for subtitle in item.subtitles}
            )
            repair_event = _event(
                "repair_diarization_flicker",
                "diarization_flicker_repaired",
                "Diarization speaker flicker repaired.",
                measurements={
                    "cue_count": len(run),
                    "speaker_transition_count": transitions,
                    "distinct_speaker_count": len(distinct_speakers),
                    "continuation_threshold_ms": continuation_threshold_ms,
                },
                source_references=references,
            )
            repaired[start:end] = [
                _SpeechPart(
                    text=part.text,
                    optimized_text=part.optimized_text,
                    subtitles=list(part.subtitles),
                    start_ms=part.start_ms,
                    end_ms=part.end_ms,
                    speaker=anchor.speaker,
                    speaker_key=anchor.speaker_key,
                    text_spans=list(part.text_spans),
                    optimized_spans=list(part.optimized_spans),
                    timings=dict(part.timings),
                    cue_texts=dict(part.cue_texts),
                    formation_events=[*part.formation_events, repair_event],
                    boundary_before=dict(part.boundary_before),
                    risk_flags=[*part.risk_flags, "diarization_flicker_repaired"],
                )
                for part in run
            ]
            logger.info(
                "Repaired diarization flicker across subtitle cues %s.",
                sorted({subtitle for part in run for subtitle in part.subtitles}),
            )
        start = end
    return repaired


def _join_variant(
    previous_text: str,
    previous_spans: list[tuple[int, int, int]],
    current_text: str,
    current_spans: list[tuple[int, int, int]],
) -> tuple[str, list[tuple[int, int, int]]]:
    separator = " " if previous_text and current_text else ""
    offset = len(previous_text) + len(separator)
    return (
        f"{previous_text}{separator}{current_text}".strip(),
        [
            *previous_spans,
            *[
                (start + offset, end + offset, subtitle)
                for start, end, subtitle in current_spans
            ],
        ],
    )


def _combine_parts(previous: _SpeechPart, current: _SpeechPart) -> _SpeechPart:
    text, text_spans = _join_variant(
        previous.text,
        previous.text_spans,
        current.text,
        current.text_spans,
    )
    optimized_text, optimized_spans = _join_variant(
        previous.optimized_text,
        previous.optimized_spans,
        current.optimized_text,
        current.optimized_spans,
    )
    return _SpeechPart(
        text=text,
        optimized_text=optimized_text,
        subtitles=sorted(set(previous.subtitles + current.subtitles)),
        start_ms=previous.start_ms,
        end_ms=max(previous.end_ms, current.end_ms),
        speaker=previous.speaker,
        speaker_key=previous.speaker_key,
        text_spans=text_spans,
        optimized_spans=optimized_spans,
        timings={**previous.timings, **current.timings},
        cue_texts={**previous.cue_texts, **current.cue_texts},
        formation_events=[*previous.formation_events, *current.formation_events],
        boundary_before=dict(previous.boundary_before),
        risk_flags=sorted({*previous.risk_flags, *current.risk_flags}),
    )


def _break_cost(text: str, position: int, preferred_breaks: set[int]) -> float:
    if position >= len(text):
        return 0.0
    before = text[:position].rstrip()
    if not before:
        return 30.0
    last = before[-1]
    cost = 3.0
    if last in ".!?\u2026\u3002\uff01\uff1f":
        cost -= 8.0
    elif last in ",;:\u2014\u2013":
        cost -= 4.0
    if position in preferred_breaks:
        cost -= 6.0
    if position < len(text) and not text[position].isspace():
        cost += 18.0
    return cost


def _partition_variant_exact(
    text: str,
    spans: list[tuple[int, int, int]],
    *,
    part_count: int,
    min_chars: int,
    max_chars: int,
) -> list[tuple[str, int, int, list[int]]] | None:
    """Partition one text variant into an exact number of balanced ranges.

    Cue ends, sentence endings, and clause punctuation are preferred, but
    ``min_chars`` remains a soft quality target.  A character-level fallback
    exists solely for languages without spaces and single tokens longer than
    an engine's hard limit.
    """

    text = str(text or "").strip()
    if not text or part_count < 1:
        return None
    preferred_breaks = {end for _start, end, _subtitle in spans}
    positions = {0, len(text), *preferred_breaks}
    positions.update(match.start() for match in re.finditer(r"\s+", text))
    positions.update(
        match.end()
        for match in re.finditer(r"[.!?,;:\u2026\u3002\uff01\uff1f\u2014\u2013]", text)
    )

    def solve(candidates: list[int]) -> list[int] | None:
        target = len(text) / part_count
        states: dict[int, tuple[float, list[int]]] = {0: (0.0, [0])}
        for part_index in range(part_count):
            next_states: dict[int, tuple[float, list[int]]] = {}
            parts_left = part_count - part_index - 1
            for start, (base_cost, path) in states.items():
                for end in candidates:
                    if end <= start:
                        continue
                    if parts_left and end >= len(text):
                        break
                    if not parts_left and end != len(text):
                        continue
                    segment = text[start:end].strip()
                    if not segment or len(segment) > max_chars:
                        if len(segment) > max_chars:
                            break
                        continue
                    remaining = text[end:].strip()
                    if parts_left and len(remaining) < parts_left:
                        continue
                    balance_cost = (
                        (len(segment) - target) / max(target, 1.0)
                    ) ** 2 * 12.0
                    shortfall = max(0, min_chars - len(segment))
                    quality_cost = balance_cost + (shortfall * shortfall * 0.3)
                    quality_cost += _break_cost(text, end, preferred_breaks)
                    total = base_cost + quality_cost
                    existing = next_states.get(end)
                    if existing is None or total < existing[0]:
                        next_states[end] = (total, [*path, end])
            states = next_states
            if not states:
                return None
        result = states.get(len(text))
        return result[1] if result is not None else None

    candidates = sorted(
        position for position in positions if 0 <= position <= len(text)
    )
    boundaries = solve(candidates)
    if boundaries is None:
        # Unspaced scripts and unexpectedly long tokens still need to respect
        # the synthesis engine's hard cap.
        boundaries = solve(list(range(len(text) + 1)))
    if boundaries is None:
        return None

    result: list[tuple[str, int, int, list[int]]] = []
    for start, end in pairwise(boundaries):
        value = text[start:end].strip()
        references = sorted(
            {
                subtitle
                for span_start, span_end, subtitle in spans
                if span_start < end and span_end > start
            }
        )
        result.append((value, start, end, references))
    return result


def _minimum_part_count(text: str, max_chars: int) -> int:
    return max(1, (len(str(text or "").strip()) + max_chars - 1) // max_chars)


def _rebase_partition_spans(
    spans: list[tuple[int, int, int]],
    start: int,
    end: int,
    text: str,
) -> list[tuple[int, int, int]]:
    """Clip and rebase spans after the partition text is stripped."""

    selected = str(text or "")[start:end]
    leading = len(selected) - len(selected.lstrip())
    trailing = len(selected.rstrip())
    kept_start = start + leading
    kept_end = start + trailing
    if kept_end <= kept_start:
        return []
    return [
        (
            max(span_start, kept_start) - kept_start,
            min(span_end, kept_end) - kept_start,
            subtitle,
        )
        for span_start, span_end, subtitle in spans
        if span_start < kept_end and span_end > kept_start
    ]


def _partition_break_rule(
    text: str, offset: int, spans: list[tuple[int, int, int]]
) -> str:
    if any(end == offset for _start, end, _subtitle in spans):
        return "cue"
    before = str(text or "")[:offset].rstrip()
    if before and before[-1] in ".!?\u2026\u3002\uff01\uff1f":
        return "sentence"
    if before and before[-1] in ",;:\u2014\u2013":
        return "clause"
    if (offset > 0 and str(text or "")[offset - 1].isspace()) or (
        offset < len(str(text or "")) and str(text or "")[offset].isspace()
    ):
        return "whitespace"
    return "hard"


def _split_utterance(
    utterance: _SpeechPart,
    language_code: str,
    min_chars: int,
    max_chars: int,
    *,
    reviewed_speech: bool,
) -> list[_SpeechPart]:
    # SentenceSplitter remains useful for estimating a natural lower bound,
    # while the exact paired partition below is responsible for preserving
    # display/speech correspondence and provenance.
    display_hint = _split_subtitle_text(
        utterance.text, language_code, min_chars, max_chars
    )
    speech_hint = _split_subtitle_text(
        utterance.optimized_text, language_code, min_chars, max_chars
    )
    # The hard limit protects the text sent to TTS.  When a reviewed speech
    # variant exists, its display subtitle may legitimately be longer; forcing
    # the display copy under the TTS cap would manufacture tiny speech chunks.
    part_count = max(
        _minimum_part_count(utterance.optimized_text, max_chars),
        len(speech_hint),
        *(
            ()
            if reviewed_speech
            else (
                _minimum_part_count(utterance.text, max_chars),
                len(display_hint),
            )
        ),
    )
    maximum_parts = max(len(utterance.text), len(utterance.optimized_text), part_count)
    display_parts = None
    speech_parts = None
    while part_count <= maximum_parts:
        display_parts = _partition_variant_exact(
            utterance.text,
            utterance.text_spans,
            part_count=part_count,
            min_chars=min_chars,
            max_chars=(max(len(utterance.text), 1) if reviewed_speech else max_chars),
        )
        speech_parts = _partition_variant_exact(
            utterance.optimized_text,
            utterance.optimized_spans,
            part_count=part_count,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if display_parts is not None and speech_parts is not None:
            break
        part_count += 1
    if display_parts is None or speech_parts is None:
        raise ValueError("Could not create a bounded paired speech-block partition.")

    result: list[_SpeechPart] = []
    for part_index, (display, speech) in enumerate(
        zip(display_parts, speech_parts, strict=True)
    ):
        display_text, display_start, display_end, display_refs = display
        speech_text, speech_start, speech_end, speech_refs = speech
        references = sorted({*display_refs, *speech_refs})
        if not references:
            references = list(utterance.subtitles)
        timings = {
            reference: utterance.timings[reference]
            for reference in references
            if reference in utterance.timings
        }
        start_ms = min(
            (value[0] for value in timings.values()), default=utterance.start_ms
        )
        end_ms = max((value[1] for value in timings.values()), default=utterance.end_ms)
        display_spans = _rebase_partition_spans(
            utterance.text_spans,
            display_start,
            display_end,
            utterance.text,
        )
        speech_spans = _rebase_partition_spans(
            utterance.optimized_spans,
            speech_start,
            speech_end,
            utterance.optimized_text,
        )
        display_boundary_offset = display_start if part_index else display_end
        speech_boundary_offset = speech_start if part_index else speech_end
        display_rule = _partition_break_rule(
            utterance.text,
            display_boundary_offset,
            utterance.text_spans,
        )
        speech_rule = _partition_break_rule(
            utterance.optimized_text,
            speech_boundary_offset,
            utterance.optimized_spans,
        )
        break_rule = display_rule if display_rule == speech_rule else "paired"
        split_event = (
            _event(
                "split_capacity",
                "capacity_split",
                "Speech block split at a deterministic capacity boundary.",
                measurements={
                    "display_offset": display_boundary_offset,
                    "speech_offset": speech_boundary_offset,
                    "display_length": len(utterance.text),
                    "speech_length": len(utterance.optimized_text),
                    "block_display_length": len(display_text),
                    "block_speech_length": len(speech_text),
                    "max_chars": max_chars,
                    "part_index": part_index,
                    "part_count": len(display_parts),
                    "display_break_rule": display_rule,
                    "speech_break_rule": speech_rule,
                    "break_rule": break_rule,
                },
                source_references=references,
            )
            if len(display_parts) > 1
            else None
        )
        boundary_before = (
            dict(utterance.boundary_before)
            if part_index == 0
            else _boundary(
                "keep_boundary",
                "capacity_split",
                "Capacity boundary retained between speech blocks.",
                measurements={
                    "display_offset": display_start,
                    "speech_offset": speech_start,
                    "display_break_rule": display_rule,
                    "speech_break_rule": speech_rule,
                    "max_chars": max_chars,
                },
                source_references=references,
            )
        )
        risk_flags = sorted(set(utterance.risk_flags))
        if len(display_parts) > 1:
            if "hard" in {display_rule, speech_rule}:
                risk_flags.append("hard_capacity_split")
            elif "whitespace" in {display_rule, speech_rule}:
                risk_flags.append("whitespace_capacity_split")
        result.append(
            _SpeechPart(
                text=display_text,
                optimized_text=speech_text,
                subtitles=references,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker=utterance.speaker,
                speaker_key=utterance.speaker_key,
                text_spans=display_spans,
                optimized_spans=speech_spans,
                timings=timings,
                cue_texts=dict(utterance.cue_texts),
                formation_events=[
                    *utterance.formation_events,
                    *([split_event] if split_event is not None else []),
                ],
                boundary_before=boundary_before,
                risk_flags=sorted(set(risk_flags)),
            )
        )
    return result


def _parts_to_blocks(
    parts: list[_SpeechPart],
    *,
    include_optimized_text: bool,
) -> list[SpeechBlock]:
    def pairs(spans: list[tuple[int, int, int]], reference: int) -> list[list[int]]:
        seen: set[tuple[int, int]] = set()
        values: list[list[int]] = []
        for start, end, subtitle in spans:
            if subtitle != reference or end <= start:
                continue
            pair = (int(start), int(end))
            if pair not in seen:
                seen.add(pair)
                values.append([pair[0], pair[1]])
        return values

    def provenance(part: _SpeechPart) -> dict[str, object]:
        references = sorted(set(part.subtitles))
        source_cues = []
        for reference in references:
            display_text, speech_text = part.cue_texts.get(reference, (None, None))
            timing = part.timings.get(reference)
            source_cues.append(
                {
                    "reference": int(reference),
                    "start_ms": int(timing[0]) if timing else None,
                    "end_ms": int(timing[1]) if timing else None,
                    "display_text": display_text,
                    "speech_text": speech_text,
                    "display_spans": pairs(part.text_spans, reference),
                    "speech_spans": pairs(part.optimized_spans, reference),
                }
            )
        boundary = dict(part.boundary_before or {})
        if not boundary:
            boundary = _boundary(
                "keep_boundary",
                "document_start",
                "Speech block starts the document.",
                source_references=references,
            )
        return {
            "schema_version": 1,
            "origin": "automatic",
            "source_reference_namespace": "subtitle_ordinal",
            "source_cues": source_cues,
            "formation_events": [dict(item) for item in part.formation_events],
            "boundary_before": boundary,
            "risk_flags": sorted({str(value) for value in part.risk_flags}),
        }

    blocks: list[SpeechBlock] = []
    alignment_group_number = 0
    previous_subtitles: set[int] = set()
    for index, part in enumerate(parts, start=1):
        subtitles = sorted(set(part.subtitles))
        current_subtitles = set(subtitles)
        if not blocks or not previous_subtitles.intersection(current_subtitles):
            alignment_group_number += 1
        alignment_group = f"a{alignment_group_number:04d}"
        blocks.append(
            SpeechBlock(
                number=str(index).zfill(4),
                text=re.sub(r"\s+", " ", part.text).strip(),
                subtitles=subtitles,
                speaker=part.speaker,
                alignment_group=alignment_group,
                optimized_text=(
                    re.sub(r"\s+", " ", part.optimized_text).strip()
                    if include_optimized_text
                    else ""
                ),
                provenance=provenance(part),
            )
        )
        previous_subtitles = current_subtitles
    return blocks


def create_speech_blocks(
    srt_content: str,
    target_language: str = "en",
    min_chars: int = 10,
    max_chars: int = 220,
    merge_threshold: int = 1500,
    *,
    continuation_threshold_ms: int | None = None,
    max_internal_gap_ms: int | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    speech_srt_content: str | None = None,
) -> list[dict[str, object]]:
    """Create natural, speaker-safe Pandrator/Subdub speech blocks.

    ``merge_threshold`` controls optional packing of complete utterances.
    ``continuation_threshold_ms`` may be larger so an unfinished sentence is
    not stranded merely because a subtitle cue boundary contains a pause.
    ``max_internal_gap_ms`` is an independent hard timing guard: a TTS chunk
    never spans a larger silent interval even if the sentence is unfinished.
    When ``speech_srt_content`` is supplied, display and reviewed speech text
    are partitioned together and neither variant is repeated.
    """
    max_chars = max(1, int(max_chars))
    min_chars = max(1, min(int(min_chars), max_chars))
    merge_threshold = max(0, int(merge_threshold))
    continuation_threshold = max(
        0,
        int(
            merge_threshold
            if continuation_threshold_ms is None
            else continuation_threshold_ms
        ),
    )
    maximum_internal_gap = max(
        0,
        int(
            continuation_threshold
            if max_internal_gap_ms is None
            else max_internal_gap_ms
        ),
    )
    language_code = normalize_language_code(target_language)
    subtitles = parse_srt(srt_content)
    optimized_subtitles = (
        parse_srt(speech_srt_content) if speech_srt_content is not None else None
    )
    if optimized_subtitles is not None and [item.index for item in subtitles] != [
        item.index for item in optimized_subtitles
    ]:
        raise ValueError(
            "Reviewed speech subtitles must have the same cue indexes as display subtitles."
        )
    speaker_metadata_expected = bool(speaker_by_subtitle) or any(
        subtitle.speaker for subtitle in subtitles
    )
    all_parts = [
        part
        for index, subtitle in enumerate(subtitles)
        if (
            part := _subtitle_to_part(
                subtitle,
                optimized_subtitles[index] if optimized_subtitles is not None else None,
                str((speaker_by_subtitle or {}).get(subtitle.index) or ""),
                speaker_metadata_expected,
            )
        )
        is not None
    ]
    all_parts = _repair_diarization_flicker(
        all_parts,
        continuation_threshold,
    )

    # First reconstruct complete utterances without imposing the TTS size cap.
    # Applying the cap greedily at cue boundaries creates tiny orphan blocks
    # when a sentence happens to cross a long subtitle pause.
    utterances: list[_SpeechPart] = []
    for part in all_parts:
        if not part.text:
            continue
        previous = utterances[-1] if utterances else None
        gap_ms = part.start_ms - previous.end_ms if previous is not None else None
        if (
            previous is not None
            and gap_ms is not None
            and previous.speaker_key == part.speaker_key
            and not _sentence_is_complete(previous.text)
            and -SAME_SPEAKER_OVERLAP_TOLERANCE_MS <= gap_ms <= continuation_threshold
            and gap_ms <= maximum_internal_gap
        ):
            combined = _combine_parts(previous, part)
            combined.formation_events.append(
                _event(
                    "reconstruct_unfinished_same_speaker",
                    "unfinished_same_speaker_reconstructed",
                    "Unfinished same-speaker utterance reconstructed across cues.",
                    measurements={
                        "gap_ms": gap_ms,
                        "continuation_threshold_ms": continuation_threshold,
                        "max_internal_gap_ms": maximum_internal_gap,
                        "previous_end_ms": previous.end_ms,
                        "current_start_ms": part.start_ms,
                    },
                    source_references=sorted({*previous.subtitles, *part.subtitles}),
                )
            )
            if gap_ms is not None and gap_ms < 0:
                combined.risk_flags.append("timing_overlap")
            utterances[-1] = combined
        else:
            if previous is None:
                part.boundary_before = _boundary(
                    "keep_boundary",
                    "document_start",
                    "Speech block starts the document.",
                    source_references=part.subtitles,
                )
            else:
                previous_refs = sorted(set(previous.subtitles))
                current_refs = sorted(set(part.subtitles))
                if previous.speaker_key != part.speaker_key:
                    reason_code = "speaker_boundary"
                    summary = "Speaker boundary retained."
                elif not _sentence_is_complete(previous.text) and gap_ms is not None:
                    if gap_ms > maximum_internal_gap:
                        reason_code = "internal_gap_limit"
                        summary = "Internal gap exceeded the reconstruction limit."
                        part.risk_flags.append("internal_gap_exceeded")
                    elif gap_ms > continuation_threshold:
                        reason_code = "continuation_gap_limit"
                        summary = "Continuation gap exceeded the reconstruction limit."
                        part.risk_flags.append("continuation_gap_exceeded")
                    elif gap_ms < -SAME_SPEAKER_OVERLAP_TOLERANCE_MS:
                        reason_code = "overlap_boundary"
                        summary = "Overlapping cues retained as separate blocks."
                        part.risk_flags.append("timing_overlap")
                    else:
                        reason_code = "unfinished_boundary"
                        summary = "Unfinished utterance boundary retained."
                else:
                    reason_code = "complete_utterance_boundary"
                    summary = "Complete utterance boundary retained."
                part.boundary_before = _boundary(
                    "keep_boundary",
                    reason_code,
                    summary,
                    measurements={
                        "gap_ms": gap_ms,
                        "continuation_threshold_ms": continuation_threshold,
                        "max_internal_gap_ms": maximum_internal_gap,
                        "previous_speaker": previous.speaker or None,
                        "current_speaker": part.speaker or None,
                    },
                    source_references=[*previous_refs, *current_refs],
                )
            utterances.append(part)

    # Split reconstructed utterances at balanced linguistic boundaries.  The
    # splitter observes ``min_chars`` where possible, avoiding the one- or
    # two-word tails produced by a hard greedy capacity cut.
    sized_parts = [
        part
        for utterance in utterances
        for part in _split_utterance(
            utterance,
            language_code,
            min_chars,
            max_chars,
            reviewed_speech=optimized_subtitles is not None,
        )
    ]

    # Finally pack nearby complete utterances when the ordinary timing rule
    # and the TTS size cap permit it.
    merged_parts: list[_SpeechPart] = []
    for part in sized_parts:
        if merged_parts and _should_merge_parts(
            merged_parts[-1],
            part,
            max_chars=max_chars,
            merge_threshold=merge_threshold,
            max_internal_gap_ms=maximum_internal_gap,
        ):
            previous = merged_parts[-1]
            gap_ms = part.start_ms - previous.end_ms
            combined = _combine_parts(previous, part)
            combined.formation_events.append(
                _event(
                    "pack_complete_utterances",
                    "nearby_complete_utterances_packed",
                    "Nearby complete utterances packed into one speech block.",
                    measurements={
                        "gap_ms": gap_ms,
                        "merge_threshold_ms": merge_threshold,
                        "display_length": len(combined.text),
                        "speech_length": len(combined.optimized_text),
                        "max_chars": max_chars,
                    },
                    source_references=sorted({*previous.subtitles, *part.subtitles}),
                )
            )
            merged_parts[-1] = combined
        else:
            merged_parts.append(part)

    return [
        block.to_dict()
        for block in _parts_to_blocks(
            merged_parts,
            include_optimized_text=optimized_subtitles is not None,
        )
    ]


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
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
    max_chars: int = 220,
    merge_threshold: int = 1500,
    *,
    continuation_threshold_ms: int | None = None,
    max_internal_gap_ms: int | None = None,
    speaker_by_subtitle: Mapping[int, str] | None = None,
    speech_srt_content: str | None = None,
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
        continuation_threshold_ms=continuation_threshold_ms,
        max_internal_gap_ms=max_internal_gap_ms,
        speaker_by_subtitle=speaker_by_subtitle,
        speech_srt_content=speech_srt_content,
    )

    output_path = session_path / f"{srt_path.stem}_speech_blocks.json"
    _write_json_atomic(output_path, blocks)
    logger.info("Generated %d speech block(s): %s", len(blocks), output_path)
    return str(output_path)
