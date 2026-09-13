"""Display-only projection of logical subtitle passages."""

from __future__ import annotations

from math import ceil
from typing import Any

from .models import SubtitleSegment
from .subtitle_finalization import SubtitleFinalizationConfig, finalize_segments
from .subtitle_rebalancing import rebalance_display_cues


def project_subtitle_display(
    values: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    timing_words: list[dict[str, Any]] | None = None,
    match_source_words: bool = False,
) -> list[dict[str, Any]]:
    """Fit display text, with bounded local reflow for inherited cue fragments.

    An English display boundary is not a reliable German phrase boundary.
    Reflow at most three adjacent intervals, within three maximum cue durations,
    only when needed for readability or contradictory same-speaker overlaps.
    Speaker changes, substantial pauses and review metadata remain boundaries.
    """
    config = SubtitleFinalizationConfig.from_settings(settings)
    if not values:
        return []
    groups = [
        (dict(item), 1) for item in sorted(values, key=lambda v: int(v["start_ms"]))
    ]
    timeline_end = max(int(item["end_ms"]) for item in values)

    def render(item: dict[str, Any]) -> list[dict[str, Any]]:
        source_start, source_end = int(item["start_ms"]), int(item["end_ms"])
        cues = finalize_segments(
            [
                SubtitleSegment(
                    0,
                    source_start,
                    source_end,
                    str(item["text"]),
                    str(item.get("speaker") or ""),
                )
            ],
            config,
        )
        result = []
        for cue in cues:
            end_ms = min(cue.end_ms, source_end)
            if end_ms <= cue.start_ms:
                raise ValueError("Edited text cannot fit within its subtitle interval.")
            result.append(
                {**item, "start_ms": cue.start_ms, "end_ms": end_ms, "text": cue.text}
            )
        return result

    def quality(items: list[dict[str, Any]]) -> tuple[int, float, int]:
        deficits = []
        speeds = []
        for item in items:
            duration = int(item["end_ms"]) - int(item["start_ms"])
            chars = len(" ".join(str(item["text"]).split()))
            needed = max(
                config.min_duration_ms, ceil(chars * 1000 / config.max_chars_per_second)
            )
            deficits.append(max(0, needed - duration))
            speeds.append(chars * 1000 / duration)
        return sum(d > 1 for d in deficits), max(speeds, default=0), sum(deficits)

    def signature(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("speaker"),
            item.get("review_state") or "clear",
            item.get("review_note") or "",
            tuple(item.get("evidence_ids") or []),
            tuple(item.get("uncertain_source_cue_ids") or []),
        )

    index = 0
    while index < len(groups):
        item, count = groups[index]
        current = render(item)
        # Reading time may use a little genuinely empty trailing space, but
        # never a following cue's speech, or time beyond the source timeline.
        deficit = quality(current)[2]
        next_start = (
            int(groups[index + 1][0]["start_ms"])
            if index + 1 < len(groups)
            else timeline_end + config.min_gap_ms
        )
        available_end = min(
            timeline_end,
            next_start - config.min_gap_ms,
            int(item["end_ms"]) + config.phrase_gap_ms,
        )
        if deficit and available_end > int(item["end_ms"]):
            candidate = {
                **item,
                "end_ms": min(available_end, int(item["end_ms"]) + deficit),
            }
            if quality(render(candidate)) < quality(current):
                item = candidate
                groups[index] = (item, count)
                current = render(item)

        candidates = []
        for left in (index - 1, index):
            if left < 0 or left + 1 >= len(groups):
                continue
            a, ac = groups[left]
            b, bc = groups[left + 1]
            gap = int(b["start_ms"]) - int(a["end_ms"])
            span = max(int(a["end_ms"]), int(b["end_ms"])) - int(a["start_ms"])
            if (
                not a.get("speaker")
                or signature(a) != signature(b)
                or gap > config.phrase_gap_ms
                or ac + bc > 3
                or span > config.max_duration_ms * 3
            ):
                continue
            before = quality(render(a) + render(b))
            if gap >= 0 and not before[0]:
                continue
            joined = {
                **a,
                "end_ms": max(int(a["end_ms"]), int(b["end_ms"])),
                "text": " ".join((str(a["text"]), str(b["text"]))).strip(),
            }
            after = quality(render(joined))
            if gap < 0 or after < before:
                candidates.append((after, left, joined, ac + bc))
        if candidates:
            _score, left, joined, members = min(candidates, key=lambda x: (x[0], x[1]))
            groups[left : left + 2] = [(joined, members)]
            index = max(0, left - 1)
        else:
            index += 1

    output = [cue for item, _count in groups for cue in render(item)]
    return rebalance_display_cues(
        sorted(output, key=lambda item: int(item["start_ms"])),
        config,
        timing_words=timing_words,
        match_source_words=match_source_words,
    )
