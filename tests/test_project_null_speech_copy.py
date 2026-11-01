"""Current copies preserve a saved speech selection with an explicit null input."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.generation_cast_runtime import resolve_project_cast_binding
from pandrator.web.jobs import Worker
from pandrator.web.project_tts_settings import bind_project_tts_reference
from pandrator.web.projects import copy_publication as publisher
from pandrator.web.projects import copy_snapshot as capture
from pandrator.web.projects.copy_contracts import COPY_STATE_VERSION, ProjectCopyError
from pandrator.web.projects.copy_files import StagedProjectFiles
from tests.project_copy_test_support import capture_with_connections, seed_copy_model
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_retained_preview_workspace import MODEL


def null_source(design):
    users = design.users
    source = body(
        users.alice.post(
            "/api/v1/sessions",
            json={
                "name": "Saved selection without retained audio",
                "workflow_kind": "subtitles",
                "source_language": "en",
            },
            headers=headers(users.ah, "null-speech-copy-source"),
        ),
        201,
    )
    saved = body(
        users.alice.put(
            f"/api/v1/sessions/{source['id']}/settings/tts",
            json={
                "value": {
                    "service": "audio_cpp",
                    "model": MODEL,
                    "voice": "saved-null-narrator",
                    "language": "en",
                    "voice_mode_version": 1,
                    "casting_enabled": False,
                    "performance_enabled": False,
                }
            },
            headers={**headers(users.ah, "null-speech-copy-choice"), "If-Match": '"0"'},
        )
    )
    assert saved["revision"] == 1
    with design.case.database.snapshot_session() as session:
        reference = session.get_one(m.ProjectSpeechReference, (source["id"], "tts"))
        assert reference.input_artifact_id is None and reference.input_content_hash is None
    return source


def capture_source(f, source):
    users = f.users
    seed_copy_model(f.case, users.workspace_id)
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=users.alice)))
    assert actor is not None
    with f.case.database.snapshot_session() as session:
        snapshot = capture_with_connections(
            f.case,
            session,
            AccessContext(actor),
            source["project_id"],
            users.workspace_id,
            settings_service=f.services.workspace_settings,
        )
    return snapshot


def test_explicit_null_narrator_selection_is_a_copyable_current_state(design):
    f = design
    source = null_source(f)
    snapshot = capture_source(f, source)
    assert snapshot.ready, snapshot.blockers
    state = json.loads(snapshot.state_json)
    assert state["schema_version"] == COPY_STATE_VERSION == 15
    references = state["rows"]["project_speech_references"]
    assert len(references) == 1 and references[0]["slot_key"] == "tts"
    assert references[0]["binding_json"]["voice"] == "saved-null-narrator"
    assert (
        references[0]["input_artifact_id"] is None and references[0]["input_content_hash"] is None
    )
    assert snapshot.files == ()


@pytest.mark.parametrize("cast", [False, True])
def test_registered_null_selection_copy_remains_owned_after_creator_departure(
    design, monkeypatch, cast
):
    f, users = design, design.users
    source = null_source(f)
    if cast:
        controls = body(
            users.alice.put(
                f"/api/v1/sessions/{source['id']}/generation-controls",
                json={
                    "expected_revision": 0,
                    "cast": {
                        "narrator": {
                            "voice": "saved-null-cast",
                            "service": "audio_cpp",
                            "model": MODEL,
                        }
                    },
                },
                headers=headers(users.ah, "null-cast-copy-choice"),
            )
        )
        assert controls["revision"] == 1
    snapshot = capture_source(f, source)
    assert snapshot.ready, snapshot.blockers
    expected = deepcopy(json.loads(snapshot.state_json)["rows"]["project_speech_references"])
    assert len(expected) == (2 if cast else 1) and snapshot.files == ()
    assert all(
        row["input_artifact_id"] is None and row["input_content_hash"] is None for row in expected
    )

    def no_library(*_args, **_kwargs):
        raise AssertionError("Copy or retained resolution must not recapture the source library")

    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input", no_library
    )
    monkeypatch.setattr("pandrator.web.speech_inputs.capture_speech_input", no_library)
    queued = body(
        users.alice.post(
            f"/api/v1/projects/{source['project_id']}/copy",
            json={
                "destination_workspace_id": users.workspace_id,
                "expected_fingerprint": snapshot.fingerprint,
                "name": snapshot.name,
            },
            headers=headers(users.ah, "registered-null-speech-copy"),
        ),
        202,
    )
    worker = Worker(
        f.services.jobs, "null-speech-copy-worker", f.services.workflow_handlers.handlers()
    )
    assert worker.run_once()
    finished = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert finished["state"] == "succeeded", finished
    copied = finished["result"]
    copied_sid = copied["source_session_id"]
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.WorkspaceMembership, (users.workspace_id, users.alice_id)))
    assert users.bob.get(f"/api/v1/sessions/{copied_sid}").status_code == 200
    assert users.bob.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    bob = actor_from_principal(current_browser_principal(replace(f.case, client=users.bob)))
    assert bob is not None
    with execution_actor_scope(bob), f.case.database.snapshot_session() as session:
        references = list(
            session.scalars(
                select(m.ProjectSpeechReference).where(
                    m.ProjectSpeechReference.session_id == copied_sid
                )
            )
        )
        assert len(references) == len(expected)
        for row in references:
            original = next(value for value in expected if value["slot_key"] == row.slot_key)
            assert (
                row.revision == 1
                and row.input_artifact_id is None
                and row.input_content_hash is None
            )
            assert (
                row.selector_json == original["selector_json"]
                and row.binding_json == original["binding_json"]
            )
        settings = f.services.workspace_settings.get_in_session(session, copied_sid, "tts")["effective"]
        frozen = {"tts": deepcopy(settings)}
        bind_project_tts_reference(session, copied_sid, frozen)
        assert (
            "_project_speech_input" in frozen["tts"]
            and frozen["tts"]["_project_speech_input"] is None
        )
        assert frozen["tts"]["_project_speech_binding"]["voice"] == "saved-null-narrator"
        if cast:
            choice = next(row for row in references if row.slot_key == "cast:narrator")
            resolved = resolve_project_cast_binding(
                session, copied_sid, choice.selector_json, settings, "cast:narrator"
            )
            assert resolved is not None and resolved["_project_speech_input"] is None
            assert resolved["voice"] == "saved-null-cast"


def test_format10_cannot_publish_explicit_null_inputs_even_with_valid_current_rows(
    design, monkeypatch
):
    f = design
    source = null_source(f)
    snapshot = capture_source(f, source)
    state = json.loads(snapshot.state_json)
    state["schema_version"] = 10
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    legacy = replace(
        snapshot, state_json=encoded, fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
    )

    def no_stage(*_args, **_kwargs):
        raise AssertionError("Legacy null graph passed its version validation boundary")

    monkeypatch.setattr(publisher, "_staging", no_stage)
    with f.case.database.immediate_session() as session:
        before = session.scalar(select(func.count()).select_from(m.Project))
        with pytest.raises(ProjectCopyError) as caught:
            publisher.publish_project_copy_rows(
                session,
                legacy,
                cast(StagedProjectFiles, object()),
                copy_id="00000000-0000-0000-0000-000000000099",
                created_by_user_id=f.users.alice_id,
            )
        assert caught.value.code == "copy_graph_invalid"
        assert session.scalar(select(func.count()).select_from(m.Project)) == before


def test_absent_narrator_reference_is_not_manufactured_by_copy_capture(design):
    f = design
    source = null_source(f)
    with f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.ProjectSpeechReference, (source["id"], "tts")))
    snapshot = capture_source(f, source)
    assert snapshot.ready, snapshot.blockers
    assert json.loads(snapshot.state_json)["rows"]["project_speech_references"] == []
    assert snapshot.files == ()


@pytest.mark.parametrize("version", [10, 11, 12, 13, 14])
def test_already_admitted_legacy_copy_runs_from_its_frozen_state(design, monkeypatch, version):
    f, users = design, design.users
    source = null_source(f)
    if version == 10:
        with f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.ProjectSpeechReference, (source["id"], "tts")))
    with monkeypatch.context() as earlier:
        earlier.setattr(capture, "COPY_STATE_VERSION", version)
        snapshot = capture_source(f, source)
        assert snapshot.ready and json.loads(snapshot.state_json)["schema_version"] == version
        queued = body(
            users.alice.post(
                f"/api/v1/projects/{source['project_id']}/copy",
                json={
                    "destination_workspace_id": users.workspace_id,
                    "expected_fingerprint": snapshot.fingerprint,
                    "name": snapshot.name,
                },
                headers=headers(users.ah, f"queued-format{version}-copy"),
            ),
            202,
        )
    assert capture.COPY_STATE_VERSION == 15
    worker = Worker(
        f.services.jobs, f"queued-format{version}-copy-worker", f.services.workflow_handlers.handlers()
    )
    assert worker.run_once()
    result = body(users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert result["state"] == "succeeded", result
    copied_sid = result["result"]["source_session_id"]
    with f.case.database.snapshot_session() as session:
        reference = session.get(m.ProjectSpeechReference, (copied_sid, "tts"))
        if version == 10:
            assert reference is None
        else:
            assert reference is not None
            assert reference.input_artifact_id is None and reference.input_content_hash is None
