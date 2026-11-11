from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from pandrator.web.database import Database, sqlite_url, upgrade_database
from pandrator.web.models import (
    Voice,
    VoiceCollection,
    VoiceCollectionMember,
    Workspace,
)
from pandrator.web.voice_catalog_schemas import (
    VoiceCollectionCreate,
    VoiceCollectionUpdate,
    VoiceReference,
    canonical_voice_key,
)
from pandrator.web.voice_collections import (
    create_collection,
    list_collections,
    update_collection,
)
from pandrator.web.voice_identity import voice_creation_workspace

ACTIVE_A = "collection-workspace-a"
ACTIVE_B = "collection-workspace-b"
ARCHIVED = "collection-workspace-archived"


def _use_legacy_schema(database: Database, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database.path))
    command.downgrade(config, revision)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database_path = tmp_path / "voice-collections.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    try:
        yield database
    finally:
        database.engine.dispose()


def _workspace(
    session: Session,
    workspace_id: str,
    *,
    state: str = "active",
) -> Workspace:
    workspace = Workspace(
        id=workspace_id,
        kind="team",
        name=workspace_id,
        state=state,
    )
    session.add(workspace)
    session.flush()
    return workspace


def _managed_voice(
    session: Session,
    workspace_id: str,
    voice_id: str,
) -> Voice:
    voice = Voice(
        id=voice_id,
        workspace_id=workspace_id,
        name=f"Voice {voice_id}",
    )
    session.add(voice)
    session.flush()
    return voice


def _provider_reference(voice: str) -> VoiceReference:
    return VoiceReference(
        kind="provider",
        service_id="test-service",
        model="test-model",
        voice=voice,
    )


def _utc_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _member(session: Session, collection_id: str, voice_key: str) -> VoiceCollectionMember:
    member = session.get(VoiceCollectionMember, (collection_id, voice_key))
    assert member is not None
    return member


def test_same_names_are_isolated_and_omitted_scope_uses_personal_workspace(
    database: Database,
) -> None:
    provider = _provider_reference("Narrator A")
    with database.session() as session:
        personal_workspace_id = voice_creation_workspace(session).id
        _workspace(session, ACTIVE_A)
        _workspace(session, ACTIVE_B)

        personal = create_collection(
            session,
            VoiceCollectionCreate(name="Shared name"),
        )
        first = create_collection(
            session,
            VoiceCollectionCreate(name="Shared name"),
            workspace_id=ACTIVE_A,
        )
        second = create_collection(
            session,
            VoiceCollectionCreate(name="Shared name"),
            workspace_id=ACTIVE_B,
        )
        first = update_collection(
            session,
            first["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[provider]),
            workspace_id=ACTIVE_A,
        )

        assert first["members"] == [
            {
                "key": canonical_voice_key(provider),
                "reference": provider.model_dump(exclude_none=True),
            }
        ]
        assert [item["id"] for item in list_collections(session)] == [personal["id"]]
        assert [item["id"] for item in list_collections(session, workspace_id=ACTIVE_A)] == [
            first["id"]
        ]
        assert [item["id"] for item in list_collections(session, workspace_id=ACTIVE_B)] == [
            second["id"]
        ]

        stored_personal = session.get(VoiceCollection, personal["id"])
        stored_first = session.get(VoiceCollection, first["id"])
        stored_second = session.get(VoiceCollection, second["id"])
        assert stored_personal is not None and stored_personal.workspace_id == personal_workspace_id
        assert stored_first is not None and stored_first.workspace_id == ACTIVE_A
        assert stored_second is not None and stored_second.workspace_id == ACTIVE_B


def test_foreign_collection_ids_and_managed_voice_references_are_rejected(
    database: Database,
) -> None:
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        _workspace(session, ACTIVE_B)
        voice_a = _managed_voice(session, ACTIVE_A, "voice-owned-by-a")
        collection_a = create_collection(
            session,
            VoiceCollectionCreate(name="A"),
            workspace_id=ACTIVE_A,
        )
        collection_b = create_collection(
            session,
            VoiceCollectionCreate(name="B"),
            workspace_id=ACTIVE_B,
        )

        with pytest.raises(KeyError):
            update_collection(
                session,
                collection_a["id"],
                VoiceCollectionUpdate(expected_revision=1, name="Stolen"),
                workspace_id=ACTIVE_B,
            )
        with pytest.raises(ValueError, match="does not exist"):
            update_collection(
                session,
                collection_b["id"],
                VoiceCollectionUpdate(
                    expected_revision=1,
                    add_members=[VoiceReference(kind="managed", voice_id=voice_a.id)],
                ),
                workspace_id=ACTIVE_B,
            )
        assert list_collections(session, workspace_id=ACTIVE_B)[0]["id"] == collection_b["id"]


def test_member_order_and_duplicate_add_keep_no_op_revision_semantics(
    database: Database,
) -> None:
    alpha = _provider_reference("Alpha")
    zed = _provider_reference("Zed")
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        collection = create_collection(
            session,
            VoiceCollectionCreate(name="Ordered"),
            workspace_id=ACTIVE_A,
        )
        first = update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[zed, alpha]),
            workspace_id=ACTIVE_A,
        )
        second = update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=2, add_members=[alpha, zed]),
            workspace_id=ACTIVE_A,
        )

        assert first["revision"] == 2
        assert [member["key"] for member in first["members"]] == sorted(
            [canonical_voice_key(alpha), canonical_voice_key(zed)]
        )
        assert second["revision"] == 2
        assert _utc_timestamp(second["updated_at"]) == _utc_timestamp(first["updated_at"])
        assert second["members"] == first["members"]


def test_failed_update_can_be_caught_and_committed_without_partial_changes(
    database: Database,
) -> None:
    retained_reference = _provider_reference("Existing")
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        _workspace(session, ACTIVE_B)
        foreign_voice = _managed_voice(session, ACTIVE_B, "voice-owned-by-b")
        collection = create_collection(
            session,
            VoiceCollectionCreate(name="Before", description="Original"),
            workspace_id=ACTIVE_A,
        )
        update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[retained_reference]),
            workspace_id=ACTIVE_A,
        )

    with database.session() as session:
        with pytest.raises(ValueError, match="does not exist"):
            update_collection(
                session,
                collection["id"],
                VoiceCollectionUpdate(
                    expected_revision=2,
                    name="After",
                    description="Changed",
                    add_members=[VoiceReference(kind="managed", voice_id=foreign_voice.id)],
                ),
                workspace_id=ACTIVE_A,
            )

    with database.session() as session:
        persisted = session.get(VoiceCollection, collection["id"])
        assert persisted is not None
        assert (persisted.name, persisted.description, persisted.revision) == (
            "Before",
            "Original",
            2,
        )
        rows = session.scalars(
            select(VoiceCollectionMember).where(
                VoiceCollectionMember.collection_id == collection["id"]
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].voice_key == canonical_voice_key(retained_reference)
        assert rows[0].voice_ref_json == retained_reference.model_dump(exclude_none=True)
        assert rows[0].workspace_id == ACTIVE_A


@pytest.mark.parametrize(
    "corruption",
    ["workspace", "key", "reference", "managed_id", "linked_voice"],
)
def test_list_rejects_corrupt_member_after_valid_member_before_projection(
    database: Database,
    corruption: str,
) -> None:
    if corruption == "workspace":
        _use_legacy_schema(database, "0074_actor_attribution")
    elif corruption == "linked_voice":
        _use_legacy_schema(database, "0077_media_scope_consistency")
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        _workspace(session, ACTIVE_B)
        first_voice = _managed_voice(session, ACTIVE_A, "voice-a")
        second_voice = _managed_voice(session, ACTIVE_A, "voice-z")
        collection = create_collection(
            session,
            VoiceCollectionCreate(name="Corrupt"),
            workspace_id=ACTIVE_A,
        )
        update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(
                expected_revision=1,
                add_members=[
                    VoiceReference(kind="managed", voice_id=first_voice.id),
                    VoiceReference(kind="managed", voice_id=second_voice.id),
                ],
            ),
            workspace_id=ACTIVE_A,
        )

        bad_member = _member(session, collection["id"], "managed:voice-z")
        if corruption == "workspace":
            bad_member.workspace_id = None
        elif corruption == "key":
            bad_member.voice_key = "managed:wrong"
        elif corruption == "reference":
            bad_member.voice_ref_json = {"kind": "invalid"}
        elif corruption == "managed_id":
            bad_member.managed_voice_id = first_voice.id
        elif corruption == "linked_voice":
            second_voice.workspace_id = ACTIVE_B
        session.flush()

        with pytest.raises(ValueError):
            list_collections(session, workspace_id=ACTIVE_A)


def test_update_rejects_corrupt_existing_member_before_renaming(database: Database) -> None:
    _use_legacy_schema(database, "0077_media_scope_consistency")
    provider = _provider_reference("Stored")
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        _workspace(session, ACTIVE_B)
        collection = create_collection(
            session,
            VoiceCollectionCreate(name="Before"),
            workspace_id=ACTIVE_A,
        )
        update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(expected_revision=1, add_members=[provider]),
            workspace_id=ACTIVE_A,
        )
        _member(session, collection["id"], canonical_voice_key(provider)).workspace_id = ACTIVE_B

    with database.session() as session:
        with pytest.raises(ValueError, match="workspace link"):
            update_collection(
                session,
                collection["id"],
                VoiceCollectionUpdate(expected_revision=2, name="After"),
                workspace_id=ACTIVE_A,
            )

    with database.session() as session:
        persisted = session.get(VoiceCollection, collection["id"])
        assert persisted is not None
        assert (persisted.name, persisted.revision) == ("Before", 2)


def test_list_uses_three_queries_for_many_collections_and_all_members(
    database: Database,
) -> None:
    collection_count = 64
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        collections = [
            VoiceCollection(
                workspace_id=ACTIVE_A,
                name=f"Collection {index:03d}",
            )
            for index in range(collection_count)
        ]
        session.add_all(collections)
        session.flush()
        members = [
            VoiceCollectionMember(
                collection_id=collection.id,
                workspace_id=ACTIVE_A,
                voice_key=canonical_voice_key(reference),
                voice_ref_json=reference.model_dump(exclude_none=True),
            )
            for index, collection in enumerate(collections)
            for reference in [_provider_reference(f"Voice {index:03d}")]
        ]
        session.add_all(members)
        session.flush()

    statements: list[str] = []

    def record_statement(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        with database.session() as session:
            listed = list_collections(session, workspace_id=ACTIVE_A)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    assert len(listed) == collection_count
    assert all(item["member_count"] == 1 for item in listed)
    assert len(statements) == 3


def test_missing_or_archived_workspace_fails_and_same_owner_voice_delete_cascades(
    database: Database,
) -> None:
    with database.session() as session:
        _workspace(session, ACTIVE_A)
        _workspace(session, ARCHIVED, state="archived")
        voice = _managed_voice(session, ACTIVE_A, "voice-to-delete")
        collection = create_collection(
            session,
            VoiceCollectionCreate(name="Cascade"),
            workspace_id=ACTIVE_A,
        )
        update_collection(
            session,
            collection["id"],
            VoiceCollectionUpdate(
                expected_revision=1,
                add_members=[VoiceReference(kind="managed", voice_id=voice.id)],
            ),
            workspace_id=ACTIVE_A,
        )
        with pytest.raises(ValueError, match="missing workspace"):
            list_collections(session, workspace_id="collection-workspace-missing")
        with pytest.raises(ValueError, match="inactive"):
            create_collection(
                session,
                VoiceCollectionCreate(name="Archived"),
                workspace_id=ARCHIVED,
            )
        session.delete(voice)

    with database.session() as session:
        listed = list_collections(session, workspace_id=ACTIVE_A)
        assert len(listed) == 1
        assert listed[0]["id"] == collection["id"]
        assert listed[0]["member_count"] == 0


def test_concurrent_new_collection_does_not_break_a_list_already_reading_roots(
    database: Database,
) -> None:
    with database.immediate_session() as session:
        _workspace(session, ACTIVE_A)
        original = create_collection(
            session, VoiceCollectionCreate(name="Original"), workspace_id=ACTIVE_A
        )

    created = False

    def create_after_root_read(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal created
        if created or "FROM voice_collections" not in statement:
            return
        created = True
        with database.immediate_session() as writer:
            added = create_collection(
                writer, VoiceCollectionCreate(name="Added concurrently"), workspace_id=ACTIVE_A
            )
            update_collection(
                writer,
                added["id"],
                VoiceCollectionUpdate(
                    expected_revision=1, add_members=[_provider_reference("New member")]
                ),
                workspace_id=ACTIVE_A,
            )

    event.listen(database.engine, "after_cursor_execute", create_after_root_read)
    try:
        with database.session() as session:
            listed = list_collections(session, workspace_id=ACTIVE_A)
    finally:
        event.remove(database.engine, "after_cursor_execute", create_after_root_read)
    assert created
    assert [item["id"] for item in listed] == [original["id"]]
    with database.session() as session:
        assert len(list_collections(session, workspace_id=ACTIVE_A)) == 2
