"""Deletion of one logical run preserves audio used by independent runs."""

import unittest

from sqlalchemy import select

from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationRun,
    GenerationPlanRevision,
    Job,
    UsageEvent,
)
from pandrator.web.workspace import GenerationService, WorkspaceSettingsService
from tests import test_web_voiceover_repair as repair_fixtures


class RepairHistoryDeletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = repair_fixtures.VoiceoverRepairTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.plan(groups=2)
        result, _ = self.fixture.generate()
        self.assertEqual(2, result["repaired_blocks"])
        self.database = self.fixture.database
        self.service = GenerationService(
            self.database,
            self.fixture.handlers.jobs,
            WorkspaceSettingsService(self.database),
            artifacts=self.fixture.handlers.artifacts,
        )
        self.result_run_id = result["generation_run_id"]

    def test_delete_group_preserves_shared_artifacts_and_independent_run(self):
        with self.database.session() as session:
            final = session.get(GenerationRun, self.result_run_id)
            shared_take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == self.result_run_id,
                    AudioTake.parent_take_id.is_not(None),
                )
            )
            shared_artifact_id = shared_take.artifact_id
            shared_path = self.fixture.paths.managed_path(
                session.get(Artifact, shared_artifact_id).relative_path
            )
            independent = GenerationRun(
                session_id=final.session_id,
                plan_revision_id=final.plan_revision_id,
                sequence_number=final.sequence_number + 1,
                status="completed",
            )
            task = GenerationRun(
                session_id=final.session_id,
                plan_revision_id=final.plan_revision_id,
                sequence_number=final.sequence_number + 2,
                operation="regenerate",
                output_generation_run_id=final.id,
                status="completed",
            )
            session.add_all([independent, task])
            session.flush()
            independent_id, task_id = independent.id, task.id
            retained = AudioTake(
                generation_segment_id=shared_take.generation_segment_id,
                generation_run_id=independent.id,
                artifact_id=shared_artifact_id,
                parent_take_id=shared_take.id,
                status="completed",
            )
            session.add(retained)
            session.flush()
            retained_id = retained.id
            family_ids = {
                row.id
                for row in session.scalars(select(GenerationRun)).all()
                if row.id != independent_id
            }
        self.assertTrue(shared_path.is_file())
        self.service.delete_run(self.fixture.run_id)
        with self.database.session() as session:
            self.assertEqual(
                [independent_id], list(session.scalars(select(GenerationRun.id)))
            )
            self.assertIsNone(session.get(GenerationRun, task_id))
            self.assertIsNotNone(session.get(Artifact, shared_artifact_id))
            self.assertEqual(
                shared_artifact_id, session.get(AudioTake, retained_id).artifact_id
            )
            self.assertFalse(
                session.scalar(
                    select(AudioTake.id).where(
                        AudioTake.generation_run_id.in_(family_ids)
                    )
                )
            )
        self.assertTrue(shared_path.is_file())

    def test_active_repair_blocks_whole_group_deletion(self):
        with self.database.session() as session:
            session.get(GenerationRun, self.result_run_id).status = "running"
        with self.assertRaisesRegex(ValueError, "Stop or cancel"):
            self.service.delete_run(self.fixture.run_id)
        with self.database.session() as session:
            self.assertIsNotNone(session.get(GenerationRun, self.fixture.run_id))
            self.assertIsNotNone(session.get(GenerationRun, self.result_run_id))

    def test_projection_preserves_versions_numbers_and_total_cost(self):
        with self.database.session() as session:
            final = session.get(GenerationRun, self.result_run_id)
            manual = GenerationRun(
                session_id=final.session_id,
                plan_revision_id=final.plan_revision_id,
                sequence_number=4,
                status="completed",
            )
            task = GenerationRun(
                session_id=final.session_id,
                plan_revision_id=final.plan_revision_id,
                sequence_number=5,
                operation="regenerate",
                status="completed",
                output_generation_run_id=final.id,
                settings_snapshot_json=final.settings_snapshot_json,
            )
            session.add_all([manual, task])
            session.flush()
            manual_id, task_id = manual.id, task.id
            for run_id, cost in [
                (self.fixture.run_id, 1.0),
                (final.id, 0.2),
                (task.id, 0.1),
                (manual.id, 10.0),
            ]:
                session.add(
                    UsageEvent(
                        session_id=final.session_id,
                        generation_run_id=run_id,
                        provider_key="test",
                        model_id="test",
                        cost_usd=cost,
                    )
                )
        rows = {
            item["id"]: item for item in self.service.list_runs(self.fixture.record.id)
        }
        root = rows[self.fixture.run_id]
        self.assertEqual(self.fixture.revision_id, root["plan_revision_id"])
        self.assertEqual(
            self.result_run_id, root["timing_repair"]["result_generation_run_id"]
        )
        self.assertEqual(2, root["timing_repair"]["applied_count"])
        self.assertAlmostEqual(1.3, root["timing_repair"]["usage"]["total_cost_usd"])
        self.assertEqual(1.0, root["usage"]["total_cost_usd"])
        self.assertTrue(rows[manual_id]["label"].startswith("Run 2:"))
        self.assertNotIn("timing_repair", rows[manual_id])
        self.assertIsNone(rows[task_id]["early_repair_parent_run_id"])
        self.assertEqual(rows[self.result_run_id]["label"], rows[task_id]["label"])

    def test_repair_phase_ends_when_parent_job_terminates(self):
        with self.database.session() as session:
            root = session.get(GenerationRun, self.fixture.run_id)
            job = Job(
                kind="generation.run",
                session_id=root.session_id,
                status="running",
                progress=0.9,
            )
            session.add(job)
            session.flush()
            root.job_id = job.id
            job_id = job.id
        root = next(
            item
            for item in self.service.list_runs(self.fixture.record.id)
            if item["id"] == self.fixture.run_id
        )
        self.assertEqual("running", root["status"])
        self.assertEqual("repairing_timing", root["phase"])
        with self.database.session() as session:
            session.get(Job, job_id).status = "succeeded"
            child = session.get(GenerationRun, self.result_run_id)
            child.status = "queued"
            revision = session.get(GenerationPlanRevision, child.plan_revision_id)
            revision.operation_json = {
                **revision.operation_json,
                "repair_status": "pending",
            }
        root = self.service.latest_run(self.fixture.record.id)
        self.assertEqual(self.fixture.run_id, root["id"])
        self.assertEqual("completed", root["status"])
        self.assertNotIn("phase", root)
        self.assertEqual("stopped", root["timing_repair"]["status"])
