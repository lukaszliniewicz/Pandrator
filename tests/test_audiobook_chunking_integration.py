import json
import re
import tempfile
import threading
import unittest
from unittest import mock

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.global_settings import GlobalSettingsService
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
        self.assertEqual(350, effective["max_sentence_length"])
        self.assertEqual("qwen3_tts", result["segmentation_budget"]["profile"])
        self.assertEqual(350, result["segmentation_budget"]["target_chars"])

    def test_prepare_text_uses_captured_sentence_packing_settings(self):
        self._register_clean_text("The captured packing policy must win.")
        workspace = WorkspaceSettingsService(self.database)
        saved = workspace.update(
            self.session.id,
            "text",
            0,
            {
                "audiobook_sentence_packing": "sentences",
                "audiobook_sentence_packing_threshold": 351,
            },
        )
        queued = WorkflowService(self.database, self.jobs).run_stage(
            self.session.id,
            "prepare_text",
        )
        payload = json.loads(json.dumps(queued.payload_json))

        workspace.update(
            self.session.id,
            "text",
            saved["revision"],
            {
                "audiobook_sentence_packing": "clauses",
                "audiobook_sentence_packing_threshold": 900,
            },
        )

        with mock.patch(
            "pandrator.logic.text_preprocessor.preprocess_text",
            return_value=[{"original_sentence": "The captured packing policy must win."}],
        ) as preprocess:
            self.handlers.prepare_text(
                payload,
                self.progress,
                threading.Event(),
            )

        effective = preprocess.call_args.args[1]
        self.assertEqual("sentences", effective["audiobook_sentence_packing"])
        self.assertEqual(351, effective["audiobook_sentence_packing_threshold"])

    def test_effective_sentence_packing_policy_reaches_real_preparation(self):
        first = "A lantern warmed the small kitchen while rain tapped on the old windows through the quiet evening."
        oversized = (
            "The travelers followed a narrow track through the valley, carrying provisions and maps while "
            "the last light faded behind the hills, and they continued carefully along the winding road "
            "as the evening air grew colder; after sunset they reached a village beside the river, where "
            "warm windows shone above the stone bridge and a welcoming host prepared a room before the "
            "heavy clouds gathered over the fields."
        )
        last = "The river passed beneath the stone bridge and disappeared beyond the fields."
        text = " ".join((first, oversized, last))
        source = self._register_clean_text(text, filename="sentence-packing.txt")
        base = {
            "audiobook_chunking": "model",
            # The effective Qwen target, rather than this ignored manual value,
            # must determine when automatic sentence packing takes effect.
            "max_sentence_length": 900,
            "enable_sentence_appending": True,
            "enable_nemo_normalization": False,
            "normalize_all_caps": False,
            "_audiobook_tts_settings": self._qwen_settings(language="en"),
        }
        for policy, threshold, isolated in (
            ("auto", 350, True),
            ("auto", 351, False),
            ("sentences", 900, True),
            ("clauses", 1, False),
        ):
            with self.subTest(policy=policy, threshold=threshold):
                result, _artifact, rows = self._prepare(source, {
                    **base,
                    "audiobook_sentence_packing": policy,
                    "audiobook_sentence_packing_threshold": threshold,
                })
                blocks = [row["original_sentence"] for row in rows]
                self.assertEqual(350, result["segmentation_budget"]["target_chars"])
                self.assertTrue(all(len(block) <= 350 for block in blocks))
                self.assertEqual(self._words(text), self._words(" ".join(blocks)))
                self.assertEqual("yes", rows[-1]["paragraph"])
                if isolated:
                    self.assertEqual(first, blocks[0])
                    self.assertEqual(last, blocks[-1])
                    self.assertTrue(all(row.get("split_part") is not None for row in rows[1:-1]))
                else:
                    self.assertEqual(2, len(blocks))
                    self.assertTrue(blocks[0].startswith(first + " "))
                    self.assertTrue(blocks[-1].endswith(" " + last))

    def test_model_budget_groups_paragraphs_and_manual_mode_keeps_two_hundred(self):
        sentence = (
            "Every evening, the quiet reader follows the narrator through the village, "
            "where familiar voices gather around the kitchen fire."
        )
        self.assertGreaterEqual(len(sentence), 120)
        self.assertLessEqual(len(sentence), 145)
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
        self.assertEqual(350, model_result["segmentation_budget"]["target_chars"])
        self.assertEqual(8, len(model_rows))
        self.assertGreater(min(model_lengths), 200)
        self.assertLessEqual(max(model_lengths), 350)
        self.assertEqual(
            ["no", "no", "no", "yes"] * 2,
            [row["paragraph"] for row in model_rows],
        )
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
        self.assertEqual(16, len(manual_rows))
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

    def test_dialogue_flow_default_off_switch_and_casting_are_applied(self):
        source = self._register_clean_text(
            '"Where are you going?"\n\n"To the old house, before sunset."',
            filename="short-exchange.txt",
        )
        base = {
            "enable_nemo_normalization": False,
            "remove_quotation_marks": False,
            "remove_diacritics": False,
            "normalize_all_caps": False,
            "_audiobook_tts_settings": self._qwen_settings(language="en"),
        }
        _result, _artifact, grouped = self._prepare(source, base)
        self.assertEqual(1, len(grouped))
        self.assertEqual(2, grouped[0]["dialogue_flow"]["paragraph_count"])
        self.assertEqual("paragraph", grouped[0]["speech_boundary_after"])
        self.assertIn('\n\n', grouped[0]["original_sentence"])

        _result, _artifact, disabled = self._prepare(
            source, {**base, "audiobook_dialogue_flow": "off"}
        )
        self.assertEqual(2, len(disabled))
        self.assertTrue(all("dialogue_flow" not in row for row in disabled))

        _result, _artifact, cast = self._prepare(
            source,
            {
                **base,
                "_audiobook_tts_settings": {
                    **base["_audiobook_tts_settings"],
                    "casting_enabled": True,
                },
            },
        )
        self.assertEqual(2, len(cast))
        self.assertEqual("dialogue_turn", cast[0]["speech_boundary_after"])
        self.assertEqual("paragraph", cast[1]["speech_boundary_after"])

    def test_voiceover_never_opts_into_audiobook_preprocessing(self):
        self.session = self.sessions.create(
            "Timed workflow guard", workflow_kind="voiceover", source_language="en"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir(parents=True, exist_ok=True)
        source = self._register_clean_text('"First turn."\n\n"Second turn."')
        with mock.patch(
            "pandrator.logic.text_preprocessor.preprocess_text",
            return_value=[{"original_sentence": "Voiceover fixture."}],
        ) as preprocess:
            self._prepare(source, {"enable_nemo_normalization": False})
        self.assertIs(False, preprocess.call_args.args[1]["audiobook_preparation"])

    def test_saved_dialogue_flow_rejects_unknown_modes(self):
        workspace = WorkspaceSettingsService(self.database)
        defaults = GlobalSettingsService(self.database, self.paths)
        for mode in ("english", True, None):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                ValueError, "audiobook_dialogue_flow must be auto or off"
            ):
                workspace.update(
                    self.session.id, "text", 0, {"audiobook_dialogue_flow": mode}
                )
            with self.subTest(global_mode=mode), self.assertRaisesRegex(
                ValueError, "audiobook_dialogue_flow must be auto or off"
            ):
                defaults.replace("defaults.text", {"audiobook_dialogue_flow": mode}, "*")


if __name__ == "__main__":
    unittest.main()
