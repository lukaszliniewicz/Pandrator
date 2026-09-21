import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from pandrator.web.audiobook_setup import (
    configure_audiobook_setup,
    get_audiobook_setup,
)
from pandrator.web.database import Database
from pandrator.web.models import (
    AppSetting,
    OutcomePlan,
    OutcomePlanHistory,
    SessionSetting,
    SessionSettingHistory,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workspace import (
    OutcomePlanService,
    RevisionConflict,
    WorkspaceSettingsService,
)
from tests.web_test_support import prepare_web_test_data_root


class AudiobookSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        paths = prepare_web_test_data_root(Path(self.temporary.name))
        self.database = Database(paths.database)
        self.settings = WorkspaceSettingsService(self.database)
        self.outcomes = OutcomePlanService(self.database)
        self.services = SimpleNamespace(
            workspace_settings=self.settings,
            outcome_plans=self.outcomes,
        )
        self.record = SessionService(self.database).create("Audiobook setup")

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def read(self):
        with self.database.session() as session:
            return get_audiobook_setup(self.services, session, self.record.id)

    def configure(self, mode, expected_revision=None):
        current = self.read() if expected_revision is None else None
        token = expected_revision or current["configuration_revision"]
        with self.database.immediate_session() as session:
            return configure_audiobook_setup(
                self.services,
                session,
                self.record.id,
                expected_revision=token,
                mode=mode,
            )

    def test_get_is_compact_and_does_not_create_legacy_outcome(self):
        setup = self.read()

        self.assertEqual(self.record.id, setup["session_id"])
        self.assertEqual("single_voice", setup["mode"])
        self.assertTrue(setup["configured"])
        self.assertEqual({"text": 0, "tts": 0, "outcome": 0}, setup["revisions"])
        self.assertEqual(
            {"service", "model", "voice", "language"}, setup["tts"].keys()
        )
        self.assertNotIn("effective", setup)
        self.assertGreater(setup["segmentation"]["target_chars"], 0)
        self.assertEqual("model", setup["segmentation"]["mode"])
        with self.database.session() as session:
            self.assertIsNone(session.get(OutcomePlan, self.record.id))

    def test_multi_voice_preserves_overrides_and_updates_existing_outcome(self):
        self.settings.update(
            self.record.id,
            "text",
            0,
            {
                "llm_tts_document_optimization": False,
                "llm_tts_annotation_mode": "off",
                "llm_tts_annotation_only": False,
                "selection_marker": "keep-text",
            },
        )
        self.settings.update(
            self.record.id,
            "tts",
            0,
            {
                "service": "audio_cpp",
                "model": "model-x",
                "voice": "voice-x",
                "performance_enabled": True,
                "casting_enabled": False,
            },
        )
        self.outcomes.update(
            self.record.id,
            0,
            {
                "version": 1,
                "workflow_kind": "audiobook",
                "transformations": {
                    "generate_audio": True,
                    "llm_tts_document_optimization": False,
                    "selection_marker": "keep-outcome",
                },
                "inputs": {"translation": "source", "generation": "source"},
            },
        )

        result = self.configure("multi_voice")

        self.assertEqual("multi_voice", result["mode"])
        self.assertTrue(result["configured"])
        self.assertEqual("model-x", result["tts"]["model"])
        self.assertEqual("voice-x", result["tts"]["voice"])
        self.assertTrue(result["casting_enabled"])
        self.assertEqual("speakers", result["annotation_mode"])
        self.assertTrue(result["annotation_only"])
        self.assertTrue(result["document_optimization_enabled"])
        with self.database.session() as session:
            text = session.get(SessionSetting, (self.record.id, "text"))
            tts = session.get(SessionSetting, (self.record.id, "tts"))
            outcome = session.get(OutcomePlan, self.record.id)
            self.assertEqual("keep-text", text.value_json["selection_marker"])
            self.assertTrue(text.value_json["llm_tts_document_optimization"])
            self.assertTrue(tts.value_json["performance_enabled"])
            self.assertEqual("model-x", tts.value_json["model"])
            self.assertEqual("voice-x", tts.value_json["voice"])
            self.assertEqual("keep-outcome", outcome.value_json["transformations"]["selection_marker"])
            self.assertTrue(
                outcome.value_json["transformations"]["llm_tts_document_optimization"]
            )
            self.assertEqual(2, outcome.revision)
            self.assertEqual(
                1,
                session.scalar(
                    select(func.count(OutcomePlanHistory.id)).where(
                        OutcomePlanHistory.session_id == self.record.id
                    )
                ),
            )

    def test_multi_voice_creates_missing_outcome_from_legacy_projection(self):
        result = self.configure("multi_voice")

        self.assertTrue(result["configured"])
        with self.database.session() as session:
            outcome = session.get(OutcomePlan, self.record.id)
            self.assertIsNotNone(outcome)
            self.assertEqual(1, outcome.revision)
            self.assertTrue(
                outcome.value_json["transformations"]["llm_tts_document_optimization"]
            )

    def test_multi_voice_does_not_copy_inherited_defaults_into_overrides(self):
        with self.database.session() as session:
            session.add(
                AppSetting(
                    key="defaults.text",
                    value_json={"inherited_marker": "text-default"},
                )
            )
            session.add(
                AppSetting(
                    key="defaults.tts",
                    value_json={
                        "performance_enabled": True,
                        "inherited_marker": "tts-default",
                    },
                )
            )

        self.configure("multi_voice")

        with self.database.session() as session:
            text = session.get(SessionSetting, (self.record.id, "text"))
            tts = session.get(SessionSetting, (self.record.id, "tts"))
            self.assertNotIn("inherited_marker", text.value_json)
            self.assertNotIn("inherited_marker", tts.value_json)
            self.assertNotIn("performance_enabled", tts.value_json)

    def test_single_voice_leaves_document_optimization_and_outcome_untouched(self):
        multi = self.configure("multi_voice")
        with self.database.session() as session:
            before = session.get(OutcomePlan, self.record.id)
            before_value = before.value_json
            before_revision = before.revision

        result = self.configure("single_voice", multi["configuration_revision"])

        self.assertEqual("single_voice", result["mode"])
        self.assertTrue(result["configured"])
        self.assertTrue(result["document_optimization_enabled"])
        self.assertEqual("off", result["annotation_mode"])
        self.assertFalse(result["annotation_only"])
        self.assertFalse(result["casting_enabled"])
        with self.database.session() as session:
            after = session.get(OutcomePlan, self.record.id)
            self.assertEqual(before_revision, after.revision)
            self.assertEqual(before_value, after.value_json)

    def test_same_configured_mode_is_a_no_op(self):
        first = self.configure("multi_voice")
        with self.database.session() as session:
            before_settings_history = session.scalar(
                select(func.count(SessionSettingHistory.id)).where(
                    SessionSettingHistory.session_id == self.record.id
                )
            )
            before_outcome_history = session.scalar(
                select(func.count(OutcomePlanHistory.id)).where(
                    OutcomePlanHistory.session_id == self.record.id
                )
            )

        second = self.configure("multi_voice", first["configuration_revision"])

        self.assertEqual(first, second)
        with self.database.session() as session:
            self.assertEqual(
                before_settings_history,
                session.scalar(
                    select(func.count(SessionSettingHistory.id)).where(
                        SessionSettingHistory.session_id == self.record.id
                    )
                ),
            )
            self.assertEqual(
                before_outcome_history,
                session.scalar(
                    select(func.count(OutcomePlanHistory.id)).where(
                        OutcomePlanHistory.session_id == self.record.id
                    )
                ),
            )

    def test_stale_configuration_revision_writes_nothing(self):
        stale = self.read()["configuration_revision"]
        self.settings.update(self.record.id, "text", 0, {"selection_marker": "changed"})

        with self.assertRaises(RevisionConflict):
            with self.database.immediate_session() as session:
                configure_audiobook_setup(
                    self.services,
                    session,
                    self.record.id,
                    expected_revision=stale,
                    mode="multi_voice",
                )
        with self.database.session() as session:
            self.assertIsNone(session.get(SessionSetting, (self.record.id, "tts")))
            self.assertIsNone(session.get(OutcomePlan, self.record.id))

    def test_other_workflow_is_rejected(self):
        other = SessionService(self.database).create(
            "Voiceover", workflow_kind="voiceover"
        )
        with self.database.session() as session:
            with self.assertRaises(ValueError):
                get_audiobook_setup(self.services, session, other.id)
            with self.assertRaises(ValueError):
                configure_audiobook_setup(
                    self.services,
                    session,
                    other.id,
                    expected_revision="0" * 64,
                    mode="single_voice",
                )


if __name__ == "__main__":
    unittest.main()
