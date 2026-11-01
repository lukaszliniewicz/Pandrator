"""Ownership survives the existing solo project and recovery lifecycles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.bundles import SessionBundleService
from pandrator.web.database import Database
from pandrator.web.legacy_migration import import_legacy_data
from pandrator.web.models import (
    Artifact,
    Project,
    ProjectFolder,
    ProjectFolderMembership,
    SessionPurge,
    SessionRecord,
    TranslationProject,
    TranslationProjectBranch,
    UploadSessionRecord,
    User,
    Workspace,
    utcnow,
)
from pandrator.web.project_folders import (
    create_project_folder,
    replace_folder_projects,
    set_session_project_folders,
    update_project_folder,
)
from pandrator.web.session_forks import SessionForkService
from pandrator.web.session_purge import PurgeBlocked, SessionPurgeService
from pandrator.web.sessions import SessionService
from pandrator.web.translation_projects import (
    create_branches_in_session,
    create_project_in_session,
    rename_project_in_session,
)
from pandrator.web.upload_identity import upload_ownership
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class Harness:
    database: Database
    paths: DataPaths
    sessions: SessionService
    artifacts: ArtifactService
    forks: SessionForkService


@pytest.fixture
def harness(tmp_path: Path):
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


def translation_case(harness: Harness):
    source = harness.sessions.create(
        "Source session",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
    )
    directory = harness.paths.sessions / source.storage_key
    directory.mkdir(parents=True)
    checkpoint_path = directory / "correction.srt"
    checkpoint_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    checkpoint = harness.artifacts.register(
        checkpoint_path,
        kind="srt",
        role="correction",
        session_id=source.id,
        metadata={"language": "en"},
    )
    with harness.database.immediate_session() as db:
        created = create_project_in_session(
            db,
            source.id,
            checkpoint.id,
            "Independent project title",
            source.revision,
            paths=harness.paths,
        )["project"]
        branched = create_branches_in_session(
            db,
            created["id"],
            created["revision"],
            [{"target_language": "pl"}],
            session_forks=harness.forks,
            paths=harness.paths,
            created_directories=[],
        )["project"]
    branch = harness.sessions.get(branched["branches"][0]["session_id"])
    return source, checkpoint, branch, branched


def test_language_branches_share_ownership_but_manual_forks_create_a_new_root(harness):
    source, checkpoint, branch, translation = translation_case(harness)
    assert source.project_id and branch.project_id == source.project_id
    with harness.database.immediate_session() as db:
        fork = harness.forks.fork_in_session(db, source.id, checkpoint.id, name="Independent copy")
        assert fork.record.project_id != source.project_id
        assert db.get(TranslationProject, translation["id"]).project_root_id == source.project_id
        root = db.get(Project, source.project_id)
        copied = db.get(Project, fork.record.project_id)
        assert root.source_session_id == source.id
        assert copied.source_session_id == fork.record.id
        assert copied.workspace_id == root.workspace_id
        assert copied.created_by_user_id is not None


def test_project_name_mirror_preserves_independent_source_and_language_titles(harness):
    source, _checkpoint, branch, translation = translation_case(harness)
    renamed_source = harness.sessions.update(source.id, source.revision, {"name": "Renamed source"})
    harness.sessions.update(branch.id, branch.revision, {"name": "Polish edition"})
    with harness.database.immediate_session() as db:
        root = db.get(Project, source.project_id)
        assert root.name == "Independent project title"
        rename_project_in_session(
            db,
            translation["id"],
            expected_revision=translation["revision"],
            name="New project title",
        )
        assert root.name == "New project title"
        assert db.get(SessionRecord, source.id).name == renamed_source.name
        assert db.get(SessionRecord, branch.id).name == "Polish edition"
    standalone = harness.sessions.create("Standalone")
    harness.sessions.update(standalone.id, standalone.revision, {"name": "Changed standalone"})
    with harness.database.session() as db:
        assert db.get(Project, standalone.project_id).name == "Changed standalone"


def test_folder_mapping_keeps_public_source_ids_and_adds_the_shared_root(harness):
    source, _checkpoint, branch, _translation = translation_case(harness)
    with harness.database.immediate_session() as db:
        folder = create_project_folder(db, "Editorial")["folder"]
        nested = create_project_folder(db, "Languages", folder["id"])["folder"]
        snapshot = replace_folder_projects(
            db,
            nested["id"],
            expected_revision=nested["revision"],
            project_ids=[branch.id],
        )
        assert snapshot["memberships"] == [
            {
                "folder_id": nested["id"],
                "project_id": source.id,
                "source_session_id": source.id,
                "project_root_id": source.project_id,
                "position": 0,
            }
        ]
        mapping = db.get(ProjectFolderMembership, (nested["id"], source.id))
        assert mapping.project_id == source.project_id
        assert (
            db.get(ProjectFolder, folder["id"]).workspace_id
            == db.get(Project, source.project_id).workspace_id
        )
        assert (
            db.get(ProjectFolder, nested["id"]).workspace_id
            == db.get(ProjectFolder, folder["id"]).workspace_id
        )
        set_session_project_folders(
            db,
            branch.id,
            folder_ids=[folder["id"]],
            expected_revisions={folder["id"]: 1, nested["id"]: 2},
        )
        moved = db.get(ProjectFolderMembership, (folder["id"], source.id))
        assert moved.project_id == source.project_id


def test_folder_mutations_reject_cross_workspace_parents_and_project_links(harness):
    record = harness.sessions.create("Personal project")
    with harness.database.immediate_session() as db:
        own_folder = create_project_folder(db, "Personal folder")["folder"]
        other_workspace = Workspace(kind="team", name="Another workspace")
        db.add(other_workspace)
        db.flush()
        foreign_folder = ProjectFolder(name="Foreign folder", workspace_id=other_workspace.id)
        db.add(foreign_folder)
        db.flush()
        with pytest.raises(ValueError, match="workspace"):
            update_project_folder(
                db,
                own_folder["id"],
                expected_revision=1,
                parent_id=foreign_folder.id,
                change_parent=True,
            )
        with pytest.raises(ValueError, match="workspace"):
            replace_folder_projects(
                db,
                foreign_folder.id,
                expected_revision=1,
                project_ids=[record.id],
            )
        assert db.get(ProjectFolder, own_folder["id"]).parent_id is None


def test_failed_session_creation_rolls_back_its_new_root(harness):
    existing = harness.sessions.create("Original", record_id="fixed-session")
    with harness.database.session() as db:
        before = db.scalar(select(func.count()).select_from(Project))
    with pytest.raises(IntegrityError):
        harness.sessions.create("Conflicting", record_id=existing.id)
    with harness.database.session() as db:
        assert db.scalar(select(func.count()).select_from(Project)) == before
        assert db.get(SessionRecord, existing.id).project_id == existing.project_id


def test_permanent_purge_removes_only_the_empty_project_root(harness):
    removed = harness.sessions.create("Remove me")
    kept = harness.sessions.create("Keep me")
    harness.sessions.trash(removed.id, removed.revision)
    purge = SessionPurgeService(harness.database, harness.paths)
    preview = purge.preview(removed.id)
    assert preview["can_purge"], preview["blockers"]
    assert (
        purge.purge(removed.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    )
    with harness.database.session() as db:
        assert db.get(Project, removed.project_id) is None
        assert db.get(Project, kept.project_id).source_session_id == kept.id
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.scalar(select(func.count()).select_from(Workspace)) == 1


def _project_rows(harness: Harness) -> dict:
    with harness.database.session() as db:
        return {
            model.__tablename__: [
                dict(row)
                for row in db.execute(select(model.__table__).order_by(model.id)).mappings()
            ]
            for model in (
                SessionRecord,
                Project,
                TranslationProject,
                TranslationProjectBranch,
                Artifact,
                UploadSessionRecord,
            )
        }


def test_branch_purge_keeps_shared_root_source_and_restrictive_checkpoint(harness):
    source, checkpoint, branch, translation = translation_case(harness)
    checkpoint_path = harness.paths.managed_path(checkpoint.relative_path)
    before = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    harness.sessions.trash(branch.id, branch.revision)
    purge = SessionPurgeService(harness.database, harness.paths)
    before_preview = _project_rows(harness)
    preview = purge.preview(branch.id)
    assert preview["can_purge"], preview["blockers"]
    assert _project_rows(harness) == before_preview
    assert (
        purge.purge(branch.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    )
    with harness.database.session() as db:
        assert db.get(Project, source.project_id).source_session_id == source.id
        assert db.get(TranslationProject, translation["id"]).project_root_id == source.project_id
        assert db.get(SessionRecord, branch.id) is None
        assert (
            db.scalar(
                select(TranslationProjectBranch.id).where(
                    TranslationProjectBranch.session_id == branch.id,
                )
            )
            is None
        )
    harness.sessions.trash(source.id, harness.sessions.get(source.id).revision)
    assert not purge.preview(source.id)["can_purge"]
    assert hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() == before


def test_branch_purge_retains_checkpoint_used_by_a_surviving_branch(harness: Harness):
    source, checkpoint, removed, translation = translation_case(harness)
    source_path = harness.paths.managed_path(checkpoint.relative_path)
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with harness.database.immediate_session() as db:
        added = create_branches_in_session(
            db,
            translation["id"],
            translation["revision"],
            [{"target_language": "de"}],
            session_forks=harness.forks,
            paths=harness.paths,
            created_directories=[],
        )["project"]
        surviving_id = next(
            item["session_id"] for item in added["branches"] if item["session_id"] != removed.id
        )
        removed_link = db.scalar(
            select(TranslationProjectBranch).where(
                TranslationProjectBranch.session_id == removed.id,
            )
        )
        surviving_link = db.scalar(
            select(TranslationProjectBranch).where(
                TranslationProjectBranch.session_id == surviving_id,
            )
        )
        assert removed_link is not None and surviving_link is not None
        shared_id = removed_link.source_checkpoint_artifact_id
        shared_artifact = db.get(Artifact, shared_id)
        assert shared_artifact is not None and shared_artifact.session_id == removed.id
        shared_path = harness.paths.managed_path(shared_artifact.relative_path)
        shared_hash = hashlib.sha256(shared_path.read_bytes()).hexdigest()
        surviving_link.source_checkpoint_artifact_id = shared_id

    harness.sessions.trash(removed.id, removed.revision)
    purge = SessionPurgeService(harness.database, harness.paths)
    before_preview = _project_rows(harness)
    preview = purge.preview(removed.id)
    assert not preview["can_purge"]
    assert (
        "external_reference:translation_project_branches.source_checkpoint_artifact_id"
        in preview["blockers"]
    )
    assert _project_rows(harness) == before_preview
    with pytest.raises(IntegrityError):
        with harness.database.immediate_session() as db:
            purge._preflight_delete(db, removed.id, set())
    assert _project_rows(harness) == before_preview
    with pytest.raises(PurgeBlocked):
        purge.purge(removed.id, preview["revision"], preview["impact_token"])
    assert _project_rows(harness) == before_preview

    with harness.database.session() as db:
        assert db.get(SessionRecord, removed.id) is not None
        assert db.get(SessionRecord, surviving_id) is not None
        kept = db.get(Artifact, shared_id)
        survivor = db.scalar(
            select(TranslationProjectBranch).where(
                TranslationProjectBranch.session_id == surviving_id,
            )
        )
        root = db.get(Project, source.project_id)
        assert kept is not None and kept.session_id == removed.id
        assert survivor is not None and survivor.source_checkpoint_artifact_id == kept.id
        assert root is not None and root.source_session_id == source.id
    assert hashlib.sha256(shared_path.read_bytes()).hexdigest() == shared_hash
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash


def test_purge_preflight_checks_project_cleanup_before_removing_files(harness: Harness):
    record = harness.sessions.create("Project with retained upload")
    directory = harness.paths.sessions / record.storage_key
    directory.mkdir()
    path = directory / "input.txt"
    path.write_bytes(b"Synthetic source bytes.\n")
    harness.artifacts.register(path, kind="txt", role="output", session_id=record.id)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with harness.database.immediate_session() as db:
        project = db.get(Project, record.project_id)
        assert project is not None
        upload = UploadSessionRecord(
            workspace_id=project.workspace_id,
            project_id=project.id,
            session_id=None,
            filename="retained.txt",
            size_bytes=1,
            chunk_size=1024 * 1024,
            chunk_count=1,
            temporary_relative_path="tmp/uploads/retained-project-upload",
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(upload)
        db.flush()
        assert upload_ownership(db, upload).project_id == project.id
        upload_id = upload.id

    harness.sessions.trash(record.id, record.revision)
    purge = SessionPurgeService(harness.database, harness.paths)
    before_preview = _project_rows(harness)
    preview = purge.preview(record.id)
    assert not preview["can_purge"]
    assert "delete_constraint:IntegrityError" in preview["blockers"]
    assert _project_rows(harness) == before_preview
    with pytest.raises(PurgeBlocked):
        purge.purge(record.id, preview["revision"], preview["impact_token"])
    assert _project_rows(harness) == before_preview
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hash
    with harness.database.immediate_session() as db:
        assert db.get(SessionPurge, record.id) is None
        upload = db.get(UploadSessionRecord, upload_id)
        assert upload is not None
        db.delete(upload)

    allowed = purge.preview(record.id)
    assert allowed["can_purge"], allowed["blockers"]
    assert (
        purge.purge(record.id, allowed["revision"], allowed["impact_token"])["state"] == "complete"
    )
    assert not path.exists()
    with harness.database.session() as db:
        assert db.get(Project, record.project_id) is None


def test_failed_bundle_import_removes_its_session_and_empty_root(harness):
    source, _checkpoint, _branch, _translation = translation_case(harness)
    bundles = SessionBundleService(harness.database, harness.paths)
    bundle_path = harness.paths.root / "source.pandrator-session"
    bundles.export_bundle(source.id, bundle_path)
    with harness.database.session() as db:
        before_roots = set(db.scalars(select(Project.id)))
        before_sessions = set(db.scalars(select(SessionRecord.id)))
    before_directories = set(harness.paths.sessions.iterdir())

    def fail_registration(unit, _destination, **kwargs):
        imported = unit.get(SessionRecord, kwargs["session_id"])
        assert imported is not None and imported.id not in before_sessions
        assert imported.project_id not in before_roots
        assert unit.get(Project, imported.project_id) is not None
        assert (harness.paths.sessions / imported.storage_key).is_dir()
        raise RuntimeError("injected import failure")

    with patch.object(
        bundles.artifacts, "register_in_session", side_effect=fail_registration
    ) as registration:
        with pytest.raises(RuntimeError, match="injected import failure"):
            bundles.import_bundle(bundle_path, name="Failed copy")
    registration.assert_called_once()
    assert set(harness.paths.sessions.iterdir()) == before_directories
    with harness.database.session() as db:
        assert set(db.scalars(select(Project.id))) == before_roots
        assert set(db.scalars(select(SessionRecord.id))) == before_sessions


def test_legacy_import_assigns_ownership_without_inventing_authorship(tmp_path):
    paths = DataPaths.from_value(tmp_path).ensure()
    legacy = paths.legacy_outputs / "Legacy project"
    legacy.mkdir(parents=True)
    media = legacy / "captions.srt"
    media.write_text("1\n00:00:00,000 --> 00:00:01,000\nLegacy.\n", encoding="utf-8")
    original = media.read_bytes()
    import_legacy_data(paths)
    database = Database(paths.database)
    try:
        with database.session() as db:
            record = db.scalar(
                select(SessionRecord).where(SessionRecord.legacy_name == legacy.name)
            )
            assert record is not None and record.project_id is not None
            root = db.get(Project, record.project_id)
            assert root is not None
            assert root.source_session_id == record.id
            assert root.created_by_user_id is None
            assert root.name == record.name
            artifact_count = db.scalar(select(func.count()).select_from(Artifact))
            assert artifact_count is not None and artifact_count >= 1
            root_id = root.id
        import_legacy_data(paths)
        with database.session() as db:
            assert set(db.scalars(select(Project.id))) == {root_id}
        assert media.read_bytes() == original
    finally:
        database.dispose()
