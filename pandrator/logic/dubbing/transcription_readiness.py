"""Read-only assessment of the selected local transcription model files."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, Mapping

from . import crispasr, crispasr_qwen_assets, qwen_alignment, qwen_asr
from .stt_backends import STT_ENGINE_AUTO, normalize_stt_backend
from .stt_provider_profiles import CLOUD_STT_ENGINE_IDS

_VAD_ALIASES = {
    "": "silero",
    "auto": "silero",
    "default": "silero",
    "silero": "silero",
    "ggml-silero-v6.2.0.bin": "silero",
    "firered": "firered",
    "firered-vad": "firered",
    "firered-vad.gguf": "firered",
    "marblenet": "marblenet",
    "marblenet-vad": "marblenet",
    "marblenet-vad.gguf": "marblenet",
    "whisper-vad": "whisper-vad",
    "whisper-vad-asmr-q4_k.gguf": "whisper-vad",
    "webrtc": "webrtc",
}
_MOSS_CANARY_ALIASES = {
    "auto",
    "canary-ctc-aligner",
    crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT.filename,
}
_KNOWN_AUTO_ENGINES = {"", "auto", "automatic", "parakeet_preferred"}
def _file_asset(
    kind: str,
    asset_id: str,
    path: str | Path,
    *,
    pinned_size: int | None = None,
) -> dict[str, str]:
    """Check only file metadata; never read or hash file contents."""

    try:
        file_stat = Path(path).expanduser().stat()
    except FileNotFoundError:
        return {
            "kind": kind,
            "id": asset_id,
            "status": "missing",
            "verification": "not_found",
        }
    except (OSError, RuntimeError, ValueError):
        return {
            "kind": kind,
            "id": asset_id,
            "status": "unresolved",
            "verification": "unavailable",
        }

    if not stat.S_ISREG(file_stat.st_mode):
        return {
            "kind": kind,
            "id": asset_id,
            "status": "missing",
            "verification": "not_regular_file",
        }
    if file_stat.st_size <= 0:
        return {
            "kind": kind,
            "id": asset_id,
            "status": "missing",
            "verification": "empty",
        }
    if pinned_size is not None and file_stat.st_size != pinned_size:
        return {
            "kind": kind,
            "id": asset_id,
            "status": "unresolved",
            "verification": "pinned_size_mismatch",
        }
    return {
        "kind": kind,
        "id": asset_id,
        "status": "cached",
        "verification": (
            "pinned_size_pending_hash" if pinned_size is not None else "nonempty"
        ),
    }


def _crisp_cached_asset(
    settings: dict[str, Any], kind: str, asset_id: str, artifact: crispasr.CrispASRArtifact
) -> dict[str, str]:
    try:
        configured_cache = crispasr._cache_dir(settings)
        cache_dir = (
            Path(configured_cache).expanduser()
            if configured_cache
            else Path.home() / ".cache" / "crispasr"
        )
        candidate = cache_dir / artifact.filename
        cached = crispasr._cached_artifact_path(settings, artifact)
    except (OSError, RuntimeError, ValueError):
        return {
            "kind": kind,
            "id": asset_id,
            "status": "unresolved",
            "verification": "unavailable",
        }
    if cached is not None:
        return {
            "kind": kind,
            "id": asset_id,
            "status": "cached",
            "verification": "nonempty",
        }
    return _file_asset(kind, asset_id, candidate)


def _asset_status(assets: list[dict[str, str]]) -> tuple[str, bool | None]:
    statuses = {asset["status"] for asset in assets}
    if "missing" in statuses:
        return "missing", False
    if "unresolved" in statuses:
        return "unresolved", None
    if statuses == {"not_required"}:
        return "not_required", True
    return "cached", True


def _not_required(kind: str, asset_id: str, verification: str) -> dict[str, str]:
    return {
        "kind": kind,
        "id": asset_id,
        "status": "not_required",
        "verification": verification,
    }


def _named_vad_id(value: str) -> str | None:
    return _VAD_ALIASES.get(value.strip().lower())


def is_named_vad_selection(value: object) -> bool:
    """Return whether a VAD selection is a fixed alias rather than a path."""

    return isinstance(value, str) and _named_vad_id(value) is not None


def _vad_asset(settings: dict[str, Any], enabled: bool) -> dict[str, str]:
    selected = str(settings.get("crispasr_vad_model") or "silero").strip()
    normalized = _named_vad_id(selected)
    if not enabled:
        return _not_required("vad", "disabled", "disabled")
    if normalized == "webrtc":
        return _not_required("vad", "webrtc", "native_no_file")
    if normalized is None:
        try:
            custom_path = Path(selected).expanduser()
        except (OSError, RuntimeError, ValueError):
            return {
                "kind": "vad",
                "id": "selected_local_file",
                "status": "unresolved",
                "verification": "unavailable",
            }
        return _file_asset("vad", "selected_local_file", custom_path)
    artifact = crispasr.VAD_ARTIFACTS[normalized]
    return _crisp_cached_asset(settings, "vad", normalized, artifact)


def _pinned_crisp_asset(
    settings: dict[str, Any], key: str, kind: str, asset_id: str
) -> dict[str, str]:
    try:
        asset = crispasr_qwen_assets.ASSETS[key]
        path = crispasr_qwen_assets.cache_path(asset, settings)
    except (KeyError, OSError, RuntimeError, ValueError):
        return {
            "kind": kind,
            "id": asset_id,
            "status": "unresolved",
            "verification": "unavailable",
        }
    return _file_asset(
        kind,
        asset_id,
        path,
        pinned_size=asset.size,
    )


def _qwen_alignment_asset(settings: dict[str, Any]) -> dict[str, str]:
    custom = str(settings.get("qwen_aligner_model_path") or "").strip()
    if custom:
        try:
            selected_path = Path(custom).expanduser()
        except (OSError, RuntimeError, ValueError):
            return {
                "kind": "aligner",
                "id": "selected_local_file",
                "status": "unresolved",
                "verification": "unavailable",
            }
        return _file_asset("aligner", "selected_local_file", selected_path)
    try:
        path = qwen_alignment.cache_path(settings)
        expected_size = qwen_alignment.MODEL_SIZE
    except (OSError, RuntimeError, ValueError):
        return {
            "kind": "aligner",
            "id": "qwen3_forced_aligner",
            "status": "unresolved",
            "verification": "unavailable",
        }
    return _file_asset(
        "aligner", "qwen3_forced_aligner", path, pinned_size=expected_size
    )


def _moss_aligner_assets(settings: dict[str, Any]) -> list[dict[str, str]]:
    configured = str(settings.get("moss_ctc_aligner_model") or "auto").strip() or "auto"
    lowered = configured.lower()
    if lowered not in _MOSS_CANARY_ALIASES and lowered not in qwen_alignment.ALIASES:
        try:
            custom_path = Path(configured).expanduser()
        except (OSError, RuntimeError, ValueError):
            return [{
                "kind": "aligner",
                "id": "selected_local_file",
                "status": "unresolved",
                "verification": "unavailable",
            }]
        return [_file_asset("aligner", "selected_local_file", custom_path)]

    language = qwen_alignment.source_language(settings)
    if not language and lowered == "auto":
        return [
            _crisp_cached_asset(
                settings,
                "aligner",
                "canary_ctc_aligner",
                crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT,
            ),
            {
                "kind": "aligner",
                "id": "language_dependent",
                "status": "unresolved",
                "verification": "language_required",
            },
        ]

    if qwen_alignment.uses_qwen(
        settings, model_key="moss_ctc_aligner_model"
    ):
        return [_qwen_alignment_asset(settings)]

    language_problem = crispasr.ctc_language_problem(
        settings, model_key="moss_ctc_aligner_model"
    )
    if language_problem:
        return [
            _not_required("aligner", "language_unsupported", "native_timing_retained")
        ]
    return [
        _crisp_cached_asset(
            settings,
            "aligner",
            "canary_ctc_aligner",
            crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT,
        )
    ]


def _qwen_asr_aligner_asset(settings: dict[str, Any]) -> dict[str, str]:
    raw_language = settings.get("stt_language") or settings.get("whisper_language")
    try:
        language = qwen_asr.normalize_qwen_asr_language(raw_language)
    except (TypeError, ValueError):
        language = ""
    if language == "auto":
        return {
            "kind": "aligner",
            "id": "language_dependent",
            "status": "unresolved",
            "verification": "language_required",
        }
    timing_plan = qwen_asr.timing_plan_for_language(language)
    if timing_plan == "qwen3_forced_aligner":
        return _pinned_crisp_asset(
            settings,
            "qwen3_forced_aligner",
            "aligner",
            "qwen3_forced_aligner",
        )
    if timing_plan == "canary_ctc_fallback":
        return _crisp_cached_asset(
            settings,
            "aligner",
            "canary_ctc_aligner",
            crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT,
        )
    return {
        "kind": "aligner",
        "id": "unsupported_language",
        "status": "unresolved",
        "verification": "timing_plan_unavailable",
    }


def _lid_asset(settings: dict[str, Any]) -> dict[str, str] | None:
    language = str(
        settings.get("stt_language") or settings.get("whisper_language") or ""
    ).strip().lower()
    if language not in {"", "auto", "und", "unknown"}:
        return None
    backend = str(settings.get("stt_lid_backend") or "whisper").strip().lower()
    if backend in {"off", "none", "disabled"}:
        return None
    if backend in {"", "auto", "whisper"}:
        return _crisp_cached_asset(
            settings,
            "lid",
            "whisper_tiny_language_detector",
            crispasr.DEFAULT_LID_ARTIFACT,
        )
    return {
        "kind": "lid",
        "id": "selected_lid_backend",
        "status": "unresolved",
        "verification": "backend_asset_unknown",
    }


def diagnose_transcription_readiness(
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Assess the selected transcription files without acquiring or hashing them.

    ``ready`` means only that each selected local file is present and nonempty
    (or no local file is required). A separate runtime check is always needed.
    """

    options = dict(settings or {})
    raw_engine = str(
        options.get("stt_engine") or options.get("stt_backend") or options.get("engine") or ""
    ).strip().lower().replace("-", "_").replace(" ", "_")
    if raw_engine in _KNOWN_AUTO_ENGINES:
        engine = STT_ENGINE_AUTO
        status, ready = "unresolved", None
        assets: list[dict[str, str]] = []
    else:
        normalized_backend = normalize_stt_backend(raw_engine)
        if normalized_backend in CLOUD_STT_ENGINE_IDS:
            engine = normalized_backend
            assets = []
            status, ready = "not_required", True
        else:
            # ``normalize_engine`` intentionally maps legacy aliases and its
            # unrecognized legacy default to Whisper. Auto was handled above.
            engine = crispasr.normalize_engine(raw_engine)
            if engine == crispasr.STT_ENGINE_QWEN3:
                try:
                    model_id = qwen_asr.normalize_qwen_asr_model(
                        options.get("qwen_asr_model")
                    )
                    assets = [
                        _pinned_crisp_asset(
                            options,
                            model_id,
                            "asr",
                            model_id,
                        )
                    ]
                    chunk_mode = qwen_asr.resolve_chunk_mode(
                        options,
                        float(options.get("qwen_asr_chunk_seconds") or 30),
                    )
                    if chunk_mode == "vad":
                        assets.append(_vad_asset(options, True))
                    else:
                        assets.append(
                            _not_required("vad", "disabled", "fixed_chunk_mode")
                        )
                    assets.append(_qwen_asr_aligner_asset(options))
                except (TypeError, ValueError, qwen_asr.QwenASRError):
                    assets = [
                        {
                            "kind": "asr",
                            "id": "selected_model",
                            "status": "unresolved",
                            "verification": "invalid_selection",
                        }
                    ]
                status, ready = _asset_status(assets)
            elif engine in crispasr.MODELS:
                try:
                    selected_quantization = (
                        options.get("stt_model_quantization")
                        or options.get("crispasr_model_quantization")
                        or options.get("parakeet_quantization")
                        or crispasr.MODELS[engine].default_quantization
                    )
                    quantization = crispasr.validate_model_quantization(
                        str(selected_quantization), engine
                    )
                    filename = crispasr.MODELS[engine].filenames[quantization]
                    model_artifact = crispasr.CrispASRArtifact(
                        filename=filename, url=""
                    )
                    assets = [
                        _crisp_cached_asset(
                            options,
                            "asr",
                            f"{engine}_{quantization}",
                            model_artifact,
                        )
                    ]
                    vad_enabled = crispasr._vad_enabled(options, engine)
                    assets.append(_vad_asset(options, vad_enabled))
                    if engine == crispasr.STT_ENGINE_MOSS:
                        if crispasr.moss_ctc_alignment_enabled(options):
                            assets.extend(_moss_aligner_assets(options))
                        else:
                            assets.append(
                                _not_required("aligner", "disabled", "alignment_disabled")
                            )
                    lid = _lid_asset(options)
                    if lid is not None:
                        assets.append(lid)
                except (KeyError, TypeError, ValueError):
                    assets = [
                        {
                            "kind": "asr",
                            "id": "selected_model",
                            "status": "unresolved",
                            "verification": "invalid_selection",
                        }
                    ]
                status, ready = _asset_status(assets)
            else:
                engine = "unresolved"
                assets = []
                status, ready = "unresolved", None

    return {
        "schema_version": "1",
        "scope": "local_model_files",
        "status": status,
        "ready": ready,
        "engine": engine,
        "assets": assets,
        "runtime_check_required": True,
    }
