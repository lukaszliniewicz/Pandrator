"""Focused Qwen3/CrispASR contract tests; no native inference or network I/O."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing import crispasr, crispasr_qwen_assets, qwen_asr, transcription
from pandrator.logic.dubbing.qwen_asr import QwenASRError


def _wav(path: Path, seconds: float = 2.0) -> Path:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * int(seconds * 16000))
    return path


def _fake_cli(words: bool = True, *, invalid: str = ""):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        base = Path(command[command.index("-of") + 1])
        Path(f"{base}.srt").write_text("1\n00:00:00,100 --> 00:00:01,000\nHello world.\n", encoding="utf-8")
        entries = [
            {"text": "Hello", "offsets": {"from": 100, "to": 400}},
            {"text": "world.", "offsets": {"from": 450, "to": 950}},
        ] if words else []
        if invalid == "missing":
            entries = []
        elif invalid == "outside":
            entries[-1]["offsets"]["to"] = 999999
        elif invalid == "nonfinite":
            entries[-1]["offsets"]["to"] = float("nan")
        elif invalid == "reversed":
            entries[-1]["offsets"]["from"] = 100
        Path(f"{base}.json").write_text(
            json.dumps({"transcription": [{"text": "Hello world.", "words": entries}], "native_extra": {"retained": True}}),
            encoding="utf-8",
        )
        return SimpleNamespace(stderr=b"", stdout=b"")

    run.commands = commands
    return run


def _transcribe(tmp_path: Path, monkeypatch, *, settings=None, fake=None, timed=True):
    audio = _wav(tmp_path / "audio.wav")
    monkeypatch.setattr(qwen_asr, "ensure_asr_model", lambda *_args: tmp_path / "qwen.gguf")
    monkeypatch.setattr(qwen_asr, "ensure_aligner_model", lambda *_args: tmp_path / "align.gguf")
    return qwen_asr.transcribe(
        audio,
        session_dir=tmp_path,
        output_name="result",
        settings={"stt_language": "en", "stt_compute_backend": "cpu", **(settings or {})},
        executable="crispasr",
        run_func=fake or _fake_cli(),
        require_word_timestamps=timed,
    )


def test_language_and_timing_coverage():
    assert len(qwen_asr.recognizer_languages()) == 30
    assert len(qwen_asr.alignment_languages()) == 11
    assert len(qwen_asr.timed_supported_languages()) == 20
    assert qwen_asr.timing_plan_for_language("pl") == "canary_ctc_fallback"
    assert qwen_asr.timing_plan_for_language("ar") == "unsupported"
    assert qwen_asr.normalize_qwen_asr_model("qwen3_asr_1_7b") == "qwen3_asr_1_7b"


def test_pinned_model_and_aligner_specs():
    assets = crispasr_qwen_assets.ASSETS
    assert assets["qwen3_asr_0_6b"].size == 1006809760
    assert assets["qwen3_asr_1_7b"].size == 2506723200
    assert assets["qwen3_forced_aligner"].size == 985594624
    assert all(asset.filename.endswith("q8_0.gguf") for asset in assets.values())
    assert all(len(asset.sha256) == 64 and len(asset.revision) == 40 for asset in assets.values())
    assert list(crispasr.MODELS)[:3] == ["whisper", "parakeet", "moss"]


def test_old_runtime_rejected_before_download(tmp_path, monkeypatch):
    executable = tmp_path / "crispasr"
    executable.write_bytes(b"fake executable")
    qwen_asr._runtime_version_for_file.cache_clear()
    with patch.object(
        qwen_asr.subprocess,
        "run",
        return_value=SimpleNamespace(stdout="CrispASR v0.8.32", stderr="", returncode=0),
    ):
        with pytest.raises(QwenASRError, match="too old"):
            qwen_asr._verify_qwen_runtime(str(executable))
    qwen_asr._runtime_version_for_file.cache_clear()
    with patch.object(
        qwen_asr.subprocess,
        "run",
        return_value=SimpleNamespace(stdout="version: 0.8.36", stderr="", returncode=0),
    ):
        qwen_asr._verify_qwen_runtime(str(executable))


@pytest.mark.parametrize("language", ["auto", "ar"])
def test_timed_preflight_rejects_before_download(tmp_path, monkeypatch, language):
    audio = _wav(tmp_path / "audio.wav")
    monkeypatch.setattr(qwen_asr, "ensure_asr_model", lambda *_args: pytest.fail("download ran"))
    with pytest.raises(QwenASRError):
        qwen_asr.transcribe(
            audio, session_dir=tmp_path, output_name="result",
            settings={"stt_language": language}, run_func=lambda *_a, **_kw: pytest.fail("CLI ran"),
        )


def test_preflight_rejects_long_chunk_underfunded_tokens_and_invalid_timeout():
    with pytest.raises(QwenASRError, match="minimum 960"):
        qwen_asr.validate_transcription_settings({"stt_language": "en", "qwen_asr_chunk_seconds": 120})
    with pytest.raises(QwenASRError, match="timeout"):
        qwen_asr.validate_transcription_settings({"stt_language": "en", "qwen_asr_timeout_seconds": float("inf")})
    assert qwen_asr.validate_transcription_settings({"stt_language": "en", "qwen_asr_chunk_seconds": 0})["chunk_seconds"] == 30


@pytest.mark.parametrize("model_id", qwen_asr.QWEN3_ASR_MODELS)
def test_qwen_command_native_flags_and_model_size(tmp_path, monkeypatch, model_id):
    fake = _fake_cli()
    result = _transcribe(tmp_path, monkeypatch, settings={"qwen_asr_model": model_id}, fake=fake)
    command = fake.commands[0]
    assert command[command.index("--backend") + 1] == "qwen3"
    assert command[command.index("-m") + 1] == str(tmp_path / "qwen.gguf")
    assert command[command.index("-l") + 1] == "en"
    assert command[command.index("-am") + 1] == str(tmp_path / "align.gguf")
    assert command[command.index("--max-new-tokens") + 1] == "512"
    assert command[command.index("--chunk-seconds") + 1] == "30"
    assert float(command[command.index("--vad-max-speech-duration-s") + 1]) == 30
    assert "--vad" in command
    assert "--strict-pipeline" in command
    assert "--require-word-timestamps" in command
    for old in ("--task", "--family", "--text-out", "--words-out", "--session-option"):
        assert old not in command
    assert result.engine == "qwen3"
    payload = json.loads(Path(result.word_timestamps_path).read_text())
    assert payload["native_extra"] == {"retained": True}
    assert Path(result.srt_path).exists()


@pytest.mark.parametrize(
    ("mode", "shared_vad", "expected"),
    [("auto", True, True), ("auto", False, False), ("vad", False, True), ("fixed", True, False), ("none", True, False)],
)
def test_chunk_vad_compatibility(tmp_path, monkeypatch, mode, shared_vad, expected):
    fake = _fake_cli()
    _transcribe(tmp_path, monkeypatch, settings={"qwen_asr_chunk_mode": mode, "crispasr_vad_enabled": shared_vad}, fake=fake)
    command = fake.commands[0]
    assert ("--vad" in command) is expected
    assert "--chunk-seconds" in command
    assert command[command.index("--chunk-overlap") + 1] == "3"


def test_transcript_only_needs_no_aligner(tmp_path, monkeypatch):
    monkeypatch.setattr(qwen_asr, "ensure_aligner_model", lambda *_args: pytest.fail("aligner ran"))
    fake = _fake_cli(words=False)
    result = _transcribe(tmp_path, monkeypatch, settings={"stt_language": "auto"}, fake=fake, timed=False)
    command = fake.commands[0]
    assert "-am" not in command
    assert "--require-word-timestamps" not in command
    assert "--strict-pipeline" in command
    assert "-l" not in command
    assert json.loads(Path(result.word_timestamps_path).read_text())["transcription"][0]["words"] == []


def test_explicit_vad_load_failure_is_not_silent(tmp_path, monkeypatch):
    def fail_vad(command, **_kwargs):
        assert "--vad" in command
        assert "--strict-pipeline" in command
        raise subprocess.CalledProcessError(
            1, command, stderr=b"VAD model load failed: invalid local model\n"
        )

    with pytest.raises(QwenASRError, match="VAD model load failed"):
        _transcribe(
            tmp_path, monkeypatch,
            settings={"qwen_asr_chunk_mode": "vad"},
            fake=fail_vad, timed=False,
        )
    assert not (tmp_path / "result_words.json").exists()


@pytest.mark.parametrize("invalid", ["missing", "outside", "nonfinite", "reversed"])
def test_invalid_native_words_are_rejected_without_publishing(tmp_path, monkeypatch, invalid):
    with pytest.raises(QwenASRError):
        _transcribe(tmp_path, monkeypatch, fake=_fake_cli(invalid=invalid))
    assert not (tmp_path / "result_words.json").exists()


def test_zero_duration_native_word_is_preserved(tmp_path, monkeypatch):
    def run(command, **_kwargs):
        base = Path(command[command.index("-of") + 1])
        Path(f"{base}.srt").write_text("1\n00:00:00,100 --> 00:00:01,000\nHello for world.\n")
        Path(f"{base}.json").write_text(json.dumps({
            "crispasr": {"backend": "qwen3", "language": "en"},
            "transcription": [{
                "text": "Hello for world.", "offsets": {"from": 100, "to": 1000},
                "words": [
                    {"text": "Hello", "offsets": {"from": 100, "to": 400}},
                    {"text": "for", "offsets": {"from": 410, "to": 410}},
                    {"text": "world.", "offsets": {"from": 450, "to": 950}},
                ],
            }],
        }))
        return SimpleNamespace(stdout=b"", stderr=b"")

    result = _transcribe(tmp_path, monkeypatch, fake=run)
    raw = json.loads(Path(result.word_timestamps_path).read_text())
    assert raw["transcription"][0]["words"][1]["offsets"] == {"from": 410, "to": 410}


def test_missing_trailing_word_rejected_even_when_some_words_exist(tmp_path):
    audio = _wav(tmp_path / "audio.wav")
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"transcription": [{
        "text": "Hello world", "words": [{"text": "Hello", "offsets": {"from": 0, "to": 400}}],
    }]}))
    with pytest.raises(crispasr.CrispASRError, match="does not cover"):
        crispasr._validate_qwen_json(path, audio, True)


def test_cjk_punctuation_lexical_coverage(tmp_path):
    audio = _wav(tmp_path / "audio.wav")
    path = tmp_path / "chinese.json"
    path.write_text(json.dumps({"transcription": [{
        "text": "你好，世界！", "words": [
            {"text": "你好", "offsets": {"from": 10, "to": 300}},
            {"text": "世界", "offsets": {"from": 300, "to": 300}},
        ],
    }]}))
    crispasr._validate_qwen_json(path, audio, True)


def test_no_speech_is_clear_failure_without_output(tmp_path, monkeypatch):
    def silent_run(_command, **_kwargs):
        return SimpleNamespace(stdout=b"", stderr=b"VAD: no speech detected\n" + b"noise\n" * 500)

    with pytest.raises(QwenASRError, match="detected no speech"):
        _transcribe(tmp_path, monkeypatch, fake=silent_run)
    assert not (tmp_path / "result_words.json").exists()


def test_guard_uses_audio_cpp_identity_for_custom_server_port(monkeypatch):
    seen = []

    @contextmanager
    def guard(command, event):
        seen.append((command, event))
        yield

    monkeypatch.setattr(qwen_asr, "native_audio_cpp_guard", guard)
    monkeypatch.setattr(
        qwen_asr, "run_cancellable", lambda *_args, **_kwargs: SimpleNamespace(stderr=b"", stdout=b"")
    )
    qwen_asr._run_tool(
        ["/opt/crispasr/crispasr", "--backend", "qwen3"],
        run_func=subprocess.run, cancel_event=None, timeout=1,
        backend="vulkan", guard_executable="/opt/audio_cpp/audiocpp_cli",
    )
    assert seen[0][0] == ["/opt/audio_cpp/audiocpp_cli", "--backend", "vulkan"]


def test_canary_route_uses_existing_cache_or_explicit_download(tmp_path, monkeypatch):
    fake = _fake_cli()
    _transcribe(tmp_path, monkeypatch, settings={"stt_language": "pl", "crispasr_cache_dir": str(tmp_path)}, fake=fake)
    command = fake.commands[0]
    assert command[command.index("-am") + 1] == "canary-ctc-aligner"
    assert "--auto-download" in command
    assert command[command.index("-l") + 1] == "pl"
    cached = tmp_path / crispasr.DEFAULT_CTC_ALIGNER_ARTIFACT.filename
    cached.write_bytes(b"cached")
    fake2 = _fake_cli()
    _transcribe(tmp_path, monkeypatch, settings={"stt_language": "pl", "crispasr_cache_dir": str(tmp_path)}, fake=fake2)
    assert fake2.commands[0][fake2.commands[0].index("-am") + 1] == str(cached)
    assert "--auto-download" not in fake2.commands[0]


def test_cancel_and_timeout():
    event = threading.Event()
    event.set()
    with pytest.raises(ProcessCancelled):
        qwen_asr._run_tool(["sleep", "30"], run_func=subprocess.run, cancel_event=event, timeout=1)
    with pytest.raises(QwenASRError, match="timed out after 1 seconds"):
        qwen_asr._run_tool(["sleep", "30"], run_func=subprocess.run, cancel_event=None, timeout=1, backend="cpu")


def test_pinned_assets_verified_atomic_reuse_and_cancel(tmp_path, monkeypatch):
    content = b"verified GGUF"
    pin = crispasr_qwen_assets.PinnedAsset("example/model", "abc123", "model.gguf", len(content), hashlib.sha256(content).hexdigest())
    monkeypatch.setitem(crispasr_qwen_assets.ASSETS, "test", pin)

    class Response:
        def __init__(self, data):
            self.data = data
            self.used = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            if self.used:
                return b""
            self.used = True
            return self.data

    settings = {"crispasr_cache_dir": str(tmp_path)}
    calls = []

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        return Response(content)

    path = crispasr_qwen_assets.ensure_asset("test", settings, opener=opener)
    assert path.read_bytes() == content
    assert "/resolve/abc123/" in calls[0]
    assert crispasr_qwen_assets.ensure_asset("test", settings, opener=lambda *_a, **_kw: pytest.fail("network used")) == path
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="verification"):
        crispasr_qwen_assets.ensure_asset("test", settings, opener=lambda *_a, **_kw: Response(b"wrong"))
    assert not list(tmp_path.glob("*.part-*"))
    event = threading.Event()
    event.set()
    with pytest.raises(ProcessCancelled):
        crispasr_qwen_assets.ensure_asset("test", settings, cancel_event=event, opener=opener)


def test_orchestrator_preflight_blocks_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(transcription, "extract_audio", lambda *_a, **_kw: pytest.fail("extraction ran"))
    monkeypatch.setattr(qwen_asr, "ensure_asr_model", lambda *_a: pytest.fail("download ran"))
    with pytest.raises(QwenASRError, match="unsupported_qwen_timestamps"):
        transcription.transcribe_source_file_with_metadata(
            tmp_path / "session", tmp_path / "clip.mp4",
            {"stt_engine": "qwen3", "stt_language": "ar"},
        )
