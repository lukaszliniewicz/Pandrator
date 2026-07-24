"""Cheap, deterministic checks for raw generated speech takes.

The checks in this module deliberately run before any future normalization or
other post-processing.  They screen for suspicious signal properties; they do
not claim to measure pronunciation, prosody, or perceptual quality.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Sequence
from typing import Any

from pydub import AudioSegment


SCHEMA_VERSION = 1
VERIFICATION_MODE_OFF = "off"
VERIFICATION_MODE_SIGNAL = "signal"

_MIN_RMS_DBFS = -55.0
_CLIP_WARNING_FRACTION = 0.001
_CLIP_FAILURE_FRACTION = 0.02
_DC_OFFSET_WARNING = 0.05
_ACTIVE_TAIL_DBFS = -20.0
_MIN_DURATION_BASE_MS = 200
_MIN_DURATION_PER_UNIT_MS = 70
_MAX_DURATION_BASE_MS = 1500
_MAX_DURATION_PER_UNIT_MS = 750
_RUN_OUTLIER_MIN_SEGMENTS = 8
_RUN_OUTLIER_MIN_DELTA_DB = 4.0
_RUN_OUTLIER_MIN_ROBUST_Z = 6.0

_CJK_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\uac00-\ud7af]"
)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def verification_mode(settings: dict[str, Any] | None) -> str:
    value = str((settings or {}).get("audio_verification_mode") or VERIFICATION_MODE_OFF)
    normalized = value.strip().lower().replace("-", "_")
    return VERIFICATION_MODE_SIGNAL if normalized in {"signal", "screen", "flag"} else VERIFICATION_MODE_OFF


def _dbfs(amplitude: float) -> float | None:
    if amplitude <= 0 or not math.isfinite(amplitude):
        return None
    return 20.0 * math.log10(amplitude)


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _speech_unit_count(text: str) -> int:
    """Estimate vocal units without treating a complete CJK sentence as one word."""
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub(" ", text)
    return cjk_count + len(_WORD_RE.findall(non_cjk))


def _issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def analyze_audio(audio: AudioSegment, expected_text: str = "") -> dict[str, Any]:
    """Return raw signal measurements and conservative screening findings."""
    channels = max(1, int(audio.channels or 1))
    sample_width = max(1, int(audio.sample_width or 1))
    sample_rate = max(1, int(audio.frame_rate or 1))
    samples = audio.get_array_of_samples()
    sample_count = len(samples)
    frame_count = sample_count // channels
    duration_ms = int(len(audio))
    unit_count = _speech_unit_count(expected_text)
    issues: list[dict[str, str]] = []

    metrics: dict[str, Any] = {
        "duration_ms": duration_ms,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "sample_count": sample_count,
        "speech_units": unit_count,
    }
    if sample_count == 0 or frame_count == 0:
        issues.append(_issue("empty_audio", "error", "The speech service returned no audio samples."))
        return _result(metrics, issues)

    full_scale = float(1 << (sample_width * 8 - 1))
    clip_level = full_scale * 0.999
    clipped = sum(1 for sample in samples if abs(int(sample)) >= clip_level)
    rms = float(audio.rms) / full_scale
    peak = float(audio.max) / full_scale
    clip_fraction = clipped / sample_count
    dc_offset = max(
        abs(audio.get_dc_offset(channel))
        for channel in range(1, min(channels, 2) + 1)
    )
    tail_rms = float(audio[-min(10, duration_ms) :].rms) / full_scale
    rms_dbfs = _dbfs(rms)
    peak_dbfs = _dbfs(peak)
    tail_rms_dbfs = _dbfs(tail_rms)

    metrics.update(
        {
            "rms": _rounded(rms),
            "rms_dbfs": _rounded(rms_dbfs, 3),
            "peak": _rounded(peak),
            "peak_dbfs": _rounded(peak_dbfs, 3),
            "crest_factor": _rounded(peak / rms if rms > 0 else None, 3),
            "clipped_sample_fraction": _rounded(clip_fraction, 8),
            "dc_offset": _rounded(dc_offset),
            "tail_rms_dbfs": _rounded(tail_rms_dbfs, 3),
        }
    )

    if rms_dbfs is None or rms_dbfs < _MIN_RMS_DBFS:
        issues.append(_issue("near_silence", "error", "The generated take is silent or nearly silent."))
    if clip_fraction >= _CLIP_FAILURE_FRACTION:
        issues.append(
            _issue(
                "heavy_clipping",
                "error",
                f"{clip_fraction:.2%} of samples are at digital full scale.",
            )
        )
    elif clip_fraction >= _CLIP_WARNING_FRACTION:
        issues.append(
            _issue(
                "clipping",
                "warning",
                f"{clip_fraction:.2%} of samples are at digital full scale.",
            )
        )
    if dc_offset >= _DC_OFFSET_WARNING:
        issues.append(
            _issue(
                "dc_offset",
                "warning",
                f"The waveform has an unusually large DC offset ({dc_offset:.3f}).",
            )
        )
    if tail_rms_dbfs is not None and tail_rms_dbfs >= _ACTIVE_TAIL_DBFS:
        issues.append(
            _issue(
                "active_tail",
                "warning",
                "The final 10 ms remain loud, so the take may have been truncated.",
            )
        )

    if unit_count:
        minimum_duration = _MIN_DURATION_BASE_MS + unit_count * _MIN_DURATION_PER_UNIT_MS
        maximum_duration = _MAX_DURATION_BASE_MS + unit_count * _MAX_DURATION_PER_UNIT_MS
        metrics["expected_duration_range_ms"] = [minimum_duration, maximum_duration]
        if duration_ms < minimum_duration:
            issues.append(
                _issue(
                    "implausibly_short",
                    "warning",
                    f"The take is unusually short for approximately {unit_count} spoken units.",
                )
            )
        elif duration_ms > maximum_duration:
            issues.append(
                _issue(
                    "implausibly_long",
                    "warning",
                    f"The take is unusually long for approximately {unit_count} spoken units.",
                )
            )

    return _result(metrics, issues)


def verify_audio(
    audio: AudioSegment,
    expected_text: str,
    settings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Screen a take when the opt-in signal mode is enabled."""
    mode = verification_mode(settings)
    if mode == VERIFICATION_MODE_OFF:
        return None
    result = analyze_audio(audio, expected_text)
    result["mode"] = mode
    return result


def run_rms_outliers(rms_dbfs_values: Sequence[float | None]) -> dict[int, dict[str, float]]:
    """Find conspicuously loud takes relative to a sufficiently large peer run."""
    finite = [
        (index, float(value))
        for index, value in enumerate(rms_dbfs_values)
        if value is not None and math.isfinite(float(value))
    ]
    if len(finite) < _RUN_OUTLIER_MIN_SEGMENTS:
        return {}
    values = [value for _index, value in finite]
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    outliers: dict[int, dict[str, float]] = {}
    for index, value in finite:
        delta_db = value - median
        robust_z = math.inf if mad == 0 and delta_db > 0 else (0.6745 * delta_db / mad if mad else 0.0)
        if delta_db >= _RUN_OUTLIER_MIN_DELTA_DB and robust_z >= _RUN_OUTLIER_MIN_ROBUST_Z:
            outliers[index] = {
                "run_median_rms_dbfs": round(median, 3),
                "run_rms_delta_db": round(delta_db, 3),
                "run_rms_robust_z": round(robust_z, 3) if math.isfinite(robust_z) else 999.0,
            }
    return outliers


def add_run_rms_warning(result: dict[str, Any], detail: dict[str, float]) -> dict[str, Any]:
    """Return a copy of a result with one run-relative warning attached."""
    updated = {
        **result,
        "metrics": {**dict(result.get("metrics") or {}), **detail},
        "issues": [*list(result.get("issues") or [])],
    }
    if not any(item.get("code") == "run_rms_outlier" for item in updated["issues"]):
        updated["issues"].append(
            _issue(
                "run_rms_outlier",
                "warning",
                f"The take is {detail['run_rms_delta_db']:.1f} dB louder than the median take in this run.",
            )
        )
    if updated.get("status") == "passed":
        updated["status"] = "warning"
    return updated


def _result(metrics: dict[str, Any], issues: list[dict[str, str]]) -> dict[str, Any]:
    severities = {item["severity"] for item in issues}
    status = "failed" if "error" in severities else "warning" if issues else "passed"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": VERIFICATION_MODE_SIGNAL,
        "status": status,
        "issues": issues,
        "metrics": metrics,
        "scope": "raw_signal",
    }
