import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.logic import dubbing_handler
from pandrator.logic.dubbing import audio_sync


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:01,000
Hello.

2
00:00:01,100 --> 00:00:02,000
Friend.
"""


class DubbingAudioSyncTests(unittest.TestCase):
    def _write_sync_fixture(self, temp_dir: str) -> tuple[str, str, str]:
        srt_path = os.path.join(temp_dir, "selected.srt")
        Path(srt_path).write_text(SAMPLE_SRT, encoding="utf-8")
        Path(os.path.join(temp_dir, "newer_wrong.srt")).write_text(
            """1
00:00:09,000 --> 00:00:10,000
Wrong.
""",
            encoding="utf-8",
        )

        speech_blocks_path = os.path.join(temp_dir, "selected_speech_blocks.json")
        Path(speech_blocks_path).write_text(
            json.dumps(
                [
                    {"number": "0001", "text": "Hello.", "subtitles": [1]},
                    {"number": "0002", "text": "Friend.", "subtitles": [2]},
                ]
            ),
            encoding="utf-8",
        )

        wavs_dir = os.path.join(temp_dir, "Sentence_wavs")
        os.makedirs(wavs_dir, exist_ok=True)
        Path(os.path.join(wavs_dir, "Session_sentence_0001.wav")).write_bytes(b"wav1")
        Path(os.path.join(wavs_dir, "Session_sentence_0002.wav")).write_bytes(b"wav2")
        return srt_path, speech_blocks_path, wavs_dir

    def test_create_alignment_blocks_uses_explicit_srt_and_matches_sentence_wavs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path, speech_blocks_path, wavs_dir = self._write_sync_fixture(temp_dir)

            blocks = audio_sync.create_alignment_blocks(
                srt_path,
                speech_blocks_path,
                wavs_dir,
            )

            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0].start_ms, 0)
            self.assertEqual(blocks[0].end_ms, 1000)
            self.assertEqual([path.name for path in blocks[0].audio_files], ["Session_sentence_0001.wav"])
            self.assertEqual(blocks[1].start_ms, 1100)
            self.assertEqual([path.name for path in blocks[1].audio_files], ["Session_sentence_0002.wav"])

    def test_create_alignment_blocks_sorts_subtitle_references_chronologically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path, speech_blocks_path, wavs_dir = self._write_sync_fixture(temp_dir)
            Path(speech_blocks_path).write_text(
                json.dumps([{"number": "0001", "text": "Hello friend.", "subtitles": [2, 1, 2]}]),
                encoding="utf-8",
            )

            blocks = audio_sync.create_alignment_blocks(srt_path, speech_blocks_path, wavs_dir)

            self.assertEqual([1, 2], blocks[0].subtitles)
            self.assertEqual(0, blocks[0].start_ms)
            self.assertEqual(2000, blocks[0].end_ms)

    def test_create_alignment_blocks_rejects_missing_generated_audio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path, speech_blocks_path, wavs_dir = self._write_sync_fixture(temp_dir)
            Path(wavs_dir, "Session_sentence_0002.wav").unlink()

            with self.assertRaisesRegex(audio_sync.AudioSyncError, "0002.*no generated audio"):
                audio_sync.create_alignment_blocks(srt_path, speech_blocks_path, wavs_dir)

    def test_parse_ffmpeg_max_volume(self):
        self.assertEqual(
            audio_sync.parse_ffmpeg_max_volume("[Parsed_volumedetect_0] max_volume: -3.5 dB"),
            -3.5,
        )

    def test_audio_sync_ffmpeg_command_builders_are_deterministic(self):
        extract = audio_sync.build_extract_original_audio_command(
            "video.mp4",
            "original.wav",
            ffmpeg_executable="ffmpeg-bin",
        )
        self.assertEqual(extract[:4], ["ffmpeg-bin", "-y", "-i", "video.mp4"])
        self.assertEqual(extract[-1], "original.wav")
        self.assertIn("pcm_s16le", extract)

        analyze = audio_sync.build_volume_analysis_command("dub.wav", ffmpeg_executable="ffmpeg-bin")
        self.assertEqual(analyze[:4], ["ffmpeg-bin", "-y", "-i", "dub.wav"])
        self.assertIn("volumedetect", analyze)
        self.assertEqual(analyze[-2:], ["null", os.devnull])

        amplify = audio_sync.build_amplify_audio_command(
            "dub.wav",
            "amplified.wav",
            4.5,
            ffmpeg_executable="ffmpeg-bin",
        )
        self.assertIn("volume=4.5dB", amplify)
        self.assertEqual(amplify[-1], "amplified.wav")

        mix = audio_sync.build_mix_audio_command(
            "original.wav",
            "amplified.wav",
            "mixed.wav",
            ffmpeg_executable="ffmpeg-bin",
        )
        self.assertIn(audio_sync.MIX_FILTER_COMPLEX, mix)
        self.assertEqual(mix[-2:], ["[mixed]", "mixed.wav"])

        mux = audio_sync.build_mux_mixed_audio_command(
            "video.mp4",
            "mixed.wav",
            "final.mp4",
            ffmpeg_executable="ffmpeg-bin",
        )
        self.assertIn("0:v", mux)
        self.assertIn("1:a", mux)
        self.assertEqual(mux[-1], "final.mp4")

    def test_mix_audio_tracks_uses_command_builders_in_order(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            if "volumedetect" in command:
                return SimpleNamespace(stderr="max_volume: -4.0 dB")
            return SimpleNamespace(stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            result = audio_sync.mix_audio_tracks_with_result(
                "video.mp4",
                "aligned.wav",
                temp_dir,
                ffmpeg_executable="ffmpeg-bin",
                run_func=fake_run,
            )

        self.assertTrue(result.output_video_path.endswith("final_output.mp4"))
        self.assertTrue(result.original_audio_path.endswith("original_audio.wav"))
        self.assertTrue(result.amplified_dubbed_audio_path.endswith("amplified_dubbed_audio.wav"))
        self.assertTrue(result.mixed_audio_path.endswith("mixed_audio.wav"))
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0][:4], ["ffmpeg-bin", "-y", "-i", "video.mp4"])
        self.assertIn("volumedetect", commands[1])
        self.assertIn("volume=4.0dB", commands[2])
        self.assertIn(audio_sync.MIX_FILTER_COMPLEX, commands[3])
        self.assertIn("mixed_audio.wav", commands[3][-1])
        self.assertEqual(commands[4][:4], ["ffmpeg-bin", "-y", "-i", "video.mp4"])
        self.assertTrue(any(str(item).endswith("mixed_audio.wav") for item in commands[4]))
        self.assertTrue(str(commands[4][-1]).endswith("final_output.mp4"))

    def test_synchronize_audio_video_uses_injected_align_and_mix(self):
        calls = {}

        def fake_align(blocks, session_dir, **kwargs):
            calls["blocks"] = blocks
            calls["align_kwargs"] = kwargs
            aligned_path = os.path.join(session_dir, "aligned_audio.wav")
            Path(aligned_path).write_bytes(b"aligned")
            return aligned_path

        def fake_mix(video_file, aligned_audio_path, session_dir, **kwargs):
            calls["mix"] = (video_file, aligned_audio_path, kwargs)
            output_path = os.path.join(session_dir, "final_output.mp4")
            Path(output_path).write_bytes(b"video")
            return output_path

        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path, speech_blocks_path, wavs_dir = self._write_sync_fixture(temp_dir)
            video_path = os.path.join(temp_dir, "clip.mp4")
            Path(video_path).write_bytes(b"video")

            output_path = audio_sync.synchronize_audio_video(
                temp_dir,
                video_path,
                srt_path,
                speech_blocks_path,
                sentence_wavs_dir=wavs_dir,
                delay_start_ms=1234,
                speed_up_percent=130,
                align_func=fake_align,
                mix_func=fake_mix,
            )

            self.assertTrue(output_path.endswith("final_output.mp4"))
            self.assertEqual(calls["align_kwargs"]["delay_start_ms"], 1234)
            self.assertEqual(calls["align_kwargs"]["speed_up_percent"], 130)
            self.assertEqual(len(calls["blocks"]), 2)
            self.assertEqual(calls["mix"][0], video_path)

    def test_synchronize_audio_video_with_result_reports_sync_artifacts(self):
        calls = {}

        def fake_align(_blocks, session_dir, **_kwargs):
            aligned_path = os.path.join(session_dir, "aligned_audio.wav")
            Path(aligned_path).write_bytes(b"aligned")
            return aligned_path

        def fake_mix(video_file, aligned_audio_path, session_dir, **kwargs):
            calls["mix"] = (video_file, aligned_audio_path, kwargs)
            return audio_sync.AudioMixResult(
                output_video_path=os.path.join(session_dir, "final_output.mp4"),
                original_audio_path=os.path.join(session_dir, "original_audio.wav"),
                amplified_dubbed_audio_path=os.path.join(session_dir, "amplified_dubbed_audio.wav"),
                mixed_audio_path=os.path.join(session_dir, "mixed_audio.wav"),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path, speech_blocks_path, wavs_dir = self._write_sync_fixture(temp_dir)
            video_path = os.path.join(temp_dir, "clip.mp4")
            Path(video_path).write_bytes(b"video")

            result = audio_sync.synchronize_audio_video_with_result(
                temp_dir,
                video_path,
                srt_path,
                speech_blocks_path,
                sentence_wavs_dir=wavs_dir,
                align_func=fake_align,
                mix_func=fake_mix,
            )

            self.assertEqual(result.output_video_path, os.path.join(temp_dir, "final_output.mp4"))
            self.assertEqual(result.aligned_audio_path, os.path.join(temp_dir, "aligned_audio.wav"))
            self.assertEqual(result.original_audio_path, os.path.join(temp_dir, "original_audio.wav"))
            self.assertEqual(
                result.amplified_dubbed_audio_path,
                os.path.join(temp_dir, "amplified_dubbed_audio.wav"),
            )
            self.assertEqual(result.mixed_audio_path, os.path.join(temp_dir, "mixed_audio.wav"))
            self.assertEqual(calls["mix"][1], result.aligned_audio_path)

    def test_dubbing_handler_sync_no_longer_runs_subdub_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path, speech_blocks_path, _wavs_dir = self._write_sync_fixture(temp_dir)
            video_path = os.path.join(temp_dir, "clip.mp4")
            Path(video_path).write_bytes(b"video")
            output_path = os.path.join(temp_dir, "final_output.mp4")
            amplified_path = os.path.join(temp_dir, "amplified_dubbed_audio.wav")

            with patch(
                "pandrator.logic.dubbing_handler.subprocess.Popen",
                side_effect=AssertionError("Subdub subprocess should not run"),
            ), patch(
                "pandrator.logic.dubbing_handler.synchronize_audio_video_with_result",
                return_value=audio_sync.AudioSyncResult(
                    output_video_path=output_path,
                    aligned_audio_path=os.path.join(temp_dir, "aligned_audio.wav"),
                    amplified_dubbed_audio_path=amplified_path,
                ),
            ) as native_sync:
                self.assertEqual(
                    dubbing_handler.synchronize_audio_with_result(
                        temp_dir,
                        video_file=video_path,
                        srt_file=srt_path,
                        speech_blocks_file=speech_blocks_path,
                        delay_start_ms=500,
                        speed_up_percent=125,
                    ),
                    output_path,
                )
                metadata = dubbing_handler.synchronize_audio_with_metadata(
                    temp_dir,
                    video_file=video_path,
                    srt_file=srt_path,
                    speech_blocks_file=speech_blocks_path,
                )
                self.assertEqual(metadata.output_video_path, output_path)
                self.assertEqual(metadata.amplified_dubbed_audio_path, amplified_path)
                self.assertTrue(
                    dubbing_handler.synchronize_audio(
                        temp_dir,
                        video_file=video_path,
                        srt_file=srt_path,
                        speech_blocks_file=speech_blocks_path,
                        delay_start_ms=500,
                        speed_up_percent=125,
                    )
                )

            self.assertEqual(native_sync.call_count, 3)
            first_call_kwargs = native_sync.call_args_list[0].kwargs
            self.assertEqual(first_call_kwargs["delay_start_ms"], 500)
            self.assertEqual(first_call_kwargs["speed_up_percent"], 125)
            call_kwargs = native_sync.call_args.kwargs
            self.assertEqual(call_kwargs["srt_file"], srt_path)
            self.assertEqual(call_kwargs["speech_blocks_file"], speech_blocks_path)

    def test_replace_video_audio_track_uses_native_command_builder(self):
        captured_commands = []

        class FakeProcess:
            def __init__(self, command, **_kwargs):
                captured_commands.append(command)
                self.command = command
                self.stdout = []
                self.returncode = 0

            def wait(self):
                Path(self.command[-1]).write_bytes(b"video")

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = os.path.join(temp_dir, "clip.mp4")
            audio_path = os.path.join(temp_dir, "dub.wav")
            output_path = os.path.join(temp_dir, "dubbed_only.mp4")
            Path(video_path).write_bytes(b"video")
            Path(audio_path).write_bytes(b"audio")

            with patch("pandrator.logic.dubbing_handler.subprocess.Popen", FakeProcess):
                self.assertTrue(
                    dubbing_handler.replace_video_audio_track(
                        video_path,
                        audio_path,
                        output_path,
                    )
                )

            self.assertTrue(os.path.exists(output_path))
            self.assertIn("0:v:0", captured_commands[0])
            self.assertIn("1:a:0", captured_commands[0])
            self.assertIn("-shortest", captured_commands[0])


if __name__ == "__main__":
    unittest.main()
