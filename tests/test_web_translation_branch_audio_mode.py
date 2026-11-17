"""The audio-mode migration rewrites only branches that never chose a value."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database, sqlite_url, upgrade_database
from pandrator.web.models import (
    Artifact,
    SessionRecord,
    SessionSetting,
    SessionSettingHistory,
    TranslationProjectBranch,
)
from tests.web_test_support import prepare_web_test_data_root

PREVIOUS_REVISION = "0087_public_project_links"


def _alembic_config(database_path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    return config


def _attach_media(services, session_id: str) -> Artifact:
    database, paths, artifacts = (
        services[key] for key in ("database", "paths", "artifacts")
    )
    with database.session() as db:
        record = db.get(SessionRecord, session_id)
        directory = paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "source.mp4"
    path.write_bytes(b"migration media fixture")
    artifact = artifacts.register(
        path,
        kind="video",
        role="upload",
        session_id=session_id,
        metadata={"original_filename": "source.mp4"},
    )
    asset = services["source_library"].ensure_for_artifact(
        artifact.id, display_name="source.mp4", kind="mp4",
    )
    services["source_library"].attach(session_id, asset.id)
    return artifact


def _reviewed_correction(services, session_id: str) -> Artifact:
    from pandrator.web.models import Document, DocumentRevision

    database, paths, artifacts = (
        services[key] for key in ("database", "paths", "artifacts")
    )
    with database.session() as db:
        record = db.get(SessionRecord, session_id)
        directory = paths.sessions / record.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "correction.srt"
    path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8"
    )
    checkpoint = artifacts.register(
        path, kind="srt", role="correction", session_id=session_id,
        metadata={"language": "en"},
    )
    with database.session() as db:
        document = Document(session_id=session_id, stage="correction", language="en")
        db.add(document)
        db.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_number=1,
            content_hash=checkpoint.content_hash,
            reviewed=True,
        )
        db.add(revision)
        db.flush()
        document.active_revision_id = revision.id
        artifact = db.get(Artifact, checkpoint.id)
        artifact.metadata_json = {**artifact.metadata_json, "revision_id": revision.id}
    return checkpoint


def _voiceover_branch(client, headers, services, *, name: str) -> str:
    created = client.post(
        "/api/v1/sessions",
        json={
            "name": name,
            "workflow_kind": "voiceover",
            "source_language": "auto",
            "included_stages": ["transcribe", "correct", "translate", "generate_audio"],
            "multilingual_setup": {
                "target_languages": ["fr"],
                "generate_voiceover": True,
            },
        },
        headers={**headers, "Idempotency-Key": f"create-{name}-key"},
    )
    assert created.status_code == 201, created.get_json()
    session = created.get_json()
    _attach_media(services, session["id"])
    checkpoint = _reviewed_correction(services, session["id"])
    project = client.post(
        f"/api/v1/sessions/{session['id']}/translation-project",
        json={
            "checkpoint_artifact_id": checkpoint.id,
            "expected_revision": session["revision"],
            "create_planned_branches": True,
        },
        headers={**headers, "Idempotency-Key": f"project-{name}-key"},
    )
    assert project.status_code == 200, project.get_json()
    return project.get_json()["project"]["branches"][0]["session_id"]


def test_upgrade_unstamps_only_untouched_translation_branches():
    with tempfile.TemporaryDirectory() as root:
        prepare_web_test_data_root(root)
        tokens = BootstrapTokenStore()
        app = create_app(data_root=root, testing=True, bootstrap_tokens=tokens)
        client = app.test_client()
        csrf = client.post(
            "/api/v1/auth/bootstrap", json={"token": tokens.issue()}
        ).get_json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        services = app.extensions["pandrator"]
        database = services["database"]
        database_path = Path(services["paths"].database)

        untouched = _voiceover_branch(client, headers, services, name="Untouched")
        edited = _voiceover_branch(client, headers, services, name="Edited")
        plain = client.post(
            "/api/v1/sessions",
            json={"name": "Plain voiceover", "workflow_kind": "voiceover"},
            headers=headers,
        ).get_json()

        # Recreate the pre-migration stamp: every branch stored dubbing_only.
        with database.session() as db:
            for session_id in (untouched, edited):
                setting = db.get(SessionSetting, (session_id, "output"))
                setting.value_json = {
                    **setting.value_json,
                    "audio_mode": "dubbing_only",
                }
                setting.revision = 1
                for history in db.scalars(
                    select(SessionSettingHistory).where(
                        SessionSettingHistory.session_id == session_id,
                        SessionSettingHistory.section == "output",
                    )
                ).all():
                    db.delete(history)
            db.add(
                SessionSetting(
                    session_id=plain["id"],
                    section="output",
                    value_json={"audio_mode": "dubbing_only", "export_mode": "media"},
                    revision=1,
                )
            )
            db.flush()
            # This branch's history shows a deliberate choice of mixed.
            db.add(
                SessionSettingHistory(
                    session_id=edited,
                    section="output",
                    value_json={"audio_mode": "mixed", "export_mode": "media"},
                    revision=0,
                )
            )
            untouched_before = db.get(SessionSetting, (untouched, "output"))
            before_revision = untouched_before.revision
            before_updated_at = untouched_before.updated_at
        database.dispose()

        config = _alembic_config(database_path)
        command.downgrade(config, PREVIOUS_REVISION)
        upgrade_database(database_path)
        # A second upgrade must not rewrite anything again.
        upgrade_database(database_path)

        upgraded = Database(database_path)
        try:
            with upgraded.session() as db:
                setting = db.get(SessionSetting, (untouched, "output"))
                assert setting.value_json["audio_mode"] == "mixed"
                assert setting.value_json["export_mode"] == "media"
                assert setting.revision == before_revision + 1
                # The typed timestamp parameters must read back as datetimes,
                # not as raw strings or a driver-dependent representation.
                assert isinstance(setting.updated_at, datetime)
                assert setting.updated_at != before_updated_at
                assert setting.updated_at > before_updated_at
                history = db.scalars(
                    select(SessionSettingHistory).where(
                        SessionSettingHistory.session_id == untouched,
                        SessionSettingHistory.section == "output",
                    )
                ).all()
                assert len(history) == 1
                # The replaced value is recorded at the replaced revision, as
                # update_in_session does.
                assert history[0].value_json["audio_mode"] == "dubbing_only"
                assert history[0].revision == before_revision
                assert isinstance(history[0].created_at, datetime)
                assert history[0].created_at == setting.updated_at

                kept = db.get(SessionSetting, (edited, "output"))
                assert kept.value_json["audio_mode"] == "dubbing_only"
                assert kept.revision == 1
                assert db.get(SessionSetting, (plain["id"], "output")).value_json[
                    "audio_mode"
                ] == "dubbing_only"
                assert len(
                    db.scalars(
                        select(TranslationProjectBranch).where(
                            TranslationProjectBranch.session_id == edited
                        )
                    ).all()
                ) == 1
        finally:
            upgraded.dispose()
