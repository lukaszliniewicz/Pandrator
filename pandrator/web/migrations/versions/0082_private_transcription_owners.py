"""Persist stable User and personal-workspace ownership for private transcriptions."""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import RowMapping

revision = "0082_private_transcription_owners"
down_revision = "0081_credential_resource_grants"
branch_labels = None
depends_on = None

_OWNER_TABLE = "quick_transcription_owners"
_OWNER_INDEXES = (
    "ix_quick_transcription_owners_user_id",
    "ix_quick_transcription_owners_workspace_id",
)
_OWNER_TABLE_SQL = """CREATE TABLE quick_transcription_owners (
    transcription_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    workspace_id VARCHAR(36) NOT NULL,
    PRIMARY KEY (transcription_id),
    CONSTRAINT fk_quick_transcription_owners_transcription_id
        FOREIGN KEY(transcription_id) REFERENCES quick_transcriptions (id) ON DELETE CASCADE,
    CONSTRAINT fk_quick_transcription_owners_user_id
        FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_quick_transcription_owners_workspace_id
        FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE RESTRICT
)"""
_INDEX_SQL = {
    _OWNER_INDEXES[0]: "CREATE INDEX ix_quick_transcription_owners_user_id "
    "ON quick_transcription_owners (user_id)",
    _OWNER_INDEXES[1]: "CREATE INDEX ix_quick_transcription_owners_workspace_id "
    "ON quick_transcription_owners (workspace_id)",
}
_TRIGGER_SQL = {
    "quick_transcription_owners_scope_insert": """CREATE TRIGGER quick_transcription_owners_scope_insert
BEFORE INSERT ON quick_transcription_owners
FOR EACH ROW
WHEN EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
  AND EXISTS (SELECT 1 FROM workspaces WHERE id = NEW.workspace_id)
  AND EXISTS (SELECT 1 FROM quick_transcriptions WHERE id = NEW.transcription_id)
  AND (
      NOT EXISTS (
          SELECT 1
          FROM workspaces AS workspace
          WHERE workspace.id = NEW.workspace_id
            AND workspace.kind = 'personal'
            AND workspace.personal_owner_user_id = NEW.user_id
      )
      OR EXISTS (
          SELECT 1
          FROM quick_transcriptions AS transcription
          JOIN jobs AS job ON job.id = transcription.job_id
          WHERE transcription.id = NEW.transcription_id
            AND transcription.job_id IS NOT NULL
            AND (
                job.kind IS NOT 'transcription.quick'
                OR job.workspace_id IS NOT NEW.workspace_id
                OR job.project_id IS NOT NULL
                OR job.session_id IS NOT NULL
                OR job.workflow_run_id IS NOT NULL
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid private transcription owner scope');
END;""",
    "quick_transcription_owners_scope_update": """CREATE TRIGGER quick_transcription_owners_scope_update
BEFORE UPDATE ON quick_transcription_owners
FOR EACH ROW
WHEN NEW.transcription_id IS NOT OLD.transcription_id
  OR NEW.user_id IS NOT OLD.user_id
  OR NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'immutable private transcription owner identity');
END;""",
    "workspaces_private_transcription_scope_update": """CREATE TRIGGER workspaces_private_transcription_scope_update
BEFORE UPDATE OF kind, personal_owner_user_id ON workspaces
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM quick_transcription_owners AS owner
    WHERE owner.workspace_id = OLD.id
      AND (
          NEW.kind IS NOT 'personal'
          OR NEW.personal_owner_user_id IS NOT owner.user_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid private transcription workspace scope');
END;""",
    "quick_transcriptions_private_job_update": """CREATE TRIGGER quick_transcriptions_private_job_update
BEFORE UPDATE OF job_id ON quick_transcriptions
FOR EACH ROW
WHEN NEW.job_id IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM quick_transcription_owners AS owner
      WHERE owner.transcription_id = OLD.id
        AND NOT EXISTS (
            SELECT 1
            FROM jobs AS job
            WHERE job.id = NEW.job_id
              AND job.kind = 'transcription.quick'
              AND job.workspace_id = owner.workspace_id
              AND job.project_id IS NULL
              AND job.session_id IS NULL
              AND job.workflow_run_id IS NULL
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'invalid private transcription job scope');
END;""",
    "jobs_private_transcription_scope_update": """CREATE TRIGGER jobs_private_transcription_scope_update
BEFORE UPDATE OF workspace_id, project_id, session_id, workflow_run_id, kind ON jobs
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM quick_transcription_owners AS owner
    JOIN quick_transcriptions AS transcription
      ON transcription.id = owner.transcription_id
    WHERE transcription.job_id = OLD.id
      AND (
          NEW.kind IS NOT 'transcription.quick'
          OR NEW.workspace_id IS NOT owner.workspace_id
          OR NEW.project_id IS NOT NULL
          OR NEW.session_id IS NOT NULL
          OR NEW.workflow_run_id IS NOT NULL
      )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid private transcription job scope');
END;""",
}
_TRIGGER_TABLES = {
    "quick_transcription_owners_scope_insert": _OWNER_TABLE,
    "quick_transcription_owners_scope_update": _OWNER_TABLE,
    "workspaces_private_transcription_scope_update": "workspaces",
    "quick_transcriptions_private_job_update": "quick_transcriptions",
    "jobs_private_transcription_scope_update": "jobs",
}

_REQUIRED_COLUMNS: dict[str, dict[str, tuple[str, bool, bool]]] = {
    "users": {
        "id": ("VARCHAR(36)", False, True),
        "legacy_subject": ("VARCHAR(255)", True, False),
    },
    "workspaces": {
        "id": ("VARCHAR(36)", False, True),
        "kind": ("VARCHAR(16)", False, False),
        "personal_owner_user_id": ("VARCHAR(36)", True, False),
    },
    "workspace_memberships": {
        "workspace_id": ("VARCHAR(36)", False, True),
        "user_id": ("VARCHAR(36)", False, True),
        "role": ("VARCHAR(16)", False, False),
    },
    "quick_transcriptions": {
        "id": ("VARCHAR(36)", False, True),
        "owner_subject": ("VARCHAR(255)", False, False),
        "job_id": ("VARCHAR(36)", True, False),
    },
    "jobs": {
        "id": ("VARCHAR(36)", False, True),
        "kind": ("VARCHAR(120)", False, False),
        "workspace_id": ("VARCHAR(36)", False, False),
        "project_id": ("VARCHAR(36)", True, False),
        "session_id": ("VARCHAR(36)", True, False),
        "workflow_run_id": ("VARCHAR(36)", True, False),
    },
    "job_actors": {
        "job_id": ("VARCHAR(36)", False, True),
        "user_id": ("VARCHAR(36)", True, False),
        "auth_revision": ("INTEGER", True, False),
        "context_json": ("JSON", False, False),
    },
    "api_tokens": {
        "id": ("VARCHAR(36)", False, True),
        "subject": ("VARCHAR(200)", True, False),
        "user_id": ("VARCHAR(36)", True, False),
        "user_auth_revision": ("INTEGER", True, False),
    },
    "automation_clients": {
        "id": ("VARCHAR(36)", False, True),
        "subject": ("VARCHAR(200)", False, False),
        "user_id": ("VARCHAR(36)", True, False),
        "user_auth_revision": ("INTEGER", True, False),
    },
    "browser_auth_sessions": {
        "id": ("VARCHAR(36)", False, True),
        "subject": ("VARCHAR(200)", False, False),
        "user_id": ("VARCHAR(36)", False, False),
        "auth_revision": ("INTEGER", False, False),
    },
}
_REQUIRED_PRIMARY_KEYS = {
    "users": ("id",),
    "workspaces": ("id",),
    "workspace_memberships": ("workspace_id", "user_id"),
    "quick_transcriptions": ("id",),
    "jobs": ("id",),
    "job_actors": ("job_id",),
    "api_tokens": ("id",),
    "automation_clients": ("id",),
    "browser_auth_sessions": ("id",),
}
_REQUIRED_FOREIGN_KEYS = (
    ("workspaces", ("personal_owner_user_id",), "users", ("id",), "RESTRICT"),
    (
        "workspace_memberships",
        ("workspace_id",),
        "workspaces",
        ("id",),
        "CASCADE",
    ),
    ("workspace_memberships", ("user_id",), "users", ("id",), "RESTRICT"),
    (
        "quick_transcriptions",
        ("job_id",),
        "jobs",
        ("id",),
        "SET NULL",
    ),
    ("jobs", ("workspace_id",), "workspaces", ("id",), "RESTRICT"),
    ("job_actors", ("job_id",), "jobs", ("id",), "CASCADE"),
    ("job_actors", ("user_id",), "users", ("id",), "RESTRICT"),
    ("api_tokens", ("user_id",), "users", ("id",), "RESTRICT"),
    (
        "automation_clients",
        ("user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
    (
        "browser_auth_sessions",
        ("user_id",),
        "users",
        ("id",),
        "RESTRICT",
    ),
)


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Private transcription ownership is supported only on SQLite.")
    return bind


def _quote(bind: sa.Connection, identifier: str) -> str:
    return bind.dialect.identifier_preparer.quote(identifier)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, RowMapping]:
    quoted_table = _quote(bind, table_name)
    rows = bind.exec_driver_sql(f"PRAGMA table_info({quoted_table})").mappings().all()
    return {str(row["name"]): row for row in rows}


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> tuple[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None], ...]:
    # SQLite's catalog retains actions when Inspector omits them on historical unnamed FKs.
    inspector_names: dict[tuple[tuple[str, ...], str, tuple[str, ...]], str | None] = {}
    ambiguous_names: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
        structure = (
            tuple(foreign_key.get("constrained_columns") or ()),
            str(foreign_key.get("referred_table") or ""),
            tuple(foreign_key.get("referred_columns") or ()),
        )
        if structure in inspector_names:
            ambiguous_names.add(structure)
        else:
            name = foreign_key.get("name")
            inspector_names[structure] = str(name) if name is not None else None

    quoted_table = _quote(bind, table_name)
    rows = bind.exec_driver_sql(f"PRAGMA foreign_key_list({quoted_table})").mappings().all()
    groups: dict[int, list[RowMapping]] = {}
    for row in rows:
        groups.setdefault(int(row["id"]), []).append(row)

    signatures = []
    for group in groups.values():
        ordered = sorted(group, key=lambda row: int(row["seq"]))
        structure = (
            tuple(str(row["from"]) for row in ordered),
            str(ordered[0]["table"]),
            tuple(str(row["to"]) for row in ordered),
        )
        name = None if structure in ambiguous_names else inspector_names.get(structure)
        signatures.append(
            (
                structure[0],
                structure[1],
                structure[2],
                str(ordered[0]["on_delete"] or "").upper(),
                name,
            )
        )
    return tuple(signatures)


def _has_unique_index(
    bind: sa.Connection,
    table_name: str,
    expected_columns: tuple[str, ...],
) -> bool:
    quoted_table = _quote(bind, table_name)
    for index in bind.exec_driver_sql(f"PRAGMA index_list({quoted_table})").all():
        if not bool(index[2]) or (len(index) > 4 and bool(index[4])):
            continue
        quoted_index = _quote(bind, str(index[1]))
        rows = bind.exec_driver_sql(f"PRAGMA index_info({quoted_index})").all()
        if any(row[2] is None for row in rows):
            continue
        if tuple(str(row[2]) for row in rows) == expected_columns:
            return True
    return False


def _validate_required_schema(bind: sa.Connection) -> None:
    available_tables = set(sa.inspect(bind).get_table_names())
    missing_tables = set(_REQUIRED_COLUMNS) - available_tables
    if missing_tables:
        raise RuntimeError(
            "Cannot create private transcription owners; a required table is missing."
        )

    for table_name, expected_columns in _REQUIRED_COLUMNS.items():
        columns = _columns(bind, table_name)
        if not set(expected_columns).issubset(columns):
            raise RuntimeError(
                "Cannot create private transcription owners; a required source column is missing."
            )

        primary_key = tuple(
            name
            for name, column in sorted(columns.items(), key=lambda item: int(item[1]["pk"]))
            if int(column["pk"]) > 0
        )
        if primary_key != _REQUIRED_PRIMARY_KEYS[table_name]:
            raise RuntimeError(
                "Cannot create private transcription owners; a required source key has drifted."
            )

        for column_name, (column_type, nullable, primary_key_column) in expected_columns.items():
            column = columns[column_name]
            if (
                str(column["type"]).upper() != column_type
                or bool(column["notnull"]) != (not nullable)
                or bool(column["pk"]) != primary_key_column
            ):
                raise RuntimeError(
                    "Cannot create private transcription owners; a required source column has drifted."
                )

    for table_name, columns, target_table, target_columns, ondelete in _REQUIRED_FOREIGN_KEYS:
        matches = [
            signature
            for signature in _foreign_key_signatures(bind, table_name)
            if signature[0] == columns
        ]
        if len(matches) != 1 or matches[0][1:4] != (target_table, target_columns, ondelete):
            raise RuntimeError(
                "Cannot create private transcription owners; a required source foreign key has drifted."
            )

    if not _has_unique_index(bind, "workspaces", ("personal_owner_user_id",)):
        raise RuntimeError(
            "Cannot create private transcription owners; personal workspace ownership is not unique."
        )
    if not _has_unique_index(bind, "quick_transcriptions", ("job_id",)):
        raise RuntimeError(
            "Cannot create private transcription owners; quick transcription job links are not unique."
        )


def _validate_foreign_key_integrity(bind: sa.Connection) -> None:
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError(
            "Cannot create private transcription owners; existing foreign-key integrity is invalid."
        )


def _reject_name_collisions(bind: sa.Connection) -> None:
    owned_names = (_OWNER_TABLE, *_OWNER_INDEXES, *_TRIGGER_SQL)
    for name in owned_names:
        collision = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if collision is not None:
            raise RuntimeError(
                "Cannot create private transcription owners; an owned schema name is already in use."
            )


def _create_owner_table() -> None:
    op.create_table(
        _OWNER_TABLE,
        sa.Column("transcription_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["transcription_id"],
            ["quick_transcriptions.id"],
            name="fk_quick_transcription_owners_transcription_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_quick_transcription_owners_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_quick_transcription_owners_workspace_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("transcription_id"),
    )
    op.create_index(_OWNER_INDEXES[0], _OWNER_TABLE, ["user_id"], unique=False)
    op.create_index(_OWNER_INDEXES[1], _OWNER_TABLE, ["workspace_id"], unique=False)


def _nonblank_subject(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _add_proof(
    proofs: dict[str, set[str]], subject: object, user_id: object, users: set[str]
) -> None:
    if (
        isinstance(subject, str)
        and _nonblank_subject(subject)
        and isinstance(user_id, str)
        and user_id in users
    ):
        proofs.setdefault(subject, set()).add(user_id)


def _effective_api_subject(subject: object, token_id: str) -> object:
    if subject:
        return subject
    return f"api-token:{token_id}"


def _candidate_users(bind: sa.Connection, users: set[str]) -> dict[str, set[str]]:
    proofs: dict[str, set[str]] = {}
    for row in bind.exec_driver_sql("SELECT id, legacy_subject FROM users").mappings():
        _add_proof(proofs, row["legacy_subject"], row["id"], users)

    for table_name in ("api_tokens", "automation_clients"):
        rows = bind.exec_driver_sql(
            f"SELECT id, subject, user_id, user_auth_revision FROM {table_name}"
        ).mappings()
        for row in rows:
            user_id = row["user_id"]
            if user_id not in users or not _positive_integer(row["user_auth_revision"]):
                continue
            subject = row["subject"]
            if table_name == "api_tokens":
                subject = _effective_api_subject(subject, str(row["id"]))
            _add_proof(proofs, subject, user_id, users)

    for row in bind.exec_driver_sql(
        "SELECT id, subject, user_id, auth_revision FROM browser_auth_sessions"
    ).mappings():
        if row["user_id"] not in users or not _positive_integer(row["auth_revision"]):
            continue
        _add_proof(proofs, row["subject"], row["user_id"], users)

    return proofs


def _actor_candidates_by_transcription(bind: sa.Connection, users: set[str]) -> dict[str, set[str]]:
    proofs: dict[str, set[str]] = {}
    transcriptions_by_job: dict[str, list[tuple[str, str]]] = {}
    for row in bind.exec_driver_sql(
        "SELECT id, owner_subject, job_id FROM quick_transcriptions"
    ).mappings():
        if row["job_id"] is not None:
            transcriptions_by_job.setdefault(str(row["job_id"]), []).append(
                (str(row["id"]), str(row["owner_subject"]))
            )

    for row in bind.exec_driver_sql(
        "SELECT job_id, user_id, auth_revision, context_json FROM job_actors"
    ).mappings():
        user_id = row["user_id"]
        if user_id not in users or not _positive_integer(row["auth_revision"]):
            continue
        try:
            context = json.loads(row["context_json"])
        except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
            continue
        if (
            not isinstance(context, dict)
            or type(context.get("version")) is not int
            or context.get("version") != 1
            or context.get("source") != "user"
        ):
            continue
        actor_subject = context.get("subject")
        if not _nonblank_subject(actor_subject):
            continue
        for transcription_id, owner_subject in transcriptions_by_job.get(str(row["job_id"]), ()):
            if actor_subject == owner_subject:
                proofs.setdefault(transcription_id, set()).add(str(user_id))

    return proofs


def _personal_workspaces(bind: sa.Connection) -> tuple[dict[str, list[str]], set[tuple[str, str]]]:
    workspace_ids: dict[str, list[str]] = {}
    for row in bind.exec_driver_sql(
        """SELECT id, personal_owner_user_id
FROM workspaces
WHERE kind = 'personal' AND personal_owner_user_id IS NOT NULL"""
    ).mappings():
        workspace_ids.setdefault(str(row["personal_owner_user_id"]), []).append(str(row["id"]))
    memberships = {
        (str(row["workspace_id"]), str(row["user_id"]))
        for row in bind.exec_driver_sql(
            "SELECT workspace_id, user_id FROM workspace_memberships WHERE role = 'owner'"
        ).mappings()
    }
    return workspace_ids, memberships


def _private_job_shape(job: RowMapping | None, workspace_id: str) -> bool:
    return bool(
        job is not None
        and job["kind"] == "transcription.quick"
        and job["workspace_id"] == workspace_id
        and job["project_id"] is None
        and job["session_id"] is None
        and job["workflow_run_id"] is None
    )


def _backfill_owners(bind: sa.Connection) -> None:
    user_ids = {str(row["id"]) for row in bind.exec_driver_sql("SELECT id FROM users").mappings()}
    proofs = _candidate_users(bind, user_ids)
    actor_proofs = _actor_candidates_by_transcription(bind, user_ids)
    workspace_ids, owner_memberships = _personal_workspaces(bind)
    jobs = {
        str(row["id"]): row
        for row in bind.exec_driver_sql(
            """SELECT id, kind, workspace_id, project_id, session_id, workflow_run_id
FROM jobs"""
        ).mappings()
    }

    for transcription in bind.exec_driver_sql(
        "SELECT id, owner_subject, job_id FROM quick_transcriptions ORDER BY id"
    ).mappings():
        subject = transcription["owner_subject"]
        if not _nonblank_subject(subject):
            continue
        candidates = proofs.get(subject, set()) | actor_proofs.get(str(transcription["id"]), set())
        if len(candidates) != 1:
            continue
        user_id = next(iter(candidates))
        workspaces = workspace_ids.get(user_id, ())
        if len(workspaces) != 1:
            continue
        workspace_id = workspaces[0]
        if (workspace_id, user_id) not in owner_memberships:
            continue
        if transcription["job_id"] is not None:
            job = jobs.get(str(transcription["job_id"]))
            if not _private_job_shape(job, workspace_id):
                continue
        bind.exec_driver_sql(
            """INSERT INTO quick_transcription_owners
(transcription_id, user_id, workspace_id) VALUES (?, ?, ?)""",
            (str(transcription["id"]), user_id, workspace_id),
        )


def upgrade() -> None:
    bind = _sqlite_bind()
    _validate_required_schema(bind)
    _validate_foreign_key_integrity(bind)
    _reject_name_collisions(bind)
    _create_owner_table()
    for trigger_sql in _TRIGGER_SQL.values():
        bind.exec_driver_sql(trigger_sql)
    _backfill_owners(bind)


def _normalize_sql(value: object) -> str:
    # SQL formatting/identifier quotes are incidental; literal values are not.
    parts = re.split(r"('(?:''|[^'])*')", str(value or ""))
    normalized = "".join(
        part if index % 2 else re.sub(r"[\s\"`\[\]]+", "", part).casefold()
        for index, part in enumerate(parts)
    )
    return normalized.rstrip(";")


def _validate_owned_table(bind: sa.Connection) -> None:
    quoted_table = _quote(bind, _OWNER_TABLE)
    column_rows = bind.exec_driver_sql(f"PRAGMA table_xinfo({quoted_table})").all()
    if tuple(str(row[1]) for row in column_rows) != (
        "transcription_id",
        "user_id",
        "workspace_id",
    ):
        raise RuntimeError(
            "Cannot downgrade private transcription owners; the owned table columns have drifted."
        )
    for row, expected_pk in zip(column_rows, (1, 0, 0), strict=True):
        if (
            str(row[2]).upper() != "VARCHAR(36)"
            or not bool(row[3])
            or row[4] is not None
            or int(row[5]) != expected_pk
            or int(row[6]) != 0
        ):
            raise RuntimeError(
                "Cannot downgrade private transcription owners; an owned column has drifted."
            )

    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _OWNER_TABLE},
    ).scalar_one()
    if _normalize_sql(table_sql) != _normalize_sql(_OWNER_TABLE_SQL):
        raise RuntimeError(
            "Cannot downgrade private transcription owners; the owned table definition has drifted."
        )

    expected_foreign_keys = {
        (
            ("transcription_id",),
            "quick_transcriptions",
            ("id",),
            "CASCADE",
            "fk_quick_transcription_owners_transcription_id",
        ),
        (
            ("user_id",),
            "users",
            ("id",),
            "RESTRICT",
            "fk_quick_transcription_owners_user_id",
        ),
        (
            ("workspace_id",),
            "workspaces",
            ("id",),
            "RESTRICT",
            "fk_quick_transcription_owners_workspace_id",
        ),
    }
    actual_foreign_keys = _foreign_key_signatures(bind, _OWNER_TABLE)
    if len(actual_foreign_keys) != 3 or set(actual_foreign_keys) != expected_foreign_keys:
        raise RuntimeError(
            "Cannot downgrade private transcription owners; owned foreign keys have drifted."
        )

    index_rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name"
        ),
        {"table_name": _OWNER_TABLE},
    ).all()
    explicit_indexes = {str(name): sql for name, sql in index_rows if sql is not None}
    if set(explicit_indexes) != set(_OWNER_INDEXES):
        raise RuntimeError(
            "Cannot downgrade private transcription owners; owned indexes have drifted."
        )
    implicit_indexes = [name for name, sql in index_rows if sql is None]
    if len(implicit_indexes) != 1:
        raise RuntimeError(
            "Cannot downgrade private transcription owners; the primary-key index has drifted."
        )
    if any(
        _normalize_sql(explicit_indexes[name]) != _normalize_sql(expected_sql)
        for name, expected_sql in _INDEX_SQL.items()
    ):
        raise RuntimeError(
            "Cannot downgrade private transcription owners; owned index definitions have drifted."
        )
    for index_name, column_name in zip(_OWNER_INDEXES, ("user_id", "workspace_id"), strict=True):
        quoted_index = _quote(bind, index_name)
        indexed_columns = tuple(
            str(row[2])
            for row in bind.exec_driver_sql(f"PRAGMA index_info({quoted_index})").all()
            if row[2] is not None
        )
        index_list = bind.exec_driver_sql(f"PRAGMA index_list({quoted_table})").all()
        index_info = next((row for row in index_list if str(row[1]) == index_name), None)
        if (
            indexed_columns != (column_name,)
            or index_info is None
            or bool(index_info[2])
            or (len(index_info) > 4 and bool(index_info[4]))
        ):
            raise RuntimeError(
                "Cannot downgrade private transcription owners; an owned index has drifted."
            )

    attached_triggers = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = :table_name AND name NOT IN (:trigger_a, :trigger_b) LIMIT 1"
        ),
        {
            "table_name": _OWNER_TABLE,
            "trigger_a": "quick_transcription_owners_scope_insert",
            "trigger_b": "quick_transcription_owners_scope_update",
        },
    ).first()
    if attached_triggers is not None:
        raise RuntimeError(
            "Cannot downgrade private transcription owners; an unexpected attached trigger exists."
        )


def _validate_owned_triggers(bind: sa.Connection) -> None:
    trigger_names = tuple(_TRIGGER_SQL)
    placeholders = ", ".join(f":name_{index}" for index in range(len(trigger_names)))
    parameters = {f"name_{index}": name for index, name in enumerate(trigger_names)}
    rows = (
        bind.execute(
            sa.text(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                f"WHERE type = 'trigger' AND name IN ({placeholders})"
            ),
            parameters,
        )
        .mappings()
        .all()
    )
    actual = {str(row["name"]): row for row in rows}
    if set(actual) != set(_TRIGGER_SQL):
        raise RuntimeError(
            "Cannot downgrade private transcription owners; owned trigger definitions have drifted."
        )
    for name, expected_sql in _TRIGGER_SQL.items():
        row = actual[name]
        if str(row["tbl_name"]) != _TRIGGER_TABLES[name] or _normalize_sql(
            row["sql"]
        ) != _normalize_sql(expected_sql):
            raise RuntimeError(
                "Cannot downgrade private transcription owners; owned trigger definitions have drifted."
            )


def _validate_owned_schema(bind: sa.Connection) -> None:
    if _OWNER_TABLE not in set(sa.inspect(bind).get_table_names()):
        raise RuntimeError(
            "Cannot downgrade private transcription owners; the owned table is missing."
        )
    _validate_owned_table(bind)
    _validate_owned_triggers(bind)


def downgrade() -> None:
    bind = _sqlite_bind()
    _validate_owned_schema(bind)
    for trigger_name in _TRIGGER_SQL:
        op.execute(sa.text(f"DROP TRIGGER {trigger_name}"))
    for index_name in _OWNER_INDEXES:
        op.drop_index(index_name, table_name=_OWNER_TABLE)
    op.drop_table(_OWNER_TABLE)
