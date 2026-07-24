"""Store provider voice overrides and normalize inherited segment language."""

import json
from alembic import op
import sqlalchemy as sa


revision = "0015_generation_segment_voice"
down_revision = "0014_usage_event_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("generation_segments", "generation_segment_revisions"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "voice" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("voice", sa.String(length=255), nullable=True))

    revisions = sa.table(
        "generation_plan_revisions",
        sa.column("id", sa.String(length=36)),
        sa.column("settings_json", sa.JSON()),
    )
    segments = sa.table(
        "generation_segments",
        sa.column("id", sa.String(length=36)),
        sa.column("plan_revision_id", sa.String(length=36)),
        sa.column("language", sa.String(length=40)),
    )
    inherited_by_revision: dict[str, str] = {}
    for revision_id, raw_settings in connection.execute(
        sa.select(revisions.c.id, revisions.c.settings_json)
    ):
        settings = raw_settings
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except json.JSONDecodeError:
                settings = {}
        if not isinstance(settings, dict):
            continue
        inherited = str(settings.get("language") or settings.get("target_language") or "").strip()
        if inherited:
            inherited_by_revision[str(revision_id)] = inherited.casefold()

    redundant_ids = [
        str(segment_id)
        for segment_id, plan_revision_id, language in connection.execute(
            sa.select(segments.c.id, segments.c.plan_revision_id, segments.c.language)
            .where(segments.c.language.is_not(None))
        )
        if str(language or "").strip().casefold() == inherited_by_revision.get(str(plan_revision_id), "")
    ]
    if redundant_ids:
        connection.execute(
            segments.update()
            .where(segments.c.id.in_(redundant_ids))
            .values(language=None)
        )


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("generation_segment_revisions", "generation_segments"):
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "voice" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("voice")
