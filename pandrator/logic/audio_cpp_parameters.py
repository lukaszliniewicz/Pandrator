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

from .audio_cpp_inventory_data import family_metadata

_UINT32_MAX = 2**32 - 1
_SAFE_INTEGER_MAX = 2**53 - 1
_CHUNK_MODES = ["default", "tag_aware", "japanese", "endline"]
_MAX_GENERIC_STRING_LENGTH = 1200
FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT = 300
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
    "voice_samples",
    "speaker_embedding_file",
    "reference_codes_file",
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

# ``request_parameters_for_family`` is the TTS request projection.  Some
# native family specs combine TTS, ASR, and speech-to-speech controls, so keep
# controls from those other operations out of the TTS option surface.
_TTS_UNSUPPORTED_NATIVE_OPTIONS = {
    "lfm2_audio": {
        "audio_chunk_mode",
        "audio_chunk_seconds",
        "audio_chunk_duration_sec",
        "text_temperature",
        "text_top_k",
        "stream_frames_per_event",
    },
    # The edit operation owns this field; the speech request builder must not
    # expose it as a TTS tuning control.
    "dots_tts": {"use_xvector", "vocoder_merge_steps"},
    # Higgs accepts this field for API compatibility, but audio-code sampling
    # does not consume it.
    "higgs_audio_tts": {"repetition_penalty"},
}
_AUK_FLASH_MODEL_IDS = frozenset(
    {"auk_flash_f16", "auk_flash_f32", "auk_flash_q8_0"}
)
_AUK_FLASH_BASE_ONLY_OPTIONS = frozenset(
    {"num_inference_steps", "guidance_scale", "sway_sampling_coef"}
)


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
        "max_tokens": _integer(minimum=1),
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
        "text_chunk_size": {
            **_integer(default=FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT, minimum=1),
            "native_default": 200,
            "default_source": "pandrator_policy",
            "description": (
                "Pandrator defaults to 300 characters per chunk; the native audio.cpp "
                "default is 200."
            ),
        },
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

# v0.9.2's generated inventory uses these names, while saved v0.9.1 settings
# may still contain the names on the left. Keep the aliases scoped to the
# families whose upstream option contracts established them.
_AUDIO_CPP_OPTION_ALIASES: dict[str, dict[str, str]] = {
    "chatterbox": {"s3gen_cfg_rate": "flow_guidance_scale"},
    "chatterbox_turbo": {"max_new_tokens": "max_tokens"},
    "pocket_tts": {
        "max_steps": "max_frames",
        "max_tokens": "text_chunk_tokens",
    },
    "f5_tts": {"cfg_strength": "guidance_scale"},
    "firered_audio": {
        "max_new_audio_steps": "max_audio_steps",
        "min_new_audio_steps": "min_audio_steps",
        "max_new_text_tokens": "max_text_tokens",
        "max_new_tokens": "max_tokens",
    },
    "index_tts2": {"duration_factor": "duration_scale"},
    "inflect_v2": {"speaking_rate": "speed"},
    "sanotts": {"speaking_rate": "speed"},
    "sopro_tts": {
        "max_seconds": "max_duration_sec",
        "min_seconds": "min_duration_sec",
        "ref_seconds": "reference_duration_sec",
    },
    "zipvoice": {"t_shift": "shift", "lang": "language"},
    "moss_tts_v15": {"tokens": "duration_frames"},
}


def _normalized_family_name(family: str) -> str:
    normalized = str(family or "").strip().lower()
    return "fish_audio_s2" if normalized == "fish_audio" else normalized


def _public_option_name(family: str, name: str) -> str:
    """Map a native alias to its one retained public option name."""

    normalized = _normalized_family_name(family)
    for legacy_name, native_name in _AUDIO_CPP_OPTION_ALIASES.get(normalized, {}).items():
        if name not in {legacy_name, native_name}:
            continue
        if normalized == "zipvoice" and legacy_name == "lang":
            return legacy_name
        if legacy_name in AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY.get(normalized, {}):
            return legacy_name
        return native_name
    return name


def _alias_values_equal(left: Any, right: Any) -> bool:
    # bool is an int subclass in Python. Treating True and 1 as equal here
    # could hide a value that the descriptor must reject as the wrong type.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    try:
        equal = left == right
    except Exception:
        return False
    return isinstance(equal, bool) and equal


def _normalize_option_aliases(family: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with known old/native option spellings consolidated."""

    normalized_family = _normalized_family_name(family)
    aliases = _AUDIO_CPP_OPTION_ALIASES.get(normalized_family, {})
    if not aliases:
        return dict(options)

    alias_pairs = tuple(aliases.items())
    alias_names = {
        name: pair_index
        for pair_index, (legacy_name, native_name) in enumerate(alias_pairs)
        for name in (legacy_name, native_name)
    }
    result: dict[str, Any] = {}
    processed_pairs: set[int] = set()
    for key, value in options.items():
        pair_index = alias_names.get(key)
        if pair_index is None:
            result[key] = value
            continue
        if pair_index in processed_pairs:
            continue
        processed_pairs.add(pair_index)
        legacy_name, native_name = alias_pairs[pair_index]
        legacy_present = legacy_name in options
        native_present = native_name in options
        legacy_value = options.get(legacy_name)
        native_value = options.get(native_name)
        legacy_nonempty = legacy_present and not _is_empty(legacy_value)
        native_nonempty = native_present and not _is_empty(native_value)

        if legacy_nonempty and native_nonempty:
            if not _alias_values_equal(legacy_value, native_value):
                raise ValueError(
                    f"Conflicting audio.cpp options '{legacy_name}' and "
                    f"'{native_name}' for family '{family}'."
                )
            selected_value = legacy_value
        elif legacy_nonempty:
            selected_value = legacy_value
        elif native_nonempty:
            selected_value = native_value
        else:
            # Empty and null values already mean "use the model default".
            continue

        result[_public_option_name(normalized_family, legacy_name)] = selected_value
    return result


def request_parameters_for_family(family: str) -> dict[str, dict[str, Any]]:
    """Return reviewed manual and safe native descriptors for a family.

    The generated catalogue describes the upstream request surface, while the
    manual registry remains authoritative for controls already wired through
    Pandrator.  Unknown, non-scalar, and non-TTS catalogue fields are
    deliberately left out of the returned mapping.
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
                if (
                    isinstance(option, Mapping)
                    and option.get("name")
                    in _TTS_UNSUPPORTED_NATIVE_OPTIONS.get(normalized, set())
                ):
                    continue
                native_option = option
                if (
                    normalized == "zipvoice"
                    and isinstance(option, Mapping)
                    and option.get("name") == "language"
                ):
                    # ZipVoice's language is a scalar option, but the generic
                    # language name is reserved for the speech request builder.
                    native_option = {**option, "name": "lang"}
                descriptor = _descriptor_from_native_option(native_option)
                if descriptor is not None:
                    native_name = descriptor.pop("_name")
                    derived[_public_option_name(normalized, native_name)] = descriptor
    if normalized == "chatterbox" and "greedy" not in derived:
        # audio.cpp still accepts this persisted v0.9.1 bool although the
        # current native inventory no longer advertises it. Do not add a default.
        derived["greedy"] = _boolean()
    for name, descriptor in manual.items():
        merged = dict(descriptor)
        native = derived.get(name, {})
        if "default" not in merged and "default" in native and _valid_native_default(
            native["default"], merged["type"], merged
        ):
            merged["default"] = native["default"]
        derived[name] = merged
    if normalized == "qwen3_tts":
        max_tokens = derived.get("max_tokens")
        if max_tokens is not None:
            max_tokens.pop("default", None)
            max_tokens["default_note"] = (
                "Selected checkpoint generation_config.json; 2048 fallback."
            )
    return derived


def request_parameters_for_model(
    family: str, model_id: str
) -> dict[str, dict[str, Any]]:
    """Return the TTS parameter projection for one exact model package."""

    parameters = request_parameters_for_family(family)
    normalized_family = str(family or "").strip().lower()
    if (
        normalized_family == "auk"
        and str(model_id or "").strip().casefold() in _AUK_FLASH_MODEL_IDS
    ):
        for name in _AUK_FLASH_BASE_ONLY_OPTIONS:
            parameters.pop(name, None)
    if normalized_family == "pocket_tts" and model_id.startswith("pocket_tts_french_"):
        parameters["temperature"].update(
            default=0.3,
            native_default=0.7,
            default_source="pandrator_policy",
            description="French uses Kyutai's 0.3 default; audio.cpp's fallback is 0.7.",
        )
    if normalized_family == "voxtral_tts":
        parameters["max_tokens"]["description"] = (
            "Generated audio frames per segment (80 ms each), not text tokens."
        )
    return parameters


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
        if descriptor.get("exclusive_minimum") is not None and value <= descriptor["exclusive_minimum"]:
            return False
        if descriptor.get("exclusive_maximum") is not None and value >= descriptor["exclusive_maximum"]:
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
    *,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize one selected model's scalar option mapping.

    ``None`` and empty strings intentionally disappear so audio.cpp can apply
    its own model defaults.  Defaults in the registry describe the server
    contract and are never injected into a request.
    """

    if not isinstance(options, Mapping):
        raise ValueError("audio.cpp model settings must be an object.")
    descriptors = (
        request_parameters_for_model(family, model_id)
        if model_id is not None
        else request_parameters_for_family(family)
    )
    normalized_family = _normalized_family_name(family)
    manual_descriptors = AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY.get(
        normalized_family, {}
    )
    result: dict[str, Any] = {}
    for key, value in _normalize_option_aliases(normalized_family, options).items():
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
    *,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility-named entry point for request builders and identity code."""

    return validate_model_options(family, options, model_id=model_id)


__all__ = [
    "AUDIO_CPP_PARAMETER_REGISTRY",
    "AUDIO_CPP_REQUEST_PARAMETERS_BY_FAMILY",
    "FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT",
    "request_parameters_for_family",
    "request_parameters_for_model",
    "validate_audio_cpp_model_options",
    "validate_model_options",
]
