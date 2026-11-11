"""Typed media admissions, their outputs and retained work keep their SQL scope."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.bundles import SessionBundleService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.job_identity import job_creation_ownership, job_ownership
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    Job,
    Project,
    SessionRecord,
    User,
    Workspace,
    WorkspaceMembership,
)
from pandrator.web.projects.identity import delete_empty_project
from pandrator.web.sessions import SessionService
from pandrator.web.video_previews import VideoInfo
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class MediaJobs:
    app: Flask
    client: FlaskClient
    headers: dict[str, str]
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    queue: JobQueue
    workspaces: dict[str, str]
    sessions: dict[str, SessionRecord]
    handlers: Any

    def source(self, filename: str, *, bound: bool = False, workspace: str = "alpha") -> Artifact:
        path = self.paths.uploads / filename
        path.write_bytes(b"Synthetic media input only.\n")
        return self.artifacts.register(
            path,
            kind="audio",
            role="upload",
            workspace_id=self.workspaces[workspace],
            session_id=self.sessions[workspace].id if bound else None,
        )


@pytest.fixture
def media_jobs(tmp_path: Path) -> Iterator[MediaJobs]:
    paths = prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap, background_maintenance=False
    )
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    services = app.extensions["pandrator"]
    database: Database = services["database"]
    workspaces: dict[str, str] = {}
    with database.immediate_session() as session:
        team_ids: list[str] = []
        for name in ("alpha", "beta"):
            workspace = Workspace(kind="team", name=name)
            session.add(workspace)
            session.flush()
            workspaces[name] = workspace.id
            team_ids.append(workspace.id)
        owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert owner is not None
        workspaces["personal"] = initial_personal_workspace(session).id
        # Typed media reads require current membership in the owning team.
        for workspace_id in team_ids:
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace_id, user_id=owner.id, role="owner"
                )
            )
    sessions = {
        name: SessionService(database).create(name, workspace_id=owner)
        for name, owner in workspaces.items()
    }
    try:
        yield MediaJobs(
            app,
            client,
            {"X-CSRF-Token": csrf},
            database,
            paths,
            ArtifactService(database, paths),
            services["jobs"],
            workspaces,
            sessions,
            services["workflow_handlers"],
        )
    finally:
        database.dispose()


@pytest.mark.parametrize("bound", (False, True))
@pytest.mark.parametrize("kind", ("audio.preview", "audio.waveform", "video.preview"))
def test_media_admission_and_generated_artifact_keep_input_owner(
    media_jobs: MediaJobs, monkeypatch: pytest.MonkeyPatch, kind: str, bound: bool
) -> None:
    harness = media_jobs
    source = harness.source("source.mp4" if kind == "video.preview" else "source.wav", bound=bound)

    def render(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        Path(command[-1]).write_bytes(b"Synthetic rendered media.\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr("pandrator.web.media_process.run_media_process", render)
    monkeypatch.setattr("pandrator.web.video_previews.run_media_process", render)
    monkeypatch.setattr(
        "pandrator.web.video_previews.probe_video",
        lambda *_args, **_kwargs: VideoInfo(1, 1280, 720, 24, False, "h264", "yuv420p", None),
    )
    monkeypatch.setattr(
        "pandrator.web.waveform.generate_waveform_peaks",
        lambda *_args, **_kwargs: SimpleNamespace(
            duration_ms=1000,
            start_ms=0,
            end_ms=1000,
            channels=1,
            points=[0.2],
            analysis_sample_rate_hz=200,
        ),
    )

    foreign = harness.queue.enqueue(
        kind, {"source_artifact_id": source.id}, workspace_id=harness.workspaces["beta"]
    )
    endpoint = {
        "audio.preview": "audio-preview",
        "audio.waveform": "waveform",
        "video.preview": "video-preview",
    }[kind]
    admitted = harness.client.post(
        f"/api/v1/artifacts/{source.id}/{endpoint}", headers=harness.headers
    )
    assert admitted.status_code == 202, admitted.get_json()
    job_id = admitted.get_json()["job_id"]
    assert job_id != foreign.id
    with harness.database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        owner = job_ownership(session, job)
        assert (owner.workspace_id, owner.project_id) == (source.workspace_id, source.project_id)
        payload = dict(job.payload_json)
    handler = {
        "audio.preview": harness.handlers.generate_audio_preview,
        "audio.waveform": harness.handlers.generate_waveform,
        "video.preview": harness.handlers.generate_video_preview,
    }[kind]
    result = handler(payload, lambda *_args: None, threading.Event())
    with harness.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        assert artifact is not None
        assert (artifact.workspace_id, artifact.project_id) == (
            owner.workspace_id,
            owner.project_id,
        )
        retained = session.get(Job, foreign.id)
        assert retained is not None and retained.workspace_id == harness.workspaces["beta"]


def test_bundle_import_admission_and_new_project_use_input_workspace(media_jobs: MediaJobs) -> None:
    harness = media_jobs
    bundle_path = harness.paths.uploads / "source-session.zip"
    SessionBundleService(harness.database, harness.paths).export_bundle(
        harness.sessions["alpha"].id, bundle_path
    )
    source = harness.artifacts.register(
        bundle_path, kind="bundle", role="session_bundle", workspace_id=harness.workspaces["alpha"]
    )
    admitted = harness.client.post(
        "/api/v1/session-bundles/import",
        json={"source_artifact_id": source.id},
        headers=harness.headers,
    )
    assert admitted.status_code == 202, admitted.get_json()
    with harness.database.session() as session:
        job = session.get(Job, admitted.get_json()["id"])
        assert (
            job is not None and job.workspace_id == source.workspace_id and job.project_id is None
        )
        payload = dict(job.payload_json)
    result = harness.handlers.import_session_bundle(payload, lambda *_args: None, threading.Event())
    with harness.database.session() as session:
        imported = session.get(SessionRecord, result["session_id"])
        assert imported is not None
        project = session.get(Project, imported.project_id)
        assert project is not None and project.workspace_id == source.workspace_id
        assert project.id != harness.sessions["alpha"].project_id


def test_rvc_admission_keeps_private_workspace_and_selected_project(media_jobs: MediaJobs) -> None:
    harness = media_jobs
    # RVC model writes remain an original-personal-owner host-local tool.
    weights = harness.source("weights.pth", bound=True, workspace="personal")
    index = harness.source("weights.index", workspace="personal")
    response = harness.client.post(
        "/api/v1/rvc/models",
        json={"pth_artifact_id": weights.id, "index_artifact_id": index.id},
        headers=harness.headers,
    )
    assert response.status_code == 202, response.get_json()
    with harness.database.session() as session:
        job = session.get(Job, response.get_json()["id"])
        assert (
            job is not None and job.workspace_id == weights.workspace_id and job.project_id is None
        )
    other = SessionService(harness.database).create(
        "Selected conversion project", workspace_id=harness.workspaces["personal"]
    )
    response = harness.client.post(
        "/api/v1/rvc/convert",
        json={"source_artifact_id": weights.id, "session_id": other.id},
        headers=harness.headers,
    )
    assert response.status_code == 202, response.get_json()
    with harness.database.session() as session:
        job = session.get(Job, response.get_json()["id"])
        assert job is not None
        assert (job.workspace_id, job.project_id, job.session_id) == (
            weights.workspace_id,
            other.project_id,
            other.id,
        )


def test_sessionless_work_keeps_minimal_project_history_after_session_deletion(
    media_jobs: MediaJobs,
) -> None:
    harness = media_jobs
    record = harness.sessions["alpha"]
    job = harness.queue.enqueue(
        "maintenance.session_purge",
        {"target_session_id": record.id},
        workspace_id=harness.workspaces["alpha"],
        project_id=record.project_id,
    )
    with harness.database.immediate_session() as session:
        target = session.get(SessionRecord, record.id)
        assert target is not None
        session.delete(target)
        delete_empty_project(session, record.project_id)
    with harness.database.session() as session:
        project = session.get(Project, record.project_id)
        retained = session.get(Job, job.id)
        assert project is not None and project.source_session_id is None and retained is not None
        owner = job_ownership(session, retained)
        assert (owner.workspace_id, owner.project_id) == (project.workspace_id, project.id)
        with pytest.raises(ValueError, match="no source session"):
            job_creation_ownership(session, project_id=project.id)
    with harness.database.immediate_session() as session:
        retained = session.get(Job, job.id)
        assert retained is not None
        session.delete(retained)
        delete_empty_project(session, record.project_id)
    with harness.database.session() as session:
        assert session.get(Project, record.project_id) is None
        assert session.scalar(select(Job.id).where(Job.id == job.id)) is None


@pytest.mark.parametrize(
    "corruption",
    ("no_workspace", "no_project", "different_session", "source_missing_with_survivor"),
)
def test_retained_job_checks_reject_inconsistent_roots_without_repair(
    media_jobs: MediaJobs, corruption: str
) -> None:
    harness = media_jobs
    if corruption in {"no_workspace", "source_missing_with_survivor"}:
        # Current constraints reject these writes; keep the legacy reader fence.
        predecessor = (
            "0074_actor_attribution" if corruption == "no_workspace" else "0075_mandatory_ownership"
        )
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
        config.set_main_option("sqlalchemy.url", sqlite_url(harness.database.path))
        command.downgrade(config, predecessor)
        # The downgrade targets the legacy ownership constraint shape only. The
        # current ORM mapper still writes 0084's nullable receipt column, so add
        # that unrelated column back instead of downgrading the whole model.
        with sqlite3.connect(harness.database.path) as connection:
            connection.execute("ALTER TABLE jobs ADD COLUMN external_effect_json JSON")
    elif corruption in {"no_project", "different_session"}:
        # 0075 makes execution roots immutable for writes; this case exercises
        # the reader fence over a retained pre-0075 row instead.
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
        config.set_main_option("sqlalchemy.url", sqlite_url(harness.database.path))
        command.downgrade(config, "0074_actor_attribution")
        with sqlite3.connect(harness.database.path) as connection:
            connection.execute("ALTER TABLE jobs ADD COLUMN external_effect_json JSON")
    job = harness.queue.enqueue("test.retained", session_id=harness.sessions["alpha"].id)
    with harness.database.immediate_session() as session:
        record = session.get(Job, job.id)
        assert record is not None
        if corruption == "no_workspace":
            record.workspace_id = None
        elif corruption == "no_project":
            record.project_id = None
        elif corruption == "different_session":
            record.session_id = harness.sessions["beta"].id
        else:
            record.session_id = None
            project = session.get(Project, record.project_id)
            assert project is not None
            project.source_session_id = None
        expected = (record.workspace_id, record.project_id, record.session_id)
    with harness.database.session() as session:
        record = session.get(Job, job.id)
        assert record is not None
        with pytest.raises(ValueError):
            job_ownership(session, record, allow_archived=True)
        assert (record.workspace_id, record.project_id, record.session_id) == expected


def test_archived_job_ownership_is_available_only_for_explicit_history_use(
    media_jobs: MediaJobs,
) -> None:
    harness = media_jobs
    job = harness.queue.enqueue("test.archived", session_id=harness.sessions["alpha"].id)
    with harness.database.immediate_session() as session:
        workspace = session.get(Workspace, harness.workspaces["alpha"])
        assert workspace is not None
        workspace.state = "archived"
    with harness.database.session() as session:
        record = session.get(Job, job.id)
        assert record is not None
        with pytest.raises(ValueError, match="inactive workspace"):
            job_ownership(session, record)
        retained = job_ownership(session, record, allow_archived=True)
        assert retained.workspace_id == harness.workspaces["alpha"]
        with pytest.raises(ValueError, match="inactive workspace"):
            job_creation_ownership(session, project_id=retained.project_id)
