"""Native HTTP/SQLite upload rollback and admission contracts."""

from __future__ import annotations

import errno
import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pandrator.web import upload_publication
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    ApiIdempotency,
    Artifact,
    ArtifactEdge,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    SourceRecord,
    UploadSessionRecord,
)
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.upload_activity import upload_activity
from tests.web_test_support import prepare_web_test_data_root


class PublicationFailure(RuntimeError):
    pass


@pytest.fixture
def workspace(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    response = client.post(
        "/api/v1/sessions",
        json={"name": "Upload contract", "workflow_kind": "audiobook"},
        headers=headers,
    )
    session_id = response.get_json()["id"]
    database = app.extensions["pandrator"]["database"]
    try:
        yield client, headers, session_id, database, paths
    finally:
        database.dispose()


def upload(workspace, *, filename="new.txt", session=True):
    client, headers, session_id, _, _ = workspace
    data = {"file": (io.BytesIO(b"new source bytes"), filename)}
    if session:
        data["session_id"] = session_id
    return client.post("/api/v1/uploads", data=data, headers=headers)


def snapshot(workspace):
    _, _, _, database, paths = workspace
    models = (
        Artifact,
        ArtifactEdge,
        SourceRecord,
        SourceAsset,
        SessionSource,
        SessionRecord,
        SessionSetting,
        UploadSessionRecord,
        ApiIdempotency,
    )
    with database.engine.connect() as connection:
        rows = {
            model.__tablename__: sorted(
                (tuple(row) for row in connection.execute(select(model.__table__))),
                key=repr,
            )
            for model in models
        }
    files = {
        path.relative_to(paths.root).as_posix(): path.read_bytes()
        for root in (paths.uploads, paths.temporary / "uploads")
        for path in root.rglob("*")
        if path.is_file()
    }
    directories = sorted(
        path.relative_to(paths.root).as_posix()
        for path in (paths.temporary / "uploads").glob("*")
        if path.is_dir()
    )
    return rows, files, directories


@pytest.mark.parametrize("fault", ["source_asset", "attachment", "commit"])
def test_multipart_failure_rolls_back_rows_files_and_previous_attachment(
    workspace, monkeypatch, fault
):
    assert upload(workspace, filename="previous.txt").status_code == 201
    before = snapshot(workspace)
    if fault == "source_asset":
        original = SourceLibraryService.ensure_for_artifact_in_session

        def fail(session, artifact_id, **kwargs):
            original(session, artifact_id, **kwargs)
            raise PublicationFailure("after source asset flush")

        monkeypatch.setattr(
            SourceLibraryService, "ensure_for_artifact_in_session", staticmethod(fail)
        )
    elif fault == "attachment":
        original = SourceLibraryService.attach_in_session

        def fail(self, session, *args, **kwargs):
            original(self, session, *args, **kwargs)
            session.flush()
            raise PublicationFailure("after attachment flush")

        monkeypatch.setattr(SourceLibraryService, "attach_in_session", fail)
    else:
        database = workspace[3]

        def fail(connection):
            if connection.scalar(
                select(SourceRecord.id).where(SourceRecord.display_name == "new.txt")
            ):
                raise PublicationFailure("native commit failure")

        event.listen(database.engine, "commit", fail)
    try:
        with pytest.raises(PublicationFailure):
            upload(workspace)
        assert snapshot(workspace) == before
        assert not list(workspace[4].temporary.glob("upload-*.part"))
    finally:
        if fault == "commit":
            event.remove(database.engine, "commit", fail)


@pytest.mark.parametrize("state", ["trashed", "purging"])
def test_multipart_rejects_inactive_owner_without_publication(workspace, state):
    with workspace[3].immediate_session() as session:
        session.get(SessionRecord, workspace[2]).status = state
    before = snapshot(workspace)
    response = upload(workspace)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_error"
    assert snapshot(workspace) == before


def test_multipart_rejects_concurrent_owner_activity(workspace):
    before = snapshot(workspace)
    with upload_activity(workspace[4], session_id=workspace[2]):
        response = upload(workspace)
    assert response.status_code == 422
    assert snapshot(workspace) == before
    assert not list(workspace[4].temporary.glob("upload-*.part"))


def test_multipart_rollback_preserves_foreign_replacement(workspace, monkeypatch):
    before = snapshot(workspace)
    original = SourceLibraryService.ensure_for_artifact_in_session
    replacement = workspace[4].temporary / "foreign.txt"
    replacement.write_bytes(b"foreign replacement")
    published_path = None

    def fail(session, artifact_id, **kwargs):
        nonlocal published_path
        original(session, artifact_id, **kwargs)
        artifact = session.get(Artifact, artifact_id)
        published_path = workspace[4].managed_path(artifact.relative_path)
        replacement.replace(published_path)
        raise PublicationFailure("after replacement by another owner")

    monkeypatch.setattr(SourceLibraryService, "ensure_for_artifact_in_session", staticmethod(fail))
    with pytest.raises(PublicationFailure):
        upload(workspace)
    after = snapshot(workspace)
    assert after[0] == before[0]
    assert published_path.read_bytes() == b"foreign replacement"
    assert not list(workspace[4].temporary.glob("upload-*.part"))


def test_cover_failure_preserves_previous_current_cover(workspace, monkeypatch):
    client, headers, session_id, _, _ = workspace

    def cover():
        body = io.BytesIO()
        Image.new("RGB", (24, 24)).save(body, format="PNG")
        body.seek(0)
        return client.post(
            "/api/v1/uploads",
            headers=headers,
            data={
                "file": (body, "cover.png"),
                "session_id": session_id,
                "purpose": "cover",
            },
        )

    assert cover().status_code == 201
    before = snapshot(workspace)
    original = ArtifactService.register_in_session

    def fail(self, session, *args, **kwargs):
        original(self, session, *args, **kwargs)
        session.flush()
        raise PublicationFailure("after replacing current cover record")

    monkeypatch.setattr(ArtifactService, "register_in_session", fail)
    with pytest.raises(PublicationFailure):
        cover()
    assert snapshot(workspace) == before


@pytest.mark.parametrize("publication_fails", [False, True])
def test_staging_cleanup_error_preserves_response_or_primary_failure(
    workspace, monkeypatch, publication_fails
):
    original_unlink = Path.unlink

    def fail_cleanup(self, *args, **kwargs):
        if self.parent == workspace[4].temporary and self.name.startswith("upload-"):
            raise PermissionError("native injected staging cleanup failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    if publication_fails:
        before = snapshot(workspace)
        original = SourceLibraryService.ensure_for_artifact_in_session

        def fail(session, artifact_id, **kwargs):
            original(session, artifact_id, **kwargs)
            raise PublicationFailure("primary publication failure")

        monkeypatch.setattr(
            SourceLibraryService, "ensure_for_artifact_in_session", staticmethod(fail)
        )
        with pytest.raises(PublicationFailure, match="primary publication failure"):
            upload(workspace)
        assert snapshot(workspace) == before
    else:
        response = upload(workspace)
        assert response.status_code == 201
        with workspace[3].session() as session:
            artifact = session.get(Artifact, response.get_json()["artifact_id"])
            assert (
                workspace[4].managed_path(artifact.relative_path).read_bytes()
                == b"new source bytes"
            )


@pytest.mark.parametrize("key", [None, "native-upload-init"])
@pytest.mark.parametrize("fault", ["flush", "commit"])
def test_chunk_initialization_failure_removes_owned_directory(workspace, monkeypatch, key, fault):
    client, headers, session_id, database, _ = workspace
    before = snapshot(workspace)
    if fault == "flush":
        original = Session.flush

        def fail(self, *args, **kwargs):
            initializing = any(isinstance(row, UploadSessionRecord) for row in self.new)
            original(self, *args, **kwargs)
            if initializing:
                raise PublicationFailure("after upload record flush")

        monkeypatch.setattr(Session, "flush", fail)
    else:

        def fail(connection):
            if connection.scalar(select(UploadSessionRecord.id).limit(1)):
                raise PublicationFailure("native chunk initialization commit failure")

        event.listen(database.engine, "commit", fail)
    try:
        with pytest.raises(PublicationFailure):
            client.post(
                "/api/v1/uploads/init",
                json={"session_id": session_id, "filename": "new.txt", "size_bytes": 16},
                headers={**headers, **({"Idempotency-Key": key} if key else {})},
            )
        assert snapshot(workspace) == before
    finally:
        if fault == "commit":
            event.remove(database.engine, "commit", fail)


@pytest.mark.parametrize("key", [None, "native-foreign-directory"])
def test_chunk_initialization_failure_preserves_foreign_directory(workspace, monkeypatch, key):
    client, headers, session_id, database, paths = workspace
    before = snapshot(workspace)
    original = Session.flush
    replacement = None

    def fail(self, *args, **kwargs):
        nonlocal replacement
        record = next((row for row in self.new if isinstance(row, UploadSessionRecord)), None)
        original(self, *args, **kwargs)
        if record is not None:
            replacement = paths.managed_path(record.temporary_relative_path)
            replacement.rename(paths.temporary / f"replaced-{record.id}")
            replacement.mkdir()
            (replacement / "sentinel.txt").write_bytes(b"foreign directory")
            raise PublicationFailure("after directory replacement")

    monkeypatch.setattr(Session, "flush", fail)
    with pytest.raises(PublicationFailure):
        client.post(
            "/api/v1/uploads/init",
            json={
                "session_id": session_id,
                "filename": "new.txt",
                "size_bytes": 16,
            },
            headers={**headers, **({"Idempotency-Key": key} if key else {})},
        )
    assert snapshot(workspace)[0] == before[0]
    assert (replacement / "sentinel.txt").read_bytes() == b"foreign directory"
    with database.session() as session:
        assert session.scalar(select(UploadSessionRecord.id)) is None


@pytest.mark.parametrize("with_session", [False, True])
def test_multipart_success_preserves_hash_and_attachment_contract(workspace, with_session):
    response = upload(workspace, session=with_session)
    assert response.status_code == 201
    result = response.get_json()
    assert result["filename"] == "new.txt"
    assert result["sha256"] == hashlib.sha256(b"new source bytes").hexdigest()
    assert result["size_bytes"] == len(b"new source bytes")
    assert bool(result["attachment"]) == with_session
    with workspace[3].session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        asset = session.get(SourceAsset, result["source_asset_id"])
        assert artifact.content_hash == asset.content_hash == result["sha256"]
        assert artifact.mime_type == "text/plain"
        assert workspace[4].managed_path(artifact.relative_path).read_bytes() == b"new source bytes"


@pytest.mark.parametrize("copy_fails", [False, True])
def test_filesystem_without_hardlinks_uses_exclusive_copy_outside_writer(
    workspace, monkeypatch, copy_fails
):
    before = snapshot(workspace)

    def unsupported(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr(upload_publication.os, "link", unsupported)
    original_copy = upload_publication.copyfileobj

    def copy(source, output):
        # SQLite can still admit an independent writer during the disk copy.
        with workspace[3].immediate_session():
            pass
        if copy_fails:
            output.write(b"partial")
            raise PublicationFailure("partial fallback copy failure")
        original_copy(source, output)

    monkeypatch.setattr(upload_publication, "copyfileobj", copy)
    if copy_fails:
        with pytest.raises(PublicationFailure, match="partial fallback copy failure"):
            upload(workspace)
        assert snapshot(workspace) == before
    else:
        response = upload(workspace)
        assert response.status_code == 201
        with workspace[3].session() as session:
            artifact = session.get(Artifact, response.get_json()["artifact_id"])
            assert (
                workspace[4].managed_path(artifact.relative_path).read_bytes()
                == b"new source bytes"
            )


def test_chunk_initialization_replay_reuses_directory_and_rows(workspace):
    client, headers, session_id, _, _ = workspace
    payload = {"session_id": session_id, "filename": "new.txt", "size_bytes": 16}
    headers = {**headers, "Idempotency-Key": "native-upload-replay"}
    initial = client.post("/api/v1/uploads/init", json=payload, headers=headers)
    assert initial.status_code == 201
    before = snapshot(workspace)
    replay = client.post("/api/v1/uploads/init", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.get_json() == initial.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert snapshot(workspace) == before
