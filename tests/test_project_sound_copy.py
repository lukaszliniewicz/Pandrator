"""Current sound material survives registered workspace copying and retirement."""

from __future__ import annotations

import hashlib
import json
import threading
import wave
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.access.artifact_policy import require_artifact_access
from pandrator.web.access.contracts import AccessContext, ProjectAction
from pandrator.web.access.current_session_policy import require_current_session_access
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.jobs import Worker
from pandrator.web.projects import copy_jobs, copy_publication
from pandrator.web.projects.copy_contracts import ProjectCopyError
from pandrator.web.projects.copy_outputs import freeze_saved_speech_output
from pandrator.web.projects.copy_sound import COPIED_SOUND_KEY, SOUND_FAMILIES
from pandrator.web.projects.copy_sound_capture import capture_current_sounds
from tests.project_copy_test_support import seed_copy_model
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_project_output_copy import snapshot_for
from tests.test_project_sound_output_copy import mixed_output
from tests.test_project_sound_workspace_access import add, cue, source
from tests.test_provider_workspace_access import scoped_token
from tests.test_sound_library_workspace_http import case as case  # noqa: F401
from tests.test_sound_library_workspace_http import library as library  # noqa: F401
from tests.test_sound_library_workspace_http import no_network as no_network  # noqa: F401
from tests.test_sound_library_workspace_http import seed


def _render(f, client, session_id, base_id):
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=client)))
    assert actor is not None

    def authorize(unit):
        require_current_session_access(unit, session_id, ProjectAction.RUN)

    def before_io():
        with f.case.database.snapshot_session() as unit:
            authorize(unit)

    def authorize_input(unit, identifier):
        require_artifact_access(unit, AccessContext(actor), identifier)

    with execution_actor_scope(actor):
        return f.services.sound_mixes.render_for_artifact(
            session_id, base_id, progress=lambda *_: None, cancel_event=threading.Event(),
            before_io=before_io, authorization_guard=authorize, authorize_input=authorize_input,
        )


def _base(f, session_id):
    path = f.services.paths.artifacts / f"copy-sound-base-{uuid4()}.wav"
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\0\0" * 16000)
    return f.services.artifacts.register(path, kind="audio", role="assembled_audio",
        session_id=session_id, metadata={
            "audio_timeline": {"version": 1, "sample_rate_hz": 8000, "total_frames": 16000},
            "duration_ms": 2000,
        })


def _copy(f, record, client, csrf, destination, key):
    seed_copy_model(f.case, destination)
    query = {"destination_workspace_id": destination}
    url = f"/api/v1/projects/{record['project_id']}"
    preview = body(client.post(url + "/copy-preview", json=query, headers=csrf))
    if preview["connection_requirements"]:
        option = preview["connection_options"][0]
        query["destination_connections"] = [
            {"requirement_id": item["id"], "provider_id": option["provider_id"],
             "provider_model_id": option["provider_model_id"]}
            for item in preview["connection_requirements"]
        ]
        preview = body(client.post(url + "/copy-preview", json=query, headers=csrf))
    assert preview["ready"], preview["blockers"]
    queued = body(client.post(url + "/copy", json={**query,
        "expected_fingerprint": preview["fingerprint"], "name": "Copied sound project"},
        headers=headers(csrf, key)), 202)
    extension = f.case.app.extensions["pandrator"]
    worker = Worker(extension["jobs"], "sound-copy-integration-worker",
        extension["workflow_handlers"].handlers())
    assert worker.run_once()
    completed = body(client.get(f"/api/v1/project-copies/{queued['id']}", headers=csrf))
    assert completed["state"] == "succeeded", completed
    return completed["result"], preview


def _purge(f, client, csrf, session_id):
    current = body(client.get(f"/api/v1/sessions/{session_id}"))
    body(client.delete(f"/api/v1/sessions/{session_id}",
        headers={**csrf, "If-Match": str(current["revision"])}))
    impact = body(client.get(f"/api/v1/sessions/{session_id}/purge-preview"))
    assert impact["can_purge"], impact["blockers"]
    body(client.post(f"/api/v1/sessions/{session_id}/purge", headers=csrf,
        json={"expected_revision": impact["revision"], "impact_token": impact["impact_token"]}))
    with f.case.database.snapshot_session() as unit:
        assert unit.get(m.SessionRecord, session_id) is None


def test_registered_copy_member_edit_play_mix_and_workspace_audio_lifetime(library):
    f, u = library, library.users
    record = source(u.alice, u.ah)
    candidate, _item = seed(f.services, f.homes[u.alice_id], "Private chime")
    saved = body(add(u.alice, u.ah, record, cue(candidate["id"])), 201)
    source_base = _base(f, record["id"])
    source_mix = _render(f, u.alice, record["id"], source_base.id)
    with f.case.database.snapshot_session() as unit:
        original = unit.get_one(m.Artifact, candidate["artifact_id"])
        original_path = f.services.paths.managed_path(original.relative_path)
        expected = original_path.read_bytes()
        source_rows = {model.__tablename__: len(list(unit.scalars(select(model.id))))
            for model in (m.SoundItem, m.SoundVariant)}

    # The token can use the source project and destination workspace, without
    # any source-library grant. File admission/worker fences need the cue proof.
    _token, bearer = scoped_token(f.case, u.alice_id, {"app.read", "app.write", "app.run"},
        project=record["project_id"], workspace=u.workspace_id)
    api = f.case.app.test_client()
    result, preview = _copy(f, record, api, bearer, u.workspace_id, "registered-sound-copy")
    assert preview["counts"]["project_sound_plans"] == 1
    assert preview["counts"]["project_sound_cues"] == 1
    assert preview["counts"]["sound_variants"] == 1
    target = result["source_session_id"]
    plan = body(u.bob.get(f"/api/v1/sessions/{target}/sound-plan"))
    assert plan["revision"] == 1 and len(plan["cues"]["items"]) == 1
    copied = plan["cues"]["items"][0]
    assert copied["variant_id"] != candidate["id"]
    assert copied["logical_id"] != saved["cue"]["logical_id"]
    with f.case.database.snapshot_session() as unit:
        variant = unit.get_one(m.SoundVariant, copied["variant_id"])
        audio = unit.get_one(m.Artifact, variant.artifact_id)
        assert variant.workspace_id == audio.workspace_id == u.workspace_id
        assert variant.item_id is variant.job_id is variant.fingerprint is None
        assert audio.project_id is audio.session_id is audio.settings_hash is None
        assert audio.metadata_json == {COPIED_SOUND_KEY: {"version": 1}}
        assert variant.provenance_json == {
            COPIED_SOUND_KEY: {"version": 1}, "display_name": "Private chime"}
        audio_id, audio_path = audio.id, f.services.paths.managed_path(audio.relative_path)
        assert audio_path != original_path and audio_path.read_bytes() == expected
        outputs = list(unit.scalars(select(m.Artifact).where(m.Artifact.session_id == target)))
        frozen = [row for row in outputs if row.role == "export_saved_speech"]
        assert {row.metadata_json["copied_output"]["kind"] for row in frozen} == {
            "speech_assembly", "sound_mix"}
        target_base = next(row.id for row in frozen
            if row.metadata_json["copied_output"]["kind"] == "speech_assembly")
        assert all("sound_mix" not in row.metadata_json for row in frozen)
        assert len(list(unit.scalars(select(m.SoundItem.id)))) == source_rows["sound_items"]
        assert len(list(unit.scalars(select(m.SoundVariant.id)))) == source_rows["sound_variants"] + 1

    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    assert u.alice.get(f"/api/v1/sessions/{target}/sound-plan").status_code == 404
    assert u.bob.get(f"/api/v1/sessions/{record['id']}/sound-plan").status_code == 404
    content = u.bob.get(f"/api/v1/artifacts/{audio_id}/content")
    assert content.status_code == 200 and content.data == expected
    updated = body(u.bob.patch(
        f"/api/v1/sessions/{target}/sound-plan/cues/{copied['logical_id']}",
        json={"expected_revision": 1, "playback": {"gain_db": -6.0}},
        headers=headers(u.bh, "remaining-member-sound-edit")))
    assert updated["revision"] == 2
    rendered = _render(f, u.bob, target, target_base)
    assert rendered.session_id == target and rendered.role == "sound_mix.audio"
    assert f.services.paths.managed_path(rendered.relative_path).is_file()
    assert original_path.read_bytes() == expected
    assert source_mix.content_hash is not None

    _purge(f, u.alice, u.ah, record["id"])
    assert audio_path.read_bytes() == expected
    assert u.bob.get(f"/api/v1/artifacts/{audio_id}/content").data == expected
    _purge(f, u.bob, u.bh, target)
    assert audio_path.read_bytes() == expected
    remaining = body(u.bob.get(f"/api/v1/sounds/candidates/{copied['variant_id']}",
        query_string={"workspace_id": u.workspace_id}))
    assert remaining["status"] == "ready"


def test_no_plan_capture_has_one_header_query_and_no_sound_bodies(library):
    f, u = library, library.users
    record = source(u.alice, u.ah)
    statements = []

    def observed(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    with f.case.database.snapshot_session() as unit:
        event.listen(f.case.database.engine, "before_cursor_execute", observed)
        try:
            rows, assets, omitted = capture_current_sounds(unit,
                source_project_id=record["project_id"], workspace_id=f.homes[u.alice_id],
                session_workflows={record["id"]: "subtitles"}, speech_revisions={},
                max_rows=1000, max_artifacts=1000)
        finally:
            event.remove(f.case.database.engine, "before_cursor_execute", observed)
    assert rows == {family: [] for family in SOUND_FAMILIES}
    assert assets == set() and omitted == 0
    assert len(statements) == 1
    assert "provenance_json" not in statements[0] and "anchor_json" not in statements[0]


def test_frozen_v13_recovery_shape_remains_strict_before_sound_staging():
    state = {"schema_version": 13, "binding_fields": {}, "unsupported_counts": {},
        "destination_connections": [],
        "rows": {family: [] for family in copy_publication._FIELDS if family not in SOUND_FAMILIES and family != "performance_plans"}}
    snapshot = snapshot_for(state)
    encoded, fingerprint = snapshot.state_json, snapshot.fingerprint
    copy_publication._decode(snapshot)
    assert snapshot.state_json == encoded and snapshot.fingerprint == fingerprint
    for family in SOUND_FAMILIES:
        forged = {**state, "rows": {**state["rows"], family: []}}
        with pytest.raises(ProjectCopyError):
            copy_publication._decode(snapshot_for(forged))
    # No rehashing a newly inserted sound capsule into a legacy format.
    forged = json.loads(encoded)
    forged["rows"]["artifacts"] = [freeze_saved_speech_output(mixed_output(), allow_sound_mix=True)]
    with pytest.raises(ProjectCopyError):
        copy_publication._decode(snapshot_for(forged))
    assert hashlib.sha256(encoded.encode()).hexdigest() == fingerprint


@pytest.mark.parametrize("retired", ["source", "destination"])
def test_retired_sound_copy_namespace_fails_before_staging(library, monkeypatch, retired):
    f, u = library, library.users
    record = source(u.alice, u.ah)
    candidate, _item = seed(f.services, f.homes[u.alice_id], "Retirement chime")
    body(add(u.alice, u.ah, record, cue(candidate["id"])), 201)
    seed_copy_model(f.case, u.workspace_id)
    query = {"destination_workspace_id": u.workspace_id}
    url = f"/api/v1/projects/{record['project_id']}"
    preview = body(u.alice.post(url + "/copy-preview", json=query, headers=u.ah))
    assert preview["ready"], preview["blockers"]
    queued = body(u.alice.post(url + "/copy", json={**query,
        "expected_fingerprint": preview["fingerprint"]},
        headers=headers(u.ah, "retired-sound-namespace")), 202)
    with f.case.database.immediate_session() as unit:
        workspace_id = f.homes[u.alice_id] if retired == "source" else u.workspace_id
        unit.get_one(m.Workspace, workspace_id).state = "archived"
        before = {model.__tablename__: set(unit.scalars(select(model.id)))
            for model in (m.Project, m.SoundVariant, m.Artifact)}
    called = []

    def forbidden_staging(*_args, **_kwargs):
        called.append(True)
        pytest.fail("Retired copy namespace reached file staging")

    monkeypatch.setattr(copy_jobs, "stage_project_files", forbidden_staging)
    assert Worker(f.services.jobs, "retired-sound-copy-worker",
        f.services.workflow_handlers.handlers()).run_once()
    assert called == []
    assert f.services.jobs.get(queued["id"]).status != "succeeded"
    with f.case.database.snapshot_session() as unit:
        assert {model.__tablename__: set(unit.scalars(select(model.id)))
            for model in (m.Project, m.SoundVariant, m.Artifact)} == before
