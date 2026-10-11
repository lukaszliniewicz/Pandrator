"""Sound-aware speech selection transaction and revision checks."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web import speech_selection_sounds as adapter
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.sound_project_commands import write_version
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from pandrator.web.speech_plan_workspace import plan_signature
from pandrator.web.speech_selection_sounds import (
    apply_speech_selection,
    get_speech_selection_context,
    preview_speech_selection,
)
from tests.test_performance_plans import case as case  # noqa: PLC0414


def _setup(case):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    text = "Hello world"
    with services["database"].session() as session:
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.speech_plan_json = {"speech_xml": f'<segment id="{segment_id}">{text}</segment>'}
        artifact = m.Artifact(
            kind="audio", role="sound.original", relative_path="bell.wav", content_hash="bell"
        )
        session.add(artifact)
        session.flush()
        variant = m.SoundVariant(
            artifact_id=artifact.id, state="ready", duration_seconds=1.0,
            provenance_json={"display_name": "Bell"},
        )
        session.add(variant)
        session.flush()
        revision = segment.revision
        variant_id = variant.id
    request = {
        "revision_id": case["revision_id"], "segment_id": segment_id,
        "expected_segment_revision": revision, "start": 0, "end": len(text),
    }
    return request, variant_id


def _sound_xml(segment_id, variant_id, cue_id):
    return (
        f'<segment id="{segment_id}">Hello'
        f'<sound cue="{cue_id}" variant="{variant_id}" placement="after"/>'
        ' world</segment>'
    )


def test_sound_only_preview_and_apply_do_not_create_speech_plan(case):
    request, variant_id = _setup(case)
    services = case["services"]
    cue_id = str(uuid.uuid4())
    request.update(
        speech_xml=_sound_xml(request["segment_id"], variant_id, cue_id),
        expected_sound_plan_revision=0,
    )
    with services["database"].session() as session:
        signature_before = plan_signature(session, case["revision_id"])
        segment_before = session.get(m.GenerationSegment, request["segment_id"]).revision
        settings_before = session.scalars(select(m.SessionSettingHistory.id)).all()
        before = (session.scalar(select(m.PerformancePlan.id)), session.scalar(select(ProjectSoundPlan.id)))
        preview = preview_speech_selection(services, session, case["session_id"], request)
        after = (session.scalar(select(m.PerformancePlan.id)), session.scalar(select(ProjectSoundPlan.id)))
        assert before == after == (None, None)
        assert preview["sound_changes"]["added"] == [cue_id]
    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        assert session.scalar(select(m.PerformancePlan.id)) is None
        assert session.scalar(select(ProjectSoundCue.logical_id)) == cue_id
        assert plan_signature(session, case["revision_id"]) == signature_before
        assert session.get(m.GenerationSegment, request["segment_id"]).revision == segment_before
        assert session.scalars(select(m.SessionSettingHistory.id)).all() == settings_before
        assert applied["sound_plan_revision"] == 1
        assert applied["performance_plan"] is None
    with services["database"].session() as session:
        context = get_speech_selection_context(services, session, case["session_id"], {
            key: request[key] for key in (
                "revision_id", "segment_id", "expected_segment_revision", "start", "end",
            )
        })
    assert context["speech_xml"].count("<sound ") == 1
    assert context["sound_plan_revision"] == 1


def test_missing_stale_revision_and_guard(case):
    request, variant_id = _setup(case)
    services = case["services"]
    request["speech_xml"] = _sound_xml(request["segment_id"], variant_id, str(uuid.uuid4()))
    with services["database"].session() as session:
        with pytest.raises(ValueError, match="expected_sound_plan_revision"):
            preview_speech_selection(services, session, case["session_id"], request)
        request["expected_sound_plan_revision"] = 1
        with pytest.raises(RevisionConflict):
            preview_speech_selection(services, session, case["session_id"], request)
        request["expected_sound_plan_revision"] = 0
        preview = preview_speech_selection(services, session, case["session_id"], request)
    with services["database"].immediate_session() as session:
        with pytest.raises(RevisionConflict):
            apply_speech_selection(
                services, session, case["session_id"],
                {**request, "expected_preview_revision": "0" * 64},
            )
        assert session.scalar(select(ProjectSoundPlan.id)) is None
        assert preview["preview_revision"] != "0" * 64
    with services["database"].immediate_session() as session:
        write_version(session, case["session_id"], None, case["revision_id"], [])
    with services["database"].immediate_session() as session:
        with pytest.raises(RevisionConflict):
            apply_speech_selection(
                services, session, case["session_id"],
                {**request, "expected_preview_revision": preview["preview_revision"]},
            )


def test_removing_all_visible_markers_preserves_other_segment_and_time_cues(case):
    request, variant_id = _setup(case)
    services = case["services"]
    visible_id, other_id, time_id = (str(uuid.uuid4()) for _ in range(3))
    def cue(identifier, anchor):
        return {
            "logical_id": identifier, "variant_id": variant_id,
            "anchor_json": anchor, "playback_json": {"mode": "insert", "offset_ms": 0},
            "included": True, "placement_status": "ready",
        }
    with services["database"].immediate_session() as session:
        write_version(session, case["session_id"], None, case["revision_id"], [
            cue(visible_id, {"kind": "before_segment", "segment_id": request["segment_id"]}),
            cue(other_id, {"kind": "before_segment", "segment_id": case["segment_ids"][1]}),
            cue(time_id, {"kind": "source_time", "time_seconds": 1.0}),
        ])
    request.update(
        speech_xml=f'<segment id="{request["segment_id"]}">Hello world</segment>',
        expected_sound_plan_revision=1,
    )
    with services["database"].session() as session:
        context = get_speech_selection_context(services, session, case["session_id"], {
            key: request[key] for key in (
                "revision_id", "segment_id", "expected_segment_revision", "start", "end",
            )
        })
        assert context["speech_xml"].count("<sound ") == 1
        preview = preview_speech_selection(services, session, case["session_id"], request)
        assert preview["sound_changes"]["removed"] == [visible_id]
        assert len(session.scalars(select(ProjectSoundPlan)).all()) == 1
    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        retained = set(session.scalars(
            select(ProjectSoundCue.logical_id).where(ProjectSoundCue.plan_id == session.scalar(
                select(ProjectSoundPlan.id).order_by(ProjectSoundPlan.revision.desc()).limit(1)
            ))
        ))
        assert retained == {other_id, time_id}
        assert applied["sound_plan_revision"] == 2


def test_foreign_cue_and_invalid_variant_rejected(case):
    request, variant_id = _setup(case)
    services = case["services"]
    foreign_id = str(uuid.uuid4())
    with services["database"].immediate_session() as session:
        write_version(session, case["session_id"], None, case["revision_id"], [{
            "logical_id": foreign_id, "variant_id": variant_id,
            "anchor_json": {"kind": "before_segment", "segment_id": case["segment_ids"][1]},
            "playback_json": {"mode": "insert", "offset_ms": 0},
            "included": True, "placement_status": "ready",
        }])
    request.update(expected_sound_plan_revision=1)
    request["speech_xml"] = _sound_xml(request["segment_id"], variant_id, foreign_id)
    with services["database"].session() as session:
        with pytest.raises(ValueError, match="outside this representable segment"):
            preview_speech_selection(services, session, case["session_id"], request)
        request["speech_xml"] = _sound_xml(
            request["segment_id"], str(uuid.uuid4()), str(uuid.uuid4())
        )
        with pytest.raises(ValueError, match="ready sound variant"):
            preview_speech_selection(services, session, case["session_id"], request)


def test_mixed_speech_sound_failure_rolls_back_both(case, monkeypatch):
    request, variant_id = _setup(case)
    services = case["services"]
    cue_id = str(uuid.uuid4())
    request.update(
        speech_xml=_sound_xml(request["segment_id"], variant_id, cue_id).replace(
            "Hello", '<ins>softly</ins>Hello'
        ),
        expected_sound_plan_revision=0,
    )
    with services["database"].session() as session:
        preview = preview_speech_selection(services, session, case["session_id"], request)
    def fail_write(*_args, **_kwargs):
        raise RuntimeError("simulated sound write failure")
    monkeypatch.setattr(adapter, "write_version", fail_write)
    with pytest.raises(RuntimeError, match="simulated sound write failure"):
        with services["database"].immediate_session() as session:
            apply_speech_selection(
                services, session, case["session_id"],
                {**request, "expected_preview_revision": preview["preview_revision"]},
            )
    with services["database"].session() as session:
        assert session.scalar(select(m.PerformancePlan.id)) is None
        assert session.scalar(select(ProjectSoundPlan.id)) is None


def test_mixed_speech_and_sound_commits_speech_only_xml(case):
    request, variant_id = _setup(case)
    services = case["services"]
    cue_id = str(uuid.uuid4())
    request.update(
        speech_xml=_sound_xml(request["segment_id"], variant_id, cue_id).replace(
            "Hello", '<ins>softly</ins>Hello'
        ),
        expected_sound_plan_revision=0,
    )
    with services["database"].session() as session:
        preview = preview_speech_selection(services, session, case["session_id"], request)
    with services["database"].immediate_session() as session:
        result = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        persisted = session.get(m.PerformancePlan, result["performance_plan"]["id"])
        saved_xml = persisted.manual_annotations_json[request["segment_id"]]["_speech_xml"]
        assert "<sound " not in saved_xml and "<ins>softly</ins>" in saved_xml
        assert session.scalar(select(ProjectSoundCue.logical_id)) == cue_id
        assert result["speech_xml"].count("<sound ") == 1
        assert result["sound_plan_revision"] == 1


def test_legacy_speech_xml_without_markers_preserves_existing_cue(case):
    request, variant_id = _setup(case)
    services = case["services"]
    cue_id = str(uuid.uuid4())
    with services["database"].immediate_session() as session:
        write_version(session, case["session_id"], None, case["revision_id"], [{
            "logical_id": cue_id, "variant_id": variant_id,
            "anchor_json": {"kind": "before_segment", "segment_id": request["segment_id"]},
            "playback_json": {"mode": "insert", "offset_ms": 0},
            "included": True, "placement_status": "ready",
        }])
    request["speech_xml"] = f'<segment id="{request["segment_id"]}">Hello world</segment>'
    with services["database"].session() as session:
        preview = preview_speech_selection(services, session, case["session_id"], request)
        assert preview["sound_changes"] == {"added": [], "updated": [], "removed": []}
    with services["database"].immediate_session() as session:
        result = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        assert result["sound_plan_revision"] == 1
        assert session.scalar(select(ProjectSoundCue.logical_id)) == cue_id
        assert len(session.scalars(select(ProjectSoundPlan)).all()) == 1


def test_large_cue_plan_projects_32_and_preserves_unrepresented_on_edit(case):
    request, variant_id = _setup(case)
    services = case["services"]
    identifiers = [str(uuid.UUID(int=index + 1)) for index in range(45)]
    cues = [{
        "logical_id": identifier, "variant_id": variant_id,
        "anchor_json": {"kind": "before_segment", "segment_id": request["segment_id"]},
        "playback_json": {"mode": "insert", "offset_ms": 0},
        "included": True, "placement_status": "ready",
    } for identifier in reversed(identifiers)]
    with services["database"].immediate_session() as session:
        write_version(session, case["session_id"], None, case["revision_id"], cues)
        stored = session.scalar(select(ProjectSoundCue).where(
            ProjectSoundCue.logical_id == identifiers[0]
        ))
        stored.anchor_json = {**stored.anchor_json, "origin_resolution": "private"}
    with services["database"].session() as session:
        context = get_speech_selection_context(services, session, case["session_id"], request)
        root = ET.fromstring(context["speech_xml"])
        assert [child.attrib["cue"] for child in root if child.tag == "sound"] == identifiers[:32]
        assert context["sound_cue_count"] == 45
        assert context["unrepresented_sound_cue_count"] == 13
        assert context["sound_cues_truncated"] is True
        assert len(context["sound_cues"]) == 40
        assert sum(not cue["represented_in_xml"] for cue in context["sound_cues"]) == 8
        assert "origin_resolution" not in context["sound_cues"][0]["anchor"]
        root.remove(next(child for child in root if child.attrib.get("cue") == identifiers[0]))
        request.update(
            speech_xml=ET.tostring(root, encoding="unicode"),
            expected_sound_plan_revision=1,
        )
        preview = preview_speech_selection(services, session, case["session_id"], request)
        assert preview["sound_changes"]["removed"] == [identifiers[0]]
    with services["database"].immediate_session() as session:
        result = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        latest_plan = session.scalar(
            select(ProjectSoundPlan).order_by(ProjectSoundPlan.revision.desc()).limit(1)
        )
        retained = set(session.scalars(
            select(ProjectSoundCue.logical_id).where(ProjectSoundCue.plan_id == latest_plan.id)
        ))
        assert retained == set(identifiers[1:])
        assert result["sound_plan_revision"] == 2
