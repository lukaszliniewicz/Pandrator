"""Model-aware context budgeting for research batches."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .database import Database
from .models import Provider, ProviderModel

DEFAULT_CONTEXT_WINDOW_TOKENS = 262_144
DEFAULT_MAX_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class ContextBudget:
    model: str
    context_window_tokens: int
    max_output_tokens: int
    fraction: float
    input_budget_tokens: int


def _split_model_reference(value: str) -> tuple[str, str]:
    """Return the stored model ID and an optional provider hint.

    Pandrator's custom model references use ``custom:<provider>/<model>``.
    The provider-native model identifier may itself contain slashes, so only
    the first separator after the provider may be consumed.
    """

    model_value = str(value or "").strip()
    if model_value.casefold().startswith("custom:"):
        remainder = model_value[len("custom:") :]
        provider_hint, separator, native_model = remainder.partition("/")
        return (native_model if separator else remainder), provider_hint
    if "/" in model_value:
        provider_hint, native_model = model_value.rsplit("/", 1)
        return native_model, provider_hint
    return model_value, ""


def estimate_tokens(value: Any, model: str = "") -> int:
    """Count with LiteLLM when possible, otherwise use a conservative fallback."""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    try:
        from litellm import token_counter

        count = token_counter(model=model or None, text=text)
        if count is not None:
            return max(1, int(count))
    except Exception:  # noqa: BLE001 - tokenizer support varies per custom model
        return max(1, math.ceil(len(text) / 3))
    return max(1, math.ceil(len(text) / 3))


class ContextBudgetService:
    def __init__(self, database: Database):
        self.database = database

    def resolve(
        self,
        model: str,
        *,
        fraction: float = 0.8,
        fixed_prompt: Any = "",
        ledger: Any = "",
        tools: Any = "",
    ) -> ContextBudget:
        model_value = str(model or "")
        model_id, provider_hint = _split_model_reference(model_value)
        with self.database.session() as session:
            candidates = list(
                session.execute(
                    select(ProviderModel, Provider)
                    .join(Provider, Provider.id == ProviderModel.provider_id)
                    .where(ProviderModel.model_id == model_id)
                    .order_by(
                        ProviderModel.is_default.desc(),
                        ProviderModel.updated_at.desc(),
                    )
                ).all()
            )
        if provider_hint:
            matched = next(
                (
                    model_record
                    for model_record, provider in candidates
                    if provider_hint
                    in {
                        str(provider.id),
                        str(provider.provider_key),
                        str((provider.options_json or {}).get("provider_id") or ""),
                        str((provider.options_json or {}).get("profile_id") or ""),
                    }
                ),
                None,
            )
        else:
            matched = None
        record = matched or (candidates[0][0] if candidates else None)
        context = max(
            4_096,
            int(
                record.context_window_tokens
                if record
                else DEFAULT_CONTEXT_WINDOW_TOKENS
            ),
        )
        output = max(
            1_024,
            int(
                record.max_output_tokens
                if record and record.max_output_tokens
                else DEFAULT_MAX_OUTPUT_TOKENS
            ),
        )
        bounded_fraction = min(0.8, max(0.1, float(fraction or 0.8)))
        gross = math.floor(context * bounded_fraction)
        overhead = sum(
            estimate_tokens(value, model)
            for value in (fixed_prompt, ledger, tools)
            if value
        )
        return ContextBudget(
            model=model,
            context_window_tokens=context,
            max_output_tokens=output,
            fraction=bounded_fraction,
            input_budget_tokens=max(1_000, gross - overhead - output),
        )

    @staticmethod
    def partition(
        records: Iterable[Mapping[str, Any]],
        *,
        model: str,
        budget_tokens: int,
    ) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_tokens = 2
        for raw in records:
            record = dict(raw)
            size = estimate_tokens(record, model) + 1
            if current and current_tokens + size > budget_tokens:
                batches.append(current)
                current = []
                current_tokens = 2
            current.append(record)
            current_tokens += size
        if current:
            batches.append(current)
        return batches
