"""Pure request identity and finite incurred-usage values shared by native LLM work."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _request_identity(model_name: str, settings: Any) -> tuple[Any, ...]:
    from pandrator.logic import llm_handler

    details = llm_handler._resolve_model_request_details(model_name, settings)
    record = details.get("model_record")
    temperature = (
        llm_handler._model_optional_float(record.get("default_temperature"))
        if isinstance(record, dict)
        else None
    )
    if temperature is not None and temperature > 2.0:
        temperature = None
    try:
        timeout = max(10, int(getattr(settings, "request_timeout_seconds", 180)))
    except (TypeError, ValueError):
        timeout = 180
    return deepcopy(
        (
            details["model"],
            details.get("request_overrides") or {},
            temperature,
            llm_handler._request_reasoning_effort(record, settings),
            timeout,
        )
    )


def _sanitized_usage(value: Any) -> dict[str, Any]:
    """Retain only native token counts and their numeric detail maps."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_prompt_tokens",
        "uncached_prompt_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
    ):
        count = value.get(key)
        if isinstance(count, (int, float)):
            result[key] = count
    for key in (
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
        "token_details",
    ):
        details = value.get(key)
        if isinstance(details, dict):
            result[key] = {
                name: count
                for name, count in details.items()
                if name
                in {
                    "cached_tokens",
                    "cache_read_input_tokens",
                    "audio_tokens",
                    "reasoning_tokens",
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                    "text_tokens",
                }
                and isinstance(count, (int, float))
            }
    return result
