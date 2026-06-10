import unittest
from pandrator.logic.text_preprocessor import normalize_punctuation, preprocess_text

class TextPreprocessorTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
