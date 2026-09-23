"""Provider-aware audiobook text chunk budgets.

The values in this module are Pandrator application policies.  They are
deliberately conservative and are not presented as a provider's acoustic
optimum.  The worker receives an immutable TTS snapshot and therefore this
module must remain a pure, local calculation: no endpoint discovery or
provider request belongs here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .tts_handler import _audio_cpp_model_metadata, _audio_cpp_selected_model_options

DEFAULT_MANUAL_LENGTH = 200
MAX_MANUAL_LENGTH = 8192
QWEN_DEFAULT_MAX_TOKENS = 2048


def _normalise_mode(value: object) -> str:
    """Return one of the supported chunking modes or the default mode."""

    if value is None or value == "":
        return "model"
    if isinstance(value, Mapping):
        value = value.get("mode")
        if value is None or value == "":
            return "model"
    mode = str(value).strip().lower().replace("-", "_")
    if mode == "model":
        return "model"
    if mode == "manual":
        return "manual"
    raise ValueError("audiobook_chunking must be 'model' or 'manual'.")


def _manual_length(value: object) -> int:
    """Validate the established positive narration length range."""

    if isinstance(value, bool) or value is None:
        raise ValueError("max_sentence_length must be a positive integer.")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("max_sentence_length must be a positive integer.")
        try:
            value = int(value)
        except ValueError as error:
            raise ValueError("max_sentence_length must be a positive integer.") from error
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("max_sentence_length must be a positive integer.")
    if int(value) != value:
        raise ValueError("max_sentence_length must be a positive integer.")
    length = int(value)
    if not 1 <= length <= MAX_MANUAL_LENGTH:
        raise ValueError(
            f"max_sentence_length must be between 1 and {MAX_MANUAL_LENGTH}."
        )
    return length


def validate_audiobook_chunking_settings(settings: Mapping[str, Any]) -> None:
    """Validate text settings that control audiobook chunk policy."""

    mode_value = settings.get("audiobook_chunking")
    mode = _normalise_mode(mode_value)
    if mode == "manual" and "max_sentence_length" in settings:
        _manual_length(settings.get("max_sentence_length"))
    elif "max_sentence_length" in settings:
        # Preserve the existing text setting's range even while model mode
        # ignores its value for automatic provider budgets.
        _manual_length(settings.get("max_sentence_length"))


def _normalised_service(settings: Mapping[str, Any]) -> str:
    value = str(
        settings.get("service")
        or settings.get("tts_service")
        or settings.get("provider")
        or ""
    )
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _selected_adapter(settings: Mapping[str, Any]) -> str:
    for key in ("adapter", "tts_adapter", "provider_adapter"):
        value = str(settings.get(key) or "").strip().casefold().replace("-", "_")
        if value:
            return value
    selected_endpoint = str(settings.get("openai_audio_endpoint") or "").strip()
    if not selected_endpoint:
        return ""
    for key in ("provider_configs", "service_configs"):
        records = settings.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            record_id = str(record.get("id") or record.get("name") or "").strip()
            if record_id.casefold() == selected_endpoint.casefold():
                return str(record.get("adapter") or "").strip().casefold().replace(
                    "-", "_"
                )
    return ""


def _model_name(settings: Mapping[str, Any]) -> str:
    return str(settings.get("xtts_model") or settings.get("model") or "").strip()


def _audio_cpp_family_and_model(settings: Mapping[str, Any]) -> tuple[str, str]:
    model = _model_name(settings)
    metadata = _audio_cpp_model_metadata(model, dict(settings)) if model else {}
    family = str(metadata.get("family") or "").strip().casefold()
    return family, model


def _audio_cpp_max_tokens(
    settings: Mapping[str, Any], family: str, model: str
) -> tuple[int | None, str]:
    """Read max_tokens with the same model-map/scalar precedence as runtime."""

    model_settings = settings.get("audio_cpp_model_settings")
    if isinstance(model_settings, Mapping) and isinstance(
        model_settings.get(model), Mapping
    ):
        selected = _audio_cpp_selected_model_options(dict(settings), model, family)
        if selected is not None:
            value = selected.get("max_tokens")
            return (_positive_integer(value), "selected model settings")

    if "audio_cpp_max_tokens" in settings:
        return (_positive_integer(settings.get("audio_cpp_max_tokens")), "scalar")
    for key in ("audio_cpp_options", "options"):
        options = settings.get(key)
        if isinstance(options, Mapping) and "max_tokens" in options:
            return (_positive_integer(options.get("max_tokens")), key)
    return None, "default"


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    if int(value) != value or int(value) <= 0:
        return None
    return int(value)


def _language_is_cjk(language: str) -> bool:
    base = str(language or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return base in {"zh", "ja", "ko"}


def _provider_profile(settings: Mapping[str, Any]) -> tuple[str, int, int | None, str]:
    """Return profile, policy limit, verified input limit, and explanation."""

    service = _normalised_service(settings)
    adapter = _selected_adapter(settings)
    model = _model_name(settings).casefold()
    family, _model = _audio_cpp_family_and_model(settings)

    is_audio_cpp = (
        service in {"audio_cpp", "audio.cpp", "audiocpp"}
        or adapter == "audio_cpp"
        or str(settings.get("provider") or "").strip().casefold() == "audio_cpp"
    )
    if is_audio_cpp and family == "qwen3_tts":
        return "qwen3_tts", 2000, 8192, "audio.cpp Qwen application policy"
    if family == "voxcpm2" or service in {"voxcpm", "voxcpm2"}:
        return "voxcpm2", 2000, 2048, "VoxCPM2 application policy"
    if family in {"fish_audio", "fish_audio_s2"} or service in {
        "fishs2",
        "fish_audio",
        "fish_audio_s2",
    }:
        return "fish_audio_s2", 200, None, "Fish application policy"
    if "kokoro" in model or service in {"kokoro", "kokoro_tts"}:
        return "kokoro", 240, None, "Kokoro application policy"
    if model in {"gpt-4o-mini-tts", "tts-1", "tts-1-hd"}:
        return "openai", 4000, 4096, "OpenAI TTS application policy"
    if model.startswith("gemini-") or service in {
        "gemini",
        "google_gemini",
        "google_gemini_tts",
        "vertex_ai",
        "google_vertex_ai",
    }:
        return "gemini", 2000, None, "Gemini application policy"
    if adapter in {"azure_speech", "native_azure_speech", "nativeazurespeech"} or service in {
        "azure",
        "azure_speech",
        "azurespeech",
        "native_azure_speech",
        "nativeazurespeech",
    }:
        return "azure_speech", 4000, None, "native Azure Speech application policy"
    if service in {
        "qwen3_tts",
        "kobold_qwen",
        "qwen",
        "qwen3",
    } or "qwen" in model:
        return "qwen3_tts", 2000, 8192, "Qwen application policy"
    if service in {"xtts", "xtts_v2", "xtts2", "pandrator_xtts2_api"} or "xtts" in model:
        return "xtts", 200, None, "XTTS application policy"
    return "unknown", 300, None, "unknown provider/family fallback policy"


def audiobook_chunk_budget(
    text_settings: Mapping[str, Any] | None,
    tts_settings: Mapping[str, Any] | None,
    language: str = "en",
) -> dict[str, Any]:
    """Resolve the immutable text chunk budget for one audiobook prepare run."""

    text = text_settings if isinstance(text_settings, Mapping) else {}
    tts = tts_settings if isinstance(tts_settings, Mapping) else {}
    mode = (
        "manual"
        if "audiobook_chunking" not in text and "max_sentence_length" in text
        else _normalise_mode(text.get("audiobook_chunking"))
    )
    profile, policy_limit, input_limit, policy_reason = _provider_profile(tts)
    cjk_qwen = profile == "qwen3_tts" and _language_is_cjk(language)
    if cjk_qwen:
        policy_limit = 500
    if mode == "manual":
        value = text.get("max_sentence_length", DEFAULT_MANUAL_LENGTH)
        target = _manual_length(value)
        reason = "manual max_sentence_length"
        if input_limit is not None and target > input_limit:
            target = input_limit
            reason += f"; clamped to verified {input_limit}-character input limit"
        return {
            "mode": mode,
            "profile": profile,
            "target_chars": target,
            "policy_limit_chars": policy_limit,
            "input_limit_chars": input_limit,
            "reason": reason,
        }

    target = (
        policy_limit
        if profile in {"xtts", "unknown"}
        else math.floor(policy_limit * 0.9)
    )
    reason = policy_reason
    if profile == "qwen3_tts" and input_limit == 8192:
        family, model = _audio_cpp_family_and_model(tts)
        if _normalised_service(tts) in {"audio_cpp", "audio.cpp", "audiocpp"} or _selected_adapter(tts) == "audio_cpp":
            configured_tokens, source = _audio_cpp_max_tokens(tts, family, model)
            effective_tokens = configured_tokens or QWEN_DEFAULT_MAX_TOKENS
            if effective_tokens < QWEN_DEFAULT_MAX_TOKENS:
                target = math.floor(
                    target * effective_tokens / QWEN_DEFAULT_MAX_TOKENS
                )
                reason += f"; scaled to {effective_tokens} configured max_tokens"
            elif configured_tokens is None:
                reason += "; app policy assumes 2048 max_tokens when unconfigured"
            elif source != "default":
                reason += f"; using {effective_tokens} configured max_tokens"
    if cjk_qwen:
        reason += "; CJK policy"
    return {
        "mode": mode,
        "profile": profile,
        "target_chars": max(1, target),
        "policy_limit_chars": policy_limit,
        "input_limit_chars": input_limit,
        "reason": reason,
    }


__all__ = [
    "DEFAULT_MANUAL_LENGTH",
    "MAX_MANUAL_LENGTH",
    "audiobook_chunk_budget",
    "validate_audiobook_chunking_settings",
]
