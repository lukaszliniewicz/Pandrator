from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Table, create_engine
from sqlalchemy.exc import SQLAlchemyError

from pandrator.web.accounts.actor_models import AuditActorRecord, JobActorRecord
from pandrator.web.database import sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
PREVIOUS_REVISION = "0073_account_authentication"
REVISION = "0074_actor_attribution"
ACTOR_TABLES = {"job_actors", "audit_actors"}
ACTOR_INDEXES = {"ix_job_actors_user_id", "ix_audit_actors_user_id"}
NOW = "2026-10-23 14:15:16.123456"


@pytest.fixture(scope="session")
def schema_at_0073(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("actor-attribution-base") / "at-0073.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    assert not ACTOR_TABLES.intersection(_table_names(path))
    return path


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _full_snapshot(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents = tuple(
            (
                table_name,
                tuple(
                    connection.execute(
                        f"SELECT * FROM {_quote_identifier(table_name)} ORDER BY rowid"
                    ).fetchall()
                ),
            )
            for table_name in tables
        )
    return objects, contents


def _old_graph_snapshot(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
                "AND tbl_name NOT IN ('job_actors', 'audit_actors') "
                "ORDER BY type, name"
            ).fetchall()
        )
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name NOT IN ('alembic_version', 'job_actors', 'audit_actors') "
                "ORDER BY name"
            )
        ]
        contents = tuple(
            (
                table_name,
                tuple(
                    connection.execute(
                        f"SELECT * FROM {_quote_identifier(table_name)} ORDER BY rowid"
                    ).fetchall()
                ),
            )
            for table_name in tables
        )
    return objects, contents


def _seed_old_graph(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        owner = connection.execute("SELECT id FROM users WHERE legacy_subject = 'owner'").fetchone()
        assert owner is not None
        job_id = "job-actor-fixture"
        connection.execute(
            "INSERT INTO jobs (id, kind, status, payload_json, resource_keys_json, progress, "
            "lease_generation, attempts, max_attempts, created_at, updated_at) "
            "VALUES (?, 'fixture', 'queued', '{\"keep\":true}', '[\"old-resource\"]', "
            "0.0, 0, 0, 1, ?, ?)",
            (job_id, NOW, NOW),
        )
        audit_cursor = connection.execute(
            "INSERT INTO audit_events (request_id, principal_subject, principal_kind, "
            "scopes_json, action, method, path, outcome, status_code, duration_ms, "
            "metadata_json, created_at) VALUES ('actor-fixture-request', 'owner', 'owner', "
            "'[]', 'fixture.action', 'POST', '/fixture', 'accepted', 202, 7, "
            "'{\"keep\":true}', ?)",
            (NOW,),
        )
        audit_event_id = audit_cursor.lastrowid
        assert audit_event_id is not None
        return job_id, int(audit_event_id)


def _create_actor_tables(
    path: Path, *, include_job: bool = True, include_audit: bool = True
) -> None:
    engine = create_engine(sqlite_url(path))
    try:
        if include_job:
            cast(Table, JobActorRecord.__table__).create(engine)
        if include_audit:
            cast(Table, AuditActorRecord.__table__).create(engine)
    finally:
        engine.dispose()


def _foreign_key_check(path: Path) -> tuple[tuple[Any, ...], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(connection.execute("PRAGMA foreign_key_check").fetchall())


def _foreign_key_signature(path: Path, table_name: str) -> tuple[tuple[Any, ...], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                f"PRAGMA foreign_key_list({_quote_identifier(table_name)})"
            ).fetchall()
        )


def _index_signatures(
    path: Path, table_name: str
) -> dict[str, tuple[bool, tuple[str | None, ...]]]:
    with sqlite3.connect(path) as connection:
        indexes = connection.execute(
            f"PRAGMA index_list({_quote_identifier(table_name)})"
        ).fetchall()
        result: dict[str, tuple[bool, tuple[str | None, ...]]] = {}
        for index in indexes:
            if str(index[3]) != "c":
                continue
            name = str(index[1])
            columns = connection.execute(f"PRAGMA index_info({_quote_identifier(name)})").fetchall()
            result[name] = (bool(index[2]), tuple(column[2] for column in columns))
    return result


def _actor_target_snapshot(path: Path) -> tuple[Any, ...]:
    names = sorted(ACTOR_TABLES | ACTOR_INDEXES)
    placeholders = ", ".join("?" for _ in names)
    with sqlite3.connect(path) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                f"WHERE name IN ({placeholders}) ORDER BY type, name",
                names,
            ).fetchall()
        )
        contents = tuple(
            (
                table_name,
                tuple(
                    connection.execute(
                        f"SELECT * FROM {_quote_identifier(table_name)} ORDER BY rowid"
                    ).fetchall()
                ),
            )
            for table_name in sorted(ACTOR_TABLES & _table_names(path))
        )
    return objects, contents


def test_fresh_bootstrap_defers_sidecars_and_upgrade_repeat_roundtrips(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh-bootstrap.sqlite"
    command.upgrade(_config(path), "0001_web_schema")
    assert not ACTOR_TABLES.intersection(_table_names(path))
    command.upgrade(_config(path), PREVIOUS_REVISION)
    _seed_old_graph(path)
    before = _old_graph_snapshot(path)
    assert _foreign_key_check(path) == ()

    command.upgrade(_config(path), REVISION)
    assert _version(path) == REVISION
    assert ACTOR_TABLES <= _table_names(path)
    assert _old_graph_snapshot(path) == before
    assert _actor_target_snapshot(path)[1] == (("audit_actors", ()), ("job_actors", ()))
    assert _foreign_key_check(path) == ()

    command.upgrade(_config(path), REVISION)
    assert _version(path) == REVISION
    assert _old_graph_snapshot(path) == before
    assert _foreign_key_check(path) == ()

    command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _version(path) == PREVIOUS_REVISION
    assert _old_graph_snapshot(path) == before
    assert not ACTOR_TABLES.intersection(_table_names(path))
    assert _foreign_key_check(path) == ()


def test_canonical_empty_early_actor_tables_are_adopted_without_old_graph_changes(
    schema_at_0073: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-empty-targets.sqlite"
    shutil.copy2(schema_at_0073, path)
    _seed_old_graph(path)
    before = _old_graph_snapshot(path)
    _create_actor_tables(path)
    assert _actor_target_snapshot(path)[1] == (("audit_actors", ()), ("job_actors", ()))

    command.upgrade(_config(path), REVISION)

    assert _version(path) == REVISION
    assert _old_graph_snapshot(path) == before
    assert _foreign_key_check(path) == ()


@pytest.mark.parametrize("target_state", ["partial", "malformed", "prepopulated", "view_collision"])
def test_bad_early_targets_fail_atomically_and_can_be_repaired(
    schema_at_0073: Path, tmp_path: Path, target_state: str
) -> None:
    path = tmp_path / f"bad-target-{target_state}.sqlite"
    shutil.copy2(schema_at_0073, path)
    job_id, _ = _seed_old_graph(path)
    before_old_graph = _old_graph_snapshot(path)

    if target_state == "partial":
        _create_actor_tables(path, include_audit=False)
    elif target_state == "view_collision":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE VIEW job_actors AS SELECT id FROM jobs")
    else:
        _create_actor_tables(path)
        with sqlite3.connect(path) as connection:
            if target_state == "malformed":
                connection.execute("DROP INDEX ix_job_actors_user_id")
                connection.execute(
                    "CREATE INDEX ix_job_actors_user_id ON job_actors (auth_revision)"
                )
            else:
                connection.execute(
                    "INSERT INTO job_actors "
                    "(job_id, user_id, auth_revision, context_json, created_at) "
                    "VALUES (?, NULL, NULL, ?, ?)",
                    (job_id, '{"version":1,"source":"host","label":"pandrator-cli"}', NOW),
                )

    before_full = _full_snapshot(path)
    with pytest.raises((RuntimeError, SQLAlchemyError)):
        command.upgrade(_config(path), REVISION)
    assert _full_snapshot(path) == before_full
    assert _version(path) == PREVIOUS_REVISION

    if target_state == "partial":
        _create_actor_tables(path, include_job=False)
    elif target_state == "view_collision":
        with sqlite3.connect(path) as connection:
            connection.execute("DROP VIEW job_actors")
    elif target_state == "malformed":
        with sqlite3.connect(path) as connection:
            connection.execute("DROP INDEX ix_job_actors_user_id")
            connection.execute("CREATE INDEX ix_job_actors_user_id ON job_actors (user_id)")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM job_actors")

    command.upgrade(_config(path), REVISION)
    assert _version(path) == REVISION
    assert _old_graph_snapshot(path) == before_old_graph
    assert _foreign_key_check(path) == ()


@pytest.mark.parametrize("malformation", ["check_grouping", "onupdate"])
def test_early_target_rejects_noncanonical_check_or_foreign_key_options(
    schema_at_0073: Path, tmp_path: Path, malformation: str
) -> None:
    path = tmp_path / f"noncanonical-target-{malformation}.sqlite"
    shutil.copy2(schema_at_0073, path)
    _create_actor_tables(path)

    check = (
        "user_id IS NULL AND (auth_revision IS NULL OR user_id IS NOT NULL) "
        "AND auth_revision IS NOT NULL AND auth_revision >= 1"
        if malformation == "check_grouping"
        else "(user_id IS NULL AND auth_revision IS NULL) OR "
        "(user_id IS NOT NULL AND auth_revision IS NOT NULL AND auth_revision >= 1)"
    )
    user_fk_action = (
        "ON UPDATE CASCADE ON DELETE RESTRICT"
        if malformation == "onupdate"
        else "ON DELETE RESTRICT"
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE job_actors")
        connection.execute(
            "CREATE TABLE job_actors ("
            "job_id VARCHAR(36) NOT NULL, user_id VARCHAR(36), "
            "auth_revision INTEGER, context_json JSON NOT NULL, created_at DATETIME NOT NULL, "
            "CONSTRAINT ck_job_actors_user_auth_pair CHECK (" + check + "), PRIMARY KEY (job_id), "
            "CONSTRAINT fk_job_actors_job_id_jobs FOREIGN KEY (job_id) "
            "REFERENCES jobs (id) ON DELETE CASCADE, "
            "CONSTRAINT fk_job_actors_user_id_users FOREIGN KEY (user_id) "
            "REFERENCES users (id) " + user_fk_action + ")"
        )
        connection.execute("CREATE INDEX ix_job_actors_user_id ON job_actors (user_id)")
    before = _full_snapshot(path)

    with pytest.raises(RuntimeError):
        command.upgrade(_config(path), REVISION)
    assert _full_snapshot(path) == before
    assert _version(path) == PREVIOUS_REVISION

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE job_actors")
    _create_actor_tables(path, include_audit=False)
    command.upgrade(_config(path), REVISION)
    assert _version(path) == REVISION
    assert _foreign_key_check(path) == ()


def test_existing_foreign_key_corruption_fails_before_ddl_and_retries_after_repair(
    schema_at_0073: Path, tmp_path: Path
) -> None:
    path = tmp_path / "bad-old-foreign-key.sqlite"
    shutil.copy2(schema_at_0073, path)
    job_id, _ = _seed_old_graph(path)
    old_graph = _old_graph_snapshot(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE jobs SET workspace_id = 'missing-workspace' WHERE id = ?", (job_id,)
        )
    before = _full_snapshot(path)
    assert _foreign_key_check(path)

    with pytest.raises(RuntimeError, match="existing foreign-key integrity is invalid"):
        command.upgrade(_config(path), REVISION)
    assert _full_snapshot(path) == before
    assert _version(path) == PREVIOUS_REVISION

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE jobs SET workspace_id = NULL WHERE id = ?", (job_id,))
    assert _foreign_key_check(path) == ()
    assert _old_graph_snapshot(path) == old_graph
    command.upgrade(_config(path), REVISION)
    assert _version(path) == REVISION
    assert _old_graph_snapshot(path) == old_graph
    assert _foreign_key_check(path) == ()


@pytest.mark.parametrize("source_table", ["users", "jobs", "audit_events"])
def test_source_key_type_and_primary_key_are_preflighted(tmp_path: Path, source_table: str) -> None:
    path = tmp_path / f"bad-key-{source_table}.sqlite"
    ddl = {
        "users": "CREATE TABLE users (id TEXT PRIMARY KEY)",
        "jobs": "CREATE TABLE jobs (id INTEGER PRIMARY KEY)",
        "audit_events": "CREATE TABLE audit_events (id VARCHAR(36) PRIMARY KEY)",
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (PREVIOUS_REVISION,)
        )
        connection.execute("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)")
        connection.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY)")
        connection.execute("DROP TABLE " + _quote_identifier(source_table))
        connection.execute(ddl[source_table])
    before = _full_snapshot(path)

    with pytest.raises(RuntimeError, match="has an unexpected key shape"):
        command.upgrade(_config(path), REVISION)
    assert _full_snapshot(path) == before
    assert _version(path) == PREVIOUS_REVISION
    assert not ACTOR_TABLES.intersection(_table_names(path))


@pytest.mark.parametrize("actor_table", ["job_actors", "audit_actors"])
def test_downgrade_refuses_any_attribution_rows_atomically(
    schema_at_0073: Path, tmp_path: Path, actor_table: str
) -> None:
    path = tmp_path / f"retained-{actor_table}.sqlite"
    shutil.copy2(schema_at_0073, path)
    job_id, audit_event_id = _seed_old_graph(path)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        if actor_table == "job_actors":
            connection.execute(
                "INSERT INTO job_actors "
                "(job_id, user_id, auth_revision, context_json, created_at) "
                "VALUES (?, NULL, NULL, '{}', ?)",
                (job_id, NOW),
            )
        else:
            connection.execute(
                "INSERT INTO audit_actors "
                "(audit_event_id, user_id, auth_revision, context_json, created_at) "
                "VALUES (?, NULL, NULL, '{}', ?)",
                (audit_event_id, NOW),
            )
    before = _full_snapshot(path)

    with pytest.raises(RuntimeError, match="attribution records must be retained"):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _full_snapshot(path) == before
    assert _version(path) == REVISION


def test_downgrade_refuses_unexpected_inbound_actor_foreign_key_before_mutation(
    schema_at_0073: Path, tmp_path: Path
) -> None:
    path = tmp_path / "inbound-actor-reference.sqlite"
    shutil.copy2(schema_at_0073, path)
    _seed_old_graph(path)
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE external_actor_reference (id INTEGER PRIMARY KEY, "
            "job_actor_id VARCHAR(36), FOREIGN KEY(job_actor_id) REFERENCES job_actors(job_id))"
        )
    before = _full_snapshot(path)

    with pytest.raises(RuntimeError, match="another table references an actor sidecar"):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _full_snapshot(path) == before
    assert _version(path) == REVISION
