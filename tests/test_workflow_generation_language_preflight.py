"""Workflow enqueue guards reject known unsupported TTS routes early."""

from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    SessionSetting,
    WorkflowExecutionPlan,
)
from tests.web_test_support import prepare_web_test_data_root


class WorkflowGenerationLanguagePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            public_origin="https://pandrator.example",
        )
        self.extension = self.app.extensions["pandrator"]
        self.client = self.app.test_client()
        authorization = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()
        self.headers = {"X-CSRF-Token": authorization["csrf_token"]}
        self.sequence = 0

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def _session_with_narration(self, *, language: str | None = None) -> tuple[str, str]:
        self.sequence += 1
        created = self.client.post(
            "/api/v1/sessions",
            json={
                "name": f"Preflight {self.sequence}",
                "workflow_kind": "audiobook",
                "source_language": "auto",
            },
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        source_path = Path(self.temporary.name) / f"prepared-{self.sequence}.json"
        source_path.write_text('[{"text":"Narration."}]\n', encoding="utf-8")
        artifact = ArtifactService(
            self.extension["database"], self.paths
        ).register(
            source_path,
            kind="json",
            role="prepared_text",
            session_id=session_id,
            metadata={"language": language} if language else {},
        )
        with self.extension["database"].session() as db_session:
            tts_setting = db_session.get(SessionSetting, (session_id, "tts"))
            if tts_setting is None:
                tts_setting = SessionSetting(
                    session_id=session_id, section="tts", revision=1
                )
                db_session.add(tts_setting)
            tts_setting.value_json = {
                **dict(tts_setting.value_json or {}),
                "service": "Silero",
                "silero_model": "v3_en",
                "voice_mode_version": 1,
                "casting_enabled": False,
            }
            tts_setting.revision = max(1, int(tts_setting.revision or 0))
        return session_id, artifact.id

    def _session_with_upload_only(self) -> tuple[str, str]:
        self.sequence += 1
        created = self.client.post(
            "/api/v1/sessions",
            json={
                "name": f"Upload only {self.sequence}",
                "workflow_kind": "audiobook",
                "source_language": "auto",
            },
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        source_path = Path(self.temporary.name) / f"upload-{self.sequence}.json"
        source_path.write_text('[{"text":"Narration."}]\n', encoding="utf-8")
        artifact = ArtifactService(
            self.extension["database"], self.paths
        ).register(
            source_path,
            kind="json",
            role="upload",
            session_id=session_id,
            metadata={"original_filename": source_path.name},
        )
        with self.extension["database"].session() as db_session:
            tts_setting = db_session.get(SessionSetting, (session_id, "tts"))
            if tts_setting is None:
                tts_setting = SessionSetting(
                    session_id=session_id, section="tts", revision=1
                )
                db_session.add(tts_setting)
            tts_setting.value_json = {
                **dict(tts_setting.value_json or {}),
                "service": "Silero",
                "silero_model": "v3_en",
                "voice_mode_version": 1,
                "casting_enabled": False,
            }
            tts_setting.revision = max(1, int(tts_setting.revision or 0))
        return session_id, artifact.id

    def _counts(self) -> tuple[int, int, int]:
        with self.extension["database"].session() as db_session:
            return (
                int(db_session.scalar(select(func.count()).select_from(Job)) or 0),
                int(
                    db_session.scalar(
                        select(func.count()).select_from(GenerationRun)
                    )
                    or 0
                ),
                int(
                    db_session.scalar(
                        select(func.count()).select_from(WorkflowExecutionPlan)
                    )
                    or 0
                ),
            )

    def _add_plan_revision(
        self,
        session_id: str,
        *,
        source_artifact_id: str | None = None,
        revision_number: int = 1,
        operation_json: dict | None = None,
        language: str = "en",
        active: bool = True,
    ) -> str:
        with self.extension["database"].immediate_session() as db_session:
            plan = db_session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == session_id
                )
            )
            if plan is None:
                plan = GenerationPlan(session_id=session_id)
                db_session.add(plan)
                db_session.flush()
            revision_settings = {}
            if source_artifact_id is not None:
                revision_settings["_source_artifact_id"] = source_artifact_id
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=revision_number,
                settings_json=revision_settings,
                operation_json=operation_json or {},
                content_hash=f"fixture-{session_id}-{revision_number}",
            )
            db_session.add(revision)
            db_session.flush()
            db_session.add(
                GenerationSegment(
                    plan_revision_id=revision.id,
                    ordinal=0,
                    text="Narration.",
                    language=language,
                )
            )
            if active:
                plan.active_revision_id = revision.id
            return revision.id

    def _invalid_revision_request(self, case: str) -> tuple[str, str]:
        if case == "missing_input":
            session_id, upload_id = self._session_with_upload_only()
            revision_id = self._add_plan_revision(
                session_id, source_artifact_id=upload_id
            )
            return session_id, revision_id

        session_id, source_id = self._session_with_narration(language="en")
        if case == "missing":
            self._add_plan_revision(session_id, source_artifact_id=source_id)
            return session_id, "missing-revision-id"
        if case == "foreign":
            self._add_plan_revision(session_id, source_artifact_id=source_id)
            other_session, other_source = self._session_with_narration(language="en")
            return session_id, self._add_plan_revision(
                other_session, source_artifact_id=other_source
            )
        if case == "stale":
            stale_id = self._add_plan_revision(
                session_id, source_artifact_id=source_id, active=False
            )
            self._add_plan_revision(
                session_id,
                source_artifact_id=source_id,
                revision_number=2,
            )
            return session_id, stale_id
        if case == "conflicting":
            stale_id = self._add_plan_revision(
                session_id,
                source_artifact_id=source_id,
                active=False,
            )
            self._add_plan_revision(
                session_id,
                source_artifact_id=source_id,
                revision_number=2,
                operation_json={"prepared": True},
            )
            return session_id, stale_id
        if case == "source_mismatch":
            return session_id, self._add_plan_revision(
                session_id,
                source_artifact_id="different-generation-input",
                operation_json={"prepared": True},
            )
        raise AssertionError(f"unknown revision test case: {case}")

    def _run_with_revision(self, session_id: str, revision_id: str):
        return self.client.post(
            f"/api/v1/sessions/{session_id}/stages/generate_audio/run",
            json={"speech_plan_revision_id": revision_id},
            headers=self.headers,
        )

    def _create_plan_with_revision(self, session_id: str, revision_id: str):
        return self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json={
                "target_stage": "generate_audio",
                "overrides": {"speech_plan_revision_id": revision_id},
            },
            headers=self.headers,
        )

    def test_legacy_direct_stage_rejects_known_unsupported_language_before_enqueue(self):
        session_id, _artifact_id = self._session_with_narration(language="fr")
        before = self._counts()

        with patch(
            "requests.Session.request",
            side_effect=AssertionError("language preflight must not make network calls"),
        ):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/stages/generate_audio/run",
                json={},
                headers=self.headers,
            )

        self.assertEqual(409, response.status_code, response.get_json())
        self.assertIn(
            "v3_en does not support 'fr'", response.get_json()["error"]["message"]
        )
        self.assertEqual(before, self._counts())

    def test_workflow_plan_creation_rejects_before_persisting_plan_or_job(self):
        session_id, _artifact_id = self._session_with_narration(language="fr")
        before = self._counts()

        with patch(
            "requests.Session.request",
            side_effect=AssertionError("language preflight must not make network calls"),
        ):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/workflow-plans",
                json={"target_stage": "generate_audio", "overrides": {}},
                headers=self.headers,
            )

        self.assertEqual(409, response.status_code, response.get_json())
        self.assertIn(
            "v3_en does not support 'fr'", response.get_json()["error"]["message"]
        )
        self.assertEqual(before, self._counts())

    def test_plan_execution_rechecks_stored_payload_before_enqueue(self):
        session_id, _artifact_id = self._session_with_narration()
        planned = self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json={
                "target_stage": "generate_audio",
                "overrides": {
                    "service": "Silero",
                    "silero_model": "v3_en",
                    "language": "en",
                },
            },
            headers=self.headers,
        )
        self.assertEqual(201, planned.status_code, planned.get_json())
        plan_id = planned.get_json()["plan_id"]
        with self.extension["database"].session() as db_session:
            record = db_session.get(WorkflowExecutionPlan, plan_id)
            stored = copy.deepcopy(record.plan_json)
            execution_payload = stored["_execution"]["payload"]
            self.assertTrue(
                execution_payload["_tts_language_preflight_input_selected"]
            )
            execution_payload["settings"].update(language="fr", target_language="fr")
            record.plan_json = stored
        before = self._counts()

        with patch(
            "requests.Session.request",
            side_effect=AssertionError("language preflight must not make network calls"),
        ):
            response = self.client.post(
                f"/api/v1/workflow-plans/{plan_id}/execute",
                json={
                    "plan_digest": planned.get_json()["plan_digest"],
                    "accepted_confirmations": planned.get_json()[
                        "required_confirmations"
                    ],
                },
                headers={
                    **self.headers,
                    "Idempotency-Key": "preflight-plan-execute",
                },
            )

        self.assertEqual(409, response.status_code, response.get_json())
        self.assertIn(
            "v3_en does not support 'fr'", response.get_json()["error"]["message"]
        )
        self.assertEqual(before, self._counts())

    def test_supported_base_language_still_queues_direct_generation(self):
        session_id, _artifact_id = self._session_with_narration(language="en")

        with patch(
            "requests.Session.request",
            side_effect=AssertionError("language preflight must not make network calls"),
        ):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/stages/generate_audio/run",
                json={},
                headers=self.headers,
            )

        self.assertEqual(202, response.status_code, response.get_json())
        self.assertEqual(1, self._counts()[0])

    def test_direct_run_rejects_explicit_revision_identity_and_input_errors(self):
        for case in (
            "missing",
            "foreign",
            "stale",
            "conflicting",
            "source_mismatch",
            "missing_input",
        ):
            with self.subTest(case=case):
                session_id, revision_id = self._invalid_revision_request(case)
                before = self._counts()
                response = self._run_with_revision(session_id, revision_id)
                self.assertEqual(409, response.status_code, response.get_json())
                self.assertEqual(before, self._counts())

    def test_workflow_plan_creation_rejects_explicit_revision_identity_and_input_errors(self):
        for case in (
            "missing",
            "foreign",
            "stale",
            "conflicting",
            "source_mismatch",
            "missing_input",
        ):
            with self.subTest(case=case):
                session_id, revision_id = self._invalid_revision_request(case)
                before = self._counts()
                response = self._create_plan_with_revision(session_id, revision_id)
                self.assertEqual(409, response.status_code, response.get_json())
                self.assertEqual(before, self._counts())

    def test_matching_and_legacy_unpinned_speech_revisions_still_queue(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                session_id, source_id = self._session_with_narration(language="en")
                revision_id = self._add_plan_revision(
                    session_id,
                    source_artifact_id=source_id if explicit else None,
                    operation_json={"prepared": True} if explicit else {},
                )
                settings = (
                    {"speech_plan_revision_id": revision_id}
                    if explicit
                    else {}
                )
                response = self.client.post(
                    f"/api/v1/sessions/{session_id}/stages/generate_audio/run",
                    json=settings,
                    headers=self.headers,
                )
                self.assertEqual(202, response.status_code, response.get_json())

    def test_automatic_prerequisite_chaining_without_explicit_revision_still_queues(self):
        session_id, _upload_id = self._session_with_upload_only()
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/stages/generate_audio/run",
            json={},
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())

    def test_workflow_plan_execution_rechecks_revision_and_source_before_enqueue(self):
        cases = ("missing", "foreign", "stale", "conflicting", "source_mismatch")
        for case in cases:
            with self.subTest(case=case):
                session_id, source_id = self._session_with_narration(language="en")
                if case == "stale" or case == "conflicting":
                    stale_id = self._add_plan_revision(
                        session_id, source_artifact_id=source_id, active=False
                    )
                    active_id = self._add_plan_revision(
                        session_id,
                        source_artifact_id=source_id,
                        revision_number=2,
                    )
                else:
                    active_id = self._add_plan_revision(
                        session_id,
                        source_artifact_id=source_id,
                    )
                    stale_id = ""
                if case == "foreign":
                    foreign_session, foreign_source = self._session_with_narration(
                        language="en"
                    )
                    invalid_id = self._add_plan_revision(
                        foreign_session, source_artifact_id=foreign_source
                    )
                elif case == "stale":
                    invalid_id = stale_id
                elif case == "conflicting":
                    invalid_id = stale_id
                elif case == "missing":
                    invalid_id = "missing-revision-id"
                else:
                    invalid_id = active_id

                planned = self.client.post(
                    f"/api/v1/sessions/{session_id}/workflow-plans",
                    json={"target_stage": "generate_audio", "overrides": {}},
                    headers=self.headers,
                )
                self.assertEqual(201, planned.status_code, planned.get_json())
                plan_id = planned.get_json()["plan_id"]
                if case == "source_mismatch":
                    alternate_path = (
                        Path(self.temporary.name) / f"alternate-{self.sequence}.json"
                    )
                    alternate_path.write_text(
                        '[{"text":"Other narration."}]\n', encoding="utf-8"
                    )
                    alternate = ArtifactService(
                        self.extension["database"], self.paths
                    ).register(
                        alternate_path,
                        kind="json",
                        role="prepared_text",
                        session_id=session_id,
                        metadata={"language": "en"},
                    )
                with self.extension["database"].session() as db_session:
                    record = db_session.get(WorkflowExecutionPlan, plan_id)
                    stored = copy.deepcopy(record.plan_json)
                    execution_payload = stored["_execution"]["payload"]
                    if case == "conflicting":
                        execution_payload["speech_plan_revision_id"] = active_id
                        execution_payload["settings"][
                            "speech_plan_revision_id"
                        ] = invalid_id
                    else:
                        execution_payload["settings"][
                            "speech_plan_revision_id"
                        ] = invalid_id
                        if case == "source_mismatch":
                            execution_payload["speech_plan_revision_id"] = active_id
                            execution_payload["source_artifact_id"] = alternate.id
                    record.plan_json = stored
                before = self._counts()
                body = planned.get_json()
                response = self.client.post(
                    f"/api/v1/workflow-plans/{plan_id}/execute",
                    json={
                        "plan_digest": body["plan_digest"],
                        "accepted_confirmations": body["required_confirmations"],
                    },
                    headers={
                        **self.headers,
                        "Idempotency-Key": f"revision-preflight-{case}",
                    },
                )
                self.assertEqual(409, response.status_code, response.get_json())
                self.assertEqual(before, self._counts())

    def test_worker_json_reuse_rejects_known_source_mismatch(self):
        session_id, source_id = self._session_with_narration(language="en")
        self._add_plan_revision(
            session_id,
            source_artifact_id="different-generation-input",
        )
        handlers = self.extension["workflow_handlers"]
        payload = {
            "session_id": session_id,
            "source_artifact_id": source_id,
            "settings": {},
            "resolved_settings_snapshot": {},
        }
        with self.assertRaisesRegex(ValueError, "different generation input"):
            handlers._run_reviewable_generation(
                payload,
                lambda *_args, **_kwargs: None,
                threading.Event(),
            )

    def test_worker_final_guard_rechecks_active_revision_before_freeze(self):
        session_id, source_id = self._session_with_narration(language="en")
        self._add_plan_revision(
            session_id,
            source_artifact_id=source_id,
        )
        new_revision_id = self._add_plan_revision(
            session_id,
            source_artifact_id=source_id,
            revision_number=2,
            active=False,
        )
        database = self.extension["database"]
        original_immediate_session = database.immediate_session

        @contextmanager
        def switch_revision_before_final_check():
            with original_immediate_session() as db_session:
                plan = db_session.scalar(
                    select(GenerationPlan).where(
                        GenerationPlan.session_id == session_id
                    )
                )
                plan.active_revision_id = new_revision_id
                db_session.flush()
                yield db_session

        handlers = self.extension["workflow_handlers"]
        payload = {
            "session_id": session_id,
            "source_artifact_id": source_id,
            "settings": {},
            "resolved_settings_snapshot": {},
        }
        with (
            patch.object(
                database,
                "immediate_session",
                side_effect=switch_revision_before_final_check,
            ),
            patch(
                "pandrator.web.speech_plan_workspace.freeze_speech_snapshot",
                side_effect=AssertionError("stale revision must not be frozen"),
            ) as freeze,
        ):
            with self.assertRaisesRegex(ValueError, "no longer available"):
                handlers._run_reviewable_generation(
                    payload,
                    lambda *_args, **_kwargs: None,
                    threading.Event(),
                )
        freeze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
