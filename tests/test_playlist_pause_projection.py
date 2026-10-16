from __future__ import annotations

import uuid
from typing import get_args

from sqlalchemy import event

from pandrator.web import models as m
from pandrator.web.assembly_silences import render_part_silences
from pandrator.web.generation_review import PROJECTABLE_SEGMENT_FIELDS
from pandrator.web.global_settings import GlobalSettingsService
from pandrator_mcp.schemas.generation_fields import GenerationSegmentField
from pandrator_mcp.tools.generation import _segment_projection

pytest_plugins = ("tests.test_performance_plans",)


def _add_run(case, snapshot: dict, *, sequence_number: int) -> str:
    with case["services"]["database"].session() as session:
        run = m.GenerationRun(
            session_id=case["session_id"],
            plan_revision_id=case["revision_id"],
            sequence_number=sequence_number,
            status="completed",
            settings_snapshot_json=snapshot,
        )
        session.add(run)
        session.flush()
        return run.id


def _set_segment(
    case,
    segment_id: str,
    *,
    silence_after_ms: int,
    speech_plan: dict | None = None,
    voice: str | None = None,
    text: str | None = None,
) -> None:
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, segment_id)
        row.silence_after_ms = silence_after_ms
        if speech_plan is not None:
            row.speech_plan_json = speech_plan
        if voice is not None:
            row.voice = voice
        if text is not None:
            row.text = text


def _global_audio_defaults(case, **changes) -> None:
    settings = GlobalSettingsService(case["services"]["database"], case["services"]["paths"])
    current = settings.defaults("audio")
    settings.replace(
        "defaults.audio",
        {**current["value"], **changes},
        "*" if current["revision"] == 0 else str(current["revision"]),
    )


def _read_pause(case, **options) -> dict:
    return case["services"]["generation"].list_segments(
        case["session_id"],
        fields=["assembly_silence_after_ms", "silence_after_ms"],
        take_history="selected",
        **options,
    )


def test_selected_run_uses_frozen_pauses_and_legacy_snapshot_not_current_defaults(case):
    services = case["services"]
    sid = case["segment_ids"][0]
    _set_segment(
        case,
        sid,
        silence_after_ms=700,
        speech_plan={"pause_kind": "sentence"},
    )
    frozen, _ = services["workspace_settings"].resolve(
        case["session_id"], sections=["tts", "audio"]
    )
    frozen["audio"]["sentence_silence_ms"] = 250
    frozen["speech_boundaries"] = {
        sid: {"boundary_after": None, "silence_after_ms": 250, "text_hash": None}
    }
    frozen_run_id = _add_run(case, frozen, sequence_number=900)

    legacy_snapshot = dict(frozen)
    legacy_snapshot.pop("speech_boundaries")
    legacy_run_id = _add_run(case, legacy_snapshot, sequence_number=901)
    _global_audio_defaults(case, sentence_silence_ms=900)

    pinned = _read_pause(
        case,
        generation_run_id=frozen_run_id,
        segment_ids=[sid],
    )["items"][0]
    legacy = _read_pause(
        case,
        generation_run_id=legacy_run_id,
        segment_ids=[sid],
    )["items"][0]
    assert pinned["assembly_silence_after_ms"] == 250
    assert legacy["assembly_silence_after_ms"] == 250
    assert pinned["silence_after_ms"] == legacy["silence_after_ms"] == 700

    with services["database"].session() as session:
        assert session.get(m.GenerationSegment, sid).silence_after_ms == 700
        assert session.get(m.GenerationRun, legacy_run_id).settings_snapshot_json == legacy_snapshot
    sid_zero, sid_dialogue = case["segment_ids"][:2]
    _set_segment(
        case,
        sid_zero,
        silence_after_ms=700,
        speech_plan={"pause_kind": "paragraph", "silence_override_ms": 0},
    )
    text = "A line of dialogue."
    _set_segment(
        case,
        sid_dialogue,
        silence_after_ms=700,
        text=text,
        speech_plan={
            "speech_xml": f'<segment id="{sid_dialogue}"><dialogue>{text}</dialogue></segment>'
        },
    )
    _global_audio_defaults(case, sentence_silence_ms=250, paragraph_silence_ms=700)

    items = services["generation"].list_segments(
        case["session_id"], segment_ids=[sid_zero, sid_dialogue]
    )["items"]
    pauses = {item["id"]: item["assembly_silence_after_ms"] for item in items}
    assert pauses == {sid_zero: 0, sid_dialogue: 250}


def test_legacy_selected_markup_without_frozen_characters_uses_stored_pause(case):
    from pandrator.web.generation_controls import save_generation_controls

    sid = case["segment_ids"][0]
    text = "Hello there."
    markup = (
        f'<segment id="{sid}"><dialogue><speaker ref="legacy-speaker">'
        f"{text}</speaker></dialogue></segment>"
    )
    _set_segment(
        case,
        sid,
        silence_after_ms=700,
        text=text,
        speech_plan={"speech_xml": markup},
    )
    with case["services"]["database"].session() as session:
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=0,
            characters=[{"id": "legacy-speaker", "display_name": "Legacy speaker"}],
        )

    legacy_snapshot = {
        "audio": {"sentence_silence_ms": 250, "paragraph_silence_ms": 700},
        "performance_snapshot": {"annotations": {sid: {"annotation": {"_speech_xml": markup}}}},
    }
    run_id = _add_run(case, legacy_snapshot, sequence_number=902)
    item = _read_pause(
        case,
        generation_run_id=run_id,
        segment_ids=[sid],
    )["items"][0]

    assert item["assembly_silence_after_ms"] == 700
    assert item["silence_after_ms"] == 700
    with case["services"]["database"].session() as session:
        assert session.get(m.GenerationRun, run_id).settings_snapshot_json == legacy_snapshot
        assert session.get(m.GenerationSegment, sid).silence_after_ms == 700


def test_page_terminal_pause_uses_next_voice_floor_and_is_not_suppressed(case):
    sid_first, sid_second, sid_last = case["segment_ids"]
    _set_segment(case, sid_first, silence_after_ms=0, voice="Kore")
    _set_segment(case, sid_second, silence_after_ms=0, voice="Puck")
    _set_segment(
        case,
        sid_last,
        silence_after_ms=700,
        speech_plan={"silence_override_ms": 700},
    )
    _global_audio_defaults(case, voice_change_silence_ms=300)

    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if "from generation_segments" in normalized and "count(" not in normalized:
            statements.append(normalized)

    event.listen(case["services"]["database"].engine, "before_cursor_execute", capture)
    try:
        first_page = _read_pause(case, cursor=0, limit=1)["items"]
        final_page = _read_pause(case, cursor=2, limit=1)["items"]
    finally:
        event.remove(case["services"]["database"].engine, "before_cursor_execute", capture)

    assert first_page[0]["id"] == sid_first
    assert first_page[0]["assembly_silence_after_ms"] == 300
    assert final_page[0]["id"] == sid_last
    assert final_page[0]["assembly_silence_after_ms"] == 700
    assert statements and all(" limit " in sql for sql in statements), statements
    with case["services"]["database"].session() as session:
        session.get(m.SessionRecord, case["session_id"]).workflow_kind = "voiceover"
    _set_segment(
        case,
        sid_last,
        silence_after_ms=700,
        speech_plan={"pause_kind": "paragraph"},
    )

    metadata = {
        "render_parts": [
            {
                "silence_policy": "assembly-v1",
                "start_frame": 0,
                "end_frame": 1600,
                "sample_rate_hz": 16000,
                "silence_before_ms": 0,
            },
            {
                "silence_policy": "assembly-v1",
                "start_frame": 1600,
                "end_frame": 3200,
                "sample_rate_hz": 16000,
                "silence_before_ms": 100,
            },
        ]
    }
    with case["services"]["database"].session() as session:
        artifact = m.Artifact(
            session_id=case["session_id"],
            kind="audio",
            role="generation_take",
            relative_path=f"test/{uuid.uuid4().hex}.wav",
            metadata_json=metadata,
        )
        session.add(artifact)
        session.flush()
        session.add(
            m.AudioTake(
                generation_segment_id=sid_last,
                artifact_id=artifact.id,
                status="completed",
                is_active=True,
                duration_ms=100,
            )
        )

    item = case["services"]["generation"].list_segments(
        case["session_id"],
        segment_ids=[sid_last],
        fields=["assembly_silence_after_ms", "silence_after_ms", "takes"],
    )["items"][0]
    assert item["silence_after_ms"] == 700
    assert item["assembly_silence_after_ms"] == 0
    assert item["takes"][0]["assembly_silences"] == []

    with case["services"]["database"].session() as session:
        session.get(m.SessionRecord, case["session_id"]).workflow_kind = "audiobook"
        session.get(m.GenerationSegment, sid_last).node_kind = "subtitle_cue"
    subtitle_item = case["services"]["generation"].list_segments(
        case["session_id"],
        segment_ids=[sid_last],
        fields=["assembly_silence_after_ms", "silence_after_ms", "takes"],
    )["items"][0]
    assert subtitle_item["assembly_silence_after_ms"] == 0
    assert subtitle_item["takes"][0]["assembly_silences"] == []


def test_marked_take_silences_are_safe_and_legacy_parts_add_no_silence(case):
    sid = case["segment_ids"][0]
    marked_parts = [
        {
            "index": 0,
            "silence_policy": "assembly-v1",
            "start_frame": 0,
            "end_frame": 1600,
            "sample_rate_hz": 16000,
            "duration_ms": 100,
            "silence_before_ms": 0,
        },
        {
            "index": 1,
            "silence_policy": "assembly-v1",
            "start_frame": 1600,
            "end_frame": 3200,
            "sample_rate_hz": 16000,
            "duration_ms": 100,
            "silence_before_ms": 100,
        },
    ]
    legacy_parts = [
        {key: value for key, value in part.items() if key != "silence_policy"}
        for part in marked_parts
    ]
    insertions = render_part_silences(
        {"render_parts": marked_parts}, {"in_segment_voice_change_silence_ms": 100}
    )
    assert [(item.offset_frames, item.sample_rate_hz, item.duration_ms) for item in insertions] == [
        (1600, 16000, 100)
    ]
    assert (
        render_part_silences(
            {"render_parts": legacy_parts}, {"in_segment_voice_change_silence_ms": 100}
        )
        == ()
    )

    frozen, _ = case["services"]["workspace_settings"].resolve(
        case["session_id"], sections=["tts", "audio"]
    )
    frozen["audio"]["in_segment_voice_change_silence_ms"] = 100
    run_id = _add_run(case, frozen, sequence_number=900)
    _global_audio_defaults(case, in_segment_voice_change_silence_ms=250)

    for index, parts in enumerate((marked_parts, legacy_parts)):
        with case["services"]["database"].session() as session:
            artifact = m.Artifact(
                session_id=case["session_id"],
                kind="audio",
                role="generation_take",
                relative_path=f"test/{uuid.uuid4().hex}.wav",
                metadata_json={"render_parts": parts},
            )
            session.add(artifact)
            session.flush()
            take = m.AudioTake(
                generation_segment_id=sid,
                artifact_id=artifact.id,
                status="completed",
                is_active=index == 0,
                duration_ms=200,
            )
            session.add(take)

    payload = case["services"]["generation"].list_segments(
        case["session_id"],
        generation_run_id=run_id,
        segment_ids=[sid],
        fields=["takes"],
    )
    takes = payload["items"][0]["takes"]
    marked_take = next(take for take in takes if take["assembly_silences"])
    legacy_take = next(take for take in takes if not take["assembly_silences"])
    assert marked_take["assembly_silences"] == [{"at_ms": 100, "duration_ms": 100}]
    assert legacy_take["assembly_silences"] == []

    safe = _segment_projection(payload["items"][0])
    safe_marked_take = next(take for take in safe["takes"] if take["assembly_silences"])
    assert safe_marked_take["assembly_silences"] == marked_take["assembly_silences"]
    assert "render_parts" not in safe_marked_take
    assert "relative_path" not in safe_marked_take


def test_mcp_projection_field_enum_matches():
    assert "assembly_silence_after_ms" in PROJECTABLE_SEGMENT_FIELDS
    assert PROJECTABLE_SEGMENT_FIELDS == set(get_args(GenerationSegmentField))
