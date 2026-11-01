"""Team workspace ownership across project creation and recovery workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.bundles import SessionBundleService
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    Project,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    TranslationProject,
    Workspace,
)
from pandrator.web.session_forks import SessionForkService
from pandrator.web.sessions import SessionService
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.source_management import (
    preview_source_change,
    start_new_source_session_in_session,
)
from pandrator.web.translation_projects import (
    create_branches_in_session,
    create_project_in_session,
)
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class ProjectHarness:
    database: Database
    paths: DataPaths
    sessions: SessionService
    artifacts: ArtifactService
    forks: SessionForkService
    source_library: SourceLibraryService


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[ProjectHarness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    try:
        yield ProjectHarness(
            database=database,
            paths=paths,
            sessions=SessionService(database),
            artifacts=artifacts,
            forks=SessionForkService(database, paths, artifacts),
            source_library=SourceLibraryService(database, artifacts),
        )
    finally:
        database.dispose()


def _team_workspace(harness: ProjectHarness, name: str, *, state: str = "active") -> str:
    with harness.database.session() as db:
        workspace = Workspace(kind="team", name=name, state=state)
        db.add(workspace)
        db.flush()
        return workspace.id


def _catalog(harness: ProjectHarness) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with harness.database.session() as db:
        projects = tuple(
            sorted(
                (row.id, row.workspace_id, row.source_session_id, row.name)
                for row in db.scalars(select(Project))
            )
        )
        sessions = tuple(
            sorted(
                (row.id, row.name, row.project_id, row.revision, row.status)
                for row in db.scalars(select(SessionRecord))
            )
        )
    return projects, sessions


def _write_srt(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n",
        encoding="utf-8",
    )


def test_session_creation_and_children_keep_the_requested_team_root(
    harness: ProjectHarness,
) -> None:
    workspace_id = _team_workspace(harness, "Editorial team")

    source = harness.sessions.create(
        "Source session", workflow_kind="subtitles", workspace_id=workspace_id
    )
    child = harness.sessions.create(
        "Child session",
        workflow_kind="subtitles",
        project_id=source.project_id,
        workspace_id=workspace_id,
    )

    assert source.project_id is not None
    assert child.project_id == source.project_id
    with harness.database.session() as db:
        root = db.get(Project, source.project_id)
        assert root is not None
        assert root.workspace_id == workspace_id
        assert root.source_session_id == source.id
        child_record = db.get(SessionRecord, child.id)
        assert child_record is not None and child_record.project_id == root.id


@pytest.mark.parametrize("invalid_workspace", ["other", "archived", "missing"])
def test_rejected_workspace_creation_can_be_caught_and_committed_without_changes(
    harness: ProjectHarness,
    invalid_workspace: str,
) -> None:
    owner_workspace_id = _team_workspace(harness, "Project owner")
    other_workspace_id = _team_workspace(harness, "Other team")
    archived_workspace_id = _team_workspace(harness, "Archived team", state="archived")
    source = harness.sessions.create(
        "Existing source", workflow_kind="subtitles", workspace_id=owner_workspace_id
    )
    before = _catalog(harness)

    with harness.database.immediate_session() as db:
        with pytest.raises(ValueError, match="workspace"):
            if invalid_workspace == "other":
                harness.sessions.create(
                    "Rejected child",
                    project_id=source.project_id,
                    workspace_id=other_workspace_id,
                    db_session=db,
                )
            elif invalid_workspace == "archived":
                harness.sessions.create(
                    "Rejected archived root",
                    workspace_id=archived_workspace_id,
                    db_session=db,
                )
            else:
                harness.sessions.create(
                    "Rejected missing root",
                    workspace_id="missing-team-workspace",
                    db_session=db,
                )

    assert _catalog(harness) == before


def test_independent_fork_inherits_team_and_ignores_duplicate_name_in_other_team(
    harness: ProjectHarness,
) -> None:
    workspace_id = _team_workspace(harness, "Source team")
    other_workspace_id = _team_workspace(harness, "Other team")
    source = harness.sessions.create(
        "Corrected source",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
        workspace_id=workspace_id,
    )
    requested_name = "Shared title"
    harness.sessions.create(
        requested_name,
        workflow_kind="subtitles",
        workspace_id=other_workspace_id,
    )
    checkpoint_path = harness.paths.sessions / source.storage_key / "correction.srt"
    _write_srt(checkpoint_path, "Corrected source text.")
    checkpoint = harness.artifacts.register(
        checkpoint_path,
        kind="srt",
        role="correction",
        session_id=source.id,
        metadata={"language": "en"},
    )

    with harness.database.immediate_session() as db:
        fork = harness.forks.fork_in_session(db, source.id, checkpoint.id, name=requested_name)

    assert fork.record.name == requested_name
    assert fork.record.project_id != source.project_id
    with harness.database.session() as db:
        source_root = db.get(Project, source.project_id)
        fork_root = db.get(Project, fork.record.project_id)
        assert source_root is not None and fork_root is not None
        assert source_root.workspace_id == workspace_id
        assert fork_root.workspace_id == workspace_id
        assert fork_root.source_session_id == fork.record.id


def test_translation_source_and_language_branch_share_the_team_root(
    harness: ProjectHarness,
) -> None:
    workspace_id = _team_workspace(harness, "Translation team")
    source = harness.sessions.create(
        "English source",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
        workspace_id=workspace_id,
    )
    checkpoint_path = harness.paths.sessions / source.storage_key / "correction.srt"
    _write_srt(checkpoint_path, "Hello.")
    checkpoint = harness.artifacts.register(
        checkpoint_path,
        kind="srt",
        role="correction",
        session_id=source.id,
        metadata={"language": "en"},
    )
    created_directories: list[Path] = []

    with harness.database.immediate_session() as db:
        created = create_project_in_session(
            db,
            source.id,
            checkpoint.id,
            "Team translation project",
            source.revision,
            paths=harness.paths,
        )["project"]
        project = create_branches_in_session(
            db,
            created["id"],
            created["revision"],
            [{"target_language": "pl"}],
            session_forks=harness.forks,
            paths=harness.paths,
            created_directories=created_directories,
        )["project"]

    branch_id = project["branches"][0]["session_id"]
    branch = harness.sessions.get(branch_id)
    assert source.project_id is not None
    assert branch.project_id == source.project_id
    with harness.database.session() as db:
        root = db.get(Project, source.project_id)
        translation = db.get(TranslationProject, created["id"])
        assert root is not None and root.workspace_id == workspace_id
        assert translation is not None and translation.project_root_id == root.id
        branch_record = db.get(SessionRecord, branch.id)
        assert branch_record is not None and branch_record.project_id == root.id


def test_new_source_session_keeps_team_and_preserves_original_source_and_settings(
    harness: ProjectHarness,
) -> None:
    workspace_id = _team_workspace(harness, "Source team")
    source = harness.sessions.create(
        "Subtitle work",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["transcribe", "correct", "export"],
        workspace_id=workspace_id,
    )

    def source_asset(filename: str, text: str) -> tuple[Path, SourceAsset]:
        path = harness.paths.uploads / filename
        _write_srt(path, text)
        artifact = harness.artifacts.register(
            path,
            kind="srt",
            role="source.original",
            workspace_id=workspace_id,
        )
        return path, harness.source_library.ensure_for_artifact(artifact.id)

    original_path, original_asset = source_asset("original.srt", "Original words.")
    _replacement_path, replacement_asset = source_asset("replacement.srt", "Replacement words.")
    harness.source_library.attach(
        source.id,
        original_asset.id,
        expected_session_revision=source.revision,
    )
    original_setting = {"language_defaults": False, "max_chars_per_line": 42}
    with harness.database.session() as db:
        db.add(
            SessionSetting(
                session_id=source.id,
                section="subtitles",
                value_json=original_setting,
            )
        )

    with harness.database.session() as db:
        current = db.get(SessionRecord, source.id)
        assert current is not None
        current_setting = db.get(SessionSetting, (source.id, "subtitles"))
        assert current_setting is not None
        impact = preview_source_change(db, source.id, "primary", replacement_asset.id)
        expected_revision = impact["session_revision"]
        original_before = (
            current.name,
            current.project_id,
            current.revision,
            tuple(current.included_stages_json),
            dict(current_setting.value_json),
            tuple(
                sorted(
                    (item.source_asset_id, item.role, item.is_current)
                    for item in db.scalars(
                        select(SessionSource).where(SessionSource.session_id == source.id)
                    )
                )
            ),
        )

    services = SimpleNamespace(
        source_library=harness.source_library,
        sessions=harness.sessions,
        paths=harness.paths,
    )
    with harness.database.immediate_session() as db:
        result = start_new_source_session_in_session(
            services,
            db,
            source.id,
            role="primary",
            new_source_asset_id=replacement_asset.id,
            expected_revision=expected_revision,
            impact_token=impact["impact_token"],
        )

    new_id = result["session_id"]
    assert result["original_session_id"] == source.id
    assert result["original_preserved"] is True
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == original_asset.content_hash
    with harness.database.session() as db:
        original = db.get(SessionRecord, source.id)
        created = db.get(SessionRecord, new_id)
        assert original is not None and created is not None
        original_setting_after = db.get(SessionSetting, (source.id, "subtitles"))
        created_setting = db.get(SessionSetting, (created.id, "subtitles"))
        assert original_setting_after is not None and created_setting is not None
        after_original = (
            original.name,
            original.project_id,
            original.revision,
            tuple(original.included_stages_json),
            dict(original_setting_after.value_json),
            tuple(
                sorted(
                    (item.source_asset_id, item.role, item.is_current)
                    for item in db.scalars(
                        select(SessionSource).where(SessionSource.session_id == source.id)
                    )
                )
            ),
        )
        assert after_original == original_before
        assert created.project_id is not None
        created_project = db.get(Project, created.project_id)
        assert created_project is not None and created_project.workspace_id == workspace_id
        assert created_setting.value_json == original_setting
        new_source = db.scalar(
            select(SessionSource).where(
                SessionSource.session_id == created.id,
                SessionSource.role == "primary",
                SessionSource.is_current.is_(True),
            )
        )
        assert new_source is not None
        assert new_source.source_asset_id == replacement_asset.id


def test_bundle_import_uses_requested_team_and_preserves_original(
    harness: ProjectHarness,
) -> None:
    source_workspace_id = _team_workspace(harness, "Exporting team")
    destination_workspace_id = _team_workspace(harness, "Importing team")
    source = harness.sessions.create(
        "Bundle source", workflow_kind="subtitles", workspace_id=source_workspace_id
    )
    source_path = harness.paths.sessions / source.storage_key / "source.srt"
    _write_srt(source_path, "Bundle bytes stay put.")
    original = harness.artifacts.register(
        source_path,
        kind="srt",
        role="correction",
        session_id=source.id,
        metadata={"language": "en"},
    )
    original_bytes = source_path.read_bytes()
    bundles = SessionBundleService(harness.database, harness.paths)
    bundle_path = harness.paths.root / "team-session.pandrator-session"
    bundles.export_bundle(source.id, bundle_path)

    imported = bundles.import_bundle(
        bundle_path,
        name="Imported into second team",
        workspace_id=destination_workspace_id,
    )

    with harness.database.session() as db:
        imported_record = db.get(SessionRecord, imported["session_id"])
        assert imported_record is not None
        assert imported_record.project_id != source.project_id
        imported_root = db.get(Project, imported_record.project_id)
        assert imported_root is not None
        assert imported_root.workspace_id == destination_workspace_id
        assert imported_root.source_session_id == imported_record.id
        imported_artifacts = list(
            db.scalars(select(Artifact).where(Artifact.session_id == imported_record.id))
        )
        assert imported_artifacts
        assert all(item.workspace_id == destination_workspace_id for item in imported_artifacts)
        original_record = db.get(SessionRecord, source.id)
        original_root = db.get(Project, source.project_id)
        original_artifact = db.get(Artifact, original.id)
        assert original_record is not None and original_record.project_id == source.project_id
        assert original_root is not None and original_root.workspace_id == source_workspace_id
        assert original_artifact is not None
        assert original_artifact.workspace_id == source_workspace_id
        assert original_artifact.content_hash == original.content_hash
    assert source_path.read_bytes() == original_bytes
