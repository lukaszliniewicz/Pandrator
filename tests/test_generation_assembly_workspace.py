"""Actual shared-project assembly workers obey current project rights."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from tests.test_evidence_access_routes import body, headers
from tests.test_generation_workspace_execution import (
    case as case,
)
from tests.test_generation_workspace_execution import (
    design as design,
)
from tests.test_generation_workspace_execution import (
    generation_case as generation_case,
)
from tests.test_generation_workspace_execution import (
    no_network as no_network,
)
from tests.test_shared_tts_preview import fake_post


@pytest.fixture
def assembly_case(generation_case, monkeypatch):
    g = generation_case
    calls = fake_post(monkeypatch)
    assert g.worker.run_once()
    generated = g.f.services.jobs.get(g.marker["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 1
    queued = body(
        g.f.users.alice.post(
            f"/api/v1/sessions/{g.source['id']}/output-assemblies",
            json={
                "generation_run_id": g.marker["generation_run_id"],
                "run_override": {"output": {"format": "wav"}},
            },
            headers=headers(g.f.users.ah, "member-assembly-job"),
        ),
        202,
    )
    with g.f.case.database.snapshot_session() as session:
        before = set(session.scalars(select(m.Artifact.id)).all())
    return SimpleNamespace(
        g=g,
        queued=queued,
        artifacts_before=before,
        wav_before=set(g.f.services.paths.root.rglob("*.wav")),
    )


def revoke_member(a):
    g = a.g
    with g.f.case.database.immediate_session() as session:
        session.delete(
            session.get_one(m.WorkspaceMembership, (g.f.users.workspace_id, g.f.users.alice_id))
        )


def assert_unpublished(a):
    g = a.g
    job = g.f.services.jobs.get(a.queued["job_id"])
    assert job.status == "failed", (job.status, job.error_message)
    with g.f.case.database.snapshot_session() as session:
        assembly = session.get_one(m.OutputAssembly, a.queued["id"])
        assert assembly.artifact_id is None
        assert set(session.scalars(select(m.Artifact.id)).all()) == a.artifacts_before
    assert set(g.f.services.paths.root.rglob("*.wav")) == a.wav_before
    assert g.keep.read_bytes() == b"Earlier output must remain intact"


def test_registered_member_assembly_publishes_owned_audio(assembly_case):
    a = assembly_case
    g = a.g
    assert g.worker.run_once()
    job = g.f.services.jobs.get(a.queued["job_id"])
    assert job.status == "succeeded", job.error_message
    with g.f.case.database.snapshot_session() as session:
        assembly = session.get_one(m.OutputAssembly, a.queued["id"])
        assert assembly.status == "completed" and assembly.job_id == job.id
        artifact = session.get_one(m.Artifact, assembly.artifact_id)
        assert (artifact.workspace_id, artifact.project_id, artifact.session_id) == (
            g.f.users.workspace_id,
            g.source["project_id"],
            g.source["id"],
        )
        assert artifact.role == "assembled_audio" and artifact.state == "current"
        assert session.get_one(m.JobActorRecord, job.id).user_id == g.f.users.alice_id
    assert g.keep.read_bytes() == b"Earlier output must remain intact"


def test_assembly_membership_loss_denies_before_reading_takes(assembly_case, monkeypatch):
    a = assembly_case
    revoke_member(a)
    # These are real assembler read boundaries; they must never be reached after
    # admission loses its project rights.
    monkeypatch.setattr(
        "pandrator.web.audio_assembly.preferred_pcm_format",
        lambda *_a, **_k: pytest.fail("Denied assembly read an audio take"),
    )
    assert a.g.worker.run_once()
    assert_unpublished(a)


def test_assembly_membership_loss_before_publication_discards_only_own_staging(
    assembly_case,
    monkeypatch,
):
    a = assembly_case
    original = a.g.handlers.assemble_generation_output
    changed = []

    def render(payload, progress, cancel_event):
        def watched(value, detail=None):
            if value >= 0.96 and not changed:
                changed.append(True)
                revoke_member(a)
            progress(value, detail)

        return original(payload, watched, cancel_event)

    monkeypatch.setattr(a.g.handlers, "assemble_generation_output", render)
    assert a.g.worker.run_once()
    assert changed
    assert_unpublished(a)


@pytest.mark.parametrize("rollback", [False, True])
def test_assembly_collision_and_failed_publication_preserve_prior_output(
    assembly_case,
    monkeypatch,
    rollback,
):
    a = assembly_case
    collision = (
        a.g.handlers._session_dir(a.g.source["id"])
        / "assemblies"
        / f"assembly-{a.queued['id']}.wav"
    )
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_bytes(b"An earlier export must remain intact")
    a.wav_before.add(collision)
    if rollback:
        register = a.g.handlers.artifacts.register_in_session

        def fail_after_registration(*args, **kwargs):
            register(*args, **kwargs)
            raise RuntimeError("Synthetic publication rollback")

        monkeypatch.setattr(a.g.handlers.artifacts, "register_in_session", fail_after_registration)
    assert a.g.worker.run_once()
    assert collision.read_bytes() == b"An earlier export must remain intact"
    if rollback:
        assert_unpublished(a)
    else:
        job = a.g.f.services.jobs.get(a.queued["job_id"])
        assert job.status == "succeeded", job.error_message
        with a.g.f.case.database.snapshot_session() as session:
            assembly = session.get_one(m.OutputAssembly, a.queued["id"])
            artifact = session.get_one(m.Artifact, assembly.artifact_id)
            assert a.g.f.services.paths.managed_path(artifact.relative_path) != collision


@pytest.mark.parametrize("cache", ["reuse", "revoked"])
def test_registered_export_bridge_attributes_and_reuses_the_selected_assembly(
    generation_case,
    monkeypatch,
    cache,
):
    from pandrator.web.accounts.execution_actor import actor_from_storage, execution_actor_scope

    g = generation_case
    fake_post(monkeypatch)
    assert g.worker.run_once()
    with g.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, g.marker["generation_run_id"])
        snapshot = dict(run.settings_snapshot_json)
        actor_row = session.get_one(m.JobActorRecord, g.marker["job_id"])
        actor = actor_from_storage(
            actor_row.user_id, actor_row.auth_revision, actor_row.context_json
        )
    payload = {
        "session_id": g.source["id"],
        "settings": {
            "generation_run_id": g.marker["generation_run_id"],
            "export_mode": "audio",
            "audio_mode": "dubbing_only",
            "format": "wav",
            "include_sounds": False,
        },
        "resolved_settings_snapshot": snapshot,
    }
    published = []

    def stop_after_assembly(value, _progress, _cancel):
        # Delivery encoding is outside this packet. Keep the real registered
        # export-variant/assembly bridge and actual artifact publication.
        published.append(value["pinned_assembly_artifact_id"])
        return {"artifact_id": published[-1]}

    monkeypatch.setattr(g.handlers, "export", stop_after_assembly)
    with execution_actor_scope(actor):
        queued = g.f.services.jobs.enqueue("export.variant", payload, session_id=g.source["id"])
    assert g.worker.run_once()
    first = g.f.services.jobs.get(queued.id)
    assert first.status == "succeeded", first.error_message
    with g.f.case.database.snapshot_session() as session:
        assemblies = session.scalars(
            select(m.OutputAssembly).where(m.OutputAssembly.session_id == g.source["id"])
        ).all()
        assert len(assemblies) == 1 and assemblies[0].job_id == first.id
        assert assemblies[0].artifact_id == published[0]
    with execution_actor_scope(actor):
        repeated = g.f.services.jobs.enqueue("export.variant", payload, session_id=g.source["id"])
    if cache == "revoked":
        with g.f.case.database.immediate_session() as session:
            session.delete(
                session.get_one(m.WorkspaceMembership, (g.f.users.workspace_id, g.f.users.alice_id))
            )
    assert g.worker.run_once()
    second = g.f.services.jobs.get(repeated.id)
    if cache == "revoked":
        assert second.status == "failed" and len(published) == 1
    else:
        assert second.status == "succeeded", second.error_message
        assert published == [published[0], published[0]]
    with g.f.case.database.snapshot_session() as session:
        assert (
            len(
                session.scalars(
                    select(m.OutputAssembly.id).where(m.OutputAssembly.session_id == g.source["id"])
                ).all()
            )
            == 1
        )
        assert session.get_one(m.Artifact, published[0]).state == "current"


def test_uncertain_assembly_commit_preserves_the_registered_output(assembly_case, monkeypatch):
    from contextlib import contextmanager

    a = assembly_case
    database = a.g.f.case.database
    immediate = database.immediate_session
    register = a.g.handlers.artifacts.register_in_session
    publication_units = []

    def remember_publication_unit(*args, **kwargs):
        artifact = register(*args, **kwargs)
        publication_units[:] = [args[0]]
        return artifact

    @contextmanager
    def uncertain_after_commit(*args, **kwargs):
        with immediate(*args, **kwargs) as session:
            yield session
        if publication_units and publication_units[0] is session:
            publication_units.clear()
            raise RuntimeError("Synthetic uncertain commit outcome")

    monkeypatch.setattr(a.g.handlers.artifacts, "register_in_session", remember_publication_unit)
    monkeypatch.setattr(database, "immediate_session", uncertain_after_commit)
    assert a.g.worker.run_once()
    job = a.g.f.services.jobs.get(a.queued["job_id"])
    assert job.status == "failed" and "uncertain commit" in job.error_message
    with database.snapshot_session() as session:
        assembly = session.get_one(m.OutputAssembly, a.queued["id"])
        assert assembly.artifact_id is not None
        artifact = session.get_one(m.Artifact, assembly.artifact_id)
        assert artifact.state == "current" and artifact.role == "assembled_audio"
        assert a.g.f.services.paths.managed_path(artifact.relative_path).is_file()
    assert a.g.keep.read_bytes() == b"Earlier output must remain intact"
