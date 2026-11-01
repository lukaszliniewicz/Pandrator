"""Workspace annotation isolation using synthetic catalogue and audio evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from sqlalchemy import select

from pandrator.web.accounts.models import Workspace
from pandrator.web.models import Voice, VoiceCatalogOverride
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.voice_catalog import catalog_entries
from pandrator.web.voice_catalog_metadata import catalog_overrides, update_catalog_metadata
from pandrator.web.voice_catalog_schemas import (
    CatalogVoiceUpdate,
    VoiceReference,
    canonical_voice_key,
)
from tests.test_voice_workspace_library import (
    VoiceHarness,
    _new_sample_artifact,
)
from tests.test_voice_workspace_library import voice_harness as voice_harness

REFERENCE = VoiceReference(kind="provider", service_id="reader", model="basic", voice="stable-id")
KEY = canonical_voice_key(REFERENCE)


def _update(
    changes: dict[str, Any], *, revision: int = 0, reference: VoiceReference = REFERENCE
) -> CatalogVoiceUpdate:
    return CatalogVoiceUpdate.model_validate(
        {"reference": reference, "expected_revision": revision, "changes": changes}
    )


def _rows(harness: VoiceHarness) -> list[tuple[Any, ...]]:
    with harness.database.snapshot_session() as session:
        return [
            (
                row.workspace_id,
                row.voice_key,
                deepcopy(row.voice_ref_json),
                row.name,
                row.description,
                row.voice_category,
                deepcopy(row.profile_json),
                row.revision,
                row.created_at,
                row.updated_at,
            )
            for row in session.scalars(
                select(VoiceCatalogOverride).order_by(
                    VoiceCatalogOverride.workspace_id, VoiceCatalogOverride.voice_key
                )
            )
        ]


def test_identical_provider_keys_keep_separate_workspace_annotations(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    for name, owner in (
        ("Alpha narrator", harness.workspaces["alpha"]),
        ("Beta narrator", harness.workspaces["beta"]),
        ("Solo narrator", harness.personal_workspace_id),
    ):
        with harness.database.immediate_session() as session:
            result = update_catalog_metadata(
                session,
                harness.paths,
                _update({"name": f" {name} ", "profile": {"pitch": "low"}}),
                workspace_id=owner,
            )
            assert result["key"] == KEY
            assert result["reference"] == REFERENCE.model_dump(exclude_none=True)
            assert result["name"] == name
            assert result["revision"] == 1

    provider_catalog = {
        "services": [
            {"id": "reader", "models": ["basic"], "voices": ["stable-id"], "available": True}
        ]
    }
    for name, owner in (
        ("Alpha narrator", harness.workspaces["alpha"]),
        ("Beta narrator", harness.workspaces["beta"]),
        ("Solo narrator", None),
    ):
        with harness.database.session() as session:
            overrides = catalog_overrides(session, workspace_id=owner)
        assert list(overrides) == [KEY]
        assert overrides[KEY]["name"] == name
        entries = catalog_entries([], provider_catalog, [], overrides)
        assert len(entries) == 1 and entries[0]["name"] == name
    assert len(_rows(harness)) == 3


def test_stale_revision_cannot_update_a_different_workspace_annotation(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    with harness.database.immediate_session() as session:
        update_catalog_metadata(
            session,
            harness.paths,
            _update({"name": "Private"}),
            workspace_id=harness.workspaces["beta"],
        )
    before = _rows(harness)
    # A foreign revision is not an existing annotation in the requested workspace.
    with harness.database.immediate_session() as session:
        with pytest.raises(RevisionConflict):
            update_catalog_metadata(
                session,
                harness.paths,
                _update({"name": "Changed"}, revision=1),
                workspace_id=harness.workspaces["alpha"],
            )
    assert _rows(harness) == before


def test_foreign_profile_evidence_rejection_leaves_no_new_or_partial_annotation(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    own = _new_sample_artifact(harness, harness.workspaces["alpha"], "own-evidence")
    foreign = _new_sample_artifact(harness, harness.workspaces["beta"], "foreign-evidence")

    def reviewed_profile(artifact_id: str) -> dict[str, Any]:
        return {
            "pitch": "low",
            "evidence": {
                "pitch": {
                    "source": "audition_review",
                    "status": "reviewed",
                    "artifact_id": artifact_id,
                }
            },
        }

    before = _rows(harness)
    with harness.database.immediate_session() as session:
        with pytest.raises(ValueError, match="workspace"):
            update_catalog_metadata(
                session,
                harness.paths,
                _update({"name": "Partial", "profile": reviewed_profile(foreign.id)}),
                workspace_id=harness.workspaces["alpha"],
            )
    assert _rows(harness) == before

    with harness.database.immediate_session() as session:
        update_catalog_metadata(
            session,
            harness.paths,
            _update({"name": "Original", "profile": reviewed_profile(own.id)}),
            workspace_id=harness.workspaces["alpha"],
        )
    before = _rows(harness)
    own_bytes = harness.paths.managed_path(own.relative_path).read_bytes()
    foreign_bytes = harness.paths.managed_path(foreign.relative_path).read_bytes()
    with harness.database.immediate_session() as session:
        with pytest.raises(ValueError, match="workspace"):
            update_catalog_metadata(
                session,
                harness.paths,
                _update(
                    {"name": "Partial edit", "profile": reviewed_profile(foreign.id)}, revision=1
                ),
                workspace_id=harness.workspaces["alpha"],
            )
    assert _rows(harness) == before
    assert harness.paths.managed_path(own.relative_path).read_bytes() == own_bytes
    assert harness.paths.managed_path(foreign.relative_path).read_bytes() == foreign_bytes


def test_category_evidence_is_cleared_without_rewriting_other_evidence(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    profile = {
        "pitch": "low",
        "evidence": {
            "voice_category": {
                "source": "user",
                "status": "described",
                "note": "Original category",
            },
            "pitch": {"source": "user", "status": "described", "note": "Keep pitch"},
        },
    }
    with harness.database.immediate_session() as session:
        update_catalog_metadata(
            session,
            harness.paths,
            _update({"voice_category": "male", "profile": profile}),
            workspace_id=harness.workspaces["alpha"],
        )
        changed = update_catalog_metadata(
            session,
            harness.paths,
            _update({"voice_category": "female"}, revision=1),
            workspace_id=harness.workspaces["alpha"],
        )
        assert changed["revision"] == 2
        assert "voice_category" not in changed["profile"]["evidence"]
        assert changed["profile"]["evidence"]["pitch"]["note"] == "Keep pitch"


@pytest.mark.parametrize("scope", ["missing", "archived"])
def test_invalid_workspace_blocks_metadata_reads_and_writes(
    voice_harness: VoiceHarness,
    scope: str,
) -> None:
    harness = voice_harness
    owner = "missing" if scope == "missing" else harness.workspaces["archived"]
    before = _rows(harness)
    with harness.database.immediate_session() as session:
        with pytest.raises(ValueError):
            catalog_overrides(session, workspace_id=owner)
        with pytest.raises(ValueError):
            update_catalog_metadata(
                session, harness.paths, _update({"name": "Invalid"}), workspace_id=owner
            )
    assert _rows(harness) == before


def test_malformed_stored_reference_blocks_projection_and_mutation(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    owner = harness.workspaces["alpha"]
    with harness.database.immediate_session() as session:
        update_catalog_metadata(
            session, harness.paths, _update({"name": "Saved"}), workspace_id=owner
        )
        row = session.get(VoiceCatalogOverride, (owner, KEY))
        assert row is not None
        row.voice_ref_json = {**REFERENCE.model_dump(exclude_none=True), "voice": "another-id"}
    before = _rows(harness)
    with harness.database.immediate_session() as session:
        with pytest.raises(ValueError, match="inconsistent"):
            catalog_overrides(session, workspace_id=owner)
        with pytest.raises(ValueError, match="inconsistent"):
            update_catalog_metadata(
                session, harness.paths, _update({"name": "Partial"}, revision=1), workspace_id=owner
            )
    assert _rows(harness) == before
    # Corruption in another workspace cannot poison an independent private list.
    with harness.database.session() as session:
        assert catalog_overrides(session, workspace_id=harness.workspaces["beta"]) == {}


def test_archive_after_save_keeps_annotation_history_and_rejects_new_work(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    owner = harness.workspaces["alpha"]
    with harness.database.immediate_session() as session:
        update_catalog_metadata(
            session, harness.paths, _update({"description": "Retained"}), workspace_id=owner
        )
        workspace = session.get(Workspace, owner)
        assert workspace is not None
        workspace.state = "archived"
    before = _rows(harness)
    with harness.database.immediate_session() as session:
        with pytest.raises(ValueError, match="inactive"):
            update_catalog_metadata(
                session,
                harness.paths,
                _update({"description": "Changed"}, revision=1),
                workspace_id=owner,
            )
    assert _rows(harness) == before


def test_ignored_historical_managed_annotation_survives_voice_deletion(
    voice_harness: VoiceHarness,
) -> None:
    harness = voice_harness
    owner = harness.workspaces["alpha"]
    with harness.database.immediate_session() as session:
        voice = Voice(workspace_id=owner, name="Historical annotation")
        session.add(voice)
        session.flush()
        reference = VoiceReference(kind="managed", voice_id=voice.id)
        key = canonical_voice_key(reference)
        session.add(
            VoiceCatalogOverride(
                workspace_id=owner,
                voice_key=key,
                voice_ref_json=reference.model_dump(exclude_none=True),
                name="Retained original label",
            )
        )
        session.flush()
        session.delete(voice)
    with harness.database.session() as session:
        overrides = catalog_overrides(session, workspace_id=owner)
    assert overrides[key]["name"] == "Retained original label"
    assert catalog_entries([], {"services": []}, [], overrides) == []
