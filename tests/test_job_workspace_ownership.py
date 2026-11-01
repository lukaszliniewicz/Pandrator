from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from pandrator.web.database import sqlite_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "pandrator" / "web" / "migrations"
REVISION = "0066_job_ownership"
PREVIOUS_REVISION = "0065_upload_ownership"
TIMESTAMP = "2026-10-23 12:34:56"
JOB_COLUMNS = (
    "id",
    "kind",
    "session_id",
    "workflow_run_id",
    "status",
    "payload_json",
    "resource_keys_json",
    "result_json",
    "progress",
    "progress_detail",
    "error_code",
    "error_message",
    "lease_owner",
    "lease_expires_at",
    "lease_generation",
    "available_at",
    "attempts",
    "max_attempts",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
)


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    return config


@pytest.fixture(scope="session")
def schema_at_0065(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("job-ownership-base") / "at-0065.sqlite"
    command.upgrade(_config(path), PREVIOUS_REVISION)
    return path


def _connect(path: Path) -> Engine:
    return sa.create_engine(sqlite_url(path), future=True)


def _legacy_jobs_database(schema_at_0065: Path, path: Path) -> None:
    shutil.copy2(schema_at_0065, path)
    quoted_columns = ", ".join(f'"{name}"' for name in JOB_COLUMNS)
    old_columns = """
        id VARCHAR(36) NOT NULL,
        kind VARCHAR(120) NOT NULL,
        session_id VARCHAR(36),
        workflow_run_id VARCHAR(36),
        status VARCHAR(32) NOT NULL,
        payload_json JSON NOT NULL,
        resource_keys_json JSON DEFAULT '[]' NOT NULL,
        result_json JSON,
        progress FLOAT NOT NULL,
        progress_detail TEXT,
        error_code VARCHAR(120),
        error_message TEXT,
        lease_owner VARCHAR(120),
        lease_expires_at DATETIME,
        lease_generation INTEGER DEFAULT 0 NOT NULL,
        available_at DATETIME,
        attempts INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        started_at DATETIME,
        finished_at DATETIME,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE,
        FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs (id) ON DELETE CASCADE
    """
    indexes = (
        "CREATE INDEX ix_jobs_kind ON jobs (kind)",
        "CREATE INDEX ix_jobs_session_id ON jobs (session_id)",
        "CREATE INDEX ix_jobs_status ON jobs (status)",
        "CREATE INDEX ix_jobs_lease_owner ON jobs (lease_owner)",
        "CREATE INDEX ix_jobs_lease_expires_at ON jobs (lease_expires_at)",
        "CREATE INDEX ix_jobs_available_at ON jobs (available_at)",
        "CREATE INDEX ix_jobs_created_at ON jobs (created_at)",
        "CREATE INDEX idx_jobs_session_kind_created "
        "ON jobs (session_id, kind, created_at DESC, id DESC)",
        "CREATE INDEX idx_jobs_status_created ON jobs (status, created_at, id)",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE jobs_legacy ({old_columns})")
        connection.execute(
            f"INSERT INTO jobs_legacy ({quoted_columns}) SELECT {quoted_columns} FROM jobs"
        )
        connection.execute("DROP TABLE jobs")
        connection.execute("ALTER TABLE jobs_legacy RENAME TO jobs")
        for statement in indexes:
            connection.execute(statement)


def _owner_workspace(connection: Connection) -> str:
    return str(
        connection.execute(
            sa.text(
                "SELECT workspaces.id FROM workspaces JOIN users "
                "ON users.id = workspaces.personal_owner_user_id "
                "WHERE users.legacy_subject = 'owner' AND workspaces.kind = 'personal'"
            )
        ).scalar_one()
    )


def _create_workspace(connection: Connection, workspace_id: str, *, state: str = "active") -> None:
    connection.execute(
        sa.text(
            "INSERT INTO workspaces "
            "(id, kind, name, personal_owner_user_id, state, revision, created_at, updated_at) "
            "VALUES (:id, 'team', :name, NULL, :state, 1, :now, :now)"
        ),
        {"id": workspace_id, "name": workspace_id, "state": state, "now": TIMESTAMP},
    )


def _insert_session(
    connection: Connection,
    session_id: str,
    *,
    project_id: str | None = None,
    trashed: bool = False,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO sessions "
            "(id, project_id, name, storage_key, legacy_name, legacy_path, workflow_kind, "
            "interface_mode, source_language, target_language, workflow_preset, "
            "included_stages_json, status, revision, created_at, updated_at, trashed_at, purge_after) "
            "VALUES (:id, :project_id, :name, :storage_key, NULL, NULL, 'audiobook', "
            "'advanced', 'auto', NULL, 'custom', '[]', :status, 1, :now, :now, :trashed_at, NULL)"
        ),
        {
            "id": session_id,
            "project_id": project_id,
            "name": session_id,
            "storage_key": f"storage-{session_id}",
            "status": "trashed" if trashed else "complete",
            "now": TIMESTAMP,
            "trashed_at": TIMESTAMP if trashed else None,
        },
    )


def _create_project_graph(
    connection: Connection,
    *,
    workspace_id: str,
    project_id: str,
    root_session_id: str,
    sessions: tuple[str, ...] = (),
    trashed_sessions: frozenset[str] = frozenset(),
) -> None:
    _insert_session(
        connection,
        root_session_id,
        trashed=root_session_id in trashed_sessions,
    )
    connection.execute(
        sa.text(
            "INSERT INTO projects "
            "(id, workspace_id, source_session_id, name, created_by_user_id, revision, "
            "created_at, updated_at) VALUES (:id, :workspace_id, :source_session_id, "
            ":name, NULL, 1, :now, :now)"
        ),
        {
            "id": project_id,
            "workspace_id": workspace_id,
            "source_session_id": root_session_id,
            "name": project_id,
            "now": TIMESTAMP,
        },
    )
    connection.execute(
        sa.text("UPDATE sessions SET project_id = :project_id WHERE id = :session_id"),
        {"project_id": project_id, "session_id": root_session_id},
    )
    for session_id in sessions:
        _insert_session(
            connection,
            session_id,
            project_id=project_id,
            trashed=session_id in trashed_sessions,
        )


def _insert_workflow(connection: Connection, workflow_id: str, session_id: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO workflow_runs "
            "(id, session_id, preset, status, settings_json, created_at, updated_at) "
            "VALUES (:id, :session_id, 'custom', 'complete', '{}', :now, :now)"
        ),
        {"id": workflow_id, "session_id": session_id, "now": TIMESTAMP},
    )


def _insert_job(
    connection: Connection,
    job_id: str,
    *,
    kind: str = "generic.test",
    session_id: str | None = None,
    workflow_run_id: str | None = None,
    payload_json: str = "{}",
    result_json: str | None = '{"result":"kept"}',
    status: str = "succeeded",
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO jobs "
            "(id, kind, session_id, workflow_run_id, status, payload_json, resource_keys_json, "
            "result_json, progress, progress_detail, error_code, error_message, lease_owner, "
            "lease_expires_at, lease_generation, available_at, attempts, max_attempts, "
            "created_at, started_at, finished_at, updated_at) "
            "VALUES (:id, :kind, :session_id, :workflow_run_id, :status, :payload_json, "
            "'[\"resource:test\"]', :result_json, 0.25, 'retained detail', 'retained-code', "
            "'retained error', 'worker-test', '2026-10-23 12:45:00', 4, "
            "'2026-10-23 12:35:00', 2, 5, :now, :now, :now, :now)"
        ),
        {
            "id": job_id,
            "kind": kind,
            "session_id": session_id,
            "workflow_run_id": workflow_run_id,
            "status": status,
            "payload_json": payload_json,
            "result_json": result_json,
            "now": TIMESTAMP,
        },
    )


def _create_artifact(
    connection: Connection,
    *,
    artifact_id: str,
    workspace_id: str,
    project_id: str | None = None,
    session_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO artifacts "
            "(id, workspace_id, project_id, session_id, kind, role, relative_path, mime_type, "
            "size_bytes, content_hash, state, settings_hash, metadata_json, created_at, updated_at) "
            "VALUES (:id, :workspace_id, :project_id, :session_id, 'audio', 'upload', :path, "
            "'audio/wav', 7, 'hash', 'ready', NULL, '{}', :now, :now)"
        ),
        {
            "id": artifact_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "session_id": session_id,
            "path": f"artifacts/{artifact_id}.wav",
            "now": TIMESTAMP,
        },
    )


def _insert_voice(connection: Connection, voice_id: str, workspace_id: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO voices "
            "(id, workspace_id, name, language, description, rvc_model_ref, metadata_json, "
            "revision, created_at, updated_at) VALUES (:id, :workspace_id, :name, NULL, NULL, "
            "NULL, '{}', 1, :now, :now)"
        ),
        {"id": voice_id, "workspace_id": workspace_id, "name": voice_id, "now": TIMESTAMP},
    )


def _insert_sample(
    connection: Connection, sample_id: str, workspace_id: str, voice_id: str, artifact_id: str
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO voice_samples "
            "(id, workspace_id, voice_id, artifact_id, transcript, transcript_language, "
            "transcript_reviewed, created_at) VALUES (:id, :workspace_id, :voice_id, "
            ":artifact_id, NULL, NULL, 0, :now)"
        ),
        {
            "id": sample_id,
            "workspace_id": workspace_id,
            "voice_id": voice_id,
            "artifact_id": artifact_id,
            "now": TIMESTAMP,
        },
    )


def _insert_training_run(
    connection: Connection,
    training_id: str,
    workspace_id: str,
    *,
    job_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO training_runs "
            "(id, workspace_id, kind, voice_id, job_id, source_artifact_id, "
            "source_text_artifact_id, output_artifact_id, model_name, status, settings_json, "
            "error_message, created_at, updated_at) VALUES (:id, :workspace_id, 'xtts', NULL, "
            ":job_id, NULL, NULL, NULL, 'xtts-test', 'queued', '{}', NULL, :now, :now)"
        ),
        {
            "id": training_id,
            "workspace_id": workspace_id,
            "job_id": job_id,
            "now": TIMESTAMP,
        },
    )


def _insert_sound_variant(
    connection: Connection,
    candidate_id: str,
    workspace_id: str,
    *,
    job_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO sound_variants "
            "(id, workspace_id, item_id, job_id, artifact_id, state, fingerprint, "
            "provenance_json, sample_rate, channels, frame_count, duration_seconds, created_at) "
            "VALUES (:id, :workspace_id, NULL, :job_id, NULL, 'queued', NULL, '{}', "
            "NULL, NULL, NULL, NULL, :now)"
        ),
        {
            "id": candidate_id,
            "workspace_id": workspace_id,
            "job_id": job_id,
            "now": TIMESTAMP,
        },
    )


def _insert_purge_receipt(connection: Connection, session_id: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO session_purges "
            "(session_id, state, manifest_json, expected_revision, impact_token, error_message, "
            "created_at, updated_at) VALUES (:session_id, 'complete', '[]', 1, 'token', NULL, "
            ":now, :now)"
        ),
        {"session_id": session_id, "now": TIMESTAMP},
    )


def _insert_event(connection: Connection, job_id: str, event_type: str = "job.queued") -> None:
    connection.execute(
        sa.text(
            "INSERT INTO job_events (job_id, event_type, payload_json, created_at) "
            "VALUES (:job_id, :event_type, :payload_json, :now)"
        ),
        {
            "job_id": job_id,
            "event_type": event_type,
            "payload_json": '{"retained":true}',
            "now": TIMESTAMP,
        },
    )


def _insert_resource_claim(connection: Connection, job_id: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO resource_claims "
            "(resource_key, job_id, lease_owner, lease_generation, expires_at, created_at) "
            "VALUES ('resource:test', :job_id, 'worker-test', 4, '2026-10-23 12:45:00', :now)"
        ),
        {"job_id": job_id, "now": TIMESTAMP},
    )


def _job_scope(connection: Connection, job_id: str) -> tuple[str | None, str | None]:
    row = connection.execute(
        sa.text("SELECT workspace_id, project_id FROM jobs WHERE id = :job_id"),
        {"job_id": job_id},
    ).one()
    return row.workspace_id, row.project_id


def _database_snapshot(connection: Connection) -> tuple[Any, ...]:
    objects = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).all()
    tables = (
        connection.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        .scalars()
        .all()
    )
    data = []
    for table_name in tables:
        quoted_table = '"' + str(table_name).replace('"', '""') + '"'
        columns = connection.exec_driver_sql(f"PRAGMA table_info({quoted_table})").all()
        order_by = ", ".join('"' + str(column[1]).replace('"', '""') + '"' for column in columns)
        rows = connection.exec_driver_sql(f"SELECT * FROM {quoted_table} ORDER BY {order_by}").all()
        data.append((table_name, tuple(tuple(row) for row in rows)))
    version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    return tuple(objects), tuple(data), version


def _version(connection: Connection) -> str:
    return str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())


def test_upgrade_backfills_complete_early_metadata_without_rewriting_job_history(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    path = tmp_path / "early-job-metadata.sqlite"
    shutil.copy2(schema_at_0065, path)
    engine = _connect(path)
    payload_json = ' { "workspace_id" : "ignored-decoy", "opaque" : [1, 2] } '
    result_json = ' { "preserve" : "exact bytes" } '
    try:
        with engine.begin() as connection:
            _insert_job(
                connection,
                "job-early-default",
                payload_json=payload_json,
                result_json=result_json,
                status="failed",
            )
            assert {column["name"] for column in sa.inspect(connection).get_columns("jobs")} >= {
                "workspace_id",
                "project_id",
            }
            assert (
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM jobs WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL"
                    )
                ).scalar_one()
                == 0
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            assert _job_scope(connection, "job-early-default") == (
                _owner_workspace(connection),
                None,
            )
            row = connection.execute(
                sa.text(
                    "SELECT payload_json, result_json, status, attempts, lease_generation FROM jobs"
                )
            ).one()
            assert tuple(row) == (payload_json, result_json, "failed", 2, 4)
    finally:
        engine.dispose()


def test_old_0065_job_shape_adopts_session_and_workflow_roots_and_preserves_links(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    path = tmp_path / "session-workflow-roots.sqlite"
    _legacy_jobs_database(schema_at_0065, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-archived-job-test"
            _create_workspace(connection, workspace_id, state="archived")
            project_id = "project-trashed-job-test"
            _create_project_graph(
                connection,
                workspace_id=workspace_id,
                project_id=project_id,
                root_session_id="session-trashed-root-job-test",
                sessions=("session-trashed-job-test",),
                trashed_sessions=frozenset(
                    {"session-trashed-root-job-test", "session-trashed-job-test"}
                ),
            )
            _insert_job(
                connection,
                "job-session-history",
                session_id="session-trashed-job-test",
                status="succeeded",
            )
            _insert_workflow(connection, "workflow-job-history", "session-trashed-job-test")
            _insert_job(
                connection,
                "job-workflow-history",
                workflow_run_id="workflow-job-history",
                status="failed",
            )
            _insert_event(connection, "job-session-history")
            _insert_resource_claim(connection, "job-session-history")
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _job_scope(connection, "job-session-history") == (workspace_id, project_id)
            assert _job_scope(connection, "job-workflow-history") == (workspace_id, project_id)
            assert (
                connection.execute(
                    sa.text("SELECT session_id FROM jobs WHERE id = 'job-workflow-history'")
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM job_events WHERE job_id = 'job-session-history'")
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM resource_claims WHERE job_id = 'job-session-history'"
                    )
                ).scalar_one()
                == 1
            )
            assert _version(connection) == REVISION
    finally:
        engine.dispose()


def test_known_payload_roots_resolve_only_typed_fields_and_keep_project_grants_narrow(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    path = tmp_path / "known-payload-roots.sqlite"
    _legacy_jobs_database(schema_at_0065, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            workspace_id = "workspace-known-roots"
            _create_workspace(connection, workspace_id, state="archived")
            _create_project_graph(
                connection,
                workspace_id=workspace_id,
                project_id="project-preview-root",
                root_session_id="session-preview-root",
                sessions=("session-purge-root",),
                trashed_sessions=frozenset({"session-purge-root"}),
            )
            _create_project_graph(
                connection,
                workspace_id=workspace_id,
                project_id="project-other-root",
                root_session_id="session-other-root",
            )
            _create_artifact(
                connection,
                artifact_id="artifact-preview-root",
                workspace_id=workspace_id,
                project_id="project-preview-root",
            )
            _create_artifact(
                connection,
                artifact_id="artifact-other-root",
                workspace_id=workspace_id,
                project_id="project-other-root",
            )
            _create_artifact(
                connection,
                artifact_id="artifact-sample-root",
                workspace_id=workspace_id,
            )
            _insert_voice(connection, "voice-known-root", workspace_id)
            _insert_sample(
                connection,
                "sample-known-root",
                workspace_id,
                "voice-known-root",
                "artifact-sample-root",
            )
            _insert_training_run(connection, "training-known-root", workspace_id)
            _insert_sound_variant(connection, "candidate-known-root", workspace_id)

            for kind in ("audio.preview", "audio.waveform", "video.preview", "rvc.convert"):
                job_id = f"job-{kind.replace('.', '-')}"
                _insert_job(
                    connection,
                    job_id,
                    kind=kind,
                    payload_json='{"source_artifact_id":"artifact-preview-root"}',
                )

            workspace_only_payloads = {
                "session.bundle.import": {"source_artifact_id": "artifact-preview-root"},
                "rvc.model.upload": {
                    "pth_artifact_id": "artifact-preview-root",
                    "index_artifact_id": "artifact-other-root",
                },
                "voice.normalize_recording": {
                    "voice_id": "voice-known-root",
                    "sample_id": "sample-known-root",
                    "source_artifact_id": "artifact-other-root",
                },
                "voice.publish": {
                    "voice_id": "voice-known-root",
                    "sample_id": "sample-known-root",
                    "source_artifact_id": "artifact-other-root",
                },
                "voice.unpublish": {
                    "voice_id": "voice-known-root",
                    "source_artifact_id": "artifact-other-root",
                },
                "voice.transcribe": {
                    "voice_id": "voice-known-root",
                    "sample_id": "sample-known-root",
                    "source_artifact_id": "artifact-other-root",
                },
                "voice.google_design": {
                    "voice_id": "voice-known-root",
                    "source_artifact_id": "artifact-other-root",
                },
                "training.xtts": {"training_id": "training-known-root"},
                "sound.generate": {"candidate_id": "candidate-known-root"},
            }
            for kind, payload in workspace_only_payloads.items():
                _insert_job(
                    connection,
                    f"job-{kind.replace('.', '-')}",
                    kind=kind,
                    payload_json=json.dumps(payload, separators=(",", ":")),
                )
            for kind in ("maintenance.session_purge_preview", "maintenance.session_purge"):
                job_id = f"job-{kind.replace('.', '-')}"
                _insert_job(
                    connection,
                    job_id,
                    kind=kind,
                    payload_json='{"target_session_id":"session-purge-root"}',
                )
            _insert_purge_receipt(connection, "session-purge-root")

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            for kind in ("audio.preview", "audio.waveform", "video.preview", "rvc.convert"):
                job_id = f"job-{kind.replace('.', '-')}"
                assert _job_scope(connection, job_id) == (
                    workspace_id,
                    "project-preview-root",
                )
            for kind in workspace_only_payloads:
                job_id = f"job-{kind.replace('.', '-')}"
                assert _job_scope(connection, job_id) == (workspace_id, None)
            for kind in ("maintenance.session_purge_preview", "maintenance.session_purge"):
                job_id = f"job-{kind.replace('.', '-')}"
                assert _job_scope(connection, job_id) == (workspace_id, "project-preview-root")
    finally:
        engine.dispose()


def test_unbound_training_and_sound_sql_links_supply_workspace_and_must_agree(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    path = tmp_path / "domain-job-links.sqlite"
    _legacy_jobs_database(schema_at_0065, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _create_workspace(connection, "workspace-domain-link")
            _create_workspace(connection, "workspace-domain-conflict")
            _insert_job(connection, "job-domain-link")
            _insert_training_run(
                connection,
                "training-linked-job",
                "workspace-domain-link",
                job_id="job-domain-link",
            )
            _insert_sound_variant(
                connection,
                "candidate-linked-job-a",
                "workspace-domain-link",
                job_id="job-domain-link",
            )
            _insert_sound_variant(
                connection,
                "candidate-linked-job-b",
                "workspace-domain-link",
                job_id="job-domain-link",
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _job_scope(connection, "job-domain-link") == (
                "workspace-domain-link",
                None,
            )
    finally:
        engine.dispose()

    conflict_path = tmp_path / "domain-job-link-conflict.sqlite"
    _legacy_jobs_database(schema_at_0065, conflict_path)
    conflict_engine = _connect(conflict_path)
    try:
        with conflict_engine.begin() as connection:
            _create_workspace(connection, "workspace-domain-link")
            _create_workspace(connection, "workspace-domain-conflict")
            _insert_job(connection, "job-domain-link-conflict")
            _insert_training_run(
                connection,
                "training-domain-link-conflict",
                "workspace-domain-link",
                job_id="job-domain-link-conflict",
            )
            _insert_sound_variant(
                connection,
                "candidate-domain-link-conflict",
                "workspace-domain-conflict",
                job_id="job-domain-link-conflict",
            )
        with conflict_engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError, match="conflict across workspaces"):
            command.upgrade(_config(conflict_path), REVISION)
        with conflict_engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        conflict_engine.dispose()


def test_known_payload_reference_uses_exact_opaque_id_and_missing_or_unknown_roots_fall_back(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    path = tmp_path / "opaque-and-orphan-roots.sqlite"
    _legacy_jobs_database(schema_at_0065, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _create_workspace(connection, "workspace-opaque-id")
            _create_project_graph(
                connection,
                workspace_id="workspace-opaque-id",
                project_id="project-opaque-id",
                root_session_id="session-opaque-id",
            )
            _create_artifact(
                connection,
                artifact_id=" artifact opaque ",
                workspace_id="workspace-opaque-id",
                project_id="project-opaque-id",
            )
            fallback = _owner_workspace(connection)
            _insert_job(
                connection,
                "job-opaque-id",
                kind="audio.preview",
                payload_json='{"source_artifact_id":" artifact opaque "}',
            )
            _insert_job(
                connection,
                "job-missing-known-reference",
                kind="audio.preview",
                payload_json='{"source_artifact_id":"deleted-artifact"}',
            )
            _insert_job(
                connection,
                "job-unknown-kind",
                kind="custom.unknown",
                payload_json=(
                    '{"source_artifact_id":" artifact opaque ",'
                    '"workspace_id":"workspace-opaque-id",'
                    '"project_id":"project-opaque-id"}'
                ),
            )
            _insert_job(
                connection,
                "job-non-object-payload",
                kind="audio.preview",
                payload_json=' [ "artifact opaque" ] ',
            )
            _insert_job(
                connection,
                "job-deleted-purge-target",
                kind="maintenance.session_purge",
                payload_json='{"target_session_id":"already-purged-session"}',
            )
            _insert_job(
                connection,
                "job-null-input",
                kind="voice.normalize_recording",
                payload_json='{"voice_id":null,"source_artifact_id":17,"sample_id":" "}',
            )
            _insert_job(
                connection,
                "job-empty-kind",
                kind="",
                payload_json='{"source_artifact_id":" artifact opaque "}',
            )
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _job_scope(connection, "job-opaque-id") == (
                "workspace-opaque-id",
                "project-opaque-id",
            )
            for job_id in (
                "job-missing-known-reference",
                "job-unknown-kind",
                "job-non-object-payload",
                "job-deleted-purge-target",
                "job-null-input",
                "job-empty-kind",
            ):
                assert _job_scope(connection, job_id) == (fallback, None)
            raw = connection.execute(
                sa.text("SELECT payload_json FROM jobs WHERE id = 'job-non-object-payload'")
            ).scalar_one()
            assert raw == ' [ "artifact opaque" ] '
    finally:
        engine.dispose()


@pytest.mark.parametrize("conflict", ["session_workflow", "workspace_evidence"])
def test_conflicting_sql_or_payload_roots_fail_before_ddl(
    schema_at_0065: Path, tmp_path: Path, conflict: str
) -> None:
    path = tmp_path / f"conflicting-job-roots-{conflict}.sqlite"
    _legacy_jobs_database(schema_at_0065, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            _create_workspace(connection, "workspace-conflicts")
            _create_workspace(connection, "workspace-payload-conflict")
            _create_project_graph(
                connection,
                workspace_id="workspace-conflicts",
                project_id="project-conflicts-a",
                root_session_id="session-conflicts-a-root",
                sessions=("session-conflicts-a",),
            )
            _create_project_graph(
                connection,
                workspace_id="workspace-conflicts",
                project_id="project-conflicts-b",
                root_session_id="session-conflicts-b-root",
                sessions=("session-conflicts-b",),
            )
            if conflict == "session_workflow":
                _insert_workflow(connection, "workflow-conflicts", "session-conflicts-b")
                _insert_job(
                    connection,
                    "job-conflict",
                    session_id="session-conflicts-a",
                    workflow_run_id="workflow-conflicts",
                )
            else:
                _create_artifact(
                    connection,
                    artifact_id="artifact-workspace-conflict",
                    workspace_id="workspace-payload-conflict",
                )
                _insert_job(
                    connection,
                    "job-conflict",
                    kind="voice.publish",
                    session_id="session-conflicts-a",
                    payload_json='{"source_artifact_id":"artifact-workspace-conflict"}',
                )
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_session_root_requires_valid_project_source_and_translation_agreement_before_ddl(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    for bad_graph in ("missing_source", "wrong_source", "translation_conflict"):
        path = tmp_path / f"job-invalid-root-{bad_graph}.sqlite"
        _legacy_jobs_database(schema_at_0065, path)
        engine = _connect(path)
        try:
            with engine.begin() as connection:
                _create_workspace(connection, "workspace-invalid-job-root")
                project_id = "project-invalid-job-root"
                _create_project_graph(
                    connection,
                    workspace_id="workspace-invalid-job-root",
                    project_id=project_id,
                    root_session_id="session-invalid-job-root",
                    sessions=("session-invalid-job-child",),
                )
                _insert_job(
                    connection,
                    "job-invalid-root",
                    session_id="session-invalid-job-child",
                )
                if bad_graph == "missing_source":
                    connection.execute(
                        sa.text("UPDATE projects SET source_session_id = NULL WHERE id = :id"),
                        {"id": project_id},
                    )
                elif bad_graph == "wrong_source":
                    connection.execute(
                        sa.text("UPDATE sessions SET project_id = NULL WHERE id = :session_id"),
                        {"session_id": "session-invalid-job-root"},
                    )
                else:
                    _create_project_graph(
                        connection,
                        workspace_id="workspace-invalid-job-root",
                        project_id="project-invalid-translation-root",
                        root_session_id="session-invalid-translation-root",
                    )
                    _create_artifact(
                        connection,
                        artifact_id="artifact-translation-checkpoint",
                        workspace_id="workspace-invalid-job-root",
                    )
                    connection.execute(
                        sa.text(
                            "INSERT INTO translation_projects "
                            "(id, project_root_id, name, source_session_id, checkpoint_artifact_id, "
                            "source_content_hash, source_language, source_media_edit_revision_id, "
                            "source_media_edit_content_hash, revision, created_at, updated_at) "
                            "VALUES ('translation-invalid-root', :wrong_project, 'translation', "
                            ":source_session_id, 'artifact-translation-checkpoint', 'hash', 'en', "
                            "NULL, NULL, 1, :now, :now)"
                        ),
                        {
                            "wrong_project": "project-invalid-translation-root",
                            "source_session_id": "session-invalid-job-root",
                            "now": TIMESTAMP,
                        },
                    )
            with engine.connect() as connection:
                before = _database_snapshot(connection)
            with pytest.raises(RuntimeError):
                command.upgrade(_config(path), REVISION)
            with engine.connect() as connection:
                assert _database_snapshot(connection) == before
        finally:
            engine.dispose()


@pytest.mark.parametrize("malformation", ["partial", "wrong_type", "populated"])
def test_partial_malformed_or_populated_early_target_fails_atomically(
    schema_at_0065: Path, tmp_path: Path, malformation: str
) -> None:
    path = tmp_path / f"job-bad-target-{malformation}.sqlite"
    if malformation == "partial":
        shutil.copy2(schema_at_0065, path)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP INDEX ix_jobs_workspace_id")
            connection.execute("DROP INDEX ix_jobs_project_id")
            connection.execute("ALTER TABLE jobs RENAME COLUMN project_id TO partial_project_id")
    elif malformation == "wrong_type":
        _legacy_jobs_database(schema_at_0065, path)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN workspace_id VARCHAR(20) "
                "REFERENCES workspaces(id) ON DELETE RESTRICT"
            )
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN project_id VARCHAR(36) "
                "REFERENCES projects(id) ON DELETE RESTRICT"
            )
            connection.execute("CREATE INDEX ix_jobs_workspace_id ON jobs (workspace_id)")
            connection.execute("CREATE INDEX ix_jobs_project_id ON jobs (project_id)")
    else:
        shutil.copy2(schema_at_0065, path)
        populated_engine = _connect(path)
        try:
            with populated_engine.begin() as connection:
                _insert_job(connection, "job-populated-target")
                connection.execute(
                    sa.text("UPDATE jobs SET workspace_id = :workspace_id WHERE id = :job_id"),
                    {
                        "workspace_id": _owner_workspace(connection),
                        "job_id": "job-populated-target",
                    },
                )
        finally:
            populated_engine.dispose()

    engine = _connect(path)
    try:
        with engine.connect() as connection:
            before = _database_snapshot(connection)
        with pytest.raises(RuntimeError):
            command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
    finally:
        engine.dispose()


def test_late_failure_restores_full_schema_data_and_version_then_retry_repeat_and_downgrade(
    schema_at_0065: Path, tmp_path: Path
) -> None:
    path = tmp_path / "job-late-rollback.sqlite"
    _legacy_jobs_database(schema_at_0065, path)
    engine = _connect(path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX ix_custom_jobs_error_code ON jobs (error_code)"
            )
            workspace_id = "workspace-late-rollback"
            _create_workspace(connection, workspace_id)
            _create_project_graph(
                connection,
                workspace_id=workspace_id,
                project_id="project-late-rollback",
                root_session_id="session-late-rollback",
                sessions=("session-late-rollback-child",),
            )
            _insert_job(
                connection,
                "job-late-bound",
                session_id="session-late-rollback-child",
                status="running",
            )
            _insert_job(connection, "job-late-fallback", status="failed")
            _insert_training_run(
                connection,
                "training-late-linked",
                workspace_id,
                job_id="job-late-bound",
            )
            _insert_sound_variant(
                connection,
                "candidate-late-linked",
                workspace_id,
                job_id="job-late-bound",
            )
            _insert_event(connection, "job-late-bound")
            _insert_resource_claim(connection, "job-late-bound")
        with engine.connect() as connection:
            before = _database_snapshot(connection)
            old_columns = [column["name"] for column in sa.inspect(connection).get_columns("jobs")]
        old_job_indexes = {
            name: sql
            for object_type, name, table_name, sql in before[0]
            if object_type == "index" and table_name == "jobs"
        }

        updated_jobs = 0

        def fail_after_first_job_backfill(
            connection: Connection,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            nonlocal updated_jobs
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("update jobs set workspace_id"):
                updated_jobs += 1
                if updated_jobs == 2:
                    raise RuntimeError("injected late job backfill failure")

        event.listen(Engine, "before_cursor_execute", fail_after_first_job_backfill)
        try:
            with pytest.raises(RuntimeError, match="injected late job backfill failure"):
                command.upgrade(_config(path), REVISION)
        finally:
            event.remove(Engine, "before_cursor_execute", fail_after_first_job_backfill)

        with engine.connect() as connection:
            assert _database_snapshot(connection) == before
            assert not connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
            ).first()

        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _version(connection) == REVISION
            assert _job_scope(connection, "job-late-bound") == (
                workspace_id,
                "project-late-rollback",
            )
            assert _job_scope(connection, "job-late-fallback") == (
                _owner_workspace(connection),
                None,
            )
            upgraded = _database_snapshot(connection)
            upgraded_job_indexes = {
                name: sql
                for object_type, name, table_name, sql in upgraded[0]
                if object_type == "index" and table_name == "jobs"
            }
            assert {name: upgraded_job_indexes[name] for name in old_job_indexes} == old_job_indexes
        command.upgrade(_config(path), REVISION)
        with engine.connect() as connection:
            assert _database_snapshot(connection) == upgraded

        command.downgrade(_config(path), PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert _version(connection) == PREVIOUS_REVISION
            assert [
                column["name"] for column in sa.inspect(connection).get_columns("jobs")
            ] == old_columns
            after_downgrade = _database_snapshot(connection)
            assert after_downgrade[1:] == before[1:]
            assert tuple(obj for obj in after_downgrade[0] if obj[2] != "jobs") == tuple(
                obj for obj in before[0] if obj[2] != "jobs"
            )
            assert tuple(
                obj for obj in after_downgrade[0] if obj[2] == "jobs" and obj[0] == "index"
            ) == tuple(obj for obj in before[0] if obj[2] == "jobs" and obj[0] == "index")
            assert not {
                foreign_key["name"]
                for foreign_key in sa.inspect(connection).get_foreign_keys("jobs")
            } & {"fk_jobs_workspace_id_workspaces", "fk_jobs_project_id_projects"}
            assert not {index["name"] for index in sa.inspect(connection).get_indexes("jobs")} & {
                "ix_jobs_workspace_id",
                "ix_jobs_project_id",
            }
            assert "ix_custom_jobs_error_code" in {
                index["name"] for index in sa.inspect(connection).get_indexes("jobs")
            }
    finally:
        engine.dispose()
