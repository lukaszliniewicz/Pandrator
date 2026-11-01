"""Assign every source asset to its historical session workspace."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0060_source_workspace"
down_revision = "0059_project_ownership"
branch_labels = None
depends_on = None

_WORKSPACE_FK = "fk_source_assets_workspace_id_workspaces"
_WORKSPACE_INDEX = "ix_source_assets_workspace_id"


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_info(bind: sa.Connection, table_name: str) -> Mapping[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {
        "artifacts",
        "projects",
        "session_sources",
        "sessions",
        "source_assets",
        "sources",
        "users",
        "workspace_memberships",
        "workspaces",
    }
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt source ownership; required tables are missing: "
            + ", ".join(sorted(missing))
        )


def _reject_preexisting_source_ownership(bind: sa.Connection) -> None:
    columns = _column_info(bind, "source_assets")
    if "workspace_id" not in columns:
        return
    found = bind.execute(
        sa.text("SELECT id FROM source_assets WHERE workspace_id IS NOT NULL ORDER BY id LIMIT 1")
    ).first()
    if found is not None:
        raise RuntimeError(
            "Cannot adopt source ownership; source_assets.workspace_id "
            f"already contains data on asset {found[0]!r}."
        )


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
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
            "Cannot adopt sessionless source assets; the seeded owner user is missing or ambiguous."
        )
    owner = owners[0]
    if not owner["is_active"] or not owner["is_instance_admin"]:
        raise RuntimeError(
            "Cannot adopt sessionless source assets; the seeded owner user is inactive "
            "or is not an instance admin."
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
            "Cannot adopt sessionless source assets; the seeded owner personal workspace "
            "is missing or ambiguous."
        )
    workspace = workspaces[0]
    if workspace["state"] != "active":
        raise RuntimeError(
            "Cannot adopt sessionless source assets; the seeded owner personal workspace "
            "is not active."
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
            "Cannot adopt sessionless source assets; the seeded owner workspace "
            "membership is missing or invalid."
        )
    return str(workspace["id"])


def _load_adoption_plan(bind: sa.Connection, fallback_workspace_id: str) -> dict[str, str]:
    asset_rows = (
        bind.execute(sa.text("SELECT id, artifact_id FROM source_assets ORDER BY id"))
        .mappings()
        .all()
    )
    linked_sessions: dict[str, set[str]] = defaultdict(set)
    artifact_sessions: dict[str, set[str]] = defaultdict(set)
    artifact_to_assets: dict[str, list[str]] = defaultdict(list)
    asset_artifacts: dict[str, str | None] = {}

    artifact_rows = (
        bind.execute(
            sa.text(
                "SELECT source_assets.id AS asset_id, source_assets.artifact_id, "
                "artifacts.id AS found_artifact_id, artifacts.session_id "
                "FROM source_assets LEFT JOIN artifacts "
                "ON artifacts.id = source_assets.artifact_id "
                "ORDER BY source_assets.id"
            )
        )
        .mappings()
        .all()
    )
    for row in artifact_rows:
        asset_id = str(row["asset_id"])
        artifact_id = row["artifact_id"]
        asset_artifacts[asset_id] = None if artifact_id is None else str(artifact_id)
        if artifact_id is None:
            continue
        if row["found_artifact_id"] is None:
            raise RuntimeError(
                f"Source asset {row['asset_id']!r} references missing artifact {artifact_id!r}."
            )
        artifact_to_assets[str(artifact_id)].append(asset_id)
        if row["session_id"] is not None:
            artifact_sessions[str(artifact_id)].add(str(row["session_id"]))

    attachment_rows = (
        bind.execute(
            sa.text(
                "SELECT source_asset_id, session_id FROM session_sources "
                "ORDER BY source_asset_id, id"
            )
        )
        .mappings()
        .all()
    )
    for row in attachment_rows:
        asset_id = str(row["source_asset_id"])
        if asset_id in asset_artifacts:
            artifact_id = asset_artifacts[asset_id]
            if artifact_id is None:
                linked_sessions[asset_id].add(str(row["session_id"]))
            else:
                artifact_sessions[artifact_id].add(str(row["session_id"]))

    legacy_rows = (
        bind.execute(
            sa.text(
                "SELECT artifact_id, session_id FROM sources "
                "WHERE artifact_id IS NOT NULL ORDER BY artifact_id, id"
            )
        )
        .mappings()
        .all()
    )
    for row in legacy_rows:
        artifact_id = str(row["artifact_id"])
        if artifact_id in artifact_to_assets:
            artifact_sessions[artifact_id].add(str(row["session_id"]))

    for artifact_id, asset_ids in artifact_to_assets.items():
        for asset_id in asset_ids:
            linked_sessions[asset_id].update(artifact_sessions[artifact_id])

    session_rows = (
        bind.execute(
            sa.text(
                "SELECT sessions.id AS session_id, sessions.project_id, "
                "projects.id AS found_project_id, projects.workspace_id, "
                "workspaces.id AS found_workspace_id "
                "FROM sessions LEFT JOIN projects ON projects.id = sessions.project_id "
                "LEFT JOIN workspaces ON workspaces.id = projects.workspace_id"
            )
        )
        .mappings()
        .all()
    )
    session_owners: dict[str, str] = {}
    for row in session_rows:
        session_id = str(row["session_id"])
        if row["project_id"] is None:
            continue
        if row["found_project_id"] is not None and row["found_workspace_id"] is not None:
            session_owners[session_id] = str(row["workspace_id"])

    plan: dict[str, str] = {}
    for asset in asset_rows:
        asset_id = str(asset["id"])
        candidates: set[str] = set()
        for session_id in linked_sessions.get(asset_id, set()):
            if session_id not in session_owners:
                raise RuntimeError(
                    f"Source asset {asset_id!r} links to session {session_id!r} "
                    "without a valid project/workspace assignment."
                )
            candidates.add(session_owners[session_id])
        if len(candidates) > 1:
            raise RuntimeError(
                f"Source asset {asset_id!r} links to conflicting workspaces: "
                + ", ".join(sorted(candidates))
                + "."
            )
        plan[asset_id] = next(iter(candidates), fallback_workspace_id)

    if set(plan) != {str(asset["id"]) for asset in asset_rows}:
        raise RuntimeError("Source ownership adoption did not cover every source asset.")
    return plan


def _foreign_key_for_workspace(
    bind: sa.Connection,
) -> Mapping[str, Any] | None:
    relevant = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys("source_assets")
        if foreign_key["constrained_columns"] == ["workspace_id"]
    ]
    if len(relevant) > 1:
        raise RuntimeError(
            "Cannot adopt source ownership; source_assets.workspace_id has multiple foreign keys."
        )
    return relevant[0] if relevant else None


def _ensure_schema(bind: sa.Connection) -> None:
    columns = _column_info(bind, "source_assets")
    existing = columns.get("workspace_id")
    if existing is not None:
        if not existing["nullable"] or not isinstance(existing["type"], sa.String):
            raise RuntimeError(
                "Cannot adopt source ownership; source_assets.workspace_id must be a "
                "nullable string column."
            )
        if existing["type"].length not in (None, 36):
            raise RuntimeError(
                "Cannot adopt source ownership; source_assets.workspace_id has an "
                "unexpected length."
            )

    foreign_key = _foreign_key_for_workspace(bind)
    if foreign_key is not None and (
        foreign_key["name"] != _WORKSPACE_FK
        or foreign_key["referred_table"] != "workspaces"
        or foreign_key["referred_columns"] != ["id"]
        or (foreign_key.get("options") or {}).get("ondelete", "").upper() != "RESTRICT"
    ):
        raise RuntimeError(
            "Cannot adopt source ownership; source_assets.workspace_id has an "
            "unexpected foreign key."
        )

    if existing is None or foreign_key is None:
        with op.batch_alter_table("source_assets") as batch:
            if existing is None:
                batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
            if foreign_key is None:
                batch.create_foreign_key(
                    _WORKSPACE_FK,
                    "workspaces",
                    ["workspace_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )

    indexes = {
        index["name"]: index
        for index in sa.inspect(bind).get_indexes("source_assets")
        if index["name"] is not None
    }
    named_index = indexes.get(_WORKSPACE_INDEX)
    if named_index is not None and (
        named_index["column_names"] != ["workspace_id"] or named_index["unique"]
    ):
        raise RuntimeError(
            "Cannot adopt source ownership; the source workspace index is malformed."
        )
    if named_index is None:
        op.create_index(_WORKSPACE_INDEX, "source_assets", ["workspace_id"])

    final_column = _column_info(bind, "source_assets").get("workspace_id")
    final_fk = _foreign_key_for_workspace(bind)
    if (
        final_column is None
        or not final_column["nullable"]
        or final_fk is None
        or final_fk["name"] != _WORKSPACE_FK
        or final_fk["referred_table"] != "workspaces"
        or final_fk["referred_columns"] != ["id"]
        or (final_fk.get("options") or {}).get("ondelete", "").upper() != "RESTRICT"
    ):
        raise RuntimeError("Source workspace ownership schema validation failed.")
    final_indexes = {
        index["name"]: index
        for index in sa.inspect(bind).get_indexes("source_assets")
        if index["name"] is not None
    }
    if _WORKSPACE_INDEX not in final_indexes:
        raise RuntimeError("Source workspace ownership index was not created.")


def _apply_adoption_plan(bind: sa.Connection, plan: dict[str, str]) -> None:
    for asset_id, workspace_id in plan.items():
        result = bind.execute(
            sa.text(
                "UPDATE source_assets SET workspace_id = :workspace_id "
                "WHERE id = :asset_id AND workspace_id IS NULL"
            ),
            {"workspace_id": workspace_id, "asset_id": asset_id},
        )
        if result.rowcount == 0:
            raise RuntimeError(f"Source asset {asset_id!r} changed during ownership adoption.")


def _validate_adoption(bind: sa.Connection, plan: dict[str, str]) -> None:
    actual_rows = (
        bind.execute(
            sa.text(
                "SELECT source_assets.id, source_assets.workspace_id, workspaces.id "
                "AS found_workspace_id FROM source_assets LEFT JOIN workspaces "
                "ON workspaces.id = source_assets.workspace_id ORDER BY source_assets.id"
            )
        )
        .mappings()
        .all()
    )
    if len(actual_rows) != len(plan):
        raise RuntimeError("Source ownership adoption changed the source asset count.")
    for row in actual_rows:
        asset_id = str(row["id"])
        if (
            row["workspace_id"] is None
            or row["found_workspace_id"] is None
            or plan.get(asset_id) != row["workspace_id"]
        ):
            raise RuntimeError(
                f"Source asset {asset_id!r} has invalid adopted workspace ownership."
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _reject_preexisting_source_ownership(bind)
    initial_workspace_id = _initial_personal_workspace_id(bind)
    adoption_plan = _load_adoption_plan(bind, initial_workspace_id)

    _ensure_schema(bind)
    _apply_adoption_plan(bind, adoption_plan)
    _validate_adoption(bind, adoption_plan)


def downgrade() -> None:
    bind = op.get_bind()
    if "source_assets" not in _table_names(bind):
        return
    columns = _column_info(bind, "source_assets")
    if "workspace_id" not in columns:
        return

    indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("source_assets")
        if index["name"] is not None
    }
    foreign_key = _foreign_key_for_workspace(bind)
    if foreign_key is not None and foreign_key["name"] is None:
        raise RuntimeError(
            "Cannot downgrade source ownership; source_assets.workspace_id has an "
            "unnamed foreign key."
        )

    with op.batch_alter_table("source_assets") as batch:
        if _WORKSPACE_INDEX in indexes:
            batch.drop_index(_WORKSPACE_INDEX)
        if foreign_key is not None:
            batch.drop_constraint(foreign_key["name"], type_="foreignkey")
        batch.drop_column("workspace_id")
