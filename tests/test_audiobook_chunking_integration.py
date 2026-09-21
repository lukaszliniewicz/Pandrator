import json
import re
import tempfile
import threading
import unittest
from unittest import mock

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root


class AudiobookChunkingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.jobs = JobQueue(self.database)
        self.session = self.sessions.create(
            "Audiobook chunking integration",
            source_language="en",
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    @staticmethod
    def progress(_value, _detail=None):
        return None

    def _register_clean_text(
        self,
        text: str,
        *,
        filename: str = "cleaned.txt",
        metadata: dict | None = None,
    ):
        path = self.session_dir / filename
        path.write_text(text, encoding="utf-8")
        return self.artifacts.register(
            path,
            kind="text",
            role="clean_text",
            session_id=self.session.id,
            metadata=metadata,
        )

    def _prepare(self, source, settings: dict):
        result = self.handlers.prepare_text(
            {
                "session_id": self.session.id,
                "source_artifact_id": source.id,
                "settings": settings,
            },
            self.progress,
            threading.Event(),
        )
        artifact, path = self.artifacts.resolve(result["artifact_id"])
        return result, artifact, json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _words(text: str) -> list[str]:
        return re.findall(r"[A-Za-z]+", text)

    @staticmethod
    def _qwen_settings(language: str = "ja") -> dict:
        return {
            "service": "audio_cpp",
            "model": "qwen3_tts_1_7b_base_q8_0",
            "language": language,
            "audio_cpp_model_settings": {
                "qwen3_tts_1_7b_base_q8_0": {"max_tokens": 2048}
            },
        }

    def test_run_stage_captures_qwen_before_session_provider_switch(self):
        self._register_clean_text("The captured provider must win.")
        workspace = WorkspaceSettingsService(self.database)
        saved = workspace.update(
            self.session.id,
            "tts",
            0,
            self._qwen_settings(),
        )

        queued = WorkflowService(self.database, self.jobs).run_stage(
            self.session.id,
            "prepare_text",
        )
        payload = json.loads(json.dumps(queued.payload_json))
        captured = payload["settings"]["_audiobook_tts_settings"]
        self.assertEqual("audio_cpp", captured["service"])
        self.assertIn("qwen", captured["model"].lower())
        self.assertNotIn("service", payload["settings"])
        self.assertNotIn("model", payload["settings"])
        self.assertNotIn("language", payload["settings"])

        workspace.update(
            self.session.id,
            "tts",
            saved["revision"],
            {"service": "XTTS", "model": "xtts_v2", "language": "en"},
        )

        with mock.patch(
            "pandrator.logic.text_preprocessor.preprocess_text",
            return_value=[{"original_sentence": "The captured provider must win."}],
        ) as preprocess:
            result = self.handlers.prepare_text(
                payload,
                self.progress,
                threading.Event(),
            )

        effective = preprocess.call_args.args[1]
        self.assertEqual("en", effective["language"])
        self.assertEqual("XTTS", effective["tts_service"])
        self.assertEqual(1800, effective["max_sentence_length"])
        self.assertEqual("qwen3_tts", result["segmentation_budget"]["profile"])
        self.assertEqual(1800, result["segmentation_budget"]["target_chars"])

    def test_model_budget_groups_paragraphs_and_manual_mode_keeps_two_hundred(self):
        sentence = (
            "The narrator carries the story through a quiet room while every "
            "careful phrase settles gently around the listening reader steady "
            "steady steady steady steady steady steady steady."
        )
        text = "\n\n".join((" ".join([sentence] * 8),) * 2)
        source = self._register_clean_text(text, filename="long-paragraphs.txt")
        base_settings = {
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "enable_nemo_normalization": False,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "normalize_all_caps": False,
            "_audiobook_tts_settings": self._qwen_settings(language="en"),
        }

        model_result, _model_artifact, model_rows = self._prepare(
            source,
            {**base_settings, "audiobook_chunking": "model"},
        )
        model_lengths = [len(row["original_sentence"]) for row in model_rows]
        self.assertEqual(1800, model_result["segmentation_budget"]["target_chars"])
        self.assertEqual(2, len(model_rows))
        self.assertGreater(min(model_lengths), 200)
        self.assertLessEqual(max(model_lengths), 1800)
        self.assertEqual(["yes", "yes"], [row["paragraph"] for row in model_rows])
        self.assertEqual(
            self._words(text),
            self._words(" ".join(row["original_sentence"] for row in model_rows)),
        )

        manual_result, _manual_artifact, manual_rows = self._prepare(
            source,
            {
                **base_settings,
                "audiobook_chunking": "manual",
                "max_sentence_length": 200,
            },
        )
        manual_lengths = [len(row["original_sentence"]) for row in manual_rows]
        self.assertEqual(200, manual_result["segmentation_budget"]["target_chars"])
        self.assertGreater(len(manual_rows), len(model_rows))
        self.assertLessEqual(max(manual_lengths), 200)
        self.assertEqual(
            self._words(text),
            self._words(" ".join(row["original_sentence"] for row in manual_rows)),
        )

    def test_annotated_markup_topology_is_unchanged_and_policy_is_recorded(self):
        first_xml = (
            '<segment id="1" boundary_after="paragraph">'
            "<dialogue>First annotated sentence.</dialogue></segment>"
        )
        second_xml = (
            '<segment id="2"><dialogue>Second annotated sentence.</dialogue>'
            "</segment>"
        )
        source = self._register_clean_text(
            "First annotated sentence. Second annotated sentence.",
            filename="annotated.txt",
            metadata={"speech_markup": {"1": first_xml, "2": second_xml}},
        )

        result, artifact, rows = self._prepare(
            source,
            {
                "audiobook_chunking": "model",
                "_audiobook_tts_settings": self._qwen_settings(language="en"),
            },
        )

        segmentation = artifact.metadata_json["segmentation"]
        self.assertFalse(segmentation["policy_applied"])
        self.assertIn("immutable", segmentation["policy_explanation"])
        self.assertFalse(result["segmentation"]["policy_applied"])
        self.assertEqual(
            [first_xml, second_xml],
            [row["speech_plan"]["speech_xml"] for row in rows],
        )
        self.assertEqual(
            ["paragraph", None],
            [row["speech_boundary_after"] for row in rows],
        )
        self.assertEqual([True, False], [row["paragraph_break_after"] for row in rows])


if __name__ == "__main__":
    unittest.main()
