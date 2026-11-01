from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from pandrator.web.database import sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0064_voice_annotations"
PREVIOUS_REVISION = "0063_voice_workspace"
TABLES = (
    "voice_collections",
    "voice_collection_members",
    "voice_catalog_overrides",
)
OLD_COLUMNS = {
    "voice_collections": (
        "id",
        "name",
        "description",
        "revision",
        "created_at",
        "updated_at",
    ),
    "voice_collection_members": (
        "collection_id",
        "voice_key",
        "voice_ref_json",
        "managed_voice_id",
        "created_at",
    ),
    "voice_catalog_overrides": (
        "voice_key",
        "voice_ref_json",
        "name",
        "description",
        "voice_category",
        "profile_json",
        "revision",
        "created_at",
        "updated_at",
    ),
}
PERSONAL_WORKSPACE = "workspace-active-test"
ARCHIVED_WORKSPACE = "workspace-archived-test"
UNSCOPED_VOICE = "voice-unscoped-test"
ARCHIVED_VOICE = "voice-archived-test"
ACTIVE_VOICE = "voice-active-test"
_TIMESTAMP = "2026-10-23 12:34:56"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0063(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("voice-annotation-base") / "at-0063.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _connect(path: Path) -> Engine:
    return sa.create_engine(sqlite_url(path), future=True)


def _legacy_table_sql(*, server_defaults: bool) -> str:
    defaults = "DEFAULT 1" if server_defaults else ""
    timestamp_default = "DEFAULT CURRENT_TIMESTAMP" if server_defaults else ""
    return f"""
        CREATE TABLE voice_collections (
            id VARCHAR(36) NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            revision INTEGER {defaults} NOT NULL,
            created_at DATETIME {timestamp_default} NOT NULL,
            updated_at DATETIME {timestamp_default} NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (name)
        )
    """


def _create_legacy_annotation_tables(connection: Connection, *, server_defaults: bool) -> None:
    connection.exec_driver_sql("DROP TABLE voice_collection_members")
    connection.exec_driver_sql("DROP TABLE voice_catalog_overrides")
    connection.exec_driver_sql("DROP TABLE voice_collections")
    connection.exec_driver_sql(_legacy_table_sql(server_defaults=server_defaults))
    member_created_default = "DEFAULT CURRENT_TIMESTAMP " if server_defaults else ""
    connection.exec_driver_sql(
        f"""
        CREATE TABLE voice_collection_members (
            collection_id VARCHAR(36) NOT NULL,
            voice_key VARCHAR(1024) NOT NULL,
            voice_ref_json JSON NOT NULL,
            managed_voice_id VARCHAR(36),
            created_at DATETIME {member_created_default}NOT NULL,
            PRIMARY KEY (collection_id, voice_key),
            FOREIGN KEY (collection_id) REFERENCES voice_collections(id) ON DELETE CASCADE,
            FOREIGN KEY (managed_voice_id) REFERENCES voices(id) ON DELETE CASCADE
        )
        """
    )
    override_revision_default = "DEFAULT 1 " if server_defaults else ""
    override_timestamp_default = "DEFAULT CURRENT_TIMESTAMP " if server_defaults else ""
    connection.exec_driver_sql(
        f"""
        CREATE TABLE voice_catalog_overrides (
            voice_key VARCHAR(1024) NOT NULL PRIMARY KEY,
            voice_ref_json JSON NOT NULL,
            name VARCHAR(255),
            description TEXT,
            voice_category VARCHAR(20),
            profile_json JSON,
            revision INTEGER {override_revision_default}NOT NULL,
            created_at DATETIME {override_timestamp_default}NOT NULL,
            updated_at DATETIME {override_timestamp_default}NOT NULL
        )
        """
    )


def _seed_workspaces_and_voices(connection: Connection) -> str:
    owner = connection.execute(
        sa.text("SELECT id FROM users WHERE legacy_subject = 'owner'")
    ).scalar_one()
    personal = connection.execute(
        sa.text(
            "SELECT id FROM workspaces WHERE kind = 'personal' "
            "AND personal_owner_user_id = :owner_id"
        ),
        {"owner_id": owner},
    ).scalar_one()
    connection.execute(
        sa.text(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
            "VALUES (:id, 'team', :name, NULL, :state, 1, :created_at, :updated_at)"
        ),
        [
            {
                "id": PERSONAL_WORKSPACE,
                "name": "Active test workspace",
                "state": "active",
                "created_at": _TIMESTAMP,
                "updated_at": _TIMESTAMP,
            },
            {
                "id": ARCHIVED_WORKSPACE,
                "name": "Archived test workspace",
                "state": "archived",
                "created_at": _TIMESTAMP,
                "updated_at": _TIMESTAMP,
            },
        ],
    )
    connection.execute(
        sa.text(
            "INSERT INTO voices "
            "(id, workspace_id, name, metadata_json, revision, created_at, updated_at) "
            "VALUES (:id, :workspace_id, :name, '{}', 1, :created_at, :updated_at)"
        ),
        [
            {
                "id": ARCHIVED_VOICE,
                "workspace_id": ARCHIVED_WORKSPACE,
                "name": "Archived voice",
                "created_at": _TIMESTAMP,
                "updated_at": _TIMESTAMP,
            },
            {
                "id": ACTIVE_VOICE,
                "workspace_id": PERSONAL_WORKSPACE,
                "name": "Active voice",
                "created_at": _TIMESTAMP,
                "updated_at": _TIMESTAMP,
            },
            {
                "id": UNSCOPED_VOICE,
                "workspace_id": None,
                "name": "Unreferenced unscoped voice",
                "created_at": _TIMESTAMP,
                "updated_at": _TIMESTAMP,
            },
        ],
    )
    return str(personal)


def _prepare_legacy_database(
    base_path: Path,
    path: Path,
    *,
    server_defaults: bool = True,
) -> str:
    shutil.copy2(base_path, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _create_legacy_annotation_tables(connection, server_defaults=server_defaults)
            personal_id = _seed_workspaces_and_voices(connection)
        return personal_id
    finally:
        engine.dispose()


def _insert_collection(
    connection: Connection,
    collection_id: str,
    name: str,
    *,
    description: str | None = None,
    revision: int = 1,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO voice_collections "
            "(id, name, description, revision, created_at, updated_at) "
            "VALUES (:id, :name, :description, :revision, :created_at, :updated_at)"
        ),
        {
            "id": collection_id,
            "name": name,
            "description": description,
            "revision": revision,
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
        },
    )


def _insert_member(
    connection: Connection,
    collection_id: str,
    voice_key: str,
    voice_ref_json: str,
    managed_voice_id: str | None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO voice_collection_members "
            "(collection_id, voice_key, voice_ref_json, managed_voice_id, created_at) "
            "VALUES (:collection_id, :voice_key, :voice_ref_json, :managed_voice_id, :created_at)"
        ),
        {
            "collection_id": collection_id,
            "voice_key": voice_key,
            "voice_ref_json": voice_ref_json,
            "managed_voice_id": managed_voice_id,
            "created_at": _TIMESTAMP,
        },
    )


def _insert_override(
    connection: Connection,
    voice_key: str,
    voice_ref_json: str,
    *,
    name: str = "Preserved name",
    profile_json: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO voice_catalog_overrides "
            "(voice_key, voice_ref_json, name, description, voice_category, profile_json, "
            "revision, created_at, updated_at) "
            "VALUES (:voice_key, :voice_ref_json, :name, :description, :voice_category, "
            ":profile_json, 4, :created_at, :updated_at)"
        ),
        {
            "voice_key": voice_key,
            "voice_ref_json": voice_ref_json,
            "name": name,
            "description": "Preserved description",
            "voice_category": "warm",
            "profile_json": profile_json,
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
        },
    )


def _snapshot_annotations(
    connection: Connection,
) -> tuple[dict[str, str], dict[str, list[tuple[Any, ...]]]]:
    schema: dict[str, str] = {}
    data: dict[str, list[tuple[Any, ...]]] = {}
    for table_name in TABLES:
        schema[table_name] = connection.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table_name},
        ).scalar_one()
        columns = [column["name"] for column in sa.inspect(connection).get_columns(table_name)]
        rows = connection.exec_driver_sql(f"SELECT * FROM {table_name} ORDER BY rowid").all()
        data[table_name] = [tuple(row) for row in rows]
        assert columns
    return schema, data


def _version(connection: Connection) -> str:
    return str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())


def _provider_key(service_id: str, model: str, voice: str) -> str:
    return ":".join(
        ("provider", quote(service_id, safe=""), quote(model, safe=""), quote(voice, safe=""))
    )


def test_migrates_genuine_history_without_rewriting_json_or_defaults(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "historical.sqlite"
    personal_id = _prepare_legacy_database(schema_at_0063, path, server_defaults=True)
    managed_json = (
        '{ "kind": "managed", "voice_id": " voice-archived-test ", '
        '"service_id": null, "model": "", "voice": " " }'
    )
    provider_json = (
        '{ "kind": "provider", "voice_id": "", "service_id": " service-x ", '
        '"model": " model-y ", "voice": " voice-z " }'
    )
    simple_override_json = (
        '{"kind":"provider","service_id":"service-override",'
        '"model":"model-override","voice":"voice-override"}'
    )
    long_service = "ü" * 160
    long_model = "☃" * 200
    long_voice = "😀" * 255
    long_json = json.dumps(
        {
            "kind": "provider",
            "service_id": long_service,
            "model": long_model,
            "voice": long_voice,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    long_key = _provider_key(long_service, long_model, long_voice)
    assert len(long_key) > 1024
    profile_json = '{ "languages": ["en"], "evidence": {"id": "artifact-x"} }'

    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_collection(connection, "collection-managed", "Managed", description="Keep me")
            _insert_collection(connection, "collection-provider", "Provider only")
            _insert_collection(connection, "collection-empty", "Empty")
            _insert_member(
                connection,
                "collection-managed",
                "managed:voice-archived-test",
                managed_json,
                ARCHIVED_VOICE,
            )
            _insert_member(
                connection,
                "collection-managed",
                _provider_key("service-x", "model-y", "voice-z"),
                provider_json,
                None,
            )
            _insert_member(
                connection,
                "collection-provider",
                _provider_key("service-only", "model-only", "voice-only"),
                '{"kind":"provider","service_id":"service-only",'
                '"model":"model-only","voice":"voice-only"}',
                None,
            )
            _insert_override(
                connection,
                "managed:voice-archived-test",
                managed_json,
                name="Managed override",
                profile_json=profile_json,
            )
            _insert_override(
                connection,
                _provider_key("service-override", "model-override", "voice-override"),
                simple_override_json,
            )
            _insert_override(connection, long_key, long_json, name="Long override")
            before_schema, before_data = _snapshot_annotations(connection)

        command.upgrade(_config(path), REVISION)

        with engine.connect() as connection:
            assert _version(connection) == REVISION
            after_schema, after_data = _snapshot_annotations(connection)
            assert after_schema.keys() == before_schema.keys()
            for table_name in TABLES:
                assert len(before_data[table_name]) == len(after_data[table_name])
                old_table_rows = connection.exec_driver_sql(
                    f"SELECT {', '.join(OLD_COLUMNS[table_name])} FROM {table_name} ORDER BY rowid"
                ).all()
                assert [tuple(row) for row in old_table_rows] == before_data[table_name]

            collection_scopes = {
                row[0]: row[1]
                for row in connection.execute(
                    sa.text("SELECT id, workspace_id FROM voice_collections")
                ).all()
            }
            assert collection_scopes == {
                "collection-managed": ARCHIVED_WORKSPACE,
                "collection-provider": personal_id,
                "collection-empty": personal_id,
            }
            member_scopes = {
                row[0]: row[1]
                for row in connection.execute(
                    sa.text(
                        "SELECT voice_key, workspace_id FROM voice_collection_members "
                        "WHERE collection_id = 'collection-managed'"
                    )
                ).all()
            }
            assert set(member_scopes.values()) == {ARCHIVED_WORKSPACE}
            override_scopes = {
                row[0]: row[1]
                for row in connection.execute(
                    sa.text("SELECT voice_key, workspace_id FROM voice_catalog_overrides")
                ).all()
            }
            assert override_scopes == {
                "managed:voice-archived-test": ARCHIVED_WORKSPACE,
                _provider_key("service-override", "model-override", "voice-override"): personal_id,
                long_key: personal_id,
            }
            long_override = connection.execute(
                sa.text(
                    "SELECT voice_key, voice_ref_json FROM voice_catalog_overrides "
                    "WHERE voice_key = :voice_key"
                ),
                {"voice_key": long_key},
            ).one()
            assert long_override == (long_key, long_json)
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

            inspector = sa.inspect(connection)
            collection_columns = {
                column["name"]: column for column in inspector.get_columns("voice_collections")
            }
            member_columns = {
                column["name"]: column
                for column in inspector.get_columns("voice_collection_members")
            }
            override_columns = {
                column["name"]: column
                for column in inspector.get_columns("voice_catalog_overrides")
            }
            assert collection_columns["workspace_id"]["nullable"] is True
            assert member_columns["workspace_id"]["nullable"] is True
            assert override_columns["workspace_id"]["nullable"] is False
            override_scope_type = override_columns["workspace_id"]["type"]
            assert isinstance(override_scope_type, sa.String)
            assert override_scope_type.length == 36
            assert inspector.get_pk_constraint("voice_catalog_overrides")[
                "constrained_columns"
            ] == ["workspace_id", "voice_key"]
            assert inspector.get_indexes("voice_catalog_overrides") == []
            assert inspector.get_unique_constraints("voice_collections")[0]["column_names"] == [
                "workspace_id",
                "name",
            ]
            collection_fk = inspector.get_foreign_keys("voice_collections")[0]
            override_fk = inspector.get_foreign_keys("voice_catalog_overrides")[0]
            assert collection_fk["name"] == "fk_voice_collections_workspace_id_workspaces"
            assert (collection_fk.get("options") or {}).get("ondelete") == "RESTRICT"
            assert override_fk["name"] == "fk_voice_catalog_overrides_workspace_id_workspaces"
            assert (override_fk.get("options") or {}).get("ondelete") == "RESTRICT"
            assert {
                column["name"]: column["default"]
                for column in inspector.get_columns("voice_collections")
            }["created_at"] == "CURRENT_TIMESTAMP"
            assert {
                column["name"]: column["default"]
                for column in inspector.get_columns("voice_catalog_overrides")
            }["revision"] == "1"

            # The same canonical provider key can now be represented for separate owners.
            connection.execute(
                sa.text(
                    "INSERT INTO voice_catalog_overrides "
                    "(workspace_id, voice_key, voice_ref_json, revision, created_at, updated_at) "
                    "VALUES (:workspace_id, :voice_key, :voice_ref_json, 1, :created_at, :updated_at)"
                ),
                {
                    "workspace_id": PERSONAL_WORKSPACE,
                    "voice_key": _provider_key(
                        "service-override", "model-override", "voice-override"
                    ),
                    "voice_ref_json": simple_override_json,
                    "created_at": _TIMESTAMP,
                    "updated_at": _TIMESTAMP,
                },
            )
            assert (
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM voice_catalog_overrides WHERE voice_key = :voice_key"
                    ),
                    {
                        "voice_key": _provider_key(
                            "service-override", "model-override", "voice-override"
                        )
                    },
                ).scalar_one()
                == 2
            )
            connection.execute(
                sa.text(
                    "INSERT INTO voice_collections "
                    "(id, workspace_id, name, revision, created_at, updated_at) "
                    "VALUES ('same-name-a', :owner_a, 'Same name', 1, :now, :now), "
                    "('same-name-b', :owner_b, 'Same name', 1, :now, :now)"
                ),
                {"owner_a": personal_id, "owner_b": PERSONAL_WORKSPACE, "now": _TIMESTAMP},
            )
    finally:
        engine.dispose()


def test_metadata_created_empty_target_shape_is_recognized_without_rebuild(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "empty-target.sqlite"
    shutil.copy2(schema_at_0063, path)
    engine = _connect(path)
    try:
        with engine.connect() as connection:
            before = {
                table: connection.execute(
                    sa.text("SELECT sql FROM sqlite_master WHERE name = :table"),
                    {"table": table},
                ).scalar_one()
                for table in TABLES
            }
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            after = {
                table: connection.execute(
                    sa.text("SELECT sql FROM sqlite_master WHERE name = :table"),
                    {"table": table},
                ).scalar_one()
                for table in TABLES
            }
            assert before == after
            assert _version(connection) == REVISION
    finally:
        engine.dispose()


def test_unrelated_indexes_survive_upgrade_and_downgrade(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "preserve-indexes.sqlite"
    _prepare_legacy_database(schema_at_0063, path)
    custom_indexes = {
        "voice_collections": ("ix_custom_collection_description", "description"),
        "voice_collection_members": ("ix_custom_member_created_at", "created_at"),
        "voice_catalog_overrides": ("ix_custom_override_name", "name"),
    }
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            for table_name, (index_name, column_name) in custom_indexes.items():
                connection.exec_driver_sql(
                    f"CREATE INDEX {index_name} ON {table_name} ({column_name})"
                )

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            for table_name, (index_name, column_name) in custom_indexes.items():
                index = next(
                    item
                    for item in sa.inspect(connection).get_indexes(table_name)
                    if item["name"] == index_name
                )
                assert index["column_names"] == [column_name]

        command.downgrade(_config(path), PREVIOUS_REVISION)
        with engine.connect() as connection:
            for table_name, (index_name, column_name) in custom_indexes.items():
                index = next(
                    item
                    for item in sa.inspect(connection).get_indexes(table_name)
                    if item["name"] == index_name
                )
                assert index["column_names"] == [column_name]
    finally:
        engine.dispose()


@pytest.mark.parametrize("malformation", ["partial_scope", "extra_column"])
def test_partial_or_malformed_legacy_shape_fails_without_ddl(
    schema_at_0063: Path, tmp_path: Path, malformation: str
) -> None:
    path = tmp_path / f"malformed-{malformation}.sqlite"
    _prepare_legacy_database(schema_at_0063, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            if malformation == "partial_scope":
                connection.exec_driver_sql(
                    "ALTER TABLE voice_collections ADD COLUMN workspace_id VARCHAR(36)"
                )
            else:
                connection.exec_driver_sql(
                    "ALTER TABLE voice_collection_members ADD COLUMN unexpected TEXT"
                )
        with engine.connect() as connection:
            before_schema, before_data = _snapshot_annotations(connection)
            before_version = _version(connection)
        with pytest.raises(RuntimeError, match="partial or malformed|only part"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _snapshot_annotations(connection) == (before_schema, before_data)
            assert _version(connection) == before_version == PREVIOUS_REVISION
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_collection",
        "missing_voice",
        "invalid_workspace",
        "wrong_key",
        "wrong_managed_id",
        "malformed_json",
        "non_string_field",
        "unknown_field",
        "mixed_owners",
    ],
)
def test_invalid_history_fails_before_any_ddl(
    schema_at_0063: Path, tmp_path: Path, failure: str
) -> None:
    path = tmp_path / f"invalid-{failure}.sqlite"
    _prepare_legacy_database(schema_at_0063, path)
    managed_ref = '{"kind":"managed","voice_id":"voice-archived-test"}'
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            if failure == "missing_collection":
                _insert_member(
                    connection,
                    "missing-collection",
                    "managed:voice-archived-test",
                    managed_ref,
                    ARCHIVED_VOICE,
                )
            else:
                _insert_collection(connection, "collection", "Collection")
                if failure == "missing_voice":
                    reference = '{"kind":"managed","voice_id":"missing-voice"}'
                    key = "managed:missing-voice"
                    managed_id = "missing-voice"
                elif failure == "wrong_key":
                    reference = managed_ref
                    key = "managed:wrong"
                    managed_id = ARCHIVED_VOICE
                elif failure == "wrong_managed_id":
                    reference = managed_ref
                    key = "managed:voice-archived-test"
                    managed_id = ACTIVE_VOICE
                elif failure == "malformed_json":
                    reference = "{broken"
                    key = "managed:voice-archived-test"
                    managed_id = ARCHIVED_VOICE
                elif failure == "non_string_field":
                    reference = '{"kind":"managed","voice_id":7}'
                    key = "managed:voice-archived-test"
                    managed_id = ARCHIVED_VOICE
                elif failure == "unknown_field":
                    reference = (
                        '{"kind":"managed","voice_id":"voice-archived-test",'
                        '"owner":"workspace-active-test"}'
                    )
                    key = "managed:voice-archived-test"
                    managed_id = ARCHIVED_VOICE
                else:
                    reference = managed_ref
                    key = "managed:voice-archived-test"
                    managed_id = ARCHIVED_VOICE
                _insert_member(connection, "collection", key, reference, managed_id)
                if failure == "invalid_workspace":
                    connection.execute(
                        sa.text(
                            "UPDATE voices SET workspace_id = 'missing-workspace' WHERE id = :id"
                        ),
                        {"id": ARCHIVED_VOICE},
                    )
                if failure == "mixed_owners":
                    _insert_member(
                        connection,
                        "collection",
                        "managed:voice-active-test",
                        '{"kind":"managed","voice_id":"voice-active-test"}',
                        ACTIVE_VOICE,
                    )

        with engine.connect() as connection:
            before_schema, before_data = _snapshot_annotations(connection)
            before_version = _version(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _snapshot_annotations(connection) == (before_schema, before_data)
            assert _version(connection) == before_version == PREVIOUS_REVISION
    finally:
        engine.dispose()


def test_invalid_initial_personal_workspace_rejects_fallback_before_ddl(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "invalid-fallback.sqlite"
    _prepare_legacy_database(schema_at_0063, path)
    provider_ref = '{"kind":"provider","service_id":"s","model":"m","voice":"v"}'
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_collection(connection, "provider-only", "Provider only")
            _insert_member(connection, "provider-only", "provider:s:m:v", provider_ref, None)
            connection.execute(
                sa.text(
                    "UPDATE workspaces SET state = 'archived' WHERE personal_owner_user_id = "
                    "(SELECT id FROM users WHERE legacy_subject = 'owner')"
                )
            )
        with engine.connect() as connection:
            before = _snapshot_annotations(connection)
            version = _version(connection)
        with pytest.raises(RuntimeError, match="seeded owner personal workspace"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _snapshot_annotations(connection) == before
            assert _version(connection) == version == PREVIOUS_REVISION
    finally:
        engine.dispose()


def test_populated_target_shape_is_rejected_atomically(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "populated-target.sqlite"
    shutil.copy2(schema_at_0063, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            personal_id = connection.execute(
                sa.text(
                    "SELECT workspaces.id FROM workspaces JOIN users "
                    "ON users.id = workspaces.personal_owner_user_id "
                    "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
                )
            ).scalar_one()
            connection.execute(
                sa.text(
                    "INSERT INTO voice_collections "
                    "(id, workspace_id, name, revision, created_at, updated_at) "
                    "VALUES ('already-there', :workspace_id, 'Already there', 1, :now, :now)"
                ),
                {"workspace_id": personal_id, "now": _TIMESTAMP},
            )
        with engine.connect() as connection:
            before_schema, before_data = _snapshot_annotations(connection)
            version = _version(connection)
        with pytest.raises(RuntimeError, match="target schema is already populated"):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _snapshot_annotations(connection) == (before_schema, before_data)
            assert _version(connection) == version == PREVIOUS_REVISION
    finally:
        engine.dispose()


def test_late_ddl_failure_rolls_back_and_retry_and_repeat_succeed(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "late-rollback.sqlite"
    _prepare_legacy_database(schema_at_0063, path)
    provider_ref = '{"kind":"provider","service_id":"s","model":"m","voice":"v"}'
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_collection(connection, "provider-only", "Provider only")
            _insert_override(connection, "provider:s:m:v", provider_ref)
        with engine.connect() as connection:
            before = _snapshot_annotations(connection)
            version = _version(connection)
            before_ddl = connection.exec_driver_sql(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).all()

        seen_override_backfill = False

        def fail_late(
            connection: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            nonlocal seen_override_backfill
            normalized_statement = " ".join(statement.lower().split())
            if "update voice_catalog_overrides set workspace_id" in normalized_statement:
                seen_override_backfill = True
            if (
                seen_override_backfill
                and "create table" in normalized_statement
                and "_alembic_tmp_voice_catalog_overrides" in normalized_statement
            ):
                raise RuntimeError("injected failure after annotation backfill")

        event.listen(Engine, "before_cursor_execute", fail_late)
        try:
            with pytest.raises(RuntimeError, match="injected failure"):
                command.upgrade(_config(path), REVISION)
        finally:
            event.remove(Engine, "before_cursor_execute", fail_late)

        with engine.connect() as connection:
            assert _snapshot_annotations(connection) == before
            assert _version(connection) == version == PREVIOUS_REVISION
            assert (
                connection.exec_driver_sql(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
                ).all()
                == before_ddl
            )

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            first_success = _snapshot_annotations(connection)
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            assert _snapshot_annotations(connection) == first_success
    finally:
        engine.dispose()


@pytest.mark.parametrize("collision", ["collection_name", "override_key"])
def test_downgrade_collision_refusal_is_before_ddl(
    schema_at_0063: Path, tmp_path: Path, collision: str
) -> None:
    path = tmp_path / f"downgrade-collision-{collision}.sqlite"
    _prepare_legacy_database(schema_at_0063, path)
    provider_json = '{"kind":"provider","service_id":"s","model":"m","voice":"v"}'
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            if collision == "collection_name":
                _insert_collection(connection, "original", "Duplicate")
            else:
                _insert_override(connection, "provider:s:m:v", provider_json)
        command.upgrade(_config(path), REVISION)
        with engine.begin() as connection:
            if collision == "collection_name":
                connection.execute(
                    sa.text(
                        "INSERT INTO voice_collections "
                        "(id, workspace_id, name, revision, created_at, updated_at) "
                        "VALUES ('collision', :workspace_id, 'Duplicate', 1, :now, :now)"
                    ),
                    {"workspace_id": PERSONAL_WORKSPACE, "now": _TIMESTAMP},
                )
            else:
                connection.execute(
                    sa.text(
                        "INSERT INTO voice_catalog_overrides "
                        "(workspace_id, voice_key, voice_ref_json, revision, created_at, updated_at) "
                        "VALUES (:workspace_id, 'provider:s:m:v', :voice_ref_json, 1, :now, :now)"
                    ),
                    {
                        "workspace_id": PERSONAL_WORKSPACE,
                        "voice_ref_json": provider_json,
                        "now": _TIMESTAMP,
                    },
                )
        with engine.connect() as connection:
            before = _snapshot_annotations(connection)
            version = _version(connection)
        with pytest.raises(RuntimeError, match="would collide"):
            command.downgrade(_config(path), PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert _snapshot_annotations(connection) == before
            assert _version(connection) == version == REVISION
    finally:
        engine.dispose()


def test_downgrade_without_collisions_restores_global_keys_and_preserves_rows(
    schema_at_0063: Path, tmp_path: Path
) -> None:
    path = tmp_path / "downgrade-safe.sqlite"
    _prepare_legacy_database(schema_at_0063, path, server_defaults=False)
    provider_json = '{"kind":"provider","service_id":"s","model":"m","voice":"v"}'
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _insert_collection(connection, "collection", "Keep me", description="description")
            _insert_override(connection, "provider:s:m:v", provider_json)
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            before_data = {
                table: connection.exec_driver_sql(
                    f"SELECT {', '.join(OLD_COLUMNS[table])} FROM {table} ORDER BY rowid"
                ).all()
                for table in TABLES
            }
        command.downgrade(_config(path), PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert _version(connection) == PREVIOUS_REVISION
            inspector = sa.inspect(connection)
            assert "workspace_id" not in {
                column["name"] for column in inspector.get_columns("voice_collections")
            }
            assert "workspace_id" not in {
                column["name"] for column in inspector.get_columns("voice_collection_members")
            }
            assert "workspace_id" not in {
                column["name"] for column in inspector.get_columns("voice_catalog_overrides")
            }
            assert inspector.get_pk_constraint("voice_catalog_overrides")[
                "constrained_columns"
            ] == ["voice_key"]
            assert inspector.get_unique_constraints("voice_collections")[0]["column_names"] == [
                "name"
            ]
            assert {
                column["name"]: column["default"]
                for column in inspector.get_columns("voice_collections")
            }["created_at"] is None
            for table, rows in before_data.items():
                after = connection.exec_driver_sql(
                    f"SELECT {', '.join(OLD_COLUMNS[table])} FROM {table} ORDER BY rowid"
                ).all()
                assert [tuple(row) for row in after] == [tuple(row) for row in rows]

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            assert (
                connection.execute(
                    sa.text("SELECT voice_key FROM voice_catalog_overrides")
                ).scalar_one()
                == "provider:s:m:v"
            )
    finally:
        engine.dispose()
