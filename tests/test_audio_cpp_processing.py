"""No-network tests for native vocal isolation and voice-sample cleanup.

Synthetic WAVs only; the audiocpp_cli child is always a fake run_func that
writes stem/denoised WAVs. No acoustic quality is claimed: assertions cover
command shape, byte preservation, timeline validation, and failure modes.
"""

import subprocess
import threading
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest

from pandrator.logic import audio_cpp_processing as proc
from pandrator.logic.cancellable_process import ProcessCancelled


def make_wav(path: Path, seconds: float = 1.0, rate: int = 44100, channels: int = 2):
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * frames * channels)
    return path


def wav_frames(path: Path) -> int:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes()


@pytest.fixture
def native(tmp_path, monkeypatch):
    """Fake ensure_model/resolve_executable; real logic, no child processes."""
    models = {}

    def fake_ensure(model_id, settings, cancel_event=None, progress=None):
        assert model_id in set(proc.ISOLATION_MODEL.values()) | {"deepfilternet2"}
        path = tmp_path / f"{model_id}.bin"
        path.write_bytes(b"model")
        models[model_id] = path
        return path

    monkeypatch.setattr(proc.assets, "ensure_model", fake_ensure)
    monkeypatch.setattr(
        proc.assets, "resolve_executable", lambda settings: tmp_path / "audiocpp_cli"
    )
    return models


def test_off_mode_skips_without_side_effects(tmp_path, native, monkeypatch):
    ensure = Mock(side_effect=AssertionError("must not fetch models"))
    monkeypatch.setattr(proc.assets, "ensure_model", ensure)
    source = make_wav(tmp_path / "song.wav")
    before = source.read_bytes()
    destination = tmp_path / "vocals.wav"
    result = proc.isolate_vocals(
        source, destination, {"transcription_vocal_isolation": "off"}
    )
    assert result["status"] == "skipped"
    assert not destination.exists()
    assert source.read_bytes() == before
    ensure.assert_not_called()


def test_unknown_isolation_mode_is_rejected(tmp_path):
    with pytest.raises(proc.AudioProcessingError, match="transcription_vocal"):
        proc.isolate_vocals(
            tmp_path / "a.wav",
            tmp_path / "b.wav",
            {
                "transcription_vocal_isolation": "htdemucs",
            },
        )


@pytest.mark.parametrize(
    ("mode", "model_id"),
    [("bs_roformer", "bs_roformer"), ("mel_band_roformer", "mel_band_roformer")],
)
def test_isolation_command_shape_and_valid_output(tmp_path, native, mode, model_id):
    source = make_wav(tmp_path / "song.wav", seconds=1.0)
    before = source.read_bytes()
    destination = tmp_path / "vocals.wav"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert command[1:6] == ["--task", "sep", "--family", model_id, "--model"]
        assert "--out-dir" in command
        out_dir = Path(command[command.index("--out-dir") + 1])
        assert Path(command[command.index("--audio") + 1]).resolve() == source.resolve()
        with wave.open(str(source), "rb") as src:
            params, frames = src.getparams(), src.getnframes()
        with wave.open(str(out_dir / "vocals.wav"), "wb") as out:
            out.setparams(params)
            out.writeframes(b"\1\0" * frames * params.nchannels)
        return subprocess.CompletedProcess(command, 0)

    stages = []
    result = proc.isolate_vocals(
        source,
        destination,
        {"transcription_vocal_isolation": mode},
        progress=lambda stage, done, total: stages.append(stage),
        run_func=runner,
    )
    assert len(calls) == 1  # already-normalized input needs no ffmpeg
    assert result["status"] == "isolated"
    assert result["model"] == model_id
    assert result["source_duration_s"] == pytest.approx(1.0)
    assert result["output_duration_s"] == pytest.approx(1.0)
    assert result["output_sample_rate_hz"] == 44100
    assert wav_frames(destination) == wav_frames(source)
    assert source.read_bytes() == before  # original bytes preserved
    assert [s for s in stages if s in {"normalize", "isolate", "install"}] == [
        "normalize",
        "isolate",
        "isolate",
        "install",
    ]


def test_isolation_normalizes_non_conforming_input(tmp_path, native):
    source = make_wav(tmp_path / "speech.wav", seconds=0.5, rate=16000, channels=1)
    destination = tmp_path / "vocals.wav"
    seen = {}

    def runner(command, **kwargs):
        if "ffmpeg" in command[0]:
            seen["ffmpeg"] = command
            rate = int(command[command.index("-ar") + 1])
            with wave.open(str(source), "rb") as src:
                duration = src.getnframes() / src.getframerate()
            with wave.open(command[-1], "wb") as out:
                out.setnchannels(2)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(b"\0\0" * int(duration * rate) * 2)
        else:
            out_dir = Path(command[command.index("--out-dir") + 1])
            audio = Path(command[command.index("--audio") + 1])
            seen["audio"] = audio
            with wave.open(str(audio), "rb") as src:
                params, frames = src.getparams(), src.getnframes()
            with wave.open(str(out_dir / "vocals.wav"), "wb") as out:
                out.setparams(params)
                out.writeframes(b"\2\0" * frames * params.nchannels)
        return subprocess.CompletedProcess(command, 0)

    result = proc.isolate_vocals(
        source,
        destination,
        {"transcription_vocal_isolation": "bs_roformer"},
        run_func=runner,
    )
    assert "ffmpeg" in seen
    assert seen["audio"].resolve() != source.resolve()
    assert result["status"] == "isolated"
    assert result["output_sample_rate_hz"] == 44100


def test_small_padding_within_sample_window_is_accepted(tmp_path, native):
    source = make_wav(tmp_path / "song.wav", seconds=2.0)
    destination = tmp_path / "vocals.wav"

    def runner(command, **kwargs):
        out_dir = Path(command[command.index("--out-dir") + 1])
        with wave.open(str(source), "rb") as src:
            params, frames = src.getparams(), src.getnframes()
        with wave.open(str(out_dir / "vocals.wav"), "wb") as out:
            out.setparams(params)
            out.writeframes(b"\1\0" * (frames + 100) * params.nchannels)
        return subprocess.CompletedProcess(command, 0)

    result = proc.isolate_vocals(
        source,
        destination,
        {"transcription_vocal_isolation": "bs_roformer"},
        run_func=runner,
    )
    assert result["status"] == "isolated"


def test_duration_drift_beyond_50ms_is_rejected(tmp_path, native):
    source = make_wav(tmp_path / "song.wav", seconds=2.0)
    destination = tmp_path / "vocals.wav"

    def runner(command, **kwargs):
        out_dir = Path(command[command.index("--out-dir") + 1])
        with wave.open(str(source), "rb") as src:
            params, frames = src.getparams(), src.getnframes()
        with wave.open(str(out_dir / "vocals.wav"), "wb") as out:
            out.setparams(params)
            out.writeframes(b"\1\0" * (frames + 10000) * params.nchannels)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(proc.AudioProcessingError, match="[Dd]rift|timeline"):
        proc.isolate_vocals(
            source,
            destination,
            {"transcription_vocal_isolation": "bs_roformer"},
            run_func=runner,
        )
    assert not destination.exists()


def test_missing_or_empty_model_output_is_rejected(tmp_path, native):
    source = make_wav(tmp_path / "song.wav")
    destination = tmp_path / "vocals.wav"

    def silent_runner(command, **kwargs):
        Path(command[command.index("--out-dir") + 1]).mkdir(exist_ok=True)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(proc.AudioProcessingError, match="no output"):
        proc.isolate_vocals(
            source,
            destination,
            {"transcription_vocal_isolation": "bs_roformer"},
            run_func=silent_runner,
        )

    def empty_runner(command, **kwargs):
        out_dir = Path(command[command.index("--out-dir") + 1])
        (out_dir / "vocals.wav").write_bytes(b"")
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(proc.AudioProcessingError, match="[Ee]mpty|readable|WAV"):
        proc.isolate_vocals(
            source,
            destination,
            {"transcription_vocal_isolation": "bs_roformer"},
            run_func=empty_runner,
        )
    assert not destination.exists()


def test_child_failure_and_cancellation_leave_no_partial_output(tmp_path, native):
    source = make_wav(tmp_path / "song.wav")
    destination = tmp_path / "vocals.wav"

    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(proc.AudioProcessingError, match="failed"):
        proc.isolate_vocals(
            source,
            destination,
            {"transcription_vocal_isolation": "bs_roformer"},
            run_func=failing,
        )
    assert not destination.exists()

    event = threading.Event()
    event.set()
    with pytest.raises(ProcessCancelled):
        proc.isolate_vocals(
            source,
            destination,
            {"transcription_vocal_isolation": "bs_roformer"},
            cancel_event=event,
            run_func=Mock(side_effect=AssertionError("must not run")),
        )
    assert not destination.exists()


def test_invalid_backend_and_threads_are_rejected(tmp_path, native):
    source = make_wav(tmp_path / "song.wav")
    with pytest.raises(proc.AudioProcessingError, match="[Bb]ackend"):
        proc.isolate_vocals(
            source,
            tmp_path / "o.wav",
            {
                "transcription_vocal_isolation": "bs_roformer",
                "audio_cpp_backend": "tpu",
            },
            run_func=Mock(),
        )
    with pytest.raises(proc.AudioProcessingError, match="integer"):
        proc.isolate_vocals(
            source,
            tmp_path / "o.wav",
            {
                "transcription_vocal_isolation": "bs_roformer",
                "audio_cpp_threads": "many",
            },
            run_func=Mock(),
        )


def test_clean_voice_sample_uses_dfn2_route(tmp_path, native):
    source = make_wav(tmp_path / "mic.wav", seconds=1.0, rate=48000, channels=1)
    before = source.read_bytes()
    destination = tmp_path / "clean.wav"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert command[1:6] == [
            "--task",
            "s2s",
            "--family",
            "builtin_audio_utils",
            "--model",
        ]
        assert "--load-option" in command
        assert command[command.index("--load-option") + 1] == "utility=deepfilternet2"
        assert Path(command[command.index("--audio") + 1]).resolve() == source.resolve()
        target = Path(command[command.index("--out") + 1])
        with wave.open(str(source), "rb") as src:
            params, frames = src.getparams(), src.getnframes()
        with wave.open(str(target), "wb") as out:
            out.setparams(params)
            out.writeframes(b"\3\0" * frames)
        return subprocess.CompletedProcess(command, 0)

    stages = []
    result = proc.clean_voice_sample(
        source,
        destination,
        {},
        progress=lambda stage, done, total: stages.append(stage),
        run_func=runner,
    )
    assert len(calls) == 1
    assert result["status"] == "cleaned"
    assert result["utility"] == "deepfilternet2"
    assert result["source_duration_s"] == pytest.approx(1.0)
    assert result["output_duration_s"] == pytest.approx(1.0)
    assert result["output_sample_rate_hz"] == 48000
    assert wav_frames(destination) == 48000
    assert source.read_bytes() == before
    assert [s for s in stages if s in {"normalize", "clean", "install"}] == [
        "normalize",
        "clean",
        "clean",
        "install",
    ]


def test_watchdog_timeout_is_explicit(tmp_path):
    with pytest.raises(proc.AudioProcessingError, match="[Tt]imed out"):
        proc._run_child(
            ["sleep", "30"],
            {"audio_cpp_timeout_seconds": 0.2},
            None,
            None,
            purpose="watchdog probe",
        )


def test_timeout_never_sets_caller_event():
    event = threading.Event()
    with pytest.raises(proc.AudioProcessingError, match="[Tt]imed out"):
        proc._run_child(
            ["sleep", "30"],
            {"audio_cpp_timeout_seconds": 0.2},
            event,
            None,
            purpose="watchdog probe",
        )
    assert not event.is_set()


def test_mid_run_cancellation_propagates_without_timeout_error():
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    try:
        with pytest.raises(ProcessCancelled):
            proc._run_child(
                ["sleep", "30"],
                {"audio_cpp_timeout_seconds": 30},
                event,
                None,
                purpose="cancel probe",
            )
    finally:
        timer.cancel()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), "nan", "inf"])
def test_nonfinite_timeout_is_rejected_before_running(tmp_path, native, timeout):
    source = make_wav(tmp_path / "song.wav")
    runner = Mock(side_effect=AssertionError("must not run"))
    with pytest.raises(proc.AudioProcessingError, match="[Ff]inite"):
        proc.isolate_vocals(
            source,
            tmp_path / "vocals.wav",
            {
                "transcription_vocal_isolation": "bs_roformer",
                "audio_cpp_timeout_seconds": timeout,
            },
            run_func=runner,
        )
    runner.assert_not_called()


def test_same_source_and_destination_are_rejected(tmp_path, native, monkeypatch):
    ensure = Mock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(proc.assets, "ensure_model", ensure)
    runner = Mock(side_effect=AssertionError("must not run"))
    source = make_wav(tmp_path / "song.wav")
    with pytest.raises(proc.AudioProcessingError, match="[Ss]ame file"):
        proc.isolate_vocals(
            source,
            source,
            {"transcription_vocal_isolation": "bs_roformer"},
            run_func=runner,
        )
    link = tmp_path / "alias.wav"
    link.symlink_to(source)
    with pytest.raises(proc.AudioProcessingError, match="[Ss]ame file"):
        proc.clean_voice_sample(source, link, {}, run_func=runner)
    ensure.assert_not_called()
    runner.assert_not_called()
    assert source.stat().st_size > 0  # original untouched


def test_truncated_wav_data_is_rejected(tmp_path):
    path = make_wav(tmp_path / "cut.wav", seconds=1.0)
    raw = path.read_bytes()
    path.write_bytes(raw[:-100])
    with pytest.raises(proc.AudioProcessingError, match="[Tt]runcated"):
        proc._wav_info(path)


def test_truncated_source_never_reaches_native_child(tmp_path, native):
    source = make_wav(tmp_path / "cut.wav", seconds=0.5)
    raw = source.read_bytes()
    source.write_bytes(raw[:-100])
    runner = Mock(side_effect=AssertionError("must not run"))
    with pytest.raises(proc.AudioProcessingError, match="[Tt]runcated|WAV"):
        proc.isolate_vocals(
            source,
            tmp_path / "vocals.wav",
            {"transcription_vocal_isolation": "bs_roformer"},
            run_func=runner,
        )
    runner.assert_not_called()


def test_atomic_install_stages_inside_destination_parent(tmp_path, monkeypatch):
    import os as os_module

    staged = tmp_path / "work" / "vocals-out.wav"
    staged.parent.mkdir()
    staged.write_bytes(b"audio-bytes")
    destination = tmp_path / "takes" / "vocals.wav"
    seen = {}
    real_replace = os_module.replace

    def recorder(src, dst):
        seen["src"], seen["dst"] = src, dst
        real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", recorder)
    proc._atomic_install(staged, destination)
    assert destination.read_bytes() == b"audio-bytes"
    assert Path(seen["dst"]) == destination
    assert Path(seen["src"]).parent == destination.parent
    assert not list(destination.parent.glob(".pandrator-install-*"))


def test_long_separation_budget_preserves_explicit_limits():
    # A 23-minute recording took >40 minutes on an older GPU; it must not
    # inherit the same one-hour limit as a short clip by default.
    assert proc._timeout_seconds({}, duration_seconds=1380) > 2 * 3600
    assert proc._timeout_seconds({}, duration_seconds=12) == 3600
    assert proc._timeout_seconds({"audio_cpp_timeout_seconds": 120}, duration_seconds=1380) == 120
    assert proc._timeout_seconds({}, duration_seconds=86400) == 86400
    with pytest.raises(proc.AudioProcessingError):
        proc._timeout_seconds({"audio_cpp_timeout_seconds": float("nan")}, duration_seconds=1380)
