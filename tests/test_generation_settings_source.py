"""Generation settings-source selection on disposable local fixtures."""

from __future__ import annotations

import tempfile
import threading
import unittest
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import GenerationRun, Job
from pandrator.web.settings_policy import stable_hash
from pandrator_mcp.schemas.generation import (
    GenerateSpeechPlanInput,
    RegenerateSegmentsInput,
)
from pandrator_mcp.tools.generation import generate_speech_plan, regenerate_segments


class _RegenerationApplication:
    def __init__(self) -> None:
        self.call: dict[str, object] | None = None

    def start_generation_run(self, session_id: str, **kwargs):
        self.call = {"session_id": session_id, **kwargs}
        return {
            "id": "admission-job-1",
            "job_id": "admission-job-1",
            "session_id": session_id,
            "status": "queued",
            "stage": "preparing",
        }


class GenerationSettingsSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        app = create_app(
            data_root=temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.services = app.extensions["pandrator"]
        self.database = self.services["database"]
        self.addCleanup(self.database.dispose)
        self.client = app.test_client()
        token = bootstrap.issue()
        csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token})
        self.headers = {"X-CSRF-Token": csrf.get_json()["csrf_token"]}
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Settings source test", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.session_id = response.get_json()["id"]
        plan = self.services["generation"].create_plan(
            self.session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "A short sentence."}],
        )
        self.revision_id = plan["active_revision_id"]
        self.segment_id = self.services["generation"].list_segments(self.session_id)[
            "items"
        ][0]["id"]

    def make_source_run(
        self,
        sequence: int,
        *,
        prompt: str,
        performance_enabled: bool,
        casting_enabled: bool,
        context_mode: str,
    ) -> str:
        snapshot, _ = self.services["workspace_settings"].resolve(self.session_id)
        tts = dict(snapshot.get("tts") or {})
        tts.update(
            {
                "generation_prompt": prompt,
                "performance_enabled": performance_enabled,
                "casting_enabled": casting_enabled,
                "performance_allow_vocalizations": performance_enabled,
                "tts_context_mode": context_mode,
            }
        )
        snapshot["tts"] = tts
        with self.database.immediate_session() as session:
            run = GenerationRun(
                session_id=self.session_id,
                plan_revision_id=self.revision_id,
                sequence_number=sequence,
                operation="generate",
                status="completed",
                settings_snapshot_json=snapshot,
                settings_hash=stable_hash(snapshot),
            )
            session.add(run)
            session.flush()
            return run.id

    def set_current_settings(self) -> None:
        current = self.services["workspace_settings"].get(self.session_id, "tts")
        self.services["workspace_settings"].patch(
            self.session_id,
            "tts",
            current["revision"],
            {
                "generation_prompt": "Speak the revised line in a quiet whisper.",
                "performance_enabled": False,
                "casting_enabled": True,
                "performance_allow_vocalizations": True,
                "tts_context_mode": "both",
            },
        )

    def admit(self, key: str, payload: dict[str, object]):
        return self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-admissions",
            json=payload,
            headers={**self.headers, "Idempotency-Key": key},
        )

    def run_admission(self, admission_id: str) -> dict[str, object]:
        job = self.services["jobs"].claim("settings-source-test-worker")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.id, admission_id)
        assert job.lease_owner is not None
        lease_token = current_job_lease.set(
            {
                "job_id": job.id,
                "owner_id": job.lease_owner,
                "lease_generation": job.lease_generation,
            }
        )
        try:
            return self.services["workflow_handlers"].run_generation_admission(
                job.payload_json,
                lambda _value, _detail: None,
                threading.Event(),
            )
        finally:
            current_job_lease.reset(lease_token)

    def get_run(self, run_id: str) -> GenerationRun:
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            assert run is not None
            session.expunge(run)
            return run

    def test_mcp_regeneration_defaults_to_current_and_keeps_take_origin(self):
        source_id = self.make_source_run(
            1,
            prompt="Old plain delivery.",
            performance_enabled=False,
            casting_enabled=False,
            context_mode="off",
        )
        audio_origin_id = self.make_source_run(
            2,
            prompt="Audio lineage source.",
            performance_enabled=False,
            casting_enabled=False,
            context_mode="off",
        )
        self.set_current_settings()

        arguments = RegenerateSegmentsInput(
            session_id=self.session_id,
            segment_ids=[self.segment_id],
            generation_run_id=audio_origin_id,
            idempotency_key="settings:current:mcp-001",
        )
        self.assertEqual(arguments.settings_source, "current")
        fake_application = _RegenerationApplication()
        regenerate_segments(
            SimpleNamespace(require_application=lambda: fake_application), arguments
        )
        assert fake_application.call is not None
        self.assertEqual(fake_application.call["settings_source"], "current")
        self.assertEqual(fake_application.call["generation_run_id"], audio_origin_id)
        self.assertNotEqual(source_id, audio_origin_id)

        admitted = self.admit(
            "settings:current:mcp-backend-001",
            {
                "operation": "regenerate",
                "segment_ids": [self.segment_id],
                "generation_run_id": audio_origin_id,
                "settings_source": arguments.settings_source,
            },
        )
        self.assertEqual(admitted.status_code, 202, admitted.get_json())
        marker = self.run_admission(admitted.get_json()["job_id"])
        run = self.get_run(str(marker["generation_run_id"]))
        snapshot = run.settings_snapshot_json
        current_tts = snapshot["tts"]
        self.assertEqual(
            current_tts["generation_prompt"],
            "Speak the revised line in a quiet whisper.",
        )
        self.assertFalse(current_tts["performance_enabled"])
        self.assertTrue(current_tts["casting_enabled"])
        self.assertTrue(current_tts["performance_allow_vocalizations"])
        self.assertEqual(current_tts["tts_context_mode"], "both")
        self.assertEqual(run.source_generation_run_id, audio_origin_id)
        receipt = marker["settings_source_receipt"]
        self.assertEqual(receipt["effective"], "current")
        self.assertIsNone(receipt["settings_source_run_id"])
        self.assertEqual(receipt["generation_run_id"], audio_origin_id)
        self.assertEqual(receipt["source_generation_run_id"], audio_origin_id)
        self.assertEqual(receipt["settings_hash"], run.settings_hash)
        self.assertEqual(
            receipt["flags"],
            {
                "performance_enabled": False,
                "casting_enabled": True,
                "performance_allow_vocalizations": True,
                "tts_context_mode": "both",
            },
        )

    def test_explicit_run_replays_frozen_settings_separately_from_audio_origin(self):
        settings_source_id = self.make_source_run(
            1,
            prompt="Old plain delivery.",
            performance_enabled=False,
            casting_enabled=False,
            context_mode="off",
        )
        audio_origin_id = self.make_source_run(
            2,
            prompt="Different take lineage.",
            performance_enabled=True,
            casting_enabled=True,
            context_mode="both",
        )
        self.set_current_settings()

        admitted = self.admit(
            "settings:run:replay-001",
            {
                "operation": "regenerate",
                "segment_ids": [self.segment_id],
                "generation_run_id": audio_origin_id,
                "settings_source": "run",
                "settings_source_run_id": settings_source_id,
            },
        )
        self.assertEqual(admitted.status_code, 202, admitted.get_json())
        marker = self.run_admission(admitted.get_json()["job_id"])
        run = self.get_run(str(marker["generation_run_id"]))
        tts = run.settings_snapshot_json["tts"]
        self.assertEqual(tts["generation_prompt"], "Old plain delivery.")
        self.assertFalse(tts["performance_enabled"])
        self.assertFalse(tts["casting_enabled"])
        self.assertFalse(tts["performance_allow_vocalizations"])
        self.assertEqual(tts["tts_context_mode"], "off")
        self.assertEqual(run.source_generation_run_id, audio_origin_id)
        receipt = marker["settings_source_receipt"]
        self.assertEqual(receipt["effective"], "run")
        self.assertEqual(receipt["settings_source_run_id"], settings_source_id)
        self.assertEqual(receipt["generation_run_id"], audio_origin_id)
        self.assertEqual(receipt["source_generation_run_id"], audio_origin_id)
        self.assertEqual(receipt["settings_source_settings_hash"], self.get_run(settings_source_id).settings_hash)

    def test_matching_run_missing_only_uses_frozen_settings_and_missing_source_fails(self):
        source_id = self.make_source_run(
            1,
            prompt="Frozen selection settings.",
            performance_enabled=False,
            casting_enabled=False,
            context_mode="off",
        )
        self.set_current_settings()
        generation = self.services["generation"]
        prepared = generation.prepare_start(
            self.session_id,
            speech_plan_revision_id=self.revision_id,
            settings_source="matching_run",
            missing_only=True,
        )
        self.assertEqual(prepared["requested_segment_ids"], [self.segment_id])
        with self.database.immediate_session() as session:
            result = generation.start_in_session(
                session, self.session_id, prepared=prepared
            )
        run = self.get_run(str(result["id"]))
        tts = run.settings_snapshot_json["tts"]
        self.assertEqual(tts["generation_prompt"], "Frozen selection settings.")
        self.assertFalse(tts["performance_enabled"])
        self.assertEqual(tts["tts_context_mode"], "off")
        self.assertEqual(
            result["settings_source_receipt"]["settings_source_run_id"], source_id
        )
        self.assertTrue(run.settings_snapshot_json["missing_only"])

        fresh = self.client.post(
            "/api/v1/sessions",
            json={"name": "No matching source", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        other_session_id = fresh.get_json()["id"]
        other_plan = generation.create_plan(
            other_session_id,
            source_revision_id=None,
            settings={},
            segments=[{"text": "An ungenerated sentence."}],
        )
        other_segment_id = generation.list_segments(other_session_id)["items"][0]["id"]
        missing = self.client.post(
            f"/api/v1/sessions/{other_session_id}/generation-admissions",
            json={
                "operation": "regenerate",
                "segment_ids": [other_segment_id],
                "settings_source": "matching_run",
            },
            headers={**self.headers, "Idempotency-Key": "settings:no-match-001"},
        )
        self.assertEqual(missing.status_code, 409, missing.get_json())
        self.assertIn(
            "No compatible generation run",
            missing.get_json()["error"]["message"],
        )
        self.assertIsNotNone(other_plan["active_revision_id"])

    def test_same_key_changed_selector_conflicts_and_explicit_run_is_session_fenced(self):
        source_id = self.make_source_run(
            1,
            prompt="Frozen source.",
            performance_enabled=False,
            casting_enabled=False,
            context_mode="off",
        )
        first = self.admit(
            "settings:same-key:001",
            {
                "operation": "regenerate",
                "segment_ids": [self.segment_id],
                "settings_source": "current",
            },
        )
        self.assertEqual(first.status_code, 202, first.get_json())
        conflict = self.admit(
            "settings:same-key:001",
            {
                "operation": "regenerate",
                "segment_ids": [self.segment_id],
                "settings_source": "run",
                "settings_source_run_id": source_id,
            },
        )
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        with self.database.session() as session:
            count = int(
                session.scalar(
                    select(func.count()).select_from(Job).where(
                        Job.kind == "generation.admit"
                    )
                )
                or 0
            )
        self.assertEqual(count, 1)

        second_session = self.client.post(
            "/api/v1/sessions",
            json={"name": "Foreign session", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        generation = self.services["generation"]
        other_plan = generation.create_plan(
            second_session,
            source_revision_id=None,
            settings={},
            segments=[{"text": "A foreign sentence."}],
        )
        with self.assertRaisesRegex(ValueError, "not available in this session"):
            generation.prepare_start(
                second_session,
                speech_plan_revision_id=other_plan["active_revision_id"],
                settings_source="run",
                settings_source_run_id=source_id,
            )

    def test_mcp_input_defaults_and_historical_selector_validation(self):
        regenerate = RegenerateSegmentsInput(
            session_id=self.session_id,
            segment_ids=[self.segment_id],
            idempotency_key="settings:input:regen-001",
        )
        self.assertEqual(regenerate.settings_source, "current")
        with self.assertRaises(ValidationError):
            RegenerateSegmentsInput(
                session_id=self.session_id,
                segment_ids=[self.segment_id],
                idempotency_key="settings:input:bad-001",
                settings_source="run",
            )
        plan_generation = GenerateSpeechPlanInput(
            session_id=self.session_id,
            speech_plan_revision_id=self.revision_id,
            idempotency_key="settings:input:plan-001",
            missing_only=True,
            settings_source="run",
            settings_source_run_id="source-run-1",
        )
        self.assertTrue(plan_generation.missing_only)
        self.assertEqual(plan_generation.settings_source, "run")
        fake_application = _RegenerationApplication()
        generate_speech_plan(
            SimpleNamespace(require_application=lambda: fake_application),
            plan_generation,
        )
        assert fake_application.call is not None
        self.assertTrue(fake_application.call["missing_only"])
        self.assertEqual(fake_application.call["settings_source"], "run")
        self.assertEqual(
            fake_application.call["settings_source_run_id"], "source-run-1"
        )


if __name__ == "__main__":
    unittest.main()
