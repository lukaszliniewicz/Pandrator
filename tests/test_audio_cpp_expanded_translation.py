"""Pinned runtime request contracts; no weight downloads or inference."""

import pytest

from pandrator.logic.audio_cpp_catalogue import (
    catalogue_page,
    inventory,
    package_metadata,
)
from pandrator.logic.speech_performance import compile_performance
from pandrator.logic.tts_handler import _build_audio_cpp_audio_payload as payload


def settings(model, **extra):
    return {"service": "audio_cpp", "model": model, **extra}


@pytest.mark.parametrize(
    "model,event,language,token",
    [
        ("chatterbox_turbo_q8_0", "laugh", "en", "[laugh]"),
        ("breeze_tts_2_q8_0", "laugh", "en", "(laugh)"),
        ("breeze_tts_2_q8_0", "clear_throat", "zh", "[清嗓子]"),
        ("omnivoice_q8_0", "laugh", "en", "[laughter]"),
    ],
)
def test_event_dialects_preserve_transcript(model, event, language, token):
    compiled = compile_performance(
        "Hello.",
        settings(
            model,
            language=language,
            performance_allow_vocalizations=True,
            _performance={"decision": "steer", "events": [{"kind": event}]},
        ),
    )
    assert compiled.transcript == "Hello."
    assert compiled.input == token + "Hello."


def test_voxcpm_style_prefix_is_not_a_spoken_transcript_or_unused_field():
    opts = settings("voxcpm2_q8_0", generation_prompt="Warm baritone")
    compiled = compile_performance("Hello.", opts)
    assert compiled.transcript == "Hello."
    assert compiled.input == "(Warm baritone)Hello."
    result = payload("Hello.", opts, {})
    assert result["input"] == compiled.input
    assert "instructions" not in result
    with pytest.raises(ValueError, match="nested parentheses"):
        payload("Hello.", {**opts, "generation_prompt": "Warm (loud)"}, {})


def test_firered_selects_design_or_reference_template_without_editing():
    opts = settings("fireredtts3_instruct_q8_0", generation_prompt="Quiet baritone")
    designed = payload("Hello.", opts, {})
    assert designed["options"]["template_name"] == "voice_design"
    assert designed["instructions"] == "Quiet baritone"
    with pytest.raises(ValueError, match="requires a voice description"):
        payload("Hello.", settings("fireredtts3_instruct_q8_0"), {})
    cloning = {
        **opts,
        "audio_cpp_voice_ref": {"data": "fixture"},
        "audio_cpp_reference_text": "Reference.",
    }
    cloned = payload("Hello.", cloning, {})
    assert cloned["options"]["template_name"] == "instruct_tts"
    assert cloned["reference_text"] == "Reference."
    assert "instructions" not in cloned
    assert any(
        item["status"] == "unsupported"
        for item in compile_performance("Hello.", cloning).report
    )


def test_cosyvoice_instruction_template_and_neutts_voice_option():
    result = payload(
        "Hello.",
        settings("cosyvoice3_q8_0", generation_prompt="Cheerfully", language="en"),
        {},
    )
    assert result["options"]["template_name"] == "instruct"
    assert result["instructions"] == "Cheerfully"
    assert "language" not in result
    result = payload("Hello.", settings("neutts_2e_orig", voice="greta"), {})
    assert result["options"]["voice_id"] == "greta"
    assert "voice" not in result


def test_unknown_model_does_not_silently_gain_cloning():
    with pytest.raises(ValueError, match="no verified speech request contract"):
        payload(
            "Hello.",
            settings("mystery-model", audio_cpp_voice_ref={"data": "fixture"}),
            {},
        )
    result = payload(
        "Hello.",
        settings("local-preset", voice="M2"),
        {
            "model_catalog": [
                {"id": "local-preset", "family": "supertonic", "task": "tts"}
            ]
        },
    )
    assert result["voice"] == "M2"


def test_catalogue_covers_inventory_without_claiming_editing_or_installation():
    assert len(inventory()["families"]) == 86
    assert catalogue_page(limit=100)["total"] == 256
    qwen = package_metadata("qwen3_tts_1_7b_customvoice_q8_0")
    assert qwen["reference_audio"] == "not_used"
    assert not qwen["upstream_features"]["voice_cloning"]
    assert qwen["pandrator_features"]["speech_editing"] == "not_implemented"
    assert qwen["estimated_download_bytes"] > 0
    assert qwen["package_availability"]["status"] == "installable"
    music = package_metadata("vevo2_q8_0")
    assert music["pandrator_features"]["speech_generation"] == "catalogued_only"
    assert music["voice_mode"] == "none"
    licensed = catalogue_page(commercial_use="permitted", limit=100)
    assert licensed["items"]
    assert all(
        item["license"]["commercial_use"].startswith("permitted")
        for item in licensed["items"]
    )
    assert all(item["family"] != "breeze_tts" for item in licensed["items"])


def test_package_language_and_control_filters():
    results = catalogue_page(family="pocket_tts", language="de", limit=100)
    assert results["items"]
    assert all(item["supported_languages"] == ["de"] for item in results["items"])
    for item in results["items"]:
        with pytest.raises(ValueError, match="matching the requested language"):
            payload("Hello.", settings(item["id"], language="en"), {})
    assert (
        catalogue_page(capability="vocal_events", recommended_only=True)["total"] >= 3
    )


def test_neutts_emotion_and_supertonic_native_rate_options():
    result = payload(
        "Hello.",
        settings(
            "neutts_2e_orig",
            _performance={"decision": "steer", "delivery": {"emotion": "happy"}},
        ),
        {},
    )
    assert result["options"]["emotion"] == "happy"
    assert result["input"] == "Hello."
    model = "supertonic_3_q8_0"
    result = payload(
        "Hello.",
        settings(
            model,
            audio_cpp_model_settings={
                model: {"speaking_rate": 1.2, "num_inference_steps": 8}
            },
        ),
        {},
    )
    assert result["options"] == {"speaking_rate": 1.2, "num_inference_steps": 8}


def test_supertonic_does_not_emit_unverified_event_tags():
    compiled = compile_performance(
        "Hello.",
        settings(
            "supertonic_3_q8_0",
            performance_allow_vocalizations=True,
            _performance={"decision": "steer", "events": [{"kind": "sigh"}]},
        ),
    )
    assert compiled.input == "Hello."
    assert any(row["status"] == "unsupported" for row in compiled.report)


def test_turbo_token_limit_uses_native_field():
    model = "chatterbox_turbo_q8_0"
    result = payload(
        "Hello.",
        settings(model, audio_cpp_model_settings={model: {"max_new_tokens": 200}}),
        {},
    )
    assert result["options"]["max_new_tokens"] == 200
    assert "max_tokens" not in result


def test_editing_variants_remain_catalogue_only():
    from pandrator_manager.components.audiocpp import MODEL_PACKAGES
    for model in ("dots_tts_edit_bf16", "dots_tts_edit_q8_0"):
        assert model not in MODEL_PACKAGES
        item = package_metadata(model)
        assert item["upstream_features"]["speech_editing"]
        assert item["pandrator_features"]["speech_generation"] == "catalogued_only"
        assert item["package_availability"]["status"] == "catalogued_only"
        with pytest.raises(ValueError, match="no verified speech request contract"):
            payload("Hello.", settings(model), {})


def test_moss_design_maps_instruction_language_and_full_width_seed():
    model = "moss_voicegen_bf16_codec_f16_decode"
    result = payload("Hello.", settings(model, language="en", generation_prompt="Warm baritone", audio_cpp_seed=4294967295), {})
    assert result["instructions"] == "Warm baritone"
    assert result["options"]["language"] == "English"
    assert result["options"]["seed"] == -1
    assert "language" not in result and "seed" not in result
