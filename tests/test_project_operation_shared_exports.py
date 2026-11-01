"""Direct backend evidence for shared, read-only export receipts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, text, update

from pandrator.runtime import DataPaths
from pandrator.web.access.contracts import AccessDenied, ProjectAction, ProjectNotFound
from pandrator.web.accounts.auth_contracts import USER_BROWSER_SCOPES
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import UserActor, actor_from_principal
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import AuthService, Principal
from pandrator.web.database import Database
from pandrator.web.models import (
    ApiToken,
    Job,
    TranslationProject,
    TranslationProjectBranch,
    TranslationProjectOperation,
    WorkflowExecutionPlan,
    utcnow,
)
from pandrator.web.project_operations import (
    ProjectOperationError,
    TranslationProjectOperationService,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root

TARGET_IDENTITY = {
    "instance_id": "00000000-0000-4000-8000-000000000001",
    "canonical_origin": "https://pandrator.example",
    "application_version": "0.1.0",
}


class _IdentityRedactor:
    @staticmethod
    def redact_value(value):
        return value


class _NoManagedPath:
    def __init__(self):
        self.calls = []

    def managed_path(self, relative_path):
        self.calls.append(relative_path)
        raise AssertionError("A foreign artifact path was inspected.")


@dataclass(slots=True)
class SharedExportWorld:
    database: Database
    paths: DataPaths
    service: TranslationProjectOperationService
    target_identity: dict[str, str]
    operation_id: str
    creator: Principal
    member: Principal
    viewer: Principal
    creator_user_id: str
    member_user_id: str
    viewer_user_id: str
    workspace_id: str
    root_project_id: str
    project_id: str
    branch_id: str
    branch_session_id: str
    source_session_id: str
    checkpoint_artifact_id: str
    branch_checkpoint_artifact_id: str
    job_id: str
    plan_id: str


def _mint_principal(
    database: Database,
    user_id: str,
    *,
    scopes: frozenset[str],
    resources: CredentialResources,
    label: str,
) -> Principal:
    auth = AuthService(database)
    _token, raw = auth.create_api_token(
        label,
        scopes=scopes,
        user_id=user_id,
        auth_revision=1,
        resource_grants=resources,
    )
    principal = auth.resolve_api_token(
        raw,
        network_zone="loopback",
        target_instance_id=TARGET_IDENTITY["instance_id"],
    )
    assert principal is not None
    assert type(actor_from_principal(principal)) is UserActor
    return principal


def _add_user(session, login_name: str, *, administrator: bool = False) -> User:
    user = User(
        display_name=login_name,
        is_active=True,
        is_instance_admin=administrator,
    )
    session.add(user)
    session.flush()
    session.add(
        UserCredential(
            user_id=user.id,
            login_name=login_name,
            auth_revision=1,
        )
    )
    return user


def _add_job(
    world: SharedExportWorld,
    session_id: str,
    *,
    status: str = "queued",
    result_json: dict | None = None,
) -> str:
    with world.database.session() as db:
        job = Job(
            kind="export.pdf",
            workspace_id=world.workspace_id,
            project_id=world.root_project_id,
            session_id=session_id,
            status=status,
            payload_json={},
            resource_keys_json=[],
            result_json=result_json,
        )
        db.add(job)
        db.flush()
        return job.id


def _add_plan(
    world: SharedExportWorld,
    session_id: str,
    *,
    resulting_job_id: str | None,
) -> str:
    with world.database.session() as db:
        plan = WorkflowExecutionPlan(
            session_id=session_id,
            principal_subject=world.creator.subject,
            target_instance_id=TARGET_IDENTITY["instance_id"],
            plan_digest="p" * 64,
            plan_json={"_execution": {"payload": {"settings": {}}}},
            state_fingerprint="s" * 64,
            required_confirmations_json=[],
            resulting_job_id=resulting_job_id,
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(plan)
        db.flush()
        return plan.id


@pytest.fixture
def shared_world(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path / "data")
    database = Database(paths.database)
    with database.session() as db:
        workspace_owner = _add_user(db, "workspace-owner")
        creator_user = _add_user(db, "export-creator")
        member_user = _add_user(db, "workspace-member")
        viewer_user = _add_user(db, "external-viewer")
        workspace = Workspace(
            kind="team",
            name="Shared workspace",
            personal_owner_user_id=None,
        )
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
        db.add_all(
            [
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    user_id=workspace_owner.id,
                    role="owner",
                ),
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    user_id=creator_user.id,
                    role="admin",
                ),
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    user_id=member_user.id,
                    role="member",
                ),
            ]
        )
        creator_user_id = creator_user.id
        member_user_id = member_user.id
        viewer_user_id = viewer_user.id

    sessions = SessionService(database)
    source = sessions.create(
        "Shared export source",
        workspace_id=workspace_id,
        seed_voice_mode=False,
    )
    assert source.project_id is not None
    source_path = paths.artifacts / "shared-export-source.txt"
    source_path.write_text("source text for a shared export\n", encoding="utf-8")
    artifacts = ArtifactService(database, paths)
    checkpoint = artifacts.register(
        source_path,
        kind="text",
        role="transcription",
        session_id=source.id,
        project_id=source.project_id,
        workspace_id=workspace_id,
    )
    branch_session = sessions.create(
        "French export branch",
        workspace_id=workspace_id,
        project_id=source.project_id,
        seed_voice_mode=False,
    )
    branch_path = paths.artifacts / "shared-export-branch.txt"
    branch_path.write_text("source text for a shared export\n", encoding="utf-8")
    branch_checkpoint = artifacts.register(
        branch_path,
        kind="text",
        role="transcription",
        session_id=branch_session.id,
        project_id=source.project_id,
        workspace_id=workspace_id,
    )
    with database.session() as db:
        translation_project = TranslationProject(
            project_root_id=source.project_id,
            name="Shared translation project",
            source_session_id=source.id,
            checkpoint_artifact_id=checkpoint.id,
            source_content_hash=checkpoint.content_hash,
            source_language="en",
        )
        db.add(translation_project)
        db.flush()
        branch = TranslationProjectBranch(
            project_id=translation_project.id,
            session_id=branch_session.id,
            target_language="fr",
            source_checkpoint_artifact_id=branch_checkpoint.id,
            source_content_hash=branch_checkpoint.content_hash,
        )
        db.add(branch)
        db.add(
            ProjectMembership(
                project_id=source.project_id,
                user_id=viewer_user_id,
                role="viewer",
                granted_by_user_id=creator_user_id,
            )
        )
        db.flush()
        project_id = translation_project.id
        branch_id = branch.id

    workspace_resources = CredentialResources(workspace_ids=frozenset({workspace_id}))
    creator = _mint_principal(
        database,
        creator_user_id,
        scopes=USER_BROWSER_SCOPES,
        resources=workspace_resources,
        label="export creator",
    )
    member = _mint_principal(
        database,
        member_user_id,
        scopes=USER_BROWSER_SCOPES,
        resources=workspace_resources,
        label="workspace member",
    )
    viewer = _mint_principal(
        database,
        viewer_user_id,
        scopes=USER_BROWSER_SCOPES,
        resources=CredentialResources(project_ids=frozenset({source.project_id})),
        label="external project viewer",
    )
    service = TranslationProjectOperationService(
        SimpleNamespace(
            database=database,
            paths=paths,
            redactor=_IdentityRedactor(),
        )
    )
    world = SharedExportWorld(
        database=database,
        paths=paths,
        service=service,
        target_identity=dict(TARGET_IDENTITY),
        operation_id="",
        creator=creator,
        member=member,
        viewer=viewer,
        creator_user_id=creator_user_id,
        member_user_id=member_user_id,
        viewer_user_id=viewer_user_id,
        workspace_id=workspace_id,
        root_project_id=source.project_id,
        project_id=project_id,
        branch_id=branch_id,
        branch_session_id=branch_session.id,
        source_session_id=source.id,
        checkpoint_artifact_id=checkpoint.id,
        branch_checkpoint_artifact_id=branch_checkpoint.id,
        job_id="",
        plan_id="",
    )
    with database.session() as db:
        job = Job(
            kind="export.pdf",
            workspace_id=workspace_id,
            project_id=source.project_id,
            session_id=branch_session.id,
            status="queued",
            payload_json={},
            resource_keys_json=[],
        )
        db.add(job)
        db.flush()
        plan = WorkflowExecutionPlan(
            session_id=branch_session.id,
            principal_subject=creator.subject,
            target_instance_id=TARGET_IDENTITY["instance_id"],
            plan_digest="p" * 64,
            plan_json={"_execution": {"payload": {"settings": {}}}},
            state_fingerprint="s" * 64,
            required_confirmations_json=[],
            resulting_job_id=job.id,
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(plan)
        db.flush()
        operation = TranslationProjectOperation(
            project_id=project_id,
            principal_subject=creator.subject,
            target_instance_id=TARGET_IDENTITY["instance_id"],
            action="export",
            status="completed",
            preview_digest="d" * 64,
            preview_json={
                "project_revision": 1,
                "export_kind": "configured",
                "captures": {
                    branch_id: {
                        "plan": {
                            "plan_id": plan.id,
                            "plan_digest": plan.plan_digest,
                            "source": {"artifact_id": branch_checkpoint.id},
                        }
                    }
                },
                "previous_operation_id": None,
            },
            children_json=[
                {
                    "branch_id": branch_id,
                    "session_id": branch_session.id,
                    "attempt": 1,
                    "state": "queued",
                    "eligible": True,
                    "job_id": job.id,
                }
            ],
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(operation)
        db.flush()
        world.operation_id = operation.id
        world.job_id = job.id
        world.plan_id = plan.id
    yield world
    database.dispose()


def _poison_operation_json(world: SharedExportWorld) -> None:
    with world.database.session() as db:
        db.execute(
            text(
                "UPDATE translation_project_operations "
                "SET preview_json = :preview, children_json = :children "
                "WHERE id = :operation_id"
            ),
            {
                "preview": "{invalid-preview-json",
                "children": "{invalid-children-json",
                "operation_id": world.operation_id,
            },
        )


def test_workspace_member_reads_creator_receipts_after_creator_leaves(shared_world):
    world = shared_world
    with world.database.session() as db:
        db.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == world.workspace_id,
                WorkspaceMembership.user_id == world.creator_user_id,
            )
        )

    response = world.service.get_export_receipts(
        world.operation_id,
        principal=world.member,
        target_identity=world.target_identity,
    )
    assert response["action"] == "export"
    assert response["children"][0]["job_id"] == world.job_id
    with pytest.raises(ProjectNotFound):
        world.service.get_export_receipts(
            world.operation_id,
            principal=world.creator,
            target_identity=world.target_identity,
        )


def test_same_user_with_a_different_valid_credential_reads_shared_receipts(shared_world):
    world = shared_world
    second_credential = _mint_principal(
        world.database,
        world.creator_user_id,
        scopes=USER_BROWSER_SCOPES,
        resources=CredentialResources(workspace_ids=frozenset({world.workspace_id})),
        label="second creator credential",
    )
    assert second_credential.subject != world.creator.subject
    response = world.service.get_export_receipts(
        world.operation_id,
        principal=second_credential,
        target_identity=world.target_identity,
    )
    assert response["children"][0]["job_id"] == world.job_id


def test_external_project_viewer_reads_receipts_but_cannot_run(shared_world):
    world = shared_world
    response = world.service.get_export_receipts(
        world.operation_id,
        principal=world.viewer,
        target_identity=world.target_identity,
    )
    assert response["children"][0]["job_id"] == world.job_id
    with world.database.session() as db:
        with pytest.raises(AccessDenied):
            world.service.require_export_operation_access(
                db,
                world.operation_id,
                world.viewer,
                world.target_identity,
                action=ProjectAction.RUN,
            )


def test_non_export_operation_is_rejected_before_operation_json_hydration(shared_world):
    world = shared_world
    with world.database.session() as db:
        db.execute(
            text(
                "UPDATE translation_project_operations SET action = 'translate' "
                "WHERE id = :operation_id"
            ),
            {"operation_id": world.operation_id},
        )
    _poison_operation_json(world)
    with pytest.raises(ProjectOperationError) as error:
        world.service.get_export_receipts(
            world.operation_id,
            principal=world.member,
            target_identity=world.target_identity,
        )
    assert error.value.code == "not_found"


def test_wrong_instance_is_rejected_before_operation_json_hydration(shared_world):
    world = shared_world
    with world.database.session() as db:
        db.execute(
            text(
                "UPDATE translation_project_operations "
                "SET target_instance_id = :target_id WHERE id = :operation_id"
            ),
            {
                "target_id": "00000000-0000-4000-8000-000000000002",
                "operation_id": world.operation_id,
            },
        )
    _poison_operation_json(world)
    with pytest.raises(ProjectOperationError) as error:
        world.service.get_export_receipts(
            world.operation_id,
            principal=world.member,
            target_identity=world.target_identity,
        )
    assert error.value.code == "target_identity_mismatch"


@pytest.mark.parametrize(
    "loss",
    ("account", "token", "membership", "credential_resource"),
    ids=(
        "account-loss-before-json",
        "token-loss-before-json",
        "membership-loss-before-json",
        "credential-resource-loss-before-json",
    ),
)
def test_current_authority_loss_is_rejected_before_operation_json_hydration(
    shared_world, loss: str
):
    world = shared_world
    with world.database.session() as db:
        if loss == "account":
            db.execute(update(User).where(User.id == world.member_user_id).values(is_active=False))
        elif loss == "token":
            db.execute(
                update(ApiToken)
                .where(ApiToken.id == world.member.token_id)
                .values(revoked_at=utcnow())
            )
        elif loss == "membership":
            db.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == world.workspace_id,
                    WorkspaceMembership.user_id == world.member_user_id,
                )
            )
        else:
            db.execute(
                delete(ApiTokenWorkspaceGrant).where(
                    ApiTokenWorkspaceGrant.token_id == world.member.token_id
                )
            )
    _poison_operation_json(world)
    with pytest.raises((AccessDenied, ProjectNotFound)):
        world.service.get_export_receipts(
            world.operation_id,
            principal=world.member,
            target_identity=world.target_identity,
        )


@pytest.mark.parametrize(
    ("administrator", "scopes"),
    (
        (True, frozenset({"app.read", "app.admin"})),
        (False, USER_BROWSER_SCOPES),
    ),
    ids=("unrelated-installation-administrator", "unrelated-other-user"),
)
def test_unrelated_user_cannot_read_shared_export_receipts(
    shared_world, administrator: bool, scopes: frozenset[str]
):
    world = shared_world
    with world.database.session() as db:
        outsider = _add_user(
            db,
            "unrelated-administrator" if administrator else "unrelated-user",
            administrator=administrator,
        )
        outsider_id = outsider.id
    principal = _mint_principal(
        world.database,
        outsider_id,
        scopes=scopes,
        resources=CredentialResources(),
        label="unrelated credential",
    )
    _poison_operation_json(world)
    with pytest.raises((AccessDenied, ProjectNotFound)):
        world.service.get_export_receipts(
            world.operation_id,
            principal=principal,
            target_identity=world.target_identity,
        )


def test_existing_execute_remains_principal_bound_before_operation_json_hydration(
    shared_world,
):
    world = shared_world
    _poison_operation_json(world)
    with pytest.raises(ProjectOperationError) as error:
        world.service.execute(
            world.operation_id,
            "unused-digest",
            [],
            principal=world.member,
            target_identity=world.target_identity,
            idempotency_key="peer-execute",
        )
    assert error.value.code == "not_found"


@pytest.mark.parametrize(
    "foreign_reference",
    ("job", "plan", "artifact"),
    ids=(
        "foreign-child-job-before-json-or-filesystem",
        "foreign-plan-before-json-or-filesystem",
        "foreign-artifact-before-json-or-filesystem",
    ),
)
def test_foreign_receipt_references_are_scoped_before_json_or_file_access(
    shared_world, foreign_reference: str
):
    world = shared_world
    path_guard = _NoManagedPath()
    world.service.services.paths = path_guard

    if foreign_reference == "job":
        foreign_job_id = _add_job(world, world.source_session_id)
        with world.database.session() as db:
            db.execute(
                text("UPDATE workflow_execution_plans SET resulting_job_id = NULL WHERE id = :id"),
                {"id": world.plan_id},
            )
            operation = db.get(TranslationProjectOperation, world.operation_id)
            children = deepcopy(operation.children_json)
            children[0]["job_id"] = foreign_job_id
            operation.children_json = children
        with world.database.session() as db:
            db.execute(
                text("UPDATE jobs SET result_json = :value WHERE id = :id"),
                {"value": "{invalid-job-json", "id": foreign_job_id},
            )
    elif foreign_reference == "plan":
        foreign_plan_id = _add_plan(
            world,
            world.source_session_id,
            resulting_job_id=None,
        )
        with world.database.session() as db:
            operation = db.get(TranslationProjectOperation, world.operation_id)
            preview = deepcopy(operation.preview_json)
            preview["captures"][world.branch_id]["plan"]["plan_id"] = foreign_plan_id
            operation.preview_json = preview
            job = db.get(Job, world.job_id)
            job.status = "completed"
            job.result_json = {}
        with world.database.session() as db:
            db.execute(
                text("UPDATE workflow_execution_plans SET plan_json = :value WHERE id = :id"),
                {"value": "{invalid-plan-json", "id": foreign_plan_id},
            )
    else:
        foreign_path = world.paths.artifacts / "foreign-export.pdf"
        foreign_path.write_bytes(b"foreign export body")
        artifact = ArtifactService(world.database, world.paths).register(
            foreign_path,
            kind="export",
            role="export_pdf",
            session_id=world.source_session_id,
            project_id=world.root_project_id,
            workspace_id=world.workspace_id,
            settings={},
        )
        with world.database.session() as db:
            job = db.get(Job, world.job_id)
            job.status = "completed"
            job.result_json = {"artifact_id": artifact.id}
        with world.database.session() as db:
            db.execute(
                text("UPDATE artifacts SET metadata_json = :value WHERE id = :id"),
                {"value": "{invalid-artifact-json", "id": artifact.id},
            )

    response = world.service.get_export_receipts(
        world.operation_id,
        principal=world.member,
        target_identity=world.target_identity,
    )
    child = response["children"][0]
    assert child["state"] == "failed"
    if foreign_reference == "job":
        assert child["reason"] == "The child job is unavailable."
    else:
        assert child["reason"] == (
            "An export artifact does not match the captured job settings or output role."
        )
    assert path_guard.calls == []
