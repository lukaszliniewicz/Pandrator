"""Project export bundles preserve branch authority and immutable file provenance."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.application_services import ApplicationServices
from pandrator.web.auth import ALL_SCOPES, BootstrapTokenStore, Principal
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.project_export_bundle_routes import (
    PROJECT_EXPORT_BUNDLE_SCHEMAS,
    project_export_bundle_paths,
    register_project_export_bundle_routes,
)
from pandrator.web.project_export_bundles import (
    ProjectExportBundleCanceled,
    ProjectExportBundleError,
    ProjectExportBundleService,
)
from pandrator.web.project_operations import TranslationProjectOperationService
from pandrator.web.route_context import RouteContext
from pandrator.web.translation_projects import create_branches_in_session, create_project_in_session
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def bundle_case(tmp_path):
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        public_origin="https://pandrator.example",
    )
    services = cast(ApplicationServices, app.extensions["pandrator"]["services"])
    target = services.identity.snapshot(observed_origin="https://pandrator.example/").model_dump(
        mode="json"
    )
    principal = Principal(
        "owner", "owner_session", ALL_SCOPES, None, "loopback", target["instance_id"]
    )
    source = services.sessions.create(
        "English source",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
    )
    source_dir = services.paths.sessions / source.storage_key
    source_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = source_dir / "correction.srt"
    checkpoint_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    checkpoint = services.artifacts.register(
        checkpoint_path,
        kind="srt",
        role="correction",
        session_id=source.id,
        metadata={"language": "en"},
    )
    with services.database.immediate_session() as db:
        project = create_project_in_session(
            db,
            source.id,
            checkpoint.id,
            "My Project",
            source.revision,
            paths=services.paths,
        )["project"]
    with services.database.immediate_session() as db:
        project = create_branches_in_session(
            db,
            project["id"],
            project["revision"],
            [{"target_language": language} for language in ("ja", "pl")],
            session_forks=services.session_forks,
            paths=services.paths,
            created_directories=[],
        )["project"]
    client = app.test_client()
    guards = ApiGuards(app, services, testing=True, script_policy="'self'")
    if not any(rule.rule.endswith("/exports/manifest") for rule in app.url_map.iter_rules()):
        register_project_export_bundle_routes(app, RouteContext(services, guards, Path("/tmp")))
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}).get_json()[
        "csrf_token"
    ]
    yield SimpleNamespace(
        app=app,
        client=client,
        headers={"X-CSRF-Token": csrf},
        services=services,
        database=services.database,
        principal=principal,
        target=target,
        source=source,
        checkpoint=checkpoint,
        project=project,
        branches=project["branches"],
        operations=TranslationProjectOperationService(services),
        bundles=ProjectExportBundleService(services),
        root=tmp_path,
    )
    services.database.dispose()


def _translation(case, branch):
    with case.database.session() as db:
        session_record = db.get(m.SessionRecord, branch["session_id"])
    directory = case.services.paths.sessions / session_record.storage_key
    path = directory / "translation.srt"
    path.write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\nTranslated {branch['target_language']}.\n",
        encoding="utf-8",
    )
    return case.services.artifacts.register(
        path,
        kind="srt",
        role="translation",
        session_id=branch["session_id"],
        parent_ids=[branch["source_checkpoint_artifact_id"]],
        metadata={
            "language": branch["target_language"],
            "source_artifact_id": branch["source_checkpoint_artifact_id"],
        },
    )


def _claim_bundle_job(case, job_id, lease_generation=1):
    with case.database.immediate_session() as db:
        job = db.get(m.Job, job_id)
        job.status = "running"
        job.lease_owner = "bundle-test-worker"
        job.lease_generation = lease_generation
        job.lease_expires_at = m.utcnow()
        payload = dict(job.payload_json)
        payload["_job_id"] = job.id
        payload["_lease_generation"] = lease_generation
    return job_id, payload


def _finish_job(case, job_id, result, lease_generation=1):
    with case.database.immediate_session() as db:
        job = db.get(m.Job, job_id)
        job.status = "running"
        job.lease_owner = "bundle-test-worker"
        job.lease_generation = lease_generation
        job.lease_expires_at = m.utcnow()
    case.services.jobs.complete(
        job_id,
        "bundle-test-worker",
        result,
        lease_generation=lease_generation,
    )


def _start_export(case, branches, *, execute=True, operation_key="bundle-export-preview"):
    translations = {branch["id"]: _translation(case, branch) for branch in branches}
    operation = case.operations.preview(
        case.project["id"],
        [branch["id"] for branch in branches],
        expected_project_revision=case.project["revision"],
        action="export",
        export_kind="subtitles",
        principal=case.principal,
        target_identity=case.target,
        idempotency_key=operation_key,
    )
    if not execute:
        return operation, translations
    confirmations = sorted(
        {
            item
            for child in operation["children"]
            for item in child.get("preview", {}).get("required_confirmations", [])
        }
    )
    started = case.operations.execute(
        operation["id"],
        operation["preview_digest"],
        confirmations,
        principal=case.principal,
        target_identity=case.target,
        idempotency_key=f"execute-{operation_key}",
    )
    return started, translations


def _complete_exports(case, branches=None, *, fail_branch_ids=(), operation_key="bundle-export"):
    branches = branches or case.branches
    operation, translations = _start_export(case, branches, operation_key=operation_key)
    for child, branch in zip(operation["children"], branches, strict=True):
        if branch["id"] in fail_branch_ids:
            _finish_job(case, child["job_id"], {"artifact_ids": []})
            continue
        with case.database.session() as db:
            job = db.get(m.Job, child["job_id"])
            settings = job.payload_json["settings"]
            session_record = db.get(m.SessionRecord, branch["session_id"])
        directory = case.services.paths.sessions / session_record.storage_key
        output_path = directory / "export.srt"
        output_path.write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\nExport {branch['target_language']}.\n",
            encoding="utf-8",
        )
        exported = case.services.artifacts.register(
            output_path,
            kind="srt",
            role="export_subtitle_translation",
            session_id=branch["session_id"],
            parent_ids=[translations[branch["id"]].id],
            settings=settings,
            metadata={
                "language": branch["target_language"],
                "source_artifact_id": translations[branch["id"]].id,
            },
        )
        _finish_job(case, child["job_id"], {"artifact_ids": [exported.id]})
    return case.operations.get(
        operation["id"], principal=case.principal, target_identity=case.target
    )


def _bundle_job(case, body):
    with case.database.session() as db:
        return db.get(m.Job, body["job_id"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode bits are not Windows ACLs")
def test_published_bundle_directory_rejects_group_or_world_access_on_posix(bundle_case):
    case = bundle_case
    published = case.root / "unsafe-directory-mode"
    published.mkdir()
    original_mode = stat.S_IMODE(published.stat().st_mode)
    try:
        published.chmod(0o755)
        with pytest.raises(ProjectExportBundleError, match="not private"):
            case.bundles._verify_published_directory(
                published,
                b"",
                [],
                cancel_event=None,
            )
    finally:
        published.chmod(original_mode)


def test_manifest_and_bundle_keep_language_exports_separate_and_use_no_generation_run(
    bundle_case,
):
    case = bundle_case
    operation = _complete_exports(case)
    before = {}
    for child in operation["children"]:
        for item in child["result"]["artifacts"]:
            with case.database.session() as db:
                artifact = db.get(m.Artifact, item["artifact_id"])
                before[artifact.id] = (
                    artifact.content_hash,
                    artifact.size_bytes,
                    artifact.state,
                    artifact.relative_path,
                )

    response = case.bundles.manifest(operation["id"], case.principal, case.target)
    document = response["manifest"]
    assert document["manifest_version"] == 1
    assert document["project_id"] == case.project["id"]
    assert document["project_name"] == "My Project"
    assert document["operation_id"] == operation["id"]
    assert document["export_kind"] == "subtitles"
    assert document["complete"] is True
    assert [row["language"] for row in document["languages"]] == ["ja", "pl"]
    assert response["manifest_digest"] == document["manifest_digest"]
    assert re.fullmatch(r"[0-9a-f]{64}", response["manifest_digest"])
    assert all("relative_path" not in row for row in document["languages"])
    assert "storage_key" not in json.dumps(document)

    request_body, status = case.bundles.request_bundle(
        operation["id"],
        response["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="bundle-request-one",
    )
    assert status == 202
    assert request_body["status"] == "queued"
    job = _bundle_job(case, request_body)
    assert job.kind == "project.exports.bundle"
    assert job.session_id == case.source.id
    assert job.resource_keys_json == [f"project:{case.project['id']}:bundle"]
    assert job.payload_json["input_digest"]
    assert all(
        "relative_path" in item
        for child in job.payload_json["languages"]
        for item in child["files"]
    )

    run_rows_before = 0
    with case.database.session() as db:
        run_rows_before = len(list(db.scalars(select(m.GenerationRun))))
    job_id, worker_payload = _claim_bundle_job(case, job.id)
    result = case.bundles.generate(
        worker_payload, lambda _progress, _detail: None, threading.Event()
    )
    interrupted_response, interrupted_status = case.bundles.request_bundle(
        operation["id"],
        response["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="bundle-request-after-publication",
    )
    assert interrupted_status == 200
    assert interrupted_response["status"] == "ready"
    assert interrupted_response["job_status"] == "running"
    assert interrupted_response["recovered"] is True
    assert interrupted_response["artifact_id"] == result["artifact_id"]
    assert interrupted_response["manifest_artifact_id"] == result["manifest_artifact_id"]
    _finish_job(case, job_id, result)
    cached, cached_status = case.bundles.request_bundle(
        operation["id"],
        response["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="bundle-request-cache",
    )
    assert cached_status == 200
    assert cached["status"] == "ready"
    assert cached["job_id"] == job.id
    assert cached["artifact_id"] == result["artifact_id"]
    assert cached["manifest_artifact_id"] == result["manifest_artifact_id"]

    with case.database.session() as db:
        bundle_artifact = db.get(m.Artifact, result["artifact_id"])
        manifest_artifact = db.get(m.Artifact, result["manifest_artifact_id"])
        assert bundle_artifact.session_id == manifest_artifact.session_id == case.source.id
        for output in (bundle_artifact, manifest_artifact):
            output_metadata = output.metadata_json or {}
            assert output_metadata["project_id"] == case.project["id"]
            assert output_metadata["operation_id"] == operation["id"]
            assert output_metadata["source_operation_id"] == operation["id"]
            assert output_metadata["source_artifact_ids"] == sorted(before)
            parents = set(
                db.scalars(
                    select(m.ArtifactEdge.parent_artifact_id).where(
                        m.ArtifactEdge.child_artifact_id == output.id
                    )
                )
            )
            assert parents == set(before)
        assert len(list(db.scalars(select(m.GenerationRun)))) == run_rows_before
        current_source = {
            item.id: (item.content_hash, item.size_bytes, item.state, item.relative_path)
            for item in db.scalars(select(m.Artifact).where(m.Artifact.id.in_(list(before))))
        }
        assert current_source == before
        bundle_path = case.services.paths.managed_path(bundle_artifact.relative_path)
        manifest_path = case.services.paths.managed_path(manifest_artifact.relative_path)

    with manifest_path.open(encoding="utf-8") as handle:
        standalone = json.load(handle)
    assert standalone == document
    with zipfile.ZipFile(bundle_path) as archive:
        names = archive.namelist()
        assert names[0] == "manifest.json"
        assert len(names) == len(set(names)) == 3
        assert set(names[1:]) == {
            row["filename"] for language in document["languages"] for row in language["artifacts"]
        }
        assert archive.testzip() is None
        embedded = json.loads(archive.read("manifest.json"))
        assert embedded == document
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644
        for row in document["languages"]:
            artifact_row = row["artifacts"][0]
            assert artifact_row["filename"] == (
                f"my-project-{row['language']}-r{document['project_revision']}-"
                f"{artifact_row['sha256'][:12]}.srt"
            )


def test_pending_and_failed_exports_keep_block_reasons_and_refuse_bundle(bundle_case):
    case = bundle_case
    pending, _translations = _start_export(
        case, [case.branches[0]], execute=False, operation_key="pending-export"
    )
    pending_manifest = case.bundles.manifest(pending["id"], case.principal, case.target)["manifest"]
    assert pending_manifest["complete"] is False
    assert pending_manifest["languages"][0]["state"] == "pending"
    with pytest.raises(ProjectExportBundleError) as pending_error:
        case.bundles.request_bundle(
            pending["id"],
            pending_manifest["manifest_digest"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="pending-bundle-request",
        )
    assert pending_error.value.code == "bundle_incomplete"

    failed = _complete_exports(
        case,
        [case.branches[1]],
        fail_branch_ids={case.branches[1]["id"]},
        operation_key="failed-export",
    )
    failed_manifest = case.bundles.manifest(failed["id"], case.principal, case.target)["manifest"]
    assert failed_manifest["complete"] is False
    assert failed_manifest["languages"][0]["state"] == "failed"
    assert "artifact receipts" in failed_manifest["languages"][0]["reason"]
    with pytest.raises(ProjectExportBundleError) as failed_error:
        case.bundles.request_bundle(
            failed["id"],
            failed_manifest["manifest_digest"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="failed-bundle-request",
        )
    assert failed_error.value.code == "bundle_incomplete"


@pytest.mark.parametrize("mutation", ["stale_revision", "missing", "tampered", "symlink"])
def test_manifest_blocks_stale_or_untrusted_export_inputs(bundle_case, mutation):
    case = bundle_case
    operation = _complete_exports(case, [case.branches[0]], operation_key=f"mutated-{mutation}")
    ready = case.bundles.manifest(operation["id"], case.principal, case.target)
    output_id = operation["children"][0]["result"]["artifact_id"]
    with case.database.session() as db:
        artifact = db.get(m.Artifact, output_id)
        path = case.services.paths.managed_path(artifact.relative_path)
    if mutation == "stale_revision":
        with case.database.session() as db:
            project = db.get(m.TranslationProject, case.project["id"])
            project.revision += 1
    elif mutation == "missing":
        path.unlink()
    elif mutation == "tampered":
        path.write_text("modified after export registration\n", encoding="utf-8")
    else:
        outside = case.root.parent / f"{case.root.name}-outside-export.srt"
        outside.write_text("external\n", encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"symlink creation unavailable on this platform: {error}")

    current = case.bundles.manifest(operation["id"], case.principal, case.target)["manifest"]
    assert current["complete"] is False
    assert current["languages"][0]["state"] == "blocked"
    assert current["languages"][0]["reason"]
    assert "relative_path" not in json.dumps(current)
    with pytest.raises(ProjectExportBundleError) as stale:
        case.bundles.request_bundle(
            operation["id"],
            ready["manifest_digest"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key=f"reject-mutated-{mutation}",
        )
    assert stale.value.code == "bundle_incomplete"


def test_exact_replay_and_different_key_dedupe_reuse_job_then_ready_cache(bundle_case):
    case = bundle_case
    operation = _complete_exports(case, operation_key="dedupe-export")
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    first, status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="dedupe-request-one",
    )
    replay, replay_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="dedupe-request-one",
    )
    same_inputs, same_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="dedupe-request-two",
    )
    assert status == replay_status == same_status == 202
    assert first["job_id"] == replay["job_id"] == same_inputs["job_id"]
    job = _bundle_job(case, first)
    job_id, worker_payload = _claim_bundle_job(case, job.id)
    result = case.bundles.generate(
        worker_payload, lambda _progress, _detail: None, threading.Event()
    )
    _finish_job(case, job_id, result)
    ready_replay, ready_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="dedupe-request-one",
    )
    cached, cached_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="dedupe-request-three",
    )
    assert ready_status == cached_status == 200
    assert ready_replay["status"] == cached["status"] == "ready"
    assert ready_replay["job_id"] == cached["job_id"] == job.id
    with case.database.session() as db:
        matching_jobs = list(
            db.scalars(
                select(m.Job).where(
                    m.Job.kind == "project.exports.bundle",
                    m.Job.session_id == case.source.id,
                )
            )
        )
    assert len(matching_jobs) == 1


def test_active_job_with_forged_publication_receipt_never_returns_ready(bundle_case):
    case = bundle_case
    operation = _complete_exports(case, [case.branches[0]], operation_key="forged-receipt-export")
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    queued, status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="forged-receipt-request",
    )
    assert status == 202
    job = _bundle_job(case, queued)
    _job_id, _worker_payload = _claim_bundle_job(case, job.id)
    with case.database.immediate_session() as db:
        running_job = db.get(m.Job, job.id)
        running_job.result_json = {
            "publication_receipt": {"schema_version": 1},
            "artifact_id": "unverified-bundle-id",
            "manifest_artifact_id": "unverified-manifest-id",
            "sha256": "f" * 64,
            "manifest_sha256": "e" * 64,
        }

    replayed, replay_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="forged-receipt-request",
    )
    deduped, deduped_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="forged-receipt-second-key",
    )
    assert replay_status == deduped_status == 202
    for response in (replayed, deduped):
        assert response["status"] == response["job_status"] == "running"
        assert response["job_id"] == job.id
        assert "artifact_id" not in response
        assert "manifest_artifact_id" not in response
        assert "content_url" not in response
        assert "manifest_content_url" not in response


def test_deep_completed_retry_chain_keeps_receipts_and_can_bundle(bundle_case):
    case = bundle_case
    operation = _complete_exports(case, [case.branches[0]], operation_key="deep-retry-original")
    source_ids = [item["artifact_id"] for item in operation["children"][0]["result"]["artifacts"]]
    with case.database.session() as db:
        source_before = {
            artifact.id: (
                artifact.content_hash,
                artifact.size_bytes,
                artifact.state,
                artifact.relative_path,
            )
            for artifact in db.scalars(select(m.Artifact).where(m.Artifact.id.in_(source_ids)))
        }

    original_child = operation["children"][0]
    chain = original_child
    for _attempt in range(5):
        wrapper = {
            key: value
            for key, value in original_child.items()
            if key not in {"result", "job_id", "generation_run_id", "progress", "error"}
        }
        wrapper["state"] = "existing"
        wrapper["retained"] = chain
        chain = wrapper
    with case.database.immediate_session() as db:
        stored_operation = db.get(m.TranslationProjectOperation, operation["id"])
        stored_operation.children_json = [chain]
    operation = case.operations.get(
        operation["id"], principal=case.principal, target_identity=case.target
    )

    node = operation["children"][0]
    depth = 0
    while isinstance(node.get("retained"), dict):
        node = node["retained"]
        depth += 1
    assert depth >= 5
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    assert manifest["manifest"]["complete"] is True
    assert {
        item["artifact_id"] for item in manifest["manifest"]["languages"][0]["artifacts"]
    } == set(source_ids)

    request, status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="deep-retry-bundle-request",
    )
    assert status == 202
    bundle_job = _bundle_job(case, request)
    job_id, worker_payload = _claim_bundle_job(case, bundle_job.id)
    result = case.bundles.generate(
        worker_payload,
        lambda _progress, _detail: None,
        threading.Event(),
    )
    _finish_job(case, job_id, result)
    assert result["artifact_id"]
    assert result["manifest_artifact_id"]
    with case.database.session() as db:
        source_after = {
            artifact.id: (
                artifact.content_hash,
                artifact.size_bytes,
                artifact.state,
                artifact.relative_path,
            )
            for artifact in db.scalars(select(m.Artifact).where(m.Artifact.id.in_(source_ids)))
        }
    assert source_after == source_before


def test_cancellation_and_registration_failure_remove_staging_and_preserve_source(bundle_case):
    case = bundle_case
    operation = _complete_exports(case, [case.branches[0]], operation_key="rollback-export")
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    request, _status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="rollback-bundle-request",
    )
    job = _bundle_job(case, request)
    _job_id, worker_payload = _claim_bundle_job(case, job.id)
    source_ids = [operation["children"][0]["result"]["artifact_id"]]
    with case.database.session() as db:
        source_snapshot = {
            artifact.id: (artifact.content_hash, artifact.size_bytes, artifact.state)
            for artifact in db.scalars(select(m.Artifact).where(m.Artifact.id.in_(source_ids)))
        }

    canceled = threading.Event()

    def cancel_after_first_copy(_progress, detail):
        if detail and detail.startswith("Packing language export"):
            canceled.set()

    with pytest.raises(ProjectExportBundleCanceled):
        case.bundles.generate(worker_payload, cancel_after_first_copy, canceled)
    output_root = case.services.paths.artifacts / "project-exports" / case.project["id"]
    assert not list(output_root.glob(".bundle-stage-*"))

    with patch.object(
        case.services.artifacts,
        "register_in_session",
        side_effect=RuntimeError("registration failed"),
    ):
        with pytest.raises(RuntimeError, match="registration failed"):
            case.bundles.generate(
                worker_payload, lambda _progress, _detail: None, threading.Event()
            )
    assert not list(output_root.glob(".bundle-stage-*"))
    assert not [path for path in output_root.iterdir() if path.is_dir()]
    with case.database.session() as db:
        current_source = {
            artifact.id: (artifact.content_hash, artifact.size_bytes, artifact.state)
            for artifact in db.scalars(select(m.Artifact).where(m.Artifact.id.in_(source_ids)))
        }
        assert current_source == source_snapshot
        assert not list(
            db.scalars(
                select(m.Artifact).where(
                    m.Artifact.role.in_(("project_export_bundle", "project_export_manifest"))
                )
            )
        )


def test_generation_rechecks_export_state_after_job_request(bundle_case):
    case = bundle_case
    operation = _complete_exports(case, [case.branches[0]], operation_key="state-recheck-export")
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    request, _status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="state-recheck-request",
    )
    job = _bundle_job(case, request)
    _job_id, worker_payload = _claim_bundle_job(case, job.id)
    output_id = operation["children"][0]["result"]["artifact_id"]
    with case.database.session() as db:
        db.get(m.Artifact, output_id).state = "stale"

    with pytest.raises(ProjectExportBundleError) as error:
        case.bundles.generate(worker_payload, lambda _progress, _detail: None, threading.Event())
    assert error.value.code == "bundle_inputs_changed"
    output_root = case.services.paths.artifacts / "project-exports" / case.project["id"]
    assert not list(output_root.glob(".bundle-stage-*"))
    assert not output_root.exists() or not [path for path in output_root.iterdir() if path.is_dir()]


def test_process_death_after_atomic_publish_recovers_one_pair_and_job_receipt(bundle_case):
    class SimulatedProcessDeath(BaseException):
        pass

    case = bundle_case
    operation = _complete_exports(case, [case.branches[0]], operation_key="crash-recovery-export")
    source_id = operation["children"][0]["result"]["artifacts"][0]["artifact_id"]
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    request, status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="crash-recovery-bundle",
    )
    assert status == 202
    job = _bundle_job(case, request)
    job_id, worker_payload = _claim_bundle_job(case, job.id, lease_generation=1)

    with case.database.session() as db:
        source_before = db.get(m.Artifact, source_id)
        source_snapshot = (
            source_before.content_hash,
            source_before.size_bytes,
            source_before.state,
            source_before.relative_path,
        )
    with patch.object(
        case.services.artifacts,
        "register_in_session",
        side_effect=SimulatedProcessDeath(),
    ):
        with pytest.raises(SimulatedProcessDeath):
            case.bundles.generate(
                worker_payload, lambda _progress, _detail: None, threading.Event()
            )

    output_root = case.services.paths.artifacts / "project-exports" / case.project["id"]
    publication = output_root / job.payload_json["input_digest"]
    assert sorted(path.name for path in publication.iterdir()) == ["bundle.zip", "manifest.json"]
    with case.database.session() as db:
        assert not list(
            db.scalars(
                select(m.Artifact).where(
                    m.Artifact.role.in_(("project_export_bundle", "project_export_manifest"))
                )
            )
        )
        interrupted_job = db.get(m.Job, job_id)
        assert interrupted_job.status == "running"
        assert interrupted_job.result_json is None

    _retried_job_id, retry_payload = _claim_bundle_job(case, job_id, lease_generation=2)
    result = case.bundles.generate(
        retry_payload, lambda _progress, _detail: None, threading.Event()
    )
    assert result["publication_receipt"]["job_id"] == job_id
    recovered_again = case.bundles.generate(
        retry_payload, lambda _progress, _detail: None, threading.Event()
    )
    assert recovered_again["artifact_id"] == result["artifact_id"]
    assert recovered_again["manifest_artifact_id"] == result["manifest_artifact_id"]
    with case.database.immediate_session() as db:
        retrying_job = db.get(m.Job, job_id)
        retrying_job.attempts = retrying_job.max_attempts
    case.services.jobs.fail(
        job_id,
        "bundle-test-worker",
        "simulated_post_publication_failure",
        "Simulated worker failure after publication receipt commit.",
        lease_generation=2,
    )
    terminal_recovery, terminal_status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="crash-recovery-after-terminal-failure",
    )
    assert terminal_status == 200
    assert terminal_recovery["status"] == "ready"
    assert terminal_recovery["job_status"] == "failed"
    assert terminal_recovery["recovered"] is True

    with case.database.session() as db:
        outputs = list(
            db.scalars(
                select(m.Artifact).where(
                    m.Artifact.role.in_(("project_export_bundle", "project_export_manifest"))
                )
            )
        )
        completed_job = db.get(m.Job, job_id)
        source_after = db.get(m.Artifact, source_id)
        assert completed_job.status == "failed"
        assert (
            completed_job.result_json["publication_receipt"]["input_digest"]
            == job.payload_json["input_digest"]
        )
        assert len(outputs) == 2
        assert (
            source_after.content_hash,
            source_after.size_bytes,
            source_after.state,
            source_after.relative_path,
        ) == source_snapshot


def test_authenticated_routes_and_openapi_fragment(bundle_case):
    case = bundle_case
    for name, model in PROJECT_EXPORT_BUNDLE_SCHEMAS.items():
        assert model.model_json_schema()["additionalProperties"] is False, name
    paths = project_export_bundle_paths()
    assert len(paths) == 2
    assert "/api/v1/translation-project-operations/{operationId}/exports/manifest" in paths
    assert "/api/v1/translation-project-operations/{operationId}/exports/bundle" in paths

    _translation(case, case.branches[0])
    url = f"/api/v1/translation-projects/{case.project['id']}/operations/preview"
    created = case.client.post(
        url,
        json={
            "selected_branch_ids": [case.branches[0]["id"]],
            "expected_project_revision": case.project["revision"],
            "action": "export",
            "export_kind": "subtitles",
        },
        headers={**case.headers, "Idempotency-Key": "route-export-preview"},
    )
    assert created.status_code == 201, created.get_json()
    operation_id = created.get_json()["id"]
    manifest_response = case.client.get(
        f"/api/v1/translation-project-operations/{operation_id}/exports/manifest"
    )
    assert manifest_response.status_code == 200
    document = manifest_response.get_json()["manifest"]
    assert document["complete"] is False
    rejected = case.client.post(
        f"/api/v1/translation-project-operations/{operation_id}/exports/bundle",
        json={"expected_manifest_digest": document["manifest_digest"]},
        headers={**case.headers, "Idempotency-Key": "route-incomplete-bundle"},
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "bundle_incomplete"
