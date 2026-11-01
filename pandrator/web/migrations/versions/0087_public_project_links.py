"""Optional project bearer links, bound browser sessions and review submissions."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import SchemaItem

revision = "0087_public_project_links"
down_revision = "0086_storage_reservations"
branch_labels = None
depends_on = None


def _create(name: str, *items: SchemaItem) -> None:
    # 0001 bootstraps current metadata. An existing table is acceptable only if
    # it is structurally identical to this migration's independent definition.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(name):
        expected = sa.Table(name, sa.MetaData(), *items)
        columns = inspector.get_columns(name)
        if {column["name"] for column in columns} != set(expected.columns.keys()) or any(
            column["nullable"] != expected.c[column["name"]].nullable
            or str(column["type"]) != str(expected.c[column["name"]].type)
            for column in columns
        ):
            raise RuntimeError(f"Existing {name} table does not match the migration schema.")
        expected_checks = {
            str(item.sqltext)
            for item in expected.constraints
            if isinstance(item, sa.CheckConstraint)
        }
        actual_checks = {item["sqltext"] for item in inspector.get_check_constraints(name)}
        expected_unique = {
            tuple(item.columns.keys())
            for item in expected.constraints
            if isinstance(item, sa.UniqueConstraint)
        }
        actual_unique = {
            tuple(item["column_names"]) for item in inspector.get_unique_constraints(name)
        }
        expected_fks = {
            (
                tuple(item.column_keys),
                tuple(element.target_fullname for element in item.elements),
                item.ondelete,
            )
            for item in expected.foreign_key_constraints
        }
        actual_fks = {
            (
                tuple(item["constrained_columns"]),
                tuple(f"{item['referred_table']}.{column}" for column in item["referred_columns"]),
                item.get("options", {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(name)
        }
        if (
            expected_checks != actual_checks
            or expected_unique != actual_unique
            or expected_fks != actual_fks
            or set(inspector.get_pk_constraint(name)["constrained_columns"])
            != set(expected.primary_key.columns.keys())
        ):
            raise RuntimeError(f"Existing {name} constraints do not match the migration schema.")
        return
    op.create_table(name, *items)


def _index(name: str, table: str, column: str) -> None:
    indexes = sa.inspect(op.get_bind()).get_indexes(table)
    existing = next((index for index in indexes if index["name"] == name), None)
    if existing is not None:
        if existing["column_names"] != [column] or existing["unique"]:
            raise RuntimeError(f"Existing {name} index does not match the migration schema.")
        return
    op.create_index(name, table, [column])


def _upload_attachment_mode() -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns("upload_sessions")
    }
    existing = columns.get("attach_to_session")
    if existing is None:
        op.add_column(
            "upload_sessions",
            sa.Column("attach_to_session", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    elif (
        not isinstance(existing["type"], sa.Boolean)
        or existing["nullable"]
        or str(existing.get("default") or "").strip("()'\"") not in {"1", "true", "TRUE"}
    ):
        raise RuntimeError("Existing upload attachment mode does not match the migration schema.")


def upgrade() -> None:
    _upload_attachment_mode()
    _create(
        "project_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("issuer_user_id", sa.String(36), nullable=False),
        sa.Column("issuer_auth_revision", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("target_session_id", sa.String(36)),
        sa.Column("target_artifact_id", sa.String(36)),
        sa.Column("target_revision_id", sa.String(36)),
        sa.Column("target_hash", sa.String(128)),
        sa.Column("target_revision_number", sa.Integer()),
        sa.Column("target_composition_hash", sa.String(128)),
        sa.Column("request_key", sa.String(100)),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("root_session_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issuer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "mode IN ('manage_project', 'review_translation')", name="ck_project_links_mode"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_project_links_revision"),
        sa.UniqueConstraint("token_hash", name="uq_project_links_token_hash"),
        sa.UniqueConstraint(
            "project_id", "issuer_user_id", "request_key", name="uq_project_links_request"
        ),
    )
    _index("ix_project_links_project_id", "project_links", "project_id")
    _create(
        "project_link_sessions",
        sa.Column("browser_session_id", sa.String(36), primary_key=True),
        sa.Column("link_id", sa.String(36), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_auth_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["link_id"], ["project_links.id"], ondelete="CASCADE"),
    )
    _index("ix_project_link_sessions_link_id", "project_link_sessions", "link_id")
    _create(
        "project_link_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("link_id", sa.String(36), nullable=False),
        sa.Column("browser_session_id", sa.String(36)),
        sa.Column("reviewer_label", sa.String(80)),
        sa.Column("changes_json", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(100), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_user_id", sa.String(36)),
        sa.Column("accepted_artifact_id", sa.String(36)),
        sa.ForeignKeyConstraint(["link_id"], ["project_links.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_project_link_submissions_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_project_link_submissions_revision"),
        sa.UniqueConstraint(
            "link_id",
            "browser_session_id",
            "request_key",
            name="uq_project_link_submissions_request",
        ),
    )
    _index("ix_project_link_submissions_link_id", "project_link_submissions", "link_id")


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM upload_sessions WHERE attach_to_session = 0 LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Cannot discard staged upload attachment modes.")
    if op.get_bind().execute(sa.text("SELECT 1 FROM project_link_submissions LIMIT 1")).first():
        raise RuntimeError("Cannot discard retained translation submissions.")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM project_links WHERE revoked_at IS NULL AND expires_at > :now LIMIT 1"
            ).bindparams(sa.bindparam("now", type_=sa.DateTime(timezone=True))),
            {"now": datetime.now(UTC)},
        )
        .first()
    ):
        raise RuntimeError("Cannot discard live project links.")
    op.drop_table("project_link_submissions")
    op.drop_table("project_link_sessions")
    op.drop_table("project_links")
    op.drop_column("upload_sessions", "attach_to_session")
