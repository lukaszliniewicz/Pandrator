from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pandrator.web.accounts.models import Workspace
from pandrator.web.database import create_database_engine, sqlite_url
from pandrator.web.models import TrainingRun, Voice, VoiceSample
from pandrator.web.voice_identity import (
    training_workspace,
    voice_creation_workspace,
    voice_sample_workspace,
    voice_workspace,
)

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
VOICE_REVISION = "0063_voice_workspace"
SOUND_REVISION = "0062_sound_workspace"
SCRATCH_ROOT = Path("/tmp/pandrator-voice-schema-worker")
FIXED_TIME = "2026-10-23 12:00:00.000000"
ACTIVE_A = "workspace-active-a"
ACTIVE_B = "workspace-active-b"
ARCHIVED = "workspace-archived"
WORKSPACE_FKS = {
    "voices": "fk_voices_workspace_id_workspaces",
    "voice_samples": "fk_voice_samples_workspace_id_workspaces",
    "training_runs": "fk_training_runs_workspace_id_workspaces",
}
WORKSPACE_INDEXES = {
    "voices": {"ix_voices_workspace_id": ("workspace_id",)},
    "voice_samples": {"ix_voice_samples_workspace_id": ("workspace_id",)},
    "training_runs": {
        "ix_training_runs_workspace_id": ("workspace_id",),
        "ix_training_runs_workspace_created_at_id": (
            "workspace_id",
            "created_at",
            "id",
        ),
    },
}
LEGACY_COLUMNS = {
    "voices": (
        "id",
        "name",
        "language",
        "description",
        "rvc_model_ref",
        "metadata_json",
        "revision",
        "created_at",
        "updated_at",
    ),
    "voice_samples": (
        "id",
        "voice_id",
        "artifact_id",
        "transcript",
        "transcript_language",
        "transcript_reviewed",
        "created_at",
    ),
    "training_runs": (
        "id",
        "kind",
        "voice_id",
        "job_id",
        "source_artifact_id",
        "source_text_artifact_id",
        "output_artifact_id",
        "model_name",
        "status",
        "settings_json",
        "error_message",
        "created_at",
        "updated_at",
    ),
}


@pytest.fixture
def database_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_ROOT / f"{request.node.name}-{uuid.uuid4().hex}.sqlite"
    yield path
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _initialize_to_0062(path: Path) -> None:
    command.upgrade(_config(path), SOUND_REVISION)


def _has_column(connection: sa.Connection, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspect(connection).get_columns(table_name)}


def _drop_scope_from_table(connection: sa.Connection, table_name: str) -> None:
    operations = Operations(MigrationContext.configure(connection, opts={"render_as_batch": True}))
    with operations.batch_alter_table(table_name) as batch:
        for index_name in WORKSPACE_INDEXES[table_name]:
            batch.drop_index(index_name)
        batch.drop_constraint(WORKSPACE_FKS[table_name], type_="foreignkey")
        batch.drop_column("workspace_id")
        if table_name == "voices":
            batch.drop_constraint("uq_voices_workspace_name", type_="unique")
            batch.create_unique_constraint("uq_voices_name", ["name"])


def _recreate_old_0062_voice_shape(path: Path, *, unnamed_voice_unique: bool = False) -> None:
    engine = create_database_engine(path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        with connection.begin():
            for table_name in ("training_runs", "voice_samples", "voices"):
                _drop_scope_from_table(connection, table_name)
            if unnamed_voice_unique:
                # The original Voice.name unique constraint had no name. Build
                # that genuine legacy shape instead of only a named equivalent.
                voices = sa.Table("voices", sa.MetaData(), autoload_with=connection)
                for constraint in voices.constraints:
                    if isinstance(constraint, sa.UniqueConstraint):
                        constraint.name = None
                operations = Operations(MigrationContext.configure(connection))
                with operations.batch_alter_table("voices", recreate="always", copy_from=voices):
                    pass
                assert inspect(connection).get_unique_constraints("voices") == [
                    {"name": None, "column_names": ["name"]}
                ]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()


def _initialize_old_0062(path: Path, *, unnamed_voice_unique: bool = False) -> None:
    _initialize_to_0062(path)
    _recreate_old_0062_voice_shape(path, unnamed_voice_unique=unnamed_voice_unique)


def _seed_workspaces_and_artifacts(
    path: Path,
    artifact_owners: Mapping[str, str | None] | None = None,
    *,
    artifact_states: Mapping[str, str] | None = None,
    artifact_metadata: Mapping[str, str] | None = None,
) -> str:
    engine = create_database_engine(path)
    try:
        with engine.begin() as connection:
            owner_id = connection.execute(
                text("SELECT id FROM users WHERE legacy_subject = 'owner'")
            ).scalar_one()
            personal_id = connection.execute(
                text(
                    "SELECT id FROM workspaces "
                    "WHERE kind = 'personal' AND personal_owner_user_id = :owner_id"
                ),
                {"owner_id": owner_id},
            ).scalar_one()
            for workspace_id, name, state in (
                (ACTIVE_A, "Active A", "active"),
                (ACTIVE_B, "Active B", "active"),
                (ARCHIVED, "Archived", "archived"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO workspaces "
                        "(id, kind, name, personal_owner_user_id, state, revision, "
                        "created_at, updated_at) "
                        "VALUES (:id, 'team', :name, NULL, :state, 1, :created, :updated)"
                    ),
                    {
                        "id": workspace_id,
                        "name": name,
                        "state": state,
                        "created": FIXED_TIME,
                        "updated": FIXED_TIME,
                    },
                )
            for artifact_id, workspace_id in (artifact_owners or {}).items():
                _insert_artifact(
                    connection,
                    artifact_id,
                    workspace_id,
                    state=(artifact_states or {}).get(artifact_id, "current"),
                    metadata_json=(artifact_metadata or {}).get(artifact_id, "{}"),
                )
    finally:
        engine.dispose()
    return str(personal_id)


def _insert_artifact(
    connection: sa.Connection,
    artifact_id: str,
    workspace_id: str | None,
    *,
    state: str = "current",
    metadata_json: str = "{}",
) -> None:
    connection.execute(
        text(
            "INSERT INTO artifacts "
            "(id, workspace_id, project_id, session_id, kind, role, relative_path, "
            "mime_type, size_bytes, content_hash, state, settings_hash, metadata_json, "
            "created_at, updated_at) "
            "VALUES (:id, :workspace_id, NULL, NULL, 'audio', 'voice', :path, "
            "'audio/wav', 32, :hash, :state, NULL, :metadata, :created, :updated)"
        ),
        {
            "id": artifact_id,
            "workspace_id": workspace_id,
            "path": f"voice/{artifact_id}.wav",
            "hash": f"hash-{artifact_id}",
            "state": state,
            "metadata": metadata_json,
            "created": FIXED_TIME,
            "updated": FIXED_TIME,
        },
    )


def _insert_voice(
    connection: sa.Connection,
    voice_id: str,
    name: str,
    *,
    workspace_id: str | None = None,
    metadata_json: str = "{}",
) -> None:
    columns = [
        "id",
        "name",
        "language",
        "description",
        "rvc_model_ref",
        "metadata_json",
        "revision",
        "created_at",
        "updated_at",
    ]
    values: dict[str, Any] = {
        "id": voice_id,
        "name": name,
        "language": None,
        "description": None,
        "rvc_model_ref": None,
        "metadata_json": metadata_json,
        "revision": 3,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    if _has_column(connection, "voices", "workspace_id"):
        columns.insert(1, "workspace_id")
        values["workspace_id"] = workspace_id
    column_sql = ", ".join(columns)
    value_sql = ", ".join(f":{column}" for column in columns)
    connection.execute(text(f"INSERT INTO voices ({column_sql}) VALUES ({value_sql})"), values)


def _insert_sample(
    connection: sa.Connection,
    sample_id: str,
    voice_id: str,
    artifact_id: str | None,
    *,
    workspace_id: str | None = None,
) -> None:
    columns = [
        "id",
        "voice_id",
        "artifact_id",
        "transcript",
        "transcript_language",
        "transcript_reviewed",
        "created_at",
    ]
    values: dict[str, Any] = {
        "id": sample_id,
        "voice_id": voice_id,
        "artifact_id": artifact_id,
        "transcript": "retained transcript",
        "transcript_language": "en",
        "transcript_reviewed": 1,
        "created_at": FIXED_TIME,
    }
    if _has_column(connection, "voice_samples", "workspace_id"):
        columns.insert(1, "workspace_id")
        values["workspace_id"] = workspace_id
    column_sql = ", ".join(columns)
    value_sql = ", ".join(f":{column}" for column in columns)
    connection.execute(
        text(f"INSERT INTO voice_samples ({column_sql}) VALUES ({value_sql})"), values
    )


def _insert_training(
    connection: sa.Connection,
    training_id: str,
    *,
    voice_id: str | None = None,
    source_artifact_id: str | None = None,
    source_text_artifact_id: str | None = None,
    output_artifact_id: str | None = None,
    workspace_id: str | None = None,
    status: str = "failed",
    settings_json: str = "{}",
) -> None:
    columns = [
        "id",
        "kind",
        "voice_id",
        "job_id",
        "source_artifact_id",
        "source_text_artifact_id",
        "output_artifact_id",
        "model_name",
        "status",
        "settings_json",
        "error_message",
        "created_at",
        "updated_at",
    ]
    values: dict[str, Any] = {
        "id": training_id,
        "kind": "xtts",
        "voice_id": voice_id,
        "job_id": None,
        "source_artifact_id": source_artifact_id,
        "source_text_artifact_id": source_text_artifact_id,
        "output_artifact_id": output_artifact_id,
        "model_name": "retained-model",
        "status": status,
        "settings_json": settings_json,
        "error_message": "retained error",
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    if _has_column(connection, "training_runs", "workspace_id"):
        columns.insert(1, "workspace_id")
        values["workspace_id"] = workspace_id
    column_sql = ", ".join(columns)
    value_sql = ", ".join(f":{column}" for column in columns)
    connection.execute(
        text(f"INSERT INTO training_runs ({column_sql}) VALUES ({value_sql})"), values
    )


def _legacy_rows(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    engine = create_database_engine(path)
    try:
        with engine.connect() as connection:
            return {
                table_name: [
                    tuple(row)
                    for row in connection.execute(
                        text(f"SELECT {', '.join(columns)} FROM {table_name} ORDER BY id")
                    )
                ]
                for table_name, columns in LEGACY_COLUMNS.items()
            }
    finally:
        engine.dispose()


def _ownership_rows(connection: sa.Connection, table_name: str) -> dict[str, str | None]:
    return {
        str(row["id"]): row["workspace_id"]
        for row in connection.execute(text(f"SELECT id, workspace_id FROM {table_name}")).mappings()
    }


def _snapshot(path: Path) -> tuple[Any, ...]:
    engine = create_database_engine(path)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            schema = []
            data = []
            for table_name in WORKSPACE_FKS:
                columns = tuple(
                    (
                        column["name"],
                        str(column["type"]),
                        column["nullable"],
                        column.get("default"),
                        column.get("primary_key"),
                    )
                    for column in inspector.get_columns(table_name)
                )
                foreign_keys = tuple(
                    sorted(
                        [
                            (
                                foreign_key.get("name"),
                                tuple(foreign_key["constrained_columns"]),
                                foreign_key["referred_table"],
                                tuple(foreign_key["referred_columns"]),
                                (foreign_key.get("options") or {}).get("ondelete"),
                            )
                            for foreign_key in inspector.get_foreign_keys(table_name)
                        ],
                        key=repr,
                    )
                )
                indexes = tuple(
                    sorted(
                        [
                            (
                                index.get("name"),
                                tuple(index["column_names"] or ()),
                                bool(index.get("unique")),
                            )
                            for index in inspector.get_indexes(table_name)
                        ],
                        key=repr,
                    )
                )
                unique_constraints = tuple(
                    sorted(
                        [
                            (
                                constraint.get("name"),
                                tuple(constraint["column_names"] or ()),
                            )
                            for constraint in inspector.get_unique_constraints(table_name)
                        ],
                        key=repr,
                    )
                )
                schema.append((table_name, columns, foreign_keys, indexes, unique_constraints))
                data.append(
                    (
                        table_name,
                        tuple(
                            tuple(row)
                            for row in connection.execute(
                                text(f"SELECT * FROM {table_name} ORDER BY id")
                            )
                        ),
                    )
                )
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            ddl = tuple(
                connection.execute(
                    text(
                        "SELECT type, name, sql FROM sqlite_master "
                        "WHERE tbl_name IN ('voices', 'voice_samples', 'training_runs') "
                        "ORDER BY type, name"
                    )
                ).all()
            )
            return version, tuple(schema), tuple(data), ddl
    finally:
        engine.dispose()


def _upgrade_voice(path: Path) -> None:
    command.upgrade(_config(path), VOICE_REVISION)


def _insert_orphan_row(path: Path, sql: str, values: tuple[Any, ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(sql, values)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("unnamed_voice_unique", [False, True], ids=["named", "unnamed"])
def test_old_0062_group_adoption_preserves_history_and_ignores_metadata(
    database_path: Path,
    unnamed_voice_unique: bool,
) -> None:
    _initialize_old_0062(database_path, unnamed_voice_unique=unnamed_voice_unique)
    personal_id = _seed_workspaces_and_artifacts(
        database_path,
        {
            "artifact-sample-a": ACTIVE_A,
            "artifact-training-a": ACTIVE_A,
            "artifact-sample-archived": ARCHIVED,
            "artifact-run-b": ACTIVE_B,
            "artifact-unlinked-b": ACTIVE_B,
        },
        artifact_states={"artifact-sample-a": "deleted", "artifact-training-a": "deleted"},
    )
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_voice(
                connection,
                "voice-a",
                "Narrator",
                metadata_json=json.dumps({"workspace_id": ACTIVE_B}),
            )
            _insert_voice(connection, "voice-archived", "Archived Narrator")
            _insert_voice(
                connection,
                "voice-unbound",
                "Unbound Narrator",
                metadata_json=json.dumps({"workspace_id": ACTIVE_B}),
            )
            _insert_sample(connection, "sample-a", "voice-a", "artifact-sample-a")
            _insert_sample(
                connection,
                "sample-archived",
                "voice-archived",
                "artifact-sample-archived",
            )
            _insert_training(
                connection,
                "training-a",
                voice_id="voice-a",
                source_artifact_id="artifact-training-a",
                status="completed",
            )
            _insert_training(
                connection,
                "training-a-retained",
                voice_id="voice-a",
                status="cancelled",
            )
            _insert_training(
                connection,
                "training-unlinked-b",
                source_artifact_id="artifact-run-b",
                status="failed",
            )
            _insert_training(
                connection,
                "training-unbound",
                settings_json=json.dumps(
                    {"workspace_id": ACTIVE_B, "artifact_id": "artifact-unlinked-b"}
                ),
                status="queued",
            )
    finally:
        engine.dispose()

    before_rows = _legacy_rows(database_path)
    _upgrade_voice(database_path)
    after_rows = _legacy_rows(database_path)
    assert after_rows == before_rows

    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            voice_owners = _ownership_rows(connection, "voices")
            sample_owners = _ownership_rows(connection, "voice_samples")
            training_owners = _ownership_rows(connection, "training_runs")
            assert voice_owners == {
                "voice-a": ACTIVE_A,
                "voice-archived": ARCHIVED,
                "voice-unbound": personal_id,
            }
            assert sample_owners == {
                "sample-a": ACTIVE_A,
                "sample-archived": ARCHIVED,
            }
            assert training_owners == {
                "training-a": ACTIVE_A,
                "training-a-retained": ACTIVE_A,
                "training-unlinked-b": ACTIVE_B,
                "training-unbound": personal_id,
            }
            assert connection.execute(
                text("SELECT status, error_message FROM training_runs WHERE id = 'training-a'")
            ).one() == ("completed", "retained error")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []

    finally:
        engine.dispose()

    # Workspace/name uniqueness is exact-case and allows identical names in other owners.
    engine = create_database_engine(database_path)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert_voice(connection, "same-name-a", "Narrator", workspace_id=ACTIVE_A)
        with engine.begin() as connection:
            _insert_voice(connection, "same-name-b", "Narrator", workspace_id=ACTIVE_B)
            _insert_voice(connection, "lower-name-a", "narrator", workspace_id=ACTIVE_A)
    finally:
        engine.dispose()

    after_first_upgrade = _snapshot(database_path)
    _upgrade_voice(database_path)
    assert _snapshot(database_path) == after_first_upgrade


def test_complete_empty_initial_metadata_scope_is_adopted_and_repeat_is_noop(
    database_path: Path,
) -> None:
    _initialize_to_0062(database_path)
    personal_id = _seed_workspaces_and_artifacts(
        database_path,
        {"artifact-target": ACTIVE_A, "artifact-unlinked": ACTIVE_B},
    )
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            for table_name in WORKSPACE_FKS:
                assert _has_column(connection, table_name, "workspace_id")
            assert inspect(connection).get_unique_constraints("voices") == [
                {
                    "name": "uq_voices_workspace_name",
                    "column_names": ["workspace_id", "name"],
                }
            ]
            _insert_voice(
                connection,
                "target-backed",
                "Target Backed",
                metadata_json=json.dumps({"workspace_id": ACTIVE_B}),
            )
            _insert_voice(
                connection,
                "target-unbound",
                "Target Unbound",
                metadata_json=json.dumps({"workspace_id": ACTIVE_B}),
            )
            _insert_sample(connection, "target-sample", "target-backed", "artifact-target")
            _insert_training(
                connection,
                "target-training",
                voice_id="target-backed",
                source_artifact_id="artifact-target",
            )
            _insert_training(
                connection,
                "target-unbound-run",
                settings_json=json.dumps({"artifact_id": "artifact-unlinked"}),
            )
    finally:
        engine.dispose()

    _upgrade_voice(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            assert _ownership_rows(connection, "voices") == {
                "target-backed": ACTIVE_A,
                "target-unbound": personal_id,
            }
            assert _ownership_rows(connection, "voice_samples") == {"target-sample": ACTIVE_A}
            assert _ownership_rows(connection, "training_runs") == {
                "target-training": ACTIVE_A,
                "target-unbound-run": personal_id,
            }
    finally:
        engine.dispose()
    completed_snapshot = _snapshot(database_path)
    _upgrade_voice(database_path)
    assert _snapshot(database_path) == completed_snapshot


@pytest.mark.parametrize(
    "broken_case",
    (
        "missing-sample-voice",
        "missing-sample-artifact",
        "missing-training-voice",
        "missing-training-artifact",
        "missing-artifact-workspace",
        "conflicting-artifacts",
    ),
)
def test_invalid_old_scope_plan_fails_before_ddl(
    database_path: Path,
    broken_case: str,
) -> None:
    _initialize_old_0062(database_path)
    owners: dict[str, str | None] = {"artifact-a": ACTIVE_A, "artifact-b": ACTIVE_B}
    if broken_case == "missing-artifact-workspace":
        owners["artifact-ownerless"] = None
    _seed_workspaces_and_artifacts(database_path, owners)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            if broken_case != "missing-sample-voice":
                _insert_voice(connection, "voice-a", "Voice A")
            if broken_case == "missing-artifact-workspace":
                _insert_sample(connection, "sample", "voice-a", "artifact-ownerless")
            elif broken_case == "conflicting-artifacts":
                _insert_sample(connection, "sample", "voice-a", "artifact-a")
                _insert_training(
                    connection,
                    "training",
                    voice_id="voice-a",
                    source_artifact_id="artifact-b",
                )
    finally:
        engine.dispose()

    if broken_case == "missing-sample-voice":
        _insert_orphan_row(
            database_path,
            "INSERT INTO voice_samples "
            "(id, voice_id, artifact_id, transcript, transcript_language, "
            "transcript_reviewed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sample", "voice-missing", "artifact-a", "text", "en", 1, FIXED_TIME),
        )
    elif broken_case == "missing-sample-artifact":
        _insert_orphan_row(
            database_path,
            "INSERT INTO voice_samples "
            "(id, voice_id, artifact_id, transcript, transcript_language, "
            "transcript_reviewed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sample", "voice-a", "artifact-missing", "text", "en", 1, FIXED_TIME),
        )
    elif broken_case == "missing-training-voice":
        _insert_orphan_row(
            database_path,
            "INSERT INTO training_runs "
            "(id, kind, voice_id, job_id, source_artifact_id, "
            "source_text_artifact_id, output_artifact_id, model_name, status, "
            "settings_json, error_message, created_at, updated_at) "
            "VALUES (?, 'xtts', ?, NULL, NULL, NULL, NULL, 'model', 'failed', '{}', NULL, ?, ?)",
            ("training", "voice-missing", FIXED_TIME, FIXED_TIME),
        )
    elif broken_case == "missing-training-artifact":
        _insert_orphan_row(
            database_path,
            "INSERT INTO training_runs "
            "(id, kind, voice_id, job_id, source_artifact_id, "
            "source_text_artifact_id, output_artifact_id, model_name, status, "
            "settings_json, error_message, created_at, updated_at) "
            "VALUES (?, 'xtts', NULL, NULL, ?, NULL, NULL, 'model', 'failed', '{}', NULL, ?, ?)",
            ("training", "artifact-missing", FIXED_TIME, FIXED_TIME),
        )

    before = _snapshot(database_path)
    with pytest.raises(RuntimeError):
        _upgrade_voice(database_path)
    assert _snapshot(database_path) == before


def test_partial_target_scope_is_rejected_without_changes(database_path: Path) -> None:
    _initialize_to_0062(database_path)
    engine = create_database_engine(database_path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        with connection.begin():
            _drop_scope_from_table(connection, "training_runs")
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()

    before = _snapshot(database_path)
    with pytest.raises(RuntimeError, match="only part"):
        _upgrade_voice(database_path)
    assert _snapshot(database_path) == before


@pytest.mark.parametrize("malformation", ("missing-index", "extra-global-unique"))
def test_malformed_target_scope_is_rejected_without_changes(
    database_path: Path,
    malformation: str,
) -> None:
    _initialize_to_0062(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            if malformation == "missing-index":
                connection.exec_driver_sql("DROP INDEX ix_training_runs_workspace_created_at_id")
            else:
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX ix_voices_global_name ON voices (name)"
                )
    finally:
        engine.dispose()

    before = _snapshot(database_path)
    with pytest.raises(RuntimeError):
        _upgrade_voice(database_path)
    assert _snapshot(database_path) == before


def test_populated_early_scope_is_rejected_without_overwriting(database_path: Path) -> None:
    _initialize_to_0062(database_path)
    _seed_workspaces_and_artifacts(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_voice(connection, "already-scoped", "Already Scoped", workspace_id=ACTIVE_A)
    finally:
        engine.dispose()

    before = _snapshot(database_path)
    with pytest.raises(RuntimeError, match="preexisting scope data"):
        _upgrade_voice(database_path)
    assert _snapshot(database_path) == before


def test_late_failure_rolls_back_exactly_and_retry_succeeds(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize_old_0062(database_path)
    _seed_workspaces_and_artifacts(database_path, {"artifact-a": ACTIVE_A})
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_voice(connection, "voice-a", "Voice A")
            _insert_sample(connection, "sample-a", "voice-a", "artifact-a")
    finally:
        engine.dispose()

    before = _snapshot(database_path)
    execute = Connection.execute

    def fail_after_voice_update(
        connection: Connection,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if str(statement).startswith("UPDATE voice_samples"):
            raise RuntimeError("injected late migration failure")
        return execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", fail_after_voice_update)
    with pytest.raises(RuntimeError, match="injected late"):
        _upgrade_voice(database_path)
    assert _snapshot(database_path) == before

    monkeypatch.setattr(Connection, "execute", execute)
    _upgrade_voice(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT workspace_id FROM voices WHERE id = 'voice-a'")
                ).scalar_one()
                == ACTIVE_A
            )
            assert (
                connection.execute(
                    text("SELECT workspace_id FROM voice_samples WHERE id = 'sample-a'")
                ).scalar_one()
                == ACTIVE_A
            )
    finally:
        engine.dispose()


def test_downgrade_collision_refuses_before_schema_or_data_changes(
    database_path: Path,
) -> None:
    _initialize_to_0062(database_path)
    _upgrade_voice(database_path)
    _seed_workspaces_and_artifacts(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_voice(connection, "voice-a", "Same Name", workspace_id=ACTIVE_A)
            _insert_voice(connection, "voice-b", "Same Name", workspace_id=ACTIVE_B)
    finally:
        engine.dispose()

    before = _snapshot(database_path)
    with pytest.raises(RuntimeError, match="names would collide"):
        command.downgrade(_config(database_path), SOUND_REVISION)
    assert _snapshot(database_path) == before


def test_downgrade_restores_global_uniqueness_and_preserves_case(database_path: Path) -> None:
    _initialize_to_0062(database_path)
    _upgrade_voice(database_path)
    _seed_workspaces_and_artifacts(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.begin() as connection:
            _insert_voice(connection, "voice-upper", "Echo", workspace_id=ACTIVE_A)
            _insert_voice(connection, "voice-lower", "echo", workspace_id=ACTIVE_B)
    finally:
        engine.dispose()

    before_rows = _legacy_rows(database_path)
    command.downgrade(_config(database_path), SOUND_REVISION)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            for table_name in WORKSPACE_FKS:
                assert not _has_column(connection, table_name, "workspace_id")
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == SOUND_REVISION
            )
            constraints = inspect(connection).get_unique_constraints("voices")
            assert len(constraints) == 1
            assert constraints[0]["column_names"] == ["name"]
    finally:
        engine.dispose()
    assert _legacy_rows(database_path) == before_rows

    # The downgraded legacy shape is still a valid 0062 input for a later retry.
    _upgrade_voice(database_path)
    engine = create_database_engine(database_path)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT workspace_id FROM voices WHERE id = 'voice-upper'")
                ).scalar_one()
                is not None
            )
            assert (
                connection.execute(
                    text("SELECT workspace_id FROM voices WHERE id = 'voice-lower'")
                ).scalar_one()
                is not None
            )
    finally:
        engine.dispose()


def _insert_current_helper_records(path: Path) -> str:
    personal_id = _seed_workspaces_and_artifacts(
        path,
        {
            "helper-artifact-a": ACTIVE_A,
            "helper-artifact-b": ACTIVE_B,
            "helper-artifact-archived": ARCHIVED,
        },
        artifact_states={"helper-artifact-a": "deleted"},
    )
    engine = create_database_engine(path)
    try:
        with engine.begin() as connection:
            _insert_voice(connection, "helper-voice-a", "Helper A", workspace_id=ACTIVE_A)
            _insert_voice(connection, "helper-voice-b", "Helper B", workspace_id=ACTIVE_B)
            _insert_voice(
                connection,
                "helper-voice-archived",
                "Helper Archived",
                workspace_id=ARCHIVED,
            )
            _insert_voice(connection, "helper-voice-null", "Helper Null")
            _insert_sample(
                connection,
                "helper-sample-good",
                "helper-voice-a",
                "helper-artifact-a",
                workspace_id=ACTIVE_A,
            )
            _insert_sample(
                connection,
                "helper-sample-voice-mismatch",
                "helper-voice-a",
                "helper-artifact-b",
                workspace_id=ACTIVE_B,
            )
            _insert_sample(
                connection,
                "helper-sample-artifact-mismatch",
                "helper-voice-a",
                "helper-artifact-b",
                workspace_id=ACTIVE_A,
            )
            _insert_sample(
                connection,
                "helper-sample-archived",
                "helper-voice-archived",
                "helper-artifact-archived",
                workspace_id=ARCHIVED,
            )
            _insert_sample(
                connection,
                "helper-sample-null",
                "helper-voice-a",
                "helper-artifact-a",
            )
            _insert_training(
                connection,
                "helper-training-good",
                voice_id="helper-voice-a",
                source_artifact_id="helper-artifact-a",
                workspace_id=ACTIVE_A,
            )
            _insert_training(
                connection,
                "helper-training-retained",
                workspace_id=ACTIVE_B,
            )
            _insert_training(
                connection,
                "helper-training-voice-mismatch",
                voice_id="helper-voice-a",
                workspace_id=ACTIVE_B,
            )
            _insert_training(
                connection,
                "helper-training-artifact-mismatch",
                voice_id="helper-voice-a",
                source_artifact_id="helper-artifact-b",
                workspace_id=ACTIVE_A,
            )
            _insert_training(
                connection,
                "helper-training-archived",
                workspace_id=ARCHIVED,
            )
            _insert_training(
                connection,
                "helper-training-null",
                voice_id="helper-voice-a",
                source_artifact_id="helper-artifact-a",
            )
    finally:
        engine.dispose()
    return personal_id


def test_identity_helpers_validate_owners_links_and_never_autoflush(
    database_path: Path,
) -> None:
    _initialize_to_0062(database_path)
    _upgrade_voice(database_path)
    personal_id = _insert_current_helper_records(database_path)
    engine = create_database_engine(database_path)
    session = Session(engine, expire_on_commit=False)
    try:
        active_a = session.get(Workspace, ACTIVE_A)
        assert active_a is not None
        active_b = session.get(Workspace, ACTIVE_B)
        assert active_b is not None
        assert voice_creation_workspace(session).id == personal_id
        assert voice_creation_workspace(session, workspace_id=active_a.id).id == ACTIVE_A
        with pytest.raises(ValueError):
            voice_creation_workspace(session, workspace_id="workspace-missing")
        with pytest.raises(ValueError, match="inactive"):
            voice_creation_workspace(session, workspace_id=ARCHIVED)

        pending = Voice(
            id="pending-unflushed-voice",
            workspace_id="workspace-missing",
            name="Pending",
        )
        session.add(pending)
        assert voice_creation_workspace(session).id == personal_id
        assert pending in session.new
        session.expunge(pending)

        voice_a = session.get(Voice, "helper-voice-a")
        voice_b = session.get(Voice, "helper-voice-b")
        voice_archived = session.get(Voice, "helper-voice-archived")
        voice_null = session.get(Voice, "helper-voice-null")
        assert voice_a is not None and voice_b is not None
        assert voice_archived is not None and voice_null is not None
        assert voice_workspace(session, voice_a).id == ACTIVE_A
        assert voice_workspace(session, voice_a, workspace_id=ACTIVE_A).id == ACTIVE_A
        with pytest.raises(ValueError, match="not requested"):
            voice_workspace(session, voice_a, workspace_id=ACTIVE_B)
        with pytest.raises(ValueError, match="no stored workspace"):
            voice_workspace(session, voice_null)
        with pytest.raises(ValueError, match="inactive"):
            voice_workspace(session, voice_archived)
        voice_a.workspace_id = "workspace-missing"
        with pytest.raises(ValueError, match="missing workspace"):
            voice_workspace(session, voice_a)
        voice_a.workspace_id = ACTIVE_A

        sample_good = session.get(VoiceSample, "helper-sample-good")
        sample_voice_mismatch = session.get(VoiceSample, "helper-sample-voice-mismatch")
        sample_artifact_mismatch = session.get(VoiceSample, "helper-sample-artifact-mismatch")
        sample_archived = session.get(VoiceSample, "helper-sample-archived")
        sample_null = session.get(VoiceSample, "helper-sample-null")
        assert sample_good is not None
        assert sample_voice_mismatch is not None
        assert sample_artifact_mismatch is not None
        assert sample_archived is not None and sample_null is not None
        assert voice_sample_workspace(session, sample_good).id == ACTIVE_A
        with pytest.raises(ValueError, match="not requested"):
            voice_sample_workspace(session, sample_good, workspace_id=ACTIVE_B)
        with pytest.raises(ValueError, match="belongs to workspace"):
            voice_sample_workspace(session, sample_voice_mismatch)
        with pytest.raises(ValueError, match="different workspace"):
            voice_sample_workspace(session, sample_artifact_mismatch)
        with pytest.raises(ValueError, match="inactive"):
            voice_sample_workspace(session, sample_archived)
        with pytest.raises(ValueError, match="no stored workspace"):
            voice_sample_workspace(session, sample_null)
        sample_good.voice_id = "missing-voice"
        with pytest.raises(ValueError, match="missing voice"):
            voice_sample_workspace(session, sample_good)
        sample_good.voice_id = "helper-voice-a"
        sample_good.artifact_id = "missing-artifact"
        with pytest.raises(ValueError, match="missing artifact"):
            voice_sample_workspace(session, sample_good)
        sample_good.artifact_id = "helper-artifact-a"

        training_good = session.get(TrainingRun, "helper-training-good")
        training_retained = session.get(TrainingRun, "helper-training-retained")
        training_voice_mismatch = session.get(TrainingRun, "helper-training-voice-mismatch")
        training_artifact_mismatch = session.get(TrainingRun, "helper-training-artifact-mismatch")
        training_archived = session.get(TrainingRun, "helper-training-archived")
        training_null = session.get(TrainingRun, "helper-training-null")
        assert training_good is not None and training_retained is not None
        assert training_voice_mismatch is not None
        assert training_artifact_mismatch is not None
        assert training_archived is not None and training_null is not None
        assert training_workspace(session, training_good).id == ACTIVE_A
        assert training_workspace(session, training_retained).id == ACTIVE_B
        with pytest.raises(ValueError, match="belongs to workspace"):
            training_workspace(session, training_voice_mismatch)
        with pytest.raises(ValueError, match="different workspace"):
            training_workspace(session, training_artifact_mismatch)
        with pytest.raises(ValueError, match="inactive"):
            training_workspace(session, training_archived)
        with pytest.raises(ValueError, match="no stored workspace"):
            training_workspace(session, training_null)
        training_good.voice_id = "missing-voice"
        with pytest.raises(ValueError, match="missing voice"):
            training_workspace(session, training_good)
        training_good.voice_id = "helper-voice-a"
        training_good.source_artifact_id = "missing-artifact"
        with pytest.raises(ValueError, match="missing source artifact"):
            training_workspace(session, training_good)
    finally:
        session.rollback()
        session.close()
        engine.dispose()
