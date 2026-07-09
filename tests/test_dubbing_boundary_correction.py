import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from pandrator.logic.dubbing import boundary_correction, srt_utils, transcription


def _synthetic_boundary_audio(sample_rate: int = 1000) -> np.ndarray:
    audio = np.zeros(sample_rate * 2, dtype=np.float32)
    audio[int(0.775 * sample_rate) : int(0.800 * sample_rate)] = 1.0
    audio[int(1.000 * sample_rate) : int(1.100 * sample_rate)] = 1.0
    return audio


def _boundary_segments():
    return [
        {"start": 0.2, "end": 1.0, "text": "Hello."},
        {"start": 1.02, "end": 1.5, "text": "Next."},
    ]


class DubbingBoundaryCorrectionTests(unittest.TestCase):
    def test_check_and_correct_overlaps_clips_previous_segment(self):
        segments = [
            {"start": 0.0, "end": 1.1, "text": "First."},
            {"start": 1.0, "end": 2.0, "text": "Second."},
        ]

        corrections = boundary_correction.check_and_correct_overlaps(
            segments,
            boundary_correction.BoundaryCorrectionConfig(overlap_buffer=0.02),
        )

        self.assertEqual(corrections[0]["type"], "overlap")
        self.assertAlmostEqual(segments[0]["end"], 0.98)

    def test_process_whisperx_segments_moves_boundary_to_low_energy(self):
        config = boundary_correction.BoundaryCorrectionConfig(sample_rate=1000)

        corrected, corrections = boundary_correction.process_whisperx_segments(
            _boundary_segments(),
            _synthetic_boundary_audio(1000),
            config,
        )

        self.assertTrue(any(correction["type"] == "energy_boundary" for correction in corrections))
        self.assertLess(corrected[0]["end"], 1.0)
        self.assertAlmostEqual(corrected[0]["end"], 0.875)

    def test_correct_boundaries_from_json_file_writes_srt_and_report(self):
        config = boundary_correction.BoundaryCorrectionConfig(sample_rate=1000)

        with tempfile.TemporaryDirectory() as temp_dir:
            json_path = os.path.join(temp_dir, "clip.json")
            Path(json_path).write_text(json.dumps({"segments": _boundary_segments()}), encoding="utf-8")

            result = boundary_correction.correct_boundaries_from_json_file(
                json_path,
                os.path.join(temp_dir, "clip.wav"),
                config=config,
                audio_loader=lambda _path, _sample_rate: _synthetic_boundary_audio(1000),
            )

            self.assertTrue(os.path.exists(result.srt_path))
            self.assertTrue(os.path.exists(result.corrections_path))
            segments = srt_utils.parse_srt(Path(result.srt_path).read_text(encoding="utf-8"))
            self.assertEqual(segments[0].end_ms, 875)
            payload = json.loads(Path(result.corrections_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["corrections"][0]["type"], "energy_boundary")

    def test_transcribe_video_file_uses_whisperx_json_for_boundary_correction(self):
        commands = []
        audio = _synthetic_boundary_audio(16000)

        def fake_run(command, **_kwargs):
            commands.append(command)
            executable = Path(command[0]).name.lower()
            if executable.startswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"wav")
                return SimpleNamespace(stderr=b"")

            whisperx_index = command.index("whisperx")
            audio_path = Path(command[whisperx_index + 1])
            output_dir = Path(command[command.index("--output_dir") + 1])
            output_format = command[command.index("--output_format") + 1]
            self.assertEqual(output_format, "json")
            output_dir.joinpath(f"{audio_path.stem}.json").write_text(
                json.dumps({"segments": _boundary_segments()}),
                encoding="utf-8",
            )
            return SimpleNamespace(stderr=b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = os.path.join(temp_dir, "clip.mp4")
            Path(video_path).write_bytes(b"video")

            output_path = transcription.transcribe_video_file(
                temp_dir,
                video_path,
                {
                    "stt_language": "English",
                    "whisper_model": "large-v3",
                    "boundary_correction_enabled": True,
                    "subtitle_merge_threshold": 0,
                },
                run_func=fake_run,
                boundary_audio_loader=lambda _path, _sample_rate: audio,
            )

            self.assertTrue(output_path.endswith("clip_corrected.srt"))
            corrected_segments = srt_utils.parse_srt(Path(output_path).read_text(encoding="utf-8"))
            self.assertEqual(corrected_segments[0].end_ms, 875)
            whisperx_command = next(command for command in commands if "whisperx" in command)
            self.assertIn("--output_format", whisperx_command)


if __name__ == "__main__":
    unittest.main()
