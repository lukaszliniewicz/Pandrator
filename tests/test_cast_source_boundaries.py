"""Managed selection metadata is filtered to its authorized workspace before load."""

from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.project_speech_reference_capture import (
    capture_project_speech_reference,
    prepare_project_speech_reference,
    publish_project_speech_reference,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.speech_input_contracts import SpeechInputError, SpeechInputOwner
from tests.test_evidence_access_routes import body, headers
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_project_tts_retention import save
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL
from tests.test_retained_speech_inputs import harness as harness  # noqa: F401
from tests.test_voice_crud_workspace_access import create
from tests.test_voice_sample_execution_access import api_actor


def test_foreign_managed_selection_never_loads_its_private_voice_body(project):
    p, r = project, project.r
    private = body(create(r.f.users.bob, r.f.users.bh, "Bob private reference"), 201)
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.Voice, private["id"]).metadata_json = {
            "private_marker": "KEEP_BOB_REFERENCE_PRIVATE",
        }
    materialized = []

    def loaded(_session, instance):
        if isinstance(instance, m.Voice) and instance.id == private["id"]:
            materialized.append(instance.id)

    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        foreign = save(p, {"service": "audio_cpp", "model": MODEL,
            "voice": "workshop-reference", "voice_id": private["id"]},
            key="foreign-managed-selection")
        missing = save(p, {"service": "audio_cpp", "model": MODEL,
            "voice": "workshop-reference", "voice_id": str(uuid4())},
            key="missing-managed-selection")
    finally:
        event.remove(Session, "loaded_as_persistent", loaded)
    assert foreign.status_code == missing.status_code == 422
    assert foreign.get_json()["error"]["code"] == missing.get_json()["error"]["code"]
    assert "KEEP_BOB_REFERENCE_PRIVATE" not in str(foreign.get_json())
    assert materialized == []
    with r.f.case.database.snapshot_session() as session:
        assert session.get(m.ProjectSpeechReference, (p.source["id"], "tts")) is None
    assert r.path.is_file()


def test_history_identity_never_loads_a_foreign_managed_voice(project):
    p, r = project, project.r
    private = body(create(r.f.users.bob, r.f.users.bh, "Bob private identity"), 201)
    r.f.services.generation.create_plan(p.source["id"], source_revision_id=None, settings={},
        segments=[{"text": "A private reference must stay private.", "language": "en"}])
    segment_id = r.f.services.generation.list_segments(p.source["id"])["items"][0]["id"]
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.GenerationSegment, segment_id).voice_id = private["id"]
    actor, _token = api_actor(r.f.case, r.f.users.alice_id, r.f.users.workspace_id)
    materialized = []

    def loaded(_session, instance):
        if isinstance(instance, m.Voice) and instance.id == private["id"]:
            materialized.append(instance.id)

    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        with execution_actor_scope(actor), r.f.case.database.snapshot_session() as session:
            segment = session.get_one(m.GenerationSegment, segment_id)
            result = AudioIdentityContext(session, {"tts": {
                "service": "audio_cpp", "model": MODEL, "voice": "workshop-reference",
                "voice_mode_version": 0, "casting_enabled": False,
            }}).for_segment(segment)
    finally:
        event.remove(Session, "loaded_as_persistent", loaded)
    assert result.get("performance_error")
    assert materialized == []


def test_cast_save_rejects_foreign_managed_ids_without_private_voice_materialization(project):
    p, r = project, project.r
    private = body(create(r.f.users.bob, r.f.users.bh, "Bob private cast"), 201)
    materialized = []

    def loaded(_session, instance):
        if isinstance(instance, m.Voice) and instance.id == private["id"]:
            materialized.append(instance.id)

    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        response = r.f.users.alice.put(
            f"/api/v1/sessions/{p.source['id']}/generation-controls",
            json={"expected_revision": 0, "cast": {"narrator": {"voice_id": private["id"]}}},
            headers=headers(r.f.users.ah, "foreign-voice-cast-save"),
        )
    finally:
        event.remove(Session, "loaded_as_persistent", loaded)
    assert response.status_code == 422, response.get_json()
    assert materialized == []
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(m.KnowledgeLedger.id).where(
            m.KnowledgeLedger.session_id == p.source["id"],
            m.KnowledgeLedger.kind == "generation_controls",
        )) is None


def test_project_only_cast_write_cannot_query_workspace_voice_ids(project):
    p, r = project, project.r
    _token, bearer = scoped_token(r.f.case, r.f.users.alice_id,
        {"app.read", "app.write"}, project=p.source["project_id"])
    source_queries = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "FROM voices" in statement:
            source_queries.append(statement)

    event.listen(r.f.case.database.engine, "before_cursor_execute", observe)
    try:
        response = r.f.users.alice.put(
            f"/api/v1/sessions/{p.source['id']}/generation-controls",
            json={"expected_revision": 0, "cast": {"narrator": {"voice_id": r.voice["id"]}}},
            headers=headers(bearer, "project-only-voice-cast-save"),
        )
    finally:
        event.remove(r.f.case.database.engine, "before_cursor_execute", observe)
    assert response.status_code == 404, response.get_json()
    assert source_queries == []


@pytest.mark.parametrize("operation", ["refresh", "model_change"])
def test_missing_original_cannot_silently_replace_a_saved_reference_with_null(harness, operation):
    h = harness
    with h.database.immediate_session() as session:
        record = m.SessionRecord(name="Saved speech", workflow_kind="audiobook")
        root = create_session_project(session, record, workspace_id=h.owner.workspace_id)
        h.owner = SpeechInputOwner(h.owner.workspace_id, root.id, record.id)
    session_id = h.owner.session_id
    assert session_id is not None
    selector = {"voice": "chosen", "service": "audio_cpp", "model": "synthetic"}
    def guard(_session):
        return None
    with h.database.snapshot_session() as session:
        capture = capture_project_speech_reference(session, h.paths, session_id, "tts",
            selector=selector, settings=h.settings,
            authorization_guard=guard, source_authorization_guard=guard)
    prepared = prepare_project_speech_reference(h.database, h.paths, capture, before_io=lambda: None)
    assert prepared is not None
    with h.database.immediate_session() as session:
        saved = publish_project_speech_reference(session, h.paths, capture, prepared,
            settings=h.settings, authorization_guard=guard, source_authorization_guard=guard)
        session.delete(session.get_one(m.VoiceSample, h.sample_id))
        session.delete(session.get_one(m.Voice, h.voice_id))
    settings = dict(h.settings)
    if operation == "model_change":
        settings["model"] = selector["model"] = "another-model"
    with h.database.snapshot_session() as session, pytest.raises(SpeechInputError, match="reference"):
        capture_project_speech_reference(session, h.paths, session_id, "tts",
            selector=selector, settings=settings, refresh=operation == "refresh",
            authorization_guard=guard, source_authorization_guard=guard)
    with h.database.snapshot_session() as session:
        row = session.get_one(m.ProjectSpeechReference, (session_id, "tts"))
        assert saved.pin is not None and row.input_artifact_id == saved.pin["artifact_id"]
    assert h.paths.managed_path(prepared.audio.relative_path).is_file()
    assert h.paths.managed_path(prepared.snapshot.relative_path).is_file()
