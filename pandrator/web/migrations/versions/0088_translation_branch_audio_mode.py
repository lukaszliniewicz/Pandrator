"""Unstamp dubbing-only audio mode on existing translation branches.

Revision ID: 0088_translation_branch_audio_mode
Revises: 0087_public_project_links
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0088_translation_branch_audio_mode"
down_revision = "0087_public_project_links"
branch_labels = None
depends_on = None

_STAMPED_MODE = "dubbing_only"
_UNSTAMPED_MODE = "mixed"
_LOGGER = logging.getLogger(__name__)


def _json_object(value: Any) -> dict[str, Any] | None:
    """Return a detached JSON object without letting malformed legacy JSON abort.

    Raw SQL intentionally bypasses SQLAlchemy's JSON result processor here, so
    older databases with malformed or scalar JSON in these columns are skipped
    rather than blocking an otherwise valid upgrade.
    """

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _required_tables(bind) -> bool:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    required = {
        "translation_project_branches": {"session_id"},
        "session_settings": {"session_id", "section", "value_json", "revision", "updated_at"},
        "session_settings_history": {
            "session_id",
            "section",
            "value_json",
            "revision",
            "created_at",
        },
    }
    if not set(required) <= tables:
        return False
    return all(
        fields <= {str(column["name"]) for column in inspector.get_columns(table)}
        for table, fields in required.items()
    )


def _never_changed(bind, session_id: str) -> bool:
    """True when every recorded output value kept the stamped audio mode."""

    rows = bind.execute(
        sa.text(
            "SELECT value_json FROM session_settings_history "
            "WHERE session_id = :session_id AND section = :section"
        ),
        {"session_id": session_id, "section": "output"},
    ).mappings()
    for row in rows:
        value = _json_object(row["value_json"])
        if value is None or "audio_mode" not in value:
            continue
        if value["audio_mode"] != _STAMPED_MODE:
            return False
    return True


def upgrade() -> None:
    """Rewrite the stamped audio mode a translation branch never chose.

    Branches created before the branch stamp distinguished sources with audio
    recorded ``dubbing_only`` unconditionally, so an export that omits
    ``audio_mode`` would now inherit a value the user never picked. Only rows
    whose whole history kept that stamped value are rewritten; a value the user
    changed is left alone, and sources without audio are clamped back to
    ``dubbing_only`` by the resolver.
    """

    bind = op.get_bind()
    if not _required_tables(bind):
        return

    session_ids = bind.execute(
        sa.text("SELECT session_id FROM translation_project_branches")
    ).scalars().all()
    read_setting = sa.text(
        "SELECT value_json, revision FROM session_settings "
        "WHERE session_id = :session_id AND section = :section"
    )
    update_setting = sa.text(
        "UPDATE session_settings SET value_json = :value_json, revision = :revision, "
        "updated_at = :updated_at "
        "WHERE session_id = :session_id AND section = :section"
    ).bindparams(
        sa.bindparam("value_json", type_=sa.JSON()),
        sa.bindparam("updated_at", type_=sa.DateTime(timezone=True)),
    )
    insert_history = sa.text(
        "INSERT INTO session_settings_history "
        "(session_id, section, value_json, revision, created_at) "
        "VALUES (:session_id, :section, :value_json, :revision, :created_at)"
    ).bindparams(
        sa.bindparam("value_json", type_=sa.JSON()),
        sa.bindparam("created_at", type_=sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)

    for session_id in session_ids:
        row = bind.execute(
            read_setting, {"session_id": session_id, "section": "output"}
        ).mappings().first()
        if row is None:
            continue
        value = _json_object(row["value_json"])
        if value is None or value.get("audio_mode") != _STAMPED_MODE:
            continue
        if not _never_changed(bind, session_id):
            continue
        new_revision = int(row["revision"]) + 1
        updated = {**value, "audio_mode": _UNSTAMPED_MODE}
        _LOGGER.info(
            "Unstamped audio_mode for translation branch session %s (revision %s -> %s).",
            session_id,
            row["revision"],
            new_revision,
        )
        bind.execute(
            update_setting,
            {
                "session_id": session_id,
                "section": "output",
                "value_json": updated,
                "revision": new_revision,
                "updated_at": now,
            },
        )
        # Record the replaced value, as WorkspaceSettingsService.update_in_session
        # does, so the history reads the same as if the change had gone through
        # the service.
        bind.execute(
            insert_history,
            {
                "session_id": session_id,
                "section": "output",
                "value_json": value,
                "revision": row["revision"],
                "created_at": now,
            },
        )


def downgrade() -> None:
    # Nothing to restore: the previous value was a stamped default, not a
    # user choice, and a downgrade cannot tell which branches still use it.
    pass
