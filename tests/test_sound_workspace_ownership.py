from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, event, inspect

from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, create_database_engine, sqlite_url
from pandrator.web.models import Artifact, ArtifactEdge, SoundItem, SoundVariant
from pandrator.web.sound_identity import (
    sound_creation_workspace,
    sound_item_workspace,
    sound_variant_workspace,
)

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
SOUND_REVISION = "0062_sound_workspace"
WORKSPACE_FKS = {
    "sound_items": "fk_sound_items_workspace_id_workspaces",
    "sound_variants": "fk_sound_variants_workspace_id_workspaces",
}
WORKSPACE_INDEXES = {
    "sound_items": {
        "ix_sound_items_workspace_id": ["workspace_id"],
        "ix_sound_items_workspace_archived_created_at_id": [
            "workspace_id",
            "archived",
            "created_at",
            "id",
        ],
    },
    "sound_variants": {
        "ix_sound_variants_workspace_id": ["workspace_id"],
        "ix_sound_variants_workspace_fingerprint_state": [
            "workspace_id",
            "fingerprint",
            "state",
        ],
        "ix_sound_variants_workspace_created_at_id": [
            "workspace_id",
            "created_at",
            "id",
        ],
    },
}
EXISTING_INDEXES = {
    "sound_items": {
        "ix_sound_items_archived_created_at_id": ["archived", "created_at", "id"],
    },
    "sound_variants": {
        "ix_sound_variants_item_created_at_id": ["item_id", "created_at", "id"],
        "ix_sound_variants_fingerprint_state": ["fingerprint", "state"],
    },
}
FIXED_TIME = "2026-10-23 12:00:00.000000"


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _upgrade_to_0061(path: Path) -> None:
    command.upgrade(_config(path), "0061_artifact_ownership")


def _strip_sound_scope(path: Path) -> None:
    """Recreate the historical 0061 sound shape despite current initial metadata."""
    engine = create_database_engine(path)
    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        operations = Operations(
            MigrationContext.configure(connection, opts={"render_as_batch": True})
        )
        with connection.begin():
            for table_name in ("sound_variants", "sound_items"):
                with operations.batch_alter_table(table_name) as batch:
                    for index_name in WORKSPACE_INDEXES[table_name]:
                        batch.drop_index(index_name)
                    batch.drop_constraint(WORKSPACE_FKS[table_name], type_="foreignkey")
                    batch.drop_column("workspace_id")
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        assert violations == []
    finally:
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
        finally:
            connection.close()
            engine.dispose()


def _seed_workspaces_and_artifacts(path: Path) -> dict[str, str]:
    _upgrade_to_0061(path)
    database = Database(path)
    try:
        with database.session() as session:
            personal = initial_personal_workspace(session)
            active = Workspace(
                id="workspace-active-team",
                kind="team",
                name="Active team",
                state="active",
            )
            archived = Workspace(
                id="workspace-archived-team",
                kind="team",
                name="Archived team",
                state="archived",
            )
            session.add_all([active, archived])
            session.flush([active, archived])
            artifacts = [
                Artifact(
                    id="artifact-active",
                    workspace_id=active.id,
                    kind="audio",
                    role="sound",
                    relative_path="sound/active.wav",
                    mime_type="audio/wav",
                    size_bytes=64,
                    content_hash="hash-active",
                    state="deleted",
                    metadata_json={},
                ),
                Artifact(
                    id="artifact-archived-a",
                    workspace_id=archived.id,
                    kind="audio",
                    role="sound",
                    relative_path="sound/archived-a.wav",
                    mime_type="audio/wav",
                    size_bytes=64,
                    content_hash="hash-archived-a",
                    state="stale",
                    metadata_json={"project_id": "project-consumer-cue"},
                ),
                Artifact(
                    id="artifact-archived-b",
                    workspace_id=archived.id,
                    kind="audio",
                    role="sound",
                    relative_path="sound/archived-b.wav",
                    mime_type="audio/wav",
                    size_bytes=64,
                    content_hash="hash-archived-b",
                    state="ready",
                    metadata_json={},
                ),
            ]
            session.add_all(artifacts)
            session.flush(artifacts)
            session.add(
                ArtifactEdge(
                    parent_artifact_id="artifact-archived-a",
                    child_artifact_id="artifact-active",
                    relation="derived_from",
                )
            )
            return {
                "personal": personal.id,
                "active": active.id,
                "archived": archived.id,
            }
    finally:
        database.dispose()


def _insert_sound_rows(
    path: Path,
    *,
    items: list[tuple[str, str, bool, str | None]],
    variants: list[tuple[str, str | None, str | None, str, dict[str, object] | None]],
) -> None:
    with sqlite3.connect(path) as connection:
        for item_id, name, archived, preferred_variant_id in items:
            connection.execute(
                "INSERT INTO sound_items "
                "(id, name, tags_json, revision, archived, preferred_variant_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    name,
                    "[]",
                    3,
                    int(archived),
                    preferred_variant_id,
                    FIXED_TIME,
                    FIXED_TIME,
                ),
            )
        for variant_id, item_id, artifact_id, state, provenance in variants:
            connection.execute(
                "INSERT INTO sound_variants "
                "(id, item_id, job_id, artifact_id, state, fingerprint, provenance_json, "
                "sample_rate, channels, frame_count, duration_seconds, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    variant_id,
                    item_id,
                    artifact_id,
                    state,
                    f"fingerprint-{variant_id}",
                    json.dumps(provenance or {}, sort_keys=True),
                    48000,
                    2,
                    2400,
                    0.05,
                    FIXED_TIME,
                ),
            )


def _schema_snapshot(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def _data_snapshot(
    path: Path, *, exclude_sound_scope: bool = False, exclude_version: bool = False
) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        snapshot: dict[str, list[tuple[object, ...]]] = {}
        for table in tables:
            if exclude_version and table == "alembic_version":
                continue
            columns = [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if not (exclude_sound_scope and table in WORKSPACE_FKS and row[1] == "workspace_id")
            ]
            if columns:
                selected = ", ".join(f'"{column}"' for column in columns)
                snapshot[table] = connection.execute(
                    f'SELECT {selected} FROM "{table}" ORDER BY rowid'
                ).fetchall()
        return snapshot


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def test_genuine_0061_sound_migration_propagates_groups_and_preserves_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "genuine-0061.sqlite3"
    workspaces = _seed_workspaces_and_artifacts(path)
    _strip_sound_scope(path)
    _insert_sound_rows(
        path,
        items=[
            ("item-archived", "Archived history", True, "variant-member"),
            ("item-empty", "Empty item", False, None),
            ("item-cues-only", "Consumer cues", False, None),
            ("item-active", "Active artifact", False, None),
        ],
        variants=[
            ("variant-artifact", "item-archived", "artifact-archived-a", "stale", None),
            ("variant-member", "item-archived", None, "discarded", None),
            (
                "variant-cues-only",
                "item-cues-only",
                None,
                "failed",
                {"project_id": "project-consumer-cue", "artifact_id": "artifact-archived-a"},
            ),
            ("variant-active", "item-active", "artifact-active", "ready", None),
            ("variant-orphan-artifact", None, "artifact-archived-b", "stale", None),
            ("variant-orphan-empty", None, None, "queued", None),
        ],
    )
    with sqlite3.connect(path) as connection:
        assert "workspace_id" not in {
            row[1] for row in connection.execute('PRAGMA table_info("sound_items")')
        }
        assert "workspace_id" not in {
            row[1] for row in connection.execute('PRAGMA table_info("sound_variants")')
        }
    before = _data_snapshot(path, exclude_version=True)

    command.upgrade(_config(path), SOUND_REVISION)

    assert _version(path) == SOUND_REVISION
    with sqlite3.connect(path) as connection:
        item_owners = dict(
            connection.execute("SELECT id, workspace_id FROM sound_items ORDER BY id").fetchall()
        )
        variant_owners = dict(
            connection.execute("SELECT id, workspace_id FROM sound_variants ORDER BY id").fetchall()
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT archived, revision, name FROM sound_items WHERE id = 'item-archived'"
        ).fetchone() == (1, 3, "Archived history")
        assert connection.execute(
            "SELECT state FROM sound_variants WHERE id = 'variant-artifact'"
        ).fetchone() == ("stale",)
        assert connection.execute(
            "SELECT state FROM sound_variants WHERE id = 'variant-member'"
        ).fetchone() == ("discarded",)
        assert connection.execute(
            "SELECT state FROM sound_variants WHERE id = 'variant-active'"
        ).fetchone() == ("ready",)

    assert item_owners == {
        "item-archived": workspaces["archived"],
        "item-empty": workspaces["personal"],
        "item-cues-only": workspaces["personal"],
        "item-active": workspaces["active"],
    }
    assert variant_owners == {
        "variant-artifact": workspaces["archived"],
        "variant-member": workspaces["archived"],
        "variant-cues-only": workspaces["personal"],
        "variant-active": workspaces["active"],
        "variant-orphan-artifact": workspaces["archived"],
        "variant-orphan-empty": workspaces["personal"],
    }
    assert _data_snapshot(path, exclude_sound_scope=True, exclude_version=True) == before

    engine = Database(path).engine
    try:
        inspector = inspect(engine)
        for table_name, fk_name in WORKSPACE_FKS.items():
            column = next(
                column
                for column in inspector.get_columns(table_name)
                if column["name"] == "workspace_id"
            )
            assert column["nullable"] is True
            indexes = {index["name"]: index for index in inspector.get_indexes(table_name)}
            for index_name, columns in WORKSPACE_INDEXES[table_name].items():
                assert indexes[index_name]["column_names"] == columns
                assert not indexes[index_name]["unique"]
            for index_name, columns in EXISTING_INDEXES[table_name].items():
                assert indexes[index_name]["column_names"] == columns
            foreign_key = next(
                key for key in inspector.get_foreign_keys(table_name) if key["name"] == fk_name
            )
            assert foreign_key["constrained_columns"] == ["workspace_id"]
            assert foreign_key["referred_table"] == "workspaces"
            assert foreign_key["referred_columns"] == ["id"]
            assert (foreign_key.get("options") or {}).get("ondelete", "").upper() == "RESTRICT"
    finally:
        engine.dispose()

    first_schema = _schema_snapshot(path)
    first_data = _data_snapshot(path)
    command.upgrade(_config(path), SOUND_REVISION)
    assert _schema_snapshot(path) == first_schema
    assert _data_snapshot(path) == first_data

    command.downgrade(_config(path), "0061_artifact_ownership")
    assert _version(path) == "0061_artifact_ownership"
    assert _data_snapshot(path, exclude_version=True) == before
    with sqlite3.connect(path) as connection:
        for table_name in WORKSPACE_FKS:
            assert "workspace_id" not in {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }


@pytest.mark.parametrize(
    ("issue", "items", "variants"),
    [
        (
            "group-conflict",
            [("item", "Conflict", False, None)],
            [
                ("variant-a", "item", "artifact-active", "ready", None),
                ("variant-b", "item", "artifact-archived-a", "ready", None),
            ],
        ),
        (
            "missing-item",
            [],
            [("variant", "missing-item", None, "queued", None)],
        ),
        (
            "missing-artifact",
            [],
            [("variant", None, "missing-artifact", "queued", None)],
        ),
        (
            "missing-preferred",
            [("item", "Missing preferred", False, "missing-variant")],
            [],
        ),
        (
            "preferred-other-item",
            [
                ("item-a", "Preferred source", False, "variant-b"),
                ("item-b", "Preferred target", False, None),
            ],
            [("variant-b", "item-b", None, "ready", None)],
        ),
    ],
)
def test_invalid_sound_ownership_evidence_fails_before_ddl(
    tmp_path: Path,
    issue: str,
    items: list[tuple[str, str, bool, str | None]],
    variants: list[tuple[str, str | None, str | None, str, dict[str, object] | None]],
) -> None:
    path = tmp_path / f"invalid-{issue}.sqlite3"
    _seed_workspaces_and_artifacts(path)
    _insert_sound_rows(path, items=items, variants=variants)
    before_schema = _schema_snapshot(path)
    before_data = _data_snapshot(path)

    with pytest.raises(RuntimeError):
        command.upgrade(_config(path), SOUND_REVISION)

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_data
    assert _version(path) == "0061_artifact_ownership"


def test_missing_artifact_workspace_fails_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "missing-artifact-workspace.sqlite3"
    _seed_workspaces_and_artifacts(path)
    _insert_sound_rows(
        path,
        items=[],
        variants=[("variant", None, "artifact-active", "ready", None)],
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE artifacts SET workspace_id = 'missing-workspace' WHERE id = 'artifact-active'"
        )
    before_schema = _schema_snapshot(path)
    before_data = _data_snapshot(path)

    with pytest.raises(RuntimeError, match="missing or invalid workspace ownership"):
        command.upgrade(_config(path), SOUND_REVISION)

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_data


def test_empty_early_metadata_scope_is_adopted(tmp_path: Path) -> None:
    path = tmp_path / "early-empty-scope.sqlite3"
    workspaces = _seed_workspaces_and_artifacts(path)
    _insert_sound_rows(
        path,
        items=[("item", "Early empty", False, None)],
        variants=[("variant", "item", None, "queued", None)],
    )

    command.upgrade(_config(path), SOUND_REVISION)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT workspace_id FROM sound_items WHERE id = 'item'"
        ).fetchone() == (workspaces["personal"],)
        assert connection.execute(
            "SELECT workspace_id FROM sound_variants WHERE id = 'variant'"
        ).fetchone() == (workspaces["personal"],)


@pytest.mark.parametrize("issue", ["partial", "malformed", "prepopulated"])
def test_early_scope_guard_rejects_partial_malformed_or_populated_data(
    tmp_path: Path, issue: str
) -> None:
    path = tmp_path / f"early-{issue}.sqlite3"
    workspaces = _seed_workspaces_and_artifacts(path)
    _insert_sound_rows(
        path,
        items=[("item", "Early scope", False, None)],
        variants=[],
    )
    if issue == "partial":
        _strip_sound_scope(path)
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE sound_items ADD COLUMN workspace_id VARCHAR(36)")
    elif issue == "malformed":
        with sqlite3.connect(path) as connection:
            connection.execute("DROP INDEX ix_sound_items_workspace_id")
            connection.execute("CREATE INDEX ix_sound_items_workspace_id ON sound_items(name)")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE sound_items SET workspace_id = ? WHERE id = 'item'",
                (workspaces["personal"],),
            )
    before_schema = _schema_snapshot(path)
    before_data = _data_snapshot(path)

    with pytest.raises(RuntimeError):
        command.upgrade(_config(path), SOUND_REVISION)

    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_data
    assert _version(path) == "0061_artifact_ownership"


def test_late_sound_adoption_failure_rolls_back_and_retries(tmp_path: Path) -> None:
    path = tmp_path / "late-failure-retry.sqlite3"
    _seed_workspaces_and_artifacts(path)
    _strip_sound_scope(path)
    _insert_sound_rows(
        path,
        items=[("item-a", "A", False, None), ("item-b", "B", False, None)],
        variants=[],
    )
    before_schema = _schema_snapshot(path)
    before_data = _data_snapshot(path)
    update_count = 0

    def fail_after_first_adoption(
        _connection, _cursor, statement: str, _parameters, _context, _executemany
    ) -> None:
        nonlocal update_count
        if statement.startswith("UPDATE sound_items SET workspace_id"):
            update_count += 1
            if update_count == 2:
                raise RuntimeError("Injected late sound adoption failure")

    event.listen(Engine, "before_cursor_execute", fail_after_first_adoption)
    try:
        with pytest.raises(RuntimeError, match="Injected late sound adoption failure"):
            command.upgrade(_config(path), SOUND_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_first_adoption)

    assert update_count == 2
    assert _schema_snapshot(path) == before_schema
    assert _data_snapshot(path) == before_data
    assert _version(path) == "0061_artifact_ownership"

    command.upgrade(_config(path), SOUND_REVISION)
    assert _version(path) == "0062_sound_workspace"


def test_sound_identity_helpers_validate_active_stored_ownership(tmp_path: Path) -> None:
    path = tmp_path / "sound-identity.sqlite3"
    workspaces = _seed_workspaces_and_artifacts(path)
    command.upgrade(_config(path), SOUND_REVISION)
    database = Database(path)
    try:
        with database.session() as session:
            personal = initial_personal_workspace(session)
            active = session.get(Workspace, workspaces["active"])
            archived = session.get(Workspace, workspaces["archived"])
            assert active is not None
            assert archived is not None
            assert sound_creation_workspace(session).id == personal.id
            assert sound_creation_workspace(session, workspace_id=active.id).id == active.id
            with pytest.raises(ValueError, match="inactive workspace"):
                sound_creation_workspace(session, workspace_id=archived.id)

            artifact = session.get(Artifact, "artifact-active")
            assert artifact is not None
            item = SoundItem(
                id="helper-item",
                workspace_id=personal.id,
                name="Helper item",
                tags_json=[],
                revision=1,
                archived=False,
                preferred_variant_id=None,
            )
            session.add(item)
            session.flush([item])
            variant = SoundVariant(
                id="helper-variant",
                workspace_id=personal.id,
                item_id=item.id,
                artifact_id=None,
                state="ready",
                fingerprint="helper-fingerprint",
                provenance_json={},
                created_at=item.created_at,
            )
            session.add(variant)
            session.flush([variant])
            other_item = SoundItem(
                id="helper-other-item",
                workspace_id=active.id,
                name="Other helper item",
                tags_json=[],
                revision=1,
                archived=False,
                preferred_variant_id=None,
            )
            session.add(other_item)
            session.flush([other_item])
            other_variant = SoundVariant(
                id="helper-other-variant",
                workspace_id=active.id,
                item_id=other_item.id,
                artifact_id=None,
                state="ready",
                fingerprint="other-helper-fingerprint",
                provenance_json={},
                created_at=other_item.created_at,
            )
            session.add(other_variant)
            session.flush([other_variant])
            assert sound_item_workspace(session, item).id == personal.id
            assert sound_variant_workspace(session, variant).id == personal.id
            with pytest.raises(ValueError, match="not requested workspace"):
                sound_item_workspace(session, item, workspace_id=active.id)
            with pytest.raises(ValueError, match="not requested workspace"):
                sound_variant_workspace(session, variant, workspace_id=active.id)

            item.preferred_variant_id = "missing-preferred"
            with pytest.raises(ValueError, match="missing preferred variant"):
                sound_item_workspace(session, item)
            item.preferred_variant_id = other_variant.id
            with pytest.raises(ValueError, match="does not belong to sound item"):
                sound_item_workspace(session, item)
            item.preferred_variant_id = variant.id
            assert sound_item_workspace(session, item).id == personal.id

            variant.item_id = other_item.id
            with pytest.raises(ValueError, match="different workspace ownership"):
                sound_variant_workspace(session, variant)
            variant.item_id = item.id

            item.workspace_id = None
            with pytest.raises(ValueError, match="no stored workspace"):
                sound_item_workspace(session, item)
            item.workspace_id = personal.id
            variant.workspace_id = None
            with pytest.raises(ValueError, match="no stored workspace"):
                sound_variant_workspace(session, variant)
            variant.workspace_id = personal.id
            variant.workspace_id = archived.id
            with pytest.raises(ValueError, match="inactive workspace"):
                sound_variant_workspace(session, variant)
            variant.workspace_id = personal.id

            artifact.workspace_id = personal.id
            variant.artifact_id = artifact.id
            assert sound_variant_workspace(session, variant).id == personal.id
            artifact.workspace_id = active.id
            with pytest.raises(ValueError, match="different workspace ownership"):
                sound_variant_workspace(session, variant)
    finally:
        database.dispose()
