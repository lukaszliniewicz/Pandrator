"""Shared completed-export receipts through real HTTP/MCP and bundle workers."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import (
    UserActor,
    actor_from_principal,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.jobs import Worker
from pandrator.web.project_export_bundles import ProjectExportBundleService
from pandrator.web.project_operations import TranslationProjectOperationService
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.translation_projects import create_branches_in_session, create_project_in_session
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.session_branches import (
    GetTranslationProjectExportManifestInput,
    RequestTranslationProjectExportBundleInput,
)
from pandrator_mcp.tools.session_branches import (
    get_translation_project_export_manifest,
    request_translation_project_export_bundle,
)
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_evidence_access_routes import body, headers, team
from tests.test_project_export_bundles import _complete_exports
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_provider_workspace_access import scoped_token

SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
OUTPUT_ROLES = ("project_export_bundle", "project_export_manifest")


def _services(case):
    return case.app.extensions["pandrator"]["services"]


def _exports(case, monkeypatch, *, named=False, personal=False):
    people = team(case, monkeypatch) if named else None
    user_id = people.alice_id if people else case.actor.user_id
    with case.database.snapshot_session() as unit:
        home = unit.scalar(
            select(m.Workspace.id).where(m.Workspace.personal_owner_user_id == user_id)
        )
    workspace = people.workspace_id if people and not personal else home
    _, auth = scoped_token(case, user_id, SCOPES, workspace=workspace)
    services = _services(case)
    target = services.identity.snapshot(observed_origin="http://localhost/").model_dump(mode="json")
    principal = case.auth.resolve_api_token(
        auth["Authorization"].removeprefix("Bearer "),
        network_zone="loopback",
        target_instance_id=target["instance_id"],
    )
    assert principal is not None
    with execution_actor_scope(actor_from_principal(principal)):
        source = services.sessions.create(
            "Shared export source",
            workflow_kind="subtitles",
            source_language="en",
            included_stages=["correct", "translate", "export"],
            workspace_id=workspace,
        )
        directory = services.paths.sessions / source.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "correction.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nShared source.\n", encoding="utf-8")
        checkpoint = services.artifacts.register(
            path, kind="srt", role="correction", session_id=source.id, metadata={"language": "en"}
        )
        with case.database.immediate_session() as unit:
            project = create_project_in_session(
                unit,
                source.id,
                checkpoint.id,
                "Shared language exports",
                source.revision,
                paths=services.paths,
            )["project"]
        with case.database.immediate_session() as unit:
            project = create_branches_in_session(
                unit,
                project["id"],
                project["revision"],
                [{"target_language": language} for language in ("ja", "pl")],
                session_forks=services.session_forks,
                paths=services.paths,
                created_directories=[],
            )["project"]
        fixture = SimpleNamespace(
            services=services,
            database=case.database,
            principal=principal,
            target=target,
            source=source,
            checkpoint=checkpoint,
            project=project,
            branches=project["branches"],
            operations=TranslationProjectOperationService(services),
            people=people,
            auth=auth,
            workspace=workspace,
        )
        fixture.operation = _complete_exports(fixture)
    with case.database.snapshot_session() as unit:
        source_files = list(
            unit.execute(
                select(m.Artifact.id, m.Artifact.relative_path, m.Artifact.content_hash).where(
                    m.Artifact.project_id == source.project_id,
                )
            )
        )
    fixture.original_bytes = {
        row.relative_path: services.paths.managed_path(row.relative_path).read_bytes()
        for row in source_files
    }
    return fixture


def _base(exports):
    return f"/api/v1/translation-project-operations/{exports.operation['id']}/exports"


def _manifest(client, auth, exports):
    return body(client.get(_base(exports) + "/manifest", headers=auth))["manifest"]


def _request(client, auth, exports, manifest, *, key=None, status=202):
    return body(
        client.post(
            _base(exports) + "/bundle",
            json={"expected_manifest_digest": manifest["manifest_digest"]},
            headers=headers(auth, key or str(uuid4())),
        ),
        status,
    )


def _peer(case, exports):
    _, auth = scoped_token(case, exports.people.bob_id, SCOPES, project=exports.source.project_id)
    return case.app.test_client(), auth


def _remove(case, exports, user_id):
    with case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (exports.workspace, user_id)))


def _run(case, receipt, *, effect=None, late=False):
    services = _services(case)
    handlers = services.workflow_handlers.handlers()
    original = handlers["project.exports.bundle"]

    def execute(payload, progress, cancel):
        changed = False

        def report(value, detail=None):
            nonlocal changed
            if effect and not changed and 0 < value < 0.9:
                changed = True
                effect(payload)
            if late and value == 1:
                raise RuntimeError("Synthetic late bundle reporting fault")
            progress(value, detail)

        return original(payload, report, cancel)

    handlers["project.exports.bundle"] = execute
    assert Worker(services.jobs, "shared-bundle-journey", handlers).run_once()
    return services.jobs.get(receipt["job_id"])


def _assert_pair(case, client, auth, exports, result):
    services = _services(case)
    content = client.get(f"/api/v1/artifacts/{result['artifact_id']}/content", headers=auth)
    assert content.status_code == 200
    value = content.get_data()
    assert hashlib.sha256(value).hexdigest() == result["sha256"]
    assert len(value) == result["size_bytes"]
    manifest = client.get(
        f"/api/v1/artifacts/{result['manifest_artifact_id']}/content", headers=auth
    )
    assert manifest.status_code == 200
    assert hashlib.sha256(manifest.get_data()).hexdigest() == result["manifest_sha256"]
    with zipfile.ZipFile(io.BytesIO(value)) as archive:
        document = json.loads(archive.read("manifest.json"))
        assert document == json.loads(manifest.get_data())
        assert len(archive.namelist()) == 3
        assert {child["language"] for child in document["languages"]} == {"ja", "pl"}
        for child in document["languages"]:
            for artifact in child["artifacts"]:
                assert (
                    hashlib.sha256(archive.read(artifact["filename"])).hexdigest()
                    == artifact["sha256"]
                )
    assert all(
        services.paths.managed_path(name).read_bytes() == content
        for name, content in exports.original_bytes.items()
    )
    with case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(m.GenerationRun)) == 0
    return value


def _assert_no_publication(case):
    services = _services(case)
    with case.database.snapshot_session() as unit:
        assert (
            unit.scalar(
                select(func.count())
                .select_from(m.Artifact)
                .where(m.Artifact.role.in_(OUTPUT_ROLES))
            )
            == 0
        )
    assert not list(services.paths.artifacts.glob("project-exports/**/bundle.zip"))
    assert not list(services.paths.artifacts.glob("project-exports/**/.bundle-stage-*"))


def test_default_single_user_bundle_uses_registered_worker_and_actual_downloads(case, monkeypatch):
    exports = _exports(case, monkeypatch)
    client = case.app.test_client()
    manifest = _manifest(client, exports.auth, exports)
    receipt = _request(client, exports.auth, exports, manifest)
    finished = _run(case, receipt)
    assert finished.status == "succeeded", finished.error_message
    _assert_pair(case, client, exports.auth, exports, finished.result_json)
    with case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(m.User)) == 1


def test_member_bundles_peer_exports_after_creator_departure_with_bounded_publication(
    case, monkeypatch
):
    exports = _exports(case, monkeypatch, named=True)
    _remove(case, exports, exports.people.alice_id)
    client, auth = _peer(case, exports)
    statements = []

    def record(_connection, _cursor, statement, parameters, _context, _many):
        if "FROM artifacts" in statement and "JSON_EXTRACT" in statement and "LIMIT" in statement:
            statements.append(parameters)

    event.listen(case.database.engine, "before_cursor_execute", record)
    try:
        manifest = _manifest(client, auth, exports)
        key = str(uuid4())
        receipt = _request(client, auth, exports, manifest, key=key)
        replay = _request(client, auth, exports, manifest, key=key)
        assert all(replay[key] == receipt[key] for key in ("job_id", "status", "manifest_digest"))
        with case.database.snapshot_session() as unit:
            assert (
                unit.scalar(
                    select(func.count())
                    .select_from(m.Job)
                    .where(m.Job.kind == "project.exports.bundle")
                )
                == 1
            )
        finished = _run(case, receipt)
    finally:
        event.remove(case.database.engine, "before_cursor_execute", record)
    assert finished.status == "succeeded", finished.error_message
    _assert_pair(case, client, auth, exports, finished.result_json)
    assert statements and all(tuple(parameters)[-2:] == (3, 0) for parameters in statements)
    with case.database.snapshot_session() as unit:
        saved = unit.get_one(m.JobActorRecord, receipt["job_id"])
        job = unit.get_one(m.Job, receipt["job_id"])
        assert saved.user_id == exports.people.bob_id
        assert job.payload_json["principal_subject"] == exports.principal.subject
    ready = _request(client, auth, exports, manifest, key=key, status=200)
    assert (
        ready["status"] == "ready" and ready["artifact_id"] == finished.result_json["artifact_id"]
    )


class _HttpApplication(ApplicationClient):
    def __init__(self, client, auth):
        self.client, self.auth = client, auth
        self.calls: list[tuple[str, str]] = []

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        method = options.get("method", "GET")
        self.calls.append((method, path))
        auth = dict(self.auth)
        if options.get("idempotency_key"):
            auth["Idempotency-Key"] = options["idempotency_key"]
        response = self.client.open(path, method=method, json=options.get("body"), headers=auth)
        value = response.get_json()
        assert isinstance(value, dict)
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", str(value.get("error")))
        return value


def test_existing_mcp_builders_request_and_replay_actual_member_bundle(case, monkeypatch):
    exports = _exports(case, monkeypatch, named=True)
    _remove(case, exports, exports.people.alice_id)
    client, auth = _peer(case, exports)
    application = _HttpApplication(client, auth)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    manifest = get_translation_project_export_manifest(
        runtime, GetTranslationProjectExportManifestInput(operation_id=exports.operation["id"])
    ).result["manifest"]
    arguments = RequestTranslationProjectExportBundleInput(
        operation_id=exports.operation["id"],
        expected_manifest_digest=manifest["manifest_digest"],
        idempotency_key=str(uuid4()),
    )
    receipt = request_translation_project_export_bundle(runtime, arguments).result
    replay = request_translation_project_export_bundle(runtime, arguments).result
    assert replay["job_id"] == receipt["job_id"]
    finished = _run(case, receipt)
    assert finished.status == "succeeded", finished.error_message
    _assert_pair(case, client, auth, exports, finished.result_json)
    ready = request_translation_project_export_bundle(runtime, arguments).result
    assert (
        ready["status"] == "ready" and ready["artifact_id"] == finished.result_json["artifact_id"]
    )
    assert application.calls[0] == ("GET", _base(exports) + "/manifest")
    assert all(method == "POST" for method, _path in application.calls[1:])


def test_viewer_can_read_shared_manifest_and_bundle_denial_precedes_bad_body(case, monkeypatch):
    exports = _exports(case, monkeypatch, named=True)
    _remove(case, exports, exports.people.bob_id)
    with case.database.immediate_session() as unit:
        unit.add(
            ProjectMembership(
                project_id=exports.source.project_id, user_id=exports.people.bob_id, role="viewer"
            )
        )
    client, auth = _peer(case, exports)
    assert _manifest(client, auth, exports)["complete"]
    response = client.post(_base(exports) + "/bundle", json={"invalid": True}, headers=auth)
    assert response.status_code == 403, response.get_json()
    _assert_no_publication(case)


def test_installation_admin_and_teammate_cannot_read_another_users_private_exports(
    case, monkeypatch
):
    exports = _exports(case, monkeypatch, named=True, personal=True)
    with case.database.immediate_session() as unit:
        unit.execute(
            text(
                "UPDATE translation_project_operations SET preview_json = :bad, children_json = :bad WHERE id = :id"
            ),
            {"bad": "{private-invalid", "id": exports.operation["id"]},
        )
    for client in (exports.people.owner, exports.people.bob):
        assert client.get(_base(exports) + "/manifest").status_code == 404
    response = exports.people.owner.post(
        _base(exports) + "/bundle", json={"invalid": True}, headers=exports.people.oh
    )
    assert response.status_code == 404
    _assert_no_publication(case)


@pytest.mark.parametrize("loss", ["membership", "scope"])
def test_queued_revocation_precedes_any_bundle_file_io(case, monkeypatch, loss):
    exports = _exports(case, monkeypatch, named=True)
    client, auth = _peer(case, exports)
    manifest = _manifest(client, auth, exports)
    receipt = _request(client, auth, exports, manifest)
    with case.database.immediate_session() as unit:
        if loss == "membership":
            unit.delete(
                unit.get_one(m.WorkspaceMembership, (exports.workspace, exports.people.bob_id))
            )
        else:
            actor = unit.get_one(m.JobActorRecord, receipt["job_id"])
            from pandrator.web.accounts.execution_actor import actor_from_storage

            captured = actor_from_storage(actor.user_id, actor.auth_revision, actor.context_json)
            assert type(captured) is UserActor
            unit.get_one(m.ApiToken, captured.token_id).scopes_json = ["app.read"]
    opened = Mock(side_effect=AssertionError("Revoked job opened a bundle source"))
    monkeypatch.setattr(ProjectExportBundleService, "_open_managed_regular_file", opened)
    finished = _run(case, receipt)
    assert finished.status == "failed"
    opened.assert_not_called()
    _assert_no_publication(case)


@pytest.mark.parametrize("loss", ["membership", "lease", "context"])
def test_progress_authority_loss_stops_copy_and_publication(case, monkeypatch, loss):
    exports = _exports(case, monkeypatch, named=True)
    client, auth = _peer(case, exports)
    receipt = _request(client, auth, exports, _manifest(client, auth, exports))
    changed = []

    def revoke(payload):
        changed.append(True)
        if loss == "context":
            current_execution_actor.set(None)
            return
        with case.database.immediate_session() as unit:
            if loss == "membership":
                unit.delete(
                    unit.get_one(m.WorkspaceMembership, (exports.workspace, exports.people.bob_id))
                )
            else:
                job = unit.get_one(m.Job, payload["bundle_job_id"])
                job.lease_owner = "replacement-bundle-worker"
                job.lease_generation += 1

    finished = _run(case, receipt, effect=revoke)
    assert changed == [True] and finished.status != "succeeded"
    _assert_no_publication(case)


@pytest.mark.parametrize("fault", ["late_progress", "uncertain_commit"])
def test_committed_bundle_is_retained_and_replayed_after_late_failure(case, monkeypatch, fault):
    exports = _exports(case, monkeypatch)
    client, auth = case.app.test_client(), exports.auth
    manifest = _manifest(client, auth, exports)
    key = str(uuid4())
    receipt = _request(client, auth, exports, manifest, key=key)
    raised = []
    if fault == "uncertain_commit":
        original = case.database.immediate_session

        @contextmanager
        def uncertain(*args: Any, **options: Any):
            changed = False
            with original(*args, **options) as unit:
                before = unit.scalar(select(m.Job.result_json).where(m.Job.id == receipt["job_id"]))
                yield unit
                after = unit.scalar(select(m.Job.result_json).where(m.Job.id == receipt["job_id"]))
                changed = not (before or {}).get("publication_receipt") and bool(
                    (after or {}).get("publication_receipt")
                )
            if changed and not raised:
                raised.append(True)
                raise RuntimeError("Synthetic uncertain bundle commit")

        monkeypatch.setattr(case.database, "immediate_session", uncertain)
    finished = _run(case, receipt, late=fault == "late_progress")
    assert finished.status == "failed", finished.error_message
    if fault == "uncertain_commit":
        assert raised == [True]
    _assert_pair(case, client, auth, exports, finished.result_json)
    ready = _request(client, auth, exports, manifest, key=key, status=200)
    assert ready["status"] == "ready" and ready["recovered"]
    assert ready["artifact_id"] == finished.result_json["artifact_id"]
