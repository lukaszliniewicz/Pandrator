import json
import tempfile
import unittest
from pathlib import Path

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
    compose_from_crispasr_json,
    finalize_srt_content,
    wrap_subtitle_text,
)


class SubtitleFinalizationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
