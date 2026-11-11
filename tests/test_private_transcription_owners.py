from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import String
from sqlalchemy.engine import URL, Connection

from pandrator.web.accounts.private_work_models import QuickTranscriptionOwner

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
PREDECESSOR_REVISION = "0081_credential_resource_grants"
TARGET_REVISION = "0082_private_transcription_owners"
OWNER_TABLE = "quick_transcription_owners"
OWNER_INDEXES = {
    "ix_quick_transcription_owners_user_id",
    "ix_quick_transcription_owners_workspace_id",
}
OWNER_TRIGGERS = {
    "quick_transcription_owners_scope_insert",
    "quick_transcription_owners_scope_update",
    "workspaces_private_transcription_scope_update",
    "quick_transcriptions_private_job_update",
    "jobs_private_transcription_scope_update",
}
OWNED_SCHEMA_NAMES = {OWNER_TABLE, *OWNER_INDEXES, *OWNER_TRIGGERS}
NOW = "2026-10-24 12:00:00.000000"
PAST = "2020-01-01 00:00:00.000000"

IDS = {
    "legacy_user": "91000000-0000-4000-8000-000000000001",
    "api_user": "91000000-0000-4000-8000-000000000002",
    "default_api_user": "91000000-0000-4000-8000-000000000003",
    "client_user": "91000000-0000-4000-8000-000000000004",
    "browser_user": "91000000-0000-4000-8000-000000000005",
    "actor_user": "91000000-0000-4000-8000-000000000006",
    "same_user": "91000000-0000-4000-8000-000000000007",
    "inactive_user": "91000000-0000-4000-8000-000000000008",
    "no_workspace_user": "91000000-0000-4000-8000-000000000009",
    "no_membership_user": "91000000-0000-4000-8000-000000000010",
    "ambiguous_user_a": "91000000-0000-4000-8000-000000000011",
    "ambiguous_user_b": "91000000-0000-4000-8000-000000000012",
    "conflict_credential_user": "91000000-0000-4000-8000-000000000013",
    "conflict_actor_user": "91000000-0000-4000-8000-000000000014",
    "guard_user": "91000000-0000-4000-8000-000000000015",
    "legacy_workspace": "92000000-0000-4000-8000-000000000001",
    "api_workspace": "92000000-0000-4000-8000-000000000002",
    "default_api_workspace": "92000000-0000-4000-8000-000000000003",
    "client_workspace": "92000000-0000-4000-8000-000000000004",
    "browser_workspace": "92000000-0000-4000-8000-000000000005",
    "actor_workspace": "92000000-0000-4000-8000-000000000006",
    "same_workspace": "92000000-0000-4000-8000-000000000007",
    "inactive_workspace": "92000000-0000-4000-8000-000000000008",
    "no_membership_workspace": "92000000-0000-4000-8000-000000000009",
    "ambiguous_workspace_a": "92000000-0000-4000-8000-000000000010",
    "ambiguous_workspace_b": "92000000-0000-4000-8000-000000000011",
    "conflict_credential_workspace": "92000000-0000-4000-8000-000000000012",
    "conflict_actor_workspace": "92000000-0000-4000-8000-000000000013",
    "guard_workspace": "92000000-0000-4000-8000-000000000014",
    "team_workspace": "92000000-0000-4000-8000-000000000015",
    "project": "93000000-0000-4000-8000-000000000001",
    "session": "93000000-0000-4000-8000-000000000002",
    "workflow_run": "93000000-0000-4000-8000-000000000003",
    "token_default_subject": "94000000-0000-4000-8000-000000000001",
    "browser_actor": "96000000-0000-4000-8000-000000000001",
    "qt_legacy": "97000000-0000-4000-8000-000000000001",
    "qt_api": "97000000-0000-4000-8000-000000000002",
    "qt_default_api": "97000000-0000-4000-8000-000000000003",
    "qt_client": "97000000-0000-4000-8000-000000000004",
    "qt_browser": "97000000-0000-4000-8000-000000000005",
    "qt_actor": "97000000-0000-4000-8000-000000000006",
    "qt_same_duplicate": "97000000-0000-4000-8000-000000000007",
    "qt_same_alt": "97000000-0000-4000-8000-000000000008",
    "qt_inactive": "97000000-0000-4000-8000-000000000009",
    "qt_unmatched": "97000000-0000-4000-8000-000000000010",
    "qt_blank": "97000000-0000-4000-8000-000000000011",
    "qt_whitespace": "97000000-0000-4000-8000-000000000012",
    "qt_initial_fallback": "97000000-0000-4000-8000-000000000013",
    "qt_no_workspace": "97000000-0000-4000-8000-000000000014",
    "qt_no_membership": "97000000-0000-4000-8000-000000000015",
    "qt_ambiguous": "97000000-0000-4000-8000-000000000016",
    "qt_conflict": "97000000-0000-4000-8000-000000000017",
    "qt_invalid_json": "97000000-0000-4000-8000-000000000018",
    "qt_invalid_version": "97000000-0000-4000-8000-000000000019",
    "qt_invalid_source": "97000000-0000-4000-8000-000000000020",
    "qt_case_mismatch": "97000000-0000-4000-8000-000000000021",
    "qt_trailing_mismatch": "97000000-0000-4000-8000-000000000022",
    "qt_null_identity": "97000000-0000-4000-8000-000000000023",
    "qt_bad_revision": "97000000-0000-4000-8000-000000000024",
    "qt_browser_bad_revision": "97000000-0000-4000-8000-000000000030",
    "qt_job_workspace": "97000000-0000-4000-8000-000000000025",
    "qt_job_kind": "97000000-0000-4000-8000-000000000026",
    "qt_job_project": "97000000-0000-4000-8000-000000000027",
    "qt_job_session": "97000000-0000-4000-8000-000000000028",
    "qt_job_workflow": "97000000-0000-4000-8000-000000000029",
    "job_actor": "98000000-0000-4000-8000-000000000001",
    "job_conflict": "98000000-0000-4000-8000-000000000002",
    "job_invalid_json": "98000000-0000-4000-8000-000000000003",
    "job_invalid_version": "98000000-0000-4000-8000-000000000004",
    "job_invalid_source": "98000000-0000-4000-8000-000000000005",
    "job_mismatch_workspace": "98000000-0000-4000-8000-000000000006",
    "job_mismatch_kind": "98000000-0000-4000-8000-000000000007",
    "job_mismatch_project": "98000000-0000-4000-8000-000000000008",
    "job_mismatch_session": "98000000-0000-4000-8000-000000000009",
    "job_mismatch_workflow": "98000000-0000-4000-8000-000000000010",
    "job_unassigned": "98000000-0000-4000-8000-000000000011",
    "qt_guard_valid": "97000000-0000-4000-8000-000000000101",
    "qt_guard_wrong_workspace": "97000000-0000-4000-8000-000000000102",
    "qt_guard_wrong_kind": "97000000-0000-4000-8000-000000000103",
    "qt_guard_project": "97000000-0000-4000-8000-000000000104",
    "qt_guard_session": "97000000-0000-4000-8000-000000000105",
    "qt_guard_workflow": "97000000-0000-4000-8000-000000000106",
    "qt_guard_delete": "97000000-0000-4000-8000-000000000107",
    "job_guard_wrong_workspace": "98000000-0000-4000-8000-000000000101",
    "job_guard_wrong_kind": "98000000-0000-4000-8000-000000000102",
    "job_guard_project": "98000000-0000-4000-8000-000000000103",
    "job_guard_session": "98000000-0000-4000-8000-000000000104",
    "job_guard_workflow": "98000000-0000-4000-8000-000000000105",
    "job_guard_delete": "98000000-0000-4000-8000-000000000106",
    "browser_bad_revision": "96000000-0000-4000-8000-000000000003",
}


def _config(path: Path) -> Config:
    config = Config()
    # Alembic uses ConfigParser interpolation, so escape SQLAlchemy 2.1 URL %.
    config.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
    config.set_main_option(
        "sqlalchemy.url",
        str(URL.create("sqlite", database=str(path))).replace("%", "%%"),
    )
    return config


def _upgrade(path: Path, revision: str) -> Config:
    config = _config(path)
    command.upgrade(config, revision)
    return config


def _connect(path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    return connection


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _version(path: Path) -> str:
    with _connect(path, foreign_keys=False) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _schema_names(path: Path) -> set[str]:
    with _connect(path, foreign_keys=False) as connection:
        return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}


def _database_state(path: Path, *, include_owned: bool = True) -> tuple[Any, ...]:
    with _connect(path, foreign_keys=False) as connection:
        objects = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents = []
        for table_name in table_names:
            if table_name == "alembic_version":
                continue
            if not include_owned and table_name == OWNER_TABLE:
                continue
            quoted_table = _quote_identifier(table_name)
            rows = tuple(
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {quoted_table} ORDER BY rowid")
            )
            contents.append((table_name, rows))

    if not include_owned:
        objects = tuple(
            row for row in objects if row[1] not in OWNED_SCHEMA_NAMES and row[2] != OWNER_TABLE
        )
    return objects, tuple(contents)


def _owner_rows(path: Path) -> dict[str, tuple[str, str]]:
    with _connect(path) as connection:
        return {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT transcription_id, user_id, workspace_id "
                "FROM quick_transcription_owners ORDER BY transcription_id"
            )
        }


def _insert_user(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    legacy_subject: str | None = None,
    is_active: bool = True,
) -> None:
    connection.execute(
        """INSERT INTO users (
    id, legacy_subject, display_name, is_active, is_instance_admin, created_at, updated_at
) VALUES (?, ?, ?, ?, 0, ?, ?)""",
        (user_id, legacy_subject, f"Synthetic {user_id[-4:]}", int(is_active), NOW, NOW),
    )


def _insert_user_credential(
    connection: sqlite3.Connection,
    user_id: str,
    *,
    login_name: str,
    auth_revision: int,
) -> None:
    connection.execute(
        """INSERT INTO user_credentials (
    user_id, login_name, password_hash, auth_revision, created_at, updated_at
) VALUES (?, ?, NULL, ?, ?, ?)""",
        (user_id, login_name, auth_revision, NOW, NOW),
    )


def _insert_workspace(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    owner_user_id: str | None,
    kind: str = "personal",
    state: str = "active",
) -> None:
    connection.execute(
        """INSERT INTO workspaces (
    id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (workspace_id, kind, f"Synthetic {workspace_id[-4:]}", owner_user_id, state, NOW, NOW),
    )


def _insert_membership(
    connection: sqlite3.Connection,
    workspace_id: str,
    user_id: str,
    *,
    role: str = "owner",
) -> None:
    connection.execute(
        """INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at)
VALUES (?, ?, ?, ?)""",
        (workspace_id, user_id, role, NOW),
    )


def _insert_api_token(
    connection: sqlite3.Connection,
    token_id: str,
    *,
    subject: str | None,
    user_id: str | None,
    user_auth_revision: int | None,
    expired: bool = False,
    revoked: bool = False,
) -> None:
    connection.execute(
        """INSERT INTO api_tokens (
    id, label, token_hash, token_prefix, subject, scopes_json, expires_at,
    principal_kind, created_by, user_id, user_auth_revision, client_id,
    target_instance_id, canonical_origin, created_at, last_used_at, revoked_at
) VALUES (?, ?, ?, ?, ?, '[]', ?, 'api_token', ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
        (
            token_id,
            f"Synthetic {token_id[-4:]}",
            f"hash-{token_id}",
            f"prefix-{token_id[-4:]}",
            subject,
            PAST if expired else "2030-01-01 00:00:00.000000",
            subject or "synthetic-issuer",
            user_id,
            user_auth_revision,
            "instance-00000000000000000000000000000000",
            "https://example.invalid",
            PAST if expired else NOW,
            PAST if expired else NOW,
            PAST if revoked else None,
        ),
    )


def _insert_automation_client(
    connection: sqlite3.Connection,
    client_id: str,
    *,
    subject: str,
    user_id: str | None,
    user_auth_revision: int | None,
    revoked: bool = False,
) -> None:
    connection.execute(
        """INSERT INTO automation_clients (
    id, name, subject, redirect_uris_json, allowed_scopes_json, target_instance_id,
    canonical_origin, created_by, user_id, user_auth_revision, created_at,
    last_used_at, revoked_at
) VALUES (?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            client_id,
            f"Synthetic {client_id[-4:]}",
            subject,
            "instance-00000000000000000000000000000000",
            "https://example.invalid",
            "synthetic-issuer",
            user_id,
            user_auth_revision,
            PAST,
            PAST,
            PAST if revoked else None,
        ),
    )


def _insert_browser_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    subject: str,
    user_id: str,
    auth_revision: int,
    invalid_revision: bool = False,
) -> None:
    connection.execute(
        """INSERT INTO browser_auth_sessions (
    id, user_id, token_hash, auth_revision, subject, principal_kind, scopes_json,
    client_id, created_at, last_seen_at, idle_expires_at, expires_at, revoked_at
) VALUES (?, ?, ?, ?, ?, 'owner_session', '[]', NULL, ?, ?, ?, ?, ?)""",
        (
            session_id,
            user_id,
            f"browser-hash-{session_id}",
            0 if invalid_revision else auth_revision,
            subject,
            PAST,
            PAST,
            PAST,
            PAST,
            PAST,
        ),
    )


def _insert_project(connection: sqlite3.Connection, project_id: str, workspace_id: str) -> None:
    connection.execute(
        """INSERT INTO projects (
    id, workspace_id, source_session_id, name, created_by_user_id, revision,
    created_at, updated_at
) VALUES (?, ?, NULL, ?, NULL, 1, ?, ?)""",
        (project_id, workspace_id, f"Synthetic {project_id[-4:]}", NOW, NOW),
    )


def _insert_session(connection: sqlite3.Connection, session_id: str, project_id: str) -> None:
    connection.execute(
        """INSERT INTO sessions (
    id, project_id, name, storage_key, legacy_name, legacy_path, workflow_kind,
    interface_mode, source_language, target_language, workflow_preset,
    included_stages_json, status, revision, created_at, updated_at, trashed_at, purge_after
) VALUES (?, ?, ?, ?, NULL, NULL, 'audiobook', 'advanced', 'auto', NULL,
          'custom', '[]', 'idle', 1, ?, ?, NULL, NULL)""",
        (session_id, project_id, f"Synthetic {session_id[-4:]}", session_id, NOW, NOW),
    )


def _insert_workflow_run(
    connection: sqlite3.Connection, workflow_run_id: str, session_id: str
) -> None:
    connection.execute(
        """INSERT INTO workflow_runs (
    id, session_id, preset, status, settings_json, created_at, updated_at
) VALUES (?, ?, 'custom', 'pending', '{}', ?, ?)""",
        (workflow_run_id, session_id, NOW, NOW),
    )


def _insert_job(
    connection: sqlite3.Connection,
    job_id: str,
    workspace_id: str,
    *,
    kind: str = "transcription.quick",
    project_id: str | None = None,
    session_id: str | None = None,
    workflow_run_id: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO jobs (
    id, kind, workspace_id, project_id, session_id, workflow_run_id, status,
    payload_json, resource_keys_json, result_json, progress, error_code,
    error_message, lease_owner, lease_expires_at, lease_generation, available_at,
    attempts, max_attempts, created_at, started_at, finished_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, 'queued', '{}', '[]', NULL, 0.0, NULL, NULL,
          NULL, NULL, 0, NULL, 0, 1, ?, NULL, NULL, ?)""",
        (
            job_id,
            kind,
            workspace_id,
            project_id,
            session_id,
            workflow_run_id,
            NOW,
            NOW,
        ),
    )


def _insert_transcription(
    connection: sqlite3.Connection,
    transcription_id: str,
    owner_subject: str,
    *,
    job_id: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO quick_transcriptions (
    id, owner_subject, job_id, source_suffix, size_bytes, sha256, uploaded_bytes,
    next_chunk_index, format, settings_json, state, created_at, expires_at
) VALUES (?, ?, ?, '.wav', 16, ?, 16, 1, 'txt', '{}', 'complete', ?, ?)""",
        (
            transcription_id,
            owner_subject,
            job_id,
            hashlib.sha256(transcription_id.encode()).hexdigest(),
            NOW,
            "2030-01-01 00:00:00.000000",
        ),
    )


def _insert_actor(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    user_id: str,
    subject: str,
    context_json: str | None = None,
    auth_revision: int = 1,
) -> None:
    if context_json is None:
        context_json = json.dumps({"version": 1, "source": "user", "subject": subject})
    connection.execute(
        """INSERT INTO job_actors (
    job_id, user_id, auth_revision, context_json, created_at
) VALUES (?, ?, ?, ?, ?)""",
        (job_id, user_id, auth_revision, context_json, NOW),
    )


def _seed_predecessor(path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    _upgrade(path, PREDECESSOR_REVISION)
    assert not OWNED_SCHEMA_NAMES.intersection(_schema_names(path))

    media_path = path.parent / "private-media" / "source.wav"
    result_path = path.parent / "private-media" / "result.txt"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"\x00synthetic private audio\x7f")
    result_path.write_bytes(b"synthetic private transcription result\n")

    user_subjects = {
        "legacy_user": "legacy-proof",
        "no_workspace_user": None,
        "no_membership_user": None,
    }
    active_states = {"inactive_user": False}
    with _connect(path) as connection:
        for name in (
            "legacy_user",
            "api_user",
            "default_api_user",
            "client_user",
            "browser_user",
            "actor_user",
            "same_user",
            "inactive_user",
            "no_workspace_user",
            "no_membership_user",
            "ambiguous_user_a",
            "ambiguous_user_b",
            "conflict_credential_user",
            "conflict_actor_user",
            "guard_user",
        ):
            _insert_user(
                connection,
                IDS[name],
                legacy_subject=user_subjects.get(name),
                is_active=active_states.get(name, True),
            )

        for user_key, login_name in (
            ("api_user", "api-current"),
            ("default_api_user", "default-api-current"),
            ("client_user", "client-current"),
            ("browser_user", "browser-current"),
            ("actor_user", "actor-current"),
        ):
            _insert_user_credential(
                connection,
                IDS[user_key],
                login_name=login_name,
                auth_revision=1,
            )

        workspace_owners = (
            ("legacy_workspace", "legacy_user", "active"),
            ("api_workspace", "api_user", "active"),
            ("default_api_workspace", "default_api_user", "active"),
            ("client_workspace", "client_user", "active"),
            ("browser_workspace", "browser_user", "active"),
            ("actor_workspace", "actor_user", "active"),
            ("same_workspace", "same_user", "active"),
            ("inactive_workspace", "inactive_user", "archived"),
            ("no_membership_workspace", "no_membership_user", "active"),
            ("ambiguous_workspace_a", "ambiguous_user_a", "active"),
            ("ambiguous_workspace_b", "ambiguous_user_b", "active"),
            (
                "conflict_credential_workspace",
                "conflict_credential_user",
                "active",
            ),
            ("conflict_actor_workspace", "conflict_actor_user", "active"),
            ("guard_workspace", "guard_user", "active"),
        )
        for workspace_key, user_key, state in workspace_owners:
            _insert_workspace(
                connection,
                IDS[workspace_key],
                owner_user_id=IDS[user_key],
                state=state,
            )
            if user_key != "no_membership_user":
                _insert_membership(connection, IDS[workspace_key], IDS[user_key])
        _insert_workspace(
            connection,
            IDS["team_workspace"],
            owner_user_id=None,
            kind="team",
        )

        _insert_project(connection, IDS["project"], IDS["api_workspace"])
        _insert_session(connection, IDS["session"], IDS["project"])
        _insert_workflow_run(connection, IDS["workflow_run"], IDS["session"])

        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000101",
            subject="api-subject",
            user_id=IDS["api_user"],
            user_auth_revision=91,
            expired=True,
            revoked=True,
        )
        _insert_api_token(
            connection,
            IDS["token_default_subject"],
            subject=None,
            user_id=IDS["default_api_user"],
            user_auth_revision=7,
            expired=True,
            revoked=True,
        )
        _insert_automation_client(
            connection,
            "95000000-0000-4000-8000-000000000101",
            subject="client-subject",
            user_id=IDS["client_user"],
            user_auth_revision=4,
            revoked=True,
        )
        _insert_browser_session(
            connection,
            IDS["browser_actor"],
            subject="browser-subject",
            user_id=IDS["browser_user"],
            auth_revision=8,
        )
        _insert_browser_session(
            connection,
            "96000000-0000-4000-8000-000000000002",
            subject="same-subject",
            user_id=IDS["same_user"],
            auth_revision=3,
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints = ON")
        _insert_browser_session(
            connection,
            IDS["browser_bad_revision"],
            subject="browser-bad-revision",
            user_id=IDS["browser_user"],
            auth_revision=0,
            invalid_revision=True,
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints = OFF")

        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000102",
            subject="same-subject",
            user_id=IDS["same_user"],
            user_auth_revision=3,
        )
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000103",
            subject="same-subject-alt",
            user_id=IDS["same_user"],
            user_auth_revision=3,
        )
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000104",
            subject="inactive-subject",
            user_id=IDS["inactive_user"],
            user_auth_revision=1,
            expired=True,
            revoked=True,
        )
        for index, user_key in enumerate(("ambiguous_user_a", "ambiguous_user_b"), 1):
            _insert_api_token(
                connection,
                f"94000000-0000-4000-8000-{index + 200:012d}",
                subject="ambiguous-subject",
                user_id=IDS[user_key],
                user_auth_revision=1,
            )
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000203",
            subject="conflict-subject",
            user_id=IDS["conflict_credential_user"],
            user_auth_revision=1,
        )
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000204",
            subject="no-workspace-subject",
            user_id=IDS["no_workspace_user"],
            user_auth_revision=1,
        )
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000205",
            subject="no-membership-subject",
            user_id=IDS["no_membership_user"],
            user_auth_revision=1,
        )
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000206",
            subject="null-identity-subject",
            user_id=None,
            user_auth_revision=None,
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints = ON")
        _insert_api_token(
            connection,
            "94000000-0000-4000-8000-000000000207",
            subject="bad-revision-subject",
            user_id=IDS["api_user"],
            user_auth_revision=0,
        )
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints = OFF")

        _insert_job(connection, IDS["job_actor"], IDS["actor_workspace"])
        _insert_job(
            connection,
            IDS["job_conflict"],
            IDS["conflict_credential_workspace"],
        )
        _insert_job(connection, IDS["job_invalid_json"], IDS["actor_workspace"])
        _insert_job(connection, IDS["job_invalid_version"], IDS["actor_workspace"])
        _insert_job(connection, IDS["job_invalid_source"], IDS["actor_workspace"])
        _insert_job(
            connection,
            IDS["job_mismatch_workspace"],
            IDS["conflict_actor_workspace"],
        )
        _insert_job(
            connection,
            IDS["job_mismatch_kind"],
            IDS["api_workspace"],
            kind="transcription.other",
        )
        _insert_job(
            connection,
            IDS["job_mismatch_project"],
            IDS["api_workspace"],
            project_id=IDS["project"],
        )
        _insert_job(
            connection,
            IDS["job_mismatch_session"],
            IDS["api_workspace"],
            project_id=IDS["project"],
            session_id=IDS["session"],
        )
        _insert_job(
            connection,
            IDS["job_mismatch_workflow"],
            IDS["api_workspace"],
            project_id=IDS["project"],
            session_id=IDS["session"],
            workflow_run_id=IDS["workflow_run"],
        )
        _insert_job(connection, IDS["job_unassigned"], IDS["api_workspace"])
        _insert_job(connection, IDS["job_guard_delete"], IDS["actor_workspace"])
        _insert_job(
            connection,
            IDS["job_guard_wrong_workspace"],
            IDS["conflict_actor_workspace"],
        )
        _insert_job(
            connection,
            IDS["job_guard_wrong_kind"],
            IDS["api_workspace"],
            kind="transcription.other",
        )
        _insert_job(
            connection,
            IDS["job_guard_project"],
            IDS["api_workspace"],
            project_id=IDS["project"],
        )
        _insert_job(
            connection,
            IDS["job_guard_session"],
            IDS["api_workspace"],
            project_id=IDS["project"],
            session_id=IDS["session"],
        )
        _insert_job(
            connection,
            IDS["job_guard_workflow"],
            IDS["api_workspace"],
            project_id=IDS["project"],
            session_id=IDS["session"],
            workflow_run_id=IDS["workflow_run"],
        )

        _insert_actor(
            connection,
            IDS["job_actor"],
            user_id=IDS["actor_user"],
            subject="actor-subject",
            auth_revision=9,
        )
        _insert_actor(
            connection,
            IDS["job_conflict"],
            user_id=IDS["conflict_actor_user"],
            subject="conflict-subject",
        )
        _insert_actor(
            connection,
            IDS["job_invalid_json"],
            user_id=IDS["actor_user"],
            subject="invalid-json-subject",
            context_json='{"version":1,',
        )
        _insert_actor(
            connection,
            IDS["job_invalid_version"],
            user_id=IDS["actor_user"],
            subject="invalid-version-subject",
            context_json=json.dumps(
                {"version": True, "source": "user", "subject": "invalid-version-subject"}
            ),
        )
        _insert_actor(
            connection,
            IDS["job_invalid_source"],
            user_id=IDS["actor_user"],
            subject="invalid-source-subject",
            context_json=json.dumps(
                {"version": 1, "source": "host", "subject": "invalid-source-subject"}
            ),
        )

        transcriptions = (
            ("qt_legacy", "legacy-proof", None),
            ("qt_api", "api-subject", None),
            (
                "qt_default_api",
                f"api-token:{IDS['token_default_subject']}",
                None,
            ),
            ("qt_client", "client-subject", None),
            ("qt_browser", "browser-subject", None),
            ("qt_actor", "actor-subject", "job_actor"),
            ("qt_same_duplicate", "same-subject", None),
            ("qt_same_alt", "same-subject-alt", None),
            ("qt_inactive", "inactive-subject", None),
            ("qt_unmatched", "unmatched-subject", "job_unassigned"),
            ("qt_blank", "", None),
            ("qt_whitespace", " \t ", None),
            ("qt_initial_fallback", "no-explicit-user-proof", None),
            ("qt_no_workspace", "no-workspace-subject", None),
            ("qt_no_membership", "no-membership-subject", None),
            ("qt_ambiguous", "ambiguous-subject", None),
            ("qt_conflict", "conflict-subject", "job_conflict"),
            ("qt_invalid_json", "invalid-json-subject", "job_invalid_json"),
            ("qt_invalid_version", "invalid-version-subject", "job_invalid_version"),
            ("qt_invalid_source", "invalid-source-subject", "job_invalid_source"),
            ("qt_case_mismatch", "Legacy-Proof", None),
            ("qt_trailing_mismatch", "legacy-proof ", None),
            ("qt_null_identity", "null-identity-subject", None),
            ("qt_bad_revision", "bad-revision-subject", None),
            ("qt_browser_bad_revision", "browser-bad-revision", None),
            ("qt_job_workspace", "api-subject", "job_mismatch_workspace"),
            ("qt_job_kind", "api-subject", "job_mismatch_kind"),
            ("qt_job_project", "api-subject", "job_mismatch_project"),
            ("qt_job_session", "api-subject", "job_mismatch_session"),
            ("qt_job_workflow", "api-subject", "job_mismatch_workflow"),
        )
        for transcription_key, subject, job_key in transcriptions:
            _insert_transcription(
                connection,
                IDS[transcription_key],
                subject,
                job_id=IDS[job_key] if job_key is not None else None,
            )

    return IDS.copy(), {"media": media_path, "result": result_path}


@pytest.fixture(scope="session")
def seeded_predecessor(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, dict[str, str], dict[str, Path]]:
    root = tmp_path_factory.mktemp("private-transcription-predecessor")
    path = root / "at-0081.sqlite3"
    ids, assets = _seed_predecessor(path)
    return path, ids, assets


@pytest.fixture
def predecessor_copy(
    tmp_path: Path,
    seeded_predecessor: tuple[Path, dict[str, str], dict[str, Path]],
) -> tuple[Path, dict[str, str], dict[str, Path]]:
    source_path, ids, source_assets = seeded_predecessor
    path = tmp_path / "predecessor.sqlite3"
    shutil.copy2(source_path, path)
    assets = {}
    for name, source in source_assets.items():
        target = tmp_path / "private-media" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        assets[name] = target
    return path, ids, assets


def _asset_hashes(assets: dict[str, Path]) -> dict[str, str]:
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in assets.items()}


def _expected_backfill(ids: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {
        ids["qt_legacy"]: (ids["legacy_user"], ids["legacy_workspace"]),
        ids["qt_api"]: (ids["api_user"], ids["api_workspace"]),
        ids["qt_default_api"]: (
            ids["default_api_user"],
            ids["default_api_workspace"],
        ),
        ids["qt_client"]: (ids["client_user"], ids["client_workspace"]),
        ids["qt_browser"]: (ids["browser_user"], ids["browser_workspace"]),
        ids["qt_actor"]: (ids["actor_user"], ids["actor_workspace"]),
        ids["qt_same_duplicate"]: (ids["same_user"], ids["same_workspace"]),
        ids["qt_same_alt"]: (ids["same_user"], ids["same_workspace"]),
        ids["qt_inactive"]: (ids["inactive_user"], ids["inactive_workspace"]),
    }


def test_owner_model_is_a_three_column_base_only_leaf() -> None:
    table = QuickTranscriptionOwner.__table__
    assert table.name == OWNER_TABLE
    assert tuple(column.name for column in table.columns) == (
        "transcription_id",
        "user_id",
        "workspace_id",
    )
    assert all(isinstance(column.type, String) for column in table.columns)
    assert {column.type.length for column in table.columns} == {36}
    assert all(not column.nullable for column in table.columns)
    assert tuple(column.name for column in table.primary_key.columns) == ("transcription_id",)
    assert {index.name for index in table.indexes} == OWNER_INDEXES
    assert not QuickTranscriptionOwner.__mapper__.relationships

    expected_foreign_keys = {
        "transcription_id": (
            "fk_quick_transcription_owners_transcription_id",
            "CASCADE",
        ),
        "user_id": ("fk_quick_transcription_owners_user_id", "RESTRICT"),
        "workspace_id": (
            "fk_quick_transcription_owners_workspace_id",
            "RESTRICT",
        ),
    }
    actual_foreign_keys = {
        next(iter(constraint.column_keys)): (
            constraint.name,
            next(iter(constraint.elements)).ondelete,
        )
        for constraint in table.foreign_key_constraints
    }
    assert actual_foreign_keys == expected_foreign_keys


def test_fresh_0081_predecessor_has_no_owned_schema_names(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, _ids, _assets = predecessor_copy
    assert _version(path) == PREDECESSOR_REVISION
    assert OWNED_SCHEMA_NAMES.isdisjoint(_schema_names(path))


def test_upgrade_backfills_only_unambiguous_historical_user_proofs_and_preserves_sources(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, assets = predecessor_copy
    before = _database_state(path)
    asset_hashes = _asset_hashes(assets)
    expected = _expected_backfill(ids)

    _upgrade(path, TARGET_REVISION)

    assert _version(path) == TARGET_REVISION
    assert _owner_rows(path) == expected
    assert _database_state(path, include_owned=False) == before
    assert _asset_hashes(assets) == asset_hashes
    assert OWNED_SCHEMA_NAMES.issubset(_schema_names(path))

    unassigned = set(ids[key] for key in IDS if key.startswith("qt_")) - set(expected)
    assert set(_owner_rows(path)).isdisjoint(unassigned)


def test_owner_table_metadata_indexes_foreign_keys_and_five_literal_guards(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, _ids, _assets = predecessor_copy
    _upgrade(path, TARGET_REVISION)

    with _connect(path) as connection:
        columns = connection.execute(f"PRAGMA table_info({OWNER_TABLE})").fetchall()
        assert tuple(str(row[1]) for row in columns) == (
            "transcription_id",
            "user_id",
            "workspace_id",
        )
        assert all(str(row[2]).upper() == "VARCHAR(36)" for row in columns)
        assert all(bool(row[3]) for row in columns)
        assert tuple(int(row[5]) for row in columns) == (1, 0, 0)
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (OWNER_TABLE,),
        ).fetchone()[0]
        assert all(
            name in table_sql
            for name in (
                "fk_quick_transcription_owners_transcription_id",
                "fk_quick_transcription_owners_user_id",
                "fk_quick_transcription_owners_workspace_id",
            )
        )

        foreign_keys = {
            (
                str(row[3]),
                str(row[2]),
                str(row[4]),
                str(row[6]).upper(),
            )
            for row in connection.execute(f"PRAGMA foreign_key_list({OWNER_TABLE})")
        }
        assert foreign_keys == {
            (
                "transcription_id",
                "quick_transcriptions",
                "id",
                "CASCADE",
            ),
            ("user_id", "users", "id", "RESTRICT"),
            ("workspace_id", "workspaces", "id", "RESTRICT"),
        }

        indexes = connection.execute(f"PRAGMA index_list({OWNER_TABLE})").fetchall()
        assert len(indexes) == 3
        explicit = {str(row[1]): bool(row[2]) for row in indexes if str(row[1]) in OWNER_INDEXES}
        assert explicit == {name: False for name in OWNER_INDEXES}
        trigger_rows = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN (%s)" % ",".join("?" for _ in OWNER_TRIGGERS),
            tuple(OWNER_TRIGGERS),
        ).fetchall()
        assert {str(row[0]) for row in trigger_rows} == OWNER_TRIGGERS
        assert all(row[2] and "RAISE(ABORT" in str(row[2]).upper() for row in trigger_rows)


def test_owner_insert_and_identity_guards_check_private_root_without_blocking_same_values(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, _assets = predecessor_copy
    _upgrade(path, TARGET_REVISION)

    with _connect(path) as connection:
        _insert_transcription(
            connection,
            ids["qt_guard_valid"],
            "synthetic-owner",
            job_id=ids["job_guard_delete"],
        )
        connection.execute(
            "INSERT INTO quick_transcription_owners VALUES (?, ?, ?)",
            (ids["qt_guard_valid"], ids["actor_user"], ids["actor_workspace"]),
        )
        connection.execute(
            "UPDATE quick_transcription_owners SET user_id=user_id, workspace_id=workspace_id "
            "WHERE transcription_id=?",
            (ids["qt_guard_valid"],),
        )

        for column, changed in (
            ("transcription_id", ids["qt_guard_delete"]),
            ("user_id", ids["guard_user"]),
            ("workspace_id", ids["guard_workspace"]),
        ):
            with pytest.raises(
                sqlite3.IntegrityError, match="immutable private transcription owner identity"
            ):
                connection.execute(
                    f"UPDATE quick_transcription_owners SET {column}=? WHERE transcription_id=?",
                    (changed, ids["qt_guard_valid"]),
                )

        for transcription_key in (
            "qt_guard_wrong_workspace",
            "qt_guard_wrong_kind",
            "qt_guard_project",
        ):
            _insert_transcription(connection, ids[transcription_key], "synthetic-owner")

        missing_reference_rows = (
            (
                "99999999-9999-4999-8999-999999999901",
                ids["actor_user"],
                ids["actor_workspace"],
            ),
            (
                ids["qt_guard_wrong_workspace"],
                "99999999-9999-4999-8999-999999999902",
                ids["actor_workspace"],
            ),
            (
                ids["qt_guard_wrong_kind"],
                ids["actor_user"],
                "99999999-9999-4999-8999-999999999903",
            ),
        )
        for row in missing_reference_rows:
            with pytest.raises(sqlite3.IntegrityError) as failure:
                connection.execute("INSERT INTO quick_transcription_owners VALUES (?, ?, ?)", row)
            assert "private transcription owner scope" not in str(failure.value)

        invalid_owner_rows = (
            (ids["qt_guard_wrong_workspace"], ids["actor_user"], ids["team_workspace"]),
            (ids["qt_guard_wrong_kind"], ids["guard_user"], ids["actor_workspace"]),
            (ids["qt_guard_project"], ids["actor_user"], ids["guard_workspace"]),
        )
        for row in invalid_owner_rows:
            with pytest.raises(
                sqlite3.IntegrityError, match="invalid private transcription owner scope"
            ):
                connection.execute("INSERT INTO quick_transcription_owners VALUES (?, ?, ?)", row)

        _insert_transcription(connection, ids["qt_guard_delete"], "synthetic-owner")
        connection.execute(
            "INSERT INTO quick_transcription_owners VALUES (?, ?, ?)",
            (ids["qt_guard_delete"], ids["guard_user"], ids["guard_workspace"]),
        )
        connection.execute("DELETE FROM quick_transcriptions WHERE id=?", (ids["qt_guard_delete"],))
        assert (
            connection.execute(
                "SELECT 1 FROM quick_transcription_owners WHERE transcription_id=?",
                (ids["qt_guard_delete"],),
            ).fetchone()
            is None
        )


def test_wrong_private_job_roots_are_rejected_for_owner_insert_and_job_updates(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, _assets = predecessor_copy
    _upgrade(path, TARGET_REVISION)

    invalid_roots = (
        ("qt_guard_wrong_workspace", "job_guard_wrong_workspace"),
        ("qt_guard_wrong_kind", "job_guard_wrong_kind"),
        ("qt_guard_project", "job_guard_project"),
        ("qt_guard_session", "job_guard_session"),
        ("qt_guard_workflow", "job_guard_workflow"),
    )
    with _connect(path) as connection:
        for transcription_key, job_key in invalid_roots:
            _insert_transcription(
                connection,
                ids[transcription_key],
                "synthetic-owner",
                job_id=ids[job_key],
            )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="invalid private transcription owner scope",
            ):
                connection.execute(
                    "INSERT INTO quick_transcription_owners VALUES (?, ?, ?)",
                    (
                        ids[transcription_key],
                        ids["api_user"],
                        ids["api_workspace"],
                    ),
                )

        _insert_transcription(connection, ids["qt_guard_valid"], "synthetic-owner")
        connection.execute(
            "INSERT INTO quick_transcription_owners VALUES (?, ?, ?)",
            (ids["qt_guard_valid"], ids["api_user"], ids["api_workspace"]),
        )
        for _transcription_key, job_key in invalid_roots:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="invalid private transcription job scope",
            ):
                connection.execute(
                    "UPDATE quick_transcriptions SET job_id=? WHERE id=?",
                    (ids[job_key], ids["qt_guard_valid"]),
                )


def test_job_association_null_deletion_cleanup_and_unassigned_legacy_updates(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, _assets = predecessor_copy
    _upgrade(path, TARGET_REVISION)

    with _connect(path) as connection:
        _insert_transcription(connection, ids["qt_guard_valid"], "synthetic-owner")
        connection.execute(
            "INSERT INTO quick_transcription_owners VALUES (?, ?, ?)",
            (ids["qt_guard_valid"], ids["actor_user"], ids["actor_workspace"]),
        )
        connection.execute(
            "UPDATE quick_transcriptions SET job_id=? WHERE id=?",
            (ids["job_guard_delete"], ids["qt_guard_valid"]),
        )
        connection.execute(
            "UPDATE quick_transcriptions SET job_id=NULL WHERE id=?",
            (ids["qt_guard_valid"],),
        )
        connection.execute(
            "UPDATE quick_transcriptions SET job_id=? WHERE id=?",
            (ids["job_guard_delete"], ids["qt_guard_valid"]),
        )
        connection.execute("DELETE FROM jobs WHERE id=?", (ids["job_guard_delete"],))
        assert (
            connection.execute(
                "SELECT job_id FROM quick_transcriptions WHERE id=?",
                (ids["qt_guard_valid"],),
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM quick_transcription_owners WHERE transcription_id=?",
                (ids["qt_guard_valid"],),
            ).fetchone()
            is not None
        )

        connection.execute(
            "UPDATE jobs SET kind='transcription.other', progress=0.5 WHERE id=?",
            (ids["job_unassigned"],),
        )
        assert (
            connection.execute(
                "SELECT user_id FROM quick_transcription_owners WHERE transcription_id=?",
                (ids["qt_unmatched"],),
            ).fetchone()
            is None
        )


def test_owned_scope_guards_allow_workspace_metadata_and_membership_changes_but_reject_retargeting(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, _assets = predecessor_copy
    _upgrade(path, TARGET_REVISION)

    with _connect(path) as connection:
        connection.execute(
            "UPDATE workspaces SET name='renamed', state='archived', revision=2 WHERE id=?",
            (ids["actor_workspace"],),
        )
        connection.execute(
            "UPDATE workspaces SET kind='personal', personal_owner_user_id=? WHERE id=?",
            (ids["actor_user"], ids["actor_workspace"]),
        )
        connection.execute(
            "UPDATE workspace_memberships SET role='member' WHERE workspace_id=? AND user_id=?",
            (ids["actor_workspace"], ids["actor_user"]),
        )
        connection.execute(
            "DELETE FROM workspace_memberships WHERE workspace_id=? AND user_id=?",
            (ids["actor_workspace"], ids["actor_user"]),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid private transcription workspace scope",
        ):
            connection.execute(
                "UPDATE workspaces SET personal_owner_user_id=? WHERE id=?",
                (ids["guard_user"], ids["actor_workspace"]),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid private transcription workspace scope",
        ):
            connection.execute(
                "UPDATE workspaces SET kind='team', personal_owner_user_id=NULL WHERE id=?",
                (ids["actor_workspace"],),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM workspaces WHERE id=?", (ids["actor_workspace"],))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM users WHERE id=?", (ids["actor_user"],))


def test_owned_job_shape_is_immutable_while_progress_and_queue_updates_remain_legal(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, _assets = predecessor_copy
    _upgrade(path, TARGET_REVISION)

    invalid_updates = (
        ("workspace_id", ids["guard_workspace"]),
        ("kind", "transcription.other"),
        ("project_id", ids["project"]),
        ("session_id", ids["session"]),
        ("workflow_run_id", ids["workflow_run"]),
    )
    with _connect(path) as connection:
        _insert_transcription(
            connection,
            ids["qt_guard_valid"],
            "synthetic-owner",
            job_id=ids["job_guard_delete"],
        )
        connection.execute(
            "INSERT INTO quick_transcription_owners VALUES (?, ?, ?)",
            (ids["qt_guard_valid"], ids["actor_user"], ids["actor_workspace"]),
        )
        connection.execute(
            "UPDATE jobs SET status='running', progress=0.5, lease_owner='worker', "
            "lease_generation=1 WHERE id=?",
            (ids["job_guard_delete"],),
        )
        for column, value in invalid_updates:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="invalid private transcription job scope",
            ):
                connection.execute(
                    f"UPDATE jobs SET {column}=? WHERE id=?",
                    (value, ids["job_guard_delete"]),
                )


@pytest.mark.parametrize(
    "corruption",
    ["missing_column", "case_insensitive_name_collision", "foreign_key_violation"],
)
def test_upgrade_preflight_failures_leave_predecessor_state_and_version_unchanged(
    tmp_path: Path,
    seeded_predecessor: tuple[Path, dict[str, str], dict[str, Path]],
    corruption: str,
) -> None:
    source_path, _ids, _assets = seeded_predecessor
    path = tmp_path / f"preflight-{corruption}.sqlite3"
    shutil.copy2(source_path, path)
    with _connect(path, foreign_keys=False) as connection:
        if corruption == "missing_column":
            connection.execute(
                "ALTER TABLE api_tokens RENAME COLUMN user_auth_revision TO old_revision"
            )
        elif corruption == "case_insensitive_name_collision":
            connection.execute('CREATE TABLE "QUICK_TRANSCRIPTION_OWNERS" (reserved TEXT)')
        else:
            connection.execute(
                "UPDATE quick_transcriptions SET job_id=? WHERE id=?",
                (
                    "99999999-9999-4999-8999-999999999999",
                    IDS["qt_unmatched"],
                ),
            )
    before = _database_state(path)

    with pytest.raises(RuntimeError, match="Cannot create private transcription owners"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _database_state(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_mid_ddl_failure_rolls_back_owned_schema_and_revision(
    tmp_path: Path,
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.operations.base import Operations

    source_path, _ids, _assets = predecessor_copy
    path = tmp_path / "ddl-rollback.sqlite3"
    shutil.copy2(source_path, path)
    before = _database_state(path)
    original_create_index = Operations.create_index

    def fail_after_first_index(
        operations: Operations,
        index_name: str,
        table_name: str,
        columns: Any,
        **kwargs: Any,
    ) -> None:
        if index_name == "ix_quick_transcription_owners_workspace_id":
            raise RuntimeError("injected private transcription DDL failure")
        original_create_index(operations, index_name, table_name, columns, **kwargs)

    monkeypatch.setattr(Operations, "create_index", fail_after_first_index)
    with pytest.raises(RuntimeError, match="injected private transcription DDL failure"):
        command.upgrade(_config(path), TARGET_REVISION)

    assert _database_state(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_mid_backfill_failure_rolls_back_owned_rows_schema_and_revision(
    tmp_path: Path,
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path, _ids, _assets = predecessor_copy
    path = tmp_path / "backfill-rollback.sqlite3"
    shutil.copy2(source_path, path)
    before = _database_state(path)
    original_exec_driver_sql = Connection.exec_driver_sql
    inserted = 0

    def fail_after_one_owner(
        connection: Connection,
        statement: str,
        parameters: Any = (),
        execution_options: Any = None,
    ) -> Any:
        nonlocal inserted
        if statement.lstrip().startswith("INSERT INTO quick_transcription_owners"):
            inserted += 1
            if inserted == 2:
                raise RuntimeError("injected private transcription backfill failure")
        return original_exec_driver_sql(
            connection,
            statement,
            parameters,
            execution_options=execution_options,
        )

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_after_one_owner)
    with pytest.raises(
        RuntimeError,
        match="injected private transcription backfill failure",
    ):
        command.upgrade(_config(path), TARGET_REVISION)

    assert inserted == 2
    assert _database_state(path) == before
    assert _version(path) == PREDECESSOR_REVISION


def test_downgrade_upgrade_round_trip_preserves_legacy_rows_files_and_owner_mapping(
    predecessor_copy: tuple[Path, dict[str, str], dict[str, Path]],
) -> None:
    path, ids, assets = predecessor_copy
    predecessor = _database_state(path)
    predecessor_sources = _database_state(path, include_owned=False)
    asset_hashes = _asset_hashes(assets)
    expected = _expected_backfill(ids)
    config = _upgrade(path, TARGET_REVISION)
    upgraded = _database_state(path)
    assert _owner_rows(path) == expected

    command.downgrade(config, PREDECESSOR_REVISION)
    assert _version(path) == PREDECESSOR_REVISION
    assert _database_state(path) == predecessor

    command.upgrade(config, TARGET_REVISION)
    assert _version(path) == TARGET_REVISION
    assert _owner_rows(path) == expected
    assert _database_state(path, include_owned=False) == predecessor_sources
    assert _database_state(path) == upgraded
    assert _asset_hashes(assets) == asset_hashes


@pytest.mark.parametrize(
    "drift",
    [
        "trigger",
        "index",
        "column",
        "foreign_key",
        "trigger_literal_case",
        "trigger_literal_space",
        "generated_column",
        "foreign_key_on_update",
        "foreign_key_deferrable",
    ],
)
def test_downgrade_drift_refusal_is_atomic(
    tmp_path: Path,
    drift: str,
) -> None:
    path = tmp_path / f"downgrade-drift-{drift}.sqlite3"
    _upgrade(path, PREDECESSOR_REVISION)
    config = _upgrade(path, TARGET_REVISION)
    with _connect(path, foreign_keys=False) as connection:
        if drift == "trigger":
            connection.execute("DROP TRIGGER quick_transcription_owners_scope_update")
        elif drift == "index":
            connection.execute("DROP INDEX ix_quick_transcription_owners_user_id")
        elif drift == "column":
            connection.execute("ALTER TABLE quick_transcription_owners ADD COLUMN unexpected TEXT")
        elif drift in {"trigger_literal_case", "trigger_literal_space"}:
            name = "quick_transcription_owners_scope_insert"
            original = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()[0]
            changed = original.replace(
                "'transcription.quick'" if drift == "trigger_literal_case" else "'personal'",
                "'TRANSCRIPTION.QUICK'" if drift == "trigger_literal_case" else "'per sonal'",
            )
            assert changed != original
            connection.execute(f"DROP TRIGGER {name}")
            connection.execute(changed)
        elif drift == "generated_column":
            connection.execute(
                "ALTER TABLE quick_transcription_owners ADD COLUMN unexpected TEXT "
                "GENERATED ALWAYS AS (user_id || workspace_id) VIRTUAL"
            )
        elif drift in {"foreign_key_on_update", "foreign_key_deferrable"}:
            objects = connection.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
                "AND sql IS NOT NULL ORDER BY type,name",
                (OWNER_TABLE,),
            ).fetchall()
            original = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (OWNER_TABLE,),
            ).fetchone()[0]
            suffix = (
                " ON UPDATE CASCADE"
                if drift == "foreign_key_on_update"
                else " DEFERRABLE INITIALLY DEFERRED"
            )
            changed = original.replace("ON DELETE CASCADE", "ON DELETE CASCADE" + suffix)
            assert changed != original
            connection.execute("DROP TABLE quick_transcription_owners")
            connection.execute(changed)
            for row in objects:
                connection.execute(row[0])
        else:
            connection.execute("DROP TABLE quick_transcription_owners")
            connection.execute(
                """CREATE TABLE quick_transcription_owners (
    transcription_id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    workspace_id VARCHAR(36) NOT NULL
)"""
            )
            connection.execute(
                "CREATE INDEX ix_quick_transcription_owners_user_id "
                "ON quick_transcription_owners (user_id)"
            )
            connection.execute(
                "CREATE INDEX ix_quick_transcription_owners_workspace_id "
                "ON quick_transcription_owners (workspace_id)"
            )
    before = _database_state(path)

    with pytest.raises(RuntimeError, match="Cannot downgrade private transcription owners"):
        command.downgrade(config, PREDECESSOR_REVISION)

    assert _database_state(path) == before
    assert _version(path) == TARGET_REVISION


def test_actor_only_identity_proof_is_local_to_its_transcription(predecessor_copy):
    path, ids, _assets = predecessor_copy
    unproved_id = "97000000-0000-4000-8000-000000009901"
    with _connect(path) as connection:
        _insert_transcription(connection, unproved_id, "actor-subject")
    before = _database_state(path)
    _upgrade(path, TARGET_REVISION)
    owners = _owner_rows(path)
    assert owners[ids["qt_actor"]] == (ids["actor_user"], ids["actor_workspace"])
    assert unproved_id not in owners
    assert _database_state(path, include_owned=False) == before


def test_distinct_actor_proofs_for_same_subject_do_not_contaminate_each_other(predecessor_copy):
    path, ids, _assets = predecessor_copy
    second_id = "97000000-0000-4000-8000-000000009902"
    second_job = "98000000-0000-4000-8000-000000009902"
    with _connect(path) as connection:
        _insert_job(connection, second_job, ids["guard_workspace"])
        _insert_actor(connection, second_job, user_id=ids["guard_user"], subject="actor-subject")
        _insert_transcription(connection, second_id, "actor-subject", job_id=second_job)
    before = _database_state(path)
    _upgrade(path, TARGET_REVISION)
    owners = _owner_rows(path)
    assert owners[ids["qt_actor"]] == (ids["actor_user"], ids["actor_workspace"])
    assert owners[second_id] == (ids["guard_user"], ids["guard_workspace"])
    assert _database_state(path, include_owned=False) == before
