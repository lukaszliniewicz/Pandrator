from __future__ import annotations

import shutil
import sqlite3
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SAWarning, SQLAlchemyError

from pandrator.web.database import sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0071_provider_workspace"
PREVIOUS_REVISION = "0070_credential_namespaces"
TABLE = "providers"
MODEL_TABLE = "provider_models"
WORKSPACE_FK = "fk_providers_workspace_id_workspaces"
WORKSPACE_INDEX = "ix_providers_workspace_id"
OLD_IDENTITY = "uq_provider_identity"
SCOPED_IDENTITY = "uq_provider_workspace_identity"
NOW = "2026-10-23 13:20:00.123456"
UPDATED = "2026-10-23 13:21:01.654321"

ProviderRow = tuple[str, str, str, str, int, str | None, str | None, str, int, str, str]
ProviderModelRow = tuple[Any, ...]


@pytest.fixture(scope="session")
def schema_at_0070(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("provider-schema-base") / "at-0070.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _owner_workspace(connection: sqlite3.Connection) -> tuple[str, str]:
    rows = connection.execute(
        "SELECT users.id, workspaces.id FROM users JOIN workspaces "
        "ON workspaces.personal_owner_user_id = users.id "
        "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
    ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1])


def _provider_row(
    provider_id: str,
    *,
    kind: str = "llm",
    provider_key: str = "sample",
    label: str = "Sample provider",
    enabled: int = 1,
    base_url: str | None = "https://fixture.invalid/v1",
    secret_ref: str | None = "db:tts:provider-schema-fixture",
    options_json: str = '{ "opaque": [ 1, 2 ], "note": "preserve spacing" }',
    revision: int = 7,
    created_at: str = NOW,
    updated_at: str = UPDATED,
) -> ProviderRow:
    return (
        provider_id,
        kind,
        provider_key,
        label,
        enabled,
        base_url,
        secret_ref,
        options_json,
        revision,
        created_at,
        updated_at,
    )


def _provider_model_row(
    provider_id: str,
    *,
    model_record_id: str = "model-record-a",
    model_id: str = "sample-model-v2",
    is_active: int = 1,
    is_default: int = 0,
    temperature: float | None = 0.375,
    reasoning_effort: str | None = "high",
    input_cost: float | None = 1.25,
    cached_input_cost: float | None = 0.25,
    output_cost: float | None = 2.5,
    context_window: int = 65536,
    max_output: int | None = 4096,
    input_modalities: str = '[ "text", "image" ]',
    output_modalities: str = '["text"]',
    options_json: str = '{"capability": [ 1, 2 ], "raw": true}',
    revision: int = 9,
) -> ProviderModelRow:
    return (
        model_record_id,
        provider_id,
        model_id,
        is_active,
        is_default,
        temperature,
        reasoning_effort,
        input_cost,
        cached_input_cost,
        output_cost,
        context_window,
        max_output,
        input_modalities,
        output_modalities,
        options_json,
        revision,
        NOW,
        UPDATED,
    )


def _legacy_table_sql(
    *, identity_name: str | None = OLD_IDENTITY, custom_check: bool = False
) -> str:
    identity = (
        "UNIQUE (kind, provider_key, label)"
        if identity_name is None
        else f"CONSTRAINT {identity_name} UNIQUE (kind, provider_key, label)"
    )
    check = (
        ", CONSTRAINT ck_providers_revision_positive CHECK (revision > 0)" if custom_check else ""
    )
    return f"""CREATE TABLE providers (
        id VARCHAR(36) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        provider_key VARCHAR(80) NOT NULL,
        label VARCHAR(160) NOT NULL,
        enabled BOOLEAN NOT NULL,
        base_url TEXT,
        secret_ref VARCHAR(255),
        options_json JSON NOT NULL,
        revision INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        {identity}{check}
    )"""


def _insert_provider(
    connection: sqlite3.Connection,
    row: ProviderRow,
    *,
    workspace_id: str | None = None,
) -> None:
    columns = (
        "id, kind, provider_key, label, enabled, base_url, secret_ref, "
        "options_json, revision, created_at, updated_at"
    )
    values: tuple[Any, ...] = row
    if workspace_id is not None or "workspace_id" in {
        str(column[1]) for column in connection.execute("PRAGMA table_info(providers)")
    }:
        columns = "workspace_id, " + columns
        values = (workspace_id, *row)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO providers ({columns}) VALUES ({placeholders})",
        values,
    )


def _insert_provider_model(connection: sqlite3.Connection, row: ProviderModelRow) -> None:
    connection.execute(
        "INSERT INTO provider_models "
        "(id, provider_id, model_id, is_active, is_default, default_temperature, "
        "default_reasoning_effort, input_cost_per_million, cached_input_cost_per_million, "
        "output_cost_per_million, context_window_tokens, max_output_tokens, "
        "input_modalities_json, output_modalities_json, options_json, revision, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )


def _legacy_database(
    schema_at_0070: Path,
    path: Path,
    *,
    providers: Sequence[ProviderRow] = (),
    models: Sequence[ProviderModelRow] = (),
    custom_index: bool = False,
    custom_check: bool = False,
    identity_name: str | None = OLD_IDENTITY,
) -> None:
    shutil.copy2(schema_at_0070, path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE providers")
        connection.execute(
            _legacy_table_sql(
                identity_name=identity_name,
                custom_check=custom_check,
            )
        )
        for row in providers:
            _insert_provider(connection, row)
        for row in models:
            _insert_provider_model(connection, row)
        if custom_index:
            connection.execute(
                "CREATE INDEX custom_provider_label_desc ON providers (label DESC, provider_key)"
            )


def _target_table_sql(
    *,
    workspace_type: str = "VARCHAR(36)",
    with_workspace_fk: bool = True,
    scoped_unique_columns: Sequence[str] = (
        "workspace_id",
        "kind",
        "provider_key",
        "label",
    ),
) -> str:
    workspace_fk = (
        ", CONSTRAINT fk_providers_workspace_id_workspaces "
        "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT"
        if with_workspace_fk
        else ""
    )
    unique_columns = ", ".join(scoped_unique_columns)
    return f"""CREATE TABLE providers (
        id VARCHAR(36) NOT NULL,
        kind VARCHAR(32) NOT NULL,
        provider_key VARCHAR(80) NOT NULL,
        label VARCHAR(160) NOT NULL,
        enabled BOOLEAN NOT NULL,
        base_url TEXT,
        secret_ref VARCHAR(255),
        options_json JSON NOT NULL,
        revision INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        workspace_id {workspace_type},
        PRIMARY KEY (id),
        CONSTRAINT uq_provider_workspace_identity UNIQUE ({unique_columns})
        {workspace_fk}
    )"""


def _replace_with_target_shape(
    path: Path,
    *,
    workspace_type: str = "VARCHAR(36)",
    with_workspace_fk: bool = True,
    with_workspace_index: bool = True,
    scoped_unique_columns: Sequence[str] = (
        "workspace_id",
        "kind",
        "provider_key",
        "label",
    ),
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE providers")
        connection.execute(
            _target_table_sql(
                workspace_type=workspace_type,
                with_workspace_fk=with_workspace_fk,
                scoped_unique_columns=scoped_unique_columns,
            )
        )
        if with_workspace_index:
            connection.execute(f"CREATE INDEX {WORKSPACE_INDEX} ON providers (workspace_id)")


def _provider_rows(path: Path, *, target: bool) -> list[tuple[Any, ...]]:
    columns = (
        "workspace_id, id, kind, provider_key, label, enabled, base_url, secret_ref, "
        "options_json, revision, created_at, updated_at"
        if target
        else "id, kind, provider_key, label, enabled, base_url, secret_ref, "
        "options_json, revision, created_at, updated_at"
    )
    order = "workspace_id, id" if target else "id"
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT {columns} FROM providers ORDER BY {order}").fetchall()


def _provider_model_rows(path: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT id, provider_id, model_id, is_active, is_default, "
            "default_temperature, default_reasoning_effort, input_cost_per_million, "
            "cached_input_cost_per_million, output_cost_per_million, context_window_tokens, "
            "max_output_tokens, input_modalities_json, output_modalities_json, options_json, "
            "revision, created_at, updated_at FROM provider_models ORDER BY id"
        ).fetchall()


def _index_sql(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'providers' AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
    return {str(name): str(sql) for name, sql in rows}


def _check_constraints(path: Path) -> list[dict[str, Any]]:
    engine = sa.create_engine(sqlite_url(path))
    try:
        return [dict(constraint) for constraint in sa.inspect(engine).get_check_constraints(TABLE)]
    finally:
        engine.dispose()


def _database_snapshot(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as connection:
        objects = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        table_names = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        data: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
        for (table_name_raw,) in table_names:
            table_name = str(table_name_raw)
            columns = connection.execute(
                f'PRAGMA table_info("{table_name.replace(chr(34), chr(34) * 2)}")'
            ).fetchall()
            primary_key = [
                str(column[1]) for column in sorted(columns, key=lambda item: item[5]) if column[5]
            ]
            order_by = primary_key or [str(column[1]) for column in columns]
            order_sql = ", ".join('"' + column.replace('"', '""') + '"' for column in order_by)
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            rows = connection.execute(
                f"SELECT * FROM {quoted_table} ORDER BY {order_sql}"
            ).fetchall()
            data.append((table_name, tuple(tuple(row) for row in rows)))
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    return objects, tuple(data), str(version)


def _assert_no_batch_tables(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("_alembic_tmp_") for name in names)


def _assert_upgrade_failure_is_atomic(path: Path, *, message: str | None = None) -> None:
    before = _database_snapshot(path)
    pattern = pytest.raises(RuntimeError, match=message) if message else pytest.raises(RuntimeError)
    with pattern:
        command.upgrade(_config(path), REVISION)
    assert _database_snapshot(path) == before
    _assert_no_batch_tables(path)


def _run_with_sa_warnings_as_errors(callback: Any) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        callback()


def test_legacy_providers_and_models_are_adopted_without_data_or_index_changes(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    path = tmp_path / "legacy-provider-adoption.sqlite"
    provider_rows = [
        _provider_row("provider-a"),
        _provider_row(
            "provider-b",
            kind="tts",
            provider_key="voice-service",
            label="Voice service",
            enabled=0,
            base_url=None,
            secret_ref="db:tts:voice-service:fixture-key",
            options_json=' { "auth": {"mode":"reference"}, "order": [3, 1] } ',
            revision=2,
        ),
    ]
    model_rows = [
        _provider_model_row("provider-a"),
        _provider_model_row(
            "provider-b",
            model_record_id="model-record-b",
            model_id="voice-model-v1",
            is_active=0,
            is_default=1,
            temperature=None,
            reasoning_effort=None,
            input_cost=None,
            cached_input_cost=None,
            output_cost=0.75,
            context_window=32768,
            max_output=None,
            input_modalities='["text"]',
            output_modalities='[ "audio" ]',
            options_json=' {"supports_streaming": false} ',
            revision=3,
        ),
    ]
    _legacy_database(
        schema_at_0070,
        path,
        providers=provider_rows,
        models=model_rows,
        custom_index=True,
        custom_check=True,
    )
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
    old_providers = _provider_rows(path, target=False)
    old_models = _provider_model_rows(path)
    old_indexes = _index_sql(path)
    old_checks = _check_constraints(path)
    with sqlite3.connect(path) as connection:
        old_defaults = {
            str(row[1]): row[4] for row in connection.execute("PRAGMA table_info(providers)")
        }

    _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))

    expected_providers = [
        (workspace_id, *row) for row in sorted(old_providers, key=lambda item: item[0])
    ]
    assert _provider_rows(path, target=True) == expected_providers
    assert _provider_model_rows(path) == old_models
    assert _check_constraints(path) == old_checks
    expected_indexes = {
        **old_indexes,
        WORKSPACE_INDEX: "CREATE INDEX ix_providers_workspace_id ON providers (workspace_id)",
    }
    assert _index_sql(path) == expected_indexes
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == REVISION
        )
        defaults = {
            str(row[1]): row[4] for row in connection.execute("PRAGMA table_info(providers)")
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {key: defaults[key] for key in old_defaults} == old_defaults

    engine = sa.create_engine(sqlite_url(path))
    try:
        provider_fks = sa.inspect(engine).get_foreign_keys(TABLE)
        model_fks = sa.inspect(engine).get_foreign_keys(MODEL_TABLE)
    finally:
        engine.dispose()
    assert provider_fks == [
        {
            "name": WORKSPACE_FK,
            "constrained_columns": ["workspace_id"],
            "referred_schema": None,
            "referred_table": "workspaces",
            "referred_columns": ["id"],
            "options": {"ondelete": "RESTRICT"},
        }
    ]
    assert model_fks == [
        {
            "name": None,
            "constrained_columns": ["provider_id"],
            "referred_schema": None,
            "referred_table": TABLE,
            "referred_columns": ["id"],
            "options": {"ondelete": "CASCADE"},
        }
    ]


def test_workspace_scoped_identity_and_fk_delete_behavior(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    path = tmp_path / "provider-scoped-identity.sqlite"
    _legacy_database(
        schema_at_0070,
        path,
        providers=[_provider_row("provider-a")],
        models=[_provider_model_row("provider-a")],
    )
    _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _owner_id, initial_workspace_id = _owner_workspace(connection)
        connection.execute(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
            "VALUES (?, 'team', ?, NULL, 'active', 1, ?, ?)",
            ("workspace-second", "Second", NOW, UPDATED),
        )
        _insert_provider(
            connection,
            _provider_row("provider-second"),
            workspace_id="workspace-second",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_provider(
                connection,
                _provider_row("provider-duplicate"),
                workspace_id="workspace-second",
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM workspaces WHERE id = ?", ("workspace-second",))
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM providers WHERE kind = ? AND provider_key = ? AND label = ?",
                ("llm", "sample", "Sample provider"),
            ).fetchone()[0]
            == 2
        )
        connection.execute("DELETE FROM providers WHERE id = ?", ("provider-a",))
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM provider_models WHERE provider_id = ?", ("provider-a",)
            ).fetchone()[0]
            == 0
        )
        assert initial_workspace_id


def test_empty_and_null_owned_early_targets_are_adopted(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    empty_path = tmp_path / "empty-early-provider-target.sqlite"
    shutil.copy2(schema_at_0070, empty_path)
    before_objects = _database_snapshot(empty_path)[0]
    command.upgrade(_config(empty_path), REVISION)
    assert _database_snapshot(empty_path)[0] == before_objects
    assert _database_snapshot(empty_path)[2] == REVISION

    populated_path = tmp_path / "null-owned-early-provider-target.sqlite"
    shutil.copy2(schema_at_0070, populated_path)
    with sqlite3.connect(populated_path) as connection:
        _insert_provider(connection, _provider_row("early-provider"))
        _owner_id, workspace_id = _owner_workspace(connection)
    command.upgrade(_config(populated_path), REVISION)
    assert _provider_rows(populated_path, target=True) == [
        (workspace_id, *_provider_row("early-provider"))
    ]


@pytest.mark.parametrize(
    ("shape", "message"),
    [
        ("partial", "unexpected foreign-key shape"),
        ("bad_column", "unexpected definition"),
        ("missing_fk", "unexpected foreign-key shape"),
        ("missing_index", "workspace index is malformed"),
        ("bad_unique", "scoped provider identity constraint is malformed"),
        ("old_unique", "scoped provider identity constraint is malformed"),
    ],
)
def test_malformed_early_target_fails_before_ddl(
    schema_at_0070: Path, tmp_path: Path, shape: str, message: str
) -> None:
    path = tmp_path / f"malformed-target-{shape}.sqlite"
    _legacy_database(schema_at_0070, path)
    if shape == "partial":
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE providers ADD COLUMN workspace_id VARCHAR(36)")
    else:
        _replace_with_target_shape(
            path,
            workspace_type="TEXT" if shape == "bad_column" else "VARCHAR(36)",
            with_workspace_fk=shape != "missing_fk",
            with_workspace_index=shape != "missing_index",
            scoped_unique_columns=(
                ("workspace_id", "kind", "provider_key")
                if shape == "bad_unique"
                else ("workspace_id", "kind", "provider_key", "label")
            ),
        )
        if shape == "old_unique":
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE UNIQUE INDEX old_provider_identity "
                    "ON providers (kind, provider_key, label)"
                )
    _assert_upgrade_failure_is_atomic(path, message=message)


def test_populated_early_owner_values_fail_before_ddl(schema_at_0070: Path, tmp_path: Path) -> None:
    path = tmp_path / "populated-early-owner.sqlite"
    shutil.copy2(schema_at_0070, path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
        _insert_provider(connection, _provider_row("already-owned"), workspace_id=workspace_id)
    _assert_upgrade_failure_is_atomic(path, message="must all be NULL")


def test_duplicate_early_identity_fails_before_ddl(schema_at_0070: Path, tmp_path: Path) -> None:
    path = tmp_path / "duplicate-early-identity.sqlite"
    shutil.copy2(schema_at_0070, path)
    with sqlite3.connect(path) as connection:
        _insert_provider(connection, _provider_row("duplicate-one"))
        _insert_provider(connection, _provider_row("duplicate-two"))
    _assert_upgrade_failure_is_atomic(path, message="identities contain duplicates")


def test_invalid_seed_and_orphan_model_rows_fail_before_ddl(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    invalid_seed_path = tmp_path / "invalid-provider-seed.sqlite"
    _legacy_database(schema_at_0070, invalid_seed_path, providers=[_provider_row("provider-a")])
    with sqlite3.connect(invalid_seed_path) as connection:
        owner_id, workspace_id = _owner_workspace(connection)
        connection.execute(
            "DELETE FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, owner_id),
        )
    _assert_upgrade_failure_is_atomic(invalid_seed_path, message="membership is missing")

    orphan_path = tmp_path / "orphan-provider-model.sqlite"
    _legacy_database(
        schema_at_0070,
        orphan_path,
        models=[_provider_model_row("provider-does-not-exist")],
    )
    _assert_upgrade_failure_is_atomic(orphan_path, message="no provider parent")


def test_unrecognized_inbound_provider_relation_fails_before_ddl(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    path = tmp_path / "unknown-provider-inbound.sqlite"
    _legacy_database(schema_at_0070, path, providers=[_provider_row("provider-a")])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE provider_notes (id TEXT PRIMARY KEY, provider_id TEXT "
            "REFERENCES providers(id))"
        )
    _assert_upgrade_failure_is_atomic(path, message="unrecognized table")


def test_late_version_failure_rolls_back_retry_repeat_and_downgrade(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    path = tmp_path / "provider-late-version-failure.sqlite"
    providers = [
        _provider_row("provider-a"),
        _provider_row(
            "provider-b",
            kind="tts",
            provider_key="voice-service",
            label="Voice service",
            enabled=0,
            options_json=' {"preserve": [ 9, 4 ]} ',
        ),
    ]
    models = [_provider_model_row("provider-a")]
    _legacy_database(
        schema_at_0070,
        path,
        providers=providers,
        models=models,
        custom_index=True,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_provider_version_update "
            "BEFORE UPDATE OF version_num ON alembic_version "
            f"WHEN NEW.version_num = '{REVISION}' "
            "BEGIN SELECT RAISE(ABORT, 'late provider migration failure'); END"
        )
    before = _database_snapshot(path)
    with pytest.raises(SQLAlchemyError):
        _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))
    assert _database_snapshot(path) == before
    _assert_no_batch_tables(path)

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_provider_version_update")
    old_rows = _provider_rows(path, target=False)
    old_models = _provider_model_rows(path)
    old_indexes = _index_sql(path)
    old_checks = _check_constraints(path)
    _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))
    upgraded_snapshot = _database_snapshot(path)
    with sqlite3.connect(path) as connection:
        _owner_id, workspace_id = _owner_workspace(connection)
    assert _provider_rows(path, target=True) == [
        (workspace_id, *row) for row in sorted(old_rows, key=lambda item: item[0])
    ]
    assert _provider_model_rows(path) == old_models
    assert _check_constraints(path) == old_checks
    assert _index_sql(path) == {
        **old_indexes,
        WORKSPACE_INDEX: "CREATE INDEX ix_providers_workspace_id ON providers (workspace_id)",
    }
    _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))
    assert _database_snapshot(path) == upgraded_snapshot

    _run_with_sa_warnings_as_errors(lambda: command.downgrade(_config(path), PREVIOUS_REVISION))
    assert _provider_rows(path, target=False) == old_rows
    assert _provider_model_rows(path) == old_models
    assert _check_constraints(path) == old_checks
    assert _index_sql(path) == old_indexes
    assert _database_snapshot(path)[2] == PREVIOUS_REVISION
    _assert_no_batch_tables(path)
    _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))
    assert _provider_rows(path, target=True) == [
        (workspace_id, *row) for row in sorted(old_rows, key=lambda item: item[0])
    ]


def test_downgrade_refuses_noninitial_workspace_before_ddl(
    schema_at_0070: Path, tmp_path: Path
) -> None:
    path = tmp_path / "provider-downgrade-refusal.sqlite"
    _legacy_database(schema_at_0070, path, providers=[_provider_row("provider-a")])
    command.upgrade(_config(path), REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
            "VALUES (?, 'team', ?, NULL, 'active', 1, ?, ?)",
            ("workspace-other", "Other", NOW, UPDATED),
        )
        _insert_provider(
            connection,
            _provider_row("provider-other"),
            workspace_id="workspace-other",
        )
    before = _database_snapshot(path)
    with pytest.raises(RuntimeError, match="non-initial workspace"):
        command.downgrade(_config(path), PREVIOUS_REVISION)
    assert _database_snapshot(path) == before
    _assert_no_batch_tables(path)


@pytest.mark.parametrize("identity_name", [OLD_IDENTITY, None])
def test_named_and_unnamed_legacy_identity_constraints_are_replaced_cleanly(
    schema_at_0070: Path, tmp_path: Path, identity_name: str | None
) -> None:
    label = identity_name or "unnamed"
    path = tmp_path / f"provider-identity-name-{label}.sqlite"
    _legacy_database(
        schema_at_0070,
        path,
        providers=[_provider_row("provider-a")],
        identity_name=identity_name,
    )
    _run_with_sa_warnings_as_errors(lambda: command.upgrade(_config(path), REVISION))
    _run_with_sa_warnings_as_errors(lambda: command.downgrade(_config(path), PREVIOUS_REVISION))
    assert _provider_rows(path, target=False) == [_provider_row("provider-a")]
