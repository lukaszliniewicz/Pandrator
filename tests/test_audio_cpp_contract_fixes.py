import re
from types import SimpleNamespace

import pytest

from pandrator.logic import (
    audio_cpp_parameters,
    speech_performance,
    tts_handler,
    tts_provider_profiles,
)
from pandrator.logic.audio_cpp_parameters import (
    FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT,
    request_parameters_for_family,
    request_parameters_for_model,
    validate_model_options,
)


def _payload(model: str, **settings: object) -> dict:
    return tts_handler._build_audio_cpp_audio_payload(
        "The quick brown fox.", {"model": model, **settings}, {}
    )


def _stub_compiler(monkeypatch, *, request_options=None):
    monkeypatch.setattr(
        speech_performance,
        "compile_for_provider",
        lambda text, settings, endpoint: SimpleNamespace(
            input=f"compiled:{text}",
            instructions="",
            capabilities={},
            request_options=dict(request_options or {}),
        ),
    )


def _auk_payload(model: str, **settings: object) -> dict:
    settings.setdefault("generation_prompt", "Warm and clear.")
    return _payload(model, **settings)


@pytest.mark.parametrize("family", ("moss_ttsd", "vibevoice"))
def test_server_managed_voice_samples_are_not_scalar_tts_options(family):
    assert "voice_samples" not in request_parameters_for_family(family)


def test_managed_reference_and_path_list_fields_are_not_projected(monkeypatch):
    monkeypatch.setattr(
        audio_cpp_parameters,
        "family_metadata",
        lambda family: {
            "options": {
                "request": [
                    {"name": "voice_samples", "type": "string"},
                    {"name": "speaker_embedding_file", "type": "string"},
                    {"name": "reference_codes_file", "type": "string"},
                    {"name": "unmanaged_paths", "type": "string_list"},
                    {"name": "unmanaged_audio_path", "type": "audio_path"},
                ]
            }
        },
    )

    assert request_parameters_for_family("fixture") == {}


@pytest.mark.parametrize(
    ("family", "forbidden"),
    (
        (
            "lfm2_audio",
            (
                "audio_chunk_mode",
                "audio_chunk_seconds",
                "text_temperature",
                "text_top_k",
            ),
        ),
        ("dots_tts", ("use_xvector",)),
    ),
)
def test_non_tts_native_fields_are_not_projected_or_selected(family, forbidden):
    parameters = request_parameters_for_family(family)
    for name in forbidden:
        assert name not in parameters
        with pytest.raises(
            ValueError, match=re.escape(f"Unknown audio.cpp option '{name}'")
        ):
            validate_model_options(family, {name: None})


@pytest.mark.parametrize(
    ("family", "model", "forbidden"),
    (
        ("lfm2_audio", "lfm2_audio_1_5b_f16", "stream_frames_per_event"),
        ("dots_tts", "dots_tts_mf_q8_0", "vocoder_merge_steps"),
    ),
)
def test_additional_operation_specific_fields_reject_selected_and_legacy_options(
    family, model, forbidden
):
    assert forbidden not in request_parameters_for_family(family)
    with pytest.raises(
        ValueError, match=re.escape(f"Unknown audio.cpp option '{forbidden}'")
    ):
        _payload(model, audio_cpp_model_settings={model: {forbidden: None}})
    with pytest.raises(
        ValueError, match=re.escape(f"Unknown audio.cpp option '{forbidden}'")
    ):
        _payload(model, audio_cpp_options={forbidden: None})


@pytest.mark.parametrize(
    ("model", "field"),
    (
        ("lfm2_audio_1_5b_f16", "audio_chunk_mode"),
        ("lfm2_audio_1_5b_f16", "audio_chunk_seconds"),
        ("lfm2_audio_1_5b_f16", "text_temperature"),
        ("lfm2_audio_1_5b_f16", "text_top_k"),
        ("dots_tts_mf_q8_0", "use_xvector"),
    ),
)
def test_non_tts_native_fields_are_rejected_from_selected_and_legacy_options(
    model, field
):
    with pytest.raises(
        ValueError, match=re.escape(f"Unknown audio.cpp option '{field}'")
    ):
        _payload(model, audio_cpp_model_settings={model: {field: None}})

    with pytest.raises(
        ValueError, match=re.escape(f"Unknown audio.cpp option '{field}'")
    ):
        _payload(model, audio_cpp_options={field: None})


@pytest.mark.parametrize(
    "model", ("moss_ttsd_q4_k_codec_f16", "vibevoice_1_5b_q8_0")
)
def test_legacy_options_reject_server_reference_paths_but_keep_unknown_fields(model):
    with pytest.raises(
        ValueError, match=re.escape("Unknown audio.cpp option 'voice_samples'")
    ):
        _payload(
            model,
            audio_cpp_options={"voice_samples": "/srv/private/reference.wav"},
        )

    payload = _payload(
        "qwen3_tts_1_7b_base_q8_0",
        audio_cpp_options={"experimental_server_option": "preserved"},
    )
    assert payload["options"]["experimental_server_option"] == "preserved"


def test_moss_ttsd_prefixes_only_linked_reference_text_in_request(monkeypatch):
    model = "moss_ttsd_q4_k_codec_f16"
    original_metadata = tts_handler._audio_cpp_model_metadata

    def cloning_metadata(selected_model, endpoint):
        metadata = original_metadata(selected_model, endpoint)
        if selected_model == model:
            return {**metadata, "voice_mode": "cloning"}
        return metadata

    monkeypatch.setattr(tts_handler, "_audio_cpp_model_metadata", cloning_metadata)
    _stub_compiler(monkeypatch)
    reference = {"sample_id": "managed-reference"}
    payload = _payload(
        model,
        audio_cpp_voice_ref=reference,
        audio_cpp_reference_text="Reviewed words.",
    )

    assert payload["input"] == "compiled:The quick brown fox."
    assert payload["reference_text"] == "[S1] Reviewed words."
    assert payload["voice_ref"] == reference

    payload = _payload(
        model,
        audio_cpp_voice_ref=reference,
        audio_cpp_options={"reference_text": "Legacy reviewed words."},
    )
    assert payload["options"]["reference_text"] == "[S1] Legacy reviewed words."

    already_tagged = _payload(
        model,
        audio_cpp_voice_ref=reference,
        audio_cpp_reference_text="[S1] Reviewed words.",
        audio_cpp_options={"reference_text": "[S1] Legacy reviewed words."},
    )
    assert already_tagged["reference_text"] == "[S1] Reviewed words."
    assert already_tagged["options"]["reference_text"] == "[S1] Legacy reviewed words."


_LINKED_REFERENCE_MODELS = (
    ("f5_tts", "habibi_unified", "F5-TTS"),
    ("glm_tts", "glm_tts_q8_0", "GLM-TTS"),
    ("zipvoice", "zipvoice_distill_q8_0", "ZipVoice"),
    ("outetts", "outetts_1_0_1b_q8_0", "OuteTTS"),
    ("kitten_tts2", "kitten_tts2_q8_0", "KittenTTS2"),
)


@pytest.mark.parametrize(("family", "model", "family_name"), _LINKED_REFERENCE_MODELS)
def test_managed_linked_references_require_transcripts_before_compilation(
    monkeypatch, family, model, family_name
):
    reference = {"sample_id": "managed-sample", "type": "path"}
    message = (
        "KittenTTS2 voice cloning requires a matching reference transcript."
        if family == "kitten_tts2"
        else f"{family_name} linked voice references require a matching reference transcript."
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            speech_performance,
            "compile_for_provider",
            lambda *args, **kwargs: pytest.fail(
                "compiler ran before transcript validation"
            ),
        )
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            _payload(model, audio_cpp_voice_ref=reference)

    payload = _payload(
        model,
        audio_cpp_voice_ref=reference,
        audio_cpp_reference_text="Matching reviewed transcript.",
    )
    assert payload["voice_ref"] == reference
    assert payload["reference_text"] == "Matching reviewed transcript."


def test_generic_cached_voice_reference_behavior_and_omnivoice_guard_remain():
    reference = {"cached_voice_id": "managed-cache-id"}
    payload = _payload("qwen3_tts_1_7b_base_q8_0", audio_cpp_voice_ref=reference)
    assert payload["voice_ref"] == reference
    assert payload["options"]["x_vector_only_mode"] is True

    with pytest.raises(ValueError, match="OmniVoice linked voice references require"):
        _payload("omnivoice_q8_0", audio_cpp_voice_ref=reference)


def test_moss_nano_requires_audio_for_reference_text_before_compilation(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(
            speech_performance,
            "compile_for_provider",
            lambda *args, **kwargs: pytest.fail(
                "compiler ran before reference validation"
            ),
        )
        with pytest.raises(
            ValueError,
            match=re.escape(
                "MOSS-TTS-Nano reference text requires linked reference audio."
            ),
        ):
            _payload(
                "moss_tts_nano_100m_q8_0",
                audio_cpp_reference_text="Words from the missing reference.",
            )

    reference = {"sample_id": "nano-reference"}
    payload = _payload(
        "moss_tts_nano_100m_q8_0",
        audio_cpp_voice_ref=reference,
        audio_cpp_reference_text="Words from the linked reference.",
    )
    assert payload["voice_ref"] == reference
    assert payload["reference_text"] == "Words from the linked reference."


def test_moss_local_does_not_forward_reference_text_from_managed_or_raw_settings():
    payload = _payload(
        "moss_tts_local_v1_5_q8_0",
        audio_cpp_reference_text="Ignored managed transcript.",
        audio_cpp_options={"reference_text": "Ignored legacy transcript."},
    )
    assert "reference_text" not in payload
    assert "reference_text" not in payload.get("options", {})


def test_fish_effective_chunk_default_is_policy_owned_and_preserves_overrides(
    monkeypatch,
):
    descriptor = request_parameters_for_family("fish_audio_s2")["text_chunk_size"]
    assert descriptor["default"] == FISH_AUDIO_TTS_TEXT_CHUNK_SIZE_DEFAULT == 300
    assert descriptor["native_default"] == 200
    assert descriptor["default_source"] == "pandrator_policy"
    assert "native audio.cpp default is 200" in descriptor["description"]

    _stub_compiler(monkeypatch)
    model = "fish_audio_s2_pro_q8_0"
    assert _payload(model)["options"]["text_chunk_size"] == 300

    for value in (200, 400):
        selected = _payload(
            model,
            audio_cpp_model_settings={model: {"text_chunk_size": value}},
        )
        legacy = _payload(model, audio_cpp_options={"text_chunk_size": value})
        assert selected["options"]["text_chunk_size"] == value
        assert legacy["options"]["text_chunk_size"] == value

def test_fish_compiler_request_option_wins_over_policy_default(monkeypatch):
    _stub_compiler(monkeypatch, request_options={"text_chunk_size": 450})
    payload = _payload("fish_audio_s2_pro_q8_0")
    assert payload["options"]["text_chunk_size"] == 450


def test_qwen_max_tokens_is_checkpoint_dynamic_and_never_injected(monkeypatch):
    descriptor = request_parameters_for_family("qwen3_tts")["max_tokens"]
    assert "default" not in descriptor
    assert descriptor["default_note"] == (
        "Selected checkpoint generation_config.json; 2048 fallback."
    )

    _stub_compiler(monkeypatch)
    model = "qwen3_tts_1_7b_base_q8_0"
    payload = _payload(model)
    assert "max_tokens" not in payload.get("options", {})

    explicit = _payload(model, audio_cpp_options={"max_tokens": 700})
    assert explicit["options"]["max_tokens"] == 700


_INDEX_TTS2_MODELS = (
    "index_tts2_f16",
    "index_tts2_orig",
    "index_tts2_q8_0",
    "index_tts2_safetensors",
    "index_tts2_5_f16",
    "index_tts2_5_orig",
    "index_tts2_5_q8_0",
)


@pytest.mark.parametrize("model", _INDEX_TTS2_MODELS)
def test_index_tts2_requires_wav_reference_without_requiring_transcript(
    monkeypatch, model
):
    with monkeypatch.context() as scoped:
        scoped.setattr(
            speech_performance,
            "compile_for_provider",
            lambda *args, **kwargs: pytest.fail(
                "compiler ran before IndexTTS2 reference validation"
            ),
        )
        with pytest.raises(
            ValueError,
            match=re.escape("IndexTTS2 requires a linked WAV reference audio file."),
        ):
            _payload(model)
        with pytest.raises(
            ValueError,
            match=re.escape("IndexTTS2 requires a linked WAV reference audio file."),
        ):
            _payload(
                model,
                audio_cpp_voice_ref={
                    "type": "base64",
                    "data": "data:audio/mpeg;base64,ZmFrZQ==",
                },
            )

    reference = {
        "type": "base64",
        "data": "data:audio/wav;base64,ZmFrZQ==",
    }
    payload = _payload(model, audio_cpp_voice_ref=reference)
    assert payload["voice_ref"] == reference
    assert "reference_text" not in payload


_AUK_GENERATOR_MODELS = (
    "auk_base_f16",
    "auk_base_f32",
    "auk_base_q8_0",
    "auk_flash_f16",
    "auk_flash_f32",
    "auk_flash_q8_0",
)
_AUK_FLASH_BASE_ONLY_OPTIONS = (
    "num_inference_steps",
    "guidance_scale",
    "sway_sampling_coef",
)


def _mark_auk_as_optional_cloning(monkeypatch, *models):
    original = tts_handler._audio_cpp_model_metadata

    def read_metadata(model, endpoint):
        metadata = original(model, endpoint)
        if model in models:
            return {**metadata, "voice_mode": "optional_cloning"}
        return metadata

    monkeypatch.setattr(tts_handler, "_audio_cpp_model_metadata", read_metadata)


def test_auk_exact_model_projection_removes_flash_base_controls():
    base = request_parameters_for_model("auk", "auk_base_q8_0")
    flash = request_parameters_for_model("auk", "auk_flash_q8_0")
    assert set(_AUK_FLASH_BASE_ONLY_OPTIONS) <= base.keys()
    assert not set(_AUK_FLASH_BASE_ONLY_OPTIONS) & flash.keys()
    assert set(_AUK_FLASH_BASE_ONLY_OPTIONS) <= request_parameters_for_family("auk").keys()

    for field in _AUK_FLASH_BASE_ONLY_OPTIONS:
        with pytest.raises(ValueError, match=re.escape(f"Unknown audio.cpp option '{field}'")):
            validate_model_options("auk", {field: None}, model_id="auk_flash_q8_0")
        validate_model_options("auk", {field: None}, model_id="auk_base_q8_0")

    catalogue = {row["id"]: row for row in tts_provider_profiles.AUDIO_CPP_MODEL_CATALOG}
    for model in _AUK_GENERATOR_MODELS:
        row = catalogue[model]
        assert row["reference_audio"] == "optional"
        assert row["upstream_features"]["voice_cloning"] is True
        if "flash" in model:
            assert not set(_AUK_FLASH_BASE_ONLY_OPTIONS) & row["request_parameters"].keys()


@pytest.mark.parametrize("model", _AUK_GENERATOR_MODELS)
def test_auk_requires_finite_positive_duration_without_linked_audio(model):
    message = "AuK generation without linked reference audio requires a finite positive options.duration_sec."
    with pytest.raises(ValueError, match=re.escape(message)):
        _auk_payload(model)

    payload = _auk_payload(model, audio_cpp_options={"duration_sec": 2.5})
    assert payload["options"]["duration_sec"] == 2.5


@pytest.mark.parametrize("duration", (None, 0, -1, True, "2", float("nan"), float("inf")))
def test_auk_rejects_invalid_duration_without_linked_audio(duration):
    message = "AuK generation without linked reference audio requires a finite positive options.duration_sec."
    with pytest.raises(ValueError, match=re.escape(message)):
        _auk_payload("auk_base_q8_0", audio_cpp_options={"duration_sec": duration})


@pytest.mark.parametrize("model", ("auk_base_q8_0", "auk_flash_q8_0"))
def test_auk_linked_wav_reference_can_omit_duration(monkeypatch, model):
    _mark_auk_as_optional_cloning(monkeypatch, model)
    reference = {
        "type": "base64",
        "data": "data:audio/wav;base64,ZmFrZQ==",
    }
    payload = _auk_payload(model, audio_cpp_voice_ref=reference)
    assert payload["voice_ref"] == reference
    assert "duration_sec" not in payload.get("options", {})


@pytest.mark.parametrize("field", _AUK_FLASH_BASE_ONLY_OPTIONS)
def test_auk_flash_rejects_base_controls_in_selected_and_legacy_options(field):
    model = "auk_flash_q8_0"
    with pytest.raises(
        ValueError, match=re.escape(f"Unknown audio.cpp option '{field}'")
    ):
        _auk_payload(
            model,
            audio_cpp_model_settings={model: {field: None, "duration_sec": 2.0}},
        )
    with pytest.raises(
        ValueError, match=re.escape(f"Unknown audio.cpp option '{field}'")
    ):
        _auk_payload(
            model,
            audio_cpp_options={field: 1, "duration_sec": 2.0},
        )

    payload = _auk_payload(
        model,
        audio_cpp_options={
            "experimental_server_option": "preserved",
            "duration_sec": 2.0,
        },
    )
    assert payload["options"]["experimental_server_option"] == "preserved"


@pytest.mark.parametrize("transport", ("options.instruct", "options.instruction"))
def test_compiler_instruction_transport_and_selected_model_precedence_remain(
    monkeypatch, transport
):
    model = "qwen3_tts_1_7b_base_q8_0"

    def compile_for_provider(text, settings, endpoint):
        return SimpleNamespace(
            input=f"compiled:{text}",
            instructions="Compiled voice direction.",
            capabilities={"instruction_transport": transport},
            request_options={
                "instruct": "stale compiler alias",
                "instruction": "stale compiler alias",
            },
        )

    monkeypatch.setattr(speech_performance, "compile_for_provider", compile_for_provider)
    payload = _payload(
        model,
        audio_cpp_model_settings={model: {"temperature": 0.8}},
        audio_cpp_options={"temperature": 0.2, "top_p": 0.3},
    )

    assert payload["input"] == "compiled:The quick brown fox."
    assert payload["options"]["temperature"] == 0.8
    assert "top_p" not in payload["options"]
    expected_key = transport.split(".", 1)[1]
    assert payload["options"][expected_key] == "Compiled voice direction."
    assert "instruct" not in payload["options"] or expected_key == "instruct"
    assert "instruction" not in payload["options"] or expected_key == "instruction"
    assert "instructions" not in payload


@pytest.mark.parametrize('model', ['voxcpm2_q8_0', 'pocket_tts_english_bf16'])
@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('speed_settings', [
    {'speed': 1.0},
    {'audio_cpp_speed': 1},
    {'audio_cpp_options': {'speed': 1.0}},
    {'options': {'speed': None}},
])
def test_plain_speech_omits_inherited_neutral_style(model, preview, speed_settings):
    settings = {
        'generation_prompt': '',
        'openai_audio_instructions': '',
        'audio_cpp_seed': 123,
        **speed_settings,
    }
    if preview:
        settings['preview_service_id'] = 'audio_cpp'
    payload = _payload(model, **settings)
    assert 'speed' not in payload
    assert 'speed' not in payload.get('options', {})
    assert payload['seed'] == 123
    assert payload['input'] == 'The quick brown fox.'
    assert 'instructions' not in payload
    # Resetting model options must not be necessary for ordinary speech.
    reset = _payload(model, audio_cpp_model_settings={model: {}}, speed=1.0)
    assert payload['input'] == reset['input']
    assert set(payload) - set(reset) == {'seed'}


@pytest.mark.parametrize('model', ['voxcpm2_q8_0', 'pocket_tts_english_bf16'])
@pytest.mark.parametrize('speed_settings', [
    {'speed': 1.2},
    {'audio_cpp_speed': 0.8},
    {'audio_cpp_options': {'speed': 1.2}},
    {'speed': 1, 'options': {'speed': 0.8}},
    {'speed': True},
])
def test_unsupported_speech_speed_fails_before_http(model, speed_settings):
    with pytest.raises(ValueError, match='speed is unsupported'):
        _payload(model, **speed_settings)


def test_neutral_speed_filter_preserves_supported_speed_and_voxcpm_direction():
    payload = _payload('kokoro_82m_q8_0', speed=1.2, speaker='af_heart')
    assert payload['speed'] == 1.2
    directed = _payload('voxcpm2_q8_0', speed=1.0, generation_prompt='Warm and clear.')
    assert 'Warm and clear.' in directed['input']
    assert 'speed' not in directed


@pytest.mark.parametrize('settings', [
    {'audio_cpp_options': {'repetition_penalty': 1.1}},
    {'options': {'repetition_penalty': 1.1}},
    {'audio_cpp_repetition_penalty': 1.1},
])
def test_higgs_no_op_cannot_be_forwarded_through_legacy_settings(settings):
    with pytest.raises(ValueError, match='Unknown.*repetition_penalty'):
        _payload('higgs_audio_tts_4b_q8_0', **settings)
