"""Deterministic regression cases from the subtitle-first speech-plan review."""

import unittest

from pandrator.logic.dubbing.speech_blocks import create_speech_blocks


def subtitles(*cues):
    return "\n\n".join(f"{index}\n{start} --> {end}\n{text}" for index, (start, end, text) in enumerate(cues, 1))


class SpeechBlockProsodyRegressionTests(unittest.TestCase):
    def test_complete_sentence_preferred_over_appositional_comma(self):
        cases = [
            ("en", "A complete opening thought.", "These meetings, these interfaith circles can offer real value."),
            ("de", "Ein vollständiger Gedanke.", "Diese Treffen, diese interreligiösen Kreise sind wichtig."),
            ("es", "Una idea completa.", "Estos encuentros, estos círculos interreligiosos son importantes."),
        ]
        for language, first, second in cases:
            with self.subTest(language=language):
                text = f"{first} {second}"
                blocks = create_speech_blocks(subtitles(("00:00:00,000", "00:00:10,000", text)), target_language=language, max_chars=70)
                self.assertEqual([block["text"] for block in blocks], [first, second])
                self.assertTrue(all(len(block["text"]) <= 70 for block in blocks))

    def test_unfinished_sentence_survives_2360ms_pause_within_hard_guard(self):
        content = subtitles(
            ("00:00:00,000", "00:00:02,000", "Something that can help"),
            ("00:00:04,360", "00:00:07,000", "in every aspect of living here."),
        )
        joined = create_speech_blocks(content, continuation_threshold_ms=3000, max_internal_gap_ms=4000)
        guarded = create_speech_blocks(content, continuation_threshold_ms=3000, max_internal_gap_ms=1800)
        self.assertEqual([block["text"] for block in joined], ["Something that can help in every aspect of living here."])
        self.assertEqual(len(guarded), 2)

    def test_near_capacity_complete_thoughts_keep_natural_boundary(self):
        first = "This first thought is already long enough to stand on its own without another sentence being packed into the same block."
        second = "This second thought should be spoken separately rather than filling the synthesis engine almost to its hard character limit."
        content = subtitles(("00:00:00,000", "00:00:05,000", first), ("00:00:05,100", "00:00:10,000", second))
        blocks = create_speech_blocks(content, max_chars=300)
        self.assertEqual([block["text"] for block in blocks], [first, second])
        self.assertEqual(" ".join(block["text"] for block in blocks), f"{first} {second}")
