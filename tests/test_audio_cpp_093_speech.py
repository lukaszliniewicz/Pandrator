"""Offline payload and segmentation contracts for audio.cpp 0.9.3."""

from functools import partial
from types import SimpleNamespace

import pytest

from pandrator.logic.audio_cpp_catalogue import package_metadata
from pandrator.logic.audio_cpp_parameters import (
    request_parameters_for_family,
    request_parameters_for_model,
    validate_model_options,
)
from pandrator.logic.audio_cpp_segmentation import (
    REVIEWED_RUNTIME,
    narration_character_policy,
    native_character_chunk_default,
    single_segment_options,
)
from pandrator.logic.audio_cpp_speech_payload import (
    _audio_cpp_language,
    _audio_cpp_selected_model_options,
    _build_audio_cpp_audio_payload,
)

XTTS = "xtts_v2_gguf_q8_0"
GRADIUM = "gradium_phonon_31m_en_q8_0"
MING_TTS = "ming_3_0_tts_0_5b_q8_0"
MING_AUDIO = "ming3_audio_tiny_q8_0"
MF_MODELS = ("irodori_tts_v4_1_small_mf_q8_0", "irodori_tts_v4_1_small_mf_f16")
WAV_REFERENCE = {"type": "base64", "data": "data:audio/wav;base64,UklGRg=="}


def _metadata(model, endpoint):
    return package_metadata(model)


def _payload(model, *, instructions="", text="Hello there.", **settings):
    def compile_request(text, settings, endpoint):
        return SimpleNamespace(
            input=text,
            instructions=instructions,
            request_options={},
            capabilities={"instruction_transport": "options.instruct"},
        )

    return _build_audio_cpp_audio_payload(
        text,
        {"model": model, **settings},
        {},
        model_metadata=_metadata,
        model_language=partial(_audio_cpp_language, model_metadata=_metadata),
        selected_model_options=partial(
            _audio_cpp_selected_model_options, validate_options=validate_model_options
        ),
        validate_options=validate_model_options,
        get_performance_compiler=lambda: compile_request,
    )


@pytest.mark.parametrize(
    "reference",
    [
        None,
        {},
        {"sample_id": "local"},
        {"type": "path", "path": "/tmp/ref.wav"},
        {"type": "base64", "data": "UklGRg=="},
    ],
)
def test_xtts_requires_managed_wav(reference):
    with pytest.raises(ValueError, match="XTTS v2 requires a linked WAV"):
        _payload(XTTS, audio_cpp_voice_ref=reference)


@pytest.mark.parametrize(
    ("language", "native"),
    [("zh", "zh-cn"), ("zh-cn", "zh-cn"), ("pt-BR", "pt")]
    + [
        (code, code)
        for code in (
            "en",
            "es",
            "fr",
            "de",
            "it",
            "pt",
            "pl",
            "tr",
            "ru",
            "nl",
            "cs",
            "ar",
            "hu",
            "ko",
            "ja",
            "hi",
        )
    ],
)
def test_xtts_language_and_waveform_only_reference(language, native):
    payload = _payload(
        XTTS,
        language=language,
        audio_cpp_voice_ref=WAV_REFERENCE,
        audio_cpp_reference_text="Ignored transcript.",
        audio_cpp_options={"reference_text": "Also ignored."},
        speed=1,
    )
    assert payload["language"] == native
    assert payload["voice_ref"] == WAV_REFERENCE
    assert "reference_text" not in payload
    assert "reference_text" not in payload["options"]
    assert "speed" not in payload
    assert "speed" not in payload["options"]


def test_gradium_prebuilt_default_does_not_send_clone_fields():
    payload = _payload(
        GRADIUM,
        speaker="default",
        audio_cpp_voice_ref=WAV_REFERENCE,
        audio_cpp_reference_text="Not consumed.",
        speed=1,
    )
    assert payload["voice"] == "default"
    assert "voice_ref" not in payload
    assert "reference_text" not in payload
    assert "speed" not in payload


@pytest.mark.parametrize("model", [XTTS, GRADIUM, MING_TTS, MING_AUDIO])
@pytest.mark.parametrize("rate", [0.9, True, "1"])
def test_new_families_reject_unsupported_shared_rate(model, rate):
    settings = {"audio_cpp_voice_ref": WAV_REFERENCE} if model == XTTS else {}
    with pytest.raises(ValueError, match="speed is unsupported"):
        _payload(model, speed=rate, **settings)


@pytest.mark.parametrize("model", [MING_TTS, MING_AUDIO])
def test_ming_reference_transcript_is_optional_but_needs_managed_audio(model):
    plain = _payload(model, speed=1)
    assert "voice_ref" not in plain
    assert "speed" not in plain
    with pytest.raises(ValueError, match="reference text requires linked reference audio"):
        _payload(model, audio_cpp_reference_text="Reference words.")
    with pytest.raises(ValueError, match="requires a linked WAV"):
        _payload(model, audio_cpp_voice_ref={"type": "path", "path": "/tmp/ref.wav"})
    clone = _payload(model, speaker="local-reference", audio_cpp_voice_ref=WAV_REFERENCE)
    assert clone["voice_ref"] == WAV_REFERENCE
    assert "reference_text" not in clone
    assert "voice" not in clone
    reviewed = _payload(
        model,
        audio_cpp_voice_ref=WAV_REFERENCE,
        audio_cpp_reference_text="Reference words.",
    )
    assert reviewed["reference_text"] == "Reference words."


@pytest.mark.parametrize(
    ("options", "voice", "instructions", "template"),
    [
        ({}, "", "", "tts"),
        ({"speed": "慢速", "pitch": "高", "volume": "中"}, "", "", "basic"),
        ({"emotion": "happy"}, "", "", "emotion"),
        ({"dialect": "cantonese"}, "", "", "dialect"),
        ({}, "US_Emma", "", "ip"),
        ({}, "", "A warm, resonant voice.", "voice_design"),
    ],
)
@pytest.mark.parametrize("selected", [False, True])
def test_ming_tts_builder_selects_conditioning_template(
    options, voice, instructions, template, selected
):
    settings = (
        {"audio_cpp_model_settings": {MING_TTS: options}}
        if selected
        else {"audio_cpp_options": options}
    )
    payload = _payload(MING_TTS, speaker=voice, instructions=instructions, **settings)
    assert payload["options"]["template_name"] == template
    if voice:
        assert payload["options"]["voice_id"] == voice
        assert "voice" not in payload
    if instructions:
        assert payload["options"]["instruct"] == instructions
        assert "instructions" not in payload


@pytest.mark.parametrize(
    ("options", "voice", "instructions"),
    [
        ({"speed": "快速", "emotion": "happy"}, "", ""),
        ({"pitch": "高"}, "US_Emma", ""),
        ({"dialect": "cantonese"}, "", "Warm voice."),
        ({}, "US_James", "Warm voice."),
        ({"emotion": "happy", "dialect": "cantonese"}, "", ""),
    ],
)
def test_ming_tts_rejects_competing_conditioning_modes(options, voice, instructions):
    with pytest.raises(ValueError, match="Ming3 TTS accepts one conditioning mode per request"):
        _payload(MING_TTS, audio_cpp_options=options, speaker=voice, instructions=instructions)


def test_ming_tts_linked_reference_removes_preset_and_validates_remaining_mode():
    payload = _payload(
        MING_TTS,
        speaker="US_Emma",
        audio_cpp_voice_ref=WAV_REFERENCE,
        audio_cpp_options={"emotion": "happy"},
    )
    assert payload["options"]["template_name"] == "emotion"
    assert "voice_id" not in payload["options"]
    assert "voice" not in payload
    with pytest.raises(ValueError, match="Ming3 TTS voice must be one of"):
        _payload(MING_TTS, speaker="unexpected")


def test_ming_tts_native_categorical_speed_and_legacy_neutral_rate_stay_distinct():
    descriptors = request_parameters_for_family("ming3_tts")
    assert descriptors["speed"]["enum"] == ["慢速", "中速", "快速"]
    assert "speaking_rate" not in descriptors
    assert _payload(MING_TTS, audio_cpp_options={"speed": 1})["options"]["template_name"] == "tts"
    with pytest.raises(ValueError, match="must be a string"):
        _payload(MING_TTS, audio_cpp_model_settings={MING_TTS: {"speed": 1}})
    with pytest.raises(ValueError, match="must be one of"):
        _payload(MING_TTS, audio_cpp_options={"speed": "fast"})


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (GRADIUM, "noise_file"),
        (GRADIUM, "voice_embedding_file"),
        (GRADIUM, "export_voice_state_file"),
        (MING_TTS, "template_name"),
        (MING_TTS, "instruct"),
        (MING_TTS, "voice_id"),
        (MING_TTS, "speaker_embedding_file"),
        (MING_TTS, "export_speaker_embedding_file"),
    ],
)
def test_reserved_transport_and_path_options_cannot_bypass_builder(model, field):
    assert field not in request_parameters_for_family(package_metadata(model)["family"])
    with pytest.raises(ValueError, match="Unknown audio.cpp option"):
        _payload(model, audio_cpp_options={field: "bypass"})


@pytest.mark.parametrize("field", ["system_prompt", "enable_thinking", "prompt", "max_tokens"])
def test_ming_audio_asr_only_controls_are_not_tts_parameters(field):
    assert field not in request_parameters_for_family("ming3_audio")
    with pytest.raises(ValueError, match="Unknown audio.cpp option"):
        _payload(MING_AUDIO, audio_cpp_options={field: 1})
    if field == "max_tokens":
        with pytest.raises(ValueError, match="Unknown audio.cpp option"):
            _payload(MING_AUDIO, audio_cpp_max_tokens=256)


def test_ming_audio_moves_language_to_talker_options_and_retains_audio_budget():
    payload = _payload(
        MING_AUDIO,
        language="zh",
        speaker="US_James",
        audio_cpp_options={"max_audio_tokens": 800},
    )
    assert "language" not in payload
    assert payload["options"]["language"] == "zh"
    assert payload["options"]["max_audio_tokens"] == 800
    assert payload["voice"] == "US_James"


@pytest.mark.parametrize("model", MF_MODELS)
def test_irodori_mf_default_steps_describe_model_without_injecting_them(model):
    assert request_parameters_for_model("irodori_tts", model)["num_inference_steps"]["default"] == 4
    plain = _payload(model)
    assert plain["options"]["no_ref"] is True
    assert "num_inference_steps" not in plain["options"]
    clone = _payload(model, audio_cpp_voice_ref=WAV_REFERENCE)
    assert clone["options"]["no_ref"] is False
    assert "num_inference_steps" not in clone["options"]


def test_irodori_rf_default_is_40_and_unreviewed_variants_are_not_assigned_defaults():
    assert (
        request_parameters_for_model("irodori_tts", "irodori_tts_v4_small_q8_0")[
            "num_inference_steps"
        ]["default"]
        == 40
    )
    assert (
        "default"
        not in request_parameters_for_model("irodori_tts", "irodori_tts_v4_1_anime_q8_0")[
            "num_inference_steps"
        ]
    )


@pytest.mark.parametrize(
    ("family", "latin", "cjk"),
    [
        ("gradium_phonon", 200, 200),
        ("ming3_tts", 300, 50),
        ("ming3_audio", 100, 50),
        ("xtts_v2", 256, 256),
    ],
)
def test_new_family_policies_and_whole_segment_native_chunking(family, latin, cjk):
    assert REVIEWED_RUNTIME == "0.9.3"
    assert narration_character_policy(family, "en")[0] == latin
    assert narration_character_policy(family, "zh")[0] == cjk
    options = single_segment_options(family, "a" * 800, {"text_chunk_size": 20})
    assert options["text_chunk_size"] == 800
    assert native_character_chunk_default("xtts_v2") == 256


def test_kitten_old_checkpoints_keep_their_own_voices_and_languages():
    from pandrator.logic.preset_voice_metadata import preset_voice_info
    from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_PRESETS

    old = 'kitten_tts2_q8_v2'
    new = 'kitten_tts2_20261021_q8_0'
    assert len(AUDIO_CPP_MODEL_PRESETS[old]) == 48
    assert len(AUDIO_CPP_MODEL_PRESETS[new]) == 124
    assert 'pl' not in package_metadata(old)['supported_languages']
    assert 'pl' in package_metadata(new)['supported_languages']
    assert 'PolishNewsAnchor' not in AUDIO_CPP_MODEL_PRESETS[old]
    assert 'PolishNewsAnchor' in AUDIO_CPP_MODEL_PRESETS[new]
    assert preset_voice_info('kitten_tts2', new, 'PolishNewsAnchor')['language_code'] == 'pl'


def test_score_conditioned_singing_is_not_a_narrator_choice():
    from pandrator.logic.audio_cpp_catalogue import speech_model_catalog
    from pandrator_manager.audio_cpp_inventory import load_audio_cpp_packages

    model = 'voxcpm2_vocalrender_pro'
    metadata = package_metadata(model)
    assert metadata['voice_mode'] == 'none'
    assert metadata['package_availability']['status'] == 'catalogued_only'
    assert metadata['pandrator_features']['speech_generation'] == 'catalogued_only'
    assert model not in {row['id'] for row in speech_model_catalog()}
    assert model not in {row.id for row in load_audio_cpp_packages()}


@pytest.mark.parametrize(('language', 'native'), [('en', 240), ('fr', 240), ('zh', 100), ('ja', 100)])
def test_kokoro_keeps_language_specific_narration_targets(language, native):
    from pandrator.logic.audiobook_chunking import audiobook_chunk_budget

    assert narration_character_policy('kokoro_tts', language)[0] == native
    budget = audiobook_chunk_budget({}, {'service': 'audio_cpp', 'model': 'kokoro_82m_q8_0'}, language=language)
    assert budget['target_chars'] == native * 9 // 10


@pytest.mark.parametrize(('family', 'latin', 'cjk'), [
    ('fireredtts3', 600, 80), ('magpie_tts', 300, 100), ('moss_tts_nano', 256, 50),
])
def test_refreshed_native_defaults_keep_latin_targets_and_cjk_headroom(family, latin, cjk):
    assert narration_character_policy(family, 'en')[0] == latin
    assert narration_character_policy(family, 'zh')[0] == cjk


def test_known_family_component_does_not_inherit_speech_controls():
    from pandrator.logic.speech_capabilities import capabilities_for_model

    profile = capabilities_for_model('qwen3_tts_0_6b_base_speech_decoder_rk3588', backend='audio_cpp', family='qwen3_tts', voice_mode='none')
    assert profile['instructions'] == 'none'
    assert profile['notes'] == []
