"""Atomic selection batch behavior through the authenticated API."""

from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web import speech_sound_cues
from pandrator.web.sound_project_commands import write_version
from pandrator.web.sound_project_models import ProjectSoundCue, ProjectSoundPlan
from tests.test_performance_plans import case as case  # noqa: PLC0414
from tests.test_performance_plans import create


def _change(case, index, **fields):
    segment_id = case["segment_ids"][index]
    with case["services"]["database"].session() as session:
        revision = session.get(m.GenerationSegment, segment_id).revision
    return {
        "revision_id": case["revision_id"],
        "segment_id": segment_id,
        "expected_segment_revision": revision,
        "start": 0,
        "end": 5,
        **fields,
    }


def _post(case, suffix, body, *, key=None):
    headers = dict(case["headers"])
    if key:
        headers["Idempotency-Key"] = key
    return case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/{suffix}',
        json=body, headers=headers,
    )


def test_three_edits_create_one_adopted_plan_and_replay(case):
    changes = [_change(case, index, speaker="narrator") for index in range(3)]
    before = None
    with case["services"]["database"].session() as session:
        before = list(session.scalars(select(m.PerformancePlan.id)))
    preview = _post(case, "selection-batch-preview", {"changes": changes})
    assert preview.status_code == 200, preview.get_json()
    with case["services"]["database"].session() as session:
        assert list(session.scalars(select(m.PerformancePlan.id))) == before
    body = {"changes": changes, "expected_preview_revision": preview.get_json()["preview_revision"]}
    applied = _post(case, "selection-batch", body, key="selection-batch-replay")
    assert applied.status_code == 200, applied.get_json()
    result = applied.get_json()
    assert result["performance_plan"]["status"] == "adopted"
    assert result["affected_segment_ids"] == case["segment_ids"]
    with case["services"]["database"].session() as session:
        all_plans = list(session.scalars(select(m.PerformancePlan)))
        assert len(all_plans) == 1
        annotations = plans._annotations(session, all_plans[0])
        assert set(case["segment_ids"]).issubset(annotations)
        session.get(m.GenerationSegment, case["segment_ids"][0]).revision += 1
    replay = _post(case, "selection-batch", body, key="selection-batch-replay")
    assert replay.status_code == 200, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == result


def test_same_segment_disjoint_fields_combine_and_conflicts_reject(case):
    first = _change(case, 0, speaker="narrator")
    second = _change(case, 0, delivery={"pace": "slower"})
    preview = _post(case, "selection-batch-preview", {
        "changes": [first, second], "detailed_preview": True,
    })
    assert preview.status_code == 200, preview.get_json()
    xml = preview.get_json()["details"][0]["speech_xml"]
    assert "narrator" in xml and "slower" in xml
    conflict = _post(case, "selection-batch-preview", {"changes": [first, first]})
    assert conflict.status_code == 422
    mismatch = _post(case, "selection-batch-preview", {
        "changes": [first, {**second, "expected_segment_revision": second["expected_segment_revision"] + 1}],
    })
    assert mismatch.status_code == 409
    with case["services"]["database"].session() as session:
        assert session.scalar(select(m.PerformancePlan.id)) is None


def test_invalid_xml_and_stale_token_leave_no_plan(case):
    change = _change(case, 0, speaker="narrator")
    preview = _post(case, "selection-batch-preview", {"changes": [change]})
    assert preview.status_code == 200, preview.get_json()
    stale = _post(case, "selection-batch", {
        "changes": [change], "expected_preview_revision": "0" * 64,
    }, key="selection-batch-stale")
    assert stale.status_code == 409
    invalid = _post(case, "selection-batch-preview", {"changes": [
        {**change, "speaker": "unchanged", "end": 5,
         "speech_xml": f'<segment id="{change["segment_id"]}">wrong words</segment>'},
    ]})
    assert invalid.status_code == 422
    with case["services"]["database"].session() as session:
        assert session.scalar(select(m.PerformancePlan.id)) is None


def test_duplicate_cursor_event_rejected(case):
    event = _change(case, 0, start=5, end=5, vocal_event={"kind": "pause"})
    result = _post(case, "selection-batch-preview", {"changes": [event, event]})
    assert result.status_code == 422


def test_missing_key_is_rejected(case):
    change = _change(case, 0, speaker="narrator")
    preview = _post(case, "selection-batch-preview", {"changes": [change]})
    assert preview.status_code == 200
    result = _post(case, "selection-batch", {
        "changes": [change], "expected_preview_revision": preview.get_json()["preview_revision"],
    })
    assert result.status_code == 422


def test_locked_change_requires_every_request_unlock(case):
    source = create(case, annotation_format="xml")
    segment_id = case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        text = session.get(m.GenerationSegment, segment_id).text
    edited = case["post"](f'/{source["id"]}', {
        "expected_version": source["version"],
        "items": [{"segment_id": segment_id,
                   "speech_xml": f'<segment id="{segment_id}">{text}</segment>',
                   "locked": True}],
    }, method="patch")
    assert edited.status_code == 200, edited.get_json()
    adopted = case["post"](f'/{source["id"]}/adopt', {
        "expected_version": edited.get_json()["version"], "accept_unanalysed": True,
    })
    assert adopted.status_code == 200, adopted.get_json()
    first = _change(case, 0, speaker="narrator", unlock_locked=True)
    second = _change(case, 0, delivery={"pace": "slower"})
    preview = _post(case, "selection-batch-preview", {"changes": [first, second]})
    assert preview.status_code == 200, preview.get_json()
    denied = _post(case, "selection-batch", {
        "changes": [first, second],
        "expected_preview_revision": preview.get_json()["preview_revision"],
    }, key="selection-batch-lock")
    assert denied.status_code == 409
    with case["services"]["database"].session() as session:
        rows = list(session.scalars(select(m.PerformancePlan)))
        assert len(rows) == 1 and rows[0].status == "adopted"


def test_optimized_speech_preserves_display_and_timing(case):
    segment_id = case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, segment_id)
        display = segment.text
        segment.optimized_text = "Spoken differently today."
        segment.speech_block_provenance_json = {"start_ms": 1200, "end_ms": 3400}
    change = _change(case, 0, end=6, speaker="narrator")
    preview = _post(case, "selection-batch-preview", {"changes": [change]})
    assert preview.status_code == 200, preview.get_json()
    applied = _post(case, "selection-batch", {
        "changes": [change], "expected_preview_revision": preview.get_json()["preview_revision"],
    }, key="selection-batch-timing")
    assert applied.status_code == 200, applied.get_json()
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, segment_id)
        assert segment.text == display
        assert segment.optimized_text == "Spoken differently today."
        assert segment.speech_block_provenance_json == {"start_ms": 1200, "end_ms": 3400}
        adopted = session.get(m.PerformancePlan, applied.get_json()["performance_plan"]["id"])
        unit = next(item for item in adopted.units_json if item["id"] == segment_id)
        assert unit["text"] == display
        assert unit["spoken_text"] == "Spoken differently today."
        assert unit["timing"] == {"start_ms": 1200, "end_ms": 3400}


def test_sound_xml_batch_writes_one_version_preserving_other_cues(case, monkeypatch):
    import uuid

    services = case["services"]
    first_id, second_id, third_id = case["segment_ids"]
    first_cue, second_cue, kept_cue = (str(uuid.uuid4()) for _ in range(3))
    with services["database"].session() as session:
        artifact = m.Artifact(
            kind="audio", role="sound.original", relative_path="bell.wav", content_hash="bell",
        )
        session.add(artifact)
        session.flush()
        variant = m.SoundVariant(
            artifact_id=artifact.id, state="ready", duration_seconds=1.0,
            provenance_json={"display_name": "Bell"},
        )
        session.add(variant)
        session.flush()
        variant_id = variant.id
        texts = {
            segment_id: session.get(m.GenerationSegment, segment_id).text
            for segment_id in (first_id, second_id)
        }
    with services["database"].immediate_session() as session:
        write_version(session, case["session_id"], None, case["revision_id"], [{
            "logical_id": kept_cue, "variant_id": variant_id,
            "anchor_json": {"kind": "before_segment", "segment_id": third_id},
            "playback_json": {"mode": "insert", "offset_ms": 0},
            "included": True, "placement_status": "ready",
        }])
    changes = []
    for segment_id, cue in ((first_id, first_cue), (second_id, second_cue)):
        with services["database"].session() as session:
            revision = session.get(m.GenerationSegment, segment_id).revision
        changes.append({
            "revision_id": case["revision_id"], "segment_id": segment_id,
            "expected_segment_revision": revision,
            "start": 0, "end": len(texts[segment_id]),
            "speech_xml": (
                f'<segment id="{segment_id}"><sound cue="{cue}" '
                f'variant="{variant_id}" placement="before"/>'
                f'{texts[segment_id]}</segment>'
            ),
            "expected_sound_plan_revision": 1,
        })
    loads = 0
    original = speech_sound_cues.latest_cues

    def counted(*args, **kwargs):
        nonlocal loads
        loads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(speech_sound_cues, "latest_cues", counted)
    preview = _post(case, "selection-batch-preview", {"changes": changes})
    assert preview.status_code == 200, preview.get_json()
    assert loads == 1
    assert set(preview.get_json()["sound_changes"]["added"]) == {first_cue, second_cue}
    applied = _post(case, "selection-batch", {
        "changes": changes, "expected_preview_revision": preview.get_json()["preview_revision"],
    }, key="selection-batch-sounds")
    assert applied.status_code == 200, applied.get_json()
    assert loads == 3  # Preview, apply preflight, and writer guard recheck.
    assert applied.get_json()["sound_plan_revision"] == 2
    with services["database"].session() as session:
        sound_plans = list(session.scalars(select(ProjectSoundPlan).order_by(ProjectSoundPlan.revision)))
        assert len(sound_plans) == 2
        latest = sound_plans[-1]
        assert set(session.scalars(
            select(ProjectSoundCue.logical_id).where(ProjectSoundCue.plan_id == latest.id)
        )) == {first_cue, second_cue, kept_cue}
        assert len(list(session.scalars(select(m.PerformancePlan)))) == 1
