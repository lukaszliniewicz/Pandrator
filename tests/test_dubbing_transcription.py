import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.logic import dubbing_handler
from pandrator.logic.dubbing import crispasr, srt_utils, stt_backends, transcription


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:01,000
Hello friend
"""


def _crisp_json(words=None):
    words = words or [
        {"text": "Hello", "offsets": {"from": 0, "to": 400}},
        {"text": "friend.", "offsets": {"from": 420, "to": 900}},
    ]
    return {
        "crispasr": {"backend": "whisper", "model": "ggml-large-v3.bin", "language": "en"},
        "transcription": [
            {
                "offsets": {"from": 0, "to": 900},
                "text": "Hello friend.",
                "words": words,
            }
        ],
    }


class CrispASRTranscriptionTests(unittest.TestCase):
    def test_legacy_backend_names_migrate_to_crispasr_engines(self):
        self.assertEqual(stt_backends.normalize_stt_backend("whisperx"), "whisper")
        self.assertEqual(stt_backends.normalize_stt_backend("parakeet_onnx"), "parakeet")
        self.assertEqual(crispasr.normalize_engine("parakeet-tdt-0.6b-v3"), "parakeet")

    def test_whisper_command_pins_f16_large_v3_and_dtw(self):
        command = crispasr.build_command(
            "audio.wav",
            "output",
            {
                "stt_engine": "whisper",
                "stt_language": "Polish",
                "stt_compute_backend": "vulkan",
                "stt_compute_device": 1,
                "crispasr_vad_enabled": True,
                "crispasr_vad_threshold": 0.42,
                "crispasr_vad_min_speech_ms": 300,
                "crispasr_vad_min_silence_ms": 450,
                "crispasr_vad_max_speech_seconds": 90,
                "crispasr_vad_speech_pad_ms": 50,
            },
            executable="crispasr-test",
        )

        self.assertEqual(command[0], "crispasr-test")
        self.assertIn("ggerganov/whisper.cpp:ggml-large-v3.bin", command)
        self.assertIn("ggml-large-v3.bin", command)
        self.assertIn("-dtw", command)
        self.assertEqual(command[command.index("-dtw") + 1], "large.v3")
        self.assertEqual(command[command.index("--gpu-backend") + 1], "vulkan")
        self.assertEqual(command[command.index("--device") + 1], "1")
        self.assertEqual(command[command.index("-l") + 1], "pl")
        self.assertEqual(command[command.index("--vad-threshold") + 1], "0.42")
        self.assertEqual(command[command.index("--vad-min-silence-duration-ms") + 1], "450")

    def test_parakeet_command_pins_unquantized_v3_and_native_timing(self):
        command = crispasr.build_command(
            "audio.wav",
            "output",
            {"stt_engine": "parakeet", "stt_compute_backend": "auto", "crispasr_vad_enabled": False},
            executable="crispasr-test",
        )

        self.assertIn("cstr/parakeet-tdt-0.6b-v3-GGUF:parakeet-tdt-0.6b-v3.gguf", command)
        self.assertNotIn("q4", " ".join(command).lower())
        self.assertNotIn("-dtw", command)
        self.assertNotIn("--gpu-backend", command)
        self.assertNotIn("--vad", command)
        self.assertIn("-ojf", command)

    def test_model_specific_quantization_and_advanced_decoder_controls_are_forwarded(self):
        parakeet = crispasr.build_command(
            "audio.wav",
            "output",
            {
                "stt_engine": "parakeet",
                "stt_model_quantization": "q4_k",
                "stt_threads": 6,
                "stt_chunk_seconds": 120,
                "stt_chunk_overlap_seconds": 2.5,
                "stt_hotwords": "Pandrator,CrispASR",
                "stt_lid_backend": "ecapa",
                "stt_beam_size": 4,
                "parakeet_decoder": "maes",
                "crispasr_vad_enabled": True,
                "crispasr_vad_model": "firered",
            },
            executable="crispasr-test",
        )
        self.assertIn("parakeet-tdt-0.6b-v3-q4_k.gguf", parakeet)
        self.assertEqual(parakeet[parakeet.index("--threads") + 1], "6")
        self.assertEqual(parakeet[parakeet.index("--chunk-seconds") + 1], "120")
        self.assertEqual(parakeet[parakeet.index("--chunk-overlap") + 1], "2.5")
        self.assertEqual(parakeet[parakeet.index("--hotwords") + 1], "Pandrator,CrispASR")
        self.assertEqual(parakeet[parakeet.index("--lid-backend") + 1], "ecapa")
        self.assertEqual(parakeet[parakeet.index("--beam-size") + 1], "4")
        self.assertEqual(parakeet[parakeet.index("--parakeet-decoder") + 1], "maes")
        self.assertEqual(parakeet[parakeet.index("--vad-model") + 1], "firered")

        whisper = crispasr.build_command(
            "audio.wav",
            "output",
            {"stt_engine": "whisper", "stt_model_quantization": "q5_0"},
            executable="crispasr-test",
        )
        self.assertIn("ggml-large-v3-q5_0.bin", whisper)

    def test_unsupported_quantization_falls_back_to_engine_f16(self):
        command = crispasr.build_command(
            "audio.wav",
            "output",
            {"stt_engine": "whisper", "stt_model_quantization": "q4_k"},
            executable="crispasr-test",
        )
        self.assertIn("ggml-large-v3.bin", command)
        self.assertNotIn("ggml-large-v3-q4_k.bin", command)

    def test_vad_zero_values_are_forwarded_instead_of_replaced_by_defaults(self):
        command = crispasr.build_command(
            "audio.wav",
            "output",
            {
                "stt_engine": "parakeet",
                "crispasr_vad_enabled": True,
                "crispasr_vad_threshold": 0,
                "crispasr_vad_min_speech_ms": 0,
                "crispasr_vad_min_silence_ms": 0,
                "crispasr_vad_speech_pad_ms": 0,
            },
            executable="crispasr-test",
        )

        self.assertEqual(command[command.index("--vad-threshold") + 1], "0.0")
        self.assertEqual(command[command.index("--vad-min-speech-duration-ms") + 1], "0")
        self.assertEqual(command[command.index("--vad-min-silence-duration-ms") + 1], "0")
        self.assertEqual(command[command.index("--vad-speech-pad-ms") + 1], "0")

    def test_nonempty_transcript_without_words_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "output.json"
            metadata.write_text(
                json.dumps({"transcription": [{"text": "Words are missing.", "offsets": {"from": 0, "to": 500}}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(crispasr.CrispASRError, "did not return word timestamps"):
                crispasr._validate_word_timestamps(metadata)

    def test_runtime_probe_reports_compiled_compute_backends(self):
        version_output = """=== build info ===
  version       : 0.8.9
  ggml backends : vulkan cpu
"""
        result = stt_backends.probe_crispasr_runtime(
            environ={"CRISPASR_EXECUTABLE": "C:/tools/crispasr.exe"},
            path_exists=lambda path: str(path).replace("\\", "/") == "C:/tools/crispasr.exe",
            run_func=lambda *_args, **_kwargs: SimpleNamespace(stdout=version_output, stderr=""),
        )

        self.assertTrue(result.installed)
        self.assertEqual(result.version, "0.8.9")
        self.assertEqual(result.compute_backends, ("vulkan", "cpu"))

    def test_transcribe_source_writes_srt_and_word_metadata(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            if Path(command[0]).name.lower().startswith("ffmpeg"):
                Path(command[-1]).write_bytes(b"wav")
                return SimpleNamespace(stderr=b"")
            output_base = Path(command[command.index("-of") + 1])
            Path(f"{output_base}.srt").write_text(SAMPLE_SRT, encoding="utf-8")
            Path(f"{output_base}.json").write_text(json.dumps(_crisp_json()), encoding="utf-8")
            return SimpleNamespace(stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "meeting.v2.final.mp4"
            source.write_bytes(b"video")
            result = transcription.transcribe_source_file_with_metadata(
                temp_dir,
                source,
                {
                    "stt_engine": "whisper",
                    "stt_compute_backend": "cpu",
                    "subtitle_merge_threshold": -1,
                },
                run_func=fake_run,
            )

            self.assertTrue(Path(result.srt_path).is_file())
            self.assertTrue(Path(result.word_timestamps_path).is_file())
            self.assertEqual(result.engine, "whisper")
            self.assertEqual(result.compute_backend, "cpu")
            self.assertIn("-dtw", commands[1])
            segments = srt_utils.parse_srt(Path(result.srt_path).read_text(encoding="utf-8"))
            self.assertEqual(segments[0].text, "Hello friend.")
            self.assertGreaterEqual(segments[0].end_ms - segments[0].start_ms, 833)

    def test_missing_full_json_is_a_hard_failure(self):
        def fake_run(command, **_kwargs):
            output_base = Path(command[command.index("-of") + 1])
            Path(f"{output_base}.srt").write_text(SAMPLE_SRT, encoding="utf-8")
            return SimpleNamespace(stdout=b"", stderr=b"")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(crispasr.CrispASRError, "both SRT and full JSON"):
                crispasr.transcribe(
                    Path(temp_dir) / "audio.wav",
                    session_dir=temp_dir,
                    output_name="audio",
                    settings={"stt_engine": "parakeet"},
                    executable="crispasr-test",
                    run_func=fake_run,
                )

    def test_dubbing_handler_transcription_remains_independent_from_correction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transcribed_path = os.path.join(temp_dir, "clip.srt")
            Path(transcribed_path).write_text(SAMPLE_SRT, encoding="utf-8")
            with patch(
                "pandrator.logic.dubbing_handler.transcribe_video_file",
                return_value=transcribed_path,
            ), patch(
                "pandrator.logic.dubbing_handler.correct_srt_file_with_result",
            ) as native_correction:
                result = dubbing_handler.transcribe_video_with_metadata(
                    temp_dir,
                    os.path.join(temp_dir, "clip.mp4"),
                    {"stt_engine": "whisper", "correction_enabled": True},
                )

            self.assertEqual(result.output_path, transcribed_path)
            native_correction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
