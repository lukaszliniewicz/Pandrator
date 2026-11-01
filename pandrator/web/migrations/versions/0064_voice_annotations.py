"""Adopt workspace ownership for voice collections and catalogue annotations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from alembic import op

revision = "0064_voice_annotations"
down_revision = "0063_voice_workspace"
branch_labels = None
depends_on = None

_TABLES = (
    "voice_collections",
    "voice_collection_members",
    "voice_catalog_overrides",
)
_WORKSPACE_FKS = {
    "voice_collections": "fk_voice_collections_workspace_id_workspaces",
    "voice_collection_members": "fk_voice_collection_members_workspace_id_workspaces",
    "voice_catalog_overrides": "fk_voice_catalog_overrides_workspace_id_workspaces",
}
_WORKSPACE_INDEXES = {
    "voice_collections": "ix_voice_collections_workspace_id",
    "voice_collection_members": "ix_voice_collection_members_workspace_id",
}
_COLLECTION_UNIQUE = "uq_voice_collections_workspace_name"
_FK_DROP_CASCADE = "CASCADE"
_PK_NAMING = {
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}
_OLD_COLUMNS = {
    "voice_collections": {
        "id": (sa.String, 36, False),
        "name": (sa.String, 255, False),
        "description": (sa.Text, None, True),
        "revision": (sa.Integer, None, False),
        "created_at": (sa.DateTime, None, False),
        "updated_at": (sa.DateTime, None, False),
    },
    "voice_collection_members": {
        "collection_id": (sa.String, 36, False),
        "voice_key": (sa.String, 1024, False),
        "voice_ref_json": (sa.JSON, None, False),
        "managed_voice_id": (sa.String, 36, True),
        "created_at": (sa.DateTime, None, False),
    },
    "voice_catalog_overrides": {
        "voice_key": (sa.String, 1024, False),
        "voice_ref_json": (sa.JSON, None, False),
        "name": (sa.String, 255, True),
        "description": (sa.Text, None, True),
        "voice_category": (sa.String, 20, True),
        "profile_json": (sa.JSON, None, True),
        "revision": (sa.Integer, None, False),
        "created_at": (sa.DateTime, None, False),
        "updated_at": (sa.DateTime, None, False),
    },
}
_OLD_PRIMARY_KEYS = {
    "voice_collections": ("id",),
    "voice_collection_members": ("collection_id", "voice_key"),
    "voice_catalog_overrides": ("voice_key",),
}
_TARGET_COLUMNS = {
    **_OLD_COLUMNS,
    "voice_collections": {
        **_OLD_COLUMNS["voice_collections"],
        "workspace_id": (sa.String, 36, True),
    },
    "voice_collection_members": {
        **_OLD_COLUMNS["voice_collection_members"],
        "workspace_id": (sa.String, 36, True),
    },
    "voice_catalog_overrides": {
        **_OLD_COLUMNS["voice_catalog_overrides"],
        "workspace_id": (sa.String, 36, False),
    },
}
_TARGET_PRIMARY_KEYS = {
    **_OLD_PRIMARY_KEYS,
    "voice_catalog_overrides": ("workspace_id", "voice_key"),
}
_REFERENCE_FIELDS = {"kind", "voice_id", "service_id", "model", "voice"}
_REFERENCE_LENGTHS = {
    "voice_id": 160,
    "service_id": 160,
    "model": 200,
    "voice": 255,
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {
        *_TABLES,
        "voices",
        "workspaces",
    }
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt voice annotation ownership; required tables are missing: "
            + ", ".join(sorted(missing))
        )

    required_columns = {
        "voices": {"id", "workspace_id"},
        "workspaces": {"id", "state"},
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                "Cannot adopt voice annotation ownership; "
                f"{table_name} is missing columns: " + ", ".join(sorted(missing_columns))
            )


def _primary_key(bind: sa.Connection, table_name: str) -> tuple[str, ...]:
    constraint = sa.inspect(bind).get_pk_constraint(table_name)
    return tuple(constraint.get("constrained_columns") or ())


def _foreign_key_signatures(
    bind: sa.Connection, table_name: str
) -> list[tuple[tuple[str, ...], str, tuple[str, ...], str | None]]:
    result = []
    for foreign_key in sa.inspect(bind).get_foreign_keys(table_name):
        result.append(
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                (foreign_key.get("options") or {}).get("ondelete", "").upper() or None,
            )
        )
    return result


def _expected_foreign_keys(
    table_name: str, *, target: bool
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str | None]]:
    result: set[tuple[tuple[str, ...], str, tuple[str, ...], str | None]] = set()
    if table_name == "voice_collection_members":
        result.update(
            {
                (("collection_id",), "voice_collections", ("id",), _FK_DROP_CASCADE),
                (("managed_voice_id",), "voices", ("id",), _FK_DROP_CASCADE),
            }
        )
    if target:
        result.add((("workspace_id",), "workspaces", ("id",), "RESTRICT"))
    return result


def _normalize_default(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == "'":
        normalized = normalized[1:-1]
    return normalized


def _validate_defaults(
    table_name: str,
    columns: Mapping[str, Mapping[str, Any]],
    *,
    target: bool,
    allow_historical_target_defaults: bool = False,
) -> bool:
    defaulted_fields = {
        "voice_collections": ("revision", "created_at", "updated_at"),
        "voice_collection_members": ("created_at",),
        "voice_catalog_overrides": ("revision", "created_at", "updated_at"),
    }[table_name]
    defaults = tuple(_normalize_default(columns[name].get("default")) for name in defaulted_fields)
    historical = {
        "voice_collections": ("1", "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP"),
        "voice_collection_members": ("CURRENT_TIMESTAMP",),
        "voice_catalog_overrides": ("1", "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP"),
    }[table_name]
    if target and not allow_historical_target_defaults:
        return all(value is None for value in defaults)
    return all(value is None for value in defaults) or defaults == historical


def _validate_index_shape(bind: sa.Connection, table_name: str, *, target: bool) -> bool:
    inspector = sa.inspect(bind)
    indexes = inspector.get_indexes(table_name)
    expected_name = _WORKSPACE_INDEXES.get(table_name)
    workspace_indexes = [
        index for index in indexes if "workspace_id" in (index.get("column_names") or ())
    ]
    if not target:
        return not workspace_indexes
    if expected_name is None:
        return not workspace_indexes
    return (
        len(workspace_indexes) == 1
        and workspace_indexes[0].get("name") == expected_name
        and tuple(workspace_indexes[0].get("column_names") or ()) == ("workspace_id",)
        and not workspace_indexes[0].get("unique")
    )


def _validate_unique_shape(bind: sa.Connection, table_name: str, *, target: bool) -> bool:
    inspector = sa.inspect(bind)
    constraints = inspector.get_unique_constraints(table_name)
    unique_indexes = [index for index in inspector.get_indexes(table_name) if index.get("unique")]
    if table_name == "voice_collections":
        if target:
            expected = [
                (_COLLECTION_UNIQUE, ("workspace_id", "name")),
            ]
        else:
            expected = [(None, ("name",))]
        actual = [
            (constraint.get("name"), tuple(constraint.get("column_names") or ()))
            for constraint in constraints
        ]
        if not target and actual == [("uq_voice_collections_name", ("name",))]:
            return not unique_indexes
        return actual == expected and not unique_indexes
    return not constraints and not unique_indexes


def _validate_table_shape(
    bind: sa.Connection,
    table_name: str,
    *,
    target: bool,
    allow_historical_target_defaults: bool = False,
) -> bool:
    expected_columns = _TARGET_COLUMNS[table_name] if target else _OLD_COLUMNS[table_name]
    columns = _columns(bind, table_name)
    if set(columns) != set(expected_columns):
        return False

    primary_keys = _TARGET_PRIMARY_KEYS if target else _OLD_PRIMARY_KEYS
    if _primary_key(bind, table_name) != primary_keys[table_name]:
        return False

    for name, (expected_type, expected_length, expected_nullable) in expected_columns.items():
        column = columns[name]
        actual_type = column["type"]
        if expected_type is sa.String:
            type_matches = (
                isinstance(actual_type, sa.String)
                and not isinstance(actual_type, sa.Text)
                and actual_type.length == expected_length
            )
        else:
            type_matches = isinstance(actual_type, expected_type)
        if not type_matches or bool(column["nullable"]) is not expected_nullable:
            return False
        if name == "workspace_id" and column.get("default") is not None:
            return False

    if not _validate_defaults(
        table_name,
        columns,
        target=target,
        allow_historical_target_defaults=allow_historical_target_defaults,
    ):
        return False
    actual_foreign_keys = _foreign_key_signatures(bind, table_name)
    expected_foreign_keys = _expected_foreign_keys(table_name, target=target)
    if (
        len(actual_foreign_keys) != len(expected_foreign_keys)
        or set(actual_foreign_keys) != expected_foreign_keys
    ):
        return False
    foreign_keys = sa.inspect(bind).get_foreign_keys(table_name)
    if target:
        workspace_foreign_keys = [
            foreign_key
            for foreign_key in foreign_keys
            if "workspace_id" in foreign_key["constrained_columns"]
        ]
        if (
            len(workspace_foreign_keys) != 1
            or workspace_foreign_keys[0].get("name") != _WORKSPACE_FKS[table_name]
        ):
            return False
        if table_name == "voice_collection_members" and any(
            foreign_key.get("name") is not None
            for foreign_key in foreign_keys
            if "workspace_id" not in foreign_key["constrained_columns"]
        ):
            return False
    if not _validate_index_shape(bind, table_name, target=target):
        return False
    if not _validate_unique_shape(bind, table_name, target=target):
        return False
    return True


def _classify_tables(bind: sa.Connection, *, allow_historical_target_defaults: bool = False) -> str:
    states: list[str] = []
    for table_name in _TABLES:
        if _validate_table_shape(bind, table_name, target=False):
            states.append("old")
        elif _validate_table_shape(
            bind,
            table_name,
            target=True,
            allow_historical_target_defaults=allow_historical_target_defaults,
        ):
            states.append("target")
        else:
            raise RuntimeError(
                "Cannot adopt voice annotation ownership; "
                f"{table_name} has a partial or malformed schema."
            )
    if len(set(states)) != 1:
        raise RuntimeError(
            "Cannot adopt voice annotation ownership; only part of the target schema exists."
        )
    return states[0]


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
    required = {"users", "workspace_memberships"}
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt unbound voice annotations; required tables are missing: "
            + ", ".join(sorted(missing))
        )
    required_columns = {
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                "Cannot adopt unbound voice annotations; "
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
            "Cannot adopt unbound voice annotations; the seeded owner user is missing or ambiguous."
        )
    owner = owners[0]
    if not owner["is_active"] or not owner["is_instance_admin"]:
        raise RuntimeError(
            "Cannot adopt unbound voice annotations; the seeded owner user is "
            "inactive or is not an instance admin."
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
    if len(workspaces) != 1 or workspaces[0]["state"] != "active":
        raise RuntimeError(
            "Cannot adopt unbound voice annotations; the seeded owner personal "
            "workspace is missing, ambiguous, or inactive."
        )
    workspace_id = workspaces[0]["id"]
    memberships = (
        bind.execute(
            sa.text(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND user_id = :user_id"
            ),
            {"workspace_id": workspace_id, "user_id": owner["id"]},
        )
        .mappings()
        .all()
    )
    if len(memberships) != 1 or memberships[0]["role"] != "owner":
        raise RuntimeError(
            "Cannot adopt unbound voice annotations; the seeded owner workspace "
            "membership is missing or invalid."
        )
    if not isinstance(workspace_id, str):
        raise RuntimeError(
            "Cannot adopt unbound voice annotations; the initial personal workspace id is invalid."
        )
    return workspace_id


def _voice_ownership_rows(bind: sa.Connection) -> dict[Any, tuple[Any, Any]]:
    rows = (
        bind.execute(
            sa.text(
                "SELECT voices.id, voices.workspace_id, workspaces.state "
                "FROM voices LEFT JOIN workspaces ON workspaces.id = voices.workspace_id "
                "ORDER BY voices.id"
            )
        )
        .mappings()
        .all()
    )
    rows_by_id: dict[Any, tuple[Any, Any]] = {}
    for row in rows:
        voice_id = row["id"]
        rows_by_id[voice_id] = (row["workspace_id"], row["state"])
    return rows_by_id


def _validated_voice_owner(
    voice_id: str, voice_rows: Mapping[Any, tuple[Any, Any]], *, record: str
) -> str:
    row = voice_rows.get(voice_id)
    if row is None:
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its managed voice is missing."
        )
    workspace_id, state = row
    if not isinstance(workspace_id, str) or state not in {"active", "archived"}:
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its managed voice has "
            "missing or invalid stored workspace ownership."
        )
    return workspace_id


def _parse_reference(raw_json: Any, *, record: str) -> tuple[str, str | None]:
    if not isinstance(raw_json, str):
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its reference JSON is not text."
        )
    try:
        reference = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its reference JSON is malformed."
        ) from exc
    if not isinstance(reference, dict) or set(reference) - _REFERENCE_FIELDS:
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its reference shape is invalid."
        )

    kind = reference.get("kind")
    if not isinstance(kind, str) or kind not in {"managed", "provider"}:
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its reference kind is invalid."
        )
    normalized: dict[str, str | None] = {"kind": kind}
    for field_name, max_length in _REFERENCE_LENGTHS.items():
        value = reference.get(field_name)
        if value is not None and not isinstance(value, str):
            raise RuntimeError(
                f"Cannot adopt voice annotation {record!r}; "
                f"reference field {field_name!r} is not text."
            )
        value = value.strip() if isinstance(value, str) else None
        if value == "":
            value = None
        if value is not None and len(value) > max_length:
            raise RuntimeError(
                f"Cannot adopt voice annotation {record!r}; "
                f"reference field {field_name!r} exceeds {max_length} characters."
            )
        normalized[field_name] = value

    if kind == "managed":
        voice_id = normalized["voice_id"]
        if voice_id is None or any(
            normalized[field_name] is not None for field_name in ("service_id", "model", "voice")
        ):
            raise RuntimeError(
                f"Cannot adopt voice annotation {record!r}; its managed reference is invalid."
            )
        return f"managed:{voice_id}", voice_id

    if normalized["voice_id"] is not None or any(
        normalized[field_name] is None for field_name in ("service_id", "model", "voice")
    ):
        raise RuntimeError(
            f"Cannot adopt voice annotation {record!r}; its provider reference is invalid."
        )
    key = ":".join(
        (
            "provider",
            quote(normalized["service_id"] or "", safe=""),
            quote(normalized["model"] or "", safe=""),
            quote(normalized["voice"] or "", safe=""),
        )
    )
    return key, None


def _load_adoption_plan(
    bind: sa.Connection,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    voice_rows = _voice_ownership_rows(bind)
    workspaces = {
        str(row["id"]): str(row["state"])
        for row in bind.execute(sa.text("SELECT id, state FROM workspaces")).mappings()
    }

    collections = (
        bind.execute(sa.text("SELECT id FROM voice_collections ORDER BY id")).mappings().all()
    )
    collection_ids = {str(row["id"]) for row in collections}
    collection_owner_sets = {collection_id: set() for collection_id in collection_ids}
    member_rows = (
        bind.execute(
            sa.text(
                "SELECT collection_id, voice_key, voice_ref_json, managed_voice_id "
                "FROM voice_collection_members ORDER BY collection_id, voice_key"
            )
        )
        .mappings()
        .all()
    )
    for row in member_rows:
        collection_id = row["collection_id"]
        voice_key = row["voice_key"]
        record = f"member:{collection_id!r}:{voice_key!r}"
        if not isinstance(collection_id, str) or collection_id not in collection_ids:
            raise RuntimeError(
                f"Cannot adopt voice annotation {record}; its collection is missing."
            )
        canonical_key, managed_voice_id = _parse_reference(row["voice_ref_json"], record=record)
        if not isinstance(voice_key, str) or voice_key != canonical_key:
            raise RuntimeError(
                f"Cannot adopt voice annotation {record}; its canonical key is inconsistent."
            )
        stored_managed_id = row["managed_voice_id"]
        if stored_managed_id is not None and not isinstance(stored_managed_id, str):
            raise RuntimeError(
                f"Cannot adopt voice annotation {record}; its managed voice id is invalid."
            )
        if stored_managed_id != managed_voice_id:
            raise RuntimeError(
                f"Cannot adopt voice annotation {record}; its managed voice link "
                "does not match the reference."
            )
        if managed_voice_id is not None:
            owner = _validated_voice_owner(managed_voice_id, voice_rows, record=record)
            collection_owner_sets[collection_id].add(owner)

    fallback_needed = any(not owners for owners in collection_owner_sets.values())
    override_owners: dict[str, str | None] = {}
    override_rows = (
        bind.execute(
            sa.text(
                "SELECT voice_key, voice_ref_json FROM voice_catalog_overrides ORDER BY voice_key"
            )
        )
        .mappings()
        .all()
    )
    for row in override_rows:
        voice_key = row["voice_key"]
        record = f"override:{voice_key!r}"
        canonical_key, managed_voice_id = _parse_reference(row["voice_ref_json"], record=record)
        if not isinstance(voice_key, str) or voice_key != canonical_key:
            raise RuntimeError(
                f"Cannot adopt voice annotation {record}; its canonical key is inconsistent."
            )
        owner = None
        if managed_voice_id is not None:
            owner = _validated_voice_owner(managed_voice_id, voice_rows, record=record)
        else:
            fallback_needed = True
        override_owners[voice_key] = owner

    fallback_workspace_id = _initial_personal_workspace_id(bind) if fallback_needed else None

    collection_owners: dict[str, str] = {}
    for collection_id, owners in collection_owner_sets.items():
        if len(owners) > 1:
            raise RuntimeError(
                f"Cannot adopt voice collection {collection_id!r}; managed members "
                "have conflicting workspace owners."
            )
        if owners:
            collection_owners[collection_id] = next(iter(owners))
        else:
            assert fallback_workspace_id is not None
            collection_owners[collection_id] = fallback_workspace_id

    override_owner_map: dict[str, str] = {}
    for voice_key, owner in override_owners.items():
        if owner is None:
            assert fallback_workspace_id is not None
            owner = fallback_workspace_id
        override_owner_map[voice_key] = owner

    for owner in (*collection_owners.values(), *override_owner_map.values()):
        if workspaces.get(owner) not in {"active", "archived"}:
            raise RuntimeError(
                f"Cannot adopt voice annotations; workspace {owner!r} is missing or invalid."
            )

    member_owners = {
        f"{row['collection_id']}\x00{row['voice_key']}": collection_owners[
            str(row["collection_id"])
        ]
        for row in member_rows
    }
    return collection_owners, member_owners, override_owner_map


def _ensure_new_scope_columns() -> None:
    with op.batch_alter_table("voice_collections", naming_convention=_PK_NAMING) as batch:
        batch.drop_constraint("uq_voice_collections_name", type_="unique")
        batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            _WORKSPACE_FKS["voice_collections"],
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(_WORKSPACE_INDEXES["voice_collections"], ["workspace_id"])
        batch.create_unique_constraint(_COLLECTION_UNIQUE, ["workspace_id", "name"])

    with op.batch_alter_table("voice_collection_members") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            _WORKSPACE_FKS["voice_collection_members"],
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(_WORKSPACE_INDEXES["voice_collection_members"], ["workspace_id"])

    with op.batch_alter_table("voice_catalog_overrides") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            _WORKSPACE_FKS["voice_catalog_overrides"],
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def _apply_backfill(
    bind: sa.Connection,
    collection_owners: Mapping[str, str],
    member_owners: Mapping[str, str],
    override_owners: Mapping[str, str],
) -> None:
    for collection_id, workspace_id in collection_owners.items():
        result = bind.execute(
            sa.text(
                "UPDATE voice_collections SET workspace_id = :workspace_id "
                "WHERE id = :record_id AND workspace_id IS NULL"
            ),
            {"workspace_id": workspace_id, "record_id": collection_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Voice collection ownership changed for {collection_id!r}.")
    for compound_key, workspace_id in member_owners.items():
        collection_id, voice_key = compound_key.split("\x00", 1)
        result = bind.execute(
            sa.text(
                "UPDATE voice_collection_members SET workspace_id = :workspace_id "
                "WHERE collection_id = :collection_id AND voice_key = :voice_key "
                "AND workspace_id IS NULL"
            ),
            {
                "workspace_id": workspace_id,
                "collection_id": collection_id,
                "voice_key": voice_key,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Voice collection member ownership changed for {collection_id!r}.")
    for voice_key, workspace_id in override_owners.items():
        result = bind.execute(
            sa.text(
                "UPDATE voice_catalog_overrides SET workspace_id = :workspace_id "
                "WHERE voice_key = :voice_key AND workspace_id IS NULL"
            ),
            {"workspace_id": workspace_id, "voice_key": voice_key},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Voice catalogue override ownership changed for {voice_key!r}.")


def _require_fully_scoped_overrides(bind: sa.Connection) -> None:
    missing = bind.execute(
        sa.text(
            "SELECT voice_key FROM voice_catalog_overrides "
            "WHERE workspace_id IS NULL ORDER BY voice_key LIMIT 1"
        )
    ).first()
    if missing is not None:
        raise RuntimeError(
            f"Cannot adopt voice catalogue overrides; a row remains unscoped: {missing[0]!r}."
        )


def _make_override_scope_mandatory() -> None:
    with op.batch_alter_table("voice_catalog_overrides", naming_convention=_PK_NAMING) as batch:
        batch.alter_column(
            "workspace_id",
            existing_type=sa.String(36),
            nullable=False,
        )
        batch.drop_constraint("pk_voice_catalog_overrides", type_="primary")
        batch.create_primary_key("pk_voice_catalog_overrides", ["workspace_id", "voice_key"])


def _assert_empty_target_schema(bind: sa.Connection) -> None:
    for table_name in _TABLES:
        row = bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first()
        if row is not None:
            raise RuntimeError(
                "Cannot adopt voice annotation ownership; the target schema is "
                f"already populated in {table_name}."
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    schema_state = _classify_tables(bind)
    if schema_state == "target":
        _assert_empty_target_schema(bind)
        return

    # Validate every reference and owner before the first DDL statement.
    plan = _load_adoption_plan(bind)
    _ensure_new_scope_columns()
    _apply_backfill(bind, *plan)
    _require_fully_scoped_overrides(bind)
    _make_override_scope_mandatory()


def _assert_downgrade_has_no_collisions(bind: sa.Connection) -> None:
    collection_collision = bind.execute(
        sa.text(
            "SELECT name FROM voice_collections GROUP BY name "
            "HAVING COUNT(*) > 1 ORDER BY name LIMIT 1"
        )
    ).first()
    if collection_collision is not None:
        raise RuntimeError(
            "Cannot downgrade voice annotation ownership; collection names would "
            f"collide for {collection_collision[0]!r}."
        )
    override_collision = bind.execute(
        sa.text(
            "SELECT voice_key FROM voice_catalog_overrides GROUP BY voice_key "
            "HAVING COUNT(*) > 1 ORDER BY voice_key LIMIT 1"
        )
    ).first()
    if override_collision is not None:
        raise RuntimeError(
            "Cannot downgrade voice annotation ownership; override keys would "
            f"collide for {override_collision[0]!r}."
        )


def _drop_scope_columns() -> None:
    with op.batch_alter_table("voice_collections", naming_convention=_PK_NAMING) as batch:
        batch.drop_constraint(_COLLECTION_UNIQUE, type_="unique")
        batch.drop_index(_WORKSPACE_INDEXES["voice_collections"])
        batch.drop_constraint(_WORKSPACE_FKS["voice_collections"], type_="foreignkey")
        batch.drop_column("workspace_id")
        batch.create_unique_constraint("uq_voice_collections_name", ["name"])

    with op.batch_alter_table("voice_collection_members") as batch:
        batch.drop_index(_WORKSPACE_INDEXES["voice_collection_members"])
        batch.drop_constraint(_WORKSPACE_FKS["voice_collection_members"], type_="foreignkey")
        batch.drop_column("workspace_id")

    with op.batch_alter_table("voice_catalog_overrides", naming_convention=_PK_NAMING) as batch:
        batch.drop_constraint("pk_voice_catalog_overrides", type_="primary")
        batch.drop_constraint(_WORKSPACE_FKS["voice_catalog_overrides"], type_="foreignkey")
        batch.drop_column("workspace_id")
        batch.create_primary_key("pk_voice_catalog_overrides", ["voice_key"])


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    if _classify_tables(bind, allow_historical_target_defaults=True) != "target":
        raise RuntimeError(
            "Cannot downgrade voice annotation ownership; the scoped schema is missing."
        )
    # Collision checks must finish before any batch operation can rebuild a table.
    _assert_downgrade_has_no_collisions(bind)
    _drop_scope_columns()
