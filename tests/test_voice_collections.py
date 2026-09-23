from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from pandrator.web.database import SCHEMA_HEAD, Database, sqlite_url, upgrade_database
from pandrator.web.models import Voice
from pandrator.web.voice_catalog_schemas import (
    VoiceCollectionCreate,
    VoiceCollectionUpdate,
    VoiceReference,
)
from pandrator.web.voice_collections import (
    create_collection,
    list_collections,
    update_collection,
)
from pandrator.web.workspace import RevisionConflict


def _database(tmp_path: Path) -> Database:
    database_path = tmp_path / "pandrator.sqlite3"
    upgrade_database(database_path)
    return Database(database_path)


def _managed_voice(session, name: str = "Narrator") -> Voice:
    voice = Voice(name=name)
    session.add(voice)
    session.flush()
    return voice


def test_multiple_collections_share_a_managed_voice_without_duplicate_members(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        voice = _managed_voice(session)
        first = create_collection(session, VoiceCollectionCreate(name="First"))
        second = create_collection(session, VoiceCollectionCreate(name="Second"))
        reference = VoiceReference(kind="managed", voice_id=voice.id)
        first = update_collection(
            session,
            first["id"],
            VoiceCollectionUpdate(
                expected_revision=1, add_members=[reference, reference]
            ),
        )
        second = update_collection(
            session,
            second["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[reference]),
        )

    assert first["member_count"] == 1
    assert second["member_count"] == 1
    with database.session() as session:
        assert [item["member_count"] for item in list_collections(session)] == [1, 1]


def test_provider_reference_roundtrip_and_idempotent_revision(tmp_path: Path) -> None:
    database = _database(tmp_path)
    provider = VoiceReference(
        kind="provider", service_id="svc", model="model", voice="en-US"
    )
    with database.session() as session:
        collection = create_collection(session, VoiceCollectionCreate(name="Provider"))
        first = update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[provider]),
        )
        second = update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=2, add_members=[provider]),
        )
    assert first["revision"] == 2
    assert second["revision"] == 2
    assert second["members"][0]["reference"] == provider.model_dump(exclude_none=True)


def test_same_reference_cannot_be_added_and_removed_in_one_update() -> None:
    reference = VoiceReference(
        kind="provider", service_id="svc", model="model", voice="en-US"
    )
    with pytest.raises(ValidationError):
        VoiceCollectionUpdate(
            expected_revision=1,
            add_members=[reference],
            remove_members=[reference],
        )


def test_managed_delete_cascades_members_and_missing_managed_is_rejected(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        voice = _managed_voice(session)
        collection = create_collection(session, VoiceCollectionCreate(name="Cascade"))
        reference = VoiceReference(kind="managed", voice_id=voice.id)
        update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[reference]),
        )
        session.delete(voice)

    with database.session() as session:
        assert list_collections(session)[0]["member_count"] == 0
        with pytest.raises(ValueError, match="does not exist"):
            update_collection(
                session,
                collection["id"],
                VoiceCollectionUpdate(
                    expected_revision=2,
                    add_members=[reference],
                ),
            )


def test_revision_conflict_and_description_omission_or_null(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        collection = create_collection(
            session,
            VoiceCollectionCreate(name="Descriptions", description="Keep this"),
        )
        preserved = update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1),
        )
        assert preserved["revision"] == 1
        assert preserved["description"] == "Keep this"
        cleared = update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1, description=None),
        )
        assert cleared["revision"] == 2
        assert cleared["description"] is None
        with pytest.raises(RevisionConflict):
            update_collection(
                session,
                collection["id"],
                VoiceCollectionUpdate(expected_revision=1),
            )


def test_duplicate_normalized_collection_name_and_missing_collection(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        collection = create_collection(
            session, VoiceCollectionCreate(name=" My voices ")
        )
        with pytest.raises(ValueError, match="already exists"):
            create_collection(session, VoiceCollectionCreate(name="my voices"))
        with pytest.raises(KeyError):
            update_collection(
                session,
                "missing",
                VoiceCollectionUpdate(expected_revision=1),
            )
        assert collection["name"] == "My voices"


def test_migration_preserves_existing_voice_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    upgrade_database(database_path)
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    command.downgrade(config, "0047_performance_plans")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO voices (id, name, metadata_json, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("legacy-voice", "Legacy voice", "{}", 3),
        )
        connection.commit()

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone() == (SCHEMA_HEAD,)
        assert connection.execute(
            "SELECT name, revision FROM voices WHERE id = ?", ("legacy-voice",)
        ).fetchone() == ("Legacy voice", 3)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "voice_collections",
        "voice_collection_members",
        "voice_catalog_overrides",
    } <= tables
