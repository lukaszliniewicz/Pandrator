"""The existing project settings API owns retained local voice selections."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web import project_tts_settings as writer
from pandrator.web import speech_inputs
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.session_purge import SessionPurgeService
from pandrator.web.speech_input_contracts import SpeechInputOwner
from tests.test_evidence_access_routes import body, headers
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL, transport
from tests.test_retained_preview_workspace import case as case  # noqa: F401
from tests.test_retained_preview_workspace import design as design  # noqa: F401
from tests.test_retained_preview_workspace import no_network as no_network  # noqa: F401
from tests.test_retained_preview_workspace import retained as retained  # noqa: F401
from tests.test_settings_workspace_access import put


@pytest.fixture
def project(retained):
    r = retained
    source = body(
        r.f.users.alice.post(
            "/api/v1/sessions",
            json={"name": "Retained project voice", "workspace_id": r.f.users.workspace_id},
            headers=headers(r.f.users.ah, "retained-project-session"),
        ),
        201,
    )
    return SimpleNamespace(r=r, source=source, path=f"/api/v1/sessions/{source['id']}/settings/tts")


def save(p, value=None, *, revision=0, key="retained-project-voice", patch=False, client=None, auth=None):
    r = p.r
    if value is None:
        value = {"service": "audio_cpp", "model": MODEL, "voice": "workshop-reference", "language": "en"}
    return (client or r.f.users.alice).open(
        p.path,
        method="PATCH" if patch else "PUT",
        json={"value": value},
        headers={**headers(auth or r.f.users.ah, key), "If-Match": f'"{revision}"'},
    )


def reference(p):
    with p.r.f.case.database.snapshot_session() as session:
        row = session.get(m.ProjectSpeechReference, (p.source["id"], "tts"))
        assert row is not None
        return SimpleNamespace(
            revision=row.revision,
            pin=None if row.input_artifact_id is None else {
                "version": 1, "artifact_id": row.input_artifact_id, "content_hash": row.input_content_hash,
            },
            binding=dict(row.binding_json),
        )


def input_paths(p):
    with p.r.f.case.database.snapshot_session() as session:
        return session.scalars(select(m.Artifact.relative_path).where(
            m.Artifact.session_id == p.source["id"],
            m.Artifact.role.in_(["speech_input", "speech_reference_audio"]),
        )).all()


def test_member_save_peer_edit_and_exact_retry_survive_original_voice_deletion(project, monkeypatch):
    p, r = project, project.r
    first = save(p)
    assert first.headers["ETag"] == '"1"'
    first_body = body(first)
    selected = reference(p)
    assert selected.pin is not None and selected.binding["voice"] == "workshop-reference"
    files = input_paths(p)
    assert len(files) == 2
    with r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, r.sid))
        session.delete(session.get_one(m.Voice, r.voice["id"]))
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input",
        lambda *_a, **_k: pytest.fail("Saved project voice reread its original library"),
    )
    peer = save(p, {"max_attempts": 2}, revision=1, patch=True,
        key="peer-retained-project-edit", client=r.f.users.bob, auth=r.f.users.bh)
    assert body(peer)["revision"] == 2
    assert reference(p).pin == selected.pin and reference(p).revision == selected.revision
    replay = save(p)
    assert body(replay) == first_body and replay.headers["Idempotency-Replayed"] == "true"
    loaded = speech_inputs.load_speech_input(
        r.f.case.database, r.f.services.paths, selected.pin,
        {"service": "audio_cpp", "model": MODEL, "voice": "workshop-reference"},
        owner=SpeechInputOwner(r.f.users.workspace_id, p.source["project_id"], p.source["id"]),
        before_io=lambda: None,
    )
    assert loaded["audio_cpp_reference_text"] == "The selected original reference."
    assert all(r.f.services.paths.managed_path(path).is_file() for path in files)
    assert "speech_input" not in first_body and "binding_json" not in first_body


def test_project_credential_reuses_retention_but_cannot_read_library_for_a_new_choice(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    _token, bearer = scoped_token(r.f.case, r.f.users.alice_id,
        {"app.read", "app.write"}, project=p.source["project_id"])
    monkeypatch.setattr(
        "pandrator.web.project_speech_reference_capture.capture_speech_input",
        lambda *_a, **_k: pytest.fail("Project credential reached a protected library body"),
    )
    assert body(save(p, {"max_attempts": 2}, patch=True, revision=1,
        key="project-token-retained", auth=bearer))["revision"] == 2
    denied = save(p, {"voice": "a-new-reference"}, patch=True, revision=2,
        key="project-token-new-reference", auth=bearer)
    assert denied.status_code == 404, denied.get_json()
    assert body(r.f.users.alice.get(p.path))["revision"] == 2


def test_late_source_change_rolls_back_settings_and_removes_unpublished_files(project, monkeypatch):
    p, r = project, project.r
    original_prepare = writer.prepare_project_speech_reference
    created = []

    def change_after_prepare(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        assert prepared is not None
        created.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Voice, r.voice["id"]).revision += 1
        return prepared

    monkeypatch.setattr(writer, "prepare_project_speech_reference", change_after_prepare)
    response = save(p)
    assert response.status_code in {409, 422}, response.get_json()
    assert body(r.f.users.alice.get(p.path))["revision"] == 0
    assert input_paths(p) == []
    assert created and all(not r.f.services.paths.managed_path(path).exists() for path in created)
    assert r.path.is_file()


def test_committed_project_settings_and_files_survive_late_error_and_replay(project, monkeypatch):
    p, r = project, project.r
    original_writer = r.f.case.database.immediate_session
    original_publish = writer.publish_project_speech_reference
    published, failed = False, False

    def publish(*args, **kwargs):
        nonlocal published
        result = original_publish(*args, **kwargs)
        published = True
        return result

    @contextmanager
    def late_error(**kwargs):
        nonlocal failed
        with original_writer(**kwargs) as session:
            yield session
        if published and not failed:
            failed = True
            raise RuntimeError("synthetic error after project voice commit")

    monkeypatch.setattr(writer, "publish_project_speech_reference", publish)
    monkeypatch.setattr(r.f.case.database, "immediate_session", late_error)
    with pytest.raises(RuntimeError, match="after project voice commit"):
        save(p)
    assert body(r.f.users.alice.get(p.path))["revision"] == 1
    assert reference(p).pin is not None
    assert len(input_paths(p)) == 2
    assert all(r.f.services.paths.managed_path(path).is_file() for path in input_paths(p))
    monkeypatch.setattr(writer, "capture_project_speech_reference",
        lambda *_a, **_k: pytest.fail("Committed retry touched the voice source"))
    assert save(p).headers["Idempotency-Replayed"] == "true"


def test_concurrent_exact_retry_keeps_one_graph_and_cleans_losing_preparation(project, monkeypatch):
    p, r = project, project.r
    original_prepare = writer.prepare_project_speech_reference
    entered = False
    loser = []
    winner = []

    def prepare(*args, **kwargs):
        nonlocal entered
        prepared = original_prepare(*args, **kwargs)
        assert prepared is not None
        if not entered:
            entered = True
            loser.extend([prepared.audio.relative_path, prepared.snapshot.relative_path])
            # A second HTTP request needs its own Flask g and actor lifetime.
            with execution_actor_scope(None), r.f.case.app.app_context():
                winner.append(body(save(p)))
        return prepared

    monkeypatch.setattr(writer, "prepare_project_speech_reference", prepare)
    response = save(p)
    assert response.headers["Idempotency-Replayed"] == "true" and body(response) == winner[0]
    assert len(input_paths(p)) == 2 and reference(p).revision == 1
    assert loser and all(not r.f.services.paths.managed_path(path).exists() for path in loser)


def test_no_reference_selection_stays_explicit_after_a_later_library_alias(project):
    p, r = project, project.r
    body(save(p, {"service": "audio_cpp", "model": MODEL, "voice": "future-reference"}))
    assert reference(p).pin is None and input_paths(p) == []
    with r.f.case.database.immediate_session() as session:
        voice = session.get_one(m.Voice, r.voice["id"])
        metadata = dict(voice.metadata_json)
        metadata["providers"] = {"audio_cpp": {
            "status": "ready", "resource_kind": "linked_reference", "voice_id": "future-reference",
        }}
        voice.metadata_json = metadata
    body(save(p, {"max_attempts": 2}, patch=True, revision=1, key="keep-no-reference"))
    assert reference(p).pin is None and input_paths(p) == []


def test_service_switch_clears_saved_binding_and_preserves_historical_inputs(project):
    p, r = project, project.r
    body(save(p))
    files = input_paths(p)
    body(save(p, {"service": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore"},
        revision=1, key="switch-from-retained"))
    with r.f.case.database.snapshot_session() as session:
        assert session.get(m.ProjectSpeechReference, (p.source["id"], "tts")) is None
    assert len(files) == 2 and all(r.f.services.paths.managed_path(path).is_file() for path in files)


def test_put_empty_restores_inherited_voice_without_reusing_the_replaced_override(project):
    p, r = project, project.r
    body(put(r.f.users.owner, r.f.users.oh, "defaults.tts",
        {"service": "audio_cpp", "model": MODEL, "voice": "workshop-reference"},
        workspace=r.f.users.workspace_id))
    body(save(p, {"service": "audio_cpp", "model": MODEL, "voice": "temporary-voice"}))
    assert reference(p).pin is None
    restored = body(save(p, {}, revision=1, key="restore-inherited-voice"))
    # The established voice-mode migration flag remains explicit; voice/model
    # choices themselves must resume inheritance after a replacing empty PUT.
    assert set(restored["override"]) <= {"voice_mode_version"}
    assert restored["effective"]["voice"] == "workshop-reference"
    assert reference(p).pin is not None
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.ProjectSpeechReference)) == 1


def admit_generation(p):
    r = p.r
    plan = r.f.services.generation.create_plan(
        p.source["id"], source_revision_id=None, settings={},
        segments=[{"text": "A retained project narration sentence.", "language": "en"}],
    )
    handlers = r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(r.f.services.jobs, "retained-project-generation", handlers.handlers())
    queued = body(r.f.users.alice.post(
        f"/api/v1/sessions/{p.source['id']}/generation-admissions", json={},
        headers=headers(r.f.users.ah, "retained-generation-admission"),
    ), 202)
    assert worker.run_once()
    admission = r.f.services.jobs.get(queued["id"])
    assert admission.status == "succeeded", admission.error_message
    return SimpleNamespace(worker=worker, handlers=handlers, plan=plan, result=admission.result_json)


def test_registered_generation_uses_retained_bytes_after_original_voice_and_file_deletion(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    selected = reference(p)
    g = admit_generation(p)
    with r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, r.sid))
        session.delete(session.get_one(m.Voice, r.voice["id"]))
    r.path.unlink()
    monkeypatch.setattr(g.handlers, "prepare_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("Retained generation called its legacy library reader"))
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index",
        lambda *_a, **_k: pytest.fail("Retained generation indexed the live library"))
    calls = transport(monkeypatch)
    assert g.worker.run_once()
    result = r.f.services.jobs.get(g.result["job_id"])
    assert result.status == "succeeded", result.error_message
    assert len(calls) == 1
    with r.f.case.database.snapshot_session() as session:
        take = session.scalar(select(m.AudioTake).where(
            m.AudioTake.generation_run_id == g.result["generation_run_id"],
        ))
        assert take is not None and take.artifact_id is not None
        assert selected.pin is not None
        edge = session.get(m.ArtifactEdge, (selected.pin["artifact_id"], take.artifact_id))
        assert edge is not None and edge.relation == "derived_from"
        run = session.get_one(m.GenerationRun, g.result["generation_run_id"])
        assert run.settings_snapshot_json["tts"]["_project_speech_input"] == selected.pin
        artifact_id = take.artifact_id
    peer = r.f.users.bob.get(f"/api/v1/artifacts/{artifact_id}/content")
    assert peer.status_code == 200 and peer.data.startswith(b"RIFF")
    public = body(r.f.users.bob.get(f"/api/v1/generation-runs/{g.result['generation_run_id']}"))
    assert "_project_speech_input" not in str(public) and "base64" not in str(public)


@pytest.mark.parametrize("when", ["before_synthesis", "after_provider"])
def test_retained_graph_retirement_prevents_new_take_publication(project, monkeypatch, when):
    p, r = project, project.r
    body(save(p))
    selected = reference(p)
    assert selected.pin is not None
    g = admit_generation(p)

    def retire():
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, selected.pin["artifact_id"]).state = "deleted"

    if when == "before_synthesis":
        retire()
    calls = transport(monkeypatch, after_send=retire if when == "after_provider" else None)
    assert g.worker.run_once()
    result = r.f.services.jobs.get(g.result["job_id"])
    assert result.status == "failed"
    assert len(calls) == (1 if when == "after_provider" else 0)
    with r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(m.AudioTake.id).where(
            m.AudioTake.generation_run_id == g.result["generation_run_id"],
        )) is None


def test_project_purge_removes_its_retained_inputs_and_preserves_the_reusable_library(project):
    p, r = project, project.r
    body(save(p))
    files = input_paths(p)
    with r.f.case.database.snapshot_session() as session:
        revision = session.get_one(m.SessionRecord, p.source["id"]).revision
    r.f.services.sessions.trash(p.source["id"], revision)
    purge = SessionPurgeService(r.f.case.database, r.f.services.paths)
    preview = purge.preview(p.source["id"])
    assert preview["can_purge"], preview
    result = purge.purge(p.source["id"], preview["revision"], preview["impact_token"])
    assert result["state"] == "complete", result
    with r.f.case.database.snapshot_session() as session:
        assert session.get(m.SessionRecord, p.source["id"]) is None
        assert session.get(m.ProjectSpeechReference, (p.source["id"], "tts")) is None
        assert session.get(m.Project, p.source["project_id"]) is None
        assert session.get(m.Voice, r.voice["id"]) is not None
        assert session.get(m.VoiceSample, r.sid) is not None
    assert files and all(not r.f.services.paths.managed_path(path).exists() for path in files)
    assert r.path.is_file()


@pytest.mark.parametrize("retained_choice", [True, False])
def test_admitted_generation_survives_logout_with_saved_or_legacy_reference(project, monkeypatch, retained_choice):
    p, r = project, project.r
    if retained_choice:
        body(save(p))
    else:
        # Trusted old-session fixture: bypass the new HTTP capture adapter to
        # exercise its explicit compatibility fallback under a captured actor.
        r.f.services.workspace_settings.update(p.source["id"], "tts", 0,
            {"service": "audio_cpp", "model": MODEL, "voice": "workshop-reference", "language": "en"})
    g = admit_generation(p)
    assert r.f.users.alice.post("/api/v1/auth/logout", headers=r.f.users.ah).status_code == 200
    calls = transport(monkeypatch)
    assert g.worker.run_once()
    result = r.f.services.jobs.get(g.result["job_id"])
    assert result.status == "succeeded", result.error_message
    assert len(calls) == 1


def test_project_only_admission_uses_owned_reference_without_workspace_voice_bodies(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    r.f.services.generation.create_plan(p.source["id"], source_revision_id=None, settings={},
        segments=[{"text": "A project credential boundary.", "language": "en"}])
    _token, bearer = scoped_token(r.f.case, r.f.users.alice_id,
        {"app.read", "app.write", "app.run"}, project=p.source["project_id"])
    protected_reads = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "voices.metadata_json" in statement or "voice_samples.transcript" in statement:
            protected_reads.append(statement)

    event.listen(r.f.case.database.engine, "before_cursor_execute", observe)
    try:
        response = r.f.users.alice.post(
            f"/api/v1/sessions/{p.source['id']}/generation-admissions", json={},
            headers=headers(bearer, "project-only-admission-source-boundary"),
        )
        queued = body(response, 202)
        handlers = r.f.case.app.extensions["pandrator"]["workflow_handlers"]
        worker = Worker(r.f.services.jobs, "project-only-retained-generation", handlers.handlers())
        calls = transport(monkeypatch)
        assert worker.run_once()
        admission = r.f.services.jobs.get(queued["id"])
        assert admission.status == "succeeded", admission.error_message
        assert worker.run_once()
        generated = r.f.services.jobs.get(admission.result_json["job_id"])
        assert generated.status == "succeeded", generated.error_message
        assert len(calls) == 1
    finally:
        event.remove(r.f.case.database.engine, "before_cursor_execute", observe)
    assert protected_reads == []


def test_historical_managed_id_override_cannot_reuse_the_previous_voice_waveform(project, monkeypatch):
    from uuid import uuid4

    p, r = project, project.r
    body(save(p, {"service": "audio_cpp", "model": MODEL,
        "voice_id": r.voice["id"], "voice": "workshop-reference", "language": "en"}))
    g = admit_generation(p)
    calls = transport(monkeypatch)
    assert g.worker.run_once()
    assert r.f.services.jobs.get(g.result["job_id"]).status == "succeeded"
    segment = r.f.services.generation.list_segments(p.source["id"])["items"][0]
    missing_voice_id = str(uuid4())
    response = r.f.users.alice.post(
        f"/api/v1/sessions/{p.source['id']}/generation-runs",
        json={"settings_source": "run", "settings_source_run_id": g.result["generation_run_id"],
            "segment_ids": [segment["id"]], "run_override": {"tts": {"voice_id": missing_voice_id}}},
        headers=headers(r.f.users.ah, "different-historical-managed-reference"),
    )
    assert response.status_code == 409, response.get_json()
    assert "Managed cast voice no longer exists" in str(response.get_json())
    assert missing_voice_id in str(response.get_json())
    assert len(calls) == 1


def test_historical_voice_pin_survives_current_project_reassignment_and_original_deletion(project, monkeypatch):
    p, r = project, project.r
    body(save(p))
    old = reference(p)
    g = admit_generation(p)
    calls = transport(monkeypatch)
    assert g.worker.run_once()
    assert r.f.services.jobs.get(g.result["job_id"]).status == "succeeded"
    body(save(p, {"voice": "new-project-voice"}, revision=1, patch=True,
        key="reassign-current-project-voice"))
    assert reference(p).pin is None
    with r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, r.sid))
        session.delete(session.get_one(m.Voice, r.voice["id"]))
    r.path.unlink()
    segment = r.f.services.generation.list_segments(p.source["id"])["items"][0]
    response = r.f.users.alice.post(
        f"/api/v1/sessions/{p.source['id']}/generation-runs",
        json={"settings_source": "run", "settings_source_run_id": g.result["generation_run_id"],
            "segment_ids": [segment["id"]]},
        headers=headers(r.f.users.ah, "retain-historical-project-voice"),
    )
    started = body(response, 202)
    assert g.worker.run_once()
    generated = r.f.services.jobs.get(started["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 2
    with r.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, started["id"])
        assert run.settings_snapshot_json["tts"]["_project_speech_input"] == old.pin
