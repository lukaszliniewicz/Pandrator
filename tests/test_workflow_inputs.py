"""Revision-safe exact input selection for each workflow consumer."""

from __future__ import annotations

import json
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
    SessionRecord,
    SessionSetting,
    SessionStageSelection,
)
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_plan_workspace import planning_settings
from pandrator.web.workflow_inputs import (
    configure_speech_optimization,
    get_workflow_inputs,
    select_workflow_input,
)

_SUBTITLE = "1\n00:00:01,000 --> 00:00:02,000\nFixture subtitle.\n"


class WorkflowInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}).get_json()["csrf_token"]
        self.addCleanup(self.app.extensions["pandrator"]["database"].dispose)
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


    def _speech_snapshot(self, session_id):
        with self.database.snapshot_session() as session:
            text = session.get(SessionSetting, (session_id, "text"))
            return self._snapshot(session_id), (dict(text.value_json), text.revision) if text else None

    def _text_artifact(self, session_id, role):
        path = self.services["paths"].uploads / f"{uuid.uuid4().hex}.json"
        path.write_text(json.dumps([{"processed_sentence": "Hello, world.", "language": "en"}]), encoding="utf-8")
        return self.services["artifacts"].register(path, kind="json", role=role, session_id=session_id)

    def test_configure_synchronizes_flags_preserves_fields_and_is_noop(self):
        sid = self._session("audiobook")
        self.services["workspace_settings"].patch(sid, "text", 0, {"unrelated": "keep"})
        prepared = self._text_artifact(sid, "prepared_text")
        before = get_workflow_inputs(self.services, sid)
        result = configure_speech_optimization(
            self.services, sid, mode="document", annotation_mode="speakers", annotation_only=True,
            expected_outcome_revision=before["outcome_revision"], expected_text_settings_revision=before["text_settings_revision"],
        )
        self.assertTrue(result["transformations"]["llm_tts_document_optimization"])
        self.assertFalse(result["transformations"]["llm_tts_optimization"])
        self.assertEqual("tts_optimized", result["inputs"]["generation"])
        stable = self._speech_snapshot(sid)
        self.assertEqual("keep", stable[1][0]["unrelated"])
        self.assertEqual("speakers", stable[1][0]["llm_tts_annotation_mode"])
        self.assertIn(prepared.id, [row[1] for row in stable[0][2]])
        replay = configure_speech_optimization(
            self.services, sid, mode="document", annotation_mode="speakers", annotation_only=True,
            expected_outcome_revision=result["outcome_revision"], expected_text_settings_revision=result["text_settings_revision"],
        )
        self.assertEqual(result, replay)
        self.assertEqual(stable, self._speech_snapshot(sid))
        for mode in ("inline", "off"):
            result = configure_speech_optimization(
                self.services, sid, mode=mode, expected_outcome_revision=result["outcome_revision"],
                expected_text_settings_revision=result["text_settings_revision"],
            )
            self.assertEqual(mode == "inline", result["transformations"]["llm_tts_optimization"])
            self.assertFalse(result["transformations"]["llm_tts_document_optimization"])

    def test_configure_stale_fences_and_invalid_annotations_write_nothing(self):
        sid = self._session()
        before = self._speech_snapshot(sid)
        for outcome_revision, text_revision in ((99, 0), (0, 99)):
            with self.assertRaises(RevisionConflict):
                configure_speech_optimization(self.services, sid, mode="inline",
                    expected_outcome_revision=outcome_revision, expected_text_settings_revision=text_revision)
            self.assertEqual(before, self._speech_snapshot(sid))
        for mode, annotation_mode, annotation_only in (("inline", "speakers", False), ("document", "off", True), ("document", "invalid", False)):
            with self.assertRaises(ValueError):
                configure_speech_optimization(self.services, sid, mode=mode, annotation_mode=annotation_mode,
                    annotation_only=annotation_only, expected_outcome_revision=0, expected_text_settings_revision=0)
            self.assertEqual(before, self._speech_snapshot(sid))

    def test_first_configuration_preserves_existing_producer_selections(self):
        sid = self._session()
        with self.database.session() as session:
            session.get(SessionRecord, sid).included_stages_json = ["generate_audio"]
        self._artifact(sid, "translation", "existing-translation")
        before = self._snapshot(sid)[2]
        configure_speech_optimization(self.services, sid, mode="off",
            expected_outcome_revision=0, expected_text_settings_revision=0)
        self.assertEqual(before, self._snapshot(sid)[2])

    def test_missing_outcome_read_freezes_effective_legacy_flags(self):
        sid = self._session("audiobook")
        self.services["workspace_settings"].patch(sid, "text", 0, {"llm_tts_document_optimization": True})
        manifest = get_workflow_inputs(self.services, sid)
        self.assertEqual(0, manifest["outcome_revision"])
        self.assertTrue(manifest["transformations"]["llm_tts_document_optimization"])
        self.assertEqual("tts_optimized", manifest["inputs"]["generation"])
        outcome = self.services["outcome_plans"].get(sid)
        self.assertTrue(outcome["value"]["transformations"]["llm_tts_document_optimization"])

    def test_audiobook_txt_manifest_requires_prepared_text_not_subtitles(self):
        sid = self._session("audiobook")
        path = self.services["paths"].uploads / f"{uuid.uuid4().hex}.txt"
        path.write_text("Accepted audiobook text.", encoding="utf-8")
        source = self.services["artifacts"].register(path, kind="text", role="upload", session_id=sid)
        asset = self.services["source_library"].ensure_for_artifact(source.id, display_name=path.name, kind="text")
        self.services["source_library"].attach(sid, asset.id)
        manifest = get_workflow_inputs(self.services, sid)
        self.assertFalse(manifest["consumers"]["translation"]["applicable"])
        self.assertTrue(manifest["consumers"]["generation"]["applicable"])
        self.assertEqual("prepared_text", manifest["inputs"]["generation"])
        self.assertEqual(["generation: No artifact is selected for this workflow input."], manifest["blocking_reasons"])
        prepared = self._text_artifact(sid, "prepared_text")
        manifest = get_workflow_inputs(self.services, sid)
        self.assertEqual(prepared.id, manifest["consumers"]["generation"]["artifact"]["artifact_id"])
        self.assertEqual([], manifest["blocking_reasons"])

    def test_audiobook_exact_selection_synchronizes_and_rolls_back_stale_fences(self):
        sid = self._session("audiobook")
        prepared = self._text_artifact(sid, "prepared_text")
        optimized = self._text_artifact(sid, "tts_optimized")
        configured = configure_speech_optimization(self.services, sid, mode="inline",
            expected_outcome_revision=0, expected_text_settings_revision=0)
        configured = select_workflow_input(self.services, sid, "generation", "prepared_text", prepared.id,
            expected_outcome_revision=configured["outcome_revision"], expected_text_settings_revision=configured["text_settings_revision"],
            expected_selection_revision=self._expected_selection_revision(sid, "prepare_text"))
        self.assertTrue(configured["transformations"]["llm_tts_optimization"])
        stable = self._speech_snapshot(sid)
        values = {
            "expected_outcome_revision": configured["outcome_revision"],
            "expected_selection_revision": self._expected_selection_revision(sid, "optimize_tts"),
            "expected_text_settings_revision": configured["text_settings_revision"],
        }
        for field in values:
            with self.assertRaises(RevisionConflict):
                select_workflow_input(self.services, sid, "generation", "tts_optimized", optimized.id,
                    **{**values, field: 99})
            self.assertEqual(stable, self._speech_snapshot(sid))
        result = select_workflow_input(self.services, sid, "generation", "tts_optimized", optimized.id, **values)
        self.assertTrue(result["transformations"]["llm_tts_document_optimization"])
        self.assertFalse(result["transformations"]["llm_tts_optimization"])
        self.assertEqual(optimized.id, result["consumers"]["generation"]["artifact"]["artifact_id"])
        self.assertEqual(stable[0][0][0]["inputs"], self._snapshot(sid)[0][0]["inputs"])
        self.services["workspace_settings"].patch(sid, "text", result["text_settings_revision"],
            {"llm_tts_document_optimization": False})
        result = get_workflow_inputs(self.services, sid)
        self.assertEqual(optimized.id, result["consumers"]["generation"]["artifact"]["artifact_id"])
        settings = planning_settings(self.services["services"], sid, final_optimization=True)
        self.assertTrue(settings["llm_tts_document_optimization"])
        self.assertFalse(settings["llm_tts_optimization"])
        result = select_workflow_input(self.services, sid, "generation", "prepared_text", prepared.id,
            expected_outcome_revision=result["outcome_revision"], expected_text_settings_revision=result["text_settings_revision"],
            expected_selection_revision=self._expected_selection_revision(sid, "prepare_text"))
        self.assertFalse(result["transformations"]["llm_tts_document_optimization"])
        self.assertEqual(prepared.id, result["consumers"]["generation"]["artifact"]["artifact_id"])
        with self.assertRaisesRegex(ValueError, "Audiobook generation"):
            select_workflow_input(self.services, sid, "generation", "source", prepared.id,
                expected_outcome_revision=result["outcome_revision"], expected_selection_revision=0)

    def test_configuration_http_is_idempotent_and_validates_annotations(self):
        sid = self._session()
        endpoint = f"/api/v1/sessions/{sid}/workflow-inputs/speech-optimization"
        values = {"mode": "document", "expected_outcome_revision": 0, "expected_text_settings_revision": 0,
            "annotation_mode": "speakers", "annotation_only": True}
        headers = {"X-CSRF-Token": self.csrf, "Idempotency-Key": "speech-config-http-1"}
        invalid = self.client.post(endpoint, json={**values, "mode": "inline"}, headers=headers)
        self.assertEqual(422, invalid.status_code, invalid.get_json())
        response = self.client.post(endpoint, json=values, headers=headers)
        self.assertEqual(200, response.status_code, response.get_json())
        stable = self._speech_snapshot(sid)
        replay = self.client.post(endpoint, json=values, headers=headers)
        self.assertEqual(200, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(response.get_json(), replay.get_json())
        self.assertEqual(stable, self._speech_snapshot(sid))

    def test_audiobook_selection_rejects_changed_content_without_mutation(self):
        sid = self._session("audiobook")
        prepared = self._text_artifact(sid, "prepared_text")
        manifest = get_workflow_inputs(self.services, sid)
        before = self._speech_snapshot(sid)
        self.services["paths"].managed_path(prepared.relative_path).write_text("Changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "recorded content hash"):
            select_workflow_input(self.services, sid, "generation", "prepared_text", prepared.id,
                expected_outcome_revision=manifest["outcome_revision"], expected_text_settings_revision=manifest["text_settings_revision"],
                expected_selection_revision=self._expected_selection_revision(sid, "prepare_text"))
        self.assertEqual(before, self._speech_snapshot(sid))

    def test_disabled_consumers_do_not_add_input_blockers(self):
        sid = self._session()
        self.services["outcome_plans"].update(sid, 0, {
            "workflow_kind": "voiceover", "transformations": {"translate": False, "generate_audio": False},
            "deliverables": {}, "inputs": {"translation": "source", "generation": "source"},
        })
        manifest = get_workflow_inputs(self.services, sid)
        self.assertTrue(manifest["consumers"]["translation"]["blocking_reasons"])
        self.assertTrue(manifest["consumers"]["generation"]["blocking_reasons"])
        self.assertEqual([], manifest["blocking_reasons"])

    def test_audiobook_input_http_guards_text_revision_and_replays(self):
        sid = self._session("audiobook")
        prepared = self._text_artifact(sid, "prepared_text")
        body = {
            "consumer": "generation", "role": "prepared_text", "artifact_id": prepared.id,
            "expected_outcome_revision": 0, "expected_text_settings_revision": 0,
            "expected_selection_revision": self._expected_selection_revision(sid, "prepare_text"),
        }
        endpoint = f"/api/v1/sessions/{sid}/workflow-inputs"
        headers = {"X-CSRF-Token": self.csrf, "Idempotency-Key": "audiobook-input-http-1"}
        before = self._speech_snapshot(sid)
        stale = self.client.put(endpoint, json={**body, "expected_text_settings_revision": 99}, headers=headers)
        self.assertEqual(409, stale.status_code, stale.get_json())
        self.assertEqual(before, self._speech_snapshot(sid))
        response = self.client.put(endpoint, json=body, headers=headers)
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual(prepared.id, response.get_json()["selected"]["artifact_id"])
        stable = self._speech_snapshot(sid)
        replay = self.client.put(endpoint, json=body, headers=headers)
        self.assertEqual(200, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(response.get_json(), replay.get_json())
        self.assertEqual(stable, self._speech_snapshot(sid))


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
