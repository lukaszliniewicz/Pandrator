"""Guard every speech completion attempt and record incurred finite response values."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, NoReturn

from pandrator.runtime import DataPaths

from .database import Database
from .llm_effects import _request_identity, _sanitized_usage
from .provider_settings import build_llm_settings

_CONNECTION_CHANGED = "The speech-optimization model connection is no longer available."


def build_speech_optimization_completion(
    database: Database,
    paths: DataPaths,
    session_id: str,
    model_name: str,
    request_timeout_seconds: int,
    prove: Callable[[], None],
    record_response: Callable[..., None],
    reject: Callable[[Exception], Any],
    *,
    cancel_event: Any = None,
) -> Callable[..., Any]:
    frozen_model = model_name

    def unavailable() -> NoReturn:
        error = ValueError(_CONNECTION_CHANGED)
        reject(error)
        raise error

    def current_settings() -> Any:
        prove()
        try:
            settings, selected = build_llm_settings(
                database,
                paths,
                requested_model=frozen_model,
                request_timeout_seconds=request_timeout_seconds,
                session_id=session_id,
            )
        except ValueError:
            unavailable()
        if selected != frozen_model:
            unavailable()
        prove()
        return settings

    initial_settings = current_settings()
    try:
        approved_identity = _request_identity(frozen_model, initial_settings)
    except ValueError:
        unavailable()

    def complete(*, messages: list[dict[str, Any]], model_name: str, llm_settings: Any) -> Any:
        from pandrator.logic import llm_handler

        if model_name != frozen_model:
            unavailable()
        settings = current_settings()
        try:
            identity = _request_identity(frozen_model, settings)
            details = llm_handler._resolve_model_request_details(frozen_model, settings)
        except ValueError:
            unavailable()
        if identity != approved_identity:
            unavailable()

        def before_request() -> None:
            fresh = current_settings()
            try:
                fresh_identity = _request_identity(frozen_model, fresh)
            except ValueError:
                unavailable()
            if fresh_identity != identity:
                unavailable()
            prove()

        def response_received(response: Any) -> None:
            raw_cost = response.cost
            cost = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float)) and math.isfinite(raw_cost)
                else None
            )
            allowed_sources = {
                "litellm_hidden_params",
                "response_usage",
                "litellm_response_header",
                "litellm_completion_cost",
                "custom_model_pricing",
            }
            cost_source = (
                ",".join(
                    dict.fromkeys(
                        value
                        for value in str(response.cost_source or "").split(",")
                        if value in allowed_sources
                    )
                )
                or None
            )
            usage = _sanitized_usage(response.usage)
            for key, value in list(usage.items()):
                if isinstance(value, dict):
                    usage[key] = {
                        name: count for name, count in value.items() if math.isfinite(count)
                    }
                elif not math.isfinite(value):
                    del usage[key]
            record_response(
                usage=usage,
                provider_key=str(details["provider_key"]),
                model_id=str(details["model"]),
                cost=cost,
                cost_source=cost_source,
            )

        return llm_handler.chat_completion_with_metadata(
            messages=messages,
            model_name=frozen_model,
            llm_settings=settings,
            cancel_event=cancel_event,
            before_request=before_request,
            response_callback=response_received,
        )

    return complete
