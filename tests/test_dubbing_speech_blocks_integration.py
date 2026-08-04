import json
import os
import tempfile
import unittest
from unittest.mock import patch

from pandrator.logic import dubbing_handler
from pandrator.logic.dubbing import speech_blocks


MERGE_SRT = """1
00:00:00,000 --> 00:00:01,000
Hello there

2
00:00:01,100 --> 00:00:01,500
friend

3
00:00:03,000 --> 00:00:04,000
Later
"""


class DubbingSpeechBlocksIntegrationTests(unittest.TestCase):
    def test_create_speech_blocks_uses_merge_threshold(self):
        merged = speech_blocks.create_speech_blocks(
            MERGE_SRT,
            target_language="en",
            min_chars=10,
            max_chars=80,
            merge_threshold=200,
        )
        unmerged = speech_blocks.create_speech_blocks(
            MERGE_SRT,
            target_language="en",
            min_chars=10,
            max_chars=80,
            merge_threshold=50,
        )

        self.assertEqual([block["text"] for block in merged], ["Hello there friend", "Later"])
        self.assertEqual(merged[0]["subtitles"], [1, 2])
        self.assertEqual([block["text"] for block in unmerged], ["Hello there", "friend", "Later"])

    def test_short_leading_fragment_merges_when_the_timing_rule_allows_it(self):
        content = """1
00:00:00,000 --> 00:00:00,500
Hi

2
00:00:00,600 --> 00:00:02,000
This following block is already long enough
"""
        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=80,
            merge_threshold=250,
        )

        self.assertEqual(
            [block["text"] for block in blocks],
            ["Hi This following block is already long enough"],
        )

    def test_capacity_cut_inside_sentence_is_rejoined_for_tts(self):
        content = """1
00:00:00,000 --> 00:00:02,000
This is the first half of one thought,

2
00:00:02,080 --> 00:00:04,000
and this is its natural continuation.

3
00:00:04,080 --> 00:00:06,000
This is a separate sentence.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=100,
            merge_threshold=250,
        )

        self.assertEqual(
            [block["text"] for block in blocks],
            [
                "This is the first half of one thought, and this is its natural continuation.",
                "This is a separate sentence.",
            ],
        )
        self.assertEqual(blocks[0]["subtitles"], [1, 2])

    def test_unfinished_german_sentence_bridges_long_subtitle_pauses(self):
        content = """1
00:00:00,000 --> 00:00:01,000
Und wir sind Mark und diese

2
00:00:02,680 --> 00:00:03,000
sehr interessante

3
00:00:04,647 --> 00:00:06,000
Webinarreihe geht heute weiter.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="de",
            min_chars=10,
            max_chars=220,
            merge_threshold=800,
            continuation_threshold_ms=2500,
        )

        self.assertEqual(
            ["Und wir sind Mark und diese sehr interessante Webinarreihe geht heute weiter."],
            [block["text"] for block in blocks],
        )
        self.assertEqual([1, 2, 3], blocks[0]["subtitles"])

    def test_reconstructed_utterance_is_balanced_before_capacity_split(self):
        content = """1
00:00:00,000 --> 00:00:01,000
This carefully reconstructed sentence begins with a useful explanation and

2
00:00:02,000 --> 00:00:03,000
continues across a subtitle pause before ending with a natural conclusion.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=80,
            merge_threshold=250,
            continuation_threshold_ms=2500,
        )

        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(10 <= len(str(block["text"])) <= 80 for block in blocks))
        self.assertFalse(any(len(str(block["text"]).split()) <= 2 for block in blocks))

    def test_speaker_labels_are_not_spoken_or_merged_across_speakers(self):
        content = """1
00:00:00,000 --> 00:00:01,500
[SPEAKER_0]: An unfinished thought,

2
00:00:01,580 --> 00:00:03,000
[SPEAKER_1]: answered by somebody else.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=100,
            merge_threshold=250,
        )

        self.assertEqual(
            [block["text"] for block in blocks],
            ["An unfinished thought,", "answered by somebody else."],
        )
        self.assertTrue(all("SPEAKER" not in block["text"] for block in blocks))
        self.assertEqual(
            ["SPEAKER_0", "SPEAKER_1"],
            [block["speaker"] for block in blocks],
        )

    def test_repeated_speaker_flicker_inside_one_sentence_is_repaired(self):
        content = """1
00:00:00,000 --> 00:00:00,600
Whereas this

2
00:00:00,680 --> 00:00:01,200
is one internal

3
00:00:01,280 --> 00:00:01,800
question that

4
00:00:01,880 --> 00:00:02,400
raises points about

5
00:00:02,480 --> 00:00:03,100
epistemology.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=160,
            merge_threshold=250,
            continuation_threshold_ms=2500,
            speaker_by_subtitle={
                1: "Speaker 3",
                2: "Speaker 1",
                3: "Speaker 3",
                4: "Speaker 1",
                5: "Speaker 3",
            },
        )

        self.assertEqual(
            [
                "Whereas this is one internal question that raises points about "
                "epistemology."
            ],
            [block["text"] for block in blocks],
        )
        self.assertEqual("Speaker 3", blocks[0]["speaker"])
        self.assertEqual([1, 2, 3, 4, 5], blocks[0]["subtitles"])

    def test_structured_speakers_prevent_plain_cues_from_merging(self):
        content = """1
00:00:00,000 --> 00:00:01,500
An unfinished thought,

2
00:00:01,580 --> 00:00:03,000
answered by somebody else.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=100,
            merge_threshold=250,
            speaker_by_subtitle={1: "Speaker 0", 2: "Speaker 1"},
        )

        self.assertEqual(
            [block["text"] for block in blocks],
            ["An unfinished thought,", "answered by somebody else."],
        )
        self.assertEqual(
            ["Speaker 0", "Speaker 1"],
            [block["speaker"] for block in blocks],
        )

    def test_missing_speaker_is_a_boundary_when_diarization_is_present(self):
        content = """1
00:00:00,000 --> 00:00:01,000
Known speaker.

2
00:00:01,050 --> 00:00:02,000
Unattributed cue one.

3
00:00:02,050 --> 00:00:03,000
Unattributed cue two.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=100,
            merge_threshold=250,
            speaker_by_subtitle={1: "Speaker 0"},
        )

        self.assertEqual(
            ["Known speaker.", "Unattributed cue one.", "Unattributed cue two."],
            [block["text"] for block in blocks],
        )

    def test_sentence_boundary_does_not_override_merge_threshold(self):
        content = """1
00:00:00,000 --> 00:00:01,000
First sentence.

2
00:00:01,150 --> 00:00:02,000
Second sentence.
"""

        merged = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=100,
            merge_threshold=200,
        )
        separate = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=100,
            merge_threshold=100,
        )

        self.assertEqual(["First sentence. Second sentence."], [
            block["text"] for block in merged
        ])
        self.assertEqual(["First sentence.", "Second sentence."], [
            block["text"] for block in separate
        ])

    def test_small_same_speaker_timestamp_overlap_is_treated_as_jitter(self):
        content = """1
00:00:00,000 --> 00:00:01,000
Great.

2
00:00:00,940 --> 00:00:01,800
America.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=5,
            max_chars=100,
            merge_threshold=250,
            speaker_by_subtitle={1: "Speaker 1", 2: "Speaker 1"},
        )

        self.assertEqual(["Great. America."], [block["text"] for block in blocks])

    def test_small_overlap_remains_a_boundary_across_speakers(self):
        content = """1
00:00:00,000 --> 00:00:01,000
Great.

2
00:00:00,940 --> 00:00:01,800
America.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=5,
            max_chars=100,
            merge_threshold=250,
            speaker_by_subtitle={1: "Speaker 1", 2: "Speaker 2"},
        )

        self.assertEqual(
            ["Great.", "America."],
            [block["text"] for block in blocks],
        )

    def test_speech_block_conjunction_map_preserves_non_ascii_languages(self):
        self.assertIn("ponieważ", speech_blocks.CONJUNCTIONS["pl"])
        self.assertIn("потому что", speech_blocks.CONJUNCTIONS["ru"])
        self.assertIn("因为", speech_blocks.CONJUNCTIONS["zh-cn"])

    def test_create_speech_blocks_splits_long_text_under_max_chars(self):
        srt_content = """1
00:00:00,000 --> 00:00:05,000
This is a long sentence, and it should split into smaller pieces because the dubbing generator needs manageable chunks for speech synthesis.
"""

        blocks = speech_blocks.create_speech_blocks(
            srt_content,
            target_language="en",
            min_chars=10,
            max_chars=45,
            merge_threshold=250,
        )

        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(str(block["text"])) <= 45 for block in blocks))
        self.assertEqual(
            [block["number"] for block in blocks],
            [str(index).zfill(4) for index in range(1, len(blocks) + 1)],
        )

    def test_max_chars_remains_hard_when_minimum_is_misconfigured(self):
        srt_content = """1
00:00:00,000 --> 00:00:05,000
This deliberately long sentence must still be divided at the synthesis engine limit even when the configured minimum is larger.
"""

        blocks = speech_blocks.create_speech_blocks(
            srt_content,
            target_language="en",
            min_chars=200,
            max_chars=40,
            merge_threshold=250,
        )

        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(str(block["text"])) <= 40 for block in blocks))

    def test_split_blocks_keep_exact_cue_lineage_and_alignment_groups(self):
        content = """1
00:00:00,000 --> 00:00:03,000
This first cue contains a deliberately long complete sentence that needs several synthesis chunks.

2
00:00:04,000 --> 00:00:07,000
This second cue contains another deliberately long complete sentence that also needs several chunks.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=10,
            max_chars=42,
            merge_threshold=250,
        )

        first = [block for block in blocks if block["subtitles"] == [1]]
        second = [block for block in blocks if block["subtitles"] == [2]]
        self.assertGreater(len(first), 1)
        self.assertGreater(len(second), 1)
        self.assertEqual(1, len({block["alignment_group"] for block in first}))
        self.assertEqual(1, len({block["alignment_group"] for block in second}))
        self.assertNotEqual(first[0]["alignment_group"], second[0]["alignment_group"])
        self.assertTrue(all(len(str(block["text"])) <= 42 for block in blocks))

    def test_reviewed_speech_is_split_once_without_text_duplication(self):
        display = """1
00:00:00,000 --> 00:00:02,000
This display cue is deliberately much longer than the synthesis limit.
"""
        reviewed = """1
00:00:00,000 --> 00:00:02,000
A single reviewed cue.
"""

        blocks = speech_blocks.create_speech_blocks(
            display,
            target_language="en",
            min_chars=5,
            max_chars=20,
            speech_srt_content=reviewed,
        )

        self.assertEqual(2, len(blocks))
        self.assertTrue(all(block["subtitles"] == [1] for block in blocks))
        self.assertEqual(1, len({block["alignment_group"] for block in blocks}))
        self.assertEqual(
            "A single reviewed cue.",
            " ".join(str(block["_optimized_text"]) for block in blocks),
        )
        self.assertTrue(
            all(len(str(block["_optimized_text"])) <= 20 for block in blocks)
        )

    def test_zero_continuation_threshold_is_not_replaced_by_merge_threshold(self):
        content = """1
00:00:00,000 --> 00:00:01,000
This thought continues

2
00:00:01,100 --> 00:00:02,000
after the cue boundary.
"""

        blocks = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=5,
            max_chars=100,
            merge_threshold=250,
            continuation_threshold_ms=0,
        )

        self.assertEqual(
            ["This thought continues", "after the cue boundary."],
            [block["text"] for block in blocks],
        )

    def test_maximum_internal_gap_is_a_hard_utterance_boundary(self):
        content = """1
00:00:00,000 --> 00:00:01,000
This thought continues

2
00:00:01,500 --> 00:00:02,500
after a noticeable pause.
"""

        merged = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=5,
            max_chars=100,
            merge_threshold=100,
            continuation_threshold_ms=1000,
            max_internal_gap_ms=600,
        )
        separated = speech_blocks.create_speech_blocks(
            content,
            target_language="en",
            min_chars=5,
            max_chars=100,
            merge_threshold=100,
            continuation_threshold_ms=1000,
            max_internal_gap_ms=400,
        )

        self.assertEqual(1, len(merged))
        self.assertEqual(2, len(separated))

    def test_display_line_breaks_are_removed_from_speech_text(self):
        srt_content = """1
00:00:00,000 --> 00:00:03,000
This is visually wrapped
but spoken continuously.
"""

        blocks = speech_blocks.create_speech_blocks(
            srt_content,
            target_language="en",
            min_chars=10,
            max_chars=100,
        )

        self.assertEqual(
            [block["text"] for block in blocks],
            ["This is visually wrapped but spoken continuously."],
        )
        self.assertNotIn("\n", blocks[0]["text"])

    def test_generate_speech_blocks_file_writes_subdub_compatible_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "sample.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(MERGE_SRT)

            output_path = speech_blocks.generate_speech_blocks_file(
                temp_dir,
                srt_path,
                target_language="en",
                min_chars=10,
                max_chars=80,
                merge_threshold=200,
            )

            self.assertEqual(output_path, os.path.join(temp_dir, "sample_speech_blocks.json"))
            with open(output_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload[0]["number"], "0001")
            self.assertEqual(payload[0]["text"], "Hello there friend")
            self.assertEqual(payload[0]["subtitles"], [1, 2])

    def test_dubbing_handler_speech_blocks_write_native_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "native.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(MERGE_SRT)

            self.assertEqual(
                dubbing_handler.generate_speech_blocks_with_result(
                    temp_dir,
                    srt_path,
                    target_language="en",
                ),
                os.path.join(temp_dir, "native_speech_blocks.json"),
            )
            self.assertTrue(
                dubbing_handler.generate_speech_blocks(
                    temp_dir,
                    srt_path,
                    target_language="en",
                )
            )

            self.assertTrue(os.path.exists(os.path.join(temp_dir, "native_speech_blocks.json")))

    def test_dubbing_handler_equalization_writes_native_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "native.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(
                    """1
00:00:00,000 --> 00:00:03,000
This subtitle line is long enough to be wrapped by the native equalizer.
"""
                )

            self.assertEqual(
                dubbing_handler.equalize_subtitles_with_result(srt_path),
                os.path.join(temp_dir, "native_equalized.srt"),
            )
            self.assertTrue(dubbing_handler.equalize_subtitles(srt_path))

            equalized_path = os.path.join(temp_dir, "native_equalized.srt")
            self.assertTrue(os.path.exists(equalized_path))
            with open(equalized_path, "r", encoding="utf-8") as handle:
                self.assertIn("\n", handle.read())

    def test_add_subtitles_uses_target_language_metadata(self):
        captured_commands = []

        class FakeProcess:
            def __init__(self, command, **_kwargs):
                captured_commands.append(command)
                self.command = command
                self.stdout = []
                self.returncode = 0

            def wait(self):
                with open(self.command[-1], "wb") as handle:
                    handle.write(b"video")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "final.mp4")
            with patch("pandrator.logic.dubbing_handler.subprocess.Popen", FakeProcess):
                self.assertTrue(
                    dubbing_handler.add_subtitles_to_video(
                        synced_video_path=os.path.join(temp_dir, "video.mp4"),
                        equalized_srt_path=os.path.join(temp_dir, "subs.srt"),
                        output_video_path=output_path,
                        subtitle_mode="soft",
                        subtitle_language="pl",
                    )
                )

            self.assertTrue(os.path.exists(output_path))
            self.assertIn("language=pol", captured_commands[0])


if __name__ == "__main__":
    unittest.main()
