"""Single-pass frozen-tail export: fast copy vs deferred render-filter tpad."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pandrator.logic.dubbing.video_muxing import (
    build_add_subtitles_command,
    build_multi_soft_subtitle_command,
    build_video_transcode_command,
    build_web_optimized_remux_command,
)
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


def _tpad_in(command: list[str]) -> bool:
    return any("tpad=stop_mode=clone" in part for part in command)


def _vf_value(command: list[str]) -> str:
    if "-vf" in command:
        return command[command.index("-vf") + 1]
    if "-filter_complex" in command:
        return command[command.index("-filter_complex") + 1]
    return ""


class TailFilterBuilderTests(unittest.TestCase):
    def test_transcode_backward_compat_and_tail(self):
        plain = build_video_transcode_command("in.mp4", "out.mp4")
        self.assertFalse(_tpad_in(plain))
        extended = build_video_transcode_command(
            "in.mp4", "out.mp4", tail_extension_seconds=0.56
        )
        self.assertIn("tpad=stop_mode=clone:stop_duration=0.560", " ".join(extended))

    def test_burned_tail_runs_before_subtitles(self):
        command = build_add_subtitles_command(
            "in.mp4", "sub.srt", "out.mp4",
            subtitle_mode="burned", tail_extension_seconds=0.44,
        )
        vf = _vf_value(command)
        self.assertIn("tpad=stop_mode=clone", vf)
        self.assertIn("subtitles=", vf)
        self.assertLess(vf.index("tpad="), vf.index("subtitles="))

    def test_soft_transcode_extends_but_remux_never_does(self):
        extended = build_multi_soft_subtitle_command(
            "in.mp4", [{"path": "a.srt", "language": "en", "title": "English"}],
            "out.mp4", transcode_video=True, tail_extension_seconds=0.5,
        )
        self.assertTrue(_tpad_in(extended))
        remux = build_multi_soft_subtitle_command(
            "in.mp4", [{"path": "a.srt", "language": "en", "title": "English"}],
            "out.mp4", transcode_video=False, tail_extension_seconds=0.5,
        )
        self.assertFalse(_tpad_in(remux))
        self.assertEqual("copy", remux[remux.index("-c:v") + 1])
        web = build_web_optimized_remux_command("in.mp4", "out.mp4")
        self.assertFalse(_tpad_in(web))


class SinglePassTailExportTests(unittest.TestCase):
    """Mocked-runner exports: count commands, inspect tpad placement, check cleanup."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = prepare_web_test_data_root(temporary.name)
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.session = SessionService(self.database).create(
            "Single pass tail", workflow_kind="voiceover", source_language="en"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()
        self.output_dir = self.session_dir / "exports"
        self.source_path = self.session_dir / "source.mp4"
        self.source_path.write_bytes(b"original source video")
        self.source = self.artifacts.register(
            self.source_path, kind="source", role="upload", session_id=self.session.id
        )
        self.speech_path = self.session_dir / "speech.wav"
        self.speech_path.write_bytes(b"original generated speech")
        self.speech = self.artifacts.register(
            self.speech_path, kind="audio", role="assembled_audio", session_id=self.session.id
        )
        self.base_settings = {
            "export_mode": "media",
            "audio_mode": "dubbing_only",
            "subtitle_mode": "none",
            "video_tail_extension_policy": "extend",
        }
        self.cancel_event = threading.Event()
        self.commands: list[list[str]] = []
        self.command_outputs: list[Path] = []
        self.video_dur = 1.0
        self.speech_dur = 2.0
        self.enterContext(mock.patch.dict("os.environ", {
            "PANDRATOR_FFMPEG_EXE": "ffmpeg",
            "PANDRATOR_FFPROBE_EXE": "ffprobe",
        }))
        self.enterContext(mock.patch(
            "pandrator.logic.dubbing.audio_sync.media_has_audio_stream", return_value=True
        ))
        self.enterContext(mock.patch(
            "pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}
        ))
        self.enterContext(mock.patch(
            "pandrator.web.soundtrack_export.probe_soundtrack_media", side_effect=self.probe
        ))
        self.enterContext(mock.patch(
            "pandrator.web.export_video_commands.run_cancellable", side_effect=self.run_media_command
        ))
        self.fast_mock = self.enterContext(mock.patch(
            "pandrator.web.export_video._attempt_fast_video_tail", side_effect=self.fake_fast
        ))
        self.fast_result: bool | BaseException = False
        self.fast_calls: list[dict] = []

    def probe(self, path):
        is_video = Path(path) == self.source_path
        return {
            "duration": self.video_dur if is_video else self.speech_dur,
            "video_duration": self.video_dur if is_video else None,
            "has_video": is_video,
            "has_audio": True,
            "fps": 25.0 if is_video else None,
        }

    def fake_fast(self, **kwargs):
        self.fast_calls.append(kwargs)
        result = self.fast_result
        if isinstance(result, BaseException):
            raise result
        if result:
            Path(kwargs["destination"]).write_bytes(b"fast extended video")
            return True
        return False

    def run_media_command(self, command, **_kwargs):
        destination = Path(command[-1])
        self.commands.append(list(command))
        self.command_outputs.append(destination)
        destination.write_bytes(b"rendered media")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def export(self, settings):
        return self.handlers.export(
            {"session_id": self.session.id, "settings": settings},
            lambda *_: None,
            self.cancel_event,
        )

    def exported_metadata(self, result):
        _record, path = self.artifacts.resolve(result["artifact_ids"][0])

        from pandrator.web.models import Artifact

        with self.database.session() as session:
            artifact = session.get(Artifact, result["artifact_ids"][0])
            assert artifact is not None
            return dict(artifact.metadata_json or {}), path

    def tpad_commands(self):
        return [cmd for cmd in self.commands if _tpad_in(cmd)]

    def assert_no_scratch(self):
        leftovers = [
            str(p.relative_to(self.output_dir))
            for p in self.output_dir.rglob("*")
            if p.name.startswith(".")
        ]
        self.assertEqual([], leftovers)
        for output in self.command_outputs:
            self.assertFalse(output.exists(), str(output))

    def test_fast_success_avoids_full_preparation_encode(self):
        self.fast_result = True
        result = self.export(dict(self.base_settings))
        metadata, _ = self.exported_metadata(result)
        # 1.04 s tail for a 1 s overrun at 25 fps.
        self.assertEqual(1040, metadata["tail_extension_ms"])
        self.assertEqual("stream_copy_tail", metadata["tail_extension_method"])
        # Fast body-copy tail is not a full original reencode.
        self.assertFalse(metadata["video_transcoded"])
        self.assertIsNone(metadata["video_encoder"])
        self.assertEqual("source", metadata["video_resolution"])
        # No preparatory full tpad encode: audio replacement + web remux only.
        self.assertEqual(1, len(self.fast_calls))
        self.assertEqual([], self.tpad_commands())
        self.assertEqual(2, len(self.commands))
        self.assert_no_scratch()

    def test_fast_fallback_runs_old_full_tpad_once(self):
        self.fast_result = False
        result = self.export(dict(self.base_settings))
        metadata, _ = self.exported_metadata(result)
        self.assertEqual(1040, metadata["tail_extension_ms"])
        self.assertEqual("full_transcode", metadata["tail_extension_method"])
        self.assertTrue(metadata["video_transcoded"])
        self.assertEqual("libx264", metadata["video_encoder"])
        # Exactly one tpad (preparation fallback); final remux must not extend again.
        self.assertEqual(1, len(self.tpad_commands()))
        self.assertEqual(3, len(self.commands))
        self.assert_no_scratch()

    def test_fast_cancel_propagates_without_fallback(self):
        self.fast_result = InterruptedError("Video export canceled.")
        with self.assertRaises(InterruptedError):
            self.export(dict(self.base_settings))
        self.assertEqual(1, len(self.fast_calls))
        self.assertEqual([], self.commands)
        self.assert_no_scratch()

    def test_explicit_transcode_defers_tail_to_single_final_encode(self):
        self.fast_result = True  # must be ignored when deferring
        settings = dict(self.base_settings, video_transcode=True)
        result = self.export(settings)
        metadata, _ = self.exported_metadata(result)
        self.assertEqual("render_filter", metadata["tail_extension_method"])
        self.assertTrue(metadata["video_transcoded"])
        self.assertEqual([], self.fast_calls)
        # Audio replacement + one final transcode; only the final has tpad.
        self.assertEqual(2, len(self.commands))
        tpad = self.tpad_commands()
        self.assertEqual(1, len(tpad))
        self.assertIn("-render-", tpad[0][-1])
        self.assert_no_scratch()

    def test_resize_defers_tail_to_single_final_encode(self):
        settings = dict(self.base_settings, burn_video_resolution="720p")
        result = self.export(settings)
        metadata, _ = self.exported_metadata(result)
        self.assertEqual("render_filter", metadata["tail_extension_method"])
        self.assertEqual("720p", metadata["video_resolution"])
        self.assertEqual([], self.fast_calls)
        self.assertEqual(1, len(self.tpad_commands()))
        self.assertEqual(2, len(self.commands))
        vf = _vf_value(self.tpad_commands()[0])
        self.assertIn("scale=", vf)
        self.assertIn("tpad=", vf)
        self.assert_no_scratch()

    def test_burned_subtitles_extend_in_single_pass_before_captions(self):
        path = self.session_dir / "source.srt"
        path.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nA line past the freeze.\n",
            encoding="utf-8",
        )
        self.artifacts.register(
            path, kind="srt", role="correction", session_id=self.session.id
        )
        settings = dict(self.base_settings, subtitle_mode="burned")
        with mock.patch(
            "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles",
            return_value="ffmpeg",
        ):
            result = self.export(settings)
        metadata, _ = self.exported_metadata(result)
        # Burned forces a full render: tail defers to the final filtergraph,
        # exactly one video encode carries tpad BEFORE the burned captions.
        self.assertEqual("render_filter", metadata["tail_extension_method"])
        self.assertTrue(metadata["video_transcoded"])
        self.assertEqual([], self.fast_calls)
        self.assertEqual(2, len(self.commands))
        tpad = self.tpad_commands()
        self.assertEqual(1, len(tpad))
        vf = _vf_value(tpad[0])
        self.assertIn("tpad=", vf)
        self.assertIn("subtitles=", vf)
        self.assertLess(vf.index("tpad="), vf.index("subtitles="))
        self.assert_no_scratch()

    def test_burned_command_order_with_real_builder(self):
        # Burned single-pass order is enforced at the builder level (tpad
        # BEFORE subtitles) so captions during the extended tail render.
        with mock.patch(
            "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles",
            return_value="ffmpeg",
        ), mock.patch(
            "pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}
        ), mock.patch(
            "pandrator.web.export_video_commands.run_cancellable",
            side_effect=lambda cmd, **_: self.commands.append(list(cmd)),
        ):
            from pandrator.web.export_video_commands import (
                VideoEncodingOptions,
                render_burned_subtitle_video,
            )

            options = VideoEncodingOptions(
                "ffmpeg", "libx264", 18, "balanced", "source", "copy", "192k"
            )
            render_burned_subtitle_video(
                Path("in.mp4"), Path("sub.srt"), Path("out.mp4"), options,
                language="en", tail_extension_seconds=0.5,
                progress=lambda *_: None, cancel_event=threading.Event(),
            )
        vf = _vf_value(self.commands[-1])
        self.assertIn("tpad=", vf)
        self.assertLess(vf.index("tpad="), vf.index("subtitles="))

    def test_soft_transcode_extends_in_single_pass(self):
        from pandrator.web.export_video_commands import (
            VideoEncodingOptions,
            render_soft_subtitle_video,
        )

        options = VideoEncodingOptions(
            "ffmpeg", "libx264", 18, "balanced", "source", "copy", "192k"
        )
        with mock.patch(
            "pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}
        ), mock.patch(
            "pandrator.web.export_video_commands.run_cancellable",
            side_effect=lambda cmd, **_: self.commands.append(list(cmd)),
        ):
            render_soft_subtitle_video(
                Path("in.mp4"),
                [{"path": "a.srt", "language": "en", "title": "English", "default": True}],
                Path("out.mp4"), options, transcode_video=True,
                tail_extension_seconds=0.6,
                progress=lambda *_: None, cancel_event=threading.Event(),
            )
        self.assertTrue(_tpad_in(self.commands[-1]))

    def test_fallback_remux_transcode_does_not_double_extend(self):
        from pandrator.web.export_video_commands import (
            VideoEncodingOptions,
            render_soft_subtitle_video,
            render_web_video,
        )

        options = VideoEncodingOptions(
            "ffmpeg", "libx264", 18, "balanced", "720p", "copy", "192k"
        )
        calls: list[list[str]] = []

        def fail_once_then_ok(command, **_kwargs):
            calls.append(list(command))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, command, stderr="incompatible")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch(
            "pandrator.web.capabilities.ffmpeg_video_encoder_ids", return_value={"libx264"}
        ), mock.patch(
            "pandrator.web.export_video_commands.run_cancellable", side_effect=fail_once_then_ok
        ):
            render_web_video(
                Path("already-extended.mp4"), Path("out.mp4"), options,
                tail_extension_ms=1040,
                progress=lambda *_: None, cancel_event=threading.Event(),
            )
            calls.clear()
            render_soft_subtitle_video(
                Path("already-extended.mp4"),
                [{"path": "a.srt", "language": "en", "title": "English", "default": True}],
                Path("out2.mp4"), options, transcode_video=False,
                progress=lambda *_: None, cancel_event=threading.Event(),
            )
        # Both fallbacks transcode for compatibility but must not add tpad:
        # the source was already extended in preparation.
        for cmd in calls[1:]:
            self.assertFalse(_tpad_in(cmd))

    def test_sub_tolerance_overrun_retains_audio_without_freeze(self):
        self.video_dur = 3.0
        self.speech_dur = 3.03
        self.fast_result = True
        result = self.export(dict(self.base_settings))
        metadata, _ = self.exported_metadata(result)
        self.assertEqual(0, metadata["tail_extension_ms"])
        self.assertEqual("none", metadata["tail_extension_method"])
        self.assertFalse(metadata["video_transcoded"])
        self.assertEqual([], self.fast_calls)
        # Audio replacement must not use -shortest so the 30 ms tail survives.
        audio_cmd = self.commands[0]
        self.assertNotIn("-shortest", audio_cmd)
        self.assert_no_scratch()


if __name__ == "__main__":
    unittest.main()
