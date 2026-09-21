"""Casting reaches the real generation runner without splitting logical takes."""

import threading
from unittest.mock import patch

import pytest
from pydub import AudioSegment
from sqlalchemy import select

from tests.test_performance_plans import case as case, create, adopt, get
from pandrator.logic.speech_performance import compile_performance
from pandrator.web import models as m
from pandrator.web.generation_controls import (
    get_generation_controls,
    save_generation_controls,
)
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.generation_cast_runtime import combine_source_markup
from pandrator.web.tts_providers import TtsCapabilities


def test_cast_design_description_keeps_general_delivery_direction(monkeypatch):
    from pandrator.web import generation_cast_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "resolve_capabilities",
        lambda *_args: {"voice_design": True, "model": "design-model"},
    )
    binding = {"voice_description": "An older resonant voice."}
    settings = {
        "service": "audio_cpp",
        "model": "design-model",
        "generation_prompt": "Read quietly.",
    }
    resolved = runtime.resolve_binding(None, binding, settings)
    applied = runtime.apply_resolved_binding(
        binding, settings, {runtime._binding_key(binding): resolved}
    )
    assert applied["generation_prompt"] == "An older resonant voice.\nRead quietly."
    assert applied["openai_audio_instructions"] == applied["generation_prompt"]


def test_cast_voice_replaces_legacy_provider_id_and_narrator_reference():
    from pandrator.web import generation_cast_runtime as runtime

    binding = {"voice": "character-provider-id"}
    settings = {
        "voice": "narrator",
        "speaker": "narrator",
        "elevenlabs_voice_id": "narrator",
        "audio_cpp_voice_ref": "narrator-waveform",
        "audio_cpp_reference_text": "Narrator reference.",
    }
    applied = runtime.apply_resolved_binding(
        binding, settings, {runtime._binding_key(binding): binding}
    )
    assert (
        applied["voice"]
        == applied["speaker"]
        == applied["elevenlabs_voice_id"]
        == "character-provider-id"
    )
    assert "audio_cpp_voice_ref" not in applied
    assert "audio_cpp_reference_text" not in applied


@pytest.mark.parametrize("model", ["qwen3_tts_1_7b_customvoice_q8_0", "qwen3_tts_1_7b_voicedesign_q8_0"])
def test_prebuilt_and_design_models_reject_cloned_cast(model):
    from pandrator.web.generation_cast_runtime import resolve_binding

    with pytest.raises(ValueError, match="cloned cast"):
        resolve_binding(None, {"voice_id": "managed-reference"}, {"service": "audio_cpp", "model": model})


def test_customvoice_requires_a_native_speaker():
    from pandrator.web.generation_cast_runtime import resolve_binding

    settings = {"service": "audio_cpp", "model": "qwen3_tts_1_7b_customvoice_q8_0"}
    with pytest.raises(ValueError, match="built-in speaker"):
        resolve_binding(None, {"voice": "My cloned Scrooge"}, settings)
    assert resolve_binding(None, {"voice": "Ryan"}, settings)["voice"] == "Ryan"


def test_preview_service_override_keeps_cast_and_reports_live_readiness(case):
    plan, _xml = seed_cast(case)
    catalogue = case["services"]["tts_catalogue"]
    with patch.object(catalogue, "snapshot", return_value=({"services": [{"id": "audio_cpp", "available": True, "models": []}]}, 1)) as refresh:
        response = case["post"]("/" + plan["id"] + "/preview", {
            "segment_id": case["segment_ids"][0], "service": "audio_cpp",
            "model": "qwen3_tts_1_7b_base_q8_0", "casting_enabled": True,
        })
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert len(result["parts"]) == 3
    assert result["readiness"]["model_available"] is False
    assert result["readiness"]["compilation_only"] is True
    assert result["generation_source"]["plan_revision_id"] == case["revision_id"]
    refresh.assert_called_once_with(refresh=True)


@pytest.mark.parametrize("cancel_at", ["part", "verification"])
def test_canceled_cast_never_publishes_a_take(case, cancel_at):
    seed_cast(case)
    services = case["services"]
    handlers = services["workflow_handlers"]
    started = services["generation"].start(
        case["session_id"],
        speech_plan_revision_id=case["revision_id"],
        run_override=overrides(),
    )
    event = threading.Event()

    def synthesize(*_args, **_kwargs):
        if cancel_at == "part":
            event.set()
        return AudioSegment.silent(duration=20)

    def verify(*_args, **_kwargs):
        # Simulate an MCP/API cancellation during post-synthesis verification.
        with services["database"].session() as session:
            session.get(m.GenerationRun, started["id"]).cancel_requested = True
        return None

    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(handlers, "_verification_metadata", side_effect=verify),
    ):
        result = handlers.run_generation(
            {"generation_run_id": started["id"]}, lambda *_args: None, event
        )
    assert result["status"] == "canceled"
    with services["database"].session() as session:
        assert session.get(m.GenerationRun, started["id"]).status == "canceled"
        assert not list(session.scalars(select(m.AudioTake)))
        assert not list(
            session.scalars(
                select(m.Artifact).where(m.Artifact.role == "generation_take")
            )
        )
    assert not list(services["paths"].sessions.rglob("tts-*.wav"))


def seed_cast(case):
    db = case["services"]["database"]
    with db.session() as session:
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=0,
            characters=[
                {"id": "c-scrooge", "display_name": "Scrooge", "voice_category": "male"}
            ],
            cast={
                "narrator": {"voice": "Kore"},
                "characters": {"c-scrooge": {"voice": "Puck"}},
            },
        )
        segment = session.get(m.GenerationSegment, case["segment_ids"][0])
        segment.text = "He said, “Bah!” Then left."
    plan = create(case, annotation_format="xml")
    sid = case["segment_ids"][0]
    xml = f'<segment id="{sid}">He said, <dialogue><speaker ref="c-scrooge"><em>cross</em>“Bah!”</speaker></dialogue> Then left.</segment>'
    edited = case["post"](
        "/" + plan["id"],
        {
            "expected_version": plan["version"],
            "items": [{"segment_id": sid, "speech_xml": xml}],
        },
        method="patch",
    )
    assert edited.status_code == 200, edited.get_json()
    return adopt(case, get(case, plan)), xml


def overrides(**extra):
    return {
        "tts": {
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
            "voice": "Kore",
            "casting_enabled": True,
            "performance_enabled": True,
            "tts_batch_size": 4,
            **extra,
        }
    }


@pytest.mark.parametrize("casting_enabled", [False, True])
def test_managed_block_voices_reach_provider_and_invalidate_narrator_takes(case, casting_enabled):
    services = case["services"]
    database = services["database"]
    handlers = services["workflow_handlers"]
    run_override = overrides(casting_enabled=casting_enabled, performance_enabled=False, tts_batch_size=1)
    with database.session() as session:
        save_generation_controls(session, case["session_id"], expected_revision=0,
                                 cast={"narrator": {"voice": "Kore"}})
        old_identity = AudioIdentityContext(session, run_override).for_segment(
            session.get(m.GenerationSegment, case["segment_ids"][1]))
        for segment_id, provider_voice in zip(case["segment_ids"][1:], ["Puck", "Charon"], strict=True):
            voice = m.Voice(name=provider_voice, metadata_json={"providers": {
                "gemini": {"status": "ready", "provider_voice_id": provider_voice}}})
            session.add(voice)
            session.flush()
            session.get(m.GenerationSegment, segment_id).voice_id = voice.id
    started = services["generation"].start(case["session_id"],
        speech_plan_revision_id=case["revision_id"], run_override=run_override)
    requests = []

    def synthesize(text, settings, **kwargs):
        requests.append(settings["voice"])
        return AudioSegment.silent(duration=20)

    with patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize):
        handlers.run_generation({"generation_run_id": started["id"]}, lambda *_: None, threading.Event())
    assert requests == ["Kore", "Puck", "Charon"]
    with database.session() as session:
        run = session.get(m.GenerationRun, started["id"])
        assert run.status == "completed"
        identities = run.settings_snapshot_json["generation_audio_identities"]
        assert identities[case["segment_ids"][1]] != old_identity
        takes = list(session.scalars(select(m.AudioTake).where(m.AudioTake.generation_run_id == run.id)))
        assert len(takes) == 3
        if casting_enabled:
            voices = {}
            for take in takes:
                artifact = session.get(m.Artifact, take.artifact_id)
                voices[take.generation_segment_id] = artifact.metadata_json["render_parts"][0]["voice"]
            assert [voices[key] for key in case["segment_ids"]] == requests


def test_block_binding_overrides_cast_markup_and_is_frozen(case):
    from pandrator.web.generation_cast_runtime import (
        freeze_cast_snapshot,
        segment_render_parts,
    )
    from pandrator.web.generation_rendering import build_render_parts

    settings = overrides(performance_enabled=False)["tts"]
    with case["services"]["database"].session() as session:
        voice = m.Voice(name="Scrooge", metadata_json={"providers": {
            "gemini": {"status": "ready", "provider_voice_id": "Puck"}}})
        session.add(voice)
        session.flush()
        segment = session.get(m.GenerationSegment, case["segment_ids"][0])
        segment.voice_id = voice.id
        snapshot = {"tts": settings}
        freeze_cast_snapshot(session, case["revision_id"], snapshot, settings)
        voice.metadata_json = {"providers": {"gemini": {"status": "ready", "provider_voice_id": "Charon"}}}
        parts = segment_render_parts(settings, snapshot, segment.id, segment.text)
        assert parts[0]["settings"]["voice"] == "Puck"
    xml = '<segment id="s"><narrator>Hello.</narrator></segment>'
    parts = build_render_parts("Hello.", settings, segment_id="s", speech_xml=xml,
        controls={"cast": {"narrator": {"voice": "Kore"}}}, voice_binding={"voice": "Puck"})
    assert parts[0]["settings"]["voice"] == "Puck"
    assert parts[0]["voice_source"] == "segment"


def test_missing_managed_block_voice_blocks_generation(case):
    with case["services"]["database"].session() as session:
        voice = m.Voice(name="Unpublished character", metadata_json={})
        session.add(voice)
        session.flush()
        session.get(m.GenerationSegment, case["segment_ids"][0]).voice_id = voice.id
    with pytest.raises(ValueError, match="Publish or link"):
        case["services"]["generation"].start(case["session_id"],
            speech_plan_revision_id=case["revision_id"],
            run_override=overrides(performance_enabled=False))


def test_generation_snapshot_compilation_does_not_hold_sqlite_writer(case):
    import sqlite3

    from pandrator.web import generation_audio_identity, speech_plan_workspace

    services = case["services"]
    checked = []

    def check_writer_free(original):
        def wrapped(*args, **kwargs):
            with sqlite3.connect(services["database"].path, timeout=0.05) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            checked.append(original.__name__)
            return original(*args, **kwargs)
        return wrapped

    with (
        patch.object(speech_plan_workspace, "freeze_speech_snapshot",
                     side_effect=check_writer_free(speech_plan_workspace.freeze_speech_snapshot)),
        patch.object(generation_audio_identity, "plan_audio_identities",
                     side_effect=check_writer_free(generation_audio_identity.plan_audio_identities)),
    ):
        started = services["generation"].start(case["session_id"],
            speech_plan_revision_id=case["revision_id"], run_override=overrides(performance_enabled=False))
    assert started["status"] == "queued"
    assert checked == ["freeze_speech_snapshot", "plan_audio_identities"]


@pytest.mark.parametrize("change", ["text", "cast", "voice"])
def test_generation_commit_rejects_changed_prepared_inputs(case, change):
    from pandrator.web.workspace import RevisionConflict

    services = case["services"]
    with services["database"].session() as session:
        voice = m.Voice(name="Managed block", metadata_json={"providers": {
            "gemini": {"status": "ready", "provider_voice_id": "Puck"}}})
        session.add(voice)
        session.flush()
        voice_id = voice.id
        session.get(m.GenerationSegment, case["segment_ids"][0]).voice_id = voice_id
    prepared = services["generation"].prepare_start(case["session_id"],
        speech_plan_revision_id=case["revision_id"], run_override=overrides(performance_enabled=False))
    with services["database"].session() as session:
        if change == "text":
            session.get(m.GenerationSegment, case["segment_ids"][0]).text = "Changed words."
        elif change == "cast":
            save_generation_controls(session, case["session_id"], expected_revision=0,
                                     cast={"narrator": {"voice": "Charon"}})
        else:
            session.get(m.Voice, voice_id).metadata_json = {"providers": {
                "gemini": {"status": "ready", "provider_voice_id": "Charon"}}}
    with (
        pytest.raises(RevisionConflict, match="changed while generation"),
        services["database"].immediate_session() as session,
    ):
        services["generation"].start_in_session(session, case["session_id"], prepared=prepared)
    with services["database"].session() as session:
        assert not list(session.scalars(select(m.GenerationRun)))
        assert not list(session.scalars(select(m.Job)))


def test_repeated_cast_binding_is_resolved_once(case):
    from pandrator.web import generation_cast_runtime as runtime

    with case["services"]["database"].session() as session:
        save_generation_controls(session, case["session_id"], expected_revision=0,
                                 cast={"narrator": {"voice": "Kore"}})
        snapshot = overrides(performance_enabled=False)
        with patch.object(runtime, "resolve_binding", wraps=runtime.resolve_binding) as resolve:
            runtime.freeze_cast_snapshot(session, case["revision_id"], snapshot, snapshot["tts"])
        assert resolve.call_count == 1


def test_generation_commit_rejects_source_run_swap_with_identical_settings(case):
    from pandrator.web.workspace import RevisionConflict

    services = case["services"]
    snapshot = overrides(performance_enabled=False)
    with services["database"].session() as session:
        original = m.GenerationRun(session_id=case["session_id"],
            plan_revision_id=case["revision_id"], sequence_number=1,
            status="completed", settings_snapshot_json=snapshot)
        session.add(original)
        session.flush()
        original_id = original.id
    prepared = services["generation"].prepare_start(case["session_id"],
        segment_ids=[case["segment_ids"][0]], operation="regenerate",
        speech_plan_revision_id=case["revision_id"])
    assert prepared["snapshot_source_run_id"] == original_id
    with services["database"].session() as session:
        session.add(m.GenerationRun(session_id=case["session_id"],
            plan_revision_id=case["revision_id"], sequence_number=2,
            status="completed", settings_snapshot_json=snapshot))
    with (
        pytest.raises(RevisionConflict, match="changed while generation"),
        services["database"].immediate_session() as session,
    ):
        services["generation"].start_in_session(session, case["session_id"], prepared=prepared)
    with services["database"].session() as session:
        assert len(list(session.scalars(select(m.GenerationRun)))) == 2
        assert not list(session.scalars(select(m.Job)))


def test_composite_generation_frozen_cast_and_one_take_per_segment(case):
    plan, xml = seed_cast(case)
    handlers = case["services"]["workflow_handlers"]
    started = case["services"]["generation"].start(
        case["session_id"],
        speech_plan_revision_id=case["revision_id"],
        run_override=overrides(),
    )
    requests = []

    def synthesize(text, settings, **kwargs):
        requests.append((text, settings["voice"], compile_performance(text, settings)))
        if len(requests) == 1:
            with case["services"]["database"].session() as session:
                controls = get_generation_controls(session, case["session_id"])
                controls["cast"]["characters"]["c-scrooge"]["voice"] = "Charon"
                save_generation_controls(
                    session,
                    case["session_id"],
                    expected_revision=controls["revision"],
                    cast=controls["cast"],
                )
        return AudioSegment.silent(duration=20)

    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(
            handlers.tts_providers,
            "synthesis_capabilities",
            return_value=TtsCapabilities(
                batch_synthesis=True, streaming_batch=True, max_batch_size=4
            ),
        ),
        patch.object(
            handlers.tts_providers,
            "synthesize_batch",
            side_effect=AssertionError("A cast segment must own its parts"),
        ),
    ):
        handlers.run_generation(
            {"generation_run_id": started["id"]}, lambda *args: None, threading.Event()
        )
    assert [voice for _, voice, _ in requests[:3]] == ["Kore", "Puck", "Kore"]
    assert "".join(text for text, _, _ in requests[:3]) == "He said, “Bah!” Then left."
    assert "cross" in requests[1][2].input
    assert "<" not in "".join(item[0] for item in requests)
    with case["services"]["database"].session() as session:
        takes = list(
            session.scalars(
                select(m.AudioTake).where(
                    m.AudioTake.generation_run_id == started["id"]
                )
            )
        )
        assert len(takes) == 3
        first = next(
            item
            for item in takes
            if item.generation_segment_id == case["segment_ids"][0]
        )
        artifact = session.get(m.Artifact, first.artifact_id)
        assert first.duration_ms == 60
        assert len(artifact.metadata_json["render_parts"]) == 3
        assert all(
            "settings" not in part for part in artifact.metadata_json["render_parts"]
        )
    # The draft preview sees the current cast; the completed run retained Puck.
    response = case["post"](
        "/" + plan["id"] + "/preview",
        {
            "segment_id": case["segment_ids"][0],
            "speech_xml": xml,
            "casting_enabled": True,
            "service": "gemini",
            "model": "gemini-2.5-flash-tts",
        },
    )
    assert response.status_code == 200, response.get_json()
    assert [part["voice"] for part in response.get_json()["parts"]] == [
        "Kore",
        "Charon",
        "Kore",
    ]


def test_casting_works_without_directions_and_failure_never_publishes_partial_take(
    case,
):
    seed_cast(case)
    handlers = case["services"]["workflow_handlers"]
    started = case["services"]["generation"].start(
        case["session_id"],
        speech_plan_revision_id=case["revision_id"],
        run_override=overrides(performance_enabled=False),
    )
    calls = []

    def synthesize(text, settings, **kwargs):
        calls.append((text, settings))
        assert "_performance" not in settings
        if len(calls) == 2:
            raise RuntimeError("second voice unavailable")
        return AudioSegment.silent(duration=20)

    with (
        patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize),
        patch.object(
            handlers.tts_providers,
            "synthesis_capabilities",
            return_value=TtsCapabilities(),
        ),
    ):
        with pytest.raises(RuntimeError, match="second voice"):
            handlers.run_generation(
                {"generation_run_id": started["id"]},
                lambda *args: None,
                threading.Event(),
            )
    with case["services"]["database"].session() as session:
        assert not list(
            session.scalars(
                select(m.AudioTake).where(
                    m.AudioTake.generation_run_id == started["id"]
                )
            )
        )


def test_only_effective_cast_changes_invalidate_identity(case):
    seed_cast(case)
    settings, _ = case["services"]["workspace_settings"].resolve(
        case["session_id"], run_override=overrides()
    )
    with case["services"]["database"].session() as session:
        first = session.get(m.GenerationSegment, case["segment_ids"][0])
        other = session.get(m.GenerationSegment, case["segment_ids"][1])
        old = AudioIdentityContext(session, settings).for_segment(first)
        old_other = AudioIdentityContext(session, settings).for_segment(other)
        from copy import deepcopy

        changed_base = deepcopy(settings)
        changed_base["tts"].update(voice="Unused voice", speaker="Unused voice")
        assert AudioIdentityContext(session, changed_base).for_segment(first) == old
        controls = get_generation_controls(session, case["session_id"])
        controls["characters"][0]["notes"] = "An older man."
        controls["characters"][0]["aliases"] = ["Ebenezer"]
        controls = save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            characters=controls["characters"],
        )
        assert AudioIdentityContext(session, settings).for_segment(first) == old
        controls["cast"]["characters"]["c-scrooge"]["voice"] = "Charon"
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=controls["revision"],
            cast=controls["cast"],
        )
        assert AudioIdentityContext(session, settings).for_segment(first) != old
        assert AudioIdentityContext(session, settings).for_segment(other) == old_other


def test_markup_import_preserves_source_scopes_and_dialogue_pauses(case):
    handlers = case["services"]["workflow_handlers"]
    records = [
        {
            "original_sentence": "“Hello.”",
            "paragraph": "yes",
            "speech_xml": '<segment id="1" boundary_after="dialogue_turn"><dialogue>“Hello.”</dialogue></segment>',
        }
    ]
    revision, ids = handlers._store_generation_plan(
        case["session_id"],
        records,
        settings={"paragraph_silence_ms": 700, "sentence_silence_ms": 120},
    )
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, ids[0])
        assert segment.silence_after_ms == 120
        assert not segment.paragraph_break_after
        assert f'id="{segment.id}"' in segment.speech_plan_json["speech_xml"]
    xml = combine_source_markup(
        [("One.", '<segment id="1"><em>happy</em>One.</segment>'), ("Two.", None)],
        "combined",
        "One. Two.",
        [],
    )
    assert "<span><em>happy</em>One.</span> <span>Two.</span>" in xml
    with pytest.raises(ValueError, match="no longer matches"):
        combine_source_markup([("One.", None)], "combined", "Rewritten.", [])


def test_passive_xml_only_submission_survives_artifact_and_plan_import(case):
    import json
    import uuid

    database = case["services"]["database"]
    with database.session() as session:
        record = session.get(m.SessionRecord, case["session_id"])
        storage_key = record.storage_key
    path = case["services"]["paths"].sessions / storage_key / "cast-source.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"original_sentence": "Hello."}, {"original_sentence": "Goodbye."}])
    )
    artifact = case["services"]["artifacts"].register(
        path, kind="json", role="prepared_text", session_id=case["session_id"]
    )
    case["services"]["workspace_settings"].update(case["session_id"], "text", 0,
        {"llm_tts_document_optimization": True, "llm_tts_annotation_mode": "speakers",
         "llm_tts_annotation_only": True})

    def post(url, body, key=None):
        return case["client"].post(
            "/api/v1/" + url,
            json=body,
            headers={**case["headers"], "Idempotency-Key": key or str(uuid.uuid4())},
        )

    run = post(
        f"sessions/{case['session_id']}/speech-optimization-dispatch-runs",
        {
            "source_artifact_id": artifact.id,
            "annotation_mode": "speakers",
            "annotation_only": True,
        },
    )
    assert run.status_code == 201, run.get_json()
    claim = post(
        f"speech-optimization-dispatch-runs/{run.get_json()['id']}/claim",
        {"lease_seconds": 900},
    ).get_json()
    assert claim["character_dictionary"]["entries"] == []
    body = {
        "lease_token": claim["lease_token"],
        "character_proposals": [
            {"id": "c-new", "display_name": "Alice", "voice_category": "female"}
        ],
        "result": {
            "kind": "speech_optimization",
            "items": [
                {
                    "unit_id": 1,
                    "speech_xml": '<segment id="1" boundary_after="dialogue_turn"><dialogue><speaker ref="c-new">Hello.</speaker></dialogue></segment>',
                },
                {"unit_id": 2, "speech_xml": '<segment id="2">Goodbye.</segment>'},
            ],
        },
    }
    key = str(uuid.uuid4())
    url = f"speech-optimization-dispatch-batches/{claim['batch_id']}/submit"
    submitted = post(url, body, key)
    assert submitted.status_code in {200, 202}, submitted.get_json()
    repeated = post(url, body, key)
    assert repeated.status_code == submitted.status_code
    result = submitted.get_json()
    with database.session() as session:
        controls = get_generation_controls(session, case["session_id"])
        assert controls["revision"] == 1 and len(controls["characters"]) == 1
        assert controls["characters"][0]["status"] == "proposed"
        saved = session.get(m.Artifact, result["result_artifact_id"])
        assert (
            saved.metadata_json["speech_markup"]["1"]
            == body["result"]["items"][0]["speech_xml"]
        )
        assert not list(
            session.scalars(select(m.Job).where(m.Job.session_id == case["session_id"]))
        )
    status_url = f"/api/v1/sessions/{case['session_id']}/generation-plan/status"
    status = case["client"].get(status_url).get_json()
    assert status["current_input"]["artifact_id"] == result["result_artifact_id"]
    response = post(f"sessions/{case['session_id']}/generation-plan/prepare", {
        "source_artifact_id": result["result_artifact_id"],
        "expected_revision": status["session_revision"],
        "expected_plan_revision_id": status["selected_revision_id"],
    })
    assert response.status_code == 200, response.get_json()
    revision = response.get_json()["selected_revision_id"]
    with database.session() as session:
        first = session.scalar(select(m.GenerationSegment).where(
            m.GenerationSegment.plan_revision_id == revision).order_by(m.GenerationSegment.ordinal))
        assert first.optimized_text == "Hello."
        assert "c-new" in first.speech_plan_json["speech_xml"]
        assert first.paragraph_break_after is False
        save_generation_controls(session, case["session_id"], expected_revision=1,
            cast={"narrator": {"voice": "Kore"}, "characters": {"c-new": {"voice": "Puck"}}})
    started = case["services"]["generation"].start(case["session_id"],
        speech_plan_revision_id=revision, run_override=overrides(performance_enabled=False))
    requests = []

    def synthesize(text, settings, **kwargs):
        requests.append((text, settings["voice"]))
        return AudioSegment.silent(duration=20)

    handlers = case["services"]["workflow_handlers"]
    with patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize):
        handlers.run_generation({"generation_run_id": started["id"]}, lambda *_: None, threading.Event())
    assert requests == [("Hello.", "Puck"), ("Goodbye.", "Kore")]


@pytest.mark.parametrize("outcome_enabled", [None, False, True])
def test_annotation_input_selection_agrees_across_planners(case, outcome_enabled):
    import json

    services = case["services"]
    sid = case["session_id"]
    sources = {}
    for role in ("prepared_text", "tts_optimized"):
        path = services["paths"].uploads / f"{role}.json"
        path.write_text(json.dumps([{"original_sentence": "Hello."}]))
        sources[role] = services["artifacts"].register(path, kind="json", role=role, session_id=sid)
    services["workspace_settings"].update(sid, "text", 0, {"llm_tts_document_optimization": True})
    if outcome_enabled is not None:
        with services["database"].session() as session:
            session.add(m.OutcomePlan(session_id=sid, value_json={"transformations": {
                "llm_tts_document_optimization": outcome_enabled}}))
    expected = sources["prepared_text" if outcome_enabled is False else "tts_optimized"].id
    status = case["client"].get(f"/api/v1/sessions/{sid}/generation-plan/status").get_json()
    assert status["current_input"]["artifact_id"] == expected
    assert services["workflows"].resolve_stage(sid, "generate_audio").source_artifact_id == expected


def test_timed_annotated_cues_remain_integral_and_store_document_markup(case):
    from pathlib import Path

    database = case["services"]["database"]
    handlers = case["services"]["workflow_handlers"]
    with database.session() as session:
        record = session.get(m.SessionRecord, case["session_id"])
        record.workflow_kind = "voiceover"
        storage_key = record.storage_key
    directory = case["services"]["paths"].sessions / storage_key
    Path(directory).mkdir(parents=True, exist_ok=True)
    path = directory / "timed-dialogue.srt"
    text = "He paused. “Please come in.” The door opened slowly."
    path.write_text(
        f"1\n00:00:01,250 --> 00:00:06,750\n{text}\n\n2\n00:00:08,000 --> 00:00:09,000\nSilence.\n",
        encoding="utf-8",
    )
    xml = '<segment id="1">He paused. <dialogue>“Please come in.”</dialogue> The door opened slowly.</segment>'
    artifact = case["services"]["artifacts"].register(
        path,
        kind="srt",
        role="tts_optimized",
        session_id=case["session_id"],
        metadata={"speech_markup": {"1": xml}},
    )
    _doc, revision = handlers._store_srt_document(
        case["session_id"], artifact, "tts_optimization", language="en"
    )
    with database.session() as session:
        cues = list(
            session.scalars(
                select(m.Segment)
                .where(m.Segment.revision_id == revision)
                .order_by(m.Segment.ordinal)
            )
        )
        assert cues[0].metadata_json["speech_xml"] == xml
    records, _, _ = handlers._subtitle_generation_records(
        artifact, path, {"speech_block_max_chars": 25}, "en", case["session_id"]
    )
    assert len(records) == 2
    timing = records[0]["provenance"]["source_cues"][0]
    assert timing["start_ms"] == 1250 and timing["end_ms"] == 6750
    assert records[0]["alignment_group"] != records[1]["alignment_group"]
    plan, ids = handlers._store_generation_plan(
        case["session_id"], records, settings={}
    )
    with database.session() as session:
        first = session.get(m.GenerationSegment, ids[0])
        assert first.text == text
        assert first.source_segment_ids_json == [1]
        assert "<dialogue>" in first.speech_plan_json["speech_xml"]


def test_configured_annotation_only_skips_word_rewriting_and_retains_srt_markup(case):
    import json
    from pandrator.logic.llm_handler import ChatCompletionResult

    handlers = case["services"]["workflow_handlers"]
    with case["services"]["database"].session() as session:
        storage_key = session.get(m.SessionRecord, case["session_id"]).storage_key
    path = case["services"]["paths"].sessions / storage_key / "configured-dialogue.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n00:00:00,000 --> 00:00:03,000\nCome in.\n", encoding="utf-8")
    artifact = case["services"]["artifacts"].register(
        path, kind="srt", role="transcription", session_id=case["session_id"]
    )
    settings = {
        "llm_tts_annotation_mode": "dialogue",
        "llm_tts_annotation_only": True,
        "llm_provider_configs": [],
        "llm_default_model": "provider/model",
        "tts_optimization_model": "provider/model",
        "request_timeout_seconds": 30,
    }
    xml = '<segment id="1"><dialogue>Come in.</dialogue></segment>'
    with (
        patch.object(handlers, "_with_database_llm_settings", return_value=settings),
        patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=AssertionError("No rewriting during annotation-only"),
        ),
        patch(
            "pandrator.web.speech_structure_analysis.chat_completion_with_metadata",
            return_value=ChatCompletionResult(
                content=json.dumps({"items": [{"unit_id": 1, "speech_xml": xml}]})
            ),
        ),
    ):
        result = handlers.optimize_tts(
            {
                "session_id": case["session_id"],
                "source_artifact_id": artifact.id,
                "settings": settings,
            },
            lambda *args: None,
            threading.Event(),
        )
    output, output_path = case["services"]["artifacts"].resolve(result["artifact_id"])
    assert output.metadata_json["speech_markup"]["1"] == xml
    assert (
        "Come in." in output_path.read_text()
        and "<dialogue>" not in output_path.read_text()
    )
    with case["services"]["database"].session() as session:
        cue = session.scalar(
            select(m.Segment).where(
                m.Segment.revision_id == output.metadata_json["revision_id"]
            )
        )
        assert cue.metadata_json["speech_xml"] == xml
