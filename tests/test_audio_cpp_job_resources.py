"""Native audio jobs must queue against managed TTS even with auto compute."""

import pytest

from pandrator.web.stt_resources import stt_resource_keys


@pytest.mark.parametrize("settings", [
    {"stt_engine": "qwen3"},
    {"stt_backend": "qwen3-asr"},
    {"stt_engine": "qwen3", "qwen_asr_backend": "vulkan", "stt_compute_backend": "cpu"},
    {"stt_engine": "whisper", "transcription_vocal_isolation": "bs_roformer"},
    {"stt_engine": "qwen3", "stt_compute_backend": "cpu", "transcription_vocal_isolation": "mel_band_roformer", "audio_cpp_backend": "vulkan"},
    {"caption_alignment_ctc_model": "qwen3-forced-aligner"},
    {"caption_alignment_ctc_model": "auto", "original_language": "ja"},
])
def test_native_gpu_or_auto_queues_with_speech(settings):
    keys = stt_resource_keys(settings)
    assert "service:tts:audio_cpp" in keys
    assert "gpu:default" in keys


def test_cpu_native_still_serializes_without_claiming_gpu():
    keys = stt_resource_keys({"stt_engine": "qwen3", "stt_compute_backend": "cpu"})
    assert "service:tts:audio_cpp" in keys
    assert not any(key.startswith("gpu:") for key in keys)


def test_unrelated_plain_transcription_retains_resources():
    assert stt_resource_keys({}) == ["service:stt"]
    assert stt_resource_keys({"compute_backend": "cuda"}) == ["service:stt", "gpu:cuda"]
