"""Regression coverage for revision-pinned generation and atomic speech-plan review."""

import json
import tempfile
import threading
import unittest
import uuid
import wave
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AudioTake, GenerationPlan, GenerationPlanRevision, GenerationRun, GenerationSegment, Job
from pandrator.web.workspace import RevisionConflict


class GenerationPlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.headers = {"X-CSRF-Token": self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]}
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]
        self.generation = self.services["generation"]
        self.handlers = self.services["workflow_handlers"]
        self.session_id = self.client.post("/api/v1/sessions", json={"name": "Speech-plan regression", "workflow_kind": "voiceover"}, headers=self.headers).get_json()["id"]
        self.source_path = self.services["paths"].uploads / "source.srt"
        self.source_path.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello world.\n", encoding="utf-8")
        self.source = self.services["artifacts"].register(self.source_path, kind="srt", role="transcription", session_id=self.session_id)
        text = "Hello world. These meetings, these interfaith circles."
        self.initial_text = text
        plan = self.generation.create_plan(self.session_id, source_revision_id=None, settings={"speech_block_max_chars": 120, "_source_artifact_id": self.source.id}, segments=[
            {"text": text, "source_segment_ids": [1, 2], "speech_block_provenance": {
                "schema_version": 1, "source_reference_namespace": "subtitle_ordinal",
                "source_cues": [
                    {"reference": 1, "start_ms": 1000, "end_ms": 2000, "display_spans": [[0, 12]], "speech_spans": [[0, 12]]},
                    {"reference": 2, "start_ms": 2100, "end_ms": 4000, "display_spans": [[13, len(text)]], "speech_spans": [[13, len(text)]]},
                ],
            }},
            {"text": "Another complete sentence.", "source_segment_ids": [3]},
            {"text": "A final sentence.", "source_segment_ids": [4]},
        ])
        self.revision_id = plan["active_revision_id"]
        self.segment_ids = [item["id"] for item in self.generation.list_segments(self.session_id)["items"]]
        path = self.services["paths"].uploads / "reusable.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        artifact = self.services["artifacts"].register(path, kind="audio", role="generation_take", session_id=self.session_id)
        with self.database.session() as session:
            last = session.get(GenerationSegment, self.segment_ids[-1])
            last.status = "completed"
            take = AudioTake(generation_segment_id=last.id, artifact_id=artifact.id, status="completed", is_active=True, duration_ms=100)
            session.add(take)
            session.flush()
            self.take_id = take.id

    def batch(self, operations, *, revision=None, key=None):
        expected = revision or self.revision_id
        return self.client.post(f"/api/v1/sessions/{self.session_id}/generation-plan/topology/batch", json={"expected_revision_id": expected, "operations": operations}, headers={**self.headers, "If-Match": f'"{expected}"', "Idempotency-Key": key or f"batch-{uuid.uuid4().hex}"})

    def split(self):
        response = self.batch([{"action": "split", "segment_id": self.segment_ids[0], "boundary": {"after_source_cue_id": 1}, "label": "opening"}])
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def test_batch_follows_original_ids_and_named_results_and_is_idempotent(self):
        operations = [
            {"action": "split", "segment_id": self.segment_ids[0], "boundary": {"after_sentence": 1}, "label": "opening"},
            {"action": "split", "segment": {"result_ref": "opening.right"}, "boundary": {"before_text": "these interfaith"}, "label": "topic"},
            {"action": "merge", "left": {"result_ref": "topic.left"}, "right": {"result_ref": "topic.right"}, "label": "joined"},
        ]
        response = self.batch(operations, key="batch-idempotent-regression")
        self.assertEqual(response.status_code, 201, response.get_json())
        result = response.get_json()
        self.assertEqual(result["operation_count"], 3)
        self.assertEqual(len(result["lineage"][self.segment_ids[0]]), 2)
        self.assertEqual(len(result["lineage"][self.segment_ids[-1]]), 1)
        rows = self.generation.list_segments(self.session_id)["items"]
        self.assertEqual([row["text"] for row in rows[:2]], ["Hello world.", "These meetings, these interfaith circles."])
        self.assertEqual(rows[-1]["takes"][0]["status"], "completed")
        self.assertEqual(rows[0]["source_segment_ids"], [1])
        self.assertEqual(rows[1]["source_segment_ids"], [2])
        replay = self.batch(operations, key="batch-idempotent-regression")
        self.assertEqual(replay.status_code, 201, replay.get_json())
        self.assertEqual(replay.get_json()["plan_revision_id"], result["plan_revision_id"])
        self.assertEqual(replay.headers.get("Idempotency-Replayed"), "true")

    def test_ambiguous_later_anchor_rolls_back_all_prior_edits(self):
        response = self.batch([
            {"action": "split", "segment_id": self.segment_ids[0], "boundary": {"after_sentence": 1}, "label": "opening"},
            {"action": "split", "segment": {"result_ref": "opening.right"}, "boundary": {"before_text": " "}},
        ])
        self.assertEqual(response.status_code, 422, response.get_json())
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == self.session_id))
            self.assertEqual(plan.active_revision_id, self.revision_id)
            self.assertEqual(session.scalar(select(func.count()).select_from(GenerationPlanRevision).where(GenerationPlanRevision.plan_id == plan.id)), 1)
            self.assertTrue(session.get(AudioTake, self.take_id).is_active)

    def test_compact_projection_and_context_keep_revision_identity(self):
        result = self.split()
        response = self.client.get(f"/api/v1/sessions/{self.session_id}/generation-segments", query_string={"view": "compact", "source_cue_id": "2", "radius": 1})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["plan_revision_id"], result["plan_revision_id"])
        self.assertEqual([item["ordinal"] for item in payload["items"]], [0, 1, 2])
        self.assertNotIn("speech_block_provenance", payload["items"][0])
        self.assertNotIn("takes", payload["items"][0])
        self.assertIn("has_usable_take", payload["items"][0])
        old = self.client.get(f"/api/v1/sessions/{self.session_id}/generation-segments", query_string={"plan_revision_id": self.revision_id, "fields": "text,source_segment_ids"}).get_json()
        self.assertFalse(old["is_active_revision"])
        self.assertEqual(old["items"][0]["text"], self.initial_text)

    def test_revision_history_lists_automatic_and_manual_with_reusable_counts(self):
        result = self.split()
        response = self.client.get(f"/api/v1/sessions/{self.session_id}/generation-plan/revisions")
        self.assertEqual(response.status_code, 200, response.get_json())
        history = response.get_json()
        self.assertEqual(history["active_revision_id"], result["plan_revision_id"])
        self.assertEqual([item["origin"] for item in history["items"]], ["manual", "automatic"])
        self.assertEqual(history["items"][0]["reusable_segment_count"], 1)
        self.assertEqual(history["items"][0]["stale_segment_count"], 3)
        self.assertNotIn("lineage", json.dumps(history))

    def test_explicit_revision_start_never_calls_plan_refresher(self):
        with patch.object(self.generation, "plan_refresher", side_effect=AssertionError("must not rebuild a selected plan")):
            result = self.generation.start(self.session_id, speech_plan_revision_id=self.revision_id)
        self.assertEqual(result["plan_revision_id"], self.revision_id)
        with self.database.session() as session:
            run = session.get(GenerationRun, result["id"])
            self.assertEqual(run.settings_snapshot_json["speech_plan_revision_id"], self.revision_id)

    def test_changed_revision_between_prepare_and_queue_is_rejected(self):
        prepared = self.generation.prepare_start(self.session_id, speech_plan_revision_id=self.revision_id)
        self.split()
        with self.assertRaises(RevisionConflict), self.database.immediate_session() as session:
            self.generation.start_in_session(session, self.session_id, prepared=prepared)
        with self.database.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(GenerationRun)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(Job).where(Job.kind == "generation.run")), 0)

    def test_stale_only_copies_unchanged_take_into_new_output_run(self):
        result = self.generation.start(self.session_id, speech_plan_revision_id=self.revision_id, stale_only=True)
        with self.database.session() as session:
            run = session.get(GenerationRun, result["id"])
            job = session.get(Job, run.job_id)
            self.assertEqual(set(job.payload_json["segment_ids"]), set(self.segment_ids[:-1]))
            copied = session.scalar(select(AudioTake).where(AudioTake.generation_run_id == run.id))
            self.assertEqual(copied.parent_take_id, self.take_id)
            self.assertEqual(copied.generation_segment_id, self.segment_ids[-1])
            self.assertTrue(copied.is_active)

    def test_workflow_generation_preserves_reviewed_plan(self):
        result = self.split()
        with patch.object(self.handlers, "_subtitle_generation_records", side_effect=AssertionError("must not rebuild reviewed blocks")), patch.object(self.handlers, "run_generation", return_value={"status": "completed"}):
            self.handlers._run_reviewable_generation({"session_id": self.session_id, "source_artifact_id": self.source.id, "speech_plan_revision_id": result["plan_revision_id"], "settings": {}}, lambda *_: None, threading.Event())
        with self.database.session() as session:
            run = session.scalar(select(GenerationRun).where(GenerationRun.session_id == self.session_id))
            self.assertEqual(run.plan_revision_id, result["plan_revision_id"])
            self.assertEqual(run.settings_snapshot_json["speech_plan_revision_id"], result["plan_revision_id"])
