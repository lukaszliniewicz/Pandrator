"""Bulk operation boundaries recheck current accounts, roots and worker leases."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace

import pytest
from sqlalchemy import delete, func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied, ProjectNotFound
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.execution_actor import (
    actor_from_principal,
    actor_to_storage,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.auth import Principal
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.model_base import new_id
from pandrator.web.project_export_bundles import ProjectExportBundleError
from pandrator.web.project_operations import ProjectOperationError
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.translation_projects import create_branches_in_session, create_project_in_session
from tests.test_project_export_bundles import (
    _claim_bundle_job,
    _complete_exports,
    _generate,
)
from tests.test_project_export_bundles import (
    bundle_case as _bundle_case,
)

bundle_case = _bundle_case

SCOPES = frozenset({"app.read", "app.write", "app.run", "app.cancel"})
DENIALS = (AccessDenied, ProjectNotFound, ProjectOperationError, ProjectExportBundleError)


def _person(case, *, workspace_id=None, admin=False):
    user_id, token_id = new_id(), new_id()
    subject = f"bulk:{token_id}"
    with case.database.immediate_session() as db:
        db.add(User(id=user_id, display_name="Bulk test", is_instance_admin=admin))
        db.flush()
        db.add(UserCredential(user_id=user_id, login_name=user_id, auth_revision=1))
        db.add(
            m.ApiToken(
                id=token_id,
                label="Bulk test",
                token_hash=hashlib.sha256(token_id.encode()).hexdigest(),
                token_prefix=token_id[:12],
                subject=subject,
                scopes_json=sorted(SCOPES),
                principal_kind="api_token",
                user_id=user_id,
                user_auth_revision=1,
            )
        )
        db.flush()
        if workspace_id:
            db.add(ApiTokenWorkspaceGrant(token_id=token_id, workspace_id=workspace_id))
    return Principal(
        subject,
        "api_token",
        SCOPES,
        token_id,
        "loopback",
        case.target["instance_id"],
        user_id=user_id,
        auth_revision=1,
    )


@pytest.fixture
def team_case(bundle_case):
    case = bundle_case
    workspace_id = new_id()
    with case.database.immediate_session() as db:
        db.add(Workspace(id=workspace_id, kind="team", name="Bulk team"))
        db.flush()
        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id, user_id=case.principal.user_id, role="owner"
            )
        )
    source = case.services.sessions.create(
        "Team subtitles",
        workflow_kind="subtitles",
        source_language="en",
        included_stages=["correct", "translate", "export"],
        workspace_id=workspace_id,
    )
    directory = case.services.paths.sessions / source.storage_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "corrected.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
    checkpoint = case.services.artifacts.register(
        path, kind="srt", role="correction", session_id=source.id, metadata={"language": "en"}
    )
    with case.database.immediate_session() as db:
        project = create_project_in_session(
            db,
            source.id,
            checkpoint.id,
            "Team languages",
            source.revision,
            paths=case.services.paths,
        )["project"]
    with case.database.immediate_session() as db:
        project = create_branches_in_session(
            db,
            project["id"],
            project["revision"],
            [{"target_language": "pl"}],
            session_forks=case.services.session_forks,
            paths=case.services.paths,
            created_directories=[],
        )["project"]
    case.source, case.checkpoint, case.project, case.branches = (
        source,
        checkpoint,
        project,
        project["branches"],
    )
    case.workspace_id = workspace_id
    case.principal = _person(case, workspace_id=workspace_id)
    with case.database.immediate_session() as db:
        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id, user_id=case.principal.user_id, role="member"
            )
        )
    with execution_actor_scope(actor_from_principal(case.principal)):
        yield case


def _preview(case, key="authority-preview"):
    return case.operations.preview(
        case.project["id"],
        [b["id"] for b in case.branches],
        expected_project_revision=case.project["revision"],
        action="export",
        export_kind="subtitles",
        principal=case.principal,
        target_identity=case.target,
        idempotency_key=key,
    )


def _queue(case):
    operation = _complete_exports(case)
    manifest = case.bundles.manifest(operation["id"], case.principal, case.target)
    body, status = case.bundles.request_bundle(
        operation["id"],
        manifest["manifest_digest"],
        principal=case.principal,
        target_identity=case.target,
        idempotency_key="authority-bundle",
    )
    assert status == 202
    return operation, manifest, body


def _counts(case):
    with case.database.session() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (m.ApiIdempotency, m.Job, m.Artifact, m.TranslationProjectOperation)
        )


def _remove(case):
    with case.database.immediate_session() as db:
        db.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.workspace_id,
                WorkspaceMembership.user_id == case.principal.user_id,
            )
        )


def _assert_no_output(case):
    with case.database.session() as db:
        assert not list(
            db.scalars(
                select(m.Artifact.id).where(
                    m.Artifact.role.in_(("project_export_bundle", "project_export_manifest"))
                )
            )
        )
    root = case.services.paths.artifacts / "project-exports" / case.project["id"]
    assert not root.exists() or not list(root.iterdir())


def test_team_member_without_project_rows_can_execute_and_publish(team_case):
    case = team_case
    with case.database.session() as db:
        assert (
            db.scalar(
                select(ProjectMembership).where(ProjectMembership.user_id == case.principal.user_id)
            )
            is None
        )
    _operation, _manifest, body = _queue(case)
    _job_id, payload = _claim_bundle_job(case, body["job_id"])
    result = _generate(case, payload, lambda *_args: None, threading.Event())
    assert result["artifact_id"] and result["manifest_artifact_id"]


@pytest.mark.parametrize("role", ["admin", "manager", "editor", "reviewer", "viewer"])
def test_unrelated_admin_and_project_only_roles_cannot_prepare_or_run(team_case, role):
    case = team_case
    operation, manifest, _body = _queue(case)
    original = case.principal
    outsider = _person(case, workspace_id=case.workspace_id, admin=role == "admin")
    if role != "admin":
        with case.database.immediate_session() as db:
            root_id = db.scalar(
                select(m.TranslationProject.project_root_id).where(
                    m.TranslationProject.id == case.project["id"]
                )
            )
            db.add(ProjectMembership(project_id=root_id, user_id=outsider.user_id, role=role))
    case.principal = outsider
    before = _counts(case)
    with pytest.raises(DENIALS):
        _preview(case)
    with pytest.raises(DENIALS):
        case.operations.execute(
            operation["id"],
            operation["preview_digest"],
            [],
            principal=outsider,
            target_identity=case.target,
            idempotency_key="outsider-execute",
        )
    with pytest.raises(DENIALS):
        case.bundles.request_bundle(
            operation["id"],
            manifest["manifest_digest"],
            principal=outsider,
            target_identity=case.target,
            idempotency_key="outsider-bundle",
        )
    assert _counts(case) == before
    case.principal = original


def test_membership_removal_denies_all_operations_and_same_key_replay(team_case):
    case = team_case
    operation, manifest, _body = _queue(case)
    _remove(case)
    before = _counts(case)
    calls = [
        lambda: _preview(case, "bundle-export"),
        lambda: case.operations.get(
            operation["id"], principal=case.principal, target_identity=case.target
        ),
        lambda: case.operations.retry_preview(
            operation["id"],
            principal=case.principal,
            target_identity=case.target,
            expected_project_revision=case.project["revision"],
            idempotency_key="removed-retry",
        ),
        lambda: case.operations.cancel(
            operation["id"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="removed-cancel",
        ),
        lambda: case.operations.execute(
            operation["id"],
            operation["preview_digest"],
            [],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="execute-bundle-export",
        ),
        lambda: case.bundles.manifest(operation["id"], case.principal, case.target),
        lambda: case.bundles.request_bundle(
            operation["id"],
            manifest["manifest_digest"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="authority-bundle",
        ),
    ]
    for call in calls:
        with pytest.raises(DENIALS):
            call()
        assert _counts(case) == before
    _assert_no_output(case)


@pytest.mark.parametrize("change", ["revoked", "narrowed", "resources"])
def test_changed_token_denies_preview_replay(team_case, change):
    case = team_case
    _preview(case)
    before = _counts(case)
    with case.database.immediate_session() as db:
        token = db.get(m.ApiToken, case.principal.token_id)
        if change == "revoked":
            token.revoked_at = m.utcnow()
        elif change == "narrowed":
            token.scopes_json = ["app.read"]
        else:
            db.execute(
                delete(ApiTokenWorkspaceGrant).where(ApiTokenWorkspaceGrant.token_id == token.id)
            )
    with pytest.raises(DENIALS):
        _preview(case)
    assert _counts(case) == before


@pytest.mark.parametrize("change", ["membership", "revoked", "narrowed", "resources"])
def test_worker_authority_removed_after_staging_publishes_nothing(team_case, change):
    case = team_case
    _operation, _manifest, body = _queue(case)
    _job_id, payload = _claim_bundle_job(case, body["job_id"])
    changed = False

    def progress(amount, _detail):
        nonlocal changed
        if amount < 0.9 or changed:
            return
        changed = True
        if change == "membership":
            _remove(case)
        else:
            with case.database.immediate_session() as db:
                token = db.get(m.ApiToken, case.principal.token_id)
                if change == "revoked":
                    token.revoked_at = m.utcnow()
                elif change == "narrowed":
                    token.scopes_json = ["app.read"]
                else:
                    db.execute(
                        delete(ApiTokenWorkspaceGrant).where(
                            ApiTokenWorkspaceGrant.token_id == token.id
                        )
                    )

    with pytest.raises((PermissionError, ProjectNotFound)):
        _generate(case, payload, progress, threading.Event())
    assert changed
    _assert_no_output(case)


def test_captured_browser_actor_can_publish_after_logout(bundle_case):
    case = bundle_case
    _operation, _manifest, body = _queue(case)
    _job_id, payload = _claim_bundle_job(case, body["job_id"])
    with case.database.immediate_session() as db:
        db.get(BrowserAuthSession, case.principal.browser_session_id).revoked_at = m.utcnow()
    result = _generate(case, payload, lambda *_args: None, threading.Event())
    assert result["artifact_id"]


@pytest.mark.parametrize("lease", [None, "wrong-owner", "wrong-job"])
def test_running_bundle_requires_actual_active_lease(bundle_case, lease):
    case = bundle_case
    _operation, _manifest, body = _queue(case)
    _job_id, payload = _claim_bundle_job(case, body["job_id"])
    value: JobLease | None = (
        None
        if lease is None
        else {
            "job_id": body["job_id"] if lease == "wrong-owner" else "unrelated-job",
            "owner_id": "impostor",
            "lease_generation": 1,
        }
    )
    token = current_job_lease.set(value)
    try:
        with pytest.raises(PermissionError):
            case.bundles.generate(payload, lambda *_args: None, threading.Event())
    finally:
        current_job_lease.reset(token)
    _assert_no_output(case)


@pytest.mark.parametrize("change", ["missing", "malformed"])
def test_preview_requires_actual_user_actor(bundle_case, change):
    case = bundle_case
    case.principal = replace(
        case.principal, user_id=None, auth_revision=None if change == "missing" else 1
    )
    before = _counts(case)
    with pytest.raises(AccessDenied):
        _preview(case)
    assert _counts(case) == before


def test_operation_owner_downgraded_to_project_reader_retains_only_read(team_case):
    case = team_case
    operation, manifest, _body = _queue(case)
    _remove(case)
    with case.database.immediate_session() as db:
        root_id = db.scalar(
            select(m.TranslationProject.project_root_id).where(
                m.TranslationProject.id == case.project["id"]
            )
        )
        db.add(ProjectMembership(project_id=root_id, user_id=case.principal.user_id, role="viewer"))
    assert (
        case.operations.get(operation["id"], principal=case.principal, target_identity=case.target)[
            "id"
        ]
        == operation["id"]
    )
    assert (
        case.bundles.manifest(operation["id"], case.principal, case.target)["manifest_digest"]
        == manifest["manifest_digest"]
    )
    before = _counts(case)
    calls = [
        lambda: case.operations.execute(
            operation["id"],
            operation["preview_digest"],
            [],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="execute-bundle-export",
        ),
        lambda: case.operations.cancel(
            operation["id"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="reader-cancel",
        ),
        lambda: case.operations.retry_preview(
            operation["id"],
            principal=case.principal,
            target_identity=case.target,
            expected_project_revision=case.project["revision"],
            idempotency_key="reader-retry",
        ),
        lambda: case.bundles.request_bundle(
            operation["id"],
            manifest["manifest_digest"],
            principal=case.principal,
            target_identity=case.target,
            idempotency_key="authority-bundle",
        ),
    ]
    for call in calls:
        with pytest.raises(AccessDenied):
            call()
        assert _counts(case) == before


@pytest.mark.parametrize("action", ["execute", "bundle", "cancel"])
def test_commands_also_require_literal_write_scope(team_case, action):
    case = team_case
    operation, manifest, _body = _queue(case)
    scopes = SCOPES - {"app.write"}
    with case.database.immediate_session() as db:
        db.get(m.ApiToken, case.principal.token_id).scopes_json = sorted(scopes)
    case.principal = replace(case.principal, scopes=scopes)
    before = _counts(case)
    with pytest.raises(AccessDenied):
        if action == "execute":
            case.operations.execute(
                operation["id"],
                operation["preview_digest"],
                [],
                principal=case.principal,
                target_identity=case.target,
                idempotency_key="execute-bundle-export",
            )
        elif action == "bundle":
            case.bundles.request_bundle(
                operation["id"],
                manifest["manifest_digest"],
                principal=case.principal,
                target_identity=case.target,
                idempotency_key="authority-bundle",
            )
        else:
            case.operations.cancel(
                operation["id"],
                principal=case.principal,
                target_identity=case.target,
                idempotency_key="write-less-cancel",
            )
    assert _counts(case) == before


@pytest.mark.parametrize("actor_state", ["missing-context", "missing-record"])
def test_bundle_worker_requires_provable_captured_actor(bundle_case, actor_state):
    case = bundle_case
    _operation, _manifest, body = _queue(case)
    _job_id, payload = _claim_bundle_job(case, body["job_id"])
    if actor_state == "missing-record":
        with case.database.immediate_session() as db:
            db.execute(delete(m.JobActorRecord).where(m.JobActorRecord.job_id == body["job_id"]))
    lease = current_job_lease.set(
        {"job_id": body["job_id"], "owner_id": "bundle-test-worker", "lease_generation": 1}
    )
    try:
        with execution_actor_scope(
            None if actor_state == "missing-context" else actor_from_principal(case.principal)
        ):
            with pytest.raises(PermissionError):
                case.bundles.generate(payload, lambda *_args: None, threading.Event())
    finally:
        current_job_lease.reset(lease)
    _assert_no_output(case)


@pytest.mark.parametrize("ambient", ["missing", "different"])
def test_direct_bulk_enqueue_captures_required_principal_and_restores_ambient(team_case, ambient):
    case = team_case
    outsider = _person(case, workspace_id=case.workspace_id)
    outer = None if ambient == "missing" else actor_from_principal(outsider)
    expected = actor_to_storage(actor_from_principal(case.principal))
    with execution_actor_scope(outer):
        _operation, _manifest, body = _queue(case)
        assert current_execution_actor.get() == outer
        with case.database.session() as db:
            jobs = list(db.scalars(select(m.Job).where(m.Job.project_id == case.source.project_id)))
            assert len(jobs) == 2
            assert body["job_id"] in {job.id for job in jobs}
            for job in jobs:
                captured = db.get(m.JobActorRecord, job.id)
                assert captured is not None
                assert {
                    "user_id": captured.user_id,
                    "auth_revision": captured.auth_revision,
                    "context_json": captured.context_json,
                } == expected
