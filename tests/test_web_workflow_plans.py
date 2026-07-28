import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    Job,
    OutcomePlan,
    Provider,
    SessionRecord,
    SessionSetting,
    SessionStageSelection,
    SourceAsset,
    WorkflowExecutionPlan,
    utcnow,
)
from pandrator.web.workflow_plans import canonical_digest
from tests.web_test_support import prepare_web_test_data_root


class WorkflowExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
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
            "/api/v1/auth/bootstrap",
            json={"token": token},
        ).get_json()
        self.headers = {
            "X-CSRF-Token": authorization["csrf_token"],
        }
        self.source_counter = 0

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def _ready_session(
        self,
        *,
        workflow_kind="audiobook",
        suffix="txt",
    ):
        created = self.client.post(
            "/api/v1/sessions",
            json={
                "name": f"Plan fixture {self.source_counter}",
                "workflow_kind": workflow_kind,
            },
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        self.source_counter += 1
        source_path = (
            Path(self.temporary.name)
            / f"workflow-plan-{self.source_counter}.{suffix}"
        )
        if suffix == "srt":
            source_path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
        else:
            source_path.write_bytes(b"workflow plan source fixture")
        artifact = ArtifactService(
            self.extension["database"],
            self.extension["paths"],
        ).register(
            source_path,
            kind=suffix,
            role="upload",
            session_id=session_id,
            metadata={"original_filename": source_path.name},
        )
        source_asset = self.extension[
            "source_library"
        ].ensure_for_artifact(
            artifact.id,
            display_name=source_path.name,
            kind=suffix,
        )
        self.extension["source_library"].attach(
            session_id,
            source_asset.id,
        )
        return session_id, artifact.id, source_asset.id

    def _plan(
        self,
        session_id,
        *,
        target_stage="generate_audio",
        overrides=None,
    ):
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json={
                "target_stage": target_stage,
                "overrides": overrides or {},
            },
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _execute(
        self,
        plan,
        *,
        key,
        digest=None,
        confirmations=None,
    ):
        return self.client.post(
            f"/api/v1/workflow-plans/{plan['plan_id']}/execute",
            json={
                "plan_digest": digest or plan["plan_digest"],
                "accepted_confirmations": (
                    plan["required_confirmations"]
                    if confirmations is None
                    else confirmations
                ),
            },
            headers={
                **self.headers,
                "Idempotency-Key": key,
            },
        )

    def _job_count(self):
        with self.extension["database"].session() as db_session:
            return db_session.scalar(
                select(func.count()).select_from(Job)
            )

    def test_plan_is_reviewable_canonical_and_execute_once(self):
        session_id, _artifact_id, _asset_id = self._ready_session()
        plan = self._plan(session_id)
        public_plan = {
            key: value
            for key, value in plan.items()
            if key != "plan_digest"
        }

        self.assertEqual(
            plan["plan_digest"],
            canonical_digest(public_plan),
        )
        self.assertNotIn("execution", plan)
        self.assertNotIn("_execution", plan)
        with self.extension["database"].session() as db_session:
            stored = db_session.get(
                WorkflowExecutionPlan,
                plan["plan_id"],
            )
            self.assertEqual(
                "workflow.continue",
                stored.plan_json["_execution"]["job_kind"],
            )
        self.assertTrue(plan["ordered_steps"])
        self.assertEqual(
            "generate_audio",
            plan["ordered_steps"][-1]["stage"],
        )

        before = self._job_count()
        first = self._execute(plan, key="execute-reviewed-plan")
        replay = self._execute(plan, key="execute-reviewed-plan")
        duplicate = self._execute(plan, key="execute-plan-again")

        self.assertEqual(202, first.status_code, first.get_json())
        self.assertEqual(202, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(before + 1, self._job_count())
        self.assertEqual(409, duplicate.status_code)
        self.assertEqual(
            "plan_consumed",
            duplicate.get_json()["error"]["code"],
        )
        self.assertNotIn(
            "payload",
            first.get_data(as_text=True).lower(),
        )

    def test_deterministic_cleaning_does_not_claim_an_external_llm(self):
        session_id, _artifact_id, _asset_id = self._ready_session()
        deterministic = self._plan(
            session_id,
            target_stage="clean_source",
            overrides={
                "source_cleaning": {
                    "agentic": False,
                }
            },
        )
        self.assertEqual([], deterministic["required_confirmations"])
        self.assertEqual([], deterministic["selected_providers"])

        agentic = self._plan(
            session_id,
            target_stage="clean_source",
            overrides={
                "source_cleaning": {
                    "agentic": True,
                }
            },
        )
        self.assertIn(
            "external_provider",
            agentic["required_confirmations"],
        )
        self.assertIn(
            "estimated_cost_unknown",
            agentic["required_confirmations"],
        )

    def test_every_workflow_kind_and_primary_source_type_can_be_planned(self):
        cases = (
            ("audiobook", "txt", "generate_audio"),
            ("voiceover", "mp4", "generate_audio"),
            ("subtitles", "srt", "export"),
        )
        for workflow_kind, suffix, target_stage in cases:
            with self.subTest(
                workflow_kind=workflow_kind,
                suffix=suffix,
            ):
                session_id, _artifact_id, _asset_id = (
                    self._ready_session(
                        workflow_kind=workflow_kind,
                        suffix=suffix,
                    )
                )
                plan = self._plan(
                    session_id,
                    target_stage=target_stage,
                )
                self.assertEqual(
                    workflow_kind,
                    plan["session"]["workflow_kind"],
                )
                self.assertEqual(
                    target_stage,
                    plan["target_stage"],
                )
                self.assertTrue(plan["source"]["content_hash"])

    def test_relevant_state_changes_make_a_plan_stale(self):
        def session_change(db_session, session_id, _artifact_id, _asset_id):
            record = db_session.get(SessionRecord, session_id)
            record.revision += 1

        def source_change(db_session, _session_id, artifact_id, asset_id):
            artifact = db_session.get(Artifact, artifact_id)
            asset = db_session.get(SourceAsset, asset_id)
            artifact.content_hash = "a" * 64
            asset.content_hash = "a" * 64
            asset.revision += 1

        def outcome_change(db_session, session_id, _artifact_id, _asset_id):
            outcome = db_session.get(OutcomePlan, session_id)
            if outcome is None:
                db_session.add(
                    OutcomePlan(
                        session_id=session_id,
                        value_json={"changed": True},
                    )
                )
            else:
                outcome.value_json = {"changed": True}
                outcome.revision += 1

        def selection_change(
            db_session,
            session_id,
            artifact_id,
            _asset_id,
        ):
            selection = db_session.get(
                SessionStageSelection,
                (session_id, "prepare_text"),
            )
            if selection is None:
                db_session.add(
                    SessionStageSelection(
                        session_id=session_id,
                        stage_key="prepare_text",
                        artifact_id=artifact_id,
                    )
                )
            else:
                selection.revision += 1

        def provider_change(
            db_session,
            _session_id,
            _artifact_id,
            _asset_id,
        ):
            db_session.add(
                Provider(
                    kind="llm",
                    provider_key=f"fixture-{self.source_counter}",
                    label=f"Fixture {self.source_counter}",
                )
            )

        def settings_change(
            db_session,
            session_id,
            _artifact_id,
            _asset_id,
        ):
            db_session.add(
                SessionSetting(
                    session_id=session_id,
                    section="tts",
                    value_json={"speed": 1.01},
                )
            )

        for name, mutation in (
            ("session", session_change),
            ("source", source_change),
            ("outcome", outcome_change),
            ("selection", selection_change),
            ("provider", provider_change),
            ("settings", settings_change),
        ):
            with self.subTest(change=name):
                session_id, artifact_id, asset_id = self._ready_session()
                plan = self._plan(session_id)
                with self.extension[
                    "database"
                ].immediate_session() as db_session:
                    mutation(
                        db_session,
                        session_id,
                        artifact_id,
                        asset_id,
                    )
                response = self._execute(
                    plan,
                    key=f"stale-{name}",
                )
                self.assertEqual(409, response.status_code)
                self.assertEqual(
                    "plan_stale",
                    response.get_json()["error"]["code"],
                )

    def test_digest_confirmation_expiry_and_inline_url_credentials(self):
        session_id, _artifact_id, _asset_id = self._ready_session()
        rejected = self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json={
                "target_stage": "generate_audio",
                "overrides": {
                    "tts": {
                        "service": "OpenAI",
                        "base_url": (
                            "https://api.openai.com/v1?token=secret"
                        ),
                    }
                },
            },
            headers=self.headers,
        )
        self.assertEqual(422, rejected.status_code)
        self.assertEqual(
            "validation_error",
            rejected.get_json()["error"]["code"],
        )

        external = self._plan(
            session_id,
            overrides={
                "tts": {
                    "service": "OpenAI",
                    "base_url": "https://api.openai.com/v1?region=us",
                }
            },
        )
        self.assertIn(
            "external_provider",
            external["required_confirmations"],
        )
        self.assertEqual(
            "https://api.openai.com/v1",
            next(
                item["base_url"]
                for item in external["selected_providers"]
                if item["section"] == "tts"
            ),
        )
        confirmation = self._execute(
            external,
            key="missing-confirmation",
            confirmations=[],
        )
        self.assertEqual(409, confirmation.status_code)
        self.assertEqual(
            "confirmation_required",
            confirmation.get_json()["error"]["code"],
        )

        wrong_digest = self._execute(
            external,
            key="wrong-digest",
            digest=("0" * 64),
        )
        self.assertEqual(409, wrong_digest.status_code)
        self.assertEqual(
            "plan_digest_mismatch",
            wrong_digest.get_json()["error"]["code"],
        )

        with self.extension["database"].immediate_session() as db_session:
            record = db_session.get(
                WorkflowExecutionPlan,
                external["plan_id"],
            )
            record.expires_at = utcnow() - timedelta(seconds=1)
        expired = self._execute(external, key="expired-plan")
        self.assertEqual(409, expired.status_code)
        self.assertEqual(
            "plan_expired",
            expired.get_json()["error"]["code"],
        )

    def test_enqueue_and_idempotency_complete_roll_back_together(self):
        session_id, _artifact_id, _asset_id = self._ready_session()
        plan = self._plan(session_id)
        idempotency = self.extension["idempotency"]
        original_complete = idempotency.complete
        before = self._job_count()

        def fail_after_complete(*args, **kwargs):
            original_complete(*args, **kwargs)
            raise RuntimeError("injected plan rollback")

        with (
            patch.object(
                idempotency,
                "complete",
                side_effect=fail_after_complete,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected plan rollback",
            ),
        ):
            self._execute(plan, key="atomic-plan")

        self.assertEqual(before, self._job_count())
        retry = self._execute(plan, key="atomic-plan")
        self.assertEqual(202, retry.status_code, retry.get_json())
        self.assertEqual(before + 1, self._job_count())


if __name__ == "__main__":
    unittest.main()
