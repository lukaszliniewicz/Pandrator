"""SQLite store with short transactions and versioned typed records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..errors import ConflictError, NotFoundError
from ..models import (
    TERMINAL_OPERATION_STATES,
    ComponentInspection,
    DesiredComponentState,
    ManagedService,
    ManagerEvent,
    OperationPlan,
    OperationRecord,
    OperationState,
    OperationTaskRecord,
    TaskSpec,
    TaskState,
)
from .migrations import migrate


def _timestamp(value: datetime | None = None) -> float:
    selected = value or datetime.now(timezone.utc)
    return selected.timestamp()


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ManagerStore:
    """The daemon is the only writer; methods remain thread-safe for API workers."""

    def __init__(
        self,
        database: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        event_retention: int = 5000,
    ) -> None:
        self.database = Path(database).expanduser().resolve(strict=False)
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        self.event_retention = max(100, int(event_retention))
        self._write_lock = threading.RLock()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        lock = self._write_lock if write else _NullLock()
        with lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> int:
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                version = migrate(connection)
                connection.commit()
                return version
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def schema_version(self) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])

    def configuration_revision(self) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key='configuration_revision'"
            ).fetchone()
            return int(json.loads(row["value_json"])) if row else 0

    def setting(self, key: str, default: Any = None) -> Any:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key=?",
                (key,),
            ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        if not key or len(key) > 200:
            raise ValueError("Setting key is invalid.")
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, _json(value), _timestamp()),
            )

    def bump_configuration_revision(self) -> int:
        now = _timestamp()
        with self.transaction(write=True) as connection:
            return self._bump_revision(connection, now)

    @staticmethod
    def _bump_revision(connection: sqlite3.Connection, now: float) -> int:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key='configuration_revision'"
        ).fetchone()
        revision = int(json.loads(row["value_json"])) + 1 if row else 1
        connection.execute(
            """
            INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            ("configuration_revision", _json(revision), now),
        )
        return revision

    def save_component(
        self,
        inspection: ComponentInspection,
        *,
        desired: DesiredComponentState | None = None,
        bump_revision: bool = False,
    ) -> int:
        now = _timestamp()
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO components(
                    component_id, desired_json, inspection_json,
                    installed_version, installed_revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(component_id) DO UPDATE SET
                    desired_json=COALESCE(excluded.desired_json, components.desired_json),
                    inspection_json=excluded.inspection_json,
                    installed_version=excluded.installed_version,
                    installed_revision=excluded.installed_revision,
                    updated_at=excluded.updated_at
                """,
                (
                    inspection.component_id,
                    _json(desired) if desired is not None else None,
                    _json(inspection),
                    inspection.installed_version,
                    inspection.installed_revision,
                    now,
                ),
            )
            if bump_revision:
                return self._bump_revision(connection, now)
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key='configuration_revision'"
            ).fetchone()
            return int(json.loads(row["value_json"])) if row else 0

    def commit_operation_success(
        self,
        operation: OperationRecord,
        *,
        inspections: Mapping[str, ComponentInspection],
        desired: Mapping[str, DesiredComponentState],
        expected_revision: int,
        claimed_owned_paths: tuple[
            tuple[Path, str, str, Mapping[str, Any]],
            ...,
        ] = (),
        released_owned_paths: tuple[Path, ...] = (),
        release_activation: Mapping[str, Any] | None = None,
    ) -> int:
        """Atomically publish component state and the terminal operation.

        Filesystem activation happens before this transaction and retains its
        rollback material until after this method succeeds.  Keeping the
        component records, ownership release, configuration revision, and
        operation terminal state in one SQLite transaction prevents a partial
        configuration commit if any database write fails.
        """

        if operation.state != OperationState.SUCCEEDED:
            raise ValueError("Only a succeeded operation can be committed.")
        now = _timestamp(operation.updated_at)
        with self.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key='configuration_revision'"
            ).fetchone()
            actual_revision = int(json.loads(row["value_json"])) if row else 0
            if actual_revision != int(expected_revision):
                raise ConflictError(
                    "Manager configuration changed while the operation was running.",
                    {
                        "expected_revision": int(expected_revision),
                        "actual_revision": actual_revision,
                    },
                )

            for component_id, inspection in inspections.items():
                selected_desired = desired.get(component_id)
                connection.execute(
                    """
                    INSERT INTO components(
                        component_id, desired_json, inspection_json,
                        installed_version, installed_revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(component_id) DO UPDATE SET
                        desired_json=excluded.desired_json,
                        inspection_json=excluded.inspection_json,
                        installed_version=excluded.installed_version,
                        installed_revision=excluded.installed_revision,
                        updated_at=excluded.updated_at
                    """,
                    (
                        component_id,
                        _json(selected_desired),
                        _json(inspection),
                        inspection.installed_version,
                        inspection.installed_revision,
                        now,
                    ),
                )

            for path in released_owned_paths:
                canonical = str(path.expanduser().resolve(strict=False))
                connection.execute(
                    "DELETE FROM owned_paths WHERE canonical_path=?",
                    (canonical,),
                )

            for path, owner_kind, owner_id, evidence in claimed_owned_paths:
                canonical = str(path.expanduser().resolve(strict=False))
                connection.execute(
                    """
                    INSERT INTO owned_paths(
                        canonical_path, owner_kind, owner_id,
                        evidence_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(canonical_path) DO UPDATE SET
                        owner_kind=excluded.owner_kind,
                        owner_id=excluded.owner_id,
                        evidence_json=excluded.evidence_json,
                        recorded_at=excluded.recorded_at
                    """,
                    (
                        canonical,
                        owner_kind,
                        owner_id,
                        _json(dict(evidence)),
                        now,
                    ),
                )

            if release_activation is not None:
                product = str(release_activation["product"])
                version = str(release_activation["version"])
                channel = str(release_activation["channel"])
                sequence = int(release_activation["sequence"])
                manifest_digest = str(
                    release_activation["manifest_digest"]
                )
                slot_path = str(
                    Path(release_activation["slot_path"])
                    .expanduser()
                    .resolve(strict=False)
                )
                existing_sequence = connection.execute(
                    """
                    SELECT manifest_digest FROM accepted_releases
                    WHERE product=? AND sequence=?
                    """,
                    (product, sequence),
                ).fetchone()
                if (
                    existing_sequence is not None
                    and existing_sequence["manifest_digest"] != manifest_digest
                ):
                    raise ConflictError(
                        "A different signed release already owns this sequence.",
                        {"product": product, "sequence": sequence},
                    )
                existing_version = connection.execute(
                    """
                    SELECT manifest_digest FROM release_slots
                    WHERE product=? AND version=?
                    """,
                    (product, version),
                ).fetchone()
                if (
                    existing_version is not None
                    and existing_version["manifest_digest"] != manifest_digest
                ):
                    raise ConflictError(
                        "A different signed release already owns this version.",
                        {"product": product, "version": version},
                    )
                connection.execute(
                    "UPDATE release_slots SET active=0 WHERE product=?",
                    (product,),
                )
                connection.execute(
                    """
                    INSERT INTO release_slots(
                        product, version, slot_path, manifest_digest,
                        active, healthy, installed_at
                    ) VALUES (?, ?, ?, ?, 1, 1, ?)
                    ON CONFLICT(product, version) DO UPDATE SET
                        slot_path=excluded.slot_path,
                        manifest_digest=excluded.manifest_digest,
                        active=1,
                        healthy=1
                    """,
                    (
                        product,
                        version,
                        slot_path,
                        manifest_digest,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO accepted_releases(
                        product, channel, version, sequence,
                        manifest_digest, envelope_json, artifact_json,
                        verified_key_ids_json, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product, sequence) DO NOTHING
                    """,
                    (
                        product,
                        channel,
                        version,
                        sequence,
                        manifest_digest,
                        _json(dict(release_activation["envelope"])),
                        _json(dict(release_activation["artifact"])),
                        _json(
                            list(
                                release_activation.get(
                                    "verified_key_ids",
                                    (),
                                )
                            )
                        ),
                        now,
                    ),
                )

            revision = self._bump_revision(connection, now)
            result = connection.execute(
                """
                UPDATE operations
                SET state=?, record_json=?, updated_at=?, finished_at=?
                WHERE operation_id=?
                """,
                (
                    operation.state.value,
                    operation.model_dump_json(),
                    now,
                    (
                        _timestamp(operation.finished_at)
                        if operation.finished_at
                        else None
                    ),
                    operation.id,
                ),
            )
            if result.rowcount != 1:
                raise NotFoundError(
                    "Operation was not found.",
                    {"operation_id": operation.id},
                )
            return revision

    def component_records(
        self,
    ) -> dict[str, tuple[DesiredComponentState | None, ComponentInspection]]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT component_id, desired_json, inspection_json FROM components ORDER BY component_id"
            ).fetchall()
        return {
            str(row["component_id"]): (
                (
                    DesiredComponentState.model_validate_json(row["desired_json"])
                    if row["desired_json"]
                    else None
                ),
                ComponentInspection.model_validate_json(row["inspection_json"]),
            )
            for row in rows
        }

    def save_plan(self, plan: OperationPlan) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO plans(
                    plan_id, plan_json, digest, expected_revision,
                    created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    plan.id,
                    plan.model_dump_json(),
                    plan.digest,
                    plan.expected_revision,
                    _timestamp(plan.created_at),
                    _timestamp(plan.expires_at),
                ),
            )

    def get_plan(self, plan_id: str) -> OperationPlan:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT plan_json FROM plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Operation plan was not found.", {"plan_id": plan_id})
        return OperationPlan.model_validate_json(row["plan_json"])

    def consume_plan(
        self,
        plan_id: str,
        *,
        now: datetime,
        expected_digest: str,
    ) -> OperationPlan:
        timestamp = _timestamp(now)
        with self.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT plan_json, digest, expires_at, consumed_at FROM plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Operation plan was not found.", {"plan_id": plan_id})
            if row["digest"] != expected_digest:
                raise ConflictError("Operation plan digest does not match.")
            if row["expires_at"] <= timestamp:
                raise ConflictError("Operation plan has expired.", {"plan_id": plan_id})
            if row["consumed_at"] is not None:
                raise ConflictError("Operation plan was already consumed.", {"plan_id": plan_id})
            connection.execute(
                "UPDATE plans SET consumed_at=? WHERE plan_id=?",
                (timestamp, plan_id),
            )
            return OperationPlan.model_validate_json(row["plan_json"])

    def create_operation(
        self,
        record: OperationRecord,
        *,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
    ) -> tuple[OperationRecord, bool]:
        request_digest = hashlib.sha256(_json(request_payload).encode("utf-8")).hexdigest()
        now = _timestamp(record.created_at)
        with self.transaction(write=True) as connection:
            existing = connection.execute(
                """
                SELECT request_digest, operation_id
                FROM idempotency WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError(
                        "Idempotency key was already used for a different request."
                    )
                row = connection.execute(
                    "SELECT record_json FROM operations WHERE operation_id=?",
                    (existing["operation_id"],),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Idempotency record references a missing operation.")
                return OperationRecord.model_validate_json(row["record_json"]), False

            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, plan_id, kind, state, record_json,
                    created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.plan_id,
                    record.kind.value,
                    record.state.value,
                    record.model_dump_json(),
                    now,
                    _timestamp(record.updated_at),
                    _timestamp(record.finished_at) if record.finished_at else None,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotency(
                    idempotency_key, request_digest, operation_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (idempotency_key, request_digest, record.id, now),
            )
            return record, True

    def begin_operation(
        self,
        plan: OperationPlan,
        record: OperationRecord,
        *,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        now: datetime,
        actual_revision: int,
    ) -> tuple[OperationRecord, bool]:
        """Atomically consume an exact plan and create an idempotent operation."""
        request_digest = hashlib.sha256(_json(request_payload).encode("utf-8")).hexdigest()
        timestamp = _timestamp(now)
        with self.transaction(write=True) as connection:
            existing = connection.execute(
                """
                SELECT request_digest, operation_id
                FROM idempotency WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError(
                        "Idempotency key was already used for a different request."
                    )
                operation = connection.execute(
                    "SELECT record_json FROM operations WHERE operation_id=?",
                    (existing["operation_id"],),
                ).fetchone()
                if operation is None:
                    raise RuntimeError(
                        "Idempotency record references a missing operation."
                    )
                return OperationRecord.model_validate_json(
                    operation["record_json"]
                ), False

            row = connection.execute(
                """
                SELECT digest, expected_revision, expires_at, consumed_at
                FROM plans WHERE plan_id=?
                """,
                (plan.id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "Operation plan was not found.",
                    {"plan_id": plan.id},
                )
            if row["digest"] != plan.digest:
                raise ConflictError("Operation plan digest does not match.")
            if row["expires_at"] <= timestamp:
                raise ConflictError(
                    "Operation plan has expired.",
                    {"plan_id": plan.id},
                )
            if row["consumed_at"] is not None:
                raise ConflictError(
                    "Operation plan was already consumed.",
                    {"plan_id": plan.id},
                )
            if int(row["expected_revision"]) != int(actual_revision):
                raise ConflictError(
                    "Manager configuration changed after this plan was created.",
                    {
                        "expected_revision": int(row["expected_revision"]),
                        "actual_revision": int(actual_revision),
                    },
                )

            connection.execute(
                "UPDATE plans SET consumed_at=? WHERE plan_id=?",
                (timestamp, plan.id),
            )
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, plan_id, kind, state, record_json,
                    created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.plan_id,
                    record.kind.value,
                    record.state.value,
                    record.model_dump_json(),
                    _timestamp(record.created_at),
                    _timestamp(record.updated_at),
                    None,
                ),
            )
            for ordinal, task in enumerate(plan.tasks):
                connection.execute(
                    """
                    INSERT INTO operation_tasks(
                        operation_id, task_id, ordinal, state, task_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        task.id,
                        ordinal,
                        "pending",
                        task.model_dump_json(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO idempotency(
                    idempotency_key, request_digest, operation_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (idempotency_key, request_digest, record.id, timestamp),
            )
            return record, True

    def update_operation(self, record: OperationRecord) -> None:
        with self.transaction(write=True) as connection:
            result = connection.execute(
                """
                UPDATE operations
                SET state=?, record_json=?, updated_at=?, finished_at=?
                WHERE operation_id=?
                """,
                (
                    record.state.value,
                    record.model_dump_json(),
                    _timestamp(record.updated_at),
                    _timestamp(record.finished_at) if record.finished_at else None,
                    record.id,
                ),
            )
            if result.rowcount != 1:
                raise NotFoundError("Operation was not found.", {"operation_id": record.id})

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Operation was not found.", {"operation_id": operation_id})
        return OperationRecord.model_validate_json(row["record_json"])

    def operation_tasks(self, operation_id: str) -> list[OperationTaskRecord]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT task_id, ordinal, state, task_json, attempt,
                       result_json, error_json, started_at, finished_at
                FROM operation_tasks
                WHERE operation_id=?
                ORDER BY ordinal
                """,
                (operation_id,),
            ).fetchall()
        return [
            OperationTaskRecord(
                operation_id=operation_id,
                task=TaskSpec.model_validate_json(row["task_json"]),
                ordinal=int(row["ordinal"]),
                state=TaskState(row["state"]),
                attempt=int(row["attempt"]),
                result=(
                    json.loads(row["result_json"])
                    if row["result_json"]
                    else {}
                ),
                error=(
                    json.loads(row["error_json"])
                    if row["error_json"]
                    else {}
                ),
                started_at=(
                    datetime.fromtimestamp(row["started_at"], timezone.utc)
                    if row["started_at"] is not None
                    else None
                ),
                finished_at=(
                    datetime.fromtimestamp(row["finished_at"], timezone.utc)
                    if row["finished_at"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def update_operation_task(
        self,
        operation_id: str,
        task_id: str,
        *,
        state: TaskState,
        attempt: int,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        with self.transaction(write=True) as connection:
            changed = connection.execute(
                """
                UPDATE operation_tasks
                SET state=?, attempt=?, result_json=?, error_json=?,
                    started_at=?, finished_at=?
                WHERE operation_id=? AND task_id=?
                """,
                (
                    state.value,
                    int(attempt),
                    _json(dict(result or {})) if result is not None else None,
                    _json(dict(error or {})) if error is not None else None,
                    _timestamp(started_at) if started_at else None,
                    _timestamp(finished_at) if finished_at else None,
                    operation_id,
                    task_id,
                ),
            )
            if changed.rowcount != 1:
                raise NotFoundError(
                    "Operation task was not found.",
                    {"operation_id": operation_id, "task_id": task_id},
                )

    def list_operations(
        self,
        *,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[OperationRecord]:
        parameters: list[Any] = []
        query = "SELECT record_json FROM operations"
        if active_only:
            terminal = sorted(state.value for state in TERMINAL_OPERATION_STATES)
            placeholders = ",".join("?" for _ in terminal)
            query += f" WHERE state NOT IN ({placeholders})"
            parameters.extend(terminal)
        query += " ORDER BY updated_at DESC LIMIT ?"
        parameters.append(max(1, min(int(limit), 500)))
        with self.transaction() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            OperationRecord.model_validate_json(row["record_json"])
            for row in rows
        ]

    def request_cancellation(self, operation_id: str) -> bool:
        with self.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "Operation was not found.",
                    {"operation_id": operation_id},
                )
            if row["state"] == OperationState.HANDOFF_PENDING.value:
                raise ConflictError(
                    "Manager handoff is already in progress and cannot be "
                    "cancelled through the retiring daemon.",
                    {"operation_id": operation_id},
                )
            connection.execute(
                "UPDATE operations SET cancel_requested=1 WHERE operation_id=?",
                (operation_id,),
            )
            return True

    def cancellation_requested(self, operation_id: str) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("Operation was not found.", {"operation_id": operation_id})
        return bool(row["cancel_requested"])

    def save_service(self, service: ManagedService) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO services(service_id, component_id, service_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(service_id) DO UPDATE SET
                    component_id=excluded.component_id,
                    service_json=excluded.service_json,
                    updated_at=excluded.updated_at
                """,
                (
                    service.id,
                    service.component_id,
                    service.model_dump_json(),
                    _timestamp(),
                ),
            )

    def list_services(self) -> list[ManagedService]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT service_json FROM services ORDER BY service_id"
            ).fetchall()
        return [ManagedService.model_validate_json(row["service_json"]) for row in rows]

    def append_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        component_id: str | None = None,
        service_id: str | None = None,
    ) -> ManagerEvent:
        now = _timestamp()
        with self.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO events(
                    event_type, operation_id, component_id, service_id,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    operation_id,
                    component_id,
                    service_id,
                    _json(dict(payload)),
                    now,
                ),
            ).lastrowid
            cutoff = connection.execute(
                """
                SELECT cursor FROM events ORDER BY cursor DESC LIMIT 1 OFFSET ?
                """,
                (self.event_retention,),
            ).fetchone()
            if cutoff is not None:
                connection.execute(
                    "DELETE FROM events WHERE cursor <= ?",
                    (int(cutoff["cursor"]),),
                )
        return ManagerEvent(
            cursor=int(cursor),
            event_type=event_type,
            payload=dict(payload),
            operation_id=operation_id,
            component_id=component_id,
            service_id=service_id,
            created_at=datetime.fromtimestamp(now, timezone.utc),
        )

    def events_after(self, cursor: int, *, limit: int = 500) -> list[ManagerEvent]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT cursor, event_type, operation_id, component_id,
                       service_id, payload_json, created_at
                FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?
                """,
                (max(0, int(cursor)), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [
            ManagerEvent(
                cursor=int(row["cursor"]),
                event_type=row["event_type"],
                operation_id=row["operation_id"],
                component_id=row["component_id"],
                service_id=row["service_id"],
                payload=json.loads(row["payload_json"]),
                created_at=datetime.fromtimestamp(row["created_at"], timezone.utc),
            )
            for row in rows
        ]

    def recent_events(self, *, limit: int = 100) -> list[ManagerEvent]:
        """Return newest manager events first for finite activity views."""

        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT cursor, event_type, operation_id, component_id,
                       service_id, payload_json, created_at
                FROM events ORDER BY cursor DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [
            ManagerEvent(
                cursor=int(row["cursor"]),
                event_type=row["event_type"],
                operation_id=row["operation_id"],
                component_id=row["component_id"],
                service_id=row["service_id"],
                payload=json.loads(row["payload_json"]),
                created_at=datetime.fromtimestamp(row["created_at"], timezone.utc),
            )
            for row in rows
        ]

    def event_bounds(self) -> tuple[int | None, int | None]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT MIN(cursor) AS first, MAX(cursor) AS last FROM events"
            ).fetchone()
        return (
            int(row["first"]) if row["first"] is not None else None,
            int(row["last"]) if row["last"] is not None else None,
        )

    def api_idempotency_result(
        self,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], int] | None:
        request_digest = hashlib.sha256(
            _json(request_payload).encode("utf-8")
        ).hexdigest()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT request_digest, status_code, response_json
                FROM api_idempotency WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise ConflictError(
                "Idempotency key was already used for a different request."
            )
        return json.loads(row["response_json"]), int(row["status_code"])

    def record_api_idempotency(
        self,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        response_payload: Mapping[str, Any],
        status_code: int,
    ) -> None:
        request_digest = hashlib.sha256(
            _json(request_payload).encode("utf-8")
        ).hexdigest()
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO api_idempotency(
                    idempotency_key, request_digest, status_code,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    request_digest,
                    int(status_code),
                    _json(dict(response_payload)),
                    _timestamp(),
                ),
            )

    def create_browser_session(
        self,
        *,
        session_id: str,
        token_digest: str,
        security_context: str,
        remembered: bool,
        created_at: float,
        last_seen_at: float,
        idle_ttl_seconds: int,
        idle_expires_at: float,
        absolute_expires_at: float,
        user_agent: str,
    ) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO browser_sessions(
                    session_id, token_digest, security_context, remembered,
                    created_at, last_seen_at, idle_ttl_seconds,
                    idle_expires_at, absolute_expires_at, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    token_digest,
                    security_context,
                    int(remembered),
                    float(created_at),
                    float(last_seen_at),
                    int(idle_ttl_seconds),
                    float(idle_expires_at),
                    float(absolute_expires_at),
                    user_agent,
                ),
            )

    def browser_session(self, token_digest: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT session_id, token_digest, security_context, remembered,
                       created_at, last_seen_at, idle_ttl_seconds,
                       idle_expires_at, absolute_expires_at, user_agent
                FROM browser_sessions WHERE token_digest=?
                """,
                (token_digest,),
            ).fetchone()
        return self._browser_session_payload(row) if row is not None else None

    def browser_sessions(self, security_context: str) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT session_id, token_digest, security_context, remembered,
                       created_at, last_seen_at, idle_ttl_seconds,
                       idle_expires_at, absolute_expires_at, user_agent
                FROM browser_sessions
                WHERE security_context=?
                ORDER BY last_seen_at DESC, created_at DESC
                """,
                (security_context,),
            ).fetchall()
        return [self._browser_session_payload(row) for row in rows]

    def touch_browser_session(
        self,
        token_digest: str,
        *,
        last_seen_at: float,
        idle_expires_at: float,
    ) -> bool:
        with self.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE browser_sessions
                SET last_seen_at=?, idle_expires_at=?
                WHERE token_digest=?
                """,
                (
                    float(last_seen_at),
                    float(idle_expires_at),
                    token_digest,
                ),
            )
        return bool(cursor.rowcount)

    def delete_browser_session(self, token_digest: str) -> bool:
        with self.transaction(write=True) as connection:
            cursor = connection.execute(
                "DELETE FROM browser_sessions WHERE token_digest=?",
                (token_digest,),
            )
        return bool(cursor.rowcount)

    def delete_browser_sessions(self) -> int:
        with self.transaction(write=True) as connection:
            cursor = connection.execute("DELETE FROM browser_sessions")
        return max(0, int(cursor.rowcount))

    def prune_browser_sessions(
        self,
        *,
        now: float,
        security_context: str,
    ) -> int:
        with self.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM browser_sessions
                WHERE idle_expires_at <= ?
                   OR absolute_expires_at <= ?
                   OR security_context <> ?
                """,
                (float(now), float(now), security_context),
            )
        return max(0, int(cursor.rowcount))

    @staticmethod
    def _browser_session_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "token_digest": row["token_digest"],
            "security_context": row["security_context"],
            "remembered": bool(row["remembered"]),
            "created_at": float(row["created_at"]),
            "last_seen_at": float(row["last_seen_at"]),
            "idle_ttl_seconds": int(row["idle_ttl_seconds"]),
            "idle_expires_at": float(row["idle_expires_at"]),
            "absolute_expires_at": float(row["absolute_expires_at"]),
            "user_agent": row["user_agent"],
        }

    def legacy_import(self, source_key: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT report_json FROM legacy_imports WHERE source_key=?",
                (source_key,),
            ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def record_legacy_import(
        self,
        *,
        source_key: str,
        source_digest: str,
        report: Mapping[str, Any],
    ) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO legacy_imports(
                    source_key, source_digest, report_json, imported_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_digest=excluded.source_digest,
                    report_json=excluded.report_json,
                    imported_at=excluded.imported_at
                """,
                (source_key, source_digest, _json(dict(report)), _timestamp()),
            )

    def record_owned_path(
        self,
        path: Path,
        *,
        owner_kind: str,
        owner_id: str,
        evidence: Mapping[str, Any],
    ) -> None:
        canonical = str(path.expanduser().resolve(strict=False))
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO owned_paths(
                    canonical_path, owner_kind, owner_id, evidence_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    owner_kind=excluded.owner_kind,
                    owner_id=excluded.owner_id,
                    evidence_json=excluded.evidence_json,
                    recorded_at=excluded.recorded_at
                """,
                (canonical, owner_kind, owner_id, _json(dict(evidence)), _timestamp()),
            )

    def owned_paths(self) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT canonical_path, owner_kind, owner_id, evidence_json, recorded_at
                FROM owned_paths ORDER BY canonical_path
                """
            ).fetchall()
        return [
            {
                "path": row["canonical_path"],
                "owner_kind": row["owner_kind"],
                "owner_id": row["owner_id"],
                "evidence": json.loads(row["evidence_json"]),
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def delete_owned_path(self, path: Path) -> None:
        canonical = str(path.expanduser().resolve(strict=False))
        with self.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM owned_paths WHERE canonical_path=?",
                (canonical,),
            )

    def save_release_slot(
        self,
        *,
        product: str,
        version: str,
        slot_path: Path,
        manifest_digest: str,
        active: bool,
        healthy: bool,
    ) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO release_slots(
                    product, version, slot_path, manifest_digest,
                    active, healthy, installed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product, version) DO UPDATE SET
                    slot_path=excluded.slot_path,
                    manifest_digest=excluded.manifest_digest,
                    active=excluded.active,
                    healthy=excluded.healthy
                """,
                (
                    product,
                    version,
                    str(slot_path.expanduser().resolve(strict=False)),
                    manifest_digest,
                    int(active),
                    int(healthy),
                    _timestamp(),
                ),
            )

    def activate_release_slot(self, *, product: str, version: str) -> None:
        with self.transaction(write=True) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM release_slots
                WHERE product=? AND version=?
                """,
                (product, version),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "Release slot was not found.",
                    {"product": product, "version": version},
                )
            connection.execute(
                "UPDATE release_slots SET active=0 WHERE product=?",
                (product,),
            )
            connection.execute(
                """
                UPDATE release_slots SET active=1, healthy=1
                WHERE product=? AND version=?
                """,
                (product, version),
            )

    def release_slots(self, product: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT product, version, slot_path, manifest_digest, "
            "active, healthy, installed_at FROM release_slots"
        )
        parameters: tuple[Any, ...] = ()
        if product is not None:
            query += " WHERE product=?"
            parameters = (product,)
        query += " ORDER BY product, installed_at DESC"
        with self.transaction() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "product": row["product"],
                "version": row["version"],
                "slot_path": row["slot_path"],
                "manifest_digest": row["manifest_digest"],
                "active": bool(row["active"]),
                "healthy": bool(row["healthy"]),
                "installed_at": row["installed_at"],
            }
            for row in rows
        ]

    def accepted_release(self, product: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT product, channel, version, sequence, manifest_digest,
                       envelope_json, artifact_json, verified_key_ids_json,
                       accepted_at
                FROM accepted_releases
                WHERE product=?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (product,),
            ).fetchone()
        return self._accepted_release_payload(row) if row is not None else None

    def accepted_releases(self, product: str) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT product, channel, version, sequence, manifest_digest,
                       envelope_json, artifact_json, verified_key_ids_json,
                       accepted_at
                FROM accepted_releases
                WHERE product=?
                ORDER BY sequence
                """,
                (product,),
            ).fetchall()
        return [self._accepted_release_payload(row) for row in rows]

    @staticmethod
    def _accepted_release_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "product": row["product"],
            "channel": row["channel"],
            "version": row["version"],
            "sequence": int(row["sequence"]),
            "manifest_digest": row["manifest_digest"],
            "envelope": json.loads(row["envelope_json"]),
            "artifact": json.loads(row["artifact_json"]),
            "verified_key_ids": tuple(
                json.loads(row["verified_key_ids_json"])
            ),
            "accepted_at": row["accepted_at"],
        }

    def save_automation_grant(
        self,
        grant: Mapping[str, Any],
    ) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM automation_enrollment_grants
                WHERE expires_at <= ? OR consumed_at IS NOT NULL
                """,
                (float(grant["created_at"]),),
            )
            connection.execute(
                """
                INSERT INTO automation_enrollment_grants(
                    grant_digest, client_id, client_name, subject,
                    manager_instance_id, application_instance_id,
                    canonical_application_origin,
                    canonical_recovery_origin, scopes_json,
                    code_challenge, created_at, expires_at,
                    token_expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    grant["grant_digest"],
                    grant["client_id"],
                    grant["client_name"],
                    grant["subject"],
                    grant["manager_instance_id"],
                    grant["application_instance_id"],
                    grant["canonical_application_origin"],
                    grant["canonical_recovery_origin"],
                    _json(list(grant["scopes"])),
                    grant["code_challenge"],
                    float(grant["created_at"]),
                    float(grant["expires_at"]),
                    float(grant["token_expires_at"]),
                ),
            )

    def consume_automation_grant(
        self,
        grant_digest: str,
        *,
        now: float,
    ) -> dict[str, Any] | None:
        with self.transaction(write=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM automation_enrollment_grants
                WHERE grant_digest=?
                """,
                (grant_digest,),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or float(row["expires_at"]) <= float(now)
            ):
                return None
            connection.execute(
                """
                UPDATE automation_enrollment_grants
                SET consumed_at=?
                WHERE grant_digest=? AND consumed_at IS NULL
                """,
                (float(now), grant_digest),
            )
            return {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
                "subject": row["subject"],
                "manager_instance_id": row["manager_instance_id"],
                "application_instance_id": row[
                    "application_instance_id"
                ],
                "canonical_application_origin": row[
                    "canonical_application_origin"
                ],
                "canonical_recovery_origin": row[
                    "canonical_recovery_origin"
                ],
                "scopes": tuple(json.loads(row["scopes_json"])),
                "code_challenge": row["code_challenge"],
                "created_at": float(row["created_at"]),
                "expires_at": float(row["expires_at"]),
                "token_expires_at": float(row["token_expires_at"]),
            }

    def save_automation_token(
        self,
        *,
        principal: Mapping[str, Any],
        token_id: str,
        token_digest: str,
        now: float,
        expires_at: float,
    ) -> None:
        with self.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_clients(
                    client_id, client_name, subject, manager_instance_id,
                    application_instance_id, canonical_application_origin,
                    canonical_recovery_origin, scopes_json, created_at,
                    last_used_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(client_id) DO UPDATE SET
                    client_name=excluded.client_name,
                    subject=excluded.subject,
                    manager_instance_id=excluded.manager_instance_id,
                    application_instance_id=excluded.application_instance_id,
                    canonical_application_origin=
                        excluded.canonical_application_origin,
                    canonical_recovery_origin=
                        excluded.canonical_recovery_origin,
                    scopes_json=excluded.scopes_json,
                    revoked_at=NULL
                """,
                (
                    principal["client_id"],
                    principal["client_name"],
                    principal["subject"],
                    principal["manager_instance_id"],
                    principal["application_instance_id"],
                    principal["canonical_application_origin"],
                    principal["canonical_recovery_origin"],
                    _json(list(principal["scopes"])),
                    float(now),
                ),
            )
            connection.execute(
                """
                UPDATE automation_tokens
                SET revoked_at=?
                WHERE client_id=? AND revoked_at IS NULL
                """,
                (float(now), principal["client_id"]),
            )
            connection.execute(
                """
                INSERT INTO automation_tokens(
                    token_id, client_id, token_digest, created_at,
                    expires_at, last_used_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    token_id,
                    principal["client_id"],
                    token_digest,
                    float(now),
                    float(expires_at),
                ),
            )

    @staticmethod
    def _automation_principal_payload(
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "subject": row["subject"],
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "manager_instance_id": row["manager_instance_id"],
            "application_instance_id": row["application_instance_id"],
            "canonical_application_origin": row[
                "canonical_application_origin"
            ],
            "canonical_recovery_origin": row[
                "canonical_recovery_origin"
            ],
            "scopes": tuple(json.loads(row["scopes_json"])),
            "expires_at": float(row["expires_at"]),
            "created_at": float(row["created_at"]),
            "last_used_at": (
                float(row["effective_last_used_at"])
                if row["effective_last_used_at"] is not None
                else None
            ),
            "revoked_at": (
                float(row["effective_revoked_at"])
                if row["effective_revoked_at"] is not None
                else None
            ),
            "token_id": row["token_id"],
        }

    def automation_principal(
        self,
        token_digest: str,
        *,
        now: float,
    ) -> dict[str, Any] | None:
        with self.transaction(write=True) as connection:
            row = connection.execute(
                """
                SELECT c.*, t.token_id, t.expires_at,
                       t.created_at AS token_created_at,
                       COALESCE(
                           t.last_used_at, c.last_used_at
                       ) AS effective_last_used_at,
                       COALESCE(
                           t.revoked_at, c.revoked_at
                       ) AS effective_revoked_at
                FROM automation_tokens AS t
                JOIN automation_clients AS c
                    ON c.client_id=t.client_id
                WHERE t.token_digest=?
                """,
                (token_digest,),
            ).fetchone()
            if (
                row is None
                or row["effective_revoked_at"] is not None
                or float(row["expires_at"]) <= float(now)
            ):
                return None
            if (
                row["effective_last_used_at"] is None
                or float(now)
                - float(row["effective_last_used_at"])
                >= 60
            ):
                connection.execute(
                    """
                    UPDATE automation_tokens
                    SET last_used_at=?
                    WHERE token_id=?
                    """,
                    (float(now), row["token_id"]),
                )
                connection.execute(
                    """
                    UPDATE automation_clients
                    SET last_used_at=?
                    WHERE client_id=?
                    """,
                    (float(now), row["client_id"]),
                )
            payload = self._automation_principal_payload(row)
            payload["created_at"] = float(row["token_created_at"])
            payload["last_used_at"] = float(now)
            return payload

    def automation_clients(self) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT c.*, t.token_id, t.expires_at,
                       t.created_at AS token_created_at,
                       COALESCE(
                           t.last_used_at, c.last_used_at
                       ) AS effective_last_used_at,
                       COALESCE(
                           t.revoked_at, c.revoked_at
                       ) AS effective_revoked_at
                FROM automation_clients AS c
                LEFT JOIN automation_tokens AS t
                    ON t.token_id=(
                        SELECT selected.token_id
                        FROM automation_tokens AS selected
                        WHERE selected.client_id=c.client_id
                        ORDER BY selected.created_at DESC
                        LIMIT 1
                    )
                ORDER BY c.created_at DESC, c.client_id
                """
            ).fetchall()
        payloads = []
        for row in rows:
            payload = self._automation_principal_payload(row)
            payload["expires_at"] = (
                float(row["expires_at"])
                if row["expires_at"] is not None
                else 0.0
            )
            payload["token_id"] = (
                str(row["token_id"])
                if row["token_id"] is not None
                else None
            )
            payloads.append(payload)
        return payloads

    def revoke_automation_client(
        self,
        client_id: str,
        *,
        now: float,
    ) -> bool:
        with self.transaction(write=True) as connection:
            result = connection.execute(
                """
                UPDATE automation_clients
                SET revoked_at=?
                WHERE client_id=? AND revoked_at IS NULL
                """,
                (float(now), client_id),
            )
            connection.execute(
                """
                UPDATE automation_tokens
                SET revoked_at=?
                WHERE client_id=? AND revoked_at IS NULL
                """,
                (float(now), client_id),
            )
            return bool(result.rowcount)


class StoreEventSink:
    def __init__(self, store: ManagerStore) -> None:
        self.store = store

    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        component_id: str | None = None,
        service_id: str | None = None,
    ) -> None:
        self.store.append_event(
            event_type,
            payload,
            operation_id=operation_id,
            component_id=component_id,
            service_id=service_id,
        )


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
