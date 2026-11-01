"""Source ownership follows real creation, reuse and source-change boundaries."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    Artifact,
    Project,
    SessionRecord,
    SessionSource,
    SourceAsset,
    SourceRecord,
    Workspace,
)
from pandrator.web.projects.identity import initial_personal_workspace
from pandrator.web.session_forks import SessionForkService
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.source_management import (
    change_source_in_session,
    preview_source_change,
    start_new_source_session_in_session,
)
from pandrator.web.upload_publication import publish_multipart_upload
from pandrator.web.uploads import ChunkUploadService
from pandrator.web.workflow_source import SourceWorkflowContext, reuse_source
from tests.web_test_support import prepare_web_test_data_root


def _use_pre_constraint_schema(database: Database) -> None:
    """Keep corrupt-link service defenses covered on their genuine predecessor."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database.path))
    command.downgrade(config, "0076_project_graph_consistency")


@dataclass
class Sources:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    sessions: SessionService
    library: SourceLibraryService
    other_workspace_id: str

    def record(self, name: str, *, other: bool = False) -> SessionRecord:
        return self.sessions.create(
            name,
            workflow_kind="subtitles",
            workspace_id=self.other_workspace_id if other else None,
        )

    def artifact(
        self, name: str, *, record: SessionRecord | None = None, role: str = "upload",
        workspace_id: str | None = None,
    ) -> Artifact:
        path = self.paths.uploads / name
        path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSource text.\n"
            if name.endswith(".srt")
            else "Original source bytes.",
            encoding="utf-8",
        )
        return self.artifacts.register(
            path,
            kind="srt" if name.endswith(".srt") else "text",
            role=role,
            session_id=record.id if record else None,
            workspace_id=workspace_id,
            metadata={"language": "en"},
        )


@pytest.fixture
def sources(tmp_path: Path) -> Iterator[Sources]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    with database.session() as db:
        other = Workspace(kind="team", name="Independent workspace")
        db.add(other)
        db.flush()
        other_id = other.id
    try:
        yield Sources(
            database,
            paths,
            artifacts,
            SessionService(database),
            SourceLibraryService(database, artifacts),
            other_id,
        )
    finally:
        database.dispose()


def test_creation_inherits_project_workspace_and_sessionless_creation_stays_personal(
    sources: Sources,
):
    record = sources.record("Other source", other=True)
    artifact = sources.artifact("other.txt", record=record)
    asset = sources.library.ensure_for_artifact(artifact.id)
    assert asset.workspace_id == sources.other_workspace_id
    assert sources.library.ensure_for_artifact(artifact.id).id == asset.id
    loose_artifact = sources.artifact("loose.txt")
    loose = sources.library.ensure_for_artifact(loose_artifact.id)
    with sources.database.session() as db:
        assert loose.workspace_id == initial_personal_workspace(db).id
        assert db.scalar(select(func.count()).select_from(SourceAsset)) == 2
    explicit = sources.library.ensure_for_artifact(
        sources.artifact("explicit.txt", workspace_id=sources.other_workspace_id).id,
        workspace_id=sources.other_workspace_id,
    )
    assert explicit.workspace_id == sources.other_workspace_id


def test_cross_workspace_attachment_rejection_preserves_current_selection(sources: Sources):
    own = sources.record("Personal source")
    foreign = sources.record("Foreign source", other=True)
    own_artifact = sources.artifact("own.txt", record=own)
    foreign_artifact = sources.artifact("foreign.txt", record=foreign)
    own_asset = sources.library.ensure_for_artifact(own_artifact.id)
    foreign_asset = sources.library.ensure_for_artifact(foreign_artifact.id)
    sources.library.attach(own.id, own_asset.id)
    with pytest.raises(ValueError, match="workspace"):
        sources.library.attach(own.id, foreign_asset.id, expected_session_revision=own.revision)
    with sources.database.session() as db:
        links = list(db.scalars(select(SessionSource).where(SessionSource.session_id == own.id)))
        assert len(links) == 1 and links[0].source_asset_id == own_asset.id
        assert links[0].is_current
        current = db.get(SessionRecord, own.id)
        assert current is not None and current.revision == own.revision


def test_same_workspace_source_is_shared_by_an_independent_fork(sources: Sources):
    original = sources.record("Original")
    artifact = sources.artifact("shared.txt", record=original)
    asset = sources.library.ensure_for_artifact(artifact.id)
    sources.library.attach(original.id, asset.id)
    checkpoint = sources.artifact("reviewed.srt", record=original, role="correction")
    forks = SessionForkService(sources.database, sources.paths, sources.artifacts)
    with sources.database.immediate_session() as db:
        fork = forks.fork_in_session(db, original.id, checkpoint.id)
        copied = db.scalar(select(SessionSource).where(SessionSource.session_id == fork.record.id))
        assert copied is not None and copied.source_asset_id == asset.id
        assert fork.record.project_id != original.project_id
        project = db.get(Project, fork.record.project_id)
        assert project is not None and project.workspace_id == asset.workspace_id
    assert (
        sources.paths.managed_path(artifact.relative_path).read_text() == "Original source bytes."
    )


def test_fork_rejects_foreign_attachment_without_leaving_root_or_files(sources: Sources):
    original = sources.record("Corrupt source")
    foreign = sources.record("Foreign source", other=True)
    artifact = sources.artifact("fork-foreign.txt", record=foreign)
    asset = sources.library.ensure_for_artifact(artifact.id)
    checkpoint = sources.artifact("fork-checkpoint.srt", record=original, role="correction")
    _use_pre_constraint_schema(sources.database)
    with sources.database.session() as db:
        db.add(SessionSource(session_id=original.id, source_asset_id=asset.id, role="primary"))
        roots_before = set(db.scalars(select(Project.id)))
        records_before = set(db.scalars(select(SessionRecord.id)))
    directories_before = set(sources.paths.sessions.iterdir())
    forks = SessionForkService(sources.database, sources.paths, sources.artifacts)
    with pytest.raises(ValueError, match="workspace"):
        with sources.database.immediate_session() as db:
            forks.fork_in_session(db, original.id, checkpoint.id)
    with sources.database.session() as db:
        assert set(db.scalars(select(Project.id))) == roots_before
        assert set(db.scalars(select(SessionRecord.id))) == records_before
    assert set(sources.paths.sessions.iterdir()) == directories_before
    assert (
        sources.paths.managed_path(artifact.relative_path).read_text() == "Original source bytes."
    )


@pytest.mark.parametrize("operation", ["preview", "replace", "new_session"])
def test_source_change_rejects_foreign_input_before_reset_or_root_creation(
    sources: Sources, operation: str
):
    own = sources.record("Current work")
    other = sources.record("Foreign input", other=True)
    artifact = sources.artifact("foreign-change.txt", record=other)
    asset = sources.library.ensure_for_artifact(artifact.id)
    services = SimpleNamespace(
        source_library=sources.library, sessions=sources.sessions, paths=sources.paths
    )
    with sources.database.session() as db:
        roots_before = set(db.scalars(select(Project.id)))
    with patch("pandrator.web.source_management._reset_results") as reset:
        with pytest.raises(ValueError, match="workspace"):
            with sources.database.immediate_session() as db:
                if operation == "preview":
                    preview_source_change(db, own.id, "primary", asset.id)
                else:
                    function = (
                        change_source_in_session
                        if operation == "replace"
                        else start_new_source_session_in_session
                    )
                    function(
                        services,
                        db,
                        own.id,
                        role="primary",
                        new_source_asset_id=asset.id,
                        expected_revision=own.revision,
                        impact_token="irrelevant-foreign-input",
                    )
        reset.assert_not_called()
    with sources.database.session() as db:
        assert set(db.scalars(select(Project.id))) == roots_before
        current = db.get(SessionRecord, own.id)
        assert current is not None and current.revision == own.revision
    assert (
        sources.paths.managed_path(artifact.relative_path).read_text() == "Original source bytes."
    )


def test_foreign_subtitle_adoption_rejects_corrupt_attachment_before_promotion(sources: Sources):
    own = sources.record("Target")
    other = sources.record("Subtitle owner", other=True)
    artifact = sources.artifact("foreign.srt", record=other)
    asset = sources.library.ensure_for_artifact(artifact.id)
    _use_pre_constraint_schema(sources.database)
    # Simulate a corrupt internal link; normal attachment commands reject this.
    with sources.database.session() as db:
        db.add(SessionSource(session_id=own.id, source_asset_id=asset.id, role="primary"))
    with patch("pandrator.web.subtitle_sources.adopt_subtitle_source_in_session") as promote:
        with pytest.raises(ValueError, match="workspace"):
            sources.library.adopt_subtitles(own.id, asset.id)
        promote.assert_not_called()


@pytest.mark.parametrize("foreign", [False, True])
def test_reuse_checks_scope_before_copying_and_keeps_original_bytes(
    sources: Sources, foreign: bool
):
    original = sources.record("Reusable", other=foreign)
    target = sources.record("Destination")
    artifact = sources.artifact("reuse.txt", record=original)
    sources.library.ensure_for_artifact(artifact.id)
    original_path = sources.paths.managed_path(artifact.relative_path)
    target_directory = sources.paths.sessions / target.storage_key
    context = cast(
        SourceWorkflowContext,
        SimpleNamespace(
            database=sources.database,
            paths=sources.paths,
            artifacts=sources.artifacts,
            _resolve_input=lambda _identifier: (artifact, original_path),
            _session_dir=lambda _identifier: target_directory,
        ),
    )
    if foreign:
        with patch("pandrator.web.workflow_source.shutil.copy2") as copy:
            with pytest.raises(ValueError, match="workspace"):
                reuse_source(
                    context,
                    {"session_id": target.id, "artifact_id": artifact.id},
                    Mock(),
                    threading.Event(),
                )
            copy.assert_not_called()
        assert not (target_directory / "sources").exists()
    else:
        result = reuse_source(
            context,
            {"session_id": target.id, "artifact_id": artifact.id},
            Mock(),
            threading.Event(),
        )
        with sources.database.session() as db:
            created = db.scalar(
                select(SourceAsset).where(SourceAsset.artifact_id == result["artifact_id"])
            )
            assert created is not None and created.workspace_id is not None
            assert created.workspace_id != sources.other_workspace_id
    assert original_path.read_text() == "Original source bytes."


def test_legacy_compatibility_backfill_assigns_source_session_workspace(sources: Sources):
    record = sources.record("Legacy source", other=True)
    artifact = sources.artifact("legacy.txt", record=record)
    with sources.database.session() as db:
        db.add(
            SourceRecord(
                session_id=record.id,
                artifact_id=artifact.id,
                kind="txt",
                display_name="Legacy title",
            )
        )
    assert sources.library.backfill_legacy() == 1
    assert sources.library.backfill_legacy() == 0
    with sources.database.session() as db:
        asset = db.scalar(select(SourceAsset).where(SourceAsset.artifact_id == artifact.id))
        assert asset is not None and asset.workspace_id == sources.other_workspace_id
        assert asset.display_name == "Legacy title"


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("session_bound", [False, True])
def test_real_upload_publication_populates_source_ownership(
    sources: Sources, chunked: bool, session_bound: bool
):
    record = sources.record("Uploaded source", other=True) if session_bound else None
    data = b"Uploaded source bytes."
    if chunked:
        uploads = ChunkUploadService(
            sources.database, sources.paths, sources.artifacts, sources.library
        )
        upload = uploads.initialize(
            filename="source.txt", size_bytes=len(data), session_id=record.id if record else None
        )
        workspace_id = sources.other_workspace_id if record else None
        uploads.write_chunk(upload["id"], 0, io.BytesIO(data), workspace_id=workspace_id)
        result = uploads.complete(upload["id"], workspace_id=workspace_id)
    else:
        temporary = sources.paths.temporary / "source.part"
        temporary.write_bytes(data)
        result = publish_multipart_upload(
            temporary,
            filename="source.txt",
            original_filename="source.txt",
            session_id=record.id if record else None,
            purpose="source",
            database=sources.database,
            paths=sources.paths,
            artifacts=sources.artifacts,
            sources=sources.library,
        )
    with sources.database.session() as db:
        asset = db.get(SourceAsset, result["source_asset_id"])
        expected = sources.other_workspace_id if record else initial_personal_workspace(db).id
        assert asset is not None and asset.workspace_id == expected
