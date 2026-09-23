from __future__ import annotations

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.generation_controls import (
    get_generation_controls,
    save_generation_controls,
)
from pandrator.web.speech_plan_preview import preview_speech_segment
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
