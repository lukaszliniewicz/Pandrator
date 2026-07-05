import unittest
from unittest.mock import patch
from pandrator.logic.text_preprocessor import (
    CHUNK_SIZE,
    find_best_split_index,
    normalize_punctuation,
    preprocess_text,
    split_into_sentences,
)

class TextPreprocessorTests(unittest.TestCase):
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_wtpsplit_is_primary_sentence_segmenter(self, split_text):
        split_text.return_value = ["See Sec. IV, Ch. IX, and pp. 12-14.", "Then continue."]

        result = split_into_sentences(
            "See Sec. IV, Ch. IX, and pp. 12-14. Then continue.",
            "en",
            "XTTS",
        )

        self.assertEqual(result, split_text.return_value)

    @patch("pandrator.logic.text_preprocessor._split_with_sentence_splitter")
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text", return_value=None)
    def test_rule_based_segmenter_is_used_as_wtpsplit_fallback(self, _split_text, legacy_split):
        legacy_split.return_value = ["One.", "Two."]

        result = split_into_sentences("One. Two.", "en", "XTTS")

        self.assertEqual(result, ["One.", "Two."])
        legacy_split.assert_called_once_with("One. Two.", "en")

    @patch("pandrator.logic.text_preprocessor._sequential_preprocess_text", return_value=[])
    @patch("pandrator.logic.text_preprocessor._parallel_preprocess_text")
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.is_available", return_value=True)
    def test_large_documents_avoid_loading_wtpsplit_in_worker_processes(
        self,
        _is_available,
        parallel_preprocess,
        sequential_preprocess,
    ):
        settings = {"enable_nemo_normalization": False}

        preprocess_text("a" * (CHUNK_SIZE + 1), settings)

        sequential_preprocess.assert_called_once()
        parallel_preprocess.assert_not_called()

    def test_normalize_punctuation_fancy_quotes(self):
        text = "“Hello,” she said, ‘It’s a nice day.’ «Bonjour»"
        normalized = normalize_punctuation(text)
        self.assertEqual(normalized, '"Hello," she said, \'It\'s a nice day.\' "Bonjour"')

    def test_normalize_punctuation_dashes(self):
        text = "Scrooge said that he would see him—yes, indeed–he did."
        normalized = normalize_punctuation(text)
        self.assertEqual(normalized, "Scrooge said that he would see him, yes, indeed - he did.")

    def test_normalize_punctuation_ellipsis(self):
        text = "To be continued…"
        normalized = normalize_punctuation(text)
        self.assertEqual(normalized, "To be continued...")

    def test_preprocessor_pipeline_integration(self):
        settings = {
            "pdf_preprocessed": False,
            "source_file": "",
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "tts_service": "XTTS"
        }
        text = "This is a test—with a dash. And “fancy quotes”."
        sentences = preprocess_text(text, settings)
        
        self.assertEqual(len(sentences), 2)
        self.assertEqual(sentences[0]["original_sentence"], "This is a test, with a dash.")
        self.assertEqual(sentences[1]["original_sentence"], 'And "fancy quotes".')

    @patch("pandrator.logic.text_preprocessor.nemo_normalizer.normalize_text_for_tts")
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_nemo_normalization_runs_before_sentence_splitting(self, split_text, normalize_text):
        normalize_text.return_value = "It costs twelve dollars. Continue."
        split_text.return_value = ["It costs twelve dollars.", "Continue."]
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": True,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text("It costs $12. Continue.", settings)

        normalize_text.assert_called_once_with("It costs $12. Continue.", "en")
        split_text.assert_called_once_with("It costs twelve dollars. Continue.")
        self.assertEqual(sentences[0]["original_sentence"], "It costs twelve dollars.")

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_chapter_markers_are_not_sent_to_sentence_segmenter(self, split_text):
        seen_texts = []

        def fake_split(text):
            seen_texts.append(text)
            return [text.strip()]

        split_text.side_effect = fake_split
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text(
            "[[Chapter]]By CHARLES DICKENS\n\nThis is body text.",
            settings,
        )

        self.assertTrue(seen_texts)
        self.assertTrue(all("[[Chapter]]" not in text for text in seen_texts))
        self.assertEqual(sentences[0]["original_sentence"], "By CHARLES DICKENS.")
        self.assertEqual(sentences[0]["chapter"], "yes")
        self.assertEqual(sentences[1]["original_sentence"], "This is body text.")
        self.assertEqual(sentences[1]["chapter"], "no")

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_japanese_terminal_punctuation_does_not_get_ascii_period(self, split_text):
        seen_texts = []

        def fake_split(text):
            seen_texts.append(text)
            return [text.strip()]

        split_text.side_effect = fake_split
        settings = {
            "disable_paragraph_detection": True,
            "language": "ja",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "Kokoro",
        }

        preprocess_text("これはテストです。\n\n次の行です！\n\n質問です？\n\n全角です．", settings)

        self.assertEqual(
            seen_texts,
            ["これはテストです。\n\n次の行です！\n\n質問です？\n\n全角です．"],
        )
        self.assertTrue(all("。." not in text for text in seen_texts))
        self.assertTrue(all("！." not in text for text in seen_texts))
        self.assertTrue(all("？." not in text for text in seen_texts))
        self.assertTrue(all("．." not in text for text in seen_texts))

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_adjacent_chapter_markers_merge_without_segmenter_damage(self, split_text):
        split_text.return_value = ["Body starts here."]
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text(
            "[[Chapter]]STAVE ONE\n\n[[Chapter]]MARLEY'S GHOST\n\nBody starts here.",
            settings,
        )

        self.assertEqual(sentences[0]["original_sentence"], "STAVE ONE. MARLEY'S GHOST.")
        self.assertEqual(sentences[0]["chapter"], "yes")
        split_text.assert_called_once_with("Body starts here.")

    def test_conjunction_fallback_chooses_balanced_split(self):
        text = (
            "This is a long clause without commas and it keeps going because the author wanted "
            "the rhythm to continue and the fallback should probably find a conjunction or another "
            "soft boundary before falling back to an arbitrary word boundary near the hard limit."
        )

        split_index = find_best_split_index(text, "en", 200)

        self.assertIsNotNone(split_index)
        self.assertGreater(split_index, 80)
        self.assertLessEqual(split_index, 200)

if __name__ == "__main__":
    unittest.main()
