"""Current native-cleaning request authority and per-response incurred usage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pandrator.runtime import DataPaths

from .database import Database
from .llm_effects import _request_identity as _request_identity
from .llm_effects import _sanitized_usage as _sanitized_usage
from .models import UsageEvent
from .provider_settings import build_llm_settings
from .usage_identity import bind_usage_ownership

_CONNECTION_CHANGED = "The source-cleaning model connection is no longer available."




def build_source_cleaning_completion(
    database: Database,
    paths: DataPaths,
    session_id: str,
    model_name: str,
    request_timeout_seconds: int,
    prove: Callable[[], None],
    job_id: str | None = None,
    cancel_event: Any | None = None,
) -> Callable[..., Any]:
    """Freeze model selection while rechecking each application-managed attempt."""
    frozen_model = model_name

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
            raise ValueError(_CONNECTION_CHANGED) from None
        if selected != frozen_model:
            raise ValueError(_CONNECTION_CHANGED)
        prove()
        return settings

    def complete(*, messages: list[dict[str, Any]], model_name: str, llm_settings: Any) -> Any:
        from pandrator.logic import llm_handler

        if model_name != frozen_model:
            raise ValueError(_CONNECTION_CHANGED)
        settings = current_settings()
        try:
            identity = _request_identity(frozen_model, settings)
            details = llm_handler._resolve_model_request_details(frozen_model, settings)
        except ValueError:
            raise ValueError(_CONNECTION_CHANGED) from None
        provider_key = str(details["provider_key"])

        def before_request() -> None:
            fresh = current_settings()
            try:
                fresh_identity = _request_identity(frozen_model, fresh)
            except ValueError:
                raise ValueError(_CONNECTION_CHANGED) from None
            if fresh_identity != identity:
                raise ValueError(_CONNECTION_CHANGED)
            prove()

        def record_response(response: Any) -> None:
            usage = _sanitized_usage(response.usage)
            detail = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            with database.immediate_session() as unit:
                event = UsageEvent(
                    session_id=session_id,
                    job_id=job_id,
                    stage="source_cleaning",
                    provider_key=provider_key,
                    model_id=str(response.model or details["model"]),
                    input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                    cached_input_tokens=int(
                        usage.get("cached_prompt_tokens") or detail.get("cached_tokens") or 0
                    ),
                    output_tokens=int(
                        usage.get("completion_tokens") or usage.get("output_tokens") or 0
                    ),
                    cost_usd=response.cost,
                    cost_source=response.cost_source or None,
                    raw_usage_json=usage,
                )
                bind_usage_ownership(unit, event)
                unit.add(event)

        return llm_handler.chat_completion_with_metadata(
            messages=messages,
            model_name=frozen_model,
            llm_settings=settings,
            cancel_event=cancel_event,
            before_request=before_request,
            response_callback=record_response,
        )

    return complete
