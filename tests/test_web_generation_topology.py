import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
)
from pandrator.web.workspace import GenerationService, stable_hash


class GenerationTopologyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.database = self.app.extensions["pandrator"]["database"]
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Topology review", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.session_id = created.get_json()["id"]
        plan = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan",
            json={
                "settings": {"speech_block_max_chars": 100},
                "segments": [
                    {
                        "text": "A🙂 B",
                        "source_segment_ids": ["source-uuid-1"],
                        "alignment_group": "a0001",
                        "speaker": "Narrator",
                    },
                    {
                        "text": "A second block.",
                        "source_segment_ids": ["source-uuid-2"],
                        "alignment_group": "a0002",
                        "paragraph_break_after": True,
                    },
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        self.initial_revision_id = plan.get_json()["active_revision_id"]
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id == self.initial_revision_id
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.initial_segment_ids = [segment.id for segment in segments]
            segments[0].marked = True
            segments[0].status = "completed"
            segments[0].speech_block_provenance_json = {
                "schema_version": 1,
                "origin": "automatic",
                "source_reference_namespace": "document_segment_id",
                "source_cues": [
                    {
                        "reference": "source-uuid-1",
                        "start_ms": 100,
                        "end_ms": 900,
                        "display_text": "A🙂 B",
                        "speech_text": "A🙂 B",
                        "display_spans": [[0, 4]],
                        "speech_spans": [[0, 4]],
                    }
                ],
                "formation_events": [],
                "boundary_before": {
                    "action": "keep_boundary",
                    "reason_code": "document_start",
                    "summary": "Speech block starts the document.",
                    "measurements": {},
                    "source_references": ["source-uuid-1"],
                },
                "risk_flags": [],
            }
            segments[1].status = "completed"
            first_artifact = Artifact(
                session_id=self.session_id,
                kind="audio",
                role="generation_take",
                relative_path="sessions/topology/first.wav",
            )
            second_artifact = Artifact(
                session_id=self.session_id,
                kind="audio",
                role="generation_take",
                relative_path="sessions/topology/second.wav",
            )
            session.add_all([first_artifact, second_artifact])
            session.flush()
            first_take = AudioTake(
                generation_segment_id=segments[0].id,
                artifact_id=first_artifact.id,
                status="completed",
                is_active=True,
            )
            second_take = AudioTake(
                generation_segment_id=segments[1].id,
                artifact_id=second_artifact.id,
                status="completed",
                is_active=True,
            )
            stale_take = AudioTake(
                generation_segment_id=segments[1].id,
                artifact_id=second_artifact.id,
                status="stale",
                is_active=False,
            )
            session.add_all([first_take, second_take, stale_take])
            session.flush()
            self.initial_take_ids = [first_take.id, second_take.id]
            self.stale_take_id = stale_take.id
            run = GenerationRun(
                session_id=self.session_id,
                plan_revision_id=self.initial_revision_id,
                sequence_number=1,
                status="completed",
            )
            session.add(run)
            session.flush()
            self.run_id = run.id
            assembly = OutputAssembly(
                session_id=self.session_id,
                status="completed",
            )
            historical_assembly = OutputAssembly(
                session_id=self.session_id,
                generation_run_id=run.id,
                status="completed",
            )
            queued_job = Job(
                kind="generation.assemble",
                session_id=self.session_id,
                status="queued",
            )
            running_job = Job(
                kind="generation.assemble",
                session_id=self.session_id,
                status="running",
                lease_owner="topology-test-worker",
                lease_generation=1,
            )
            session.add_all([assembly, historical_assembly, queued_job, running_job])
            session.flush()
            queued_assembly = OutputAssembly(
                session_id=self.session_id,
                job_id=queued_job.id,
                status="queued",
                settings_json={"plan_revision_id": self.initial_revision_id},
            )
            running_assembly = OutputAssembly(
                session_id=self.session_id,
                job_id=running_job.id,
                status="running",
                settings_json={"plan_revision_id": self.initial_revision_id},
            )
            session.add_all([queued_assembly, running_assembly])
            session.flush()
            self.assembly_id = assembly.id
            self.historical_assembly_id = historical_assembly.id
            self.queued_job_id = queued_job.id
            self.running_job_id = running_job.id
            self.queued_assembly_id = queued_assembly.id
            self.running_assembly_id = running_assembly.id

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _topology(self, revision_id, operation, key):
        return self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan/topology",
            json={"expected_revision_id": revision_id, **operation},
            headers={
                **self.headers,
                "If-Match": f'"{revision_id}"',
                "Idempotency-Key": key,
            },
        )

    def _segments(self, **query):
        return self.client.get(
            f"/api/v1/sessions/{self.session_id}/generation-segments",
            query_string=query,
        ).get_json()

    def test_topology_requires_idempotency_key_for_browser_calls(self):
        response = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan/topology",
            json={
                "expected_revision_id": self.initial_revision_id,
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            headers={
                **self.headers,
                "If-Match": f'"{self.initial_revision_id}"',
            },
        )

        self.assertEqual(400, response.status_code, response.get_json())
        self.assertEqual(
            "idempotency_key_required", response.get_json()["error"]["code"]
        )

    def _concurrent_plan_creates(self, session_id):
        generation = self.app.extensions["pandrator"]["generation"]
        barrier = threading.Barrier(2)

        def create(index):
            barrier.wait(timeout=30)
            return generation.create_plan(
                session_id,
                source_revision_id=None,
                segments=[{"text": f"Concurrent plan {index}."}],
                settings={"caller": index},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(create, range(2)))

    def test_concurrent_initial_plan_creates_serialize_plan_allocation(self):
        session = self.app.extensions["pandrator"]["sessions"].create(
            "Concurrent initial plan",
            workflow_kind="voiceover",
        )

        results = self._concurrent_plan_creates(session.id)

        self.assertEqual(2, len({result["active_revision_id"] for result in results}))
        with self.database.session() as database_session:
            plan_rows = list(
                database_session.scalars(
                    select(GenerationPlan).where(GenerationPlan.session_id == session.id)
                ).all()
            )
            self.assertEqual(1, len(plan_rows))
            revisions = list(
                database_session.scalars(
                    select(GenerationPlanRevision)
                    .where(GenerationPlanRevision.plan_id == plan_rows[0].id)
                    .order_by(GenerationPlanRevision.revision_number)
                ).all()
            )
            self.assertEqual([1, 2], [revision.revision_number for revision in revisions])
            self.assertEqual(plan_rows[0].active_revision_id, revisions[-1].id)

    def test_concurrent_revision_creates_serialize_existing_plan_allocation(self):
        results = self._concurrent_plan_creates(self.session_id)

        self.assertEqual(2, len({result["active_revision_id"] for result in results}))
        with self.database.session() as database_session:
            plan = database_session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == self.session_id)
            )
            self.assertIsNotNone(plan)
            revisions = list(
                database_session.scalars(
                    select(GenerationPlanRevision)
                    .where(GenerationPlanRevision.plan_id == plan.id)
                    .order_by(GenerationPlanRevision.revision_number)
                ).all()
            )
            self.assertEqual([1, 2, 3], [revision.revision_number for revision in revisions])
            self.assertEqual(plan.active_revision_id, revisions[-1].id)

    def test_split_merge_restore_preserve_immutable_lineage(self):
        split = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            "topology-split-1",
        )
        self.assertEqual(201, split.status_code, split.get_json())
        split_result = split.get_json()
        split_revision_id = split_result["plan_revision_id"]
        self.assertEqual(self.initial_revision_id, split_result["parent_revision_id"])

        replay = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 2,
                "text_layer": "display",
            },
            "topology-split-1",
        )
        self.assertEqual(201, replay.status_code, replay.get_json())
        self.assertEqual(split_result, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])

        stale = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[0],
                "cursor": 1,
                "text_layer": "display",
            },
            "topology-stale-1",
        )
        self.assertEqual(409, stale.status_code, stale.get_json())

        split_page = self._segments()
        self.assertEqual(split_revision_id, split_page["plan_revision_id"])
        self.assertEqual(self.initial_revision_id, split_page["parent_revision_id"])
        self.assertEqual("split", split_page["operation_json"]["action"])
        self.assertEqual(
            ["A🙂", "B", "A second block."],
            [item["text"] for item in split_page["items"]],
        )
        left, right, unchanged = split_page["items"]
        for child in (left, right):
            self.assertEqual(["source-uuid-1"], child["source_segment_ids"])
            self.assertEqual(
                "document_segment_id",
                child["speech_block_provenance"]["source_reference_namespace"],
            )
            self.assertEqual(
                "source-uuid-1",
                child["speech_block_provenance"]["source_cues"][0]["reference"],
            )
        self.assertEqual(
            [[0, 2]],
            left["speech_block_provenance"]["source_cues"][0]["display_spans"],
        )
        self.assertEqual(
            [[0, 1]],
            right["speech_block_provenance"]["source_cues"][0]["display_spans"],
        )
        self.assertEqual(
            "manual_split",
            right["speech_block_provenance"]["boundary_before"]["reason_code"],
        )
        self.assertEqual(
            self.initial_take_ids[1], unchanged["takes"][0]["parent_take_id"]
        )
        self.assertEqual(1, len(unchanged["takes"]))
        self.assertNotEqual(self.stale_take_id, unchanged["takes"][0]["parent_take_id"])

        historical = self._segments(generation_run_id=self.run_id)
        self.assertEqual(self.initial_revision_id, historical["plan_revision_id"])
        self.assertEqual(
            ["A🙂 B", "A second block."], [item["text"] for item in historical["items"]]
        )
        with self.database.session() as session:
            self.assertEqual(
                "stale", session.get(OutputAssembly, self.assembly_id).status
            )
            self.assertEqual(
                self.initial_revision_id,
                session.get(GenerationRun, self.run_id).plan_revision_id,
            )
            self.assertEqual(
                "completed",
                session.get(OutputAssembly, self.historical_assembly_id).status,
            )
            self.assertEqual(
                "canceled", session.get(OutputAssembly, self.queued_assembly_id).status
            )
            self.assertEqual("canceled", session.get(Job, self.queued_job_id).status)
            self.assertEqual(
                "cancel_requested",
                session.get(OutputAssembly, self.running_assembly_id).status,
            )
            self.assertEqual(
                "cancel_requested", session.get(Job, self.running_job_id).status
            )
            split_revision = session.get(GenerationPlanRevision, split_revision_id)
            stored_segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == split_revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.assertEqual(
                stable_hash(
                    {
                        "parent_revision_id": split_revision.parent_revision_id,
                        "operation": split_revision.operation_json,
                        "segments": [
                            GenerationService._segment_copy_values(segment)
                            for segment in stored_segments
                        ],
                    }
                ),
                split_revision.content_hash,
            )

        merged = self._topology(
            split_revision_id,
            {
                "action": "merge",
                "left_segment_id": left["id"],
                "right_segment_id": right["id"],
            },
            "topology-merge-1",
        )
        self.assertEqual(201, merged.status_code, merged.get_json())
        merged_revision_id = merged.get_json()["plan_revision_id"]
        merged_page = self._segments()
        self.assertEqual(
            ["A🙂 B", "A second block."],
            [item["text"] for item in merged_page["items"]],
        )
        merged_first = merged_page["items"][0]
        self.assertEqual("Narrator", merged_first["speaker"])
        self.assertTrue(merged_first["marked"])
        self.assertEqual(["source-uuid-1"], merged_first["source_segment_ids"])

        restored = self._topology(
            merged_revision_id,
            {
                "action": "restore",
                "target_revision_id": self.initial_revision_id,
            },
            "topology-restore-1",
        )
        self.assertEqual(201, restored.status_code, restored.get_json())
        restored_page = self._segments()
        self.assertEqual("restore", restored_page["operation_json"]["action"])
        self.assertEqual(merged_revision_id, restored_page["parent_revision_id"])
        self.assertEqual(
            ["a0001", "a0002"],
            [item["alignment_group"] for item in restored_page["items"]],
        )
        self.assertEqual(
            self.initial_take_ids,
            [item["takes"][0]["parent_take_id"] for item in restored_page["items"]],
        )
        self.assertEqual(
            [1, 1], [len(item["takes"]) for item in restored_page["items"]]
        )
        with self.database.session() as session:
            revisions = list(
                session.scalars(
                    select(GenerationPlanRevision).order_by(
                        GenerationPlanRevision.revision_number
                    )
                ).all()
            )
            self.assertEqual([1, 2, 3, 4], [item.revision_number for item in revisions])
            self.assertEqual(4, len({item.content_hash for item in revisions}))

    def test_split_preserves_source_references_for_legacy_rows_without_spans(self):
        response = self._topology(
            self.initial_revision_id,
            {
                "action": "split",
                "segment_id": self.initial_segment_ids[1],
                "cursor": 8,
                "text_layer": "display",
            },
            "topology-legacy-source-1",
        )

        self.assertEqual(201, response.status_code, response.get_json())
        page = self._segments()
        children = page["items"][1:3]
        self.assertEqual(["A second", "block."], [item["text"] for item in children])
        for child in children:
            provenance = child["speech_block_provenance"]
            self.assertEqual(
                "generation_source_reference",
                provenance["source_reference_namespace"],
            )
            self.assertEqual(
                ["source-uuid-2"],
                [cue["reference"] for cue in provenance["source_cues"]],
            )
            self.assertEqual([], provenance["source_cues"][0]["display_spans"])


if __name__ == "__main__":
    unittest.main()
