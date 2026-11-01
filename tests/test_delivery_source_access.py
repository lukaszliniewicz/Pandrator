"""Attached-source selection never broadens a queued credential's resource ceiling."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, event
from sqlalchemy.orm import Session

from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
)
from pandrator.web.export_inputs import resolve_export_inputs
from pandrator.web.models import Artifact, SessionSetting, SessionSource, SourceAsset
from pandrator.web.source_management import require_recording_timing_review
from pandrator.web.source_resolution import resolve_media_source, resolve_primary_source
from pandrator.web.workflow_prerequisites import WorkflowPrerequisiteService
from tests.test_delivery_runtime import case as case
from tests.test_delivery_runtime import no_network as no_network
from tests.test_delivery_runtime import runtime_case as runtime_case
from tests.test_delivery_runtime import scoped
from tests.test_delivery_runtime import template as template


@pytest.mark.parametrize("boundary", ["primary", "media", "prerequisite", "fallback", "timing"])
def test_project_credential_cannot_load_attached_other_project_source_body(runtime_case, boundary):
    f = runtime_case
    source_path = f.paths.uploads / "other-project-source.wav"
    source_path.write_bytes(b"Synthetic cross-project fixture")
    source = f.artifacts.register(source_path, kind="audio", session_id=f.f.b.id)
    with f.f.database.immediate_session() as session:
        asset = SourceAsset(
            workspace_id=f.f.team,
            artifact_id=source.id,
            display_name="Private source name",
            kind="audio",
            metadata_json={"private_note": "Do not load before source authorization"},
        )
        session.add(asset)
        session.flush()
        session.add(
            SessionSource(
                session_id=f.f.a.id,
                source_asset_id=asset.id,
                role="media" if boundary in {"media", "timing"} else "primary",
            )
        )
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(
                ApiTokenWorkspaceGrant.token_id == f.actor.token_id
            )
        )
        session.add(ApiTokenProjectGrant(token_id=f.actor.token_id, project_id=f.f.a.project_id))
        if boundary == "timing":
            session.add(
                SessionSetting(
                    session_id=f.f.a.id,
                    section="_recording_timing_review",
                    value_json={"required": True},
                )
            )
    materialized = []

    def loaded(_session, instance):
        if isinstance(instance, SourceAsset) or (
            isinstance(instance, Artifact) and instance.id == source.id
        ):
            materialized.append(type(instance).__name__)

    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        with scoped(f) as context, pytest.raises(AccessDenied):
            assert context._authorization_guard is not None and context._authorize_input is not None
            if boundary == "prerequisite":
                WorkflowPrerequisiteService(f.f.database, f.paths)._latest_stage_input(
                    f.f.a.id,
                    ("upload",),
                    authorization_guard=context._authorization_guard,
                    authorize_input=context._authorize_input,
                )
            elif boundary == "fallback":
                resolve_export_inputs(
                    context,
                    {
                        "session_id": f.f.a.id,
                        "settings": {"export_mode": "subtitles", "audio_mode": "preserve"},
                    },
                )
            elif boundary == "timing":
                require_recording_timing_review(
                    f.f.database,
                    f.f.a.id,
                    {"export_mode": "audio"},
                    authorization_guard=context._authorization_guard,
                    authorize_input=context._authorize_input,
                )
            else:
                with f.f.database.snapshot_session() as session:
                    context._authorization_guard(session)
                    resolve = (
                        resolve_primary_source if boundary == "primary" else resolve_media_source
                    )
                    resolve(session, f.f.a.id, authorize_input=context._authorize_input)
    finally:
        event.remove(Session, "loaded_as_persistent", loaded)
    assert not materialized
    assert source_path.read_bytes() == b"Synthetic cross-project fixture"
