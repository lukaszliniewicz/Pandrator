"""Durable generation admission and worker fencing on a disposable database."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.generation_admission import (
    GenerationAdmissionStaleInputs,
    run_generation_admission,
)
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import (
    AppSetting,
    GenerationPlan,
    GenerationRun,
    Job,
    SessionSetting,
    utcnow,
)
from pandrator.web.workspace import GenerationService


class GenerationAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        app = create_app(data_root=temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.services = app.extensions["pandrator"]
        self.database = self.services["database"]
        self.addCleanup(self.database.dispose)
        self.client = app.test_client()
        token = bootstrap.issue()
        csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        self.headers = {"X-CSRF-Token": csrf.get_json()["csrf_token"]}
        session_response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Admission test", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(session_response.status_code, 201)
        self.session_id = session_response.get_json()["id"]
        plan = self.services["generation"].create_plan(
            self.session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "A short sentence."}],
        )
        self.revision_id = plan["active_revision_id"]

    def admit(self, *, key="admission-key-0001", payload=None):
        return self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-admissions",
            json=payload or {},
            headers={**self.headers, "Idempotency-Key": key},
        )

    def count(self, kind: str) -> int:
        with self.database.session() as session:
            return int(session.scalar(select(func.count()).select_from(Job).where(Job.kind == kind)) or 0)

    def historical_source(self):
        old_segment = self.services["generation"].list_segments(self.session_id)["items"][0]
        snapshot, settings_hash = self.services["workspace_settings"].resolve(self.session_id)
        with self.database.session() as session:
            source = GenerationRun(
                session_id=self.session_id,
                plan_revision_id=self.revision_id,
                sequence_number=1,
                operation="generate",
                status="completed",
                settings_snapshot_json=snapshot,
                settings_hash=settings_hash,
            )
            session.add(source)
            session.flush()
            source_id = source.id
        edited = self.services["generation"].update_segment(
            old_segment["id"], old_segment["revision"], {"text": "Edited sentence."}
        )
        self.assertNotEqual(edited["plan_revision_id"], self.revision_id)
        return old_segment["id"], source_id, edited["plan_revision_id"]

    def claimed(self, admission_id: str, *, worker="admission-test-worker"):
        job = self.services["jobs"].claim(worker)
        self.assertIsNotNone(job)
        self.assertEqual(job.id, admission_id)
        return job

    def run_claimed(self, job, *, cancel_event=None, progress_hook=None):
        assert job.lease_owner is not None
        token = current_job_lease.set(
            {"job_id": job.id, "owner_id": job.lease_owner, "lease_generation": job.lease_generation}
        )
        try:
            return self.services["workflow_handlers"].run_generation_admission(
                job.payload_json, progress_hook or (lambda _value, _detail: None),
                cancel_event or threading.Event(),
            )
        finally:
            current_job_lease.reset(token)

    def test_http_admits_without_preparing_and_replays_one_job(self):
        with patch.object(GenerationService, "prepare_start", side_effect=AssertionError("HTTP prepared")):
            first = self.admit()
            replay = self.admit()
        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual(replay.status_code, 202, replay.get_json())
        receipt = first.get_json()
        self.assertEqual(receipt, replay.get_json())
        self.assertEqual(receipt["id"], receipt["job_id"])
        self.assertEqual(receipt["stage"], "preparing")
        self.assertEqual(receipt["speech_plan_revision_id"], self.revision_id)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertLess(len(first.data), 4096)
        self.assertEqual(self.count("generation.admit"), 1)

    def test_worker_creates_one_child_and_marker_survives_restart(self):
        admission_id = self.admit().get_json()["job_id"]
        first_claim = self.claimed(admission_id)
        marker = self.run_claimed(first_claim)
        self.assertEqual(marker["stage"], "generating")
        self.assertEqual(self.count("generation.run"), 1)
        exact = self.client.get(f"/api/v1/generation-runs/{marker['generation_run_id']}?view=compact")
        self.assertEqual(exact.status_code, 200, exact.get_json())
        self.assertEqual(exact.get_json()["id"], marker["generation_run_id"])
        self.assertNotIn("settings_snapshot", exact.get_json())
        with self.database.session() as session:
            run = session.get(GenerationRun, marker["generation_run_id"])
            run.settings_snapshot_json = {
                **run.settings_snapshot_json,
                "tts": {"service": "test", "api_key": "DO_NOT_EXPOSE_ADMISSION_TEST"},
            }
        full = self.client.get(f"/api/v1/generation-runs/{marker['generation_run_id']}")
        self.assertEqual(full.status_code, 200, full.get_json())
        self.assertNotIn("DO_NOT_EXPOSE_ADMISSION_TEST", full.data.decode())
        self.assertEqual(
            self.client.get(f"/api/v1/generation-runs/{marker['generation_run_id']}?view=unknown").status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/api/v1/generation-runs/missing").status_code, 404
        )
        with self.database.session() as session:
            job = session.get(Job, admission_id)
            self.assertEqual(job.result_json, marker)
            self.assertEqual(session.get(GenerationRun, marker["generation_run_id"]).job_id, marker["job_id"])
            job.lease_expires_at = utcnow() - timedelta(seconds=1)
        reopened = Database(self.database.path)
        self.addCleanup(reopened.dispose)
        retry_queue = JobQueue(reopened)
        second_claim = retry_queue.claim("restarted-worker")
        self.assertIsNotNone(second_claim)
        assert second_claim is not None
        self.assertEqual(second_claim.id, admission_id)
        assert second_claim.lease_owner is not None
        token = current_job_lease.set(
            {"job_id": admission_id, "owner_id": second_claim.lease_owner, "lease_generation": second_claim.lease_generation}
        )
        try:
            with patch.object(GenerationService, "prepare_start", side_effect=AssertionError("prepared twice")):
                replay = run_generation_admission(
                    reopened, retry_queue, self.services["artifacts"],
                    second_claim.payload_json, lambda _value, _detail: None, threading.Event(),
                )
        finally:
            current_job_lease.reset(token)
        self.assertEqual(replay, marker)
        self.assertEqual(self.count("generation.run"), 1)

    def test_admission_commits_child_without_history_projection(self):
        admission_id = self.admit().get_json()["job_id"]
        claim = self.claimed(admission_id)
        with patch.object(
            GenerationService, "_run_payload", side_effect=AssertionError("history projected")
        ):
            marker = self.run_claimed(claim)
        self.assertEqual(marker["stage"], "generating")
        self.assertEqual(self.count("generation.run"), 1)
        with self.database.session() as session:
            runs = list(session.scalars(select(GenerationRun)).all())
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].id, marker["generation_run_id"])
            self.assertEqual(runs[0].job_id, marker["job_id"])
            self.assertEqual(session.get(Job, admission_id).result_json, marker)

    def test_legacy_start_keeps_full_history_projection(self):
        generation = self.services["generation"]
        prepared = generation.prepare_start(
            self.session_id, speech_plan_revision_id=self.revision_id
        )
        with patch.object(
            generation, "_run_payload", wraps=generation._run_payload
        ) as full_projection:
            with self.database.immediate_session() as session:
                result = generation.start_in_session(
                    session, self.session_id, prepared=prepared
                )
        full_projection.assert_called_once()
        self.assertEqual(result["reused_run"], False)
        self.assertEqual(result["session_id"], self.session_id)
        self.assertEqual(result["plan_revision_id"], self.revision_id)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["sequence_number"], 1)
        self.assertIn("label", result)
        self.assertIn("settings_snapshot", result)
        self.assertEqual(self.count("generation.run"), 1)

    def test_lost_lease_cancellation_and_stale_inputs_create_no_child(self):
        for reason in ("lost", "canceled", "stale"):
            with self.subTest(reason=reason):
                admission_id = self.admit(key=f"admission-{reason}-0001").get_json()["job_id"]
                claim = self.claimed(admission_id)
                canceled = threading.Event()
                if reason == "lost":
                    with self.database.session() as session:
                        session.get(Job, admission_id).lease_generation += 1
                elif reason == "canceled":
                    canceled.set()
                else:
                    with self.database.session() as session:
                        session.get(Job, admission_id).payload_json["snapshot_guard"] = "changed"
                    claim.payload_json["snapshot_guard"] = "changed"
                error_type = GenerationAdmissionStaleInputs if reason == "stale" else RuntimeError
                with self.assertRaises(error_type):
                    self.run_claimed(claim, cancel_event=canceled)
                self.assertEqual(self.count("generation.run"), 0)
                with self.database.session() as session:
                    job = session.get(Job, admission_id)
                    job.status = "canceled"
                    job.lease_owner = None
                    job.lease_expires_at = None
                    if reason == "stale":
                        self.assertEqual(job.result_json["code"], "generation_admission_stale_inputs")

    def test_preparation_exception_leaves_job_for_normal_retry(self):
        admission_id = self.admit().get_json()["job_id"]
        worker = Worker(
            self.services["jobs"], "admission-failure-worker",
            {"generation.admit": self.services["workflow_handlers"].run_generation_admission},
        )
        with patch.object(GenerationService, "prepare_start", side_effect=RuntimeError("prepare failed")):
            self.assertTrue(worker.run_once())
        with self.database.session() as session:
            job = session.get(Job, admission_id)
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.attempts, 1)
            self.assertEqual(job.max_attempts, 2)
        self.assertEqual(self.count("generation.run"), 0)

    def test_cancellation_during_preparation_blocks_child_commit(self):
        admission_id = self.admit().get_json()["job_id"]
        claim = self.claimed(admission_id)
        original_prepare = GenerationService.prepare_start

        def prepare_then_cancel(service, *args, **kwargs):
            prepared = original_prepare(service, *args, **kwargs)
            with self.database.immediate_session() as session:
                session.get(Job, admission_id).status = "cancel_requested"
            return prepared

        with patch.object(GenerationService, "prepare_start", prepare_then_cancel):
            with self.assertRaises(RuntimeError):
                self.run_claimed(claim)
        self.assertEqual(self.count("generation.run"), 0)

    def test_cancel_immediately_after_child_commit_cancels_queued_child(self):
        admission_id = self.admit().get_json()["job_id"]
        claim = self.claimed(admission_id)

        def cancel_after_commit(value, _detail):
            if value == 1.0:
                with self.database.immediate_session() as session:
                    self.services["jobs"].request_cancel_in_session(session, admission_id)

        marker = self.run_claimed(claim, progress_hook=cancel_after_commit)
        with self.database.session() as session:
            admission = session.get(Job, admission_id)
            child = session.get(Job, marker["job_id"])
            run = session.get(GenerationRun, marker["generation_run_id"])
            self.assertEqual(admission.status, "cancel_requested")
            self.assertEqual(admission.result_json, marker)
            self.assertEqual(child.status, "canceled")
            self.assertEqual(run.status, "canceled")

    def test_cancel_succeeded_admission_cascades_to_running_child_without_stealing_lease(self):
        admission_id = self.admit().get_json()["job_id"]
        admission_claim = self.claimed(admission_id)
        marker = self.run_claimed(admission_claim)
        self.services["jobs"].complete(
            admission_id, admission_claim.lease_owner, marker,
            lease_generation=admission_claim.lease_generation,
        )
        child_claim = self.services["jobs"].claim("generation-child-worker")
        self.assertIsNotNone(child_claim)
        assert child_claim is not None
        self.assertEqual(child_claim.id, marker["job_id"])
        with self.database.immediate_session() as session:
            self.services["jobs"].request_cancel_in_session(session, admission_id)
        with self.database.session() as session:
            admission = session.get(Job, admission_id)
            child = session.get(Job, marker["job_id"])
            self.assertEqual(admission.status, "succeeded")
            self.assertEqual(child.status, "cancel_requested")
            self.assertEqual(child.lease_owner, child_claim.lease_owner)
            self.assertEqual(child.lease_generation, child_claim.lease_generation)

    def test_invalid_child_marker_cannot_cancel_unrelated_job(self):
        admission_id = self.admit().get_json()["job_id"]
        claim = self.claimed(admission_id)
        marker = self.run_claimed(claim)
        self.services["jobs"].complete(
            admission_id, claim.lease_owner, marker,
            lease_generation=claim.lease_generation,
        )
        with self.database.immediate_session() as session:
            unrelated = Job(
                kind="generation.run", session_id=self.session_id,
                payload_json={"generation_run_id": marker["generation_run_id"]},
            )
            session.add(unrelated)
            session.flush()
            unrelated_id = unrelated.id
            admission = session.get(Job, admission_id)
            admission.result_json = {**marker, "job_id": unrelated_id}
            self.services["jobs"].request_cancel_in_session(session, admission_id)
        with self.database.session() as session:
            self.assertEqual(session.get(Job, unrelated_id).status, "queued")
            self.assertEqual(session.get(Job, marker["job_id"]).status, "queued")

    def test_historical_settings_source_revision_can_be_admitted(self):
        _segment_id, source_id, active_id = self.historical_source()
        response = self.admit(payload={
            "settings_source_run_id": source_id,
            "speech_plan_revision_id": self.revision_id,
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        receipt = response.get_json()
        self.assertEqual(receipt["speech_plan_revision_id"], self.revision_id)
        with self.database.session() as session:
            payload = session.get(Job, receipt["job_id"]).payload_json
            self.assertEqual(payload["active_revision_id_at_admission"], active_id)
            self.assertEqual(payload["request"]["speech_plan_revision_id"], self.revision_id)
        claim = self.claimed(receipt["job_id"])
        marker = self.run_claimed(claim)
        self.assertEqual(marker["speech_plan_revision_id"], self.revision_id)
        with self.database.session() as session:
            self.assertEqual(
                session.get(GenerationRun, marker["generation_run_id"]).plan_revision_id,
                self.revision_id,
            )

    def test_historical_selected_segments_preserve_implicit_revision(self):
        segment_id, source_id, active_id = self.historical_source()
        response = self.admit(payload={
            "segment_ids": [segment_id],
            "generation_run_id": source_id,
            "operation": "regenerate",
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        receipt = response.get_json()
        self.assertEqual(receipt["speech_plan_revision_id"], self.revision_id)
        with self.database.session() as session:
            payload = session.get(Job, receipt["job_id"]).payload_json
            self.assertEqual(payload["active_revision_id_at_admission"], active_id)
            self.assertIsNone(payload["request"]["speech_plan_revision_id"])
        claim = self.claimed(receipt["job_id"])
        marker = self.run_claimed(claim)
        with self.database.session() as session:
            self.assertEqual(
                session.get(GenerationRun, marker["generation_run_id"]).plan_revision_id,
                self.revision_id,
            )

    def test_historical_cross_session_source_and_active_plan_change_fail_closed(self):
        _segment_id, source_id, _active_id = self.historical_source()
        other_response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Other admission test", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        other_id = other_response.get_json()["id"]
        other_plan = self.services["generation"].create_plan(
            other_id, source_revision_id=None, settings={},
            segments=[{"text": "Other sentence."}],
        )
        rejected = self.admit(key="foreign-source-0001", payload={
            "settings_source_run_id": source_id,
            "speech_plan_revision_id": other_plan["active_revision_id"],
        })
        self.assertEqual(rejected.status_code, 409, rejected.get_json())
        self.assertEqual(self.count("generation.admit"), 0)
        with self.database.session() as session:
            foreign_source = GenerationRun(
                session_id=other_id,
                plan_revision_id=other_plan["active_revision_id"],
                sequence_number=1,
                operation="generate",
                status="completed",
                settings_snapshot_json={},
            )
            session.add(foreign_source)
            session.flush()
            foreign_source_id = foreign_source.id
        foreign = self.admit(key="cross-session-source-0001", payload={
            "settings_source_run_id": foreign_source_id,
        })
        self.assertEqual(foreign.status_code, 409, foreign.get_json())
        self.assertEqual(self.count("generation.admit"), 0)

        accepted = self.admit(key="valid-historical-0001", payload={
            "settings_source_run_id": source_id,
            "speech_plan_revision_id": self.revision_id,
        })
        self.assertEqual(accepted.status_code, 202, accepted.get_json())
        with self.database.immediate_session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == self.session_id))
            plan.active_revision_id = self.revision_id
        claim = self.claimed(accepted.get_json()["job_id"])
        with self.assertRaises(GenerationAdmissionStaleInputs):
            self.run_claimed(claim)
        self.assertEqual(self.count("generation.run"), 0)

    def test_preflight_projection_is_stored_with_child_marker(self):
        admission_id = self.admit().get_json()["job_id"]
        claim = self.claimed(admission_id)
        original_prepare = GenerationService.prepare_start

        def prepare_with_preflight(service, *args, **kwargs):
            prepared = original_prepare(service, *args, **kwargs)
            prepared["provider_preflight"] = {"service": "test", "ready": True}
            return prepared

        with patch.object(GenerationService, "prepare_start", prepare_with_preflight):
            marker = self.run_claimed(claim)
        self.assertEqual(marker["provider_preflight"], {"service": "test", "ready": True})
        with self.database.session() as session:
            self.assertEqual(session.get(Job, admission_id).result_json, marker)

    def test_global_defaults_revision_change_after_admission_creates_no_child(self):
        admission_id = self.admit().get_json()["job_id"]
        with self.database.immediate_session() as session:
            setting = session.get(AppSetting, "defaults.tts")
            if setting is None:
                session.add(AppSetting(key="defaults.tts", value_json={}, revision=1))
            else:
                setting.revision += 1
        claim = self.claimed(admission_id)
        with self.assertRaises(GenerationAdmissionStaleInputs):
            self.run_claimed(claim)
        self.assertEqual(self.count("generation.run"), 0)
        with self.database.session() as session:
            self.assertEqual(
                session.get(Job, admission_id).result_json["code"],
                "generation_admission_stale_inputs",
            )

    def test_session_tts_revision_change_during_preparation_creates_no_child(self):
        admission_id = self.admit().get_json()["job_id"]
        claim = self.claimed(admission_id)
        original_prepare = GenerationService.prepare_start

        def prepare_then_update_setting(service, *args, **kwargs):
            prepared = original_prepare(service, *args, **kwargs)
            with self.database.immediate_session() as session:
                setting = session.get(SessionSetting, (self.session_id, "tts"))
                if setting is None:
                    session.add(SessionSetting(
                        session_id=self.session_id, section="tts", value_json={}, revision=1
                    ))
                else:
                    setting.revision += 1
            return prepared

        with patch.object(GenerationService, "prepare_start", prepare_then_update_setting):
            with self.assertRaises(GenerationAdmissionStaleInputs):
                self.run_claimed(claim)
        self.assertEqual(self.count("generation.run"), 0)
