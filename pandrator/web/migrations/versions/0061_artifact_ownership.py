"""Adopt durable workspace and project ownership for historical artifacts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0061_artifact_ownership"
down_revision = "0060_source_workspace"
branch_labels = None
depends_on = None

_WORKSPACE_FK = "fk_artifacts_workspace_id_workspaces"
_PROJECT_FK = "fk_artifacts_project_id_projects"
_WORKSPACE_INDEX = "ix_artifacts_workspace_id"
_PROJECT_INDEX = "ix_artifacts_project_id"
_SCOPE_COLUMNS = ("workspace_id", "project_id")


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_info(bind: sa.Connection, table_name: str) -> Mapping[str, Mapping[str, Any]]:
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
    }
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt artifact ownership; required tables are missing: "
            + ", ".join(sorted(missing))
        )

    required_columns = {
        "artifacts": {"id", "session_id"},
        "projects": {"id", "workspace_id", "source_session_id"},
        "sessions": {"id", "project_id"},
        "source_assets": {"id", "artifact_id", "workspace_id"},
        "users": {
            "id",
            "legacy_subject",
            "is_active",
            "is_instance_admin",
        },
        "workspace_memberships": {"workspace_id", "user_id", "role"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_column_info(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot adopt artifact ownership; {table_name} is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_keys_for_column(bind: sa.Connection, column_name: str) -> list[Mapping[str, Any]]:
    return [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys("artifacts")
        if column_name in foreign_key["constrained_columns"]
    ]


def _indexes_for_column(bind: sa.Connection, column_name: str) -> list[Mapping[str, Any]]:
    return [
        index
        for index in sa.inspect(bind).get_indexes("artifacts")
        if column_name in (index["column_names"] or [])
    ]


def _validate_scope_column(
    bind: sa.Connection,
    *,
    column_name: str,
    foreign_key_name: str,
    referred_table: str,
    ondelete: str,
    index_name: str,
) -> None:
    column = _column_info(bind, "artifacts").get(column_name)
    if (
        column is None
        or not column["nullable"]
        or not isinstance(column["type"], sa.String)
        or column["type"].length != 36
        or column.get("default") is not None
        or column.get("autoincrement") is not None
        or column.get("primary_key")
    ):
        raise RuntimeError(
            f"Cannot adopt artifact ownership; artifacts.{column_name} has an "
            "unexpected column definition."
        )

    foreign_keys = _foreign_keys_for_column(bind, column_name)
    if len(foreign_keys) != 1:
        raise RuntimeError(
            f"Cannot adopt artifact ownership; artifacts.{column_name} must have "
            "exactly one ownership foreign key."
        )
    foreign_key = foreign_keys[0]
    if (
        foreign_key.get("name") != foreign_key_name
        or foreign_key["constrained_columns"] != [column_name]
        or foreign_key["referred_table"] != referred_table
        or foreign_key["referred_columns"] != ["id"]
        or (foreign_key.get("options") or {}).get("ondelete", "").upper() != ondelete
    ):
        raise RuntimeError(
            f"Cannot adopt artifact ownership; artifacts.{column_name} has an "
            "unexpected foreign key."
        )

    indexes = _indexes_for_column(bind, column_name)
    if len(indexes) != 1:
        raise RuntimeError(
            f"Cannot adopt artifact ownership; artifacts.{column_name} must have "
            "exactly one ownership index."
        )
    index = indexes[0]
    if (
        index.get("name") != index_name
        or index["column_names"] != [column_name]
        or index.get("unique")
    ):
        raise RuntimeError(
            f"Cannot adopt artifact ownership; artifacts.{column_name} has a "
            "malformed ownership index."
        )


def _validate_unadopted_scope(bind: sa.Connection) -> bool:
    columns = _column_info(bind, "artifacts")
    present = {name for name in _SCOPE_COLUMNS if name in columns}
    if present and present != set(_SCOPE_COLUMNS):
        raise RuntimeError(
            "Cannot adopt artifact ownership; only part of the ownership scope schema exists."
        )
    if not present:
        for column_name in _SCOPE_COLUMNS:
            if _foreign_keys_for_column(bind, column_name) or _indexes_for_column(
                bind, column_name
            ):
                raise RuntimeError(
                    "Cannot adopt artifact ownership; scope indexes or foreign keys "
                    "exist without their columns."
                )
        return False

    _validate_scope_column(
        bind,
        column_name="workspace_id",
        foreign_key_name=_WORKSPACE_FK,
        referred_table="workspaces",
        ondelete="RESTRICT",
        index_name=_WORKSPACE_INDEX,
    )
    _validate_scope_column(
        bind,
        column_name="project_id",
        foreign_key_name=_PROJECT_FK,
        referred_table="projects",
        ondelete="SET NULL",
        index_name=_PROJECT_INDEX,
    )
    populated = bind.execute(
        sa.text(
            "SELECT id FROM artifacts "
            "WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL "
            "ORDER BY id LIMIT 1"
        )
    ).first()
    if populated is not None:
        raise RuntimeError(
            "Cannot adopt artifact ownership; artifacts already contain partial or "
            f"preexisting scope data on row {populated[0]!r}."
        )
    return True


def _active_fallback_workspace_id(bind: sa.Connection) -> str:
    owners = (
        bind.execute(
            sa.text(
                "SELECT id, is_active, is_instance_admin FROM users WHERE legacy_subject = 'owner'"
            )
        )
        .mappings()
        .all()
    )
    if len(owners) != 1:
        raise RuntimeError(
            "Cannot adopt unbound artifacts; the seeded owner user is missing or ambiguous."
        )
    owner = owners[0]
    if not owner["is_active"] or not owner["is_instance_admin"]:
        raise RuntimeError(
            "Cannot adopt unbound artifacts; the seeded owner user is inactive or "
            "is not an instance admin."
        )

    workspaces = (
        bind.execute(
            sa.text(
                "SELECT id, state FROM workspaces "
                "WHERE kind = 'personal' AND personal_owner_user_id = :owner_id"
            ),
            {"owner_id": owner["id"]},
        )
        .mappings()
        .all()
    )
    if len(workspaces) != 1:
        raise RuntimeError(
            "Cannot adopt unbound artifacts; the seeded owner personal workspace "
            "is missing or ambiguous."
        )
    workspace = workspaces[0]
    if workspace["state"] != "active":
        raise RuntimeError(
            "Cannot adopt unbound artifacts; the seeded owner personal workspace is not active."
        )

    membership = bind.execute(
        sa.text(
            "SELECT role FROM workspace_memberships "
            "WHERE workspace_id = :workspace_id AND user_id = :owner_id"
        ),
        {"workspace_id": workspace["id"], "owner_id": owner["id"]},
    ).scalar_one_or_none()
    if membership != "owner":
        raise RuntimeError(
            "Cannot adopt unbound artifacts; the seeded owner workspace membership "
            "is missing or invalid."
        )
    return str(workspace["id"])


def _session_owner(bind: sa.Connection, session_id: str) -> tuple[str, str]:
    row = (
        bind.execute(
            sa.text(
                "SELECT sessions.id AS session_id, sessions.project_id, "
                "projects.id AS found_project_id, projects.workspace_id, "
                "projects.source_session_id, root.id AS found_root_session_id, "
                "root.project_id AS root_project_id, workspaces.id AS found_workspace_id, "
                "workspaces.state AS workspace_state "
                "FROM sessions "
                "LEFT JOIN projects ON projects.id = sessions.project_id "
                "LEFT JOIN sessions AS root ON root.id = projects.source_session_id "
                "LEFT JOIN workspaces ON workspaces.id = projects.workspace_id "
                "WHERE sessions.id = :session_id"
            ),
            {"session_id": session_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt artifact ownership; session {session_id!r} is missing.")
    if (
        row["project_id"] is None
        or row["found_project_id"] is None
        or row["source_session_id"] is None
        or row["found_root_session_id"] is None
        or row["root_project_id"] != row["found_project_id"]
        or row["found_workspace_id"] is None
        or row["workspace_state"] not in {"active", "archived"}
    ):
        raise RuntimeError(
            f"Cannot adopt artifact ownership; session {session_id!r} has no valid "
            "project/workspace root."
        )

    if "translation_projects" in _table_names(bind):
        columns = set(_column_info(bind, "translation_projects"))
        if {"source_session_id", "project_root_id"} <= columns:
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
            if any(
                translation_root is not None and translation_root != row["found_project_id"]
                for translation_root in translation_roots
            ):
                raise RuntimeError(
                    f"Cannot adopt artifact ownership; project root for session "
                    f"{session_id!r} conflicts with its translation root."
                )

    return str(row["found_project_id"]), str(row["workspace_id"])


def _load_adoption_plan(
    bind: sa.Connection,
) -> dict[str, tuple[str, str | None]]:
    artifact_rows = (
        bind.execute(sa.text("SELECT id, session_id FROM artifacts ORDER BY id")).mappings().all()
    )
    asset_rows = (
        bind.execute(
            sa.text(
                "SELECT source_assets.id AS asset_id, source_assets.artifact_id, "
                "source_assets.workspace_id, artifacts.id AS found_artifact_id, "
                "workspaces.id AS found_workspace_id, workspaces.state AS workspace_state "
                "FROM source_assets "
                "LEFT JOIN artifacts ON artifacts.id = source_assets.artifact_id "
                "LEFT JOIN workspaces ON workspaces.id = source_assets.workspace_id "
                "WHERE source_assets.artifact_id IS NOT NULL "
                "ORDER BY source_assets.id"
            )
        )
        .mappings()
        .all()
    )

    artifact_ids = {str(row["id"]) for row in artifact_rows}
    asset_workspaces: dict[str, set[str]] = defaultdict(set)
    for asset in asset_rows:
        artifact_id = str(asset["artifact_id"])
        if asset["found_artifact_id"] is None or artifact_id not in artifact_ids:
            raise RuntimeError(
                f"Cannot adopt artifact ownership; source asset {asset['asset_id']!r} "
                f"references missing artifact {artifact_id!r}."
            )
        if (
            asset["workspace_id"] is None
            or asset["found_workspace_id"] is None
            or asset["workspace_state"] not in {"active", "archived"}
        ):
            raise RuntimeError(
                f"Cannot adopt artifact ownership; source asset {asset['asset_id']!r} "
                "has missing or invalid workspace ownership."
            )
        asset_workspaces[artifact_id].add(str(asset["workspace_id"]))

    plan: dict[str, tuple[str, str | None]] = {}
    session_owners: dict[str, tuple[str, str]] = {}
    needs_fallback = False
    for artifact in artifact_rows:
        artifact_id = str(artifact["id"])
        candidates = asset_workspaces.get(artifact_id, set())
        project_id: str | None = None
        if artifact["session_id"] is not None:
            session_id = str(artifact["session_id"])
            if session_id not in session_owners:
                session_owners[session_id] = _session_owner(bind, session_id)
            project_id, session_workspace_id = session_owners[session_id]
            candidates.add(session_workspace_id)
        if len(candidates) > 1:
            raise RuntimeError(
                f"Cannot adopt artifact {artifact_id!r}; linked session and source "
                "workspaces disagree: " + ", ".join(sorted(candidates)) + "."
            )
        if candidates:
            workspace_id = next(iter(candidates))
        else:
            needs_fallback = True
            workspace_id = ""
        plan[artifact_id] = (workspace_id, project_id)

    if needs_fallback:
        fallback_workspace_id = _active_fallback_workspace_id(bind)
        plan = {
            artifact_id: (
                fallback_workspace_id if workspace_id == "" else workspace_id,
                project_id,
            )
            for artifact_id, (workspace_id, project_id) in plan.items()
        }

    if set(plan) != artifact_ids or any(not workspace_id for workspace_id, _ in plan.values()):
        raise RuntimeError("Artifact ownership adoption did not cover every artifact.")
    return plan


def _ensure_schema(bind: sa.Connection, existing_scope: bool) -> None:
    if not existing_scope:
        with op.batch_alter_table("artifacts") as batch:
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
                ondelete="SET NULL",
            )
            batch.create_index(_WORKSPACE_INDEX, ["workspace_id"])
            batch.create_index(_PROJECT_INDEX, ["project_id"])

    _validate_scope_column(
        bind,
        column_name="workspace_id",
        foreign_key_name=_WORKSPACE_FK,
        referred_table="workspaces",
        ondelete="RESTRICT",
        index_name=_WORKSPACE_INDEX,
    )
    _validate_scope_column(
        bind,
        column_name="project_id",
        foreign_key_name=_PROJECT_FK,
        referred_table="projects",
        ondelete="SET NULL",
        index_name=_PROJECT_INDEX,
    )


def _apply_adoption_plan(bind: sa.Connection, plan: dict[str, tuple[str, str | None]]) -> None:
    for artifact_id, (workspace_id, project_id) in plan.items():
        result = bind.execute(
            sa.text(
                "UPDATE artifacts SET workspace_id = :workspace_id, "
                "project_id = :project_id "
                "WHERE id = :artifact_id AND workspace_id IS NULL AND project_id IS NULL"
            ),
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "artifact_id": artifact_id,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Artifact {artifact_id!r} changed during ownership adoption.")


def _validate_adoption(bind: sa.Connection, plan: dict[str, tuple[str, str | None]]) -> None:
    actual_rows = (
        bind.execute(
            sa.text(
                "SELECT artifacts.id, artifacts.workspace_id, artifacts.project_id, "
                "workspaces.id AS found_workspace_id, projects.id AS found_project_id "
                "FROM artifacts "
                "LEFT JOIN workspaces ON workspaces.id = artifacts.workspace_id "
                "LEFT JOIN projects ON projects.id = artifacts.project_id "
                "ORDER BY artifacts.id"
            )
        )
        .mappings()
        .all()
    )
    if len(actual_rows) != len(plan):
        raise RuntimeError("Artifact ownership adoption changed the artifact count.")
    for row in actual_rows:
        artifact_id = str(row["id"])
        expected = plan.get(artifact_id)
        if (
            expected is None
            or row["workspace_id"] is None
            or row["found_workspace_id"] is None
            or row["workspace_id"] != expected[0]
            or row["project_id"] != expected[1]
            or (row["project_id"] is not None and row["found_project_id"] is None)
        ):
            raise RuntimeError(f"Artifact {artifact_id!r} has invalid adopted ownership.")


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    existing_scope = _validate_unadopted_scope(bind)
    adoption_plan = _load_adoption_plan(bind)

    _ensure_schema(bind, existing_scope)
    _apply_adoption_plan(bind, adoption_plan)
    _validate_adoption(bind, adoption_plan)


def downgrade() -> None:
    bind = op.get_bind()
    if "artifacts" not in _table_names(bind):
        return
    columns = _column_info(bind, "artifacts")
    present = {name for name in _SCOPE_COLUMNS if name in columns}
    if not present:
        return

    expected = {
        "workspace_id": (_WORKSPACE_FK, _WORKSPACE_INDEX),
        "project_id": (_PROJECT_FK, _PROJECT_INDEX),
    }
    indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("artifacts")
        if index["name"] is not None
    }
    foreign_keys = {
        str(foreign_key["name"]): foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys("artifacts")
        if foreign_key.get("name") is not None
    }

    with op.batch_alter_table("artifacts") as batch:
        for column_name in _SCOPE_COLUMNS:
            if column_name not in present:
                continue
            foreign_key_name, index_name = expected[column_name]
            if index_name in indexes:
                batch.drop_index(index_name)
            if foreign_key_name in foreign_keys:
                batch.drop_constraint(foreign_key_name, type_="foreignkey")
            batch.drop_column(column_name)
