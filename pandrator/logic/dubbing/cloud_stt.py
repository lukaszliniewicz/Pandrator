"""Synchronous HTTP runtime for the supported cloud STT profile.

Only providers with a documented, real word-timing response belong here.  The
current implementation intentionally contains Azure Speech
MAI-Transcribe-1.5; adding another provider requires a separately documented
wire contract and a parser that never fabricates timing.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import tempfile
import wave
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .crispasr import CrispASRTranscriptionResult
from .languages import normalize_language_code
from .stt_provider_profiles import (
    AZURE_MAI_TRANSCRIBE_1_5_LOCALES,
    STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5,
    get_stt_provider_profile,
    is_cloud_stt_engine,
)
from .transcript_normalization import NormalizedTranscript, TimedSegment, TimedWord

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 600.0
DEFAULT_AZURE_STYLE = "readability"
DEFAULT_MAX_CHUNK_SECONDS = 90.0 * 60.0
DEFAULT_MIN_CHUNK_SECONDS = 5.0 * 60.0
DEFAULT_BOUNDARY_SEARCH_WINDOW_SECONDS = 5.0 * 60.0
DEFAULT_QUIET_RUN_SECONDS = 1.5
DEFAULT_RMS_WINDOW_MS = 250.0
AZURE_MAX_AUDIO_SECONDS = 2.0 * 60.0 * 60.0
MAX_ALLOWED_CHUNK_SECONDS = AZURE_MAX_AUDIO_SECONDS - 1.0
_AUTO_LANGUAGE_VALUES = {"", "auto", "automatic", "detect", "und", "unknown"}
_AZURE_SPEECH_RESOURCE_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cognitiveservices\.azure\.com"
)


class CloudSTTError(RuntimeError):
    """Base class for cloud STT configuration, transport, and response errors."""


class CloudSTTConfigurationError(CloudSTTError):
    """Raised when a cloud provider cannot be configured safely."""


class CloudSTTRequestError(CloudSTTError):
    """Raised when the remote service rejects or cannot receive a request."""


class CloudSTTResponseError(CloudSTTError):
    """Raised when a response has no usable real word timings."""


RequestFunc = Callable[..., Any]


@dataclass(frozen=True)
class CloudSTTChunk:
    """One half-open, sample-indexed WAV chunk planned for remote STT."""

    index: int
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class _WavInfo:
    channels: int
    sample_width: int
    sample_rate: int
    frame_count: int
    comptype: str
    compname: str

    @property
    def frame_width(self) -> int:
        return self.channels * self.sample_width


def _setting(settings: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = settings.get(name)
        if value is not None and value != "":
            return value
    return default


def _positive_setting(
    settings: Mapping[str, Any], names: tuple[str, ...], default: float, *, label: str
) -> float:
    raw = _setting(settings, *names, default=default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise CloudSTTConfigurationError(
            f"{label} must be a positive number."
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise CloudSTTConfigurationError(f"{label} must be a positive number.")
    return value


def _wav_info(audio_path: str | os.PathLike[str]) -> _WavInfo:
    """Read PCM WAV parameters without loading audio data into memory."""

    try:
        with wave.open(str(audio_path), "rb") as source:
            info = _WavInfo(
                channels=source.getnchannels(),
                sample_width=source.getsampwidth(),
                sample_rate=source.getframerate(),
                frame_count=source.getnframes(),
                comptype=source.getcomptype(),
                compname=source.getcompname(),
            )
    except (OSError, wave.Error) as error:
        raise CloudSTTConfigurationError(
            f"Cloud STT requires a readable PCM WAV file: {audio_path}"
        ) from error
    if info.channels <= 0 or info.sample_width <= 0 or info.sample_rate <= 0:
        raise CloudSTTConfigurationError(
            "PCM WAV has invalid channel, sample-width, or rate metadata."
        )
    if info.comptype != "NONE":
        raise CloudSTTConfigurationError(
            f"Cloud STT requires uncompressed PCM WAV audio, not {info.compname or info.comptype}."
        )
    if info.sample_width not in {1, 2, 3, 4}:
        raise CloudSTTConfigurationError(
            f"Cloud STT does not support {info.sample_width * 8}-bit PCM WAV analysis."
        )
    return info


@dataclass(frozen=True)
class _ChunkConfig:
    max_seconds: float
    min_seconds: float
    search_seconds: float
    quiet_run_seconds: float
    rms_window_ms: float


def _chunk_config(settings: Mapping[str, Any]) -> _ChunkConfig:
    max_seconds = _positive_setting(
        settings,
        ("stt_cloud_max_chunk_seconds",),
        DEFAULT_MAX_CHUNK_SECONDS,
        label="stt_cloud_max_chunk_seconds",
    )
    if max_seconds > MAX_ALLOWED_CHUNK_SECONDS:
        raise CloudSTTConfigurationError(
            "stt_cloud_max_chunk_seconds must be below Azure Speech's 2-hour limit."
        )
    # A short configured maximum is useful for tests and constrained services;
    # when no minimum is configured, scale the five-minute default down so a
    # valid final chunk remains possible.
    min_default = min(DEFAULT_MIN_CHUNK_SECONDS, max_seconds / 2.0)
    min_seconds = _positive_setting(
        settings,
        ("stt_cloud_min_chunk_seconds",),
        min_default,
        label="stt_cloud_min_chunk_seconds",
    )
    if min_seconds > max_seconds / 2.0:
        raise CloudSTTConfigurationError(
            "stt_cloud_min_chunk_seconds must not exceed half the maximum chunk length."
        )
    search_seconds = _positive_setting(
        settings,
        (
            "stt_cloud_boundary_search_window_seconds",
            "stt_cloud_boundary_window_seconds",
            "stt_cloud_chunk_search_seconds",
        ),
        DEFAULT_BOUNDARY_SEARCH_WINDOW_SECONDS,
        label="stt_cloud_boundary_search_window_seconds",
    )
    if max_seconds + search_seconds > MAX_ALLOWED_CHUNK_SECONDS:
        raise CloudSTTConfigurationError(
            "stt_cloud_max_chunk_seconds plus its boundary search window must remain below "
            "Azure Speech's 2-hour limit."
        )
    quiet_seconds_value = _setting(settings, "stt_cloud_quiet_run_seconds", default="")
    if quiet_seconds_value == "":
        quiet_ms_value = _setting(settings, "stt_cloud_min_silence_ms", default="")
        if quiet_ms_value != "":
            try:
                quiet_seconds_value = float(quiet_ms_value) / 1000.0
            except (TypeError, ValueError) as error:
                raise CloudSTTConfigurationError(
                    "stt_cloud_min_silence_ms must be a positive number."
                ) from error
    quiet_run_seconds = _positive_setting(
        {"stt_cloud_quiet_run_seconds": quiet_seconds_value},
        ("stt_cloud_quiet_run_seconds",),
        DEFAULT_QUIET_RUN_SECONDS,
        label="stt_cloud_quiet_run_seconds",
    )
    rms_window_ms = _positive_setting(
        settings,
        ("stt_cloud_rms_window_ms",),
        DEFAULT_RMS_WINDOW_MS,
        label="stt_cloud_rms_window_ms",
    )
    return _ChunkConfig(
        max_seconds=max_seconds,
        min_seconds=min_seconds,
        search_seconds=search_seconds,
        quiet_run_seconds=quiet_run_seconds,
        rms_window_ms=rms_window_ms,
    )


@dataclass(frozen=True)
class _RMSWindow:
    start_frame: int
    end_frame: int
    rms: float

    @property
    def midpoint(self) -> int:
        return self.start_frame + (self.end_frame - self.start_frame) // 2


def _rms(data: bytes, *, sample_width: int, channels: int) -> float:
    """Return RMS amplitude for one bounded PCM byte window."""

    frame_width = sample_width * channels
    sample_count = len(data) // sample_width
    if sample_count <= 0 or frame_width <= 0:
        return 0.0
    if sample_width == 1:
        total = sum((value - 128) ** 2 for value in data)
    elif sample_width in {2, 4}:
        code = "h" if sample_width == 2 else "i"
        values = struct.unpack(
            f"<{len(data) // sample_width}{code}", data[: sample_count * sample_width]
        )
        total = sum(value * value for value in values)
    else:
        total = 0
        for offset in range(0, sample_count * 3, 3):
            value = int.from_bytes(data[offset : offset + 3], "little", signed=False)
            if value & 0x800000:
                value -= 0x1000000
            total += value * value
    return math.sqrt(total / sample_count)


def _rms_windows(
    audio_path: str | os.PathLike[str],
    info: _WavInfo,
    start_frame: int,
    end_frame: int,
    window_frames: int,
) -> Iterator[_RMSWindow]:
    """Analyze a bounded search interval using bounded-memory streaming reads."""

    try:
        with wave.open(str(audio_path), "rb") as source:
            source.setpos(start_frame)
            cursor = start_frame
            while cursor < end_frame:
                requested = min(window_frames, end_frame - cursor)
                data = source.readframes(requested)
                if not data:
                    break
                actual = len(data) // info.frame_width
                if actual <= 0:
                    break
                actual_end = cursor + actual
                yield _RMSWindow(
                    cursor,
                    min(actual_end, end_frame),
                    _rms(data, sample_width=info.sample_width, channels=info.channels),
                )
                cursor = actual_end
    except (OSError, wave.Error) as error:
        raise CloudSTTConfigurationError(
            f"Could not stream PCM WAV for boundary analysis: {audio_path}"
        ) from error


def _choose_boundary(
    audio_path: str | os.PathLike[str],
    info: _WavInfo,
    *,
    start_frame: int,
    target_frame: int,
    min_end_frame: int,
    max_end_frame: int,
    config: _ChunkConfig,
) -> int:
    lower = max(
        min_end_frame, target_frame - round(config.search_seconds * info.sample_rate)
    )
    upper = min(
        max_end_frame, target_frame + round(config.search_seconds * info.sample_rate)
    )
    if upper <= lower:
        return max(lower, min(max_end_frame, target_frame))
    window_frames = max(1, round(info.sample_rate * config.rms_window_ms / 1000.0))
    quiet_limit = (1 << (8 * info.sample_width - 1)) * 0.02
    quiet_run_frames = max(1, round(config.quiet_run_seconds * info.sample_rate))
    best_run: tuple[int, int] | None = None
    lowest_window: _RMSWindow | None = None
    run_start: int | None = None
    run_end = 0

    def record_run() -> None:
        nonlocal best_run, run_start
        if run_start is None:
            return
        candidate = (run_start, run_end)
        if candidate[1] - candidate[0] < quiet_run_frames:
            return
        if best_run is None or (
            candidate[1] - candidate[0],
            -abs((candidate[0] + candidate[1]) / 2 - target_frame),
            -candidate[0],
        ) > (
            best_run[1] - best_run[0],
            -abs((best_run[0] + best_run[1]) / 2 - target_frame),
            -best_run[0],
        ):
            best_run = candidate

    windows_seen = False
    for window in _rms_windows(audio_path, info, lower, upper, window_frames):
        windows_seen = True
        if lowest_window is None or (
            window.rms,
            abs(window.midpoint - target_frame),
            window.start_frame,
        ) < (
            lowest_window.rms,
            abs(lowest_window.midpoint - target_frame),
            lowest_window.start_frame,
        ):
            lowest_window = window
        if window.rms <= quiet_limit:
            if run_start is None:
                run_start = window.start_frame
            run_end = window.end_frame
            continue
        record_run()
        run_start = None
    record_run()

    if best_run is not None:
        run_start, run_end = best_run
        candidate = run_start + (run_end - run_start) // 2
    elif lowest_window is not None:
        candidate = lowest_window.midpoint
    elif not windows_seen:
        return max(lower, min(upper, target_frame))
    else:  # Defensive fallback for a future iterator implementation.
        candidate = target_frame
    return max(lower, min(upper, candidate))


def plan_cloud_stt_chunks(
    audio_path: str | os.PathLike[str], settings: Mapping[str, Any]
) -> tuple[CloudSTTChunk, ...]:
    """Plan exact, non-overlapping PCM WAV chunks around quiet boundaries."""

    info = _wav_info(audio_path)
    config = _chunk_config(settings)
    max_frames = max(1, round(config.max_seconds * info.sample_rate))
    min_frames = max(1, round(config.min_seconds * info.sample_rate))
    if info.frame_count <= max_frames:
        return (
            CloudSTTChunk(
                index=1,
                start_frame=0,
                end_frame=info.frame_count,
                start_ms=0,
                end_ms=round(info.frame_count * 1000 / info.sample_rate),
            ),
        )

    chunks: list[CloudSTTChunk] = []
    start_frame = 0
    while info.frame_count - start_frame > max_frames:
        target = start_frame + max_frames
        min_end = start_frame + min_frames
        # A boundary may land on either side of the nominal target.  Reserve
        # the configured minimum for the final chunk, while retaining the
        # conservative Azure-safe upper bound validated above.
        max_end = min(
            info.frame_count - min_frames,
            target + round(config.search_seconds * info.sample_rate),
        )
        if max_end <= min_end:
            boundary = max_end
        else:
            boundary = _choose_boundary(
                audio_path,
                info,
                start_frame=start_frame,
                target_frame=target,
                min_end_frame=min_end,
                max_end_frame=max_end,
                config=config,
            )
        if boundary <= start_frame:
            boundary = min_end
        if info.frame_count - boundary < min_frames:
            boundary = info.frame_count - min_frames
        chunks.append(
            CloudSTTChunk(
                index=len(chunks) + 1,
                start_frame=start_frame,
                end_frame=boundary,
                start_ms=round(start_frame * 1000 / info.sample_rate),
                end_ms=round(boundary * 1000 / info.sample_rate),
            )
        )
        start_frame = boundary
    chunks.append(
        CloudSTTChunk(
            index=len(chunks) + 1,
            start_frame=start_frame,
            end_frame=info.frame_count,
            start_ms=round(start_frame * 1000 / info.sample_rate),
            end_ms=round(info.frame_count * 1000 / info.sample_rate),
        )
    )
    return tuple(chunks)


# Concise aliases for callers that treat this as generic WAV chunk planning.
plan_wav_chunks = plan_cloud_stt_chunks


def _profile_for(settings: Mapping[str, Any]) -> dict[str, Any]:
    engine = (
        str(_setting(settings, "stt_engine", "stt_backend", "service", default=""))
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    profile = get_stt_provider_profile(engine)
    if profile is None:
        if engine and not is_cloud_stt_engine(engine):
            raise CloudSTTConfigurationError(
                f"Unsupported cloud STT engine '{engine}'. Select a supported cloud profile."
            )
        raise CloudSTTConfigurationError("A cloud STT engine must be configured.")
    if profile["id"] != STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5:
        raise CloudSTTConfigurationError(
            f"Cloud STT profile '{profile['id']}' has no documented timed-word runtime."
        )
    provider_configs = settings.get("provider_configs") or []
    configured = next(
        (
            item
            for item in provider_configs
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip().lower().replace("-", "_") == engine
        ),
        None,
    )
    merged = {**profile, **dict(configured or {})}
    for key in (
        "id",
        "engine",
        "provider",
        "adapter",
        "path",
        "transcription_path",
        "api_key_env",
        "auth_mode",
        "auth_header_mode",
        "auth_scheme",
        "auth_header",
        "auth",
    ):
        merged[key] = deepcopy(profile[key])
    return merged


def normalize_azure_speech_api_base(value: Any) -> str:
    """Validate and canonicalize a public Azure Speech resource origin."""

    configured = str(value or "").strip().rstrip("/")
    if not configured or "your-resource-name" in configured.lower():
        raise CloudSTTConfigurationError(
            "Azure Speech requires a resource endpoint in stt_api_base "
            "(for example, https://RESOURCE-NAME.cognitiveservices.azure.com)."
        )
    try:
        parsed = urlsplit(configured)
        port = parsed.port
    except ValueError as error:
        raise CloudSTTConfigurationError(
            "Azure Speech base URL is not a valid HTTPS resource endpoint."
        ) from error
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or _AZURE_SPEECH_RESOURCE_HOST.fullmatch(hostname) is None
    ):
        raise CloudSTTConfigurationError(
            "Azure Speech base URL must be an HTTPS origin of the form "
            "https://RESOURCE-NAME.cognitiveservices.azure.com."
        )
    return f"https://{hostname}"


def _api_base(settings: Mapping[str, Any], profile: Mapping[str, Any]) -> str:
    configured = (
        str(
            _setting(
                settings,
                "stt_api_base",
                "stt_api_base_url",
                "stt_base_url",
                "cloud_stt_base_url",
                "api_base",
                "base_url",
                default=profile.get("api_base", ""),
            )
            or ""
        )
        .strip()
        .rstrip("/")
    )
    return normalize_azure_speech_api_base(configured)


def _api_key(settings: Mapping[str, Any], profile: Mapping[str, Any]) -> str:
    direct = str(
        _setting(
            settings,
            "stt_api_key",
            "cloud_stt_api_key",
            "api_key",
            default=profile.get("api_key", ""),
        )
        or ""
    ).strip()
    if direct:
        return direct
    builtin_profile = get_stt_provider_profile(str(profile.get("id") or "")) or {}
    env_name = str(builtin_profile.get("api_key_env") or "").strip()
    value = os.environ.get(env_name, "").strip() if env_name else ""
    if not value:
        raise CloudSTTConfigurationError(
            f"Missing Azure Speech credential. Set {env_name} or configure stt_api_key."
        )
    return value


def _language_locale(language: Any) -> str | None:
    raw = str(language or "").strip()
    if raw.casefold() in _AUTO_LANGUAGE_VALUES:
        return None
    lowered = raw.replace("_", "-").casefold()
    for locale in AZURE_MAI_TRANSCRIBE_1_5_LOCALES:
        if lowered == locale.casefold():
            return locale
    language_code = normalize_language_code(raw, default="").casefold()
    if language_code in _AUTO_LANGUAGE_VALUES:
        return None
    for locale in AZURE_MAI_TRANSCRIBE_1_5_LOCALES:
        if locale.casefold().split("-", 1)[0] == language_code:
            return locale
    raise CloudSTTConfigurationError(
        f"Azure Speech MAI-Transcribe-1.5 does not support the configured language '{raw}'."
    )


def _split_hotwords(value: Any) -> list[str]:
    raw = str(value or "")
    values: list[str] = []
    for item in raw.replace("\r", "\n").replace(",", "\n").split("\n"):
        phrase = item.strip()
        if phrase and phrase not in values:
            values.append(phrase)
    return values


def build_azure_definition(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Build the Azure multipart ``definition`` object from Pandrator settings."""

    definition: dict[str, Any] = {
        "enhancedMode": {
            "enabled": True,
            "model": "mai-transcribe-1.5",
        }
    }
    locale = _language_locale(
        _setting(settings, "stt_language", "whisper_language", default="auto")
    )
    if locale is not None:
        definition["locales"] = [locale]
    hotwords = _split_hotwords(_setting(settings, "stt_hotwords", default=""))
    if hotwords:
        definition["phraseList"] = {"phrases": hotwords}
    style = (
        str(
            _setting(settings, "stt_transcribe_style", default=DEFAULT_AZURE_STYLE)
            or ""
        )
        .strip()
        .lower()
    )
    if style == "verbatim":
        definition["enhancedMode"]["transcribeStyle"] = "verbatim"
    elif style not in {"", "readability"}:
        raise CloudSTTConfigurationError(
            "stt_transcribe_style must be 'readability' or 'verbatim'."
        )
    diarization = bool(
        _setting(
            settings,
            "diarization_enabled",
            "stt_diarization_enabled",
            "diarize",
            default=False,
        )
    )
    if diarization:
        raise CloudSTTConfigurationError(
            "Azure Speech MAI-Transcribe-1.5 cloud transcription does not support diarization."
        )
    return definition


def _number(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CloudSTTResponseError(
            f"Azure response has a non-numeric {field}."
        ) from error
    if not math.isfinite(number) or number < 0:
        raise CloudSTTResponseError(f"Azure response has an invalid {field}.")
    return number


def _azure_span(item: Mapping[str, Any], *, context: str) -> tuple[int, int] | None:
    offset = item.get("offsetMilliseconds")
    duration = item.get("durationMilliseconds")
    if offset is not None or duration is not None:
        if offset is None or duration is None:
            raise CloudSTTResponseError(
                f"Azure response {context} has an incomplete timed span."
            )
        start = round(_number(offset, field=f"{context} offsetMilliseconds"))
        end = start + round(_number(duration, field=f"{context} durationMilliseconds"))
        if end <= start:
            raise CloudSTTResponseError(
                f"Azure response {context} has a non-positive duration."
            )
        return start, end
    start_value = item.get("startMilliseconds", item.get("start_ms"))
    end_value = item.get("endMilliseconds", item.get("end_ms"))
    if start_value is None and end_value is None:
        return None
    if start_value is None or end_value is None:
        raise CloudSTTResponseError(
            f"Azure response {context} has an incomplete timed span."
        )
    start = round(_number(start_value, field=f"{context} startMilliseconds"))
    end = round(_number(end_value, field=f"{context} endMilliseconds"))
    if end <= start:
        raise CloudSTTResponseError(
            f"Azure response {context} has a non-positive duration."
        )
    return start, end


def _azure_word(
    item: Mapping[str, Any], *, segment_index: int, word_index: int
) -> TimedWord:
    text = str(item.get("word") or item.get("text") or "").strip()
    if not text:
        raise CloudSTTResponseError(
            f"Azure response phrase {segment_index + 1} word {word_index + 1} has no text."
        )
    span = _azure_span(
        item, context=f"phrase {segment_index + 1} word {word_index + 1}"
    )
    if span is None:
        raise CloudSTTResponseError(
            f"Azure response phrase {segment_index + 1} word {word_index + 1} has no timing."
        )
    excluded = {
        "word",
        "text",
        "offsetMilliseconds",
        "durationMilliseconds",
        "startMilliseconds",
        "endMilliseconds",
    }
    metadata = {
        key: deepcopy(value) for key, value in item.items() if key not in excluded
    }
    confidence = item.get("confidence", item.get("probability"))
    try:
        normalized_confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        normalized_confidence = None
    return TimedWord(
        text=text,
        start_ms=span[0],
        end_ms=span[1],
        confidence=normalized_confidence,
        metadata=metadata,
    )


def parse_azure_response(payload: Mapping[str, Any]) -> NormalizedTranscript:
    """Convert Azure ``phrases`` output to the canonical transcript model."""

    if not isinstance(payload, Mapping):
        raise CloudSTTResponseError("Azure Speech returned a non-object JSON response.")
    phrases = payload.get("phrases")
    if phrases is None:
        text = str(payload.get("text") or payload.get("displayText") or "").strip()
        if text:
            raise CloudSTTResponseError(
                "Azure Speech returned text but no timed words."
            )
        raise CloudSTTResponseError(
            "Azure Speech response did not contain phrases or timed words."
        )
    if not isinstance(phrases, list):
        raise CloudSTTResponseError(
            "Azure Speech response contained malformed phrases."
        )

    segments: list[TimedSegment] = []
    for segment_index, phrase in enumerate(phrases):
        if not isinstance(phrase, Mapping):
            raise CloudSTTResponseError(
                f"Azure response phrase {segment_index + 1} is malformed."
            )
        phrase_text = str(phrase.get("text") or phrase.get("phrase") or "").strip()
        raw_words = phrase.get("words")
        if raw_words is None:
            if phrase_text:
                raise CloudSTTResponseError(
                    f"Azure response phrase {segment_index + 1} contained text but no timed words."
                )
            continue
        if not isinstance(raw_words, list):
            raise CloudSTTResponseError(
                f"Azure response phrase {segment_index + 1} has malformed words."
            )
        words = tuple(
            _azure_word(word, segment_index=segment_index, word_index=word_index)
            for word_index, word in enumerate(raw_words)
            if isinstance(word, Mapping)
        )
        if len(words) != len(raw_words):
            raise CloudSTTResponseError(
                f"Azure response phrase {segment_index + 1} has malformed words."
            )
        if not words:
            if phrase_text:
                raise CloudSTTResponseError(
                    f"Azure response phrase {segment_index + 1} contained text but no timed words."
                )
            continue
        phrase_span = _azure_span(phrase, context=f"phrase {segment_index + 1}")
        span = phrase_span or (words[0].start_ms, max(word.end_ms for word in words))
        metadata = {
            key: deepcopy(value)
            for key, value in phrase.items()
            if key
            not in {
                "text",
                "phrase",
                "words",
                "offsetMilliseconds",
                "durationMilliseconds",
                "startMilliseconds",
                "endMilliseconds",
            }
        }
        segments.append(
            TimedSegment(
                text=phrase_text or " ".join(word.text for word in words),
                start_ms=span[0],
                end_ms=span[1],
                identifier=str(
                    phrase.get("id")
                    or phrase.get("phraseId")
                    or f"azure-{segment_index + 1}"
                ),
                words=words,
                metadata=metadata,
            )
        )
    if not segments or not any(segment.words for segment in segments):
        raise CloudSTTResponseError("Azure Speech returned no real timed words.")
    top_metadata = {
        key: deepcopy(value)
        for key, value in payload.items()
        if key not in {"phrases", "text", "displayText"}
    }
    language = str(payload.get("locale") or payload.get("language") or "").strip()
    return NormalizedTranscript(
        segments=tuple(sorted(segments, key=lambda item: (item.start_ms, item.end_ms))),
        source_format="azure-speech-fast-transcription",
        language=language,
        metadata={
            "provider": "azure",
            "adapter": "azure_speech_fast_transcription",
            "engine": STT_ENGINE_AZURE_MAI_TRANSCRIBE_1_5,
            "model": "mai-transcribe-1.5",
            "remote": True,
            "response": top_metadata,
        },
    )


def _response_json(response: Any) -> Mapping[str, Any]:
    status_code = getattr(response, "status_code", 200)
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 200
    if status >= 400:
        detail = str(getattr(response, "text", "") or "").strip()
        raise CloudSTTRequestError(
            f"Azure Speech request failed with HTTP {status}."
            + (f" {detail[:500]}" if detail else "")
        )
    try:
        parser = response.json
        payload = parser() if callable(parser) else parser
    except (ValueError, TypeError, AttributeError) as error:
        raise CloudSTTResponseError("Azure Speech returned invalid JSON.") from error
    if not isinstance(payload, Mapping):
        raise CloudSTTResponseError("Azure Speech returned a non-object JSON response.")
    return payload


def _request_function(
    request_func: RequestFunc | None, session: Any | None
) -> RequestFunc:
    if request_func is not None:
        return request_func
    if session is not None and callable(getattr(session, "post", None)):
        return session.post
    return requests.post


def _write_wav_chunk(
    source_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    info: _WavInfo,
    chunk: CloudSTTChunk,
) -> None:
    """Copy one exact frame interval while retaining the source WAV format."""

    remaining = chunk.frame_count
    try:
        with (
            wave.open(str(source_path), "rb") as source,
            wave.open(str(destination), "wb") as target,
        ):
            source.setpos(chunk.start_frame)
            target.setnchannels(info.channels)
            target.setsampwidth(info.sample_width)
            target.setframerate(info.sample_rate)
            target.setcomptype("NONE", "not compressed")
            while remaining:
                data = source.readframes(min(65536, remaining))
                if not data:
                    break
                actual = len(data) // info.frame_width
                if actual <= 0:
                    break
                target.writeframesraw(data)
                remaining -= actual
    except (OSError, wave.Error) as error:
        raise CloudSTTConfigurationError(
            f"Could not write temporary cloud STT WAV chunk: {destination}"
        ) from error
    if remaining:
        raise CloudSTTConfigurationError(
            f"Source WAV ended while writing chunk {chunk.index}."
        )


def _submit_azure(
    audio_path: Path,
    *,
    url: str,
    headers: Mapping[str, str],
    definition: Mapping[str, Any],
    timeout: float,
    request_func: RequestFunc | None,
    session: Any | None,
) -> NormalizedTranscript:
    """Submit one WAV stream and parse its response before returning."""

    try:
        with audio_path.open("rb") as audio_stream:
            files = {
                "audio": (audio_path.name, audio_stream, "audio/wav"),
                "definition": (
                    None,
                    json.dumps(definition, ensure_ascii=False),
                    "application/json",
                ),
            }
            response = _request_function(request_func, session)(
                url,
                headers=dict(headers),
                files=files,
                timeout=timeout,
            )
    except (requests.RequestException, OSError) as error:
        raise CloudSTTRequestError(f"Azure Speech request failed: {error}") from error
    except Exception as error:
        # Injected request functions may use a custom transport exception type;
        # preserve a typed runtime error at this boundary.
        raise CloudSTTRequestError(f"Azure Speech request failed: {error}") from error
    return parse_azure_response(_response_json(response))


def _rebase_transcript(
    transcript: NormalizedTranscript, chunk: CloudSTTChunk
) -> NormalizedTranscript:
    """Offset one parsed response and prefix identifiers with its chunk index."""

    segments: list[TimedSegment] = []
    for segment_index, segment in enumerate(transcript.segments, start=1):
        words = tuple(
            TimedWord(
                text=word.text,
                start_ms=word.start_ms + chunk.start_ms,
                end_ms=word.end_ms + chunk.start_ms,
                speaker=word.speaker,
                confidence=word.confidence,
                metadata={
                    **deepcopy(word.metadata),
                    "chunk_index": chunk.index,
                    "chunk_start_frame": chunk.start_frame,
                },
            )
            for word in segment.words
        )
        segment_id = segment.identifier or f"segment-{segment_index}"
        segments.append(
            TimedSegment(
                text=segment.text,
                start_ms=segment.start_ms + chunk.start_ms,
                end_ms=segment.end_ms + chunk.start_ms,
                speaker=segment.speaker,
                identifier=f"chunk-{chunk.index:04d}-{segment_id}",
                words=words,
                metadata={
                    **deepcopy(segment.metadata),
                    "chunk_index": chunk.index,
                    "chunk_start_frame": chunk.start_frame,
                },
            )
        )
    return NormalizedTranscript(
        segments=tuple(segments),
        source_format=transcript.source_format,
        language=transcript.language,
        metadata=deepcopy(transcript.metadata),
    )


def _merge_chunk_transcripts(
    transcripts: list[NormalizedTranscript],
    chunks: tuple[CloudSTTChunk, ...],
    info: _WavInfo,
) -> NormalizedTranscript:
    merged_segments = tuple(
        sorted(
            (segment for transcript in transcripts for segment in transcript.segments),
            key=lambda segment: (segment.start_ms, segment.end_ms),
        )
    )
    metadata = deepcopy(transcripts[0].metadata) if transcripts else {}
    metadata["chunk_count"] = len(chunks)
    metadata["chunk_boundaries"] = [
        {
            "index": chunk.index,
            "start_frame": chunk.start_frame,
            "end_frame": chunk.end_frame,
            "start_ms": chunk.start_ms,
            "end_ms": chunk.end_ms,
        }
        for chunk in chunks
    ]
    metadata["audio_sample_rate"] = info.sample_rate
    metadata["audio_channels"] = info.channels
    metadata["audio_sample_width"] = info.sample_width
    if len(transcripts) > 1:
        metadata["chunk_responses"] = [
            deepcopy(transcript.metadata) for transcript in transcripts
        ]
    return NormalizedTranscript(
        segments=merged_segments,
        source_format=transcripts[0].source_format
        if transcripts
        else "azure-speech-fast-transcription",
        language=next(
            (transcript.language for transcript in transcripts if transcript.language),
            "",
        ),
        metadata=metadata,
    )


def transcribe(
    audio_path: str | os.PathLike[str],
    *,
    session_dir: str | os.PathLike[str],
    output_name: str,
    settings: Mapping[str, Any],
    request_func: RequestFunc | None = None,
    session: Any | None = None,
) -> CrispASRTranscriptionResult:
    """Transcribe one normalized WAV file through Azure Speech."""

    profile = _profile_for(settings)
    if not is_cloud_stt_engine(profile["id"]):
        raise CloudSTTConfigurationError(
            f"Unsupported cloud STT profile '{profile['id']}'."
        )
    audio = Path(audio_path)
    if not audio.is_file():
        raise CloudSTTConfigurationError(
            f"Normalized audio file does not exist: {audio}"
        )
    session_path = Path(session_dir)
    try:
        session_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CloudSTTConfigurationError(
            f"Cloud STT session directory is not writable: {session_path}"
        ) from error
    info = _wav_info(audio)
    chunks = plan_cloud_stt_chunks(audio, settings)
    definition = build_azure_definition(settings)
    url = urljoin(f"{_api_base(settings, profile)}/", str(profile["path"]).lstrip("/"))
    headers = {"Ocp-Apim-Subscription-Key": _api_key(settings, profile)}
    try:
        timeout = float(
            _setting(
                settings,
                "stt_request_timeout_seconds",
                "cloud_stt_timeout_seconds",
                default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
        )
    except (TypeError, ValueError) as error:
        raise CloudSTTConfigurationError(
            "stt_request_timeout_seconds must be a positive number."
        ) from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise CloudSTTConfigurationError(
            "stt_request_timeout_seconds must be a positive number."
        )
    parsed_chunks: list[NormalizedTranscript] = []
    with tempfile.TemporaryDirectory(
        prefix="cloud-stt-", dir=str(session_path)
    ) as temp_dir:
        temporary_root = Path(temp_dir)
        for chunk in chunks:
            chunk_path = audio
            if len(chunks) > 1:
                chunk_path = temporary_root / f"chunk-{chunk.index:04d}.wav"
                _write_wav_chunk(audio, chunk_path, info, chunk)
            parsed = _submit_azure(
                chunk_path,
                url=url,
                headers=headers,
                definition=definition,
                timeout=timeout,
                request_func=request_func,
                session=session,
            )
            parsed_chunks.append(_rebase_transcript(parsed, chunk))
    transcript = _merge_chunk_transcripts(parsed_chunks, chunks, info)

    words_path = session_path / f"{output_name}_words.json"
    srt_path = session_path / f"{output_name}.srt"
    words_path.write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # The central transcription orchestrator composes readable SRT from this
    # canonical word file.  Keep a concrete placeholder for callers that use
    # the cloud runtime directly.
    srt_path.write_text("", encoding="utf-8")
    return CrispASRTranscriptionResult(
        srt_path=str(srt_path),
        word_timestamps_path=str(words_path),
        engine=profile["id"],
        compute_backend="remote",
    )


# Private aliases make the adapter easy to exercise without making callers
# depend on those names; the public functions above remain the stable API.
_build_azure_definition = build_azure_definition
_parse_azure_response = parse_azure_response


__all__ = [
    "CloudSTTChunk",
    "CloudSTTConfigurationError",
    "CloudSTTError",
    "CloudSTTRequestError",
    "CloudSTTResponseError",
    "build_azure_definition",
    "parse_azure_response",
    "plan_cloud_stt_chunks",
    "plan_wav_chunks",
    "transcribe",
]
