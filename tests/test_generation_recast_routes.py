"""Shared recast admission, retry and undo contracts in a disposable app."""

from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.generation_audio_versions import record_audio_version_take
from pandrator.web.generation_recast_workspace import start_recast_in_session
from pandrator.web.settings_policy import RevisionConflict
from tests.test_generation_recast import case as case
from tests.test_generation_recast import change, prepare, record
from tests.test_generation_recast import recast_case as recast_case


def post(case, suffix, body=None, key=None):
    return case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-recast{suffix}",
        json=body or {},
        headers={**case["headers"], "Idempotency-Key": key or str(uuid4())},
    )


def start(case):
    record(case)
    change(case)
    preview = post(case, "/preview", {"selector_keys": ["character:fred"]})
    assert preview.status_code == 200, preview.get_json()
    body = {"selector_keys": preview.json["selected_keys"],
            "speech_plan_revision_id": preview.json["speech_plan_revision_id"],
            "expected_selection_hash": preview.json["selection_hash"]}
    key = str(uuid4())
    response = post(case, "", body, key)
    assert response.status_code == 201, response.get_json()
    return response.json, body, key


def finish(case, run_id):
    database = case["services"]["database"]
    with database.session() as session:
        run = session.get(m.GenerationRun, run_id)
        segment_id = run.audio_version_json["target_segment_ids"][0]
        original = session.scalar(select(m.AudioTake).where(
            m.AudioTake.generation_segment_id == segment_id, m.AudioTake.is_active.is_(True)
        ))
        original.is_active = False
        original.revision += 1
        replacement = m.AudioTake(
            generation_segment_id=segment_id, generation_run_id=run.id,
            artifact_id=original.artifact_id, status="completed", is_active=True,
        )
        session.add(replacement)
        session.flush()
        record_audio_version_take(session, run.id, replacement, applied=True)
        run.status = "completed"
        session.get(m.Job, run.job_id).status = "succeeded"
        return segment_id, replacement.id, run.audio_version_json["before_take_ids"][segment_id]


def test_start_pins_baseline_and_retry_replays_without_repreparing(recast_case, monkeypatch):
    case = recast_case
    run, body, key = start(case)
    with case["services"]["database"].session() as session:
        stored = session.get(m.GenerationRun, run["id"])
        assert stored.output_generation_run_id is None
        assert len(stored.audio_version_json["before_take_ids"]) == 3
        assert stored.settings_snapshot_json["generation_recast_controls_revision"] > 0
    monkeypatch.setattr("pandrator.web.generation_recast.prepare_recast",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Retry recompiled")))
    replay = post(case, "", body, key)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json["id"] == run["id"]


def test_stale_preview_rejects_and_reservation_can_be_retried(recast_case):
    case = recast_case
    record(case)
    change(case)
    preview = post(case, "/preview", {"selector_keys": ["character:fred"]}).json
    body = {"selector_keys": preview["selected_keys"], "expected_selection_hash": preview["selection_hash"]}
    change(case, fred="Charon")
    key = str(uuid4())
    rejected = post(case, "", body, key)
    assert rejected.status_code == 409, rejected.json
    fresh = post(case, "/preview", {"selector_keys": ["character:fred"]}).json
    body["expected_selection_hash"] = fresh["selection_hash"]
    assert post(case, "", body, key).status_code == 201


def test_idempotency_key_required_and_changed_payload_conflicts(recast_case):
    case = recast_case
    missing = case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-recast",
        json={"expected_selection_hash": "unused"}, headers=case["headers"],
    )
    assert missing.status_code == 400
    _run, body, key = start(case)
    assert post(case, "", {**body, "force": True}, key).status_code == 409


def test_active_failed_job_is_rechecked_in_writer(recast_case):
    import pytest

    case = recast_case
    record(case)
    change(case)
    prepared = prepare(case, selector_keys=["character:fred"])
    with case["services"]["database"].immediate_session() as session:
        job = m.Job(session_id=case["session_id"], kind="generation.run", status="running")
        session.add(job)
        session.flush()
        session.add(m.GenerationRun(session_id=case["session_id"], plan_revision_id=case["revision_id"],
                                    sequence_number=2, status="failed", job_id=job.id))
    with pytest.raises(RevisionConflict), case["services"]["database"].immediate_session() as session:
        start_recast_in_session(case["services"]["generation"], session, case["session_id"],
                                prepared, prepared["preview"]["selection_hash"])


def test_before_after_reads_and_atomic_undo_preserve_manifest_and_cast(recast_case):
    case = recast_case
    run, _body, _key = start(case)
    segment_id, replacement_id, baseline_id = finish(case, run["id"])
    base = f"/api/v1/sessions/{case['session_id']}/generation-segments"
    for side, expected in [("before", baseline_id), ("after", replacement_id)]:
        response = case["client"].get(base, query_string={"generation_run_id": run["id"], "audio_version_side": side})
        assert response.status_code == 200, response.json
        segment = next(item for item in response.json["items"] if item["id"] == segment_id)
        assert segment["history_take_id"] == expected
    path = f"/api/v1/generation-runs/{run['id']}/audio-version"
    preview = case["client"].post(path + "/undo-preview", json={}, headers=case["headers"])
    assert preview.json["restore_count"] == 1, preview.json
    with case["services"]["database"].session() as session:
        before_manifest = deepcopy(session.get(m.GenerationRun, run["id"]).audio_version_json)
    response = case["client"].post(path + "/undo", json={"expected_selection_hash": preview.json["selection_hash"]},
        headers={**case["headers"], "Idempotency-Key": str(uuid4())})
    assert response.status_code == 200, response.json
    assert response.json["restored_count"] == 1
    with case["services"]["database"].session() as session:
        selected = session.scalar(select(m.AudioTake).where(m.AudioTake.generation_segment_id == segment_id,
                                                            m.AudioTake.is_active.is_(True)))
        assert selected.id == baseline_id
        manifest = session.get(m.GenerationRun, run["id"]).audio_version_json
        assert manifest["after_take_ids"] == before_manifest["after_take_ids"]
        assert manifest["undone"]
    assert case["client"].get(f"/api/v1/sessions/{case['session_id']}/generation-controls").json["cast"]["characters"]["fred"]["voice"] == "Fenrir"


def test_manual_reselection_blocks_undo(recast_case):
    case = recast_case
    run, _body, _key = start(case)
    segment_id, replacement_id, _baseline_id = finish(case, run["id"])
    database = case["services"]["database"]
    with database.session() as session:
        revision = session.get(m.GenerationSegment, segment_id).revision
    case["services"]["generation"].select_take(segment_id, replacement_id, revision)
    preview = case["client"].post(f"/api/v1/generation-runs/{run['id']}/audio-version/undo-preview",
                                   json={}, headers=case["headers"])
    assert preview.json["restore_count"] == 0
    assert "manual choice" in preview.json["blocked_reason"]
