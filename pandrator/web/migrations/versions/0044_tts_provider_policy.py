"""Preserve the effective TTS default when introducing audio.cpp defaults."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0044_tts_provider_policy"
down_revision = "0043_quick_transcriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # Fresh databases have neither an owner nor sessions while migrations run.
    # Existing workspaces must keep their pre-upgrade inherited XTTS selection.
    established = any(
        connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
        for table in ("owner_account", "sessions")
    )
    established = (
        established
        or connection.execute(
            sa.text(
                "SELECT 1 FROM app_settings WHERE key IN ('defaults.tts', 'services.tts') LIMIT 1"
            )
        ).first()
    )
    if not established:
        return
    settings = sa.table(
        "app_settings",
        sa.column("key", sa.String),
        sa.column("value_json", sa.JSON),
        sa.column("revision", sa.Integer),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    existing = (
        connection.execute(sa.select(settings).where(settings.c.key == "defaults.tts"))
        .mappings()
        .first()
    )
    value = dict(existing["value_json"] or {}) if existing else {}
    if value.get("service"):
        return
    now = datetime.now(UTC)
    if existing:
        history = sa.table(
            "app_settings_history",
            sa.column("key", sa.String),
            sa.column("value_json", sa.JSON),
            sa.column("revision", sa.Integer),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        connection.execute(
            history.insert().values(
                key="defaults.tts",
                value_json=existing["value_json"],
                revision=existing["revision"],
                created_at=now,
            )
        )
        connection.execute(
            settings.update()
            .where(settings.c.key == "defaults.tts")
            .values(
                value_json={**value, "service": "XTTS"},
                revision=existing["revision"] + 1,
                updated_at=now,
            )
        )
    else:
        connection.execute(
            settings.insert().values(
                key="defaults.tts",
                value_json={"service": "XTTS"},
                revision=1,
                updated_at=now,
            )
        )


def downgrade() -> None:
    # Keep the explicit selection: removing it could change the user's engine.
    pass
