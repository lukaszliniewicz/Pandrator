"""Focused regressions for one-click export chaining and frozen-tail recovery."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave

from pandrator.logic.dubbing.video_muxing import (
    build_replace_video_audio_command,
    build_video_tail_extension_command,
    video_tail_extension_seconds,
)
from pandrator.web.soundtrack_export import (
    VideoTailExtensionRequired,
    resolve_video_tail_extension_ms,
)


class TailMathTests(unittest.TestCase):
    def test_default_policy_asks_for_true_overrun(self):
        with self.assertRaisesRegex(
            VideoTailExtensionRequired,
            r"Generated speech is 5\.00 seconds longer than the video\. "
            r"Freeze the last frame and extend the video by 5\.04 seconds",
        ):
            resolve_video_tail_extension_ms(
                reference_duration=3.0,
                generated_duration=8.0,
                fps=25.0,
                settings={},
            )

    def test_extend_policy_has_no_legacy_cap(self):
        self.assertEqual(
            resolve_video_tail_extension_ms(
                reference_duration=3.0,
                generated_duration=8.0,
                fps=25.0,
                settings={
                    "video_tail_extension_policy": "extend",
                    "video_tail_extension_max_ms": 0,
                },
            ),
            round(5.04 * 1000),
        )

    def test_invalid_policy_is_rejected_even_when_timeline_fits(self):
        for bad in (None, "", "askk", 0, [], True):
            with self.subTest(value=bad), self.assertRaisesRegex(
                ValueError, r"video_tail_extension_policy must be ask or extend\."
            ):
                resolve_video_tail_extension_ms(
                    reference_duration=3.0,
                    generated_duration=3.04,
                    fps=25.0,
                    settings={"video_tail_extension_policy": bad},
                )

    def test_tail_seconds_ceils_to_frames_plus_one(self):
        # 0.51 s at 25 fps: ceil(12.75) + 1 extra frame = 14 frames.
        self.assertAlmostEqual(video_tail_extension_seconds(0.51, 25.0), 14 / 25)
        self.assertGreaterEqual(video_tail_extension_seconds(0.51, 25.0), 0.51)

    def test_tail_seconds_without_fps_still_covers(self):
        self.assertGreaterEqual(video_tail_extension_seconds(0.51, None), 0.51)

    def test_tail_seconds_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            video_tail_extension_seconds(0, 25.0)

    def test_resolve_returns_zero_when_timeline_fits(self):
        self.assertEqual(
            resolve_video_tail_extension_ms(
                reference_duration=3.0,
                generated_duration=3.04,
                fps=25.0,
                settings={},
            ),
            0,
        )

    def test_extend_policy_covers_small_overrun(self):
        extension = resolve_video_tail_extension_ms(
            reference_duration=3.0,
            generated_duration=3.51,
            fps=25.0,
            settings={"video_tail_extension_policy": "extend"},
        )
        self.assertEqual(extension, round(14 / 25 * 1000))


class TailCommandTests(unittest.TestCase):
    def test_tail_command_freezes_last_frame_without_shortest(self):
        command = build_video_tail_extension_command(
            "in.mp4", "out.mp4", 0.56, video_encoder="libx264"
        )
        joined = " ".join(command)
        self.assertIn("tpad=stop_mode=clone", joined)
        self.assertNotIn("-shortest", command)
        self.assertIn("libx264", command)

    def test_replace_command_shortest_flag(self):
        with_shortest = build_replace_video_audio_command("v.mp4", "a.wav", "o.mp4")
        self.assertIn("-shortest", with_shortest)
        without_shortest = build_replace_video_audio_command(
            "v.mp4", "a.wav", "o.mp4", shortest=False
        )
        self.assertNotIn("-shortest", without_shortest)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required"
)
class ExportChainingAndTailIntegrationTests(unittest.TestCase):
    """Tiny real-ffmpeg runs: no user exports, no models, seconds-long media."""

    def setUp(self):
        from pandrator.web.api import create_app
        from pandrator.web.auth import BootstrapTokenStore

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.addCleanup(self.app.extensions["pandrator"]["database"].dispose)
        self.client = self.app.test_client()
        # Start the token lifetime after potentially slow app setup.
        token = bootstrap.issue()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]["services"]
        self.handlers = self.services.workflow_handlers
        self.workflows = self.services.workflows

    def _voiceover_session(self, name="Tail integration"):
        sid = self.client.post(
            "/api/v1/sessions",
            json={"name": name, "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        return sid

    def _attach_upload(self, sid, path, role="upload", display="recording.mp4"):
        artifact = self.services.artifacts.register(
            path, kind="mp4", role=role, session_id=sid
        )
        asset = self.services.source_library.ensure_for_artifact(
            artifact.id, display_name=display
        )
        self.services.source_library.attach(
            sid, asset["id"] if isinstance(asset, dict) else asset.id
        )
        return artifact

    @staticmethod
    def _make_video(path, duration, with_audio=True):
        command = [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i",
            f"color=black:s=64x64:r=25:d={duration}",
        ]
        if with_audio:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        command += [
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
        ]
        if with_audio:
            command += ["-c:a", "aac"]
        else:
            command += ["-an"]
        command.append(str(path))
        subprocess.run(command, check=True, capture_output=True, timeout=120)

    @staticmethod
    def _write_speech(path, duration):
        rate = 48000
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(
                b"".join(
                    struct.pack(
                        "<h",
                        int(4000 * math.sin(2 * math.pi * 440 * index / rate)),
                    )
                    for index in range(round(duration * rate))
                )
            )

    def _register_speech(self, sid, duration):
        path = self.services.paths.uploads / f"speech-{duration}.wav"
        self._write_speech(path, duration)
        return self.services.artifacts.register(
            path, kind="audio", role="dubbing_audio", session_id=sid
        )

    def _export_result(self, sid, settings):
        from pandrator.web.models import Artifact

        settings = {"video_tail_extension_policy": "extend", **settings}
        result = self.handlers.export(
            {"session_id": sid, "settings": settings},
            lambda *_: None,
            threading.Event(),
        )
        self.assertEqual(len(result["artifact_ids"]), 1)
        with self.services.database.session() as session:
            artifact = session.get(Artifact, result["artifact_ids"][0])
            assert artifact is not None
            metadata = dict(artifact.metadata_json or {})
            relative = artifact.relative_path
        return metadata, relative

    def test_resolve_stage_routes_export_with_run_to_variant(self):
        from pandrator.web.models import (
            GenerationPlan,
            GenerationPlanRevision,
            GenerationRun,
        )

        sid = self._voiceover_session()
        video = self.services.paths.uploads / "routing.mp4"
        self._make_video(video, 2)
        self._attach_upload(sid, video)
        with self.services.database.session() as session:
            plan = GenerationPlan(session_id=sid)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                settings_json={},
                content_hash="routing-plan",
            )
            session.add(revision)
            session.flush()
            plan.active_revision_id = revision.id
            run = GenerationRun(
                session_id=sid,
                plan_revision_id=revision.id,
                sequence_number=1,
                status="completed",
            )
            session.add(run)
            session.flush()
            run_id = run.id
        # Canonical modes and normalized aliases share one routing point:
        # "dubbed" normalizes to "dubbing_only", "source" to "preserve".
        cases = [
            ("mixed", "export.variant"),
            ("dubbing_only", "export.variant"),
            ("dubbed", "export.variant"),
            ("preserve", "export.create"),
            ("source", "export.create"),
        ]
        for audio_mode, expected_kind in cases:
            with self.subTest(audio_mode=audio_mode):
                routed = self.workflows.resolve_stage(
                    sid,
                    "export",
                    {
                        "export_mode": "media",
                        "audio_mode": audio_mode,
                        "generation_run_id": run_id,
                    },
                )
                self.assertEqual(routed.job_kind, expected_kind)
                self.assertEqual(
                    routed.payload["settings"]["generation_run_id"], run_id
                )
        direct = self.workflows.resolve_stage(
            sid, "export", {"export_mode": "media", "audio_mode": "mixed"}
        )
        self.assertEqual(direct.job_kind, "export.create")

    def test_dubbed_tail_freeze_covers_small_overrun(self):
        from pandrator.web.soundtrack_export import probe_soundtrack_media

        sid = self._voiceover_session()
        video = self.services.paths.uploads / "tail-source.mp4"
        self._make_video(video, 2)
        self._attach_upload(sid, video)
        speech = self._register_speech(sid, 2.6)
        _record, speech_path = self.handlers._resolve_input(speech.id)
        reference = probe_soundtrack_media(video)
        generated = probe_soundtrack_media(speech_path)
        expected_tail = resolve_video_tail_extension_ms(
            reference_duration=reference["duration"],
            generated_duration=generated["duration"],
            fps=reference["fps"],
            settings={"video_tail_extension_policy": "extend"},
        )
        self.assertGreater(expected_tail, 0)
        metadata, relative = self._export_result(
            sid,
            {
                "export_mode": "media",
                "audio_mode": "dubbing_only",
                "subtitle_mode": "none",
            },
        )
        self.assertEqual(metadata["tail_extension_ms"], expected_tail)
        self.assertTrue(metadata["video_transcoded"])
        info = probe_soundtrack_media(self.services.paths.managed_path(relative))
        self.assertTrue(info["has_video"])
        self.assertGreaterEqual(info["duration"], generated["duration"] - 0.05)

    def test_mixed_tail_freeze_with_default_cap(self):
        from pandrator.web.soundtrack_export import probe_soundtrack_media

        sid = self._voiceover_session()
        video = self.services.paths.uploads / "mixed-source.mp4"
        self._make_video(video, 2)
        self._attach_upload(sid, video)
        self._register_speech(sid, 2.51)
        metadata, relative = self._export_result(
            sid,
            {
                "export_mode": "media",
                "audio_mode": "mixed",
                "subtitle_mode": "none",
            },
        )
        self.assertGreater(metadata["tail_extension_ms"], 0)
        self.assertTrue(metadata["video_transcoded"])
        info = probe_soundtrack_media(self.services.paths.managed_path(relative))
        self.assertGreaterEqual(info["duration"], 2.51)

    def test_sub_tolerance_overrun_keeps_all_speech_without_freeze(self):
        from pandrator.web.soundtrack_export import probe_soundtrack_media

        for audio_mode in ("dubbing_only", "mixed"):
            with self.subTest(audio_mode=audio_mode):
                sid = self._voiceover_session(name=f"Sub-tolerance {audio_mode}")
                video = self.services.paths.uploads / f"subtol-{audio_mode}.mp4"
                self._make_video(video, 3)
                self._attach_upload(sid, video)
                speech = self._register_speech(sid, 3.03)
                _record, speech_path = self.handlers._resolve_input(speech.id)
                reference = probe_soundtrack_media(video)
                generated = probe_soundtrack_media(speech_path)
                overrun = generated["duration"] - (
                    reference.get("video_duration") or reference["duration"]
                )
                self.assertGreater(overrun, 0)
                self.assertLessEqual(overrun, 0.05)
                metadata, relative = self._export_result(
                    sid,
                    {
                        "export_mode": "media",
                        "audio_mode": audio_mode,
                        "subtitle_mode": "none",
                    },
                )
                self.assertEqual(metadata["tail_extension_ms"], 0)
                self.assertFalse(metadata["video_transcoded"])
                info = probe_soundtrack_media(
                    self.services.paths.managed_path(relative)
                )
                # probe "duration" tracks the VIDEO stream when one exists, so a
                # sub-tolerance mux (frozen-length video + full speech audio)
                # reports the video length there. Speech survival is proven by
                # the AUDIO stream duration: the -shortest trim gives ~2.99s
                # here and must keep failing this bound.
                self.assertGreaterEqual(
                    info["audio_duration"], generated["duration"] - 0.01
                )
                if audio_mode == "mixed":
                    # Content guard: the mixed master must carry real speech
                    # past the reference length, not apad silence backfill
                    # after an amix duration=first cut. The master tail is
                    # decoded to float32 via FFmpeg because stdlib wave
                    # cannot parse WAVE_FORMAT_EXTENSIBLE headers; silence
                    # RMS is 0 while the normalized sine tail is ~0.12, so
                    # 0.01 preserves the previous int24 threshold of 100000
                    # (100000 / 2**23 ~= 0.0119) in float units.
                    from sqlalchemy import select

                    from pandrator.web.models import Artifact

                    with self.services.database.session() as session:
                        master = session.scalars(
                            select(Artifact)
                            .where(
                                Artifact.session_id == sid,
                                Artifact.role == "soundtrack_master",
                                Artifact.state == "current",
                            )
                            .order_by(Artifact.created_at.desc())
                        ).first()
                        assert master is not None
                        master_path = self.services.paths.managed_path(
                            master.relative_path
                        )
                    tail_info = probe_soundtrack_media(master_path)
                    tail_start = max(0.0, tail_info["duration"] - 0.1)
                    raw = subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-nostdin",
                            "-ss",
                            str(tail_start),
                            "-i",
                            str(master_path),
                            "-t",
                            "0.1",
                            "-f",
                            "f32le",
                            "-ac",
                            "1",
                            "-ar",
                            "48000",
                            "-",
                        ],
                        check=True,
                        capture_output=True,
                    ).stdout
                    samples = struct.unpack("<" + "f" * (len(raw) // 4), raw)
                    rms = (
                        sum(sample * sample for sample in samples)
                        / max(1, len(samples))
                    ) ** 0.5
                    self.assertGreater(rms, 0.01)

    def test_no_overrun_keeps_stream_copy_fast_path(self):
        sid = self._voiceover_session()
        video = self.services.paths.uploads / "exact-source.mp4"
        self._make_video(video, 3)
        self._attach_upload(sid, video)
        self._register_speech(sid, 2.0)
        metadata, _relative = self._export_result(
            sid,
            {
                "export_mode": "media",
                "audio_mode": "dubbing_only",
                "subtitle_mode": "none",
            },
        )
        self.assertEqual(metadata["tail_extension_ms"], 0)
        self.assertFalse(metadata["video_transcoded"])

    def test_silent_source_without_audio_still_freezes_tail(self):
        from pandrator.web.soundtrack_export import probe_soundtrack_media

        sid = self._voiceover_session()
        video = self.services.paths.uploads / "silent-source.mp4"
        self._make_video(video, 2, with_audio=False)
        self._attach_upload(sid, video)
        self._register_speech(sid, 2.6)
        metadata, relative = self._export_result(
            sid,
            {
                "export_mode": "media",
                "audio_mode": "dubbing_only",
                "subtitle_mode": "none",
            },
        )
        self.assertGreater(metadata["tail_extension_ms"], 0)
        info = probe_soundtrack_media(self.services.paths.managed_path(relative))
        self.assertGreaterEqual(info["duration"], 2.6)

    def test_default_ask_warns_before_final_artifact(self):
        sid = self._voiceover_session()
        video = self.services.paths.uploads / "short-source.mp4"
        self._make_video(video, 2)
        self._attach_upload(sid, video)
        self._register_speech(sid, 5.0)
        with self.assertRaisesRegex(
            VideoTailExtensionRequired,
            "This requires reencoding the video",
        ):
            self.handlers.export(
                {
                    "session_id": sid,
                    "settings": {
                        "export_mode": "media",
                        "audio_mode": "dubbing_only",
                        "subtitle_mode": "none",
                    },
                },
                lambda *_: None,
                threading.Event(),
            )
        from sqlalchemy import select

        from pandrator.web.models import Artifact

        with self.services.database.session() as session:
            self.assertIsNone(
                session.scalar(
                    select(Artifact.id).where(
                        Artifact.session_id == sid,
                        Artifact.role == "export",
                        Artifact.state == "current",
                    )
                )
            )

    def test_ask_policy_warns_even_when_legacy_cap_is_zero(self):
        sid = self._voiceover_session()
        video = self.services.paths.uploads / "strict-source.mp4"
        self._make_video(video, 2)
        self._attach_upload(sid, video)
        self._register_speech(sid, 2.6)
        with self.assertRaisesRegex(
            VideoTailExtensionRequired,
            r"Generated speech is .* longer than the video\. .*This requires reencoding",
        ):
            self.handlers.export(
                {
                    "session_id": sid,
                    "settings": {
                        "export_mode": "media",
                        "audio_mode": "dubbing_only",
                        "subtitle_mode": "none",
                        "video_tail_extension_policy": "ask",
                        "video_tail_extension_max_ms": 0,
                    },
                },
                lambda *_: None,
                threading.Event(),
            )
        from sqlalchemy import select

        from pandrator.web.models import Artifact

        with self.services.database.session() as session:
            self.assertIsNone(
                session.scalar(
                    select(Artifact.id).where(
                        Artifact.session_id == sid,
                        Artifact.role == "export",
                        Artifact.state == "current",
                    )
                )
            )

    def test_audio_only_overrun_stays_strict(self):
        sid = self._voiceover_session()
        video = self.services.paths.uploads / "audio-source.mp4"
        self._make_video(video, 3)
        source = self._attach_upload(sid, video)
        speech = self._register_speech(sid, 4.0)
        from pandrator.web.soundtrack_export import ensure_soundtrack_master

        with self.assertRaisesRegex(ValueError, "exceeds the recording"):
            ensure_soundtrack_master(
                self.handlers,
                session_id=sid,
                source=source,
                speech=speech,
                audio_mode="mixed",
                settings={
                    "export_mode": "audio",
                    "audio_mode": "mixed",
                    "audio_match_source_duration": True,
                },
                cancel_event=threading.Event(),
            )


if __name__ == "__main__":
    unittest.main()
