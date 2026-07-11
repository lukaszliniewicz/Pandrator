import os
import json
import tempfile
import unittest
from pathlib import Path

from pandrator.logic.dubbing import subtitle_comparison, workflow
from pandrator.logic.dubbing.video_muxing import build_multi_soft_subtitle_command


class GuidedDubbingWorkflowTests(unittest.TestCase):
    def test_presets_are_source_aware(self):
        self.assertEqual(
            workflow.preset_stages("clean_subtitles", "movie.mp4"),
            ["transcribe", "correct", "export"],
        )
        self.assertEqual(
            workflow.preset_stages("clean_subtitles", "captions.srt"),
            ["correct", "export"],
        )
        self.assertIn(
            "translate",
            workflow.preset_stages("voiceover", "movie.mp4", translate_voiceover=True),
        )

    def test_recording_upstream_stage_marks_descendants_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.srt"
            corrected = Path(temp_dir) / "corrected.srt"
            source.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
            corrected.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
            workflow.record_stage(temp_dir, "correct", str(corrected), parent_path=str(source))
            workflow.record_stage(temp_dir, "translate", str(corrected), parent_path=str(corrected))
            workflow.record_stage(temp_dir, "correct", str(corrected), parent_path=str(source), stage_settings={"v": 2})
            states = workflow.stage_states(temp_dir, list(workflow.STAGE_ORDER))
            self.assertEqual(states["correct"], "completed")
            self.assertEqual(states["translate"], "stale")

    def test_comparison_groups_merge_and_split_by_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.srt"
            corrected = Path(temp_dir) / "corrected.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nworld\n",
                encoding="utf-8",
            )
            corrected.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n",
                encoding="utf-8",
            )
            rows = subtitle_comparison.comparison_rows(
                {"source": str(source), "corrected": str(corrected)}
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["corrected"], "Hello world.")
            self.assertTrue(all(row["changed"] for row in rows))

    def test_external_parent_edit_marks_stage_and_descendants_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.srt"
            corrected = Path(temp_dir) / "corrected.srt"
            translated = Path(temp_dir) / "translated.srt"
            source.write_text("original", encoding="utf-8")
            corrected.write_text("corrected", encoding="utf-8")
            translated.write_text("translated", encoding="utf-8")
            workflow.record_stage(temp_dir, "correct", str(corrected), parent_path=str(source))
            workflow.record_stage(temp_dir, "translate", str(translated), parent_path=str(corrected))
            source.write_text("edited original", encoding="utf-8")

            states = workflow.stage_states(temp_dir, list(workflow.STAGE_ORDER))
            self.assertEqual(states["correct"], "stale")
            self.assertEqual(states["translate"], "stale")

    def test_recorded_lineage_takes_precedence_over_legacy_timing_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.srt"
            corrected = Path(temp_dir) / "corrected.srt"
            lineage = Path(temp_dir) / "correct_lineage.json"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nKeep\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nDeleted\n",
                encoding="utf-8",
            )
            corrected.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nKeep.\n",
                encoding="utf-8",
            )
            lineage.write_text(
                json.dumps(
                    {
                        "parent_path": str(source.resolve()),
                        "child_path": str(corrected.resolve()),
                        "edges": [{"parent": 0, "children": [0]}],
                    }
                ),
                encoding="utf-8",
            )
            rows = subtitle_comparison.comparison_rows(
                {"source": str(source), "corrected": str(corrected)},
                {"corrected": str(lineage)},
            )
            self.assertEqual(len(rows), 2)
            self.assertTrue(any(row.get("source") == "Deleted" and not row.get("corrected") for row in rows))

    def test_dual_soft_subtitle_command_maps_and_labels_tracks(self):
        command = build_multi_soft_subtitle_command(
            "video.mp4",
            [
                {"path": "source.srt", "language": "en", "title": "Source", "default": False},
                {"path": "pl.srt", "language": "pl", "title": "Translation", "default": True},
            ],
            "output.mp4",
        )
        self.assertIn("1:0", command)
        self.assertIn("2:0", command)
        self.assertIn("language=eng", command)
        self.assertIn("language=pol", command)
        self.assertIn("-disposition:s:1", command)


if __name__ == "__main__":
    unittest.main()
