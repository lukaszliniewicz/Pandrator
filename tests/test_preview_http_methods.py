"""Safe preview reads and CSRF-protected explicit preparation controls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, Job, new_id
from pandrator.web.video_previews import PREVIEW_ROLE, PREVIEW_VERSION
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import credential, error, grant_workspace_content_access
from tests.test_job_access_routes import no_network as no_network
from tests.web_test_support import prepare_web_test_data_root

PREVIEWS = (
    {"name": "audio", "path": "audio-preview", "kind": "audio", "suffix": ".wav"},
    {"name": "video", "path": "video-preview", "kind": "video", "suffix": ".mp4"},
    {"name": "waveform", "path": "waveform", "kind": "audio", "suffix": ".wav"},
)
READERS = (
    ("editor", None, True),
    ("viewer", None, False),
    ("editor", frozenset({"app.read"}), False),
)


@pytest.fixture
def media_mocks(case, monkeypatch):
    probe = Mock(return_value=None)
    inline = Mock(return_value=None)
    monkeypatch.setattr("pandrator.web.video_previews.probe_video", probe)
    monkeypatch.setattr(case.services.workflow_handlers, "generate_waveform", inline)
    return SimpleNamespace(probe=probe, inline=inline)


def _reader(case, actor_name, scopes):
    if scopes is not None:
        return credential(case, actor_name, scopes=scopes)
    return credential(case, actor_name)


def _source(case, preview, *, session_name="a"):
    session = getattr(case, session_name)
    path = case.services.paths.artifacts / f"{new_id()}{preview['suffix']}"
    path.write_bytes(b"synthetic preview input")
    return case.services.artifacts.register(
        path, kind=preview["kind"], role="source", session_id=session.id
    )


def _url(preview, artifact_id, *, bounded=True):
    url = f"/api/v1/artifacts/{artifact_id}/{preview['path']}"
    return f"{url}?start_ms=0&end_ms=1000" if preview["name"] == "waveform" and bounded else url


def _cached_child(case, preview, source):
    path = case.services.paths.artifacts / f"{new_id()}.json"
    if preview["name"] == "audio":
        path = path.with_suffix(".mp3")
        path.write_bytes(b"synthetic audio preview")
        return case.services.artifacts.register(
            path,
            kind="audio",
            role="source_audio_preview",
            session_id=source.session_id,
            parent_ids=[source.id],
        )
    if preview["name"] == "video":
        path = path.with_suffix(".mp4")
        path.write_bytes(b"synthetic video preview")
        return case.services.artifacts.register(
            path,
            kind="video",
            role=PREVIEW_ROLE,
            session_id=source.session_id,
            parent_ids=[source.id],
            metadata={
                "source_artifact_id": source.id,
                "source_content_hash": source.content_hash,
                "preview_version": PREVIEW_VERSION,
            },
        )
    path.write_text(
        '{"duration_ms":1000,"start_ms":0,"end_ms":1000,"channels":1,"points":[0.5]}',
        encoding="utf-8",
    )
    return case.services.artifacts.register(
        path,
        kind="json",
        role="waveform_peaks_window",
        session_id=source.session_id,
        parent_ids=[source.id],
        metadata={"max_points": 1600, "start_ms": 0, "end_ms": 1000},
    )


def _job_count(database):
    with database.snapshot_session() as session:
        return session.scalar(select(func.count()).select_from(Job))


def _block_enqueue(case, monkeypatch):
    enqueue = Mock(side_effect=AssertionError("A safe or denied preview must not enqueue."))
    monkeypatch.setattr(case.services.jobs, "enqueue_in_session", enqueue)
    return enqueue


def _block_paths(case, monkeypatch):
    managed = Mock(side_effect=AssertionError("A hidden preview must not resolve a path."))
    video = Mock(side_effect=AssertionError("A hidden preview must not resolve a path."))
    monkeypatch.setattr(type(case.services.paths), "managed_path", managed)
    monkeypatch.setattr("pandrator.web.video_previews._managed_file", video)
    return managed, video


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
@pytest.mark.parametrize("actor_name,scopes,can_prepare", READERS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_get_and_head_misses_are_read_only_with_fresh_prepare_hint(
    case, monkeypatch, media_mocks, preview, actor_name, scopes, can_prepare, method
):
    if actor_name == "editor" and scopes is None:
        grant_workspace_content_access(case, "editor")
    actor = _reader(case, actor_name, scopes)
    source = _source(case, preview)
    enqueue = _block_enqueue(case, monkeypatch)

    response = getattr(actor.client, method.lower())(
        _url(preview, source.id), headers=actor.headers
    )

    assert response.status_code == 409, response.get_json() if method == "GET" else response.data
    if method == "GET":
        error_body = response.get_json()["error"]
        assert error_body["code"] == "preview_preparation_required"
        assert error_body["details"]["can_prepare"] is can_prepare
        assert error_body["details"]["required_scopes"] == ["app.read", "app.run"]
    else:
        assert response.data == b""
    enqueue.assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    assert _job_count(case.database) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_hidden_missing_and_deleted_source_reads_stay_404_before_effects(
    case, monkeypatch, media_mocks, preview, method
):
    viewer = credential(case, "viewer")
    hidden = _source(case, preview, session_name="b")
    deleted = _source(case, preview)
    with case.database.immediate_session() as session:
        session.get(Artifact, deleted.id).state = "deleted"
    paths = _block_paths(case, monkeypatch)
    enqueue = _block_enqueue(case, monkeypatch)
    call = getattr(viewer.client, method.lower())

    denied = call(_url(preview, hidden.id), headers=viewer.headers)
    missing = call(_url(preview, new_id()), headers=viewer.headers)
    deleted_response = call(_url(preview, deleted.id), headers=viewer.headers)

    assert denied.status_code == missing.status_code == deleted_response.status_code == 404
    if method == "GET":
        assert error(denied) == error(missing) == error(deleted_response)
    else:
        assert denied.data == missing.data == deleted_response.data == b""
    paths[0].assert_not_called()
    paths[1].assert_not_called()
    enqueue.assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    assert _job_count(case.database) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_post_with_read_only_scope_is_denied_before_media_or_job_effects(
    case, monkeypatch, media_mocks, preview
):
    reader = credential(case, "editor", scopes=frozenset({"app.read"}))
    source = _source(case, preview)
    paths = _block_paths(case, monkeypatch)
    enqueue = _block_enqueue(case, monkeypatch)

    response = reader.client.post(_url(preview, source.id), headers=reader.headers)

    assert response.status_code == 403, response.get_json()
    paths[0].assert_not_called()
    paths[1].assert_not_called()
    enqueue.assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    assert _job_count(case.database) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_viewer_cannot_post_even_when_a_current_cache_exists(
    case, monkeypatch, media_mocks, preview
):
    viewer = credential(case, "viewer")
    source = _source(case, preview)
    _cached_child(case, preview, source)
    paths = _block_paths(case, monkeypatch)
    enqueue = _block_enqueue(case, monkeypatch)

    response = viewer.client.post(_url(preview, source.id), headers=viewer.headers)

    assert response.status_code == 403, response.get_json()
    paths[0].assert_not_called()
    paths[1].assert_not_called()
    enqueue.assert_not_called()
    media_mocks.probe.assert_not_called()
    media_mocks.inline.assert_not_called()
    assert _job_count(case.database) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_editor_can_post_and_observe_a_cached_preview(case, media_mocks, preview):
    grant_workspace_content_access(case, "editor")
    editor = credential(case, "editor")
    source = _source(case, preview)
    cached = _cached_child(case, preview, source)

    response = editor.client.post(_url(preview, source.id), headers=editor.headers)

    assert response.status_code == 200, response.get_json()
    if preview["name"] == "waveform":
        assert response.get_json()["points"] == [0.5]
    else:
        assert response.get_json()["artifact_id"] == cached.id
    media_mocks.inline.assert_not_called()
    assert _job_count(case.database) == 0


@pytest.mark.parametrize("preview", PREVIEWS, ids=lambda row: row["name"])
def test_cookie_preparation_requires_bootstrap_csrf_and_records_real_user(
    tmp_path, monkeypatch, preview
):
    paths = prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    services = app.extensions["pandrator"]["services"]
    try:
        bootstrap_response = client.post(
            "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
        )
        csrf_token = bootstrap_response.get_json()["csrf_token"]
        user_id = services.auth.initial_account().user_id
        owner_session = services.sessions.create("Preview CSRF control")
        source_path = paths.sessions / owner_session.storage_key / f"{new_id()}{preview['suffix']}"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"synthetic cookie-auth preview input")
        source = services.artifacts.register(
            source_path,
            kind=preview["kind"],
            role="source",
            session_id=owner_session.id,
        )
        probe = Mock(return_value=None)
        inline = Mock(return_value=None)
        monkeypatch.setattr("pandrator.web.video_previews.probe_video", probe)
        monkeypatch.setattr(services.workflow_handlers, "generate_waveform", inline)
        url = _url(preview, source.id, bounded=False)

        safe_read = client.get(url)
        assert safe_read.status_code == 409, safe_read.get_json()
        assert safe_read.get_json()["error"]["details"]["can_prepare"] is True
        assert _job_count(services.database) == 0

        no_csrf = client.post(url)
        assert no_csrf.status_code == 403, no_csrf.get_json()
        assert _job_count(services.database) == 0
        probe.assert_not_called()
        inline.assert_not_called()

        prepared = client.post(url, headers={"X-CSRF-Token": csrf_token})
        assert prepared.status_code == 202, prepared.get_json()
        identifier = prepared.get_json()["job_id"]
        with services.database.snapshot_session() as session:
            job = session.get(Job, identifier)
            actor = session.get(JobActorRecord, identifier)
            assert actor.user_id == user_id
            assert (job.workspace_id, job.project_id) == (
                source.workspace_id,
                source.project_id,
            )
            assert job.session_id == (None if preview["name"] == "audio" else source.session_id)
            assert job.payload_json["source_artifact_id"] == source.id
        inline.assert_not_called()
    finally:
        services.close()
