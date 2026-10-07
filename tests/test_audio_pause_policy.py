"""Exact pause overrides, frozen casting and bounded fake-audio acceptance."""

from copy import deepcopy

import pytest
from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.generation_cast_runtime import freeze_cast_snapshot, segment_render_parts
from pandrator.web.generation_rendering import build_render_parts, execute_render_parts
from pandrator.web.global_settings import GlobalSettingsService
from pandrator.web.settings_policy import BUILTIN_DEFAULTS, validate_audio_pause_settings
from pandrator.web.speech_boundaries import assembly_pause, freeze_boundaries
from pandrator.web.workflow_handlers import _default_silence_after_ms
from tests.test_audiobook_resegmentation import rows, topology
from tests.test_generation_rendering import CONTROLS
from tests.test_performance_plans import case as case

PAUSE_KEYS = (
    "sentence_silence_ms",
    "paragraph_silence_ms",
    "clause_silence_ms",
    "voice_change_silence_ms",
)


@pytest.mark.parametrize("key", PAUSE_KEYS)
@pytest.mark.parametrize("value", [True, False, "100", 1.5, -1, 10001, None])
def test_pause_validation_rejects_non_integer_or_out_of_range(key, value):
    with pytest.raises(ValueError, match="integer from 0 to 10000"):
        validate_audio_pause_settings({key: value, "unrelated": "kept"})


@pytest.mark.parametrize("value", [0, 10000])
def test_pause_validation_accepts_bounds_and_preserves_unrelated(value):
    settings = {key: value for key in PAUSE_KEYS} | {"unrelated": "kept"}
    validate_audio_pause_settings(settings)
    assert settings["unrelated"] == "kept"


def test_pause_validation_runs_on_session_and_global_saves(case):
    services = case["services"]
    with pytest.raises(ValueError, match="integer"):
        services["workspace_settings"].update(
            case["session_id"], "audio", 0, {"clause_silence_ms": True}
        )
    globals_service = GlobalSettingsService(services["database"], services["paths"])
    current = globals_service.defaults("audio")
    with pytest.raises(ValueError, match="integer"):
        globals_service.replace(
            "defaults.audio",
            {"voice_change_silence_ms": "40"},
            "*" if current["revision"] == 0 else str(current["revision"]),
        )
    assert globals_service.defaults("audio") == current


def test_default_clause_sentence_paragraph_explicit_zero_and_legacy_fallback():
    settings = {"sentence_silence_ms": 300, "paragraph_silence_ms": 900, "clause_silence_ms": 40}
    assert _default_silence_after_ms({"sentence_continues_after": True}, settings) == 40
    assert _default_silence_after_ms({}, settings) == 300
    assert _default_silence_after_ms({"paragraph": "yes"}, settings) == 900
    assert _default_silence_after_ms({"paragraph": "yes", "silence_after_ms": 0}, settings) == 0
    assert (
        _default_silence_after_ms({"sentence_continues_after": True}, {"sentence_silence_ms": 300})
        == 100
    )
    assert _default_silence_after_ms({}, settings, is_subtitle=True) == 0
    assert BUILTIN_DEFAULTS["audio"]["clause_silence_ms"] == 83
    assert BUILTIN_DEFAULTS["audio"]["voice_change_silence_ms"] == 0


@pytest.mark.parametrize("boundary", ["continuation", "paragraph", "dialogue_turn"])
@pytest.mark.parametrize("pause", [0, 123])
def test_manual_pause_survives_xml_and_performance_boundary_and_freezes(case, boundary, pause):
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, case["segment_ids"][0])
        case["services"]["generation"]._apply_segment_changes(
            session, row, {"silence_after_ms": pause}
        )
        xml = f'<segment id="{row.id}" boundary_after="{boundary}">{row.text}</segment>'
        row.speech_plan_json = {**row.speech_plan_json, "speech_xml": xml}
        snapshot = {
            "audio": {"sentence_silence_ms": 250, "paragraph_silence_ms": 700},
            "performance_snapshot": {"annotations": {row.id: {"annotation": {"_speech_xml": xml}}}},
        }
        freeze_boundaries(session, case["revision_id"], snapshot)
        assert assembly_pause(row, snapshot) == pause
        snapshot["audio"]["paragraph_silence_ms"] = 900
        row.speech_plan_json = {}
        row.silence_after_ms = 999
        assert assembly_pause(row, snapshot) == pause


def test_source_explicit_pause_and_clause_classification_are_stored(case):
    plan = case["services"]["generation"].create_plan(
        case["session_id"],
        source_revision_id=None,
        settings={"clause_silence_ms": 41},
        segments=[
            {"text": "A clause,", "sentence_continues_after": True},
            {"text": "End.", "silence_after_ms": 0, "paragraph": "yes"},
        ],
    )
    segments = rows(case, plan["active_revision_id"])
    assert segments[0].speech_plan_json["pause_kind"] == "clause"
    assert segments[1].speech_plan_json["silence_override_ms"] == 0
    with case["services"]["database"].session() as session:
        snapshot = {"audio": {"clause_silence_ms": 42}}
        freeze_boundaries(session, plan["active_revision_id"], snapshot)
        assert assembly_pause(segments[0], snapshot) == 42
        assert assembly_pause(segments[1], snapshot) == 0


def test_manual_pause_survives_text_reset_split_and_merge(case):
    generation = case["services"]["generation"]
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, case["segment_ids"][0])
        generation._apply_segment_changes(
            session, row, {"text": "A long sentence.", "silence_after_ms": 0}
        )
        assert row.speech_plan_json["silence_override_ms"] == 0
    response = topology(
        case, case["revision_id"], action="split", segment_id=case["segment_ids"][0], cursor=6
    )
    assert response.status_code == 201, response.get_json()
    revision = response.get_json()["plan_revision_id"]
    split_rows = rows(case, revision)
    assert "silence_override_ms" not in split_rows[0].speech_plan_json
    assert split_rows[1].speech_plan_json["silence_override_ms"] == 0
    response = topology(
        case,
        revision,
        action="merge",
        left_segment_id=split_rows[0].id,
        right_segment_id=split_rows[1].id,
    )
    assert response.status_code == 201, response.get_json()
    merged = rows(case, response.get_json()["plan_revision_id"])[0]
    assert merged.speech_plan_json["silence_override_ms"] == 0


def test_voice_change_floor_uses_actual_voice_and_explicit_pause_wins(case):
    with case["services"]["database"].session() as session:
        segments = list(
            session.scalars(
                select(m.GenerationSegment)
                .where(m.GenerationSegment.plan_revision_id == case["revision_id"])
                .order_by(m.GenerationSegment.ordinal)
            )
        )
        for row, voice in zip(segments, ["one", "two", "two"], strict=True):
            row.voice = voice
            row.speaker = "different-label" if voice == "two" else "label"
            row.silence_after_ms = 100
            row.speech_plan_json = {}
        snapshot = {
            "audio": {"voice_change_silence_ms": 200},
            "tts": {"performance_enabled": False},
        }
        freeze_boundaries(session, case["revision_id"], snapshot)
        assert [assembly_pause(row, snapshot) for row in segments] == [200, 100, 100]
        old = deepcopy(snapshot)
        segments[0].speech_plan_json = {"silence_override_ms": 0}
        segments[1].voice = "three"
        snapshot["audio"]["voice_change_silence_ms"] = 500
        freeze_boundaries(session, case["revision_id"], snapshot)
        assert assembly_pause(segments[0], snapshot) == 0
        assert assembly_pause(segments[0], old) == 200
        segments[0].speech_plan_json = {"silence_override_ms": 700}
        freeze_boundaries(session, case["revision_id"], snapshot)
        assert assembly_pause(segments[0], snapshot) == 700
        segments[0].speech_plan_json = {
            "speech_xml": f'<segment id="{segments[0].id}" boundary_after="paragraph">{segments[0].text}</segment>'
        }
        snapshot["audio"]["paragraph_silence_ms"] = 700
        freeze_boundaries(session, case["revision_id"], snapshot)
        assert assembly_pause(segments[0], snapshot) == 700


@pytest.mark.parametrize(
    "same_voice,subtitle,gap,expected",
    [(False, False, 50, 80), (True, False, 50, 10), (False, True, 50, 30), (False, False, 0, 30)],
)
def test_render_parts_insert_one_gap_only_at_voice_change(same_voice, subtitle, gap, expected):
    controls = deepcopy(CONTROLS)
    if same_voice:
        controls["cast"]["characters"]["c-two"] = {"voice": "one"}
    markup = (
        '<segment id="s"><speaker ref="c-one">A</speaker><speaker ref="c-two">B</speaker></segment>'
    )
    parts = build_render_parts(
        "AB",
        {
            "casting_enabled": True,
            "performance_enabled": False,
            "voice_change_silence_ms": gap,
            "_subtitle_timed": subtitle,
        },
        speech_xml=markup,
        segment_id="s",
        controls=controls,
    )
    combined, manifest = execute_render_parts(
        parts,
        synthesize=lambda text, settings: AudioSegment.silent(10 if text in {"A", "AB"} else 20),
        cancelled=lambda: False,
    )
    assert len(combined) == expected
    assert [row["silence_before_ms"] for row in manifest] == (
        [0] if same_voice else [0, 0 if subtitle else gap]
    )
    assert sum(row["duration_ms"] for row in manifest) == (10 if same_voice else 30)


def test_audible_padding_changes_only_multi_voice_identity_and_snapshot_gap(case, monkeypatch):
    from pandrator.web import generation_cast_runtime as casting

    monkeypatch.setattr(casting, "get_generation_controls", lambda session, sid: deepcopy(CONTROLS))
    monkeypatch.setattr(
        casting, "resolve_binding", lambda session, binding, settings, cache: dict(binding)
    )
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, case["segment_ids"][0])
        first, rest = row.text[:3], row.text[3:]
        row.speech_plan_json = {
            "speech_xml": f'<segment id="{row.id}"><speaker ref="c-one">{first}</speaker><speaker ref="c-two">{rest}</speaker></segment>'
        }
        snapshot = {
            "audio": {"voice_change_silence_ms": 0},
            "tts": {
                "service": "openai",
                "model": "tts-1",
                "voice": "alloy",
                "casting_enabled": True,
                "performance_enabled": False,
            },
        }
        freeze_cast_snapshot(session, case["revision_id"], snapshot, snapshot["tts"])
        before = AudioIdentityContext(session, snapshot).for_segment(row)
        padded = deepcopy(snapshot)
        padded["audio"]["voice_change_silence_ms"] = 80
        after = AudioIdentityContext(session, padded).for_segment(row)
        assert before["settings_hash"] == after["settings_hash"]
        assert before["voice_reference_hash"] == after["voice_reference_hash"]
        assert before["performance_request_hash"] != after["performance_request_hash"]
        only_assembly = deepcopy(snapshot)
        only_assembly["audio"].update(
            sentence_silence_ms=999, clause_silence_ms=99, paragraph_silence_ms=1234
        )
        assert AudioIdentityContext(session, only_assembly).for_segment(row) == before
        parts = segment_render_parts(
            {**padded["tts"], "voice_change_silence_ms": 900}, padded, row.id, row.text
        )
        assert parts[1]["silence_before_ms"] == 80
        old_parts = segment_render_parts(
            {**padded["tts"], "voice_change_silence_ms": 900}, snapshot, row.id, row.text
        )
        assert old_parts[1]["silence_before_ms"] == 0
        padded["generation_control_snapshot"]["segments"][row.id]["subtitle_timed"] = True
        assert AudioIdentityContext(session, padded).for_segment(row) == before
        row.speech_plan_json = {}
        freeze_cast_snapshot(session, case["revision_id"], snapshot, snapshot["tts"])
        padded = deepcopy(snapshot)
        padded["audio"]["voice_change_silence_ms"] = 80
        assert AudioIdentityContext(session, padded).for_segment(row) == AudioIdentityContext(
            session, snapshot
        ).for_segment(row)


def test_api_omitted_pause_follows_defaults_but_explicit_zero_survives_split(case):
    response = case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-plan",
        json={
            "settings": {"sentence_silence_ms": 444},
            "segments": [
                {"text": "Automatic sentence."},
                {"text": "Explicit zero pause.", "silence_after_ms": 0},
            ],
        },
        headers=case["headers"],
    )
    assert response.status_code == 201, response.get_json()
    revision = response.get_json()["active_revision_id"]
    segments = rows(case, revision)
    assert segments[0].silence_after_ms == 444
    assert segments[0].speech_plan_json["pause_kind"] == "sentence"
    assert "silence_override_ms" not in segments[0].speech_plan_json
    assert segments[1].speech_plan_json["silence_override_ms"] == 0
    response = topology(case, revision, action="split", segment_id=segments[1].id, cursor=8)
    assert response.status_code == 201, response.get_json()
    descendants = rows(case, response.get_json()["plan_revision_id"])[1:]
    assert "silence_override_ms" not in descendants[0].speech_plan_json
    assert descendants[1].speech_plan_json["silence_override_ms"] == 0


def test_resegmentation_retains_only_terminal_explicit_pause(case):
    with case["services"]["database"].session() as session:
        terminal = session.get(m.GenerationSegment, case["segment_ids"][1])
        case["services"]["generation"]._apply_segment_changes(
            session,
            terminal,
            {"silence_after_ms": 0},
        )
    response = topology(
        case,
        case["revision_id"],
        action="resegment",
        segment_ids=case["segment_ids"][:2],
        boundaries=[11],
    )
    assert response.status_code == 201, response.get_json()
    descendants = rows(case, response.get_json()["plan_revision_id"])[:2]
    assert "silence_override_ms" not in descendants[0].speech_plan_json
    assert descendants[1].speech_plan_json["silence_override_ms"] == 0
    assert rows(case, case["revision_id"])[1].speech_plan_json["silence_override_ms"] == 0
