"""Pure copy validation and remapping cases for saved TTS references."""

from copy import deepcopy
from hashlib import sha256
from uuid import UUID

import pytest

from pandrator.web.project_speech_references import (
    normalize_speech_reference_binding,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from pandrator.web.projects.copy_speech import (
    freeze_tts_reference,
    remap_tts_reference,
    validate_tts_copy_graph,
)
from pandrator.web.speech_input_contracts import SpeechInputOwner

WORKSPACE_ID = str(UUID(int=1))
PROJECT_ID = str(UUID(int=2))
SESSION_ID = str(UUID(int=3))
HEADER_ID = str(UUID(int=4))
AUDIO_ID = str(UUID(int=5))
NEW_SESSION_ID = str(UUID(int=6))
NEW_HEADER_ID = str(UUID(int=7))
CONTENT_HASH = sha256(b"snapshot").hexdigest()
AUDIO_HASH = sha256(b"wav bytes").hexdigest()
EFFECTIVE_TTS = {
    "service": "audio_cpp",
    "model": "xtts_v2",
    "voice": "Narrator",
}
_selection_selector = {
    "voice": "Narrator",
    "voice_id": None,
    "service": "audio_cpp",
    "model": "xtts_v2",
}
SELECTOR = speech_reference_selector(_selection_selector)
SELECTOR_HASH = speech_reference_selector_hash(SELECTOR)
BINDING = normalize_speech_reference_binding(
    {
        "adapter": "audio_cpp",
        "model": "xtts_v2",
        "voice": "Narrator",
        "display_name": "Narrator",
    }
)
PIN = {"version": 1, "artifact_id": HEADER_ID, "content_hash": CONTENT_HASH}
OWNER = SpeechInputOwner(WORKSPACE_ID, PROJECT_ID, SESSION_ID)


def reference_row(
    *,
    selector: dict | None = None,
    selector_hash: str = SELECTOR_HASH,
    binding: dict | None = None,
    pin: dict | None = PIN,
    revision: int = 3,
) -> dict:
    selected_pin = PIN if pin is PIN else pin
    return {
        "session_id": SESSION_ID,
        "slot_key": "tts",
        "selector_json": deepcopy(SELECTOR if selector is None else selector),
        "selector_hash": selector_hash,
        "binding_json": deepcopy(BINDING if binding is None else binding),
        "input_artifact_id": None if selected_pin is None else selected_pin["artifact_id"],
        "input_content_hash": None if selected_pin is None else selected_pin["content_hash"],
        "revision": revision,
    }


def artifact_row(
    artifact_id: str,
    *,
    workspace_id: str = WORKSPACE_ID,
    project_id: str = PROJECT_ID,
    session_id: str = SESSION_ID,
    kind: str,
    role: str,
    mime_type: str,
    content_hash: str,
    size_bytes: int = 64,
    state: str = "current",
) -> dict:
    return {
        "id": artifact_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "session_id": session_id,
        "kind": kind,
        "role": role,
        "mime_type": mime_type,
        "content_hash": content_hash,
        "size_bytes": size_bytes,
        "state": state,
    }


def speech_graph() -> tuple[dict[str, dict], list[dict]]:
    return (
        {
            HEADER_ID: artifact_row(
                HEADER_ID,
                kind="speech_voice_snapshot",
                role="speech_input",
                mime_type="application/json",
                content_hash=CONTENT_HASH,
            ),
            AUDIO_ID: artifact_row(
                AUDIO_ID,
                kind="audio",
                role="speech_reference_audio",
                mime_type="audio/wav",
                content_hash=AUDIO_HASH,
            ),
        },
        [
            {
                "parent_artifact_id": AUDIO_ID,
                "child_artifact_id": HEADER_ID,
                "relation": "speech_reference",
            }
        ],
    )


def copy_rows(*, reference: dict | None = None, settings: list | None = None) -> dict:
    artifacts, edges = speech_graph()
    return {
        "project_speech_references": [reference_row() if reference is None else reference],
        "sessions": [{"id": SESSION_ID, "project_id": PROJECT_ID}],
        "projects": [{"id": PROJECT_ID, "workspace_id": WORKSPACE_ID}],
        "session_settings": (
            [{"session_id": SESSION_ID, "section": "tts", "value_json": EFFECTIVE_TTS}]
            if settings is None
            else settings
        ),
        "artifacts": list(artifacts.values()),
        "artifact_edges": edges,
    }


def test_freeze_returns_deep_copy_of_valid_retained_reference():
    row = reference_row()
    artifacts, edges = speech_graph()

    frozen = freeze_tts_reference(
        row, EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
    )

    assert frozen == row
    assert frozen is not row
    assert frozen["selector_json"] is not row["selector_json"]
    frozen["selector_json"]["voice"] = "changed"
    assert row["selector_json"]["voice"] == "Narrator"


def test_remap_changes_only_session_and_input_ids_and_resets_revision():
    row = reference_row()
    identities = {SESSION_ID: NEW_SESSION_ID, HEADER_ID: NEW_HEADER_ID}

    remapped = remap_tts_reference(row, identities)

    assert remapped["session_id"] == NEW_SESSION_ID
    assert remapped["input_artifact_id"] == NEW_HEADER_ID
    assert remapped["revision"] == 1
    for field in ("slot_key", "selector_json", "selector_hash", "binding_json", "input_content_hash"):
        assert remapped[field] == row[field]
    assert remapped is not row
    assert remapped["selector_json"] is not row["selector_json"]


def test_remap_never_rewrites_recorded_managed_voice_identity():
    voice_id = str(UUID(int=11))
    selector = speech_reference_selector(
        {
            "voice": "",
            "voice_id": voice_id,
            "service": "audio_cpp",
            "model": "xtts_v2",
        }
    )
    row = reference_row(selector=selector, selector_hash=speech_reference_selector_hash(selector))

    remapped = remap_tts_reference(
        row,
        {
            SESSION_ID: NEW_SESSION_ID,
            HEADER_ID: NEW_HEADER_ID,
            voice_id: str(UUID(int=12)),
        },
    )

    assert remapped["selector_json"]["voice_id"] == voice_id


def test_freeze_keeps_null_pin_structurally_valid_but_publication_rejects_it():
    row = reference_row(pin=None)
    artifacts, edges = speech_graph()

    assert freeze_tts_reference(
        row, EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
    ) == row
    with pytest.raises(ValueError):
        validate_tts_copy_graph(copy_rows(reference=row))


@pytest.mark.parametrize("revision", [0, -1, True, 1.0])
def test_freeze_rejects_invalid_reference_revision(revision):
    row = reference_row(revision=revision)
    artifacts, edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(row, EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges)


def test_freeze_rejects_row_extra_fields_and_non_tts_slots():
    artifacts, edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(
            {**reference_row(), "created_at": "ignored"},
            EFFECTIVE_TTS,
            owner=OWNER,
            artifacts=artifacts,
            edges=edges,
        )
    row = reference_row()
    row["slot_key"] = "cast:narrator"
    with pytest.raises(ValueError):
        freeze_tts_reference(row, EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges)


def test_freeze_rejects_reference_session_outside_owner():
    row = reference_row()
    row["session_id"] = str(UUID(int=9))
    artifacts, edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(row, EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges)


def test_freeze_rejects_selector_hash_mismatch_and_noncanonical_selector_fields():
    artifacts, edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(selector_hash="0" * 64),
            EFFECTIVE_TTS,
            owner=OWNER,
            artifacts=artifacts,
            edges=edges,
        )
    extra = {**SELECTOR, "secret": "not copied"}
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(selector=extra),
            EFFECTIVE_TTS,
            owner=OWNER,
            artifacts=artifacts,
            edges=edges,
        )


@pytest.mark.parametrize(
    ("effective", "binding", "expected_selector"),
    [
        ({**EFFECTIVE_TTS, "model": "different-model"}, BINDING, SELECTOR),
        (EFFECTIVE_TTS, {**BINDING, "model": "different-model"}, SELECTOR),
        (EFFECTIVE_TTS, {**BINDING, "voice": "Different"}, SELECTOR),
        (EFFECTIVE_TTS, {**BINDING, "voice": " Narrator "}, SELECTOR),
    ],
)
def test_freeze_rejects_effective_settings_or_noncanonical_binding_mismatch(
    effective, binding, expected_selector
):
    artifacts, edges = speech_graph()
    row = reference_row(binding=binding)
    if effective.get("model") != EFFECTIVE_TTS["model"]:
        row["selector_json"] = deepcopy(expected_selector)
    with pytest.raises(ValueError):
        freeze_tts_reference(row, effective, owner=OWNER, artifacts=artifacts, edges=edges)


def test_freeze_rejects_binding_extra_fields():
    artifacts, edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(binding={**BINDING, "voice_id": str(UUID(int=10))}),
            EFFECTIVE_TTS,
            owner=OWNER,
            artifacts=artifacts,
            edges=edges,
        )


def test_freeze_preserves_managed_voice_alias_without_matching_binding_voice():
    effective = {
        "service": "audio_cpp",
        "model": "xtts_v2",
        "voice": "Effective alias",
        "voice_id": str(UUID(int=8)),
    }
    selector = speech_reference_selector(
        {
            "voice": "",
            "voice_id": effective["voice_id"],
            "service": "audio_cpp",
            "model": "xtts_v2",
        }
    )
    binding = normalize_speech_reference_binding(
        {
            "adapter": "audio_cpp",
            "model": "xtts_v2",
            "voice": "Saved alias",
            "display_name": "Library title",
        }
    )
    row = reference_row(selector=selector, selector_hash=speech_reference_selector_hash(selector), binding=binding)
    artifacts, edges = speech_graph()

    assert freeze_tts_reference(
        row, effective, owner=OWNER, artifacts=artifacts, edges=edges
    ) == row


@pytest.mark.parametrize(
    "mutation",
    [
        {"state": "deleted"},
        {"workspace_id": str(UUID(int=20))},
        {"project_id": str(UUID(int=21))},
        {"session_id": str(UUID(int=22))},
        {"kind": "audio"},
        {"role": "other"},
        {"mime_type": "text/plain"},
        {"content_hash": AUDIO_HASH},
        {"size_bytes": True},
        {"size_bytes": 256 * 1024 + 1},
    ],
)
def test_freeze_rejects_invalid_header_graph_metadata(mutation):
    artifacts, edges = speech_graph()
    artifacts[HEADER_ID].update(mutation)
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
        )


def test_freeze_rejects_missing_or_duplicate_header_projection():
    artifacts, edges = speech_graph()
    del artifacts[HEADER_ID]
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
        )
    artifacts, edges = speech_graph()
    artifacts["duplicate-audio-key"] = deepcopy(artifacts[AUDIO_ID])
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
        )
    duplicate = artifact_row(
        HEADER_ID,
        kind="speech_voice_snapshot",
        role="speech_input",
        mime_type="application/json",
        content_hash=CONTENT_HASH,
    )
    artifacts, edges = speech_graph()
    artifacts["duplicate-key"] = duplicate
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
        )


@pytest.mark.parametrize(
    "edges_mutation",
    [
        [],
        [
            {"parent_artifact_id": AUDIO_ID, "child_artifact_id": HEADER_ID, "relation": "derived_from"}
        ],
        [
            {"parent_artifact_id": AUDIO_ID, "child_artifact_id": HEADER_ID, "relation": "speech_reference"},
            {"parent_artifact_id": AUDIO_ID, "child_artifact_id": HEADER_ID, "relation": "speech_reference"},
        ],
    ],
)
def test_freeze_requires_one_matching_incoming_edge(edges_mutation):
    artifacts, _edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges_mutation
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"state": "deleted"},
        {"workspace_id": str(UUID(int=30))},
        {"project_id": str(UUID(int=31))},
        {"session_id": str(UUID(int=32))},
        {"kind": "speech_voice_snapshot"},
        {"role": "speech_input"},
        {"mime_type": "audio/mpeg"},
        {"content_hash": "NOT-A-SHA256"},
        {"size_bytes": False},
        {"size_bytes": 5 * 1024 * 1024 + 1},
    ],
)
def test_freeze_rejects_invalid_audio_graph_metadata(mutation):
    artifacts, edges = speech_graph()
    artifacts[AUDIO_ID].update(mutation)
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
        )


def test_freeze_rejects_missing_audio_and_owner_mismatch():
    artifacts, edges = speech_graph()
    del artifacts[AUDIO_ID]
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(), EFFECTIVE_TTS, owner=OWNER, artifacts=artifacts, edges=edges
        )
    artifacts, edges = speech_graph()
    with pytest.raises(ValueError):
        freeze_tts_reference(
            reference_row(),
            EFFECTIVE_TTS,
            owner=SpeechInputOwner(WORKSPACE_ID, str(UUID(int=40)), SESSION_ID),
            artifacts=artifacts,
            edges=edges,
        )


def test_validate_graph_accepts_valid_copy_rows_and_rejects_duplicate_reference():
    validate_tts_copy_graph(copy_rows())
    rows = copy_rows()
    rows["project_speech_references"].append(deepcopy(rows["project_speech_references"][0]))
    with pytest.raises(ValueError):
        validate_tts_copy_graph(rows)


def test_validate_graph_accepts_remapped_tts_settings_revision():
    rows = copy_rows()
    rows["session_settings"][0]["revision"] = 1

    validate_tts_copy_graph(rows)


def test_validate_graph_rejects_tts_settings_without_value_json():
    rows = copy_rows()
    rows["session_settings"] = [{"session_id": SESSION_ID, "section": "tts"}]

    with pytest.raises(ValueError):
        validate_tts_copy_graph(rows)


@pytest.mark.parametrize(
    "rows_mutation",
    [
        {"sessions": []},
        {"sessions": [{"id": SESSION_ID, "project_id": PROJECT_ID}] * 2},
        {"projects": []},
        {"projects": [{"id": PROJECT_ID, "workspace_id": WORKSPACE_ID}] * 2},
        {"session_settings": []},
        {
            "session_settings": [
                {"session_id": SESSION_ID, "section": "tts", "value_json": EFFECTIVE_TTS},
                {"session_id": SESSION_ID, "section": "tts", "value_json": EFFECTIVE_TTS},
            ]
        },
    ],
)
def test_validate_graph_requires_unique_owner_and_tts_settings(rows_mutation):
    rows = copy_rows()
    rows.update(rows_mutation)
    with pytest.raises(ValueError):
        validate_tts_copy_graph(rows)


def test_legacy_empty_reference_family_is_noop_without_other_row_families():
    validate_tts_copy_graph({})
    validate_tts_copy_graph({"project_speech_references": []})


@pytest.mark.parametrize(
    "identities",
    [
        {SESSION_ID: NEW_SESSION_ID},
        {HEADER_ID: NEW_HEADER_ID},
        {SESSION_ID: "", HEADER_ID: NEW_HEADER_ID},
    ],
)
def test_remap_rejects_missing_or_invalid_identity_mapping(identities):
    with pytest.raises(ValueError):
        remap_tts_reference(reference_row(), identities)


def test_remap_rejects_null_pin_and_missing_source_keys():
    with pytest.raises(ValueError):
        remap_tts_reference(reference_row(pin=None), {SESSION_ID: NEW_SESSION_ID})
    row = reference_row()
    del row["input_artifact_id"]
    with pytest.raises(ValueError):
        remap_tts_reference(row, {SESSION_ID: NEW_SESSION_ID, HEADER_ID: NEW_HEADER_ID})
