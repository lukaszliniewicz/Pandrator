"""Registered Member delivery preserves prior work and rechecks late authority."""

from __future__ import annotations

import wave
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_storage, execution_actor_scope
from tests.test_evidence_access_routes import body, headers
from tests.test_generation_workspace_execution import case as case
from tests.test_generation_workspace_execution import design as design
from tests.test_generation_workspace_execution import generation_case as generation_case
from tests.test_generation_workspace_execution import no_network as no_network
from tests.test_provider_workspace_access import scoped_token
from tests.test_shared_tts_preview import fake_post


@pytest.fixture
def delivery_case(generation_case, monkeypatch, request):
    g = generation_case
    fake_post(monkeypatch)
    assert g.worker.run_once()
    generated = g.f.services.jobs.get(g.marker["job_id"])
    assert generated.status == "succeeded", generated.error_message
    options = getattr(request, "param", {})
    if options.get("sound"):
        sound_path = g.f.services.paths.artifacts / "existing-workspace-sound.wav"
        with wave.open(str(sound_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x01\x00" * 800)
        sound = g.f.services.artifacts.register(
            sound_path, kind="audio", role="sound.original", workspace_id=g.f.users.workspace_id
        )
        with g.f.case.database.immediate_session() as session:
            variant = m.SoundVariant(
                workspace_id=g.f.users.workspace_id,
                artifact_id=sound.id,
                state="ready",
                sample_rate=8000,
                channels=1,
                frame_count=800,
                duration_seconds=0.1,
            )
            session.add(variant)
            session.flush()
            revision = session.get_one(
                m.GenerationRun, g.marker["generation_run_id"]
            ).plan_revision_id
            segment = session.scalar(
                select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == revision)
            )
            assert segment is not None
            g.handlers.sound_mixes.project_sounds.add_in_session(
                session,
                g.source["id"],
                {
                    "expected_revision": 0,
                    "variant_id": variant.id,
                    "anchor": {"kind": "before_segment", "segment_id": segment.id},
                    "playback": {"mode": "insert", "gain_db": -12.0},
                },
            )
    settings = {
        "generation_run_id": g.marker["generation_run_id"],
        "export_mode": "audio",
        "audio_mode": "dubbing_only",
        "format": "wav",
        "include_sounds": False,
        **options.get("settings", {}),
    }
    queue_headers = headers(g.f.users.ah, "member-delivery-job")
    if options.get("project_credential"):
        _token, queue_headers = scoped_token(
            g.f.case,
            g.f.users.alice_id,
            {"app.read", "app.write", "app.run"},
            project=g.source["project_id"],
        )
    if options.get("assembly_only"):
        assembly = body(
            g.f.users.alice.post(
                f"/api/v1/sessions/{g.source['id']}/output-assemblies",
                json={
                    "generation_run_id": g.marker["generation_run_id"],
                    "run_override": {"output": {"format": "wav"}},
                },
                headers=queue_headers,
            ),
            202,
        )
        queued = {
            "id": assembly["job_id"],
            "assembly_id": assembly["id"],
            "kind": "generation.assemble",
        }
    else:
        queued = body(
            g.f.users.alice.post(
                f"/api/v1/sessions/{g.source['id']}/stages/export/run",
                json=settings,
                headers=queue_headers,
            ),
            202,
        )
        assert queued["kind"] == "export.variant"
    return SimpleNamespace(g=g, queued=queued, settings=settings, options=options)


def revoke_member(d):
    g = d.g
    with g.f.case.database.immediate_session() as session:
        session.delete(
            session.get_one(m.WorkspaceMembership, (g.f.users.workspace_id, g.f.users.alice_id))
        )


def outputs(d):
    with d.g.f.case.database.snapshot_session() as session:
        return list(
            session.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == d.g.source["id"],
                    m.Artifact.kind == "export",
                )
            ).all()
        )


def assert_kept_assembly(d):
    g = d.g
    with g.f.case.database.snapshot_session() as session:
        assembly = session.scalar(
            select(m.OutputAssembly).where(m.OutputAssembly.session_id == g.source["id"])
        )
        assert assembly is not None and assembly.status == "completed"
        artifact = session.get_one(m.Artifact, assembly.artifact_id)
        assert artifact.state == "current"
        assert g.f.services.paths.managed_path(artifact.relative_path).is_file()
    assert g.keep.read_bytes() == b"Earlier output must remain intact"
    assert not list(g.f.services.paths.sessions.rglob(".delivery-*"))


def test_http_member_export_worker_publishes_owned_audio_readable_by_peer(delivery_case):
    d = delivery_case
    g = d.g
    assert g.worker.run_once()
    job = g.f.services.jobs.get(d.queued["id"])
    assert job.status == "succeeded", job.error_message
    result = outputs(d)
    assert len(result) == 1 and job.result_json["artifact_ids"] == [result[0].id]
    output = result[0]
    assert (output.workspace_id, output.project_id, output.session_id) == (
        g.f.users.workspace_id,
        g.source["project_id"],
        g.source["id"],
    )
    assert ".delivery-" not in output.relative_path
    path = g.f.services.paths.managed_path(output.relative_path)
    peer = g.f.users.bob.get(f"/api/v1/artifacts/{output.id}/content")
    assert peer.status_code == 200 and peer.data == path.read_bytes()
    assert_kept_assembly(d)


def test_admitted_member_export_worker_survives_browser_logout(delivery_case):
    d = delivery_case
    g = d.g
    assert g.f.users.alice.post("/api/v1/auth/logout", headers=g.f.users.ah).status_code == 200
    assert g.worker.run_once()
    job = g.f.services.jobs.get(d.queued["id"])
    assert job.status == "succeeded", job.error_message
    assert len(outputs(d)) == 1
    assert_kept_assembly(d)


@pytest.mark.parametrize("generation_case", [{"workflow_kind": "audiobook"}], indirect=True)
@pytest.mark.parametrize(
    "delivery_case",
    [{"settings": {"export_mode": "subtitles", "book_cue_mode": "segments"}}],
    indirect=True,
)
def test_member_book_subtitle_delivery_persists_cues_and_final_file(delivery_case):
    d = delivery_case
    g = d.g
    assert g.worker.run_once()
    job = g.f.services.jobs.get(d.queued["id"])
    assert job.status == "succeeded", job.error_message
    output = outputs(d)
    assert len(output) == 1 and output[0].relative_path.endswith(".srt")
    text = g.f.services.paths.managed_path(output[0].relative_path).read_text()
    assert "A shared narration sentence." in text
    with g.f.case.database.snapshot_session() as session:
        cues = session.scalars(
            select(m.Artifact).where(
                m.Artifact.session_id == g.source["id"], m.Artifact.role == "book_cues"
            )
        ).all()
        assert len(cues) == 1
        assert ".delivery-" not in cues[0].relative_path
        assert g.f.services.paths.managed_path(cues[0].relative_path).is_file()
    assert_kept_assembly(d)


@pytest.mark.parametrize("generation_case", [{"workflow_kind": "audiobook"}], indirect=True)
@pytest.mark.parametrize(
    "delivery_case",
    [
        {"sound": True, "settings": {"include_sounds": True}},
        {"sound": True, "settings": {"include_sounds": False}},
        {"sound": True, "project_credential": True, "settings": {"include_sounds": True}},
    ],
    ids=["shared-sound", "sound-disabled", "project-credential"],
    indirect=True,
)
def test_member_delivery_uses_existing_workspace_sound_only_when_authorized_and_enabled(
    delivery_case, monkeypatch
):
    d = delivery_case
    g = d.g
    if not d.settings["include_sounds"]:
        monkeypatch.setattr(
            g.handlers.sound_mixes,
            "render_for_artifact",
            lambda *_a, **_k: pytest.fail("Disabled sound processing invoked a mix"),
        )
    assert g.worker.run_once()
    job = g.f.services.jobs.get(d.queued["id"])
    limited = bool(d.options.get("project_credential"))
    assert job.status == ("failed" if limited else "succeeded"), job.error_message
    assert len(outputs(d)) == (0 if limited else 1)
    with g.f.case.database.snapshot_session() as session:
        mixes = session.scalars(
            select(m.Artifact).where(
                m.Artifact.session_id == g.source["id"], m.Artifact.role == "sound_mix.audio"
            )
        ).all()
        assert len(mixes) == (1 if d.settings["include_sounds"] and not limited else 0)
        for mix in mixes:
            assert (mix.workspace_id, mix.project_id) == (
                g.f.users.workspace_id,
                g.source["project_id"],
            )
            assert g.f.services.paths.managed_path(mix.relative_path).is_file()
    assert_kept_assembly(d)


@pytest.mark.parametrize("generation_case", [{"workflow_kind": "audiobook"}], indirect=True)
@pytest.mark.parametrize(
    "delivery_case",
    [{"sound": True, "assembly_only": True}],
    indirect=True,
)
def test_primary_member_assembly_can_publish_a_separate_owned_sound_mix(delivery_case):
    d = delivery_case
    g = d.g
    assert g.worker.run_once()
    job = g.f.services.jobs.get(d.queued["id"])
    assert job.status == "succeeded", job.error_message
    assert job.result_json["sound_mix_status"] == "ready"
    with g.f.case.database.snapshot_session() as session:
        sound = session.get_one(m.Artifact, job.result_json["sound_mix_artifact_id"])
        assert sound.id != job.result_json["artifact_id"] and sound.role == "sound_mix.audio"
        assert (sound.workspace_id, sound.project_id, sound.session_id) == (
            g.f.users.workspace_id,
            g.source["project_id"],
            g.source["id"],
        )
        assert g.f.services.paths.managed_path(sound.relative_path).is_file()
    assert not outputs(d)
    assert_kept_assembly(d)


def test_registered_continuation_keeps_one_lease_through_assembly_and_export(
    generation_case, monkeypatch
):
    g = generation_case
    fake_post(monkeypatch)
    assert g.worker.run_once()
    generated = g.f.services.jobs.get(g.marker["job_id"])
    assert generated.status == "succeeded", generated.error_message
    with g.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, g.marker["generation_run_id"])
        snapshot = dict(run.settings_snapshot_json)
        stored = session.get_one(m.JobActorRecord, generated.id)
        actor = actor_from_storage(stored.user_id, stored.auth_revision, stored.context_json)
        jobs_before = set(session.scalars(select(m.Job.id)).all())
    # The fixture supplies an already reviewed SRT and completed speech. This
    # exercises the real continuation/export chain without an STT prerequisite.
    source_path = g.f.services.paths.uploads / "reviewed-continuation.srt"
    source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nA shared narration sentence.\n")
    with execution_actor_scope(actor):
        source = g.f.services.artifacts.register(
            source_path, kind="srt", role="upload", session_id=g.source["id"]
        )
        asset = g.f.services.source_library.ensure_for_artifact(source.id, kind="srt")
        g.f.services.source_library.attach(g.source["id"], asset.id)
    with g.f.case.database.immediate_session() as session:
        session.get_one(m.SessionRecord, g.source["id"]).included_stages_json = ["export"]
    payload = {
        "session_id": g.source["id"],
        "target_stage": "export",
        "settings": {
            "generation_run_id": g.marker["generation_run_id"],
            "export_mode": "audio",
            "audio_mode": "dubbing_only",
            "format": "wav",
            "include_sounds": False,
        },
        "resolved_settings_snapshot": snapshot,
    }
    with execution_actor_scope(actor):
        queued = g.f.services.jobs.enqueue("workflow.continue", payload, session_id=g.source["id"])
    assert g.worker.run_once()
    job = g.f.services.jobs.get(queued.id)
    assert job.status == "succeeded", job.error_message
    d = SimpleNamespace(g=g)
    assert len(outputs(d)) == 1
    assert_kept_assembly(d)
    with g.f.case.database.snapshot_session() as session:
        assert set(session.scalars(select(m.Job.id)).all()) == jobs_before | {queued.id}
        assembly = session.scalar(
            select(m.OutputAssembly).where(m.OutputAssembly.session_id == g.source["id"])
        )
        assert assembly is not None and assembly.job_id == queued.id


def test_delivery_revocation_after_assembly_keeps_completed_audio(delivery_case, monkeypatch):
    d = delivery_case
    export = d.g.handlers.export

    def revoke_then_export(payload, progress, cancel_event):
        assert payload["pinned_assembly_artifact_id"]
        revoke_member(d)
        return export(payload, progress, cancel_event)

    monkeypatch.setattr(d.g.handlers, "export", revoke_then_export)
    assert d.g.worker.run_once()
    job = d.g.f.services.jobs.get(d.queued["id"])
    assert job.status == "failed", job.error_message
    assert not outputs(d)
    assert_kept_assembly(d)


def test_delivery_revocation_after_render_before_writer_keeps_prior_files(
    delivery_case, monkeypatch
):
    d = delivery_case
    prepare = d.g.handlers.artifacts.prepare_registration
    reached = []

    def revoke_after_hash(path, **kwargs):
        prepared = prepare(path, **kwargs)
        if any(part.startswith(".delivery-") for part in path.parts):
            assert not reached
            reached.append(path)
            revoke_member(d)
        return prepared

    monkeypatch.setattr(d.g.handlers.artifacts, "prepare_registration", revoke_after_hash)
    assert d.g.worker.run_once()
    job = d.g.f.services.jobs.get(d.queued["id"])
    assert reached and job.status == "failed", job.error_message
    assert not outputs(d)
    assert_kept_assembly(d)
