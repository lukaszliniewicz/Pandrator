"""Video tail extension policy: ask before extending, or preserve all speech.

Covers terminal speech overruns while 'Match recording timeline' is on:
exports ask before freezing the last video frame by default, explicit extension
preserves the complete speech tail without a legacy duration cap, and fitting
timelines keep the copy fast path.
"""

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
    probe_soundtrack_media,
    resolve_video_tail_extension_ms,
)
from pandrator.web.workspace import validate_output_settings


def decode_audio_tail(path, seconds=0.25, rate=48000):
    start = max(0.0, probe_soundtrack_media(path)["duration"] - seconds)
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-t",
            str(seconds),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return struct.unpack("<" + "f" * (len(raw) // 4), raw)


def decode_frame_rgb(path, position):
    info = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    width, height = (int(part) for part in info.split(","))
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            str(position),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return bytes(raw), width, height


def mean_abs_diff(left, right):
    total = 0
    for left_byte, right_byte in zip(left, right):
        total += abs(left_byte - right_byte)
    return total / max(1, len(left))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required"
)
class TailCommandTests(unittest.TestCase):
    def test_tail_seconds_round_up_to_frames_plus_one(self):
        self.assertAlmostEqual(video_tail_extension_seconds(0.4, 25.0), 0.44)
        self.assertAlmostEqual(video_tail_extension_seconds(0.508, 25.0), 0.56)

    def test_tail_seconds_without_fps_falls_back_to_margin(self):
        self.assertAlmostEqual(video_tail_extension_seconds(0.4, None), 0.52)

    def test_tail_seconds_rejects_non_positive_overrun(self):
        with self.assertRaises(ValueError):
            video_tail_extension_seconds(0.0, 25.0)

    def test_tail_command_clones_last_frame_without_shortest(self):
        command = build_video_tail_extension_command(
            "in.mp4", "out.mp4", 0.44, video_encoder="libx264"
        )
        rendered = " ".join(command)
        self.assertIn("tpad=stop_mode=clone:stop_duration=0.440", rendered)
        self.assertNotIn("-shortest", command)

    def test_replace_command_shortest_flag(self):
        with_shortest = build_replace_video_audio_command("v.mp4", "a.wav", "o.mp4")
        self.assertIn("-shortest", with_shortest)
        without = build_replace_video_audio_command(
            "v.mp4", "a.wav", "o.mp4", shortest=False
        )
        self.assertNotIn("-shortest", without)

    def test_default_policy_asks_and_extend_ignores_legacy_cap(self):
        with self.assertRaises(VideoTailExtensionRequired):
            resolve_video_tail_extension_ms(
                reference_duration=3.0,
                generated_duration=3.4,
                fps=25.0,
                settings={},
            )
        self.assertEqual(
            resolve_video_tail_extension_ms(
                reference_duration=3.0,
                generated_duration=34.0,
                fps=25.0,
                settings={
                    "video_tail_extension_policy": "extend",
                    "video_tail_extension_max_ms": 2000,
                },
            ),
            31040,
        )

    def test_invalid_policy_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError, r"video_tail_extension_policy must be ask or extend\."
        ):
            resolve_video_tail_extension_ms(
                reference_duration=3.0,
                generated_duration=3.0,
                fps=25.0,
                settings={"video_tail_extension_policy": "never"},
            )

    def test_output_settings_validation(self):
        validate_output_settings({})
        validate_output_settings({"video_tail_extension_policy": "ask"})
        validate_output_settings({"video_tail_extension_policy": "extend"})
        for bad in ("x", None, 0, True, [], {}):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    validate_output_settings({"video_tail_extension_policy": bad})


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required"
)
class TailExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from pandrator.web.api import create_app
        from pandrator.web.auth import BootstrapTokenStore

        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]["services"]
        self.sid = self.client.post(
            "/api/v1/sessions",
            json={"name": "Tail regression", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        self.handlers = self.services.workflow_handlers
        self.source_path = self.services.paths.uploads / "tail-source.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=64x64:r=25:d=2.5",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=64x64:r=25:d=0.5",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map",
                "[v]",
                "-map",
                "2:a",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(self.source_path),
            ],
            check=True,
            capture_output=True,
        )
        self.source = self.services.artifacts.register(
            self.source_path, kind="mp4", role="upload", session_id=self.sid
        )
        asset = self.services.source_library.ensure_for_artifact(
            self.source.id, display_name="tail-source.mp4"
        )
        self.services.source_library.attach(
            self.sid, asset["id"] if isinstance(asset, dict) else asset.id
        )

    @staticmethod
    def write_tone(path, duration, tone_until):
        rate = 48000
        total = round(duration * rate)
        voiced = round(tone_until * rate)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(
                b"".join(
                    struct.pack(
                        "<h",
                        int(12000 * math.sin(2 * math.pi * 440 * index / rate))
                        if index < voiced
                        else 0,
                    )
                    for index in range(total)
                )
            )

    def register_speech(self, name, duration, tone_until):
        path = self.services.paths.uploads / name
        self.write_tone(path, duration, tone_until)
        return self.services.artifacts.register(
            path, kind="audio", role="dubbing_audio", session_id=self.sid
        )

    def base_settings(self, audio_mode="mixed"):
        return {
            "export_mode": "media",
            "audio_mode": audio_mode,
            "subtitle_mode": "none",
            "subtitle_selection": "translation",
            "subtitle_format": "srt",
            "video_transcode": False,
            "burn_video_encoder": "libx264",
            "burn_video_resolution": "source",
            "burn_video_quality": 18,
            "burn_video_speed": "balanced",
            "burn_audio_codec": "copy",
            "burn_audio_bitrate": "192k",
            "audio_match_source_duration": True,
            "video_tail_extension_policy": "extend",
        }

    def run_export(self, settings):
        result = self.handlers.export(
            {"session_id": self.sid, "settings": settings},
            lambda *_: None,
            threading.Event(),
        )
        self.assertEqual(len(result["artifact_ids"]), 1)
        with self.services.database.session() as session:
            from pandrator.web.models import Artifact

            artifact = session.get(Artifact, result["artifact_ids"][0])
            metadata = dict(artifact.metadata_json or {})
            relative = artifact.relative_path
        return self.services.paths.managed_path(relative), metadata

    def test_small_overrun_freezes_last_frame_and_preserves_audio_tail(self):
        self.register_speech("speech-long.wav", 3.4, 3.2)
        output, metadata = self.run_export(self.base_settings())
        self.assertEqual(metadata["tail_extension_ms"], 440)
        self.assertTrue(metadata["video_transcoded"])
        info = probe_soundtrack_media(output)
        self.assertTrue(info["has_video"])
        self.assertGreaterEqual(info["duration"], 3.39)
        tail = decode_audio_tail(output)
        self.assertGreater(max(abs(value) for value in tail), 0.001)
        source_last, _, _ = decode_frame_rgb(self.source_path, 2.9)
        output_last, _, _ = decode_frame_rgb(output, info["duration"] - 0.05)
        output_early, _, _ = decode_frame_rgb(output, 0.5)
        self.assertLess(mean_abs_diff(source_last, output_last), 12.0)
        self.assertGreater(mean_abs_diff(output_early, output_last), 40.0)

    def test_dubbed_overrun_within_policy_is_not_truncated(self):
        self.register_speech("speech-dubbed.wav", 3.4, 3.2)
        output, metadata = self.run_export(self.base_settings(audio_mode="dubbed"))
        self.assertEqual(metadata["tail_extension_ms"], 440)
        info = probe_soundtrack_media(output)
        self.assertGreaterEqual(info["duration"], 3.39)

    def test_fit_keeps_copy_fastpath(self):
        self.register_speech("speech-fit.wav", 2.9, 2.7)
        output, metadata = self.run_export(self.base_settings())
        self.assertEqual(metadata["tail_extension_ms"], 0)
        self.assertFalse(metadata["video_transcoded"])
        info = probe_soundtrack_media(output)
        self.assertAlmostEqual(info["duration"], 3.0, places=1)

    def test_overrun_beyond_legacy_cap_is_extended(self):
        self.register_speech("speech-far.wav", 5.5, 5.3)
        output, metadata = self.run_export(
            {
                **self.base_settings(),
                "video_tail_extension_max_ms": 2000,
            }
        )
        self.assertGreater(metadata["tail_extension_ms"], 2000)
        info = probe_soundtrack_media(output)
        self.assertGreaterEqual(info["duration"], 5.5 - 0.05)
        self.assertGreaterEqual(info["audio_duration"], 5.5 - 0.05)
        self.assertGreater(
            max(abs(value) for value in decode_audio_tail(output, seconds=0.75)),
            0.001,
        )

    def test_ask_policy_warns_before_final_artifact_even_with_legacy_cap(self):
        self.register_speech("speech-strict.wav", 3.4, 3.2)
        settings = {
            **self.base_settings(),
            "video_tail_extension_policy": "ask",
            "video_tail_extension_max_ms": 0,
        }
        with self.assertRaisesRegex(
            VideoTailExtensionRequired,
            "This requires reencoding the video",
        ):
            self.handlers.export(
                {"session_id": self.sid, "settings": settings},
                lambda *_: None,
                threading.Event(),
            )
        with self.services.database.session() as session:
            from pandrator.web.models import Artifact
            from sqlalchemy import select

            self.assertIsNone(
                session.scalar(
                    select(Artifact.id).where(
                        Artifact.session_id == self.sid,
                        Artifact.role == "export",
                        Artifact.state == "current",
                    )
                )
            )

    def test_over_30_second_extension_preserves_video_and_speech_tail(self):
        generated_duration = 34.0
        self.register_speech("speech-over-30.wav", generated_duration, 33.5)
        output, metadata = self.run_export(
            {
                **self.base_settings(audio_mode="dubbed"),
                "video_tail_extension_max_ms": 30000,
            }
        )
        info = probe_soundtrack_media(output)
        self.assertGreater(metadata["tail_extension_ms"], 30000)
        self.assertGreaterEqual(info["duration"], generated_duration - 0.05)
        self.assertGreaterEqual(info["audio_duration"], generated_duration - 0.05)
        source_last, _, _ = decode_frame_rgb(self.source_path, 2.9)
        output_last, _, _ = decode_frame_rgb(output, info["duration"] - 0.05)
        self.assertLess(mean_abs_diff(source_last, output_last), 12.0)

    def test_resolve_reports_comparable_assembly_digest(self):
        from pandrator.web.workspace import output_assembly_settings_hash

        response = self.client.post(
            f"/api/v1/sessions/{self.sid}/settings/resolve",
            json={"sections": ["audio", "output"], "overrides": {}},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(
            payload["assembly_settings_hash"],
            output_assembly_settings_hash(payload["value"]),
        )
        # The full resolve digest never equals an assembly digest; the export
        # page must compare against assembly_settings_hash.
        self.assertNotEqual(payload["settings_hash"], payload["assembly_settings_hash"])

    def test_output_setting_roundtrip_with_video_source(self):
        endpoint = f"/api/v1/sessions/{self.sid}/settings/output"
        current = self.client.get(endpoint).get_json()
        response = self.client.put(
            endpoint,
            json={
                "value": {
                    **current["override"],
                    "video_tail_extension_policy": "extend",
                }
            },
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = self.client.get(endpoint).get_json()
        self.assertEqual(saved["effective"]["video_tail_extension_policy"], "extend")
        bad = self.client.get(endpoint).get_json()
        rejected = self.client.put(
            endpoint,
            json={
                "value": {
                    **bad["override"],
                    "video_tail_extension_policy": "invalid",
                }
            },
            headers={**self.headers, "If-Match": f'"{bad["revision"]}"'},
        )
        self.assertEqual(rejected.status_code, 422, rejected.get_json())


if __name__ == "__main__":
    unittest.main()
