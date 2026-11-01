"""HTTP authorization boundaries for speech optimization review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.models import Artifact, ArtifactEdge, new_id
from tests.test_job_access_routes import SCOPES, credential
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import no_network as no_network


def create_optimized(case, record, *, name="optimized.json"):
    paths = case.services.artifacts.paths
    path = paths.sessions / record.storage_key / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"source_text": "Original", "text": "Optimized"}]),
        encoding="utf-8",
    )
    artifact = case.services.artifacts.register(
        path,
        kind="json",
        role="tts_optimized",
        session_id=record.id,
    )
    return artifact.id, path


def create_rootless_optimized(case):
    paths = case.services.artifacts.paths
    path = paths.sessions / "rootless" / f"{new_id()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"source_text": "Original", "text": "Optimized"}]),
        encoding="utf-8",
    )
    artifact = case.services.artifacts.register(
        path,
        kind="json",
        role="tts_optimized",
        workspace_id=case.team,
    )
    return artifact.id, path


def review(client_credential, artifact_id):
    return client_credential.client.post(
        f"/api/v1/artifacts/{artifact_id}/optimization-review",
        json={"items": [{"index": 0, "text": "Reviewed"}]},
        headers=client_credential.headers,
    )


def artifact_ids(case):
    with case.database.snapshot_session() as session:
        return set(session.scalars(select(Artifact.id)).all())


def file_inventory(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def watch_source_io(monkeypatch, case, source_path: Path):
    paths = case.services.artifacts.paths
    resolved = []
    paths_type = type(paths)
    original_managed_path = paths_type.managed_path

    def tracked_managed_path(instance, relative_path):
        if instance is paths:
            resolved.append(relative_path)
        return original_managed_path(instance, relative_path)

    monkeypatch.setattr(paths_type, "managed_path", tracked_managed_path)
    reads = []
    original_read_text = Path.read_text

    def tracked_read_text(candidate, *args, **kwargs):
        if candidate == source_path:
            reads.append(candidate)
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    return resolved, reads


def test_cross_project_optimization_review_is_hidden(case):
    editor = credential(case, "editor")
    artifact_id, _path = create_optimized(case, case.b)

    response = review(editor, artifact_id)

    assert response.status_code == 404, response.get_json()


@pytest.mark.parametrize("actor_name", ["owner", "member"])
def test_team_owner_and_member_can_review_optimization_artifact(case, actor_name):
    actor = credential(case, actor_name)
    artifact_id, source_path = create_optimized(case, case.a)
    source_bytes = source_path.read_bytes()

    response = review(actor, artifact_id)

    assert response.status_code == 201, response.get_json()
    child_id = response.get_json()["id"]
    assert source_path.read_bytes() == source_bytes
    with case.database.snapshot_session() as session:
        child = session.get(Artifact, child_id)
        assert child is not None
        assert child.session_id == case.a.id
        assert child.role == "tts_optimized"
        assert child.metadata_json["reviewed_from"] == artifact_id
        assert session.get(Artifact, artifact_id) is not None
        assert (
            session.get(
                ArtifactEdge,
                {"parent_artifact_id": artifact_id, "child_artifact_id": child_id},
            )
            is not None
        )


def test_personal_project_owner_can_review_optimization_artifact(case):
    personal_session = case.services.sessions.create(
        "Owner personal optimization", workspace_id=case.personal[case.users["owner"]]
    )
    owner = credential(case, "owner", grant_team=False)
    artifact_id, source_path = create_optimized(case, personal_session)
    source_bytes = source_path.read_bytes()

    response = review(owner, artifact_id)

    assert response.status_code == 201, response.get_json()
    assert source_path.read_bytes() == source_bytes
    with case.database.snapshot_session() as session:
        child = session.get(Artifact, response.get_json()["id"])
        assert child is not None
        assert child.session_id == personal_session.id
        assert child.metadata_json["reviewed_from"] == artifact_id


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("unknown", 404),
        ("outsider", 404),
        ("instance_admin_only", 404),
        ("project_viewer", 403),
        ("project_editor", 403),
        ("restricted_token", 403),
        ("restricted_project_grant", 404),
        ("deleted", 404),
        ("archived", 404),
        ("rootless", 404),
    ],
)
def test_denials_precede_optimization_source_file_io(case, monkeypatch, scenario, status):
    if scenario == "rootless":
        artifact_id, source_path = create_rootless_optimized(case)
    else:
        artifact_id, source_path = create_optimized(case, case.a)

    if scenario == "unknown":
        actor = credential(case, "member")
        artifact_id = new_id()
    elif scenario == "outsider":
        with case.database.immediate_session() as session:
            session.get(User, case.users["outsider"]).is_instance_admin = False
        actor = credential(case, "outsider")
    elif scenario == "instance_admin_only":
        actor = credential(
            case,
            "outsider",
            scopes=SCOPES | {"app.admin"},
            grant_team=False,
        )
    elif scenario == "project_viewer":
        actor = credential(case, "viewer")
    elif scenario == "project_editor":
        actor = credential(case, "editor")
    elif scenario == "restricted_token":
        actor = credential(case, "member", scopes=frozenset({"app.read"}))
    elif scenario == "restricted_project_grant":
        actor = credential(case, "member", grant_team=False)
    elif scenario == "deleted":
        actor = credential(case, "member")
        with case.database.immediate_session() as session:
            session.get(Artifact, artifact_id).state = "deleted"
    elif scenario == "archived":
        actor = credential(case, "member")
        with case.database.immediate_session() as session:
            session.get(Workspace, case.team).state = "archived"
    else:
        actor = credential(case, "member")

    directory = source_path.parent
    before_files = file_inventory(directory)
    before_artifacts = artifact_ids(case)
    resolved, reads = watch_source_io(monkeypatch, case, source_path)

    response = review(actor, artifact_id)

    assert response.status_code == status, response.get_json()
    assert resolved == []
    assert reads == []
    assert file_inventory(directory) == before_files
    assert artifact_ids(case) == before_artifacts


def test_revoked_membership_after_authentication_denies_before_source_io(case, monkeypatch):
    member = credential(case, "member")
    artifact_id, source_path = create_optimized(case, case.a)
    original_resolver = case.auth.resolve_api_token
    revoked = False

    def resolve_then_revoke(*args, **kwargs):
        nonlocal revoked
        principal = original_resolver(*args, **kwargs)
        if principal is not None and not revoked:
            with case.database.immediate_session() as session:
                session.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == case.team,
                        WorkspaceMembership.user_id == case.users["member"],
                    )
                )
            revoked = True
        return principal

    monkeypatch.setattr(case.auth, "resolve_api_token", resolve_then_revoke)
    resolved, reads = watch_source_io(monkeypatch, case, source_path)

    response = review(member, artifact_id)

    assert revoked
    assert response.status_code == 404, response.get_json()
    assert resolved == []
    assert reads == []
    assert source_path.exists()


def test_publication_time_membership_revocation_denies_and_cleans(case, monkeypatch):
    member = credential(case, "member")
    artifact_id, source_path = create_optimized(case, case.a)
    source_bytes = source_path.read_bytes()
    session_directory = case.services.artifacts.paths.sessions / case.a.storage_key
    before_files = file_inventory(session_directory)
    before_artifacts = artifact_ids(case)
    original_prepare = case.services.artifacts.prepare_registration
    revoked = False

    def prepare_then_revoke(path, **kwargs):
        nonlocal revoked
        prepared = original_prepare(path, **kwargs)
        if not revoked:
            with case.database.immediate_session() as session:
                session.execute(
                    delete(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == case.team,
                        WorkspaceMembership.user_id == case.users["member"],
                    )
                )
            revoked = True
        return prepared

    monkeypatch.setattr(
        case.services.artifacts,
        "prepare_registration",
        prepare_then_revoke,
    )

    response = review(member, artifact_id)

    assert revoked
    assert response.status_code == 404, response.get_json()
    assert file_inventory(session_directory) == before_files
    assert artifact_ids(case) == before_artifacts
    assert source_path.read_bytes() == source_bytes
