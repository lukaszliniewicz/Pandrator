"""Selection-slot provenance and retained-input grouping contracts."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

import pytest

from pandrator.web.generation_controls import resolve_cast_voice
from pandrator.web.generation_rendering import build_render_parts
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SELECTOR_KEY,
    SpeechInputError,
    canonical_json,
)

SlotBindingCallback = Callable[
    [dict[str, Any] | None, dict[str, Any], str | None], dict[str, Any]
]
def _binding(voice: str) -> dict[str, str | None]:
    return {
        "voice": voice,
        "voice_id": None,
        "service": None,
        "model": None,
        "voice_description": "",
    }


def _slot(bucket: str, member: str) -> str:
    return "cast:" + canonical_json([bucket, member])


def _controls() -> dict[str, Any]:
    return {
        "characters": [
            {"id": "c-one", "display_name": "One", "voice_category": "female"},
            {"id": "c-two", "display_name": "Two", "voice_category": "female"},
        ],
        "cast": {
            "narrator": {"voice": "narrator"},
            "categories": {"female": {"voice": "category-female"}},
            "characters": {
                "c-one": {"voice": "shared"},
                "c-two": {"voice": "shared"},
            },
            "source_speakers": {"S1": {"voice": "source"}},
        },
    }


def test_resolver_returns_canonical_slot_separately_for_each_selection_source():
    controls = _controls()

    span = resolve_cast_voice(
        controls,
        speaker_id="c-one",
        dialogue=True,
        span_voice="authored",
        source_speaker="S1",
    )
    assert span == {
        "binding": _binding("authored"),
        "source": "span",
        "fallback": False,
        "slot_key": None,
    }

    character = resolve_cast_voice(controls, speaker_id=" c-one ", dialogue=True)
    assert character == {
        "binding": _binding("shared"),
        "source": "character",
        "fallback": False,
        "slot_key": _slot("characters", "c-one"),
    }
    assert "slot_key" not in character["binding"]

    source = resolve_cast_voice(
        controls, source_speaker=" S1 ", dialogue=True, voice_category="female"
    )
    assert source == {
        "binding": _binding("source"),
        "source": "source_speaker",
        "fallback": False,
        "slot_key": _slot("source_speakers", "S1"),
    }

    category = resolve_cast_voice(
        controls, dialogue=True, voice_category=" FEMALE "
    )
    assert category == {
        "binding": _binding("category-female"),
        "source": "category",
        "fallback": True,
        "slot_key": _slot("categories", "female"),
    }

    narrator = resolve_cast_voice(controls, dialogue=True)
    assert narrator == {
        "binding": _binding("narrator"),
        "source": "narrator",
        "fallback": True,
        "slot_key": "cast:narrator",
    }

    inherited = resolve_cast_voice({"characters": [], "cast": {}}, dialogue=True)
    assert inherited == {
        "binding": None,
        "source": "inherited",
        "fallback": True,
        "slot_key": None,
    }


def test_inferred_character_category_uses_the_category_slot_without_character_binding():
    controls = {
        "characters": [{"id": "c-one", "display_name": "One", "voice_category": "female"}],
        "cast": {"categories": {"female": {"voice": "category-female"}}},
    }

    result = resolve_cast_voice(controls, speaker_id="c-one", dialogue=True)

    assert result == {
        "binding": _binding("category-female"),
        "source": "category",
        "fallback": True,
        "slot_key": _slot("categories", "female"),
    }


def test_slot_callback_gets_selected_slot_and_isolated_binding_and_settings():
    controls = _controls()
    original_controls = deepcopy(controls)
    settings: dict[str, Any] = {
        "casting_enabled": True,
        "performance_enabled": False,
        "nested": {"items": [1]},
    }
    original_settings = {**settings, "nested": {"items": [1]}}
    seen: list[tuple[dict[str, Any] | None, str | None]] = []

    def apply_at_slot(
        binding: dict[str, Any] | None,
        prepared: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        seen.append((deepcopy(binding), slot_key))
        assert binding is not None
        binding["voice"] = "callback-mutated"
        prepared["nested"]["items"].append(2)
        prepared["voice"] = "callback-voice"
        return prepared

    parts = build_render_parts(
        "Hello",
        settings,
        speech_xml='<segment id="s"><speaker ref="c-one">Hello</speaker></segment>',
        segment_id="s",
        controls=controls,
        apply_binding_at_slot=apply_at_slot,
    )

    assert seen == [(_binding("shared"), _slot("characters", "c-one"))]
    assert settings == original_settings
    assert controls == original_controls
    assert parts[0]["settings"]["nested"] == {"items": [1, 2]}
    assert parts[0]["settings"]["voice"] == "callback-voice"
    assert "slot_key" not in parts[0]
    assert parts[0]["start"] == 0
    assert parts[0]["end"] == 5


def test_legacy_two_argument_callback_and_strict_single_voice_behavior_remain_compatible():
    legacy_calls: list[dict[str, Any] | None] = []

    def apply_legacy(
        binding: dict[str, Any] | None, prepared: dict[str, Any]
    ) -> dict[str, Any]:
        legacy_calls.append(binding)
        assert binding is not None
        prepared["voice"] = binding["voice"]
        return prepared

    legacy = build_render_parts(
        "Hello",
        {"casting_enabled": True, "performance_enabled": False},
        controls=_controls(),
        source_speaker="S1",
        apply_binding=apply_legacy,
    )
    assert legacy[0]["settings"]["voice"] == "source"
    assert legacy_calls == [_binding("source")]

    new_calls: list[str | None] = []

    def apply_at_slot(
        binding: dict[str, Any] | None,
        prepared: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        new_calls.append(slot_key)
        prepared["voice"] = "should-not-apply"
        return prepared

    strict = build_render_parts(
        "Hello",
        {
            "voice": "base",
            "casting_enabled": False,
            "voice_mode_version": 1,
            "performance_enabled": False,
        },
        controls=_controls(),
        voice_binding={"voice": "block"},
        apply_binding_at_slot=apply_at_slot,
    )
    assert strict[0]["settings"]["voice"] == "base"
    assert strict[0]["voice_source"] == "base"
    assert new_calls == []


def test_callback_arguments_validate_before_markup_planning_and_callback_errors_propagate():
    def legacy(binding: dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, Any]:
        return settings

    def slot_callback(
        binding: dict[str, Any] | None,
        settings: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        return settings

    with pytest.raises(TypeError, match="only one"):
        build_render_parts(
            "Text",
            {},
            speech_xml="not markup",
            apply_binding=legacy,
            apply_binding_at_slot=slot_callback,
        )
    with pytest.raises(TypeError, match="must be callable"):
        build_render_parts(
            "Text",
            {},
            speech_xml="not markup",
            apply_binding_at_slot=cast(SlotBindingCallback, object()),
        )
    def return_non_dict(
        binding: dict[str, Any] | None,
        settings: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        return cast(dict[str, Any], None)

    with pytest.raises(TypeError, match="settings dictionary"):
        build_render_parts(
            "Text",
            {"casting_enabled": True},
            controls=_controls(),
            apply_binding_at_slot=return_non_dict,
        )

    error = LookupError("slot adapter failed")

    def fail(
        binding: dict[str, Any] | None,
        settings: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        raise error

    with pytest.raises(LookupError) as raised:
        build_render_parts(
            "Text",
            {"casting_enabled": True},
            controls=_controls(),
            source_speaker="S1",
            apply_binding_at_slot=fail,
        )
    assert raised.value is error


def test_narrator_nested_in_dialogue_gets_narrator_slot_not_source_or_category():
    seen: list[tuple[dict[str, Any] | None, str | None]] = []

    def apply_at_slot(
        binding: dict[str, Any] | None,
        prepared: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        seen.append((binding, slot_key))
        assert binding is not None
        prepared["voice"] = binding["voice"]
        return prepared

    parts = build_render_parts(
        "Aside",
        {"casting_enabled": True, "performance_enabled": False},
        speech_xml=(
            '<segment id="s"><dialogue>'
            '<narrator>Aside</narrator></dialogue></segment>'
        ),
        segment_id="s",
        controls=_controls(),
        source_speaker="S1",
        apply_binding_at_slot=apply_at_slot,
    )

    assert seen == [(_binding("narrator"), "cast:narrator")]
    assert parts[0]["settings"]["voice"] == "narrator"
    assert parts[0]["voice_source"] == "narrator"
    assert parts[0]["speaker_ids"] == []


@pytest.mark.parametrize(
    ("segment_id", "expected_slot"),
    [
        ("00000000-0000-0000-0000-000000000001", "segment:00000000-0000-0000-0000-000000000001"),
        ("preview-segment", None),
    ],
)
def test_explicit_segment_binding_has_priority_and_only_canonical_ids_get_a_slot(
    segment_id: str, expected_slot: str | None
):
    seen: list[tuple[dict[str, Any] | None, str | None]] = []

    def apply_at_slot(
        binding: dict[str, Any] | None,
        prepared: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        seen.append((binding, slot_key))
        assert binding is not None
        prepared["voice"] = binding["voice"]
        return prepared

    parts = build_render_parts(
        "Hello",
        {"casting_enabled": True, "performance_enabled": False},
        speech_xml=(
            f'<segment id="{segment_id}"><speaker ref="c-one">Hello</speaker></segment>'
        ),
        segment_id=segment_id,
        controls=_controls(),
        voice_binding={"voice": "segment-voice"},
        apply_binding_at_slot=apply_at_slot,
    )

    assert seen == [({"voice": "segment-voice"}, expected_slot)]
    assert parts[0]["settings"]["voice"] == "segment-voice"
    assert parts[0]["voice_source"] == "segment"


def _two_character_markup() -> str:
    return (
        '<segment id="s"><dialogue>'
        '<speaker ref="c-one">A<event kind="pause" duration_ms="300" '
        'affinity="previous"/></speaker>'
        '<speaker ref="c-two">B</speaker>'
        '</dialogue></segment>'
    )


def _retained_pin(content_hash: str, artifact_id: str | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "artifact_id": artifact_id or str(uuid4()),
        "content_hash": content_hash,
    }


def _build_with_pins(
    pin_by_slot: dict[str, dict[str, object] | None],
    *,
    selector_hash_by_slot: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    def apply_at_slot(
        binding: dict[str, Any] | None,
        prepared: dict[str, Any],
        slot_key: str | None,
    ) -> dict[str, Any]:
        assert binding is not None
        assert slot_key is not None
        prepared["voice"] = binding["voice"]
        if slot_key in pin_by_slot:
            prepared[PROJECT_SPEECH_INPUT_KEY] = pin_by_slot[slot_key]
        if selector_hash_by_slot is not None and slot_key in selector_hash_by_slot:
            prepared[PROJECT_SPEECH_SELECTOR_KEY] = selector_hash_by_slot[slot_key]
        return prepared

    return build_render_parts(
        "AB",
        {"casting_enabled": True, "performance_enabled": True},
        speech_xml=_two_character_markup(),
        segment_id="s",
        controls=_controls(),
        apply_binding_at_slot=apply_at_slot,
    )


def test_different_retained_content_keeps_equal_voice_slots_in_separate_groups():
    one = _slot("characters", "c-one")
    two = _slot("characters", "c-two")

    parts = _build_with_pins(
        {
            one: _retained_pin("a" * 64),
            two: _retained_pin("b" * 64),
        }
    )

    assert [part["text"] for part in parts] == ["A", "B"]
    assert [(part["start"], part["end"]) for part in parts] == [(0, 1), (1, 2)]
    assert [part["speaker_ids"] for part in parts] == [["c-one"], ["c-two"]]
    assert [part["speaker_labels"] for part in parts] == [["One"], ["Two"]]
    assert parts[0]["settings"]["_performance"]["events"] == [
        {
            "kind": "pause",
            "anchor": None,
            "position": "after",
            "duration_ms": 300,
        }
    ]
    assert parts[1]["settings"]["_performance"]["events"] == []


def test_same_retained_content_ignores_artifact_and_selector_ids_for_grouping():
    one = _slot("characters", "c-one")
    two = _slot("characters", "c-two")
    content_hash = "a" * 64

    parts = _build_with_pins(
        {
            one: _retained_pin(content_hash, str(uuid4())),
            two: _retained_pin(content_hash, str(uuid4())),
        },
        selector_hash_by_slot={one: "selector-one", two: "selector-two"},
    )

    assert len(parts) == 1
    assert parts[0]["text"] == "AB"
    assert (parts[0]["start"], parts[0]["end"]) == (0, 2)
    assert parts[0]["speaker_ids"] == ["c-one", "c-two"]
    assert parts[0]["speaker_labels"] == ["One", "Two"]
    assert parts[0]["settings"]["_performance"]["events"] == [
        {
            "kind": "pause",
            "anchor": {"quote": "A", "occurrence": 1},
            "position": "after",
            "duration_ms": 300,
        }
    ]


def test_explicit_null_retained_input_does_not_group_with_absent_pin():
    two = _slot("characters", "c-two")

    parts = _build_with_pins({two: None})

    assert [part["text"] for part in parts] == ["A", "B"]
    assert PROJECT_SPEECH_INPUT_KEY not in parts[0]["settings"]
    assert parts[1]["settings"][PROJECT_SPEECH_INPUT_KEY] is None


def test_malformed_retained_pin_is_rejected_before_render_parts_are_returned():
    one = _slot("characters", "c-one")

    with pytest.raises(SpeechInputError, match="Invalid speech input pin"):
        _build_with_pins({one: {"version": 1, "artifact_id": "not-a-uuid", "content_hash": "x"}})
