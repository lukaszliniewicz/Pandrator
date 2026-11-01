"""Translation aggregates follow fresh project access at every HTTP entry point."""

from __future__ import annotations

import io

from sqlalchemy import func, select

from pandrator.web.models import (
    ApiIdempotency,
    Artifact,
    Document,
    DocumentRevision,
    Project,
    ProjectMembership,
    SessionRecord,
    SessionSetting,
    TranslationProject,
    TranslationProjectBranch,
)
from pandrator.web.subtitle_sources import parse_subtitle_source
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import PASSWORD, enable, new_account

SOURCE_SRT = (
    "1\n00:00:01,000 --> 00:00:03,500\n"
    "Private source line for translation access.\n\n"
    "2\n00:00:06,000 --> 00:00:09,000\n"
    "A second synthetic source cue.\n"
)


def _login(case, login_name):
    client = case.app.test_client()
    response = client.post(
        "/api/v1/auth/login",
        json={"login_name": login_name, "password": PASSWORD},
    )
    assert response.status_code == 200, response.get_json()
    status = client.get("/api/v1/auth/status").get_json()
    assert status["principal"] is not None
    return client, {"X-CSRF-Token": response.get_json()["csrf_token"]}, status[
        "principal"
    ]["user_id"]


def _headers(csrf_headers, key):
    return {**csrf_headers, "Idempotency-Key": key}


def _aggregate_read_urls(project_id, source_session_id, branch_session_id):
    urls = []
    for view in ("full", "compact"):
        urls.extend(
            (
                f"/api/v1/translation-projects/{project_id}?view={view}",
                f"/api/v1/sessions/{source_session_id}/translation-project?view={view}",
                f"/api/v1/sessions/{branch_session_id}/translation-project?view={view}",
            )
        )
    return urls


def _assert_not_found_without_project_details(response, private_values):
    assert response.status_code == 404, response.get_json()
    body = response.get_data(as_text=True)
    for value in private_values:
        assert value not in body
    for key in ("name", "checkpoint_artifact_id", "settings"):
        assert f'"{key}"' not in body


def _database_state(case, translation_project_id, root_project_id):
    models = (
        ApiIdempotency,
        Artifact,
        Document,
        DocumentRevision,
        Project,
        ProjectMembership,
        SessionRecord,
        SessionSetting,
        TranslationProject,
        TranslationProjectBranch,
    )
    with case.database.snapshot_session() as db:
        translation_project = db.get(TranslationProject, translation_project_id)
        root_project = db.get(Project, root_project_id)
        assert translation_project is not None
        assert root_project is not None
        counts = {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in models
        }
        return translation_project.revision, root_project.revision, counts


def _managed_entries(paths):
    entries = set()
    for root in (paths.sessions, paths.artifacts):
        if root.exists():
            entries.update(
                path.relative_to(paths.root).as_posix() for path in root.rglob("*")
            )
    return frozenset(entries)


def test_translation_project_routes_enforce_private_project_membership(case, monkeypatch):
    alice_account = new_account(case, login="alice")
    enable(case, monkeypatch)
    owner_client, owner_csrf, owner_id = _login(case, "owner")
    alice_client, alice_csrf, alice_id = _login(case, "alice")
    assert owner_id == case.auth.initial_account().user_id
    assert alice_id == alice_account.user_id

    source_name = "Private source: translation access sentinel"
    source_response = owner_client.post(
        "/api/v1/sessions",
        json={
            "name": source_name,
            "workflow_kind": "subtitles",
            "source_language": "en",
        },
        headers=_headers(owner_csrf, "translation-access-create-source"),
    )
    assert source_response.status_code == 201, source_response.get_json()
    source_session_id = source_response.get_json()["id"]

    source_bytes = SOURCE_SRT.encode("utf-8")
    upload_response = owner_client.post(
        "/api/v1/uploads",
        data={
            "session_id": source_session_id,
            "file": (io.BytesIO(source_bytes), "private-source.srt"),
        },
        headers=_headers(owner_csrf, "translation-access-upload-source"),
    )
    assert upload_response.status_code == 201, upload_response.get_json()
    checkpoint_artifact_id = upload_response.get_json()["attachment"][
        "subtitle_revision"
    ]["artifact_id"]
    checkpoint_response = owner_client.get(
        f"/api/v1/artifacts/{checkpoint_artifact_id}/content"
    )
    assert checkpoint_response.status_code == 200
    cues = parse_subtitle_source(checkpoint_response.data.decode("utf-8-sig"), "srt")
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (1000, 3500, "Private source line for translation access."),
        (6000, 9000, "A second synthetic source cue."),
    ]

    source_detail = owner_client.get(
        f"/api/v1/sessions/{source_session_id}"
    ).get_json()
    project_name = "Private translation aggregate sentinel"
    create_project_response = owner_client.post(
        f"/api/v1/sessions/{source_session_id}/translation-project",
        json={
            "checkpoint_artifact_id": checkpoint_artifact_id,
            "name": project_name,
            "expected_revision": source_detail["revision"],
        },
        headers=_headers(owner_csrf, "translation-access-create-project"),
    )
    assert create_project_response.status_code == 200, create_project_response.get_json()
    project = create_project_response.get_json()["project"]
    project_id = project["id"]

    branch_name = "Private German branch sentinel"
    branches_response = owner_client.post(
        f"/api/v1/translation-projects/{project_id}/branches",
        json={
            "expected_revision": project["revision"],
            "targets": [{"target_language": "de", "name": branch_name}],
        },
        headers=_headers(owner_csrf, "translation-access-create-branch"),
    )
    assert branches_response.status_code == 200, branches_response.get_json()
    branch_session_id = branches_response.get_json()["project"]["branches"][0][
        "session_id"
    ]
    with case.database.snapshot_session() as db:
        translation_project = db.get(TranslationProject, project_id)
        assert translation_project is not None
        root_project_id = translation_project.project_root_id

    private_values = (
        source_name,
        project_name,
        branch_name,
        checkpoint_artifact_id,
    )
    read_urls = _aggregate_read_urls(project_id, source_session_id, branch_session_id)
    for url in read_urls:
        _assert_not_found_without_project_details(alice_client.get(url), private_values)

    with case.database.immediate_session() as db:
        db.add(
            ProjectMembership(
                project_id=root_project_id,
                user_id=alice_id,
                role="viewer",
                granted_by_user_id=owner_id,
            )
        )

    for url in read_urls:
        response = alice_client.get(url)
        assert response.status_code == 200, response.get_json()

    current_source = owner_client.get(
        f"/api/v1/sessions/{source_session_id}"
    ).get_json()
    current_project = owner_client.get(
        f"/api/v1/translation-projects/{project_id}"
    ).get_json()["project"]
    state_before_denied_mutations = _database_state(case, project_id, root_project_id)
    entries_before_denied_mutations = _managed_entries(
        case.app.extensions["pandrator"]["paths"]
    )
    source_create_attempt = alice_client.post(
        f"/api/v1/sessions/{source_session_id}/translation-project",
        json={
            "checkpoint_artifact_id": checkpoint_artifact_id,
            "name": "Forbidden duplicate aggregate",
            "expected_revision": current_source["revision"],
        },
        headers=_headers(alice_csrf, "translation-access-viewer-create"),
    )
    source_update_attempt = alice_client.post(
        f"/api/v1/translation-projects/{project_id}/source",
        json={
            "expected_revision": current_project["revision"],
            "expected_source_revision": current_source["revision"],
            "checkpoint_artifact_id": checkpoint_artifact_id,
        },
        headers=_headers(alice_csrf, "translation-access-viewer-source"),
    )
    branch_create_attempt = alice_client.post(
        f"/api/v1/translation-projects/{project_id}/branches",
        json={
            "expected_revision": current_project["revision"],
            "targets": [{"target_language": "fr"}],
        },
        headers=_headers(alice_csrf, "translation-access-viewer-branch"),
    )
    rename_attempt = alice_client.patch(
        f"/api/v1/translation-projects/{project_id}",
        json={
            "expected_revision": current_project["revision"],
            "name": "Forbidden viewer rename",
        },
        headers=_headers(alice_csrf, "translation-access-viewer-rename"),
    )
    for response in (
        source_create_attempt,
        source_update_attempt,
        branch_create_attempt,
        rename_attempt,
    ):
        assert response.status_code == 403, response.get_json()

    state_after_denied_mutations = _database_state(case, project_id, root_project_id)
    entries_after_denied_mutations = _managed_entries(
        case.app.extensions["pandrator"]["paths"]
    )
    assert state_after_denied_mutations == state_before_denied_mutations
    assert entries_after_denied_mutations == entries_before_denied_mutations

    with case.database.immediate_session() as db:
        membership = db.get(ProjectMembership, (root_project_id, alice_id))
        assert membership is not None and membership.role == "viewer"
        db.delete(membership)

    for url in read_urls:
        _assert_not_found_without_project_details(alice_client.get(url), private_values)
