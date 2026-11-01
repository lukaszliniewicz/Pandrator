"""Adopt workspace ownership for historical sound-library records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0062_sound_workspace"
down_revision = "0061_artifact_ownership"
branch_labels = None
depends_on = None

_TABLES = ("sound_items", "sound_variants")
_WORKSPACE_COLUMNS = {table: "workspace_id" for table in _TABLES}
_WORKSPACE_FKS = {
    "sound_items": "fk_sound_items_workspace_id_workspaces",
    "sound_variants": "fk_sound_variants_workspace_id_workspaces",
}
_WORKSPACE_INDEXES = {
    "sound_items": {
        "ix_sound_items_workspace_id": ("workspace_id",),
        "ix_sound_items_workspace_archived_created_at_id": (
            "workspace_id",
            "archived",
            "created_at",
            "id",
        ),
    },
    "sound_variants": {
        "ix_sound_variants_workspace_id": ("workspace_id",),
        "ix_sound_variants_workspace_fingerprint_state": (
            "workspace_id",
            "fingerprint",
            "state",
        ),
        "ix_sound_variants_workspace_created_at_id": (
            "workspace_id",
            "created_at",
            "id",
        ),
    },
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_info(bind: sa.Connection, table_name: str) -> Mapping[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {"artifacts", "sound_items", "sound_variants", "workspaces"}
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt sound ownership; required tables are missing: "
            + ", ".join(sorted(missing))
        )

    required_columns = {
        "artifacts": {"id", "workspace_id"},
        "sound_items": {"id", "preferred_variant_id"},
        "sound_variants": {"id", "item_id", "artifact_id"},
        "workspaces": {"id", "state"},
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_column_info(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot adopt sound ownership; {table_name} is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_keys_for_column(
    bind: sa.Connection, table_name: str, column_name: str
) -> list[Mapping[str, Any]]:
    return [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        if column_name in foreign_key["constrained_columns"]
    ]


def _indexes_for_column(
    bind: sa.Connection, table_name: str, column_name: str
) -> list[Mapping[str, Any]]:
    return [
        index
        for index in sa.inspect(bind).get_indexes(table_name)
        if column_name in (index["column_names"] or [])
    ]


def _validate_scope_column(bind: sa.Connection, table_name: str) -> None:
    column_name = _WORKSPACE_COLUMNS[table_name]
    column = _column_info(bind, table_name).get(column_name)
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
            f"Cannot adopt sound ownership; {table_name}.{column_name} has an "
            "unexpected column definition."
        )

    foreign_keys = _foreign_keys_for_column(bind, table_name, column_name)
    if len(foreign_keys) != 1:
        raise RuntimeError(
            f"Cannot adopt sound ownership; {table_name}.{column_name} must have "
            "exactly one ownership foreign key."
        )
    foreign_key = foreign_keys[0]
    if (
        foreign_key.get("name") != _WORKSPACE_FKS[table_name]
        or foreign_key["constrained_columns"] != [column_name]
        or foreign_key["referred_table"] != "workspaces"
        or foreign_key["referred_columns"] != ["id"]
        or (foreign_key.get("options") or {}).get("ondelete", "").upper() != "RESTRICT"
    ):
        raise RuntimeError(
            f"Cannot adopt sound ownership; {table_name}.{column_name} has an "
            "unexpected foreign key."
        )

    expected_indexes = _WORKSPACE_INDEXES[table_name]
    indexes = _indexes_for_column(bind, table_name, column_name)
    actual_names = {index.get("name") for index in indexes}
    if actual_names != set(expected_indexes):
        raise RuntimeError(
            f"Cannot adopt sound ownership; {table_name}.{column_name} has an "
            "unexpected or incomplete workspace index set."
        )
    for index in indexes:
        if (
            index.get("name") not in expected_indexes
            or tuple(index["column_names"] or ()) != expected_indexes[index["name"]]
            or index.get("unique")
        ):
            raise RuntimeError(
                f"Cannot adopt sound ownership; workspace index "
                f"{index.get('name')!r} on {table_name} is malformed."
            )


def _validate_unadopted_scope(bind: sa.Connection) -> bool:
    present = {
        table_name for table_name in _TABLES if "workspace_id" in _column_info(bind, table_name)
    }
    if present and present != set(_TABLES):
        raise RuntimeError(
            "Cannot adopt sound ownership; only part of the ownership scope schema exists."
        )

    if not present:
        for table_name in _TABLES:
            if _foreign_keys_for_column(bind, table_name, "workspace_id"):
                raise RuntimeError(
                    "Cannot adopt sound ownership; a workspace foreign key exists "
                    f"without {table_name}.workspace_id."
                )
            if any(
                index.get("name") in _WORKSPACE_INDEXES[table_name]
                for index in sa.inspect(bind).get_indexes(table_name)
            ):
                raise RuntimeError(
                    "Cannot adopt sound ownership; a workspace index exists "
                    f"without {table_name}.workspace_id."
                )
        return False

    for table_name in _TABLES:
        _validate_scope_column(bind, table_name)
    for table_name in _TABLES:
        row = bind.execute(
            sa.text(
                f"SELECT id FROM {table_name} WHERE workspace_id IS NOT NULL ORDER BY id LIMIT 1"
            )
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot adopt sound ownership; sound records already contain "
                f"preexisting scope data on {table_name} row {row[0]!r}."
            )
    return True


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
    required = {"users", "workspace_memberships"}
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt unbound sound records; required tables are missing: "
            + ", ".join(sorted(missing))
        )
    required_columns = {
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_column_info(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                "Cannot adopt unbound sound records; "
                f"{table_name} is missing columns: " + ", ".join(sorted(missing_columns))
            )

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
            "Cannot adopt unbound sound records; the seeded owner user is missing or ambiguous."
        )
    owner = owners[0]
    if not owner["is_active"] or not owner["is_instance_admin"]:
        raise RuntimeError(
            "Cannot adopt unbound sound records; the seeded owner user is inactive "
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
            "Cannot adopt unbound sound records; the seeded owner personal workspace "
            "is missing or ambiguous."
        )
    workspace = workspaces[0]
    if workspace["state"] != "active":
        raise RuntimeError(
            "Cannot adopt unbound sound records; the seeded owner personal workspace is not active."
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
            "Cannot adopt unbound sound records; the seeded owner workspace membership "
            "is missing or invalid."
        )
    return str(workspace["id"])


def _load_adoption_plan(bind: sa.Connection) -> tuple[dict[str, str], dict[str, str]]:
    item_rows = (
        bind.execute(sa.text("SELECT id, preferred_variant_id FROM sound_items ORDER BY id"))
        .mappings()
        .all()
    )
    variant_rows = (
        bind.execute(sa.text("SELECT id, item_id, artifact_id FROM sound_variants ORDER BY id"))
        .mappings()
        .all()
    )
    items = {str(row["id"]): row for row in item_rows}
    variants = {str(row["id"]): row for row in variant_rows}

    for item_id, item in items.items():
        preferred_id = item["preferred_variant_id"]
        if preferred_id is None:
            continue
        preferred = variants.get(str(preferred_id))
        if preferred is None:
            raise RuntimeError(
                f"Cannot adopt sound ownership; item {item_id!r} references missing "
                f"preferred variant {preferred_id!r}."
            )
        if preferred["item_id"] != item_id:
            raise RuntimeError(
                f"Cannot adopt sound ownership; preferred variant {preferred_id!r} "
                f"does not belong to item {item_id!r}."
            )

    groups: dict[tuple[str, str], set[str]] = {}
    item_groups: dict[str, tuple[str, str]] = {}
    variant_groups: dict[str, tuple[str, str]] = {}
    for item_id in items:
        group = ("item", item_id)
        groups[group] = set()
        item_groups[item_id] = group
    for variant_id, variant in variants.items():
        item_id = variant["item_id"]
        if item_id is None:
            group = ("variant", variant_id)
            groups[group] = set()
        else:
            item_key = str(item_id)
            if item_key not in items:
                raise RuntimeError(
                    f"Cannot adopt sound ownership; variant {variant_id!r} references "
                    f"missing item {item_key!r}."
                )
            group = item_groups[item_key]
        variant_groups[variant_id] = group

    artifact_links = (
        bind.execute(
            sa.text(
                "SELECT sound_variants.id AS variant_id, sound_variants.artifact_id, "
                "artifacts.id AS found_artifact_id, artifacts.workspace_id, "
                "workspaces.id AS found_workspace_id, workspaces.state AS workspace_state "
                "FROM sound_variants "
                "LEFT JOIN artifacts ON artifacts.id = sound_variants.artifact_id "
                "LEFT JOIN workspaces ON workspaces.id = artifacts.workspace_id "
                "WHERE sound_variants.artifact_id IS NOT NULL "
                "ORDER BY sound_variants.id"
            )
        )
        .mappings()
        .all()
    )
    for linked in artifact_links:
        variant_id = str(linked["variant_id"])
        artifact_id = str(linked["artifact_id"])
        if linked["found_artifact_id"] is None:
            raise RuntimeError(
                f"Cannot adopt sound ownership; variant {variant_id!r} references "
                f"missing artifact {artifact_id!r}."
            )
        if (
            linked["workspace_id"] is None
            or linked["found_workspace_id"] is None
            or linked["workspace_state"] not in {"active", "archived"}
        ):
            raise RuntimeError(
                f"Cannot adopt sound ownership; artifact {artifact_id!r} linked to "
                f"variant {variant_id!r} has missing or invalid workspace ownership."
            )
        groups[variant_groups[variant_id]].add(str(linked["workspace_id"]))

    fallback_needed = False
    group_owners: dict[tuple[str, str], str] = {}
    for group, evidence in groups.items():
        if len(evidence) > 1:
            raise RuntimeError(
                f"Cannot adopt sound group {group[1]!r}; linked artifact workspaces "
                "conflict: " + ", ".join(sorted(evidence)) + "."
            )
        if evidence:
            group_owners[group] = next(iter(evidence))
        else:
            fallback_needed = True

    fallback_workspace_id = _initial_personal_workspace_id(bind) if fallback_needed else None
    for group, evidence in groups.items():
        if not evidence:
            assert fallback_workspace_id is not None
            group_owners[group] = fallback_workspace_id

    item_owners = {item_id: group_owners[group] for item_id, group in item_groups.items()}
    variant_owners = {
        variant_id: group_owners[group] for variant_id, group in variant_groups.items()
    }
    if set(item_owners) != set(items) or set(variant_owners) != set(variants):
        raise RuntimeError("Sound ownership adoption did not cover every sound record.")
    return item_owners, variant_owners


def _ensure_schema(bind: sa.Connection, existing_scope: bool) -> None:
    if not existing_scope:
        for table_name in _TABLES:
            with op.batch_alter_table(table_name) as batch:
                batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
                batch.create_foreign_key(
                    _WORKSPACE_FKS[table_name],
                    "workspaces",
                    ["workspace_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )
                for index_name, columns in _WORKSPACE_INDEXES[table_name].items():
                    batch.create_index(index_name, list(columns))

    for table_name in _TABLES:
        _validate_scope_column(bind, table_name)


def _apply_adoption_plan(
    bind: sa.Connection, item_owners: dict[str, str], variant_owners: dict[str, str]
) -> None:
    for item_id, workspace_id in item_owners.items():
        result = bind.execute(
            sa.text(
                "UPDATE sound_items SET workspace_id = :workspace_id "
                "WHERE id = :record_id AND workspace_id IS NULL"
            ),
            {"workspace_id": workspace_id, "record_id": item_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Sound item {item_id!r} changed during ownership adoption.")

    for variant_id, workspace_id in variant_owners.items():
        result = bind.execute(
            sa.text(
                "UPDATE sound_variants SET workspace_id = :workspace_id "
                "WHERE id = :record_id AND workspace_id IS NULL"
            ),
            {"workspace_id": workspace_id, "record_id": variant_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Sound variant {variant_id!r} changed during ownership adoption.")


def _validate_adoption(bind: sa.Connection, table_name: str, plan: dict[str, str]) -> None:
    rows = (
        bind.execute(
            sa.text(
                f"SELECT records.id, records.workspace_id, workspaces.id AS found_workspace_id, "
                "workspaces.state AS workspace_state "
                f"FROM {table_name} AS records "
                "LEFT JOIN workspaces ON workspaces.id = records.workspace_id "
                "ORDER BY records.id"
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != len(plan):
        raise RuntimeError(f"Sound ownership adoption changed the {table_name} count.")
    for row in rows:
        record_id = str(row["id"])
        if (
            row["workspace_id"] is None
            or row["found_workspace_id"] is None
            or row["workspace_state"] not in {"active", "archived"}
            or plan.get(record_id) != row["workspace_id"]
        ):
            raise RuntimeError(
                f"Sound record {record_id!r} has invalid adopted workspace ownership."
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    existing_scope = _validate_unadopted_scope(bind)
    item_owners, variant_owners = _load_adoption_plan(bind)

    _ensure_schema(bind, existing_scope)
    _apply_adoption_plan(bind, item_owners, variant_owners)
    _validate_adoption(bind, "sound_items", item_owners)
    _validate_adoption(bind, "sound_variants", variant_owners)


def downgrade() -> None:
    bind = op.get_bind()
    if not set(_TABLES) <= _table_names(bind):
        return
    present = {
        table_name for table_name in _TABLES if "workspace_id" in _column_info(bind, table_name)
    }
    if not present:
        return
    if present != set(_TABLES):
        raise RuntimeError(
            "Cannot downgrade sound ownership; only part of the scope schema exists."
        )

    for table_name in reversed(_TABLES):
        _validate_scope_column(bind, table_name)
        with op.batch_alter_table(table_name) as batch:
            for index_name in _WORKSPACE_INDEXES[table_name]:
                batch.drop_index(index_name)
            batch.drop_constraint(_WORKSPACE_FKS[table_name], type_="foreignkey")
            batch.drop_column("workspace_id")
