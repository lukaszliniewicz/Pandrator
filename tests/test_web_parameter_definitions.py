"""Focused contract tests for the parameter explanation registry."""

import json

import pytest

from pandrator.web.parameter_definitions import (
    DOCUMENTED_SECTIONS,
    PARAMETER_DEFINITIONS,
    describe_parameters,
)
from pandrator.web.workspace import BUILTIN_DEFAULTS, SETTING_SECTIONS


def _items(**filters):
    return describe_parameters(limit=300, **filters)["items"]


def test_registry_covers_every_default_in_every_setting_section():
    assert set(PARAMETER_DEFINITIONS) == set(DOCUMENTED_SECTIONS)
    assert DOCUMENTED_SECTIONS == SETTING_SECTIONS
    for section in DOCUMENTED_SECTIONS:
        assert list(PARAMETER_DEFINITIONS[section]) == list(BUILTIN_DEFAULTS[section])
        for name, definition in PARAMETER_DEFINITIONS[section].items():
            assert definition["section"] == section
            assert definition["name"] == name
            assert definition["label"]
            assert definition["description"]
            assert definition["default"] == BUILTIN_DEFAULTS[section][name]
            assert definition["value_type"] in {
                "boolean",
                "integer",
                "number",
                "string",
                "object",
                "array",
            }


def test_key_semantics_and_known_caveats_are_explained():
    moss_vad = PARAMETER_DEFINITIONS["stt"]["moss_vad_enabled"]
    assert "MOSS" in moss_vad["description"]
    assert "crispasr_vad_enabled" in moss_vad["description"]
    assert (
        "Azure" in PARAMETER_DEFINITIONS["stt"]["stt_transcribe_style"]["applicability"]
    )
    assert "forward" in PARAMETER_DEFINITIONS["tts"]["xtts_send_top_p"]["description"]
    assert "XTTS" in PARAMETER_DEFINITIONS["tts"]["top_p"]["applicability"]
    assert (
        "speech-block"
        in PARAMETER_DEFINITIONS["tts"]["speech_block_max_chars"]["applicability"]
    )
    assert "4x" in PARAMETER_DEFINITIONS["audio"]["synchronization_speed"]["caveat"]
    assert (
        "current consumer"
        in PARAMETER_DEFINITIONS["subtitles"]["boundary_correction_enabled"][
            "caveat"
        ].lower()
    )
    assert (
        "current consumer"
        in PARAMETER_DEFINITIONS["tts"]["kokoro_default_voices"]["caveat"].lower()
    )
    assert PARAMETER_DEFINITIONS["tts"]["speed"]["unit"] == "provider-defined"
    assert "destructive" in PARAMETER_DEFINITIONS["text"]["remove_diacritics"]["caveat"]
    assert PARAMETER_DEFINITIONS["correction"]["timing_context_mode"]["choices"] == [
        "full",
        "overlap_only",
        "none",
    ]
    assert "DeepL" in PARAMETER_DEFINITIONS["translation"]["backend"]["description"]
    assert PARAMETER_DEFINITIONS["source_cleaning"]["pdf_ocr_dpi"]["maximum"] == 400


def test_items_follow_setting_section_and_default_insertion_order():
    names = [
        f"{item['section']}.{item['name']}"
        for item in _items(
            names=[
                name
                for section in DOCUMENTED_SECTIONS
                for name in BUILTIN_DEFAULTS[section]
            ]
        )
    ]
    expected = [
        f"{section}.{name}"
        for section in SETTING_SECTIONS
        if section in DOCUMENTED_SECTIONS
        for name in BUILTIN_DEFAULTS[section]
    ]
    assert names == expected


def test_each_filter_and_intersection_restricts_results():
    assert {item["section"] for item in _items(sections=["stt"])} == {"stt"}
    assert {item["name"] for item in _items(names=["language"])} == {"language"}
    assert _items(names=["language"], sections=["tts"])[0]["section"] == "tts"
    assert _items(sections=["stt"], workflow_kind="audiobook") == []
    assert {item["section"] for item in _items(workflow_kind="subtitles")} == {
        "stt",
        "subtitles",
        "correction",
        "translation",
        "output",
    }
    assert all(
        item["section"] == "tts"
        for item in _items(sections=["tts"], query="provider-only")
    )


def test_duplicate_names_are_returned_once_per_section():
    items = _items(names=["language"])
    assert [(item["section"], item["name"]) for item in items] == [
        ("tts", "language"),
        ("output", "language"),
    ]


def test_query_is_case_insensitive_and_searches_metadata():
    assert _items(query="mOsS")
    assert _items(query="CURRENT CONSUMER") == [
        PARAMETER_DEFINITIONS["subtitles"]["boundary_correction_enabled"],
        PARAMETER_DEFINITIONS["tts"]["kokoro_default_voices"],
    ]


def test_blank_filters_do_not_count_as_an_actual_filter():
    with pytest.raises(ValueError):
        describe_parameters(
            sections=[" ", "\t"], names=[""], workflow_kind=" ", query=" "
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sections": ["not-a-section"]},
        {"workflow_kind": "documentary"},
        {"limit": 0},
        {"limit": 301},
        {"limit": True},
        {"query": 12},
    ],
)
def test_invalid_filters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        describe_parameters(**kwargs)


def test_truncation_and_counts_are_reported_before_limiting():
    result = describe_parameters(sections=["tts"], limit=2)
    assert result["matched_count"] == len(BUILTIN_DEFAULTS["tts"])
    assert result["returned_count"] == 2
    assert len(result["items"]) == 2
    assert result["truncated"] is True
    assert result["available_sections"] == list(DOCUMENTED_SECTIONS)


def test_results_are_defensive_copies_and_json_serializable():
    result = describe_parameters(names=["kokoro_default_voices"])
    result["items"][0]["default"]["en"] = "mutated"
    result["available_sections"].append("mutated")
    fresh = describe_parameters(names=["kokoro_default_voices"])
    assert fresh["items"][0]["default"] == {"en": "af_heart"}
    assert "mutated" not in fresh["available_sections"]
    json.dumps(fresh)
