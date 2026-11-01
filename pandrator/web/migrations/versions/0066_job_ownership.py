"""Adopt retained workspace and project ownership for historical jobs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import sqlalchemy as sa
from alembic import op

revision = "0066_job_ownership"
down_revision = "0065_upload_ownership"
branch_labels = None
depends_on = None

_TABLE = "jobs"
_WORKSPACE_FK = "fk_jobs_workspace_id_workspaces"
_PROJECT_FK = "fk_jobs_project_id_projects"
_WORKSPACE_INDEX = "ix_jobs_workspace_id"
_PROJECT_INDEX = "ix_jobs_project_id"
_SCOPE_COLUMNS = ("workspace_id", "project_id")
_OLD_COLUMNS: dict[str, tuple[type[sa.types.TypeEngine[Any]], int | None, bool]] = {
    "id": (sa.String, 36, False),
    "kind": (sa.String, 120, False),
    "session_id": (sa.String, 36, True),
    "workflow_run_id": (sa.String, 36, True),
    "status": (sa.String, 32, False),
    "payload_json": (sa.JSON, None, False),
    "resource_keys_json": (sa.JSON, None, False),
    "result_json": (sa.JSON, None, True),
    "progress": (sa.Float, None, False),
    "progress_detail": (sa.Text, None, True),
    "error_code": (sa.String, 120, True),
    "error_message": (sa.Text, None, True),
    "lease_owner": (sa.String, 120, True),
    "lease_expires_at": (sa.DateTime, None, True),
    "lease_generation": (sa.Integer, None, False),
    "available_at": (sa.DateTime, None, True),
    "attempts": (sa.Integer, None, False),
    "max_attempts": (sa.Integer, None, False),
    "created_at": (sa.DateTime, None, False),
    "started_at": (sa.DateTime, None, True),
    "finished_at": (sa.DateTime, None, True),
    "updated_at": (sa.DateTime, None, False),
}
_OLD_INDEXES: dict[str, tuple[str, ...]] = {
    "ix_jobs_kind": ("kind",),
    "ix_jobs_session_id": ("session_id",),
    "ix_jobs_status": ("status",),
    "ix_jobs_lease_owner": ("lease_owner",),
    "ix_jobs_lease_expires_at": ("lease_expires_at",),
    "ix_jobs_available_at": ("available_at",),
    "ix_jobs_created_at": ("created_at",),
    "idx_jobs_session_kind_created": ("session_id", "kind", "created_at", "id"),
    "idx_jobs_status_created": ("status", "created_at", "id"),
}
_PROJECT_RESULT_KINDS = {"audio.preview", "audio.waveform", "video.preview", "rvc.convert"}
_ARTIFACT_FIELDS: dict[str, tuple[str, ...]] = {
    "audio.preview": ("source_artifact_id",),
    "audio.waveform": ("source_artifact_id",),
    "video.preview": ("source_artifact_id",),
    "rvc.convert": ("source_artifact_id",),
    "session.bundle.import": ("source_artifact_id",),
    "rvc.model.upload": ("pth_artifact_id", "index_artifact_id"),
    "voice.normalize_recording": ("source_artifact_id",),
    "voice.publish": ("source_artifact_id",),
    "voice.unpublish": ("source_artifact_id",),
    "voice.transcribe": ("source_artifact_id",),
    "voice.google_design": ("source_artifact_id",),
}
_VOICE_FIELDS = {
    "voice.normalize_recording": ("voice_id", "sample_id"),
    "voice.publish": ("voice_id", "sample_id"),
    "voice.unpublish": ("voice_id",),
    "voice.transcribe": ("voice_id", "sample_id"),
    "voice.google_design": ("voice_id",),
}
_PAYLOAD_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    kind: tuple(("artifact", field) for field in _ARTIFACT_FIELDS.get(kind, ()))
    + tuple(("voice", field) for field in _VOICE_FIELDS.get(kind, ()))
    for kind in dict.fromkeys((*_ARTIFACT_FIELDS, *_VOICE_FIELDS))
}
_PAYLOAD_FIELDS.update(
    {
        "training.xtts": (("training", "training_id"),),
        "sound.generate": (("sound", "candidate_id"),),
        "maintenance.session_purge_preview": (("session", "target_session_id"),),
        "maintenance.session_purge": (("session", "target_session_id"),),
    }
)


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}


def _require_tables(bind: sa.Connection) -> None:
    required_columns = {
        "artifacts": {"id", "workspace_id", "project_id", "session_id"},
        "jobs": set(_OLD_COLUMNS),
        "projects": {"id", "workspace_id", "source_session_id"},
        "sessions": {"id", "project_id"},
        "sound_variants": {"id", "job_id", "workspace_id"},
        "training_runs": {"id", "job_id", "workspace_id"},
        "translation_projects": {"source_session_id", "project_root_id"},
        "voice_samples": {"id", "workspace_id", "voice_id"},
        "voices": {"id", "workspace_id"},
        "workflow_runs": {"id", "session_id"},
        "workspaces": {"id", "kind", "personal_owner_user_id", "state"},
        "users": {"id", "legacy_subject", "is_active", "is_instance_admin"},
        "workspace_memberships": {"workspace_id", "user_id", "role"},
    }
    missing_tables = set(required_columns) - _table_names(bind)
    if missing_tables:
        raise RuntimeError(
            "Cannot adopt job ownership; required tables are missing: "
            + ", ".join(sorted(missing_tables))
        )
    for table_name, expected in required_columns.items():
        missing_columns = expected - set(_columns(bind, table_name))
        if missing_columns:
            raise RuntimeError(
                f"Cannot adopt job ownership; {table_name} is missing columns: "
                + ", ".join(sorted(missing_columns))
            )


def _foreign_key_signatures(
    bind: sa.Connection,
) -> list[tuple[tuple[str, ...], str, tuple[str, ...], str | None, str | None]]:
    return [
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
            (foreign_key.get("options") or {}).get("ondelete", "").upper() or None,
            foreign_key.get("name"),
        )
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
    ]


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
            f"Cannot adopt job ownership; {_TABLE}.{column_name} has an unexpected definition."
        )

    foreign_keys = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(_TABLE)
        if column_name in foreign_key["constrained_columns"]
    ]
    if len(foreign_keys) != 1:
        raise RuntimeError(
            f"Cannot adopt job ownership; {_TABLE}.{column_name} must have one ownership foreign key."
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
            f"Cannot adopt job ownership; {_TABLE}.{column_name} has an unexpected foreign key."
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
            f"Cannot adopt job ownership; {_TABLE}.{column_name} has a malformed ownership index."
        )


def _validate_jobs_schema(bind: sa.Connection, *, target: bool) -> None:
    columns = _columns(bind, _TABLE)
    present_scope = {name for name in _SCOPE_COLUMNS if name in columns}
    if target and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot adopt job ownership; only part of the ownership schema exists.")
    if not target and present_scope:
        raise RuntimeError("Cannot adopt job ownership; scope columns already exist.")
    expected_columns = set(_OLD_COLUMNS) | (set(_SCOPE_COLUMNS) if target else set())
    if set(columns) != expected_columns:
        raise RuntimeError(
            "Cannot adopt job ownership; jobs has a partial or malformed column shape."
        )

    for name, (type_class, length, nullable) in _OLD_COLUMNS.items():
        column = columns[name]
        default = column.get("default")
        # 0003/0019 added SQL defaults on older installations; metadata-created
        # tables have Python defaults instead. Preserve both genuine shapes.
        valid_default = (
            default is None
            or (name == "lease_generation" and str(default).strip() in {"0", "(0)"})
            or (name == "resource_keys_json" and str(default).strip() in {"'[]'", "('[]')"})
        )
        if (
            not isinstance(column["type"], type_class)
            or getattr(column["type"], "length", None) != length
            or column["nullable"] != nullable
            or not valid_default
            or column.get("primary_key", False) != (name == "id")
        ):
            raise RuntimeError(
                f"Cannot adopt job ownership; jobs.{name} has an unexpected legacy definition."
            )

    primary_key = tuple(sa.inspect(bind).get_pk_constraint(_TABLE).get("constrained_columns") or ())
    if primary_key != ("id",):
        raise RuntimeError("Cannot adopt job ownership; jobs has an unexpected primary key.")

    expected_foreign_keys = {
        (("session_id",), "sessions", ("id",), "CASCADE", None),
        (("workflow_run_id",), "workflow_runs", ("id",), "CASCADE", None),
    }
    actual_foreign_keys = _foreign_key_signatures(bind)
    if not expected_foreign_keys.issubset(set(actual_foreign_keys)):
        raise RuntimeError("Cannot adopt job ownership; jobs is missing an existing foreign key.")
    for column_name in _SCOPE_COLUMNS:
        scope_foreign_keys = [
            foreign_key for foreign_key in actual_foreign_keys if column_name in foreign_key[0]
        ]
        if not target and scope_foreign_keys:
            raise RuntimeError(
                "Cannot adopt job ownership; a scope foreign key exists without its columns."
            )

    indexes = sa.inspect(bind).get_indexes(_TABLE)
    for index_name, index_columns in _OLD_INDEXES.items():
        matches = [index for index in indexes if index.get("name") == index_name]
        if (
            len(matches) != 1
            or matches[0].get("column_names") != list(index_columns)
            or matches[0].get("unique")
        ):
            raise RuntimeError(
                f"Cannot adopt job ownership; jobs index {index_name!r} is missing or malformed."
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
        for index in indexes:
            if set(index.get("column_names") or ()) & set(_SCOPE_COLUMNS):
                raise RuntimeError(
                    "Cannot adopt job ownership; a scope index exists without its columns."
                )


def _validate_empty_target(bind: sa.Connection) -> None:
    populated = bind.execute(
        sa.text(
            "SELECT id FROM jobs WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL "
            "ORDER BY id LIMIT 1"
        )
    ).first()
    if populated is not None:
        raise RuntimeError(
            f"Cannot adopt job ownership; jobs already contains scope data on row {populated[0]!r}."
        )


def _stored_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Cannot adopt job ownership; {label} is missing or malformed.")
    return value


def _workspace_state(bind: sa.Connection, workspace_id: str) -> str:
    workspace_id = _stored_id(workspace_id, label="workspace ID")
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
            f"Cannot adopt job ownership; workspace {workspace_id!r} is missing or invalid."
        )
    return str(row["state"])


def _project_owner(bind: sa.Connection, project_id: str) -> tuple[str, str]:
    project_id = _stored_id(project_id, label="project ID")
    row = (
        bind.execute(
            sa.text(
                "SELECT id, workspace_id, source_session_id FROM projects WHERE id = :project_id"
            ),
            {"project_id": project_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(f"Cannot adopt job ownership; project {project_id!r} is missing.")
    workspace_id = _stored_id(row["workspace_id"], label=f"project {project_id!r} workspace")
    _workspace_state(bind, workspace_id)
    source_session_id = _stored_id(
        row["source_session_id"], label=f"project {project_id!r} source session"
    )
    source = (
        bind.execute(
            sa.text("SELECT id, project_id FROM sessions WHERE id = :session_id"),
            {"session_id": source_session_id},
        )
        .mappings()
        .first()
    )
    if source is None or source["project_id"] != project_id:
        raise RuntimeError(
            f"Cannot adopt job ownership; project {project_id!r} has an invalid source root."
        )

    roots = (
        bind.execute(
            sa.text(
                "SELECT project_root_id FROM translation_projects "
                "WHERE source_session_id = :source_session_id ORDER BY id"
            ),
            {"source_session_id": source_session_id},
        )
        .scalars()
        .all()
    )
    if any(root is not None and root != project_id for root in roots):
        raise RuntimeError(
            f"Cannot adopt job ownership; project {project_id!r} conflicts with its translation root."
        )
    return project_id, workspace_id


def _session_owner(bind: sa.Connection, session_id: str) -> tuple[str, str]:
    session_id = _stored_id(session_id, label="session ID")
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
            f"Cannot adopt job ownership; session {session_id!r} has no retained project."
        )
    return _project_owner(bind, _stored_id(row["project_id"], label="session project ID"))


def _workflow_owner(bind: sa.Connection, workflow_run_id: str) -> tuple[str, str, str]:
    workflow_run_id = _stored_id(workflow_run_id, label="workflow run ID")
    row = (
        bind.execute(
            sa.text("SELECT id, session_id FROM workflow_runs WHERE id = :id"),
            {"id": workflow_run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"Cannot adopt job ownership; workflow run {workflow_run_id!r} is missing."
        )
    session_id = _stored_id(row["session_id"], label="workflow session ID")
    project_id, workspace_id = _session_owner(bind, session_id)
    return session_id, project_id, workspace_id


def _artifact_owner(bind: sa.Connection, artifact_id: str) -> tuple[str, str | None] | None:
    artifact_id = _stored_id(artifact_id, label="artifact ID")
    row = (
        bind.execute(
            sa.text(
                "SELECT id, workspace_id, project_id, session_id FROM artifacts WHERE id = :id"
            ),
            {"id": artifact_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    workspace_id = _stored_id(row["workspace_id"], label=f"artifact {artifact_id!r} workspace")
    _workspace_state(bind, workspace_id)

    project_id: str | None = None
    if row["project_id"] is not None:
        project_id = _stored_id(row["project_id"], label=f"artifact {artifact_id!r} project")
        _project_id, project_workspace_id = _project_owner(bind, project_id)
        if project_workspace_id != workspace_id:
            raise RuntimeError(
                f"Cannot adopt job ownership; artifact {artifact_id!r} conflicts with its project workspace."
            )

    if row["session_id"] is not None:
        session_project_id, session_workspace_id = _session_owner(
            bind, _stored_id(row["session_id"], label=f"artifact {artifact_id!r} session")
        )
        if project_id != session_project_id or workspace_id != session_workspace_id:
            raise RuntimeError(
                f"Cannot adopt job ownership; artifact {artifact_id!r} has conflicting session ownership."
            )
    return workspace_id, project_id


def _voice_owner(bind: sa.Connection, voice_id: str) -> tuple[str, None] | None:
    voice_id = _stored_id(voice_id, label="voice ID")
    row = (
        bind.execute(
            sa.text("SELECT id, workspace_id FROM voices WHERE id = :id"),
            {"id": voice_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    workspace_id = _stored_id(row["workspace_id"], label=f"voice {voice_id!r} workspace")
    _workspace_state(bind, workspace_id)
    return workspace_id, None


def _sample_owner(bind: sa.Connection, sample_id: str) -> tuple[str, None] | None:
    sample_id = _stored_id(sample_id, label="voice sample ID")
    row = (
        bind.execute(
            sa.text("SELECT id, workspace_id, voice_id FROM voice_samples WHERE id = :id"),
            {"id": sample_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    workspace_id = _stored_id(row["workspace_id"], label=f"sample {sample_id!r} workspace")
    _workspace_state(bind, workspace_id)
    voice_id = _stored_id(row["voice_id"], label=f"sample {sample_id!r} voice")
    voice = (
        bind.execute(
            sa.text("SELECT id, workspace_id FROM voices WHERE id = :id"),
            {"id": voice_id},
        )
        .mappings()
        .first()
    )
    if voice is None:
        raise RuntimeError(
            f"Cannot adopt job ownership; sample {sample_id!r} references a missing voice."
        )
    voice_workspace_id = _stored_id(
        voice["workspace_id"], label=f"sample {sample_id!r} voice workspace"
    )
    _workspace_state(bind, voice_workspace_id)
    if voice_workspace_id != workspace_id:
        raise RuntimeError(
            f"Cannot adopt job ownership; sample {sample_id!r} conflicts with its voice workspace."
        )
    return workspace_id, None


def _training_owner(bind: sa.Connection, training_id: str) -> tuple[str, None] | None:
    training_id = _stored_id(training_id, label="training ID")
    row = (
        bind.execute(
            sa.text("SELECT id, workspace_id FROM training_runs WHERE id = :id"),
            {"id": training_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    workspace_id = _stored_id(row["workspace_id"], label=f"training run {training_id!r} workspace")
    _workspace_state(bind, workspace_id)
    return workspace_id, None


def _sound_owner(bind: sa.Connection, candidate_id: str) -> tuple[str, None] | None:
    candidate_id = _stored_id(candidate_id, label="sound candidate ID")
    row = (
        bind.execute(
            sa.text("SELECT id, workspace_id FROM sound_variants WHERE id = :id"),
            {"id": candidate_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    workspace_id = _stored_id(
        row["workspace_id"], label=f"sound variant {candidate_id!r} workspace"
    )
    _workspace_state(bind, workspace_id)
    return workspace_id, None


def _payload_object(raw_payload: Any) -> Mapping[str, Any] | None:
    if raw_payload is None:
        return None
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _payload_roots(
    bind: sa.Connection, *, kind: str, raw_payload: Any
) -> list[tuple[str, str, str | None]]:
    payload = _payload_object(raw_payload)
    if payload is None:
        return []

    roots: list[tuple[str, str, str | None]] = []
    for root_kind, field_name in _PAYLOAD_FIELDS.get(kind, ()):
        if field_name not in payload:
            continue
        root_id = payload[field_name]
        if not isinstance(root_id, str) or not root_id.strip():
            # Failed generic jobs can legitimately retain incomplete inputs.
            # Such values supply no ownership evidence and are not rewritten.
            continue
        owner: tuple[str, str | None] | tuple[str, None] | None
        if root_kind == "artifact":
            owner = _artifact_owner(bind, root_id)
        elif root_kind == "voice":
            if field_name == "voice_id":
                owner = _voice_owner(bind, root_id)
            else:
                owner = _sample_owner(bind, root_id)
        elif root_kind == "training":
            owner = _training_owner(bind, root_id)
        elif root_kind == "sound":
            owner = _sound_owner(bind, root_id)
        else:
            exists = bind.execute(
                sa.text("SELECT id FROM sessions WHERE id = :session_id"),
                {"session_id": root_id},
            ).first()
            if exists is None:
                continue
            session_owner = _session_owner(bind, root_id)
            owner = (session_owner[1], session_owner[0])
        if owner is None:
            continue
        workspace_id, project_id = owner
        selected_project = (
            project_id if kind in _PROJECT_RESULT_KINDS or root_kind == "session" else None
        )
        roots.append((root_kind, workspace_id, selected_project))
    return roots


def _linked_workspace_roots(bind: sa.Connection, job_id: str) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    for table_name, label in (
        ("training_runs", "training run"),
        ("sound_variants", "sound variant"),
    ):
        rows = (
            bind.execute(
                sa.text(
                    f"SELECT id, workspace_id FROM {table_name} WHERE job_id = :job_id ORDER BY id"
                ),
                {"job_id": job_id},
            )
            .mappings()
            .all()
        )
        for row in rows:
            row_id = _stored_id(row["id"], label=f"linked {label} ID")
            workspace_id = _stored_id(
                row["workspace_id"], label=f"linked {label} {row_id!r} workspace"
            )
            _workspace_state(bind, workspace_id)
            roots.append((label, workspace_id))
    return roots


def _job_owner(bind: sa.Connection, row: Mapping[str, Any]) -> tuple[str, str | None]:
    job_id = _stored_id(row["id"], label="job ID")
    selected_session: tuple[str, str, str] | None = None
    if row["session_id"] is not None:
        session_id = _stored_id(row["session_id"], label=f"job {job_id!r} session ID")
        project_id, workspace_id = _session_owner(bind, session_id)
        selected_session = (session_id, project_id, workspace_id)

    selected_workflow: tuple[str, str, str] | None = None
    if row["workflow_run_id"] is not None:
        workflow_run_id = _stored_id(
            row["workflow_run_id"], label=f"job {job_id!r} workflow run ID"
        )
        selected_workflow = _workflow_owner(bind, workflow_run_id)
        if selected_session is not None and selected_workflow[0] != selected_session[0]:
            raise RuntimeError(
                f"Cannot adopt job {job_id!r}; its workflow session conflicts with its session link."
            )

    preferred = selected_session or selected_workflow
    workspace_roots: list[tuple[str, str]] = []
    project_id: str | None = None
    if preferred is not None:
        _root_session_id, project_id, workspace_id = preferred
        workspace_roots.append(("session/workflow", workspace_id))

    payload_roots = _payload_roots(
        bind,
        kind=str(row["kind"]),
        raw_payload=row["payload_json"],
    )
    for root_kind, workspace_id, root_project_id in payload_roots:
        workspace_roots.append((root_kind, workspace_id))
        if preferred is None and project_id is None and root_project_id is not None:
            project_id = root_project_id

    workspace_roots.extend(_linked_workspace_roots(bind, job_id))
    distinct_workspaces = {workspace_id for _root_kind, workspace_id in workspace_roots}
    if len(distinct_workspaces) > 1:
        raise RuntimeError(
            f"Cannot adopt job {job_id!r}; retained ownership roots conflict across workspaces."
        )
    if distinct_workspaces:
        workspace_id = next(iter(distinct_workspaces))
        return workspace_id, project_id
    return "", None


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
            "Cannot adopt unrooted jobs; the seeded owner user is missing, ambiguous, "
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
            "Cannot adopt unrooted jobs; the seeded owner personal workspace is "
            "missing, ambiguous, or inactive."
        )
    workspace_id = _stored_id(workspaces[0]["id"], label="seeded personal workspace ID")
    membership = (
        bind.execute(
            sa.text(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND user_id = :owner_id"
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        )
        .scalars()
        .all()
    )
    if membership != ["owner"]:
        raise RuntimeError(
            "Cannot adopt unrooted jobs; the seeded owner workspace membership is missing or invalid."
        )
    return workspace_id


def _load_adoption_plan(bind: sa.Connection) -> dict[str, tuple[str, str | None]]:
    rows = (
        bind.execute(
            sa.text(
                "SELECT id, kind, session_id, workflow_run_id, "
                "CAST(payload_json AS TEXT) AS payload_json FROM jobs ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    plan: dict[str, tuple[str, str | None]] = {}
    needs_fallback = False
    for row in rows:
        job_id = _stored_id(row["id"], label="job ID")
        workspace_id, project_id = _job_owner(bind, cast(Mapping[str, Any], row))
        if not workspace_id:
            needs_fallback = True
        plan[job_id] = (workspace_id, project_id)

    if needs_fallback:
        fallback_workspace_id = _initial_personal_workspace_id(bind)
        plan = {
            job_id: (fallback_workspace_id if not workspace_id else workspace_id, project_id)
            for job_id, (workspace_id, project_id) in plan.items()
        }
    if len(plan) != len(rows) or any(not workspace_id for workspace_id, _ in plan.values()):
        raise RuntimeError("Job ownership adoption did not cover every job.")
    return plan


def _job_index_definitions(
    bind: sa.Connection, *, excluded_names: set[str] | None = None
) -> dict[str, str]:
    if bind.dialect.name != "sqlite":
        return {}
    excluded_names = excluded_names or set()
    rows = bind.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'jobs' AND sql IS NOT NULL ORDER BY name"
    ).all()
    return {
        str(name): str(sql) for name, sql in rows if name not in excluded_names and sql is not None
    }


def _restore_job_index_definitions(bind: sa.Connection, definitions: Mapping[str, str]) -> None:
    if not definitions or bind.dialect.name != "sqlite":
        return
    current = _job_index_definitions(bind)
    quote = bind.dialect.identifier_preparer.quote
    for name, sql in definitions.items():
        if current.get(name) == sql:
            continue
        if name in current:
            bind.exec_driver_sql(f"DROP INDEX {quote(name)}")
        bind.exec_driver_sql(sql)


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
    _validate_jobs_schema(bind, target=True)


def upgrade() -> None:
    bind = op.get_bind()
    _require_tables(bind)
    columns = _columns(bind, _TABLE)
    present_scope = {name for name in _SCOPE_COLUMNS if name in columns}
    if present_scope and present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot adopt job ownership; only part of the ownership schema exists.")
    existing_scope = bool(present_scope)
    _validate_jobs_schema(bind, target=existing_scope)
    if existing_scope:
        _validate_empty_target(bind)

    # Validate every SQL and typed payload root before changing schema or job rows.
    plan = _load_adoption_plan(bind)
    old_indexes = {} if existing_scope else _job_index_definitions(bind)

    _ensure_scope_schema(bind, existing_scope=existing_scope)
    _restore_job_index_definitions(bind, old_indexes)
    for job_id, (workspace_id, project_id) in plan.items():
        result = bind.execute(
            sa.text(
                "UPDATE jobs SET workspace_id = :workspace_id, project_id = :project_id "
                "WHERE id = :job_id AND workspace_id IS NULL AND project_id IS NULL"
            ),
            {"workspace_id": workspace_id, "project_id": project_id, "job_id": job_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(f"Cannot adopt job {job_id!r}; its scope changed during migration.")


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in _table_names(bind):
        return
    columns = _columns(bind, _TABLE)
    present_scope = {name for name in _SCOPE_COLUMNS if name in columns}
    if not present_scope:
        return
    if present_scope != set(_SCOPE_COLUMNS):
        raise RuntimeError("Cannot remove job ownership; only part of the ownership schema exists.")
    _validate_jobs_schema(bind, target=True)
    old_indexes = _job_index_definitions(bind, excluded_names={_WORKSPACE_INDEX, _PROJECT_INDEX})
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_index(_WORKSPACE_INDEX)
        batch.drop_index(_PROJECT_INDEX)
        batch.drop_constraint(_WORKSPACE_FK, type_="foreignkey")
        batch.drop_constraint(_PROJECT_FK, type_="foreignkey")
        batch.drop_column("workspace_id")
        batch.drop_column("project_id")
    _restore_job_index_definitions(bind, old_indexes)
    _validate_jobs_schema(bind, target=False)
