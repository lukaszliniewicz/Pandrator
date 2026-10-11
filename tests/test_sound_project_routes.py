"""HTTP acceptance for versioned project sound cues."""

from __future__ import annotations

import io
import threading
import wave
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
    SessionSetting,
    SoundVariant,
)
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan


@pytest.fixture(scope="session")
def case(tmp_path_factory):
    root = tmp_path_factory.mktemp("sound-http") / "data"
    app = create_app(data_root=root, testing=True, background_maintenance=False)
    services = app.extensions["pandrator"]
    scopes = {
        "read": ("app.read",),
        "write": ("app.read", "app.write"),
        "run": ("app.read", "app.run"),
        "full": ("app.read", "app.write", "app.run"),
    }
    tokens = {
        name: services["auth"].create_api_token(f"sound-http-{name}", scopes=value)[1]
        for name, value in scopes.items()
    }

    def headers(scope, key=None):
        result = {"Authorization": f"Bearer {tokens[scope]}"}
        if key:
            result["Idempotency-Key"] = key
        return result

    yield {"client": app.test_client(), "services": services, "headers": headers}
    services["database"].dispose()


def _wav(sample: bytes = b"\x00\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(sample * 800)
    return output.getvalue()


def _raw_import(case, data: bytes, filename: str, key: str):
    headers = {
        **case["headers"]("full", key),
        "Content-Type": "application/octet-stream",
        "X-Audio-Filename": quote(filename, safe=""),
    }
    return case["client"].put("/api/v1/sounds/import", data=data, headers=headers)


def _seed_project(case, workflow_kind: str):
    services = case["services"]
    with services["database"].immediate_session() as session:
        project = SessionRecord(name=f"Sound {workflow_kind}", workflow_kind=workflow_kind)
        session.add(project)
        session.flush()
        plan = GenerationPlan(session_id=project.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash=f"{workflow_kind}-speech"
        )
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        segment = GenerationSegment(plan_revision_id=revision.id, ordinal=0, text="Hello world")
        session.add(segment)
        session.flush()
        return project.id, revision.id, segment.id


def _register_session_audio(
    case,
    session_id: str,
    filename: str,
    *,
    role: str,
    metadata: dict,
    parent_ids: list[str] | None = None,
):
    services = case["services"]
    with services["database"].session() as session:
        record = session.get(SessionRecord, session_id)
        assert record is not None
        storage_key = record.storage_key
    path = services["paths"].sessions / storage_key / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav())
    return services["artifacts"].register(
        path,
        kind="audio",
        role=role,
        session_id=session_id,
        metadata=metadata,
        parent_ids=parent_ids,
    )


def _register_global_variant(case, filename: str, *, role: str = "sound.original") -> str:
    services = case["services"]
    path = services["paths"].artifacts / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav())
    artifact = services["artifacts"].register(path, kind="audio", role=role)
    with services["database"].immediate_session() as session:
        variant = SoundVariant(
            artifact_id=artifact.id,
            state="ready",
            sample_rate=8000,
            channels=1,
            frame_count=800,
            duration_seconds=0.1,
        )
        session.add(variant)
        session.flush()
        return variant.id


def test_project_cue_http_versions_restore_defaults_and_leave_speech_untouched(case):
    client, headers, services = case["client"], case["headers"], case["services"]
    imported = _raw_import(case, _wav(), "cue.wav", "sound-cue-import-001")
    assert imported.status_code == 201
    variant_id = imported.get_json()["id"]
    book_id, _book_revision, book_segment = _seed_project(case, "audiobook")
    timed_id, _timed_revision, _timed_segment = _seed_project(case, "voiceover")

    def counts():
        with services["database"].session() as session:
            return (
                session.scalar(select(func.count()).select_from(GenerationPlanRevision)),
                session.scalar(select(func.count()).select_from(GenerationSegment)),
                session.scalar(select(func.count()).select_from(AudioTake)),
                tuple(
                    session.scalars(
                        select(GenerationSegment.revision).order_by(GenerationSegment.id)
                    )
                ),
            )

    before = counts()
    plan_path = f"/api/v1/sessions/{book_id}/sound-plan"
    assert client.get(plan_path, headers=headers("read")).get_json()["revision"] == 0
    add_body = {
        "expected_revision": 0,
        "variant_id": variant_id,
        "anchor": {"kind": "before_segment", "segment_id": book_segment},
    }
    add_headers = headers("full", "sound-cue-add-replay-001")
    added = client.post(f"{plan_path}/cues", json=add_body, headers=add_headers)
    add_replay = client.post(f"{plan_path}/cues", json=add_body, headers=add_headers)
    assert added.status_code == 201, added.get_json()
    assert add_replay.headers["Idempotency-Replayed"] == "true"
    assert add_replay.get_json() == added.get_json()
    cue_id, original_plan = added.get_json()["cue"]["logical_id"], added.get_json()["plan_id"]
    assert added.get_json()["cue"]["playback"]["mode"] == "insert"

    patch_body = {"expected_revision": 1, "included": False}
    cue_path = f"{plan_path}/cues/{cue_id}"
    edited = client.patch(cue_path, json=patch_body, headers=headers("full", "sound-cue-patch-001"))
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()["revision"] == 2
    stale = client.patch(
        cue_path, json=patch_body, headers=headers("full", "sound-cue-patch-stale-001")
    )
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "revision_conflict"
    deleted = client.delete(
        cue_path, headers={**headers("full", "sound-cue-delete-001"), "If-Match": '"2"'}
    )
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()["revision"] == 3
    stale_delete = client.delete(
        cue_path, headers={**headers("full", "sound-cue-delete-stale-001"), "If-Match": "2"}
    )
    assert stale_delete.status_code == 409
    restored_body = {"expected_revision": 3, "source_plan_id": original_plan}
    restored = client.post(
        f"{plan_path}/restore", json=restored_body, headers=headers("full", "sound-cue-restore-001")
    )
    assert restored.status_code == 200, restored.get_json()
    assert restored.get_json()["revision"] == 4
    restored_plan = client.get(plan_path, headers=headers("read")).get_json()
    assert restored_plan["cues"]["items"][0]["included"] is True

    timed_path = f"/api/v1/sessions/{timed_id}/sound-plan"
    timed = client.post(
        f"{timed_path}/cues",
        json={
            "expected_revision": 0,
            "variant_id": variant_id,
            "anchor": {"kind": "source_time", "time_seconds": 2.0},
        },
        headers=headers("full", "sound-cue-timed-001"),
    )
    assert timed.status_code == 201, timed.get_json()
    assert timed.get_json()["cue"]["playback"]["mode"] == "overlay"
    assert counts() == before


def test_mix_and_resolution_http_admission_scopes_revisions_and_replays(case, monkeypatch):
    client, headers, services = case["client"], case["headers"], case["services"]
    variant_id = _raw_import(case, _wav(), "job-sound.wav", "sound-job-source-001").get_json()["id"]

    mix_session_id, _mix_speech_revision, _mix_segment = _seed_project(case, "voiceover")
    timeline = {"audio_timeline": {"version": 1, "sample_rate_hz": 8000, "total_frames": 800}}
    base = _register_session_audio(
        case, mix_session_id, "mix-base.wav", role="assembled_audio", metadata=timeline
    )
    dry_timeline = _register_session_audio(
        case, mix_session_id, "dry-timeline.wav", role="tts_audio", metadata=timeline
    )
    mix_plan_path = f"/api/v1/sessions/{mix_session_id}/sound-plan"
    cue = client.post(
        f"{mix_plan_path}/cues",
        json={
            "expected_revision": 0,
            "variant_id": variant_id,
            "anchor": {"kind": "source_time", "time_seconds": 0.0},
            "playback": {"mode": "overlay"},
        },
        headers=headers("full", "sound-job-mix-cue-001"),
    )
    assert cue.status_code == 201, cue.get_json()
    mix_path = f"{mix_plan_path}/mix?base_artifact_id={base.id}"
    assert client.get(mix_path, headers=headers("read")).status_code == 200
    render_path = f"{mix_plan_path}/render"
    mix_service = services["services"].sound_mixes
    render_body = {
        "expected_revision": 1,
        "base_artifact_id": base.id,
        "timeline_artifact_id": dry_timeline.id,
    }
    with monkeypatch.context() as no_mix_preflight:
        no_mix_preflight.setattr(
            mix_service,
            "prepare",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid render request prepared sound-mix inputs"
            ),
        )
        assert (
            client.post(
                render_path,
                json=render_body,
                headers=headers("read", "sound-job-read-mix-001"),
            ).status_code
            == 403
        )
        assert client.post(render_path, json=render_body, headers=headers("run")).status_code == 400
    with monkeypatch.context() as no_stale_mix_audio_work:
        no_stale_mix_audio_work.setattr(
            "pandrator.web.sound_mix_snapshot._base_geometry",
            lambda *_args, **_kwargs: pytest.fail(
                "stale render request inspected audio geometry"
            ),
        )
        no_stale_mix_audio_work.setattr(
            "pandrator.web.sound_mix_snapshot._verify_input_files",
            lambda *_args, **_kwargs: pytest.fail(
                "stale render request hashed audio inputs"
            ),
        )
        stale_mix = client.post(
            render_path,
            json={**render_body, "expected_revision": 0},
            headers=headers("run", "sound-job-mix-stale-001"),
        )
        assert stale_mix.status_code == 409
        assert stale_mix.get_json()["error"]["code"] == "revision_conflict"

    prepare_mix = mix_service.prepare
    prepared_mixes = []

    def capture_mix(session_id, base_id, *, timeline_artifact_id=None, expected_revision=None):
        prepared = prepare_mix(
            session_id,
            base_id,
            timeline_artifact_id=timeline_artifact_id,
            expected_revision=expected_revision,
        )
        prepared_mixes.append(prepared)
        return prepared

    monkeypatch.setattr(mix_service, "prepare", capture_mix)
    mix_headers = headers("run", "sound-job-mix-replay-001")
    mixed = client.post(render_path, json=render_body, headers=mix_headers)
    assert mixed.status_code == 202, mixed.get_json()
    prepared_mix = prepared_mixes[-1]
    with services["database"].session() as session:
        mix_job = session.get(Job, mixed.get_json()["job_id"])
        assert mix_job is not None
        assert mix_job.kind == "sound.mix" and mix_job.status == "queued"
        assert mix_job.payload_json == prepared_mix.payload
        assert set(mix_job.payload_json) == {
            "session_id",
            "base_artifact_id",
            "base_content_hash",
            "timeline_artifact_id",
            "timeline_content_hash",
            "sound_plan_id",
            "sound_plan_revision",
            "speech_plan_revision_id",
            "request_fingerprint",
        }
        assert mix_job.max_attempts == 1
        assert mix_job.resource_keys_json == [f"session:{mix_session_id}"]
        assert mix_job.lease_owner is None
    conflict_mix = client.post(
        render_path,
        json={**render_body, "timeline_artifact_id": None},
        headers=mix_headers,
    )
    assert conflict_mix.status_code == 409
    assert conflict_mix.get_json()["error"]["code"] == "idempotency_conflict"
    monkeypatch.setattr(
        mix_service,
        "prepare",
        lambda *_args, **_kwargs: pytest.fail("mix replay prepared inputs again"),
    )
    mix_replay = client.post(render_path, json=render_body, headers=mix_headers)
    assert mix_replay.status_code == 202
    assert mix_replay.headers["Idempotency-Replayed"] == "true"
    assert mix_replay.get_json() == mixed.get_json()

    resolve_session_id, _resolve_speech_revision, resolve_segment = _seed_project(case, "audiobook")
    take_artifact = _register_session_audio(
        case,
        resolve_session_id,
        "resolve-take.wav",
        role="tts_audio",
        metadata={"synthesized_text": "Hello world"},
    )
    with services["database"].immediate_session() as session:
        session.add(
            AudioTake(
                generation_segment_id=resolve_segment,
                artifact_id=take_artifact.id,
                status="completed",
                is_active=True,
                duration_ms=100,
            )
        )
    resolve_plan_path = f"/api/v1/sessions/{resolve_session_id}/sound-plan"
    pending = client.post(
        f"{resolve_plan_path}/cues",
        json={
            "expected_revision": 0,
            "variant_id": variant_id,
            "anchor": {
                "kind": "after_word",
                "segment_id": resolve_segment,
                "quote": "Hello",
                "occurrence": 1,
            },
            "playback": {"mode": "insert"},
        },
        headers=headers("full", "sound-job-resolve-cue-001"),
    )
    assert pending.status_code == 201, pending.get_json()
    resolve_path = f"{resolve_plan_path}/resolve"
    resolve_body = {"expected_revision": 1}
    resolve_service = services["services"].sound_resolution
    with monkeypatch.context() as no_resolution_preflight:
        no_resolution_preflight.setattr(
            resolve_service,
            "prepare",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid resolution request prepared sound-resolution inputs"
            ),
        )
        assert (
            client.post(
                resolve_path,
                json=resolve_body,
                headers=headers("read", "sound-job-read-resolve-001"),
            ).status_code
            == 403
        )
        assert client.post(resolve_path, json=resolve_body, headers=headers("run")).status_code == 400
        too_many = client.post(
            resolve_path,
            json={"expected_revision": 1, "cue_ids": [f"cue-{index}" for index in range(21)]},
            headers=headers("run", "sound-job-resolve-many-001"),
        )
        assert too_many.status_code == 422
    with monkeypatch.context() as no_stale_resolution_work:
        no_stale_resolution_work.setattr(
            resolve_service.settings,
            "resolve_in_session",
            lambda *_args, **_kwargs: pytest.fail(
                "stale resolution request resolved runtime settings"
            ),
        )
        stale_resolve = client.post(
            resolve_path,
            json={"expected_revision": 2},
            headers=headers("run", "sound-job-resolve-stale-001"),
        )
        assert stale_resolve.status_code == 409
        assert stale_resolve.get_json()["error"]["code"] == "revision_conflict"

    prepare_resolution = resolve_service.prepare
    prepared_resolutions = []

    def capture_resolution(session_id, *, expected_revision, cue_ids=None):
        prepared = prepare_resolution(
            session_id, expected_revision=expected_revision, cue_ids=cue_ids
        )
        prepared_resolutions.append(prepared)
        return prepared

    monkeypatch.setattr(resolve_service, "prepare", capture_resolution)
    resolve_headers = headers("run", "sound-job-resolve-replay-001")
    resolved = client.post(resolve_path, json=resolve_body, headers=resolve_headers)
    assert resolved.status_code == 202, resolved.get_json()
    prepared_resolution = prepared_resolutions[-1]
    with services["database"].session() as session:
        resolve_job = session.get(Job, resolved.get_json()["job_id"])
        assert resolve_job is not None
        assert resolve_job.kind == "sound.resolve" and resolve_job.status == "queued"
        assert resolve_job.payload_json == prepared_resolution.payload
        assert set(resolve_job.payload_json) == {"session_id", "pins", "pin_hash"}
        assert resolve_job.max_attempts == 1
        assert resolve_job.resource_keys_json == list(prepared_resolution.resource_keys)
        assert resolve_job.lease_owner is None
    conflict_resolve = client.post(
        resolve_path,
        json={"expected_revision": 1, "cue_ids": [pending.get_json()["cue"]["logical_id"]]},
        headers=resolve_headers,
    )
    assert conflict_resolve.status_code == 409
    assert conflict_resolve.get_json()["error"]["code"] == "idempotency_conflict"
    monkeypatch.setattr(
        resolve_service,
        "prepare",
        lambda *_args, **_kwargs: pytest.fail("resolution replay prepared inputs again"),
    )
    resolve_replay = client.post(resolve_path, json=resolve_body, headers=resolve_headers)
    assert resolve_replay.status_code == 202
    assert resolve_replay.headers["Idempotency-Replayed"] == "true"
    assert resolve_replay.get_json() == resolved.get_json()


def test_project_cue_rejects_forged_sound_role(case):
    client, headers = case["client"], case["headers"]
    session_id, _revision, _segment = _seed_project(case, "voiceover")
    forged_variant = _register_global_variant(case, "forged-reference.wav", role="voice.reference")
    response = client.post(
        f"/api/v1/sessions/{session_id}/sound-plan/cues",
        json={
            "expected_revision": 0,
            "variant_id": forged_variant,
            "anchor": {"kind": "source_time", "time_seconds": 0.05},
            "playback": {"mode": "overlay"},
        },
        headers=headers("full", "sound-forged-role-001"),
    )
    assert response.status_code == 422
    assert "current global audio artifact" in response.get_json()["error"]["message"]


def test_overlay_resolution_survives_gain_trim_but_not_insert_mode(case):
    client, headers, services = case["client"], case["headers"], case["services"]
    session_id, _revision, segment_id = _seed_project(case, "audiobook")
    variant_id = _register_global_variant(case, "resolution-overlay.wav")
    plan_path = f"/api/v1/sessions/{session_id}/sound-plan"
    added = client.post(
        f"{plan_path}/cues",
        json={
            "expected_revision": 0,
            "variant_id": variant_id,
            "anchor": {
                "kind": "after_word",
                "segment_id": segment_id,
                "quote": "Hello",
                "occurrence": 1,
            },
            "playback": {"mode": "overlay"},
        },
        headers=headers("full", "sound-overlay-resolution-add-001"),
    )
    assert added.status_code == 201, added.get_json()
    cue_id = added.get_json()["cue"]["logical_id"]
    resolution = {
        "status": "ready",
        "take_id": "take-fixture",
        "take_content_hash": "a" * 64,
        "transcript_hash": "b" * 64,
        "take_frame": 400,
        "take_sample_rate": 8000,
        "evidence": {"alignment_only_overlay": True},
    }
    with services["database"].immediate_session() as session:
        plan = session.scalar(
            select(ProjectSoundPlan).where(
                ProjectSoundPlan.session_id == session_id,
                ProjectSoundPlan.revision == 1,
            )
        )
        cue = session.scalar(
            select(ProjectSoundCue).where(
                ProjectSoundCue.plan_id == plan.id,
                ProjectSoundCue.logical_id == cue_id,
            )
        )
        cue.anchor_json = {**cue.anchor_json, "resolution": resolution}
        cue.placement_status = "ready"

    gain_trimmed = client.patch(
        f"{plan_path}/cues/{cue_id}",
        json={"expected_revision": 1, "playback": {"mode": "overlay", "gain_db": -3.0}},
        headers=headers("full", "sound-overlay-resolution-gain-001"),
    )
    assert gain_trimmed.status_code == 200, gain_trimmed.get_json()
    assert gain_trimmed.get_json()["cue"]["anchor"]["resolution"] == resolution
    changed_mode = client.patch(
        f"{plan_path}/cues/{cue_id}",
        json={"expected_revision": 2, "playback": {"mode": "insert", "gain_db": -3.0}},
        headers=headers("full", "sound-overlay-resolution-insert-001"),
    )
    assert changed_mode.status_code == 200, changed_mode.get_json()
    assert "resolution" not in changed_mode.get_json()["cue"]["anchor"]
    assert changed_mode.get_json()["cue"]["placement_status"] == "needs_placement"


def test_cue_edit_stales_only_mixed_audio_descendants(case):
    client, headers, services = case["client"], case["headers"], case["services"]
    session_id, _revision, segment_id = _seed_project(case, "voiceover")
    variant_id = _register_global_variant(case, "invalidation-effect.wav")
    timeline = {"audio_timeline": {"version": 1, "sample_rate_hz": 8000, "total_frames": 800}}
    dry = _register_session_audio(
        case, session_id, "invalidation-dry.wav", role="assembled_audio", metadata=timeline
    )
    take_artifact = _register_session_audio(
        case,
        session_id,
        "invalidation-take.wav",
        role="tts_audio",
        metadata={"synthesized_text": "Hello world"},
    )
    with services["database"].immediate_session() as session:
        take = AudioTake(
            generation_segment_id=segment_id,
            artifact_id=take_artifact.id,
            status="completed",
            is_active=True,
            duration_ms=100,
        )
        setting = SessionSetting(
            session_id=session_id,
            section="tts",
            value_json={"provider": "preserve-me"},
        )
        assembly = OutputAssembly(
            session_id=session_id,
            artifact_id=dry.id,
            status="completed",
            settings_json={"output": "preserve-me"},
        )
        session.add_all([take, setting, assembly])
        session.flush()
        take_id, assembly_id = take.id, assembly.id

    plan_path = f"/api/v1/sessions/{session_id}/sound-plan"
    added = client.post(
        f"{plan_path}/cues",
        json={
            "expected_revision": 0,
            "variant_id": variant_id,
            "anchor": {"kind": "source_time", "time_seconds": 0.0},
            "playback": {"mode": "overlay"},
        },
        headers=headers("full", "sound-invalidation-cue-001"),
    )
    assert added.status_code == 201, added.get_json()
    mix_service = services["services"].sound_mixes
    mixed = mix_service.render_for_artifact(
        session_id,
        dry.id,
        progress=lambda _fraction, _detail: None,
        cancel_event=threading.Event(),
    )
    export = _register_session_audio(
        case,
        session_id,
        "invalidation-export.wav",
        role="finalized_export",
        metadata={},
        parent_ids=[mixed.id],
    )
    changed = client.patch(
        f"{plan_path}/cues/{added.get_json()['cue']['logical_id']}",
        json={"expected_revision": 1, "included": False},
        headers=headers("full", "sound-invalidation-cue-patch-001"),
    )
    assert changed.status_code == 200, changed.get_json()
    with services["database"].session() as session:
        assert session.get(Artifact, mixed.id).state == "stale"
        assert session.get(Artifact, export.id).state == "stale"
        assert session.get(Artifact, dry.id).state == "current"
        assert session.get(AudioTake, take_id).status == "completed"
        assembly = session.get(OutputAssembly, assembly_id)
        assert assembly is not None
        assert (assembly.status, assembly.artifact_id, assembly.settings_json) == (
            "completed",
            dry.id,
            {"output": "preserve-me"},
        )
        setting = session.get(SessionSetting, (session_id, "tts"))
        assert setting is not None and setting.value_json == {"provider": "preserve-me"}
