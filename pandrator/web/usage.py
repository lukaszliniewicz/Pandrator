"""Aggregation helpers for provider usage displayed in the workspace UI."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import UsageEvent


def usage_summary(events: Iterable[UsageEvent]) -> dict:
    unique = {event.id: event for event in events}.values()
    total = 0.0
    priced_count = 0
    commercial = False
    unpriced_commercial = False
    estimated = False
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    model_ids: set[str] = set()
    stages: dict[str, dict] = defaultdict(
        lambda: {
            "cost_usd": 0.0,
            "event_count": 0,
            "priced_event_count": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
    )
    event_count = 0
    for event in unique:
        event_count += 1
        raw = event.raw_usage_json if isinstance(event.raw_usage_json, dict) else {}
        event_is_commercial = bool(raw.get("commercial"))
        commercial = commercial or event_is_commercial
        unpriced_commercial = unpriced_commercial or (
            event_is_commercial and event.cost_usd is None
        )
        estimated = estimated or bool(raw.get("estimated"))
        stage = str(event.stage or "other")
        event_input = int(event.input_tokens or 0)
        event_cached = int(event.cached_input_tokens or 0)
        event_output = int(event.output_tokens or 0)
        input_tokens += event_input
        cached_input_tokens += event_cached
        output_tokens += event_output
        if str(event.model_id or "").strip():
            model_ids.add(str(event.model_id))
        stages[stage]["event_count"] += 1
        stages[stage]["input_tokens"] += event_input
        stages[stage]["cached_input_tokens"] += event_cached
        stages[stage]["output_tokens"] += event_output
        if event.cost_usd is not None:
            cost = float(event.cost_usd)
            total += cost
            priced_count += 1
            stages[stage]["cost_usd"] += cost
            stages[stage]["priced_event_count"] += 1
    return {
        "total_cost_usd": total if priced_count else None,
        "event_count": event_count,
        "priced_event_count": priced_count,
        "has_unpriced_usage": unpriced_commercial,
        "commercial": commercial,
        "estimated": estimated,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "model_ids": sorted(model_ids),
        "stages": [
            {"stage": stage, **values} for stage, values in sorted(stages.items())
        ],
    }
