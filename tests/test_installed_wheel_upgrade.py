from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from pandrator.web.database import SCHEMA_HEAD

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
FIXTURE = ROOT / "tests" / "fixtures" / "schema-0058-installed-wheel.sql.gz"
FIXTURE_METADATA = ROOT / "tests" / "fixtures" / "schema-0058-installed-wheel.sql.json"
FIXTURE_SHA256 = "4e32115df911ee3255401bd7c0b91406d0cd4201e4cd812ba3e060571e3204d9"
FIXTURE_SQL_SHA256 = "4a3a651cac33883af4a73aae5b12970506a7655dfc616d52f888d752b8154c4c"
OLD_REVISION = "0058_project_folders"
PREDECESSOR_REVISION = "0080_configuration_scope_consistency"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", str(URL.create("sqlite", database=str(path))))
    return config


def _load_fixture(path: Path) -> dict[str, Any]:
    compressed = FIXTURE.read_bytes()
    metadata: dict[str, Any] = json.loads(FIXTURE_METADATA.read_text(encoding="utf-8"))
    assert hashlib.sha256(compressed).hexdigest() == FIXTURE_SHA256
    assert metadata["gzip_sha256"] == FIXTURE_SHA256
    sql = gzip.decompress(compressed)
    assert hashlib.sha256(sql).hexdigest() == FIXTURE_SQL_SHA256
    assert metadata["sql_sha256"] == FIXTURE_SQL_SHA256
    assert metadata["schema"] == OLD_REVISION
    assert metadata["non_version_tables"] == 80
    assert metadata["non_version_rows"] == 17
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(sql.decode("utf-8"))
    return metadata


def _snapshot(
    connection: sqlite3.Connection,
    old_columns: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]]:
    if old_columns is None:
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT GLOB 'sqlite_*' AND name != 'alembic_version' ORDER BY name"
            )
        ]
        old_columns = {
            table: tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
            )
            for table in table_names
        }

    result: dict[str, tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]] = {}
    for table, columns in old_columns.items():
        projection = ", ".join(_quote(column) for column in columns)
        rows = [
            tuple(row) for row in connection.execute(f"SELECT {projection} FROM {_quote(table)}")
        ]
        result[table] = (columns, tuple(sorted(rows, key=repr)))
    return result


def _database_state(path: Path) -> tuple[str, tuple[str, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None
        return str(row[0]), tuple(connection.iterdump())


def test_installed_fixture_upgrade_preserves_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "installed-schema-0058.sqlite3"
    metadata = _load_fixture(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            OLD_REVISION,
        )
        before = _snapshot(connection)
    assert len(before) == metadata["non_version_tables"]
    assert sum(len(rows) for _columns, rows in before.values()) == metadata["non_version_rows"]

    command.upgrade(_config(path), "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            SCHEMA_HEAD,
        )
        after = _snapshot(connection, {table: value[0] for table, value in before.items()})
        # 0072 moves non-host defaults into the initial personal workspace.
        # Every other old row stays exact; the moved setting is checked below.
        assert after["app_settings"] == (before["app_settings"][0], ())
        assert {key: value for key, value in after.items() if key != "app_settings"} == {
            key: value for key, value in before.items() if key != "app_settings"
        }
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        ids = metadata["seed_ids"]
        session_projects = connection.execute(
            "SELECT COUNT(*), COUNT(project_id), COUNT(DISTINCT project_id), MIN(project_id) "
            "FROM sessions WHERE id IN (?, ?)",
            (ids["source_session_id"], ids["branch_session_id"]),
        ).fetchone()
        assert session_projects is not None and tuple(session_projects[:3]) == (2, 2, 1)
        project_id = str(session_projects[3])
        workspace = connection.execute(
            "SELECT workspaces.id, workspaces.kind, workspaces.state, "
            "workspaces.personal_owner_user_id FROM workspaces "
            "JOIN projects ON projects.workspace_id = workspaces.id WHERE projects.id = ?",
            (project_id,),
        ).fetchone()
        assert workspace is not None and tuple(workspace[1:3]) == ("personal", "active")
        workspace_id = str(workspace[0])
        assert connection.execute(
            "SELECT id FROM users WHERE legacy_subject='owner'"
        ).fetchall() == [(workspace[3],)]
        moved_settings = connection.execute(
            "SELECT key,value_json,revision,updated_at FROM workspace_settings "
            "WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
        assert tuple(sorted(moved_settings, key=repr)) == before["app_settings"][1]

        folder_ids = (ids["parent_folder_id"], ids["child_folder_id"])
        assert connection.execute(
            "SELECT COUNT(*) FROM project_folders WHERE id IN (?, ?) AND workspace_id = ?",
            (*folder_ids, workspace_id),
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(DISTINCT folder_id) FROM project_folder_memberships "
            "WHERE folder_id IN (?, ?) AND project_session_id = ? AND project_id = ?",
            (*folder_ids, ids["source_session_id"], project_id),
        ).fetchone() == (2,)

        artifact_ids = (ids["source_audio_artifact_id"], ids["transcription_artifact_id"])
        assert connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE id IN (?, ?) "
            "AND workspace_id = ? AND project_id = ?",
            (*artifact_ids, workspace_id, project_id),
        ).fetchone() == (2,)
        job = connection.execute(
            "SELECT status, attempts FROM jobs WHERE id = ?", (ids["job_id"],)
        ).fetchone()
        assert job == ("queued", 0)


@pytest.mark.parametrize("action", ("CASCADE", "NO ACTION"))
def test_unnamed_api_token_foreign_key_action_is_rejected_atomically(
    tmp_path: Path, action: str
) -> None:
    path = tmp_path / f"installed-schema-0080-{action.lower().replace(' ', '-')}.sqlite3"
    _load_fixture(path)
    command.upgrade(_config(path), PREDECESSOR_REVISION)
    assert _database_state(path)[0] == PREDECESSOR_REVISION
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM api_tokens").fetchone() == (0,)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE api_tokens")
        connection.execute(
            "CREATE TABLE api_tokens ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY, "
            "user_id VARCHAR(36), user_auth_revision INTEGER, "
            f"FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE {action})"
        )
        connection.commit()
        foreign_key = connection.execute('PRAGMA foreign_key_list("api_tokens")').fetchone()
        assert foreign_key is not None and foreign_key[6] == action
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)

    _, before = _database_state(path)
    with pytest.raises(RuntimeError, match="unexpected foreign-key target or action"):
        command.upgrade(_config(path), "0081_credential_resource_grants")
    assert _database_state(path) == (PREDECESSOR_REVISION, before)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
