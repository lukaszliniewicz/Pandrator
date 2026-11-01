"""Direct workflow routes and plans use current root authority in their units."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.access.contracts import AccessContext, AccessDenied
from pandrator.web.accounts import installation_mode
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import (
    actor_from_principal,
    actor_to_storage,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.projects.models import ProjectMembership
from tests.test_project_export_bundles import _translation
from tests.test_project_operation_access import (
    _person,
    _remove,
)
from tests.test_project_operation_access import (
    bundle_case as _bundle_case,
)
from tests.test_project_operation_access import (
    team_case as _team_case,
)

bundle_case = _bundle_case
team_case = _team_case
EXPORT = {
    "export_mode": "subtitles",
    "subtitle_mode": "translation",
    "subtitle_format": "srt",
    "subtitle_selection": "translation",
}


@pytest.fixture
def protected_case(team_case, monkeypatch):
    case = team_case
    # Exercise the production opt-in command in this disposable installation.
    monkeypatch.setattr(installation_mode, "COLLABORATION_AVAILABLE", True)
    case.services.auth.initialize_owner("disposable workflow acceptance password")
    response = case.client.post(
        "/api/v1/admin/installation/collaboration",
        json={"expected_revision": 0},
        headers=case.headers,
    )
    assert response.status_code == 200, response.get_json()
    case.branch = case.branches[0]
    case.sid = case.branch["session_id"]
    case.translation = _translation(case, case.branch)
    actor = actor_from_principal(case.principal)
    assert actor is not None
    case.context = AccessContext(actor)
    yield case


def _headers(principal):
    return {"Authorization": f"Bearer {principal.token_id}"}


def _read_urls(case):
    base = f"/api/v1/sessions/{case.sid}"
    return [
        base + "/workflow",
        base + "/stages/translate/artifacts",
        base + "/stages/translate/impact",
        base + "/stages/translate/settings-mismatches",
    ]


def _counts(case):
    with case.database.session() as db:
        counts = tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (m.Job, m.WorkflowExecutionPlan, m.ApiIdempotency, m.SessionStageSelection)
        )
        return counts, db.get(m.Artifact, case.translation.id).state


def _plan(case):
    return case.services.workflow_plans.create(
        principal=case.principal,
        target_identity=case.target,
        session_id=case.sid,
        target_stage="export",
        overrides=EXPORT,
        continuation=False,
    )


def _execute(case, plan, *, key="protected-execute"):
    return case.services.workflow_plans.execute(
        principal=case.principal,
        target_identity=case.target,
        plan_id=plan["plan_id"],
        supplied_digest=plan["plan_digest"],
        accepted_confirmations=plan["required_confirmations"],
        idempotency_key=key,
    )


def _mutations(case, principal, plan):
    headers = _headers(principal)
    base = f"/api/v1/sessions/{case.sid}"
    return [
        lambda: case.client.put(
            base + "/stages/translate/selection",
            json={"artifact_id": case.translation.id},
            headers={**headers, "If-Match": '"0"'},
        ),
        lambda: case.client.delete(
            base + f"/stages/translate/artifacts/{case.translation.id}", headers=headers
        ),
        lambda: case.client.post(base + "/stages/export/run", json=EXPORT, headers=headers),
        lambda: case.client.post(
            base + "/workflow-plans",
            json={"target_stage": "export", "overrides": EXPORT, "continuation": False},
            headers=headers,
        ),
        lambda: case.client.post(
            f"/api/v1/workflow-plans/{plan['plan_id']}/execute",
            json={
                "plan_digest": plan["plan_digest"],
                "accepted_confirmations": plan["required_confirmations"],
            },
            headers={**headers, "Idempotency-Key": "protected-execute"},
        ),
    ]


def test_member_without_project_memberships_can_read_select_and_queue_export(protected_case):
    case = protected_case
    headers = _headers(case.principal)
    with case.database.session() as db:
        assert (
            db.scalar(
                select(ProjectMembership).where(ProjectMembership.user_id == case.principal.user_id)
            )
            is None
        )
    for url in _read_urls(case):
        response = case.client.get(url, headers=headers)
        assert response.status_code == 200, response.get_json()
    history = case.client.get(_read_urls(case)[1], headers=headers).get_json()
    selected = case.client.put(
        f"/api/v1/sessions/{case.sid}/stages/translate/selection",
        json={"artifact_id": case.translation.id},
        headers={**headers, "If-Match": f'"{history["revision"]}"'},
    )
    assert selected.status_code == 200, selected.get_json()
    response = case.client.post(
        f"/api/v1/sessions/{case.sid}/stages/export/run", json=EXPORT, headers=headers
    )
    assert response.status_code == 202, response.get_json()
    with case.database.session() as db:
        assert len(list(db.scalars(select(m.Job).where(m.Job.session_id == case.sid)))) == 1


@pytest.mark.parametrize("role", ["manager", "editor", "reviewer", "viewer"])
def test_project_only_reader_may_inspect_but_cannot_mutate_prepare_or_replay(protected_case, role):
    case = protected_case
    plan = _plan(case)
    _execute(case, plan)
    _remove(case)
    with case.database.immediate_session() as db:
        db.add(
            ProjectMembership(
                project_id=case.source.project_id, user_id=case.principal.user_id, role=role
            )
        )
    headers = _headers(case.principal)
    for url in _read_urls(case):
        response = case.client.get(url, headers=headers)
        assert response.status_code == 200, response.get_json()
    assert (
        case.client.get(f"/api/v1/workflow-plans/{plan['plan_id']}", headers=headers).status_code
        == 200
    )
    before = _counts(case)
    for mutation in _mutations(case, case.principal, plan):
        response = mutation()
        assert response.status_code == 403, response.get_json()
        assert _counts(case) == before


@pytest.mark.parametrize("admin", [False, True])
def test_foreign_user_and_unrelated_admin_cannot_read_or_modify_workflow(protected_case, admin):
    case = protected_case
    plan = _plan(case)
    outsider = _person(case, workspace_id=case.workspace_id, admin=admin)
    headers = _headers(outsider)
    before = _counts(case)
    for url in _read_urls(case) + [f"/api/v1/workflow-plans/{plan['plan_id']}"]:
        response = case.client.get(url, headers=headers)
        assert response.status_code == 404, response.get_json()
        assert "Translated" not in response.get_data(as_text=True)
    for mutation in _mutations(case, outsider, plan):
        response = mutation()
        assert response.status_code == 404, response.get_json()
        assert _counts(case) == before


@pytest.mark.parametrize("change", ["membership", "epoch", "token"])
def test_current_authority_required_for_preflight_and_same_key_plan_replay(protected_case, change):
    case = protected_case
    plan = _plan(case)
    _execute(case, plan)
    if change == "membership":
        _remove(case)
    else:
        with case.database.immediate_session() as db:
            if change == "epoch":
                db.get(UserCredential, case.principal.user_id).auth_revision += 1
            else:
                db.get(m.ApiToken, case.principal.token_id).scopes_json = ["app.read"]
    before = _counts(case)
    for call in [
        lambda: case.services.workflows.run_stage_for_user(case.context, case.sid, "invalid-stage"),
        lambda: _plan(case),
        lambda: _execute(case, plan),
    ]:
        with pytest.raises((AccessDenied, KeyError)):
            call()
        assert _counts(case) == before


@pytest.mark.parametrize("entry", ["run", "plan-create"])
def test_writer_rechecks_membership_removed_after_preflight(protected_case, entry):
    case = protected_case
    original = case.services.workflows.resolve_stage
    before = _counts(case)

    def revoke_after_resolution(*args, **kwargs):
        resolved = original(*args, **kwargs)
        _remove(case)
        return resolved

    with patch.object(
        case.services.workflows, "resolve_stage", side_effect=revoke_after_resolution
    ):
        with pytest.raises(KeyError):
            if entry == "run":
                case.services.workflows.run_stage_for_user(case.context, case.sid, "export", EXPORT)
            else:
                _plan(case)
    assert _counts(case) == before


@pytest.mark.parametrize("ambient", ["missing", "different"])
def test_direct_run_and_plan_execute_capture_actual_actor_and_restore_ambient(
    protected_case, ambient
):
    case = protected_case
    outsider = _person(case, workspace_id=case.workspace_id)
    actor = None if ambient == "missing" else actor_from_principal(outsider)
    expected = actor_to_storage(case.context.actor)
    with execution_actor_scope(actor):
        job = case.services.workflows.run_stage_for_user(case.context, case.sid, "export", EXPORT)
        assert current_execution_actor.get() == actor
        plan = _plan(case)
        response, status, replayed = _execute(case, plan)
        assert status == 202 and replayed is False
        assert current_execution_actor.get() == actor
    with case.database.session() as db:
        for job_id in [job.id, response["id"]]:
            captured = db.get(m.JobActorRecord, job_id)
            assert captured is not None
            assert {
                "user_id": captured.user_id,
                "auth_revision": captured.auth_revision,
                "context_json": captured.context_json,
            } == expected


@pytest.mark.parametrize("malformed", [False, True])
def test_plans_require_actual_principal_actor(protected_case, malformed):
    case = protected_case
    plan = _plan(case)
    case.principal = replace(case.principal, user_id=None, auth_revision=1 if malformed else None)
    before = _counts(case)
    for call in [
        lambda: _plan(case),
        lambda: _execute(case, plan),
        lambda: case.services.workflow_plans.get(plan["plan_id"], principal=case.principal),
    ]:
        with pytest.raises(AccessDenied):
            call()
        assert _counts(case) == before
