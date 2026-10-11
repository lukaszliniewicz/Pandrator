"""Capability-owned scope transport, native controls, and subtitle fidelity."""

from copy import deepcopy

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import speech_performance, tts_handler
from pandrator.logic.speech_performance import compile_performance, resolve_capabilities
from pandrator.web.generation_rendering import build_render_parts


def test_synthesis_preserves_scoped_whitespace_at_voice_boundaries(monkeypatch):
    text = "Mara paused. Keep the key, Fred said. I will be back."
    xml = (
        '<segment id="s"><ins>Speak clearly.</ins>'
        '<narrator>Mara paused. </narrator>'
        '<speaker ref="fred">Keep the key</speaker>'
        '<narrator>, Fred said. </narrator>'
        '<speaker ref="fred">I will be <span><ins>Stress this word.</ins>'
        'back</span>.</speaker></segment>'
    )
    settings = {
        "service": "audio_cpp",
        "model": "breeze_tts_2_q8_0",
        "speaker": "narrator-reference",
        "language": "en",
        "casting_enabled": True,
        "performance_enabled": True,
    }
    parts = build_render_parts(
        text,
        settings,
        speech_xml=xml,
        segment_id="s",
        controls={
            "characters": [{"id": "fred", "display_name": "Fred"}],
            "cast": {
                "narrator": {"voice": "narrator-reference"},
                "characters": {"fred": {"voice": "fred-reference"}},
            },
        },
    )
    payloads = []

    def request_audio(part_text, part_settings, **kwargs):
        payloads.append(
            tts_handler._build_audio_cpp_audio_payload(
                part_text, part_settings, {"id": "audio_cpp", "adapter": "audio_cpp"}
            )
        )
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(tts_handler, "_request_openai_compatible_audio", request_audio)
    monkeypatch.setattr(
        tts_handler, "_decode_audio_response", lambda response: AudioSegment.silent(20)
    )
    for part in parts:
        audio = tts_handler.text_to_audio(
            part["text"], part["settings"], max_attempts=1, _audio_cpp_lock_held=True
        )
        assert audio is not None

    assert [payload["input"] for payload in payloads] == [part["text"] for part in parts]
    assert {payload["voice"] for payload in payloads} == {"narrator-reference", "fred-reference"}
    assert "For occurrence 1 of 'back': Stress this word." in payloads[-1]["instructions"]


def markup_request(model, text, xml, *, service="audio_cpp", **settings):
    parts = build_render_parts(text, {"service": service, "model": model, "performance_enabled": True, **settings}, speech_xml=xml, segment_id="s")
    assert len(parts) == 1
    return compile_performance(parts[0]["text"], parts[0]["settings"], {"id": service})


@pytest.mark.parametrize("model,service", [("fish_audio_s2_pro_q8_0", "audio_cpp"), ("eleven_v4", "elevenlabs"), ("gpt-4o-mini-tts", "openai"), ("gemini-3.8-flash-tts", "vertex_ai")])
def test_uniform_markup_is_one_request_direction_on_each_transport(model, service):
    text = "The clock stopped."
    compiled = markup_request(model, text, '<segment id="s"><ins>Whisper.</ins>The clock stopped.</segment>', service=service)
    assert compiled.transcript == text
    assert (compiled.input + compiled.instructions).count("Whisper.") == 1
    assert not any(row["control"] == "span" for row in compiled.report)


def test_adjacent_fish_directions_emit_one_transition_and_no_automatic_competing_reset():
    text = "Before secret after."
    compiled = markup_request("fish_audio_s2_pro_q8_0", text, '<segment id="s">Before <span><ins>Whisper.</ins>secret</span><span><ins>Speak normally.</ins> after.</span></segment>')
    assert compiled.input == "Before [Whisper.]secret[Speak normally.] after."
    assert "natural delivery" not in compiled.input
    assert compiled.request_options["text_chunk_mode"] == "tag_aware"
    assert compiled.transcript == text


def test_legacy_anchor_transition_does_not_add_a_reset_before_the_next_direction():
    compiled = compile_performance("A B C", {"service": "audio_cpp", "model": "fish_audio_s2_pro_q8_0", "_performance": {"decision": "steer", "spans": [{"anchor": {"quote": "B"}, "delivery": {"instruction": "Whisper"}}, {"anchor": {"quote": " C"}, "delivery": {"instruction": "Normal"}}]}}, {"id": "audio_cpp"})
    assert compiled.input == "A [Whisper]B[Normal] C"


def test_future_instruction_transport_uses_quoted_phrase_hints_and_hidden_context(monkeypatch):
    profile = resolve_capabilities({"service": "vertex_ai", "model": "gemini-3.8-flash-tts"}, {"id": "vertex_ai"})
    profile.update(model="future-omni", backend="future", dialect="future", instruction_transport="style")
    monkeypatch.setattr(speech_performance, "resolve_capabilities", lambda *args, **kwargs: deepcopy(profile))
    compiled = markup_request("future-omni", "Before secret after.", '<segment id="s"><ins>Warm.</ins>Before <span><ins>Whisper.</ins>secret</span> after.</segment>', service="future", tts_context_mode="both", _semantic_context={"before": "Earlier.", "after": "Later."})
    assert compiled.input == compiled.transcript == "Before secret after."
    assert compiled.instructions.startswith("Warm.\nFor occurrence 1 of 'secret': Whisper.")
    assert "Preceding context:" in compiled.instructions and "Following context:" in compiled.instructions
    assert "Earlier." not in compiled.input


def test_request_only_channel_does_not_emit_phrase_directions():
    compiled = markup_request("voxcpm2_q8_0", "Before secret after.", '<segment id="s"><ins>Warm.</ins>Before <span><ins>Whisper.</ins>secret</span> after.</segment>')
    assert compiled.input == "(Warm.)Before secret after."
    assert any(row["control"] == "span" and row["status"] == "unsupported" for row in compiled.report)


def test_index_tts_emotion_uses_native_option_without_passing_general_instructions():
    compiled = markup_request("index_tts2_q8_0", "Hello.", '<segment id="s"><em>quiet delight</em><ins>Whisper.</ins>Hello.</segment>')
    assert compiled.input == compiled.transcript == "Hello."
    assert compiled.instructions == ""
    assert compiled.request_options == {"emotion_text": "quiet delight", "use_emotion_text": True}
    assert any(row["control"] == "direction" and row["status"] == "unsupported" for row in compiled.report)


@pytest.mark.parametrize("model", ["eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_flash_v2"])
def test_provider_break_clamps_duration_and_keeps_display_text(model):
    compiled = compile_performance("Hello.", {"service": "elevenlabs", "model": model, "_performance": {"decision": "steer", "events": [{"kind": "pause", "duration_ms": 5000}]}}, {"id": "elevenlabs"})
    assert compiled.input == '<break time="3000ms" />Hello.'
    assert compiled.transcript == "Hello."
    assert any(row["control"] == "pause" and row["status"] == "approximated" for row in compiled.report)


def test_v4_pause_duration_selects_a_contextual_tag_and_turbo_omits_unknown_pause():
    settings = {"service": "elevenlabs", "model": "eleven_v4", "_performance": {"decision": "steer", "events": [{"kind": "pause", "duration_ms": 2000}]}}
    assert compile_performance("Hello.", settings, {"id": "elevenlabs"}).input == "[long pause]Hello."
    turbo = compile_performance("Hello.", {**settings, "model": "eleven_v4_turbo"}, {"id": "elevenlabs"})
    assert turbo.input == "Hello."
    assert any(row["status"] == "unsupported" for row in turbo.report)


def test_ttsd_positional_speaker_prefix_does_not_reach_subtitle_text():
    compiled = markup_request("moss_ttsd_q8_0_codec_f16", "Hello.", '<segment id="s"><ins>Warm.</ins>Hello.</segment>')
    assert compiled.input == "[S1] Hello."
    assert compiled.transcript == "Hello."
    assert compiled.instructions == "Warm."


def test_azure_deployment_requires_explicit_identity_and_retains_selector():
    endpoint = {"id": "azure-openai-v1", "provider": "openai", "adapter": "openai_compatible"}
    settings = {"service": "azure-openai-v1", "model": "narrator", "generation_prompt": "Warm."}
    assert compile_performance("Hello.", settings, endpoint).instructions == ""
    mapped = {**endpoint, "model_catalog": [{"id": "narrator", "upstream_model_id": "gpt-4o-mini-tts-2025-12-15"}]}
    compiled = compile_performance("Hello.", settings, mapped)
    assert compiled.instructions == "Warm."
    assert compiled.capabilities["model"] == "narrator"
    assert compiled.capabilities["upstream_model_id"] == "gpt-4o-mini-tts-2025-12-15"


def test_maya_upstream_exhale_tag_stays_out_of_subtitle_text():
    compiled = compile_performance("Hello.", {"service": "audio_cpp", "model": "maya1_q8_0", "generation_prompt": "A calm adult voice.", "performance_allow_vocalizations": True, "_performance": {"decision": "steer", "events": [{"kind": "exhale"}]}}, {"id": "audio_cpp"})
    assert compiled.input == "<exhale>Hello."
    assert compiled.transcript == "Hello."
