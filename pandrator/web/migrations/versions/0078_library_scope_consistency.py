"""Enforce workspace ownership across voice, sound, and cache records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0078_library_scope_consistency"
down_revision = "0077_media_scope_consistency"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_library_scope_voices_update",
        "voices",
        """CREATE TRIGGER pandrator_library_scope_voices_update
BEFORE UPDATE OF workspace_id ON voices
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'voice workspace is immutable');
END""",
    ),
    (
        "pandrator_library_scope_voice_collections_update",
        "voice_collections",
        """CREATE TRIGGER pandrator_library_scope_voice_collections_update
BEFORE UPDATE OF workspace_id ON voice_collections
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'voice collection workspace is immutable');
END""",
    ),
    (
        "pandrator_library_scope_jobs_update",
        "jobs",
        """CREATE TRIGGER pandrator_library_scope_jobs_update
BEFORE UPDATE OF workspace_id ON jobs
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'job workspace is immutable');
END""",
    ),
    (
        "pandrator_library_scope_voice_samples_insert",
        "voice_samples",
        """CREATE TRIGGER pandrator_library_scope_voice_samples_insert
BEFORE INSERT ON voice_samples
BEGIN
    SELECT RAISE(ABORT, 'voice sample voice must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM voices AS voice
        WHERE voice.id IS NEW.voice_id
          AND voice.workspace_id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'voice sample artifact must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM artifacts AS artifact
        WHERE artifact.id IS NEW.artifact_id
          AND artifact.workspace_id IS NEW.workspace_id
    );
END""",
    ),
    (
        "pandrator_library_scope_voice_samples_update",
        "voice_samples",
        """CREATE TRIGGER pandrator_library_scope_voice_samples_update
BEFORE UPDATE OF workspace_id, voice_id, artifact_id ON voice_samples
BEGIN
    SELECT RAISE(ABORT, 'voice sample workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'voice sample voice is immutable')
    WHERE NEW.voice_id IS NOT OLD.voice_id;

    SELECT RAISE(ABORT, 'voice sample voice must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM voices AS voice
        WHERE voice.id IS NEW.voice_id
          AND voice.workspace_id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'voice sample artifact must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM artifacts AS artifact
        WHERE artifact.id IS NEW.artifact_id
          AND artifact.workspace_id IS NEW.workspace_id
    );
END""",
    ),
    (
        "pandrator_library_scope_training_runs_insert",
        "training_runs",
        """CREATE TRIGGER pandrator_library_scope_training_runs_insert
BEFORE INSERT ON training_runs
BEGIN
    SELECT RAISE(ABORT, 'training voice must share its workspace')
    WHERE NEW.voice_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM voices AS voice
          WHERE voice.id IS NEW.voice_id
            AND voice.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training job must share its workspace')
    WHERE NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS job
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training source artifact must share its workspace')
    WHERE NEW.source_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.source_artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training text artifact must share its workspace')
    WHERE NEW.source_text_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.source_text_artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training output artifact must share its workspace')
    WHERE NEW.output_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.output_artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_library_scope_training_runs_update",
        "training_runs",
        """CREATE TRIGGER pandrator_library_scope_training_runs_update
BEFORE UPDATE OF workspace_id, voice_id, job_id, source_artifact_id,
    source_text_artifact_id, output_artifact_id ON training_runs
BEGIN
    SELECT RAISE(ABORT, 'training workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'training voice must share its workspace')
    WHERE NEW.voice_id IS NOT OLD.voice_id
      AND NEW.voice_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM voices AS voice
          WHERE voice.id IS NEW.voice_id
            AND voice.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training job must share its workspace')
    WHERE NEW.job_id IS NOT OLD.job_id
      AND NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS job
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training source artifact must share its workspace')
    WHERE NEW.source_artifact_id IS NOT OLD.source_artifact_id
      AND NEW.source_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.source_artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training text artifact must share its workspace')
    WHERE NEW.source_text_artifact_id IS NOT OLD.source_text_artifact_id
      AND NEW.source_text_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.source_text_artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'training output artifact must share its workspace')
    WHERE NEW.output_artifact_id IS NOT OLD.output_artifact_id
      AND NEW.output_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.output_artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_library_scope_voice_collection_members_insert",
        "voice_collection_members",
        """CREATE TRIGGER pandrator_library_scope_voice_collection_members_insert
BEFORE INSERT ON voice_collection_members
BEGIN
    SELECT RAISE(ABORT, 'voice collection must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM voice_collections AS collection
        WHERE collection.id IS NEW.collection_id
          AND collection.workspace_id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'managed voice must share its workspace')
    WHERE NEW.managed_voice_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM voices AS voice
          WHERE voice.id IS NEW.managed_voice_id
            AND voice.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_library_scope_voice_collection_members_update",
        "voice_collection_members",
        """CREATE TRIGGER pandrator_library_scope_voice_collection_members_update
BEFORE UPDATE ON voice_collection_members
BEGIN
    SELECT RAISE(ABORT, 'voice collection member workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'voice collection must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM voice_collections AS collection
        WHERE collection.id IS NEW.collection_id
          AND collection.workspace_id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'managed voice must share its workspace')
    WHERE NEW.managed_voice_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM voices AS voice
          WHERE voice.id IS NEW.managed_voice_id
            AND voice.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_library_scope_sound_items_insert",
        "sound_items",
        """CREATE TRIGGER pandrator_library_scope_sound_items_insert
BEFORE INSERT ON sound_items
WHEN NEW.preferred_variant_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'preferred sound variant must belong to its item')
    WHERE NOT EXISTS (
        SELECT 1 FROM sound_variants AS variant
        WHERE variant.id IS NEW.preferred_variant_id
          AND variant.workspace_id IS NEW.workspace_id
          AND variant.item_id IS NEW.id
    );
END""",
    ),
    (
        "pandrator_library_scope_sound_items_update",
        "sound_items",
        """CREATE TRIGGER pandrator_library_scope_sound_items_update
BEFORE UPDATE OF workspace_id, preferred_variant_id ON sound_items
BEGIN
    SELECT RAISE(ABORT, 'sound item workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'preferred sound variant must belong to its item')
    WHERE NEW.preferred_variant_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sound_variants AS variant
          WHERE variant.id IS NEW.preferred_variant_id
            AND variant.workspace_id IS NEW.workspace_id
            AND variant.item_id IS NEW.id
      );
END""",
    ),
    (
        "pandrator_library_scope_sound_variants_insert",
        "sound_variants",
        """CREATE TRIGGER pandrator_library_scope_sound_variants_insert
BEFORE INSERT ON sound_variants
BEGIN
    SELECT RAISE(ABORT, 'sound variant item must share its workspace')
    WHERE NEW.item_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sound_items AS item
          WHERE item.id IS NEW.item_id
            AND item.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'sound variant job must share its workspace')
    WHERE NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS job
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'sound variant artifact must share its workspace')
    WHERE NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_library_scope_sound_variants_update",
        "sound_variants",
        """CREATE TRIGGER pandrator_library_scope_sound_variants_update
BEFORE UPDATE OF workspace_id, item_id, job_id, artifact_id ON sound_variants
BEGIN
    SELECT RAISE(ABORT, 'sound variant workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'sound variant item attachment is immutable')
    WHERE OLD.item_id IS NOT NULL
      AND NEW.item_id IS NOT OLD.item_id;

    SELECT RAISE(ABORT, 'sound variant item must share its workspace')
    WHERE NEW.item_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sound_items AS item
          WHERE item.id IS NEW.item_id
            AND item.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'sound variant job must share its workspace')
    WHERE NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS job
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'sound variant artifact must share its workspace')
    WHERE NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
      );
END""",
    ),
    (
        "pandrator_library_scope_research_cache_entries_insert",
        "research_cache_entries",
        """CREATE TRIGGER pandrator_library_scope_research_cache_entries_insert
BEFORE INSERT ON research_cache_entries
WHEN NEW.project_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'research cache project must share its workspace')
    WHERE NOT EXISTS (
        SELECT 1 FROM projects AS project
        WHERE project.id IS NEW.project_id
          AND project.workspace_id IS NEW.workspace_id
    );
END""",
    ),
    (
        "pandrator_library_scope_research_cache_entries_update",
        "research_cache_entries",
        """CREATE TRIGGER pandrator_library_scope_research_cache_entries_update
BEFORE UPDATE OF workspace_id, project_id ON research_cache_entries
BEGIN
    SELECT RAISE(ABORT, 'research cache workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'research cache project is immutable')
    WHERE NEW.project_id IS NOT OLD.project_id;
END""",
    ),
)


_REQUIRED_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workspaces", ("id",)),
    ("projects", ("id", "workspace_id")),
    ("artifacts", ("id", "workspace_id")),
    ("voices", ("id", "workspace_id")),
    ("voice_samples", ("id", "workspace_id", "voice_id", "artifact_id")),
    (
        "training_runs",
        (
            "id",
            "workspace_id",
            "voice_id",
            "job_id",
            "source_artifact_id",
            "source_text_artifact_id",
            "output_artifact_id",
        ),
    ),
    ("voice_collections", ("id", "workspace_id")),
    (
        "voice_collection_members",
        ("workspace_id", "collection_id", "voice_key", "managed_voice_id"),
    ),
    ("jobs", ("id", "workspace_id")),
    ("sound_items", ("id", "workspace_id", "preferred_variant_id")),
    (
        "sound_variants",
        ("id", "workspace_id", "item_id", "job_id", "artifact_id"),
    ),
    ("research_cache_entries", ("cache_key", "workspace_id", "project_id")),
)

_REQUIRED_FOREIGN_KEYS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("voices", "workspace_id", "workspaces", "RESTRICT", False),
    ("voice_samples", "workspace_id", "workspaces", "RESTRICT", False),
    ("voice_samples", "voice_id", "voices", "CASCADE", False),
    ("voice_samples", "artifact_id", "artifacts", "RESTRICT", False),
    ("training_runs", "workspace_id", "workspaces", "RESTRICT", False),
    ("training_runs", "voice_id", "voices", "SET NULL", True),
    ("training_runs", "job_id", "jobs", "SET NULL", True),
    ("training_runs", "source_artifact_id", "artifacts", "SET NULL", True),
    ("training_runs", "source_text_artifact_id", "artifacts", "SET NULL", True),
    ("training_runs", "output_artifact_id", "artifacts", "SET NULL", True),
    ("voice_collections", "workspace_id", "workspaces", "RESTRICT", False),
    (
        "voice_collection_members",
        "workspace_id",
        "workspaces",
        "RESTRICT",
        False,
    ),
    (
        "voice_collection_members",
        "collection_id",
        "voice_collections",
        "CASCADE",
        False,
    ),
    (
        "voice_collection_members",
        "managed_voice_id",
        "voices",
        "CASCADE",
        True,
    ),
    ("jobs", "workspace_id", "workspaces", "RESTRICT", False),
    ("sound_items", "workspace_id", "workspaces", "RESTRICT", False),
    ("sound_items", "preferred_variant_id", "sound_variants", "RESTRICT", True),
    ("sound_variants", "workspace_id", "workspaces", "RESTRICT", False),
    ("sound_variants", "item_id", "sound_items", "RESTRICT", True),
    ("sound_variants", "job_id", "jobs", "RESTRICT", True),
    ("sound_variants", "artifact_id", "artifacts", "RESTRICT", True),
    (
        "research_cache_entries",
        "workspace_id",
        "workspaces",
        "RESTRICT",
        False,
    ),
    (
        "research_cache_entries",
        "project_id",
        "projects",
        "CASCADE",
        True,
    ),
)

_PREFLIGHT_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "Cannot enforce library scope consistency; voices have inconsistent workspace roots.",
        """SELECT 1 FROM voices AS voice
LEFT JOIN workspaces AS workspace ON workspace.id = voice.workspace_id
WHERE workspace.id IS NULL LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; voice samples have inconsistent roots.",
        """SELECT 1 FROM voice_samples AS sample
LEFT JOIN workspaces AS workspace ON workspace.id = sample.workspace_id
LEFT JOIN voices AS voice ON voice.id = sample.voice_id
LEFT JOIN artifacts AS artifact ON artifact.id = sample.artifact_id
WHERE workspace.id IS NULL
   OR voice.id IS NULL
   OR artifact.id IS NULL
   OR voice.workspace_id IS NOT sample.workspace_id
   OR artifact.workspace_id IS NOT sample.workspace_id
LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; training runs have inconsistent roots.",
        """SELECT 1 FROM training_runs AS training
LEFT JOIN workspaces AS workspace ON workspace.id = training.workspace_id
LEFT JOIN voices AS voice ON voice.id = training.voice_id
LEFT JOIN jobs AS job ON job.id = training.job_id
LEFT JOIN artifacts AS source ON source.id = training.source_artifact_id
LEFT JOIN artifacts AS source_text ON source_text.id = training.source_text_artifact_id
LEFT JOIN artifacts AS output ON output.id = training.output_artifact_id
WHERE workspace.id IS NULL
   OR (training.voice_id IS NOT NULL AND (
       voice.id IS NULL OR voice.workspace_id IS NOT training.workspace_id
   ))
   OR (training.job_id IS NOT NULL AND (
       job.id IS NULL OR job.workspace_id IS NOT training.workspace_id
   ))
   OR (training.source_artifact_id IS NOT NULL AND (
       source.id IS NULL OR source.workspace_id IS NOT training.workspace_id
   ))
   OR (training.source_text_artifact_id IS NOT NULL AND (
       source_text.id IS NULL OR source_text.workspace_id IS NOT training.workspace_id
   ))
   OR (training.output_artifact_id IS NOT NULL AND (
       output.id IS NULL OR output.workspace_id IS NOT training.workspace_id
   ))
LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; voice collections have inconsistent workspace roots.",
        """SELECT 1 FROM voice_collections AS collection
LEFT JOIN workspaces AS workspace ON workspace.id = collection.workspace_id
WHERE workspace.id IS NULL LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; collection members have inconsistent roots.",
        """SELECT 1 FROM voice_collection_members AS member
LEFT JOIN workspaces AS workspace ON workspace.id = member.workspace_id
LEFT JOIN voice_collections AS collection ON collection.id = member.collection_id
LEFT JOIN voices AS voice ON voice.id = member.managed_voice_id
WHERE workspace.id IS NULL
   OR collection.id IS NULL
   OR collection.workspace_id IS NOT member.workspace_id
   OR (member.managed_voice_id IS NOT NULL AND (
       voice.id IS NULL OR voice.workspace_id IS NOT member.workspace_id
   ))
LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; jobs have inconsistent workspace roots.",
        """SELECT 1 FROM jobs AS job
LEFT JOIN workspaces AS workspace ON workspace.id = job.workspace_id
WHERE workspace.id IS NULL LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; sound items have inconsistent roots.",
        """SELECT 1 FROM sound_items AS item
LEFT JOIN workspaces AS workspace ON workspace.id = item.workspace_id
LEFT JOIN sound_variants AS variant ON variant.id = item.preferred_variant_id
WHERE workspace.id IS NULL
   OR (item.preferred_variant_id IS NOT NULL AND (
       variant.id IS NULL
       OR variant.workspace_id IS NOT item.workspace_id
       OR variant.item_id IS NOT item.id
   ))
LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; sound variants have inconsistent roots.",
        """SELECT 1 FROM sound_variants AS variant
LEFT JOIN workspaces AS workspace ON workspace.id = variant.workspace_id
LEFT JOIN sound_items AS item ON item.id = variant.item_id
LEFT JOIN jobs AS job ON job.id = variant.job_id
LEFT JOIN artifacts AS artifact ON artifact.id = variant.artifact_id
WHERE workspace.id IS NULL
   OR (variant.item_id IS NOT NULL AND (
       item.id IS NULL OR item.workspace_id IS NOT variant.workspace_id
   ))
   OR (variant.job_id IS NOT NULL AND (
       job.id IS NULL OR job.workspace_id IS NOT variant.workspace_id
   ))
   OR (variant.artifact_id IS NOT NULL AND (
       artifact.id IS NULL OR artifact.workspace_id IS NOT variant.workspace_id
   ))
LIMIT 1""",
    ),
    (
        "Cannot enforce library scope consistency; research cache entries have inconsistent roots.",
        """SELECT 1 FROM research_cache_entries AS cache
LEFT JOIN workspaces AS workspace ON workspace.id = cache.workspace_id
LEFT JOIN projects AS project ON project.id = cache.project_id
WHERE workspace.id IS NULL
   OR (cache.project_id IS NOT NULL AND (
       project.id IS NULL OR project.workspace_id IS NOT cache.workspace_id
   ))
LIMIT 1""",
    ),
)

_EXPECTED_PROJECT_TRIGGER_SQL = """CREATE TRIGGER pandrator_project_graph_projects_update
BEFORE UPDATE OF workspace_id, source_session_id ON projects
BEGIN
    SELECT RAISE(ABORT, 'project workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'project source must belong to project')
    WHERE NEW.source_session_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sessions AS source
          WHERE source.id = NEW.source_session_id
            AND source.project_id = NEW.id
      );

    SELECT RAISE(ABORT, 'project source is immutable after attachment')
    WHERE OLD.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NOT OLD.source_session_id;

    SELECT RAISE(ABORT, 'project source cannot be cleared while its session exists')
    WHERE OLD.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NULL
      AND EXISTS (
          SELECT 1 FROM sessions AS source
          WHERE source.id = OLD.source_session_id
      );
END"""

_EXPECTED_ARTIFACT_TRIGGER_SQL = """CREATE TRIGGER pandrator_media_scope_artifacts_update
BEFORE UPDATE OF workspace_id, project_id, session_id ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'artifact project is immutable')
    WHERE NEW.project_id IS NOT OLD.project_id
      AND NOT (
          OLD.project_id IS NOT NULL
          AND NEW.project_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM projects AS project
              WHERE project.id IS OLD.project_id
          )
      );

    SELECT RAISE(ABORT, 'artifact workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'artifact project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'artifact session must belong to its project')
    WHERE NEW.session_id IS NOT NULL
      AND (
          NEW.project_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id = member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END"""

_REQUIRED_PARENT_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_project_graph_projects_update",
        "projects",
        _EXPECTED_PROJECT_TRIGGER_SQL,
    ),
    (
        "pandrator_media_scope_artifacts_update",
        "artifacts",
        _EXPECTED_ARTIFACT_TRIGGER_SQL,
    ),
)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _has_expected_foreign_key(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    parent_table: str,
    ondelete: str,
) -> bool:
    matches = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        if column_name in (foreign_key.get("constrained_columns") or ())
    ]
    if len(matches) != 1:
        return False
    foreign_key = matches[0]
    options = foreign_key.get("options") or {}
    return (
        tuple(foreign_key.get("constrained_columns") or ()) == (column_name,)
        and foreign_key.get("referred_table") == parent_table
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and str(options.get("ondelete") or "").upper() == ondelete
    )


def _validate_schema(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name, required_columns in _REQUIRED_COLUMNS:
        if table_name not in tables:
            raise RuntimeError(
                "Cannot enforce library scope consistency; a required table is missing."
            )
        columns = _columns(bind, table_name)
        if any(column_name not in columns for column_name in required_columns):
            raise RuntimeError(
                "Cannot enforce library scope consistency; a required column is missing."
            )

    for table_name, column_name, parent_table, ondelete, nullable in _REQUIRED_FOREIGN_KEYS:
        column = _columns(bind, table_name)[column_name]
        column_type = column["type"]
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, "length", None) != 36
            or bool(column.get("nullable")) != nullable
            or column.get("default") is not None
            or not _has_expected_foreign_key(bind, table_name, column_name, parent_table, ondelete)
        ):
            raise RuntimeError(
                "Cannot enforce library scope consistency; a library ownership foreign key has an unexpected shape."
            )


def _validate_parent_triggers(bind: sa.Connection) -> None:
    for name, expected_table, expected_sql in _REQUIRED_PARENT_TRIGGERS:
        row = bind.execute(
            sa.text(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = :name COLLATE NOCASE LIMIT 1"
            ),
            {"name": name},
        ).first()
        if (
            row is None
            or str(row[0]) != name
            or str(row[1]) != expected_table
            or str(row[2]) != expected_sql
        ):
            raise RuntimeError(
                "Cannot enforce library scope consistency; a required parent trigger is missing or altered."
            )


def _validate_data(bind: sa.Connection) -> None:
    for message, query in _PREFLIGHT_QUERIES:
        if bind.exec_driver_sql(query).first() is not None:
            raise RuntimeError(message)


def _reject_name_collisions(bind: sa.Connection) -> None:
    for name, _table_name, _sql in _TRIGGERS:
        row = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot enforce library scope consistency; an owned trigger name is already in use."
            )


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Library scope consistency migration is supported only on SQLite.")
    return bind


def _expected_library_trigger_sql(bind: sa.Connection) -> dict[str, tuple[str, str]]:
    names = tuple(name for name, _table_name, _sql in _TRIGGERS)
    parameters = {f"name_{index}": name for index, name in enumerate(names)}
    placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
    rows = bind.execute(
        sa.text(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            f"WHERE type = 'trigger' AND name IN ({placeholders}) ORDER BY name"
        ),
        parameters,
    ).all()
    actual = {str(name): (str(table_name), str(sql)) for name, table_name, sql in rows}
    expected = {name: (table_name, sql) for name, table_name, sql in _TRIGGERS}
    if actual != expected:
        raise RuntimeError(
            "Cannot downgrade library scope consistency; an owned trigger is missing or altered."
        )
    return expected


def upgrade() -> None:
    bind = _sqlite_bind()
    _validate_schema(bind)
    _validate_parent_triggers(bind)
    _validate_data(bind)
    _reject_name_collisions(bind)
    for _name, _table_name, sql in _TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    bind = _sqlite_bind()
    expected = _expected_library_trigger_sql(bind)
    preparer = bind.dialect.identifier_preparer
    for name, _table_name, _sql in _TRIGGERS:
        if name not in expected:
            raise RuntimeError(
                "Cannot downgrade library scope consistency; an owned trigger is missing or altered."
            )
    for name, _table_name, _sql in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {preparer.quote(name)}")
