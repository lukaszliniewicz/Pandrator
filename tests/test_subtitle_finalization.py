import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
    compose_from_crispasr_json,
    finalize_srt_content,
    wrap_subtitle_text,
)


class SubtitleFinalizationTests(unittest.TestCase):
    def _compose_payload(self, payload, settings=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "words.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch(
                "pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries",
                return_value=None,
            ):
                return parse_srt(compose_from_crispasr_json(path, settings))

    def test_default_is_two_lines_and_more_permissive_than_netflix_42(self):
        config = SubtitleFinalizationConfig()
        self.assertEqual(config.max_lines, 2)
        self.assertEqual(config.max_chars_per_line, 48)

    def test_balanced_wrap_prefers_punctuation_and_never_exceeds_two_lines(self):
        text = "This is the first complete phrase, and this is the second part of the meeting update."
        wrapped = wrap_subtitle_text(text, SubtitleFinalizationConfig(max_chars_per_line=50))
        lines = wrapped.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 50 for line in lines))
        self.assertTrue(lines[0].endswith(","))

    def test_long_event_splits_into_two_line_cues_without_touching_source(self):
        source = """1
00:00:00,000 --> 00:00:08,000
This is a deliberately long meeting subtitle containing enough words to require several readable subtitle cues while remaining independent from speech blocks and TTS segmentation.
"""
        finalized = finalize_srt_content(source, {"subtitle_max_chars_per_line": 40})
        segments = parse_srt(finalized)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(len(item.text.splitlines()) <= 2 for item in segments))
        self.assertTrue(all(len(line) <= 40 for item in segments for line in item.text.splitlines()))
        self.assertIn("deliberately long meeting", source)

    def test_word_timestamps_drive_cue_boundaries_and_minimum_duration(self):
        payload = {
            "transcription": [{
                "offsets": {"from": 0, "to": 2500},
                "text": "Hello everyone. Next topic now.",
                "words": [
                    {"text": "Hello", "offsets": {"from": 0, "to": 300}},
                    {"text": "everyone.", "offsets": {"from": 320, "to": 650}},
                    {"text": "Next", "offsets": {"from": 1200, "to": 1500}},
                    {"text": "topic", "offsets": {"from": 1520, "to": 1800}},
                    {"text": "now.", "offsets": {"from": 1820, "to": 2100}},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "words.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            segments = parse_srt(compose_from_crispasr_json(path))
        self.assertEqual([segment.text for segment in segments], ["Hello everyone.", "Next topic now."])
        self.assertTrue(all(segment.end_ms - segment.start_ms >= 833 for segment in segments))

    def test_reading_speed_extends_a_cue_when_the_timeline_has_room(self):
        content = """1
00:00:00,000 --> 00:00:01,000
This cue contains forty readable characters.
"""
        segments = parse_srt(finalize_srt_content(content, {"subtitle_max_cps": 10}))

        self.assertGreaterEqual(segments[0].end_ms - segments[0].start_ms, 4000)

    def test_zero_minimum_gap_is_a_valid_explicit_setting(self):
        config = SubtitleFinalizationConfig.from_settings({"subtitle_min_gap_ms": 0})
        self.assertEqual(config.min_gap_ms, 0)

    def test_diarized_words_break_on_speaker_changes(self):
        payload = {
            "transcription": [
                {"speaker": "0", "words": [{"text": "Hello.", "offsets": {"from": 0, "to": 500}}]},
                {"speaker": "1", "words": [{"text": "Welcome.", "offsets": {"from": 700, "to": 1200}}]},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "words.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            segments = parse_srt(compose_from_crispasr_json(path))

        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].text.startswith("[SPEAKER_0]:"))
        self.assertTrue(segments[1].text.startswith("[SPEAKER_1]:"))

    def test_long_silence_is_a_hard_boundary_without_punctuation(self):
        payload = {"transcription": [{"words": [
            {"text": "First", "offsets": {"from": 0, "to": 250}},
            {"text": "complete", "offsets": {"from": 270, "to": 600}},
            {"text": "thought", "offsets": {"from": 620, "to": 900}},
            {"text": "here", "offsets": {"from": 920, "to": 1150}},
            {"text": "another", "offsets": {"from": 3000, "to": 3300}},
            {"text": "complete", "offsets": {"from": 3320, "to": 3620}},
            {"text": "thought", "offsets": {"from": 3640, "to": 3900}},
            {"text": "follows", "offsets": {"from": 3920, "to": 4200}},
        ]}]}

        segments = self._compose_payload(payload)

        self.assertEqual(
            [segment.text for segment in segments],
            ["First complete thought here", "another complete thought follows"],
        )

    def test_short_pause_is_soft_evidence_and_does_not_force_a_split(self):
        payload = {"transcription": [{"words": [
            {"text": "A", "offsets": {"from": 0, "to": 150}},
            {"text": "brief", "offsets": {"from": 170, "to": 400}},
            {"text": "pause", "offsets": {"from": 420, "to": 650}},
            {"text": "inside", "offsets": {"from": 850, "to": 1100}},
            {"text": "one", "offsets": {"from": 1120, "to": 1300}},
            {"text": "thought", "offsets": {"from": 1320, "to": 1600}},
            {"text": "is", "offsets": {"from": 1620, "to": 1740}},
            {"text": "natural.", "offsets": {"from": 1760, "to": 2100}},
        ]}]}

        segments = self._compose_payload(payload)

        self.assertEqual(len(segments), 1)

    def test_capacity_split_does_not_leave_a_dangling_final_word(self):
        tokens = "This carefully constructed sentence has an unavoidable final word too.".split()
        payload = {"transcription": [{"words": [
            {"text": token, "offsets": {"from": index * 320, "to": index * 320 + 280}}
            for index, token in enumerate(tokens)
        ]}]}

        segments = self._compose_payload(
            payload,
            {"subtitle_max_chars_per_line": 25, "subtitle_max_lines": 2},
        )

        self.assertGreater(len(segments), 1)
        self.assertGreaterEqual(len(segments[-1].text.replace("\n", " ")), 20)
        self.assertFalse(any(segment.text.replace("\n", " ") == "too." for segment in segments))

    def test_sat_probability_can_select_an_unpunctuated_semantic_boundary(self):
        tokens = "We carefully reviewed the report today everyone approved the final version.".split()
        payload = {"transcription": [{"words": [
            {"text": token, "offsets": {"from": index * 330, "to": index * 330 + 290}}
            for index, token in enumerate(tokens)
        ]}]}
        source_text = " ".join(tokens)
        probabilities = [0.0] * len(source_text)
        probabilities[source_text.index("today") + len("today") - 1] = 0.98
        prediction = {"threshold": 0.25, "probabilities": probabilities, "boundaries": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "words.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch(
                "pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries",
                return_value=prediction,
            ):
                segments = parse_srt(compose_from_crispasr_json(path))

        self.assertEqual(len(segments), 2)
        self.assertTrue(segments[0].text.endswith("today"))
        self.assertTrue(segments[1].text.startswith("everyone"))

    def test_implausible_word_span_is_sanitized_and_all_words_are_preserved_once(self):
        payload = {"transcription": [{"words": [
            {"text": "This", "offsets": {"from": 0, "to": 240}},
            {"text": "ends", "offsets": {"from": 260, "to": 500}},
            {"text": "too.", "offsets": {"from": 520, "to": 16840}},
            {"text": "Another", "offsets": {"from": 17000, "to": 17300}},
            {"text": "thought", "offsets": {"from": 17320, "to": 17620}},
            {"text": "follows.", "offsets": {"from": 17640, "to": 18000}},
        ]}]}

        segments = self._compose_payload(payload)
        flattened = " ".join(segment.text.replace("\n", " ") for segment in segments)

        self.assertTrue(all(segment.end_ms - segment.start_ms <= 7000 for segment in segments))
        for token in ("This", "ends", "too.", "Another", "thought", "follows."):
            self.assertEqual(flattened.split().count(token), 1)


if __name__ == "__main__":
    unittest.main()
