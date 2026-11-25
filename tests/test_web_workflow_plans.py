import hashlib
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import Worker
from pandrator.web.models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    OutcomePlan,
    OutputAssembly,
    Provider,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SessionStageSelection,
    SourceAsset,
    WorkflowExecutionPlan,
    WorkspaceSetting,
    utcnow,
)
from pandrator.web.projects.artifact_identity import resolve_artifact_ownership
from pandrator.web.workflow_plans import (
    FINGERPRINT_EXPORT,
    WorkflowExecutionPlanService,
    canonical_digest,
)
from pandrator.web.workflow_prerequisites import WorkflowPrerequisiteService
from pandrator.web.workflows import ResolvedWorkflowStage, WorkflowService
from pandrator.web.workspace import (
    WorkspaceSettingsService,
    output_assembly_settings_hash,
)
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
        included_stages=None,
    ):
        body = {
            "name": f"Plan fixture {self.source_counter}",
            "workflow_kind": workflow_kind,
        }
        if included_stages is not None:
            body["included_stages"] = list(included_stages)
        created = self.client.post(
            "/api/v1/sessions",
            json=body,
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
        continuation=None,
        reuse_stages=None,
        rerun_stages=None,
    ):
        payload = {
            "target_stage": target_stage,
            "overrides": overrides or {},
        }
        if continuation is not None:
            payload["continuation"] = continuation
        if reuse_stages is not None:
            payload["reuse_stages"] = list(reuse_stages)
        if rerun_stages is not None:
            payload["rerun_stages"] = list(rerun_stages)
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json=payload,
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _voiceover_session_with_translation(
        self,
        *,
        dispatch_kind=None,
        fingerprint=None,
        language="en",
    ):
        """Create a voiceover session with a selected translation artifact."""
        session_id, source_id, _asset_id = self._ready_session(
            workflow_kind="voiceover", suffix="srt"
        )
        with self.extension["database"].session() as db_session:
            record = db_session.get(SessionRecord, session_id)
            record.target_language = "en"
            imported = db_session.scalar(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role == "transcription",
                    Artifact.state == "current",
                )
            )
            if imported is not None:
                source_id = imported.id
        translation_path = (
            Path(self.temporary.name) / f"plan-translation-{self.source_counter}.srt"
        )
        self.source_counter += 1
        translation_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nCześć\n",
            encoding="utf-8",
        )
        metadata = {"source_artifact_id": source_id}
        if dispatch_kind is not None:
            metadata.update(
                {
                    "dispatch_run_id": f"dispatch-{dispatch_kind}",
                    "dispatch_kind": dispatch_kind,
                    "language": language,
                }
            )
        if fingerprint is not None:
            metadata["settings_fingerprint"] = fingerprint
        translation = ArtifactService(
            self.extension["database"],
            self.extension["paths"],
        ).register(
            translation_path,
            kind="srt",
            role="translation",
            session_id=session_id,
            parent_ids=[source_id],
            metadata=metadata,
        )
        with self.extension["database"].session() as db_session:
            outcome = db_session.get(OutcomePlan, session_id)
            value = dict(outcome.value_json or {}) if outcome is not None else {}
            value["inputs"] = {"translation": "source", "generation": "translation"}
            value["transformations"] = {
                **dict(value.get("transformations") or {}),
                "translation": True,
                "generate_audio": True,
            }
            if outcome is None:
                db_session.add(OutcomePlan(session_id=session_id, value_json=value))
            else:
                outcome.value_json = value
                outcome.revision += 1
            selection = db_session.get(
                SessionStageSelection, (session_id, "translate")
            )
            if selection is None:
                db_session.add(
                    SessionStageSelection(
                        session_id=session_id,
                        stage_key="translate",
                        artifact_id=translation.id,
                    )
                )
            else:
                selection.artifact_id = translation.id
        return session_id, translation.id

    def _selected_transcription(self, session_id, source_id):
        """Register a finished transcription and select it for its stage."""

        path = (
            Path(self.temporary.name)
            / f"plan-transcription-{self.source_counter}.srt"
        )
        self.source_counter += 1
        path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            encoding="utf-8",
        )
        transcription = ArtifactService(
            self.extension["database"],
            self.extension["paths"],
        ).register(
            path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[source_id],
        )
        with self.extension["database"].session() as db_session:
            selection = db_session.get(
                SessionStageSelection, (session_id, "transcribe")
            )
            if selection is None:
                db_session.add(
                    SessionStageSelection(
                        session_id=session_id,
                        stage_key="transcribe",
                        artifact_id=transcription.id,
                    )
                )
            else:
                selection.artifact_id = transcription.id
        return transcription.id

    def _prepared_generation_plan(self, session_id):
        """Record an active generation plan so take reuse can be read."""

        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash=f"plan-fixture-{session_id}",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            return revision.id

    def _plan_error(self, session_id, **payload):
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json={"target_stage": "generate_audio", "overrides": {}, **payload},
            headers=self.headers,
        )
        self.assertEqual(422, response.status_code, response.get_json())
        return response.get_json()["error"]

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

    def _assert_direct_run_snapshot(self, stage_key, last_section):
        session_id, original_source, _ = self._ready_session()
        _, replacement_source, replacement_asset = self._ready_session()
        database = self.extension["database"]
        original_get = WorkspaceSettingsService._get_in_workspace
        changed = False

        def replace_after_settings(
            service, db_session, target, section, workspace_id, *,
            _read_context=None, _service_config_cache=None,
        ):
            nonlocal changed
            result = original_get(
                service,
                db_session,
                target,
                section,
                workspace_id,
                _read_context=_read_context,
                _service_config_cache=_service_config_cache,
            )
            if target == session_id and section == last_section and not changed:
                changed = True
                # Commit on a separate connection between settings resolution
                # and input selection. Both changes become visible together.
                with database.session() as writer:
                    writer.add(SessionSetting(
                        session_id=session_id, section="text",
                        value_json={"max_sentence_length": 999},
                    ))
                    attachment = writer.scalar(select(SessionSource).where(
                        SessionSource.session_id == session_id,
                    ))
                    attachment.source_asset_id = replacement_asset
                    attachment.revision += 1
            return result

        with patch.object(WorkspaceSettingsService, "_get_in_workspace", replace_after_settings):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/stages/{stage_key}/run",
                json={}, headers=self.headers,
            )
        self.assertEqual(202, response.status_code, response.get_json())
        payload = response.get_json()["payload_json"]
        self.assertEqual(original_source, payload["source_artifact_id"])
        self.assertEqual(200, payload["settings"]["max_sentence_length"])
        # The concurrent edit committed; a subsequent run must see all of it.
        following = self.client.post(
            f"/api/v1/sessions/{session_id}/stages/{stage_key}/run",
            json={}, headers=self.headers,
        )
        self.assertEqual(202, following.status_code, following.get_json())
        self.assertEqual(replacement_source, following.get_json()["payload_json"]["source_artifact_id"])
        self.assertEqual(999, following.get_json()["payload_json"]["settings"]["max_sentence_length"])

    def test_export_endpoint_queues_variant_before_first_assembly(self):
        session_id, _artifact_id, _asset_id = self._ready_session()
        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="http-export-first-assembly",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            db_session.add(run)
            db_session.flush()
            run_id = run.id

        response = self.client.post(
            f"/api/v1/sessions/{session_id}/stages/export/run",
            json={"output": {"generation_run_id": run_id}},
            headers=self.headers,
        )

        self.assertEqual(202, response.status_code, response.get_json())
        queued = response.get_json()
        self.assertEqual("export.variant", queued["kind"])
        self.assertEqual(
            run_id,
            queued["payload_json"]["settings"]["generation_run_id"],
        )
        self.assertIsNone(queued["payload_json"]["source_artifact_id"])

    def test_direct_run_captures_settings_and_source_from_one_snapshot(self):
        self._assert_direct_run_snapshot("clean_source", "text")

    def test_direct_continuation_captures_settings_and_source_from_one_snapshot(self):
        self._assert_direct_run_snapshot("generate_audio", "tts")

    def test_settings_resolution_captures_sections_and_connections_together(self):
        session_id, _, _ = self._ready_session()
        database = self.extension["database"]
        original_get = WorkspaceSettingsService._get_in_workspace
        with database.session() as writer:
            workspace_id = initial_personal_workspace(writer).id
            writer.merge(WorkspaceSetting(
                workspace_id=workspace_id,
                key="services.tts",
                value_json={"service": "kokoro"},
            ))

        def replace_after_text(
            service, db_session, target, section, workspace_id, *,
            _read_context=None, _service_config_cache=None,
        ):
            result = original_get(
                service, db_session, target, section, workspace_id,
                _read_context=_read_context,
                _service_config_cache=_service_config_cache,
            )
            if target == session_id and section == "text":
                with database.session() as writer:
                    workspace_id = initial_personal_workspace(writer).id
                    writer.add_all([
                        SessionSetting(session_id=session_id, section="text", value_json={"max_sentence_length": 999}),
                        SessionSetting(session_id=session_id, section="audio", value_json={"sentence_silence_ms": 999}),
                    ])
                    connection = writer.get(WorkspaceSetting, (workspace_id, "services.tts"))
                    connection.value_json = {"service": "silero"}
                    connection.revision += 1
            return result

        service = WorkspaceSettingsService(database)
        with patch.object(WorkspaceSettingsService, "_get_in_workspace", replace_after_text):
            resolved, digest = service.resolve(session_id, ["text", "audio", "tts"])
        self.assertEqual(200, resolved["text"]["max_sentence_length"])
        self.assertEqual(250, resolved["audio"]["sentence_silence_ms"])
        self.assertEqual("kokoro", resolved["tts"]["service"])
        following, following_digest = service.resolve(session_id, ["text", "audio", "tts"])
        self.assertEqual(999, following["text"]["max_sentence_length"])
        self.assertEqual(999, following["audio"]["sentence_silence_ms"])
        self.assertEqual("silero", following["tts"]["service"])
        self.assertNotEqual(digest, following_digest)

    def test_direct_run_keeps_captured_settings_when_queue_handoff_changes_them(self):
        session_id, source_id, _ = self._ready_session()
        original_resolve = WorkflowService.resolve_stage

        def update_before_enqueue(service, *args, **kwargs):
            resolved = original_resolve(service, *args, **kwargs)
            # Resolution's snapshot is closed; inject the same concurrent edit
            # before the user-scoped enqueue acquires its immediate writer.
            with service.database.session() as writer:
                writer.add(SessionSetting(
                    session_id=session_id, section="text",
                    value_json={"max_sentence_length": 999},
                ))
            return resolved

        with patch.object(WorkflowService, "resolve_stage", update_before_enqueue):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/stages/generate_audio/run",
                json={"text": {"max_sentence_length": 321}}, headers=self.headers,
            )
        self.assertEqual(202, response.status_code, response.get_json())
        payload = response.get_json()["payload_json"]
        self.assertEqual(source_id, payload["source_artifact_id"])
        self.assertEqual(321, payload["settings"]["max_sentence_length"])
        self.assertEqual(321, payload["resolved_settings_snapshot"]["text"]["max_sentence_length"])
        self.assertEqual(321, payload["stage_settings"]["prepare_text"]["max_sentence_length"])
        with self.extension["database"].session() as reader:
            self.assertEqual(999, reader.get(SessionSetting, (session_id, "text")).value_json["max_sentence_length"])
            self.assertEqual(payload, reader.get(Job, response.get_json()["id"]).payload_json)

    def test_plan_still_rejects_edits_committed_during_snapshot_resolution(self):
        session_id, _, _ = self._ready_session()
        database = self.extension["database"]
        original_get = WorkspaceSettingsService._get_in_workspace
        changed = False

        def update_once(
            service, db_session, target, section, workspace_id, *,
            _read_context=None, _service_config_cache=None,
        ):
            nonlocal changed
            result = original_get(
                service,
                db_session,
                target,
                section,
                workspace_id,
                _read_context=_read_context,
                _service_config_cache=_service_config_cache,
            )
            if target == session_id and not changed:
                changed = True
                with database.session() as writer:
                    writer.add(SessionSetting(
                        session_id=session_id, section="text",
                        value_json={"max_sentence_length": 999},
                    ))
            return result

        with patch.object(WorkspaceSettingsService, "_get_in_workspace", update_once):
            response = self.client.post(
                f"/api/v1/sessions/{session_id}/workflow-plans",
                json={"target_stage": "generate_audio"}, headers=self.headers,
            )
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("planning_conflict", response.get_json()["error"]["code"])
        self.assertEqual(0, self._job_count())
        with database.session() as reader:
            self.assertEqual(0, reader.scalar(select(func.count()).select_from(WorkflowExecutionPlan)))

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

    def test_dispatch_translation_is_reused_with_its_evidence(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            dispatch_kind="translation",
            fingerprint={
                "executor": "dispatch",
                "dispatch_kind": "translation",
                "target_language": "en",
            },
        )

        plan = self._plan(session_id)

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("reuse_if_unchanged", by_stage["translate"]["decision"])
        self.assertEqual("dispatch_result", by_stage["translate"]["reuse_basis"])
        self.assertEqual([], plan["warnings"])
        with self.extension["database"].session() as db_session:
            stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            execution_payload = stored.plan_json["_execution"]["payload"]
            self.assertEqual(["translate"], execution_payload["reuse_stages"])
            self.assertNotIn("rerun_stages", execution_payload)

    def test_dispatch_correction_is_reused_with_its_evidence(self):
        session_id, translation_id = self._voiceover_session_with_translation(
            dispatch_kind="translation",
            fingerprint={
                "executor": "dispatch",
                "dispatch_kind": "translation",
                "target_language": "en",
            },
        )
        with self.extension["database"].session() as db_session:
            translation = db_session.get(Artifact, translation_id)
            assert translation is not None
            source_id = str(translation.metadata_json["source_artifact_id"])
        correction_path = (
            Path(self.temporary.name) / f"plan-correction-{self.source_counter}.srt"
        )
        self.source_counter += 1
        correction_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello!\n",
            encoding="utf-8",
        )
        correction = ArtifactService(
            self.extension["database"],
            self.extension["paths"],
        ).register(
            correction_path,
            kind="srt",
            role="correction",
            session_id=session_id,
            parent_ids=[source_id],
            metadata={
                "source_artifact_id": source_id,
                "dispatch_run_id": "dispatch-correction",
                "dispatch_kind": "correction",
                "language": "en",
                "settings_fingerprint": {
                    "executor": "dispatch",
                    "dispatch_kind": "correction",
                },
            },
        )
        with self.extension["database"].session() as db_session:
            outcome = db_session.get(OutcomePlan, session_id)
            assert outcome is not None
            value = dict(outcome.value_json)
            value["inputs"] = {
                "translation": "correction",
                "generation": "translation",
            }
            value["transformations"] = {
                **dict(value.get("transformations") or {}),
                "correction": True,
            }
            outcome.value_json = value
            selection = db_session.get(
                SessionStageSelection, (session_id, "correct")
            )
            if selection is None:
                db_session.add(
                    SessionStageSelection(
                        session_id=session_id,
                        stage_key="correct",
                        artifact_id=correction.id,
                    )
                )
            else:
                selection.artifact_id = correction.id
            translation = db_session.get(Artifact, translation_id)
            assert translation is not None
            translation.metadata_json = {
                **translation.metadata_json,
                "source_artifact_id": correction.id,
            }

        plan = self._plan(session_id)

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("reuse_if_unchanged", by_stage["correct"]["decision"])
        self.assertEqual("dispatch_result", by_stage["correct"]["reuse_basis"])

    def test_explicit_reuse_argument_names_the_reuse(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            fingerprint={
                "backend": "llm",
                "target_language": "de",
                "model": "fixture-model",
                "instructions": "",
            },
        )

        plan = self._plan(session_id, reuse_stages=["translate"])

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("reuse_explicit", by_stage["translate"]["decision"])
        self.assertEqual("explicit", by_stage["translate"]["reuse_basis"])

    def test_rerun_argument_runs_a_current_translation(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            dispatch_kind="translation",
            fingerprint={
                "executor": "dispatch",
                "dispatch_kind": "translation",
                "target_language": "en",
            },
        )

        plan = self._plan(session_id, rerun_stages=["translate"])

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("rerun_explicit", by_stage["translate"]["decision"])
        self.assertNotIn("reuse_basis", by_stage["translate"])
        with self.extension["database"].session() as db_session:
            stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            execution_payload = stored.plan_json["_execution"]["payload"]
            self.assertEqual(["translate"], execution_payload["rerun_stages"])
            self.assertNotIn("reuse_stages", execution_payload)

    def test_rerun_argument_beats_a_legacy_reuse_override(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            dispatch_kind="translation",
            fingerprint={
                "executor": "dispatch",
                "dispatch_kind": "translation",
                "target_language": "en",
            },
        )

        plan = self._plan(
            session_id,
            overrides={"reuse_stages": ["translate"]},
            rerun_stages=["translate"],
        )

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("rerun_explicit", by_stage["translate"]["decision"])
        with self.extension["database"].session() as db_session:
            stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            execution_payload = stored.plan_json["_execution"]["payload"]
            self.assertEqual(["translate"], execution_payload["rerun_stages"])
            self.assertNotIn("reuse_stages", execution_payload)

    def test_stage_argument_validation_errors(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            dispatch_kind="translation",
            fingerprint={
                "executor": "dispatch",
                "dispatch_kind": "translation",
                "target_language": "en",
            },
        )

        conflict = self._plan_error(
            session_id,
            reuse_stages=["translate"],
            rerun_stages=["translate"],
        )
        self.assertEqual("validation_error", conflict["code"])
        self.assertIn("cannot be both reused and re-run", conflict["message"])

        target = self._plan_error(session_id, reuse_stages=["generate_audio"])
        self.assertEqual("validation_error", target["code"])
        self.assertIn("always runs", target["message"])

        unknown = self._plan_error(session_id, rerun_stages=["not_a_stage"])
        self.assertEqual("validation_error", unknown["code"])
        self.assertIn("not_a_stage", unknown["message"])
        self.assertIn("translate", unknown["message"])

        direct = self._plan_error(
            session_id,
            continuation=False,
            reuse_stages=["translate"],
        )
        self.assertEqual("validation_error", direct["code"])
        self.assertIn("only to continuation plans", direct["message"])

        unavailable = self._plan_error(
            session_id,
            target_stage="translate",
            rerun_stages=["generate_audio"],
        )
        self.assertEqual("validation_error", unavailable["code"])
        self.assertIn("cannot run in this plan", unavailable["message"])

    def test_reuse_request_without_an_artifact_is_refused(self):
        """A reuse request that cannot be honoured must not vanish from the plan."""

        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
        )

        transcription = self._plan_error(
            session_id,
            target_stage="generate_audio",
            reuse_stages=["transcribe"],
        )
        self.assertEqual("reuse_unavailable", transcription["code"])
        self.assertEqual(
            "Nothing from transcribe can be reused here; remove it from "
            "reuse_stages or let it run.",
            transcription["message"],
        )

        generation = self._plan_error(
            session_id,
            target_stage="export",
            reuse_stages=["generate_audio"],
        )
        self.assertEqual("reuse_unavailable", generation["code"])
        self.assertIn("Nothing from generate_audio can be reused here", generation["message"])

    def test_freshness_failure_is_reported_in_the_plan(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            dispatch_kind="translation",
            fingerprint={
                "executor": "dispatch",
                "dispatch_kind": "translation",
                "target_language": "en",
            },
        )

        with patch.object(
            WorkflowPrerequisiteService,
            "prerequisite_freshness",
            side_effect=RuntimeError("freshness exploded"),
        ):
            plan = self._plan(session_id)

        self.assertIn(
            "Prerequisite freshness could not be checked; existing prerequisite "
            "results are reused without verification.",
            plan["warnings"],
        )
        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("reuse_if_unchanged", by_stage["translate"]["decision"])
        self.assertEqual("no_prior_check", by_stage["translate"]["reuse_basis"])

    def test_unverifiable_current_settings_are_reported(self):
        session_id, _translation_id = self._voiceover_session_with_translation(
            fingerprint={
                "backend": "llm",
                "target_language": "en",
                "model": "fixture-model",
                "instructions": "",
            },
        )

        with patch.object(
            WorkflowPrerequisiteService,
            "_with_database_llm_settings",
            side_effect=ValueError("provider unavailable"),
        ):
            plan = self._plan(session_id)

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("reuse_if_unchanged", by_stage["translate"]["decision"])
        self.assertEqual(
            "current_settings_unavailable", by_stage["translate"]["reuse_basis"]
        )
        self.assertIn(
            "Prerequisite stage 'translate' is reused, but its settings could not be "
            "compared with the current configuration. Include 'translate' in "
            "rerun_stages to run it again.",
            plan["warnings"],
        )

    def test_export_plan_reports_effective_audio_mode_and_its_origin(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        plan = self._plan(session_id, target_stage="export", continuation=False)
        effective = plan["settings"]["effective"]
        self.assertEqual(
            {"effective": "mixed", "origin": "session_context"},
            effective["audio_mode"],
        )
        self.assertEqual("builtin", effective["video_tail_extension_policy"]["origin"])
        self.assertEqual("builtin", effective["synchronization_speed"]["origin"])
        self.assertEqual(1.15, effective["synchronization_speed"]["effective"])
        self.assertFalse(
            [item for item in plan["warnings"] if "soundtrack" in item]
        )

        with self.extension["database"].session() as db_session:
            db_session.add(
                SessionSetting(
                    session_id=session_id,
                    section="output",
                    value_json={"audio_mode": "dubbing_only"},
                )
            )
        stored = self._plan(session_id, target_stage="export", continuation=False)
        self.assertEqual(
            {"effective": "dubbing_only", "origin": "session_override"},
            stored["settings"]["effective"]["audio_mode"],
        )
        self.assertEqual(
            [
                "This export replaces the original soundtrack with the voiceover only "
                "(audio_mode=dubbing_only, from session_override). Set audio_mode=mixed "
                "to keep the original under the voice."
            ],
            [item for item in stored["warnings"] if "soundtrack" in item],
        )

        requested = self._plan(
            session_id,
            target_stage="export",
            overrides={"output": {"audio_mode": "mixed"}},
            continuation=False,
        )
        self.assertEqual(
            {"effective": "mixed", "origin": "run_override"},
            requested["settings"]["effective"]["audio_mode"],
        )
        self.assertFalse(
            [item for item in requested["warnings"] if "soundtrack" in item]
        )

    def test_export_plan_warns_about_rushed_synchronization_speed(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        for speed in (1.15, 1.2, 120):
            with self.subTest(speed=speed):
                plan = self._plan(
                    session_id,
                    target_stage="export",
                    overrides={"audio": {"synchronization_speed": speed}},
                    continuation=False,
                )
                warnings = [
                    item for item in plan["warnings"] if "rushed" in item
                ]
                self.assertEqual([], warnings)
        for speed, expected in ((1.21, "1.21"), (130, "130")):
            with self.subTest(speed=speed):
                plan = self._plan(
                    session_id,
                    target_stage="export",
                    overrides={"audio": {"synchronization_speed": speed}},
                    continuation=False,
                )
                self.assertEqual(
                    {
                        "effective": speed,
                        "origin": "run_override",
                    },
                    plan["settings"]["effective"]["synchronization_speed"],
                )
                self.assertEqual(
                    [
                        f"Synchronization speed {expected} may sound rushed; "
                        "above 1.2× is not recommended."
                    ],
                    [item for item in plan["warnings"] if "rushed" in item],
                )

    def _set_global_setting(self, key, value):
        with self.extension["database"].session() as db_session:
            workspace_id = initial_personal_workspace(db_session).id
            record = db_session.get(WorkspaceSetting, (workspace_id, key))
            if record is None:
                db_session.add(
                    WorkspaceSetting(
                        workspace_id=workspace_id,
                        key=key,
                        value_json=value,
                        revision=1,
                    )
                )
            else:
                record.value_json = value
                record.revision += 1

    def test_transcribe_plan_names_an_inherited_stt_engine(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
        )
        self._set_global_setting(
            "defaults.stt",
            {"stt_engine": "moss", "stt_model_quantization": "q8_0"},
        )

        plan = self._plan(session_id, target_stage="transcribe")
        effective = plan["settings"]["effective"]

        self.assertEqual(
            {"effective": "moss", "origin": "global"}, effective["stt_engine"]
        )
        self.assertEqual("global", effective["stt_model_quantization"]["origin"])
        self.assertIn("stt_language", effective)
        self.assertTrue(
            [
                item
                for item in plan["warnings"]
                if item.startswith("Speech recognition will use moss, inherited from "
                                   "the global settings")
            ],
            plan["warnings"],
        )

    def test_transcribe_plan_keeps_quiet_when_the_session_chooses_an_engine(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
        )
        self._set_global_setting("defaults.stt", {"stt_engine": "moss"})
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/stt", headers=self.headers
        ).get_json()
        saved = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/stt",
            json={"value": {**current["effective"], "stt_engine": "parakeet"}},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())

        plan = self._plan(session_id, target_stage="transcribe")

        self.assertEqual(
            {"effective": "parakeet", "origin": "session_override"},
            plan["settings"]["effective"]["stt_engine"],
        )
        self.assertFalse(
            [item for item in plan["warnings"] if "Speech recognition" in item]
        )

    def test_transcribe_readiness_also_covers_an_explicit_rerun(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
        )
        missing = {
            "schema_version": "1",
            "scope": "local_model_files",
            "status": "missing",
            "ready": False,
            "engine": "crispasr",
            "runtime_check_required": False,
            "assets": [
                {
                    "kind": "vad",
                    "id": "silero",
                    "status": "missing",
                    "download": "administrator_required",
                }
            ],
        }

        with patch(
            "pandrator.logic.dubbing.transcription_readiness."
            "diagnose_transcription_readiness",
            return_value=missing,
        ):
            plan = self._plan(
                session_id,
                target_stage="export",
                rerun_stages=["transcribe"],
            )

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("rerun_explicit", by_stage["transcribe"]["decision"])
        self.assertEqual("missing", plan["transcription_readiness"]["status"])
        self.assertIn(
            "The Silero VAD model is not cached, and only an Administrator can "
            "download it. Ask one to prepare it, or transcription will fail.",
            plan["warnings"],
        )

    def test_reused_transcription_is_not_disclosed_as_current_settings(self):
        session_id, source_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
        )
        self._selected_transcription(session_id, source_id)
        self._set_global_setting("defaults.stt", {"stt_engine": "moss"})

        plan = self._plan(
            session_id,
            target_stage="generate_audio",
            reuse_stages=["transcribe"],
        )

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("reuse_explicit", by_stage["transcribe"]["decision"])
        effective = plan["settings"]["effective"]
        self.assertNotIn("stt_engine", effective)
        self.assertIn("model", effective)
        self.assertFalse(
            [item for item in plan["warnings"] if "Speech recognition" in item],
            plan["warnings"],
        )

    def test_generate_audio_plan_reports_no_history_without_a_measured_pace(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover", suffix="srt"
        )
        plan = self._plan(session_id, target_stage="generate_audio")
        step = plan["ordered_steps"][-1]
        self.assertEqual("generate_audio", step["stage"])
        # No prepared speech plan and no completed run: nothing to measure.
        self.assertIsNone(step["estimated_seconds"])
        self.assertEqual("no_history", step["estimate_basis"])

    def test_export_plan_withholds_the_estimate_from_a_reused_generation(self):
        """A continuation that reuses generation predicts no synthesis time."""

        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
            included_stages=["generate_audio", "export"],
        )
        snapshot = self.extension["services"].workflows.snapshot(session_id)
        stages = [
            {
                **stage,
                "status": (
                    "completed"
                    if stage["key"] == "generate_audio"
                    else stage["status"]
                ),
                "selected_artifact_id": (
                    "ready-generation"
                    if stage["key"] == "generate_audio"
                    else stage.get("selected_artifact_id")
                ),
            }
            for stage in snapshot["stages"]
        ]

        with patch.object(
            self.extension["services"].workflows,
            "snapshot",
            return_value={"stages": stages},
        ):
            plan = self._plan(session_id, target_stage="export")

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual(
            "reuse_if_unchanged", by_stage["generate_audio"]["decision"]
        )
        self.assertNotIn("estimated_seconds", by_stage["generate_audio"])
        self.assertNotIn("estimate_basis", by_stage["generate_audio"])
        self.assertIn(
            "generate_audio",
            [item["stage"] for item in plan["reuse_decisions"]],
        )

    def test_generate_audio_plan_warns_when_every_take_is_reusable(self):
        """A plan prepared again after a timing-only edit keeps every take."""

        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover", suffix="srt"
        )
        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash=f"prepared-again-{session_id}",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            revision_id = revision.id
        calls = []

        def tally(reusable):
            def history(_database, _session_id, *, revision_ids, limit):
                calls.append(revision_ids)
                return {
                    "items": [
                        {"active_segment_count": 21, "reusable_segment_count": reusable}
                    ]
                }

            return history

        def warnings_for(reusable, overrides=None):
            with patch(
                "pandrator.web.generation_review.revision_history", tally(reusable)
            ):
                plan = self._plan(
                    session_id, target_stage="generate_audio", overrides=overrides
                )
            self.assertEqual("run", plan["ordered_steps"][-1]["decision"])
            return [item for item in plan["warnings"] if "reusable take" in item]

        self.assertEqual(
            [
                "Every block of the current speech plan (21) already has a reusable "
                "take, and generating synthesises them all again. To keep them, "
                "assemble the current takes without naming a generation run, then "
                "export without one."
            ],
            warnings_for(21),
        )
        self.assertEqual([], warnings_for(20))
        self.assertEqual([[revision_id], [revision_id]], calls)
        # Reuse is counted against saved settings; a speech override is not.
        self.assertEqual([], warnings_for(21, {"tts": {"voice": "Other voice"}}))
        self.assertEqual([[revision_id], [revision_id]], calls)

    def test_generate_audio_plan_warns_when_an_explicit_rerun_repeats_every_take(self):
        """Rerunning generation explicitly still synthesises every block."""

        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
            included_stages=["generate_audio", "export"],
        )
        self._prepared_generation_plan(session_id)

        with patch(
            "pandrator.web.generation_review.revision_history",
            return_value={
                "items": [{"active_segment_count": 12, "reusable_segment_count": 12}]
            },
        ):
            plan = self._plan(
                session_id,
                target_stage="export",
                rerun_stages=["generate_audio"],
            )

        by_stage = {step["stage"]: step for step in plan["ordered_steps"]}
        self.assertEqual("rerun_explicit", by_stage["generate_audio"]["decision"])
        self.assertTrue(
            [
                warning
                for warning in plan["warnings"]
                if "(12) already has a reusable take" in warning
            ],
            plan["warnings"],
        )

    def test_generate_audio_plan_warns_about_an_inherited_unverified_route(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        self._set_global_setting(
            "defaults.tts",
            {
                "service": "vertex_ai",
                "model": "gemini-2.5-flash-preview-tts",
                "voice": "Algenib",
                "language": "ja",
            },
        )

        plan = self._plan(session_id, target_stage="generate_audio")
        effective = plan["settings"]["effective"]

        self.assertEqual("global", effective["service"]["origin"])
        self.assertEqual(
            {"effective": "unverified", "origin": "derived", "language": "ja"},
            effective["language_support"],
        )
        self.assertTrue(
            [
                item
                for item in plan["warnings"]
                if "was inherited from the global settings and is not verified for ja"
                in item
            ],
            plan["warnings"],
        )

    def test_generate_audio_plan_keeps_quiet_when_the_session_chooses_the_route(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        self._set_global_setting(
            "defaults.tts",
            {"service": "vertex_ai", "model": "gemini-2.5-flash-preview-tts"},
        )
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/tts", headers=self.headers
        ).get_json()
        saved = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={
                "value": {
                    **current["effective"],
                    "service": "vertex_ai",
                    "model": "gemini-2.5-flash-preview-tts",
                    "voice": "Algenib",
                    "language": "ja",
                }
            },
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())

        plan = self._plan(session_id, target_stage="generate_audio")
        effective = plan["settings"]["effective"]

        self.assertEqual("session_override", effective["service"]["origin"])
        self.assertEqual("unverified", effective["language_support"]["effective"])
        self.assertFalse(
            [item for item in plan["warnings"] if "speech route" in item]
        )

    def test_export_plan_does_not_warn_about_dubbing_without_source_audio(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="txt",
        )
        with self.extension["database"].session() as db_session:
            db_session.add(
                SessionSetting(
                    session_id=session_id,
                    section="output",
                    value_json={"audio_mode": "mixed"},
                )
            )

        plan = self._plan(session_id, target_stage="export", continuation=False)

        # The voiceover clamp replaces a stored mixed mode with no source audio.
        self.assertEqual(
            {"effective": "dubbing_only", "origin": "clamped"},
            plan["settings"]["effective"]["audio_mode"],
        )
        self.assertFalse(
            [item for item in plan["warnings"] if "soundtrack" in item]
        )

    def test_export_plan_reads_the_resolved_snapshot_of_the_planned_run(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        service = self.extension["services"].workflow_plans
        stored = service._export_effective_settings(
            session_id,
            {},
            ResolvedWorkflowStage(
                job_kind="export.create",
                payload={"resolved_settings_snapshot": {"output": {}}},
                resource_keys=(),
                session_revision=1,
                workflow_kind="voiceover",
                source_artifact_id=None,
                source_content_hash=None,
                outcome_revision=1,
            ),
        )[0]
        # No resolved value: the stored session snapshot answers.
        self.assertEqual(
            {"effective": "mixed", "origin": "session_context"},
            stored["audio_mode"],
        )

        resolved = service._export_effective_settings(
            session_id,
            {},
            ResolvedWorkflowStage(
                job_kind="export.create",
                payload={
                    "resolved_settings_snapshot": {"output": {"audio_mode": "preserve"}}
                },
                resource_keys=(),
                session_revision=1,
                workflow_kind="voiceover",
                source_artifact_id=None,
                source_content_hash=None,
                outcome_revision=1,
            ),
        )[0]
        # The plan's own resolution wins over a second read of the session.
        self.assertEqual(
            {"effective": "preserve", "origin": "clamped"},
            resolved["audio_mode"],
        )

    def test_export_plan_refuses_mixed_audio_without_a_source_track(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="txt",
        )

        response = self.client.post(
            f"/api/v1/sessions/{session_id}/workflow-plans",
            json={
                "target_stage": "export",
                "overrides": {"output": {"audio_mode": "mixed"}},
                "continuation": False,
            },
            headers=self.headers,
        )

        self.assertEqual(422, response.status_code, response.get_json())
        self.assertEqual("validation_error", response.get_json()["error"]["code"])
        self.assertIn(
            "Mixing source audio requires an attached audio or video source",
            response.get_json()["error"]["message"],
        )

    def _assembly_for_plan(
        self,
        session_id,
        output_override,
        *,
        duration_ms=2_000,
        blocks=None,
        timing_drift=None,
    ):
        """Create a completed run plus the assembly an export plan will reuse."""

        from pandrator.web.workspace import expected_output_assembly_snapshot

        database = self.extension["database"]
        with database.session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash=f"timing-assembly-plan-{session_id}",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            db_session.add(run)
            db_session.flush()
            run_id = run.id
        resolved = self.extension["services"].workflows.resolve_stage(
            session_id,
            "export",
            {
                "output": {**output_override, "generation_run_id": run_id},
            },
            continuation=False,
        )
        snapshot = resolved.payload["resolved_settings_snapshot"]
        synchronization = {"mode": "subtitle_timed"}
        if timing_drift is not None:
            synchronization["timing_drift"] = timing_drift
        if blocks is not None:
            synchronization["blocks"] = blocks
        with database.session() as db_session:
            run = db_session.get(GenerationRun, run_id)
            ownership = resolve_artifact_ownership(db_session, session_id=session_id)
            artifact = Artifact(
                session_id=session_id,
                workspace_id=ownership.workspace_id,
                project_id=ownership.project_id,
                kind="audio",
                role="assembled_audio",
                relative_path=f"timing-assembly-{run_id}.wav",
                content_hash=hashlib.sha256(
                    f"timing-assembly-{run_id}".encode()
                ).hexdigest(),
                state="current",
            )
            db_session.add(artifact)
            db_session.flush()
            db_session.add(
                OutputAssembly(
                    session_id=session_id,
                    generation_run_id=run_id,
                    status="completed",
                    artifact_id=artifact.id,
                    settings_json={
                        "resolved": expected_output_assembly_snapshot(
                            db_session, run, snapshot
                        ),
                        "plan_revision_id": run.plan_revision_id,
                        "duration_ms": duration_ms,
                        "synchronization": synchronization,
                    },
                    settings_hash=output_assembly_settings_hash(snapshot),
                )
            )
        return run_id

    def test_export_plan_reports_speech_timing_from_the_reused_assembly(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        stored_drift = {
            "worst_block_lag_ms": 14_500,
            "worst_block": "0417",
            "blocks_late_over_1s": 151,
            "blocks_late_over_3s": 64,
            "blocks_late_over_10s": 8,
            "final_drift_ms": 4_200,
            "blocks": 522,
            "late_cues": 1,
            "worst_cue_lag_ms": 5_700,
            "cues": 10,
        }
        run_id = self._assembly_for_plan(
            session_id,
            {"export_mode": "media", "audio_mode": "mixed"},
            duration_ms=2_000,
            timing_drift=stored_drift,
        )
        plan = self._plan(            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "export_mode": "media",
                    "audio_mode": "mixed",
                    "generation_run_id": run_id,
                }
            },
            continuation=False,
        )

        speech_timing = plan["speech_timing"]
        self.assertEqual(2_000, speech_timing["assembly_duration_ms"])
        self.assertEqual(stored_drift, speech_timing["timing_drift"])
        self.assertEqual(
            "reuse_if_unchanged",
            next(
                step
                for step in plan["ordered_steps"]
                if step["stage"] == "assemble_audio"
            )["decision"],
        )
        self.assertEqual(
            [
                "Speech drifts behind the subtitles: the worst block starts 14.5 s "
                "late, and 64 of 522 blocks start more than 3 s late. Raising "
                "synchronization_speed (now 1.15) helps; above 1.2 speech may sound "
                "rushed, so for drift this large, condensing the text is the better fix.",
                "1 of 10 subtitle cues appear after their speech has ended (worst 5.7 s), "
                "inside speech blocks that merge several cues. Timing repair "
                "(repair-timing) splits and regenerates such blocks.",
            ],
            [warning for warning in plan["warnings"] if warning.startswith("Speech drifts") or "subtitle cues appear" in warning],
        )

    def test_export_plan_computes_metrics_for_an_older_assembly(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        run_id = self._assembly_for_plan(
            session_id,
            {"export_mode": "media", "audio_mode": "mixed"},
            duration_ms=2_000,
            blocks=[
                {"block": "0001", "drift_before_ms": 0, "drift_after_ms": 0},
                {"block": "0002", "drift_before_ms": 12_000, "drift_after_ms": 6_000},
            ],
        )
        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "export_mode": "media",
                    "audio_mode": "mixed",
                    "generation_run_id": run_id,
                }
            },
            continuation=False,
        )

        drift = plan["speech_timing"]["timing_drift"]
        self.assertEqual(12_000, drift["worst_block_lag_ms"])
        self.assertEqual("0002", drift["worst_block"])
        self.assertEqual(1, drift["blocks_late_over_10s"])
        self.assertEqual(6_000, drift["final_drift_ms"])
        self.assertEqual(2, drift["blocks"])
        # An assembly from before the late-cue pass cannot report cues at all.
        self.assertIsNone(drift["late_cues"])
        self.assertIsNone(drift["worst_cue_lag_ms"])
        self.assertIsNone(drift["cues"])
        self.assertTrue(
            any(warning.startswith("Speech drifts") for warning in plan["warnings"])
        )

    def test_export_plan_predicts_the_tail_outcome(self):
        cases = [
            ("fits", "media", "ask", 2_000),
            ("extend", "media", "extend", 2_600),
            ("stops", "media", "ask", 2_600),
            ("fails", "audio", "ask", 2_600),
        ]
        for outcome, export_mode, policy, duration_ms in cases:
            with self.subTest(outcome=outcome):
                session_id, _artifact_id, _asset_id = self._ready_session(
                    workflow_kind="voiceover",
                    suffix="mp4",
                )
                run_id = self._assembly_for_plan(
                    session_id,
                    {
                        "export_mode": export_mode,
                        "audio_mode": "mixed",
                        "video_tail_extension_policy": policy,
                    },
                    duration_ms=duration_ms,
                )
                with patch(
                    "pandrator.web.soundtrack_export.probe_soundtrack_media",
                    return_value={
                        "video_duration": 2.0,
                        "duration": 2.0,
                        "fps": 25.0,
                    },
                ):
                    plan = self._plan(
                        session_id,
                        target_stage="export",
                        overrides={
                            "output": {
                                "export_mode": export_mode,
                                "audio_mode": "mixed",
                                "video_tail_extension_policy": policy,
                                "generation_run_id": run_id,
                            }
                        },
                        continuation=False,
                    )

                tail = plan["speech_timing"]["tail"]
                self.assertTrue(tail["checked"])
                self.assertEqual(outcome, tail["outcome"])
                self.assertEqual(2_000, tail["source_duration_ms"])
                self.assertEqual(policy, tail["policy"])
                self.assertEqual(duration_ms - 2_000, tail["overrun_ms"])
                export_step = next(
                    step for step in plan["ordered_steps"] if step["stage"] == "export"
                )
                tail_warnings = [
                    warning
                    for warning in plan["warnings"]
                    if "last frame" in warning or "never cuts speech" in warning
                ]
                if outcome == "fits":
                    self.assertEqual(0, tail["extension_ms"])
                    self.assertEqual([], tail_warnings)
                    self.assertNotIn("tail_extension", export_step)
                elif outcome == "extend":
                    # 0.60 s overrun at 25 fps: ceil(15) + 1 frame = 0.64 s,
                    # and the float difference rounds up to a 17th frame.
                    self.assertEqual(680, tail["extension_ms"])
                    self.assertEqual(
                        [
                            "The video will be 0.68 s longer: its last frame is held "
                            "so the speech can finish. Usually only that tail is encoded."
                        ],
                        tail_warnings,
                    )
                    self.assertEqual(tail, export_step["tail_extension"])
                elif outcome == "stops":
                    self.assertEqual(680, tail["extension_ms"])
                    self.assertEqual(
                        [
                            "Speech runs 0.60 s past the end of the video, so this "
                            "export will stop before rendering. Set "
                            "video_tail_extension_policy=extend to hold the last frame "
                            "(usually only that tail is encoded), or raise "
                            "synchronization_speed."
                        ],
                        tail_warnings,
                    )
                    self.assertNotIn("tail_extension", export_step)
                else:
                    self.assertEqual(
                        [
                            "Speech runs 0.60 s past the end of the recording, and an "
                            "audio export never cuts speech, so this export will fail. "
                            "Turn off audio_match_source_duration to export the full "
                            "speech, or export media with "
                            "video_tail_extension_policy=extend."
                        ],
                        tail_warnings,
                    )

    def test_export_plan_predicts_no_video_tail_for_an_audio_only_source(self):
        """A 2 s recording with 2.6 s of speech exports longer audio, not video."""

        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="wav",
        )
        output = {
            "export_mode": "media",
            "audio_mode": "dubbing_only",
            "video_tail_extension_policy": "ask",
        }
        run_id = self._assembly_for_plan(session_id, output, duration_ms=2_600)
        with patch(
            "pandrator.web.soundtrack_export.probe_soundtrack_media",
            return_value={
                "duration": 2.0,
                "video_duration": None,
                "audio_duration": 2.0,
                "fps": None,
                "has_audio": True,
                "has_video": False,
            },
        ):
            plan = self._plan(
                session_id,
                target_stage="export",
                overrides={"output": {**output, "generation_run_id": run_id}},
                continuation=False,
            )

        tail = plan["speech_timing"]["tail"]
        self.assertTrue(tail["checked"])
        self.assertTrue(tail["audio_only"])
        self.assertEqual(2_000, tail["source_duration_ms"])
        self.assertEqual(600, tail["overrun_ms"])
        self.assertEqual("extend", tail["outcome"])
        self.assertEqual(
            [
                "Speech runs 0.60 s past the end of the source audio; the "
                "exported audio is that much longer."
            ],
            [warning for warning in plan["warnings"] if "past the end" in warning],
        )
        export_step = next(
            step for step in plan["ordered_steps"] if step["stage"] == "export"
        )
        # No frame is held, so the plan promises no frozen tail.
        self.assertNotIn("tail_extension", export_step)

    def test_export_plan_measures_the_tail_against_the_rendered_recording(self):
        """A cut session renders its edited media, which is shorter than the source."""

        from pandrator.web import workflows as workflows_module

        session_id, source_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        edited_path = Path(self.temporary.name) / "edited-render.mp4"
        edited_path.write_bytes(b"edited render fixture")
        edited = ArtifactService(
            self.extension["database"],
            self.extension["paths"],
        ).register(
            edited_path,
            kind="video",
            role="media_edit_media",
            session_id=session_id,
        )
        build_contract = workflows_module.build_export_contract

        def contract_for_the_render(*, workflow_kind, settings, source):
            contract = build_contract(
                workflow_kind=workflow_kind, settings=settings, source=source
            )
            return {**contract, "source_artifact_id": edited.id}

        def probe(path):
            edited_render = Path(path).read_bytes() == b"edited render fixture"
            seconds = 1.5 if edited_render else 2.0
            return {"video_duration": seconds, "duration": seconds, "fps": 25.0}

        output = {
            "export_mode": "media",
            "audio_mode": "mixed",
            "video_tail_extension_policy": "ask",
        }
        with patch.object(
            workflows_module, "build_export_contract", contract_for_the_render
        ):
            run_id = self._assembly_for_plan(session_id, output, duration_ms=1_900)
            with patch(
                "pandrator.web.soundtrack_export.probe_soundtrack_media",
                side_effect=probe,
            ):
                plan = self._plan(
                    session_id,
                    target_stage="export",
                    overrides={"output": {**output, "generation_run_id": run_id}},
                    continuation=False,
                )

        tail = plan["speech_timing"]["tail"]
        self.assertNotEqual(source_id, edited.id)
        # The uncut source (2.0 s) would say the speech fits.
        self.assertEqual(1_500, tail["source_duration_ms"])
        self.assertEqual(400, tail["overrun_ms"])
        self.assertEqual("stops", tail["outcome"])

    def _current_takes_assembly(self, session_id, *, revision_number=2, name="current"):
        """An assembly of the current takes, made without a generation run."""

        database = self.extension["database"]
        with database.session() as db_session:
            generation_plan = db_session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            prepared = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=revision_number,
                settings_json={},
                content_hash=f"timing-only-prepare-{name}-{session_id}",
            )
            db_session.add(prepared)
            db_session.flush()
            generation_plan.active_revision_id = prepared.id
            ownership = resolve_artifact_ownership(db_session, session_id=session_id)
            artifact = Artifact(
                session_id=session_id,
                workspace_id=ownership.workspace_id,
                project_id=ownership.project_id,
                kind="audio",
                role="assembled_audio",
                relative_path=f"{name}-takes-{session_id}.wav",
                content_hash=hashlib.sha256(f"{name}{session_id}".encode()).hexdigest(),
                state="current",
            )
            db_session.add(artifact)
            db_session.flush()
            current = OutputAssembly(
                session_id=session_id,
                generation_run_id=None,
                status="completed",
                artifact_id=artifact.id,
                settings_json={
                    "plan_revision_id": prepared.id,
                    "duration_ms": 2_000,
                    "synchronization": {
                        "mode": "subtitle_timed",
                        "timing_drift": {"worst_block_lag_ms": 400, "blocks": 2},
                    },
                },
            )
            db_session.add(current)
            db_session.flush()
            return artifact.id, current.id

    def test_export_plan_without_a_run_reports_the_active_plan_assembly(self):
        """After a timing-only edit the plan is assembled again without a run."""

        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        output = {"export_mode": "media", "audio_mode": "mixed"}
        self._assembly_for_plan(
            session_id,
            output,
            timing_drift={"worst_block_lag_ms": 9_000, "blocks": 2},
        )
        _artifact_id, current_id = self._current_takes_assembly(session_id)

        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={"output": output},
            continuation=False,
        )

        self.assertEqual(current_id, plan["speech_timing"]["assembly_id"])
        self.assertNotIn("_assembly_artifact_id", plan["speech_timing"])
        export_audio = plan["export_audio"]
        self.assertEqual("named", export_audio["status"])
        self.assertEqual([], export_audio["candidates"])
        version = export_audio["version"]
        # The field and the measured figures agree on the assembly.
        self.assertEqual(current_id, version["assembly_id"])
        self.assertIsNone(version["generation_run_id"])
        self.assertEqual(2, version["plan_revision_number"])
        self.assertTrue(version["active_revision"])
        self.assertTrue(version["assembled_at"])
        self.assertNotIn("relative_path", str(export_audio))
        self.assertEqual(
            400, plan["speech_timing"]["timing_drift"]["worst_block_lag_ms"]
        )

    def test_a_no_run_export_plan_runs_only_the_assembly_it_measured(self):
        """A replaced or removed assembly is refused, not rendered unreviewed."""

        output = {"export_mode": "media", "audio_mode": "mixed"}
        database = self.extension["database"]

        def replace(session_id, artifact_id):
            with database.session() as db_session:
                db_session.get(Artifact, artifact_id).state = "superseded"
            self._current_takes_assembly(session_id, revision_number=3, name="longer")

        def remove(session_id, artifact_id):
            with database.session() as db_session:
                db_session.get(Artifact, artifact_id).state = "deleted"

        for name, change in (("replaced", replace), ("removed", remove)):
            with self.subTest(change=name):
                session_id, _source_id, _asset_id = self._ready_session(
                    workflow_kind="voiceover",
                    suffix="mp4",
                )
                self._assembly_for_plan(session_id, output)
                artifact_id, _assembly_id = self._current_takes_assembly(session_id)
                plan = self._plan(
                    session_id,
                    target_stage="export",
                    overrides={"output": output},
                    continuation=False,
                )
                with database.session() as db_session:
                    stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
                    self.assertEqual(
                        artifact_id,
                        stored.plan_json["_execution"]["payload"][
                            "expected_assembly_artifact_id"
                        ],
                    )
                change(session_id, artifact_id)
                response = self._execute(plan, key=f"no-run-assembly-{name}")
                self.assertEqual(409, response.status_code, response.get_json())
                self.assertEqual("plan_stale", response.get_json()["error"]["code"])

    def test_a_no_run_export_plan_binds_the_named_version_without_timing(self):
        """The version the plan names is the one bound, figures or not."""

        output = {"export_mode": "media", "audio_mode": "mixed"}
        session_id, _source_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        self._assembly_for_plan(session_id, output)
        artifact_id, _assembly_id = self._current_takes_assembly(session_id)
        with patch.object(
            WorkflowExecutionPlanService, "_export_speech_timing", return_value=None
        ):
            plan = self._plan(
                session_id,
                target_stage="export",
                overrides={"output": output},
                continuation=False,
            )
        self.assertEqual("named", plan["export_audio"]["status"])
        self.assertEqual(artifact_id, plan["export_audio"]["version"]["artifact_id"])
        with self.extension["database"].session() as db_session:
            stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            self.assertEqual(
                artifact_id,
                stored.plan_json["_execution"]["payload"]["expected_assembly_artifact_id"],
            )

    def test_export_plan_drops_figures_measured_on_another_assembly(self):
        """An assembly made between the two reads leaves no mismatched figures."""

        output = {"export_mode": "media", "audio_mode": "mixed"}
        real = WorkflowExecutionPlanService._export_audio_version

        def another_assembly(service, session_id, resolved):
            described = real(service, session_id, resolved)
            if described["status"] == "chosen_run":
                described["chosen_run"]["assembly_id"] = "assembly-made-in-between"
            else:
                described["version"] = {
                    **described["version"],
                    "artifact_id": "artifact-made-in-between",
                }
            return described

        for chosen in (False, True):
            with self.subTest(chosen_run=chosen):
                session_id, _source_id, _asset_id = self._ready_session(
                    workflow_kind="voiceover",
                    suffix="mp4",
                )
                run_id = self._assembly_for_plan(session_id, output)
                if not chosen:
                    self._current_takes_assembly(session_id)
                overrides = {**output, "generation_run_id": run_id} if chosen else output
                with patch.object(
                    WorkflowExecutionPlanService,
                    "_export_audio_version",
                    another_assembly,
                ):
                    plan = self._plan(
                        session_id,
                        target_stage="export",
                        overrides={"output": overrides},
                        continuation=False,
                    )
                self.assertIsNone(plan.get("speech_timing"))
                if not chosen:
                    with self.extension["database"].session() as db_session:
                        stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
                        self.assertEqual(
                            "artifact-made-in-between",
                            stored.plan_json["_execution"]["payload"][
                                "expected_assembly_artifact_id"
                            ],
                        )

    def test_export_plan_with_a_run_names_the_run_and_its_assembly(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        output = {"export_mode": "media", "audio_mode": "mixed"}
        run_id = self._assembly_for_plan(session_id, output)

        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={"output": {**output, "generation_run_id": run_id}},
            continuation=False,
        )

        export_audio = plan["export_audio"]
        self.assertEqual("chosen_run", export_audio["status"])
        self.assertIsNone(export_audio["version"])
        chosen = export_audio["chosen_run"]
        self.assertEqual(run_id, chosen["generation_run_id"])
        self.assertEqual(1, chosen["run_sequence_number"])
        self.assertEqual(1, chosen["plan_revision_number"])
        self.assertEqual(plan["speech_timing"]["assembly_id"], chosen["assembly_id"])
        self.assertTrue(chosen["assembled_at"])

        # With no matching assembly the run is still named; the export will
        # assemble it, so there is no assembly or time to report yet.
        database = self.extension["database"]
        with database.session() as db_session:
            for assembly in db_session.scalars(
                select(OutputAssembly).where(OutputAssembly.generation_run_id == run_id)
            ):
                assembly.status = "stale"
        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={"output": {**output, "generation_run_id": run_id}},
            continuation=False,
        )
        chosen = plan["export_audio"]["chosen_run"]
        self.assertEqual("chosen_run", plan["export_audio"]["status"])
        self.assertEqual(run_id, chosen["generation_run_id"])
        self.assertIsNone(chosen["assembly_id"])
        self.assertIsNone(chosen["assembled_at"])

    def test_export_plan_lists_the_candidates_when_none_is_singled_out(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        output = {"export_mode": "media", "audio_mode": "mixed"}
        self._assembly_for_plan(session_id, output)
        # Two more on later revisions, the newest of which is active but has
        # no assembly of its own.
        _first, second_id = self._current_takes_assembly(
            session_id, revision_number=2, name="second"
        )
        _third, third_id = self._current_takes_assembly(
            session_id, revision_number=3, name="third"
        )
        with self.extension["database"].session() as db_session:
            generation_plan = db_session.scalar(
                select(GenerationPlan).where(GenerationPlan.session_id == session_id)
            )
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=4,
                settings_json={},
                content_hash=f"active-without-assembly-{session_id}",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id

        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={"output": output},
            continuation=False,
        )

        export_audio = plan["export_audio"]
        self.assertEqual("ambiguous", export_audio["status"])
        self.assertIsNone(export_audio["version"])
        candidates = export_audio["candidates"]
        self.assertEqual(3, len(candidates))
        self.assertTrue(
            {second_id, third_id} <= {item["assembly_id"] for item in candidates}
        )
        self.assertEqual(
            sorted((item["assembled_at"] for item in candidates), reverse=True),
            [item["assembled_at"] for item in candidates],
        )
        self.assertFalse(any(item["active_revision"] for item in candidates))
        self.assertEqual(
            {1, 2, 3}, {item["plan_revision_number"] for item in candidates}
        )
        # Nothing is measured when the export cannot tell which one to use.
        self.assertIsNone(plan["speech_timing"])

    def test_export_plan_without_generated_audio_carries_no_export_audio(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "export_mode": "media",
                    "audio_mode": "preserve",
                    "subtitle_mode": "none",
                }
            },
            continuation=False,
        )

        self.assertNotIn("export_audio", plan)

    def test_export_plan_skips_the_tail_when_the_timeline_is_not_matched(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        run_id = self._assembly_for_plan(
            session_id,
            {"export_mode": "media", "audio_mode": "mixed"},
            duration_ms=30_000,
        )
        with patch(
            "pandrator.web.soundtrack_export.probe_soundtrack_media",
            side_effect=AssertionError("an unmatched timeline is not measured"),
        ):
            plan = self._plan(
                session_id,
                target_stage="export",
                overrides={
                    "output": {
                        "export_mode": "media",
                        "audio_mode": "mixed",
                        "generation_run_id": run_id,
                        "audio_match_source_duration": False,
                    },
                },
                continuation=False,
            )

        self.assertIsNone(plan["speech_timing"]["tail"])
        self.assertEqual([], [w for w in plan["warnings"] if "last frame" in w])

    def test_export_plan_reports_an_unprobeable_source_without_a_warning(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        run_id = self._assembly_for_plan(
            session_id,
            {"export_mode": "media", "audio_mode": "mixed"},
            duration_ms=30_000,
        )
        with patch(
            "pandrator.web.soundtrack_export.probe_soundtrack_media",
            side_effect=ValueError("The recording duration could not be determined."),
        ):
            plan = self._plan(
                session_id,
                target_stage="export",
                overrides={
                    "output": {
                        "export_mode": "media",
                        "audio_mode": "mixed",
                        "generation_run_id": run_id,
                    }
                },
                continuation=False,
            )

        tail = plan["speech_timing"]["tail"]
        self.assertFalse(tail["checked"])
        self.assertEqual(
            "The source recording's duration could not be probed.", tail["reason"]
        )
        self.assertEqual(
            [],
            [
                warning
                for warning in plan["warnings"]
                if "last frame" in warning or "never cuts speech" in warning
            ],
        )

    def test_export_plan_reports_null_speech_timing_without_an_assembly(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="unassembled-run",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            db_session.add(run)
            db_session.flush()
            run_id = run.id

        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "generation_run_id": run_id,
                    "export_mode": "media",
                    "audio_mode": "mixed",
                }
            },
            continuation=False,
        )

        self.assertIn("speech_timing", plan)
        self.assertIsNone(plan["speech_timing"])
        self.assertEqual("chosen_run", plan["export_audio"]["status"])
        self.assertEqual(run_id, plan["export_audio"]["chosen_run"]["generation_run_id"])
        self.assertIsNone(plan["export_audio"]["chosen_run"]["assembly_id"])
        self.assertIsNone(plan["export_audio"]["chosen_run"]["assembled_at"])
        export_step = next(
            step for step in plan["ordered_steps"] if step["stage"] == "export"
        )
        self.assertNotIn("tail_extension", export_step)

    def test_export_plan_fingerprints_the_effective_settings_block(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )

        plan = self._plan(session_id, target_stage="export", continuation=False)
        public_plan = {
            key: value for key, value in plan.items() if key != "plan_digest"
        }

        self.assertIn("effective", public_plan["settings"])
        # One plan response: its digest covers the block it returns.
        self.assertEqual(plan["plan_digest"], canonical_digest(public_plan))

        edited = {
            **public_plan,
            "settings": {
                **public_plan["settings"],
                "effective": {
                    **public_plan["settings"]["effective"],
                    "export_mode": {"effective": "audio", "origin": "override"},
                },
            },
        }
        self.assertNotEqual(
            public_plan["settings"]["effective"]["export_mode"],
            edited["settings"]["effective"]["export_mode"],
        )
        self.assertNotEqual(plan["plan_digest"], canonical_digest(edited))

    def test_direct_export_plan_does_not_wrap_in_workflow_continuation(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "export_mode": "media",
                    "audio_mode": "preserve",
                    "subtitle_mode": "none",
                }
            },
            continuation=False,
        )

        self.assertEqual(["export"], [item["stage"] for item in plan["ordered_steps"]])
        self.assertEqual("available_after_render", plan["subtitle_review"]["counts_status"])
        self.assertIn("max_chars_per_line", plan["subtitle_review"]["provenance"]["fields"])
        self.assertIn("source", plan["subtitle_review"]["profiles"])
        with self.extension["database"].session() as db_session:
            stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            self.assertEqual("export.create", stored.plan_json["_execution"]["job_kind"])

    def test_direct_generation_export_plans_assembly_instead_of_regeneration(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        for role in ("transcription", "translation"):
            path = self.extension["paths"].uploads / f"assembly-export-{role}.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
            self.extension["services"].artifacts.register(
                path, kind="srt", role=role, session_id=session_id,
            )
        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="plan-content",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            db_session.add(run)
            db_session.flush()
            run_id = run.id

        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "generation_run_id": run_id,
                    "export_mode": "media",
                    "audio_mode": "mixed",
                    "subtitle_mode": "soft",
                    "subtitle_selection": "dual",
                }
            },
            continuation=False,
        )

        self.assertEqual(
            ["assemble_audio", "export"],
            [item["stage"] for item in plan["ordered_steps"]],
        )
        self.assertNotIn("transcribe", [item["stage"] for item in plan["ordered_steps"]])
        self.assertNotIn("generate_audio", [item["stage"] for item in plan["ordered_steps"]])
        with self.extension["database"].session() as db_session:
            stored = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            self.assertEqual("export.variant", stored.plan_json["_execution"]["job_kind"])

    def test_direct_export_plan_does_not_reuse_deleted_assembly_artifact(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        output_override = {
            "generation_run_id": "",
            "export_mode": "media",
            "audio_mode": "mixed",
            "subtitle_mode": "none",
        }
        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="deleted-assembly-plan",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            db_session.add(run)
            db_session.flush()
            run_id = run.id

        output_override["generation_run_id"] = run_id
        resolved, _settings_hash = WorkspaceSettingsService(
            self.extension["database"]
        ).resolve(
            session_id,
            sections=["audio", "output"],
            run_override={"output": output_override},
        )
        with self.extension["database"].session() as db_session:
            artifact_ownership = resolve_artifact_ownership(
                db_session, session_id=session_id
            )
            deleted = Artifact(
                session_id=session_id,
                workspace_id=artifact_ownership.workspace_id,
                project_id=artifact_ownership.project_id,
                kind="audio",
                role="assembled_audio",
                relative_path="deleted-assembly.wav",
                state="deleted",
            )
            db_session.add(deleted)
            db_session.flush()
            db_session.add(
                OutputAssembly(
                    session_id=session_id,
                    generation_run_id=run_id,
                    status="completed",
                    artifact_id=deleted.id,
                    settings_json={
                        "resolved": resolved,
                        "plan_revision_id": revision.id,
                    },
                    settings_hash=output_assembly_settings_hash(resolved),
                )
            )

        plan = self._plan(
            session_id,
            target_stage="export",
            overrides={"output": output_override},
            continuation=False,
        )

        assemble = next(
            item for item in plan["ordered_steps"] if item["stage"] == "assemble_audio"
        )
        self.assertEqual("run", assemble["decision"])
        self.assertEqual("ready", assemble["current_status"])
        self.assertIsNone(assemble["artifact_id"])

    def test_plan_fingerprint_tracks_generation_and_assembly_snapshots(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover",
            suffix="mp4",
        )
        database = self.extension["database"]
        with database.session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="fingerprint-plan",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
                settings_snapshot_json={"speech_boundaries": {"one": {"silence_after_ms": 10}}},
            )
            db_session.add(run)
            db_session.flush()
            artifact_ownership = resolve_artifact_ownership(
                db_session, session_id=session_id
            )
            artifact = Artifact(
                session_id=session_id,
                workspace_id=artifact_ownership.workspace_id,
                project_id=artifact_ownership.project_id,
                kind="audio",
                role="assembled_audio",
                relative_path="fingerprint.wav",
                state="current",
            )
            db_session.add(artifact)
            db_session.flush()
            assembly = OutputAssembly(
                session_id=session_id,
                generation_run_id=run.id,
                status="completed",
                artifact_id=artifact.id,
                settings_json={"resolved": {"speech_boundaries": {"one": {"silence_after_ms": 10}}}},
                settings_hash="same-material-hash",
            )
            db_session.add(assembly)
            db_session.flush()
            run_id = run.id
            assembly_id = assembly.id

        with database.session() as db_session:
            before = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id
            )
            run = db_session.get(GenerationRun, run_id)
            run.settings_snapshot_json = {
                "speech_boundaries": {"one": {"silence_after_ms": 20}}
            }
        with database.session() as db_session:
            after_run_change = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id
            )
            self.assertNotEqual(before, after_run_change)
            assembly = db_session.get(OutputAssembly, assembly_id)
            assembly.settings_json = {
                "resolved": {"speech_boundaries": {"one": {"silence_after_ms": 30}}}
            }
        with database.session() as db_session:
            after_assembly_change = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id
            )
        self.assertNotEqual(after_run_change, after_assembly_change)

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

    def test_local_stt_and_tts_providers_do_not_require_external_confirmation(self):
        resolved = ResolvedWorkflowStage(
            job_kind="workflow.continue",
            payload={
                "resolved_settings_snapshot": {
                    "stt": {"stt_engine": "parakeet"},
                    "tts": {"tts_service": "audio_cpp"},
                }
            },
            resource_keys=("service:stt", "service:tts:audio_cpp"),
            session_revision=1,
            workflow_kind="voiceover",
            source_artifact_id="source",
            source_content_hash="hash",
            outcome_revision=1,
        )
        disclosures = WorkflowExecutionPlanService._provider_disclosures(resolved)
        self.assertEqual(
            {"stt": "parakeet", "tts": "audio_cpp"},
            {item["section"]: item["provider"] for item in disclosures},
        )
        self.assertFalse(
            any(WorkflowExecutionPlanService._is_external(item) for item in disclosures)
        )

    def test_public_ip_endpoint_requires_confirmation_for_local_provider(self):
        session_id, artifact_id, _asset_id = self._ready_session()
        resolved = ResolvedWorkflowStage(
            job_kind="workflow.continue",
            payload={"resolved_settings_snapshot": {
                "tts": {"tts_service": "xtts", "base_url": "https://8.8.8.8"},
            }},
            resource_keys=("service:tts:xtts",),
            session_revision=1,
            workflow_kind="audiobook",
            source_artifact_id=artifact_id,
            source_content_hash="hash",
            outcome_revision=1,
        )
        with patch(
            "pandrator.web.workflows.WorkflowService.resolve_stage", return_value=resolved
        ):
            plan = self._plan(session_id, continuation=False)
        self.assertIn("external_provider", plan["required_confirmations"])
        self.assertIn("estimated_cost_unknown", plan["required_confirmations"])

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
                    workspace_id=initial_personal_workspace(db_session).id,
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
            setting = db_session.get(SessionSetting, (session_id, "tts"))
            if setting is None:
                db_session.add(SessionSetting(
                    session_id=session_id, section="tts", value_json={"speed": 1.01},
                ))
            else:
                setting.value_json = {**setting.value_json, "speed": 1.01}
                setting.revision += 1

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

    def _run_jobs(self) -> int:
        worker = Worker(
            self.extension["services"].jobs,
            "plan-test-worker",
            self.extension["workflow_handlers"].handlers(),
        )
        ran = 0
        while worker.run_once():
            ran += 1
        return ran

    def _publish(self, session_id, *, role, filename, kind="srt"):
        path = Path(self.temporary.name) / filename
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        return self.extension["artifacts"].register(
            path, kind=kind, role=role, session_id=session_id,
        )

    def _export_artifact(self, session_id):
        return self._publish(
            session_id, role="export_subtitle_source", filename="sibling-export.srt"
        )

    def _sibling_export_plan(self, session_id, subtitle_format):
        return self._plan(
            session_id,
            target_stage="export",
            overrides={
                "output": {
                    "export_mode": "subtitles",
                    "subtitle_mode": "none",
                    "subtitle_selection": "source",
                    "subtitle_format": subtitle_format,
                }
            },
            continuation=False,
        )

    def _export_outputs(self, session_id) -> list[str]:
        with self.extension["database"].session() as db_session:
            return list(
                db_session.scalars(
                    select(Artifact.role).where(
                        Artifact.session_id == session_id,
                        Artifact.role.like("export%"),
                    )
                ).all()
            )

    def test_two_subtitle_export_plans_both_execute_back_to_back(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="subtitles", suffix="srt",
        )
        srt_plan = self._sibling_export_plan(session_id, "srt")
        vtt_plan = self._sibling_export_plan(session_id, "vtt")
        self.assertNotEqual(srt_plan["plan_digest"], vtt_plan["plan_digest"])

        first = self._execute(srt_plan, key="sibling-srt-execute")
        self.assertEqual(202, first.status_code, first.get_json())
        self._run_jobs()
        self.assertTrue(self._export_outputs(session_id))
        with self.extension["database"].session() as db_session:
            first_job = db_session.get(Job, first.get_json()["id"])
        self.assertEqual("succeeded", first_job.status, first_job.error_message)

        second = self._execute(vtt_plan, key="sibling-vtt-execute")
        self.assertEqual(202, second.status_code, second.get_json())

    def test_an_audio_export_plan_survives_the_assembly_its_sibling_built(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="voiceover", suffix="mp4",
        )
        with self.extension["database"].session() as db_session:
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="sibling-assembly-plan",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            db_session.add(run)
            db_session.flush()
            run_id = run.id

        def audio_plan(subtitle_format):
            return self._plan(
                session_id,
                target_stage="export",
                overrides={
                    "output": {
                        "generation_run_id": run_id,
                        "export_mode": "media",
                        "audio_mode": "dubbing_only",
                        "subtitle_mode": "none",
                        "subtitle_format": subtitle_format,
                    }
                },
                continuation=False,
            )

        first_plan = audio_plan("srt")
        second_plan = audio_plan("vtt")
        self.assertEqual("run", first_plan["ordered_steps"][0]["decision"])
        self.assertEqual("assemble_audio", first_plan["ordered_steps"][0]["stage"])

        executed = self._execute(first_plan, key="sibling-audio-first")
        self.assertEqual(202, executed.status_code, executed.get_json())
        # Write what that execution publishes: the assembly row, its published
        # audio artifact, and the export file itself.
        assembled = self._publish(
            session_id, role="assembled_audio", filename="sibling-assembly.wav",
            kind="audio",
        )
        with self.extension["database"].session() as db_session:
            db_session.add(
                OutputAssembly(
                    session_id=session_id,
                    generation_run_id=run_id,
                    status="completed",
                    artifact_id=assembled.id,
                    settings_json={"resolved": {}},
                    settings_hash=output_assembly_settings_hash({"resolved": {}}),
                )
            )
        self._export_artifact(session_id)

        second = self._execute(second_plan, key="sibling-audio-second")
        self.assertEqual(202, second.status_code, second.get_json())

    def test_an_export_plan_still_goes_stale_on_settings_selection_or_run(self):
        def settings_change(db_session, session_id, _artifact_id, _asset_id):
            setting = db_session.get(SessionSetting, (session_id, "output"))
            if setting is None:
                db_session.add(SessionSetting(
                    session_id=session_id, section="output",
                    value_json={"subtitle_format": "vtt"},
                ))
            else:
                setting.value_json = {**setting.value_json, "subtitle_format": "vtt"}
                setting.revision += 1

        def selection_change(db_session, session_id, artifact_id, _asset_id):
            selection = db_session.get(
                SessionStageSelection, (session_id, "translate")
            )
            if selection is None:
                db_session.add(SessionStageSelection(
                    session_id=session_id, stage_key="translate",
                    artifact_id=artifact_id,
                ))
            else:
                selection.revision += 1

        def run_change(db_session, session_id, _artifact_id, _asset_id):
            generation_plan = GenerationPlan(session_id=session_id)
            db_session.add(generation_plan)
            db_session.flush()
            revision = GenerationPlanRevision(
                plan_id=generation_plan.id,
                revision_number=1,
                settings_json={},
                content_hash="sibling-stale-run-plan",
            )
            db_session.add(revision)
            db_session.flush()
            generation_plan.active_revision_id = revision.id
            db_session.add(
                GenerationRun(
                    session_id=session_id,
                    plan_revision_id=revision.id,
                    sequence_number=1,
                )
            )

        for name, mutation in (
            ("settings", settings_change),
            ("selection", selection_change),
            ("generation_run", run_change),
        ):
            with self.subTest(change=name):
                session_id, artifact_id, asset_id = self._ready_session(
                    workflow_kind="subtitles", suffix="srt",
                )
                plan = self._sibling_export_plan(session_id, "srt")
                with self.extension["database"].immediate_session() as db_session:
                    mutation(db_session, session_id, artifact_id, asset_id)
                response = self._execute(plan, key=f"sibling-stale-{name}")
                self.assertEqual(409, response.status_code, response.get_json())
                self.assertEqual("plan_stale", response.get_json()["error"]["code"])

    def test_a_non_export_plan_keeps_its_full_fingerprint(self):
        session_id, _artifact_id, _asset_id = self._ready_session()
        plan = self._plan(session_id)
        database = self.extension["database"]
        self._export_artifact(session_id)
        with database.session() as db_session:
            full = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id
            )
            export = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id, fingerprint_variant=FINGERPRINT_EXPORT,
            )
        self.assertNotEqual(full, export)
        response = self._execute(plan, key="non-export-fingerprint")
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("plan_stale", response.get_json()["error"]["code"])

    def test_an_export_artifact_does_not_move_an_export_plan_fingerprint(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="subtitles", suffix="srt",
        )
        plan = self._sibling_export_plan(session_id, "srt")
        database = self.extension["database"]
        with database.session() as db_session:
            before = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id, fingerprint_variant=FINGERPRINT_EXPORT,
            )
        self._export_artifact(session_id)
        # A mixed sibling export caches its soundtrack master; that is derived
        # output, not an input this plan reviewed.
        self._publish(
            session_id, role="soundtrack_master", filename="soundtrack-master.wav"
        )
        with database.session() as db_session:
            after = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id, fingerprint_variant=FINGERPRINT_EXPORT,
            )
        self.assertEqual(before, after)
        response = self._execute(plan, key="export-artifact-ignored")
        self.assertEqual(202, response.status_code, response.get_json())

    def test_a_plan_stored_before_the_variant_compares_with_all_state(self):
        session_id, _artifact_id, _asset_id = self._ready_session(
            workflow_kind="subtitles", suffix="srt",
        )
        plan = self._sibling_export_plan(session_id, "srt")
        database = self.extension["database"]
        # An earlier release stored no variant marker and hashed every row.
        with database.immediate_session() as db_session:
            record = db_session.get(WorkflowExecutionPlan, plan["plan_id"])
            execution = dict(record.plan_json["_execution"])
            execution.pop("fingerprint_variant", None)
            record.plan_json = {**record.plan_json, "_execution": execution}
            record.state_fingerprint = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id
            )
        response = self._execute(plan, key="pre-upgrade-export-plan")
        self.assertEqual(202, response.status_code, response.get_json())

        stored = self._sibling_export_plan(session_id, "vtt")
        with database.immediate_session() as db_session:
            record = db_session.get(WorkflowExecutionPlan, stored["plan_id"])
            execution = dict(record.plan_json["_execution"])
            execution.pop("fingerprint_variant", None)
            record.plan_json = {**record.plan_json, "_execution": execution}
            record.state_fingerprint = WorkflowExecutionPlanService._state_fingerprint(
                db_session, session_id
            )
        self._export_artifact(session_id)
        stale = self._execute(stored, key="pre-upgrade-export-stale")
        self.assertEqual(409, stale.status_code, stale.get_json())
        self.assertEqual("plan_stale", stale.get_json()["error"]["code"])

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


class ProviderEndpointClassificationTests(unittest.TestCase):
    def test_explicit_endpoint_determines_external_classification(self):
        cases = (
            ("https://8.8.8.8", True),
            ("https://[2001:4860:4860::8888]", True),
            ("http://127.0.0.1:8020", False),
            ("http://[::1]:8020", False),
            ("http://192.168.1.10:8020", False),
            ("http://localhost:8020", False),
            ("http://host.docker.internal:8020", False),
            ("https://remote.example", True),
        )
        for provider in ("xtts", "external-provider"):
            for endpoint, expected in cases:
                with self.subTest(provider=provider, endpoint=endpoint):
                    self.assertEqual(expected, WorkflowExecutionPlanService._is_external({
                        "provider": provider, "base_url": endpoint,
                    }))
        self.assertFalse(WorkflowExecutionPlanService._is_external({"provider": "xtts"}))
        self.assertTrue(WorkflowExecutionPlanService._is_external({"provider": "external"}))

    def test_malformed_url_preserves_parser_failure(self):
        with self.assertRaisesRegex(ValueError, "Invalid IPv6 URL"):
            WorkflowExecutionPlanService._is_external({
                "provider": "xtts", "base_url": "https://[",
            })
