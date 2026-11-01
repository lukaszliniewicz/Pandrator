"""Workspace ownership stays fixed throughout resumable upload operations."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web import uploads as uploads_module
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    Artifact,
    SessionRecord,
    SourceAsset,
    UploadSessionRecord,
    Workspace,
    utcnow,
)
from pandrator.web.projects.identity import initial_personal_workspace
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.upload_activity import UploadBusy, upload_activity
from pandrator.web.uploads import ChunkUploadService
from tests.web_test_support import prepare_web_test_data_root

CHUNK_SIZE = 1024 * 1024


def _current_upload_mode(database: Database) -> None:
    # Historical ownership fixtures use the current service after downgrade.
    # Restore only the default-preserving attachment mode its current ORM needs.
    with database.engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(upload_sessions)")}
        if "attach_to_session" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE upload_sessions ADD COLUMN attach_to_session BOOLEAN NOT NULL DEFAULT 1"
            )


def _use_pre_constraint_schema(database: Database) -> None:
    """Exercise retained-owner defenses before storage forbids the corruption."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database.path))
    command.downgrade(config, "0076_project_graph_consistency")
    _current_upload_mode(database)


@dataclass
class UploadHarness:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    sessions: SessionService
    service: ChunkUploadService
    other_workspace_id: str

    def session(self, name: str, *, other: bool = False) -> SessionRecord:
        return self.sessions.create(
            name,
            workflow_kind="subtitles",
            workspace_id=self.other_workspace_id if other else None,
        )


@pytest.fixture
def uploads(tmp_path: Path) -> Iterator[UploadHarness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    sessions = SessionService(database)
    with database.session() as session:
        other = Workspace(kind="team", name="Upload workspace")
        session.add(other)
        session.flush()
        other_workspace_id = other.id
    try:
        yield UploadHarness(
            database=database,
            paths=paths,
            artifacts=artifacts,
            sessions=sessions,
            service=ChunkUploadService(
                database, paths, artifacts, SourceLibraryService(database, artifacts)
            ),
            other_workspace_id=other_workspace_id,
        )
    finally:
        database.dispose()


def _new_upload(
    uploads: UploadHarness,
    *,
    data: bytes = b"resumable bytes",
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> dict:
    return uploads.service.initialize(
        filename="source.txt",
        size_bytes=len(data),
        session_id=session_id,
        workspace_id=workspace_id,
        chunk_size=CHUNK_SIZE,
    )


def _write_all(
    uploads: UploadHarness, upload: dict, data: bytes, *, workspace_id: str | None = None
):
    for index in range(upload["chunk_count"]):
        start = index * upload["chunk_size"]
        chunk = data[start : start + upload["chunk_size"]]
        uploads.service.write_chunk(
            upload["id"], index, io.BytesIO(chunk), workspace_id=workspace_id
        )


def _solo_workspace_id(uploads: UploadHarness) -> str:
    with uploads.database.session() as session:
        return initial_personal_workspace(session).id


def test_creation_inherits_bound_scope_and_keeps_sessionless_scope_explicit_or_solo(
    uploads: UploadHarness,
):
    record = uploads.session("Bound upload", other=True)
    bound = _new_upload(uploads, session_id=record.id)
    loose = _new_upload(uploads, workspace_id=uploads.other_workspace_id)
    solo = _new_upload(uploads)

    with uploads.database.session() as session:
        bound_row = session.get(UploadSessionRecord, bound["id"])
        loose_row = session.get(UploadSessionRecord, loose["id"])
        solo_row = session.get(UploadSessionRecord, solo["id"])
        assert bound_row is not None and loose_row is not None and solo_row is not None
        assert (bound_row.workspace_id, bound_row.project_id, bound_row.session_id) == (
            uploads.other_workspace_id,
            record.project_id,
            record.id,
        )
        assert (loose_row.workspace_id, loose_row.project_id, loose_row.session_id) == (
            uploads.other_workspace_id,
            None,
            None,
        )
        assert (solo_row.workspace_id, solo_row.project_id, solo_row.session_id) == (
            initial_personal_workspace(session).id,
            None,
            None,
        )
    assert (
        uploads.service.status(loose["id"], workspace_id=uploads.other_workspace_id)["id"]
        == loose["id"]
    )


def test_missing_session_fails_before_upload_directory_creation(uploads: UploadHarness):
    with pytest.raises(KeyError, match="missing-session"):
        uploads.service.initialize(
            filename="missing.txt", size_bytes=1, session_id="missing-session"
        )
    assert not list((uploads.paths.temporary / "uploads").glob("*"))


def test_foreign_id_is_filtered_before_projection_lock_or_stream_read(
    uploads: UploadHarness, monkeypatch: pytest.MonkeyPatch
):
    foreign = _new_upload(uploads, workspace_id=uploads.other_workspace_id)

    class UnreadableStream(io.BytesIO):
        def read(self, _size: int | None = -1) -> bytes:
            raise AssertionError("foreign stream was read")

    def forbidden_lock(*_args, **_kwargs):
        raise AssertionError("foreign upload acquired its native lock")

    monkeypatch.setattr(uploads_module, "upload_activity", forbidden_lock)
    with pytest.raises(KeyError):
        uploads.service.status(foreign["id"])
    with pytest.raises(KeyError):
        uploads.service.write_chunk(foreign["id"], 0, UnreadableStream())
    with pytest.raises(KeyError):
        uploads.service.complete(foreign["id"])
    with pytest.raises(KeyError):
        uploads.service.cancel(foreign["id"])
    assert (
        uploads.service.status(foreign["id"], workspace_id=uploads.other_workspace_id)["id"]
        == foreign["id"]
    )


def test_null_archived_and_mismatched_scope_are_rejected_without_repair(
    uploads: UploadHarness,
):
    # Retained NULL ownership is a legacy state, rejected by current INSERT/UPDATE.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(uploads.database.path))
    command.downgrade(config, "0074_actor_attribution")
    _current_upload_mode(uploads.database)
    unscoped = _new_upload(uploads)
    with uploads.database.immediate_session() as session:
        row = session.get(UploadSessionRecord, unscoped["id"])
        assert row is not None
        row.workspace_id = None
    with pytest.raises(KeyError):
        uploads.service.status(unscoped["id"])
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, unscoped["id"])
        assert row is not None and row.workspace_id is None

    archived = _new_upload(uploads, workspace_id=uploads.other_workspace_id)
    with uploads.database.immediate_session() as session:
        workspace = session.get(Workspace, uploads.other_workspace_id)
        assert workspace is not None
        workspace.state = "archived"
    with pytest.raises(ValueError, match="inactive workspace"):
        uploads.service.status(archived["id"], workspace_id=uploads.other_workspace_id)

    bound = uploads.session("Mismatched upload")
    mismatch = _new_upload(uploads, session_id=bound.id)
    with uploads.database.immediate_session() as session:
        row = session.get(UploadSessionRecord, mismatch["id"])
        assert row is not None
        row.project_id = None
    with pytest.raises(ValueError, match="project ownership disagree"):
        uploads.service.status(mismatch["id"])


def test_streamed_scope_change_preserves_previously_received_chunk_bytes(
    uploads: UploadHarness,
):
    _use_pre_constraint_schema(uploads.database)
    data = b"A" * CHUNK_SIZE + b"tail"
    initialized = _new_upload(uploads, data=data, workspace_id=uploads.other_workspace_id)
    _write_all(
        uploads,
        {**initialized, "chunk_count": 1},
        data[:CHUNK_SIZE],
        workspace_id=uploads.other_workspace_id,
    )
    directory = uploads.paths.temporary / "uploads" / initialized["id"]

    class ScopeChangingStream(io.BytesIO):
        changed = False

        def read(self, size: int | None = -1) -> bytes:
            chunk = super().read(size)
            if not self.changed:
                self.changed = True
                with uploads.database.immediate_session() as session:
                    row = session.get(UploadSessionRecord, initialized["id"])
                    assert row is not None
                    row.workspace_id = solo_workspace_id
            return chunk

    solo_workspace_id = _solo_workspace_id(uploads)
    with pytest.raises(KeyError):
        uploads.service.write_chunk(
            initialized["id"],
            1,
            ScopeChangingStream(data[CHUNK_SIZE:]),
            workspace_id=uploads.other_workspace_id,
        )
    assert (directory / "00000000.part").read_bytes() == data[:CHUNK_SIZE]
    assert not (directory / "00000001.part").exists()
    assert not list(directory.glob("*.tmp"))
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, initialized["id"])
        assert row is not None and row.received_json == {
            "0": hashlib.sha256(data[:CHUNK_SIZE]).hexdigest()
        }
        row.workspace_id = uploads.other_workspace_id


def test_completion_registers_explicit_scope_replays_and_rejects_result_tampering(
    uploads: UploadHarness,
):
    record = uploads.session("Completed upload", other=True)
    data = b"complete me"
    initialized = _new_upload(uploads, data=data, session_id=record.id)
    _write_all(uploads, initialized, data, workspace_id=uploads.other_workspace_id)

    result = uploads.service.complete(initialized["id"], workspace_id=uploads.other_workspace_id)
    assert (
        uploads.service.complete(initialized["id"], workspace_id=uploads.other_workspace_id)
        == result
    )
    with uploads.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        source = session.get(SourceAsset, result["source_asset_id"])
        row = session.get(UploadSessionRecord, initialized["id"])
        assert artifact is not None and source is not None and row is not None
        assert (artifact.workspace_id, artifact.project_id, artifact.session_id) == (
            uploads.other_workspace_id,
            record.project_id,
            record.id,
        )
        assert source.workspace_id == uploads.other_workspace_id
        assert row.result_json is not None
        result_json = dict(row.result_json)
        result_json["artifact_id"] = "missing-artifact"
        row.result_json = result_json
    with pytest.raises(ValueError, match="artifact is missing"):
        uploads.service.status(initialized["id"], workspace_id=uploads.other_workspace_id)
    with pytest.raises(ValueError, match="artifact is missing"):
        uploads.service.complete(initialized["id"], workspace_id=uploads.other_workspace_id)


def test_completion_scope_revalidation_rolls_back_output_and_keeps_chunks(
    uploads: UploadHarness, monkeypatch: pytest.MonkeyPatch
):
    _use_pre_constraint_schema(uploads.database)
    initialized = _new_upload(
        uploads, data=b"prepared bytes", workspace_id=uploads.other_workspace_id
    )
    _write_all(uploads, initialized, b"prepared bytes", workspace_id=uploads.other_workspace_id)
    directory = uploads.paths.temporary / "uploads" / initialized["id"]
    original_prepare = uploads.artifacts.prepare_registration
    solo_workspace_id = _solo_workspace_id(uploads)

    def prepare_then_change_scope(path: Path, **kwargs):
        prepared = original_prepare(path, **kwargs)
        with uploads.database.immediate_session() as session:
            row = session.get(UploadSessionRecord, initialized["id"])
            assert row is not None
            row.workspace_id = solo_workspace_id
        return prepared

    monkeypatch.setattr(uploads.artifacts, "prepare_registration", prepare_then_change_scope)
    with pytest.raises(KeyError):
        uploads.service.complete(initialized["id"], workspace_id=uploads.other_workspace_id)
    assert (directory / "00000000.part").read_bytes() == b"prepared bytes"
    assert not list(uploads.paths.uploads.glob("*source.txt"))
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, initialized["id"])
        assert row is not None and row.state == "open"
        row.workspace_id = uploads.other_workspace_id
        assert session.scalar(select(Artifact.id)) is None


def test_expiry_allows_archived_scope_skips_bad_roots_and_respects_native_lock(
    uploads: UploadHarness,
):
    archived = _new_upload(uploads, workspace_id=uploads.other_workspace_id)
    with uploads.database.immediate_session() as session:
        row = session.get(UploadSessionRecord, archived["id"])
        workspace = session.get(Workspace, uploads.other_workspace_id)
        assert row is not None and workspace is not None
        row.expires_at = utcnow()
        workspace.state = "archived"
    directory = uploads.paths.temporary / "uploads" / archived["id"]
    with upload_activity(uploads.paths, session_id=None, upload_id=archived["id"]):
        assert uploads.service.cleanup_expired() == 0
        assert directory.exists()
    assert uploads.service.cleanup_expired() == 1
    assert not directory.exists()
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, archived["id"])
        assert row is not None and row.state == "expired"

    _use_pre_constraint_schema(uploads.database)
    bad = _new_upload(uploads, session_id=uploads.session("Bad expiry root").id)
    with uploads.database.immediate_session() as session:
        row = session.get(UploadSessionRecord, bad["id"])
        assert row is not None
        row.project_id = None
        row.expires_at = utcnow()
    bad_directory = uploads.paths.temporary / "uploads" / bad["id"]
    assert uploads.service.cleanup_expired() == 0
    assert bad_directory.exists()
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, bad["id"])
        assert row is not None and row.state == "open"


def test_cancel_preserves_cleanup_rollback_and_native_lock_exclusion(
    uploads: UploadHarness, monkeypatch: pytest.MonkeyPatch
):
    record = uploads.session("Cancel lock")
    initialized = _new_upload(uploads, session_id=record.id)
    directory = uploads.paths.temporary / "uploads" / initialized["id"]
    with upload_activity(uploads.paths, session_id=record.id, upload_id=initialized["id"]):
        with pytest.raises(UploadBusy):
            uploads.service.cancel(initialized["id"])
        assert directory.exists()
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, initialized["id"])
        assert row is not None and row.state == "open"

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("simulated cancel cleanup failure")

    monkeypatch.setattr(uploads_module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(OSError, match="simulated cancel cleanup failure"):
        uploads.service.cancel(initialized["id"])
    assert directory.exists()
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, initialized["id"])
        assert row is not None and row.state == "open"


def test_post_commit_cleanup_failure_preserves_completed_result_and_artifact(
    uploads: UploadHarness, monkeypatch: pytest.MonkeyPatch
):
    data = b"committed bytes"
    initialized = _new_upload(uploads, data=data)
    _write_all(uploads, initialized, data)
    directory = uploads.paths.temporary / "uploads" / initialized["id"]
    original_rmtree = uploads_module.shutil.rmtree

    def leave_upload_directory(path, *args, **kwargs):
        if Path(path) == directory:
            return None
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(uploads_module.shutil, "rmtree", leave_upload_directory)
    result = uploads.service.complete(initialized["id"])
    assert directory.exists()
    assert uploads.service.complete(initialized["id"]) == result
    with uploads.database.session() as session:
        row = session.get(UploadSessionRecord, initialized["id"])
        artifact = session.get(Artifact, result["artifact_id"])
        assert row is not None and row.state == "completed"
        assert artifact is not None
        assert uploads.paths.managed_path(artifact.relative_path).read_bytes() == data
