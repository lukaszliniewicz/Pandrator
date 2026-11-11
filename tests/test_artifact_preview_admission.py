"""Current artifact authority gates all derivative preview admission."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, func, select, text

from pandrator.web import video_previews
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.accounts.actor_authorization import ActorAuthorizationDenied
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import UserActor, current_execution_actor
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.models import Artifact, ArtifactEdge, Job, new_id
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.video_previews import PREVIEW_ROLE, PREVIEW_VERSION
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential, error, grant_workspace_content_access
from tests.test_job_access_routes import no_network as no_network

PREVIEWS = (
    {
        "name": "audio",
        "path": "audio-preview",
        "kind": "audio",
        "suffix": ".wav",
        "job_kind": "audio.preview",
    },
    {
        "name": "video",
        "path": "video-preview",
        "kind": "video",
        "suffix": ".mp4",
        "job_kind": "video.preview",
    },
    {
        "name": "waveform",
        "path": "waveform",
        "kind": "audio",
        "suffix": ".wav",
        "job_kind": "audio.waveform",
    },
)


@pytest.fixture
def media_mocks(case, monkeypatch):
    probe = Mock(return_value=None)
    inline = Mock(return_value=None)
    monkeypatch.setattr("pandrator.web.video_previews.probe_video", probe)
    monkeypatch.setattr(case.services.workflow_handlers, "generate_waveform", inline)
    return SimpleNamespace(probe=probe, inline=inline)


def _scope(case, name):
    if name in {"a", "b"}:
        return {"session_id": getattr(case, name).id}
    if name == "team":
        return {"workspace_id": case.team}
    if name == "editor_personal":
        return {"workspace_id": case.personal[case.users["editor"]]}
    raise AssertionError(f"Unknown fixture scope: {name}")


def _create_artifact(
    case,
    *,
    preview,
    scope="a",
    kind=None,
    suffix=None,
    role="source",
    content=b"synthetic managed media",
    metadata=None,
    parent_ids=None,
):
    path = case.services.paths.artifacts / f"{new_id()}{suffix or preview['suffix']}"
    path.write_bytes(content)
    artifact = case.services.artifacts.register(
        path,
        kind=kind or preview["kind"],
        role=role,
        parent_ids=parent_ids,
        metadata=metadata,
        **_scope(case, scope),
    )
    return artifact, path


def _source(case, preview, *, scope="a"):
    return _create_artifact(case, preview=preview, scope=scope, role="source")


def _url(preview, artifact_id, *, bounded=False):
    url = f"/api/v1/artifacts/{artifact_id}/{preview['path']}"
    if preview["name"] == "waveform" and bounded:
        return f"{url}?start_ms=0&end_ms=1000"
    return url


def _payload(preview, source, *, bounded=False):
    payload = {"source_artifact_id": source.id}
    if preview["name"] == "video":
        payload.update(
            source_content_hash=source.content_hash,
            preview_version=PREVIEW_VERSION,
        )
    elif preview["name"] == "waveform":
        payload.update(
            max_points=1600,
            start_ms=0,
            end_ms=1000 if bounded else None,
        )
    return payload


def _resource_keys(preview, source):
    if preview["name"] == "audio":
        return [f"artifact:audio-preview:{source.id}"]
    if preview["name"] == "video":
        return [f"artifact:video-preview:{source.id}"]
    return (
        [f"session:{source.session_id}"]
        if source.session_id
        else [f"artifact:waveform:{source.id}"]
    )


def _preview_job(case, preview, source, *, bounded=False, author="editor"):
    session_id = None if preview["name"] == "audio" else source.session_id
    identifier = new_id()
    with case.database.immediate_session() as session:
        row = Job(
            id=identifier,
            kind=preview["job_kind"],
            workspace_id=source.workspace_id,
            project_id=source.project_id,
            session_id=session_id,
            status="queued",
            payload_json=_payload(preview, source, bounded=bounded),
            resource_keys_json=_resource_keys(preview, source),
        )
        session.add(row)
        session.flush()
        session.add(
            JobActorRecord(
                job_id=identifier,
                user_id=case.users[author],
                auth_revision=1,
                context_json={"version": 1, "source": "user", "subject": "preview fixture"},
            )
        )
    return identifier


def _cached_child(case, preview, source, *, scope="a", bounded=False):
    if preview["name"] == "audio":
        return _create_artifact(
            case,
            preview=preview,
            scope=scope,
            kind="audio",
            suffix=".mp3",
            role="source_audio_preview",
            content=b"synthetic cached audio preview",
            parent_ids=[source.id],
        )
    if preview["name"] == "video":
        return _create_artifact(
            case,
            preview=preview,
            scope=scope,
            kind="video",
            suffix=".mp4",
            role=PREVIEW_ROLE,
            content=b"synthetic cached video preview",
            metadata={
                "source_artifact_id": source.id,
                "source_content_hash": source.content_hash,
                "preview_version": PREVIEW_VERSION,
            },
            parent_ids=[source.id],
        )
    return _create_artifact(
        case,
        preview=preview,
        scope=scope,
        kind="json",
        suffix=".json",
        role="waveform_peaks_window" if bounded else "waveform_peaks",
        content=(b'{"duration_ms":1000,"start_ms":0,"end_ms":1000,"channels":1,"points":[0.5]}'),
        metadata={
            "max_points": 1600,
            "start_ms": 0,
            **({"end_ms": 1000} if bounded else {}),
        },
        parent_ids=[source.id],
    )


def _job_count(case):
    with case.database.snapshot_session() as session:
        return session.scalar(select(func.count()).select_from(Job))


def _assert_child_scope_and_edge(case, source, child):
    assert (child.workspace_id, child.project_id, child.session_id) == (
        source.workspace_id,
        source.project_id,
        source.session_id,
    )
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(ArtifactEdge.child_artifact_id).where(
                    ArtifactEdge.parent_artifact_id == source.id,
                    ArtifactEdge.child_artifact_id == child.id,
                )
            )
            == child.id
        )


def _block_any_path(case, monkeypatch):
    managed_path = Mock(side_effect=AssertionError("Denied preview must not resolve a path."))
    video_path = Mock(side_effect=AssertionError("Denied preview must not resolve a path."))
    monkeypatch.setattr(type(case.services.paths), "managed_path", managed_path)
    monkeypatch.setattr("pandrator.web.video_previews._managed_file", video_path)
    return managed_path, video_path


def _forbid_enqueue(case, monkeypatch):
    enqueue = Mock(side_effect=AssertionError("Denied preview must not enqueue a job."))
    monkeypatch.setattr(case.services.jobs, "enqueue_in_session", enqueue)
    return enqueue


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_unknown_hidden_and_deleted_sources_deny_before_preview_effects(
    case, monkeypatch, media_mocks, preview
):
    viewer = credential(case, "viewer")
    hidden, _ = _source(case, preview, scope="b")
    deleted, _ = _source(case, preview)
    with case.database.immediate_session() as session:
        session.get(Artifact, deleted.id).state = "deleted"
    paths = _block_any_path(case, monkeypatch)
    enqueue = _forbid_enqueue(case, monkeypatch)

    hidden_response = viewer.client.get(_url(preview, hidden.id), headers=viewer.headers)
    missing_response = viewer.client.get(_url(preview, new_id()), headers=viewer.headers)
    deleted_response = viewer.client.get(_url(preview, deleted.id), headers=viewer.headers)

    assert hidden_response.status_code == missing_response.status_code == 404
    assert deleted_response.status_code == 404
    assert error(hidden_response) == error(missing_response) == error(deleted_response)
    paths[0].assert_not_called()
    paths[1].assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    enqueue.assert_not_called()
    assert _job_count(case) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
@pytest.mark.parametrize(
    ("actor_name", "scopes"),
    [("viewer", None), ("editor", frozenset({"app.read"}))],
)
def test_cache_miss_requires_current_run_before_inline_or_queue(
    case, monkeypatch, media_mocks, preview, actor_name, scopes
):
    actor = credential(case, actor_name, scopes=scopes) if scopes else credential(case, actor_name)
    source, _ = _source(case, preview)
    enqueue = _forbid_enqueue(case, monkeypatch)

    response = actor.client.post(
        _url(preview, source.id, bounded=preview["name"] == "waveform"),
        headers=actor.headers,
    )

    assert response.status_code == 403, response.get_json()
    assert media_mocks.inline.called is False
    enqueue.assert_not_called()
    assert _job_count(case) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_editor_cache_miss_enqueues_with_actual_roots_payload_and_actor(case, media_mocks, preview):
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor")
    source, _ = _source(case, preview)

    response = editor.client.post(_url(preview, source.id), headers=editor.headers)

    assert response.status_code == 202, response.get_json()
    identifier = response.get_json()["job_id"]
    with case.database.snapshot_session() as session:
        job = session.get(Job, identifier)
        actor = session.get(JobActorRecord, identifier)
        assert job.kind == preview["job_kind"]
        assert (job.workspace_id, job.project_id) == (case.team, case.a.project_id)
        assert job.session_id == (None if preview["name"] == "audio" else source.session_id)
        assert job.payload_json == _payload(preview, source)
        assert job.resource_keys_json == _resource_keys(preview, source)
        assert actor.user_id == case.users["editor"]
    assert response.get_json()["status"] == "queued"
    media_mocks.inline.assert_not_called()


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
@pytest.mark.parametrize("observation", ["existing", "cache"])
@pytest.mark.parametrize("actor_name", ["viewer", "editor"])
def test_read_only_user_can_observe_authorized_existing_job_or_cache(
    case, media_mocks, preview, observation, actor_name
):
    reader = credential(case, actor_name, scopes=frozenset({"app.read"}))
    bounded = preview["name"] == "waveform"
    source, _ = _source(case, preview)
    if observation == "existing":
        expected_job = _preview_job(case, preview, source, bounded=bounded)
        original_count = 1
    else:
        cached, _ = _cached_child(case, preview, source, bounded=bounded)
        _assert_child_scope_and_edge(case, source, cached)
        original_count = 0

    response = reader.client.get(_url(preview, source.id, bounded=bounded), headers=reader.headers)

    if observation == "existing":
        assert response.status_code == 202, response.get_json()
        assert response.get_json() == {"status": "queued", "job_id": expected_job}
    elif preview["name"] == "waveform":
        assert response.status_code == 200
        assert response.get_json() == {
            "duration_ms": 1000,
            "start_ms": 0,
            "end_ms": 1000,
            "channels": 1,
            "points": [0.5],
        }
    else:
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["status"] == "ready"
        assert response.get_json()["artifact_id"] == cached.id
        if preview["name"] == "video":
            assert cached.metadata_json["source_content_hash"] == source.content_hash
            assert cached.metadata_json["source_artifact_id"] == source.id
            assert cached.metadata_json["preview_version"] == PREVIEW_VERSION

    assert _job_count(case) == original_count
    media_mocks.inline.assert_not_called()


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_cross_project_cache_edges_and_poisoned_pending_jobs_do_not_leak(
    case, monkeypatch, media_mocks, preview
):
    viewer = credential(case, "viewer")
    source, _ = _source(case, preview)
    foreign_child, _ = _cached_child(
        case, preview, source, scope="b", bounded=preview["name"] == "waveform"
    )
    foreign_source, _ = _source(case, preview, scope="b")
    foreign_job = _preview_job(
        case,
        preview,
        foreign_source,
        bounded=preview["name"] == "waveform",
    )
    with case.database.immediate_session() as session:
        session.execute(
            text("UPDATE artifacts SET metadata_json = :value WHERE id = :id"),
            {"value": "invalid hidden artifact metadata sentinel", "id": foreign_child.id},
        )
        session.execute(
            text("UPDATE jobs SET payload_json = :value WHERE id = :id"),
            {"value": "invalid hidden preview payload sentinel", "id": foreign_job},
        )

    opened = []
    original_managed_path = type(case.services.paths).managed_path

    def managed_path(paths, relative):
        value = str(relative)
        opened.append(value)
        if value == foreign_child.relative_path:
            raise AssertionError("A hidden cached child path was opened.")
        return original_managed_path(paths, relative)

    original_video_path = video_previews._managed_file

    def video_path(paths, relative):
        value = str(relative)
        opened.append(value)
        if value == foreign_child.relative_path:
            raise AssertionError("A hidden cached child path was opened.")
        return original_video_path(paths, relative)

    monkeypatch.setattr(type(case.services.paths), "managed_path", managed_path)
    monkeypatch.setattr("pandrator.web.video_previews._managed_file", video_path)
    enqueue = _forbid_enqueue(case, monkeypatch)

    response = viewer.client.get(
        _url(preview, source.id, bounded=preview["name"] == "waveform"),
        headers=viewer.headers,
    )

    assert response.status_code == 409, response.get_json()
    error_body = response.get_json()["error"]
    assert error_body["code"] == "preview_preparation_required"
    assert error_body["details"]["can_prepare"] is False
    assert foreign_child.relative_path not in opened
    assert foreign_child.id not in response.get_data(as_text=True)
    assert foreign_job not in response.get_data(as_text=True)
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
@pytest.mark.parametrize("revocation", ["membership", "workspace_grant"])
def test_authority_revoked_after_guard_is_rechecked_before_paths_or_jobs(
    case, monkeypatch, media_mocks, preview, revocation
):
    editor = credential(case, "editor")
    source, _ = _source(case, preview)
    real_access_context = ApiGuards.access_context

    def revoke_before_policy(guards):
        access = real_access_context(guards)
        with case.database.immediate_session() as session:
            if revocation == "membership":
                session.execute(
                    delete(ProjectMembership).where(
                        ProjectMembership.project_id == case.a.project_id,
                        ProjectMembership.user_id == case.users["editor"],
                    )
                )
            else:
                session.execute(
                    delete(ApiTokenWorkspaceGrant).where(
                        ApiTokenWorkspaceGrant.token_id == editor.token.id,
                        ApiTokenWorkspaceGrant.workspace_id == case.team,
                    )
                )
        return access

    monkeypatch.setattr(ApiGuards, "access_context", revoke_before_policy)
    paths = _block_any_path(case, monkeypatch)
    enqueue = _forbid_enqueue(case, monkeypatch)

    response = editor.client.get(
        _url(preview, source.id, bounded=preview["name"] == "waveform"),
        headers=editor.headers,
    )

    assert response.status_code == 404, response.get_json()
    paths[0].assert_not_called()
    paths[1].assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    enqueue.assert_not_called()
    assert _job_count(case) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_noninitial_personal_owner_can_run_own_rootless_preview(case, media_mocks, preview):
    editor = credential(case, "editor", grant_team=False)
    source, _ = _source(case, preview, scope="editor_personal")

    response = editor.client.post(_url(preview, source.id), headers=editor.headers)

    assert response.status_code == 202, response.get_json()
    with case.database.snapshot_session() as session:
        job = session.get(Job, response.get_json()["job_id"])
        actor = session.get(JobActorRecord, job.id)
        assert (job.workspace_id, job.project_id, job.session_id) == (
            case.personal[case.users["editor"]],
            None,
            None,
        )
        assert job.payload_json == _payload(preview, source)
        assert actor.user_id == case.users["editor"]


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
@pytest.mark.parametrize("actor_name", ["viewer", "outsider"])
def test_team_rootless_artifact_is_hidden_from_members_and_instance_admins(
    case, monkeypatch, media_mocks, preview, actor_name
):
    if actor_name == "outsider":
        actor = credential(case, "outsider", scopes={"app.read", "app.run", "app.admin"})
    else:
        actor = credential(case, "viewer")
    source, _ = _source(case, preview, scope="team")
    paths = _block_any_path(case, monkeypatch)
    enqueue = _forbid_enqueue(case, monkeypatch)

    response = actor.client.get(_url(preview, source.id), headers=actor.headers)

    assert response.status_code == 404, response.get_json()
    paths[0].assert_not_called()
    paths[1].assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    enqueue.assert_not_called()
    assert _job_count(case) == 0


def test_bounded_waveform_inline_runs_after_writer_closes_with_actual_user(case, monkeypatch):
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor")
    source, _ = _source(case, PREVIEWS[2])
    actors = []
    handlers = case.services.workflow_handlers

    def generate(payload, _progress, _cancel_event):
        actor = current_execution_actor.get()
        assert isinstance(actor, UserActor)
        assert actor.user_id == case.users["editor"]
        actors.append(actor)
        path = case.services.paths.artifacts / f"{new_id()}.json"
        path.write_text(
            json.dumps(
                {
                    "duration_ms": 1000,
                    "start_ms": 0,
                    "end_ms": 1000,
                    "channels": 1,
                    "points": [0.5],
                }
            ),
            encoding="utf-8",
        )
        with case.database.immediate_session(busy_timeout_ms=0) as session:
            child = case.services.artifacts.register_in_session(
                session,
                path,
                kind="json",
                role="waveform_peaks_window",
                session_id=source.session_id,
                parent_ids=[source.id],
                settings={"max_points": 1600, "start_ms": 0, "end_ms": 1000},
                metadata={"max_points": 1600, "start_ms": 0, "end_ms": 1000},
            )
            child_id = child.id
        assert payload == _payload(PREVIEWS[2], source, bounded=True)
        return {"artifact_id": child_id}

    monkeypatch.setattr(handlers, "generate_waveform", generate)

    response = editor.client.post(
        _url(PREVIEWS[2], source.id, bounded=True), headers=editor.headers
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["points"] == [0.5]
    assert len(actors) == 1
    assert _job_count(case) == 0


@pytest.mark.parametrize("denial", ["artifact", "actor"])
def test_waveform_inline_authorization_error_is_not_converted_to_queued_work(
    case, monkeypatch, denial
):
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor")
    source, _ = _source(case, PREVIEWS[2])
    denied = (
        AccessDenied()
        if denial == "artifact"
        else ActorAuthorizationDenied("preview authority denied")
    )
    inline = Mock(side_effect=denied)
    monkeypatch.setattr(case.services.workflow_handlers, "generate_waveform", inline)

    response = editor.client.post(
        _url(PREVIEWS[2], source.id, bounded=True), headers=editor.headers
    )

    assert response.status_code == 403, response.get_json()
    inline.assert_called_once()
    assert _job_count(case) == 0


@pytest.mark.parametrize("actor_name,can_prepare", [("viewer", False), ("editor", True)])
def test_missing_waveform_cache_falls_through_without_sending_missing_file(
    case, monkeypatch, media_mocks, actor_name, can_prepare
):
    if actor_name == "editor":
        grant_workspace_content_access(case, "editor")
    actor = credential(case, actor_name)
    source, _ = _source(case, PREVIEWS[2])
    missing_cache, cache_path = _cached_child(case, PREVIEWS[2], source)
    cache_path.unlink()
    send_file = Mock(side_effect=AssertionError("A missing waveform cache must not be sent."))
    monkeypatch.setattr("pandrator.web.artifact_routes.send_file", send_file)

    response = actor.client.get(_url(PREVIEWS[2], source.id), headers=actor.headers)

    assert response.status_code == 409, response.get_json()
    error_body = response.get_json()["error"]
    assert error_body["code"] == "preview_preparation_required"
    assert error_body["details"]["can_prepare"] is can_prepare
    send_file.assert_not_called()
    media_mocks.inline.assert_not_called()
    assert missing_cache.id not in response.get_data(as_text=True)
    assert _job_count(case) == 0
