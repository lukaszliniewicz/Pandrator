from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TypeAlias
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    PronunciationEntry,
    Provider,
    SessionRecord,
    StoredCredential,
    WorkspaceSetting,
    WorkspaceSettingHistory,
)
from pandrator.web.projects.models import Project

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
TARGET_REVISION = "0080_configuration_scope_consistency"
PREDECESSOR_REVISION = "0079_execution_scope_consistency"
TRIGGER_PREFIX = "pandrator_configuration_scope_"
TRIGGERS = (
    ("pandrator_configuration_scope_providers_update", "providers"),
    ("pandrator_configuration_scope_stored_credentials_update", "stored_credentials"),
    ("pandrator_configuration_scope_workspace_settings_update", "workspace_settings"),
    (
        "pandrator_configuration_scope_workspace_settings_history_update",
        "workspace_settings_history",
    ),
    (
        "pandrator_configuration_scope_pronunciation_entries_insert",
        "pronunciation_entries",
    ),
    (
        "pandrator_configuration_scope_pronunciation_entries_update",
        "pronunciation_entries",
    ),
)

_SqlParams: TypeAlias = dict[str, object]
_NOW = "2026-10-24T12:00:00+00:00"
_SYNTHETIC_SECRET = "configuration-scope-test-secret-one"
_ROTATED_SECRET = "configuration-scope-test-secret-two"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _make_valid_predecessor(path: Path) -> dict[str, str]:
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    database = Database(path)
    ids: dict[str, str] = {}
    try:
        with database.session() as session:
            main_workspace = initial_personal_workspace(session)
            active_workspace = Workspace(kind="team", name="Active retained workspace")
            empty_workspace = Workspace(kind="team", name="Empty target workspace")
            archived_workspace = Workspace(
                kind="team",
                name="Archived retained workspace",
                state="archived",
            )
            session.add_all([active_workspace, empty_workspace, archived_workspace])
            session.flush()

            project_main = Project(
                workspace_id=main_workspace.id,
                source_session_id=None,
                name="Main project",
                created_by_user_id=None,
            )
            project_same_workspace = Project(
                workspace_id=main_workspace.id,
                source_session_id=None,
                name="Second project in the same workspace",
                created_by_user_id=None,
            )
            project_active = Project(
                workspace_id=active_workspace.id,
                source_session_id=None,
                name="Active workspace project",
                created_by_user_id=None,
            )
            project_archived = Project(
                workspace_id=archived_workspace.id,
                source_session_id=None,
                name="Archived project",
                created_by_user_id=None,
            )
            session.add_all(
                [project_main, project_same_workspace, project_active, project_archived]
            )
            session.flush()

            session_main = SessionRecord(
                id=str(uuid4()),
                project_id=project_main.id,
                name="Main session",
                source_language="en",
            )
            session_same_workspace = SessionRecord(
                id=str(uuid4()),
                project_id=project_same_workspace.id,
                name="Second same-workspace session",
                source_language="en",
            )
            session_active = SessionRecord(
                id=str(uuid4()),
                project_id=project_active.id,
                name="Active workspace session",
                source_language="en",
            )
            session_archived = SessionRecord(
                id=str(uuid4()),
                project_id=project_archived.id,
                name="Archived session",
                source_language="en",
            )
            session.add_all(
                [session_main, session_same_workspace, session_active, session_archived]
            )
            session.flush()

            workspace_rows = (
                (main_workspace.id, "main", project_main.id, session_main.id),
                (
                    active_workspace.id,
                    "active",
                    project_active.id,
                    session_active.id,
                ),
                (
                    archived_workspace.id,
                    "archived",
                    project_archived.id,
                    session_archived.id,
                ),
            )
            for workspace_id, label, project_id, session_id in workspace_rows:
                session.add_all(
                    [
                        Provider(
                            workspace_id=workspace_id,
                            kind="fixture",
                            provider_key="configuration-scope-fixture",
                            label=f"Provider {label}",
                            enabled=True,
                            options_json={"fixture": label},
                        ),
                        StoredCredential(
                            workspace_id=workspace_id,
                            key="fixture-credential",
                            label="synthetic fixture credential",
                            secret_value=_SYNTHETIC_SECRET,
                            revision=1,
                        ),
                        WorkspaceSetting(
                            workspace_id=workspace_id,
                            key="fixture.setting",
                            value_json={"fixture": label, "revision": 1},
                            revision=1,
                        ),
                        WorkspaceSettingHistory(
                            workspace_id=workspace_id,
                            key="fixture.setting",
                            value_json={"fixture": label, "revision": 1},
                            revision=1,
                        ),
                        PronunciationEntry(
                            workspace_id=workspace_id,
                            project_id=None,
                            scope="global",
                            session_id=None,
                            source_form=f"Global {label}",
                            normalized_form=f"global-{label}",
                            language="en",
                            phonetic=f"global-{label}",
                            backend="fixture",
                        ),
                        PronunciationEntry(
                            workspace_id=workspace_id,
                            project_id=project_id,
                            scope="session",
                            session_id=session_id,
                            source_form=f"Session {label}",
                            normalized_form=f"session-{label}",
                            language="en",
                            phonetic=f"session-{label}",
                            backend="fixture",
                        ),
                    ]
                )
            session.flush()

            provider = session.query(Provider).filter_by(workspace_id=main_workspace.id).one()
            ids.update(
                {
                    "workspace_main": main_workspace.id,
                    "workspace_active": active_workspace.id,
                    "workspace_empty": empty_workspace.id,
                    "workspace_archived": archived_workspace.id,
                    "project_main": project_main.id,
                    "project_same_workspace": project_same_workspace.id,
                    "project_active": project_active.id,
                    "project_archived": project_archived.id,
                    "session_main": session_main.id,
                    "session_same_workspace": session_same_workspace.id,
                    "session_active": session_active.id,
                    "session_archived": session_archived.id,
                    "provider_main": provider.id,
                    "credential_key": "fixture-credential",
                    "setting_key": "fixture.setting",
                }
            )
            ids["global_main"] = (
                session.query(PronunciationEntry)
                .filter_by(workspace_id=main_workspace.id, scope="global")
                .one()
                .id
            )
            ids["session_entry_main"] = (
                session.query(PronunciationEntry)
                .filter_by(workspace_id=main_workspace.id, scope="session")
                .one()
                .id
            )
    finally:
        database.dispose()
    return ids


def _make_target_case(path: Path) -> tuple[Config, dict[str, str]]:
    ids = _make_valid_predecessor(path)
    config = _config(path)
    command.upgrade(config, TARGET_REVISION)
    return config, ids


def _schema_without_owned_triggers(path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    return tuple(tuple(row) for row in rows if not str(row[1]).startswith(TRIGGER_PREFIX))


def _safe_rows(path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    queries = {
        "workspaces": (
            "SELECT id, kind, name, personal_owner_user_id, state, revision "
            "FROM workspaces ORDER BY id"
        ),
        "projects": (
            "SELECT id, workspace_id, source_session_id, name, created_by_user_id, revision "
            "FROM projects ORDER BY id"
        ),
        "sessions": "SELECT id, project_id, name, status, revision FROM sessions ORDER BY id",
        "providers": (
            "SELECT id, kind, provider_key, label, enabled, base_url, secret_ref, "
            "options_json, revision, workspace_id, created_at, updated_at "
            "FROM providers ORDER BY id"
        ),
        "stored_credentials": (
            "SELECT workspace_id, key, label, created_at, updated_at, revision "
            "FROM stored_credentials ORDER BY workspace_id, key"
        ),
        "workspace_settings": (
            "SELECT workspace_id, key, value_json, revision, updated_at "
            "FROM workspace_settings ORDER BY workspace_id, key"
        ),
        "workspace_settings_history": (
            "SELECT id, workspace_id, key, value_json, revision, created_at "
            "FROM workspace_settings_history ORDER BY id"
        ),
        "pronunciation_entries": (
            "SELECT id, workspace_id, project_id, scope, session_id, source_form, "
            "normalized_form, language, phonetic, alphabet, backend, status, source, "
            "notes, metadata_json, revision, created_at, updated_at "
            "FROM pronunciation_entries ORDER BY id"
        ),
    }
    with sqlite3.connect(path) as connection:
        return {
            table_name: tuple(tuple(row) for row in connection.execute(query).fetchall())
            for table_name, query in queries.items()
        }


def _synthetic_credentials_match(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT count(*) FROM stored_credentials WHERE secret_value = ?",
            (_SYNTHETIC_SECRET,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _trigger_rows(path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    return tuple(
        (str(name), str(table_name), str(sql))
        for name, table_name, sql in rows
        if str(name).startswith(TRIGGER_PREFIX)
    )


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _foreign_key_violations(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def _connection_rows(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    queries = {
        "providers": "SELECT id, workspace_id, label FROM providers ORDER BY id",
        "stored_credentials": (
            "SELECT workspace_id, key, label, revision "
            "FROM stored_credentials ORDER BY workspace_id, key"
        ),
        "workspace_settings": (
            "SELECT workspace_id, key, value_json, revision "
            "FROM workspace_settings ORDER BY workspace_id, key"
        ),
        "workspace_settings_history": (
            "SELECT id, workspace_id, key, value_json, revision "
            "FROM workspace_settings_history ORDER BY id"
        ),
        "pronunciation_entries": (
            "SELECT id, workspace_id, project_id, scope, session_id, phonetic, revision "
            "FROM pronunciation_entries ORDER BY id"
        ),
    }
    return {
        table_name: tuple(tuple(row) for row in connection.execute(query).fetchall())
        for table_name, query in queries.items()
    }


def _assert_abort(
    connection: sqlite3.Connection,
    statement: str,
    parameters: _SqlParams,
    message: str,
) -> None:
    before = _connection_rows(connection)
    with pytest.raises(sqlite3.IntegrityError, match=re.escape(message)):
        connection.execute(statement, parameters)
    assert _connection_rows(connection) == before


def _pronunciation_values(
    *,
    workspace_id: str,
    scope: str = "global",
    project_id: str | None = None,
    session_id: str | None = None,
) -> _SqlParams:
    identity = str(uuid4())
    return {
        "id": identity,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "scope": scope,
        "session_id": session_id,
        "source_form": f"Fixture {identity}",
        "normalized_form": f"fixture-{identity}",
        "language": "en",
        "phonetic": f"fixture-{identity}",
        "alphabet": "respelling",
        "backend": "fixture",
        "status": "reviewed",
        "source": "manual",
        "metadata_json": "{}",
        "revision": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


_INSERT_PRONUNCIATION = """INSERT INTO pronunciation_entries (
    id, workspace_id, project_id, scope, session_id, source_form,
    normalized_form, language, phonetic, alphabet, backend, status,
    source, metadata_json, revision, created_at, updated_at
) VALUES (
    :id, :workspace_id, :project_id, :scope, :session_id, :source_form,
    :normalized_form, :language, :phonetic, :alphabet, :backend, :status,
    :source, :metadata_json, :revision, :created_at, :updated_at
)"""


def _foreign_key_actions(path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("PRAGMA foreign_key_list(pronunciation_entries)").fetchall()
    return tuple(sorted((str(row[3]), str(row[2]), str(row[6]).upper()) for row in rows))


def _corrupt_predecessor(path: Path, case: str, ids: dict[str, str]) -> None:
    corruptions: dict[str, tuple[str, _SqlParams]] = {
        "provider_missing_workspace": (
            "UPDATE providers SET workspace_id = :missing WHERE id = :id",
            {"missing": "missing-workspace", "id": ids["provider_main"]},
        ),
        "credential_missing_workspace": (
            "UPDATE stored_credentials SET workspace_id = :missing "
            "WHERE workspace_id = :workspace AND key = :key",
            {
                "missing": "missing-workspace",
                "workspace": ids["workspace_main"],
                "key": ids["credential_key"],
            },
        ),
        "setting_missing_workspace": (
            "UPDATE workspace_settings SET workspace_id = :missing "
            "WHERE workspace_id = :workspace AND key = :key",
            {
                "missing": "missing-workspace",
                "workspace": ids["workspace_main"],
                "key": ids["setting_key"],
            },
        ),
        "history_missing_workspace": (
            "UPDATE workspace_settings_history SET workspace_id = :missing "
            "WHERE workspace_id = :workspace AND key = :key",
            {
                "missing": "missing-workspace",
                "workspace": ids["workspace_main"],
                "key": ids["setting_key"],
            },
        ),
        "pronunciation_invalid_scope": (
            "UPDATE pronunciation_entries SET scope = 'project' WHERE id = :id",
            {"id": ids["global_main"]},
        ),
        "pronunciation_missing_session": (
            "UPDATE pronunciation_entries SET session_id = 'missing-session' WHERE id = :id",
            {"id": ids["session_entry_main"]},
        ),
        "pronunciation_missing_project": (
            "UPDATE pronunciation_entries SET project_id = 'missing-project' WHERE id = :id",
            {"id": ids["session_entry_main"]},
        ),
        "pronunciation_session_project_mismatch": (
            "UPDATE pronunciation_entries SET project_id = :project WHERE id = :id",
            {
                "project": ids["project_same_workspace"],
                "id": ids["session_entry_main"],
            },
        ),
        "pronunciation_foreign_workspace_project": (
            "UPDATE pronunciation_entries SET workspace_id = :workspace WHERE id = :id",
            {"workspace": ids["workspace_archived"], "id": ids["session_entry_main"]},
        ),
    }
    statement, parameters = corruptions[case]
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(statement, parameters)


def _replace_stored_credentials_without_workspace_fk(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE stored_credentials")
        connection.execute(
            "CREATE TABLE stored_credentials ("
            "workspace_id VARCHAR(36) NOT NULL, key VARCHAR(255) NOT NULL, "
            "label VARCHAR(160) NOT NULL, secret_value TEXT NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "revision INTEGER NOT NULL, PRIMARY KEY (workspace_id, key))"
        )


def _replace_sessions_without_project_fk(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE sessions")
        connection.execute(
            "CREATE TABLE sessions ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY, project_id VARCHAR(36) NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER pandrator_project_graph_sessions_update "
            "BEFORE UPDATE OF project_id ON sessions "
            "WHEN NEW.project_id IS NOT OLD.project_id BEGIN "
            "SELECT RAISE(ABORT, 'session project is immutable'); END"
        )


def _replace_projects_without_workspace_fk(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE projects")
        connection.execute(
            "CREATE TABLE projects ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY, "
            "workspace_id VARCHAR(36) NOT NULL, "
            "source_session_id VARCHAR(36))"
        )


def test_fresh_upgrade_installs_six_triggers_and_repeats_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh-configuration-scope.sqlite3"
    config = _config(path)

    command.upgrade(config, TARGET_REVISION)
    first = _trigger_rows(path)
    assert tuple((name, table) for name, table, _sql in first) == tuple(sorted(TRIGGERS))
    assert len(first) == 6
    sql_by_name = {name: sql for name, _table, sql in first}
    assert (
        "BEFORE UPDATE OF workspace_id ON providers"
        in sql_by_name["pandrator_configuration_scope_providers_update"]
    )
    assert (
        "BEFORE INSERT ON pronunciation_entries"
        in sql_by_name["pandrator_configuration_scope_pronunciation_entries_insert"]
    )
    assert (
        "BEFORE UPDATE OF workspace_id, project_id, session_id, scope"
        in sql_by_name["pandrator_configuration_scope_pronunciation_entries_update"]
    )
    assert _version(path) == TARGET_REVISION
    assert _foreign_key_violations(path) == []

    command.upgrade(config, TARGET_REVISION)
    assert _trigger_rows(path) == first
    assert _foreign_key_violations(path) == []


def test_populated_upgrade_preserves_rows_and_round_trips_only_owned_triggers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "populated-configuration-scope.sqlite3"
    config = _config(path)
    ids = _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fixture_unrelated_configuration_trigger "
            "AFTER UPDATE OF label ON providers BEGIN SELECT NEW.id; END"
        )
        connection.execute(
            "CREATE INDEX fixture_unrelated_configuration_index "
            "ON workspace_settings (key, revision)"
        )
    before_schema = _schema_without_owned_triggers(path)
    before_rows = _safe_rows(path)
    assert _synthetic_credentials_match(path) == 3

    command.upgrade(config, TARGET_REVISION)

    assert _version(path) == TARGET_REVISION
    assert _schema_without_owned_triggers(path) == before_schema
    assert _safe_rows(path) == before_rows
    assert _synthetic_credentials_match(path) == 3
    assert len(_trigger_rows(path)) == 6
    assert _foreign_key_violations(path) == []
    assert ids["workspace_archived"] != ids["workspace_main"]

    command.downgrade(config, PREDECESSOR_REVISION)
    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema
    assert _safe_rows(path) == before_rows
    assert _synthetic_credentials_match(path) == 3

    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert len(_trigger_rows(path)) == 6
    assert _schema_without_owned_triggers(path) == before_schema
    assert _safe_rows(path) == before_rows
    assert _synthetic_credentials_match(path) == 3
    assert _foreign_key_violations(path) == []


@pytest.mark.parametrize(
    ("table", "where", "parameters"),
    [
        ("providers", "id = :id", {"id_key": "provider_main"}),
        (
            "stored_credentials",
            "workspace_id = :workspace AND key = :key",
            {"id_key": "credential_key", "workspace": "workspace_main"},
        ),
        (
            "workspace_settings",
            "workspace_id = :workspace AND key = :key",
            {"id_key": "setting_key", "workspace": "workspace_main"},
        ),
        (
            "workspace_settings_history",
            "workspace_id = :workspace AND key = :key",
            {"id_key": "setting_key", "workspace": "workspace_main"},
        ),
    ],
)
@pytest.mark.parametrize("destination", ["other_workspace", None])
def test_configuration_workspace_roots_are_immutable_including_null_transitions(
    tmp_path: Path,
    table: str,
    where: str,
    parameters: dict[str, str],
    destination: str | None,
) -> None:
    path = tmp_path / f"immutable-{table}-{destination}.sqlite3"
    _config_value, ids = _make_target_case(path)
    key = parameters["id_key"]
    binds: _SqlParams = {name: ids[value] for name, value in parameters.items() if name != "id_key"}
    if table == "providers":
        binds["id"] = ids[key]
    elif "key" in where:
        binds["key"] = ids[key]
    if destination is None:
        binds["destination"] = None
    else:
        binds["destination"] = ids["workspace_empty"]
    statement = f"UPDATE {table} SET workspace_id = :destination WHERE {where}"

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_abort(connection, statement, binds, "workspace is immutable")


def test_configuration_value_updates_and_upserts_remain_allowed(tmp_path: Path) -> None:
    path = tmp_path / "configuration-value-updates.sqlite3"
    _config_value, ids = _make_target_case(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE providers SET label = 'updated provider label', "
            "provider_key = 'updated-fixture-provider', base_url = 'https://fixture.invalid', "
            "options_json = '{\"updated\":true}', enabled = 0, revision = revision + 1 "
            "WHERE id = :id",
            {"id": ids["provider_main"]},
        )
        connection.execute(
            "UPDATE stored_credentials SET secret_value = :secret, revision = revision + 1 "
            "WHERE workspace_id = :workspace AND key = :key",
            {
                "secret": _ROTATED_SECRET,
                "workspace": ids["workspace_main"],
                "key": ids["credential_key"],
            },
        )
        connection.execute(
            "INSERT INTO workspace_settings "
            "(workspace_id, key, value_json, revision, updated_at) "
            "VALUES (:workspace, :key, '{\"updated\":true}', 2, :now) "
            "ON CONFLICT(workspace_id, key) DO UPDATE SET "
            "value_json = excluded.value_json, "
            "revision = workspace_settings.revision + 1, "
            "updated_at = excluded.updated_at",
            {
                "workspace": ids["workspace_main"],
                "key": ids["setting_key"],
                "now": _NOW,
            },
        )
        connection.execute(
            "UPDATE workspace_settings_history SET value_json = '{\"updated\":true}', "
            "revision = revision + 1 WHERE key = :key",
            {"key": ids["setting_key"]},
        )
        connection.execute(
            "UPDATE pronunciation_entries SET phonetic = 'updated pronunciation' "
            "WHERE id IN (:global, :session)",
            {"global": ids["global_main"], "session": ids["session_entry_main"]},
        )
        assert connection.execute(
            "SELECT count(*) FROM stored_credentials "
            "WHERE workspace_id = :workspace AND key = :key AND secret_value = :secret",
            {
                "workspace": ids["workspace_main"],
                "key": ids["credential_key"],
                "secret": _ROTATED_SECRET,
            },
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM workspace_settings "
            "WHERE workspace_id = :workspace AND key = :key AND revision = 2",
            {"workspace": ids["workspace_main"], "key": ids["setting_key"]},
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pronunciation_entries "
            "WHERE id IN (:global, :session) AND phonetic = 'updated pronunciation'",
            {"global": ids["global_main"], "session": ids["session_entry_main"]},
        ).fetchone() == (2,)
        connection.execute(
            "UPDATE workspace_settings SET key = 'fixture.setting.renamed' "
            "WHERE workspace_id = :workspace AND key = :key",
            {"workspace": ids["workspace_main"], "key": ids["setting_key"]},
        )
        connection.execute(
            "UPDATE workspace_settings_history SET key = 'fixture.setting.renamed' "
            "WHERE key = :key",
            {"key": ids["setting_key"]},
        )


def test_pronunciation_insert_update_and_same_workspace_retarget_are_allowed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pronunciation-root-updates.sqlite3"
    _config_value, ids = _make_target_case(path)

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        inserted_global = _pronunciation_values(workspace_id=ids["workspace_main"])
        connection.execute(_INSERT_PRONUNCIATION, inserted_global)
        inserted_session = _pronunciation_values(
            workspace_id=ids["workspace_main"],
            scope="session",
            project_id=ids["project_main"],
            session_id=ids["session_main"],
        )
        connection.execute(_INSERT_PRONUNCIATION, inserted_session)
        connection.execute(
            "UPDATE pronunciation_entries SET scope = 'session', project_id = :project, "
            "session_id = :session WHERE id = :id",
            {
                "project": ids["project_main"],
                "session": ids["session_main"],
                "id": ids["global_main"],
            },
        )
        connection.execute(
            "UPDATE pronunciation_entries SET scope = 'global', project_id = NULL, "
            "session_id = NULL WHERE id = :id",
            {"id": ids["session_entry_main"]},
        )
        connection.execute(
            "UPDATE pronunciation_entries SET project_id = :project, session_id = :session "
            "WHERE id = :id",
            {
                "project": ids["project_same_workspace"],
                "session": ids["session_same_workspace"],
                "id": inserted_session["id"],
            },
        )
        assert connection.execute(
            "SELECT scope, project_id, session_id FROM pronunciation_entries WHERE id = :id",
            {"id": ids["global_main"]},
        ).fetchone() == ("session", ids["project_main"], ids["session_main"])
        assert connection.execute(
            "SELECT scope, project_id, session_id FROM pronunciation_entries WHERE id = :id",
            {"id": ids["session_entry_main"]},
        ).fetchone() == ("global", None, None)
        assert connection.execute(
            "SELECT project_id, session_id FROM pronunciation_entries WHERE id = :id",
            {"id": inserted_session["id"]},
        ).fetchone() == (ids["project_same_workspace"], ids["session_same_workspace"])


@pytest.mark.parametrize(
    ("scope", "project_kind", "session_kind", "workspace_kind", "message"),
    [
        ("project", "none", "none", "main", "pronunciation scope is invalid"),
        (
            "global",
            "main",
            "none",
            "main",
            "global pronunciation cannot have project or session roots",
        ),
        (
            "session",
            "main",
            "none",
            "main",
            "session pronunciation roots are inconsistent",
        ),
        (
            "session",
            "main",
            "missing",
            "main",
            "session pronunciation roots are inconsistent",
        ),
        (
            "session",
            "missing",
            "main",
            "main",
            "session pronunciation roots are inconsistent",
        ),
        (
            "session",
            "same-workspace",
            "main",
            "main",
            "session pronunciation roots are inconsistent",
        ),
        (
            "session",
            "main",
            "main",
            "archived",
            "session pronunciation roots are inconsistent",
        ),
        (
            "global",
            "none",
            "none",
            "missing",
            "pronunciation workspace does not exist",
        ),
    ],
)
def test_pronunciation_insert_rejects_invalid_scope_and_roots(
    tmp_path: Path,
    scope: str,
    project_kind: str,
    session_kind: str,
    workspace_kind: str,
    message: str,
) -> None:
    path = tmp_path / f"pronunciation-insert-{uuid4()}.sqlite3"
    _config_value, ids = _make_target_case(path)
    project_ids = {
        "none": None,
        "main": ids["project_main"],
        "same-workspace": ids["project_same_workspace"],
        "missing": "missing-project",
    }
    session_ids = {
        "none": None,
        "main": ids["session_main"],
        "missing": "missing-session",
    }
    workspace_ids = {
        "main": ids["workspace_main"],
        "archived": ids["workspace_archived"],
        "missing": "missing-workspace",
    }
    parameters = _pronunciation_values(
        workspace_id=workspace_ids[workspace_kind],
        scope=scope,
        project_id=project_ids[project_kind],
        session_id=session_ids[session_kind],
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_abort(connection, _INSERT_PRONUNCIATION, parameters, message)


@pytest.mark.parametrize(
    ("statement", "values", "message_key"),
    [
        (
            "UPDATE pronunciation_entries SET scope = 'project' WHERE id = :id",
            "global_main",
            "pronunciation scope is invalid",
        ),
        (
            "UPDATE pronunciation_entries SET project_id = :project WHERE id = :id",
            "global_main",
            "global pronunciation cannot have project or session roots",
        ),
        (
            "UPDATE pronunciation_entries SET project_id = :project WHERE id = :id",
            "session_entry_main",
            "session pronunciation roots are inconsistent",
        ),
        (
            "UPDATE pronunciation_entries SET session_id = :session WHERE id = :id",
            "session_entry_main",
            "session pronunciation roots are inconsistent",
        ),
        (
            "UPDATE pronunciation_entries SET workspace_id = :workspace, "
            "project_id = :project, session_id = :session WHERE id = :id",
            "session_entry_main",
            "pronunciation workspace is immutable",
        ),
    ],
)
def test_pronunciation_update_rejects_invalid_scope_and_root_changes(
    tmp_path: Path, statement: str, values: str, message_key: str
) -> None:
    path = tmp_path / f"pronunciation-update-{uuid4()}.sqlite3"
    _config_value, ids = _make_target_case(path)
    parameters: _SqlParams = {"id": ids[values]}
    if "project" in statement:
        parameters["project"] = ids["project_same_workspace"]
    if "session_id = :session" in statement:
        parameters["session"] = "missing-session"
    if "workspace_id = :workspace" in statement:
        parameters.update(
            {
                "workspace": ids["workspace_archived"],
                "project": ids["project_archived"],
                "session": ids["session_archived"],
            }
        )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_abort(connection, statement, parameters, message_key)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("provider_missing_workspace", "providers has a missing workspace root"),
        (
            "credential_missing_workspace",
            "stored_credentials has a missing workspace root",
        ),
        (
            "setting_missing_workspace",
            "workspace_settings has a missing workspace root",
        ),
        (
            "history_missing_workspace",
            "workspace_settings_history has a missing workspace root",
        ),
        (
            "pronunciation_invalid_scope",
            "pronunciation_entries has an invalid scope or inconsistent roots",
        ),
        (
            "pronunciation_missing_session",
            "pronunciation_entries has an invalid scope or inconsistent roots",
        ),
        (
            "pronunciation_missing_project",
            "pronunciation_entries has an invalid scope or inconsistent roots",
        ),
        (
            "pronunciation_session_project_mismatch",
            "pronunciation_entries has an invalid scope or inconsistent roots",
        ),
        (
            "pronunciation_foreign_workspace_project",
            "pronunciation_entries has an invalid scope or inconsistent roots",
        ),
    ],
)
def test_preflight_rejects_malformed_predecessor_rows_without_repair(
    tmp_path: Path, case: str, message: str
) -> None:
    path = tmp_path / f"preflight-row-{case}.sqlite3"
    ids = _make_valid_predecessor(path)
    _corrupt_predecessor(path, case, ids)
    before_schema = _schema_without_owned_triggers(path)
    before_rows = _safe_rows(path)

    with pytest.raises(RuntimeError, match=re.escape(message)):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema
    assert _safe_rows(path) == before_rows


def test_missing_required_table_fails_before_owned_ddl(tmp_path: Path) -> None:
    path = tmp_path / "missing-configuration-table.sqlite3"
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE workspace_settings_history")
    before_schema = _schema_without_owned_triggers(path)

    with pytest.raises(RuntimeError, match="required tables are missing"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


def test_missing_target_workspace_fk_fails_before_owned_ddl(tmp_path: Path) -> None:
    path = tmp_path / "malformed-configuration-fk.sqlite3"
    _make_valid_predecessor(path)
    _replace_stored_credentials_without_workspace_fk(path)
    before_schema = _schema_without_owned_triggers(path)

    with pytest.raises(
        RuntimeError, match="stored_credentials has an unexpected foreign-key shape"
    ):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


def test_missing_parent_session_project_fk_fails_before_owned_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed-parent-project-fk.sqlite3"
    _make_valid_predecessor(path)
    _replace_sessions_without_project_fk(path)
    before_schema = _schema_without_owned_triggers(path)

    with pytest.raises(RuntimeError, match="sessions.project_id has an unexpected foreign key"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


def test_missing_parent_project_workspace_fk_fails_before_owned_ddl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed-parent-workspace-fk.sqlite3"
    _make_valid_predecessor(path)
    _replace_projects_without_workspace_fk(path)
    before_schema = _schema_without_owned_triggers(path)

    with pytest.raises(RuntimeError, match="projects.workspace_id has an unexpected foreign key"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


@pytest.mark.parametrize("change", ["missing", "altered"])
def test_missing_or_altered_parent_trigger_fails_before_owned_ddl(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / f"parent-trigger-{change}.sqlite3"
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER pandrator_project_graph_projects_update")
        if change == "altered":
            connection.execute(
                "CREATE TRIGGER pandrator_project_graph_projects_update "
                "BEFORE UPDATE OF workspace_id ON projects BEGIN "
                "SELECT RAISE(ABORT, 'altered project trigger'); END"
            )
    before_schema = _schema_without_owned_triggers(path)

    with pytest.raises(RuntimeError, match="required parent trigger"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


def test_owned_trigger_name_collision_fails_before_owned_ddl(tmp_path: Path) -> None:
    path = tmp_path / "configuration-trigger-collision.sqlite3"
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE pandrator_configuration_scope_providers_update (fixture INTEGER)"
        )
    before_schema = _schema_without_owned_triggers(path)

    with pytest.raises(RuntimeError, match="owned trigger name .* already in use"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


def test_mid_create_failure_rolls_back_all_owned_triggers_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "configuration-trigger-rollback.sqlite3"
    _make_valid_predecessor(path)
    before_schema = _schema_without_owned_triggers(path)
    original_execute = Operations.execute
    created = 0

    def fail_during_owned_trigger_creation(
        operations: Operations, statement: object, *args: object, **kwargs: object
    ) -> object:
        nonlocal created
        if isinstance(statement, str) and statement.lstrip().startswith(
            f"CREATE TRIGGER {TRIGGER_PREFIX}"
        ):
            created += 1
            if created == 4:
                raise RuntimeError("injected configuration trigger failure")
        return original_execute(operations, statement, *args, **kwargs)

    monkeypatch.setattr(Operations, "execute", fail_during_owned_trigger_creation)
    with pytest.raises(RuntimeError, match="injected configuration trigger failure"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert created == 4
    assert _version(path) == PREDECESSOR_REVISION
    assert _trigger_rows(path) == ()
    assert _schema_without_owned_triggers(path) == before_schema


@pytest.mark.parametrize("change", ["missing", "altered"])
def test_downgrade_rejects_missing_or_altered_owned_definition_atomically(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / f"owned-trigger-downgrade-{change}.sqlite3"
    config, _ids = _make_target_case(path)
    owned_before = _trigger_rows(path)
    name = TRIGGERS[0][0]
    with sqlite3.connect(path) as connection:
        connection.execute(f'DROP TRIGGER "{name}"')
        if change == "altered":
            connection.execute(
                f'CREATE TRIGGER "{name}" AFTER UPDATE OF label ON providers '
                "BEGIN SELECT NEW.id; END"
            )
    before = _trigger_rows(path)
    with pytest.raises(RuntimeError, match="an owned trigger is missing or altered"):
        command.downgrade(config, PREDECESSOR_REVISION)

    assert _version(path) == TARGET_REVISION
    assert _trigger_rows(path) == before
    assert len(owned_before) == 6


def test_pronunciation_cascade_actions_remain_declared_and_session_delete_cascades(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pronunciation-cascades.sqlite3"
    _config_value, ids = _make_target_case(path)

    assert _foreign_key_actions(path) == (
        ("project_id", "projects", "CASCADE"),
        ("session_id", "sessions", "CASCADE"),
        ("workspace_id", "workspaces", "RESTRICT"),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM sessions WHERE id = :id", {"id": ids["session_main"]})
        assert connection.execute(
            "SELECT count(*) FROM pronunciation_entries WHERE id = :id",
            {"id": ids["session_entry_main"]},
        ).fetchone() == (0,)


def test_archived_workspace_remains_a_valid_storage_root(tmp_path: Path) -> None:
    path = tmp_path / "archived-configuration-root.sqlite3"
    _config_value, ids = _make_target_case(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT state FROM workspaces WHERE id = :id",
            {"id": ids["workspace_archived"]},
        ).fetchone() == ("archived",)
        assert connection.execute(
            "SELECT count(*) FROM providers WHERE workspace_id = :id",
            {"id": ids["workspace_archived"]},
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pronunciation_entries WHERE workspace_id = :id",
            {"id": ids["workspace_archived"]},
        ).fetchone() == (2,)
