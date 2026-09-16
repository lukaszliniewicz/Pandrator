"""Focused: the active-plan safety guard skips explicitly and never calls TTS.

Covers the 00:47:06 UTC shape (source R1 != active R2): the wrapper must
keep the guard (no rebase/retarget, no staging, no generation) and report a
structured skip so ``0 attempts`` is distinct from ``no eligible groups``.
Pure mocks only: no database, no TTS, no migrations.
"""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from pandrator.web.voiceover_regroup import repair_regroup_blocks


class ActivePlanChangedSkipTests(unittest.TestCase):
    def test_mismatch_returns_skip_status_and_never_generates(self):
        source_revision_id = "2a54007e-5015-46a7-a74f-1718fcd781b7"
        active_revision_id = "0492eb17-1bde-4cd4-a696-9b49fcd450ba"
        source_run = SimpleNamespace(
            settings_snapshot_json={},
            plan_revision_id=source_revision_id,
            session_id="a462facf-1548-46d6-862a-9028f25c2641",
            job_id="685c89af-bfc3-433e-9f37-097a8e88bc32",
        )
        active_plan = SimpleNamespace(active_revision_id=active_revision_id)

        session = MagicMock()
        session.get.return_value = source_run
        session.scalar.return_value = active_plan

        @contextmanager
        def _session():
            yield session

        handler = SimpleNamespace()
        handler.database = SimpleNamespace(session=_session)
        handler.run_generation = MagicMock(
            side_effect=AssertionError("TTS/generation must not run on skip")
        )

        result = repair_regroup_blocks(
            handler,
            "49c67d91-5316-4dd6-a57a-d89152406ab8",
            lambda _value, _detail=None: None,
            MagicMock(is_set=MagicMock(return_value=False)),
        )

        self.assertEqual(result["regrouped_groups"], 0)
        self.assertEqual(result["regroup_attempted_groups"], 0)
        self.assertEqual(result["regroup_status"], "skipped_active_plan_changed")
        self.assertEqual(result["regroup_reason"], "active_plan_changed")
        self.assertEqual(result["source_plan_revision_id"], source_revision_id)
        self.assertEqual(result["active_plan_revision_id"], active_revision_id)
        handler.run_generation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
