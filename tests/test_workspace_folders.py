"""Workspace folder isolation, inherited content rights and retry boundaries."""

from __future__ import annotations

from sqlalchemy import delete, event, func, select

from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.models import ApiIdempotency, Job, JobActorRecord, ProjectFolder
from pandrator.web.project_folders import create_project_folder
from pandrator.web.projects.models import ProjectMembership
from tests.test_job_access_routes import case as case  # noqa: F401
from tests.test_job_access_routes import credential, job
from tests.test_job_access_routes import no_network as no_network


def folder(case, name, workspace_id, parent_id=None):
    with case.database.immediate_session() as session:
        return create_project_folder(session, name, parent_id, workspace_id=workspace_id)["folder"]


def endpoint(case, suffix=""):
    return f"/api/v1/project-folders{suffix}?workspace_id={case.team}"


def write_headers(credential, key):
    return {**credential.headers, "Idempotency-Key": key}


def test_folder_scope_defaults_to_actual_user_and_excludes_guest_and_admin(case):
    owner_home = folder(case, "Owner private sentinel", case.personal[case.users["owner"]])
    member_home = folder(case, "Member private sentinel", case.personal[case.users["member"]])
    shared = folder(case, "Shared", case.team)
    member = credential(case, "member")
    response = member.client.get("/api/v1/project-folders", headers=member.headers)
    assert response.status_code == 200
    assert response.get_json()["workspace_id"] == case.personal[case.users["member"]]
    assert [row["id"] for row in response.get_json()["items"]] == [member_home["id"]]
    response = member.client.get(endpoint(case), headers=member.headers)
    assert response.status_code == 200
    assert [row["id"] for row in response.get_json()["items"]] == [shared["id"]]
    assert owner_home["name"] not in response.get_data(as_text=True)
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.user_id == case.users["member"]
                )
            )
            is None
        )
    for role in ("viewer", "editor", "outsider"):
        guest = credential(case, role)
        denied = guest.client.get(endpoint(case), headers=guest.headers)
        assert denied.status_code == 404
        assert shared["name"] not in denied.get_data(as_text=True)
        assert (
            guest.client.post(
                endpoint(case),
                json={"name": "Denied"},
                headers=write_headers(guest, f"folder-denied-{role}"),
            ).status_code
            == 404
        )


def test_member_creates_nested_folders_and_retains_project_identity_aliases(case):
    member = credential(case, "member")
    root = member.client.post(
        endpoint(case),
        json={"name": "Team course"},
        headers=write_headers(member, "folder-member-root"),
    )
    assert root.status_code == 201, root.get_json()
    root = root.get_json()["folder"]
    child = member.client.post(
        endpoint(case),
        json={"name": "Translations", "parent_id": root["id"]},
        headers=write_headers(member, "folder-member-child"),
    )
    assert child.status_code == 201, child.get_json()
    child = child.get_json()["folder"]
    assert child["workspace_id"] == root["workspace_id"] == case.team
    attached = member.client.put(
        f"/api/v1/sessions/{case.a.id}/project-folders?workspace_id={case.team}",
        json={
            "folder_ids": [root["id"], child["id"]],
            "expected_revisions": {root["id"]: 1, child["id"]: 1},
        },
        headers=write_headers(member, "folder-member-assign"),
    )
    assert attached.status_code == 200, attached.get_json()
    body = attached.get_json()
    assert body["project_id"] == body["source_session_id"] == case.a.id
    assert body["project_root_id"] == case.a.project_id
    assert {row["folder_id"] for row in body["memberships"]} == {root["id"], child["id"]}
    for row in body["memberships"]:
        assert row["project_id"] == row["source_session_id"] == case.a.id
        assert row["project_root_id"] == case.a.project_id
    removed = member.client.delete(
        endpoint(case, f"/{root['id']}"),
        json={"expected_revision": 2},
        headers=write_headers(member, "folder-member-delete"),
    )
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()["items"][0]["parent_id"] is None
    assert len(removed.get_json()["memberships"]) == 1
    # An admitted creation remains replayable after its parent was removed.
    replay = member.client.post(
        endpoint(case),
        json={"name": "Translations", "parent_id": root["id"]},
        headers=write_headers(member, "folder-member-child"),
    )
    assert replay.status_code == 201, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json()["folder"]["id"] == child["id"]
    # A new command with the now-missing parent still fails without mutation.
    assert (
        member.client.post(
            endpoint(case),
            json={"name": "Another", "parent_id": root["id"]},
            headers=write_headers(member, "folder-member-child-fresh"),
        ).status_code
        == 404
    )
    with case.database.snapshot_session() as session:
        assert session.get(ProjectFolder, root["id"]) is None
        assert session.get(ProjectFolder, child["id"]).parent_id is None
        assert session.scalar(select(func.count()).select_from(ProjectFolder)) == 1
    assert (
        member.client.get(f"/api/v1/sessions/{case.a.id}", headers=member.headers).status_code
        == 200
    )


def test_foreign_folder_and_project_inputs_fail_before_receipt_and_mutation(case):
    shared = folder(case, "Shared", case.team)
    private = folder(case, "Private sentinel", case.personal[case.users["owner"]])
    private_session = case.services.sessions.create("Private project sentinel")
    member = credential(case, "member")
    requests = [
        ("post", endpoint(case), {"name": "New", "parent_id": private["id"]}),
        ("patch", endpoint(case, f"/{private['id']}"), {"name": "Rename", "expected_revision": 1}),
        (
            "patch",
            endpoint(case, f"/{shared['id']}"),
            {"parent_id": private["id"], "expected_revision": 1},
        ),
        ("delete", endpoint(case, f"/{private['id']}"), {"expected_revision": 1}),
        (
            "put",
            endpoint(case, f"/{shared['id']}/projects"),
            {"expected_revision": 1, "project_ids": [private_session.id]},
        ),
        (
            "put",
            f"/api/v1/sessions/{case.a.id}/project-folders?workspace_id={case.team}",
            {"folder_ids": [private["id"]], "expected_revisions": {private["id"]: 1}},
        ),
    ]
    for index, (method, path, payload) in enumerate(requests):
        response = getattr(member.client, method)(
            path,
            json=payload,
            headers=write_headers(member, f"folder-foreign-{index}"),
        )
        assert response.status_code == 404, response.get_json()
        assert "sentinel" not in response.get_data(as_text=True)
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(ApiIdempotency)) == 0
        assert session.get(ProjectFolder, shared["id"]).revision == 1
        assert session.get(ProjectFolder, private["id"]).name == private["name"]


def test_member_revocation_and_narrowed_credential_fence_cached_retries(case):
    member = credential(case, "member")
    headers = write_headers(member, "folder-revoked-retry")
    created = member.client.post(
        endpoint(case), json={"name": "Saved team folder"}, headers=headers
    )
    assert created.status_code == 201
    with case.database.immediate_session() as session:
        session.execute(
            delete(ApiTokenWorkspaceGrant).where(
                ApiTokenWorkspaceGrant.token_id == member.token.id,
                ApiTokenWorkspaceGrant.workspace_id == case.team,
            )
        )
    assert (
        member.client.post(
            endpoint(case), json={"name": "Saved team folder"}, headers=headers
        ).status_code
        == 404
    )
    with case.database.immediate_session() as session:
        session.add(ApiTokenWorkspaceGrant(token_id=member.token.id, workspace_id=case.team))
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.users["member"],
            )
        )
    assert member.client.get(endpoint(case), headers=member.headers).status_code == 404
    assert (
        member.client.post(
            endpoint(case), json={"name": "Saved team folder"}, headers=headers
        ).status_code
        == 404
    )
    with case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(ProjectFolder)) == 1


def test_new_retries_survive_deletion_but_legacy_foreign_snapshots_do_not_replay(case):
    owner = credential(case, "owner")
    headers = write_headers(owner, "folder-original-legacy-key")
    path = "/api/v1/project-folders"
    created = owner.client.post(path, json={"name": "Personal folder"}, headers=headers)
    assert created.status_code == 201
    # Explicit personal selection shares the existing personal fingerprint.
    personal_path = f"{path}?workspace_id={case.personal[case.users['owner']]}"
    assert (
        owner.client.post(
            personal_path, json={"name": "Personal folder"}, headers=headers
        ).get_json()
        == created.get_json()
    )
    legacy = dict(created.get_json())
    legacy.pop("workspace_id")
    with case.database.immediate_session() as session:
        row = session.scalar(
            select(ApiIdempotency).where(
                ApiIdempotency.idempotency_key == headers["Idempotency-Key"]
            )
        )
        row.response_json = legacy
    old_retry = owner.client.post(path, json={"name": "Personal folder"}, headers=headers)
    assert old_retry.status_code == 201
    assert old_retry.get_json() == legacy
    # A team request cannot replay the same personal key under another scope.
    assert (
        owner.client.post(
            endpoint(case), json={"name": "Personal folder"}, headers=headers
        ).status_code
        == 409
    )
    folder_id = created.get_json()["folder"]["id"]
    deletion_headers = write_headers(owner, "folder-delete-replay")
    deletion = owner.client.delete(
        f"{path}/{folder_id}", json={"expected_revision": 1}, headers=deletion_headers
    )
    replay = owner.client.delete(
        f"{path}/{folder_id}", json={"expected_revision": 1}, headers=deletion_headers
    )
    assert deletion.status_code == replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == deletion.get_json()
    foreign = folder(case, "Foreign replay sentinel", case.team)
    with case.database.immediate_session() as session:
        row = session.scalar(
            select(ApiIdempotency).where(
                ApiIdempotency.idempotency_key == headers["Idempotency-Key"]
            )
        )
        row.response_json = {"items": [foreign], "memberships": [], "folder": foreign}
    denied = owner.client.post(path, json={"name": "Personal folder"}, headers=headers)
    assert denied.status_code == 404
    assert foreign["name"] not in denied.get_data(as_text=True)


def test_folder_authorization_queries_do_not_grow_per_displayed_row(case):
    member = credential(case, "member")
    folder(case, "First", case.team)
    # Exclude one-time HTTP/credential initialization from the row-scaling check.
    assert member.client.get(endpoint(case), headers=member.headers).status_code == 200
    counts = []
    for size in (1, 20):
        if size == 20:
            for index in range(19):
                folder(case, f"More {index}", case.team)
        statements = []

        def capture(
            _connection, _cursor, statement, _parameters, _context, _many, *, captured=statements
        ):
            if statement.lstrip().upper().startswith("SELECT"):
                captured.append(statement)

        event.listen(case.database.engine, "before_cursor_execute", capture)
        try:
            response = member.client.get(endpoint(case), headers=member.headers)
        finally:
            event.remove(case.database.engine, "before_cursor_execute", capture)
        assert response.status_code == 200
        assert len(response.get_json()["items"]) == size
        counts.append(len(statements))
    assert counts[0] == counts[1]


def test_zero_grant_member_can_read_and_cancel_another_contributors_work(case):
    member = credential(case, "member")
    identifier = job(case, author="owner")
    response = member.client.get(f"/api/v1/work/{identifier}", headers=member.headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["cancellable"] is True
    canceled = member.client.post(f"/api/v1/jobs/{identifier}/cancel", headers=member.headers)
    assert canceled.status_code == 200, canceled.get_json()


def test_zero_grant_member_can_admit_export_but_read_only_credential_cannot(case):
    member = credential(case, "member")
    path = f"/api/v1/sessions/{case.a.id}/bundle"
    headers = write_headers(member, "member-bundle-export")
    first = member.client.post(path, json={"include_sources": False}, headers=headers)
    assert first.status_code == 202, first.get_json()
    replay = member.client.post(path, json={"include_sources": False}, headers=headers)
    assert replay.status_code == 202, replay.get_json()
    assert replay.get_json() == first.get_json()
    read_only = credential(case, "member", scopes=frozenset({"app.read"}))
    denied = read_only.client.post(
        path,
        json={"include_sources": False},
        headers=write_headers(read_only, "member-bundle-no-run"),
    )
    assert denied.status_code == 403, denied.get_json()
    with case.database.snapshot_session() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "session.bundle.export")).all()
        assert len(jobs) == 1
        assert jobs[0].project_id == case.a.project_id
        assert jobs[0].workspace_id == case.team
        assert session.get(JobActorRecord, jobs[0].id).user_id == case.users["member"]
        assert (
            session.scalar(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.user_id == case.users["member"]
                )
            )
            is None
        )
