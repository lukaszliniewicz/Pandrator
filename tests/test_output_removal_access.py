"""HTTP authorization boundaries for deleting session outputs."""

from pathlib import Path

from sqlalchemy import delete

from pandrator.web.accounts.models import User, WorkspaceMembership
from pandrator.web.models import Artifact, ExportRecord, new_id
from tests.test_job_access_routes import SCOPES, credential
from tests.test_job_access_routes import case as case
from tests.test_job_access_routes import no_network as no_network


def create_output(case, record, *, name="exports/final.mp4"):
    paths = case.services.artifacts.paths
    path = paths.sessions / record.storage_key / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"authorized output deletion fixture")
    artifact = case.services.artifacts.register(
        path,
        kind="export",
        role="export_video",
        session_id=record.id,
    )
    with case.database.immediate_session() as session:
        export = ExportRecord(
            session_id=record.id,
            artifact_id=artifact.id,
            options_json={},
            status="completed",
        )
        session.add(export)
        session.flush()
        export_id = export.id
    return artifact.id, path, export_id


def api_error(response):
    payload = dict(response.get_json()["error"])
    payload.pop("request_id", None)
    return payload


def assert_unchanged(case, artifact_id, export_id):
    with case.database.snapshot_session() as session:
        assert session.get(Artifact, artifact_id).state == "current"
        assert session.get(ExportRecord, export_id).status == "completed"


def test_personal_owner_and_team_member_can_delete_own_outputs(case):
    personal_workspace_id = case.personal[case.users["owner"]]
    personal_session = case.services.sessions.create(
        "Owner personal output", workspace_id=personal_workspace_id
    )
    owner = credential(case, "owner", grant_team=False)
    personal_artifact, personal_path, personal_export_id = create_output(
        case, personal_session, name="exports/personal.mp4"
    )

    personal_response = owner.client.delete(
        f"/api/v1/sessions/{personal_session.id}/outputs/{personal_artifact}",
        headers=owner.headers,
    )
    assert personal_response.status_code == 200, personal_response.get_json()
    assert not personal_path.exists()
    with case.database.snapshot_session() as session:
        assert session.get(Artifact, personal_artifact).state == "deleted"
        assert session.get(ExportRecord, personal_export_id).status == "deleted"

    member = credential(case, "member")
    team_artifact, team_path, team_export_id = create_output(case, case.a)
    team_response = member.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{team_artifact}",
        headers=member.headers,
    )
    assert team_response.status_code == 200, team_response.get_json()
    assert not team_path.exists()
    with case.database.snapshot_session() as session:
        assert session.get(Artifact, team_artifact).state == "deleted"
        assert session.get(ExportRecord, team_export_id).status == "deleted"


def test_hidden_ordinary_user_get_then_delete_are_uniform_404_without_effects(case, monkeypatch):
    with case.database.immediate_session() as session:
        session.get(User, case.users["outsider"]).is_instance_admin = False
    outsider = credential(
        case,
        "outsider",
        scopes=frozenset({"app.read", "app.write"}),
    )
    artifact_id, path, export_id = create_output(case, case.a)
    unlinks = []
    original_unlink = Path.unlink

    def unlink_spy(candidate, *args, **kwargs):
        unlinks.append(candidate)
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_spy)
    hidden_get = outsider.client.get(
        f"/api/v1/artifacts/{artifact_id}/context", headers=outsider.headers
    )
    hidden_delete = outsider.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{artifact_id}",
        headers=outsider.headers,
    )

    assert hidden_get.status_code == 404, hidden_get.get_json()
    assert hidden_delete.status_code == 404, hidden_delete.get_json()
    assert not unlinks
    assert path.exists()
    assert_unchanged(case, artifact_id, export_id)


def test_instance_admin_outsider_has_no_private_output_override(case, monkeypatch):
    outsider = credential(case, "outsider", scopes=SCOPES | {"app.admin"})
    artifact_id, path, export_id = create_output(case, case.a)
    unlinks = []
    original_unlink = Path.unlink

    def unlink_spy(candidate, *args, **kwargs):
        unlinks.append(candidate)
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_spy)
    hidden_get = outsider.client.get(
        f"/api/v1/artifacts/{artifact_id}/context", headers=outsider.headers
    )
    hidden_delete = outsider.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{artifact_id}",
        headers=outsider.headers,
    )

    assert hidden_get.status_code == 404, hidden_get.get_json()
    assert hidden_delete.status_code == 404, hidden_delete.get_json()
    assert not unlinks
    assert path.exists()
    assert_unchanged(case, artifact_id, export_id)


def test_visible_viewer_cannot_delete_output(case, monkeypatch):
    viewer = credential(case, "viewer")
    artifact_id, path, export_id = create_output(case, case.a)
    unlinks = []
    original_unlink = Path.unlink

    def unlink_spy(candidate, *args, **kwargs):
        unlinks.append(candidate)
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_spy)
    visible_get = viewer.client.get(
        f"/api/v1/artifacts/{artifact_id}/context", headers=viewer.headers
    )
    denied = viewer.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{artifact_id}",
        headers=viewer.headers,
    )

    assert visible_get.status_code == 200, visible_get.get_json()
    assert denied.status_code == 403, denied.get_json()
    assert not unlinks
    assert path.exists()
    assert_unchanged(case, artifact_id, export_id)


def test_revoked_member_cannot_read_or_delete_output(case, monkeypatch):
    member = credential(case, "member")
    artifact_id, path, export_id = create_output(case, case.a)
    with case.database.immediate_session() as session:
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == case.team,
                WorkspaceMembership.user_id == case.users["member"],
            )
        )
    unlinks = []
    original_unlink = Path.unlink

    def unlink_spy(candidate, *args, **kwargs):
        unlinks.append(candidate)
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_spy)
    hidden_get = member.client.get(
        f"/api/v1/artifacts/{artifact_id}/context", headers=member.headers
    )
    hidden_delete = member.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{artifact_id}",
        headers=member.headers,
    )

    assert hidden_get.status_code == 404, hidden_get.get_json()
    assert hidden_delete.status_code == 404, hidden_delete.get_json()
    assert not unlinks
    assert path.exists()
    assert_unchanged(case, artifact_id, export_id)


def test_token_resource_scope_does_not_expand_to_team_output(case, monkeypatch):
    member = credential(case, "member", grant_team=False)
    artifact_id, path, export_id = create_output(case, case.a)
    unlinks = []
    original_unlink = Path.unlink

    def unlink_spy(candidate, *args, **kwargs):
        unlinks.append(candidate)
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_spy)
    hidden_get = member.client.get(
        f"/api/v1/artifacts/{artifact_id}/context", headers=member.headers
    )
    hidden_delete = member.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{artifact_id}",
        headers=member.headers,
    )

    assert hidden_get.status_code == 404, hidden_get.get_json()
    assert hidden_delete.status_code == 404, hidden_delete.get_json()
    assert not unlinks
    assert path.exists()
    assert_unchanged(case, artifact_id, export_id)


def test_unknown_and_wrong_session_deletes_have_the_same_404(case):
    member = credential(case, "member")
    artifact_id, path, _export_id = create_output(case, case.a)
    sibling = case.services.sessions.create(
        "Same project sibling",
        project_id=case.a.project_id,
        workspace_id=case.team,
    )
    sibling_artifact, sibling_path, _sibling_export_id = create_output(
        case, sibling, name="exports/sibling.mp4"
    )

    unknown = member.client.delete(
        f"/api/v1/sessions/{new_id()}/outputs/{artifact_id}",
        headers=member.headers,
    )
    wrong_session = member.client.delete(
        f"/api/v1/sessions/{case.a.id}/outputs/{sibling_artifact}",
        headers=member.headers,
    )

    assert unknown.status_code == wrong_session.status_code == 404, {
        "unknown": unknown.get_json(),
        "wrong_session": wrong_session.get_json(),
    }
    assert api_error(unknown) == api_error(wrong_session)
    assert path.exists()
    assert sibling_path.exists()
