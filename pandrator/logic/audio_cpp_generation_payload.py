"""Strict request canonicalization and argv-only audio.cpp generation compiler."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from numbers import Real
from pathlib import Path
from typing import Any

from .audio_cpp_generation_models import default_kind, get_model, seed_maximum

_REQUEST_FIELDS = frozenset(
    {
        "model_id",
        "prompt",
        "duration_seconds",
        "seed",
        "threads",
        "backend",
        "kind",
        "lyrics",
        "parameters",
        # Backwards-compatible aliases for the established SFX request shape.
        "num_inference_steps",
        "guidance_scale",
    }
)
_NEW_STYLE_FIELDS = frozenset({"kind", "lyrics", "parameters"})
_MAX_PROMPT_LENGTH = 8000
_MAX_LYRICS_LENGTH = 16000
_MAX_PARAMETER_COUNT = 32
_MAX_PARAMETER_KEY_LENGTH = 64
_MAX_PARAMETER_STRING_LENGTH = 2000


def _logical_cpu_count() -> int:
    count = os.cpu_count()
    return count if isinstance(count, int) and count > 0 else 1


def _integer(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}.")
    return value


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite number.")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number.") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number.")
    return result


def _validate_parameter_value(
    name: str, value: Any, descriptor: dict[str, Any]
) -> Any:
    field = f"parameters.{name}"
    expected_type = descriptor["type"]
    if expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer.")
        normalized: Any = value
    elif expected_type == "number":
        normalized = _finite_number(value, field=field)
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be a boolean.")
        normalized = value
    elif expected_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string.")
        if len(value) > _MAX_PARAMETER_STRING_LENGTH:
            raise ValueError(
                f"{field} must be at most {_MAX_PARAMETER_STRING_LENGTH} characters."
            )
        normalized = value
    else:
        raise ValueError(f"Unsupported parameter type for {name!r}.")

    minimum = descriptor.get("minimum")
    maximum = descriptor.get("maximum")
    if minimum is not None and normalized < minimum:
        raise ValueError(f"{field} must be at least {minimum}.")
    exclusive_minimum = descriptor.get("exclusive_minimum")
    if exclusive_minimum is not None and normalized <= exclusive_minimum:
        if exclusive_minimum == 0:
            raise ValueError(f"{field} must be greater than zero.")
        raise ValueError(f"{field} must be greater than {exclusive_minimum}.")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"{field} must be at most {maximum}.")
    options = descriptor.get("options")
    if options is not None and normalized not in options:
        raise ValueError(f"{field} must be one of {', '.join(options)}.")
    return normalized


def validate_request(
    payload: dict[str, Any], *, resolved_seed: int | None = None
) -> dict[str, Any]:
    """Validate and normalize a public generation request.

    Old SFX callers keep the exact established canonical keys when they do not
    supply any of the newer ``kind``, ``lyrics`` or ``parameters`` fields.
    """
    if not isinstance(payload, dict):
        raise ValueError("Audio.cpp generation request must be an object.")
    unknown = [field for field in payload if field not in _REQUEST_FIELDS]
    if unknown:
        if payload.get("model_id", get_model("stable_audio_3_small_sfx_q8_0").id) == get_model(
            "stable_audio_3_small_sfx_q8_0"
        ).id:
            raise ValueError(
                f"Unknown Stable Audio SFX request field: {unknown[0]!r}."
            )
        raise ValueError(f"Unknown audio.cpp generation request field: {unknown[0]!r}.")

    model_id = payload.get("model_id", get_model("stable_audio_3_small_sfx_q8_0").id)
    if not isinstance(model_id, str):
        raise ValueError("model_id must be a string.")
    try:
        spec = get_model(model_id)
    except ValueError as error:
        raise ValueError("model_id must be a registered audio.cpp generation model.") from error
    legacy_sfx = (
        model_id == "stable_audio_3_small_sfx_q8_0"
        and not (_NEW_STYLE_FIELDS & payload.keys())
    )

    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string.")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt must not be empty.")
    prompt_limit = 2000 if legacy_sfx else _MAX_PROMPT_LENGTH
    if len(prompt) > prompt_limit:
        raise ValueError(f"prompt must be at most {prompt_limit} characters.")

    raw_duration = payload.get("duration_seconds", spec.duration_default)
    duration_seconds = _finite_number(raw_duration, field="duration_seconds")
    if not spec.duration_minimum <= duration_seconds <= spec.duration_maximum:
        raise ValueError(
            "duration_seconds must be between "
            f"{spec.duration_minimum:g} and {spec.duration_maximum:g} seconds."
        )

    if "seed" in payload:
        raw_seed = payload["seed"]
    elif resolved_seed is not None:
        raw_seed = resolved_seed
    else:
        raise ValueError("seed is required unless a resolved_seed is supplied.")
    seed = _integer(raw_seed, field="seed", minimum=0, maximum=seed_maximum(model_id))

    raw_threads = payload.get("threads", min(4, _logical_cpu_count()))
    threads = _integer(raw_threads, field="threads", minimum=1, maximum=_logical_cpu_count())

    raw_backend = payload.get("backend", spec.backends[0])
    if not isinstance(raw_backend, str) or raw_backend.strip().lower() not in {
        "cpu",
        "vulkan",
        "cuda",
    }:
        raise ValueError("backend must be cpu, vulkan or cuda.")
    backend = raw_backend.strip().lower()

    if legacy_sfx and backend != "cpu":
        raise ValueError("Stable Audio SFX legacy requests support only the cpu backend.")
    if backend not in spec.backends:
        raise ValueError(
            f"backend for {model_id} must be one of {', '.join(spec.backends)}."
        )

    raw_lyrics = payload.get("lyrics", "")
    if not isinstance(raw_lyrics, str):
        raise ValueError("lyrics must be a string.")
    lyrics = raw_lyrics.strip()
    if len(lyrics) > _MAX_LYRICS_LENGTH:
        raise ValueError(f"lyrics must be at most {_MAX_LYRICS_LENGTH} characters.")
    if spec.requires_lyrics and not lyrics:
        raise ValueError(f"{model_id} requires lyrics.")
    if lyrics and not spec.supports_lyrics:
        raise ValueError(f"{model_id} does not support a separate lyrics field.")

    raw_kind = payload.get("kind", default_kind(model_id))
    if not isinstance(raw_kind, str) or raw_kind not in spec.kinds:
        raise ValueError(
            f"kind for {model_id} must be one of {', '.join(spec.kinds)}."
        )

    descriptors = spec.parameter_descriptors()
    raw_parameters = payload.get("parameters", {})
    if not isinstance(raw_parameters, dict):
        raise ValueError("parameters must be an object.")
    if len(raw_parameters) > _MAX_PARAMETER_COUNT:
        raise ValueError(f"parameters may contain at most {_MAX_PARAMETER_COUNT} entries.")
    parameters: dict[str, Any] = {}
    for key, value in raw_parameters.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_PARAMETER_KEY_LENGTH:
            raise ValueError(
                f"parameter keys must be nonempty strings of at most {_MAX_PARAMETER_KEY_LENGTH} characters."
            )
        if key not in descriptors:
            raise ValueError(f"Unknown parameter for {model_id}: {key!r}.")
        parameters[key] = _validate_parameter_value(
            key, value, descriptors[key]
        )

    if legacy_sfx:
        for name, expected in (("num_inference_steps", 8), ("guidance_scale", 1.0)):
            if name not in payload:
                continue
            value = payload[name]
            if isinstance(value, bool) or not isinstance(value, Real) or value != expected:
                raise ValueError(f"{name} is fixed at {expected}.")
        return {
            "model_id": model_id,
            "prompt": prompt,
            "duration_seconds": duration_seconds,
            "seed": seed,
            "threads": threads,
            "backend": "cpu",
            "num_inference_steps": 8,
            "guidance_scale": 1.0,
        }

    aliases: dict[str, Any] = {}
    for name in ("num_inference_steps", "guidance_scale"):
        if name not in payload:
            continue
        if name not in descriptors:
            raise ValueError(f"{name} is not supported by {model_id}.")
        alias_value = _validate_parameter_value(
            name, payload[name], descriptors[name]
        )
        if name in parameters and parameters[name] != alias_value:
            raise ValueError(
                f"Conflicting {name} values were supplied in parameters and at the top level."
            )
        aliases[name] = alias_value
    parameters.update(aliases)

    for name, descriptor in descriptors.items():
        if name not in parameters and "default" in descriptor:
            parameters[name] = _validate_parameter_value(
                name, descriptor["default"], descriptor
            )

    return {
        "model_id": model_id,
        "prompt": prompt,
        "duration_seconds": duration_seconds,
        "seed": seed,
        "threads": threads,
        "backend": backend,
        "kind": raw_kind,
        "lyrics": lyrics,
        "parameters": parameters,
    }


def _number(value: int | float) -> str:
    return format(float(value), ".15g")


def _request_option(command: list[str], name: str, value: Any) -> None:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value) if isinstance(value, int) else _number(value) if isinstance(value, float) else value
    command.extend(("--request-option", f"{name}={rendered}"))


def _ace_variant(model_id: str) -> str:
    if "xl_turbo" in model_id:
        return "acestep-v15-xl-turbo"
    if "xl_sft" in model_id:
        return "acestep-v15-xl-sft"
    if "turbo" in model_id:
        return "acestep-v15-turbo"
    return "acestep-v15-base"


def _music3_component(spec, prefix: str) -> str:
    matches = [
        Path(item.path).name
        for item in spec.files
        if Path(item.path).name.startswith(prefix)
        and Path(item.path).name.endswith(".gguf")
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Pinned MiniMax Music 3 manifest must contain one {prefix} GGUF component."
        )
    return matches[0]


def build_argv(
    executable: str | Path,
    request: Mapping[str, Any],
    model_path: str | Path,
    output_path: str | Path,
) -> list[str]:
    """Compile a canonical request into argv; never invokes a shell."""
    canonical = validate_request(dict(request))
    model_id = canonical["model_id"]
    spec = get_model(model_id)
    parameters = canonical.get("parameters", {})
    command = [
        str(executable),
        "--task",
        "gen",
        "--family",
        spec.family,
        "--model",
        str(model_path),
        "--backend",
        canonical["backend"],
        "--threads",
        str(canonical["threads"]),
    ]
    if spec.family != "yue2":
        command.extend(("--text", canonical["prompt"]))
    command.extend(("--seed", str(canonical["seed"])))

    if spec.family == "stable_audio":
        command.extend(("--duration-seconds", _number(canonical["duration_seconds"])))
        command.extend(
            (
                "--num-inference-steps",
                str(parameters.get("num_inference_steps", 8)),
                "--guidance-scale",
                _number(parameters.get("guidance_scale", 1.0)),
            )
        )
        for name in ("negative_prompt", "sampler"):
            if name in parameters:
                _request_option(command, name, parameters[name])
    elif spec.family == "controlfoley":
        _request_option(command, "duration_sec", canonical["duration_seconds"])
        for name in ("num_inference_steps", "guidance_scale", "negative_prompt"):
            if name in parameters:
                _request_option(command, name, parameters[name])
        _request_option(command, "seed", canonical["seed"])
    elif spec.family == "midashenglm_gen":
        prompt = canonical["prompt"]
        if not any(tag in prompt for tag in ("<|caption|>", "<|sfx|>", "<|music|>")):
            task_tag = "sfx" if canonical["kind"] == "sound" else "music"
            prompt = f"<|caption|><|{task_tag}|> {prompt}"
            command[command.index("--text") + 1] = prompt
        _request_option(command, "duration_sec", canonical["duration_seconds"])
        for name in ("guidance_scale", "stop_threshold", "min_stop_step"):
            if name in parameters:
                _request_option(command, name, parameters[name])
        _request_option(command, "seed", canonical["seed"])
    elif spec.family == "ace_step":
        if canonical["lyrics"]:
            command.extend(("--lyrics", canonical["lyrics"]))
        ace_steps = parameters.pop("num_inference_steps")
        command.extend(
            (
                "--duration-seconds",
                _number(canonical["duration_seconds"]),
                "--num-inference-steps",
                str(ace_steps),
                "--task-route",
                "text2music",
                "--language",
                parameters["language"],
                "--load-option",
                f"ace_step.dit_model_path={_ace_variant(model_id)}",
            )
        )
        if "guidance_scale" in parameters:
            command.extend(("--guidance-scale", _number(parameters["guidance_scale"])))
        for name, value in parameters.items():
            if name not in {"guidance_scale", "language"}:
                _request_option(command, name, value)
    elif spec.family == "heartmula":
        if canonical["lyrics"]:
            command.extend(("--lyrics", canonical["lyrics"]))
        command.extend(("--duration-seconds", _number(canonical["duration_seconds"])))
        for name, value in parameters.items():
            _request_option(command, name, value)
    elif spec.family == "minimax_music3":
        _request_option(command, "lyrics", canonical["lyrics"])
        _request_option(command, "duration_sec", canonical["duration_seconds"])
        for name, value in parameters.items():
            _request_option(command, name, value)
        _request_option(command, "seed", canonical["seed"])
        command.extend(
            (
                "--session-option",
                f"minimax_music3.language_model_gguf={_music3_component(spec, 'language_model_')}",
                "--session-option",
                f"minimax_music3.rvq_depth_decoder_gguf={_music3_component(spec, 'rvq_depth_decoder_')}",
                "--session-option",
                f"minimax_music3.flow_transformer_gguf={_music3_component(spec, 'transformer_')}",
            )
        )
    elif spec.family == "yue2":
        if canonical["lyrics"]:
            command.extend(("--lyrics", canonical["lyrics"]))
        _request_option(command, "style", canonical["prompt"])
        parameter_names = {
            "num_inference_steps": "steps",
            "guidance_scale": "guidance_scale",
            "cot": "cot",
            "semantic_temperature": "semantic_temperature",
            "semantic_top_p": "semantic_top_p",
            "semantic_top_k": "semantic_top_k",
            "semantic_repetition_penalty": "semantic_repetition_penalty",
        }
        for public_name, native_name in parameter_names.items():
            _request_option(command, native_name, parameters[public_name])
        _request_option(
            command,
            "semantic_max_tokens",
            math.ceil(canonical["duration_seconds"] * 25),
        )
        _request_option(command, "semantic_min_tokens", 200)
        main_ggufs = [
            Path(item.path).name
            for item in spec.files
            if Path(item.path).name.startswith("yue2-3b-")
            and Path(item.path).name.endswith(".gguf")
        ]
        if len(main_ggufs) != 1 or "yue2-vae-f16.gguf" not in {
            Path(item.path).name for item in spec.files
        }:
            raise ValueError("Pinned Yue 2 package must include its exact main and F16 VAE GGUFs.")
        command.extend(
            (
                "--session-option",
                f"yue2.model_gguf={main_ggufs[0]}",
                "--session-option",
                "yue2.vae_gguf=yue2-vae-f16.gguf",
            )
        )
    elif spec.family == "minimax_h3":
        _request_option(command, "steps", parameters["num_inference_steps"])
        for name in ("guidance_scale", "flow_shift", "audio_flow_shift"):
            _request_option(command, name, parameters[name])
        requested_frames = max(5, math.ceil(canonical["duration_seconds"] * 24))
        aligned_frames = ((requested_frames - 5 + 16) // 17) * 17 + 5
        _request_option(command, "num_frames", aligned_frames)
        _request_option(command, "return_video", False)

    command.extend(
        (
            "--out",
            str(Path(output_path).resolve(strict=False)),
            "--out-format",
            "pcm16",
        )
    )
    return command


__all__ = ["build_argv", "seed_maximum", "validate_request"]
