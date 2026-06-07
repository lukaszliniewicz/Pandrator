import json
import threading
import unittest
from types import SimpleNamespace
from pandrator.app_state import AppState, TextProcessingSettings, TTSSettings
from pandrator.app_logic import AppLogic


class DummySignal:
    def __init__(self):
        self.emitted = False
        self.args = []

    def emit(self, *args):
        self.emitted = True
        self.args = list(args)


class LogicTestHarness:
    def __init__(self):
        self._state_lock = threading.RLock()
        self.state = AppState()
        self.progress_updated = DummySignal()
        self.state_changed = DummySignal()

    def get_processed_sentences_snapshot(self):
        return AppLogic.get_processed_sentences_snapshot(self)

    def _set_processed_sentences_snapshot(self, sentences, persist=True):
        self.state.processed_sentences = sentences

    def _clear_audio_variants(self):
        pass

    def _get_candidate_sentence_wavs_dirs(self):
        return []

    def has_any_generation_progress(self):
        return AppLogic.has_any_generation_progress(self)

    def _have_preprocessing_settings_changed(self):
        return AppLogic._have_preprocessing_settings_changed(self)

    def _reset_generation_progress(self):
        return AppLogic._reset_generation_progress(self)


class GenerationAnewTests(unittest.TestCase):
    def test_has_any_generation_progress(self):
        harness = LogicTestHarness()
        self.assertFalse(harness.has_any_generation_progress())

        harness.state.processed_sentences = [
            {"sentence_number": "1", "tts_generated": "no"},
            {"sentence_number": "2", "tts_generated": "no"},
        ]
        self.assertFalse(harness.has_any_generation_progress())

        harness.state.processed_sentences = [
            {"sentence_number": "1", "tts_generated": "yes"},
            {"sentence_number": "2", "tts_generated": "no"},
        ]
        self.assertTrue(harness.has_any_generation_progress())

    def test_have_preprocessing_settings_changed_no_metadata(self):
        harness = LogicTestHarness()
        harness.state.metadata = {}
        # No metadata means settings are treated as changed
        self.assertTrue(harness._have_preprocessing_settings_changed())

    def test_have_preprocessing_settings_changed_matching(self):
        harness = LogicTestHarness()
        settings = {
            "pdf_preprocessed": False,
            "source_file": "",
            "disable_paragraph_detection": False,
            "language": "en",
            "max_sentence_length": 160,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "tts_service": "XTTS",
            "remove_footnotes": False,
            "filter_citations": True
        }
        harness.state.metadata = {"preprocessing_settings": json.dumps(settings)}
        self.assertFalse(harness._have_preprocessing_settings_changed())

    def test_have_preprocessing_settings_changed_not_matching(self):
        harness = LogicTestHarness()
        settings = {
            "pdf_preprocessed": False,
            "source_file": "",
            "disable_paragraph_detection": False,
            "language": "en",
            "max_sentence_length": 160,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "tts_service": "XTTS",
            "remove_footnotes": False,
            "filter_citations": True
        }
        harness.state.metadata = {"preprocessing_settings": json.dumps(settings)}

        # Change a setting
        harness.state.tts.language = "fr"
        self.assertTrue(harness._have_preprocessing_settings_changed())

    def test_reset_generation_progress_clears_if_settings_changed(self):
        harness = LogicTestHarness()
        harness.state.processed_sentences = [
            {"sentence_number": "1", "tts_generated": "yes"},
        ]
        # settings differ because metadata is missing
        harness.state.metadata = {}

        result = harness._reset_generation_progress()
        self.assertTrue(result)
        self.assertEqual(harness.state.processed_sentences, [])
        self.assertTrue(harness.progress_updated.emitted)
        self.assertEqual(harness.progress_updated.args, [0, 1, 0.0])

    def test_reset_generation_progress_resets_flags_if_settings_not_changed(self):
        harness = LogicTestHarness()
        harness.state.processed_sentences = [
            {"sentence_number": "1", "tts_generated": "yes", "processed_sentence": "Modified text"},
        ]
        settings = {
            "pdf_preprocessed": False,
            "source_file": "",
            "disable_paragraph_detection": False,
            "language": "en",
            "max_sentence_length": 160,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "tts_service": "XTTS",
            "remove_footnotes": False,
            "filter_citations": True
        }
        harness.state.metadata = {"preprocessing_settings": json.dumps(settings)}

        result = harness._reset_generation_progress()
        self.assertTrue(result)
        self.assertEqual(len(harness.state.processed_sentences), 1)
        self.assertEqual(harness.state.processed_sentences[0]["tts_generated"], "no")
        self.assertNotIn("processed_sentence", harness.state.processed_sentences[0])
        self.assertTrue(harness.progress_updated.emitted)
        self.assertEqual(harness.progress_updated.args, [0, 1, 0.0])


if __name__ == "__main__":
    unittest.main()
