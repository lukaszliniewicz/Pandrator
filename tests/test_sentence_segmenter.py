import unittest
from unittest.mock import MagicMock, patch

from pandrator.logic import sentence_segmenter


class SentenceSegmenterTests(unittest.TestCase):
    def setUp(self):
        sentence_segmenter._SEGMENTER = None
        sentence_segmenter._SEGMENTER_FAILED = False

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


if __name__ == "__main__":
    unittest.main()
