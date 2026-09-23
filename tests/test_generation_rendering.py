"""Pure render planning and in-memory assembly contract tests."""

from __future__ import annotations

import pytest
from pydub import AudioSegment

from pandrator.web.generation_rendering import (
    build_render_parts,
    execute_render_parts,
)

CHARACTERS = [
    {"id": "c-one", "display_name": "One", "voice_category": "male"},
    {"id": "c-two", "display_name": "Two", "voice_category": "female"},
]
CONTROLS = {
    "characters": CHARACTERS,
    "cast": {
        "narrator": {"voice": "narrator"},
        "characters": {"c-one": {"voice": "one"}, "c-two": {"voice": "two"}},
        "categories": {"male": {"voice": "male"}, "female": {"voice": "female"}},
        "source_speakers": {"S1": {"voice": "source"}},
    },
}


def xml(text: str) -> str:
    return (
        '<segment id="s"><dialogue><speaker ref="c-one">'
        + text
        + '</speaker></dialogue></segment>'
    )


def test_two_voices_preserve_one_logical_text_and_precedence():
    markup = '<segment id="s"><dialogue><speaker ref="c-one">A</speaker><speaker ref="c-two">B</speaker></dialogue></segment>'
    parts = build_render_parts(
        "AB",
        {"casting_enabled": True, "performance_enabled": False},
        speech_xml=markup,
        segment_id="s",
        controls=CONTROLS,
    )
    assert [part["text"] for part in parts] == ["A", "B"]
    assert [part["settings"]["voice"] for part in parts] == ["one", "two"]
    assert "_performance" not in parts[0]["settings"]
    assert "".join(part["text"] for part in parts) == "AB"
    assert parts[0]["speaker_ids"] == ["c-one"]
    assert parts[1]["speaker_ids"] == ["c-two"]


def test_category_only_speaker_uses_category_cast_without_character_dictionary():
    markup = '<segment id="s"><speaker g="female">Hello</speaker></segment>'
    controls = {
        "characters": [],
        "cast": {"categories": {"female": {"voice": "category-female"}}},
    }
    parts = build_render_parts(
        "Hello",
        {"casting_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=controls,
    )
    assert parts[0]["settings"]["voice"] == "category-female"
    assert parts[0]["voice_source"] == "category"
    assert parts[0]["speaker_ids"] == []


def test_explicit_narrator_inside_dialogue_ignores_source_and_category_fallbacks():
    markup = '<segment id="s"><dialogue><narrator>Aside</narrator></dialogue></segment>'
    controls = {
        "characters": [],
        "cast": {
            "narrator": {"voice": "narrator"},
            "categories": {"female": {"voice": "category-female"}},
            "source_speakers": {"S1": {"voice": "source"}},
        },
    }
    parts = build_render_parts(
        "Aside",
        {"casting_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=controls,
        source_speaker="S1",
    )
    assert parts[0]["settings"]["voice"] == "narrator"
    assert parts[0]["voice_source"] == "narrator"
    assert parts[0]["speaker_ids"] == []


def test_same_voice_coalesces_even_when_delivery_and_speaker_differ():
    markup = '<segment id="s"><dialogue><speaker ref="c-one"><emphasis>light</emphasis>A</speaker><speaker ref="c-two"><pace>brisk</pace>B</speaker></dialogue></segment>'
    controls = {**CONTROLS, "cast": {**CONTROLS["cast"], "characters": {"c-one": {"voice": "same"}, "c-two": {"voice": "same"}}}}
    parts = build_render_parts(
        "AB",
        {"casting_enabled": True, "performance_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=controls,
    )
    assert len(parts) == 1
    assert parts[0]["text"] == "AB"
    assert parts[0]["settings"]["_performance"]["decision"] == "steer"


def test_root_and_scoped_directions_project_inside_one_same_voice_part():
    markup = '<segment id="s"><ins>root direction</ins>A<span><em>local emotion</em>B</span></segment>'
    controls = {**CONTROLS, "cast": {**CONTROLS["cast"], "narrator": {"voice": "one"}}}
    parts = build_render_parts(
        "AB",
        {"casting_enabled": True, "performance_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=controls,
    )
    assert len(parts) == 1
    annotation = parts[0]["settings"]["_performance"]
    assert annotation["decision"] == "steer"
    assert annotation["spans"][0]["anchor"]["quote"] == "A"
    assert annotation["spans"][0]["delivery"]["instruction"] == "root direction"
    assert annotation["spans"][1]["anchor"]["quote"] == "B"
    assert annotation["spans"][1]["delivery"]["instruction"] == "root direction"


def test_xml_directions_project_to_each_part_and_events_choose_one_boundary():
    markup = '<segment id="s"><dialogue><speaker ref="c-one">A<event kind="pause" duration_ms="100"/></speaker><speaker ref="c-two"><emphasis>strong</emphasis>B<event kind="sigh"/></speaker></dialogue></segment>'
    parts = build_render_parts(
        "AB",
        {"casting_enabled": True, "performance_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=CONTROLS,
    )
    assert [part["text"] for part in parts] == ["A", "B"]
    assert parts[0]["settings"]["_performance"]["events"] == []
    assert len(parts[1]["settings"]["_performance"]["events"]) == 2
    assert parts[1]["settings"]["_performance"]["events"][0]["position"] == "before"
    assert parts[1]["settings"]["_performance"]["events"][1]["position"] == "after"


def test_whitespace_folds_without_empty_provider_requests():
    markup = '<segment id="s"><speaker ref="c-one">A</speaker> <speaker ref="c-two">B</speaker></segment>'
    parts = build_render_parts(
        "A B",
        {"casting_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=CONTROLS,
    )
    assert [part["text"] for part in parts] == ["A ", "B"]
    assert all(part["text"] for part in parts)


def test_punctuation_only_narrator_after_dialogue_uses_previous_speakable_voice():
    markup = '<segment id="s"><dialogue><speaker ref="c-one">Hello</speaker><narrator>.</narrator></dialogue></segment>'
    parts = build_render_parts(
        "Hello.",
        {"casting_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=CONTROLS,
    )
    assert len(parts) == 1
    assert parts[0]["text"] == "Hello."
    assert parts[0]["settings"]["voice"] == "one"
    assert parts[0]["voice_source"] == "character"
    assert parts[0]["start"] == 0
    assert parts[0]["end"] == len("Hello.")


def test_leading_orphan_punctuation_uses_following_speakable_voice():
    markup = '<segment id="s"><narrator>…</narrator><dialogue><speaker ref="c-two">你好</speaker></dialogue></segment>'
    parts = build_render_parts(
        "…你好",
        {"casting_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=CONTROLS,
    )
    assert len(parts) == 1
    assert parts[0]["text"] == "…你好"
    assert parts[0]["settings"]["voice"] == "two"
    assert parts[0]["voice_source"] == "character"


def test_punctuation_control_and_event_are_projected_once_when_folded():
    markup = '<segment id="s"><speaker ref="c-one"><emphasis>strong</emphasis>.<event kind="pause" duration_ms="100"/></speaker><speaker ref="c-two">word</speaker></segment>'
    controls = {**CONTROLS, "cast": {**CONTROLS["cast"], "characters": {"c-one": {"voice": "same"}, "c-two": {"voice": "same"}}}}
    parts = build_render_parts(
        ".word",
        {"casting_enabled": True, "performance_enabled": True},
        speech_xml=markup,
        segment_id="s",
        controls=controls,
    )
    assert len(parts) == 1
    assert parts[0]["text"] == ".word"
    assert parts[0]["settings"]["voice"] == "same"
    events = parts[0]["settings"]["_performance"]["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "pause"


@pytest.mark.parametrize("text", ["...", "!!!", "、？！"])
def test_punctuation_only_segments_fail_instead_of_synthesizing(text):
    with pytest.raises(ValueError, match="speakable|punctuation"):
        build_render_parts(text, {})


def test_event_only_segments_fail_explicitly():
    markup = '<segment id="s"><event kind="pause" duration_ms="100"/></segment>'
    with pytest.raises(ValueError, match="vocal events|event-only"):
        build_render_parts(
            "",
            {},
            speech_xml=markup,
            segment_id="s",
        )


def test_no_xml_preserves_existing_request_and_does_not_leak_markup():
    settings = {
        "voice": "base",
        "generation_prompt": "direction",
        "performance_enabled": True,
        "_performance": {"decision": "none"},
        "secret": "retain in settings only",
    }
    parts = build_render_parts("Plain text", settings)
    assert len(parts) == 1
    assert parts[0]["text"] == "Plain text"
    assert parts[0]["settings"]["voice"] == "base"
    assert "speech_xml" not in parts[0]["settings"]
    assert parts[0]["settings"]["_performance"]["decision"] == "none"
    assert parts[0]["settings"] == settings


def test_casting_and_directions_are_independent():
    markup = '<segment id="s"><speaker ref="c-one"><emphasis>strong</emphasis>A</speaker></segment>'
    parts = build_render_parts(
        "A",
        {"casting_enabled": True, "performance_enabled": False},
        speech_xml=markup,
        segment_id="s",
        controls=CONTROLS,
    )
    assert parts[0]["settings"]["voice"] == "one"
    assert parts[0]["settings"]["performance_enabled"] is False
    assert "_performance" not in parts[0]["settings"]


def test_source_speaker_applies_only_to_unnamed_dialogue_or_no_xml():
    narrator_markup = '<segment id="s"><narrator>Aside</narrator></segment>'
    narrator = build_render_parts(
        "Aside",
        {"casting_enabled": True},
        speech_xml=narrator_markup,
        segment_id="s",
        controls=CONTROLS,
        source_speaker="S1",
    )
    assert narrator[0]["settings"]["voice"] == "narrator"
    assert narrator[0]["voice_source"] == "narrator"

    plain = build_render_parts(
        "Cue",
        {"casting_enabled": True},
        controls=CONTROLS,
        source_speaker="S1",
    )
    assert plain[0]["settings"]["voice"] == "source"
    assert plain[0]["voice_source"] == "source_speaker"


def test_unknown_managed_voice_requires_callback():
    controls = {"characters": [], "cast": {"narrator": {"voice_id": "managed"}}}
    with pytest.raises(ValueError, match="voice_id"):
        build_render_parts("Text", {"casting_enabled": True}, controls=controls)

    seen = []

    def apply(binding, prepared):
        seen.append(binding)
        prepared["voice"] = f"resolved:{binding['voice_id']}"
        return prepared

    parts = build_render_parts(
        "Text",
        {"casting_enabled": True},
        controls=controls,
        apply_binding=apply,
    )
    assert seen == [{"voice": "", "voice_id": "managed", "service": None, "model": None, "voice_description": ""}]
    assert parts[0]["settings"]["voice"] == "resolved:managed"


def test_context_preserved_and_other_voice_sibling_labeled():
    markup = '<segment id="s"><dialogue><speaker ref="c-one">A</speaker><speaker ref="c-two">B</speaker></dialogue></segment>'
    settings = {
        "casting_enabled": True,
        "tts_context_mode": "both",
        "_semantic_context": {"before": "external before", "after": "external after"},
    }
    parts = build_render_parts(
        "AB", settings, speech_xml=markup, segment_id="s", controls=CONTROLS
    )
    assert "external before" in parts[0]["settings"]["_semantic_context"]["before"]
    assert "[Other speaker: c-two] B" in parts[0]["settings"]["_semantic_context"]["after"]
    assert "[Other speaker: c-one] A" in parts[1]["settings"]["_semantic_context"]["before"]
    assert parts[1]["settings"]["_semantic_context"]["after"] == "external after"


def test_execution_is_contiguous_and_failure_or_cancel_returns_no_partial_result():
    parts = [
        {"index": 0, "start": 0, "end": 1, "text": "A", "settings": {}, "voice_source": "base", "fallback": False, "speaker_ids": []},
        {"index": 1, "start": 1, "end": 2, "text": "B", "settings": {}, "voice_source": "base", "fallback": False, "speaker_ids": []},
    ]
    combined, manifest = execute_render_parts(
        parts,
        synthesize=lambda text, _settings: AudioSegment.silent(duration=10 if text == "A" else 20),
        cancelled=lambda: False,
    )
    assert len(combined) == 30
    assert [item["duration_ms"] for item in manifest] == [10, 20]
    assert set(manifest[0]) == {"index", "range", "voice", "source", "fallback", "speaker_ids", "duration_ms"}
    assert manifest[0]["range"] == [0, 1]

    calls = 0

    def cancel_after_first():
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(RuntimeError, match="cancelled"):
        execute_render_parts(
            parts,
            synthesize=lambda _text, _settings: AudioSegment.silent(duration=10),
            cancelled=cancel_after_first,
        )

    def fail(text, _settings):
        if text == "B":
            raise RuntimeError("failed")
        return AudioSegment.silent(duration=10)

    with pytest.raises(RuntimeError, match="failed"):
        execute_render_parts(parts, synthesize=fail, cancelled=lambda: False)
