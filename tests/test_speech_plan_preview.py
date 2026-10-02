from __future__ import annotations

import wave
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web.generation_audio_identity import IDENTITY_KEY, AudioIdentityContext
from pandrator.web.generation_controls import (
    get_generation_controls,
    save_generation_controls,
)
from pandrator.web.speech_plan_preview import preview_speech_segment
from pandrator.web.speech_plan_workspace import freeze_generation_performance_snapshot
from tests.test_performance_plans import adopt, create, edit
from tests.test_performance_plans import case as case


def _set_tts(case, **values):
    settings = case["services"]["workspace_settings"]
    current = settings.get(case["session_id"], "tts")
    settings.update(
        case["session_id"],
        "tts",
        current["revision"],
        {**current["effective"], **values},
    )


def _cast(case, *, narrator="Kore", character="Puck"):
    with case["services"]["database"].session() as session:
        current = get_generation_controls(session, case["session_id"])
        return save_generation_controls(
            session,
            case["session_id"],
            expected_revision=current["revision"],
            characters=[
                {
                    "id": "c-scrooge",
                    "display_name": "Scrooge",
                    "voice_category": "male",
                }
            ],
            cast={
                "narrator": {"voice": narrator},
                "characters": {"c-scrooge": {"voice": character}},
            },
        )


def _markup(case, text):
    sid = case["segment_ids"][0]
    xml = (
        f'<segment id="{sid}">'
        f'<speaker ref="c-scrooge">Scrooge</speaker> said: 👋 '
        f'<narrator>then Scrooge</narrator> '
        f'<speaker ref="c-scrooge">Scrooge</speaker>.</segment>'
    )
    assert text == "Scrooge said: 👋 then Scrooge Scrooge."
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, sid)
        segment.text = text
        segment.speech_plan_json = {"speech_xml": xml}
    return sid


def _seed_segment_read_takes(case, snapshot, *, generation_run_id=None):
    services = case["services"]
    database = services["database"]
    with database.session() as session:
        rows = list(session.scalars(select(m.GenerationSegment).where(
            m.GenerationSegment.plan_revision_id == case["revision_id"]
        ).order_by(m.GenerationSegment.ordinal)))
        context = AudioIdentityContext(session, snapshot)
        identities = {row.id: context.for_segment(row) for row in rows}
    for segment_id in case["segment_ids"]:
        path = services["paths"].uploads / f"segment-read-{segment_id}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        artifact = services["artifacts"].register(
            path, kind="audio", role="generation_take",
            session_id=case["session_id"], metadata={IDENTITY_KEY: identities[segment_id]},
        )
        with database.session() as session:
            session.get(m.GenerationSegment, segment_id).status = "completed"
            session.add(m.AudioTake(
                generation_segment_id=segment_id, generation_run_id=generation_run_id,
                artifact_id=artifact.id, status="completed", is_active=True, duration_ms=100,
            ))


def _adopt_segment_read_markup(case):
    plan = create(case, annotation_format="xml")
    segment_id = case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        markup = session.get(m.GenerationSegment, segment_id).speech_plan_json["speech_xml"]
    adopted_xml = markup.replace(
        '<speaker ref="c-scrooge">', '<speaker ref="c-scrooge"><em>dry</em>', 1
    )
    response = case["post"](
        "/" + plan["id"],
        {
            "expected_version": plan["version"],
            "items": [{"segment_id": segment_id, "speech_xml": adopted_xml}],
        },
        method="patch",
    )
    assert response.status_code == 200, response.get_json()
    adopt(case, response.get_json())
    return adopted_xml


def _segment_read_scope_parity(
    case, *, expected_markup, generation_run_id=None, expected_snapshot=None
):
    services = case["services"]
    settings = services["workspace_settings"]
    real_resolve = settings.resolve
    statements = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(services["database"].engine, "before_cursor_execute", record_statement)
    try:
        with patch.object(settings, "resolve", side_effect=lambda sid, **kwargs: real_resolve(sid)):
            full = services["generation"].list_segments(
                case["session_id"], generation_run_id=generation_run_id
            )
        full_selects = len(statements)
        statements.clear()
        with patch.object(settings, "resolve", wraps=real_resolve) as scoped_resolve, patch(
            "pandrator.web.generation_audio_identity.AudioIdentityContext",
            wraps=AudioIdentityContext,
        ) as identity_context:
            scoped = services["generation"].list_segments(
                case["session_id"], generation_run_id=generation_run_id
            )
            scoped_resolve.assert_called_once_with(case["session_id"], sections=["tts", "audio"])
            if expected_snapshot is not None:
                identity_context.assert_called_once()
                assert identity_context.call_args.args[1] == expected_snapshot
        scoped_selects = len(statements)
    finally:
        event.remove(services["database"].engine, "before_cursor_execute", record_statement)
    assert full == scoped
    assert [item["audio_reuse_reason"] for item in scoped["items"]] == ["reusable"] * 3
    assert all(item["has_reusable_take"] for item in scoped["items"])
    assert scoped["items"][0]["speech_annotation_xml"] == expected_markup
    return full_selects, scoped_selects


@pytest.mark.parametrize(
    ("casting_enabled", "performance_enabled", "context_mode", "query_budget"),
    [(False, False, "off", 23), (False, True, "both", 31), (True, False, "both", 32)],
    ids=["strict-single", "directed-context", "named-cast-context"],
)
def test_segment_reader_scoped_settings_preserve_rich_payload(
    case, record_property, casting_enabled, performance_enabled, context_mode, query_budget
):
    _markup(case, "Scrooge said: 👋 then Scrooge Scrooge.")
    _cast(case)
    adopted_xml = _adopt_segment_read_markup(case)
    _set_tts(
        case, service="gemini", model="gemini-2.5-flash-tts", voice="Kore",
        voice_mode_version=1, casting_enabled=casting_enabled,
        performance_enabled=performance_enabled, tts_context_mode=context_mode,
    )
    snapshot, _ = case["services"]["workspace_settings"].resolve(case["session_id"])
    _seed_segment_read_takes(case, snapshot)
    full_selects, scoped_selects = _segment_read_scope_parity(case, expected_markup=adopted_xml)
    record_property("full_selects", full_selects)
    record_property("scoped_selects", scoped_selects)
    assert scoped_selects <= query_budget


def test_segment_reader_scoped_current_settings_preserve_entire_frozen_run(case, record_property):
    _markup(case, "Scrooge said: 👋 then Scrooge Scrooge.")
    _cast(case)
    adopted_xml = _adopt_segment_read_markup(case)
    _set_tts(
        case, service="gemini", model="gemini-2.5-flash-tts", voice="Kore",
        voice_mode_version=1, casting_enabled=True,
        performance_enabled=True, tts_context_mode="both",
    )
    services = case["services"]
    snapshot, _ = services["workspace_settings"].resolve(case["session_id"])
    with services["database"].session() as session:
        assert freeze_generation_performance_snapshot(session, case["revision_id"], snapshot)
        assert all(snapshot[key] for key in (
            "performance_snapshot", "generation_control_snapshot", "semantic_context_snapshot"
        ))
        snapshot["selected_segment_override"] = {
            "tts": {"voice": "Kore"}, "rvc": {"enabled": True, "model": "test-rvc"},
        }
        run = m.GenerationRun(
            session_id=case["session_id"], plan_revision_id=case["revision_id"],
            sequence_number=1, settings_snapshot_json=snapshot, status="completed",
        )
        session.add(run)
        session.flush()
        run_id = run.id
    _seed_segment_read_takes(case, snapshot, generation_run_id=run_id)
    _set_tts(case, voice="Charon")
    _cast(case, narrator="Charon", character="Charon")
    full_selects, scoped_selects = _segment_read_scope_parity(
        case, expected_markup=adopted_xml, generation_run_id=run_id, expected_snapshot=snapshot
    )
    record_property("full_selects", full_selects)
    record_property("scoped_selects", scoped_selects)
    with services["database"].session() as session:
        frozen = session.get(m.GenerationRun, run_id).settings_snapshot_json
        assert frozen == snapshot
        assert frozen["selected_segment_override"] == {
            "tts": {"voice": "Kore"}, "rvc": {"enabled": True, "model": "test-rvc"},
        }


def test_current_preview_resolves_named_cast_unicode_offsets_and_no_sidecar(case):
    text = "Scrooge said: 👋 then Scrooge Scrooge."
    sid = _markup(case, text)
    _cast(case)
    _set_tts(
        case,
        service="gemini",
        model="gemini-2.5-flash-tts",
        casting_enabled=True,
        performance_enabled=False,
    )

    with case["services"]["database"].session() as session:
        before = session.scalar(select(m.PerformancePlan.id))
    result = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
    )
    with case["services"]["database"].session() as session:
        after = session.scalar(select(m.PerformancePlan.id))

    assert result["source"] == "current_plan"
    assert result["text"] == text
    assert result["casting_enabled"] is True
    assert result["performance_enabled"] is False
    assert before == after is None
    assert [(item["start"], item["end"], item["speaker_name"], item["role"])
            for item in result["spans"]] == [
        (0, 7, "Scrooge", "speaker"),
        (7, 16, None, "narrator"),
        (16, 28, None, "narrator"),
        (28, 29, None, "narrator"),
        (29, 36, "Scrooge", "speaker"),
        (36, 37, None, "narrator"),
    ]
    assert [item["voice"] for item in result["spans"]] == [
        "Puck",
        "Kore",
        "Kore",
        "Kore",
        "Puck",
        "Puck",
    ]
    assert sum(item["end"] - item["start"] for item in result["spans"]) == len(text)
    assert [(part["start"], part["end"], part["voice"]) for part in result["parts"]] == [
        (0, 7, "Puck"),
        (7, 29, "Kore"),
        (29, 37, "Puck"),
    ]
    assert all("text" not in part and "input" not in part for part in result["parts"])


def test_block_voice_wins_when_casting_is_disabled_and_include_request_is_bounded(case):
    sid = case["segment_ids"][0]
    text = "A block voice overrides the cast."
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, sid)
        segment.text = text
        segment.voice = "BlockVoice"
        segment.speech_plan_json = {"speech_xml": f'<segment id="{sid}">{text}</segment>'}
    _cast(case, narrator="Narrator", character="Character")
    _set_tts(
        case,
        service="gemini",
        model="gemini-2.5-flash-tts",
        casting_enabled=False,
        voice_mode_version=0,
        performance_enabled=False,
    )

    compact = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
    )
    expanded = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
        include_request=True,
    )
    assert compact["parts"][0]["voice"] == "BlockVoice"
    assert compact["parts"][0]["voice_source"] == "segment"
    assert "text" not in compact["parts"][0]
    assert expanded["parts"][0]["text"] == text
    assert expanded["parts"][0]["input"] == text
    assert set(expanded["parts"][0]) == {
        "start", "end", "voice", "voice_source", "fallback", "report",
        "instructions", "text", "input", "request_options",
    }


def test_strict_single_preview_ignores_inactive_block_binding_and_keeps_language(
    case, monkeypatch
):
    from pandrator.web import speech_plan_preview

    sid = case["segment_ids"][0]
    text = "A block voice is inactive in strict single mode."
    with case["services"]["database"].session() as session:
        voice = m.Voice(name="Unpublished voice", metadata_json={})
        session.add(voice)
        session.flush()
        segment = session.get(m.GenerationSegment, sid)
        segment.text = text
        segment.voice = "BlockVoice"
        segment.voice_id = voice.id
        segment.language = "pl"
        segment.speech_plan_json = {
            "speech_xml": f'<segment id="{sid}">{text}</segment>'
        }
    _set_tts(
        case,
        service="gemini",
        model="gemini-2.5-flash-tts",
        voice="BaseVoice",
        language="en",
        casting_enabled=False,
        voice_mode_version=1,
        performance_enabled=False,
    )
    captured = []
    original_compile = speech_plan_preview._compile_parts

    def capture_settings(parts, *, include_request):
        captured.extend(part["settings"] for part in parts)
        return original_compile(parts, include_request=include_request)

    monkeypatch.setattr(speech_plan_preview, "_compile_parts", capture_settings)
    result = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
    )

    assert result["parts"][0]["voice"] == "BaseVoice"
    assert result["parts"][0]["voice_source"] == "base"
    assert captured[0]["language"] == "pl"
    assert captured[0]["voice"] == "BaseVoice"


def test_preview_alternate_voice_overrides_follow_voice_mode():
    from pandrator.web.speech_plan_preview import _runtime_settings, _segment_overrides

    base = {
        "tts": {
            "voice_mode_version": 1,
            "casting_enabled": False,
            "voice": "BaseVoice",
            "speaker": "BaseVoice",
            "language": "en",
        },
        "selected_segment_override": {
            "tts": {
                "voice": "AlternateVoice",
                "speaker": "AlternateVoice",
                "language": "pl",
                "generation_prompt": "Preserve this instruction.",
            }
        },
    }
    strict = _runtime_settings(base)
    _segment_overrides(
        strict,
        SimpleNamespace(language="de", voice="BlockVoice"),
    )
    assert strict["voice"] == strict["speaker"] == "BaseVoice"
    assert strict["language"] == "de"
    assert strict["generation_prompt"] == "Preserve this instruction."

    legacy = _runtime_settings(
        {
            "tts": {"voice_mode_version": 0, "casting_enabled": False, "voice": "BaseVoice"},
            "selected_segment_override": {"tts": {"voice": "AlternateVoice"}},
        }
    )
    _segment_overrides(legacy, SimpleNamespace(language="", voice="BlockVoice"))
    assert legacy["voice"] == legacy["speaker"] == "BlockVoice"

    multi = _runtime_settings(
        {
            "tts": {"voice_mode_version": 1, "casting_enabled": True, "voice": "BaseVoice"},
            "selected_segment_override": {"tts": {"voice": "AlternateVoice"}},
        }
    )
    _segment_overrides(multi, SimpleNamespace(language="", voice="BlockVoice"))
    assert multi["voice"] == multi["speaker"] == "BlockVoice"


@pytest.mark.parametrize("casting_enabled", [True, False])
def test_adopted_markup_remains_visible_with_runtime_flags_disabled(case, casting_enabled):
    sid = case["segment_ids"][0]
    text = "Scrooge."
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, sid)
        segment.text = text
        segment.speech_plan_json = {"speech_xml": f'<segment id="{sid}">{text}</segment>'}
    _cast(case, narrator="Kore", character="Puck")
    plan = create(case, annotation_format="xml")
    adopted_xml = (
        f'<segment id="{sid}"><speaker ref="c-scrooge"><em>dry</em>{text}</speaker></segment>'
    )
    response = case["post"](
        "/" + plan["id"],
        {
            "expected_version": plan["version"],
            "items": [{"segment_id": sid, "speech_xml": adopted_xml}],
        },
        method="patch",
    )
    assert response.status_code == 200, response.get_json()
    adopt(case, response.get_json())
    _set_tts(
        case,
        service="gemini",
        model="gemini-2.5-flash-tts",
        voice="Kore",
        casting_enabled=casting_enabled,
        performance_enabled=False,
    )

    result = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
    )
    assert result["casting_enabled"] is casting_enabled
    assert result["performance_enabled"] is False
    assert result["spans"][0]["speaker_name"] == "Scrooge"
    assert result["parts"][0]["voice"] == ("Puck" if casting_enabled else "Kore")
    assert result["spans"][0]["delivery"]["emotion"] == "dry"
    assert result["parts"][0]["instructions"] == ""
    drawer = case["services"]["generation"].list_segments(case["session_id"])
    assert drawer["items"][0]["speech_annotation_xml"] == adopted_xml
    assert drawer["items"][0]["speech_plan"]["speech_xml"] != adopted_xml
    compact = case["services"]["generation"].list_segments(case["session_id"], view="compact")
    assert "speech_annotation_xml" not in compact["items"][0]


def test_frozen_preview_uses_old_cast_after_current_cast_edit(case):
    text = "Scrooge said: 👋 then Scrooge Scrooge."
    sid = _markup(case, text)
    _cast(case, narrator="Kore", character="Puck")
    _set_tts(
        case,
        service="gemini",
        model="gemini-2.5-flash-tts",
        casting_enabled=True,
        performance_enabled=False,
    )
    started = case["services"]["generation"].start(
        case["session_id"],
        speech_plan_revision_id=case["revision_id"],
        run_override={
            "tts": {
                "service": "gemini",
                "model": "gemini-2.5-flash-tts",
                "casting_enabled": True,
                "performance_enabled": False,
            }
        },
    )
    with case["services"]["database"].session() as session:
        run = session.get(m.GenerationRun, started["id"])
        assert run.settings_snapshot_json["generation_control_snapshot"]
    _cast(case, narrator="Charon", character="Charon")

    frozen = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
        generation_run_id=started["id"],
    )
    current = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
    )
    assert frozen["source"] == "run_snapshot"
    assert frozen["generation_run_id"] == started["id"]
    assert frozen["parts"][0]["voice"] == "Puck"
    assert current["parts"][0]["voice"] == "Charon"
    frozen_drawer = case["services"]["generation"].list_segments(
        case["session_id"], generation_run_id=started["id"]
    )
    assert "c-scrooge" in frozen_drawer["items"][0]["speech_annotation_xml"]


def test_foreign_revision_and_segment_are_rejected(case):
    other = case["services"]["sessions"].create("Other preview session")
    foreign = case["services"]["generation"].create_plan(
        other.id,
        source_revision_id=None,
        settings={},
        segments=[{"text": "Foreign segment."}],
    )
    with pytest.raises(KeyError):
        preview_speech_segment(
            case["services"],
            case["session_id"],
            revision_id=foreign["active_revision_id"],
            segment_id=case["segment_ids"][0],
        )

    with case["services"]["database"].session() as session:
        foreign_segment = session.scalar(
            select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id == foreign["active_revision_id"]
            )
        )
    with pytest.raises(KeyError):
        preview_speech_segment(
            case["services"],
            case["session_id"],
            revision_id=case["revision_id"],
            segment_id=foreign_segment.id,
        )


def test_qwen_unsupported_direction_is_reported(case):
    sid = case["segment_ids"][0]
    text = "A restrained direction remains visible."
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, sid)
        segment.text = text
    plan = create(case)
    plan = edit(case, plan, index=0, instruction="Read this with a restrained pause.")
    adopt(case, plan)
    _set_tts(
        case,
        service="audio_cpp",
        model="qwen3_tts_1_7b_base_q8_0",
        casting_enabled=False,
        performance_enabled=True,
    )

    result = preview_speech_segment(
        case["services"],
        case["session_id"],
        revision_id=case["revision_id"],
        segment_id=sid,
    )
    statuses = [item["status"] for part in result["parts"] for item in part["report"]]
    assert "unsupported" in statuses
