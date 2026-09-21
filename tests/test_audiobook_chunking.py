import pytest

from pandrator.logic.audiobook_chunking import audiobook_chunk_budget


def test_default_model_budget_uses_unknown_provider_fallback():
    budget = audiobook_chunk_budget({"audiobook_chunking": "model"}, {})
    assert budget["mode"] == "model"
    assert budget["profile"] == "unknown"
    assert budget["policy_limit_chars"] == 300
    assert budget["target_chars"] == 300


def test_manual_budget_respects_length_without_policy_clamp():
    budget = audiobook_chunk_budget(
        {"audiobook_chunking": "manual", "max_sentence_length": 5000},
        {"service": "XTTS", "model": "xtts_v2"},
    )
    assert budget["mode"] == "manual"
    assert budget["target_chars"] == 5000
    assert budget["policy_limit_chars"] == 200

    legacy = audiobook_chunk_budget({"max_sentence_length": 500}, {})
    assert legacy["mode"] == "manual"
    assert legacy["target_chars"] == 500


def test_manual_budget_clamps_only_to_verified_provider_input_limit():
    budget = audiobook_chunk_budget(
        {"audiobook_chunking": "manual", "max_sentence_length": 5000},
        {"service": "audio_cpp", "model": "qwen3_tts_1_7b_base_q8_0"},
    )
    assert budget["target_chars"] == 5000
    assert budget["input_limit_chars"] == 8192

    openai = audiobook_chunk_budget(
        {"audiobook_chunking": "manual", "max_sentence_length": 5000},
        {"service": "OpenAI", "model": "gpt-4o-mini-tts"},
    )
    assert openai["target_chars"] == 4096
    assert openai["input_limit_chars"] == 4096


def test_audio_cpp_qwen_default_and_lower_max_tokens_scale_policy():
    default = audiobook_chunk_budget(
        {"audiobook_chunking": "model"},
        {"service": "audio_cpp", "model": "qwen3_tts_1_7b_base_q8_0"},
    )
    assert default["target_chars"] == 1800
    assert default["policy_limit_chars"] == 2000

    lower = audiobook_chunk_budget(
        {"audiobook_chunking": "model"},
        {
            "service": "audio_cpp",
            "model": "qwen3_tts_1_7b_base_q8_0",
            "audio_cpp_model_settings": {
                "qwen3_tts_1_7b_base_q8_0": {"max_tokens": 1024}
            },
        },
    )
    assert lower["target_chars"] == 900

    scalar_wins = audiobook_chunk_budget(
        {"audiobook_chunking": "model"},
        {
            "service": "audio_cpp",
            "model": "qwen3_tts_1_7b_base_q8_0",
            "audio_cpp_options": {"max_tokens": 1024},
            "audio_cpp_max_tokens": 512,
        },
    )
    assert scalar_wins["target_chars"] == 450


@pytest.mark.parametrize(
    ("language", "expected", "policy"),
    [
        ("en", 1800, 2000),
        ("zh-Hant", 450, 500),
        ("ja", 450, 500),
        ("ko-KR", 450, 500),
    ],
)
def test_qwen_cjk_budget(language, expected, policy):
    budget = audiobook_chunk_budget(
        {"audiobook_chunking": "model"},
        {"service": "audio_cpp", "model": "qwen3_tts_1_7b_base_q8_0"},
        language,
    )
    assert budget["target_chars"] == expected
    assert budget["policy_limit_chars"] == policy


@pytest.mark.parametrize(
    ("tts", "profile", "policy", "target", "input_limit"),
    [
        ({"service": "VoxCPM", "model": "openbmb/VoxCPM2"}, "voxcpm2", 2000, 1800, 2048),
        ({"service": "FishS2", "model": "fishaudio/s2-pro"}, "fish_audio_s2", 200, 180, None),
        ({"service": "Kokoro", "model": "kokoro"}, "kokoro", 240, 216, None),
        ({"service": "OpenAI", "model": "tts-1"}, "openai", 4000, 3600, 4096),
        ({"service": "Google Gemini", "model": "gemini-2.5-flash-tts"}, "gemini", 2000, 1800, None),
        ({"service": "azure", "adapter": "azure_speech", "model": "MAI-Voice-2"}, "azure_speech", 4000, 3600, None),
        ({"service": "XTTS", "model": "tts_models/multilingual/multi-dataset/xtts_v2"}, "xtts", 200, 200, None),
    ],
)
def test_provider_policy_profiles(tts, profile, policy, target, input_limit):
    budget = audiobook_chunk_budget({"audiobook_chunking": "model"}, tts)
    assert budget["profile"] == profile
    assert budget["policy_limit_chars"] == policy
    assert budget["target_chars"] == target
    assert budget["input_limit_chars"] == input_limit


@pytest.mark.parametrize("value", ["bad", 0, -1, 8193, True])
def test_invalid_chunking_values_are_rejected(value):
    with pytest.raises(ValueError):
        audiobook_chunk_budget(
            {"audiobook_chunking": "manual", "max_sentence_length": value}, {}
        )


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        audiobook_chunk_budget({"audiobook_chunking": "auto"}, {})
