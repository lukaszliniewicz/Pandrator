"""Pure copy contracts for saved speech choices attached to current segments."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest

from pandrator.web.generation_control_schemas import VoiceBinding
from pandrator.web.project_speech_references import (
    segment_speech_reference_identity,
    speech_reference_selector_hash,
)
from pandrator.web.projects.copy_speech import (
    freeze_segment_reference,
    remap_segment_reference,
    segment_copy_references,
    validate_speech_copy_graph,
)
from pandrator.web.projects.copy_speech_plan import (
    SPEECH_FAMILIES,
    freeze_speech_plan_rows,
    remap_speech_plan_rows,
    speech_plan_signature,
    validate_speech_plan_graph,
)
from pandrator.web.settings_policy import stable_hash
from pandrator.web.speech_input_contracts import SpeechInputOwner
from pandrator.web.speech_plan_context import SIGNATURE_FIELDS
from tests.test_project_speech_plan_copy import raw_graph, uid
from tests.test_retained_preview_workspace import MODEL

_SEGMENT_ID = uid(5)
_SESSION_ID = uid(2)
_PROJECT_ID = uid(1)
_WORKSPACE_ID = uid(10)
_PIN_ID = uid(12)
_AUDIO_ID = uid(13)
_PIN_HASH = "a" * 64


def _reference_row(*, pinned: bool) -> dict[str, Any]:
    binding = VoiceBinding(voice="Saved narrator", service="audio_cpp", model=MODEL)
    selector = binding.model_dump(mode="json")
    return {
        "session_id": _SESSION_ID,
        "slot_key": f"segment:{_SEGMENT_ID}",
        "selector_json": selector,
        "selector_hash": speech_reference_selector_hash(selector),
        "binding_json": {
            "adapter": "audio_cpp",
            "model": MODEL,
            "voice": "Saved narrator",
            "display_name": "Saved narrator",
        },
        "input_artifact_id": _PIN_ID if pinned else None,
        "input_content_hash": _PIN_HASH if pinned else None,
        "revision": 4,
    }


def _rows(*, pinned: bool = False, authored_columns: bool = True) -> dict[str, Any]:
    rows = raw_graph()
    rows["knowledge_ledgers"][0]["source_language"] = "auto"
    rows["knowledge_ledgers"][0]["target_language"] = ""
    segment = rows["generation_segments"][0]
    if authored_columns:
        segment["voice_id"] = "retired-library-row"
        segment["voice"] = "Old authored label"
    rows["session_settings"] = [{
        "session_id": _SESSION_ID,
        "section": "tts",
        "value_json": {"service": "audio_cpp", "model": MODEL, "voice": "unused"},
    }]
    rows["project_speech_references"] = [_reference_row(pinned=pinned)]
    rows["artifact_edges"] = []
    if pinned:
        owner = {
            "workspace_id": _WORKSPACE_ID,
            "project_id": _PROJECT_ID,
            "session_id": _SESSION_ID,
        }
        rows["artifacts"].extend([
            {
                "id": _PIN_ID,
                "state": "current",
                "kind": "speech_voice_snapshot",
                "role": "speech_input",
                "mime_type": "application/json",
                "size_bytes": 16,
                "content_hash": _PIN_HASH,
                **owner,
            },
            {
                "id": _AUDIO_ID,
                "state": "current",
                "kind": "audio",
                "role": "speech_reference_audio",
                "mime_type": "audio/wav",
                "size_bytes": 32,
                "content_hash": "b" * 64,
                **owner,
            },
        ])
        rows["artifact_edges"].append({
            "parent_artifact_id": _AUDIO_ID,
            "child_artifact_id": _PIN_ID,
            "relation": "speech_reference",
        })
    references = segment_copy_references(rows)
    rows["speech_plan_reviews"][0]["content_hash"] = speech_plan_signature(
        [segment], references=references
    )
    return rows


def _settings(rows: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        uid(4): deepcopy(rows["generation_plan_revisions"][0]["settings_json"]),
    }


@pytest.mark.parametrize("pinned", [False, True])
def test_current_segment_choice_freezes_as_saved_reference_and_clears_live_voice(pinned):
    rows = _rows(pinned=pinned)
    before = deepcopy(rows)
    references = segment_copy_references(rows)

    assert set(references) == {_SEGMENT_ID}
    stored = references[_SEGMENT_ID]
    assert stored.slot_key == f"segment:{_SEGMENT_ID}"
    assert stored.owner == SpeechInputOwner(_WORKSPACE_ID, _PROJECT_ID, _SESSION_ID)
    assert (stored.pin is not None) is pinned
    assert rows == before

    validate_speech_copy_graph(
        rows,
        allow_null_inputs=True,
        allow_segment_references=True,
    )
    frozen = freeze_speech_plan_rows(
        rows,
        _settings(rows),
        allow_segment_references=True,
    )
    copied_segment = frozen["generation_segments"][0]
    assert copied_segment["voice"] is None
    assert copied_segment["voice_id"] is None
    assert len(frozen["speech_plan_reviews"]) == 1

    copied_rows = {**rows, **frozen}
    validate_speech_plan_graph(copied_rows, allow_segment_references=True)

    identities = {
        row["id"]: uid(int(UUID(row["id"])) + 100)
        for values in rows.values()
        if type(values) is list
        for row in values
        if type(row) is dict and type(row.get("id")) is str
    }
    remapped = remap_speech_plan_rows(
        copied_rows,
        identities,
        allow_segment_references=True,
    )
    mapped_references = {
        identities[segment_id]: reference
        for segment_id, reference in references.items()
    }
    assert remapped["speech_plan_reviews"][0]["content_hash"] == speech_plan_signature(
        remapped["generation_segments"],
        references=mapped_references,
    )
    assert remapped["speech_plan_reviews"][0]["content_hash"] != frozen["speech_plan_reviews"][0]["content_hash"]


def test_segment_references_are_optional_and_old_no_reference_codec_stays_identical():
    assert segment_copy_references({}) == {}
    assert segment_copy_references({"project_speech_references": []}) == {}

    rows = raw_graph()
    legacy = freeze_speech_plan_rows(rows, _settings(rows))
    opted_in = freeze_speech_plan_rows(
        rows,
        _settings(rows),
        allow_segment_references=True,
    )
    assert legacy == opted_in
    assert speech_plan_signature(rows["generation_segments"]) == speech_plan_signature(
        rows["generation_segments"], references={}
    )
    assert set(legacy) == set(SPEECH_FAMILIES)


def test_segment_references_are_strict_by_default_and_missing_choice_is_not_inferred():
    rows = _rows(pinned=True)
    identities = {
        row["id"]: uid(int(UUID(row["id"])) + 100)
        for values in rows.values()
        if type(values) is list
        for row in values
        if type(row) is dict and type(row.get("id")) is str
    }
    with pytest.raises(ValueError):
        validate_speech_copy_graph(rows)
    with pytest.raises(ValueError):
        freeze_speech_plan_rows(rows, _settings(rows))
    with pytest.raises(ValueError):
        remap_speech_plan_rows(rows, identities)

    rows["project_speech_references"] = []
    with pytest.raises(ValueError):
        freeze_speech_plan_rows(
            rows,
            _settings(rows),
            allow_segment_references=True,
        )


@pytest.mark.parametrize("pinned", [False, True])
def test_segment_reference_remap_changes_owner_slot_and_optional_pin_only(pinned):
    row = _reference_row(pinned=pinned)
    identities = {
        _SESSION_ID: uid(102),
        _SEGMENT_ID: uid(105),
    }
    if pinned:
        identities[_PIN_ID] = uid(112)

    remapped = remap_segment_reference(
        row,
        identities,
        allow_null_inputs=not pinned,
    )
    assert remapped["session_id"] == uid(102)
    assert remapped["slot_key"] == f"segment:{uid(105)}"
    assert remapped["revision"] == 1
    assert remapped["selector_json"] == row["selector_json"]
    assert remapped["selector_hash"] == row["selector_hash"]
    assert remapped["binding_json"] == row["binding_json"]
    assert remapped["input_content_hash"] == row["input_content_hash"]
    assert remapped["input_artifact_id"] == (uid(112) if pinned else None)

    with pytest.raises(ValueError):
        remap_segment_reference(row, {_SESSION_ID: uid(102)}, allow_null_inputs=True)


def test_segment_reference_validation_keeps_complete_cast_closure_in_the_same_session():
    rows = _rows(pinned=True)
    rows["knowledge_ledgers"][0]["payload_json"]["cast"] = {
        "narrator": {"voice": "Saved narrator", "service": "audio_cpp", "model": MODEL},
    }
    cast_reference = deepcopy(rows["project_speech_references"][0])
    cast_reference["slot_key"] = "cast:narrator"
    rows["project_speech_references"].append(cast_reference)

    validate_speech_copy_graph(
        rows,
        allow_null_inputs=True,
        allow_segment_references=True,
    )
    rows["project_speech_references"].pop()
    with pytest.raises(ValueError):
        validate_speech_copy_graph(
            rows,
            allow_null_inputs=True,
            allow_segment_references=True,
        )


def test_saved_reference_signature_matches_runtime_identity_and_survives_identity_remap():
    rows = _rows(pinned=True)
    segment = rows["generation_segments"][0]
    references = segment_copy_references(rows)
    stored = references[_SEGMENT_ID]

    payload = {key: segment[key] for key in SIGNATURE_FIELDS}
    payload["_speech_reference"] = segment_speech_reference_identity(stored)
    payload["voice_id"] = stored.selector.get("voice_id")
    payload["voice"] = stored.selector.get("voice") or None
    actual = speech_plan_signature([segment], references=references)
    assert actual == stable_hash([payload])
    assert actual != speech_plan_signature([segment])

    mapped_segment = {**segment, "id": uid(105)}
    mapped = speech_plan_signature(
        [mapped_segment],
        references={uid(105): stored},
    )
    assert mapped == actual

    stale = deepcopy(rows)
    stale["speech_plan_reviews"][0]["content_hash"] = "stale review"
    frozen = freeze_speech_plan_rows(
        stale,
        _settings(stale),
        allow_segment_references=True,
    )
    assert frozen["speech_plan_reviews"] == []


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "orphan",
        "noncanonical_slot",
        "foreign_session",
        "missing_owner",
        "selector",
        "selector_hash",
        "binding_voice",
        "binding_model",
        "pin_half",
        "pin_hash",
        "artifact_owner",
        "edge",
    ],
)
def test_segment_graph_rejects_invalid_reference_identity_or_retained_proof(defect):
    rows = _rows(pinned=True)
    reference = rows["project_speech_references"][0]
    if defect == "duplicate":
        rows["project_speech_references"].append(deepcopy(reference))
    elif defect == "orphan":
        reference["slot_key"] = f"segment:{uid(99)}"
    elif defect == "noncanonical_slot":
        reference["slot_key"] = "segment:00000000-0000-0000-0000-00000000000A"
    elif defect == "foreign_session":
        reference["session_id"] = uid(99)
    elif defect == "missing_owner":
        rows["projects"] = []
    elif defect == "selector":
        reference["selector_json"]["voice"] = "different"
    elif defect == "selector_hash":
        reference["selector_hash"] = "f" * 64
    elif defect == "binding_voice":
        reference["binding_json"]["voice"] = "different"
    elif defect == "binding_model":
        reference["binding_json"]["model"] = "different-model"
    elif defect == "pin_half":
        reference["input_content_hash"] = None
    elif defect == "pin_hash":
        reference["input_content_hash"] = "c" * 64
    elif defect == "artifact_owner":
        rows["artifacts"][1]["project_id"] = uid(99)
    else:
        rows["artifact_edges"] = []

    with pytest.raises(ValueError):
        validate_speech_copy_graph(
            rows,
            allow_null_inputs=True,
            allow_segment_references=True,
        )


def test_segment_reference_requires_canonical_saved_metadata_and_complete_identity_map():
    rows = _rows(pinned=False)
    reference = rows["project_speech_references"][0]
    identities = {
        _SESSION_ID: uid(102),
        _SEGMENT_ID: uid(105),
    }

    partial = deepcopy(reference)
    partial["input_content_hash"] = "a" * 64
    with pytest.raises(ValueError):
        remap_segment_reference(partial, identities, allow_null_inputs=True)

    with pytest.raises(ValueError):
        remap_segment_reference(reference, {_SEGMENT_ID: uid(105)}, allow_null_inputs=True)

    with pytest.raises(ValueError):
        freeze_segment_reference(
            {**reference, "slot_key": "segment:00000000-0000-0000-0000-00000000000A"},
            rows["session_settings"][0]["value_json"],
            binding=VoiceBinding.model_validate(reference["selector_json"]),
            owner=SpeechInputOwner(_WORKSPACE_ID, _PROJECT_ID, _SESSION_ID),
            artifacts={row["id"]: row for row in rows["artifacts"]},
            edges=rows["artifact_edges"],
            allow_null_inputs=True,
        )
