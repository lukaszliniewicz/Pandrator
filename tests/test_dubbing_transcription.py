import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.logic import dubbing_handler
from pandrator.logic.dubbing import parakeet_onnx, srt_utils, stt_backends, transcription


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:01,000
Hello

2
00:00:01,100 --> 00:00:01,500
friend
"""


class DubbingTranscriptionTests(unittest.TestCase):
    def test_automatic_boundary_correction_is_whisperx_only(self):
        settings = {"boundary_correction_enabled": True}

        self.assertTrue(
            transcription.automatic_boundary_correction_enabled(
                settings,
                "whisperx",
            )
        )
        self.assertFalse(
            transcription.automatic_boundary_correction_enabled(
                settings,
                "parakeet_onnx",
            )
        )

    def test_build_whisperx_args_includes_prompt_and_diarization(self):
        args = transcription.build_whisperx_args(
            "audio.wav",
            language="English",
            session_dir="session",
            whisper_model="large-v3",
            align_model="custom-align",
            initial_prompt="Names: Ada, Turing.",
            diarize=True,
            hf_token="hf-token",
            chunk_size=22,
        )

        self.assertIn("--initial_prompt", args)
        self.assertEqual(args[args.index("--initial_prompt") + 1], "Names: Ada, Turing.")
        self.assertIn("--diarize", args)
        self.assertEqual(args[args.index("--hf_token") + 1], "hf-token")
        self.assertEqual(args[args.index("--language") + 1], "en")
        self.assertEqual(args[args.index("--align_model") + 1], "custom-align")
        self.assertEqual(args[args.index("--chunk_size") + 1], "22")

    def test_build_whisperx_args_requires_hf_token_for_diarization(self):
        with self.assertRaises(ValueError):
            transcription.build_whisperx_args(
                "audio.wav",
                language="English",
                session_dir="session",
                whisper_model="large-v3",
                diarize=True,
            )

    def test_stt_backend_detection_accepts_manifest_or_importable_module(self):
        statuses = stt_backends.detect_stt_backend_statuses(
            environ={
                "WHISPERX_PIXI_MANIFEST": "C:/installed/whisperx/pixi.toml",
                "PARAKEET_PIXI_MANIFEST": "C:/missing/parakeet/pixi.toml",
            },
            path_exists=lambda path: str(path).replace("\\", "/") == "C:/installed/whisperx/pixi.toml",
            find_module=lambda name: name == "onnx_asr",
            find_executable=lambda name: None,
        )

        self.assertTrue(statuses[stt_backends.STT_BACKEND_WHISPERX].installed)
        self.assertTrue(statuses[stt_backends.STT_BACKEND_PARAKEET_ONNX].installed)

    def test_stt_backend_language_options_are_backend_specific(self):
        parakeet_options = stt_backends.language_options_for_backend("parakeet_onnx")
        whisper_options = stt_backends.language_options_for_backend("whisperx")

        self.assertEqual(len(parakeet_options), 25)
        self.assertIn(("Polish", "pl"), [(option.name, option.code) for option in parakeet_options])
        self.assertIn(("Ukrainian", "uk"), [(option.name, option.code) for option in parakeet_options])
        self.assertIn("Yoruba", [option.name for option in whisper_options])
        self.assertNotIn("Yoruba", [option.name for option in parakeet_options])
        self.assertEqual(
            stt_backends.normalize_stt_language_for_backend("parakeet_onnx", "Yoruba").code,
            "en",
        )

    def test_transcribe_video_file_extracts_audio_runs_whisperx_and_postprocesses_srt(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            executable = Path(command[0]).name.lower()
            if executable.startswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"wav")
                return SimpleNamespace(stderr=b"")

            whisperx_index = command.index("whisperx")
            audio_path = Path(command[whisperx_index + 1])
            output_dir = Path(command[command.index("--output_dir") + 1])
            output_dir.joinpath(f"{audio_path.stem}.srt").write_text(SAMPLE_SRT, encoding="utf-8")
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
                    "whisper_prompt": "Keep names.",
                    "subtitle_merge_threshold": 200,
                },
                run_func=fake_run,
            )

            self.assertTrue(output_path.endswith("clip_merged.srt"))
            self.assertTrue(os.path.exists(output_path))
            segments = srt_utils.parse_srt(Path(output_path).read_text(encoding="utf-8"))
            self.assertEqual(len(segments), 1)
            self.assertEqual(segments[0].text, "Hello friend")
            whisperx_command = next(command for command in commands if "whisperx" in command)
            self.assertIn("--initial_prompt", whisperx_command)

    def test_transcribe_source_file_accepts_audio_source_without_overwriting_input_wav(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            executable = Path(command[0]).name.lower()
            if executable.startswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"normalized-wav")
                return SimpleNamespace(stderr=b"")

            whisperx_index = command.index("whisperx")
            audio_path = Path(command[whisperx_index + 1])
            output_dir = Path(command[command.index("--output_dir") + 1])
            output_dir.joinpath(f"{audio_path.stem}.srt").write_text(SAMPLE_SRT, encoding="utf-8")
            return SimpleNamespace(stderr=b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = os.path.join(temp_dir, "clip.wav")
            Path(audio_path).write_bytes(b"source-wav")

            output_path = transcription.transcribe_source_file(
                temp_dir,
                audio_path,
                {
                    "stt_language": "English",
                    "whisper_model": "large-v3",
                    "subtitle_merge_threshold": -1,
                },
                run_func=fake_run,
            )

            ffmpeg_command = commands[0]
            self.assertEqual(Path(ffmpeg_command[-1]).name, "clip_transcription.wav")
            self.assertNotEqual(os.path.abspath(ffmpeg_command[-1]), os.path.abspath(audio_path))
            whisperx_command = next(command for command in commands if "whisperx" in command)
            self.assertEqual(Path(whisperx_command[whisperx_command.index("whisperx") + 1]).name, "clip_transcription.wav")
            self.assertEqual(Path(output_path).name, "clip.srt")
            self.assertTrue(os.path.exists(output_path))

    def test_build_parakeet_vad_options_exposes_silero_controls(self):
        options = parakeet_onnx.build_vad_options(
            {
                "parakeet_vad_max_speech_seconds": 15,
                "parakeet_vad_threshold": 0.25,
                "parakeet_vad_neg_threshold": 0.1,
                "parakeet_vad_min_silence_ms": 1000,
                "parakeet_vad_min_speech_ms": 300,
                "parakeet_vad_speech_pad_ms": 50,
                "parakeet_vad_batch_size": 4,
            }
        )

        self.assertEqual(options["max_speech_duration_s"], 15.0)
        self.assertEqual(options["threshold"], 0.25)
        self.assertEqual(options["neg_threshold"], 0.1)
        self.assertEqual(options["min_silence_duration_ms"], 1000.0)
        self.assertEqual(options["min_speech_duration_ms"], 300.0)
        self.assertEqual(options["speech_pad_ms"], 50.0)
        self.assertEqual(options["batch_size"], 4)

    def test_transcribe_video_file_runs_parakeet_backend_and_writes_json(self):
        calls = {}

        class FakeModel:
            def with_vad(self, vad, **kwargs):
                calls["vad"] = vad
                calls["vad_kwargs"] = kwargs
                return self

            def with_timestamps(self):
                calls["timestamps"] = True
                return self

            def recognize(self, audio_path):
                calls["audio_path"] = audio_path
                return [
                    SimpleNamespace(
                        start=0.0,
                        end=1.0,
                        text="Hello.",
                        tokens=["Hello"],
                        timestamps=[0.12],
                        logprobs=[-0.1],
                    ),
                    SimpleNamespace(start=1.2, end=1.5, text="", tokens=[], timestamps=[], logprobs=[]),
                    SimpleNamespace(
                        start=1.6,
                        end=2.4,
                        text="Next line.",
                        tokens=["Next", "line"],
                        timestamps=[0.05, 0.4],
                        logprobs=[-0.2, -0.3],
                    ),
                ]

        fake_onnx_asr = SimpleNamespace(
            load_model=lambda model, quantization=None, providers=None: calls.update(
                {
                    "model": model,
                    "quantization": quantization,
                    "providers": providers,
                }
            )
            or FakeModel(),
            load_vad=lambda name, providers=None: {"name": name, "providers": providers},
        )

        def fake_run(command, **_kwargs):
            executable = Path(command[0]).name.lower()
            if executable.startswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"wav")
                return SimpleNamespace(stderr=b"")
            raise AssertionError(f"Unexpected command: {command}")

        def fail_boundary_audio_loader(*_args):
            raise AssertionError("Parakeet must not run WhisperX boundary correction")

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = os.path.join(temp_dir, "clip.mp4")
            Path(video_path).write_bytes(b"video")

            with patch.dict(sys.modules, {"onnx_asr": fake_onnx_asr}):
                output_path = transcription.transcribe_video_file(
                    temp_dir,
                    video_path,
                    {
                        "stt_backend": "parakeet_onnx",
                        "boundary_correction_enabled": True,
                        "parakeet_model": "nemo-parakeet-tdt-0.6b-v3",
                        "parakeet_vad_max_speech_seconds": 15,
                        "parakeet_vad_threshold": 0.5,
                        "subtitle_merge_threshold": 0,
                    },
                    run_func=fake_run,
                    boundary_audio_loader=fail_boundary_audio_loader,
                )

            self.assertEqual(Path(output_path).name, "clip.srt")
            self.assertEqual(calls["model"], "nemo-parakeet-tdt-0.6b-v3")
            self.assertEqual(calls["quantization"], None)
            self.assertEqual(calls["providers"], ["CPUExecutionProvider"])
            self.assertEqual(calls["vad_kwargs"]["max_speech_duration_s"], 15.0)
            json_path = Path(temp_dir) / "clip_parakeet.json"
            self.assertTrue(json_path.exists())
            payload = json_path.read_text(encoding="utf-8")
            self.assertIn('"absolute_timestamps"', payload)
            segments = srt_utils.parse_srt(Path(output_path).read_text(encoding="utf-8"))
            self.assertEqual([segment.text for segment in segments], ["Hello.", "Next line."])

    def test_dubbing_handler_transcription_no_longer_runs_subdub_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "clip.srt")
            Path(output_path).write_text(SAMPLE_SRT, encoding="utf-8")

            with patch(
                "pandrator.logic.dubbing_handler.subprocess.Popen",
                side_effect=AssertionError("Subdub subprocess should not run"),
            ), patch(
                "pandrator.logic.dubbing_handler.transcribe_video_file",
                return_value=output_path,
            ):
                self.assertEqual(
                    dubbing_handler.transcribe_video_with_result(
                        temp_dir,
                        os.path.join(temp_dir, "clip.mp4"),
                        {"stt_language": "English", "whisper_model": "large-v3"},
                    ),
                    output_path,
                )
                self.assertTrue(
                    dubbing_handler.transcribe_video(
                        temp_dir,
                        os.path.join(temp_dir, "clip.mp4"),
                        {"stt_language": "English", "whisper_model": "large-v3"},
                    )
                )

    def test_dubbing_handler_transcription_metadata_includes_correction_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transcribed_path = os.path.join(temp_dir, "clip.srt")
            corrected_path = os.path.join(temp_dir, "clip_corrected.srt")
            Path(transcribed_path).write_text(SAMPLE_SRT, encoding="utf-8")
            Path(corrected_path).write_text(SAMPLE_SRT.replace("Hello", "Hello."), encoding="utf-8")

            with patch(
                "pandrator.logic.dubbing_handler.transcribe_video_file",
                return_value=transcribed_path,
            ), patch(
                "pandrator.logic.dubbing_handler.correct_srt_file_with_result",
                return_value=SimpleNamespace(
                    output_path=corrected_path,
                    cost=0.015,
                    response_count=2,
                ),
            ) as native_correction:
                result = dubbing_handler.transcribe_video_with_metadata(
                    temp_dir,
                    os.path.join(temp_dir, "clip.mp4"),
                    {
                        "stt_language": "English",
                        "whisper_model": "large-v3",
                        "correction_enabled": True,
                        "correction_model": "anthropic/claude-sonnet-4-6",
                    },
                    correction_prompt="Fix punctuation.",
                )

            self.assertEqual(result.output_path, corrected_path)
            self.assertEqual(result.correction_cost, 0.015)
            self.assertEqual(result.correction_response_count, 2)
            native_correction.assert_called_once()
            self.assertEqual(native_correction.call_args.kwargs["srt_file"], transcribed_path)
            self.assertEqual(native_correction.call_args.kwargs["correction_instructions"], "Fix punctuation.")


if __name__ == "__main__":
    unittest.main()
