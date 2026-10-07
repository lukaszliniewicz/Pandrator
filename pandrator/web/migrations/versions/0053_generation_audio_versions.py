"""Pinned before/after take manifests for standalone recast runs."""

import sqlalchemy as sa
from alembic import op

revision = "0053_generation_audio_versions"
down_revision = "0052_translation_project_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("generation_runs")}
    if "audio_version_json" not in columns:
        op.add_column("generation_runs", sa.Column("audio_version_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("generation_runs", "audio_version_json")
