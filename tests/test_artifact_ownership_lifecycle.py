"""Managed files keep an owner across registration, copying and retention."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import TypedDict

import pytest
from sqlalchemy import select

from pandrator.web.artifacts import ArtifactService, sha256_file
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Project,
    SessionRecord,
    SessionStageSelection,
    SourceAsset,
    Workspace,
)
from pandrator.web.projects.identity import initial_personal_workspace
from pandrator.web.session_forks import SessionForkService
from pandrator.web.session_purge import SessionPurgeService
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from tests import test_web_dispatch as dispatch_tests
from tests import test_web_session_forks as legacy_forks
from tests.test_project_ownership_lifecycle import Harness, translation_case
from tests.test_session_fork_assets import _edited_fixture, _fork
from tests.test_source_passage_backend import _preview_guards, _rebuild
from tests.web_test_support import prepare_web_test_data_root


class _ScopeContext(TypedDict, total=False):
    session_id: str | None
    project_id: str | None
    workspace_id: str | None


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    try:
        yield Harness(
            database,
            paths,
            SessionService(database),
            artifacts,
            SessionForkService(database, paths, artifacts),
        )
    finally:
        database.dispose()


def _file(harness: Harness, name: str) -> Path:
    path = harness.paths.uploads / name
    path.write_text("Original file bytes.\n", encoding="utf-8")
    return path


def _foreign_record(harness: Harness) -> tuple[SessionRecord, str]:
    with harness.database.session() as db:
        workspace = Workspace(kind="team", name="Other workspace")
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
    record = harness.sessions.create("Other workspace project", workspace_id=workspace_id)
    return record, workspace_id


def _snapshot(harness: Harness) -> dict:
    with harness.database.session() as db:
        return deepcopy(
            {
                model.__tablename__: [
                    dict(row) for row in db.execute(select(model.__table__)).mappings().all()
                ]
                for model in (Artifact, ArtifactEdge, SessionStageSelection)
            }
        )


def test_registration_assigns_session_default_and_explicit_owners(harness: Harness):
    record, workspace_id = _foreign_record(harness)
    bound = harness.artifacts.register(
        _file(harness, "bound.txt"), kind="text", session_id=record.id
    )
    assert (bound.workspace_id, bound.project_id) == (workspace_id, record.project_id)
    project_only = harness.artifacts.register(
        _file(harness, "project-only.txt"), kind="text", project_id=record.project_id
    )
    assert project_only.session_id is None
    assert (project_only.workspace_id, project_only.project_id) == (workspace_id, record.project_id)
    library = harness.artifacts.register(
        _file(harness, "library.txt"), kind="text", workspace_id=workspace_id
    )
    assert library.workspace_id == workspace_id
    assert library.project_id is None and library.session_id is None
    personal = harness.artifacts.register(_file(harness, "personal.txt"), kind="text")
    with harness.database.session() as db:
        assert personal.workspace_id == initial_personal_workspace(db).id
    assert personal.project_id is None and personal.session_id is None


def test_omitted_context_and_restoration_retain_registered_scope(harness: Harness):
    record, workspace_id = _foreign_record(harness)
    path = _file(harness, "restore.txt")
    original = harness.artifacts.register(path, kind="text", session_id=record.id)
    with harness.database.session() as db:
        stored = db.get(Artifact, original.id)
        assert stored is not None
        stored.state = "deleted"
        stored.metadata_json = {"deleted_at": "historical deletion", "preserved": True}
    restored = harness.artifacts.register(path, kind="text")
    assert restored.id == original.id and restored.state == "current"
    assert (restored.workspace_id, restored.project_id, restored.session_id) == (
        workspace_id,
        record.project_id,
        record.id,
    )
    assert restored.metadata_json == {"preserved": True}


def test_same_project_language_registration_retains_existing_session_behavior(harness: Harness):
    source, _checkpoint, language, _translation = translation_case(harness)
    path = _file(harness, "language-shared.txt")
    original = harness.artifacts.register(path, kind="text", session_id=source.id)
    reused = harness.artifacts.register(path, kind="text", session_id=language.id)
    assert reused.id == original.id and reused.session_id == language.id
    assert reused.project_id == source.project_id == language.project_id
    assert reused.workspace_id == original.workspace_id


@pytest.mark.parametrize("conflict", ["workspace", "project", "library-claim"])
def test_conflicting_registration_has_no_partial_database_or_file_mutation(
    harness: Harness, conflict: str
):
    original_record = harness.sessions.create("Original project")
    target = (
        _foreign_record(harness)[0]
        if conflict == "workspace"
        else harness.sessions.create("Other project")
    )
    path = _file(harness, "already-owned.txt")
    original = harness.artifacts.register(
        path,
        kind="text",
        role="correction",
        session_id=None if conflict == "library-claim" else original_record.id,
        metadata={"must_survive": True},
    )
    target_current = harness.artifacts.register(
        _file(harness, "target-current.txt"),
        kind="text",
        role="correction",
        session_id=target.id,
    )
    harness.artifacts.register(
        _file(harness, "target-descendant.txt"),
        kind="text",
        session_id=target.id,
        parent_ids=[target_current.id],
    )
    before = _snapshot(harness)
    original_hash = sha256_file(path)
    # Catch inside the caller's transaction, then commit: a validation failure
    # must not depend on the transaction manager rolling back partial writes.
    with harness.database.session() as db:
        with pytest.raises(ValueError):
            harness.artifacts.register_in_session(
                db,
                path,
                kind="changed",
                role="correction",
                session_id=target.id,
                metadata={"must_not_be_written": True},
                parent_ids=[target_current.id],
                replace_parent_ids=True,
            )
    assert _snapshot(harness) == before
    assert sha256_file(path) == original_hash
    assert harness.artifacts.resolve(original.id)[0].metadata_json == {"must_survive": True}


def test_explicit_context_mismatch_rejects_new_registration(harness: Harness):
    original = harness.sessions.create("Original")
    other, workspace_id = _foreign_record(harness)
    path = _file(harness, "rejected-new.txt")
    before = _snapshot(harness)
    contexts: list[_ScopeContext] = [
        {"session_id": original.id, "workspace_id": workspace_id},
        {"session_id": original.id, "project_id": other.project_id},
        {"project_id": original.project_id, "workspace_id": workspace_id},
    ]
    for context in contexts:
        with pytest.raises(ValueError):
            harness.artifacts.register(path, kind="text", **context)
        assert _snapshot(harness) == before


def test_detached_artifact_retains_source_owner_and_rejects_other_workspace(harness: Harness):
    record, workspace_id = _foreign_record(harness)
    path = _file(harness, "detached.txt")
    artifact = harness.artifacts.register(path, kind="text", session_id=record.id)
    with harness.database.session() as db:
        stored = db.get(Artifact, artifact.id)
        assert stored is not None
        stored.session_id = None
    library = SourceLibraryService(harness.database, harness.artifacts)
    asset = library.ensure_for_artifact(artifact.id)
    assert asset.workspace_id == workspace_id
    with harness.database.session() as db:
        personal_id = initial_personal_workspace(db).id
    with pytest.raises(ValueError, match="workspace"):
        library.ensure_for_artifact(artifact.id, workspace_id=personal_id)
    assert sha256_file(path) == artifact.content_hash


def test_last_session_purge_retains_library_workspace_after_project_deletion(harness: Harness):
    record, workspace_id = _foreign_record(harness)
    directory = harness.paths.sessions / record.storage_key
    directory.mkdir(parents=True)
    path = directory / "source.txt"
    path.write_text("Retained original media.\n", encoding="utf-8")
    artifact = harness.artifacts.register(path, kind="text", role="source", session_id=record.id)
    library = SourceLibraryService(harness.database, harness.artifacts)
    asset = library.ensure_for_artifact(artifact.id)
    trashed = harness.sessions.trash(record.id, record.revision)
    purge = SessionPurgeService(harness.database, harness.paths)
    preview = purge.preview(record.id)
    assert preview["can_purge"], preview
    result = purge.purge(record.id, trashed.revision, preview["impact_token"])
    assert result["state"] == "complete"
    with harness.database.session() as db:
        assert db.get(SessionRecord, record.id) is None
        assert db.get(Project, record.project_id) is None
        retained = db.get(Artifact, artifact.id)
        retained_source = db.get(SourceAsset, asset.id)
        assert retained is not None and retained_source is not None
        assert retained.session_id is None and retained.project_id is None
        assert retained.workspace_id == retained_source.workspace_id == workspace_id
        assert (
            sha256_file(harness.paths.managed_path(retained.relative_path)) == artifact.content_hash
        )
    reused = harness.artifacts.register(path, kind="text", role="source")
    assert reused.id == artifact.id and reused.workspace_id == workspace_id
    assert reused.session_id is None and reused.project_id is None


def test_forked_checkpoints_get_destination_project_and_preserve_original(harness: Harness):
    source, checkpoint, language, _translation = translation_case(harness)
    with harness.database.immediate_session() as db:
        copied = harness.forks.fork_in_session(db, source.id, checkpoint.id, name="Owned copy")
        copied_id = copied.record.id
        copied_project = copied.record.project_id
    with harness.database.session() as db:
        for session_id, project_id in (
            (source.id, source.project_id),
            (language.id, source.project_id),
            (copied_id, copied_project),
        ):
            root = db.get(Project, project_id)
            assert root is not None
            outputs = list(db.scalars(select(Artifact).where(Artifact.session_id == session_id)))
            assert outputs
            assert all(
                item.project_id == root.id and item.workspace_id == root.workspace_id
                for item in outputs
            )
        assert copied_project != source.project_id
        original = db.get(Artifact, checkpoint.id)
        assert original is not None and original.project_id == source.project_id


def test_fork_ancestry_constructor_assigns_scope_to_every_copied_artifact():
    case = legacy_forks.WebSessionForkTests(methodName="runTest")
    case.setUp()
    try:
        _edited_fixture(case, with_evidence=False)
        result = _fork(case)
        with case.database.session() as db:
            root = db.get(Project, result.record.project_id)
            assert root is not None
            copied = list(
                db.scalars(select(Artifact).where(Artifact.session_id == result.record.id))
            )
            assert any((item.metadata_json or {}).get("forked_from_artifact_id") for item in copied)
            assert all(
                item.project_id == root.id and item.workspace_id == root.workspace_id
                for item in copied
            )
            original = db.get(Artifact, case.source.id)
            assert original is not None and original.project_id != root.id
    finally:
        case.tearDown()


def test_passage_branch_constructor_assigns_the_session_owner():
    case = dispatch_tests.DispatchWebTests(methodName="runTest")
    case.setUp()
    try:
        session_id, source_id = case._source(
            texts=("First complete sentence. Second complete sentence.",)
        )
        guards = _preview_guards(case, session_id, source_id)
        response = _rebuild(case, session_id, source_id, guards)
        assert response.status_code == 201, response.get_json()
        with case.extension["database"].session() as db:
            record = db.get(SessionRecord, session_id)
            assert record is not None
            root = db.get(Project, record.project_id)
            branch = db.get(Artifact, response.get_json()["branch_artifact_id"])
            assert root is not None and branch is not None
            assert (branch.workspace_id, branch.project_id, branch.session_id) == (
                root.workspace_id,
                root.id,
                record.id,
            )
            assert branch.state == "stale"
    finally:
        case.tearDown()
