"""The dubbed audio stream carries its language through every export stage."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pandrator.logic.dubbing.video_muxing import (
    audio_language_metadata,
    build_multi_soft_subtitle_command,
    build_replace_video_audio_command,
    build_video_transcode_command,
)


class AudioLanguageMetadataTests(unittest.TestCase):
    def test_known_language_names_the_stream(self):
        self.assertEqual(
            [
                "-metadata:s:a:0",
                "language=hun",
                "-metadata:s:a:0",
                "title=Hungarian",
                "-metadata:s:a:0",
                "handler_name=Hungarian",
            ],
            audio_language_metadata("hu"),
        )

    def test_three_letter_codes_name_the_stream_too(self):
        self.assertEqual(audio_language_metadata("en"), audio_language_metadata("eng"))
        self.assertIn("title=French", audio_language_metadata("fra"))

    def test_no_language_leaves_the_stream_alone(self):
        self.assertEqual([], audio_language_metadata(""))
        command = build_replace_video_audio_command("v.mp4", "a.wav", "o.mp4")
        self.assertFalse(any(item.startswith(("-metadata", "-map_metadata")) for item in command))

    def test_unknown_language_drops_inherited_tags(self):
        for language in ("auto", "und", "zz"):
            self.assertEqual(["-map_metadata:s:a:0", "-1"], audio_language_metadata(language))

    def test_replace_command_tags_before_the_output(self):
        command = build_replace_video_audio_command(
            "v.mp4", "a.wav", "o.mp4", audio_language="ja", shortest=False
        )
        self.assertEqual("o.mp4", command[-1])
        self.assertIn("language=jpn", command)
        self.assertLess(command.index("language=jpn"), command.index("o.mp4"))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required"
)
class AudioLanguageIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _run(self, command):
        subprocess.run(command, check=True, capture_output=True, timeout=120)

    def _audio_tags(self, path):
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream_tags", "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return json.loads(probe.stdout)["streams"][0].get("tags", {})

    def test_tags_survive_subtitle_remux_and_transcode(self):
        video = self.root / "source.mp4"
        speech = self.root / "speech.wav"
        subtitle = self.root / "track.srt"
        self._run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=black:s=64x64:r=25:d=1",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(video),
        ])
        self._run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            str(speech),
        ])
        subtitle.write_text("1\n00:00:00,000 --> 00:00:00,900\nSzia.\n", encoding="utf-8")

        dubbed = self.root / "dubbed.mp4"
        self._run(build_replace_video_audio_command(
            str(video), str(speech), str(dubbed), audio_language="hu"
        ))
        remuxed = self.root / "remuxed.mp4"
        self._run(build_multi_soft_subtitle_command(
            str(dubbed),
            [{"path": str(subtitle), "language": "hu", "title": "Hungarian", "default": "1"}],
            str(remuxed),
        ))
        transcoded = self.root / "transcoded.mp4"
        self._run(build_video_transcode_command(str(dubbed), str(transcoded)))

        for path in (dubbed, remuxed, transcoded):
            tags = self._audio_tags(path)
            self.assertEqual("hun", tags.get("language"), path.name)
            self.assertEqual("Hungarian", tags.get("handler_name"), path.name)

    def test_unknown_language_does_not_inherit_the_input_language(self):
        video = self.root / "source.mp4"
        speech = self.root / "speech.m4a"
        self._run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=black:s=64x64:r=25:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ])
        self._run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:a", "aac", "-metadata:s:a:0", "language=fra", str(speech),
        ])
        dubbed = self.root / "dubbed.mp4"
        self._run(build_replace_video_audio_command(
            str(video), str(speech), str(dubbed), audio_language="zz"
        ))
        self.assertEqual("und", self._audio_tags(dubbed).get("language", "und"))


if __name__ == "__main__":
    unittest.main()
