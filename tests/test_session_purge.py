"""Disposable database and storage checks for session purge."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm.attributes import flag_modified

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import ALL_SCOPES, Principal
from pandrator.web.database import Database, upgrade_database
from pandrator.web.models import (
    ApiIdempotency,
    Artifact,
    ArtifactEdge,
    Job,
    SessionPurge,
    SessionRecord,
    SessionSource,
    SourceAsset,
    TranslationProject,
    TranslationProjectBranch,
    TranslationProjectOperation,
    UploadSessionRecord,
    utcnow,
)
from pandrator.web.project_operations import (
    ProjectOperationError,
    TranslationProjectOperationService,
)
from pandrator.web.session_purge import PurgeBlocked, SessionPurgeService
from pandrator.web.sessions import RevisionConflict, SessionService
from pandrator.web.settings_policy import stable_hash
from pandrator.web.source_library import SourceLibraryService
from pandrator.web.upload_activity import UploadBusy, upload_activity
from pandrator.web.uploads import ChunkUploadService
from pandrator.web.workspace_settings import WorkspaceSettingsService


def _project_operation(harness, *, status="running"):
    database, _paths, _sessions, _purge = harness
    source, source_folder = _session(harness, "project source")
    selected, selected_folder = _session(harness, "Japanese")
    other, other_folder = _session(harness, "Polish")
    checkpoint = source_folder / "checkpoint.txt"
    checkpoint.write_text("source")
    _artifact(harness, source, checkpoint, "project-checkpoint")
    for record, folder, artifact_id in (
        (selected, selected_folder, "deleted-language-artifact"),
        (other, other_folder, "other-language-artifact"),
    ):
        path = folder / "export.txt"
        path.write_text(artifact_id)
        _artifact(harness, record, path, artifact_id)
    children = [
        {
            "branch_id": branch_id, "session_id": record.id,
            "target_language": language, "attempt": 2,
            "state": "completed", "eligible": True,
            "preview": {"text": canary}, "retained": {"settings": canary},
            "result": {"artifact_id": artifact_id, "download_url": canary},
            "job_id": f"{branch_id}-job", "generation_run_id": f"{branch_id}-run",
            "artifact_ids": [artifact_id], "frozen_settings": {"private": canary},
        }
        for branch_id, record, language, artifact_id, canary in (
            ("branch-ja", selected, "ja", "deleted-language-artifact", "PRIVATE-JA-CANARY"),
            ("branch-pl", other, "pl", "other-language-artifact", "KEEP-PL-CANARY"),
        )
    ]
    target = {"instance_id": "instance", "canonical_origin": "https://example.test",
              "application_version": "test"}
    private = {
        "target": target, "project_revision": 1, "export_kind": "configured",
        "previous_operation_id": "previous-audit-operation", "children": deepcopy(children),
        "captures": {
            child["branch_id"]: {
                "guard": {"session_id": child["session_id"]},
                "frozen_snapshot": {"text": child["preview"]["text"]},
            } for child in children
        },
    }
    with database.session() as db:
        db.add(TranslationProject(
            id="project", name="languages", source_session_id=source.id,
            checkpoint_artifact_id="project-checkpoint", source_content_hash="a" * 64,
            source_language="en",
        ))
        db.flush()
        for child in children:
            db.add(TranslationProjectBranch(
                id=child["branch_id"], project_id="project", session_id=child["session_id"],
                target_language=child["target_language"],
                source_checkpoint_artifact_id="project-checkpoint", source_content_hash="a" * 64,
            ))
        db.add(TranslationProjectOperation(
            id="operation", project_id="project", principal_subject="owner",
            target_instance_id="instance", action="export", status=status,
            preview_digest=stable_hash(private), preview_json=private,
            children_json=deepcopy(children), expires_at=utcnow() + timedelta(minutes=30),
        ))
    return SimpleNamespace(source=source, selected=selected, other=other, target=target,
                           private=private, children=children, digest=stable_hash(private))


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


def _uploads(harness):
    database, paths, _sessions, _purge = harness
    artifacts = ArtifactService(database, paths)
    return ChunkUploadService(database, paths, artifacts, SourceLibraryService(database, artifacts))


def _upload(harness, owner=None):
    service = _uploads(harness)
    upload = service.initialize(filename="pending.txt", size_bytes=10, session_id=owner.id if owner else None)
    directory = harness[1].temporary / "uploads" / upload["id"]
    return service, upload, directory


@pytest.mark.parametrize("status", ["queued", "running", "cancel_requested"])
@pytest.mark.parametrize("reference", ["session", "file_session", "artifact"])
def test_project_bundle_frozen_branch_reference_blocks_purge(harness, status, reference):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    language = {"session_id": case.other.id, "files": []}
    if reference == "session":
        language["session_id"] = case.selected.id
    elif reference == "file_session":
        language["files"] = [{"session_id": case.selected.id}]
    else:
        language["files"] = [{"artifact_id": "deleted-language-artifact"}]
    with database.session() as db:
        db.add(Job(id="bundle-job", kind="project.exports.bundle", status=status,
                   session_id=case.source.id, payload_json={"languages": [language]}))
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert preview["blockers"] == [f"unfinished:project.exports.bundle:{status}"]
    with pytest.raises(PurgeBlocked):
        purge.purge(case.selected.id, preview["revision"], preview["impact_token"])
    with database.session() as db:
        assert db.get(SessionRecord, case.selected.id) is not None
        assert db.get(SessionPurge, case.selected.id) is None


def test_project_operation_and_unrelated_bundle_do_not_block_idle_branch(harness):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    with database.session() as db:
        db.add(Job(id="other-child-job", kind="export", status="queued",
                   session_id=case.other.id))
        db.add(Job(id="other-bundle-job", kind="project.exports.bundle", status="running",
                   session_id=case.source.id, payload_json={"languages": [{
                       "session_id": case.other.id,
                       "files": [{"artifact_id": "other-language-artifact"}],
                   }]}))
    _trashed(harness, case.selected)
    assert purge.preview(case.selected.id)["can_purge"]


def _assert_project_operation_scrubbed(db, case):
    operation = db.get(TranslationProjectOperation, "operation")
    tombstone = {
        "branch_id": "branch-ja", "target_language": "ja", "attempt": 2,
        "state": "skipped", "eligible": False,
        "reason": "Language session permanently deleted.", "session_deleted": True,
    }
    assert operation.children_json == [tombstone, case.children[1]]
    assert operation.preview_json["children"] == [tombstone, case.children[1]]
    assert operation.preview_json["captures"] == {
        "branch-pl": case.private["captures"]["branch-pl"],
    }
    serialized = json.dumps({"children": operation.children_json, "preview": operation.preview_json})
    assert "PRIVATE-JA-CANARY" not in serialized
    assert "deleted-language-artifact" not in serialized
    assert case.selected.id not in serialized
    assert "KEEP-PL-CANARY" in serialized
    assert operation.id == "operation" and operation.project_id == "project"
    assert operation.principal_subject == "owner" and operation.target_instance_id == "instance"
    assert operation.preview_json["target"] == case.target
    assert operation.preview_json["previous_operation_id"] == "previous-audit-operation"
    assert operation.preview_digest == stable_hash(operation.preview_json)
    assert operation.preview_digest != case.digest
    assert operation.revision == 2
    return tombstone


@pytest.mark.parametrize("status", ["preview", "running", "completed"])
def test_project_operation_private_capture_scrubbed_with_branch_purge(harness, status):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness, status=status)
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert preview["can_purge"]
    with database.session() as db:
        assert db.get(TranslationProjectOperation, "operation").preview_json == case.private
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"]) == {"state": "complete"}
    with database.session() as db:
        _assert_project_operation_scrubbed(db, case)
        assert db.get(TranslationProjectBranch, "branch-ja") is None
        assert db.get(TranslationProjectBranch, "branch-pl") is not None
        assert db.get(SessionRecord, case.other.id) is not None
        assert db.get(Artifact, "other-language-artifact") is not None
        assert db.get(TranslationProjectOperation, "operation").status == status


def test_project_purge_scrubs_every_matching_operation_and_preserves_audit_identity(harness):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    with database.session() as db:
        db.add(TranslationProjectOperation(
            id="second-operation", project_id="project", principal_subject="other-principal",
            target_instance_id="other-instance", action="translate", status="preview",
            preview_digest=case.digest, preview_json=deepcopy(case.private),
            children_json=deepcopy(case.children), expires_at=utcnow() + timedelta(minutes=30),
        ))
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    with database.session() as db:
        _assert_project_operation_scrubbed(db, case)
        operation = db.get(TranslationProjectOperation, "second-operation")
        assert "PRIVATE-JA-CANARY" not in json.dumps(operation.preview_json)
        assert "PRIVATE-JA-CANARY" not in json.dumps(operation.children_json)
        assert operation.children_json[1] == case.children[1]
        assert operation.principal_subject == "other-principal"
        assert operation.target_instance_id == "other-instance"
        assert operation.action == "translate" and operation.status == "preview"


def test_project_source_session_purge_keeps_existing_fk_restrictions(harness):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    _trashed(harness, case.source)
    preview = purge.preview(case.source.id)
    assert not preview["can_purge"]
    assert "external_reference:translation_project_branches.source_checkpoint_artifact_id" in preview["blockers"]
    with database.session() as db:
        assert db.get(TranslationProject, "project") is not None
        assert db.get(Artifact, "project-checkpoint") is not None
        assert db.get(TranslationProjectOperation, "operation").preview_json == case.private


def test_project_operation_scrub_rolls_back_and_retries_with_branch_delete(harness, monkeypatch):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    original = purge._scrub_project_operations

    def interrupted(db, session_id):
        original(db, session_id)
        db.flush()
        raise OSError("injected failure after privacy scrub")

    monkeypatch.setattr(purge, "_scrub_project_operations", interrupted)
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "failed"
    with database.session() as db:
        operation = db.get(TranslationProjectOperation, "operation")
        assert operation.preview_json == case.private
        assert operation.children_json == case.children
        assert operation.preview_digest == case.digest and operation.revision == 1
        assert db.get(TranslationProjectBranch, "branch-ja") is not None
        assert db.get(SessionRecord, case.selected.id).status == "purging"
    monkeypatch.setattr(purge, "_scrub_project_operations", original)
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    with database.session() as db:
        _assert_project_operation_scrubbed(db, case)


def test_old_project_operation_preview_cannot_enqueue_purged_branch(harness):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness, status="preview")
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    service = TranslationProjectOperationService(SimpleNamespace(database=database))
    principal = Principal("owner", "owner_session", ALL_SCOPES, None, "loopback", "instance")
    with pytest.raises(ProjectOperationError) as rejected:
        service.execute("operation", case.digest, [], principal=principal,
                        target_identity=case.target, idempotency_key="old-preview-key")
    assert rejected.value.code == "preview_digest_mismatch"
    with database.session() as db:
        assert not list(db.query(Job).all())
        with pytest.raises(ProjectOperationError) as missing:
            service._branch_guard(db, "project", "branch-ja")
        assert missing.value.code == "branch_not_owned"


def test_purged_project_child_poll_and_retry_never_restore_old_job_run_receipt(harness, monkeypatch):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    with database.session() as db:
        db.add(ApiIdempotency(
            id="child-receipt", principal_subject="owner",
            operation_id="executeProjectGenerationChild",
            idempotency_key="operation:branch-ja:2", request_digest="d" * 64,
            state="completed", response_json={"job_id": "deleted-job", "id": "deleted-run"},
            expires_at=utcnow() + timedelta(days=1),
        ))
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    service = TranslationProjectOperationService(SimpleNamespace(
        database=database, redactor=SimpleNamespace(redact_value=lambda value: value),
    ))
    principal = Principal("owner", "owner_session", ALL_SCOPES, None, "loopback", "instance")
    with database.session() as db:
        tombstone = _assert_project_operation_scrubbed(db, case)
        assert service._receipt(db, db.get(TranslationProjectOperation, "operation"), tombstone) == {}
    view = service.get("operation", principal=principal, target_identity=case.target)
    assert view["children"][0] == tombstone
    assert "deleted-job" not in json.dumps(view) and "deleted-run" not in json.dumps(view)
    captured = {}

    def preview_retry(project_id, branch_ids, **kwargs):
        captured.update(project_id=project_id, branch_ids=branch_ids)
        return {"children": [], "retry_test": True}

    monkeypatch.setattr(service, "preview", preview_retry)
    assert service.retry_preview("operation", principal=principal, target_identity=case.target,
                         expected_project_revision=1, idempotency_key="retry-operation-key")["retry_test"]
    assert captured == {"project_id": "project", "branch_ids": ["branch-pl"]}
    with database.session() as db:
        assert not db.query(Job).all()


def _completed_project_bundle(harness, case, *, manifest=True):
    database, paths, _sessions, _purge = harness
    source_ids = ["deleted-language-artifact", "other-language-artifact"]
    frozen = {
        "project_id": "project", "operation_id": "operation", "project_revision": 1,
        "source_session_id": case.source.id, "principal_subject": "owner",
        "target_identity": case.target, "input_digest": "b" * 64,
        "manifest_digest": "c" * 64, "languages": [],
    }
    output_paths = {}
    result = {"input_digest": frozen["input_digest"], "manifest_digest": frozen["manifest_digest"]}
    with database.session() as db:
        for child, artifact_id in zip(case.children, source_ids, strict=True):
            artifact = db.get(Artifact, artifact_id)
            path = paths.root / artifact.relative_path
            artifact.role = "export_text"
            artifact.content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            artifact.size_bytes = path.stat().st_size
            frozen["languages"].append({
                "branch_id": child["branch_id"], "session_id": child["session_id"],
                "language": child["target_language"], "operation_state": "completed",
                "files": [{"artifact_id": artifact.id, "session_id": artifact.session_id,
                           "relative_path": artifact.relative_path, "role": artifact.role,
                           "sha256": artifact.content_hash}],
            })
        contracts = [("project_export_bundle", "zip", "artifact_id", "sha256", "size_bytes")]
        if manifest:
            contracts.append(("project_export_manifest", "json", "manifest_artifact_id",
                              "manifest_sha256", "manifest_size_bytes"))
        for role, kind, id_key, hash_key, size_key in contracts:
            path = paths.sessions / case.source.storage_key / f"{role}.{kind}"
            path.write_bytes(b"retained selected Japanese and Polish content")
            artifact_id = f"{role}-id"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            db.add(Artifact(
                id=artifact_id, session_id=case.source.id, kind=kind, role=role,
                relative_path=path.relative_to(paths.root).as_posix(), state="current",
                content_hash=digest, size_bytes=path.stat().st_size,
                metadata_json={
                    "project_id": "project", "operation_id": "operation",
                    "source_operation_id": "operation", "source_artifact_ids": sorted(source_ids),
                    "input_digest": frozen["input_digest"], "manifest_digest": frozen["manifest_digest"],
                    "output_kind": role,
                },
            ))
            db.flush()
            for source_id in source_ids:
                db.add(ArtifactEdge(parent_artifact_id=source_id, child_artifact_id=artifact_id))
            result.update({id_key: artifact_id, hash_key: digest, size_key: path.stat().st_size})
            output_paths[artifact_id] = path
        db.add(Job(id="completed-bundle-job", kind="project.exports.bundle", status="succeeded",
                   session_id=case.source.id, payload_json=frozen, result_json=result))
    return output_paths


def _committed_bundle_receipt(harness, *, status="failed"):
    with harness[0].session() as db:
        job = db.get(Job, "completed-bundle-job")
        payload, result = deepcopy(job.payload_json), deepcopy(job.result_json)
        payload["bundle_job_id"] = job.id
        result["publication_receipt"] = {
            "schema_version": 1, "job_id": job.id,
            "project_id": payload["project_id"], "source_operation_id": payload["operation_id"],
            "source_session_id": job.session_id,
            "source_artifact_ids": sorted(file["artifact_id"] for language in payload["languages"]
                                          for file in language["files"]),
            "input_digest": result["input_digest"], "manifest_digest": result["manifest_digest"],
            "bundle_artifact_id": result["artifact_id"], "bundle_sha256": result["sha256"],
            "bundle_size_bytes": result["size_bytes"],
            "manifest_artifact_id": result["manifest_artifact_id"],
            "manifest_sha256": result["manifest_sha256"], "manifest_size_bytes": result["manifest_size_bytes"],
        }
        job.payload_json, job.result_json, job.status = payload, result, status


@pytest.mark.parametrize("source_state", ["current", "superseded"])
@pytest.mark.parametrize("job_status", ["succeeded", "failed"])
def test_completed_project_exports_disclosed_and_retained_during_branch_purge(harness, source_state, job_status):
    database, paths, _sessions, purge = harness
    case = _project_operation(harness)
    outputs = _completed_project_bundle(harness, case)
    if job_status == "failed":
        _committed_bundle_receipt(harness)
    original = {artifact_id: path.read_bytes() for artifact_id, path in outputs.items()}
    with database.session() as db:
        # A later project edit must not turn its independent completed copies
        # into a permanent blocker for the selected branch's deletion.
        db.get(TranslationProject, "project").revision = 2
        operation = db.get(TranslationProjectOperation, "operation")
        children = deepcopy(operation.children_json)
        children[0]["state"] = "queued"  # Durable receipt; completion is in Job.
        operation.children_json = children
        case.children = children
        db.get(Artifact, "deleted-language-artifact").state = source_state
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert preview["can_purge"]
    assert preview["retained_project_exports"] == {
        "bundle_count": 1, "manifest_count": 1, "artifact_ids": sorted(outputs),
    }
    assert not any("path" in key or "text" in key for key in preview["retained_project_exports"])
    assert purge.purge(case.selected.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    with database.session() as db:
        assert db.get(Artifact, "deleted-language-artifact") is None
        assert db.get(Artifact, "other-language-artifact") is not None
        for artifact_id in outputs:
            assert db.get(Artifact, artifact_id).session_id == case.source.id
            assert db.get(ArtifactEdge, ("deleted-language-artifact", artifact_id)) is None
            assert db.get(ArtifactEdge, ("other-language-artifact", artifact_id)) is not None
        assert db.get(Job, "completed-bundle-job").status == job_status
    assert {artifact_id: path.read_bytes() for artifact_id, path in outputs.items()} == original
    assert (paths.sessions / case.other.storage_key / "export.txt").is_file()
    # A previous language deletion removes its source rows and edges; the
    # surviving committed receipt still identifies the remaining language.
    _trashed(harness, case.other)
    other_preview = purge.preview(case.other.id)
    assert other_preview["can_purge"]
    assert other_preview["retained_project_exports"] == preview["retained_project_exports"]
    assert purge.purge(case.other.id, other_preview["revision"], other_preview["impact_token"])["state"] == "complete"
    assert {artifact_id: path.read_bytes() for artifact_id, path in outputs.items()} == original
    with database.session() as db:
        operation = db.get(TranslationProjectOperation, "operation")
        assert operation.preview_json["captures"] == {}
        assert all(child.get("session_deleted") for child in operation.children_json)


@pytest.mark.parametrize("field", [
    "schema_version", "job_id", "project_id", "source_operation_id", "source_session_id",
    "source_artifact_ids", "input_digest", "manifest_digest", "bundle_artifact_id",
    "bundle_sha256", "bundle_size_bytes", "manifest_artifact_id", "manifest_sha256",
    "manifest_size_bytes", "missing_receipt", "boolean_schema", "payload_job_id",
])
def test_failed_project_export_forged_publication_receipt_blocks_branch_purge(harness, field):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    _completed_project_bundle(harness, case)
    _committed_bundle_receipt(harness)
    with database.session() as db:
        job = db.get(Job, "completed-bundle-job")
        result, payload = deepcopy(job.result_json), deepcopy(job.payload_json)
        if field == "missing_receipt":
            result.pop("publication_receipt")
        elif field == "boolean_schema":
            result["publication_receipt"]["schema_version"] = True
        elif field == "payload_job_id":
            payload["bundle_job_id"] = "other-job"
        else:
            result["publication_receipt"][field] = "forged"
        job.result_json, job.payload_json = result, payload
        flag_modified(job, "result_json")  # JSON True compares equal to 1 in Python.
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert "external_reference:artifact_edges" in preview["blockers"]
    assert preview["retained_project_exports"] == {"bundle_count": 0, "manifest_count": 0, "artifact_ids": []}
    with pytest.raises(PurgeBlocked):
        purge.purge(case.selected.id, preview["revision"], preview["impact_token"])


@pytest.mark.parametrize("mismatch", [
    "missing_manifest", "state", "owner", "kind", "role", "metadata", "hash", "size",
    "result_digest", "missing_edge", "extra_edge", "missing_file",
])
def test_failed_project_export_incomplete_or_mismatched_pair_blocks_branch_purge(harness, mismatch):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    outputs = _completed_project_bundle(harness, case)
    _committed_bundle_receipt(harness)
    with database.session() as db:
        manifest = db.get(Artifact, "project_export_manifest-id")
        job = db.get(Job, "completed-bundle-job")
        if mismatch == "missing_manifest":
            db.delete(manifest)
        elif mismatch == "state":
            manifest.state = "superseded"
        elif mismatch == "owner":
            manifest.session_id = case.other.id
        elif mismatch == "kind":
            manifest.kind = "zip"
        elif mismatch == "role":
            manifest.role = "arbitrary"
        elif mismatch == "metadata":
            metadata = deepcopy(manifest.metadata_json)
            metadata["source_operation_id"] = "other-operation"
            manifest.metadata_json = metadata
        elif mismatch == "hash":
            manifest.content_hash = "e" * 64
        elif mismatch == "size":
            manifest.size_bytes += 1
        elif mismatch == "result_digest":
            result = deepcopy(job.result_json)
            result["manifest_digest"] = "e" * 64
            job.result_json = result
        elif mismatch == "missing_edge":
            db.delete(db.get(ArtifactEdge, ("deleted-language-artifact", manifest.id)))
        elif mismatch == "extra_edge":
            db.add(ArtifactEdge(parent_artifact_id="project-checkpoint", child_artifact_id=manifest.id))
        elif mismatch == "missing_file":
            outputs[manifest.id].unlink()
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert "external_reference:artifact_edges" in preview["blockers"]
    assert preview["retained_project_exports"] == {"bundle_count": 0, "manifest_count": 0, "artifact_ids": []}


@pytest.mark.parametrize("status", ["canceled", "queued", "running", "cancel_requested"])
def test_committed_project_export_receipt_does_not_relax_other_job_statuses(harness, status):
    _database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    _completed_project_bundle(harness, case)
    _committed_bundle_receipt(harness, status=status)
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert "external_reference:artifact_edges" in preview["blockers"]
    assert preview["retained_project_exports"] == {"bundle_count": 0, "manifest_count": 0, "artifact_ids": []}
    if status != "canceled":
        assert f"unfinished:project.exports.bundle:{status}" in preview["blockers"]


@pytest.mark.parametrize("invalid", [
    "role", "state", "source_operation", "project", "source_ids", "job_status",
    "result_id", "result_hash", "file_receipt", "missing_file", "reverse_edge",
])
def test_unvalidated_project_export_edge_still_blocks_branch_purge(harness, invalid):
    database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    outputs = _completed_project_bundle(harness, case, manifest=False)
    with database.session() as db:
        output = db.get(Artifact, "project_export_bundle-id")
        job = db.get(Job, "completed-bundle-job")
        metadata = deepcopy(output.metadata_json)
        if invalid == "role":
            output.role = "arbitrary_shared_copy"
        elif invalid == "state":
            output.state = "superseded"
        elif invalid == "source_operation":
            metadata["source_operation_id"] = "missing-operation"
        elif invalid == "project":
            metadata["project_id"] = "missing-project"
        elif invalid == "source_ids":
            metadata["source_artifact_ids"] = ["other-language-artifact"]
        elif invalid == "job_status":
            job.status = "failed"
        elif invalid.startswith("result_"):
            result = deepcopy(job.result_json)
            result["artifact_id" if invalid == "result_id" else "sha256"] = "wrong"
            job.result_json = result
        elif invalid == "file_receipt":
            payload = deepcopy(job.payload_json)
            payload["languages"][0]["files"][0]["relative_path"] = "artifacts/wrong.txt"
            job.payload_json = payload
        elif invalid == "missing_file":
            outputs[output.id].unlink()
        elif invalid == "reverse_edge":
            db.delete(db.get(ArtifactEdge, ("deleted-language-artifact", output.id)))
            db.flush()
            db.add(ArtifactEdge(parent_artifact_id=output.id, child_artifact_id="deleted-language-artifact"))
        output.metadata_json = metadata
    _trashed(harness, case.selected)
    preview = purge.preview(case.selected.id)
    assert "external_reference:artifact_edges" in preview["blockers"]
    assert preview["retained_project_exports"] == {"bundle_count": 0, "manifest_count": 0, "artifact_ids": []}


def test_completed_project_export_disclosure_is_bound_to_impact_token(harness):
    _database, _paths, _sessions, purge = harness
    case = _project_operation(harness)
    _trashed(harness, case.selected)
    before = purge.preview(case.selected.id)
    _completed_project_bundle(harness, case)
    after = purge.preview(case.selected.id)
    assert before["can_purge"] and after["can_purge"]
    assert before["impact_token"] != after["impact_token"]
    with pytest.raises(RevisionConflict):
        purge.purge(case.selected.id, before["revision"], before["impact_token"])


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


def test_pending_upload_files_and_empty_directories_are_purged(harness):
    database, _paths, _sessions, purge = harness
    record, _folder = _session(harness)
    other, _ = _session(harness, "other")
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    (directory / "00000001.tmp").write_bytes(b"tmp")
    (directory / "assembled.part").write_bytes(b"part")
    (directory / "empty").mkdir()
    _, empty_upload, empty_directory = _upload(harness, record)
    _, other_upload, other_directory = _upload(harness, other)
    _, unbound_upload, unbound_directory = _upload(harness)
    (other_directory / "other.part").write_bytes(b"other")
    (unbound_directory / "global.part").write_bytes(b"global")
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert preview["can_purge"]
    assert preview["owned_file_count"] == 3 and preview["owned_bytes"] == 17
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert not directory.exists() and not empty_directory.exists()
    assert (other_directory / "other.part").read_bytes() == b"other"
    assert (unbound_directory / "global.part").read_bytes() == b"global"
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]) is None
        assert session.get(UploadSessionRecord, empty_upload["id"]) is None
        assert session.get(UploadSessionRecord, other_upload["id"]) is not None
        assert session.get(UploadSessionRecord, unbound_upload["id"]) is not None


def test_empty_upload_identity_and_state_change_the_impact_token(harness):
    database, _paths, _sessions, purge = harness
    record, _ = _session(harness)
    _uploads_service, upload, _directory = _upload(harness, record)
    _trashed(harness, record)
    before = purge.preview(record.id)
    with database.immediate_session() as session:
        session.get(UploadSessionRecord, upload["id"]).state = "canceled"
    after = purge.preview(record.id)
    assert before["owned_file_count"] == after["owned_file_count"] == 0
    assert before["impact_token"] != after["impact_token"]
    with pytest.raises(RevisionConflict, match="impact changed"):
        purge.purge(record.id, before["revision"], before["impact_token"])


def test_pending_upload_unlink_interruption_retains_records_and_retries(harness, monkeypatch):
    database, _paths, _sessions, purge = harness
    record, _ = _session(harness)
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    (directory / "extra.tmp").write_bytes(b"temporary")
    _trashed(harness, record)
    preview = purge.preview(record.id)
    remove = purge._remove_managed_entry
    calls = 0

    def interrupt(relative, *, directory=False):
        nonlocal calls
        remove(relative, directory=directory)
        calls += 1
        if calls == 1:
            raise OSError("interrupted after first unlink")

    monkeypatch.setattr(purge, "_remove_managed_entry", interrupt)
    result = purge.purge(record.id, preview["revision"], preview["impact_token"])
    assert result["state"] == "failed"
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]) is not None
        assert session.get(SessionRecord, record.id).status == "purging"
        assert len(session.get(SessionPurge, record.id).manifest_json) == 2
    monkeypatch.setattr(purge, "_remove_managed_entry", remove)
    assert purge.sweep() == {"attempted": 1, "complete": 1, "failed": 0}
    assert not directory.exists()
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]) is None


def test_upload_directory_removal_failure_keeps_empty_upload_for_retry(harness, monkeypatch):
    database, _paths, _sessions, purge = harness
    record, _ = _session(harness)
    _, upload, directory = _upload(harness, record)
    _trashed(harness, record)
    preview = purge.preview(record.id)
    remove = purge._remove_managed_entry

    def fail_directory(relative, *, directory=False):
        if relative.endswith(upload["id"]):
            raise OSError("directory busy")
        remove(relative, directory=directory)

    monkeypatch.setattr(purge, "_remove_managed_entry", fail_directory)
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "failed"
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]) is not None
    monkeypatch.setattr(purge, "_remove_managed_entry", remove)
    assert purge.sweep()["complete"] == 1 and not directory.exists()


@pytest.mark.parametrize("unsafe", ["traversal", "absolute", "alias", "directory_symlink", "file_symlink", "fifo"])
def test_unsafe_upload_storage_blocks_purge(harness, unsafe):
    database, paths, _sessions, purge = harness
    record, _ = _session(harness)
    _, upload, directory = _upload(harness, record)
    sentinel = paths.root / "sentinel.txt"
    sentinel.write_text("retain")
    with database.session() as session:
        row = session.get(UploadSessionRecord, upload["id"])
        if unsafe == "traversal":
            row.temporary_relative_path = f"tmp/uploads/{upload['id']}/../{upload['id']}"
        elif unsafe == "absolute":
            row.temporary_relative_path = str(directory)
        elif unsafe == "alias":
            row.temporary_relative_path = f"tmp/uploads/./{upload['id']}"
        elif unsafe == "directory_symlink":
            directory.rmdir()
            directory.symlink_to(paths.root, target_is_directory=True)
        elif unsafe == "file_symlink":
            (directory / "linked.part").symlink_to(sentinel)
        else:
            os.mkfifo(directory / "pipe.part")
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert not preview["can_purge"]
    assert any(blocker.startswith("unsafe_upload_storage:") for blocker in preview["blockers"])
    with pytest.raises(PurgeBlocked):
        purge.purge(record.id, preview["revision"], preview["impact_token"])
    assert sentinel.read_text() == "retain"
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]) is not None


@pytest.mark.parametrize("unbound", [False, True])
def test_colliding_other_upload_owner_blocks_purge(harness, unbound):
    database, _paths, _sessions, purge = harness
    record, _ = _session(harness)
    other, _ = _session(harness, "other")
    _, _upload_record, directory = _upload(harness, record)
    _, other_upload, _ = _upload(harness, None if unbound else other)
    (directory / "shared.part").write_bytes(b"retain")
    with database.session() as session:
        session.get(UploadSessionRecord, other_upload["id"]).temporary_relative_path = directory.relative_to(harness[1].root).as_posix()
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert f"external_upload_owner:{other_upload['id']}" in preview["blockers"]
    with pytest.raises(PurgeBlocked):
        purge.purge(record.id, preview["revision"], preview["impact_token"])
    assert (directory / "shared.part").read_bytes() == b"retain"


def test_shared_artifact_in_upload_storage_blocks_purge(harness):
    database, _paths, _sessions, purge = harness
    record, _ = _session(harness)
    _, _upload_record, directory = _upload(harness, record)
    file = directory / "shared.part"
    file.write_text("shared")
    _artifact(harness, record, file, "upload-shared-artifact")
    with database.session() as session:
        session.add(SourceAsset(id="upload-source", artifact_id="upload-shared-artifact", display_name="source", kind="text"))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    assert "shared_upload_storage" in preview["blockers"]
    assert file.read_text() == "shared"


@pytest.mark.parametrize("status", ["trashed", "purging"])
def test_upload_mutations_reject_unwritable_owner_without_filesystem_changes(harness, status):
    database, paths, _sessions, _purge = harness
    record, _ = _session(harness)
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    with database.immediate_session() as session:
        owner = session.get(SessionRecord, record.id)
        owner.status = status
        owner.trashed_at = utcnow()
    for action in (
        lambda: uploads.write_chunk(upload["id"], 0, io.BytesIO(b"abcdefghij")),
        lambda: uploads.complete(upload["id"]),
        lambda: uploads.cancel(upload["id"]),
        lambda: uploads.initialize(filename="new.txt", size_bytes=10, session_id=record.id),
    ):
        with pytest.raises(ValueError, match="trashed or purging"):
            action()
    with database.immediate_session() as session:
        with pytest.raises(ValueError, match="trashed or purging"):
            uploads.initialize_in_session(session, filename="new.txt", size_bytes=10, session_id=record.id, upload_id="denied-upload")
    assert not (paths.temporary / "uploads" / "denied-upload").exists()
    assert (directory / "00000000.part").read_bytes() == b"1234567890"
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]).state == "open"


def test_expired_upload_cleanup_retries_failure_and_preserves_trashed_owner(harness, monkeypatch):
    database, _paths, _sessions, _purge = harness
    record, _ = _session(harness)
    uploads, expired, directory = _upload(harness)
    _, trashed, trashed_directory = _upload(harness, record)
    (directory / "partial.tmp").write_bytes(b"partial")
    (trashed_directory / "partial.tmp").write_bytes(b"retain")
    with database.immediate_session() as session:
        for identifier in (expired["id"], trashed["id"]):
            session.get(UploadSessionRecord, identifier).expires_at = utcnow() - timedelta(hours=1)
    _trashed(harness, record)
    import pandrator.web.uploads as upload_module

    remove = upload_module.shutil.rmtree
    monkeypatch.setattr(upload_module.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError, match="interrupted"):
        uploads.cleanup_expired()
    with database.session() as session:
        assert session.get(UploadSessionRecord, expired["id"]).state == "open"
    monkeypatch.setattr(upload_module.shutil, "rmtree", remove)
    assert uploads.cleanup_expired() == 1
    assert not directory.exists() and trashed_directory.exists()
    with database.session() as session:
        assert session.get(UploadSessionRecord, expired["id"]).state == "expired"
        assert session.get(UploadSessionRecord, trashed["id"]).state == "open"


def test_activity_lock_allows_settings_and_trash_but_blocks_purge_during_stream(harness):
    database, paths, sessions, purge = harness
    record, _ = _session(harness)
    other, _ = _session(harness, "unrelated")
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    (directory / "interrupted.tmp").write_bytes(b"retain until purge")
    (directory / "empty").mkdir()
    reading = threading.Event()
    release = threading.Event()
    mutations_done = threading.Event()
    outcomes = []

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            reading.set()
            assert release.wait(8), "test stream was not released"
            return super().read(size)

    def write():
        try:
            uploads.write_chunk(upload["id"], 0, BoundedStream(b"abcdefghij"))
        except Exception as error:
            outcomes.append(("write", error))

    def mutate():
        try:
            settings = WorkspaceSettingsService(database)
            revision = settings.get(other.id, "text")["revision"]
            outcomes.append(("settings", settings.update(other.id, "text", revision, {"max_sentence_length": 333})))
            outcomes.append(("trash", sessions.trash(record.id, record.revision)))
        except Exception as error:
            outcomes.append(("mutations", error))
        finally:
            mutations_done.set()

    writer = threading.Thread(target=write, daemon=True)
    mutator = threading.Thread(target=mutate, daemon=True)
    writer.start()
    try:
        assert reading.wait(5)
        for service in (uploads, _uploads(harness)):
            with pytest.raises(UploadBusy):
                service.write_chunk(upload["id"], 0, io.BytesIO(b"abcdefghij"))
            with pytest.raises(UploadBusy):
                service.complete(upload["id"])
            with pytest.raises(UploadBusy):
                service.cancel(upload["id"])
        code = """import sys
from pandrator.runtime import DataPaths
from pandrator.web.upload_activity import UploadBusy, upload_activity
try:
    with upload_activity(DataPaths.from_value(sys.argv[1]), session_id=sys.argv[2]):
        raise AssertionError('cross-process exclusion failed')
except UploadBusy:
    print('busy')
"""
        probe = subprocess.run([sys.executable, "-c", code, str(paths.root), record.id],
                               capture_output=True, text=True, timeout=5,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert probe.returncode == 0 and probe.stdout.strip() == "busy", probe.stderr
        mutator.start()
        assert mutations_done.wait(3), "unrelated settings/trash waited on upload IO"
        assert "mutations" not in dict(outcomes)
        assert dict(outcomes)["settings"]["effective"]["max_sentence_length"] == 333
        preview = purge.preview(record.id)
        assert not preview["can_purge"] and "upload_writer_active" in preview["blockers"]
        with pytest.raises(PurgeBlocked) as blocked:
            purge.purge(record.id, preview["revision"], preview["impact_token"])
        assert blocked.value.blockers == ["upload_writer_active"]
    finally:
        release.set()
        writer.join(5)
        if mutator.ident is not None:
            mutator.join(5)
    assert not writer.is_alive() and not mutator.is_alive()
    assert isinstance(dict(outcomes)["write"], ValueError)
    assert "trashed or purging" in str(dict(outcomes)["write"])
    assert (directory / "00000000.part").read_bytes() == b"1234567890"
    assert sorted(path.name for path in directory.glob("*.tmp")) == ["interrupted.tmp"]
    preview = purge.preview(record.id)
    assert preview["can_purge"]
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
    assert not directory.exists()
    with database.session() as session:
        assert session.get(SessionRecord, record.id) is None
        assert session.get(UploadSessionRecord, upload["id"]) is None
    with pytest.raises(KeyError):
        _uploads(harness).write_chunk(upload["id"], 0, io.BytesIO(b"abcdefghij"))


def test_complete_preserves_chunks_if_registration_transaction_fails(harness, monkeypatch):
    database, paths, _sessions, _purge = harness
    record, _ = _session(harness)
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    immediate = database.immediate_session

    @contextmanager
    def fail_before_commit():
        with immediate() as session:
            yield session
            raise OSError("commit interrupted")

    monkeypatch.setattr(database, "immediate_session", fail_before_commit)
    with pytest.raises(OSError, match="commit interrupted"):
        uploads.complete(upload["id"])
    assert (directory / "00000000.part").read_bytes() == b"1234567890"
    assert not list(paths.uploads.iterdir())
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]).state == "open"
    monkeypatch.setattr(database, "immediate_session", immediate)
    result = uploads.complete(upload["id"])
    assert result["size_bytes"] == 10 and not directory.exists()
    assert uploads.complete(upload["id"]) == result


def test_purge_claim_rejects_chunk_writer_without_wait_or_file_resurrection(harness, monkeypatch):
    database, _paths, _sessions, purge = harness
    record, _ = _session(harness)
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    _trashed(harness, record)
    preview = purge.preview(record.id)
    removing = threading.Event()
    release = threading.Event()
    writing = threading.Event()
    write_done = threading.Event()
    outcomes = []
    remove = purge._remove_managed_entry

    def pause_unlink(relative, *, directory=False):
        removing.set()
        assert release.wait(5), "test purge was not released"
        remove(relative, directory=directory)

    def run_purge():
        try:
            outcomes.append(("purge", purge.purge(record.id, preview["revision"], preview["impact_token"])))
        except Exception as error:
            outcomes.append(("purge", error))

    def run_write():
        try:
            writing.set()
            # A separate service proves its process-local lock is irrelevant.
            _uploads(harness).write_chunk(upload["id"], 0, io.BytesIO(b"abcdefghij"))
        except Exception as error:
            outcomes.append(("write", error))
        finally:
            write_done.set()

    monkeypatch.setattr(purge, "_remove_managed_entry", pause_unlink)
    purger = threading.Thread(target=run_purge, daemon=True)
    writer = threading.Thread(target=run_write, daemon=True)
    purger.start()
    try:
        assert removing.wait(5)
        writer.start()
        assert writing.wait(5)
        assert write_done.wait(2), "writer waited for the purge activity lock"
    finally:
        release.set()
        purger.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not writer.is_alive() and not purger.is_alive()
    assert dict(outcomes)["purge"]["state"] == "complete"
    assert isinstance(dict(outcomes)["write"], UploadBusy)
    assert not directory.exists()
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]) is None


def test_post_commit_cleanup_failure_preserves_registered_artifact_and_replay(harness, monkeypatch):
    database, paths, _sessions, _purge = harness
    record, _ = _session(harness)
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    import pandrator.web.uploads as upload_module

    def fail_cleanup(_directory, **_kwargs):
        raise OSError("post-commit cleanup interrupted")

    monkeypatch.setattr(upload_module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(OSError, match="post-commit cleanup interrupted"):
        uploads.complete(upload["id"])
    with database.session() as session:
        current = session.get(UploadSessionRecord, upload["id"])
        assert current.state == "completed"
        result = dict(current.result_json)
        artifact = session.get(Artifact, result["artifact_id"])
        registered = paths.root / artifact.relative_path
    assert registered.read_bytes() == b"1234567890"
    assert directory.exists()
    assert uploads.complete(upload["id"]) == result
    assert registered.read_bytes() == b"1234567890"


def test_activity_lock_is_nonblocking_scoped_and_keeps_its_inode(harness):
    _database, paths, _sessions, _purge = harness
    with upload_activity(paths, session_id="owner-one"):
        lock = paths.temporary / "upload-locks/session-owner-one.lock"
        inode = lock.stat().st_ino
        with pytest.raises(UploadBusy):
            with upload_activity(paths, session_id="owner-one"):
                pytest.fail("same owner lock was acquired twice")
        with upload_activity(paths, session_id="owner-two"):
            with upload_activity(paths, upload_id="unbound-upload"):
                pass
    assert lock.exists() and lock.stat().st_ino == inode
    with upload_activity(paths, session_id="owner-one"):
        assert lock.stat().st_ino == inode


@pytest.mark.parametrize("unsafe", ["identifier", "lock_symlink", "lock_fifo", "ancestor_symlink", "ancestor_file"])
def test_activity_lock_rejects_unsafe_paths(harness, unsafe):
    _database, paths, _sessions, _purge = harness
    directory = paths.temporary / "upload-locks"
    identifier = "safe-owner"
    if unsafe == "identifier":
        identifier = "../escape"
    elif unsafe == "ancestor_symlink":
        directory.symlink_to(paths.sessions, target_is_directory=True)
    elif unsafe == "ancestor_file":
        directory.write_text("retain")
    else:
        directory.mkdir()
        lock = directory / f"session-{identifier}.lock"
        if unsafe == "lock_symlink":
            sentinel = paths.root / "sentinel-lock"
            sentinel.write_text("retain")
            lock.symlink_to(sentinel)
        else:
            os.mkfifo(lock)
    with pytest.raises(ValueError):
        with upload_activity(paths, session_id=identifier):
            pytest.fail("unsafe lock was accepted")
    if unsafe == "lock_symlink":
        assert (paths.root / "sentinel-lock").read_text() == "retain"


def test_expiry_skips_busy_activity_and_retries_after_release(harness):
    database, paths, _sessions, _purge = harness
    uploads, upload, directory = _upload(harness)
    (directory / "partial.tmp").write_bytes(b"partial")
    with database.immediate_session() as session:
        session.get(UploadSessionRecord, upload["id"]).expires_at = utcnow() - timedelta(hours=1)
    with upload_activity(paths, upload_id=upload["id"]):
        assert uploads.cleanup_expired() == 0
        assert (directory / "partial.tmp").read_bytes() == b"partial"
        with database.session() as session:
            assert session.get(UploadSessionRecord, upload["id"]).state == "open"
    assert uploads.cleanup_expired() == 1 and not directory.exists()


def test_complete_revalidates_after_preparation_and_preserves_chunks_on_trash(harness, monkeypatch):
    database, paths, sessions, purge = harness
    record, _ = _session(harness)
    other, _ = _session(harness, "other")
    uploads, upload, directory = _upload(harness, record)
    uploads.write_chunk(upload["id"], 0, io.BytesIO(b"1234567890"))
    prepared = uploads.artifacts.prepare_registration
    mutations_done = threading.Event()
    outcomes = []

    def mutate():
        try:
            settings = WorkspaceSettingsService(database)
            revision = settings.get(other.id, "text")["revision"]
            settings.update(other.id, "text", revision, {"max_sentence_length": 333})
            sessions.trash(record.id, record.revision)
        except Exception as error:
            outcomes.append(error)
        finally:
            mutations_done.set()

    def prepare(path, **kwargs):
        mutator = threading.Thread(target=mutate, daemon=True)
        mutator.start()
        assert mutations_done.wait(3), "prepared registration held the global writer"
        mutator.join(3)
        assert not outcomes
        assert "upload_writer_active" in purge.preview(record.id)["blockers"]
        return prepared(path, **kwargs)

    monkeypatch.setattr(uploads.artifacts, "prepare_registration", prepare)
    with pytest.raises(ValueError, match="trashed or purging"):
        uploads.complete(upload["id"])
    assert (directory / "00000000.part").read_bytes() == b"1234567890"
    assert not list(directory.glob("assembled-*.part")) and not list(paths.uploads.iterdir())
    with database.session() as session:
        assert session.get(UploadSessionRecord, upload["id"]).state == "open"
    preview = purge.preview(record.id)
    assert purge.purge(record.id, preview["revision"], preview["impact_token"])["state"] == "complete"
