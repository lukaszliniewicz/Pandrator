from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import SCHEMA_HEAD, Database, sqlite_url
from pandrator.web.models import (
    AppSetting,
    Artifact,
    Job,
    ProjectFolder,
    SessionPurge,
    SessionRecord,
    SessionSetting,
    TranslationProject,
    TranslationProjectBranch,
    utcnow,
)
from pandrator.web.project_folders import (
    FolderPromotionConflict,
    create_project_folder,
    delete_project_folder,
    project_folder_snapshot,
    replace_folder_projects,
    set_session_project_folders,
    update_project_folder,
)
from pandrator.web.settings_policy import RevisionConflict
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def folder_app(tmp_path: Path):
    prepare_web_test_data_root(tmp_path)
    tokens = BootstrapTokenStore()
    token = tokens.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    bootstrap = client.post("/api/v1/auth/bootstrap", json={"token": token})
    headers = {"X-CSRF-Token": bootstrap.get_json()["csrf_token"]}
    extension = app.extensions["pandrator"]
    yield extension["services"], client, headers
    extension["services"].database.dispose()


def _session(services, name: str, *, target_language: str | None = None):
    return services.sessions.create(
        name,
        workflow_kind="subtitles",
        source_language="en",
        target_language=target_language,
        included_stages=["transcribe", "export"],
    )


def _folder(services, name: str, parent_id: str | None = None) -> dict:
    with services.database.immediate_session() as session:
        return create_project_folder(session, name, parent_id)["folder"]


def _project_aliases(services):
    source = _session(services, "Source")
    branch = _session(services, "German output", target_language="de")
    standalone = _session(services, "Standalone")
    with services.database.session() as session:
        artifact = Artifact(
            session_id=source.id,
            kind="srt",
            role="correction",
            relative_path="legacy/source.srt",
            content_hash="a" * 64,
            state="current",
        )
        session.add(artifact)
        session.flush()
        project = TranslationProject(
            name="Languages",
            source_session_id=source.id,
            checkpoint_artifact_id=artifact.id,
            source_content_hash=artifact.content_hash,
            source_language="en",
        )
        session.add(project)
        session.flush()
        session.add(
            TranslationProjectBranch(
                project_id=project.id,
                session_id=branch.id,
                target_language="de",
                source_checkpoint_artifact_id=artifact.id,
                source_content_hash=artifact.content_hash,
            )
        )
    return source, branch, standalone


def test_upgrade_from_0057_keeps_sessions_artifacts_and_settings(tmp_path: Path):
    database_path = tmp_path / "legacy.sqlite3"
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    command.upgrade(config, "0057_segment_lineage_child_index")

    database = Database(database_path)
    with database.session() as session:
        record = SessionRecord(
            id="legacy-session",
            name="Legacy session",
            storage_key="legacy-storage",
            workflow_kind="subtitles",
            source_language="en",
            included_stages_json=["transcribe"],
        )
        session.add(record)
        session.flush()
        artifact = Artifact(
            session_id=record.id,
            kind="srt",
            role="transcription",
            relative_path="legacy/legacy.srt",
            content_hash="b" * 64,
        )
        session.add_all(
            [
                artifact,
                SessionSetting(
                    session_id=record.id,
                    section="tts",
                    value_json={"voice": "saved"},
                ),
                AppSetting(key="legacy.setting", value_json={"kept": True}),
            ]
        )
    database.dispose()

    command.upgrade(config, "head")
    database = Database(database_path)
    try:
        with database.session() as session:
            record = session.get(SessionRecord, "legacy-session")
            assert record is not None and record.name == "Legacy session"
            assert session.scalar(
                select(Artifact).where(Artifact.session_id == record.id)
            ).content_hash == "b" * 64
            assert session.get(SessionSetting, (record.id, "tts")).value_json == {
                "voice": "saved"
            }
            assert session.get(AppSetting, "legacy.setting").value_json == {"kept": True}
            assert create_project_folder(session, "Migrated")["folder"]["revision"] == 1
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == SCHEMA_HEAD
    finally:
        database.dispose()


def test_nesting_reparenting_rename_cycles_and_casefolded_siblings(folder_app):
    services, _client, _headers = folder_app
    root = _folder(services, " Root ")
    sibling = _folder(services, "Sibling", root["id"])
    child = _folder(services, "Child", sibling["id"])
    with services.database.immediate_session() as session:
        with pytest.raises(ValueError, match="descendant"):
            update_project_folder(
                session,
                root["id"],
                expected_revision=1,
                parent_id=child["id"],
                change_parent=True,
            )
        with pytest.raises(ValueError, match="sibling"):
            create_project_folder(session, "sibling", root["id"])
        with pytest.raises(ValueError, match="sibling"):
            create_project_folder(session, "rOoT")

    with services.database.immediate_session() as session:
        changed = update_project_folder(
            session,
            child["id"],
            expected_revision=1,
            name=" Renamed ",
            parent_id=root["id"],
            change_name=True,
            change_parent=True,
        )["folder"]
        assert (changed["name"], changed["parent_id"], changed["revision"]) == (
            "Renamed",
            root["id"],
            2,
        )
        unchanged = update_project_folder(
            session,
            child["id"],
            expected_revision=2,
            name="Renamed",
            parent_id=root["id"],
            change_name=True,
            change_parent=True,
        )["folder"]
        assert unchanged["revision"] == 2
        with pytest.raises(RevisionConflict):
            update_project_folder(
                session,
                child["id"],
                expected_revision=1,
                name="Stale",
                change_name=True,
            )


def test_membership_order_alias_deduplication_and_noop_revision(folder_app):
    services, _client, _headers = folder_app
    source, branch, standalone = _project_aliases(services)
    folder = _folder(services, "Ordered")
    with services.database.immediate_session() as session:
        result = replace_folder_projects(
            session,
            folder["id"],
            expected_revision=1,
            project_ids=[source.id, branch.id, standalone.id, branch.id],
        )
        assert result["memberships"] == [
            {"folder_id": folder["id"], "project_id": source.id, "position": 0},
            {"folder_id": folder["id"], "project_id": standalone.id, "position": 1},
        ]
        assert next(item for item in result["items"] if item["id"] == folder["id"])[
            "revision"
        ] == 2
        again = replace_folder_projects(
            session,
            folder["id"],
            expected_revision=2,
            project_ids=[source.id, branch.id, standalone.id],
        )
        assert next(item for item in again["items"] if item["id"] == folder["id"])[
            "revision"
        ] == 2
        reordered = replace_folder_projects(
            session,
            folder["id"],
            expected_revision=2,
            project_ids=[standalone.id, source.id],
        )
        assert [row["project_id"] for row in reordered["memberships"]] == [
            standalone.id,
            source.id,
        ]
        assert [row["position"] for row in reordered["memberships"]] == [0, 1]


def test_session_memberships_fence_union_compact_positions_and_rollback(folder_app):
    services, _client, _headers = folder_app
    target = _session(services, "Target")
    first = _session(services, "First")
    last = _session(services, "Last")
    folder_a = _folder(services, "A")
    folder_b = _folder(services, "B")
    with services.database.immediate_session() as session:
        replace_folder_projects(
            session,
            folder_a["id"],
            expected_revision=1,
            project_ids=[first.id, target.id, last.id],
        )
        before = project_folder_snapshot(session)
        with pytest.raises(RevisionConflict, match="Reload saved memberships"):
            set_session_project_folders(
                session,
                target.id,
                folder_ids=[folder_b["id"]],
                expected_revisions={folder_a["id"]: 2},
            )
        with pytest.raises(RevisionConflict):
            set_session_project_folders(
                session,
                target.id,
                folder_ids=[folder_b["id"]],
                expected_revisions={folder_a["id"]: 1, folder_b["id"]: 1},
            )
        assert project_folder_snapshot(session) == before

        result = set_session_project_folders(
            session,
            target.id,
            folder_ids=[folder_b["id"]],
            expected_revisions={folder_a["id"]: 2, folder_b["id"]: 1},
        )
        assert result["project_id"] == target.id
        assert {
            folder_id: [
                (item["project_id"], item["position"])
                for item in result["memberships"]
                if item["folder_id"] == folder_id
            ]
            for folder_id in {item["folder_id"] for item in result["memberships"]}
        } == {
            folder_a["id"]: [(first.id, 0), (last.id, 1)],
            folder_b["id"]: [(target.id, 0)],
        }
        after = project_folder_snapshot(session)
        folder_revisions = {
            item["id"]: item["revision"] for item in after["items"]
        }
        assert folder_revisions[folder_a["id"]] == 3
        assert folder_revisions[folder_b["id"]] == 2
        noop = set_session_project_folders(
            session,
            target.id,
            folder_ids=[folder_b["id"]],
            expected_revisions={folder_b["id"]: 2},
        )
        assert noop["items"] == after["items"]
        with pytest.raises(ValueError, match="duplicates"):
            set_session_project_folders(
                session,
                target.id,
                folder_ids=[folder_b["id"], folder_b["id"]],
                expected_revisions={folder_b["id"]: 2},
            )


def test_stale_and_unexpected_membership_fences_reject_without_writes(folder_app):
    services, _client, _headers = folder_app
    target = _session(services, "Target")
    folder_a = _folder(services, "A")
    folder_b = _folder(services, "B")
    folder_c = _folder(services, "C")

    with services.database.immediate_session() as session:
        replace_folder_projects(
            session,
            folder_a["id"],
            expected_revision=1,
            project_ids=[target.id],
        )
    with services.database.immediate_session() as session:
        set_session_project_folders(
            session,
            target.id,
            folder_ids=[],
            expected_revisions={folder_a["id"]: 2},
        )

    with services.database.immediate_session() as session:
        before = project_folder_snapshot(session)
        with pytest.raises(RevisionConflict, match="Reload saved memberships"):
            set_session_project_folders(
                session,
                target.id,
                folder_ids=[folder_b["id"]],
                expected_revisions={folder_a["id"]: 2, folder_b["id"]: 1},
            )
        after = project_folder_snapshot(session)
        assert after == before
        assert session.get(ProjectFolder, folder_a["id"]).revision == 3
        assert session.get(ProjectFolder, folder_b["id"]).revision == 1

        with pytest.raises(RevisionConflict, match="Reload saved memberships"):
            set_session_project_folders(
                session,
                target.id,
                folder_ids=[folder_b["id"]],
                expected_revisions={folder_b["id"]: 1, folder_c["id"]: 1},
            )
        assert project_folder_snapshot(session) == before
        assert session.get(ProjectFolder, folder_b["id"]).revision == 1
        assert session.get(ProjectFolder, folder_c["id"]).revision == 1

        retried = set_session_project_folders(
            session,
            target.id,
            folder_ids=[folder_b["id"]],
            expected_revisions={folder_b["id"]: 1},
        )
        assert retried["memberships"] == [
            {"folder_id": folder_b["id"], "project_id": target.id, "position": 0}
        ]
        revisions = {item["id"]: item["revision"] for item in retried["items"]}
        assert revisions[folder_a["id"]] == 3
        assert revisions[folder_b["id"]] == 2
        assert revisions[folder_c["id"]] == 1


def test_project_folder_list_uses_one_sqlite_snapshot(folder_app):
    services, client, _headers = folder_app
    target = _session(services, "Concurrent target")
    did_write = False

    def write_after_folder_select(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal did_write
        if (
            did_write
            or not statement.lstrip().lower().startswith("select")
            or "project_folders" not in statement.lower()
        ):
            return
        did_write = True
        with services.database.immediate_session() as session:
            created = create_project_folder(session, "Concurrent folder")
            replace_folder_projects(
                session,
                created["folder"]["id"],
                expected_revision=1,
                project_ids=[target.id],
            )

    event.listen(
        services.database.engine,
        "after_cursor_execute",
        write_after_folder_select,
    )
    try:
        first = client.get("/api/v1/project-folders").get_json()
    finally:
        event.remove(
            services.database.engine,
            "after_cursor_execute",
            write_after_folder_select,
        )

    assert did_write
    assert first == {"items": [], "memberships": []}
    second = client.get("/api/v1/project-folders").get_json()
    assert len(second["items"]) == 1
    folder_id = second["items"][0]["id"]
    assert second["memberships"] == [
        {"folder_id": folder_id, "project_id": target.id, "position": 0}
    ]


def test_folder_delete_promotes_children_and_preserves_session_data(folder_app):
    services, _client, _headers = folder_app
    root = _folder(services, "Root")
    deleted = _folder(services, "Delete me", root["id"])
    child = _folder(services, "Child", deleted["id"])
    grandchild = _folder(services, "Grandchild", child["id"])
    record = _session(services, "Keep me")
    file_path = services.paths.sessions / record.storage_key / "keep.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("retained", encoding="utf-8")
    with services.database.immediate_session() as session:
        session.add(Job(kind="test.job", session_id=record.id, status="completed"))
        replace_folder_projects(
            session,
            deleted["id"],
            expected_revision=1,
            project_ids=[record.id],
        )
        deleted_snapshot = delete_project_folder(
            session, deleted["id"], expected_revision=2
        )
    child_after = next(item for item in deleted_snapshot["items"] if item["id"] == child["id"])
    assert child_after["parent_id"] == root["id"]
    assert child_after["revision"] == 2
    grandchild_after = next(
        item for item in deleted_snapshot["items"] if item["id"] == grandchild["id"]
    )
    assert grandchild_after["parent_id"] == child["id"]
    assert deleted_snapshot["memberships"] == []
    with services.database.session() as session:
        assert session.get(SessionRecord, record.id) is not None
        assert session.scalar(select(Job).where(Job.session_id == record.id)) is not None
    assert file_path.read_text(encoding="utf-8") == "retained"


def test_delete_rejects_promotion_collision_without_partial_changes(folder_app):
    services, _client, _headers = folder_app
    parent = _folder(services, "Parent")
    child = _folder(services, "Shared", parent["id"])
    conflict = _folder(services, "shared")
    with services.database.immediate_session() as session:
        with pytest.raises(FolderPromotionConflict, match="rename or move"):
            delete_project_folder(session, parent["id"], expected_revision=1)
    with services.database.session() as session:
        assert session.get(ProjectFolder, parent["id"]).revision == 1
        assert session.get(ProjectFolder, child["id"]).parent_id == parent["id"]
        assert session.get(ProjectFolder, conflict["id"]) is not None


def test_trashed_memberships_remain_and_purge_cascade_removes_them(folder_app):
    services, _client, _headers = folder_app
    record = _session(services, "Trash then purge")
    folder = _folder(services, "Archive")
    with services.database.immediate_session() as session:
        session.get(SessionRecord, record.id).trashed_at = utcnow()
        result = replace_folder_projects(
            session,
            folder["id"],
            expected_revision=1,
            project_ids=[record.id],
        )
        assert result["memberships"][0]["project_id"] == record.id
        session.get(SessionRecord, record.id).trashed_at = None
    with services.database.session() as session:
        assert len(project_folder_snapshot(session)["memberships"]) == 1
    with services.database.immediate_session() as session:
        session.delete(session.get(SessionRecord, record.id))
    with services.database.session() as session:
        assert project_folder_snapshot(session)["memberships"] == []
        with pytest.raises(KeyError):
            replace_folder_projects(
                session,
                folder["id"],
                expected_revision=2,
                project_ids=[record.id],
            )


def test_missing_and_purging_sessions_are_rejected(folder_app):
    services, _client, _headers = folder_app
    folder = _folder(services, "Test")
    record = _session(services, "Purging")
    with services.database.immediate_session() as session:
        session.add(
            SessionPurge(
                session_id=record.id,
                state="purging",
                manifest_json=[],
                expected_revision=record.revision,
                impact_token="c" * 64,
            )
        )
        with pytest.raises(KeyError):
            replace_folder_projects(
                session,
                folder["id"],
                expected_revision=1,
                project_ids=["missing-session"],
            )
        with pytest.raises(ValueError, match="purged"):
            replace_folder_projects(
                session,
                folder["id"],
                expected_revision=1,
                project_ids=[record.id],
            )


def test_http_scope_csrf_idempotency_and_openapi_parity(folder_app):
    _services, client, headers = folder_app
    read = client.get("/api/v1/project-folders")
    assert read.status_code == 200
    assert read.get_json() == {"items": [], "memberships": []}

    missing_csrf = client.post(
        "/api/v1/project-folders",
        json={"name": "No CSRF"},
        headers={"Idempotency-Key": "folder-no-csrf-0001"},
    )
    assert missing_csrf.status_code == 403

    no_key = client.post(
        "/api/v1/project-folders", json={"name": "No key"}, headers=headers
    )
    assert no_key.status_code == 400

    write_headers = {**headers, "Idempotency-Key": "folder-create-http-0001"}
    created = client.post(
        "/api/v1/project-folders", json={"name": "HTTP"}, headers=write_headers
    )
    assert created.status_code == 201
    folder = created.get_json()["folder"]
    replay = client.post(
        "/api/v1/project-folders", json={"name": "HTTP"}, headers=write_headers
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == created.get_json()

    patch_headers = {**headers, "Idempotency-Key": "folder-patch-http-0001"}
    moved = client.patch(
        f"/api/v1/project-folders/{folder['id']}",
        json={"expected_revision": 1, "parent_id": None},
        headers=patch_headers,
    )
    assert moved.status_code == 200

    document = client.get("/api/v1/openapi.json").get_json()
    paths = document["paths"]
    assert set(paths["/api/v1/project-folders"]) == {"get", "post"}
    assert "patch" in paths["/api/v1/project-folders/{folderId}"]
    assert "delete" in paths["/api/v1/project-folders/{folderId}"]
    for path, method in (
        ("/api/v1/project-folders", "post"),
        ("/api/v1/project-folders/{folderId}", "patch"),
        ("/api/v1/project-folders/{folderId}", "delete"),
        ("/api/v1/project-folders/{folderId}/projects", "put"),
        ("/api/v1/sessions/{sessionId}/project-folders", "put"),
    ):
        assert any(
            parameter["name"] == "Idempotency-Key" and parameter["required"]
            for parameter in paths[path][method]["parameters"]
        )

    runtime = {
        (rule.rule.replace("<folder_id>", "{folderId}").replace("<session_id>", "{sessionId}"), method.lower())
        for rule in client.application.url_map.iter_rules()
        for method in rule.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    for path, operations in paths.items():
        for method in operations:
            assert (path, method) in runtime or path not in {
                "/api/v1/project-folders",
                "/api/v1/project-folders/{folderId}",
                "/api/v1/project-folders/{folderId}/projects",
                "/api/v1/sessions/{sessionId}/project-folders",
            }
