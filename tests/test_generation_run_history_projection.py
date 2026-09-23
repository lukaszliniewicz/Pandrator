"""Focused checks for immutable generation-run history projection metadata."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from pandrator.web.generation_run_history import build_generation_run_history
from pandrator_mcp.schemas.e2e import ListGenerationRunsInput
from pandrator_mcp.tools.e2e import list_generation_runs


def _run(
    run_id: str,
    *,
    session_id: str = "session-1",
    sequence_number: int = 1,
    source_generation_run_id: str | None = None,
    output_generation_run_id: str | None = None,
    operation: str = "generate",
    status: str = "completed",
    marker: str | None = None,
):
    return SimpleNamespace(
        id=run_id,
        session_id=session_id,
        plan_revision_id=f"plan-{run_id}",
        source_generation_run_id=source_generation_run_id,
        output_generation_run_id=output_generation_run_id,
        operation=operation,
        status=status,
        sequence_number=sequence_number,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        settings_snapshot_json=(
            {"early_repair_parent_run_id": marker} if marker else {}
        ),
    )


def _revision(run, *, repair_status: str = "applied", source: str | None = None):
    return SimpleNamespace(
        id=run.plan_revision_id,
        operation_json={
            "reason": "early_timing_repair",
            "source_generation_run_id": source or run.source_generation_run_id,
            "repair_status": repair_status,
        },
    )


class GenerationRunHistoryProjectionTests(unittest.TestCase):
    def test_only_verified_children_group_and_result_follows_accepted_chain(self):
        root = _run("root")
        first = _run(
            "first",
            sequence_number=2,
            source_generation_run_id=root.id,
            marker=root.id,
        )
        rejected = _run(
            "rejected",
            sequence_number=3,
            source_generation_run_id=first.id,
            marker=root.id,
        )
        failed = _run(
            "failed",
            sequence_number=4,
            source_generation_run_id=first.id,
            status="failed",
            marker=root.id,
        )
        revisions = {
            first.plan_revision_id: _revision(first, source=root.id),
            rejected.plan_revision_id: _revision(
                rejected, repair_status="not_applied", source=root.id
            ),
            failed.plan_revision_id: _revision(
                failed, repair_status="applied", source=root.id
            ),
        }
        histories = build_generation_run_history(
            [root, first, rejected, failed], revisions
        )

        history = histories[root.id]
        self.assertEqual(
            (first.id, rejected.id, failed.id),
            tuple(child.id for child in history.repair_children),
        )
        self.assertEqual(first.id, history.result.id)
        self.assertEqual(root.id, histories[first.id].root.id)

    def test_malformed_cross_session_and_output_owned_markers_stay_standalone(self):
        root = _run("root")
        malformed = _run(
            "malformed",
            sequence_number=2,
            source_generation_run_id=root.id,
            marker=root.id,
        )
        cross_session_root = _run("foreign", session_id="session-2")
        cross_session = _run(
            "cross-session",
            sequence_number=3,
            session_id="session-1",
            source_generation_run_id=root.id,
            marker=cross_session_root.id,
        )
        output_owned = _run(
            "targeted",
            sequence_number=4,
            source_generation_run_id=root.id,
            output_generation_run_id=root.id,
            operation="regenerate",
            marker=root.id,
        )
        histories = build_generation_run_history(
            [root, malformed, cross_session_root, cross_session, output_owned],
            {
                malformed.plan_revision_id: SimpleNamespace(
                    operation_json={
                        "reason": "manual_split",
                        "source_generation_run_id": root.id,
                    }
                ),
                output_owned.plan_revision_id: _revision(output_owned, source=root.id),
            },
        )

        self.assertFalse(histories[malformed.id].is_repair_child(malformed.id))
        self.assertFalse(histories[cross_session.id].is_repair_child(cross_session.id))
        self.assertFalse(histories[output_owned.id].is_repair_child(output_owned.id))
        self.assertEqual((), histories[root.id].repair_children)

    def test_long_history_rejects_cycles_and_keeps_last_accepted_result(self):
        root = _run("root")
        children = []
        revisions = {}
        source = root.id
        for index in range(133):
            child = _run(
                f"repair-{index}",
                sequence_number=index + 2,
                source_generation_run_id=source,
                marker=root.id,
            )
            children.append(child)
            revisions[child.plan_revision_id] = _revision(child, source=root.id)
            source = child.id
        cycle = _run(
            "cycle",
            sequence_number=135,
            source_generation_run_id="cycle",
            marker=root.id,
        )
        revisions[cycle.plan_revision_id] = _revision(cycle, source=root.id)
        history = build_generation_run_history([root, *children, cycle], revisions)
        self.assertEqual(133, len(history[root.id].applied_children))
        self.assertEqual(children[-1].id, history[root.id].result.id)
        self.assertFalse(history[cycle.id].is_repair_child(cycle.id))

    def test_mcp_filters_verified_children_before_limit(self):
        requests = []

        class Application:
            def list_generation_runs(self, session_id, **kwargs):
                requests.append((session_id, kwargs))
                return {
                    "items": [
                        *[
                            {
                                "id": f"repair-{index}",
                                "early_repair_parent_run_id": "root",
                            }
                            for index in range(133, 0, -1)
                        ],
                        {"id": "root", "label": "Run 1"},
                        {"id": "manual", "label": "Run 2"},
                    ]
                }

        class Runtime:
            def require_application(self):
                return Application()

        runtime = Runtime()
        default = list_generation_runs(
            runtime,
            ListGenerationRunsInput(session_id="session-1", limit=2),
        )
        raw = list_generation_runs(
            runtime,
            ListGenerationRunsInput(
                session_id="session-1", limit=2, include_repairs=True
            ),
        )
        self.assertEqual(["root", "manual"], [item["id"] for item in default["items"]])
        self.assertEqual(
            ["repair-133", "repair-132"], [item["id"] for item in raw["items"]]
        )
        self.assertEqual(
            [
                ("session-1", {"limit": 2, "include_repairs": False}),
                ("session-1", {"limit": 2, "include_repairs": True}),
            ],
            requests,
        )


if __name__ == "__main__":
    unittest.main()
