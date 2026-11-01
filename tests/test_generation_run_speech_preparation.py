"""Run-local narrator staging never writes saved project selections."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from pandrator.web import generation_speech_preparation as preparation
from pandrator.web import models as m
from pandrator.web.speech_input_contracts import PROJECT_SPEECH_INPUT_KEY
from tests.test_evidence_access_routes import body, headers
from tests.test_project_tts_retention import admit_generation, input_paths, reference, save
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import MODEL, transport
from tests.test_settings_workspace_access import put


def defaults(p, **overrides):
    value = {"service": "audio_cpp", "model": MODEL, "voice": "workshop-reference",
             "language": "en", "voice_mode_version": 1, "performance_enabled": False,
             "casting_enabled": False, **overrides}
    body(put(p.r.f.users.owner, p.r.f.users.oh, "defaults.tts", value,
             workspace=p.r.f.users.workspace_id))
    return value


@pytest.fixture
def run_project(project):
    p = project
    defaults(p)
    plan = p.r.f.services.generation.create_plan(p.source["id"], source_revision_id=None,
        settings={}, segments=[{"text": "A run-owned narration sentence.", "language": "en"}])
    return SimpleNamespace(p=p, service=p.r.f.services.generation,
        revision=plan["active_revision_id"])


def stages(monkeypatch):
    created = []
    original = preparation.prepare_speech_input

    def prepare(*args, **kwargs):
        item = original(*args, **kwargs)
        created.extend([item.audio.relative_path, item.snapshot.relative_path])
        return item

    monkeypatch.setattr(preparation, "prepare_speech_input", prepare)
    return created


def state(p):
    with p.r.f.case.database.snapshot_session() as session:
        return (
            session.scalar(select(func.count()).select_from(m.SessionSetting)),
            session.scalar(select(func.count()).select_from(m.ProjectSpeechReference)),
            [(row.id, row.settings_json, row.operation_json) for row in session.scalars(select(m.GenerationPlanRevision))],
            [(row.id, row.active_revision_id) for row in session.scalars(select(m.GenerationPlan))],
        )


@pytest.mark.parametrize("legacy,managed,performance", [(False, False, False), (True, False, False),
    (False, True, False), (False, False, True)])
def test_inherited_or_whole_run_capture_is_atomic_and_does_not_save(run_project, legacy, managed, performance):
    g, p = run_project, run_project.p
    if performance:
        from pandrator.logic.speech_performance import content_hash
        from pandrator.web.speech_plan_context import plan_signature

        with p.r.f.case.database.immediate_session() as session:
            segment = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == g.revision))
            assert segment is not None
            session.add(m.PerformancePlan(session_id=p.source["id"], plan_revision_id=g.revision,
                base_signature=plan_signature(session, g.revision), status="adopted",
                units_json=[{"id": segment.id, "text_hash": content_hash(segment.text)}],
                manual_annotations_json={segment.id: {"decision": "none"}}))
    before = state(p)
    override = {"voice_mode_version": 0 if legacy else 1, "performance_enabled": performance}
    if managed:
        override.update(voice_id=p.r.voice["id"], voice="", speaker="")
    prepared = g.service.prepare_start(p.source["id"], run_override={"tts": override})
    pin = prepared["frozen_snapshot"]["tts"][PROJECT_SPEECH_INPUT_KEY]
    assert pin is not None
    assert input_paths(p) == []  # staging is outside the publication writer
    try:
        with p.r.f.case.database.immediate_session() as session:
            result = g.service.start_in_session(session, p.source["id"], prepared=prepared)
    finally:
        g.service.cleanup_prepared_start(prepared)
    assert result["status"] == "queued" and len(input_paths(p)) == 2
    assert state(p) == before
    assert all(p.r.f.services.paths.managed_path(path).is_file() for path in input_paths(p))
    with p.r.f.case.database.snapshot_session() as session:
        snapshot = session.get_one(m.GenerationRun, result["id"]).settings_snapshot_json
        assert snapshot["tts"][PROJECT_SPEECH_INPUT_KEY] == pin
        assert "_speech_preparation" not in snapshot
        assert not any(key.startswith("audio_cpp_voice_ref") for key in snapshot["tts"])


def test_metadata_preview_omits_carrier_and_staging(run_project, monkeypatch):
    g, p = run_project, run_project.p
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("preview captured"))
    prepared = g.service.prepare_start(p.source["id"], retain_speech_inputs=False)
    assert "_speech_preparation" not in prepared
    json.dumps(prepared)
    g.service.cleanup_prepared_start(prepared)
    g.service.cleanup_prepared_start({})


@pytest.mark.parametrize("tamper", ["source", "frozen_pin", "staged_pin", "rollback"])
def test_publication_failure_rolls_back_and_cleans_private_files(run_project, monkeypatch, tamper):
    g, p = run_project, run_project.p
    created = stages(monkeypatch)
    before = state(p)
    prepared = g.service.prepare_start(p.source["id"])
    carrier = prepared["_speech_preparation"]
    if tamper == "source":
        with p.r.f.case.database.immediate_session() as session:
            session.get_one(m.Voice, p.r.voice["id"]).revision += 1
    elif tamper == "frozen_pin":
        prepared["frozen_snapshot"]["tts"][PROJECT_SPEECH_INPUT_KEY]["content_hash"] = "0" * 64
    elif tamper == "staged_pin":
        material = next(iter(carrier._materials.values()))
        material.prepared = replace(material.prepared,
            snapshot=replace(material.prepared.snapshot, content_hash="0" * 64))
    try:
        with pytest.raises((ValueError, RuntimeError)):
            with p.r.f.case.database.immediate_session() as session:
                g.service.start_in_session(session, p.source["id"], prepared=prepared)
                assert tamper == "rollback"
                raise RuntimeError("synthetic rollback after publication")
    finally:
        g.service.cleanup_prepared_start(prepared)
    assert state(p) == before and input_paths(p) == []
    assert created and all(not p.r.f.services.paths.managed_path(path).exists() for path in created)
    with p.r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.GenerationRun)) == 0


@pytest.mark.parametrize("failure", ["preflight", "freeze", "cancel"])
def test_preparation_failures_cleanup_without_escaping_carrier(run_project, monkeypatch, failure):
    g, p = run_project, run_project.p
    created = stages(monkeypatch)
    if failure == "preflight":
        monkeypatch.setattr("pandrator.web.generation_provider_preflight.generation_provider_preflight",
            lambda *_a, **_k: {"configuration_warning": "synthetic preflight failure"})
    elif failure == "freeze":
        # The local import is intentional; fail the later identity build after staging.
        monkeypatch.setattr("pandrator.web.generation_audio_identity.plan_audio_identities",
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError("synthetic freeze failure")))
    calls = 0

    def before_io():
        nonlocal calls
        calls += 1
        if failure == "cancel" and calls >= 3:
            raise RuntimeError("synthetic admission lease/cancellation changed")

    with pytest.raises((ValueError, RuntimeError)):
        g.service.prepare_start(p.source["id"], before_io=before_io)
    assert input_paths(p) == []
    assert all(not p.r.f.services.paths.managed_path(path).exists() for path in created)


def test_null_alias_is_explicit_and_detects_late_rebinding(run_project):
    g, p = run_project, run_project.p
    prepared = g.service.prepare_start(p.source["id"], run_override={"tts": {"voice": "unbound-alias"}})
    assert prepared["frozen_snapshot"]["tts"][PROJECT_SPEECH_INPUT_KEY] is None
    try:
        with p.r.f.case.database.immediate_session() as session:
            result = g.service.start_in_session(session, p.source["id"], prepared=prepared)
    finally:
        g.service.cleanup_prepared_start(prepared)
    with p.r.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, result["id"]).settings_snapshot_json["tts"][PROJECT_SPEECH_INPUT_KEY] is None
    assert input_paths(p) == []
    # A null selection is frozen too: later alias registration cannot bind it.
    prepared = g.service.prepare_start(p.source["id"], run_override={"tts": {"voice": "late-alias"}})
    with p.r.f.case.database.immediate_session() as session:
        session.get_one(m.Voice, p.r.voice["id"]).metadata_json = {"providers": {"audio_cpp": {
            "status": "ready", "resource_kind": "linked_reference", "voice_id": "late-alias"}}}
    try:
        with pytest.raises(ValueError):
            with p.r.f.case.database.immediate_session() as session:
                g.service.start_in_session(session, p.source["id"], prepared=prepared)
    finally:
        g.service.cleanup_prepared_start(prepared)


def test_project_only_fresh_denied_before_capture_and_saved_reuse_after_deletion(run_project, monkeypatch):
    p = run_project.p
    _token, bearer = scoped_token(p.r.f.case, p.r.f.users.alice_id,
        {"app.read", "app.write", "app.run"}, project=p.source["project_id"])
    original = preparation.capture_speech_selection
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("protected library body"))
    path = f"/api/v1/sessions/{p.source['id']}/generation-runs"
    denied = p.r.f.users.alice.post(path, json={}, headers=headers(bearer, "fresh-project-denied"))
    assert denied.status_code == 404, denied.get_json()
    monkeypatch.setattr(preparation, "capture_speech_selection", original)
    body(save(p))
    pin = reference(p).pin
    with p.r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, p.r.sid))
        session.delete(session.get_one(m.Voice, p.r.voice["id"]))
    p.r.path.unlink()
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("saved reread"))
    result = body(p.r.f.users.alice.post(path, json={}, headers=headers(bearer, "saved-project-run")), 202)
    with p.r.f.case.database.snapshot_session() as session:
        assert session.get_one(m.GenerationRun, result["id"]).settings_snapshot_json["tts"][PROJECT_SPEECH_INPUT_KEY] == pin
    carried = body(p.r.f.users.alice.post(path, json={"settings_source": "run",
        "settings_source_run_id": result["id"]}, headers=headers(bearer, "carried-project-run")), 202)
    assert carried["id"] != result["id"]


def test_keyed_retry_reuses_one_published_graph(run_project, monkeypatch):
    p = run_project.p
    path = f"/api/v1/sessions/{p.source['id']}/generation-runs"
    auth = headers(p.r.f.users.ah, "one-run-reference")
    result = body(p.r.f.users.alice.post(path, json={}, headers=auth), 202)
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("retry staged"))
    replay = p.r.f.users.alice.post(path, json={}, headers=auth)
    assert body(replay, 202) == result and replay.headers["Idempotency-Replayed"] == "true"
    assert len(input_paths(p)) == 2


def test_admission_inference_reads_run_owned_bytes_and_derived_edge_after_source_deletion(project, monkeypatch):
    p, r = project, project.r
    defaults(p)
    g = admit_generation(p)
    with r.f.case.database.immediate_session() as session:
        run = session.get_one(m.GenerationRun, g.result["generation_run_id"])
        pin = run.settings_snapshot_json["tts"][PROJECT_SPEECH_INPUT_KEY]
        session.delete(session.get_one(m.VoiceSample, r.sid))
        session.delete(session.get_one(m.Voice, r.voice["id"]))
    r.path.unlink()
    monkeypatch.setattr(g.handlers, "prepare_audio_cpp_voice_reference", lambda *_a, **_k: pytest.fail("legacy library reader"))
    calls = transport(monkeypatch)
    assert g.worker.run_once()
    job = r.f.services.jobs.get(g.result["job_id"])
    assert job.status == "succeeded", job.error_message
    assert len(calls) == 1 and "The selected original reference." in json.dumps(calls[0])
    with r.f.case.database.snapshot_session() as session:
        take = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id))
        assert take is not None
        edge = session.get(m.ArtifactEdge, (pin["artifact_id"], take.artifact_id))
        assert edge is not None and edge.relation == "derived_from"


@pytest.mark.parametrize("mode", ["other_adapter", "prebuilt", "design"])
def test_authoritative_filter_does_not_invoke_missing_selection(mode, monkeypatch):
    from pandrator.web import project_tts_settings as binder

    settings = {"service": "gemini" if mode == "other_adapter" else "audio_cpp",
                "model": MODEL, "voice": "unused"}
    monkeypatch.setattr(binder, "resolve_capabilities", lambda _settings: {"model": MODEL, "voice_mode": mode})
    snapshot = {"tts": settings}
    binder.bind_project_tts_reference(MagicMock(), "unused", snapshot,
        _missing_selection=lambda *_a: pytest.fail("ineligible selection captured"))
    assert PROJECT_SPEECH_INPUT_KEY not in snapshot["tts"]


@pytest.mark.parametrize("excluded", ["cast", "performance", "selected", "rvc", "physical", "saved"])
def test_unused_narrator_does_not_capture_library(run_project, monkeypatch, excluded):
    from pandrator.web.project_speech_references import put_project_speech_reference
    from pandrator.web.speech_input_contracts import SpeechInputOwner

    g, p = run_project, run_project.p
    settings = {"service": "audio_cpp", "model": MODEL, "voice": "workshop-reference",
        "voice_mode_version": 0}
    selector = {"voice": settings["voice"], "voice_id": None, "service": "audio_cpp", "model": MODEL}
    if excluded in {"cast", "performance"}:
        settings["casting_enabled" if excluded == "cast" else "performance_enabled"] = True
    with p.r.f.case.database.immediate_session() as session:
        segment = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == g.revision))
        assert segment is not None
        if excluded == "physical":
            segment.voice = "a-block-choice"
        if excluded == "saved":
            put_project_speech_reference(session, p.source["id"], f"segment:{segment.id}",
                selector={"voice": "logical-choice"}, binding={"adapter": "audio_cpp", "model": MODEL,
                    "voice": "logical-choice", "display_name": "Logical"}, pin=None, expected_revision=0,
                authorization_guard=lambda _s: None)
    carrier = preparation.GenerationSpeechPreparation(p.r.f.case.database, p.r.f.services.paths,
        SpeechInputOwner(p.r.f.users.workspace_id, p.source["project_id"], p.source["id"]),
        g.revision, operation="rvc" if excluded == "rvc" else "generate",
        selected_override={"tts": {"voice": "alternate"}} if excluded == "selected" else {})
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("unused narrator captured"))
    with p.r.f.case.database.snapshot_session() as session:
        assert carrier.missing_selection(session, selector, settings) is None
    carrier.cleanup()


def test_fresh_membership_loss_after_staging_prevents_run_and_cleans(run_project, monkeypatch):
    p = run_project.p
    created = stages(monkeypatch)
    original = preparation.prepare_speech_input

    def revoke(*args, **kwargs):
        prepared = original(*args, **kwargs)
        with p.r.f.case.database.immediate_session() as session:
            session.delete(session.get_one(m.WorkspaceMembership, (p.r.f.users.workspace_id, p.r.f.users.alice_id)))
        return prepared

    monkeypatch.setattr(preparation, "prepare_speech_input", revoke)
    response = p.r.f.users.alice.post(f"/api/v1/sessions/{p.source['id']}/generation-runs",
        json={}, headers=headers(p.r.f.users.ah, "run-staging-membership-loss"))
    assert response.status_code == 404, response.get_json()
    assert created and all(not p.r.f.services.paths.managed_path(path).exists() for path in created)
    assert input_paths(p) == []


@pytest.mark.parametrize("fence", ["cancel", "lease"])
def test_admission_io_rechecks_actual_job_after_files_are_staged(run_project, monkeypatch, fence):
    from pandrator.web import speech_inputs
    from pandrator.web.job_execution import current_job_lease
    from pandrator.web.jobs import Worker

    p = run_project.p
    original_prepare = preparation.prepare_speech_input
    original_stage = speech_inputs.stage_file
    created = []

    def stage(*args, **kwargs):
        item = original_stage(*args, **kwargs)
        created.append(item.relative_path)
        return item

    def prepare(database, paths, capture, *, before_io):
        calls = 0

        def fence_io():
            nonlocal calls
            calls += 1
            if calls == 2:
                assert created
                lease = current_job_lease.get()
                assert lease is not None
                with database.immediate_session() as session:
                    job = session.get_one(m.Job, lease["job_id"])
                    if fence == "cancel":
                        job.status = "cancel_requested"
                    else:
                        job.lease_generation += 1
            before_io()

        return original_prepare(database, paths, capture, before_io=fence_io)

    monkeypatch.setattr(speech_inputs, "stage_file", stage)
    monkeypatch.setattr(preparation, "prepare_speech_input", prepare)
    queued = body(p.r.f.users.alice.post(f"/api/v1/sessions/{p.source['id']}/generation-admissions",
        json={}, headers=headers(p.r.f.users.ah, f"fresh-admission-{fence}")), 202)
    handlers = p.r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(p.r.f.services.jobs, "run-input-fence", handlers.handlers())
    assert worker.run_once()
    assert created and all(not p.r.f.services.paths.managed_path(path).exists() for path in created)
    assert input_paths(p) == []
    with p.r.f.case.database.snapshot_session() as session:
        assert session.scalar(select(func.count()).select_from(m.GenerationRun)) == 0
        assert session.scalar(select(func.count()).select_from(m.Job).where(m.Job.kind == "generation.run")) == 0
        assert not (session.get_one(m.Job, queued["id"]).result_json or {}).get("generation_run_id")


def test_legacy_inline_reference_is_preserved_without_new_capture(run_project, monkeypatch):
    import base64

    from tests.test_voice_lifecycle_idempotency import silent_wav

    g, p = run_project, run_project.p
    inline = base64.b64encode(silent_wav()).decode("ascii")
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("inline library capture"))
    prepared = g.service.prepare_start(p.source["id"], run_override={"tts": {
        "audio_cpp_voice_ref": inline, "audio_cpp_reference_text": "Legacy inline reference.",
    }})
    try:
        tts = prepared["frozen_snapshot"]["tts"]
        assert tts["audio_cpp_voice_ref"] == inline
        assert tts["audio_cpp_reference_text"] == "Legacy inline reference."
        assert PROJECT_SPEECH_INPUT_KEY not in tts
        assert "_speech_preparation" not in prepared
        assert input_paths(p) == []
    finally:
        g.service.cleanup_prepared_start(prepared)


def source_settings():
    from uuid import uuid4

    from pandrator.web.speech_input_contracts import (
        PROJECT_SPEECH_BINDING_KEY,
        PROJECT_SPEECH_SOURCE_KEY,
    )

    pin = {"version": 1, "artifact_id": str(uuid4()), "content_hash": "a" * 64}
    binding = {"adapter": "audio_cpp", "model": MODEL, "voice": "workshop-reference", "display_name": "Reference"}
    return {"service": "audio_cpp", "model": MODEL, "voice": binding["voice"],
        PROJECT_SPEECH_INPUT_KEY: pin, PROJECT_SPEECH_BINDING_KEY: binding,
        PROJECT_SPEECH_SOURCE_KEY: {"version": 1, "pin": dict(pin), "binding": dict(binding),
            "source_voice_reference_hash": "b" * 64}}


@pytest.mark.parametrize("change", ["valid", "extra", "version", "null", "pin", "binding", "hash", "binding_extra"])
def test_source_compatibility_metadata_is_exact_and_bound(change):
    from pandrator.web.generation_audio_identity import source_voice_reference_hash
    from pandrator.web.speech_input_contracts import PROJECT_SPEECH_SOURCE_KEY

    settings = source_settings()
    metadata = settings[PROJECT_SPEECH_SOURCE_KEY]
    if change == "extra":
        metadata["path"] = "not-permitted"
    elif change == "version":
        metadata["version"] = True
    elif change == "null":
        metadata["pin"] = None
    elif change == "pin":
        metadata["pin"]["content_hash"] = "c" * 64
    elif change == "binding":
        metadata["binding"]["voice"] = "another-choice"
    elif change == "hash":
        metadata["source_voice_reference_hash"] = "bad"
    elif change == "binding_extra":
        metadata["binding"]["path"] = "not-permitted"
    assert source_voice_reference_hash(settings) == ("b" * 64 if change == "valid" else None)


@pytest.mark.parametrize("reviewed", [True, False])
def test_legacy_source_hash_preserves_version1_exact_bytes(reviewed):
    from pandrator.web.generation_audio_identity import (
        _hash,
        audio_cpp_reference_identity,
        audio_cpp_source_reference_hash,
        voice_reference_hash,
    )

    reference = {"sample_id": "sample", "artifact_id": "artifact", "content_hash": "a" * 64,
        "transcript_hash": _hash("Reviewed text." if reviewed else "")}
    assert audio_cpp_reference_identity("sample", "artifact", "a" * 64,
        "  Reviewed text.  ", reviewed) == reference
    expected = _hash(sorted({_hash(reference)}))
    assert voice_reference_hash([reference, reference]) == expected
    assert audio_cpp_source_reference_hash("sample", "artifact", "a" * 64,
        "Reviewed text." if reviewed else "") == expected


@pytest.mark.parametrize("direction", ["legacy_take", "retained_take", "changed", "invalid"])
def test_reference_compatibility_preserves_other_reuse_guards(direction):
    from pandrator.web.generation_audio_identity import IDENTITY_KEY, take_reuse_reason

    legacy = {"schema_version": 1, "settings_hash": "settings", "voice_reference_hash": "a" * 64}
    retained = {**legacy, "voice_reference_hash": "b" * 64, "source_voice_reference_hash": "a" * 64}
    actual, expected = (legacy, retained) if direction == "legacy_take" else (retained, legacy)
    if direction == "changed":
        expected = {**legacy, "voice_reference_hash": "c" * 64}
    if direction == "invalid":
        actual = {**retained, "source_voice_reference_hash": "bad"}
        expected = {**legacy, "voice_reference_hash": "bad"}
    segment, take = m.GenerationSegment(status="completed"), m.AudioTake(status="completed")
    artifact = m.Artifact(state="current", metadata_json={IDENTITY_KEY: actual})
    reason = take_reuse_reason(segment, take, artifact, expected)
    assert reason == ("voice_reference_changed" if direction in {"changed", "invalid"} else "reusable")
    assert take_reuse_reason(segment, take, artifact, {**expected, "settings_hash": "changed"}) == "generation_settings_changed"
    # A compatible reference must never hide changed delivery.
    if direction in {"legacy_take", "retained_take"}:
        assert take_reuse_reason(segment, take, artifact,
            {**expected, "performance_request_hash": "changed"}) == "performance_changed"


def test_source_metadata_is_private_in_project_layers():
    from pandrator.web.project_settings_policy import _connection_override, public_project_settings
    from pandrator.web.speech_input_contracts import PROJECT_SPEECH_SOURCE_KEY

    value = {"tts": {"nested": [{PROJECT_SPEECH_SOURCE_KEY: source_settings()[PROJECT_SPEECH_SOURCE_KEY]}]}}
    assert _connection_override(value)
    assert public_project_settings(value) == {"tts": {"nested": [{}]}}


@pytest.mark.parametrize("saved", [False, True])
def test_current_and_selected_history_reuse_retained_narrator_and_worker_identity(project, monkeypatch, saved):
    from pandrator.web.generation_audio_identity import IDENTITY_KEY, AudioIdentityContext
    from pandrator.web.speech_input_contracts import PROJECT_SPEECH_SOURCE_KEY

    p, r = project, project.r
    defaults(p)
    if saved:
        body(save(p))
    g = admit_generation(p)
    calls = transport(monkeypatch)
    assert g.worker.run_once()
    assert r.f.services.jobs.get(g.result["job_id"]).status == "succeeded"
    assert len(calls) == 1
    current = r.f.services.generation.list_segments(p.source["id"])["items"][0]
    history = r.f.services.generation.list_segments(p.source["id"],
        generation_run_id=g.result["generation_run_id"])["items"][0]
    assert current["has_reusable_take"] and history["has_reusable_take"], (current["audio_reuse_reason"], history["audio_reuse_reason"])
    with r.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, g.result["generation_run_id"])
        segment = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == run.plan_revision_id))
        take = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id))
        assert segment is not None and take is not None and take.artifact_id is not None
        identity = run.settings_snapshot_json["generation_audio_identities"][segment.id]
        assert session.get_one(m.Artifact, take.artifact_id).metadata_json[IDENTITY_KEY] == identity
        assert AudioIdentityContext(session, run.settings_snapshot_json).for_segment(segment) == identity
        assert (PROJECT_SPEECH_SOURCE_KEY in run.settings_snapshot_json["tts"]) is not saved


@pytest.mark.parametrize("change", ["audio", "transcript"])
def test_inherited_current_view_still_rejects_changed_source(project, monkeypatch, change):
    p, r = project, project.r
    defaults(p)
    g = admit_generation(p)
    transport(monkeypatch)
    assert g.worker.run_once()
    assert r.f.services.jobs.get(g.result["job_id"]).status == "succeeded"
    assert r.f.services.generation.list_segments(p.source["id"])["items"][0]["has_reusable_take"]
    if change == "audio":
        import hashlib

        data = bytearray(r.path.read_bytes())
        data[-1] = 1  # publish a valid changed PCM source and its metadata hash
        r.path.write_bytes(data)
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.Artifact, r.aid).content_hash = hashlib.sha256(data).hexdigest()
    else:
        with r.f.case.database.immediate_session() as session:
            session.get_one(m.VoiceSample, r.sid).transcript = "Changed reviewed reference."
    current = r.f.services.generation.list_segments(p.source["id"])["items"][0]
    history = r.f.services.generation.list_segments(p.source["id"],
        generation_run_id=g.result["generation_run_id"])["items"][0]
    assert not current["has_reusable_take"], current
    assert history["has_reusable_take"], history


def test_current_saved_narrator_survives_deleted_origin_with_project_read(project, monkeypatch):
    p, r = project, project.r
    defaults(p)
    body(save(p))
    g = admit_generation(p)
    transport(monkeypatch)
    assert g.worker.run_once()
    with r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, r.sid))
        session.delete(session.get_one(m.Voice, r.voice["id"]))
    r.path.unlink()
    monkeypatch.setattr("pandrator.web.voice_library.resolve_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("saved current view read library"))
    monkeypatch.setattr("pandrator.web.generation_audio_identity.voice_reference_index",
        lambda *_a, **_k: pytest.fail("saved current view indexed library"))
    _token, bearer = scoped_token(r.f.case, r.f.users.alice_id, {"app.read"}, project=p.source["project_id"])
    result = body(r.f.users.alice.get(f"/api/v1/sessions/{p.source['id']}/generation-segments", headers=bearer))
    assert result["items"][0]["has_reusable_take"], result


def test_one_identity_context_keeps_two_sessions_saved_pins_separate(run_project, monkeypatch):
    from pandrator.web.generation_audio_identity import AudioIdentityContext

    g, p, r = run_project, run_project.p, run_project.p.r
    body(save(p))
    first = reference(p).pin
    other = body(r.f.users.alice.post("/api/v1/sessions", json={"name": "Other retained narrator",
        "workspace_id": r.f.users.workspace_id}, headers=headers(r.f.users.ah, "other-root-narrator")), 201)
    second = SimpleNamespace(r=r, source=other, path=f"/api/v1/sessions/{other['id']}/settings/tts")
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.VoiceSample, r.sid).transcript = "A different saved narrator input."
    body(save(second, key="second-root-narrator"))
    second_pin = reference(second).pin
    assert first is not None and second_pin is not None and first != second_pin
    other_plan = g.service.create_plan(other["id"], source_revision_id=None, settings={},
        segments=[{"text": "Other session text.", "language": "en"}])
    snapshot, _digest = g.service.settings.resolve(p.source["id"])
    monkeypatch.setattr("pandrator.web.voice_library.resolve_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("saved root indexed live source"))
    with r.f.case.database.snapshot_session() as session:
        one = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == g.revision))
        two = session.scalar(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == other_plan["active_revision_id"]))
        assert one is not None and two is not None
        context = AudioIdentityContext(session, snapshot)
        first_identity = context.for_segment(one)
        second_identity = context.for_segment(two)
        assert first_identity["voice_reference_hash"] != second_identity["voice_reference_hash"]
        pins = {key[0]: settings[PROJECT_SPEECH_INPUT_KEY] for key, settings in context.effective_settings_cache.items()}
        assert pins == {p.source["id"]: first, other["id"]: second_pin}
        assert context.for_segment(one) == first_identity


def test_binder_replacement_and_ineligibility_clear_source_compatibility(run_project):
    from pandrator.web.project_speech_references import speech_reference_selector_hash
    from pandrator.web.project_tts_settings import bind_project_tts_reference
    from pandrator.web.speech_input_contracts import (
        PROJECT_SPEECH_BINDING_KEY,
        PROJECT_SPEECH_SELECTOR_KEY,
        PROJECT_SPEECH_SOURCE_KEY,
    )

    p = run_project.p
    body(save(p))
    stored = reference(p)
    settings = source_settings()
    settings[PROJECT_SPEECH_BINDING_KEY]["voice"] = "previous-choice"
    settings[PROJECT_SPEECH_SELECTOR_KEY] = speech_reference_selector_hash({"voice": "previous-choice"})
    snapshot = {"tts": settings}
    with p.r.f.case.database.snapshot_session() as session:
        bind_project_tts_reference(session, p.source["id"], snapshot)
    assert snapshot["tts"][PROJECT_SPEECH_INPUT_KEY] == stored.pin
    assert PROJECT_SPEECH_SOURCE_KEY not in snapshot["tts"]
    snapshot["tts"][PROJECT_SPEECH_SOURCE_KEY] = source_settings()[PROJECT_SPEECH_SOURCE_KEY]
    snapshot["tts"].update(service="gemini", model="gemini-2.5-flash-tts", voice="Kore")
    with p.r.f.case.database.snapshot_session() as session:
        bind_project_tts_reference(session, p.source["id"], snapshot)
    assert PROJECT_SPEECH_SOURCE_KEY not in snapshot["tts"]
    assert PROJECT_SPEECH_INPUT_KEY not in snapshot["tts"]


def test_voice_rebinding_strips_narrator_source_compatibility():
    from pandrator.web.generation_cast_runtime import (
        apply_resolved_binding,
        cast_resolution_key,
        filter_segment_tts_override,
    )
    from pandrator.web.speech_input_contracts import PROJECT_SPEECH_SOURCE_KEY

    settings = source_settings()
    alternate = filter_segment_tts_override({"voice_mode_version": 1, "casting_enabled": False}, settings)
    assert PROJECT_SPEECH_SOURCE_KEY not in alternate
    binding = {"voice": "another-choice"}
    applied = apply_resolved_binding(binding, settings,
        {cast_resolution_key(binding): {"voice": "another-choice"}})
    assert applied["voice"] == "another-choice" and PROJECT_SPEECH_SOURCE_KEY not in applied


def test_read_only_managed_narrator_normalization_never_fabricates_retained_pin(run_project, monkeypatch):
    from pandrator.web.project_tts_settings import bind_project_tts_reference

    p = run_project.p
    snapshot = {"tts": {"service": "audio_cpp", "model": MODEL, "voice_id": p.r.voice["id"], "voice": ""}}
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("read-only capture"))
    with p.r.f.case.database.snapshot_session() as session:
        bind_project_tts_reference(session, p.source["id"], snapshot, _resolve_missing_managed=True)
    assert snapshot["tts"]["voice"] == "workshop-reference"
    assert snapshot["tts"]["model"] == MODEL
    assert PROJECT_SPEECH_INPUT_KEY not in snapshot["tts"]
    assert input_paths(p) == []
