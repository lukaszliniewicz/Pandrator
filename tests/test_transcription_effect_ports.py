"""Synthetic hook order/default-runner coverage; no real native or provider calls."""

from __future__ import annotations

import json
import subprocess
import threading
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing import (
    cloud_stt,
    crispasr,
    qwen_alignment,
    qwen_asr,
    stt_routing,
    transcription,
)


@pytest.fixture
def media(tmp_path: Path):
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * 3200)
    return path


def _result(tmp_path: Path):
    words = tmp_path / "fake_words.json"
    srt = tmp_path / "fake.srt"
    words.write_text(json.dumps({"transcription": [], "language": "en"}), encoding="utf-8")
    srt.write_text("", encoding="utf-8")
    return crispasr.CrispASRTranscriptionResult(str(srt), str(words), "whisper", "cpu")


def _wrapper(monkeypatch, result, *, cloud=False):
    engine = "azure_mai_transcribe_1_5" if cloud else "whisper"
    route = SimpleNamespace(
        engine=engine, resolved_language="en", as_dict=lambda: {"engine": engine}
    )
    monkeypatch.setattr(stt_routing, "resolve_stt_route", Mock(return_value=route))
    monkeypatch.setattr(
        transcription,
        "apply_vocal_isolation",
        Mock(side_effect=lambda audio, *_args, **_kwargs: (audio, None)),
    )
    monkeypatch.setattr(
        transcription, "compose_from_transcript_json", Mock(return_value="subtitles")
    )
    monkeypatch.setattr(transcription, "postprocess_transcribed_srt", lambda path: path)
    native = Mock(return_value=result)
    monkeypatch.setattr(cloud_stt if cloud else transcription, "transcribe", native)
    return engine, native


def test_wrapper_default_omits_hook_and_preserves_real_runner(media, tmp_path, monkeypatch):
    engine, native = _wrapper(monkeypatch, _result(tmp_path))
    event = threading.Event()
    transcription.transcribe_source_file_with_metadata(
        tmp_path,
        media,
        {"stt_engine": engine, "stt_language": "en"},
        source_is_normalized=True,
        cancel_event=event,
    )
    assert "before_io" not in native.call_args.kwargs
    assert native.call_args.kwargs["run_func"] is subprocess.run
    assert native.call_args.kwargs["cancel_event"] is event


def test_wrapper_guard_failure_precedes_source_or_output_io(media, tmp_path, monkeypatch):
    error = ValueError("guard sentinel")
    forbidden = Mock(side_effect=AssertionError("IO occurred"))
    monkeypatch.setattr(Path, "mkdir", forbidden)
    with pytest.raises(ValueError) as caught:
        transcription.transcribe_source_file_with_metadata(
            tmp_path, media, {}, before_io=Mock(side_effect=error)
        )
    assert caught.value is error
    forbidden.assert_not_called()


def test_language_failure_cannot_swallow_latched_guard(media, tmp_path, monkeypatch):
    result = _result(tmp_path)
    engine, native = _wrapper(monkeypatch, result)
    denied = [False]
    error = ValueError("guard sentinel")

    def guard():
        if denied[0]:
            raise error

    def detect(*args, **kwargs):
        denied[0] = True
        raise RuntimeError("ordinary detector error")

    with pytest.raises(ValueError) as caught:
        transcription.transcribe_source_file_with_metadata(
            tmp_path,
            media,
            {"stt_engine": "auto", "stt_language": "auto"},
            source_is_normalized=True,
            before_io=guard,
            language_detector=detect,
        )
    assert caught.value is error
    native.assert_not_called()


def test_wrapper_rechecks_after_progress_before_composing(media, tmp_path, monkeypatch):
    result = _result(tmp_path)
    engine, native = _wrapper(monkeypatch, result)
    compose = Mock(side_effect=AssertionError("composition after denial"))
    monkeypatch.setattr(transcription, "compose_from_transcript_json", compose)
    denied = False
    error = PermissionError("guard sentinel")

    def guard():
        if denied:
            raise error

    def progress(value, _detail=None):
        nonlocal denied
        if value == 0.82:
            denied = True

    with pytest.raises(PermissionError) as caught:
        transcription.transcribe_source_file_with_metadata(
            tmp_path,
            media,
            {"stt_engine": engine, "stt_language": "en"},
            source_is_normalized=True,
            before_io=guard,
            progress_callback=progress,
        )
    assert caught.value is error
    native.assert_called_once()
    compose.assert_not_called()
    assert Path(result.srt_path).read_text(encoding="utf-8") == ""


def _crisp(monkeypatch, tmp_path):
    monkeypatch.setattr(crispasr, "_prefetch_windows_model", Mock(return_value=None))
    monkeypatch.setattr(crispasr, "_prefetch_windows_vad_model", Mock(return_value=None))
    monkeypatch.setattr(crispasr, "_prefetch_windows_moss_aligner", Mock(return_value=None))
    monkeypatch.setattr(crispasr, "build_command", Mock(return_value=["fake-crispasr"]))
    monkeypatch.setattr(crispasr, "_validate_word_timestamps", Mock())

    def run(*args, **kwargs):
        (tmp_path / "out_crispasr.srt").write_text("", encoding="utf-8")
        (tmp_path / "out_crispasr.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(stdout=b"", stderr=b"")

    native = Mock(side_effect=run)
    monkeypatch.setattr(crispasr, "_run_tool", native)
    return native


def test_crisp_default_runner_and_none_kwargs_unchanged(media, tmp_path, monkeypatch):
    native = _crisp(monkeypatch, tmp_path)
    event = threading.Event()
    result = crispasr.transcribe(
        media,
        session_dir=tmp_path,
        output_name="out",
        settings={"stt_engine": "whisper", "stt_language": "en"},
        cancel_event=event,
    )
    assert Path(result.word_timestamps_path).exists()
    assert native.call_args.kwargs["run_func"] is subprocess.run
    assert native.call_args.kwargs["cancel_event"] is event
    assert "before_io" not in native.call_args.kwargs


def test_crisp_rechecks_after_acquisition_before_launch(monkeypatch):
    denied = [False]
    error = ValueError("guard sentinel")

    def guard():
        if denied[0]:
            raise error

    def acquire(*args):
        denied[0] = True

    monkeypatch.setattr(crispasr, "_require_native_acquisition_if_needed", acquire)
    native = Mock(side_effect=AssertionError("native launch"))
    monkeypatch.setattr(crispasr, "run_cancellable", native)
    with pytest.raises(ValueError) as caught:
        crispasr._run_tool(
            ["fake"], run_func=subprocess.run, cancel_event=threading.Event(), before_io=guard
        )
    assert caught.value is error
    native.assert_not_called()


def test_crisp_prefetch_guard_does_not_become_downloader_fallback(media, tmp_path, monkeypatch):
    native = _crisp(monkeypatch, tmp_path)
    denied = [False]
    error = ValueError("guard sentinel")

    def guard():
        if denied[0]:
            raise error

    def prefetch(*args):
        denied[0] = True

    monkeypatch.setattr(crispasr, "_prefetch_windows_model", prefetch)
    with pytest.raises(ValueError) as caught:
        crispasr.transcribe(
            media,
            session_dir=tmp_path,
            output_name="out",
            settings={"stt_engine": "whisper", "stt_language": "en"},
            before_io=guard,
        )
    assert caught.value is error
    native.assert_not_called()


def _qwen(monkeypatch):
    monkeypatch.setattr(
        qwen_asr,
        "validate_transcription_settings",
        Mock(
            return_value={
                "isolation": "off",
                "language": "en",
                "backend": "cpu",
                "chunk_mode": "none",
                "chunk_seconds": 30,
                "timeout": 42,
                "model_id": "fake",
            }
        ),
    )
    monkeypatch.setattr(qwen_asr, "resolve_executable", lambda *_args: Path("fake-crisp"))
    monkeypatch.setattr(
        qwen_asr, "resolve_audio_cpp_executable", lambda *_args: Path("fake-audio.cpp")
    )
    version = Mock()
    monkeypatch.setattr(qwen_asr, "_verify_qwen_runtime", version)
    model = Mock(return_value=Path("fake-model"))
    monkeypatch.setattr(qwen_asr, "ensure_asr_model", model)
    return version, model


def test_qwen_preserves_version_setup_default_runner_event_and_bounded_options(
    media, tmp_path, monkeypatch
):
    version, model = _qwen(monkeypatch)
    event = threading.Event()
    guard = Mock()
    result = _result(tmp_path)
    native = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(qwen_asr, "_run_tool", native)

    def crisp(*args, **kwargs):
        assert kwargs["before_io"] is guard
        assert kwargs["cancel_event"] is event
        kwargs["run_func"](["fake-crisp", "--test"])
        return result

    monkeypatch.setattr(crispasr, "transcribe", crisp)
    qwen_asr.transcribe(
        media,
        session_dir=tmp_path,
        output_name="out",
        settings={},
        cancel_event=event,
        require_word_timestamps=False,
        before_io=guard,
    )
    version.assert_called_once()
    assert model.call_args.args[-1] is event
    assert native.call_args.kwargs["run_func"] is subprocess.run
    assert native.call_args.kwargs["cancel_event"] is event
    assert native.call_args.kwargs["timeout"] == 42
    assert native.call_args.kwargs["backend"] == "cpu"


@pytest.mark.parametrize("mode", ["success", "cancel", "timeout"])
def test_qwen_real_default_native_coordination_and_watchdog_remain(mode, monkeypatch):
    event = threading.Event()
    entered = []

    @contextmanager
    def coordination(command, combined):
        entered.append((command, combined))
        yield

    monkeypatch.setattr(qwen_asr, "native_audio_cpp_guard", coordination)
    if mode == "cancel":
        event.set()
    if mode == "timeout":
        values = iter([0.0, 100.0])
        monkeypatch.setattr(
            qwen_asr, "time", SimpleNamespace(monotonic=lambda: next(values, 100.0))
        )

    def native(command, *, cancel_event, **kwargs):
        assert entered and entered[-1][1] is cancel_event
        if mode == "timeout":
            assert cancel_event.wait(1)
            raise ProcessCancelled("synthetic timeout")
        return "done"

    command = Mock(side_effect=native)
    monkeypatch.setattr(qwen_asr, "run_cancellable", command)
    if mode == "cancel":
        with pytest.raises(ProcessCancelled):
            qwen_asr._run_tool(
                ["fake"],
                run_func=subprocess.run,
                cancel_event=event,
                timeout=1,
                guard_executable="fake-audio.cpp",
            )
        command.assert_not_called()
    elif mode == "timeout":
        with pytest.raises(qwen_asr.QwenASRError, match="timed out"):
            qwen_asr._run_tool(
                ["fake"],
                run_func=subprocess.run,
                cancel_event=event,
                timeout=1,
                guard_executable="fake-audio.cpp",
            )
    else:
        assert (
            qwen_asr._run_tool(
                ["fake"],
                run_func=subprocess.run,
                cancel_event=event,
                timeout=1,
                guard_executable="fake-audio.cpp",
            )
            == "done"
        )
        assert entered[0][0] == ["fake-audio.cpp", "--backend", "auto"]


def test_qwen_rechecks_after_native_coordination_before_launch(monkeypatch):
    denied = False
    error = PermissionError("guard sentinel")

    def guard():
        if denied:
            raise error

    @contextmanager
    def coordination(_command, _event):
        nonlocal denied
        denied = True
        yield

    native = Mock(side_effect=AssertionError("native launch after denial"))
    monkeypatch.setattr(qwen_asr, "native_audio_cpp_guard", coordination)
    monkeypatch.setattr(qwen_asr, "run_cancellable", native)
    with pytest.raises(PermissionError) as caught:
        qwen_asr._run_tool(
            ["fake"],
            run_func=subprocess.run,
            cancel_event=threading.Event(),
            timeout=1,
            guard_executable="fake-audio.cpp",
            before_io=guard,
        )
    assert caught.value is error
    native.assert_not_called()


def test_cloud_default_submission_preserves_transport_kwargs(media, monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {})
    post = Mock(return_value=response)
    parsed = object()
    monkeypatch.setattr(cloud_stt, "parse_azure_response", Mock(return_value=parsed))
    assert (
        cloud_stt._submit_azure(
            media,
            url="https://synthetic",
            headers={"auth": "synthetic"},
            definition={"test": True},
            timeout=12,
            profile={},
            request_func=post,
            session=None,
        )
        is parsed
    )
    assert post.call_args.args == ("https://synthetic",)
    assert set(post.call_args.kwargs) == {"headers", "files", "timeout"}
    assert post.call_args.kwargs["timeout"] == 12
    assert post.call_args.kwargs["files"]["audio"][0] == media.name


def test_cloud_guard_stops_later_chunks_and_output_after_first_post(media, tmp_path, monkeypatch):
    denied = [False]
    error = ValueError("guard sentinel")

    def guard():
        if denied[0]:
            raise error

    chunks = [
        cloud_stt.CloudSTTChunk(0, 0, 1600, 0, 100),
        cloud_stt.CloudSTTChunk(1, 1600, 3200, 100, 200),
    ]
    monkeypatch.setattr(cloud_stt, "plan_cloud_stt_chunks", Mock(return_value=chunks))

    def post(*args, **kwargs):
        denied[0] = True
        return SimpleNamespace(status_code=200, json=lambda: {})

    request = Mock(side_effect=post)
    with pytest.raises(ValueError) as caught:
        cloud_stt.transcribe(
            media,
            session_dir=tmp_path,
            output_name="guarded",
            settings={
                "stt_engine": "azure_mai_transcribe_1_5",
                "stt_api_base": "https://synthetic.cognitiveservices.azure.com",
                "stt_api_key": "synthetic",
            },
            request_func=request,
            before_io=guard,
        )
    assert caught.value is error and request.call_count == 1
    assert not (tmp_path / "guarded_words.json").exists()


def test_cloud_hook_error_before_post_is_original_not_request_wrapper(media):
    error = ValueError("guard sentinel")
    count = [0]

    def guard():
        count[0] += 1
        if count[0] == 2:
            raise error

    post = Mock(side_effect=AssertionError("post after denial"))
    with pytest.raises(ValueError) as caught:
        cloud_stt._submit_azure(
            media,
            url="synthetic",
            headers={},
            definition={},
            timeout=1,
            profile={},
            request_func=post,
            session=None,
            before_io=guard,
        )
    assert caught.value is error
    post.assert_not_called()


def test_two_batch_moss_chain_cannot_launch_second_group_after_latched_loss(media, monkeypatch):
    denied = [False]
    error = ValueError("guard sentinel")

    def guard():
        if denied[0]:
            raise error

    monkeypatch.setattr(qwen_alignment, "ensure_model", Mock(return_value=Path("fake-model")))
    monkeypatch.setattr(
        qwen_alignment, "resolve_executable", Mock(return_value=Path("fake-audio.cpp"))
    )

    def native(*args, **kwargs):
        denied[0] = True

    command = Mock(side_effect=native)
    payload = {
        "transcription": [{"text": "hello", "offsets": {"from": 0, "to": 100}} for _ in range(9)]
    }
    with pytest.raises(ValueError) as caught:
        qwen_alignment.align_moss_turns(
            media, payload, {"original_language": "en"}, run_func=command, before_io=guard
        )
    assert caught.value is error and command.call_count == 1
    assert all("words" not in row for row in payload["transcription"])


def test_wrapper_supplies_cloud_hook_identity_without_native_runner_change(
    media, tmp_path, monkeypatch
):
    engine, cloud = _wrapper(monkeypatch, _result(tmp_path), cloud=True)
    guard = Mock()
    transport = Mock()
    transcription.transcribe_source_file_with_metadata(
        tmp_path,
        media,
        {"stt_engine": engine, "stt_language": "en"},
        source_is_normalized=True,
        before_io=guard,
        cloud_request_func=transport,
    )
    assert cloud.call_args.kwargs["before_io"] is guard
    assert cloud.call_args.kwargs["request_func"] is transport
    assert "run_func" not in cloud.call_args.kwargs


def test_qwen_guard_precedes_runtime_probe_and_model_acquisition(media, tmp_path, monkeypatch):
    version, model = _qwen(monkeypatch)
    error = ValueError("guard sentinel")
    with pytest.raises(ValueError) as caught:
        qwen_asr.transcribe(
            media,
            session_dir=tmp_path,
            output_name="out",
            settings={},
            before_io=Mock(side_effect=error),
        )
    assert caught.value is error
    version.assert_not_called()
    model.assert_not_called()
