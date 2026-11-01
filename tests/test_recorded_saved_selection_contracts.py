"""Finite saved recording selections stay closed across projection and remapping."""

from __future__ import annotations

import builtins
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from pandrator.web.generation_cast_runtime import cast_resolution_key
from pandrator.web.generation_control_schemas import CastSettings
from pandrator.web.project_speech_references import (
    speech_reference_selector,
    speech_reference_selector_hash,
)
from pandrator.web.recorded_speech_contracts import (
    project_recorded_snapshot,
    recorded_input_pins,
    recorded_provenance_version,
    remap_recorded_snapshot,
    validate_recorded_snapshot,
    validate_speech_provenance,
)
from tests.test_project_audio_copy import (
    BINDING,
    MODEL,
    PIN,
    SELECTOR,
    SOURCE,
    TEXT,
    fallback_source,
    source_snapshot,
    uid,
)


def segment_source(*, null=False, managed=True, legacy_mode=False):
    source = source_snapshot()
    frozen = source["generation_control_snapshot"]
    selector = speech_reference_selector(
        {"voice_id": uid(3)} if managed else {"voice": "saved-alias", "model": MODEL, "service": "audio_cpp"}
    )
    frozen["segments"][uid(5)]["voice_binding"] = selector
    binding = {"adapter": "audio_cpp", "model": MODEL, "voice": "saved-alias", "display_name": "Saved"}
    pin = None if null else {"version": 1, "artifact_id": uid(24), "content_hash": "3" * 64}
    resolution = {
        **selector, "voice": binding["voice"], PIN: pin, BINDING: binding,
        SELECTOR: speech_reference_selector_hash(selector),
        "_recast_reference_fingerprint": [] if pin is None else [pin["content_hash"]],
    }
    if pin is not None:
        resolution[SOURCE] = {
            "version": 1, "pin": deepcopy(pin), "binding": deepcopy(binding),
            "source_voice_reference_hash": "4" * 64,
        }
    frozen["resolved_bindings"][cast_resolution_key(selector, f"segment:{uid(5)}", schema_version=2)] = resolution
    # Unused private base evidence must disappear before canonical validation.
    frozen["controls"]["_base_voice_settings"] = {"unrecognized_private_input": "unused"}
    if legacy_mode:
        source["tts"].update(voice_mode_version=0, casting_enabled=False)
    return source


def project(source, *, segment_id=None):
    return project_recorded_snapshot(source, entry_id=uid(5), segment_id=segment_id or uid(105),
                                     revision_id=uid(104), text=TEXT, allow_saved_selections=True)


def validate(value, **kwargs):
    return validate_recorded_snapshot(value, segment_id=uid(105), revision_id=uid(104),
                                      text=TEXT, allow_saved_selections=True, **kwargs)


@pytest.mark.parametrize("null", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("legacy_mode", [False, True])
def test_segment_projection_maps_source_slots_prunes_unused_inputs_and_preserves_identity(null, managed, legacy_mode):
    source = segment_source(null=null, managed=managed, legacy_mode=legacy_mode)
    before = deepcopy(source)
    selected = project(source)
    assert source == before
    frozen = selected["generation_control_snapshot"]
    entry = frozen["segments"][uid(105)]
    selector = entry["voice_binding"]
    key = cast_resolution_key(selector, f"segment:{uid(105)}", schema_version=2)
    assert set(frozen["resolved_bindings"]) == {key}
    assert uid(105) in entry["speech_xml"] and uid(5) not in entry["speech_xml"]
    assert [character["id"] for character in frozen["controls"]["characters"]] == ["fred", "wilma"]
    assert frozen["controls"]["cast"] == CastSettings().model_dump(mode="json")
    assert "_base_voice_settings" not in frozen["controls"]
    assert not {PIN, BINDING, SELECTOR, SOURCE} & selected["tts"].keys()
    assert selected["tts"]["casting_enabled"] is True and selected["tts"]["voice_mode_version"] == 1
    assert recorded_provenance_version(selected) == 2
    assert validate(selected) == selected
    pin = frozen["resolved_bindings"][key][PIN]
    assert recorded_input_pins(selected, allow_saved_selections=True) == ([] if null else [pin])
    selected_before = deepcopy(selected)
    mapped = remap_recorded_snapshot(selected, {} if null else {uid(24): uid(124)},
        segment_id=uid(205), revision_id=uid(204), text=TEXT, allow_saved_selections=True)
    assert selected == selected_before
    mapped_frozen = mapped["generation_control_snapshot"]
    mapped_key = cast_resolution_key(selector, f"segment:{uid(205)}", schema_version=2)
    assert set(mapped_frozen["resolved_bindings"]) == {mapped_key}
    expected = deepcopy(frozen["resolved_bindings"][key])
    if not null:
        expected[PIN]["artifact_id"] = uid(124)
        expected[SOURCE]["pin"]["artifact_id"] = uid(124)
    assert mapped_frozen["resolved_bindings"][mapped_key] == expected
    assert mapped_frozen["segments"][uid(205)]["voice_binding"] == selector
    assert uid(205) in mapped_frozen["segments"][uid(205)]["speech_xml"]


@pytest.mark.parametrize("kind", ["plain", "base", "narrator", "cast", "segment"])
def test_explicit_null_forms_are_opt_in_and_produce_no_pin(kind):
    source: dict[str, Any]
    if kind == "segment":
        source = segment_source(null=True)
    elif kind == "cast":
        source = source_snapshot()
        for resolution in source["generation_control_snapshot"]["resolved_bindings"].values():
            resolution[PIN] = None
            resolution.pop(SOURCE)
            resolution["_recast_reference_fingerprint"] = []
    elif kind == "narrator":
        source = source_snapshot()
        frozen = source["generation_control_snapshot"]
        entry = frozen["segments"][uid(5)]
        entry["speech_xml"] = None
        selector = speech_reference_selector({"voice_id": uid(1)})
        resolution = next(iter(frozen["resolved_bindings"].values()))
        resolution[PIN] = None
        resolution.pop(SOURCE)
        resolution["_recast_reference_fingerprint"] = []
        frozen["controls"]["cast"] = {"narrator": selector}
        frozen["resolved_bindings"] = {cast_resolution_key(selector, "cast:narrator", schema_version=2): resolution}
    else:
        source = fallback_source()
        base = source["generation_control_snapshot"]["controls"]["_base_voice_settings"]
        base[PIN] = None
        base[SOURCE] = None
        if kind == "plain":
            source["tts"].update({key: value for key, value in base.items()
                                  if key in {"voice", "speaker", "voice_id", PIN, BINDING, SELECTOR, SOURCE}})
            source["generation_control_snapshot"] = None
    selected = project(source)
    assert recorded_provenance_version(selected) == 2
    assert recorded_input_pins(selected, allow_saved_selections=True) == []
    assert validate(selected) == selected
    assert remap_recorded_snapshot(selected, {}, segment_id=uid(205), revision_id=uid(204),
        text=TEXT, allow_saved_selections=True)
    with pytest.raises(ValueError):
        project_recorded_snapshot(source, entry_id=uid(5), segment_id=uid(105), revision_id=uid(104), text=TEXT)
    with pytest.raises(ValueError):
        validate_recorded_snapshot(selected, segment_id=uid(105), revision_id=uid(104), text=TEXT)
    with pytest.raises(ValueError):
        recorded_input_pins(selected)
    with pytest.raises(ValueError):
        remap_recorded_snapshot(selected, {}, segment_id=uid(205), revision_id=uid(204), text=TEXT)


def test_strict_single_voice_inactive_override_is_rejected():
    source = segment_source()
    source["tts"].update(voice_mode_version=1, casting_enabled=False)
    with pytest.raises(ValueError):
        project(source)


@pytest.mark.parametrize("defect", ["selector", "hash", "model", "voice", "half_null", "null_source",
                                    "fingerprint", "extra_resolution", "foreign_slot", "old_slot", "binding", "inactive"])
def test_expanded_validator_rejects_targeted_invalid_evidence(defect):
    selected = project(segment_source(null=True))
    frozen = selected["generation_control_snapshot"]
    entry = frozen["segments"][uid(105)]
    key, resolution = next(iter(frozen["resolved_bindings"].items()))
    if defect == "selector":
        resolution[SELECTOR] = "f" * 64
    elif defect == "hash":
        resolution[PIN] = {"version": 1, "artifact_id": uid(24), "content_hash": "bad"}
    elif defect == "model":
        resolution[BINDING]["model"] = "different-model"
    elif defect == "voice":
        resolution[BINDING]["voice"] = "different-alias"
    elif defect == "half_null":
        resolution.pop(BINDING)
    elif defect == "null_source":
        resolution[SOURCE] = None
    elif defect == "fingerprint":
        resolution["_recast_reference_fingerprint"] = ["3" * 64]
    elif defect == "extra_resolution":
        frozen["resolved_bindings"]["unexpected"] = deepcopy(resolution)
    elif defect in {"foreign_slot", "old_slot"}:
        frozen["resolved_bindings"] = {cast_resolution_key(entry["voice_binding"],
            f"segment:{uid(99) if defect == 'foreign_slot' else uid(5)}", schema_version=2): resolution}
    elif defect == "binding":
        entry["voice_binding"].pop("voice_description")
    elif defect == "inactive":
        selected["tts"]["casting_enabled"] = False
    assert key
    with pytest.raises(ValueError):
        validate(selected)


@pytest.mark.parametrize("defect", ["missing", "target_in_source", "foreign_slot", "selector"])
def test_projection_requires_resolution_in_source_coordinates(defect):
    source = segment_source()
    frozen = source["generation_control_snapshot"]
    selector = frozen["segments"][uid(5)]["voice_binding"]
    key = cast_resolution_key(selector, f"segment:{uid(5)}", schema_version=2)
    resolution = frozen["resolved_bindings"].pop(key)
    if defect == "selector":
        resolution[SELECTOR] = "f" * 64
        frozen["resolved_bindings"][key] = resolution
    elif defect != "missing":
        frozen["resolved_bindings"][cast_resolution_key(selector,
            f"segment:{uid(105) if defect == 'target_in_source' else uid(99)}", schema_version=2)] = resolution
    with pytest.raises(ValueError):
        project(source)


def test_missing_identity_and_invalid_source_fail_before_remap():
    selected = project(segment_source())
    before = deepcopy(selected)
    with pytest.raises(ValueError):
        remap_recorded_snapshot(selected, {}, segment_id=uid(205), revision_id=uid(204), text=TEXT,
                                allow_saved_selections=True)
    assert selected == before
    selected["generation_control_snapshot"]["resolved_bindings"]["extra"] = {}
    with pytest.raises(ValueError):
        remap_recorded_snapshot(selected, {uid(24): uid(124)}, segment_id=uid(205), revision_id=uid(204),
                                text=TEXT, allow_saved_selections=True)


def test_null_projection_drops_only_an_empty_source_proof():
    source = segment_source(null=True)
    selector = source["generation_control_snapshot"]["segments"][uid(5)]["voice_binding"]
    key = cast_resolution_key(selector, f"segment:{uid(5)}", schema_version=2)
    resolution = source["generation_control_snapshot"]["resolved_bindings"][key]
    resolution[SOURCE] = None
    selected = project(source)
    assert SOURCE not in next(iter(selected["generation_control_snapshot"]["resolved_bindings"].values()))
    resolution[SOURCE] = {"version": 1, "pin": None, "binding": deepcopy(resolution[BINDING]),
                          "source_voice_reference_hash": "4" * 64}
    with pytest.raises(ValueError):
        project(source)


def test_pin_collection_deduplicates_real_inputs_and_rejects_conflicting_hashes():
    selected = project(source_snapshot())
    resolutions = list(selected["generation_control_snapshot"]["resolved_bindings"].values())
    resolutions[1][PIN] = deepcopy(resolutions[0][PIN])
    resolutions[1][SOURCE]["pin"] = deepcopy(resolutions[0][PIN])
    resolutions[1]["_recast_reference_fingerprint"] = deepcopy(resolutions[0]["_recast_reference_fingerprint"])
    assert validate(selected) == selected
    assert recorded_input_pins(selected, allow_saved_selections=True) == [resolutions[0][PIN]]
    resolutions[1][PIN]["content_hash"] = "f" * 64
    resolutions[1][SOURCE]["pin"]["content_hash"] = "f" * 64
    resolutions[1]["_recast_reference_fingerprint"] = ["f" * 64]
    with pytest.raises(ValueError):
        recorded_input_pins(selected, allow_saved_selections=True)


@pytest.mark.parametrize("source", [source_snapshot, fallback_source])
def test_legacy_outputs_and_remap_are_unchanged_when_opted_in(source):
    raw = source()
    kwargs: dict[str, Any] = {"entry_id": uid(5), "segment_id": uid(105), "revision_id": uid(104), "text": TEXT}
    legacy = project_recorded_snapshot(raw, **kwargs)
    assert project_recorded_snapshot(raw, **kwargs, allow_saved_selections=True) == legacy
    assert recorded_provenance_version(legacy) == 1
    assert recorded_input_pins(legacy, allow_saved_selections=True) == recorded_input_pins(legacy)
    identities = {pin["artifact_id"]: uid(200 + index) for index, pin in enumerate(recorded_input_pins(legacy))}
    kwargs = {"segment_id": uid(205), "revision_id": uid(204), "text": TEXT}
    assert remap_recorded_snapshot(legacy, identities, **kwargs, allow_saved_selections=True) == remap_recorded_snapshot(legacy, identities, **kwargs)


@pytest.mark.parametrize("defect", ["binding", "selector", "source", "model", "fingerprint"])
def test_pin_collection_checks_complete_explicit_null_evidence(defect):
    selected = project(segment_source(null=True))
    resolution = next(iter(selected["generation_control_snapshot"]["resolved_bindings"].values()))
    if defect == "source":
        resolution[SOURCE] = None
    elif defect == "model":
        resolution[BINDING]["model"] = "wrong"
    elif defect == "fingerprint":
        resolution["_recast_reference_fingerprint"] = ["1" * 64]
    else:
        resolution.pop(BINDING if defect == "binding" else SELECTOR)
    with pytest.raises(ValueError):
        recorded_input_pins(selected, allow_saved_selections=True)


@pytest.mark.parametrize("version", [1, 2, True, 0, 3, "2"])
def test_envelope_version_gate_is_explicit_and_strict(version):
    value = {"version": version, "segments": {uid(105): project(segment_source())}}
    if type(version) is int and version in {1, 2}:
        assert validate_speech_provenance(value, allow_saved_selections=True) == value
    else:
        with pytest.raises(ValueError):
            validate_speech_provenance(value, allow_saved_selections=True)
    if type(version) is int and version == 1:
        assert validate_speech_provenance(value) == value
    else:
        with pytest.raises(ValueError):
            validate_speech_provenance(value)


@pytest.mark.parametrize("defect", ["empty", "outer", "segment", "shape", "nonfinite"])
def test_envelope_keeps_closed_finite_shapes(defect):
    value = {"version": 2, "segments": {uid(105): project(segment_source())}}
    if defect == "empty":
        value["segments"] = {}
    elif defect == "outer":
        value["unexpected"] = True
    elif defect == "segment":
        value["segments"]["not-a-uuid"] = value["segments"].pop(uid(105))
    elif defect == "shape":
        value["segments"][uid(105)]["extra"] = None
    else:
        value["segments"][uid(105)]["tts"]["voice"] = float("nan")
    with pytest.raises(ValueError):
        validate_speech_provenance(value, allow_saved_selections=True)


def test_codecs_need_no_files_or_network(monkeypatch):
    source = segment_source(null=True)

    def forbidden(*args, **kwargs):
        pytest.fail("Pure recorded selection codec attempted file or network access")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        selected = project(source)
        assert validate(selected) == selected
        assert recorded_input_pins(selected, allow_saved_selections=True) == []
        assert recorded_provenance_version(selected) == 2
        mapped = remap_recorded_snapshot(selected, {}, segment_id=uid(205), revision_id=uid(204),
                                        text=TEXT, allow_saved_selections=True)
        assert validate_speech_provenance({"version": 2, "segments": {uid(205): mapped}},
                                          allow_saved_selections=True)
