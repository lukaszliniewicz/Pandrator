"""Forward-only SQLite migrations for manager-owned state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


def migration_0001(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE components (
            component_id TEXT PRIMARY KEY,
            desired_json TEXT,
            inspection_json TEXT NOT NULL,
            installed_version TEXT,
            installed_revision TEXT,
            updated_at REAL NOT NULL
        );

        CREATE TABLE plans (
            plan_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            digest TEXT NOT NULL,
            expected_revision INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL
        );
        CREATE INDEX plans_expiry_idx ON plans(expires_at);

        CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            record_json TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            finished_at REAL,
            FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
        );
        CREATE INDEX operations_state_idx ON operations(state, updated_at);

        CREATE TABLE operation_tasks (
            operation_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            state TEXT NOT NULL,
            task_json TEXT NOT NULL,
            started_at REAL,
            finished_at REAL,
            error_json TEXT,
            PRIMARY KEY(operation_id, task_id),
            FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
                ON DELETE CASCADE
        );

        CREATE TABLE services (
            service_id TEXT PRIMARY KEY,
            component_id TEXT NOT NULL,
            service_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            operation_id TEXT,
            component_id TEXT,
            service_id TEXT,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX events_operation_idx ON events(operation_id, cursor);

        CREATE TABLE idempotency (
            idempotency_key TEXT PRIMARY KEY,
            request_digest TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
        );

        CREATE TABLE legacy_imports (
            source_key TEXT PRIMARY KEY,
            source_digest TEXT NOT NULL,
            report_json TEXT NOT NULL,
            imported_at REAL NOT NULL
        );

        CREATE TABLE owned_paths (
            canonical_path TEXT PRIMARY KEY,
            owner_kind TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            recorded_at REAL NOT NULL
        );

        CREATE TABLE release_slots (
            product TEXT NOT NULL,
            version TEXT NOT NULL,
            slot_path TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            healthy INTEGER NOT NULL DEFAULT 0,
            installed_at REAL NOT NULL,
            PRIMARY KEY(product, version)
        );
        """
    )
    now = __import__("time").time()
    connection.execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
        ("configuration_revision", "0", now),
    )


def migration_0002(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE api_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            request_digest TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX api_idempotency_created_idx
            ON api_idempotency(created_at);
        """
    )


def migration_0003(connection: sqlite3.Connection) -> None:
    connection.execute(
        "ALTER TABLE operation_tasks ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"
    )
    connection.execute(
        "ALTER TABLE operation_tasks ADD COLUMN result_json TEXT"
    )


def migration_0004(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE accepted_releases (
            product TEXT NOT NULL,
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            manifest_digest TEXT NOT NULL,
            envelope_json TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            verified_key_ids_json TEXT NOT NULL,
            accepted_at REAL NOT NULL,
            PRIMARY KEY(product, sequence),
            UNIQUE(product, manifest_digest)
        );
        CREATE INDEX accepted_releases_current_idx
            ON accepted_releases(product, sequence DESC);
        """
    )


def migration_0005(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE browser_sessions (
            session_id TEXT PRIMARY KEY,
            token_digest TEXT NOT NULL UNIQUE,
            security_context TEXT NOT NULL,
            remembered INTEGER NOT NULL,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            idle_ttl_seconds INTEGER NOT NULL,
            idle_expires_at REAL NOT NULL,
            absolute_expires_at REAL NOT NULL,
            user_agent TEXT NOT NULL
        );
        CREATE INDEX browser_sessions_expiry_idx
            ON browser_sessions(idle_expires_at, absolute_expires_at);
        CREATE INDEX browser_sessions_context_idx
            ON browser_sessions(security_context, last_seen_at DESC);
        """
    )


def migration_0006(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE automation_clients (
            client_id TEXT PRIMARY KEY,
            client_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            manager_instance_id TEXT NOT NULL,
            application_instance_id TEXT NOT NULL,
            canonical_application_origin TEXT NOT NULL,
            canonical_recovery_origin TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_used_at REAL,
            revoked_at REAL
        );
        CREATE INDEX automation_clients_subject_idx
            ON automation_clients(subject, created_at DESC);

        CREATE TABLE automation_tokens (
            token_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            token_digest TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_used_at REAL,
            revoked_at REAL,
            FOREIGN KEY(client_id) REFERENCES automation_clients(client_id)
                ON DELETE CASCADE
        );
        CREATE INDEX automation_tokens_client_idx
            ON automation_tokens(client_id, expires_at);

        CREATE TABLE automation_enrollment_grants (
            grant_digest TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            client_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            manager_instance_id TEXT NOT NULL,
            application_instance_id TEXT NOT NULL,
            canonical_application_origin TEXT NOT NULL,
            canonical_recovery_origin TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            token_expires_at REAL NOT NULL,
            consumed_at REAL
        );
        CREATE INDEX automation_grants_expiry_idx
            ON automation_enrollment_grants(expires_at);
        """
    )


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, migration_0001),
    (2, migration_0002),
    (3, migration_0003),
    (4, migration_0004),
    (5, migration_0005),
    (6, migration_0006),
)


def migrate(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    }
    current = max(applied, default=0)
    for version, migration in MIGRATIONS:
        if version in applied:
            continue
        if version != current + 1:
            raise RuntimeError(
                f"Manager state migration gap: expected {current + 1}, found {version}."
            )
        migration(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%s','now'))",
            (version,),
        )
        current = version
    return current
