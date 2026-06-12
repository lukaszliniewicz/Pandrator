import unittest
from unittest.mock import MagicMock, patch

from pandrator.logic import nemo_normalizer


class NeMoNormalizerTests(unittest.TestCase):
    def test_supported_language_aliases(self):
        self.assertEqual(nemo_normalizer.normalize_nemo_language("en-US"), "en")
        self.assertEqual(nemo_normalizer.normalize_nemo_language("English"), "en")
        self.assertIsNone(nemo_normalizer.normalize_nemo_language("zh-cn"))
        self.assertIsNone(nemo_normalizer.normalize_nemo_language("vi"))
        self.assertEqual(nemo_normalizer.normalize_nemo_language("German (v3)"), "de")
        self.assertIsNone(nemo_normalizer.normalize_nemo_language("pl"))
        self.assertIsNone(nemo_normalizer.normalize_nemo_language("ru"))

    @patch("pandrator.logic.nemo_normalizer._get_normalizer")
    def test_normalization_preserves_chapter_markers_and_newlines(self, get_normalizer):
        normalizer = MagicMock()
        normalizer.normalize.side_effect = lambda text, **_kwargs: text.replace("2", "two").replace("5", "five")
        get_normalizer.return_value = normalizer

        result = nemo_normalizer.normalize_text_for_tts("[[Chapter]]Chapter 2\nPay 5 dollars.", "en")

        self.assertEqual(result, "[[Chapter]]Chapter two\nPay five dollars.")
        self.assertEqual(normalizer.normalize.call_count, 2)

    @patch("pandrator.logic.nemo_normalizer._get_normalizer")
    def test_unsupported_language_is_a_noop(self, get_normalizer):
        text = "Rozdzial 2."

        self.assertEqual(nemo_normalizer.normalize_text_for_tts(text, "pl"), text)
        get_normalizer.assert_not_called()

    @patch("pandrator.logic.nemo_normalizer._get_normalizer")
    def test_internal_token_markup_falls_back_to_source_text(self, get_normalizer):
        normalizer = MagicMock()
        normalizer.normalize.return_value = 'tokens { cardinal { integer: "twelve" } }'
        get_normalizer.return_value = normalizer

        self.assertEqual(nemo_normalizer.normalize_text_for_tts("12", "en"), "12")


if __name__ == "__main__":
    unittest.main()
