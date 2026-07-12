"""Add revisioned web parity workspace resources."""

from alembic import op
import sqlalchemy as sa

from pandrator.web.models import Base

revision = "0003_parity_workspace"
down_revision = "0002_training_runs"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "session_settings",
    "session_settings_history",
    "outcome_plans",
    "outcome_plan_history",
    "source_assets",
    "session_sources",
    "timed_words",
    "generation_plans",
    "generation_plan_revisions",
    "generation_runs",
    "generation_segments",
    "generation_segment_revisions",
    "audio_takes",
    "output_assemblies",
    "resource_claims",
    "upload_sessions",
    "agent_runs",
    "agent_steps",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    segment_columns = {column["name"] for column in inspector.get_columns("segments")}
    if "node_kind" not in segment_columns:
        with op.batch_alter_table("segments") as batch:
            batch.add_column(sa.Column("node_kind", sa.String(length=40), nullable=False, server_default="subtitle_cue"))
    job_columns = {column["name"] for column in inspector.get_columns("jobs")}
    if "resource_keys_json" not in job_columns:
        with op.batch_alter_table("jobs") as batch:
            batch.add_column(sa.Column("resource_keys_json", sa.JSON(), nullable=False, server_default="[]"))
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[name] for name in NEW_TABLES])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name in reversed(NEW_TABLES):
        if name in inspector.get_table_names():
            op.drop_table(name)
    with op.batch_alter_table("segments") as batch:
        batch.drop_column("node_kind")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("resource_keys_json")
