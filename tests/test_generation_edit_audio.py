"""Editing and regenerating during an immutable run must not lose audio."""
from unittest.mock import patch

import pytest
from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web.jobs import Worker
from pandrator.web.models import GenerationPlanRevision, GenerationRun, Job


@pytest.fixture
def case():
    from tests.test_web_generation_regeneration import GenerationRegenerationTests

    value = GenerationRegenerationTests()
    value.setUp()
    try:
        yield value
    finally:
        value.tearDown()


def page(case):
    response = case.client.get(f"/api/v1/sessions/{case.session_id}/generation-segments")
    assert response.status_code == 200
    return response.get_json()


def edit(case, ordinal, text):
    item = page(case)["items"][ordinal]
    response = case.client.patch(
        f"/api/v1/generation-segments/{item['id']}",
        json={"text": text},
        headers={**case.headers, "If-Match": f'"{item["revision"]}"'},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def execute(case, run, ids=None, operation=None):
    calls = []
    with case._fake_tts(calls, [], batch_size=1) as handlers:
        result = case._run_job(handlers, {
            "generation_run_id": run["id"],
            "segment_ids": ids or [],
            "operation": operation or run["operation"],
        })
    with case.database.session() as session:
        session.get(Job, run["job_id"]).status = "succeeded"
    return result, calls


def test_repeated_edits_keep_stale_take_and_late_regeneration(case):
    original = case._start()
    execute(case, original)
    first = edit(case, 0, "Edited one")
    first_page = page(case)
    assert first["id"] != case.segment_ids[0]
    assert first_page["items"][0]["takes"][0]["status"] == "stale"
    replacement = case._start(operation="regenerate", segment_ids=[first["id"]],
                              speech_plan_revision_id=first_page["plan_revision_id"])
    edit(case, 1, "Edited two")
    current = page(case)
    first_current = current["items"][0]
    assert len(first_current["takes"]) == 1, "A second edit must retain the stale original"
    assert first_current["takes"][0]["artifact_id"]
    execute(case, replacement, [first["id"]], "regenerate")
    current = page(case)
    assert current["plan_revision_id"] != replacement["plan_revision_id"]
    row = current["items"][0]
    assert row["text"] == "Edited one"
    assert row["status"] == "completed"
    assert any(t["is_active"] and t["status"] == "completed" for t in row["takes"])
    assert any(t["status"] == "stale" for t in row["takes"])
    assert current["items"][1]["text"] == "Edited two"


def test_edit_during_synthesis_keeps_old_result_stale_and_other_blocks_current(case):
    run = case._start()
    handlers = case.app.extensions["pandrator"]["workflow_handlers"]
    calls = []

    def synth(text, *_args, **_kwargs):
        calls.append(text)
        if text == "One":
            edit(case, 0, "Updated while one was being spoken")
            assert page(case)["items"][0]["status"] == "stale"
        return AudioSegment.silent(duration=20)

    with patch.object(handlers.tts_providers, "synthesize", side_effect=synth):
        result = case._run_job(handlers, {"generation_run_id": run["id"]})
    assert result["generated"] == 3
    assert calls == ["One", "Two", "Three"]
    current = page(case)["items"]
    assert current[0]["text"] == "Updated while one was being spoken"
    assert current[0]["status"] == "stale"
    assert current[0]["takes"][0]["status"] == "stale"
    assert all(row["status"] == "completed" for row in current[1:])
    with case.database.session() as session:
        assert not session.get(GenerationRun, run["id"]).pause_requested


def test_standalone_regeneration_can_be_regenerated_again(case):
    original = case._start()
    execute(case, original)
    edited = edit(case, 0, "New words")
    revision = page(case)["plan_revision_id"]
    first = case._start(operation="regenerate", segment_ids=[edited["id"]],
                        speech_plan_revision_id=revision)
    execute(case, first, [edited["id"]], "regenerate")
    again = case._start(operation="regenerate", segment_ids=[edited["id"]],
                        speech_plan_revision_id=revision)
    assert again["plan_revision_id"] == revision
    result, calls = execute(case, again, [edited["id"]], "regenerate")
    assert result["generated"] == 1
    assert calls == ["New words"]


def test_cross_revision_regeneration_yields_and_resumes_original_run(case):
    original = case._start()
    handlers = case.app.extensions["pandrator"]["workflow_handlers"]
    child = None
    selected = None

    def synth(text, *_args, **_kwargs):
        nonlocal child, selected
        if text == "One":
            selected = edit(case, 0, "Replacement one")
            child = case._start(operation="regenerate", segment_ids=[selected["id"]],
                                speech_plan_revision_id=page(case)["plan_revision_id"])
        return AudioSegment.silent(duration=20)

    with patch.object(handlers.tts_providers, "synthesize", side_effect=synth):
        result = case._run_job(handlers, {"generation_run_id": original["id"]})
    assert result["status"] == "paused"
    assert child["plan_revision_id"] != original["plan_revision_id"]
    assert child["output_generation_run_id"] is None
    assert child["resume_source_on_completion"]
    with case.database.session() as session:
        payload = dict(session.get(Job, child["job_id"]).payload_json)
        assert payload["auto_resume_source_generation_run_id"] == original["id"]
    with case._fake_tts([], [], batch_size=1) as handlers:
        replacement_result = case._run_job(handlers, payload)
    assert replacement_result["resumed_source_job_id"]
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == "queued"
        assert not root.pause_requested
    assert page(case)["items"][0]["text"] == "Replacement one"
    assert page(case)["items"][0]["status"] == "completed"
    result, calls = execute(case, original, operation="resume")
    assert calls == ["Two", "Three"]
    assert all(row["status"] == "completed" for row in page(case)["items"])


def test_explicit_pause_revokes_cross_revision_auto_resume(case):
    original = case._start()
    edited = edit(case, 0, "Edited words")
    child = case._start(operation="regenerate", segment_ids=[edited["id"]],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    generation = case.app.extensions["pandrator"]["generation"]
    generation.request_pause(original["id"])
    with case.database.session() as session:
        assert not session.get(GenerationRun, child["id"]).resume_source_on_completion
        assert "auto_resume_source_generation_run_id" not in session.get(Job, child["job_id"]).payload_json


def test_a_later_text_edit_cannot_be_marked_current_by_an_older_request(case):
    original = case._start()
    execute(case, original)
    changed = edit(case, 0, "First correction")
    child = case._start(operation="regenerate", segment_ids=[changed["id"]],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    edit(case, 0, "Second correction")
    execute(case, child, [changed["id"]], "regenerate")
    row = page(case)["items"][0]
    assert row["text"] == "Second correction"
    assert row["status"] == "stale"
    assert all(take["status"] == "stale" for take in row["takes"])


def test_regeneration_follows_an_unrelated_edit_and_selects_the_new_take(case):
    original = case._start()
    execute(case, original)
    edit(case, 1, "Changed second sentence")
    row = page(case)["items"][0]
    old_artifact = next(take for take in row["takes"] if take["is_active"])["artifact_id"]
    child = case._start(operation="regenerate", segment_ids=[row["id"]],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    edit(case, 2, "Changed third sentence")
    execute(case, child, [row["id"]], "regenerate")
    row = page(case)["items"][0]
    selected = next(take for take in row["takes"] if take["is_active"])
    assert selected["artifact_id"] != old_artifact
    assert selected["status"] == "completed"


def test_manual_take_selection_after_queuing_is_not_overwritten(case):
    original = case._start()
    execute(case, original)
    edit(case, 1, "Changed second sentence")
    row = page(case)["items"][0]
    child = case._start(operation="regenerate", segment_ids=[row["id"]],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    edit(case, 2, "Changed third sentence")
    current = page(case)["items"][0]
    old = next(take for take in current["takes"] if take["is_active"])
    response = case.client.post(
        f"/api/v1/generation-segments/{current['id']}/takes/{old['id']}/select",
        headers={**case.headers, "If-Match": f'"{current["revision"]}"'},
    )
    assert response.status_code == 200, response.get_json()
    execute(case, child, [row["id"]], "regenerate")
    current = page(case)["items"][0]
    assert len(current["takes"]) == 2
    assert next(take for take in current["takes"] if take["is_active"])["id"] == old["id"]


def test_inheritance_is_idempotent_and_does_not_cross_real_restore(case):
    from pandrator.web.generation_edit_audio import inherit_edit_copy_audio

    original = case._start()
    execute(case, original)
    edit(case, 0, "New one")
    revision = page(case)["plan_revision_id"]
    with case.database.immediate_session() as session:
        assert inherit_edit_copy_audio(session, revision) == 0
        copied = session.get(GenerationPlanRevision, revision)
        copied.operation_json = {**copied.operation_json, "reason": "user_restore"}
        assert inherit_edit_copy_audio(session, revision) == 0


def test_cancel_queued_replacement_releases_temporary_pause(case):
    original = case._start()
    edited = edit(case, 0, "Replacement")
    child = case._start(operation="regenerate", segment_ids=[edited["id"]],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    generation = case.app.extensions["pandrator"]["generation"]
    result = generation.cancel(child["id"])
    assert result["status"] == "canceled"
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == "queued"
        assert not root.pause_requested
        assert not session.get(GenerationRun, child["id"]).resume_source_on_completion


def test_cancel_last_replacement_transfers_resume_to_waiting_sibling(case):
    original = case._start()
    edited = edit(case, 0, "Replacement")
    revision = page(case)["plan_revision_id"]
    first = case._start(operation="regenerate", segment_ids=[edited["id"]],
                        speech_plan_revision_id=revision)
    second = case._start(operation="regenerate", segment_ids=[edited["id"]],
                         speech_plan_revision_id=revision)
    generation = case.app.extensions["pandrator"]["generation"]
    generation.cancel(second["id"])
    with case.database.session() as session:
        assert session.get(GenerationRun, original["id"]).pause_requested
        assert session.get(GenerationRun, first["id"]).resume_source_on_completion
        payload = session.get(Job, first["job_id"]).payload_json
        assert payload["auto_resume_source_generation_run_id"] == original["id"]
        assert not session.get(GenerationRun, second["id"]).resume_source_on_completion


@pytest.mark.parametrize("edited_plan", [False, True])
@pytest.mark.parametrize("terminal", ["failed", "canceled", "partial"])
@pytest.mark.parametrize("explicit_pause", [False, True])
def test_terminal_replacement_waits_for_resumed_sibling(
    case, edited_plan, terminal, explicit_pause
):
    original = case._start()
    selected_id = edit(case, 0, "Replacement")["id"] if edited_plan else case.segment_ids[0]
    revision = page(case)["plan_revision_id"]
    first = case._start(operation="regenerate", segment_ids=[selected_id],
                        speech_plan_revision_id=revision)
    services = case.app.extensions["pandrator"]
    generation = services["generation"]
    handlers = services["workflow_handlers"]
    worker = Worker(handlers.jobs, "s1-resume", {"generation.run": handlers.run_generation})

    # Real queue ordering: root yields; first replacement pauses; resuming it
    # appends its new job behind the still-queued second replacement.
    with case._fake_tts([], [], batch_size=1):
        assert worker.run_once()
        generation.request_pause(first["id"])
        assert worker.run_once()
    second = case._start(operation="regenerate", segment_ids=[selected_id],
                         speech_plan_revision_id=revision)
    resumed_first = generation.resume(first["id"])
    if explicit_pause:
        generation.request_pause(original["id"])
    if terminal == "failed":
        with patch.object(handlers.tts_providers, "synthesize",
                          side_effect=RuntimeError("S1 synthesis failure")):
            assert worker.run_once()
    elif terminal == "canceled":
        generation.cancel(second["id"])
    else:
        with case._fake_tts([], [], batch_size=1):
            assert worker.run_once()

    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        sibling = session.get(GenerationRun, first["id"])
        finished = session.get(GenerationRun, second["id"])
        assert session.get(Job, second["job_id"]).status == (
            "succeeded" if terminal == "partial" else terminal
        )
        assert finished.status == terminal
        assert not finished.resume_source_on_completion
        assert "auto_resume_source_generation_run_id" not in session.get(
            Job, second["job_id"]
        ).payload_json
        assert root.status == "paused"
        assert root.pause_requested
        assert root.job_id == original["job_id"]
        assert sibling.status == "queued"
        assert sibling.resume_source_on_completion is not explicit_pause
        payload = session.get(Job, resumed_first["job_id"]).payload_json
        assert payload["segment_ids"] == [selected_id]
        assert payload.get("auto_resume_source_generation_run_id") == (
            None if explicit_pause else original["id"]
        )
        # Scheduling must not rewrite the replacement's immutable output lineage.
        assert sibling.source_generation_run_id == first["source_generation_run_id"]
        assert sibling.output_generation_run_id == first["output_generation_run_id"]
        assert sibling.plan_revision_id == revision

    calls = []
    with case._fake_tts(calls, [], batch_size=1):
        assert worker.run_once()
    # A successful newer replacement already supplied the shared output take.
    assert calls == ([] if terminal == "partial" else ["Replacement" if edited_plan else "One"])
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == ("paused" if explicit_pause else "queued")
        assert root.pause_requested is explicit_pause
        assert not session.get(GenerationRun, first["id"]).resume_source_on_completion
        assert "auto_resume_source_generation_run_id" not in session.get(
            Job, resumed_first["job_id"]
        ).payload_json
        assert session.get(Job, root.job_id).payload_json["segment_ids"] == []
        root_job_id = root.job_id

    # Duplicate terminal callbacks cannot enqueue a second resume.
    for child in (second, first):
        assert handlers._resume_generation_after_regeneration(child["id"], original["id"]) is None
    with case.database.session() as session:
        root_jobs = list(session.scalars(select(Job).where(
            Job.payload_json["generation_run_id"].as_string() == original["id"]
        )))
        assert len(root_jobs) == (1 if explicit_pause else 2)
        assert session.get(GenerationRun, original["id"]).job_id == root_job_id
    with case._fake_tts([], [], batch_size=1):
        assert worker.run_once() is not explicit_pause
    row = page(case)["items"][0]
    assert row["id"] == selected_id
    assert row["text"] == ("Replacement" if edited_plan else "One")
    assert row["status"] == "completed"


@pytest.mark.parametrize("edited_plan", [False, True])
@pytest.mark.parametrize("explicit_pause", [False, True])
def test_running_sibling_consumes_permission_transferred_after_claim(case, edited_plan, explicit_pause):
    original = case._start()
    selected_id = edit(case, 0, "Replacement")["id"] if edited_plan else case.segment_ids[0]
    revision = page(case)["plan_revision_id"]
    first = case._start(operation="regenerate", segment_ids=[selected_id],
                        speech_plan_revision_id=revision)
    second = case._start(operation="regenerate", segment_ids=[selected_id],
                         speech_plan_revision_id=revision)
    services = case.app.extensions["pandrator"]
    generation = services["generation"]
    handlers = services["workflow_handlers"]
    worker = Worker(handlers.jobs, "s1-running", {"generation.run": handlers.run_generation})
    with case._fake_tts([], [], batch_size=1):
        assert worker.run_once()

    def cancel_waiting_owner(*_args, **_kwargs):
        # The worker has already loaded a payload without resume permission.
        with case.database.session() as session:
            assert session.get(Job, first["job_id"]).status == "running"
            assert "auto_resume_source_generation_run_id" not in session.get(
                Job, first["job_id"]
            ).payload_json
        generation.cancel(second["id"])
        with case.database.session() as session:
            assert session.get(GenerationRun, first["id"]).resume_source_on_completion
            assert session.get(Job, first["job_id"]).payload_json[
                "auto_resume_source_generation_run_id"
            ] == original["id"]
        if explicit_pause:
            generation.request_pause(original["id"])
        return AudioSegment.silent(duration=20)

    with patch.object(handlers.tts_providers, "synthesize", side_effect=cancel_waiting_owner):
        assert worker.run_once()
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == ("paused" if explicit_pause else "queued")
        assert root.pause_requested is explicit_pause
        assert not session.get(GenerationRun, first["id"]).resume_source_on_completion
        assert "auto_resume_source_generation_run_id" not in session.get(
            Job, first["job_id"]
        ).payload_json
        assert session.get(Job, root.job_id).payload_json["segment_ids"] == []
    assert handlers._resume_generation_after_regeneration(first["id"], original["id"]) is None


@pytest.mark.parametrize("legacy_chain", [False, True])
def test_nested_revocation_clears_marker_even_when_it_names_another_ancestor(case, legacy_chain):
    original = case._start()
    selected_id = case.segment_ids[0] if legacy_chain else edit(case, 0, "Replacement")["id"]
    revision = page(case)["plan_revision_id"]
    first = case._start(operation="regenerate", segment_ids=[selected_id],
                        speech_plan_revision_id=revision)
    second = case._start(operation="regenerate", segment_ids=[selected_id],
                         speech_plan_revision_id=revision)
    if legacy_chain:
        # Persisted shape from versions that chained regeneration children:
        # root <- first <- second, each child temporarily interrupting its parent.
        # Current start APIs intentionally flatten this shape, so seed only the
        # historical links and permission state needed to exercise revocation.
        with case.database.session() as session:
            root = session.get(GenerationRun, original["id"])
            root.status = "paused"
            session.get(Job, original["job_id"]).status = "succeeded"
            parent = session.get(GenerationRun, first["id"])
            parent.status = "paused"
            parent.pause_requested = True
            parent.resume_source_on_completion = True
            parent_job = session.get(Job, first["job_id"])
            parent_job.status = "succeeded"
            parent_job.payload_json = {
                **parent_job.payload_json,
                "auto_resume_source_generation_run_id": original["id"],
            }
            child = session.get(GenerationRun, second["id"])
            child.source_generation_run_id = first["id"]
            child.output_generation_run_id = first["id"]
            child.settings_snapshot_json = {
                key: value for key, value in child.settings_snapshot_json.items()
                if key != "interrupted_generation_run_id"
            }
            child_job = session.get(Job, second["job_id"])
            child_job.payload_json = {
                **child_job.payload_json,
                "auto_resume_source_generation_run_id": first["id"],
            }
    pause_id = original["id"] if legacy_chain else first["id"]
    source_id = first["id"] if legacy_chain else original["id"]
    services = case.app.extensions["pandrator"]
    with case.database.session() as session:
        assert session.get(GenerationRun, second["id"]).resume_source_on_completion
        stale_payload = dict(session.get(Job, second["job_id"]).payload_json)
        assert stale_payload["auto_resume_source_generation_run_id"] == source_id
        job_ids = set(session.scalars(select(Job.id)))
    services["generation"].request_pause(pause_id)
    # Even a worker holding the old payload cannot undo explicit revocation.
    assert services["workflow_handlers"]._resume_generation_after_regeneration(
        second["id"], stale_payload["auto_resume_source_generation_run_id"]
    ) is None
    with case.database.session() as session:
        assert not session.get(GenerationRun, second["id"]).resume_source_on_completion
        assert session.get(GenerationRun, pause_id).pause_requested
        assert set(session.scalars(select(Job.id))) == job_ids
        assert "auto_resume_source_generation_run_id" not in session.get(
            Job, second["job_id"]
        ).payload_json


def test_failed_replacement_resumes_parent_and_keeps_stale_audio(case):
    original = case._start()
    handlers = case.app.extensions["pandrator"]["workflow_handlers"]
    child = None

    def synth(text, *_args, **_kwargs):
        nonlocal child
        if text == "One":
            selected = edit(case, 0, "Replacement one")
            child = case._start(operation="regenerate", segment_ids=[selected["id"]],
                                speech_plan_revision_id=page(case)["plan_revision_id"])
        return AudioSegment.silent(duration=20)

    with patch.object(handlers.tts_providers, "synthesize", side_effect=synth):
        result = case._run_job(handlers, {"generation_run_id": original["id"]})
    assert result["status"] == "paused"
    before = page(case)["items"][0]["takes"][0]["artifact_id"]
    with case.database.session() as session:
        payload = dict(session.get(Job, child["job_id"]).payload_json)
    with patch.object(handlers.tts_providers, "synthesize", side_effect=RuntimeError("test failed synthesis")):
        with pytest.raises(RuntimeError, match="test failed synthesis"):
            case._run_job(handlers, payload)
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == "queued"
        assert not root.pause_requested
    assert page(case)["items"][0]["takes"][0]["artifact_id"] == before


def test_cancel_replacement_does_not_undo_explicit_pause(case):
    original = case._start()
    edited = edit(case, 0, "Replacement")
    child = case._start(operation="regenerate", segment_ids=[edited["id"]],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    generation = case.app.extensions["pandrator"]["generation"]
    generation.request_pause(original["id"])
    generation.cancel(child["id"])
    with case.database.session() as session:
        assert session.get(GenerationRun, original["id"]).pause_requested


@pytest.mark.parametrize("edited_plan", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_paused_replacement_retains_parent_resume_permission(case, edited_plan, fails):
    original = case._start()
    selected_id = edit(case, 0, "Replacement")["id"] if edited_plan else case.segment_ids[0]
    child = case._start(operation="regenerate", segment_ids=[selected_id],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    result, _ = execute(case, original)
    assert result["status"] == "paused"
    generation = case.app.extensions["pandrator"]["generation"]
    generation.request_pause(child["id"])
    result, _ = execute(case, child, [selected_id])
    assert result["status"] == "paused"

    resumed = generation.resume(child["id"])
    with case.database.session() as session:
        payload = dict(session.get(Job, resumed["job_id"]).payload_json)
    assert payload["segment_ids"] == [selected_id]
    assert payload.get("auto_resume_source_generation_run_id") == original["id"]
    handlers = case.app.extensions["pandrator"]["workflow_handlers"]
    if fails:
        with patch.object(handlers.tts_providers, "synthesize", side_effect=RuntimeError("test failure")):
            with pytest.raises(RuntimeError, match="test failure"):
                case._run_job(handlers, payload)
    else:
        with case._fake_tts([], [], batch_size=1) as handlers:
            case._run_job(handlers, payload)
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == "queued"
        assert not root.pause_requested
        assert not session.get(GenerationRun, child["id"]).resume_source_on_completion


def test_resuming_replacement_does_not_restore_revoked_parent_permission(case):
    original = case._start()
    selected_id = edit(case, 0, "Replacement")["id"]
    child = case._start(operation="regenerate", segment_ids=[selected_id],
                        speech_plan_revision_id=page(case)["plan_revision_id"])
    assert execute(case, original)[0]["status"] == "paused"
    generation = case.app.extensions["pandrator"]["generation"]
    generation.request_pause(child["id"])
    assert execute(case, child, [selected_id])[0]["status"] == "paused"
    generation.request_pause(original["id"])

    resumed = generation.resume(child["id"])
    with case.database.session() as session:
        payload = dict(session.get(Job, resumed["job_id"]).payload_json)
    assert "auto_resume_source_generation_run_id" not in payload
    with case._fake_tts([], [], batch_size=1) as handlers:
        case._run_job(handlers, payload)
    with case.database.session() as session:
        root = session.get(GenerationRun, original["id"])
        assert root.status == "paused"
        assert root.pause_requested


def test_interrupting_a_targeted_output_does_not_expand_resume_to_full_plan(case):
    original = case._start()
    execute(case, original)
    edit(case, 0, "Edited one")
    current = page(case)
    requested = [row["id"] for row in current["items"][:2]]
    targeted = case._start(operation="regenerate", segment_ids=requested,
                          speech_plan_revision_id=current["plan_revision_id"])
    handlers = case.app.extensions["pandrator"]["workflow_handlers"]
    child = None

    def synth(text, *_args, **_kwargs):
        nonlocal child
        if child is None:
            child = case._start(operation="regenerate", segment_ids=[requested[0]],
                                speech_plan_revision_id=current["plan_revision_id"])
        return AudioSegment.silent(duration=20)

    with case.database.session() as session:
        payload = dict(session.get(Job, targeted["job_id"]).payload_json)
    with patch.object(handlers.tts_providers, "synthesize", side_effect=synth):
        result = case._run_job(handlers, payload)
    assert result["status"] == "paused"
    with case.database.session() as session:
        child_payload = dict(session.get(Job, child["job_id"]).payload_json)
    with case._fake_tts([], [], batch_size=1) as handlers:
        result = case._run_job(handlers, child_payload)
    assert result["resumed_source_job_id"]
    with case.database.session() as session:
        resume_payload = dict(session.get(Job, result["resumed_source_job_id"]).payload_json)
    assert resume_payload["segment_ids"] == requested
    calls = []
    with case._fake_tts(calls, [], batch_size=1) as handlers:
        case._run_job(handlers, resume_payload)
    assert calls == ["Two"], "The unrequested third block must not be synthesized"
