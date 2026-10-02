"""Pure routing and bounded detector tests; no model acquisition or inference."""

import json
import subprocess
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing import crispasr, transcription
from pandrator.logic.dubbing import stt_language_detection as detection
from pandrator.logic.dubbing.stt_backends import CrispASRRuntimeStatus, normalize_stt_backend
from pandrator.logic.dubbing.stt_routing import STTRoutingError, resolve_stt_route
from pandrator.web.stt_resources import stt_resource_keys

READY = {key: {"installed": True, "version": "0.8.40", "cached": False} for key in ("parakeet", "qwen3", "whisper")}
RUNTIME = CrispASRRuntimeStatus(True, "fake-crispasr", "0.8.40", ("cpu",), "ready")


@pytest.mark.parametrize("language,engine,alignment", [
    ("en", "parakeet", "native"), ("pl", "parakeet", "native"),
    ("ja", "qwen3", "qwen3_forced_aligner"), ("zh", "qwen3", "qwen3_forced_aligner"),
    ("ar", "whisper", "dtw"), ("th", "whisper", "dtw"),
])
def test_timed_auto_language_order(language, engine, alignment):
    route = resolve_stt_route({"stt_engine": "auto", "stt_language": language}, runtime_statuses=READY)
    assert (route.engine, route.alignment_route) == (engine, alignment)
    assert route.require_word_timestamps


@pytest.mark.parametrize("language", ["ar", "th"])
def test_untimed_qwen_has_full_recognizer_coverage(language):
    route = resolve_stt_route({"stt_engine": "auto", "stt_language": language}, runtime_statuses=READY, require_word_timestamps=False)
    assert (route.engine, route.alignment_route) == ("qwen3", "none")


@pytest.mark.parametrize("qwen", [None, {"installed": False}, {"installed": True, "version": "0.8.35"}])
def test_missing_or_old_qwen_falls_to_whisper(qwen):
    statuses = {**READY, "qwen3": qwen}
    route = resolve_stt_route({"stt_engine": "auto", "stt_language": "ja"}, runtime_statuses=statuses)
    assert route.engine == "whisper"
    assert "qwen_runtime_unavailable" in route.fallback_reason


@pytest.mark.parametrize("confidence,reason", [(0.69, "low_confidence"), (None, "invalid_confidence"), (float("nan"), "invalid_confidence")])
def test_detection_confidence_is_conservative(confidence, reason):
    route = resolve_stt_route({"stt_engine": "auto"}, resolved_language="en", language_source="detected", confidence=confidence, runtime_statuses=READY)
    assert (route.engine, route.resolved_language) == ("whisper", "auto")
    assert reason in route.fallback_reason


def test_detector_failure_and_unknown_language_do_not_need_tiny_for_fallback():
    route = resolve_stt_route({"stt_engine": "auto"}, detection_reason="tiny_unavailable", runtime_statuses=READY)
    assert route.engine == "whisper"
    route = resolve_stt_route({"stt_engine": "auto", "stt_language": "unknown"}, runtime_statuses=READY)
    assert route.resolved_language == "auto"
    with pytest.raises(STTRoutingError, match="explicit source language"):
        resolve_stt_route({"stt_engine": "auto"}, runtime_statuses={})


@pytest.mark.parametrize("engine", ["whisper", "parakeet", "qwen3", "moss", "azure_mai_transcribe_2"])
def test_explicit_choice_ignores_other_engines_availability(engine):
    route = resolve_stt_route({"stt_engine": engine, "stt_language": "en"}, runtime_statuses={})
    assert route.engine == engine
    assert route.fallback_reason == ""


def test_regional_tag_preserved_in_route_and_copy_is_detached():
    settings = {"stt_engine": "automatic", "stt_language": "pt-BR"}
    route = resolve_stt_route(settings, runtime_statuses=READY)
    settings["stt_language"] = "ja"
    metadata = route.as_dict()
    metadata["runtime_requirements"].append("changed")
    assert route.requested_language == "pt-BR"
    assert route.resolved_language == "pt"
    assert "changed" not in route.runtime_requirements
    assert normalize_stt_backend("parakeet_preferred") == "auto"
    assert normalize_stt_backend(None) == "whisper"


@pytest.mark.parametrize("output", [
    "", "whisper_full_with_state: auto-detected language: xx (p = 0.99)",
    "whisper_full_with_state: auto-detected language: en (p = nan)",
    "whisper_full_with_state: auto-detected language: en (p = 1.01)",
    "whisper_full_with_state: auto-detected language: en (p = 0.91)\nwhisper_full_with_state: auto-detected language: pl (p = 0.95)",
    "whisper_full_with_state: auto-detected language: en (p = 0.91)\nwhisper_full_with_state: auto-detected language: en (p = 0.91)",
])
def test_detector_rejects_missing_unknown_invalid_and_duplicate_reports(output):
    with pytest.raises(detection.LanguageDetectionError):
        detection.parse_language_detection(output)


def write_wave(path, seconds):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * int(seconds * 16000))


@pytest.mark.parametrize("seconds,expected_ms", [(1, 1000), (3.5, 3500), (25, 15000)])
def test_detector_sample_cpu_flags_watchdog_and_cleanup(tmp_path, monkeypatch, seconds, expected_ms):
    audio = tmp_path / "source.wav"
    write_wave(audio, seconds)
    seen = {}
    def excerpt(_audio, scratch, _name, start, end, **_kwargs):
        seen.update(scratch=scratch, start=start, end=end)
        return str(Path(scratch) / "sample.wav")
    def run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(stdout=b"", stderr=b"whisper_full_with_state: auto-detected language: pl (p = 0.920000)\n")
    monkeypatch.setattr(transcription, "extract_audio_excerpt", excerpt)
    monkeypatch.setattr(detection.crispasr_qwen_assets, "ensure_asset", lambda *a, **k: tmp_path / "ggml-tiny.bin")
    from pandrator.logic.dubbing import qwen_asr
    monkeypatch.setattr(qwen_asr, "_run_tool", run)
    result = detection.detect_source_language(audio, settings={}, excerpt_func=excerpt, runtime_status=RUNTIME)
    assert result == detection.LanguageDetectionResult("pl", 0.92)
    assert (seen["start"], seen["end"]) == (0, expected_ms)
    assert "--detect-language" in seen["command"]
    assert "--no-gpu" in seen["command"]
    assert "-of" not in seen["command"]
    assert seen["kwargs"]["timeout"] == 120
    assert seen["kwargs"]["backend"] == "cpu"
    assert not Path(seen["scratch"]).exists()


def test_detector_old_runtime_does_not_acquire_asset(tmp_path, monkeypatch):
    acquire = Mock(side_effect=AssertionError("download must not run"))
    monkeypatch.setattr(detection.crispasr_qwen_assets, "ensure_asset", acquire)
    with pytest.raises(detection.LanguageDetectionError, match="0.8.40"):
        detection.detect_source_language(tmp_path / "audio.wav", settings={}, excerpt_func=Mock(), runtime_status=CrispASRRuntimeStatus(True, "fake", "0.8.36", (), "ready"))
    acquire.assert_not_called()


@pytest.mark.parametrize("during", [False, True])
def test_detector_cancellation_never_becomes_fallback(tmp_path, monkeypatch, during):
    audio = tmp_path / "source.wav"
    write_wave(audio, 1)
    event = threading.Event()
    seen = {}
    if not during:
        event.set()
    def excerpt(_audio, scratch, *_args, **_kwargs):
        seen["scratch"] = scratch
        event.set()
        return str(Path(scratch) / "sample.wav")
    monkeypatch.setattr(transcription, "extract_audio_excerpt", excerpt)
    with pytest.raises(ProcessCancelled):
        detection.detect_source_language(audio, settings={}, excerpt_func=excerpt, runtime_status=RUNTIME, cancel_event=event)
    assert "scratch" not in seen or not Path(seen["scratch"]).exists()


def test_auto_resources_claim_native_and_gpu_unless_cpu():
    assert {"service:tts:audio_cpp", "gpu:default"} <= set(stt_resource_keys({"stt_engine": "auto"}))
    assert stt_resource_keys({"stt_engine": "auto", "stt_compute_backend": "cpu"}) == ["service:stt", "service:tts:audio_cpp"]


def fake_transcript(tmp_path):
    words = tmp_path / "words.json"
    words.write_text(json.dumps({"crispasr": {"language": "en"}, "transcription": [{"text": "Hello", "offsets": {"from": 0, "to": 1000}, "words": [{"text": "Hello", "offsets": {"from": 0, "to": 1000}}]}]}))
    return crispasr.CrispASRTranscriptionResult(str(tmp_path / "result.srt"), str(words), "parakeet", "cpu")


def test_shared_freezes_metadata_before_composition_and_uses_resolved_language(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_wave(source, 1)
    stub = fake_transcript(tmp_path)
    dispatch = Mock(return_value=stub)
    monkeypatch.setattr(transcription, "transcribe", dispatch)
    def compose(path, settings):
        payload = json.loads(Path(path).read_text())
        assert payload["metadata"]["stt_routing"]["engine"] == "parakeet"
        assert payload["language"] == settings["stt_language"] == "pl"
        return "1\n00:00:00,000 --> 00:00:01,000\nCześć\n"
    monkeypatch.setattr(transcription, "compose_from_transcript_json", compose)
    result = transcription.transcribe_source_file_with_metadata(tmp_path, source, {"stt_engine": "auto"}, source_is_normalized=True, runtime_statuses=READY, language_detector=lambda *a, **k: detection.LanguageDetectionResult("pl", 0.95))
    assert result.resolved_language == "pl"
    assert result.routing["language_source"] == "detected"
    assert dispatch.call_args.kwargs["settings"]["stt_engine"] == "parakeet"


def test_recognition_failure_does_not_trigger_fallback(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    write_wave(source, 1)
    dispatch = Mock(side_effect=subprocess.CalledProcessError(1, "fake"))
    monkeypatch.setattr(transcription, "transcribe", dispatch)
    with pytest.raises(subprocess.CalledProcessError):
        transcription.transcribe_source_file_with_metadata(tmp_path, source, {"stt_engine": "auto", "stt_language": "en"}, source_is_normalized=True, runtime_statuses=READY)
    dispatch.assert_called_once()


def test_tiny_asset_is_concretely_pinned():
    asset = detection.crispasr_qwen_assets.ASSETS[detection.DETECTOR_ASSET_KEY]
    assert (asset.repository, asset.revision, asset.filename, asset.size, asset.sha256) == (
        "ggerganov/whisper.cpp", "5359861c739e955e79d9a303bcbc70fb988958b1", "ggml-tiny.bin", 77691713,
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    )


@pytest.mark.parametrize("language", ["fil", "fil-PH"])
def test_untimed_filipino_uses_qwen_even_though_whisper_uses_different_code(language):
    route = resolve_stt_route({"stt_engine": "auto", "stt_language": language}, runtime_statuses=READY, require_word_timestamps=False)
    assert route.engine == "qwen3"
    assert route.resolved_language == "fil"
    assert route.requested_language == language


def test_explicit_unknown_language_remains_error():
    with pytest.raises(ValueError, match="Unsupported language"):
        resolve_stt_route({"stt_engine": "parakeet", "stt_language": "not-a-language"})


def test_explicit_qwen_timed_unsupported_fails_before_normalization(tmp_path, monkeypatch):
    normalize = Mock(side_effect=AssertionError("costly extraction must not start"))
    monkeypatch.setattr(transcription, "extract_audio", normalize)
    with pytest.raises(RuntimeError, match="unsupported_qwen_timestamps"):
        transcription.transcribe_source_file_with_metadata(tmp_path, tmp_path / "source.mp4", {"stt_engine": "qwen3", "stt_language": "ar"})
    normalize.assert_not_called()


def test_shared_untimed_qwen_passes_false_and_detected_language(tmp_path, monkeypatch):
    from pandrator.logic.dubbing import qwen_asr
    source = tmp_path / "source.wav"
    write_wave(source, 1)
    dispatch = Mock(return_value=fake_transcript(tmp_path))
    monkeypatch.setattr(qwen_asr, "transcribe", dispatch)
    result = transcription.transcribe_source_file_with_metadata(tmp_path, source, {"stt_engine": "qwen3", "stt_language": "auto"}, source_is_normalized=True, require_word_timestamps=False, language_detector=lambda *a, **k: detection.LanguageDetectionResult("ar", 0.99))
    assert dispatch.call_args.kwargs["require_word_timestamps"] is False
    assert dispatch.call_args.kwargs["settings"]["stt_language"] == "ar"
    assert result.resolved_language == "ar"


@pytest.mark.parametrize("during", [False, True])
def test_shared_cancellation_prevents_detection_or_dispatch(tmp_path, monkeypatch, during):
    source = tmp_path / "source.wav"
    write_wave(source, 1)
    event = threading.Event()
    if not during:
        event.set()
    def detector(*_args, **_kwargs):
        event.set()
        raise ValueError("detector stopped after cancellation")
    dispatch = Mock(side_effect=AssertionError("ASR must not run"))
    monkeypatch.setattr(transcription, "transcribe", dispatch)
    with pytest.raises(ProcessCancelled):
        transcription.transcribe_source_file_with_metadata(tmp_path, source, {"stt_engine": "auto"}, source_is_normalized=True, runtime_statuses=READY, cancel_event=event, language_detector=detector)
    dispatch.assert_not_called()
