"""Real HTTP evidence rights, CAS receipts and registered named-user execution."""

from __future__ import annotations

import io
import json
import re
import socket
import wave
from copy import deepcopy
from inspect import unwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from pandrator.web import models as m
from pandrator.web.accounts.credential_resources import CredentialResources
from pandrator.web.accounts.execution_actor import UserActor, actor_from_storage
from pandrator.web.jobs import Worker
from tests.test_account_authentication_integration import AuthCase
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_project_sharing_routes import login
from tests.test_workspace_routes import create_workspace, invite


@pytest.fixture(autouse=True)
def local_provider_boundary(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("HTTP evidence acceptance must not access the network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    calls = []

    def transcribe(output_dir, _clip, _settings, **kwargs):
        calls.append(str(output_dir))
        if kwargs.get("progress_callback"):
            kwargs["progress_callback"](0.5, "Synthetic provider progress")
        output = Path(output_dir) / "transcript.json"
        output.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "language": "en",
                    "metadata": {"provider": "synthetic", "model": "local-fixture"},
                    "segments": [
                        {
                            "id": "one",
                            "start_ms": 1000,
                            "end_ms": 2000,
                            "text": "Verified private cue",
                            "words": [
                                {"text": "Verified private cue", "start_ms": 1000, "end_ms": 2000}
                            ],
                        }
                    ],
                }
            )
        )
        return SimpleNamespace(
            word_timestamps_path=str(output), engine="whisper", compute_backend="cpu"
        )

    monkeypatch.setattr(
        "pandrator.web.subtitle_evidence.transcribe_source_file_with_metadata", transcribe
    )
    return calls


def body(response, status=200) -> dict[str, Any]:
    value = response.get_json()
    assert response.status_code == status, value
    assert isinstance(value, dict)
    return value


def headers(csrf, key):
    return {**csrf, "Idempotency-Key": key}


def team(case, monkeypatch):
    alice = new_account(case, "alice")
    bob = new_account(case, "bob")
    enable(case, monkeypatch)
    owner, oh = login(case, "owner")
    ac, ah = login(case, "alice")
    bc, bh = login(case, "bob")
    workspace = create_workspace(case, "Evidence team")
    for account, client, csrf in ((alice, ac, ah), (bob, bc, bh)):
        current = body(owner.get(f"/api/v1/workspaces/{workspace['id']}"))
        invitation = invite(case, current, account)
        body(client.post(f"/api/v1/workspace-invitations/{invitation['id']}/accept", headers=csrf))
    return SimpleNamespace(
        owner=owner,
        oh=oh,
        alice=ac,
        ah=ah,
        bob=bc,
        bh=bh,
        alice_id=alice.user_id,
        bob_id=bob.user_id,
        workspace_id=workspace["id"],
    )


def source(case, client, csrf, *, workspace_id=None):
    values = {
        "name": "PRIVATE evidence source",
        "workflow_kind": "subtitles",
        "source_language": "en",
    }
    if workspace_id:
        values["workspace_id"] = workspace_id
    value = body(
        client.post("/api/v1/sessions", json=values, headers=headers(csrf, "evidence-http-source")),
        201,
    )
    uploaded = body(
        client.post(
            "/api/v1/uploads",
            data={
                "session_id": value["id"],
                "file": (
                    io.BytesIO(b"1\n00:00:01,000 --> 00:00:02,000\nPRIVATE original cue\n"),
                    "source.srt",
                ),
            },
            headers=headers(csrf, "evidence-http-upload"),
        ),
        201,
    )
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as session:
        stored = session.get_one(m.SessionRecord, value["id"])
        directory = services.paths.sessions / stored.storage_key
    media_path = directory / "synthetic.wav"
    with wave.open(str(media_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 40000)
    media = services.artifacts.register(
        media_path, kind="wav", role="upload", session_id=value["id"]
    )
    # The HTTP subtitle upload creates a real current source slot. Attach the
    # synthetic recording to that subtitle's provenance through the real registry,
    # rather than relying on the legacy unlinked-media fallback.
    artifact_id = uploaded["attachment"]["subtitle_revision"]["artifact_id"]
    with case.database.snapshot_session() as session:
        subtitle = session.get_one(m.Artifact, artifact_id)
        subtitle_path = services.paths.managed_path(subtitle.relative_path)
        metadata = deepcopy(subtitle.metadata_json)
        kind, role = subtitle.kind, subtitle.role
    services.artifacts.register(
        subtitle_path,
        kind=kind,
        role=role,
        session_id=value["id"],
        parent_ids=[media.id],
        metadata=metadata,
    )
    return SimpleNamespace(
        session_id=value["id"],
        project_id=value["project_id"],
        artifact_id=uploaded["attachment"]["subtitle_revision"]["artifact_id"],
        media_id=media.id,
    )


def request_body(src):
    return {
        "source_artifact_id": src.artifact_id,
        "cue_id": 1,
        "reason": "PRIVATE evidence reason",
        "routes": ["whisper"],
    }


def admit(client, csrf, src, key="evidence-http-request"):
    return body(
        client.post(
            f"/api/v1/sessions/{src.session_id}/subtitle-evidence",
            json=request_body(src),
            headers=headers(csrf, key),
        ),
        202,
    )


def run(case, admitted):
    services = case.app.extensions["pandrator"]["services"]
    # Consume the actual application registry and public WorkflowHandlers adapter.
    registry = services.workflow_handlers.handler_registry
    assert (
        unwrap(registry["subtitle.evidence"])
        == services.workflow_handlers.run_subtitle_evidence
    )
    assert Worker(services.jobs, "evidence-http-worker", registry).run_once()
    job = services.jobs.get(admitted["record"]["job_id"])
    assert job.status == "succeeded", (job.status, job.error_message)
    return job


def get(client, admitted):
    value = body(client.get(f"/api/v1/subtitle-evidence/{admitted['record']['id']}"))
    assert re.fullmatch("[0-9a-f]{64}", value["record"]["revision"])
    return value


def resolve(client, csrf, src, admitted, decision, key):
    return client.post(
        f"/api/v1/sessions/{src.session_id}/subtitle-evidence/{admitted['record']['id']}/resolve",
        json=decision,
        headers=headers(csrf, key),
    )


def raw_evidence(case, evidence_id):
    with case.database.snapshot_session() as session:
        record = session.get_one(m.SubtitleEvidence, evidence_id)
        return deepcopy(
            {
                column.key: getattr(record, column.key)
                for column in m.SubtitleEvidence.__table__.columns
            }
        )


def no_receipt(case, key):
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(m.ApiIdempotency.id).where(m.ApiIdempotency.idempotency_key == key)
            )
            is None
        )


def test_http_admission_runs_actual_registered_handler_and_pins_named_actor(
    case: AuthCase, monkeypatch
):
    members = team(case, monkeypatch)
    src = source(case, members.alice, members.ah, workspace_id=members.workspace_id)
    admitted = admit(members.alice, members.ah, src)
    job = run(case, admitted)
    current = get(members.alice, admitted)
    assert current["record"]["status"] == "completed"
    listing = body(members.bob.get(f"/api/v1/sessions/{src.session_id}/subtitle-evidence"))
    assert listing["items"][0]["record"]["revision"] == current["record"]["revision"]
    assert listing["items"][0]["record"]["id"] == admitted["record"]["id"]
    with case.database.snapshot_session() as session:
        sidecar = session.get_one(m.JobActorRecord, job.id)
        actor = actor_from_storage(sidecar.user_id, sidecar.auth_revision, sidecar.context_json)
        assert type(actor) is UserActor and actor.user_id == members.alice_id
        assert actor.user_id != case.auth.initial_account().user_id
        stored = session.get_one(m.Job, job.id)
        assert (stored.session_id, stored.project_id, stored.workspace_id) == (
            src.session_id,
            src.project_id,
            members.workspace_id,
        )
        assert stored.workflow_run_id is None
        outputs = list(
            session.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == src.session_id,
                    m.Artifact.role.like("subtitle_evidence_%"),
                )
            )
        )
        assert len(outputs) == 2
        assert all(
            (a.project_id, a.workspace_id) == (src.project_id, members.workspace_id)
            for a in outputs
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.JobEvent)
                .where(m.JobEvent.job_id == job.id, m.JobEvent.event_type == "job.succeeded")
            )
            == 1
        )
    services = case.app.extensions["pandrator"]["services"]
    assert all(services.paths.managed_path(a.relative_path).is_file() for a in outputs)
    # Actual duplicate Queue acknowledgement is idempotent after the domain seal.
    services.jobs.complete(
        job.id, "evidence-http-worker", job.result_json, lease_generation=job.lease_generation
    )
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.JobEvent)
                .where(m.JobEvent.job_id == job.id, m.JobEvent.event_type == "job.succeeded")
            )
            == 1
        )


def test_foreign_admin_view_only_and_revocation_fence_old_receipts(case: AuthCase, monkeypatch):
    admin = new_account(case, "foreign-admin", "administrator")
    members = team(case, monkeypatch)
    outsider, xh = login(case, "foreign-admin")
    assert admin.user_id != case.auth.initial_account().user_id
    src = source(case, members.owner, members.oh, workspace_id=members.workspace_id)
    accepted = admit(members.alice, members.ah, src)
    run(case, accepted)
    current = get(members.alice, accepted)
    decision = {
        "action": "accepted",
        "candidate_id": current["record"]["candidates"][0]["id"],
        "expected_revision": current["record"]["revision"],
    }
    body(resolve(members.alice, members.ah, src, accepted, decision, "member-old-decision"))
    get_url = f"/api/v1/subtitle-evidence/{accepted['record']['id']}"
    list_url = f"/api/v1/sessions/{src.session_id}/subtitle-evidence"
    for response in (
        outsider.get(get_url),
        outsider.get(list_url),
        outsider.post(list_url, json=request_body(src), headers=headers(xh, "foreign-request")),
        resolve(outsider, xh, src, accepted, decision, "foreign-decision"),
    ):
        assert response.status_code == 404, response.get_json()
        assert "PRIVATE" not in response.get_data(as_text=True)
    workspace = body(members.owner.get(f"/api/v1/workspaces/{members.workspace_id}"))
    body(
        members.owner.delete(
            f"/api/v1/workspaces/{members.workspace_id}/members/{members.alice_id}",
            json={"expected_revision": workspace["revision"]},
            headers=members.oh,
        )
    )
    viewers = f"/api/v1/projects/{src.project_id}/viewers"
    revision = body(members.owner.get(viewers))["revision"]
    granted = body(
        members.owner.put(
            f"{viewers}/{members.alice_id}",
            json={"expected_revision": revision},
            headers=members.oh,
        )
    )
    assert members.alice.get(get_url).status_code == 200
    assert members.alice.get(list_url).status_code == 200
    for key in ("evidence-http-request", "view-only-request"):
        response = members.alice.post(
            list_url, json=request_body(src), headers=headers(members.ah, key)
        )
        assert response.status_code == 403, response.get_json()
    assert (
        resolve(
            members.alice, members.ah, src, accepted, decision, "member-old-decision"
        ).status_code
        == 403
    )
    assert (
        resolve(
            members.alice, members.ah, src, accepted, decision, "view-only-decision"
        ).status_code
        == 403
    )
    body(
        members.owner.post(
            f"{viewers}/{members.alice_id}/revoke",
            json={"expected_revision": granted["revision"]},
            headers=members.oh,
        )
    )
    for response in (
        members.alice.get(get_url),
        members.alice.get(list_url),
        members.alice.post(
            list_url, json=request_body(src), headers=headers(members.ah, "evidence-http-request")
        ),
        resolve(members.alice, members.ah, src, accepted, decision, "member-old-decision"),
    ):
        assert response.status_code == 404, response.get_json()
        assert "PRIVATE" not in response.get_data(as_text=True)


def test_two_members_use_current_cas_and_exact_retry_precedes_cas(case: AuthCase, monkeypatch):
    members = team(case, monkeypatch)
    src = source(case, members.alice, members.ah, workspace_id=members.workspace_id)
    accepted = admit(members.alice, members.ah, src)
    run(case, accepted)
    original = get(members.alice, accepted)["record"]["revision"]
    first = {"action": "edited", "text": "First member decision", "expected_revision": original}
    first_response = resolve(
        members.alice, members.ah, src, accepted, first, "first-member-decision"
    )
    first_value = body(first_response)
    assert get(members.alice, accepted)["record"] == first_value["record"]
    before = raw_evidence(case, accepted["record"]["id"])
    stale = resolve(
        members.bob,
        members.bh,
        src,
        accepted,
        {"action": "edited", "text": "Stale different decision", "expected_revision": original},
        "stale-second-decision",
    )
    assert body(stale, 409)["error"]["code"] == "evidence_revision_conflict"
    no_receipt(case, "stale-second-decision")
    missing = resolve(
        members.bob,
        members.bh,
        src,
        accepted,
        {"action": "edited", "text": "Missing token"},
        "missing-decision-token",
    )
    assert body(missing, 428)["error"]["code"] == "precondition_required"
    no_receipt(case, "missing-decision-token")
    retry = resolve(members.alice, members.ah, src, accepted, first, "first-member-decision")
    assert body(retry) == first_value and retry.headers["Idempotency-Replayed"] == "true"
    assert raw_evidence(case, accepted["record"]["id"]) == before
    second = {
        "action": "edited",
        "text": "Second member decision",
        "expected_revision": get(members.bob, accepted)["record"]["revision"],
    }
    final = body(resolve(members.bob, members.bh, src, accepted, second, "second-member-decision"))
    assert final["record"]["resolution"]["text"] == "Second member decision"
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(m.AuditEvent)
                .where(
                    m.AuditEvent.action == "subtitle_evidence.resolve",
                    m.AuditEvent.resource_id == accepted["record"]["id"],
                )
            )
            == 2
        )


def test_resolution_audit_failure_rolls_back_body_provenance_and_new_receipt(
    case: AuthCase, monkeypatch
):
    members = team(case, monkeypatch)
    src = source(case, members.alice, members.ah, workspace_id=members.workspace_id)
    accepted = admit(members.alice, members.ah, src)
    run(case, accepted)
    current = get(members.alice, accepted)
    before = raw_evidence(case, accepted["record"]["id"])
    services = case.app.extensions["pandrator"]["services"]
    original = services.audit.record_command_in_session

    def fail_after_real_audit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic evidence audit failure")

    monkeypatch.setattr(services.audit, "record_command_in_session", fail_after_real_audit)
    with pytest.raises(RuntimeError, match="Synthetic evidence audit failure"):
        resolve(
            members.alice,
            members.ah,
            src,
            accepted,
            {
                "action": "edited",
                "text": "Rolled back decision",
                "expected_revision": current["record"]["revision"],
            },
            "audit-rollback-decision",
        )
    assert raw_evidence(case, accepted["record"]["id"]) == before
    no_receipt(case, "audit-rollback-decision")
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(m.AuditEvent.id).where(
                    m.AuditEvent.action == "subtitle_evidence.resolve",
                    m.AuditEvent.resource_id == accepted["record"]["id"],
                )
            )
            is None
        )


def test_denied_worker_start_is_stopped_for_other_reviewers_without_get_writes(
    case: AuthCase, monkeypatch, local_provider_boundary,
):
    members = team(case, monkeypatch)
    src = source(case, members.alice, members.ah, workspace_id=members.workspace_id)
    accepted = admit(members.alice, members.ah, src)
    workspace = body(members.owner.get(f"/api/v1/workspaces/{members.workspace_id}"))
    body(members.owner.delete(
        f"/api/v1/workspaces/{members.workspace_id}/members/{members.alice_id}",
        json={"expected_revision": workspace["revision"]}, headers=members.oh,
    ))
    services = case.app.extensions["pandrator"]["services"]
    assert Worker(
        services.jobs, "denied-evidence-start", services.workflow_handlers.handler_registry,
    ).run_once()
    job = services.jobs.get(accepted["record"]["job_id"])
    assert job.status == "failed" and not local_provider_boundary
    before = raw_evidence(case, accepted["record"]["id"])
    assert before["status"] == "queued"
    current = get(members.bob, accepted)["record"]
    assert current["status"] == "failed"
    assert current["error_message"] == "Evidence processing stopped before completion."
    assert raw_evidence(case, accepted["record"]["id"]) == before
    stopped = body(resolve(
        members.bob, members.bh, src, accepted,
        {"action": "uncertain", "note": "The team will check this recording manually.",
         "expected_revision": current["revision"]}, "decide-after-denied-start",
    ))["record"]
    assert stopped["status"] == "uncertain"
    assert services.jobs.get(job.id).status == "failed"


def test_solo_legacy_resolution_can_omit_expected_revision(case: AuthCase):
    csrf = {"X-CSRF-Token": case.csrf}
    src = source(case, case.client, csrf)
    accepted = admit(case.client, csrf, src)
    run(case, accepted)
    final = body(
        resolve(
            case.client,
            csrf,
            src,
            accepted,
            {"action": "edited", "text": "Legacy solo decision"},
            "solo-decision",
        )
    )
    assert final["record"]["resolution"]["text"] == "Legacy solo decision"
    assert re.fullmatch("[0-9a-f]{64}", final["record"]["revision"])


def test_project_token_read_write_run_and_audio_library_ceilings(case: AuthCase, monkeypatch):
    members = team(case, monkeypatch)
    src = source(case, members.alice, members.ah, workspace_id=members.workspace_id)
    accepted = admit(members.alice, members.ah, src)
    run(case, accepted)
    api = case.app.test_client()

    def token(scopes):
        _, raw = case.auth.create_api_token(
            "Scoped evidence test",
            scopes=frozenset(scopes),
            user_id=members.alice_id,
            resource_grants=CredentialResources(project_ids=frozenset({src.project_id})),
        )
        return {"Authorization": f"Bearer {raw}"}

    read = token({"app.read"})
    assert (
        api.get(f"/api/v1/subtitle-evidence/{accepted['record']['id']}", headers=read).status_code
        == 200
    )
    assert (
        api.get(f"/api/v1/sessions/{src.session_id}/subtitle-evidence", headers=read).status_code
        == 200
    )
    assert (
        api.post(
            f"/api/v1/sessions/{src.session_id}/subtitle-evidence",
            json=request_body(src),
            headers=headers(read, "reader-cannot-request"),
        ).status_code
        == 403
    )
    assert (
        resolve(
            api,
            read,
            src,
            accepted,
            {"action": "edited", "text": "Reader cannot decide"},
            "reader-cannot-resolve",
        ).status_code
        == 403
    )
    write = token({"app.read", "app.write"})
    current = body(api.get(f"/api/v1/subtitle-evidence/{accepted['record']['id']}", headers=write))
    body(
        resolve(
            api,
            write,
            src,
            accepted,
            {
                "action": "edited",
                "text": "Project-scoped write decision",
                "expected_revision": current["record"]["revision"],
            },
            "writer-can-resolve",
        )
    )
    assert (
        api.post(
            f"/api/v1/sessions/{src.session_id}/subtitle-evidence",
            json=request_body(src),
            headers=headers(write, "writer-without-run"),
        ).status_code
        == 403
    )
    full = token({"app.read", "app.write", "app.run"})
    assert body(api.get("/api/v1/workspaces", headers=full))["items"] == []
    with case.database.immediate_session() as session:
        provider = m.Provider(
            workspace_id=members.workspace_id,
            kind="llm",
            provider_key="openai",
            label="Scoped synthetic audio",
        )
        session.add(provider)
        session.flush()
        model = m.ProviderModel(
            provider_id=provider.id,
            model_id="synthetic-audio",
            is_active=True,
            input_modalities_json=["audio"],
        )
        session.add(model)
        session.flush()
        model_id = model.id
    audio = {**request_body(src), "routes": ["audio_llm"], "audio_model_ids": [model_id]}
    denied = api.post(
        f"/api/v1/sessions/{src.session_id}/subtitle-evidence",
        json=audio,
        headers=headers(full, "project-not-library"),
    )
    assert denied.status_code == 404, denied.get_json()
    no_receipt(case, "project-not-library")
    # The same current resource ceiling permits builtin project routes.
    queued = body(
        api.post(
            f"/api/v1/sessions/{src.session_id}/subtitle-evidence",
            json=request_body(src),
            headers=headers(full, "project-builtin-request"),
        ),
        202,
    )
    run(case, queued)
