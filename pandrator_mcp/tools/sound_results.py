"""Bounded public projections for sound-domain results."""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import urlsplit

from ..clients.application_sounds import ApplicationSoundMethods
from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..results import ToolOutcome
from ..work_mapping import application_work_reference

_PROVENANCE_FIELDS = frozenset(
    {
        "provider",
        "model_id",
        "operation",
        "kind",
        "prompt",
        "lyrics",
        "parameters",
        "seed",
        "backend",
        "duration_seconds",
        "threads",
        "num_inference_steps",
        "guidance_scale",
    }
)
_VARIANT_FIELDS = (
    "id",
    "status",
    "artifact_id",
    "job_id",
    "item_id",
    "duration_seconds",
    "sample_rate",
    "channels",
    "created_at",
)
_ITEM_FIELDS = ("id", "name", "tags", "revision", "archived")
_DESCRIPTOR_TYPES = frozenset({"integer", "number", "boolean", "string"})
_SENSITIVE_PARAMETER_PARTS = (
    "path",
    "directory",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
)
_HEX_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_TEXTUAL_PARAMETER_KEYS = frozenset(
    {
        "negative_prompt",
        "tags",
        "keyscale",
        "timesignature",
        "language",
        "sampler",
        "sampler_mode",
        "cot",
    }
)
_BACKENDS = frozenset({"cpu", "vulkan", "cuda"})
_NATIVE_FILE_SUFFIXES = frozenset(
    {".bin", ".ckpt", ".gguf", ".json", ".model", ".onnx", ".pt", ".pth", ".safetensors", ".wav"}
)


def _application(runtime: McpRuntime) -> ApplicationSoundMethods:
    """Use the sound mixin surface that the composition root attaches to the client."""

    return cast(ApplicationSoundMethods, runtime.require_application())


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _safe_backends(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item in _BACKENDS))[:3]


def _bounded_number(value: Any, *, minimum: float, maximum: float) -> int | float | None:
    if type(value) is int:
        return value if minimum <= value <= maximum else None
    if type(value) is float and math.isfinite(value) and minimum <= value <= maximum:
        return value
    return None


def _safe_source_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _looks_like_path(value: str) -> bool:
    if value.startswith(("/", "\\", "~")) or "\\" in value or "://" in value:
        return True
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    parts = PurePosixPath(value).parts
    return len(parts) > 1 or ".." in parts


def _looks_like_native_path(value: str) -> bool:
    if (
        value.startswith(("/", "\\", "~"))
        or "\\" in value
        or "://" in value
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or ".." in PurePosixPath(value).parts
    ):
        return True
    return PurePosixPath(value).suffix.casefold() in _NATIVE_FILE_SUFFIXES


def _sensitive_parameter_key(value: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).casefold()
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    return bool(tokens & {"file", "filename", "dir"}) or any(
        part in normalized for part in _SENSITIVE_PARAMETER_PARTS
    )


def _safe_parameters(value: Any) -> dict[str, str | bool | int | float] | None:
    if not isinstance(value, dict) or len(value) > 32:
        return None
    result: dict[str, str | bool | int | float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64:
            continue
        if _sensitive_parameter_key(key):
            continue
        if isinstance(item, str):
            safe_textual_control = (
                key.casefold() in _TEXTUAL_PARAMETER_KEYS
                and not _looks_like_native_path(item)
                and not any(ord(character) < 32 for character in item)
            )
            if len(item) <= 2000 and (
                safe_textual_control
                or (not _looks_like_path(item) and not _looks_like_native_path(item))
            ):
                result[key] = item
        elif type(item) in (bool, int):
            result[key] = item
        elif type(item) is float and math.isfinite(item):
            result[key] = item
        if len(result) == 32:
            break
    return result


def _safe_asset_manifest(
    value: Any,
    *,
    from_model_assets: bool = False,
    default_revision: Any = None,
) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, Any]] = []
    for raw in value[:64]:
        item = _mapping(raw)
        filename = item.get("path" if from_model_assets else "filename")
        digest = item.get("sha256")
        if (
            not isinstance(filename, str)
            or len(filename) > 255
            or "\\" in filename
            or filename.startswith("~")
            or filename.startswith("/")
            or re.match(r"^[A-Za-z]:", filename)
            or "://" in filename
            or any(ord(character) < 32 for character in filename)
            or any(part in {"", ".", ".."} for part in filename.split("/"))
            or not isinstance(digest, str)
            or not _HEX_SHA256.fullmatch(digest)
        ):
            continue
        asset: dict[str, Any] = {"filename": filename, "sha256": digest.lower()}
        revision = item.get("revision")
        if revision is None and from_model_assets:
            revision = default_revision
        if (
            isinstance(revision, str)
            and len(revision) <= 255
            and not _looks_like_path(revision)
            and not any(ord(character) < 32 for character in revision)
        ):
            asset["revision"] = revision
        size = item.get("size_bytes" if from_model_assets else "bytes")
        if type(size) is int and 0 <= size <= 2**63 - 1:
            asset["bytes"] = size
        result.append(asset)
    return result


def _safe_public_reference(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    if value.startswith("https://"):
        return _safe_source_url(value)
    if (
        value.startswith(("/", "\\", "~"))
        or "\\" in value
        or ".." in value.split("/")
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _safe_runtime_identity(value: Any) -> dict[str, Any] | None:
    raw = _mapping(value)
    result: dict[str, Any] = {}
    for key in (
        "model_sha256",
        "model_revision",
        "runtime_version",
        "runtime_sha256",
        "minimum_runtime_version",
        "source_id",
        "repository",
    ):
        item = raw.get(key)
        if key.endswith("sha256"):
            if isinstance(item, str) and _HEX_SHA256.fullmatch(item):
                result[key] = item.lower()
        elif key in {"source_id", "repository"}:
            safe = _safe_public_reference(item)
            if safe is not None:
                result[key] = safe
        elif (
            isinstance(item, str)
            and len(item) <= 255
            and not _looks_like_path(item)
            and not any(ord(character) < 32 for character in item)
        ):
            result[key] = item
    manifest = _safe_asset_manifest(raw.get("asset_manifest"))
    if not manifest:
        model_assets = _safe_asset_manifest(
            raw.get("model_assets"),
            from_model_assets=True,
            default_revision=raw.get("model_revision"),
        )
        if model_assets:
            manifest = model_assets
    if manifest is not None:
        result["asset_manifest"] = manifest
    return result or None


def _safe_provenance(value: Any) -> dict[str, Any] | None:
    raw = _mapping(value)
    result: dict[str, Any] = {}
    for key in _PROVENANCE_FIELDS:
        if key not in raw:
            continue
        item = raw[key]
        if key in {"prompt", "lyrics"} and isinstance(item, str):
            result[key] = item[: (8000 if key == "prompt" else 16000)]
        elif key == "kind" and isinstance(item, str) and item in {"sound", "music"}:
            result[key] = item
        elif key == "parameters":
            parameters = _safe_parameters(item)
            if parameters is not None:
                result[key] = parameters
        elif (key == "duration_seconds" and _bounded_number(item, minimum=1, maximum=600) is not None) or (
            key == "guidance_scale" and _bounded_number(item, minimum=0, maximum=10**12) is not None
        ) or (
            key in {"threads", "num_inference_steps"} and type(item) is int
        ) or (
            key == "seed" and (item is None or type(item) is int)
        ):
            if key in {"duration_seconds", "guidance_scale"}:
                result[key] = _bounded_number(
                    item,
                    minimum=1 if key == "duration_seconds" else 0,
                    maximum=600 if key == "duration_seconds" else 10**12,
                )
            else:
                result[key] = item
        elif (
            key in {"provider", "model_id", "operation", "backend"}
            and isinstance(item, str)
            and not _looks_like_path(item)
            and not any(ord(character) < 32 for character in item)
        ):
            if key == "backend" and item not in _BACKENDS:
                continue
            result[key] = item[:255]
    safe_identity = _safe_runtime_identity(raw.get("runtime_identity"))
    if safe_identity:
        result["runtime_identity"] = safe_identity
    return result or None


def _safe_variant(value: Any, *, include_detail: bool = False) -> dict[str, Any] | None:
    raw = _mapping(value)
    if not raw:
        return None
    result = {key: raw[key] for key in _VARIANT_FIELDS if key in raw}
    if include_detail:
        provenance = _safe_provenance(raw.get("provenance"))
        if provenance is not None:
            result["provenance"] = provenance
    return result


def _safe_candidate(value: Any, *, include_detail: bool = False) -> dict[str, Any]:
    return _safe_variant(value, include_detail=include_detail) or {}


def _safe_sound_item(value: Any, *, include_detail: bool = False) -> dict[str, Any]:
    raw = _mapping(value)
    result = {key: raw[key] for key in _ITEM_FIELDS if key in raw}
    if "preferred_variant" in raw:
        result["preferred_variant"] = _safe_variant(
            raw.get("preferred_variant"), include_detail=include_detail
        )
    return result


def _safe_sound_page(value: Any, *, candidates: bool = False) -> dict[str, Any]:
    raw = _mapping(value)
    items: list[dict[str, Any]] = []
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        items.append(_safe_candidate(item) if candidates else _safe_sound_item(item))
    return {"items": items, "next_cursor": raw.get("next_cursor")}


def _safe_sound_detail(value: Any, *, include_detail: bool) -> dict[str, Any]:
    raw = _mapping(value)
    result = _safe_sound_item(raw, include_detail=include_detail)
    variants = _mapping(raw.get("variants"))
    result["variants"] = {
        "items": [
            _safe_candidate(item, include_detail=include_detail)
            for item in variants.get("items") or [] if isinstance(item, dict)
        ],
        "next_cursor": variants.get("next_cursor"),
    }
    return result


def _safe_descriptor_number(value: Any, *, integer_only: bool = False) -> int | float | None:
    if integer_only:
        return value if type(value) is int and abs(value) <= 10**12 else None
    if type(value) is int and abs(value) <= 10**12:
        return value
    if type(value) is float and math.isfinite(value) and abs(value) <= 10**12:
        return value
    return None


def _safe_model_duration(value: Any) -> dict[str, int | float | str] | None:
    raw = _mapping(value)
    minimum = _safe_descriptor_number(raw.get("minimum"))
    maximum = _safe_descriptor_number(raw.get("maximum"))
    default = _safe_descriptor_number(raw.get("default"))
    if (
        minimum is None
        or maximum is None
        or default is None
        or not 1 <= minimum <= default <= maximum <= 600
    ):
        return None
    result: dict[str, int | float | str] = {
        "minimum": minimum,
        "maximum": maximum,
        "default": default,
    }
    description = _bounded_text(raw.get("description"), 500)
    if description is not None:
        result["description"] = description
    return result


def _safe_parameter_descriptor(value: Any) -> dict[str, Any] | None:
    raw = _mapping(value)
    parameter_type = raw.get("type")
    label = raw.get("label")
    if (
        not isinstance(parameter_type, str)
        or parameter_type not in _DESCRIPTOR_TYPES
        or not isinstance(label, str)
    ):
        return None
    result: dict[str, Any] = {
        "type": parameter_type,
        "label": label[:200],
    }
    description = raw.get("description")
    if isinstance(description, str):
        result["description"] = description[:1000]

    if parameter_type in {"integer", "number"}:
        integer_only = parameter_type == "integer"
        for key in ("minimum", "maximum", "exclusive_minimum"):
            if key not in raw:
                continue
            number = _safe_descriptor_number(raw[key], integer_only=integer_only)
            if number is not None:
                result[key] = number
        if "minimum" in result and "maximum" in result and result["minimum"] > result["maximum"]:
            return None
    default = raw.get("default")
    if parameter_type == "integer" and type(default) is int and abs(default) <= 10**12:
        result["default"] = default
    elif parameter_type == "number" and _safe_descriptor_number(default) is not None:
        result["default"] = default
    elif parameter_type == "boolean" and type(default) is bool:
        result["default"] = default
    elif (
        parameter_type == "string"
        and isinstance(default, str)
        and len(default) <= 2000
        and not _looks_like_path(default)
    ):
        result["default"] = default

    options = raw.get("options")
    if isinstance(options, list) and len(options) <= 64:
        safe_options = [
            option[:255]
            for option in options
            if isinstance(option, str) and len(option) <= 255 and not _looks_like_path(option)
        ]
        if safe_options:
            result["options"] = safe_options
    return result


def _safe_model_parameters(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, dict) or len(value) > 64:
        return None
    result: dict[str, dict[str, Any]] = {}
    for key in sorted(key for key in value if isinstance(key, str)):
        if (
            not isinstance(key, str)
            or len(key) > 64
            or _sensitive_parameter_key(key)
        ):
            continue
        descriptor = _safe_parameter_descriptor(value[key])
        if descriptor is not None:
            result[key] = descriptor
    return result


def _safe_model(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    result: dict[str, Any] = {}
    string_limits = {
        "id": 255,
        "label": 255,
        "family": 80,
        "task": 80,
        "model_revision": 255,
        "minimum_runtime_version": 255,
    }
    for key, limit in string_limits.items():
        text = _bounded_text(raw.get(key), limit)
        if text is not None:
            result[key] = text
    cached = raw.get("cached")
    if type(cached) is bool:
        result["cached"] = cached
    kinds = raw.get("kinds")
    if isinstance(kinds, list):
        safe_kinds = list(
            dict.fromkeys(
                item for item in kinds if isinstance(item, str) and item in {"sound", "music"}
            )
        )
        result["kinds"] = safe_kinds[:2]
    backends = raw.get("backends")
    if isinstance(backends, list):
        result["backends"] = _safe_backends(backends)
    description = _bounded_text(raw.get("description"), 2000)
    if description is not None:
        result["description"] = description
    source_url = _safe_source_url(raw.get("source_url"))
    if source_url is not None:
        result["source_url"] = source_url
    duration = _safe_model_duration(raw.get("duration"))
    if duration is not None:
        result["duration"] = duration
    for key in ("supports_lyrics", "requires_lyrics", "available"):
        item = raw.get(key)
        if type(item) is bool:
            result[key] = item
    reason = _bounded_text(raw.get("reason"), 1000)
    if reason is not None:
        result["reason"] = reason
    for key in ("download_bytes", "seed_maximum"):
        item = raw.get(key)
        maximum = 2**63 - 1 if key == "download_bytes" else 2**32 - 1
        if type(item) is int and 0 <= item <= maximum:
            result[key] = item
    model_hash = raw.get("model_sha256")
    if isinstance(model_hash, str) and _HEX_SHA256.fullmatch(model_hash):
        result["model_sha256"] = model_hash.lower()
    parameters = _safe_model_parameters(raw.get("parameters"))
    if parameters is not None:
        result["parameters"] = parameters
    return result


def _safe_catalogue(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    result: dict[str, Any] = {}
    for key in ("provider", "operation"):
        text = _bounded_text(raw.get(key), 255)
        if text is not None:
            result[key] = text
    notes = _bounded_text(raw.get("notes"), 1000)
    if notes is not None:
        result["notes"] = notes
    for key in ("models", "unavailable_models"):
        models = raw.get(key)
        if isinstance(models, list):
            result[key] = [_safe_model(model) for model in models[:100] if isinstance(model, dict)]
    backends = raw.get("backends")
    if isinstance(backends, list):
        safe_backends = _safe_backends(backends)
        if safe_backends:
            result["backends"] = safe_backends
    runtime = raw.get("runtime")
    if isinstance(runtime, dict):
        safe_runtime: dict[str, Any] = {}
        available = runtime.get("available")
        if type(available) is bool:
            safe_runtime["available"] = available
        version = _bounded_text(runtime.get("version"), 255)
        reason = _bounded_text(runtime.get("reason"), 2000)
        if version is not None:
            safe_runtime["version"] = version
        if reason is not None:
            safe_runtime["reason"] = reason
        runtime_backends = runtime.get("backends")
        if isinstance(runtime_backends, list):
            safe_runtime["backends"] = _safe_backends(runtime_backends)
        result["runtime"] = safe_runtime
    defaults = raw.get("defaults")
    if isinstance(defaults, dict):
        safe_defaults: dict[str, Any] = {}
        model_id = _bounded_text(defaults.get("model_id"), 255)
        if model_id is not None:
            safe_defaults["model_id"] = model_id
        duration = _bounded_number(defaults.get("duration_seconds"), minimum=1, maximum=600)
        if duration is not None:
            safe_defaults["duration_seconds"] = duration
        threads = defaults.get("threads")
        if type(threads) is int and threads >= 1:
            safe_defaults["threads"] = threads
        backend = defaults.get("backend")
        if isinstance(backend, str) and backend in _BACKENDS:
            safe_defaults["backend"] = backend
        kind = defaults.get("kind")
        if isinstance(kind, str) and kind in {"sound", "music"}:
            safe_defaults["kind"] = kind
        for key in ("num_inference_steps",):
            item = defaults.get(key)
            if type(item) is int and item >= 1:
                safe_defaults[key] = item
        guidance_scale = _bounded_number(
            defaults.get("guidance_scale"), minimum=0, maximum=10**12
        )
        if guidance_scale is not None:
            safe_defaults["guidance_scale"] = guidance_scale
        result["defaults"] = safe_defaults
    return result


def _safe_generation_result(value: Any) -> tuple[dict[str, Any], ToolOutcome | None]:
    raw = _mapping(value)
    candidate_id = raw.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise PandratorMcpError(
            "downstream_unavailable",
            "Pandrator returned sound-generation work without a candidate ID.",
        )
    cache_hit = raw.get("cache_hit") is True
    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "job_id": raw.get("job_id") if isinstance(raw.get("job_id"), str) else None,
        "status": str(raw.get("status") or "unknown")[:80],
        "cache_hit": cache_hit,
    }
    if isinstance(raw.get("item_id"), str):
        result["item_id"] = raw["item_id"]
    settings = _mapping(raw.get("resolved_settings"))
    safe_settings: dict[str, Any] = {}
    model_id = _bounded_text(settings.get("model_id"), 255)
    if model_id is not None:
        safe_settings["model_id"] = model_id
    kind = settings.get("kind")
    if isinstance(kind, str) and kind in {"sound", "music"}:
        safe_settings["kind"] = kind
    backend = settings.get("backend")
    if isinstance(backend, str) and backend in _BACKENDS:
        safe_settings["backend"] = backend
    duration = _bounded_number(settings.get("duration_seconds"), minimum=1, maximum=600)
    if duration is not None:
        safe_settings["duration_seconds"] = duration
    seed = settings.get("seed")
    if type(seed) is int and 0 <= seed <= 2**32 - 1:
        safe_settings["seed"] = seed
    threads = settings.get("threads")
    if type(threads) is int and threads >= 1:
        safe_settings["threads"] = threads
    steps = settings.get("num_inference_steps")
    if type(steps) is int and steps >= 1:
        safe_settings["num_inference_steps"] = steps
    guidance_scale = _bounded_number(
        settings.get("guidance_scale"), minimum=0, maximum=10**12
    )
    if guidance_scale is not None:
        safe_settings["guidance_scale"] = guidance_scale
    result["resolved_settings"] = safe_settings

    raw_candidates = raw.get("candidates")
    raw_candidate_count = raw.get("candidate_count")
    if (
        type(raw_candidate_count) is int
        and raw_candidate_count > 1
        and isinstance(raw_candidates, list)
    ):
        candidates = [
            child
            for child in (_safe_batch_candidate(item) for item in raw_candidates[:8])
            if child is not None
        ]
        if not candidates:
            raise PandratorMcpError(
                "downstream_unavailable",
                "Pandrator returned sound-generation batch work without valid candidates.",
            )
        first = candidates[0]
        result.update(
            {
                "candidate_id": first["candidate_id"],
                "job_id": first["job_id"],
                "item_id": first["item_id"],
                "status": first["status"],
                "cache_hit": all(child["cache_hit"] for child in candidates),
                "candidate_count": len(candidates),
                "candidates": candidates,
            }
        )
        pending: list[dict[str, Any]] = []
        pending_job_ids: set[str] = set()
        for child in candidates:
            job_id = child["job_id"]
            if (
                child["status"] in {"queued", "running", "cancel_requested"}
                and isinstance(job_id, str)
                and job_id not in pending_job_ids
            ):
                pending_job_ids.add(job_id)
                pending.append(child)
        if not pending:
            return result, None
        first_pending = pending[0]
        work = application_work_reference(
            {
                **raw,
                "candidate_id": first_pending["candidate_id"],
                "job_id": first_pending["job_id"],
                "status": first_pending["status"],
                "cache_hit": first_pending["cache_hit"],
            }
        )
        return result, ToolOutcome(
            result=result,
            work=work,
            next_actions=[
                NextAction(
                    tool="pandrator_get_work",
                    arguments={"work_id": child["job_id"], "work_type": "job"},
                    reason="Inspect this sound-generation job until its candidate is ready.",
                )
                for child in pending
            ],
        )

    job_id = result["job_id"]
    if cache_hit or not job_id:
        return result, None
    work = application_work_reference(raw)
    return result, ToolOutcome(
        result=result,
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={"work_id": job_id, "work_type": "job"},
                reason="Inspect the sound-generation job until its candidate is ready.",
            )
        ],
    )


def _safe_batch_candidate(value: Any) -> dict[str, Any] | None:
    raw = _mapping(value)
    candidate_id = raw.get("candidate_id")
    seed = raw.get("seed")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 255
        or _looks_like_path(candidate_id)
        or any(ord(character) < 32 for character in candidate_id)
        or type(seed) is not int
        or not 0 <= seed <= 2**32 - 1
    ):
        return None

    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "job_id": None,
        "item_id": None,
        "status": "unknown",
        "cache_hit": raw.get("cache_hit") is True,
        "seed": seed,
    }
    for key in ("job_id", "item_id"):
        item = raw.get(key)
        if (
            isinstance(item, str)
            and item
            and len(item) <= 255
            and not _looks_like_path(item)
            and not any(ord(character) < 32 for character in item)
        ):
            result[key] = item
    status = raw.get("status")
    if (
        isinstance(status, str)
        and len(status) <= 80
        and not _looks_like_path(status)
        and not any(ord(character) < 32 for character in status)
    ):
        result["status"] = status
    return result
