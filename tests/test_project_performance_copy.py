"""Current annotation intent survives real copy, member review and generation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from sqlalchemy import event, func, select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import actor_from_principal, execution_actor_scope
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.jobs import Worker
from pandrator.web.projects import copy_publication
from pandrator.web.projects.copy_contracts import ProjectCopyError
from pandrator.web.projects.copy_performance_capture import capture_current_performance
from pandrator.web.projects.copy_speech import segment_copy_references
from pandrator.web.projects.copy_speech_plan import speech_plan_signature
from pandrator.web.speech_plan_context import plan_signature
from tests.test_account_authentication_integration import current_browser_principal
from tests.test_evidence_access_routes import body, headers
from tests.test_google_design_workspace_http import case as case  # noqa: F401
from tests.test_google_design_workspace_http import design as design  # noqa: F401
from tests.test_google_design_workspace_http import no_network as no_network  # noqa: F401
from tests.test_project_null_speech_copy import capture_source, null_source
from tests.test_project_output_copy import snapshot_for
from tests.test_project_sound_copy import _copy
from tests.test_retained_preview_workspace import MODEL
from tests.test_settings_workspace_access import put
from tests.test_voice_lifecycle_idempotency import silent_wav


def annotation(direction):
    return {"decision": "steer", "delivery": {"instruction": direction}}


def command(client, csrf, root, path, values, *, method="post", status=200):
    return body(getattr(client, method)(root + path, json=values,
        headers=headers(csrf, str(uuid4()))), status)


@pytest.fixture
def performance(design):
    f, u = design, design.users
    source = null_source(f)
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=u.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        speech = f.services.generation.create_plan(source["id"], source_revision_id=None,
            settings={}, segments=[{"text": text, "language": "en"}
                for text in ("The first line.", "The second line.", "The final line.")])
    with f.case.database.immediate_session() as unit:
        segments = list(unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == speech["active_revision_id"])
            .order_by(m.GenerationSegment.ordinal)))
        segments[0].speech_plan_json = {"parent_revision_id": str(uuid4()),
            "speech_xml": f'<segment id="{segments[0].id}">The first line.</segment>'}
        ids = [segment.id for segment in segments]
    root = f"/api/v1/sessions/{source['id']}/performance-plans"
    body(u.alice.put(f"/api/v1/sessions/{source['id']}/generation-controls",
        json={"expected_revision": 0, "characters": [{"id": "fred", "display_name": "Fred"}]},
        headers=headers(u.ah, "performance-copy-characters")))

    def create(**options):
        return command(u.alice, u.ah, root, "",
            {"expected_plan_revision_id": speech["active_revision_id"], **options}, status=201)

    adopted = create(annotation_format="xml", purpose="combined")
    items = [{"segment_id": key, "speech_xml":
        f'<segment id="{key}">' + (
            '<ins>calm</ins><dialogue><speaker ref="fred">The first line.</speaker></dialogue>'
            if index == 0 else ("The second line." if index == 1 else "The final line."))
        + '</segment>', "locked": index == 0} for index, key in enumerate(ids)]
    changed = command(u.alice, u.ah, root, "/" + adopted["id"],
        {"expected_version": adopted["version"], "items": items}, method="patch")
    command(u.alice, u.ah, root, "/" + adopted["id"] + "/adopt",
        {"expected_version": changed["version"], "enable": True})
    draft = create(mode="passive", annotation_format="pssml", batch_size=1,
        model_name="private-analysis-model", instructions="private-analysis-instructions")
    lease = command(u.alice, u.ah, root, "/" + draft["id"] + "/claim", {})
    command(u.alice, u.ah, root, "/" + draft["id"] + "/batches/" + lease["batch_id"] + "/submit",
        {"lease_token": lease["lease_token"], "items": [{"segment_id": ids[1],
            "annotation": annotation("automatic direction")} ]})
    draft = body(u.alice.get(root + "/" + draft["id"]))
    command(u.alice, u.ah, root, "/" + draft["id"],
        {"expected_version": draft["version"], "items": [{"segment_id": ids[1],
            "annotation": annotation("manual direction")} ]}, method="patch")
    speakers = create(mode="manual", purpose="speakers", annotation_format="xml")
    with f.case.database.immediate_session() as unit:
        completed = unit.scalar(select(m.PerformanceBatch).where(
            m.PerformanceBatch.performance_plan_id == draft["id"],
            m.PerformanceBatch.status == "completed"))
        assert completed is not None
        completed.usage_json = {"private_usage": "private-analysis-usage"}
        unit.get_one(m.PerformancePlan, draft["id"]).job_id = "private-analysis-job"
    return SimpleNamespace(f=f, u=u, source=source, speech=speech, ids=ids, root=root,
        adopted=adopted, draft=draft, speakers=speakers)


def test_registered_copy_member_edit_adopt_recopy_and_generation(performance, monkeypatch):
    p, f, u = performance, performance.f, performance.u
    body(put(u.owner, u.oh, "services.tts", {"provider_configs": [{
        "id": "audio_cpp", "enabled": True, "models": [MODEL], "default_model": MODEL,
        "api_base": "https://speech.invalid", "connection_mode": "external",
        "api_key": "synthetic-performance-copy-key", "credential_backend": "database",
    }]}, revision=1, workspace=u.workspace_id))
    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    frozen = json.loads(snapshot.state_json)["rows"]["performance_plans"]
    assert len(frozen) == 3
    assert all(row["version"] == 1 and row["job_id"] is None for row in frozen)
    assert all(row["settings_json"]["mode"] == "manual" for row in frozen)
    assert all(marker not in snapshot.state_json for marker in (
        "private-analysis-", "automatic direction", "performance_batches"))
    partial = next(row for row in frozen if row["id"] == p.draft["id"])
    assert set(partial["manual_annotations_json"]) == set(p.ids[:2])
    assert partial["manual_annotations_json"][p.ids[1]]["delivery"]["instruction"] == "manual direction"
    with f.case.database.snapshot_session() as unit:
        source_rows = deepcopy([dict(row) for row in unit.execute(select(m.PerformancePlan.__table__)
            .where(m.PerformancePlan.session_id == p.source["id"])).mappings()])
    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, "copy-performance-intent")
    target = copied["source_session_id"]
    with f.case.database.snapshot_session() as unit:
        plans = list(unit.scalars(select(m.PerformancePlan).where(m.PerformancePlan.session_id == target)))
        assert len(plans) == 3
        assert unit.scalar(select(func.count()).select_from(m.PerformanceBatch)
            .where(m.PerformanceBatch.performance_plan_id.in_([row.id for row in plans]))) == 0
        segments = list(unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == plans[0].plan_revision_id)
            .order_by(m.GenerationSegment.ordinal)))
        target_ids = [row.id for row in segments]
        draft = next(row for row in plans if row.settings_json["annotation_format"] == "pssml")
        draft_id, revision_id = draft.id, draft.plan_revision_id
        assert draft.base_signature == plan_signature(unit, revision_id)
        assert p.ids[0] not in draft.units_json[0]["speech_xml"]
        assert target_ids[0] in draft.units_json[0]["speech_xml"]
        assert 'ref="fred"' in draft.units_json[0]["speech_xml"]
        assert not (set(p.ids) & set(draft.manual_annotations_json))
    with f.case.database.immediate_session() as unit:
        unit.delete(unit.get_one(m.WorkspaceMembership, (u.workspace_id, u.alice_id)))
    base = f"/api/v1/sessions/{target}/performance-plans"
    assert u.alice.get(base).status_code == 404
    assert len(body(u.bob.get(base))["items"]) == 3
    changed = command(u.bob, u.bh, base, "/" + draft_id,
        {"expected_version": 1, "items": [{"segment_id": target_ids[1],
            "annotation": annotation("member direction")} ]}, method="patch")
    command(u.bob, u.bh, base, "/" + draft_id + "/adopt",
        {"expected_version": changed["version"], "accept_unanalysed": True, "enable": True})
    second, _ = _copy(f, {"project_id": copied["project_id"]}, u.bob, u.bh,
        u.workspace_id, "recopy-performance-intent")
    with f.case.database.snapshot_session() as unit:
        # The earlier adopted row is now historical, and the untouched speaker
        # draft has an old adopted baseline. Only the current adopted intent moves.
        twice = list(unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == second["source_session_id"])))
        assert len(twice) == 1 and twice[0].status == "adopted"
        assert twice[0].base_signature == plan_signature(unit, twice[0].plan_revision_id)
        signature = plan_signature(unit, revision_id)
    body(u.bob.post(f"/api/v1/sessions/{target}/generation-plan/review",
        json={"revision_id": revision_id, "content_signature": signature},
        headers=headers(u.bh, "review-performance-copy")))
    calls = []

    def post(url, **options):
        assert url == "https://speech.invalid/v1/audio/speech"
        calls.append(options["json"])
        response = requests.Response()
        response.status_code, response.url = 200, url
        response._content, response._content_consumed = silent_wav(), True
        response.headers["Content-Type"] = "audio/wav"
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests.Session, "post", lambda _session, url, **kw: post(url, **kw))
    admission = body(u.bob.post(f"/api/v1/sessions/{target}/generation-admissions", json={},
        headers=headers(u.bh, "generate-performance-copy")), 202)
    worker = Worker(f.services.jobs, "performance-copy-worker", f.services.workflow_handlers.handlers())
    assert worker.run_once()
    accepted = f.services.jobs.get(admission["id"])
    assert accepted.status == "succeeded", accepted.error_message
    assert worker.run_once()
    generated = f.services.jobs.get(accepted.result_json["job_id"])
    assert generated.status == "succeeded", generated.error_message
    assert len(calls) == 3
    with f.case.database.snapshot_session() as unit:
        run = unit.get_one(m.GenerationRun, accepted.result_json["generation_run_id"])
        chosen = run.settings_snapshot_json["performance_snapshot"]
        assert set(chosen["annotations"]) == set(target_ids)
        assert chosen["annotations"][target_ids[1]]["annotation"]["delivery"]["instruction"] == "member direction"
        assert source_rows == [dict(row) for row in unit.execute(select(m.PerformancePlan.__table__)
            .where(m.PerformancePlan.session_id == p.source["id"])).mappings()]
        audio = unit.scalar(select(m.AudioTake.artifact_id).where(m.AudioTake.generation_run_id == run.id))
        assert audio is not None
    assert u.bob.get(f"/api/v1/artifacts/{audio}/content").status_code == 200


@pytest.mark.parametrize("kind", ["leased", "limit"])
def test_unavailable_current_performance_blocks_before_units_or_annotation_bodies(performance, kind):
    p, f = performance, performance.f
    with f.case.database.immediate_session() as unit:
        if kind == "leased":
            batch = unit.scalar(select(m.PerformanceBatch).where(
                m.PerformanceBatch.performance_plan_id == p.draft["id"],
                m.PerformanceBatch.status == "pending"))
            assert batch is not None
            batch.status, batch.lease_expires_at = "leased", m.utcnow() + timedelta(minutes=5)
    snapshot = capture_source(f, p.source) if kind == "limit" else None
    rows = json.loads(snapshot.state_json)["rows"] if snapshot else {
        "sessions": [{"id": p.source["id"]}],
        "generation_plans": [{"session_id": p.source["id"],
            "active_revision_id": p.speech["active_revision_id"]}],
    }
    statements = []

    def observe(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with f.case.database.snapshot_session() as unit:
            if kind == "limit":
                with pytest.raises(ProjectCopyError) as error:
                    capture_current_performance(unit, rows=rows, max_rows=1)
                assert error.value.code == "copy_limit_exceeded"
            else:
                saved, unavailable = capture_current_performance(unit, rows=rows, max_rows=100)
                assert saved == [] and unavailable == 1
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    assert not any("SELECT performance_plans.id, performance_plans.units_json" in sql
        or "performance_batches.annotations_json" in sql for sql in statements)


def edit_in_place(p, index, text):
    actor = actor_from_principal(current_browser_principal(replace(p.f.case, client=p.u.alice)))
    assert actor is not None
    with p.f.case.database.immediate_session() as unit:
        revision = unit.get_one(m.GenerationSegment, p.ids[index]).revision
    with execution_actor_scope(actor):
        result = p.f.services.generation.update_segment(p.ids[index], revision, {"text": text})
    assert result["id"] == p.ids[index]


def adopted_rows(snapshot):
    rows = json.loads(snapshot.state_json)["rows"]["performance_plans"]
    return [row for row in rows if row["status"] == "adopted"]


def test_signature_only_change_is_captured_with_every_entry(performance):
    p, f = performance, performance.f
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.PerformancePlan, p.adopted["id"]).base_signature = "stale"
    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    (row,) = adopted_rows(snapshot)
    rows = json.loads(snapshot.state_json)["rows"]
    current = speech_plan_signature(rows["generation_segments"],
        references=segment_copy_references(rows))
    assert [unit["id"] for unit in row["units_json"]] == p.ids
    assert set(row["manual_annotations_json"]) == set(p.ids)
    assert row["base_signature"] == current
    # The adopted plan plus both fresh drafts.
    assert len(json.loads(snapshot.state_json)["rows"]["performance_plans"]) == 3


def enable_performance(f, session_id):
    service = f.services.workspace_settings
    current = service.get(session_id, "tts")
    service.patch(session_id, "tts", current["revision"], {"performance_enabled": True})


def segment_identities(f, session_id, ids):
    snapshot, _digest = f.services.workspace_settings.resolve(session_id, sections=["tts", "audio"])
    with f.case.database.snapshot_session() as unit:
        context = AudioIdentityContext(unit, snapshot)
        return [context.for_segment(unit.get_one(m.GenerationSegment, key)) for key in ids]


def entries_by_ordinal(f, session_id, revision_id, ids):
    """The frozen speaker plan entries of a revision, keyed by ordinal, ids blanked."""
    from pandrator.web.performance_plans import freeze_performance_snapshot

    snapshot: dict = {}
    with f.case.database.snapshot_session() as unit:
        freeze_performance_snapshot(unit, revision_id, snapshot)
        ordinals = dict(unit.execute(select(m.GenerationSegment.id, m.GenerationSegment.ordinal)
            .where(m.GenerationSegment.plan_revision_id == revision_id)).all())
    frozen = snapshot["performance_snapshot"]
    return {ordinals[key]: json.loads(json.dumps(entry).replace(key, "<id>"))
        for key, entry in frozen["annotations"].items()}, frozen.get("review_segment_ids", [])


def test_copy_keeps_an_adopted_plan_with_a_fitted_and_a_review_segment(performance):
    p, f, u = performance, performance.f, performance.u
    # Segment 1 changes inside its plain entry (fits); segment 0's change reaches
    # the edge of its dialogue (needs review).
    enable_performance(f, p.source["id"])
    edit_in_place(p, 1, "The second, better line.")
    edit_in_place(p, 0, "The first line!")
    source_plan = body(u.alice.get(p.root + "/" + p.adopted["id"]))
    assert source_plan["stale"] is True and source_plan["review_segment_count"] == 1
    assert [item["id"] for item in source_plan["review_segments"]] == [p.ids[0]]
    source_entries, source_review = entries_by_ordinal(
        f, p.source["id"], p.speech["active_revision_id"], p.ids)
    assert source_review == [p.ids[0]] and set(source_entries) == {1, 2}

    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    (frozen,) = adopted_rows(snapshot)
    assert [unit["id"] for unit in frozen["units_json"]] == p.ids[1:]
    assert set(frozen["manual_annotations_json"]) == set(p.ids[1:])
    assert len(json.loads(snapshot.state_json)["rows"]["performance_plans"]) == 1
    for old in ("The first line.", "The second line.", "fred\">The first"):
        assert old not in snapshot.state_json, old

    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, "copy-judged-performance")
    target = copied["source_session_id"]
    with f.case.database.snapshot_session() as unit:
        plans = list(unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == target)))
        assert [plan.status for plan in plans] == ["adopted"]
        plan, revision_id = plans[0], plans[0].plan_revision_id
        target_ids = [row.id for row in unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal))]
        signature = plan_signature(unit, revision_id)
        plan_id = plan.id
        assert plan.base_signature != signature
        assert [unit["id"] for unit in plan.units_json] == target_ids[1:]
    read = body(u.alice.get(f"/api/v1/sessions/{target}/performance-plans/{plan_id}"))
    assert read["status"] == "adopted" and read["stale"] is True
    assert read["review_segment_count"] == 1 and read["changed_segment_count"] == 1
    assert [item["id"] for item in read["review_segments"]] == [target_ids[0]]

    entries, review = entries_by_ordinal(f, target, revision_id, target_ids)
    assert review == [target_ids[0]]
    assert entries == source_entries
    assert 'id="' not in json.dumps(entries).replace('id=\\"<id>\\"', "")

    # The same segments read the same way in both projects; the review block
    # alone carries the per-segment error. The fixture holds no takes, so this
    # identity equality is what governs reuse here.
    source_identities = segment_identities(f, p.source["id"], p.ids)
    target_identities = segment_identities(f, target, target_ids)
    assert "performance_error" in target_identities[0]
    assert "performance_error" in source_identities[0]
    for ordinal in (1, 2):
        assert "performance_error" not in target_identities[ordinal]
        assert target_identities[ordinal] == source_identities[ordinal]


def edit_segment(f, client, segment_id, text):
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=client)))
    assert actor is not None
    with f.case.database.immediate_session() as unit:
        revision = unit.get_one(m.GenerationSegment, segment_id).revision
    with execution_actor_scope(actor):
        f.services.generation.update_segment(segment_id, revision, {"text": text})


def plan_copy(client, csrf, session_id, revision_id, source_id):
    return command(client, csrf, f"/api/v1/sessions/{session_id}/performance-plans", "", {
        "expected_plan_revision_id": revision_id, "mode": "manual", "purpose": "combined",
        "annotation_format": "xml", "copy_from_id": source_id}, status=201)


def test_a_speaker_plan_copy_in_the_copied_project_sees_a_fitted_block_as_unchanged(performance):
    p, f, u = performance, performance.f, performance.u
    # Segment 1 changes inside its plain entry: the source plan is stale with
    # nothing to review, and the copy carries it fresh on the current text.
    edit_in_place(p, 1, "The second, better line.")
    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, "copy-fitted-only")
    target = copied["source_session_id"]
    with f.case.database.snapshot_session() as unit:
        (plan,) = unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == target))
        revision_id, plan_id = plan.plan_revision_id, plan.id
        assert plan.base_signature == plan_signature(unit, revision_id)
        assert all("source_hash" not in item for item in plan.units_json)
        target_ids = [row.id for row in unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == revision_id)
            .order_by(m.GenerationSegment.ordinal))]
    read = body(u.alice.get(f"/api/v1/sessions/{target}/performance-plans/{plan_id}"))
    assert read["stale"] is False and read["changed_segment_count"] == 0
    source_read = body(u.alice.get(p.root + "/" + p.adopted["id"]))
    assert source_read["stale"] is True and source_read["changed_segment_count"] == 1

    # A speaker plan copy in the source still sees the edit: the edited block
    # and its unlocked neighbour are left to review; the locked one is kept.
    source_copy = plan_copy(u.alice, u.ah, p.source["id"], p.speech["active_revision_id"],
        p.adopted["id"])
    assert (source_copy["copied_count"], source_copy["skipped_count"]) == (1, 2)
    # In the copied project the fitted block counts as unchanged, so every
    # entry is kept, its neighbours' included.
    target_copy = plan_copy(u.alice, u.ah, target, revision_id, plan_id)
    assert (target_copy["copied_count"], target_copy["skipped_count"]) == (3, 0)

    # After an edit in the copied project, the copied units (no block hash) are
    # compared field by field: the unchanged locked block keeps its entry.
    edit_segment(f, u.alice, target_ids[2], "The final, closing line.")
    assert body(u.alice.get(f"/api/v1/sessions/{target}/performance-plans/{plan_id}"))[
        "stale"] is True
    edited_copy = plan_copy(u.alice, u.ah, target, revision_id, plan_id)
    assert (edited_copy["copied_count"], edited_copy["skipped_count"]) == (1, 2)
    with f.case.database.snapshot_session() as unit:
        seeded = unit.get_one(m.PerformancePlan, edited_copy["id"]).manual_annotations_json
    assert set(seeded) == {target_ids[0]}


def test_copy_after_a_restore_of_a_stale_plan_judges_the_restored_blocks(performance):
    # A restore carries a stale adopted plan with its units still on their old
    # text and block hashes; copying the project right after it is not refused
    # and judges the same blocks as the restored revision's own plan read.
    p, f, u = performance, performance.f, performance.u
    enable_performance(f, p.source["id"])
    edit_in_place(p, 1, "The second, better line.")
    edit_in_place(p, 0, "The first line!")
    actor = actor_from_principal(current_browser_principal(replace(f.case, client=u.alice)))
    assert actor is not None
    with execution_actor_scope(actor):
        restored = f.services.generation.revise_topology(p.source["id"],
            p.speech["active_revision_id"],
            {"action": "restore", "target_revision_id": p.speech["active_revision_id"]})
    carried_id, revision = restored["carried_performance_plan_id"], restored["plan_revision_id"]
    restored_ids = restored["segment_ids"]
    carried = body(u.alice.get(p.root + "/" + carried_id))
    assert carried["status"] == "adopted" and carried["stale"] is True
    assert carried["carried_from_id"] == p.adopted["id"]
    assert [item["id"] for item in carried["review_segments"]] == [restored_ids[0]]
    restored_entries, restored_review = entries_by_ordinal(f, p.source["id"], revision,
        restored_ids)
    assert restored_review == [restored_ids[0]] and set(restored_entries) == {1, 2}

    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    (frozen,) = adopted_rows(snapshot)
    assert frozen["id"] == carried_id
    assert [unit["id"] for unit in frozen["units_json"]] == restored_ids[1:]
    assert "source_hash" not in json.dumps(frozen)
    for old in ("The first line.", "The second line.", "fred\">The first"):
        assert old not in snapshot.state_json, old

    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, "copy-restored-performance")
    target = copied["source_session_id"]
    with f.case.database.snapshot_session() as unit:
        (plan,) = unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == target))
        assert plan.status == "adopted"
        target_revision = plan.plan_revision_id
        target_ids = [row.id for row in unit.scalars(select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == target_revision)
            .order_by(m.GenerationSegment.ordinal))]
        plan_id = plan.id
    read = body(u.alice.get(f"/api/v1/sessions/{target}/performance-plans/{plan_id}"))
    assert read["status"] == "adopted" and read["stale"] is True
    assert [item["ordinal"] for item in read["review_segments"]] == [
        item["ordinal"] for item in carried["review_segments"]]
    assert [item["id"] for item in read["review_segments"]] == [target_ids[0]]
    entries, review = entries_by_ordinal(f, target, target_revision, target_ids)
    assert review == [target_ids[0]]
    assert entries == restored_entries
    restored_identities = segment_identities(f, p.source["id"], restored_ids)
    target_identities = segment_identities(f, target, target_ids)
    assert "performance_error" in restored_identities[0]
    assert "performance_error" in target_identities[0]
    for ordinal in (1, 2):
        assert "performance_error" not in target_identities[ordinal]
        assert target_identities[ordinal] == restored_identities[ordinal]


def test_a_new_draft_on_a_judged_adopted_plan_keeps_its_baseline_in_the_copy(performance):
    p, f, u = performance, performance.f, performance.u
    edit_in_place(p, 1, "The second, better line.")
    edit_in_place(p, 0, "The first line!")
    # The review asks for a new speaker plan: a draft on the same revision.
    review = command(u.alice, u.ah, p.root, "", {
        "expected_plan_revision_id": p.speech["active_revision_id"], "mode": "manual",
        "purpose": "speakers", "annotation_format": "xml"}, status=201)
    assert review["settings"]["base_adopted_plan_id"] == p.adopted["id"]

    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    rows = json.loads(snapshot.state_json)["rows"]["performance_plans"]
    assert sorted(row["id"] for row in rows) == sorted([p.adopted["id"], review["id"]])

    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, "copy-judged-draft")
    target = copied["source_session_id"]
    with f.case.database.snapshot_session() as unit:
        plans = {plan.status: plan for plan in unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == target))}
        assert set(plans) == {"adopted", "draft"}
        draft = plans["draft"]
        assert draft.settings_json["base_adopted_plan_id"] == plans["adopted"].id
        assert draft.base_signature == plan_signature(unit, draft.plan_revision_id)
        assert plans["adopted"].base_signature != draft.base_signature
        assert len(draft.units_json) == 3


def test_an_adopted_plan_on_a_fully_removed_revision_is_copied_without_units(performance):
    p, f = performance, performance.f
    with f.case.database.immediate_session() as unit:
        for key in p.ids:
            unit.get_one(m.GenerationSegment, key).removed = True
    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    (row,) = adopted_rows(snapshot)
    assert row["units_json"] == [] and row["manual_annotations_json"] == {}
    assert len(json.loads(snapshot.state_json)["rows"]["performance_plans"]) == 1
    copied, _preview = _copy(f, p.source, p.u.alice, p.u.ah, p.u.workspace_id,
        "copy-removed-performance")
    with f.case.database.snapshot_session() as unit:
        (plan,) = unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == copied["source_session_id"]))
        assert plan.status == "adopted" and plan.units_json == []


def adopt(p, plan):
    command(p.u.alice, p.u.ah, p.root, "/" + plan["id"] + "/adopt",
        {"expected_version": plan["version"], "enable": True})


@pytest.mark.parametrize("mode", ["manual", "passive"])
def test_a_pssml_plan_seeded_from_another_plan_is_copied(performance, mode):
    # A pSSML plan made from another plan's entries marks its seeded units as
    # legacy choices, a flag only an XML plan reads; the copy reads past it.
    p, f, u = performance, performance.f, performance.u
    source = command(u.alice, u.ah, p.root, "", {
        "expected_plan_revision_id": p.speech["active_revision_id"], "mode": "manual",
        "annotation_format": "pssml"}, status=201)
    source = command(u.alice, u.ah, p.root, "/" + source["id"], {
        "expected_version": source["version"], "items": [
            {"segment_id": key, "annotation": {**annotation(f"direction {index}"),
                "locked": True}} for index, key in enumerate(p.ids)]}, method="patch")
    adopt(p, source)
    seeded = command(u.alice, u.ah, p.root, "", {
        "expected_plan_revision_id": p.speech["active_revision_id"], "mode": mode,
        "annotation_format": "pssml", "copy_from_id": source["id"]}, status=201)
    with f.case.database.snapshot_session() as unit:
        units = unit.get_one(m.PerformancePlan, seeded["id"]).units_json
    assert [item.get("legacy_annotation") for item in units] == [True] * 3
    adopt(p, body(u.alice.get(p.root + "/" + seeded["id"])))

    snapshot = capture_source(f, p.source)
    assert snapshot.ready, snapshot.blockers
    (frozen,) = adopted_rows(snapshot)
    assert frozen["id"] == seeded["id"]
    assert all("legacy_annotation" not in item for item in frozen["units_json"])
    copied, _preview = _copy(f, p.source, u.alice, u.ah, u.workspace_id, f"copy-pssml-{mode}")
    with f.case.database.snapshot_session() as unit:
        (plan,) = unit.scalars(select(m.PerformancePlan)
            .where(m.PerformancePlan.session_id == copied["source_session_id"]))
        assert plan.status == "adopted"
        assert plan.base_signature == plan_signature(unit, plan.plan_revision_id)
        assert [record["delivery"]["instruction"] for record in
            plan.manual_annotations_json.values()] == [f"direction {index}" for index in range(3)]


def test_stale_draft_and_historical_body_are_not_loaded_or_copied(performance):
    p, f = performance, performance.f
    with f.case.database.immediate_session() as unit:
        for status, signature in (("draft", "stale"), ("superseded", "historical")):
            unit.add(m.PerformancePlan(session_id=p.source["id"],
                plan_revision_id=p.speech["active_revision_id"], base_signature=signature,
                status=status, units_json=[{"unqualified": "old-private-body"}]))
    snapshot = capture_source(f, p.source)
    assert snapshot.ready and "old-private-body" not in snapshot.state_json
    assert len(json.loads(snapshot.state_json)["rows"]["performance_plans"]) == 3


def test_absent_performance_has_one_header_and_zero_bodies(design):
    f = design
    source = null_source(f)
    statements = []

    def observe(_c, _cu, sql, _p, _ctx, _many):
        statements.append(sql)

    event.listen(f.case.database.engine, "before_cursor_execute", observe)
    try:
        with f.case.database.snapshot_session() as unit:
            assert capture_current_performance(unit, rows={"sessions": [source]}, max_rows=0) == ([], 0)
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", observe)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1
    assert not any(key in reads[0] for key in ("units_json", "annotations_json", "settings_json"))
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql in statements)


def test_format14_keeps_frozen_shape_and_rejects_new_family_before_staging():
    state = {"schema_version": 14, "binding_fields": {}, "unsupported_counts": {},
        "destination_connections": [], "rows": {family: [] for family in copy_publication._FIELDS
            if family != "performance_plans"}}
    snapshot = snapshot_for(state)
    original = snapshot.state_json, snapshot.fingerprint
    copy_publication._decode(snapshot)
    assert (snapshot.state_json, snapshot.fingerprint) == original
    forged = {**state, "rows": {**state["rows"], "performance_plans": []}}
    with pytest.raises(ProjectCopyError):
        copy_publication._decode(snapshot_for(forged))
