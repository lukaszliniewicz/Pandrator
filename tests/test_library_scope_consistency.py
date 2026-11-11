from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import Workspace
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import (
    Artifact,
    ResearchCacheEntry,
    SoundItem,
    SoundVariant,
    TrainingRun,
    Voice,
    VoiceCollection,
    VoiceCollectionMember,
    VoiceSample,
)
from pandrator.web.projects.models import Project
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_library_commands import save_in_session
from tests.legacy_schema_support import insert_job

MIGRATIONS = Path(__file__).parents[1] / "pandrator" / "web" / "migrations"
TARGET_REVISION = "0078_library_scope_consistency"
PREDECESSOR_REVISION = "0077_media_scope_consistency"
LIBRARY_PREFIX = "pandrator_library_scope_"
_SqlParams: TypeAlias = dict[str, object]
_NOW = "2026-10-24T12:00:00+00:00"

TRIGGERS = (
    ("pandrator_library_scope_voices_update", "voices"),
    ("pandrator_library_scope_voice_collections_update", "voice_collections"),
    ("pandrator_library_scope_jobs_update", "jobs"),
    ("pandrator_library_scope_voice_samples_insert", "voice_samples"),
    ("pandrator_library_scope_voice_samples_update", "voice_samples"),
    ("pandrator_library_scope_training_runs_insert", "training_runs"),
    ("pandrator_library_scope_training_runs_update", "training_runs"),
    ("pandrator_library_scope_voice_collection_members_insert", "voice_collection_members"),
    ("pandrator_library_scope_voice_collection_members_update", "voice_collection_members"),
    ("pandrator_library_scope_sound_items_insert", "sound_items"),
    ("pandrator_library_scope_sound_items_update", "sound_items"),
    ("pandrator_library_scope_sound_variants_insert", "sound_variants"),
    ("pandrator_library_scope_sound_variants_update", "sound_variants"),
    ("pandrator_library_scope_research_cache_entries_insert", "research_cache_entries"),
    ("pandrator_library_scope_research_cache_entries_update", "research_cache_entries"),
)

IDS = {
    "workspace_other": "81000000-0000-4000-8000-000000000001",
    "workspace_archived": "81000000-0000-4000-8000-000000000002",
    "project_main": "82000000-0000-4000-8000-000000000001",
    "project_other": "82000000-0000-4000-8000-000000000002",
    "voice_main": "83000000-0000-4000-8000-000000000001",
    "voice_main_alt": "83000000-0000-4000-8000-000000000002",
    "voice_other": "83000000-0000-4000-8000-000000000003",
    "voice_archived": "83000000-0000-4000-8000-000000000004",
    "collection_main": "84000000-0000-4000-8000-000000000001",
    "collection_main_alt": "84000000-0000-4000-8000-000000000002",
    "collection_other": "84000000-0000-4000-8000-000000000003",
    "job_main": "85000000-0000-4000-8000-000000000001",
    "job_other": "85000000-0000-4000-8000-000000000002",
    "artifact_sample": "86000000-0000-4000-8000-000000000001",
    "artifact_sample_alt": "86000000-0000-4000-8000-000000000002",
    "artifact_training": "86000000-0000-4000-8000-000000000003",
    "artifact_sound": "86000000-0000-4000-8000-000000000004",
    "artifact_other": "86000000-0000-4000-8000-000000000005",
    "artifact_archived": "86000000-0000-4000-8000-000000000006",
    "sample_main": "87000000-0000-4000-8000-000000000001",
    "training_main": "88000000-0000-4000-8000-000000000001",
    "member_main": "89000000-0000-4000-8000-000000000001",
    "member_collection": "89000000-0000-4000-8000-000000000002",
    "item_main": "8a000000-0000-4000-8000-000000000001",
    "item_other": "8a000000-0000-4000-8000-000000000002",
    "variant_main": "8b000000-0000-4000-8000-000000000001",
    "variant_main_alt": "8b000000-0000-4000-8000-000000000002",
    "variant_other": "8b000000-0000-4000-8000-000000000003",
    "cache_main": "cache-main-key",
    "cache_workspace": "cache-workspace-only-key",
    "missing_parent": "99000000-0000-4000-8000-000000000099",
}


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


def _upgrade_to_0077(path: Path) -> Config:
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    return config


def _make_valid_predecessor(path: Path) -> dict[str, str]:
    _upgrade_to_0077(path)
    database = Database(path)
    try:
        with database.session() as session:
            workspace = initial_personal_workspace(session)
            other_workspace = Workspace(
                id=IDS["workspace_other"],
                kind="team",
                name="Other workspace",
                state="active",
            )
            archived_workspace = Workspace(
                id=IDS["workspace_archived"],
                kind="team",
                name="Archived workspace",
                state="archived",
            )
            session.add_all([other_workspace, archived_workspace])
            session.flush()

            session.add_all(
                [
                    Project(
                        id=IDS["project_main"],
                        workspace_id=workspace.id,
                        source_session_id=None,
                        name="Main retained root",
                        created_by_user_id=None,
                    ),
                    Project(
                        id=IDS["project_other"],
                        workspace_id=other_workspace.id,
                        source_session_id=None,
                        name="Other retained root",
                        created_by_user_id=None,
                    ),
                ]
            )
            insert_job(
                session,
                job_id=IDS["job_main"],
                kind="library-fixture",
                workspace_id=workspace.id,
            )
            insert_job(
                session,
                job_id=IDS["job_other"],
                kind="library-fixture",
                workspace_id=other_workspace.id,
            )
            session.add_all(
                [
                    Artifact(
                        id=IDS["artifact_sample"],
                        workspace_id=workspace.id,
                        kind="audio",
                        role="voice.sample",
                        relative_path="library/sample.wav",
                        size_bytes=8,
                        content_hash="a" * 64,
                    ),
                    Artifact(
                        id=IDS["artifact_sample_alt"],
                        workspace_id=workspace.id,
                        kind="audio",
                        role="voice.sample",
                        relative_path="library/sample-alt.wav",
                        size_bytes=8,
                        content_hash="b" * 64,
                    ),
                    Artifact(
                        id=IDS["artifact_training"],
                        workspace_id=workspace.id,
                        kind="model",
                        role="training.output",
                        relative_path="library/training.bin",
                        size_bytes=8,
                        content_hash="c" * 64,
                    ),
                    Artifact(
                        id=IDS["artifact_sound"],
                        workspace_id=workspace.id,
                        kind="audio",
                        role="sound.original",
                        relative_path="library/sound.wav",
                        size_bytes=8,
                        content_hash="d" * 64,
                    ),
                    Artifact(
                        id=IDS["artifact_other"],
                        workspace_id=other_workspace.id,
                        kind="audio",
                        role="sound.original",
                        relative_path="library/other.wav",
                        size_bytes=8,
                        content_hash="e" * 64,
                    ),
                    Artifact(
                        id=IDS["artifact_archived"],
                        workspace_id=archived_workspace.id,
                        kind="audio",
                        role="voice.sample",
                        relative_path="library/archived.wav",
                        size_bytes=8,
                        content_hash="f" * 64,
                    ),
                    Voice(
                        id=IDS["voice_main"],
                        workspace_id=workspace.id,
                        name="Main voice",
                    ),
                    Voice(
                        id=IDS["voice_main_alt"],
                        workspace_id=workspace.id,
                        name="Second main voice",
                    ),
                    Voice(
                        id=IDS["voice_other"],
                        workspace_id=other_workspace.id,
                        name="Other voice",
                    ),
                    Voice(
                        id=IDS["voice_archived"],
                        workspace_id=archived_workspace.id,
                        name="Archived voice",
                    ),
                    VoiceCollection(
                        id=IDS["collection_main"],
                        workspace_id=workspace.id,
                        name="Main collection",
                    ),
                    VoiceCollection(
                        id=IDS["collection_main_alt"],
                        workspace_id=workspace.id,
                        name="Second main collection",
                    ),
                    VoiceCollection(
                        id=IDS["collection_other"],
                        workspace_id=other_workspace.id,
                        name="Other collection",
                    ),
                ]
            )
            session.flush()

            session.add_all(
                [
                    VoiceSample(
                        id=IDS["sample_main"],
                        workspace_id=workspace.id,
                        voice_id=IDS["voice_main"],
                        artifact_id=IDS["artifact_sample"],
                    ),
                    TrainingRun(
                        id=IDS["training_main"],
                        workspace_id=workspace.id,
                        voice_id=IDS["voice_main"],
                        job_id=IDS["job_main"],
                        source_artifact_id=IDS["artifact_training"],
                        source_text_artifact_id=IDS["artifact_training"],
                        output_artifact_id=IDS["artifact_training"],
                        model_name="fixture-model",
                    ),
                    VoiceCollectionMember(
                        workspace_id=workspace.id,
                        collection_id=IDS["collection_main"],
                        voice_key="opaque:catalogue:voice-key",
                        voice_ref_json={"provider": "fixture", "voice_id": "remote-1"},
                        managed_voice_id=IDS["voice_main"],
                    ),
                    VoiceCollectionMember(
                        workspace_id=workspace.id,
                        collection_id=IDS["collection_main"],
                        voice_key="external:provider:member-2",
                        voice_ref_json={"provider": "fixture", "voice_id": "remote-2"},
                        managed_voice_id=IDS["voice_main_alt"],
                    ),
                    VoiceCollectionMember(
                        workspace_id=workspace.id,
                        collection_id=IDS["collection_main_alt"],
                        voice_key="opaque:provider:member-3",
                        voice_ref_json={"provider": "fixture", "voice_id": "remote-3"},
                        managed_voice_id=IDS["voice_main_alt"],
                    ),
                    VoiceCollectionMember(
                        workspace_id=other_workspace.id,
                        collection_id=IDS["collection_other"],
                        voice_key="other:workspace:voice-key",
                        voice_ref_json={"provider": "fixture", "voice_id": "remote-4"},
                        managed_voice_id=IDS["voice_other"],
                    ),
                    SoundItem(
                        id=IDS["item_main"],
                        workspace_id=workspace.id,
                        name="Main sound",
                        tags_json=["wind"],
                        revision=1,
                        archived=False,
                        preferred_variant_id=None,
                    ),
                    SoundItem(
                        id=IDS["item_other"],
                        workspace_id=other_workspace.id,
                        name="Other sound",
                        tags_json=[],
                        revision=1,
                        archived=False,
                        preferred_variant_id=None,
                    ),
                    ResearchCacheEntry(
                        cache_key=IDS["cache_main"],
                        workspace_id=workspace.id,
                        project_id=IDS["project_main"],
                        provider="fixture",
                        operation="search",
                        request_json={"q": "test"},
                        response_json={"items": []},
                        expires_at=datetime.now(UTC) + timedelta(days=1),
                    ),
                    ResearchCacheEntry(
                        cache_key=IDS["cache_workspace"],
                        workspace_id=workspace.id,
                        project_id=None,
                        provider="fixture",
                        operation="search",
                        request_json={"q": "workspace"},
                        response_json={"items": []},
                        expires_at=datetime.now(UTC) + timedelta(days=1),
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    SoundVariant(
                        id=IDS["variant_main"],
                        workspace_id=workspace.id,
                        item_id=IDS["item_main"],
                        artifact_id=IDS["artifact_sound"],
                        state="ready",
                        provenance_json={"source": "fixture"},
                    ),
                    SoundVariant(
                        id=IDS["variant_main_alt"],
                        workspace_id=workspace.id,
                        item_id=IDS["item_main"],
                        artifact_id=IDS["artifact_sound"],
                        state="ready",
                        provenance_json={"source": "fixture-alt"},
                    ),
                    SoundVariant(
                        id=IDS["variant_other"],
                        workspace_id=other_workspace.id,
                        item_id=IDS["item_other"],
                        artifact_id=IDS["artifact_other"],
                        state="ready",
                        provenance_json={"source": "other"},
                    ),
                ]
            )
            session.flush()
            main_item = session.get(SoundItem, IDS["item_main"])
            other_item = session.get(SoundItem, IDS["item_other"])
            assert main_item is not None and other_item is not None
            main_item.preferred_variant_id = IDS["variant_main"]
            other_item.preferred_variant_id = IDS["variant_other"]
            session.flush([main_item, other_item])
            result = dict(IDS)
            result["workspace_main"] = workspace.id
            return result
    finally:
        database.dispose()


def _snapshot(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        table_names = [
            str(name)
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if name != "alembic_version"
        ]
        rows = {
            name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in table_names
        }
    return {"schema": schema, "rows": rows}


def _schema_without_library_triggers(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return [
            row
            for row in connection.execute(
                "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            if not (row[0] == "trigger" and str(row[1]).startswith(LIBRARY_PREFIX))
        ]


def _rows_without_alembic_version(path: Path) -> dict[str, list[tuple[object, ...]]]:
    snapshot = _snapshot(path)
    rows = snapshot["rows"]
    assert isinstance(rows, dict)
    return {str(name): value for name, value in rows.items()}


def _library_triggers(path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND substr(name, 1, ?) = ? ORDER BY name",
            (len(LIBRARY_PREFIX), LIBRARY_PREFIX),
        ).fetchall()
    return tuple((str(name), str(table), str(sql)) for name, table, sql in rows)


def _version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _foreign_key_violations(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def _reject(path: Path, statement: str, parameters: _SqlParams, message: str) -> None:
    before = _snapshot(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match=re.escape(message)):
            connection.execute(statement, parameters)
    assert _snapshot(path) == before


def _read_rows(
    path: Path, statement: str, parameters: _SqlParams | None = None
) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(statement, parameters or {}).fetchall()


def _update_foreign_keys_off(path: Path, statement: str, parameters: _SqlParams) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(statement, parameters)


def test_fresh_upgrade_installs_exact_library_triggers_and_repeats_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh-library-scope.sqlite3"
    config = _config(path)

    command.upgrade(config, TARGET_REVISION)
    first = _library_triggers(path)
    assert tuple((name, table) for name, table, _sql in first) == tuple(sorted(TRIGGERS))
    assert len(first) == 15
    assert _foreign_key_violations(path) == []

    command.upgrade(config, TARGET_REVISION)
    assert _library_triggers(path) == first
    assert _foreign_key_violations(path) == []


def test_populated_upgrade_preserves_rows_objects_and_unrelated_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "populated-library-scope.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER unrelated_project_name_snapshot "
            "AFTER UPDATE OF name ON projects BEGIN SELECT NEW.id; END"
        )
    before_schema = _schema_without_library_triggers(path)
    before_rows = _rows_without_alembic_version(path)

    command.upgrade(config, TARGET_REVISION)

    assert _schema_without_library_triggers(path) == before_schema
    assert _rows_without_alembic_version(path) == before_rows
    assert len(_library_triggers(path)) == 15
    assert _foreign_key_violations(path) == []

    command.downgrade(config, PREDECESSOR_REVISION)
    assert _schema_without_library_triggers(path) == before_schema
    assert _rows_without_alembic_version(path) == before_rows
    assert _library_triggers(path) == ()

    command.upgrade(config, TARGET_REVISION)
    assert len(_library_triggers(path)) == 15
    assert _rows_without_alembic_version(path) == before_rows
    assert _foreign_key_violations(path) == []


def test_raw_triggers_reject_cross_workspace_and_immutable_scope_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw-library-scope.sqlite3"
    ids = _make_valid_predecessor(path)
    command.upgrade(_config(path), TARGET_REVISION)
    params = {"main": ids["workspace_main"], "other": ids["workspace_other"]}

    _reject(
        path,
        "UPDATE voices SET workspace_id = :other WHERE id = :id",
        {**params, "id": ids["voice_main"]},
        "voice workspace is immutable",
    )
    _reject(
        path,
        "UPDATE voice_collections SET workspace_id = :other WHERE id = :id",
        {**params, "id": ids["collection_main"]},
        "voice collection workspace is immutable",
    )
    _reject(
        path,
        "UPDATE jobs SET workspace_id = :other WHERE id = :id",
        {**params, "id": ids["job_main"]},
        "job workspace is immutable",
    )

    _reject(
        path,
        "INSERT INTO voice_samples (id, workspace_id, voice_id, artifact_id) "
        "VALUES ('sample-cross', :main, :voice, :artifact)",
        {
            **params,
            "voice": ids["voice_main"],
            "artifact": ids["artifact_other"],
        },
        "voice sample artifact must share its workspace",
    )
    _reject(
        path,
        "UPDATE voice_samples SET artifact_id = :artifact WHERE id = :id",
        {"artifact": ids["artifact_other"], "id": ids["sample_main"]},
        "voice sample artifact must share its workspace",
    )
    _reject(
        path,
        "UPDATE voice_samples SET workspace_id = :other WHERE id = :id",
        {"other": ids["workspace_other"], "id": ids["sample_main"]},
        "voice sample workspace is immutable",
    )
    _reject(
        path,
        "UPDATE voice_samples SET voice_id = :voice WHERE id = :id",
        {"voice": ids["voice_main_alt"], "id": ids["sample_main"]},
        "voice sample voice is immutable",
    )

    _reject(
        path,
        "INSERT INTO training_runs (id, workspace_id, voice_id, model_name) "
        "VALUES ('training-cross', :main, :voice, 'fixture')",
        {**params, "voice": ids["voice_other"]},
        "training voice must share its workspace",
    )
    training_links = (
        ("voice_id", ids["voice_other"], "training voice must share its workspace"),
        ("job_id", ids["job_other"], "training job must share its workspace"),
        (
            "source_artifact_id",
            ids["artifact_other"],
            "training source artifact must share its workspace",
        ),
        (
            "source_text_artifact_id",
            ids["artifact_other"],
            "training text artifact must share its workspace",
        ),
        (
            "output_artifact_id",
            ids["artifact_other"],
            "training output artifact must share its workspace",
        ),
    )
    for column, value, message in training_links:
        _reject(
            path,
            f"UPDATE training_runs SET {column} = :value WHERE id = :id",
            {"value": value, "id": ids["training_main"]},
            message,
        )
    _reject(
        path,
        "UPDATE training_runs SET workspace_id = :other WHERE id = :id",
        {"other": ids["workspace_other"], "id": ids["training_main"]},
        "training workspace is immutable",
    )

    _reject(
        path,
        "INSERT INTO voice_collection_members (workspace_id, collection_id, voice_key, "
        "voice_ref_json, managed_voice_id) VALUES (:main, :collection, 'cross:voice-key', '{}', NULL)",
        {**params, "collection": ids["collection_other"]},
        "voice collection must share its workspace",
    )
    _reject(
        path,
        "UPDATE voice_collection_members SET managed_voice_id = :voice "
        "WHERE collection_id = :collection AND voice_key = :key",
        {
            "voice": ids["voice_other"],
            "collection": ids["collection_main"],
            "key": "opaque:catalogue:voice-key",
        },
        "managed voice must share its workspace",
    )
    _reject(
        path,
        "UPDATE voice_collection_members SET workspace_id = :other "
        "WHERE collection_id = :collection AND voice_key = :key",
        {
            "other": ids["workspace_other"],
            "collection": ids["collection_main"],
            "key": "opaque:catalogue:voice-key",
        },
        "voice collection member workspace is immutable",
    )

    _reject(
        path,
        "INSERT INTO sound_items (id, workspace_id, name, tags_json, revision, archived, "
        "preferred_variant_id, created_at, updated_at) "
        "VALUES ('item-cross', :main, 'bad', '[]', 1, 0, :variant, :now, :now)",
        {**params, "variant": ids["variant_other"], "now": _NOW},
        "preferred sound variant must belong to its item",
    )
    _reject(
        path,
        "UPDATE sound_items SET preferred_variant_id = :variant WHERE id = :id",
        {"variant": ids["variant_other"], "id": ids["item_main"]},
        "preferred sound variant must belong to its item",
    )
    _reject(
        path,
        "UPDATE sound_items SET workspace_id = :other WHERE id = :id",
        {"other": ids["workspace_other"], "id": ids["item_main"]},
        "sound item workspace is immutable",
    )

    _reject(
        path,
        "INSERT INTO sound_variants (id, workspace_id, item_id, state, provenance_json, created_at) "
        "VALUES ('variant-cross', :main, :item, 'ready', '{}', :now)",
        {**params, "item": ids["item_other"], "now": _NOW},
        "sound variant item must share its workspace",
    )
    variant_links = (
        ("job_id", ids["job_other"], "sound variant job must share its workspace"),
        (
            "artifact_id",
            ids["artifact_other"],
            "sound variant artifact must share its workspace",
        ),
    )
    for column, value, message in variant_links:
        _reject(
            path,
            f"UPDATE sound_variants SET {column} = :value WHERE id = :id",
            {"value": value, "id": ids["variant_main"]},
            message,
        )
    _reject(
        path,
        "UPDATE sound_variants SET workspace_id = :other WHERE id = :id",
        {"other": ids["workspace_other"], "id": ids["variant_main"]},
        "sound variant workspace is immutable",
    )
    _reject(
        path,
        "UPDATE sound_variants SET item_id = :item WHERE id = :id",
        {"item": ids["item_other"], "id": ids["variant_main"]},
        "sound variant item attachment is immutable",
    )
    _reject(
        path,
        "UPDATE sound_variants SET item_id = NULL WHERE id = :id",
        {"id": ids["variant_main"]},
        "sound variant item attachment is immutable",
    )

    _reject(
        path,
        "INSERT INTO research_cache_entries (cache_key, workspace_id, project_id, provider, "
        "operation, request_json, response_json, created_at, expires_at) "
        "VALUES ('cross-cache', :main, :project, 'fixture', 'search', '{}', '{}', :now, :expires)",
        {
            **params,
            "project": ids["project_other"],
            "now": _NOW,
            "expires": "2026-10-25T12:00:00+00:00",
        },
        "research cache project must share its workspace",
    )
    _reject(
        path,
        "UPDATE research_cache_entries SET project_id = :project WHERE cache_key = :key",
        {"project": ids["project_other"], "key": ids["cache_main"]},
        "research cache project is immutable",
    )
    _reject(
        path,
        "UPDATE research_cache_entries SET workspace_id = :other WHERE cache_key = :key",
        {"other": ids["workspace_other"], "key": ids["cache_workspace"]},
        "research cache workspace is immutable",
    )
    _reject(
        path,
        "UPDATE research_cache_entries SET project_id = :project WHERE cache_key = :key",
        {"project": ids["project_main"], "key": ids["cache_workspace"]},
        "research cache project is immutable",
    )


def test_raw_triggers_and_required_foreign_keys_reject_missing_parents(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-library-parents.sqlite3"
    ids = _make_valid_predecessor(path)
    command.upgrade(_config(path), TARGET_REVISION)
    missing = ids["missing_parent"]
    workspace = ids["workspace_main"]
    cases: tuple[tuple[str, _SqlParams, str], ...] = (
        (
            "INSERT INTO voice_samples (id, workspace_id, voice_id, artifact_id) "
            "VALUES ('sample-missing', :workspace, :voice, :parent)",
            {"workspace": workspace, "voice": ids["voice_main"], "parent": missing},
            "voice sample artifact must share its workspace",
        ),
        (
            "UPDATE voice_samples SET artifact_id = :parent WHERE id = :id",
            {"parent": missing, "id": ids["sample_main"]},
            "voice sample artifact must share its workspace",
        ),
        (
            "INSERT INTO training_runs (id, workspace_id, job_id, model_name) "
            "VALUES ('training-missing', :workspace, :parent, 'fixture')",
            {"workspace": workspace, "parent": missing},
            "training job must share its workspace",
        ),
        (
            "UPDATE training_runs SET job_id = :parent WHERE id = :id",
            {"parent": missing, "id": ids["training_main"]},
            "training job must share its workspace",
        ),
        (
            "INSERT INTO voice_collection_members (workspace_id, collection_id, voice_key, "
            "voice_ref_json, managed_voice_id) VALUES (:workspace, :parent, 'missing:key', '{}', NULL)",
            {"workspace": workspace, "parent": missing},
            "voice collection must share its workspace",
        ),
        (
            "UPDATE voice_collection_members SET managed_voice_id = :parent "
            "WHERE collection_id = :collection AND voice_key = :key",
            {
                "parent": missing,
                "collection": ids["collection_main"],
                "key": "opaque:catalogue:voice-key",
            },
            "managed voice must share its workspace",
        ),
        (
            "INSERT INTO sound_items (id, workspace_id, name, tags_json, revision, archived, "
            "preferred_variant_id, created_at, updated_at) "
            "VALUES ('item-missing', :workspace, 'bad', '[]', 1, 0, :parent, :now, :now)",
            {"workspace": workspace, "parent": missing, "now": _NOW},
            "preferred sound variant must belong to its item",
        ),
        (
            "UPDATE sound_items SET preferred_variant_id = :parent WHERE id = :id",
            {"parent": missing, "id": ids["item_main"]},
            "preferred sound variant must belong to its item",
        ),
        (
            "INSERT INTO sound_variants (id, workspace_id, job_id, state, provenance_json, created_at) "
            "VALUES ('variant-missing', :workspace, :parent, 'queued', '{}', :now)",
            {"workspace": workspace, "parent": missing, "now": _NOW},
            "sound variant job must share its workspace",
        ),
        (
            "UPDATE sound_variants SET job_id = :parent WHERE id = :id",
            {"parent": missing, "id": ids["variant_main"]},
            "sound variant job must share its workspace",
        ),
        (
            "INSERT INTO research_cache_entries (cache_key, workspace_id, project_id, provider, "
            "operation, request_json, response_json, created_at, expires_at) "
            "VALUES ('missing-cache', :workspace, :parent, 'fixture', 'search', '{}', '{}', :now, :expires)",
            {
                "workspace": workspace,
                "parent": missing,
                "now": _NOW,
                "expires": "2026-10-25T12:00:00+00:00",
            },
            "research cache project must share its workspace",
        ),
        (
            "INSERT INTO voices (id, workspace_id, name, metadata_json, revision, created_at, updated_at) "
            "VALUES ('voice-missing', :parent, 'Missing root', '{}', 1, :now, :now)",
            {"parent": missing, "now": _NOW},
            "FOREIGN KEY constraint failed",
        ),
        (
            "INSERT INTO voice_collections (id, workspace_id, name, revision, created_at, updated_at) "
            "VALUES ('collection-missing', :parent, 'Missing root', 1, :now, :now)",
            {"parent": missing, "now": _NOW},
            "FOREIGN KEY constraint failed",
        ),
        (
            "INSERT INTO jobs (id, kind, workspace_id, status, payload_json, resource_keys_json, "
            "progress, lease_generation, attempts, max_attempts, created_at, updated_at) "
            "VALUES ('job-missing', 'fixture', :parent, 'queued', '{}', '[]', 0, 0, 0, 1, :now, :now)",
            {"parent": missing, "now": _NOW},
            "FOREIGN KEY constraint failed",
        ),
    )
    for statement, parameters, message in cases:
        _reject(path, statement, parameters, message)


def test_supported_scope_updates_and_nullable_link_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "positive-library-scope.sqlite3"
    ids = _make_valid_predecessor(path)
    command.upgrade(_config(path), TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "UPDATE voices SET workspace_id = workspace_id WHERE id = :id",
            {"id": ids["voice_archived"]},
        )
        connection.execute(
            "UPDATE voice_collections SET workspace_id = workspace_id WHERE id = :id",
            {"id": ids["collection_main"]},
        )
        connection.execute(
            "UPDATE jobs SET workspace_id = workspace_id WHERE id = :id",
            {"id": ids["job_main"]},
        )
        connection.execute(
            "UPDATE voice_samples SET artifact_id = :artifact WHERE id = :id",
            {"artifact": ids["artifact_sample_alt"], "id": ids["sample_main"]},
        )
        connection.execute(
            "UPDATE voice_samples SET voice_id = voice_id WHERE id = :id",
            {"id": ids["sample_main"]},
        )
        connection.execute(
            "UPDATE training_runs SET workspace_id = workspace_id WHERE id = :id",
            {"id": ids["training_main"]},
        )
        connection.execute(
            "UPDATE training_runs SET job_id = NULL, source_artifact_id = NULL, "
            "output_artifact_id = NULL WHERE id = :id",
            {"id": ids["training_main"]},
        )
        connection.execute(
            "UPDATE training_runs SET job_id = :job, output_artifact_id = :artifact WHERE id = :id",
            {
                "job": ids["job_main"],
                "artifact": ids["artifact_training"],
                "id": ids["training_main"],
            },
        )
        connection.execute(
            "UPDATE voice_collection_members SET collection_id = :collection, "
            "managed_voice_id = :voice WHERE collection_id = :old_collection AND voice_key = :key",
            {
                "collection": ids["collection_main_alt"],
                "voice": ids["voice_main_alt"],
                "old_collection": ids["collection_main"],
                "key": "external:provider:member-2",
            },
        )
        connection.execute(
            "UPDATE voice_collection_members SET workspace_id = workspace_id "
            "WHERE collection_id = :collection AND voice_key = :key",
            {
                "collection": ids["collection_main_alt"],
                "key": "external:provider:member-2",
            },
        )
        connection.execute(
            "UPDATE sound_items SET workspace_id = workspace_id WHERE id = :id",
            {"id": ids["item_main"]},
        )
        connection.execute(
            "UPDATE sound_variants SET job_id = :job, artifact_id = :artifact WHERE id = :id",
            {
                "job": ids["job_main"],
                "artifact": ids["artifact_training"],
                "id": ids["variant_main_alt"],
            },
        )
        connection.execute(
            "UPDATE sound_variants SET workspace_id = workspace_id WHERE id = :id",
            {"id": ids["variant_main_alt"]},
        )
        connection.execute(
            "UPDATE sound_items SET preferred_variant_id = :variant WHERE id = :id",
            {"variant": ids["variant_main_alt"], "id": ids["item_main"]},
        )
        connection.execute(
            "UPDATE sound_items SET preferred_variant_id = NULL WHERE id = :id",
            {"id": ids["item_main"]},
        )
        connection.execute(
            "UPDATE sound_items SET preferred_variant_id = :variant WHERE id = :id",
            {"variant": ids["variant_main"], "id": ids["item_main"]},
        )
        connection.execute(
            "UPDATE research_cache_entries SET response_json = '{\"refreshed\":true}' "
            "WHERE cache_key = :key",
            {"key": ids["cache_main"]},
        )
        connection.execute(
            "UPDATE research_cache_entries SET workspace_id = workspace_id, "
            "project_id = project_id WHERE cache_key = :key",
            {"key": ids["cache_main"]},
        )
        connection.execute(
            "INSERT INTO sound_variants (id, workspace_id, item_id, state, provenance_json, created_at) "
            "VALUES ('one-time-attachment', :workspace, NULL, 'ready', '{}', :now)",
            {"workspace": ids["workspace_main"], "now": _NOW},
        )
        connection.execute(
            "UPDATE sound_variants SET item_id = :item WHERE id = 'one-time-attachment'",
            {"item": ids["item_main"]},
        )
        connection.execute(
            "INSERT INTO sound_variants (id, workspace_id, job_id, artifact_id, state, provenance_json, created_at) "
            "VALUES ('mutable-links', :workspace, NULL, NULL, 'queued', '{}', :now)",
            {"workspace": ids["workspace_main"], "now": _NOW},
        )
        connection.execute(
            "UPDATE sound_variants SET job_id = :job, artifact_id = :artifact "
            "WHERE id = 'mutable-links'",
            {"job": ids["job_main"], "artifact": ids["artifact_sound"]},
        )
        connection.execute(
            "INSERT INTO research_cache_entries (cache_key, workspace_id, project_id, provider, "
            "operation, request_json, response_json, created_at, expires_at) "
            "VALUES ('new-workspace-cache', :workspace, NULL, 'fixture', 'search', '{}', '{}', :now, :expires)",
            {
                "workspace": ids["workspace_main"],
                "now": _NOW,
                "expires": "2026-10-25T12:00:00+00:00",
            },
        )
        connection.execute(
            "UPDATE voices SET name = 'Archived voice after upgrade' WHERE id = :id",
            {"id": ids["voice_archived"]},
        )
        connection.commit()

    assert _read_rows(
        path,
        "SELECT artifact_id FROM voice_samples WHERE id = :id",
        {"id": ids["sample_main"]},
    ) == [(ids["artifact_sample_alt"],)]
    assert _read_rows(
        path,
        "SELECT job_id, source_artifact_id, source_text_artifact_id, output_artifact_id "
        "FROM training_runs WHERE id = :id",
        {"id": ids["training_main"]},
    ) == [(ids["job_main"], None, ids["artifact_training"], ids["artifact_training"])]
    assert _read_rows(
        path,
        "SELECT preferred_variant_id FROM sound_items WHERE id = :id",
        {"id": ids["item_main"]},
    ) == [(ids["variant_main"],)]
    assert _foreign_key_violations(path) == []


def test_real_foreign_key_cascades_and_restrictions_preserve_scope(tmp_path: Path) -> None:
    path = tmp_path / "library-cascades.sqlite3"
    ids = _make_valid_predecessor(path)
    command.upgrade(_config(path), TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                "DELETE FROM artifacts WHERE id = :id", {"id": ids["artifact_sample"]}
            )
    assert _read_rows(
        path,
        "SELECT id FROM voice_samples WHERE id = :id",
        {"id": ids["sample_main"]},
    ) == [(ids["sample_main"],)]

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute("DELETE FROM sound_items WHERE id = :id", {"id": ids["item_main"]})
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                "DELETE FROM sound_variants WHERE id = :id",
                {"id": ids["variant_main"]},
            )

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM voices WHERE id = :id", {"id": ids["voice_main"]})
        connection.commit()
    assert _read_rows(path, "SELECT id FROM voice_samples") == []
    assert (
        _read_rows(
            path,
            "SELECT managed_voice_id FROM voice_collection_members "
            "WHERE voice_key = 'opaque:catalogue:voice-key'",
        )
        == []
    )
    assert _read_rows(
        path,
        "SELECT voice_id FROM training_runs WHERE id = :id",
        {"id": ids["training_main"]},
    ) == [(None,)]

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "DELETE FROM voice_collections WHERE id = :id",
            {"id": ids["collection_main"]},
        )
        connection.execute("DELETE FROM jobs WHERE id = :id", {"id": ids["job_main"]})
        connection.execute(
            "DELETE FROM artifacts WHERE id = :id",
            {"id": ids["artifact_training"]},
        )
        connection.execute("DELETE FROM projects WHERE id = :id", {"id": ids["project_main"]})
        connection.commit()

    assert (
        _read_rows(
            path,
            "SELECT collection_id FROM voice_collection_members WHERE voice_key = :key",
            {"key": "external:provider:member-2"},
        )
        == []
    )
    assert _read_rows(
        path,
        "SELECT job_id, source_artifact_id, source_text_artifact_id, output_artifact_id "
        "FROM training_runs WHERE id = :id",
        {"id": ids["training_main"]},
    ) == [(None, None, None, None)]
    assert (
        _read_rows(
            path,
            "SELECT cache_key FROM research_cache_entries WHERE cache_key = :key",
            {"key": ids["cache_main"]},
        )
        == []
    )
    assert _foreign_key_violations(path) == []


@pytest.mark.parametrize(
    ("statement", "parameters", "message"),
    [
        (
            "UPDATE voices SET workspace_id = :other WHERE id = :id",
            {"other": IDS["workspace_other"], "id": IDS["voice_main"]},
            "voice samples have inconsistent roots",
        ),
        (
            "UPDATE voice_samples SET artifact_id = :artifact WHERE id = :id",
            {"artifact": IDS["artifact_other"], "id": IDS["sample_main"]},
            "voice samples have inconsistent roots",
        ),
        (
            "UPDATE training_runs SET job_id = :job WHERE id = :id",
            {"job": IDS["job_other"], "id": IDS["training_main"]},
            "training runs have inconsistent roots",
        ),
        (
            "UPDATE voice_collections SET workspace_id = :other WHERE id = :id",
            {"other": IDS["workspace_other"], "id": IDS["collection_main"]},
            "collection members have inconsistent roots",
        ),
        (
            "UPDATE voice_collection_members SET managed_voice_id = :voice "
            "WHERE collection_id = :collection AND voice_key = :key",
            {
                "voice": IDS["voice_other"],
                "collection": IDS["collection_main"],
                "key": "opaque:catalogue:voice-key",
            },
            "collection members have inconsistent roots",
        ),
        (
            "UPDATE jobs SET workspace_id = :other WHERE id = :id",
            {"other": IDS["workspace_other"], "id": IDS["job_main"]},
            "training runs have inconsistent roots",
        ),
        (
            "UPDATE sound_items SET preferred_variant_id = :variant WHERE id = :id",
            {"variant": IDS["variant_other"], "id": IDS["item_main"]},
            "sound items have inconsistent roots",
        ),
        (
            "UPDATE sound_variants SET artifact_id = :artifact WHERE id = :id",
            {"artifact": IDS["artifact_other"], "id": IDS["variant_main"]},
            "sound variants have inconsistent roots",
        ),
        (
            "UPDATE research_cache_entries SET project_id = :project WHERE cache_key = :key",
            {"project": IDS["project_other"], "key": IDS["cache_main"]},
            "research cache entries have inconsistent roots",
        ),
        (
            "UPDATE voices SET workspace_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["voice_main"]},
            "voices have inconsistent workspace roots",
        ),
        (
            "UPDATE voice_samples SET voice_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["sample_main"]},
            "voice samples have inconsistent roots",
        ),
        (
            "UPDATE training_runs SET job_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["training_main"]},
            "training runs have inconsistent roots",
        ),
        (
            "UPDATE voice_collections SET workspace_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["collection_main"]},
            "voice collections have inconsistent workspace roots",
        ),
        (
            "UPDATE voice_collection_members SET collection_id = :missing "
            "WHERE collection_id = :collection AND voice_key = :key",
            {
                "missing": IDS["missing_parent"],
                "collection": IDS["collection_main"],
                "key": "opaque:catalogue:voice-key",
            },
            "collection members have inconsistent roots",
        ),
        (
            "UPDATE jobs SET workspace_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["job_other"]},
            "jobs have inconsistent workspace roots",
        ),
        (
            "UPDATE sound_items SET preferred_variant_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["item_main"]},
            "sound items have inconsistent roots",
        ),
        (
            "UPDATE sound_variants SET artifact_id = :missing WHERE id = :id",
            {"missing": IDS["missing_parent"], "id": IDS["variant_main"]},
            "sound variants have inconsistent roots",
        ),
        (
            "UPDATE research_cache_entries SET project_id = :missing WHERE cache_key = :key",
            {"missing": IDS["missing_parent"], "key": IDS["cache_main"]},
            "research cache entries have inconsistent roots",
        ),
    ],
)
def test_inconsistent_0077_library_rows_fail_before_ddl(
    tmp_path: Path, statement: str, parameters: _SqlParams, message: str
) -> None:
    path = tmp_path / "invalid-library.sqlite3"
    config = _config(path)
    ids = _make_valid_predecessor(path)
    resolved = {key: ids.get(key, value) for key, value in parameters.items()}
    _update_foreign_keys_off(path, statement, resolved)
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match=re.escape(message)):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_malformed_foreign_key_shape_fails_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "malformed-library-scope.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE voice_samples")
        connection.execute(
            "CREATE TABLE voice_samples ("
            "id VARCHAR(36) PRIMARY KEY NOT NULL, "
            "workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT, "
            "voice_id VARCHAR(36) NOT NULL REFERENCES voices(id) ON DELETE CASCADE, "
            "artifact_id VARCHAR(36) NOT NULL REFERENCES artifacts(id) ON DELETE SET NULL)"
        )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="library ownership foreign key has an unexpected shape"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


@pytest.mark.parametrize("table", ["voice_samples", "sound_variants"])
def test_missing_required_library_table_fails_before_ddl(tmp_path: Path, table: str) -> None:
    path = tmp_path / f"missing-{table}.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(f'DROP TABLE "{table}"')
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="a required table is missing"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


@pytest.mark.parametrize(
    ("name", "sql", "mode"),
    [
        (
            "pandrator_project_graph_projects_update",
            "CREATE TRIGGER pandrator_project_graph_projects_update "
            "BEFORE UPDATE OF workspace_id, source_session_id ON projects "
            "BEGIN SELECT RAISE(ABORT, 'changed'); END",
            "altered",
        ),
        (
            "pandrator_media_scope_artifacts_update",
            "CREATE TRIGGER pandrator_media_scope_artifacts_update "
            "BEFORE UPDATE OF workspace_id, project_id, session_id ON artifacts "
            "BEGIN SELECT RAISE(ABORT, 'changed'); END",
            "altered",
        ),
        ("pandrator_project_graph_projects_update", "", "missing"),
        ("pandrator_media_scope_artifacts_update", "", "missing"),
    ],
)
def test_missing_or_altered_parent_trigger_fails_before_ddl(
    tmp_path: Path, name: str, sql: str, mode: str
) -> None:
    path = tmp_path / f"parent-trigger-{name}.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f'DROP TRIGGER "{name}"')
        if mode == "altered":
            connection.execute(sql)
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="required parent trigger is missing or altered"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_case_insensitive_owned_trigger_collision_fails_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "library-trigger-collision.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER PANDRATOR_LIBRARY_SCOPE_VOICES_UPDATE "
            "AFTER UPDATE OF name ON voices BEGIN SELECT NEW.id; END"
        )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="an owned trigger name is already in use"):
        command.upgrade(config, TARGET_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_trigger_ddl_failure_rolls_back_exact_predecessor_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "library-trigger-ddl-failure.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    before = _snapshot(path)
    execution_count = 0

    def fail_during_owned_trigger_create(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        nonlocal execution_count
        if statement.startswith("CREATE TRIGGER pandrator_library_scope_"):
            execution_count += 1
            if execution_count == 8:
                raise RuntimeError("Injected library trigger DDL failure")

    event.listen(Engine, "before_cursor_execute", fail_during_owned_trigger_create)
    try:
        with pytest.raises(RuntimeError, match="Injected library trigger DDL failure"):
            command.upgrade(config, TARGET_REVISION)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_during_owned_trigger_create)

    assert execution_count == 8
    assert _snapshot(path) == before
    assert _version(path) == PREDECESSOR_REVISION

    command.upgrade(config, TARGET_REVISION)
    assert len(_library_triggers(path)) == 15
    assert _foreign_key_violations(path) == []


@pytest.mark.parametrize("corruption", ["missing", "altered"])
def test_downgrade_checks_all_owned_triggers_before_dropping_any(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / f"{corruption}-library-trigger.sqlite3"
    config = _config(path)
    _make_valid_predecessor(path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER pandrator_library_scope_training_runs_update")
        if corruption == "altered":
            connection.execute(
                "CREATE TRIGGER pandrator_library_scope_training_runs_update "
                "BEFORE UPDATE OF workspace_id ON training_runs "
                "BEGIN SELECT RAISE(ABORT, 'altered trigger'); END"
            )
    before = _snapshot(path)

    with pytest.raises(RuntimeError, match="owned trigger is missing or altered"):
        command.downgrade(config, PREDECESSOR_REVISION)

    assert _snapshot(path) == before
    assert _version(path) == TARGET_REVISION


def _ready_candidate(
    session: Session,
    paths: DataPaths,
    workspace_id: str,
    name: str,
) -> tuple[str, str]:
    artifact_id = f"test-artifact-{name}"
    candidate_id = f"test-candidate-{name}"
    relative_path = f"artifacts/{name}.wav"
    audio_path = paths.managed_path(relative_path)
    audio_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    audio_path.write_bytes(b"tiny synthetic audio")
    artifact = Artifact(
        id=artifact_id,
        workspace_id=workspace_id,
        kind="audio",
        role="sound.original",
        relative_path=relative_path,
        size_bytes=audio_path.stat().st_size,
        content_hash="a" * 64,
        state="current",
    )
    candidate = SoundVariant(
        id=candidate_id,
        workspace_id=workspace_id,
        artifact_id=artifact.id,
        state="ready",
        provenance_json={"source": "synthetic-test"},
    )
    session.add(artifact)
    session.flush([artifact])
    session.add(candidate)
    session.flush([candidate])
    return candidate.id, artifact.id


def test_save_in_session_flushes_item_and_candidate_in_scope_order(tmp_path: Path) -> None:
    database_path = tmp_path / "producer-library-scope.sqlite3"
    ids = _make_valid_predecessor(database_path)
    command.upgrade(_config(database_path), TARGET_REVISION)
    paths = DataPaths.from_value(tmp_path / "managed-data").ensure()
    database = Database(database_path)
    try:
        with database.session() as session:
            new_candidate, _new_artifact = _ready_candidate(
                session, paths, ids["workspace_main"], "new"
            )
            created = save_in_session(
                session,
                paths,
                {"candidate_id": new_candidate, "name": "Created item", "tags": ["wind"]},
                workspace_id=ids["workspace_main"],
            )
            created_item = session.get(SoundItem, created["item_id"])
            created_variant = session.get(SoundVariant, new_candidate)
            assert created_item is not None and created_variant is not None
            assert created_item.revision == 1
            assert created_item.preferred_variant_id == new_candidate
            assert created_variant.item_id == created_item.id

            empty_item = SoundItem(
                workspace_id=ids["workspace_main"],
                name="Empty preference",
                tags_json=[],
                revision=1,
                archived=False,
                preferred_variant_id=None,
            )
            session.add(empty_item)
            session.flush([empty_item])
            empty_candidate, _empty_artifact = _ready_candidate(
                session, paths, ids["workspace_main"], "empty"
            )
            saved_empty = save_in_session(
                session,
                paths,
                {
                    "candidate_id": empty_candidate,
                    "item_id": empty_item.id,
                    "expected_revision": 1,
                },
                workspace_id=ids["workspace_main"],
            )
            assert saved_empty["item_id"] == empty_item.id
            assert empty_item.revision == 2
            assert empty_item.preferred_variant_id == empty_candidate
            empty_candidate_row = session.get(SoundVariant, empty_candidate)
            assert empty_candidate_row is not None
            assert empty_candidate_row.item_id == empty_item.id

            preferred_item = SoundItem(
                workspace_id=ids["workspace_main"],
                name="Existing preference",
                tags_json=[],
                revision=1,
                archived=False,
                preferred_variant_id=None,
            )
            session.add(preferred_item)
            session.flush([preferred_item])
            old_preferred = SoundVariant(
                workspace_id=ids["workspace_main"],
                item_id=preferred_item.id,
                state="ready",
                provenance_json={"source": "existing"},
            )
            session.add(old_preferred)
            session.flush([old_preferred])
            preferred_item.preferred_variant_id = old_preferred.id
            session.flush([preferred_item])
            new_preferred_candidate, _preferred_artifact = _ready_candidate(
                session, paths, ids["workspace_main"], "preserve"
            )
            save_in_session(
                session,
                paths,
                {
                    "candidate_id": new_preferred_candidate,
                    "item_id": preferred_item.id,
                    "expected_revision": 1,
                },
                workspace_id=ids["workspace_main"],
            )
            assert preferred_item.revision == 2
            assert preferred_item.preferred_variant_id == old_preferred.id
            preferred_candidate_row = session.get(SoundVariant, new_preferred_candidate)
            assert preferred_candidate_row is not None
            assert preferred_candidate_row.item_id == preferred_item.id

            stale_candidate, _stale_artifact = _ready_candidate(
                session, paths, ids["workspace_main"], "stale"
            )
            with pytest.raises(RevisionConflict):
                save_in_session(
                    session,
                    paths,
                    {
                        "candidate_id": stale_candidate,
                        "item_id": preferred_item.id,
                        "expected_revision": 1,
                    },
                    workspace_id=ids["workspace_main"],
                )
            assert preferred_item.revision == 2
            assert preferred_item.preferred_variant_id == old_preferred.id
            stale_candidate_row = session.get(SoundVariant, stale_candidate)
            assert stale_candidate_row is not None
            assert stale_candidate_row.item_id is None
    finally:
        database.dispose()
