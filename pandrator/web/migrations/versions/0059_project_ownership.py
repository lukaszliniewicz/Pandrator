"""Add personal workspaces and adopt existing sessions as owned projects."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

import sqlalchemy as sa
from alembic import op

revision = "0059_project_ownership"
down_revision = "0058_project_folders"
branch_labels = None
depends_on = None


def _table_names(bind: sa.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(bind).get_indexes(table_name)
        if index["name"] is not None
    }


def _has_unique(bind: sa.Connection, table_name: str, columns: tuple[str, ...]) -> bool:
    inspector = sa.inspect(bind)
    if any(
        tuple(constraint["column_names"]) == columns
        for constraint in inspector.get_unique_constraints(table_name)
    ):
        return True
    return any(
        index["unique"] and tuple(index["column_names"]) == columns
        for index in inspector.get_indexes(table_name)
    )


def _require_legacy_tables(bind: sa.Connection) -> None:
    required = {
        "sessions",
        "translation_projects",
        "translation_project_branches",
        "project_folders",
        "project_folder_memberships",
    }
    missing = required - _table_names(bind)
    if missing:
        raise RuntimeError(
            "Cannot adopt project ownership; required legacy tables are missing: "
            + ", ".join(sorted(missing))
        )


def _legacy_rows(bind: sa.Connection) -> dict[str, list[Mapping[str, Any]]]:
    return {
        "sessions": [
            cast(Mapping[str, Any], row)
            for row in bind.execute(
                sa.text("SELECT id, name, created_at, updated_at FROM sessions")
            ).mappings()
        ],
        "translations": [
            cast(Mapping[str, Any], row)
            for row in bind.execute(
                sa.text("SELECT id, name, source_session_id FROM translation_projects")
            ).mappings()
        ],
        "branches": [
            cast(Mapping[str, Any], row)
            for row in bind.execute(
                sa.text("SELECT id, project_id, session_id FROM translation_project_branches")
            ).mappings()
        ],
        "folders": [
            cast(Mapping[str, Any], row)
            for row in bind.execute(sa.text("SELECT id FROM project_folders")).mappings()
        ],
        "memberships": [
            cast(Mapping[str, Any], row)
            for row in bind.execute(
                sa.text(
                    "SELECT folder_id, project_session_id, position FROM project_folder_memberships"
                )
            ).mappings()
        ],
    }


def _validate_legacy_graph(
    rows: dict[str, list[Mapping[str, Any]]],
) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
    sessions = {row["id"]: row for row in rows["sessions"]}
    translations = rows["translations"]
    translation_by_source: dict[str, Mapping[str, Any]] = {}
    translation_by_id: dict[str, Mapping[str, Any]] = {}
    for translation in translations:
        source_id = translation["source_session_id"]
        if source_id not in sessions:
            raise RuntimeError(
                f"Translation project {translation['id']!r} has a missing source session."
            )
        if source_id in translation_by_source:
            raise RuntimeError(
                f"Session {source_id!r} is the source of multiple translation projects."
            )
        translation_by_source[source_id] = translation
        translation_by_id[translation["id"]] = translation

    source_ids = set(translation_by_source)
    session_source = {session_id: session_id for session_id in sessions}
    branch_ids: set[str] = set()
    for branch in rows["branches"]:
        project = translation_by_id.get(branch["project_id"])
        if project is None:
            raise RuntimeError(
                f"Translation branch {branch['id']!r} has a missing translation project."
            )
        branch_session_id = branch["session_id"]
        if branch_session_id not in sessions:
            raise RuntimeError(f"Translation branch {branch['id']!r} has a missing session.")
        if branch_session_id in branch_ids:
            raise RuntimeError(
                f"Session {branch_session_id!r} is attached as more than one translation branch."
            )
        if branch_session_id in source_ids:
            raise RuntimeError(
                f"Translation session {branch_session_id!r} is both a source and a branch."
            )
        source_id = project["source_session_id"]
        if branch_session_id == source_id:
            raise RuntimeError(f"Translation source {source_id!r} cannot also be its own branch.")
        branch_ids.add(branch_session_id)
        session_source[branch_session_id] = source_id

    folders = {row["id"] for row in rows["folders"]}
    membership_roots: set[tuple[str, str]] = set()
    for membership in rows["memberships"]:
        folder_id = membership["folder_id"]
        session_id = membership["project_session_id"]
        if folder_id not in folders:
            raise RuntimeError(
                f"Project-folder membership references missing folder {folder_id!r}."
            )
        if session_id not in sessions:
            raise RuntimeError(
                f"Project-folder membership references missing session {session_id!r}."
            )
        canonical_source_id = session_source[session_id]
        if session_id != canonical_source_id:
            raise RuntimeError(
                "Project-folder membership is not canonical: "
                f"session {session_id!r} must use source session "
                f"{canonical_source_id!r}."
            )
        root_key = (folder_id, canonical_source_id)
        if root_key in membership_roots:
            raise RuntimeError(
                "Project-folder memberships contain duplicate canonical projects "
                f"for folder {folder_id!r}."
            )
        membership_roots.add(root_key)

    return session_source, translation_by_source


def _validate_unadopted_targets(bind: sa.Connection) -> None:
    table_names = _table_names(bind)
    targets = {
        "sessions": "project_id",
        "translation_projects": "project_root_id",
        "project_folders": "workspace_id",
        "project_folder_memberships": "project_id",
    }
    for table_name, column_name in targets.items():
        if column_name not in _column_names(bind, table_name):
            continue
        found = bind.execute(
            sa.text(f"SELECT 1 FROM {table_name} WHERE {column_name} IS NOT NULL LIMIT 1")
        ).first()
        if found is not None:
            raise RuntimeError(
                f"Cannot adopt project ownership; {table_name}.{column_name} "
                "already contains ownership data."
            )

    for table_name in (
        "users",
        "workspaces",
        "workspace_memberships",
        "projects",
        "project_memberships",
    ):
        if table_name in table_names:
            found = bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first()
            if found is not None:
                raise RuntimeError(
                    f"Cannot adopt project ownership; {table_name} already contains rows."
                )


def _create_tables_if_missing(bind: sa.Connection) -> None:
    tables = _table_names(bind)
    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("legacy_subject", sa.String(255), nullable=True),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("is_instance_admin", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("legacy_subject", name="uq_users_legacy_subject"),
        )
    tables = _table_names(bind)
    if "workspaces" not in tables:
        op.create_table(
            "workspaces",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "personal_owner_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("kind IN ('personal', 'team')", name="ck_workspaces_kind"),
            sa.CheckConstraint(
                "(kind = 'personal' AND personal_owner_user_id IS NOT NULL) "
                "OR (kind = 'team' AND personal_owner_user_id IS NULL)",
                name="ck_workspaces_personal_owner",
            ),
            sa.CheckConstraint("state IN ('active', 'archived')", name="ck_workspaces_state"),
            sa.CheckConstraint("revision >= 1", name="ck_workspaces_revision"),
            sa.UniqueConstraint(
                "personal_owner_user_id",
                name="uq_workspaces_personal_owner_user_id",
            ),
        )
    tables = _table_names(bind)
    if "workspace_memberships" not in tables:
        op.create_table(
            "workspace_memberships",
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                primary_key=True,
            ),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "role IN ('owner', 'admin', 'member')",
                name="ck_workspace_memberships_role",
            ),
        )
    tables = _table_names(bind)
    if "projects" not in tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "source_session_id",
                sa.String(36),
                sa.ForeignKey("sessions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "created_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("revision >= 1", name="ck_projects_revision"),
            sa.UniqueConstraint("source_session_id", name="uq_projects_source_session_id"),
        )
    tables = _table_names(bind)
    if "project_memberships" not in tables:
        op.create_table(
            "project_memberships",
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                primary_key=True,
            ),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column(
                "granted_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "role IN ('manager', 'editor', 'reviewer', 'viewer')",
                name="ck_project_memberships_role",
            ),
        )


def _add_nullable_column_if_missing(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    column: sa.Column[Any],
) -> None:
    if column_name not in _column_names(bind, table_name):
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(column)


def _ensure_columns_and_indexes(bind: sa.Connection) -> None:
    _add_nullable_column_if_missing(
        bind,
        "sessions",
        "project_id",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey(
                "projects.id",
                ondelete="RESTRICT",
                name="fk_sessions_project_id_projects",
            ),
            nullable=True,
        ),
    )
    _add_nullable_column_if_missing(
        bind,
        "translation_projects",
        "project_root_id",
        sa.Column(
            "project_root_id",
            sa.String(36),
            sa.ForeignKey(
                "projects.id",
                ondelete="RESTRICT",
                name="fk_translation_projects_project_root_id_projects",
            ),
            nullable=True,
        ),
    )
    _add_nullable_column_if_missing(
        bind,
        "project_folders",
        "workspace_id",
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey(
                "workspaces.id",
                ondelete="RESTRICT",
                name="fk_project_folders_workspace_id_workspaces",
            ),
            nullable=True,
        ),
    )
    _add_nullable_column_if_missing(
        bind,
        "project_folder_memberships",
        "project_id",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey(
                "projects.id",
                ondelete="CASCADE",
                name="fk_project_folder_memberships_project_id_projects",
            ),
            nullable=True,
        ),
    )

    indexes = _index_names(bind, "projects")
    if "ix_projects_workspace_id" not in indexes:
        op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"])
    indexes = _index_names(bind, "sessions")
    if "ix_sessions_project_id" not in indexes:
        op.create_index("ix_sessions_project_id", "sessions", ["project_id"])
    indexes = _index_names(bind, "project_folders")
    if "ix_project_folders_workspace_id" not in indexes:
        op.create_index("ix_project_folders_workspace_id", "project_folders", ["workspace_id"])

    if not _has_unique(bind, "projects", ("source_session_id",)):
        with op.batch_alter_table("projects") as batch:
            batch.create_unique_constraint("uq_projects_source_session_id", ["source_session_id"])
    if not _has_unique(bind, "translation_projects", ("project_root_id",)):
        with op.batch_alter_table("translation_projects") as batch:
            batch.create_unique_constraint(
                "uq_translation_projects_project_root_id", ["project_root_id"]
            )
    if not _has_unique(bind, "project_folder_memberships", ("folder_id", "project_id")):
        with op.batch_alter_table("project_folder_memberships") as batch:
            batch.create_unique_constraint(
                "uq_project_folder_memberships_folder_project",
                ["folder_id", "project_id"],
            )


def _seed_personal_workspace(bind: sa.Connection) -> tuple[str, str]:
    owner_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    bind.execute(
        sa.text(
            "INSERT INTO users "
            "(id, legacy_subject, display_name, is_active, is_instance_admin, "
            "created_at, updated_at) "
            "VALUES (:id, 'owner', 'Owner', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"id": owner_id},
    )
    bind.execute(
        sa.text(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, "
            "created_at, updated_at) "
            "VALUES (:id, 'personal', 'Personal', :owner_id, 'active', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"id": workspace_id, "owner_id": owner_id},
    )
    bind.execute(
        sa.text(
            "INSERT INTO workspace_memberships "
            "(workspace_id, user_id, role, created_at) "
            "VALUES (:workspace_id, :owner_id, 'owner', CURRENT_TIMESTAMP)"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    )
    return owner_id, workspace_id


def _adopt_rows(
    bind: sa.Connection,
    rows: dict[str, list[Mapping[str, Any]]],
    session_source: dict[str, str],
    translation_by_source: dict[str, Mapping[str, Any]],
    workspace_id: str,
) -> None:
    project_id_by_source = {
        source_id: str(uuid.uuid4()) for source_id in dict.fromkeys(session_source.values())
    }
    sessions = {row["id"]: row for row in rows["sessions"]}
    for source_id, project_id in project_id_by_source.items():
        source = sessions[source_id]
        translation = translation_by_source.get(source_id)
        name = translation["name"] if translation is not None else source["name"]
        bind.execute(
            sa.text(
                "INSERT INTO projects "
                "(id, workspace_id, source_session_id, name, created_by_user_id, "
                "revision, created_at, updated_at) "
                "VALUES (:id, :workspace_id, :source_id, :name, NULL, 1, "
                ":created_at, :updated_at)"
            ),
            {
                "id": project_id,
                "workspace_id": workspace_id,
                "source_id": source_id,
                "name": name,
                "created_at": source["created_at"],
                "updated_at": source["updated_at"],
            },
        )

    for session_id, source_id in session_source.items():
        bind.execute(
            sa.text("UPDATE sessions SET project_id = :project_id WHERE id = :id"),
            {"project_id": project_id_by_source[source_id], "id": session_id},
        )
    for source_id, translation in translation_by_source.items():
        bind.execute(
            sa.text("UPDATE translation_projects SET project_root_id = :project_id WHERE id = :id"),
            {"project_id": project_id_by_source[source_id], "id": translation["id"]},
        )
    bind.execute(
        sa.text("UPDATE project_folders SET workspace_id = :workspace_id"),
        {"workspace_id": workspace_id},
    )
    for membership in rows["memberships"]:
        source_id = session_source[membership["project_session_id"]]
        bind.execute(
            sa.text(
                "UPDATE project_folder_memberships SET project_id = :project_id "
                "WHERE folder_id = :folder_id AND project_session_id = :session_id"
            ),
            {
                "project_id": project_id_by_source[source_id],
                "folder_id": membership["folder_id"],
                "session_id": membership["project_session_id"],
            },
        )

    _validate_adoption(
        bind,
        rows,
        session_source,
        translation_by_source,
        project_id_by_source,
        workspace_id,
    )


def _validate_adoption(
    bind: sa.Connection,
    rows: dict[str, list[Mapping[str, Any]]],
    session_source: dict[str, str],
    translation_by_source: dict[str, Mapping[str, Any]],
    project_id_by_source: dict[str, str],
    workspace_id: str,
) -> None:
    missing_sessions = bind.execute(
        sa.text("SELECT id FROM sessions WHERE project_id IS NULL LIMIT 1")
    ).first()
    missing_folders = bind.execute(
        sa.text("SELECT id FROM project_folders WHERE workspace_id IS NULL LIMIT 1")
    ).first()
    missing_memberships = bind.execute(
        sa.text("SELECT folder_id FROM project_folder_memberships WHERE project_id IS NULL LIMIT 1")
    ).first()
    missing_translations = bind.execute(
        sa.text("SELECT id FROM translation_projects WHERE project_root_id IS NULL LIMIT 1")
    ).first()
    if any(
        item is not None
        for item in (
            missing_sessions,
            missing_folders,
            missing_memberships,
            missing_translations,
        )
    ):
        raise RuntimeError("Project ownership adoption left a null ownership link.")

    for source_id, project_id in project_id_by_source.items():
        root = (
            bind.execute(
                sa.text(
                    "SELECT workspace_id, source_session_id, created_by_user_id, name "
                    "FROM projects WHERE id = :id"
                ),
                {"id": project_id},
            )
            .mappings()
            .one()
        )
        if (
            root["workspace_id"] != workspace_id
            or root["source_session_id"] != source_id
            or root["created_by_user_id"] is not None
        ):
            raise RuntimeError(f"Project root for legacy source {source_id!r} is inconsistent.")
        expected_name = (
            translation_by_source[source_id]["name"]
            if source_id in translation_by_source
            else next(row["name"] for row in rows["sessions"] if row["id"] == source_id)
        )
        if root["name"] != expected_name:
            raise RuntimeError(f"Project root for legacy source {source_id!r} has the wrong name.")

    for session_id, source_id in session_source.items():
        project_id = bind.execute(
            sa.text("SELECT project_id FROM sessions WHERE id = :id"),
            {"id": session_id},
        ).scalar_one()
        if project_id != project_id_by_source[source_id]:
            raise RuntimeError(f"Session {session_id!r} was adopted into the wrong project root.")
    for source_id, translation in translation_by_source.items():
        root_id = bind.execute(
            sa.text("SELECT project_root_id FROM translation_projects WHERE id = :id"),
            {"id": translation["id"]},
        ).scalar_one()
        if root_id != project_id_by_source[source_id]:
            raise RuntimeError(
                f"Translation project {translation['id']!r} has the wrong project root."
            )
    for folder in rows["folders"]:
        workspace = bind.execute(
            sa.text("SELECT workspace_id FROM project_folders WHERE id = :id"),
            {"id": folder["id"]},
        ).scalar_one()
        if workspace != workspace_id:
            raise RuntimeError(f"Project folder {folder['id']!r} has the wrong workspace.")
    for membership in rows["memberships"]:
        session_id = membership["project_session_id"]
        source_id = session_source[session_id]
        project_id = bind.execute(
            sa.text(
                "SELECT project_id FROM project_folder_memberships "
                "WHERE folder_id = :folder_id AND project_session_id = :session_id"
            ),
            {
                "folder_id": membership["folder_id"],
                "session_id": session_id,
            },
        ).scalar_one()
        if project_id != project_id_by_source[source_id]:
            raise RuntimeError(
                "Project-folder membership does not match its canonical source project."
            )


def upgrade() -> None:
    bind = op.get_bind()
    _require_legacy_tables(bind)
    rows = _legacy_rows(bind)
    session_source, translation_by_source = _validate_legacy_graph(rows)
    _validate_unadopted_targets(bind)

    _create_tables_if_missing(bind)
    _ensure_columns_and_indexes(bind)
    _owner_id, workspace_id = _seed_personal_workspace(bind)
    _adopt_rows(
        bind,
        rows,
        session_source,
        translation_by_source,
        workspace_id,
    )


def _drop_column_if_present(bind: sa.Connection, table_name: str, column_name: str) -> None:
    if column_name in _column_names(bind, table_name):
        with op.batch_alter_table(table_name) as batch:
            batch.drop_column(column_name)


def _drop_index_if_present(bind: sa.Connection, table_name: str, index_name: str) -> None:
    if index_name in _index_names(bind, table_name):
        op.drop_index(index_name, table_name=table_name)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)
    if "sessions" in tables:
        _drop_index_if_present(bind, "sessions", "ix_sessions_project_id")
        _drop_column_if_present(bind, "sessions", "project_id")
    if "translation_projects" in tables:
        _drop_column_if_present(bind, "translation_projects", "project_root_id")
    if "project_folders" in tables:
        _drop_index_if_present(bind, "project_folders", "ix_project_folders_workspace_id")
        _drop_column_if_present(bind, "project_folders", "workspace_id")
    if "project_folder_memberships" in tables:
        _drop_column_if_present(bind, "project_folder_memberships", "project_id")

    tables = _table_names(bind)
    for table_name in (
        "project_memberships",
        "projects",
        "workspace_memberships",
        "workspaces",
        "users",
    ):
        if table_name in tables:
            op.drop_table(table_name)
