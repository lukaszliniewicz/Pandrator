"""Legacy workflow input defaults are shared by read and execution paths."""

from __future__ import annotations

import tempfile
import unittest
import uuid
import wave

from pandrator.web.api import create_app
from pandrator.web.artifact_selection import choose_artifact
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, OutcomePlan, SessionRecord
from pandrator.web.outcome_plans import resolve_workflow_inputs
from pandrator.web.speech_plan_workspace import speech_plan_status
from pandrator.web.workflow_inputs import get_workflow_inputs
from pandrator.web.workflow_prerequisites import WorkflowPrerequisiteService


class WorkflowInputDefaultsTests(unittest.TestCase):
    @staticmethod
    def _record(
        included_stages: list[str], workflow_kind: str = "voiceover"
    ) -> SessionRecord:
        return SessionRecord(
            id=uuid.uuid4().hex,
            name="Workflow input defaults",
            workflow_kind=workflow_kind,
            workflow_preset="custom",
            included_stages_json=included_stages,
        )

    def test_legacy_input_defaults_follow_included_stages(self) -> None:
        cases = (
            ([], {"generation": "source", "translation": "source"}),
            (
                ["correct"],
                {"generation": "correction", "translation": "correction"},
            ),
            (
                ["translate"],
                {"generation": "translation", "translation": "source"},
            ),
            (
                ["correct", "translate"],
                {"generation": "translation", "translation": "correction"},
            ),
        )
        for included, expected in cases:
            with self.subTest(included=included):
                self.assertEqual(expected, resolve_workflow_inputs(self._record(included), None))

    def test_explicit_choices_normalize_and_invalid_roles_fall_back(self) -> None:
        record = self._record(["correct", "translate"])
        self.assertEqual(
            {"generation": "correction", "translation": "source"},
            resolve_workflow_inputs(
                record,
                {"inputs": {"generation": "  CORRECTION ", "translation": " SOURCE "}},
            ),
        )
        self.assertEqual(
            {"generation": "translation", "translation": "correction"},
            resolve_workflow_inputs(
                record,
                {"inputs": {"generation": "prepared_text", "translation": "bad-role"}},
            ),
        )

    def test_translation_consumer_cannot_select_translation(self) -> None:
        self.assertEqual(
            {"generation": "source", "translation": "source"},
            resolve_workflow_inputs(
                self._record([]), {"inputs": {"translation": "translation"}}
            ),
        )

    def test_media_edit_source_choice_is_canonicalized(self) -> None:
        self.assertEqual(
            {"generation": "media_edit", "translation": "media_edit"},
            resolve_workflow_inputs(
                self._record([], "media_edit"),
                {"inputs": {"generation": "source", "translation": "source"}},
            ),
        )

    def test_continuation_uses_source_without_unneeded_text_stages(self) -> None:
        inputs = resolve_workflow_inputs(self._record([]), None)
        self.assertEqual(
            ("transcription", "upload"),
            WorkflowPrerequisiteService._continuation_input_roles(
                "generate_audio", (), "voiceover", inputs, {}
            ),
        )
        self.assertEqual(
            {"generate_audio"},
            WorkflowPrerequisiteService._continuation_required_stages(
                "voiceover", "generate_audio", True, inputs, {}
            ),
        )

    def test_explicit_translation_generation_still_requires_translation(self) -> None:
        inputs = resolve_workflow_inputs(
            self._record([]), {"inputs": {"generation": "translation"}}
        )
        self.assertEqual(
            ("translation",),
            WorkflowPrerequisiteService._continuation_input_roles(
                "generate_audio", (), "voiceover", inputs, {}
            ),
        )
        self.assertEqual(
            {"generate_audio", "translate"},
            WorkflowPrerequisiteService._continuation_required_stages(
                "voiceover", "generate_audio", True, inputs, {}
            ),
        )


class WorkflowInputDefaultsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.services = self.app.extensions["pandrator"]["services"]
        self.addCleanup(self.services.database.dispose)

    def test_selected_transcription_is_shared_without_persisted_generation_choice(self) -> None:
        session_id = self.services.sessions.create(
            f"Legacy voiceover input {uuid.uuid4().hex[:8]}",
            workflow_kind="voiceover",
            included_stages=["transcribe", "generate_audio"],
        ).id
        wav_path = self.services.paths.uploads / f"{uuid.uuid4().hex}-source.wav"
        with wave.open(str(wav_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 800)
        source = self.services.artifacts.register(
            wav_path,
            kind="wav",
            role="upload",
            session_id=session_id,
            metadata={"original_filename": wav_path.name},
        )
        source_asset = self.services.source_library.ensure_for_artifact(
            source.id, display_name="source.wav", kind="wav"
        )
        self.services.source_library.attach(session_id, source_asset.id)

        transcription_path = self.services.paths.uploads / f"{uuid.uuid4().hex}-transcription.srt"
        transcription_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSelected transcription.\n",
            encoding="utf-8",
        )
        transcription = self.services.artifacts.register(
            transcription_path,
            kind="srt",
            role="transcription",
            session_id=session_id,
            parent_ids=[source.id],
            metadata={"original_filename": transcription_path.name},
        )
        with self.services.database.immediate_session() as session:
            choose_artifact(session, session_id, "transcribe", transcription.id)
            plan = session.get(OutcomePlan, session_id)
            if plan is None:
                plan = OutcomePlan(session_id=session_id, value_json={})
                session.add(plan)
            plan.value_json = {**dict(plan.value_json or {}), "inputs": {}}

        manifest = get_workflow_inputs(self.services, session_id)
        workflow = self.services.workflows.snapshot(session_id)
        generation_stage = next(
            item for item in workflow["stages"] if item["key"] == "generate_audio"
        )
        status = speech_plan_status(self.services, session_id)

        self.assertEqual("source", manifest["inputs"]["generation"])
        self.assertEqual(
            transcription.id,
            manifest["consumers"]["generation"]["artifact"]["artifact_id"],
        )
        self.assertEqual(transcription.id, generation_stage["resolved_input"]["artifact_id"])
        self.assertEqual(transcription.id, status["current_input"]["artifact_id"])
        self.assertEqual("transcription", status["current_input"]["role"])
        self.assertTrue(status["can_prepare"])
        with self.services.database.snapshot_session() as session:
            stored_plan = session.get(OutcomePlan, session_id)
            self.assertEqual({}, stored_plan.value_json["inputs"])
            stored_source = session.get(Artifact, source.id)
            self.assertEqual("upload", stored_source.role)
