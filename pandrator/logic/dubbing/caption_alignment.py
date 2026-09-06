"""Caption-authoritative CTC alignment for media-edit transcripts.

This module deliberately keeps the alignment contract independent from the web
worker.  The worker supplies a normalized WAV and the CrispASR executable; the
pure helpers below are also useful for validating evidence produced by a test
double or another compatible runtime.
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
import threading
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..cancellable_process import ProcessCancelled
from ..media_edit import MediaCue, MediaWord
from . import crispasr


class CaptionAlignmentError(RuntimeError):
    """Raised when a required CTC/VAD evidence step cannot be trusted."""


@dataclass(frozen=True)
class SpeechSpan:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("Speech spans must be positive half-open intervals")


@dataclass(frozen=True)
class AlignmentBatch:
    index: int
    cues: tuple[MediaCue, ...]
    start_ms: int
    end_ms: int

    @property
    def token_count(self) -> int:
        return sum(len(token_surfaces(cue.text)) for cue in self.cues)


@dataclass
class AlignmentDiagnostics:
    method: str = "ctc_cue_alignment"
    timing_quality_basis: str = "vad_midpoint_support_and_temporal_checks"
    first_pass_batch_count: int = 0
    overlap_cluster_count: int = 0
    oversized_cluster_count: int = 0
    oversized_cue_count: int = 0
    cluster_retries: int = 0
    individual_retries: int = 0
    failed_cue_ids: dict[str, list[str]] = field(default_factory=dict)
    outside_media_count: int = 0
    cue_count: int = 0
    word_count: int = 0
    accepted_cue_count: int = 0
    accepted_token_count: int = 0
    all_token_count: int = 0
    eligible_token_count: int = 0
    eligible_alignment_coverage: float = 0.0
    alignment_coverage: float = 0.0
    alignment_confidence: float = 0.0
    vad_enabled: bool = False
    vad_engine: str = "crispasr"
    vad_model: str = ""
    vad_options: dict[str, Any] = field(default_factory=dict)
    ctc_engine: str = "crispasr"
    ctc_model: str = "auto"
    ctc_options: dict[str, Any] = field(default_factory=dict)
    fallback_triggered: bool = False
    fallback_engine: str = ""
    fallback_filled_cue_count: int = 0
    fallback_filled_token_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "alignment_method": self.method,
            "timing_quality_basis": self.timing_quality_basis,
            "first_pass_batch_count": self.first_pass_batch_count,
            "overlap_cluster_count": self.overlap_cluster_count,
            "oversized_cluster_count": self.oversized_cluster_count,
            "oversized_cue_count": self.oversized_cue_count,
            "cluster_retries": self.cluster_retries,
            "individual_retries": self.individual_retries,
            "failed_cue_ids": {key: list(value) for key, value in self.failed_cue_ids.items()},
            "final_rejected_cue_count": len(self.failed_cue_ids),
            "final_rejected_cue_ids": list(self.failed_cue_ids),
            "outside_media_count": self.outside_media_count,
            "cue_count": self.cue_count,
            "word_count": self.word_count,
            "accepted_cue_count": self.accepted_cue_count,
            "accepted_token_count": self.accepted_token_count,
            "all_token_count": self.all_token_count,
            "eligible_token_count": self.eligible_token_count,
            "alignment_coverage": self.alignment_coverage,
            "eligible_alignment_coverage": self.eligible_alignment_coverage,
            "alignment_confidence": self.alignment_confidence,
            "confidence_is_timing_quality": True,
            "vad_enabled": self.vad_enabled,
            "vad_engine": self.vad_engine,
            "vad_model": self.vad_model,
            "vad_options": dict(self.vad_options),
            "ctc_engine": self.ctc_engine,
            "ctc_model": self.ctc_model,
            "ctc_options": dict(self.ctc_options),
            "fallback_triggered": self.fallback_triggered,
            "fallback_engine": self.fallback_engine,
            "fallback_filled_cue_count": self.fallback_filled_cue_count,
            "fallback_filled_token_count": self.fallback_filled_token_count,
        }


@dataclass(frozen=True)
class CaptionAlignmentResult:
    cues: tuple[MediaCue, ...]
    diagnostics: AlignmentDiagnostics
    duration_ms: int
    normalized_wav_path: str
    vad_path: str | None = None
    diagnostics_path: str | None = None

    @property
    def metrics(self) -> dict[str, Any]:
        return self.diagnostics.as_dict()


def _number(value: Any, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = _number(value, name="setting")
    except ValueError:
        result = default
    return max(low, min(high, result))


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    return max(low, min(high, result))


def normalize_alignment_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize the alignment boundary without rejecting old run snapshots."""

    raw = dict(settings or {})
    method = str(raw.get("caption_alignment_method") or "ctc").strip().lower()
    if method not in {"ctc", "ctc_asr_fallback", "asr"}:
        method = "ctc"
    return {
        **raw,
        "caption_alignment_method": method,
        "caption_alignment_ctc_model": str(
            raw.get("caption_alignment_ctc_model") or "auto"
        ).strip()
        or "auto",
        "caption_alignment_padding_ms": _bounded_int(
            raw.get("caption_alignment_padding_ms"), 2000, 250, 5000
        ),
        "caption_alignment_batch_seconds": _bounded_int(
            raw.get("caption_alignment_batch_seconds"), 30, 5, 60
        ),
        "caption_alignment_min_confidence": _bounded_float(
            raw.get("caption_alignment_min_confidence"), 0.5, 0.5, 1.0
        ),
        "caption_alignment_fallback_coverage": _bounded_float(
            raw.get("caption_alignment_fallback_coverage"), 0.9, 0.0, 1.0
        ),
    }


def token_surfaces(text: str) -> tuple[str, ...]:
    """Return exact non-empty caption token surfaces."""

    return tuple(token for token in str(text or "").split() if token.strip())


def token_key(token: str) -> str:
    import unicodedata

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", str(token)).casefold()
        if character.isalnum()
    )


def validate_normalized_wav(path: str | Path) -> int:
    """Validate the exact audio contract and return duration in milliseconds."""

    try:
        with wave.open(str(path), "rb") as source:
            params = source.getparams()
            if (
                params.nchannels != 1
                or params.sampwidth != 2
                or params.framerate != 16000
                or params.comptype != "NONE"
                or params.nframes <= 0
            ):
                raise CaptionAlignmentError(
                    "Caption alignment requires mono 16-bit 16 kHz PCM WAV audio."
                )
            return max(1, round(params.nframes * 1000 / params.framerate))
    except (OSError, wave.Error) as error:
        raise CaptionAlignmentError(f"Invalid normalized WAV: {path}") from error


def build_overlap_clusters(
    cues: Sequence[MediaCue], duration_ms: int
) -> tuple[tuple[tuple[MediaCue, ...], ...], tuple[MediaCue, ...]]:
    """Group strict temporal overlaps while retaining source cue ordering."""

    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    eligible: list[tuple[int, MediaCue]] = []
    outside: list[MediaCue] = []
    for index, cue in enumerate(cues):
        if cue.start_ms >= duration_ms:
            outside.append(cue)
        else:
            eligible.append((index, cue))
    chronological = sorted(eligible, key=lambda item: (item[1].start_ms, item[0]))
    clusters: list[list[tuple[int, MediaCue]]] = []
    running_end = -1
    for item in chronological:
        cue = item[1]
        if not clusters or cue.start_ms >= running_end:
            clusters.append([item])
            running_end = cue.end_ms
        else:
            clusters[-1].append(item)
            running_end = max(running_end, cue.end_ms)
    result = tuple(
        tuple(cue for _index, cue in sorted(cluster, key=lambda item: item[0]))
        for cluster in clusters
    )
    return result, tuple(outside)


def build_alignment_batches(
    clusters: Sequence[Sequence[MediaCue]],
    *,
    duration_ms: int,
    padding_ms: int = 2000,
    batch_seconds: int = 30,
    token_budget: int = 160,
) -> tuple[AlignmentBatch, ...]:
    """Combine bounded consecutive clusters without splitting a cluster.

    Oversized clusters are omitted.  The orchestration layer may retry their
    individual cues, but no CTC process is allowed to exceed either bound.
    """

    if padding_ms < 0 or batch_seconds <= 0 or token_budget <= 0:
        raise ValueError("invalid batch bounds")
    batches: list[AlignmentBatch] = []
    pending: list[MediaCue] = []
    pending_start = pending_end = 0

    def flush() -> None:
        nonlocal pending, pending_start, pending_end
        if pending:
            batches.append(
                AlignmentBatch(
                    len(batches) + 1,
                    tuple(pending),
                    max(0, pending_start - padding_ms),
                    min(duration_ms, pending_end + padding_ms),
                )
            )
        pending = []
        pending_start = pending_end = 0

    for cluster in clusters:
        current = tuple(cluster)
        if not current:
            continue
        cluster_start = min(cue.start_ms for cue in current)
        cluster_end = max(cue.end_ms for cue in current)
        cluster_tokens = sum(len(token_surfaces(cue.text)) for cue in current)
        padded_start = max(0, cluster_start - padding_ms)
        padded_end = min(duration_ms, cluster_end + padding_ms)
        if (
            cluster_tokens > token_budget
            or padded_end - padded_start > batch_seconds * 1000
        ):
            flush()
            continue
        if not pending:
            pending = list(current)
            pending_start, pending_end = cluster_start, cluster_end
            continue
        candidate_tokens = sum(len(token_surfaces(cue.text)) for cue in pending) + cluster_tokens
        candidate_start = max(0, pending_start - padding_ms)
        candidate_end = min(duration_ms, cluster_end + padding_ms)
        if (
            candidate_end - candidate_start <= batch_seconds * 1000
            and candidate_tokens <= token_budget
        ):
            pending.extend(current)
            pending_end = max(pending_end, cluster_end)
        else:
            flush()
            pending = list(current)
            pending_start, pending_end = cluster_start, cluster_end
    flush()
    return tuple(batches)


def _fits_alignment_limits(
    cues: Sequence[MediaCue],
    *,
    duration_ms: int,
    padding_ms: int,
    batch_seconds: int,
    token_budget: int = 160,
) -> bool:
    if not cues:
        return False
    start_ms = max(0, min(cue.start_ms for cue in cues) - padding_ms)
    end_ms = min(duration_ms, max(cue.end_ms for cue in cues) + padding_ms)
    return (
        end_ms > start_ms
        and end_ms - start_ms <= batch_seconds * 1000
        and sum(len(token_surfaces(cue.text)) for cue in cues) <= token_budget
    )


def _span_from_vad_item(item: dict[str, Any], sample_rate: int) -> SpeechSpan:
    if "start_sample" in item or "end_sample" in item or "start_sample_index" in item:
        start = _number(
            item.get("start_sample", item.get("start_sample_index")),
            name="start_sample",
        )
        end = _number(
            item.get("end_sample", item.get("end_sample_index")),
            name="end_sample",
        )
        start_ms = round(start * 1000 / sample_rate)
        end_ms = round(end * 1000 / sample_rate)
    elif (
        "start_centis" in item
        or "end_centis" in item
        or "start_centisecond" in item
        or "t0_cs" in item
        or "t1_cs" in item
    ):
        start_ms = round(
            _number(
                item.get(
                    "start_centis",
                    item.get("start_centisecond", item.get("t0_cs")),
                ),
                name="start_centis",
            )
            * 10
        )
        end_ms = round(
            _number(
                item.get(
                    "end_centis",
                    item.get("end_centisecond", item.get("t1_cs")),
                ),
                name="end_centis",
            )
            * 10
        )
    elif "start" in item or "end" in item:
        # CrispASR's raw export has used both sample indices and centiseconds
        # across releases; an explicit unit is preferred, otherwise default to
        # sample indices because the export is a 16 kHz signal.
        unit = str(item.get("unit") or item.get("time_unit") or "samples").lower()
        start_value = _number(item.get("start"), name="start")
        end_value = _number(item.get("end"), name="end")
        if unit in {"cs", "centisecond", "centiseconds", "10ms"}:
            start_ms, end_ms = round(start_value * 10), round(end_value * 10)
        elif unit in {"ms", "millisecond", "milliseconds"}:
            start_ms, end_ms = round(start_value), round(end_value)
        else:
            start_ms = round(start_value * 1000 / sample_rate)
            end_ms = round(end_value * 1000 / sample_rate)
    else:
        raise CaptionAlignmentError("VAD segment has no supported offsets")
    return SpeechSpan(start_ms, end_ms)


def parse_vad_export(
    payload_or_path: dict[str, Any] | list[Any] | str | Path,
    *,
    duration_ms: int,
) -> tuple[SpeechSpan, ...]:
    """Parse and validate CrispASR VAD raw schema version 1."""

    if isinstance(payload_or_path, (str, Path)):
        try:
            payload = json.loads(Path(payload_or_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CaptionAlignmentError("CrispASR VAD export is not valid JSON") from error
    else:
        payload = payload_or_path
    if not isinstance(payload, dict):
        raise CaptionAlignmentError("CrispASR VAD export must be an object")
    # CrispASR's raw export is nested so it can coexist with command
    # metadata.  Requiring this wrapper prevents ambiguous evidence from
    # being treated as a valid speech map.
    raw_vad = payload.get("crispasr_vad")
    if not isinstance(raw_vad, dict):
        raise CaptionAlignmentError("CrispASR VAD export has no crispasr_vad object")
    schema = raw_vad.get("version")
    if schema != 1 or raw_vad.get("kind") != "vad_segments":
        raise CaptionAlignmentError("Unsupported CrispASR VAD schema")
    sample_rate = raw_vad.get("sample_rate")
    try:
        sample_rate = int(sample_rate)
    except (TypeError, ValueError) as error:
        raise CaptionAlignmentError("CrispASR VAD sample rate is invalid") from error
    if sample_rate != 16000:
        raise CaptionAlignmentError("CrispASR VAD export must use 16 kHz")
    raw_segments = raw_vad.get("slices")
    if not isinstance(raw_segments, list):
        raise CaptionAlignmentError("CrispASR VAD export has no slices array")
    num_slices = raw_vad.get("num_slices")
    if num_slices is not None:
        try:
            if int(num_slices) != len(raw_segments):
                raise CaptionAlignmentError("CrispASR VAD slice count is inconsistent")
        except (TypeError, ValueError) as error:
            raise CaptionAlignmentError("CrispASR VAD slice count is invalid") from error
    spans: list[SpeechSpan] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            raise CaptionAlignmentError("CrispASR VAD segment is malformed")
        try:
            span = _span_from_vad_item(item, sample_rate)
        except (TypeError, ValueError) as error:
            raise CaptionAlignmentError("CrispASR VAD segment offsets are invalid") from error
        if span.start_ms >= duration_ms:
            continue
        spans.append(SpeechSpan(span.start_ms, min(duration_ms, span.end_ms)))
    spans.sort(key=lambda span: (span.start_ms, span.end_ms))
    merged: list[SpeechSpan] = []
    for span in spans:
        if merged and span.start_ms <= merged[-1].end_ms:
            merged[-1] = SpeechSpan(merged[-1].start_ms, max(merged[-1].end_ms, span.end_ms))
        else:
            merged.append(span)
    return tuple(merged)


def _midpoint_supported(midpoint: float, vad: Sequence[SpeechSpan], tolerance_ms: int = 120) -> bool:
    return any(span.start_ms - tolerance_ms <= midpoint <= span.end_ms + tolerance_ms for span in vad)


def parse_ctc_words(payload_or_path: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(payload_or_path, (str, Path)):
        try:
            payload = json.loads(Path(payload_or_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CaptionAlignmentError("CTC alignment output is not valid JSON") from error
    else:
        payload = payload_or_path
    if isinstance(payload, dict):
        payload = payload.get("words", payload.get("transcription"))
    if not isinstance(payload, list):
        raise CaptionAlignmentError("CTC alignment output must be a word array")
    words: list[dict[str, Any]] = []
    previous_start = -1.0
    for raw in payload:
        if not isinstance(raw, dict):
            raise CaptionAlignmentError("CTC output contains a malformed word")
        if "word" not in raw or not isinstance(raw.get("word"), str):
            raise CaptionAlignmentError("CTC output word is missing its word surface")
        text = raw["word"].strip()
        if not text:
            raise CaptionAlignmentError("CTC output contains an empty word")
        try:
            start = _number(raw.get("start"), name="word.start")
            end = _number(raw.get("end"), name="word.end")
        except (TypeError, ValueError) as error:
            raise CaptionAlignmentError("CTC word timestamps are invalid") from error
        if start < 0 or end <= start or start <= previous_start:
            raise CaptionAlignmentError("CTC word timestamps are not monotonic")
        words.append({"word": text, "start": start, "end": end})
        previous_start = start
    return tuple(words)


def _reject(cue: MediaCue, reason: str) -> tuple[MediaCue, str]:
    return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), reason


def validate_cue_words(
    cue: MediaCue,
    words: Sequence[dict[str, Any] | MediaWord],
    *,
    padding_ms: int,
    vad_spans: Sequence[SpeechSpan] = (),
    vad_enabled: bool = False,
    min_confidence: float = 0.5,
    duration_ms: int | None = None,
) -> tuple[MediaCue, str | None, float]:
    """Validate one cue and assign authoritative surfaces and timing quality."""

    surfaces = token_surfaces(cue.text)
    if len(words) != len(surfaces):
        return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "wrong_count", 0.0
    local_start, local_end = max(0, cue.start_ms - padding_ms), cue.end_ms + padding_ms
    if duration_ms is not None:
        local_end = min(local_end, duration_ms)
    parsed: list[MediaWord] = []
    previous_start = -1
    previous_end = -1
    for index, raw in enumerate(words):
        try:
            if isinstance(raw, MediaWord):
                start_ms, end_ms, observed = raw.start_ms, raw.end_ms, raw.text
            else:
                start_ms = round(_number(raw.get("start"), name="word.start") * 1000)
                end_ms = round(_number(raw.get("end"), name="word.end") * 1000)
                observed = str(raw.get("word", raw.get("text", ""))).strip()
            if not observed:
                return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "empty_word", 0.0
            if token_key(observed) != token_key(surfaces[index]):
                return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "wrong_surface", 0.0
            if start_ms < local_start or end_ms > local_end:
                return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "out_of_window", 0.0
            if end_ms - start_ms > 2500:
                return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "overlong_word", 0.0
            if (
                end_ms <= start_ms
                or start_ms <= previous_start
                or end_ms <= previous_end
            ):
                return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "nonmonotonic", 0.0
        except (AttributeError, TypeError, ValueError):
            return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "malformed_word", 0.0
        parsed.append(MediaWord(surfaces[index], start_ms, end_ms))
        previous_start = start_ms
        previous_end = end_ms
    if not parsed:
        return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "no_words", 0.0
    if vad_enabled:
        support = sum(
            _midpoint_supported((word.start_ms + word.end_ms) / 2, vad_spans)
            for word in parsed
        ) / len(parsed)
        if support < 0.5:
            return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "inadequate_speech_support", support
        quality = support
    else:
        # No VAD is evidence, not permission to call timing a probability.
        quality = 0.75
    if quality < min_confidence:
        return replace(cue, words=(), timing_confidence=0.0, timing_source="caption"), "below_min_confidence", quality
    accepted = replace(
        cue,
        start_ms=parsed[0].start_ms,
        end_ms=parsed[-1].end_ms,
        words=tuple(replace(word, confidence=quality) for word in parsed),
        timing_confidence=quality,
        timing_source="ctc_alignment",
    )
    return accepted, None, quality


def _record_failure(diagnostics: AlignmentDiagnostics, cue_id: str, reason: str) -> None:
    reasons = diagnostics.failed_cue_ids.setdefault(cue_id, [])
    if reason not in reasons:
        reasons.append(reason)


def map_ctc_words_to_cues(
    cues: Sequence[MediaCue],
    raw_words: Sequence[dict[str, Any]],
    *,
    clip_start_ms: int,
    padding_ms: int,
    vad_spans: Sequence[SpeechSpan],
    vad_enabled: bool,
    min_confidence: float,
    duration_ms: int | None = None,
    failure_reasons: dict[str, str] | None = None,
) -> tuple[MediaCue, ...]:
    """Map one bounded CTC output sequentially to cue token counts."""

    output: list[MediaCue] = []
    cursor = 0
    for cue in cues:
        count = len(token_surfaces(cue.text))
        segment = []
        for raw in raw_words[cursor : cursor + count]:
            segment.append(
                {
                    "word": raw["word"],
                    "start": float(raw["start"]) + clip_start_ms / 1000.0,
                    "end": float(raw["end"]) + clip_start_ms / 1000.0,
                }
            )
        cursor += count
        accepted, reason, _quality = validate_cue_words(
            cue,
            segment,
            padding_ms=padding_ms,
            vad_spans=vad_spans,
            vad_enabled=vad_enabled,
            min_confidence=min_confidence,
            duration_ms=duration_ms,
        )
        if reason and failure_reasons is not None:
            failure_reasons[cue.id] = reason
        output.append(accepted)
    if cursor != len(raw_words):
        raise CaptionAlignmentError("CTC output word count does not match caption tokens")
    return tuple(output)


def _write_wave_clip(source: Path, target: Path, start_ms: int, end_ms: int) -> None:
    with wave.open(str(source), "rb") as original:
        params = original.getparams()
        if params.nchannels != 1 or params.sampwidth != 2 or params.framerate != 16000:
            raise CaptionAlignmentError("CTC clip source is not normalized WAV")
        start_frame = max(0, round(start_ms * params.framerate / 1000))
        end_frame = min(params.nframes, round(end_ms * params.framerate / 1000))
        original.setpos(start_frame)
        frames = original.readframes(max(0, end_frame - start_frame))
    with wave.open(str(target), "wb") as clip:
        clip.setparams(params)
        clip.writeframes(frames)


def align_caption_cues(
    normalized_wav: str | Path,
    cues: Sequence[MediaCue],
    settings: dict[str, Any] | None = None,
    *,
    vad_spans: Sequence[SpeechSpan] | None = None,
    ctc_runner: Callable[[Path, Path, str, dict[str, Any], threading.Event | None], Any] | None = None,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float, str | None], None] | None = None,
) -> CaptionAlignmentResult:
    """Run bounded CTC alignment with cluster and cue isolation retries."""

    options = normalize_alignment_settings(settings)
    duration_ms = validate_normalized_wav(normalized_wav)
    clusters, outside = build_overlap_clusters(cues, duration_ms)
    bounded_clusters = tuple(
        cluster
        for cluster in clusters
        if _fits_alignment_limits(
            cluster,
            duration_ms=duration_ms,
            padding_ms=options["caption_alignment_padding_ms"],
            batch_seconds=options["caption_alignment_batch_seconds"],
        )
    )
    oversized_clusters = tuple(
        cluster for cluster in clusters if cluster not in bounded_clusters
    )
    batches = build_alignment_batches(
        bounded_clusters,
        duration_ms=duration_ms,
        padding_ms=options["caption_alignment_padding_ms"],
        batch_seconds=options["caption_alignment_batch_seconds"],
    )
    diagnostics = AlignmentDiagnostics(
        overlap_cluster_count=len(clusters),
        oversized_cluster_count=len(oversized_clusters),
        first_pass_batch_count=len(batches),
        outside_media_count=len(outside),
        cue_count=len(cues),
        all_token_count=sum(len(token_surfaces(cue.text)) for cue in cues),
        eligible_token_count=sum(len(token_surfaces(cue.text)) for cluster in clusters for cue in cluster),
        vad_enabled=vad_spans is not None,
        timing_quality_basis=(
            "vad_midpoint_support_and_temporal_checks"
            if vad_spans is not None
            else "conservative_temporal_span_checks_without_vad"
        ),
        ctc_model=options["caption_alignment_ctc_model"],
        ctc_options={
            key: options[key]
            for key in (
                "caption_alignment_method",
                "caption_alignment_ctc_model",
                "caption_alignment_padding_ms",
                "caption_alignment_batch_seconds",
                "caption_alignment_min_confidence",
                "caption_alignment_fallback_coverage",
            )
        },
    )
    accepted_by_id: dict[str, MediaCue] = {cue.id: replace(cue, words=(), timing_confidence=0.0, timing_source="caption") for cue in cues}
    for cue in outside:
        _record_failure(diagnostics, cue.id, "outside_media")
    for cluster in oversized_clusters:
        for cue in cluster:
            _record_failure(diagnostics, cue.id, "cluster_exceeds_ctc_limits")
    vad = tuple(vad_spans or ())
    runner = ctc_runner
    if runner is None:
        def runner(clip_path: Path, text_path: Path, output_path: str, run_settings: dict[str, Any], event: threading.Event | None):
            return crispasr.run_ctc_alignment(
                clip_path,
                text_path,
                output_path,
                run_settings,
                cancel_event=event,
            )
    with tempfile.TemporaryDirectory(prefix="pandrator-ctc-") as temporary:
        root = Path(temporary)

        def execute(
            selected_cues: Sequence[MediaCue],
            start_ms: int,
            end_ms: int,
            stem: str,
        ) -> tuple[MediaCue, ...]:
            clip = root / f"{stem}.wav"
            text = root / f"{stem}.txt"
            output = root / f"{stem}.json"
            _write_wave_clip(Path(normalized_wav), clip, start_ms, end_ms)
            text.write_text(
                " ".join(token for cue in selected_cues for token in token_surfaces(cue.text)),
                encoding="utf-8",
            )
            try:
                raw = runner(clip, text, str(output), options, cancel_event)
                failure_reasons: dict[str, str] = {}
                mapped = map_ctc_words_to_cues(
                    selected_cues,
                    parse_ctc_words(raw if raw is not None else output),
                    clip_start_ms=start_ms,
                    padding_ms=options["caption_alignment_padding_ms"],
                    vad_spans=vad,
                    vad_enabled=vad_spans is not None,
                    min_confidence=options["caption_alignment_min_confidence"],
                    duration_ms=duration_ms,
                    failure_reasons=failure_reasons,
                )
                for cue_id, reason in failure_reasons.items():
                    _record_failure(diagnostics, cue_id, reason)
                return mapped
            except ProcessCancelled:
                raise
            except (CaptionAlignmentError, OSError, ValueError, TypeError, subprocess.CalledProcessError) as error:
                for cue in selected_cues:
                    _record_failure(diagnostics, cue.id, str(error) or "ctc_failed")
                return tuple(
                    replace(cue, words=(), timing_confidence=0.0, timing_source="caption")
                    for cue in selected_cues
                )

        for index, batch in enumerate(batches, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
            if progress:
                progress(index / max(1, len(batches)), f"Aligning batch {index}/{len(batches)}")
            mapped = execute(batch.cues, batch.start_ms, batch.end_ms, f"batch-{index:04d}")
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
            for cue, candidate in zip(batch.cues, mapped, strict=True):
                accepted_by_id[cue.id] = candidate

        # Isolation retries prevent one bad overlap/window from poisoning
        # otherwise independent cues.
        for cluster in clusters:
            rejected = [cue for cue in cluster if not accepted_by_id[cue.id].words]
            if not rejected:
                continue
            if progress:
                progress(1.0, f"Retrying cluster {cluster[0].id}")
            cluster_fits = _fits_alignment_limits(
                cluster,
                duration_ms=duration_ms,
                padding_ms=options["caption_alignment_padding_ms"],
                batch_seconds=options["caption_alignment_batch_seconds"],
            )
            cluster_was_batched = cluster_fits and any(
                len(batch.cues) > len(cluster)
                and any(item.id == cluster[0].id for item in batch.cues)
                for batch in batches
            )
            if cluster_fits and (len(cluster) > 1 or cluster_was_batched):
                diagnostics.cluster_retries += 1
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelled("Caption alignment was canceled.")
            cluster_start = max(0, min(cue.start_ms for cue in cluster) - options["caption_alignment_padding_ms"])
            cluster_end = min(duration_ms, max(cue.end_ms for cue in cluster) + options["caption_alignment_padding_ms"])
            if cluster_fits and (len(cluster) > 1 or cluster_was_batched):
                retry_result = execute(cluster, cluster_start, cluster_end, f"retry-cluster-{cluster[0].id}")
                if cancel_event is not None and cancel_event.is_set():
                    raise ProcessCancelled("Caption alignment was canceled.")
                for cue, candidate in zip(cluster, retry_result, strict=True):
                    accepted_by_id[cue.id] = candidate
                rejected = [cue for cue in cluster if not accepted_by_id[cue.id].words]
            for cue in rejected:
                if cancel_event is not None and cancel_event.is_set():
                    raise ProcessCancelled("Caption alignment was canceled.")
                if not _fits_alignment_limits(
                    (cue,),
                    duration_ms=duration_ms,
                    padding_ms=options["caption_alignment_padding_ms"],
                    batch_seconds=options["caption_alignment_batch_seconds"],
                ):
                    diagnostics.oversized_cue_count += 1
                    _record_failure(diagnostics, cue.id, "cue_exceeds_ctc_limits")
                    continue
                if progress:
                    progress(1.0, f"Retrying cue {cue.id}")
                diagnostics.individual_retries += 1
                start_ms = max(0, cue.start_ms - options["caption_alignment_padding_ms"])
                end_ms = min(duration_ms, cue.end_ms + options["caption_alignment_padding_ms"])
                accepted_by_id[cue.id] = execute((cue,), start_ms, end_ms, f"retry-{cue.id}")[0]
                if cancel_event is not None and cancel_event.is_set():
                    raise ProcessCancelled("Caption alignment was canceled.")

    diagnostics.accepted_cue_count = sum(bool(cue.words) for cue in accepted_by_id.values())
    diagnostics.accepted_token_count = sum(len(cue.words) for cue in accepted_by_id.values())
    diagnostics.alignment_coverage = diagnostics.accepted_token_count / max(1, diagnostics.all_token_count)
    diagnostics.eligible_alignment_coverage = diagnostics.accepted_token_count / max(1, diagnostics.eligible_token_count)
    diagnostics.alignment_confidence = sum(float(cue.timing_confidence or 0) for cue in accepted_by_id.values()) / max(1, len(cues))
    diagnostics.word_count = diagnostics.accepted_token_count
    final_failed_ids = {
        cue.id for cue in accepted_by_id.values() if not cue.words
    }
    diagnostics.failed_cue_ids = {
        cue_id: reasons
        for cue_id, reasons in diagnostics.failed_cue_ids.items()
        if cue_id in final_failed_ids
    }
    ordered = tuple(accepted_by_id[cue.id] for cue in cues)
    return CaptionAlignmentResult(ordered, diagnostics, duration_ms, str(normalized_wav))


def write_diagnostics(result: CaptionAlignmentResult, path: str | Path) -> Path:
    target = Path(path)
    target.write_text(json.dumps(result.metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


# Stable descriptive aliases used by integrations and focused tests.
normalize_caption_alignment_settings = normalize_alignment_settings
cluster_overlapping_cues = build_overlap_clusters
plan_first_pass_batches = build_alignment_batches
parse_vad_segments = parse_vad_export
validate_ctc_words = validate_cue_words
VADSegment = SpeechSpan
CaptionAlignmentDiagnostics = AlignmentDiagnostics
align_media_captions = align_caption_cues
