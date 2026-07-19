import unittest
from unittest.mock import MagicMock, patch

from pandrator.logic import sentence_segmenter


class SentenceSegmenterTests(unittest.TestCase):
    def setUp(self):
        sentence_segmenter._SEGMENTER = None
        sentence_segmenter._SEGMENTER_FAILED = False

    def test_default_model_is_high_quality_sentence_model(self):
        self.assertEqual(sentence_segmenter.WTPSPLIT_MODEL, "sat-12l-sm")

    @patch("pandrator.logic.sentence_segmenter._create_segmenter")
    def test_segments_paragraphs_and_trims_boundary_whitespace(self, create_segmenter):
        segmenter = MagicMock()
        segmenter.split.side_effect = [
            ["Dr. Smith spoke. ", "Then he left."],
            ["Prof. Jones stayed. "],
        ]
        create_segmenter.return_value = segmenter

        result = sentence_segmenter.split_text(
            "Dr. Smith spoke. Then he left.\n\nProf. Jones stayed."
        )

        self.assertEqual(
            result,
            ["Dr. Smith spoke.", "Then he left.", "Prof. Jones stayed."],
        )
        self.assertEqual(segmenter.split.call_count, 2)
        segmenter.split.assert_any_call(
            "Dr. Smith spoke. Then he left.",
            threshold=0.05,
            stride=128,
            block_size=256,
            weighting="hat",
            treat_newline_as_space=True,
        )

    @patch("pandrator.logic.sentence_segmenter._create_segmenter", side_effect=RuntimeError("missing"))
    def test_returns_none_when_runtime_is_unavailable(self, _create_segmenter):
        self.assertIsNone(sentence_segmenter.split_text("One. Two."))
        self.assertFalse(sentence_segmenter.is_available())

    @patch("pandrator.logic.sentence_segmenter._create_segmenter")
    def test_predict_boundaries_preserves_probabilities_and_character_indices(self, create_segmenter):
        segmenter = MagicMock()
        segmenter.predict_proba.return_value = [0.01, 0.8, 0.2, 0.9]
        create_segmenter.return_value = segmenter

        result = sentence_segmenter.predict_boundaries("A. B", threshold=0.25)

        self.assertEqual(result["probabilities"], [0.01, 0.8, 0.2, 0.9])
        self.assertEqual(
            result["boundaries"],
            [{"index": 1, "probability": 0.8}, {"index": 3, "probability": 0.9}],
        )
        segmenter.predict_proba.assert_called_once_with(
            "A. B",
            stride=128,
            block_size=256,
            weighting="hat",
        )

    @patch("pandrator.logic.sentence_segmenter._create_segmenter")
    def test_predict_boundaries_returns_none_when_prediction_fails(self, create_segmenter):
        segmenter = MagicMock()
        segmenter.predict_proba.side_effect = RuntimeError("bad model")
        create_segmenter.return_value = segmenter

        self.assertIsNone(sentence_segmenter.predict_boundaries("A. B"))


if __name__ == "__main__":
    unittest.main()
