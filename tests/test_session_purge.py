"""Disposable database and storage checks for session purge."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pandrator.runtime import DataPaths
from pandrator.web.database import Database, upgrade_database
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    Job,
    SessionPurge,
    SessionRecord,
    SessionSource,
    SourceAsset,
    utcnow,
)
from pandrator.web.session_purge import PurgeBlocked, SessionPurgeService
from pandrator.web.sessions import RevisionConflict, SessionService


@pytest.fixture
def harness(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    upgrade_database(paths.database)
    database = Database(paths.database)
    yield database, paths, SessionService(database), SessionPurgeService(database, paths)
    database.dispose()


def _session(harness, name="one"):
    _database, paths, sessions, _purge = harness
    record = sessions.create(name)
    folder = paths.sessions / record.storage_key
    folder.mkdir()
    return record, folder


def _artifact(harness, record, path, artifact_id):
    database, paths, _sessions, _purge = harness
    with database.session() as session:
        session.add(Artifact(id=artifact_id, session_id=record.id, kind="text", role="test",
                             relative_path=path.relative_to(paths.root).as_posix()))


def _trashed(harness, record):
    return harness[2].trash(record.id, record.revision)


def test_purge_removes_owned_file_and_session_only_after_cleanup(harness):
    database, _paths, _sessions, purge = harness
    record, folder = _session(harness)
    file = folder / "owned.txt"
    file.write_text("abc")
    _artifact(harness, record, file, "owned")
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["can_purge"] and preview["owned_file_count"] == 1 and preview["owned_bytes"] == 3
    assert purge.purge(record.id, preview["revision"], preview["impact_token"]) == {"state": "complete"}
    assert not file.exists() and not folder.exists()
    with database.session() as session:
        assert session.get(SessionRecord, record.id) is None
        assert session.get(SessionPurge, record.id).state == "complete"


def test_shared_source_file_and_record_survive(harness):
    database, _paths, _sessions, purge = harness
    record, folder = _session(harness)
    other, _ = _session(harness, "other")
    file = folder / "shared.txt"
    file.write_text("shared")
    _artifact(harness, record, file, "shared-artifact")
    with database.session() as session:
        session.add(SourceAsset(id="source", artifact_id="shared-artifact", display_name="source", kind="text"))
        session.flush()
        session.add(SessionSource(session_id=other.id, source_asset_id="source"))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["retained_shared_count"] == 1 and preview["owned_file_count"] == 0
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert file.read_text() == "shared"
    with database.session() as session:
        assert session.get(SourceAsset, "source").artifact_id == "shared-artifact"
        assert session.get(Artifact, "shared-artifact").session_id is None


def test_legacy_shared_source_path_survives_without_artifact_fk(harness):
    database, _paths, _sessions, purge = harness
    record, folder = _session(harness)
    file = folder / "legacy-source.txt"
    file.write_text("shared")
    with database.session() as session:
        session.add(SourceAsset(id="legacy-source", artifact_id=None,
                                display_name="source", kind="text", external_path=str(file)))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["retained_shared_count"] == 1
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert file.read_text() == "shared"
    with database.session() as session:
        assert session.get(SourceAsset, "legacy-source") is not None


def test_shared_artifact_dot_alias_keeps_canonical_file(harness):
    database, paths, _sessions, purge = harness
    record, folder = _session(harness)
    file = folder / "shared.txt"
    file.write_text("retain")
    with database.session() as session:
        session.add(Artifact(id="dot-artifact", session_id=record.id, kind="text",
                             relative_path=f"sessions/{record.storage_key}/./shared.txt"))
        session.flush()
        session.add(SourceAsset(id="dot-source", artifact_id="dot-artifact", display_name="source", kind="text"))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["retained_shared_count"] == 1 and preview["owned_file_count"] == 0
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert file.read_text() == "retain"
    with database.session() as session:
        assert session.get(Artifact, "dot-artifact").session_id is None
        assert session.get(SourceAsset, "dot-source").artifact_id == "dot-artifact"


def test_external_path_keeps_file_with_owned_dot_alias(harness):
    database, paths, _sessions, purge = harness
    record, folder = _session(harness)
    file = folder / "shared.txt"
    file.write_text("retain")
    with database.session() as session:
        session.add(Artifact(id="dot-artifact", session_id=record.id, kind="text",
                             relative_path=f"sessions/{record.storage_key}/./shared.txt"))
        session.add(SourceAsset(id="external-source", display_name="source", kind="text", external_path=str(file)))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["retained_shared_count"] == 1 and preview["owned_file_count"] == 0
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert file.read_text() == "retain"
    with database.session() as session:
        assert session.get(Artifact, "dot-artifact").session_id is None


def test_cached_caller_cannot_resurrect_partially_purged_session(harness, monkeypatch):
    database, _paths, sessions, purge = harness
    record, folder = _session(harness)
    first = folder / "first.txt"
    second = folder / "z-second.txt"
    first.write_text("first")
    second.write_text("second")
    trashed = _trashed(harness, record)
    preview = purge.preview(record.id)
    with database.session() as stale:
        cached = stale.get(SessionRecord, record.id)
        assert cached.status == "trashed"
        original = purge._remove_managed_entry

        def fail_second(relative, **kwargs):
            if relative.endswith("z-second.txt"):
                raise OSError("simulated second-file failure")
            return original(relative, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(purge, "_remove_managed_entry", fail_second)
            assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "failed"
        assert not first.exists() and second.exists()
        with pytest.raises((RevisionConflict, ValueError)):
            sessions.update_in_session(stale, record.id, trashed.revision, {"status": "idle"})
        stale.rollback()
    with pytest.raises((RevisionConflict, ValueError)):
        sessions.restore(record.id, trashed.revision + 1)
    with database.session() as session:
        current = session.get(SessionRecord, record.id)
        assert current.status == "purging" and current.trashed_at is not None


def test_unfinished_job_and_cross_session_edge_block(harness):
    database, _paths, _sessions, purge = harness
    record, folder = _session(harness)
    other, other_folder = _session(harness, "other")
    first = folder / "first.txt"
    second = other_folder / "second.txt"
    first.write_text("first")
    second.write_text("second")
    _artifact(harness, record, first, "first")
    _artifact(harness, other, second, "second")
    with database.session() as session:
        session.add(Job(kind="test", session_id=record.id, status="queued"))
        session.add(ArtifactEdge(parent_artifact_id="first", child_artifact_id="second"))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert not preview["can_purge"]
    assert any("unfinished:jobs" in item for item in preview["blockers"])
    assert "external_reference:artifact_edges" in preview["blockers"]
    with pytest.raises(PurgeBlocked):
        purge.purge(record.id, preview["revision"], preview["impact_token"])
    assert first.exists()


def test_revision_impact_and_restore_fence(harness):
    _database, _paths, sessions, purge = harness
    record, folder = _session(harness)
    _trashed(harness, record)
    preview = purge.preview(record.id)
    (folder / "new.txt").write_text("changed")
    with pytest.raises(RevisionConflict):
        purge.purge(record.id, preview["revision"], preview["impact_token"])
    current = purge.preview(record.id)
    with pytest.raises(RevisionConflict):
        purge.purge(record.id, current["revision"] + 1, current["impact_token"])
    assert purge.purge(record.id, current["revision"], current["impact_token"])["state"] == "complete"
    with pytest.raises(KeyError):
        sessions.restore(record.id, current["revision"] + 1)


def test_unlink_failure_keeps_manifest_for_idempotent_retry(harness, monkeypatch):
    database, _paths, sessions, purge = harness
    record, folder = _session(harness)
    file = folder / "owned.txt"
    file.write_text("abc")
    _trashed(harness, record)
    preview = purge.preview(record.id)
    real_remove = purge._remove_managed_entry

    def fail_once(relative, *args, **kwargs):
        if relative.endswith("/owned.txt"):
            raise OSError("simulated unlink failure")
        return real_remove(relative, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(purge, "_remove_managed_entry", fail_once)
        assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "failed"
    with database.session() as session:
        journal = session.get(SessionPurge, record.id)
        assert journal.state == "failed" and journal.manifest_json
        assert session.get(SessionRecord, record.id).status == "purging"
    retry_preview = purge.preview(record.id)
    assert (retry_preview["revision"], retry_preview["impact_token"]) == (
        preview["revision"], preview["impact_token"]
    )
    with pytest.raises(ValueError):
        sessions.restore(record.id, preview["revision"] + 1)
    assert purge.sweep()["complete"] == 1  # disabled policy still finishes a manual claim
    assert not file.exists()


def test_symlink_and_escape_block_without_unlink(harness, tmp_path):
    _database, _paths, _sessions, purge = harness
    record, folder = _session(harness)
    external = tmp_path.parent / (tmp_path.name + "-keep.txt")
    external.write_text("keep")
    (folder / "escape.txt").symlink_to(external)
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert not preview["can_purge"]
    assert any("unsafe_session_storage" in item for item in preview["blockers"])
    assert external.read_text() == "keep"
    external.unlink()


def test_policy_only_schedules_future_trash_and_disable_stops_sweep(harness):
    database, _paths, sessions, purge = harness
    old, _ = _session(harness)
    _trashed(harness, old)
    assert purge.preview(old.id)["scheduled_delete_at"] is None
    assert purge.update_policy(0, 1) == {"days": 1, "revision": 1}
    newer, _ = _session(harness, "newer")
    trashed = _trashed(harness, newer)
    assert trashed.purge_after is not None
    with database.immediate_session() as session:
        session.get(SessionRecord, newer.id).purge_after = utcnow() - timedelta(days=1)
    assert purge.update_policy(1, None) == {"days": None, "revision": 2}
    assert purge.sweep()["attempted"] == 0
    with database.session() as session:
        assert session.get(SessionRecord, newer.id) is not None
    assert purge.update_policy(2, 1)["revision"] == 3
    assert purge.sweep()["complete"] == 1
    with database.session() as session:
        assert session.get(SessionRecord, old.id) is not None


def test_retention_cutoff_is_inclusive_and_does_not_delete_early(harness):
    database, _paths, sessions, purge = harness
    purge.update_policy(0, 1)
    record, _folder = _session(harness)
    deadline = sessions.trash(record.id, record.revision).purge_after
    assert deadline is not None
    assert purge.sweep(now=deadline - timedelta(microseconds=1))["attempted"] == 0
    assert purge.sweep(now=deadline)["complete"] == 1
    with database.session() as session:
        assert session.get(SessionRecord, record.id) is None


def test_stopped_partial_generation_and_interrupted_agent_can_be_purged(harness):
    from pandrator.web.models import AgentRun, GenerationPlan, GenerationPlanRevision, GenerationRun

    database, _paths, _sessions, purge = harness
    record, _folder = _session(harness)
    with database.session() as session:
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(plan_id=plan.id, revision_number=1, content_hash="fixture")
        session.add(revision)
        session.flush()
        session.add(GenerationRun(session_id=record.id, plan_revision_id=revision.id, status="partial"))
        session.add(AgentRun(session_id=record.id, status="interrupted"))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["can_purge"], preview
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"


def test_upgrade_keeps_existing_trash_without_a_deletion_deadline(harness):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from pandrator.web.database import sqlite_url

    database, paths, _sessions, purge = harness
    record, _folder = _session(harness)
    _trashed(harness, record)
    database.dispose()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(paths.database))
    command.downgrade(config, "0049_segment_count_index")
    upgrade_database(paths.database)
    assert purge.policy() == {"days": None, "revision": 0}
    assert purge.preview(record.id)["scheduled_delete_at"] is None
    assert purge.sweep()["attempted"] == 0
    with database.session() as session:
        assert session.get(SessionRecord, record.id).trashed_at is not None


def test_http_policy_preview_and_confirmed_purge_contract(tmp_path):
    from pandrator.web.api import create_app
    from pandrator.web.auth import BootstrapTokenStore

    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    try:
        client = app.test_client()
        token = client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}).get_json()["csrf_token"]
        headers = {"X-CSRF-Token": token}
        policy = client.get("/api/v1/session-trash-policy").get_json()
        assert policy == {"days": None, "revision": 0}
        updated = client.patch("/api/v1/session-trash-policy", json={"expected_revision": 0, "days": 3}, headers=headers)
        assert updated.status_code == 200
        record = client.post("/api/v1/sessions", json={"name": "Disposable purge HTTP fixture", "workflow_kind": "voiceover"}, headers=headers).get_json()
        path = f"/api/v1/sessions/{record['id']}"
        trashed = client.delete(path, headers={**headers, "If-Match": str(record["revision"])})
        assert trashed.status_code == 200 and trashed.get_json()["purge_after"]
        preview = client.get(path + "/purge-preview").get_json()
        assert preview["can_purge"]
        body = {"expected_revision": preview["revision"], "impact_token": preview["impact_token"]}
        assert client.post(path + "/purge", json={**body, "unexpected": True}, headers=headers).status_code == 400
        assert client.post(path + "/purge", json={**body, "expected_revision": 0}, headers=headers).status_code == 409
        response = client.post(path + "/purge", json=body, headers=headers)
        assert response.status_code == 200 and response.get_json() == {"state": "complete"}
        assert client.post(path + "/purge", json=body, headers=headers).get_json() == {"state": "complete"}
        assert client.get(path).status_code == 404
    finally:
        app.extensions["pandrator"]["database"].dispose()


def test_other_session_artifact_in_folder_blocks_unregistered_file(harness):
    _database, _paths, _sessions, purge = harness
    record, folder = _session(harness)
    other, _ = _session(harness, "other")
    file = folder / "owned-by-other.txt"
    file.write_text("retain")
    _artifact(harness, other, file, "other-owns-file")
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert not preview["can_purge"]
    assert "external_file_owner:other-owns-file" in preview["blockers"]
    assert file.exists()


def test_unsafe_storage_key_cannot_target_sessions_root(harness):
    _database, paths, sessions, purge = harness
    record = sessions.create("unsafe", storage_key="..")
    sessions.trash(record.id, record.revision)
    preview = purge.preview(record.id)
    assert not preview["can_purge"]
    assert any("unsafe_session_storage" in blocker for blocker in preview["blockers"])
    assert paths.sessions.is_dir()
