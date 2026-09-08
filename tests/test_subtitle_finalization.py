import json
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from pandrator.logic.dubbing import subtitle_finalization
from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
    compose_from_crispasr_json,
    compose_transcript_segments,
    compose_transcript_segments_with_ownership,
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
            segments = parse_srt(
                compose_from_crispasr_json(
                    path,
                    {"subtitle_phrase_gap_ms": 500},
                )
            )
        self.assertEqual([segment.text for segment in segments], ["Hello everyone.", "Next topic now."])
        self.assertTrue(all(segment.end_ms - segment.start_ms >= 833 for segment in segments))

    def test_reading_speed_extends_a_cue_when_the_timeline_has_room(self):
        content = """1
00:00:00,000 --> 00:00:01,000
This cue contains forty readable characters.
"""
        segments = parse_srt(finalize_srt_content(content, {"subtitle_max_cps": 10}))

        self.assertGreaterEqual(segments[0].end_ms - segments[0].start_ms, 4000)

    def test_duration_adjustment_preserves_actual_overlapping_speech(self):
        from pandrator.logic.dubbing.models import SubtitleSegment

        cues = [
            SubtitleSegment(1, 11320, 12760, "Co-chair.", "A"),
            SubtitleSegment(2, 11360, 15000, "Thank you for joining us.", "B"),
        ]
        result = subtitle_finalization._adjust_durations(cues, SubtitleFinalizationConfig())

        self.assertEqual(result[0], cues[0])
        self.assertEqual([cue.speaker for cue in result], ["A", "B"])

    def test_word_cleanup_preserves_an_overlapping_speaker_interjection(self):
        payload = {"segments": [
            {"start_ms": 11320, "end_ms": 12760, "text": "Co-chair.", "speaker": "A",
             "words": [{"text": "Co-chair.", "start_ms": 11320, "end_ms": 12760}]},
            {"start_ms": 11360, "end_ms": 13000, "text": "Yes, welcome.", "speaker": "B",
             "words": [{"text": "Yes,", "start_ms": 11360, "end_ms": 12000},
                       {"text": "welcome.", "start_ms": 12100, "end_ms": 13000}]},
        ]}
        with patch("pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries", return_value=None):
            result = compose_transcript_segments(payload)

        self.assertEqual((result[0].start_ms, result[0].end_ms), (11320, 12760))
        self.assertEqual(result[0].text, "Co-chair.")
        self.assertEqual(result[0].speaker, "A")

    def test_word_cleanup_does_not_cap_plausible_sustained_words_to_fast_speech_median(self):
        word = subtitle_finalization._TimedWord
        words = [word(i, "short", i * 200, i * 200 + 80, "B") for i in range(10)]
        words.extend([
            word(10, "Co-chair.", 11320, 12760, "A"),
            word(11, "Yes.", 11360, 12080, "B"),
        ])
        result = subtitle_finalization._sanitize_timed_words(words)

        self.assertEqual(result[10].end_ms, 12760)

    def test_duration_adjustment_sorts_cues_without_erasing_their_intervals(self):
        from pandrator.logic.dubbing.models import SubtitleSegment

        cues = [
            SubtitleSegment(1, 2000, 6000, "A longer overlapping contribution.", "A"),
            SubtitleSegment(2, 1000, 3500, "A second speaker.", "B"),
        ]
        result = subtitle_finalization._adjust_durations(cues, SubtitleFinalizationConfig())

        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in result], [(1000, 3500), (2000, 6000)])
        self.assertEqual([cue.speaker for cue in result], ["B", "A"])

    def test_composer_does_not_treat_untimed_display_extension_as_source_overlap(self):
        payload = {"segments": [
            {"start_ms": 0, "end_ms": 500, "text": "First reply.", "speaker": "A"},
            {"start_ms": 600, "end_ms": 1200, "text": "Second reply.", "speaker": "B"},
        ]}
        result = compose_transcript_segments(payload)

        self.assertEqual(result[0].end_ms, 520)
        self.assertGreaterEqual(result[1].end_ms, 1200)

    def test_word_timed_composer_coalesces_compact_cross_cue_thought(self):
        payload = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "cue-a",
                    "start_ms": 0,
                    "end_ms": 500,
                    "speaker": "P",
                    "text": "...one single.",
                    "words": [
                        {"text": "...one", "start_ms": 0, "end_ms": 200},
                        {"text": "single.", "start_ms": 220, "end_ms": 500},
                    ],
                },
                {
                    "id": "cue-b",
                    "start_ms": 980,
                    "end_ms": 1500,
                    "speaker": "P",
                    "text": "Kanon, Kanon.",
                    "words": [
                        {"text": "Kanon,", "start_ms": 980, "end_ms": 1200},
                        {"text": "Kanon.", "start_ms": 1220, "end_ms": 1500},
                    ],
                },
            ],
        }
        with patch(
            "pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries",
            return_value=None,
        ):
            segments = compose_transcript_segments(payload)

        self.assertEqual(1, len(segments))
        self.assertEqual("...one single. Kanon, Kanon.", segments[0].text)
        self.assertEqual("P", segments[0].speaker)

    def test_word_timed_composer_preserves_untimed_source_cue(self):
        payload = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "timed-a",
                    "start_ms": 0,
                    "end_ms": 500,
                    "speaker": "P",
                    "text": "Timed before.",
                    "words": [{"text": "Timed", "start_ms": 0, "end_ms": 250}, {"text": "before.", "start_ms": 280, "end_ms": 500}],
                },
                {
                    "id": "untimed",
                    "start_ms": 600,
                    "end_ms": 900,
                    "speaker": "P",
                    "text": "Untimed cue.",
                },
                {
                    "id": "timed-b",
                    "start_ms": 980,
                    "end_ms": 1400,
                    "speaker": "P",
                    "text": "Timed after.",
                    "words": [{"text": "Timed", "start_ms": 980, "end_ms": 1150}, {"text": "after.", "start_ms": 1180, "end_ms": 1400}],
                },
            ],
        }
        with patch(
            "pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries",
            return_value=None,
        ):
            segments = compose_transcript_segments(payload)

        self.assertEqual(["Timed before.", "Untimed cue.", "Timed after."], [item.text for item in segments])
        self.assertTrue(
            all(left.end_ms <= right.start_ms for left, right in pairwise(segments))
        )

    def test_word_ownership_preserves_overlapping_speaker_cues(self):
        payload = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "alice",
                    "start_ms": 0,
                    "end_ms": 3000,
                    "speaker": "Alice",
                    "text": "alpha",
                    "words": [{"text": "alpha", "start_ms": 0, "end_ms": 3000}],
                },
                {
                    "id": "bob",
                    "start_ms": 1000,
                    "end_ms": 1300,
                    "speaker": "Bob",
                    "text": "beta",
                    "words": [{"text": "beta", "start_ms": 1000, "end_ms": 1300}],
                },
            ],
        }

        result = compose_transcript_segments_with_ownership(payload)

        self.assertEqual(["alpha", "beta"], [item.text for item in result.segments])
        self.assertEqual(["Alice", "Bob"], [item.speaker for item in result.segments])
        self.assertEqual({0: 0, 1: 1}, result.word_segment_ordinals)

    def test_word_ownership_is_deterministic_for_same_speaker_overlap(self):
        payload = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "first",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "speaker": "Speaker",
                    "text": "echo",
                    "words": [{"text": "echo", "start_ms": 0, "end_ms": 1000}],
                },
                {
                    "id": "second",
                    "start_ms": 500,
                    "end_ms": 700,
                    "speaker": "Speaker",
                    "text": "echo",
                    "words": [{"text": "echo", "start_ms": 500, "end_ms": 700}],
                },
            ],
        }

        first = compose_transcript_segments_with_ownership(payload)
        second = compose_transcript_segments_with_ownership(payload)

        self.assertEqual(first, second)
        self.assertEqual({0: 0, 1: 0}, first.word_segment_ordinals)

    def test_word_ownership_tracks_sorted_words_across_split_coalesce_and_wordless_runs(
        self,
    ):
        payload = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "before",
                    "start_ms": 0,
                    "end_ms": 400,
                    "speaker": "Speaker",
                    "text": "first part.",
                    "words": [
                        {"text": "first", "start_ms": 0, "end_ms": 180},
                        {"text": "part.", "start_ms": 200, "end_ms": 400},
                    ],
                },
                {
                    "id": "wordless",
                    "start_ms": 600,
                    "end_ms": 900,
                    "speaker": "Speaker",
                    "text": "A wordless cue.",
                },
                {
                    "id": "after",
                    "start_ms": 1000,
                    "end_ms": 1800,
                    "speaker": "Speaker",
                    "text": "early late",
                    "words": [
                        {"text": "late", "start_ms": 1500, "end_ms": 1600},
                        {"text": "early", "start_ms": 1100, "end_ms": 1200},
                    ],
                },
            ],
        }

        result = compose_transcript_segments_with_ownership(payload)

        self.assertEqual(
            ["first part.", "A wordless cue.", "early late"],
            [item.text for item in result.segments],
        )
        self.assertEqual({0: 0, 1: 0, 2: 2, 3: 2}, result.word_segment_ordinals)

    def test_word_ownership_uses_global_time_order_across_wordless_boundaries(self):
        result = compose_transcript_segments_with_ownership(
            {
                "schema": "pandrator.transcript.v1",
                "segments": [
                    {
                        "text": "late",
                        "start_ms": 3000,
                        "end_ms": 3400,
                        "words": [{"text": "late", "start_ms": 3000, "end_ms": 3400}],
                    },
                    {"text": "Wordless.", "start_ms": 1500, "end_ms": 2000},
                    {
                        "text": "early",
                        "start_ms": 0,
                        "end_ms": 400,
                        "words": [{"text": "early", "start_ms": 0, "end_ms": 400}],
                    },
                ],
            }
        )
        self.assertEqual(
            ["early", "Wordless.", "late"], [cue.text for cue in result.segments]
        )
        self.assertEqual({0: 0, 1: 2}, result.word_segment_ordinals)

    def test_word_ownership_omits_deduplicated_moss_words(self):
        def word(text, start, speaker, segment_id):
            return {
                "text": text,
                "start_ms": start,
                "end_ms": start + 220,
                "speaker": speaker,
                "metadata": {"moss_segment_id": segment_id},
            }

        payload = {
            "schema": "pandrator.transcript.v1",
            "segments": [
                {
                    "id": "moss-a",
                    "start_ms": 0,
                    "end_ms": 1700,
                    "speaker": "S1",
                    "text": "asleep when you're under",
                    "words": [
                        word("asleep", 0, "S1", "moss-a"),
                        word("when", 400, "S1", "moss-a"),
                        word("you're", 800, "S1", "moss-a"),
                        word("under", 1200, "S1", "moss-a"),
                    ],
                },
                {
                    "id": "moss-b",
                    "start_ms": 420,
                    "end_ms": 2300,
                    "speaker": "S2",
                    "text": "when you're under anesthesia",
                    "words": [
                        word("when", 420, "S2", "moss-b"),
                        word("you're", 820, "S2", "moss-b"),
                        word("under", 1220, "S2", "moss-b"),
                        word("anesthesia", 1700, "S2", "moss-b"),
                    ],
                },
            ],
        }

        result = compose_transcript_segments_with_ownership(payload)

        self.assertEqual({0: 0, 2: 0, 4: 0, 6: 0, 7: 0}, result.word_segment_ordinals)

    def test_zero_minimum_gap_is_a_valid_explicit_setting(self):
        config = SubtitleFinalizationConfig.from_settings({"subtitle_min_gap_ms": 0})
        self.assertEqual(config.min_gap_ms, 0)

    def test_hard_gap_and_sentence_threshold_are_configurable(self):
        config = SubtitleFinalizationConfig.from_settings(
            {
                "subtitle_hard_gap_ms": 1000,
                "subtitle_sentence_boundary_threshold": 0.7,
            }
        )

        self.assertEqual(1000, config.hard_gap_ms)
        self.assertEqual(0.7, config.sentence_boundary_threshold)

    def test_diarized_words_break_on_speaker_changes_without_visible_labels(self):
        payload = {
            "transcription": [
                {"speaker": "0", "words": [{"text": "Hello.", "offsets": {"from": 0, "to": 500}}]},
                {"speaker": "1", "words": [{"text": "Welcome.", "offsets": {"from": 700, "to": 1200}}]},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "words.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            content = compose_from_crispasr_json(path)
            segments = parse_srt(content)

        self.assertEqual(len(segments), 2)
        self.assertEqual([segment.text for segment in segments], ["Hello.", "Welcome."])
        self.assertNotIn("[SPEAKER_", content)

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
        tokens = ["This", "carefully", "constructed", "sentence", "has", "an", "unavoidable", "final", "word", "too."]
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

    def test_capacity_partition_balances_a_just_over_two_line_event(self):
        text = (
            "This is a deliberately balanced subtitle sentence that contains "
            "enough words to produce a properly sized final event today."
        )
        config = SubtitleFinalizationConfig(max_chars_per_line=60, max_lines=2)
        chunks = subtitle_finalization._split_words_to_capacity(text, config)

        self.assertGreater(len(text), config.max_event_chars)
        self.assertEqual(2, len(chunks))
        self.assertEqual(text.split(), " ".join(chunks).split())
        self.assertLessEqual(max(map(len, chunks)) - min(map(len, chunks)), 10)

        segments = subtitle_finalization._split_segment(
            subtitle_finalization.SubtitleSegment(0, 0, 10_000, text, "A"),
            config,
        )
        self.assertTrue(all(segment.end_ms - segment.start_ms >= 833 for segment in segments))

    def test_capacity_partition_keeps_exact_nonfirst_range_lengths(self):
        left = " ".join(["alpha"] * 9 + ["planet"])
        right = " ".join(["bravo"] * 9 + ["rocket"])
        text = f"{left} {right}"
        config = SubtitleFinalizationConfig(max_chars_per_line=60, max_lines=2)

        chunks = subtitle_finalization._split_words_to_capacity(text, config)

        self.assertEqual([60, 60], [len(chunk) for chunk in chunks])
        self.assertEqual(text.split(), " ".join(chunks).split())

    def test_capacity_partition_isolates_one_overlong_token_without_word_explosion(self):
        overlong = "x" * 80
        text = f"one two three four five {overlong} six seven eight nine ten"
        config = SubtitleFinalizationConfig(max_chars_per_line=60, max_lines=2)

        chunks = subtitle_finalization._split_words_to_capacity(text, config)

        self.assertEqual(3, len(chunks))
        self.assertEqual(overlong, chunks[1])
        self.assertEqual(text.split(), " ".join(chunks).split())

    def test_split_segment_covers_long_interval_with_duration_chunks_and_gaps(self):
        config = SubtitleFinalizationConfig(max_duration_ms=12_000, min_gap_ms=80)
        segments = subtitle_finalization._split_segment(
            subtitle_finalization.SubtitleSegment(
                0,
                0,
                20_000,
                "One moderate sentence is enough for two timed cues.",
                "A",
            ),
            config,
        )

        self.assertGreaterEqual(len(segments), 2)
        self.assertEqual(0, segments[0].start_ms)
        self.assertEqual(20_000, segments[-1].end_ms)
        self.assertTrue(
            all(segment.end_ms - segment.start_ms <= 12_000 for segment in segments)
        )
        self.assertTrue(
            all(
                right.start_ms - left.end_ms == 80
                for left, right in pairwise(segments)
            )
        )

    def test_split_segment_keeps_infeasible_short_interval_bounded(self):
        segments = subtitle_finalization._split_segment(
            subtitle_finalization.SubtitleSegment(0, 0, 240, "Yes.", "A"),
            SubtitleFinalizationConfig(),
        )

        self.assertTrue(all(segment.end_ms > segment.start_ms for segment in segments))
        self.assertTrue(all(segment.start_ms >= 0 and segment.end_ms <= 240 for segment in segments))
        self.assertEqual(["Yes."], [segment.text for segment in segments])

    def test_split_segment_keeps_layout_and_reading_limits_when_interval_allows(self):
        text = (
            "Every readable subtitle cue should preserve the entire sentence while "
            "respecting line limits and duration floors for this test."
        )
        config = SubtitleFinalizationConfig(
            max_chars_per_line=40,
            max_lines=2,
            max_chars_per_second=20,
            min_duration_ms=833,
            max_duration_ms=7_000,
        )
        segments = subtitle_finalization._split_segment(
            subtitle_finalization.SubtitleSegment(0, 0, 15_000, text, "A"),
            config,
        )

        self.assertEqual(text.split(), " ".join(segment.text for segment in segments).split())
        self.assertTrue(
            all(
                len(line) <= 40
                for segment in segments
                for line in segment.text.splitlines()
            )
        )
        self.assertTrue(
            all(
                segment.end_ms - segment.start_ms
                >= max(
                    833,
                    (len(segment.text.replace("\n", " ")) * 1000 + 19) // 20,
                )
                for segment in segments
            )
        )

    def test_sat_probability_can_select_an_unpunctuated_semantic_boundary(self):
        tokens = ["We", "carefully", "reviewed", "the", "report", "today", "everyone", "approved", "the", "final", "version."]
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

    def test_sentence_boundary_threshold_actually_gates_sat_evidence(self):
        tokens = ["We", "carefully", "reviewed", "the", "report", "today", "everyone", "approved", "the", "final", "version"]
        payload = {"transcription": [{"words": [
            {"text": token, "offsets": {"from": index * 330, "to": index * 330 + 290}}
            for index, token in enumerate(tokens)
        ]}]}
        source_text = " ".join(tokens)
        probabilities = [0.0] * len(source_text)
        probabilities[source_text.index("today") + len("today") - 1] = 0.98

        def prediction(_text, *, threshold):
            return {
                "threshold": threshold,
                "probabilities": probabilities,
                "boundaries": [],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "words.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch(
                "pandrator.logic.dubbing.subtitle_finalization.sentence_segmenter.predict_boundaries",
                side_effect=prediction,
            ):
                permissive = parse_srt(
                    compose_from_crispasr_json(
                        path,
                        {"subtitle_sentence_boundary_threshold": 0.25},
                    )
                )
                conservative = parse_srt(
                    compose_from_crispasr_json(
                        path,
                        {"subtitle_sentence_boundary_threshold": 0.99},
                    )
                )

        self.assertEqual(2, len(permissive))
        self.assertEqual(1, len(conservative))

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

    def test_moss_chunk_seam_deduplicates_only_a_time_aligned_word_run(self):
        payload = {
            "schema": "pandrator.transcript.v1",
            "source_format": "moss-transcribe-cpp",
            "segments": [
                {
                    "id": "moss-a",
                    "start_ms": 0,
                    "end_ms": 1800,
                    "speaker": "S1",
                    "text": "A lot of the other things",
                    "words": [
                        {
                            "text": token,
                            "start_ms": index * 300,
                            "end_ms": index * 300 + 250,
                            "speaker": "S1",
                            "metadata": {"moss_segment_id": "moss-a"},
                        }
                        for index, token in enumerate(["A", "lot", "of", "the", "other", "things"])
                    ],
                },
                {
                    "id": "moss-b",
                    "start_ms": 20,
                    "end_ms": 2150,
                    "speaker": "S2",
                    "text": "A lot of the other things remain",
                    "words": [
                        {
                            "text": token,
                            "start_ms": 20 + index * 300,
                            "end_ms": 270 + index * 300,
                            "speaker": "S2",
                            "metadata": {"moss_segment_id": "moss-b"},
                        }
                        for index, token in enumerate(
                            ["A", "lot", "of", "the", "other", "things", "remain"]
                        )
                    ],
                },
            ],
        }

        segments = self._compose_payload(payload)
        flattened = " ".join(segment.text.replace("\n", " ") for segment in segments)

        self.assertEqual(flattened.casefold().split().count("lot"), 1)
        self.assertIn("remain", flattened)

    def test_moss_suffix_prefix_seam_keeps_the_continuing_later_stream(self):
        def word(text, start_ms, speaker, segment_id):
            return {
                "text": text,
                "start_ms": start_ms,
                "end_ms": start_ms + 220,
                "speaker": speaker,
                "metadata": {"moss_segment_id": segment_id},
            }

        payload = {
            "schema": "pandrator.transcript.v1",
            "source_format": "moss-transcribe-cpp",
            "segments": [
                {
                    "id": "moss-a",
                    "start_ms": 0,
                    "end_ms": 1700,
                    "speaker": "S1",
                    "text": "asleep when you're under",
                    "words": [
                        word("asleep", 0, "S1", "moss-a"),
                        word("when", 400, "S1", "moss-a"),
                        word("you're", 800, "S1", "moss-a"),
                        word("under", 1200, "S1", "moss-a"),
                    ],
                },
                {
                    "id": "moss-b",
                    "start_ms": 420,
                    "end_ms": 2300,
                    "speaker": "S2",
                    "text": "when you're under anesthesia",
                    "words": [
                        word("when", 420, "S2", "moss-b"),
                        word("you're", 820, "S2", "moss-b"),
                        word("under", 1220, "S2", "moss-b"),
                        word("anesthesia", 1700, "S2", "moss-b"),
                    ],
                },
            ],
        }

        words = subtitle_finalization._timed_words(payload)
        tokens = [item.text.casefold() for item in words]

        self.assertEqual(1, tokens.count("when"))
        self.assertEqual(1, tokens.count("you're"))
        self.assertEqual(1, tokens.count("under"))
        self.assertEqual(
            ["S2", "S2", "S2"],
            [item.speaker for item in words if item.text.casefold() in {"when", "you're", "under"}],
        )


if __name__ == "__main__":
    unittest.main()
