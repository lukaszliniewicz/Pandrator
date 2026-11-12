"""Private identity catalogue reuse preserves public planning isolation."""

from copy import deepcopy
from unittest.mock import patch

from pandrator.web import generation_audio_identity
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.generation_rendering import build_render_parts


def test_identity_catalogue_copies_are_owned_once_per_source_and_keep_other_fields_detached():
    context = object.__new__(AudioIdentityContext)
    context._inspection_catalogue_copies = {}
    context._inspection_catalogue_sources = {}
    providers = [{"id": "test", "models": [{"id": "model", "options": ["one"]}]}]
    services = {"nested": {"voices": ["narrator"]}}
    distinct_providers = deepcopy(providers)
    settings = {
        "provider_configs": providers,
        "service_configs": services,
        "mutable": {"values": ["original"]},
    }
    expected = deepcopy(settings)
    source_copies = []
    original_deepcopy = generation_audio_identity.deepcopy

    def counted_copy(value, memo=None):
        if any(value is source for source in (providers, services, distinct_providers)):
            source_copies.append(id(value))
        return original_deepcopy(value, memo)

    with patch.object(generation_audio_identity, "deepcopy", side_effect=counted_copy):
        first = context._copy_inspection_settings(settings)
        second = context._copy_inspection_settings(first)
        third = context._copy_inspection_settings(settings)
        distinct = context._copy_inspection_settings(
            {**settings, "provider_configs": distinct_providers}
        )

    assert source_copies == [id(providers), id(services), id(distinct_providers)]
    assert first == second == third == distinct == expected
    assert first["provider_configs"] is second["provider_configs"] is third["provider_configs"]
    assert first["service_configs"] is second["service_configs"] is third["service_configs"]
    assert first["provider_configs"] is not providers
    assert first["provider_configs"][0]["models"] is not providers[0]["models"]
    assert first["service_configs"]["nested"] is not services["nested"]
    assert distinct["provider_configs"] is not first["provider_configs"]
    assert context._inspection_catalogue_sources[id(providers)] is providers
    assert context._inspection_catalogue_sources[id(distinct_providers)] is distinct_providers
    first["mutable"]["values"].append("changed")
    assert second["mutable"]["values"] == ["original"]
    assert third["mutable"]["values"] == ["original"]
    assert settings == expected


def test_default_public_planner_keeps_nested_catalogues_and_parts_independent():
    settings = {
        "casting_enabled": True,
        "performance_enabled": False,
        "provider_configs": [{"id": "test", "options": {"values": ["original"]}}],
        "service_configs": [{"id": "other", "voices": ["narrator"]}],
        "mutable": {"values": ["original"]},
    }
    expected = deepcopy(settings)
    parts = build_render_parts(
        "AB",
        settings,
        speech_xml='<segment id="s"><speaker ref="one">A</speaker><speaker ref="two">B</speaker></segment>',
        segment_id="s",
        controls={
            "characters": [
                {"id": "one", "display_name": "One"},
                {"id": "two", "display_name": "Two"},
            ],
            "cast": {"characters": {"one": {"voice": "one"}, "two": {"voice": "two"}}},
        },
    )
    assert len(parts) == 2
    first, second = [part["settings"] for part in parts]
    first["provider_configs"][0]["options"]["values"].append("changed")
    first["service_configs"][0]["voices"].append("changed")
    first["mutable"]["values"].append("changed")
    for key in ("provider_configs", "service_configs", "mutable"):
        assert second[key] == expected[key]
    assert settings == expected


def _settings_with_aliases():
    shared = {"values": ["one", ("two", [3])]}
    return {
        "name": "voice",
        "count": 3,
        "enabled": True,
        "ratio": 0.5,
        "nothing": None,
        "provider_configs": [{"id": "test", "models": [{"id": "model"}]}],
        "service_configs": {"nested": {"voices": ["narrator"]}},
        "first": shared,
        "second": shared,
        "pair": ("a", ["b"]),
    }


def test_inspection_copy_equals_a_deep_copy_and_keeps_aliasing_and_independence():
    context = object.__new__(AudioIdentityContext)
    context._inspection_catalogue_copies = {}
    context._inspection_catalogue_sources = {}
    settings = _settings_with_aliases()
    expected = deepcopy(settings)
    copied = context._copy_inspection_settings(settings)
    assert copied == expected == settings
    assert type(copied) is dict
    # Aliasing inside the settings is kept, as deepcopy keeps it.
    assert copied["first"] is copied["second"] and copied["first"] is not settings["first"]
    assert copied["pair"] is not settings["pair"] and copied["pair"][1] is not settings["pair"][1]
    copied["first"]["values"].append("changed")
    copied["pair"][1].append("changed")
    again = context._copy_inspection_settings(settings)
    assert settings == expected and again == expected
    assert again["first"] is not copied["first"]


def test_copy_record_equals_deepcopy_without_sharing_containers():
    from collections import OrderedDict

    from pandrator.logic.speech_performance import copy_record

    record = _settings_with_aliases()
    expected = deepcopy(record)
    copied = copy_record(record)
    assert copied == expected and copied is not record
    assert copied["first"] is copied["second"] and copied["first"] is not record["first"]
    copied["provider_configs"][0]["id"] = "changed"
    assert record == expected
    ordered = OrderedDict(record)
    assert type(copy_record(ordered)) is OrderedDict
    assert copy_record(None) is None


def test_copy_record_keeps_a_record_that_contains_itself():
    from pandrator.logic.speech_performance import copy_record

    record: dict = {"name": "loop", "nested": {}}
    record["self"] = record
    record["nested"]["back"] = record
    copied = copy_record(record)
    assert copied is not record
    assert copied["self"] is copied
    assert copied["nested"]["back"] is copied


def test_copy_record_copies_a_mutable_key_with_its_aliases():
    from pandrator.logic.speech_performance import copy_record

    class Key:
        def __init__(self) -> None:
            self.items: list[str] = []

    key = Key()
    record = {key: key, "alias": key}
    copied = copy_record(record)
    (copied_key,) = [item for item in copied if item != "alias"]
    assert copied_key is not key
    assert copied[copied_key] is copied_key
    assert copied["alias"] is copied_key
    copied_key.items.append("changed")
    assert key.items == []
