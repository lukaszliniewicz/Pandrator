"""Adopt retained workspace and project ownership for resumable uploads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0065_upload_ownership"
down_revision = "0064_voice_annotations"
branch_labels = None
depends_on = None

_TABLE = "upload_sessions"
_WORKSPACE_FK = "fk_upload_sessions_workspace_id_workspaces"
_PROJECT_FK = "fk_upload_sessions_project_id_projects"
_WORKSPACE_INDEX = "ix_upload_sessions_workspace_id"
_PROJECT_INDEX = "ix_upload_sessions_project_id"
_SCOPE_COLUMNS = ("workspace_id", "project_id")
_OLD_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool]] = {
    "id": (sa.String, 36, False),
    "session_id": (sa.String, 36, True),
    "filename": (sa.String, 255, False),
    "mime_type": (sa.String, 160, True),
    "size_bytes": (sa.Integer, None, False),
    "chunk_size": (sa.Integer, None, False),
    "chunk_count": (sa.Integer, None, False),
    "received_json": (sa.JSON, None, False),
    "expected_hash": (sa.String, 128, True),
    "temporary_relative_path": (sa.Text, None, False),
    "state": (sa.String, 32, False),
    "result_json": (sa.JSON, None, True),
    "created_at": (sa.DateTime, None, False),
    "updated_at": (sa.DateTime, None, False),
    "expires_at": (sa.DateTime, None, False),
}
_OLD_INDEXES = {
    "session_id": "ix_upload_sessions_session_id",
    "state": "ix_upload_sessions_state",
    "expires_at": "ix_upload_sessions_expires_at",
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {
        "artifacts",
        "projects",
        "sessions",
        "source_assets",
        "users",
        "workspace_memberships",
        "workspaces",
        _TABLE,
    }
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt upload ownership; required tables are missing: "
            + ", ".join(sorted(missing))
        )

    required_columns = {
        "artifacts": {"id", "workspace_id", "project_id", "session_id"},
        "projects": {"id", "workspace_id", "source_session_id"},
        "sessions": {"id", "project_id"},
        "source_assets": {"id", "artifact_id", "workspace_id"},
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
        _TABLE: set(_OLD_COLUMNS),
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                "Cannot adopt upload ownership; "
                f"{table_name} is missing columns: " + ", ".join(sorted(missing_columns))
            )

    if "translation_projects" in _table_names(bind):
        missing_columns = {"source_session_id", "project_root_id"} - set(
            _columns(bind, "translation_projects")
        )
        if missing_columns:
            raise RuntimeError(
                "Cannot adopt upload ownership; translation_projects is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_key_signatures(
    bind: sa.Connection,
) -> list[tuple[tuple[str, ...], str, tuple[str, ...], str | None, str | None]]:
    result = []
    for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE):
        result.append(
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (foreign_key.get("options") or {}).get("ondelete", "").upper() or None,
                foreign_key.get("name"),
            )
        )
    return result


def _validate_scope_column(
    bind: sa.Connection,
    *,
    column_name: str,
    foreign_key_name: str,
    referred_table: str,
    index_name: str,
) -> None:
    column = _columns(bind, _TABLE).get(column_name)
    if (
        column is None
        or not column["nullable"]
        or not isinstance(column["type"], sa.String)
        or column["type"].length != 36
        or column.get("default") is not None
        or column.get("primary_key")
    ):
        raise RuntimeError(
            f"Cannot adopt upload ownership; {_TABLE}.{column_name} has an "
            "unexpected column definition."
        )

    foreign_keys = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
        if column_name in foreign_key["constrained_columns"]
    ]
    if len(foreign_keys) != 1:
        raise RuntimeError(
            f"Cannot adopt upload ownership; {_TABLE}.{column_name} must have "
            "exactly one ownership foreign key."
        )
    foreign_key = foreign_keys[0]
    if (
        foreign_key.get("name") != foreign_key_name
        or foreign_key["constrained_columns"] != [column_name]
        or foreign_key["referred_table"] != referred_table
        or foreign_key["referred_columns"] != ["id"]
        or (foreign_key.get("options") or {}).get("ondelete", "").upper() != "RESTRICT"
    ):
        raise RuntimeError(
            f"Cannot adopt upload ownership; {_TABLE}.{column_name} has an unexpected foreign key."
        )

    indexes = [
        index
        for index in sa.inspect(bind).get_indexes(_TABLE)
        if column_name in (index.get("column_names") or ())
    ]
    if (
        len(indexes) != 1
        or indexes[0].get("name") != index_name
        or indexes[0].get("column_names") != [column_name]
        or indexes[0].get("unique")
    ):
        raise RuntimeError(
            f"Cannot adopt upload ownership; {_TABLE}.{column_name} has a "
            "malformed ownership index."
        )


def _validate_upload_schema(bind: sa.Connection, *, target: bool) -> None:
    columns = _columns(bind, _TABLE)
    present_scope = {name for name in _SCOPE_COLUMNS if name in columns}
    if target and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError(
            "Cannot adopt upload ownership; only part of the ownership scope schema exists."
        )
    if not target and present_scope:
        raise RuntimeError("Cannot adopt upload ownership; upload scope columns already exist.")

    expected_columns = set(_OLD_COLUMNS) | (set(_SCOPE_COLUMNS) if target else set())
    if set(columns) != expected_columns:
        raise RuntimeError(
            "Cannot adopt upload ownership; upload_sessions has a partial or malformed "
            "column shape."
        )

    for name, (type_class, length, nullable) in _OLD_COLUMNS.items():
        column = columns[name]
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or column.get("default") is not None
            or column.get("primary_key", False) != (name == "id")
        ):
            raise RuntimeError(
                f"Cannot adopt upload ownership; upload_sessions.{name} has an "
                "unexpected legacy definition."
            )

    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != ("id",):
        raise RuntimeError(
            "Cannot adopt upload ownership; upload_sessions has an unexpected primary key."
        )

    expected_foreign_keys: set[tuple[tuple[str, ...], str, tuple[str, ...], str, str | None]] = {
        (("session_id",), "sessions", ("id",), "CASCADE", None),
    }
    if target:
        expected_foreign_keys.update(
            {
                (("workspace_id",), "workspaces", ("id",), "RESTRICT", _WORKSPACE_FK),
                (("project_id",), "projects", ("id",), "RESTRICT", _PROJECT_FK),
            }
        )
    actual_foreign_keys = set(_foreign_key_signatures(bind))
    if actual_foreign_keys != expected_foreign_keys:
        raise RuntimeError(
            "Cannot adopt upload ownership; upload_sessions has an unexpected foreign key shape."
        )

    indexes = sa.inspect(bind).get_indexes(_TABLE)
    for column_name, index_name in _OLD_INDEXES.items():
        matches = [index for index in indexes if index.get("name") == index_name]
        if (
            len(matches) != 1
            or matches[0].get("column_names") != [column_name]
            or matches[0].get("unique")
        ):
            raise RuntimeError(
                f"Cannot adopt upload ownership; upload_sessions.{column_name} has an "
                "unexpected legacy index."
            )

    if target:
        _validate_scope_column(
            bind,
            column_name="workspace_id",
            foreign_key_name=_WORKSPACE_FK,
            referred_table="workspaces",
            index_name=_WORKSPACE_INDEX,
        )
        _validate_scope_column(
            bind,
            column_name="project_id",
            foreign_key_name=_PROJECT_FK,
            referred_table="projects",
            index_name=_PROJECT_INDEX,
        )
    else:
        for column_name in _SCOPE_COLUMNS:
            if any(
                column_name in foreign_key[0] for foreign_key in _foreign_key_signatures(bind)
            ) or any(column_name in (index.get("column_names") or ()) for index in indexes):
                raise RuntimeError(
                    "Cannot adopt upload ownership; a scope foreign key or index "
                    "exists without its column."
                )


def _validate_empty_target(bind: sa.Connection) -> None:
    populated = bind.execute(
        sa.text(
            "SELECT id FROM upload_sessions "
            "WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL "
            "ORDER BY id LIMIT 1"
        )
    ).first()
    if populated is not None:
        raise RuntimeError(
            "Cannot adopt upload ownership; upload_sessions already contains scope "
            f"data on row {populated[0]!r}."
        )


def _workspace_state(bind: sa.Connection, workspace_id: str) -> str:
    row = (
        bind.execute(
            sa.text("SELECT id, state FROM workspaces WHERE id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["state"] not in {"active", "archived"}:
        raise RuntimeError(
            f"Cannot adopt upload ownership; workspace {workspace_id!r} is missing or invalid."
        )
    return str(row["state"])


def _project_owner(bind: sa.Connection, project_id: str) -> tuple[str, str]:
    row = (
        bind.execute(
            sa.text(
                "SELECT projects.id, projects.workspace_id, projects.source_session_id, "
                "root.id AS found_root_session_id, root.project_id AS root_project_id "
                "FROM projects "
                "LEFT JOIN sessions AS root ON root.id = projects.source_session_id "
                "WHERE projects.id = :project_id"
            ),
            {"project_id": project_id},
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or row["workspace_id"] is None
        or row["source_session_id"] is None
        or row["found_root_session_id"] is None
        or row["root_project_id"] != row["id"]
    ):
        raise RuntimeError(
            f"Cannot adopt upload ownership; project {project_id!r} has no valid retained root."
        )
    workspace_id = str(row["workspace_id"])
    _workspace_state(bind, workspace_id)

    if "translation_projects" in _table_names(bind):
        translation_roots = (
            bind.execute(
                sa.text(
                    "SELECT project_root_id FROM translation_projects "
                    "WHERE source_session_id = :source_session_id ORDER BY id"
                ),
                {"source_session_id": row["source_session_id"]},
            )
            .scalars()
            .all()
        )
        if any(root is not None and root != project_id for root in translation_roots):
            raise RuntimeError(
                "Cannot adopt upload ownership; project root for source session "
                f"{row['source_session_id']!r} conflicts with translation ownership."
            )
    return project_id, workspace_id


def _session_owner(bind: sa.Connection, session_id: str) -> tuple[str, str]:
    row = (
        bind.execute(
            sa.text("SELECT id, project_id FROM sessions WHERE id = :session_id"),
            {"session_id": session_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["project_id"] is None:
        raise RuntimeError(
            f"Cannot adopt upload ownership; session {session_id!r} has no retained project."
        )
    return _project_owner(bind, str(row["project_id"]))


def _artifact_workspace(
    bind: sa.Connection,
    artifact_id: str,
    *,
    required_project_id: str | None = None,
) -> str:
    row = (
        bind.execute(
            sa.text(
                "SELECT id, workspace_id, project_id, session_id FROM artifacts "
                "WHERE id = :artifact_id"
            ),
            {"artifact_id": artifact_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["workspace_id"] is None:
        raise RuntimeError(
            f"Cannot adopt upload ownership; result artifact {artifact_id!r} is missing or unowned."
        )
    workspace_id = str(row["workspace_id"])
    _workspace_state(bind, workspace_id)

    artifact_project_id = row["project_id"]
    if artifact_project_id is not None:
        _, project_workspace_id = _project_owner(bind, str(artifact_project_id))
        if project_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt upload ownership; result artifact {artifact_id!r} "
                "and its retained project have different workspaces."
            )

    artifact_session_id = row["session_id"]
    if artifact_session_id is not None:
        session_project_id, session_workspace_id = _session_owner(bind, str(artifact_session_id))
        if artifact_project_id != session_project_id:
            raise RuntimeError(
                f"Cannot adopt upload ownership; result artifact {artifact_id!r} "
                "and its retained session have conflicting projects."
            )
        if session_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt upload ownership; result artifact {artifact_id!r} "
                "and its retained session have different workspaces."
            )

    if required_project_id is not None and row["project_id"] != required_project_id:
        raise RuntimeError(
            f"Cannot adopt upload ownership; result artifact {artifact_id!r} "
            f"does not retain project {required_project_id!r}."
        )
    return workspace_id


def _nonblank_id(value: Any, *, field_name: str, upload_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"Cannot adopt upload {upload_id!r}; result {field_name} must be a non-empty string."
        )
    return value


def _result_object(raw_result: Any, *, upload_id: str) -> Mapping[str, Any] | None:
    if raw_result is None:
        return None
    try:
        result = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Cannot adopt upload {upload_id!r}; result_json is malformed."
        ) from error
    if not isinstance(result, dict):
        raise RuntimeError(
            f"Cannot adopt upload {upload_id!r}; a non-null result_json must be an object."
        )
    return result or None


def _result_workspace_evidence(
    bind: sa.Connection,
    *,
    upload_id: str,
    session_owner: tuple[str, str] | None,
    raw_result: Any,
) -> set[str]:
    result = _result_object(raw_result, upload_id=upload_id)
    if result is None:
        return set()

    if "upload_id" in result and result["upload_id"] != upload_id:
        raise RuntimeError(
            f"Cannot adopt upload {upload_id!r}; result upload_id does not match its row."
        )

    artifact_id: str | None = None
    if "artifact_id" in result:
        artifact_id = _nonblank_id(
            result["artifact_id"], field_name="artifact_id", upload_id=upload_id
        )

    asset_id: str | None = None
    if "source_asset_id" in result:
        asset_id = _nonblank_id(
            result["source_asset_id"], field_name="source_asset_id", upload_id=upload_id
        )

    evidence: set[str] = set()
    if artifact_id is not None:
        artifact_workspace = _artifact_workspace(
            bind,
            artifact_id,
            required_project_id=session_owner[0] if session_owner is not None else None,
        )
        if session_owner is not None and artifact_workspace != session_owner[1]:
            raise RuntimeError(
                f"Cannot adopt upload {upload_id!r}; result artifact workspace conflicts "
                "with its session owner."
            )
        evidence.add(artifact_workspace)

    if asset_id is not None:
        asset = (
            bind.execute(
                sa.text(
                    "SELECT id, workspace_id, artifact_id FROM source_assets WHERE id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            .mappings()
            .first()
        )
        if asset is None or asset["workspace_id"] is None:
            raise RuntimeError(
                f"Cannot adopt upload {upload_id!r}; result source asset {asset_id!r} "
                "is missing or unowned."
            )
        asset_workspace = str(asset["workspace_id"])
        _workspace_state(bind, asset_workspace)

        backing_artifact_id = asset["artifact_id"]
        if backing_artifact_id is not None:
            backing_artifact_id = _nonblank_id(
                backing_artifact_id,
                field_name="source asset artifact_id",
                upload_id=upload_id,
            )
            backing_workspace = _artifact_workspace(
                bind,
                backing_artifact_id,
                required_project_id=session_owner[0] if session_owner is not None else None,
            )
            if backing_workspace != asset_workspace:
                raise RuntimeError(
                    f"Cannot adopt upload {upload_id!r}; source asset {asset_id!r} "
                    "and its backing artifact have different workspaces."
                )
            if artifact_id is not None and backing_artifact_id != artifact_id:
                raise RuntimeError(
                    f"Cannot adopt upload {upload_id!r}; source asset {asset_id!r} "
                    "does not reference its result artifact."
                )

        if session_owner is not None and asset_workspace != session_owner[1]:
            raise RuntimeError(
                f"Cannot adopt upload {upload_id!r}; result source asset workspace "
                "conflicts with its session owner."
            )
        evidence.add(asset_workspace)

    if len(evidence) > 1:
        raise RuntimeError(
            f"Cannot adopt upload {upload_id!r}; its result references conflicting workspaces."
        )
    return evidence


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
    owners = (
        bind.execute(
            sa.text(
                "SELECT id, is_active, is_instance_admin FROM users "
                "WHERE legacy_subject = 'owner' ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    if len(owners) != 1 or not owners[0]["is_active"] or not owners[0]["is_instance_admin"]:
        raise RuntimeError(
            "Cannot adopt unbound uploads; the seeded owner user is missing, ambiguous, "
            "inactive, or not an instance admin."
        )
    owner_id = str(owners[0]["id"])
    workspaces = (
        bind.execute(
            sa.text(
                "SELECT id, state FROM workspaces "
                "WHERE kind = 'personal' AND personal_owner_user_id = :owner_id ORDER BY id"
            ),
            {"owner_id": owner_id},
        )
        .mappings()
        .all()
    )
    if len(workspaces) != 1 or workspaces[0]["state"] != "active":
        raise RuntimeError(
            "Cannot adopt unbound uploads; the seeded owner personal workspace is "
            "missing, ambiguous, or inactive."
        )
    workspace_id = str(workspaces[0]["id"])
    membership = bind.execute(
        sa.text(
            "SELECT role FROM workspace_memberships "
            "WHERE workspace_id = :workspace_id AND user_id = :owner_id"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    ).scalar_one_or_none()
    if membership != "owner":
        raise RuntimeError(
            "Cannot adopt unbound uploads; the seeded owner workspace membership "
            "is missing or invalid."
        )
    return workspace_id


def _load_adoption_plan(bind: sa.Connection) -> dict[str, tuple[str, str | None]]:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, session_id, CAST(result_json AS TEXT) AS result_json "
                "FROM upload_sessions ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    plan: dict[str, tuple[str, str | None]] = {}
    session_owners: dict[str, tuple[str, str]] = {}
    needs_fallback = False

    for row in rows:
        upload_id = str(row["id"])
        session_owner: tuple[str, str] | None = None
        if row["session_id"] is not None:
            session_id = str(row["session_id"])
            if session_id not in session_owners:
                session_owners[session_id] = _session_owner(bind, session_id)
            session_owner = session_owners[session_id]

        result_workspaces = _result_workspace_evidence(
            bind,
            upload_id=upload_id,
            session_owner=session_owner,
            raw_result=row["result_json"],
        )

        if session_owner is not None:
            project_id, workspace_id = session_owner
            if result_workspaces and result_workspaces != {workspace_id}:
                raise RuntimeError(
                    f"Cannot adopt upload {upload_id!r}; result workspace conflicts "
                    "with its session owner."
                )
            plan[upload_id] = (workspace_id, project_id)
        elif result_workspaces:
            plan[upload_id] = (next(iter(result_workspaces)), None)
        else:
            needs_fallback = True
            plan[upload_id] = ("", None)

    if needs_fallback:
        fallback_workspace_id = _initial_personal_workspace_id(bind)
        plan = {
            upload_id: (
                fallback_workspace_id if not workspace_id else workspace_id,
                project_id,
            )
            for upload_id, (workspace_id, project_id) in plan.items()
        }

    if len(plan) != len(rows) or any(not workspace_id for workspace_id, _ in plan.values()):
        raise RuntimeError("Upload ownership adoption did not cover every upload.")
    return plan


def _ensure_scope_schema(bind: sa.Connection, *, existing_scope: bool) -> None:
    if not existing_scope:
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
            batch.add_column(sa.Column("project_id", sa.String(36), nullable=True))
            batch.create_foreign_key(
                _WORKSPACE_FK,
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                _PROJECT_FK,
                "projects",
                ["project_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_index(_WORKSPACE_INDEX, ["workspace_id"])
            batch.create_index(_PROJECT_INDEX, ["project_id"])
    _validate_upload_schema(bind, target=True)


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    columns = _columns(bind, _TABLE)
    present_scope = {name for name in _SCOPE_COLUMNS if name in columns}
    if present_scope and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError(
            "Cannot adopt upload ownership; only part of the ownership scope schema exists."
        )
    existing_scope = bool(present_scope)
    _validate_upload_schema(bind, target=existing_scope)
    if existing_scope:
        _validate_empty_target(bind)

    # Validate every retained root and result reference before the first DDL.
    plan = _load_adoption_plan(bind)

    _ensure_scope_schema(bind, existing_scope=existing_scope)
    for upload_id, (workspace_id, project_id) in plan.items():
        result = bind.execute(
            sa.text(
                "UPDATE upload_sessions SET workspace_id = :workspace_id, "
                "project_id = :project_id WHERE id = :upload_id "
                "AND workspace_id IS NULL AND project_id IS NULL"
            ),
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "upload_id": upload_id,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"Cannot adopt upload {upload_id!r}; its scope changed during migration."
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in _table_names(bind):
        return
    columns = _columns(bind, _TABLE)
    present_scope = {name for name in _SCOPE_COLUMNS if name in columns}
    if not present_scope:
        return
    if present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError(
            "Cannot downgrade upload ownership; only part of the ownership scope schema exists."
        )
    _validate_upload_schema(bind, target=True)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_index(_PROJECT_INDEX)
        batch.drop_index(_WORKSPACE_INDEX)
        batch.drop_constraint(_PROJECT_FK, type_="foreignkey")
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_column("project_id")
        batch.drop_column("workspace_id")
