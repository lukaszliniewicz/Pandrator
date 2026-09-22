"""Focused stubbed tests for the Qwen3 ASR transcription pipeline.

All CLI/model/network boundaries are stubbed: no native inference, no model
downloads, no audio.cpp execution. Native verification (espeak-ng synthetic
speech) is owned by the parent with the callable signature in
``/tmp/pandrator-qwen-stt-contract.md``.
"""

import json
import subprocess
import threading
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing import qwen_asr, transcription
from pandrator.logic.dubbing.qwen_asr import QwenASRError
from pandrator.logic.dubbing.settings import migrate_dubbing_payload


def _write_wav(path, *, seconds=2.0, rate=16000):
    path = Path(path)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\0\0" * frames)
    return path


def _stub_cli_factory(texts, *, with_words=True):
    """Fake audio.cpp CLI: writes transcript/words files from command args."""

    def fake_run(command, **kwargs):
        command = list(command)
        assert command[1:5] == ["--task", "asr", "--family", "qwen3_asr"], command[:6]
        assert "--mode" not in command or "streaming" not in command
        text_out = Path(command[command.index("--text-out") + 1])
        index = len(calls)
        calls.append(command)
        text_out.write_text(texts[index], encoding="utf-8")
        if "--words-out" in command:
            words_out = Path(command[command.index("--words-out") + 1])
            assert any(
                part.startswith("qwen3_asr.forced_aligner_model_path=")
                for part in command
                if isinstance(part, str)
            )
            words = (
                [
                    {"word": word, "start": 0.0 + n * 0.4, "end": 0.3 + n * 0.4}
                    for n, word in enumerate(texts[index].split())
                ]
                if with_words
                else []
            )
            words_out.write_text(json.dumps(words), encoding="utf-8")
        return SimpleNamespace(stdout=b"", stderr=b"")

    calls: list = []
    fake_run.calls = calls
    return fake_run


class QwenASRLanguageTests(unittest.TestCase):
    def test_recognizer_cover_30_aligner_11_timed_20(self):
        self.assertEqual(len(qwen_asr.QWEN3_ASR_LANGUAGE_CODES), 30)
        self.assertIn("pl", qwen_asr.QWEN3_ASR_LANGUAGE_CODES)
        self.assertIn("nl", qwen_asr.QWEN3_ASR_LANGUAGE_CODES)
        self.assertEqual(len(qwen_asr.alignment_languages()), 11)
        timed = qwen_asr.timed_supported_languages()
        self.assertEqual(len(timed), 20)
        for code in qwen_asr.alignment_languages():
            self.assertIn(code, timed)
        # Canary fallback intersect contributes Polish/Dutch/Czech/...
        for code in ("pl", "nl", "cs", "sv", "da", "fi", "ro", "el", "hu"):
            self.assertIn(code, timed)
            self.assertEqual(qwen_asr.timing_plan_for_language(code), "canary_ctc_fallback")
        for code in ("ar", "id", "th", "vi", "tr", "hi", "ms", "fil", "fa", "mk"):
            self.assertEqual(qwen_asr.timing_plan_for_language(code), "unsupported")
        for code in ("en", "de", "ja", "zh"):
            self.assertEqual(
                qwen_asr.timing_plan_for_language(code), "qwen3_forced_aligner"
            )

    def test_unsupported_asr_language_rejected(self):
        with self.assertRaises(QwenASRError):
            qwen_asr.normalize_qwen_asr_language("xx")

    def test_timestamp_preflight_fails_before_any_download_or_work(self):
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav")
            with patch.object(
                qwen_asr, "ensure_asr_model", side_effect=AssertionError("must not run")
            ):
                with self.assertRaisesRegex(QwenASRError, "unsupported_qwen_timestamps"):
                    qwen_asr.transcribe(
                        audio,
                        session_dir=temp_dir,
                        output_name="clip",
                        settings={
                            "stt_language": "Arabic",
                            "qwen_asr_chunk_seconds": 30,
                        },
                        run_func=lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("CLI must not run")
                        ),
                    )

    def test_auto_timestamps_rejected_preflight_before_download(self):
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav")
            with patch.object(
                qwen_asr, "ensure_asr_model", side_effect=AssertionError("must not run")
            ):
                with self.assertRaisesRegex(QwenASRError, "explicit"):
                    qwen_asr.transcribe(
                        audio,
                        session_dir=temp_dir,
                        output_name="clip",
                        settings={"stt_language": "auto"},
                        run_func=lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("CLI must not run")
                        ),
                    )

    def test_transcript_only_never_loads_aligner(self):
        for language in ("auto", "en", "pl"):
            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                audio = _write_wav(root / "clip.wav")
                fake_run = _stub_cli_factory(["recognized words"], with_words=False)
                with (
                    patch.object(
                        qwen_asr, "ensure_asr_model", return_value=root / "model.gguf"
                    ),
                    patch.object(
                        qwen_asr,
                        "ensure_aligner_model",
                        side_effect=AssertionError("transcript-only must not align"),
                    ),
                ):
                    result = qwen_asr.transcribe(
                        audio,
                        session_dir=temp_dir,
                        output_name="clip",
                        settings={"stt_language": language},
                        executable="audiocpp_cli",
                        run_func=fake_run,
                        require_word_timestamps=False,
                    )
                payload = json.loads(Path(result.word_timestamps_path).read_text())
                self.assertEqual(payload["language"], language)
                self.assertEqual(payload["segments"][0]["text"], "recognized words")
                self.assertEqual(payload["segments"][0]["words"], [])
                self.assertEqual(payload["metadata"]["timing_plan"], "not_requested")


    def test_capabilities_expose_pipeline_keys(self):
        caps = qwen_asr.capabilities()
        self.assertEqual(caps["kind"], "recognizer")
        self.assertEqual(len(caps["recognizer_languages"]), 30)
        self.assertEqual(len(caps["alignment_languages"]), 11)
        self.assertEqual(len(caps["timed_supported_languages"]), 20)
        self.assertEqual(len(caps["transcript_only_languages"]), 10)
        self.assertTrue(caps["requires_explicit_language_for_timestamps"])
        self.assertEqual(caps["timing_fallback"], "canary_ctc_where_supported")


class QwenASRChunkingTests(unittest.TestCase):
    def _settings(self, **overrides):
        settings = {
            "stt_engine": "qwen3",
            "stt_language": "en",
            "qwen_asr_model": "qwen3_asr_0_6b",
            "qwen_asr_chunk_seconds": 30,
            "qwen_asr_max_tokens": 512,
        }
        settings.update(overrides)
        return settings

    def test_offsets_retained_across_chunks(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = _write_wav(root / "long.wav", seconds=65.0)
            fake_run = _stub_cli_factory(["hello world", "second chunk", "third chunk"])
            with (
                patch.object(
                    qwen_asr, "ensure_asr_model", return_value=root / "model.gguf"
                ),
                patch.object(
                    qwen_asr, "ensure_aligner_model", return_value=root / "a.gguf"
                ),
            ):
                result = qwen_asr.transcribe(
                    audio,
                    session_dir=temp_dir,
                    output_name="long",
                    settings=self._settings(),
                    executable="/usr/bin/audiocpp_cli",
                    run_func=fake_run,
                )
            self.assertEqual(len(fake_run.calls), 3)
            payload = json.loads(Path(result.word_timestamps_path).read_text())
            self.assertEqual(payload["schema"], "pandrator.transcript.v1")
            starts = [seg["start_ms"] for seg in payload["segments"]]
            self.assertEqual(starts, [0, 30000, 60000])
            # Word offsets rebased onto the original timeline.
            second_words = payload["segments"][1]["words"]
            self.assertGreaterEqual(second_words[0]["start_ms"], 30000)
            self.assertEqual(payload["metadata"]["chunk_count"], 3)

    def test_missing_chunk_output_is_hard_failure(self):
        def broken_run(command, **kwargs):
            command = list(command)
            text_out = Path(command[command.index("--text-out") + 1])
            if len(calls) == 1:
                raise FileNotFoundError("cli gone")
            calls.append(command)
            text_out.write_text("hello", encoding="utf-8")
            return SimpleNamespace(stdout=b"", stderr=b"")

        calls: list = []
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "long.wav", seconds=65.0)
            with (
                patch.object(
                    qwen_asr, "ensure_asr_model", return_value=Path(temp_dir) / "m.gguf"
                ),
                patch.object(
                    qwen_asr, "ensure_aligner_model", return_value=Path(temp_dir) / "a"
                ),
            ):
                with self.assertRaises(QwenASRError):
                    qwen_asr.transcribe(
                        audio,
                        session_dir=temp_dir,
                        output_name="long",
                        settings=self._settings(),
                        executable="cli",
                        run_func=broken_run,
                    )

    def test_cancel_before_work_raises_without_cli(self):
        event = threading.Event()
        event.set()
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav")
            with self.assertRaises(ProcessCancelled):
                qwen_asr.transcribe(
                    audio,
                    session_dir=temp_dir,
                    output_name="clip",
                    settings=self._settings(),
                    run_func=lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("CLI must not run")
                    ),
                    cancel_event=event,
                )

    def test_token_budget_preflight_and_truncation_detection(self):
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav")
            with self.assertRaisesRegex(QwenASRError, "qwen_asr_max_tokens"):
                qwen_asr.transcribe(
                    audio,
                    session_dir=temp_dir,
                    output_name="clip",
                    settings=self._settings(
                        qwen_asr_chunk_seconds=120, qwen_asr_max_tokens=512
                    ),
                    run_func=lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("CLI must not run")
                    ),
                )
            # Near-budget text without a clean ending is refused, not truncated.
            with self.assertRaisesRegex(QwenASRError, "token budget"):
                qwen_asr._check_truncation(
                    chunk_index=1,
                    is_final=False,
                    text="word " * 130,
                    max_tokens=512,
                )

    def test_canary_fallback_for_timed_non_qwen_language(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = _write_wav(root / "clip.wav", seconds=5.0)
            fake_run = _stub_cli_factory(["witaj swiecie"], with_words=False)
            ctc_calls: list = []

            def fake_ctc(audio_path, text_path, output_path, settings, **kwargs):
                ctc_calls.append((audio_path, text_path, output_path))
                return [
                    {"word": "witaj", "start": 0.1, "end": 0.5},
                    {"word": "swiecie", "start": 0.6, "end": 1.0},
                ]

            with (
                patch.object(
                    qwen_asr, "ensure_asr_model", return_value=root / "model.gguf"
                ),
                patch("pandrator.logic.dubbing.crispasr.run_ctc_alignment", fake_ctc),
            ):
                result = qwen_asr.transcribe(
                    audio,
                    session_dir=temp_dir,
                    output_name="clip",
                    settings=self._settings(stt_language="pl"),
                    executable="cli",
                    run_func=fake_run,
                )
            self.assertEqual(len(ctc_calls), 1)
            payload = json.loads(Path(result.word_timestamps_path).read_text())
            self.assertEqual(
                payload["metadata"]["word_timing"], "canary_ctc_fallback"
            )
            self.assertEqual(len(payload["segments"][0]["words"]), 2)


class QwenASRCommandTests(unittest.TestCase):
    def test_command_uses_only_documented_flags(self):
        command = qwen_asr.build_asr_command(
            "in.wav",
            "out.txt",
            {
                "stt_compute_backend": "cpu",
                "qwen_asr_max_tokens": 512,
                "qwen_asr_chunk_mode": "auto",
            },
            executable="audiocpp_cli",
            model_path="model.gguf",
            words_path="words.json",
            aligner_model_path="align.gguf",
            language_code="en",
        )
        self.assertEqual(command[:6], ["audiocpp_cli", "--task", "asr", "--family", "qwen3_asr", "--model"])
        for banned in ("--hf-repo", "-m", "--auto-download", "--align-only", "-am"):
            self.assertNotIn(banned, command)
        self.assertIn("--session-option", command)
        self.assertTrue(
            any("qwen3_asr.forced_aligner_model_path=" in part for part in command)
        )
        self.assertEqual(command[command.index("--language") + 1], "English")
        # auto with bounded outer chunks (default 30 s) resolves to none,
        # keeping the unbundled VAD path out of the invocation.
        self.assertEqual(command[command.index("--audio-chunk-mode") + 1], "none")

    def test_chunk_mode_resolution(self):
        self.assertEqual(
            qwen_asr.resolve_chunk_mode({"qwen_asr_chunk_mode": "auto"}, 30), "none"
        )
        self.assertEqual(
            qwen_asr.resolve_chunk_mode({"qwen_asr_chunk_mode": "auto"}, 0), "fixed"
        )
        self.assertEqual(
            qwen_asr.resolve_chunk_mode({"qwen_asr_chunk_mode": "none"}, 0), "none"
        )
        self.assertEqual(
            qwen_asr.resolve_chunk_mode({"qwen_asr_chunk_mode": "vad"}, 30), "vad"
        )
        with self.assertRaises(QwenASRError):
            qwen_asr.resolve_chunk_mode({"qwen_asr_chunk_mode": "stream"}, 30)

    def test_native_error_stage_preserved_sanitized(self):
        import subprocess as _subprocess

        error = _subprocess.CalledProcessError(
            1,
            ["audiocpp_cli", "--audio", "secret-path.wav"],
            stderr=b"ggml_vulkan: noise\naudiocpp_cli failed: Silero VAD model path does not exist: assets/framework/models/silero_vad\n",
        )
        tail = qwen_asr.native_error_tail(error)
        self.assertIn("Silero VAD model path does not exist", tail)
        self.assertNotIn("secret-path.wav", tail)

        def failing_run(command, **kwargs):
            raise error

        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav", seconds=5.0)
            with (
                patch.object(
                    qwen_asr, "ensure_asr_model", return_value=Path(temp_dir) / "m.gguf"
                ),
                patch.object(
                    qwen_asr, "ensure_aligner_model", return_value=Path(temp_dir) / "a"
                ),
            ):
                with self.assertRaisesRegex(
                    QwenASRError, r"chunk 1.*audio\.cpp asr task.*exit 1.*Silero VAD"
                ):
                    qwen_asr.transcribe(
                        audio,
                        session_dir=temp_dir,
                        output_name="clip",
                        settings={
                            "stt_language": "en",
                            "qwen_asr_chunk_seconds": 30,
                        },
                        executable="cli",
                        run_func=failing_run,
                    )

    def test_settings_migration_defaults(self):
        migrated = migrate_dubbing_payload({}, None)
        self.assertEqual(migrated["qwen_asr_model"], "qwen3_asr_0_6b")
        self.assertEqual(migrated["transcription_vocal_isolation"], "off")
        self.assertEqual(migrated["qwen_asr_chunk_seconds"], 30)
        self.assertEqual(migrated["qwen_asr_max_tokens"], 512)
        self.assertEqual(migrated["qwen_asr_chunk_mode"], "auto")
        self.assertEqual(migrated["qwen_asr_timeout_seconds"], 3600)
        self.assertFalse(migrated["qwen_asr_clamp_timestamps"])


class NativeFixtureRegressionTests(unittest.TestCase):
    """Regression against the parent-owned native smoke fixtures.

    Fixtures vendored from /tmp/pandrator-qwen-native-smoke-hi22ov33
    (chunk mode none, word timestamps): native transcript text plus the
    native ``start_sample``/``end_sample`` word array. Audio bounds come
    from a synthesized WAV with the exact native frame count (97809 @
    16 kHz = 6113 ms). No native process runs here.
    """

    FIXTURES = Path(__file__).parent / "data" / "qwen_asr_native"
    NATIVE_FRAMES = 97809
    NATIVE_MS = 6113

    def _native_audio(self, root: Path) -> Path:
        audio = root / "speech16k.wav"
        with wave.open(str(audio), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\0\0" * self.NATIVE_FRAMES)
        return audio

    def test_native_words_validate_against_audio_bounds(self):
        payload = json.loads((self.FIXTURES / "no-vad.words.json").read_text())
        words = qwen_asr.parse_qwen_words(
            payload, chunk_ms=0, duration_ms=self.NATIVE_MS
        )
        self.assertEqual(len(words), 13)
        self.assertEqual(words[0]["word"], "This")
        self.assertEqual(words[0]["start"], 0.0)
        self.assertEqual(words[-1]["word"], "unchanged")
        self.assertEqual(words[-1]["end"], 5.68)
        text = (self.FIXTURES / "no-vad.txt").read_text(encoding="utf-8").strip()
        import re as _re

        def _normalize(value: str) -> str:
            return _re.sub(r"[^0-9a-z]+", " ", value.casefold()).strip()

        self.assertEqual(
            _normalize(" ".join(item["word"] for item in words)), _normalize(text)
        )

    def test_native_words_outside_bounds_rejected(self):
        payload = json.loads((self.FIXTURES / "no-vad.words.json").read_text())
        payload.append({"word": "extra", "start_sample": 200000, "end_sample": 210000})
        with self.assertRaisesRegex(QwenASRError, "outside the audio bounds"):
            qwen_asr.parse_qwen_words(
                payload, chunk_ms=0, duration_ms=self.NATIVE_MS
            )
        with self.assertRaises(QwenASRError):
            qwen_asr.parse_qwen_words(payload, chunk_ms=0, duration_ms=0)

    def test_native_fixture_end_to_end_with_mode_none(self):
        fixture_text = (self.FIXTURES / "no-vad.txt").read_text(encoding="utf-8")
        fixture_words = json.loads(
            (self.FIXTURES / "no-vad.words.json").read_text(encoding="utf-8")
        )

        def fixture_run(command, **kwargs):
            command = list(command)
            self.assertEqual(
                command[command.index("--audio-chunk-mode") + 1], "none"
            )
            Path(command[command.index("--text-out") + 1]).write_text(
                fixture_text, encoding="utf-8"
            )
            Path(command[command.index("--words-out") + 1]).write_text(
                json.dumps(fixture_words), encoding="utf-8"
            )
            return SimpleNamespace(stdout=b"", stderr=b"")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = self._native_audio(root)
            with (
                patch.object(
                    qwen_asr, "ensure_asr_model", return_value=root / "model.gguf"
                ),
                patch.object(
                    qwen_asr, "ensure_aligner_model", return_value=root / "a.gguf"
                ),
            ):
                result = qwen_asr.transcribe(
                    audio,
                    session_dir=temp_dir,
                    output_name="smoke",
                    settings={
                        "stt_language": "en",
                        "qwen_asr_model": "qwen3_asr_0_6b",
                        "qwen_asr_chunk_seconds": 30,
                        "qwen_asr_chunk_mode": "none",
                    },
                    executable="audiocpp_cli",
                    run_func=fixture_run,
                )
            payload = json.loads(Path(result.word_timestamps_path).read_text())
            self.assertEqual(len(payload["segments"]), 1)
            self.assertEqual(len(payload["segments"][0]["words"]), 13)
            last = payload["segments"][0]["words"][-1]
            self.assertEqual((last["text"], last["end_ms"]), ("unchanged.", 5680))
            # Restored transcript punctuation: bare native surfaces ("test")
            # project back to exact transcript graphemes ("test.").
            words = payload["segments"][0]["words"]
            self.assertEqual(words[6]["text"], "test.")
            self.assertEqual(
                " ".join(word["text"] for word in words),
                fixture_text.strip(),
            )

    def test_native_fixture_full_pipeline_srt_keeps_punctuation(self):
        fixture_text = (self.FIXTURES / "no-vad.txt").read_text(encoding="utf-8")
        fixture_words = json.loads(
            (self.FIXTURES / "no-vad.words.json").read_text(encoding="utf-8")
        )

        def routing_run(command, **kwargs):
            name = Path(command[0]).name
            if name.startswith("ffmpeg"):
                with wave.open(str(command[-1]), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(16000)
                    target.writeframes(b"\0\0" * self.NATIVE_FRAMES)
                return SimpleNamespace(stdout=b"", stderr=b"")
            command = list(command)
            Path(command[command.index("--text-out") + 1]).write_text(
                fixture_text, encoding="utf-8"
            )
            Path(command[command.index("--words-out") + 1]).write_text(
                json.dumps(fixture_words), encoding="utf-8"
            )
            return SimpleNamespace(stdout=b"", stderr=b"")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "talk.mp4"
            source.write_bytes(b"video")
            with (
                patch.object(
                    qwen_asr, "ensure_asr_model", return_value=root / "model.gguf"
                ),
                patch.object(
                    qwen_asr, "ensure_aligner_model", return_value=root / "a.gguf"
                ),
                patch.object(
                    qwen_asr, "resolve_executable", return_value=root / "audiocpp_cli"
                ),
            ):
                result = transcription.transcribe_source_file_with_metadata(
                    temp_dir,
                    source,
                    {
                        "stt_engine": "qwen3",
                        "stt_language": "en",
                        "qwen_asr_chunk_seconds": 30,
                        "qwen_asr_chunk_mode": "none",
                    },
                    run_func=routing_run,
                )
            self.assertEqual(result.engine, "qwen3")
            srt = Path(result.srt_path).read_text(encoding="utf-8")
            self.assertIn("test.", srt)
            self.assertIn("unchanged.", srt)


class NativeTimeoutTests(unittest.TestCase):
    def test_invalid_timeout_rejected_preflight(self):
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav")
            with self.assertRaisesRegex(QwenASRError, "qwen_asr_timeout_seconds"):
                qwen_asr.transcribe(
                    audio,
                    session_dir=temp_dir,
                    output_name="clip",
                    settings={"stt_language": "en", "qwen_asr_timeout_seconds": 5},
                    run_func=lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("must not run")
                    ),
                )

    def test_hung_native_call_fails_on_finite_timeout(self):
        with self.assertRaisesRegex(QwenASRError, "timed out after 1 seconds"):
            qwen_asr._run_tool(
                ["sleep", "30"],
                run_func=subprocess.run,
                cancel_event=None,
                timeout=1,
            )

    def test_caller_cancel_still_reports_cancelled(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        timer.start()
        try:
            with self.assertRaises(ProcessCancelled):
                qwen_asr._run_tool(
                    ["sleep", "30"],
                    run_func=subprocess.run,
                    cancel_event=event,
                    timeout=60,
                )
        finally:
            timer.cancel()


class VocalIsolationOwnershipTests(unittest.TestCase):
    def test_explicit_isolation_failure_never_silently_skips(self):
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "mix.wav")
            with patch(
                "pandrator.logic.audio_cpp_processing.isolate_vocals",
                side_effect=RuntimeError("separation exploded"),
            ):
                with self.assertRaises(QwenASRError):
                    transcription.apply_vocal_isolation(
                        audio,
                        temp_dir,
                        "mix",
                        {"transcription_vocal_isolation": "bs_roformer"},
                    )

    def test_derivative_used_original_retained_duration_preserved(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = _write_wav(root / "mix.wav", seconds=3.0)
            original_bytes = audio.read_bytes()

            def fake_isolate(source, destination, settings, **kwargs):
                # Separator output at its native rate; duration matches.
                with wave.open(str(source), "rb") as origin:
                    nframes = origin.getnframes()
                    rate = origin.getframerate()
                duration = nframes / rate
                out_frames = int(duration * 44100)
                with wave.open(str(destination), "wb") as target:
                    target.setnchannels(2)
                    target.setsampwidth(2)
                    target.setframerate(44100)
                    target.writeframes(b"\0\0" * out_frames * 2)
                progress = kwargs.get("progress")
                if progress is not None:
                    progress("isolate", 1, 1)
                    progress("install", 1, 1)
                return {"status": "isolated", "model": "bs_roformer"}

            def fake_run(command, **kwargs):
                # ffmpeg resample stub: same duration at 16 kHz mono.
                with wave.open(str(command[command.index("-i") + 1]), "rb") as origin:
                    duration = origin.getnframes() / origin.getframerate()
                out_frames = int(duration * 16000)
                with wave.open(str(command[-1]), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(16000)
                    target.writeframes(b"\0\0" * out_frames)
                return SimpleNamespace(stdout=b"", stderr=b"")

            with patch(
                "pandrator.logic.audio_cpp_processing.isolate_vocals", fake_isolate
            ):
                derivative, provenance = transcription.apply_vocal_isolation(
                    audio,
                    temp_dir,
                    "mix",
                    {"transcription_vocal_isolation": "mel_band_roformer"},
                    run_func=fake_run,
                )
            # Original retained for playback/export.
            self.assertEqual(audio.read_bytes(), original_bytes)
            self.assertNotEqual(Path(derivative), audio)
            with wave.open(derivative, "rb") as check:
                self.assertEqual(check.getframerate(), 16000)
                self.assertEqual(check.getnchannels(), 1)
            self.assertEqual(provenance["status"], "isolated")


class TranscriptionDispatchTests(unittest.TestCase):
    def test_qwen3_engine_routes_to_qwen_asr(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "talk.mp4"
            source.write_bytes(b"video")
            words_path = root / "talk_words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "schema": "pandrator.transcript.v1",
                        "source_format": "qwen3-asr",
                        "language": "en",
                        "metadata": {},
                        "segments": [
                            {
                                "id": "qwen3-chunk-0001",
                                "start_ms": 0,
                                "end_ms": 1000,
                                "text": "Hello world.",
                                "words": [
                                    {"text": "Hello", "start_ms": 0, "end_ms": 400},
                                    {"text": "world.", "start_ms": 450, "end_ms": 900},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            srt_path = root / "talk.srt"
            srt_path.write_text("", encoding="utf-8")
            from pandrator.logic.dubbing.crispasr import CrispASRTranscriptionResult

            stub_result = CrispASRTranscriptionResult(
                srt_path=str(srt_path),
                word_timestamps_path=str(words_path),
                engine="qwen3",
                compute_backend="cpu",
            )

            def fake_ffmpeg(command, **kwargs):
                Path(command[-1]).write_bytes(b"wav")
                return SimpleNamespace(stderr=b"")

            with patch.object(
                qwen_asr, "transcribe", return_value=stub_result
            ) as asr_call:
                result = transcription.transcribe_source_file_with_metadata(
                    temp_dir,
                    source,
                    {"stt_engine": "qwen3", "stt_language": "en"},
                    run_func=fake_ffmpeg,
                )
            asr_call.assert_called_once()
            self.assertEqual(result.engine, "qwen3")
            self.assertTrue(Path(result.srt_path).is_file())


class SettingsContractTests(unittest.TestCase):
    def test_present_invalid_model_and_isolation_raise_in_migration(self):
        with self.assertRaisesRegex(ValueError, "qwen_asr_model"):
            migrate_dubbing_payload({"qwen_asr_model": "qwen3_asr_9_9b"}, None)
        with self.assertRaisesRegex(ValueError, "transcription_vocal_isolation"):
            migrate_dubbing_payload(
                {"transcription_vocal_isolation": "demucs"}, None
            )
        # Truly legacy/absent values still fall back quietly.
        migrated = migrate_dubbing_payload({}, None)
        self.assertEqual(migrated["qwen_asr_model"], "qwen3_asr_0_6b")
        self.assertEqual(migrated["transcription_vocal_isolation"], "off")

    def test_save_contract_validates_qwen_enums(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from pandrator.web.database import Database
        from pandrator.web.sessions import SessionService
        from pandrator.web.workspace import WorkspaceSettingsService
        from tests.web_test_support import prepare_web_test_data_root

        temporary = _tempfile.TemporaryDirectory()
        try:
            paths = prepare_web_test_data_root(_Path(temporary.name))
            database = Database(paths.database)
            try:
                service = WorkspaceSettingsService(database)
                record = SessionService(database).create(
                    "Qwen contract", workflow_kind="voiceover"
                )
                with self.assertRaisesRegex(ValueError, "qwen_asr_model"):
                    service.update(
                        record.id, "stt", 0, {"qwen_asr_model": "qwen3_asr_9_9b"}
                    )
                with self.assertRaisesRegex(
                    ValueError, "transcription_vocal_isolation"
                ):
                    service.update(
                        record.id, "stt", 0, {"transcription_vocal_isolation": "demucs"}
                    )
                with self.assertRaisesRegex(ValueError, "qwen_asr_max_tokens"):
                    service.update(record.id, "stt", 0, {"qwen_asr_max_tokens": 8})
                saved = service.update(
                    record.id,
                    "stt",
                    0,
                    {
                        "stt_engine": "qwen3",
                        "qwen_asr_model": "qwen3_asr_1_7b",
                        "transcription_vocal_isolation": "bs_roformer",
                    },
                )
                self.assertEqual(
                    saved["effective"]["qwen_asr_model"], "qwen3_asr_1_7b"
                )
                self.assertEqual(
                    saved["effective"]["transcription_vocal_isolation"], "bs_roformer"
                )
            finally:
                database.dispose()
        finally:
            temporary.cleanup()


class TranscriptionPreflightTests(unittest.TestCase):
    def _assert_qwen_fails_before_preprocessing(self, settings, message):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = root / "not-created" / "session"
            source = root / "clip.mp4"
            with (
                patch.object(
                    transcription,
                    "extract_audio",
                    side_effect=AssertionError("audio extraction must not run"),
                ) as extract_audio,
                patch.object(
                    transcription,
                    "apply_vocal_isolation",
                    side_effect=AssertionError("vocal isolation must not run"),
                ) as isolate,
                patch.object(
                    qwen_asr,
                    "ensure_asr_model",
                    side_effect=AssertionError("model provisioning must not run"),
                ) as ensure_model,
            ):
                with self.assertRaisesRegex(QwenASRError, message):
                    transcription.transcribe_source_file_with_metadata(
                        session_dir,
                        source,
                        settings,
                    )
            extract_audio.assert_not_called()
            isolate.assert_not_called()
            ensure_model.assert_not_called()
            self.assertFalse(session_dir.exists())

    def test_arabic_with_selected_isolation_fails_before_preprocessing(self):
        self._assert_qwen_fails_before_preprocessing(
            {
                "stt_engine": "qwen3",
                "stt_language": "Arabic",
                "transcription_vocal_isolation": "bs_roformer",
            },
            "unsupported_qwen_timestamps:ar",
        )

    def test_auto_with_selected_isolation_alias_fails_before_preprocessing(self):
        self._assert_qwen_fails_before_preprocessing(
            {
                "stt_engine": "qwen3-asr",
                "stt_language": "auto",
                "transcription_vocal_isolation": "mel_band_roformer",
            },
            "needs an explicit, validated source language",
        )

    def test_supported_polish_preflight_accepts_canary_fallback(self):
        validated = qwen_asr.validate_transcription_settings(
            {
                "stt_language": "pl",
                "transcription_vocal_isolation": "bs_roformer",
            }
        )
        self.assertEqual(validated["language"], "pl")
        self.assertEqual(validated["isolation"], "bs_roformer")
        self.assertEqual(
            qwen_asr.timing_plan_for_language(validated["language"]),
            "canary_ctc_fallback",
        )

    def test_invalid_qwen_options_fail_before_preprocessing(self):
        invalid_settings = (
            ({"qwen_asr_model": "qwen3_asr_9_9b"}, "Unknown Qwen3 ASR model"),
            ({"qwen_asr_backend": "npu"}, "Unsupported audio.cpp ASR backend"),
            ({"qwen_asr_chunk_seconds": 5}, "qwen_asr_chunk_seconds"),
            ({"qwen_asr_chunk_mode": "stream"}, "Unknown qwen_asr_chunk_mode"),
            ({"qwen_asr_max_tokens": 8}, "qwen_asr_max_tokens"),
            (
                {"qwen_asr_chunk_seconds": 120, "qwen_asr_max_tokens": 512},
                "minimum 960",
            ),
            ({"qwen_asr_timeout_seconds": 5}, "qwen_asr_timeout_seconds"),
            (
                {"transcription_vocal_isolation": "demucs"},
                "Unknown transcription_vocal_isolation",
            ),
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(
                    transcription,
                    "extract_audio",
                    side_effect=AssertionError("audio extraction must not run"),
                ) as extract_audio,
                patch.object(
                    transcription,
                    "apply_vocal_isolation",
                    side_effect=AssertionError("vocal isolation must not run"),
                ) as isolate,
                patch.object(
                    qwen_asr,
                    "ensure_asr_model",
                    side_effect=AssertionError("model provisioning must not run"),
                ) as ensure_model,
            ):
                for index, (overrides, message) in enumerate(invalid_settings):
                    settings = {
                        "stt_engine": "qwen",
                        "stt_language": "en",
                        **overrides,
                    }
                    session_dir = root / f"session-{index}"
                    with self.subTest(overrides=overrides):
                        with self.assertRaisesRegex(QwenASRError, message):
                            transcription.transcribe_source_file_with_metadata(
                                session_dir,
                                root / "clip.mp4",
                                settings,
                            )
                        self.assertFalse(session_dir.exists())
            extract_audio.assert_not_called()
            isolate.assert_not_called()
            ensure_model.assert_not_called()

    def test_non_qwen_does_not_run_qwen_preflight(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write_wav(root / "clip.wav")
            with (
                patch.object(
                    qwen_asr,
                    "validate_transcription_settings",
                    side_effect=AssertionError("Qwen preflight must not run"),
                ) as validate,
                patch.object(
                    transcription,
                    "transcribe",
                    side_effect=RuntimeError("non-Qwen recognizer reached"),
                ) as recognizer,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "non-Qwen recognizer reached"
                ):
                    transcription.transcribe_source_file_with_metadata(
                        root / "session",
                        source,
                        {
                            "stt_engine": "whisper",
                            "stt_language": "Arabic",
                            "qwen_asr_model": "invalid-model",
                            "qwen_asr_backend": "invalid-backend",
                        },
                        source_is_normalized=True,
                    )
            validate.assert_not_called()
            recognizer.assert_called_once()

    def test_direct_transcribe_still_rejects_isolation_as_wiring_error(self):
        with TemporaryDirectory() as temp_dir:
            audio = _write_wav(Path(temp_dir) / "clip.wav")
            with patch.object(
                qwen_asr,
                "ensure_asr_model",
                side_effect=AssertionError("model provisioning must not run"),
            ) as ensure_model:
                with self.assertRaisesRegex(
                    QwenASRError, "must be applied by the transcription orchestrator"
                ):
                    qwen_asr.transcribe(
                        audio,
                        session_dir=temp_dir,
                        output_name="clip",
                        settings={
                            "stt_language": "en",
                            "transcription_vocal_isolation": "bs_roformer",
                        },
                    )
            ensure_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
