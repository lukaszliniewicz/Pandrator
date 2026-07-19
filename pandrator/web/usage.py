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
    stages: dict[str, dict] = defaultdict(lambda: {"cost_usd": 0.0, "event_count": 0, "priced_event_count": 0})
    event_count = 0
    for event in unique:
        event_count += 1
        raw = event.raw_usage_json if isinstance(event.raw_usage_json, dict) else {}
        event_is_commercial = bool(raw.get("commercial"))
        commercial = commercial or event_is_commercial
        unpriced_commercial = unpriced_commercial or (event_is_commercial and event.cost_usd is None)
        estimated = estimated or bool(raw.get("estimated"))
        stage = str(event.stage or "other")
        stages[stage]["event_count"] += 1
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
        "stages": [
            {"stage": stage, **values}
            for stage, values in sorted(stages.items())
        ],
    }
