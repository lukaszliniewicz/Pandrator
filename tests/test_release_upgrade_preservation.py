"""Preserve a synthetic data tree created by the exact published 0.13.0 wheel.

The checked-in SQL is historical output, not a reconstruction of its schema by
current models. metadata.json records its public wheel provenance and row oracle.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths
from pandrator.web.application_services import ApplicationServices
from pandrator.web.database import SCHEMA_HEAD

FIXTURE = Path(__file__).parent / "fixtures" / "release-0.13.0-rich"
WHEEL_SHA256 = "e5794a13bd5736c5e1336a19ddc4d17cf287f97f7c7ef6b85cde89603fb22042"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _rows(connection: sqlite3.Connection, table: str, columns: list[str]) -> list[list[Any]]:
    projection = ", ".join(_quote(column) for column in columns)
    return sorted(
        [list(row) for row in connection.execute(f"SELECT {projection} FROM {_quote(table)}")],
        key=repr,
    )


def _fixture_digest(path: Path) -> str:
    """Hash fixture bytes, tolerating Git's Windows text checkout conversion."""

    data = path.read_bytes()
    if os.name == "nt" and path.suffix in {".txt", ".srt", ".json"}:
        # The oracle records committed LF bytes; a Windows checkout may write
        # CRLF before Pandrator ever sees the tree.
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def test_published_release_tree_survives_normal_startup(tmp_path: Path) -> None:
    metadata: dict[str, Any] = json.loads((FIXTURE / "metadata.json").read_text())
    assert metadata["wheel_sha256"] == WHEEL_SHA256
    assert metadata["schema"] == "0058_project_folders"
    assert len(metadata["tables"]) == 80
    assert sum(len(table["rows"]) for table in metadata["tables"].values()) == 35
    compressed = (FIXTURE / "database.sql.gz").read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == metadata["gzip_sha256"]
    sql = gzip.decompress(compressed)
    assert hashlib.sha256(sql).hexdigest() == metadata["sql_sha256"]
    paths = DataPaths.from_value(tmp_path / "restored-release").ensure()
    shutil.copytree(FIXTURE / "tree", paths.root, dirs_exist_ok=True)
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.executescript(sql.decode())
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            metadata["schema"],
        )
        for table, oracle in metadata["tables"].items():
            assert _rows(connection, table, oracle["columns"]) == oracle["rows"], table
    for relative, digest in metadata["files"].items():
        assert _fixture_digest(paths.root / relative) == digest

    # The normal service constructor owns both migration and startup, without a
    # Flask surface or a worker/provider being started by this qualification.
    services = ApplicationServices.build(data_root=paths.root)
    try:
        with closing(sqlite3.connect(paths.database)) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                SCHEMA_HEAD,
            )
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            owner = connection.execute(
                "SELECT users.id, workspaces.id FROM users JOIN workspaces "
                "ON workspaces.personal_owner_user_id=users.id "
                "WHERE users.legacy_subject='owner' AND workspaces.kind='personal' "
                "AND workspaces.state='active'"
            ).fetchone()
            assert owner is not None
            user_id, workspace_id = owner
            # Every original column of every old table is checked. Added scope
            # columns are checked separately, and 0072's setting relocation is
            # the only intentional change to this fixture's old projections.
            for table, oracle in metadata["tables"].items():
                if table == "app_settings":
                    assert _rows(connection, table, oracle["columns"]) == [
                        row for row in oracle["rows"] if row[0] == "system.identity"
                    ]
                    projection = ", ".join(_quote(column) for column in oracle["columns"])
                    moved = connection.execute(
                        f"SELECT {projection} FROM workspace_settings WHERE workspace_id=?",
                        (workspace_id,),
                    ).fetchall()
                    assert sorted([list(row) for row in moved], key=repr) == [
                        row for row in oracle["rows"] if row[0] != "system.identity"
                    ]
                else:
                    assert _rows(connection, table, oracle["columns"]) == oracle["rows"], table
            old_hash = metadata["tables"]["owner_account"]["rows"][0][1]
            assert old_hash.startswith("$argon2")
            assert connection.execute(
                "SELECT password_hash FROM user_credentials WHERE user_id=?", (user_id,)
            ).fetchone() == (old_hash,)
            assert connection.execute(
                "SELECT workspace_id,secret_value FROM stored_credentials WHERE key=?",
                ("release-rich-provider",),
            ).fetchone() == (workspace_id, "SYNTHETIC-NOT-A-REAL-CREDENTIAL")
            setting = connection.execute(
                "SELECT value_json FROM workspace_settings WHERE workspace_id=? AND key=?",
                (workspace_id, "qualification.release-rich"),
            ).fetchone()
            assert setting is not None
            assert json.loads(setting[0])["credential_ref"] == "db:release-rich-provider"
            ids = metadata["ids"]
            job = connection.execute(
                "SELECT id,status,payload_json,attempts FROM jobs WHERE id=?", (ids["job"],)
            ).fetchone()
            assert job is not None and job[0:2] == (ids["job"], "queued") and job[3] == 0
            assert json.loads(job[2]) == {
                "synthetic": True,
                "message": "preserve queued legacy payload",
            }
            assert connection.execute(
                "SELECT id,is_active,revision FROM audio_takes ORDER BY revision"
            ).fetchall() == [("release-rich-take-v1", 1, 1), ("release-rich-take-v2", 0, 2)]
            assert connection.execute(
                "SELECT COUNT(*) FROM sessions JOIN projects ON projects.id=sessions.project_id "
                "WHERE sessions.id IN (?,?) AND projects.workspace_id=?",
                (ids["audiobook"], ids["subtitle"], workspace_id),
            ).fetchone() == (2,)
            for table, identifier in (
                ("source_assets", ids["source"]),
                ("voices", "release-rich-voice"),
            ):
                assert connection.execute(
                    f"SELECT workspace_id FROM {_quote(table)} WHERE id=?", (identifier,)
                ).fetchone() == (workspace_id,)
        assert services.auth.verify_password(metadata["password"])
        for relative, digest in metadata["files"].items():
            if relative != "migration-web-v1.json":
                assert _fixture_digest(paths.root / relative) == digest
        # This startup marker records the upgrade itself. User media/text bytes
        # stay exact; the old marker hash remains in the historical restore oracle.
        old_marker = json.loads((FIXTURE / "tree" / "migration-web-v1.json").read_text())
        new_marker = json.loads(paths.migration_marker.read_text())
        assert new_marker == {
            **old_marker,
            "web_schema": SCHEMA_HEAD,
            "promoted_generation_segments": 0,
        }
    finally:
        services.close()
