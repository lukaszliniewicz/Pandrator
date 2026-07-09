import json
import os
import tempfile
import unittest
from pathlib import Path

from pandrator.logic.dubbing import manual_timing, srt_utils


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:02,000
Hello there friend.

2
00:00:02,500 --> 00:00:04,000
Next subtitle.
"""


class DubbingManualTimingTests(unittest.TestCase):
    def test_segment_preview_command_uses_fast_input_seek_and_short_pcm_output(self):
        command = manual_timing.build_segment_preview_command(
            "reference.wav",
            "preview.wav",
            start_ms=850,
            end_ms=2200,
            ffmpeg_executable="ffmpeg-bin",
        )

        self.assertEqual(command[0], "ffmpeg-bin")
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "0.850")
        self.assertEqual(command[command.index("-t") + 1], "1.350")
        self.assertEqual(command[-3:], ["-acodec", "pcm_s16le", "preview.wav"])

    def test_srt_timing_round_trip(self):
        segments = manual_timing.srt_to_timing_segments(SAMPLE_SRT)
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 2.0)
        self.assertEqual(segments[0]["text"], "Hello there friend.")

        srt_content = manual_timing.timing_segments_to_srt(segments)
        parsed = srt_utils.parse_srt(srt_content)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[1].start_ms, 2500)

    def test_boundary_editor_model_splits_merges_and_tracks_manual_corrections(self):
        model = manual_timing.BoundaryEditorModel(
            manual_timing.srt_to_timing_segments(SAMPLE_SRT),
            corrections=[],
        )

        self.assertTrue(model.split_segment(0, split_position=1, split_time=1.2))
        self.assertEqual(len(model.segments), 3)
        self.assertEqual(model.segments[0]["text"], "Hello there")
        self.assertEqual(model.segments[1]["text"], "friend.")

        self.assertTrue(model.set_boundary_time(1, "end", 2.2, allow_adjacent_shift=True))
        self.assertEqual(model.segments[1]["end"], 2.2)

        self.assertTrue(model.set_segment_text(2, "Next line."))
        corrections = model.build_manual_corrections()
        self.assertTrue(any(correction["type"] == "text_edit" for correction in corrections))
        self.assertTrue(any(correction.get("new_end") == 2.2 for correction in corrections))

        self.assertTrue(model.merge_down(0))
        self.assertEqual(len(model.segments), 2)
        self.assertEqual(model.segments[0]["text"], "Hello there friend.")

    def test_boundary_editor_persistence_writes_srt_and_json(self):
        segments = manual_timing.srt_to_timing_segments(SAMPLE_SRT)
        model = manual_timing.BoundaryEditorModel(segments, corrections=[])
        self.assertTrue(model.set_segment_text(0, "Updated text."))

        with tempfile.TemporaryDirectory() as temp_dir:
            persistence = manual_timing.BoundaryEditorPersistence(
                save_folder=temp_dir,
                json_path=os.path.join(temp_dir, "source.json"),
            )
            controller = manual_timing.BoundaryEditorController(model, persistence)

            srt_path = controller.save_srt()
            json_path = controller.save_json()

            self.assertTrue(Path(srt_path).exists())
            self.assertTrue(Path(json_path).exists())
            self.assertIn("Updated text.", Path(srt_path).read_text(encoding="utf-8"))
            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["segments"][0]["text"], "Updated text.")
            self.assertEqual(payload["corrections"][0]["type"], "text_edit")

    def test_boundary_editor_persistence_rejects_missing_output_path(self):
        persistence = manual_timing.BoundaryEditorPersistence()

        with self.assertRaises(ValueError):
            persistence.write_srt([])

        with self.assertRaises(ValueError):
            persistence.write_json([], [])


if __name__ == "__main__":
    unittest.main()
