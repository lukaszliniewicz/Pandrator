import pytest

from pandrator.logic.audiobook_chunking import audiobook_chunk_budget
from pandrator.logic.tts_generation_limits import generation_limit_metadata

_CAP_KEYS = {"input_characters", "input_tokens", "output_tokens", "output_seconds"}


@pytest.mark.parametrize(
    ("service", "model", "family", "adapter", "caps"),
    [
        ("openai", "gpt-4o-mini-tts", "", "", {"input_characters": 4096, "input_tokens": 2000}),
        ("openai", "gpt-4o-mini-tts-2025-12-15", "", "", {"input_characters": 4096, "input_tokens": 2000}),
        ("openai", "tts-1", "", "", {"input_characters": 4096}),
        ("gemini", "gemini-3.8-flash-tts", "", "", {"input_tokens": 8192, "output_tokens": 16384}),
        ("vertex_ai", "gemini-2.5-flash-tts", "", "", {"input_tokens": 8192, "output_tokens": 16384}),
        ("azure", "MAI-Voice-2", "", "azure_speech", {"output_seconds": 600}),
        ("audio_cpp", "qwen3_tts_1_7b_base_q8_0", "qwen3_tts", "audio_cpp", {}),
        ("audio_cpp", "voxcpm2_q8_0", "voxcpm2", "audio_cpp", {}),
        ("custom", "unknown", "", "", {}),
        ("gemini", "gemini-future-custom", "", "", {}),
    ],
)
def test_metadata_has_only_evidenced_caps_and_shared_default_policy(service, model, family, adapter, caps):
    metadata = generation_limit_metadata(service=service, model=model, family=family, adapter=adapter)
    assert {key: metadata[key] for key in _CAP_KEYS if key in metadata} == caps
    policy = audiobook_chunk_budget({}, {"service": service, "model": model, "adapter": adapter})
    assert metadata["default_segment_characters"] == policy["target_chars"]
    assert metadata["policy_max_segment_characters"] == 8192
    assert metadata["checked_at"] == "2026-10-07"
    if caps:
        assert metadata["source_urls"]


@pytest.mark.parametrize(
    ("model", "family", "chunk", "output"),
    [
        ("qwen3_tts_1_7b_base_q8_0", "qwen3_tts", 8192, {"value": 2048, "unit": "codec_frames"}),
        ("breeze_tts_2_q8_0", "breeze_tts", 600, {"value": 1500, "unit": "acoustic_frames"}),
        ("voxcpm2_q8_0", "voxcpm2", 2048, None),
    ],
)
def test_runtime_defaults_are_separate_from_model_caps(model, family, chunk, output):
    metadata = generation_limit_metadata(service="audio.cpp", model=model, family=family)
    assert metadata["runtime_chunk_characters"] == chunk
    assert metadata.get("runtime_output_budget") == output
    assert not (_CAP_KEYS & metadata.keys())
    assert "/v0.9.0/" in metadata["source_urls"][0]


def test_explicit_family_selects_shared_policy_for_unfamiliar_package_id():
    metadata = generation_limit_metadata(service="audio_cpp", model="custom-package", family="breeze_tts")
    assert metadata["default_segment_characters"] == 540
    assert metadata["runtime_output_budget"] == {"value": 1500, "unit": "acoustic_frames"}


def test_kobold_does_not_claim_audio_cpp_runtime_defaults():
    metadata = generation_limit_metadata(service="kobold_qwen", model="Prebuilt Voices", family="qwen3_tts")
    assert metadata["default_segment_characters"] == 1800
    assert "runtime_output_budget" not in metadata
    assert "runtime_chunk_characters" not in metadata


def test_each_metadata_result_owns_nested_values():
    arguments = {"service": "audio_cpp", "model": "breeze_tts_2_q8_0", "family": "breeze_tts"}
    first = generation_limit_metadata(**arguments)
    first["runtime_output_budget"]["value"] = 1
    first["source_urls"].append("https://caller.invalid")
    second = generation_limit_metadata(**arguments)
    assert second["runtime_output_budget"] == {"value": 1500, "unit": "acoustic_frames"}
    assert "https://caller.invalid" not in second["source_urls"]
