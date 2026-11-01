"""Public copy commands feed a durable named-user edit/export handoff."""

from __future__ import annotations

import hashlib
import io
import json
import socket
import wave
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from werkzeug.test import TestResponse

from pandrator.web.access.contracts import AccessContext
from pandrator.web.accounts.actor_models import JobActorRecord
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.jobs import Worker
from pandrator.web.models import (
    Artifact,
    Document,
    Job,
    JobEvent,
    SessionRecord,
    SubtitleEvidence,
    TranslationProjectBranch,
)
from pandrator.web.projects.copy_publication import ProjectCopyResult
from pandrator.web.projects.copy_snapshot import capture_current_project
from pandrator.web.projects.models import ProjectMembership
from tests.project_copy_test_support import seed_copy_model, snapshot_destination_connections
from tests.test_account_authentication_integration import (
    AuthCase,
    current_browser_principal,
)
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_two_person_collaboration_flow import (
    SOURCE_SRT,
    _active_transcription,
    _assert_srt,
    _headers,
    _login,
    _run_export,
)


def _body(response: TestResponse, expected: int) -> dict[str, Any]:
    payload = response.get_json()
    assert response.status_code == expected, payload
    assert isinstance(payload, dict)
    return payload


def _saved_evidence(case, client, csrf, session_id, artifact_id, text, monkeypatch):
    """Real admission/registered worker/decision with only the recognizer stubbed."""
    services = case.app.extensions["pandrator"]["services"]
    with case.database.snapshot_session() as db:
        source = db.get_one(SessionRecord, session_id)
        directory = services.paths.sessions / source.storage_key
        subtitle = db.get_one(Artifact, artifact_id)
        path = services.paths.managed_path(subtitle.relative_path)
        metadata, kind, role = deepcopy(subtitle.metadata_json), subtitle.kind, subtitle.role
    media_path = directory / "copy-evidence-source.wav"
    with wave.open(str(media_path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\0\0" * 48000)
    media = services.artifacts.register(media_path, kind="wav", role="upload", session_id=session_id)
    services.artifacts.register(
        path, kind=kind, role=role, session_id=session_id, parent_ids=[media.id], metadata=metadata,
    )

    def recognize(output_dir, _clip, _settings, **_kwargs):
        output = Path(output_dir) / "transcript.json"
        output.write_text(json.dumps({
            "schema": "pandrator.transcript.v1", "language": "de",
            "metadata": {"provider": "synthetic", "model": "local-copy-fixture"},
            "segments": [{"id": "cue-one", "start_ms": 1000, "end_ms": 3500,
                          "text": text, "words": [{"text": text, "start_ms": 1000, "end_ms": 3500}]}],
        }))
        return SimpleNamespace(word_timestamps_path=str(output), engine="whisper", compute_backend="cpu")

    monkeypatch.setattr("pandrator.web.subtitle_evidence.transcribe_source_file_with_metadata", recognize)
    admitted = _body(client.post(
        f"/api/v1/sessions/{session_id}/subtitle-evidence",
        json={"source_artifact_id": artifact_id, "cue_id": 1,
              "reason": "Check the saved German wording.", "routes": ["whisper"]},
        headers=_headers(csrf, "copy-flow-evidence-request"),
    ), 202)
    worker = Worker(services.jobs, "copy-flow-evidence-worker", {
        "subtitle.evidence": services.workflow_handlers.handler_registry["subtitle.evidence"],
    })
    assert worker.run_once()
    job = services.jobs.get(admitted["record"]["job_id"])
    assert job.status == "succeeded", job.error_message
    evidence_id = admitted["record"]["id"]
    current = _body(client.get(f"/api/v1/subtitle-evidence/{evidence_id}"), 200)["record"]
    return _body(client.post(
        f"/api/v1/sessions/{session_id}/subtitle-evidence/{evidence_id}/resolve",
        json={"action": "accepted", "candidate_id": current["candidates"][0]["id"],
              "note": "Verified against the recording.", "expected_revision": current["revision"]},
        headers=_headers(csrf, "copy-flow-evidence-decision"),
    ), 200)["record"]


def test_private_multilingual_copy_survives_creator_departure(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_account(case, login="alice")
    enable(case, monkeypatch)

    def blocked_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("The copy handoff must not access the network.")

    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    owner, owner_csrf, owner_id = _login(case, "owner")
    alice, alice_csrf, alice_id = _login(case, "alice")
    services = case.app.extensions["pandrator"]["services"]
    source = _body(
        owner.post(
            "/api/v1/sessions",
            json={
                "name": "Private multilingual lesson",
                "workflow_kind": "subtitles",
                "source_language": "en",
            },
            headers=_headers(owner_csrf, "copy-flow-source"),
        ),
        201,
    )
    source_id, original_project_id = source["id"], source["project_id"]
    uploaded = _body(
        owner.post(
            "/api/v1/uploads",
            data={
                "session_id": source_id,
                "file": (io.BytesIO(SOURCE_SRT.encode()), "private-source.srt"),
            },
            headers=_headers(owner_csrf, "copy-flow-upload"),
        ),
        201,
    )
    checkpoint_id = uploaded["attachment"]["subtitle_revision"]["artifact_id"]
    current = _body(owner.get(f"/api/v1/sessions/{source_id}"), 200)
    project = _body(
        owner.post(
            f"/api/v1/sessions/{source_id}/translation-project",
            json={
                "checkpoint_artifact_id": checkpoint_id,
                "expected_revision": current["revision"],
            },
            headers=_headers(owner_csrf, "copy-flow-translation-group"),
        ),
        200,
    )["project"]
    project = _body(
        owner.post(
            f"/api/v1/translation-projects/{project['id']}/branches",
            json={
                "expected_revision": project["revision"],
                "targets": [{"target_language": "de"}, {"target_language": "pl"}],
            },
            headers=_headers(owner_csrf, "copy-flow-language-versions"),
        ),
        200,
    )["project"]
    original_branches = {
        item["target_language"]: item["session_id"] for item in project["branches"]
    }
    german_id = original_branches["de"]
    revision, _checkpoint = _active_transcription(case, german_id)
    german_cues: list[dict[str, Any]] = [
        {"start_ms": 1000, "end_ms": 3500, "text": "Willkommen zum gemeinsamen Kurs."},
        {"start_ms": 6000, "end_ms": 9000, "text": "Wir prüfen jede Übersetzung gemeinsam."},
    ]
    _body(
        owner.post(
            f"/api/v1/sessions/{german_id}/subtitles/transcription/review",
            json={"expected_revision": revision, "segments": german_cues},
            headers=_headers(owner_csrf, "copy-flow-private-german-review"),
        ),
        201,
    )
    original_revision, original_german_artifact = _active_transcription(case, german_id)
    original_evidence = _saved_evidence(
        case, owner, owner_csrf, german_id, original_german_artifact,
        german_cues[0]["text"], monkeypatch,
    )
    german_cues[0]["evidence_ids"] = [original_evidence["id"]]
    _body(owner.post(
        f"/api/v1/sessions/{german_id}/subtitles/transcription/review",
        json={"expected_revision": original_revision, "segments": german_cues},
        headers=_headers(owner_csrf, "copy-flow-attach-evidence"),
    ), 201)
    original_revision, original_german_artifact = _active_transcription(case, german_id)
    assert alice.get(f"/api/v1/subtitle-evidence/{original_evidence['id']}").status_code == 404

    glossary_scope = {"source_language": "en", "target_language": "de"}
    glossary_payload = {
        "entries": [
            {
                "source": "shared course",
                "target": "gemeinsamer Kurs",
                "locked": True,
                "status": "active",
            },
            {"source": "course", "target": "Lehrgang", "locked": False, "status": "disabled"},
        ],
        "conflicts": [{"source": "course", "retained": "Kurs", "rejected": "Lehrgang"}],
    }
    glossary = _body(
        owner.post(
            f"/api/v1/sessions/{german_id}/knowledge/glossary",
            json=glossary_scope,
            headers=owner_csrf,
        ),
        201,
    )
    _body(
        owner.patch(
            f"/api/v1/knowledge/{glossary['id']}",
            json={"payload": glossary_payload},
            headers={**owner_csrf, "If-Match": f'"{glossary["revision"]}"'},
        ),
        200,
    )
    research_scope = {"source_language": "en", "target_language": ""}
    research_payload = {
        "evidence": [
            {
                "recommendation": "Use Kurs for the course title.",
                "source_url": "https://reference.invalid/course",
            }
        ],
        "warnings": ["Confirm the official title"],
        "summary": "Course terminology for translators.",
    }
    research = _body(
        owner.post(
            f"/api/v1/sessions/{source_id}/knowledge/research",
            json=research_scope,
            headers=owner_csrf,
        ),
        201,
    )
    _body(
        owner.patch(
            f"/api/v1/knowledge/{research['id']}",
            json={"payload": research_payload},
            headers={**owner_csrf, "If-Match": f'"{research["revision"]}"'},
        ),
        200,
    )

    # Alice manages the destination; the copier is an ordinary workspace Member.
    team = _body(
        alice.post(
            "/api/v1/workspaces",
            json={"name": "Translation handoff"},
            headers=_headers(alice_csrf, "copy-flow-destination"),
        ),
        201,
    )
    invitation = _body(
        alice.post(
            f"/api/v1/workspaces/{team['id']}/invitations",
            json={"user_id": owner_id, "role": "member", "expected_revision": team["revision"]},
            headers=_headers(alice_csrf, "copy-flow-invite-copier"),
        ),
        201,
    )
    _body(
        owner.post(
            f"/api/v1/workspace-invitations/{invitation['id']}/accept",
            json={},
            headers=_headers(owner_csrf, "copy-flow-join-destination"),
        ),
        200,
    )
    assert alice.get(f"/api/v1/sessions/{source_id}").status_code == 404
    assert alice.get(f"/api/v1/sessions/{german_id}").status_code == 404
    assert (
        alice.get(
            f"/api/v1/sessions/{german_id}/knowledge/glossary", query_string=glossary_scope
        ).status_code
        == 404
    )
    assert (
        alice.patch(
            f"/api/v1/knowledge/{glossary['id']}",
            json={"payload": glossary_payload},
            headers={**alice_csrf, "If-Match": '"2"'},
        ).status_code
        == 404
    )

    principal = current_browser_principal(replace(case, client=owner))
    actor = actor_from_principal(principal)
    assert actor is not None and actor.user_id == owner_id
    context = AccessContext(actor)
    provider_id, provider_model_id = seed_copy_model(case, team["id"])
    with case.database.snapshot_session() as db:
        initial = capture_current_project(
            db,
            context,
            original_project_id,
            team["id"],
            name="Shared working copy",
            settings_service=services.workspace_settings,
        )
        assert not initial.ready
        assert initial.blockers == (("destination_binding_required", "translation"),)
        choices = [
            {
                "requirement_id": item["id"],
                "provider_id": provider_id,
                "provider_model_id": provider_model_id,
            }
            for item in initial.manifest()["connection_requirements"]
        ]
        assert choices
        snapshot = capture_current_project(
            db,
            context,
            original_project_id,
            team["id"],
            name="Shared working copy",
            settings_service=services.workspace_settings,
            destination_connections=choices,
        )
    assert snapshot.ready, snapshot.blockers
    assert snapshot_destination_connections(snapshot) == choices
    original_bytes = {
        file.artifact_id: services.paths.managed_path(file.relative_path).read_bytes()
        for file in snapshot.files
    }
    preview = _body(
        owner.post(
            f"/api/v1/projects/{original_project_id}/copy-preview",
            json={
                "destination_workspace_id": team["id"],
                "destination_connections": choices,
            },
            headers=owner_csrf,
        ),
        200,
    )
    assert preview["ready"] and preview["fingerprint"] == snapshot.fingerprint
    assert preview["counts"]["knowledge_ledgers"] == 2
    assert preview["counts"]["subtitle_evidence"] == 1
    assert "state_json" not in preview and "files" not in preview
    assert (
        alice.get(
            f"/api/v1/projects/{original_project_id}/copy-preview",
            query_string={"workspace_id": team["id"]},
        ).status_code
        == 404
    )
    copy_payload = {
        "destination_workspace_id": team["id"],
        "expected_fingerprint": preview["fingerprint"],
        "name": "Shared working copy",
        "destination_connections": choices,
    }
    copy_headers = _headers(owner_csrf, "copy-flow-durable-copy")
    copy_url = f"/api/v1/projects/{original_project_id}/copy"
    queued = _body(owner.post(copy_url, json=copy_payload, headers=copy_headers), 202)
    assert queued["kind"] == "project.copy.current" and queued["result"] is None
    assert (
        _body(owner.post(copy_url, json=copy_payload, headers=copy_headers), 202)["id"]
        == queued["id"]
    )
    assert alice.get(f"/api/v1/project-copies/{queued['id']}").status_code == 404
    extension = case.app.extensions["pandrator"]
    worker = Worker(
        extension["jobs"], "copy-flow-worker", extension["workflow_handlers"].handlers()
    )
    assert worker.run_once()
    completed = _body(owner.get(f"/api/v1/project-copies/{queued['id']}"), 200)
    assert completed["state"] == "succeeded", completed
    copied = ProjectCopyResult(**completed["result"])
    assert _body(owner.post(copy_url, json=copy_payload, headers=copy_headers), 200) == completed
    with case.database.snapshot_session() as db:
        job = db.get(Job, queued["id"])
        job_actor = db.get(JobActorRecord, queued["id"])
        assert job and job.session_id is None and job.project_id == original_project_id
        assert job_actor and job_actor.user_id == owner_id
        finished_event = db.scalar(
            select(JobEvent).where(
                JobEvent.job_id == queued["id"],
                JobEvent.event_type == "job.succeeded",
            )
        )
        assert finished_event and "sessions" in finished_event.payload_json["changed_entities"]
    generic_job = _body(owner.get(f"/api/v1/jobs/{queued['id']}"), 200)
    assert generic_job["payload_json"] == {}
    assert generic_job["result_json"] == {"copy_id": queued["id"]}
    assert "snapshot_zlib_b64" not in str(generic_job)
    assert team["id"] not in str(generic_job) and copied.project_id not in str(generic_job)
    event_snapshot = _body(owner.get("/api/v1/events/snapshot"), 200)
    snapshot_job = next(
        item for item in event_snapshot["jobs"]["items"] if item["id"] == queued["id"]
    )
    assert snapshot_job["payload_json"] == {} and snapshot_job["result_json"] == {
        "copy_id": queued["id"]
    }

    assert copied.project_id != original_project_id and copied.source_session_id != source_id
    assert copied.workspace_id == team["id"] and copied.name == "Shared working copy"
    assert copied.translation_project_id != project["id"]
    with case.database.snapshot_session() as db:
        copied_branches = {
            row.target_language: row.session_id
            for row in db.scalars(
                select(TranslationProjectBranch).where(
                    TranslationProjectBranch.project_id == copied.translation_project_id
                )
            )
        }
        assert set(copied_branches) == {"de", "pl"}
        assert set(copied_branches.values()).isdisjoint(original_branches.values())
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProjectMembership)
                .where(ProjectMembership.project_id == copied.project_id)
            )
            == 0
        )
        assert all(
            record.status == "idle"
            for record in db.scalars(
                select(SessionRecord).where(SessionRecord.project_id == copied.project_id)
            )
        )
    assert (
        _body(alice.get(f"/api/v1/sessions/{copied.source_session_id}"), 200)["workspace_id"]
        == team["id"]
    )
    assert (
        alice.get(f"/api/v1/translation-projects/{copied.translation_project_id}").status_code
        == 200
    )
    with case.database.snapshot_session() as db:
        copied_files = list(
            db.scalars(select(Artifact).where(Artifact.project_id == copied.project_id))
        )
        assert len(copied_files) == len(snapshot.files)
        hashes = {hashlib.sha256(data).hexdigest() for data in original_bytes.values()}
        for file in copied_files:
            data = services.paths.managed_path(file.relative_path).read_bytes()
            assert data in original_bytes.values()
            assert (
                file.content_hash in hashes
                and file.content_hash == hashlib.sha256(data).hexdigest()
            )

    copied_german = copied_branches["de"]
    copied_evidence = _body(
        alice.get(f"/api/v1/sessions/{copied_german}/subtitle-evidence"), 200,
    )["items"][0]["record"]
    assert copied_evidence["id"] != original_evidence["id"]
    assert copied_evidence["job_id"] is None and copied_evidence["audio_model_ids"] == []
    assert copied_evidence["resolution"]["action"] == "accepted"
    assert copied_evidence["resolution"]["candidate_id"] == copied_evidence["candidates"][0]["id"]
    assert copied_evidence["candidates"][0]["id"] != original_evidence["candidates"][0]["id"]
    assert copied_evidence["candidates"][0]["text"] == german_cues[0]["text"]
    with case.database.snapshot_session() as db:
        active_document = db.scalar(select(Document).where(Document.session_id == copied_german))
        assert active_document is not None
        assert copied_evidence["source_revision_id"] != active_document.active_revision_id
        stored_evidence = db.get_one(SubtitleEvidence, copied_evidence["id"])
        assert stored_evidence.session_id == copied_german and stored_evidence.job_id is None
    copied_glossary = _body(
        alice.get(
            f"/api/v1/sessions/{copied_german}/knowledge/glossary",
            query_string=glossary_scope,
        ),
        200,
    )
    assert copied_glossary["id"] != glossary["id"] and copied_glossary["revision"] == 1
    assert copied_glossary["payload"] == glossary_payload
    copied_research = _body(
        alice.get(
            f"/api/v1/sessions/{copied.source_session_id}/knowledge/research",
            query_string=research_scope,
        ),
        200,
    )
    assert (
        copied_research["id"] != research["id"] and copied_research["payload"] == research_payload
    )
    changed_glossary = {
        **glossary_payload,
        "entries": [dict(item) for item in glossary_payload["entries"]],
    }
    changed_glossary["entries"][0]["target"] = "gemeinschaftlicher Kurs"
    _body(
        alice.patch(
            f"/api/v1/knowledge/{copied_glossary['id']}",
            json={"payload": changed_glossary},
            headers={**alice_csrf, "If-Match": '"1"'},
        ),
        200,
    )
    assert (
        _body(
            owner.get(
                f"/api/v1/sessions/{german_id}/knowledge/glossary", query_string=glossary_scope
            ),
            200,
        )["payload"]
        == glossary_payload
    )
    copied_revision, copied_german_artifact = _active_transcription(case, copied_german)
    _assert_srt(
        alice,
        copied_german_artifact,
        [(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in german_cues],
    )
    changed_cues = [*german_cues]
    changed_cues[0] = {
        **changed_cues[0], "text": "Willkommen zu unserer neuen Fassung.",
        "evidence_ids": [copied_evidence["id"]],
    }
    _body(
        alice.post(
            f"/api/v1/sessions/{copied_german}/subtitles/transcription/review",
            json={"expected_revision": copied_revision, "segments": changed_cues},
            headers=_headers(alice_csrf, "copy-flow-alice-edit-copy"),
        ),
        201,
    )
    assert _active_transcription(case, german_id) == (original_revision, original_german_artifact)
    _assert_srt(
        owner,
        original_german_artifact,
        [(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in german_cues],
    )

    team_now = _body(owner.get(f"/api/v1/workspaces/{team['id']}"), 200)
    _body(
        owner.post(
            f"/api/v1/workspaces/{team['id']}/leave",
            json={"expected_revision": team_now["revision"]},
            headers=_headers(owner_csrf, "copy-flow-copier-leaves"),
        ),
        200,
    )
    assert owner.get(f"/api/v1/sessions/{copied_german}").status_code == 404
    assert owner.get(f"/api/v1/sessions/{german_id}").status_code == 200
    assert owner.get(f"/api/v1/project-copies/{queued['id']}").status_code == 404
    assert (
        owner.get(
            f"/api/v1/sessions/{copied_german}/knowledge/glossary", query_string=glossary_scope
        ).status_code
        == 404
    )
    assert (
        _body(
            alice.get(
                f"/api/v1/sessions/{copied_german}/knowledge/glossary", query_string=glossary_scope
            ),
            200,
        )["payload"]
        == changed_glossary
    )
    assert owner.post(copy_url, json=copy_payload, headers=copy_headers).status_code == 404
    assert alice.get(f"/api/v1/sessions/{german_id}").status_code == 404
    assert owner.get(f"/api/v1/subtitle-evidence/{copied_evidence['id']}").status_code == 404
    revised_evidence = _body(alice.post(
        f"/api/v1/sessions/{copied_german}/subtitle-evidence/{copied_evidence['id']}/resolve",
        json={"action": "edited", "text": changed_cues[0]["text"],
              "note": "The team keeps this evidence after the copier leaves.",
              "expected_revision": copied_evidence["revision"]},
        headers=_headers(alice_csrf, "copy-flow-evidence-after-departure"),
    ), 200)["record"]
    assert revised_evidence["resolution"]["text"] == changed_cues[0]["text"]
    assert _body(owner.get(f"/api/v1/subtitle-evidence/{original_evidence['id']}"), 200)["record"] == original_evidence
    export_id = _run_export(
        case,
        alice,
        alice_csrf,
        copied_german,
        alice_id,
        "copy-flow-export-after-departure",
    )
    _assert_srt(
        alice, export_id, [(cue["start_ms"], cue["end_ms"], cue["text"]) for cue in changed_cues]
    )
    for file in snapshot.files:
        assert (
            services.paths.managed_path(file.relative_path).read_bytes()
            == original_bytes[file.artifact_id]
        )
