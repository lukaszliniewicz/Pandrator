"""Adopt workspace ownership for voices, their samples, and training runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0063_voice_workspace"
down_revision = "0062_sound_workspace"
branch_labels = None
depends_on = None

_TABLES = ("voices", "voice_samples", "training_runs")
_WORKSPACE_FKS = {
    "voices": "fk_voices_workspace_id_workspaces",
    "voice_samples": "fk_voice_samples_workspace_id_workspaces",
    "training_runs": "fk_training_runs_workspace_id_workspaces",
}
_WORKSPACE_INDEXES = {
    "voices": {"ix_voices_workspace_id": ("workspace_id",)},
    "voice_samples": {"ix_voice_samples_workspace_id": ("workspace_id",)},
    "training_runs": {
        "ix_training_runs_workspace_id": ("workspace_id",),
        "ix_training_runs_workspace_created_at_id": (
            "workspace_id",
            "created_at",
            "id",
        ),
    },
}
_VOICE_WORKSPACE_UNIQUE = ("uq_voices_workspace_name", ("workspace_id", "name"))
_VOICE_GLOBAL_UNIQUE_COLUMNS = ("name",)


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_info(bind: sa.Connection, table_name: str) -> Mapping[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required = {"artifacts", "voices", "voice_samples", "training_runs", "workspaces"}
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt voice ownership; required tables are missing: "
            + ", ".join(sorted(missing))
        )

    required_columns = {
        "artifacts": {"id", "workspace_id"},
        "voices": {"id", "name"},
        "voice_samples": {"id", "voice_id", "artifact_id"},
        "training_runs": {
            "id",
            "voice_id",
            "source_artifact_id",
            "source_text_artifact_id",
            "output_artifact_id",
            "created_at",
        },
        "workspaces": {"id", "state"},
    }
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_column_info(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                "Cannot adopt voice ownership; "
                f"{table_name} is missing columns: " + ", ".join(sorted(missing_columns))
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


def _validate_voice_name_uniqueness(bind: sa.Connection, *, scoped: bool) -> None:
    inspector = sa.inspect(bind)
    relevant_constraints = [
        (constraint.get("name"), tuple(constraint["column_names"] or ()))
        for constraint in inspector.get_unique_constraints("voices")
        if set(constraint["column_names"] or ()) & {"workspace_id", "name"}
    ]
    relevant_indexes = [
        (index.get("name"), tuple(index["column_names"] or ()))
        for index in inspector.get_indexes("voices")
        if index.get("unique") and set(index["column_names"] or ()) & {"workspace_id", "name"}
    ]
    if scoped:
        valid_constraints = relevant_constraints == [_VOICE_WORKSPACE_UNIQUE]
    else:
        valid_constraints = (
            len(relevant_constraints) == 1
            and relevant_constraints[0][1] == _VOICE_GLOBAL_UNIQUE_COLUMNS
        )
    if not valid_constraints or relevant_indexes:
        description = "workspace/name" if scoped else "global name"
        raise RuntimeError(
            "Cannot adopt voice ownership; voices has an unexpected "
            f"{description} uniqueness definition."
        )


def _validate_scope_column(bind: sa.Connection, table_name: str) -> None:
    column_name = "workspace_id"
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
            f"Cannot adopt voice ownership; {table_name}.{column_name} has an "
            "unexpected column definition."
        )

    foreign_keys = _foreign_keys_for_column(bind, table_name, column_name)
    if len(foreign_keys) != 1:
        raise RuntimeError(
            f"Cannot adopt voice ownership; {table_name}.{column_name} must have "
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
            f"Cannot adopt voice ownership; {table_name}.{column_name} has an "
            "unexpected foreign key."
        )

    expected_indexes = _WORKSPACE_INDEXES[table_name]
    indexes = _indexes_for_column(bind, table_name, column_name)
    actual_names = {index.get("name") for index in indexes}
    if actual_names != set(expected_indexes):
        raise RuntimeError(
            f"Cannot adopt voice ownership; {table_name}.{column_name} has an "
            "unexpected or incomplete workspace index set."
        )
    for index in indexes:
        if (
            index.get("name") not in expected_indexes
            or tuple(index["column_names"] or ()) != expected_indexes[index["name"]]
            or index.get("unique")
        ):
            raise RuntimeError(
                f"Cannot adopt voice ownership; workspace index "
                f"{index.get('name')!r} on {table_name} is malformed."
            )


def _validate_unadopted_scope(bind: sa.Connection) -> bool:
    present = {
        table_name for table_name in _TABLES if "workspace_id" in _column_info(bind, table_name)
    }
    if present and present != set(_TABLES):
        raise RuntimeError(
            "Cannot adopt voice ownership; only part of the ownership scope schema exists."
        )

    if not present:
        for table_name in _TABLES:
            if _foreign_keys_for_column(bind, table_name, "workspace_id"):
                raise RuntimeError(
                    "Cannot adopt voice ownership; a workspace foreign key exists "
                    f"without {table_name}.workspace_id."
                )
            if any(
                index.get("name") in _WORKSPACE_INDEXES[table_name]
                for index in sa.inspect(bind).get_indexes(table_name)
            ):
                raise RuntimeError(
                    "Cannot adopt voice ownership; a workspace index exists "
                    f"without {table_name}.workspace_id."
                )
        _validate_voice_name_uniqueness(bind, scoped=False)
        return False

    for table_name in _TABLES:
        _validate_scope_column(bind, table_name)
    _validate_voice_name_uniqueness(bind, scoped=True)
    for table_name in _TABLES:
        row = bind.execute(
            sa.text(
                f"SELECT id FROM {table_name} WHERE workspace_id IS NOT NULL ORDER BY id LIMIT 1"
            )
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot adopt voice ownership; voice records already contain "
                f"preexisting scope data on {table_name} row {row[0]!r}."
            )
    return True


def _initial_personal_workspace_id(bind: sa.Connection) -> str:
    required = {"users", "workspace_memberships"}
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt unbound voice records; required tables are missing: "
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
                "Cannot adopt unbound voice records; "
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
            "Cannot adopt unbound voice records; the seeded owner user is missing or ambiguous."
        )
    owner = owners[0]
    if not owner["is_active"] or not owner["is_instance_admin"]:
        raise RuntimeError(
            "Cannot adopt unbound voice records; the seeded owner user is inactive "
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
            "Cannot adopt unbound voice records; the seeded owner personal workspace "
            "is missing or ambiguous."
        )
    workspace = workspaces[0]
    if workspace["state"] != "active":
        raise RuntimeError(
            "Cannot adopt unbound voice records; the seeded owner personal workspace is not active."
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
            "Cannot adopt unbound voice records; the seeded owner workspace membership "
            "is missing or invalid."
        )
    return str(workspace["id"])


def _load_artifact_owners(bind: sa.Connection) -> dict[str, str]:
    rows = (
        bind.execute(
            sa.text(
                "WITH linked_artifacts(id) AS ("
                " SELECT artifact_id FROM voice_samples"
                " UNION SELECT source_artifact_id FROM training_runs"
                "  WHERE source_artifact_id IS NOT NULL"
                " UNION SELECT source_text_artifact_id FROM training_runs"
                "  WHERE source_text_artifact_id IS NOT NULL"
                " UNION SELECT output_artifact_id FROM training_runs"
                "  WHERE output_artifact_id IS NOT NULL"
                ") "
                "SELECT linked_artifacts.id AS linked_artifact_id, "
                "artifacts.id AS found_artifact_id, artifacts.workspace_id, "
                "workspaces.id AS found_workspace_id, workspaces.state AS workspace_state "
                "FROM linked_artifacts "
                "LEFT JOIN artifacts ON artifacts.id = linked_artifacts.id "
                "LEFT JOIN workspaces ON workspaces.id = artifacts.workspace_id "
                "ORDER BY linked_artifacts.id"
            )
        )
        .mappings()
        .all()
    )
    owners: dict[str, str] = {}
    for row in rows:
        artifact_id = str(row["linked_artifact_id"])
        if row["found_artifact_id"] is None:
            raise RuntimeError(
                f"Cannot adopt voice ownership; a voice or training record references "
                f"missing artifact {artifact_id!r}."
            )
        if (
            row["workspace_id"] is None
            or row["found_workspace_id"] is None
            or row["workspace_state"] not in {"active", "archived"}
        ):
            raise RuntimeError(
                f"Cannot adopt voice ownership; linked artifact {artifact_id!r} has "
                "missing or invalid stored workspace ownership."
            )
        owners[artifact_id] = str(row["workspace_id"])
    return owners


def _load_adoption_plan(
    bind: sa.Connection,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    voice_rows = bind.execute(sa.text("SELECT id FROM voices ORDER BY id")).mappings().all()
    sample_rows = (
        bind.execute(sa.text("SELECT id, voice_id, artifact_id FROM voice_samples ORDER BY id"))
        .mappings()
        .all()
    )
    training_rows = (
        bind.execute(
            sa.text(
                "SELECT id, voice_id, source_artifact_id, source_text_artifact_id, "
                "output_artifact_id FROM training_runs ORDER BY id"
            )
        )
        .mappings()
        .all()
    )

    voices = {str(row["id"]): row for row in voice_rows}
    groups: dict[tuple[str, str], set[str]] = {("voice", voice_id): set() for voice_id in voices}
    sample_groups: dict[str, tuple[str, str]] = {}
    training_groups: dict[str, tuple[str, str]] = {}
    artifact_groups: dict[str, set[tuple[str, str]]] = {}

    for row in sample_rows:
        sample_id = str(row["id"])
        voice_id = row["voice_id"]
        if voice_id is None or str(voice_id) not in voices:
            raise RuntimeError(
                f"Cannot adopt voice sample {sample_id!r}; its linked voice is missing."
            )
        if row["artifact_id"] is None:
            raise RuntimeError(
                f"Cannot adopt voice sample {sample_id!r}; its artifact link is missing."
            )
        group = ("voice", str(voice_id))
        sample_groups[sample_id] = group
        artifact_groups.setdefault(str(row["artifact_id"]), set()).add(group)

    for row in training_rows:
        training_id = str(row["id"])
        voice_id = row["voice_id"]
        if voice_id is not None:
            if str(voice_id) not in voices:
                raise RuntimeError(
                    f"Cannot adopt training run {training_id!r}; its linked voice "
                    f"{voice_id!r} is missing."
                )
            group = ("voice", str(voice_id))
        else:
            group = ("training_run", training_id)
            groups[group] = set()
        training_groups[training_id] = group
        for column in (
            "source_artifact_id",
            "source_text_artifact_id",
            "output_artifact_id",
        ):
            artifact_id = row[column]
            if artifact_id is not None:
                artifact_groups.setdefault(str(artifact_id), set()).add(group)

    artifact_owners = _load_artifact_owners(bind)
    for artifact_id, linked_groups in artifact_groups.items():
        owner = artifact_owners.get(artifact_id)
        if owner is None:
            raise RuntimeError(
                f"Cannot adopt voice ownership; artifact {artifact_id!r} has no "
                "validated stored workspace owner."
            )
        for group in linked_groups:
            groups[group].add(owner)

    fallback_needed = False
    group_owners: dict[tuple[str, str], str] = {}
    for group, evidence in groups.items():
        if len(evidence) > 1:
            raise RuntimeError(
                f"Cannot adopt voice group {group[1]!r}; linked artifact workspaces "
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

    voice_owners = {voice_id: group_owners[("voice", voice_id)] for voice_id in voices}
    sample_owners = {sample_id: group_owners[group] for sample_id, group in sample_groups.items()}
    training_owners = {
        training_id: group_owners[group] for training_id, group in training_groups.items()
    }
    if (
        set(voice_owners) != set(voices)
        or set(sample_owners) != {str(row["id"]) for row in sample_rows}
        or set(training_owners) != {str(row["id"]) for row in training_rows}
    ):
        raise RuntimeError("Voice ownership adoption did not cover every voice record.")
    return voice_owners, sample_owners, training_owners


def _ensure_schema(bind: sa.Connection, existing_scope: bool) -> None:
    if not existing_scope:
        with op.batch_alter_table(
            "voices", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
        ) as batch:
            batch.drop_constraint("uq_voices_name", type_="unique")
            batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
            batch.create_foreign_key(
                _WORKSPACE_FKS["voices"],
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_index("ix_voices_workspace_id", ["workspace_id"])
            batch.create_unique_constraint(
                _VOICE_WORKSPACE_UNIQUE[0], list(_VOICE_WORKSPACE_UNIQUE[1])
            )

        for table_name in ("voice_samples", "training_runs"):
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
    _validate_voice_name_uniqueness(bind, scoped=True)


def _apply_adoption_plan(
    bind: sa.Connection,
    voice_owners: dict[str, str],
    sample_owners: dict[str, str],
    training_owners: dict[str, str],
) -> None:
    for table_name, owners in (
        ("voices", voice_owners),
        ("voice_samples", sample_owners),
        ("training_runs", training_owners),
    ):
        for record_id, workspace_id in owners.items():
            result = bind.execute(
                sa.text(
                    f"UPDATE {table_name} SET workspace_id = :workspace_id "
                    "WHERE id = :record_id AND workspace_id IS NULL"
                ),
                {"workspace_id": workspace_id, "record_id": record_id},
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"Voice ownership changed for {table_name} record {record_id!r}."
                )


def _validate_adoption(bind: sa.Connection, table_name: str, plan: dict[str, str]) -> None:
    rows = (
        bind.execute(
            sa.text(
                "SELECT records.id, records.workspace_id, "
                "workspaces.id AS found_workspace_id, workspaces.state AS workspace_state "
                f"FROM {table_name} AS records "
                "LEFT JOIN workspaces ON workspaces.id = records.workspace_id "
                "ORDER BY records.id"
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != len(plan):
        raise RuntimeError(f"Voice ownership adoption changed the {table_name} count.")
    for row in rows:
        record_id = str(row["id"])
        if (
            row["workspace_id"] is None
            or row["found_workspace_id"] is None
            or row["workspace_state"] not in {"active", "archived"}
            or plan.get(record_id) != row["workspace_id"]
        ):
            raise RuntimeError(
                f"Voice record {record_id!r} has invalid adopted workspace ownership."
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    existing_scope = _validate_unadopted_scope(bind)
    voice_owners, sample_owners, training_owners = _load_adoption_plan(bind)

    _ensure_schema(bind, existing_scope)
    _apply_adoption_plan(bind, voice_owners, sample_owners, training_owners)
    _validate_adoption(bind, "voices", voice_owners)
    _validate_adoption(bind, "voice_samples", sample_owners)
    _validate_adoption(bind, "training_runs", training_owners)


def _require_downgrade_shape(bind: sa.Connection) -> None:
    present = {
        table_name for table_name in _TABLES if "workspace_id" in _column_info(bind, table_name)
    }
    if not present:
        return
    if present != set(_TABLES):
        raise RuntimeError(
            "Cannot downgrade voice ownership; only part of the scope schema exists."
        )
    for table_name in _TABLES:
        _validate_scope_column(bind, table_name)
    _validate_voice_name_uniqueness(bind, scoped=True)


def _require_global_name_compatibility(bind: sa.Connection) -> None:
    names = bind.execute(sa.text("SELECT id, name FROM voices ORDER BY id")).all()
    seen: dict[str, str] = {}
    for voice_id, name in names:
        prior_voice_id = seen.get(name)
        if prior_voice_id is not None:
            raise RuntimeError(
                "Cannot downgrade voice ownership; voice names would collide "
                f"between {prior_voice_id!r} and {voice_id!r}."
            )
        seen[name] = str(voice_id)


def downgrade() -> None:
    bind = op.get_bind()
    if not set(_TABLES) <= _table_names(bind):
        return
    _require_downgrade_shape(bind)
    if not any("workspace_id" in _column_info(bind, table_name) for table_name in _TABLES):
        return

    _require_global_name_compatibility(bind)

    with op.batch_alter_table("voices") as batch:
        batch.drop_constraint(_VOICE_WORKSPACE_UNIQUE[0], type_="unique")
        batch.drop_index("ix_voices_workspace_id")
        batch.drop_constraint(_WORKSPACE_FKS["voices"], type_="foreignkey")
        batch.drop_column("workspace_id")
        batch.create_unique_constraint("uq_voices_name", ["name"])

    for table_name in ("training_runs", "voice_samples"):
        with op.batch_alter_table(table_name) as batch:
            for index_name in _WORKSPACE_INDEXES[table_name]:
                batch.drop_index(index_name)
            batch.drop_constraint(_WORKSPACE_FKS[table_name], type_="foreignkey")
            batch.drop_column("workspace_id")
