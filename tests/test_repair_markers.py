"""The second passes keep one home for their reason codes and markers."""

from __future__ import annotations

import inspect

from pandrator.logic.dubbing import repair_markers
from pandrator.logic.dubbing.repair_markers import (
    EARLY_REPAIR_MARKER_KEY,
    EARLY_REPAIR_REASON,
    MIXED_REASON,
    REGROUP_MARKER_KEY,
    REGROUP_REASON,
    SECOND_PASS_MARKER_KEYS,
    SECOND_PASS_REASONS,
    UNDO_REGROUP_REASON,
    UNDO_REPAIR_REASON,
)
from pandrator.web import generation_run_history, repair_batches


def test_every_constant_keeps_its_stored_value():
    assert EARLY_REPAIR_REASON == "early_timing_repair"
    assert REGROUP_REASON == "passage_regroup"
    assert MIXED_REASON == "mixed"
    assert SECOND_PASS_REASONS == frozenset({"early_timing_repair", "passage_regroup"})
    assert UNDO_REPAIR_REASON == "undo_timing_repairs"
    assert UNDO_REGROUP_REASON == "undo_passage_regroup"
    assert EARLY_REPAIR_MARKER_KEY == "early_repair_parent_run_id"
    assert REGROUP_MARKER_KEY == "regroup_parent_run_id"
    assert SECOND_PASS_MARKER_KEYS == (
        "early_repair_parent_run_id",
        "regroup_parent_run_id",
    )


def test_repair_batches_still_exposes_the_names_it_always_exported():
    assert repair_batches.REPAIR_REASON == EARLY_REPAIR_REASON
    assert repair_batches.REGROUP_REASON == REGROUP_REASON
    assert repair_batches.MIXED_REASON == MIXED_REASON
    assert repair_batches.SECOND_PASS_REASONS == SECOND_PASS_REASONS
    assert repair_batches.UNDO_REPAIR_REASON == UNDO_REPAIR_REASON
    assert repair_batches.UNDO_REGROUP_REASON == UNDO_REGROUP_REASON


def test_generation_run_history_still_exposes_the_names_it_always_exported():
    assert generation_run_history.EARLY_REPAIR_REASON == EARLY_REPAIR_REASON
    assert generation_run_history.REGROUP_REASON == REGROUP_REASON
    assert generation_run_history.SECOND_PASS_REASONS == SECOND_PASS_REASONS
    assert generation_run_history.EARLY_REPAIR_MARKER_KEY == EARLY_REPAIR_MARKER_KEY
    assert generation_run_history.REGROUP_MARKER_KEY == REGROUP_MARKER_KEY
    assert generation_run_history.SECOND_PASS_MARKER_KEYS == SECOND_PASS_MARKER_KEYS


def test_the_marker_module_imports_nothing_from_the_application():
    source = inspect.getsource(repair_markers)
    imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    assert imports == ["from __future__ import annotations"]
