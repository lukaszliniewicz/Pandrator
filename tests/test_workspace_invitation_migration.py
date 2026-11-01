"""Fresh schema and immutable genuine 0082 fixture upgrade/downgrade checks."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from pandrator.web.database import SCHEMA_HEAD
from pandrator.web.models import Base, WorkspaceInvitation

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator/web/migrations"
FIXTURE = ROOT / "tests/fixtures/schema-0082-workspace-invitations.sql.gz"
PREVIOUS = "0082_private_transcription_owners"
TARGET = "0083_workspace_invitations"


def config(path: Path) -> Config:
    result = Config()
    result.set_main_option("script_location", str(MIGRATIONS))
    result.set_main_option("sqlalchemy.url", str(URL.create("sqlite", database=str(path))))
    return result


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def all_old_rows(path: Path) -> dict[str, list[tuple]]:
    with connect(path) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in {"alembic_version", "workspace_invitations"}
        ]
        return {
            table: sorted(
                db.execute('SELECT * FROM "' + table.replace('"', '""') + '"').fetchall(), key=repr
            )
            for table in tables
        }


def invitation_schema(path: Path) -> list[tuple]:
    with connect(path) as db:
        return db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='workspace_invitations' ORDER BY type,name"
        ).fetchall()


def test_fresh_and_genuine_0082_upgrade_match_and_preserve_old_rows(tmp_path: Path) -> None:
    assert FIXTURE.is_file(), "The actual read-only pre-R2 fixture is required."
    original_hash = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    previous = tmp_path / "actual-0082-copy.sqlite3"
    metadata = json.loads(FIXTURE.with_suffix(".json").read_text())
    dump = gzip.decompress(FIXTURE.read_bytes())
    assert original_hash == metadata["gzip_sha256"]
    assert hashlib.sha256(dump).hexdigest() == metadata["sql_sha256"]
    with sqlite3.connect(previous) as db:
        db.executescript(dump.decode("utf-8"))
    with connect(previous) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    before = all_old_rows(previous)
    command.upgrade(config(previous), TARGET)
    assert all_old_rows(previous) == before
    fresh = tmp_path / "fresh.sqlite3"
    command.upgrade(config(fresh), TARGET)
    # This test deliberately stops at the historical invitation revision.
    # The latest migration's tests check its own target/current head parity.
    from alembic.script import ScriptDirectory

    assert ScriptDirectory.from_config(config(fresh)).get_heads() == [SCHEMA_HEAD]
    assert Base.metadata.tables["workspace_invitations"] is WorkspaceInvitation.__table__
    assert invitation_schema(previous) == invitation_schema(fresh)
    for path in (fresh, previous):
        with connect(path) as db:
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET,)
            assert len(db.execute("PRAGMA foreign_key_list(workspace_invitations)").fetchall()) == 3
            assert len(db.execute("PRAGMA index_list(workspace_invitations)").fetchall()) == 3
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == original_hash


def test_empty_solo_downgrade_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "empty-solo.sqlite3"
    command.upgrade(config(path), TARGET)
    before = all_old_rows(path)
    command.downgrade(config(path), PREVIOUS)
    assert all_old_rows(path) == before
    assert invitation_schema(path) == []
    with connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("reason", ["rows", "collaboration"])
def test_downgrade_refuses_invitation_data_or_collaboration(tmp_path: Path, reason: str) -> None:
    path = tmp_path / f"refuse-{reason}.sqlite3"
    command.upgrade(config(path), TARGET)
    with connect(path) as db:
        if reason == "rows":
            user_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
            workspace_id = db.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]
            db.execute(
                """INSERT INTO workspace_invitations
            (id,workspace_id,user_id,invited_by_user_id,inviter_auth_revision,role,state,revision,created_at,expires_at)
            VALUES ('synthetic-invite',?,?,?,1,'member','pending',1,'2030-01-01','2030-01-08')""",
                (workspace_id, user_id, user_id),
            )
        else:
            db.execute(
                """INSERT INTO app_settings (key,value_json,revision,updated_at)
            VALUES ('auth.installation',?,1,'2030-01-01')
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json""",
                (json.dumps({"schema_version": 1, "mode": "collaboration"}),),
            )
    before = all_old_rows(path)
    schema = invitation_schema(path)
    with pytest.raises(RuntimeError, match="Cannot downgrade workspace invitations"):
        command.downgrade(config(path), PREVIOUS)
    assert all_old_rows(path) == before
    assert invitation_schema(path) == schema
    with connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_invitation_storage_constraints_and_restrict_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "constraints.sqlite3"
    command.upgrade(config(path), TARGET)
    with connect(path) as db:
        user_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        workspace_id = db.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]
        statement = """INSERT INTO workspace_invitations
        (id,workspace_id,user_id,invited_by_user_id,inviter_auth_revision,role,state,revision,created_at,expires_at)
        VALUES ('synthetic-invite',?,?,?,?,?,?,?,'2030-01-01','2030-01-08')"""
        for auth_revision, role, state, revision in (
            (0, "member", "pending", 1),
            (1, "owner", "pending", 1),
            (1, "member", "unknown", 1),
            (1, "member", "pending", 0),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    statement,
                    (workspace_id, user_id, user_id, auth_revision, role, state, revision),
                )
        db.execute(statement, (workspace_id, user_id, user_id, 1, "member", "pending", 1))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
