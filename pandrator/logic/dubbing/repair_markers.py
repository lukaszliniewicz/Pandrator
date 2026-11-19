"""Reason codes and snapshot markers of the optional second passes.

Early timing repair (one block split and regenerated) and passage regroup
(adjacent passages merged and regenerated as one group) write the same small
vocabulary into plan revisions, generation runs and history projections. It
imports nothing, so the dubbing logic and the web services can both import it
without pulling in a service module, and the strings cannot drift apart again.
"""

from __future__ import annotations

EARLY_REPAIR_REASON = "early_timing_repair"
REGROUP_REASON = "passage_regroup"
# A batch that mixes both passes reports one reason of its own.
MIXED_REASON = "mixed"
SECOND_PASS_REASONS = frozenset({EARLY_REPAIR_REASON, REGROUP_REASON})

UNDO_REPAIR_REASON = "undo_timing_repairs"
UNDO_REGROUP_REASON = "undo_passage_regroup"

EARLY_REPAIR_MARKER_KEY = "early_repair_parent_run_id"
REGROUP_MARKER_KEY = "regroup_parent_run_id"
SECOND_PASS_MARKER_KEYS = (EARLY_REPAIR_MARKER_KEY, REGROUP_MARKER_KEY)

__all__ = [
    "EARLY_REPAIR_MARKER_KEY",
    "EARLY_REPAIR_REASON",
    "MIXED_REASON",
    "REGROUP_MARKER_KEY",
    "REGROUP_REASON",
    "SECOND_PASS_MARKER_KEYS",
    "SECOND_PASS_REASONS",
    "UNDO_REGROUP_REASON",
    "UNDO_REPAIR_REASON",
]
