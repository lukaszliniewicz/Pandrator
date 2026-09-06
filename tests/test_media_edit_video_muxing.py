import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pandrator.logic.dubbing.video_muxing import build_removal_only_video_command


def test_removal_builder_with_audio_uses_exact_trim_concat_and_aac():
    command = build_removal_only_video_command(
        "input.mp4",
        "output.mp4",
        [(0, 1250), (2500, 3000)],
        has_audio=True,
    )
    rendered = " ".join(command)
    assert "trim=start=0.000:end=1.250" in rendered
    assert "atrim=start=2.500:end=3.000" in rendered
    assert "setpts=PTS-STARTPTS" in rendered
    assert "asetpts=PTS-STARTPTS" in rendered
    assert "concat=n=2:v=1:a=1" in rendered
    assert "-c:a aac" in rendered
    assert "-b:a 192k" in rendered
    assert "-map_metadata 0" in rendered
    assert "-movflags +faststart" in rendered


def test_removal_builder_without_audio_omits_audio_mapping():
    command = build_removal_only_video_command(
        "input.mp4",
        "output.mp4",
        [(100, 900)],
        has_audio=False,
    )
    rendered = " ".join(command)
    assert "concat=n=1:v=1:a=0" in rendered
    assert "[0:a]" not in rendered
    assert "-map [aout]" not in rendered
    assert "-map [vout]" in rendered


def test_removal_builder_integrates_output_scaling_into_complex_graph():
    command = build_removal_only_video_command(
        "input.mp4",
        "output.mp4",
        [(100, 900)],
        has_audio=True,
        video_resolution="720p",
    )
    rendered = " ".join(command)
    assert "[vconcat]scale=-2:720:flags=lanczos[vout]" in rendered
    assert " -vf " not in f" {rendered} "


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg integration requires ffmpeg and ffprobe",
)
def test_removal_command_renders_exact_duration_with_audio_and_scaling():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory, "source.mp4")
        output = Path(directory, "edited.mp4")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x480:r=25:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        command = build_removal_only_video_command(
            str(source),
            str(output),
            [(0, 1000), (2000, 3000)],
            has_audio=True,
            video_resolution="360p",
        )

        subprocess.run(command, check=True, capture_output=True)
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,height",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(probe.stdout)

        assert float(metadata["format"]["duration"]) == pytest.approx(2.0, abs=0.08)
        assert {stream["codec_type"] for stream in metadata["streams"]} == {
            "audio",
            "video",
        }
        video = next(
            stream for stream in metadata["streams"] if stream["codec_type"] == "video"
        )
        assert video["height"] == 360
