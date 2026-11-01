"""Exercise a real two-person subtitle collaboration through durable exports."""

from __future__ import annotations

import io
import socket

from sqlalchemy import func, select

from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.jobs import Worker
from pandrator.web.models import (
    ApiIdempotency,
    Artifact,
    Document,
    DocumentRevision,
    Job,
    Project,
    SessionRecord,
    TranslationProject,
)
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.subtitle_sources import parse_subtitle_source
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import PASSWORD, enable, new_account

SOURCE_SRT = (
    "1\n00:00:01,000 --> 00:00:03,500\nWelcome to the shared course.\n\n"
    "2\n00:00:06,000 --> 00:00:09,000\n"
    "We review each translation together.\n"
)

# These intervals fit the default 20 CPS export profile and avoid cue regrouping.
# This test covers manual branch translation and authorization, not renderer defaults.


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


def _active_transcription(case, session_id):
    with case.database.snapshot_session() as db:
        document = db.scalar(
            select(Document).where(
                Document.session_id == session_id,
                Document.stage == "transcription",
            )
        )
        assert document is not None and document.active_revision_id is not None
        revision = db.get(DocumentRevision, document.active_revision_id)
        assert revision is not None
        artifacts = list(
            db.scalars(
                select(Artifact).where(
                    Artifact.session_id == session_id,
                    Artifact.role == "transcription",
                    Artifact.state != "deleted",
                )
            )
        )
        matching = [
            artifact
            for artifact in artifacts
            if (artifact.metadata_json or {}).get("revision_id") == revision.id
        ]
        assert len(matching) == 1
        return revision.revision_number, matching[0].id


def _translation_project_state(case, project_id):
    with case.database.snapshot_session() as db:
        project = db.get(TranslationProject, project_id)
        assert project is not None
        root_project = db.get(Project, project.project_root_id)
        assert root_project is not None
        receipt_count = db.scalar(select(func.count()).select_from(ApiIdempotency))
        return project.revision, root_project.revision, receipt_count


def _assert_srt(client, artifact_id, expected):
    response = client.get(f"/api/v1/artifacts/{artifact_id}/content")
    assert response.status_code == 200, response.get_json()
    content = response.data.decode("utf-8-sig")
    cues = parse_subtitle_source(content, "srt")
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == expected
    return response.data


def _run_export(case, client, csrf_headers, session_id, expected_user_id, key):
    queued_response = client.post(
        f"/api/v1/sessions/{session_id}/stages/export/run",
        json={
            "output": {
                "export_mode": "subtitles",
                "subtitle_format": "srt",
                "subtitle_selection": "source",
            }
        },
        headers=_headers(csrf_headers, key),
    )
    assert queued_response.status_code == 202, queued_response.get_json()
    queued = queued_response.get_json()
    assert queued["kind"] == "export.create"

    extension = case.app.extensions["pandrator"]
    worker = Worker(
        extension["jobs"],
        f"two-person-export-{key}",
        extension["workflow_handlers"].handlers(),
    )
    assert worker.run_once()

    with case.database.snapshot_session() as db:
        job = db.get(Job, queued["id"])
        actor = db.get(JobActorRecord, queued["id"])
        assert job is not None and job.status == "succeeded", (
            job.error_code if job else None,
            job.error_message if job else None,
        )
        assert actor is not None and actor.user_id == expected_user_id
        artifact_ids = (job.result_json or {}).get("artifact_ids")
        assert artifact_ids and len(artifact_ids) == 1
        artifact_id = artifact_ids[0]
    return artifact_id


def test_two_people_translate_export_and_continue_after_creator_leaves(
    case, monkeypatch
):
    alice_account = new_account(case, login="alice")
    enable(case, monkeypatch)

    def block_network(self, *_args, **_kwargs):
        raise AssertionError("Network access is blocked in this acceptance test.")

    monkeypatch.setattr(socket.socket, "connect", block_network)
    owner_client, owner_csrf, owner_id = _login(case, "owner")
    alice_client, alice_csrf, alice_id = _login(case, "alice")
    assert owner_id == case.auth.initial_account().user_id
    assert alice_id == alice_account.user_id

    team_response = owner_client.post(
        "/api/v1/workspaces",
        json={"name": "Shared subtitles"},
        headers=_headers(owner_csrf, "two-person-workspace"),
    )
    assert team_response.status_code == 201, team_response.get_json()
    team = team_response.get_json()
    team_id = team["id"]

    invitation_response = owner_client.post(
        f"/api/v1/workspaces/{team_id}/invitations",
        json={
            "user_id": alice_id,
            "role": "member",
            "expected_revision": team["revision"],
        },
        headers=_headers(owner_csrf, "two-person-invite-alice"),
    )
    assert invitation_response.status_code == 201, invitation_response.get_json()
    invitation_id = invitation_response.get_json()["id"]
    accepted = alice_client.post(
        f"/api/v1/workspace-invitations/{invitation_id}/accept",
        json={},
        headers=_headers(alice_csrf, "two-person-accept-invite"),
    )
    assert accepted.status_code == 200, accepted.get_json()

    source_response = owner_client.post(
        "/api/v1/sessions",
        json={
            "name": "Shared English source",
            "workflow_kind": "subtitles",
            "source_language": "en",
            "workspace_id": team_id,
        },
        headers=_headers(owner_csrf, "two-person-create-source"),
    )
    assert source_response.status_code == 201, source_response.get_json()
    source_session_id = source_response.get_json()["id"]

    source_bytes = SOURCE_SRT.encode("utf-8")
    assert len(source_bytes) < 1024
    upload_response = owner_client.post(
        "/api/v1/uploads",
        data={
            "session_id": source_session_id,
            "file": (io.BytesIO(source_bytes), "shared-source.srt"),
        },
        headers=_headers(owner_csrf, "two-person-upload-source"),
    )
    assert upload_response.status_code == 201, upload_response.get_json()
    uploaded = upload_response.get_json()
    source_checkpoint_id = uploaded["attachment"]["subtitle_revision"]["artifact_id"]
    assert _assert_srt(
        owner_client,
        source_checkpoint_id,
        [
            (1000, 3500, "Welcome to the shared course."),
            (6000, 9000, "We review each translation together."),
        ],
    )

    source_detail = owner_client.get(
        f"/api/v1/sessions/{source_session_id}"
    ).get_json()
    create_project_payload = {
        "checkpoint_artifact_id": source_checkpoint_id,
        "expected_revision": source_detail["revision"],
    }
    create_project_headers = _headers(
        owner_csrf, "two-person-create-translation-project"
    )
    project_response = owner_client.post(
        f"/api/v1/sessions/{source_session_id}/translation-project",
        json=create_project_payload,
        headers=create_project_headers,
    )
    assert project_response.status_code == 200, project_response.get_json()
    project = project_response.get_json()["project"]

    create_branches_payload = {
        "expected_revision": project["revision"],
        "targets": [{"target_language": "de"}],
    }
    create_branches_headers = _headers(
        owner_csrf, "two-person-create-german-branch"
    )
    branches_response = owner_client.post(
        f"/api/v1/translation-projects/{project['id']}/branches",
        json=create_branches_payload,
        headers=create_branches_headers,
    )
    assert branches_response.status_code == 200, branches_response.get_json()
    branch = branches_response.get_json()["project"]["branches"][0]
    assert branch["target_language"] == "de"
    branch_session_id = branch["session_id"]

    branch_session = alice_client.get(
        f"/api/v1/sessions/{branch_session_id}"
    ).get_json()
    assert branch_session["workspace_id"] == team_id
    initial_transcription_revision, _initial_branch_checkpoint_id = _active_transcription(
        case, branch_session_id
    )
    review_url = (
        f"/api/v1/sessions/{branch_session_id}/subtitles/transcription/review"
    )
    review_payload = {
        "expected_revision": initial_transcription_revision,
        "segments": [
            {
                "start_ms": 1000,
                "end_ms": 3500,
                "text": "Willkommen zum gemeinsamen Kurs.",
            },
            {
                "start_ms": 6000,
                "end_ms": 9000,
                "text": "Wir prüfen jede Übersetzung gemeinsam.",
            },
        ],
    }
    reviewed = alice_client.post(
        review_url,
        json=review_payload,
        headers=_headers(alice_csrf, "two-person-review-german-branch"),
    )
    assert reviewed.status_code == 201, reviewed.get_json()
    current_transcription_revision, reviewed_artifact_id = _active_transcription(
        case, branch_session_id
    )
    assert current_transcription_revision > initial_transcription_revision
    assert _assert_srt(
        alice_client,
        reviewed_artifact_id,
        [
            (1000, 3500, "Willkommen zum gemeinsamen Kurs."),
            (6000, 9000, "Wir prüfen jede Übersetzung gemeinsam."),
        ],
    )

    stale_edit = owner_client.post(
        review_url,
        json={
            **review_payload,
            "segments": [
                {
                    "start_ms": 1000,
                    "end_ms": 3500,
                    "text": "Stale edit must not replace Alice's work.",
                },
                {
                    "start_ms": 6000,
                    "end_ms": 9000,
                    "text": "This stale line must also be rejected.",
                },
            ],
        },
        headers=_headers(owner_csrf, "two-person-stale-branch-edit"),
    )
    assert stale_edit.status_code == 409, stale_edit.get_json()
    assert _active_transcription(case, branch_session_id) == (
        current_transcription_revision,
        reviewed_artifact_id,
    )
    _assert_srt(
        alice_client,
        reviewed_artifact_id,
        [
            (1000, 3500, "Willkommen zum gemeinsamen Kurs."),
            (6000, 9000, "Wir prüfen jede Übersetzung gemeinsam."),
        ],
    )

    assert _assert_srt(
        owner_client,
        source_checkpoint_id,
        [
            (1000, 3500, "Welcome to the shared course."),
            (6000, 9000, "We review each translation together."),
        ],
    )
    source_export_id = _run_export(
        case,
        owner_client,
        owner_csrf,
        source_session_id,
        owner_id,
        "two-person-export-english-source",
    )
    assert _assert_srt(
        owner_client,
        source_export_id,
        [
            (1000, 3500, "Welcome to the shared course."),
            (6000, 9000, "We review each translation together."),
        ],
    )

    german_export_id = _run_export(
        case,
        alice_client,
        alice_csrf,
        branch_session_id,
        alice_id,
        "two-person-export-german-branch",
    )
    assert _assert_srt(
        alice_client,
        german_export_id,
        [
            (1000, 3500, "Willkommen zum gemeinsamen Kurs."),
            (6000, 9000, "Wir prüfen jede Übersetzung gemeinsam."),
        ],
    )

    with case.database.snapshot_session() as db:
        project_ids = set(
            db.scalars(
                select(SessionRecord.project_id).where(
                    SessionRecord.id.in_([source_session_id, branch_session_id])
                )
            )
        )
        membership_grants = db.scalar(
            select(func.count())
            .select_from(ProjectMembership)
            .where(
                ProjectMembership.project_id.in_(project_ids),
                ProjectMembership.user_id.in_([owner_id, alice_id]),
            )
        )
        assert membership_grants == 0

    workspace_detail = owner_client.get(
        f"/api/v1/workspaces/{team_id}"
    ).get_json()
    promoted = owner_client.patch(
        f"/api/v1/workspaces/{team_id}/members/{alice_id}",
        json={"role": "manager", "expected_revision": workspace_detail["revision"]},
        headers=_headers(owner_csrf, "two-person-promote-alice"),
    )
    assert promoted.status_code == 200, promoted.get_json()
    departed = owner_client.post(
        f"/api/v1/workspaces/{team_id}/leave",
        json={"expected_revision": promoted.get_json()["revision"]},
        headers=_headers(owner_csrf, "two-person-owner-leaves"),
    )
    assert departed.status_code == 200, departed.get_json()

    assert owner_client.get(f"/api/v1/sessions/{source_session_id}").status_code == 404
    assert owner_client.get(
        f"/api/v1/artifacts/{german_export_id}/content"
    ).status_code == 404
    assert alice_client.get(f"/api/v1/sessions/{branch_session_id}").status_code == 200
    assert alice_client.get(
        f"/api/v1/translation-projects/{project['id']}"
    ).status_code == 200

    source_url = f"/api/v1/sessions/{source_session_id}/translation-project"
    branch_url = f"/api/v1/sessions/{branch_session_id}/translation-project"
    project_url = f"/api/v1/translation-projects/{project['id']}"
    for view in ("full", "compact"):
        assert owner_client.get(f"{source_url}?view={view}").status_code == 404
        assert owner_client.get(f"{branch_url}?view={view}").status_code == 404
        assert owner_client.get(f"{project_url}?view={view}").status_code == 404

    current_project = alice_client.get(project_url).get_json()["project"]
    current_source = alice_client.get(
        f"/api/v1/sessions/{source_session_id}"
    ).get_json()
    state_before_denied_mutations = _translation_project_state(case, project["id"])
    assert owner_client.post(
        source_url,
        json=create_project_payload,
        headers=create_project_headers,
    ).status_code == 404
    assert owner_client.post(
        f"{project_url}/branches",
        json=create_branches_payload,
        headers=create_branches_headers,
    ).status_code == 404
    assert owner_client.patch(
        project_url,
        json={
            "expected_revision": current_project["revision"],
            "name": "Former owner cannot rename",
        },
        headers=_headers(owner_csrf, "two-person-former-owner-rename"),
    ).status_code == 404
    assert owner_client.post(
        f"{project_url}/source",
        json={
            "expected_revision": current_project["revision"],
            "expected_source_revision": current_source["revision"],
            "checkpoint_artifact_id": source_checkpoint_id,
        },
        headers=_headers(owner_csrf, "two-person-former-owner-source"),
    ).status_code == 404
    assert owner_client.post(
        f"{project_url}/branches",
        json={
            "expected_revision": current_project["revision"],
            "targets": [{"target_language": "fr"}],
        },
        headers=_headers(owner_csrf, "two-person-former-owner-branch"),
    ).status_code == 404
    assert _translation_project_state(case, project["id"]) == state_before_denied_mutations

    latest_revision, latest_artifact_id = _active_transcription(case, branch_session_id)
    update_payload = {
        "expected_revision": latest_revision,
        "segments": [
            {
                "start_ms": 1000,
                "end_ms": 3500,
                "text": "Willkommen in unserem gemeinsamen Kurs.",
            },
            {
                "start_ms": 6000,
                "end_ms": 9000,
                "text": "Wir besprechen jede Übersetzung gemeinsam.",
            },
        ],
    }
    updated = alice_client.post(
        review_url,
        json=update_payload,
        headers=_headers(alice_csrf, "two-person-post-departure-edit"),
    )
    assert updated.status_code == 201, updated.get_json()
    new_revision, new_artifact_id = _active_transcription(case, branch_session_id)
    assert new_revision > latest_revision
    assert new_artifact_id != latest_artifact_id

    final_export_id = _run_export(
        case,
        alice_client,
        alice_csrf,
        branch_session_id,
        alice_id,
        "two-person-post-departure-export",
    )
    assert _assert_srt(
        alice_client,
        final_export_id,
        [
            (1000, 3500, "Willkommen in unserem gemeinsamen Kurs."),
            (6000, 9000, "Wir besprechen jede Übersetzung gemeinsam."),
        ],
    )
    assert owner_client.get(
        f"/api/v1/translation-projects/{project['id']}"
    ).status_code == 404
