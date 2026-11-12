"""Actual audible run choices are captured without changing editable selections."""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from pandrator.web import generation_speech_preparation as preparation
from pandrator.web import models as m
from pandrator.web.generation_audio_identity import plan_audio_identities
from pandrator.web.generation_cast_runtime import segment_render_parts
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.generation_performance_snapshot import performance_runtime_settings
from pandrator.web.jobs import Worker
from pandrator.web.speech_input_contracts import (
    PROJECT_SPEECH_INPUT_KEY,
    PROJECT_SPEECH_SOURCE_KEY,
)
from tests.test_evidence_access_routes import body, headers
from tests.test_generation_run_speech_preparation import run_project as run_project  # noqa: F401
from tests.test_generation_run_speech_preparation import stages, state
from tests.test_project_tts_retention import case as case  # noqa: F401
from tests.test_project_tts_retention import design as design  # noqa: F401
from tests.test_project_tts_retention import input_paths
from tests.test_project_tts_retention import no_network as no_network  # noqa: F401
from tests.test_project_tts_retention import project as project  # noqa: F401
from tests.test_project_tts_retention import retained as retained  # noqa: F401
from tests.test_provider_workspace_access import scoped_token
from tests.test_retained_preview_workspace import transport


def defaults(p, **overrides):
    settings = p.r.f.services.workspace_settings
    current = settings.get(p.source["id"], "tts")
    return settings.update(p.source["id"], "tts", current["revision"],
        {**current["override"], **overrides})


def segment_ids(g):
    with g.p.r.f.case.database.snapshot_session() as session:
        return list(session.scalars(select(m.GenerationSegment.id).where(
            m.GenerationSegment.plan_revision_id == g.revision,
        ).order_by(m.GenerationSegment.ordinal)))


def physical(g, choice):
    with g.p.r.f.case.database.immediate_session() as session:
        segment = session.get_one(m.GenerationSegment, segment_ids(g)[0])
        segment.voice_id, segment.voice = choice.get("voice_id"), choice.get("voice")


def freeze(g, **kwargs):
    return g.service.prepare_start(g.p.source["id"], **kwargs)


def publish(g, prepared):
    from pandrator.web.accounts.execution_actor import execution_actor_scope
    from tests.test_voice_sample_execution_access import api_actor

    p = g.p
    actor, _token = api_actor(p.r.f.case, p.r.f.users.alice_id, p.r.f.users.workspace_id)
    try:
        with execution_actor_scope(actor), p.r.f.case.database.immediate_session() as session:
            return g.service.start_in_session(session, p.source["id"], prepared=prepared)
    finally:
        g.service.cleanup_prepared_start(prepared)


def parts(prepared, identifier):
    snapshot = prepared["frozen_snapshot"]
    return segment_render_parts(performance_runtime_settings(snapshot), snapshot, identifier,
        "A run-owned narration sentence.")


@pytest.mark.parametrize("managed", [False, True])
def test_initial_physical_choice_is_retained_without_saved_row(run_project, managed):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    physical(g, {"voice_id": p.r.voice["id"]} if managed else {"voice": "workshop-reference"})
    before = state(p)
    prepared = freeze(g)
    selected = parts(prepared, segment_ids(g)[0])[0]["settings"]
    assert selected[PROJECT_SPEECH_INPUT_KEY] is not None
    assert selected[PROJECT_SPEECH_SOURCE_KEY]["version"] == 2
    assert PROJECT_SPEECH_INPUT_KEY not in prepared["frozen_snapshot"]["tts"]
    publish(g, prepared)
    assert state(p) == before and len(input_paths(p)) == 2
    with p.r.f.case.database.snapshot_session() as session:
        assert plan_audio_identities(session, g.revision, prepared["frozen_snapshot"]) == prepared["frozen_snapshot"]["generation_audio_identities"]


@pytest.mark.parametrize("managed", [False, True])
def test_selected_identity_overrides_saved_segment_without_inheriting_pin(run_project, managed):
    from pandrator.web.project_speech_references import put_project_speech_reference
    from tests.test_retained_preview_workspace import MODEL

    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    identifier = segment_ids(g)[0]
    with p.r.f.case.database.immediate_session() as session:
        put_project_speech_reference(session, p.source["id"], f"segment:{identifier}",
            selector={"voice": "old-choice"}, binding={"adapter": "audio_cpp", "model": MODEL,
                "voice": "old-choice", "display_name": "Old"}, pin=None, expected_revision=0,
            authorization_guard=lambda _s: None)
    override = {"voice_id": p.r.voice["id"]} if managed else {"voice": "workshop-reference"}
    prepared = freeze(g, selected_segment_override={"tts": override}, segment_ids=[identifier])
    selected = parts(prepared, identifier)[0]["settings"]
    assert selected["voice"] == "workshop-reference" and selected[PROJECT_SPEECH_INPUT_KEY] is not None
    assert prepared["frozen_snapshot"]["generation_control_snapshot"]["segments"][identifier]["voice_binding"].get("voice_id") == override.get("voice_id")
    publish(g, prepared)


def test_strict_single_ignores_alternate_identity(run_project):
    g = run_project
    prepared = freeze(g, selected_segment_override={"tts": {"voice_id": "missing-managed", "voice": "other"}},
        segment_ids=segment_ids(g))
    assert prepared["frozen_snapshot"]["tts"]["voice"] == "workshop-reference"
    assert not (prepared["frozen_snapshot"].get("generation_control_snapshot") or {}).get("resolved_bindings")
    publish(g, prepared)


def second_reference(p):
    from tests.test_voice_crud_workspace_access import create, sample

    r = p.r
    voice = body(create(r.f.users.alice, r.f.users.ah, "Second audible voice",
        wid=r.f.users.workspace_id), 201)
    sid, _aid, _path = sample(r.f.case, voice)
    with r.f.case.database.immediate_session() as session:
        session.get_one(m.Voice, voice["id"]).metadata_json = {"providers": {"audio_cpp": {
            "status": "ready", "resource_kind": "linked_reference", "voice_id": "second-reference",
        }}}
        row = session.get_one(m.VoiceSample, sid)
        row.transcript, row.transcript_reviewed = "Second retained transcript.", True
    return voice


def cast_setup(g, *, fallback=False, two_sources=False):
    p = g.p
    defaults(p, casting_enabled=True, performance_enabled=False)
    cast = {"characters": {"a": {"voice_id": p.r.voice["id"]},
        "b": {"voice": "workshop-reference"}}}
    if two_sources:
        second = second_reference(p)
        cast["characters"]["b"] = {"voice_id": second["id"]}
    with p.r.f.case.database.immediate_session() as session:
        save_generation_controls(session, p.source["id"], expected_revision=0,
            characters=[{"id": "a", "display_name": "A"}, {"id": "b", "display_name": "B"}], cast=cast)
        identifier = segment_ids(g)[0]
        segment = session.get_one(m.GenerationSegment, identifier)
        segment.text = "Alpha speaks. Beta replies." + (" Narrator closes." if fallback else "")
        segment.speech_plan_json = {"speech_xml": f'<segment id="{identifier}">'
            '<dialogue><speaker ref="a">Alpha speaks.</speaker></dialogue> '
            '<dialogue><speaker ref="b">Beta replies.</speaker></dialogue>'
            + (' Narrator closes.' if fallback else '') + '</segment>'}
    return identifier


def test_same_material_across_cast_slots_deduplicates_and_fallback_is_local(run_project):
    g, p = run_project, run_project.p
    identifier = cast_setup(g, fallback=True)
    prepared = freeze(g)
    carrier = prepared["_speech_preparation"]
    assert len(carrier._materials) == 1
    snapshot = prepared["frozen_snapshot"]
    entry = snapshot["generation_control_snapshot"]["segments"][identifier]
    assert entry["controls"]["_base_voice_settings"][PROJECT_SPEECH_INPUT_KEY] is not None
    assert PROJECT_SPEECH_INPUT_KEY not in snapshot["tts"]
    result = publish(g, prepared)
    assert len(input_paths(p)) == 2
    with p.r.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, result["id"])
        assert plan_audio_identities(session, g.revision, run.settings_snapshot_json) == snapshot["generation_audio_identities"]


def test_narrator_fallback_of_one_segment_never_leaks_into_cast_only_sibling(run_project, monkeypatch):
    from pandrator.web import generation_rendering

    g, p = run_project, run_project.p
    cast_setup(g)
    plan = g.service.create_plan(p.source["id"], source_revision_id=None, settings={}, segments=[
        {"text": "Alpha speaks. Beta replies. Narrator closes.", "language": "en"},
        {"text": "Alpha speaks. Beta replies.", "language": "en"},
    ])
    g.revision = plan["active_revision_id"]
    first, second = segment_ids(g)

    def write(first_narrator):
        with p.r.f.case.database.immediate_session() as session:
            for identifier, narrator in ((first, first_narrator), (second, False)):
                segment = session.get_one(m.GenerationSegment, identifier)
                segment.text = "Alpha speaks. Beta replies." + (" Narrator closes." if narrator else "")
                segment.speech_plan_json = {"speech_xml": f'<segment id="{identifier}">'
                    '<dialogue><speaker ref="a">Alpha speaks.</speaker></dialogue> '
                    '<dialogue><speaker ref="b">Beta replies.</speaker></dialogue>'
                    + (' Narrator closes.' if narrator else '') + '</segment>'}

    write(False)
    prepared = freeze(g)
    expected = prepared["frozen_snapshot"]["generation_audio_identities"][second]
    g.service.cleanup_prepared_start(prepared)

    write(True)
    # Controls each segment is rendered with, as the freeze loop hands them over.
    seen = {}
    original = generation_rendering.build_render_parts
    def spy(*args, **kwargs):
        seen.setdefault(kwargs.get("segment_id"), "_base_voice_settings" in (kwargs.get("controls") or {}))
        return original(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(generation_rendering, "build_render_parts", spy)
        prepared = freeze(g)
    snapshot = prepared["frozen_snapshot"]
    entries = snapshot["generation_control_snapshot"]["segments"]
    assert entries[first]["controls"]["_base_voice_settings"][PROJECT_SPEECH_INPUT_KEY] is not None
    assert "controls" not in entries[second]
    assert snapshot["generation_audio_identities"][second] == expected
    # Segments share one controls copy shallowly; the cast-only sibling must not be
    # handed the first segment's narrator fallback.
    assert seen[second] is False
    publish(g, prepared)


def test_all_cast_never_captures_or_reads_unused_narrator(run_project, monkeypatch):
    g = run_project
    cast_setup(g)
    defaults(g.p, casting_enabled=True, voice_id="missing-unused-narrator", voice="missing-unused-alias")
    original = preparation.capture_speech_selection
    calls = []
    def capture(session, paths, owner, selector, settings):
        assert selector.get("voice_id") != "missing-unused-narrator"
        assert settings.get("voice") != "missing-unused-alias"
        calls.append(selector)
        return original(session, paths, owner, selector, settings)
    monkeypatch.setattr(preparation, "capture_speech_selection", capture)
    monkeypatch.setattr("pandrator.web.voice_library.resolve_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("unused narrator source read"))
    prepared = freeze(g)
    assert calls and PROJECT_SPEECH_INPUT_KEY not in prepared["frozen_snapshot"]["tts"]
    publish(g, prepared)


@pytest.mark.parametrize("tamper", ["local", "fallback", "pending", "source", "service", "service_id", "preview_service_id", "preview_adapter", "alternate", "rollback"])
def test_multi_selection_publication_rejects_tampering_and_cleans(run_project, monkeypatch, tamper):
    g, p = run_project, run_project.p
    identifier = cast_setup(g, fallback=True, two_sources=True)
    created = stages(monkeypatch)
    prepared = freeze(g)
    snapshot = prepared["frozen_snapshot"]
    if tamper == "local":
        entry = snapshot["generation_control_snapshot"]["segments"][identifier]
        entry["resolved_bindings"] = {}
    elif tamper == "fallback":
        snapshot["generation_control_snapshot"]["segments"][identifier]["controls"]["_base_voice_settings"][PROJECT_SPEECH_INPUT_KEY] = None
    elif tamper == "pending":
        resolution = next(iter(snapshot["generation_control_snapshot"]["resolved_bindings"].values()))
        resolution[PROJECT_SPEECH_INPUT_KEY]["content_hash"] = "0" * 64
        with p.r.f.case.database.snapshot_session() as session:
            identity = plan_audio_identities(session, g.revision, snapshot,
                _pending_proof=prepared["_speech_preparation"].pending_proof)
            assert any(value.get("performance_error") for value in identity.values())
    elif tamper in {"service", "service_id", "preview_service_id", "preview_adapter"}:
        snapshot["tts"][tamper] = "gemini"
    elif tamper == "alternate":
        snapshot["selected_segment_override"] = {"tts": {"voice_id": "new-choice"}}
    elif tamper == "source":
        with p.r.f.case.database.immediate_session() as session:
            session.get_one(m.VoiceSample, p.r.sid).transcript = "Changed after staging."
    try:
        with pytest.raises(RuntimeError if tamper == "rollback" else ValueError):
            with p.r.f.case.database.immediate_session() as session:
                g.service.start_in_session(session, p.source["id"], prepared=prepared)
                assert tamper == "rollback"
                raise RuntimeError("synthetic rollback")
    finally:
        g.service.cleanup_prepared_start(prepared)
    assert input_paths(p) == []
    assert created and all(not p.r.f.services.paths.managed_path(path).exists() for path in created)


def test_subset_skips_missing_unrequested_choice_and_keeps_full_identity_map(run_project):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    second = second_reference(p)
    with p.r.f.case.database.immediate_session() as session:
        for sample in session.scalars(select(m.VoiceSample).where(m.VoiceSample.voice_id == second["id"])):
            session.delete(sample)
    plan = g.service.create_plan(p.source["id"], source_revision_id=None, settings={}, segments=[
        {"text": "Requested segment.", "voice": "workshop-reference"},
        {"text": "Unrequested segment.", "voice_id": second["id"]},
    ])
    g.revision = plan["active_revision_id"]
    ids = segment_ids(g)
    prepared = freeze(g, segment_ids=[ids[0]])
    assert len(prepared["_speech_preparation"]._materials) == 1
    assert set(prepared["frozen_snapshot"]["generation_audio_identities"]) == set(ids)
    assert not prepared["frozen_snapshot"]["generation_control_snapshot"]["segments"][ids[1]].get("resolved_bindings")
    publish(g, prepared)


@pytest.mark.parametrize("creator_removed", [False, True])
def test_project_only_fresh_denial_and_explicit_run_reuse_after_deletion(run_project, monkeypatch, creator_removed):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    physical(g, {"voice_id": p.r.voice["id"]})
    _token, bearer = scoped_token(p.r.f.case, p.r.f.users.alice_id,
        {"app.read", "app.write", "app.run"}, project=p.source["project_id"])
    path = f"/api/v1/sessions/{p.source['id']}/generation-runs"
    denied = p.r.f.users.alice.post(path, json={}, headers=headers(bearer, "fresh-audible-denied"))
    assert denied.status_code == 404
    first = publish(g, freeze(g))
    with p.r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, p.r.sid))
        session.delete(session.get_one(m.Voice, p.r.voice["id"]))
        if creator_removed:
            session.get_one(m.User, p.r.f.users.alice_id).is_active = False
            session.delete(session.get_one(m.WorkspaceMembership,
                (p.r.f.users.workspace_id, p.r.f.users.alice_id)))
    _peer_token, bearer = scoped_token(p.r.f.case, p.r.f.users.bob_id,
        {"app.read", "app.write", "app.run"}, project=p.source["project_id"])
    p.r.path.unlink()
    monkeypatch.setattr(preparation, "capture_speech_selection", lambda *_a, **_k: pytest.fail("carried reread"))
    result = body(p.r.f.users.bob.post(path, json={"settings_source": "run", "settings_source_run_id": first["id"]},
        headers=headers(bearer, "carried-audible-reuse")), 202)
    with p.r.f.case.database.snapshot_session() as session:
        one, two = session.get_one(m.GenerationRun, first["id"]), session.get_one(m.GenerationRun, result["id"])
        one_cast = one.settings_snapshot_json["generation_control_snapshot"]
        two_cast = two.settings_snapshot_json["generation_control_snapshot"]
        assert two_cast["resolved_bindings"] == one_cast["resolved_bindings"]
        identifier = segment_ids(g)[0]
        assert two_cast["segments"][identifier]["voice_binding"] == one_cast["segments"][identifier]["voice_binding"]
        assert two.settings_snapshot_json["generation_audio_identities"] == one.settings_snapshot_json["generation_audio_identities"]


def test_registered_worker_uses_retained_physical_choice_and_frozen_identity(run_project, monkeypatch):
    from pandrator.web.generation_audio_identity import IDENTITY_KEY

    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    physical(g, {"voice_id": p.r.voice["id"]})
    result = publish(g, freeze(g))
    with p.r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, p.r.sid))
        session.delete(session.get_one(m.Voice, p.r.voice["id"]))
    p.r.path.unlink()
    handlers = p.r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference", lambda *_a, **_k: pytest.fail("legacy source reader"))
    calls = transport(monkeypatch)
    worker = Worker(p.r.f.services.jobs, "audible-input-worker", handlers.handlers())
    assert worker.run_once()
    with p.r.f.case.database.snapshot_session() as session:
        job = session.scalar(select(m.Job).where(m.Job.kind == "generation.run"))
        assert job is not None and job.status == "succeeded", job.error_message if job else "missing"
        run = session.get_one(m.GenerationRun, result["id"])
        take = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id))
        assert take is not None
        artifact = session.get_one(m.Artifact, take.artifact_id)
        assert artifact.metadata_json[IDENTITY_KEY] == run.settings_snapshot_json["generation_audio_identities"][take.generation_segment_id]
        assert "The selected original reference." in str(calls)
        assert session.scalar(select(func.count()).select_from(m.ProjectSpeechReference)) == 0


@pytest.mark.parametrize("failure", ["prepare_second", "publish_second", "role", "lease"])
def test_two_material_failure_is_atomic_and_cleans_every_owned_stage(run_project, monkeypatch, failure):
    g, p = run_project, run_project.p
    cast_setup(g, two_sources=True)
    created = stages(monkeypatch)
    prepare_original, publish_original = preparation.prepare_speech_input, preparation.publish_speech_input
    count = 0
    def prepare(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2 and failure == "prepare_second":
            raise ValueError("synthetic second preparation failure")
        result = prepare_original(*args, **kwargs)
        if count == 2 and failure == "role":
            with p.r.f.case.database.immediate_session() as session:
                session.delete(session.get_one(m.WorkspaceMembership,
                    (p.r.f.users.workspace_id, p.r.f.users.alice_id)))
        return result
    monkeypatch.setattr(preparation, "prepare_speech_input", prepare)
    publications = 0
    def publish_material(*args, **kwargs):
        nonlocal publications
        publications += 1
        result = publish_original(*args, **kwargs)
        if publications == 2 and failure == "publish_second":
            raise ValueError("synthetic second publication failure")
        return result
    monkeypatch.setattr(preparation, "publish_speech_input", publish_material)
    if failure == "role":
        response = p.r.f.users.alice.post(f"/api/v1/sessions/{p.source['id']}/generation-runs",
            json={}, headers=headers(p.r.f.users.ah, "second-input-role-loss"))
        assert response.status_code == 404, response.get_json()
    else:
        lease_active = True
        def guard():
            if not lease_active:
                raise RuntimeError("synthetic multi-input lease loss")
        if failure == "prepare_second":
            with pytest.raises(ValueError, match="second preparation"):
                freeze(g, before_io=guard)
        else:
            prepared = freeze(g, before_io=guard)
            assert len(prepared["_speech_preparation"]._materials) == 2
            if failure == "lease":
                lease_active = False
                # The real admission callback is checked at each byte boundary.
                with pytest.raises(RuntimeError, match="lease loss"):
                    prepared["_speech_preparation"]._io()
                g.service.cleanup_prepared_start(prepared)
            else:
                with pytest.raises(ValueError, match="second publication"):
                    publish(g, prepared)
                assert publications == 2
    assert input_paths(p) == []
    assert created and all(not p.r.f.services.paths.managed_path(path).exists() for path in created)


def test_run_source_preserves_choices_while_current_source_uses_edits(run_project):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    physical(g, {"voice_id": p.r.voice["id"]})
    first = publish(g, freeze(g))
    physical(g, {"voice": "new-null-choice"})
    prepared = freeze(g, settings_source="run", settings_source_run_id=first["id"])
    assert parts(prepared, segment_ids(g)[0])[0]["settings"]["voice"] == "workshop-reference"
    assert "_speech_preparation" not in prepared
    publish(g, prepared)
    current = freeze(g, settings_source="current")
    assert parts(current, segment_ids(g)[0])[0]["settings"]["voice"] == "new-null-choice"
    assert parts(current, segment_ids(g)[0])[0]["settings"][PROJECT_SPEECH_INPUT_KEY] is None
    publish(g, current)


@pytest.mark.parametrize("change", ["pin", "binding", "hash", "extra", "version"])
def test_v2_source_evidence_cannot_broaden_reuse(change):
    from uuid import uuid4

    from pandrator.web.generation_audio_identity import source_voice_reference_hash
    from pandrator.web.speech_input_contracts import (
        PROJECT_SPEECH_BINDING_KEY,
        PreparedSpeechSelection,
        SpeechInputError,
        canonical_json,
    )
    from tests.test_generation_run_speech_preparation import source_settings

    settings = source_settings()
    evidence = {"version": 2, "pin": deepcopy(settings[PROJECT_SPEECH_INPUT_KEY]),
        "binding": deepcopy(settings[PROJECT_SPEECH_BINDING_KEY]), "reference_identity": {
            "sample_id": str(uuid4()), "artifact_id": str(uuid4()),
            "content_hash": "a" * 64, "transcript_hash": "b" * 64}}
    settings[PROJECT_SPEECH_SOURCE_KEY] = evidence
    assert source_voice_reference_hash(settings) is not None
    selection = PreparedSpeechSelection("{}", canonical_json(settings[PROJECT_SPEECH_BINDING_KEY]),
        canonical_json(settings[PROJECT_SPEECH_INPUT_KEY]), canonical_json(evidence))
    copied = selection.source
    assert copied is not None
    copied["reference_identity"]["sample_id"] = "changed-copy"
    unchanged = selection.source
    assert unchanged is not None
    assert unchanged["reference_identity"]["sample_id"] != "changed-copy"
    if change == "pin":
        evidence["pin"]["content_hash"] = "c" * 64
    elif change == "binding":
        evidence["binding"]["voice"] = "different"
    elif change == "hash":
        evidence["reference_identity"]["content_hash"] = "bad"
    elif change == "extra":
        evidence["reference_identity"]["path"] = "not-allowed"
    else:
        evidence["version"] = True
    assert source_voice_reference_hash(settings) is None
    malformed = PreparedSpeechSelection(selection.selector_json, selection.binding_json,
        selection.pin_json, canonical_json(evidence))
    with pytest.raises(SpeechInputError, match="source identity"):
        _ = malformed.source


def test_partial_stale_generation_preserves_prior_take_and_full_identity_map(run_project, monkeypatch):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    plan = g.service.create_plan(p.source["id"], source_revision_id=None, settings={}, segments=[
        {"text": "First audible sentence.", "voice": "workshop-reference"},
        {"text": "Second audible sentence.", "voice": "workshop-reference"},
    ])
    g.revision = plan["active_revision_id"]
    ids = segment_ids(g)
    first = publish(g, freeze(g))
    transport(monkeypatch)
    handlers = p.r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(p.r.f.services.jobs, "partial-audible-worker", handlers.handlers())
    assert worker.run_once()
    with p.r.f.case.database.immediate_session() as session:
        run = session.get_one(m.GenerationRun, first["id"])
        assert run.status == "completed", run.status
        session.get_one(m.GenerationSegment, ids[0]).status = "pending"
        preserved = session.scalar(select(m.AudioTake).where(
            m.AudioTake.generation_segment_id == ids[1], m.AudioTake.is_active.is_(True)))
        assert preserved is not None
        preserved_id = preserved.id
    prepared = freeze(g, stale_only=True)
    assert prepared["requested_segment_ids"] == [ids[0]]
    assert prepared["reusable_take_ids"] == {ids[1]: preserved_id}
    assert set(prepared["frozen_snapshot"]["generation_audio_identities"]) == set(ids)
    second = publish(g, prepared)
    assert worker.run_once()
    with p.r.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, second["id"])
        assert run.status == "completed", run.status
        takes = list(session.scalars(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)))
        assert len(takes) == 2


@pytest.mark.parametrize("xml_body,text", [
    ('<dialogue><speaker ref="a">Alpha.</speaker></dialogue> '
     '<dialogue><speaker ref="b">Beta.</speaker></dialogue>', 'Alpha. Beta.'),
    ('... <dialogue><speaker ref="a">Alpha.</speaker></dialogue> ...', '... Alpha. ...'),
    ('<narrator>Start.</narrator> '
     '<dialogue><speaker ref="a">Alpha.</speaker></dialogue> ...', 'Start. Alpha. ...'),
])
def test_v2_separator_optimization_preserves_existing_primary_parts(xml_body, text):
    from uuid import uuid4

    from pandrator.logic.speech_performance import compile_performance, content_hash
    from pandrator.web.generation_audio_identity import _hash, _material_settings
    from pandrator.web.generation_cast_runtime import apply_resolved_binding, cast_resolution_key
    from pandrator.web.generation_rendering import build_render_parts

    identifier = str(uuid4())
    settings = {"service": "gemini", "model": "gemini-2.5-flash-tts", "voice": "Narrator",
        "casting_enabled": True, "performance_enabled": True,
        "_subtitle_timed": False, "in_segment_voice_change_silence_ms": 0}
    controls = {"characters": [{"id": "a", "display_name": "A"}, {"id": "b", "display_name": "B"}],
        "cast": {"characters": {"a": {"voice": "Alpha"}, "b": {"voice": "Beta"}}}}
    resolutions = {cast_resolution_key({"voice": voice}, f'cast:["characters","{character}"]', schema_version=2):
        {"voice": voice} for character, voice in (("a", "Alpha"), ("b", "Beta"))}
    xml = f'<segment id="{identifier}">{xml_body}</segment>'
    old = build_render_parts(text, settings, speech_xml=xml, segment_id=identifier, controls=controls,
        apply_binding_at_slot=lambda binding, base, slot: apply_resolved_binding(binding, base,
            resolutions, slot_key=slot, schema_version=2))
    snapshot = {"generation_control_snapshot": {"schema_version": 2, "plan_revision_id": str(uuid4()),
        "controls": controls, "resolved_bindings": resolutions, "segments": {identifier: {
            "text_hash": content_hash(text), "speech_xml": xml, "voice_binding": None,
            "source_speaker": None, "subtitle_timed": False}}}}
    current = segment_render_parts(settings, snapshot, identifier, text)
    def primary(parts):
        return [{"range": [part["start"], part["end"]], "text": part["text"],
            "settings": _material_settings({"tts": part["settings"]}),
            "references": [_hash(part["settings"]["voice"])],
            "performance": compile_performance(part["text"], part["settings"]).fingerprint}
            for part in parts]
    assert primary(current) == primary(old)


def test_two_cast_voices_and_fallback_preserve_ordered_alternate_reference_hash(run_project):
    from pandrator.web.generation_audio_identity import _hash, source_reference_identity

    g = run_project
    identifier = cast_setup(g, fallback=True, two_sources=True)
    prepared = freeze(g)
    snapshot = prepared["frozen_snapshot"]
    with g.p.r.f.case.database.snapshot_session() as session:
        segment = session.get_one(m.GenerationSegment, identifier)
        rendered = segment_render_parts(performance_runtime_settings(snapshot), snapshot, identifier, segment.text)
    assert len(rendered) == 3 and len(prepared["_speech_preparation"]._materials) == 2
    alternate = [[_hash(source_reference_identity(part["settings"]))] for part in rendered]
    primary = [[_hash({"retained_content_hash": part["settings"][PROJECT_SPEECH_INPUT_KEY]["content_hash"]})]
        for part in rendered]
    identity = snapshot["generation_audio_identities"][identifier]
    assert identity["voice_reference_hash"] == _hash(primary)
    assert identity["source_voice_reference_hash"] == _hash(alternate)
    assert identity["source_voice_reference_hash"] != _hash([alternate[1], alternate[0], alternate[2]])
    publish(g, prepared)



def test_current_and_history_physical_reuse_tracks_changed_source(run_project, monkeypatch):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    physical(g, {"voice_id": p.r.voice["id"]})
    result = publish(g, freeze(g))
    transport(monkeypatch)
    handlers = p.r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    worker = Worker(p.r.f.services.jobs, "physical-current-history", handlers.handlers())
    assert worker.run_once()
    current = g.service.list_segments(p.source["id"])["items"][0]
    history = g.service.list_segments(p.source["id"], generation_run_id=result["id"])["items"][0]
    assert current["has_reusable_take"] and history["has_reusable_take"], (
        current["audio_reuse_reason"], history["audio_reuse_reason"])
    with p.r.f.case.database.immediate_session() as session:
        session.get_one(m.VoiceSample, p.r.sid).transcript = "Changed physical source transcript."
    current = g.service.list_segments(p.source["id"])["items"][0]
    history = g.service.list_segments(p.source["id"], generation_run_id=result["id"])["items"][0]
    assert not current["has_reusable_take"] and history["has_reusable_take"]


@pytest.mark.parametrize("alternate,expected", [
    ({"voice_id": "selected", "voice": " selected-alias ", "speaker": "unused"},
     {"voice_id": "selected", "voice": "selected-alias"}),
    ({"voice_id": " ", "voice": " ", "speaker": " speaker-alias "}, {"voice": "speaker-alias"}),
    ({"voice": "selected-alias", "speaker": "unused"}, {"voice": "selected-alias"}),
    ({}, {"voice_id": "persisted"}),
])
def test_selected_raw_voice_precedence_is_finite_and_sparse(alternate, expected):
    from pandrator.web.generation_cast_runtime import segment_voice_binding

    segment = m.GenerationSegment(voice_id="persisted", voice="persisted-alias")
    snapshot = {"tts": {"voice_mode_version": 0}, "selected_segment_override": {"tts": alternate}}
    assert segment_voice_binding(segment, snapshot) == expected



def test_metadata_preview_keeps_complete_cast_metadata_without_staging(run_project, monkeypatch):
    g, p = run_project, run_project.p
    defaults(p, voice_mode_version=0)
    plan = g.service.create_plan(p.source["id"], source_revision_id=None, settings={}, segments=[
        {"text": "First metadata choice.", "voice": "workshop-reference"},
        {"text": "Second metadata choice.", "voice": "workshop-reference"},
    ])
    g.revision = plan["active_revision_id"]
    ids = segment_ids(g)
    monkeypatch.setattr(preparation, "capture_speech_selection",
        lambda *_a, **_k: pytest.fail("metadata preview captured"))
    prepared = freeze(g, segment_ids=[ids[0]], retain_speech_inputs=False)
    assert "_speech_preparation" not in prepared
    frozen = prepared["frozen_snapshot"]["generation_control_snapshot"]
    assert len(frozen["resolved_bindings"]) == 2
    assert set(frozen["segments"]) == set(ids)
    assert input_paths(p) == []



@pytest.mark.parametrize("requested_index", [0, 1])
def test_shared_cast_metadata_cannot_bypass_requested_capture(run_project, monkeypatch, requested_index):
    from pandrator.web.generation_audio_identity import IDENTITY_KEY

    g, p = run_project, run_project.p
    cast_setup(g)
    plan = g.service.create_plan(p.source["id"], source_revision_id=None, settings={}, segments=[
        {"text": "First shared character."}, {"text": "Second shared character."},
    ])
    g.revision = plan["active_revision_id"]
    ids = segment_ids(g)
    with p.r.f.case.database.immediate_session() as session:
        for identifier in ids:
            segment = session.get_one(m.GenerationSegment, identifier)
            segment.speech_plan_json = {"speech_xml": f'<segment id="{identifier}">'
                f'<dialogue><speaker ref="a">{segment.text}</speaker></dialogue></segment>'}
    prepared = freeze(g, segment_ids=[ids[requested_index]])
    snapshot = prepared["frozen_snapshot"]
    carrier = prepared["_speech_preparation"]
    assert len(carrier._materials) == 1
    resolutions = snapshot["generation_control_snapshot"]["resolved_bindings"]
    assert len(resolutions) == 1
    pin = next(iter(resolutions.values()))[PROJECT_SPEECH_INPUT_KEY]
    assert pin is not None
    result = publish(g, prepared)
    assert len(input_paths(p)) == 2
    with p.r.f.case.database.immediate_session() as session:
        session.delete(session.get_one(m.VoiceSample, p.r.sid))
        session.delete(session.get_one(m.Voice, p.r.voice["id"]))
    p.r.path.unlink()
    with p.r.f.case.database.snapshot_session() as session:
        assert plan_audio_identities(session, g.revision, snapshot) == snapshot["generation_audio_identities"]
    handlers = p.r.f.case.app.extensions["pandrator"]["workflow_handlers"]
    monkeypatch.setattr(handlers, "prepare_audio_cpp_voice_reference",
        lambda *_a, **_k: pytest.fail("shared cast used live source bytes"))
    calls = transport(monkeypatch)
    worker = Worker(p.r.f.services.jobs, "shared-cast-ordering", handlers.handlers())
    assert worker.run_once()
    with p.r.f.case.database.snapshot_session() as session:
        run = session.get_one(m.GenerationRun, result["id"])
        job = session.get_one(m.Job, run.job_id)
        assert job.status == "succeeded", job.error_message
        takes = list(session.scalars(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)))
        assert len(takes) == 1 and takes[0].generation_segment_id == ids[requested_index]
        artifact = session.get_one(m.Artifact, takes[0].artifact_id)
        assert artifact.metadata_json[IDENTITY_KEY] == snapshot["generation_audio_identities"][ids[requested_index]]
    assert len(calls) == 1
