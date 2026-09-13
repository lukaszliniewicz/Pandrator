"""Pure grouping metadata for generation-run history projections.

The database keeps every generation run immutable.  This module only verifies
the explicit metadata used by optional early timing repair and returns an
in-memory view that callers can use for display or cleanup decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class GenerationRunHistory:
    """One logical history entry and its verified repair attempts."""

    root: Any
    repair_children: tuple[Any, ...]
    result: Any
    repair_operations: Mapping[str, Mapping[str, Any]]
    applied_children: tuple[Any, ...] = ()

    @property
    def repair_child_ids(self) -> frozenset[str]:
        """IDs of verified children, useful to callers performing cleanup."""

        return frozenset(str(child.id) for child in self.repair_children)

    def is_repair_child(self, run_id: str) -> bool:
        """Return whether ``run_id`` is one of this group's repair children."""

        return str(run_id) in self.repair_child_ids


def _repair_marker(run: Any) -> str:
    snapshot = run.settings_snapshot_json
    if not isinstance(snapshot, dict):
        return ""
    value = snapshot.get("early_repair_parent_run_id")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _repair_operation(
    run: Any,
    revisions: Mapping[str, Any],
    root_id: str,
) -> dict[str, Any] | None:
    revision = revisions.get(run.plan_revision_id)
    operation = getattr(revision, "operation_json", None)
    if not isinstance(operation, dict):
        return None
    if operation.get("reason") != "early_timing_repair":
        return None
    if str(operation.get("source_generation_run_id") or "") != root_id:
        return None
    return dict(operation)


def _run_sort_key(run: Any) -> tuple[int, str, str]:
    return (
        int(getattr(run, "sequence_number", 0) or 0),
        str(getattr(run, "created_at", "") or ""),
        str(getattr(run, "id", "") or ""),
    )


def build_generation_run_history(
    runs: Sequence[Any],
    revisions: Mapping[str, Any],
) -> dict[str, GenerationRunHistory]:
    """Build verified logical groups for a batch of session-scoped runs.

    A repair child is accepted only when its snapshot marker names an existing
    unmarked run in the same session, its output owner is empty, it is an
    ordinary ``generate`` run, and its plan revision records the exact repair
    reason and source root.  This intentionally does not infer groups from
    generic source links, which are used by targeted regeneration.
    """

    by_id = {str(run.id): run for run in runs if getattr(run, "id", None)}
    children_by_root: dict[str, list[Any]] = {}
    operations_by_child: dict[str, dict[str, Any]] = {}

    for child in runs:
        child_id = str(getattr(child, "id", "") or "")
        marker = _repair_marker(child)
        if (
            not child_id
            or not marker
            or str(getattr(child, "operation", "") or "") != "generate"
            or getattr(child, "output_generation_run_id", None)
        ):
            continue
        root = by_id.get(marker)
        if (
            root is None
            or root.id == child.id
            or getattr(root, "output_generation_run_id", None)
        ):
            continue
        if str(getattr(root, "session_id", "") or "") != str(
            getattr(child, "session_id", "") or ""
        ):
            continue
        source_id = str(getattr(child, "source_generation_run_id", "") or "")
        source = by_id.get(source_id)
        if source is None or str(getattr(source, "session_id", "") or "") != str(
            getattr(child, "session_id", "") or ""
        ):
            continue
        # A marked run cannot be the original root.  This also rejects marker
        # cycles without needing recursive traversal.
        if _repair_marker(root):
            continue
        operation = _repair_operation(child, revisions, str(root.id))
        if operation is None:
            continue
        children_by_root.setdefault(str(root.id), []).append(child)
        operations_by_child[child_id] = operation

    histories: dict[str, GenerationRunHistory] = {}
    for root_id, children in children_by_root.items():
        root = by_id[root_id]
        reachable = {str(root.id)}
        verified = []
        for child in sorted(children, key=_run_sort_key):
            source_id = str(getattr(child, "source_generation_run_id", "") or "")
            source = by_id.get(source_id)
            if source_id not in reachable or _run_sort_key(source) >= _run_sort_key(
                child
            ):
                continue
            verified.append(child)
            reachable.add(str(child.id))
        ordered = tuple(verified)
        accepted = root
        applied = []
        # A repair operation is allowed to build on the root or the last
        # accepted result.  A rejected attempt never becomes a new source.
        for child in ordered:
            operation = operations_by_child[str(child.id)]
            repair_status = str(operation.get("repair_status") or "")
            if str(getattr(child, "source_generation_run_id", "") or "") != str(
                accepted.id
            ):
                continue
            if (
                getattr(child, "status", None) == "completed"
                and repair_status == "applied"
            ):
                accepted = child
                applied.append(child)
        history = GenerationRunHistory(
            root=root,
            repair_children=ordered,
            result=accepted,
            applied_children=tuple(applied),
            repair_operations={
                str(child.id): operations_by_child[str(child.id)] for child in ordered
            },
        )
        histories[root_id] = history
        for child in ordered:
            histories[str(child.id)] = history

    # Every run gets metadata so callers can safely ask for a history entry.
    # Runs rejected above remain standalone and therefore retain their raw
    # sequence and source/output semantics.
    for run in runs:
        run_id = str(getattr(run, "id", "") or "")
        if run_id and run_id not in histories:
            histories[run_id] = GenerationRunHistory(
                root=run,
                repair_children=(),
                result=run,
                repair_operations={},
            )
    return histories


__all__ = ["GenerationRunHistory", "build_generation_run_history"]
