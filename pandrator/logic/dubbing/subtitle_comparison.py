"""Subtitle comparison and timing-based lineage helpers."""

from __future__ import annotations

import json
import os
from typing import Any

from .srt_utils import parse_srt


def _segments(path: str) -> list[Any]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as handle:
        return parse_srt(handle.read())


def _overlap(left: Any, right: Any) -> int:
    return max(0, min(int(left.end_ms), int(right.end_ms)) - max(int(left.start_ms), int(right.start_ms)))


def build_lineage(parent_path: str, child_path: str, output_path: str = "") -> dict[str, Any]:
    parents = _segments(parent_path)
    children = _segments(child_path)
    edges = []
    for parent_index, parent in enumerate(parents):
        matches = [
            child_index
            for child_index, child in enumerate(children)
            if _overlap(parent, child) > 0
        ]
        if matches:
            edges.append({"parent": parent_index, "children": matches})
    payload = {
        "parent_path": os.path.abspath(parent_path) if parent_path else "",
        "child_path": os.path.abspath(child_path) if child_path else "",
        "edges": edges,
    }
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    return payload


def _load_lineage(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def comparison_rows(
    paths: dict[str, str], lineage_paths: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Group related segments, preferring recorded lineage over timing."""
    stage_segments = {
        stage: _segments(path)
        for stage, path in paths.items()
        if stage in {"source", "corrected", "translated"} and path
    }
    nodes = [
        (stage, index)
        for stage, segments in stage_segments.items()
        for index in range(len(segments))
    ]
    parent = {node: node for node in nodes}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        if left not in parent or right not in parent:
            return
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    lineage_paths = dict(lineage_paths or {})
    paired_with_lineage: set[tuple[str, str]] = set()
    for child_stage in ("corrected", "translated"):
        payload = _load_lineage(str(lineage_paths.get(child_stage) or ""))
        if not payload:
            continue
        recorded_parent_path = os.path.abspath(str(payload.get("parent_path") or ""))
        parent_stage = next(
            (
                stage
                for stage, path in paths.items()
                if stage in stage_segments
                and os.path.abspath(str(path or "")) == recorded_parent_path
            ),
            "source" if child_stage == "corrected" else "corrected",
        )
        paired_with_lineage.add((parent_stage, child_stage))
        for edge in payload.get("edges") or []:
            try:
                parent_index = int(edge.get("parent"))
            except (TypeError, ValueError, AttributeError):
                continue
            for child_index in edge.get("children") or []:
                try:
                    union((parent_stage, parent_index), (child_stage, int(child_index)))
                except (TypeError, ValueError):
                    continue

    stages = [stage for stage in ("source", "corrected", "translated") if stage in stage_segments]
    for left_stage, right_stage in zip(stages, stages[1:]):
        if (left_stage, right_stage) in paired_with_lineage:
            continue
        for left_segment_index, left_segment in enumerate(stage_segments[left_stage]):
            for right_segment_index, right_segment in enumerate(stage_segments[right_stage]):
                if _overlap(left_segment, right_segment) > 0:
                    union(
                        (left_stage, left_segment_index),
                        (right_stage, right_segment_index),
                    )

    groups: dict[Any, list[tuple[str, int]]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    rows: list[dict[str, Any]] = []
    for members in groups.values():
        member_segments = [stage_segments[stage][index] for stage, index in members]
        row: dict[str, Any] = {
            "start_ms": min(int(segment.start_ms) for segment in member_segments),
            "end_ms": max(int(segment.end_ms) for segment in member_segments),
        }
        for stage in stages:
            texts = []
            for member_stage, index in sorted(members, key=lambda item: item[1]):
                if member_stage != stage:
                    continue
                text = str(stage_segments[stage][index].text or "").strip()
                if text and text not in texts:
                    texts.append(text)
            row[stage] = " ⟷ ".join(texts)
        comparable = [row.get(stage, "") for stage in stages]
        row["changed"] = len(set(comparable)) > 1
        rows.append(row)
    return sorted(rows, key=lambda row: (row["start_ms"], row["end_ms"]))
