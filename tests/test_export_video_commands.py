"""Render policy keeps useful errors and never falls back after cancellation."""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from pandrator.web.export_video_commands import (
    VideoEncodingOptions,
    render_burned_subtitle_video,
    render_soft_subtitle_video,
    render_transcoded_video,
    render_web_video,
    run_video_command,
)


class VideoRenderPolicyTests(unittest.TestCase):
    def setUp(self):
        self.options = VideoEncodingOptions("ffmpeg", "libx264", 19, "fast", "720p", "copy", "256k")
        self.cancel_event = threading.Event()
        self.progress = mock.Mock()
        self.encoder_ids = self.enterContext(mock.patch(
            "pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}
        ))
        self.run = self.enterContext(mock.patch("pandrator.web.export_video_commands.run_cancellable"))

    def render(self, mode, *, transcode=False):
        if mode == "soft":
            return render_soft_subtitle_video(
                Path("source.mkv"), [{"path": "source.srt", "language": "en", "title": "English", "default": True}],
                Path("output.mp4"), self.options, transcode_video=transcode,
                progress=self.progress, cancel_event=self.cancel_event,
            )
        return render_web_video(
            Path("source.mkv"), Path("output.mp4"), self.options,
            tail_extension_ms=0, progress=self.progress, cancel_event=self.cancel_event,
        )

    @staticmethod
    def failure(stderr="first line\ncodec is incompatible\n", stdout=None):
        return subprocess.CalledProcessError(1, ["ffmpeg"], output=stdout, stderr=stderr)

    def test_successful_stream_copy_does_not_probe_or_start_fallback(self):
        for mode in ("soft", "web"):
            with self.subTest(mode=mode):
                self.run.reset_mock()
                self.assertFalse(self.render(mode))
                self.run.assert_called_once()
                command = self.run.call_args.args[0]
                self.assertEqual("copy", command[command.index("-c:v") + 1])
                self.encoder_ids.assert_not_called()

    def test_remux_failure_transcodes_with_source_resolution_and_requested_audio_bitrate(self):
        for mode in ("soft", "web"):
            with self.subTest(mode=mode):
                self.run.reset_mock()
                self.run.side_effect = [self.failure(), mock.DEFAULT]
                self.assertTrue(self.render(mode))
                self.assertEqual(2, self.run.call_count)
                command = self.run.call_args.args[0]
                self.assertEqual("libx264", command[command.index("-c:v") + 1])
                self.assertEqual("aac", command[command.index("-c:a") + 1])
                self.assertEqual("256k", command[command.index("-b:a") + 1])
                self.assertFalse(any("scale=" in argument for argument in command))
                if mode == "soft":
                    self.assertIn("source.srt", command)
                    self.assertIn("mov_text", command)

    def test_unavailable_fallback_encoder_preserves_last_diagnostic_line(self):
        self.encoder_ids.return_value = set()
        for mode in ("soft", "web"):
            with self.subTest(mode=mode):
                self.run.reset_mock()
                self.run.side_effect = self.failure()
                with self.assertRaisesRegex(RuntimeError, "fallback encoder is unavailable: codec is incompatible"):
                    self.render(mode)
                self.run.assert_called_once()

    def test_fallback_failure_reports_last_stderr_line(self):
        for mode, label in (("soft", "Selectable-subtitle"), ("web", "Web-compatible video")):
            with self.subTest(mode=mode):
                self.run.side_effect = [self.failure(), self.failure("noise\nencoder failed\n")]
                with self.assertRaisesRegex(RuntimeError, f"{label} fallback with libx264 failed: encoder failed"):
                    self.render(mode)

    def test_fallback_failure_uses_stdout_or_default_when_stderr_is_empty(self):
        for stdout, expected in (("noise\nstdout detail", "stdout detail"), (None, "FFmpeg returned a non-zero exit status")):
            with self.subTest(stdout=stdout):
                self.run.side_effect = [self.failure(), self.failure("", stdout)]
                with self.assertRaisesRegex(RuntimeError, expected):
                    self.render("web")

    def test_explicit_soft_transcode_failure_does_not_retry(self):
        self.run.side_effect = self.failure()
        with self.assertRaises(subprocess.CalledProcessError):
            self.render("soft", transcode=True)
        self.run.assert_called_once()

    def test_explicit_transcode_requires_encoder_before_launch(self):
        self.encoder_ids.return_value = set()
        with self.assertRaisesRegex(RuntimeError, "does not provide the libx264"):
            render_transcoded_video(
                Path("source.mp4"), Path("output.mp4"), self.options,
                progress=self.progress, cancel_event=self.cancel_event,
            )
        self.run.assert_not_called()

    def test_direct_transcode_preserves_error_detail(self):
        self.run.side_effect = self.failure()
        with self.assertRaisesRegex(RuntimeError, "Video transcoding with libx264 failed: codec is incompatible"):
            render_transcoded_video(
                Path("source.mp4"), Path("output.mp4"), self.options,
                progress=self.progress, cancel_event=self.cancel_event,
            )

    def test_burned_subtitles_use_capable_binary_and_preserve_error_detail(self):
        self.run.side_effect = self.failure()
        with mock.patch("pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles", return_value="ffmpeg-libass"):
            with self.assertRaisesRegex(RuntimeError, "Burned-subtitle transcoding with libx264 failed: codec is incompatible"):
                render_burned_subtitle_video(
                    Path("source.mp4"), Path("source.srt"), Path("output.mp4"), self.options,
                    language="en", progress=self.progress, cancel_event=self.cancel_event,
                )
        self.assertEqual("ffmpeg-libass", self.run.call_args.args[0][0])
        self.encoder_ids.assert_called_once_with("ffmpeg-libass")

    def test_cancellation_during_failed_remux_does_not_start_fallback(self):
        def canceled_failure(*_args, **_kwargs):
            self.cancel_event.set()
            raise self.failure()

        self.run.side_effect = canceled_failure
        with self.assertRaises(InterruptedError):
            self.render("web")
        self.run.assert_called_once()
        self.encoder_ids.assert_not_called()

    def test_cancellation_from_fallback_progress_does_not_start_fallback(self):
        self.run.side_effect = self.failure()
        self.progress.side_effect = lambda value, _detail: self.cancel_event.set() if value == 0.68 else None
        with self.assertRaises(InterruptedError):
            self.render("soft")
        self.run.assert_called_once()


class VideoProcessCancellationTests(unittest.TestCase):
    def test_running_child_is_stopped_and_reaped_on_cancellation(self):
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as executor:
            ready = Path(directory) / "ready"
            finished = Path(directory) / "finished"
            cancel_event = threading.Event()
            processes = []
            original_popen = subprocess.Popen

            def start_process(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                return process

            command = [
                sys.executable, "-c",
                "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30); pathlib.Path(sys.argv[2]).touch()",
                str(ready), str(finished),
            ]
            with mock.patch("pandrator.logic.cancellable_process.subprocess.Popen", side_effect=start_process):
                future = executor.submit(run_video_command, command, cancel_event)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "Child failed to start")
                    cancel_event.set()
                    with self.assertRaises(InterruptedError):
                        future.result(timeout=5)
                    self.assertEqual(1, len(processes))
                    self.assertIsNotNone(processes[0].poll())
                    self.assertFalse(finished.exists())
                finally:
                    cancel_event.set()
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
