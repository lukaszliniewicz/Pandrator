import pytest

from pandrator.logic.audio_cpp_segmentation import narration_character_policy
from pandrator.logic.audiobook_chunking import (
    audiobook_chunk_budget,
    validate_audiobook_chunking_settings,
)
from pandrator.logic.narration_packing import (
    DEFAULT_SENTENCE_PACKING,
    DEFAULT_SENTENCE_PACKING_THRESHOLD,
    preserve_sentence_boundaries,
)


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


@pytest.mark.parametrize(
    "model", ["qwen3_tts_1_7b_base_q8_0", "voxcpm2_q8_0", "breeze_tts_2_q8_0"]
)
def test_manual_budget_has_no_runtime_chunk_default_cap(model):
    budget = audiobook_chunk_budget(
        {"audiobook_chunking": "manual", "max_sentence_length": 8192},
        {"service": "audio_cpp", "model": model},
    )
    assert budget["target_chars"] == 8192
    assert budget["input_limit_chars"] is None


def test_manual_budget_clamps_to_openai_character_cap():
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
    assert default["target_chars"] == 350
    assert default["policy_limit_chars"] == 350

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
    assert lower["target_chars"] == 175

    scalar_wins = audiobook_chunk_budget(
        {"audiobook_chunking": "model"},
        {
            "service": "audio_cpp",
            "model": "qwen3_tts_1_7b_base_q8_0",
            "audio_cpp_options": {"max_tokens": 1024},
            "audio_cpp_max_tokens": 512,
        },
    )
    assert scalar_wins["target_chars"] == 87


@pytest.mark.parametrize(
    "settings",
    [
        {"service": "audio_cpp", "model": "qwen3_tts_1_7b_customvoice_q8_0"},
        {"service": "qwen3_tts", "model": "qwen3-tts"},
        {"service": "kobold_qwen", "model": "qwen3-tts"},
        {"service": "qwen", "model": "qwen3-tts"},
    ],
)
def test_qwen_aliases_use_the_same_automatic_350_character_target(settings):
    budget = audiobook_chunk_budget({}, settings, "ja")
    assert budget["profile"] == "qwen3_tts"
    assert budget["policy_limit_chars"] == 350
    assert budget["target_chars"] == 350


def test_qwen_narration_policy_is_language_independent():
    assert narration_character_policy("qwen3_tts")[0] == 350
    assert narration_character_policy("qwen3_tts", "ja")[0] == 350


@pytest.mark.parametrize("language", ["en", "pl", "ja", "zh-Hant", "ko"])
def test_voxcpm2_uses_direct_350_character_target(language):
    budget = audiobook_chunk_budget(
        {"audiobook_chunking": "model"},
        {"service": "audio_cpp", "model": "voxcpm2_q8_0"},
        language,
    )
    assert budget["target_chars"] == budget["policy_limit_chars"] == 350
    assert narration_character_policy("voxcpm1", "en")[0] == 2000
    assert narration_character_policy("voxcpm1", "ja")[0] == 500


@pytest.mark.parametrize("language,expected", [("en", 540), ("zh-Hant", 270), ("ja", 270), ("ko", 270)])
def test_drama_uses_narration_policy_instead_of_native_split_default(language, expected):
    model = "fish_drama3_small_iq3_xxs"
    settings = {"service": "audio_cpp", "model": model}
    assert audiobook_chunk_budget({}, settings, language)["target_chars"] == expected
    settings["audio_cpp_model_settings"] = {model: {"max_tokens": 512}}
    reduced = audiobook_chunk_budget({}, settings, language)
    assert reduced["target_chars"] == expected // 2
    assert "scaled to 512 configured max_tokens" in reduced["reason"]
    manual = audiobook_chunk_budget(
        {"audiobook_chunking": "manual", "max_sentence_length": 800}, settings, language
    )
    assert manual["target_chars"] == 800


def test_drama_respects_explicit_chunk_size_and_authoritative_model_options():
    model = "fish_drama3_small_iq3_xxs"
    settings = {
        "service": "audio_cpp", "model": model,
        "audio_cpp_options": {"text_chunk_size": 128, "max_tokens": 256},
        "audio_cpp_max_tokens": 512,
    }
    assert audiobook_chunk_budget({}, settings)["target_chars"] == 57
    settings["audio_cpp_model_settings"] = {model: {}}
    assert audiobook_chunk_budget({}, settings)["target_chars"] == 540


@pytest.mark.parametrize(
    ("language", "expected", "policy"),
    [
        ("en", 350, 350),
        ("zh-Hant", 350, 350),
        ("ja", 350, 350),
        ("ko-KR", 350, 350),
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
        ({"service": "VoxCPM", "model": "openbmb/VoxCPM2"}, "voxcpm2", 350, 350, None),
        ({"service": "FishS2", "model": "fishaudio/s2-pro"}, "fish_audio_s2", 300, 270, None),
        ({"service": "Kokoro", "model": "kokoro"}, "kokoro", 240, 216, None),
        ({"service": "OpenAI", "model": "tts-1"}, "openai", 4000, 3600, 4096),
        ({"service": "Google Gemini", "model": "gemini-2.5-flash-tts"}, "gemini", 4000, 3600, None),
        ({"service": "audio_cpp", "model": "breeze_tts_2_q8_0"}, "breeze_tts", 500, 450, None),
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


@pytest.mark.parametrize(
    ("model", "default_tokens", "target"),
    [("qwen3_tts_1_7b_base_q8_0", 2048, 350), ("breeze_tts_2_q8_0", 1500, 450)],
)
@pytest.mark.parametrize("source", ["model_map", "scalar", "audio_cpp_options", "options"])
def test_output_budget_scaling_uses_each_runtime_option_source(model, default_tokens, target, source):
    settings = {"service": "audio_cpp", "model": model}
    lower_tokens = default_tokens // 2
    if source == "model_map":
        settings.update({
            "audio_cpp_model_settings": {model: {"max_tokens": lower_tokens}},
            "audio_cpp_max_tokens": 1,
            "audio_cpp_options": {"max_tokens": 1},
        })
    elif source == "scalar":
        settings.update({
            "audio_cpp_max_tokens": lower_tokens,
            "audio_cpp_options": {"max_tokens": 1},
        })
    else:
        settings[source] = {"max_tokens": lower_tokens}
    budget = audiobook_chunk_budget({}, settings)
    assert budget["target_chars"] == target // 2
    assert budget["input_limit_chars"] is None


@pytest.mark.parametrize(
    ("model", "default_tokens", "target"),
    [("qwen3_tts_1_7b_base_q8_0", 2048, 350), ("breeze_tts_2_q8_0", 1500, 450)],
)
def test_output_budget_does_not_expand_policy_or_borrow_scalar_from_empty_map(model, default_tokens, target):
    base = {"service": "audio_cpp", "model": model}
    higher = audiobook_chunk_budget({}, {**base, "audio_cpp_max_tokens": default_tokens * 2})
    assert higher["target_chars"] == target
    empty = audiobook_chunk_budget({}, {
        **base, "audio_cpp_model_settings": {model: {}}, "audio_cpp_max_tokens": 1,
    })
    assert empty["target_chars"] == target


@pytest.mark.parametrize("model", ["gpt-4o-mini-tts", "gpt-4o-mini-tts-2025-12-15"])
@pytest.mark.parametrize(("language", "target", "policy"), [("en", 3600, 4000), ("ja", 900, 1000), ("zh-Hant", 900, 1000), ("ko-KR", 900, 1000)])
def test_mini_tts_dated_ids_and_cjk_are_application_policies(model, language, target, policy):
    budget = audiobook_chunk_budget({}, {"service": "OpenAI", "model": model}, language)
    assert budget["target_chars"] == target
    assert budget["policy_limit_chars"] == policy
    assert budget["input_limit_chars"] == 4096


@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd"])
def test_legacy_openai_cjk_policy_is_unchanged(model):
    assert audiobook_chunk_budget({}, {"service": "OpenAI", "model": model}, "ja")["target_chars"] == 3600


@pytest.mark.parametrize("value", ["bad", 0, -1, 8193, True])
def test_invalid_chunking_values_are_rejected(value):
    with pytest.raises(ValueError):
        audiobook_chunk_budget(
            {"audiobook_chunking": "manual", "max_sentence_length": value}, {}
        )


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        audiobook_chunk_budget({"audiobook_chunking": "auto"}, {})


@pytest.mark.parametrize(
    ("target_chars", "threshold", "expected"),
    [(299, 300, False), (300, 300, True), (350, 350, True), (351, 350, True)],
)
def test_auto_sentence_packing_uses_inclusive_effective_target_threshold(
    target_chars, threshold, expected
):
    assert preserve_sentence_boundaries(
        {"audiobook_sentence_packing_threshold": threshold}, target_chars
    ) is expected


@pytest.mark.parametrize(
    ("mode", "target_chars", "expected"),
    [
        ("sentences", 20, True),
        ("sentences", 8192, True),
        ("clauses", 20, False),
        ("clauses", 8192, False),
    ],
)
def test_forced_sentence_packing_modes_ignore_target_and_threshold(
    mode, target_chars, expected
):
    assert preserve_sentence_boundaries(
        {"audiobook_sentence_packing": mode, "audiobook_sentence_packing_threshold": 1},
        target_chars,
    ) is expected


def test_sentence_packing_settings_default_and_validate_strictly():
    assert DEFAULT_SENTENCE_PACKING == "auto"
    assert DEFAULT_SENTENCE_PACKING_THRESHOLD == 300
    validate_audiobook_chunking_settings({})
    validate_audiobook_chunking_settings(
        {"audiobook_sentence_packing_threshold": "350"}
    )

    invalid_packing = (None, "", "Auto", "split", 1)
    for value in invalid_packing:
        with pytest.raises(
            ValueError,
            match="^audiobook_sentence_packing must be 'auto', 'sentences' or 'clauses'.$",
        ):
            validate_audiobook_chunking_settings({"audiobook_sentence_packing": value})

    invalid_thresholds = (None, True, False, 0, 8193, 300.0, 300.5, "300.0", "bad")
    for value in invalid_thresholds:
        with pytest.raises(
            ValueError,
            match="^audiobook_sentence_packing_threshold must be an integer between 1 and 8192.$",
        ):
            validate_audiobook_chunking_settings(
                {"audiobook_sentence_packing_threshold": value}
            )
