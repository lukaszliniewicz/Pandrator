"""Workspace and project fences for multipart source publication."""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web import upload_publication
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.artifacts import ArtifactService, sha256_file
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    Artifact,
    SessionRecord,
    SessionSource,
    SourceAsset,
    SourceRecord,
    UploadSessionRecord,
)
from pandrator.web.projects.identity import session_project
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.uploads import ChunkUploadService
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class UploadHarness:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    sources: SourceLibraryService
    workspaces: dict[str, str]
    sessions: dict[str, str]


@pytest.fixture
def upload_harness(tmp_path: Path) -> Iterator[UploadHarness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    sources = SourceLibraryService(database, artifacts)
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        workspaces["personal"] = initial_personal_workspace(session).id
        for key in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=key, state="active")
            session.add(workspace)
            session.flush()
            workspaces[key] = workspace.id
    sessions = {
        key: SessionService(database).create(f"Upload {key}", workspace_id=owner).id
        for key, owner in workspaces.items()
    }
    try:
        yield UploadHarness(database, paths, artifacts, sources, workspaces, sessions)
    finally:
        database.dispose()


def _stage(harness: UploadHarness, name: str) -> Path:
    path = harness.paths.temporary / f"staged-{name}.txt"
    path.write_bytes(b"Synthetic source content.\n")
    return path


def _publish(
    harness: UploadHarness,
    temporary: Path,
    *,
    workspace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    return upload_publication.publish_multipart_upload(
        temporary,
        filename="source.txt",
        original_filename="Original source.txt",
        session_id=session_id,
        purpose="source",
        database=harness.database,
        paths=harness.paths,
        artifacts=harness.artifacts,
        sources=harness.sources,
        workspace_id=workspace_id,
    )


def _rows(harness: UploadHarness) -> dict[str, list[tuple[Any, ...]]]:
    models = (Artifact, SourceAsset, SourceRecord, SessionSource, SessionRecord)
    with harness.database.snapshot_session() as session:
        return {
            model.__tablename__: [
                tuple(row)
                for row in session.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key)
                )
            ]
            for model in models
        }


def test_unbound_multipart_sources_keep_selected_and_solo_workspace_owners(
    upload_harness: UploadHarness,
) -> None:
    harness = upload_harness
    published: list[Path] = []
    for key in ("alpha", "beta", "personal"):
        staged = _stage(harness, key)
        result = _publish(
            harness,
            staged,
            workspace_id=None if key == "personal" else harness.workspaces[key],
        )
        assert set(result) == {
            "artifact_id",
            "source_asset_id",
            "attachment",
            "filename",
            "size_bytes",
            "sha256",
        }
        assert result["attachment"] is None
        with harness.database.session() as session:
            artifact = session.get(Artifact, result["artifact_id"])
            source = session.get(SourceAsset, result["source_asset_id"])
            assert artifact is not None and source is not None
            assert artifact.workspace_id == source.workspace_id == harness.workspaces[key]
            assert artifact.project_id is None and artifact.session_id is None
            assert source.artifact_id == artifact.id
            destination = harness.paths.managed_path(artifact.relative_path)
        assert destination.read_bytes() == staged.read_bytes()
        assert result["sha256"] == sha256_file(destination)
        published.append(destination)
    assert len(set(published)) == 3


def test_bound_multipart_source_derives_project_and_workspace_without_a_default_override(
    upload_harness: UploadHarness,
) -> None:
    harness = upload_harness
    session_id = harness.sessions["alpha"]
    result = _publish(harness, _stage(harness, "bound"), session_id=session_id)
    with harness.database.session() as session:
        project = session_project(session, session_id)
        artifact = session.get(Artifact, result["artifact_id"])
        source = session.get(SourceAsset, result["source_asset_id"])
        assert artifact is not None and source is not None
        assert artifact.session_id == session_id and artifact.project_id == project.id
        assert artifact.workspace_id == source.workspace_id == harness.workspaces["alpha"]
        assert session.scalar(select(SourceRecord).where(SourceRecord.artifact_id == artifact.id))
        assert session.scalar(
            select(SessionSource).where(SessionSource.source_asset_id == source.id)
        )


def test_wrong_workspace_is_rejected_before_hashing_or_publication(
    upload_harness: UploadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = upload_harness
    staged = _stage(harness, "mismatch")
    original = staged.read_bytes()
    before = _rows(harness)

    def unexpected_io(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Publication I/O ran before scope validation")

    monkeypatch.setattr(upload_publication, "sha256_file", unexpected_io)
    monkeypatch.setattr(upload_publication.os, "link", unexpected_io)
    with pytest.raises(ValueError, match="workspace"):
        _publish(
            harness,
            staged,
            session_id=harness.sessions["alpha"],
            workspace_id=harness.workspaces["beta"],
        )
    assert _rows(harness) == before
    assert staged.read_bytes() == original
    assert not list(harness.paths.uploads.iterdir())


def test_archival_during_copy_rejects_publication_and_cleans_only_its_output(
    upload_harness: UploadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = upload_harness
    staged = _stage(harness, "archived")
    retained = harness.paths.uploads / "retained.bin"
    retained.write_bytes(b"Preserve this unrelated upload.")
    before = _rows(harness)
    copyfileobj = upload_publication.copyfileobj

    def unavailable_link(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.ENOSYS, "Synthetic filesystem without hard links")

    def archive_during_copy(source: Any, destination: Any) -> None:
        copyfileobj(source, destination)
        with harness.database.immediate_session() as session:
            workspace = session.get(Workspace, harness.workspaces["alpha"])
            assert workspace is not None
            workspace.state = "archived"

    monkeypatch.setattr(upload_publication.os, "link", unavailable_link)
    monkeypatch.setattr(upload_publication, "copyfileobj", archive_during_copy)
    with pytest.raises(ValueError, match="inactive"):
        _publish(harness, staged, workspace_id=harness.workspaces["alpha"])
    assert _rows(harness) == before
    assert retained.read_bytes() == b"Preserve this unrelated upload."
    assert list(harness.paths.uploads.iterdir()) == [retained]
    assert staged.read_bytes() == b"Synthetic source content.\n"


def test_changed_project_cannot_publish_under_the_new_root(
    upload_harness: UploadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = upload_harness
    # Exercise the publication fence on the genuine schema that allowed retargeting.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(harness.database.path))
    command.downgrade(config, "0075_mandatory_ownership")
    session_id = harness.sessions["alpha"]
    other = SessionService(harness.database).create(
        "Other project", workspace_id=harness.workspaces["alpha"]
    )
    with harness.database.session() as session:
        other_project_id = session_project(session, other.id).id
    original_link = os.link

    def retarget_after_link(source: Path, destination: Path) -> None:
        original_link(source, destination)
        with harness.database.immediate_session() as session:
            record = session.get(SessionRecord, session_id)
            assert record is not None
            record.project_id = other_project_id

    monkeypatch.setattr(upload_publication.os, "link", retarget_after_link)
    with pytest.raises(ValueError, match="project changed"):
        _publish(harness, _stage(harness, "retargeted"), session_id=session_id)
    assert not list(harness.paths.uploads.iterdir())
    with harness.database.session() as session:
        assert session.scalar(select(Artifact.id)) is None
        assert session.scalar(select(SourceAsset.id)) is None


@pytest.mark.parametrize(
    "invalid_link",
    (
        "foreign_artifact",
        "foreign_source",
        "other_project",
        "source_disagrees",
        "empty_nonobject",
    ),
)
def test_status_and_completed_replay_reject_inconsistent_retained_results(
    upload_harness: UploadHarness,
    invalid_link: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = upload_harness
    workspace_id = harness.workspaces["alpha"]
    session_id = harness.sessions["alpha"]
    service = ChunkUploadService(
        harness.database, harness.paths, harness.artifacts, harness.sources
    )
    initialized = service.initialize(filename="retained.txt", size_bytes=1, session_id=session_id)
    result: Any = {"upload_id": initialized["id"]}
    if invalid_link in {"foreign_artifact", "foreign_source"}:
        foreign = _publish(
            harness,
            _stage(harness, "foreign-result"),
            session_id=harness.sessions["beta"],
        )
        field = "artifact_id" if invalid_link == "foreign_artifact" else "source_asset_id"
        result[field] = foreign[field]
    elif invalid_link == "other_project":
        other = SessionService(harness.database).create(
            "Another project in alpha", workspace_id=workspace_id
        )
        result["artifact_id"] = _publish(
            harness, _stage(harness, "other-project"), session_id=other.id
        )["artifact_id"]
    elif invalid_link == "source_disagrees":
        first = _publish(harness, _stage(harness, "first"), session_id=session_id)
        second = _publish(harness, _stage(harness, "second"), session_id=session_id)
        result.update(artifact_id=first["artifact_id"], source_asset_id=second["source_asset_id"])
    else:
        result = []
    with harness.database.immediate_session() as session:
        record = session.get(UploadSessionRecord, initialized["id"])
        assert record is not None
        record.state = "completed"
        record.result_json = result

    def unexpected_media_io(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Result validation reached upload media paths")

    from pandrator.web import uploads as uploads_module

    monkeypatch.setattr(uploads_module, "managed_upload_directory", unexpected_media_io)
    for read in (service.status, service.complete):
        with pytest.raises(ValueError):
            read(initialized["id"], workspace_id=workspace_id)
    with harness.database.session() as session:
        record = session.get(UploadSessionRecord, initialized["id"])
        assert record is not None and record.state == "completed"
        assert record.result_json == result
