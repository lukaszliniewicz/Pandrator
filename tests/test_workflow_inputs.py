"""Revision-safe exact input selection for each workflow consumer."""

from __future__ import annotations

import tempfile
import unittest
import uuid

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.artifact_selection import choose_artifact
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    Job,
    MediaEditPlan,
    MediaEditPlanRevision,
    OutcomePlan,
    SessionSetting,
    SessionStageSelection,
)
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.workflow_inputs import (
    get_workflow_inputs,
    select_workflow_input,
)

_SUBTITLE = "1\n00:00:01,000 --> 00:00:02,000\nFixture subtitle.\n"


class WorkflowInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}).get_json()["csrf_token"]
        self.addCleanup(self.app.extensions["pandrator"]["database"].dispose)
        self.addCleanup(self.temporary.cleanup)
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]

    def _session(self, kind: str = "voiceover") -> str:
        included = ["transcribe", "correct", "translate", "generate_audio"]
        return (
            self.services["sessions"]
            .create(
                f"Workflow input test {uuid.uuid4().hex[:8]}",
                workflow_kind=kind,
                included_stages=included,
            )
            .id
        )

    def _artifact(
        self,
        session_id: str,
        role: str,
        name: str,
        *,
        parent_ids: list[str] | None = None,
        content: str = _SUBTITLE,
        metadata: dict | None = None,
    ) -> Artifact:
        path = self.services["paths"].uploads / f"{uuid.uuid4().hex}-{name}.srt"
        path.write_text(content, encoding="utf-8")
        return self.services["artifacts"].register(
            path,
            kind="srt",
            role=role,
            session_id=session_id,
            parent_ids=parent_ids,
            metadata={"original_filename": path.name, **(metadata or {})},
        )

    def _upload(self, session_id: str) -> Artifact:
        artifact = self._artifact(session_id, "upload", "source")
        asset = self.services["source_library"].ensure_for_artifact(
            artifact.id,
            display_name="source.srt",
            kind="srt",
        )
        self.services["source_library"].attach(session_id, asset.id)
        return artifact

    def _save_outcome(self, session_id: str, **input_roles: str) -> dict:
        current = self.services["outcome_plans"].get(session_id)
        value = current["value"]
        value["unrelated_custom_field"] = {"keep": [1, 2, 3]}
        value["inputs"] = {**value.get("inputs", {}), **input_roles}
        return self.services["outcome_plans"].update(session_id, current["revision"], value)

    def _snapshot(self, session_id: str) -> tuple:
        with self.database.snapshot_session() as session:
            outcome = session.get(OutcomePlan, session_id)
            setting = session.get(SessionSetting, (session_id, "translation"))
            selections = list(
                session.scalars(
                    select(SessionStageSelection)
                    .where(SessionStageSelection.session_id == session_id)
                    .order_by(SessionStageSelection.stage_key)
                ).all()
            )
            return (
                (dict(outcome.value_json), outcome.revision) if outcome else None,
                (dict(setting.value_json), setting.revision) if setting else None,
                tuple((row.stage_key, row.artifact_id, row.revision) for row in selections),
            )

    def _expected_selection_revision(self, session_id: str, stage: str) -> int:
        with self.database.snapshot_session() as session:
            selection = session.get(SessionStageSelection, (session_id, stage))
            return int(selection.revision) if selection is not None else 0

    def test_exact_correction_updates_translation_and_speech_inputs_atomically(self) -> None:
        session_id = self._session()
        source = self._upload(session_id)
        transcription = self._artifact(
            session_id, "transcription", "transcription", parent_ids=[source.id]
        )
        correction_before = self._artifact(
            session_id, "correction", "correction-before", parent_ids=[transcription.id]
        )
        translation_before = self._artifact(
            session_id, "translation", "translation-before", parent_ids=[correction_before.id]
        )
        correction = self._artifact(
            session_id, "correction", "correction-selected", parent_ids=[transcription.id]
        )
        with self.database.immediate_session() as session:
            choose_artifact(session, session_id, "transcribe", transcription.id)
            choose_artifact(session, session_id, "correct", correction_before.id)
            choose_artifact(session, session_id, "translate", translation_before.id)

        outcome = self._save_outcome(session_id, translation="source", generation="source")
        translation_settings = self.services["workspace_settings"].patch(
            session_id,
            "translation",
            0,
            {
                "source_artifact_id": transcription.id,
                "target_language": "fr",
                "glossary_id": "preserve-this-glossary",
            },
        )
        initial_manifest = get_workflow_inputs(self.services, session_id)
        self.assertEqual(
            transcription.id,
            initial_manifest["consumers"]["translation"]["artifact"]["artifact_id"],
        )
        self.assertNotIn("Fixture subtitle", repr(initial_manifest))
        expected_outcome_revision = outcome["revision"]
        expected_selection_revision = self._expected_selection_revision(session_id, "correct")

        translated_input = select_workflow_input(
            self.services,
            session_id,
            "translation",
            "correction",
            correction.id,
            expected_outcome_revision=expected_outcome_revision,
            expected_selection_revision=expected_selection_revision,
            expected_translation_settings_revision=translation_settings["revision"],
        )

        self.assertEqual("correction", translated_input["inputs"]["translation"])
        self.assertEqual(correction.id, translated_input["selected"]["artifact_id"])
        self.assertEqual(
            translated_input["consumers"]["translation"]["producer_selection_revision"],
            translated_input["selected"]["producer_selection_revision"],
        )
        self.assertEqual([], translated_input["blocking_reasons"])
        self.assertEqual(
            correction.id,
            translated_input["consumers"]["translation"]["artifact"]["artifact_id"],
        )
        self.assertNotIn("Fixture subtitle", repr(translated_input))

        after_translation = self._snapshot(session_id)
        self.assertEqual(
            {
                "source_artifact_id": correction.id,
                "target_language": "fr",
                "glossary_id": "preserve-this-glossary",
            },
            after_translation[1][0],
        )
        self.assertEqual(
            {"keep": [1, 2, 3]},
            after_translation[0][0]["unrelated_custom_field"],
        )
        self.assertIsNone(
            next(
                (
                    artifact_id
                    for stage, artifact_id, _revision in after_translation[2]
                    if stage == "translate"
                ),
                None,
            ),
            "Changing the correction branch must clear the old translation selection.",
        )

        generation_input = select_workflow_input(
            self.services,
            session_id,
            "generation",
            "correction",
            correction.id,
            expected_outcome_revision=translated_input["outcome_revision"],
            expected_selection_revision=translated_input["selected"]["producer_selection_revision"],
        )
        self.assertEqual("correction", generation_input["inputs"]["generation"])
        self.assertEqual(
            correction.id,
            generation_input["consumers"]["generation"]["artifact"]["artifact_id"],
        )
        self.assertEqual(
            correction.id,
            generation_input["consumers"]["translation"]["artifact"]["artifact_id"],
        )

    def test_media_edit_source_role_uses_canonical_media_edit_subtitles(self) -> None:
        session_id = self._session("media_edit")
        source_media = self._artifact(
            session_id,
            "upload",
            "media",
            content="media payload",
        )
        source_captions = self._artifact(session_id, "upload", "captions", content=_SUBTITLE)
        edited = self._artifact(
            session_id,
            "media_edit_subtitles",
            "edited-captions",
            parent_ids=[source_captions.id],
        )
        with self.database.immediate_session() as session:
            plan = MediaEditPlan(session_id=session_id)
            session.add(plan)
            session.flush()
            revision = MediaEditPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                source_media_artifact_id=source_media.id,
                editorial_transcript_artifact_id=source_captions.id,
                duration_ms=1000,
                content_hash="media-edit-revision-hash",
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            edited_record = session.get(Artifact, edited.id)
            edited_record.metadata_json = {
                **edited_record.metadata_json,
                "content_hash": revision.content_hash,
                "media_edit_revision_id": revision.id,
            }
        outcome = self._save_outcome(session_id, translation="source", generation="source")

        result = select_workflow_input(
            self.services,
            session_id,
            "translation",
            "source",
            edited.id,
            expected_outcome_revision=outcome["revision"],
            expected_selection_revision=self._expected_selection_revision(session_id, "edit_media"),
            expected_translation_settings_revision=0,
        )

        self.assertEqual("media_edit", result["inputs"]["translation"])
        self.assertEqual("media_edit", result["selected"]["stored_role"])
        consumer = result["consumers"]["translation"]
        self.assertEqual("media_edit", consumer["input_role"])
        self.assertEqual("source", consumer["requested_role"])
        self.assertEqual("media_edit_subtitles", consumer["artifact"]["role"])
        self.assertEqual(edited.id, consumer["artifact"]["artifact_id"])
        self.assertEqual(1, consumer["producer_selection_revision"])

    def test_upload_source_requires_current_primary_and_selection_revision_zero(self) -> None:
        session_id = self._session("subtitles")
        primary = self._upload(session_id)
        other_upload = self._artifact(session_id, "upload", "unattached-upload")
        outcome = self._save_outcome(session_id, translation="source", generation="source")
        before = self._snapshot(session_id)

        with self.assertRaises(ValueError):
            select_workflow_input(
                self.services,
                session_id,
                "generation",
                "source",
                other_upload.id,
                expected_outcome_revision=outcome["revision"],
                expected_selection_revision=0,
            )

        self.assertEqual(before, self._snapshot(session_id))
        result = select_workflow_input(
            self.services,
            session_id,
            "generation",
            "source",
            primary.id,
            expected_outcome_revision=outcome["revision"],
            expected_selection_revision=0,
        )
        self.assertEqual(0, result["selected"]["producer_selection_revision"])
        self.assertEqual(primary.id, result["selected"]["artifact_id"])

    def test_first_input_selection_preserves_effective_legacy_speech_flags(self) -> None:
        session_id = self._session()
        primary = self._upload(session_id)
        self.services["workspace_settings"].patch(
            session_id,
            "text",
            0,
            {
                "llm_tts_optimization": True,
                "llm_tts_document_optimization": False,
            },
        )
        self.assertIsNone(self._snapshot(session_id)[0])

        manifest = get_workflow_inputs(self.services, session_id)
        self.assertEqual(
            {
                "llm_tts_optimization": True,
                "llm_tts_document_optimization": False,
            },
            manifest["transformations"],
        )
        self.assertIsNone(
            self._snapshot(session_id)[0],
            "Reading the manifest must not create an explicit outcome plan.",
        )

        selected = select_workflow_input(
            self.services,
            session_id,
            "generation",
            "source",
            primary.id,
            expected_outcome_revision=manifest["outcome_revision"],
            expected_selection_revision=0,
        )

        self.assertEqual(manifest["transformations"], selected["transformations"])
        saved_outcome = self._snapshot(session_id)[0][0]
        self.assertEqual(
            manifest["transformations"]["llm_tts_optimization"],
            saved_outcome["transformations"]["llm_tts_optimization"],
        )
        self.assertEqual(
            manifest["transformations"]["llm_tts_document_optimization"],
            saved_outcome["transformations"]["llm_tts_document_optimization"],
        )

    def test_explicit_saved_false_speech_flags_override_enabled_text_settings(self) -> None:
        session_id = self._session()
        primary = self._upload(session_id)
        outcome = self._save_outcome(session_id, generation="source")
        self.services["workspace_settings"].patch(
            session_id,
            "text",
            0,
            {"llm_tts_optimization": True},
        )

        manifest = get_workflow_inputs(self.services, session_id)
        self.assertFalse(manifest["transformations"]["llm_tts_optimization"])
        self.assertFalse(manifest["transformations"]["llm_tts_document_optimization"])
        selected = select_workflow_input(
            self.services,
            session_id,
            "generation",
            "source",
            primary.id,
            expected_outcome_revision=outcome["revision"],
            expected_selection_revision=0,
        )

        self.assertFalse(selected["transformations"]["llm_tts_optimization"])
        self.assertFalse(selected["transformations"]["llm_tts_document_optimization"])

    def test_stale_outcome_revision_rolls_back_everything(self) -> None:
        session_id = self._session()
        source = self._upload(session_id)
        transcription = self._artifact(
            session_id, "transcription", "transcription", parent_ids=[source.id]
        )
        correction = self._artifact(
            session_id, "correction", "correction", parent_ids=[transcription.id]
        )
        with self.database.immediate_session() as session:
            choose_artifact(session, session_id, "correct", correction.id)
        outcome = self._save_outcome(session_id, translation="source")
        self.services["workspace_settings"].patch(
            session_id, "translation", 0, {"source_artifact_id": source.id}
        )
        before = self._snapshot(session_id)

        with self.assertRaises(RevisionConflict):
            select_workflow_input(
                self.services,
                session_id,
                "translation",
                "correction",
                correction.id,
                expected_outcome_revision=outcome["revision"] - 1,
                expected_selection_revision=self._expected_selection_revision(
                    session_id, "correct"
                ),
                expected_translation_settings_revision=1,
            )

        self.assertEqual(before, self._snapshot(session_id))

    def test_selection_joins_caller_transaction_and_rolls_back_with_it(self) -> None:
        session_id = self._session()
        source = self._upload(session_id)
        transcription = self._artifact(
            session_id, "transcription", "transcription", parent_ids=[source.id]
        )
        correction = self._artifact(
            session_id, "correction", "correction", parent_ids=[transcription.id]
        )
        with self.database.immediate_session() as session:
            choose_artifact(session, session_id, "correct", correction.id)
        outcome = self._save_outcome(session_id, translation="source")
        setting = self.services["workspace_settings"].patch(
            session_id, "translation", 0, {"source_artifact_id": source.id}
        )
        before = self._snapshot(session_id)

        with self.assertRaisesRegex(RuntimeError, "idempotency completion failed"):
            with self.database.immediate_session() as db_session:
                select_workflow_input(
                    self.services,
                    session_id,
                    "translation",
                    "correction",
                    correction.id,
                    expected_outcome_revision=outcome["revision"],
                    expected_selection_revision=self._expected_selection_revision(
                        session_id, "correct"
                    ),
                    expected_translation_settings_revision=setting["revision"],
                    db_session=db_session,
                )
                raise RuntimeError("idempotency completion failed")

        self.assertEqual(before, self._snapshot(session_id))

    def test_stale_producer_and_translation_settings_revisions_roll_back(self) -> None:
        session_id = self._session()
        source = self._upload(session_id)
        transcription = self._artifact(
            session_id, "transcription", "transcription", parent_ids=[source.id]
        )
        correction = self._artifact(
            session_id, "correction", "correction", parent_ids=[transcription.id]
        )
        with self.database.immediate_session() as session:
            choose_artifact(session, session_id, "correct", correction.id)
        outcome = self._save_outcome(session_id, translation="source")
        self.services["workspace_settings"].patch(
            session_id, "translation", 0, {"source_artifact_id": source.id}
        )
        baseline = self._snapshot(session_id)

        for selection_revision, settings_revision in ((99, 1), (1, 99)):
            with self.subTest(
                selection_revision=selection_revision,
                settings_revision=settings_revision,
            ):
                with self.assertRaises(RevisionConflict):
                    select_workflow_input(
                        self.services,
                        session_id,
                        "translation",
                        "correction",
                        correction.id,
                        expected_outcome_revision=outcome["revision"],
                        expected_selection_revision=selection_revision,
                        expected_translation_settings_revision=settings_revision,
                    )
                self.assertEqual(baseline, self._snapshot(session_id))

    def test_foreign_session_and_wrong_role_artifacts_roll_back(self) -> None:
        session_id = self._session()
        other_session = self._session()
        source = self._upload(session_id)
        foreign = self._artifact(other_session, "correction", "foreign")
        transcription = self._artifact(
            session_id, "transcription", "transcription", parent_ids=[source.id]
        )
        correction = self._artifact(
            session_id, "correction", "correction", parent_ids=[transcription.id]
        )
        with self.database.immediate_session() as session:
            choose_artifact(session, session_id, "correct", correction.id)
        outcome = self._save_outcome(session_id, translation="source")
        self.services["workspace_settings"].patch(
            session_id, "translation", 0, {"source_artifact_id": source.id}
        )
        baseline = self._snapshot(session_id)

        for role, artifact_id in (("correction", foreign.id), ("source", correction.id)):
            with self.subTest(role=role, artifact_id=artifact_id):
                with self.assertRaises((KeyError, ValueError)):
                    select_workflow_input(
                        self.services,
                        session_id,
                        "translation",
                        role,
                        artifact_id,
                        expected_outcome_revision=outcome["revision"],
                        expected_selection_revision=self._expected_selection_revision(
                            session_id, "correct"
                        ),
                        expected_translation_settings_revision=1,
                    )
                self.assertEqual(baseline, self._snapshot(session_id))

    def test_hash_mismatch_and_active_job_fail_without_partial_state(self) -> None:
        session_id = self._session()
        source = self._upload(session_id)
        transcription = self._artifact(
            session_id, "transcription", "transcription", parent_ids=[source.id]
        )
        correction = self._artifact(
            session_id, "correction", "correction", parent_ids=[transcription.id]
        )
        with self.database.immediate_session() as session:
            choose_artifact(session, session_id, "correct", correction.id)
        outcome = self._save_outcome(session_id, translation="source")
        self.services["workspace_settings"].patch(
            session_id, "translation", 0, {"source_artifact_id": source.id}
        )
        hash_path = self.services["paths"].managed_path(correction.relative_path)
        hash_path.write_text("tampered subtitle", encoding="utf-8")
        baseline = self._snapshot(session_id)
        kwargs = {
            "expected_outcome_revision": outcome["revision"],
            "expected_selection_revision": self._expected_selection_revision(session_id, "correct"),
            "expected_translation_settings_revision": 1,
        }

        with self.assertRaises(ValueError):
            select_workflow_input(
                self.services,
                session_id,
                "translation",
                "correction",
                correction.id,
                **kwargs,
            )
        self.assertEqual(baseline, self._snapshot(session_id))

        hash_path.write_text(_SUBTITLE, encoding="utf-8")
        with self.database.session() as session:
            session.add(Job(kind="test-active-work", session_id=session_id, status="running"))
        baseline = self._snapshot(session_id)
        with self.assertRaises(RevisionConflict):
            select_workflow_input(
                self.services,
                session_id,
                "translation",
                "correction",
                correction.id,
                **kwargs,
            )
        self.assertEqual(baseline, self._snapshot(session_id))


if __name__ == "__main__":
    unittest.main()


    def test_http_input_selection_replays_atomically_and_rejects_stale_fences(self):
        session_id = self._session()
        source = self._upload(session_id)
        endpoint = f"/api/v1/sessions/{session_id}/workflow-inputs"
        manifest = self.client.get(endpoint).get_json()
        before = self._snapshot(session_id)
        body = {"consumer": "generation", "role": "source", "artifact_id": source.id,
                "expected_outcome_revision": manifest["outcome_revision"], "expected_selection_revision": 0}
        headers = {"X-CSRF-Token": self.csrf, "Idempotency-Key": "input-http-select-1"}
        stale = self.client.put(endpoint, json={**body, "expected_outcome_revision": 99}, headers=headers)
        self.assertEqual(409, stale.status_code, stale.get_json())
        self.assertEqual(before, self._snapshot(session_id))
        selected = self.client.put(endpoint, json=body, headers=headers)
        self.assertEqual(200, selected.status_code, selected.get_json())
        snapshot = self._snapshot(session_id)
        replay = self.client.put(endpoint, json=body, headers=headers)
        self.assertEqual(200, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(selected.get_json(), replay.get_json())
        self.assertEqual(snapshot, self._snapshot(session_id))
        fresh = self.client.put(endpoint, json=body,
                headers={**headers, "Idempotency-Key": "input-http-select-2"})
        self.assertEqual(409, fresh.status_code, fresh.get_json())
        self.assertEqual(snapshot, self._snapshot(session_id))
