"""Add scoped principals, automation enrollment, and bounded audit.

Revision ID: 0023_scoped_api_principals
Revises: 0022_source_asset_backfill
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from pandrator.web.models import Base

revision = "0023_scoped_api_principals"
down_revision = "0022_source_asset_backfill"
branch_labels = None
depends_on = None

ADMIN_SCOPES = (
    "app.read",
    "app.write",
    "app.run",
    "app.cancel",
    "app.credentials.read",
    "app.credentials.write",
    "manager.read",
    "manager.runtime",
    "manager.mutate",
    "app.admin",
)


def _columns(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(column["name"]) for column in inspector.get_columns(table)}


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    if "automation_clients" not in tables:
        Base.metadata.tables["automation_clients"].create(bind=connection)
        tables.add("automation_clients")

    if "api_tokens" in tables:
        columns = _columns(inspector, "api_tokens")
        additions: tuple[tuple[str, sa.types.TypeEngine, object | None], ...] = (
            ("subject", sa.String(200), None),
            ("scopes_json", sa.JSON(), json.dumps(list(ADMIN_SCOPES))),
            ("expires_at", sa.DateTime(timezone=True), None),
            ("principal_kind", sa.String(40), "api_token"),
            ("created_by", sa.String(200), None),
            ("client_id", sa.String(36), None),
            ("target_instance_id", sa.String(36), None),
            ("canonical_origin", sa.Text(), None),
        )
        with op.batch_alter_table("api_tokens") as batch:
            for name, type_, default in additions:
                if name in columns:
                    continue
                batch.add_column(
                    sa.Column(
                        name,
                        type_,
                        nullable=name not in {"scopes_json", "principal_kind"},
                        server_default=default,
                    )
                )
            existing_foreign_keys = inspector.get_foreign_keys("api_tokens")
            if not any(
                key.get("constrained_columns") == ["client_id"]
                for key in existing_foreign_keys
            ):
                batch.create_foreign_key(
                    "fk_api_tokens_client_id_automation_clients",
                    "automation_clients",
                    ["client_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        connection.execute(
            sa.text(
                """
                UPDATE api_tokens
                SET scopes_json = COALESCE(scopes_json, :scopes),
                    principal_kind = COALESCE(principal_kind, 'api_token'),
                    subject = COALESCE(subject, 'api-token:' || id),
                    created_by = COALESCE(created_by, 'legacy-migration')
                """
            ),
            {"scopes": json.dumps(list(ADMIN_SCOPES))},
        )
        existing_indexes = {
            str(index["name"])
            for index in sa.inspect(connection).get_indexes("api_tokens")
        }
        for index_name, column in (
            ("ix_api_tokens_subject", "subject"),
            ("ix_api_tokens_expires_at", "expires_at"),
            ("ix_api_tokens_client_id", "client_id"),
        ):
            if index_name not in existing_indexes:
                op.create_index(index_name, "api_tokens", [column])

    # Fresh databases create current metadata in revision 0001. Existing
    # databases need only the new tables here.
    for name in (
        "automation_enrollment_grants",
        "audit_events",
    ):
        if name not in tables:
            Base.metadata.tables[name].create(bind=connection)


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "api_tokens" in tables:
        columns = _columns(sa.inspect(connection), "api_tokens")
        existing_indexes = {
            str(index["name"])
            for index in sa.inspect(connection).get_indexes("api_tokens")
        }
        for index_name in (
            "ix_api_tokens_client_id",
            "ix_api_tokens_expires_at",
            "ix_api_tokens_subject",
        ):
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="api_tokens")
        foreign_keys = sa.inspect(connection).get_foreign_keys("api_tokens")
        with op.batch_alter_table("api_tokens") as batch:
            for foreign_key in foreign_keys:
                if (
                    foreign_key.get("constrained_columns") == ["client_id"]
                    and foreign_key.get("name")
                ):
                    batch.drop_constraint(
                        str(foreign_key["name"]),
                        type_="foreignkey",
                    )
            for name in (
                "canonical_origin",
                "target_instance_id",
                "client_id",
                "created_by",
                "principal_kind",
                "expires_at",
                "scopes_json",
                "subject",
            ):
                if name in columns:
                    batch.drop_column(name)
    for name in (
        "audit_events",
        "automation_enrollment_grants",
        "automation_clients",
    ):
        if name in tables:
            op.drop_table(name)
