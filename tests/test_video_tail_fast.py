"""Focused unit + real-FFmpeg regressions for the tail stream-copy fast path."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from pandrator.web.video_tail_fast import (
    _eligible_info,
    _level_name,
    _parse_ratio,
    _tail_plan,
    try_fast_video_tail,
)


def _base_stream(**overrides):
    stream = {
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": 1280,
        "height": 720,
        "sample_aspect_ratio": "1:1",
        "level": 31,
        "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1",
        "time_base": "1/12800",
        "start_time": "0.000000",
        "duration": "30.000000",
        "nb_frames": "750",
    }
    stream.update(overrides)
    return stream


class ParseHelperTests(unittest.TestCase):
    def test_parse_ratio(self):
        self.assertEqual(_parse_ratio("30000/1001"), (30000, 1001))
        self.assertIsNone(_parse_ratio("0/0"))
        self.assertIsNone(_parse_ratio("25"))
        self.assertIsNone(_parse_ratio(None))

    def test_level_name(self):
        self.assertEqual(_level_name(31), "3.1")
        self.assertEqual(_level_name("40"), "4.0")
        self.assertEqual(_level_name("3.1"), "3.1")
        self.assertIsNone(_level_name("unknown"))
        self.assertIsNone(_level_name(None))

    def test_tail_plan_minimal_ceil_no_extra_frame(self):
        # The caller (resolve_video_tail_extension_ms) already adds the
        # safety frame, so 0.84s at 25fps is exactly 21 frames, not 22.
        frames, duration = _tail_plan(0.84, 25.0)
        self.assertEqual(frames, 21)
        self.assertAlmostEqual(duration, 21 / 25)
        # Exact frame multiples stay minimal despite float dust...
        self.assertEqual(_tail_plan(13 / 25, 25.0)[0], 13)
        # ...while genuine fractions still round up.
        self.assertEqual(_tail_plan(0.51, 25.0)[0], 13)


class EligibilityTests(unittest.TestCase):
    def test_prototype_source_is_eligible(self):
        info = _eligible_info(_base_stream())
        self.assertEqual(info["fps"], 25.0)
        self.assertEqual(info["profile"], "high")
        self.assertEqual(info["level"], "3.1")
        self.assertEqual(info["timescale"], 12800)

    def test_rejections_fall_back(self):
        from pandrator.web.video_tail_fast import _Ineligible

        cases = [
            {"codec_name": "hevc"},
            {"codec_name": "mpeg4"},
            {"profile": "High 10"},
            {"pix_fmt": "yuv444p"},
            {"width": 1279},
            {"sample_aspect_ratio": "4:3"},
            {"color_transfer": "smpte2084"},
            {"color_primaries": "bt2020"},
            {"r_frame_rate": "0/0"},
            {"r_frame_rate": "25/1", "avg_frame_rate": "30/1"},
            {"time_base": "0/0"},
            {"start_time": "1.000000"},
            {"duration": "N/A", "nb_frames": "N/A"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(_Ineligible):
                _eligible_info(_base_stream(**overrides))

    def test_rotation_and_hdr_side_data_rejected(self):
        from pandrator.web.video_tail_fast import _Ineligible

        rotated = _base_stream()
        rotated["tags"] = {"rotate": "90"}
        with self.assertRaises(_Ineligible):
            _eligible_info(rotated)
        hdr = _base_stream()
        hdr["side_data_list"] = [{"side_data_type": "Mastering display metadata"}]
        with self.assertRaises(_Ineligible):
            _eligible_info(hdr)


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _libx264_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "encoder=libx264"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@unittest.skipUnless(_ffmpeg_available() and _libx264_available(), "FFmpeg + libx264 required")
class FastTailIntegrationTests(unittest.TestCase):
    """Small real-FFmpeg runs; clips are seconds long with tiny frames."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    @staticmethod
    def _run(command: list[str], cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise InterruptedError("canceled")
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
        if cancel_event.is_set():
            raise InterruptedError("canceled")

    def _make_source(
        self,
        name: str,
        *,
        duration: float = 3.0,
        fps: str = "25",
        size: str = "160x120",
        profile: str = "high",
        audio_duration: float | None = None,
        codec: str = "libx264",
    ) -> Path:
        path = self.root / name
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate={fps}:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=8000:duration={audio_duration or duration}",
            "-t",
            str(max(duration, audio_duration or 0)),
            "-c:v",
            codec,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ]
        if codec == "libx264":
            command += ["-profile:v", profile]
        command.append(str(path))
        subprocess.run(command, check=True, capture_output=True, timeout=180)
        return path

    def _probe_video(self, path: Path) -> dict:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,profile,width,height,time_base,start_time,duration,nb_frames",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(result.stdout)["streams"][0]

    def _framemd5(self, path: Path) -> list[list[str]]:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "warning",
                "-threads",
                "2",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-an",
                "-fps_mode",
                "passthrough",
                "-f",
                "framemd5",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        rows = []
        for line in result.stdout.decode().splitlines():
            if line and not line.startswith("#"):
                rows.append([item.strip() for item in line.split(",")])
        return rows

    def _try(self, source: Path, extra: float, name: str = "out.mp4") -> tuple[bool, Path]:
        destination = self.root / name
        ok = try_fast_video_tail(
            source,
            destination,
            scratch_dir=self.scratch,
            extra_seconds=extra,
            ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
            cancel_event=threading.Event(),
            run_command=self._run,
        )
        return ok, destination

    def test_h264_body_preserved_and_tail_continuous(self):
        source = self._make_source("src.mp4")
        ok, destination = self._try(source, 0.52)
        self.assertTrue(ok)
        self.assertTrue(destination.is_file())
        src_info = self._probe_video(source)
        out_info = self._probe_video(destination)
        self.assertEqual(out_info["codec_name"], "h264")
        src_frames = self._framemd5(source)
        out_frames = self._framemd5(destination)
        # 3s at 25fps + ceil(0.52*25) = 75 + 13 tail frames (minimal ceil:
        # the caller's shared tail math already holds the safety frame).
        self.assertEqual(len(out_frames), len(src_frames) + 13)
        for original, joined in zip(src_frames, out_frames, strict=False):
            self.assertEqual(original[-1], joined[-1])
            self.assertEqual(original[2], joined[2])
        pts = [int(row[2]) for row in out_frames]
        self.assertTrue(all(b - a == 1 for a, b in zip(pts, pts[1:], strict=False)))
        self.assertAlmostEqual(
            float(out_info["duration"]),
            float(src_info["duration"]) + 13 / 25,
            delta=0.09,
        )

    def test_supported_profiles(self):
        for profile in ("baseline", "main", "high"):
            with self.subTest(profile=profile):
                source = self._make_source(f"{profile}.mp4", profile=profile)
                ok, destination = self._try(source, 0.3, name=f"{profile}-out.mp4")
                self.assertTrue(ok)
                self.assertTrue(destination.is_file())

    def test_noninteger_fps(self):
        source = self._make_source("ntsc.mp4", duration=2.0, fps="30000/1001")
        ok, destination = self._try(source, 0.4)
        self.assertTrue(ok)
        out_frames = self._framemd5(destination)
        src_frames = self._framemd5(source)
        self.assertGreater(len(out_frames), len(src_frames))
        for original, joined in zip(src_frames, out_frames, strict=False):
            self.assertEqual(original[-1], joined[-1])

    def test_long_source_audio_uses_video_end(self):
        # Audio (8s) outlasts video (3s); the seek and body must track video.
        source = self._make_source("longaudio.mp4", duration=3.0, audio_duration=8.0)
        ok, destination = self._try(source, 0.5)
        self.assertTrue(ok)
        out_info = self._probe_video(destination)
        self.assertAlmostEqual(float(out_info["duration"]), 3.0 + 13 / 25, delta=0.12)

    def test_unsupported_codec_falls_back_and_cleans(self):
        source = self._make_source("mpeg4.mp4", codec="mpeg4")
        ok, destination = self._try(source, 0.5)
        self.assertFalse(ok)
        self.assertFalse(destination.exists())

    def test_paths_with_spaces_quotes_and_unicode(self):
        weird = self.root / "we ird's caf\u00e9 \u65e5\u672c\u8a9e"
        weird.mkdir()
        source = self._make_source("src.mp4")
        spaced = weird / "so urce's vid\u00e9o.mp4"
        shutil.copyfile(source, spaced)
        scratch = weird / "scra tch"
        scratch.mkdir()
        destination = weird / "o ut's fin\u00e9.mp4"
        ok = try_fast_video_tail(
            spaced,
            destination,
            scratch_dir=scratch,
            extra_seconds=0.3,
            ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
            cancel_event=threading.Event(),
            run_command=self._run,
        )
        self.assertTrue(ok)
        self.assertTrue(destination.is_file())

    def test_destination_equal_source_refuses(self):
        source = self._make_source("noself.mp4")
        before = source.read_bytes()
        ok, _destination = self._try(source, 0.3, name="noself.mp4")
        self.assertFalse(ok)
        self.assertEqual(source.read_bytes(), before)

    def test_mid_run_cancel_propagates_without_destination(self):
        source = self._make_source("midcancel.mp4")
        calls = 0

        def _cancel_midway(command: list[str], cancel_event: threading.Event) -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise InterruptedError("canceled mid-run")
            self._run(command, cancel_event)

        destination = self.root / "midcancel-out.mp4"
        with self.assertRaises(InterruptedError):
            try_fast_video_tail(
                source,
                destination,
                scratch_dir=self.scratch,
                extra_seconds=0.3,
                ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
                cancel_event=threading.Event(),
                run_command=_cancel_midway,
            )
        self.assertFalse(destination.exists())

    def test_cancel_propagates(self):
        source = self._make_source("cancel.mp4")
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            try_fast_video_tail(
                source,
                self.root / "cancel-out.mp4",
                scratch_dir=self.scratch,
                extra_seconds=0.5,
                ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
                cancel_event=event,
                run_command=self._run,
            )

    def test_invalid_extra_seconds_falls_back(self):
        source = self._make_source("zero.mp4")
        ok, _destination = self._try(source, 0.0)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
