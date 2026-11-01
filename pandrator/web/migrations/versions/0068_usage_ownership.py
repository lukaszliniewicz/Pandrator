"""Adopt retained workspace and project ownership for usage receipts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0068_usage_ownership"
down_revision = "0067_research_cache_scope"
branch_labels = None
depends_on = None

_TABLE = "usage_events"
_SCOPE_COLUMNS = ("workspace_id", "project_id")
_WORKSPACE_FK = "fk_usage_events_workspace_id_workspaces"
_PROJECT_FK = "fk_usage_events_project_id_projects"
_WORKSPACE_INDEX = "ix_usage_events_workspace_id"
_PROJECT_INDEX = "ix_usage_events_project_id"

# These are the columns created by the historical model and migrations.  In
# particular, integer/JSON Python-side defaults never became SQL defaults.
_OLD_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool, bool]] = {
    "id": (sa.String, 36, False, True),
    "session_id": (sa.String, 36, True, False),
    "workflow_run_id": (sa.String, 36, True, False),
    "job_id": (sa.String, 36, True, False),
    "artifact_id": (sa.String, 36, True, False),
    "generation_run_id": (sa.String, 36, True, False),
    "agent_run_id": (sa.String, 36, True, False),
    "request_key": (sa.String, 200, True, False),
    "stage": (sa.String, 80, True, False),
    "provider_key": (sa.String, 80, False, False),
    "model_id": (sa.String, 255, False, False),
    "input_tokens": (sa.Integer, None, False, False),
    "cached_input_tokens": (sa.Integer, None, False, False),
    "output_tokens": (sa.Integer, None, False, False),
    "cost_usd": (sa.Float, None, True, False),
    "cost_source": (sa.String, 40, True, False),
    "raw_usage_json": (sa.JSON, None, False, False),
    "created_at": (sa.DateTime, None, False, False),
}
_OLD_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_usage_events_session_id": ("session_id",),
    "ix_usage_events_job_id": ("job_id",),
    "ix_usage_events_artifact_id": ("artifact_id",),
    "ix_usage_events_generation_run_id": ("generation_run_id",),
    "ix_usage_events_agent_run_id": ("agent_run_id",),
    "ix_usage_events_created_at": ("created_at",),
    "idx_usage_events_session_stage_created": (
        "session_id",
        "stage",
        "created_at",
        "id",
    ),
}
_OLD_FOREIGN_KEYS = {
    (("session_id",), "sessions", ("id",), "SET NULL"),
    (("workflow_run_id",), "workflow_runs", ("id",), "SET NULL"),
    (("job_id",), "jobs", ("id",), "SET NULL"),
    (("artifact_id",), "artifacts", ("id",), "SET NULL"),
    (("generation_run_id",), "generation_runs", ("id",), "SET NULL"),
    (("agent_run_id",), "agent_runs", ("id",), "SET NULL"),
}


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required_columns = {
        _TABLE: set(_OLD_COLUMNS),
        "agent_runs": {"id", "session_id", "source_artifact_id", "result_artifact_id", "job_id"},
        "artifacts": {"id", "workspace_id", "project_id", "session_id"},
        "generation_runs": {"id", "session_id", "job_id"},
        "jobs": {"id", "workspace_id", "project_id", "session_id", "workflow_run_id"},
        "projects": {"id", "workspace_id", "source_session_id"},
        "sessions": {"id", "project_id"},
        "source_assets": {"id", "artifact_id", "workspace_id"},
        "translation_projects": {"id", "source_session_id", "project_root_id"},
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workflow_runs": {"id", "session_id"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
    }
    missing_tables = set(required_columns) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot adopt usage ownership; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot adopt usage ownership; {table_name} is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_key_signatures(
    bind: sa.Connection,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    return {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete", "").upper(),
        )
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
    }


def _validate_no_inbound_foreign_keys(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    for table_name in sorted(_table_names(bind) - {_TABLE}):
        if any(
            foreign_key["referred_table"] == _TABLE
            for foreign_key in inspector.get_foreign_keys(table_name)
        ):
            raise RuntimeError(
                f"Cannot adopt usage ownership; an unexpected table references {_TABLE!r}."
            )


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
            f"Cannot adopt usage ownership; {_TABLE}.{column_name} has an unexpected definition."
        )

    foreign_keys = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
        if column_name in foreign_key["constrained_columns"]
    ]
    if (
        len(foreign_keys) != 1
        or foreign_keys[0].get("name") != foreign_key_name
        or foreign_keys[0]["constrained_columns"] != [column_name]
        or foreign_keys[0]["referred_table"] != referred_table
        or foreign_keys[0]["referred_columns"] != ["id"]
        or (foreign_keys[0].get("options") or {}).get("ondelete", "").upper() != "RESTRICT"
    ):
        raise RuntimeError(
            f"Cannot adopt usage ownership; {_TABLE}.{column_name} has an unexpected foreign key."
        )

    indexes = [
        index for index in sa.inspect(bind).get_indexes(_TABLE) if index.get("name") == index_name
    ]
    if (
        len(indexes) != 1
        or indexes[0].get("column_names") != [column_name]
        or indexes[0].get("unique")
    ):
        raise RuntimeError(
            f"Cannot adopt usage ownership; {_TABLE}.{column_name} has a malformed index."
        )


def _validate_usage_schema(bind: sa.Connection, *, target: bool) -> None:
    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if target and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot adopt usage ownership; only part of the target schema exists.")
    if not target and present_scope:
        raise RuntimeError("Cannot adopt usage ownership; scope columns already exist.")
    expected_columns = set(_OLD_COLUMNS) | (set(_SCOPE_COLUMNS) if target else set())
    if set(columns) != expected_columns:
        raise RuntimeError(
            "Cannot adopt usage ownership; usage_events has an unexpected column shape."
        )

    for name, (type_class, length, nullable, primary_key) in _OLD_COLUMNS.items():
        column = columns[name]
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or column.get("default") is not None
            or column.get("primary_key", False) != primary_key
        ):
            raise RuntimeError(
                f"Cannot adopt usage ownership; {_TABLE}.{name} has an unexpected definition."
            )

    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != ("id",):
        raise RuntimeError(
            "Cannot adopt usage ownership; usage_events.id is no longer its primary key."
        )

    unique_constraints = {
        (constraint.get("name"), tuple(constraint.get("column_names") or ()))
        for constraint in sa.inspect(bind).get_unique_constraints(_TABLE)
    }
    if unique_constraints != {("uq_usage_event_agent_request", ("agent_run_id", "request_key"))}:
        raise RuntimeError(
            "Cannot adopt usage ownership; the agent/request uniqueness constraint changed."
        )
    if sa.inspect(bind).get_check_constraints(_TABLE):
        raise RuntimeError("Cannot adopt usage ownership; usage_events has unexpected checks.")

    expected_foreign_keys = set(_OLD_FOREIGN_KEYS)
    if target:
        expected_foreign_keys.update(
            {
                (("workspace_id",), "workspaces", ("id",), "RESTRICT"),
                (("project_id",), "projects", ("id",), "RESTRICT"),
            }
        )
    if _foreign_key_signatures(bind) != expected_foreign_keys:
        raise RuntimeError(
            "Cannot adopt usage ownership; usage_events has an unexpected foreign-key shape."
        )

    indexes = sa.inspect(bind).get_indexes(_TABLE)
    required_indexes = dict(_OLD_INDEXES)
    if target:
        required_indexes.update(
            {
                _WORKSPACE_INDEX: ("workspace_id",),
                _PROJECT_INDEX: ("project_id",),
            }
        )
    for index_name, index_columns in required_indexes.items():
        matches = [index for index in indexes if index.get("name") == index_name]
        if (
            len(matches) != 1
            or matches[0].get("column_names") != list(index_columns)
            or matches[0].get("unique")
        ):
            raise RuntimeError(
                f"Cannot adopt usage ownership; index {index_name!r} is missing or malformed."
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


def _validate_empty_target(bind: sa.Connection) -> None:
    row = bind.execute(
        sa.text(
            "SELECT id FROM usage_events "
            "WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL ORDER BY id LIMIT 1"
        )
    ).first()
    if row is not None:
        raise RuntimeError(
            "Cannot adopt usage ownership; an early target event already has ownership "
            f"values on row {row[0]!r}."
        )


def _stored_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Cannot adopt usage ownership; {label} is missing or malformed.")
    return value


def _workspace_state(bind: sa.Connection, workspace_id: str) -> str:
    workspace_id = _stored_id(workspace_id, label="workspace ID")
    row = (
        bind.execute(
            sa.text("SELECT state FROM workspaces WHERE id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["state"] not in {"active", "archived"}:
        raise RuntimeError(
            f"Cannot adopt usage ownership; workspace {workspace_id!r} is missing or invalid."
        )
    return str(row["state"])


def _validate_translation_roots(
    bind: sa.Connection, *, project_id: str, source_session_id: str | None
) -> None:
    if source_session_id is None:
        rows = (
            bind.execute(
                sa.text(
                    "SELECT id, source_session_id, project_root_id FROM translation_projects "
                    "WHERE project_root_id = :project_id ORDER BY id"
                ),
                {"project_id": project_id},
            )
            .mappings()
            .all()
        )
    else:
        rows = (
            bind.execute(
                sa.text(
                    "SELECT id, source_session_id, project_root_id FROM translation_projects "
                    "WHERE project_root_id = :project_id OR source_session_id = :source_session_id "
                    "ORDER BY id"
                ),
                {"project_id": project_id, "source_session_id": source_session_id},
            )
            .mappings()
            .all()
        )
    for row in rows:
        translation_id = _stored_id(row["id"], label="translation project ID")
        translation_source = _stored_id(
            row["source_session_id"],
            label=f"translation project {translation_id!r} source session ID",
        )
        source = (
            bind.execute(
                sa.text("SELECT project_id FROM sessions WHERE id = :session_id"),
                {"session_id": translation_source},
            )
            .mappings()
            .first()
        )
        if source is None or source["project_id"] != project_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; project {project_id!r} conflicts with a "
                "translation source root."
            )
        translation_root = row["project_root_id"]
        if translation_root is not None and translation_root != project_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; project {project_id!r} conflicts with a "
                "translation project root."
            )


def _project_owner(bind: sa.Connection, project_id: str) -> tuple[str, str]:
    project_id = _stored_id(project_id, label="project ID")
    row = (
        bind.execute(
            sa.text("SELECT workspace_id, source_session_id FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt usage ownership; project {project_id!r} is missing.")
    workspace_id = _stored_id(row["workspace_id"], label=f"project {project_id!r} workspace ID")
    _workspace_state(bind, workspace_id)
    source_session_id: str | None = None
    if row["source_session_id"] is not None:
        source_session_id = _stored_id(
            row["source_session_id"], label=f"project {project_id!r} source session ID"
        )
        source = (
            bind.execute(
                sa.text("SELECT project_id FROM sessions WHERE id = :session_id"),
                {"session_id": source_session_id},
            )
            .mappings()
            .first()
        )
        if source is None or source["project_id"] != project_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; project {project_id!r} has an invalid source root."
            )
    else:
        remaining_sessions = bind.execute(
            sa.text("SELECT COUNT(*) FROM sessions WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one()
        if int(remaining_sessions) != 0:
            raise RuntimeError(
                f"Cannot adopt usage ownership; retained project {project_id!r} has sessions "
                "but no source session."
            )
    _validate_translation_roots(
        bind,
        project_id=project_id,
        source_session_id=source_session_id,
    )
    return workspace_id, project_id


def _session_owner(bind: sa.Connection, session_id: str) -> tuple[str, str]:
    session_id = _stored_id(session_id, label="session ID")
    row = (
        bind.execute(
            sa.text("SELECT project_id FROM sessions WHERE id = :session_id"),
            {"session_id": session_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt usage ownership; session {session_id!r} is missing.")
    project_id = _stored_id(row["project_id"], label=f"session {session_id!r} project ID")
    return _project_owner(bind, project_id)


def _workflow_owner(bind: sa.Connection, workflow_run_id: str) -> tuple[str, str, str]:
    workflow_run_id = _stored_id(workflow_run_id, label="workflow run ID")
    row = (
        bind.execute(
            sa.text("SELECT session_id FROM workflow_runs WHERE id = :workflow_run_id"),
            {"workflow_run_id": workflow_run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"Cannot adopt usage ownership; workflow run {workflow_run_id!r} is missing."
        )
    session_id = _stored_id(row["session_id"], label=f"workflow run {workflow_run_id!r} session ID")
    workspace_id, project_id = _session_owner(bind, session_id)
    return session_id, workspace_id, project_id


def _reconcile_roots(
    bind: sa.Connection,
    roots: list[tuple[str, str | None]],
    *,
    label: str,
) -> tuple[str, str | None]:
    workspaces = {workspace_id for workspace_id, _project_id in roots}
    projects = {project_id for _workspace_id, project_id in roots if project_id is not None}
    if len(workspaces) != 1:
        raise RuntimeError(
            f"Cannot adopt usage ownership; {label} has missing or conflicting workspace roots."
        )
    if len(projects) > 1:
        raise RuntimeError(f"Cannot adopt usage ownership; {label} has conflicting project roots.")
    workspace_id = next(iter(workspaces))
    project_id = next(iter(projects), None)
    if project_id is not None:
        project_workspace_id, _ = _project_owner(bind, project_id)
        if project_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; {label} project and workspace roots conflict."
            )
    return workspace_id, project_id


def _job_owner(bind: sa.Connection, job_id: str) -> tuple[str, str | None]:
    job_id = _stored_id(job_id, label="job ID")
    row = (
        bind.execute(
            sa.text(
                "SELECT workspace_id, project_id, session_id, workflow_run_id "
                "FROM jobs WHERE id = :job_id"
            ),
            {"job_id": job_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt usage ownership; job {job_id!r} is missing.")

    workspace_id = _stored_id(row["workspace_id"], label=f"job {job_id!r} workspace ID")
    _workspace_state(bind, workspace_id)
    roots: list[tuple[str, str | None]] = [(workspace_id, None)]
    stored_project_id: str | None = None
    if row["project_id"] is not None:
        stored_project_id = _stored_id(row["project_id"], label=f"job {job_id!r} project ID")
        project_workspace_id, project_id = _project_owner(bind, stored_project_id)
        if project_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; job {job_id!r} conflicts with its project workspace."
            )
        roots.append((project_workspace_id, project_id))

    selected_session_id: str | None = None
    if row["session_id"] is not None:
        selected_session_id = _stored_id(row["session_id"], label=f"job {job_id!r} session ID")
        session_workspace_id, session_project_id = _session_owner(bind, selected_session_id)
        if stored_project_id is None or session_project_id != stored_project_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; job {job_id!r} stored project conflicts "
                "with its session root."
            )
        roots.append((session_workspace_id, session_project_id))

    if row["workflow_run_id"] is not None:
        workflow_run_id = _stored_id(
            row["workflow_run_id"], label=f"job {job_id!r} workflow run ID"
        )
        workflow_session_id, workflow_workspace_id, workflow_project_id = _workflow_owner(
            bind, workflow_run_id
        )
        if selected_session_id is not None and workflow_session_id != selected_session_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; job {job_id!r} workflow session conflicts "
                "with its session link."
            )
        if stored_project_id is None or workflow_project_id != stored_project_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; job {job_id!r} stored project conflicts "
                "with its workflow root."
            )
        roots.append((workflow_workspace_id, workflow_project_id))

    return _reconcile_roots(bind, roots, label=f"job {job_id!r}")


def _artifact_owner(bind: sa.Connection, artifact_id: str) -> tuple[str, str | None]:
    artifact_id = _stored_id(artifact_id, label="artifact ID")
    row = (
        bind.execute(
            sa.text(
                "SELECT workspace_id, project_id, session_id FROM artifacts WHERE id = :artifact_id"
            ),
            {"artifact_id": artifact_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt usage ownership; artifact {artifact_id!r} is missing.")
    workspace_id = _stored_id(row["workspace_id"], label=f"artifact {artifact_id!r} workspace ID")
    _workspace_state(bind, workspace_id)
    roots: list[tuple[str, str | None]] = [(workspace_id, None)]

    if row["project_id"] is not None:
        artifact_project_id = _stored_id(
            row["project_id"], label=f"artifact {artifact_id!r} project ID"
        )
        project_workspace_id, project_id = _project_owner(bind, artifact_project_id)
        if project_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; artifact {artifact_id!r} conflicts with its project workspace."
            )
        roots.append((project_workspace_id, project_id))

    if row["session_id"] is not None:
        session_id = _stored_id(row["session_id"], label=f"artifact {artifact_id!r} session ID")
        session_workspace_id, session_project_id = _session_owner(bind, session_id)
        if workspace_id != session_workspace_id or row["project_id"] != session_project_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; artifact {artifact_id!r} has conflicting session ownership."
            )
        roots.append((session_workspace_id, session_project_id))

    assets = (
        bind.execute(
            sa.text(
                "SELECT id, workspace_id FROM source_assets "
                "WHERE artifact_id = :artifact_id ORDER BY id"
            ),
            {"artifact_id": artifact_id},
        )
        .mappings()
        .all()
    )
    for asset in assets:
        asset_id = _stored_id(asset["id"], label="source asset ID")
        asset_workspace_id = _stored_id(
            asset["workspace_id"], label=f"source asset {asset_id!r} workspace ID"
        )
        _workspace_state(bind, asset_workspace_id)
        if asset_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt usage ownership; artifact {artifact_id!r} conflicts with "
                f"source asset {asset_id!r}."
            )

    return _reconcile_roots(bind, roots, label=f"artifact {artifact_id!r}")


def _generation_owner(bind: sa.Connection, generation_run_id: str) -> tuple[str, str | None]:
    generation_run_id = _stored_id(generation_run_id, label="generation run ID")
    row = (
        bind.execute(
            sa.text("SELECT session_id, job_id FROM generation_runs WHERE id = :generation_run_id"),
            {"generation_run_id": generation_run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"Cannot adopt usage ownership; generation run {generation_run_id!r} is missing."
        )
    session_id = _stored_id(
        row["session_id"], label=f"generation run {generation_run_id!r} session ID"
    )
    roots: list[tuple[str, str | None]] = [_session_owner(bind, session_id)]
    if row["job_id"] is not None:
        job_id = _stored_id(row["job_id"], label=f"generation run {generation_run_id!r} job ID")
        roots.append(_job_owner(bind, job_id))
    return _reconcile_roots(bind, roots, label=f"generation run {generation_run_id!r}")


def _agent_owner(bind: sa.Connection, agent_run_id: str) -> tuple[str, str | None]:
    agent_run_id = _stored_id(agent_run_id, label="agent run ID")
    row = (
        bind.execute(
            sa.text(
                "SELECT session_id, job_id, source_artifact_id, result_artifact_id "
                "FROM agent_runs WHERE id = :agent_run_id"
            ),
            {"agent_run_id": agent_run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt usage ownership; agent run {agent_run_id!r} is missing.")
    session_id = _stored_id(row["session_id"], label=f"agent run {agent_run_id!r} session ID")
    roots: list[tuple[str, str | None]] = [_session_owner(bind, session_id)]
    if row["job_id"] is not None:
        job_id = _stored_id(row["job_id"], label=f"agent run {agent_run_id!r} job ID")
        roots.append(_job_owner(bind, job_id))
    if row["source_artifact_id"] is not None:
        source_artifact_id = _stored_id(
            row["source_artifact_id"],
            label=f"agent run {agent_run_id!r} source_artifact_id",
        )
        source_workspace_id, _source_project_id = _artifact_owner(bind, source_artifact_id)
        # The source artifact is an input dependency. Its project can differ
        # from the AgentRun's billing project when both share a workspace.
        roots.append((source_workspace_id, None))
    if row["result_artifact_id"] is not None:
        result_artifact_id = _stored_id(
            row["result_artifact_id"],
            label=f"agent run {agent_run_id!r} result_artifact_id",
        )
        roots.append(_artifact_owner(bind, result_artifact_id))
    return _reconcile_roots(bind, roots, label=f"agent run {agent_run_id!r}")


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
    if (
        len(owners) != 1
        or not bool(owners[0]["is_active"])
        or not bool(owners[0]["is_instance_admin"])
    ):
        raise RuntimeError(
            "Cannot adopt unbound usage events; the seeded owner is missing, ambiguous, "
            "inactive, or not an instance admin."
        )
    owner_id = _stored_id(owners[0]["id"], label="seeded owner ID")
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
            "Cannot adopt unbound usage events; the seeded personal workspace is missing, "
            "ambiguous, or inactive."
        )
    workspace_id = _stored_id(workspaces[0]["id"], label="seeded personal workspace ID")
    membership = (
        bind.execute(
            sa.text(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND user_id = :owner_id ORDER BY role"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )
        .scalars()
        .all()
    )
    if membership != ["owner"]:
        raise RuntimeError(
            "Cannot adopt unbound usage events; the seeded owner workspace membership "
            "is missing or invalid."
        )
    return workspace_id


def _event_owner(
    bind: sa.Connection,
    row: Mapping[Any, Any],
    *,
    fallback_workspace_id: str,
) -> tuple[str, str | None]:
    event_id = _stored_id(row["id"], label="usage event ID")
    roots: list[tuple[str, str | None]] = []
    for column_name in _OLD_COLUMNS:
        if (
            column_name
            not in {
                "session_id",
                "workflow_run_id",
                "job_id",
                "artifact_id",
                "generation_run_id",
                "agent_run_id",
            }
            or row[column_name] is None
        ):
            continue
        root_id = _stored_id(row[column_name], label=f"usage event {event_id!r} {column_name}")
        if column_name == "session_id":
            roots.append(_session_owner(bind, root_id))
        elif column_name == "workflow_run_id":
            _workflow_session_id, workspace_id, project_id = _workflow_owner(bind, root_id)
            roots.append((workspace_id, project_id))
        elif column_name == "job_id":
            roots.append(_job_owner(bind, root_id))
        elif column_name == "artifact_id":
            roots.append(_artifact_owner(bind, root_id))
        elif column_name == "generation_run_id":
            roots.append(_generation_owner(bind, root_id))
        elif column_name == "agent_run_id":
            roots.append(_agent_owner(bind, root_id))
    if not roots:
        return fallback_workspace_id, None
    return _reconcile_roots(bind, roots, label=f"usage event {event_id!r}")


def _load_adoption_plan(
    bind: sa.Connection, *, fallback_workspace_id: str
) -> dict[str, tuple[str, str | None]]:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, session_id, workflow_run_id, job_id, artifact_id, "
                "generation_run_id, agent_run_id FROM usage_events ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    plan: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        event_id = _stored_id(row["id"], label="usage event ID")
        plan[event_id] = _event_owner(
            bind,
            row,
            fallback_workspace_id=fallback_workspace_id,
        )
    if len(plan) != len(rows):
        raise RuntimeError("Cannot adopt usage ownership; event IDs are not unique.")
    return plan


def _index_definitions(
    bind: sa.Connection, *, exclude_names: set[str] | None = None
) -> dict[str, str]:
    excluded = exclude_names or set()
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"
        ),
        {"table_name": _TABLE},
    ).all()
    return {str(name): str(sql) for name, sql in rows if str(name) not in excluded}


def _restore_index_definitions(bind: sa.Connection, definitions: Mapping[str, str]) -> None:
    preparer = bind.dialect.identifier_preparer
    for name, sql in definitions.items():
        bind.exec_driver_sql(f"DROP INDEX IF EXISTS {preparer.quote(name)}")
        bind.exec_driver_sql(sql)


def _adopt_schema(bind: sa.Connection, old_indexes: Mapping[str, str]) -> None:
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
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
    _restore_index_definitions(bind, old_indexes)
    op.create_index(_WORKSPACE_INDEX, _TABLE, ["workspace_id"], unique=False)
    op.create_index(_PROJECT_INDEX, _TABLE, ["project_id"], unique=False)


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)

    columns = _columns(bind, _TABLE)
    present_scope = set(_SCOPE_COLUMNS) & set(columns)
    if present_scope == set(_SCOPE_COLUMNS):
        _validate_usage_schema(bind, target=True)
        _validate_empty_target(bind)
        early_target = True
    elif present_scope:
        raise RuntimeError("Cannot adopt usage ownership; only part of the target columns exists.")
    else:
        _validate_usage_schema(bind, target=False)
        early_target = False

    # The seeded fallback is checked even when every event has an explicit root
    # or the table is empty, so malformed initial identity never passes unnoticed.
    fallback_workspace_id = _initial_personal_workspace_id(bind)
    plan = _load_adoption_plan(bind, fallback_workspace_id=fallback_workspace_id)

    old_indexes = {} if early_target else _index_definitions(bind)
    if not early_target:
        _adopt_schema(bind, old_indexes)
    _validate_usage_schema(bind, target=True)

    for event_id, (workspace_id, project_id) in plan.items():
        bind.execute(
            sa.text(
                "UPDATE usage_events SET workspace_id = :workspace_id, project_id = :project_id "
                "WHERE id = :event_id"
            ),
            {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "event_id": event_id,
            },
        )

    _validate_usage_schema(bind, target=True)


def downgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    _validate_no_inbound_foreign_keys(bind)
    _validate_usage_schema(bind, target=True)
    old_indexes = _index_definitions(
        bind,
        exclude_names={_WORKSPACE_INDEX, _PROJECT_INDEX},
    )
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_constraint(_PROJECT_FK, type_="foreignkey")
        batch.drop_index(_WORKSPACE_INDEX)
        batch.drop_index(_PROJECT_INDEX)
        batch.drop_column("workspace_id")
        batch.drop_column("project_id")
    _restore_index_definitions(bind, old_indexes)
    _validate_usage_schema(bind, target=False)
