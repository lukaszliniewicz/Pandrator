"""Validated audio.cpp request-parameter contracts.

The descriptors in this module are intentionally small.  They describe only
scalar request controls that are safe to expose through the audio.cpp
``options`` object; voice/reference and instruction fields remain owned by the
speech request builder.
"""

from __future__ import annotations

import copy
import math
import re
from numbers import Real
from typing import Any, Mapping

from .audio_cpp_catalogue import family_metadata


_UINT32_MAX = 2**32 - 1
_SAFE_INTEGER_MAX = 2**53 - 1
_CHUNK_MODES = ["default", "tag_aware", "japanese", "endline"]
_MAX_GENERIC_STRING_LENGTH = 1200
_SAFE_OPTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_RESERVED_OPTION_NAMES = {
    "instruction",
    "instruct",
    "instructions",
    "reference_text",
    "reference_language",
    "prompt_text",
    "voice_clone_text",
    "voice_id",
    "voice",
    "speaker",
    "language",
    "template_name",
    "no_ref",
    "source_text",
    "target_text",
    "multi_reference_cond",
    "source_audio",
    "target_voice",
    "phonemes",
    "return_video",
    "path",
    "audio_path",
    "string_list",
    "list",
    "array",
    "object",
    "objects",
}
_UNSAFE_NATIVE_TYPES = {
    "path",
    "audio_path",
    "string_list",
    "list",
    "array",
    "object",
    "objects",
    "dict",
    "mapping",
}
_NATIVE_SCALAR_TYPES = {
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "enum": "string",
    "string": "string",
}


def _integer(
    *,
    default: int | None = None,
    minimum: int | float | None = None,
    maximum: int | float | None = _SAFE_INTEGER_MAX,
    exclusive_minimum: int | float | None = None,
    exclusive_maximum: int | float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "integer"}
    if default is not None:
        result["default"] = default
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    if exclusive_minimum is not None:
        result["exclusive_minimum"] = exclusive_minimum
    if exclusive_maximum is not None:
        result["exclusive_maximum"] = exclusive_maximum
    return result


def _number(
    *,
    default: float | int | None = None,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
    exclusive_minimum: int | float | None = None,
    exclusive_maximum: int | float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "number"}
    if default is not None:
        result["default"] = default
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    if exclusive_minimum is not None:
        result["exclusive_minimum"] = exclusive_minimum
    if exclusive_maximum is not None:
        result["exclusive_maximum"] = exclusive_maximum
    return result


def _boolean(*, default: bool | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "boolean"}
    if default is not None:
        result["default"] = default
    return result


def _string(
    *, default: str | None = None, enum: list[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "string"}
    if default is not None:
        result["default"] = default
    if enum is not None:
        result["enum"] = list(enum)
    return result


_UINT32_OPTIONAL = _integer(minimum=0, maximum=_UINT32_MAX)
_UINT32_DEFAULT = _integer(default=1234, minimum=0, maximum=_UINT32_MAX)
_CHUNK_MODE = _string(enum=_CHUNK_MODES)


# The keys below are wire names consumed by audio.cpp.  Do not add reference,
# voice, or instruction fields here: those values have separate safeguards in
# the request builder and are not generic tuning controls.
AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY: dict[str, dict[str, dict[str, Any]]] = {
    "moss_voicegen": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "temperature": _number(exclusive_minimum=0),
        "top_p": _number(minimum=0, maximum=1),
        "top_k": _integer(minimum=0, maximum=2**31 - 1),
        "repetition_penalty": _number(exclusive_minimum=0),
        "text_chunk_size": _integer(minimum=1),
        "text_chunk_mode": copy.deepcopy(_CHUNK_MODE),
    },
    "chatterbox_turbo": {
        "seed": _integer(default=0, minimum=0, maximum=_UINT32_MAX),
        "temperature": _number(default=0.8, minimum=0),
        "top_p": _number(default=0.95, minimum=0, maximum=1),
        "top_k": _integer(default=1000, minimum=0, maximum=_UINT32_MAX),
        "repetition_penalty": _number(default=1.2, exclusive_minimum=0),
        "max_new_tokens": _integer(default=1000, minimum=1, maximum=_UINT32_MAX),
    },
    "supertonic": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "num_inference_steps": _integer(minimum=1),
        "speaking_rate": _number(exclusive_minimum=0),
        "text_chunk_size": _integer(minimum=1),
        "text_chunk_mode": copy.deepcopy(_CHUNK_MODE),
    },
    "qwen3_tts": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "max_tokens": _integer(default=2048, minimum=1),
        "do_sample": _boolean(default=True),
        "temperature": _number(default=0.9, exclusive_minimum=0),
        "top_k": _integer(default=50, minimum=0),
        "top_p": _number(default=1, minimum=0, maximum=1),
        "repetition_penalty": _number(default=1.05, exclusive_minimum=0),
        "subtalker_do_sample": _boolean(default=True),
        "subtalker_temperature": _number(default=0.9, exclusive_minimum=0),
        "subtalker_top_k": _integer(default=50, minimum=0),
        "subtalker_top_p": _number(default=1, minimum=0, maximum=1),
    },
    "fish_audio_s2": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "max_tokens": _integer(default=1024, minimum=1),
        "top_p": _number(default=0.8, exclusive_minimum=0, maximum=1),
        "top_k": _integer(default=30, minimum=1),
        "temperature": _number(default=0.8, exclusive_minimum=0, exclusive_maximum=2),
        "text_chunk_size": _integer(default=200, minimum=1),
        "text_chunk_mode": {
            "type": "string",
            # ``word_budget`` is the Fish spec's default alias for the
            # framework's ``default`` splitter; the server accepts both.
            "enum": [*_CHUNK_MODES, "word_budget"],
            "default": "word_budget",
        },
    },
    "voxcpm2": {
        "seed": copy.deepcopy(_UINT32_DEFAULT),
        "min_tokens": _integer(default=2, minimum=0),
        "max_tokens": _integer(default=4096, minimum=0),
        "num_inference_steps": _integer(default=10, minimum=1),
        "guidance_scale": _number(default=2, minimum=0),
        "retry_badcase": _boolean(default=True),
        "retry_badcase_max_times": _integer(default=3, minimum=1),
        "retry_badcase_ratio_threshold": _number(default=6, exclusive_minimum=0),
    },
    "magpie_tts": {
        "seed": _integer(default=0, minimum=0, maximum=_SAFE_INTEGER_MAX),
        "temperature": _number(default=0.6, minimum=0),
        "top_k": _integer(default=80, minimum=1),
        "guidance_scale": _number(default=2.5, minimum=0),
        "max_tokens": _integer(default=500, minimum=1),
        "text_chunk_size": _integer(default=300, minimum=1),
        "text_chunk_mode": {**_CHUNK_MODE, "default": "default"},
    },
    "chatterbox": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "exaggeration": _number(default=0.5),
        "guidance_scale": _number(default=0.5),
        "temperature": _number(default=0.8),
        "repetition_penalty": _number(default=1.2),
        "min_p": _number(default=0.05),
        "top_p": _number(default=1),
        "s3gen_cfg_rate": _number(default=0.7),
        "max_tokens": _integer(default=384, minimum=1),
        "do_sample": _boolean(default=True),
        "stop_on_eos": _boolean(default=True),
    },
    "omnivoice": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "num_inference_steps": _integer(default=32, minimum=1),
        "guidance_scale": _number(default=2),
        "speed": _number(default=1, exclusive_minimum=0),
        "duration": _number(exclusive_minimum=0),
        "t_shift": _number(default=0.1),
        "denoise": _boolean(default=True),
        "preprocess_prompt": _boolean(default=True),
        "postprocess_output": _boolean(default=True),
        "layer_penalty_factor": _number(default=5),
        "position_temperature": _number(default=5),
        "class_temperature": _number(default=0),
        "audio_chunk_duration": _number(default=15, exclusive_minimum=0),
        "audio_chunk_threshold": _number(default=30, exclusive_minimum=0),
        "text_chunk_size": _integer(minimum=1),
        "text_chunk_mode": {**_CHUNK_MODE, "default": "tag_aware"},
    },
    "pocket_tts": {
        "seed": copy.deepcopy(_UINT32_OPTIONAL),
        "max_steps": _integer(default=0, minimum=0),
        "max_tokens": _integer(default=50, minimum=1),
        "temperature": _number(exclusive_minimum=0),
        "noise_clamp": _number(default=-1),
        "eos_threshold": _number(default=-4),
        "frames_after_eos": _integer(default=-1, minimum=-1),
        "text_chunk_size": _integer(minimum=1),
        "truncate_clone_audio": _boolean(default=False),
    },
    "fireredtts3": {
        "seed": copy.deepcopy(_UINT32_DEFAULT),
        "num_inference_steps": _integer(default=10, minimum=1),
        "guidance_scale": _number(default=2, exclusive_minimum=0),
        "stop_threshold": _number(default=0.5, minimum=0, maximum=1),
        "text_chunk_size": _integer(default=600, minimum=1),
        "text_chunk_mode": {**_CHUNK_MODE, "default": "default"},
    },
    "breeze_tts": {
        "seed": _integer(default=0, minimum=0, maximum=_SAFE_INTEGER_MAX),
        "max_tokens": _integer(default=1500, minimum=1),
        "guidance_scale": _number(default=1, minimum=0),
        "temperature": _number(default=0.9, exclusive_minimum=0),
        "depth_temperature": _number(default=0.9, exclusive_minimum=0),
        "top_k": _integer(default=50, minimum=0),
        "top_p": _number(default=1, exclusive_minimum=0, maximum=1),
        "text_chunk_size": _integer(default=600, minimum=1),
        "text_chunk_mode": {**_CHUNK_MODE, "default": "default"},
    },
}

# Stable alias for callers that describe this as a registry.
AUDIO_CPP_PARAMETER_REGISTRY = AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY


def request_parameters_for_family(family: str) -> dict[str, dict[str, Any]]:
    """Return reviewed manual and safe native descriptors for a family.

    The generated catalogue describes the upstream request surface, while the
    manual registry remains authoritative for controls already wired through
    Pandrator.  Unknown or non-scalar catalogue fields are deliberately left
    out of the returned mapping.
    """

    normalized = str(family or "").strip().lower()
    if normalized == "fish_audio":
        normalized = "fish_audio_s2"
    manual = copy.deepcopy(AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY.get(normalized, {}))
    derived: dict[str, dict[str, Any]] = {}
    try:
        metadata = family_metadata(normalized)
    except (OSError, TypeError, ValueError):
        metadata = {}
    if isinstance(metadata, Mapping):
        options = metadata.get("options")
        if isinstance(options, Mapping):
            request_options = options.get("request", [])
        else:
            request_options = []
        if isinstance(request_options, list):
            for option in request_options:
                descriptor = _descriptor_from_native_option(option)
                if descriptor is not None:
                    derived[descriptor.pop("_name")] = descriptor
    derived.update(manual)
    return derived


def _finite_native_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return value if isinstance(value, int) else numeric


def _valid_native_default(
    value: Any,
    option_type: str,
    descriptor: Mapping[str, Any],
) -> bool:
    if option_type == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif option_type == "number":
        valid = _finite_native_number(value) is not None
    elif option_type == "boolean":
        valid = isinstance(value, bool)
    else:
        valid = isinstance(value, str)
        if valid and "enum" in descriptor:
            valid = value in descriptor["enum"]
        if valid and "enum" not in descriptor:
            valid = "\x00" not in value and len(value) <= _MAX_GENERIC_STRING_LENGTH
    if not valid:
        return False
    if option_type in {"integer", "number"}:
        minimum = descriptor.get("minimum")
        maximum = descriptor.get("maximum")
        if minimum is not None and value < minimum:
            return False
        if maximum is not None and value > maximum:
            return False
    return True


def _descriptor_from_native_option(option: Any) -> dict[str, Any] | None:
    if not isinstance(option, Mapping):
        return None
    name = option.get("name")
    native_type = option.get("type")
    if (
        not isinstance(name, str)
        or not _SAFE_OPTION_NAME.fullmatch(name)
        or name in _RESERVED_OPTION_NAMES
        or not isinstance(native_type, str)
    ):
        return None
    native_type = native_type.strip().lower()
    if native_type in _UNSAFE_NATIVE_TYPES:
        return None
    option_type = _NATIVE_SCALAR_TYPES.get(native_type)
    if option_type is None:
        return None

    descriptor: dict[str, Any] = {"_name": name, "type": option_type}
    if native_type == "enum":
        values = option.get("values")
        if not isinstance(values, (list, tuple)) or not values or not all(
            isinstance(value, str) for value in values
        ):
            return None
        descriptor["enum"] = list(values)

    if option_type in {"integer", "number"}:
        minimum = _finite_native_number(option.get("min"))
        maximum = _finite_native_number(option.get("max"))
        if minimum is not None and maximum is not None and minimum > maximum:
            minimum = maximum = None
        if minimum is not None:
            descriptor["minimum"] = minimum
        if maximum is not None:
            descriptor["maximum"] = maximum

    if "default" in option and _valid_native_default(
        option["default"], option_type, descriptor
    ):
        descriptor["default"] = option["default"]
    return descriptor


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _numeric(value: Any, *, key: str, integer: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, Real):
        expected = "an integer" if integer else "a number"
        raise ValueError(f"audio.cpp option '{key}' must be {expected}.")
    # JSON settings are also edited by JavaScript clients. Keep integers exact
    # until their descriptor bounds reject values outside the safe wire range.
    if integer and isinstance(value, int):
        return value
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"audio.cpp option '{key}' must be finite.") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"audio.cpp option '{key}' must be finite.")
    if integer:
        if not numeric.is_integer():
            raise ValueError(f"audio.cpp option '{key}' must be an integer.")
        return int(numeric)
    return value if isinstance(value, int) else numeric


def validate_model_options(
    family: str,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and normalize one selected model's scalar option mapping.

    ``None`` and empty strings intentionally disappear so audio.cpp can apply
    its own model defaults.  Defaults in the registry describe the server
    contract and are never injected into a request.
    """

    if not isinstance(options, Mapping):
        raise ValueError("audio.cpp model settings must be an object.")
    descriptors = request_parameters_for_family(family)
    normalized_family = str(family or "").strip().lower()
    if normalized_family == "fish_audio":
        normalized_family = "fish_audio_s2"
    manual_descriptors = AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY.get(
        normalized_family, {}
    )
    result: dict[str, Any] = {}
    for key, value in options.items():
        if key not in descriptors:
            raise ValueError(f"Unknown audio.cpp option '{key}' for family '{family}'.")
        if _is_empty(value):
            continue
        descriptor = descriptors[key]
        option_type = descriptor["type"]
        if option_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"audio.cpp option '{key}' must be a boolean.")
            normalized: Any = value
        elif option_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"audio.cpp option '{key}' must be a string.")
            if key not in manual_descriptors and "enum" not in descriptor:
                if "\x00" in value:
                    raise ValueError(
                        f"audio.cpp option '{key}' must not contain NUL characters."
                    )
                if len(value) > _MAX_GENERIC_STRING_LENGTH:
                    raise ValueError(
                        f"audio.cpp option '{key}' must be at most "
                        f"{_MAX_GENERIC_STRING_LENGTH} characters."
                    )
            normalized = value
        elif option_type in {"integer", "number"}:
            normalized = _numeric(value, key=key, integer=option_type == "integer")
        else:  # pragma: no cover - registry is internal and exhaustively typed
            raise ValueError(f"Unsupported audio.cpp option type '{option_type}'.")

        enum = descriptor.get("enum")
        if enum is not None and normalized not in enum:
            allowed = ", ".join(map(str, enum))
            raise ValueError(f"audio.cpp option '{key}' must be one of: {allowed}.")
        for bound_name, predicate, symbol in (
            ("minimum", lambda actual, bound: actual < bound, "at least"),
            ("maximum", lambda actual, bound: actual > bound, "at most"),
            (
                "exclusive_minimum",
                lambda actual, bound: actual <= bound,
                "greater than",
            ),
            ("exclusive_maximum", lambda actual, bound: actual >= bound, "less than"),
        ):
            bound = descriptor.get(bound_name)
            if bound is not None and predicate(normalized, bound):
                raise ValueError(f"audio.cpp option '{key}' must be {symbol} {bound}.")
        result[key] = normalized
    return result


def validate_audio_cpp_model_options(
    family: str,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Compatibility-named entry point for request builders and identity code."""

    return validate_model_options(family, options)


__all__ = [
    "AUDIO_CPP_PARAMETER_REGISTRY",
    "AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY",
    "request_parameters_for_family",
    "validate_audio_cpp_model_options",
    "validate_model_options",
]
