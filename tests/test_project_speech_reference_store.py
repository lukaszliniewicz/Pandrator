from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Table, event, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from pandrator.web import project_speech_references as speech_reference_store
from pandrator.web import speech_inputs
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.database import SCHEMA_HEAD, Database
from pandrator.web.model_base import Base, new_id
from pandrator.web.models import (
    AppSetting,
    Artifact,
    ArtifactEdge,
    SessionRecord,
    Workspace,
)
from pandrator.web.project_speech_references import (
    clear_project_speech_reference,
    get_project_speech_reference,
    normalize_speech_reference_binding,
    normalize_speech_reference_slot,
    put_project_speech_reference,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.projects.speech_reference_models import ProjectSpeechReference
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_input_contracts import (
    SpeechInputError,
    SpeechInputOwner,
    canonical_json,
)
from tests.web_test_support import prepare_web_test_data_root

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator/web/migrations"
PREVIOUS = "0084_job_external_effects"
TARGET = "0085_project_speech_references"
_BINDING = {
    "adapter": "audio_cpp",
    "model": "model-one",
    "voice": "voice-one",
    "display_name": "Voice One",
}
_SELECTOR = {"voice": "Narrator", "service": "audio_cpp", "model": "model-one"}


def _migration_config(path: Path) -> Config:
    config = Config()
    # Config.set_main_option goes through ConfigParser interpolation, so a
    # literal "%" must be escaped. SQLAlchemy renders Windows sqlite paths
    # percent-encoded (C%3A%5C...), which would otherwise raise ValueError.
    config.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
    config.set_main_option(
        "sqlalchemy.url",
        str(URL.create("sqlite", database=str(path))).replace("%", "%%"),
    )
    return config


def _sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _snapshot_existing_schema_and_rows(path: Path) -> tuple[object, ...]:
    with _sqlite(path) as connection:
        tables = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('alembic_version', 'project_speech_references')"
            )
        )
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name != 'alembic_version' AND tbl_name != 'alembic_version' "
                "AND name != 'project_speech_references' "
                "AND tbl_name != 'project_speech_references' "
                "ORDER BY type,name"
            )
        )
        rows = tuple(
            (
                table,
                tuple(
                    sorted(
                        (tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')),
                        key=repr,
                    )
                ),
            )
            for table in tables
        )
        foreign_keys = tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
                ),
            )
            for table in tables
        )
    return schema, rows, foreign_keys


def _new_database(path: Path, revision: str = TARGET) -> None:
    command.upgrade(_migration_config(path), revision)


def test_migration_config_accepts_percent_encoded_windows_database_urls(
    tmp_path: Path,
) -> None:
    # SQLAlchemy renders Windows sqlite paths percent-encoded (C%3A%5C...) and
    # ConfigParser rejects a raw "%". The escaped config must hand Alembic the
    # original URL and still open the requested file, not a mangled name.
    path = tmp_path / "percent%3Aencoded.sqlite3"
    url = str(URL.create("sqlite", database=str(path)))
    assert "%" in url
    config = _migration_config(path)
    assert config.get_main_option("sqlalchemy.url") == url
    _new_database(path)
    assert make_url(config.get_main_option("sqlalchemy.url")).database == str(path)
    with _sqlite(path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == TARGET


@dataclass(frozen=True)
class RootedOwner:
    workspace_id: str
    project_id: str
    session_id: str
    speech_owner: SpeechInputOwner
    pin: dict[str, object]


def _add_root(session: Session, name: str) -> RootedOwner:
    workspace = Workspace(kind="team", name=f"{name} workspace", state="active")
    session.add(workspace)
    session.flush()
    record = SessionRecord(id=new_id(), name=f"{name} session")
    project = create_session_project(
        session, record, workspace_id=workspace.id, legacy=True
    )
    speech_owner = SpeechInputOwner(
        workspace_id=workspace.id,
        project_id=project.id,
        session_id=record.id,
    )
    audio_id, snapshot_id = new_id(), new_id()
    content_hash = hashlib.sha256(f"{name} retained snapshot".encode()).hexdigest()
    session.add_all(
        [
            Artifact(
                id=audio_id,
                workspace_id=speech_owner.workspace_id,
                project_id=speech_owner.project_id,
                session_id=speech_owner.session_id,
                kind="audio",
                role="speech_reference_audio",
                relative_path=f"synthetic/{name}/audio.wav",
                mime_type="audio/wav",
                size_bytes=16,
                content_hash=hashlib.sha256(f"{name} audio".encode()).hexdigest(),
                state="current",
            ),
            Artifact(
                id=snapshot_id,
                workspace_id=speech_owner.workspace_id,
                project_id=speech_owner.project_id,
                session_id=speech_owner.session_id,
                kind="speech_voice_snapshot",
                role="speech_input",
                relative_path=f"synthetic/{name}/snapshot.json",
                mime_type="application/json",
                size_bytes=32,
                content_hash=content_hash,
                state="current",
            ),
        ]
    )
    session.flush()
    session.add(
        ArtifactEdge(
            parent_artifact_id=audio_id,
            child_artifact_id=snapshot_id,
            relation="speech_reference",
        )
    )
    session.flush()
    return RootedOwner(
        workspace_id=workspace.id,
        project_id=project.id,
        session_id=record.id,
        speech_owner=speech_owner,
        pin={"version": 1, "artifact_id": snapshot_id, "content_hash": content_hash},
    )


@dataclass(frozen=True)
class StoreHarness:
    database: Database
    owner: RootedOwner
    pin: dict[str, object]
    other_owner: RootedOwner
    other_pin: dict[str, object]


@pytest.fixture

def store_harness(tmp_path: Path) -> Iterator[StoreHarness]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.session() as session:
        owner = _add_root(session, "primary")
        other_owner = _add_root(session, "other")
    try:
        yield StoreHarness(database, owner, owner.pin, other_owner, other_owner.pin)
    finally:
        database.dispose()


def test_slot_selector_and_selector_hash_contract() -> None:
    canonical_cast = "cast:" + canonical_json(["characters", "Jean Éluard"])
    segment = str(uuid4())
    assert normalize_speech_reference_slot("tts") == "tts"
    assert normalize_speech_reference_slot("cast:narrator") == "cast:narrator"
    assert normalize_speech_reference_slot(canonical_cast) == canonical_cast
    assert normalize_speech_reference_slot(f"segment:{segment}") == f"segment:{segment}"
    source_speaker_slot = "cast:" + canonical_json(["source_speakers", "s" * 160])
    assert normalize_speech_reference_slot(source_speaker_slot) == source_speaker_slot

    invalid_slots = (
        " TTS",
        "cast:[]",
        'cast:["characters", "Cast member"]',
        'cast:["voices","member"]',
        'cast:["characters"," member"]',
        'cast:["characters","member\\u007f"]',
        "cast:" + canonical_json(["characters", "x" * 161]),
        f"segment:{segment.upper()}",
        "segment:relative-path",
        "session.json",
        "x" * 513,
    )
    for slot in invalid_slots:
        with pytest.raises(ValueError):
            normalize_speech_reference_slot(slot)

    binding = {"voice": " Narrator ", "service": " audio_cpp ", "model": " model-one "}
    selector = speech_reference_selector(binding)
    assert selector == {
        "voice": "Narrator",
        "voice_id": None,
        "service": "audio_cpp",
        "model": "model-one",
        "voice_description": "",
    }
    assert selector["voice"] == "Narrator"
    assert selector["service"] == "audio_cpp"
    assert selector["model"] == "model-one"
    from pandrator.logic.speech_performance import content_hash

    assert speech_reference_selector_hash(binding) == content_hash(selector)
    with pytest.raises(ValueError):
        speech_reference_selector({"voice": "Narrator", "credential": "secret"})
    with pytest.raises(ValueError):
        speech_reference_selector({"voice": "", "voice_id": None})


def test_public_binding_normalizer_is_strict_and_finite() -> None:
    assert normalize_speech_reference_binding(
        {
            "adapter": " audio_cpp ",
            "model": " model-one ",
            "voice": " voice-one ",
            "display_name": " Voice One ",
        }
    ) == _BINDING
    for value in (
        {**_BINDING, "credential": "secret"},
        {key: item for key, item in _BINDING.items() if key != "display_name"},
        {**_BINDING, "adapter": "remote"},
        {**_BINDING, "model": " "},
        {**_BINDING, "model": "m" * 256},
        {**_BINDING, "voice": "v" * 513},
        {**_BINDING, "display_name": "d" * 256},
    ):
        with pytest.raises(ValueError):
            normalize_speech_reference_binding(value)


def test_fresh_0084_defers_table_and_0085_creates_it(tmp_path: Path) -> None:
    path = tmp_path / "fresh-schema.sqlite3"
    command.upgrade(_migration_config(path), PREVIOUS)
    with _sqlite(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == PREVIOUS
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='project_speech_references'"
        ).fetchone() is None

    command.upgrade(_migration_config(path), TARGET)
    with _sqlite(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='project_speech_references'"
        ).fetchone() is not None
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_upgrade_preserves_existing_rows_triggers_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "preserve-existing.sqlite3"
    command.upgrade(_migration_config(path), PREVIOUS)
    with _sqlite(path) as connection:
        connection.execute(
            "INSERT INTO app_settings(key,value_json,revision,updated_at) "
            "VALUES('packet.preservation','{}',1,'2026-10-26T00:00:00Z')"
        )
        connection.execute(
            "CREATE TABLE packet_preservation_probe (event TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER packet_preservation_trigger AFTER UPDATE ON app_settings "
            "WHEN NEW.key='packet.preservation' BEGIN "
            "INSERT INTO packet_preservation_probe(event) VALUES ('updated'); END"
        )
    before = _snapshot_existing_schema_and_rows(path)

    command.upgrade(_migration_config(path), TARGET)
    assert _snapshot_existing_schema_and_rows(path) == before
    with _sqlite(path) as connection:
        connection.execute(
            "UPDATE app_settings SET value_json='{\"changed\":true}' "
            "WHERE key='packet.preservation'"
        )
        assert [
            row[0]
            for row in connection.execute(
                "SELECT event FROM packet_preservation_probe"
            ).fetchall()
        ] == ["updated"]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET


def test_migration_matches_model_columns_indexes_checks_and_foreign_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "metadata-parity.sqlite3"
    _new_database(path)
    model = cast(Table, ProjectSpeechReference.__table__)
    with _sqlite(path) as connection:
        table_info = connection.execute(
            'PRAGMA table_info("project_speech_references")'
        ).fetchall()
        assert [row["name"] for row in table_info] == [column.name for column in model.columns]
        assert {row["name"] for row in table_info if row["notnull"]} == {
            column.name for column in model.columns if not column.nullable
        }
        primary_key = tuple(
            row["name"] for row in sorted(table_info, key=lambda row: row["pk"]) if row["pk"]
        )
        assert primary_key == tuple(column.name for column in model.primary_key.columns)

        actual_fks = {
            (row["from"], row["table"], row["to"], row["on_delete"].upper())
            for row in connection.execute(
                'PRAGMA foreign_key_list("project_speech_references")'
            )
        }
        expected_fks = {
            (
                foreign_key.parent.name,
                foreign_key.column.table.name,
                foreign_key.column.name,
                (foreign_key.ondelete or "NO ACTION").upper(),
            )
            for foreign_key in model.foreign_keys
        }
        assert actual_fks == expected_fks

        actual_indexes = {
            row["name"]: tuple(
                index_row["name"]
                for index_row in connection.execute(
                    f'PRAGMA index_info("{row["name"]}")'
                )
            )
            for row in connection.execute(
                'PRAGMA index_list("project_speech_references")'
            )
            if not row["unique"]
        }
        expected_indexes = {
            index.name: tuple(column.name for column in index.columns)
            for index in model.indexes
        }
        assert actual_indexes == expected_indexes

        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='project_speech_references'"
        ).fetchone()[0]
        assert create_sql is not None
        checks = [
            constraint
            for constraint in model.constraints
            if isinstance(constraint, CheckConstraint)
        ]
        assert len(checks) == 4
        for constraint in checks:
            assert constraint.name in create_sql
            assert " ".join(str(constraint.sqltext).lower().split()) in " ".join(
                create_sql.lower().split()
            )
    assert Base.metadata.tables["project_speech_references"] is model
    assert ScriptDirectory.from_config(_migration_config(path)).get_heads() == [SCHEMA_HEAD]


def test_unsafe_downgrade_retains_reference_and_empty_downgrade_works(
    tmp_path: Path,
) -> None:
    paths = prepare_web_test_data_root(tmp_path / "with-row")
    database = Database(paths.database)
    with database.session() as session:
        owner = _add_root(session, "downgrade")
        stored = put_project_speech_reference(
            session,
            owner.session_id,
            "tts",
            selector=_SELECTOR,
            binding=_BINDING,
            pin=None,
            expected_revision=0,
            authorization_guard=lambda _session: None,
        )
        assert stored.revision == 1
    database.dispose()
    before = _snapshot_existing_schema_and_rows(paths.database)
    with pytest.raises(RuntimeError, match="Cannot discard saved project speech references"):
        command.downgrade(_migration_config(paths.database), PREVIOUS)
    assert _snapshot_existing_schema_and_rows(paths.database) == before
    with _sqlite(paths.database) as connection:
        # The aborted downgrade leaves the head revision untouched.
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == SCHEMA_HEAD

    empty_path = tmp_path / "empty-downgrade.sqlite3"
    _new_database(empty_path)
    before_empty = _snapshot_existing_schema_and_rows(empty_path)
    command.downgrade(_migration_config(empty_path), PREVIOUS)
    assert _snapshot_existing_schema_and_rows(empty_path) == before_empty
    with _sqlite(empty_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == PREVIOUS
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='project_speech_references'"
        ).fetchone() is None
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_guard_is_first_and_denial_does_not_autoflush(
    store_harness: StoreHarness,
) -> None:
    h = store_harness
    for operation in ("get", "put", "clear"):
        with h.database.session() as session:
            pending = AppSetting(
                key=f"denied-{operation}", value_json={"pending": True}, revision=1
            )
            session.add(pending)
            events: list[str] = []

            def capture(
                _conn, _cursor, statement, _parameters, _context, _many, _events=events
            ) -> None:
                _events.append("sql:" + statement)

            event.listen(h.database.engine, "before_cursor_execute", capture)
            try:
                def deny(_session: Session, _events=events) -> None:
                    _events.append("guard")
                    raise AccessDenied()

                with pytest.raises(AccessDenied):
                    if operation == "get":
                        get_project_speech_reference(
                            session,
                            h.owner.session_id,
                            "tts",
                            authorization_guard=deny,
                        )
                    elif operation == "put":
                        put_project_speech_reference(
                            session,
                            h.owner.session_id,
                            "tts",
                            selector=_SELECTOR,
                            binding=_BINDING,
                            pin=None,
                            expected_revision=0,
                            authorization_guard=deny,
                        )
                    else:
                        clear_project_speech_reference(
                            session,
                            h.owner.session_id,
                            "tts",
                            expected_revision=0,
                            authorization_guard=deny,
                        )
                assert events == ["guard"]
                assert pending in session.new
                session.rollback()
            finally:
                event.remove(h.database.engine, "before_cursor_execute", capture)


def test_guard_precedes_root_and_reference_queries_and_missing_roots_deny(
    store_harness: StoreHarness,
) -> None:
    h = store_harness
    events: list[tuple[str, str]] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        if statement.lower().strip() not in {"begin", "begin immediate"}:
            events.append(("sql", statement.lower()))

    event.listen(h.database.engine, "before_cursor_execute", capture)
    try:
        with h.database.snapshot_session() as session:
            def allow(_session: Session) -> None:
                events.append(("guard", ""))

            assert get_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                authorization_guard=allow,
            ) is None
            assert events[0] == ("guard", "")
            assert len([event for event in events if event[0] == "sql"]) == 2
            assert "sessions" in events[1][1]
            assert "project_speech_references" in events[2][1]

            events.clear()
            with pytest.raises(AccessDenied):
                get_project_speech_reference(
                    session,
                    new_id(),
                    "tts",
                    authorization_guard=allow,
                )
            assert events[0] == ("guard", "")
            sql = [statement for kind, statement in events if kind == "sql"]
            assert len(sql) == 1
            assert "sessions" in sql[0]
            assert "project_speech_references" not in sql[0]
    finally:
        event.remove(h.database.engine, "before_cursor_execute", capture)


def test_guard_exception_propagates_and_inactive_roots_are_denied(
    store_harness: StoreHarness,
) -> None:
    h = store_harness
    marker = RuntimeError("guard marker")
    with h.database.snapshot_session() as session:
        with pytest.raises(RuntimeError, match="guard marker"):
            get_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                authorization_guard=lambda _session: (_ for _ in ()).throw(marker),
            )

    with h.database.session() as session:
        workspace = session.get(Workspace, h.owner.workspace_id)
        assert workspace is not None
        workspace.state = "archived"
    with h.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            get_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                authorization_guard=lambda _session: None,
            )

    with h.database.session() as session:
        workspace = session.get(Workspace, h.owner.workspace_id)
        record = session.get(SessionRecord, h.owner.session_id)
        assert workspace is not None and record is not None
        workspace.state = "active"
        record.trashed_at = datetime.now(UTC)
    with h.database.snapshot_session() as session:
        with pytest.raises(AccessDenied):
            get_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                authorization_guard=lambda _session: None,
            )


def test_strict_binding_and_pin_values_reject_without_writing(
    store_harness: StoreHarness,
) -> None:
    h = store_harness
    invalid_bindings = (
        {**_BINDING, "secret": "credential"},
        {key: value for key, value in _BINDING.items() if key != "display_name"},
        {**_BINDING, "adapter": "other"},
        {**_BINDING, "model": " "},
        {**_BINDING, "model": "m" * 256},
        {**_BINDING, "voice": "v" * 513},
        {**_BINDING, "display_name": "d" * 256},
    )
    for binding in invalid_bindings:
        slot_key = f"segment:{uuid4()}"
        with h.database.session() as session:
            with pytest.raises(ValueError):
                put_project_speech_reference(
                    session,
                    h.owner.session_id,
                    slot_key,
                    selector=_SELECTOR,
                    binding=binding,
                    pin=None,
                    expected_revision=0,
                    authorization_guard=lambda _session: None,
                )
        with h.database.snapshot_session() as session:
            assert get_project_speech_reference(
                session,
                h.owner.session_id,
                slot_key,
                authorization_guard=lambda _session: None,
            ) is None

    for bad_pin in (
        {"version": True, "artifact_id": h.pin["artifact_id"], "content_hash": h.pin["content_hash"]},
        {"version": 1, "artifact_id": "not-a-uuid", "content_hash": h.pin["content_hash"]},
        {"version": 1, "artifact_id": h.pin["artifact_id"], "content_hash": "x" * 64},
        {"version": 1, "artifact_id": h.pin["artifact_id"], "content_hash": h.pin["content_hash"], "path": "secret.wav"},
    ):
        with h.database.session() as session:
            with pytest.raises(SpeechInputError):
                put_project_speech_reference(
                    session,
                    h.owner.session_id,
                    "tts",
                    selector=_SELECTOR,
                    binding=_BINDING,
                    pin=bad_pin,
                    expected_revision=0,
                    authorization_guard=lambda _session: None,
                )
    with h.database.snapshot_session() as session:
        assert get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        ) is None

    for bad_revision in (True, -1, 0.0):
        with h.database.session() as session:
            with pytest.raises(ValueError):
                put_project_speech_reference(
                    session,
                    h.owner.session_id,
                    "tts",
                    selector=_SELECTOR,
                    binding=_BINDING,
                    pin=None,
                    expected_revision=cast(int, bad_revision),
                    authorization_guard=lambda _session: None,
                )


def test_valid_graph_saves_but_foreign_owner_and_wrong_hash_fail_without_io(
    store_harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = store_harness
    file_reads: list[str] = []
    forbidden_queries: list[str] = []

    def forbid_file_read(*_args: object, **_kwargs: object) -> None:
        file_reads.append("read")
        raise AssertionError("The metadata store must not read retained files.")

    def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        lowered = statement.lower()
        if "voice_samples" in lowered or " from voices" in lowered:
            forbidden_queries.append(statement)

    monkeypatch.setattr(speech_inputs, "read_verified_file", forbid_file_read)
    event.listen(h.database.engine, "before_cursor_execute", capture)
    try:
        with h.database.session() as session:
            stored = put_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                selector=_SELECTOR,
                binding=_BINDING,
                pin=h.pin,
                expected_revision=0,
                authorization_guard=lambda _session: None,
            )
            assert stored.pin == h.pin
            assert stored.binding == _BINDING
        for pin in (
            h.other_pin,
            {
                "version": 1,
                "artifact_id": h.pin["artifact_id"],
                "content_hash": "0" * 64,
            },
        ):
            slot_key = f"segment:{uuid4()}"
            with h.database.session() as session:
                with pytest.raises(SpeechInputError):
                    put_project_speech_reference(
                        session,
                        h.owner.session_id,
                        slot_key,
                        selector=_SELECTOR,
                        binding={
                            "adapter": " audio_cpp ",
                            "model": " model-one ",
                            "voice": " voice-one ",
                            "display_name": " Voice One ",
                        },
                        pin=pin,
                        expected_revision=0,
                        authorization_guard=lambda _session: None,
                    )
            with h.database.snapshot_session() as session:
                assert get_project_speech_reference(
                    session,
                    h.owner.session_id,
                    slot_key,
                    authorization_guard=lambda _session: None,
                ) is None
    finally:
        event.remove(h.database.engine, "before_cursor_execute", capture)
    assert file_reads == []
    assert forbidden_queries == []


def test_get_is_metadata_only_and_cas_clear_preserves_retained_artifacts(
    store_harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = store_harness
    with h.database.session() as session:
        inserted = put_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            selector=_SELECTOR,
            binding=_BINDING,
            pin=h.pin,
            expected_revision=0,
            authorization_guard=lambda _session: None,
        )
        assert inserted.revision == 1
    with h.database.session() as session:
        edge = session.scalar(
            select(ArtifactEdge).where(
                ArtifactEdge.child_artifact_id == h.pin["artifact_id"]
            )
        )
        assert edge is not None
        session.delete(edge)
    with h.database.snapshot_session() as session:
        detached = get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        )
        assert detached is not None and detached.pin == h.pin
        with pytest.raises(SpeechInputError):
            speech_inputs.check_speech_input(
                session, h.pin, owner=h.owner.speech_owner
            )

    with h.database.snapshot_session() as session:
        artifact_count_before = len(session.scalars(select(Artifact.id)).all())
        saved = get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        )
        assert saved is not None
        original_row = session.get(
            ProjectSpeechReference, (h.owner.session_id, "tts")
        )
        assert original_row is not None
        original_created_at = original_row.created_at
        original_updated_at = original_row.updated_at
        saved.selector["voice"] = "caller mutation"
        saved.binding["voice"] = "caller mutation"
        assert saved.pin is not None
        saved.pin["artifact_id"] = new_id()

    with h.database.snapshot_session() as session:
        fresh = get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        )
        assert fresh is not None
        assert fresh.selector["voice"] == "Narrator"
        assert fresh.binding["voice"] == "voice-one"
        assert fresh.pin == h.pin

    with h.database.session() as session:
        with pytest.raises(RevisionConflict):
            put_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                selector={"voice": "Other"},
                binding=_BINDING,
                pin=None,
                expected_revision=0,
                authorization_guard=lambda _session: None,
            )
        session.rollback()

    fixed_update_time = datetime.now(UTC) + timedelta(days=1)
    monkeypatch.setattr(
        speech_reference_store, "utcnow", lambda: fixed_update_time
    )
    with h.database.session() as session:
        updated = put_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            selector={"voice": "Other", "service": "audio_cpp", "model": "model-two"},
            binding={**_BINDING, "model": "model-two"},
            pin=None,
            expected_revision=1,
            authorization_guard=lambda _session: None,
        )
        assert updated.revision == 2
        assert updated.pin is None
    with h.database.snapshot_session() as session:
        row = session.get(ProjectSpeechReference, (h.owner.session_id, "tts"))
        assert row is not None
        assert row.created_at == original_created_at
        updated_at = row.updated_at
        assert updated_at.replace(tzinfo=UTC) == fixed_update_time
        assert updated_at > original_updated_at
        assert row.input_artifact_id is None and row.input_content_hash is None

    with h.database.session() as session:
        with pytest.raises(RevisionConflict):
            put_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                selector={"voice": "stale"},
                binding={**_BINDING, "voice": "stale"},
                pin=None,
                expected_revision=1,
                authorization_guard=lambda _session: None,
            )
        session.rollback()
    with h.database.snapshot_session() as session:
        unchanged = get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        )
        assert unchanged is not None and unchanged.revision == 2
        assert unchanged.selector["voice"] == "Other"
        assert unchanged.binding["model"] == "model-two"
        assert unchanged.pin is None

    with h.database.session() as session:
        with pytest.raises(RevisionConflict):
            clear_project_speech_reference(
                session,
                h.owner.session_id,
                "tts",
                expected_revision=1,
                authorization_guard=lambda _session: None,
            )
    with h.database.snapshot_session() as session:
        still_saved = get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        )
        assert still_saved is not None and still_saved.revision == 2
        current_row = session.get(
            ProjectSpeechReference, (h.owner.session_id, "tts")
        )
        assert current_row is not None
        assert current_row.updated_at == updated_at

    with h.database.session() as session:
        assert clear_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            expected_revision=2,
            authorization_guard=lambda _session: None,
        )
    with h.database.session() as session:
        assert not clear_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            expected_revision=0,
            authorization_guard=lambda _session: None,
        )
    with h.database.snapshot_session() as session:
        assert get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        ) is None
        assert len(session.scalars(select(Artifact.id)).all()) == artifact_count_before
        assert session.get(Artifact, h.pin["artifact_id"]) is not None


def test_caller_rollback_undoes_reference_update(store_harness: StoreHarness) -> None:
    h = store_harness
    with h.database.session() as session:
        put_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            selector=_SELECTOR,
            binding=_BINDING,
            pin=h.pin,
            expected_revision=0,
            authorization_guard=lambda _session: None,
        )

    writer = Session(bind=h.database.engine, expire_on_commit=False, future=True)
    try:
        updated = put_project_speech_reference(
            writer,
            h.owner.session_id,
            "tts",
            selector={"voice": "Uncommitted", "service": "audio_cpp"},
            binding={**_BINDING, "voice": "uncommitted"},
            pin=None,
            expected_revision=1,
            authorization_guard=lambda _session: None,
        )
        assert updated.revision == 2
        writer.rollback()
    finally:
        writer.close()

    with h.database.snapshot_session() as session:
        stored = get_project_speech_reference(
            session,
            h.owner.session_id,
            "tts",
            authorization_guard=lambda _session: None,
        )
        assert stored is not None
        assert stored.revision == 1
        assert stored.selector == speech_reference_selector(_SELECTOR)
        assert stored.pin == h.pin
