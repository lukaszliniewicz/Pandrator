"""The retained artifact graph copies without private library references."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace

from pandrator.web import models as m
from pandrator.web import speech_inputs
from pandrator.web.access.contracts import AccessContext, ProjectAction
from pandrator.web.access.project_policy import require_project_access
from pandrator.web.accounts.execution_actor import actor_from_principal
from pandrator.web.jobs import Worker
from pandrator.web.projects.copy_publication import copy_identity
from pandrator.web.speech_input_contracts import SpeechInputOwner
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_retained_preview_workspace import MODEL, TRANSCRIPT
from tests.test_voice_crud_workspace_access import create, sample
from tests.test_voice_lifecycle_idempotency import silent_wav


def test_public_copy_rebinds_retained_reference_without_private_library_access(design, monkeypatch):
    f = design
    source = body(
        f.users.alice.post(
            "/api/v1/sessions",
            json={
                "name": "Private project with a selected reference",
                "workflow_kind": "subtitles",
                "source_language": "en",
            },
            headers=headers(f.users.ah, "retained-copy-session"),
        ),
        201,
    )
    voice = body(create(f.users.alice, f.users.ah, "Private selected reference"), 201)
    sid, _aid, path = sample(f.case, voice)
    with f.case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {
            "providers": {
                "audio_cpp": {
                    "status": "ready",
                    "resource_kind": "linked_reference",
                    "voice_id": voice["name"],
                }
            }
        }
        selected = session.get_one(m.VoiceSample, sid)
        selected.transcript, selected.transcript_reviewed = TRANSCRIPT, True
        record = session.get_one(m.SessionRecord, source["id"])
        project = session.get_one(m.Project, record.project_id)
        owner = SpeechInputOwner(project.workspace_id, record.project_id, record.id)
    assert owner.workspace_id == voice["workspace_id"] != f.users.workspace_id
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=f.users.alice)))
    assert actor is not None
    access = AccessContext(actor)

    def source_proof():
        with f.case.database.snapshot_session() as session:
            require_project_access(session, access, source["project_id"], ProjectAction.EDIT)

    settings = {"service": "audio_cpp", "model": MODEL, "voice": voice["name"]}
    with f.case.database.snapshot_session() as session:
        source_proof()
        capture = speech_inputs.capture_speech_input(
            session, f.services.paths, settings, owner=owner, voice_id=voice["id"]
        )
    prepared = speech_inputs.prepare_speech_input(
        f.case.database, f.services.paths, capture, before_io=source_proof
    )
    committed = False
    try:
        with f.case.database.immediate_session() as session:
            require_project_access(session, access, source["project_id"], ProjectAction.EDIT)
            pin = speech_inputs.publish_speech_input(session, f.services.paths, capture, prepared)
        committed = True
    finally:
        if not committed:
            speech_inputs.cleanup_speech_input(prepared)
    assert f.users.bob.get(f"/api/v1/artifacts/{pin['artifact_id']}/content").status_code == 404
    removed = f.users.alice.delete(
        f"/api/v1/voices/{voice['id']}/samples/{sid}", headers={**f.users.ah, "If-Match": '"1"'}
    )
    assert removed.status_code == 200 and not path.exists()
    preview = body(
        f.users.alice.get(
            f"/api/v1/projects/{source['project_id']}/copy-preview",
            query_string={"workspace_id": f.users.workspace_id},
        )
    )
    assert preview["ready"], preview["blockers"]
    assert preview["file_count"] == 2
    queued = body(
        f.users.alice.post(
            f"/api/v1/projects/{source['project_id']}/copy",
            json={
                "destination_workspace_id": f.users.workspace_id,
                "expected_fingerprint": preview["fingerprint"],
                "name": "Shared project with retained reference",
            },
            headers=headers(f.users.ah, "retained-copy-publish"),
        ),
        202,
    )
    extension = f.case.app.extensions["pandrator"]
    assert Worker(
        f.services.jobs, "retained-reference-copy", extension["workflow_handlers"].handlers()
    ).run_once()
    finished = body(f.users.alice.get(f"/api/v1/project-copies/{queued['id']}"))
    assert finished["state"] == "succeeded", finished
    copied = finished["result"]
    copied_pin = {
        **pin,
        "artifact_id": copy_identity(queued["id"], "artifacts", pin["artifact_id"]),
    }
    bob = actor_from_principal(current_browser_principal(replace(f.case, client=f.users.bob)))
    assert bob is not None
    bob_access = AccessContext(bob)
    copied_owner = SpeechInputOwner(
        f.users.workspace_id, copied["project_id"], copied["source_session_id"]
    )

    def copied_proof():
        with f.case.database.snapshot_session() as session:
            require_project_access(session, bob_access, copied["project_id"], ProjectAction.READ)

    def forbid_library_capture(*_args, **_kwargs):
        raise AssertionError("Copied input consulted private library")

    monkeypatch.setattr(speech_inputs, "capture_speech_input", forbid_library_capture)
    loaded = speech_inputs.load_speech_input(
        f.case.database,
        f.services.paths,
        copied_pin,
        settings,
        owner=copied_owner,
        before_io=copied_proof,
    )
    assert base64.b64decode(loaded["audio_cpp_voice_ref"]["data"].split(",", 1)[1]) == silent_wav()
    assert loaded["audio_cpp_voice_ref_hash"] == hashlib.sha256(silent_wav()).hexdigest()
    assert loaded["audio_cpp_reference_text"] == TRANSCRIPT
    assert f.users.bob.get(f"/api/v1/voices/{voice['id']}").status_code == 404
    assert f.users.bob.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    envelope = f.users.bob.get(f"/api/v1/artifacts/{copied_pin['artifact_id']}/content")
    assert envelope.status_code == 200
    assert voice["id"] not in envelope.get_data(as_text=True)
    assert sid not in envelope.get_data(as_text=True)
