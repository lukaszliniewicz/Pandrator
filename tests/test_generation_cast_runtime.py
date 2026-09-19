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
    from pathlib import Path

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
        saved_path = case["services"]["paths"].managed_path(saved.relative_path)
        assert (
            saved.metadata_json["speech_markup"]["1"]
            == body["result"]["items"][0]["speech_xml"]
        )
        assert not list(
            session.scalars(select(m.Job).where(m.Job.session_id == case["session_id"]))
        )
    rows = json.loads(Path(saved_path).read_text())
    revision, ids = case["services"]["workflow_handlers"]._store_generation_plan(
        case["session_id"], rows, settings={}
    )
    with database.session() as session:
        first = session.get(m.GenerationSegment, ids[0])
        assert first.optimized_text == "Hello."
        assert "c-new" in first.speech_plan_json["speech_xml"]
        assert first.paragraph_break_after is False


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
