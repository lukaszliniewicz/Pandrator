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

    def test_speech_block_merge_keeps_subdub_directionality(self):
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
            ["Hi", "This following block is already long enough"],
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
